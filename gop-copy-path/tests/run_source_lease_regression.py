#!/usr/bin/env python3
"""Host-only source/lease regression; no device, VM, or kernel mutation."""
import argparse
import hashlib
import json
from pathlib import Path
import platform
import shutil
import subprocess

OVERLAY = Path(__file__).resolve().parents[1]


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--compiler', help='local host-check override; CI uses Apple clang++')
    args = parser.parse_args()
    compiler = args.compiler or shutil.which('clang++')
    if not compiler:
        raise RuntimeError('host compiler not found')
    args.output.mkdir(parents=True, exist_ok=False)
    expected = json.loads((OVERLAY / 'expected.json').read_text())['overlay']
    inputs = ['tests/source_lease_regression.cpp', 'tests/run_source_lease_regression.py',
              'display/gop_scanout.h', 'original/stage15-gop_scanout.h']
    for relative in inputs:
        if sha(OVERLAY / relative) != expected[relative]:
            raise ValueError('regression input SHA mismatch: ' + relative)
    source = OVERLAY / 'tests/source_lease_regression.cpp'
    results = []
    for name, header, expectation in [('original', 'original/stage15-gop_scanout.h', 'expect-source-fault'),
                                      ('proposed', 'display/gop_scanout.h', 'expect-pass')]:
        binary = args.output / ('source-lease-' + name)
        command = [compiler, '-std=c++17', '-O2', '-Wall', '-Wextra', '-Werror',
                   '-DGOP_HEADER="' + str(OVERLAY / header) + '"', str(source), '-o', str(binary)]
        subprocess.run(command, check=True, timeout=120)
        result = subprocess.run([str(binary.resolve()), expectation], capture_output=True, text=True, timeout=30)
        (args.output / (name + '.stdout.log')).write_text(result.stdout)
        (args.output / (name + '.stderr.log')).write_text(result.stderr)
        result.check_returncode()
        results.append(dict(variant=name, command=command, expectation=expectation,
                            exit_code=result.returncode, stdout=result.stdout, header_sha256=sha(OVERLAY / header)))
    (args.output / 'result.json').write_text(json.dumps(dict(host_os=platform.system(), tests=results,
        passed=True, cross_page_row=True, qemu_executed=False, runtime_tested=False,
        scope='mprotect/fork access-ordering regression, not an XNU or MMIO simulation'), indent=2) + '\n')
    print('source lease regression passed: original protection fault, proposed full-row staging')


if __name__ == '__main__':
    main()
