#pragma once

// Isolated renderer: not included by the accepted 072 sketch yet.
// Only the Arduino/main display task may call it, inside an existing safe tick.
// Surface follows U8g2's drawRBox/setDrawColor API. No I2C, heap, timers or delays.
namespace XnuFace {
enum Expression : unsigned char { Neutral, Happy, Gentle, Sleepy, Thinking, Quiet };

inline bool same(const char* a, const char* b) {
  if (!a) return false;
  while (*a && *a == *b) { ++a; ++b; }
  return *a == *b;
}

inline Expression fromName(const char* name) {
  if (same(name,"happy")) return Happy;
  if (same(name,"gentle")) return Gentle;
  if (same(name,"sleepy")) return Sleepy;
  if (same(name,"thinking")) return Thinking;
  if (same(name,"quiet")) return Quiet;
  return Neutral;
}

inline Expression visible(Expression value, unsigned long nowMs, unsigned long updatedAt, bool quiet) {
  return quiet?Quiet:((unsigned long)(nowMs-updatedAt)<8000UL?value:Neutral);
}

template<class Surface>
void draw(Surface& screen, Expression expression, unsigned long nowMs, bool quiet=false) {
  if (expression > Quiet) expression=Neutral;
  bool closed=quiet || expression==Quiet || nowMs%5100UL<160UL;
  screen.setDrawColor(1);
  for (int eye=0;eye<2;++eye) {
    int x=20+53*eye;
    if (closed) {
      screen.drawRBox(x,33,35,5,2);
    } else if (expression==Happy) {
      screen.drawRBox(x,24,35,24,10);
      screen.setDrawColor(0);
      screen.drawRBox(x,31,35,23,10);
      screen.setDrawColor(1);
    } else {
      int height=expression==Sleepy?13:expression==Gentle?19:27;
      int y=21+(27-height)/2;
      screen.drawRBox(x,y,35,height,expression==Sleepy?5:9);
      screen.setDrawColor(0);
      int pupil=expression==Thinking?22:15;
      int pupilHeight=height-8;
      screen.drawRBox(x+pupil,y+4,7,pupilHeight,pupilHeight<6?2:3);
      screen.setDrawColor(1);
    }
  }
  // Only rows 21..53 are touched; header/status labels remain caller-owned.
}
}
