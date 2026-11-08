import importlib.util
from pathlib import Path
import unittest
from unittest.mock import Mock

spec=importlib.util.spec_from_file_location('host_reconnect_probe',Path(__file__).resolve().parents[1]/'tools/probe_host_reconnect.py')
probe=importlib.util.module_from_spec(spec);spec.loader.exec_module(probe)


class ReconnectCleanupTests(unittest.TestCase):
    def test_host_is_restarted_when_outage_body_fails(self):
        service=Mock();service.stop.return_value={'stopped':True}
        with self.assertRaisesRegex(ValueError,'fixture'):
            with probe.host_outage(service,Path('fixture')):raise ValueError('fixture')
        service.start.assert_called_once_with(Path('fixture'))
    def test_failed_stop_still_reenables_the_host(self):
        service=Mock();service.stop.return_value={'stopped':False}
        with self.assertRaisesRegex(RuntimeError,'HOST_STOP_NOT_CONFIRMED'):
            with probe.host_outage(service,Path('fixture')):self.fail('Must not start outage')
        service.start.assert_called_once()
    def test_stop_exception_and_keyboard_interrupt_still_restore_host(self):
        for error in (OSError('fixture'),KeyboardInterrupt()):
            service=Mock();service.stop.side_effect=error
            with self.subTest(error=type(error).__name__),self.assertRaises(type(error)):
                with probe.host_outage(service,Path('fixture')):self.fail('Must not start outage')
            service.start.assert_called_once()
    def test_wake_is_restored_if_link_observation_fails(self):
        port=Mock()
        with self.assertRaisesRegex(RuntimeError,'fixture'):
            with probe.pause_wake(port):raise RuntimeError('fixture')
        self.assertEqual([call.args[0] for call in port.write.call_args_list],[b'NET_WAKE OFF\n',b'NET_WAKE ON\n'])
