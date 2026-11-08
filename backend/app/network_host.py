"""One model/owner lifecycle, two loopback listeners, one cancellation domain."""
import asyncio
from contextlib import contextmanager
from pathlib import Path
import signal
import socket
import ssl
import threading

import uvicorn

from .device_gateway import create_device_gateway
from .errors import XiaoNuoError
from .main import create_app


def create_network_host(settings, *, enable_audio=False, usb_enabled=False,chat_provider=None,computer_wake=None):
    owner = create_app(settings, network_audio_enabled=enable_audio, usb_enabled=usb_enabled,
                       network_chat_provider=chat_provider,computer_wake=computer_wake)
    gateway = create_device_gateway(device_links=owner.state.device_links)
    return owner, gateway


class _Listener(uvicorn.Server):
    @contextmanager
    def capture_signals(self):
        # The supervisor owns process signals; listeners must not replace each
        # other's handlers or send a captured signal back to this process.
        yield


@contextmanager
def stop_signals(stop):
    if threading.current_thread() is not threading.main_thread():
        yield
        return
    loop = asyncio.get_running_loop()
    def request_stop(signum, frame):
        loop.call_soon_threadsafe(stop.set)
    previous = {sig: signal.signal(sig, request_stop) for sig in (signal.SIGINT, signal.SIGTERM)}
    try:
        yield
    finally:
        for sig, handler in previous.items():
            signal.signal(sig, handler)


def _bind(port):
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        if hasattr(socket, 'SO_EXCLUSIVEADDRUSE'):
            listener.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        listener.bind(('127.0.0.1', port))
        listener.setblocking(False)
        return listener
    except BaseException:
        listener.close()
        raise


def check_audio_readiness(owner):
    handler=owner.state.device_links.audio_handler
    if handler is not None:
        if handler.chat_provider=='deepseek':
            from .network_chat import require_deepseek_chat
            require_deepseek_chat(owner.state.companion.router,preflight=True)
        elif not owner.state.local_runtime.ready:
            raise XiaoNuoError('NETWORK_LOCAL_MODEL_NOT_READY',503)


async def serve_network_host(owner, gateway, cert: Path, key: Path, *,
                             console_port=8765, device_port=8768, stop=None, ready=None):
    """Both listeners stop if either exits. No listener accepts remote sockets.

    Callers own the process locks. Port zero is supported for isolated tests.
    ready, when supplied, is a Future receiving the actual loopback ports.
    """
    if owner.state.device_links is not gateway.state.device_links:
        raise ValueError('Both listeners must share device links')
    if console_port == device_port and console_port != 0:
        raise ValueError('Console and device ports must differ')
    # Verify key pairing and reserve both ports before expensive model startup.
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain(str(cert), str(key))
    sockets = []
    try:
        sockets.append(_bind(console_port))
        sockets.append(_bind(device_port))
        common = dict(host='127.0.0.1', workers=1, lifespan='off',
            proxy_headers=False, access_log=False, log_level='warning',
            ws_max_size=1024, ws_max_queue=4, ws_per_message_deflate=False,
            timeout_keep_alive=5, timeout_graceful_shutdown=5)
        servers = [_Listener(uvicorn.Config(owner, port=sockets[0].getsockname()[1], **common)),
                   _Listener(uvicorn.Config(gateway, port=sockets[1].getsockname()[1],
                       ssl_certfile=str(cert), ssl_keyfile=str(key), **common))]
        stop = stop if stop is not None else asyncio.Event()
        async with owner.router.lifespan_context(owner):
            check_audio_readiness(owner)
            tasks = []
            stop_task = None
            try:
                tasks = [asyncio.create_task(server.serve(sockets=[listener]))
                         for server, listener in zip(servers, sockets)]
                async with asyncio.timeout(10):
                    while not all(server.started for server in servers):
                        if any(task.done() for task in tasks):
                            raise XiaoNuoError('NETWORK_LISTENER_START_FAILED', 503)
                        if stop.is_set():
                            return
                        await asyncio.sleep(.01)
                if ready is not None and not ready.done():
                    ready.set_result(tuple(listener.getsockname()[1] for listener in sockets))
                stop_task = asyncio.create_task(stop.wait())
                finished, _ = await asyncio.wait([*tasks, stop_task], return_when=asyncio.FIRST_COMPLETED)
                if stop_task not in finished:
                    raise XiaoNuoError('NETWORK_LISTENER_EXITED', 503)
            finally:
                for server in servers:
                    server.should_exit = True
                if stop_task is not None:
                    stop_task.cancel()
                    await asyncio.gather(stop_task, return_exceptions=True)
                try:
                    await owner.state.device_links.shutdown()
                finally:
                    if tasks:
                        done, pending = await asyncio.wait(tasks, timeout=12)
                        for task in pending:
                            task.cancel()
                        await asyncio.gather(*tasks, return_exceptions=True)
    finally:
        for listener in sockets:
            listener.close()
