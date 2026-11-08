"""Bounded pipe transport. Timeout/corruption discards the worker and its queue."""
import queue
import struct
import subprocess
import threading
import time
from .errors import XiaoNuoError

MAX_REQUEST=4000
MAX_AUDIO=1_920_044

def _read_exact(stream,count):
    chunks=[]
    while count:
        part=stream.read(count)
        if not part:raise EOFError()
        chunks.append(part);count-=len(part)
    return b''.join(chunks)

def _reader(stream,results):
    try:
        while True:
            status,size=struct.unpack('!BI',_read_exact(stream,5))
            if status not in {0,1,2} or size>MAX_AUDIO or (status and size) or (not status and not size):
                raise ValueError('Invalid frame')
            results.put_nowait((status,_read_exact(stream,size)))
    except Exception:
        try:results.put_nowait(None)
        except queue.Full:pass

class TtsSession:
    def __init__(self,command,*,env=None,timeout=40):
        self.command=command;self.env=env;self.timeout=timeout
        self._process=None;self._thread=None;self._results=None
        self._lock=threading.RLock()

    def _start(self):
        with self._lock:
            if self._process is not None and self._process.poll() is None:return self._process,self._results
            self.close()
            process=subprocess.Popen(self.command,stdin=subprocess.PIPE,stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,env=self.env,creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
            results=queue.Queue(maxsize=1)
            thread=threading.Thread(target=_reader,args=(process.stdout,results),daemon=True)
            self._process,self._results,self._thread=process,results,thread
            thread.start();return process,results

    def request(self,payload):
        if not isinstance(payload,bytes) or not 0<len(payload)<=MAX_REQUEST:raise ValueError('Invalid request size')
        started=time.monotonic()
        try:
            process,results=self._start()
            process.stdin.write(struct.pack('!I',len(payload))+payload);process.stdin.flush()
            remaining=self.timeout-(time.monotonic()-started)
            if remaining<=0:raise queue.Empty()
            result=results.get(timeout=remaining)
            if result is None:raise XiaoNuoError('TTS_PROVIDER_FAILED')
            status,audio=result
            if status==1:raise XiaoNuoError('TTS_AUDIO_LENGTH_INVALID')
            if status:raise XiaoNuoError('TTS_PROVIDER_FAILED')
            return audio
        except queue.Empty:
            self.close();raise XiaoNuoError('TTS_TIMEOUT') from None
        except XiaoNuoError:
            self.close();raise
        except Exception:
            self.close();raise XiaoNuoError('TTS_PROVIDER_FAILED') from None

    def close(self):
        with self._lock:
            process,thread=self._process,self._thread
            self._process=self._results=self._thread=None
            if process is None:return
            if process.poll() is None:
                process.terminate()
                try:process.wait(timeout=2)
                except subprocess.TimeoutExpired:process.kill();process.wait(timeout=2)
            if thread is not None:thread.join(timeout=2)
            for stream in (process.stdin,process.stdout):
                if stream is not None:stream.close()
