#pragma once
#include <stdint.h>

// User-confirmed signal assignments. Power connections are not inferred here.
namespace XiaoNuoPins {
constexpr uint8_t OLED_SDA = 21;
constexpr uint8_t OLED_SCL = 22;
constexpr uint8_t MIC_SCK = 14;
constexpr uint8_t MIC_WS = 27;
constexpr uint8_t MIC_SD = 32;
// INMP441 L/R is physically tied to GND (left channel).
constexpr uint8_t AMP_BCLK = 26;
constexpr uint8_t AMP_LRC = 25;
constexpr uint8_t AMP_DIN = 33;
}  // namespace XiaoNuoPins
