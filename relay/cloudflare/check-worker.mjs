// Actual local workerd runtime. No Cloudflare account, physical I/O, or API chat.
import assert from 'node:assert/strict';
import { createHash } from 'node:crypto';
import { fileURLToPath } from 'node:url';
import { resolve } from 'node:path';
import { Miniflare } from '../../data/tools/cloudflare-runtime/node_modules/miniflare/dist/src/index.js';

const hostToken='1'.repeat(64),deviceToken='2'.repeat(64),hash=x=>createHash('sha256').update(x).digest('hex');
const mf=new Miniflare({modules:true,scriptPath:process.argv[2]?resolve(process.argv[2]):fileURLToPath(new URL('./worker.mjs',import.meta.url)),
  compatibilityDate:'2026-07-01',durableObjects:{RELAY:{className:'DeviceRelay',useSQLite:true}},
  bindings:{HOST_TOKEN_SHA256:hash(hostToken),DEVICE_HASHES:JSON.stringify([hash(deviceToken)])}});
const open=async(path,token,extra={})=>mf.dispatchFetch('https://relay.example'+path,{headers:{Upgrade:'websocket',Authorization:'Bearer '+token,...extra}});
function inbox(ws){
  const queued=[],waiting=[];
  ws.addEventListener('message',e=>{if(waiting.length)waiting.shift().resolve(e.data);else queued.push(e.data);});
  ws.addEventListener('close',e=>{for(const w of waiting.splice(0))w.reject(new Error('CLOSED:'+e.code));});
  const read=()=>queued.length?Promise.resolve(queued.shift()):new Promise((resolve,reject)=>{
    const item={resolve:v=>{clearTimeout(timer);resolve(v);},reject:e=>{clearTimeout(timer);reject(e);}};
    const timer=setTimeout(()=>{const index=waiting.indexOf(item);if(index>=0)waiting.splice(index,1);reject(new Error('RECV_TIMEOUT'));},3000);waiting.push(item);
  });
  read.queued=()=>queued.length;
  return read;
}
try{
  assert.equal((await mf.dispatchFetch('http://relay.example/healthz')).status,426);
  assert.equal((await mf.dispatchFetch('https://relay.example/healthz')).status,200);
  for(const path of ['/api/v1/privacy/export','/api/v1/preferences','/dashboard/app.js'])assert.equal((await mf.dispatchFetch('https://relay.example'+path)).status,404);
  const devicePath='/api/v1/device-link/ws',hostPath='/host/'+hash(deviceToken);
  const replyId='11111111-1111-1111-1111-111111111111';
  const replyPath='/reply/'+hash(deviceToken)+'/'+replyId;
  const uploadPath='/host/'+hash(deviceToken)+'/reply/'+replyId;
  const payload=Buffer.from(Array.from({length:9000},(_,i)=>i%251));
  const sha=hash(payload);
  const upload=(token=hostToken,digest=sha)=>mf.dispatchFetch('https://relay.example'+uploadPath,{method:'POST',
    headers:{Authorization:'Bearer '+token,'X-XiaoNuo-SHA256':digest},body:payload});
  assert.equal((await upload(deviceToken)).status,403);
  assert.equal((await upload(hostToken,'0'.repeat(64))).status,422);
  assert.equal((await upload()).status,201);
  assert.equal((await mf.dispatchFetch('https://relay.example'+replyPath,{method:'GET',
    headers:{Authorization:'Bearer '+hostToken}})).status,403);
  const head=await mf.dispatchFetch('https://relay.example'+replyPath,{method:'HEAD',
    headers:{Authorization:'Bearer '+deviceToken}});
  assert.equal(head.status,200);assert.equal(head.headers.get('accept-ranges'),'bytes');
  assert.equal(head.headers.get('content-length'),String(payload.length));
  const ranged=await mf.dispatchFetch('https://relay.example'+replyPath,{headers:{
    Authorization:'Bearer '+deviceToken,Range:'bytes=100-199'}});
  assert.equal(ranged.status,206);assert.equal(ranged.headers.get('content-range'),'bytes 100-199/'+payload.length);
  assert.deepEqual(Buffer.from(await ranged.arrayBuffer()),payload.subarray(100,200));
  const replyFull=await mf.dispatchFetch('https://relay.example'+replyPath,{headers:{Authorization:'Bearer '+deviceToken}});
  assert.equal(replyFull.status,200);assert.deepEqual(Buffer.from(await replyFull.arrayBuffer()),payload);
  assert.equal((await mf.dispatchFetch('https://relay.example'+replyPath,{headers:{
    Authorization:'Bearer '+deviceToken,Range:'bytes=999999-'}})).status,416);
  assert.equal((await open(devicePath,'0'.repeat(64))).status,403);
  assert.equal((await open(hostPath,deviceToken)).status,403);
  assert.equal((await open(devicePath,deviceToken,{Origin:'https://evil.example'})).status,403);
  assert.equal((await open(devicePath+'?token=hidden',deviceToken)).status,403);
  assert.equal((await open(devicePath,deviceToken)).status,503);
  const hostResponse=await open(hostPath,hostToken,{'X-XiaoNuo-Batch':'16'});assert.equal(hostResponse.status,101);
  const host=hostResponse.webSocket,readHost=inbox(host);host.accept();
  assert.deepEqual(JSON.parse(await readHost()),{type:'relay_waiting',protocol:1,batch_frames:16});
  const deviceResponse=await open(devicePath,deviceToken);assert.equal(deviceResponse.status,101);
  const device=deviceResponse.webSocket,readDevice=inbox(device);device.accept();
  assert.deepEqual(JSON.parse(await readHost()),{type:'relay_open',protocol:1,authorization:'Bearer '+deviceToken});
  assert.equal((await open(devicePath,deviceToken)).status,409);
  device.send('{"type":"hello"}');assert.equal(await readHost(),'{"type":"hello"}');
  host.send('{"type":"ready"}');assert.equal(await readDevice(),'{"type":"ready"}');
  const pcm=Uint8Array.from({length:1024},(_,i)=>i%256);
  device.send(pcm);assert.deepEqual(new Uint8Array(await readHost()),pcm);
  host.send(pcm);assert.deepEqual(new Uint8Array(await readDevice()),pcm);
  const frame=Buffer.alloc(1024);frame.write('XNA1');frame.fill(23,24);
  const batch=Buffer.concat([Buffer.from('XNB1'),Buffer.from([0,2,4,0]),frame,Buffer.from([4,0]),frame]);
  host.send(batch);assert.deepEqual(new Uint8Array(await readDevice()),new Uint8Array(frame));
  assert.deepEqual(new Uint8Array(await readDevice()),new Uint8Array(frame));
  const full=Buffer.concat([Buffer.from('XNB1'),Buffer.from([0,16]),...Array.from({length:16},()=>[Buffer.from([4,0]),frame]).flat()]);
  host.send(full);for(let i=0;i<16;i++)assert.deepEqual(new Uint8Array(await readDevice()),new Uint8Array(frame));
  const closed=new Promise(resolve=>device.addEventListener('close',resolve,{once:true}));
  host.close(1012,'OWNER_STOPPED');assert.equal((await closed).code,1012);
  // A fresh room closes both peers on oversized device frames.
  const h2=(await open(hostPath,hostToken)).webSocket,rh2=inbox(h2);h2.accept();await rh2();
  const d2=(await open(devicePath,deviceToken)).webSocket;d2.accept();await rh2();
  const limit=new Promise(resolve=>d2.addEventListener('close',resolve,{once:true}));
  d2.send(new Uint8Array(1025));assert.equal((await limit).code,1009);
  for(const malformed of [Buffer.from('XNB1'),Buffer.concat([batch,Buffer.from([0])]),
       Buffer.concat([Buffer.from('XNB1'),Buffer.from([0,2,4,0]),frame,Buffer.from([0,25]),Buffer.alloc(25)])]){
    const response=await open(hostPath,hostToken,{'X-XiaoNuo-Batch':'7'});assert.equal(response.status,101);
    const h=response.webSocket,rh=inbox(h);h.accept();await rh();
    const d=(await open(devicePath,deviceToken)).webSocket;d.accept();await rh();let received=0;
    d.addEventListener('message',()=>received++);
    const rejected=new Promise(resolve=>d.addEventListener('close',resolve,{once:true}));
    h.send(malformed);assert.equal((await rejected).code,1008);assert.equal(received,0);
  }
  // Opted-in ESP32 receives bounded eight-frame batches; legacy peers above
  // still receive the original individual frames.
  const bh=(await open(hostPath,hostToken,{'X-XiaoNuo-Batch':'16'})).webSocket,brh=inbox(bh);bh.accept();await brh();
  const bdResponse=await open(devicePath,deviceToken,{'X-XiaoNuo-Rx-Batch':'8'});assert.equal(bdResponse.status,101);
  const bd=bdResponse.webSocket,brd=inbox(bd);bd.accept();await brh();
  bh.send(full);
  const expectedEight=Buffer.concat([Buffer.from('XNB1'),Buffer.from([0,8]),...Array.from({length:8},()=>[Buffer.from([4,0]),frame]).flat()]);
  for(let i=0;i<2;i++){
    const received=Buffer.from(await brd());assert.equal(received.length,8214);assert.deepEqual(received,expectedEight);
  }
  const three=Buffer.concat([Buffer.from('XNB1'),Buffer.from([0,3]),...Array.from({length:3},()=>[Buffer.from([4,0]),frame]).flat()]);
  bh.send(three);assert.deepEqual(Buffer.from(await brd()),three);
  bh.send(frame);assert.deepEqual(Buffer.from(await brd()),frame);
  bd.send(frame);assert.deepEqual(Buffer.from(await brh()),frame);
  assert.equal((await open(devicePath,deviceToken,{'X-XiaoNuo-Rx-Batch':'7'})).status,422);
  bh.send('{"type":"ready"}');assert.equal(await brd(),'{"type":"ready"}');
  const batchPeerClosed=new Promise(resolve=>bd.addEventListener('close',resolve,{once:true}));
  bh.close(1012,'TEST_DONE');assert.equal((await batchPeerClosed).code,1012);
  // A new host has no old ping sequence or audio state. Reset the old device
  // and require a fresh authenticated hello instead of replaying cached state.
  const h1r=await open(hostPath,hostToken,{'X-XiaoNuo-Batch':'16'});assert.equal(h1r.status,101);
  const h1=h1r.webSocket,r1=inbox(h1);h1.accept();await r1();
  const dr=await open(devicePath,deviceToken);assert.equal(dr.status,101);
  const d=dr.webSocket,rd=inbox(d);d.accept();await r1();
  d.send('{"type":"hello","marker":1}');assert.equal(await r1(),'{"type":"hello","marker":1}');
  assert.equal((await open(hostPath,deviceToken)).status,403);
  d.send('{"type":"ping","sequence":7}');assert.equal(await r1(),'{"type":"ping","sequence":7}');
  const oldClosed=new Promise(resolve=>h1.addEventListener('close',resolve,{once:true}));
  const oldDeviceClosed=new Promise(resolve=>d.addEventListener('close',resolve,{once:true}));
  const nh2r=await open(hostPath,hostToken,{'X-XiaoNuo-Batch':'16'});assert.equal(nh2r.status,101);
  const nh2=nh2r.webSocket,nr2=inbox(nh2);nh2.accept();
  assert.deepEqual(JSON.parse(await nr2()),{type:'relay_waiting',protocol:1,batch_frames:16});
  await new Promise(resolve=>setTimeout(resolve,50));
  assert.equal(nr2.queued(),0,'new host must not receive stale device credentials or hello');
  assert.equal((await oldClosed).code,1012);
  const oldDeviceEvent=await oldDeviceClosed;
  assert.equal(oldDeviceEvent.code,1012);assert.equal(oldDeviceEvent.reason,'HOST_REPLACED');
  const newDeviceResponse=await open(devicePath,deviceToken);assert.equal(newDeviceResponse.status,101);
  const newDevice=newDeviceResponse.webSocket,readNewDevice=inbox(newDevice);newDevice.accept();
  assert.deepEqual(JSON.parse(await nr2()),{type:'relay_open',protocol:1,authorization:'Bearer '+deviceToken});
  newDevice.send('{"type":"hello","marker":"new-session"}');assert.equal(await nr2(),'{"type":"hello","marker":"new-session"}');
  nh2.send('{"type":"ready"}');assert.equal(await readNewDevice(),'{"type":"ready"}');
  newDevice.send('{"type":"ping","sequence":1}');assert.equal(await nr2(),'{"type":"ping","sequence":1}');
  nh2.send('{"type":"pong","sequence":1}');assert.equal(await readNewDevice(),'{"type":"pong","sequence":1}');
  nh2.send(frame);assert.deepEqual(Buffer.from(await readNewDevice()),frame);
  nh2.close(1012,'TEST_DONE');
  // A silent old device is replaced only after the real 30-second grace.
  const sh=(await open(hostPath,hostToken)).webSocket,srh=inbox(sh);sh.accept();await srh();
  const sd=(await open(devicePath,deviceToken)).webSocket;sd.accept();await srh();
  assert.equal((await open(devicePath,deviceToken)).status,409);
  await new Promise(resolve=>setTimeout(resolve,31000));
  const staleHostClosed=new Promise(resolve=>sh.addEventListener('close',resolve,{once:true}));
  const staleDeviceClosed=new Promise(resolve=>sd.addEventListener('close',resolve,{once:true}));
  assert.equal((await open(devicePath,deviceToken)).status,503);
  assert.equal((await staleHostClosed).code,1012);assert.equal((await staleDeviceClosed).code,1012);
  const fh=(await open(hostPath,hostToken)).webSocket,frh=inbox(fh);fh.accept();
  assert.equal(JSON.parse(await frh()).type,'relay_waiting');
  const freshResponse=await open(devicePath,deviceToken);assert.equal(freshResponse.status,101);
  const fd=freshResponse.webSocket,frd=inbox(fd);fd.accept();
  assert.equal(JSON.parse(await frh()).type,'relay_open');
  fd.send('{"type":"hello","marker":"fresh"}');assert.equal(await frh(),'{"type":"hello","marker":"fresh"}');
  fh.send('{"type":"ready"}');assert.equal(await frd(),'{"type":"ready"}');
  fh.close(1012,'TEST_DONE');
  console.log(JSON.stringify({actual_workerd:true,owner_routes_absent:true,unauthorized_and_offline_rejected:true,
    one_computer_and_one_device:true,text_and_binary_exact:true,disconnect_closes_peer:true,frame_limit_passed:true,
    reply_http_ranges:true,stale_device_reconnect:true,healthy_duplicate_rejected:true,device_receive_batch8:true,
    new_host_requires_fresh_device_hello:true,stale_host_hello_not_replayed:true,fresh_session_ping_and_binary_passed:true,cloud_deployed:false}));
}finally{await mf.dispose();}
