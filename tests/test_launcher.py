"""Launcher regressions without starting a browser, process or model."""
import importlib.util
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock,patch

root=Path(__file__).resolve().parents[1]
spec=importlib.util.spec_from_file_location('xiaonuo_manage',root/'manage.py')
manage=importlib.util.module_from_spec(spec)
spec.loader.exec_module(manage)


class LauncherTests(unittest.TestCase):
    def test_configured_worker_service_does_not_start_legacy_usb_backend(self):
        with tempfile.TemporaryDirectory() as directory,patch.object(manage,'ROOT',Path(directory)),patch.object(manage,'initialize'):
            data=Path(directory)/'data/worker-service';data.mkdir(parents=True);(data/'control.json').write_text('{}')
            with patch.object(manage,'local_request',side_effect=[manage.XiaoNuoError('LOCAL_BACKEND_NOT_RUNNING'),{}]),\
                 patch('app.worker_service.start') as start,patch.object(manage.subprocess,'Popen') as spawn:
                manage.ensure_backend();start.assert_called_once_with(Path(directory));spawn.assert_not_called()
    def test_model_start_longer_than_five_seconds_keeps_waiting(self):
        with tempfile.TemporaryDirectory() as directory:
            process=Mock(pid=12345)
            process.poll.return_value=None
            offline=manage.XiaoNuoError('LOCAL_BACKEND_NOT_RUNNING')
            with patch.object(manage,'ROOT',Path(directory)),patch.object(manage,'initialize'),\
                 patch.object(manage,'local_request',side_effect=[offline,offline,{}, {'ticket':'fake-public-test-ticket'}]),\
                 patch.object(manage.subprocess,'Popen',return_value=process),\
                 patch.object(manage.time,'sleep'),\
                 patch.object(manage.webbrowser,'open',return_value=True) as browser:
                manage.open_console()
                browser.assert_called_once()
            self.assertIn('12345',(Path(directory)/'data/local-server.json').read_text())

    def test_failed_start_does_not_open_browser(self):
        with tempfile.TemporaryDirectory() as directory:
            process=Mock(pid=12345)
            process.poll.return_value=1
            with patch.object(manage,'ROOT',Path(directory)),patch.object(manage,'initialize'),\
                 patch.object(manage,'local_request',side_effect=manage.XiaoNuoError('LOCAL_BACKEND_NOT_RUNNING')),\
                 patch.object(manage.subprocess,'Popen',return_value=process),\
                 patch.object(manage.webbrowser,'open') as browser:
                with self.assertRaisesRegex(manage.XiaoNuoError,'LOCAL_BACKEND_START_FAILED'):manage.open_console()
                browser.assert_not_called()

    def test_runtime_lock_prevents_second_owner_and_releases(self):
        with tempfile.TemporaryDirectory() as directory, patch.object(manage,'ROOT',Path(directory)):
            with manage.process_lock('backend-runtime.lock',timeout=0):
                with self.assertRaisesRegex(manage.XiaoNuoError,'LOCAL_BACKEND_BUSY'):
                    with manage.process_lock('backend-runtime.lock',timeout=0):
                        self.fail('Two backend owners acquired the lock')
            with manage.process_lock('backend-runtime.lock',timeout=0):
                pass

    def test_repeated_ready_start_does_not_spawn_or_replace_stale_pid(self):
        with tempfile.TemporaryDirectory() as directory, patch.object(manage,'ROOT',Path(directory)),\
             patch.object(manage,'initialize'), patch.object(manage,'local_request',return_value={}),\
             patch.object(manage.subprocess,'Popen') as spawn:
            data=Path(directory)/'data'
            data.mkdir()
            state=data/'local-server.json'
            state.write_text('{"pid":12345}')
            manage.ensure_backend()
            manage.ensure_backend()
            spawn.assert_not_called()
            self.assertEqual(state.read_text(),'{"pid":12345}')
