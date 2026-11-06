#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""Bounded mountlist observation. Caller must already have stopped the VM in EL1.

capture(packet, plan) sends only GDB remote `m` packets. It does not stop a CPU,
read registers, authenticate pointers, acquire locks, or infer APFS volume roles.
"""
import re


def capture(packet, plan):
    def integer(value, lower, upper):
        return type(value) is int and lower <= value <= upper

    if (plan.get('schema') != 'native-mount-layout-v1' or plan.get('kernel_va_bits') != 47
            or plan.get('pointer_bytes') != 8 or plan.get('head_bytes') != 16
            or not integer(plan.get('mount_bytes'), 16, 4096)
            or not integer(plan.get('max_mounts'), 1, 32)
            or plan.get('max_read_bytes') != 4096 or plan.get('read_only') is not True):
        raise ValueError('unqualified bounded mount layout')
    offsets, strings, scalars = plan['offsets'], plan['strings'], plan['scalars']
    size = plan['mount_bytes']
    if offsets.get('head_first') != 0 or offsets.get('head_last') != 8:
        raise ValueError('unqualified TAILQ head')
    for name in ('mount_next', 'mount_prev'):
        if not integer(offsets.get(name), 0, size - 8) or offsets[name] % 8:
            raise ValueError('unqualified TAILQ member')
    if set(strings) != {'f_fstypename', 'f_mntonname', 'f_mntfromname'}:
        raise ValueError('missing required mount strings')
    if set(scalars) != {'mnt_flag', 'mnt_kern_flag', 'mnt_lflag', 'f_flags', 'f_fssubtype'}:
        raise ValueError('unexpected mount scalar layout')
    for name, field in (*strings.items(), *scalars.items()):
        if (not integer(field.get('offset'), 0, size - 1)
                or not integer(field.get('bytes'), 1, min(1024, size - field['offset']))
                or (name in scalars and field['bytes'] not in (4, 8))):
            raise ValueError('unqualified field bounds: ' + name)
    mask, base, limit = (1 << 47) - 1, (1 << 64) - (1 << 47), 1 << 64
    head_address = plan['mountlist']
    if not integer(head_address, base, limit - 16) or head_address % 8:
        raise ValueError('unqualified canonical mountlist address')
    result = dict(kernel_uuid=plan.get('kernel_uuid'), kernel_sha256=plan.get('kernel_sha256'),
        mountlist=hex(head_address), mounts=[], errors=[], read_only=True,
        stopped_el1_precondition='caller responsibility; not verified by this memory-only reader',
        pointer_metadata_stripped_for_lookup_only=True, pac_authentication_performed=False,
        traversal_complete=False, list_links_consistent=True, head_stable=False, complete=False,
        max_mounts=plan['max_mounts'], max_read_bytes=4096, read_requests=0, read_bytes_requested=0,
        interpretation='observed kernel mountlist entries and cached vfsstatfs fields; no volume-role inference')

    class ReadError(Exception):
        pass

    def read(address, count):
        if not integer(address, base, limit - count) or not integer(count, 1, 4096):
            raise ValueError(f'noncanonical or unbounded kernel read: {address:#x}, {count}')
        request = f'm{address:x},{count:x}'
        result['read_requests'] += 1
        result['read_bytes_requested'] += count
        try:
            response = packet(request)
        except Exception as exc:
            result['errors'].append(dict(request=request, exception_type=type(exc).__name__, error=str(exc)))
            raise ReadError('packet exception') from exc
        if not isinstance(response, str) or not re.fullmatch(r'[0-9a-fA-F]{' + str(2 * count) + r'}', response):
            result['errors'].append(dict(request=request, response=response, error='invalid memory response'))
            raise ReadError('invalid memory response')
        return bytes.fromhex(response)

    def pointer(raw):
        return ((raw & mask) | base) if raw else 0

    def u64(data, at):
        return int.from_bytes(data[at:at + 8], 'little')

    def string(data, field):
        value = data[field['offset']:field['offset'] + field['bytes']]
        prefix, nul, _ = value.partition(b'\0')
        return dict(text=prefix.decode('utf-8', errors='backslashreplace'), raw_hex=prefix.hex(),
                    nul_terminated=bool(nul), capacity=field['bytes'])

    initial = None
    try:
        initial = read(head_address, 16)
        node_raw = u64(initial, offsets['head_first'])
        last_raw = u64(initial, offsets['head_last'])
        result['head_first_raw'], result['head_last_raw'] = hex(node_raw), hex(last_raw)
        result['head_first'], result['head_last'] = hex(pointer(node_raw)), hex(pointer(last_raw))
        seen, expected_prev = set(), head_address + offsets['head_first']
        while node_raw:
            node = pointer(node_raw)
            if node % 8:
                result['termination'] = 'unaligned_mount_pointer'
                result['errors'].append(dict(pointer_raw=hex(node_raw), pointer=hex(node), error='unaligned mount'))
                break
            if node in seen:
                result['termination'] = 'repeated_mount_pointer'
                result['errors'].append(dict(pointer=hex(node), error='TAILQ cycle or repeated entry'))
                break
            if len(seen) >= plan['max_mounts']:
                result['termination'] = 'mount_limit'
                result['next_unread_raw'] = hex(node_raw)
                break
            data = read(node, size)
            seen.add(node)
            next_raw, prev_raw = u64(data, offsets['mount_next']), u64(data, offsets['mount_prev'])
            prev_matches = pointer(prev_raw) == expected_prev
            row = dict(address_raw=hex(node_raw), address=hex(node), next_raw=hex(next_raw),
                next=hex(pointer(next_raw)), prev_raw=hex(prev_raw), prev=hex(pointer(prev_raw)),
                expected_prev=hex(expected_prev), prev_link_matches=prev_matches,
                strings={name: string(data, field) for name, field in strings.items()},
                scalars={name: int.from_bytes(data[field['offset']:field['offset'] + field['bytes']], 'little')
                         for name, field in scalars.items()})
            result['mounts'].append(row)
            if not prev_matches:
                result['list_links_consistent'] = False
                result['termination'] = 'prev_link_mismatch'
                break
            expected_prev = node + offsets['mount_next']
            node_raw = next_raw
        else:
            result['traversal_complete'] = True
            result['termination'] = 'null_next'
            if pointer(last_raw) != expected_prev:
                result['list_links_consistent'] = False
                result['errors'].append(dict(error='TAILQ last pointer mismatch',
                    expected=hex(expected_prev), observed=hex(pointer(last_raw)), raw=hex(last_raw)))
        final = read(head_address, 16)
        result['head_stable'] = final == initial
        if final != initial:
            result['errors'].append(dict(error='mountlist head changed during snapshot',
                                        initial_hex=initial.hex(), final_hex=final.hex()))
    except (ReadError, ValueError) as exc:
        result['termination'] = 'read_error'
        if not isinstance(exc, ReadError):
            result['errors'].append(dict(exception_type=type(exc).__name__, error=str(exc)))
    result['all_strings_nul_terminated'] = all(field['nul_terminated'] for row in result['mounts']
                                               for field in row['strings'].values())
    result['complete'] = (result['traversal_complete'] and result['list_links_consistent']
                          and result['head_stable'] and result['all_strings_nul_terminated']
                          and not result['errors'])
    return result
