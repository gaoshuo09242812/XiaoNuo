import asyncio
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from uuid import uuid4

from app.budgeting import BudgetManager, Price, UsageLedger
from app.config import Settings, ProviderSpec, load_settings
from app.errors import XiaoNuoError
from app.fx_rates import reference, save_reference
from app.llm.providers import Usage
from app.main import create_app
from test_core import request_asgi
from tools.configure_deepseek import configure


class CnyBudgetTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.settings = Settings(root=self.root, admin_token='offline-owner-'+'x'*40,
            monthly_budget=Decimal('.01'), monthly_server_cost=Decimal('0'),
            monthly_budget_cny=Decimal('20'), monthly_server_cost_cny=Decimal('0'))
        self.ledger = UsageLedger(self.settings.database)
        self.budget = BudgetManager(self.ledger, self.settings)

    def price(self, rate='.15', currency='CNY'):
        return Price('deepseek', 'fixture', currency, Decimal('1'), Decimal('0'),
                     Decimal('0'), Decimal(rate), 'fixture', input_ceiling=1000000)

    def test_twenty_cny_is_not_twenty_usd_or_old_usd_limit(self):
        self.budget.reserve(str(uuid4()), self.price(), 32)
        snap = self.budget.snapshot()
        self.assertEqual((snap['currency'], snap['budget'], snap['remaining']), ('CNY', '20', '19'))
        self.assertEqual(snap['outstanding_reservations_cny'], '1')
        self.assertIsNone(snap['budget_usd'])
        self.assertIsNone(snap['remaining_usd'])

    def test_concurrent_reservations_cannot_exceed_twenty_cny(self):
        def reserve(_):
            try:
                self.budget.reserve(str(uuid4()), self.price(), 32)
                return True
            except XiaoNuoError as error:
                self.assertEqual(error.code, 'BUDGET_EXHAUSTED')
                return False
        with ThreadPoolExecutor(max_workers=12) as pool:
            results = list(pool.map(reserve, range(30)))
        self.assertEqual(sum(results), 20)
        self.assertEqual(self.budget.snapshot()['remaining_cny'], '0')

    def test_exact_cny_limit_does_not_move_with_fx(self):
        self.budget.reserve(str(uuid4()), self.price('.10'), 32)
        self.budget.reserve(str(uuid4()), self.price('.20'), 32)
        self.assertEqual(self.budget.snapshot()['outstanding_reservations_cny'], '2')
        self.assertEqual(self.budget.snapshot()['remaining_cny'], '18')

    def test_settlement_counts_native_amount_and_releases_hold(self):
        price = self.price()
        reservation = self.budget.reserve(str(uuid4()), price, 32)
        self.budget.settle(reservation, Usage(250000, 0), price)
        snap = self.budget.snapshot()
        self.assertEqual(snap['api_cost_estimate_cny'], '0.25')
        self.assertEqual(snap['outstanding_reservations_cny'], '0')
        self.assertEqual(snap['remaining_cny'], '19.75')
        self.assertEqual(snap['categories']['LLM']['estimated_cny'], '0.25')

    def test_unknown_request_survives_restart_and_month_rollover(self):
        reservation = self.budget.reserve(str(uuid4()), self.price(), 32)
        self.budget.uncertain(reservation)
        with self.ledger.connect() as db:
            db.execute("UPDATE budget_reservations SET month='2026-10' WHERE id=?", (reservation,))
        restarted = BudgetManager(UsageLedger(self.settings.database), self.settings)
        self.assertEqual(restarted.snapshot()['outstanding_reservations_cny'], '1')
        self.assertEqual(restarted.snapshot()['uncertain_requests'], 1)

    def test_legacy_uncertain_hold_uses_original_rate_conservatively(self):
        reservation = self.budget.reserve(str(uuid4()), self.price('.123456789'), 32)
        with self.ledger.connect() as db:
            db.execute('UPDATE budget_reservations SET amount_microcny=NULL WHERE id=?', (reservation,))
        snap = self.budget.snapshot()
        held = Decimal(snap['outstanding_reservations_cny'])
        self.assertGreaterEqual(held, Decimal('1'))
        self.assertLess(held, Decimal('1.00001'))

    def test_existing_native_cost_is_preserved_and_included(self):
        old = BudgetManager(self.ledger, replace(self.settings, monthly_budget_cny=None, monthly_budget=Decimal('5')))
        price = self.price()
        reservation = old.reserve(str(uuid4()), price, 32)
        old.settle(reservation, Usage(500000, 0), price)
        with self.ledger.connect() as db:
            before = [tuple(r) for r in db.execute('SELECT * FROM api_usage')]
        self.assertEqual(self.budget.snapshot()['remaining_cny'], '19.5')
        with self.ledger.connect() as db:
            self.assertEqual(before, [tuple(r) for r in db.execute('SELECT * FROM api_usage')])

    def test_free_and_local_usd_zero_records_are_not_paid_charges(self):
        self.budget.record_local(str(uuid4()), 'vosk', 'fixture', 'ASR')
        self.budget.record_free(str(uuid4()), 'fixture', 'fixture', Usage(10, 1))
        self.assertEqual(self.budget.snapshot()['api_cost_estimate_cny'], '0')
        self.assertEqual(self.budget.snapshot()['remaining_cny'], '20')

    def test_foreign_paid_history_fails_closed_without_breaking_status(self):
        old = BudgetManager(self.ledger, replace(self.settings, monthly_budget_cny=None, monthly_budget=Decimal('5')))
        price = self.price('1', 'USD')
        reservation = old.reserve(str(uuid4()), price, 32)
        old.settle(reservation, Usage(500000, 0), price)
        snap = self.budget.snapshot()
        self.assertEqual(snap['paid_block_reason'], 'CNY_HISTORY_REVIEW_REQUIRED')
        self.assertIsNone(snap['remaining_cny'])
        with self.assertRaisesRegex(XiaoNuoError, 'CNY_HISTORY_REVIEW_REQUIRED'):
            self.budget.reserve(str(uuid4()), self.price(), 32)

    def test_foreign_paid_reservation_is_rejected_before_transport(self):
        with self.assertRaisesRegex(XiaoNuoError, 'CNY_PAID_CURRENCY_REQUIRED'):
            self.budget.reserve(str(uuid4()), self.price('1', 'USD'), 32)

    def test_server_cost_and_lower_limit_are_enforced_without_resetting_usage(self):
        reservation = self.budget.reserve(str(uuid4()), self.price(), 32)
        self.budget.settle(reservation, Usage(500000, 0), self.price())
        self.budget.set_limits_cny(Decimal('1'), Decimal('.25'))
        self.assertEqual(self.budget.snapshot()['remaining_cny'], '0.25')
        with self.assertRaisesRegex(XiaoNuoError, 'BUDGET_EXHAUSTED'):
            self.budget.reserve(str(uuid4()), self.price(), 32)
        self.assertEqual(self.budget.snapshot()['api_cost_estimate_cny'], '0.5')

    def test_unknown_server_cost_blocks_reservation(self):
        self.budget.set_limits_cny(Decimal('20'), None)
        with self.assertRaisesRegex(XiaoNuoError, 'SERVER_COST_NOT_CONFIRMED'):
            self.budget.reserve(str(uuid4()), self.price(), 32)

    def test_usd_update_or_invoice_cannot_silently_change_cny_mode(self):
        with self.assertRaisesRegex(XiaoNuoError, 'BUDGET_CURRENCY_MISMATCH'):
            self.budget.set_limits(Decimal('20'), Decimal('0'))
        with self.assertRaisesRegex(XiaoNuoError, 'BUDGET_CURRENCY_MISMATCH'):
            self.budget.reconcile(str(uuid4()), Decimal('1'), 'fixture')

    def test_explicit_native_limit_persists_without_env_and_old_usd_override_is_ignored(self):
        self.budget.set_limits_cny(Decimal('20'), Decimal('0'))
        legacy_settings = replace(self.settings, monthly_budget_cny=None)
        restarted = BudgetManager(UsageLedger(self.settings.database), legacy_settings)
        self.assertEqual((restarted.snapshot()['currency'], restarted.snapshot()['budget']), ('CNY', '20'))

    def test_migration_retains_pending_holds(self):
        reservation = self.budget.reserve(str(uuid4()), self.price(), 32)
        with self.ledger.connect() as db:
            db.execute('ALTER TABLE budget_reservations DROP COLUMN amount_microcny')
        migrated = BudgetManager(UsageLedger(self.settings.database), self.settings)
        self.assertEqual(migrated.snapshot()['outstanding_reservations_cny'], '1')
        with self.ledger.connect() as db:
            self.assertEqual(db.execute('SELECT state FROM budget_reservations WHERE id=?', (reservation,)).fetchone()[0], 'pending')

    def test_invalid_native_cost_blocks_instead_of_zeroing_it(self):
        reservation = self.budget.reserve(str(uuid4()), self.price(), 32)
        self.budget.settle(reservation, Usage(10, 0), self.price())
        for value in ('NaN', '-1', 'not-money'):
            with self.ledger.connect() as db:
                db.execute('UPDATE api_usage SET native_cost=?', (value,))
            self.assertEqual(self.budget.snapshot()['paid_block_reason'], 'CNY_HISTORY_REVIEW_REQUIRED')

    def test_price_bound_exceeded_still_blocks_all_paid_requests(self):
        price = self.price()
        reservation = self.budget.reserve(str(uuid4()), price, 32)
        self.budget.settle(reservation, Usage(2000000, 0), price)
        self.assertEqual(self.budget.snapshot()['paid_block_reason'], 'PRICE_BOUND_EXCEEDED')
        with self.assertRaisesRegex(XiaoNuoError, 'PRICE_BOUND_EXCEEDED'):
            self.budget.reserve(str(uuid4()), price, 32)

    def test_api_uses_explicit_cny_and_rejects_ambiguous_or_usd_payloads(self):
        app = create_app(self.settings)
        for body, expected in [({'monthly_cny':'20','server_cny':'0'},200),
                               ({'monthly_usd':'20','server_usd':'0'},409),
                               ({'monthly_cny':'20','monthly_usd':'20'},422),
                               ({'monthly_cny':'20','server_usd':'1'},422), ({},422)]:
            code, response = asyncio.run(request_asgi(app, '/api/v1/budget', token=self.settings.admin_token, method='PUT', body=body))
            self.assertEqual(code, expected)
            if code == 200:
                self.assertEqual((response['currency'],response['budget_cny']), ('CNY','20'))

    def test_native_config_loads_without_overwriting_legacy_fields(self):
        path = self.root/'.env.local'
        path.write_text('XIAONUO_MONTHLY_BUDGET_CNY=20\nXIAONUO_MONTHLY_SERVER_COST_CNY=0\nXIAONUO_MONTHLY_BUDGET_USD=5\n')
        settings = load_settings(path)
        self.assertEqual(settings.monthly_budget_cny, Decimal('20'))
        self.assertEqual(settings.monthly_budget, Decimal('5'))

    def test_native_budget_preflight_and_routed_deepseek_settlement(self):
        from app.main import build_router
        from app.network_chat import require_deepseek_chat
        spec = ProviderSpec('deepseek','deepseek-flash','sk-offline-fixture-not-real',False,True)
        settings = replace(self.settings, remote_enabled=True,allow_paid=True,primary_paid='deepseek',
                           deployment_region='CN',providers=(spec,),cny_to_usd=Decimal('.15'),fx_checked_on='2026-11-05')
        router = build_router(settings)
        transport = unittest.mock.Mock()
        transport.post.return_value={'choices':[{'message':{'content':'公开测试回复'}}],
                                     'usage':{'prompt_tokens':100,'completion_tokens':10,'prompt_cache_hit_tokens':0}}
        router.providers['deepseek'].transport=transport
        with patch('app.budgeting.now',return_value=datetime(2026,11,6,tzinfo=timezone.utc)):
            require_deepseek_chat(router,preflight=True)
            answer=router.reply(str(uuid4()),'公开测试',[{'role':'user','content':'你好'}])
            self.assertEqual(answer.provider,'deepseek')
            self.assertEqual(router.budget.snapshot()['api_cost_estimate_cny'],'0.00028')
            self.assertEqual(router.budget.snapshot()['outstanding_reservations_cny'],'0')
        transport.post.assert_called_once()


class SetupFxRepairTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory();self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.path = self.root/'.env.local'
        self.path.write_text('# owner\nXIAONUO_MONTHLY_BUDGET_CNY=20\nXIAONUO_MONTHLY_BUDGET_USD=5\nXIAONUO_MONTHLY_SERVER_COST_CNY=0\nXIAONUO_CNY_TO_USD=.15\nXIAONUO_FX_CHECKED_ON=2026-10-28\n')
        clock = patch('app.budgeting.now',return_value=datetime(2026,11,6,tzinfo=timezone.utc))
        clock.start();self.addCleanup(clock.stop)
        self.key = 'sk-fixture-not-a-real-key'

    def verified(self,key):return {'authenticated':True,'balance_available':True}

    def refresh(self,settings,*,at):
        self.assertTrue(settings.remote_enabled and settings.allow_paid)
        self.assertEqual(settings.primary_paid,'deepseek')
        save_reference(settings, reference('1.12','7.5','2026-11-05',at))

    def test_expired_installer_reference_refreshes_and_keeps_twenty_cny(self):
        result = configure(self.root,self.key,verify=self.verified,fx_refresh=self.refresh)
        loaded = load_settings(self.path)
        self.assertEqual(loaded.fx_checked_on,'2026-11-05')
        self.assertEqual(loaded.monthly_budget_cny,Decimal('20'))
        self.assertEqual((result['budget_currency'],result['monthly_budget']),('CNY','20'))
        self.assertNotIn(self.key,str(result))

    def test_offline_expired_reference_preserves_original_config(self):
        original=self.path.read_bytes()
        with self.assertRaisesRegex(XiaoNuoError,'FX_RATE_NOT_CONFIRMED'):
            configure(self.root,self.key,verify=self.verified,fx_refresh=lambda *a,**k:None)
        self.assertEqual(self.path.read_bytes(),original)

    def test_recent_config_can_be_used_offline_without_redating(self):
        self.path.write_text(self.path.read_text().replace('2026-10-28','2026-11-05'))
        configure(self.root,self.key,verify=self.verified,fx_refresh=lambda *a,**k:None)
        self.assertEqual(load_settings(self.path).fx_checked_on,'2026-11-05')

    def test_concurrent_edit_during_reference_refresh_is_preserved(self):
        def concurrent(settings,*,at):
            self.refresh(settings,at=at)
            self.path.write_text('XIAONUO_ADMIN_TOKEN=user-edit\n')
        with self.assertRaisesRegex(XiaoNuoError,'ENV_FILE_CHANGED_DURING_SETUP'):
            configure(self.root,self.key,verify=self.verified,fx_refresh=concurrent)
        self.assertEqual(self.path.read_text(),'XIAONUO_ADMIN_TOKEN=user-edit\n')


if __name__ == '__main__':unittest.main()
