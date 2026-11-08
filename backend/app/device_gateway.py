"""Dedicated TLS device listener: never mounts the owner's console or data API."""
from contextlib import asynccontextmanager
from fastapi import FastAPI, WebSocket

from .budgeting import UsageLedger
from .config import load_settings
from .device_link import DeviceLinks
from .devices import DeviceRegistry
from .storage import CompanionStore


class NoUsbBridge:
    def status(self):
        return {'connected':False,'paused':True,'info':{}}


def create_device_gateway(settings=None, *, registry=None, audio_handler=None, device_links=None):
    if device_links is not None and (registry is not None or audio_handler is not None):
        raise ValueError('Shared device links cannot be combined with a different registry or handler')
    if registry is None and device_links is None:
        settings = settings or load_settings()
        store = CompanionStore(UsageLedger(settings.database))
        registry = DeviceRegistry(store, NoUsbBridge())
    links = device_links if device_links is not None else DeviceLinks(registry,audio_handler=audio_handler)
    @asynccontextmanager
    async def lifespan(app):
        try:
            yield
        finally:
            if device_links is None:
                await links.shutdown()
    app = FastAPI(title='XiaoNuo device link',docs_url=None,redoc_url=None,openapi_url=None,
                  lifespan=lifespan)
    app.state.device_links = links

    @app.get('/healthz')
    def health():
        return {'service':'xiaonuo-device-link','audio_upload_enabled':links.audio_handler is not None}

    @app.websocket('/api/v1/device-link/ws')
    async def connection(socket: WebSocket):
        await links.serve(socket)

    return app
