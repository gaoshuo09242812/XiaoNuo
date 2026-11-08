import importlib.util
from pathlib import Path
import unittest

spec=importlib.util.spec_from_file_location('real_reply_observer',Path(__file__).resolve().parents[1]/'tools/observe_real_reply.py')
observer=importlib.util.module_from_spec(spec);spec.loader.exec_module(observer)


class ObserverPollingTests(unittest.TestCase):
    def test_model_mode_ignores_wake_ack_and_fixed_tone(self):
        self.assertFalse(observer.is_target_reply({'synthetic':False,'source_samples':16000,'voice_probe':{}},True))
        self.assertFalse(observer.is_target_reply({'synthetic':True,'source_samples':16000,'voice_probe':{'model_characters':30}},True))
        for size in (None,True,0,-1,'30'):
            self.assertFalse(observer.is_target_reply({'source_samples':16000,'voice_probe':{'model_characters':size}},True))
        self.assertTrue(observer.is_target_reply({'synthetic':False,'source_samples':48000,'voice_probe':{'model_characters':30}},True))

    def test_legacy_mode_can_still_inspect_local_reply_and_model_failure(self):
        self.assertTrue(observer.is_target_reply({'synthetic':False,'source_samples':16000}))
        self.assertTrue(observer.is_target_reply({'source_samples':16000,'complete':False,'voice_probe':{'model_characters':30}},True))
        self.assertFalse(observer.is_target_reply({'source_samples':0,'voice_probe':{'model_characters':30}},True))

    def test_wake_readiness_can_be_checked_before_announcement(self):
        self.assertIn(b'NET_DIAG\n',observer.poll_commands(False))

    def test_known_readiness_never_repeats_blocking_dns_during_reply(self):
        for _ in range(100):
            commands=observer.poll_commands(True)
            self.assertNotIn(b'NET_DIAG',commands)
            self.assertEqual(commands.splitlines(),[b'INFO',b'LINK_STATUS'])


if __name__=='__main__':unittest.main()
