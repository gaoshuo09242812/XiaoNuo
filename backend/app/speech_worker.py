"""One bounded ASR subprocess. Audio and transcripts stay in memory."""
import json
from pathlib import Path
import sys


def main():
    import vosk
    vosk.SetLogLevel(-1)
    model=vosk.Model(str(Path(sys.argv[1])))
    recognizer=vosk.KaldiRecognizer(model,16000)
    recognizer.SetWords(True)
    pcm=sys.stdin.buffer.read(192001)
    if not pcm or len(pcm)>192000 or len(pcm)%2:
        raise ValueError('Invalid audio length')
    results=[]
    for offset in range(0,len(pcm),4000):
        if recognizer.AcceptWaveform(pcm[offset:offset+4000]):
            results.append(json.loads(recognizer.Result()))
    results.append(json.loads(recognizer.FinalResult()))
    text=''.join(part.get('text','').replace(' ','') for part in results).strip()
    words=[word for part in results for word in part.get('result',[])]
    confidence=sum(word.get('conf',0) for word in words)/len(words) if words else None
    print(json.dumps({'text':text[:2000],'confidence':confidence,'language':'zh',
                      'words':[{'word':word.get('word',''),'confidence':word.get('conf',0),
                                'start':word.get('start',0),'end':word.get('end',0)} for word in words[:250]],
                      'endpointing':'vosk','duration_seconds':len(pcm)/32000},ensure_ascii=False))


if __name__=='__main__':
    try:
        main()
    except Exception:
        sys.stderr.write('LOCAL_ASR_FAILED\n')
        raise SystemExit(1)
