import os
"""Bounded passive link observation; never changes settings or initiates audio."""
from datetime import datetime
import json
from pathlib import Path
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT/'.runtime-libs'), str(ROOT/'backend'), str(ROOT/'tools')]
from probe_device_restart import configuration_fingerprint


class LinkObservation:
    def __init__(self):
        self.last_status = self.last_network = self.last_pong = None
        self.uptime = self.pongs = self.fingerprint = None
        self.new_pongs = 0
        self.heap_min = self.heap_max = None
        self.status_samples = self.network_samples = 0

    def accept(self, message, now):
        if not isinstance(message, dict):
            return
        event = message.get('event')
        if event == 'status':
            fingerprint = configuration_fingerprint(message)
            uptime = message.get('uptime_ms')
            heap = message.get('free_heap')
            if type(uptime) is not int or type(heap) is not int or uptime < 0 or heap <= 0:
                raise RuntimeError('INVALID_DEVICE_COUNTER')
            if self.fingerprint is not None and self.fingerprint != fingerprint:
                raise RuntimeError('DEVICE_CONFIGURATION_CHANGED')
            if self.uptime is not None and uptime < self.uptime:
                raise RuntimeError('DEVICE_RESTART_OR_COUNTER_WRAP')
            if message.get('wifi_connected') is not True or message.get('computer_wake_enabled') is not True:
                raise RuntimeError('DEVICE_WIFI_OR_WAKE_LOST')
            self.fingerprint, self.uptime, self.last_status = fingerprint, uptime, now
            self.heap_min = heap if self.heap_min is None else min(heap, self.heap_min)
            self.heap_max = heap if self.heap_max is None else max(heap, self.heap_max)
            self.status_samples += 1
        elif event == 'network_status':
            pongs = message.get('pong_count')
            if message.get('ready') is not True or type(pongs) is not int or pongs < 0:
                raise RuntimeError('DEVICE_LINK_NOT_READY')
            if self.pongs is not None:
                if pongs < self.pongs:
                    raise RuntimeError('DEVICE_HEARTBEAT_COUNTER_RESET')
                if pongs > self.pongs:
                    self.new_pongs += pongs-self.pongs
                    self.last_pong = now
            else:
                self.last_pong = now
            self.pongs, self.last_network = pongs, now
            self.network_samples += 1
        elif event in {'network_disconnect', 'network_transport_error', 'error'}:
            raise RuntimeError('DEVICE_REPORTED_DIAGNOSTIC_EVENT')

    def check_fresh(self, now):
        if self.last_status is None or self.last_network is None:
            raise RuntimeError('DEVICE_STATUS_MISSING')
        if now-self.last_status > 8 or now-self.last_network > 8:
            raise RuntimeError('DEVICE_STATUS_STALE')
        if self.last_pong is None or now-self.last_pong > 35:
            raise RuntimeError('DEVICE_HEARTBEAT_STALLED')

    def finish(self, now, duration):
        self.check_fresh(now)
        if now < duration or self.new_pongs < 3:
            raise RuntimeError('OBSERVATION_INCOMPLETE')

    def summary(self):
        return dict(status_samples=self.status_samples, network_samples=self.network_samples,
                    new_pongs=self.new_pongs, heap_min_bytes=self.heap_min, heap_max_bytes=self.heap_max)


def main():
    import argparse
    import serial
    from app import worker_service as service
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--seconds', type=int, default=300)
    args = parser.parse_args()
    if not 60 <= args.seconds <= 3600:
        parser.error('seconds must be between 60 and 3600')
    path = ROOT/'data'/datetime.now().strftime('link-soak-probe-%Y%m%d-%H%M%S.json')
    record = dict(time=datetime.now().astimezone().isoformat(), state='PRECHECK',
                  requested_seconds=args.seconds, events=[], settings_written=False,
                  audio_request_initiated=False, power_loss_tested=False)
    observation = LinkObservation()
    port = serial.Serial(port=None, baudrate=460800, timeout=.2)
    port.dtr=False; port.rts=False; port.port=os.environ['XIAONUO_USB_PORT']
    start=time.monotonic(); passed=False
    def save(**values):
        record.update(values); service.save(path, record)
    try:
        worker_path=ROOT/'data/worker-service/status.json'
        worker=json.loads(worker_path.read_text())
        if not service.running(ROOT) or not service.enabled(ROOT) or not service.health(ROOT, worker.get('run_id')):
            raise RuntimeError('HOST_PRECHECK_FAILED')
        port.open(); port.reset_input_buffer()
        poll=host_poll=journal=0; command=0
        save(state='OBSERVING')
        while True:
            now=time.monotonic()-start
            if now >= poll:
                # One command per poll avoids back-to-back startup input overlap.
                port.write((b'INFO\n', b'LINK_STATUS\n')[command % 2])
                command+=1; poll=now+1
            raw=port.readline()
            try: message=json.loads(raw)
            except ValueError: message=None
            now=time.monotonic()-start
            if isinstance(message,dict) and message.get('event') in {'status','network_status','network_disconnect','network_transport_error','error'}:
                safe={k:message[k] for k in ('event','wifi_connected','computer_wake_enabled','ready',
                     'turn_active','uptime_ms','pong_count','free_heap','http_status') if k in message}
                safe['observed_ms']=round(now*1000)
                record['events'].append(safe); record['events']=record['events'][-800:]
                observation.accept(message,now)
            if now > 6:
                observation.check_fresh(now)
            if now >= host_poll:
                current=json.loads(worker_path.read_text())
                if any(current.get(k)!=worker.get(k) for k in ('run_id','child_pid','supervisor_pid','restarts')):
                    raise RuntimeError('HOST_PROCESS_CHANGED')
                if not service.running(ROOT) or not service.enabled(ROOT) or not service.health(ROOT, worker['run_id']):
                    raise RuntimeError('HOST_HEALTH_FAILED')
                host_poll=now+15
                record['host_health_checks']=record.get('host_health_checks',0)+1
            if now >= journal:
                save(observed_seconds=round(now,3), **observation.summary())
                print(json.dumps({'elapsed_seconds':round(now), **observation.summary()}),flush=True)
                journal=now+30
            if now >= args.seconds:
                observation.finish(now,args.seconds); passed=True; break
        save(state='PASSED',observed_seconds=round(time.monotonic()-start,3), **observation.summary())
    except Exception as error:
        code=str(error)
        save(state='FAILED_OR_INCOMPLETE',error_code=code if code.isupper() and len(code)<80 else type(error).__name__,
             observed_seconds=round(time.monotonic()-start,3), **observation.summary())
    finally:
        if port.is_open: port.close()
        print(json.dumps({'ok':passed,'state':record['state'],'record':str(path)},ensure_ascii=False),flush=True)
    return 0 if passed else 1


if __name__=='__main__': raise SystemExit(main())
