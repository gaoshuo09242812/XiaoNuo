#pragma once
#include <stdint.h>
#include <stddef.h>
#include <string.h>
namespace XiaoNuoWire {
constexpr size_t HEADER=24,PAYLOAD=1000,FRAME=1024;
constexpr uint32_t MAX_REPLY=8*32000;
inline int hex(char c){return c>='0'&&c<='9'?c-'0':c>='a'&&c<='f'?c-'a'+10:-1;}
inline bool uuid(const char* text,uint8_t out[16]) {
  if(!text||strlen(text)!=36)return false;size_t n=0;
  for(size_t i=0;i<36;){
    if(i==8||i==13||i==18||i==23){if(text[i++]!='-')return false;continue;}
    int a=hex(text[i++]),b=hex(text[i++]);if(a<0||b<0)return false;out[n++]=(a<<4)|b;
  }return n==16;
}
inline void uuidText(const uint8_t id[16],char out[37]) {
  const char* digits="0123456789abcdef";size_t n=0;
  for(size_t i=0;i<16;i++){if(i==4||i==6||i==8||i==10)out[n++]='-';out[n++]=digits[id[i]>>4];out[n++]=digits[id[i]&15];}out[n]=0;
}
inline void header(uint8_t* out,const uint8_t id[16],uint32_t sequence) {
  memcpy(out,"XNA1",4);memcpy(out+4,id,16);for(int i=0;i<4;i++)out[20+i]=(uint8_t)(sequence>>(24-i*8));
}
inline bool frameAny(const uint8_t* data,size_t length,const uint8_t id[16],uint32_t sequence) {
  if(length<=24||length>1024||memcmp(data,"XNA1",4)||memcmp(data+4,id,16))return false;
  uint32_t actual=0;for(int i=0;i<4;i++)actual=(actual<<8)|data[20+i];return actual==sequence;
}
inline bool frame(const uint8_t* data,size_t length,const uint8_t id[16],uint32_t sequence) {
  return length>24&&(length-24)%2==0&&frameAny(data,length,id,sequence);
}
inline bool digest(const uint8_t bytes[32],const char* expected) {
  if(!expected||strlen(expected)!=64)return false;uint8_t difference=0;
  for(size_t i=0;i<32;i++){int a=hex(expected[2*i]),b=hex(expected[2*i+1]);if(a<0||b<0)return false;difference|=bytes[i]^(uint8_t)((a<<4)|b);}return difference==0;
}
inline void digestText(const uint8_t bytes[32],char out[65]) {
  const char* digits="0123456789abcdef";for(size_t i=0;i<32;i++){out[i*2]=digits[bytes[i]>>4];out[i*2+1]=digits[bytes[i]&15];}out[64]=0;
}
}
