#pragma once
#include <stddef.h>
#include <stdint.h>
#include "adpcm_codec.h"

// Shared by the HTTP probe and real replies. Never pass a decoded HTTP read
// directly to SpeakerStream: its PCM frame limit is 1024 bytes.
typedef int (*XnuReplyWrite)(void*,const uint8_t*,size_t);
typedef struct {
  XnuAdpcmState codec;
  uint32_t samples,decoded,wireBytes;
  int failed;
  uint8_t pcm[1024];
} XnuHttpPlayback;

static inline int xnu_http_playback_begin(XnuHttpPlayback* state,uint32_t samples) {
  state->codec.predictor=state->codec.index=0;
  state->samples=samples;state->decoded=state->wireBytes=0;state->failed=0;
  return samples>0;
}
static inline int xnu_http_playback_push(XnuHttpPlayback* state,const uint8_t* wire,
                                         size_t size,XnuReplyWrite write,void* context) {
  if(state->failed||!size||!write||size>(state->samples+1)/2-state->wireBytes) {
    state->failed=1;return 0;
  }
  size_t offset=0;
  while(offset<size) {
    size_t count=size-offset;if(count>256)count=256;
    size_t bytes=xnu_adpcm_decode_bytes(&state->codec,wire+offset,count,
        state->pcm,sizeof(state->pcm),state->samples-state->decoded);
    if(!bytes||!write(context,state->pcm,bytes)){state->failed=1;return 0;}
    state->decoded+=bytes/2;state->wireBytes+=count;offset+=count;
  }
  return 1;
}
static inline int xnu_http_playback_complete(const XnuHttpPlayback* state) {
  return !state->failed&&state->samples>0&&state->decoded==state->samples&&
         state->wireBytes==(state->samples+1)/2;
}
