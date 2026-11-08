#pragma once
#include <Arduino.h>
#include <esp_partition.h>
#include <esp_random.h>
#include <esp_wifi.h>
#include <mbedtls/aes.h>
#include <mbedtls/sha256.h>
#include "reply_spool_core.h"

// Candidate component only; not connected to NetworkLink or microphone capture.
// Runtime must explicitly qualify/claim a blank data partition before use.
class ReplyFlashSpool {
 public:
  ReplyFlashSpool(){mbedtls_aes_init(&aes);mbedtls_sha256_init(&sha);}
  ~ReplyFlashSpool(){clear();}
  ReplyFlashSpool(const ReplyFlashSpool&)=delete;
  ReplyFlashSpool& operator=(const ReplyFlashSpool&)=delete;
  bool attach(bool claimBlank=false){
    clear();initialized=false;partition=nullptr;
    partition=esp_partition_find_first(ESP_PARTITION_TYPE_DATA,ESP_PARTITION_SUBTYPE_DATA_SPIFFS,"spiffs");
    // This candidate is restricted to the known 4MB default layout. Refuse
    // an altered table rather than risking NVS, applications or other data.
    if(!partition||partition->address!=0x290000||partition->size!=0x160000||partition->encrypted)return false;
    XnuSpoolOps ops={readFlash,writeFlash,eraseFlash,startKey,crypt,hashStart,hashUpdate,hashFinish,clearKey};
    initialized=xnu_spool_init(&state,ops,this,partition->size,esp_random()%2)!=0;
    return initialized&&xnu_spool_attach(&state,claimBlank);
  }
  bool begin(uint32_t bytes){return initialized&&xnu_spool_begin(&state,bytes);}
  bool append(uint32_t offset,const uint8_t* data,size_t bytes){return initialized&&xnu_spool_append(&state,offset,data,bytes);}
  bool seal(const uint8_t expected[32]){return initialized&&xnu_spool_seal(&state,expected);}
  bool read(uint8_t* output,size_t bytes){return initialized&&xnu_spool_read(&state,output,bytes);}
  uint32_t remaining()const{return initialized&&state.state==XNU_SPOOL_SEALED?state.target-state.position:0;}
  void clear(){if(initialized)xnu_spool_close(&state);else clearKey(this);}
 private:
  const esp_partition_t* partition=nullptr;XnuReplySpool state={};bool initialized=false,keyReady=false;
  mbedtls_aes_context aes;mbedtls_sha256_context sha;uint8_t nonce[12]={};
  static ReplyFlashSpool* self(void* p){return static_cast<ReplyFlashSpool*>(p);}
  static int readFlash(void* p,uint32_t at,uint8_t* data,size_t bytes){
    auto* s=self(p);return s->partition&&at<=s->partition->size&&bytes<=s->partition->size-at&&
      esp_partition_read(s->partition,at,data,bytes)==ESP_OK;
  }
  static int writeFlash(void* p,uint32_t at,const uint8_t* data,size_t bytes){
    auto* s=self(p);return s->partition&&at<=s->partition->size&&bytes<=s->partition->size-at&&
      esp_partition_write(s->partition,at,data,bytes)==ESP_OK;
  }
  static int eraseFlash(void* p,uint32_t at,uint32_t bytes){
    auto* s=self(p);return s->partition&&(at==XNU_SPOOL_SECTOR||at==XNU_SPOOL_SECTOR+XNU_SPOOL_SLOT)&&
      bytes&&bytes<=XNU_SPOOL_SLOT&&bytes%XNU_SPOOL_SECTOR==0&&at<=s->partition->size&&bytes<=s->partition->size-at&&
      esp_partition_erase_range(s->partition,at,bytes)==ESP_OK;
  }
  static void clearKey(void* p){
    auto* s=self(p);s->keyReady=false;
    mbedtls_aes_free(&s->aes);xnu_spool_zero(&s->aes,sizeof(s->aes));mbedtls_aes_init(&s->aes);
    mbedtls_sha256_free(&s->sha);xnu_spool_zero(&s->sha,sizeof(s->sha));mbedtls_sha256_init(&s->sha);
    xnu_spool_zero(s->nonce,sizeof(s->nonce));
  }
  static int startKey(void* p){
    auto* s=self(p);uint8_t key[32];clearKey(p);
    // esp_fill_random has a true-random source while the WiFi RF subsystem
    // is enabled. Call begin only from the online, authenticated reply path.
    wifi_ap_record_t accessPoint;
    if(esp_wifi_sta_get_ap_info(&accessPoint)!=ESP_OK)return 0;
    xnu_spool_zero(&accessPoint,sizeof(accessPoint));
    esp_fill_random(key,sizeof(key));esp_fill_random(s->nonce,sizeof(s->nonce));
    bool ok=mbedtls_aes_setkey_enc(&s->aes,key,256)==0&&mbedtls_sha256_starts(&s->sha,0)==0;
    xnu_spool_zero(key,sizeof(key));s->keyReady=ok;
    if(!ok)clearKey(p);return ok;
  }
  static int crypt(void* p,uint32_t offset,const uint8_t* input,uint8_t* output,size_t bytes){
    auto* s=self(p);if(!s->keyReady||offset>XNU_SPOOL_LIMIT||bytes>XNU_SPOOL_LIMIT-offset)return 0;
    uint8_t counter[16]={},stream[16]={},skip[16]={};size_t used=0;
    for(unsigned i=0;i<12;i++)counter[i]=s->nonce[i];
    uint32_t block=offset/16;for(unsigned i=0;i<4;i++)counter[15-i]=(uint8_t)(block>>(8*i));
    size_t prefix=offset%16;
    bool ok=(!prefix||mbedtls_aes_crypt_ctr(&s->aes,prefix,&used,counter,stream,skip,skip)==0)&&
      mbedtls_aes_crypt_ctr(&s->aes,bytes,&used,counter,stream,input,output)==0;
    xnu_spool_zero(counter,sizeof(counter));xnu_spool_zero(stream,sizeof(stream));xnu_spool_zero(skip,sizeof(skip));
    return ok;
  }
  static int hashUpdate(void* p,const uint8_t* data,size_t bytes){return mbedtls_sha256_update(&self(p)->sha,data,bytes)==0;}
  static int hashStart(void* p){return mbedtls_sha256_starts(&self(p)->sha,0)==0;}
  static int hashFinish(void* p,uint8_t digest[32]){return mbedtls_sha256_finish(&self(p)->sha,digest)==0;}
};
