#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""Follow the matched kernel's mountlist DWARF to a bounded read-only layout."""
import argparse
import hashlib
import json
from pathlib import Path
import re
import subprocess

from observe_macos26_kernel import identify


def dies(text):
    pattern = r'^0x([0-9a-f]+):( +)(DW_TAG_\w+|NULL)\n(.*?)(?=^0x|\Z)'
    return [dict(offset=int(m[1], 16), depth=len(m[2]), tag=m[3], text=m[0], attrs=m[4])
            for m in re.finditer(pattern, text, re.M | re.S)]


def number(die, attr):
    match = re.search(r'DW_AT_' + attr + r'\s+\((0x[0-9a-f]+|[0-9]+)\)', die['attrs'])
    if not match:
        raise ValueError(f"missing integer {attr} at DIE {die['offset']:#x}")
    return int(match[1], 0)


def typename(die):
    match = re.search(r'DW_AT_type\s+\((0x[0-9a-f]+) "([^"]*)"\)', die['attrs'])
    if not match:
        raise ValueError(f"missing referenced type at DIE {die['offset']:#x}")
    return int(match[1], 16), match[2]


class Dwarf:
    def __init__(self, tool, path):
        self.tool, self.path, self.cache = tool, path, {}
        self.evidence = {}

    def query(self, selector):
        if selector not in self.cache:
            text = subprocess.check_output([str(self.tool), selector, '--show-children', str(self.path)],
                                           text=True, timeout=30)
            self.cache[selector] = dies(text)
        return self.cache[selector]

    def at(self, offset):
        values = self.query(f'--debug-info={offset:#x}')
        if not values or values[0]['offset'] != offset:
            raise ValueError(f'no exact DWARF DIE at {offset:#x}')
        return values

    def member(self, values, name, label):
        matches = [d for d in values[1:] if d['tag'] == 'DW_TAG_member'
                   and d['depth'] == values[0]['depth'] + 2
                   and re.search(r'DW_AT_name\s+\("' + re.escape(name) + r'"\)', d['attrs'])]
        if len(matches) != 1:
            raise ValueError(f'no unique direct member {label}.{name}')
        die = matches[0]
        ref, spelling = typename(die)
        self.evidence[label + '.' + name] = dict(die=hex(die['offset']), type_die=hex(ref),
            type=spelling, offset=number(die, 'data_member_location'), dwarf=die['text'].rstrip())
        return die

    def structure(self, ref, name=None):
        values = self.at(ref)
        if values[0]['tag'] != 'DW_TAG_structure_type':
            raise ValueError(f'{ref:#x} is not a structure')
        if name and not re.search(r'DW_AT_name\s+\("' + re.escape(name) + r'"\)', values[0]['attrs']):
            raise ValueError(f'{ref:#x} is not {name}')
        self.evidence[name or hex(ref)] = dict(die=hex(ref), size=number(values[0], 'byte_size'),
                                               dwarf=values[0]['text'].rstrip())
        return values

    def pointer_target(self, ref):
        die = self.at(ref)[0]
        if die['tag'] != 'DW_TAG_pointer_type':
            raise ValueError(f'{ref:#x} is not a pointer')
        return typename(die)[0]

    def scalar_size(self, ref, depth=0):
        if depth > 8:
            raise ValueError('DWARF scalar chain too deep')
        die = self.at(ref)[0]
        if die['tag'] in ('DW_TAG_typedef', 'DW_TAG_const_type'):
            return self.scalar_size(typename(die)[0], depth + 1)
        if die['tag'] != 'DW_TAG_base_type':
            raise ValueError('not a scalar base type')
        return number(die, 'byte_size')

    def char_array_size(self, ref):
        values = self.at(ref)
        if values[0]['tag'] != 'DW_TAG_array_type' or typename(values[0])[1] != 'char':
            raise ValueError('requires an actual DWARF char array')
        if self.scalar_size(typename(values[0])[0]) != 1:
            raise ValueError('char size differs from one byte')
        ranges = [d for d in values[1:] if d['tag'] == 'DW_TAG_subrange_type']
        if len(ranges) != 1:
            raise ValueError('requires exactly one array dimension')
        count = number(ranges[0], 'count')
        self.evidence[hex(ref)] = dict(die=hex(ref), count=count,
            dwarf='\n'.join(d['text'].rstrip() for d in values if d['tag'] != 'NULL'))
        return count


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--kernel-build', type=Path, required=True)
    parser.add_argument('--dwarfdump', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    _, symbols, identity = identify(args.kernel_build)
    dwarf, = (args.kernel_build / 'kernel.development.sheng.dSYM/Contents/Resources/DWARF').iterdir()
    reader = Dwarf(args.dwarfdump, dwarf)
    variables = [d for d in reader.query('--name=mountlist') if d['tag'] == 'DW_TAG_variable'
                 and re.search(r'DW_AT_name\s+\("mountlist"\)', d['attrs'])]
    if len(variables) != 1:
        raise ValueError('requires exactly one global mountlist DWARF variable')
    var = variables[0]
    address = re.search(r'DW_AT_location\s+\(DW_OP_addr (0x[0-9a-f]+)\)', var['attrs'])
    if not address or int(address[1], 16) != symbols.get('_mountlist'):
        raise ValueError('mountlist DWARF location differs from linked kernel symbol')
    reader.evidence['mountlist'] = dict(die=hex(var['offset']), dwarf=var['text'].rstrip())
    head = reader.structure(typename(var)[0], 'mntlist')
    first = reader.member(head, 'tqh_first', 'mntlist')
    last = reader.member(head, 'tqh_last', 'mntlist')
    mount_ref = reader.pointer_target(typename(first)[0])
    if reader.pointer_target(reader.pointer_target(typename(last)[0])) != mount_ref:
        raise ValueError('head last is not pointer-to-pointer to matching mount')
    mount = reader.structure(mount_ref, 'mount')
    link = reader.member(mount, 'mnt_list', 'mount')
    entry = reader.structure(typename(link)[0])
    nxt = reader.member(entry, 'tqe_next', 'mnt_list')
    prev = reader.member(entry, 'tqe_prev', 'mnt_list')
    if (reader.pointer_target(typename(nxt)[0]) != mount_ref or
            reader.pointer_target(reader.pointer_target(typename(prev)[0])) != mount_ref):
        raise ValueError('TAILQ entry does not point to matching mount')
    stat_member = reader.member(mount, 'mnt_vfsstat', 'mount')
    stat = reader.structure(typename(stat_member)[0], 'vfsstatfs')
    size = number(mount[0], 'byte_size')
    stat_at = number(stat_member, 'data_member_location')
    offsets = dict(head_first=number(first, 'data_member_location'),
                   head_last=number(last, 'data_member_location'),
                   mount_next=number(link, 'data_member_location') + number(nxt, 'data_member_location'),
                   mount_prev=number(link, 'data_member_location') + number(prev, 'data_member_location'),
                   mount_vfsstat=stat_at)
    strings = {}
    for name in ('f_fstypename', 'f_mntonname', 'f_mntfromname'):
        field = reader.member(stat, name, 'vfsstatfs')
        strings[name] = dict(offset=stat_at + number(field, 'data_member_location'),
                             bytes=reader.char_array_size(typename(field)[0]))
    scalars = {}
    for group, fields, base in ((mount, ('mnt_flag', 'mnt_kern_flag', 'mnt_lflag'), 0),
                                (stat, ('f_flags', 'f_fssubtype'), stat_at)):
        for name in fields:
            field = reader.member(group, name, 'mount' if base == 0 else 'vfsstatfs')
            scalars[name] = dict(offset=base + number(field, 'data_member_location'),
                                bytes=reader.scalar_size(typename(field)[0]))
    if (number(head[0], 'byte_size') != 16 or offsets['head_first'] != 0 or offsets['head_last'] != 8
            or number(entry[0], 'byte_size') != 16 or number(nxt, 'data_member_location') != 0
            or number(prev, 'data_member_location') != 8 or not 16 <= size <= 4096
            or stat_at + number(stat[0], 'byte_size') > size
            or any(not 0 <= f['offset'] < f['offset'] + f['bytes'] <= size
                   for f in (*strings.values(), *scalars.values()))):
        raise ValueError('DWARF layout exceeds qualified 64-bit bounded TAILQ contract')
    result = dict(schema='native-mount-layout-v1', kernel_uuid=identity['uuid'],
        kernel_sha256=identity['kernel_sha256'], dwarf_sha256=hashlib.sha256(dwarf.read_bytes()).hexdigest(),
        mountlist=symbols['_mountlist'], kernel_va_bits=47, pointer_bytes=8,
        max_mounts=32, max_read_bytes=4096, head_bytes=16, mount_bytes=size,
        vfsstatfs_bytes=number(stat[0], 'byte_size'), offsets=offsets, strings=strings, scalars=scalars,
        source='matched kernel UUID, linked mountlist symbol and its exact referenced DWARF type chain',
        read_only=True, evidence=reader.evidence)
    args.output.write_text(json.dumps(result, indent=2) + '\n')
    print(json.dumps({k: v for k, v in result.items() if k != 'evidence'}, indent=2))


if __name__ == '__main__':
    main()
