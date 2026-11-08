from dataclasses import dataclass
from uuid import uuid4

from ..budgeting import BudgetManager, price_for
from ..config import Mode, Settings
from ..errors import ProviderError, XiaoNuoError
from .health import ProviderHealthMonitor
from .providers import LLMProvider, Completion


@dataclass(frozen=True)
class RoutedReply:
    completion: Completion
    provider: str
    model: str
    cost_status: str
    switch_reason: str | None


class ModelRouter:
    def __init__(self, settings: Settings, providers: dict[str, LLMProvider], budget: BudgetManager,
                 health: ProviderHealthMonitor):
        self.settings, self.providers, self.budget, self.health = settings, providers, budget, health

    def blocked_reason(self, provider: LLMProvider, free: bool) -> str | None:
        spec = provider.spec
        if spec.name=='local_qwen':
            if not self.settings.local_llm_enabled or not spec.key:return 'LOCAL_MODEL_NOT_CONFIGURED'
            state=self.health.status(spec)
            if not state['can_attempt']:return 'PROVIDER_COOLDOWN'
            return None if state['verified_free'] else 'LOCAL_MODEL_NOT_VERIFIED'
        if self.settings.usage_region == "CN" and spec.name in {"openai", "gemini"}:
            return "UNSUPPORTED_USAGE_REGION"
        if not spec.region_approved or self.settings.deployment_region == "UNDECIDED":
            return "REGION_NOT_CONFIRMED"
        if not spec.key or not spec.model:
            return "MISSING_CREDENTIAL_OR_MODEL"
        state = self.health.status(spec)
        if not state["can_attempt"]:
            return "PROVIDER_COOLDOWN"
        if free and (not spec.free_account_confirmed or not state["verified_free"]):
            return "FREE_ACCOUNT_NOT_VERIFIED"
        if not free and not self.settings.allow_paid:
            return "PAID_CALLS_NOT_AUTHORIZED"
        return None

    def reply(self, request_id: str, system: str, messages: list[dict]) -> RoutedReply:
        settings = self.settings
        if (not settings.remote_enabled and not settings.local_llm_enabled) or settings.mode == Mode.DISABLED:
            raise XiaoNuoError("REMOTE_CALLS_DISABLED")
        paid = [(settings.primary_paid, False)]
        free = [(name, True) for name in ('local_qwen',"groq", "gemini")]
        order = {Mode.PAID_FIRST: paid + free, Mode.FREE_FIRST: free + paid,
                 Mode.FREE_ONLY: free, Mode.PAID_ONLY: paid}[settings.mode]
        candidates = [(self.providers[name], is_free) for name, is_free in order if name in self.providers and (settings.remote_enabled or name=='local_qwen')]
        eligible = [(p, f) for p, f in candidates if self.blocked_reason(p, f) is None]
        if not eligible:
            raise XiaoNuoError("NO_ELIGIBLE_PROVIDER")
        self.budget.ledger.claim_request(request_id)
        reason = None
        try:
            for provider, is_free in eligible:
                reservation = None
                price = None
                if not is_free:
                    try:
                        price = price_for(settings, provider.spec.name, provider.spec.model)
                        reservation = self.budget.reserve(request_id, price, settings.max_output_tokens)
                    except XiaoNuoError as error:
                        reason = error.code
                        continue
                try:
                    answer = provider.generate(system, messages, settings.max_output_tokens)
                except ProviderError as error:
                    if reservation:
                        self.budget.uncertain(reservation)
                    self.health.report(provider.spec, error.code)
                    reason = error.code
                    continue
                except BaseException:
                    if reservation:
                        self.budget.uncertain(reservation)
                    raise
                try:
                    is_local=provider.spec.name=='local_qwen'
                    self.health.report(provider.spec,verify_free=is_local)
                    if is_local:
                        self.budget.record_local(request_id,provider.spec.name,provider.spec.model,'LLM',usage=answer.usage)
                        cost_state='local_compute_no_api_fee'
                    elif is_free:
                        self.budget.record_free(request_id, provider.spec.name, provider.spec.model, answer.usage)
                        cost_state = "free_account_asserted"
                    elif answer.usage is not None:
                        assert reservation is not None and price is not None
                        self.budget.settle(reservation, answer.usage, price)
                        cost_state = "estimate_from_reported_usage"
                    else:
                        assert reservation is not None
                        self.budget.uncertain(reservation)
                        cost_state = "unknown_reservation_retained"
                    self.budget.ledger.finish_request(request_id, "complete")
                except BaseException:
                    if reservation:
                        self.budget.uncertain(reservation)
                    raise
                return RoutedReply(answer, provider.spec.name, provider.spec.model, cost_state, reason)
            raise XiaoNuoError(reason or "ALL_PROVIDERS_UNAVAILABLE")
        except BaseException:
            self.budget.ledger.finish_request(request_id, "failed_or_uncertain")
            raise

    def verify_local(self) -> dict:
        provider=self.providers.get('local_qwen')
        if not self.settings.local_llm_enabled or self.settings.mode==Mode.DISABLED or provider is None:
            raise XiaoNuoError('LOCAL_MODEL_NOT_CONFIGURED')
        result=provider.generate('你只用中文回答，不调用工具。',[{'role':'user','content':'请只回复：你好。'}],32)
        self.health.report(provider.spec,verify_free=True)
        self.budget.record_local(str(uuid4()),provider.spec.name,provider.spec.model,'LLM',usage=result.usage)
        return {'provider':'local_qwen','model':provider.spec.model,'inference_verified':True,
                'api_cost_usd':0,'response':result.text}

    def verify_free(self, name: str) -> dict:
        if not self.settings.remote_enabled or self.settings.mode == Mode.DISABLED or name not in {"groq", "gemini"}:
            raise XiaoNuoError("FREE_VERIFICATION_DISABLED")
        provider = self.providers.get(name)
        if provider is None or not provider.spec.free_account_confirmed:
            raise XiaoNuoError("FREE_ACCOUNT_NOT_CONFIRMED")
        spec = provider.spec
        if (self.settings.usage_region == "CN" and name == "gemini") or not spec.region_approved or self.settings.deployment_region == "UNDECIDED":
            raise XiaoNuoError("REGION_NOT_CONFIRMED")
        if not spec.key or not spec.model:
            raise XiaoNuoError("MISSING_CREDENTIAL_OR_MODEL")
        try:
            result = provider.generate("只回答中文，不调用任何工具。", [{"role": "user", "content": "请只回复：你好。"}], 32)
        except ProviderError as error:
            self.health.report(spec, error.code)
            raise
        self.budget.record_free(str(uuid4()), name, spec.model, result.usage)
        self.health.report(spec, verify_free=True)
        return {"provider": name, "model": spec.model, "inference_verified": True,
                "free_tier_basis": "operator_account_confirmation", "expires_in_hours": 24}
