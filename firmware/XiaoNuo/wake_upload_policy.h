#pragma once
#include <stdint.h>

// Existing endpointing remains 2..6 seconds with 800ms of trailing silence.
// Round UP to a full 2000-sample upload packet, never drop captured speech.
static inline uint32_t xnu_wake_upload_target(uint32_t captured,bool stopped,bool negotiated) {
  if(!stopped||!negotiated||captured<32000||captured>96000)return 96000;
  return ((captured+1999)/2000)*2000;
}
static inline bool xnu_wake_trim_valid(uint32_t declaredBytes,uint32_t sentBytes,uint32_t samples,
                                      bool wake,bool compressed,bool negotiated) {
  return wake&&compressed&&negotiated&&samples>=32000&&samples<=96000&&samples%2000==0&&
         samples/2<=declaredBytes&&sentBytes==samples/2;
}
