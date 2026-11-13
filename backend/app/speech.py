"""Local Chinese ASR/TTS, bounded audio handling and ephemeral playback."""
from array import array
from dataclasses import dataclass
from io import BytesIO
import base64
import json
import math
import os
from pathlib import Path
import secrets
import subprocess
import sys
import threading
import time
import wave
from uuid import UUID, uuid5

from .errors import XiaoNuoError
from .speaker import device_speech_pcm

RATE=16000
MAX_SECONDS=6
MAX_PCM=RATE*2*MAX_SECONDS


def split_spoken_reply(text: str,limit: int=24) -> list[str]:
    """Split at natural boundaries without losing non-whitespace content."""
    if not isinstance(text,str) or not text.strip():raise XiaoNuoError('INVALID_TTS_TEXT',422)
    if type(limit) is not int or limit<2:raise ValueError('Invalid speech segment limit')
    remaining=text.strip();parts=[]
    while len(remaining)>limit:
        head=remaining[:limit]
        boundaries=[i+1 for i,char in enumerate(head) if char in '。！？!?；;，,、\n ' and i>=3]
        cut=boundaries[-1] if boundaries else limit
        parts.append(remaining[:cut].strip());remaining=remaining[cut:].strip()
    if remaining:parts.append(remaining)
    return parts


def audio_statistics(pcm: bytes) -> dict:
    if not pcm or len(pcm)>MAX_PCM or len(pcm)%2:
        raise XiaoNuoError('INVALID_AUDIO_LENGTH',422)
    samples=array('h')
    samples.frombytes(pcm)
    if sys.byteorder!='little':samples.byteswap()
    mean=sum(samples)/len(samples)
    variance=sum((sample-mean)**2 for sample in samples)/len(samples)
    return {'samples':len(samples),'duration_seconds':len(samples)/RATE,
            'mean':round(mean,2),'ac_rms':round(math.sqrt(variance),2),
            'clipped':sum(abs(sample)>=32760 for sample in samples)}


def wav_bytes(pcm: bytes) -> bytes:
    with BytesIO() as stream:
        with wave.open(stream,'wb') as output:
            output.setnchannels(1)
            output.setsampwidth(2)
            output.setframerate(RATE)
            output.writeframes(pcm)
        return stream.getvalue()


def pcm_from_wav(blob: bytes) -> bytes:
    if len(blob)>MAX_PCM+4096:
        raise XiaoNuoError('AUDIO_TOO_LONG',413)
    try:
        with wave.open(BytesIO(blob),'rb') as source:
            if source.getnchannels()!=1 or source.getsampwidth()!=2 or source.getframerate()!=RATE or source.getcomptype()!='NONE':
                raise XiaoNuoError('AUDIO_FORMAT_REQUIRES_16K_MONO_PCM16',422)
            if source.getnframes()>RATE*MAX_SECONDS:
                raise XiaoNuoError('AUDIO_TOO_LONG',413)
            pcm=source.readframes(RATE*MAX_SECONDS+1)
            if len(pcm)!=source.getnframes()*2:
                raise XiaoNuoError('TRUNCATED_AUDIO',422)
    except (wave.Error,EOFError,ValueError):
        raise XiaoNuoError('INVALID_WAV',422) from None
    audio_statistics(pcm)
    return pcm


class ASRProvider:
    def transcribe(self,pcm: bytes) -> dict:
        raise NotImplementedError


class TTSProvider:
    def synthesize(self,text: str,*,volume: float=0.5,speed: float=1.0,voice: str='soft') -> bytes:
        raise NotImplementedError


def playback_wav_info(blob: bytes) -> dict:
    try:
        with wave.open(BytesIO(blob),'rb') as source:
            rate=source.getframerate()
            frames=source.getnframes()
            if source.getnchannels()!=1 or source.getsampwidth()!=2 or rate not in {16000,24000} or source.getcomptype()!='NONE':
                raise XiaoNuoError('TTS_INVALID_RESPONSE')
            if not 0<frames<=rate*40 or len(source.readframes(frames))!=frames*2:
                raise XiaoNuoError('TTS_AUDIO_LENGTH_INVALID')
    except (wave.Error,EOFError,ValueError):raise XiaoNuoError('TTS_INVALID_RESPONSE') from None
    return {'sample_rate':rate,'duration_seconds':frames/rate,'channels':1,'bits':16}


class VoskASR(ASRProvider):
    def __init__(self,root: Path):
        self.root=root
        self.model=root/'data/models/vosk-model-small-cn-0.22'
        self.last_success=None

    def status(self):
        return {'provider':'vosk','model':self.model.name,'model_present':(self.model/'am/final.mdl').is_file(),
                'last_success':self.last_success,'cloud_upload':False}

    def transcribe(self,pcm: bytes) -> dict:
        return self._transcribe(pcm)

    def transcribe_wake(self, pcm: bytes) -> dict:
        return self._transcribe(pcm, confirm_wake=True)

    def _transcribe(self, pcm: bytes, *, confirm_wake=False) -> dict:
        metrics=audio_statistics(pcm)
        if metrics['ac_rms']<8:
            raise XiaoNuoError('ASR_EMPTY_AUDIO',422)
        if not (self.model/'am/final.mdl').is_file():
            raise XiaoNuoError('ASR_MODEL_NOT_INSTALLED')
        env=dict(os.environ)
        env['PYTHONPATH']=os.pathsep.join([str(self.root/'backend'),str(self.root/'.runtime-libs')])
        env['PYTHONIOENCODING']='utf-8'
        command = [sys.executable, '-m', 'app.speech_worker', str(self.model)]
        if confirm_wake:
            command.append('--confirm-wake')
        try:
            result=subprocess.run(command,input=pcm,
                                  capture_output=True,env=env,timeout=20,
                                  creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
        except subprocess.TimeoutExpired:
            raise XiaoNuoError('ASR_TIMEOUT') from None
        if result.returncode or len(result.stdout)>16384:
            raise XiaoNuoError('ASR_PROVIDER_FAILED')
        try:
            answer=json.loads(result.stdout)
            if not isinstance(answer.get('text'),str) or not answer['text'].strip():
                raise XiaoNuoError('ASR_NO_SPEECH_RECOGNIZED',422)
        except (ValueError,TypeError,AttributeError):
            raise XiaoNuoError('ASR_INVALID_RESPONSE') from None
        self.last_success=time.time()
        return answer|{'audio_statistics':metrics,'cloud_upload':False}


class WindowsChineseTTS(TTSProvider):
    def __init__(self,root: Path):
        self.root=root
        self.last_success=None

    def status(self):
        return {'provider':'windows_sapi','platform_supported':sys.platform=='win32',
                'last_success':self.last_success,'cloud_upload':False,'speaker':'browser_only'}

    def synthesize(self,text: str,*,volume: float=0.5,speed: float=1.0,voice: str='soft') -> bytes:
        if sys.platform!='win32':raise XiaoNuoError('LOCAL_TTS_REQUIRES_WINDOWS')
        if not isinstance(text,str) or not 1<=len(text.strip())<=500:
            raise XiaoNuoError('INVALID_TTS_TEXT',422)
        if not 0<=volume<=1 or not 0.7<=speed<=1.4:
            raise XiaoNuoError('INVALID_SPEECH_STYLE',422)
        executable=Path(os.environ.get('SystemRoot',r'C:\Windows'))/'System32/WindowsPowerShell/v1.0/powershell.exe'
        payload=json.dumps({'text':text,'volume':round(volume*100),'rate':round((speed-1)*10)},ensure_ascii=True).encode('ascii')
        try:
            result=subprocess.run([str(executable),'-NoProfile','-NonInteractive','-File',str(Path(__file__).with_name('speech_sapi.ps1'))],
                                  input=payload,capture_output=True,timeout=25,
                                  creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
        except subprocess.TimeoutExpired:
            raise XiaoNuoError('TTS_TIMEOUT') from None
        if result.returncode or len(result.stdout)>2_000_000:
            raise XiaoNuoError('TTS_PROVIDER_FAILED')
        try:
            pcm=base64.b64decode(result.stdout.strip(),validate=True)
        except ValueError:
            raise XiaoNuoError('TTS_INVALID_RESPONSE') from None
        if not pcm or len(pcm)%2 or len(pcm)>RATE*2*40:
            raise XiaoNuoError('TTS_AUDIO_LENGTH_INVALID')
        self.last_success=time.time()
        return wav_bytes(pcm)


class WindowsChineseASR(ASRProvider):
    def __init__(self,root: Path):
        self.root=root
        self.last_success=None

    def status(self):
        return {'provider':'windows_sapi','model':'zh-CN-dictation','platform_supported':sys.platform=='win32',
                'last_success':self.last_success,'cloud_upload':False}

    def transcribe(self,pcm: bytes) -> dict:
        metrics=audio_statistics(pcm)
        if metrics['ac_rms']<8:raise XiaoNuoError('ASR_EMPTY_AUDIO',422)
        executable=Path(os.environ.get('SystemRoot',r'C:\Windows'))/'System32/WindowsPowerShell/v1.0/powershell.exe'
        payload=json.dumps({'pcm':base64.b64encode(pcm).decode('ascii')}).encode('ascii')
        try:
            result=subprocess.run([str(executable),'-NoProfile','-NonInteractive','-File',str(Path(__file__).with_name('speech_recognize_sapi.ps1'))],
                                  input=payload,capture_output=True,timeout=20,
                                  creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
        except subprocess.TimeoutExpired:raise XiaoNuoError('ASR_TIMEOUT') from None
        if result.returncode or len(result.stdout)>16384:raise XiaoNuoError('ASR_PROVIDER_FAILED')
        try:
            answer=json.loads(result.stdout.decode('utf-8-sig'))
            if not isinstance(answer.get('text'),str) or not answer['text'].strip():
                raise XiaoNuoError('ASR_NO_SPEECH_RECOGNIZED',422)
        except (ValueError,TypeError,AttributeError):raise XiaoNuoError('ASR_INVALID_RESPONSE') from None
        self.last_success=time.time()
        return answer|{'duration_seconds':len(pcm)/32000,'audio_statistics':metrics,'cloud_upload':False}


@dataclass
class CachedAudio:
    data: bytes
    expires: float


class AudioCache:
    def __init__(self):
        self._lock=threading.Lock()
        self._items={}
        self._generation=0

    @property
    def generation(self):
        with self._lock:return self._generation

    def put(self,data: bytes,*,expected_generation=None) -> str:
        if len(data)>1_920_044:raise XiaoNuoError('TTS_AUDIO_LENGTH_INVALID')
        with self._lock:
            if expected_generation is not None and expected_generation!=self._generation:
                raise XiaoNuoError('AUDIO_CAPTURE_CANCELLED',409)
            self._prune()
            while len(self._items)>=8:self._items.pop(next(iter(self._items)))
            token=secrets.token_urlsafe(24)
            self._items[token]=CachedAudio(data,time.monotonic()+90)
            return token

    def _prune(self):
        expired=[key for key,item in self._items.items() if item.expires<time.monotonic()]
        for key in expired:self._items.pop(key,None)

    def get(self,token: str) -> bytes:
        with self._lock:
            self._prune()
            item=self._items.get(token)
            if not item:raise XiaoNuoError('AUDIO_EXPIRED',404)
            return item.data

    def clear(self):
        with self._lock:
            self._generation+=1
            self._items.clear()

    def discard(self,token):
        with self._lock:self._items.pop(token,None)


class VoiceService:
    def __init__(self,root,bridge,companion,budget,store,asr=None,tts=None):
        self.root=root
        self.bridge,self.companion,self.budget,self.store=bridge,companion,budget,store
        self.asr=asr or (VoskASR(root) if (root/'data/models/vosk-model-small-cn-0.22/am/final.mdl').is_file() or sys.platform!='win32' else WindowsChineseASR(root))
        self.tts=tts or WindowsChineseTTS(root)
        self.cache=AudioCache()
        self._turn=threading.Lock()
        self._cancelled=threading.Event()
        self._progress={'stage':'idle'}

    def cancel(self):
        self._cancelled.set()
        self.bridge.cancel_capture()
        self.bridge.cancel_playback()
        self.cache.clear()
        close_tts=getattr(self.tts,'close',None)
        if callable(close_tts):close_tts()
        self._progress={'stage':'stopping' if self._turn.locked() else 'idle'}

    def _guard(self,*,output=False):
        if self._cancelled.is_set():raise XiaoNuoError('AUDIO_CAPTURE_CANCELLED',409)
        controls=self.store.controls()
        if controls['quiet'] or (output and not controls['speech_enabled']):
            raise XiaoNuoError('SPEECH_DISABLED_BY_OWNER',409)

    def _activity(self,operation):
        execute=getattr(self.bridge,'execute',None)
        if execute and self.bridge.status().get('connected'):
            try:execute(operation,timeout=2)
            except XiaoNuoError:pass  # Optional OLED status must not break voice.

    def _stage(self,stage,request_id,transcript=None):
        progress={'stage':stage,'request_id':request_id}
        if transcript is not None:progress['transcript']=transcript[:500]
        self._progress=progress

    def _cue(self):
        controls=self.store.controls()
        if controls['volume']==0:return {'played':False}
        count=round(RATE*0.35)
        samples=array('h',(round(6000*controls['volume']*max(0,min(1,i/160,(count-1-i)/160))*math.sin(2*math.pi*700*i/RATE)) for i in range(count)))
        if sys.byteorder!='little':samples.byteswap()
        result=self.bridge.play_audio(samples.tobytes(),cancelled=self._cancelled)
        if not result.get('device_completed'):raise XiaoNuoError('SPEAKER_INCOMPLETE')
        return {'played':True,'duration_seconds':0.35,'device_completed':result.get('device_completed',False),'audible_verified':False}

    def _speak_reply(self,text,request_id,played):
        if len(text)>1000:raise XiaoNuoError('SPEAKER_REPLY_TOO_LONG',422)
        base=UUID(request_id)
        def fragment(segment,path):
            self._guard(output=True);self._activity('synthesizing')
            metadata=self.synthesize(segment,str(uuid5(base,'tts:'+path)),cancelled=self._cancelled)
            token=metadata['audio_url'].rsplit('/',1)[1]
            try:
                self._guard(output=True)
                if metadata['duration_seconds']>8:
                    if len(segment)<=1:raise XiaoNuoError('SPEAKER_AUDIO_TOO_LONG',422)
                    # Retry smaller text, never truncate PCM or omit a suffix.
                    children=split_spoken_reply(segment,max(2,len(segment)//2))
                    if len(children)==1:
                        middle=len(segment)//2;children=[segment[:middle],segment[middle:]]
                    for index,child in enumerate(children):fragment(child,path+'/'+str(index))
                    return
                controls=self.store.controls()
                pcm=device_speech_pcm(self.cache.get(token),self.root,volume=controls['volume'])
                receipt=self.bridge.play_audio(pcm,cancelled=self._cancelled)
                played.append({'text':segment,'duration_seconds':metadata['duration_seconds'],
                    'device_completed':receipt.get('device_completed',False),'audible_verified':False})
                if not receipt.get('device_completed'):raise XiaoNuoError('SPEAKER_INCOMPLETE')
                self._guard(output=True)
            finally:self.cache.discard(token)
        for index,segment in enumerate(split_spoken_reply(text)):
            fragment(segment,str(index))

    def transcribe(self,pcm,request_id):
        if not self._turn.acquire(blocking=False):raise XiaoNuoError('VOICE_BUSY',409)
        try:
            self._cancelled.clear()
            result=self.asr.transcribe(pcm)
            metadata=self.asr.status()
            self.budget.record_local(request_id,metadata['provider'],metadata['model'],'ASR',audio_seconds=result['duration_seconds'])
            if self._cancelled.is_set():raise XiaoNuoError('AUDIO_CAPTURE_CANCELLED',409)
            return result
        finally:
            self._progress={'stage':'idle'}
            self._turn.release()

    def status(self):
        device=self.bridge.status()
        return {'asr':self.asr.status(),'tts':self.tts.status(),'max_recording_seconds':MAX_SECONDS,
                'voice_profile':self.store.controls().get('voice_profile','soft'),
                'busy':self._turn.locked(),
                'progress':dict(self._progress),
                'raw_audio_persisted':False,'cloud_audio_upload':False,'automatic_listening':False,
                'on_device_wake_word':False,'amplifier_output':device.get('playback_state')=='playing',
                'device_playback_available':bool(device.get('connected') and device.get('speaker_enabled') and device.get('info',{}).get('speaker_available')),
                'device_playback_max_seconds':8,'device_playback_gain_divisor':device.get('info',{}).get('speaker_gain_divisor')}

    def play_on_device(self,token,request_id):
        if not self._turn.acquire(blocking=False):raise XiaoNuoError('VOICE_BUSY',409)
        claimed=False
        try:
            self._cancelled.clear()
            controls=self.store.controls()
            if controls['quiet'] or not controls['speech_enabled']:raise XiaoNuoError('SPEECH_DISABLED_BY_OWNER',409)
            pcm=device_speech_pcm(self.cache.get(token),self.root,volume=controls['volume'])
            self.budget.ledger.claim_request(request_id)
            claimed=True
            if self._cancelled.is_set():raise XiaoNuoError('SPEAKER_PLAYBACK_CANCELLED',409)
            result=self.bridge.play_audio(pcm,cancelled=self._cancelled)
            self.budget.ledger.finish_request(request_id,'complete')
            claimed=False
            return result
        finally:
            if claimed:self.budget.ledger.finish_request(request_id,'failed_or_uncertain')
            self._progress={'stage':'idle'}
            self._turn.release()

    def synthesize(self,text,request_id,*,cancelled=None):
        controls=self.store.controls()
        if controls['quiet'] or not controls['speech_enabled']:
            raise XiaoNuoError('SPEECH_DISABLED_BY_OWNER',409)
        generation=self.cache.generation
        data=self.tts.synthesize(text,volume=controls['volume'],speed=controls['speech_speed'],voice=controls.get('voice_profile','soft'))
        info=playback_wav_info(data)
        provider=self.tts.status()
        self.budget.record_local(request_id,provider['provider'],provider.get('model','zh-CN'),'TTS',audio_seconds=info['duration_seconds'],characters=len(text))
        if cancelled is not None and cancelled.is_set():raise XiaoNuoError('AUDIO_CAPTURE_CANCELLED',409)
        controls=self.store.controls()
        if controls['quiet'] or not controls['speech_enabled']:
            raise XiaoNuoError('SPEECH_DISABLED_BY_OWNER',409)
        token=self.cache.put(data,expected_generation=generation)
        return {'audio_url':'/api/v1/voice/audio/'+token,'expires_seconds':90,'playback':'browser',
                'cloud_upload':False,'api_cost_usd':0,'voice_profile':controls.get('voice_profile','soft'),**info}

    def synthesize_preview(self,text,request_id):
        """Explicit previews share the voice lock and respect stop/privacy clear."""
        if not self._turn.acquire(blocking=False):raise XiaoNuoError('VOICE_BUSY',409)
        claimed=False
        try:
            self.budget.ledger.claim_request(request_id);claimed=True
            self._cancelled.clear()
            result=self.synthesize(text,request_id,cancelled=self._cancelled)
            self.budget.ledger.finish_request(request_id,'complete');claimed=False
            return result
        finally:
            if claimed:self.budget.ledger.finish_request(request_id,'failed_or_uncertain')
            self._progress={'stage':'idle'}
            self._turn.release()

    def device_turn(self,seconds,request_id,conversation_id,consent,reply_playback='preview'):
        if consent!='local-only':raise XiaoNuoError('AUDIO_CONSENT_REQUIRED',422)
        if type(reply_playback) is not str or reply_playback not in {'preview','device'}:raise XiaoNuoError('INVALID_REPLY_PLAYBACK',422)
        if type(seconds) is not int or not 2<=seconds<=6:raise XiaoNuoError('INVALID_RECORDING_DURATION',422)
        try:base=UUID(str(request_id))
        except (ValueError,TypeError,AttributeError):raise XiaoNuoError('INVALID_REQUEST_ID',422) from None
        request_id=str(base)
        if reply_playback=='device':
            controls=self.store.controls()
            if controls['quiet'] or not controls['speech_enabled']:raise XiaoNuoError('SPEECH_DISABLED_BY_OWNER',409)
            status=self.bridge.status()
            if not status.get('connected'):raise XiaoNuoError('DEVICE_NOT_CONNECTED')
            if not status.get('speaker_enabled'):raise XiaoNuoError('DEVICE_SPEAKER_DISABLED',409)
            if not status.get('info',{}).get('speaker_available'):raise XiaoNuoError('DEVICE_FIRMWARE_NEEDS_UPDATE')
        if not self._turn.acquire(blocking=False):raise XiaoNuoError('VOICE_BUSY',409)
        claimed=False
        try:
            self.budget.ledger.claim_request(request_id);claimed=True
            self._cancelled.clear()
            self._guard();self._stage('listening',request_id)
            cue=self._cue() if reply_playback=='device' else {'played':False}
            self._guard()
            capture=self.bridge.capture_audio(seconds,cancelled=self._cancelled)
            pcm=capture.pop('pcm')
            try:
                self._guard();self._stage('recognizing',request_id);self._activity('recognizing')
                recognized=self.asr.transcribe(pcm)
            finally:
                del pcm
            metadata=self.asr.status()
            self.budget.record_local(str(uuid5(base,'asr')),metadata['provider'],metadata['model'],'ASR',audio_seconds=recognized['duration_seconds'])
            result={'transcript':recognized['text'],'recognition':recognized,'capture':capture,
                    'request_id':request_id,'capture_cue':cue,'reply_playback':reply_playback,
                    'audio_saved':False,'cloud_audio_upload':False,'playback_complete':False}
            self._guard();self._stage('thinking',request_id,recognized['text']);self._activity('thinking')
            try:
                answer=self.companion.chat(recognized['text'],str(uuid5(base,'llm')),conversation_id)
            except XiaoNuoError as error:
                return result|{'reply':None,'reply_error':error.code}
            result['reply']=answer
            if self._cancelled.is_set():return result|{'speech_error':'AUDIO_CAPTURE_CANCELLED'}
            if answer['speech_style']['enabled']:
                self._stage('replying',request_id,recognized['text'])
                try:
                    self._guard(output=True);self._activity('synthesizing')
                    if reply_playback=='device':
                        result['spoken_segments']=[]
                        result['playback_muted']=self.store.controls()['volume']==0
                        self._speak_reply(answer['reply_text'],request_id,result['spoken_segments'])
                        result['playback_complete']=all(part['device_completed'] for part in result['spoken_segments'])
                    else:
                        result['speech']=self.synthesize(answer['reply_text'][:500],str(uuid5(base,'tts:preview')),cancelled=self._cancelled)
                except XiaoNuoError as error:result['speech_error']=error.code
            self.budget.ledger.finish_request(request_id,'failed_or_uncertain' if result.get('speech_error') else 'complete');claimed=False
            return result
        finally:
            if claimed:self.budget.ledger.finish_request(request_id,'failed_or_uncertain')
            self._activity('activity_clear')
            self._progress={'stage':'idle'}
            self._turn.release()
