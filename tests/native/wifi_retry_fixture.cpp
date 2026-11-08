// The runner inserts the real config, connectWifi() and wifiTick() functions.
// Hardware/network calls are doubles; timing and production state transitions
// run unchanged. No Windows/ESP32 radio behavior is simulated by these checks.
typedef unsigned int uint32_t;
typedef int int32_t;
typedef unsigned char uint8_t;
typedef unsigned long long size_t;
template<class T> T min(T a,T b){return a<b?a:b;}
#include "production_config.h"
using namespace XiaoNuoConfig;
enum {WL_CONNECTED=3,WL_DISCONNECTED=6,WIFI_AP_STA=3,WIFI_STA=1};
struct Text {
  const char* value;
  bool isEmpty() const {return !value||!*value;}
  const char* c_str() const {return value;}
  Text& operator=(const char* next){value=next;return *this;}
};
struct Radio {
  int state,beginCalls,disconnectCalls;
  int status(){return state;}
  void mode(int){}
  bool setSleep(bool){return true;}
  void begin(const char*,const char*){beginCalls++;}
  void disconnect(bool,bool){disconnectCalls++;state=WL_DISCONNECTED;}
} WiFi;
struct Output {void println(const char*){} void printf(const char*,...){}} Serial;
struct Server {void handleClient(){}} setupServer;
Text savedSsid={"fixture"},savedPassword={"fixture"},deviceState={"IDLE"};
bool accessPointActive,wifiConnecting,quietMode,setupFallback;
uint8_t wifiAttempts;
uint32_t wifiStartedAt,wifiNextAttempt,wifiRetryCycles,setupExpiresAt,bootAt,clockNow;
uint32_t millis(){return clockNow;}
void sendStatus(){}
void stopSetup(){}
void startSetup(){}
#include "production_wifi_functions.h"
static int failed=0,cases=0;
#define CHECK(x) do {cases++;if(!(x)){failed=__LINE__;return -cases;}}while(0)
static void reset(){
  WiFi.state=WL_DISCONNECTED;WiFi.beginCalls=WiFi.disconnectCalls=0;
  savedSsid="fixture";wifiConnecting=accessPointActive=quietMode=false;
  setupFallback=true;wifiAttempts=0;wifiRetryCycles=0;
  clockNow=1000;wifiNextAttempt=wifiStartedAt=bootAt=0;
}
extern "C" __declspec(dllexport) int failed_line(){return failed;}
extern "C" __declspec(dllexport) int run_checks(){
  failed=cases=0;
  reset();wifiAttempts=4;wifiConnecting=true;wifiNextAttempt=999999;
  WiFi.state=WL_CONNECTED;wifiTick();
  CHECK(!wifiConnecting);CHECK(wifiAttempts==0);CHECK(wifiNextAttempt==clockNow);
  // A new outage starts immediately with a fresh first attempt.
  WiFi.state=WL_DISCONNECTED;clockNow++;wifiTick();
  CHECK(WiFi.beginCalls==1);CHECK(wifiAttempts==1);CHECK(wifiConnecting);
  // Successful reconnection clears the new streak too.
  WiFi.state=WL_CONNECTED;wifiTick();CHECK(wifiAttempts==0);
  for(uint8_t attempt=1;attempt<=5;attempt++){
    reset();clockNow=100000;wifiAttempts=attempt;wifiConnecting=true;
    wifiStartedAt=clockNow-WIFI_CONNECT_TIMEOUT_MS;
    wifiTick();
    CHECK(!wifiConnecting);CHECK(WiFi.disconnectCalls==1);
    uint32_t wait=wifiNextAttempt-clockNow;
    CHECK(wait>=4000&&wait<=30000);
    uint32_t due=wifiNextAttempt;
    clockNow=due-1;wifiTick();CHECK(WiFi.beginCalls==0);
    clockNow=due;wifiTick();CHECK(WiFi.beginCalls==1);
    CHECK(wifiAttempts==(attempt==MAX_WIFI_ATTEMPTS?1:attempt+1));
    CHECK(wifiRetryCycles==(attempt==MAX_WIFI_ATTEMPTS?1:0));
  }
  // Deadline wraps through UINT32_MAX without triggering early or sticking.
  reset();clockNow=0xfffffff0u;wifiAttempts=5;wifiConnecting=true;
  wifiStartedAt=clockNow-WIFI_CONNECT_TIMEOUT_MS;wifiTick();
  uint32_t due=wifiNextAttempt;CHECK(due<30000u);
  clockNow=due-1;wifiTick();CHECK(WiFi.beginCalls==0);
  clockNow=due;wifiTick();CHECK(WiFi.beginCalls==1);CHECK(wifiAttempts==1);
  // No stored hotspot means no forced association or credential overwrite.
  reset();savedSsid="";clockNow=100000;wifiTick();CHECK(WiFi.beginCalls==0);
  // A connection attempt gets its full original twenty-second timeout.
  reset();wifiAttempts=1;wifiConnecting=true;wifiStartedAt=clockNow;
  clockNow+=WIFI_CONNECT_TIMEOUT_MS-1;wifiTick();
  CHECK(wifiConnecting);CHECK(WiFi.disconnectCalls==0);
  return cases;
}
