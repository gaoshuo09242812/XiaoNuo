"""Read-only local pricing and conservative reservation preflight, without a call."""
from decimal import Decimal, InvalidOperation

from .budgeting import USD_UNIT, micros, price_for
from .errors import XiaoNuoError


MESSAGES = {
    'BILLING_READY': '当前价格记录有效，余额可覆盖一次保守预留；不代表已验证实际扣费或平台余额。',
    'BILLING_NOT_CONFIRMED': '费用条件尚未确认，请检查后台预算状态。',
    'PRICE_SNAPSHOT_EXPIRED': '价格记录已过期，需核对供应商单价并更新记录；重启不会更新价格。',
    'FX_RATE_NOT_CONFIRMED': '兼容记账所需的汇率参考缺失或已过期，请检查网络及参考更新状态。',
    'MODEL_PRICE_NOT_CONFIGURED': '当前模型没有可用价格记录。',
    'SERVER_COST_NOT_CONFIRMED': '服务器费用尚未确认，当前阻止付费请求。',
    'BUDGET_EXHAUSTED': '当前余额不足覆盖下一次请求的保守预留，程序会阻止付费调用。',
    'CNY_HISTORY_REVIEW_REQUIRED': '有历史费用无法可靠计入人民币，需核对后才能继续付费调用。',
    'CNY_PAID_CURRENCY_REQUIRED': '人民币预算模式不能直接支付其他币种的模型调用。',
    'PRICE_BOUND_EXCEEDED': '费用曾超过预留上界，需先核对价格和记录；当前继续阻止付费调用。',
}


def check_billing(settings, budget):
    def result(code):
        return {'ready': code == 'BILLING_READY', 'code': code if code in MESSAGES else 'BILLING_NOT_CONFIRMED'}

    if not isinstance(budget, dict):
        return result('BILLING_NOT_CONFIRMED')
    try:
        if 'paid_block_reason' not in budget:
            return result('BILLING_NOT_CONFIRMED')
        blocked = budget.get('paid_block_reason')
        if blocked is not None:
            return result(blocked) if isinstance(blocked, str) and blocked != 'BILLING_READY' else result('BILLING_NOT_CONFIRMED')
        if budget.get('server_cost') is None:
            return result('SERVER_COST_NOT_CONFIRMED')
        currency = budget.get('currency')
        if currency not in ('CNY', 'USD'):
            return result('BILLING_NOT_CONFIRMED')
        remaining, server = Decimal(str(budget['remaining'])), Decimal(str(budget['server_cost']))
        if not remaining.is_finite() or remaining < 0 or not server.is_finite() or server < 0:
            return result('BILLING_NOT_CONFIRMED')
        spec = next((p for p in settings.providers if p.name == 'deepseek'), None)
        if spec is None:
            return result('MODEL_PRICE_NOT_CONFIGURED')
        # Uses the SAME checked price/FX and maximum-output settings as routing.
        # No network refresh, ledger object, database write or reservation occurs.
        price = price_for(settings, 'deepseek', spec.model)
        if currency == 'CNY':
            if price.currency != 'CNY':
                return result('CNY_PAID_CURRENCY_REQUIRED')
            required = Decimal(micros(price.maximum_native(settings.max_output_tokens))) / USD_UNIT
        else:
            required = Decimal(price.maximum(settings.max_output_tokens)) / USD_UNIT
        return result('BILLING_READY' if remaining >= required else 'BUDGET_EXHAUSTED')
    except XiaoNuoError as error:
        return result(error.code)
    except (ValueError, KeyError, TypeError, AttributeError, InvalidOperation, OverflowError):
        return result('BILLING_NOT_CONFIRMED')
