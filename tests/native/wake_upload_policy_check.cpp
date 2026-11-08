#include "wake_upload_policy.h"
static int cases,failed;
#define CHECK(x) do{cases++;if(!(x)){failed=__LINE__;return -cases;}}while(0)
extern "C" __declspec(dllexport) int failed_line(){return failed;}
extern "C" __declspec(dllexport) int run_checks(){
  cases=failed=0;
  for(uint32_t n=32000;n<=96000;n+=2){
    uint32_t target=xnu_wake_upload_target(n,true,true);
    CHECK(target>=n);CHECK(target<=96000);CHECK(target-n<2000);CHECK(target%2000==0);
    CHECK(xnu_wake_trim_valid(48000,target/2,target,true,true,true));
  }
  CHECK(xnu_wake_upload_target(32000,true,false)==96000);
  CHECK(xnu_wake_upload_target(32000,false,true)==96000);
  CHECK(xnu_wake_upload_target(31999,true,true)==96000);
  CHECK(xnu_wake_upload_target(96001,true,true)==96000);
  CHECK(!xnu_wake_trim_valid(48000,16000,32000,false,true,true));
  CHECK(!xnu_wake_trim_valid(48000,16000,32000,true,false,true));
  CHECK(!xnu_wake_trim_valid(48000,16000,32000,true,true,false));
  CHECK(!xnu_wake_trim_valid(48000,15999,32000,true,true,true));
  CHECK(!xnu_wake_trim_valid(48000,16001,32000,true,true,true));
  CHECK(!xnu_wake_trim_valid(48000,16000,30000,true,true,true));
  CHECK(!xnu_wake_trim_valid(48000,16000,32001,true,true,true));
  CHECK(!xnu_wake_trim_valid(16000,17000,34000,true,true,true));
  CHECK(!xnu_wake_trim_valid(48000,49000,98000,true,true,true));
  return cases;
}
