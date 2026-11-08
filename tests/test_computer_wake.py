import asyncio
from pathlib import Path
import tempfile
import threading
import unittest
from uuid import uuid4
from app.computer_wake import ComputerWakeGate
from app.config import Settings
from app.budgeting import UsageLedger,BudgetManager
from app.storage import CompanionStore
from app.network_speech import LocalNetworkSpeech
from app.errors import XiaoNuoError
from test_voice import FakeASR,FakeTTS,PCM


def recognition(text,confidence=.98,words=None):
    return {'text':text,'confidence':confidence,'duration_seconds':2,'words':words or []}


class WakeGateTests(unittest.TestCase):
    def setUp(self):
        self.now=0.;self.gate=ComputerWakeGate(clock=lambda:self.now)
    def decide(self,text,**kwargs):return self.gate.decide(recognition(text), 'a',**kwargs)
    def test_exact_name_and_observed_homophone_open_one_followup(self):
        for name in ('小糯','小诺','你好小糯'):
            self.assertEqual(self.decide(name).status,'listening')
            self.assertEqual(self.decide('今天我很开心').status,'accepted')
            self.assertEqual(self.decide('今天我很开心').status,'ignored')
    def test_unaddressed_and_substrings_never_wake(self):
        for text in ('你好','今天我很开心','我说小糯','小糯米很好吃','小诺贝尔','小路你好'):
            self.assertEqual(self.decide(text).status,'ignored',text)
    def test_unpunctuated_asr_requires_name_word_boundary(self):
        words=[{'word':'小诺','confidence':.98},{'word':'今天','confidence':.98}]
        result=self.gate.decide(recognition('小诺今天我很开心',words=words),'a')
        self.assertEqual((result.status,result.text),('accepted','今天我很开心'))
        self.assertEqual(self.gate.decide(recognition('小诺米很好吃',words=[{'word':'小诺米','confidence':1}]),'a').status,'ignored')
    def test_low_confidence_and_wrong_name_score_are_ignored(self):
        for value in (None,True,.1,float('nan'),float('inf')):
            self.assertEqual(self.gate.decide(recognition('小糯',value),'a').status,'ignored')
        words=[{'word':'小诺','confidence':.2},{'word':'今天','confidence':1}]
        self.assertEqual(self.gate.decide(recognition('小诺今天开心',words=words),'a').status,'ignored')
    def test_followup_expiry_cancel_and_device_isolation(self):
        self.decide('小糯');self.now=20
        self.assertEqual(self.decide('你好').status,'ignored')
        self.decide('小糯')
        self.assertEqual(self.gate.decide(recognition('你好'),'other').status,'ignored')
        self.assertEqual(self.decide('取消').status,'cancelled')
        self.assertEqual(self.decide('你好').status,'ignored')
    def test_quiet_and_owner_reset_prevent_old_context_reviving(self):
        old=self.gate.generation();self.gate.reset()
        self.assertEqual(self.decide('小糯',generation=old).status,'ignored')
        self.decide('小糯')
        self.assertEqual(self.decide('你好',quiet=True).status,'ignored')
        self.assertEqual(self.decide('小糯',quiet=True).status,'listening')
    def test_context_is_bounded(self):
        for device in range(20):self.gate.decide(recognition('小糯'),str(device))
        self.assertEqual(len(self.gate._followup),8)


class WakeAdapterTests(unittest.TestCase):
    def setUp(self):
        temporary=tempfile.TemporaryDirectory();self.addCleanup(temporary.cleanup)
        settings=Settings(root=Path(temporary.name))
        self.store=CompanionStore(UsageLedger(settings.database));self.store.patch_controls({'computer_wake_enabled':True})
        self.asr=FakeASR();self.text='小糯';self.asr.transcribe=lambda pcm:recognition(self.text)
        parent=self
        class Companion:
            def __init__(self):self.calls=[]
            def local(self,text,*args):return {'reply_text':text,'speech_style':{'enabled':True}}
            def chat(self,text,*args):
                self.calls.append(text)
                if text=='安静一下':parent.store.set_quiet(True)
                return {'reply_text':'收到。','speech_style':{'enabled':True},'conversation_id':str(uuid4())}
        self.companion=Companion();self.cancelled=threading.Event()
        self.adapter=LocalNetworkSpeech(settings.root,self.asr,FakeTTS(),self.companion,
            BudgetManager(self.store.ledger,settings),self.store,computer_wake=True)
    async def probe(self):return await self.adapter.wake_probe(PCM,str(uuid4()),'fixture',self.cancelled)
    def test_background_and_name_only_do_not_call_chat(self):
        async def run():
            self.text='我今天很开心';result=await self.probe()
            self.assertEqual((result['transcript'],result['reply_text']),('',''))
            self.text='小糯';result=await self.probe()
            self.assertEqual(result['_wake_status'],'listening')
            self.assertEqual(result['_followup_seconds'],20)
            await result['_audio'].aclose()
            self.assertFalse(self.companion.calls)
            self.text='今天很开心';result=await self.probe()
            self.assertEqual(self.companion.calls,['今天很开心'])
            await result['_audio'].aclose()
            self.assertEqual((await self.probe())['_wake_status'],'ignored')
        asyncio.run(run())
    def test_explicit_wake_can_leave_quiet_but_background_cannot(self):
        async def run():
            self.store.set_quiet(True);self.text='你好'
            self.assertEqual((await self.probe())['_wake_status'],'ignored')
            self.assertTrue(self.store.controls()['quiet'])
            self.text='小糯';result=await self.probe();await result['_audio'].aclose()
            self.assertFalse(self.store.controls()['quiet'])
        asyncio.run(run())
    def test_concurrent_quiet_wins_over_recorded_wake(self):
        def transcribe(pcm):self.store.set_quiet(True);return recognition('小糯')
        self.asr.transcribe=transcribe
        async def run():
            self.assertEqual((await self.probe())['_wake_status'],'ignored')
            self.assertTrue(self.store.controls()['quiet']);self.assertFalse(self.companion.calls)
        asyncio.run(run())
    def test_cancellation_or_switch_off_after_asr_prevents_chat(self):
        async def run():
            for cancel in (True,False):
                self.cancelled.clear();self.store.patch_controls({'computer_wake_enabled':True})
                def transcribe(pcm):
                    if cancel:self.cancelled.set()
                    else:self.store.patch_controls({'computer_wake_enabled':False})
                    return recognition('小糯，今天很开心')
                self.asr.transcribe=transcribe
                with self.assertRaises(XiaoNuoError):await self.probe()
                self.assertFalse(self.companion.calls)
        asyncio.run(run())
    def test_quiet_command_is_silent_and_clears_followup(self):
        async def run():
            self.text='小糯，安静一下';result=await self.probe()
            self.assertEqual(result['_wake_status'],'quiet');self.assertNotIn('_audio',result)
            self.assertTrue(self.store.controls()['quiet'])
        asyncio.run(run())
