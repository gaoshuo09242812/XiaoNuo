"""SQLite reservations. Unknown outcomes retain their entire reservation."""
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone, timedelta, date
from decimal import Decimal, ROUND_CEILING
from pathlib import Path
import sqlite3
import json
from uuid import uuid4

from .config import Settings
from .errors import XiaoNuoError
from .llm.providers import Usage

USD_UNIT = Decimal("1000000")
SHANGHAI = timezone(timedelta(hours=8), "Asia/Shanghai")


def now() -> datetime:
    return datetime.now(timezone.utc)


def month_key() -> str:
    return now().astimezone(SHANGHAI).strftime("%Y-%m")


def micros(amount: Decimal) -> int:
    if not amount.is_finite() or amount < 0:
        raise ValueError("Invalid monetary amount")
    return int((amount * USD_UNIT).to_integral_value(rounding=ROUND_CEILING))


def dollars(amount: int) -> str:
    return str(Decimal(amount) / USD_UNIT)


class UsageLedger:
    def __init__(self, path: Path):
        self.path = path
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        with self.connect() as db:
            db.execute("PRAGMA journal_mode=WAL")
            db.executescript("""
                CREATE TABLE IF NOT EXISTS budget_reservations (
                    id TEXT PRIMARY KEY, request_id TEXT NOT NULL,
                    provider TEXT NOT NULL, model TEXT NOT NULL, month TEXT NOT NULL,
                    amount_microusd INTEGER NOT NULL CHECK(amount_microusd >= 0),
                    state TEXT NOT NULL CHECK(state IN ('pending','uncertain','settled')),
                    price_version TEXT NOT NULL, created_at TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS api_usage (
                    id TEXT PRIMARY KEY, reservation_id TEXT UNIQUE,
                    request_id TEXT NOT NULL, month TEXT NOT NULL,
                    provider TEXT NOT NULL, model TEXT NOT NULL, category TEXT NOT NULL,
                    input_tokens INTEGER, output_tokens INTEGER, cached_input_tokens INTEGER,
                    cost_microusd INTEGER NOT NULL CHECK(cost_microusd >= 0),
                    cost_kind TEXT NOT NULL, native_currency TEXT NOT NULL,
                    native_cost TEXT NOT NULL, conversion_rate TEXT NOT NULL,
                    price_version TEXT NOT NULL, created_at TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS chat_requests (
                    id TEXT PRIMARY KEY, state TEXT NOT NULL, created_at TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS provider_status (
                    provider TEXT PRIMARY KEY, model TEXT NOT NULL,
                    credential_fingerprint TEXT NOT NULL, last_error TEXT,
                    cooldown_until REAL NOT NULL DEFAULT 0,
                    last_success REAL, verified_free_at REAL);
                CREATE TABLE IF NOT EXISTS budget_config (
                    id INTEGER PRIMARY KEY CHECK(id=1), monthly_microusd INTEGER NOT NULL,
                    server_microusd INTEGER, updated_at TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS budget_cny_config (
                    id INTEGER PRIMARY KEY CHECK(id=1),
                    monthly_microcny INTEGER NOT NULL CHECK(monthly_microcny >= 0),
                    server_microcny INTEGER CHECK(server_microcny >= 0), updated_at TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS budget_safety_state (
                    id INTEGER PRIMARY KEY CHECK(id=1), reason TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS budget_reconciliations (
                    id TEXT PRIMARY KEY, reservation_id TEXT NOT NULL,
                    invoice_reference TEXT NOT NULL, confirmed_microusd INTEGER NOT NULL,
                    created_at TEXT NOT NULL, UNIQUE(reservation_id,invoice_reference));
            """)
            db.execute("BEGIN IMMEDIATE")
            try:
                additions = {"budget_reservations": {"category": "TEXT NOT NULL DEFAULT 'LLM'", "price_snapshot": "TEXT NOT NULL DEFAULT '{}'", "amount_microcny": "INTEGER"},
                             "api_usage": {"confirmed_cost_microusd": "INTEGER", "invoice_reference": "TEXT", "audio_seconds": "TEXT", "billed_characters": "INTEGER"}}
                for table, fields in additions.items():
                    existing = {r[1] for r in db.execute(f"PRAGMA table_info({table})")}
                    for name, definition in fields.items():
                        if name not in existing:
                            db.execute(f"ALTER TABLE {table} ADD COLUMN {name} {definition}")
                db.execute("COMMIT")
            except BaseException:
                db.execute("ROLLBACK")
                raise

    @contextmanager
    def connect(self):
        db = sqlite3.connect(self.path, timeout=5, isolation_level=None)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA busy_timeout=5000")
        db.execute("PRAGMA foreign_keys=ON")
        try:
            yield db
        finally:
            db.close()

    def claim_request(self, request_id: str):
        try:
            with self.connect() as db:
                db.execute("INSERT INTO chat_requests VALUES (?, 'started', ?)", (request_id, now().isoformat()))
        except sqlite3.IntegrityError:
            raise XiaoNuoError("DUPLICATE_REQUEST", 409) from None

    def finish_request(self, request_id: str, state: str):
        with self.connect() as db:
            db.execute("UPDATE chat_requests SET state=? WHERE id=?", (state, request_id))


@dataclass(frozen=True)
class Price:
    provider: str
    model: str
    currency: str
    input_per_million: Decimal
    cached_per_million: Decimal
    output_per_million: Decimal
    usd_per_currency_unit: Decimal
    version: str
    # Reserve against the entire documented context, not a guessed token count.
    input_ceiling: int = 1_100_000

    def maximum(self, output_limit: int) -> int:
        return micros(self.maximum_native(output_limit) * self.usd_per_currency_unit)

    def maximum_native(self, output_limit: int) -> Decimal:
        # OpenAI guard also covers long-context, cache-write and regional premiums.
        multiplier = Decimal("2.75") if self.provider == "openai" else Decimal("1")
        native = (Decimal(self.input_ceiling) * self.input_per_million * multiplier
                  + Decimal(output_limit) * self.output_per_million * multiplier) / USD_UNIT
        return native

    def quote(self, usage: Usage) -> tuple[int, str]:
        if min(usage.input_tokens, usage.output_tokens, usage.cached_input_tokens) < 0 or usage.cached_input_tokens > usage.input_tokens:
            raise ValueError("Invalid usage")
        native = ((usage.input_tokens - usage.cached_input_tokens) * self.input_per_million
                  + usage.cached_input_tokens * self.cached_per_million
                  + usage.output_tokens * self.output_per_million) / USD_UNIT
        return micros(native * self.usd_per_currency_unit), str(native)


def price_for(settings: Settings, provider: str, model: str) -> Price:
    checked_on=date(2026,10,28) if provider=='deepseek' else date(2026,10,24)
    if not 0 <= (now().astimezone(SHANGHAI).date() - checked_on).days <= 30:
        raise XiaoNuoError("PRICE_SNAPSHOT_EXPIRED")
    if provider == "openai" and model == "gpt-6-luna":
        return Price(provider, model, "USD", Decimal("0.10"), Decimal("0.01"), Decimal("0.50"),
                     Decimal("1"), "official-2026-10-24-standard")
    if provider == "deepseek" and model == "deepseek-flash":
        from .fx_rates import effective_fx
        rate,checked,source=effective_fx(settings,now().astimezone(SHANGHAI).date())
        return Price(provider, model, "CNY", Decimal("2"), Decimal("0.04"), Decimal("8"), rate,
                     f"official-2026-10-28-peak-fx-{checked.isoformat()}"+('-ecb' if source=='ecb' else ''))
    raise XiaoNuoError("MODEL_PRICE_NOT_CONFIGURED")


class BudgetManager:
    def __init__(self, ledger: UsageLedger, settings: Settings):
        self.ledger, self.settings = ledger, settings

    def _totals(self, db) -> tuple[int, int]:
        spent = db.execute("SELECT COALESCE(SUM(COALESCE(confirmed_cost_microusd,cost_microusd)),0) FROM api_usage WHERE month=?", (month_key(),)).fetchone()[0]
        # Unresolved requests remain reserved even after a month boundary or restart.
        held = db.execute("SELECT COALESCE(SUM(amount_microusd),0) FROM budget_reservations WHERE state!='settled'").fetchone()[0]
        return spent, held

    def _limits(self, db) -> tuple[int, int | None]:
        row = db.execute("SELECT monthly_microusd,server_microusd FROM budget_config WHERE id=1").fetchone()
        if row:
            return row[0], row[1]
        return micros(self.settings.monthly_budget), None if self.settings.monthly_server_cost is None else micros(self.settings.monthly_server_cost)

    def snapshot(self) -> dict:
        with self.ledger.connect() as db:
            spent, held = self._totals(db)
            calls = db.execute("SELECT COUNT(*) FROM api_usage WHERE month=?", (month_key(),)).fetchone()[0]
            unknown = db.execute("SELECT COUNT(*) FROM budget_reservations WHERE state='uncertain'").fetchone()[0]
            limit, server = self._limits(db)
            blocked = db.execute("SELECT reason FROM budget_safety_state WHERE id=1").fetchone()
            confirmed = db.execute("SELECT COUNT(confirmed_cost_microusd),COALESCE(SUM(confirmed_cost_microusd),0) FROM api_usage WHERE month=?", (month_key(),)).fetchone()
            totals = db.execute("SELECT category,SUM(cost_microusd) AS estimated,SUM(CASE WHEN confirmed_cost_microusd IS NOT NULL THEN confirmed_cost_microusd ELSE 0 END) AS confirmed,COUNT(*) AS calls "
                               "FROM api_usage WHERE month=? GROUP BY category", (month_key(),)).fetchall()
            pending = [dict(r) for r in db.execute("SELECT id,provider,model,category,amount_microusd,state,created_at FROM budget_reservations WHERE state!='settled' ORDER BY created_at LIMIT 100")]
            from . import cny_budget
            cny = cny_budget.enabled(db, self.settings)
            native = None
            if cny:
                native_limit, native_server = cny_budget.limits(db, self.settings)
                try:
                    native_spent, native_held, native_categories = cny_budget.totals(db, month_key())
                    native = {'currency': 'CNY', 'budget': dollars(native_limit),
                              'api_cost_estimate': dollars(native_spent), 'outstanding_reservations': dollars(native_held),
                              'server_cost': None if native_server is None else dollars(native_server),
                              'remaining': None if native_server is None else dollars(max(0, native_limit-native_server-native_spent-native_held)),
                              'owner_confirmed_cost': None,
                              'categories': {k: {'estimated_cny': dollars(v['estimated_cny']), 'calls': v['calls']} for k,v in native_categories.items()},
                              'cost_note': '人民币原币预算；DeepSeek按返回用量及已核对峰值单价估算，并非实扣账单。未知结果保留预留。旧美元字段仅兼容历史记录，不决定人民币限额。'}
                except XiaoNuoError as error:
                    native = {'currency': 'CNY', 'budget': dollars(native_limit), 'api_cost_estimate': None,
                              'outstanding_reservations': None, 'server_cost': None if native_server is None else dollars(native_server),
                              'remaining': None, 'owner_confirmed_cost': None, 'categories': {},
                              'paid_block_reason': error.code,
                              'cost_note': '存在无法可靠计入人民币的历史费用，需核对后才能继续付费调用。'}
        available = None if server is None else max(0, limit - server - spent - held)
        result = {"month": month_key(), "timezone": "Asia/Shanghai", "currency": "USD",
                "budget_usd": dollars(limit), "api_cost_estimate_usd": dollars(spent),
                "outstanding_reservations_usd": dollars(held), "server_cost_usd": None if server is None else dollars(server),
                "remaining_usd": None if available is None else dollars(available),
                "usage_records": calls, "uncertain_requests": unknown,
                "owner_confirmed_cost_usd": dollars(confirmed[1]) if confirmed[0] else None,
                "confirmed_invoice_count": confirmed[0],
                "paid_block_reason": blocked[0] if blocked else None, "outstanding_requests": pending,
                "categories": {row["category"]: {"estimated_usd": dollars(row["estimated"]), "owner_confirmed_usd": dollars(row["confirmed"]), "calls": row["calls"]} for row in totals},
                "cost_note": "Estimates use provider-reported usage; owner-confirmed invoice amounts are distinguished. DeepSeek uses peak-price upper estimates."}
        result.update(budget=result['budget_usd'], api_cost_estimate=result['api_cost_estimate_usd'],
                      outstanding_reservations=result['outstanding_reservations_usd'], server_cost=result['server_cost_usd'],
                      remaining=result['remaining_usd'], owner_confirmed_cost=result['owner_confirmed_cost_usd'])
        if native is not None:
            result.update(native)
            # There is no USD budget when native CNY is selected.
            result.update(budget_usd=None, remaining_usd=None, server_cost_usd=None)
            result.update(budget_cny=result['budget'], api_cost_estimate_cny=result['api_cost_estimate'],
                          outstanding_reservations_cny=result['outstanding_reservations'], server_cost_cny=result['server_cost'],
                          remaining_cny=result['remaining'])
        return result

    def reserve(self, request_id: str, price: Price, output_limit: int) -> str:
        amount = price.maximum(output_limit)
        snapshot = {"currency": price.currency, "input": str(price.input_per_million), "cached": str(price.cached_per_million),
                    "output": str(price.output_per_million), "usd_rate": str(price.usd_per_currency_unit), "version": price.version}
        return self.reserve_amount(request_id, provider=price.provider, model=price.model, category="LLM", amount_microusd=amount,
                                   price_version=price.version, price_snapshot=snapshot,
                                   amount_microcny=micros(price.maximum_native(output_limit)) if price.currency=='CNY' else None)

    def reserve_amount(self, request_id: str, *, provider: str, model: str, category: str,
                       amount_microusd: int, price_version: str, price_snapshot: dict, amount_microcny: int | None = None) -> str:
        if category not in {"LLM", "ASR", "TTS", "OTHER"} or type(amount_microusd) is not int or amount_microusd < 0:
            raise XiaoNuoError("INVALID_RESERVATION", 422)
        if amount_microcny is not None and (type(amount_microcny) is not int or amount_microcny < 0 or price_snapshot.get('currency')!='CNY'):
            raise XiaoNuoError('INVALID_RESERVATION', 422)
        reservation_id = str(uuid4())
        with self.ledger.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            try:
                from . import cny_budget
                native_mode = cny_budget.enabled(db, self.settings)
                if native_mode:
                    spent, held, _ = cny_budget.totals(db, month_key())
                    limit, server = cny_budget.limits(db, self.settings)
                    if price_snapshot.get('currency')!='CNY' and amount_microusd>0:
                        raise XiaoNuoError('CNY_PAID_CURRENCY_REQUIRED', 409)
                    accounting_amount = cny_budget.held_amount({'amount_microcny': amount_microcny,
                        'amount_microusd': amount_microusd, 'price_snapshot': json.dumps(price_snapshot)})
                else:
                    spent, held = self._totals(db)
                    limit, server = self._limits(db)
                    accounting_amount = amount_microusd
                if server is None:
                    raise XiaoNuoError("SERVER_COST_NOT_CONFIRMED")
                blocked = db.execute("SELECT reason FROM budget_safety_state WHERE id=1").fetchone()
                if blocked:
                    raise XiaoNuoError(blocked[0])
                if spent + held + accounting_amount > limit - server:
                    raise XiaoNuoError("BUDGET_EXHAUSTED")
                db.execute("INSERT INTO budget_reservations(id,request_id,provider,model,month,amount_microusd,state,price_version,created_at,category,price_snapshot,amount_microcny) VALUES (?,?,?,?,?,?, 'pending', ?,?,?,?,?)",
                           (reservation_id, request_id, provider, model, month_key(), amount_microusd, price_version, now().isoformat(), category, json.dumps(price_snapshot),amount_microcny))
                db.execute("COMMIT")
            except BaseException:
                db.execute("ROLLBACK")
                raise
        return reservation_id

    def uncertain(self, reservation_id: str):
        with self.ledger.connect() as db:
            db.execute("UPDATE budget_reservations SET state='uncertain' WHERE id=? AND state='pending'", (reservation_id,))

    def settle(self, reservation_id: str, usage: Usage, price: Price):
        cost, native = price.quote(usage)
        with self.ledger.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            try:
                row = db.execute("SELECT * FROM budget_reservations WHERE id=?", (reservation_id,)).fetchone()
                if row is None or row["state"] == "settled":
                    raise XiaoNuoError("INVALID_RESERVATION")
                if row["provider"] != price.provider or row["model"] != price.model or row["price_version"] != price.version:
                    raise XiaoNuoError("RESERVATION_PRICE_MISMATCH", 409)
                snapshot = {"currency": price.currency, "input": str(price.input_per_million), "cached": str(price.cached_per_million),
                            "output": str(price.output_per_million), "usd_rate": str(price.usd_per_currency_unit), "version": price.version}
                if json.loads(row["price_snapshot"]) != snapshot:
                    raise XiaoNuoError("RESERVATION_PRICE_MISMATCH", 409)
                db.execute("INSERT INTO api_usage(id,reservation_id,request_id,month,provider,model,category,input_tokens,output_tokens,cached_input_tokens,cost_microusd,cost_kind,native_currency,native_cost,conversion_rate,price_version,created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                           (str(uuid4()), reservation_id, row["request_id"], month_key(), price.provider, price.model, "LLM",
                            usage.input_tokens, usage.output_tokens, usage.cached_input_tokens, cost,
                            "upper_estimate_reported_usage" if price.provider == "deepseek" else "estimate_reported_usage",
                            price.currency, native, str(price.usd_per_currency_unit), price.version, now().isoformat()))
                if cost > row["amount_microusd"] or (price.currency=='CNY' and row['amount_microcny'] is not None and micros(Decimal(native))>row['amount_microcny']):
                    db.execute("INSERT OR REPLACE INTO budget_safety_state VALUES (1,'PRICE_BOUND_EXCEEDED')")
                db.execute("UPDATE budget_reservations SET state='settled' WHERE id=?", (reservation_id,))
                db.execute("COMMIT")
            except BaseException:
                db.execute("ROLLBACK")
                raise

    def record_free(self, request_id: str, provider: str, model: str, usage: Usage | None):
        with self.ledger.connect() as db:
            db.execute("INSERT INTO api_usage(id,reservation_id,request_id,month,provider,model,category,input_tokens,output_tokens,cached_input_tokens,cost_microusd,cost_kind,native_currency,native_cost,conversion_rate,price_version,created_at) VALUES (?,?,?, ?,?,?, 'LLM', ?,?,?,0, 'free_account_asserted', 'USD','0','1','account-confirmed',?)",
                       (str(uuid4()), None, request_id, month_key(), provider, model,
                        None if usage is None else usage.input_tokens, None if usage is None else usage.output_tokens,
                        None if usage is None else usage.cached_input_tokens, now().isoformat()))

    def record_local(self, request_id: str, provider: str, model: str, category: str,
                     *, audio_seconds: float | None = None, characters: int | None = None, usage: Usage | None = None):
        if category not in {"LLM", "ASR", "TTS"}:
            raise XiaoNuoError("INVALID_USAGE_CATEGORY",422)
        with self.ledger.connect() as db:
            db.execute("INSERT INTO api_usage(id,reservation_id,request_id,month,provider,model,category,input_tokens,output_tokens,"
                       "cached_input_tokens,cost_microusd,cost_kind,native_currency,native_cost,conversion_rate,price_version,created_at,audio_seconds,billed_characters) "
                       "VALUES (?,NULL,?,?,?,?,?,?,?,?,0,'local_compute_no_api_fee','USD','0','1','local-engine',?,?,?)",
                       (str(uuid4()),request_id,month_key(),provider,model,category,
                        None if usage is None else usage.input_tokens,None if usage is None else usage.output_tokens,
                        None if usage is None else usage.cached_input_tokens,now().isoformat(),
                        None if audio_seconds is None else str(audio_seconds),characters))

    def set_limits(self, monthly_usd: Decimal, server_usd: Decimal | None):
        if not monthly_usd.is_finite() or not 0 <= monthly_usd <= Decimal("1000000") or (server_usd is not None and (not server_usd.is_finite() or not 0 <= server_usd <= Decimal("1000000"))):
            raise XiaoNuoError("INVALID_BUDGET", 422)
        with self.ledger.connect() as db:
            from . import cny_budget
            if cny_budget.enabled(db, self.settings):
                raise XiaoNuoError('BUDGET_CURRENCY_MISMATCH', 409)
            db.execute("INSERT OR REPLACE INTO budget_config VALUES (1,?,?,?)", (micros(monthly_usd), None if server_usd is None else micros(server_usd), now().isoformat()))

    def set_limits_cny(self, monthly_cny: Decimal, server_cny: Decimal | None):
        if not monthly_cny.is_finite() or not 0 <= monthly_cny <= Decimal('1000000') or (server_cny is not None and (not server_cny.is_finite() or not 0 <= server_cny <= Decimal('1000000'))):
            raise XiaoNuoError('INVALID_BUDGET', 422)
        with self.ledger.connect() as db:
            db.execute('INSERT OR REPLACE INTO budget_cny_config VALUES (1,?,?,?)',
                       (micros(monthly_cny),None if server_cny is None else micros(server_cny),now().isoformat()))

    def reconcile(self, reservation_id: str, confirmed_usd: Decimal, invoice_reference: str):
        with self.ledger.connect() as db:
            from . import cny_budget
            if cny_budget.enabled(db, self.settings):
                raise XiaoNuoError('BUDGET_CURRENCY_MISMATCH', 409)
        if self.settings.remote_enabled and self.settings.mode.value != "DISABLED":
            raise XiaoNuoError("DISABLE_REMOTE_BEFORE_RECONCILIATION", 409)
        amount = micros(confirmed_usd)
        if amount > 1_000_000_000_000 or not 1 <= len(invoice_reference.strip()) <= 120:
            raise XiaoNuoError("INVALID_INVOICE_CONFIRMATION", 422)
        with self.ledger.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            try:
                row = db.execute("SELECT * FROM budget_reservations WHERE id=?", (reservation_id,)).fetchone()
                if row is None:
                    raise XiaoNuoError("RESERVATION_NOT_FOUND", 404)
                if row["state"] == "pending" and (now() - datetime.fromisoformat(row["created_at"])).total_seconds() < 300:
                    raise XiaoNuoError("REQUEST_MAY_STILL_BE_RUNNING", 409)
                existing = db.execute("SELECT confirmed_microusd FROM budget_reconciliations WHERE reservation_id=? AND invoice_reference=?", (reservation_id, invoice_reference)).fetchone()
                if existing:
                    if existing[0] != amount:
                        raise XiaoNuoError("INVOICE_CONFIRMATION_CONFLICT", 409)
                    db.execute("COMMIT")
                    return
                usage = db.execute("SELECT id FROM api_usage WHERE reservation_id=?", (reservation_id,)).fetchone()
                if usage:
                    db.execute("UPDATE api_usage SET confirmed_cost_microusd=?,invoice_reference=? WHERE reservation_id=?", (amount, invoice_reference, reservation_id))
                else:
                    db.execute("INSERT INTO api_usage(id,reservation_id,request_id,month,provider,model,category,cost_microusd,cost_kind,native_currency,native_cost,conversion_rate,price_version,created_at,confirmed_cost_microusd,invoice_reference) VALUES (?,?,?,?,?,?,?,?,'owner_confirmed_invoice','USD',?,'1',?,?,?,?)",
                               (str(uuid4()), reservation_id, row["request_id"], month_key(), row["provider"], row["model"], row["category"], amount, dollars(amount), row["price_version"], now().isoformat(), amount, invoice_reference))
                db.execute("UPDATE budget_reservations SET state='settled' WHERE id=?", (reservation_id,))
                db.execute("INSERT INTO budget_reconciliations VALUES (?,?,?,?,?)", (str(uuid4()), reservation_id, invoice_reference, amount, now().isoformat()))
                db.execute("COMMIT")
            except BaseException:
                db.execute("ROLLBACK")
                raise
