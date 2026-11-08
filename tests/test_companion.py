"""P2-P4 checks use temporary databases and explicit fake model providers."""
import asyncio
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch
from uuid import uuid4

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))
from app.behavior import Context, ContextAnalyzer, QuietMode, ResponseStyleManager
from app.budgeting import BudgetManager, Price, UsageLedger, price_for
from app.companion import CompanionService, current_time_context
from app.config import ProviderSpec, Settings, load_settings
from app.errors import XiaoNuoError
from app.llm.health import ProviderHealthMonitor
from app.llm.providers import Completion, Usage
from app.llm.router import ModelRouter
from app.main import create_app
from app.storage import CompanionStore
from test_core import FakeProvider, request_asgi


class CompanionTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.settings = Settings(root=Path(self.directory.name), admin_token="offline-test-" + "x" * 40,
            usage_region="US", deployment_region="US", remote_enabled=True, allow_paid=True, monthly_server_cost=Decimal("0"))
        self.ledger = UsageLedger(self.settings.database)
        self.budget = BudgetManager(self.ledger, self.settings)
        self.store = CompanionStore(self.ledger)
        self.health = ProviderHealthMonitor(self.ledger)
        self.paid = FakeProvider(ProviderSpec("openai", "gpt-6-luna", "fake-not-real", region_approved=True))
        self.router = ModelRouter(self.settings, {"openai": self.paid}, self.budget, self.health)
        self.service = CompanionService(self.router, self.store)

    def chat(self, text):
        return self.service.chat(text, str(uuid4()), None)

    def price(self):
        return Price("test", "test", "USD", Decimal("1"), Decimal("0.1"), Decimal("2"), Decimal("1"), "test-version")

    def test_rain_explanation_reaches_model_without_weather_request(self):
        with patch('app.companion.weather_reply') as weather:
            answer=self.chat('用三句话解释一下为什么会下雨')
        weather.assert_not_called()
        self.assertEqual(answer['diagnostics']['provider'],'openai')
        self.assertGreater(answer['diagnostics']['model_characters'],0)
        self.assertEqual(len(self.paid.calls),1)

    def test_forecast_stays_local_without_calling_chat_model(self):
        with patch('app.companion.weather_reply',return_value='公开天气夹具。') as weather:
            answer=self.chat('北京今天会不会下雨')
        weather.assert_called_once_with('北京今天会不会下雨')
        self.assertEqual(answer['diagnostics']['action'],'weather')
        self.assertFalse(self.paid.calls)

    def test_memory_requires_explicit_consent(self):
        with self.assertRaisesRegex(XiaoNuoError, "MEMORY_CONSENT_REQUIRED"):
            self.store.add_memory("我喜欢蓝色", consent=False)
        self.assertEqual(self.store.list_memories(), [])

    def test_unavailable_hardware_cannot_be_claimed_by_model(self):
        for command in ['打开开发板喇叭播放音乐']:
            answer=self.chat(command)
            self.assertEqual(answer['diagnostics']['action'],'hardware_unavailable')
        self.assertEqual(self.paid.calls,[])
        answer=self.chat('介绍一下扬声器的工作原理')
        self.assertEqual(answer['diagnostics']['provider'],'openai')

    def test_manual_speaker_guidance_tracks_live_capability_without_playing(self):
        device={'connected':True,'speaker_enabled':True,'info':{'speaker_available':True}}
        self.service.device_status=lambda:device
        answer=self.chat('打开开发板喇叭播放声音')
        self.assertEqual(answer['diagnostics']['action'],'manual_playback_required')
        self.assertIn('这条聊天没有启动播放',answer['reply_text'])
        for changed in [{'connected':False},{'speaker_enabled':False},{'info':{'speaker_available':False}}]:
            self.service.device_status=lambda change=changed:device|change
            answer=self.chat('打开开发板喇叭播放声音')
            self.assertEqual(answer['diagnostics']['action'],'hardware_unavailable')
        self.assertEqual(self.paid.calls,[])

    def test_tired_statement_uses_brief_response_without_diagnosis(self):
        for text in ['今天有点累。','我今天很困了','有点疲惫']:
            context=ContextAnalyzer().analyze(text)
            self.assertEqual(context.label,'tired')
            style=ResponseStyleManager().choose(context,self.store.controls())
            self.assertEqual(style.response_style.length,'brief')
            self.assertEqual(style.response_style.cuteness,0)

    def test_no_followup_removes_math_tag_question(self):
        style=ResponseStyleManager().choose(ContextAnalyzer().analyze('一加一等于几'),self.store.controls())
        self.assertEqual(ResponseStyleManager.enforce('一加一等于二，对吧？',style),'一加一等于二。')
        self.assertEqual(ResponseStyleManager.enforce('你最喜欢的颜色是蓝色，是不是？',style),'你最喜欢的颜色是蓝色。')

    def test_memory_survives_restart_and_is_owner_scoped(self):
        saved = self.store.add_memory("我喜欢蓝色", consent=True)
        restarted = CompanionStore(UsageLedger(self.settings.database))
        self.assertEqual(restarted.memory(saved["id"])["content"], "我喜欢蓝色")
        with self.assertRaisesRegex(XiaoNuoError, "MEMORY_NOT_FOUND"):
            restarted.memory(saved["id"], owner="other-owner")

    def test_memory_creation_is_idempotent_under_concurrency(self):
        request_id = str(uuid4())
        with ThreadPoolExecutor(max_workers=8) as executor:
            results = list(executor.map(lambda _: self.store.add_memory("离线测试偏好", consent=True, request_id=request_id), range(16)))
        self.assertEqual(len({r["id"] for r in results}), 1)
        self.assertEqual(len(self.store.list_memories()), 1)
        with self.assertRaisesRegex(XiaoNuoError, "DUPLICATE_MEMORY_REQUEST_CONFLICT"):
            self.store.add_memory("不同内容", consent=True, request_id=request_id)

    def test_memory_update_rejects_stale_versions(self):
        saved = self.store.add_memory("喜欢蓝色", consent=True)
        updated = self.store.update_memory(saved["id"], "喜欢绿色", consent=True, expected_version=1, allow_remote=False)
        self.assertEqual(updated["version"], 2)
        with self.assertRaisesRegex(XiaoNuoError, "MEMORY_NOT_FOUND_OR_STALE"):
            self.store.update_memory(saved["id"], "旧修改", consent=True, expected_version=1, allow_remote=False)

    def test_expired_memories_are_not_retrieved(self):
        saved = self.store.add_memory("喜欢蓝色", consent=True)
        with self.ledger.connect() as db:
            db.execute("UPDATE memories SET expires_at=? WHERE id=?", ((datetime.now(timezone.utc) - timedelta(days=1)).isoformat(), saved["id"]))
        self.assertEqual(self.store.list_memories(), [])

    def test_cloud_memory_requires_global_and_record_authorization(self):
        self.store.add_memory("我喜欢蓝色猫咪", consent=True, allow_remote=False)
        permitted = self.store.add_memory("我喜欢蓝色天空", consent=True, allow_remote=True)
        self.assertEqual(self.store.retrieve("蓝色", remote=True), [])
        self.store.patch_controls({"memory_upload_enabled": True})
        results = self.store.retrieve("蓝色", remote=True)
        self.assertEqual([r["id"] for r in results], [permitted["id"]])

    def test_provider_fallback_uses_same_authorized_memory(self):
        self.store.patch_controls({"memory_upload_enabled": True})
        self.store.add_memory("我喜欢蓝色", consent=True, allow_remote=True)
        free = FakeProvider(ProviderSpec("groq", "fake-free-model", "fake-key", True, True))
        self.health.report(free.spec, verify_free=True)
        self.paid.fail = True
        self.router.providers["groq"] = free
        response = self.chat("你记得我喜欢什么蓝色吗")
        self.assertEqual(response["diagnostics"]["provider"], "groq")
        self.assertEqual(self.paid.calls[0][0], free.calls[0][0])
        self.assertIn("我喜欢蓝色", free.calls[0][0])

    def test_explicit_memory_command_saves_once_without_model_call(self):
        request_id = str(uuid4())
        first = self.service.chat("记住：我喜欢安静的环境", request_id, None)
        second = self.service.chat("记住：我喜欢安静的环境", request_id, None)
        self.assertEqual(first["memory_id"], second["memory_id"])
        self.assertEqual(len(self.store.list_memories()), 1)
        self.assertEqual(self.paid.calls, [])

    def test_ordinary_statements_are_not_automatically_saved(self):
        self.chat("我喜欢蓝色")
        self.assertEqual(self.store.list_memories(), [])

    def test_history_persistence_is_opt_in_and_survives_restart(self):
        conversation = str(uuid4())
        messages = [{"role": "user", "content": "离线测试"}]
        self.store.save_history(conversation, messages)
        self.assertEqual(self.store.load_history(conversation), [])
        self.store.patch_controls({"persist_history": True})
        self.store.save_history(conversation, messages)
        restarted = CompanionStore(UsageLedger(self.settings.database))
        self.assertEqual(restarted.load_history(conversation), messages)
        restarted.patch_controls({"persist_history": False})
        with self.ledger.connect() as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM messages").fetchone()[0], 0)

    def test_quiet_survives_restart_and_ordinary_input_does_not_wake(self):
        self.chat("我想安静一会儿")
        service = CompanionService(self.router, CompanionStore(UsageLedger(self.settings.database)))
        response = service.chat("今天天气怎么样", str(uuid4()), None)
        self.assertEqual(response["interaction_state"], "QUIET")
        self.assertFalse(response["speech_style"]["enabled"])
        self.assertEqual(self.paid.calls, [])

    def test_history_rewrites_do_not_refresh_old_message_retention(self):
        self.store.patch_controls({"persist_history": True})
        conversation = str(uuid4())
        old = [{"role": "user", "content": "旧测试内容"}, {"role": "assistant", "content": "旧回复"}]
        recent = [{"role": "user", "content": "新测试内容"}, {"role": "assistant", "content": "新回复"}]
        with patch("app.storage.now", return_value=datetime(2026, 10, 1, tzinfo=timezone.utc)):
            self.store.save_history(conversation, old)
        with patch("app.storage.now", return_value=datetime(2026, 10, 5, tzinfo=timezone.utc)):
            self.store.save_history(conversation, old + recent)
        with patch("app.storage.now", return_value=datetime(2026, 10, 9, tzinfo=timezone.utc)):
            self.assertEqual(self.store.load_history(conversation), recent)

    def test_shorter_history_retention_removes_old_saved_content(self):
        self.store.patch_controls({"persist_history": True, "history_retention_days": 30})
        with patch("app.storage.now", return_value=datetime.now(timezone.utc) - timedelta(days=10)):
            self.store.save_history(str(uuid4()), [{"role": "user", "content": "旧测试内容"}])
        self.store.patch_controls({"history_retention_days": 1})
        with self.ledger.connect() as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM messages").fetchone()[0], 0)

    def test_explicit_wake_restores_interaction(self):
        self.chat("我想安静一会儿")
        response = self.chat("小糯")
        self.assertFalse(self.store.controls()["quiet"])
        self.assertEqual(response["reply_text"], "我在。")

    def test_weather_question_uses_local_lookup_without_paid_call(self):
        with patch("app.companion.weather_reply", return_value="上海：现在 23°C，晴。"):
            response = self.chat("今天天气怎么样")
        self.assertEqual(response["reply_text"], "上海：现在 23°C，晴。")
        self.assertEqual(response["diagnostics"]["action"], "weather")
        self.assertEqual(self.paid.calls, [])

    def test_current_time_context_uses_shanghai_clock(self):
        text = current_time_context(
            "Asia/Shanghai",
            datetime(2026, 10, 31, 4, 5, 6, tzinfo=timezone.utc),
        )
        self.assertIn("2026年10月31日", text)
        self.assertIn("星期六", text)
        self.assertIn("12:05:06", text)
        self.assertIn("不要说你无法查看日期或时间", text)

    def test_named_quiet_without_separator_enters_and_exits(self):
        response = self.chat("小糯我想安静一会儿")
        self.assertTrue(self.store.controls()["quiet"])
        self.assertEqual(response["interaction_state"], "QUIET")
        self.assertFalse(response["speech_style"]["enabled"])
        response = self.chat("小糯解除安静")
        self.assertFalse(self.store.controls()["quiet"])
        self.assertEqual(response["reply_text"], "我在。")

    def test_fatigue_is_not_an_implicit_quiet_command(self):
        response = self.chat("我累了")
        self.assertFalse(self.store.controls()["quiet"])
        self.assertEqual(response["diagnostics"]["context"], "tired")

    def test_explicit_directives_are_persisted_without_paid_calls(self):
        self.chat("别问了")
        self.chat("认真一点")
        self.chat("简单说")
        controls = CompanionStore(UsageLedger(self.settings.database)).controls()
        self.assertFalse(controls["followup_enabled"])
        self.assertTrue(controls["serious"])
        self.assertEqual(controls["reply_length"], "brief")
        self.assertEqual(self.paid.calls, [])

    def test_no_followup_policy_removes_mechanical_question_tail(self):
        self.paid.generate = lambda *args: Completion("Scorpio，天蝎座。你想聊聊星座吗？", Usage(20, 12))
        self.chat("别问了")
        response = self.chat("天蝎座的英文是什么")
        self.assertEqual(response["reply_text"], "Scorpio，天蝎座。")

    def test_no_followup_policy_handles_user_scorpio_example(self):
        self.paid.generate = lambda *args: Completion("Scorpio，读作斯考皮欧，你是不是喜欢某个天蝎座的人？", Usage(20, 30))
        response = self.chat("天蝎座的英文是什么")
        self.assertEqual(response["reply_text"], "Scorpio，读作斯考皮欧。")

    def test_brief_reply_has_enforced_bound(self):
        self.chat("简单说")
        self.paid.generate = lambda *args: Completion("解释" * 1000, Usage(20, 2000))
        response = self.chat("解释一下这个问题")
        self.assertLessEqual(len(response["reply_text"]), 160)

    def test_explicit_detail_preference_overrides_inferred_fatigue(self):
        self.chat("详细解释")
        response = self.chat("我累了，但想理解这道题")
        self.assertEqual(response["response_style"]["length"], "detailed")

    def test_uncertain_context_has_neutral_display_and_no_motion(self):
        response = self.chat("好")
        self.assertEqual(response["emotion_display"], "neutral")
        self.assertFalse(response["diagnostics"]["voice_emotion_recognition"])

    def test_quiet_blocks_greeting_and_ordinary_reminder(self):
        quiet = QuietMode(self.store)
        self.store.patch_controls({"greeting_enabled": True})
        quiet.enter()
        daytime = datetime(2026, 10, 24, 4, tzinfo=timezone.utc)
        self.assertFalse(quiet.allows_proactive("greeting", at=daytime))
        self.assertFalse(quiet.allows_proactive("reminder", at=daytime))
        self.assertTrue(quiet.allows_proactive("reminder", quiet_exception=True, at=daytime))
        self.assertTrue(quiet.allows_proactive("system_safety", at=daytime))

    def test_tone_analysis_can_be_disabled_without_disabling_directives(self):
        self.store.patch_controls({"tone_analysis_enabled": False})
        response = self.chat("我今天很难过")
        self.assertEqual(response["diagnostics"]["context"], "normal")
        self.chat("认真一点")
        self.assertTrue(self.store.controls()["serious"])

    def test_unknown_preference_is_rejected(self):
        self.assertNotIn('unsupported_preference',self.store.controls())
        with self.assertRaisesRegex(XiaoNuoError,'UNKNOWN_PREFERENCE'):
            self.store.patch_controls({'unsupported_preference':True})

    def test_disabled_followup_removes_real_model_invitation_without_dropping_answer(self):
        styles=ResponseStyleManager()
        presentation=styles.choose(Context('excited',0.9),self.store.controls())
        for ending in ['有什么想分享的吗？','还有什么想聊的吗？','愿意分享一下吗？']:
            self.assertEqual(styles.enforce('今天也开心啦！'+ending,presentation),'今天也开心啦！')
        quoted='可以用这句练习：“有什么想分享的吗？”'
        self.assertEqual(styles.enforce(quoted,presentation),quoted)
        self.store.patch_controls({'followup_enabled':True})
        allowed=styles.choose(Context('excited',0.9),self.store.controls())
        self.assertEqual(styles.enforce('今天也开心啦！有什么想分享的吗？',allowed),'今天也开心啦！有什么想分享的吗？')

    def test_legacy_unknown_preference_is_removed_without_losing_saved_volume(self):
        import json
        with self.ledger.connect() as db:
            row=db.execute("SELECT preferences FROM user_profiles WHERE id='owner'").fetchone()
            prefs=json.loads(row[0]);prefs.update(unsupported_preference=True,volume=1.0)
            db.execute("UPDATE user_profiles SET preferences=? WHERE id='owner'",(json.dumps(prefs),))
        migrated=CompanionStore(self.ledger)
        self.assertNotIn('unsupported_preference',migrated.controls())
        self.assertEqual(migrated.controls()['volume'],1.0)
        with self.ledger.connect() as db:
            stored=json.loads(db.execute("SELECT preferences FROM user_profiles WHERE id='owner'").fetchone()[0])
        self.assertNotIn('unsupported_preference',stored)

    def test_api_rejects_unknown_preference(self):
        app=create_app(self.settings)
        status,_=asyncio.run(request_asgi(app,'/api/v1/preferences',method='PATCH',
            body={'unsupported_preference':True},token=self.settings.admin_token))
        self.assertEqual(status,422)

    def test_concurrent_quiet_command_wins_over_inflight_reply(self):
        def generating(*args):
            self.service.quiet.enter()
            return Completion("测试回复", Usage(20, 10))
        self.paid.generate = generating
        response = self.chat("解释一下这个问题")
        self.assertEqual(response["interaction_state"], "QUIET")
        self.assertFalse(response["speech_style"]["enabled"])

    def test_audio_categories_share_the_same_reservation_limit(self):
        self.budget.set_limits(Decimal("1"), Decimal("0"))
        self.budget.reserve_amount(str(uuid4()), provider="test", model="asr", category="ASR", amount_microusd=600000, price_version="test", price_snapshot={})
        with self.assertRaisesRegex(XiaoNuoError, "BUDGET_EXHAUSTED"):
            self.budget.reserve_amount(str(uuid4()), provider="test", model="tts", category="TTS", amount_microusd=500000, price_version="test", price_snapshot={})

    def test_budget_limits_persist_without_resetting_spending(self):
        self.budget.set_limits(Decimal("1.5"), Decimal("0.5"))
        restarted = BudgetManager(UsageLedger(self.settings.database), self.settings)
        self.assertEqual(restarted.snapshot()["budget_usd"], "1.5")
        self.assertEqual(restarted.snapshot()["remaining_usd"], "1")

    def test_missing_invoice_amount_is_unknown_instead_of_confirmed_zero(self):
        summary = self.budget.snapshot()
        self.assertIsNone(summary["owner_confirmed_cost_usd"])
        self.assertEqual(summary["confirmed_invoice_count"], 0)

    def test_reconciliation_requires_remote_disabled_and_is_idempotent(self):
        reservation = self.budget.reserve(str(uuid4()), self.price(), 32)
        self.budget.uncertain(reservation)
        with self.assertRaisesRegex(XiaoNuoError, "DISABLE_REMOTE_BEFORE_RECONCILIATION"):
            self.budget.reconcile(reservation, Decimal("0.2"), "offline-invoice")
        offline = BudgetManager(self.ledger, replace(self.settings, remote_enabled=False))
        offline.reconcile(reservation, Decimal("0.2"), "offline-invoice")
        offline.reconcile(reservation, Decimal("0.2"), "offline-invoice")
        summary = offline.snapshot()
        self.assertEqual(summary["outstanding_reservations_usd"], "0")
        self.assertEqual(summary["api_cost_estimate_usd"], "0.2")
        self.assertEqual(summary["usage_records"], 1)
        self.assertEqual(summary["categories"]["LLM"]["owner_confirmed_usd"], "0.2")

    def test_price_mismatch_keeps_reservation(self):
        price = self.price()
        reservation = self.budget.reserve(str(uuid4()), price, 32)
        with self.assertRaisesRegex(XiaoNuoError, "RESERVATION_PRICE_MISMATCH"):
            self.budget.settle(reservation, Usage(10, 10), replace(price, version="changed"))
        self.assertGreater(Decimal(self.budget.snapshot()["outstanding_reservations_usd"]), 0)

    def test_price_rates_cannot_change_under_the_same_version(self):
        price = self.price()
        reservation = self.budget.reserve(str(uuid4()), price, 32)
        with self.assertRaisesRegex(XiaoNuoError, "RESERVATION_PRICE_MISMATCH"):
            self.budget.settle(reservation, Usage(10, 10), replace(price, input_per_million=Decimal("2")))

    def test_cost_over_bound_blocks_future_paid_reservations(self):
        price = replace(self.price(), input_ceiling=1)
        reservation = self.budget.reserve(str(uuid4()), price, 32)
        self.budget.settle(reservation, Usage(1000, 1000), price)
        self.assertEqual(self.budget.snapshot()["paid_block_reason"], "PRICE_BOUND_EXCEEDED")
        with self.assertRaisesRegex(XiaoNuoError, "PRICE_BOUND_EXCEEDED"):
            self.budget.reserve(str(uuid4()), self.price(), 32)

    def test_stale_model_prices_are_not_silently_reused(self):
        with patch("app.budgeting.now", return_value=datetime(2027, 1, 1, tzinfo=timezone.utc)):
            with self.assertRaisesRegex(XiaoNuoError, "PRICE_SNAPSHOT_EXPIRED"):
                price_for(self.settings, "openai", "gpt-6-luna")

    def test_cn_defaults_to_domestic_candidate_without_enabling_calls(self):
        target = self.settings.root / ".env.local"
        target.write_text("XIAONUO_USAGE_REGION=CN\n", encoding="utf-8")
        with patch.dict("os.environ", {}, clear=True):
            loaded = load_settings(target)
        self.assertEqual(loaded.primary_paid, "deepseek")
        self.assertFalse(loaded.remote_enabled)
        self.assertFalse(loaded.allow_paid)

    def test_privacy_export_and_deletion_do_not_delete_cost_audit(self):
        self.store.add_memory("离线测试个人内容", consent=True)
        self.budget.record_free(str(uuid4()), "fake", "fake", Usage(1, 1))
        exported = self.store.export()
        self.assertEqual(len(exported["memories"]), 1)
        self.store.clear_personal_data()
        self.assertEqual(self.store.list_memories(), [])
        self.assertEqual(self.budget.snapshot()["usage_records"], 1)

    def test_memory_api_consent_versioning_and_auth(self):
        app = create_app(replace(self.settings, remote_enabled=False))
        async def run():
            status, _ = await request_asgi(app, "/api/v1/memories", method="POST", body={"content": "test", "consent": True})
            self.assertEqual(status, 401)
            status, _ = await request_asgi(app, "/api/v1/memories", token=self.settings.admin_token, method="POST", body={"content": "test", "consent": False})
            self.assertEqual(status, 422)
            status, saved = await request_asgi(app, "/api/v1/memories", token=self.settings.admin_token, method="POST", body={"content": "明确保存的测试内容", "consent": True})
            self.assertEqual(status, 200)
            status, updated = await request_asgi(app, "/api/v1/memories/" + saved["id"], token=self.settings.admin_token, method="PATCH", body={"content": "新的测试内容", "consent": True, "expected_version": 1})
            self.assertEqual(status, 200)
            self.assertEqual(updated["version"], 2)
            status, exported = await request_asgi(app, "/api/v1/privacy/export", token=self.settings.admin_token)
            self.assertEqual(status, 200)
            self.assertEqual(exported["memories"][0]["content"], "新的测试内容")
            status, _ = await request_asgi(app, "/api/v1/privacy/data", token=self.settings.admin_token, method="DELETE", body={"confirm": "WRONG"})
            self.assertEqual(status, 422)
            status, _ = await request_asgi(app, "/api/v1/privacy/data", token=self.settings.admin_token, method="DELETE", body={"confirm": "DELETE_PERSONAL_DATA"})
            self.assertEqual(status, 200)
            status, response = await request_asgi(app, "/api/v1/memories", token=self.settings.admin_token)
            self.assertEqual(response["items"], [])
        asyncio.run(run())


if __name__ == "__main__":
    unittest.main()
