"""Outbound WSS transport reusing the existing bound-device protocol handler."""
import asyncio
import hashlib
import json
import math
import re
import socket
import ssl
import struct
import time
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import urlsplit

from starlette.websockets import WebSocketDisconnect
from websockets.asyncio.client import connect
from websockets.exceptions import ConnectionClosed

from .device_ingress import fixed_hostname

# Idle hosts still need a live relay connection when the device is offline.
# Without pings a silently lost socket can remain WAITING_FOR_DEVICE forever.
HOST_PING_INTERVAL = 20
HOST_PING_TIMEOUT = 30


def relay_hostname(value):
    host=fixed_hostname(value)
    if not ((host.endswith('.workers.dev') and host.count('.')==3) or (host.endswith('.pages.dev') and host.count('.')==2)):
        raise ValueError('Expected a fixed Worker or Pages hostname, without a scheme or path')
    return host


class RelaySocket:
    """ASGI-shaped adapter; only the validated relay_open supplies auth headers."""
    def __init__(self,connection,authorization,*,batching=True,batch_frames=16,on_close=lambda code,reason:None,on_timing=lambda record:None):
        self.connection=connection
        self.url=SimpleNamespace(scheme='wss')
        self.query_params={}
        self.headers={'authorization':authorization}
        self.batching=batching;self.batch_frames=batch_frames;self.frames=[];self.window=4;self.sent=0;self.total=0;self.sequence=0
        self.on_close=on_close
        self.on_timing=on_timing
        self.timing_reply=None;self.timing_window=None;self.timing_segment=0
        self.timing_windows={}
    async def accept(self):pass
    async def close(self,code=1000,reason=''):
        self.frames.clear()
        self.timing_reply=None;self.timing_window=None
        self.timing_windows.clear()
        self.on_close(code,reason)
        await self.connection.close(code=code,reason=reason)
    async def send_json(self,message):
        if message.get('type')=='reply_abort':
            self.frames.clear();self.timing_reply=None;self.timing_window=None
            self.timing_windows.clear()
        else:await self.flush()
        if message.get('type')=='reply_start':
            self.window=4;self.sent=0;self.sequence=0;self.total=message['bytes']
            self.timing_segment+=1;self.timing_reply=message.get('reply_id');self.timing_window=None
            self.timing_windows.clear()
        await self.connection.send(json.dumps(message,ensure_ascii=False,separators=(',',':')))
    async def send_audio(self,payload,frames):
        started=time.perf_counter()
        await self.connection.send(payload)
        ended=time.perf_counter()
        if self.timing_window is not None:
            self.timing_window['send_seconds']+=ended-started
            self.timing_window['last_sent']=ended
            self.timing_window['sends']+=1
            self.timing_window['frames']+=frames
            if self.sequence%self.window==0 or self.sent==self.total:
                self.timing_window['sequence']=self.sequence;self.timing_window['bytes']=self.sent
                if len(self.timing_windows)>=2:self.timing_windows.pop(next(iter(self.timing_windows)))
                self.timing_windows[self.sequence]=self.timing_window;self.timing_window=None
    async def flush(self):
        if self.frames:
            frames,self.frames=self.frames,[]
            await self.send_audio(b'XNB1'+struct.pack('!H',len(frames))+b''.join(struct.pack('!H',len(frame))+frame for frame in frames),len(frames))
    async def send_bytes(self,data):
        if self.timing_reply is not None and self.timing_window is None:
            self.timing_window={'started':time.perf_counter(),'last_sent':None,'send_seconds':0.,'sends':0,'frames':0}
        if not self.batching:
            self.sequence+=1;self.sent+=len(data)-24
            return await self.send_audio(data,1)
        if not 24<len(data)<=1024:raise ValueError('Invalid relay audio frame')
        self.frames.append(data);self.sequence+=1;self.sent+=len(data)-24
        # Flush at both protocol boundaries. Waiting for a batch to fill at
        # the last frame would deadlock the existing window acknowledgement.
        if len(self.frames)==self.batch_frames or self.sequence%self.window==0 or self.sent==self.total:await self.flush()
    def record_window(self,message):
        sequence=message.get('next_sequence')
        if message.get('type')!='reply_ack' or type(sequence) is not int:return
        window=self.timing_windows.get(sequence)
        if (window is None or message.get('reply_id')!=self.timing_reply
                or type(message.get('received_bytes')) is not int or message['received_bytes']!=window['bytes']):return
        now=time.perf_counter();latency=getattr(self.connection,'latency',None)
        # The library's last automatic ping is passive and may predate this
        # window; zero means not measured. It is not a device round trip.
        ping_ms=round(latency*1000) if type(latency) in {float,int} and math.isfinite(latency) and latency>0 else None
        record={'segment_number':self.timing_segment,'next_sequence':sequence,'received_bytes':window['bytes'],
                'frames':window['frames'],'transport_sends':window['sends'],
                'window_total_ms':round((now-window['started'])*1000),
                'local_assembly_ms':round((window['last_sent']-window['started']-window['send_seconds'])*1000),
                'socket_send_ms':round(window['send_seconds']*1000),
                'ack_after_send_ms':round((now-window['last_sent'])*1000),'host_relay_last_ping_rtt_ms':ping_ms}
        self.timing_windows.pop(sequence)
        try:self.on_timing(record)
        except Exception:pass # Optional diagnostics must not stop playback.
    async def receive(self):
        try:message=await self.connection.recv()
        except ConnectionClosed as error:raise WebSocketDisconnect(error.rcvd.code if error.rcvd else 1006) from None
        if isinstance(message,str):
            try:
                parsed=json.loads(message)
                if isinstance(parsed,dict):self.record_window(parsed)
                if isinstance(parsed,dict) and parsed.get('type')=='reply_ready':
                    size=parsed.get('window_frames',4)
                    if type(size) is int and size in {4,8,16,32}:self.window=size
            except ValueError:pass
        return {'type':'websocket.receive',('text' if isinstance(message,str) else 'bytes'):message}


async def device_connection(links,hostname,host_token,device_hash,*,context=None,on_state=lambda state:None,batching=True,require_batching=False):
    host=relay_hostname(hostname)
    if not re.fullmatch('[0-9a-f]{64}',host_token) or not re.fullmatch('[0-9a-f]{64}',device_hash):
        raise ValueError('Invalid relay credential format')
    context=context or ssl.create_default_context()
    if context.verify_mode!=ssl.CERT_REQUIRED or not context.check_hostname:
        raise ValueError('Relay certificate verification is required')
    async with connect(f'wss://{host}/host/{device_hash}',ssl=context,
                       additional_headers={'Authorization':'Bearer '+host_token,**({'X-XiaoNuo-Batch':'16'} if batching else {})},open_timeout=10,
                       family=socket.AF_INET,
                       user_agent_header='XiaoNuo-Host/1.0',
                       close_timeout=2,ping_interval=HOST_PING_INTERVAL,
                       ping_timeout=HOST_PING_TIMEOUT,max_size=8192,
                       max_queue=4,compression=None,**({'proxy':None} if host.endswith('.pages.dev') else {})) as connection:
        async def control():
            frame=await connection.recv()
            if not isinstance(frame,str) or len(frame.encode())>1024:raise ValueError('Invalid relay control')
            message=json.loads(frame)
            if not isinstance(message,dict):raise ValueError('Invalid relay control')
            return message
        waiting=await asyncio.wait_for(control(),5)
        base={'type':'relay_waiting','protocol':1}
        if waiting not in (base,base|{'batch_frames':7},base|{'batch_frames':16}) or type(waiting['protocol']) is not int or ('batch_frames' in waiting and type(waiting['batch_frames']) is not int):raise ValueError('Relay protocol mismatch')
        if require_batching and waiting.get('batch_frames') not in {7,16}:raise ValueError('Relay batching capability unavailable')
        on_state('WAITING_FOR_DEVICE')
        opened=await control() # Socket pings monitor a computer waiting for its device.
        if (set(opened)!={'type','protocol','authorization'} or opened['type']!='relay_open'
                or type(opened['protocol']) is not int or opened['protocol']!=1
                or not isinstance(opened['authorization'],str)
                or not re.fullmatch('Bearer [0-9a-f]{64}',opened['authorization'])
                or hashlib.sha256(opened['authorization'][7:].encode()).hexdigest()!=device_hash):
            raise ValueError('Relay device identity mismatch')
        on_state('DEVICE_CONNECTED')
        def closed(code,reason):
            safe=reason if isinstance(reason,str) and re.fullmatch(r'(?:NETWORK_|DEVICE_LINK_|SPEECH_|COMPUTER_WAKE_)[A-Z_]{1,70}',reason) else 'SESSION_CLOSED'
            try:
                path=Path(links.registry.store.ledger.path).parent/'diagnostics/relay-session-close.jsonl'
                path.parent.mkdir(parents=True,exist_ok=True)
                with path.open('a',encoding='utf-8') as log:
                    log.write(json.dumps({'time':time.strftime('%Y-%m-%dT%H:%M:%S%z'),'code':code,'reason':safe})+'\n')
            except OSError:pass
        def timing(record):
            try:
                path=Path(links.registry.store.ledger.path).parent/'diagnostics/relay-window-timing.jsonl'
                path.parent.mkdir(parents=True,exist_ok=True)
                if path.exists() and path.stat().st_size>2*1024*1024:path.replace(path.with_suffix('.previous.jsonl'))
                with path.open('a',encoding='utf-8') as log:log.write(json.dumps(record,separators=(',',':'))+'\n')
            except OSError:pass
        await links.serve(RelaySocket(connection,opened['authorization'],batching=batching and waiting.get('batch_frames') in {7,16},batch_frames=waiting.get('batch_frames',16),on_close=closed,on_timing=timing))


async def relay_forever(links,hostname,host_token,stop,*,retry_seconds=5,status=None):
    """One connection per bound identity, max eight; deletion stops its task.

    Caller owns the model/app lifespan and a process lock. Reconnect never
    replays audio, chat, or prior replies. No payload or credential logging.
    """
    relay_hostname(hostname)
    if not re.fullmatch('[0-9a-f]{64}',host_token):raise ValueError('Invalid host credential')
    tasks={}
    status=status if status is not None else {}
    status['devices']={}
    async def maintain(device_hash):
        record=status['devices'].setdefault(device_hash,{})
        while not stop.is_set():
            record['state']='CONNECTING'
            try:await device_connection(links,hostname,host_token,device_hash,on_state=lambda state:record.update(state=state,last_error=None))
            except asyncio.CancelledError:raise
            except Exception as error:
                record.update(state='RETRY_WAIT',last_error=type(error).__name__)
                try:
                    entry={'ts':time.strftime('%Y-%m-%dT%H:%M:%S%z'),'type':type(error).__name__,
                           'code':getattr(error,'code',None),'reason':str(getattr(error,'reason',''))[:120]}
                    with open(Path(__file__).resolve().parents[2]/'data/diagnostics/relay-close.jsonl','a',encoding='utf-8') as f:
                        f.write(json.dumps(entry,ensure_ascii=False)+'\n')
                except Exception:
                    pass
            try:await asyncio.wait_for(stop.wait(),retry_seconds)
            except TimeoutError:pass
    try:
        while not stop.is_set():
            with links.registry.store.ledger.connect() as db:
                hashes={row[0] for row in db.execute("SELECT token_hash FROM devices WHERE revoked_at IS NULL AND provisioning_state='bound'")
                        if re.fullmatch('[0-9a-f]{64}',row[0] or '')}
            if len(hashes)>8:raise ValueError('Relay device capacity exceeded')
            for digest in set(tasks)-hashes:
                task=tasks.pop(digest);task.cancel();await asyncio.gather(task,return_exceptions=True)
                status['devices'].pop(digest,None)
            for digest in hashes-set(tasks):tasks[digest]=asyncio.create_task(maintain(digest))
            try:await asyncio.wait_for(stop.wait(),2)
            except TimeoutError:pass
    finally:
        for task in tasks.values():task.cancel()
        await asyncio.gather(*tasks.values(),return_exceptions=True)
        status['stopped']=True


async def serve_worker_host(owner,hostname,host_token,*,stop=None,console_port=8765,ready=None):
    """One model lifecycle and loopback console; all relay traffic is outbound.

    No public local listener, reverse proxy, port mapping or private-CA origin
    is needed for this route. Caller holds the existing backend runtime lock.
    """
    import uvicorn
    from .network_host import _bind,_Listener,check_audio_readiness
    from .errors import XiaoNuoError
    relay_hostname(hostname)
    if not re.fullmatch('[0-9a-f]{64}',host_token):raise ValueError('Invalid host credential')
    listener=_bind(console_port);stop=stop if stop is not None else asyncio.Event()
    server=_Listener(uvicorn.Config(owner,host='127.0.0.1',port=listener.getsockname()[1],
        lifespan='off',proxy_headers=False,access_log=False,log_level='warning',timeout_graceful_shutdown=5))
    tasks=[];stop_task=None
    try:
        async with owner.router.lifespan_context(owner):
            check_audio_readiness(owner)
            try:
                tasks.append(asyncio.create_task(server.serve(sockets=[listener])))
                async with asyncio.timeout(10):
                    while not server.started:
                        if tasks[0].done():raise XiaoNuoError('NETWORK_LISTENER_START_FAILED',503)
                        if stop.is_set():return
                        await asyncio.sleep(.01)
                owner.state.relay_status={}
                tasks.append(asyncio.create_task(relay_forever(owner.state.device_links,hostname,host_token,stop,status=owner.state.relay_status)))
                if ready is not None and not ready.done():ready.set_result(listener.getsockname()[1])
                stop_task=asyncio.create_task(stop.wait())
                done,_=await asyncio.wait([*tasks,stop_task],return_when=asyncio.FIRST_COMPLETED)
                if stop_task not in done:raise XiaoNuoError('WORKER_RELAY_HOST_EXITED',503)
            finally:
                stop.set();server.should_exit=True
                if stop_task is not None:stop_task.cancel()
                try:await owner.state.device_links.shutdown()
                finally:
                    done,pending=await asyncio.wait(tasks,timeout=10) if tasks else (set(),set())
                    for task in pending:task.cancel()
                    await asyncio.gather(*tasks,return_exceptions=True)
                    if stop_task is not None:await asyncio.gather(stop_task,return_exceptions=True)
    finally:listener.close()

