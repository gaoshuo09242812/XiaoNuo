#include "reply_queue_policy.h"
static int cases,failed;
#define CHECK(x) do{cases++;if(!(x)){failed=__LINE__;return -cases;}}while(0)
extern "C" __declspec(dllexport) int failed_line(){return failed;}
extern "C" __declspec(dllexport) int run_checks(){
  cases=failed=0;
  // All occupancy/remaining combinations, including partial final windows.
  for(unsigned w=4;w<=16;w*=2)for(unsigned remaining=0;remaining<=480;remaining++){
    unsigned need=xnu_reply_queue_need(w,remaining,2);
    CHECK(need==(remaining<2*w?remaining:2*w)+1);
    CHECK(need<=33);
    for(unsigned q=0;q<=42;q++){
      if(42-q>=need){
        // No consumer progress after ACK, both outstanding windows arrive.
        CHECK(q+(remaining<2*w?remaining:2*w)+1<=42);
      }else CHECK(q+need>42);
    }
    CHECK(xnu_reply_queue_need(w,remaining,1)==(remaining?(remaining<w?remaining:w):1));
  }
  // Worst consumer scheduling: receive until each ACK gate, then drain only
  // enough to grant credit. Host starts with two windows and adds one per ACK.
  for(unsigned w=4;w<=16;w*=2)for(unsigned total=1;total<=480;total++){
    unsigned q=1,read=0,hostLimit=total<2*w?total:2*w;
    CHECK(q+hostLimit+1<=42); // BEGIN plus initial burst plus END.
    while(read<total){
      CHECK(xnu_reply_poll_allowed(2,false));CHECK(read<hostLimit);
      q++;read++;CHECK(q<=42);
      if(read%w==0||read==total){
        CHECK(!xnu_reply_poll_allowed(2,true));
        unsigned need=xnu_reply_queue_need(w,total-read,2);
        while(42-q<need){CHECK(q>0);q--;}
        CHECK(q+need<=42);
        hostLimit=hostLimit+w<total?hostLimit+w:total;
      }
    }
    q++;CHECK(q<=42); // final verified END
  }
  CHECK(xnu_reply_poll_allowed(1,true));
  CHECK(xnu_reply_poll_allowed(1,false));
  return cases;
}
