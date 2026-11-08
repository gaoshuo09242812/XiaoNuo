import json
from pathlib import Path
import tempfile
import unittest
from uuid import uuid4

from app.device_ingress import cloudflare_config,fixed_hostname,save_config


class IngressTests(unittest.TestCase):
    def setUp(self):
        directory=tempfile.TemporaryDirectory();self.addCleanup(directory.cleanup)
        self.root=Path(directory.name)
        self.credentials=self.root/'credential.json';self.credentials.write_text('{"secret":"test-only-do-not-copy"}')
        self.ca=self.root/'origin.pem';self.ca.write_text('test fixture, not a real certificate')

    def config(self,host='xiaonuo.example.com'):
        return cloudflare_config(host,uuid4(),self.credentials,self.ca)

    def test_only_device_and_health_routes_use_tls_origin(self):
        config=self.config()
        self.assertEqual(config['ingress'][-1],{'service':'http_status:404'})
        for route in config['ingress'][:-1]:
            self.assertEqual(route['service'],'https://localhost:8768')
            self.assertIs(route['originRequest']['noTLSVerify'],False)
            self.assertEqual(route['originRequest']['originServerName'],'localhost')
        self.assertEqual({r['path'] for r in config['ingress'][:-1]},
                         {'^/api/v1/device-link/ws$','^/healthz$'})
        self.assertNotIn('test-only-do-not-copy',json.dumps(config))
        self.assertNotIn('8765',json.dumps(config));self.assertNotIn('8766',json.dumps(config))

    def test_temporary_url_and_address_injection_are_rejected(self):
        for host in ('https://x.example.com','x.example.com:443','a-b-c.trycloudflare.com',
                     '127.0.0.1','localhost','*.example.com','x.example.com/owner',
                     'x.example.com\nother','x.local','x.invalid',' x.example.com'):
            with self.assertRaises(ValueError,msg=host):fixed_hostname(host)

    def test_valid_host_is_normalized_without_changing_route(self):
        self.assertEqual(fixed_hostname('XIAONUO.Example.com'),'xiaonuo.example.com')

    def test_missing_files_or_invalid_tunnel_id_do_not_produce_configuration(self):
        with self.assertRaises(ValueError):cloudflare_config('x.example.com','bad',self.credentials,self.ca)
        self.ca.unlink()
        with self.assertRaises(ValueError):self.config()

    def test_repeated_identical_save_is_safe_and_different_config_is_preserved(self):
        config=self.config();target=self.root/'config.json'
        save_config(config,target);save_config(config,target)
        before=target.read_bytes()
        with self.assertRaises(ValueError):save_config(self.config('other.example.com'),target)
        self.assertEqual(target.read_bytes(),before)
