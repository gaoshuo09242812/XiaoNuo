import sys,threading,time,unittest
from pathlib import Path
from unittest.mock import patch
from app.tts_session import TtsSession
from app.neural_tts import KokoroTTS
from app.errors import XiaoNuoError
from test_neural_tts import neural_wave
import tempfile

FIXTURE=Path(__file__).with_name('fixture_tts_pipe.py')

class SessionTests(unittest.TestCase):
    def test_startup_warmup_uses_silent_public_phrase_and_retains_user_voice_settings(self):
        import json
        with tempfile.TemporaryDirectory() as directory:
            engine=KokoroTTS(Path(directory),persistent=True)
            engine.model.mkdir(parents=True);(engine.model/'model.int8.onnx').touch()
            with patch('app.tts_session.TtsSession') as factory:
                session=factory.return_value;session.request.return_value=neural_wave()
                self.assertTrue(engine.warmup());self.assertTrue(engine.warmup())
                self.assertEqual(session.request.call_count,1)
                payload=json.loads(session.request.call_args.args[0])
                self.assertEqual(payload,{'text':'你好。','volume':0.0,'speed':1.0,'voice':'soft'})
                self.assertTrue(engine.status()['warmup_completed'])
                self.assertIsInstance(engine.status()['warmup_ms'],int)
                engine.synthesize('公开测试句。',volume=.65,speed=1.1,voice='clear')
                normal=json.loads(session.request.call_args.args[0])
                self.assertEqual((normal['voice'],normal['speed'],normal['volume']),('clear',1.1,.65))
                engine.close();self.assertFalse(engine.status()['warmup_completed'])

    def test_warmup_missing_or_one_shot_model_does_not_spawn_worker(self):
        with tempfile.TemporaryDirectory() as directory,patch('app.tts_session.TtsSession') as factory:
            engine=KokoroTTS(Path(directory),persistent=True)
            self.assertFalse(engine.warmup())
            engine.model.mkdir(parents=True);(engine.model/'model.int8.onnx').touch()
            engine.persistent=False;self.assertFalse(engine.warmup())
            factory.assert_not_called()

    def test_warmup_failure_is_bounded_and_next_synthesis_can_recover(self):
        with tempfile.TemporaryDirectory() as directory:
            engine=KokoroTTS(Path(directory),persistent=True)
            engine.model.mkdir(parents=True);(engine.model/'model.int8.onnx').touch()
            with patch('app.tts_session.TtsSession') as factory:
                session=factory.return_value;session.request.side_effect=XiaoNuoError('TTS_TIMEOUT')
                self.assertFalse(engine.warmup());self.assertFalse(engine.warmup_completed)
                session.close.assert_called()
                session.request.side_effect=None;session.request.return_value=neural_wave()
                self.assertEqual(engine.synthesize('公开测试句。'),neural_wave())
            engine.close()

    def test_application_finishes_optional_warmup_before_accepting_requests(self):
        from app.config import Settings
        from app.main import create_app
        from starlette.testclient import TestClient
        with tempfile.TemporaryDirectory() as directory:
            app=create_app(Settings(root=Path(directory),tts_provider='kokoro_onnx',admin_token='test-'+40*'x'))
            engine=app.state.voice_service.tts
            with patch.object(engine,'warmup',return_value=False) as warmup:
                with TestClient(app):warmup.assert_called_once_with()

    def session(self,mode='echo',timeout=3):
        value=TtsSession([sys.executable,str(FIXTURE),mode],timeout=timeout)
        self.addCleanup(value.close);return value

    def test_two_requests_reuse_process_without_reusing_response(self):
        value=self.session();self.assertEqual(value.request(b'first'),b'first')
        pid=value._process.pid
        self.assertEqual(value.request(b'second'),b'second')
        self.assertEqual(value._process.pid,pid)

    def test_timeout_discards_worker_and_next_call_is_fresh(self):
        value=self.session('hang',.2)
        with self.assertRaisesRegex(XiaoNuoError,'TTS_TIMEOUT'):value.request(b'private-old')
        self.assertIsNone(value._process)
        value.command=[sys.executable,str(FIXTURE),'echo'];value.timeout=3
        self.assertEqual(value.request(b'fresh'),b'fresh')

    def test_crash_malformed_and_oversized_frames_fail_closed(self):
        for mode in ('crash','oversize','bad_status','partial'):
            with self.subTest(mode=mode):
                value=self.session(mode)
                with self.assertRaisesRegex(XiaoNuoError,'TTS_PROVIDER_FAILED'):value.request(b'private')
                self.assertIsNone(value._process)

    def test_length_error_retains_existing_recursive_split_signal(self):
        with self.assertRaisesRegex(XiaoNuoError,'TTS_AUDIO_LENGTH_INVALID'):
            self.session('too_long').request(b'private')

    def test_request_limit_checked_before_child_spawn(self):
        value=self.session()
        for payload in (b'',bytes(4001),'text'):
            with self.assertRaises(ValueError):value.request(payload)
        self.assertIsNone(value._process)

    def test_close_stops_child_and_reader(self):
        value=self.session();value.request(b'one');process=value._process;reader=value._thread
        value.close();value.close()
        self.assertIsNotNone(process.poll());self.assertFalse(reader.is_alive())

    def test_close_interrupts_inflight_native_wait(self):
        value=self.session('hang',10);errors=[]
        def run():
            try:value.request(b'one')
            except XiaoNuoError as error:errors.append(error.code)
        thread=threading.Thread(target=run);thread.start()
        deadline=time.monotonic()+2
        while value._process is None and time.monotonic()<deadline:time.sleep(.01)
        value.close();thread.join(3)
        self.assertFalse(thread.is_alive());self.assertTrue(errors)

    def test_persistent_provider_preserves_wav_validation_and_voice_payload(self):
        import json
        with tempfile.TemporaryDirectory() as directory:
            engine=KokoroTTS(Path(directory),persistent=True)
            engine.model.mkdir(parents=True);(engine.model/'model.int8.onnx').touch()
            with patch('app.tts_session.TtsSession') as factory:
                session=factory.return_value;session.request.return_value=neural_wave()
                self.assertEqual(engine.synthesize('你好',voice='soft'),neural_wave())
                payload=json.loads(session.request.call_args.args[0]);self.assertEqual(payload['voice'],'soft')
                session.request.return_value=b'invalid'
                with self.assertRaises(XiaoNuoError):engine.synthesize('你好')
                self.assertTrue(session.close.called)
            engine.close()

    def test_application_enables_reuse_and_closes_it_on_shutdown(self):
        from app.config import Settings
        from app.main import create_app
        from starlette.testclient import TestClient
        with tempfile.TemporaryDirectory() as directory:
            app=create_app(Settings(root=Path(directory),tts_provider='kokoro_onnx',admin_token='test-'+40*'x'))
            engine=app.state.voice_service.tts
            self.assertTrue(engine.persistent)
            with patch.object(engine,'close') as close:
                with TestClient(app):pass
                self.assertTrue(close.called)

if __name__=='__main__':unittest.main()
