# sheng macOS 实验仓库（已归档）

> **项目已归档（2026-11-12）：** 本仓库已停止更新并归档。上游 [UniXNU](https://github.com/UniXNU/UniXNU) 与 [UniXNU-sheng](https://github.com/UniXNU/UniXNU-sheng)（设备页 [unixnu.com/devices/sheng](https://unixnu.com/devices/sheng)）在小米平板 6S Pro（sheng）上已有进度更快的 macOS 实现（已推进到 macOS 27.3，支持 GPU 显示拷贝与直接扫描输出、CPU/GPU 动态调频、Wi-Fi 联网、键盘保护套与触摸板、音频及内核 work interval 补丁等）。如需查看最新进展，请直接访问上游仓库；本仓库仅作为 macOS 26.5（25F71）独立移植实验的历史记录保留。

在小米平板 6S Pro（sheng，高通 SM8550）上运行 macOS 26.5（25F71）的移植实验。起点是 [CrossDarwin/macOS-ForSheng](https://github.com/CrossDarwin/macOS-ForSheng)，启动链和板级支持来自 [UniXNU](https://unixnu.com)。

**这是实验项目，不适合日常使用，也不是可以安装的系统。** 仓库只有源码、测试和文档。不含苹果软件、厂商固件、系统镜像，也不提供制作系统镜像的方法。要在设备上试验，需要自己承担风险：设备要解锁 bootloader，每次启动都是 `fastboot boot`，不刷写启动和固件分区。

## 归档时做到什么

| 里程碑 | 证据 |
| --- | --- |
| 自编译内核在平板上启动，8 个 CPU 核心上线 | 实机，2026-11-07 |
| 进入 macOS Recovery 的图形界面 | 实机，2026-11-07 |
| 电源键、音量键当键盘用；触摸屏可用（单指） | 实机，2026-11-08 |
| 完整系统在平板上进入 CPU 软件绘制的桌面 | 实机，2026-11-09 |
| **完整系统进入经 GPU 合成的 macOS 桌面**（壁纸、菜单栏、小组件、程序坞，可打开访达、备忘录、系统设置、App Store、关于本机等应用，触摸可用） | 实机，2026-11-10 至 11-11 |
| **UFS 根分区持久写盘与菜单“重启”“关机”跑通**（写入严格限制在 OS 窗口内，重启与关机后 APFS 校验均为 0 错误，可离线提取崩溃报告，点菜单“重启”可正常回到安卓、点“关机”可正常关机） | 实机，2026-11-10 至 11-11 |

本仓库归档时未完成的部分（其中多项在上游 `UniXNU-sheng` 已有实现）：温控与 CPU/GPU 动态调频（本仓库固定跑在 401 MHz）、流畅帧率（瓶颈在每帧 0.36 到 0.40 秒的 CPU 表面读取比较与显示拷贝，每秒约 2.5 到 3 帧）、拖动滚动条不崩溃（本仓库内核缺少 work interval 支持，AppKit 主动中止）、Wi-Fi 联网（本仓库只跑到芯片引导阶段，无无线协议栈）、蓝牙、键盘保护套（第 3 级电压投票修复未上机）、USB、声音、电池状态、亮度调节、睡眠。全部状态、证据和限制见[移植状态](docs/status.md)。

## 文档

| 文档 | 内容 |
| --- | --- |
| [移植状态](docs/status.md) | 里程碑、归档时遗留的问题、按功能区域的状态和证据 |
| [技术路线](docs/technical-route.md) | 每一层怎么做、为什么这样做、哪些验证过 |
| [CI 说明](docs/ci.md) | 构建流程、历史运行记录和失败原因（构建在私有仓库进行，这里的 Actions 已关闭） |
| [仓库结构](docs/repository.md) | 分支、标签和目录 |
| [致谢](ACKNOWLEDGMENTS.md) | 上游项目和参考 |
| [第三方声明](THIRD_PARTY_NOTICES.md) | 许可证和来源 |

较早的文档在 `docs/` 下按主题放着，被取代的旧版本在 `docs/history/`。

## 许可证

原创的集成工具、测试、配置和文档按 GPL-3.0-or-later 提供（[LICENSE](LICENSE)）。仓库里的上游代码保留各自的许可证（BSD-2-Clause-Patent、MIT 等），见 [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md)。macOS 及其组件受苹果许可约束。

## English summary

**Archived (2026-11-12):** This experimental repository is archived and no longer updated. Upstream ([UniXNU](https://github.com/UniXNU/UniXNU) and [UniXNU-sheng](https://github.com/UniXNU/UniXNU-sheng), device page [unixnu.com/devices/sheng](https://unixnu.com/devices/sheng)) already has a faster-progressing macOS implementation for the Xiaomi Pad 6S Pro (`sheng`), including macOS 27.3 support, GPU framebuffer copy and experimental direct scanout, CPU/GPU dynamic frequency scaling, working Wi-Fi networking, keyboard cover and touchpad support, audio and kernel work-interval patches. Please refer to upstream for active development; this repository is preserved as a historical record of our macOS 26.5 (`25F71`) port.

In this macOS 26.5 (`25F71`) port to the Xiaomi Pad 6S Pro (`sheng`, Snapdragon 8 Gen 2 / SM8550, started from CrossDarwin/macOS-ForSheng and built on UniXNU), the Metal plug-in stack (WindowServer → Metal plug-in → metal2vulkan → Turnip → Adreno 740 kernel driver) on a single GPU command ring (`sheng-gpu-preempt=0`) at 401 MHz with IO-coherent GPU buffers boots into a full GPU-composited macOS desktop (wallpaper, menu bar, widgets, Dock) where Finder, Notes, System Settings, App Store and About This Mac open and touch works. Persistent disk writes (`native-ufs-root-writes=1`) to the OS window on UFS and clean restarts (`sheng-psci-reset=1`) and shutdowns from the Apple menu are verified across four tablet boots, with post-boot TWRP chunk scans and offline APFS Fletcher-64 checks confirming zero checksum failures and allowing crash reports to be extracted from the Data volume. Unfinished areas in this repository at the time of archiving: no thermal sensor driver or CPU/GPU dynamic frequency scaling (DCVS) on hardware, low frame rate (~2.5 to 3 fps, bottlenecked by the 0.36 to 0.40 s CPU surface read/compare and display copy rather than the GPU), an AppKit abort when dragging scroll bars (`Failed to create performance regulator work interval`), Photos crashing during Metal texture context creation, and no working Wi-Fi networking, Bluetooth, keyboard cover on hardware, USB, audio, battery reporting, brightness control or sleep. The repository contains source, tests and documentation only: no Apple software, vendor firmware or system images, no instructions for producing a system image, and the plug-in bundle containing metal2vulkan is not distributed. Every boot is a RAM-only `fastboot boot`; no boot or firmware partition is flashed. Status and evidence by area: [docs/status.md](docs/status.md). Licensing: GPL-3.0-or-later for original work, upstream licenses retained; see [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md) and [ACKNOWLEDGMENTS.md](ACKNOWLEDGMENTS.md).
