"""Native CNY limits; never infer a fresh exchange rate from an old USD total."""
from decimal import Decimal, InvalidOperation, ROUND_CEILING
import json

from .errors import XiaoNuoError

UNIT = Decimal(1000000)


def amount(value):
    try:
        parsed = Decimal(str(value))
        if not parsed.is_finite() or parsed < 0:
            raise ValueError()
        return parsed
    except (ValueError, TypeError, InvalidOperation):
        raise XiaoNuoError('CNY_HISTORY_REVIEW_REQUIRED', 409) from None


def micro(value):
    return int((amount(value) * UNIT).to_integral_value(rounding=ROUND_CEILING))


def enabled(db, settings):
    return (settings.monthly_budget_cny is not None
            or db.execute('SELECT 1 FROM budget_cny_config WHERE id=1').fetchone() is not None)


def limits(db, settings):
    row = db.execute('SELECT monthly_microcny,server_microcny FROM budget_cny_config WHERE id=1').fetchone()
    if row is not None:
        return row[0], row[1]
    return (micro(settings.monthly_budget_cny),
            None if settings.monthly_server_cost_cny is None else micro(settings.monthly_server_cost_cny))


def usage_amount(row):
    # USD invoice confirmations cannot silently overwrite a native CNY bill.
    if row['confirmed_cost_microusd'] is not None:
        if row['confirmed_cost_microusd'] == 0:
            return 0
        raise XiaoNuoError('CNY_HISTORY_REVIEW_REQUIRED', 409)
    if row['native_currency'] == 'CNY':
        return micro(row['native_cost'])
    if row['cost_microusd'] == 0 and amount(row['native_cost']) == 0:
        return 0  # Local/free calls have no API charge in either currency.
    raise XiaoNuoError('CNY_HISTORY_REVIEW_REQUIRED', 409)


def held_amount(row):
    if row['amount_microcny'] is not None:
        value = row['amount_microcny']
        if type(value) is int and value >= 0:
            return value
        raise XiaoNuoError('CNY_HISTORY_REVIEW_REQUIRED', 409)
    if row['amount_microusd'] == 0:
        return 0
    try:
        snapshot = json.loads(row['price_snapshot'])
        rate = amount(snapshot['usd_rate'])
        if snapshot['currency'] != 'CNY' or rate == 0:
            raise ValueError()
        # Legacy holds used ceiling(USD): undo using their ORIGINAL stored rate,
        # rounding up again. Never use today's FX or drop uncertain old holds.
        return int((Decimal(row['amount_microusd']) / rate).to_integral_value(rounding=ROUND_CEILING))
    except (ValueError, KeyError, TypeError, InvalidOperation):
        raise XiaoNuoError('CNY_HISTORY_REVIEW_REQUIRED', 409) from None


def totals(db, month):
    rows = db.execute('SELECT * FROM api_usage WHERE month=?', (month,)).fetchall()
    spent, categories = 0, {}
    for row in rows:
        cost = usage_amount(row)
        spent += cost
        record = categories.setdefault(row['category'], {'estimated_cny': 0, 'calls': 0})
        record['estimated_cny'] += cost
        record['calls'] += 1
    held = sum(held_amount(row) for row in db.execute("SELECT * FROM budget_reservations WHERE state!='settled'"))
    return spent, held, categories
