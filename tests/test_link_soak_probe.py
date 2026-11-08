import importlib.util
from pathlib import Path
import unittest

spec=importlib.util.spec_from_file_location('link_soak_probe',Path(__file__).resolve().parents[1]/'tools/probe_link_soak.py')
probe=importlib.util.module_from_spec(spec);spec.loader.exec_module(probe)


class LinkObservationTests(unittest.TestCase):
    def status(self,uptime=10000,**values):
        return dict(event='status',device_id='private',binding_id='private',saved_ssid='private',
                    oled_driver='ssd1306',firmware='0.6.64-buffered-reply',speaker_gain_divisor=2,
                    quiet=False,wifi_connected=True,computer_wake_enabled=True,
                    uptime_ms=uptime,free_heap=60000,**values)

    def network(self,pongs=10,ready=True):
        return dict(event='network_status',pong_count=pongs,ready=ready,turn_active=True)

    def started(self):
        item=probe.LinkObservation();item.accept(self.status(),0);item.accept(self.network(),0)
        return item

    def test_fresh_status_without_fresh_heartbeats_cannot_pass(self):
        item=self.started();item.accept(self.status(50000),40);item.accept(self.network(),40)
        with self.assertRaisesRegex(RuntimeError,'HEARTBEAT_STALLED'):item.finish(40,30)

    def test_silent_device_cannot_pass_from_old_ready(self):
        with self.assertRaisesRegex(RuntimeError,'STATUS_STALE'):self.started().finish(60,60)

    def test_reboot_and_link_loss_and_counter_reset_are_failures(self):
        for message in (self.status(10),self.network(9),self.network(11,False),dict(event='network_disconnect')):
            with self.subTest(message=message),self.assertRaises(RuntimeError):self.started().accept(message,1)

    def test_configuration_changes_are_not_accepted(self):
        message=self.status(20000);message['binding_id']='replacement'
        with self.assertRaisesRegex(RuntimeError,'CONFIGURATION_CHANGED'):self.started().accept(message,1)

    def test_complete_window_requires_duration_and_new_pongs(self):
        item=self.started();item.accept(self.status(70000),60);item.accept(self.network(13),60)
        item.finish(60,60)
        with self.assertRaisesRegex(RuntimeError,'INCOMPLETE'):item.finish(60,61)
        self.assertEqual(item.summary()['new_pongs'],3)
        self.assertNotIn('private',str(item.summary()))

    def test_missing_or_malformed_messages_do_not_count_as_status(self):
        item=probe.LinkObservation()
        for message in (None,[],{},dict(event='other')):item.accept(message,0)
        with self.assertRaisesRegex(RuntimeError,'STATUS_MISSING'):item.check_fresh(0)


if __name__=='__main__':unittest.main()
