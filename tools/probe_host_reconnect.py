"""One controlled computer-host outage; no microphone or model request.

Uses the existing pairing/WiFi settings. Journals each step, restores the host
and local wake switch in finally blocks, and never prints credentials.
"""
from contextlib import contextmanager
from datetime import datetime
import json
from pathlib import Path
import sys
import time

ROOT=Path(__file__).resolve().parents[1]
sys.path[:0]=[str(ROOT/'.runtime-libs'),str(ROOT/'backend')]


@contextmanager
def host_outage(service,root):
    try:
        result=service.stop(root)
        if result.get('stopped') is not True:
            raise RuntimeError('HOST_STOP_NOT_CONFIRMED')
        yield
    finally:
        # stop() may have disabled autostart even if stopping raised or timed out.
        service.start(root)


@contextmanager
def pause_wake(port):
    try:
        port.write(b'NET_WAKE OFF\n')
        yield
    finally:
        port.write(b'NET_WAKE ON\n')


def main():
    import argparse
    import serial
    from app import worker_service as service
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--port',required=True)
    args=parser.parse_args()
    path=ROOT/'data'/f'host-reconnect-probe-{datetime.now():%Y%m%d-%H%M%S-%f}.json'
    record={'time':datetime.now().astimezone().isoformat(),'state':'PRECHECK',
            'raw_audio_recorded':False,'model_called':False,'events':[]}
    def save(**values):
        record.update(values);service.save(path,record)
    def event(value):
        record['events'].append(value)
        if len(record['events'])>500:record['events']=record['events'][-500:]
    port=serial.Serial(port=None,baudrate=460800,timeout=.15)
    port.dtr=False;port.rts=False;port.port=args.port
    allowed={'event','firmware','wifi_connected','computer_wake_enabled','quiet',
             'state','connected','ready','turn_active','pong_count','reason_code',
             'http_status','elapsed_ms','tls_code','code','last_error'}
    last=None
    def pump(seconds,predicate=lambda message:False):
        nonlocal last
        end=time.monotonic()+seconds;poll=0
        while time.monotonic()<end:
            if time.monotonic()>=poll:
                port.write(b'LINK_STATUS\n');poll=time.monotonic()+1
            try:message=json.loads(port.readline())
            except (ValueError,UnicodeError):continue
            if message.get('event') in {'status','network_status','network_disconnect','network_transport_error','network_peer_attempt','error'}:
                safe={key:value for key,value in message.items() if key in allowed}
                safe['observed_ms']=round((time.monotonic()-started)*1000);event(safe)
                state=(message.get('state'),message.get('ready'),message.get('turn_active'))
                if message['event']!='network_status' or state!=last:
                    print(json.dumps(safe),flush=True)
                if message['event']=='network_status':last=state
            if predicate(message):return message
        return None
    started=time.monotonic();success=False
    try:
        save()
        if not service.running(ROOT) or not service.enabled(ROOT):
            raise RuntimeError('HOST_NOT_RUNNING_PRECHECK')
        port.open();port.reset_input_buffer();port.write(b'INFO\n')
        info=pump(4,lambda m:m.get('event')=='status')
        if not info or not info.get('computer_wake_enabled') or not info.get('wifi_connected') or info.get('quiet'):
            raise RuntimeError('DEVICE_NOT_READY_PRECHECK')
        idle=pump(60,lambda m:m.get('event')=='network_status' and m.get('ready') and not m.get('turn_active'))
        if not idle:raise RuntimeError('DEVICE_NOT_IDLE_PRECHECK')
        save(state='PAUSING_WAKE',wake_restore_required=True)
        with pause_wake(port):
            port.write(b'INFO\n')
            paused=pump(4,lambda m:m.get('event')=='status' and not m.get('computer_wake_enabled'))
            if not paused:raise RuntimeError('WAKE_PAUSE_NOT_CONFIRMED')
            # Disabling wake can cancel an in-flight background probe. Rebuild
            # an idle live link before attributing a disconnect to host outage.
            stable=pump(180,lambda m:m.get('event')=='network_status' and m.get('ready') and not m.get('turn_active'))
            if not stable:raise RuntimeError('WAKE_PAUSE_LINK_RECOVERY_TIMEOUT')
            save(pre_outage_ready=True)

            save(state='HOST_STOPPING',host_restore_required=True)
            with host_outage(service,ROOT):
                save(state='HOST_OFFLINE')
                offline_started=time.monotonic()
                offline=pump(10,lambda m:m.get('event')=='network_status' and not m.get('ready'))
                save(offline_observed=offline is not None)
                pump(max(0,15-(time.monotonic()-offline_started)))
            restart=time.monotonic()
            save(state='WAITING_FOR_AUTOMATIC_RECONNECT',host_restore_required=False)
            ready=pump(180,lambda m:m.get('event')=='network_status' and m.get('ready') and not m.get('turn_active'))
            if not ready:raise RuntimeError('AUTOMATIC_RECONNECT_TIMEOUT')
            save(reconnected=True,reconnect_after_start_ms=round((time.monotonic()-restart)*1000))
            pongs=ready.get('pong_count',0)
            alive=pump(15,lambda m:m.get('event')=='network_status' and m.get('ready') and m.get('pong_count',0)>=pongs+3)
            if not alive:raise RuntimeError('RECONNECTED_HEARTBEAT_NOT_CONFIRMED')
            save(heartbeat_confirmed=True)
        port.write(b'INFO\n')
        restored=pump(4,lambda m:m.get('event')=='status' and m.get('computer_wake_enabled') is True)
        success=bool(restored and record.get('offline_observed') and service.running(ROOT) and service.enabled(ROOT))
        save(wake_restored=bool(restored),wake_restore_required=not bool(restored),state='PASSED' if success else 'RESTORE_NOT_CONFIRMED')
    except Exception as error:
        save(state='FAILED_OR_INCOMPLETE',error_type=type(error).__name__,
             error_code=str(error) if str(error) in {'HOST_STOP_NOT_CONFIRMED','HOST_NOT_RUNNING_PRECHECK',
                'DEVICE_NOT_READY_PRECHECK','DEVICE_NOT_IDLE_PRECHECK','WAKE_PAUSE_NOT_CONFIRMED',
                'AUTOMATIC_RECONNECT_TIMEOUT','RECONNECTED_HEARTBEAT_NOT_CONFIRMED',
                'WAKE_PAUSE_LINK_RECOVERY_TIMEOUT'} else 'DIAGNOSTIC_ERROR')
    finally:
        if port.is_open:port.close()
        save(host_running=service.running(ROOT),host_enabled=service.enabled(ROOT))
        print(json.dumps({'ok':success,'state':record['state'],'record':str(path)},ensure_ascii=False),flush=True)
    return 0 if success else 1


if __name__=='__main__':
    raise SystemExit(main())
