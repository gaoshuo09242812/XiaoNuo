"""Model selection checks with no network, model download or owner configuration."""
from io import BytesIO
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch,Mock

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'backend'))
from app.config import Settings,ProviderSpec,load_settings
from app.local_runtime import LocalModelRuntime


class LocalRuntimeTests(unittest.TestCase):
    def test_model_choice_does_not_enable_remote_or_paid(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory)
            (root/'data').mkdir()
            (root/'data/local-llm.key').write_text('fake-local-test-key',encoding='ascii')
            env=root/'.env.local'
            env.write_text('XIAONUO_LOCAL_MODEL=Qwen3-1.7B-Q8_0\nXIAONUO_LOCAL_LLM_ENABLED=true\n',encoding='utf-8')
            with patch.dict('os.environ',{},clear=True):settings=load_settings(env)
            self.assertEqual(settings.local_model,'Qwen3-1.7B-Q8_0')
            self.assertEqual(settings.providers[-1].model,settings.local_model)
            self.assertFalse(settings.remote_enabled)
            self.assertFalse(settings.allow_paid)
            env.write_text('XIAONUO_LOCAL_MODEL=arbitrary.gguf\n',encoding='utf-8')
            with self.assertRaisesRegex(ValueError,'Unsupported local model'):load_settings(env)

    def test_ready_old_model_is_not_accepted_as_new_model(self):
        model='Qwen3-1.7B-Q8_0'
        runtime=LocalModelRuntime(Settings(local_model=model,providers=(ProviderSpec('local_qwen',model,'fake'),)))
        for served,expected in [('Qwen3-0.6B-Q8_0',False),(model,True)]:
            opener=Mock()
            opener.open.side_effect=[BytesIO(json.dumps({'status':'ok'}).encode()),BytesIO(json.dumps({'data':[{'id':served}]}).encode())]
            with patch('app.local_runtime.build_opener',return_value=opener):
                self.assertEqual(runtime._health(),expected)

    def test_runtime_rejects_unapproved_port(self):
        with self.assertRaises(ValueError):LocalModelRuntime(Settings(),port=9000)
