#pragma once
#include <Arduino.h>
#include <ESP_I2S.h>
#include <math.h>
#include "pins.h"
#include "config.h"

// Bounded, half-duplex development playback. No sound is enabled at boot.
// MAX98357A does not support 24 kHz: host converts to 16 kHz PCM16 mono.
class SpeakerStream {
 public:
  static constexpr uint32_t MAX_SAMPLES = 16000 * 8;
  static constexpr uint32_t NETWORK_MAX_SAMPLES = 16000 * 60;
  bool active() const { return running; }
  uint32_t progress() const { return received; }
  uint32_t targetSamples() const { return target; }
  bool expectsBinary() const { return frameSize != 0; }
  const char* state() const { return running ? "SPEAKING" : "IDLE"; }
  bool completed() const { return !running && strcmp(lastEvent,"play_end")==0; }
  bool networkStart(const uint8_t id[16],uint32_t bytes,bool quiet) {
    if(quiet||running||!bytes||bytes%2||bytes/2>NETWORK_MAX_SAMPLES) {
      if(!running)lastError=quiet?"SPEAKER_QUIET":"SPEAKER_SAMPLE_LIMIT";
      Serial.printf("{\"event\":\"speaker_start_rejected\",\"active\":%s,\"quiet\":%s,\"bytes\":%lu}\n",
        running?"true":"false",quiet?"true":"false",(unsigned long)bytes);
      return false;
    }
    char line[48];snprintf(line,sizeof(line),"PLAY_START %02x%02x%02x%02x %lu",id[0],id[1],id[2],id[3],(unsigned long)(bytes/2));
    command(String(line),quiet,false,true);return running;
  }
  bool networkWrite(const uint8_t* data,size_t bytes) {
    if(!running||draining||frameSize||!bytes||bytes>sizeof(frame)||bytes%2||received+bytes/2>target)return false;
    frameSize=bytes;frameUsed=0;frameCrc=checksum(data,bytes);
    for(size_t i=0;i<bytes&&running;i++)consume(data[i]);return running;
  }
  bool networkFinish() {
    if(!running||draining||frameSize||received!=target)return false;
    draining=true;drainUntil=millis()+120;return true;
  }
  // A transport loss can stop playback mid-waveform. Ramp to zero and let the
  // DMA drain before disabling I2S, so the amplifier does not click/buzz.
  void fadeStop(const char* event,const char* code) {
    if(!running){stop(event,code);return;}
    int16_t level=lastOutput;
    if(level) {
      const size_t fadeSamples=480;
      for(size_t i=0;i<fadeSamples;i++) {
        int16_t value=(int16_t)((int32_t)level*(int32_t)(fadeSamples-1-i)/(int32_t)fadeSamples);
        stereo[2*i]=stereo[2*i+1]=value;
      }
      size_t written=0;
      i2s_channel_write(output.txChan(),stereo,fadeSamples*4,&written,100);
      vTaskDelay(pdMS_TO_TICKS(200));
      lastOutput=0;
    }
    stop(event,code);
  }

  void report() const {
    Serial.printf("{\"event\":\"audio_status\",\"active\":%s,\"last_event\":\"%s\",\"last_error\":\"%s\","
                  "\"written_samples\":%lu,\"input_peak\":%lu,\"output_peak\":%lu,\"output_rms\":%.2f,"
                  "\"sample_rate\":16000,\"bits\":16,\"channels\":2,\"gain_divisor\":%u,\"max_seconds\":8,"
                  "\"expected_bclk_hz\":512000,\"clock_measured\":false,\"audible_verified\":false,"
                  "\"bclk_pin\":%u,\"lrc_pin\":%u,\"din_pin\":%u}\n",
                  running?"true":"false",lastEvent,lastError,(unsigned long)received,
                  (unsigned long)inputPeak,(unsigned long)outputPeak,received?sqrt((double)outputSquares/received):0.0,
                  XiaoNuoConfig::SPEAKER_GAIN_DIVISOR,
                  XiaoNuoPins::AMP_BCLK,XiaoNuoPins::AMP_LRC,XiaoNuoPins::AMP_DIN);
  }

  void stop(const char* event = "play_stopped", const char* code = "") {
    bool wasRunning = running;
    if (wasRunning) { lastEvent=event; lastError=code; }
    // 0.6.11 tried to keep the I2S clocks alive with silence, but on this
    // board that left an audible idle/current noise after playback. Revert to
    // the 0.6.10 known-good stop: disable I2S and release the pins.
    if (running) output.end();
    outputReady = false;
    running = false; frameSize = frameUsed = 0; draining = false;
    pinMode(XiaoNuoPins::AMP_BCLK, INPUT_PULLDOWN);
    pinMode(XiaoNuoPins::AMP_LRC, INPUT_PULLDOWN);
    pinMode(XiaoNuoPins::AMP_DIN, INPUT_PULLDOWN);
    if (wasRunning || strcmp(event,"play_stopped")==0)
      Serial.printf("{\"event\":\"%s\",\"id\":\"%s\",\"samples\":%lu,\"crc32\":%lu,\"code\":\"%s\"}\n",
                    event, identifier, (unsigned long)received, (unsigned long)streamCrc, code);
  }

  bool command(const String& line, bool quiet, bool capturing, bool network=false) {
    if (!line.startsWith("PLAY_")) return false;
    if (line=="PLAY_STOP") { stop(); return true; }
    char id[9] = {}, extra; unsigned long count=0, seq=0, bytes=0, crc=0;
    if (line.startsWith("PLAY_START ") &&
        sscanf(line.c_str(),"PLAY_START %8[0-9a-f] %lu %c",id,&count,&extra)==2 && strlen(id)==8) {
      if (quiet || capturing || running || count==0 || count>(network?NETWORK_MAX_SAMPLES:MAX_SAMPLES)) {
        Serial.println("{\"event\":\"error\",\"code\":\"SPEAKER_START_BLOCKED\"}"); return true;
      }
      strcpy(identifier,id); target=count; received=0; sequence=0; streamCrc=0; lastOutput=0;
      inputPeak=outputPeak=0; outputSquares=0; lastEvent="starting"; lastError="";
      frameSize=frameUsed=0; draining=false;
      output.setPins(XiaoNuoPins::AMP_BCLK,XiaoNuoPins::AMP_LRC,XiaoNuoPins::AMP_DIN,-1);
      if (!output.begin(I2S_MODE_STD,16000,I2S_DATA_BIT_WIDTH_16BIT,I2S_SLOT_MODE_STEREO,I2S_STD_SLOT_BOTH)) {
        output.end(); running=true; stop("play_error","SPEAKER_INIT_FAILED"); return true;
      }
      running=true; started=lastData=millis();
      lastEvent="playing";
      Serial.printf("{\"event\":\"play_ready\",\"id\":\"%s\",\"sample_rate\":16000,\"gain_divisor\":%u,\"max_seconds\":%u}\n",identifier,XiaoNuoConfig::SPEAKER_GAIN_DIVISOR,network?20:8);
      return true;
    }
    if (line.startsWith("PLAY_DATA ") &&
        sscanf(line.c_str(),"PLAY_DATA %8[0-9a-f] %lu %lu %lu %c",id,&seq,&bytes,&crc,&extra)==4 && strlen(id)==8) {
      if (!running || draining || strcmp(id,identifier)!=0 || seq!=sequence || bytes==0 || bytes>sizeof(frame) || bytes%2 || received+bytes/2>target) {
        stop("play_error","SPEAKER_FRAME_INVALID");
        Serial.println("{\"event\":\"error\",\"code\":\"SPEAKER_FRAME_INVALID\"}"); return true;
      }
      frameSize=bytes; frameUsed=0; frameCrc=crc; lastData=millis(); return true;
    }
    if (line.startsWith("PLAY_END ") &&
        sscanf(line.c_str(),"PLAY_END %8[0-9a-f] %lu %c",id,&crc,&extra)==2 && strlen(id)==8) {
      if (!running || draining || strcmp(id,identifier)!=0 || received!=target || streamCrc!=crc) {
        stop("play_error","SPEAKER_INCOMPLETE"); return true;
      }
      // Six DMA buffers of 240 frames at 16 kHz: allow final audio to drain.
      draining=true; drainUntil=millis()+120; return true;
    }
    stop("play_error","SPEAKER_COMMAND_INVALID");
    Serial.println("{\"event\":\"error\",\"code\":\"SPEAKER_COMMAND_INVALID\"}");
    return true;
  }

  void consume(uint8_t byte) {
    if (!running || frameSize==0) return;
    frame[frameUsed++]=byte;
    if (frameUsed<frameSize) return;
    size_t bytes=frameSize; frameSize=frameUsed=0;
    if (checksum(frame,bytes)!=frameCrc) { stop("play_error","SPEAKER_CRC_MISMATCH"); return; }
    // Acknowledge validated receipt before the DMA write, so USB can deliver
    // the next bounded frame while I2S drains this one. PLAY_END verifies writes.
    Serial.printf("{\"event\":\"play_ack\",\"id\":\"%s\",\"seq\":%lu,\"samples\":%lu}\n",identifier,(unsigned long)sequence,(unsigned long)(received+bytes/2));
    uint32_t frameInputPeak=0,frameOutputPeak=0;
    uint64_t frameSquares=0;
    for (size_t i=0;i<bytes/2;i++) {
      int16_t sample=(int16_t)((uint16_t)frame[2*i] | ((uint16_t)frame[2*i+1]<<8));
      int32_t value=sample/XiaoNuoConfig::SPEAKER_GAIN_DIVISOR;
      uint32_t position=received+i;
      if (position<160) value=value*(int32_t)position/160;
      if (target-1-position<160) value=value*(int32_t)(target-1-position)/160;
      lastOutput=(int16_t)value;
      stereo[2*i]=stereo[2*i+1]=(int16_t)value;
      frameInputPeak=max(frameInputPeak,(uint32_t)abs((int32_t)sample));
      frameOutputPeak=max(frameOutputPeak,(uint32_t)abs(value));
      frameSquares+=(int64_t)value*value;
    }
    size_t written=0;
    if (i2s_channel_write(output.txChan(),stereo,bytes*2,&written,100)!=ESP_OK || written!=bytes*2) {
      stop("play_error","SPEAKER_WRITE_FAILED"); return;
    }
    streamCrc=checksum(frame,bytes,streamCrc); received+=bytes/2; lastData=millis();
    inputPeak=max(inputPeak,frameInputPeak); outputPeak=max(outputPeak,frameOutputPeak); outputSquares+=frameSquares;
    sequence++;
  }

  void tick(bool quiet) {
    if (!running) return;
    if (quiet) { stop(); return; }
    uint32_t now=millis();
    if (draining && (int32_t)(now-drainUntil)>=0) { stop("play_end"); return; }
    // The Pages relay can take about a second to deliver the next window.
    // Keep the I2S channel alive across that gap instead of truncating audio.
    if ((!draining && (uint32_t)(now-lastData)>3000) || (uint32_t)(now-started)>(target>MAX_SAMPLES?target/16+10000:20000))
      stop("play_error","SPEAKER_STREAM_TIMEOUT");
  }

 private:
  static uint32_t checksum(const uint8_t* data,size_t size,uint32_t previous=0) {
    uint32_t crc=~previous;
    for (size_t i=0;i<size;i++) {
      crc^=data[i];
      for (uint8_t bit=0;bit<8;bit++) crc=(crc>>1)^(0xedb88320UL & (0UL-(crc&1)));
    }
    return ~crc;
  }
  I2SClass output;
  bool running=false,draining=false,outputReady=false;
  const char* lastEvent="none";
  const char* lastError="";
  uint32_t inputPeak=0,outputPeak=0;
  uint64_t outputSquares=0;
  char identifier[9]={};
  uint32_t target=0,received=0,sequence=0,streamCrc=0,frameCrc=0,started=0,lastData=0,drainUntil=0;
  uint16_t frameSize=0,frameUsed=0;
  uint8_t frame[1024];
  int16_t stereo[1024];
  int16_t lastOutput=0;
};
