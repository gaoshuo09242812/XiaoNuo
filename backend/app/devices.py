"""Owner-controlled USB provisioning; credentials never grant console access."""
import hashlib
import hmac
import re
import secrets
import threading
from uuid import uuid4

from .budgeting import now
from .errors import XiaoNuoError
from .storage import OWNER


class DeviceRegistry:
    def __init__(self,store,bridge):
        self.store,self.bridge=store,bridge
        self._provision_lock=threading.Lock()
        with store.ledger.connect() as db:
            columns={row['name'] for row in db.execute('PRAGMA table_info(devices)')}
            for name,declaration in {
                'hardware_id':'TEXT','firmware':"TEXT NOT NULL DEFAULT ''",
                'provisioning_state':"TEXT NOT NULL DEFAULT 'legacy'",
                'audio_feedback':"TEXT NOT NULL DEFAULT 'unverified'",'feedback_at':'TEXT',
            }.items():
                if name not in columns:db.execute(f'ALTER TABLE devices ADD COLUMN {name} {declaration}')
            db.execute('CREATE UNIQUE INDEX IF NOT EXISTS devices_active_hardware ON devices(hardware_id) WHERE revoked_at IS NULL AND hardware_id IS NOT NULL')

    @staticmethod
    def public(row):
        result=dict(row)
        result['credential_present']=bool(result.pop('token_hash'))
        result.pop('owner_id',None)
        result['network_client_enabled']=False
        return result

    def list(self):
        with self.store.ledger.connect() as db:
            rows=db.execute('SELECT * FROM devices WHERE owner_id=? ORDER BY created_at DESC LIMIT 100',(OWNER,)).fetchall()
        return [self.public(row) for row in rows]

    def get(self,identifier):
        with self.store.ledger.connect() as db:
            row=db.execute('SELECT * FROM devices WHERE id=? AND owner_id=?',(identifier,OWNER)).fetchone()
        if not row:raise XiaoNuoError('DEVICE_BINDING_NOT_FOUND',404)
        return self.public(row)

    def enroll(self,name='小糯'):
        if not isinstance(name,str) or not 1<=len(name.strip())<=40:
            raise XiaoNuoError('INVALID_DEVICE_NAME',422)
        if not self._provision_lock.acquire(blocking=False):raise XiaoNuoError('DEVICE_PROVISIONING_BUSY',409)
        identifier=None
        try:
            current=self.bridge.status()
            if not current['connected'] or current['paused']:raise XiaoNuoError('DEVICE_NOT_CONNECTED')
            info=current['info'];hardware=info.get('device_id','')
            if not re.fullmatch(r'esp32-[0-9a-f]{12}',hardware):raise XiaoNuoError('DEVICE_FIRMWARE_NEEDS_UPDATE')
            with self.store.ledger.connect() as db:
                existing=db.execute('SELECT * FROM devices WHERE hardware_id=? AND revoked_at IS NULL',(hardware,)).fetchone()
            if existing and existing['owner_id']!=OWNER:raise XiaoNuoError('DEVICE_OWNED_BY_ANOTHER_USER',409)
            if existing and existing['provisioning_state']=='bound' and info.get('binding_id')==existing['id']:
                return self.public(existing)
            identifier=str(uuid4());token=secrets.token_hex(32);moment=now().isoformat()
            digest=hashlib.sha256(token.encode('ascii')).hexdigest()
            with self.store.ledger.connect() as db:
                db.execute('BEGIN IMMEDIATE')
                try:
                    db.execute("UPDATE devices SET revoked_at=?,token_hash='',provisioning_state='superseded' WHERE hardware_id=? AND revoked_at IS NULL",(moment,hardware))
                    db.execute("INSERT INTO devices(id,owner_id,name,token_hash,created_at,state,hardware_id,firmware,provisioning_state) VALUES (?,?,?,?,?,'PENDING',?,?,'pending')",
                               (identifier,OWNER,name.strip(),digest,moment,hardware,info['firmware']))
                    db.execute('COMMIT')
                except BaseException:
                    db.execute('ROLLBACK');raise
            try:
                result=self.bridge.provision(identifier,token,hardware)
                if result.get('binding_id')!=identifier or result.get('credential_saved') is not True:
                    raise XiaoNuoError('DEVICE_PROVISIONING_UNCONFIRMED')
                with self.store.ledger.connect() as db:
                    updated=db.execute("UPDATE devices SET provisioning_state='bound',state='BOUND_USB',last_seen=? WHERE id=? AND revoked_at IS NULL",
                                       (now().isoformat(),identifier))
                    if updated.rowcount!=1:raise XiaoNuoError('DEVICE_PROVISIONING_REVOKED',409)
            except Exception:
                with self.store.ledger.connect() as db:
                    db.execute("UPDATE devices SET provisioning_state='failed',state='OFFLINE',token_hash='',revoked_at=? WHERE id=?",(now().isoformat(),identifier))
                raise
            finally:token=''
            return self.get(identifier)
        finally:self._provision_lock.release()

    def revoke(self,identifier):
        record=self.get(identifier)
        with self.store.ledger.connect() as db:
            db.execute("UPDATE devices SET revoked_at=COALESCE(revoked_at,?),token_hash='',provisioning_state='revoked',state='OFFLINE' WHERE id=? AND owner_id=?",(now().isoformat(),identifier,OWNER))
        removed=False
        state=self.bridge.status()
        if state['connected'] and state['info'].get('device_id')==record['hardware_id'] and state['info'].get('binding_id')==identifier:
            try:removed=self.bridge.remove_binding(identifier,record['hardware_id']).get('credential_removed') is True
            except XiaoNuoError:pass  # Backend revocation takes effect even if USB is absent.
        return {'revoked':True,'device_credential_removed':removed}

    def feedback(self,identifier,result):
        if result not in {'unverified','heard','no_sound','unclear'}:raise XiaoNuoError('INVALID_AUDIO_FEEDBACK',422)
        with self.store.ledger.connect() as db:
            updated=db.execute('UPDATE devices SET audio_feedback=?,feedback_at=? WHERE id=? AND owner_id=? AND revoked_at IS NULL',(result,now().isoformat(),identifier,OWNER))
        if updated.rowcount!=1:raise XiaoNuoError('DEVICE_BINDING_NOT_FOUND',404)
        return self.get(identifier)

    @staticmethod
    def _verify_device(db,identifier,token,hardware_id):
        if not isinstance(token,str) or not re.fullmatch(r'[0-9a-f]{64}',token):raise XiaoNuoError('DEVICE_UNAUTHORIZED',401)
        digest=hashlib.sha256(token.encode('ascii')).hexdigest()
        row=db.execute('SELECT hardware_id,token_hash FROM devices WHERE id=? AND revoked_at IS NULL AND provisioning_state=\'bound\'',(identifier,)).fetchone()
        if not row or row['hardware_id']!=hardware_id or not hmac.compare_digest(row['token_hash'],digest):
            raise XiaoNuoError('DEVICE_UNAUTHORIZED',401)

    def authorize(self,identifier,token,hardware_id):
        # Read the current binding on every audio frame; never cache permission.
        # Only actual hello/ping heartbeats need to update last_seen on disk.
        with self.store.ledger.connect() as db:
            self._verify_device(db,identifier,token,hardware_id)
        return True

    def heartbeat(self,identifier,token,hardware_id,firmware,state):
        # TLS is required by the route before this is called. Device tokens are
        # independent of the console admin key and cannot access owner data.
        with self.store.ledger.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            try:
                self._verify_device(db,identifier,token,hardware_id)
                db.execute('UPDATE devices SET last_seen=?,state=?,firmware=? WHERE id=?',(now().isoformat(),state,firmware,identifier))
                db.execute('COMMIT')
            except BaseException:
                db.execute('ROLLBACK');raise
        return {'accepted':True,'commands':[],'audio_upload_enabled':False}
