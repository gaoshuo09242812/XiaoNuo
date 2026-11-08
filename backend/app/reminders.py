"""Durable console reminders. This module never plays audio or calls a model."""
from datetime import datetime,timedelta,timezone
import re
import hashlib
import json
import threading
from uuid import uuid4

from .behavior import QuietMode
from .budgeting import SHANGHAI,now
from .errors import XiaoNuoError
from .storage import OWNER
from .reminder_audio import ReminderAudioOutbox

ZONES={'Asia/Shanghai':SHANGHAI,'UTC':timezone.utc}


def instant(value):
    if isinstance(value,str):
        try:value=datetime.fromisoformat(value.replace('Z','+00:00'))
        except ValueError:raise XiaoNuoError('INVALID_REMINDER_TIME',422) from None
    if not isinstance(value,datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise XiaoNuoError('REMINDER_TIMEZONE_REQUIRED',422)
    return value.astimezone(timezone.utc)


def iso(value):return instant(value).isoformat(timespec='seconds')


def request_fingerprint(text,due_at,zone,repeat,quiet_exception):
    payload=[text.strip(),iso(due_at),zone,repeat,quiet_exception]
    return hashlib.sha256(json.dumps(payload,ensure_ascii=False,separators=(',',':')).encode()).hexdigest()


def next_occurrence(due,rule,zone,at):
    local=instant(due).astimezone(ZONES[zone])
    current=instant(at).astimezone(ZONES[zone])
    days=1 if rule=='daily' else 7
    count=max(1,(current.date()-local.date()).days//days)
    candidate=local+timedelta(days=days*count)
    if candidate<=current:candidate+=timedelta(days=days)
    return iso(candidate)


class ReminderService:
    def __init__(self,store,clock=now):
        self.store,self.ledger,self.clock=store,store.ledger,clock
        self._stop=threading.Event();self._thread=None
        self.last_tick=None;self.last_error=None
        with self.ledger.connect() as db:
            fields={row['name'] for row in db.execute('PRAGMA table_info(reminders)')}
            for name,definition in {'version':'INTEGER NOT NULL DEFAULT 1','request_id':'TEXT','request_fingerprint':'TEXT'}.items():
                if name not in fields:db.execute('ALTER TABLE reminders ADD COLUMN '+name+' '+definition)
            db.execute('CREATE UNIQUE INDEX IF NOT EXISTS reminder_request ON reminders(owner_id,request_id) WHERE request_id IS NOT NULL')
        self.audio=ReminderAudioOutbox(store,clock)

    def validate(self,text,due_at,zone,repeat,quiet_exception):
        if not isinstance(text,str) or not 1<=len(text.strip())<=500:raise XiaoNuoError('INVALID_REMINDER_TEXT',422)
        if zone not in ZONES:raise XiaoNuoError('REMINDER_TIMEZONE_NOT_SUPPORTED',422)
        if repeat not in {'none','daily','weekly'} or type(quiet_exception) is not bool:raise XiaoNuoError('INVALID_REMINDER_RULE',422)
        due=instant(due_at)
        current=instant(self.clock())
        if due<current-timedelta(seconds=5):raise XiaoNuoError('REMINDER_IN_PAST',422)
        if due>current+timedelta(days=3650):raise XiaoNuoError('REMINDER_TOO_FAR_AHEAD',422)
        return text.strip(),iso(due)

    def create(self,text,due_at,zone='Asia/Shanghai',repeat='none',quiet_exception=False,request_id=None,owner=OWNER):
        request_id=request_id or str(uuid4())
        fingerprint=request_fingerprint(text,due_at,zone,repeat,quiet_exception)
        with self.ledger.connect() as db:
            previous=db.execute('SELECT * FROM reminders WHERE owner_id=? AND request_id=?',(owner,request_id)).fetchone()
            if previous:
                prior=previous['request_fingerprint'] or request_fingerprint(previous['text'],previous['due_at'],previous['timezone'],previous['repeat_rule'],bool(previous['quiet_exception']))
                if prior!=fingerprint:
                    raise XiaoNuoError('REMINDER_REQUEST_CONFLICT',409)
                return dict(previous)
        text,due=self.validate(text,due_at,zone,repeat,quiet_exception)
        identifier=str(uuid4());created=iso(self.clock())
        with self.ledger.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            try:
                existing=db.execute('SELECT * FROM reminders WHERE owner_id=? AND request_id=?',(owner,request_id)).fetchone()
                if existing:
                    if existing['request_fingerprint']!=fingerprint:raise XiaoNuoError('REMINDER_REQUEST_CONFLICT',409)
                    db.execute('COMMIT');return dict(existing)
                if db.execute('SELECT COUNT(*) FROM reminders WHERE owner_id=? AND enabled=1',(owner,)).fetchone()[0]>=200:
                    raise XiaoNuoError('REMINDER_LIMIT_REACHED',409)
                db.execute('INSERT OR IGNORE INTO reminders(id,owner_id,text,due_at,timezone,repeat_rule,quiet_exception,enabled,created_at,updated_at,version,request_id,request_fingerprint) VALUES (?,?,?,?,?,?,?,1,?,?,1,?,?)',
                           (identifier,owner,text,due,zone,repeat,int(quiet_exception),created,created,request_id,fingerprint))
                result=db.execute('SELECT * FROM reminders WHERE owner_id=? AND request_id=?',(owner,request_id)).fetchone()
                if result is None:raise XiaoNuoError('REMINDER_NOT_SAVED')
                if result['request_fingerprint']!=fingerprint:
                    raise XiaoNuoError('REMINDER_REQUEST_CONFLICT',409)
                db.execute('COMMIT')
                return dict(result)
            except BaseException:db.execute('ROLLBACK');raise

    def list(self,owner=OWNER):
        with self.ledger.connect() as db:
            return [dict(row) for row in db.execute('SELECT * FROM reminders WHERE owner_id=? ORDER BY enabled DESC,due_at LIMIT 300',(owner,))]

    def update(self,identifier,*,text,due_at,zone,repeat,quiet_exception,expected_version,owner=OWNER):
        text,due=self.validate(text,due_at,zone,repeat,quiet_exception)
        with self.ledger.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            try:
                changed=db.execute('UPDATE reminders SET text=?,due_at=?,timezone=?,repeat_rule=?,quiet_exception=?,enabled=1,version=version+1,updated_at=? WHERE id=? AND owner_id=? AND version=?',
                                   (text,due,zone,repeat,int(quiet_exception),iso(self.clock()),identifier,owner,expected_version)).rowcount
                if not changed:raise XiaoNuoError('REMINDER_NOT_FOUND_OR_STALE',409)
                db.execute("UPDATE execution_records SET state='cancelled' WHERE owner_id=? AND resource_id=? AND kind='reminder' AND state='available'",(owner,identifier))
                result=db.execute('SELECT * FROM reminders WHERE id=?',(identifier,)).fetchone()
                db.execute('COMMIT');return dict(result)
            except BaseException:db.execute('ROLLBACK');raise

    def cancel(self,identifier,owner=OWNER):
        with self.ledger.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            try:
                if not db.execute('SELECT 1 FROM reminders WHERE id=? AND owner_id=?',(identifier,owner)).fetchone():
                    raise XiaoNuoError('REMINDER_NOT_FOUND',404)
                db.execute('UPDATE reminders SET enabled=0,version=version+1,updated_at=? WHERE id=? AND owner_id=? AND enabled=1',(iso(self.clock()),identifier,owner))
                db.execute("UPDATE execution_records SET state='cancelled' WHERE owner_id=? AND resource_id=? AND kind='reminder' AND state='available'",(owner,identifier))
                db.execute('COMMIT')
            except BaseException:db.execute('ROLLBACK');raise

    def tick(self,at=None):
        current=instant(at or self.clock());moment=iso(current)
        emitted=0
        with self.ledger.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            try:
                for item in db.execute('SELECT * FROM reminders WHERE owner_id=? AND enabled=1 AND due_at<=? ORDER BY due_at LIMIT 100',(OWNER,moment)).fetchall():
                    if not QuietMode(self.store).allows_proactive('reminder',quiet_exception=bool(item['quiet_exception']),at=current):continue
                    event_id=str(uuid4())
                    inserted=db.execute("INSERT OR IGNORE INTO execution_records(id,owner_id,kind,resource_id,occurrence_at,state,created_at) VALUES (?,?,'reminder',?,?,'available',?)",
                                        (event_id,item['owner_id'],item['id'],item['due_at'],moment)).rowcount
                    if inserted:self.audio.enqueue(db,event_id,item['owner_id'],instant(item['due_at']),current)
                    emitted+=inserted
                    if item['repeat_rule']=='none':
                        db.execute('UPDATE reminders SET enabled=0,version=version+1,updated_at=? WHERE id=?',(moment,item['id']))
                    else:
                        following=next_occurrence(item['due_at'],item['repeat_rule'],item['timezone'],current)
                        db.execute('UPDATE reminders SET due_at=?,version=version+1,updated_at=? WHERE id=?',(following,moment,item['id']))
                db.execute('COMMIT')
            except BaseException:db.execute('ROLLBACK');raise
        self.last_tick=moment
        return emitted

    def notifications(self,owner=OWNER):
        with self.ledger.connect() as db:
            return [dict(row) for row in db.execute("SELECT e.id,e.resource_id,e.occurrence_at,e.state,e.created_at,r.text,r.timezone FROM execution_records e JOIN reminders r ON r.id=e.resource_id AND r.owner_id=e.owner_id WHERE e.owner_id=? AND e.kind='reminder' AND e.state='available' ORDER BY e.created_at LIMIT 100",(owner,))]

    def acknowledge(self,identifier,owner=OWNER):
        with self.ledger.connect() as db:
            result=db.execute("SELECT state FROM execution_records WHERE id=? AND owner_id=? AND kind='reminder'",(identifier,owner)).fetchone()
            if not result:raise XiaoNuoError('REMINDER_EVENT_NOT_FOUND',404)
            db.execute("UPDATE execution_records SET state='acknowledged' WHERE id=? AND state='available'",(identifier,))

    def start(self):
        if self._thread and self._thread.is_alive():return
        self._stop.clear();self._thread=threading.Thread(target=self._run,name='xiaonuo-reminders',daemon=True);self._thread.start()

    def _run(self):
        while not self._stop.is_set():
            try:self.tick();self.last_error=None
            except Exception:self.last_error='REMINDER_SCHEDULER_FAILED'
            self._stop.wait(2)

    def stop(self):
        self._stop.set()
        if self._thread:self._thread.join(timeout=4)


def chinese_number(value):
    if value.isdecimal():return int(value)
    digits={'零':0,'〇':0,'一':1,'二':2,'两':2,'三':3,'四':4,'五':5,'六':6,'七':7,'八':8,'九':9}
    if '十' in value:
        if value.count('十')!=1:raise XiaoNuoError('REMINDER_TIME_UNSUPPORTED',422)
        left,right=value.split('十',1)
        if (left and left not in digits) or (right and right not in digits):raise XiaoNuoError('REMINDER_TIME_UNSUPPORTED',422)
        return (digits.get(left,1) if left else 1)*10+(digits.get(right,0) if right else 0)
    if value in digits:return digits[value]
    raise XiaoNuoError('REMINDER_TIME_UNSUPPORTED',422)


def parse_reminder(text,zone,at):
    """Restricted, explicit Chinese grammar; ambiguous times are never guessed."""
    if '提醒我' not in text:return None
    if zone not in ZONES:raise XiaoNuoError('REMINDER_TIMEZONE_NOT_SUPPORTED',422)
    pattern=r'^(?P<repeat>每天|每周[一二三四五六日天])?(?:(?P<day>今天|明天|后天)|(?P<date>\d{4}-\d{2}-\d{2}))?(?P<period>早上|上午|中午|下午|晚上|凌晨)?(?P<hour>\d{1,2}|[零〇一二两三四五六七八九十]{1,3})(?:[:：](?P<minute>\d{2})|点(?P<cnminute>半|一刻|三刻|[零〇一二两三四五六七八九十]{1,3}分?)?)\s*提醒我(?P<title>.+)$'
    match=re.fullmatch(pattern,text.strip().removeprefix('请').strip().rstrip('。！!'))
    if not match:raise XiaoNuoError('REMINDER_TIME_UNSUPPORTED',422)
    fields=match.groupdict();hour=chinese_number(fields['hour']);minute=0
    if fields['minute'] is not None:minute=int(fields['minute'])
    if fields['cnminute']:
        part=fields['cnminute'];minute={'半':30,'一刻':15,'三刻':45}.get(part)
        if minute is None:minute=chinese_number(part.removesuffix('分'))
    if hour>23 or minute>59:raise XiaoNuoError('INVALID_REMINDER_TIME',422)
    period=fields['period']
    if period in {'下午','晚上'}:
        if period=='晚上' and hour in {0,12}:raise XiaoNuoError('REMINDER_TIME_AMBIGUOUS',422)
        if 1<=hour<=11:hour+=12
    elif period=='中午':
        if hour in {1,2}:hour+=12
        elif hour not in {11,12,13,14}:raise XiaoNuoError('REMINDER_TIME_AMBIGUOUS',422)
    elif period in {'早上','上午','凌晨'}:
        if hour>=12 or (period=='凌晨' and hour>6):raise XiaoNuoError('REMINDER_TIME_AMBIGUOUS',422)
    elif not fields['minute'] and 1<=hour<=12:
        raise XiaoNuoError('REMINDER_TIME_AMBIGUOUS',422)
    local=instant(at).astimezone(ZONES[zone]);day=local.date()
    if fields['day']:day+=timedelta(days={'今天':0,'明天':1,'后天':2}[fields['day']])
    elif fields['date']:
        try:day=datetime.strptime(fields['date'],'%Y-%m-%d').date()
        except ValueError:raise XiaoNuoError('INVALID_REMINDER_TIME',422) from None
    elif not fields['repeat']:raise XiaoNuoError('REMINDER_TIME_AMBIGUOUS',422)
    repeat='none'
    if fields['repeat']:
        if fields['day'] or fields['date']:raise XiaoNuoError('REMINDER_TIME_AMBIGUOUS',422)
        repeat='daily' if fields['repeat']=='每天' else 'weekly'
        if repeat=='weekly':
            weekday='一二三四五六日'.index(fields['repeat'][-1].replace('天','日'))
            day+=timedelta(days=(weekday-local.weekday())%7)
    due=datetime(day.year,day.month,day.day,hour,minute,tzinfo=ZONES[zone])
    if repeat!='none' and due<=local:due+=timedelta(days=1 if repeat=='daily' else 7)
    return {'text':fields['title'].strip(),'due_at':due,'zone':zone,'repeat':repeat}
