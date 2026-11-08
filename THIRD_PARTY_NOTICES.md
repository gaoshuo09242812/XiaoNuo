# 第三方许可及来源

项目自编代码和文档使用 [MIT](LICENSE)。第三方组件按各自许可使用。

- OLED 中文子集 `firmware/XiaoNuo/status_font.h` 来自文泉驿 Bitmap Song，通过 U8g2 字库转码和裁剪。文泉驿字体使用 GPL v2 加字体嵌入例外。见 [上游 BDF 版权头](LICENSES/WenQuanYi-BDF-notice.txt)、[GPL v2](LICENSES/GPL-2.0.txt)、[字体来源](https://github.com/larryli/u8g2_wqy/tree/master/bdf) 和 [U8g2 字体说明](https://github.com/olikraus/u8g2/wiki/fntgrpwqy)。可用 U8g2 2.36.19 的 `src/clib/u8g2_fonts.c` 作为 `tools/subset_oled_font.py` 的输入重建。字形内容保留，索引经过裁剪。
- [U8g2](https://github.com/olikraus/u8g2) 的库代码使用 BSD-2-Clause，见 [许可](LICENSES/U8g2.txt)。[Larry Li 的转换工具仓库许可](LICENSES/WenQuanYi-COPYING.txt)适用于其代码，字体按上面的单独许可。
- 通过 Arduino 工具安装 [Arduino ESP32](https://github.com/espressif/arduino-esp32)、[ArduinoJson](https://github.com/bblanchon/ArduinoJson) 和 [arduinoWebSockets](https://github.com/Links2004/arduinoWebSockets)。安装包随上游许可发布。
- 本机模型由安装脚本从上游下载。查看 [Vosk 中文模型及许可](https://alphacephei.com/vosk/models)、[sherpa-onnx 语音模型](https://github.com/k2-fsa/sherpa-onnx/releases/tag/tts-models)、[Kokoro 中文模型](https://huggingface.co/hexgrad/Kokoro-82M-v1.1-zh)、[Qwen3 GGUF](https://huggingface.co/Qwen/Qwen3-1.7B-GGUF) 和 [llama.cpp](https://github.com/ggml-org/llama.cpp)。模型权重和第三方二进制分开下载。
- TLS 根证书参考 [ISRG 公共根证书](https://letsencrypt.org/certificates/)。自建域名应根据实际证书链配置受信任根。
- Cloudflare 和 DeepSeek 是可选的第三方服务。使用各自账号时遵循对应服务条款及计费规则。
