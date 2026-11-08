from dataclasses import replace
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from app.billing_status import check_billing
from app.config import Settings, ProviderSpec
from app.delivery_status import summarize, render
from app.errors import XiaoNuoError


class BillingStatusTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory(); self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.settings = Settings(root=self.root, providers=(ProviderSpec('deepseek','deepseek-flash','PRIVATE-KEY'),),
            monthly_budget_cny=Decimal('20'), cny_to_usd=Decimal('.15'), fx_checked_on='2026-11-05')
        self.budget = dict(currency='CNY', remaining='20', server_cost='0', paid_block_reason=None)
        clock=patch('app.budgeting.now',return_value=datetime(2026,11,6,tzinfo=timezone.utc))
        clock.start(); self.addCleanup(clock.stop)

    def check(self):return check_billing(self.settings,self.budget)

    def test_ready_without_network_or_creating_database_or_cache(self):
        with patch('urllib.request.OpenerDirector.open',side_effect=AssertionError('No network allowed')):
            self.assertEqual(self.check(),{'ready':True,'code':'BILLING_READY'})
        self.assertEqual(list(self.root.iterdir()),[])

    def test_positive_balance_below_full_reservation_is_not_ready(self):
        self.budget['remaining']='1'
        self.assertEqual(self.check()['code'],'BUDGET_EXHAUSTED')
        self.budget['remaining']='2.204095'
        self.assertFalse(self.check()['ready'])
        self.budget['remaining']='2.204096'
        self.assertTrue(self.check()['ready'])

    def test_cny_reservation_readiness_does_not_change_with_valid_fx(self):
        self.budget['remaining']='2.204096'
        for rate in ('.1','.2','.3'):
            self.settings=replace(self.settings,cny_to_usd=Decimal(rate))
            self.assertTrue(self.check()['ready'])

    def test_usd_legacy_budget_uses_usd_reservation_not_cny(self):
        self.budget.update(currency='USD',remaining='.330614')
        self.assertFalse(self.check()['ready'])
        self.budget['remaining']='.330615'
        self.assertTrue(self.check()['ready'])

    def test_price_expiry_is_detected_even_with_valid_credentials_and_balance(self):
        with patch('app.budgeting.now',return_value=datetime(2026,11,28,tzinfo=timezone.utc)):
            self.assertEqual(self.check()['code'],'PRICE_SNAPSHOT_EXPIRED')

    def test_fx_expiry_is_not_reported_as_hardware_or_budget_failure(self):
        self.settings=replace(self.settings,fx_checked_on='2026-10-28')
        self.assertEqual(self.check()['code'],'FX_RATE_NOT_CONFIRMED')

    def test_unknown_server_cost_and_invalid_values_do_not_pass(self):
        self.budget['server_cost']=None
        self.assertEqual(self.check()['code'],'SERVER_COST_NOT_CONFIRMED')
        self.budget['server_cost']='0'
        for value in (None,True,'NaN','Infinity','-1','PRIVATE-ERROR'):
            self.budget['remaining']=value
            self.assertFalse(self.check()['ready'])

    def test_native_history_and_price_bound_blocks_remain_blocked(self):
        for code in ('CNY_HISTORY_REVIEW_REQUIRED','PRICE_BOUND_EXCEEDED'):
            self.budget['paid_block_reason']=code
            self.assertEqual(self.check()['code'],code)

    def test_unknown_private_errors_are_never_reflected(self):
        self.budget['paid_block_reason']='PRIVATE-ERROR'
        self.assertEqual(self.check()['code'],'BILLING_NOT_CONFIRMED')
        self.budget['paid_block_reason']=None
        with patch('app.billing_status.price_for',side_effect=XiaoNuoError('PRIVATE-ERROR')):
            self.assertNotIn('PRIVATE',str(self.check()))

    def test_missing_or_contradictory_block_status_cannot_claim_ready(self):
        del self.budget['paid_block_reason']
        self.assertFalse(self.check()['ready'])
        self.budget['paid_block_reason']='BILLING_READY'
        self.assertFalse(self.check()['ready'])

    def test_malformed_budget_or_missing_model_returns_fixed_code(self):
        for value in (None,[],{},'PRIVATE-RESPONSE'):
            self.assertFalse(check_billing(self.settings,value)['ready'])
        self.settings=replace(self.settings,providers=())
        self.assertEqual(self.check()['code'],'MODEL_PRICE_NOT_CONFIGURED')

    def test_report_does_not_echo_forged_billing_code(self):
        service={'billing':{'ready':False,'code':'PRIVATE-TOKEN'}}
        result=summarize({},service)
        self.assertNotIn('PRIVATE',str(result)+render(result))
        self.assertEqual(next(c for c in result['checks'] if c['check']=='paid_request')['code'],'BILLING_NOT_CONFIRMED')

    def test_stopped_backend_cannot_have_paid_ready_check(self):
        result=summarize({}, {'billing':self.check()})
        self.assertEqual(next(c for c in result['checks'] if c['check']=='paid_request')['level'],'WARN')


if __name__=='__main__':unittest.main()
