#pragma once
#include <stddef.h>
#include <stdint.h>

// A single-owner transaction for generated replies, never microphone input.
// Flash contains ciphertext only. The adapter owns an ephemeral per-reply key.
// No read is allowed until exact length and the plaintext SHA-256 are verified.
enum { XNU_SPOOL_SECTOR=4096, XNU_SPOOL_SLOT=524288,
       XNU_SPOOL_SLOTS=2, XNU_SPOOL_LIMIT=480000, XNU_SPOOL_CHUNK=1000,
       XNU_SPOOL_REGION=4096+2*524288 };
enum { XNU_SPOOL_CLOSED=0, XNU_SPOOL_WRITING=1, XNU_SPOOL_SEALED=2,
       XNU_SPOOL_FAILED=3 };
typedef struct {
  int (*read)(void*,uint32_t,uint8_t*,size_t);
  int (*write)(void*,uint32_t,const uint8_t*,size_t);
  int (*erase)(void*,uint32_t,uint32_t);
  int (*start_key)(void*);
  int (*crypt)(void*,uint32_t,const uint8_t*,uint8_t*,size_t);
  int (*hash_start)(void*);
  int (*hash_update)(void*,const uint8_t*,size_t);
  int (*hash_finish)(void*,uint8_t[32]);
  void (*clear_key)(void*);
} XnuSpoolOps;
typedef struct {
  XnuSpoolOps ops;void* context;
  uint32_t partition_size,target,received,position,base;
  unsigned next_slot,state,owned;
} XnuReplySpool;

static inline void xnu_spool_zero(void* data,size_t size){
  volatile uint8_t* p=(volatile uint8_t*)data;while(size--)*p++=0;
}
static inline void xnu_spool_close(XnuReplySpool* s){
  s->ops.clear_key(s->context);s->state=XNU_SPOOL_CLOSED;
  s->target=s->received=s->position=s->base=0;
}
static inline int xnu_spool_fail(XnuReplySpool* s){
  xnu_spool_close(s);s->state=XNU_SPOOL_FAILED;return 0;
}
static inline int xnu_spool_init(XnuReplySpool* s,XnuSpoolOps ops,void* context,
                               uint32_t partition_size,unsigned first_slot){
  if(!s||!ops.read||!ops.write||!ops.erase||!ops.start_key||!ops.crypt||
     !ops.hash_start||!ops.hash_update||!ops.hash_finish||!ops.clear_key)return 0;
  s->ops=ops;s->context=context;s->partition_size=partition_size;
  s->next_slot=first_slot%XNU_SPOOL_SLOTS;s->owned=0;
  xnu_spool_close(s);
  return partition_size>=XNU_SPOOL_REGION&&partition_size<=16*1024*1024&&partition_size%XNU_SPOOL_SECTOR==0;
}
static inline int xnu_spool_marker_byte(unsigned offset){
  // A deliberately raw format. A filesystem must never mount this partition.
  const uint8_t marker[16]={'X','N','U','O','-','S','P','O','O','L','-',1,0,0,0,0};
  return marker[offset];
}
static inline int xnu_spool_attach(XnuReplySpool* s,int claim_blank){
  uint8_t block[256];unsigned match=1,blank=1;
  if(s->partition_size<XNU_SPOOL_REGION||s->partition_size>16*1024*1024||s->partition_size%XNU_SPOOL_SECTOR)
    return xnu_spool_fail(s);
  s->owned=0;xnu_spool_close(s);
  if(!s->ops.read(s->context,0,block,sizeof(block)))return xnu_spool_fail(s);
  for(unsigned i=0;i<sizeof(block);i++){
    if(block[i]!=(i<16?xnu_spool_marker_byte(i):255))match=0;
    if(block[i]!=255)blank=0;
  }
  if(match){
    for(uint32_t at=256;at<XNU_SPOOL_SECTOR;at+=sizeof(block)){
      if(!s->ops.read(s->context,at,block,sizeof(block)))return xnu_spool_fail(s);
      for(unsigned i=0;i<sizeof(block);i++)if(block[i]!=255)return xnu_spool_fail(s);
    }
    s->owned=1;return 1;
  }
  if(!blank||!claim_blank)return xnu_spool_fail(s);
  // Before the first ownership write, inspect the ENTIRE partition. Never
  // format or erase foreign data, including data beyond our two slots.
  for(uint32_t at=256;at<s->partition_size;at+=sizeof(block)){
    if(!s->ops.read(s->context,at,block,sizeof(block)))return xnu_spool_fail(s);
    for(unsigned i=0;i<sizeof(block);i++)if(block[i]!=255)return xnu_spool_fail(s);
  }
  for(unsigned i=0;i<16;i++)block[i]=(uint8_t)xnu_spool_marker_byte(i);
  if(!s->ops.write(s->context,0,block,16))return xnu_spool_fail(s);
  if(!s->ops.read(s->context,0,block,16))return xnu_spool_fail(s);
  for(unsigned i=0;i<16;i++)if(block[i]!=xnu_spool_marker_byte(i))return xnu_spool_fail(s);
  s->owned=1;return 1;
}
static inline int xnu_spool_begin(XnuReplySpool* s,uint32_t bytes){
  if(!s->owned||!bytes||bytes>XNU_SPOOL_LIMIT)return xnu_spool_fail(s);
  xnu_spool_close(s);
  s->base=XNU_SPOOL_SECTOR+s->next_slot*XNU_SPOOL_SLOT;
  s->next_slot=(s->next_slot+1)%XNU_SPOOL_SLOTS;
  // Bound both erase and payload writes independently of caller input.
  if(s->base>s->partition_size-XNU_SPOOL_SLOT)return xnu_spool_fail(s);
  uint32_t erased=(bytes+XNU_SPOOL_SECTOR-1)/XNU_SPOOL_SECTOR*XNU_SPOOL_SECTOR;
  if(!s->ops.erase(s->context,s->base,erased)||
     !s->ops.start_key(s->context))return xnu_spool_fail(s);
  s->target=bytes;s->state=XNU_SPOOL_WRITING;return 1;
}
static inline int xnu_spool_append(XnuReplySpool* s,uint32_t offset,
                                   const uint8_t* data,size_t size){
  uint8_t encrypted[XNU_SPOOL_CHUNK];
  if(s->state!=XNU_SPOOL_WRITING||!data||!size||size>XNU_SPOOL_CHUNK||
     offset!=s->received||size>s->target-s->received)return xnu_spool_fail(s);
  int ok=s->ops.crypt(s->context,offset,data,encrypted,size)&&
    s->ops.write(s->context,s->base+offset,encrypted,size)&&
    s->ops.hash_update(s->context,data,size);
  xnu_spool_zero(encrypted,sizeof(encrypted));
  if(!ok)return xnu_spool_fail(s);
  s->received+=(uint32_t)size;return 1;
}
static inline int xnu_spool_seal(XnuReplySpool* s,const uint8_t expected[32]){
  uint8_t digest[32],encrypted[XNU_SPOOL_CHUNK],plain[XNU_SPOOL_CHUNK];unsigned difference=0;
  if(s->state!=XNU_SPOOL_WRITING||!expected||s->received!=s->target)
    return xnu_spool_fail(s);
  if(!s->ops.hash_finish(s->context,digest))return xnu_spool_fail(s);
  for(unsigned i=0;i<32;i++)difference|=digest[i]^expected[i];
  xnu_spool_zero(digest,sizeof(digest));
  if(difference)return xnu_spool_fail(s);
  // Verify the actual Flash bytes as well as received plaintext. A write that
  // reports success but stores damaged ciphertext must never reach playback.
  int ok=s->ops.hash_start(s->context);
  for(uint32_t at=0;ok&&at<s->target;){
    size_t size=s->target-at;if(size>XNU_SPOOL_CHUNK)size=XNU_SPOOL_CHUNK;
    ok=s->ops.read(s->context,s->base+at,encrypted,size)&&
       s->ops.crypt(s->context,at,encrypted,plain,size)&&
       s->ops.hash_update(s->context,plain,size);
    at+=(uint32_t)size;
  }
  xnu_spool_zero(encrypted,sizeof(encrypted));xnu_spool_zero(plain,sizeof(plain));
  if(!ok||!s->ops.hash_finish(s->context,digest))return xnu_spool_fail(s);
  difference=0;for(unsigned i=0;i<32;i++)difference|=digest[i]^expected[i];
  xnu_spool_zero(digest,sizeof(digest));
  if(difference)return xnu_spool_fail(s);
  s->position=0;s->state=XNU_SPOOL_SEALED;return 1;
}
static inline int xnu_spool_read(XnuReplySpool* s,uint8_t* output,size_t size){
  uint8_t encrypted[XNU_SPOOL_CHUNK];
  if(s->state!=XNU_SPOOL_SEALED||!output||!size||size>XNU_SPOOL_CHUNK||
     size>s->target-s->position)return xnu_spool_fail(s);
  int ok=s->ops.read(s->context,s->base+s->position,encrypted,size)&&
    s->ops.crypt(s->context,s->position,encrypted,output,size);
  xnu_spool_zero(encrypted,sizeof(encrypted));
  if(!ok){xnu_spool_zero(output,size);return xnu_spool_fail(s);}
  s->position+=(uint32_t)size;return 1;
}
