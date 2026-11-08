from contextlib import redirect_stdout
import importlib.util
from io import StringIO
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

ROOT=Path(__file__).resolve().parents[1]
spec=importlib.util.spec_from_file_location('wss_probe_startup',ROOT/'tools/probe_wss_reply.py')
probe=importlib.util.module_from_spec(spec);spec.loader.exec_module(probe)

class Port:
    def __init__(self,messages):self.messages=list(messages);self.writes=[];self.is_open=False
    def open(self):self.is_open=True
    def close(self):self.is_open=False
    def flush(self):pass
    def reset_input_buffer(self):pass
    def write(self,raw):self.writes.append(raw)
    def readline(self):
        if not self.messages:raise AssertionError('Probe consumed all fixture events without finishing')
        return json.dumps(self.messages.pop(0)).encode()+b'\n'

READY={'event':'network_status','ready':True,'turn_active':False,'state':'READY'}
UNKNOWN={'event':'error','code':'UNKNOWN_OR_DISABLED_COMMAND'}
REQUESTED={'event':'reply_probe_requested'}
END={'event':'play_end','samples':128000}

class ProbeStartupTests(unittest.TestCase):
    def run_probe(self,messages):
        port=Port(messages)
        with tempfile.TemporaryDirectory() as tmp,patch.object(probe,'ROOT',Path(tmp)),patch('serial.Serial',return_value=port),patch('sys.argv',['probe','--seconds','8','--port','COM3']),redirect_stdout(StringIO()):
            (Path(tmp)/'data').mkdir();result=probe.main()
            records=list((Path(tmp)/'data').glob('wss-*.json'))
            self.assertEqual(len(records),1);record=json.loads(records[0].read_text())
        return port,result,record

    def test_leftover_command_error_before_ready_does_not_abort_or_duplicate_test(self):
        port,result,record=self.run_probe([UNKNOWN,READY,REQUESTED,END,READY])
        self.assertEqual(result,0);self.assertEqual(port.writes.count(b'NET_REPLY_TEST 8\n'),1)
        self.assertEqual(port.writes[0],b'\n');self.assertTrue(record['device_completed'])
        self.assertFalse(port.is_open)

    def test_error_after_test_was_sent_still_fails(self):
        port,result,record=self.run_probe([READY,REQUESTED,UNKNOWN])
        self.assertEqual(result,1);self.assertFalse(record['device_completed'])
        self.assertIn(b'NET_CANCEL\n',port.writes)

    def test_startup_error_allowance_is_bounded_and_other_faults_fail(self):
        for messages in ([UNKNOWN]*3,[{'event':'error','code':'REPLY_QUEUE_FULL'}],[READY,UNKNOWN]):
            with self.subTest(messages=messages):
                _,result,record=self.run_probe(messages)
                self.assertEqual(result,1);self.assertFalse(record['device_completed'])
