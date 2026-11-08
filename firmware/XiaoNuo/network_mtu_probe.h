#pragma once
#include <Arduino.h>
#include <esp_netif.h>
#include <esp_netif_net_stack.h>
#include <lwip/netif.h>
#include <lwip/tcpip.h>

// Temporary diagnostic only. Call after stopping WSS and restore before restart.
// No NVS or saved WiFi setting is modified. Existing SDK lacks a public MTU setter.
class NetworkMtuProbe {
 public:
  explicit NetworkMtuProbe(bool enabled) {
    if(!enabled)return;
    esp_netif_t* station=esp_netif_get_handle_from_ifkey("WIFI_STA_DEF");
    if(station)interface=static_cast<struct netif*>(esp_netif_get_netif_impl(station));
    if(interface){LOCK_TCPIP_CORE();previous=interface->mtu;interface->mtu=1280;UNLOCK_TCPIP_CORE();}
    Serial.printf("{\"event\":\"mtu_probe\",\"applied\":%s,\"previous\":%u,\"current\":1280}\n",interface?"true":"false",previous);
  }
  void restore() {
    if(!interface)return;
    LOCK_TCPIP_CORE();interface->mtu=previous;UNLOCK_TCPIP_CORE();
    Serial.printf("{\"event\":\"mtu_probe_restored\",\"current\":%u}\n",previous);
    interface=nullptr;
  }
  ~NetworkMtuProbe(){restore();}
  NetworkMtuProbe(const NetworkMtuProbe&)=delete;
  NetworkMtuProbe& operator=(const NetworkMtuProbe&)=delete;
 private:
  struct netif* interface=nullptr;
  uint16_t previous=0;
};
