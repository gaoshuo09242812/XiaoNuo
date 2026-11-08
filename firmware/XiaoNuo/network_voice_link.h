#include "face_renderer.h"
#pragma once
#include <Arduino.h>
#include <WiFi.h>
#include <WiFiUdp.h>
#include <WebSocketsClient.h>
#include <ArduinoJson.h>
#include <time.h>
#include <esp_system.h>
#include <mbedtls/sha256.h>
#include "network_endpoint.h"
#include "device_binding.h"
#include "network_audio_wire.h"
#include "adpcm_codec.h"
#include "wake_upload_policy.h"

// Numeric transport diagnostics only; never expose HTTP headers or credentials.
class XiaoNuoDiagnosticSocket : public WebSocketsClient {
 public:
  int tlsError() { char ignored[1]={};return _client.ssl?_client.ssl->lastError(ignored,sizeof(ignored)):0; }
  bool connectVerified(bool (*shouldStop)(void*)=nullptr,void* context=nullptr) {
    if(!_CA_cert||!_CA_cert[0]||_client.tcp)return false;
    IPAddress peers[4];unsigned count=0;
    auto add=[&](IPAddress ip){
      if(count>=4||!usable(ip))return;
      for(unsigned i=0;i<count;i++)if(peers[i]==ip)return;
      peers[count++]=ip;
    };
    add(lastGood);
    if(_host==XiaoNuoEndpoint::HOST&&_host.endsWith(".pages.dev")&&_port==8443){
      for(const char* value:XiaoNuoEndpoint::RECOVERY_PEERS){IPAddress ip;if(ip.fromString(value))add(ip);}
    }
    IPAddress answers[4];unsigned found=resolveAll(answers);
    for(unsigned i=0;i<found;i++)add(answers[i]);
    if(!found){IPAddress one;if(WiFi.hostByName(_host.c_str(),one)==1)add(one);}
    for(unsigned i=0;i<count;i++)if(peers[i]==lastGood){IPAddress first=peers[0];peers[0]=peers[i];peers[i]=first;break;}
    for(unsigned i=0;i<count;i++) {
      if(shouldStop&&shouldStop(context))return false;
      _client.ssl=new WEBSOCKETS_NETWORK_SSL_CLASS();_client.tcp=_client.ssl;
      if(!_client.ssl)return false;
      _client.ssl->setCACert(_CA_cert);_client.ssl->setHandshakeTimeout(30);_client.ssl->setConnectionTimeout(10000);
      // The IP+SNI overload uses the stream timeout for TCP connect in this SDK.
      _client.ssl->setTimeout(10000);
      static const char* protocols[]={"http/1.1",nullptr};_client.ssl->setAlpnProtocols(protocols);
      uint32_t start=millis();
      // Explicit IP selects a DNS answer; hostname remains the TLS SNI/verification target.
      bool ok=_client.ssl->connect(peers[i],_port,_host.c_str(),_CA_cert,nullptr,nullptr);
      int error=ok?0:tlsError();
      Serial.printf("{\"event\":\"network_peer_attempt\",\"peer_ipv4\":\"%s\",\"connected\":%s,\"elapsed_ms\":%lu,\"tls_code\":%d}\n",peers[i].toString().c_str(),ok?"true":"false",(unsigned long)(millis()-start),error);
      if(ok){lastGood=peers[i];connectedCb();headerPending=_client.tcp!=nullptr;headerStarted=_lastHeaderSent;return _client.tcp!=nullptr;}
      _client.ssl->stop();delete _client.ssl;_client.ssl=nullptr;_client.tcp=nullptr;
    }
    return false;
  }
  void poll() {
    if(headerPending&&_client.tcp&&(_client.status==WSC_HEADER||_client.status==WSC_BODY)) {
      uint32_t now=millis(),elapsed=now-headerStarted;
      // Keep this adaptation local to our socket, without editing the shared
      // WebSockets library's fixed five-second upgrade-response timeout.
      _lastHeaderSent=elapsed<20000?now:now-WEBSOCKETS_TCP_TIMEOUT-1;
      if(elapsed>=20000)Serial.printf("{\"event\":\"network_upgrade_timeout\",\"elapsed_ms\":%lu}\n",(unsigned long)elapsed);
    }else headerPending=false;
    WebSocketsClient::loop();
  }
 private:
  IPAddress lastGood;
  bool headerPending=false;uint32_t headerStarted=0;
  static bool usable(IPAddress ip){return ip[0]&&ip[0]!=127&&ip[0]<224&&ip[0]!=10&&!(ip[0]==192&&ip[1]==168)&&!(ip[0]==172&&ip[1]>=16&&ip[1]<=31)&&!(ip[0]==169&&ip[1]==254);}
  static uint16_t word(const uint8_t* p){return (uint16_t(p[0])<<8)|p[1];}
  static bool skipName(const uint8_t* data,size_t size,size_t& offset){
    unsigned labels=0;
    while(offset<size&&++labels<=128){uint8_t length=data[offset++];if(!length)return true;
      if((length&0xc0)==0xc0){if(offset>=size)return false;unsigned target=((length&63)<<8)|data[offset++];return target<size;}
      if(length>63||offset+length>size)return false;offset+=length;
    }return false;
  }
  unsigned resolveAll(IPAddress* peers){
    uint8_t query[256]={},answer[512];uint16_t id=uint16_t(esp_random());query[0]=id>>8;query[1]=id;query[2]=1;query[5]=1;size_t used=12;
    const char* begin=_host.c_str();const char* label=begin;
    for(const char* p=begin;;p++)if(*p=='.'||!*p){size_t n=p-label;if(!n||n>63||used+n+6>sizeof(query))return 0;query[used++]=n;memcpy(query+used,label,n);used+=n;if(!*p)break;label=p+1;}
    query[used++]=0;query[used++]=0;query[used++]=1;query[used++]=0;query[used++]=1;
    WiFiUDP dns;IPAddress server=WiFi.dnsIP();if(!dns.begin(0)||!dns.beginPacket(server,53))return 0;dns.write(query,used);if(!dns.endPacket()){dns.stop();return 0;}
    uint32_t started=millis();int bytes=0;
    while((uint32_t)(millis()-started)<2000){int size=dns.parsePacket();if(size>0){if(dns.remoteIP()!=server||dns.remotePort()!=53||size>int(sizeof(answer))){while(dns.available())dns.read();continue;}bytes=dns.read(answer,sizeof(answer));break;}vTaskDelay(pdMS_TO_TICKS(10));}
    dns.stop();if(bytes<12||word(answer)!=id||(answer[2]&0x80)==0||(answer[2]&2)||(answer[3]&15)||word(answer+4)!=1)return 0;
    size_t offset=12;if(!skipName(answer,bytes,offset)||offset+4>size_t(bytes))return 0;offset+=4;unsigned count=0,records=word(answer+6);if(records>32)return 0;
    for(unsigned i=0;i<records;i++){
      if(!skipName(answer,bytes,offset)||offset+10>size_t(bytes))return 0;
      unsigned type=word(answer+offset),cls=word(answer+offset+2),length=word(answer+offset+8);offset+=10;if(offset+length>size_t(bytes))return 0;
      if(type==1&&cls==1&&length==4&&count<4){IPAddress ip(answer[offset],answer[offset+1],answer[offset+2],answer[offset+3]);bool duplicate=false;for(unsigned j=0;j<count;j++)if(peers[j]==ip)duplicate=true;if(usable(ip)&&!duplicate)peers[count++]=ip;}
      offset+=length;
    }return count;
  }

};

// The worker owns TLS/WebSocket; the Arduino task owns NVS, I2S and OLED.
// Never allocate a whole recording or print credentials/audio to Serial.
class NetworkLink {
 public:
  enum Kind:uint8_t { BEGIN=1,PCM=2,END=3,PLAYED=4,REPLY_BEGIN=5,REPLY_PCM=6,REPLY_END=7,RESUME=8,PROBE=9,REMINDER=10 };
  struct Packet { uint32_t epoch=0,count=0;uint16_t size=0;uint8_t kind=0,id[16]={},data[1000]={}; };
  struct View { const char* state="DISABLED";bool ready=false,audio=false,turn=false,record=false,connected=false;
                bool computerWake=false,cloudAi=false,pcQuiet=false,trimUpload=false,reminders=false;uint8_t expression=0;uint32_t expressionAt=0;uint32_t epoch=1,pongs=0,followup=0; };
  // 42 packets hold one complete short reply (41 PCM frames plus control).
  // This lets playback start with the whole sentence buffered on high-RTT links.
  static constexpr unsigned RX_DEPTH=42,TX_DEPTH=4;
  static_assert(sizeof(Packet)<=1056,"Audio queue packet exceeds its memory budget");
  static_assert(RX_DEPTH>=2*16+2,"Reply buffer needs two negotiated windows and control markers");
  void begin(DeviceBinding& credentials,const char* hardware,const char* version) {
    if(worker)return;
    binding=&credentials;strlcpy(hardwareId,hardware,sizeof(hardwareId));strlcpy(firmware,version,sizeof(firmware));
    if(!configured())return;
    tx=xQueueCreate(TX_DEPTH,sizeof(Packet));rx=xQueueCreate(RX_DEPTH,sizeof(Packet));
    if(!tx||!rx||ESP.getFreeHeap()<90000){allocationFailed();return;}
    // Keep application TLS/ADPCM work off the WiFi/LwIP core. The Arduino
    // task yields during capture/playback; TLS remains owned by this worker.
    if(xTaskCreatePinnedToCore(taskEntry,"xnuo-network",12288,this,1,&worker,1)!=pdPASS)allocationFailed();
  }
  void tick(bool usbBusy,bool quiet) {
    if(!worker)return;
    String nextId=binding->id();uint32_t generation=binding->generation();
    String header=generation!=mainGeneration?binding->authorizationHeader():String();
    bool online=WiFi.status()==WL_CONNECTED;
    portENTER_CRITICAL(&mux);available=online;blocked=usbBusy;quietRequested=quiet;
    if(generation!=mainGeneration){strlcpy(credentialsId,nextId.c_str(),sizeof(credentialsId));memset(credentialsHeader,0,sizeof(credentialsHeader));strlcpy(credentialsHeader,header.c_str(),sizeof(credentialsHeader));credentialRevision++;}
    portEXIT_CRITICAL(&mux);mainId=nextId;mainGeneration=generation;
  }
  View view(){portENTER_CRITICAL(&mux);View result=published;portEXIT_CRITICAL(&mux);return result;}
  void suspend(bool on){portENTER_CRITICAL(&mux);suspended=on;pauseAcknowledged=false;portEXIT_CRITICAL(&mux);}
  bool shutdown() {
    suspend(true);
    uint32_t start=millis();
    while(worker) {
      portENTER_CRITICAL(&mux);bool closed=pauseAcknowledged;portEXIT_CRITICAL(&mux);
      if(closed)break;
      if((uint32_t)(millis()-start)>45000){suspend(false);return false;}
      vTaskDelay(pdMS_TO_TICKS(10));
    }
    // Only delete a worker after it has closed TLS in its own task. Never
    // kill a task inside connect()/TLS/lwIP and then reuse its resources.
    if(worker){vTaskDelete(worker);worker=nullptr;}
    mbedtls_sha256_free(&uploadSha);mbedtls_sha256_free(&replySha);
    if(tx){vQueueDelete(tx);tx=nullptr;}
    if(rx){vQueueDelete(rx);rx=nullptr;}
    socket.disconnect();
    portENTER_CRITICAL(&mux);published.ready=false;published.connected=false;published.turn=false;published.expression=0;published.expressionAt=0;published.state="SHUTDOWN";portEXIT_CRITICAL(&mux);
    return true;
  }
  void restart(){suspend(false);if(!worker&&binding)begin(*binding,hardwareId,firmware);}
  bool active(){return view().turn;}
  bool replyCompressed()const{return compressedReply;}
  uint32_t replySampleCount()const{return replyTargetSamples;}
  XnuAdpcmState replyInitialState()const{return replyInitialStateValue;}
  bool requestTalk(uint8_t seconds,bool wake=false,bool compressed=false,int predictor=0,int stepIndex=0) {
    if(!tx||seconds<2||seconds>6||predictor<-32768||predictor>32767||stepIndex<0||stepIndex>88)return false;Packet item;item.kind=BEGIN;item.count=seconds;item.size=(wake?1:0)|(compressed?2:0);
    portENTER_CRITICAL(&mux);bool allowed=published.ready&&published.audio&&!published.turn&&!quietRequested&&!blocked&&(!wake||published.computerWake);
    if(allowed){item.epoch=published.epoch;published.turn=true;published.state="REQUESTED";}
    portEXIT_CRITICAL(&mux);
    uint32_t queuedAt=millis();memcpy(item.data,&queuedAt,sizeof(queuedAt));int16_t initial=(int16_t)predictor;memcpy(item.data+4,&initial,2);item.data[6]=(uint8_t)stepIndex;
    if(!allowed)return false;if(xQueueSend(tx,&item,0)!=pdTRUE){cancel();return false;}return true;
  }
  bool requestReminder(){return requestReplyProbe(0,true);}
  bool requestReplyProbe(uint8_t seconds,bool reminder=false) {
    if(!tx||(reminder?seconds!=0:(seconds<1||seconds>45)))return false;
    Packet item;item.kind=reminder?REMINDER:PROBE;item.count=seconds;
    portENTER_CRITICAL(&mux);
    bool allowed=published.ready&&published.audio&&!published.turn&&!quietRequested&&!blocked&&(!reminder||(published.reminders&&!published.pcQuiet));
    if(allowed){item.epoch=published.epoch;published.turn=true;published.state="REQUESTED";}
    portEXIT_CRITICAL(&mux);
    if(!allowed)return false;
    if(xQueueSend(tx,&item,0)!=pdTRUE){cancel();return false;}return true;
  }
  bool queuePcm(const uint8_t* data,size_t length,bool failOnFull=true) {
    View current=view();if(!current.record||!length||length>1000||length%2)return false;
    Packet item;item.kind=PCM;item.epoch=current.epoch;item.size=length;memcpy(item.data,data,length);
    if(xQueueSend(tx,&item,0)!=pdTRUE){if(failOnFull)cancel();return false;}return true;
  }
  bool finishUpload(uint32_t samples=0) {
    View current=view();Packet item;item.kind=END;item.epoch=current.epoch;
    if(samples&&!current.trimUpload)return false;item.count=samples;
    if(!current.record||xQueueSend(tx,&item,0)!=pdTRUE){cancel();return false;}return true;
  }
  void cancel() {
    portENTER_CRITICAL(&mux);cancelRevision++;published.epoch++;published.turn=false;published.record=false;
    published.expression=0;published.expressionAt=0;published.ready=false;published.state="STOPPING";portEXIT_CRITICAL(&mux);
  }
  bool receive(Packet& item){return rx&&xQueueReceive(rx,&item,0)==pdTRUE;}
  unsigned queued()const{return rx?uxQueueMessagesWaiting(rx):0;}
  unsigned sendSpace()const{return tx?uxQueueSpacesAvailable(tx):0;}
  bool played(const uint8_t id[16],uint32_t samples,uint32_t epoch) {
    Packet item;item.kind=PLAYED;item.epoch=epoch;item.count=samples;memcpy(item.id,id,16);
    if(!tx||xQueueSend(tx,&item,0)!=pdTRUE){cancel();return false;}return true;
  }
  bool resume(const char* replyId,uint32_t playedSamples) {
    if(!tx||!replyId||strlen(replyId)!=36)return false;
    Packet item;item.kind=RESUME;item.count=playedSamples;strlcpy((char*)item.data,replyId,sizeof(item.data));
    portENTER_CRITICAL(&mux);bool allowed=published.ready&&!published.turn;
    if(allowed){item.epoch=published.epoch;published.turn=true;published.state="RESUMING";}
    portEXIT_CRITICAL(&mux);
    if(!allowed)return false;
    turn=true;turnStarted=millis();reply=replyEnded=resultReceived=ackPending=false;replyTarget=0;replyBytes=0;
    if(xQueueSend(tx,&item,0)!=pdTRUE){cancel();return false;}return true;
  }
  void status() {
    View current=view();Serial.printf("{\"event\":\"network_status\",\"configured\":%s,\"connected\":%s,\"ready\":%s,\"state\":\"%s\",\"pong_count\":%lu,\"audio_upload_enabled\":%s,\"turn_active\":%s,\"protocol\":5,\"tls_verified\":%s,\"queue_bytes\":%u}\n",
      configured()?"true":"false",current.connected?"true":"false",current.ready?"true":"false",current.state,(unsigned long)current.pongs,
      current.audio?"true":"false",current.turn?"true":"false",current.ready?"true":"false",(unsigned)((RX_DEPTH+TX_DEPTH)*sizeof(Packet)));
  }
 private:
  XiaoNuoDiagnosticSocket socket;int lastTlsError=0;QueueHandle_t tx=nullptr,rx=nullptr;TaskHandle_t worker=nullptr;
  DeviceBinding* binding=nullptr;String mainId,authorization,pairedId;
  uint32_t mainGeneration=0xffffffff;
  char hardwareId[19]={},firmware[40]={},credentialsId[37]={},credentialsHeader[96]={};
  portMUX_TYPE mux=portMUX_INITIALIZER_UNLOCKED;View published;volatile bool suspended=false;
  bool pauseAcknowledged=false;
  bool available=false,blocked=true,quietRequested=false;uint32_t credentialRevision=0,cancelRevision=0,seenCredentials=0,seenCancel=0;
  bool started=false,connected=false,ready=false,awaiting=false,clockStarted=false;
  bool resumeTurn=false;bool compressedTurn=false;int uploadPredictor=0,uploadIndex=0;
  bool reminderTurn=false;
  bool compressedReply=false;uint32_t replyTargetSamples=0;XnuAdpcmState replyInitialStateValue={0,0};
  uint8_t pendingExpression=0;
  bool turn=false,uploading=false,resultReceived=false,reply=false,replyEnded=false,ackPending=false,wakeTurn=false,trimTurn=false;
  uint32_t epoch=1,connectedAt=0,lastPing=0,nextPing=1,retryAt=0,turnStarted=0;
  uint32_t uploadReadyAt=0,uploadMaxSendMs=0;
  uint32_t windowOpenedAt=0,windowFirstAt=0,windowLastAt=0,ackPendingAt=0,windowReceived=0;
  uint32_t uploadTarget=0,uploadBytes=0,uploadSequence=0,replyTarget=0,replyBytes=0,replySequence=0,segments=0,windowFrames=4;
  char requestId[37]={},streamText[37]={};uint8_t stream[16]={},replyId[16]={};
  mbedtls_sha256_context uploadSha,replySha;
  static bool configured(){return XiaoNuoEndpoint::HOST[0]&&XiaoNuoEndpoint::CA_PEM[0];}
  static void taskEntry(void* self){static_cast<NetworkLink*>(self)->run();}
  static bool shouldSuspend(void* context){
    NetworkLink* self=static_cast<NetworkLink*>(context);
    portENTER_CRITICAL(&self->mux);bool result=self->suspended;portEXIT_CRITICAL(&self->mux);return result;
  }
  void allocationFailed(){if(tx)vQueueDelete(tx);if(rx)vQueueDelete(rx);tx=rx=nullptr;worker=nullptr;published.state="NO_MEMORY";}
  void publish(const char* state) {
    portENTER_CRITICAL(&mux);published.state=state;published.connected=connected;published.ready=ready;published.turn=turn;published.record=uploading;published.trimUpload=uploading&&trimTurn;portEXIT_CRITICAL(&mux);
  }
  void disconnect(const char* reason) {
    portENTER_CRITICAL(&mux);published.epoch++;epoch=published.epoch;
    published.ready=published.record=published.turn=published.audio=published.connected=false;published.state=reason;
    published.computerWake=published.cloudAi=published.pcQuiet=published.reminders=false;published.followup=0;published.expression=0;published.expressionAt=0;pendingExpression=0;
    portEXIT_CRITICAL(&mux);
    turn=uploading=resultReceived=reply=replyEnded=ackPending=wakeTurn=compressedReply=trimTurn=false;replyTargetSamples=0;connected=ready=awaiting=false;
    reminderTurn=false;
    if(started){started=false;socket.disconnect();}authorization="";publish(reason);retryAt=millis()+15000;
  }
  bool json(JsonDocument& message){String text;serializeJson(message,text);if(!socket.sendTXT(text)){disconnect("SEND_FAILED");return false;}return true;}
  bool queueReply(Packet& item){item.epoch=epoch;if(xQueueSend(rx,&item,0)!=pdTRUE){disconnect("REPLY_QUEUE_FULL");return false;}return true;}
  void run() {
    mbedtls_sha256_init(&uploadSha);mbedtls_sha256_init(&replySha);
    socket.onEvent([this](WStype_t type,uint8_t* bytes,size_t length){event(type,bytes,length);});socket.setReconnectInterval(15000);
    for(;;){
      bool online,busy,quiet,paused;uint32_t rev,cancel;char id[37],header[96];
      portENTER_CRITICAL(&mux);online=available;busy=blocked;quiet=quietRequested;paused=suspended;rev=credentialRevision;cancel=cancelRevision;
      strlcpy(id,credentialsId,sizeof(id));strlcpy(header,credentialsHeader,sizeof(header));portEXIT_CRITICAL(&mux);
      if(paused){
        if(started||turn)disconnect("SUSPENDED");else publish("SUSPENDED");
        portENTER_CRITICAL(&mux);pauseAcknowledged=true;portEXIT_CRITICAL(&mux);
        // Once acknowledged, touch no sockets, queues or mutexes until resumed.
        // shutdown() may delete this task while it is parked here.
        while(suspended)vTaskDelay(pdMS_TO_TICKS(20));
        continue;
      }
      if(cancel!=seenCancel){seenCancel=cancel;disconnect("CANCELLED");}
      if(rev!=seenCredentials){seenCredentials=rev;disconnect("BINDING_CHANGED");retryAt=0;}
      if(!id[0]||!online||busy||quiet){const char* why=!id[0]?"UNBOUND":!online?"WIFI_OFFLINE":quiet?"QUIET":"USB_BUSY";
        if(started||turn)disconnect(why);else publish(why);vTaskDelay(pdMS_TO_TICKS(20));continue;}
      if(time(nullptr)<1704067200){if(!clockStarted){configTime(0,0,"ntp.aliyun.com","time.cloudflare.com");clockStarted=true;}publish("CLOCK_WAIT");vTaskDelay(pdMS_TO_TICKS(100));continue;}
      if(!started){if(retryAt&&(int32_t)(retryAt-millis())>0){vTaskDelay(pdMS_TO_TICKS(20));continue;}
        authorization=header;pairedId=id;socket.beginSslWithCA(XiaoNuoEndpoint::HOST,XiaoNuoEndpoint::PORT,"/api/v1/device-link/ws",XiaoNuoEndpoint::CA_PEM,"");
        socket.setExtraHeaders(authorization.c_str());started=true;publish("CONNECTING");if(!socket.connectVerified(shouldSuspend,this)){disconnect("CONNECT_FAILED");continue;}}
      socket.poll();int error=socket.tlsError();if(error<0&&error!=lastTlsError){lastTlsError=error;Serial.printf("{\"event\":\"network_transport_error\",\"tls_code\":%d}\n",error);}uint32_t now=millis();
      if(connected&&!ready&&(uint32_t)(now-connectedAt)>20000){disconnect("HELLO_TIMEOUT");continue;}
      if(ready){
        if(ackPending) {
          unsigned waiting=uxQueueMessagesWaiting(rx);
          unsigned remaining=replyTarget>replyBytes?(replyTarget-replyBytes+999)/1000:0;
          // Reserve a queue slot for the verified end marker after the final
          // data window; otherwise a full queue could reject reply_end.
          unsigned need=remaining?min((unsigned)windowFrames,remaining):1;
          if(RX_DEPTH-waiting>=need) {
            char replyText[37];XiaoNuoWire::uuidText(replyId,replyText);JsonDocument ack;
            ack["type"]="reply_ack";ack["reply_id"]=replyText;ack["next_sequence"]=replySequence;ack["received_bytes"]=replyBytes;
            uint32_t sendingAt=millis();ackPending=false;if(!json(ack))continue;
            uint32_t sentAt=millis();
            Serial.printf("{\"event\":\"reply_ack_sent\",\"next_sequence\":%lu,\"received_bytes\":%lu,\"queued\":%u,\"window\":%u,\"first_frame_wait_ms\":%lu,\"frame_span_ms\":%lu,\"ack_queue_wait_ms\":%lu,\"ack_send_ms\":%lu,\"window_received\":%lu}\n",(unsigned long)replySequence,(unsigned long)replyBytes,waiting,windowFrames,(unsigned long)(windowFirstAt-windowOpenedAt),(unsigned long)(windowLastAt-windowFirstAt),(unsigned long)(sendingAt-ackPendingAt),(unsigned long)(sentAt-sendingAt),(unsigned long)windowReceived);
            windowOpenedAt=sentAt;windowReceived=0;
          }
        }
        if(awaiting&&(uint32_t)(now-lastPing)>20000){disconnect("PONG_TIMEOUT");continue;}
        if(!awaiting&&(uint32_t)(now-lastPing)>=1000){JsonDocument ping;ping["type"]="ping";ping["sequence"]=nextPing;if(!json(ping))continue;lastPing=now;awaiting=true;}
        if(turn&&(uint32_t)(now-turnStarted)>600000){disconnect("TURN_TIMEOUT");continue;}
        Packet item;for(unsigned i=0;i<4&&ready&&xQueueReceive(tx,&item,0)==pdTRUE;i++){View current=view();if(item.epoch!=current.epoch)continue;epoch=current.epoch;outgoing(item);}
      }vTaskDelay(pdMS_TO_TICKS(2));
    }
  }
  void outgoing(const Packet& item) {
    if(item.kind==BEGIN||item.kind==PROBE||item.kind==RESUME||item.kind==REMINDER)resumeTurn=item.kind==RESUME;
    if(item.kind==RESUME){
      char id[37]={};memcpy(id,item.data,36);
      JsonDocument message;message["type"]="reply_resume";message["reply_id"]=id;message["played_samples"]=item.count;
      Serial.printf("{\"event\":\"reply_resume_sent\",\"played_samples\":%lu}\n",(unsigned long)item.count);
      if(!json(message)){disconnect("RESUME_SEND_FAILED");return;}return;
    }
    if(item.kind==PROBE||item.kind==REMINDER){
      reminderTurn=item.kind==REMINDER;
      if(turn||(reminderTurn?item.count!=0:(item.count<1||item.count>45))){disconnect("TURN_BUSY");return;}
      esp_fill_random(stream,16);stream[6]=(stream[6]&15)|0x40;stream[8]=(stream[8]&63)|0x80;
      XiaoNuoWire::uuidText(stream,requestId);strlcpy(streamText,requestId,sizeof(streamText));
      turn=true;turnStarted=millis();segments=0;wakeTurn=false;uploading=false;
      uploadBytes=uploadTarget=uploadSequence=0;resultReceived=false;
      JsonDocument message;message["type"]=reminderTurn?"reminder_pull":"reply_probe";message["request_id"]=requestId;if(!reminderTurn)message["seconds"]=item.count;
      publish("PREPARING_REPLY");json(message);return;
    }
    if(item.kind==BEGIN){
      reminderTurn=false;
      if(turn||item.count<2||item.count>6){disconnect("TURN_BUSY");return;}
      uint8_t random[16];esp_fill_random(random,16);random[6]=(random[6]&15)|0x40;random[8]=(random[8]&63)|0x80;
      XiaoNuoWire::uuidText(random,requestId);turn=true;turnStarted=millis();segments=0;compressedTurn=(item.size&2)!=0;uploadTarget=item.count*(compressedTurn?8000:32000);uploadBytes=uploadSequence=0;resultReceived=false;
      wakeTurn=(item.size&1)!=0;trimTurn=false;JsonDocument begin;begin["type"]="audio_begin";begin["request_id"]=requestId;begin["seconds"]=item.count;
      begin["consent"]=wakeTurn?"owner-computer-wake":"owner-computer";if(wakeTurn)begin["trigger"]="wake";
      if(compressedTurn){int16_t initial;memcpy(&initial,item.data+4,2);uploadPredictor=initial;uploadIndex=item.data[6];begin["encoding"]="ima_adpcm4";begin["predictor"]=uploadPredictor;begin["step_index"]=uploadIndex;}
      uploadReadyAt=uploadMaxSendMs=0;publish("PREPARING");
      uint32_t queuedAt;memcpy(&queuedAt,item.data,sizeof(queuedAt));uint32_t sendStarted=millis();
      if(json(begin))Serial.printf("{\"event\":\"audio_request_sent\",\"dispatch_wait_ms\":%lu,\"send_ms\":%lu}\n",(unsigned long)(turnStarted-queuedAt),(unsigned long)(millis()-sendStarted));
    }else if(item.kind==PCM){
      if(!uploading||!item.size||item.size>1000||item.size%2||uploadBytes+item.size>uploadTarget){disconnect("UPLOAD_INVALID");return;}
      uint8_t frame[1024];XiaoNuoWire::header(frame,stream,uploadSequence);memcpy(frame+24,item.data,item.size);
      uint32_t sendStarted=millis();if(!socket.sendBIN(frame,item.size+24)){disconnect("SEND_FAILED");return;}
      uploadMaxSendMs=max(uploadMaxSendMs,(uint32_t)(millis()-sendStarted));
      if(mbedtls_sha256_update(&uploadSha,item.data,item.size)){disconnect("HASH_FAILED");return;}uploadBytes+=item.size;uploadSequence++;
      if(uploadSequence%4==0)Serial.printf("{\"event\":\"audio_upload_progress\",\"frames\":%lu,\"bytes\":%lu,\"elapsed_ms\":%lu,\"max_send_ms\":%lu}\n",(unsigned long)uploadSequence,(unsigned long)uploadBytes,(unsigned long)(millis()-uploadReadyAt),(unsigned long)uploadMaxSendMs);
    }else if(item.kind==END){
      bool trimmed=item.count!=0;
      if(!uploading||(trimmed?!xnu_wake_trim_valid(uploadTarget,uploadBytes,item.count,wakeTurn,compressedTurn,trimTurn):uploadBytes!=uploadTarget)){disconnect("UPLOAD_INCOMPLETE");return;}
      if(trimmed)uploadTarget=uploadBytes;
      uint8_t digest[32];char hash[65];if(mbedtls_sha256_finish(&uploadSha,digest)){disconnect("HASH_FAILED");return;}XiaoNuoWire::digestText(digest,hash);
      uploading=false;publish("RECOGNIZING");JsonDocument end;end["type"]="audio_end";end["stream_id"]=streamText;end["sha256"]=hash;if(trimmed)end["samples"]=item.count;
      if(json(end))Serial.printf("{\"event\":\"audio_upload_finished\",\"samples\":%lu,\"wire_bytes\":%lu,\"trim_end\":%s}\n",(unsigned long)(compressedTurn?uploadBytes*2:uploadBytes/2),(unsigned long)uploadBytes,trimmed?"true":"false");
    }else if(item.kind==PLAYED){
      if(!reply||!replyEnded||memcmp(item.id,replyId,16)||item.count!=replyTargetSamples){disconnect("PLAYBACK_INVALID");return;}
      char id[37];XiaoNuoWire::uuidText(replyId,id);JsonDocument played;played["type"]="reply_played";played["reply_id"]=id;played["completed"]=true;played["samples"]=item.count;played["gain_divisor"]=2;
      reply=replyEnded=false;segments++;publish("PREPARING_REPLY");json(played);
    }else disconnect("UPLOAD_INVALID");
  }
  bool currentStream(JsonDocument& message){return message["stream_id"].is<const char*>()&&strcmp(message["stream_id"].as<const char*>(),streamText)==0;}
  void event(WStype_t type,uint8_t* bytes,size_t length) {
    if(type==WStype_DISCONNECTED){
      unsigned reason=0,http=0;
      if(length==15&&!memcmp(bytes,"Connection lost",15))reason=1;
      else if(length==22&&!memcmp(bytes,"TCP connection cleanup",22))reason=2;
      else if(length==23&&!memcmp(bytes,"Header response timeout",23))reason=3;
      else if(length>=26&&!memcmp(bytes,"WebSocket handshake failed",26))reason=4;
      // The library can embed the status in "WebSocket handshake failed -
      // HTTP NNN". Never log the reason text or any HTTP header/credential.
      for(size_t i=0;bytes&&i+8<=length;i++)if(!memcmp(bytes+i,"HTTP ",5)&&
          bytes[i+5]>='0'&&bytes[i+5]<='9'&&bytes[i+6]>='0'&&bytes[i+6]<='9'&&bytes[i+7]>='0'&&bytes[i+7]<='9'){
        http=(bytes[i+5]-'0')*100+(bytes[i+6]-'0')*10+bytes[i+7]-'0';break;
      }
      Serial.printf("{\"event\":\"network_disconnect\",\"reason_code\":%u}\n",reason);
      if(http)Serial.printf("{\"event\":\"network_transport_error\",\"http_status\":%u}\n",http);
      if(started){started=false;disconnect("RECONNECTING");}return;
    }
    if(type==WStype_CONNECTED){connected=true;ready=awaiting=false;nextPing=1;connectedAt=millis();publish("AUTHENTICATING");
      JsonDocument hello;hello["type"]="hello";hello["protocol"]=5;hello["id"]=pairedId;hello["hardware_id"]=hardwareId;hello["firmware"]=firmware;hello["capabilities"]["reply_buffered"]=true;hello["capabilities"]["reply_reminders"]=true;json(hello);return;}
    if(type==WStype_BIN){binary(bytes,length);return;}
    if(type==WStype_PING||type==WStype_PONG)return;
    if(type!=WStype_TEXT){if(type==WStype_ERROR||type>=WStype_FRAGMENT_TEXT_START)disconnect("INVALID_FRAME");return;}
    if(!connected||length>8192){disconnect("INVALID_FRAME");return;}JsonDocument message;
    if(deserializeJson(message,bytes,length)||!message.is<JsonObject>()){disconnect("INVALID_FRAME");return;}const char* kind=message["type"]|"";
    if(!ready){
      if(strcmp(kind,"ready")||!message["protocol"].is<int>()||message["protocol"].as<int>()!=5||!message["audio_upload_enabled"].is<bool>()||
         message["heartbeat_seconds"].as<int>()!=10||!message["commands"].is<JsonArray>()||message["commands"].size()){disconnect("INVALID_READY");return;}
      if(!message["reply_buffered_enabled"].is<bool>()||!message["reply_buffered_enabled"].as<bool>()){disconnect("BUFFERED_UNSUPPORTED");return;}
      if(!message["reply_reminders_enabled"].isNull()&&(!message["reply_reminders_enabled"].is<bool>()||!message["reminder_poll_seconds"].is<int>()||message["reminder_poll_seconds"].as<int>()!=30)){disconnect("INVALID_READY");return;}
      ready=true;lastPing=millis();portENTER_CRITICAL(&mux);published.audio=message["audio_upload_enabled"].as<bool>();
      published.computerWake=message["computer_wake_enabled"].is<bool>()&&message["computer_wake_enabled"].as<bool>();
      published.reminders=message["reply_reminders_enabled"]|false;
      published.cloudAi=strcmp(message["chat_provider"]|"","deepseek")==0;published.pcQuiet=message["quiet"]|false;
      portEXIT_CRITICAL(&mux);publish("READY");return;
    }
    if(!strcmp(kind,"pong")&&awaiting&&message["sequence"].is<uint32_t>()&&message["sequence"].as<uint32_t>()==nextPing&&nextPing<1000000){awaiting=false;nextPing++;portENTER_CRITICAL(&mux);published.pongs++;portEXIT_CRITICAL(&mux);return;}
    if(!turn){disconnect("UNSOLICITED_REPLY");return;}
    if(!strcmp(kind,"reminder_empty")&&reminderTurn&&!resultReceived&&!reply&&message.size()==2&&message["request_id"].is<const char*>()&&!strcmp(message["request_id"].as<const char*>(),requestId)){
      turn=reminderTurn=false;publish("READY");return;
    }
    if(!strcmp(kind,"audio_ready")){
      if(uploading||uploadBytes||strcmp(message["request_id"]|"",requestId)||strcmp(message["state"]|"","receiving")||message["expected_bytes"].as<uint32_t>()!=uploadTarget||
         message["sample_rate"].as<int>()!=16000||message["channels"].as<int>()!=1||strcmp(message["format"]|"",compressedTurn?"ima_adpcm4":"s16le")||
         (compressedTurn&&(!message["predictor"].is<int>()||message["predictor"].as<int>()!=uploadPredictor||!message["step_index"].is<int>()||message["step_index"].as<int>()!=uploadIndex))||message["max_frame_bytes"].as<int>()!=1024||
         !XiaoNuoWire::uuid(message["stream_id"]|"",stream)){disconnect("INVALID_UPLOAD_READY");return;}
      XiaoNuoWire::uuidText(stream,streamText);if(mbedtls_sha256_starts(&uploadSha,0)){disconnect("HASH_FAILED");return;}trimTurn=wakeTurn&&compressedTurn&&message["trim_end_enabled"].is<bool>()&&message["trim_end_enabled"].as<bool>();uploadReadyAt=millis();Serial.printf("{\"event\":\"audio_upload_ready\",\"wait_ms\":%lu}\n",(unsigned long)(uploadReadyAt-turnStarted));uploading=true;publish("LISTENING");return;
    }
    if(!strcmp(kind,"audio_processing")&&!uploading&&uploadBytes==uploadTarget&&currentStream(message)){publish("THINKING");return;}
    if(!strcmp(kind,"audio_result")&&!uploading&&!resultReceived&&uploadBytes==uploadTarget&&currentStream(message)&&message["reply_audio_enabled"].is<bool>()&&message["playback_complete"].is<bool>()&&!message["playback_complete"].as<bool>()&&message["reply_text"].is<const char*>()&&message["transcript"].is<const char*>()){
      if(!wakeMetadata(message)){disconnect("INVALID_WAKE_REPLY");return;}
      pendingExpression=XnuFace::fromName(message["expression"]|"neutral");
      resultReceived=true;if(!message["reply_audio_enabled"].as<bool>()){if(!reminderTurn&&message["reply_text"].as<const char*>()[0])completeExpression();turn=false;publish("READY");}else publish("PREPARING_REPLY");return;
    }
    if(!strcmp(kind,"reply_start")&&resultReceived&&!reply){
      uint32_t count=message["bytes"]|0U;
      const char* format=message["format"]|"";
      bool compressed=!strcmp(format,"ima_adpcm4");
      uint32_t samples=compressed?(message["samples"]|0U):count/2;
      bool valid=message["bytes"].is<uint32_t>()&&count&&message["segment"].is<uint32_t>()&&message["segment"].as<uint32_t>()==segments&&
                 message["sample_rate"].as<int>()==16000&&message["channels"].as<int>()==1&&
                 !strcmp(format,compressed?"ima_adpcm4":"s16le")&&message["gain_divisor"].as<int>()==2&&
                 message["window_frames"].as<int>()==4&&XiaoNuoWire::uuid(message["reply_id"]|"",replyId);
      if(valid&&compressed){
        valid=message["samples"].is<uint32_t>()&&samples&&samples<=60*16000&&count<=60*8000&&count==(samples+1)/2&&
              message["predictor"].is<int>()&&message["step_index"].is<int>()&&
              message["predictor"].as<int>()>=-32768&&message["predictor"].as<int>()<=32767&&
              message["step_index"].as<int>()>=0&&message["step_index"].as<int>()<=88;
      }else if(valid){
        valid=count<=XiaoNuoWire::MAX_REPLY&&count%2==0;
      }
      if(!valid){disconnect("INVALID_REPLY_START");return;}
      reply=true;replyEnded=ackPending=false;replyTarget=count;replyTargetSamples=samples;
      compressedReply=compressed;replyInitialStateValue={compressed?message["predictor"].as<int>():0,compressed?message["step_index"].as<int>():0};
      replyBytes=replySequence=0;windowFrames=message["max_window_frames"].as<int>()>=16?16:4;
      if(mbedtls_sha256_starts(&replySha,0)){disconnect("HASH_FAILED");return;}
      Packet item;item.kind=REPLY_BEGIN;item.count=count;item.size=windowFrames;memcpy(item.id,replyId,16);if(!queueReply(item))return;
      JsonDocument ack;ack["type"]="reply_ready";ack["reply_id"]=message["reply_id"];ack["sample_rate"]=16000;ack["gain_divisor"]=2;ack["max_seconds"]=60;
      if(windowFrames>=16)ack["window_frames"]=windowFrames;publish("BUFFERING_REPLY");json(ack);windowOpenedAt=millis();windowReceived=0;return;
    }
    if(!strcmp(kind,"reply_end")&&reply&&!replyEnded&&!ackPending&&replyBytes==replyTarget){
      uint8_t id[16],digest[32];if(mbedtls_sha256_finish(&replySha,digest)){disconnect("HASH_FAILED");return;}
      if(!XiaoNuoWire::uuid(message["reply_id"]|"",id)||memcmp(id,replyId,16)||!XiaoNuoWire::digest(digest,message["sha256"]|"")){disconnect("REPLY_CHECKSUM");return;}
      replyEnded=true;Packet item;item.kind=REPLY_END;item.count=replyBytes;item.size=32;memcpy(item.data,digest,32);memcpy(item.id,replyId,16);queueReply(item);return;
    }
    if(!strcmp(kind,"audio_complete")&&resultReceived&&!reply&&currentStream(message)&&message["segments"].is<uint32_t>()&&message["segments"].as<uint32_t>()==segments&&message["playback_complete"].is<bool>()&&message["playback_complete"].as<bool>()==(segments>0)&&message["audible_verified"].is<bool>()&&!message["audible_verified"].as<bool>()){
      if(!wakeMetadata(message)){disconnect("INVALID_WAKE_REPLY");return;}if(segments&&!reminderTurn)completeExpression();turn=reminderTurn=false;publish("READY");return;}
    disconnect(!strcmp(kind,"audio_error")?"HOST_AUDIO_ERROR":"INVALID_REPLY");
  }
  void completeExpression() {
    portENTER_CRITICAL(&mux);published.expression=pendingExpression;published.expressionAt=millis();portEXIT_CRITICAL(&mux);
  }
  bool wakeMetadata(JsonDocument& message) {
    if(!wakeTurn)return true;
    const char* status=message["wake_status"]|"";
    if(strcmp(status,"ignored")&&strcmp(status,"accepted")&&strcmp(status,"listening")&&strcmp(status,"cancelled")&&strcmp(status,"quiet"))return false;
    if(!message["followup_seconds"].is<uint32_t>()||!message["quiet"].is<bool>())return false;
    uint32_t followup=message["followup_seconds"].as<uint32_t>();if(followup!=0&&followup!=20)return false;
    portENTER_CRITICAL(&mux);published.followup=followup;published.pcQuiet=message["quiet"].as<bool>();portEXIT_CRITICAL(&mux);return true;
  }
  void binary(uint8_t* bytes,size_t length) {
    bool frameOk=compressedReply?XiaoNuoWire::frameAny(bytes,length,replyId,replySequence):XiaoNuoWire::frame(bytes,length,replyId,replySequence);
    if(!ready||!turn||!reply||replyEnded||ackPending||!frameOk||length<=24||replyBytes+length-24>replyTarget){disconnect("INVALID_REPLY_FRAME");return;}
    uint32_t arrivedAt=millis();if(!windowReceived)windowFirstAt=arrivedAt;windowLastAt=arrivedAt;windowReceived++;
    Packet item;item.kind=REPLY_PCM;item.size=length-24;memcpy(item.data,bytes+24,item.size);memcpy(item.id,replyId,16);if(!queueReply(item))return;
    if(mbedtls_sha256_update(&replySha,item.data,item.size)){disconnect("HASH_FAILED");return;}replyBytes+=item.size;replySequence++;
    if(replySequence%windowFrames==0||replyBytes==replyTarget) {
      ackPending=true;ackPendingAt=millis();
      Serial.printf("{\"event\":\"reply_ack_pending\",\"next_sequence\":%lu,\"received_bytes\":%lu,\"queued\":%u,\"window\":%u,\"target\":%lu}\n",(unsigned long)replySequence,(unsigned long)replyBytes,uxQueueMessagesWaiting(rx),windowFrames,(unsigned long)replyTarget);
    }
  }
};





