# SPDX-License-Identifier: GPL-3.0-or-later
"""Contract of the default-off PSCI reboot and power-off hook (platform/sheng/macos26/patches/machine-platform-reset.patch).

Without PE_halt_restart XNU spins forever after a reboot or shutdown. sheng-psci-reset=1 (hardware only) sends those two requests
to PSCI through the SMC conduit that already starts the secondary CPUs. A panic is deliberately not handled, so its text and QR
code stay on the screen.
"""
import hashlib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
MACHINE_PATCH = 'platform/sheng/macos26/patches/machine-platform-reset.patch'
BOOT_ARGUMENT = 'sheng-psci-reset=1'


def patch_sha256(root: Path = ROOT) -> str:
    return hashlib.sha256((root / MACHINE_PATCH).read_bytes()).hexdigest()
