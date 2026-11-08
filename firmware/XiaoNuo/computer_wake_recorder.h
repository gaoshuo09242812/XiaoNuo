#pragma once
#include <Arduino.h>
#include <ESP_I2S.h>
#include <math.h>
#include <esp_heap_caps.h>
#include "pins.h"
#include "network_voice_link.h"
#include "adpcm_codec.h"

// Energy detects speech activity only. The owner's computer recognizes the
// actual wake word. No cloud model is called for an unaddressed probe.
class ComputerWakeRecorder {
 public:
  ComputerWakeRecorder(I2SClass& microphone,NetworkLink& connection):mic(microphone),link(connection){}
  bool listening()const{return opened;}
  bool recording()const{return collecting;}
  bool reminderIdle()const{return !collecting&&!voiceSamples&&(uint32_t)(millis()-lastVoiceAt)>=2000;}
  const char* state()const{return label;}
  void pause(){close(true,400);}
  void diagnostics(){Serial.printf("{\"event\":\"wake_buffer_diagnostics\",\"capacity_samples\":%u,\"buffered_samples\":%u,\"head\":%u,\"collecting\":%s,\"free_heap\":%u,\"byte_heap\":%u,\"word_heap\":%u}\n",unsigned(capacity),unsigned(used),unsigned(head),collecting?"true":"false",ESP.getFreeHeap(),heap_caps_get_free_size(MALLOC_CAP_INTERNAL|MALLOC_CAP_8BIT),heap_caps_get_free_size(MALLOC_CAP_INTERNAL|MALLOC_CAP_32BIT));}
  void tick(bool enabled,bool quiet,bool externalBusy) {
    NetworkLink::View view=link.view();
    if(!enabled||quiet||externalBusy||!view.ready||!view.computerWake||
       (collecting&&view.epoch!=epoch)) {close(true,400);return;}
    if(view.turn&&!collecting){close(false,400);return;}
    if(!collecting&&(int32_t)(retryAt-millis())>0)return;
    if(!opened&&!collecting&&!open())return;
    // Still capture only TARGET=6 seconds. This bound also includes waiting
    // for audio_ready and uploading on a high-latency mobile connection.
    if(collecting&&(uint32_t)(millis()-started)>30000){fail("CAPTURE_TIMEOUT");return;}
    if(opened) {
      size_t bytes=0;esp_err_t result=i2s_channel_read(mic.rxChan(),raw,sizeof(raw),&bytes,0);
      if(bytes%8){fail("MIC_ALIGNMENT");return;}
      if(result!=ESP_OK&&result!=ESP_ERR_TIMEOUT&&!bytes){fail("MIC_READ");return;}
      uint64_t squares=0;size_t samples=0;
      for(size_t i=0;i+1<bytes/sizeof(int32_t);i+=2) {
        // Close speech was hitting full scale and Vosk returned no speech.
        // Keep 6 dB of headroom before the DC blocker; speaker gain is unchanged.
        float input=(int16_t)(raw[i]>>17);filtered=input-previous+.995f*filtered;previous=input;
        int16_t sample=(int16_t)max(-32768.f,min(32767.f,filtered));
        if(warmup){noise=.999f*noise+.001f*abs(sample);warmup--;continue;}
        squares+=(int64_t)sample*sample;samples++;
        if(collecting) {
          if(collected>=TARGET)continue;
          if(used==capacity){fail("BUFFER_FULL");return;}
          push(sample);collected++;
        }else{
          if(used>=PREROLL)pop(1);push(sample);
        }
      }
      if(samples) {
        float rms=sqrt((double)squares/samples),threshold=max(90.f,noise*3.f+30.f);
        if(rms>threshold)lastVoiceAt=millis();
        if(!collecting) {
          label="PC_WAKE_LISTEN";
          if(rms>threshold)voiceSamples+=samples;else{voiceSamples=0;noise=.97f*noise+.03f*min(rms,2000.f);}
          if(voiceSamples>=1280) {
            if(!link.requestTalk(6,true,true,codes.decoder.predictor,codes.decoder.index)){close(false,1000);return;}
            collecting=true;padding=false;collected=used;captured=0;sent=0;silence=0;
            epoch=link.view().epoch;started=millis();label="PC_WAKE_RECORD";
          }
        }else{
          if(rms>threshold*.55f)silence=0;else silence+=samples;
          if(collected>=TARGET||(collected>=32000&&silence>=12800)) {
            mic.end();opened=false;captured=collected;padding=true;label="PC_WAKE_UPLOAD";
          }
        }
      }
    }
    if(!collecting)return;
    view=link.view();
    if(!view.turn||view.epoch!=epoch){close(false,1500);return;}
    if(!view.record)return;
    uint32_t target=xnu_wake_upload_target(captured,padding,view.trimUpload);
    // Drain the prefix and live input together. Queue fullness delays this
    // pump; a bounded ring overflow cancels instead of dropping speech.
    for(unsigned batch=0;batch<4&&link.sendSpace();batch++) {
      if(sent==target) {
        if(!link.finishUpload(view.trimUpload?target:0)){close(true,1500);return;}
        close(false,400);return;
      }
      size_t count=min((size_t)2000,(size_t)(target-sent));
      while(padding&&used<count&&collected<target){push(0);collected++;}
      if(!padding&&used<count)break;
      if(used<count)break;
      xnu_adpcm_pack(&codes,packet,count);
      if(!link.queuePcm(packet,count/2,false))break;
      pop(count);sent+=count;
    }
  }
 private:
  static constexpr size_t PREROLL=3200;
  static constexpr size_t capacity=96000;
  static constexpr uint32_t TARGET=6*16000;
  I2SClass& mic;NetworkLink& link;
  static constexpr size_t BLOCK_SAMPLES=16000,MAX_BLOCKS=6;
  XnuAdpcmRing codes={};
  size_t& head=codes.head;size_t& used=codes.used;
  int32_t raw[320];uint8_t packet[1000];
  bool opened=false,collecting=false,padding=false,firstOpen=true;
  const char* label="PC_WAKE_OFF";
  float previous=0,filtered=0,noise=20;
  uint32_t warmup=0,voiceSamples=0,silence=0,collected=0,captured=0,sent=0,epoch=0,started=0,retryAt=0;
  uint32_t lastVoiceAt=0;
  volatile uint32_t* allocateBlock(){
    const size_t bytes=8000;
    void* block=heap_caps_malloc(bytes,MALLOC_CAP_INTERNAL|MALLOC_CAP_32BIT|MALLOC_CAP_EXEC);
    if(!block&&heap_caps_get_free_size(MALLOC_CAP_INTERNAL|MALLOC_CAP_8BIT)>=bytes+12000)
      block=heap_caps_malloc(bytes,MALLOC_CAP_INTERNAL|MALLOC_CAP_32BIT|MALLOC_CAP_8BIT);
    if(block)for(size_t i=0;i<2000;i++)((volatile uint32_t*)block)[i]=0;
    return (volatile uint32_t*)block;
  }
  void releaseBuffer(){for(auto& block:codes.blocks)if(block){for(size_t i=0;i<2000;i++)block[i]=0;heap_caps_free((void*)block);block=nullptr;}}
  void push(int16_t sample){xnu_adpcm_push(&codes,sample);}
  void pop(size_t count){xnu_adpcm_pop(&codes,count);}
  bool open() {
    mic.setPins(XiaoNuoPins::MIC_SCK,XiaoNuoPins::MIC_WS,-1,XiaoNuoPins::MIC_SD);
    // Allocate I2S/DMA before optional buffering. Failed DMA setup in the core
    // cannot always release a channel that was never enabled.
    if(!mic.begin(I2S_MODE_STD,16000,I2S_DATA_BIT_WIDTH_32BIT,I2S_SLOT_MODE_STEREO,I2S_STD_SLOT_BOTH)) {
      mic.end();label="PC_WAKE_MIC_ERROR";retryAt=millis()+15000;return false;
    }
    bool allocated=true;
    for(size_t i=0;i<MAX_BLOCKS;i++){codes.blocks[i]=allocateBlock();if(!codes.blocks[i]){allocated=false;break;}}
    if(!allocated)releaseBuffer();
    if(!allocated){mic.end();label="PC_WAKE_NO_MEMORY";retryAt=millis()+1500;return false;}
    Serial.printf("{\"event\":\"wake_buffer_ready\",\"capacity_samples\":%u}\n",unsigned(capacity));
    codes.encoder={0,0};codes.decoder={0,0};head=used=0;
    opened=true;previous=filtered=0;noise=20;voiceSamples=0;
    warmup=firstOpen?16384:4000;firstOpen=false;label="PC_WAKE_PREPARING";return true;
  }
  void fail(const char* reason) {
    Serial.printf("{\"event\":\"wake_capture_fault\",\"reason\":\"%s\",\"buffered_samples\":%u,\"sent_samples\":%lu}\n",reason,unsigned(used),(unsigned long)sent);
    close(true,1500);
  }
  void close(bool cancel,uint32_t delay) {
    if(cancel&&collecting)link.cancel();
    if(opened)mic.end();opened=false;collecting=padding=false;
    releaseBuffer();
    memset(packet,0,sizeof(packet));head=used=0;voiceSamples=0;collected=sent=0;
    retryAt=millis()+delay;label="PC_WAKE_OFF";
  }
};
