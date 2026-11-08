"""Bounded PCM conversion and acknowledged USB playback; never claims audibility."""
from io import BytesIO
from array import array
import json
import math
from pathlib import Path
import secrets
import sys
import time
import wave
import zlib

from .errors import XiaoNuoError

MAX_SECONDS=8
RATE=16000


def device_pcm(blob: bytes,root: Path,*,max_seconds: int=8) -> bytes:
    if type(max_seconds) is not int or max_seconds not in {8,40}:
        raise XiaoNuoError('SPEAKER_AUDIO_TOO_LONG',422)
    try:
        with wave.open(BytesIO(blob),'rb') as source:
            rate=source.getframerate();frames=source.getnframes()
            if source.getnchannels()!=1 or source.getsampwidth()!=2 or rate not in {16000,24000} or source.getcomptype()!='NONE':
                raise XiaoNuoError('SPEAKER_AUDIO_FORMAT_INVALID',422)
            if not 0<frames<=rate*max_seconds:raise XiaoNuoError('SPEAKER_AUDIO_TOO_LONG',422)
            pcm=source.readframes(frames)
            if len(pcm)!=frames*2:raise XiaoNuoError('SPEAKER_AUDIO_TRUNCATED',422)
    except (wave.Error,EOFError,ValueError):raise XiaoNuoError('SPEAKER_AUDIO_FORMAT_INVALID',422) from None
    if rate==RATE:return pcm
    local=str(root/'.runtime-libs')
    if local not in sys.path:sys.path.insert(0,local)
    try:import numpy as np
    except ImportError:raise XiaoNuoError('AUDIO_CONVERTER_NOT_INSTALLED') from None
    samples=np.frombuffer(pcm,dtype='<i2').astype(np.float64)
    # Low-pass before 24 -> 16 kHz conversion; preserve duration and pitch.
    taps=np.arange(63)-31
    cutoff=7000/rate
    kernel=2*cutoff*np.sinc(2*cutoff*taps)*np.hamming(63)
    kernel/=kernel.sum()
    filtered=np.convolve(np.pad(samples,(31,31)),kernel,mode='valid')
    positions=np.arange(round(frames*RATE/rate))*rate/RATE
    converted=np.interp(positions,np.arange(frames),filtered)
    return np.clip(np.rint(converted),-32768,32767).astype('<i2').tobytes()


def device_speech_pcm(blob: bytes,root: Path,*,volume: float,max_seconds: int=8) -> bytes:
    """Use speech headroom without clipping, respecting volume and a 4x boost cap."""
    if type(volume) not in {int,float} or not math.isfinite(volume) or not 0<=volume<=1:
        raise XiaoNuoError('INVALID_SPEECH_STYLE',422)
    pcm=device_pcm(blob,root,max_seconds=max_seconds)
    target=round(32767*0.95*volume)
    if not target:return bytes(len(pcm))
    samples=array('h');samples.frombytes(pcm)
    if sys.byteorder!='little':samples.byteswap()
    peak=max(abs(value) for value in samples)
    if not peak:return pcm
    gain=min(4.0,target/peak)
    result=array('h',(max(-target,min(target,round(value*gain))) for value in samples))
    if sys.byteorder!='little':result.byteswap()
    return result.tobytes()


def stream_pcm(port,pcm: bytes,*,cancelled,on_status=lambda message:None,on_progress=lambda:None):
    if not pcm or len(pcm)%2 or len(pcm)>RATE*2*MAX_SECONDS:
        raise XiaoNuoError('SPEAKER_AUDIO_TOO_LONG',422)
    identifier=secrets.token_hex(4)
    started=False;finished=False

    def write(data):
        if port.write(data)!=len(data):raise XiaoNuoError('SPEAKER_TRANSFER_FAILED')

    def wait(event,seconds=2):
        deadline=time.monotonic()+seconds
        while time.monotonic()<deadline:
            if cancelled():raise XiaoNuoError('SPEAKER_PLAYBACK_CANCELLED',409)
            raw=port.readline(2048)
            if not raw:continue
            try:message=json.loads(raw)
            except (ValueError,UnicodeError):raise XiaoNuoError('SPEAKER_INVALID_RESPONSE') from None
            if len(raw)>=2048 or not isinstance(message,dict):raise XiaoNuoError('SPEAKER_INVALID_RESPONSE')
            kind=message.get('event')
            if kind=='status':on_status(message)
            if kind in {'play_error','error'}:raise XiaoNuoError('SPEAKER_DEVICE_ERROR')
            if kind==event:
                if message.get('id')!=identifier:raise XiaoNuoError('SPEAKER_SESSION_MISMATCH')
                on_progress()
                return message
        raise XiaoNuoError('SPEAKER_REPLY_TIMEOUT')

    try:
        if cancelled():raise XiaoNuoError('SPEAKER_PLAYBACK_CANCELLED',409)
        started=True
        write(f'PLAY_START {identifier} {len(pcm)//2}\n'.encode('ascii'))
        ready=wait('play_ready')
        gain_divisor=ready.get('gain_divisor')
        if ready.get('sample_rate')!=RATE or type(gain_divisor) is not int or gain_divisor not in {2,4,8,32} or ready.get('max_seconds')!=MAX_SECONDS:
            raise XiaoNuoError('SPEAKER_LIMIT_MISMATCH')
        # Batch at most three 1KB frames into one USB write. Separate writes
        # incur adapter latency even with multiple outstanding acknowledgments.
        # Three headers + payloads fit within the firmware's 4096-byte UART RX.
        frames=(len(pcm)+1023)//1024
        for first in range(0,frames,3):
            stop=min(frames,first+3)
            packet=bytearray()
            for sequence in range(first,stop):
                if cancelled():raise XiaoNuoError('SPEAKER_PLAYBACK_CANCELLED',409)
                offset=sequence*1024
                chunk=pcm[offset:offset+1024]
                header=f'PLAY_DATA {identifier} {sequence} {len(chunk)} {zlib.crc32(chunk)}\n'.encode('ascii')
                packet.extend(header+chunk)
            write(bytes(packet))
            for sequence in range(first,stop):
                ack=wait('play_ack')
                if type(ack.get('seq')) is not int or ack['seq']!=sequence or ack.get('samples')!=min((sequence+1)*1024,len(pcm))//2:
                    raise XiaoNuoError('SPEAKER_SEQUENCE_MISMATCH')
        checksum=zlib.crc32(pcm)
        write(f'PLAY_END {identifier} {checksum}\n'.encode('ascii'))
        end=wait('play_end')
        if end.get('samples')!=len(pcm)//2 or end.get('crc32')!=checksum:
            raise XiaoNuoError('SPEAKER_INCOMPLETE')
        finished=True
        return {'transferred':True,'device_completed':True,'audible_verified':False,
                'sample_rate':RATE,'samples':len(pcm)//2,'duration_seconds':len(pcm)/(RATE*2),
                'gain_divisor':gain_divisor,'crc32':checksum,'cloud_upload':False}
    finally:
        if started and not finished:
            try:
                port.write(b'PLAY_STOP\n')
                until=time.monotonic()+1
                while time.monotonic()<until:
                    raw=port.readline(2048)
                    try:message=json.loads(raw)
                    except (ValueError,UnicodeError):continue
                    if isinstance(message,dict) and message.get('event') in {'play_stopped','play_error'}:break
            except Exception:pass
