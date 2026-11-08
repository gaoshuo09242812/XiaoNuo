#include "adpcm_codec.h"
#include <stdlib.h>
__declspec(dllexport) void encode(const short* input,int count,int pred,int index,unsigned char* output,int* final){
XnuAdpcmState state={pred,index};for(int i=0;i<count;i+=2){unsigned a=xnu_adpcm_encode(&state,input[i]);unsigned b=xnu_adpcm_encode(&state,input[i+1]);output[i/2]=(a<<4)|b;}final[0]=state.predictor;final[1]=state.index;}
__declspec(dllexport) void scenario(const short* input,int idle,unsigned char* output,int* initial){
XnuAdpcmRing ring={0};for(int i=0;i<6;i++)ring.blocks[i]=calloc(2000,4);
for(int i=0;i<idle;i++){if(ring.used>=3200)xnu_adpcm_pop(&ring,1);xnu_adpcm_push(&ring,input[i]);}
initial[0]=ring.decoder.predictor;initial[1]=ring.decoder.index;
for(int i=0;i<92800;i++)xnu_adpcm_push(&ring,input[idle+i]);
for(int i=0;i<48;i++){xnu_adpcm_pack(&ring,output+i*1000,2000);xnu_adpcm_pop(&ring,2000);}
for(int i=0;i<6;i++)free((void*)ring.blocks[i]);}
