import copy
from datetime import datetime, timedelta, timezone
from io import BytesIO
import importlib.util
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch
from urllib.error import HTTPError
from urllib.request import Request

from app.delivery_status import render, summarize

SPEC = importlib.util.spec_from_file_location('delivery_check_tool', Path(__file__).resolve().parents[1] / 'tools/check_delivery_status.py')
tool = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(tool)


class DeliveryStatusTests(unittest.TestCase):
    def setUp(self):
        self.now = datetime(2026, 11, 6, 12, tzinfo=timezone.utc)
        self.payloads = {
            'status': {'network_audio_enabled': True, 'relay_states': ['DEVICE_CONNECTED'],
                       'network_chat_provider': 'deepseek', 'remote_enabled': True, 'allow_paid': True,
                       'providers': [{'provider': 'deepseek', 'credential_configured': True, 'block_reason': None}]},
            'devices': {'network_client_enabled': False, 'items': [
                {'id': 'PRIVATE-ID', 'hardware_id': 'PRIVATE-HARDWARE', 'name': 'PRIVATE-NAME',
                 'credential_present': True, 'revoked_at': None, 'provisioning_state': 'bound',
                 'state': 'NETWORK_IDLE', 'last_seen': self.now.isoformat(),
                 'firmware': '0.6.72-reminder-pull', 'network_client_enabled': False}]},
            'voice': {'asr': {'model_present': True}, 'tts': {'model_present': True, 'warmup_completed': True},
                      'device_playback_available': False},
            'preferences': {'computer_wake_enabled': True, 'speech_enabled': True, 'quiet': False, 'volume': 1.0},
            'reminders': {'device_audio_capable': True, 'items': [{'text': 'PRIVATE-REMINDER'}]},
        }
        self.service = dict(running=True, enabled=True, healthy=True, keep_awake_active=True,
                            billing={'ready':True,'code':'BILLING_READY'})

    def report(self):
        return summarize(self.payloads, self.service, now=self.now)

    def check(self, name):
        return next(c for c in self.report()['checks'] if c['check'] == name)

    def test_charger_and_legacy_usb_flags_do_not_require_serial(self):
        self.assertTrue(self.report()['all_checks_pass'])
        self.assertFalse(self.report()['live_chat_tested'])
        self.assertFalse(self.report()['physical_sound_tested'])

    def test_stale_future_naive_and_invalid_heartbeats_fail_closed(self):
        device = self.payloads['devices']['items'][0]
        for stamp in [(self.now - timedelta(seconds=31)).isoformat(),
                      (self.now + timedelta(seconds=1)).isoformat(), '2026-11-06T12:00:00', None, {}, 'bad']:
            with self.subTest(stamp=stamp):
                device['last_seen'] = stamp
                self.assertEqual(self.check('device')['level'], 'WARN')
        device['last_seen'] = (self.now - timedelta(seconds=30)).isoformat()
        self.assertEqual(self.check('device')['level'], 'PASS')

    def test_revoked_unbound_unauthenticated_offline_devices_rejected(self):
        original = copy.deepcopy(self.payloads['devices']['items'][0])
        for field, value in [('revoked_at', 'revoked'), ('provisioning_state', 'pending'),
                             ('credential_present', False), ('state', 'DISCONNECTED')]:
            self.payloads['devices']['items'][0] = {**original, field: value}
            self.assertEqual(self.check('device')['level'], 'WARN')
        del self.payloads['devices']['items'][0]['revoked_at']
        self.assertEqual(self.check('device')['level'], 'WARN')

    def test_stale_running_file_is_not_health(self):
        for key in ('running', 'enabled', 'healthy'):
            with self.subTest(key=key):
                self.service[key] = False
                self.assertEqual(self.check('backend')['level'], 'FAIL')
                self.assertEqual(self.check('device')['level'], 'WARN')
                self.service[key] = True

    def test_relay_must_be_connected_and_firmware_is_whitelisted(self):
        self.payloads['status']['relay_states'] = ['WAITING_FOR_DEVICE']
        self.assertEqual(self.check('device')['level'], 'WARN')
        self.payloads['status']['relay_states'] = ['DEVICE_CONNECTED']
        self.payloads['devices']['items'][0]['firmware'] = 'PRIVATE-UNTRUSTED-FIRMWARE'
        self.assertEqual(self.check('firmware')['level'], 'WARN')
        self.assertNotIn('PRIVATE', json.dumps(self.report()))

    def test_live_checked_073_is_known_but_unknown_or_missing_version_is_not(self):
        device=self.payloads['devices']['items'][0]
        device['firmware']='0.6.73-expression'
        self.assertEqual(self.check('firmware')['code'],'FIRMWARE_073')
        self.assertEqual(self.check('firmware')['level'],'PASS')
        device['firmware']=None
        self.assertEqual(self.check('firmware')['level'],'WARN')

    def test_quiet_off_wake_or_muted_are_controls_warnings(self):
        original = dict(self.payloads['preferences'])
        for key, value in [('computer_wake_enabled', False), ('quiet', True), ('speech_enabled', False), ('volume', 0)]:
            self.payloads['preferences'] = {**original, key: value}
            self.assertEqual(self.check('reminder')['level'], 'WARN')
            self.assertEqual(self.check('device')['level'], 'PASS')

    def test_bad_volume_does_not_crash_or_enable_speech(self):
        for volume in (None, True, '1', float('nan'), float('inf'), -1, 2, 10**1000):
            self.payloads['preferences']['volume'] = volume
            self.assertEqual(self.check('speech_controls')['level'], 'WARN')

    def test_model_presence_not_past_success_or_usb_status(self):
        self.payloads['voice']['tts'] = {'last_success': 'earlier', 'warmup_completed': True}
        self.assertEqual(self.check('voice_models')['level'], 'WARN')

    def test_provider_policy_block_not_reported_ready_or_echoed(self):
        self.payloads['status']['providers'][0]['block_reason'] = 'PRIVATE-POLICY-DETAIL'
        self.assertEqual(self.check('chat_policy')['level'], 'WARN')
        self.assertNotIn('PRIVATE', render(self.report()))
        self.payloads['status']['providers'] = []
        self.assertEqual(self.check('chat_policy')['level'], 'WARN')

    def test_native_budget_accounting_block_is_not_reported_ready(self):
        self.payloads['status']['budget']={'currency':'CNY','paid_block_reason':'CNY_HISTORY_REVIEW_REQUIRED'}
        self.assertEqual(self.check('chat_policy')['level'],'WARN')

    def test_missing_malformed_payloads_do_not_pass(self):
        for value in (None, [], {}, {'status': [], 'devices': {'items': [None, [], {}]}, 'voice': {'asr': []}}):
            result = summarize(value, {}, now=self.now)
            self.assertFalse(result['all_checks_pass'])
            self.assertEqual(result['checks'][0]['level'], 'FAIL')

    def test_report_only_contains_whitelisted_data_and_does_not_mutate_inputs(self):
        before = copy.deepcopy(self.payloads)
        self.payloads['status']['admin_token'] = 'PRIVATE-TOKEN'
        self.payloads['preferences']['quiet_reason'] = 'PRIVATE-NOTE'
        report = self.report()
        self.assertNotIn('PRIVATE', json.dumps(report) + render(report))
        del self.payloads['status']['admin_token']
        del self.payloads['preferences']['quiet_reason']
        self.assertEqual(before, self.payloads)


class DeliveryCollectorTests(unittest.TestCase):
    def test_fetch_uses_only_allowlisted_loopback_get_and_bounded_read(self):
        opener = Mock()
        opener.open.return_value = BytesIO(b'{"ok":true}')
        self.assertEqual(tool.fetch(opener, 'PRIVATE-TOKEN', 'status'), {'ok': True})
        request = opener.open.call_args.args[0]
        self.assertEqual(request.full_url, 'http://127.0.0.1:8765/api/v1/status')
        self.assertEqual(request.get_method(), 'GET')
        self.assertIsNone(request.data)
        with self.assertRaises(ValueError):
            tool.fetch(opener, 'PRIVATE-TOKEN', 'chat')
        self.assertEqual(opener.open.call_count, 1)

    def test_redirect_is_refused_without_forwarding_authorization(self):
        with self.assertRaises(HTTPError) as raised:
            tool.NoRedirect().redirect_request(Request('http://127.0.0.1:8765'), None, 302,
                                                'moved', {}, 'https://foreign.invalid')
        raised.exception.close()

    def test_oversize_and_nonobject_response_rejected(self):
        for content in (b'x' * (tool.MAX_BODY + 1), b'[]', b'bad'):
            opener = Mock()
            opener.open.return_value = BytesIO(content)
            with self.assertRaises(ValueError):
                tool.fetch(opener, 'PRIVATE-TOKEN', 'status')

    def test_foreign_run_or_unlocked_old_running_file_cannot_pass(self):
        from app import worker_service as service
        for locked, api_run, health in [(False, 'ours', True), (True, 'other', True), (True, 'ours', False)]:
            with tempfile.TemporaryDirectory() as folder:
                root = Path(folder)
                directory = root / 'data/worker-service'
                directory.mkdir(parents=True)
                (directory / 'supervisor.lock').touch()
                (directory / 'status.json').write_text(json.dumps({'state': 'RUNNING', 'run_id': 'ours', 'keep_awake_active': True}))
                with patch.object(service, 'running', return_value=locked), patch.object(service, 'enabled', return_value=True), \
                        patch.object(service, 'health', return_value=health), \
                        patch('app.config.load_settings', return_value=Mock(admin_token='PRIVATE-TOKEN')), \
                        patch.object(tool, 'fetch', return_value={'worker_run_id': api_run, 'network_audio_enabled': True}):
                    self.assertEqual(tool.collect(root)['checks'][0]['level'], 'FAIL')

    def test_errors_are_not_echoed_or_saved_and_routes_are_get_only(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            with patch('app.config.load_settings', return_value=Mock(admin_token='PRIVATE-TOKEN')), \
                    patch.object(tool, 'fetch', side_effect=RuntimeError('PRIVATE-EXCEPTION')) as fetch:
                report = tool.collect(root)
            self.assertEqual([call.args[2] for call in fetch.call_args_list], list(tool.ROUTES))
            self.assertNotIn('PRIVATE', json.dumps(report) + render(report))
            self.assertEqual(list(root.iterdir()), [])


if __name__ == '__main__':
    unittest.main()
