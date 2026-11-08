"""One bounded synthetic reply over the real paired WSS connection.

No microphone capture, speech recognition, TTS or paid model request is used.
Keeps saved controls/identity unchanged. Above 20 seconds requires firmware
0.6.61 or later; shorter probes require firmware 0.6.55 or later.
"""
import argparse
from datetime import datetime
import json
from pathlib import Path
import sys
import time

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'.runtime-libs'))


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--seconds',type=int,required=True,choices=range(1,46))
    parser.add_argument('--port',required=True)
    args=parser.parse_args()
    import serial
    port=serial.Serial(port=None,baudrate=460800,timeout=.15)
    port.dtr=False;port.rts=False;port.port=args.port
    events=[];start=time.monotonic();next_info=start;next_send=start
    requested=False;sent=False;attempts=0;samples=0;segments=0;complete=False;playing=False
    requested_at=None;last_status=None
    startup_errors=0
    # Two verified TLS candidates can each need 30 seconds. Keep connection
    # recovery separate from the reply transfer's own bounded test window.
    ready_timeout=180;reply_timeout=540
    last_play_end=None;play_gaps=[]
    expected=args.seconds*16000
    allowed={'event','state','connected','ready','turn_active','pong_count','seconds',
             'raw_audio_recorded','code','reason','samples','active','last_event','last_error',
             'written_samples','next_sequence','received_bytes','queued','window','target',
             'reason_code','tls_code','elapsed_ms','peer_ipv4','http_status',
             'first_frame_wait_ms','frame_span_ms','ack_queue_wait_ms','ack_send_ms','window_received',
             'bytes','prepare_ms','write_ms','seal_ms'}
    kinds={'network_status','reply_probe_requested','play_ready','play_end','play_error',
           'play_stopped','reply_ack_pending','reply_ack_sent','error','network_disconnect',
           'network_peer_attempt','network_transport_error','audio_status','wake_capture_fault','reply_cache_timing'}
    try:
        port.open();port.reset_input_buffer()
        # Finish any partial command left at a previous serial connection.
        port.write(b'\n');port.flush()
        while time.monotonic()-start<ready_timeout+reply_timeout:
            now=time.monotonic()
            if not playing and now>=next_info:
                port.write(b'LINK_STATUS\n');next_info=now+1
            try:message=json.loads(port.readline())
            except (ValueError,UnicodeError):continue
            kind=message.get('event')
            if kind in kinds:
                safe={key:value for key,value in message.items() if key in allowed}
                safe['observed_ms']=round((time.monotonic()-start)*1000)
                events.append(safe)
                status=(message.get('state'),message.get('ready'),message.get('turn_active'))
                if kind!='network_status' or status!=last_status:
                    print(json.dumps(safe,ensure_ascii=False),flush=True)
                if kind=='network_status':last_status=status
            if kind=='network_status' and message.get('ready') and not message.get('turn_active'):
                if requested and samples==expected:
                    complete=True;break
                if not requested and not sent and now>=next_send:
                    port.write(f'NET_REPLY_TEST {args.seconds}\n'.encode('ascii'))
                    sent=True;attempts+=1
            if kind=='reply_probe_requested':
                requested=True;requested_at=now
            if kind=='play_ready':
                playing=True
                if last_play_end is not None:play_gaps.append(round((time.monotonic()-last_play_end)*1000))
            if kind in {'play_end','play_error','play_stopped'}:playing=False
            if kind=='play_end' and requested:
                last_play_end=time.monotonic()
                samples+=message.get('samples',0);segments+=1
                print(json.dumps({'completed_segments':segments,'played_samples':samples,
                                  'expected_samples':expected}),flush=True)
            if kind=='error':
                # No test has been sent yet: a leftover serial fragment is
                # not a reply failure. Require fresh READY before sending.
                if not requested and not sent and message.get('code')=='UNKNOWN_OR_DISABLED_COMMAND' and startup_errors<2:
                    startup_errors+=1;continue
                # LINK_STATUS is deliberately refused while I2S is active.
                # This read-only monitor error does not stop audio output.
                if message.get('code')=='DEVICE_BUSY':continue
                if not requested and message.get('code')=='REPLY_PROBE_BLOCKED' and attempts<3:
                    sent=False;next_send=now+2
                else:break
            if requested and kind in {'play_error','play_stopped','network_disconnect','wake_capture_fault'}:
                # The callback precedes publication of the actual stop reason.
                # Fetch it before closing the observer, without restarting I/O.
                port.write(b'LINK_STATUS\n')
                until=time.monotonic()+2
                while time.monotonic()<until:
                    try:tail=json.loads(port.readline())
                    except (ValueError,UnicodeError):continue
                    if tail.get('event') in {'network_status','network_transport_error'}:
                        safe={key:value for key,value in tail.items() if key in allowed}
                        events.append(safe);print(json.dumps(safe),flush=True)
                        if tail.get('event')=='network_status':break
                break
            if not requested and now-start>ready_timeout:break
            if requested_at is not None and now-requested_at>reply_timeout:break
    finally:
        if port.is_open:
            if requested and not complete:
                # A failed reply can otherwise leave a busy turn behind.
                try:port.write(b'NET_CANCEL\n');port.flush()
                except OSError:pass
            port.close()
        record={'time':datetime.now().astimezone().isoformat(),'seconds':args.seconds,
                'device_completed':complete,'played_samples':samples,'expected_samples':expected,
                'completed_segments':segments,'play_gaps_ms':play_gaps,'raw_audio_recorded':False,'model_called':False,
                'audible_verified':False,'events':events}
        path=ROOT/'data'/f'wss-reply-probe-{args.seconds}s-{datetime.now():%Y%m%d-%H%M%S}.json'
        path.write_text(json.dumps(record,ensure_ascii=False,indent=2),encoding='utf-8')
        print(json.dumps({'device_completed':complete,'played_samples':samples,
                          'expected_samples':expected,'record':str(path)},ensure_ascii=False),flush=True)
    return 0 if complete else 1


if __name__=='__main__':
    raise SystemExit(main())
