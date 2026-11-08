import asyncio,hashlib,json,ssl,tempfile,unittest
from pathlib import Path
from unittest.mock import patch
from uuid import uuid4
from app.config import Settings
from app.budgeting import UsageLedger
from app.storage import CompanionStore
from app.devices import DeviceRegistry
from app.device_link import DeviceLinks
from app.worker_relay import device_connection,relay_forever,relay_hostname,RelaySocket
from test_devices import FakeBoard


class Remote:
    def __init__(self):self.messages=asyncio.Queue();self.output=asyncio.Queue();self.closed=None
    async def recv(self):return await self.messages.get()
    async def send(self,value):await self.output.put(value)
    async def close(self,code=1000,reason=''):self.closed=(code,reason)
    async def __aenter__(self):return self
    async def __aexit__(self,*args):pass


class WorkerRelayTests(unittest.IsolatedAsyncioTestCase):
    async def test_two_window_timing_matches_each_ack_while_next_window_is_buffering(self):
        from app.network_audio import encode_audio_frame
        records=[];remote=Remote();socket=RelaySocket(remote,'fixture',on_timing=records.append)
        identifier=str(uuid4());await socket.send_json({'type':'reply_start','reply_id':identifier,'bytes':34000})
        socket.window=16
        for sequence in range(33):await socket.send_bytes(encode_audio_frame(identifier,sequence,b'\0'*1000))
        self.assertEqual(len(socket.timing_windows),2);self.assertTrue(socket.frames)
        for sequence in (16,32):
            await remote.messages.put(json.dumps({'type':'reply_ack','reply_id':identifier,
                'next_sequence':sequence,'received_bytes':sequence*1000}));await socket.receive()
        self.assertEqual([row['frames'] for row in records],[16,16])
        self.assertEqual([row['next_sequence'] for row in records],[16,32])
        self.assertIsNotNone(socket.timing_window)
        await socket.close();self.assertFalse(socket.timing_windows);self.assertIsNone(socket.timing_window)

    async def test_window_timing_separates_local_send_ack_and_passive_ping_without_payload(self):
        from app.network_audio import encode_audio_frame
        clock=[1.0];records=[];remote=Remote();remote.latency=.123
        original_send=remote.send
        async def send(value):
            if isinstance(value,bytes):clock[0]+=.010
            await original_send(value)
        remote.send=send
        socket=RelaySocket(remote,'private-auth-sentinel',on_timing=records.append)
        identifier=str(uuid4())
        with patch('app.worker_relay.time.perf_counter',side_effect=lambda:clock[0]):
            await socket.send_json({'type':'reply_start','reply_id':identifier,'bytes':2500})
            for sequence,now,length in [(0,1.,1000),(1,1.02,1000),(2,1.05,500)]:
                clock[0]=now
                await socket.send_bytes(encode_audio_frame(identifier,sequence,b'Z'*length))
            self.assertFalse(records)
            clock[0]=1.56
            ack={'type':'reply_ack','reply_id':identifier,'next_sequence':3,'received_bytes':2500}
            await remote.messages.put(json.dumps(ack))
            self.assertEqual(json.loads((await socket.receive())['text']),ack)
        self.assertEqual(records,[{'segment_number':1,'next_sequence':3,'received_bytes':2500,
            'frames':3,'transport_sends':1,'window_total_ms':560,'local_assembly_ms':50,
            'socket_send_ms':10,'ack_after_send_ms':500,'host_relay_last_ping_rtt_ms':123}])
        encoded=json.dumps(records)
        for secret in [identifier,'private-auth-sentinel','ZZZZ','reply_id','authorization']:
            self.assertNotIn(secret,encoded)
        self.assertIsNone(socket.timing_window)

    async def test_window_timing_ignores_stale_or_wrong_ack_and_records_once(self):
        from app.network_audio import encode_audio_frame
        records=[];remote=Remote();socket=RelaySocket(remote,'fixture',on_timing=records.append)
        identifier=str(uuid4())
        await socket.send_json({'type':'reply_start','reply_id':identifier,'bytes':2})
        await socket.send_bytes(encode_audio_frame(identifier,0,b'ZZ'))
        ack={'type':'reply_ack','reply_id':identifier,'next_sequence':1,'received_bytes':2}
        for changes in [{'reply_id':str(uuid4())},{'next_sequence':True},{'received_bytes':True},{'received_bytes':3}]:
            await remote.messages.put(json.dumps(ack|changes));await socket.receive()
        self.assertFalse(records)
        for _ in range(2):await remote.messages.put(json.dumps(ack));await socket.receive()
        self.assertEqual(len(records),1)
        self.assertIsNone(records[0]['host_relay_last_ping_rtt_ms'])

    async def test_window_timing_abort_close_and_new_segment_discard_old_measurements(self):
        from app.network_audio import encode_audio_frame
        records=[];remote=Remote();socket=RelaySocket(remote,'fixture',on_timing=records.append)
        first,second=str(uuid4()),str(uuid4())
        await socket.send_json({'type':'reply_start','reply_id':first,'bytes':2000})
        await socket.send_bytes(encode_audio_frame(first,0,b'Z'*1000))
        await socket.send_json({'type':'reply_abort','reply_id':first})
        self.assertIsNone(socket.timing_window);self.assertIsNone(socket.timing_reply)
        await socket.send_json({'type':'reply_start','reply_id':second,'bytes':2})
        await socket.send_bytes(encode_audio_frame(second,0,b'ZZ'))
        await remote.messages.put(json.dumps({'type':'reply_ack','reply_id':second,'next_sequence':1,'received_bytes':2}))
        await socket.receive()
        self.assertEqual(len(records),1);self.assertEqual(records[0]['segment_number'],2)
        self.assertEqual(records[0]['frames'],1)
        await socket.close();self.assertIsNone(socket.timing_window);self.assertIsNone(socket.timing_reply)

    async def test_optional_timing_failure_keeps_unbatched_transport_and_ack_unchanged(self):
        from app.network_audio import encode_audio_frame
        def broken(_):raise OSError('fixture disk unavailable')
        remote=Remote();socket=RelaySocket(remote,'fixture',batching=False,on_timing=broken)
        identifier=str(uuid4());frame=encode_audio_frame(identifier,0,b'ZZ')
        await socket.send_json({'type':'reply_start','reply_id':identifier,'bytes':2})
        await remote.output.get();await socket.send_bytes(frame)
        self.assertEqual(await remote.output.get(),frame)
        ack={'type':'reply_ack','reply_id':identifier,'next_sequence':1,'received_bytes':2}
        await remote.messages.put(json.dumps(ack))
        self.assertEqual(json.loads((await socket.receive())['text']),ack)
        self.assertIsNone(socket.timing_window)

    async def test_close_records_control_reason_and_discards_pending_frames(self):
        from unittest.mock import Mock
        remote=Remote();callback=Mock();socket=RelaySocket(remote,'fixture',on_close=callback)
        socket.frames=[b'pending'];await socket.close(1008,'NETWORK_REPLY_INVALID_ACK')
        self.assertFalse(socket.frames)
        callback.assert_called_once_with(1008,'NETWORK_REPLY_INVALID_ACK')
        self.assertEqual(remote.closed,(1008,'NETWORK_REPLY_INVALID_ACK'))
    async def test_batches_flush_at_ack_window_and_partial_segment_boundaries(self):
        from app.network_audio import encode_audio_frame
        import struct
        remote=Remote();socket=RelaySocket(remote,'fixture')
        await socket.send_json({'type':'reply_start','bytes':16002});await remote.output.get()
        await remote.messages.put('{"type":"reply_ready","window_frames":16}');await socket.receive()
        identifier=str(uuid4());frames=[]
        for sequence in range(17):
            frame=encode_audio_frame(identifier,sequence,b'\0'*(1000 if sequence<16 else 2));frames.append(frame)
            await socket.send_bytes(frame)
        batches=[await remote.output.get() for _ in range(2)]
        self.assertEqual([struct.unpack_from('!H',b,4)[0] for b in batches],[16,1])
        restored=[]
        for batch in batches:
            self.assertLessEqual(len(batch),16422);offset=6
            for _ in range(struct.unpack_from('!H',batch,4)[0]):
                length=struct.unpack_from('!H',batch,offset)[0];offset+=2
                restored.append(batch[offset:offset+length]);offset+=length
            self.assertEqual(offset,len(batch))
        self.assertEqual(restored,frames);self.assertFalse(socket.frames)
    async def test_32_frame_window_keeps_16_frame_relay_batches(self):
        from app.network_audio import encode_audio_frame
        import struct
        remote=Remote();socket=RelaySocket(remote,'fixture')
        await socket.send_json({'type':'reply_start','bytes':32000});await remote.output.get()
        await remote.messages.put('{"type":"reply_ready","window_frames":32}')
        await socket.receive()
        identifier=str(uuid4())
        for sequence in range(32):
            await socket.send_bytes(encode_audio_frame(identifier,sequence,b'\0'*1000))
        batches=[await remote.output.get() for _ in range(2)]
        self.assertEqual([struct.unpack_from('!H',batch,4)[0] for batch in batches],[16,16])
        self.assertTrue(remote.output.empty())
        self.assertFalse(socket.frames)

    async def test_abort_discards_buffered_audio_and_legacy_mode_stays_unbatched(self):
        from app.network_audio import encode_audio_frame
        frame=encode_audio_frame(str(uuid4()),0,b'\0'*1000)
        remote=Remote();socket=RelaySocket(remote,'fixture');socket.total=64000
        await socket.send_bytes(frame);self.assertTrue(socket.frames)
        await socket.send_json({'type':'reply_abort'});self.assertFalse(socket.frames)
        self.assertEqual(json.loads(await remote.output.get())['type'],'reply_abort');self.assertTrue(remote.output.empty())
        old=RelaySocket(remote,'fixture',batching=False);await old.send_bytes(frame)
        self.assertEqual(await remote.output.get(),frame)
    async def asyncSetUp(self):
        temp=tempfile.TemporaryDirectory();self.addCleanup(temp.cleanup)
        self.store=CompanionStore(UsageLedger(Path(temp.name)/'fixture.db'))
        self.board=FakeBoard();self.registry=DeviceRegistry(self.store,self.board);self.record=self.registry.enroll()
        self.links=DeviceLinks(self.registry);self.remote=Remote()
        self.digest=hashlib.sha256(self.board.token.encode()).hexdigest()
        self.host='xiaonuo.fixture.workers.dev';self.token='1'*64
    async def open(self,**changes):
        await self.remote.messages.put(json.dumps({'type':'relay_waiting','protocol':1}))
        opened={'type':'relay_open','protocol':1,'authorization':'Bearer '+self.board.token};opened.update(changes)
        await self.remote.messages.put(json.dumps(opened))
    async def hello(self):
        await self.remote.messages.put(json.dumps({'type':'hello','protocol':1,'id':self.record['id'],
            'hardware_id':self.board.hardware,'firmware':'relay-test'}))
    async def turn(self):return await device_connection(self.links,self.host,self.token,self.digest)
    async def test_fixed_hostname_and_verified_tls_required_before_connect(self):
        self.assertEqual(relay_hostname('xiaonuo-example.pages.dev'),'xiaonuo-example.pages.dev')
        for value in ('pages.dev','x.y.pages.dev','x.pages.dev.evil.example','https://x.y.workers.dev','x.trycloudflare.com','evil.example.com','x.y.workers.dev.evil.example'):
            with self.assertRaises(ValueError):relay_hostname(value)
        context=ssl._create_unverified_context()
        with patch('app.worker_relay.connect') as connect:
            with self.assertRaises(ValueError):await device_connection(self.links,self.host,self.token,self.digest,context=context)
            connect.assert_not_called()
    async def test_bound_hello_heartbeat_and_owner_shutdown_use_existing_handler(self):
        await self.open();await self.hello()
        with patch('app.worker_relay.connect',return_value=self.remote) as connect:
            task=asyncio.create_task(self.turn())
            ready=json.loads(await asyncio.wait_for(self.remote.output.get(),2));self.assertEqual(ready['type'],'ready')
            await self.remote.messages.put('{"type":"ping","sequence":1}')
            self.assertEqual(json.loads(await self.remote.output.get()),{'type':'pong','sequence':1})
            await self.links.interrupt();await asyncio.wait_for(task,2)
            self.assertEqual(self.remote.closed[0],1012)
            options=connect.call_args.kwargs;self.assertEqual(options['max_queue'],4)
            self.assertEqual(options['user_agent_header'],'XiaoNuo-Host/1.0')
            self.assertEqual(options['ssl'].verify_mode,ssl.CERT_REQUIRED)
    async def test_pages_connection_is_direct_and_keeps_verified_authentication(self):
        self.host='xiaonuo-ingress.pages.dev'
        await self.open();await self.hello()
        with patch('app.worker_relay.connect',return_value=self.remote) as connect:
            task=asyncio.create_task(self.turn())
            self.assertEqual(json.loads(await asyncio.wait_for(self.remote.output.get(),2))['type'],'ready')
            await self.links.interrupt();await asyncio.wait_for(task,2)
            self.assertIsNone(connect.call_args.kwargs['proxy'])
            self.assertEqual(connect.call_args.kwargs['ssl'].verify_mode,ssl.CERT_REQUIRED)

    async def test_relay_cannot_substitute_another_device_credential(self):
        await self.open(authorization='Bearer '+'0'*64)
        with patch('app.worker_relay.connect',return_value=self.remote):
            with self.assertRaisesRegex(ValueError,'identity mismatch'):await self.turn()
        self.assertFalse(self.links._connections)
    async def test_wrong_hardware_still_rejected_after_relay_authentication(self):
        await self.open()
        await self.remote.messages.put(json.dumps({'type':'hello','protocol':1,'id':self.record['id'],
            'hardware_id':'esp32-ffffffffffff','firmware':'relay-test'}))
        with patch('app.worker_relay.connect',return_value=self.remote):await self.turn()
        self.assertEqual(self.remote.closed[0],1008)
    async def test_disconnect_waiting_for_hello_is_cancelled_without_leftover_connection(self):
        await self.open()
        with patch('app.worker_relay.connect',return_value=self.remote):
            task=asyncio.create_task(self.turn())
            for _ in range(100):
                if self.links._connections:break
                await asyncio.sleep(.01)
            await self.links.shutdown();await asyncio.wait_for(task,2)
        self.assertFalse(self.links._connections)
    async def test_reconnect_and_device_deletion_cancel_owned_tasks(self):
        stop=asyncio.Event();seen=asyncio.Event();cancelled=asyncio.Event();calls=[]
        async def connection(*args,**kwargs):
            calls.append(args[-1]);seen.set()
            if len(calls)==1:raise OSError('synthetic transport error')
            try:await asyncio.Future()
            finally:cancelled.set()
        with patch('app.worker_relay.device_connection',side_effect=connection):
            task=asyncio.create_task(relay_forever(self.links,self.host,self.token,stop,retry_seconds=.01))
            await asyncio.wait_for(seen.wait(),2)
            for _ in range(100):
                if len(calls)>=2:break
                await asyncio.sleep(.01)
            self.assertGreaterEqual(len(calls),2)
            with self.store.ledger.connect() as db:db.execute('DELETE FROM devices')
            await asyncio.wait_for(cancelled.wait(),3)
            stop.set();await asyncio.wait_for(task,2)
        self.assertEqual(set(calls),{self.digest})

    async def test_silent_idle_host_loss_reconnects_before_device_arrives(self):
        # Real WebSocket control frames, on loopback with synthetic credentials.
        # Drop only the first connection's pong, simulating a stale idle path.
        from websockets.asyncio.client import connect as real_connect
        from websockets.asyncio.server import serve
        from websockets.frames import Opcode
        dropped=asyncio.Event();recovered=asyncio.Event();stop=asyncio.Event()
        connections=0
        async def peer(connection):
            nonlocal connections
            connections+=1
            first=connections==1
            if first:
                send_frame=connection.protocol.send_frame
                def lose_pong(frame):
                    if frame.opcode==Opcode.PONG:dropped.set();return
                    send_frame(frame)
                connection.protocol.send_frame=lose_pong
            await connection.send('{"type":"relay_waiting","protocol":1}')
            if first:
                await connection.wait_closed();return
            await connection.send(json.dumps({'type':'relay_open','protocol':1,
                'authorization':'Bearer '+self.board.token}))
            await connection.send(json.dumps({'type':'hello','protocol':1,
                'id':self.record['id'],'hardware_id':self.board.hardware,'firmware':'idle-reconnect-test'}))
            self.assertEqual(json.loads(await connection.recv())['type'],'ready')
            await connection.send('{"type":"ping","sequence":1}')
            self.assertEqual(json.loads(await connection.recv()),{'type':'pong','sequence':1})
            recovered.set();await connection.wait_closed()
        async with serve(peer,'127.0.0.1',0,ping_interval=None) as server:
            port=server.sockets[0].getsockname()[1]
            def loopback(_uri,**options):
                # Production verifies TLS before connecting. This test replaces
                # only its destination with an isolated, unauthenticated LAN-free
                # loopback fixture; no production credentials or endpoint used.
                options.pop('ssl');options['proxy']=None
                return real_connect(f'ws://127.0.0.1:{port}',**options)
            with patch('app.worker_relay.connect',side_effect=loopback), \
                 patch('app.worker_relay.HOST_PING_INTERVAL',.03), \
                 patch('app.worker_relay.HOST_PING_TIMEOUT',.05):
                task=asyncio.create_task(relay_forever(self.links,self.host,self.token,stop,retry_seconds=.01))
                try:
                    await asyncio.wait_for(dropped.wait(),3)
                    await asyncio.wait_for(recovered.wait(),3)
                    self.assertGreaterEqual(connections,2)
                finally:
                    stop.set();await asyncio.wait_for(task,3)
        self.assertFalse(self.links._connections)
