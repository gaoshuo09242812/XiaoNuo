import importlib.util
from pathlib import Path
import unittest

spec=importlib.util.spec_from_file_location('device_restart_probe',Path(__file__).resolve().parents[1]/'tools/probe_device_restart.py')
probe=importlib.util.module_from_spec(spec);spec.loader.exec_module(probe)


class RestartAcceptanceTests(unittest.TestCase):
    def setUp(self):
        self.before={'device_id':'private-board','binding_id':'private-binding','saved_ssid':'private-network',
                     'oled_driver':'ssd1306','firmware':'0.6.64-buffered-reply','speaker_gain_divisor':2,
                     'quiet':False,'uptime_ms':90000}
    def test_configuration_fingerprint_excludes_transient_link_state_and_raw_values(self):
        value=probe.configuration_fingerprint(self.before)
        self.assertEqual(len(value),64);self.assertNotIn('private',value)
        self.assertEqual(value,probe.configuration_fingerprint(self.before|{'wifi_connected':False,'computer_wake_enabled':False}))
    def test_actual_uptime_restart_with_same_configuration_is_required(self):
        probe.validate_reboot(self.before,self.before|{'uptime_ms':1000})
        for uptime in (90000,91000,-1,True):
            with self.subTest(uptime=uptime),self.assertRaises(RuntimeError):probe.validate_reboot(self.before,self.before|{'uptime_ms':uptime})
    def test_repair_or_repairing_is_not_allowed_to_mask_lost_identity(self):
        for changed in ({'device_id':'other'},{'binding_id':'other'},{'saved_ssid':'other'},
                        {'speaker_gain_divisor':4},{'oled_driver':'sh1106'},{'quiet':True}):
            with self.subTest(changed=changed),self.assertRaises(RuntimeError):
                probe.validate_reboot(self.before,self.before|changed|{'uptime_ms':1000})
    def test_missing_identity_or_unknown_firmware_is_rejected_before_reset(self):
        for changed in ({'binding_id':''},{'saved_ssid':None},{'firmware':'unknown'},{'speaker_gain_divisor':True}):
            with self.subTest(changed=changed),self.assertRaises(RuntimeError):probe.configuration_fingerprint(self.before|changed)


if __name__=='__main__':unittest.main()
