import os
"""One controlled worker-child exit; supervisor must restart it automatically.

Uses the worker's existing owned stop-file mechanism, not process-name scans or
forced termination. This tests a graceful child exit, not a native crash/hang.
No recording, playback, model request or Windows restart is initiated.
"""
from datetime import datetime
import json
from pathlib import Path
import re
import sys
import time

ROOT=Path(__file__).resolve().parents[1]
sys.path[:0]=[str(ROOT/'.runtime-libs'),str(ROOT/'backend'),str(ROOT/'tools')]
from probe_host_reconnect import pause_wake


def request_owned_child_exit(service,root,expected):
    directory=Path(root)/'data/worker-service'
    required=('run_id','child_pid','supervisor_pid','restarts','state')
    if (not isinstance(expected,dict) or expected.get('state')!='RUNNING'
            or not isinstance(expected.get('run_id'),str)
            or not re.fullmatch('[0-9a-f]{32}',expected['run_id'])
            or any(type(expected.get(k)) is not int or expected[k]<=0 for k in ('child_pid','supervisor_pid'))
            or type(expected.get('restarts')) is not int or expected['restarts']<0):
        raise RuntimeError('CHILD_STATE_INVALID')
    def unchanged():
        current=json.loads((directory/'status.json').read_text())
        return all(current.get(k)==expected[k] for k in required)
    if not service.running(root) or not service.enabled(root) or not unchanged():
        raise RuntimeError('CHILD_STATE_CHANGED')
    if service.health(root,expected['run_id']) is not True or not unchanged():
        raise RuntimeError('CHILD_HEALTH_NOT_CONFIRMED')
    path=directory/(expected['run_id']+'.stop')
    if not path.resolve().is_relative_to(directory.resolve()) or path.exists():
        raise RuntimeError('CHILD_STOP_PATH_NOT_FRESH')
    service.save(path,{'stop':True})
    return path


def replacement_matches(old,new):
    return (new.get('state')=='RUNNING' and new.get('supervisor_pid')==old['supervisor_pid']
            and type(new.get('child_pid')) is int and new['child_pid']>0 and new['child_pid']!=old['child_pid']
            and isinstance(new.get('run_id'),str) and re.fullmatch('[0-9a-f]{32}',new['run_id']) is not None
            and new['run_id']!=old['run_id'] and type(new.get('restarts')) is int
            and new['restarts']>old['restarts'])


def main():
    import serial
    from app import worker_service as service
    path=ROOT/'data'/datetime.now().strftime('child-recovery-probe-%Y%m%d-%H%M%S.json')
    record={'time':datetime.now().astimezone().isoformat(),'state':'PRECHECK','events':[],
            'controlled_graceful_exit':True,'forced_crash_tested':False,'raw_audio_recorded':False,'model_called':False}
    port=serial.Serial(port=None,baudrate=460800,timeout=.2)
    port.dtr=False;port.rts=False;port.port=os.environ['XIAONUO_USB_PORT']
    started=time.monotonic();last=None;passed=False
    def save(**values):
        record.update(values);service.save(path,record)
    def pump(seconds,predicate=lambda m:False):
        nonlocal last
        end=time.monotonic()+seconds;poll=0
        while time.monotonic()<end:
            if time.monotonic()>=poll:port.write(b'LINK_STATUS\n');poll=time.monotonic()+1
            try:message=json.loads(port.readline())
            except ValueError:continue
            if message.get('event') not in {'status','network_status','network_disconnect','network_transport_error','error'}:continue
            safe={k:message[k] for k in ('event','firmware','state','ready','turn_active','pong_count',
                  'wifi_connected','computer_wake_enabled','quiet','code','http_status') if k in message}
            safe['observed_ms']=round((time.monotonic()-started)*1000)
            record['events'].append(safe);record['events']=record['events'][-600:]
            status=(safe.get('state'),safe.get('turn_active'))
            if message['event']!='network_status' or status!=last:print(json.dumps(safe),flush=True);last=status
            if predicate(message):return message
        return None
    try:
        save()
        if not service.running(ROOT) or not service.enabled(ROOT):raise RuntimeError('HOST_NOT_RUNNING')
        port.open();port.reset_input_buffer();port.write(b'INFO\n')
        info=pump(4,lambda m:m.get('event')=='status')
        if not info or not info.get('wifi_connected') or not info.get('computer_wake_enabled') or info.get('quiet'):
            raise RuntimeError('DEVICE_PRECHECK_FAILED')
        if not pump(60,lambda m:m.get('ready') and not m.get('turn_active')):raise RuntimeError('DEVICE_NOT_IDLE')
        with pause_wake(port):
            save(state='WAKE_PAUSED',wake_restore_required=True)
            port.write(b'INFO\n')
            if not pump(4,lambda m:m.get('event')=='status' and not m.get('computer_wake_enabled')):
                raise RuntimeError('WAKE_PAUSE_NOT_CONFIRMED')
            if not pump(180,lambda m:m.get('ready') and not m.get('turn_active')):raise RuntimeError('PAUSE_RECOVERY_TIMEOUT')
            old=json.loads((ROOT/'data/worker-service/status.json').read_text())
            request_owned_child_exit(service,ROOT,old)
            requested=time.monotonic()
            save(state='WAITING_FOR_AUTOMATIC_CHILD',exit_requested=True,old_child_pid=old['child_pid'],
                 supervisor_pid=old['supervisor_pid'],old_restart_count=old['restarts'])
            new=None;offline=False
            while time.monotonic()-requested<180:
                pump(1)
                offline=offline or any(e.get('event')=='network_status' and not e.get('ready') for e in record['events'])
                state=json.loads((ROOT/'data/worker-service/status.json').read_text())
                if replacement_matches(old,state) and service.health(ROOT,state['run_id']):new=state;break
            if new is None:raise RuntimeError('AUTOMATIC_CHILD_RECOVERY_TIMEOUT')
            save(state='WAITING_FOR_DEVICE',child_automatically_replaced=True,supervisor_unchanged=True,
                 new_child_pid=new['child_pid'],new_restart_count=new['restarts'],offline_observed=offline,
                 child_healthy_after_request_ms=round((time.monotonic()-requested)*1000))
            ready=pump(180,lambda m:m.get('ready') and not m.get('turn_active'))
            if not ready:raise RuntimeError('DEVICE_RECONNECT_TIMEOUT')
            save(device_ready_after_request_ms=round((time.monotonic()-requested)*1000))
            pongs=ready.get('pong_count',0)
            if not pump(15,lambda m:m.get('ready') and m.get('pong_count',0)>=pongs+3):raise RuntimeError('HEARTBEAT_NOT_CONFIRMED')
            save(three_new_heartbeats=True)
        port.write(b'INFO\n')
        restored=pump(4,lambda m:m.get('event')=='status' and m.get('computer_wake_enabled') is True)
        passed=bool(restored and record.get('offline_observed'))
        save(state='PASSED' if passed else 'RESTORE_NOT_CONFIRMED',wake_restored=bool(restored),wake_restore_required=not bool(restored))
    except Exception as error:
        code=str(error)
        save(state='FAILED_OR_INCOMPLETE',error_code=code if re.fullmatch('[A-Z_]{1,65}',code) else 'DIAGNOSTIC_ERROR')
    finally:
        if port.is_open:port.close()
        if record.get('exit_requested') and not service.running(ROOT):
            try:
                service.start(ROOT);save(manual_fallback_started=True,state='MANUAL_RECOVERY_USED_NOT_AUTOMATIC')
            except Exception as error:
                save(state='HOST_RESTORE_FAILED',restore_error_type=type(error).__name__)
            passed=False
        save(host_running=service.running(ROOT),host_enabled=service.enabled(ROOT))
        print(json.dumps({'ok':passed,'state':record['state'],'record':str(path)},ensure_ascii=False),flush=True)
    return 0 if passed else 1


if __name__=='__main__':raise SystemExit(main())
