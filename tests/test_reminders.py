from concurrent.futures import ThreadPoolExecutor
from datetime import datetime,timedelta,timezone
from pathlib import Path
import asyncio
import sys
import tempfile
import unittest
from uuid import uuid4

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'backend'))
from app.budgeting import UsageLedger,BudgetManager
from app.config import Settings
from app.companion import CompanionService
from app.errors import XiaoNuoError
from app.llm.health import ProviderHealthMonitor
from app.llm.router import ModelRouter
from app.main import create_app
from app.reminders import ReminderService,parse_reminder,next_occurrence,instant
from app.storage import CompanionStore
from test_core import request_asgi

FIXED=datetime(2026,10,25,2,0,tzinfo=timezone.utc)


class ReminderTests(unittest.TestCase):
    def test_voice_confirmation_depends_on_current_device_capability(self):
        router=ModelRouter(self.settings,{},BudgetManager(self.ledger,self.settings),ProviderHealthMonitor(self.ledger))
        companion=CompanionService(router,self.store,reminders=self.service)
        companion.reminder_voice_available=lambda:True
        first=companion.chat('明天晚上八点提醒我喝水',str(uuid4()),None)
        self.assertIn('支持语音提醒',first['reply_text'])
        companion.reminder_voice_available=lambda:False
        second=companion.chat('明天晚上八点提醒我休息',str(uuid4()),None)
        self.assertIn('当前提醒显示在电脑控制台',second['reply_text'])
        self.assertNotIn('支持语音提醒',second['reply_text'])

    def setUp(self):
        temp=tempfile.TemporaryDirectory();self.addCleanup(temp.cleanup)
        self.root=Path(temp.name)
        self.settings=Settings(root=self.root,admin_token='offline-'+40*'x')
        self.ledger=UsageLedger(self.settings.database)
        self.store=CompanionStore(self.ledger)
        self.service=ReminderService(self.store,clock=lambda:FIXED)

    def create(self,**kwargs):
        return self.service.create(kwargs.pop('text','背单词'),kwargs.pop('due_at',FIXED+timedelta(minutes=1)),**kwargs)

    def test_natural_tomorrow_evening_time_is_explicit_and_correct(self):
        parsed=parse_reminder('明天晚上八点提醒我背单词','Asia/Shanghai',FIXED)
        self.assertEqual(instant(parsed['due_at']),datetime(2026,10,26,12,tzinfo=timezone.utc))
        self.assertEqual(parsed['text'],'背单词')

    def test_ambiguous_or_malformed_time_never_creates_reminder(self):
        for text in ['明天八点提醒我背单词','明天晚上十二点提醒我背单词','明天二十十点提醒我背单词']:
            with self.assertRaises(XiaoNuoError):parse_reminder(text,'Asia/Shanghai',FIXED)
        self.assertEqual(self.service.list(),[])

    def test_daily_and_weekly_roll_to_future_occurrence(self):
        daily=parse_reminder('每天早上八点提醒我喝水','Asia/Shanghai',FIXED)
        weekly=parse_reminder('每周一早上八点提醒我复习','Asia/Shanghai',FIXED)
        self.assertEqual(daily['repeat'],'daily')
        self.assertEqual(weekly['repeat'],'weekly')
        self.assertGreater(daily['due_at'],FIXED)
        self.assertEqual(weekly['due_at'].weekday(),0)

    def test_idempotent_creation_conflict_and_concurrent_retry(self):
        request=str(uuid4())
        with ThreadPoolExecutor(max_workers=4) as pool:
            results=list(pool.map(lambda _:self.create(request_id=request),range(8)))
        self.assertEqual(len({row['id'] for row in results}),1)
        self.assertEqual(len(self.service.list()),1)
        with self.assertRaisesRegex(XiaoNuoError,'REMINDER_REQUEST_CONFLICT'):
            self.create(text='different',request_id=request)

    def test_concurrent_ticks_publish_one_event_only(self):
        reminder=self.create()
        with ThreadPoolExecutor(max_workers=5) as pool:
            results=list(pool.map(lambda _:self.service.tick(FIXED+timedelta(minutes=2)),range(10)))
        self.assertEqual(sum(results),1)
        events=self.service.notifications()
        self.assertEqual(len(events),1)
        self.assertEqual(events[0]['resource_id'],reminder['id'])
        self.assertEqual(self.service.list()[0]['enabled'],0)

    def test_creation_retry_after_recurrence_advances_does_not_create_or_reset_job(self):
        request=str(uuid4())
        original=self.create(repeat='daily',request_id=request)
        self.service.tick(FIXED+timedelta(minutes=2))
        following=self.service.list()[0]['due_at']
        retry=self.create(repeat='daily',request_id=request)
        self.assertEqual(retry['id'],original['id'])
        self.assertEqual(retry['due_at'],following)
        self.assertEqual(len(self.service.list()),1)

    def test_restart_recovers_unseen_event_without_duplicate_execution(self):
        self.create()
        first=ReminderService(CompanionStore(self.ledger),clock=lambda:FIXED+timedelta(minutes=2))
        self.assertEqual(first.tick(),1)
        second=ReminderService(CompanionStore(self.ledger),clock=lambda:FIXED+timedelta(minutes=3))
        self.assertEqual(second.tick(),0)
        self.assertEqual(len(second.notifications()),1)

    def test_recurrence_catches_up_once_and_skips_missed_backlog(self):
        self.create(repeat='daily')
        future=FIXED+timedelta(days=30,minutes=2)
        self.assertEqual(self.service.tick(future),1)
        self.assertEqual(len(self.service.notifications()),1)
        item=self.service.list()[0]
        self.assertTrue(item['enabled'])
        self.assertGreater(instant(item['due_at']),future)
        self.assertEqual(self.service.tick(future),0)

    def test_quiet_defers_ordinary_reminder_and_never_wakes_owner(self):
        self.create();self.store.set_quiet(True)
        self.assertEqual(self.service.tick(FIXED+timedelta(minutes=2)),0)
        self.assertEqual(self.service.notifications(),[])
        self.assertTrue(self.store.controls()['quiet'])
        self.store.set_quiet(False)
        self.assertEqual(self.service.tick(FIXED+timedelta(minutes=3)),1)

    def test_explicit_reminder_exception_keeps_quiet_flag_unchanged(self):
        self.create(quiet_exception=True);self.store.set_quiet(True)
        self.assertEqual(self.service.tick(FIXED+timedelta(minutes=2)),1)
        self.assertTrue(self.store.controls()['quiet'])

    def test_quiet_hours_hold_notification_until_allowed_time(self):
        night=datetime(2026,10,25,23,tzinfo=timezone(timedelta(hours=8)))
        self.create(due_at=night)
        self.assertEqual(self.service.tick(night+timedelta(minutes=1)),0)
        self.assertEqual(self.service.tick(night+timedelta(hours=10)),1)

    def test_cancel_and_acknowledge_are_durable_and_idempotent(self):
        reminder=self.create();self.service.tick(FIXED+timedelta(minutes=2))
        event=self.service.notifications()[0]
        self.service.acknowledge(event['id']);self.service.acknowledge(event['id'])
        self.assertEqual(self.service.notifications(),[])
        other=self.create(text='喝水');self.service.tick(FIXED+timedelta(minutes=2))
        self.assertEqual(len(self.service.notifications()),1)
        self.service.cancel(other['id']);self.service.cancel(other['id'])
        self.assertEqual(self.service.notifications(),[])

    def test_optimistic_edit_cancels_stale_notification(self):
        reminder=self.create()
        updated=self.service.update(reminder['id'],text='新内容',due_at=FIXED+timedelta(hours=1),zone='UTC',repeat='weekly',quiet_exception=False,expected_version=1)
        self.assertEqual(updated['version'],2)
        with self.assertRaisesRegex(XiaoNuoError,'REMINDER_NOT_FOUND_OR_STALE'):
            self.service.update(reminder['id'],text='旧内容',due_at=FIXED+timedelta(hours=1),zone='UTC',repeat='none',quiet_exception=False,expected_version=1)

    def test_invalid_naive_and_past_times_are_rejected(self):
        for value in [datetime(2026,10,26),FIXED-timedelta(days=1)]:
            with self.assertRaises(XiaoNuoError):self.create(due_at=value)

    def test_personal_data_clear_removes_reminders_and_execution_records(self):
        self.create();self.service.tick(FIXED+timedelta(minutes=2))
        with self.ledger.connect() as db:self.assertEqual(db.execute('SELECT COUNT(*) FROM reminder_audio_outbox').fetchone()[0],1)
        self.store.clear_personal_data()
        self.assertEqual(self.service.list(),[])
        self.assertEqual(self.service.notifications(),[])
        with self.ledger.connect() as db:self.assertEqual(db.execute('SELECT COUNT(*) FROM reminder_audio_outbox').fetchone()[0],0)

    def test_natural_command_saves_before_acknowledgment_without_model_call(self):
        router=ModelRouter(self.settings,{},BudgetManager(self.ledger,self.settings),ProviderHealthMonitor(self.ledger))
        companion=CompanionService(router,self.store,reminders=self.service)
        result=companion.chat('明天晚上八点提醒我背单词',str(uuid4()),None)
        self.assertEqual(result['diagnostics']['action'],'reminder_created')
        self.assertEqual(self.service.list()[0]['id'],result['reminder_id'])
        ambiguous=companion.chat('明天八点提醒我喝水',str(uuid4()),None)
        self.assertEqual(ambiguous['diagnostics']['action'],'reminder_needs_clarification')
        self.assertEqual(len(self.service.list()),1)

    def test_api_auth_and_timezone_validation(self):
        app=create_app(self.settings)
        async def scenario():
            for path in ['/api/v1/reminders','/api/v1/reminder-events']:
                status,_=await request_asgi(app,path);self.assertEqual(status,401)
            status,_=await request_asgi(app,'/api/v1/reminders',token=self.settings.admin_token,method='POST',body={'text':'test','due_at':'2026-10-26T20:00:00'})
            self.assertEqual(status,422)
            status,_=await request_asgi(app,'/api/v1/reminders',token=self.settings.admin_token,method='POST',body={'text':'test','due_at':'2026-10-26T20:00:00+08:00','timezone':'unsupported'})
            self.assertEqual(status,422)
        asyncio.run(scenario())


if __name__=='__main__':unittest.main()
