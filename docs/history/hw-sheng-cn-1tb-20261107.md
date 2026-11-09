> 历史记录：本文写于 2026-11-07 之前，当时尚未执行任何平板操作；现状见 [../status.md](../status.md)。

# 分支 hw/sheng-cn-1tb：国行 16 GB / 1 TB sheng 实机候选

基线：`ac397dd`（a200d204 加上共享工作区的本地状态）。本分支只做代码和离线验证，没有推送，也没有触发 CI。
所有平板操作（备份 GPT、改分区、写镜像、`fastboot boot`）都还没有执行，每一步执行前都要用户单独同意。

## 提交

| 提交 | 内容 |
| --- | --- |
| `e3dbdf6` | macoslog 从 LUN4 挪到 LUN0（`sheng-cn-1tb-lun0`：第 30 项，第 36649216–36665599 块，32 项 GPT）。存储头文件改为由构建脚本传入几何参数，缺参数时编译报错；上游 LUN4 布局保留，作为第二套测试 |
| `6e43a29` | `repartition_sheng_cn_1tb.py`：userdata 缩到 128 GiB，其后依次是 macoslog（64 MiB）和 macosroot（32 GiB），剩下约 811 GiB 不分配。顺序为先写备份表、再写主表、最后写 FAT16，每步读回校验 |
| `5ff2122` | 内存窗口和 UEFI 日志保存器，详见下文 |
| `af9dc48` | 显示：改用 Stage18B 实际运行过的源码，加实机保持唤醒开关，详见下文 |

## 内存（`5ff2122`）

- `patches/uefi-sheng/0004`：按本机 `/memory` 和保留区重写高地址内存表。新增 `0xA00000000` 起的 8 GiB，去掉 `mpss_dsm` 和本机不存在的两个空洞。于是 `0x8C0000000–0xC00000000` 成为一段连续的 13 GiB。
- `patches/UniXNU/0041`、`patches/UniXNU-sheng/0009`：DarwinBoot 和设备树生成器的上限从 8 GiB 放宽到 13 GiB（`0x340000000`）。
- `package_macos26_ufs.py` 申请 12 GiB，给 UEFI 自己从高地址往下的分配留 1 GiB。固件记录里缺上面任何一个补丁就拒绝打包。`--dram-size` 保持不变。
- XNU 仍然只认一段连续内存，其余约 3 GB 用不上。用满 16 GB 要改内核，属于后期工作。

## UEFI 日志保存器（`5ff2122`，修 `e3dbdf6` 留下的缺口）

`patches/UniXNU/0042`：DarwinBoot 的 `NativeLogSave.h` 原来写死了上游 LUN4 的分区 GUID 和起止块号，内存范围也只接受 `0x8C0000000–0xA00000000`。不改的话，实机上 UEFI 找不到新的 macoslog。现在它与 `config/macoslog.json` 一致，内存范围与 `capture_persistent_log.py` 相同（三段）。新测试会核对补丁后的头文件和这两处配置是否一致。

## 显示（`af9dc48`）

- `platform/sheng/macos26/display/` 原来还是 Stage15 之前的版本。现在由 `scripts/derive_hw_display_keep_awake.py` 生成：它先校验 Stage15B/18B 各输入文件的哈希，并能逐字节重新生成 Stage18B 的补丁，然后才加入实机改动。
- 新开关 `native-gop-keep-awake=1`：默认关闭；只在 `PE_vmm_present == 0` 时生效，不能和 VM 开关同时用。启用后每 30 秒调用一次原版 `IOPMrootDomain::requestUserActive`，原因字符串为 `sheng-native-display-keep-awake`。
- VM 开关的判断条件、原因字符串、日志文本，以及观测脚本读取的 `gVMKeepAwake*` 符号都不变。类声明、stop、像素拷贝和电源状态代码逐字节不变。
- 打包加 `--display-keep-awake`，它通过 `hw-keep-awake.json` 绑定到这一份补丁。
- `requestUserActive` 没有返回值。日志里显示“已提交”，不等于屏幕确实保持点亮。系统睡眠另外靠设备树的 `sleep-disabled` 阻止。

## 哪些在本地完成，哪些要 CI

- **本地**：UEFI 固件（uefi-sheng 0004；UniXNU 0040–0042；UniXNU-sheng 0009）和全部 Python 工具。
- **需要 macOS CI**：`NativeUfs.o`（存储驱动和 GOP 显示在同一个模块里）。macoslog 的几何参数和新的显示补丁都要重新编译。现有实验室工作流是在上游 a200d204 上叠加显示改动，存储部分仍用上游的写法，所以不能直接用。需要一个从本分支源码构建的工作流。推送和触发 CI 之前要问用户。

## 离线验证

- 三组补丁都能按顺序打到固定版本的原始源码上（UniXNU 41 个，uefi-sheng 4 个，UniXNU-sheng 9 个），结果与 `hw-build/fw-tree` 中被补丁改到的文件逐字节一致。
- 真实的 `prepare_macos26_gop.py` 在不联网的情况下能应用新的显示补丁（`--fuzz=0`），帧缓冲 ABI 头文件的哈希也对得上。
- 全部测试：142 个，失败 10 个。原因都是环境：缺 `bootcontract`；缺两个本地未跟踪的 QEMU 观测脚本；clang 的 ASan 运行库不存在；缺 OpenSSL 头文件；WSL 里的 `pkg-config` 指向 Windows 程序。与基线对比，没有新出现的失败类型。
- 固件的 arm64e 内核模块还没有编译过，QEMU 和实机都没有运行过。

## 尚未确认

- `0xA00000000` 起的 8 GiB 和 `0x8C0000000` 那段的最后 1 GiB，都还没有在实机上被 UEFI 或 XNU 实际使用过。
- UEFI 自身的分配是否会把这 13 GiB 切碎，导致 12 GiB 的窗口分配失败。
- LUN0 的备份 GPT 还没有读过。测试里的备份表是合成的，执行 `prepare` 前必须先有一份新的 `backup_device` 基线。
- 实机保持唤醒只是提交请求；屏幕是否真的一直亮着，要靠实机观察。

## Step18 实机反馈后的修正（2026-11-07）

实机（包 `f5d5df34…`，内核 `1aaea8ed…`）已到语言选择界面，按键能改选中语言。但出现了两个问题。

- **列表隔行残留蓝条**
  - 现象：按音量下后，第 1、3、5 行（另一张照片是第 2、4、6 行）同时显示蓝色选中条。
  - 原因（推断，与两张照片逐行吻合）：WindowServer 交替使用两块根缓冲，每帧只重画变化的区域，并假定这块缓冲里还保留着上一帧。驱动每次把整块缓冲拷上屏，就把另一块缓冲漏掉的更新又写了回去。
  - 为什么不能只按像素判断：一行被重画回两帧前的样子时，看起来"没变"。
  - 修法：`scripts/derive_hw_display_copy_forward.py` 在面板尺寸补丁之后生成新的显示补丁，记录在 `hw-copy-forward.json`，始终启用，没有开关。
    - 这一帧换完之前，把正在显示的缓冲同步到其他同尺寸的缓存缓冲里，让 WindowServer 的假定成立。
    - 前提：WindowServer 在交换完成前不会合成下一帧（`IOMFBDisplay::can_update_` 会先问 `swap_wait`）。
    - 如果缓冲本来就完整，这样做也没有副作用。
  - `tests/native_gop_blit.cpp` 加了交替缓冲模型：没有同步时会出现残留，同步后与真值一致。
- **两个音量键同按时先发了一个方向键**
  - 修法：`spmi_keys.h` 新增 `Chord`。单个音量键先等 80 ms（4 次轮询）；窗口内另一个键也按下，就只发 Tab。窗口内松开则补发一次按下和松开。电源键不延迟。
- **打包器**
  - `qualify_panel_size` 和 `qualify_display_keep_awake` 接受这一链式补丁。
  - 新增 `--display-waiver`（`scripts/display_waiver_evidence.py`）：用户明确豁免同核 QEMU 完整显示窗口时使用。它绑定同核的半途 fixture，只允许豁免"首帧"和"绘图页 0"两项。

以上未经 macOS 编译，也未在 QEMU 或实机上验证。
