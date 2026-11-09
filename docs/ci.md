# CI 说明

本仓库的 GitHub Actions 只做一件事：在苹果芯片的 macOS runner 上，用苹果的工具链**编译和链接**本项目的内核与驱动，并检查结果。它不运行任何东西，不碰设备，不制作系统镜像。

截至 2026-11-09，仓库里共有 40 次运行：35 次成功，5 次失败，失败都是编译错误，下面逐条说明。这些运行都是本仓库于 2026-11-07 重建之后的记录，更早的构建记录不在这里。

## 1. 工作流

| 工作流 | 在哪个分支 | 触发 | 内容 |
| --- | --- | --- | --- |
| `hw-sheng-cn-1tb.yml`（CN 1 TB sheng hardware candidate build） | `main` 和各 `ci/*` 分支的代码里都有 | 推送到 `ci/hw-sheng-cn-1tb` 或 `ci/drv-*`，或手动 | `check` 任务：Linux 上跑本次改动涉及的测试（必须通过）和全部测试（仅供参考，部分测试需要本地才有的观察器）；`kernel` 任务：`xcode-27` runner，超时 180 分钟，编译并链接内核和所有驱动 |
| `gpu-userland.yml`（GPU userland build） | 只在 `ci/gpu-userland` | 推送到 `ci/gpu-userland`，或手动 | 构建 Turnip（Mesa 的 Adreno Vulkan 驱动）、Rust 桥和 Metal 插件，`xcode-27` runner |
| `bringup.yml`、`qemu-media.yml` | `main` 和各 `ci/*` 分支的代码里都有 | 只能手动 | 早期的内核构建流程和隔离 APFS 测试介质的生成 |
| `arm64e-link.yml` | 只在 `ci/apple-arm64e-link` | — | 本仓库最初的链接实验（见 [repository.md](repository.md)） |

推送到 `main` 不会触发任何构建。想让某个分支跑 CI，把它推到对应的 `ci/` 分支。

## 2. 构建做什么

`kernel` 任务的顺序：

1. 恢复固定提交的源码（XNU、UniXNU 系列等，见 `sources.lock.json`），应用本项目的补丁；
2. 下载固定版本的苹果 Kernel Debug Kit（KDK）。KDK 取自公开的 `dortania/KdkSupportPkg` 发布页，在 runner 上校验大小和苹果签名。它只提供编译用的内核头文件和审计输入，**不进入产物**；KDK 的 dmg 会留在 runner 的 Actions 缓存里，不对外提供下载；
3. `build_macos26_xnu.py --link-kernel` 编译并链接 SHENG 内核，同时构建所有驱动（存储、显示、输入、触摸、CPU 频率、GPU、登录服务、键盘保护套、RTC、背光、内存性能读取、Wi-Fi、USB、ADSP/电池），并做 ABI 审计：虚函数表的槽位数、PAC 元数据、不接受弱导入；
4. 上传两个产物（保留 7 天）：
   - `hw-sheng-cn-1tb-kernel-<run id>`：`build.json`（所有输出的 SHA-256 和构建参数）、`kernel.development.sheng` 及其 map 和 dSYM、存储驱动和 UFS 驱动目录；
   - `hw-sheng-cn-1tb-diagnostics-<run id>`：KDK 校验记录、日志、`compile_commands.json`。

**CI 不做的事**：不运行内核（没有 QEMU 和实机）；不装配内核集合（那一步要用苹果的内核扩展，在本地完成，结果不发布）；不下载或制作系统镜像；不产出任何包含苹果二进制、厂商固件的文件。所以“CI 成功”只说明编译链接和静态审计通过，不说明代码能在平板上工作。

## 3. 产物只保留 7 天

产物过期后不能再下载，但运行记录和日志仍在。每次构建的 `build.json` 记录了输出的 SHA-256；本项目自己在使用产物前会核对 GitHub API 给出的摘要。需要产物时，重新手动运行对应的工作流。

## 4. 每次运行

结果一栏的 ✓ 是成功，✗ 是失败。日期是运行开始的 UTC 日期。“用途”只写有记录的。

### `ci/hw-sheng-cn-1tb`

| 运行 | 提交 | 日期 | 结果 | 用途 |
| --- | --- | --- | --- | --- |
| [42369896392](https://github.com/mataht/sheng-macos-link-lab/actions/runs/42369896392) | `4f9b9a7` | 11-07 | ✓ | HID 和按键的内核候选 |
| [42388402792](https://github.com/mataht/sheng-macos-link-lab/actions/runs/42388402792) | `0333d51` | 11-07 | ✓ | 显示“只拷变化的行”的第一版，被下一次取代 |
| [42388955103](https://github.com/mataht/sheng-macos-link-lab/actions/runs/42388955103) | `201bb2a` | 11-07 | ✓ | 干净的行拷贝写入；平板上 Recovery 的显示和按键用这次构建 |
| [42445927283](https://github.com/mataht/sheng-macos-link-lab/actions/runs/42445927283) | `4e85243` | 11-08 | ✓ | 第一次编译触摸驱动 |
| [42452998211](https://github.com/mataht/sheng-macos-link-lab/actions/runs/42452998211) | `5c02fb7` | 11-08 | ✓ | 修复触摸驱动的弱导入 |
| [42457862443](https://github.com/mataht/sheng-macos-link-lab/actions/runs/42457862443) | `d38cb21` | 11-08 | ✓ | 触摸读取状态报告；平板上触摸的第 1、2、4 级用这次构建 |
| [42470720224](https://github.com/mataht/sheng-macos-link-lab/actions/runs/42470720224) | `f5b9466` | 11-08 | ✓ | CPU 频率只读读取 |
| [42476904571](https://github.com/mataht/sheng-macos-link-lab/actions/runs/42476904571) | `1fa6561` | 11-08 | ✓ | 完整系统的显示页表 |
| [42487807753](https://github.com/mataht/sheng-macos-link-lab/actions/runs/42487807753) | `d0678aa` | 11-08 | ✓ | 共享缓存里 dyld 的页 |
| [42503540405](https://github.com/mataht/sheng-macos-link-lab/actions/runs/42503540405) | `bdfe089` | 11-08 | ✓ | 完整系统根卷和内存覆盖层；平板上第一次完整系统启动 |
| [42516981945](https://github.com/mataht/sheng-macos-link-lab/actions/runs/42516981945) | `64c5456` | 11-08 | ✓ | 启动日志面板回绕写入 |

### `ci/drv-integrated`

| 运行 | 提交 | 日期 | 结果 | 用途 |
| --- | --- | --- | --- | --- |
| [42576253178](https://github.com/mataht/sheng-macos-link-lab/actions/runs/42576253178) | `52667ef` | 11-09 | ✓ | 八个驱动分支合并后的构建；平板上进入桌面用的是这次构建 |

### 驱动分支 `ci/drv-*`

每个驱动在自己的分支上编译，合并前互不影响。

| 分支 | 运行（提交） | 说明 |
| --- | --- | --- |
| `ci/drv-wifi-bt` | [42544757590](https://github.com/mataht/sheng-macos-link-lab/actions/runs/42544757590) ✓（`b1bbc94`） | Wi-Fi 第 1 到 3 级 |
| `ci/drv-keyboard` | [42545245701](https://github.com/mataht/sheng-macos-link-lab/actions/runs/42545245701) ✓（`53b52be`） | 键盘保护套 |
| `ci/drv-touch-display` | [42545275464](https://github.com/mataht/sheng-macos-link-lab/actions/runs/42545275464) ✓（`827ac96`） | 触摸和显示诊断、软件光标页 |
| `ci/drv-gpu` | [42545288533](https://github.com/mataht/sheng-macos-link-lab/actions/runs/42545288533) ✓（`59aedd0`） | Adreno 内核栈 |
| `ci/drv-power-misc` | [42545788717](https://github.com/mataht/sheng-macos-link-lab/actions/runs/42545788717) ✗（`f3fac00`），[42547159133](https://github.com/mataht/sheng-macos-link-lab/actions/runs/42547159133) ✓（`2150a23`），[42613409642](https://github.com/mataht/sheng-macos-link-lab/actions/runs/42613409642) ✓（`d434817`） | RTC、背光、内存性能读取、PSCI 复位 |
| `ci/drv-adsp-battery` | [42546203537](https://github.com/mataht/sheng-macos-link-lab/actions/runs/42546203537) ✓（`d5dbc3d`），[42547916729](https://github.com/mataht/sheng-macos-link-lab/actions/runs/42547916729) ✓（`4a1ef70`） | ADSP 和电池 |
| `ci/drv-login` | [42546446517](https://github.com/mataht/sheng-macos-link-lab/actions/runs/42546446517) ✓（`2de7976`），[42551354002](https://github.com/mataht/sheng-macos-link-lab/actions/runs/42551354002) ✓（`43cdffe`），[42564329157](https://github.com/mataht/sheng-macos-link-lab/actions/runs/42564329157) ✓（`40b53fa`） | 最小的 AppleKeyStore 和 IOWatchdog 服务 |
| `ci/drv-usb` | [42549544686](https://github.com/mataht/sheng-macos-link-lab/actions/runs/42549544686) ✓（`a2508cd`，第 1 到 2 级），[42551178632](https://github.com/mataht/sheng-macos-link-lab/actions/runs/42551178632) ✓（`5163a7f`，第 1 到 4 级） | USB |

### GPU

| 分支 | 运行（提交） | 说明 |
| --- | --- | --- |
| `ci/gpu-probe` | [42502601827](https://github.com/mataht/sheng-macos-link-lab/actions/runs/42502601827) ✓（`7f75a44`），[42548826074](https://github.com/mataht/sheng-macos-link-lab/actions/runs/42548826074) ✓（`51d582d`），[42555005299](https://github.com/mataht/sheng-macos-link-lab/actions/runs/42555005299) ✓（`8ec5676`），[42564084129](https://github.com/mataht/sheng-macos-link-lab/actions/runs/42564084129) ✗（`5b93da8`），[42565690803](https://github.com/mataht/sheng-macos-link-lab/actions/runs/42565690803) ✓（`2a02df3`），[42592006666](https://github.com/mataht/sheng-macos-link-lab/actions/runs/42592006666) ✓（`d4cf92a`） | QEMU 专用的 GPU 探针：空插件、经设备树下发真插件、假的 A740、退出原因追踪 |
| `ci/gpu-adreno` | [42506833993](https://github.com/mataht/sheng-macos-link-lab/actions/runs/42506833993) ✗（`a5532cb`），[42508147355](https://github.com/mataht/sheng-macos-link-lab/actions/runs/42508147355) ✗（`0d973fc`），[42509247225](https://github.com/mataht/sheng-macos-link-lab/actions/runs/42509247225) ✓（`72b46a8`），[42510635448](https://github.com/mataht/sheng-macos-link-lab/actions/runs/42510635448) ✓（`3582bbe`） | Adreno 移植的第一版，只做了集成检查，没有上机 |
| `ci/gpu-userland` | [42517252994](https://github.com/mataht/sheng-macos-link-lab/actions/runs/42517252994) ✓（`c72c1d4`），[42541922218](https://github.com/mataht/sheng-macos-link-lab/actions/runs/42541922218) ✗（`10049e5`），[42543296940](https://github.com/mataht/sheng-macos-link-lab/actions/runs/42543296940) ✓（`b8fe7a1`），[42548818622](https://github.com/mataht/sheng-macos-link-lab/actions/runs/42548818622) ✓（`2abbb51`） | Turnip、Rust 桥、完整的 Metal 插件 |

**GPU 用户态的产物不发布。** `ci/gpu-userland` 的插件里含有 metal2vulkan（GPL-2.0 或更高 / LGPL-3.0 或更高），在这个许可证问题处理清楚之前，这些产物只作为 CI 产物保留 7 天，不转发、不放进任何发布版本。

## 5. 失败的运行

5 次失败的运行都不是环境问题，是我们的代码或上游代码在苹果编译器的 `-Werror` 下被拒绝（Rust 桥那一次是工具版本不一致），各自后续的提交已经修复：

| 运行 | 分支 | 原因 | 之后 |
| --- | --- | --- | --- |
| 42506833993 | `ci/gpu-adreno` | 上游 `GpuPowerProbe.cpp` 里对 `vm_page_free_count` 取地址再判断真假（`-Wpointer-bool-conversion`） | 下一次提交修了这处，接着暴露了下一行的问题 |
| 42508147355 | `ci/gpu-adreno` | 同一文件里未使用的变量和函数（`-Wunused-variable`、`-Wunused-function`） | 42509247225 通过 |
| 42541922218 | `ci/gpu-userland` | Rust 桥构建完成后，Xcode 自带的 `nm` 读不了 Rust 工具链生成的目标文件（`Unknown attribute kind (105)`，两边的 LLVM 版本不同） | 42543296940 通过 |
| 42545788717 | `ci/drv-power-misc` | `native_memperf.cpp` 里两个不同枚举类型的按位运算（`-Wenum-enum-conversion`） | 42547159133 通过 |
| 42564084129 | `ci/gpu-probe` | `native_gpu_probe.cpp` 里同类的枚举按位运算 | 42565690803 通过 |

## 6. 怎么读 CI 结果

- 成功 = 编译、链接和静态审计通过。
- 想知道某次构建在平板上有什么结果，看[移植状态](status.md)的里程碑和 [技术路线](technical-route.md) 里的“证据”标记：只有标了“实机”的才是平板上观察到的。
- 同一个驱动分支会有多次运行，后面的提交取代前面的；`ci/drv-integrated` 是它们合并后的结果。
