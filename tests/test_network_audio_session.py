"""Authenticated upload acceptance with synthetic PCM and temporary databases."""
import asyncio
import hashlib
import json
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch
from uuid import uuid4

from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from app.config import Settings
from app.device_gateway import create_device_gateway
from app.devices import DeviceRegistry
from app.budgeting import UsageLedger
from app.storage import CompanionStore
from app.network_audio import AudioReceiver,encode_audio_frame
from app.network_audio import HEADER,MAGIC
from app.adpcm import decode,encode
from test_devices import FakeBoard


class NetworkUploadTests(unittest.TestCase):
    def test_expression_metadata_is_additive_and_filters_untrusted_values(self):
        for value,expected in [('happy','happy'),('sleepy','sleepy'),([], 'neutral'),('PRIVATE-FIELD','neutral')]:
            async def processor(*args):
                return {'transcript':'公开测试','reply_text':'公开回复','_expression':value,'secret':'PRIVATE-FIELD'}
            self.make_app(processor)
            with self.connect() as socket:
                self.hello(socket)
                ready=self.begin(socket);self.send_pcm(socket,ready)
                result=socket.receive_json()
                self.assertEqual(result['expression'],expected)
                self.assertNotIn('_expression',result)
                self.assertNotIn('PRIVATE',json.dumps(result))

    def test_pipeline_requires_explicit_buffered_v5_capability(self):
        with self.connect() as socket:
            socket.send_json({'type':'hello','protocol':5,'id':self.record['id'],'hardware_id':self.board.hardware,
                              'firmware':'pipeline-fixture','capabilities':{'reply_buffered':True,'reply_pipelined':True}})
            ready=socket.receive_json();self.assertTrue(ready['reply_pipelined_enabled'])
            session=next(iter(self.app.state.device_links._connections.values())).audio
            self.assertTrue(session.reply_pipelined)
        for protocol,capabilities in [(5,{'reply_pipelined':True}),(4,{'reply_buffered':True,'reply_pipelined':True}),
                                      (5,{'reply_buffered':True,'reply_pipelined':1})]:
            with self.connect() as socket:
                socket.send_json({'type':'hello','protocol':protocol,'id':self.record['id'],'hardware_id':self.board.hardware,
                                  'firmware':'pipeline-fixture','capabilities':capabilities})
                with self.assertRaises(WebSocketDisconnect):socket.receive_json()

    def test_authenticated_compressed_wake_can_end_at_two_seconds_without_six_second_padding(self):
        class Handler:
            wake_enabled=True
            async def wake_probe(inner,*args):
                value=await self.processor(*args)
                return value|{'_wake_status':'accepted','_followup_seconds':20}
        self.make_app(Handler())
        with self.connect() as socket:
            self.hello(socket,5)
            ready=self.begin(socket,seconds=6,trigger='wake',consent='owner-computer-wake',encoding='ima_adpcm4',predictor=0,step_index=0)
            self.assertTrue(ready['trim_end_enabled']);self.assertEqual(ready['expected_bytes'],48000)
            wire=b'\0'*16000
            for seq,offset in enumerate(range(0,len(wire),1000)):
                socket.send_bytes(encode_audio_frame(ready['stream_id'],seq,wire[offset:offset+1000],compressed=True))
            socket.send_json({'type':'audio_end','stream_id':ready['stream_id'],'sha256':hashlib.sha256(wire).hexdigest(),'samples':32000})
            self.assertEqual(socket.receive_json()['type'],'audio_processing')
            result=socket.receive_json();self.assertEqual(result['type'],'audio_result')
            self.assertEqual(result['wake_status'],'accepted')
        self.assertEqual(len(self.calls),1);self.assertEqual(len(self.calls[0][0]),64000)
        self.assertEqual(self.states(),['complete'])

    def test_manual_compressed_stream_cannot_opt_into_wake_trim(self):
        with self.connect() as socket:
            self.hello(socket,4)
            ready=self.begin(socket,seconds=6,encoding='ima_adpcm4',predictor=0,step_index=0)
            self.assertNotIn('trim_end_enabled',ready)
            wire=b'\0'*16000
            for seq,offset in enumerate(range(0,len(wire),1000)):
                socket.send_bytes(encode_audio_frame(ready['stream_id'],seq,wire[offset:offset+1000],compressed=True))
            socket.send_json({'type':'audio_end','stream_id':ready['stream_id'],'sha256':hashlib.sha256(wire).hexdigest(),'samples':32000})
            with self.assertRaises(WebSocketDisconnect):socket.receive_json()
        self.assertFalse(self.calls);self.assertEqual(self.receivers[0].snapshot()['received_bytes'],0)

    def setUp(self):
        temporary=tempfile.TemporaryDirectory();self.addCleanup(temporary.cleanup)
        self.settings=Settings(root=Path(temporary.name),admin_token='owner-'+'x'*40)
        self.store=CompanionStore(UsageLedger(self.settings.database))
        self.board=FakeBoard();self.registry=DeviceRegistry(self.store,self.board)
        self.record=self.registry.enroll();self.calls=[];self.receivers=[]
        self.url='wss://testserver/api/v1/device-link/ws'
        self.make_app(self.processor)
        def factory(*args,**kwargs):
            receiver=AudioReceiver(*args,**kwargs);self.receivers.append(receiver);return receiver
        patched=patch('app.network_audio_session.AudioReceiver',side_effect=factory)
        patched.start();self.addCleanup(patched.stop)

    async def processor(self,pcm,claim,device,cancelled):
        self.calls.append((pcm,claim,device,cancelled))
        return {'transcript':'公开合成测试','reply_text':'收到测试。','secret':'must-not-reflect'}

    def make_app(self,handler):
        self.app=create_device_gateway(self.settings,registry=self.registry,audio_handler=handler)
        self.client=TestClient(self.app);self.addCleanup(self.client.close)

    def connect(self):
        return self.client.websocket_connect(self.url,headers={'Authorization':'Bearer '+self.board.token})

    def hello(self,socket,protocol=2,*,http_reply=False):
        message={'type':'hello','protocol':protocol,'id':self.record['id'],
                 'hardware_id':self.board.hardware,'firmware':'session-test'}
        if http_reply:message['capabilities']={'reply_http':True}
        socket.send_json(message)
        return socket.receive_json()

    def begin(self,socket,request=None,**changes):
        body={'type':'audio_begin','request_id':str(request or uuid4()),'seconds':2,'consent':'owner-computer'}
        body.update(changes);socket.send_json(body)
        return socket.receive_json()

    def send_pcm(self,socket,ready):
        pcm=b'\x01\x02'*32000
        for seq,offset in enumerate(range(0,len(pcm),1000)):
            socket.send_bytes(encode_audio_frame(ready['stream_id'],seq,pcm[offset:offset+1000]))
        socket.send_json({'type':'audio_end','stream_id':ready['stream_id'],'sha256':hashlib.sha256(pcm).hexdigest()})
        self.assertEqual(socket.receive_json()['type'],'audio_processing')
        return pcm

    def states(self):
        with self.store.ledger.connect() as db:
            return [r[0] for r in db.execute('SELECT state FROM chat_requests')]

    def test_authenticated_upload_reaches_handler_once_and_filters_response(self):
        with self.connect() as socket:
            self.assertTrue(self.hello(socket)['audio_upload_enabled'])
            ready=self.begin(socket);pcm=self.send_pcm(socket,ready)
            result=socket.receive_json()
            self.assertEqual(result['type'],'audio_result')
            self.assertNotIn('secret',result)
            self.assertFalse(result['playback_complete'])
            self.assertFalse(result['reply_audio_enabled'])
        self.assertEqual(self.calls[0][0],pcm)
        self.assertEqual(self.states(),['complete'])
        self.assertEqual(self.receivers[0].snapshot()['received_bytes'],0)

    def test_wake_probe_requires_explicit_consent_and_enabled_protocol_three(self):
        for protocol in (2,3):
            with self.connect() as socket:
                self.hello(socket,protocol)
                with self.assertRaises(WebSocketDisconnect):
                    self.begin(socket,trigger='wake',consent='owner-computer-wake')
        self.assertEqual(self.states(),[])

    def test_quiet_wake_probe_can_be_ignored_without_echoing_private_transcript(self):
        class Handler:
            wake_enabled=True
            async def wake_probe(inner,*args):
                return {'transcript':'','reply_text':'','_wake_status':'ignored','_followup_seconds':0,'secret':'must-not-reflect'}
        self.make_app(Handler());self.store.set_quiet(True)
        with self.connect() as socket:
            self.assertTrue(self.hello(socket,3)['computer_wake_enabled'])
            ready=self.begin(socket,trigger='wake',consent='owner-computer-wake')
            self.send_pcm(socket,ready);result=socket.receive_json()
            self.assertEqual(result['wake_status'],'ignored');self.assertTrue(result['quiet'])
            self.assertEqual(result['transcript'],'');self.assertNotIn('secret',result)
            socket.send_json({'type':'ping','sequence':1});self.assertEqual(socket.receive_json()['type'],'pong')
            # A completed wake probe must not authorize a later manual upload
            # while the owner is still quiet, even on the same connection.
            with self.assertRaises(WebSocketDisconnect):self.begin(socket)
        self.assertEqual(self.states(),['complete'])

    def test_wake_handler_cannot_return_ignored_transcript(self):
        class Handler:
            wake_enabled=True
            async def wake_probe(inner,*args):
                return {'transcript':'private room speech','reply_text':'','_wake_status':'ignored','_followup_seconds':0}
        self.make_app(Handler())
        with self.connect() as socket:
            self.hello(socket,3);ready=self.begin(socket,trigger='wake',consent='owner-computer-wake')
            self.send_pcm(socket,ready);result=socket.receive_json()
            self.assertEqual(result['type'],'audio_error');self.assertNotIn('private',str(result))
        self.assertEqual(self.states(),['failed_or_uncertain'])

    def test_version_one_and_default_gateway_never_enable_upload(self):
        with self.connect() as socket:
            self.assertFalse(self.hello(socket,1)['audio_upload_enabled'])
            with self.assertRaises(WebSocketDisconnect):self.begin(socket)
        self.make_app(None)
        with self.connect() as socket:
            self.assertFalse(self.hello(socket)['audio_upload_enabled'])
            with self.assertRaises(WebSocketDisconnect):self.begin(socket)
        self.assertEqual(self.calls,[]);self.assertEqual(self.states(),[])

    def test_consent_and_duration_fail_before_request_claim(self):
        for changes in ({'consent':'local-only'},{'seconds':True},{'request_id':'bad'}):
            with self.connect() as socket:
                self.hello(socket)
                with self.assertRaises(WebSocketDisconnect):self.begin(socket,**changes)
        self.assertEqual(self.states(),[]);self.assertEqual(self.calls,[])

    def test_duplicate_request_stays_consumed_after_gateway_restart(self):
        request=uuid4()
        with self.connect() as socket:
            self.hello(socket);ready=self.begin(socket,request);self.send_pcm(socket,ready)
            self.assertEqual(socket.receive_json()['type'],'audio_result')
        # A fresh registry and app share only the persisted database.
        self.registry=DeviceRegistry(CompanionStore(UsageLedger(self.settings.database)),self.board)
        self.make_app(self.processor)
        with self.connect() as socket:
            self.hello(socket)
            with self.assertRaises(WebSocketDisconnect) as error:self.begin(socket,request)
            self.assertEqual(error.exception.reason,'DUPLICATE_REQUEST')
        self.assertEqual(len(self.calls),1)

    def test_disconnect_clears_partial_audio_and_consumes_request(self):
        request=uuid4()
        with self.connect() as socket:
            self.hello(socket);ready=self.begin(socket,request)
            socket.send_bytes(encode_audio_frame(ready['stream_id'],0,b'\0'*1000))
            socket.send_json({'type':'ping','sequence':1});socket.receive_json()
        self.assertEqual(self.receivers[0].snapshot()['received_bytes'],0)
        self.assertEqual(self.states(),['failed_or_uncertain'])
        with self.connect() as socket:
            self.hello(socket)
            with self.assertRaises(WebSocketDisconnect):self.begin(socket,request)

    def test_cancel_keeps_pings_responsive_while_processor_is_running(self):
        started=threading.Event();events=[]
        async def slow(pcm,claim,device,cancelled):
            events.append(cancelled);started.set()
            await asyncio.sleep(30)
            return {'reply_text':'must-not-arrive'}
        self.make_app(slow)
        with self.connect() as socket:
            self.hello(socket);ready=self.begin(socket);self.send_pcm(socket,ready)
            self.assertTrue(started.wait(2))
            socket.send_json({'type':'ping','sequence':1})
            self.assertEqual(socket.receive_json(),{'type':'pong','sequence':1})
            socket.send_json({'type':'audio_cancel','stream_id':ready['stream_id']})
            self.assertEqual(socket.receive_json()['type'],'audio_cancelled')
            self.assertTrue(events[0].is_set())
            socket.send_json({'type':'ping','sequence':2})
            self.assertEqual(socket.receive_json()['type'],'pong')
        self.assertEqual(self.states(),['failed_or_uncertain'])

    def test_revocation_mid_upload_discards_buffer_before_handler(self):
        with self.connect() as socket:
            self.hello(socket);ready=self.begin(socket)
            socket.send_bytes(encode_audio_frame(ready['stream_id'],0,b'\0'*1000))
            socket.send_json({'type':'ping','sequence':1});socket.receive_json()
            self.board.connected=False;self.registry.revoke(self.record['id'])
            socket.send_bytes(encode_audio_frame(ready['stream_id'],1,b'\0\0'))
            with self.assertRaises(WebSocketDisconnect):socket.receive_json()
        self.assertEqual(self.receivers[0].snapshot()['received_bytes'],0)
        self.assertEqual(self.calls,[])

    def test_old_stream_frame_is_rejected_after_new_connection(self):
        with self.connect() as socket:
            self.hello(socket);old=self.begin(socket)
        with self.connect() as socket:
            self.hello(socket);new=self.begin(socket)
            self.assertNotEqual(old['stream_id'],new['stream_id'])
            socket.send_bytes(encode_audio_frame(old['stream_id'],0,b'\0\0'))
            with self.assertRaises(WebSocketDisconnect):socket.receive_json()
        self.assertEqual(self.calls,[])

    def test_processing_exception_does_not_echo_private_exception(self):
        async def broken(*args):raise ValueError('example-private-credential')
        self.make_app(broken)
        with self.connect() as socket:
            self.hello(socket);ready=self.begin(socket);self.send_pcm(socket,ready)
            result=socket.receive_json()
            self.assertEqual(result['error'],'NETWORK_AUDIO_PROCESSOR_ERROR')
            self.assertNotIn('example-private',str(result))
        self.assertEqual(self.states(),['failed_or_uncertain'])

    def test_quiet_mode_blocks_begin_without_claiming(self):
        self.store.set_quiet(True)
        with self.connect() as socket:
            self.hello(socket)
            with self.assertRaises(WebSocketDisconnect):self.begin(socket)
        self.assertEqual(self.states(),[])

    def test_cancelled_handler_cannot_send_a_late_result(self):
        started=threading.Event()
        async def ignores_cancel(pcm,claim,device,cancelled):
            started.set()
            try:await asyncio.sleep(30)
            except asyncio.CancelledError:pass
            return {'transcript':'must-not-arrive','reply_text':'late reply'}
        self.make_app(ignores_cancel)
        with self.connect() as socket:
            self.hello(socket);ready=self.begin(socket);self.send_pcm(socket,ready)
            self.assertTrue(started.wait(2))
            socket.send_json({'type':'audio_cancel','stream_id':ready['stream_id']})
            self.assertEqual(socket.receive_json()['type'],'audio_cancelled')
            socket.send_json({'type':'ping','sequence':1})
            self.assertEqual(socket.receive_json()['type'],'pong')
        self.assertEqual(self.states(),['failed_or_uncertain'])

    def test_quiet_during_processing_suppresses_transcript_and_reply(self):
        started=threading.Event();release=threading.Event()
        async def delayed(*args):
            started.set()
            while not release.is_set():await asyncio.sleep(.01)
            return {'transcript':'must-not-arrive','reply_text':'must-not-arrive'}
        self.make_app(delayed)
        with self.connect() as socket:
            self.hello(socket);ready=self.begin(socket);self.send_pcm(socket,ready)
            self.assertTrue(started.wait(2))
            self.store.set_quiet(True);release.set()
            result=socket.receive_json()
            self.assertEqual(result['error'],'SPEECH_DISABLED_BY_OWNER')
            self.assertNotIn('transcript',result)
        self.assertEqual(self.states(),['failed_or_uncertain'])

    def test_checksum_failure_never_reaches_processor(self):
        with self.connect() as socket:
            self.hello(socket);ready=self.begin(socket)
            pcm=b'\x01\x02'*32000
            for seq,offset in enumerate(range(0,len(pcm),1000)):
                socket.send_bytes(encode_audio_frame(ready['stream_id'],seq,pcm[offset:offset+1000]))
            socket.send_json({'type':'audio_end','stream_id':ready['stream_id'],'sha256':'0'*64})
            with self.assertRaises(WebSocketDisconnect):socket.receive_json()
        self.assertEqual(self.calls,[])
        self.assertEqual(self.receivers[0].snapshot()['received_bytes'],0)

    def test_cancelled_request_can_be_followed_by_a_new_request(self):
        with self.connect() as socket:
            self.hello(socket);first=self.begin(socket)
            socket.send_json({'type':'audio_cancel','stream_id':first['stream_id']})
            self.assertEqual(socket.receive_json()['type'],'audio_cancelled')
            second=self.begin(socket);self.send_pcm(socket,second)
            self.assertEqual(socket.receive_json()['type'],'audio_result')
        self.assertEqual(self.states(),['failed_or_uncertain','complete'])

    def test_transfer_deadline_closes_connection_and_discards_partial(self):
        with self.connect() as socket:
            self.hello(socket);ready=self.begin(socket)
            socket.send_bytes(encode_audio_frame(ready['stream_id'],0,b'\0'*1000))
            socket.send_json({'type':'ping','sequence':1});socket.receive_json()
            self.receivers[0]._deadline=0
            socket.send_json({'type':'ping','sequence':2})
            # Depending on scheduling, expiry is observed before or after this
            # already-pending ping. Neither path may keep the upload open.
            try:
                self.assertEqual(socket.receive_json()['type'],'pong')
            except WebSocketDisconnect:
                pass
            else:
                with self.assertRaises(WebSocketDisconnect):socket.receive_json()
        self.assertEqual(self.receivers[0].snapshot()['received_bytes'],0)
        self.assertEqual(self.states(),['failed_or_uncertain'])

    def receive_reply_segment(self,socket,start,*,window=4,max_seconds=8):
        from uuid import UUID
        socket.send_json({'type':'reply_ready','reply_id':start['reply_id'],
                          'sample_rate':16000,'gain_divisor':2,'max_seconds':max_seconds,
                          **({'window_frames':window} if window!=4 else {})})
        pcm=bytearray();sequence=0
        while len(pcm)<start['bytes']:
            frame=socket.receive_bytes()
            magic,stream,index=HEADER.unpack_from(frame)
            self.assertEqual(magic,MAGIC);self.assertEqual(str(UUID(bytes=stream)),start['reply_id'])
            self.assertEqual(index,sequence);sequence+=1;pcm.extend(frame[HEADER.size:])
            if sequence%window==0 or len(pcm)==start['bytes']:
                socket.send_json({'type':'reply_ack','reply_id':start['reply_id'],
                                  'next_sequence':sequence,'received_bytes':len(pcm)})
        end=socket.receive_json()
        self.assertEqual(end['type'],'reply_end')
        self.assertEqual(end['sha256'],hashlib.sha256(pcm).hexdigest())
        socket.send_json({'type':'reply_played','reply_id':start['reply_id'],
                          'completed':True,'samples':start.get('samples',len(pcm)//2),'gain_divisor':2})
        return bytes(pcm)

    def test_reply_probe_runs_11_and_20_seconds_without_microphone_or_models(self):
        for seconds in (11,20):
            with self.subTest(seconds=seconds):
                samples=[]
                with self.connect() as socket:
                    self.hello(socket,5)
                    identifier=str(uuid4())
                    socket.send_json({'type':'reply_probe','request_id':identifier,'seconds':seconds})
                    result=socket.receive_json()
                    self.assertEqual((result['type'],result['stream_id']),('audio_result',identifier))
                    for index in range((seconds+3)//4):
                        start=socket.receive_json()
                        self.assertEqual(start['segment'],index)
                        self.assertLessEqual(start['bytes'],32000)
                        wire=self.receive_reply_segment(socket,start,window=32)
                        self.assertEqual(len(decode(wire,0,0)[0]),start['samples']*2)
                        samples.append(start['samples'])
                    self.assertTrue(socket.receive_json()['playback_complete'])
                self.assertEqual(sum(samples),seconds*16000)
        self.assertEqual(self.calls,[])
        self.assertEqual(self.receivers,[])
        self.assertEqual(self.states(),['complete','complete'])

    def test_streaming_hello_keeps_11_and_20_seconds_in_one_reply_segment(self):
        from test_network_reply_streaming import decode_reply
        for seconds in (1,11,20):
            with self.subTest(seconds=seconds),self.connect() as socket:
                socket.send_json({'type':'hello','protocol':5,'id':self.record['id'],
                    'hardware_id':self.board.hardware,'firmware':'continuous-test',
                    'capabilities':{'reply_streaming':True}})
                self.assertTrue(socket.receive_json()['reply_streaming_enabled'])
                socket.send_json({'type':'reply_probe','request_id':str(uuid4()),'seconds':seconds})
                self.assertEqual(socket.receive_json()['type'],'audio_result')
                start=socket.receive_json();self.assertEqual(start['segment'],0)
                self.assertEqual(start['samples'],seconds*16000)
                wire=self.receive_reply_segment(socket,start,window=16,max_seconds=20)
                self.assertEqual(len(decode_reply(wire)),seconds*32000)
                complete=socket.receive_json()
                self.assertEqual(complete['type'],'audio_complete')
                self.assertEqual(complete['segments'],1)
                self.assertTrue(complete['playback_complete'])
        self.assertEqual(self.calls,[]);self.assertEqual(self.receivers,[])

    def test_long_reply_crosses_two_20_second_boundaries_without_losing_odd_tail(self):
        from test_network_reply_streaming import decode_reply
        from app.errors import XiaoNuoError
        async def source():
            for _ in range(45):yield b'\x11\x22'*16000
            yield b'\x11\x22'*501
        async def handler(*_):
            return {'transcript':'PRIVATE_TRANSCRIPT_SENTINEL','reply_text':'PRIVATE_REPLY_SENTINEL','_audio':source(),
                    '_audio_metrics':{'tts_ms':12,'pcm_conversion_ms':3,'tts_calls':1,'private':'PRIVATE_METRIC_SENTINEL'}}
        self.make_app(handler)
        with self.connect() as socket:
            socket.send_json({'type':'hello','protocol':5,'id':self.record['id'],
                'hardware_id':self.board.hardware,'firmware':'long-tail-test','capabilities':{'reply_streaming':True}})
            self.assertTrue(socket.receive_json()['reply_streaming_enabled'])
            ready=self.begin(socket);self.send_pcm(socket,ready)
            self.assertEqual(socket.receive_json()['type'],'audio_result')
            sizes=[]
            for index in range(3):
                start=socket.receive_json();self.assertEqual(start['segment'],index)
                wire=self.receive_reply_segment(socket,start,window=16,max_seconds=20)
                self.assertGreaterEqual(len(decode_reply(wire)),start['samples']*2)
                sizes.append(start['samples'])
            self.assertTrue(socket.receive_json()['playback_complete'])
        self.assertEqual(sizes,[320000,320000,80501])
        log=self.settings.database.parent/'diagnostics/reply-lifecycle.jsonl'
        rows=[json.loads(line) for line in log.read_text(encoding='utf-8').splitlines()]
        audit=rows[-1]
        self.assertEqual([audit[k] for k in ('source_samples','planned_samples','played_samples')],[720501]*3)
        self.assertTrue(audit['complete']);self.assertTrue(audit['source_exhausted'])
        self.assertEqual(audit['segments_completed'],3)
        self.assertEqual(audit['tts_ms'],12)
        self.assertEqual(audit['pcm_conversion_ms'],3)
        self.assertEqual(audit['tts_calls'],1)
        self.assertNotIn('PRIVATE_',log.read_text(encoding='utf-8'))

    def test_buffered_60_second_boundary_preserves_odd_tail(self):
        from test_network_reply_streaming import decode_reply
        from app.errors import XiaoNuoError
        async def source():
            for _ in range(60):yield b'\x11\x22'*16000
            yield b'\x11\x22'*501
        async def handler(*_):
            return {'transcript':'PRIVATE_TRANSCRIPT_SENTINEL','reply_text':'PRIVATE_REPLY_SENTINEL','_audio':source()}
        self.make_app(handler)
        with self.connect() as socket:
            socket.send_json({'type':'hello','protocol':5,'id':self.record['id'],
                'hardware_id':self.board.hardware,'firmware':'long-tail-test','capabilities':{'reply_buffered':True}})
            self.assertTrue(socket.receive_json()['reply_buffered_enabled'])
            ready=self.begin(socket);self.send_pcm(socket,ready)
            self.assertEqual(socket.receive_json()['type'],'audio_result')
            sizes=[]
            for index in range(2):
                start=socket.receive_json();self.assertEqual(start['segment'],index)
                wire=self.receive_reply_segment(socket,start,window=16,max_seconds=60)
                self.assertGreaterEqual(len(decode_reply(wire)),start['samples']*2)
                sizes.append(start['samples'])
            self.assertTrue(socket.receive_json()['playback_complete'])
        self.assertEqual(sizes,[960000,501])
        log=self.settings.database.parent/'diagnostics/reply-lifecycle.jsonl'
        rows=[json.loads(line) for line in log.read_text(encoding='utf-8').splitlines()]
        audit=rows[-1]
        self.assertEqual([audit[k] for k in ('source_samples','planned_samples','played_samples')],[960501]*3)
        self.assertTrue(audit['complete']);self.assertTrue(audit['source_exhausted'])
        self.assertEqual(audit['segments_completed'],2)
        self.assertNotIn('PRIVATE_',log.read_text(encoding='utf-8'))

    def test_tts_failure_after_first_20_seconds_is_recorded_as_incomplete(self):
        from app.errors import XiaoNuoError
        async def source():
            yield b'\0'*(20*32000)
            raise XiaoNuoError('TTS_PROVIDER_FAILED')
        async def handler(*_):
            return {'transcript':'private','reply_text':'private','_audio':source()}
        self.make_app(handler)
        with self.connect() as socket:
            socket.send_json({'type':'hello','protocol':5,'id':self.record['id'],
                'hardware_id':self.board.hardware,'firmware':'long-tail-test','capabilities':{'reply_streaming':True}})
            socket.receive_json();ready=self.begin(socket);self.send_pcm(socket,ready);socket.receive_json()
            start=socket.receive_json();self.receive_reply_segment(socket,start,window=16,max_seconds=20)
            error=socket.receive_json();self.assertEqual(error['error'],'TTS_PROVIDER_FAILED')
        log=self.settings.database.parent/'diagnostics/reply-lifecycle.jsonl'
        audit=json.loads(log.read_text(encoding='utf-8').splitlines()[-1])
        self.assertFalse(audit['complete']);self.assertFalse(audit['source_exhausted'])
        self.assertEqual(audit['played_samples'],320000)
        self.assertEqual(audit['error_code'],'TTS_PROVIDER_FAILED')

    def test_45_second_synthetic_probe_crosses_two_stream_boundaries(self):
        samples=[]
        with self.connect() as socket:
            socket.send_json({'type':'hello','protocol':5,'id':self.record['id'],
                'hardware_id':self.board.hardware,'firmware':'long-probe-test',
                'capabilities':{'reply_streaming':True}})
            self.assertTrue(socket.receive_json()['reply_streaming_enabled'])
            socket.send_json({'type':'reply_probe','request_id':str(uuid4()),'seconds':45})
            self.assertEqual(socket.receive_json()['type'],'audio_result')
            for index in range(3):
                start=socket.receive_json();self.assertEqual(start['segment'],index)
                self.receive_reply_segment(socket,start,window=16,max_seconds=20)
                samples.append(start['samples'])
            complete=socket.receive_json()
            self.assertEqual(complete['segments'],3);self.assertTrue(complete['playback_complete'])
        self.assertEqual(samples,[320000,320000,80000])
        self.assertEqual(self.calls,[]);self.assertEqual(self.receivers,[])
        log=self.settings.database.parent/'diagnostics/reply-lifecycle.jsonl'
        audit=json.loads(log.read_text(encoding='utf-8').splitlines()[-1])
        self.assertTrue(audit['synthetic']);self.assertTrue(audit['complete'])
        self.assertEqual(audit['source_samples'],720000)
        self.assertEqual(audit['played_samples'],720000)

    def test_buffered_45_second_probe_is_one_complete_segment(self):
        with self.connect() as socket:
            socket.send_json({'type':'hello','protocol':5,'id':self.record['id'],
                'hardware_id':self.board.hardware,'firmware':'buffered-test',
                'capabilities':{'reply_buffered':True}})
            ready=socket.receive_json()
            self.assertTrue(ready['reply_buffered_enabled'])
            self.assertNotIn('reply_streaming_enabled',ready)
            socket.send_json({'type':'reply_probe','request_id':str(uuid4()),'seconds':45})
            self.assertEqual(socket.receive_json()['type'],'audio_result')
            start=socket.receive_json();self.assertEqual(start['samples'],720000)
            wire=self.receive_reply_segment(socket,start,window=16,max_seconds=60)
            self.assertEqual(len(wire),360000)
            complete=socket.receive_json()
            self.assertEqual(complete['segments'],1);self.assertTrue(complete['playback_complete'])
        audit=json.loads((self.settings.database.parent/'diagnostics/reply-lifecycle.jsonl').read_text().splitlines()[-1])
        self.assertEqual(audit['source_samples'],720000);self.assertEqual(audit['played_samples'],720000)
        self.assertTrue(audit['complete']);self.assertEqual(self.calls,[])

        for key in ('handler_ms','audio_source_wait_ms','reply_encode_ms','reply_ready_ack_ms',
                    'reply_transfer_ms','reply_playback_ack_ms','reply_wire_bytes'):
            self.assertIs(type(audit[key]),int)
            self.assertGreaterEqual(audit[key],0)
        self.assertEqual(audit['reply_wire_bytes'],360000)

    def test_buffered_window32_keeps_final_partial_window_and_playback_receipt(self):
        with self.connect() as socket:
            socket.send_json({'type':'hello','protocol':5,'id':self.record['id'],
                'hardware_id':self.board.hardware,'firmware':'buffered-test',
                'capabilities':{'reply_buffered':True}})
            ready=socket.receive_json()
            self.assertTrue(ready['reply_buffered_enabled'])
            self.assertNotIn('reply_streaming_enabled',ready)
            socket.send_json({'type':'reply_probe','request_id':str(uuid4()),'seconds':45})
            self.assertEqual(socket.receive_json()['type'],'audio_result')
            start=socket.receive_json();self.assertEqual(start['samples'],720000)
            wire=self.receive_reply_segment(socket,start,window=32,max_seconds=60)
            self.assertEqual(len(wire),360000)
            complete=socket.receive_json()
            self.assertEqual(complete['segments'],1);self.assertTrue(complete['playback_complete'])
        audit=json.loads((self.settings.database.parent/'diagnostics/reply-lifecycle.jsonl').read_text().splitlines()[-1])
        self.assertEqual(audit['source_samples'],720000);self.assertEqual(audit['played_samples'],720000)
        self.assertTrue(audit['complete']);self.assertEqual(self.calls,[])

    def test_reply_probe_rejects_unbounded_duration_and_muted_output(self):
        for seconds in (0,46,True,'11'):
            with self.connect() as socket:
                self.hello(socket,5)
                socket.send_json({'type':'reply_probe','request_id':str(uuid4()),'seconds':seconds})
                with self.assertRaises(WebSocketDisconnect):socket.receive_json()
        self.store.patch_controls({'volume':0})
        with self.connect() as socket:
            self.hello(socket,5)
            socket.send_json({'type':'reply_probe','request_id':str(uuid4()),'seconds':11})
            with self.assertRaises(WebSocketDisconnect):socket.receive_json()
        self.assertEqual(self.calls,[])
        self.assertEqual(self.states(),[])

    async def spoken_processor(self,*args):
        async def audio():
            yield b'\x01\x02'*3200
            yield b'\x03\x04'*2000
        return {'transcript':'合成测试','reply_text':'分两段回复。','_audio':audio()}

    def test_version_three_returns_audio_only_after_matching_acknowledgements(self):
        self.make_app(self.spoken_processor)
        with self.connect() as socket:
            self.hello(socket,3);ready=self.begin(socket);self.send_pcm(socket,ready)
            self.assertTrue(socket.receive_json()['reply_audio_enabled'])
            self.assertEqual(self.receive_reply_segment(socket,socket.receive_json()),
                             b'\x01\x02'*3200+b'\x03\x04'*2000)
            complete=socket.receive_json()
            self.assertEqual(complete['type'],'audio_complete')
            self.assertEqual(complete['segments'],1)
            self.assertTrue(complete['playback_complete']);self.assertFalse(complete['audible_verified'])
        self.assertEqual(self.states(),['complete'])

    def test_version_five_replies_with_adpcm_metadata_and_samples(self):
        async def one_segment(*args):
            async def audio():
                yield b'\x01\x02'*3200
            return {'transcript':'合成测试','reply_text':'一段回复。','_audio':audio()}
        self.make_app(one_segment)
        with self.connect() as socket:
            self.hello(socket,5);ready=self.begin(socket);self.send_pcm(socket,ready)
            self.assertTrue(socket.receive_json()['reply_audio_enabled'])
            start=socket.receive_json()
            self.assertEqual((start['type'],start['format'],start['predictor'],start['step_index']),
                             ('reply_start','ima_adpcm4',0,0))
            self.assertEqual(start['samples'],3200)
            socket.send_json({'type':'reply_ready','reply_id':start['reply_id'],
                              'sample_rate':16000,'gain_divisor':2,'max_seconds':8})
            wire=bytearray();sequence=0
            while len(wire)<start['bytes']:
                frame=socket.receive_bytes();wire.extend(frame[HEADER.size:]);sequence+=1
                if sequence%4==0 or len(wire)==start['bytes']:
                    socket.send_json({'type':'reply_ack','reply_id':start['reply_id'],
                                      'next_sequence':sequence,'received_bytes':len(wire)})
            end=socket.receive_json()
            self.assertEqual(end['sha256'],hashlib.sha256(wire).hexdigest())
            self.assertEqual(decode(bytes(wire),0,0)[0].__len__(),start['samples']*2)
            socket.send_json({'type':'reply_played','reply_id':start['reply_id'],
                              'completed':True,'samples':start['samples'],'gain_divisor':2})
            complete=socket.receive_json()
            self.assertEqual((complete['type'],complete['segments']),('audio_complete',1))
        self.assertEqual(self.states(),['complete'])

    def test_reply_resume_replays_cached_segment(self):
        cache={}
        async def one_segment(*args):
            async def audio():
                yield b'\x01\x02'*3200
            return {'transcript':'合成测试','reply_text':'一段回复。','_audio':audio()}
        one_segment.remember_reply=lambda reply_id,pcm,index:cache.__setitem__(reply_id,pcm)
        one_segment.resume_reply=lambda reply_id:cache.get(reply_id)
        self.make_app(one_segment)
        with self.connect() as socket:
            self.hello(socket,5);ready=self.begin(socket);self.send_pcm(socket,ready)
            self.assertTrue(socket.receive_json()['reply_audio_enabled'])
            first=socket.receive_json();self.assertEqual(first['type'],'reply_start')
            socket.send_json({'type':'reply_ready','reply_id':first['reply_id'],
                              'sample_rate':16000,'gain_divisor':2,'max_seconds':8})
            wire=bytearray();sequence=0
            while len(wire)<first['bytes']:
                frame=socket.receive_bytes();wire.extend(frame[HEADER.size:]);sequence+=1
                if sequence%4==0 or len(wire)==first['bytes']:
                    socket.send_json({'type':'reply_ack','reply_id':first['reply_id'],
                                      'next_sequence':sequence,'received_bytes':len(wire)})
            socket.receive_json()
            socket.send_json({'type':'reply_played','reply_id':first['reply_id'],
                              'completed':True,'samples':first['samples'],'gain_divisor':2})
            self.assertEqual(socket.receive_json()['type'],'audio_complete')
            self.assertIn(first['reply_id'],cache)

            socket.send_json({'type':'reply_resume','reply_id':first['reply_id']})
            resumed=socket.receive_json();self.assertEqual(resumed['type'],'reply_start')
            self.assertEqual(resumed['samples'],first['samples'])
            socket.send_json({'type':'reply_ready','reply_id':resumed['reply_id'],
                              'sample_rate':16000,'gain_divisor':2,'max_seconds':8})
            wire=bytearray();sequence=0
            while len(wire)<resumed['bytes']:
                frame=socket.receive_bytes();wire.extend(frame[HEADER.size:]);sequence+=1
                if sequence%4==0 or len(wire)==resumed['bytes']:
                    socket.send_json({'type':'reply_ack','reply_id':resumed['reply_id'],
                                      'next_sequence':sequence,'received_bytes':len(wire)})
            socket.receive_json()
            socket.send_json({'type':'reply_played','reply_id':resumed['reply_id'],
                              'completed':True,'samples':resumed['samples'],'gain_divisor':2})
            socket.send_json({'type':'ping','sequence':1})
            self.assertEqual(socket.receive_json()['type'],'pong')

    def test_long_reply_uses_http_upload_instead_of_ws_frames(self):
        uploaded=[]
        async def upload(reply_id,data,digest):
            uploaded.append((reply_id,data,digest))
        async def long_reply(*args):
            async def audio():
                yield b'\x01\x02'*64000
            return {'transcript':'合成测试','reply_text':'长回复。','_audio':audio()}
        long_reply.http_reply=upload
        self.make_app(long_reply)
        with self.connect() as socket:
            self.hello(socket,5,http_reply=True);ready=self.begin(socket);self.send_pcm(socket,ready)
            self.assertTrue(socket.receive_json()['reply_audio_enabled'])
            control=socket.receive_json()
            self.assertEqual(control['type'],'reply_http')
            self.assertEqual(control['encoding'],'ima_adpcm4')
            self.assertEqual(len(uploaded),1)
            self.assertEqual(control['bytes'],len(uploaded[0][1]))
            socket.send_json({'type':'reply_played','reply_id':control['reply_id'],
                              'completed':True,'samples':control['samples'],'gain_divisor':2})
            complete=socket.receive_json()
            self.assertEqual(complete['type'],'audio_complete')

    def test_legacy_device_gets_long_ws_reply_even_when_http_uploader_is_configured(self):
        uploaded=[]
        async def upload(*args):uploaded.append(args)
        async def long_reply(*args):
            async def audio():yield b'\x01\x02'*64000
            return {'transcript':'合成测试','reply_text':'长回复。','_audio':audio()}
        long_reply.http_reply=upload
        self.make_app(long_reply)
        with self.connect() as socket:
            self.hello(socket,5);ready=self.begin(socket);self.send_pcm(socket,ready)
            self.assertTrue(socket.receive_json()['reply_audio_enabled'])
            start=socket.receive_json()
            self.assertEqual(start['type'],'reply_start')
            self.assertEqual(start['samples'],64000)
            socket.send_json({'type':'reply_ready','reply_id':start['reply_id'],
                              'sample_rate':16000,'gain_divisor':2,'max_seconds':8})
            wire=bytearray();sequence=0
            while len(wire)<start['bytes']:
                frame=socket.receive_bytes();wire.extend(frame[HEADER.size:]);sequence+=1
                if sequence%4==0 or len(wire)==start['bytes']:
                    socket.send_json({'type':'reply_ack','reply_id':start['reply_id'],
                                      'next_sequence':sequence,'received_bytes':len(wire)})
            end=socket.receive_json()
            self.assertEqual(end['sha256'],hashlib.sha256(wire).hexdigest())
            socket.send_json({'type':'reply_played','reply_id':start['reply_id'],
                              'completed':True,'samples':start['samples'],'gain_divisor':2})
            self.assertEqual(socket.receive_json()['type'],'audio_complete')
        self.assertEqual(uploaded,[])
        self.assertEqual(self.states(),['complete'])

    def test_11_second_legacy_reply_is_split_into_complete_prefetchable_segments(self):
        source=b'\x01\x02'*88000+b'\x03\x04'*88000
        async def long_reply(*args):
            async def audio():yield source
            return {'transcript':'合成测试','reply_text':'长回复。','_audio':audio()}
        self.make_app(long_reply)
        sizes=[];outputs=[]
        with self.connect() as socket:
            self.hello(socket,5);ready=self.begin(socket);self.send_pcm(socket,ready)
            self.assertTrue(socket.receive_json()['reply_audio_enabled'])
            for segment in range(3):
                start=socket.receive_json();self.assertEqual(start['type'],'reply_start')
                self.assertEqual(start['segment'],segment)
                self.assertLessEqual(start['bytes'],32000)
                sizes.append(start['samples'])
                socket.send_json({'type':'reply_ready','reply_id':start['reply_id'],
                                  'sample_rate':16000,'gain_divisor':2,'max_seconds':8,'window_frames':32})
                wire=bytearray();sequence=0
                while len(wire)<start['bytes']:
                    frame=socket.receive_bytes();wire.extend(frame[HEADER.size:]);sequence+=1
                    if sequence%32==0 or len(wire)==start['bytes']:
                        socket.send_json({'type':'reply_ack','reply_id':start['reply_id'],
                                          'next_sequence':sequence,'received_bytes':len(wire)})
                end=socket.receive_json()
                self.assertEqual(end['sha256'],hashlib.sha256(wire).hexdigest())
                outputs.append(bytes(wire))
                socket.send_json({'type':'reply_played','reply_id':start['reply_id'],
                                  'completed':True,'samples':start['samples'],'gain_divisor':2})
            complete=socket.receive_json()
            self.assertEqual(complete['segments'],3)
            self.assertTrue(complete['playback_complete'])
        self.assertEqual(sizes,[64000,64000,48000])
        self.assertEqual(outputs,[encode(source[offset:offset+128000],0,0)[0]
                                  for offset in range(0,len(source),128000)])
        self.assertEqual(self.states(),['complete'])

    def test_version_two_does_not_start_reply_audio_even_if_handler_has_it(self):
        self.make_app(self.spoken_processor)
        with self.connect() as socket:
            self.hello(socket,2);ready=self.begin(socket);self.send_pcm(socket,ready)
            self.assertFalse(socket.receive_json()['reply_audio_enabled'])
            socket.send_json({'type':'ping','sequence':1})
            self.assertEqual(socket.receive_json()['type'],'pong')

    def test_cancel_waiting_for_device_ready_sends_abort_before_cancelled(self):
        self.make_app(self.spoken_processor)
        with self.connect() as socket:
            self.hello(socket,3);ready=self.begin(socket);self.send_pcm(socket,ready)
            socket.receive_json();start=socket.receive_json()
            self.assertEqual(start['type'],'reply_start')
            socket.send_json({'type':'audio_cancel','stream_id':ready['stream_id']})
            self.assertEqual(socket.receive_json()['type'],'reply_abort')
            self.assertEqual(socket.receive_json()['type'],'audio_cancelled')
            socket.send_json({'type':'ping','sequence':1})
            self.assertEqual(socket.receive_json()['type'],'pong')
        self.assertEqual(self.states(),['failed_or_uncertain'])

    def test_wrong_gain_reply_ack_stops_stream_and_never_completes_request(self):
        self.make_app(self.spoken_processor)
        with self.connect() as socket:
            self.hello(socket,3);ready=self.begin(socket);self.send_pcm(socket,ready)
            socket.receive_json();start=socket.receive_json()
            socket.send_json({'type':'reply_ready','reply_id':start['reply_id'],
                              'sample_rate':16000,'gain_divisor':32,'max_seconds':8})
            with self.assertRaises(WebSocketDisconnect):socket.receive_json()
        self.assertEqual(self.states(),['failed_or_uncertain'])
