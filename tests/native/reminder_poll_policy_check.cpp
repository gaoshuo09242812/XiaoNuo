#include "reminder_poll_policy.h"
static int cases,failed;
#define CHECK(x) do{cases++;if(!(x)){failed=__LINE__;return -cases;}}while(0)
extern "C" __declspec(dllexport) int failed_line(){return failed;}
extern "C" __declspec(dllexport) int run_checks(){
  cases=failed=0;
  for(unsigned mask=0;mask<64;mask++){
    bool result=xnu_reminder_poll_due(60000,0,0,mask&1,mask&2,mask&4,mask&8,mask&16,mask&32);
    CHECK(result==(mask==7));
  }
  CHECK(!xnu_reminder_poll_due(29999,0,0,true,true,true,false,false,false));
  CHECK(xnu_reminder_poll_due(30000,0,0,true,true,true,false,false,false));
  CHECK(!xnu_reminder_poll_due(60000,0,35001,true,true,true,false,false,false));
  CHECK(xnu_reminder_poll_due(60000,0,35000,true,true,true,false,false,false));
  // Millis wrap and rapid loop ticks cannot create an extra poll.
  const uint32_t base=0xfffffff0u;
  for(uint32_t elapsed=0;elapsed<=60000;elapsed++){
    CHECK(xnu_reminder_poll_due(base+elapsed,base,base,true,true,true,false,false,false)==(elapsed>=30000));
  }
  uint32_t last=0;unsigned polls=0;
  for(uint32_t t=0;t<=180000;t+=2){
    if(xnu_reminder_poll_due(t,last,0,true,true,true,false,false,false)){last=t;polls++;}
  }
  CHECK(polls==6);
  return cases;
}
