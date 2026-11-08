"""Shared listener ownership and recovery without models or hardware."""
import asyncio
from pathlib import Path
import socket
import shutil
import os
import subprocess
import tempfile
import unittest
from unittest.mock import patch
from datetime import datetime,timezone

from app.config import Settings
from app.errors import XiaoNuoError
from app.network_host import create_network_host, serve_network_host


class SharedHostTests(unittest.IsolatedAsyncioTestCase):
    @classmethod
    def setUpClass(cls):
        cls.cert_directory = tempfile.TemporaryDirectory()
        directory = Path(cls.cert_directory.name)
        cls.cert, cls.key = directory/'cert.pem', directory/'key.pem'
        openssl = shutil.which('openssl')
        if not openssl and os.name == 'nt' and shutil.which('git'):
            candidate = Path(shutil.which('git')).resolve().parents[1] / 'usr/bin/openssl.exe'
            openssl = str(candidate) if candidate.is_file() else None
        if not openssl:
            cls.cert_directory.cleanup()
            raise unittest.SkipTest('OpenSSL required for local TLS fixture')
        subprocess.run([openssl, 'req', '-x509',
            '-newkey','rsa:2048','-nodes','-sha256','-days','1','-keyout',str(cls.key),
            '-out',str(cls.cert),'-subj','/CN=localhost','-addext','subjectAltName=DNS:localhost'],
            check=True, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)

    @classmethod
    def tearDownClass(cls):
        cls.cert_directory.cleanup()

    async def asyncSetUp(self):
        clock=patch('app.budgeting.now',return_value=datetime(2026,10,28,tzinfo=timezone.utc))
        clock.start();self.addCleanup(clock.stop)
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.settings = Settings(root=Path(temporary.name), admin_token='owner-'+'x'*40)
        self.patches = [patch('app.main.DeviceBridge'), patch('app.main.ReminderService'),
                        patch('app.main.LocalModelRuntime')]
        self.bridge, self.reminders, self.runtime_type = [p.start() for p in self.patches]
        for patched in self.patches:
            self.addCleanup(patched.stop)
        self.runtime = self.runtime_type.return_value
        self.runtime.ready = False

    async def test_shared_voice_state_and_usb_mutual_exclusion(self):
        owner, gateway = create_network_host(self.settings, enable_audio=True)
        voice = owner.state.voice_service
        adapter = owner.state.device_links.audio_handler
        self.assertIs(owner.state.device_links, gateway.state.device_links)
        self.assertIs(adapter.asr, voice.asr)
        self.assertIs(adapter.tts, voice.tts)
        self.assertIs(adapter.store, owner.state.companion.store)
        self.assertIs(adapter._work, voice._turn)
        voice._turn.acquire()
        try:
            import threading
            with self.assertRaisesRegex(XiaoNuoError, 'VOICE_TURN_BUSY'):
                await adapter(b'\0'*64000, '00000000-0000-4000-8000-000000000001',
                              'device', threading.Event())
        finally:
            voice._turn.release()

    async def test_audio_requires_local_only_settings(self):
        from dataclasses import replace
        for change in ({'remote_enabled':True}, {'allow_paid':True}):
            with self.assertRaises(ValueError):
                create_network_host(replace(self.settings, **change), enable_audio=True)

    async def test_wake_preference_changes_refresh_existing_device_capability(self):
        from dataclasses import replace
        from unittest.mock import AsyncMock
        from fastapi.testclient import TestClient
        owner,_=create_network_host(replace(self.settings,computer_wake_enabled=True),enable_audio=True)
        adapter=owner.state.device_links.audio_handler
        self.assertFalse(adapter.wake_enabled)
        with patch.object(owner.state.device_links,'interrupt',new_callable=AsyncMock) as interrupt:
            with TestClient(owner) as client:
                for enabled in (True,False):
                    response=client.patch('/api/v1/preferences',json={'computer_wake_enabled':enabled},
                        headers={'Authorization':'Bearer '+self.settings.admin_token})
                    self.assertEqual(response.status_code,200,response.text)
                    self.assertEqual(adapter.wake_enabled,enabled)
            self.assertEqual(interrupt.await_count,2)

    async def test_worker_host_owns_one_lifecycle_and_releases_console_port(self):
        from app.worker_relay import serve_worker_host
        owner,_=create_network_host(self.settings)
        stop=asyncio.Event();ready=asyncio.get_running_loop().create_future()
        async def relay(*args,**kwargs):await stop.wait()
        with patch('app.worker_relay.relay_forever',side_effect=relay):
            task=asyncio.create_task(serve_worker_host(owner,'xiaonuo.fixture.workers.dev','1'*64,
                stop=stop,console_port=0,ready=ready))
            port=await asyncio.wait_for(ready,3);stop.set();await asyncio.wait_for(task,5)
        with socket.socket() as probe:probe.bind(('127.0.0.1',port))
        self.runtime.start.assert_called_once();self.runtime.stop.assert_called_once()
        self.assertTrue(owner.state.device_links._closed)

    async def test_worker_relay_failure_stops_console_and_models(self):
        from app.worker_relay import serve_worker_host
        owner,_=create_network_host(self.settings)
        async def fail(*args,**kwargs):raise RuntimeError('synthetic relay supervisor failure')
        with patch('app.worker_relay.relay_forever',side_effect=fail):
            with self.assertRaisesRegex(XiaoNuoError,'WORKER_RELAY_HOST_EXITED'):
                await serve_worker_host(owner,'xiaonuo.fixture.workers.dev','1'*64,console_port=0)
        self.runtime.stop.assert_called_once();self.assertTrue(owner.state.device_links._closed)

    async def test_busy_port_fails_before_model_startup(self):
        owner, gateway = create_network_host(self.settings)
        with socket.socket() as occupied:
            occupied.bind(('127.0.0.1', 0))
            with self.assertRaises(OSError):
                await serve_network_host(owner,gateway,self.cert,self.key,
                    console_port=0,device_port=occupied.getsockname()[1])
        self.runtime.start.assert_not_called()

    async def test_invalid_tls_fails_before_model_startup(self):
        owner, gateway = create_network_host(self.settings)
        with self.assertRaises((OSError, ValueError)):
            await serve_network_host(owner,gateway,self.key,self.cert,console_port=0,device_port=0)
        self.runtime.start.assert_not_called()

    async def test_unready_model_never_opens_audio_listeners(self):
        owner, gateway = create_network_host(self.settings, enable_audio=True)
        with patch('app.network_host._Listener') as listener:
            with self.assertRaisesRegex(XiaoNuoError,'NETWORK_LOCAL_MODEL_NOT_READY'):
                await serve_network_host(owner,gateway,self.cert,self.key,console_port=0,device_port=0)
            for item in listener.return_value.mock_calls:
                self.assertNotEqual(item[0], 'serve')
        self.runtime.stop.assert_called_once()
        self.bridge.return_value.start.assert_not_called()

    async def check_supervision(self, fail=False, deepseek=False):
        settings=self.settings
        if deepseek:
            from dataclasses import replace
            from decimal import Decimal
            from app.config import ProviderSpec
            settings=replace(settings,remote_enabled=True,allow_paid=True,primary_paid='deepseek',
                deployment_region='CN',monthly_server_cost=Decimal('0'),cny_to_usd=Decimal('.15'),
                fx_checked_on='2026-10-28',network_chat_provider='deepseek',
                providers=(ProviderSpec('deepseek','deepseek-flash','offline-fake-key',False,True),))
        owner, gateway = create_network_host(settings,enable_audio=deepseek)
        instances = []
        failure = asyncio.Event()
        class Listener:
            def __init__(self, config):
                self.config=config;self.started=False;self.should_exit=False
                instances.append(self)
            async def serve(self, sockets):
                self.started=True
                while not self.should_exit:
                    if fail and self is instances[1] and failure.is_set():
                        return
                    await asyncio.sleep(.01)
        stop = asyncio.Event()
        ready = asyncio.get_running_loop().create_future()
        with patch('app.network_host._Listener', Listener):
            task = asyncio.create_task(serve_network_host(owner,gateway,self.cert,self.key,
                console_port=0,device_port=0,stop=stop,ready=ready))
            try:
                ports = await asyncio.wait_for(ready, 2)
                for instance in instances:
                    self.assertEqual(instance.config.host, '127.0.0.1')
                    self.assertFalse(instance.config.proxy_headers)
                    self.assertEqual(instance.config.lifespan, 'off')
                self.assertFalse(instances[0].config.ssl_certfile)
                self.assertEqual(instances[1].config.ssl_certfile, str(self.cert))
                if fail:
                    failure.set()
                    with self.assertRaisesRegex(XiaoNuoError,'NETWORK_LISTENER_EXITED'):
                        await asyncio.wait_for(task, 3)
                else:
                    stop.set()
                    await asyncio.wait_for(task, 3)
                for port in ports:
                    with socket.socket() as probe:
                        probe.bind(('127.0.0.1', port))
                self.assertTrue(owner.state.device_links._closed)
                self.runtime.start.assert_called_once()
                self.runtime.stop.assert_called_once()
                self.bridge.return_value.start.assert_not_called()
            finally:
                stop.set()
                if not task.done():
                    task.cancel()
                await asyncio.gather(task, return_exceptions=True)

    async def test_one_lifecycle_owns_both_listeners_and_releases_ports(self):
        await self.check_supervision()

    async def test_failure_of_either_listener_stops_the_other(self):
        await self.check_supervision(fail=True)

    async def test_deepseek_audio_does_not_require_a_second_local_chat_model(self):
        await self.check_supervision(deepseek=True)
