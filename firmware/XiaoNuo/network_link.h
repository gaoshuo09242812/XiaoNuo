#pragma once
#include <Arduino.h>
#include <WiFi.h>
#include <WebSocketsClient.h>
#include <ArduinoJson.h>
#include <time.h>
#include "network_endpoint.h"
#include "device_binding.h"

// Control/heartbeat only. No network command can record, play or read memories.
// WebSockets 2.7.2 and ArduinoJson 7.4.3. TLS always requires a non-empty CA.
class NetworkLink {
 public:
  void begin(DeviceBinding& credentials, const char* hardware, const char* version) {
    binding=&credentials; hardwareId=hardware; firmware=version;
    socket.onEvent([this](WStype_t type,uint8_t* bytes,size_t length) { event(type,bytes,length); });
    socket.setReconnectInterval(15000);
  }
  void tick(bool busy) {
    if (!binding || !XiaoNuoEndpoint::HOST[0] || !XiaoNuoEndpoint::CA_PEM[0]) { state="DISABLED"; return; }
    if (!binding->id()[0]) { stop("UNBOUND"); return; }
    if (busy) { stop("USB_BUSY"); return; }
    if (WiFi.status()!=WL_CONNECTED) { stop("WIFI_OFFLINE"); return; }
    if (time(nullptr)<1704067200) {
      if (!clockStarted) { configTime(0,0,"ntp.aliyun.com","time.cloudflare.com"); clockStarted=true; }
      state="CLOCK_WAIT"; return;
    }
    if (started && pairedId!=binding->id()) stop("BINDING_CHANGED");
    if (!started) {
      if (retryAt && (int32_t)(retryAt-millis())>0) return;
      pairedId=binding->id();
      // Overrides library's default Origin: file:// header. Never print this.
      authorization=binding->authorizationHeader();
      if (authorization.isEmpty()) { state="UNBOUND"; return; }
      socket.beginSslWithCA(XiaoNuoEndpoint::HOST,XiaoNuoEndpoint::PORT,
                           "/api/v1/device-link/ws",XiaoNuoEndpoint::CA_PEM,"");
      socket.setExtraHeaders(authorization.c_str());
      started=true; state="CONNECTING";
    }
    socket.loop();
    uint32_t now=millis();
    if (connected && !ready && (uint32_t)(now-connectedAt)>7000) { stop("HELLO_TIMEOUT"); return; }
    if (!ready) return;
    if (awaiting && (uint32_t)(now-lastPing)>20000) { stop("PONG_TIMEOUT"); return; }
    if (!awaiting && (uint32_t)(now-lastPing)>=10000) {
      char frame[80];
      snprintf(frame,sizeof(frame),"{\"type\":\"ping\",\"sequence\":%lu}",(unsigned long)nextSequence);
      if (!socket.sendTXT(frame)) { stop("SEND_FAILED"); return; }
      lastPing=now; awaiting=true;
    }
  }
  void status() const {
    Serial.printf("{\"event\":\"network_status\",\"configured\":%s,\"connected\":%s,\"ready\":%s,\"state\":\"%s\",\"pong_count\":%lu,\"audio_upload_enabled\":false,\"tls_verified\":%s}\n",
      XiaoNuoEndpoint::HOST[0]&&XiaoNuoEndpoint::CA_PEM[0]?"true":"false",connected?"true":"false",ready?"true":"false",state,
      (unsigned long)pongCount,ready?"true":"false");
  }
 private:
  WebSocketsClient socket;
  DeviceBinding* binding=nullptr;
  const char* hardwareId=""; const char* firmware=""; const char* state="DISABLED";
  String authorization,pairedId;
  bool started=false,connected=false,ready=false,awaiting=false,clockStarted=false;
  uint32_t connectedAt=0,lastPing=0,nextSequence=1,pongCount=0,retryAt=0;
  void stop(const char* why) {
    if (started) socket.disconnect();
    started=false; connected=false; ready=false; awaiting=false;
    authorization=""; state=why;
    retryAt=millis()+15000;
  }
  void event(WStype_t type,uint8_t* bytes,size_t length) {
    if (type==WStype_DISCONNECTED) { connected=false;ready=false;awaiting=false;state="RECONNECTING";return; }
    if (type==WStype_CONNECTED) {
      connected=true;ready=false;awaiting=false;nextSequence=1;connectedAt=millis();state="AUTHENTICATING";
      char hello[256];
      snprintf(hello,sizeof(hello),"{\"type\":\"hello\",\"protocol\":1,\"id\":\"%s\",\"hardware_id\":\"%s\",\"firmware\":\"%s\"}",binding->id(),hardwareId,firmware);
      if (!socket.sendTXT(hello)) stop("SEND_FAILED");
      return;
    }
    if (type!=WStype_TEXT) {
      if (type==WStype_BIN || type==WStype_FRAGMENT_TEXT_START || type==WStype_FRAGMENT_BIN_START) stop("UNSUPPORTED_FRAME");
      return;
    }
    if (!connected || length>1024) { stop("INVALID_FRAME");return; }
    JsonDocument message;
    if (deserializeJson(message,bytes,length) || !message.is<JsonObject>()) { stop("INVALID_FRAME");return; }
    const char* kind=message["type"] | "";
    if (!ready && strcmp(kind,"ready")==0 && message["protocol"].is<int>() && message["protocol"].as<int>()==1 &&
        message["audio_upload_enabled"].is<bool>() && !message["audio_upload_enabled"].as<bool>() &&
        message["heartbeat_seconds"].as<int>()==10 && message["commands"].is<JsonArray>() && message["commands"].size()==0) {
      ready=true;state="READY";lastPing=millis();return;
    }
    if (ready && awaiting && strcmp(kind,"pong")==0 && message["sequence"].is<uint32_t>() &&
        message["sequence"].as<uint32_t>()==nextSequence && nextSequence<1000000) {
      awaiting=false;nextSequence++;pongCount++;return;
    }
    stop("INVALID_REPLY");
  }
};
