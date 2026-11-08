#pragma once
// One short HTTPS request at a time. WSS must be suspended by the caller.
#include <Arduino.h>
#include <WiFi.h>
#include <WiFiClientSecure.h>
#include <NetworkClient.h>
#include "network_endpoint.h"

class HttpRangeClient {
 public:
  int begin(const char* host,const char* path,const char* authorization,
            uint32_t offset,uint32_t length,IPAddress forced=IPAddress(0,0,0,0),
            IPAddress alternate=IPAddress(0,0,0,0),uint16_t port=443) {
    end();error=0;errorTextValue[0]=0;statusLine="";resolved=IPAddress(0,0,0,0);
    if(!length||offset>UINT32_MAX-length)return -4;
    IPAddress ip=forced;
    if(ip==IPAddress(0,0,0,0)&&WiFi.hostByName(host,ip)!=1)return -1;
    client.setTimeout(15000);
    client.setCACert(XiaoNuoEndpoint::CA_PEM);
    client.setHandshakeTimeout(30);
    client.setConnectionTimeout(10000);
    static const char* protocols[]={"http/1.1",nullptr};
    client.setAlpnProtocols(protocols);
    bool connected=false;
    for(unsigned attempt=0;attempt<2;attempt++) {
      if(attempt){if(alternate==IPAddress(0,0,0,0)||alternate==ip)break;ip=alternate;}
      resolved=ip;uint32_t started=millis();
      connected=client.connect(ip,port,host,XiaoNuoEndpoint::CA_PEM,nullptr,nullptr);
      error=connected?0:client.lastError(errorTextValue,sizeof(errorTextValue));
      Serial.printf("{\"event\":\"http_peer_attempt\",\"peer_ipv4\":\"%s\",\"port\":%u,\"connected\":%s,\"elapsed_ms\":%lu,\"tls_code\":%d}\n",
        ip.toString().c_str(),port,connected?"true":"false",(unsigned long)(millis()-started),error);
      if(connected)break;
      client.stop();
      // A separate short TCP probe distinguishes a route failure from TLS.
      NetworkClient tcp;started=millis();bool reachable=tcp.connect(ip,port,3000);tcp.stop();
      Serial.printf("{\"event\":\"http_tcp_probe\",\"peer_ipv4\":\"%s\",\"port\":%u,\"connected\":%s,\"elapsed_ms\":%lu}\n",
        ip.toString().c_str(),port,reachable?"true":"false",(unsigned long)(millis()-started));
    }
    if(!connected)return -2;
    String request=String("GET ")+path+" HTTP/1.1\r\nHost: "+host;
    if(port!=443)request+=String(":")+port;
    request+="\r\n";
    if(authorization&&authorization[0])request+=String("Authorization: ")+authorization+"\r\n";
    request+=String("Range: bytes=")+offset+"-"+String(offset+length-1)+"\r\n";
    request+="User-Agent: XiaoNuo-ESP32/1.0\r\nConnection: close\r\n\r\n";
    client.print(request);
    String status=client.readStringUntil('\n');
    statusLine=status;
    if(!status.startsWith("HTTP/")){end();return -3;}
    code=status.substring(9).toInt();
    remaining=0;
    while(true){
      String line=client.readStringUntil('\n');
      if(line=="\r"||line.length()==0)break;
      String lower=line;lower.toLowerCase();
      int colon=line.indexOf(':');
      if(colon<0)continue;
      String name=lower.substring(0,colon),value=line.substring(colon+1);
      value.trim();
      if(name=="content-length")remaining=value.toInt();
    }
    if(remaining<=0)remaining=length;
    return code;
  }
  bool connected(){return client.connected()||client.available()>0;}
  int read(uint8_t* buffer,size_t length) {
    size_t want=length;
    if(remaining>0&&want>(size_t)remaining)want=remaining;
    int got=client.read(buffer,want);
    if(got>0&&remaining>0)remaining-=got;
    return got;
  }
  void end(){client.stop();remaining=0;code=0;}
  int remainingBytes()const{return remaining;}
  int statusCode()const{return code;}
  int errorCode()const{return error;}
  const char* errorText()const{return errorTextValue;}
  IPAddress resolvedIp()const{return resolved;}
  const char* lastStatusLine()const{return statusLine.c_str();}
 private:
  WiFiClientSecure client;
  int remaining=0,code=0,error=0;
  IPAddress resolved;
  String statusLine;
  char errorTextValue[64]={};
};


