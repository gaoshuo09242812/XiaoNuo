"""Durable reminder audio outbox. No transport, synthesis or automatic playback.

Claim before preparing audio; start BEFORE sending the first audio packet.
Only an exact device completion may mark played. An uncertain send is never
automatically repeated, including after process restart.
"""
from datetime import timedelta,timezone
from uuid import uuid4

from .behavior import QuietMode
from .budgeting import now
from .errors import XiaoNuoError
from .storage import OWNER

def stamp(value):
    if value.tzinfo is None or value.utcoffset() is None:raise ValueError('Aware clock required')
    return value.astimezone(timezone.utc).isoformat(timespec='seconds')

class ReminderAudioOutbox:
    def __init__(self,store,clock=now):
        self.store,self.ledger,self.clock=store,store.ledger,clock
        with self.ledger.connect() as db:
            db.execute('''CREATE TABLE IF NOT EXISTS reminder_audio_outbox(
                event_id TEXT PRIMARY KEY, owner_id TEXT NOT NULL,
                state TEXT NOT NULL DEFAULT 'pending', device_id TEXT,
                claim_token TEXT, lease_until TEXT, expires_at TEXT NOT NULL,
                expected_samples INTEGER, attempts INTEGER NOT NULL DEFAULT 0,
                updated_at TEXT NOT NULL)''')

    def enqueue(self,db,event_id,owner,due,current):
        # Same transaction as the durable console event; no retroactive import.
        db.execute('''INSERT OR IGNORE INTO reminder_audio_outbox
            (event_id,owner_id,expires_at,updated_at) VALUES (?,?,?,?)''',
            (event_id,owner,stamp(due+timedelta(minutes=15)),stamp(current)))

    def _eligible(self,row,at):
        controls=self.store.controls(row['owner_id'])
        return (not controls['quiet'] and controls['speech_enabled'] and controls['volume']>0
                and QuietMode(self.store).allows_proactive('reminder',quiet_exception=bool(row['quiet_exception']),at=at))

    def _row(self,db,event,owner):
        return db.execute('''SELECT a.*,e.state AS event_state,r.text,r.quiet_exception,
            d.revoked_at AS revoked,d.id AS bound_device FROM reminder_audio_outbox a
            JOIN execution_records e ON e.id=a.event_id AND e.owner_id=a.owner_id
            JOIN reminders r ON r.id=e.resource_id AND r.owner_id=e.owner_id
            LEFT JOIN devices d ON d.id=a.device_id AND d.owner_id=a.owner_id
            WHERE a.event_id=? AND a.owner_id=?''',(event,owner)).fetchone()

    def claim(self,device_id,owner=OWNER):
        at=self.clock();moment=stamp(at)
        with self.ledger.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            try:
                if not db.execute('SELECT 1 FROM devices WHERE id=? AND owner_id=? AND revoked_at IS NULL',(device_id,owner)).fetchone():
                    raise XiaoNuoError('REMINDER_AUDIO_DEVICE_UNAVAILABLE',409)
                db.execute("UPDATE reminder_audio_outbox SET state='uncertain',updated_at=? WHERE owner_id=? AND state='playing' AND lease_until<=?",(moment,owner,moment))
                db.execute("UPDATE reminder_audio_outbox SET state='pending',claim_token=NULL,device_id=NULL,updated_at=? WHERE owner_id=? AND state='claimed' AND lease_until<=?",(moment,owner,moment))
                db.execute("UPDATE reminder_audio_outbox SET state='expired',updated_at=? WHERE owner_id=? AND state IN ('pending','claimed') AND expires_at<=?",(moment,owner,moment))
                db.execute("UPDATE reminder_audio_outbox SET state='cancelled',updated_at=? WHERE owner_id=? AND state IN ('pending','claimed') AND event_id IN (SELECT id FROM execution_records WHERE owner_id=? AND state!='available')",(moment,owner,owner))
                selected=None
                for item in db.execute("SELECT event_id FROM reminder_audio_outbox WHERE owner_id=? AND state='pending' ORDER BY expires_at,event_id LIMIT 100",(owner,)).fetchall():
                    row=self._row(db,item['event_id'],owner)
                    if row is not None and self._eligible(row,at):selected=row;break
                if selected is None:db.execute('COMMIT');return None
                token=str(uuid4());event=selected['event_id']
                db.execute("UPDATE reminder_audio_outbox SET state='claimed',device_id=?,claim_token=?,lease_until=?,attempts=attempts+1,updated_at=? WHERE event_id=?",(device_id,token,stamp(at+timedelta(seconds=120)),moment,event))
                db.execute('COMMIT')
                return {'event_id':event,'claim_token':token,'text':selected['text']}
            except BaseException:db.execute('ROLLBACK');raise

    def _allowed(self,row,token,at):
        return bool(row and row['claim_token']==token and row['state'] in {'claimed','playing'}
                    and row['event_state']=='available' and row['bound_device'] and row['revoked'] is None
                    and row['lease_until']>stamp(at)
                    and (row['state']=='playing' or row['expires_at']>stamp(at)) and self._eligible(row,at))

    def allowed(self,event,token,owner=OWNER):
        with self.ledger.connect() as db:return self._allowed(self._row(db,event,owner),token,self.clock())

    def start(self,event,token,expected_samples,owner=OWNER):
        if type(expected_samples) is not int or not 0<expected_samples<=960000:
            raise XiaoNuoError('REMINDER_AUDIO_INVALID_SAMPLES',422)
        at=self.clock()
        with self.ledger.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            try:
                row=self._row(db,event,owner)
                if not self._allowed(row,token,at) or row['state']!='claimed':
                    raise XiaoNuoError('REMINDER_AUDIO_STALE_OR_BLOCKED',409)
                db.execute("UPDATE reminder_audio_outbox SET state='playing',expected_samples=?,lease_until=?,updated_at=? WHERE event_id=?",(expected_samples,stamp(at+timedelta(minutes=10)),stamp(at),event))
                db.execute('COMMIT')
            except BaseException:db.execute('ROLLBACK');raise

    def finish(self,event,token,*,completed,played_samples,owner=OWNER):
        if type(completed) is not bool or type(played_samples) is not int or played_samples<0:
            raise XiaoNuoError('REMINDER_AUDIO_INVALID_COMPLETION',422)
        at=self.clock()
        with self.ledger.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            try:
                row=self._row(db,event,owner)
                if not row or row['claim_token']!=token or row['state']!='playing':
                    raise XiaoNuoError('REMINDER_AUDIO_STALE_COMPLETION',409)
                state='played' if self._allowed(row,token,at) and completed and row['expected_samples']==played_samples else 'uncertain'
                db.execute('UPDATE reminder_audio_outbox SET state=?,updated_at=? WHERE event_id=?',(state,stamp(at),event))
                db.execute('COMMIT');return state
            except BaseException:db.execute('ROLLBACK');raise

    def abandon(self,event,token,owner=OWNER):
        # A failed preparation is visible in the console; never retry forever.
        with self.ledger.connect() as db:
            db.execute("UPDATE reminder_audio_outbox SET state=CASE WHEN state='playing' THEN 'uncertain' ELSE 'failed' END,updated_at=? WHERE event_id=? AND owner_id=? AND claim_token=? AND state IN ('claimed','playing')",(stamp(self.clock()),event,owner,token))
