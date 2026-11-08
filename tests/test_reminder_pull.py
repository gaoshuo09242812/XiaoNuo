from datetime import datetime,timedelta,timezone
import unittest
from unittest.mock import patch
from uuid import uuid4

from starlette.websockets import WebSocketDisconnect
import test_network_audio_session as fixtures
from app.reminders import ReminderService

class ReminderPullTests(unittest.TestCase):
    def setUp(self):
        self.f=fixtures.NetworkUploadTests();self.f.setUp();self.addCleanup(self.f.doCleanups)
        self.at=datetime(2026,11,5,2,tzinfo=timezone.utc)
        self.reminders=ReminderService(self.f.store,clock=lambda:self.at)
        self.prepared=[];outer=self
        class Handler:
            reminder_audio=outer.reminders.audio
            async def prepare_reminder(self,text,request,cancelled):
                outer.prepared.append(text)
                if outer.prepare_hook:outer.prepare_hook()
                return b'\0\0'*2000
        self.prepare_hook=None;self.handler=Handler();self.f.make_app(self.handler)

    def due(self):
        self.reminders.create('公开测试喝水',self.at+timedelta(seconds=1))
        self.at+=timedelta(seconds=2);self.reminders.tick()
        return self.reminders.notifications()[0]['id']

    def state(self,event):
        with self.f.store.ledger.connect() as db:return db.execute('SELECT state FROM reminder_audio_outbox WHERE event_id=?',(event,)).fetchone()[0]

    def hello(self,socket,cap=True):
        message={'type':'hello','protocol':5,'id':self.f.record['id'],
                 'hardware_id':self.f.board.hardware,'firmware':'future-reminder-fixture','capabilities':{'reply_buffered':True}}
        if cap:message['capabilities']['reply_reminders']=True
        socket.send_json(message);return socket.receive_json()

    def pull(self,socket):
        request=str(uuid4());socket.send_json({'type':'reminder_pull','request_id':request});return request

    def transfer(self,socket,*,wrong=False):
        result=socket.receive_json();self.assertEqual(result['type'],'audio_result')
        start=socket.receive_json();self.assertEqual(start['type'],'reply_start')
        self.assertNotIn('max_pipeline_windows',start)
        socket.send_json({'type':'reply_ready','reply_id':start['reply_id'],'sample_rate':16000,'gain_divisor':2,'max_seconds':60,'window_frames':4})
        frame=socket.receive_bytes();self.assertEqual(len(frame),1024)
        socket.send_json({'type':'reply_ack','reply_id':start['reply_id'],'next_sequence':1,'received_bytes':1000})
        self.assertEqual(socket.receive_json()['type'],'reply_end')
        socket.send_json({'type':'reply_played','reply_id':start['reply_id'],'completed':True,'samples':1999 if wrong else 2000,'gain_divisor':2})
        return result

    def test_exact_completion_is_durable_and_next_pull_empty(self):
        event=self.due()
        with self.f.connect() as socket:
            ready=self.hello(socket);self.assertTrue(ready['reply_reminders_enabled']);self.assertEqual(ready['reminder_poll_seconds'],30)
            self.pull(socket);self.transfer(socket)
            self.assertEqual(socket.receive_json()['type'],'audio_complete')
            self.assertEqual(self.state(event),'played')
            self.pull(socket);self.assertEqual(socket.receive_json()['type'],'reminder_empty')
        self.assertEqual(len(self.prepared),1);self.assertEqual(self.f.calls,[])

    def test_empty_poll_has_no_audio_synthesis_or_request_ledger_write(self):
        with self.f.connect() as socket:
            self.hello(socket);request=self.pull(socket)
            self.assertEqual(socket.receive_json(),{'type':'reminder_empty','request_id':request})
        self.assertEqual(self.prepared,[]);self.assertEqual(self.f.states(),[])

    def test_fast_empty_poll_does_not_repeatedly_claim_database(self):
        with patch.object(self.reminders.audio,'claim',wraps=self.reminders.audio.claim) as claim:
            with self.f.connect() as socket:
                self.hello(socket)
                for _ in range(3):
                    self.pull(socket);self.assertEqual(socket.receive_json()['type'],'reminder_empty')
            self.assertEqual(claim.call_count,1)

    def test_actual_local_preparation_never_calls_asr_chat_or_cloud_policy(self):
        import asyncio,threading
        from app.network_speech import LocalNetworkSpeech
        from app.budgeting import BudgetManager
        from test_voice import FakeASR,FakeTTS,FakeCompanion
        adapter=LocalNetworkSpeech(self.f.settings.root,FakeASR(),FakeTTS(),FakeCompanion(),
                    BudgetManager(self.f.store.ledger,self.f.settings),self.f.store,chat_provider='deepseek')
        with patch.object(adapter.asr,'transcribe',side_effect=AssertionError('no ASR')),patch.object(adapter.companion,'chat',side_effect=AssertionError('no chat')),patch('app.network_speech.require_deepseek_chat',side_effect=AssertionError('no cloud policy')):
            pcm=asyncio.run(adapter.prepare_reminder('喝水',str(uuid4()),threading.Event()))
        self.assertIsInstance(pcm,bytes);self.assertGreater(len(pcm),0)

    def test_old_device_not_negotiated_and_cannot_pull(self):
        with self.f.connect() as socket:
            self.assertNotIn('reply_reminders_enabled',self.hello(socket,False))
            self.pull(socket)
            with self.assertRaises(WebSocketDisconnect):socket.receive_json()

    def test_capability_status_requires_connected_authorized_new_device(self):
        links=self.f.app.state.device_links
        self.assertFalse(links.reminder_capable())
        with self.f.connect() as socket:
            self.hello(socket);self.assertTrue(links.reminder_capable())
            with self.f.store.ledger.connect() as db:
                db.execute('UPDATE devices SET revoked_at=? WHERE id=?',(self.at.isoformat(),self.f.record['id']))
            self.assertFalse(links.reminder_capable())
        self.assertFalse(links.reminder_capable())

    def test_bad_capability_or_request_rejected(self):
        with self.f.connect() as socket:
            socket.send_json({'type':'hello','protocol':5,'id':self.f.record['id'],'hardware_id':self.f.board.hardware,
                              'firmware':'fixture','capabilities':{'reply_reminders':True}})
            with self.assertRaises(WebSocketDisconnect):socket.receive_json()
        with self.f.connect() as socket:
            self.hello(socket);socket.send_json({'type':'reminder_pull','request_id':'bad'})
            with self.assertRaises(WebSocketDisconnect):socket.receive_json()
        self.assertEqual(self.prepared,[])

    def test_quiet_poll_does_not_consume_pending_reminder(self):
        event=self.due();self.f.store.set_quiet(True)
        with self.f.connect() as socket:
            self.hello(socket);self.pull(socket);self.assertEqual(socket.receive_json()['type'],'reminder_empty')
        self.assertEqual(self.prepared,[]);self.assertEqual(self.state(event),'pending')

    def test_cancel_during_prepare_sends_no_audio_and_does_not_retry(self):
        event=self.due();self.prepare_hook=lambda:self.reminders.acknowledge(event)
        with self.f.connect() as socket:
            self.hello(socket);self.pull(socket);self.assertEqual(socket.receive_json()['type'],'audio_error')
        self.assertEqual(self.state(event),'failed')

    def test_disconnect_after_start_marks_uncertain(self):
        event=self.due()
        with self.f.connect() as socket:
            self.hello(socket);self.pull(socket)
            self.assertEqual(socket.receive_json()['type'],'audio_result')
        self.assertEqual(self.state(event),'uncertain')

    def test_wrong_device_completion_never_marks_played(self):
        event=self.due()
        with self.f.connect() as socket:
            self.hello(socket);self.pull(socket);self.transfer(socket,wrong=True)
            with self.assertRaises(WebSocketDisconnect):socket.receive_json()
        self.assertEqual(self.state(event),'uncertain')
