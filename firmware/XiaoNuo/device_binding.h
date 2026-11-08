#pragma once
#include <Arduino.h>
#include <Preferences.h>

// Provisioned only over the owner's physical USB connection. Never print keys.
class DeviceBinding {
 public:
  void load(Preferences& prefs) {
    revision++;
    memset(&value,0,sizeof(value));
    if (prefs.getBytesLength("binding")!=sizeof(value)) return;
    Record loaded={};
    if (prefs.getBytes("binding",&loaded,sizeof(loaded))==sizeof(loaded) &&
        validId(loaded.id) && validKey(loaded.key)) value=loaded;
  }
  const char* id() const { return value.id; }
  uint32_t generation() const { return revision; }
  String authorizationHeader() const {
    if (!value.id[0] || !value.key[0]) return String();
    return String("Authorization: Bearer ")+value.key;
  }
  String bearerToken() const {
    if (!value.id[0] || !value.key[0]) return String();
    return String("Bearer ")+value.key;
  }
  const char* tokenValue() const { return value.key; }
  bool command(const String& line,Preferences& prefs) {
    if (line.startsWith("PAIR ")) {
      Record next={}; char extra;
      if (sscanf(line.c_str(),"PAIR %36[0-9a-f-] %64[0-9a-f] %c",next.id,next.key,&extra)!=2 ||
          !validId(next.id) || !validKey(next.key)) {
        Serial.println("{\"event\":\"error\",\"code\":\"PAIR_INVALID\"}"); return true;
      }
      if (prefs.putBytes("binding",&next,sizeof(next))!=sizeof(next)) {
        Serial.println("{\"event\":\"error\",\"code\":\"PAIR_SAVE_FAILED\"}"); return true;
      }
      value=next;
      revision++;
      Serial.printf("{\"event\":\"paired\",\"binding_id\":\"%s\",\"credential_saved\":true,\"network_client_enabled\":false}\n",value.id);
      memset(&next,0,sizeof(next));
      return true;
    }
    if (line.startsWith("UNPAIR ")) {
      String requested=line.substring(7);
      if (requested!=value.id || requested.isEmpty()) {
        Serial.println("{\"event\":\"error\",\"code\":\"PAIR_ID_MISMATCH\"}"); return true;
      }
      if (!prefs.remove("binding")) {
        Serial.println("{\"event\":\"error\",\"code\":\"PAIR_REMOVE_FAILED\"}"); return true;
      }
      memset(&value,0,sizeof(value));
      revision++;
      Serial.println("{\"event\":\"unpaired\",\"credential_removed\":true}");
      return true;
    }
    return false;
  }
 private:
  uint32_t revision=0;
  struct Record { char id[37]; char key[65]; } value={};
  static bool hex(char c) { return (c>='0'&&c<='9') || (c>='a'&&c<='f'); }
  static bool validId(const char* id) {
    if (id[36]!=0) return false;
    for (size_t i=0;i<36;i++) {
      if (i==8||i==13||i==18||i==23) { if (id[i]!='-') return false; }
      else if (!hex(id[i])) return false;
    }
    return true;
  }
  static bool validKey(const char* key) {
    if (key[64]!=0) return false;
    for (size_t i=0;i<64;i++) if (!hex(key[i])) return false;
    return true;
  }
};

