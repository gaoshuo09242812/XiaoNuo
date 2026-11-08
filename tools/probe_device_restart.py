"""Two USB-controlled resets while power remains connected; no Flash writes.

This is not charger/power-loss acceptance. It checks saved identity/configuration,
automatic relay connection, fresh heartbeats and enabled wake after each reset.
Normal wake remains enabled; the probe initiates no recording/model/playback.
"""
from datetime import datetime
import hashlib
import json
from pathlib import Path
import subprocess
import sys
import time

ROOT=Path(__file__).resolve().parents[1]
sys.path[:0]=[str(ROOT/'.runtime-libs'),str(ROOT/'backend')]
EXPECTED_FIRMWARE='0.6.64-buffered-reply'


def configuration_fingerprint(info):
    keys=('device_id','binding_id','saved_ssid','oled_driver')
    if any(not isinstance(info.get(k),str) or not info[k] for k in keys):
        raise RuntimeError('DEVICE_IDENTITY_INCOMPLETE')
    if info.get('firmware')!=EXPECTED_FIRMWARE or type(info.get('speaker_gain_divisor')) is not int:
        raise RuntimeError('DEVICE_VERSION_OR_GAIN_INVALID')
    values=[info[k] for k in keys]+[info['firmware'],info['speaker_gain_divisor'],info.get('quiet')]
    return hashlib.sha256(json.dumps(values,ensure_ascii=False,separators=(',',':')).encode()).hexdigest()


def validate_reboot(before,after):
    if configuration_fingerprint(before)!=configuration_fingerprint(after):
        raise RuntimeError('DEVICE_SAVED_CONFIGURATION_CHANGED')
    if (type(before.get('uptime_ms')) is not int or type(after.get('uptime_ms')) is not int or
            not 0<=after['uptime_ms']<before['uptime_ms']):
        raise RuntimeError('DEVICE_RESTART_NOT_CONFIRMED')


def main():
    import argparse
    import serial
    from app import worker_service as service
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--esptool',type=Path,required=True)
    parser.add_argument('--port',required=True)
    args=parser.parse_args()
    record={'time':datetime.now().astimezone().isoformat(),'state':'PRECHECK','cycles':[],
            'events':[],'usb_reset_while_powered':True,'power_loss_tested':False,
            'flash_written':False,'settings_written':False,'audio_request_initiated':False}
    path=ROOT/'data'/datetime.now().strftime('device-restart-probe-%Y%m%d-%H%M%S.json')
    port=serial.Serial(port=None,baudrate=460800,timeout=.2)
    port.dtr=False;port.rts=False;port.port=args.port
    start=time.monotonic();last=None;success=False
    def save(**values):record.update(values);service.save(path,record)
    def open_port():
        if not port.is_open:port.open();port.reset_input_buffer()
    def pump(seconds,predicate):
        nonlocal last
        end=time.monotonic()+seconds;poll=0
        while time.monotonic()<end:
            if time.monotonic()>=poll:port.write(b'INFO\nLINK_STATUS\n');poll=time.monotonic()+2
            try:message=json.loads(port.readline())
            except ValueError:continue
            if message.get('event') not in {'status','network_status','network_disconnect','network_peer_attempt','network_transport_error','error'}:continue
            safe={k:message[k] for k in ('event','firmware','wifi_connected','computer_wake_enabled','state','ready',
                  'turn_active','uptime_ms','reset_reason','pong_count','code','tls_code','elapsed_ms','http_status') if k in message}
            safe['observed_ms']=round((time.monotonic()-start)*1000)
            record['events'].append(safe);record['events']=record['events'][-900:]
            state=(safe.get('state'),safe.get('turn_active'))
            if message['event']!='status' and (message['event']!='network_status' or state!=last):
                print(json.dumps(safe),flush=True);last=state
            if predicate(message):return message
        return None
    try:
        save()
        if not service.running(ROOT) or not service.enabled(ROOT):raise RuntimeError('HOST_NOT_RUNNING')
        open_port()
        initial=pump(5,lambda m:m.get('event')=='status')
        if not initial or not initial.get('wifi_connected') or not initial.get('computer_wake_enabled') or initial.get('quiet'):
            raise RuntimeError('DEVICE_NOT_READY_PRECHECK')
        fingerprint=configuration_fingerprint(initial);save(configuration_fingerprint_sha256=fingerprint)
        for cycle in (1,2):
            if not pump(60,lambda m:m.get('event')=='network_status' and m.get('ready') and not m.get('turn_active')):
                raise RuntimeError('DEVICE_NOT_IDLE')
            before=pump(5,lambda m:m.get('event')=='status' and m.get('uptime_ms',0)>5000)
            if not before or configuration_fingerprint(before)!=fingerprint:raise RuntimeError('DEVICE_PRE_RESET_CHANGED')
            save(state='RESETTING_DEVICE',active_cycle=cycle)
            port.close();reset_started=time.monotonic()
            # Only the ROM's run command. No erase, write-flash or credential command.
            result=subprocess.run([str(args.esptool),'--chip','esp32','--port',args.port,'--baud','460800',
                '--before','default-reset','--after','hard-reset','run'],capture_output=True,timeout=60)
            if result.returncode:raise RuntimeError('DEVICE_RESET_TOOL_FAILED')
            open_port()
            boot=pump(20,lambda m:m.get('event')=='status')
            if boot is None:raise RuntimeError('DEVICE_BOOT_INFO_TIMEOUT')
            validate_reboot(before,boot)
            entry={'cycle':cycle,'uptime_before_ms':before['uptime_ms'],'first_boot_uptime_ms':boot['uptime_ms'],
                   'reset_reason':boot.get('reset_reason'),'configuration_preserved':True}
            record['cycles'].append(entry);save(state='WAITING_FOR_AUTOMATIC_CONNECTION')
            ready=pump(180,lambda m:m.get('event')=='network_status' and m.get('ready') and not m.get('turn_active'))
            if ready is None:raise RuntimeError('DEVICE_AUTOMATIC_RECONNECT_TIMEOUT')
            entry['ready_after_reset_command_ms']=round((time.monotonic()-reset_started)*1000)
            pongs=ready.get('pong_count',0)
            if not pump(15,lambda m:m.get('event')=='network_status' and m.get('ready') and m.get('pong_count',0)>=pongs+3):
                raise RuntimeError('DEVICE_HEARTBEAT_NOT_CONFIRMED')
            awake=pump(5,lambda m:m.get('event')=='status' and m.get('computer_wake_enabled'))
            if not awake or configuration_fingerprint(awake)!=fingerprint:raise RuntimeError('DEVICE_WAKE_OR_CONFIGURATION_NOT_RESTORED')
            entry.update(three_new_heartbeats=True,wake_enabled=True,passed=True)
            save(state='CYCLE_PASSED');print(json.dumps(entry),flush=True)
        success=True;save(state='PASSED')
    except Exception as error:
        code=str(error);save(state='FAILED_OR_INCOMPLETE',error_code=code if code.isupper() and len(code)<75 else type(error).__name__)
    finally:
        if port.is_open:port.close()
        save(host_running=service.running(ROOT),host_enabled=service.enabled(ROOT))
        print(json.dumps({'ok':success,'state':record['state'],'record':str(path)},ensure_ascii=False),flush=True)
    return 0 if success else 1


if __name__=='__main__':raise SystemExit(main())
