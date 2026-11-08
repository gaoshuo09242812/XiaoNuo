import asyncio
from array import array
from pathlib import Path
import tempfile
import threading
import unittest
from uuid import uuid4

from app.budgeting import UsageLedger,BudgetManager
from app.config import Settings
from app.errors import XiaoNuoError
from app.network_speech import LocalNetworkSpeech
from app.speech import wav_bytes
from app.storage import CompanionStore
from test_voice import FakeASR,FakeTTS,FakeCompanion,PCM


class Companion(FakeCompanion):
    def __init__(self):self.calls=[];self.reply='你好，很高兴见到你。'
    def chat(self,text,request,conversation):
        self.calls.append((text,conversation))
        return super().chat()|{'reply_text':self.reply}


class AdapterTests(unittest.TestCase):
    def test_presentation_expression_is_whitelisted_without_generating_audio(self):
        base=self.companion.chat
        for value,expected in [('happy','happy'),('gentle','gentle'),('quiet','quiet'),([], 'neutral'),('PRIVATE-INVALID','neutral'),(None,'neutral')]:
            self.companion.chat=lambda *args,**kwargs:base(*args,**kwargs)|{'emotion_display':value}
            result=asyncio.run(self.adapter(PCM,str(uuid4()),'expression-fixture',self.cancelled))
            self.assertEqual(result['_expression'],expected)
            if '_audio' in result:asyncio.run(result['_audio'].aclose())

    def setUp(self):
        temporary=tempfile.TemporaryDirectory();self.addCleanup(temporary.cleanup)
        settings=Settings(root=Path(temporary.name))
        self.store=CompanionStore(UsageLedger(settings.database))
        self.store.patch_controls({'volume':1.0})
        self.companion=Companion();self.cancelled=threading.Event()
        self.adapter=LocalNetworkSpeech(settings.root,FakeASR(),FakeTTS(),self.companion,
            BudgetManager(self.store.ledger,settings),self.store)

    def test_local_turn_and_audio_keep_accepted_volume_limit(self):
        async def run():
            result=await self.adapter(PCM,str(uuid4()),'device-a',self.cancelled)
            self.assertEqual(result['transcript'],'你好')
            audio=[piece async for piece in result['_audio']]
            self.assertEqual(set(result['_audio_metrics']),{'tts_ms','pcm_conversion_ms','tts_calls'})
            self.assertEqual(result['_audio_metrics']['tts_calls'],1)
            self.assertTrue(all(type(value) is int and value>=0 for value in result['_audio_metrics'].values()))
            self.assertTrue(audio)
            for pcm in audio:
                self.assertLessEqual(len(pcm),8*32000)
                samples=array('h');samples.frombytes(pcm)
                self.assertLessEqual(max(abs(n) for n in samples),round(32767*.95))
        asyncio.run(run())

    def test_conversation_context_is_separate_for_each_device(self):
        async def run():
            for device in ('a','a','b'):
                result=await self.adapter(PCM,str(uuid4()),device,self.cancelled)
                await result['_audio'].aclose()
            self.assertIsNone(self.companion.calls[0][1])
            self.assertIsNotNone(self.companion.calls[1][1])
            self.assertIsNone(self.companion.calls[2][1])
        asyncio.run(run())

    def test_long_synthesis_is_regenerated_in_smaller_pieces_without_lost_text(self):
        class LongTTS(FakeTTS):
            def __init__(self):self.accepted=[]
            def synthesize(self,text,**kwargs):
                seconds=len(text)*.7
                if seconds<=40:self.accepted.append(text)
                return wav_bytes(array('h',[100,-100]*round(seconds*8000)).tobytes())
        async def run():
            tts=LongTTS();self.adapter.tts=tts
            self.companion.reply='记得照顾好自己'*11+'故事讲完啦。'
            result=await self.adapter(PCM,str(uuid4()),'a',self.cancelled)
            pieces=[pcm async for pcm in result['_audio']]
            self.assertGreater(len(pieces),1)
            self.assertEqual(''.join(tts.accepted),self.companion.reply)
            self.assertTrue(all(len(piece)<=8*32000 for piece in pieces))
        asyncio.run(run())

    def test_network_batch_over_eight_seconds_is_generated_once_and_yielded_in_bounded_pieces(self):
        class BatchTTS(FakeTTS):
            def __init__(self):self.calls=[]
            def synthesize(self,text,**kwargs):
                self.calls.append(text)
                return wav_bytes(array('h',[100,-100]*80000).tobytes())
        async def run():
            tts=BatchTTS();self.adapter.tts=tts
            # More than the previous 64-character limit must still use one
            # synthesis, while yielding bounded PCM chunks for transport.
            text='记得照顾好自己'*11
            pieces=[piece async for piece in self.adapter.audio(text,str(uuid4()),self.cancelled)]
            self.assertEqual(tts.calls,[text])
            self.assertEqual([len(piece) for piece in pieces],[8*32000,2*32000])
        asyncio.run(run())

    def test_provider_length_rejection_splits_text_without_losing_suffix(self):
        class BatchTTS(FakeTTS):
            def __init__(self):self.accepted=[];self.rejected=0
            def synthesize(self,text,**kwargs):
                if len(text)>40:
                    self.rejected+=1;raise XiaoNuoError('TTS_AUDIO_LENGTH_INVALID')
                self.accepted.append(text)
                return wav_bytes(bytes(3200))
        async def run():
            tts=BatchTTS();self.adapter.tts=tts
            text='记得照顾好自己'*9
            pieces=[piece async for piece in self.adapter.audio(text,str(uuid4()),self.cancelled)]
            self.assertTrue(pieces);self.assertGreater(tts.rejected,0)
            self.assertEqual(''.join(tts.accepted),text)
        asyncio.run(run())

    def test_cancel_after_asr_never_calls_chat(self):
        old=self.adapter.asr.transcribe
        def cancelled(pcm):
            self.cancelled.set();return old(pcm)
        self.adapter.asr.transcribe=cancelled
        async def run():
            with self.assertRaisesRegex(XiaoNuoError,'CANCELLED'):
                await self.adapter(PCM,str(uuid4()),'a',self.cancelled)
            self.assertEqual(self.companion.calls,[])
        asyncio.run(run())

    def test_muted_turn_does_not_prepare_audio(self):
        self.store.patch_controls({'volume':0})
        async def run():
            result=await self.adapter(PCM,str(uuid4()),'a',self.cancelled)
            self.assertNotIn('_audio',result)
        asyncio.run(run())

    def test_worker_lock_stays_owned_by_actual_worker(self):
        started=threading.Event();release=threading.Event()
        old=self.adapter.asr.transcribe
        def blocking(pcm):
            started.set();release.wait(3);return old(pcm)
        self.adapter.asr.transcribe=blocking
        async def run():
            task=asyncio.create_task(self.adapter(PCM,str(uuid4()),'a',self.cancelled))
            try:
                self.assertTrue(await asyncio.to_thread(started.wait,2))
                self.cancelled.set();task.cancel()
                with self.assertRaises(asyncio.CancelledError):await task
                with self.assertRaisesRegex(XiaoNuoError,'BUSY'):
                    await self.adapter(PCM,str(uuid4()),'b',threading.Event())
            finally:release.set()
        asyncio.run(run())

    def test_privacy_context_clear_waits_for_real_worker_after_async_cancel(self):
        started=threading.Event();release=threading.Event()
        old=self.adapter.asr.transcribe
        def blocking(pcm):
            started.set();release.wait(3);return old(pcm)
        self.adapter.asr.transcribe=blocking
        async def run():
            self.adapter._conversations['old-device']=('private-context',0)
            task=asyncio.create_task(self.adapter(PCM,str(uuid4()),'a',self.cancelled))
            try:
                self.assertTrue(await asyncio.to_thread(started.wait,2))
                self.cancelled.set();task.cancel()
                with self.assertRaises(asyncio.CancelledError):await task
                with self.assertRaisesRegex(XiaoNuoError,'WORKER_STILL_STOPPING'):
                    await self.adapter.clear_context(timeout=.02)
                self.assertIn('old-device',self.adapter._conversations)
                release.set()
                await self.adapter.clear_context(timeout=2)
                self.assertFalse(self.adapter._conversations)
                self.assertFalse(self.companion.calls)
            finally:release.set()
        asyncio.run(run())
