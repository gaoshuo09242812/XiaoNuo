"""One bounded ASR subprocess. Audio and transcripts stay in memory."""
import json
from pathlib import Path
import sys


# The bundled lexicon spells the homophone "小诺". Competing names and
# [unk] give similar sounds and unrelated speech a route other than wake.
_WAKE_WORDS = (
    '小诺', '小罗', '小鹿', '小楼', '小猫', '你好', '小龙', '小刘',
    '小路', '小若', '小牛', '小花', '小林', '小王', '下楼', '洗澡',
    '小伙', '小莫', '[unk]',
)


def decode(recognizer, pcm):
    recognizer.SetWords(True)
    results = []
    for offset in range(0, len(pcm), 4000):
        if recognizer.AcceptWaveform(pcm[offset:offset+4000]):
            results.append(json.loads(recognizer.Result()))
    results.append(json.loads(recognizer.FinalResult()))
    return results


def confirm_wake_name(vosk, model, pcm, text, word_count, confidence):
    single = text in ('小糯', '小诺') and confidence is not None and .55 <= confidence < .75
    if not single and (word_count != 2 or not 4 <= len(text) <= 5):
        return False
    if not all(model.vosk_model_find_word(word) >= 0 for word in _WAKE_WORDS):
        return False
    grammar = json.dumps(_WAKE_WORDS, ensure_ascii=False)
    results = decode(vosk.KaldiRecognizer(model, 16000, grammar), pcm)
    words = [word for result in results for word in result.get('result', [])]
    if len(words) != (1 if single else 2):
        return False
    if any(word['word'] != '小诺' or not .24 <= word['end'] - word['start'] <= .95 for word in words):
        return False
    scores = [word['conf'] for word in words]
    return scores[0] >= .80 if single else min(scores) >= .65 and max(scores) >= .86


def main():
    import vosk
    vosk.SetLogLevel(-1)
    model_path = Path(sys.argv[1])
    model = vosk.Model(str(model_path))
    pcm = sys.stdin.buffer.read(192001)
    if not pcm or len(pcm)>192000 or len(pcm)%2:
        raise ValueError('Invalid audio length')
    results = decode(vosk.KaldiRecognizer(model, 16000), pcm)
    text=''.join(part.get('text','').replace(' ','') for part in results).strip()
    words=[word for part in results for word in part.get('result',[])]
    confidence=sum(word.get('conf',0) for word in words)/len(words) if words else None
    wake = {}
    if sys.argv[2:] == ['--confirm-wake'] and (model_path/'graph/HCLr.fst').is_file():
        try:
            wake['wake_name_confirmed'] = confirm_wake_name(vosk, model, pcm, text, len(words), confidence)
        except Exception:
            # A failed optional pass leaves ordinary ASR available.
            pass
    print(json.dumps({'text':text[:2000],'confidence':confidence,'language':'zh',
                      **wake,
                      'words':[{'word':word.get('word',''),'confidence':word.get('conf',0),
                                'start':word.get('start',0),'end':word.get('end',0)} for word in words[:250]],
                      'endpointing':'vosk','duration_seconds':len(pcm)/32000},ensure_ascii=False))


if __name__=='__main__':
    try:
        main()
    except Exception:
        sys.stderr.write('LOCAL_ASR_FAILED\n')
        raise SystemExit(1)
