from concurrent.futures import ThreadPoolExecutor
from datetime import datetime,timedelta,timezone
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from uuid import uuid4

from app.budgeting import UsageLedger
from app.errors import XiaoNuoError
from app.storage import CompanionStore,OWNER
from app.reminders import ReminderService

class ReminderAudioTests(unittest.TestCase):
    def setUp(self):
        tmp=tempfile.TemporaryDirectory();self.addCleanup(tmp.cleanup)
        self.store=CompanionStore(UsageLedger(Path(tmp.name)/'test.db'))
        self.at=datetime(2026,11,5,2,tzinfo=timezone.utc)
        self.service=ReminderService(self.store,clock=lambda:self.at)
        self.device=str(uuid4())
        with self.store.ledger.connect() as db:
            db.execute('INSERT INTO devices(id,owner_id,name,token_hash,created_at) VALUES (?,?,?,?,?)',
                       (self.device,OWNER,'fixture','offline-hash',self.at.isoformat()))

    def due(self,**options):
        record=self.service.create('喝水',self.at+timedelta(seconds=1),**options)
        self.at+=timedelta(seconds=2);self.service.tick()
        return record,self.service.notifications()[0]['id']

    def state(self,event):
        with self.store.ledger.connect() as db:return db.execute('SELECT state FROM reminder_audio_outbox WHERE event_id=?',(event,)).fetchone()[0]

    def test_tick_atomic_and_idempotent_keeps_console_notification(self):
        _,event=self.due();self.service.tick()
        self.assertEqual(self.state(event),'pending');self.assertEqual(len(self.service.notifications()),1)
        with self.store.ledger.connect() as db:self.assertEqual(db.execute('SELECT COUNT(*) FROM reminder_audio_outbox').fetchone()[0],1)

    def test_queue_failure_rolls_back_scheduler_event(self):
        self.service.create('喝水',self.at+timedelta(seconds=1));self.at+=timedelta(seconds=2)
        with patch.object(self.service.audio,'enqueue',side_effect=OSError('fixture')):
            with self.assertRaises(OSError):self.service.tick()
        self.assertEqual(self.service.notifications(),[])
        self.assertEqual(self.service.tick(),1)

    def test_concurrent_devices_only_one_claim(self):
        self.due()
        with ThreadPoolExecutor(max_workers=6) as pool:results=list(pool.map(lambda _:self.service.audio.claim(self.device),range(12)))
        self.assertEqual(sum(r is not None for r in results),1)

    def test_restart_before_send_reclaims_with_new_token_after_lease(self):
        _,event=self.due();first=self.service.audio.claim(self.device)
        restarted=ReminderService(self.store,clock=lambda:self.at)
        self.assertIsNone(restarted.audio.claim(self.device))
        self.at+=timedelta(seconds=121);second=restarted.audio.claim(self.device)
        self.assertNotEqual(first['claim_token'],second['claim_token'])
        with self.assertRaises(XiaoNuoError):self.service.audio.start(event,first['claim_token'],16000)

    def test_send_unknown_after_restart_never_repeats(self):
        _,event=self.due();claim=self.service.audio.claim(self.device)
        self.service.audio.start(event,claim['claim_token'],16000)
        self.at+=timedelta(seconds=601)
        restarted=ReminderService(self.store,clock=lambda:self.at)
        self.assertIsNone(restarted.audio.claim(self.device));self.assertEqual(self.state(event),'uncertain')

    def test_only_exact_played_samples_completes_and_cannot_complete_twice(self):
        _,event=self.due();claim=self.service.audio.claim(self.device);token=claim['claim_token']
        self.service.audio.start(event,token,16000)
        self.assertEqual(self.service.audio.finish(event,token,completed=True,played_samples=16000),'played')
        with self.assertRaises(XiaoNuoError):self.service.audio.finish(event,token,completed=True,played_samples=16000)
        self.assertIsNone(self.service.audio.claim(self.device))
        self.assertEqual(len(self.service.notifications()),1) # audio ACK is not console acknowledgment

    def test_partial_or_failed_output_is_uncertain_and_not_requeued(self):
        for completed,samples in ((True,15999),(False,16000)):
            with self.subTest(completed=completed):
                # Separate fixture to avoid choosing a previous event.
                self.setUp();_,event=self.due();claim=self.service.audio.claim(self.device)
                self.service.audio.start(event,claim['claim_token'],16000)
                self.assertEqual(self.service.audio.finish(event,claim['claim_token'],completed=completed,played_samples=samples),'uncertain')
                self.assertIsNone(self.service.audio.claim(self.device))

    def test_cancel_update_and_console_ack_invalidate_pending_claim(self):
        for operation in ('cancel','update','ack'):
            with self.subTest(operation=operation):
                self.setUp();reminder,event=self.due();claim=self.service.audio.claim(self.device)
                if operation=='cancel':self.service.cancel(reminder['id'])
                elif operation=='ack':self.service.acknowledge(event)
                else:
                    current=self.service.list()[0]
                    self.service.update(reminder['id'],text='新事项',due_at=self.at+timedelta(hours=1),zone='Asia/Shanghai',repeat='none',quiet_exception=False,expected_version=current['version'])
                self.assertFalse(self.service.audio.allowed(event,claim['claim_token']))
                with self.assertRaises(XiaoNuoError):self.service.audio.start(event,claim['claim_token'],16000)
                self.assertIsNone(self.service.audio.claim(self.device));self.assertEqual(self.state(event),'cancelled')

    def test_quiet_speech_off_and_volume_zero_prevent_claim_and_start(self):
        _,event=self.due();self.store.set_quiet(True)
        self.assertIsNone(self.service.audio.claim(self.device));self.store.set_quiet(False)
        for update in ({'speech_enabled':False},{'volume':0}):
            self.store.patch_controls(update);self.assertIsNone(self.service.audio.claim(self.device))
            self.store.patch_controls({'speech_enabled':True,'volume':.5})
        claim=self.service.audio.claim(self.device);self.store.set_quiet(True)
        self.assertFalse(self.service.audio.allowed(event,claim['claim_token']))

    def test_device_revocation_and_wrong_claim_token_block_send(self):
        _,event=self.due()
        with self.assertRaises(XiaoNuoError):self.service.audio.claim('unbound')
        claim=self.service.audio.claim(self.device)
        with self.assertRaises(XiaoNuoError):self.service.audio.start(event,'wrong',16000)
        with self.store.ledger.connect() as db:db.execute('UPDATE devices SET revoked_at=? WHERE id=?',(self.at.isoformat(),self.device))
        self.assertFalse(self.service.audio.allowed(event,claim['claim_token']))

    def test_stale_reminder_expires_for_audio_but_console_retained(self):
        _,event=self.due();self.at+=timedelta(minutes=16)
        self.assertIsNone(self.service.audio.claim(self.device));self.assertEqual(self.state(event),'expired')
        self.assertEqual(len(self.service.notifications()),1)

    def test_non_utc_clock_and_invalid_sample_types(self):
        self.at=self.at.astimezone(timezone(timedelta(hours=8)))
        _,event=self.due();claim=self.service.audio.claim(self.device)
        for value in (True,0,-1,960001,'16000'):
            with self.assertRaises(XiaoNuoError):self.service.audio.start(event,claim['claim_token'],value)
        self.service.audio.start(event,claim['claim_token'],16000)
        with self.assertRaises(XiaoNuoError):self.service.audio.finish(event,claim['claim_token'],completed=True,played_samples=True)
