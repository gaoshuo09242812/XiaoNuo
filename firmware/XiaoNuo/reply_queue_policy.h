#pragma once
#include <stdint.h>

// Include the end marker even while two windows may already be in flight.
static inline unsigned xnu_reply_queue_need(unsigned window, unsigned remaining, unsigned pipeline) {
  unsigned frames=window*(pipeline==2?2:1);
  if(frames>remaining)frames=remaining;
  return pipeline==2?frames+1:(remaining?frames:1);
}
static inline bool xnu_reply_poll_allowed(unsigned pipeline,bool ackPending) {
  return pipeline!=2||!ackPending;
}
