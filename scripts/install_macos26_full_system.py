#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""Write the prepared macossystem partition from root sheng Recovery: chunked, verified, never retried.

Order: verify the whole local image against the chunk map, discard the partition, then for every 8 MiB
chunk either confirm it already reads back as zeros (all-zero chunks after discard) or stage it in /tmp
RAM, write it, and compare the device-side SHA-256 of the written range. The SHNGROOT header is written
last, so an interrupted run never leaves a valid manifest. Finally the whole window is hashed on the
device. The first mismatch stops the run; nothing is retried automatically.
"""
import argparse
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import re
import shlex
import struct
import subprocess
import tempfile
import time
import uuid

from create_log_partition import geometry
import repartition_sheng_cn_1tb as cn

PARTITION = '/dev/block/by-name/macossystem'
STAGING = re.compile(r'/tmp/sheng-macossystem-[A-Za-z0-9_-]{1,40}')
DD_BLOCK = 4096
DAEMON_ATTEMPTS, DAEMON_WAIT = 40, 5  # Windows runs out of loopback ports after thousands of short adb calls; they free up in minutes


def sha(data):
    return hashlib.sha256(data).hexdigest()


class AdbTransport:
    """Device side of the write; every method is one bounded shell or push operation."""

    def __init__(self, device, staging):
        if not STAGING.fullmatch(staging):
            raise ValueError('staging directory must be /tmp/sheng-macossystem-<safe-id>')
        self.device, self.staging = device, staging
        mounts = [line.split() for line in device.shell('cat /proc/mounts').splitlines()]
        if [m[2] for m in mounts if len(m) >= 3 and m[1] == '/tmp'] != ['tmpfs']:
            raise ValueError('staging requires /tmp on tmpfs')
        device.shell('mkdir -m 700 ' + shlex.quote(staging))
        self.daemon_retries = 0

    def call(self, arguments, timeout):
        """One adb call. Repeated only when the local client cannot reach its own daemon: then nothing left this PC."""
        for _ in range(DAEMON_ATTEMPTS):
            result = subprocess.run(self.device.adb + arguments, capture_output=True, text=True, errors='replace', timeout=timeout)
            if result.returncode == 0:
                return result.stdout.strip()
            if 'cannot connect to daemon' not in result.stderr:
                break
            self.daemon_retries += 1
            time.sleep(DAEMON_WAIT)
        raise ValueError(f'adb {arguments[0]} failed with exit {result.returncode}: {result.stdout[-300:]!r} {result.stderr[-300:]!r}')

    def discard(self):
        for tool in ('blkdiscard', 'toybox blkdiscard'):
            try:
                self.device.shell(tool + ' --help', timeout=10)
            except subprocess.CalledProcessError:
                continue
            self.device.shell(tool + ' ' + PARTITION, timeout=600)
            return tool
        raise ValueError('no usable blkdiscard tool')

    def stage(self, data, name):
        remote = f'{self.staging}/{name}'
        with tempfile.TemporaryDirectory() as folder:
            local = Path(folder) / name
            local.write_bytes(data)
            self.call(['push', str(local), remote], 300)
        if self.call(['shell', 'sha256sum ' + remote], 60).split() != [sha(data), remote]:
            raise ValueError('staged chunk differs from the local bytes')
        return remote

    def write(self, remote, offset, length):
        command = (f'dd if={remote} of={PARTITION} bs={DD_BLOCK} seek={offset // DD_BLOCK} '
                   f'count={length // DD_BLOCK} conv=notrunc,fsync 2>&1')
        self.call(['shell', command], 300)

    def digest(self, offset, length):
        output = self.call(['shell', f'dd if={PARTITION} bs={DD_BLOCK} skip={offset // DD_BLOCK} '
                                     f'count={length // DD_BLOCK} 2>/dev/null | sha256sum'], 3600)
        return output.split()[0]

    def remove(self, remote):
        self.call(['shell', 'rm -f ' + shlex.quote(remote)], 30)


def verify_image(image, chunk_map):
    """Read-only pass before anything is discarded: the image must equal the prepared chunk map."""
    with Path(image).open('rb') as stream:
        for chunk in chunk_map['chunks']:
            data = stream.read(chunk['bytes'])
            if len(data) != chunk['bytes'] or sha(data) != chunk['sha256']:
                raise ValueError(f'image differs from the chunk map at offset {chunk["offset"]}')
        if stream.read(1):
            raise ValueError('image is longer than the chunk map')


def verified_prefix(journal_path, start=0):
    """How many leading chunks an interrupted run verified on the device; any gap or mismatch record stops the resume."""
    count = start
    with Path(journal_path).open() as journal:
        for line in journal:
            entry = json.loads(line)
            if entry.get('operation') == 'mismatch':
                raise ValueError('the interrupted run recorded a readback mismatch; do not resume it')
            if entry.get('operation') in ('verified', 'zero-verified') and 'chunk' in entry:
                if entry['chunk'] != count:
                    raise ValueError('the interrupted journal is not a contiguous prefix')
                count += 1
    return count


def write_partition(transport, image, chunk_map, header, window_sha256, journal, resume_from=0):
    window = chunk_map['window_offset']
    if window % DD_BLOCK or len(header) != DD_BLOCK or any(c['bytes'] % DD_BLOCK for c in chunk_map['chunks']):
        raise ValueError('partition layout is not aligned to the device write block')

    def log(**entry):
        journal.write(json.dumps(entry) + '\n')
        journal.flush()

    counts = dict(written=0, zero_verified=0, zero_written=0)
    if resume_from:
        # The earlier run discarded the partition and verified these chunks; the whole-window digest below re-checks them.
        log(operation='resume', first_chunk=resume_from)
    else:
        log(operation='discard', tool=transport.discard())
    zeros = {}
    with Path(image).open('rb') as stream:
        for index, chunk in enumerate(chunk_map['chunks']):
            if index < resume_from:
                stream.seek(chunk['bytes'], 1)
                continue
            data = stream.read(chunk['bytes'])
            if sha(data) != chunk['sha256']:
                raise ValueError(f'image changed during the write at chunk {index}')
            target = window + chunk['offset']
            if chunk['zero'] and transport.digest(target, chunk['bytes']) == chunk['sha256']:
                counts['zero_verified'] += 1
                log(operation='zero-verified', chunk=index)
                continue
            if chunk['zero']:
                if chunk['bytes'] not in zeros:
                    zeros[chunk['bytes']] = transport.stage(data, f'zero-{chunk["bytes"]}.bin')
                remote = zeros[chunk['bytes']]
            else:
                remote = transport.stage(data, 'chunk.bin')
            log(operation='starting', chunk=index, offset=target, bytes=chunk['bytes'])
            transport.write(remote, target, chunk['bytes'])
            if not chunk['zero']:
                transport.remove(remote)
            actual = transport.digest(target, chunk['bytes'])
            if actual != chunk['sha256']:
                log(operation='mismatch', chunk=index, expected=chunk['sha256'], actual=actual)
                raise ValueError(f'readback differs at chunk {index}; stopped without retry')
            counts['zero_written' if chunk['zero'] else 'written'] += 1
            log(operation='verified', chunk=index)
    for remote in zeros.values():
        transport.remove(remote)
    remote = transport.stage(header, 'manifest.bin')
    log(operation='starting', kind='manifest')
    transport.write(remote, 0, DD_BLOCK)
    transport.remove(remote)
    if transport.digest(0, DD_BLOCK) != sha(header):
        log(operation='mismatch', kind='manifest')
        raise ValueError('manifest readback differs; stopped without retry')
    log(operation='verified', kind='manifest')
    total = sum(c['bytes'] for c in chunk_map['chunks'])
    if transport.digest(window, total) != window_sha256:
        log(operation='mismatch', kind='window')
        raise ValueError('whole-window readback digest differs')
    log(operation='verified', kind='window', bytes=total)
    return counts


def qualify(device, plan_dir, package_dir, image):
    plan = json.loads((Path(plan_dir) / 'plan.json').read_text())
    build = json.loads((Path(package_dir) / 'build.json').read_text())
    header = (Path(package_dir) / 'manifest.bin').read_bytes()
    chunks_bytes = (Path(package_dir) / 'chunks.json').read_bytes()
    chunk_map = json.loads(chunks_bytes)
    status = Path(image).stat()
    if (plan.get('applied') is not True or plan.get('gpt_readback_verified') is not True
            or plan.get('serial') != device.serial or plan.get('full_name') != 'macossystem'
            or build.get('schema') != 'sheng-full-root-package-v1' or build.get('partition') != 'macossystem'
            or plan['full_bytes'] < build['partition_bytes_minimum']
            or sha(header) != build['manifest_sha256'] or header[:8] != b'SHNGROOT'
            or hashlib.sha256(header[:4064]).digest() != header[4064:]
            or sha(chunks_bytes) != build['chunks_sha256']
            or (status.st_size, status.st_mtime_ns) != (build['image']['bytes'], build['image']['mtime_ns'])
            or build['window']['bytes'] != status.st_size):
        raise ValueError('requires the applied macossystem plan and the package prepared from this image')
    device.require_recovery()
    for kind, start in (('primary', 0), ('secondary', device.lbas - cn.ALIGN)):
        current = device.read(start, cn.ALIGN)
        if current != (Path(plan_dir) / f'after-{kind}.bin').read_bytes():
            raise ValueError('LUN0 layout changed since the applied macossystem plan')
        _, _, entries, _, _, stride = geometry(current, device.lbas, start)
        entry = entries[(plan['full_index'] - 1) * stride:plan['full_index'] * stride]
        if (entry[16:32] != uuid.UUID(plan['full_unique_guid']).bytes_le
                or struct.unpack_from('<QQ', entry, 32) != (plan['full_first_lba'], plan['full_last_lba'])):
            raise ValueError('macossystem identity or extent differs from the plan')
    if (device.shell('readlink -f ' + PARTITION) != '/dev/block/sda' + str(plan['full_index'])
            or int(device.shell('blockdev --getsize64 ' + PARTITION)) != plan['full_bytes']
            or device.shell(f'ls -A /sys/class/block/sda{plan["full_index"]}/holders')):
        raise ValueError('kernel macossystem node differs from the verified GPT')
    return plan, build, header, chunk_map


def install(device, plan_dir, package_dir, image, output, staging, resume=None):
    plan, build, header, chunk_map = qualify(device, plan_dir, package_dir, image)
    verify_image(image, chunk_map)
    resume_from = 0
    if resume is not None:
        previous = json.loads((Path(resume) / 'install.json').read_text())
        if (previous.get('verified') is not False or previous.get('destination_guid') != plan['full_unique_guid']
                or previous.get('manifest_sha256') != build['manifest_sha256'] or previous.get('window') != build['window']
                or previous.get('serial') != device.serial):
            raise ValueError('the interrupted install does not belong to this partition and package')
        resume_from = verified_prefix(Path(resume) / 'install.jsonl', previous.get('first_chunk') or 0)
        if not 0 < resume_from < len(chunk_map['chunks']):
            raise ValueError('nothing to resume')
    output = Path(output)
    output.mkdir(parents=True, exist_ok=False, mode=0o700)
    record = dict(serial=device.serial, started_at=datetime.now(timezone.utc).isoformat(), destination=PARTITION,
                  destination_guid=plan['full_unique_guid'], manifest_sha256=build['manifest_sha256'],
                  window=build['window'], chunk_count=len(chunk_map['chunks']), staging=staging, verified=False,
                  resumed_from=str(Path(resume).resolve()) if resume is not None else None, first_chunk=resume_from)
    (output / 'install.json').write_text(json.dumps(record, indent=2) + '\n')
    transport = AdbTransport(device, staging)
    with (output / 'install.jsonl').open('x') as journal:
        counts = write_partition(transport, image, chunk_map, header, build['window']['sha256'], journal,
                                 resume_from=resume_from)
    record.update(counts, verified=True, daemon_retries=transport.daemon_retries, finished_at=datetime.now(timezone.utc).isoformat())
    (output / 'install.json').write_text(json.dumps(record, indent=2) + '\n')
    return record


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--serial', required=True)
    parser.add_argument('--plan', required=True, type=Path, help='applied repartition_macos26_full.py directory')
    parser.add_argument('--package', required=True, type=Path, help='prepare_macos26_full_root.py output')
    parser.add_argument('--image', required=True, type=Path, help='the delivered raw disk the package describes')
    parser.add_argument('--output', required=True, type=Path)
    parser.add_argument('--staging', required=True, help='/tmp/sheng-macossystem-<safe-id> on the device')
    parser.add_argument('--resume', type=Path, help='output directory of an interrupted install; continues after its verified chunks')
    args = parser.parse_args()
    if not args.output.resolve().is_relative_to((cn.ROOT / 'private').resolve()):
        parser.error('--output must stay in local private/')
    print(json.dumps(install(cn.Device(args.serial), args.plan, args.package, args.image, args.output,
                             args.staging, resume=args.resume), indent=2))


if __name__ == '__main__':
    main()
