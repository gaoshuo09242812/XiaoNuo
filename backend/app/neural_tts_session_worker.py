"""Sequential private pipe requests; reuse model weights, never prior audio."""
import json
from pathlib import Path
import struct
import sys
from .neural_tts_worker import create_engine,synthesize_payload,SpeechWorkerError

MAX_REQUEST=4000
MAX_AUDIO=1_920_044

def read_exact(stream,count):
    chunks=[]
    while count:
        part=stream.read(count)
        if not part:raise EOFError()
        chunks.append(part);count-=len(part)
    return b''.join(chunks)

def main():
    engine=None
    while True:
        header=sys.stdin.buffer.read(4)
        if not header:return
        if len(header)!=4:raise ValueError('Invalid frame')
        size=struct.unpack('!I',header)[0]
        if not 0<size<=MAX_REQUEST:raise ValueError('Invalid frame')
        raw=read_exact(sys.stdin.buffer,size)
        payload=None;audio=b'';status=2
        try:
            payload=json.loads(raw)
            if engine is None:engine=create_engine(Path(sys.argv[1]))
            audio=synthesize_payload(engine,payload)
            if len(audio)>MAX_AUDIO:raise SpeechWorkerError('TTS_AUDIO_TOO_LONG')
            status=0
        except SpeechWorkerError as error:
            status=1 if str(error)=='TTS_AUDIO_TOO_LONG' else 2
            audio=b''
        except Exception:
            status=2;audio=b''
        finally:
            # Request/response variables are cleared between calls. The model
            # has no conversation history; no text or audio is written to disk.
            raw=b'';payload=None
        sys.stdout.buffer.write(struct.pack('!BI',status,len(audio)))
        sys.stdout.buffer.write(audio);sys.stdout.buffer.flush()
        audio=b''

if __name__=='__main__':
    try:main()
    except Exception:raise SystemExit(1)
