"""USB protocol tests use a fake port; they never open the attached ESP32."""
from dataclasses import replace
import json
from pathlib import Path
import queue
import tempfile
import time
import unittest
import asyncio
import threading
from concurrent.futures import ThreadPoolExecutor

from app.budgeting import UsageLedger
from app.config import Settings
from app.device_bridge import DeviceBridge
from app.errors import XiaoNuoError
from app.storage import CompanionStore
from app.main import create_app
from test_core import request_asgi


class FakeSerial:
    def __init__(self):
        self.responses = queue.Queue()
        self.writes = []
        self.closed = False
        self.quiet = False
        self.open_count = 0
        self.dtr = True
        self.rts = True
        self.fail_commands = False

    def status(self):
        return {"event": "status", "firmware": "0.3.0-safe", "chip": "ESP32-D0WD-V3", "flash_bytes": 4194304,
                "free_heap": 100000, "uptime_ms": 10, "state": "QUIET" if self.quiet else "IDLE", "quiet": self.quiet,
                "speaker_output": False, "password": "must-never-be-returned"}

    def open(self):
        assert self.dtr is False and self.rts is False
        self.open_count += 1
        self.closed = False

    def close(self):
        self.closed = True

    def write(self, data):
        command = data.decode().strip()
        self.writes.append(command)
        if self.fail_commands and command == "I2C_SCAN":
            raise OSError("Test disconnected port")
        if command.startswith("STATE "):
            self.quiet = command == "STATE QUIET"
        if command in {"INFO", "STATE QUIET", "STATE IDLE"}:
            message = self.status()
        elif command == "I2C_SCAN":
            message = {"event": "i2c_scan", "addresses": [60], "password": "must-never-be-returned"}
        elif command.startswith("OLED "):
            message = {"event": "error", "code": "OLED_NOT_FOUND"}
        elif command == "MIC_PROBE":
            message = {"event": "mic_probe", "samples": 4096, "rms": 3.2, "audio_uploaded": False, "audio_saved": False}
        else:
            message = {"event": "error", "code": "UNKNOWN_OR_DISABLED_COMMAND"}
        self.responses.put((json.dumps(message) + "\n").encode())

    def readline(self, limit):
        try:
            return self.responses.get(timeout=0.03)
        except queue.Empty:
            return b""


class DeviceBridgeTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.settings = Settings(root=Path(directory.name), usb_port="COM99")
        self.store = CompanionStore(UsageLedger(self.settings.database))
        self.port = FakeSerial()
        self.bridge = DeviceBridge(self.settings, self.store, factory=lambda: self.port)
        self.addCleanup(self.bridge.stop)

    def connected(self):
        self.bridge.start()
        until = time.monotonic() + 3
        while time.monotonic() < until:
            if self.bridge.status()["connected"]:
                return
            time.sleep(0.02)
        self.fail("Fake USB link did not connect")

    def test_no_configured_port_never_opens_hardware(self):
        bridge = DeviceBridge(replace(self.settings, usb_port=""), self.store, factory=lambda: self.fail("Unexpected port open"))
        bridge.start()
        self.assertFalse(bridge.status()["configured"])

    def test_explicit_port_handshake_and_redacted_status(self):
        self.connected()
        state = self.bridge.status()
        self.assertTrue(state["connected"])
        self.assertEqual(state["port"], "COM99")
        self.assertNotIn("password", state["info"])
        self.assertFalse(self.port.dtr)
        self.assertFalse(self.port.rts)

    def test_allowed_command_has_real_correlated_response(self):
        self.connected()
        result = self.bridge.execute("i2c_scan")
        self.assertEqual(result["addresses"], [60])
        self.assertNotIn("password", result)

    def test_handshake_recovers_a_partial_command_from_previous_session(self):
        class BufferedSerial(FakeSerial):
            pending=b'interrupted-command'
            def write(self,data):
                self.pending+=data
                while b'\n' in self.pending:
                    line,self.pending=self.pending.split(b'\n',1)
                    super().write(line+b'\n')
        self.port=BufferedSerial()
        self.bridge.factory=lambda:self.port
        self.connected()
        self.assertEqual(self.port.open_count,1)
        self.assertEqual(self.port.writes[:2],['interrupted-command','INFO'])

    def test_provisioning_checks_fresh_identity_before_sending_secret(self):
        status=self.port.status
        self.port.status=lambda:status()|{'device_id':'esp32-aaaaaaaaaaaa','binding_id':''}
        self.connected()
        with self.assertRaisesRegex(XiaoNuoError,'DEVICE_ID_CHANGED'):
            self.bridge.provision('11111111-1111-1111-1111-111111111111','a'*64,'esp32-bbbbbbbbbbbb')
        self.assertFalse(any(command.startswith('PAIR ') for command in self.port.writes))

    def test_private_provisioning_data_is_not_in_request_repr(self):
        from app.device_bridge import DeviceRequest
        token='b'*64
        request=DeviceRequest('bind_device',private_command='PAIR identifier '+token)
        self.assertNotIn(token,repr(request))

    def test_readonly_audio_diagnostics_never_claim_measured_sound(self):
        reply=DeviceBridge._safe_reply({'event':'audio_status','output_peak':750,'clock_measured':False,'audible_verified':False,
                                      'binding_secret':'must-not-leak','bclk_pin':26,'lrc_pin':25,'din_pin':33})
        self.assertFalse(reply['clock_measured']);self.assertFalse(reply['audible_verified'])
        self.assertNotIn('binding_secret',reply)
        self.assertEqual(reply['output_peak'],750)

    def test_high_power_and_arbitrary_commands_are_not_allowed(self):
        self.connected()
        for operation in ["unsupported_operation", "speaker_test", "INFO\nWIFI_SETUP"]:
            with self.assertRaisesRegex(XiaoNuoError, "DEVICE_OPERATION_NOT_ALLOWED"):
                self.bridge.execute(operation)

    def test_device_error_is_not_reported_as_success(self):
        self.connected()
        with self.assertRaisesRegex(XiaoNuoError, "OLED_NOT_FOUND"):
            self.bridge.execute("oled_ssd1306")

    def test_serial_failure_is_not_reported_as_empty_success(self):
        self.connected()
        self.port.fail_commands = True
        with self.assertRaisesRegex(XiaoNuoError, "SERIAL_LINK_FAILED"):
            self.bridge.execute("i2c_scan")

    def test_pause_releases_port_before_flashing(self):
        self.connected()
        self.bridge.pause()
        self.assertTrue(self.port.closed)
        self.assertFalse(self.bridge.status()["connected"])

    def test_quiet_preference_is_synchronized_to_device(self):
        self.store.set_quiet(True)
        self.connected()
        until = time.monotonic() + 3
        while time.monotonic() < until and not self.port.quiet:
            time.sleep(0.02)
        self.assertTrue(self.port.quiet)
        self.assertIn("STATE QUIET", self.port.writes)

    def test_boot_peripheral_error_does_not_prevent_status_handshake(self):
        self.port.responses.put(b'{"event":"error","code":"OLED_NOT_FOUND"}\n')
        self.connected()
        self.assertEqual(self.port.open_count, 1)

    def test_pause_during_connection_waits_for_safe_release(self):
        entered, release = threading.Event(), threading.Event()
        def factory():
            entered.set()
            release.wait(timeout=1)
            return self.port
        bridge = DeviceBridge(self.settings, self.store, factory=factory)
        self.addCleanup(bridge.stop)
        bridge.start()
        self.assertTrue(entered.wait(timeout=1))
        with ThreadPoolExecutor(max_workers=1) as executor:
            paused = executor.submit(bridge.pause)
            time.sleep(0.05)
            self.assertFalse(paused.done())
            release.set()
            paused.result(timeout=3)
        self.assertTrue(self.port.closed)

    def test_hardware_api_requires_auth_and_rejects_unavailable_operations(self):
        settings = replace(self.settings, usb_port="", admin_token="offline-test-admin-" + "x" * 40)
        app = create_app(settings)
        async def scenario():
            status, _ = await request_asgi(app, "/api/v1/device")
            self.assertEqual(status, 401)
            status, _ = await request_asgi(app, "/api/v1/device/command", method="POST", body={"operation":"i2c_scan"})
            self.assertEqual(status, 401)
            status, _ = await request_asgi(app, "/api/v1/device/command", token=settings.admin_token, method="POST", body={"operation":"unsupported_operation"})
            self.assertEqual(status, 422)
            status, data = await request_asgi(app, "/api/v1/device", token=settings.admin_token)
            self.assertEqual(status, 200)
            self.assertFalse(data["configured"])
        asyncio.run(scenario())

    def test_screen_check_exposes_only_display_diagnostics(self):
        result=DeviceBridge._safe_reply({'event':'screen_test','duration_seconds':12,
            'glyphs_ready':True,'visual_verified':False,'data':'private audio','secret':'private key'})
        self.assertEqual(result,{'event':'screen_test','duration_seconds':12,'glyphs_ready':True,'visual_verified':False})


if __name__ == "__main__":
    unittest.main()
