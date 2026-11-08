"""Windowed reply transfer; acknowledgements never prove physical audibility."""
import asyncio
import hashlib
import time
from collections import deque
from uuid import uuid4

from .errors import XiaoNuoError
from .network_audio import encode_audio_frame
from .adpcm import ENCODING, encode as encode_adpcm


class ReplyChannel:
    def __init__(self,send,send_bytes,guard,cancelled,*,compressed=False,max_seconds=8,allow_pipeline=False):
        if max_seconds not in {8,20,60} or (max_seconds in {20,60} and not compressed):
            raise ValueError('Unsupported reply duration')
        if type(allow_pipeline) is not bool or (allow_pipeline and (not compressed or max_seconds!=60)):
            raise ValueError('Pipeline requires complete buffered ADPCM replies')
        self.send,self.send_bytes,self.guard,self.cancelled=send,send_bytes,guard,cancelled
        self.compressed=compressed
        self.max_seconds=max_seconds
        self.active_id=None
        self.last_reply_id=None
        self.expected=None
        self.waiter=None
        self.window_frames=4
        self.last_timing={}
        self.allow_pipeline=allow_pipeline
        self.pipeline_windows=1
        self.pending_windows=deque()

    def prepare(self,expected):
        self.expected=expected
        self.waiter=asyncio.get_running_loop().create_future()

    def acknowledge(self,message):
        if self.pending_windows:
            expected,future=self.pending_windows[0]
            if (future.done() or set(message)!=set(expected)
                    or any(type(message[k]) is not type(v) or message[k]!=v for k,v in expected.items())):
                raise XiaoNuoError('NETWORK_REPLY_INVALID_ACK',422)
            self.pending_windows.popleft();future.set_result(True);return
        pipeline=None
        if self.expected and self.expected.get('type')=='reply_ready' and 'pipeline_windows' in message:
            pipeline=message['pipeline_windows'];size=message.get('window_frames')
            if (not self.allow_pipeline or type(pipeline) is not int or pipeline not in {1,2}
                    or type(size) is not int or (pipeline==2 and size not in {4,8,16})):
                raise XiaoNuoError('NETWORK_REPLY_INVALID_ACK',422)
            message={k:v for k,v in message.items() if k!='pipeline_windows'}
        if (self.expected is not None and self.expected.get('type')=='reply_ready'
                and set(message)==set(self.expected)|{'window_frames'}):
            size=message['window_frames']
            if type(size) is not int or size not in {4,8,16,32}:
                raise XiaoNuoError('NETWORK_REPLY_INVALID_ACK',422)
            message={key:value for key,value in message.items() if key!='window_frames'}
        else:
            size=None
        if (self.waiter is None or self.waiter.done() or self.expected is None
                or set(message)!=set(self.expected)
                or any(type(message[key]) is not type(value) or message[key]!=value for key,value in self.expected.items())):
            raise XiaoNuoError('NETWORK_REPLY_INVALID_ACK',422)
        if size is not None:self.window_frames=size
        if pipeline is not None:self.pipeline_windows=pipeline
        self.waiter.set_result(True)

    def clear_pending_windows(self):
        while self.pending_windows:
            _,future=self.pending_windows.popleft()
            if not future.done():future.cancel()

    async def pipelined_transfer(self,wire,reply_id):
        """Two credits only; each exact ordered ACK releases one credit.

        Enabled only after both hello capability and reply_ready negotiation.
        The future receiver must reserve two windows plus the end marker and
        stop reading another frame until each pending ACK has been sent.
        """
        inflight=deque();sequence=0;window_bytes=self.window_frames*1000
        async def wait_credit(future):
            started=time.perf_counter()
            try:
                try:await asyncio.wait_for(future,30)
                except TimeoutError:raise XiaoNuoError('NETWORK_REPLY_ACK_TIMEOUT',408) from None
                self.check()
            finally:self.last_timing['window_ack_ms']=self.last_timing.get('window_ack_ms',0)+round((time.perf_counter()-started)*1000)
        try:
            for window in range(0,len(wire),window_bytes):
                self.check();end=min(window+window_bytes,len(wire));frames=(end-window+999)//1000
                future=asyncio.get_running_loop().create_future()
                expected={'type':'reply_ack','reply_id':reply_id,'next_sequence':sequence+frames,'received_bytes':end}
                if len(self.pending_windows)>=2:raise RuntimeError('Pipeline credit overflow')
                self.pending_windows.append((expected,future));inflight.append(future)
                for offset in range(window,end,1000):
                    self.check();frame=encode_audio_frame(reply_id,sequence,wire[offset:min(offset+1000,end)],compressed=True)
                    sending=time.perf_counter()
                    try:await asyncio.wait_for(self.send_bytes(frame),5)
                    finally:self.last_timing['send_ms']=self.last_timing.get('send_ms',0)+round((time.perf_counter()-sending)*1000)
                    sequence+=1
                if len(inflight)==2:await wait_credit(inflight.popleft())
            while inflight:await wait_credit(inflight.popleft())
        finally:self.clear_pending_windows()

    async def wait(self,seconds=15):
        try:await asyncio.wait_for(self.waiter,seconds)
        except TimeoutError:raise XiaoNuoError('NETWORK_REPLY_ACK_TIMEOUT',408) from None
        finally:self.expected=None;self.waiter=None
        self.check()

    def check(self):
        if self.cancelled.is_set():raise XiaoNuoError('NETWORK_REPLY_CANCELLED',409)
        started=time.perf_counter()
        try:self.guard()
        finally:self.last_timing['guard_ms']=self.last_timing.get('guard_ms',0)+round((time.perf_counter()-started)*1000)

    async def segment(self,pcm,index):
        if not isinstance(pcm,bytes) or not pcm or len(pcm)%2 or len(pcm)>self.max_seconds*32000:
            raise XiaoNuoError('NETWORK_REPLY_AUDIO_INVALID',422)
        self.last_timing={}
        self.check()
        self.active_id=str(uuid4())
        reply_id=self.active_id
        self.last_reply_id=reply_id
        samples=len(pcm)//2
        phase_started=time.perf_counter()
        if self.compressed:
            wire, _ = encode_adpcm(pcm,0,0,max_seconds=self.max_seconds)
            format_name, predictor, step_index = ENCODING, 0, 0
        else:
            wire, format_name, predictor, step_index = pcm, 's16le', None, None
        self.last_timing.update(encode_ms=round((time.perf_counter()-phase_started)*1000),wire_bytes=len(wire))
        try:
            async with asyncio.timeout(480 if self.max_seconds==60 else 90 if self.max_seconds==20 else 45):
                self.window_frames=4
                self.pipeline_windows=1
                self.prepare({'type':'reply_ready','reply_id':reply_id,'sample_rate':16000,'gain_divisor':2,'max_seconds':self.max_seconds})
                start={'type':'reply_start','reply_id':reply_id,'segment':index,'bytes':len(wire),
                       'sample_rate':16000,'channels':1,'format':format_name,'gain_divisor':2,
                       'window_frames':4,'max_window_frames':32}
                if self.compressed:
                    start.update({'predictor':predictor,'step_index':step_index,'samples':samples})
                if self.allow_pipeline:start['max_pipeline_windows']=2
                phase_started=time.perf_counter()
                await self.send(start)
                await self.wait(30 if self.max_seconds==60 else 15)
                self.last_timing['ready_ack_ms']=round((time.perf_counter()-phase_started)*1000)
                if self.allow_pipeline:self.last_timing['pipeline_windows']=self.pipeline_windows
                phase_started=time.perf_counter()
                sequence=0
                window_bytes=self.window_frames*1000
                windows=range(0,len(wire),window_bytes) if self.pipeline_windows==1 else ()
                if self.pipeline_windows==2:await self.pipelined_transfer(wire,reply_id)
                for window in windows:
                    self.check()
                    end=min(window+window_bytes,len(wire))
                    frames=(end-window+999)//1000
                    self.prepare({'type':'reply_ack','reply_id':reply_id,
                                  'next_sequence':sequence+frames,'received_bytes':end})
                    for offset in range(window,end,1000):
                        self.check()
                        frame=encode_audio_frame(reply_id,sequence,wire[offset:min(offset+1000,end)],compressed=self.compressed)
                        sending=time.perf_counter()
                        try:await asyncio.wait_for(self.send_bytes(frame),5)
                        finally:self.last_timing['send_ms']=self.last_timing.get('send_ms',0)+round((time.perf_counter()-sending)*1000)
                        sequence+=1
                    waiting=time.perf_counter()
                    try:await self.wait(30 if self.max_seconds==60 else 15)
                    finally:self.last_timing['window_ack_ms']=self.last_timing.get('window_ack_ms',0)+round((time.perf_counter()-waiting)*1000)
                self.last_timing['transfer_ms']=round((time.perf_counter()-phase_started)*1000)
                self.prepare({'type':'reply_played','reply_id':reply_id,'completed':True,
                              'samples':samples,'gain_divisor':2})
                phase_started=time.perf_counter()
                await self.send({'type':'reply_end','reply_id':reply_id,'sha256':hashlib.sha256(wire).hexdigest()})
                await self.wait(samples/16000+10 if self.max_seconds==60 else samples/16000+5 if self.max_seconds==20 else 12)
                self.last_timing['playback_ack_ms']=round((time.perf_counter()-phase_started)*1000)
            self.active_id=None
            return {'segment':index,'duration_seconds':samples/16000,'device_completed':True,'audible_verified':False}
        except TimeoutError:
            raise XiaoNuoError('NETWORK_REPLY_SEGMENT_TIMEOUT',408) from None

    async def abort(self):
        self.clear_pending_windows()
        if self.waiter and not self.waiter.done():self.waiter.cancel()
        self.expected=None
        if self.active_id:
            identifier=self.active_id;self.active_id=None
            try:await asyncio.wait_for(self.send({'type':'reply_abort','reply_id':identifier}),1)
            except Exception:pass


