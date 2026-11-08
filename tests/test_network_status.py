"""Candidate network diagnostics must never expose endpoint headers or keys."""
import unittest
from app.device_bridge import DeviceBridge, OPERATIONS


class NetworkStatusTests(unittest.TestCase):
    def test_network_status_keeps_only_public_diagnostics(self):
        result=DeviceBridge._safe_reply({'event':'network_status','configured':False,
            'connected':False,'ready':False,'state':'DISABLED','pong_count':0,
            'audio_upload_enabled':False,'tls_verified':False,
            'authorization':'Bearer not-a-real-key','key':'not-a-real-key','host':'private-host'})
        self.assertEqual(set(result),{'event','configured','connected','ready','state','pong_count',
                                     'audio_upload_enabled','tls_verified'})
        self.assertFalse(result['tls_verified'])
        self.assertEqual(OPERATIONS['network_status'],('LINK_STATUS','network_status'))
