"""Bounded local-ASR wake gate; no model, network, storage or audio calls."""
from collections import OrderedDict
from dataclasses import dataclass
import math
import threading
import time
import unicodedata
import asyncio
import inspect

async def reset_wake_context(handler,device=None):
    if not callable(getattr(type(handler),'reset_wake',None)):return
    result=handler.reset_wake(device)
    if inspect.isawaitable(result):await asyncio.wait_for(result,1)

@dataclass(frozen=True)
class WakeDecision:
    status: str
    text: str = ''
    followup_seconds: int = 0

class ComputerWakeGate:
    NAMES=('小糯','小诺')
    SEPARATORS=' ，,：:。！？!?；;\t\r\n'
    CANCEL={'取消','算了','没事了','不用了','停止'}
    def __init__(self,*,clock=time.monotonic):
        self.clock=clock;self._lock=threading.Lock();self._generation=0;self._followup=OrderedDict()
    def generation(self):
        with self._lock:return self._generation
    def reset(self,device=None):
        with self._lock:
            self._generation+=1
            if device is None:self._followup.clear()
            else:self._followup.pop(device,None)
    def decide(self,recognition,device,*,quiet=False,generation=None):
        with self._lock:
            if generation is not None and generation!=self._generation:return WakeDecision('ignored')
            now=self.clock();self._followup=OrderedDict((k,v) for k,v in self._followup.items() if now<v)
            text=recognition.get('text','');confidence=recognition.get('confidence')
            if (not isinstance(text,str) or len(text)>500 or type(confidence) not in {int,float}
                    or not math.isfinite(confidence) or confidence<.7):return WakeDecision('ignored')
            text=unicodedata.normalize('NFKC',text).strip();addressed=None
            for greeting in ('','你好','嗨','嘿'):
                for name in self.NAMES:
                    prefix=greeting+name
                    if not text.startswith(prefix):continue
                    rest=text[len(prefix):];boundary=False;spoken='';scores=[]
                    for word in recognition.get('words',[]) or []:
                        if not isinstance(word,dict):break
                        spoken+=str(word.get('word','')).replace(' ','');score=word.get('confidence')
                        if type(score) not in {float,int} or not math.isfinite(score):break
                        scores.append(score)
                        if spoken==prefix:boundary=min(scores)>=.75;break
                        if len(spoken)>=len(prefix):break
                    if (not rest or rest[0] in self.SEPARATORS or boundary) and confidence>=.75:
                        addressed=rest.strip(self.SEPARATORS);break
                if addressed is not None:break
            if addressed is not None:
                self._followup.pop(device,None)
                if addressed in self.CANCEL:return WakeDecision('cancelled')
                if addressed:return WakeDecision('accepted',addressed)
                self._followup[device]=now+20;self._followup.move_to_end(device)
                while len(self._followup)>8:self._followup.popitem(last=False)
                return WakeDecision('listening',followup_seconds=20)
            if quiet:self._followup.pop(device,None);return WakeDecision('ignored')
            if device in self._followup:
                self._followup.pop(device,None)
                if text.strip(self.SEPARATORS) in self.CANCEL:return WakeDecision('cancelled')
                if text:return WakeDecision('accepted',text)
            return WakeDecision('ignored')
