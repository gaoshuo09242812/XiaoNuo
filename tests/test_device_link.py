"""Protocol acceptance using temporary bindings; no models, mic or hardware."""
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from app.config import Settings
from app.main import create_app
from test_devices import FakeBoard


class DeviceLinkTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.app = create_app(Settings(root=Path(temporary.name), admin_token='owner-'+'x'*40))
        self.board = FakeBoard()
        self.app.state.devices.bridge = self.board
        self.record = self.app.state.devices.enroll()
        self.client = TestClient(self.app)
        self.addCleanup(self.client.close)
        self.url = 'wss://testserver/api/v1/device-link/ws'

    def connect(self, url=None, token=None, **kwargs):
        return self.client.websocket_connect(url or self.url,
            headers={'Authorization': 'Bearer '+(token or self.board.token), **kwargs})

    def hello(self, socket, **changes):
        payload = {'type':'hello', 'protocol':1, 'id':self.record['id'],
                   'hardware_id':self.board.hardware, 'firmware':'0.5.6-link'}
        payload.update(changes)
        socket.send_json(payload)
        return socket.receive_json()

    def test_authenticated_ping_and_reconnect_after_disconnect(self):
        for _ in range(2):
            with self.connect() as socket:
                ready = self.hello(socket)
                self.assertFalse(ready['audio_upload_enabled'])
                socket.send_json({'type':'ping','sequence':1})
                self.assertEqual(socket.receive_json(), {'type':'pong','sequence':1})
                self.assertNotIn(self.board.token,json.dumps(ready))
        self.assertFalse(self.app.state.device_links._active)

    def test_plaintext_even_with_spoofed_forwarded_header_is_rejected(self):
        with self.assertRaises(WebSocketDisconnect):
            with self.connect(self.url.replace('wss:','ws:'), **{'X-Forwarded-Proto':'https'}):
                pass

    def test_admin_key_is_not_a_device_key(self):
        with self.assertRaises(WebSocketDisconnect):
            with self.connect(token='owner-'+'x'*40):
                pass

    def test_query_tokens_and_browser_origins_rejected(self):
        for url, headers in ((self.url+'?token=example',{}),
                             (self.url,{'Origin':'https://testserver'})):
            with self.assertRaises(WebSocketDisconnect):
                with self.connect(url,**headers):
                    pass

    def test_wrong_hardware_or_token_cannot_connect(self):
        for token, changes in (('0'*64,{}),(None,{'hardware_id':'esp32-ffffffffffff'})):
            with self.connect(token=token) as socket:
                with self.assertRaises(WebSocketDisconnect):
                    self.hello(socket,**changes)

    def test_invalid_http_capabilities_are_rejected(self):
        for capabilities in ({}, {'reply_http':'true'}, {'reply_http':1},
                             {'reply_http':True,'extra':True}, ['reply_http'],
                             {'reply_buffered':'true'}, {'reply_buffered':True,'reply_streaming':True},
                             {'reply_buffered':True,'reply_http':True}, {'reply_streaming':'true'}, {'reply_streaming':True,'reply_http':True}):
            with self.subTest(capabilities=capabilities):
                with self.connect() as socket:
                    with self.assertRaises(WebSocketDisconnect):
                        self.hello(socket,protocol=5,capabilities=capabilities)
        with self.connect() as socket:
            with self.assertRaises(WebSocketDisconnect):
                self.hello(socket,protocol=4,capabilities={'reply_http':True})

    def test_explicit_false_http_capability_accepts_legacy_protocol(self):
        with self.connect() as socket:
            ready=self.hello(socket,capabilities={'reply_http':False})
            self.assertEqual(ready['type'],'ready')

    def test_second_connection_does_not_displace_active_device(self):
        with self.connect() as first:
            self.hello(first)
            with self.connect() as second:
                with self.assertRaises(WebSocketDisconnect):
                    self.hello(second)
            first.send_json({'type':'ping','sequence':1})
            self.assertEqual(first.receive_json()['sequence'],1)

    def test_revocation_invalidates_already_open_connection(self):
        with self.connect() as socket:
            self.hello(socket)
            self.board.connected = False
            self.app.state.devices.revoke(self.record['id'])
            socket.send_json({'type':'ping','sequence':1})
            with self.assertRaises(WebSocketDisconnect):
                socket.receive_json()

    def test_unsupported_audio_replay_and_large_frames_close_and_release(self):
        actions = [lambda s:s.send_bytes(b'fake audio'),
                   lambda s:s.send_json({'type':'ping','sequence':0}),
                   lambda s:s.send_text('x'*1025),
                   lambda s:s.send_text('[]'),
                   lambda s:s.send_json({'type':'ping','sequence':True})]
        for action in actions:
            with self.connect() as socket:
                self.hello(socket)
                action(socket)
                with self.assertRaises(WebSocketDisconnect):
                    socket.receive_json()
            self.assertFalse(self.app.state.device_links._active)

    def test_malformed_hello_never_creates_session(self):
        with self.connect() as socket:
            with self.assertRaises(WebSocketDisconnect):
                self.hello(socket,protocol=True)
        self.assertFalse(self.app.state.device_links._active)

    def test_idle_timeout_closes_connection_and_releases_slot(self):
        links=self.app.state.device_links
        original=links.message
        count=0
        async def timeout_after_hello(*args):
            nonlocal count
            count+=1
            if count>1:
                raise TimeoutError
            return await original(*args)
        with patch.object(links,'message',side_effect=timeout_after_hello):
            with self.connect() as socket:
                self.hello(socket)
                with self.assertRaises(WebSocketDisconnect) as error:
                    socket.receive_json()
                self.assertEqual(error.exception.reason,'DEVICE_LINK_TIMEOUT')
        self.assertFalse(self.app.state.device_links._active)
