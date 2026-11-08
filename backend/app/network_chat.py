"""Explicit DeepSeek chat policy. Audio recognition/synthesis remain local."""
from .budgeting import price_for
from .config import Mode
from .errors import XiaoNuoError


def require_deepseek_chat(router, *, preflight=False):
    settings = router.settings
    if (not settings.remote_enabled or not settings.allow_paid
            or settings.primary_paid != 'deepseek'
            or settings.mode not in {Mode.PAID_FIRST, Mode.PAID_ONLY}):
        raise XiaoNuoError('NETWORK_DEEPSEEK_NOT_ENABLED', 409)
    # An error must never send the conversation to another cloud vendor.
    if set(router.providers) - {'deepseek', 'local_qwen'}:
        raise XiaoNuoError('NETWORK_CHAT_PROVIDER_NOT_ALLOWED', 409)
    provider = router.providers.get('deepseek')
    if provider is None or provider.spec.model != 'deepseek-flash':
        raise XiaoNuoError('NETWORK_DEEPSEEK_MODEL_NOT_CONFIGURED', 409)
    reason = router.blocked_reason(provider, False)
    if reason and reason!='PROVIDER_COOLDOWN':
        raise XiaoNuoError(reason, 409)
    if not preflight:
        # Eligibility and billing are enforced by ModelRouter before a remote
        # request. A failed DeepSeek call may legitimately fall back to local
        # Qwen; its reply must not be discarded because DeepSeek is cooling down.
        return
    price_for(settings, 'deepseek', provider.spec.model)
    snapshot = router.budget.snapshot()
    if snapshot['server_cost'] is None:
        raise XiaoNuoError('SERVER_COST_NOT_CONFIRMED', 409)
    if snapshot['paid_block_reason']:
        raise XiaoNuoError(snapshot['paid_block_reason'], 409)
