"""Local masked API-key setup; never accepts a secret in argv or prints it."""
import argparse
from dataclasses import replace
import getpass
import json
from pathlib import Path
import re
import sys
from urllib.error import HTTPError, URLError
from urllib.request import Request, build_opener
from uuid import uuid4

ROOT=Path(__file__).resolve().parents[1]
sys.path[:0]=[str(ROOT),str(ROOT/'backend'),str(ROOT/'.runtime-libs')]
from app.config import load_settings
from app.errors import XiaoNuoError
from app.llm.providers import NoRedirect
from app.fx_rates import effective_fx, refresh_if_due


def valid_api_key(key):
    return isinstance(key,str) and re.fullmatch(r'sk-[A-Za-z0-9._-]{16,240}',key) is not None


def check_balance(key, *, opener=None):
    if not valid_api_key(key):raise XiaoNuoError('DEEPSEEK_API_KEY_INVALID_FORMAT',422)
    request=Request('https://api.deepseek.com/user/balance',headers={'Authorization':'Bearer '+key})
    try:
        with (opener or build_opener(NoRedirect())).open(request,timeout=15) as response:
            raw=response.read(65537)
        if len(raw)>65536:raise XiaoNuoError('DEEPSEEK_BALANCE_RESPONSE_INVALID')
        result=json.loads(raw)
        if not isinstance(result,dict) or type(result.get('is_available')) is not bool:
            raise XiaoNuoError('DEEPSEEK_BALANCE_RESPONSE_INVALID')
        if not result['is_available']:raise XiaoNuoError('DEEPSEEK_BALANCE_UNAVAILABLE',409)
        return {'authenticated':True,'balance_available':True}
    except HTTPError as error:
        status=error.code;error.close()
        raise XiaoNuoError('DEEPSEEK_HTTP_'+str(status)) from None
    except (URLError,OSError,TimeoutError):
        raise XiaoNuoError('DEEPSEEK_CONNECTION_FAILED') from None
    except (ValueError,UnicodeError):
        raise XiaoNuoError('DEEPSEEK_BALANCE_RESPONSE_INVALID') from None


def configure(root,key,*,verify=check_balance,fx_refresh=refresh_if_due):
    target=Path(root)/'.env.local'
    if target.is_symlink() or (target.exists() and (not target.is_file() or target.stat().st_nlink>1)):
        raise XiaoNuoError('UNSAFE_ENV_FILE',409)
    original=target.read_bytes() if target.exists() else b''
    check=verify(key)
    if (target.read_bytes() if target.exists() else b'')!=original:
        raise XiaoNuoError('ENV_FILE_CHANGED_DURING_SETUP',409)
    settings=load_settings(target)
    from app.budgeting import now,SHANGHAI
    at=now().astimezone(SHANGHAI).date()
    fx_refresh(replace(settings,remote_enabled=True,allow_paid=True,primary_paid='deepseek'),at=at)
    fx,fx_date,_=effective_fx(settings,at)
    updates={'DEEPSEEK_API_KEY':key,'DEEPSEEK_MODEL':'deepseek-flash',
        'XIAONUO_PRIMARY_PAID':'deepseek','XIAONUO_NETWORK_CHAT_PROVIDER':'deepseek',
        'XIAONUO_REMOTE_ENABLED':'true','XIAONUO_ALLOW_PAID':'true',
        'XIAONUO_DEEPSEEK_REGION_APPROVED':'true','XIAONUO_DEPLOYMENT_REGION':'CN',
        'XIAONUO_CNY_TO_USD':str(fx),'XIAONUO_FX_CHECKED_ON':fx_date.isoformat()}
    if settings.monthly_server_cost is None:
        # The selected deployment is the owner's existing PC, without server rent.
        updates['XIAONUO_MONTHLY_SERVER_COST_USD']='0'
    if settings.monthly_budget_cny is not None and settings.monthly_server_cost_cny is None:
        updates['XIAONUO_MONTHLY_SERVER_COST_CNY']='0'
    remaining=dict(updates);lines=[]
    for line in original.decode('utf-8-sig').splitlines():
        match=re.match(r'^\s*(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*=',line)
        name=match[1] if match else None
        if name in updates:
            if name in remaining:lines.append(name+'='+remaining.pop(name))
        else:lines.append(line)
    lines.extend(name+'='+value for name,value in remaining.items())
    if (target.is_symlink() or (target.exists() and (not target.is_file() or target.stat().st_nlink>1))
            or (target.read_bytes() if target.exists() else b'')!=original):
        raise XiaoNuoError('ENV_FILE_CHANGED_DURING_SETUP',409)
    # Open in place so the existing Windows ACL is preserved.
    with target.open('w',encoding='utf-8',newline='\n') as output:
        output.write('\n'.join(lines)+'\n')
    return {**check,'configured':True,'model':'deepseek-flash',
            'budget_currency':'CNY' if settings.monthly_budget_cny is not None else 'USD',
            'monthly_budget':str(settings.monthly_budget_cny if settings.monthly_budget_cny is not None else settings.monthly_budget),
            'key_displayed':False}


def verify_chat(root):
    from app.main import build_router
    from app.network_chat import require_deepseek_chat
    settings=load_settings(Path(root)/'.env.local')
    settings=replace(settings,providers=tuple(p for p in settings.providers if p.name=='deepseek'),
                     max_output_tokens=32)
    router=build_router(settings);require_deepseek_chat(router,preflight=True)
    # Public diagnostic text only, without user memories or conversation history.
    result=router.reply(str(uuid4()),'你是小糯，请简短回答中文。',
                        [{'role':'user','content':'请只回复：你好。'}])
    if result.provider!='deepseek':raise XiaoNuoError('DEEPSEEK_VERIFICATION_WRONG_PROVIDER')
    return {'actual_deepseek_chat_passed':True,'provider':result.provider,'model':result.model,
            'public_test_reply':result.completion.text,'cost_status':result.cost_status}


def main():
    parser=argparse.ArgumentParser(description='小糯 DeepSeek 本机安全配置')
    parser.add_argument('--configure',action='store_true',help='在本机窗口中输入密钥，不回显')
    parser.add_argument('--check',action='store_true',help='校验已配置密钥和账户可用状态')
    parser.add_argument('--verify-chat',action='store_true',help='在现有预算内发起一次最多32输出token的公开文本检查')
    args=parser.parse_args()
    try:
        if args.configure:
            if not sys.stdin.isatty():raise XiaoNuoError('LOCAL_INTERACTIVE_INPUT_REQUIRED',409)
            key=getpass.getpass('DeepSeek API Key（粘贴后不会显示，按回车保存）: ').strip()
            result=configure(ROOT,key);key=''
        else:
            spec=next(p for p in load_settings().providers if p.name=='deepseek')
            if not spec.key:raise XiaoNuoError('DEEPSEEK_API_KEY_MISSING',409)
            result=check_balance(spec.key)
        if args.verify_chat:result.update(verify_chat(ROOT))
        print(json.dumps(result,ensure_ascii=False))
    except XiaoNuoError as error:
        print(json.dumps({'ready':False,'error':error.code,'key_displayed':False},ensure_ascii=False))
        raise SystemExit(2) from None
    except Exception:
        # Do not let a provider or malformed key appear in a traceback.
        print(json.dumps({'ready':False,'error':'DEEPSEEK_SETUP_FAILED','key_displayed':False}))
        raise SystemExit(2) from None


if __name__=='__main__':main()
