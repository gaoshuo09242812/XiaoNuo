#include "reply_spool_core.h"

// Fault-injecting storage/key/hash doubles. This exercises the production
// transaction and boundaries; it is NOT a test or substitute for AES/SHA-256.
static uint8_t flash[0x160000],data[1000],output[1000],expected[32];
static XnuReplySpool spool,rebooted;
static unsigned checks,line,reads,writes,erases,key_number,key_on,clears;
static unsigned fail_read,fail_write,fail_erase,fail_key,fail_crypt,fail_hash;
static unsigned corrupt_write,bounds_fault;
static uint32_t hashed;static uint8_t hash[32];
static void bytes(uint8_t* p,uint32_t offset,size_t size){for(size_t i=0;i<size;i++)p[i]=(uint8_t)((offset+i)*37+11);}
static int read_flash(void* p,uint32_t at,uint8_t* out,size_t n){
  (void)p;reads++;if(at>sizeof(flash)||n>sizeof(flash)-at){bounds_fault++;return 0;}
  if(fail_read)return 0;for(size_t i=0;i<n;i++)out[i]=flash[at+i];return 1;
}
static int write_flash(void* p,uint32_t at,const uint8_t* in,size_t n){
  (void)p;writes++;if(at>sizeof(flash)||n>sizeof(flash)-at){bounds_fault++;return 0;}
  if(fail_write)return 0;
  for(size_t i=0;i<n;i++)if((flash[at+i]&in[i])!=in[i])return 0;
  for(size_t i=0;i<n;i++)flash[at+i]=in[i];
  if(corrupt_write&&at>=4096)flash[at]^=1;return 1;
}
static int erase_flash(void* p,uint32_t at,uint32_t n){
  (void)p;erases++;
  if((at!=4096&&at!=4096+XNU_SPOOL_SLOT)||!n||n>XNU_SPOOL_SLOT||n%4096||at>sizeof(flash)||n>sizeof(flash)-at){bounds_fault++;return 0;}
  if(fail_erase)return 0;for(uint32_t i=0;i<n;i++)flash[at+i]=255;return 1;
}
static int hash_start(void* p){(void)p;hashed=0;for(unsigned i=0;i<32;i++)hash[i]=0;return !fail_hash;}
static int start_key(void* p){if(fail_key)return 0;key_number++;key_on=1;return hash_start(p);}
static int crypt(void* p,uint32_t at,const uint8_t* in,uint8_t* out,size_t n){
  (void)p;if(!key_on||fail_crypt)return 0;
  for(size_t i=0;i<n;i++)out[i]=in[i]^(uint8_t)(key_number+(at+i)*13+73);return 1;
}
static int hash_update(void* p,const uint8_t* in,size_t n){
  (void)p;if(fail_hash)return 0;
  for(size_t i=0;i<n;i++,hashed++)hash[hashed%32]=(uint8_t)(hash[hashed%32]*3+in[i]+1);return 1;
}
static int hash_finish(void* p,uint8_t out[32]){(void)p;if(fail_hash)return 0;for(unsigned i=0;i<32;i++)out[i]=hash[i];return 1;}
static void clear_key(void* p){(void)p;key_on=0;clears++;}
static XnuSpoolOps ops={read_flash,write_flash,erase_flash,start_key,crypt,hash_start,hash_update,hash_finish,clear_key};
static void reset(void){
  for(unsigned i=0;i<sizeof(flash);i++)flash[i]=255;
  reads=writes=erases=key_number=key_on=clears=bounds_fault=0;
  fail_read=fail_write=fail_erase=fail_key=fail_crypt=fail_hash=corrupt_write=0;
  xnu_spool_init(&spool,ops,0,sizeof(flash),0);
}
static int ready(uint32_t size){return xnu_spool_attach(&spool,1)&&xnu_spool_begin(&spool,size);}
static int fill(uint32_t size){
  for(uint32_t at=0;at<size;){size_t n=size-at;if(n>1000)n=1000;bytes(data,at,n);
    if(!xnu_spool_append(&spool,at,data,n))return 0;at+=(uint32_t)n;}
  for(unsigned i=0;i<32;i++)expected[i]=hash[i];return 1;
}
#define CHECK(test) do{checks++;if(!(test)){line=__LINE__;return -(int)checks;}}while(0)
__declspec(dllexport) int failed_line(void){return (int)line;}
__declspec(dllexport) int run_checks(void){
  checks=line=0;
  reset();CHECK(!xnu_spool_attach(&spool,0)&&writes==0&&erases==0);
  reset();flash[sizeof(flash)-1]=17;CHECK(!xnu_spool_attach(&spool,1)&&writes==0&&erases==0&&flash[sizeof(flash)-1]==17);
  reset();flash[0]=42;CHECK(!xnu_spool_attach(&spool,1)&&writes==0&&erases==0);
  reset();fail_read=1;CHECK(!xnu_spool_attach(&spool,1)&&writes==0&&erases==0);
  reset();fail_write=1;CHECK(!xnu_spool_attach(&spool,1)&&!spool.owned&&erases==0);
  reset();CHECK(xnu_spool_attach(&spool,1)&&writes==1&&erases==0&&spool.owned);
  flash[1000]=1;CHECK(!xnu_spool_attach(&spool,0)&&!spool.owned&&erases==0);
  reset();CHECK(!xnu_spool_begin(&spool,1)&&erases==0);
  reset();xnu_spool_attach(&spool,1);CHECK(!xnu_spool_begin(&spool,0)&&erases==0);
  CHECK(!xnu_spool_begin(&spool,XNU_SPOOL_LIMIT+1)&&erases==0);
  reset();CHECK(ready(5003)&&fill(5003));
  CHECK(flash[spool.base]!=11&&key_on&&spool.state==XNU_SPOOL_WRITING);
  CHECK(xnu_spool_seal(&spool,expected)&&spool.state==XNU_SPOOL_SEALED);
  for(uint32_t at=0;at<5003;){size_t n=5003-at;if(n>333)n=333;
    if(!xnu_spool_read(&spool,output,n)){line=__LINE__;return -999;}
    bytes(data,at,n);for(size_t i=0;i<n;i++)if(data[i]!=output[i]){line=__LINE__;return -998;}at+=(uint32_t)n;}
  CHECK(spool.position==5003&&!bounds_fault);
  xnu_spool_close(&spool);CHECK(!key_on&&spool.state==XNU_SPOOL_CLOSED&&spool.position==0);
  reset();ready(17);CHECK(!xnu_spool_read(&spool,output,1)&&!key_on);
  reset();ready(17);CHECK(!xnu_spool_seal(&spool,expected)&&!key_on);
  reset();ready(17);CHECK(!xnu_spool_append(&spool,1,data,1)&&!key_on&&spool.received==0);
  reset();ready(17);CHECK(!xnu_spool_append(&spool,0,data,18)&&!key_on);
  reset();ready(2000);CHECK(!xnu_spool_append(&spool,0,data,1001)&&!key_on);
  reset();ready(17);fill(17);expected[0]^=1;CHECK(!xnu_spool_seal(&spool,expected)&&!key_on);
  reset();ready(17);corrupt_write=1;fill(17);CHECK(!xnu_spool_seal(&spool,expected)&&!key_on);
  reset();ready(17);fill(17);flash[spool.base+16]^=2;CHECK(!xnu_spool_seal(&spool,expected)&&!key_on);
  reset();ready(17);fill(17);fail_read=1;CHECK(!xnu_spool_seal(&spool,expected)&&!key_on);
  reset();ready(17);fail_write=1;CHECK(!xnu_spool_append(&spool,0,data,17)&&!key_on);
  reset();ready(17);fail_crypt=1;CHECK(!xnu_spool_append(&spool,0,data,17)&&!key_on);
  reset();ready(17);fail_hash=1;CHECK(!xnu_spool_append(&spool,0,data,17)&&!key_on);
  reset();xnu_spool_attach(&spool,1);fail_erase=1;CHECK(!xnu_spool_begin(&spool,17)&&!key_on);
  reset();xnu_spool_attach(&spool,1);fail_key=1;CHECK(!xnu_spool_begin(&spool,17)&&!key_on);
  reset();ready(17);fill(17);xnu_spool_seal(&spool,expected);fail_read=1;output[0]=99;
  CHECK(!xnu_spool_read(&spool,output,17)&&!key_on&&output[0]==0);
  reset();ready(17);fill(17);xnu_spool_seal(&spool,expected);CHECK(!xnu_spool_read(&spool,output,18)&&!key_on);
  reset();ready(17);fill(17);xnu_spool_seal(&spool,expected);xnu_spool_close(&spool);
  CHECK(!xnu_spool_read(&spool,output,1));
  reset();ready(17);fill(17);xnu_spool_init(&rebooted,ops,0,sizeof(flash),1);
  CHECK(xnu_spool_attach(&rebooted,0)&&!key_on&&rebooted.state==XNU_SPOOL_CLOSED);
  CHECK(!xnu_spool_read(&rebooted,output,1));
  reset();ready(17);fill(17);{uint8_t previous=flash[spool.base];uint32_t oldbase=spool.base;
    CHECK(xnu_spool_begin(&spool,17)&&spool.base!=oldbase&&fill(17)&&flash[spool.base]!=previous&&flash[oldbase]==previous);}
  CHECK(xnu_spool_begin(&spool,17)&&spool.base==4096&&!bounds_fault);
  reset();CHECK(ready(XNU_SPOOL_LIMIT)&&fill(XNU_SPOOL_LIMIT)&&xnu_spool_seal(&spool,expected)&&!bounds_fault);
  CHECK(!xnu_spool_init(&rebooted,ops,0,4096,0));
  CHECK(!xnu_spool_init(&rebooted,ops,0,UINT32_MAX,0));
  return (int)checks;
}
