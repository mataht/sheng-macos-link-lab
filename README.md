# sheng macOS 实验仓库

在小米平板 6S Pro（sheng，高通 SM8550）上运行 macOS 26.5（25F71）的移植实验。起点是 [CrossDarwin/macOS-ForSheng](https://github.com/CrossDarwin/macOS-ForSheng)，启动链和板级支持来自 [UniXNU](https://unixnu.com)。

**这是实验项目，不适合日常使用，也不是可以安装的系统。** 仓库只有源码、测试和文档。不含苹果软件、厂商固件、系统镜像，也不提供制作系统镜像的方法。要在设备上试验，需要自己承担风险：设备要解锁 bootloader，每次启动都是 `fastboot boot`，不刷写分区。

## 现在做到什么

| 里程碑 | 证据 |
| --- | --- |
| 自编译内核在平板上启动，8 个 CPU 核心上线 | 实机，2026-11-07 |
| 进入 macOS Recovery 的图形界面 | 实机，2026-11-07 |
| 电源键、音量键当键盘用；触摸屏可用（单指） | 实机，2026-11-08 |
| 完整系统在平板上进入 CPU 软件绘制的桌面 | 实机，2026-11-09 |
| **第一次在平板上显示出经 GPU 合成的界面**（壁纸、菜单栏、设置助理，触摸可用，每秒约 6 帧） | 实机，2026-11-10 |

还没有的：可用的 GPU 加速（虽已出画面，但还很慢，每秒约 6 帧，显示等待与内核 fence 等待大量超时，而且带插件启动偶尔会变慢，原因未查清）、流畅操作（每帧显示拷贝花 0.1 到 0.4 秒，帧缓冲为 Device 映射写速约 79 MiB/s；CPU 软件绘制下画面读取和合成也很慢）、Wi-Fi（只跑到芯片引导阶段，后面没有无线协议栈）、蓝牙、USB、键盘保护套（第 3 级电压投票未收到完成信号）、声音、电池状态、亮度调节、睡眠、持久写盘。全部状态、证据和限制见[移植状态](docs/status.md)。

## 文档

| 文档 | 内容 |
| --- | --- |
| [移植状态](docs/status.md) | 里程碑、现有问题、按功能区域的状态和证据 |
| [技术路线](docs/technical-route.md) | 每一层怎么做、为什么这样做、哪些验证过 |
| [CI 说明](docs/ci.md) | 构建流程、历史运行记录和失败原因（构建在私有仓库进行，这里的 Actions 已关闭） |
| [仓库结构](docs/repository.md) | 分支、标签和目录 |
| [致谢](ACKNOWLEDGMENTS.md) | 上游项目和参考 |
| [第三方声明](THIRD_PARTY_NOTICES.md) | 许可证和来源 |

较早的文档在 `docs/` 下按主题放着，被取代的旧版本在 `docs/history/`。

## 许可证

原创的集成工具、测试、配置和文档按 GPL-3.0-or-later 提供（[LICENSE](LICENSE)）。仓库里的上游代码保留各自的许可证（BSD-2-Clause-Patent、MIT 等），见 [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md)。macOS 及其组件受苹果许可约束。

## English summary

An experimental port of macOS 26.5 (25F71) to the Xiaomi Pad 6S Pro (sheng, Snapdragon 8 Gen 2 / SM8550), started from CrossDarwin/macOS-ForSheng and built on UniXNU. The full system boots to the desktop on the tablet using CPU software rendering, and with the Metal plug-in stack (WindowServer → Metal plug-in → metal2vulkan → Turnip → Adreno 740 kernel driver) it has for the first time displayed a GPU-composited interface on the tablet (wallpaper, menu bar and post-login Setup Assistant pages, with working touch) at about 6 frames per second. GPU acceleration is not yet usable: most 50 ms display waits and 1517 of 2274 kernel fence waits time out, plug-in boots are intermittently slow for undetermined reasons, and per-frame display copy takes 0.1 to 0.4 seconds (the framebuffer is mapped as Device memory at about 79 MiB/s write). Wi-Fi (no wireless stack behind the chip boot stages), Bluetooth, USB, the keyboard cover (stage 3 voltage vote did not receive completion), audio, battery reporting, brightness control, sleep and persistent disk writes are not working. The repository contains source, tests and documentation only: no Apple software, vendor firmware or system images, no instructions for producing a system image, and the plug-in bundle containing metal2vulkan is not distributed. Every boot is a RAM-only `fastboot boot`; nothing is flashed. Status and evidence by area: [docs/status.md](docs/status.md). Licensing: GPL-3.0-or-later for original work, upstream licenses retained; see [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md) and [ACKNOWLEDGMENTS.md](ACKNOWLEDGMENTS.md).
