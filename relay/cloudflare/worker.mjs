// One outbound computer socket and one device socket per approved device.
// No audio, transcripts, credentials, or messages are written to storage/logs.
import { DurableObject } from 'cloudflare:workers';

const encoder=new TextEncoder();
const hex=/^[0-9a-f]{64}$/;
const headers={'Cache-Control':'no-store'};
const DEVICE_STALE_MS=30000;
function packFrames(frames){
  const bytes=6+frames.reduce((sum,frame)=>sum+2+frame.byteLength,0),out=new Uint8Array(bytes),view=new DataView(out.buffer);
  out.set([88,78,66,49]);view.setUint16(4,frames.length);let offset=6;
  for(const frame of frames){view.setUint16(offset,frame.byteLength);offset+=2;out.set(new Uint8Array(frame),offset);offset+=frame.byteLength;}
  return out.buffer;
}
const reply=(status,text)=>new Response(text,{status,headers});
async function digest(token){return Array.from(new Uint8Array(await crypto.subtle.digest('SHA-256',encoder.encode(token))),n=>n.toString(16).padStart(2,'0')).join('');}
function approved(env){
  try{const list=JSON.parse(env.DEVICE_HASHES||'[]');return Array.isArray(list)&&list.length<=8&&list.every(x=>typeof x==='string'&&hex.test(x))?list:[];}catch{return [];}
}
export default {
  async fetch(request,env){
    const url=new URL(request.url);
    if(url.protocol!=='https:')return reply(426,'TLS_REQUIRED');
    if(url.search||request.headers.has('Origin'))return reply(403,'RELAY_REQUEST_REJECTED');
    if(request.method==='GET'&&url.pathname==='/healthz')return Response.json({service:'xiaonuo-device-relay',protocol:1},{headers});
    const host=url.pathname.match(/^\/host\/([0-9a-f]{64})$/);
    const hostReply=url.pathname.match(/^\/host\/([0-9a-f]{64})\/reply\/([0-9a-f-]{36})$/);
    const deviceReply=url.pathname.match(/^\/reply\/([0-9a-f]{64})\/([0-9a-f-]{36})$/);
    const deviceWs=url.pathname==='/api/v1/device-link/ws';
    if(!host&&!hostReply&&!deviceReply&&!deviceWs)return reply(404,'NOT_FOUND');
    const authorization=request.headers.get('Authorization')||'';
    if(!/^Bearer [0-9a-f]{64}$/.test(authorization))return reply(403,'RELAY_AUTH_REQUIRED');
    const tokenHash=await digest(authorization.slice(7)),devices=approved(env);
    if(!hex.test(env.HOST_TOKEN_SHA256||''))return reply(503,'RELAY_NOT_CONFIGURED');
    const deviceHash=host?host[1]:hostReply?hostReply[1]:deviceReply?deviceReply[1]:tokenHash;
    if(!devices.includes(deviceHash))return reply(403,'RELAY_AUTH_REQUIRED');
    if((host||hostReply)&&tokenHash!==env.HOST_TOKEN_SHA256)return reply(403,'RELAY_AUTH_REQUIRED');
    if(deviceReply&&tokenHash!==deviceHash)return reply(403,'RELAY_AUTH_REQUIRED');
    if(host||deviceWs){
      if(request.method!=='GET'||request.headers.get('Upgrade')?.toLowerCase()!=='websocket')return reply(426,'WEBSOCKET_REQUIRED');
      if(deviceWs&&request.headers.has('X-XiaoNuo-Rx-Batch')&&request.headers.get('X-XiaoNuo-Rx-Batch')!=='8')return reply(422,'RELAY_RX_BATCH_INVALID');
    }else if(hostReply){
      if(request.method!=='POST')return reply(405,'METHOD_NOT_ALLOWED');
    }else if(!['GET','HEAD'].includes(request.method)){
      return reply(405,'METHOD_NOT_ALLOWED');
    }
    // A digest selects a room; it cannot authenticate as either peer.
    return env.RELAY.get(env.RELAY.idFromName(deviceHash)).fetch(request);
  }
};

export class DeviceRelay extends DurableObject {
  sockets(role){return this.ctx.getWebSockets(role).filter(ws=>ws.readyState===1);}
  async fetch(request){
    const path=new URL(request.url).pathname;
    const upload=path.match(/^\/host\/[0-9a-f]{64}\/reply\/([0-9a-f-]{36})$/);
    const download=path.match(/^\/reply\/[0-9a-f]{64}\/([0-9a-f-]{36})$/);
    if(upload)return this.storeReply(request,upload[1]);
    if(download)return this.loadReply(request,download[1]);
    const host=path.startsWith('/host/');
    if(host){
      // The new computer process has no old heartbeat sequence or in-flight
      // audio state. Both peers must start a fresh authenticated session.
      // Mark every old socket first so its late close cannot kill the new host.
      for(const old of [...this.sockets('host'),...this.sockets('device')]){
        const attachment=old.deserializeAttachment()||{};
        attachment.replaced=true;old.serializeAttachment(attachment);
        try{old.close(1012,'HOST_REPLACED');}catch{}
      }
    }
    if(!host&&!this.sockets('host').length)return reply(503,'COMPUTER_OFFLINE');
    if(!host&&this.sockets('device').length){
      const devices=this.sockets('device');
      if(devices.some(socket=>Date.now()-(socket.deserializeAttachment()?.updated||0)<=DEVICE_STALE_MS)){
        return reply(409,'DEVICE_ALREADY_CONNECTED');
      }
      // Only an already authenticated device in the same room reaches here.
      // A silently lost socket must not keep its reconnect locked out forever.
      // Reset both halves so the computer rechecks the fresh device hello.
      for(const old of [...devices,...this.sockets('host')]){
        const attachment=old.deserializeAttachment()||{};
        attachment.replaced=true;old.serializeAttachment(attachment);
        try{old.close(1012,'STALE_DEVICE_RECONNECT');}catch{}
      }
      return reply(503,'RELAY_RECONNECTING');
    }
    const [client,server]=Object.values(new WebSocketPair());
    const role=host?'host':'device';
    this.ctx.acceptWebSocket(server,[role]);
    const batch=host&&['7','16'].includes(request.headers.get('X-XiaoNuo-Batch'))?Number(request.headers.get('X-XiaoNuo-Batch')):0;
    const receiveBatch=!host&&request.headers.get('X-XiaoNuo-Rx-Batch')==='8'?8:0;
    server.serializeAttachment({role,batch,receiveBatch,tokens:512,updated:Date.now(),
      authorization:host?undefined:request.headers.get('Authorization')});
    if(host){
      server.send(JSON.stringify({type:'relay_waiting',protocol:1,...(batch?{batch_frames:batch}:{})}));
    }else this.sockets('host')[0].send(JSON.stringify({type:'relay_open',protocol:1,authorization:request.headers.get('Authorization')}));
    return new Response(null,{status:101,webSocket:client});
  }
  async storeReply(request,id){
    const expected=(request.headers.get('X-XiaoNuo-SHA256')||'').toLowerCase();
    if(!hex.test(expected))return reply(422,'REPLY_HASH_REQUIRED');
    const data=await request.arrayBuffer(),length=data.byteLength;
    if(length<1||length>120*1024)return reply(413,'REPLY_TOO_LARGE');
    const declared=Number(request.headers.get('Content-Length')||0);
    if(declared&&declared!==length)return reply(422,'REPLY_LENGTH_MISMATCH');
    const actual=Array.from(new Uint8Array(await crypto.subtle.digest('SHA-256',data)),n=>n.toString(16).padStart(2,'0')).join('');
    if(actual!==expected)return reply(422,'REPLY_HASH_MISMATCH');
    const expires=Date.now()+5*60*1000;
    await this.ctx.storage.put('reply:'+id,{data,sha:expected,expires,bytes:length});
    await this.ctx.storage.setAlarm(expires);
    return new Response(null,{status:201,headers});
  }
  async loadReply(request,id){
    const record=await this.ctx.storage.get('reply:'+id);
    if(!record||record.expires<Date.now())return reply(404,'REPLY_NOT_FOUND');
    const data=record.data,total=data.byteLength;
    const range=request.headers.get('Range');
    let start=0,end=total-1,status=200;
    if(range){
      const match=/^bytes=(\d*)-(\d*)$/.exec(range.trim());
      if(!match)return reply(416,'RANGE_INVALID');
      if(match[1]===''){
        const suffix=Number(match[2]);
        if(!suffix)return reply(416,'RANGE_INVALID');
        start=Math.max(0,total-suffix);
      }else{
        start=Number(match[1]);
        if(match[2]!=='')end=Number(match[2]);
      }
      if(!Number.isInteger(start)||!Number.isInteger(end)||start<0||start>end||start>=total)return reply(416,'RANGE_INVALID');
      end=Math.min(end,total-1);status=206;
    }
    const body=request.method==='HEAD'?null:data.slice(start,end+1);
    const out={'Cache-Control':'no-store','Content-Type':'application/octet-stream',
      'Accept-Ranges':'bytes','ETag':'"'+record.sha+'"','Content-Length':String(body?body.byteLength:total)};
    if(status===206)out['Content-Range']=`bytes ${start}-${end}/${total}`;
    return new Response(body,{status,headers:out});
  }
  async alarm(){
    const now=Date.now(),all=await this.ctx.storage.list({prefix:'reply:'});
    let next=Infinity;
    for(const [key,record] of all){
      if(!record||record.expires<=now)await this.ctx.storage.delete(key);
      else next=Math.min(next,record.expires);
    }
    if(next<Infinity)await this.ctx.storage.setAlarm(next);
  }
  stop(ws,code,reason){
    const role=ws.deserializeAttachment()?.role;
    for(const socket of [ws,...this.sockets(role==='host'?'device':'host')]){
      try{socket.close(code,reason);}catch{}
    }
  }
  webSocketMessage(ws,message){
    const state=ws.deserializeAttachment(),host=state?.role==='host';
    if(state?.replaced)return;
    const size=typeof message==='string'?encoder.encode(message).length:message.byteLength;
    if(!state||!size||size>(host?(typeof message==='string'?8192:16422):1024)){this.stop(ws,1009,'RELAY_FRAME_LIMIT');return;}
    let packets=[message],batched=false;
    if(host&&typeof message!=='string'&&size>=4&&new Uint8Array(message,0,4).every((v,i)=>v===[88,78,66,49][i])){
      try{
        if(!state.batch)throw new Error();
        const view=new DataView(message),count=view.getUint16(4);let offset=6;packets=[];
        if(count<1||count>state.batch)throw new Error();
        for(let i=0;i<count;i++){
          if(offset+2>size)throw new Error();const length=view.getUint16(offset);offset+=2;
          if(length<=24||length>1024||offset+length>size)throw new Error();
          const packet=message.slice(offset,offset+length),bytes=new Uint8Array(packet,0,4);
          if(!bytes.every((v,j)=>v===[88,78,65,49][j]))throw new Error();
          packets.push(packet);offset+=length;
        }
        if(offset!==size)throw new Error();
        batched=true;
      }catch{this.stop(ws,1008,'RELAY_BATCH_INVALID');return;}
    }
    const now=Date.now();state.tokens=Math.min(512,state.tokens+Math.max(0,now-state.updated)*.128);state.updated=now;
    if(state.tokens<packets.length){this.stop(ws,1008,'RELAY_RATE_LIMIT');return;}
    state.tokens-=packets.length;ws.serializeAttachment(state);
    const peer=this.sockets(host?'device':'host')[0];
    if(!peer){this.stop(ws,1012,'PEER_DISCONNECTED');return;}
    try{
      if(host&&batched&&peer.deserializeAttachment()?.receiveBatch===8){
        for(let offset=0;offset<packets.length;offset+=8)peer.send(packFrames(packets.slice(offset,offset+8)));
      }else for(const packet of packets)peer.send(packet);
    }catch{this.stop(ws,1012,'RELAY_SEND_FAILED');}
  }
  webSocketClose(ws,code){
    if(ws.deserializeAttachment()?.replaced)return;
    this.stop(ws,[1000,1008,1009,1012].includes(code)?code:1012,'PEER_DISCONNECTED');
  }
  webSocketError(ws){
    if(ws.deserializeAttachment()?.replaced)return;
    this.stop(ws,1012,'RELAY_SOCKET_ERROR');
  }
}
