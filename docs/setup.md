# 从零搭建

以下为 Windows 管理电脑、经典 ESP32 4 MB 和自建 Cloudflare Worker 的安装路线。先按[硬件指南](hardware.md)接好设备。每次使用时，电脑与小糯可以在不同网络，双方都需要能访问自己的中转。

## 1. 准备电脑

安装 Python 3.14 x64、Arduino IDE 或 Arduino CLI。中转部署另需 Node.js、npm 及自己的 Cloudflare 账号。

```powershell
py -3.14 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
Copy-Item .env.example .env.local
.\.venv\Scripts\python.exe manage.py init
```

只在首次安装时复制配置模板。`init` 会在本机生成管理口令。保持示例的付费开关关闭，先验证本机控制台和设备。`.env.local` 与 `data/` 保存私有数据。

## 2. 编译和绑定设备

在 Arduino 中打开 `firmware/XiaoNuo/XiaoNuo.ino`，选 **ESP32 Dev Module**、4 MB 闪存、DIO、40 MHz、默认 4 MB 分区方案。参考工具链为 ESP32 Arduino 3.3.10 系列、U8g2 2.36.19、ArduinoJson 7.4.3、WebSockets 2.7.2。分区需要默认的 1.25 MiB 应用槽及地址 `0x290000`、大小 `0x160000` 的 `spiffs` 数据分区。固件会在合格的空白分区中自行建立回复缓存。曾装过其他程序的开发板需先由维护者确认数据分区，尤其是原数据需要保留时。

先用仓库默认空域名编译并上传。后续更新保留 NVS，关闭 Arduino 的整片擦除选项。连接 USB 数据线并确认端口。

```powershell
.\.venv\Scripts\python.exe -m serial.tools.list_ports
```

在 `.env.local` 的 `XIAONUO_USB_PORT` 填该端口。运行下列命令，保持服务窗口运行。另一个窗口执行 `open` 打开控制台。

```powershell
.\.venv\Scripts\python.exe manage.py serve
.\.venv\Scripts\python.exe manage.py open
```

在“设备连接”检查 USB，按屏幕型号选择 SSD1306 或 SH1106。检查屏幕显示和麦克风，再点“登记当前设备”。确认列表出现已绑定设备。设备密钥由流程生成，后端保存摘要。

## 3. 安装语音与聊天模型

```powershell
.\.venv\Scripts\python.exe tools/setup_local_speech.py
.\.venv\Scripts\python.exe tools/setup_neural_tts.py
```

两项工具分别下载 Vosk 中文识别和 Kokoro 本地语音模型，并校验模型文件。模型存于本机 `data/models/`。下载源和许可见[第三方说明](../THIRD_PARTY_NOTICES.md)。

选择一个聊天方案：

- **本机模型**。运行 `python tools/setup_local_llm.py --activate`（用虚拟环境 Python）。这是 Windows x64 CPU 安装器，下载 llama.cpp 和 Qwen3 GGUF。保留 `XIAONUO_NETWORK_CHAT_PROVIDER=local` 和远程、付费开关关闭。需要足够磁盘、内存和处理时间。
- **DeepSeek**。核对账号地区、账户可用情况及费用，先在 `.env.local` 设定自己的月预算和真实部署成本。使用 `python tools/configure_deepseek.py --configure` 在本机输入 API Key。它会检查账户并启用配置。需要时加 `--verify-chat` 发起一次计费测试。价格记录失效、预留不足或账户余额不足时，应用会拦截付费请求。

## 4. 建立自己的中转

设备绑定完成后，在电脑上结束第 2 步的 `serve`（Ctrl+C）。登录自己的 Cloudflare 账号，找到 Account ID 和 Workers 子域名前缀。

```powershell
.\.venv\Scripts\python.exe tools/prepare_worker_relay.py --account-id <账号ID> --subdomain <Workers子域名前缀>
npx wrangler@4.154.0 login
npx wrangler@4.154.0 deploy --config data/worker-relay/wrangler-production.json
```

将尖括号参数替换为自己的值。`prepare_worker_relay` 会生成本机私有主机凭据和已绑定设备白名单。部署前核对生成的 Worker 名和账号。连接配置在 `data/worker-relay/private-host.json`。若设备名单更改，先备份生成的部署配置，再重新生成白名单。保留已有主机凭据。

在自己的固件副本里改 `network_endpoint.h` 的 `HOST` 为 Worker 的完整域名（无协议、无路径），使用 HTTPS 端口 `443`。核对目标域名的证书信任链，`CA_PEM` 应为验证该站所需的受信任根 CA。保留 TLS 和主机名校验。`RECOVERY_PEERS` 可留空。重新编译上传，保留绑定。访问 `https://你的域名/healthz` 检查入口连通。

项目也提供 Pages 转发源 `relay/pages`。如果使用它，先按 Cloudflare Pages 的方式绑定自己的 Worker 到 `RELAY_SERVICE`，再把设备的 `HOST` 指向自己的 Pages 域名并核对证书。先用浏览器检查自己部署的 `/healthz`，再验证两端真实 WebSocket。部署和网络权限依账号及当地网络而定。

## 5. 配置热点并验证

需要叫名唤醒时，先由管理者确认同意，然后在 `.env.local` 设 `XIAONUO_COMPUTER_WAKE_ENABLED=true`。配置修改后重新启动后台生效。

```powershell
.\.venv\Scripts\python.exe tools/run_worker_relay_host.py --with-usb
```

这是前台调试入口。另一个窗口用 `manage.py open` 打开管理页，在“设备连接”点“开启手机配网”。手机加入 OLED 显示的临时 Wi-Fi，打开 `http://192.168.4.1`，填写真正的 2.4 GHz 热点名和密码。根据同意情况勾选“允许电脑唤醒”。使用叫名唤醒时，控制台“互动偏好”也要打开电脑唤醒并保存。

手机重新打开目标热点，等设备连通并回到两只眼睛。用“小糯，你好”测试真实听感，再测一次提醒。USB 手动收音和播放用于调试，日常无线模式可换成 5 V 充电器供电。

需要 Windows 后台时，先结束前台进程，运行 `python tools/manage_worker_service.py start`。自启动可用 `install-autostart`，完成后自行检查重新登录时是否正常。`stop` 会持久停用，之后用 `start` 恢复。`tools/check_delivery_status.py` 只读检查，实际语音、显示和提醒仍需亲自验证。
