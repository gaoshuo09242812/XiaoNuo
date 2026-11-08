"""Enrollment/auth tests use temporary databases and an explicit fake board."""
import asyncio
from concurrent.futures import ThreadPoolExecutor
import hashlib
import json
from pathlib import Path
import sys
import tempfile
import time
import unittest

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'backend'))
from app.config import Settings
from app.devices import DeviceRegistry
from app.errors import XiaoNuoError
from app.main import create_app
from test_core import request_asgi


class FakeBoard:
    def __init__(self):
        self.hardware='esp32-001122aabbcc';self.binding='';self.token=''
        self.connected=True;self.calls=0;self.unpairs=0;self.fail=False;self.delay=0
    def status(self):
        return {'connected':self.connected,'paused':False,'info':{'device_id':self.hardware,'binding_id':self.binding,'firmware':'0.5.1-diagnostics'}}
    def provision(self,identifier,token,hardware):
        assert hardware==self.hardware
        self.calls+=1;self.binding=identifier;self.token=token
        if self.delay:time.sleep(self.delay)
        if self.fail:raise XiaoNuoError('DEVICE_PROVISIONING_TIMEOUT')
        return {'binding_id':identifier,'credential_saved':True}
    def remove_binding(self,identifier,hardware):
        assert identifier==self.binding and hardware==self.hardware
        self.unpairs+=1;self.binding='';self.token=''
        return {'credential_removed':True}


class DeviceRegistryTests(unittest.TestCase):
    def test_audio_authorization_is_readonly_even_when_database_writes_are_denied(self):
        import sqlite3
        from unittest.mock import patch
        from contextlib import contextmanager
        record=self.registry.enroll();before=self.registry.get(record['id'])
        with self.registry.store.ledger.connect() as db:
            db.set_authorizer(lambda action,*args:sqlite3.SQLITE_DENY if action in {
                sqlite3.SQLITE_INSERT,sqlite3.SQLITE_UPDATE,sqlite3.SQLITE_DELETE} else sqlite3.SQLITE_OK)
            @contextmanager
            def readonly():yield db
            with patch.object(self.registry.store.ledger,'connect',side_effect=readonly):
                self.assertTrue(self.registry.authorize(self.board.binding,self.board.token,self.board.hardware))
        self.assertEqual(self.registry.get(record['id']),before)

    def test_audio_authorization_rechecks_revocation_rotation_and_binding_on_each_call(self):
        record=self.registry.enroll()
        args=(self.board.binding,self.board.token,self.board.hardware)
        self.assertTrue(self.registry.authorize(*args))
        for column,value in [('revoked_at','revoked'),('token_hash','0'*64),('hardware_id','other'),('provisioning_state','pending')]:
            with self.subTest(column=column):
                with self.registry.store.ledger.connect() as db:
                    old=db.execute('SELECT '+column+' FROM devices WHERE id=?',(record['id'],)).fetchone()[0]
                    db.execute('UPDATE devices SET '+column+'=? WHERE id=?',(value,record['id']))
                with self.assertRaisesRegex(XiaoNuoError,'DEVICE_UNAUTHORIZED'):self.registry.authorize(*args)
                with self.registry.store.ledger.connect() as db:db.execute('UPDATE devices SET '+column+'=? WHERE id=?',(old,record['id']))
        self.assertTrue(self.registry.authorize(*args))

    def setUp(self):
        directory=tempfile.TemporaryDirectory();self.addCleanup(directory.cleanup)
        self.settings=Settings(root=Path(directory.name),admin_token='admin-test-'+40*'x')
        self.app=create_app(self.settings);self.registry=self.app.state.devices
        self.board=FakeBoard();self.registry.bridge=self.board

    def beat(self,identifier=None,token=None):
        return self.registry.heartbeat(identifier or self.board.binding,token or self.board.token,self.board.hardware,'0.5.1-diagnostics','IDLE')

    def test_confirmed_usb_enrollment_is_idempotent_and_stores_only_key_digest(self):
        record=self.registry.enroll('小糯')
        self.assertEqual(record['provisioning_state'],'bound')
        self.assertTrue(record['credential_present'])
        self.assertFalse(record['network_client_enabled'])
        self.assertEqual(record['audio_feedback'],'unverified')
        self.assertNotIn('token_hash',record)
        self.assertNotIn(self.board.token,json.dumps(record))
        with self.registry.store.ledger.connect() as db:
            stored=db.execute('SELECT token_hash FROM devices WHERE id=?',(record['id'],)).fetchone()[0]
        self.assertEqual(stored,hashlib.sha256(self.board.token.encode()).hexdigest())
        self.assertEqual(self.registry.enroll()['id'],record['id'])
        self.assertEqual(self.board.calls,1)
        self.assertTrue(self.beat()['accepted'])

    def test_ambiguous_usb_result_does_not_activate_credential_and_retry_rotates(self):
        self.board.fail=True
        with self.assertRaisesRegex(XiaoNuoError,'DEVICE_PROVISIONING_TIMEOUT'):self.registry.enroll()
        old_id,old_token=self.board.binding,self.board.token
        with self.assertRaisesRegex(XiaoNuoError,'DEVICE_UNAUTHORIZED'):self.beat(old_id,old_token)
        self.assertEqual(self.registry.list()[0]['provisioning_state'],'failed')
        self.assertFalse(self.registry.list()[0]['credential_present'])
        self.board.fail=False;record=self.registry.enroll()
        self.assertNotEqual(record['id'],old_id);self.assertNotEqual(self.board.token,old_token)
        self.assertTrue(self.beat()['accepted'])
        with self.assertRaisesRegex(XiaoNuoError,'DEVICE_UNAUTHORIZED'):self.beat(old_id,old_token)

    def test_offline_revocation_immediately_disables_backend_credential(self):
        record=self.registry.enroll();self.board.connected=False
        result=self.registry.revoke(record['id'])
        self.assertTrue(result['revoked']);self.assertFalse(result['device_credential_removed'])
        self.assertEqual(self.board.unpairs,0)
        with self.assertRaisesRegex(XiaoNuoError,'DEVICE_UNAUTHORIZED'):self.beat()

    def test_connected_revocation_removes_only_matching_board_binding(self):
        record=self.registry.enroll()
        self.assertTrue(self.registry.revoke(record['id'])['device_credential_removed'])
        self.assertEqual(self.board.unpairs,1)

    def test_wrong_board_and_wrong_key_cannot_report_a_heartbeat(self):
        record=self.registry.enroll()
        with self.assertRaisesRegex(XiaoNuoError,'DEVICE_UNAUTHORIZED'):self.beat(token='0'*64)
        with self.assertRaisesRegex(XiaoNuoError,'DEVICE_UNAUTHORIZED'):
            self.registry.heartbeat(record['id'],self.board.token,'esp32-ffffffffffff','0.5.1','IDLE')

    def test_old_firmware_or_disconnected_board_cannot_be_enrolled(self):
        self.board.hardware=''
        with self.assertRaisesRegex(XiaoNuoError,'DEVICE_FIRMWARE_NEEDS_UPDATE'):self.registry.enroll()
        self.board.connected=False
        with self.assertRaisesRegex(XiaoNuoError,'DEVICE_NOT_CONNECTED'):self.registry.enroll()
        self.assertEqual(self.registry.list(),[]);self.assertEqual(self.board.calls,0)

    def test_concurrent_enrollment_cannot_create_two_active_keys(self):
        self.board.delay=.1
        def attempt():
            try:return self.registry.enroll()['id']
            except XiaoNuoError as error:
                self.assertEqual(error.code,'DEVICE_PROVISIONING_BUSY');return None
        with ThreadPoolExecutor(max_workers=4) as pool:results=list(pool.map(lambda _:attempt(),range(4)))
        self.assertEqual(len({x for x in results if x}),1)
        self.assertEqual(self.board.calls,1);self.assertEqual(len(self.registry.list()),1)

    def test_human_feedback_persists_without_implicitly_enabling_playback(self):
        record=self.registry.enroll();self.registry.feedback(record['id'],'no_sound')
        reopened=DeviceRegistry(self.registry.store,self.board)
        self.assertEqual(reopened.get(record['id'])['audio_feedback'],'no_sound')
        self.assertFalse(self.settings.device_speaker_enabled)
        self.assertEqual(self.board.calls,1)

    def test_personal_data_clear_invalidates_device_auth(self):
        self.registry.enroll();identifier,token=self.board.binding,self.board.token
        self.registry.store.clear_personal_data()
        self.assertEqual(self.registry.list(),[])
        with self.assertRaisesRegex(XiaoNuoError,'DEVICE_UNAUTHORIZED'):self.beat(identifier,token)

    def test_personal_export_includes_binding_without_credentials_or_digests(self):
        record=self.registry.enroll()
        exported=self.registry.store.export()
        self.assertEqual(exported['devices'][0]['id'],record['id'])
        encoded=json.dumps(exported)
        self.assertNotIn(self.board.token,encoded)
        self.assertNotIn(hashlib.sha256(self.board.token.encode()).hexdigest(),encoded)
        self.assertNotIn('token_hash',encoded)

    def test_admin_and_device_credentials_are_scoped_and_network_requires_tls(self):
        async def secure(scope,receive,send):
            await self.app({**scope,'scheme':'https'},receive,send)
        async def scenario():
            status,_=await request_asgi(self.app,'/api/v1/devices/enroll',method='POST',body={})
            self.assertEqual(status,401);self.assertEqual(self.board.calls,0)
            status,record=await request_asgi(self.app,'/api/v1/devices/enroll',method='POST',body={},token=self.settings.admin_token)
            self.assertEqual(status,200)
            status,_=await request_asgi(self.app,'/api/v1/preferences',token=self.board.token)
            self.assertEqual(status,401)
            body={'id':record['id'],'hardware_id':self.board.hardware,'firmware':'0.5.1-diagnostics','state':'IDLE'}
            status,_=await request_asgi(self.app,'/api/v1/device-link/heartbeat',method='POST',body=body,token=self.board.token)
            self.assertEqual(status,403)
            status,result=await request_asgi(secure,'/api/v1/device-link/heartbeat',method='POST',body=body,token=self.board.token)
            self.assertEqual(status,200);self.assertEqual(result['commands'],[])
            status,_=await request_asgi(secure,'/api/v1/device-link/heartbeat',method='POST',body=body,token=self.settings.admin_token)
            self.assertEqual(status,401)
            status,_=await request_asgi(self.app,'/api/v1/devices/'+record['id']+'/audio-feedback',method='PUT',body={'result':'no_sound'},token=self.settings.admin_token)
            self.assertEqual(status,200)
            status,_=await request_asgi(self.app,'/api/v1/devices/'+record['id'],method='DELETE',token=self.settings.admin_token)
            self.assertEqual(status,200)
            status,_=await request_asgi(secure,'/api/v1/device-link/heartbeat',method='POST',body=body,token=self.board.token)
            self.assertEqual(status,401)
        asyncio.run(scenario())
