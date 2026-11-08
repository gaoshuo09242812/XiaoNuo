"""Development USB link. Only an explicit configured port is ever opened."""
from dataclasses import dataclass, field
from datetime import datetime, timezone
import base64
import importlib
import json
import math
from pathlib import Path
import queue
import re
import sys
import threading
import time

from .config import ROOT, Settings
from .errors import XiaoNuoError
from .storage import CompanionStore
from .speaker import stream_pcm

OPERATIONS = {
    'network_status':('LINK_STATUS','network_status'),
    'screen_test':('SCREEN_TEST','screen_test'),
    'recognizing':('TASK RECOGNIZING','task_status'),
    'thinking':('TASK THINKING','task_status'),
    'synthesizing':('TASK SYNTHESIZING','task_status'),
    'activity_clear':('TASK CLEAR','task_status'),
    'audio_status':('AUDIO_STATUS','audio_status'),
    "status": ("INFO", "status"), "i2c_scan": ("I2C_SCAN", "i2c_scan"),
    "oled_ssd1306": ("OLED ssd1306", "oled_ready"), "oled_sh1106": ("OLED sh1106", "oled_ready"),
    "oled_off": ("OLED off", "oled_off"), "microphone_probe": ("MIC_PROBE", "mic_probe"),
    "wifi_setup": ("WIFI_SETUP", "setup_ready"), "wifi_stop_setup": ("WIFI_STOP_SETUP", "status"),
    "wifi_retry": ("WIFI_RETRY", "status")}


def serial_port_factory():
    try:
        serial = importlib.import_module("serial")
    except ImportError:
        local = ROOT / ".runtime-libs"
        if local.is_dir() and str(local) not in sys.path:
            sys.path.insert(0, str(local))
        try:
            serial = importlib.import_module("serial")
        except ImportError:
            raise XiaoNuoError("PYSERIAL_NOT_INSTALLED") from None
    return serial.Serial()


@dataclass
class DeviceRequest:
    operation: str
    completed: threading.Event = field(default_factory=threading.Event)
    cancelled: threading.Event = field(default_factory=threading.Event)
    result: dict | None = None
    error: str | None = None
    audio_seconds: int = 0
    pcm: bytes = field(default=b'',repr=False)
    private_command: str = field(default='',repr=False)
    external_cancel: threading.Event | None = field(default=None,repr=False)
    expected_hardware: str = ''


class DeviceBridge:
    def __init__(self, settings: Settings, store: CompanionStore, factory=serial_port_factory):
        self.settings, self.store, self.factory = settings, store, factory
        self.port_name = settings.usb_port
        self._port = None
        self._thread = None
        self._stop = threading.Event()
        self._paused = threading.Event()
        self._disconnected = threading.Event()
        self._disconnected.set()
        self._lock = threading.Lock()
        self._connection_gate = threading.Lock()
        self._requests = queue.Queue(maxsize=8)
        self._info = {}
        self._connected = False
        self._last_seen = None
        self._last_tick = 0.0
        self._last_status_clock = 0.0
        self._attempts = 0
        self._error = None
        self._capture_cancel = threading.Event()
        self._capture_state = "idle"
        self._play_cancel=threading.Event()
        self._playback_state='idle'

    def status(self):
        with self._lock:
            connected = self._connected and time.monotonic() - self._last_status_clock < 8
            return {"configured": bool(self.port_name), "port": self.port_name or None,
                    "transport": "USB" if self.port_name else None, "connected": connected,
                    "paused": self._paused.is_set(), "attempts": self._attempts,"capture_state":self._capture_state,
                    'playback_state':self._playback_state,'speaker_enabled':self.settings.device_speaker_enabled,
                    "last_seen": self._last_seen, "last_error": self._error, "info": dict(self._info)}

    def start(self):
        if not self.port_name or (self._thread and self._thread.is_alive()):
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="xiaonuo-usb", daemon=True)
        self._thread.start()

    def stop(self):
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=4)

    def pause(self):
        self._paused.set()
        with self._connection_gate:
            pass
        while not self._requests.empty():
            try:
                request = self._requests.get_nowait()
            except queue.Empty:
                break
            request.cancelled.set()
            request.error = "DEVICE_PAUSED"
            request.completed.set()
        if not self._disconnected.wait(timeout=4):
            raise XiaoNuoError("DEVICE_BUSY", 409)

    def retry(self):
        with self._lock:
            self._attempts = 0
            self._error = None
        self._paused.clear()
        self.start()

    def execute(self, operation: str, timeout: float = 7) -> dict:
        if operation not in OPERATIONS:
            raise XiaoNuoError("DEVICE_OPERATION_NOT_ALLOWED", 422)
        if not self.port_name:
            raise XiaoNuoError("USB_NOT_CONFIGURED")
        if self._paused.is_set() or not self.status()["connected"]:
            raise XiaoNuoError("DEVICE_NOT_CONNECTED")
        request = DeviceRequest(operation)
        try:
            with self._connection_gate:
                if self._paused.is_set():
                    raise XiaoNuoError("DEVICE_PAUSED")
                self._requests.put_nowait(request)
        except queue.Full:
            raise XiaoNuoError("DEVICE_BUSY", 429) from None
        if not request.completed.wait(timeout):
            request.cancelled.set()
            raise XiaoNuoError("DEVICE_COMMAND_TIMEOUT")
        if request.error:
            raise XiaoNuoError(request.error)
        return request.result or {}

    def cancel_capture(self):
        self._capture_cancel.set()

    def provision(self,identifier,token,hardware_id):
        if not re.fullmatch(r'[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}',identifier) or not re.fullmatch(r'[0-9a-f]{64}',token):
            raise XiaoNuoError('INVALID_DEVICE_PROVISIONING',422)
        return self._binding_request('bind_device',f'PAIR {identifier} {token}',hardware_id)

    def remove_binding(self,identifier,hardware_id):
        if not re.fullmatch(r'[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}',identifier):
            raise XiaoNuoError('INVALID_DEVICE_PROVISIONING',422)
        return self._binding_request('unbind_device','UNPAIR '+identifier,hardware_id)

    def _binding_request(self,operation,command,hardware_id):
        if not re.fullmatch(r'esp32-[0-9a-f]{12}',hardware_id):raise XiaoNuoError('INVALID_DEVICE_ID',422)
        request=DeviceRequest(operation,private_command=command,expected_hardware=hardware_id)
        with self._connection_gate:
            if self._paused.is_set() or self._stop.is_set() or not self.status()['connected']:raise XiaoNuoError('DEVICE_NOT_CONNECTED')
            try:self._requests.put_nowait(request)
            except queue.Full:raise XiaoNuoError('DEVICE_BUSY',429) from None
        try:
            if not request.completed.wait(8):
                request.cancelled.set();raise XiaoNuoError('DEVICE_PROVISIONING_TIMEOUT')
            if request.error:raise XiaoNuoError(request.error)
            return request.result or {}
        finally:request.private_command=''

    def _binding_exchange(self,request):
        try:
            # Re-read identity immediately before writing any credential. A port
            # name alone must never select a different newly attached board.
            state=self._exchange('INFO','status')
            if state.get('device_id')!=request.expected_hardware:raise XiaoNuoError('DEVICE_ID_CHANGED',409)
            if request.cancelled.is_set() or not request.private_command:raise XiaoNuoError('DEVICE_PROVISIONING_CANCELLED',409)
            event='paired' if request.operation=='bind_device' else 'unpaired'
            result=self._exchange(request.private_command,event)
            self._exchange('INFO','status')
            return result
        finally:request.private_command=''

    def cancel_playback(self):
        self._play_cancel.set()

    def play_audio(self,pcm: bytes,*,cancelled=None):
        if cancelled is not None and cancelled.is_set():raise XiaoNuoError('SPEAKER_PLAYBACK_CANCELLED',409)
        if not self.settings.device_speaker_enabled:raise XiaoNuoError('DEVICE_SPEAKER_DISABLED',409)
        if not pcm or len(pcm)%2 or len(pcm)>16000*2*8:raise XiaoNuoError('SPEAKER_AUDIO_TOO_LONG',422)
        state=self.status()
        if not state['connected'] or self._paused.is_set():raise XiaoNuoError('DEVICE_NOT_CONNECTED')
        if not state['info'].get('speaker_available'):raise XiaoNuoError('DEVICE_FIRMWARE_NEEDS_UPDATE')
        if self.store.controls()['quiet'] or not self.store.controls()['speech_enabled']:raise XiaoNuoError('SPEECH_DISABLED_BY_OWNER',409)
        request=DeviceRequest('play_audio',pcm=pcm,external_cancel=cancelled)
        with self._connection_gate:
            if self._paused.is_set():raise XiaoNuoError('DEVICE_PAUSED')
            if cancelled is not None and cancelled.is_set():raise XiaoNuoError('SPEAKER_PLAYBACK_CANCELLED',409)
            self._play_cancel.clear()
            try:self._requests.put_nowait(request)
            except queue.Full:raise XiaoNuoError('DEVICE_BUSY',429) from None
        if not request.completed.wait(18):
            request.cancelled.set();self._play_cancel.set()
            raise XiaoNuoError('SPEAKER_REPLY_TIMEOUT')
        if request.error:raise XiaoNuoError(request.error)
        return request.result or {}

    def _play_audio(self,request):
        def cancelled():
            controls=self.store.controls()
            return self._play_cancel.is_set() or request.cancelled.is_set() or (request.external_cancel is not None and request.external_cancel.is_set()) or self._stop.is_set() or self._paused.is_set() or controls['quiet'] or not controls['speech_enabled']
        def seen(message=None):
            if message:self._accept_status(message)
            with self._lock:
                self._last_status_clock=time.monotonic()
                self._last_seen=datetime.now(timezone.utc).isoformat()
        with self._lock:self._playback_state='playing'
        try:
            # Refresh connection time only after an actual device acknowledgment.
            return stream_pcm(self._port,request.pcm,cancelled=cancelled,on_status=seen,on_progress=seen)
        finally:
            request.pcm=b''
            with self._lock:self._playback_state='idle'

    def capture_audio(self, seconds: int = 6,*,cancelled=None) -> dict:
        if cancelled is not None and cancelled.is_set():raise XiaoNuoError('AUDIO_CAPTURE_CANCELLED',409)
        if type(seconds) is not int or not 2 <= seconds <= 6:
            raise XiaoNuoError("INVALID_RECORDING_DURATION",422)
        if not self.status()["connected"] or self._paused.is_set():
            raise XiaoNuoError("DEVICE_NOT_CONNECTED")
        request=DeviceRequest("capture_audio",audio_seconds=seconds,external_cancel=cancelled)
        with self._connection_gate:
            if self._paused.is_set():raise XiaoNuoError("DEVICE_PAUSED")
            if cancelled is not None and cancelled.is_set():raise XiaoNuoError('AUDIO_CAPTURE_CANCELLED',409)
            self._capture_cancel.clear()
            try:self._requests.put_nowait(request)
            except queue.Full:raise XiaoNuoError("DEVICE_BUSY",429) from None
        if not request.completed.wait(seconds+12):
            request.cancelled.set()
            self._capture_cancel.set()
            raise XiaoNuoError("DEVICE_COMMAND_TIMEOUT")
        if request.error:raise XiaoNuoError(request.error)
        return request.result or {}

    def _capture_audio(self, request: DeviceRequest) -> dict:
        pcm=bytearray()
        sequence=0
        started=False
        finished=False
        cancelled=False
        initial_quiet=self.store.controls()["quiet"]
        with self._lock:self._capture_state="warming"
        deadline=time.monotonic()+request.audio_seconds+8
        try:
            if self._capture_cancel.is_set() or request.cancelled.is_set() or (request.external_cancel is not None and request.external_cancel.is_set()):
                raise XiaoNuoError('AUDIO_CAPTURE_CANCELLED',409)
            self._port.write(("CAPTURE "+str(request.audio_seconds)+"\n").encode("ascii"))
            while time.monotonic()<deadline and not self._stop.is_set() and not self._paused.is_set():
                owner_quiet=not initial_quiet and self.store.controls()["quiet"]
                if not cancelled and (self._capture_cancel.is_set() or request.cancelled.is_set() or (request.external_cancel is not None and request.external_cancel.is_set()) or owner_quiet):
                    self._port.write(b"CAPTURE_CANCEL\n")
                    cancelled=True
                raw=self._port.readline(2048)
                if not raw:continue
                if len(raw)>=2048:raise XiaoNuoError("INVALID_AUDIO_FRAME")
                try:message=json.loads(raw)
                except (ValueError,UnicodeError):raise XiaoNuoError("INVALID_AUDIO_FRAME") from None
                if not isinstance(message,dict):raise XiaoNuoError("INVALID_AUDIO_FRAME")
                event=message.get("event")
                if event=="audio_start":
                    if started or message.get("sample_rate")!=16000 or message.get("channels")!=1 or message.get("bits")!=16 or message.get("max_samples")!=request.audio_seconds*16000:
                        raise XiaoNuoError("INVALID_AUDIO_FORMAT")
                    started=True
                elif event=="audio_chunk":
                    if not started or type(message.get("seq")) is not int or message["seq"]!=sequence:
                        raise XiaoNuoError("AUDIO_SEQUENCE_MISMATCH")
                    data=message.get("data")
                    if not isinstance(data,str) or len(data)>1368:raise XiaoNuoError("INVALID_AUDIO_FRAME")
                    try:chunk=base64.b64decode(data,validate=True)
                    except ValueError:raise XiaoNuoError("INVALID_AUDIO_FRAME") from None
                    if not chunk or len(chunk)%2 or len(chunk)>1024 or len(pcm)+len(chunk)>request.audio_seconds*32000:
                        raise XiaoNuoError("INVALID_AUDIO_FRAME")
                    if not cancelled:pcm.extend(chunk)
                    sequence+=1
                    with self._lock:
                        self._capture_state="recording"
                        self._last_status_clock=time.monotonic()
                        self._last_seen=datetime.now(timezone.utc).isoformat()
                elif event=="audio_end":
                    finished=True
                    if cancelled or message.get("cancelled") or self._capture_cancel.is_set() or (request.external_cancel is not None and request.external_cancel.is_set()):
                        raise XiaoNuoError("AUDIO_CAPTURE_CANCELLED",409)
                    if message.get("failed") or not started or type(message.get("chunks")) is not int or type(message.get("samples")) is not int or message.get("chunks")!=sequence or message.get("samples")*2!=len(pcm) or len(pcm)!=request.audio_seconds*32000:
                        raise XiaoNuoError("AUDIO_CAPTURE_INCOMPLETE")
                    return {"pcm":bytes(pcm),"samples":len(pcm)//2,"sample_rate":16000,"channels":1,"bits":16,
                            "dc_filter":True,"audio_saved":False,"cloud_upload":False}
                elif event=="status":self._accept_status(message)
                elif event=="error":raise XiaoNuoError("DEVICE_AUDIO_ERROR")
            raise XiaoNuoError("DEVICE_COMMAND_TIMEOUT")
        finally:
            with self._lock:self._capture_state="idle"
            pcm.clear()
            if not finished:
                try:self._port.write(b"CAPTURE_CANCEL\n")
                except Exception:pass

    def _accept_status(self, message):
        if not isinstance(message.get("firmware"), str) or not re.match(r"0\.[345]\.",message["firmware"]) or not str(message.get("chip", "")).startswith("ESP32"):
            raise XiaoNuoError("UNEXPECTED_DEVICE_FIRMWARE")
        allowed = {"event", "firmware", "chip", "flash_bytes", "free_heap", "uptime_ms", "reset_reason", "state", "oled",
                   "oled_driver", "quiet", "host_connected", "wifi_connected", "wifi_attempts",'wifi_retry_cycles','wifi_retry_wait_ms', "ip", "setup_active",
                   "speaker_output", "voice_wake", "cloud_ai",'speaker_available','speaker_gain_divisor','speaker_max_seconds','psram_bytes','device_id','binding_id','task_state'}
        clean = {}
        for key in allowed:
            value = message.get(key)
            if isinstance(value, str):
                clean[key] = value[:80]
            elif type(value) in {int, bool}:
                clean[key] = value
        with self._lock:
            self._info = clean
            self._last_seen = datetime.now(timezone.utc).isoformat()
            self._last_status_clock = time.monotonic()

    def _exchange(self, command: str, event: str, timeout: float = 4) -> dict:
        if self._paused.is_set() or self._stop.is_set():
            raise XiaoNuoError("DEVICE_PAUSED")
        self._port.write((command + "\n").encode("ascii"))
        until = time.monotonic() + timeout
        while time.monotonic() < until and not self._stop.is_set() and not self._paused.is_set():
            raw = self._port.readline(2048)
            if not raw or len(raw) >= 2048:
                continue
            try:
                message = json.loads(raw)
            except (ValueError, UnicodeError):
                continue
            if not isinstance(message, dict):
                continue
            if message.get("event") == "status":
                self._accept_status(message)
            if message.get("event") == "error":
                if command == "INFO":
                    continue
                code = message.get("code", "UNKNOWN_DEVICE_ERROR")
                raise XiaoNuoError(code if isinstance(code, str) and re.fullmatch(r"[A-Z_]{1,60}", code) else "UNKNOWN_DEVICE_ERROR")
            if message.get("event") == event:
                return self._safe_reply(message)
        raise XiaoNuoError("DEVICE_COMMAND_TIMEOUT")

    @staticmethod
    def _safe_reply(message: dict) -> dict:
        fields = {"event", "addresses", "address", "driver", "visual_verified", "samples", "nonzero", "clipped", "min", "max", "rms",
                  "mean", "ac_rms", "right_rms", "warmup_frames", "sample_rate",
                  "audio_saved", "audio_uploaded", "ssid", "ip", "password_on_oled", "expires_seconds",
                  'binding_id','credential_saved','credential_removed','network_client_enabled'}
        if message.get('event')=='screen_test':
            fields={'event','duration_seconds','glyphs_ready','visual_verified'}
        if message.get('event')=='task_status':
            fields={'event','task_state','visual_verified'}
        if message.get('event')=='network_status':
            fields={'event','configured','connected','ready','state','pong_count','audio_upload_enabled','tls_verified'}
        if message.get('event')=='audio_status':
            fields={'event','active','last_event','last_error','written_samples','input_peak','output_peak','output_rms',
                    'sample_rate','bits','channels','gain_divisor','max_seconds','expected_bclk_hz','clock_measured','audible_verified',
                    'bclk_pin','lrc_pin','din_pin'}
        if message.get("event") == "status":
            fields = {"event", "firmware", "chip", "state", "oled", "oled_driver", "quiet", "wifi_connected", "ip", "setup_active",'device_id','binding_id','task_state'}
        result = {}
        for name in fields:
            value = message.get(name)
            if isinstance(value, str):
                result[name] = value[:80]
            elif type(value) in {bool, int}:
                result[name] = value
            elif type(value) is float and math.isfinite(value):
                result[name] = value
            elif name == "addresses" and isinstance(value, list):
                result[name] = [v for v in value[:120] if type(v) is int and 0 < v < 128]
        return result

    def _close(self):
        if self._port:
            try:
                self._port.close()
            except Exception:
                pass
        self._port = None
        with self._lock:
            self._connected = False
        self._disconnected.set()

    def _connect(self):
        with self._connection_gate:
            if self._paused.is_set() or self._stop.is_set():
                raise XiaoNuoError("DEVICE_PAUSED")
            self._disconnected.clear()
        self._port = self.factory()
        self._port.port = self.port_name
        self._port.baudrate = 460800
        self._port.timeout = 0.2
        self._port.write_timeout = 1
        self._port.dtr = False
        self._port.rts = False
        if self._paused.is_set() or self._stop.is_set():
            raise XiaoNuoError("DEVICE_PAUSED")
        self._port.open()
        self._stop.wait(0.3)
        # Terminate a partial command left by an interrupted previous session.
        # INFO already ignores unrelated boot/parser errors before its reply.
        self._port.write(b"\n")
        self._exchange("INFO", "status")
        with self._lock:
            self._connected = True
            self._error = None
            self._attempts = 0

    def _run(self):
        try:
            while not self._stop.is_set():
                if self._paused.is_set():
                    self._close()
                    self._stop.wait(0.2)
                    continue
                if self._port is None:
                    if self._attempts >= 5:
                        self._stop.wait(0.5)
                        continue
                    with self._lock:
                        self._attempts += 1
                    try:
                        self._connect()
                    except Exception as error:
                        with self._lock:
                            self._error = error.code if isinstance(error, XiaoNuoError) else "SERIAL_LINK_FAILED"
                        self._close()
                        self._stop.wait(1)
                        continue
                request = None
                try:
                    request = self._requests.get(timeout=0.1)
                except queue.Empty:
                    pass
                try:
                    if request is not None and not request.cancelled.is_set():
                        try:
                            if request.operation=="capture_audio":
                                request.result=self._capture_audio(request)
                            elif request.operation=='play_audio':
                                request.result=self._play_audio(request)
                            elif request.operation in {'bind_device','unbind_device'}:
                                request.result=self._binding_exchange(request)
                            else:
                                command,event=OPERATIONS[request.operation]
                                request.result = self._exchange(command, event)
                        except XiaoNuoError as error:
                            request.error = error.code
                        except Exception:
                            request.error = "SERIAL_LINK_FAILED"
                            raise
                        finally:
                            request.completed.set()
                    if time.monotonic() - self._last_tick >= 2:
                        self._exchange("INFO", "status")
                        desired = self.store.controls()["quiet"]
                        if self._info.get("quiet") != desired or self._info.get("state") == "SAFE_START":
                            self._exchange("STATE QUIET" if desired else "STATE IDLE", "status")
                        self._last_tick = time.monotonic()
                except Exception as error:
                    if request is not None and not request.completed.is_set():
                        request.error = "SERIAL_LINK_FAILED"
                        request.completed.set()
                    with self._lock:
                        self._error = error.code if isinstance(error, XiaoNuoError) else "SERIAL_LINK_FAILED"
                    self._close()
                    self._stop.wait(1)
        finally:
            self._close()
            while not self._requests.empty():
                request = self._requests.get_nowait()
                request.error = "DEVICE_STOPPED"
                request.completed.set()
