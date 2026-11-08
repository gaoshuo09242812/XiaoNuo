"""Offline software checks only. No provider or hardware validation is claimed."""
import asyncio
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from decimal import Decimal
import json
from pathlib import Path
import sys
import tempfile
import unittest
from uuid import uuid4

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))
from app.budgeting import BudgetManager, Price, UsageLedger
from app.config import Mode, ProviderSpec, Settings, load_settings
from app.errors import ProviderError, XiaoNuoError
from app.llm.health import ProviderHealthMonitor
from app.llm.providers import Completion, LLMProvider, OpenAIProvider, Usage
from app.llm.router import ModelRouter
from app.main import create_app


class FakeProvider(LLMProvider):
    def __init__(self, spec, *, fail=False, usage=Usage(20, 10)):
        super().__init__(spec)
        self.calls = []
        self.fail, self.usage = fail, usage

    def generate(self, system, messages, max_output_tokens):
        self.calls.append((system, list(messages)))
        if self.fail:
            raise ProviderError("PROVIDER_NETWORK_ERROR")
        return Completion("离线测试替身回复", self.usage)


async def request_asgi(app, path, *, token=None, body=None, method="GET", extra_headers=(), return_headers=False):
    raw = b"" if body is None else (body if isinstance(body, bytes) else json.dumps(body).encode())
    headers = [(b"content-type", b"application/json")]
    if token is not None:
        headers.append((b"authorization", ("Bearer " + token).encode()))
    headers.extend(extra_headers)
    scope = {"type": "http", "asgi": {"version": "3.0", "spec_version": "2.4"},
             "http_version": "1.1", "method": method, "scheme": "http", "path": path,
             "raw_path": path.encode(), "query_string": b"", "root_path": "", "headers": headers,
             "server": ("127.0.0.1", 8765), "client": ("127.0.0.1", 12345)}
    received = False
    output = []

    async def receive():
        nonlocal received
        if not received:
            received = True
            return {"type": "http.request", "body": raw, "more_body": False}
        return {"type": "http.disconnect"}

    async def send(message):
        output.append(message)

    await app(scope, receive, send)
    status = next(m["status"] for m in output if m["type"] == "http.response.start")
    data = b"".join(m.get("body", b"") for m in output if m["type"] == "http.response.body")
    if return_headers:
        return status, json.loads(data), next(m["headers"] for m in output if m["type"] == "http.response.start")
    return status, json.loads(data)


class CoreTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.settings = Settings(root=self.root, admin_token="offline-admin-" + "x" * 48,
            remote_enabled=True, allow_paid=True, usage_region="US", deployment_region="US",
            monthly_server_cost=Decimal("0"))
        self.ledger = UsageLedger(self.settings.database)
        self.budget = BudgetManager(self.ledger, self.settings)
        self.health = ProviderHealthMonitor(self.ledger)

    def spec(self, name, *, key="offline-not-a-real-key"):
        return ProviderSpec(name, "gpt-6-luna" if name == "openai" else "test-model", key,
                            free_account_confirmed=name in {"groq", "gemini"}, region_approved=True)

    def router(self, settings=None, paid=None, free=None):
        providers = {}
        if paid is not None:
            providers["openai"] = paid
        if free is not None:
            providers["groq"] = free
        settings = settings or self.settings
        return ModelRouter(settings, providers, BudgetManager(self.ledger, settings), self.health)

    def test_atomic_reservations_enforce_shared_budget(self):
        price = Price("test", "test", "USD", Decimal("1"), Decimal("0"), Decimal("0"), Decimal("1"), "offline", input_ceiling=1_000_000)
        budget = BudgetManager(self.ledger, replace(self.settings, monthly_budget=Decimal("1")))

        def reserve(_):
            try:
                budget.reserve(str(uuid4()), price, 32)
                return True
            except XiaoNuoError as error:
                self.assertEqual(error.code, "BUDGET_EXHAUSTED")
                return False

        with ThreadPoolExecutor(max_workers=12) as executor:
            results = list(executor.map(reserve, range(24)))
        self.assertEqual(sum(results), 1)
        self.assertEqual(budget.snapshot()["outstanding_reservations_usd"], "1")

    def test_unknown_cost_survives_restart(self):
        price = Price("test", "test", "USD", Decimal("1"), Decimal("0"), Decimal("0"), Decimal("1"), "offline", input_ceiling=1_000_000)
        reservation = self.budget.reserve(str(uuid4()), price, 32)
        self.budget.uncertain(reservation)
        restarted = BudgetManager(UsageLedger(self.settings.database), self.settings)
        self.assertEqual(restarted.snapshot()["outstanding_reservations_usd"], "1")
        self.assertEqual(restarted.snapshot()["uncertain_requests"], 1)

    def test_settlement_releases_unused_amount_and_is_not_repeatable(self):
        price = Price("test", "test", "USD", Decimal("1"), Decimal("0.1"), Decimal("2"), Decimal("1"), "offline")
        reservation = self.budget.reserve(str(uuid4()), price, 512)
        self.budget.settle(reservation, Usage(100, 50, 20), price)
        summary = self.budget.snapshot()
        self.assertEqual(summary["outstanding_reservations_usd"], "0")
        self.assertEqual(summary["api_cost_estimate_usd"], "0.000182")
        with self.assertRaises(XiaoNuoError):
            self.budget.settle(reservation, Usage(100, 50, 20), price)

    def test_server_cost_unknown_blocks_paid_reservation(self):
        price = Price("test", "test", "USD", Decimal("1"), Decimal("0"), Decimal("0"), Decimal("1"), "offline")
        budget = BudgetManager(self.ledger, replace(self.settings, monthly_server_cost=None))
        with self.assertRaisesRegex(XiaoNuoError, "SERVER_COST_NOT_CONFIRMED"):
            budget.reserve(str(uuid4()), price, 32)

    def test_budget_exhaustion_falls_back_with_same_context(self):
        paid, free = FakeProvider(self.spec("openai")), FakeProvider(self.spec("groq"))
        self.health.report(free.spec, verify_free=True)
        router = self.router(replace(self.settings, monthly_budget=Decimal("0")), paid, free)
        messages = [{"role": "user", "content": "这是离线测试"}]
        answer = router.reply(str(uuid4()), "相同角色设定", messages)
        self.assertEqual(answer.provider, "groq")
        self.assertEqual(answer.switch_reason, "BUDGET_EXHAUSTED")
        self.assertEqual(paid.calls, [])
        self.assertEqual(free.calls[0], ("相同角色设定", messages))

    def test_timeout_keeps_paid_reservation_then_uses_verified_free(self):
        paid, free = FakeProvider(self.spec("openai"), fail=True), FakeProvider(self.spec("groq"))
        self.health.report(free.spec, verify_free=True)
        router = self.router(paid=paid, free=free)
        request_id = str(uuid4())
        answer = router.reply(request_id, "system", [{"role": "user", "content": "offline"}])
        self.assertEqual(answer.provider, "groq")
        self.assertGreater(Decimal(self.budget.snapshot()["outstanding_reservations_usd"]), 0)
        with self.assertRaisesRegex(XiaoNuoError, "DUPLICATE_REQUEST"):
            router.reply(request_id, "system", [{"role": "user", "content": "offline"}])
        self.assertEqual(len(paid.calls), 1)

    def test_missing_usage_never_settles_as_zero(self):
        paid = FakeProvider(self.spec("openai"), usage=None)
        answer = self.router(paid=paid).reply(str(uuid4()), "system", [{"role": "user", "content": "offline"}])
        self.assertEqual(answer.cost_status, "unknown_reservation_retained")
        self.assertGreater(Decimal(self.budget.snapshot()["outstanding_reservations_usd"]), 0)

    def test_free_only_never_uses_paid_provider(self):
        paid = FakeProvider(self.spec("openai"))
        router = self.router(replace(self.settings, mode=Mode.FREE_ONLY), paid=paid)
        with self.assertRaisesRegex(XiaoNuoError, "NO_ELIGIBLE_PROVIDER"):
            router.reply(str(uuid4()), "system", [{"role": "user", "content": "offline"}])
        self.assertEqual(paid.calls, [])

    def test_cn_region_blocks_openai_even_if_flag_is_approved(self):
        paid = FakeProvider(self.spec("openai"))
        router = self.router(replace(self.settings, usage_region="CN"), paid=paid)
        with self.assertRaisesRegex(XiaoNuoError, "NO_ELIGIBLE_PROVIDER"):
            router.reply(str(uuid4()), "system", [{"role": "user", "content": "offline"}])
        self.assertEqual(paid.calls, [])

    def test_free_verification_invalidates_after_key_change(self):
        spec = self.spec("groq")
        self.health.report(spec, verify_free=True)
        self.assertTrue(self.health.status(spec)["verified_free"])
        self.assertFalse(self.health.status(replace(spec, key="another-offline-key"))["verified_free"])

    def test_unverified_provider_does_not_claim_healthy(self):
        status = self.health.status(self.spec("openai"))
        self.assertEqual(status["state"], "unverified")
        self.assertIsNone(status["healthy"])

    def test_disabled_mode_blocks_even_free_verification(self):
        free = FakeProvider(self.spec("groq"))
        router = self.router(replace(self.settings, mode=Mode.DISABLED), free=free)
        with self.assertRaisesRegex(XiaoNuoError, "FREE_VERIFICATION_DISABLED"):
            router.verify_free("groq")
        self.assertEqual(free.calls, [])

    def test_invalid_region_configuration_is_rejected(self):
        from unittest.mock import patch
        env_file = self.root / ".env.local"
        env_file.write_text("XIAONUO_USAGE_REGION=CHINA\n", encoding="utf-8")
        with patch.dict("os.environ", {}, clear=True):
            with self.assertRaisesRegex(ValueError, "Regions must"):
                load_settings(env_file)

    def test_local_credential_overrides_inherited_process_key(self):
        from unittest.mock import patch
        env_file = self.root / ".env.local"
        env_file.write_text("OPENAI_API_KEY=dedicated-test-credential\n", encoding="utf-8")
        with patch.dict("os.environ", {"OPENAI_API_KEY": "inherited-test-credential"}):
            settings = load_settings(env_file)
        self.assertEqual(next(p for p in settings.providers if p.name == "openai").key, "dedicated-test-credential")

    def test_management_api_auth_and_status_redaction(self):
        secret = "must-not-appear-in-status"
        settings = replace(self.settings, providers=(self.spec("openai", key=secret),), remote_enabled=False)
        app = create_app(settings)
        status, _ = asyncio.run(request_asgi(app, "/api/v1/status"))
        self.assertEqual(status, 401)
        status, data = asyncio.run(request_asgi(app, "/api/v1/status", token=settings.admin_token))
        self.assertEqual(status, 200)
        self.assertNotIn(secret, json.dumps(data))
        self.assertNotIn(settings.admin_token, json.dumps(data))
        status, data = asyncio.run(request_asgi(app, "/api/v1/chat", token=settings.admin_token, method="POST", body={"text": "offline"}))
        self.assertEqual(status, 503)
        self.assertEqual(data["error"], "REMOTE_CALLS_DISABLED")

    def test_invalid_or_oversized_request_does_not_echo_content(self):
        app = create_app(self.settings)
        private_text = "private-test-marker"
        status, data = asyncio.run(request_asgi(app, "/api/v1/chat", token=self.settings.admin_token, method="POST", body={"text": private_text, "unknown": private_text}))
        self.assertEqual(status, 422)
        self.assertNotIn(private_text, json.dumps(data))
        status, data = asyncio.run(request_asgi(app, "/api/v1/chat", token=self.settings.admin_token, method="POST", body=b"x" * 17000))
        self.assertEqual(status, 413)

    def test_openai_parser_excludes_reasoning_and_counts_reported_usage(self):
        class FakeTransport:
            def post(self, url, headers, body, timeout):
                self.body = body
                return {"output": [{"type": "reasoning", "summary": []}, {"type": "message", "content": [{"type": "output_text", "text": "测试回复"}]}],
                        "usage": {"input_tokens": 12, "output_tokens": 8, "input_tokens_details": {"cached_tokens": 4}}}
        transport = FakeTransport()
        result = OpenAIProvider(self.spec("openai"), transport=transport).generate("system", [{"role": "user", "content": "offline"}], 64)
        self.assertEqual(result.text, "测试回复")
        self.assertEqual(result.usage, Usage(12, 8, 4))
        self.assertFalse(transport.body["store"])


if __name__ == "__main__":
    unittest.main()
