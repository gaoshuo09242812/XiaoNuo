"""Bound-device connection: v1 control only, v2 audio requires an injected handler.

Requires real TLS at the ASGI boundary: forwarded headers alone are not trusted.
Tokens travel only in Authorization, never URLs, logs, or responses.
"""
import asyncio
from contextlib import asynccontextmanager, contextmanager
from dataclasses import dataclass
import json
import re
import threading
from uuid import UUID

from starlette.websockets import WebSocketDisconnect

from .errors import XiaoNuoError
from .network_audio_session import NetworkAudioSession
from .computer_wake import reset_wake_context


@dataclass
class _Connection:
    done: asyncio.Event
    audio: NetworkAudioSession | None = None


class DeviceLinks:
    def __init__(self, registry, *, audio_handler=None):
        self.registry = registry
        self.audio_handler = audio_handler
        self._lock = threading.Lock()
        self._active = set()
        self._connections = {}
        self._stopping = set()
        self._lifecycle_lock = asyncio.Lock()
        self._accepting = True
        self._closed = False

    @asynccontextmanager
    async def suspended(self):
        """Owner operations and sockets must share the host's event loop.

        Stop accepting before cancelling: no reconnect may slip into a privacy
        operation. A cancelled native model worker is drained separately.
        """
        async with self._lifecycle_lock:
            self._accepting = False
            try:
                connections = list(self._connections.items())
                for task, connection in connections:
                    if connection.audio is not None:
                        connection.audio.cancelled.set()
                    self._stopping.add(task)
                    task.cancel()
                await reset_wake_context(self.audio_handler)
                if connections:
                    # ASGI middleware may keep the surrounding task alive after
                    # serve() returns. Only our own cleanup event proves drain.
                    try:
                        await asyncio.wait_for(asyncio.gather(*(
                            connection.done.wait() for _, connection in connections)), 4)
                    except TimeoutError:
                        raise XiaoNuoError('DEVICE_LINK_STOP_TIMEOUT', 503) from None
                yield
            finally:
                self._accepting = not self._closed and not self._connections

    def reminder_capable(self):
        if self._closed or not self._accepting:return False
        for connection in tuple(self._connections.values()):
            audio=connection.audio
            if audio is not None and audio.reply_reminders and not connection.done.is_set():
                try:audio.authorize()
                except XiaoNuoError:continue
                return True
        return False

    async def interrupt(self):
        async with self.suspended():
            pass

    async def shutdown(self):
        self._closed = True
        async with self.suspended():
            pass

    @contextmanager
    def claim(self, identifier):
        with self._lock:
            if identifier in self._active:
                raise XiaoNuoError('DEVICE_LINK_ALREADY_CONNECTED', 409)
            if len(self._active) >= 8:
                raise XiaoNuoError('DEVICE_LINK_CAPACITY', 503)
            self._active.add(identifier)
        try:
            yield
        finally:
            with self._lock:
                self._active.discard(identifier)

    async def message(self, socket, timeout, *, allow_binary=False):
        frame = await asyncio.wait_for(socket.receive(), timeout)
        if frame['type'] == 'websocket.disconnect':
            raise WebSocketDisconnect(frame.get('code', 1000))
        if allow_binary and isinstance(frame.get('bytes'),bytes):
            if len(frame['bytes'])>1024:raise XiaoNuoError('DEVICE_LINK_INVALID_FRAME',422)
            return frame['bytes']
        raw = frame.get('text')
        if not isinstance(raw, str) or len(raw.encode('utf-8')) > 1024:
            raise XiaoNuoError('DEVICE_LINK_INVALID_FRAME', 422)
        try:
            result = json.loads(raw)
        except ValueError:
            raise XiaoNuoError('DEVICE_LINK_INVALID_FRAME', 422) from None
        if not isinstance(result, dict):
            raise XiaoNuoError('DEVICE_LINK_INVALID_FRAME', 422)
        return result

    async def serve(self, socket):
        # Browser cookies, origin requests and query-string credentials cannot
        # become device sessions. Reverse-proxy deployment is a later stage.
        if (not self._accepting or socket.url.scheme != 'wss'
                or socket.query_params or socket.headers.get('origin')):
            await socket.close(code=1008)
            return
        authorization = socket.headers.get('authorization', '')
        if not re.fullmatch(r'Bearer [0-9a-f]{64}', authorization):
            await socket.close(code=1008)
            return
        token = authorization[7:]
        await socket.accept()
        if not self._accepting:
            await socket.close(code=1012, reason='DEVICE_LINK_STOPPED')
            return
        audio=None
        task = asyncio.current_task()
        # Track even a peer still waiting to authenticate its hello. Shutdown
        # must not leave this peer alive until the five-second hello deadline.
        connection = _Connection(asyncio.Event())
        self._connections[task] = connection
        send_lock=asyncio.Lock()
        async def send(message):
            async with send_lock:
                await socket.send_json(message)
        async def send_bytes(data):
            async with send_lock:
                await socket.send_bytes(data)
        try:
            hello = await self.message(socket, 20)
            fields = {'type', 'protocol', 'id', 'hardware_id', 'firmware'}
            if set(hello) not in (fields, fields | {'capabilities'}):
                raise XiaoNuoError('DEVICE_LINK_INVALID_HELLO', 422)
            if hello['type'] != 'hello' or type(hello['protocol']) is not int or hello['protocol'] not in {1,2,3,4,5}:
                raise XiaoNuoError('DEVICE_LINK_INVALID_HELLO', 422)
            http_reply_capable = False
            streaming_capable = False
            buffered_capable = False
            pipelined_capable = False
            reminder_capable = False
            if 'capabilities' in hello:
                capabilities = hello['capabilities']
                if (not isinstance(capabilities, dict) or not capabilities
                        or not set(capabilities)<={'reply_http','reply_streaming','reply_buffered','reply_pipelined','reply_reminders'}
                        or any(type(value) is not bool for value in capabilities.values())
                        or (any(capabilities.values()) and hello['protocol'] != 5)
                        or (sum(bool(capabilities.get(k)) for k in ('reply_http','reply_streaming','reply_buffered'))>1)
                        or (any(capabilities.get(k) for k in ('reply_pipelined','reply_reminders')) and not capabilities.get('reply_buffered'))):
                    raise XiaoNuoError('DEVICE_LINK_INVALID_HELLO', 422)
                http_reply_capable = capabilities.get('reply_http',False)
                streaming_capable = capabilities.get('reply_streaming',False)
                buffered_capable = capabilities.get('reply_buffered',False)
                pipelined_capable = capabilities.get('reply_pipelined',False)
                reminder_capable = bool(capabilities.get('reply_reminders',False) and
                    getattr(self.audio_handler,'reminder_audio',None) is not None and callable(getattr(self.audio_handler,'prepare_reminder',None)))
            try:
                identifier = str(UUID(hello['id']))
            except (ValueError, TypeError, AttributeError):
                raise XiaoNuoError('DEVICE_LINK_INVALID_HELLO', 422) from None
            hardware, firmware = hello['hardware_id'], hello['firmware']
            if (not isinstance(hardware, str) or not re.fullmatch(r'esp32-[0-9a-f]{12}', hardware)
                    or not isinstance(firmware, str) or not re.fullmatch(r'[A-Za-z0-9._-]{1,64}', firmware)):
                raise XiaoNuoError('DEVICE_LINK_INVALID_HELLO', 422)
            self.registry.heartbeat(identifier, token, hardware, firmware, 'NETWORK_IDLE')
            if not self._accepting:
                raise XiaoNuoError('DEVICE_LINK_STOPPED', 503)
            with self.claim(identifier):
                enabled=hello['protocol'] in {2,3,4,5} and self.audio_handler is not None
                if enabled:
                    def authorize():
                        self.registry.authorize(identifier,token,hardware)
                    audio=NetworkAudioSession(self.registry,identifier,authorize,send,self.audio_handler,
                                              send_bytes=send_bytes,reply_enabled=hello['protocol'] in {3,4,5},
                                              compressed_upload=hello['protocol'] in {4,5},
                                              compressed_reply=hello['protocol']==5,
                                              http_reply=getattr(self.audio_handler,'http_reply',None) if http_reply_capable else None,
                                              reply_streaming=streaming_capable,reply_buffered=buffered_capable,reply_pipelined=pipelined_capable,reply_reminders=reminder_capable)
                    connection.audio = audio
                await send({'type': 'ready', 'protocol': hello['protocol'], 'heartbeat_seconds': 10,
                            'audio_upload_enabled': enabled, 'commands': [],
                            'computer_wake_enabled':bool(enabled and hello['protocol'] in {3,4,5} and getattr(self.audio_handler,'wake_enabled',False) is True),
                            'chat_provider':getattr(self.audio_handler,'chat_provider','local') if enabled else 'disabled',
                            'quiet':self.registry.store.controls()['quiet'],
                            **({'reply_streaming_enabled':True} if enabled and streaming_capable else {}),
                            **({'reply_buffered_enabled':True} if enabled and buffered_capable else {}),
                            **({'reply_pipelined_enabled':True} if enabled and pipelined_capable else {}),
                            **({'reply_reminders_enabled':True,'reminder_poll_seconds':30} if enabled and reminder_capable else {})})
                expected = 1
                while True:
                    timeout=audio.receiver.remaining_seconds() if audio and audio.receiver else 25
                    if enabled:
                        message=await self.message(socket,timeout,allow_binary=True)
                    else:
                        message=await self.message(socket,timeout)
                    if enabled and (isinstance(message,bytes) or message.get('type')!='ping'):
                        await audio.handle(message)
                        continue
                    if (set(message) != {'type', 'sequence'} or message['type'] != 'ping'
                            or type(message['sequence']) is not int or message['sequence'] != expected
                            or expected > 1000000):
                        raise XiaoNuoError('DEVICE_LINK_INVALID_SEQUENCE', 422)
                    # Recheck revocation and privacy deletion on every message.
                    self.registry.heartbeat(identifier, token, hardware, firmware, 'NETWORK_IDLE')
                    await send({'type': 'pong', 'sequence': expected})
                    expected += 1
        except WebSocketDisconnect:
            pass
        except asyncio.CancelledError:
            if audio is not None:
                audio.cancelled.set()
                await audio.close()
            try:
                await asyncio.wait_for(socket.close(code=1012, reason='DEVICE_LINK_STOPPED'), 1)
            except Exception:
                pass
            if task not in self._stopping:
                raise
            task.uncancel()
        except TimeoutError:
            await socket.close(code=1008, reason='DEVICE_LINK_TIMEOUT')
        except XiaoNuoError as error:
            await socket.close(code=1008, reason=error.code)
        finally:
            try:
                if audio is not None:await audio.close()
            finally:
                self._connections.pop(task, None)
                self._stopping.discard(task)
                connection.done.set()
                token = ''
