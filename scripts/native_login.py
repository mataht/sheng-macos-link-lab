# SPDX-License-Identifier: GPL-3.0-or-later
"""Contract of the default-off login-path services (platform/sheng/macos26/login).

AppleKeyStore (sheng-aks=1 state answers, sheng-aks=2 also software key wrap) and IOWatchdog (sheng-watchdog=1).
Both are built into the storage bundle of either transport because, unlike the tablet-only companions, they are meant to
run in QEMU too: guest logs are readable there. The gate is the boot argument, not PE_vmm_present.
One definition shared by the storage builder (sources, personalities, evidence), the storage-module audit and the packager.
"""
import hashlib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PLATFORM_DIR = 'platform/sheng/macos26/login'
PLATFORM_SOURCES = ('native_login_services.cpp', 'aks_protocol.h')
AKS_ARGUMENT = 'sheng-aks'
WATCHDOG_ARGUMENT = 'sheng-watchdog'
AKS_STAGES = {'1': 'state answers only: no keybag, no passcode, nothing locked',
              '2': 'stage 1 plus the software key wrap of the upstream driver (keyed from the platform UUID, no protection)'}
PERSONALITIES = {
    'AppleKeyStore': dict(IOClass='AppleKeyStore', IOProviderClass='IOResources', IOResourceMatch='IOKit',
                          IOMatchCategory='AppleKeyStore', IOProbeScore=500),
    'IOWatchdog': dict(IOClass='IOWatchdog', IOProviderClass='IOResources', IOResourceMatch='IOKit',
                       IOMatchCategory='IOWatchdog', IOProbeScore=500),
}
ALLOWED_CLASSES = frozenset(PERSONALITIES)


def personalities(bundle: str) -> dict:
    return {name: dict(item, CFBundleIdentifier=bundle) for name, item in PERSONALITIES.items()}


def source_hashes(root: Path = ROOT) -> dict:
    return {f'{PLATFORM_DIR}/{name}': hashlib.sha256((root / PLATFORM_DIR / name).read_bytes()).hexdigest()
            for name in PLATFORM_SOURCES}


def evidence(root: Path = ROOT) -> dict:
    return dict(schema='sheng-login-v1', source_sha256=source_hashes(root), aks_argument=AKS_ARGUMENT,
                watchdog_argument=WATCHDOG_ARGUMENT, aks_stages=AKS_STAGES, default_off=True, runs_in_qemu=True,
                storage_writes=False, hardware_qualified=False, upstream='UniXNU/AppleKeyStoreState 58e5461ed3b751e4c91426b72d8686419abe9550')


def validate(record: dict, root: Path = ROOT) -> None:
    if (record.get('schema') != 'sheng-login-v1' or record.get('aks_argument') != AKS_ARGUMENT
            or record.get('watchdog_argument') != WATCHDOG_ARGUMENT or record.get('default_off') is not True
            or record.get('storage_writes') is not False or record.get('source_sha256') != source_hashes(root)):
        raise ValueError('login services differ from the reviewed default-off contract')


def validate_bundle(record: dict, info: dict, bundle: str) -> set:
    """Checks the compiled personalities against the contract and returns the IOClass names they add."""
    validate(record)
    for name, item in personalities(bundle).items():
        if info['IOKitPersonalities'].get(name) != item:
            raise ValueError(f'login service personality {name} differs')
    return set(ALLOWED_CLASSES)
