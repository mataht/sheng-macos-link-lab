#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""Generate the class declaration a subclass of macOS 26.5's AppleUSBXHCI compiles against.

Output (no Apple code and no Apple method names):
  platform/sheng/macos26/usb/OriginalUsbXhciABI.h   the class with opaque method names and private storage
  config/macos26-usb-host-slots.json                each slot's opaque mangled name and pointer-authentication discriminator

The discriminator of a virtual method is a SipHash-2-4 of its mangled name reduced to 16 bits, so a name is searched for each slot
until it gives the discriminator the original vtable carries; a vtable built from these declarations is signed slot for slot like
the original's. UniXNU-sheng's scripts/gen-usb27-host-abi.py does the same for iPadOS. The KC is local-only:
  gen_macos26_usb_abi.py extract  --bootkc <kernelcache.decoded> --facts <private.json>
  gen_macos26_usb_abi.py generate --facts <private.json>
"""
import argparse
import hashlib
import json
from pathlib import Path
import struct

ROOT = Path(__file__).resolve().parents[1]
HEADER = ROOT / 'platform/sheng/macos26/usb/OriginalUsbXhciABI.h'
CONTRACT = ROOT / 'config/macos26-usb-host-slots.json'
CLASS = 'AppleUSBXHCI'
IOSERVICE_SLOTS, XHCI_SLOTS, INSTANCE_SIZE = 272, 420, 0xf20  # the instance size is the immediate of the MetaClass constructor call
# Slots the subclass overrides: macro used in the driver, return type, parameter declarations, Itanium encoding of the parameters.
SPECIAL = {292: ('XHCI_DEFAULT_POWER', 'uint32_t', '', 'v'),
           412: ('XHCI_READ64', 'uint64_t', 'unsigned long long,unsigned int', 'yj'),
           414: ('XHCI_WRITE64', 'void', 'unsigned long long,unsigned int,unsigned long long', 'yjy')}
# The IOService virtuals the original classes override, with what the SDK declares. Parameters are spelled so overrides match.
IOSERVICE = {'free': ('void', ''), 'init': ('bool', 'OSDictionary *'), 'setProperties': ('IOReturn', 'OSObject *'),
             'didTerminate': ('bool', 'IOService *,IOOptionBits,bool *'), 'systemWillShutdown': ('void', 'IOOptionBits'),
             'registerService': ('void', 'IOOptionBits'), 'start': ('bool', 'IOService *'), 'stop': ('void', 'IOService *'),
             'open': ('bool', 'IOService *,IOOptionBits,void *'), 'close': ('void', 'IOService *,IOOptionBits'),
             'handleOpen': ('bool', 'IOService *,IOOptionBits,void *'), 'handleClose': ('void', 'IOService *,IOOptionBits'),
             'handleIsOpen': ('bool', 'const IOService *'), 'terminate': ('bool', 'IOOptionBits'),
             'matchPropertyTable': ('bool', 'OSDictionary *,SInt32 *'), 'attach': ('bool', 'IOService *'),
             'getWorkLoop': ('IOWorkLoop *', ''), 'message': ('IOReturn', 'UInt32,IOService *,void *'), 'PMstop': ('void', ''),
             'setPowerState': ('IOReturn', 'unsigned long,IOService *'), 'maxCapabilityForDomainState': ('unsigned long', 'IOPMPowerFlags'),
             'initialPowerStateForDomainState': ('unsigned long', 'IOPMPowerFlags'),
             'powerStateWillChangeTo': ('IOReturn', 'IOPMPowerFlags,unsigned long,IOService *'),
             'powerStateDidChangeTo': ('IOReturn', 'IOPMPowerFlags,unsigned long,IOService *')}
CONST_METHODS = {'handleIsOpen', 'getWorkLoop'}
MAX_TRIES = 4_000_000


def discriminator(name: str) -> int:
    """SipHash-2-4 of the mangled name (no leading underscore), reduced to the 16-bit nonzero discriminator."""
    mask = (1 << 64) - 1

    def rotl(x, b):
        return ((x << b) | (x >> (64 - b))) & mask

    key = bytes([0xb5, 0xd4, 0xc9, 0xeb, 0x79, 0x10, 0x4a, 0x79, 0x6f, 0xec, 0x8b, 0x1b, 0x42, 0x87, 0x81, 0xd4])
    k0, k1 = struct.unpack('<2Q', key)
    v = [0x736f6d6570736575 ^ k0, 0x646f72616e646f6d ^ k1, 0x6c7967656e657261 ^ k0, 0x7465646279746573 ^ k1]

    def rounds(n):
        for _ in range(n):
            v[0] = (v[0] + v[1]) & mask; v[1] = rotl(v[1], 13) ^ v[0]; v[0] = rotl(v[0], 32)  # noqa: E702
            v[2] = (v[2] + v[3]) & mask; v[3] = rotl(v[3], 16) ^ v[2]  # noqa: E702
            v[0] = (v[0] + v[3]) & mask; v[3] = rotl(v[3], 21) ^ v[0]  # noqa: E702
            v[2] = (v[2] + v[1]) & mask; v[1] = rotl(v[1], 17) ^ v[2]; v[2] = rotl(v[2], 32)  # noqa: E702
    data = name.encode()
    full = len(data) & ~7
    for i in range(0, full, 8):
        m = struct.unpack_from('<Q', data, i)[0]
        v[3] ^= m
        rounds(2)
        v[0] ^= m
    b = len(data) << 56 & mask
    for i, c in enumerate(data[full:]):
        b |= c << (8 * i)
    v[3] ^= b
    rounds(2)
    v[0] ^= b
    v[2] ^= 0xff
    rounds(4)
    return (v[0] ^ v[1] ^ v[2] ^ v[3]) % 0xffff + 1


def extract(args):
    from inspect_macos26_security import chains, macho as kc_macho
    data = args.bootkc.read_bytes()
    kc = kc_macho(data)
    symbols = {}
    for _, offset in kc['entries'].values():
        module = kc_macho(data, offset)
        command = next((p for c, p, size in module['commands'] if c == 2 and size == 24), None)
        if command is None:
            continue
        off, count, strings, size = struct.unpack_from('<4I', data, command + 8)
        for index in range(count):
            nm, kind, _, _, address = struct.unpack_from('<IBBHQ', data, off + index * 16)
            if kind & 0xe0 or kind & 0xe != 0xe:
                continue
            end = data.find(b'\0', strings + nm, strings + size)
            symbols.setdefault(address, []).append(data[strings + nm:end].decode())
    by_name = {n: a for a, names in symbols.items() for n in names}
    pointers = {p['site']: p for p in chains(data, kc)}

    def table(cls):
        site = by_name[f'__ZTV{len(cls)}{cls}'] + 16
        out = []
        while (p := pointers.get(site + 8 * len(out))) is not None:
            out.append(p)
        return out

    io, xhci = table('IOService'), table(CLASS)
    if (len(io), len(xhci)) != (IOSERVICE_SLOTS, XHCI_SLOTS):
        raise ValueError(f'unexpected vtable sizes {len(io), len(xhci)}')
    overrides = []
    for i in range(IOSERVICE_SLOTS):
        if xhci[i]['target'] == io[i]['target'] or i in (0, 1, 7):
            continue  # destructors and getMetaClass come from the structor macros
        names = [n for n in symbols.get(xhci[i]['target'], []) if n.startswith('__Z')]
        # The method's own name: the last length-prefixed component of the mangled class-qualified name.
        method = None
        for candidate in names:
            body = candidate[candidate.index('N') + 1:]
            if body.startswith('K'):
                body = body[1:]
            parts = []
            while body and body[0].isdigit():
                digits = len(body) - len(body.lstrip('0123456789'))
                length = int(body[:digits])
                parts.append(body[digits:digits + length])
                body = body[digits + length:]
            if len(parts) >= 2 and parts[-1] in IOSERVICE:
                method = parts[-1]
                break
        if method is None:
            continue  # overridden by a class outside the two whose names are declared; left to the audit
        overrides.append(dict(slot=i, method=method))
    facts = dict(build='25F71', diversity=[p['diversity'] for p in xhci], overrides=overrides,
                 bootkc_sha256=hashlib.sha256(data).hexdigest())
    module = kc_macho(data, kc['entries']['com.apple.driver.usb.AppleUSBXHCI'][1])
    text = next(s for s in module['segments'] if s['name'] == '__TEXT_EXEC')
    facts['identity'] = dict(uuid=module['uuid'], text_sha256=hashlib.sha256(data[text['fileoff']:text['fileoff'] + text['filesize']]).hexdigest())
    args.facts.parent.mkdir(parents=True, exist_ok=True)
    args.facts.write_text(json.dumps(facts, indent=0))
    print('facts:', args.facts, len(facts['diversity']), 'slots', len(overrides), 'IOService overrides')


def opaque_names(diversity):
    prefix = f'_ZN{len(CLASS)}{CLASS}'
    methods = []
    for slot in range(IOSERVICE_SLOTS, XHCI_SLOTS):
        want = diversity[slot]
        suffix = SPECIAL[slot][3] if slot in SPECIAL else 'v'
        for n in range(MAX_TRIES):
            name = f'opaque{slot}_{n}'
            mangled = f'{prefix}{len(name)}{name}E{suffix}'
            if discriminator(mangled) == want:
                methods.append(dict(slot=slot, name=name, mangled=mangled, discriminator=want))
                break
        else:
            raise ValueError(f'no opaque name for slot {slot}')
    return methods


def render(methods, overrides):
    out = ['// Generated by scripts/gen_macos26_usb_abi.py from the paired macOS 26.5 (25F71) kernel collection: declarations only, opaque\n',
           '// private storage and placeholder method names chosen so each slot carries the pointer-authentication discriminator of the original.\n',
           '#pragma once\n#include <IOKit/IOService.h>\n']
    by_slot = {m['slot']: m for m in methods}
    for slot, (macro, _, _, _) in sorted(SPECIAL.items()):
        out.append(f'#define {macro} {by_slot[slot]["name"]}\n')
    out.append(f'class {CLASS} : public IOService {{\n OSDeclareAbstractStructors({CLASS})\n')
    out.append(f' unsigned char originalPrivate[{INSTANCE_SIZE:#x}-sizeof(IOService)];\npublic:\n')
    for item in sorted(overrides, key=lambda o: o['slot']):
        method = item['method']
        returns, parameters = IOSERVICE[method]
        out.append(f' {returns} {method}({parameters}){" const" if method in CONST_METHODS else ""} override;\n')
    for m in methods:
        returns, parameters = SPECIAL[m['slot']][1:3] if m['slot'] in SPECIAL else ('void', '')
        out.append(f' virtual {returns} {m["name"]}({parameters});\n')
    out.append(f'}};\nstatic_assert(sizeof({CLASS})=={INSTANCE_SIZE:#x},"Original controller instance size");\n')
    return ''.join(out)


def generate(args):
    facts = json.loads(args.facts.read_text())
    methods = opaque_names(facts['diversity'])
    header = render(methods, facts['overrides'])
    HEADER.write_text(header)
    contract = dict(build=facts['build'], instance_size=INSTANCE_SIZE, slots=XHCI_SLOTS, ioservice_slots=IOSERVICE_SLOTS, identity=facts['identity'],
                    overrides=sorted(m['slot'] for m in methods if m['slot'] in SPECIAL),
                    ioservice_overrides=facts['overrides'], methods=[dict(slot=m['slot'], mangled=m['mangled'], discriminator=m['discriminator']) for m in methods],
                    header_sha256=hashlib.sha256(header.encode()).hexdigest())
    CONTRACT.write_text(json.dumps(contract, indent=1) + '\n')
    print('wrote', HEADER.name, len(header), 'bytes,', CONTRACT.name, len(methods), 'methods')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest='command', required=True)
    ex = sub.add_parser('extract')
    ex.add_argument('--bootkc', type=Path, required=True)
    ex.add_argument('--facts', type=Path, required=True)
    gen = sub.add_parser('generate')
    gen.add_argument('--facts', type=Path, required=True)
    args = parser.parse_args()
    (extract if args.command == 'extract' else generate)(args)


if __name__ == '__main__':
    main()
