import asyncio
from dataclasses import replace
from datetime import datetime,timezone
from decimal import Decimal
from io import BytesIO
import json
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch
from uuid import uuid4

from app.config import Settings,ProviderSpec,Mode,load_settings
from app.budgeting import UsageLedger,BudgetManager
from app.storage import CompanionStore
from app.companion import CompanionService
from app.llm.health import ProviderHealthMonitor
from app.llm.providers import DeepSeekProvider,HTTPTransport
from app.llm.router import ModelRouter
from app.errors import XiaoNuoError
from app.network_chat import require_deepseek_chat
from app.network_speech import LocalNetworkSpeech
from app.network_host import create_network_host
from test_voice import FakeASR,FakeTTS,PCM
from tools.configure_deepseek import configure,check_balance


class DeepSeekNetworkTests(unittest.TestCase):
    def setUp(self):
        # These fixtures deliberately use the checked 2026-10-28 price/FX data.
        clock=patch('app.budgeting.now',return_value=datetime(2026,10,28,tzinfo=timezone.utc))
        clock.start();self.addCleanup(clock.stop)
        temporary=tempfile.TemporaryDirectory();self.addCleanup(temporary.cleanup)
        self.root=Path(temporary.name)
        self.spec=ProviderSpec('deepseek','deepseek-flash','sk-offline-fixture-not-real',False,True)
        self.settings=Settings(root=self.root,admin_token='owner-'+'x'*40,remote_enabled=True,
            allow_paid=True,primary_paid='deepseek',deployment_region='CN',
            monthly_server_cost=Decimal('0'),cny_to_usd=Decimal('.15'),fx_checked_on='2026-10-28',
            providers=(self.spec,),network_chat_provider='deepseek')
        self.store=CompanionStore(UsageLedger(self.settings.database))
        self.requests=[]
        parent=self
        class Transport:
            def post(self,url,headers,body,timeout):
                parent.requests.append((url,headers,body))
                return {'choices':[{'message':{'content':'你好，我是小糯。'}}],
                        'usage':{'prompt_tokens':30,'completion_tokens':10,'prompt_cache_hit_tokens':0}}
        provider=DeepSeekProvider(self.spec,Transport())
        self.router=ModelRouter(self.settings,{'deepseek':provider},
            BudgetManager(self.store.ledger,self.settings),ProviderHealthMonitor(self.store.ledger))

    def test_local_asr_deepseek_text_and_local_tts_use_budgeted_router(self):
        async def run():
            companion=CompanionService(self.router,self.store)
            adapter=LocalNetworkSpeech(self.root,FakeASR(),FakeTTS(),companion,
                self.router.budget,self.store,chat_provider='deepseek')
            result=await adapter(PCM,str(uuid4()),'fixture',threading.Event())
            self.assertEqual(result['transcript'],'你好')
            self.assertIn('你好',result['reply_text'])
            pieces=[piece async for piece in result['_audio']]
            self.assertTrue(pieces)
        asyncio.run(run())
        self.assertEqual(len(self.requests),1)
        url,headers,body=self.requests[0]
        self.assertEqual(url,'https://api.deepseek.com/chat/completions')
        self.assertEqual(body['thinking'],{'type':'disabled'})
        self.assertEqual(body['model'],'deepseek-flash')
        self.assertNotIn('audio',body)
        self.assertNotIn('tools',body)
        self.assertTrue(all(isinstance(m['content'],str) for m in body['messages']))
        snapshot=self.router.budget.snapshot()
        self.assertGreater(Decimal(snapshot['api_cost_estimate_usd']),0)
        self.assertEqual(snapshot['outstanding_reservations_usd'],'0')

    def test_missing_key_or_disabled_paid_never_reaches_transport(self):
        for changes in ({'allow_paid':False},{'remote_enabled':False},{'mode':Mode.FREE_ONLY}):
            self.router.settings=replace(self.settings,**changes)
            with self.assertRaises(XiaoNuoError):require_deepseek_chat(self.router,preflight=True)
        self.router.settings=self.settings
        self.router.providers['deepseek'].spec=replace(self.spec,key='')
        with self.assertRaisesRegex(XiaoNuoError,'MISSING_CREDENTIAL'):require_deepseek_chat(self.router)
        self.assertFalse(self.requests)

    def test_other_remote_provider_is_rejected(self):
        self.router.providers['other-cloud']=self.router.providers['deepseek']
        with self.assertRaisesRegex(XiaoNuoError,'PROVIDER_NOT_ALLOWED'):require_deepseek_chat(self.router)
        self.assertFalse(self.requests)

    def test_shared_host_filters_unselected_remote_providers(self):
        extra=ProviderSpec('openai','unused','fake',False,True)
        owner,gateway=create_network_host(replace(self.settings,providers=(self.spec,extra)),enable_audio=True)
        self.assertIs(owner.state.device_links,gateway.state.device_links)
        self.assertEqual(set(owner.state.companion.router.providers),{'deepseek'})
        self.assertEqual(owner.state.device_links.audio_handler.chat_provider,'deepseek')

    def test_expired_fx_and_unknown_server_cost_block_configuration(self):
        for changes in ({'fx_checked_on':'2026-10-01'},{'monthly_server_cost':None}):
            settings=replace(self.settings,**changes)
            self.router.settings=settings
            self.router.budget=BudgetManager(self.store.ledger,settings)
            with self.assertRaises(XiaoNuoError):require_deepseek_chat(self.router,preflight=True)
        self.assertFalse(self.requests)

    def test_setup_preserves_existing_settings_and_never_returns_key(self):
        env=self.root/'.env.local'
        env.write_text('# owner configuration\nXIAONUO_ADMIN_TOKEN=existing-owner-token\nXIAONUO_MONTHLY_BUDGET_USD=3\nDEEPSEEK_API_KEY=old-key\nXIAONUO_CNY_TO_USD=0.15\nXIAONUO_FX_CHECKED_ON=2026-10-28\n',encoding='utf-8')
        result=configure(self.root,self.spec.key,verify=lambda key:{'authenticated':True,'balance_available':True})
        loaded=load_settings(env)
        self.assertEqual(loaded.admin_token,'existing-owner-token')
        self.assertEqual(loaded.monthly_budget,Decimal('3'))
        self.assertEqual(loaded.network_chat_provider,'deepseek')
        self.assertTrue(loaded.remote_enabled and loaded.allow_paid)
        self.assertEqual(loaded.monthly_server_cost,Decimal('0'))
        self.assertNotIn(self.spec.key,json.dumps(result))
        self.assertIn('# owner configuration',env.read_text(encoding='utf-8'))

    def test_deepseek_failure_does_not_discard_successful_local_fallback(self):
        from test_core import FakeProvider
        from app.errors import ProviderError
        spec=ProviderSpec('local_qwen','offline-model','fake-local',True,True)
        self.router.settings=replace(self.settings,local_llm_enabled=True)
        self.router.providers['local_qwen']=FakeProvider(spec)
        self.router.health.report(spec,verify_free=True)
        class Failed:
            def post(self,*args):raise ProviderError('PROVIDER_NETWORK_ERROR')
        self.router.providers['deepseek'].transport=Failed()
        async def run():
            adapter=LocalNetworkSpeech(self.root,FakeASR(),FakeTTS(),
                CompanionService(self.router,self.store),self.router.budget,self.store,chat_provider='deepseek')
            result=await adapter(PCM,str(uuid4()),'fixture',threading.Event())
            self.assertIn('离线测试',result['reply_text'])
            self.assertTrue([piece async for piece in result['_audio']])
        asyncio.run(run())
        self.assertEqual(self.router.budget.snapshot()['uncertain_requests'],1)

    def test_failed_or_racing_setup_preserves_original_file(self):
        env=self.root/'.env.local';env.write_text('XIAONUO_ADMIN_TOKEN=original\n',encoding='utf-8')
        original=env.read_bytes()
        def failed(key):raise XiaoNuoError('DEEPSEEK_HTTP_401')
        with self.assertRaises(XiaoNuoError):configure(self.root,self.spec.key,verify=failed)
        self.assertEqual(env.read_bytes(),original)
        def concurrent(key):
            env.write_text('XIAONUO_ADMIN_TOKEN=user-update\n',encoding='utf-8')
            return {'authenticated':True}
        with self.assertRaisesRegex(XiaoNuoError,'ENV_FILE_CHANGED'):configure(self.root,self.spec.key,verify=concurrent)
        self.assertIn('user-update',env.read_text(encoding='utf-8'))

    def test_balance_check_uses_only_official_api_and_sanitized_result(self):
        class Opener:
            def open(inner,request,timeout):
                self.assertEqual(request.full_url,'https://api.deepseek.com/user/balance')
                self.assertEqual(request.headers['Authorization'],'Bearer '+self.spec.key)
                return BytesIO(b'{"is_available":true,"balance_infos":[]}')
        self.assertEqual(check_balance(self.spec.key,opener=Opener()),{'authenticated':True,'balance_available':True})
        for invalid in ('', 'account-token', 'sk-'+('x'*20)+'\nheader'):
            with self.assertRaises(XiaoNuoError):check_balance(invalid,opener=Opener())
