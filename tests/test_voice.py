"""Offline contract checks. No microphone, paid service or model is opened."""
from array import array
import asyncio
import base64
from dataclasses import replace
from io import BytesIO
import json
from pathlib import Path
import queue
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch
from uuid import uuid4
import wave

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'backend'))
from app.budgeting import BudgetManager,UsageLedger
from app.config import Settings,ProviderSpec,Mode
from app.device_bridge import DeviceBridge,DeviceRequest
from app.errors import XiaoNuoError
from app.llm.health import ProviderHealthMonitor
from app.llm.router import ModelRouter
from app.main import create_app
from app.speech import AudioCache,VoskASR,VoiceService,audio_statistics,pcm_from_wav,wav_bytes,MAX_PCM
from app.storage import CompanionStore
from app.behavior import Context,ResponseStyleManager
from app.companion import CompanionService
from test_core import FakeProvider,request_asgi


PCM=array('h',[100,-100]*16000).tobytes()


class FakeBridge:
    def __init__(self):self.calls=0;self.cancelled=False
    def capture_audio(self,seconds,**kwargs):
        self.calls+=1
        return {'pcm':PCM,'samples':len(PCM)//2,'audio_saved':False}
    def cancel_capture(self):self.cancelled=True
    def cancel_playback(self):self.cancelled=True
    def status(self):return {'connected':False,'info':{}}


class FakeASR:
    last_success=None
    def transcribe(self,pcm):
        self.last_success=time.time()
        return {'text':'你好','duration_seconds':len(pcm)/32000,'confidence':0.9}
    def status(self):return {'provider':'offline_test_asr','model':'test','last_success':self.last_success}


class FakeTTS:
    last_success=None
    def __init__(self,callback=None):self.callback=callback
    def synthesize(self,text,**kwargs):
        if self.callback:self.callback()
        self.last_success=time.time()
        return wav_bytes(PCM)
    def status(self):return {'provider':'offline_test_tts','last_success':self.last_success}


class FakeCompanion:
    def chat(self,*args):
        return {'reply_text':'你好。','speech_style':{'enabled':True},'conversation_id':str(uuid4())}


class PacketPort:
    def __init__(self,messages):
        self.messages=queue.Queue()
        for item in messages:self.messages.put(json.dumps(item).encode()+b'\n')
        self.writes=[]
    def write(self,data):self.writes.append(data)
    def readline(self,limit):
        try:return self.messages.get_nowait()
        except queue.Empty:return b''


def packets(data=PCM):
    result=[{'event':'audio_start','sample_rate':16000,'channels':1,'bits':16,'max_samples':32000}]
    for offset in range(0,len(data),1024):
        result.append({'event':'audio_chunk','seq':offset//1024,'data':base64.b64encode(data[offset:offset+1024]).decode()})
    result.append({'event':'audio_end','samples':len(data)//2,'chunks':len(result)-1,'cancelled':False,'failed':False})
    return result


class VoiceTests(unittest.TestCase):
    def setUp(self):
        temp=tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root=Path(temp.name)
        self.settings=Settings(root=self.root,admin_token='offline-test-'+40*'x')
        self.ledger=UsageLedger(self.settings.database)
        self.store=CompanionStore(self.ledger)
        self.budget=BudgetManager(self.ledger,self.settings)
        self.bridge=FakeBridge()
        self.voice=VoiceService(self.root,self.bridge,FakeCompanion(),self.budget,self.store,asr=FakeASR(),tts=FakeTTS())

    def test_pcm_wav_roundtrip_and_duration(self):
        self.assertEqual(pcm_from_wav(wav_bytes(PCM)),PCM)
        self.assertEqual(audio_statistics(PCM)['duration_seconds'],2)

    def test_rejects_empty_odd_oversized_and_stereo_audio(self):
        for pcm in [b'',b'1',bytes(MAX_PCM+2)]:
            with self.assertRaises(XiaoNuoError):audio_statistics(pcm)
        with BytesIO() as stream:
            with wave.open(stream,'wb') as wav:
                wav.setnchannels(2);wav.setsampwidth(2);wav.setframerate(16000);wav.writeframes(PCM)
            with self.assertRaisesRegex(XiaoNuoError,'AUDIO_FORMAT_REQUIRES'):
                pcm_from_wav(stream.getvalue())

    def test_silence_does_not_start_asr_worker(self):
        with patch('app.speech.subprocess.run') as run:
            with self.assertRaisesRegex(XiaoNuoError,'ASR_EMPTY_AUDIO'):VoskASR(self.root).transcribe(bytes(32000))
            run.assert_not_called()

    def test_asr_worker_timeout_is_bounded_and_sanitized(self):
        model=self.root/'data/models/vosk-model-small-cn-0.22/am/final.mdl'
        model.parent.mkdir(parents=True);model.touch()
        with patch('app.speech.subprocess.run',side_effect=subprocess.TimeoutExpired('offline-test',20)):
            with self.assertRaisesRegex(XiaoNuoError,'ASR_TIMEOUT'):VoskASR(self.root).transcribe(PCM)

    def test_recording_requires_local_consent_before_device_access(self):
        with self.assertRaisesRegex(XiaoNuoError,'AUDIO_CONSENT_REQUIRED'):
            self.voice.device_turn(2,str(uuid4()),None,'cloud')
        self.assertEqual(self.bridge.calls,0)

    def test_local_voice_turn_records_separate_zero_api_fees_without_audio_file(self):
        result=self.voice.device_turn(2,str(uuid4()),None,'local-only')
        self.assertEqual(result['transcript'],'你好')
        self.assertNotIn('pcm',result['capture'])
        self.assertFalse(result['audio_saved'])
        summary=self.budget.snapshot()
        self.assertEqual(set(summary['categories']),{'ASR','TTS'})
        self.assertEqual(summary['api_cost_estimate_usd'],'0')
        self.assertEqual(list(self.root.rglob('*.wav')),[])
        with self.ledger.connect() as db:
            rows=db.execute('SELECT audio_seconds,billed_characters,cost_kind FROM api_usage ORDER BY category').fetchall()
        self.assertEqual(float(rows[0]['audio_seconds']),2)
        self.assertEqual(rows[1]['billed_characters'],3)
        self.assertTrue(all(row['cost_kind']=='local_compute_no_api_fee' for row in rows))

    def test_quiet_change_during_tts_discards_audio_and_tracks_work(self):
        self.voice.tts=FakeTTS(lambda:self.store.set_quiet(True))
        with self.assertRaisesRegex(XiaoNuoError,'SPEECH_DISABLED_BY_OWNER'):
            self.voice.synthesize('你好。',str(uuid4()))
        self.assertEqual(self.voice.cache._items,{})
        self.assertEqual(self.budget.snapshot()['categories']['TTS']['calls'],1)

    def test_cancel_during_tts_never_returns_a_playable_url(self):
        self.voice.tts=FakeTTS(self.voice.cancel)
        result=self.voice.device_turn(2,str(uuid4()),None,'local-only')
        self.assertEqual(result['speech_error'],'AUDIO_CAPTURE_CANCELLED')
        self.assertNotIn('speech',result)
        self.assertEqual(self.voice.cache._items,{})

    def test_concurrent_voice_request_does_not_open_second_recording(self):
        self.voice._turn.acquire()
        try:
            with self.assertRaisesRegex(XiaoNuoError,'VOICE_BUSY'):
                self.voice.device_turn(2,str(uuid4()),None,'local-only')
        finally:self.voice._turn.release()
        self.assertEqual(self.bridge.calls,0)

    def test_cache_expires_and_quiet_blocks_cached_playback(self):
        cache=AudioCache();token=cache.put(b'test')
        future=time.monotonic()+91
        with patch('app.speech.time.monotonic',return_value=future):
            with self.assertRaisesRegex(XiaoNuoError,'AUDIO_EXPIRED'):cache.get(token)

    def test_voice_api_auth_consent_and_duration_validation(self):
        app=create_app(self.settings)
        async def scenario():
            for path in ['/api/v1/voice','/api/v1/voice/audio/private-token']:
                status,_=await request_asgi(app,path)
                self.assertEqual(status,401)
            status,_=await request_asgi(app,'/api/v1/voice/device',token=self.settings.admin_token,method='POST',body={'seconds':6})
            self.assertEqual(status,422)
            status,_=await request_asgi(app,'/api/v1/voice/device',token=self.settings.admin_token,method='POST',body={'seconds':7,'consent':'local-only'})
            self.assertEqual(status,422)
            token=app.state.voice_service.cache.put(b'private-audio')
            app.state.companion.store.set_quiet(True)
            status,_=await request_asgi(app,'/api/v1/voice/audio/'+token,token=self.settings.admin_token)
            self.assertEqual(status,409)
            self.assertEqual(app.state.voice_service.cache._items,{})
        asyncio.run(scenario())

    def capture(self,messages,cancel=False):
        bridge=DeviceBridge(self.settings,self.store)
        port=PacketPort(messages);bridge._port=port
        if cancel:bridge._capture_cancel.set()
        return bridge,port,DeviceRequest('capture_audio',audio_seconds=2)

    def test_usb_frame_sequence_roundtrip(self):
        bridge,port,request=self.capture(packets())
        result=bridge._capture_audio(request)
        self.assertEqual(result['pcm'],PCM)
        self.assertEqual(result['samples'],32000)
        self.assertEqual(bridge.status()['capture_state'],'idle')

    def test_usb_missing_duplicate_and_malformed_frames_are_rejected(self):
        cases=[]
        bad=packets();bad[2]['seq']=0;cases.append((bad,'AUDIO_SEQUENCE_MISMATCH'))
        bad=packets();bad[1]['data']='@';cases.append((bad,'INVALID_AUDIO_FRAME'))
        bad=packets();bad[-1].pop('samples');cases.append((bad,'AUDIO_CAPTURE_INCOMPLETE'))
        bad=packets();bad[-1]['samples']=False;cases.append((bad,'AUDIO_CAPTURE_INCOMPLETE'))
        for messages,error in cases:
            bridge,port,request=self.capture(messages)
            with self.assertRaisesRegex(XiaoNuoError,error):bridge._capture_audio(request)
            self.assertEqual(bridge.status()['capture_state'],'idle')

    def test_cancelled_usb_capture_discards_samples_and_sends_cancel(self):
        bridge,port,request=self.capture(packets(),cancel=True)
        with self.assertRaisesRegex(XiaoNuoError,'AUDIO_CAPTURE_CANCELLED'):bridge._capture_audio(request)
        self.assertIn(b'CAPTURE_CANCEL\n',port.writes)

    def test_owner_cancel_before_queued_capture_never_starts_microphone(self):
        bridge,port,request=self.capture(packets())
        owner=threading.Event();owner.set();request.external_cancel=owner
        with self.assertRaisesRegex(XiaoNuoError,'AUDIO_CAPTURE_CANCELLED'):bridge._capture_audio(request)
        self.assertFalse(any(write.startswith(b'CAPTURE ') for write in port.writes))
        self.assertEqual(bridge.status()['capture_state'],'idle')

    def test_verified_local_model_works_with_remote_and_paid_disabled(self):
        settings=replace(self.settings,local_llm_enabled=True,remote_enabled=False,allow_paid=False)
        provider=FakeProvider(ProviderSpec('local_qwen','Qwen3-0.6B-Q8_0','offline-runtime-test',True,True))
        router=ModelRouter(settings,{'local_qwen':provider},self.budget,ProviderHealthMonitor(self.ledger))
        with self.assertRaisesRegex(XiaoNuoError,'NO_ELIGIBLE_PROVIDER'):
            router.reply(str(uuid4()),'system',[{'role':'user','content':'你好'}])
        router.verify_local()
        answer=router.reply(str(uuid4()),'system',[{'role':'user','content':'你好'}])
        self.assertEqual(answer.cost_status,'local_compute_no_api_fee')
        self.assertEqual(self.budget.snapshot()['api_cost_estimate_usd'],'0')
        router.settings=replace(settings,mode=Mode.PAID_ONLY)
        with self.assertRaisesRegex(XiaoNuoError,'NO_ELIGIBLE_PROVIDER'):
            router.reply(str(uuid4()),'system',[])
        router.settings=replace(settings,mode=Mode.DISABLED)
        with self.assertRaisesRegex(XiaoNuoError,'REMOTE_CALLS_DISABLED'):
            router.reply(str(uuid4()),'system',[])

    def test_unwanted_invitation_tail_is_removed_without_changing_answer(self):
        manager=ResponseStyleManager()
        style=manager.choose(Context(),self.store.controls())
        answer='小糯是一只AI小猫。不过现在请你直接告诉我你的问题，好吗？'
        self.assertEqual(manager.enforce(answer,style),'小糯是一只AI小猫。')

    def test_factual_answer_keeps_content_and_removes_unrequested_stage_directions(self):
        manager=ResponseStyleManager()
        style=manager.choose(Context('studying',0.85),self.store.controls())
        answer='一加一等于两！小糯眨眨眼睛，尾巴轻轻摆动，温柔地说：“喵～”'
        self.assertEqual(manager.enforce(answer,style),'一加一等于两！')

    def test_local_model_uses_local_memory_without_upload_permission(self):
        settings=replace(self.settings,local_llm_enabled=True,remote_enabled=False)
        class CapturingProvider(FakeProvider):
            def generate(provider,system,messages,max_output_tokens):
                provider.last_system=system
                return super().generate(system,messages,max_output_tokens)
        provider=CapturingProvider(ProviderSpec('local_qwen','Qwen3-0.6B-Q8_0','offline-test',True,True))
        router=ModelRouter(settings,{'local_qwen':provider},self.budget,ProviderHealthMonitor(self.ledger))
        router.verify_local()
        self.store.add_memory('喜欢蓝色',consent=True,allow_remote=False)
        answer=CompanionService(router,self.store).chat('蓝色',str(uuid4()),None)
        self.assertEqual(answer['diagnostics']['memories_used'],1)
        self.assertIn('喜欢蓝色',provider.last_system)

    def test_privacy_clear_also_cancels_voice_and_discards_cached_audio(self):
        app=create_app(self.settings)
        app.state.voice_service.cache.put(b'private-audio')
        async def scenario():
            status,_=await request_asgi(app,'/api/v1/privacy/data',token=self.settings.admin_token,method='DELETE',body={'confirm':'DELETE_PERSONAL_DATA'})
            self.assertEqual(status,200)
        asyncio.run(scenario())
        self.assertTrue(app.state.voice_service._cancelled.is_set())
        self.assertEqual(app.state.voice_service.cache._items,{})


if __name__=='__main__':unittest.main()
