from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from enum import StrEnum
import os
from pathlib import Path
import re

from dotenv import dotenv_values
from .local_models import LOCAL_MODELS

ROOT = Path(__file__).resolve().parents[2]


class Mode(StrEnum):
    PAID_FIRST = "PAID_FIRST"
    FREE_FIRST = "FREE_FIRST"
    FREE_ONLY = "FREE_ONLY"
    PAID_ONLY = "PAID_ONLY"
    DISABLED = "DISABLED"


@dataclass(frozen=True)
class ProviderSpec:
    name: str
    model: str
    key: str = field(repr=False, default="")
    free_account_confirmed: bool = False
    region_approved: bool = False


@dataclass(frozen=True)
class Settings:
    root: Path = ROOT
    admin_token: str = field(repr=False, default="")
    mode: Mode = Mode.PAID_FIRST
    remote_enabled: bool = False
    allow_paid: bool = False
    usage_region: str = "CN"
    deployment_region: str = "UNDECIDED"
    primary_paid: str = "openai"
    monthly_budget: Decimal = Decimal("5.00")
    monthly_server_cost: Decimal | None = None
    monthly_budget_cny: Decimal | None = None
    monthly_server_cost_cny: Decimal | None = None
    cny_to_usd: Decimal | None = None
    fx_checked_on: str = ""
    max_output_tokens: int = 512
    timeout_seconds: int = 30
    providers: tuple[ProviderSpec, ...] = ()
    usb_port: str = ""
    local_llm_enabled: bool = False
    tts_provider: str = 'kokoro_onnx'
    local_model: str = 'Qwen3-0.6B-Q8_0'
    device_speaker_enabled: bool = False
    network_chat_provider: str = 'local'
    computer_wake_enabled: bool = False

    @property
    def database(self) -> Path:
        return self.root / "data" / "xiaonuo.db"


def load_settings(env_path: Path | None = None) -> Settings:
    target = env_path or ROOT / ".env.local"
    local = {k: v for k, v in dotenv_values(target, interpolate=False).items() if v is not None}
    values = dict(os.environ)
    values.update(local)
    # A selected local credential file must not silently borrow another process's key.
    if target.exists():
        for key in ("OPENAI_API_KEY", "GEMINI_API_KEY", "GROQ_API_KEY", "DEEPSEEK_API_KEY"):
            values[key] = local.get(key, "")

    def flag(name: str, default: bool = False) -> bool:
        value = values.get(name, str(default)).strip().lower()
        if value not in {"true", "false", "1", "0"}:
            raise ValueError(f"Invalid boolean setting: {name}")
        return value in {"true", "1"}

    def number(name: str, default: str = "") -> Decimal | None:
        value = values.get(name, default).strip()
        if not value:
            return None
        try:
            parsed = Decimal(value)
        except InvalidOperation:
            raise ValueError(f"Invalid decimal setting: {name}") from None
        if not parsed.is_finite() or parsed < 0:
            raise ValueError(f"Invalid nonnegative setting: {name}")
        return parsed

    region = values.get("XIAONUO_USAGE_REGION", "CN").strip().upper()
    deployment = values.get("XIAONUO_DEPLOYMENT_REGION", "UNDECIDED").strip().upper()
    if not re.fullmatch(r"[A-Z]{2}", region) or (deployment != "UNDECIDED" and not re.fullmatch(r"[A-Z]{2}", deployment)):
        raise ValueError("Regions must use two-letter country codes; deployment may be UNDECIDED")
    specs = []
    defaults = {"openai": "gpt-6-luna", "deepseek": "deepseek-flash", "groq": "openai/gpt-oss-20b", "gemini": ""}
    for name, model in defaults.items():
        prefix = name.upper()
        allowed = flag(f"XIAONUO_{prefix}_REGION_APPROVED")
        if region == "CN" and name in {"openai", "gemini"}:
            allowed = False
        specs.append(ProviderSpec(name, values.get(f"{prefix}_MODEL", model),
                                  values.get(f"{prefix}_API_KEY", ""),
                                  flag(f"XIAONUO_{prefix}_FREE_ACCOUNT_CONFIRMED"), allowed))
    local_enabled=flag("XIAONUO_LOCAL_LLM_ENABLED")
    local_model=values.get('XIAONUO_LOCAL_MODEL','Qwen3-0.6B-Q8_0')
    if local_model not in LOCAL_MODELS:raise ValueError('Unsupported local model')
    local_key_file=target.resolve().parent/'data/local-llm.key'
    if local_enabled:
        local_key=local_key_file.read_text(encoding='ascii').strip() if local_key_file.is_file() and not local_key_file.is_symlink() else ''
        specs.append(ProviderSpec('local_qwen',local_model,local_key,True,True))
    output = int(values.get("XIAONUO_MAX_OUTPUT_TOKENS", "512"))
    timeout = int(values.get("XIAONUO_TIMEOUT_SECONDS", "30"))
    if not 32 <= output <= 2048 or not 5 <= timeout <= 60:
        raise ValueError("Output or timeout setting is outside allowed limits")
    primary = values.get("XIAONUO_PRIMARY_PAID", "deepseek" if region == "CN" else "openai").strip().lower()
    if primary not in {"openai", "deepseek"}:
        raise ValueError("Unsupported primary paid provider")
    budget = number("XIAONUO_MONTHLY_BUDGET_USD", "5.00")
    tts_provider=values.get('XIAONUO_TTS_PROVIDER','kokoro_onnx').strip().lower()
    if tts_provider not in {'kokoro_onnx','windows_sapi'}:raise ValueError('Unsupported speech provider')
    usb_port = values.get("XIAONUO_USB_PORT", "").strip()
    if usb_port and not re.fullmatch(r"COM[1-9][0-9]{0,2}|/dev/(?:ttyUSB|ttyACM)[0-9]{1,3}", usb_port):
        raise ValueError("Invalid USB serial port setting")
    assert budget is not None
    network_chat = values.get('XIAONUO_NETWORK_CHAT_PROVIDER','local').strip().lower()
    if network_chat not in {'local','deepseek'}:
        raise ValueError('Unsupported network chat provider')
    return Settings(root=target.resolve().parent, admin_token=values.get("XIAONUO_ADMIN_TOKEN", ""),
                    mode=Mode(values.get("XIAONUO_MODEL_MODE", "PAID_FIRST")),
                    remote_enabled=flag("XIAONUO_REMOTE_ENABLED"), allow_paid=flag("XIAONUO_ALLOW_PAID"),
                    usage_region=region, deployment_region=deployment,
                    primary_paid=primary, monthly_budget=budget,
                    monthly_server_cost=number("XIAONUO_MONTHLY_SERVER_COST_USD"),
                    monthly_budget_cny=number("XIAONUO_MONTHLY_BUDGET_CNY"),
                    monthly_server_cost_cny=number("XIAONUO_MONTHLY_SERVER_COST_CNY"),
                    cny_to_usd=number("XIAONUO_CNY_TO_USD"),
                    fx_checked_on=values.get("XIAONUO_FX_CHECKED_ON", ""),
                    max_output_tokens=output, timeout_seconds=timeout, providers=tuple(specs), usb_port=usb_port,local_llm_enabled=local_enabled,tts_provider=tts_provider,local_model=local_model,
                    device_speaker_enabled=flag('XIAONUO_DEVICE_SPEAKER_ENABLED'),network_chat_provider=network_chat,
                    computer_wake_enabled=flag('XIAONUO_COMPUTER_WAKE_ENABLED'))
