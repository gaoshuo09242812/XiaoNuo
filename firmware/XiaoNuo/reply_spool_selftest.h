#pragma once
#include "reply_flash_spool.h"

// USB-only fixed synthetic storage exercise. No I2S, microphone or AI calls.
struct ReplySpoolSelftestResult {bool ok=false;unsigned phase=0,checks=0;uint32_t verified=0;};
static inline void spoolTestPattern(uint8_t* bytes,uint32_t at,size_t count){
  for(size_t i=0;i<count;i++)bytes[i]=(uint8_t)((at+i)*37+11);
}
static inline bool spoolTestPrepare(ReplyFlashSpool& spool,uint32_t length,uint8_t digest[32]){
  uint8_t data[1000];mbedtls_sha256_context hash;mbedtls_sha256_init(&hash);
  bool ok=spool.begin(length)&&mbedtls_sha256_starts(&hash,0)==0;
  for(uint32_t at=0;ok&&at<length;){
    size_t count=min((uint32_t)sizeof(data),length-at);spoolTestPattern(data,at,count);
    ok=spool.append(at,data,count)&&mbedtls_sha256_update(&hash,data,count)==0;
    at+=count;delay(1);
  }
  if(ok)ok=mbedtls_sha256_finish(&hash,digest)==0;
  mbedtls_sha256_free(&hash);xnu_spool_zero(&hash,sizeof(hash));xnu_spool_zero(data,sizeof(data));return ok;
}
static inline bool spoolTestRoundtrip(ReplyFlashSpool& spool,uint32_t length){
  uint8_t digest[32],output[333];
  bool ok=spoolTestPrepare(spool,length,digest)&&spool.seal(digest);
  for(uint32_t at=0;ok&&at<length;){
    size_t count=min((uint32_t)sizeof(output),length-at);ok=spool.read(output,count);
    for(size_t i=0;ok&&i<count;i++)if(output[i]!=(uint8_t)((at+i)*37+11))ok=false;
    at+=count;delay(1);
  }
  ok=ok&&spool.remaining()==0;spool.clear();
  xnu_spool_zero(digest,sizeof(digest));xnu_spool_zero(output,sizeof(output));return ok;
}
static inline ReplySpoolSelftestResult runReplySpoolSelftest(){
  static ReplyFlashSpool spool;ReplySpoolSelftestResult result;uint8_t data[1000]={},digest[32]={};
  result.phase=1;bool ok=spool.attach(true);
  if(ok){result.phase=2;ok=spoolTestRoundtrip(spool,480000);if(ok){result.checks++;result.verified+=480000;}}
  if(ok){result.phase=3;ok=spoolTestRoundtrip(spool,5003);if(ok){result.checks++;result.verified+=5003;}}
  if(ok){result.phase=4;ok=spoolTestPrepare(spool,1003,digest);digest[0]^=1;
    if(ok)ok=!spool.seal(digest)&&!spool.read(data,1);spool.clear();if(ok)result.checks++;}
  if(ok){result.phase=5;ok=spool.begin(1003)&&spool.append(0,data,1000);spool.clear();
    if(ok)ok=!spool.read(data,1);if(ok)result.checks++;}
  if(ok){result.phase=6;ok=spoolTestRoundtrip(spool,501);if(ok){result.checks++;result.verified+=501;}}
  spool.clear();xnu_spool_zero(data,sizeof(data));xnu_spool_zero(digest,sizeof(digest));
  result.ok=ok&&result.checks==5&&result.verified==485504;return result;
}
