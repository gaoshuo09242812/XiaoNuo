"""Offline neural speech with bounded subprocesses and stock voices."""
import os
from pathlib import Path
import json
import subprocess
import sys
import threading
import time

from .errors import XiaoNuoError
from .speech import TTSProvider,playback_wav_info
from .voice_profiles import VOICE_PROFILES


class KokoroTTS(TTSProvider):
    def __init__(self,root: Path,*,persistent=False):
        self.root=root
        self.model=root/'data/models/kokoro-int8-multi-lang-v1_1'
        self.last_success=None
        self.last_error=None
        self.last_exit_code=None
        self._busy=threading.Lock()
        self.persistent=persistent
        self._session=None
        self.warmup_completed=False
        self.warmup_ms=None

    def close(self):
        self.warmup_completed=False
        if self._session is not None:self._session.close()

    def warmup(self):
        """Prepare the private model process before requests, discard silent PCM.

        A fixed public phrase also initializes the Chinese frontend. This never
        reads user text, alters preferences, records usage or plays audio.
        Failure leaves normal synthesis free to start a fresh worker later.
        """
        if not self.persistent or not (self.model/'model.int8.onnx').is_file():return False
        if self.warmup_completed:return True
        started=time.perf_counter()
        try:
            self.synthesize('你好。',volume=0.0,speed=1.0,voice='soft')
            self.warmup_completed=True
            return True
        except XiaoNuoError:
            return False
        finally:self.warmup_ms=round((time.perf_counter()-started)*1000)

    def status(self):
        return {'provider':'kokoro_onnx','model':'kokoro-int8-v1.1-zh',
                'model_present':(self.model/'model.int8.onnx').is_file(),
                'last_success':self.last_success,'last_error':self.last_error,'last_exit_code':self.last_exit_code,'cloud_upload':False,'speaker':'browser_only',
                'sample_rate':24000,'warmup_completed':self.warmup_completed,'warmup_ms':self.warmup_ms,
                'voices':[{'id':key,'label':value['label']} for key,value in VOICE_PROFILES.items()]}

    def synthesize(self,text: str,*,volume: float=0.5,speed: float=1.0,voice: str='soft') -> bytes:
        if voice not in VOICE_PROFILES:raise XiaoNuoError('UNKNOWN_VOICE_PROFILE',422)
        if not isinstance(text,str) or not 1<=len(text.strip())<=500:raise XiaoNuoError('INVALID_TTS_TEXT',422)
        if not 0<=volume<=1 or not 0.7<=speed<=1.4:raise XiaoNuoError('INVALID_SPEECH_STYLE',422)
        if not (self.model/'model.int8.onnx').is_file():raise XiaoNuoError('TTS_MODEL_NOT_INSTALLED')
        if not self._busy.acquire(blocking=False):raise XiaoNuoError('TTS_BUSY',409)
        env=dict(os.environ)
        env['PYTHONPATH']=os.pathsep.join([str(self.root/'backend'),str(self.root/'.runtime-libs')])
        env['PYTHONIOENCODING']='utf-8'
        try:
            payload=json.dumps({'text':text,'volume':volume,'speed':speed,'voice':voice},ensure_ascii=False).encode('utf-8')
            if self.persistent:
                from .tts_session import TtsSession
                if self._session is None:
                    self._session=TtsSession([sys.executable,'-m','app.neural_tts_session_worker',str(self.model)],env=env)
                audio=self._session.request(payload)
                self.last_exit_code=0
            else:
                try:
                    result=subprocess.run([sys.executable,'-m','app.neural_tts_worker',str(self.model)],
                                      input=payload,capture_output=True,env=env,timeout=40,
                                      creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
                except subprocess.TimeoutExpired:raise XiaoNuoError('TTS_TIMEOUT') from None
                self.last_exit_code=result.returncode
                if result.returncode:
                    code='TTS_AUDIO_LENGTH_INVALID' if result.stderr.rstrip().endswith(b'TTS_AUDIO_TOO_LONG') else 'TTS_PROVIDER_FAILED'
                    raise XiaoNuoError(code)
                audio=result.stdout
            if len(audio)>1_920_044:raise XiaoNuoError('TTS_AUDIO_LENGTH_INVALID')
            playback_wav_info(audio)
            self.last_success=time.time()
            self.last_error=None
            return audio
        except XiaoNuoError as error:
            self.close()
            self.last_error=error.code
            raise
        finally:self._busy.release()
