import asyncio
import hmac
import os
from contextlib import asynccontextmanager
from uuid import UUID, uuid4
from decimal import Decimal
from typing import Literal

from fastapi import Depends, FastAPI, Request, Response, WebSocket
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import BaseModel, ConfigDict, Field, StrictBool, field_validator
from starlette.concurrency import run_in_threadpool

from .budgeting import BudgetManager, UsageLedger
from .config import Settings, load_settings
from .companion import CompanionService
from .errors import XiaoNuoError
from .history import ConversationStore
from .llm.health import ProviderHealthMonitor
from .llm.providers import PROVIDERS
from .llm.router import ModelRouter
from .prompts import SYSTEM_PROMPT
from .storage import CompanionStore
from .sessions import ConsoleSessions
from .config import ROOT
from .device_bridge import DeviceBridge, OPERATIONS
from .speech import VoiceService,WindowsChineseTTS,pcm_from_wav,MAX_PCM
from .neural_tts import KokoroTTS
from .local_runtime import LocalModelRuntime
from .network_reply_http import make_http_reply_uploader
from .reminders import ReminderService
from .greetings import GreetingService
from .devices import DeviceRegistry
from .device_link import DeviceLinks
from .network_speech import LocalNetworkSpeech
from datetime import datetime
from dataclasses import replace


class StrictRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")


class MemoryCreate(StrictRequest):
    request_id: UUID = Field(default_factory=uuid4)
    content: str = Field(min_length=1, max_length=1000)
    kind: Literal["preference", "important", "goal"] = "important"
    consent: StrictBool
    allow_remote: StrictBool = False
    retention_days: int = Field(default=365, ge=1, le=3650)
    retain_indefinitely: StrictBool = False


class MemoryUpdate(StrictRequest):
    content: str = Field(min_length=1, max_length=1000)
    consent: StrictBool
    expected_version: int = Field(ge=1)
    allow_remote: StrictBool = False


class ControlsPatch(StrictRequest):
    reply_length: Literal["brief", "normal", "detailed"] | None = None
    serious: StrictBool | None = None
    followup_enabled: StrictBool | None = None
    tone_analysis_enabled: StrictBool | None = None
    speech_enabled: StrictBool | None = None
    computer_wake_enabled: StrictBool | None = None
    volume: float | None = Field(default=None, ge=0, le=1)
    speech_speed: float | None = Field(default=None, ge=0.7, le=1.4)
    voice_profile: Literal['soft','clear','bright'] | None = None
    greeting_enabled: StrictBool | None = None
    memory_upload_enabled: StrictBool | None = None
    persist_history: StrictBool | None = None
    history_retention_days: int | None = Field(default=None, ge=1, le=30)
    quiet_hours_start: str | None = None
    quiet_hours_end: str | None = None
    timezone: Literal["Asia/Shanghai", "UTC"] | None = None


class QuietRequest(StrictRequest):
    enabled: StrictBool


class BudgetUpdate(StrictRequest):
    monthly_usd: Decimal | None = Field(default=None, ge=0, le=1000000)
    server_usd: Decimal | None = Field(default=None, ge=0, le=1000000)
    monthly_cny: Decimal | None = Field(default=None, ge=0, le=1000000)
    server_cny: Decimal | None = Field(default=None, ge=0, le=1000000)


class InvoiceConfirmation(StrictRequest):
    confirmed_usd: Decimal = Field(ge=0, le=1000000)
    invoice_reference: str = Field(min_length=1, max_length=120)
    confirmed: StrictBool


class DataDeletion(StrictRequest):
    confirm: Literal["DELETE_PERSONAL_DATA"]


class TicketExchange(StrictRequest):
    ticket: str = Field(min_length=20, max_length=200)


class DeviceCommand(StrictRequest):
    operation: str = Field(min_length=1, max_length=40)


class DeviceEnrollment(StrictRequest):
    name: str = Field(default='小糯',min_length=1,max_length=40)


class AudioFeedback(StrictRequest):
    result: Literal['unverified','heard','no_sound','unclear']


class DeviceHeartbeat(StrictRequest):
    id: UUID
    hardware_id: str = Field(pattern=r'^esp32-[0-9a-f]{12}$')
    firmware: str = Field(min_length=1,max_length=40,pattern=r'^[A-Za-z0-9_.-]+$')
    state: Literal['IDLE','LISTENING','THINKING','SPEAKING','QUIET','OFFLINE','ERROR']


class VoiceTurnRequest(StrictRequest):
    consent: Literal['local-only']
    seconds: int = Field(default=6,ge=2,le=6,strict=True)
    request_id: UUID = Field(default_factory=uuid4)
    conversation_id: UUID | None = None
    reply_playback: Literal['preview','device'] = 'preview'


class SynthesizeRequest(StrictRequest):
    text: str = Field(min_length=1,max_length=500)
    request_id: UUID = Field(default_factory=uuid4)


class DevicePlaybackRequest(StrictRequest):
    token: str = Field(min_length=20,max_length=100,pattern=r'^[A-Za-z0-9_-]+$')
    request_id: UUID = Field(default_factory=uuid4)


class ReminderRequest(StrictRequest):
    text: str = Field(min_length=1,max_length=500)
    due_at: datetime
    timezone: Literal['Asia/Shanghai','UTC'] = 'Asia/Shanghai'
    repeat_rule: Literal['none','daily','weekly'] = 'none'
    quiet_exception: StrictBool = False
    request_id: UUID = Field(default_factory=uuid4)


class ReminderUpdate(ReminderRequest):
    expected_version: int = Field(ge=1,strict=True)


class ChatRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    text: str = Field(min_length=1, max_length=2000)
    request_id: UUID = Field(default_factory=uuid4)
    conversation_id: UUID | None = None

    @field_validator("text")
    @classmethod
    def nonempty(cls, value):
        if not value.strip():
            raise ValueError("Text cannot be blank")
        return value.strip()


def build_router(settings: Settings) -> ModelRouter:
    ledger = UsageLedger(settings.database)
    providers = {spec.name: PROVIDERS[spec.name](spec, timeout=settings.timeout_seconds) for spec in settings.providers}
    return ModelRouter(settings, providers, BudgetManager(ledger, settings), ProviderHealthMonitor(ledger))


def create_app(settings: Settings | None = None, *, network_audio_enabled=False, usb_enabled=True, network_chat_provider=None,computer_wake=None) -> FastAPI:
    settings = settings or load_settings()
    network_chat_provider = network_chat_provider or settings.network_chat_provider
    if network_chat_provider not in {'local','deepseek'}:
        raise ValueError('Unsupported network chat provider')
    if network_audio_enabled and network_chat_provider=='local' and (settings.remote_enabled or settings.allow_paid):
        raise ValueError('Network audio host requires local-only, unpaid processing')
    if network_audio_enabled and network_chat_provider=='deepseek':
        settings=replace(settings,providers=tuple(p for p in settings.providers if p.name in {'deepseek','local_qwen'}))
    if len(settings.admin_token) < 32:
        raise ValueError("Run 'python manage.py init' before starting the backend")
    router = build_router(settings)
    store = CompanionStore(router.budget.ledger)
    reminders=ReminderService(store)
    greetings=GreetingService(store)
    sessions = ConsoleSessions(router.budget.ledger, settings.admin_token)
    bridge = DeviceBridge(settings, store)
    devices=DeviceRegistry(store,bridge)
    companion = CompanionService(router, store,reminders=reminders,device_status=bridge.status)
    voice = VoiceService(settings.root,bridge,companion,router.budget,store,
                         tts=KokoroTTS(settings.root,persistent=True) if settings.tts_provider=='kokoro_onnx' else WindowsChineseTTS(settings.root))
    local_runtime=LocalModelRuntime(settings)
    @asynccontextmanager
    async def lifespan(application):
        try:
            await run_in_threadpool(local_runtime.start)
            if local_runtime.ready:
                try:await run_in_threadpool(router.verify_local)
                except XiaoNuoError as error:
                    local_runtime.error=error.code
                    await run_in_threadpool(local_runtime.stop)
            if usb_enabled:
                bridge.start()
            if isinstance(voice.tts,KokoroTTS):
                await run_in_threadpool(voice.tts.warmup)
            reminders.start()
            yield
        finally:
            try:
                await run_in_threadpool(voice.cancel)
                await device_links.shutdown()
                handler = device_links.audio_handler
                if handler is not None and hasattr(handler, 'clear_context'):
                    await handler.clear_context()
            finally:
                reminders.stop()
                bridge.stop()
                await run_in_threadpool(local_runtime.stop)
    app = FastAPI(title="XiaoNuo", version="0.5.5", docs_url=None, redoc_url=None, openapi_url=None, lifespan=lifespan)
    app.state.companion = companion
    app.state.console_sessions = sessions
    app.state.device_bridge = bridge
    app.state.devices=devices
    handler = (LocalNetworkSpeech(settings.root, voice.asr, voice.tts, companion,
                router.budget, store, work_lock=voice._turn,chat_provider=network_chat_provider,
                computer_wake=settings.computer_wake_enabled if computer_wake is None else computer_wake) if network_audio_enabled else None)
    if handler is not None:
        reply_base=os.environ.get('XIAONUO_REPLY_HTTP_BASE','').rstrip('/')
        reply_host=os.environ.get('XIAONUO_REPLY_HOST_TOKEN','')
        reply_device=os.environ.get('XIAONUO_REPLY_DEVICE_HASH','')
        if reply_base.startswith('https://') and len(reply_host)==64 and len(reply_device)==64:
            handler.http_reply=make_http_reply_uploader(reply_base,reply_host,reply_device)
    device_links = DeviceLinks(devices, audio_handler=handler)
    companion.reminder_voice_available=device_links.reminder_capable
    app.state.device_links = device_links
    app.state.local_runtime = local_runtime
    app.state.voice_service = voice
    app.state.reminders=reminders
    app.state.greetings=greetings
    bearer = HTTPBearer(auto_error=False)

    def check_origin(request: Request):
        origin = request.headers.get("origin")
        expected = str(request.base_url).rstrip("/")
        if origin and origin != expected:
            raise XiaoNuoError("CROSS_ORIGIN_REQUEST_DENIED", 403)

    def authenticated(request: Request, credentials: HTTPAuthorizationCredentials | None = Depends(bearer)):
        if credentials is not None:
            if hmac.compare_digest(credentials.credentials.encode(), settings.admin_token.encode()):
                return
            raise XiaoNuoError("UNAUTHORIZED", 401)
        token = request.cookies.get(sessions.cookie_name, "")
        if not sessions.valid(token):
            raise XiaoNuoError("UNAUTHORIZED", 401)
        if request.method not in {"GET", "HEAD", "OPTIONS"}:
            check_origin(request)
            provided = request.headers.get("x-xiaonuo-csrf", "")
            if not hmac.compare_digest(provided.encode(), sessions.csrf(token).encode()):
                raise XiaoNuoError("CSRF_CHECK_FAILED", 403)

    @app.exception_handler(XiaoNuoError)
    async def known_error(request: Request, error: XiaoNuoError):
        return JSONResponse({"error": error.code}, status_code=error.status)

    @app.exception_handler(RequestValidationError)
    async def invalid_request(request: Request, error: RequestValidationError):
        # Pydantic's default error payload can echo private request values.
        return JSONResponse({"error": "INVALID_REQUEST", "fields": [list(e["loc"]) for e in error.errors()]}, status_code=422)

    @app.exception_handler(Exception)
    async def internal_error(request: Request, error: Exception):
        return JSONResponse({"error": "INTERNAL_ERROR"}, status_code=500)

    @app.middleware("http")
    async def request_limit(request: Request, call_next):
        # Buffer at most 16 KiB, including chunked bodies, before JSON parsing.
        if request.method in {"POST", "PUT", "PATCH"}:
            size = 0
            pieces = []
            try:
                async with asyncio.timeout(10):
                    async for piece in request.stream():
                        size += len(piece)
                        limit = MAX_PCM+4096 if request.url.path=="/api/v1/voice/transcribe" else 16384
                        if size > limit:
                            return JSONResponse({"error": "REQUEST_TOO_LARGE"}, status_code=413)
                        pieces.append(piece)
            except TimeoutError:
                return JSONResponse({"error": "REQUEST_BODY_TIMEOUT"}, status_code=408)
            request._body = b"".join(pieces)
        response = await call_next(request)
        response.headers["Cache-Control"] = "no-store"
        response.headers["X-Content-Type-Options"] = "nosniff"
        return response

    @app.get("/healthz")
    def health():
        return {"service": "xiaonuo", "status": "ready"}

    @app.post("/api/v1/session/bootstrap", dependencies=[Depends(authenticated)])
    def login_ticket():
        return {"ticket": sessions.ticket(), "expires_in_seconds": 120}

    @app.post("/api/v1/session/exchange")
    def exchange_ticket(body: TicketExchange, request: Request, response: Response):
        check_origin(request)
        token = sessions.exchange(body.ticket)
        response.set_cookie(sessions.cookie_name, token, max_age=8 * 3600, httponly=True,
                            secure=request.url.scheme == "https", samesite="strict", path="/")
        return {"authenticated": True, "csrf": sessions.csrf(token)}

    @app.get("/api/v1/session", dependencies=[Depends(authenticated)])
    def session_status(request: Request):
        token = request.cookies.get(sessions.cookie_name, "")
        return {"authenticated": True, "csrf": sessions.csrf(token) if token else ""}

    @app.post("/api/v1/session/logout", dependencies=[Depends(authenticated)])
    def logout(request: Request, response: Response):
        sessions.logout(request.cookies.get(sessions.cookie_name, ""))
        response.delete_cookie(sessions.cookie_name, path="/")
        return {"logged_out": True}

    @app.get("/api/v1/status", dependencies=[Depends(authenticated)])
    def status():
        providers = []
        for name, provider in router.providers.items():
            free = name in {"gemini", "groq",'local_qwen'}
            providers.append({"provider": name, "model": provider.spec.model,
                              "credential_configured": bool(provider.spec.key),
                              "block_reason": router.blocked_reason(provider, free),
                              **router.health.status(provider.spec)})
        return {"phase": "P5-local-voice", "model_mode": settings.mode.value,
                'worker_run_id':getattr(app.state,'worker_run_id',None),
                'relay_states':[record.get('state') for record in getattr(app.state,'relay_status',{}).get('devices',{}).values()],
                'relay_errors':[record.get('last_error') for record in getattr(app.state,'relay_status',{}).get('devices',{}).values()],
                "remote_enabled": settings.remote_enabled, "allow_paid": settings.allow_paid,
                'local_llm_enabled':settings.local_llm_enabled,
                'local_llm_ready':local_runtime.ready,'local_llm_error':local_runtime.error,
                'network_audio_enabled':device_links.audio_handler is not None,
                'network_chat_provider':network_chat_provider,
                "usage_region": settings.usage_region, "deployment_region": settings.deployment_region,
                "primary_paid": settings.primary_paid, "providers": providers,
                "budget": router.budget.snapshot(), "audio_available": bool(voice.asr.last_success and voice.tts.last_success),
                "long_term_memory_available": True, "backend_quiet_policy_available": True,
                "context_analysis": "text_rules_only", "hardware_verified": False}

    @app.post('/api/v1/greetings/visit',dependencies=[Depends(authenticated)])
    def greeting_visit():
        return greetings.visit()

    @app.get("/api/v1/device", dependencies=[Depends(authenticated)])
    def device_status():
        return bridge.status()

    @app.get('/api/v1/devices',dependencies=[Depends(authenticated)])
    def list_devices():
        return {'items':devices.list(),'network_client_enabled':False}

    @app.post('/api/v1/devices/enroll',dependencies=[Depends(authenticated)])
    def enroll_device(body: DeviceEnrollment):
        return devices.enroll(body.name)

    @app.delete('/api/v1/devices/{identifier}',dependencies=[Depends(authenticated)])
    def revoke_device(identifier: UUID):
        return devices.revoke(str(identifier))

    @app.put('/api/v1/devices/{identifier}/audio-feedback',dependencies=[Depends(authenticated)])
    def audio_feedback(identifier: UUID,body: AudioFeedback):
        return devices.feedback(str(identifier),body.result)

    @app.websocket('/api/v1/device-link/ws')
    async def device_websocket(socket: WebSocket):
        await device_links.serve(socket)

    @app.post('/api/v1/device-link/heartbeat')
    def device_heartbeat(body: DeviceHeartbeat,request: Request,credentials: HTTPAuthorizationCredentials|None=Depends(bearer)):
        if request.url.scheme!='https':raise XiaoNuoError('DEVICE_TLS_REQUIRED',403)
        if credentials is None:raise XiaoNuoError('DEVICE_UNAUTHORIZED',401)
        return devices.heartbeat(str(body.id),credentials.credentials,body.hardware_id,body.firmware,body.state)

    @app.post("/api/v1/device/command", dependencies=[Depends(authenticated)])
    def device_command(body: DeviceCommand):
        return bridge.execute(body.operation)

    @app.post("/api/v1/device/retry", dependencies=[Depends(authenticated)])
    def device_retry():
        bridge.retry()
        return bridge.status()

    @app.post("/api/v1/device/disconnect", dependencies=[Depends(authenticated)])
    def device_disconnect():
        bridge.pause()
        return bridge.status()

    @app.get("/api/v1/voice",dependencies=[Depends(authenticated)])
    def voice_status():
        return voice.status()

    @app.get('/api/v1/reminders',dependencies=[Depends(authenticated)])
    def list_reminders():
        return {'items':reminders.list(),'delivery':'console','device_audio_capable':device_links.reminder_capable(),'last_tick':reminders.last_tick,'last_error':reminders.last_error}

    @app.post('/api/v1/reminders',dependencies=[Depends(authenticated)])
    def create_reminder(body: ReminderRequest):
        return reminders.create(body.text,body.due_at,body.timezone,body.repeat_rule,body.quiet_exception,str(body.request_id))

    @app.patch('/api/v1/reminders/{identifier}',dependencies=[Depends(authenticated)])
    def update_reminder(identifier: UUID,body: ReminderUpdate):
        return reminders.update(str(identifier),text=body.text,due_at=body.due_at,zone=body.timezone,
                                repeat=body.repeat_rule,quiet_exception=body.quiet_exception,expected_version=body.expected_version)

    @app.delete('/api/v1/reminders/{identifier}',dependencies=[Depends(authenticated)])
    def cancel_reminder(identifier: UUID):
        reminders.cancel(str(identifier))
        return {'cancelled':True}

    @app.get('/api/v1/reminder-events',dependencies=[Depends(authenticated)])
    def reminder_events():
        return {'items':reminders.notifications(),'delivery':'console','audio_auto_play':False,'device_audio_capable':device_links.reminder_capable()}

    @app.post('/api/v1/reminder-events/{identifier}/acknowledge',dependencies=[Depends(authenticated)])
    def acknowledge_reminder(identifier: UUID):
        reminders.acknowledge(str(identifier))
        return {'acknowledged':True}

    @app.post("/api/v1/voice/device",dependencies=[Depends(authenticated)])
    def device_voice(body: VoiceTurnRequest):
        return voice.device_turn(body.seconds,str(body.request_id),str(body.conversation_id) if body.conversation_id else None,body.consent,body.reply_playback)

    @app.post("/api/v1/voice/cancel",dependencies=[Depends(authenticated)])
    async def cancel_voice():
        await run_in_threadpool(voice.cancel)
        await device_links.interrupt()
        return {"cancel_requested":True}

    @app.post("/api/v1/voice/transcribe",dependencies=[Depends(authenticated)])
    async def transcribe_voice(request: Request):
        if request.headers.get("x-xiaonuo-audio-consent")!='local-only':
            raise XiaoNuoError("AUDIO_CONSENT_REQUIRED",422)
        pcm=pcm_from_wav(await request.body())
        return await run_in_threadpool(voice.transcribe,pcm,str(uuid4()))

    @app.post("/api/v1/voice/synthesize",dependencies=[Depends(authenticated)])
    def synthesize_voice(body: SynthesizeRequest):
        return voice.synthesize_preview(body.text,str(body.request_id))

    @app.post('/api/v1/voice/play-device',dependencies=[Depends(authenticated)])
    def play_device_voice(body: DevicePlaybackRequest):
        return voice.play_on_device(body.token,str(body.request_id))

    @app.get("/api/v1/voice/audio/{token}",dependencies=[Depends(authenticated)])
    def get_audio(token: str):
        controls=store.controls()
        if controls['quiet'] or not controls['speech_enabled']:
            voice.cache.clear()
            raise XiaoNuoError("SPEECH_DISABLED_BY_OWNER",409)
        return Response(content=voice.cache.get(token),media_type="audio/wav")

    @app.post("/api/v1/chat", dependencies=[Depends(authenticated)])
    def chat(body: ChatRequest):
        return companion.chat(body.text, str(body.request_id), str(body.conversation_id) if body.conversation_id else None)

    @app.delete("/api/v1/conversations/{conversation_id}", dependencies=[Depends(authenticated)])
    def delete_conversation(conversation_id: UUID):
        companion.history.delete(str(conversation_id))
        return {"deleted": True}

    @app.get("/api/v1/preferences", dependencies=[Depends(authenticated)])
    def preferences():
        return store.controls()

    @app.patch("/api/v1/preferences", dependencies=[Depends(authenticated)])
    async def preferences_update(body: ControlsPatch):
        result=await run_in_threadpool(store.patch_controls,body.model_dump(exclude_none=True))
        # Both directions require a fresh capability handshake. An already
        # connected device must learn that waking was enabled as well.
        if body.computer_wake_enabled is not None:await device_links.interrupt()
        return result

    @app.put("/api/v1/quiet", dependencies=[Depends(authenticated)])
    async def quiet_update(body: QuietRequest):
        await run_in_threadpool(store.set_quiet, body.enabled)
        if body.enabled:
            await run_in_threadpool(voice.cancel)
            await device_links.interrupt()
            voice.cache.clear()
        return store.controls()

    @app.get("/api/v1/memories", dependencies=[Depends(authenticated)])
    def memories():
        return {"items": store.list_memories()}

    @app.post("/api/v1/memories", dependencies=[Depends(authenticated)])
    def memory_create(body: MemoryCreate):
        values = body.model_dump()
        values["request_id"] = str(body.request_id)
        return store.add_memory(**values)

    @app.patch("/api/v1/memories/{memory_id}", dependencies=[Depends(authenticated)])
    def memory_update(memory_id: UUID, body: MemoryUpdate):
        return store.update_memory(str(memory_id), **body.model_dump())

    @app.delete("/api/v1/memories/{memory_id}", dependencies=[Depends(authenticated)])
    def memory_delete(memory_id: UUID):
        store.delete_memory(str(memory_id))
        return {"deleted": True}

    @app.get("/api/v1/privacy/export", dependencies=[Depends(authenticated)])
    def export_personal_data():
        return store.export()

    @app.delete("/api/v1/privacy/data", dependencies=[Depends(authenticated)])
    async def delete_personal_data(body: DataDeletion):
        async with device_links.suspended():
            await run_in_threadpool(voice.cancel)
            handler = device_links.audio_handler
            if handler is not None:
                clear_context = getattr(handler, 'clear_context', None)
                if clear_context is None:
                    raise XiaoNuoError('NETWORK_PRIVACY_LIFECYCLE_REQUIRED', 503)
                await clear_context()
            await run_in_threadpool(companion.history.clear)
            await run_in_threadpool(store.clear_personal_data)
        return {"personal_data_deleted": True, "financial_audit_records_retained": True}

    @app.get("/api/v1/budget", dependencies=[Depends(authenticated)])
    def budget():
        return router.budget.snapshot()

    @app.put("/api/v1/budget", dependencies=[Depends(authenticated)])
    def budget_update(body: BudgetUpdate):
        if (body.monthly_cny is None)==(body.monthly_usd is None):
            raise XiaoNuoError('BUDGET_CURRENCY_REQUIRED', 422)
        if body.monthly_cny is not None:
            if body.server_usd is not None:raise XiaoNuoError('BUDGET_CURRENCY_MISMATCH', 422)
            router.budget.set_limits_cny(body.monthly_cny, body.server_cny)
        else:
            if body.server_cny is not None:raise XiaoNuoError('BUDGET_CURRENCY_MISMATCH', 422)
            router.budget.set_limits(body.monthly_usd, body.server_usd)
        return router.budget.snapshot()

    @app.post("/api/v1/budget/reservations/{reservation_id}/reconcile", dependencies=[Depends(authenticated)])
    def reconcile(reservation_id: UUID, body: InvoiceConfirmation):
        if body.confirmed is not True:
            raise XiaoNuoError("INVOICE_CONFIRMATION_REQUIRED", 422)
        router.budget.reconcile(str(reservation_id), body.confirmed_usd, body.invoice_reference)
        return router.budget.snapshot()

    dashboard = ROOT / "dashboard"
    if dashboard.is_dir():
        app.mount("/assets", StaticFiles(directory=dashboard), name="dashboard-assets")

        @app.get("/", include_in_schema=False)
        def console():
            return FileResponse(dashboard / "index.html", headers={"Content-Security-Policy": "default-src 'self'; style-src 'self'; script-src 'self'; connect-src 'self'; img-src 'self' data:; object-src 'none'; frame-ancestors 'none'; base-uri 'none'"})

    return app
