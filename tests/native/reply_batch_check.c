#include "network_audio_batch.h"
static uint8_t data[8215],id[16];
static int checks=0,line=0;
static size_t build(unsigned count,unsigned payload){
  data[0]='X';data[1]='N';data[2]='B';data[3]='1';data[4]=0;data[5]=(uint8_t)count;
  size_t offset=6;
  for(unsigned i=0;i<count;i++){
    unsigned size=24+payload;data[offset++]=(uint8_t)(size>>8);data[offset++]=(uint8_t)size;
    data[offset]='X';data[offset+1]='N';data[offset+2]='A';data[offset+3]='1';
    for(unsigned j=0;j<16;j++)data[offset+4+j]=id[j];
    data[offset+20]=data[offset+21]=data[offset+22]=0;data[offset+23]=(uint8_t)i;
    for(unsigned j=0;j<payload;j++)data[offset+24+j]=(uint8_t)j;
    offset+=size;
  }
  return offset;
}
#define CHECK(test) do{checks++;if(!(test)){line=__LINE__;return -checks;}}while(0)
__declspec(dllexport) int failed_line(void){return line;}
__declspec(dllexport) int run_checks(void){
  XnuReplyBatch batch;size_t n;checks=line=0;
  n=build(8,1000);CHECK(xnu_reply_batch_parse(data,n,id,0,16,8000,1,&batch)&&batch.count==8&&batch.lengths[7]==1024);
  n=build(1,1);CHECK(xnu_reply_batch_parse(data,n,id,0,16,1,1,&batch)&&batch.count==1);
  CHECK(!xnu_reply_batch_parse(data,n,id,0,16,1,0,&batch)&&batch.count==0);
  n=build(2,10);data[0]='?';CHECK(!xnu_reply_batch_parse(data,n,id,0,16,20,1,&batch));
  n=build(2,10);data[5]=0;CHECK(!xnu_reply_batch_parse(data,n,id,0,16,20,1,&batch));
  n=build(8,1000);data[5]=9;CHECK(!xnu_reply_batch_parse(data,n,id,0,16,8000,1,&batch));
  n=build(8,1000);CHECK(!xnu_reply_batch_parse(data,n-1,id,0,16,8000,1,&batch));
  CHECK(!xnu_reply_batch_parse(data,n+1,id,0,16,8000,1,&batch));
  n=build(2,10);data[12]=99;CHECK(!xnu_reply_batch_parse(data,n,id,0,16,20,1,&batch));
  n=build(2,10);data[31]=9;CHECK(!xnu_reply_batch_parse(data,n,id,0,16,20,1,&batch));
  n=build(8,1000);CHECK(!xnu_reply_batch_parse(data,n,id,0,7,8000,1,&batch));
  CHECK(!xnu_reply_batch_parse(data,n,id,0,16,7999,1,&batch)&&batch.count==0);
  CHECK(!xnu_reply_batch_parse(data,n,id,UINT32_MAX-3,16,8000,1,&batch));
  n=build(2,10);data[6]=4;data[7]=1;CHECK(!xnu_reply_batch_parse(data,n,id,0,16,20,1,&batch));
  n=build(8,1000);data[6+7*1026+2+4]=99;CHECK(!xnu_reply_batch_parse(data,n,id,0,16,8000,1,&batch)&&batch.count==0);
  return checks;
}
