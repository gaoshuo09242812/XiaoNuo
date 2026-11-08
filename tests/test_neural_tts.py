from array import array
from io import BytesIO
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch
from uuid import uuid4
import wave

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'backend'))
from app.budgeting import UsageLedger,BudgetManager
from app.config import Settings
from app.errors import XiaoNuoError
from app.neural_tts import KokoroTTS
from app.speech import VoiceService,playback_wav_info,AudioCache
from app.storage import CompanionStore


def neural_wave():
    with BytesIO() as output:
        with wave.open(output,'wb') as wav:
            wav.setnchannels(1);wav.setsampwidth(2);wav.setframerate(24000)
            wav.writeframes(array('h',[100,-100]*24000).tobytes())
        return output.getvalue()


class NeuralTTSTests(unittest.TestCase):
    def setUp(self):
        directory=tempfile.TemporaryDirectory();self.addCleanup(directory.cleanup)
        self.root=Path(directory.name)
        self.engine=KokoroTTS(self.root)

    def install_stub(self):
        self.engine.model.mkdir(parents=True)
        (self.engine.model/'model.int8.onnx').touch()

    def test_unknown_voice_is_rejected_without_starting_worker(self):
        with patch('app.neural_tts.subprocess.run') as run:
            with self.assertRaisesRegex(XiaoNuoError,'UNKNOWN_VOICE_PROFILE'):
                self.engine.synthesize('你好',voice='arbitrary-reference-voice')
            run.assert_not_called()

    def test_missing_new_model_does_not_fall_back_to_old_voice(self):
        with patch('app.neural_tts.subprocess.run') as run:
            with self.assertRaisesRegex(XiaoNuoError,'TTS_MODEL_NOT_INSTALLED'):
                self.engine.synthesize('你好')
            run.assert_not_called()

    def test_native_sample_rate_and_fixed_voice_survive_provider_boundary(self):
        self.install_stub()
        result=subprocess.CompletedProcess([],0,stdout=neural_wave(),stderr=b'')
        with patch('app.neural_tts.subprocess.run',return_value=result) as run:
            audio=self.engine.synthesize('你好',voice='clear')
        self.assertEqual(playback_wav_info(audio)['sample_rate'],24000)
        self.assertEqual(playback_wav_info(audio)['duration_seconds'],2)
        payload=json.loads(run.call_args.kwargs['input'])
        self.assertEqual(payload['voice'],'clear')
        self.assertNotIn('reference_audio',payload)
        self.assertIsNotNone(self.engine.last_success)

    def test_worker_timeout_returns_stable_error(self):
        self.install_stub()
        with patch('app.neural_tts.subprocess.run',side_effect=subprocess.TimeoutExpired('tts',40)):
            with self.assertRaisesRegex(XiaoNuoError,'TTS_TIMEOUT'):self.engine.synthesize('你好')

    def test_overlong_generated_audio_reports_length_error_without_private_text(self):
        self.install_stub()
        result=subprocess.CompletedProcess([],1,stdout=b'',stderr=b'TTS_AUDIO_TOO_LONG\n')
        with patch('app.neural_tts.subprocess.run',return_value=result):
            with self.assertRaisesRegex(XiaoNuoError,'TTS_AUDIO_LENGTH_INVALID'):self.engine.synthesize('Public diagnostic phrase')
        self.assertEqual(self.engine.status()['last_error'],'TTS_AUDIO_LENGTH_INVALID')
        self.assertNotIn('text',self.engine.status())

    def test_24k_audio_uses_actual_duration_in_ledger_and_cache(self):
        self.install_stub()
        settings=Settings(root=self.root)
        ledger=UsageLedger(settings.database)
        store=CompanionStore(ledger)
        store.patch_controls({'voice_profile':'bright'})
        service=VoiceService(self.root,None,None,BudgetManager(ledger,settings),store,tts=self.engine)
        result=subprocess.CompletedProcess([],0,stdout=neural_wave(),stderr=b'')
        with patch('app.neural_tts.subprocess.run',return_value=result):
            response=service.synthesize('你好',str(uuid4()))
        self.assertEqual(response['voice_profile'],'bright')
        self.assertEqual(response['sample_rate'],24000)
        with ledger.connect() as db:row=db.execute('SELECT provider,audio_seconds,cost_microusd FROM api_usage').fetchone()
        self.assertEqual(row['provider'],'kokoro_onnx')
        self.assertEqual(float(row['audio_seconds']),2)
        self.assertEqual(row['cost_microusd'],0)
        self.assertEqual(CompanionStore(ledger).controls()['voice_profile'],'bright')


if __name__=='__main__':unittest.main()
