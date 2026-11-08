import importlib.util
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock

spec=importlib.util.spec_from_file_location('child_recovery_probe',Path(__file__).resolve().parents[1]/'tools/probe_child_recovery.py')
probe=importlib.util.module_from_spec(spec);spec.loader.exec_module(probe)


class ChildRecoveryGuardTests(unittest.TestCase):
    def setUp(self):
        temporary=tempfile.TemporaryDirectory();self.addCleanup(temporary.cleanup)
        self.root=Path(temporary.name);self.directory=self.root/'data/worker-service';self.directory.mkdir(parents=True)
        self.old={'run_id':'a'*32,'child_pid':201,'supervisor_pid':101,'restarts':0,'state':'RUNNING'}
        self.write(self.old)
        self.service=Mock();self.service.running.return_value=True;self.service.enabled.return_value=True;self.service.health.return_value=True
    def write(self,state):
        (self.directory/'status.json').write_text(json.dumps(state))
    def test_only_current_healthy_owned_run_gets_a_stop_marker(self):
        path=probe.request_owned_child_exit(self.service,self.root,self.old)
        self.assertEqual(path,self.directory/('a'*32+'.stop'))
        self.service.save.assert_called_once_with(path,{'stop':True})
        self.service.health.assert_called_once_with(self.root,'a'*32)
    def test_malformed_or_path_like_run_ids_never_write(self):
        for run in ('../other','A'*32,'a'*31,None):
            with self.subTest(run=run),self.assertRaises(RuntimeError):
                probe.request_owned_child_exit(self.service,self.root,self.old|{'run_id':run})
        self.service.save.assert_not_called()
    def test_stale_process_state_never_writes(self):
        self.write(self.old|{'child_pid':202})
        with self.assertRaises(RuntimeError):probe.request_owned_child_exit(self.service,self.root,self.old)
        self.service.save.assert_not_called()
    def test_health_failure_and_race_during_health_never_write(self):
        self.service.health.return_value=False
        with self.assertRaises(RuntimeError):probe.request_owned_child_exit(self.service,self.root,self.old)
        def race(*args):self.write(self.old|{'run_id':'b'*32});return True
        self.service.health.side_effect=race
        with self.assertRaises(RuntimeError):probe.request_owned_child_exit(self.service,self.root,self.old)
        self.service.save.assert_not_called()
    def test_existing_stop_marker_does_not_get_overwritten(self):
        marker=self.directory/('a'*32+'.stop');marker.write_text('existing')
        with self.assertRaises(RuntimeError):probe.request_owned_child_exit(self.service,self.root,self.old)
        self.assertEqual(marker.read_text(),'existing');self.service.save.assert_not_called()
    def test_manual_supervisor_replacement_is_not_automatic_child_recovery(self):
        new=self.old|{'run_id':'b'*32,'child_pid':202,'restarts':1}
        self.assertTrue(probe.replacement_matches(self.old,new))
        for altered in ({'supervisor_pid':102},{'child_pid':201},{'run_id':'a'*32},{'restarts':0},{'state':'STARTING'}):
            with self.subTest(altered=altered):self.assertFalse(probe.replacement_matches(self.old,new|altered))


if __name__=='__main__':unittest.main()
