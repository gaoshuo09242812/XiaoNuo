#pragma once
#include "pins.h"

namespace XiaoNuoConfig {
constexpr const char* VERSION = "0.6.73-expression";
// Speech needs more headroom than the initial /32 diagnostic setting.
// Keep a fixed maximum; host volume still scales below this limit.
constexpr uint8_t SPEAKER_GAIN_DIVISOR = 2;
constexpr unsigned long SERIAL_BAUD = 460800;
constexpr uint32_t WIFI_CONNECT_TIMEOUT_MS = 20000;
constexpr uint32_t SETUP_TIMEOUT_MS = 10 * 60 * 1000;
constexpr uint32_t HOST_TIMEOUT_MS = 12000;
constexpr uint8_t MAX_WIFI_ATTEMPTS = 5;
constexpr uint32_t WIFI_RETRY_COOLDOWN_MS = 30 * 1000;
constexpr size_t MAX_COMMAND_LENGTH = 384;
// Output availability and serial limits.
constexpr bool SPEAKER_OUTPUT_AVAILABLE = true;
constexpr bool VOICE_WAKE_AVAILABLE = false;
}

















































