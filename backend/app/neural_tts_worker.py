"""Isolated CPU synthesis using only fixed, stock speaker IDs."""
from io import BytesIO
import json
from pathlib import Path
import sys
import wave

class SpeechWorkerError(Exception):pass

def create_engine(model):
    import sherpa_onnx
    model=Path(model)
    config=sherpa_onnx.OfflineTtsConfig(
        model=sherpa_onnx.OfflineTtsModelConfig(
            kokoro=sherpa_onnx.OfflineTtsKokoroModelConfig(
                model=str(model/'model.int8.onnx'),voices=str(model/'voices.bin'),
                tokens=str(model/'tokens.txt'),data_dir=str(model/'espeak-ng-data'),
                dict_dir=str(model/'dict'),
                lexicon=str(model/'lexicon-us-en.txt')+','+str(model/'lexicon-zh.txt')),
            num_threads=4,debug=False,provider='cpu'),
        rule_fsts=','.join(str(model/name) for name in ['date-zh.fst','number-zh.fst','phone-zh.fst']),
        max_num_sentences=1)
    if not config.validate():raise ValueError('Invalid model configuration')
    return sherpa_onnx.OfflineTts(config)

def synthesize_payload(engine,payload):
    import numpy as np
    from .voice_profiles import VOICE_PROFILES
    text=payload['text']
    if not isinstance(text,str) or not 1<=len(text.strip())<=500:raise ValueError('Invalid text')
    profile=VOICE_PROFILES[payload['voice']]
    volume=float(payload['volume']);speed=float(payload['speed'])
    if not 0<=volume<=1 or not 0.7<=speed<=1.4:raise ValueError('Invalid style')
    audio=engine.generate(text=text,sid=profile['speaker_id'],speed=speed*profile['speed_factor'])
    samples=np.asarray(audio.samples,dtype=np.float32)
    if len(samples)>24000*40:raise SpeechWorkerError('TTS_AUDIO_TOO_LONG')
    if audio.sample_rate!=24000 or not len(samples) or not np.isfinite(samples).all():
        raise SpeechWorkerError('TTS_INVALID_AUDIO')
    pcm=(np.clip(samples*volume,-1,1)*32767).astype('<i2').tobytes()
    with BytesIO() as output:
        with wave.open(output,'wb') as wav:
            wav.setnchannels(1);wav.setsampwidth(2);wav.setframerate(24000);wav.writeframes(pcm)
        return output.getvalue()

def main():
    payload=json.loads(sys.stdin.buffer.read(16385))
    sys.stdout.buffer.write(synthesize_payload(create_engine(Path(sys.argv[1])),payload))

if __name__=='__main__':
    try:main()
    except SpeechWorkerError as error:
        sys.stderr.write(str(error)+'\n')
        raise SystemExit(1)
    except Exception:
        sys.stderr.write('LOCAL_NEURAL_TTS_FAILED\n')
        raise SystemExit(1)
