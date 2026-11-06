#!/usr/bin/env python3
"""Read a uniquely matched experiment's QMP status and pixels without stopping it."""
import argparse
import datetime
import hashlib
import json
import os
from pathlib import Path
import socket


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--fixture', type=Path, required=True)
    parser.add_argument('--name', required=True)
    args = parser.parse_args()
    fixture = args.fixture.resolve(strict=True)
    if not args.name.replace('-', '').replace('_', '').isalnum():
        parser.error('name must contain only letters, numbers, hyphens or underscores')
    target = fixture / (args.name + '.png')
    record_path = fixture / (args.name + '.json')
    if target.exists() or record_path.exists():
        parser.error('snapshot files must be new')
    matches = []
    for proc in Path('/proc').iterdir():
        if not proc.name.isdigit():
            continue
        try:
            if Path(os.readlink(proc / 'exe')).name != 'qemu-system-aarch64':
                continue
            argv = (proc / 'cmdline').read_bytes().decode().split('\0')
            if not any(str(fixture) in arg or str(args.fixture) in arg for arg in argv):
                continue
            value = argv[argv.index('-qmp') + 1]
            if not value.startswith('unix:'):
                continue
            matches.append((int(proc.name), value[5:].split(',')[0]))
        except (FileNotFoundError, PermissionError, ValueError, IndexError):
            continue
    if len(matches) != 1:
        raise RuntimeError(f'expected one matching experiment QEMU, found {len(matches)}')
    pid, endpoint = matches[0]
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
        client.settimeout(5)
        client.connect(endpoint)
        with client.makefile('rwb', buffering=0) as stream:
            greeting = json.loads(stream.readline())
            if 'QMP' not in greeting:
                raise RuntimeError('not a QMP endpoint')

            def request(command, arguments, identifier):
                client.sendall((json.dumps(dict(execute=command, arguments=arguments, id=identifier)) + '\n').encode())
                for _ in range(32):
                    response = json.loads(stream.readline())
                    if response.get('id') == identifier:
                        if 'error' in response:
                            raise RuntimeError(str(response['error']))
                        return response['return']
                raise RuntimeError('QMP response event bound exceeded')

            request('qmp_capabilities', {}, 1)
            before = request('query-status', {}, 2)
            request('screendump', dict(filename=str(target), format='png'), 3)
            after = request('query-status', {}, 4)
            blocks = request('query-block', {}, 5)
            block_stats = request('query-blockstats', {}, 6)
    data = target.read_bytes()
    if not data.startswith(b'\x89PNG\r\n\x1a\n'):
        raise RuntimeError('QMP output is not PNG')
    result = dict(timestamp_utc=datetime.datetime.now(datetime.timezone.utc).isoformat(),
                  pid=pid, qmp_version=greeting['QMP']['version'],
                  status_before=before, status_after=after, screenshot=str(target),
                  bytes=len(data), sha256=hashlib.sha256(data).hexdigest(),
                  block_devices=blocks, block_stats=block_stats,
                  helper_sha256=hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
                  guest_stop_or_resume_requested=False, guest_memory_written=False,
                  commands=['qmp_capabilities', 'query-status', 'screendump', 'query-status', 'query-block', 'query-blockstats'])
    record_path.write_text(json.dumps(result, indent=2) + '\n')
    print(json.dumps({key: value for key, value in result.items() if key not in ('block_devices', 'block_stats')}))


if __name__ == '__main__':
    main()
