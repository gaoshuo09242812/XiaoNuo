"""Synthetic PCM only. No device, recording, speaker, models or cloud calls."""
import hashlib
import struct
import unittest
from uuid import uuid4

from app.errors import XiaoNuoError
from app.network_audio import AudioReceiver,encode_audio_frame,HEADER,MAX_FRAME_BYTES


class NetworkAudioTests(unittest.TestCase):
    def feed_compressed(self,receiver,data):
        for seq,offset in enumerate(range(0,len(data),1000)):
            receiver.push(encode_audio_frame(receiver.stream_id,seq,data[offset:offset+1000],compressed=True))

    def test_negotiated_trim_preserves_exact_short_audio_and_zeroes_buffer(self):
        from app.adpcm import ENCODING,decode
        wire=bytes(range(256))*62+b'\0'*128
        self.assertEqual(len(wire),16000)
        receiver=AudioReceiver(uuid4(),6,encoding=ENCODING,allow_trim_end=True)
        self.assertTrue(receiver.snapshot()['trim_end_enabled'])
        self.feed_compressed(receiver,wire)
        result=receiver.finish(hashlib.sha256(wire).hexdigest(),samples=32000)
        self.assertEqual(result,decode(wire)[0]);self.assertEqual(len(result),64000)
        self.assertEqual(receiver.snapshot()['received_bytes'],0)

    def test_old_exact_length_completion_stays_strict_even_when_trim_advertised(self):
        from app.adpcm import ENCODING
        receiver=AudioReceiver(uuid4(),6,encoding=ENCODING,allow_trim_end=True)
        self.feed_compressed(receiver,b'\0'*16000)
        with self.assertRaisesRegex(XiaoNuoError,'INCOMPLETE'):
            receiver.finish(hashlib.sha256(b'\0'*16000).hexdigest())
        receiver=AudioReceiver(uuid4(),6,encoding=ENCODING,allow_trim_end=True)
        self.feed_compressed(receiver,b'\0'*48000)
        self.assertEqual(len(receiver.finish(hashlib.sha256(b'\0'*48000).hexdigest())),192000)

    def test_trim_rejects_unnegotiated_invalid_counts_and_wrong_hash(self):
        from app.adpcm import ENCODING
        for samples in (True,'32000',32000.,31998,32001,96002):
            with self.subTest(samples=samples):
                receiver=AudioReceiver(uuid4(),6,encoding=ENCODING,allow_trim_end=True)
                self.feed_compressed(receiver,b'\0'*16000)
                with self.assertRaisesRegex(XiaoNuoError,'INVALID_TRIM'):
                    receiver.finish('0'*64,samples=samples)
                self.assertEqual(receiver.snapshot()['received_bytes'],0)
        for negotiated in (False,True):
            receiver=AudioReceiver(uuid4(),6,encoding=ENCODING,allow_trim_end=negotiated)
            self.feed_compressed(receiver,b'\0'*16000)
            with self.assertRaisesRegex(XiaoNuoError,'CHECKSUM_MISMATCH' if negotiated else 'INVALID_TRIM'):
                receiver.finish('0'*64,samples=32000)
            self.assertEqual(receiver.snapshot()['received_bytes'],0)

    def test_trim_cannot_expand_declared_duration_or_misstate_received_length(self):
        from app.adpcm import ENCODING
        for samples,code in ((66000,'INVALID_TRIM'),(34000,'INCOMPLETE')):
            receiver=AudioReceiver(uuid4(),4,encoding=ENCODING,allow_trim_end=True)
            self.feed_compressed(receiver,b'\0'*16000)
            with self.assertRaisesRegex(XiaoNuoError,code):receiver.finish('0'*64,samples=samples)
        with self.assertRaises(XiaoNuoError):AudioReceiver(uuid4(),6,allow_trim_end=True)

    def fixture(self,seconds=2):
        return struct.pack('<h',1234)*(16000*seconds)

    def feed(self,receiver,pcm):
        for sequence,offset in enumerate(range(0,len(pcm),1000)):
            frame=encode_audio_frame(receiver.stream_id,sequence,pcm[offset:offset+1000])
            self.assertLessEqual(len(frame),1024)
            receiver.push(frame)

    def test_adpcm_mobile_transfer_has_bounded_grace_without_extending_recording(self):
        from app.adpcm import ENCODING
        clock=[0]
        receiver=AudioReceiver(uuid4(),6,clock=lambda:clock[0],encoding=ENCODING)
        clock[0]=20
        receiver.push(encode_audio_frame(receiver.stream_id,0,b'\0'*1000))
        self.assertEqual(receiver.expected_bytes,48000)
        self.assertEqual(receiver.snapshot()['received_bytes'],1000)
        self.assertAlmostEqual(receiver.remaining_seconds(),10)
        clock[0]=30
        with self.assertRaisesRegex(XiaoNuoError,'NETWORK_AUDIO_TIMEOUT'):
            receiver.remaining_seconds()
        self.assertEqual(receiver.snapshot()['received_bytes'],0)

    def test_exact_six_seconds_round_trip_and_one_time_ownership(self):
        receiver=AudioReceiver(uuid4(),6);pcm=self.fixture(6)
        self.feed(receiver,pcm)
        self.assertEqual(receiver.finish(hashlib.sha256(pcm).hexdigest()),pcm)
        self.assertEqual(receiver.snapshot()['received_bytes'],0)
        with self.assertRaisesRegex(XiaoNuoError,'STREAM_CLOSED'):
            receiver.finish(hashlib.sha256(pcm).hexdigest())

    def test_duplicate_or_out_of_order_frame_discards_partial_audio(self):
        for sequence in (0,2):
            receiver=AudioReceiver(uuid4(),2)
            receiver.push(encode_audio_frame(receiver.stream_id,0,b'\0'*1000))
            with self.assertRaisesRegex(XiaoNuoError,'INVALID_SEQUENCE'):
                receiver.push(encode_audio_frame(receiver.stream_id,sequence,b'\0'*2))
            self.assertEqual(receiver.snapshot()['received_bytes'],0)

    def test_old_connection_frame_cannot_enter_new_stream(self):
        request=uuid4();old=AudioReceiver(request,2);new=AudioReceiver(request,2)
        self.assertNotEqual(old.stream_id,new.stream_id)
        with self.assertRaisesRegex(XiaoNuoError,'INVALID_STREAM'):
            new.push(encode_audio_frame(old.stream_id,0,b'\0\0'))

    def test_bad_magic_binary_size_and_odd_pcm_fail_closed(self):
        for change in ('magic','large','odd','empty','text'):
            receiver=AudioReceiver(uuid4(),2)
            valid=encode_audio_frame(receiver.stream_id,0,b'\0\0')
            malformed={'magic':b'FAIL'+valid[4:],'large':valid+b'\0'*1024,
                       'odd':valid+b'\0','empty':valid[:HEADER.size],'text':'private text'}[change]
            with self.assertRaises(XiaoNuoError):receiver.push(malformed)
            self.assertEqual(receiver.snapshot()['state'],'failed')

    def test_declared_duration_cannot_be_overrun(self):
        receiver=AudioReceiver(uuid4(),2)
        self.feed(receiver,self.fixture())
        with self.assertRaisesRegex(XiaoNuoError,'TOO_LONG'):
            receiver.push(encode_audio_frame(receiver.stream_id,64,b'\0\0'))
        self.assertEqual(receiver.snapshot()['received_bytes'],0)

    def test_partial_recording_cannot_be_committed(self):
        receiver=AudioReceiver(uuid4(),2)
        receiver.push(encode_audio_frame(receiver.stream_id,0,b'\0\0'))
        with self.assertRaisesRegex(XiaoNuoError,'INCOMPLETE'):receiver.finish('0'*64)
        self.assertEqual(receiver.snapshot()['received_bytes'],0)

    def test_corrupt_checksum_discards_completed_buffer(self):
        receiver=AudioReceiver(uuid4(),2)
        self.feed(receiver,self.fixture())
        with self.assertRaisesRegex(XiaoNuoError,'CHECKSUM_MISMATCH'):receiver.finish('0'*64)
        self.assertEqual(receiver.snapshot()['received_bytes'],0)

    def test_cancellation_prevents_further_input(self):
        receiver=AudioReceiver(uuid4(),2)
        receiver.push(encode_audio_frame(receiver.stream_id,0,b'\0\0'))
        receiver.cancel();receiver.cancel()
        self.assertEqual(receiver.snapshot()['state'],'cancelled')
        self.assertEqual(receiver.snapshot()['received_bytes'],0)
        with self.assertRaisesRegex(XiaoNuoError,'STREAM_CLOSED'):
            receiver.push(encode_audio_frame(receiver.stream_id,1,b'\0\0'))

    def test_total_deadline_cannot_be_extended_by_drip_feed(self):
        current=[0.0];receiver=AudioReceiver(uuid4(),2,clock=lambda:current[0])
        current[0]=14
        receiver.push(encode_audio_frame(receiver.stream_id,0,b'\0\0'))
        current[0]=15
        with self.assertRaisesRegex(XiaoNuoError,'TIMEOUT'):
            receiver.push(encode_audio_frame(receiver.stream_id,1,b'\0\0'))
        self.assertEqual(receiver.snapshot()['received_bytes'],0)

    def test_finish_also_enforces_deadline(self):
        current=[0.0];receiver=AudioReceiver(uuid4(),2,clock=lambda:current[0]);pcm=self.fixture()
        self.feed(receiver,pcm);current[0]=16
        with self.assertRaisesRegex(XiaoNuoError,'TIMEOUT'):receiver.finish(hashlib.sha256(pcm).hexdigest())

    def test_invalid_duration_and_sender_metadata_rejected(self):
        for seconds in (True,1,7,2.5,'6'):
            with self.assertRaises(XiaoNuoError):AudioReceiver(uuid4(),seconds)
        with self.assertRaises(XiaoNuoError):AudioReceiver('not-a-uuid',2)
        for sequence in (True,-1,2**32):
            with self.assertRaises(XiaoNuoError):encode_audio_frame(uuid4(),sequence,b'\0\0')
        with self.assertRaises(XiaoNuoError):encode_audio_frame('not-a-uuid',0,b'\0\0')
