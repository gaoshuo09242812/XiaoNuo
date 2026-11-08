import os
"""Bounded passive UART/numerical-audit observer. No audio or text is saved."""
from datetime import datetime
import argparse
import json
from pathlib import Path
import sys
import time

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'.runtime-libs'))


def poll_commands(readiness_announced):
    # NET_DIAG performs synchronous DNS in the Arduino main task. Once wake
    # readiness is known, plain status reads suffice throughout the test.
    return b'INFO\nLINK_STATUS\n' if readiness_announced else b'INFO\nLINK_STATUS\nNET_DIAG\n'


def is_target_reply(item,require_model_reply=False):
    if not isinstance(item,dict) or item.get('synthetic') or item.get('source_samples',0)<=0:return False
    if not require_model_reply:return True
    flags=item.get('voice_probe',{})
    size=flags.get('model_characters') if isinstance(flags,dict) else None
    return type(size) is int and size>0


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--require-model-reply',action='store_true')
    args=parser.parse_args()
    import serial
    audit=ROOT/'data/diagnostics/reply-lifecycle.jsonl'
    initial=audit.stat().st_size if audit.exists() else 0
    port=serial.Serial(port=None,baudrate=460800,timeout=.2)
    port.dtr=False;port.rts=False;port.port=os.environ['XIAONUO_USB_PORT']
    events=[];result=None;play_end=None;announced=False;playing=False;status=None
    started=time.monotonic();next_poll=0;last_status=None
    allowed={'event','firmware','wifi_connected','computer_wake_enabled','state','ready',
             'turn_active','wake_state','wake_mic_open','code','samples','received_bytes','next_sequence','wire_bytes','trim_end'}
    try:
        port.open();port.reset_input_buffer()
        while time.monotonic()-started<480:
            now=time.monotonic()
            if not playing and now>=next_poll:
                port.write(poll_commands(announced));next_poll=now+3
            try:message=json.loads(port.readline())
            except ValueError:message={}
            kind=message.get('event')
            if kind in {'status','network_status','network_diagnostics','play_ready','play_end','play_error','reply_ack_sent','audio_upload_finished'}:
                safe={k:v for k,v in message.items() if k in allowed}
                safe['observed_ms']=round((time.monotonic()-started)*1000);events.append(safe)
                if kind=='network_status':
                    status=safe;pair=(safe.get('state'),safe.get('turn_active'))
                    if pair!=last_status:print(json.dumps(safe),flush=True);last_status=pair
                elif kind in {'play_ready','play_end','play_error','reply_ack_sent','audio_upload_finished'}:print(json.dumps(safe),flush=True)
                if (kind=='network_diagnostics' and status and status.get('ready') and
                        safe.get('wake_state')=='PC_WAKE_LISTEN' and not announced):
                    announced=True;print('READY_FOR_USER: PC_WAKE_LISTEN',flush=True)
                if kind=='play_ready':playing=True
                if kind in {'play_end','play_error'}:playing=False;play_end=safe
            if audit.exists() and audit.stat().st_size>initial:
                with audit.open('rb') as source:source.seek(initial);lines=source.read().splitlines()
                for line in reversed(lines):
                    try:item=json.loads(line)
                    except ValueError:continue
                    if is_target_reply(item,args.require_model_reply):result=item;break
            if result and play_end and status and status.get('state')=='READY' and not status.get('turn_active'):break
    finally:
        if port.is_open:port.close()
        record={'time':datetime.now().astimezone().isoformat(),'events':events,'audit':result,
                'raw_audio_saved':False,'text_saved':False,'readiness_announced':announced,
                'require_model_reply':args.require_model_reply}
        path=ROOT/'data'/datetime.now().strftime('real-reply-observer-%Y%m%d-%H%M%S.json')
        path.write_text(json.dumps(record,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
        print(json.dumps({'record':str(path),'audit':result}),flush=True)


if __name__=='__main__':main()
