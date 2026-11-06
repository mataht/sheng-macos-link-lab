# SPDX-License-Identifier: GPL-3.0-or-later
"""Bounded WindowServer cache reads through its own native page tables.

Only QEMU debugger memory mode changes; no guest memory/register writes or resume.
Saved user PCs are latest boundaries, not proof of a current wait or execution.
"""
import struct
import uuid

FIXED_NATIVE_GEOMETRY = dict(
    schema='sheng-single-native-16k-geometry-v1',
    kernel_sha256='7539890938ce3d93bda65246251932db0e2d240cfea8de58fe4339ad450d0aca',
    attribute_address=0xfffffe00071c4fe0,
    attributes=dict(root=1, max=3, shift=14, size=16384, tcr=0, va_mask=(1 << 47) - 1),
    pmap_user_va_bits_address=0xfffffe00075a71b0,
    pmap_user_va_bits_bytes='5f2403d5e0058052c0035fd6',
    tcr_field_usage='unused zero initializer when __ARM_MIXED_PAGE_SIZE__ is disabled')


def geometry(attr, registers, *, fixed_contract=None, attribute_address=None):
    tcr = int(registers['TCR_EL1'], 16)
    mmfr = int(registers['ID_AA64MMFR0_EL1'], 16)
    if not int(registers['SCTLR_EL1'], 16) & 1:
        raise ValueError('MMU is not enabled')
    relevant = 63 | (3 << 14) | (7 << 32) | (1 << 59)
    if fixed_contract is not None:
        if (fixed_contract != FIXED_NATIVE_GEOMETRY or attribute_address != fixed_contract['attribute_address']
                or attr != fixed_contract['attributes']):
            raise ValueError('native attributes differ from the explicit fixed-build contract')
    elif attr['tcr'] & relevant != tcr & relevant:
        raise ValueError('native attributes differ from actual CPU translation geometry')
    if (tcr >> 14) & 3 != 2 or tcr & (1 << 59):
        raise ValueError('only observed native 16K, non-LPA2 stage-1 translation is qualified')
    pa_widths = {0: 32, 1: 36, 2: 40, 3: 42, 4: 44, 5: 48}
    pa_bits = pa_widths.get(tcr >> 32 & 7)
    cpu_pa_bits = pa_widths.get(mmfr & 15)
    if pa_bits is None or cpu_pa_bits is None or pa_bits > cpu_pa_bits:
        raise ValueError('unsupported or contradictory physical address width')
    va_bits = 64 - (tcr & 63)
    if attr['va_mask'] != (1 << va_bits) - 1:
        raise ValueError('actual CPU VA width differs from the target native attribute mask')
    root, shift = attr['root'], attr['shift']
    if (shift != 14 or attr['size'] != 1 << shift or attr['max'] != 3 or
            root not in (0, 1, 2) or not 1 <= va_bits - (14 + 11 * (3 - root)) <= 11):
        raise ValueError('unsupported native task root geometry')
    return dict(page_shift=shift, page_bytes=1 << shift, root_level=root,
                va_bits=va_bits, pa_bits=pa_bits,
                root_bits=va_bits - (14 + 11 * (3 - root)))


def translate(physical, va, root, geo, *, executable=False):
    """Read ARM stage-1 descriptors; return a leaf and every actual table read."""
    if not 0 < va < 1 << geo['va_bits']:
        raise ValueError('address is outside the target user VA range')
    root_alignment = 8 << geo['root_bits']
    if root & (root_alignment - 1):
        raise ValueError('target root table is misaligned')
    mask = ((1 << geo['pa_bits']) - 1) & ~((1 << geo['page_shift']) - 1)
    table, path = root, []
    for level in range(geo['root_level'], 4):
        shift = 14 + 11 * (3 - level)
        index_mask = (1 << (geo['root_bits'] if level == geo['root_level'] else 11)) - 1
        site = table + ((va >> shift) & index_mask) * 8
        raw = physical(site, 8)
        entry = int.from_bytes(raw, 'little')
        path.append(dict(level=level, table=hex(table), descriptor_address=hex(site),
                         descriptor=hex(entry), raw=raw.hex()))
        if not entry & 1:
            raise ValueError('target user page is not resident')
        if entry & (((1 << 48) - 1) ^ ((1 << geo['pa_bits']) - 1)):
            raise ValueError('descriptor address exceeds configured physical width')
        base = entry & mask
        if level < 3 and entry & 3 == 3:
            if entry & (1 << 61) or executable and entry & (1 << 60):
                raise ValueError('table permissions prohibit the requested EL0 access')
            table = base
            continue
        if (level == 0 or level == 3 and entry & 3 != 3 or
                level < 3 and entry & 3 != 1):
            raise ValueError('invalid block/page descriptor at this level')
        if not entry & (1 << 10) or not entry & (1 << 6):
            raise ValueError('leaf lacks access flag or EL0 permission')
        if executable and entry & (1 << 54):
            raise ValueError('leaf prohibits EL0 instruction execution')
        if base & ((1 << shift) - 1):
            raise ValueError('leaf output address is misaligned')
        return dict(physical_address=base | (va & ((1 << shift) - 1)),
                    remaining=(1 << shift) - (va & ((1 << shift) - 1)), path=path)
    raise ValueError('no target page-table leaf')


def read_user(physical, va, count, root, geo, *, executable=False):
    if not 0 < count <= 512 or va + count > 1 << geo['va_bits']:
        raise ValueError('unbounded user read')
    data, pages = bytearray(), []
    while len(data) < count:
        part = translate(physical, va + len(data), root, geo, executable=executable)
        size = min(count - len(data), part.pop('remaining'))
        raw = physical(part['physical_address'], size)
        pages.append(dict(virtual_address=hex(va + len(data)), bytes=size,
                          physical_address=hex(part['physical_address']), path=part['path']))
        data.extend(raw)
    return bytes(data), pages


def capture(packet, processes, plan, registers, context):
    record = dict(schema='native-user-cache-snapshot-v1', status='unknown', complete=False,
        read_only=True, guest_registers_or_memory_modified=False, process_name='WindowServer',
        user_address_space='target-task-pmap physical walk; never current-task virtual m',
        pc_limit=4, pc_bytes=16, debugger_mode_restored=None,
        pac_authentication_performed=False,
        boundary='saved PCs are latest EL0 boundaries, not proof of the current wait or execution')
    switched = False
    try:
        if (plan['schema'] != 'native-user-cache-plan-v1' or plan['read_only'] is not True or
                plan['layout']['qualified_geometry'] != 'global_native_pt_attr'):
            raise ValueError('unqualified user cache plan')
        contract = plan.get('geometry_contract')
        if contract != FIXED_NATIVE_GEOMETRY or plan['kernel_sha256'] != contract['kernel_sha256']:
            raise ValueError('requires the explicit paired fixed-build geometry contract')
        old_mode = packet('qqemu.PhyMemMode')
        if old_mode != '0':
            raise ValueError('requires the explicitly observed virtual debugger mode')
        initial = bytes.fromhex(packet('g'))
        if len(initial) < 268 or bytes.fromhex(packet('g')) != initial:
            raise ValueError('incomplete or inconsistent stopped register snapshot')
        pc = struct.unpack_from('<Q', initial, 256)[0]
        pstate = struct.unpack_from('<I', initial, 264)[0]
        if pstate & 15 not in (4, 5) or pc != int(context['pc'], 16) or pstate != int(context['pstate'], 16):
            raise ValueError('requires the same actual stopped EL1 context')
        record['context'] = dict(pc=hex(pc), pstate=hex(pstate))
        selected = [p for p in processes if p['name'] == 'WindowServer']
        if len(selected) != 1:
            raise ValueError('exactly one observed WindowServer process is required')
        proc = selected[0]
        record['process'] = proc
        layout = plan['layout']
        fields, threads, offsets = layout['fields'], plan['process']['threads'], plan['process']['offsets']
        bits = layout['va_bits']
        if bits != 47:
            raise ValueError('kernel pointer layout differs from the paired native candidate')
        floor, mask = (1 << 64) - (1 << bits), (1 << bits) - 1
        kernel_reads, physical_reads = [], []
        record.update(kernel_metadata_reads=kernel_reads, physical_reads=physical_reads)
        total_kernel, total_physical = 0, 0

        def read(address, size):
            nonlocal total_kernel
            total_kernel += size
            if (not floor <= address <= (1 << 64) - size or not 0 < size <= 304 or
                    total_kernel > 4096 or len(kernel_reads) >= 160):
                raise ValueError('bounded kernel metadata read exceeded')
            raw = packet(f'm{address:x},{size:x}')
            if len(raw) != size * 2 or any(c not in '0123456789abcdefABCDEF' for c in raw):
                raise ValueError('kernel metadata is unavailable, not zero')
            data = bytes.fromhex(raw)
            kernel_reads.append(dict(address=hex(address), bytes=size, raw=data.hex()))
            return data

        def word(address, size=8):
            return int.from_bytes(read(address, size), 'little')

        def canonical(value):
            if not value:
                raise ValueError('null kernel object pointer')
            return (value & mask) | floor

        def pointer(address):
            result = canonical(word(address))
            if result & 7:
                raise ValueError('unaligned kernel object pointer')
            return result

        def field(base, name):
            f = fields[name]
            if not 0 <= f['offset'] < 65536 or f['bytes'] not in (1, 4, 8, 16):
                raise ValueError('unqualified DWARF field extent')
            return word(base + f['offset'], f['bytes'])

        address = int(proc['address'], 16)
        if word(address + offsets['pid'], 4) != proc['pid'] or read(address + offsets['comm'], 17).split(b'\0')[0] != b'WindowServer':
            raise ValueError('process PID/name changed')

        def identity():
            ro = pointer(address + threads['proc_ro'])
            if pointer(ro + threads['proc_owner']) != address:
                raise ValueError('proc read-only owner mismatch')
            task = pointer(ro + threads['proc_task'])
            vmmap = canonical(field(task, 'task_map'))
            pmap = canonical(field(vmmap, 'map_pmap'))
            region = canonical(field(task, 'task_shared_region'))
            attr = pointer(layout['symbols']['native_pt_attr']['address'])
            return dict(ro=ro, task=task, vmmap=vmmap, pmap=pmap, region=region,
                        attr=attr, ttep=field(pmap, 'pmap_ttep'))

        ident = identity()
        record['identity'] = {k: hex(v) for k, v in ident.items()}
        attr = {name: field(ident['attr'], 'attr_' + name) for name in ('root', 'max', 'shift', 'size', 'tcr', 'va_mask')}
        record['observed_attributes'] = attr
        if read(contract['pmap_user_va_bits_address'], 12).hex() != contract['pmap_user_va_bits_bytes']:
            raise ValueError('actual compiled user VA-width accessor differs from the fixed-build contract')
        geo = geometry(attr, registers, fixed_contract=contract, attribute_address=ident['attr'])
        record['geometry'] = dict(**geo, observed_attributes=attr,
                                  tcr_field_usage=contract['tcr_field_usage'])
        if attr['va_mask'] != (1 << geo['va_bits']) - 1:
            raise ValueError('native VA mask does not match the actual translation width')
        level_base = canonical(field(ident['attr'], 'attr_level_info'))
        level_layout = layout['level_info']
        if level_layout['stride'] != 56:
            raise ValueError('unqualified native level-info layout')
        levels = []
        for level in range(geo['root_level'], 4):
            raw = read(level_base + level * 56, 56)
            actual = {name: int.from_bytes(raw[f['offset']:f['offset'] + f['bytes']], 'little')
                      for name, f in level_layout['fields'].items()}
            shift = 14 + 11 * (3 - level)
            expected = dict(size=1 << shift, offmask=(1 << shift) - 1, shift=shift,
                index_mask=2047 << shift, valid_mask=3 if level == 3 else 1,
                type_mask=2, type_block=2 if level == 3 else 0)
            if actual != expected:
                raise ValueError('actual level descriptors do not match the qualified stage-1 geometry')
            levels.append(actual)
        record['geometry']['actual_level_info'] = levels
        if field(ident['pmap'], 'pmap_is_64bit') != 1:
            raise ValueError('target pmap is not a 64-bit user map')
        lower, upper = field(ident['pmap'], 'pmap_min'), field(ident['pmap'], 'pmap_max')
        if not 0 <= lower < upper <= 1 << geo['va_bits']:
            raise ValueError('invalid target pmap virtual bounds')
        physical_base = word(layout['symbols']['gPhysBase']['address'])
        physical_size = word(layout['symbols']['gPhysSize']['address'])
        if not 0 < physical_base < physical_base + physical_size <= 1 << geo['pa_bits']:
            raise ValueError('invalid actual kernel physical RAM bounds')
        record['physical_bounds'] = dict(base=hex(physical_base), bytes=physical_size,
                                        source='actual gPhysBase/gPhysSize; MMIO excluded')
        region = ident['region']
        region_data = {name: field(region, 'region_' + name)
                       for name in ('slide', 'first_mapping', 'base', 'size', 'uuid', 'uuid_copied', 'driverkit')}
        slide = region_data['slide']
        header_va = region_data['base'] + region_data['first_mapping']
        if (region_data['driverkit'] or region_data['uuid_copied'] not in (0, 1) or
                slide > plan['cache']['max_slide'] or slide & (geo['page_bytes'] - 1) or not lower <= region_data['base'] < upper or
                not 0 <= region_data['first_mapping'] < region_data['size'] or
                region_data['base'] + region_data['size'] > upper or header_va + 104 > upper):
            raise ValueError('invalid native macOS shared-region mapping')
        record['shared_region'] = dict(slide=hex(slide), header_address=hex(header_va),
            base=hex(region_data['base']), size=region_data['size'], first_mapping=hex(region_data['first_mapping']),
            copied_uuid=str(uuid.UUID(bytes=region_data['uuid'].to_bytes(16, 'little'))),
            uuid_copied=bool(region_data['uuid_copied']))
        head = ident['task'] + threads['task_threads']
        node, seen, saved = pointer(head), set(), []
        while node != head and node not in seen and len(saved) < 4:
            seen.add(node)
            tro = pointer(node + threads['thread_ro'])
            if pointer(tro + threads['thread_owner']) != node or pointer(tro + threads['thread_task']) != ident['task']:
                raise ValueError('target thread owner/task mismatch')
            item = dict(thread=hex(node), thread_id=word(node + threads['thread_id']), status='unknown')
            saved.append(item)
            upcb_raw = word(node + threads['upcb'])
            if upcb_raw:
                raw = read(canonical(upcb_raw), 304)
                item['saved_state_raw'] = raw.hex()
                if struct.unpack_from('<II', raw) == (21, 74) and struct.unpack_from('<I', raw, 272)[0] & 31 == 0:
                    user_pc = struct.unpack_from('<Q', raw, 264)[0]
                    item.update(pc=hex(user_pc), cpsr=hex(struct.unpack_from('<I', raw, 272)[0]))
            node = pointer(node + threads['thread_link'])
        record['thread_list_truncated'] = node != head
        record['instructions'] = saved
        switched = True
        if packet('Qqemu.PhyMemMode:1') != 'OK' or packet('qqemu.PhyMemMode') != '1':
            raise ValueError('physical debugger mode was not enabled')

        def physical(address, size):
            nonlocal total_physical
            total_physical += size
            if (not physical_base <= address <= physical_base + physical_size - size or
                    not 0 < size <= 512 or total_physical > 2048 or len(physical_reads) >= 96):
                raise ValueError('physical read is outside actual RAM or the fixed budget')
            raw = packet(f'm{address:x},{size:x}')
            if len(raw) != size * 2 or any(c not in '0123456789abcdefABCDEF' for c in raw):
                raise ValueError('physical page is unavailable, not zero')
            data = bytes.fromhex(raw)
            physical_reads.append(dict(address=hex(address), bytes=size, raw=data.hex()))
            return data

        try:
            header, header_pages = read_user(physical, header_va, 104, ident['ttep'], geo)
            runtime_uuid = str(uuid.UUID(bytes=header[88:104]))
            record['cache_header'] = dict(raw=header.hex(), uuid=runtime_uuid, translations=header_pages)
            if header[:16] != bytes.fromhex(plan['cache']['magic_hex']) or runtime_uuid != plan['cache']['uuid']:
                raise ValueError('actual cache magic/UUID does not match the pinned local macOS cache')
            if region_data['uuid_copied'] and runtime_uuid != record['shared_region']['copied_uuid']:
                raise ValueError('shared-region copied UUID differs from the actual mapped cache')
            if header_va - slide != plan['cache']['unslid_header_address']:
                raise ValueError('actual first mapping and slide disagree with the fixed cache mapping')
            record['cache_identity_verified'] = True
            for item in saved:
                try:
                    if 'pc' not in item:
                        raise ValueError('no qualified saved EL0 boundary for this thread')
                    user_pc = int(item['pc'], 16)
                    if user_pc & 3 or not lower <= user_pc < upper - 15:
                        raise ValueError('invalid target user instruction address')
                    unslid = user_pc - slide
                    matching = [r for r in plan['cache']['executable_ranges'] if r[0] <= unslid < r[1] - 15]
                    if len(matching) != 1:
                        raise ValueError('saved PC is outside the pinned executable cache ranges')
                    data, pages = read_user(physical, user_pc, 16, ident['ttep'], geo, executable=True)
                    item.update(status='captured', unslid_pc=hex(unslid), bytes=data.hex(), translations=pages,
                                local_file_bytes_compared=False, function_symbolized=False)
                except ValueError as error:
                    item['error'] = str(error)
        finally:
            if packet('Qqemu.PhyMemMode:0') != 'OK' or packet('qqemu.PhyMemMode') != '0':
                raise RuntimeError('cannot restore debugger virtual mode; snapshot is unverified')
            switched = False
            record['debugger_mode_restored'] = True
        if identity() != ident or {name: field(region, 'region_' + name) for name in region_data} != region_data:
            raise ValueError('target identity, table root, or shared-region fields changed')
        if bytes.fromhex(packet('g')) != initial:
            raise ValueError('stopped register context changed during the read-only snapshot')
        record.update(status='captured', complete=True, target_identity_rechecked=True,
                      kernel_metadata_reads=kernel_reads, physical_reads=physical_reads)
    except ValueError as error:
        record.update(status='unknown', complete=False, error=str(error))
    finally:
        if switched:
            if packet('Qqemu.PhyMemMode:0') != 'OK' or packet('qqemu.PhyMemMode') != '0':
                raise RuntimeError('cannot restore debugger virtual mode; snapshot is unverified')
            record['debugger_mode_restored'] = True
    return record
