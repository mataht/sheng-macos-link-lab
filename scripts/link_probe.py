#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""Pinned ARM64E link probe. No device access, firmware packaging, or boot claim."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import platform
import struct
import subprocess
import zipfile

REPO = "CrossDarwin/macOS-ForSheng"
COMMIT = "a200d204230846bb640b715086d8c627405ba2df"
RUN = 41008552166
ARTIFACT = 12956372647
ZIP_SHA = "bdc4acdc0f34c6d3a85a4e53e120a4f1113a78143bb13447a665077b94b5453f"
OBJECTS = {
    "storage-driver/NativeVirtioBlock.o": "ff34755c0846fc3017cc3a47054a645f335c3c31efdc50e161f9c7e2426d7235",
    "ufs-driver/NativeUfs.o": "e3fea376496dc240ddc9e70982a788d267fea37a409d32a9c552c3441950e8ef",
}


def sha(data):
    return hashlib.sha256(data).hexdigest()


def file_sha(path):
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def commands(data, filetype):
    if len(data) < 32:
        raise ValueError("truncated Mach-O")
    magic, cpu, subtype, kind, count, size, _, _ = struct.unpack_from("<8I", data)
    if (magic, cpu, subtype, kind) != (0xFEEDFACF, 0x0100000C, 0xC0000002, filetype):
        raise ValueError(f"unexpected Mach-O header: {(magic, cpu, subtype, kind)}")
    if count > 1024 or size > len(data) - 32:
        raise ValueError("invalid load command bounds")
    offset = 32
    found = []
    for _ in range(count):
        if offset + 8 > 32 + size:
            raise ValueError("truncated load command")
        cmd, length = struct.unpack_from("<II", data, offset)
        if length < 8 or length % 8 or offset + length > 32 + size:
            raise ValueError("invalid load command size")
        found.append((cmd, offset, length))
        offset += length
    if offset != 32 + size:
        raise ValueError("load command size mismatch")
    return found


def rename_kmod(data):
    """Rename one defined symbol using equal length bytes in its string table."""
    symtabs = [entry for entry in commands(data, 1) if entry[0] == 2]
    if len(symtabs) != 1 or symtabs[0][2] != 24:
        raise ValueError("expected one LC_SYMTAB")
    symoff, nsyms, stroff, strsize = struct.unpack_from("<4I", data, symtabs[0][1] + 8)
    commands_end = 32 + struct.unpack_from("<I", data, 20)[0]
    symend = symoff + nsyms * 16
    strend = stroff + strsize
    if min(symoff, stroff) < commands_end or symend > len(data) or strend > len(data):
        raise ValueError("symbol table out of bounds")
    if symoff < strend and stroff < symend:
        raise ValueError("symbol and string tables overlap")
    symbols = []
    for index in range(nsyms):
        strx, typ, sect, desc, value = struct.unpack_from("<IBBHQ", data, symoff + index * 16)
        if strx >= strsize:
            raise ValueError("string index out of bounds")
        end = data.find(b"\0", stroff + strx, stroff + strsize)
        if end < 0:
            raise ValueError("unterminated symbol")
        symbols.append((strx, typ, data[stroff + strx:end]))
    matches = [s for s in symbols if s[2] == b"_kmod_info"]
    if len(matches) != 1 or matches[0][1] & 0xE1 != 1 or matches[0][1] & 0x0E != 0x0E:
        raise ValueError("expected exactly one external defined _kmod_info")
    start = matches[0][0]
    if any(s[2] == b"_ufs_kmod_" for s in symbols):
        raise ValueError("replacement symbol already exists")
    if any(s != matches[0] and s[0] < start + 10 and s[0] + len(s[2]) > start for s in symbols):
        raise ValueError("shared symbol string would be modified")
    replacement = b"_ufs_kmod_"
    offset = stroff + start
    edited = data[:offset] + replacement + data[offset + len(replacement):]
    if len(edited) != len(data) or edited[:offset] != data[:offset] or edited[offset + 10:] != data[offset + 10:]:
        raise ValueError("unexpected modification outside symbol spelling")
    return edited, {"offset": offset, "old": "_kmod_info", "new": replacement.decode(), "input_sha256": sha(data), "output_sha256": sha(edited)}


def prepare(archive, output):
    digest = file_sha(archive)
    if digest != ZIP_SHA:
        raise ValueError(f"artifact SHA256 mismatch: {digest}")
    inputs = output / "inputs"
    inputs.mkdir(parents=True, exist_ok=True)
    record = {"repository": REPO, "source_commit": COMMIT, "run_id": RUN, "artifact_id": ARTIFACT, "artifact_sha256": digest, "objects": {}}
    with zipfile.ZipFile(archive) as bundle:
        for name, expected in OBJECTS.items():
            if bundle.namelist().count(name) != 1:
                raise ValueError(f"missing or duplicate object: {name}")
            item = bundle.getinfo(name)
            if item.file_size > 2 * 1024 * 1024:
                raise ValueError("oversized object")
            data = bundle.read(item)
            if sha(data) != expected:
                raise ValueError(f"object SHA256 mismatch: {name}")
            commands(data, 1)
            (inputs / Path(name).name).write_bytes(data)
            record["objects"][name] = {"sha256": sha(data), "bytes": len(data)}
    renamed, modification = rename_kmod((inputs / "NativeUfs.o").read_bytes())
    (inputs / "NativeUfs-renamed.o").write_bytes(renamed)
    record["rename"] = modification
    (output / "inputs.json").write_text(json.dumps(record, indent=2) + "\n")
    return record


def run_tool(command, output, stem, allow_failure=False):
    result = subprocess.run(command, capture_output=True, text=True, timeout=180)
    (output / f"{stem}.stdout.txt").write_text(result.stdout)
    (output / f"{stem}.stderr.txt").write_text(result.stderr)
    (output / f"{stem}.command.json").write_text(json.dumps({"command": command, "returncode": result.returncode}, indent=2) + "\n")
    if result.returncode and not allow_failure:
        raise RuntimeError(f"{stem} failed, exit={result.returncode}; see saved logs")
    return result.stdout


def link(output, provenance):
    if platform.system() != "Darwin" or platform.machine() != "arm64":
        raise RuntimeError("this CI comparison requires an Apple Silicon macOS runner")
    run_tool(["xcodebuild", "-version"], output, "xcode-version")
    run_tool(["sw_vers"], output, "macos-version")
    # Some ld versions print their version but return nonzero without input files.
    # Preserve that status; only the actual link invocations decide probe success.
    run_tool(["xcrun", "ld", "-v"], output, "linker-version", allow_failure=True)
    inputs = output / "inputs"
    flags = ["xcrun", "ld", "-arch", "arm64e", "-kext", "-kexts_use_stubs", "-platform_version", "macos", "26.5", "26.5", "-undefined", "dynamic_lookup", "-fixup_chains", "-no_adhoc_codesign", "-headerpad", "0x1000"]
    cases = {
        "NativeVirtioBlock": ["NativeVirtioBlock.o"],
        "NativeUfs": ["NativeUfs.o"],
        "NativeVirtioGop": ["NativeVirtioBlock.o", "NativeUfs-renamed.o"],
    }
    results = {}
    for name, objects in cases.items():
        target = output / f"{name}.macho"
        command = flags + [str(inputs / item) for item in objects] + ["-o", str(target)]
        run_tool(command, output, name + "-link")
        data = target.read_bytes()
        loads = commands(data, 11)
        if not any(cmd == 0x80000034 for cmd, _, _ in loads):
            raise ValueError("linked output is missing LC_DYLD_CHAINED_FIXUPS")
        run_tool(["xcrun", "nm", "-g", str(target)], output, name + "-symbols")
        run_tool(["xcrun", "otool", "-l", str(target)], output, name + "-load-commands")
        results[name] = {"sha256": sha(data), "bytes": len(data), "command": command, "arm64e": True, "mh_kext_bundle": True, "chained_fixups_present": True}
    report = {"status": "link_probe_passed", "commit": os.environ.get("GITHUB_SHA"), "run_id": os.environ.get("GITHUB_RUN_ID"), "runner_arch": platform.machine(), "provenance": provenance, "outputs": results, "runtime_tested": False, "abi_pac_audited": False, "hardware_loadable": False, "notes": "Combined probe retains UFS code. No Info.plist or boot package is generated; local ABI/PAC and registration review must precede any QEMU use."}
    (output / "result.json").write_text(json.dumps(report, indent=2) + "\n")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--artifact", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--prepare-only", action="store_true")
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=True)
    if any(args.output.iterdir()):
        raise ValueError("output directory must be empty")
    provenance = prepare(args.artifact, args.output)
    if not args.prepare_only:
        link(args.output, provenance)
    print("Inputs verified" if args.prepare_only else "Three link probes passed; runtime not tested")


if __name__ == "__main__":
    main()
