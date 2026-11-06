# Stage18B VM display-activity candidate

Stage18's existing language trace observed 42 system languages and 42 table rows, but the display was already idle-off before those objects were created. This candidate adds an explicit, default-off `native-gop-vm-keep-awake=1` policy guarded by the native VMM flag. It periodically sends the original ARM64 PM `requestUserActive` message from the common swap/timer path using an absolute 30-second deadline.

This is an unattended-VM observation workaround, not input emulation, GPU acceleration, a power-return override, or a proven fix for the earlier empty rows. A submitted void request is not proof that power management consumed it. The new runtime must show the display still on beyond the prior idle threshold and inspect the actual Language Chooser screen.

The NativeGOPDisplay class, 473-slot framebuffer ABI, scanout staging fix, swap_wait, queue completion and power-state implementation are unchanged. File-local singleton ownership is suspended before base stop and released afterward. A request already in flight is drained by the existing work-loop/base-stop lifecycle; atomic state alone is not a synchronous cancellation.

CI rebuilds an unchanged upstream control and this source variant using the same pinned Apple build environment. It does not access devices or start a VM. New object/link/PAC/layout admission and a separate bounded local VM run remain required. Existing stage15_* artifact keys are retained for script compatibility.
