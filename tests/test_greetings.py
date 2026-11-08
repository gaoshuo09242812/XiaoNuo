from concurrent.futures import ThreadPoolExecutor
from datetime import datetime,timedelta,timezone
from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'backend'))
from app.budgeting import UsageLedger
from app.storage import CompanionStore
from app.greetings import GreetingService


class GreetingTests(unittest.TestCase):
    def setUp(self):
        temporary=tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.store=CompanionStore(UsageLedger(Path(temporary.name)/'test.db'))
        self.at=datetime(2026,10,25,4,tzinfo=timezone.utc)
        self.service=GreetingService(self.store,clock=lambda:self.at)

    def test_default_disabled_and_quiet_modes(self):
        self.assertIsNone(self.service.visit()['text'])
        self.store.patch_controls({'greeting_enabled':True})
        self.store.set_quiet(True)
        self.assertIsNone(self.service.visit()['text'])
        self.store.set_quiet(False)
        self.at=self.at.replace(hour=15)
        self.assertIsNone(self.service.visit()['text'])
        self.at=self.at.replace(hour=4)
        result=self.service.visit()
        self.assertTrue(result['text'])
        self.assertFalse(result['automatic_audio'])

    def test_concurrent_visits_and_restart_only_offer_once(self):
        self.store.patch_controls({'greeting_enabled':True})
        with ThreadPoolExecutor(max_workers=6) as executor:
            results=list(executor.map(lambda _:self.service.visit(),range(12)))
        self.assertEqual(sum(bool(r['text']) for r in results),1)
        restarted=GreetingService(CompanionStore(self.store.ledger),clock=lambda:self.at)
        self.assertIsNone(restarted.visit()['text'])
        self.at+=timedelta(days=1)
        self.assertTrue(restarted.visit()['text'])

    def test_turning_off_does_not_reset_daily_limit(self):
        self.store.patch_controls({'greeting_enabled':True})
        self.assertTrue(self.service.visit()['text'])
        self.store.patch_controls({'greeting_enabled':False})
        self.assertIsNone(self.service.visit()['text'])
        self.store.patch_controls({'greeting_enabled':True})
        self.assertIsNone(self.service.visit()['text'])
