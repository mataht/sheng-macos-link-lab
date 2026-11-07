#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""Plan or apply the CN 1 TB sheng LUN0 layout: 128 GiB userdata, macoslog, 32 GiB macosroot, free tail.

One GPT update on sda (LUN0) only, in root Recovery:
  userdata (entry 29) shrinks to 128 GiB; macoslog becomes entry 30 at the extent and partition UUID compiled into
  the storage driver (config/macoslog.json, layout sheng-cn-1tb-lun0); macosroot becomes entry 31 (32 GiB); the rest
  of LUN0 stays unallocated for a later system container. Every other GPT entry byte stays identical.
Planning never writes. Applying discards Android userdata contents (the user rebuilds the filesystem afterwards).
"""
import argparse
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import shlex
import struct
import subprocess
import uuid

from backup_device import parse_gpt
from create_log_partition import geometry, update
from native_log_file import LAYOUT

ROOT = Path(__file__).resolve().parents[1]
BLOCK = 4096
WINDOW = 1 << 20
ALIGN = WINDOW // BLOCK
BASIC_DATA = uuid.UUID('ebd0a0a2-b9e5-4433-87c0-68b6b72699c7')
LUN0_LBAS = 257745920          # observed 2026-11-06 (sysfs and GPT), CN 1 TB unit
USERDATA_BYTES = 128 << 30     # user decision 2026-11-06
ROOT_BYTES = 32 << 30          # user decision 2026-11-06: recovery only, not the system install target
USERDATA_INDEX, LOG_INDEX, ROOT_INDEX = 28, LAYOUT['partition_index'] - 1, LAYOUT['partition_index']
LOG_BLOCKS = LAYOUT['partition_blocks']
PRESERVED = ROOT / 'private/backups/sheng-cn-1tb-lun0-layout'


def sha(data):
    return hashlib.sha256(data).hexdigest()


def up(lba):
    return (lba + ALIGN - 1) // ALIGN * ALIGN


def entry_bytes(name, first, last, unique, stride):
    entry = bytearray(stride)
    entry[:16], entry[16:32] = BASIC_DATA.bytes_le, unique.bytes_le
    struct.pack_into('<QQQ', entry, 32, first, last, 0)
    encoded = name.encode('utf-16-le')
    entry[56:56 + len(encoded)] = encoded
    return bytes(entry)


def layout(primary, secondary, lbas, root_guid=None):
    if LAYOUT['name'] != 'sheng-cn-1tb-lun0' or LAYOUT['disk'] != 'sda' or lbas != LUN0_LBAS or lbas != LAYOUT['disk_blocks']:
        raise ValueError('requires the reviewed CN 1 TB LUN0 log layout and capacity')
    before, header, entries, _, count, stride = geometry(primary, lbas, 0)
    other = geometry(secondary, lbas, lbas - ALIGN)
    if (entries != other[2] or header[56:72] != other[1][56:72] or before['partitions'] != other[0]['partitions']
            or count != LAYOUT['entry_count'] or stride != 128):
        raise ValueError('primary and secondary GPT copies disagree or have an unexpected entry array')
    slot = lambda i: entries[i * stride:(i + 1) * stride]
    names = [p['name'] for p in before['partitions']]
    if 'macoslog' in names or 'macosroot' in names or names.count('userdata') != 1:
        raise ValueError('requires exactly one userdata and no existing macoslog or macosroot')
    userdata = slot(USERDATA_INDEX)
    if userdata[56:128].decode('utf-16-le').split('\0')[0] != 'userdata':
        raise ValueError('userdata is not GPT entry 29 (sda29)')
    first, last = struct.unpack_from('<QQ', userdata, 32)
    if last != before['last_usable'] or any(p['last_lba'] >= first for p in before['partitions'] if p['name'] != 'userdata'):
        raise ValueError('userdata must be the final partition reaching the last usable block')
    if slot(LOG_INDEX) != bytes(stride) or slot(ROOT_INDEX) != bytes(stride):
        raise ValueError('GPT entries 30 and 31 must be completely empty')
    userdata_last = first + USERDATA_BYTES // BLOCK - 1
    log_first = up(userdata_last + 1)
    log_last = log_first + LOG_BLOCKS - 1
    root_first = up(log_last + 1)
    root_last = root_first + ROOT_BYTES // BLOCK - 1
    if (log_first, log_last) != (LAYOUT['partition_first'], LAYOUT['partition_last']):
        raise ValueError('computed macoslog extent differs from the compiled driver layout')
    if root_last >= before['last_usable']:
        raise ValueError('LUN0 is too small for the planned layout')
    shrunk = bytearray(userdata)
    struct.pack_into('<Q', shrunk, 40, userdata_last)
    log = entry_bytes('macoslog', log_first, log_last, uuid.UUID(LAYOUT['partition_uuid']), stride)
    root_guid = root_guid or uuid.uuid4()
    rootfs = entry_bytes('macosroot', root_first, root_last, root_guid, stride)
    outputs = {}
    for name, raw, start in (('primary', primary, 0), ('secondary', secondary, lbas - ALIGN)):
        changed = update(update(update(raw, lbas, start, USERDATA_INDEX, bytes(shrunk)), lbas, start, LOG_INDEX, log),
                         lbas, start, ROOT_INDEX, rootfs)
        after = geometry(changed, lbas, start)
        for i in range(count):
            expected = {USERDATA_INDEX: bytes(shrunk), LOG_INDEX: log, ROOT_INDEX: rootfs}.get(i, slot(i))
            if after[2][i * stride:(i + 1) * stride] != expected:
                raise ValueError('unexpected GPT entry change')
        if after[1][24:56] != (header if start == 0 else other[1])[24:56] or after[1][56:72] != header[56:72]:
            raise ValueError('GPT header geometry or disk identity changed')
        outputs[name] = changed
    return outputs, dict(layout='sheng-cn-1tb-lun0', disk='sda', disk_lbas=lbas, logical_block_bytes=BLOCK,
        disk_guid=str(uuid.UUID(bytes_le=header[56:72])), entry_count=count,
        userdata=dict(index=USERDATA_INDEX + 1, first_lba=first, last_lba=userdata_last, bytes=USERDATA_BYTES,
                      original_last_lba=last),
        macoslog=dict(index=LOG_INDEX + 1, first_lba=log_first, last_lba=log_last, unique_guid=LAYOUT['partition_uuid'],
                      bytes=LOG_BLOCKS * BLOCK),
        macosroot=dict(index=ROOT_INDEX + 1, first_lba=root_first, last_lba=root_last, unique_guid=str(root_guid),
                       bytes=ROOT_BYTES),
        unallocated=dict(first_lba=root_last + 1, last_lba=before['last_usable'],
                         bytes=(before['last_usable'] - root_last) * BLOCK),
        # Flat fields read by install_macos26_ufs_root.py and package_macos26_ufs.py (upstream plan format).
        userdata_bytes=USERDATA_BYTES, root_index=ROOT_INDEX + 1, root_name='macosroot',
        root_unique_guid=str(root_guid), root_first_lba=root_first, root_last_lba=root_last, root_bytes=ROOT_BYTES,
        other_partitions_changed=False, userdata_contents_discarded=False, userdata_filesystem_rebuilt=False,
        root_image_written=False, applied=False)


class Device:
    def __init__(self, serial):
        # The serial only selects the ADB device. prepare() binds the plan to this exact tablet by requiring its
        # live LUN0 GPT to match the private backup baseline, and apply() requires the plan's serial.
        if not serial:
            raise ValueError('select the sheng ADB serial explicitly')
        self.serial, self.adb = serial, ['adb', '-s', serial]
        self.require_recovery()
        if int(self.shell('cat /sys/block/sda/queue/logical_block_size')) != BLOCK:
            raise ValueError('unexpected UFS LUN0 block size')
        self.lbas = int(self.shell('cat /sys/block/sda/size')) * 512 // BLOCK
        if self.lbas != LUN0_LBAS:
            raise ValueError('LUN0 capacity differs from the recorded CN 1 TB sheng')

    def shell(self, command, *, timeout=30):
        return subprocess.check_output(self.adb + ['shell', command], text=True, timeout=timeout).strip()

    def require_recovery(self):
        if (subprocess.check_output(self.adb + ['get-state'], text=True, timeout=10).strip() != 'recovery'
                or self.shell('id -u') != '0' or self.shell('getprop ro.product.device') != 'sheng'):
            raise ValueError('requires root ADB in the selected sheng Recovery')
        mounts = self.shell('cat /proc/mounts')
        if any(line.split()[1] in ('/data', '/metadata') or '/dev/block/sda' in line for line in mounts.splitlines()):
            raise ValueError('userdata, metadata and LUN0 partitions must be unmounted')
        if self.shell('ls -A /sys/class/block/sda29/holders'):
            raise ValueError('userdata still has an active device-mapper holder')

    def read(self, start, count):
        data = subprocess.check_output(self.adb + ['exec-out', 'sh', '-c',
            f'dd if=/dev/block/sda bs={BLOCK} skip={start} count={count} 2>/dev/null'], timeout=300)
        if len(data) != count * BLOCK:
            raise ValueError('incomplete UFS read')
        return data

    def write(self, path, start, count):
        if path.stat().st_size != count * BLOCK:
            raise ValueError('unexpected write size')
        remote = '/tmp/sheng-cn-' + path.name
        subprocess.run(self.adb + ['push', str(path), remote], check=True, timeout=300, stdout=subprocess.DEVNULL)
        try:
            self.shell(f'dd if={shlex.quote(remote)} of=/dev/block/sda bs={BLOCK} seek={start} count={count} conv=notrunc && sync',
                       timeout=300)
            if self.read(start, count) != path.read_bytes():
                raise ValueError('write readback differs; keep Recovery and rollback files')
        finally:
            self.shell('rm -f ' + shlex.quote(remote))


def validate_fat16(data):
    """Check the bounded empty-volume format used for the dedicated log partition."""
    if len(data) != LOG_BLOCKS * BLOCK:
        raise ValueError('macoslog image size differs from the reviewed extent')
    sector, cluster, reserved, fats, entries, sectors, _, fat_blocks = struct.unpack_from('<HBHBHHBH', data, 11)
    if (data[510:512] != b'\x55\xaa' or data[54:62] != b'FAT16   ' or data[43:54] != b'MACOSLOG   '
            or sector != BLOCK or cluster != 1 or fats != 2 or sectors != LOG_BLOCKS
            or data[21] != 0xf8
            or not 0 < reserved < LOG_BLOCKS or not 0 < fat_blocks <= 32
            or not 0 < entries <= 1024 or entries % (BLOCK // 32)
            or struct.unpack_from('<I', data, 32)[0] != 0):
        raise ValueError('macoslog image is not the expected 4 KiB-sector FAT16 volume')
    root_first = reserved + fats * fat_blocks
    data_first = root_first + entries * 32 // BLOCK
    clusters = (sectors - data_first) // cluster
    if not 4085 <= clusters < 65525 or (clusters + 2) * 2 > fat_blocks * BLOCK:
        raise ValueError('macoslog FAT16 data or allocation-table extent is invalid')
    first = data[reserved * BLOCK:(reserved + fat_blocks) * BLOCK]
    second = data[(reserved + fat_blocks) * BLOCK:(reserved + 2 * fat_blocks) * BLOCK]
    if first[:4] != b'\xf8\xff\xff\xff' or first != second:
        raise ValueError('macoslog FAT16 allocation-table copies differ')


def make_fat16(image):
    """Format the macoslog image. ADB reaches the tablet only from Windows, where dosfstools runs in WSL."""
    command = ['mkfs.fat', '-F', '16', '-S', str(BLOCK), '-s', '1', '-n', 'MACOSLOG']
    if os.name == 'nt':
        # wslpath prints UTF-8; the Windows console code page (e.g. GBK) cannot decode non-ASCII paths.
        linux = subprocess.check_output(['wsl.exe', '-e', 'wslpath', '-a', str(image.resolve())]).decode('utf-8').strip()
        # wsl.exe -e does not search /usr/sbin, where dosfstools installs mkfs.fat.
        command = ['wsl.exe', '-e', '/usr/sbin/mkfs.fat', *command[1:], linux]
    else:
        command.append(str(image))
    subprocess.run(command, check=True, capture_output=True)
    validate_fat16(image.read_bytes())


def validate_prepared(directory, record=None):
    """Read-only host preflight; reconstruct every planned extent before any journal or device write.

    A valid GPT CRC is not proof of the intended layout. The log write uses plan.json directly, so its
    destination must also equal the reviewed layout even when both after-GPT files remain unchanged.
    This validates local prepared artifacts only; apply() still independently binds the live device.
    """
    directory = Path(directory)
    if record is None:
        record = json.loads((directory / 'plan.json').read_text())
    if record.get('disk_lbas') != LUN0_LBAS:
        raise ValueError('prepared plan capacity differs from the reviewed CN 1 TB device')
    before = {name: (directory / f'before-{name}.bin').read_bytes() for name in ('primary', 'secondary')}
    for name, data in before.items():
        if len(data) != WINDOW or sha(data) != record.get('before', {}).get(name):
            raise ValueError('prepared before-GPT identity differs: ' + name)
    try:
        root_guid = uuid.UUID(record['root_unique_guid'])
    except (KeyError, TypeError, ValueError, AttributeError) as error:
        raise ValueError('prepared macosroot identity is invalid') from error
    if root_guid.int == 0:
        raise ValueError('prepared macosroot identity must be nonzero')
    expected, layout_record = layout(before['primary'], before['secondary'], LUN0_LBAS, root_guid)
    for key, value in layout_record.items():
        # Bind JSON types too: an equal-valued float is not an integer dd seek/count argument.
        if json.dumps(record.get(key), sort_keys=True) != json.dumps(value, sort_keys=True):
            raise ValueError('prepared plan differs from the recomputed layout: ' + key)
    after_hashes = {}
    for name, data in expected.items():
        actual = (directory / f'after-{name}.bin').read_bytes()
        if actual != data:
            raise ValueError('prepared after-GPT differs from the recomputed layout: ' + name)
        after_hashes[name] = sha(actual)
    image = (directory / 'macoslog-fat16.img').read_bytes()
    validate_fat16(image)
    digest = sha(image)
    if digest != record.get('macoslog_filesystem_image_sha256'):
        raise ValueError('prepared macoslog image SHA-256 differs from the plan')
    return dict(validated=True, device_checked=False, device_writes=False, layout=layout_record['layout'],
                after_sha256=after_hashes, macoslog_filesystem_image_sha256=digest,
                macoslog=layout_record['macoslog'], macosroot=layout_record['macosroot'],
                userdata=layout_record['userdata'], unallocated=layout_record['unallocated'])


def prepare(device, directory, baseline):
    """Read-only: compare against a backup_device.py baseline, then save before/after copies and the plan."""
    manifest = json.loads((baseline / 'manifest.json').read_text())['gpt']['sda']
    primary = device.read(0, ALIGN)
    secondary = device.read(device.lbas - ALIGN, ALIGN)
    if manifest['device_lbas'] != device.lbas or any(
            sha(data) != manifest['copies'][kind]['sha256'] for kind, data in (('primary', primary), ('secondary', secondary))):
        raise ValueError('current LUN0 GPT differs from the verified backup; back up again before planning')
    result, record = layout(primary, secondary, device.lbas)
    directory.mkdir(parents=True, exist_ok=False, mode=0o700)
    for name, data in (('before-primary.bin', primary), ('before-secondary.bin', secondary),
                       ('after-primary.bin', result['primary']), ('after-secondary.bin', result['secondary'])):
        (directory / name).write_bytes(data)
    image = directory / 'macoslog-fat16.img'
    with image.open('xb') as stream:
        stream.truncate(LOG_BLOCKS * BLOCK)
    make_fat16(image)
    record.update(serial=device.serial, baseline=str(baseline), prepared_at=datetime.now(timezone.utc).isoformat(),
                  before=dict(primary=sha(primary), secondary=sha(secondary)),
                  macoslog_filesystem_image_sha256=sha(image.read_bytes()))
    (directory / 'plan.json').write_text(json.dumps(record, indent=2) + '\n')
    return record


def apply(device, directory):
    record = json.loads((directory / 'plan.json').read_text())
    if record['applied'] or record['serial'] != device.serial or record['disk_lbas'] != device.lbas:
        raise ValueError('stale or already applied destructive plan')
    device.require_recovery()
    validate_prepared(directory, record)
    for name, start in (('primary', 0), ('secondary', device.lbas - ALIGN)):
        raw = device.read(start, ALIGN)
        if sha(raw) != record['before'][name] or raw != (directory / f'before-{name}.bin').read_bytes():
            raise ValueError('LUN0 changed after preparation')
        parse_gpt((directory / f'after-{name}.bin').read_bytes(), BLOCK, device.lbas, start)
    if PRESERVED.exists():
        raise ValueError('a preserved layout already exists; inspect it before applying again')
    journal = directory / 'apply.jsonl'
    if journal.exists():
        raise ValueError('previous write attempt exists; inspect it before retrying')
    log = record['macoslog']
    with journal.open('x') as entries:
        def step(kind, path, at, count):
            entries.write(json.dumps(dict(operation='starting', kind=kind, lba=at, count=count)) + '\n'); entries.flush()
            device.write(path, at, count)
            entries.write(json.dumps(dict(operation='verified', kind=kind)) + '\n'); entries.flush()
        for name, window in (('secondary', device.lbas - ALIGN), ('primary', 0)):
            data = (directory / f'after-{name}.bin').read_bytes()
            _, header, array, start, _, _ = geometry(data, device.lbas, window)
            blocks = (len(array) + BLOCK - 1) // BLOCK
            payload = data[(start - window) * BLOCK:(start - window + blocks) * BLOCK]
            path = directory / f'write-{name}-array.bin'; path.write_bytes(payload)
            step(f'{name}-array', path, start, blocks)
            path = directory / f'write-{name}-header.bin'; path.write_bytes(header)
            step(f'{name}-header', path, 1 if name == 'primary' else device.lbas - 1, 1)
        step('macoslog-fat16', directory / 'macoslog-fat16.img', log['first_lba'], log['last_lba'] - log['first_lba'] + 1)
    PRESERVED.mkdir(parents=True, mode=0o700)
    for name, start in (('primary', 0), ('secondary', device.lbas - ALIGN)):
        data = device.read(start, ALIGN)
        if data != (directory / f'after-{name}.bin').read_bytes():
            raise ValueError('complete GPT readback differs')
        (directory / f'verified-{name}.bin').write_bytes(data)
        (PRESERVED / f'verified-{name}.bin').write_bytes(data)
    record.update(applied=True, gpt_readback_verified=True, recovery_reboot_required=True,
                  preserved_layout=str(PRESERVED))
    (directory / 'plan.json').write_text(json.dumps(record, indent=2) + '\n')
    return record


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--serial', required=True)
    parser.add_argument('--output', required=True, type=Path)
    parser.add_argument('--baseline', type=Path, help='backup_device.py output taken in this Recovery session')
    parser.add_argument('--apply-discard-userdata', action='store_true')
    args = parser.parse_args()
    if not args.output.resolve().is_relative_to((ROOT / 'private').resolve()):
        parser.error('--output must stay in local private/')
    device = Device(args.serial)
    if args.apply_discard_userdata:
        result = apply(device, args.output)
    elif args.baseline:
        result = prepare(device, args.output, args.baseline)
    else:
        parser.error('planning requires --baseline')
    print(json.dumps(result, indent=2))


if __name__ == '__main__':
    main()
