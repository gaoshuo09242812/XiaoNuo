"""Local ASR/TTS with explicitly selected local or DeepSeek text chat."""
import asyncio
from collections import OrderedDict
from io import BytesIO
import json
import threading
import time
from uuid import UUID,uuid5
import wave

from .errors import XiaoNuoError
from .speaker import device_speech_pcm
from .speech import split_spoken_reply
from .network_chat import require_deepseek_chat
from .computer_wake import ComputerWakeGate


class LocalNetworkSpeech:
    def __init__(self,root,asr,tts,companion,budget,store,*,work_lock=None,chat_provider='local',computer_wake=False,http_reply=None):
        if chat_provider not in {'local','deepseek'}:raise ValueError('Unsupported network chat provider')
        self.chat_provider=chat_provider
        self.http_reply=http_reply
        self.root,self.asr,self.tts=root,asr,tts
        self.companion,self.budget,self.store=companion,budget,store
        self.reminder_audio=getattr(getattr(companion,'reminders',None),'audio',None)
        self._work=work_lock if work_lock is not None else threading.Lock()
        self._conversations=OrderedDict()
        self._wake_allowed=computer_wake
        self.wake_gate=ComputerWakeGate()
        self._reply_cache=OrderedDict()
        self._reply_cache_bytes=0

    def remember_reply(self,reply_id,pcm,index):
        if not reply_id or not isinstance(pcm,bytes) or not pcm:
            return
        if reply_id in self._reply_cache:
            self._reply_cache_bytes-=len(self._reply_cache[reply_id][0])
        self._reply_cache[reply_id]=(pcm,index)
        self._reply_cache_bytes+=len(pcm)
        while len(self._reply_cache)>8 or self._reply_cache_bytes>2*1024*1024:
            _,old=self._reply_cache.popitem(last=False)
            self._reply_cache_bytes-=len(old[0])

    def resume_reply(self,reply_id):
        item=self._reply_cache.get(reply_id)
        return item[0] if item else None

    @property
    def wake_enabled(self):
        return self._wake_allowed and self.store.controls()['computer_wake_enabled']

    def reset_wake(self,device=None):
        self.wake_gate.reset(device)

    async def clear_context(self, timeout=5):
        """Call with device admission suspended and all turns cancelled.

        to_thread cancellation cannot terminate native inference. Wait for its
        real worker before clearing; on timeout the caller must not claim that
        private data has already been deleted.
        """
        def drain_and_clear():
            if not self._work.acquire(timeout=timeout):
                raise XiaoNuoError('NETWORK_WORKER_STILL_STOPPING', 409)
            try:
                self._conversations.clear()
                self.reset_wake()
            finally:
                self._work.release()
        await asyncio.to_thread(drain_and_clear)

    def guard(self,cancelled,*,output=False,allow_quiet=False,local_only=False):
        if cancelled.is_set():raise XiaoNuoError('NETWORK_REPLY_CANCELLED',409)
        controls=self.store.controls()
        settings=getattr(getattr(self.companion,'router',None),'settings',None)
        if not local_only and self.chat_provider=='deepseek':
            require_deepseek_chat(self.companion.router)
        elif not local_only and settings is not None and settings.remote_enabled:
            raise XiaoNuoError('NETWORK_LOCAL_ONLY_REQUIRED',409)
        if (controls['quiet'] and not allow_quiet) or (output and not controls['speech_enabled']):
            raise XiaoNuoError('SPEECH_DISABLED_BY_OWNER',409)
        return controls

    def run(self,operation,cancelled,*,allow_quiet=False,local_only=False):
        # Ownership stays with the actual worker if asyncio cancels to_thread.
        if not self._work.acquire(blocking=False):raise XiaoNuoError('VOICE_TURN_BUSY',409)
        try:
            self.guard(cancelled,allow_quiet=allow_quiet,local_only=local_only)
            return operation()
        finally:self._work.release()

    def recognize_and_reply(self,pcm,request,device,cancelled):
        base=UUID(request)
        recognition=self.asr.transcribe(pcm)
        metadata=self.asr.status()
        self.budget.record_local(str(uuid5(base,'network-asr')),metadata['provider'],metadata['model'],'ASR',
                                 audio_seconds=recognition['duration_seconds'])
        self.guard(cancelled)
        current=self._conversations.get(device)
        conversation=current[0] if current and time.monotonic()-current[1]<1800 else None
        answer=self.companion.chat(recognition['text'],str(uuid5(base,'network-llm')),conversation)
        self.guard(cancelled)
        self._conversations[device]=(answer.get('conversation_id'),time.monotonic())
        self._conversations.move_to_end(device)
        while len(self._conversations)>8:self._conversations.popitem(last=False)
        return recognition,answer

    async def __call__(self,pcm,request,device,cancelled):
        recognition,answer=await asyncio.to_thread(self.run,
            lambda:self.recognize_and_reply(pcm,request,device,cancelled),cancelled)
        self.guard(cancelled)
        from .expressions import safe_expression
        result={'transcript':recognition['text'],'reply_text':answer['reply_text'],
                '_expression':safe_expression(answer.get('emotion_display')),
                '_voice_probe':self.probe_flags(recognition['text'],answer['reply_text'],answer)}
        controls=self.store.controls()
        if answer.get('speech_style',{}).get('enabled') and controls['speech_enabled'] and controls['volume']>0:
            result['_audio_metrics']={}
            result['_audio']=self.audio(answer['reply_text'],request,cancelled,result['_audio_metrics'])
        return result

    @staticmethod
    def probe_flags(request,reply,answer):
        # Fixed diagnostic cue only; never retain arbitrary recognized/reply text.
        import re
        fields={'story_request_seen':'故事' in request,
                'thirty_seconds_seen':bool(re.search(r'(?:30|三十)\s*秒',request)),
                'ending_request_seen':'讲完啦' in request,
                'ending_reply_seen':'讲完啦' in reply}
        for key in ('model_characters','styled_characters'):
            value=answer.get('diagnostics',{}).get(key)
            if type(value) is int and 0<=value<=100000:fields[key]=value
        return fields

    @staticmethod
    def _pcm_metrics(pcm):
        if not isinstance(pcm,(bytes,bytearray)) or len(pcm)<2:return {}
        view=memoryview(pcm)[:len(pcm)//2*2].cast('h')
        peak=0;total=0
        for value in view:
            magnitude=abs(int(value));peak=max(peak,magnitude);total+=int(value)*int(value)
        return {'pcm_samples':len(view),'pcm_peak':peak,
                'pcm_rms':round((total/len(view))**.5,2) if view else 0}

    def _write_wake_diagnostic(self,recognition,decision,pcm,asr_error=None):
        """Sanitized wake metrics only: never transcript, words or device id."""
        text=recognition.get('text','') if isinstance(recognition,dict) else ''
        words=recognition.get('words',[]) if isinstance(recognition,dict) else []
        confidence=recognition.get('confidence') if isinstance(recognition,dict) else None
        if not isinstance(text,str) or not isinstance(words,list):text='';words=[]
        names=tuple(getattr(self.wake_gate,'NAMES',()))
        word_confidences=[]
        for word in words[:8]:
            score=word.get('confidence') if isinstance(word,dict) else None
            word_confidences.append(round(float(score),3) if isinstance(score,(int,float)) and score==score else None)
        record={'ts':time.strftime('%Y-%m-%dT%H:%M:%S%z'),**self._pcm_metrics(pcm),
                'text_length':len(text),
                'confidence':round(float(confidence),3) if isinstance(confidence,(int,float)) and confidence==confidence else None,
                'starts_with_known_name':any(text.startswith(name) for name in names),
                'contains_known_name':any(name in text for name in names),
                'word_count':len(words),'word_confidences':word_confidences,
                'decision_status':decision.status if decision is not None else 'asr_error',
                'followup_seconds':decision.followup_seconds if decision is not None else 0,
                'asr_error':asr_error}
        try:
            with open(self.root/'data/pages-ingress/wake-diagnostic.jsonl','a',encoding='utf-8') as handle:
                handle.write(json.dumps(record,ensure_ascii=False)+'\n')
        except OSError:
            pass

    async def wake_probe(self,pcm,request,device,cancelled):
        if not self.wake_enabled:raise XiaoNuoError('COMPUTER_WAKE_DISABLED',409)
        generation=self.wake_gate.generation()
        def process():
            quiet,revision=self.store.quiet_revision()
            try:recognition=self.asr.transcribe(pcm)
            except XiaoNuoError as error:
                if error.code in {'ASR_EMPTY_AUDIO','ASR_NO_SPEECH_RECOGNIZED'}:
                    self._write_wake_diagnostic(None,None,pcm,error.code)
                    return {'transcript':'','reply_text':'','_wake_status':'ignored','_followup_seconds':0}
                raise
            metadata=self.asr.status()
            self.budget.record_local(str(uuid5(UUID(request),'network-wake-asr')),metadata['provider'],metadata['model'],
                                     'ASR',audio_seconds=recognition['duration_seconds'])
            self.guard(cancelled,allow_quiet=True)
            if not self.wake_enabled:raise XiaoNuoError('COMPUTER_WAKE_DISABLED',409)
            decision=self.wake_gate.decide(recognition,device,quiet=quiet,generation=generation)
            self._write_wake_diagnostic(recognition,decision,pcm)
            if decision.status in {'ignored','cancelled'}:
                return {'transcript':'','reply_text':'','_wake_status':decision.status,'_followup_seconds':0}
            self.guard(cancelled,allow_quiet=True)
            if not self.store.wake_if_unchanged(revision):
                self.reset_wake(device)
                return {'transcript':'','reply_text':'','_wake_status':'ignored','_followup_seconds':0}
            self.guard(cancelled)
            if decision.status=='listening':
                answer=self.companion.local('我在呢，你说。',request,None)
            else:
                current=self._conversations.get(device)
                conversation=current[0] if current and time.monotonic()-current[1]<1800 else None
                answer=self.companion.chat(decision.text,str(uuid5(UUID(request),'network-wake-llm')),conversation)
                self.guard(cancelled,allow_quiet=True)
                if self.store.controls()['quiet']:
                    self.reset_wake(device)
                    return {'transcript':'','reply_text':'','_wake_status':'quiet','_followup_seconds':0}
                self._conversations[device]=(answer.get('conversation_id'),time.monotonic())
                self._conversations.move_to_end(device)
                while len(self._conversations)>8:self._conversations.popitem(last=False)
            self.guard(cancelled)
            from .expressions import safe_expression
            return {'transcript':recognition['text'],'reply_text':answer['reply_text'],
                    '_expression':safe_expression(answer.get('emotion_display')),
                    '_wake_status':decision.status,'_followup_seconds':decision.followup_seconds,'_answer':answer,
                    '_voice_probe':self.probe_flags(recognition['text'],answer['reply_text'],answer)}
        result=await asyncio.to_thread(self.run,process,cancelled,allow_quiet=True)
        self.guard(cancelled,allow_quiet=result['_wake_status'] in {'ignored','cancelled','quiet'})
        answer=result.pop('_answer',{})
        controls=self.store.controls()
        if answer.get('speech_style',{}).get('enabled') and controls['speech_enabled'] and controls['volume']>0:
            result['_audio_metrics']={}
            result['_audio']=self.audio(answer['reply_text'],request,cancelled,result['_audio_metrics'])
        return result

    def synthesize(self,text,request,path,cancelled,metrics=None,*,local_only=False):
        controls=self.guard(cancelled,output=True,local_only=local_only)
        started=time.perf_counter()
        try:
            blob=self.tts.synthesize(text,volume=1.0,speed=controls['speech_speed'],voice=controls['voice_profile'])
        finally:
            if metrics is not None:
                metrics['tts_ms']=metrics.get('tts_ms',0)+round((time.perf_counter()-started)*1000)
                metrics['tts_calls']=metrics.get('tts_calls',0)+1
        with wave.open(BytesIO(blob),'rb') as source:
            duration=source.getnframes()/source.getframerate()
        status=self.tts.status()
        self.budget.record_local(str(uuid5(UUID(request),'network-tts:'+path)),status['provider'],status.get('model','zh-CN'),
                                 'TTS',audio_seconds=duration,characters=len(text))
        self.guard(cancelled,output=True,local_only=local_only)
        if duration>40:return None
        started=time.perf_counter()
        try:return device_speech_pcm(blob,self.root,volume=self.store.controls()['volume'],max_seconds=40)
        finally:
            if metrics is not None:metrics['pcm_conversion_ms']=metrics.get('pcm_conversion_ms',0)+round((time.perf_counter()-started)*1000)

    async def audio(self,text,request,cancelled,metrics=None,*,local_only=False):
        if len(text)>1000:raise XiaoNuoError('SPEAKER_REPLY_TOO_LONG',422)
        async def fragment(segment,path):
            try:
                pcm=await asyncio.to_thread(self.run,lambda:self.synthesize(segment,request,path,cancelled,metrics,local_only=local_only),cancelled,local_only=local_only)
            except XiaoNuoError as error:
                if error.code!='TTS_AUDIO_LENGTH_INVALID':raise
                pcm=None
            self.guard(cancelled,output=True,local_only=local_only)
            if pcm is not None:
                for offset in range(0,len(pcm),8*32000):yield pcm[offset:offset+8*32000]
            else:
                if len(segment)<=1:raise XiaoNuoError('SPEAKER_AUDIO_TOO_LONG',422)
                midpoint=len(segment)//2
                async for piece in fragment(segment[:midpoint],path+'a'):yield piece
                async for piece in fragment(segment[midpoint:],path+'b'):yield piece
        for index,segment in enumerate(split_spoken_reply(text,128)):
            async for pcm in fragment(segment,str(index)):yield pcm

    async def prepare_reminder(self,text,request,cancelled):
        """Local TTS only; no ASR, chat model, memory or follow-up context."""
        if not isinstance(text,str) or not 1<=len(text)<=500:
            raise XiaoNuoError('INVALID_REMINDER_TEXT',422)
        buffer=bytearray()
        async for piece in self.audio('提醒你：'+text,request,cancelled,local_only=True):
            if not isinstance(piece,bytes) or len(piece)%2 or len(buffer)+len(piece)>60*32000:
                raise XiaoNuoError('REMINDER_AUDIO_TOO_LONG',422)
            buffer.extend(piece)
        self.guard(cancelled,output=True,local_only=True)
        if not buffer:raise XiaoNuoError('REMINDER_AUDIO_EMPTY',422)
        return bytes(buffer)
