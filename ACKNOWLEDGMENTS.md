# 致谢

这个项目建立在许多人公开的工作上。下面按用途列出，许可证只写各仓库里能看到的声明；具体条款以对应仓库的许可证文件为准，复制或改写代码时请保留原有的版权、许可证和来源声明（见 [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md)）。

## 起点和上游

| 项目 | 用在哪里 | 许可证 |
| --- | --- | --- |
| [CrossDarwin/macOS-ForSheng](https://github.com/CrossDarwin/macOS-ForSheng) | 本项目的起点：sheng 上 macOS 26.5 的移植方法、构建脚本、XNU 补丁系列、PAC 和 UFS 的早期工作、QEMU 观察方法。本仓库的历史包含它的提交，作者信息保持原样 | GPL-3.0-or-later（早期版本为 MIT） |
| [UniXNU/UniXNU](https://github.com/UniXNU/UniXNU)（文档站 [unixnu.com](https://unixnu.com)） | UEFI 启动链、DarwinBoot、ArmCpu 和 ArmDTPlatform 平台层、构建驱动和组件目录 | BSD-2-Clause-Patent |
| [UniXNU/UniXNU-sheng](https://github.com/UniXNU/UniXNU-sheng)（设备页 [unixnu.com/devices/sheng](https://unixnu.com/devices/sheng)） | sheng 的板级支持：UFS、Novatek NT36532 触摸、CPU 频率探测，以及键盘保护套（GeniI2c、Wn8030、QcomRpmh）、ADSP 和电池、Wi-Fi 的上游代码 | BSD-2-Clause-Patent |
| [UniXNU/uefi-sheng](https://github.com/UniXNU/uefi-sheng) | sheng 的 UEFI 平台集成 | 见该仓库 |
| UniXNU 的组件仓库：[Adreno740](https://github.com/UniXNU/Adreno740)、[AdrenoPM4](https://github.com/UniXNU/AdrenoPM4)、[AdrenoFirmware](https://github.com/UniXNU/AdrenoFirmware)、[ArmSmmu4K](https://github.com/UniXNU/ArmSmmu4K)、[UniXNU-sm8550-common](https://github.com/UniXNU/UniXNU-sm8550-common)、[AdrenoMetal](https://github.com/UniXNU/AdrenoMetal)、AppleKeyStoreState | Adreno 内核栈、SMMU、SM8550 公共支持、Metal 前端；登录服务的 AppleKeyStore 应答方式 | 各自仓库，主要是 BSD-2-Clause-Patent |
| UniXNU 社区在 [unixnu.com/devices](https://unixnu.com/devices/) 上登记的 sheng 设备记录（页面上列出的各位作者的仓库） | 我们读过这些记录和其中的实机证据，用来对照自己的结果。上面的 UniXNU-sheng 是其中的官方记录 | 各自仓库 |

按 UniXNU 的设备记录要求，本仓库也在[移植状态](docs/status.md)里按区域给出状态和证据。我们不是 UniXNU 的官方设备仓库：UniXNU 移植的是 iPadOS，本项目移植 macOS。

## 参考和部分引用

| 项目 | 用在哪里 | 许可证 |
| --- | --- | --- |
| [Droid-VM/macOS-DroidVM](https://github.com/Droid-VM/macOS-DroidVM) | 显示和输入伴随驱动的参考（`DroidVMDisplay.cpp`、`DroidVMInput.cpp`），GPU 插件的起点 | BSD-2-Clause-Patent |
| [Acelogic/Snapintosh](https://github.com/Acelogic/Snapintosh)、[Acelogic/q1n1](https://github.com/Acelogic/q1n1)（Asahi m1n1 的分支） | 磁盘布局、ISC 测试介质、组件划分和整体方法的参考 | MIT 及各组件自己的许可证 |
| [Mesa](https://gitlab.freedesktop.org/mesa/mesa)（Turnip，Adreno 的 Vulkan 驱动） | GPU 用户态：加上 UniXNU 的 Darwin 后端补丁，在 CI 里构建 | MIT |
| [steelbrain/metal2vulkan](https://github.com/steelbrain/metal2vulkan) | GPU 用户态：把 Metal 调用转成 Vulkan 的桥。**包含它的构建产物不发布** | GPL-2.0 或更高 / LGPL-3.0 或更高 |
| [ianchb/sm8550-mainline](https://github.com/ianchb/sm8550-mainline)、主线 Linux 的 SM8550 设备树和驱动 | 寄存器布局和设备树的参考。没有把 Linux 派生的源码放进本仓库 | GPL-2.0 |
| [Nayuki QR-Code-generator](https://github.com/nayuki/QR-Code-generator) | panic 屏幕上的二维码（原样放在 `platform/sheng/macos26/log/qr/`） | MIT |
| [LongSoft/UEFITool](https://github.com/LongSoft/UEFITool) | 审计固件卷（构建时单独取用） | BSD-2-Clause |
| [dortania/KdkSupportPkg](https://github.com/dortania/KdkSupportPkg) | CI 从它的发布页下载固定版本的 Kernel Debug Kit | — |
| [apple-oss-distributions](https://github.com/apple-oss-distributions)（xnu、libplatform、libdispatch、libpthread、IOStorageFamily、IOHIDFamily、AvailabilityVersions） | 内核和用户态源码，按固定提交取用，不放进本仓库 | Apple Public Source License 2.0 及各自声明 |
| [ONLYA/corecrypto-2023](https://github.com/ONLYA/corecrypto-2023) | corecrypto 源码（构建输入，按固定提交取用） | 见该仓库 |

## 本项目自己的部分

原创的集成工具、测试、配置和文档按 GPL-3.0-or-later 提供，版权属于 macOS-ForSheng contributors（见 [LICENSE](LICENSE) 和 [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md)）。

如果你发现某个来源没有被列出或归属有误，请开 issue，我们会补上。
