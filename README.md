# sheng macOS 实验仓库

在小米平板 6S Pro（sheng，高通 SM8550）上运行 macOS 26.5（25F71）的移植实验。起点是 [CrossDarwin/macOS-ForSheng](https://github.com/CrossDarwin/macOS-ForSheng)，启动链和板级支持来自 [UniXNU](https://unixnu.com)。

**这是实验项目，不适合日常使用，也不是可以安装的系统。** 仓库只有源码、测试和文档。不含苹果软件、厂商固件、系统镜像，也不提供制作系统镜像的方法。要在设备上试验，需要自己承担风险：设备要解锁 bootloader，每次启动都是 `fastboot boot`，不刷写分区。

## 现在做到什么

| 里程碑 | 证据 |
| --- | --- |
| 自编译内核在平板上启动，8 个 CPU 核心上线 | 实机，2026-11-07 |
| 进入 macOS Recovery 的图形界面 | 实机，2026-11-07 |
| 电源键、音量键当键盘用；触摸屏可用（单指） | 实机，2026-11-08 |
| **完整系统在平板上进入桌面**（约 2 分钟，CPU 软件绘制） | 实机，2026-11-09 |

还没有的：GPU 加速（内核栈在平板上跑到了第 9 级，Metal 插件也启动过一次，但画面是黑的，没有任何由 GPU 渲染的画面）、流畅操作（触摸到画面的延迟很大，原因没查清）、Wi-Fi、蓝牙、USB、声音、电池状态、亮度调节、睡眠、持久写盘。最新的第二版集成内核没有一次完整的 QEMU 完整系统运行，当晚的上机是在用户逐包放弃这一条的前提下进行的（见[验证上的缺口](docs/status.md#5-验证上的缺口)）。全部状态、证据和限制见[移植状态](docs/status.md)。

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

An experimental port of macOS 26.5 (25F71) to the Xiaomi Pad 6S Pro (sheng, Snapdragon 8 Gen 2 / SM8550), started from CrossDarwin/macOS-ForSheng and built on UniXNU. As of 2026-11-09 the full system boots to the desktop on the tablet in about two minutes using CPU software rendering; touch and the hardware keys work. There is no GPU acceleration (the GPU kernel stack runs through stage 9 on the tablet and one Metal plug-in boot reached the real A740, but the screen stayed black), the desktop is very laggy, and Wi-Fi, Bluetooth, USB, audio and battery reporting are not working. The repository contains source, tests and documentation only: no Apple software, vendor firmware or system images, and no instructions for producing a system image. The latest integrated kernel has no complete full-system QEMU run; its tablet boots were made under per-package user waivers of that one gate (see docs/status.md, section 5). Every boot is a RAM-only `fastboot boot`; nothing is flashed. Status and evidence by area: [docs/status.md](docs/status.md). Licensing: GPL-3.0-or-later for original work, upstream licenses retained; see [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md) and [ACKNOWLEDGMENTS.md](ACKNOWLEDGMENTS.md).
