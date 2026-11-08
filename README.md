# 小糯 XiaoNuo

小糯是一台用经典 ESP32 制作的桌面语音陪伴机器人。它用 OLED 显示眼睛和工作状态，通过 I²S 麦克风收音、功放播音。电脑负责中文识别、对话和语音合成，设备可以通过手机热点联网。

- [从选材到接线](docs/hardware.md) · [材料表 CSV](docs/bom.csv)
- [从零搭建与部署](docs/setup.md)
- [家庭使用、隐私与排障](docs/user-guide.md)
- [项目许可](LICENSE) · [第三方许可及来源](THIRD_PARTY_NOTICES.md)

固件使用已在实物试用的 073 功能基线。公开源码提供 ESP32 固件、电脑后台、浏览器控制台、Worker 中转、部署工具和测试。新安装需要自己的设备绑定、网络和模型配置。出厂示例中的中转域名留空，付费请求、电脑唤醒和记忆云端使用默认关闭。

功能包括叫名唤醒、中文对话、经同意保存的记忆、提醒和眼睛表情。播放中请等小糯说完再开口。较长回复准备时间受语音合成和网络影响。持续在线、自动登录启动和跨不同硬件批次需要在各自环境验收。提醒受联网、发声和空闲状态影响，请用合适的方式处理重要提醒。

## 开发检查

在 Windows 的 PowerShell 中从仓库根目录运行。发布整理在 Python 3.14 环境检查，TLS 测试需要 OpenSSL。

```powershell
py -3.14 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements-dev.txt
$env:PYTHONPATH = "backend;tests;."
.\.venv\Scripts\python.exe -m unittest discover -s tests
```

公开版在 Windows 上通过 557 项电脑端测试。ESP32 3.3.10-cn 工具链分别编译了空域名配置和填写示例域名的配置，均符合默认应用分区容量。实际供电、收音、播放和自建中转还需在组装后的设备验证。安装和烧录参数见搭建指南。
