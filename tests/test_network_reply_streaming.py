import asyncio
import hashlib
import unittest
from app.adpcm import decode
from app.errors import XiaoNuoError
from app.network_reply import ReplyChannel
from test_network_reply import MemorySpeaker

def decode_reply(wire):
    pcm=bytearray();state=(0,0)
    for offset in range(0,len(wire),48000):
        piece,state=decode(wire[offset:offset+48000],*state);pcm.extend(piece)
    return bytes(pcm)

class StreamingSpeaker(MemorySpeaker):
    def __init__(self):
        super().__init__()
        self.channel=ReplyChannel(self.send,self.binary,lambda:None,self.cancelled,compressed=True,max_seconds=20)
    async def send(self,message):
        self.messages.append(message)
        if message['type']=='reply_start':
            self.current=message;self.expected_bytes=message['bytes'];self.pcm.clear();self.next_sequence=0
            self.window_frames=16
            self.channel.acknowledge({'type':'reply_ready','reply_id':message['reply_id'],
                'sample_rate':16000,'gain_divisor':2,'max_seconds':20,'window_frames':16})
        elif message['type']=='reply_end':
            assert hashlib.sha256(self.pcm).hexdigest()==message['sha256']
            self.channel.acknowledge({'type':'reply_played','reply_id':message['reply_id'],
                'completed':True,'samples':self.current['samples'],'gain_divisor':2})

class StreamingReplyTests(unittest.TestCase):
    def test_one_continuous_segment_and_exact_odd_tail_samples(self):
        async def run():
            for samples in (1,501,11*16000,20*16000):
                with self.subTest(samples=samples):
                    device=StreamingSpeaker()
                    await device.channel.segment(b'\x12\x34'*samples,0)
                    self.assertEqual(len(device.pcm),(samples+1)//2)
                    self.assertEqual(len(decode_reply(device.pcm)[:samples*2]),samples*2)
                    self.assertEqual([m['type'] for m in device.messages],['reply_start','reply_end'])
                    self.assertEqual(device.channel.window_frames,16)
                    if samples==20*16000:self.assertEqual(device.acknowledged,list(range(16,161,16)))
        asyncio.run(run())
    def test_continuous_stream_cannot_exceed_20_seconds_or_use_pcm_mode(self):
        async def run():
            device=StreamingSpeaker()
            with self.assertRaises(XiaoNuoError):await device.channel.segment(b'\0'*(20*32000+2),0)
            self.assertFalse(device.messages)
            with self.assertRaises(ValueError):ReplyChannel(None,None,None,None,max_seconds=20)
        asyncio.run(run())
