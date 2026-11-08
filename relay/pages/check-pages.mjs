import assert from 'node:assert/strict';
import {createHash} from 'node:crypto';
import {fileURLToPath} from 'node:url';
import {Miniflare} from '../../data/tools/cloudflare-runtime/node_modules/miniflare/dist/src/index.js';
const hash=x=>createHash('sha256').update(x).digest('hex'),owner='1'.repeat(64),device='2'.repeat(64);
const mf=new Miniflare({workers:[{name:'ingress',modules:true,scriptPath:fileURLToPath(new URL('./public/_worker.js',import.meta.url)),compatibilityDate:'2026-07-01',serviceBindings:{RELAY_SERVICE:'relay'}},{name:'relay',modules:true,scriptPath:fileURLToPath(new URL('../cloudflare/worker.mjs',import.meta.url)),compatibilityDate:'2026-07-01',durableObjects:{RELAY:{className:'DeviceRelay',useSQLite:true}},bindings:{HOST_TOKEN_SHA256:hash(owner),DEVICE_HASHES:JSON.stringify([hash(device)])}}]});
const url='https://ingress.example',path='/api/v1/device-link/ws';
try{
 assert.equal((await mf.dispatchFetch(url+'/healthz')).status,200);
 for(const p of ['/api/v1/preferences','/api/v1/privacy/export','/host/not-a-digest'])assert.equal((await mf.dispatchFetch(url+p)).status,404);
 assert.equal((await mf.dispatchFetch(url+'/healthz?key=secret')).status,403);
 assert.equal((await mf.dispatchFetch(url+'/healthz',{headers:{Origin:'https://evil.example'}})).status,403);
 assert.equal((await mf.dispatchFetch(url+path,{headers:{Upgrade:'websocket',Authorization:'Bearer '+'0'.repeat(64)}})).status,403);
 assert.equal((await mf.dispatchFetch(url+'/host/'+hash(device),{headers:{Upgrade:'websocket',Authorization:'Bearer '+device}})).status,403);
 const relay=await mf.getWorker('relay');
 const h=(await mf.dispatchFetch(url+'/host/'+hash(device),{headers:{Upgrade:'websocket',Authorization:'Bearer '+owner}})).webSocket;h.accept();
 const d=(await mf.dispatchFetch(url+path,{headers:{Upgrade:'websocket',Authorization:'Bearer '+device}})).webSocket;assert.ok(d);d.accept();
 const receive=ws=>new Promise((resolve,reject)=>{const timer=setTimeout(()=>reject(Error('Timeout')),3000);ws.addEventListener('message',e=>{clearTimeout(timer);resolve(e.data)},{once:true});});
 let incoming=receive(h);d.send('hello');assert.equal(await incoming,'hello');
 incoming=receive(d);h.send(new Uint8Array([1,2,3]));assert.deepEqual(new Uint8Array(await incoming),new Uint8Array([1,2,3]));
 const closed=new Promise(resolve=>d.addEventListener('close',resolve,{once:true}));h.close(1012,'TEST_DONE');assert.equal((await closed).code,1012);
 console.log(JSON.stringify({actual_workerd:true,actual_service_binding:true,existing_room_shared:true,auth_enforced:true,management_routes_absent:true,text_binary_roundtrip:true,owner_disconnect_closes_device:true}));
}finally{await mf.dispose();}
