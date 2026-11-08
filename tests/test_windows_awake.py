import unittest
from unittest.mock import Mock
from app.windows_awake import keep_system_awake,CONTINUOUS,SYSTEM_REQUIRED

class AwakeTests(unittest.TestCase):
    def test_only_system_required_and_restores_previous_state(self):
        apply=Mock(side_effect=[CONTINUOUS,CONTINUOUS|SYSTEM_REQUIRED])
        with keep_system_awake(apply=apply) as active:self.assertTrue(active)
        self.assertEqual([c.args[0] for c in apply.call_args_list],[CONTINUOUS|SYSTEM_REQUIRED,CONTINUOUS])

    def test_exception_still_restores_existing_thread_request(self):
        previous=CONTINUOUS|2
        apply=Mock(side_effect=[previous,CONTINUOUS|SYSTEM_REQUIRED])
        with self.assertRaisesRegex(ValueError,'fixture'):
            with keep_system_awake(apply=apply):raise ValueError('fixture')
        self.assertEqual(apply.call_args_list[-1].args[0],previous)

    def test_failed_acquire_does_not_run_body_or_clear_unowned_state(self):
        apply=Mock(return_value=0)
        with self.assertRaises(OSError):
            with keep_system_awake(apply=apply):self.fail('must not run')
        apply.assert_called_once_with(CONTINUOUS|SYSTEM_REQUIRED)

    def test_failed_release_is_visible(self):
        apply=Mock(side_effect=[CONTINUOUS,0])
        with self.assertRaisesRegex(OSError,'restore'):
            with keep_system_awake(apply=apply):pass

    def test_other_platform_no_windows_call(self):
        with keep_system_awake(platform='posix') as active:self.assertFalse(active)
