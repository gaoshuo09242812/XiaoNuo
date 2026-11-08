#include <Arduino.h>
#include <Wire.h>
#include "reminder_poll_policy.h"
#include <WiFi.h>
#include <WiFiClientSecure.h>
#include <WebServer.h>
#include <Preferences.h>
#include <U8g2lib.h>
#include <ESP_I2S.h>
#include <esp_system.h>
#include <math.h>
#include <mbedtls/base64.h>
#include "config.h"
#include "reply_spool_selftest.h"
#include "speaker_stream.h"
#include "device_binding.h"
#include "network_voice_link.h"
#include "network_endpoint.h"
#include "http_reply_client.h"
#include "http_reply_playback.h"
#include "network_mtu_probe.h"
#include "computer_wake_recorder.h"
#include "status_font.h"

using namespace XiaoNuoPins;
using namespace XiaoNuoConfig;

Preferences settings;
WebServer setupServer(80);
U8G2_SSD1306_128X64_NONAME_F_HW_I2C ssd1306(U8G2_R0, U8X8_PIN_NONE, OLED_SCL, OLED_SDA);
U8G2_SH1106_128X64_NONAME_F_HW_I2C sh1106(U8G2_R0, U8X8_PIN_NONE, OLED_SCL, OLED_SDA);
U8G2* screen = nullptr;
I2SClass microphone;
SpeakerStream speaker;
DeviceBinding binding;
NetworkLink networkLink;
ReplyFlashSpool networkReplyCache;
uint32_t networkCacheBytes=0;
uint32_t networkCachePrepareMs=0,networkCacheWriteMs=0;
ComputerWakeRecorder wakeRecorder(microphone,networkLink);
bool computerWakeEnabled=true;
char deviceId[19]={};
String commandLine;
String deviceState = "SAFE_START";
String screenDriver = "none";
String savedSsid, savedPassword, accessPointName, accessPointPassword, setupToken;
bool quietMode = false, accessPointActive = false, wifiConnecting = false;
bool droppingCommand = false;
bool captureActive = false, captureCancelled = false;
bool networkCapture=false,networkPlaying=false,networkBuffering=false,networkDraining=false;
uint8_t networkCaptureSeconds=0;
uint32_t networkEpoch=1,networkRequestedAt=0;
NetworkLink::Packet networkReply;
NetworkLink::Packet networkIoPacket;
XnuAdpcmState networkReplyDecodeState={0,0};
uint32_t networkReplyDecoded=0;
char pendingResumeId[37]={};
uint32_t pendingResumeSamples=0;
bool resumePending=false;
bool networkReplayAfterDrop=false;
uint32_t networkReplayEmptyAt=0;
uint32_t networkBufferStartedAt=0;
uint8_t captureDurationSeconds = 0;
String displayNotice, displayNoticeDetail;
uint32_t displayNoticeUntil = 0;
String hostTask;
uint32_t hostTaskUntil = 0;
void readAndProcessCommands();
uint8_t wifiAttempts = 0;
uint32_t wifiRetryCycles = 0;
portMUX_TYPE wifiDiagnosticMux=portMUX_INITIALIZER_UNLOCKED;
uint32_t wifiDisconnectCount=0,wifiDisconnectAt=0;
uint8_t wifiDisconnectReason=0;
uint32_t wifiStartedAt = 0, wifiNextAttempt = 0, setupExpiresAt = 0, bootAt = 0;
bool setupFallback = false;
uint32_t lastDisplayAt = 0, lastHostAt = 0;
int32_t microphoneBuffer[1024];

String randomHex() {
  char value[33];
  snprintf(value, sizeof(value), "%08lx%08lx%08lx%08lx", (unsigned long)esp_random(), (unsigned long)esp_random(),
           (unsigned long)esp_random(), (unsigned long)esp_random());
  return String(value);
}

void reportWifiDiagnostics() {
  uint32_t count,at;uint8_t reason;
  portENTER_CRITICAL(&wifiDiagnosticMux);
  count=wifiDisconnectCount;at=wifiDisconnectAt;reason=wifiDisconnectReason;
  portEXIT_CRITICAL(&wifiDiagnosticMux);
  bool connected=WiFi.status()==WL_CONNECTED;
  Serial.printf("{\"event\":\"wifi_diagnostics\",\"connected\":%s,\"status_code\":%d,\"disconnect_count\":%lu,\"last_reason\":%u,\"reason_age_ms\":%lu,\"rssi_dbm\":%ld,\"channel\":%d,\"clock_valid\":%s}\n",
    connected?"true":"false",(int)WiFi.status(),(unsigned long)count,reason,
    (unsigned long)(count?millis()-at:0),(long)(connected?WiFi.RSSI():0),WiFi.channel(),
    time(nullptr)>=1704067200?"true":"false");
}

void sendError(const char* code) {
  Serial.printf("{\"event\":\"error\",\"code\":\"%s\"}\n", code);
}

void sendStatus() {
  uint32_t reportNow=millis();
  bool hostConnected = lastHostAt != 0 && (uint32_t)(reportNow - lastHostAt) < HOST_TIMEOUT_MS;
  String ip = WiFi.status() == WL_CONNECTED ? WiFi.localIP().toString() : "";
  Serial.printf("{\"event\":\"status\",\"firmware\":\"%s\",\"device_id\":\"%s\",\"binding_id\":\"%s\",\"chip\":\"%s\",\"flash_bytes\":%lu,"
                "\"free_heap\":%lu,\"uptime_ms\":%lu,\"reset_reason\":%d,\"state\":\"%s\","
                "\"oled\":%s,\"oled_driver\":\"%s\",\"quiet\":%s,\"host_connected\":%s,"
                "\"wifi_connected\":%s,\"wifi_attempts\":%u,\"wifi_retry_cycles\":%lu,\"wifi_retry_wait_ms\":%lu,\"ip\":\"%s\",\"saved_ssid\":\"%s\",\"setup_active\":%s,"
                "\"speaker_output\":%s,\"speaker_available\":true,\"speaker_gain_divisor\":%u,\"speaker_max_seconds\":8,"
                "\"psram_bytes\":%lu,\"task_state\":\"%s\",\"voice_wake\":false,\"cloud_ai\":%s,\"computer_wake_enabled\":%s,\"wake_detection\":\"owner_computer\"}\n",
                VERSION, deviceId, binding.id(), ESP.getChipModel(), (unsigned long)ESP.getFlashChipSize(), (unsigned long)ESP.getFreeHeap(),
                (unsigned long)millis(), (int)esp_reset_reason(), speaker.active()?"SPEAKING":deviceState.c_str(), screen ? "true" : "false",
                screenDriver.c_str(), quietMode ? "true" : "false", hostConnected ? "true" : "false",
                WiFi.status() == WL_CONNECTED ? "true" : "false", wifiAttempts, (unsigned long)wifiRetryCycles,
                (unsigned long)(!wifiConnecting && WiFi.status()!=WL_CONNECTED && (int32_t)(wifiNextAttempt-reportNow)>0 ? wifiNextAttempt-reportNow : 0),
                ip.c_str(), settings.getString("ssid","").c_str(), accessPointActive ? "true" : "false",
                speaker.active()?"true":"false",SPEAKER_GAIN_DIVISOR,(unsigned long)ESP.getPsramSize(),hostTask.c_str(),
                networkLink.view().cloudAi?"true":"false",computerWakeEnabled&&networkLink.view().computerWake?"true":"false");
}

void drawLargeStatus(const char* title,const char* detail) {
  screen->setFont(xiaonuo_status_font);
  uint16_t width=screen->getUTF8Width(title)*2;
  screen->drawUTF8X2(width<128?(128-width)/2:0,40,title);
  screen->setFont(u8g2_font_6x10_tr);
  screen->drawFrame(0,48,128,16);
  screen->drawStr(5,60,detail);
}

void displayTick(bool force = false) {
  if (!screen) return;
  if (speaker.active() && !force) return;  // Keep display transfers out of PCM streaming.
  uint32_t now = millis();
  if (displayNoticeUntil && (int32_t)(displayNoticeUntil-now)<=0) displayNoticeUntil=0;
  if (!hostTask.isEmpty() && ((uint32_t)(now-lastHostAt)>=HOST_TIMEOUT_MS || (int32_t)(hostTaskUntil-now)<=0)) {
    hostTask=""; hostTaskUntil=0;
  }
  uint32_t period = quietMode ? 350 : 100;
  if (!force && (uint32_t)(now - lastDisplayAt) < period) return;
  lastDisplayAt = now;
  screen->clearBuffer();
  screen->setFont(u8g2_font_6x10_tr);
  screen->drawStr(0, 9, "XIAONUO");
  if (accessPointActive && !captureActive && !speaker.active()) {
    screen->drawStr(0, 23, accessPointName.c_str());
    String passwordLine = "P:" + accessPointPassword;
    screen->drawStr(0, 37, passwordLine.c_str());
    screen->drawStr(0, 51, "192.168.4.1");
    screen->drawStr(0, 63, "WiFi setup / 10 min");
  } else if (speaker.active()) {
    drawLargeStatus("我在说话","SPEAKING");
  } else if (captureActive) {
    char detail[22];
    snprintf(detail,sizeof(detail),"REC / %u SEC",captureDurationSeconds);
    drawLargeStatus(deviceState=="LISTENING"?"请说话":"准备中",
                    deviceState=="LISTENING"?detail:"MIC WARMUP");
  } else if (!quietMode && !hostTask.isEmpty() && !networkLink.active()) {
    if (hostTask=="RECOGNIZING") drawLargeStatus("识别中","RECOGNIZING");
    else if (hostTask=="THINKING") drawLargeStatus("思考中","THINKING");
    else drawLargeStatus("准备回复","PREPARING REPLY");
  } else if(!quietMode&&networkLink.active()) {
    NetworkLink::View status=networkLink.view();
    if(!strcmp(status.state,"BUFFERING_REPLY")) {
      uint32_t percent=networkBuffering&&networkReply.count?min(100UL,(unsigned long)((uint64_t)networkCacheBytes*100/networkReply.count)):0;
      char detail[22];snprintf(detail,sizeof(detail),"RECEIVING %lu%%",(unsigned long)percent);
      drawLargeStatus("接收中",detail);
    } else drawLargeStatus(wakeRecorder.recording()?"请说话":!strcmp(status.state,"RECOGNIZING")?"识别中":!strcmp(status.state,"THINKING")?"思考中":"准备回复",status.state);
  } else if (displayNoticeUntil && (int32_t)(displayNoticeUntil-now)>0) {
    drawLargeStatus(displayNotice.c_str(),displayNoticeDetail.c_str());
  } else if(!quietMode&&computerWakeEnabled&&!networkLink.view().ready) {
    if(WiFi.status()!=WL_CONNECTED) {
      char detail[22];
      if(!wifiConnecting&&(int32_t)(wifiNextAttempt-now)>0)snprintf(detail,sizeof(detail),"WIFI RETRY %lus",(unsigned long)((wifiNextAttempt-now+999)/1000));
      else snprintf(detail,sizeof(detail),"%s",savedSsid.isEmpty()?"WIFI SETUP NEEDED":"WIFI CONNECTING");
      drawLargeStatus("联网中",detail);
    } else if(!strcmp(networkLink.view().state,"CLOCK_WAIT"))drawLargeStatus("校时中","SYNCING CLOCK");
    else drawLargeStatus("连接中","CONNECTING SERVICE");
  } else {
    NetworkLink::View face=networkLink.view();
    bool faceQuiet=quietMode||face.pcQuiet;
    XnuFace::draw(*screen,XnuFace::visible((XnuFace::Expression)face.expression,now,face.expressionAt,faceQuiet),now,faceQuiet);
    String label = speaker.active() ? "SPEAKING" : (captureActive ? deviceState : (quietMode ? "QUIET" : (WiFi.status() == WL_CONNECTED ? WiFi.localIP().toString() : deviceState)));
    screen->drawStr(0, 63, label.c_str());
    screen->drawStr(78, 9, computerWakeEnabled&&networkLink.view().computerWake?"PC WAKE":networkLink.view().ready?"NETWORK":"USB DEV");
  }
  screen->sendBuffer();
}

uint8_t findOledAddress() {
  Wire.begin(OLED_SDA, OLED_SCL);
  Wire.setClock(400000);
  Wire.setTimeOut(25);
  for (uint8_t address : {uint8_t(0x3c), uint8_t(0x3d)}) {
    Wire.beginTransmission(address);
    if (Wire.endTransmission() == 0) return address;
  }
  return 0;
}

bool enableScreen(const String& driver, bool save) {
  uint8_t address = findOledAddress();
  if (!address) { sendError("OLED_NOT_FOUND"); return false; }
  if (driver == "ssd1306") screen = &ssd1306;
  else if (driver == "sh1106") screen = &sh1106;
  else { sendError("INVALID_OLED_DRIVER"); return false; }
  screenDriver = driver;
  screen->setI2CAddress(address << 1);
  screen->setBusClock(400000);
  screen->begin();
  screen->setContrast(100);
  if (save) settings.putString("oled", driver);
  displayTick(true);
  Serial.printf("{\"event\":\"oled_ready\",\"address\":%u,\"driver\":\"%s\",\"visual_verified\":false}\n", address, driver.c_str());
  return true;
}

void scanI2C() {
  Wire.begin(OLED_SDA, OLED_SCL);
  Wire.setTimeOut(20);
  Serial.print("{\"event\":\"i2c_scan\",\"addresses\":[");
  bool first = true;
  for (uint8_t address = 8; address < 120; address++) {
    Wire.beginTransmission(address);
    if (Wire.endTransmission() == 0) {
      if (!first) Serial.print(',');
      Serial.print(address);
      first = false;
    }
    delay(0);
  }
  Serial.println("]}");
}

void microphoneProbe() {
  microphone.setPins(MIC_SCK, MIC_WS, -1, MIC_SD);
  // INMP441 requires 64 SCK edges per WS frame, including for one microphone.
  if (!microphone.begin(I2S_MODE_STD, 16000, I2S_DATA_BIT_WIDTH_32BIT, I2S_SLOT_MODE_STEREO, I2S_STD_SLOT_BOTH)) {
    sendError("MIC_INIT_FAILED"); microphone.end(); return;
  }
  // Drain DMA during startup and allow extra settling time before measuring.
  uint32_t warmupFrames = 0;
  uint32_t warmupStartedAt = millis();
  while (warmupFrames < 16384 && (uint32_t)(millis() - warmupStartedAt) < 2500) {
    size_t received = 0;
    i2s_channel_read(microphone.rxChan(), microphoneBuffer, sizeof(microphoneBuffer), &received, 150);
    warmupFrames += received / (2 * sizeof(int32_t));
    delay(0);
  }
  if (warmupFrames < 16384) {
    microphone.end(); sendError("MIC_READ_TIMEOUT"); return;
  }
  uint64_t sumSquares = 0, rightSquares = 0;
  int64_t sum = 0;
  uint32_t count = 0, nonzero = 0, clipped = 0;
  int16_t low = INT16_MAX, high = INT16_MIN;
  for (int part = 0; part < 8; part++) {
    size_t received = 0;
    esp_err_t result = i2s_channel_read(microphone.rxChan(), microphoneBuffer, sizeof(microphoneBuffer), &received, 150);
    if (result != ESP_OK && received == 0) break;
    for (size_t i = 0; i + 1 < received / sizeof(int32_t); i += 2) {
      int16_t sample = (int16_t)(microphoneBuffer[i] >> 16);
      int16_t right = (int16_t)(microphoneBuffer[i + 1] >> 16);
      low = min(low, sample); high = max(high, sample);
      nonzero += sample != 0;
      clipped += sample >= 32760 || sample <= -32760;
      sumSquares += (int64_t)sample * sample;
      rightSquares += (int64_t)right * right;
      sum += sample;
      count++;
    }
    delay(0);
  }
  microphone.end();
  if (!count) { sendError("MIC_READ_TIMEOUT"); return; }
  double rms = count ? sqrt((double)sumSquares / count) : 0;
  double mean = (double)sum / count;
  double acRms = sqrt(fmax(0.0, (double)sumSquares / count - mean * mean));
  Serial.printf("{\"event\":\"mic_probe\",\"samples\":%lu,\"nonzero\":%lu,\"clipped\":%lu,\"min\":%d,\"max\":%d,\"rms\":%.2f,\"mean\":%.2f,\"ac_rms\":%.2f,\"right_rms\":%.2f,\"warmup_frames\":%lu,\"sample_rate\":16000,\"audio_saved\":false,\"audio_uploaded\":false}\n",
                (unsigned long)count, (unsigned long)nonzero, (unsigned long)clipped, low, high, rms, mean, acRms,
                sqrt((double)rightSquares / count), (unsigned long)warmupFrames);
}

void captureAudio(uint8_t seconds,bool toNetwork=false) {
  microphone.setPins(MIC_SCK, MIC_WS, -1, MIC_SD);
  if (!microphone.begin(I2S_MODE_STD, 16000, I2S_DATA_BIT_WIDTH_32BIT, I2S_SLOT_MODE_STEREO, I2S_STD_SLOT_BOTH)) {
    if(toNetwork)networkLink.cancel();
    sendError("MIC_INIT_FAILED"); microphone.end(); return;
  }
  captureActive = true; captureCancelled = false;
  captureDurationSeconds = seconds;
  displayNoticeUntil = 0;
  hostTask=""; hostTaskUntil=0;
  deviceState = "PREPARING";
  displayTick(true);
  uint32_t target = (uint32_t)seconds * 16000, count = 0, warmup = 0, sequence = 0;
  uint32_t started = millis();
  float previous = 0, filtered = 0;
  bool failed = false;
  uint8_t networkFrame[1000];size_t networkUsed=0;
  Serial.printf("{\"event\":\"audio_start\",\"sample_rate\":16000,\"channels\":1,\"bits\":16,\"max_samples\":%lu}\n",(unsigned long)target);
  while (!captureCancelled && (warmup < 16384 || count < target)) {
    readAndProcessCommands();
    if(toNetwork) {
      networkLink.tick(false,quietMode);
      if(!networkLink.view().record)captureCancelled=true;
    }
    if (captureCancelled) break;
    if ((uint32_t)(millis() - started) > ((uint32_t)seconds + 5) * 1000) { failed = true; break; }
    size_t received = 0;
    esp_err_t result = i2s_channel_read(microphone.rxChan(), microphoneBuffer, sizeof(microphoneBuffer), &received, 150);
    if (result != ESP_OK && !received) { failed = true; break; }
    int16_t pcm[512]; size_t samples = 0;
    for (size_t i = 0; i + 1 < received / sizeof(int32_t); i += 2) {
      float input = (int16_t)(microphoneBuffer[i] >> 16);
      // DC blocker for voice input; diagnostic MIC_PROBE still reports raw samples.
      filtered = input - previous + 0.995f * filtered; previous = input;
      if (warmup < 16384) { warmup++; continue; }
      if (count >= target) break;
      pcm[samples++] = (int16_t)max(-32768.0f,min(32767.0f,filtered));
      count++;
    }
    if (samples) {
      if (sequence == 0) { deviceState = "LISTENING"; displayTick(true); }
      if(toNetwork) {
        const uint8_t* bytes=(const uint8_t*)pcm;
        for(size_t offset=0;offset<samples*2;) {
          size_t size=min(sizeof(networkFrame)-networkUsed,samples*2-offset);
          memcpy(networkFrame+networkUsed,bytes+offset,size);networkUsed+=size;offset+=size;
          if(networkUsed==sizeof(networkFrame)) {
            if(!networkLink.queuePcm(networkFrame,networkUsed)){failed=true;break;}
            networkUsed=0;sequence++;
          }
        }
        if(failed)break;
      } else {
        unsigned char encoded[1369]; size_t encodedLength = 0;
        if (mbedtls_base64_encode(encoded,sizeof(encoded),&encodedLength,(const unsigned char*)pcm,samples*2)) { failed = true; break; }
        Serial.printf("{\"event\":\"audio_chunk\",\"seq\":%lu,\"data\":\"",(unsigned long)sequence++);
        Serial.write(encoded,encodedLength); Serial.println("\"}");
      }
    }
    // Keep OLED transfers out of the sustained UART stream to avoid RX backlog.
    delay(0);
  }
  microphone.end();
  captureActive = false;
  if(toNetwork) {
    if(networkUsed&&!failed&&!captureCancelled&&!networkLink.queuePcm(networkFrame,networkUsed))failed=true;
    if(failed||captureCancelled||!networkLink.finishUpload())networkLink.cancel();
    memset(networkFrame,0,sizeof(networkFrame));
  }
  deviceState = quietMode ? "QUIET" : "IDLE";
  displayNotice = failed || captureCancelled ? "已停止" : "收音完成";
  displayNoticeDetail = failed || captureCancelled ? "STOPPED" : toNetwork?"OWNER COMPUTER":"LOCAL ONLY";
  displayNoticeUntil = millis()+2500;
  displayTick(true);
  Serial.printf("{\"event\":\"audio_end\",\"samples\":%lu,\"chunks\":%lu,\"cancelled\":%s,\"failed\":%s,\"local_transfer\":%s,\"network_transfer\":%s,\"cloud_ai\":false}\n",
                (unsigned long)count,(unsigned long)sequence,captureCancelled?"true":"false",failed?"true":"false",toNetwork?"false":"true",toNetwork?"true":"false");
}

// Runs on the Arduino task. No network callback touches I2S or this state.
void networkAudioTick() {
  NetworkLink::View current=networkLink.view();
  // Cache and I2S have one owner: this task. Network callbacks only enqueue.
  // Lost connections discard the current turn; no unverified auto-resume.
  if(quietMode||current.epoch!=networkEpoch||!current.turn) {
    if(networkPlaying)speaker.fadeStop("play_stopped","NETWORK_STOPPED");
    networkReplyCache.clear();networkPlaying=networkBuffering=networkDraining=false;
    networkCacheBytes=0;networkCaptureSeconds=0;networkEpoch=current.epoch;resumePending=false;
    while(networkLink.receive(networkIoPacket)){}memset(networkIoPacket.data,0,sizeof(networkIoPacket.data));return;
  }
  if(networkCaptureSeconds) {
    if(current.record){uint8_t seconds=networkCaptureSeconds;networkCaptureSeconds=0;networkCapture=true;
      captureAudio(seconds,true);networkCapture=false;return;}
    if((uint32_t)(millis()-networkRequestedAt)>10000){networkLink.cancel();networkCaptureSeconds=0;}return;
  }
  if(networkDraining) {
    if(!speaker.active()) {
      if(speaker.completed())networkLink.played(networkReply.id,networkLink.replySampleCount(),networkReply.epoch);
      else networkLink.cancel();
      networkReplyCache.clear();networkPlaying=networkDraining=false;
    }return;
  }
  if(networkPlaying) {
    uint8_t compressed[250],pcm[1000];size_t count=min((uint32_t)sizeof(compressed),networkReplyCache.remaining());
    if(!count||!networkReplyCache.read(compressed,count)){networkLink.cancel();return;}
    size_t bytes=xnu_adpcm_decode_bytes(&networkReplyDecodeState,compressed,count,pcm,sizeof(pcm),
                                        networkLink.replySampleCount()-networkReplyDecoded);
    bool ok=bytes&&speaker.networkWrite(pcm,bytes);networkReplyDecoded+=bytes/2;
    xnu_spool_zero(compressed,sizeof(compressed));xnu_spool_zero(pcm,sizeof(pcm));
    if(!ok){networkLink.cancel();return;}
    if(!networkReplyCache.remaining()){
      if(networkReplyDecoded==networkLink.replySampleCount()&&speaker.networkFinish())networkDraining=true;
      else networkLink.cancel();
    }return;
  }
  NetworkLink::Packet& item=networkIoPacket;if(!networkLink.receive(item))return;
  if(item.epoch!=current.epoch){networkLink.cancel();return;}
  if(!networkBuffering){
    uint32_t preparingAt=millis();
    if(item.kind!=NetworkLink::REPLY_BEGIN||!networkLink.replyCompressed()||
       !networkReplyCache.attach(false)||!networkReplyCache.begin(item.count)){networkLink.cancel();return;}
    networkCachePrepareMs=millis()-preparingAt;networkCacheWriteMs=0;
    networkReply=item;networkBuffering=true;networkCacheBytes=0;
    networkReplyDecodeState=networkLink.replyInitialState();networkReplyDecoded=0;return;
  }
  if(memcmp(item.id,networkReply.id,16)){networkLink.cancel();return;}
  if(item.kind==NetworkLink::REPLY_PCM){
    uint32_t writingAt=millis();bool ok=networkReplyCache.append(networkCacheBytes,item.data,item.size);
    networkCacheWriteMs+=millis()-writingAt;networkCacheBytes+=item.size;
    xnu_spool_zero(item.data,sizeof(item.data));if(!ok)networkLink.cancel();return;
  }
  if(item.kind!=NetworkLink::REPLY_END||item.count!=networkReply.count||item.size!=32){networkLink.cancel();return;}
  uint32_t sealingAt=millis();
  if(!networkReplyCache.seal(item.data)){networkLink.cancel();return;}
  Serial.printf("{\"event\":\"reply_cache_timing\",\"bytes\":%lu,\"prepare_ms\":%lu,\"write_ms\":%lu,\"seal_ms\":%lu}\n",(unsigned long)networkCacheBytes,(unsigned long)networkCachePrepareMs,(unsigned long)networkCacheWriteMs,(unsigned long)(millis()-sealingAt));
  // The worker may disconnect or the owner may cancel during Flash verification.
  current=networkLink.view();
  if(!current.ready||!current.turn||current.epoch!=networkReply.epoch){networkReplyCache.clear();return;}
  if(!speaker.networkStart(networkReply.id,networkLink.replySampleCount()*2,quietMode)){networkLink.cancel();return;}
  networkPlaying=true;networkBuffering=false;displayTick(true);
}

void connectWifi() {
  if (savedSsid.isEmpty() || wifiAttempts >= MAX_WIFI_ATTEMPTS) return;
  WiFi.mode(accessPointActive ? WIFI_AP_STA : WIFI_STA);
  // Interactive audio needs predictable packet latency on a phone hotspot.
  bool lowLatency=WiFi.setSleep(false);
  Serial.printf("{\"event\":\"wifi_low_latency\",\"applied\":%s}\n",lowLatency?"true":"false");
  WiFi.begin(savedSsid.c_str(), savedPassword.c_str());
  wifiAttempts++;
  wifiConnecting = true;
  wifiStartedAt = millis();
  if (!quietMode) deviceState = "WIFI_CONNECTING";
}

void stopSetup() {
  setupServer.stop();
  WiFi.softAPdisconnect(true);
  accessPointActive = false;
  WiFi.mode(savedSsid.isEmpty() ? WIFI_OFF : WIFI_STA);
}

bool setupClientAllowed() {
  IPAddress address = setupServer.client().remoteIP();
  return accessPointActive && address[0] == 192 && address[1] == 168 && address[2] == 4;
}

void startSetup() {
  if (accessPointActive) { sendError("SETUP_ALREADY_ACTIVE"); return; }
  accessPointName = "XiaoNuo-" + String((uint32_t)ESP.getEfuseMac() & 0xffffff, HEX);
  accessPointPassword = settings.getString("apkey", "");
  if (accessPointPassword.length() != 12) {
    accessPointPassword = randomHex().substring(0, 12);
    settings.putString("apkey", accessPointPassword);
  }
  setupToken = randomHex();
  WiFi.mode(WIFI_AP_STA);
  if (!WiFi.softAP(accessPointName.c_str(), accessPointPassword.c_str(), 1, false, 1)) { sendError("SETUP_START_FAILED"); return; }
  accessPointActive = true;
  setupExpiresAt = millis() + SETUP_TIMEOUT_MS;
  setupServer.on("/", HTTP_GET, []() {
    if (!setupClientAllowed()) { setupServer.send(403, "text/plain", "Setup network only"); return; }
    String html = F("<!doctype html><html lang='zh-CN'><meta charset='utf-8'><meta name='viewport' content='width=device-width,initial-scale=1'><title>小糯配网</title><body><h2>小糯 Wi-Fi 配置</h2><p>请选择可用的 2.4GHz 网络。密码仅保存到设备，不会显示或上传。</p><form method='post' action='/configure'><input type='hidden' name='token' value='");
    html += setupToken;
    html += F("'><p><label>Wi-Fi 名称 <input name='ssid' maxlength='32' required></label></p><p><label>Wi-Fi 密码 <input name='password' type='password' minlength='8' maxlength='63' autocomplete='new-password' required></label></p><p><label><input type='checkbox' name='pcwake' value='1' ");
    if(computerWakeEnabled)html += "checked ";
    html += F(">允许电脑唤醒：短音频加密发送到主人的电脑识别，不保存原始录音；只有叫到小糯后才进行聊天。</label></p><button>保存并连接</button></form></body></html>");
    setupServer.sendHeader("Cache-Control", "no-store");
    setupServer.send(200, "text/html; charset=utf-8", html);
  });
  setupServer.on("/configure", HTTP_POST, []() {
    if (!setupClientAllowed() || setupServer.arg("token") != setupToken) { setupServer.send(403, "text/plain", "Invalid setup session"); return; }
    String ssid = setupServer.arg("ssid");
    String password = setupServer.arg("password");
    if (ssid.length() < 1 || ssid.length() > 32 || password.length() < 8 || password.length() > 63) { setupServer.send(422, "text/plain", "Invalid network settings"); return; }
    savedSsid = ssid; savedPassword = password;
    computerWakeEnabled=setupServer.arg("pcwake")=="1";settings.putBool("pcwake",computerWakeEnabled);
    settings.putString("ssid", savedSsid); settings.putString("pass", savedPassword);
    wifiAttempts = 0;
    setupServer.sendHeader("Cache-Control", "no-store");
    setupServer.send(200, "text/html; charset=utf-8", "<meta charset='utf-8'><p>配置已保存，正在尝试连接。请查看屏幕或控制台连接状态。</p>");
    delay(100);
    stopSetup();
    setupFallback=false;
    deviceState="WIFI_CONNECTING";
    displayTick(true);
    Serial.println("{\"event\":\"setup_saved\"}");
    connectWifi();
  });
  setupServer.onNotFound([]() { setupServer.send(404, "text/plain", "Not found"); });
  setupServer.begin();
  deviceState = "WIFI_SETUP";
  displayTick(true);
  Serial.printf("{\"event\":\"setup_ready\",\"ssid\":\"%s\",\"ip\":\"192.168.4.1\",\"password_on_oled\":%s,\"expires_seconds\":600}\n", accessPointName.c_str(), screen ? "true" : "false");
}

void wifiTick() {
  uint32_t now = millis();
  if (accessPointActive) {
    setupServer.handleClient();
    if ((int32_t)(now - setupExpiresAt) >= 0) stopSetup();
  }
  if (wifiConnecting) {
    if (WiFi.status() == WL_CONNECTED) {
      wifiConnecting = false;
      // A successful association ends this failure streak. A later outage
      // must start with the first retry, rather than inherit old attempts.
      wifiAttempts = 0;
      wifiNextAttempt = now;
      if (!quietMode) deviceState = "IDLE";
      Serial.println("{\"event\":\"wifi_connected\"}");
      sendStatus();
    } else if ((uint32_t)(now - wifiStartedAt) >= WIFI_CONNECT_TIMEOUT_MS) {
      WiFi.disconnect(false, false);
      wifiConnecting = false;
      wifiNextAttempt = now + (wifiAttempts>=MAX_WIFI_ATTEMPTS ? WIFI_RETRY_COOLDOWN_MS : min((uint32_t)(2000UL << min((int)wifiAttempts, 4)), (uint32_t)30000));
      if (!quietMode) deviceState = "OFFLINE";
    }
  } else if (!savedSsid.isEmpty() && WiFi.status() != WL_CONNECTED && (int32_t)(now - wifiNextAttempt) >= 0) {
    if (wifiAttempts>=MAX_WIFI_ATTEMPTS) { wifiAttempts=0; wifiRetryCycles++; }
    connectWifi();
  }
  // A robot moved to a new home must be recoverable without a serial cable.
  if (!savedSsid.isEmpty() && !accessPointActive && !wifiConnecting && !setupFallback &&
      WiFi.status()!=WL_CONNECTED && wifiRetryCycles>=2 && (uint32_t)(now-bootAt)>=10UL*60UL*1000UL) {
    setupFallback=true;
    stopSetup();
    startSetup();
  }
}

void handleCommand(String line) {
  line.trim();
  if (line.isEmpty()) return;
  if (line=="AUDIO_STATUS") { speaker.report(); return; }
  if (line=="WIFI_DIAG") { reportWifiDiagnostics(); return; }
  if (line=="HTTP_PROBE") {
    if (WiFi.status()!=WL_CONNECTED) { sendError("HTTP_PROBE_WIFI_OFFLINE"); return; }
    unsigned long before=ESP.getFreeHeap(),started=millis();
    networkLink.suspend(true); delay(2500);
    const char* probeHost=XiaoNuoEndpoint::HOST;
    IPAddress probeIp;
    bool dns=WiFi.hostByName(probeHost,probeIp)==1;
    WiFiClientSecure client; client.setTimeout(15000);
    bool ok=dns&&client.connect(probeIp,443,probeHost,XiaoNuoEndpoint::CA_PEM,nullptr,nullptr);
    int status=0;
    if(ok){
      client.print(String("GET /healthz HTTP/1.1\r\nHost: ")+probeHost+"\r\nConnection: close\r\n\r\n");
      String first=client.readStringUntil('\n');
      if(first.startsWith("HTTP/")) status=first.substring(9).toInt();
      client.stop();
    }
    networkLink.suspend(false);
    Serial.printf("{\"event\":\"http_probe\",\"ok\":%s,\"status\":%d,\"dns\":%s,\"ip\":\"%s\",\"heap_before\":%lu,\"heap_after\":%lu,\"elapsed_ms\":%lu}\n",
                  status==200?"true":"false",status,dns?"true":"false",probeIp.toString().c_str(),before,ESP.getFreeHeap(),(unsigned long)(millis()-started));
    return;
  }
  if (line=="HTTP_RANGE_TEST") {
    if (WiFi.status()!=WL_CONNECTED) { sendError("HTTP_RANGE_TEST_WIFI_OFFLINE"); return; }
    const char* host=XiaoNuoEndpoint::HOST;
    wakeRecorder.pause();
    unsigned long before=ESP.getFreeHeap();
    if(!networkLink.shutdown()){sendError("HTTP_NETWORK_STOP_TIMEOUT");return;}
    uint32_t freeWait=millis()+15000;while((int32_t)(freeWait-millis())>0&&ESP.getFreeHeap()<120000)delay(200);
    static HttpRangeClient client;int status=client.begin(host,"/healthz",nullptr,0,100,IPAddress(172,66,47,160),IPAddress(172,66,44,96));
    uint8_t first[16]={};int got=status>0?client.read(first,sizeof(first)):0;
    Serial.printf("{\"event\":\"http_range_test\",\"status\":%d,\"first_bytes\":%d,\"tls_code\":%d,\"tls_text\":\"%s\",\"gateway\":\"%s\",\"dns\":\"%s\",\"range_ip\":\"%s\",\"heap_before\":%lu,\"heap_after\":%lu}\n",
                  status,got,client.errorCode(),client.errorText(),WiFi.gatewayIP().toString().c_str(),
                  WiFi.dnsIP(0).toString().c_str(),client.resolvedIp().toString().c_str(),before,ESP.getFreeHeap());
    client.end();networkLink.restart();return;
  }
  if(line.startsWith("NET_REPLY_TEST ")) {
    unsigned seconds=0;char extra;
    if(sscanf(line.c_str(),"NET_REPLY_TEST %u %c",&seconds,&extra)!=1||seconds<1||seconds>45||
       quietMode||captureActive||speaker.active()||accessPointActive||networkLink.active()){
      sendError("REPLY_PROBE_BLOCKED");return;
    }
    wakeRecorder.pause();
    if(!networkLink.requestReplyProbe(seconds)){sendError("REPLY_PROBE_BLOCKED");return;}
    Serial.printf("{\"event\":\"reply_probe_requested\",\"seconds\":%u,\"raw_audio_recorded\":false}\n",seconds);
    return;
  }
  if (line=="SPEAKER_SELFTEST") {
    if(quietMode||captureActive||accessPointActive||speaker.active()||networkLink.active()) {
      sendError("SPEAKER_SELFTEST_BLOCKED");return;
    }
    wakeRecorder.pause();
    uint8_t id[16]={0};static uint8_t pcm[1000];
    const uint32_t samples=16000*4;
    bool ok=speaker.networkStart(id,samples*2,false);
    for(uint32_t offset=0;ok&&offset<samples;) {
      size_t count=min((uint32_t)500,samples-offset);
      for(size_t i=0;i<count;i++) {
        int16_t value=(int16_t)(12000*sin(2*3.141592653589793*440*(offset+i)/16000));
        pcm[2*i]=(uint8_t)value;pcm[2*i+1]=(uint8_t)((uint16_t)value>>8);
      }
      ok=speaker.networkWrite(pcm,count*2);offset+=count;
    }
    if(ok)ok=speaker.networkFinish();
    uint32_t deadline=millis()+2000;
    while(speaker.active()&&(int32_t)(deadline-millis())>0){speaker.tick(false);delay(5);}
    ok=ok&&speaker.completed()&&speaker.progress()==samples;
    if(speaker.active())speaker.fadeStop("play_error","SELFTEST_INCOMPLETE");
    speaker.report();
    Serial.printf("{\"event\":\"speaker_selftest\",\"ok\":%s,\"audible_verified\":false}\n",ok?"true":"false");
    return;
  }
  if(line=="SPOOL_SELFTEST") {
    if(WiFi.status()!=WL_CONNECTED||quietMode||captureActive||accessPointActive||speaker.active()||networkLink.active()){
      sendError("SPOOL_SELFTEST_BLOCKED");return;
    }
    wakeRecorder.pause();
    if(!networkLink.shutdown()){sendError("SPOOL_SELFTEST_STOP_TIMEOUT");return;}
    Serial.println("{\"event\":\"spool_selftest_started\",\"synthetic_only\":true}");
    uint32_t started=millis();auto result=runReplySpoolSelftest();
    networkLink.restart();
    Serial.printf("{\"event\":\"spool_selftest\",\"ok\":%s,\"phase\":%u,\"checks\":%u,\"verified_bytes\":%lu,\"elapsed_ms\":%lu,\"synthetic_only\":true}\n",
      result.ok?"true":"false",result.phase,result.checks,(unsigned long)result.verified,(unsigned long)(millis()-started));return;
  }
  if (line=="HTTP_PLAY_TEST"||line=="HTTP_PLAY_TEST SMALL"||line=="HTTP_PLAY_TEST ALT"||line=="HTTP_PLAY_TEST SMALL ALT") {
    if (WiFi.status()!=WL_CONNECTED) { sendError("HTTP_PLAY_TEST_WIFI_OFFLINE"); return; }
    if(quietMode||captureActive||accessPointActive||speaker.active()||networkLink.active()) {
      sendError("HTTP_PLAY_TEST_BLOCKED");return;
    }
    // Close the input I2S channel and release the wake buffer before output/TLS.
    wakeRecorder.pause();
    const char* host=XiaoNuoEndpoint::HOST;
    String token=binding.tokenValue();
    uint8_t digest[32];char deviceHash[65]={};
    mbedtls_sha256((const unsigned char*)token.c_str(),token.length(),digest,0);
    XiaoNuoWire::digestText(digest,deviceHash);
    String path=String("/reply/")+deviceHash+"/22222222-2222-2222-2222-222222222222";
    String auth=binding.bearerToken();
    unsigned long before=ESP.getFreeHeap();
    if(!networkLink.shutdown()){sendError("HTTP_NETWORK_STOP_TIMEOUT");return;}
    NetworkMtuProbe smallPackets(line=="HTTP_PLAY_TEST SMALL"||line=="HTTP_PLAY_TEST SMALL ALT");
    uint16_t httpPort=line.endsWith(" ALT")?8443:443;
    // Keep a complete bounded ADPCM segment before starting I2S. Download
    // stalls must not consume the speaker's three-second streaming watchdog.
    uint8_t* compressed=static_cast<uint8_t*>(malloc(32000));
    if(!compressed){sendError("HTTP_AUDIO_NO_MEMORY");smallPackets.restore();networkLink.restart();return;}
    static HttpRangeClient client;
    int status=0,total=32000;uint32_t downloaded=0,fetches=0;
    uint32_t downloadStarted=millis();const char* downloadCode="HTTP_AUDIO_INCOMPLETE";
    // Retry only a missing suffix, never duplicate already received bytes.
    for(unsigned attempt=0;attempt<2&&downloaded<32000;attempt++) {
      fetches++;
      status=client.begin(host,path.c_str(),auth.c_str(),downloaded,32000-downloaded,
        IPAddress(172,66,47,160),IPAddress(172,66,44,96),httpPort);
      if(status!=206||client.remainingBytes()!=int(32000-downloaded)) {
        downloadCode="HTTP_AUDIO_RESPONSE";break;
      }
      uint32_t lastProgress=millis();
      while(downloaded<32000) {
        int got=client.read(compressed+downloaded,min((size_t)1000,(size_t)(32000-downloaded)));
        if(got>0){downloaded+=got;lastProgress=millis();}
        else if(!client.connected()){downloadCode="HTTP_AUDIO_CLOSED";break;}
        else if((uint32_t)(millis()-lastProgress)>10000){downloadCode="HTTP_AUDIO_READ_TIMEOUT";break;}
        else delay(5);
        if((uint32_t)(millis()-downloadStarted)>90000){downloadCode="HTTP_AUDIO_DOWNLOAD_TIMEOUT";break;}
      }
      client.end();
      if((uint32_t)(millis()-downloadStarted)>90000)break;
    }
    client.end();smallPackets.restore();
    uint32_t downloadMs=millis()-downloadStarted;
    if(downloaded==32000)downloadCode="";
    uint8_t id[16]={0};const uint32_t samples=64000;
    bool started=downloaded==32000&&speaker.networkStart(id,samples*2,false);
    static XnuHttpPlayback playback;
    xnu_http_playback_begin(&playback,samples);
    auto write=[](void* context,const uint8_t* pcm,size_t bytes)->int {
      return static_cast<SpeakerStream*>(context)->networkWrite(pcm,bytes)?1:0;
    };
    bool ok=started&&xnu_http_playback_push(&playback,compressed,32000,write,&speaker)&&
      xnu_http_playback_complete(&playback);
    memset(compressed,0,32000);free(compressed);
    if(ok)ok=speaker.networkFinish();
    Serial.printf("{\"event\":\"http_download\",\"downloaded_bytes\":%lu,\"fetch_attempts\":%lu,\"download_ms\":%lu,\"download_code\":\"%s\"}\n",
      (unsigned long)downloaded,(unsigned long)fetches,(unsigned long)downloadMs,downloadCode);
    uint32_t waitUntil=millis()+2000;
    while(ok&&speaker.active()&&(int32_t)(waitUntil-millis())>0){speaker.tick(false);delay(5);}
    ok=ok&&speaker.completed()&&speaker.progress()==samples;
    if(!ok&&speaker.active())speaker.fadeStop("play_error","HTTP_AUDIO_INCOMPLETE");
    speaker.report();
    Serial.printf("{\"event\":\"http_transport\",\"status\":%d,\"tls_code\":%d,\"peer_ipv4\":\"%s\",\"clock_valid\":%s}\n",
      status,client.errorCode(),client.resolvedIp().toString().c_str(),time(nullptr)>=1704067200?"true":"false");
    reportWifiDiagnostics();
    Serial.printf("{\"event\":\"http_play_test\",\"status\":%d,\"bytes\":%d,\"downloaded_bytes\":%lu,\"samples\":%lu,\"written\":%lu,\"started\":%s,\"ok\":%s,\"heap_before\":%lu,\"heap_after\":%lu}\n",
      status,total,(unsigned long)downloaded,(unsigned long)samples,
      (unsigned long)(playback.decoded*2),started?"true":"false",ok?"true":"false",before,ESP.getFreeHeap());
    smallPackets.restore();networkLink.restart();return;
  }
  if(line=="WIFI_FORGET") {
    networkLink.cancel();
    settings.remove("ssid"); settings.remove("pass");
    savedSsid=""; savedPassword=""; wifiConnecting=false; wifiAttempts=0; setupFallback=false;
    if(accessPointActive)stopSetup();
    startSetup(); return;
  }
  if(line=="NET_WAKE ON"||line=="NET_WAKE OFF") {
    computerWakeEnabled=line=="NET_WAKE ON";settings.putBool("pcwake",computerWakeEnabled);
    wakeRecorder.pause();if(!computerWakeEnabled&&networkLink.active())networkLink.cancel();sendStatus();return;
  }
  if(line=="NET_CANCEL"||(line=="PLAY_STOP"&&networkLink.active())) {
    networkLink.cancel();if(networkCapture)captureCancelled=true;if(networkPlaying)speaker.stop();return;
  }
  if(line=="NET_PERMIT_PROBE") {
    // USB-only control-plane diagnostic. Never opens I2S or sends PCM.
    if(computerWakeEnabled||quietMode||speaker.active()||captureActive||accessPointActive||!networkLink.requestTalk(6,true)){sendError("PERMIT_PROBE_BLOCKED");return;}
    Serial.println("{\"event\":\"permit_probe\",\"accepted\":true}");return;
  }
  if (line == "CAPTURE_CANCEL") { captureCancelled = true; return; }
  if(networkLink.active()&&line!="INFO"&&line!="PING"&&line!="LINK_STATUS"&&line!="STATE QUIET") {sendError("DEVICE_BUSY");return;}
  if(line.startsWith("PLAY_START ")||line.startsWith("CAPTURE ")||line=="MIC_PROBE"||line=="WIFI_SETUP"||line.startsWith("PAIR ")||line.startsWith("UNPAIR "))wakeRecorder.pause();
  bool wasSpeaking=speaker.active();
  if (speaker.command(line,quietMode,captureActive)) { if (!wasSpeaking && speaker.active()) displayTick(true); return; }
  if (speaker.active() && line!="INFO" && line!="PING" && line!="STATE QUIET") { sendError("DEVICE_BUSY"); return; }
  if (captureActive && line != "INFO" && line != "PING" && line != "STATE QUIET" && line != "STATE IDLE") {
    sendError("DEVICE_BUSY"); return;
  }
  if (binding.command(line,settings)) return;
  if (line == "INFO" || line == "PING") { lastHostAt = millis(); sendStatus(); }
  else if (line == "LINK_STATUS") networkLink.status();
  else if(line=="NET_DIAG") { wakeRecorder.diagnostics();IPAddress address;bool resolved=WiFi.hostByName(XiaoNuoEndpoint::HOST,address)==1&&address!=IPAddress(0,0,0,0);Serial.printf("{\"event\":\"network_diagnostics\",\"wake_state\":\"%s\",\"wake_mic_open\":%s,\"clock\":%lu,\"free_heap\":%u,\"largest_block\":%u,\"dns_ok\":%s,\"peer_ipv4\":\"%s\"}\n",wakeRecorder.state(),wakeRecorder.listening()?"true":"false",(unsigned long)time(nullptr),ESP.getFreeHeap(),ESP.getMaxAllocHeap(),resolved?"true":"false",address.toString().c_str()); }

  else if(line.length()==10&&line.startsWith("NET_TALK ")&&line[9]>='2'&&line[9]<='6') {
    wakeRecorder.pause();
    if(quietMode||speaker.active()||captureActive||accessPointActive||!networkLink.requestTalk(line[9]-'0')){sendError("NETWORK_TALK_BLOCKED");return;}
    // A handshake can finish between the last display tick and this command.
    // Bind this explicit request to its accepted epoch instead of dropping it
    // as an old turn on the next Arduino tick.
    networkEpoch=networkLink.view().epoch;
    networkPlaying=networkBuffering=networkDraining=false;
    while(networkLink.receive(networkIoPacket)){}
    networkCaptureSeconds=line[9]-'0';networkRequestedAt=millis();displayNoticeUntil=0;
  }
  else if (line == "I2C_SCAN") scanI2C();
  else if (line == "SCREEN_TEST") {
    if (!screen) { sendError("OLED_NOT_ENABLED"); return; }
    screen->setFont(xiaonuo_status_font);
    const uint16_t glyphs[]={0x663e,0x793a,0x6d4b,0x8bd5,0x8bf7,0x8bf4,0x8bdd,0x51c6,0x5907,0x4e2d,0x6536,0x97f3,0x5b8c,0x6210,0x6211,0x5728,0x5df2,0x505c,0x6b62,0x8bc6,0x522b,0x601d,0x8003,0x56de,0x590d};
    bool glyphsReady=true;
    for (uint16_t glyph:glyphs) if (u8g2_GetGlyphWidth(screen->getU8g2(),glyph)<=0) glyphsReady=false;
    displayNotice="显示测试"; displayNoticeDetail="SCREEN TEST";
    displayNoticeUntil=millis()+12000; displayTick(true);
    Serial.printf("{\"event\":\"screen_test\",\"duration_seconds\":12,\"glyphs_ready\":%s,\"visual_verified\":false}\n",glyphsReady?"true":"false");
  }
  else if (line=="TASK RECOGNIZING" || line=="TASK THINKING" || line=="TASK SYNTHESIZING" || line=="TASK CLEAR") {
    hostTask=line=="TASK CLEAR"?"":line.substring(5);
    lastHostAt=millis(); hostTaskUntil=millis()+120000;
    displayTick(true);
    Serial.printf("{\"event\":\"task_status\",\"task_state\":\"%s\",\"visual_verified\":false}\n",hostTask.c_str());
  }
  else if (line == "OLED ssd1306") enableScreen("ssd1306", true);
  else if (line == "OLED sh1106") enableScreen("sh1106", true);
  else if (line == "OLED off") { if (screen) screen->setPowerSave(1); screen = nullptr; screenDriver = "none"; settings.remove("oled"); Serial.println("{\"event\":\"oled_off\"}"); }
  else if (line == "MIC_PROBE") microphoneProbe();
  else if (line.length() == 9 && line.startsWith("CAPTURE ") && line[8] >= '2' && line[8] <= '6') captureAudio(line[8]-'0');
  else if (line == "STATE QUIET" || line == "STATE IDLE") {
    bool requestedQuiet = line == "STATE QUIET";
    if (quietMode != requestedQuiet) settings.putBool("quiet", requestedQuiet);
    quietMode = requestedQuiet;
    if (quietMode && speaker.active()) speaker.stop();
    if (quietMode && captureActive) captureCancelled = true;
    if(quietMode&&networkLink.active())networkLink.cancel();
    deviceState = quietMode ? "QUIET" : "IDLE";
    lastHostAt = millis(); displayTick(true); sendStatus();
  }
  else if (line == "WIFI_SETUP") startSetup();
  else if (line == "WIFI_STOP_SETUP") { stopSetup(); sendStatus(); }
  else if (line == "WIFI_RETRY") { wifiAttempts = 0; wifiNextAttempt = millis(); connectWifi(); sendStatus(); }
  else sendError("UNKNOWN_OR_DISABLED_COMMAND");
}

void setup() {
  // I2S output is not initialized after reset.
  pinMode(AMP_BCLK, INPUT_PULLDOWN);
  pinMode(AMP_LRC, INPUT_PULLDOWN);
  pinMode(AMP_DIN, INPUT_PULLDOWN);
  Serial.setRxBufferSize(4096);
  Serial.begin(SERIAL_BAUD);
  WiFi.onEvent([](WiFiEvent_t event,WiFiEventInfo_t info) {
    portENTER_CRITICAL(&wifiDiagnosticMux);
    wifiDisconnectReason=info.wifi_sta_disconnected.reason;
    wifiDisconnectAt=millis();wifiDisconnectCount++;
    portEXIT_CRITICAL(&wifiDiagnosticMux);
  },ARDUINO_EVENT_WIFI_STA_DISCONNECTED);
  commandLine.reserve(MAX_COMMAND_LENGTH);
  settings.begin("xnuo-v03", false);
  snprintf(deviceId,sizeof(deviceId),"esp32-%012llx",(unsigned long long)(ESP.getEfuseMac() & 0xffffffffffffULL));
  binding.load(settings);
  if(networkReplyCache.attach(false))networkLink.begin(binding,deviceId,VERSION);
  else sendError("REPLY_CACHE_UNAVAILABLE");
  quietMode = settings.getBool("quiet", false);
  computerWakeEnabled=settings.getBool("pcwake",true);
  if (quietMode) deviceState = "QUIET";
  String driver = settings.getString("oled", "");
  if (!driver.isEmpty()) enableScreen(driver, false);
  WiFi.persistent(false);
  WiFi.setAutoReconnect(false);
  WiFi.mode(WIFI_OFF);
  savedSsid = settings.getString("ssid", "");
  savedPassword = settings.getString("pass", "");
  bootAt = millis();
  if (!savedSsid.isEmpty()) connectWifi(); else startSetup();
  Serial.printf("{\"event\":\"boot\",\"firmware\":\"%s\",\"speaker_enabled\":false}\n", VERSION);
  sendStatus();
}

void readAndProcessCommands() {
  size_t consumed = 0;
  // Audio must drain UART faster than real-time PCM. The normal command budget
  // deliberately yields often, but would starve I2S between small chunks.
  const size_t budget = speaker.active() ? 2048 : 128;
  while (Serial.available() && consumed++ < budget) {
    char byte = (char)Serial.read();
    if (speaker.expectsBinary()) { speaker.consume((uint8_t)byte); continue; }
    if (byte == '\n') {
      String ready = commandLine; bool overflow = droppingCommand;
      commandLine = ""; droppingCommand = false;
      if (!overflow) handleCommand(ready);
    }
    else if (byte != '\r') {
      if (droppingCommand) continue;
      if (commandLine.length() < MAX_COMMAND_LENGTH) commandLine += byte;
      else { commandLine = ""; droppingCommand = true; sendError("COMMAND_TOO_LONG"); }
    }
  }
}

void loop() {
  readAndProcessCommands();
  speaker.tick(quietMode);
  if (!speaker.active()) wifiTick();
  networkLink.tick((speaker.active()&&!networkPlaying)||(captureActive&&!networkCapture)||accessPointActive,quietMode);
  wakeRecorder.tick(computerWakeEnabled,quietMode,speaker.active()||captureActive||accessPointActive);
  static uint32_t reminderPollAt=0,reminderBusyAt=0;
  NetworkLink::View reminderView=networkLink.view();uint32_t reminderNow=millis();
  if(reminderView.turn||speaker.active()||captureActive||accessPointActive)reminderBusyAt=reminderNow;
  if(xnu_reminder_poll_due(reminderNow,reminderPollAt,reminderBusyAt,computerWakeEnabled,reminderView.ready,reminderView.reminders,reminderView.turn,quietMode||reminderView.pcQuiet,!wakeRecorder.reminderIdle())) {
    reminderPollAt=reminderNow;
    if(networkLink.requestReminder())wakeRecorder.pause();
  }
  networkAudioTick();
  displayTick();
  delay(speaker.active() ? 1 : 2);
}











