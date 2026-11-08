"""One explicit voice turn: no real port, microphone, model, or audio output."""
from array import array
import asyncio
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import tempfile
import threading
import unittest
from uuid import uuid4
import sys

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'backend'))
from app.budgeting import BudgetManager,UsageLedger
from app.config import Settings
from app.errors import XiaoNuoError
from app.main import create_app
from app.speech import VoiceService,split_spoken_reply,wav_bytes
from app.storage import CompanionStore
from test_core import request_asgi
from test_voice import FakeASR,FakeTTS,FakeCompanion,PCM


class SpeakingBridge:
    def __init__(self):
        self.events=[];self.capture_calls=0;self.played=[];self.on_play=None
        self.connected=True;self.speaker_enabled=True;self.available=True
    def status(self):
        return {'connected':self.connected,'speaker_enabled':self.speaker_enabled,
            'info':{'speaker_available':self.available,'speaker_gain_divisor':2}}
    def execute(self,operation,timeout=2):
        self.events.append(('activity',operation));return {'event':'task_status'}
    def capture_audio(self,seconds,**kwargs):
        self.events.append(('capture',seconds));self.capture_calls+=1
        return {'pcm':PCM,'samples':len(PCM)//2,'audio_saved':False}
    def play_audio(self,pcm,**kwargs):
        assert 0<len(pcm)<=16000*2*8 and len(pcm)%2==0
        self.events.append(('play',len(pcm)));self.played.append(pcm)
        if self.on_play:self.on_play()
        return {'device_completed':True,'audible_verified':False,'gain_divisor':2}
    def cancel_capture(self):self.events.append(('cancel_capture',))
    def cancel_playback(self):self.events.append(('cancel_playback',))


class ReplyCompanion:
    def __init__(self,text='你好，很高兴认识你。'):self.text=text;self.calls=0
    def chat(self,*args):
        self.calls+=1
        return {'reply_text':self.text,'speech_style':{'enabled':True},'conversation_id':str(uuid4())}


class TextDurationTTS(FakeTTS):
    def __init__(self):super().__init__();self.texts=[]
    def synthesize(self,text,**kwargs):
        self.texts.append(text)
        # Deliberately exceed 8s for some initial segments to exercise subdivision.
        return wav_bytes(array('h',[100,-100]*round(len(text)*0.45*8000)).tobytes())


class DialogueTests(unittest.TestCase):
    def setUp(self):
        temp=tempfile.TemporaryDirectory();self.addCleanup(temp.cleanup)
        self.settings=Settings(root=Path(temp.name),admin_token='test-'+40*'x')
        self.ledger=UsageLedger(self.settings.database)
        self.store=CompanionStore(self.ledger);self.budget=BudgetManager(self.ledger,self.settings)
        self.bridge=SpeakingBridge();self.companion=ReplyCompanion()
        self.voice=VoiceService(self.settings.root,self.bridge,self.companion,self.budget,self.store,
            asr=FakeASR(),tts=FakeTTS())
    def run_turn(self,identifier=None,mode='device'):
        return self.voice.device_turn(2,identifier or str(uuid4()),None,'local-only',mode)

    def test_explicit_device_mode_cues_records_and_speaks_then_clears_progress(self):
        result=self.run_turn()
        self.assertEqual(self.bridge.capture_calls,1)
        self.assertEqual([event[0] for event in self.bridge.events if event[0] in {'play','capture'}],['play','capture','play'])
        self.assertTrue(result['capture_cue']['played']);self.assertTrue(result['playback_complete'])
        self.assertFalse(result['spoken_segments'][0]['audible_verified'])
        self.assertNotIn('pcm',result['capture']);self.assertNotIn('speech',result)
        self.assertEqual(self.voice.cache._items,{})
        self.assertEqual(self.voice.status()['progress'],{'stage':'idle'})
        self.assertEqual(self.bridge.events[-1],('activity','activity_clear'))

    def test_preview_mode_remains_non_audible(self):
        result=self.run_turn(mode='preview')
        self.assertEqual(self.bridge.played,[])
        self.assertFalse(result['capture_cue']['played']);self.assertIn('speech',result)

    def test_duplicate_request_never_opens_second_recording_even_after_service_restart(self):
        identifier=str(uuid4());self.run_turn(identifier)
        self.voice=VoiceService(self.settings.root,self.bridge,self.companion,self.budget,self.store,
            asr=FakeASR(),tts=FakeTTS())
        with self.assertRaisesRegex(XiaoNuoError,'DUPLICATE_REQUEST'):self.run_turn(identifier)
        self.assertEqual(self.bridge.capture_calls,1)

    def test_failed_asr_also_keeps_request_claimed_against_automatic_retry(self):
        def fail(pcm,**kwargs):raise XiaoNuoError('ASR_NO_SPEECH_RECOGNIZED')
        self.voice.asr.transcribe=fail;identifier=str(uuid4())
        with self.assertRaisesRegex(XiaoNuoError,'ASR_NO_SPEECH_RECOGNIZED'):self.run_turn(identifier)
        with self.assertRaisesRegex(XiaoNuoError,'DUPLICATE_REQUEST'):self.run_turn(identifier)
        self.assertEqual(self.bridge.capture_calls,1)

    def test_missing_speaker_and_quiet_fail_before_cue_or_capture(self):
        for attribute,value,error in [('connected',False,'DEVICE_NOT_CONNECTED'),
            ('speaker_enabled',False,'DEVICE_SPEAKER_DISABLED'),('available',False,'DEVICE_FIRMWARE_NEEDS_UPDATE')]:
            setattr(self.bridge,attribute,value)
            with self.assertRaisesRegex(XiaoNuoError,error):self.run_turn()
            setattr(self.bridge,attribute,True)
        self.store.set_quiet(True)
        with self.assertRaisesRegex(XiaoNuoError,'SPEECH_DISABLED_BY_OWNER'):self.run_turn()
        self.assertEqual(self.bridge.capture_calls,0);self.assertEqual(self.bridge.played,[])

    def test_invalid_duration_and_mode_fail_before_capture(self):
        for seconds in [True,1,7,2.0]:
            with self.assertRaisesRegex(XiaoNuoError,'INVALID_RECORDING_DURATION'):
                self.voice.device_turn(seconds,str(uuid4()),None,'local-only','device')
        for mode in ['unknown',None,{}]:
            with self.assertRaisesRegex(XiaoNuoError,'INVALID_REPLY_PLAYBACK'):self.run_turn(mode=mode)
        self.assertEqual(self.bridge.capture_calls,0)

    def test_long_reply_is_subdivided_to_actual_eight_second_limit_without_lost_text(self):
        text='今天很开心，我们可以聊一聊你喜欢的事情。'*4
        self.companion.text=text;tts=TextDurationTTS();self.voice.tts=tts
        result=self.run_turn()
        self.assertTrue(result['playback_complete'])
        self.assertEqual(''.join(part['text'] for part in result['spoken_segments']),text)
        self.assertTrue(all(part['duration_seconds']<=8 for part in result['spoken_segments']))
        self.assertGreater(len(tts.texts),len(result['spoken_segments']))
        self.assertEqual(self.voice.cache._items,{})

    def test_stop_after_first_reply_fragment_prevents_all_remaining_fragments(self):
        self.companion.text='今天很开心，我们可以聊一聊你喜欢的事情。'*4
        self.bridge.on_play=lambda:self.voice.cancel() if len(self.bridge.played)==2 else None
        result=self.run_turn()
        self.assertEqual(result['speech_error'],'AUDIO_CAPTURE_CANCELLED')
        self.assertFalse(result['playback_complete']);self.assertEqual(len(result['spoken_segments']),1)
        self.assertEqual(len(self.bridge.played),2);self.assertEqual(self.voice.cache._items,{})

    def test_quiet_between_fragments_cannot_be_undone_by_activity_cleanup(self):
        self.companion.text='今天很开心，我们可以聊一聊你喜欢的事情。'*4
        self.bridge.on_play=lambda:self.store.set_quiet(True) if len(self.bridge.played)==2 else None
        result=self.run_turn()
        self.assertEqual(result['speech_error'],'SPEECH_DISABLED_BY_OWNER')
        self.assertTrue(self.store.controls()['quiet']);self.assertEqual(len(self.bridge.played),2)

    def test_tts_error_keeps_text_and_does_not_claim_spoken_success(self):
        def fail(*args,**kwargs):raise XiaoNuoError('TTS_PROVIDER_FAILED')
        self.voice.tts.synthesize=fail;result=self.run_turn()
        self.assertEqual(result['reply']['reply_text'],self.companion.text)
        self.assertEqual(result['speech_error'],'TTS_PROVIDER_FAILED');self.assertFalse(result['playback_complete'])
        self.assertEqual(len(self.bridge.played),1)

    def test_failed_cue_never_opens_microphone(self):
        self.bridge.play_audio=lambda pcm,**kwargs:{'device_completed':False}
        with self.assertRaisesRegex(XiaoNuoError,'SPEAKER_INCOMPLETE'):self.run_turn()
        self.assertEqual(self.bridge.capture_calls,0)

    def test_zero_volume_keeps_cue_silent_and_reply_samples_zero(self):
        self.store.patch_controls({'volume':0});result=self.run_turn()
        self.assertFalse(result['capture_cue']['played']);self.assertTrue(result['playback_muted'])
        self.assertTrue(all(not any(pcm) for pcm in self.bridge.played))

    def test_incomplete_playback_stops_without_retrying_or_claiming_completion(self):
        original=self.bridge.play_audio
        def fail(pcm,**kwargs):
            result=original(pcm,**kwargs)
            if len(self.bridge.played)>1:raise XiaoNuoError('SPEAKER_TRANSFER_FAILED')
            return result
        self.bridge.play_audio=fail;self.companion.text='今天很开心。'*20
        result=self.run_turn()
        self.assertEqual(result['speech_error'],'SPEAKER_TRANSFER_FAILED');self.assertFalse(result['playback_complete'])
        self.assertEqual(len(self.bridge.played),2)

    def test_reply_too_long_falls_back_to_text_without_speaking_a_truncated_prefix(self):
        self.companion.text='很长的回复。'*201;result=self.run_turn()
        self.assertEqual(result['speech_error'],'SPEAKER_REPLY_TOO_LONG')
        self.assertEqual(result['reply']['reply_text'],self.companion.text)
        self.assertEqual(len(self.bridge.played),1)

    def test_inflight_transcript_is_ephemeral_and_cleared_after_turn(self):
        entered=threading.Event();release=threading.Event()
        def waiting(*args):
            entered.set();release.wait(timeout=3)
            return FakeCompanion().chat()
        self.companion.chat=waiting
        with ThreadPoolExecutor(max_workers=1) as pool:
            future=pool.submit(self.run_turn)
            self.assertTrue(entered.wait(timeout=2))
            self.assertEqual(self.voice.status()['progress']['transcript'],'你好')
            release.set();future.result(timeout=5)
        self.assertEqual(self.voice.status()['progress'],{'stage':'idle'})

    def test_api_device_reply_is_explicit_and_progress_does_not_expose_pcm(self):
        app=create_app(self.settings);voice=app.state.voice_service
        voice.bridge=self.bridge;voice.asr=FakeASR();voice.tts=FakeTTS();voice.companion=self.companion
        async def scenario():
            body={'consent':'local-only','seconds':2,'request_id':str(uuid4()),'reply_playback':'device'}
            status,result=await request_asgi(app,'/api/v1/voice/device',method='POST',body=body,token=self.settings.admin_token)
            self.assertEqual(status,200);self.assertTrue(result['playback_complete']);self.assertNotIn('pcm',result['capture'])
            status,_=await request_asgi(app,'/api/v1/voice/device',method='POST',body=body,token=self.settings.admin_token)
            self.assertEqual(status,409)
            body['reply_playback']='always-listen'
            status,_=await request_asgi(app,'/api/v1/voice/device',method='POST',body=body,token=self.settings.admin_token)
            self.assertEqual(status,422)
        asyncio.run(scenario())

    def test_segmenter_preserves_content_and_respects_boundaries(self):
        text='你好，我是小糯。今天我们聊聊你喜欢的颜色。还有明天的计划。'
        parts=split_spoken_reply(text,12)
        self.assertEqual(''.join(parts),text);self.assertTrue(all(len(part)<=12 for part in parts))

    def test_preview_synthesis_is_serialized_and_duplicate_does_not_regenerate(self):
        identifier=str(uuid4());result=self.voice.synthesize_preview('你好。',identifier)
        self.assertIn('audio_url',result);self.assertEqual(self.bridge.capture_calls,0)
        with self.assertRaisesRegex(XiaoNuoError,'DUPLICATE_REQUEST'):
            self.voice.synthesize_preview('你好。',identifier)
        self.voice._turn.acquire()
        try:
            with self.assertRaisesRegex(XiaoNuoError,'VOICE_BUSY'):
                self.voice.synthesize_preview('你好。',str(uuid4()))
        finally:self.voice._turn.release()

    def test_cancelled_preview_cannot_repopulate_cache_after_stop(self):
        self.voice.tts=FakeTTS(self.voice.cancel)
        with self.assertRaisesRegex(XiaoNuoError,'AUDIO_CAPTURE_CANCELLED'):
            self.voice.synthesize_preview('你好。',str(uuid4()))
        self.assertEqual(self.voice.cache._items,{})
        self.assertFalse(self.voice._turn.locked())
        self.assertEqual(self.voice.status()['progress'],{'stage':'idle'})

    def test_clear_between_cancel_check_and_cache_commit_cannot_restore_audio(self):
        original=self.voice.cache.put
        def racing(data,**kwargs):
            self.voice.cancel()
            return original(data,**kwargs)
        self.voice.cache.put=racing
        with self.assertRaisesRegex(XiaoNuoError,'AUDIO_CAPTURE_CANCELLED'):
            self.voice.synthesize_preview('你好。',str(uuid4()))
        self.assertEqual(self.voice.cache._items,{})

    def test_cache_clear_alone_invalidates_an_inflight_generation(self):
        self.voice.tts=FakeTTS(self.voice.cache.clear)
        with self.assertRaisesRegex(XiaoNuoError,'AUDIO_CAPTURE_CANCELLED'):
            self.voice.synthesize_preview('你好。',str(uuid4()))
        self.assertEqual(self.voice.cache._items,{})

    def test_privacy_clear_during_api_preview_discards_late_audio(self):
        app=create_app(self.settings);voice=app.state.voice_service
        entered=threading.Event();release=threading.Event()
        def waiting():entered.set();release.wait(timeout=3)
        voice.tts=FakeTTS(waiting)
        async def preview():
            return await request_asgi(app,'/api/v1/voice/synthesize',method='POST',
                body={'text':'私有试听文字','request_id':str(uuid4())},token=self.settings.admin_token)
        with ThreadPoolExecutor(max_workers=1) as pool:
            future=pool.submit(asyncio.run,preview())
            self.assertTrue(entered.wait(timeout=2))
            status,_=asyncio.run(request_asgi(app,'/api/v1/privacy/data',method='DELETE',
                body={'confirm':'DELETE_PERSONAL_DATA'},token=self.settings.admin_token))
            self.assertEqual(status,200)
            release.set();status,result=future.result(timeout=5)
        self.assertEqual(status,409);self.assertEqual(result['error'],'AUDIO_CAPTURE_CANCELLED')
        self.assertEqual(voice.cache._items,{})
