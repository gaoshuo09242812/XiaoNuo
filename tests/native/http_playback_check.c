#include "http_reply_playback.h"

static unsigned char wire[32000],expected[128000],actual[128000];
static size_t used,frames,failAt;
static int failure;
#define CHECK(condition) do { if(!(condition)){failure=__LINE__;return 0;} } while(0)
static int sink(void* ignored,const uint8_t* pcm,size_t bytes) {
  (void)ignored;
  CHECK(bytes>0&&bytes<=1024&&bytes%2==0);
  if(failAt&&frames==failAt)return 0;
  CHECK(used+bytes<=sizeof(actual));
  for(size_t i=0;i<bytes;i++)actual[used+i]=pcm[i];used+=bytes;frames++;return 1;
}
__declspec(dllexport) int run_checks(void) {
  failure=0;
  XnuHttpPlayback state;
  XnuAdpcmState reference={0,0};
  const size_t reads[]={1,17,250,256,257,1000,32000};
  unsigned cases=0;
  for(size_t i=0;i<sizeof(wire);i++)wire[i]=(unsigned char)(i*37+11);
  CHECK(xnu_adpcm_decode_bytes(&reference,wire,sizeof(wire),expected,sizeof(expected),64000)==128000);
  for(size_t i=0;i<sizeof(reads)/sizeof(reads[0]);i++) {
    CHECK(xnu_http_playback_begin(&state,64000));used=frames=failAt=0;
    for(size_t offset=0;offset<sizeof(wire);) {
      size_t count=sizeof(wire)-offset;if(count>reads[i])count=reads[i];
      CHECK(xnu_http_playback_push(&state,wire+offset,count,sink,NULL));offset+=count;
    }
    CHECK(xnu_http_playback_complete(&state));
    CHECK(used==128000);
    for(size_t j=0;j<used;j++)CHECK(actual[j]==expected[j]);
    CHECK(state.codec.predictor==reference.predictor&&state.codec.index==reference.index);
    cases++;
  }
  xnu_http_playback_begin(&state,64000);used=frames=failAt=0;
  CHECK(xnu_http_playback_push(&state,wire,1000,sink,NULL));
  CHECK(!xnu_http_playback_complete(&state));cases++;
  xnu_http_playback_begin(&state,64000);used=frames=0;failAt=2;
  CHECK(!xnu_http_playback_push(&state,wire,1000,sink,NULL));
  CHECK(state.failed&&!xnu_http_playback_complete(&state));cases++;
  xnu_http_playback_begin(&state,3);used=frames=failAt=0;
  CHECK(xnu_http_playback_push(&state,wire,2,sink,NULL));
  CHECK(used==6&&xnu_http_playback_complete(&state));cases++;
  CHECK(!xnu_http_playback_push(&state,wire,1,sink,NULL));
  CHECK(!xnu_http_playback_complete(&state));cases++;
  CHECK(!xnu_http_playback_begin(&state,0));
  CHECK(!xnu_http_playback_complete(&state));cases++;
  xnu_http_playback_begin(&state,3);used=frames=failAt=0;
  CHECK(!xnu_http_playback_push(&state,wire,3,sink,NULL));
  CHECK(used==0&&!xnu_http_playback_complete(&state));cases++;
  return (int)cases;
}

__declspec(dllexport) int failed_line(void){return failure;}
