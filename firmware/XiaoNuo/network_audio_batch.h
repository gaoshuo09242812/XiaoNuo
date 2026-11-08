#pragma once
#include <stddef.h>
#include <stdint.h>

typedef struct {
  const uint8_t* frames[8];
  uint16_t lengths[8];
  unsigned count;
} XnuReplyBatch;

// Validate the entire batch before any queue/hash/sequence state is changed.
static inline int xnu_reply_batch_parse(const uint8_t* data,size_t size,
    const uint8_t id[16],uint32_t sequence,unsigned window_remaining,
    uint32_t bytes_remaining,int compressed,XnuReplyBatch* result) {
  result->count=0;
  if(size<6||size>8214||data[0]!='X'||data[1]!='N'||data[2]!='B'||data[3]!='1')return 0;
  unsigned count=((unsigned)data[4]<<8)|data[5];
  if(!count||count>8||count>window_remaining||sequence>UINT32_MAX-(count-1))return 0;
  size_t offset=6;uint32_t bytes=0;
  for(unsigned i=0;i<count;i++){
    if(offset+2>size)return 0;
    unsigned length=((unsigned)data[offset]<<8)|data[offset+1];offset+=2;
    if(length<=24||length>1024||length>size-offset||(!compressed&&(length-24)%2))return 0;
    const uint8_t* frame=data+offset;
    if(frame[0]!='X'||frame[1]!='N'||frame[2]!='A'||frame[3]!='1')return 0;
    for(unsigned j=0;j<16;j++)if(frame[4+j]!=id[j])return 0;
    uint32_t actual=((uint32_t)frame[20]<<24)|((uint32_t)frame[21]<<16)|((uint32_t)frame[22]<<8)|frame[23];
    if(actual!=sequence+i||length-24>bytes_remaining-bytes)return 0;
    bytes+=length-24;result->frames[i]=frame;result->lengths[i]=length;offset+=length;
  }
  if(offset!=size)return 0;
  result->count=count;return 1;
}
