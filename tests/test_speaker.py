"""Speaker protocol and resampling contracts. No physical output is opened."""
from array import array
from dataclasses import replace
from io import BytesIO
from pathlib import Path
import asyncio
import json
import math
import tempfile
import unittest
import sys
import wave
import zlib
from uuid import uuid4

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'backend'))

from app.speaker import device_pcm,device_speech_pcm,stream_pcm
from app.speech import wav_bytes
from app.config import Settings
from app.device_bridge import DeviceBridge
from app.budgeting import UsageLedger
from app.storage import CompanionStore
from app.errors import XiaoNuoError
from app.main import create_app
from test_core import request_asgi

class PlaybackPort:
    def __init__(self,corrupt=None,gain_divisor=32):
        self.pending=[];self.pcm=bytearray();self.commands=[];self.corrupt=corrupt;self.seq=0
        self.gain_divisor=gain_divisor
    def write(self,data):
        if data.startswith(b'PLAY_DATA'):
            remaining=data
            while remaining:
                header,_,body=remaining.partition(b'\n')
                count=int(header.split()[3])
                self.write_one(header+b'\n'+body[:count])
                remaining=body[count:]
            return len(data)
        return self.write_one(data)

    def write_one(self,data):
        header,_,body=data.partition(b'\n');parts=header.decode('ascii').split();command=parts[0]
        self.commands.append(command)
        if command=='PLAY_START':
            self.identifier=parts[1];self.samples=int(parts[2])
            reply={'event':'play_ready','id':self.identifier,'sample_rate':16000,'gain_divisor':self.gain_divisor,'max_seconds':8}
            if self.corrupt=='gain':reply['gain_divisor']=1
        elif command=='PLAY_DATA':
            assert len(body)==int(parts[3]) and zlib.crc32(body)==int(parts[4])
            assert int(parts[2])==self.seq
            self.pcm.extend(body)
            reply={'event':'play_ack','id':self.identifier,'seq':self.seq,'samples':len(self.pcm)//2}
            self.seq+=1
            if self.corrupt=='sequence':reply['seq']+=1
            if self.corrupt=='session':reply['id']='old-session'
        elif command=='PLAY_END':
            reply={'event':'play_end','id':self.identifier,'samples':len(self.pcm)//2,'crc32':zlib.crc32(self.pcm)}
            if self.corrupt=='checksum':reply['crc32']^=1
        else:reply={'event':'play_stopped','id':getattr(self,'identifier','')}
        self.pending.append(json.dumps(reply).encode()+b'\n')
        return len(data)
    def readline(self,limit):return self.pending.pop(0) if self.pending else b''


class SpeakerTests(unittest.TestCase):
    def test_24k_conversion_preserves_pitch_duration_and_suppresses_aliasing(self):
        def convert(frequency):
            samples=array('h',(int(10000*math.sin(2*math.pi*frequency*i/24000)) for i in range(24000)))
            stream=BytesIO()
            with wave.open(stream,'wb') as wav:
                wav.setnchannels(1);wav.setsampwidth(2);wav.setframerate(24000);wav.writeframes(samples.tobytes())
            pcm=device_pcm(stream.getvalue(),ROOT)
            self.assertEqual(len(pcm),32000)
            result=array('h');result.frombytes(pcm)
            return result
        low=convert(1000);high=convert(10000)
        crosses=sum(low[i]<=0<low[i+1] for i in range(len(low)-1))
        self.assertAlmostEqual(crosses,1000,delta=2)
        rms=lambda data:math.sqrt(sum(x*x for x in data[100:-100])/(len(data)-200))
        self.assertLess(rms(high),rms(low)/20)

    def test_long_audio_is_rejected_without_silently_truncating(self):
        with self.assertRaisesRegex(XiaoNuoError,'SPEAKER_AUDIO_TOO_LONG'):
            device_pcm(wav_bytes(bytes(16000*2*9)),ROOT)
        pcm=array('h',[100,-100]*30).tobytes()
        self.assertEqual(device_pcm(wav_bytes(pcm),ROOT),pcm)

    def test_long_network_pcm_requires_explicit_bounded_opt_in(self):
        blob=wav_bytes(bytes(9*32000))
        self.assertEqual(len(device_pcm(blob,ROOT,max_seconds=40)),9*32000)
        self.assertEqual(len(device_speech_pcm(blob,ROOT,volume=.5,max_seconds=40)),9*32000)
        for limit in (True,0,41,40.0):
            with self.assertRaises(XiaoNuoError):device_pcm(blob,ROOT,max_seconds=limit)
        with self.assertRaises(XiaoNuoError):device_pcm(wav_bytes(bytes(41*32000)),ROOT,max_seconds=40)

    def test_pcm_integrity_and_device_receipt_does_not_claim_audibility(self):
        pcm=bytes(range(256))*9
        port=PlaybackPort()
        result=stream_pcm(port,pcm,cancelled=lambda:False)
        self.assertEqual(bytes(port.pcm),pcm)
        self.assertTrue(result['device_completed'])
        self.assertFalse(result['audible_verified'])

    def test_speech_normalization_uses_headroom_without_changing_length_or_polarity(self):
        source=array('h',[0,10000,-5000,2500]*100)
        result=array('h');result.frombytes(device_speech_pcm(wav_bytes(source.tobytes()),ROOT,volume=1))
        self.assertEqual(len(result),len(source))
        self.assertEqual(max(result),31129)
        self.assertAlmostEqual(result[2]/result[1],-0.5,delta=0.0001)
        self.assertAlmostEqual(result[3]/result[1],0.25,delta=0.0001)

    def test_speech_normalization_silence_zero_volume_and_low_level_gain_cap(self):
        self.assertEqual(device_speech_pcm(wav_bytes(bytes(200)),ROOT,volume=1),bytes(200))
        pcm=array('h',[10000,-10000]*10).tobytes()
        self.assertEqual(device_speech_pcm(wav_bytes(pcm),ROOT,volume=0),bytes(len(pcm)))
        quiet=array('h',[2,-2]*10)
        result=array('h');result.frombytes(device_speech_pcm(wav_bytes(quiet.tobytes()),ROOT,volume=1))
        self.assertEqual(max(result),8)

    def test_speech_normalization_bounds_full_scale_samples_and_scales_with_volume(self):
        blob=wav_bytes(array('h',[-32768,32767,0]*50).tobytes())
        full=array('h');full.frombytes(device_speech_pcm(blob,ROOT,volume=1))
        half=array('h');half.frombytes(device_speech_pcm(blob,ROOT,volume=0.5))
        self.assertLessEqual(max(abs(value) for value in full),31129)
        for loud,soft in zip(full,half):self.assertAlmostEqual(soft,loud/2,delta=1)

    def test_speech_normalization_rejects_invalid_volume(self):
        for volume in [-1,1.1,float('nan'),float('inf'),True,'1']:
            with self.assertRaisesRegex(XiaoNuoError,'INVALID_SPEECH_STYLE'):
                device_speech_pcm(wav_bytes(bytes(20)),ROOT,volume=volume)

    def test_device_playback_api_applies_speech_normalization_at_saved_volume(self):
        with tempfile.TemporaryDirectory() as directory:
            settings=Settings(root=Path(directory),admin_token='test-'+40*'x')
            app=create_app(settings);voice=app.state.voice_service
            voice.store.patch_controls({'volume':0.5})
            calls=[]
            voice.bridge.play_audio=lambda pcm,**kwargs:calls.append(pcm) or {'device_completed':True,'audible_verified':False}
            token=voice.cache.put(wav_bytes(array('h',[10000,-10000]*50).tobytes()))
            status,_=asyncio.run(request_asgi(app,'/api/v1/voice/play-device',method='POST',
                body={'token':token,'request_id':str(uuid4())},token=settings.admin_token))
            self.assertEqual(status,200)
            sent=array('h');sent.frombytes(calls[0])
            self.assertLessEqual(max(abs(value) for value in sent),15564)
            self.assertGreater(max(sent),10000)

    def test_bad_session_sequence_limits_or_checksum_stop_output(self):
        for error in ['sequence','session','gain','checksum']:
            port=PlaybackPort(corrupt=error)
            with self.assertRaises(XiaoNuoError):stream_pcm(port,bytes(2048),cancelled=lambda:False)
            self.assertEqual(port.commands[-1],'PLAY_STOP')

    def test_supported_firmware_gains_are_reported_and_unknown_gains_stop_before_data(self):
        for divisor in (2,4,8,32):
            port=PlaybackPort(gain_divisor=divisor)
            result=stream_pcm(port,bytes(2048),cancelled=lambda:False)
            self.assertEqual(result['gain_divisor'],divisor)
            self.assertEqual(len(port.pcm),2048)
        for divisor in (1,16,0,-2,True,2.0,'2',4.0,'4',8.0,'8'):
            port=PlaybackPort(gain_divisor=divisor)
            with self.assertRaisesRegex(XiaoNuoError,'SPEAKER_LIMIT_MISMATCH'):
                stream_pcm(port,bytes(2048),cancelled=lambda:False)
            self.assertEqual(len(port.pcm),0)
            self.assertEqual(port.commands[-1],'PLAY_STOP')

    def test_usb_latency_is_hidden_by_bounded_three_frame_batches(self):
        class DelayedUSB(PlaybackPort):
            acknowledged=0
            def write(self,data):
                result=super().write(data)
                if data.startswith(b'PLAY_DATA'):
                    if self.seq-self.acknowledged>3:raise AssertionError('Unbounded UART queue')
                    if len(data)>=4096:raise AssertionError('UART buffer capacity exceeded')
                return result
            def readline(self,limit):
                if self.pending and json.loads(self.pending[0]).get('event')=='play_ack':
                    if self.seq==1:return b''  # First ACK is delayed until next packet arrives.
                    self.acknowledged+=1
                return super().readline(limit)
        pcm=bytes(16384)
        port=DelayedUSB()
        result=stream_pcm(port,pcm,cancelled=lambda:False)
        self.assertTrue(result['device_completed'])
        self.assertEqual(port.acknowledged,16)
        self.assertEqual(bytes(port.pcm),pcm)

    def test_cancel_between_frames_sends_stop_and_does_not_claim_completion(self):
        port=PlaybackPort()
        with self.assertRaisesRegex(XiaoNuoError,'SPEAKER_PLAYBACK_CANCELLED'):
            stream_pcm(port,bytes(4096),cancelled=lambda:len(port.pcm)>=1024)
        self.assertEqual(len(port.pcm),3072)
        self.assertEqual(port.commands[-1],'PLAY_STOP')

    def test_speaker_is_disabled_by_default_before_port_is_opened(self):
        with tempfile.TemporaryDirectory() as directory:
            settings=Settings(root=Path(directory),usb_port='COM99')
            bridge=DeviceBridge(settings,CompanionStore(UsageLedger(settings.database)),factory=lambda:self.fail('Unexpected hardware access'))
            with self.assertRaisesRegex(XiaoNuoError,'DEVICE_SPEAKER_DISABLED'):bridge.play_audio(bytes(100))

    def test_owner_cancel_survives_reset_of_bridge_cancel_flag(self):
        import threading
        from app.device_bridge import DeviceRequest
        with tempfile.TemporaryDirectory() as directory:
            settings=Settings(root=Path(directory),usb_port='COM99',device_speaker_enabled=True)
            bridge=DeviceBridge(settings,CompanionStore(UsageLedger(settings.database)))
            owner=threading.Event();owner.set()
            request=DeviceRequest('play_audio',pcm=bytes(100),external_cancel=owner)
            port=PlaybackPort();bridge._port=port;bridge._play_cancel.clear()
            with self.assertRaisesRegex(XiaoNuoError,'SPEAKER_PLAYBACK_CANCELLED'):bridge._play_audio(request)
            self.assertNotIn('PLAY_START',port.commands)
            self.assertEqual(bridge.status()['playback_state'],'idle')

    def test_playback_api_auth_quiet_and_idempotency(self):
        with tempfile.TemporaryDirectory() as directory:
            settings=Settings(root=Path(directory),admin_token='test-'+40*'x')
            app=create_app(settings);voice=app.state.voice_service
            calls=[]
            voice.bridge.play_audio=lambda pcm,**kwargs:calls.append(pcm) or {'device_completed':True,'audible_verified':False}
            token=voice.cache.put(wav_bytes(bytes(100)))
            body={'token':token,'request_id':str(uuid4())}
            async def scenario():
                status,_=await request_asgi(app,'/api/v1/voice/play-device',method='POST',body=body)
                self.assertEqual(status,401)
                voice.store.set_quiet(True)
                status,_=await request_asgi(app,'/api/v1/voice/play-device',method='POST',body=body,token=settings.admin_token)
                self.assertEqual(status,409);self.assertEqual(calls,[])
                voice.store.set_quiet(False)
                status,result=await request_asgi(app,'/api/v1/voice/play-device',method='POST',body=body,token=settings.admin_token)
                self.assertEqual(status,200);self.assertFalse(result['audible_verified'])
                status,_=await request_asgi(app,'/api/v1/voice/play-device',method='POST',body=body,token=settings.admin_token)
                self.assertEqual(status,409);self.assertEqual(len(calls),1)
            asyncio.run(scenario())
