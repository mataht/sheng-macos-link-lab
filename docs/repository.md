# 仓库结构

## 分支

| 分支 | 内容 | 说明 |
| --- | --- | --- |
| `main` | 项目的当前代码和文档 | 对应 `ci/drv-integrated`（八个驱动分支合并后的状态）加上文档整理。推送到 `main` 不触发 CI |
| `ci/drv-integrated` | 八个驱动分支合并后的构建 | 平板上进入桌面用的就是这个分支的构建（见 [ci.md](ci.md)） |
| `ci/hw-sheng-cn-1tb` | 国行 16 GB / 1 TB 机型的硬件候选，到日志面板修复为止 | 推送到这里会触发完整构建 |
| `ci/drv-gpu`、`ci/drv-touch-display`、`ci/drv-login`、`ci/drv-keyboard`、`ci/drv-power-misc`、`ci/drv-wifi-bt`、`ci/drv-usb`、`ci/drv-adsp-battery` | 各驱动自己的分支 | 每个驱动单独编译，合并前互不影响 |
| `ci/gpu-adreno`、`ci/gpu-probe`、`ci/gpu-userland` | GPU 相关：Adreno 内核移植的第一版、QEMU 专用探针、用户态（Turnip、Rust 桥、Metal 插件） | `ci/gpu-userland` 有自己的工作流；它的产物不发布（见 [ci.md](ci.md)） |
| `ci/gop-copy-path`、`ci/gop-source-staging-stage15b`、`ci/stage18b-display-activity` | 早期显示实验的记录 | 只作历史保留 |
| `ci/apple-arm64e-link` | 本仓库最初的 ARM64E 链接实验，也是改版前 `main` 的内容 | 带自己的小型工作流，只作历史保留 |

分支名以 `ci/` 开头是因为工作流按这个前缀触发。任何不带这个前缀的分支都不会自动跑 CI。

## 标签

标签都是带说明的，指向已经通过 CI 的提交。每个标签的说明里写了对应的 CI 运行和平板上的结果。

| 标签 | 提交 | 含义 |
| --- | --- | --- |
| `link-lab-20261107` | `2dd4696` | 改版前的 `main`：最初的链接实验 |
| `milestone-recovery-keys-20261108` | `201bb2a` | Recovery 的显示和按键：CI 42388955103，平板上 2026-11-08 观察 |
| `milestone-touch-20261108` | `d38cb21` | 触摸可用：CI 42457862443 |
| `milestone-full-system-boot-20261109` | `bdfe089` | 完整系统第一次在平板上启动，到登录界面：CI 42503540405 |
| `milestone-desktop-20261109` | `52667ef` | 完整系统在平板上进入桌面：CI 42576253178 |
| `gpu-fake-a740-qemu-20261109` | `2a02df3` | QEMU 里假的 A740 上画出第一帧：CI 42565690803 |
| `gpu-userland-20261109` | `2abbb51` | 完整的 Metal 插件（含 Turnip 和 Rust 桥）：CI 42548818622，产物不发布 |

## 目录

| 目录或文件 | 内容 |
| --- | --- |
| `docs/` | 文档：[移植状态](status.md)、[技术路线](technical-route.md)、[CI 说明](ci.md)，以及按主题的参考文档；`docs/history/` 是已被取代的旧文档，保留作记录 |
| `platform/sheng/macos26/` | 内核一侧的代码，按功能分子目录：`storage`（存储）、`display`、`input`、`log`、`pac`、`recovery`（UI 策略页）、`gpu`、`net`、`usb`、`power`、`adsp`、`login`，以及对 XNU 的补丁 `patches/` |
| `patches/` | 对 UniXNU、UniXNU-sheng、uefi-sheng 等上游仓库的补丁 |
| `scripts/` | 构建、装配、检查、打包和观察工具 |
| `tests/` | 测试：主机上能跑的单元测试和字节级复核 |
| `config/` | 固定的输入、契约和各种表（来源提交、引脚、页表等） |
| `probes/`、`ci/` | 小型探针和固件构建用的描述文件 |
| `sources.lock.json` | 上游仓库的固定提交 |
| `LICENSE`、`LICENSES/`、`THIRD_PARTY_NOTICES.md`、`ACKNOWLEDGMENTS.md` | 许可证、第三方声明和致谢 |

## 仓库里没有的东西

- 苹果的系统镜像、IPSW、KDK、内核扩展、信任缓存、缓存文件，和任何苹果二进制；
- 厂商固件（触摸、Wi-Fi、ADSP、GPU 等）；
- 设备的备份、分区表、序列号和任何个人信息；
- 系统镜像的制作方法；
- 内核或驱动的二进制发布版。

## 仓库的历史

本仓库于 2026-11-07 重建，更早的构建记录不在这里。历史里包含上游 CrossDarwin/macOS-ForSheng 的提交，作者信息保持原样。
