import asyncio
import hashlib
import threading
import unittest
from unittest.mock import patch

from app.errors import XiaoNuoError
from app.network_audio import HEADER,MAGIC
from app.network_reply import ReplyChannel


class MemorySpeaker:
    def __init__(self):
        self.messages=[];self.frames=[];self.pcm=bytearray();self.expected_bytes=0
        self.next_sequence=0;self.cancelled=threading.Event()
        self.start_ack=True;self.end_ack=True;self.cancel_after=None
        self.channel=ReplyChannel(self.send,self.binary,lambda:None,self.cancelled)
        self.window_frames=4;self.negotiate=None;self.acknowledged=[]

    async def send(self,message):
        self.messages.append(message)
        if message['type']=='reply_start':
            self.expected_bytes=message['bytes'];self.pcm.clear();self.next_sequence=0
            if self.start_ack:
                ready={'type':'reply_ready','reply_id':message['reply_id'],
                       'sample_rate':16000,'gain_divisor':2,'max_seconds':8}
                self.window_frames=self.negotiate or 4
                if self.negotiate is not None:ready['window_frames']=self.negotiate
                self.channel.acknowledge(ready)
        elif message['type']=='reply_end':
            assert hashlib.sha256(self.pcm).hexdigest()==message['sha256']
            if self.end_ack:self.channel.acknowledge({'type':'reply_played','reply_id':message['reply_id'],
                'completed':True,'samples':len(self.pcm)//2,'gain_divisor':2})

    async def binary(self,frame):
        assert len(frame)<=1024
        magic,stream,seq=HEADER.unpack_from(frame)
        assert magic==MAGIC and str(__import__('uuid').UUID(bytes=stream))==self.channel.active_id
        assert seq==self.next_sequence
        self.frames.append(frame);self.next_sequence+=1;self.pcm.extend(frame[HEADER.size:])
        if self.next_sequence%self.window_frames==0 or len(self.pcm)==self.expected_bytes:
            self.acknowledged.append(self.next_sequence)
            self.channel.acknowledge({'type':'reply_ack','reply_id':self.channel.active_id,
                'next_sequence':self.next_sequence,'received_bytes':len(self.pcm)})
        if self.cancel_after is not None and self.next_sequence>=self.cancel_after:self.cancelled.set()


class ReplyTests(unittest.TestCase):
    def test_timing_separates_ready_transfer_and_playback_ack_without_identifiers(self):
        clock=[0.0]
        class TimedSpeaker(MemorySpeaker):
            async def send(self,message):
                clock[0]+=3 if message['type']=='reply_start' else 7
                await super().send(message)
            async def binary(self,frame):
                clock[0]+=2
                await super().binary(frame)
        async def run():
            device=TimedSpeaker()
            with patch('app.network_reply.time.perf_counter',side_effect=lambda:clock[0]):
                await device.channel.segment(bytes(4000),0)
            self.assertEqual(device.channel.last_timing,dict(encode_ms=0,wire_bytes=4000,
                ready_ack_ms=3000,transfer_ms=8000,playback_ack_ms=7000,
                guard_ms=0,send_ms=8000,window_ack_ms=0))
            self.assertNotIn(device.channel.last_reply_id,str(device.channel.last_timing))
        asyncio.run(run())

    def test_slow_receiver_backpressure_never_exceeds_negotiated_window(self):
        async def run():
            messages=[];frames=[];full=asyncio.Event();cancelled=threading.Event()
            async def send(message):
                messages.append(message)
                if message['type']=='reply_start':
                    channel.acknowledge({'type':'reply_ready','reply_id':message['reply_id'],
                        'sample_rate':16000,'gain_divisor':2,'max_seconds':8,'window_frames':16})
            async def binary(frame):
                frames.append(frame)
                if len(frames)==16:full.set()
            channel=ReplyChannel(send,binary,lambda:None,cancelled)
            task=asyncio.create_task(channel.segment(b'\0'*64000,0))
            try:
                await asyncio.wait_for(full.wait(),1)
                await asyncio.sleep(.02)
                self.assertEqual(len(frames),16)
                self.assertFalse(task.done())
                channel.acknowledge({'type':'reply_ack','reply_id':channel.active_id,
                    'next_sequence':16,'received_bytes':16000})
                for _ in range(100):
                    if len(frames)==32:break
                    await asyncio.sleep(.001)
                self.assertEqual(len(frames),32)
            finally:
                cancelled.set();task.cancel()
                await asyncio.gather(task,return_exceptions=True)
                await channel.abort()
        asyncio.run(run())

    def test_optional_window_negotiation_keeps_old_peers_and_bounds_new_peers(self):
        async def run():
            for size in (None,4,8,16,32):
                device=MemorySpeaker();device.negotiate=size
                await device.channel.segment(b'\0'*35002,0)
                negotiated=size or 4
                self.assertEqual(device.channel.window_frames,negotiated)
                self.assertEqual(device.acknowledged,list(range(negotiated,36,negotiated))+[36])
                self.assertEqual(device.messages[0]['max_window_frames'],32)
                device.negotiate=None;device.acknowledged.clear()
                await device.channel.segment(b'\0'*5002,1)
                self.assertEqual(device.channel.window_frames,4)
                self.assertEqual(device.acknowledged,[4,6])
        asyncio.run(run())

    def test_window_must_be_supported_integer_and_only_in_ready_ack(self):
        async def run():
            expected={'type':'reply_ready','reply_id':'fixture','sample_rate':16000,'gain_divisor':2,'max_seconds':8}
            for size in (True,0,1,3,17,33,'16',16.0):
                device=MemorySpeaker();device.channel.prepare(expected)
                with self.assertRaises(XiaoNuoError):device.channel.acknowledge(expected|{'window_frames':size})
                device.channel.waiter.cancel()
            device=MemorySpeaker();device.channel.prepare(expected)
            device.channel.acknowledge(expected|{'window_frames':32})
            self.assertEqual(device.channel.window_frames,32)
            device=MemorySpeaker()
            ack={'type':'reply_ack','reply_id':'fixture','next_sequence':1,'received_bytes':2}
            device.channel.prepare(ack)
            with self.assertRaises(XiaoNuoError):device.channel.acknowledge(ack|{'window_frames':16})
            device.channel.waiter.cancel()
        asyncio.run(run())
    def test_eight_seconds_transfer_is_bounded_and_requires_played_ack(self):
        async def run():
            device=MemorySpeaker();pcm=b'\x01\x02'*128000
            result=await device.channel.segment(pcm,0)
            self.assertEqual(bytes(device.pcm),pcm)
            self.assertEqual(len(device.frames),256)
            self.assertTrue(result['device_completed']);self.assertFalse(result['audible_verified'])
        asyncio.run(run())

    def test_partial_last_window_and_multiple_segments(self):
        async def run():
            device=MemorySpeaker()
            for index,size in enumerate((2,5002,64000)):
                pcm=b'\0'*size
                await device.channel.segment(pcm,index)
                self.assertEqual(bytes(device.pcm),pcm)
        asyncio.run(run())

    def test_missing_ready_or_played_ack_does_not_claim_success(self):
        async def run():
            for phase in ('start_ack','end_ack'):
                device=MemorySpeaker();setattr(device,phase,False)
                original=device.channel.wait
                async def fast(seconds=5):await original(.01)
                device.channel.wait=fast
                with self.assertRaisesRegex(XiaoNuoError,'ACK_TIMEOUT'):
                    await device.channel.segment(b'\0'*2000,0)
                await device.channel.abort()
                self.assertEqual(device.messages[-1]['type'],'reply_abort')
        asyncio.run(run())

    def test_invalid_audio_is_rejected_before_transmission(self):
        async def run():
            for pcm in (b'',b'1',b'\0'*(8*32000+2)):
                device=MemorySpeaker()
                with self.assertRaises(XiaoNuoError):await device.channel.segment(pcm,0)
                self.assertEqual(device.messages,[])
        asyncio.run(run())

    def test_cancellation_stops_all_following_frames(self):
        async def run():
            device=MemorySpeaker();device.cancel_after=4
            with self.assertRaisesRegex(XiaoNuoError,'CANCELLED'):
                await device.channel.segment(b'\0'*64000,0)
            self.assertEqual(len(device.frames),4)
            await device.channel.abort()
        asyncio.run(run())

    def test_ack_without_pending_window_and_wrong_counts_rejected(self):
        async def run():
            device=MemorySpeaker()
            with self.assertRaises(XiaoNuoError):device.channel.acknowledge({'type':'reply_ack'})
            expected={'type':'reply_ack','reply_id':'test','next_sequence':1,'received_bytes':2}
            for change in ({'next_sequence':True},{'received_bytes':4},{'reply_id':'old'},{'extra':'field'}):
                device.channel.prepare(expected)
                with self.assertRaises(XiaoNuoError):device.channel.acknowledge(expected|change)
                device.channel.waiter.cancel()
        asyncio.run(run())

