import asyncio
from dataclasses import replace
from datetime import date,datetime,timezone,timedelta
from decimal import Decimal
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from app.config import Settings
from app.errors import XiaoNuoError
from app.budgeting import price_for,BudgetManager,UsageLedger
from app.fx_rates import (SOURCE,parse_reference,reference,refresh_if_due,
                          effective_fx,save_reference,cache_path,maintained_fx,fetch_reference)

DAY=date(2026,11,5)
def xml(day='2026-11-04',usd='1.1267',cny='7.5177'):
    return (f'<Envelope xmlns="http://www.ecb.int/vocabulary/2002-08-01/eurofxref">'
            f'<Cube><Cube time="{day}"><Cube currency="USD" rate="{usd}"/>'
            f'<Cube currency="CNY" rate="{cny}"/></Cube></Cube></Envelope>').encode()

class FxTests(unittest.TestCase):
    def setUp(self):
        temporary=tempfile.TemporaryDirectory();self.addCleanup(temporary.cleanup)
        self.settings=Settings(root=Path(temporary.name),remote_enabled=True,allow_paid=True,
            primary_paid='deepseek',monthly_server_cost=Decimal(0),cny_to_usd=Decimal('.15'),
            fx_checked_on='2026-10-28')

    def fetch(self,at):return parse_reference(xml(),at)

    def test_cross_rate_rounding_and_actual_publication_date(self):
        save_reference(self.settings,self.fetch(DAY))
        rate,day,source=effective_fx(self.settings,DAY)
        actual=Decimal('1.1267')/Decimal('7.5177')
        self.assertGreaterEqual(rate,actual);self.assertLess(rate-actual,Decimal('.000000000001'))
        self.assertEqual(day,date(2026,11,4));self.assertEqual(source,'ecb')

    def test_rejects_stale_future_nonfinite_missing_duplicate_and_oversized(self):
        samples=[xml('2026-10-28'),xml('2026-11-06'),xml('20261104'),xml(usd='NaN'),
            xml(cny='0'),xml(cny='Infinity'),xml().replace(b'currency="CNY"',b'currency="JPY"'),
            xml().replace(b'</Cube></Cube>',b'<Cube currency="USD" rate="1"/></Cube></Cube>'),
            b'<!DOCTYPE x>'+xml(),b'<!ENTITY x "bad">'+xml(),b'x'*32769]
        for raw in samples:
            with self.subTest(raw=raw[:40]),self.assertRaises(Exception):parse_reference(raw,DAY)

    def test_expired_settings_recover_and_next_restart_reuses_cache(self):
        with self.assertRaises(XiaoNuoError):effective_fx(self.settings,DAY)
        self.assertEqual(refresh_if_due(self.settings,at=DAY,fetch=self.fetch)['state'],'UPDATED')
        def no_fetch(at):raise AssertionError('fresh cache must not fetch')
        self.assertEqual(refresh_if_due(replace(self.settings),at=DAY,fetch=no_fetch)['state'],'CURRENT')
        with patch('app.budgeting.now',return_value=datetime(2026,11,5,tzinfo=timezone.utc)):
            price=price_for(self.settings,'deepseek','deepseek-flash')
            self.assertIn('-ecb',price.version)
            budget=BudgetManager(UsageLedger(self.settings.database),self.settings)
            self.assertEqual(self.settings.monthly_budget,Decimal(5))
            self.assertGreater(price.maximum(512),0)

    def test_outage_never_redates_or_replaces_valid_cache_and_expiry_blocks(self):
        save_reference(self.settings,self.fetch(DAY));before=cache_path(self.settings).read_bytes()
        def outage(at):raise TimeoutError('offline')
        self.assertEqual(refresh_if_due(self.settings,at=DAY,fetch=outage,force=True)['state'],'REFRESH_FAILED')
        self.assertEqual(cache_path(self.settings).read_bytes(),before)
        self.assertEqual(effective_fx(self.settings,DAY+timedelta(days=6))[1],date(2026,11,4))
        with self.assertRaisesRegex(XiaoNuoError,'FX_RATE_NOT_CONFIRMED'):
            effective_fx(self.settings,DAY+timedelta(days=7))

    def test_newer_explicit_setting_wins_and_new_data_used_without_reload(self):
        settings=replace(self.settings,fx_checked_on='2026-11-05',cny_to_usd=Decimal('.15'))
        save_reference(settings,self.fetch(DAY))
        self.assertEqual(effective_fx(settings,DAY)[2],'manual')
        save_reference(settings,reference('1.12','7.5','2026-11-06',DAY+timedelta(days=1)))
        self.assertEqual(effective_fx(settings,DAY+timedelta(days=1))[2],'ecb')

    def test_malformed_cache_no_rollback_and_wrong_source_preserves_file(self):
        path=cache_path(self.settings);path.parent.mkdir(parents=True)
        for raw in ('[]','{bad','{}','x'*32769):
            path.write_text(raw)
            with self.assertRaises(XiaoNuoError):effective_fx(self.settings,DAY)
        save_reference(self.settings,reference('1.12','7.5','2026-11-05',DAY))
        before=path.read_bytes()
        self.assertEqual(refresh_if_due(self.settings,at=DAY,fetch=self.fetch,force=True)['state'],'NEWER_CACHE_RETAINED')
        bad=self.fetch(DAY)|{'source':'https://example.invalid/'}
        self.assertEqual(refresh_if_due(self.settings,at=DAY,fetch=lambda at:bad,force=True)['state'],'REFRESH_FAILED')
        self.assertEqual(path.read_bytes(),before)

    def test_network_fixed_url_timeout_size_bound_and_no_authorization(self):
        from unittest.mock import MagicMock
        opener=MagicMock();response=opener.open.return_value.__enter__.return_value
        response.read.return_value=xml()
        with patch('app.fx_rates.build_opener',return_value=opener):fetch_reference(DAY)
        args,kwargs=opener.open.call_args
        self.assertEqual(args[0].full_url,SOURCE);self.assertNotIn('Authorization',args[0].headers)
        self.assertEqual(kwargs['timeout'],12);response.read.assert_called_once_with(32769)

    def test_disabled_remote_has_no_fetch_or_cache(self):
        for change in ({'remote_enabled':False},{'allow_paid':False},{'primary_paid':'local_qwen'}):
            with self.subTest(change=change):
                result=refresh_if_due(replace(self.settings,**change),at=DAY,fetch=lambda at:self.fail('network'))
                self.assertEqual(result['state'],'NOT_REQUIRED')
        self.assertFalse(cache_path(self.settings).exists())

class MaintenanceTests(unittest.IsolatedAsyncioTestCase):
    async def test_refresh_at_start_periodically_and_shutdown_cancels_task(self):
        import threading
        calls=[];signal=threading.Event()
        def refresh(settings):
            calls.append(settings)
            if len(calls)>=2:signal.set()
        settings=Settings()
        async with maintained_fx(settings,interval=.01,refresh=refresh):
            self.assertTrue(await asyncio.to_thread(signal.wait,2))
        count=len(calls);await asyncio.sleep(.03)
        self.assertEqual(len(calls),count);self.assertGreaterEqual(count,2)
