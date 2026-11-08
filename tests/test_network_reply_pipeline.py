"""Credit/ordering tests with synthetic audio; no hardware or wall-clock RTT assumptions."""
import asyncio
import hashlib
import threading
import unittest
from uuid import UUID

from app.errors import XiaoNuoError
from app.network_audio import HEADER,MAGIC
from app.network_reply import ReplyChannel


class GatedSpeaker:
    def __init__(self,*,allow=True,negotiated=2,window_frames=16):
        self.cancelled=threading.Event();self.windows=asyncio.Queue();self.messages=[]
        self.wire=bytearray();self.sequence=0;self.negotiated=negotiated;self.start=None
        self.window_frames=window_frames
        self.channel=ReplyChannel(self.send,self.binary,lambda:None,self.cancelled,
                                  compressed=True,max_seconds=60,allow_pipeline=allow)

    async def send(self,message):
        self.messages.append(message)
        if message['type']=='reply_start':
            self.start=message
            ack={'type':'reply_ready','reply_id':message['reply_id'],'sample_rate':16000,
                 'gain_divisor':2,'max_seconds':60,'window_frames':self.window_frames}
            if self.negotiated is not None:ack['pipeline_windows']=self.negotiated
            self.channel.acknowledge(ack)
        elif message['type']=='reply_end':
            assert len(self.wire)==self.start['bytes']
            assert hashlib.sha256(self.wire).hexdigest()==message['sha256']
            self.channel.acknowledge({'type':'reply_played','reply_id':message['reply_id'],
                'completed':True,'samples':self.start['samples'],'gain_divisor':2})

    async def binary(self,frame):
        magic,identifier,sequence=HEADER.unpack_from(frame)
        assert magic==MAGIC and str(UUID(bytes=identifier))==self.start['reply_id']
        assert sequence==self.sequence and len(frame)<=1024
        self.wire.extend(frame[HEADER.size:]);self.sequence+=1
        if self.sequence%16==0 or len(self.wire)==self.start['bytes']:
            await self.windows.put({'type':'reply_ack','reply_id':self.start['reply_id'],
                'next_sequence':self.sequence,'received_bytes':len(self.wire)})

    async def next_window(self):return await asyncio.wait_for(self.windows.get(),2)


class ReplyPipelineTests(unittest.IsolatedAsyncioTestCase):
    async def test_exactly_two_windows_before_ack_and_one_credit_per_ordered_ack(self):
        peer=GatedSpeaker();task=asyncio.create_task(peer.channel.segment(b'\0\0'*128000,0))
        first,second=await peer.next_window(),await peer.next_window()
        self.assertEqual([first['next_sequence'],second['next_sequence']],[16,32])
        await asyncio.sleep(0)
        self.assertTrue(peer.windows.empty());self.assertEqual(len(peer.channel.pending_windows),2)
        peer.channel.acknowledge(first);third=await peer.next_window()
        self.assertEqual(third['next_sequence'],48);self.assertEqual(len(peer.channel.pending_windows),2)
        peer.channel.acknowledge(second);fourth=await peer.next_window()
        self.assertEqual(fourth['next_sequence'],64)
        peer.channel.acknowledge(third);await asyncio.sleep(0)
        self.assertNotIn('reply_end',[m['type'] for m in peer.messages])
        peer.channel.acknowledge(fourth);result=await asyncio.wait_for(task,2)
        self.assertTrue(result['device_completed']);self.assertFalse(peer.channel.pending_windows)
        self.assertEqual(peer.channel.last_timing['pipeline_windows'],2)

    async def test_legacy_peer_without_pipeline_field_keeps_stop_and_wait(self):
        peer=GatedSpeaker(negotiated=None);task=asyncio.create_task(peer.channel.segment(b'\0\0'*64000,0))
        first=await peer.next_window();await asyncio.sleep(0)
        self.assertTrue(peer.windows.empty());self.assertEqual(peer.channel.pipeline_windows,1)
        peer.channel.acknowledge(first);second=await peer.next_window();peer.channel.acknowledge(second)
        self.assertTrue((await asyncio.wait_for(task,2))['device_completed'])

    async def test_unsupported_or_malformed_pipeline_negotiation_sends_no_audio(self):
        for allow,size in [(False,2),(True,True),(True,0),(True,3),(True,'2')]:
            with self.subTest(allow=allow,size=size):
                peer=GatedSpeaker(allow=allow,negotiated=size)
                with self.assertRaisesRegex(XiaoNuoError,'INVALID_ACK'):
                    await peer.channel.segment(b'\0\0'*1000,0)
                self.assertFalse(peer.wire);await peer.channel.abort()
        peer=GatedSpeaker(window_frames=32)
        with self.assertRaisesRegex(XiaoNuoError,'INVALID_ACK'):
            await peer.channel.segment(b'\0\0'*1000,0)
        self.assertFalse(peer.wire);await peer.channel.abort()

    async def test_wrong_second_ack_duplicate_and_cancel_clear_all_credits(self):
        for wrong in ('out_of_order','wrong_bytes','duplicate'):
            peer=GatedSpeaker();task=asyncio.create_task(peer.channel.segment(b'\0\0'*128000,0))
            first,second=await peer.next_window(),await peer.next_window()
            if wrong=='duplicate':
                peer.channel.acknowledge(first);await peer.next_window();bad=first
            else:bad=second if wrong=='out_of_order' else first|{'received_bytes':15999}
            with self.assertRaisesRegex(XiaoNuoError,'INVALID_ACK'):peer.channel.acknowledge(bad)
            peer.cancelled.set();await peer.channel.abort()
            with self.assertRaises((asyncio.CancelledError,XiaoNuoError)):
                await asyncio.wait_for(task,2)
            self.assertFalse(peer.channel.pending_windows)
            self.assertNotIn('reply_end',[m['type'] for m in peer.messages])

    async def test_odd_adpcm_tail_and_final_ack_are_preserved(self):
        peer=GatedSpeaker();task=asyncio.create_task(peer.channel.segment(b'\x12\x34'*64001,0))
        for _ in range(3):peer.channel.acknowledge(await peer.next_window())
        result=await asyncio.wait_for(task,2)
        self.assertEqual(len(peer.wire),32001);self.assertEqual(peer.sequence,33)
        self.assertEqual(peer.start['samples'],64001);self.assertTrue(result['device_completed'])

    def test_pipeline_requires_bounded_compressed_buffered_mode(self):
        for changes in [{'compressed':False,'max_seconds':60},{'compressed':True,'max_seconds':20},
                        {'compressed':True,'max_seconds':8}]:
            with self.assertRaises(ValueError):ReplyChannel(None,None,None,None,allow_pipeline=True,**changes)
