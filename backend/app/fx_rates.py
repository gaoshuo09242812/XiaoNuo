"""Public ECB reference refresh; no credentials, owner data or billing calls."""
import asyncio
from contextlib import asynccontextmanager
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation, ROUND_CEILING
import json
import os
from pathlib import Path
import tempfile
from urllib.request import Request, ProxyHandler, build_opener
from xml.etree import ElementTree

from .errors import XiaoNuoError
from .llm.providers import NoRedirect

SOURCE='https://www.ecb.europa.eu/stats/eurofxref/eurofxref-daily.xml'
NS='{http://www.ecb.int/vocabulary/2002-08-01/eurofxref}'
LIMIT=32768

def today():
    return datetime.now(timezone(timedelta(hours=8))).date()

def checked_rate(value, published, at):
    rate=Decimal(str(value));day=date.fromisoformat(published)
    if day.isoformat()!=published or not rate.is_finite() or not Decimal('0')<rate<=Decimal('1') or not 0<=(at-day).days<=7:
        raise ValueError('Invalid or expired FX reference')
    return rate,day

def reference(usd, cny, published, at):
    usd,cny=Decimal(str(usd)),Decimal(str(cny))
    if not usd.is_finite() or not cny.is_finite() or not Decimal('.1')<usd<10 or not 1<cny<100:
        raise ValueError('Invalid ECB currency rates')
    rate=(usd/cny).quantize(Decimal('.000000000001'),rounding=ROUND_CEILING)
    checked_rate(rate,published,at)
    return {'source':SOURCE,'date':published,'usd_per_eur':str(usd),'cny_per_eur':str(cny)}

def parse_reference(raw,at):
    if not isinstance(raw,bytes) or len(raw)>LIMIT or b'<!DOCTYPE' in raw.upper() or b'<!ENTITY' in raw.upper():
        raise ValueError('Invalid ECB response')
    root=ElementTree.fromstring(raw)
    days=root.findall('./'+NS+'Cube/'+NS+'Cube')
    if len(days)!=1:raise ValueError('Expected one reference date')
    rates={}
    for item in days[0].findall(NS+'Cube'):
        name=item.get('currency')
        if name in rates:raise ValueError('Duplicate currency')
        rates[name]=item.get('rate')
    return reference(rates['USD'],rates['CNY'],days[0].attrib['time'],at)

def fetch_reference(at):
    request=Request(SOURCE,headers={'User-Agent':'XiaoNuo-FX/1'})
    with build_opener(ProxyHandler({}),NoRedirect()).open(request,timeout=12) as response:
        raw=response.read(LIMIT+1)
    return parse_reference(raw,at)

def cache_path(settings):return Path(settings.root)/'data/fx-reference.json'

def cached_reference(settings,at):
    try:
        path=cache_path(settings)
        if path.is_symlink():return None
        with path.open('rb') as stream:raw=stream.read(LIMIT+1)
        if len(raw)>LIMIT:return None
        data=json.loads(raw)
        if data['source']!=SOURCE:return None
        return reference(data['usd_per_eur'],data['cny_per_eur'],data['date'],at)
    except (OSError,ValueError,KeyError,TypeError,InvalidOperation):return None

def effective_fx(settings,at):
    candidates=[]
    try:
        rate,day=checked_rate(settings.cny_to_usd,settings.fx_checked_on,at)
        candidates.append((day,rate,'manual'))
    except (ValueError,TypeError,InvalidOperation):pass
    cached=cached_reference(settings,at)
    if cached:
        rate,day=checked_rate(Decimal(cached['usd_per_eur'])/Decimal(cached['cny_per_eur']),cached['date'],at)
        rate=rate.quantize(Decimal('.000000000001'),rounding=ROUND_CEILING)
        candidates.append((day,rate,'ecb'))
    if not candidates:raise XiaoNuoError('FX_RATE_NOT_CONFIRMED')
    # Keep the explicit owner configuration on an equal publication date.
    day,rate,source=max(candidates,key=lambda entry:entry[0])
    return rate,day,source

def save_reference(settings,data):
    target=cache_path(settings);target.parent.mkdir(parents=True,exist_ok=True)
    if target.is_symlink():raise ValueError('Unsafe FX cache')
    temporary=None
    try:
        with tempfile.NamedTemporaryFile(mode='w',encoding='utf-8',dir=target.parent,prefix='.fx-',delete=False) as output:
            temporary=output.name;json.dump(data,output);output.flush();os.fsync(output.fileno())
        os.replace(temporary,target);temporary=None
    finally:
        if temporary:
            try:os.unlink(temporary)
            except OSError:pass

def refresh_if_due(settings,*,at=None,fetch=fetch_reference,force=False):
    at=at or today()
    if not(settings.remote_enabled and settings.allow_paid and settings.primary_paid=='deepseek'):
        return {'state':'NOT_REQUIRED'}
    try:
        _,day,_=effective_fx(settings,at)
        if not force and (at-day).days<3:return {'state':'CURRENT','reference_date':day.isoformat()}
    except XiaoNuoError:pass
    try:
        candidate=fetch(at)
        if candidate['source']!=SOURCE:raise ValueError('Unexpected source')
        candidate=reference(candidate['usd_per_eur'],candidate['cny_per_eur'],candidate['date'],at)
        old=cached_reference(settings,at)
        if old and old['date']>candidate['date']:return {'state':'NEWER_CACHE_RETAINED','reference_date':old['date']}
        save_reference(settings,candidate)
        return {'state':'UPDATED','reference_date':candidate['date']}
    except Exception:
        # Retain valid data without re-dating it; expired data still fails closed.
        return {'state':'REFRESH_FAILED'}

@asynccontextmanager
async def maintained_fx(settings,*,interval=21600,refresh=None):
    refresh=refresh or refresh_if_due
    await asyncio.to_thread(refresh,settings)
    async def run():
        while True:
            await asyncio.sleep(interval)
            await asyncio.to_thread(refresh,settings)
    task=asyncio.create_task(run())
    try:yield
    finally:
        task.cancel();await asyncio.gather(task,return_exceptions=True)
