#pragma once
#include <stdint.h>
static inline bool xnu_reminder_poll_due(uint32_t now,uint32_t lastPoll,uint32_t lastBusy,
    bool enabled,bool ready,bool supported,bool busy,bool quiet,bool speechActivity) {
  return enabled&&ready&&supported&&!busy&&!quiet&&!speechActivity&&
         uint32_t(now-lastPoll)>=30000&&uint32_t(now-lastBusy)>=25000;
}
