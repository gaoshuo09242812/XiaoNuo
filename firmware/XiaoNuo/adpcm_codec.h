#pragma once
#include <stdint.h>
#include <stddef.h>

/* Portable integer-only IMA codec. High nibble first on the wire. */
typedef struct { int predictor; int index; } XnuAdpcmState;
static const int xnu_adpcm_steps[89]={
  7,8,9,10,11,12,13,14,16,17,19,21,23,25,28,31,34,37,41,45,50,55,60,66,73,80,88,97,107,118,130,
  143,157,173,190,209,230,253,279,307,337,371,408,449,494,544,598,658,724,796,876,963,1060,1166,
  1282,1411,1552,1707,1878,2066,2272,2499,2749,3024,3327,3660,4026,4428,4871,5358,5894,6484,
  7132,7845,8630,9493,10442,11487,12635,13899,15289,16818,18500,20350,22385,24623,27086,29794,32767};
static const int xnu_adpcm_index[8]={-1,-1,-1,-1,2,4,6,8};
static inline int xnu_adpcm_decode(XnuAdpcmState* state,unsigned code){
  int step=xnu_adpcm_steps[state->index],delta=step>>3;
  if(code&4)delta+=step;if(code&2)delta+=step>>1;if(code&1)delta+=step>>2;
  state->predictor+=(code&8)?-delta:delta;
  if(state->predictor>32767)state->predictor=32767;
  if(state->predictor<-32768)state->predictor=-32768;
  state->index+=xnu_adpcm_index[code&7];
  if(state->index<0)state->index=0;if(state->index>88)state->index=88;
  return state->predictor;
}
/* Decode packed nibbles into little-endian PCM16, bounded by both byte and
 * sample limits. Decoder state persists across network frames. */
static inline size_t xnu_adpcm_decode_bytes(XnuAdpcmState* state,const uint8_t* data,size_t size,
                                            uint8_t* output,size_t max_bytes,size_t max_samples){
  size_t samples=0,written=0;
  for(size_t i=0;i<size&&samples<max_samples;i++){
    unsigned codes[2]={data[i]>>4,data[i]&15};
    for(unsigned half=0;half<2&&samples<max_samples;half++){
      if(written+2>max_bytes)return written;
      int sample=xnu_adpcm_decode(state,codes[half]);
      output[written++]=(uint8_t)((unsigned)sample&255);
      output[written++]=(uint8_t)(((unsigned)sample>>8)&255);
      samples++;
    }
  }return written;
}
static inline unsigned xnu_adpcm_encode(XnuAdpcmState* state,int sample){
  int step=xnu_adpcm_steps[state->index],difference=sample-state->predictor;
  unsigned code=0;
  if(difference<0){code=8;difference=-difference;}
  if(difference>=step){code|=4;difference-=step;}
  step>>=1;if(difference>=step){code|=2;difference-=step;}
  step>>=1;if(difference>=step)code|=1;
  xnu_adpcm_decode(state,code);return code;
}

/* Six 8KB blocks hold the complete six-second clip, including its pre-roll.
 * Volatile 32-bit accesses are required even when a block lives in IRAM. */
#define XNU_ADPCM_BLOCK_SAMPLES 16000u
#define XNU_ADPCM_BLOCK_WORDS 2000u
#define XNU_ADPCM_BLOCKS 6u
typedef struct {
  volatile uint32_t* blocks[XNU_ADPCM_BLOCKS];
  size_t head,used;
  XnuAdpcmState encoder,decoder;
} XnuAdpcmRing;
static inline unsigned xnu_adpcm_code_at(const XnuAdpcmRing* ring,size_t index){
  uint32_t word=ring->blocks[index/XNU_ADPCM_BLOCK_SAMPLES][(index%XNU_ADPCM_BLOCK_SAMPLES)/8];
  return (unsigned)((word>>((index%8)*4))&15);
}
static inline void xnu_adpcm_set_code(XnuAdpcmRing* ring,size_t index,unsigned code){
  volatile uint32_t* word=&ring->blocks[index/XNU_ADPCM_BLOCK_SAMPLES][(index%XNU_ADPCM_BLOCK_SAMPLES)/8];
  unsigned shift=(unsigned)(index%8)*4;uint32_t value=*word;
  *word=(value&~(uint32_t)(15u<<shift))|((uint32_t)code<<shift);
}
static inline void xnu_adpcm_push(XnuAdpcmRing* ring,int sample){
  unsigned code=xnu_adpcm_encode(&ring->encoder,sample);
  xnu_adpcm_set_code(ring,(ring->head+ring->used)%96000u,code);ring->used++;
}
static inline void xnu_adpcm_pop(XnuAdpcmRing* ring,size_t samples){
  while(samples--){xnu_adpcm_decode(&ring->decoder,xnu_adpcm_code_at(ring,ring->head));
    ring->head=(ring->head+1)%96000u;ring->used--;}
}
static inline void xnu_adpcm_pack(const XnuAdpcmRing* ring,uint8_t* output,size_t samples){
  for(size_t i=0;i<samples;i+=2){
    unsigned first=xnu_adpcm_code_at(ring,(ring->head+i)%96000u);
    unsigned second=xnu_adpcm_code_at(ring,(ring->head+i+1)%96000u);
    output[i/2]=(uint8_t)((first<<4)|second);
  }
}
