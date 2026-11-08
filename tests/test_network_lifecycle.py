"""Host stop/privacy operations with temporary bindings and synthetic audio."""
import asyncio
import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import AsyncMock, patch
from uuid import uuid4

from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect
from app.config import Settings
from app.errors import XiaoNuoError
from app.main import create_app
from app.network_audio import encode_audio_frame
from test_core import request_asgi
from test_devices import FakeBoard


class Socket:
    """Minimal ASGI-side socket; never opens a port or uses hardware."""
    def __init__(self, token):
        self.url = type('Url', (), {'scheme': 'wss'})()
        self.headers = {'authorization': 'Bearer ' + token}
        self.query_params = {}
        self.incoming = asyncio.Queue()
        self.outgoing = asyncio.Queue()

    async def accept(self):
        pass

    async def receive(self):
        return await self.incoming.get()

    async def send_json(self, message):
        self.outgoing.put_nowait(message)

    async def send_bytes(self, data):
        self.outgoing.put_nowait(data)

    async def close(self, code=1000, reason=''):
        self.outgoing.put_nowait({'type': 'close', 'code': code, 'reason': reason})

    def send(self, message):
        self.incoming.put_nowait({'type': 'websocket.receive', 'text': json.dumps(message)})

    async def result(self):
        return await asyncio.wait_for(self.outgoing.get(), 2)


class HostLifecycleTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.settings = Settings(root=Path(temporary.name), admin_token='owner-' + 'x'*40)
        self.app = create_app(self.settings)
        self.links = self.app.state.device_links
        self.registry = self.app.state.devices
        self.board = FakeBoard()
        self.registry.bridge = self.board
        self.record = self.registry.enroll()
        self.tasks = []

    async def asyncTearDown(self):
        await self.links.shutdown()
        if self.tasks:
            await asyncio.gather(*self.tasks, return_exceptions=True)

    async def connect(self, protocol=3, hello=True):
        socket = Socket(self.board.token)
        task = asyncio.create_task(self.links.serve(socket))
        self.tasks.append(task)
        if hello:
            socket.send({'type': 'hello', 'protocol': protocol, 'id': self.record['id'],
                         'hardware_id': self.board.hardware, 'firmware': 'lifecycle-test'})
            self.assertEqual((await socket.result())['type'], 'ready')
        else:
            await asyncio.sleep(0)
        return socket

    async def begin(self, socket):
        socket.send({'type': 'audio_begin', 'request_id': str(uuid4()),
                     'seconds': 2, 'consent': 'owner-computer'})
        return await socket.result()

    async def upload(self, socket):
        ready = await self.begin(socket)
        pcm = b'\x01\x02'*32000
        for sequence, offset in enumerate(range(0, len(pcm), 1000)):
            socket.incoming.put_nowait({'type': 'websocket.receive', 'bytes':
                encode_audio_frame(ready['stream_id'], sequence, pcm[offset:offset+1000])})
        socket.send({'type': 'audio_end', 'stream_id': ready['stream_id'],
                     'sha256': hashlib.sha256(pcm).hexdigest()})
        self.assertEqual((await socket.result())['type'], 'audio_processing')

    def request_states(self):
        with self.registry.store.ledger.connect() as db:
            return [row[0] for row in db.execute('SELECT state FROM chat_requests')]

    async def owner(self, path, *, method='POST', body=None):
        return await request_asgi(self.app, path, method=method, body=body,
                                  token=self.settings.admin_token)

    async def test_stop_discards_partial_audio_and_allows_new_connection(self):
        self.links.audio_handler = AsyncMock()
        socket = await self.connect()
        ready = await self.begin(socket)
        socket.incoming.put_nowait({'type': 'websocket.receive', 'bytes':
            encode_audio_frame(ready['stream_id'], 0, b'\0'*1000)})
        socket.send({'type': 'ping', 'sequence': 1})
        self.assertEqual((await socket.result())['type'], 'pong')
        audio = next(iter(self.links._connections.values())).audio
        receiver = audio.receiver
        status, _ = await self.owner('/api/v1/voice/cancel')
        self.assertEqual(status, 200)
        self.assertEqual((await socket.result())['code'], 1012)
        self.assertEqual(receiver.snapshot()['received_bytes'], 0)
        self.assertFalse(self.links._active)
        self.assertEqual(self.request_states(), ['failed_or_uncertain'])
        self.links.audio_handler.assert_not_called()
        await self.connect()

    async def test_quiet_cancels_processor_and_prevents_late_result(self):
        started = asyncio.Event()
        cancellation = []
        async def ignores_cancel(pcm, claim, device, cancelled):
            cancellation.append(cancelled)
            started.set()
            try:
                await asyncio.sleep(30)
            except asyncio.CancelledError:
                return {'transcript': 'private', 'reply_text': 'must-not-arrive'}
        self.links.audio_handler = ignores_cancel
        socket = await self.connect()
        await self.upload(socket)
        await asyncio.wait_for(started.wait(), 2)
        status, _ = await self.owner('/api/v1/quiet', method='PUT', body={'enabled': True})
        self.assertEqual(status, 200)
        self.assertTrue(cancellation[0].is_set())
        self.assertEqual((await socket.result())['type'], 'close')
        self.assertTrue(socket.outgoing.empty())
        self.assertEqual(self.request_states(), ['failed_or_uncertain'])

    async def test_stop_aborts_pending_reply_before_socket_closes(self):
        async def handler(*args):
            async def audio():
                yield b'\x01\x02'*2000
            return {'reply_text': '公开测试', '_audio': audio()}
        self.links.audio_handler = handler
        socket = await self.connect()
        await self.upload(socket)
        self.assertEqual((await socket.result())['type'], 'audio_result')
        self.assertEqual((await socket.result())['type'], 'reply_start')
        await self.links.interrupt()
        self.assertEqual((await socket.result())['type'], 'reply_abort')
        self.assertEqual((await socket.result())['type'], 'close')
        self.assertEqual(self.request_states(), ['failed_or_uncertain'])

    async def test_suspension_rejects_reconnect_until_owner_operation_finishes(self):
        async with self.links.suspended():
            socket = Socket(self.board.token)
            await self.links.serve(socket)
            self.assertEqual((await socket.result())['type'], 'close')
        await self.connect(protocol=1)

    async def test_shutdown_closes_idle_and_preauth_peers_and_stays_closed(self):
        first = await self.connect(protocol=1)
        second = await self.connect(hello=False)
        await self.links.shutdown()
        for socket in (first, second):
            self.assertEqual((await socket.result())['code'], 1012)
        self.assertFalse(self.links._connections)
        self.assertFalse(self.links._active)
        rejected = Socket(self.board.token)
        await self.links.serve(rejected)
        self.assertEqual((await rejected.result())['type'], 'close')

    async def test_privacy_deletion_drains_handler_before_deleting_bindings(self):
        links = self.links
        registry = self.registry
        class Handler:
            async def __call__(self, *args):
                raise AssertionError('no audio should be submitted')
            async def clear_context(self):
                self.cleared = True
                if links._accepting or links._connections or not registry.list():
                    raise AssertionError('wrong privacy operation order')
        handler = Handler()
        self.links.audio_handler = handler
        socket = await self.connect()
        await self.begin(socket)
        status, body = await self.owner('/api/v1/privacy/data', method='DELETE',
                                       body={'confirm': 'DELETE_PERSONAL_DATA'})
        self.assertEqual(status, 200)
        self.assertTrue(body['personal_data_deleted'])
        self.assertTrue(handler.cleared)
        self.assertEqual(self.registry.list(), [])
        self.assertEqual((await socket.result())['type'], 'close')

    async def test_privacy_timeout_does_not_claim_deletion_or_drop_binding(self):
        class Handler:
            async def clear_context(self):
                raise XiaoNuoError('NETWORK_WORKER_STILL_STOPPING', 409)
        self.links.audio_handler = Handler()
        status, body = await self.owner('/api/v1/privacy/data', method='DELETE',
                                       body={'confirm': 'DELETE_PERSONAL_DATA'})
        self.assertEqual(status, 409)
        self.assertEqual(body['error'], 'NETWORK_WORKER_STILL_STOPPING')
        self.assertTrue(self.registry.list())

    async def test_unmanaged_handler_blocks_privacy_deletion(self):
        self.links.audio_handler = AsyncMock(spec=[])
        status, body = await self.owner('/api/v1/privacy/data', method='DELETE',
                                       body={'confirm': 'DELETE_PERSONAL_DATA'})
        self.assertEqual(status, 503)
        self.assertEqual(body['error'], 'NETWORK_PRIVACY_LIFECYCLE_REQUIRED')
        self.assertTrue(self.registry.list())


class AppShutdownTests(unittest.IsolatedAsyncioTestCase):
    async def check_cleanup(self, failure=None):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        settings = Settings(root=Path(temporary.name), admin_token='owner-'+'x'*40)
        with (patch('app.main.DeviceBridge') as bridge_type,
              patch('app.main.ReminderService') as reminders_type,
              patch('app.main.LocalModelRuntime') as runtime_type):
            runtime = runtime_type.return_value
            runtime.ready = False
            if failure == 'startup':
                bridge_type.return_value.start.side_effect = RuntimeError('test-start-failure')
            app = create_app(settings)
            if failure == 'shutdown':
                app.state.device_links.shutdown = AsyncMock(side_effect=XiaoNuoError('DEVICE_LINK_STOP_TIMEOUT',503))
            context = app.router.lifespan_context(app)
            if failure:
                with self.assertRaises((RuntimeError, XiaoNuoError)):
                    async with context:
                        pass
            else:
                async with context:
                    self.assertTrue(app.state.device_links._accepting)
                self.assertTrue(app.state.device_links._closed)
            runtime.start.assert_called_once()
            runtime.stop.assert_called_once()
            reminders_type.return_value.stop.assert_called_once()
            bridge_type.return_value.stop.assert_called_once()

    async def test_normal_shutdown_closes_connections_before_runtime_cleanup(self):
        await self.check_cleanup()

    async def test_startup_failure_still_releases_host_resources(self):
        await self.check_cleanup('startup')

    async def test_network_shutdown_error_still_releases_host_resources(self):
        await self.check_cleanup('shutdown')


class AsgiHostStopTests(unittest.TestCase):
    def test_http_stop_waits_for_socket_cleanup_not_outer_asgi_task(self):
        with (tempfile.TemporaryDirectory() as directory,
              patch('app.main.DeviceBridge'), patch('app.main.ReminderService'),
              patch('app.main.LocalModelRuntime') as runtime):
            runtime.return_value.ready = False
            settings = Settings(root=Path(directory), admin_token='owner-'+'x'*40)
            app = create_app(settings)
            board = FakeBoard()
            app.state.devices.bridge = board
            record = app.state.devices.enroll()
            with TestClient(app) as client:
                def connect():
                    return client.websocket_connect('wss://testserver/api/v1/device-link/ws',
                        headers={'Authorization': 'Bearer '+board.token})
                def hello(socket):
                    socket.send_json({'type': 'hello', 'protocol': 1, 'id': record['id'],
                        'hardware_id': board.hardware, 'firmware': 'asgi-stop-check'})
                    self.assertEqual(socket.receive_json()['type'], 'ready')
                with connect() as socket:
                    hello(socket)
                    response = client.post('/api/v1/voice/cancel',
                        headers={'Authorization': 'Bearer '+settings.admin_token})
                    self.assertEqual(response.status_code, 200, response.text)
                    with self.assertRaises(WebSocketDisconnect) as error:
                        socket.receive_json()
                    self.assertEqual(error.exception.code, 1012)
                with connect() as socket:
                    hello(socket)
                    socket.send_json({'type': 'ping', 'sequence': 1})
                    self.assertEqual(socket.receive_json(), {'type': 'pong', 'sequence': 1})
            self.assertTrue(app.state.device_links._closed)
