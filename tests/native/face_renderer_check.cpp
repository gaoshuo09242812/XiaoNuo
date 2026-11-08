#include "face_renderer.h"
static int failed=0,checks=0;
static unsigned char pixels[128*64];
struct Surface {
  int color=1;
  void setDrawColor(int value) { color=value; }
  void drawRBox(int x,int y,int width,int height,int radius) {
    ++checks;
    if(x<0||x+width>128||y<21||y+height>54||radius<0||2*radius>width||2*radius>height) failed=__LINE__;
    for(int py=y;py<y+height;++py)for(int px=x;px<x+width;++px) {
      int cx=px<x+radius?x+radius-1:px>=x+width-radius?x+width-radius:px;
      int cy=py<y+radius?y+radius-1:py>=y+height-radius?y+height-radius:py;
      int dx=px-cx,dy=py-cy;
      if(dx*dx+dy*dy<=radius*radius) pixels[py*128+px]=(unsigned char)color;
    }
  }
};
static void clear() { for(int i=0;i<128*64;++i)pixels[i]=0; }
static int lit() { int count=0;for(int i=0;i<128*64;++i)count+=pixels[i];return count; }
#define CHECK(value) do { ++checks;if(!(value)) { failed=__LINE__;return checks; } } while(0)
extern "C" __declspec(dllexport) int run_checks() {
  failed=checks=0;
  const char* names[]={"neutral","happy","gentle","sleepy","thinking","quiet"};
  for(unsigned i=0;i<6;++i)CHECK(XnuFace::fromName(names[i])==i);
  CHECK(XnuFace::fromName(nullptr)==XnuFace::Neutral);
  CHECK(XnuFace::fromName("not-an-expression")==XnuFace::Neutral);
  CHECK(XnuFace::fromName("Happy")==XnuFace::Neutral);
  CHECK(XnuFace::visible(XnuFace::Happy,100,100,false)==XnuFace::Happy);
  CHECK(XnuFace::visible(XnuFace::Happy,8099,100,false)==XnuFace::Happy);
  CHECK(XnuFace::visible(XnuFace::Happy,8100,100,false)==XnuFace::Neutral);
  CHECK(XnuFace::visible(XnuFace::Happy,101,100,true)==XnuFace::Quiet);
  CHECK(XnuFace::visible(XnuFace::Gentle,30,0xfffffff0UL,false)==XnuFace::Gentle);
  CHECK(XnuFace::visible(XnuFace::Gentle,8000,0xfffffff0UL,false)==XnuFace::Neutral);
  CHECK(XnuFace::visible(XnuFace::Happy,99,100,false)==XnuFace::Neutral);
  Surface screen;
  for(unsigned face=0;face<8;++face)for(unsigned t=0;t<10200;t+=17)for(unsigned quiet=0;quiet<2;++quiet) {
    clear();XnuFace::draw(screen,(XnuFace::Expression)face,t,quiet!=0);
    CHECK(failed==0);CHECK(screen.color==1);CHECK(lit()>0);
    for(int y=0;y<64;++y)if(y<21||y>=54)for(int x=0;x<128;++x)if(pixels[y*128+x]) { failed=__LINE__;return checks; }
  }
  int opened[6];
  for(int face=0;face<6;++face) { clear();XnuFace::draw(screen,(XnuFace::Expression)face,1000);opened[face]=lit(); }
  CHECK(opened[0]>opened[2]);CHECK(opened[2]>opened[3]);CHECK(opened[1]!=opened[0]);CHECK(opened[5]<opened[3]);
  for(int face=0;face<6;++face) { clear();XnuFace::draw(screen,(XnuFace::Expression)face,1000,true);CHECK(lit()==opened[5]); }
  const unsigned long times[]={0UL,159UL,5100UL,0xffffffffUL};
  for(unsigned long t: times) {
    clear();XnuFace::draw(screen,XnuFace::Neutral,t);CHECK(screen.color==1);
  }
  return checks;
}
extern "C" __declspec(dllexport) int failed_line() { return failed; }
extern "C" __declspec(dllexport) int render_pixel(int face,int x,int y) {
  if(x<0||x>=128||y<0||y>=64)return -1;
  if(x==0&&y==0) { clear();Surface screen;XnuFace::draw(screen,(XnuFace::Expression)face,1000); }
  return pixels[y*128+x];
}
