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
ORIGINAL_ZIP_SHA = "bdc4acdc0f34c6d3a85a4e53e120a4f1113a78143bb13447a665077b94b5453f"
BUNDLE_SHA = "6db20d3a711308cdbd8ca312ee198d8ebac7792dd423bb071c50413fc5d37e8c"
LICENSE_SHA = "3972dc9744f6499f0f9b2dbf76696f2ae7ad8af9b23dde66d6af86c9dfb36986"
OBJECTS = {
    "storage-driver/NativeVirtioBlock.o": "ff34755c0846fc3017cc3a47054a645f335c3c31efdc50e161f9c7e2426d7235",
    "ufs-driver/NativeUfs.o": "e3fea376496dc240ddc9e70982a788d267fea37a409d32a9c552c3441950e8ef",
}
WRAPPERS = {
    "storage-driver/NativeVirtioBlock.cpp": "68119d284e492dbe255153038740ce008dedf6d5f3cf28376c6ee723fb98705e",
    "ufs-driver/NativeUfs.cpp": "ea750d45600d05495fddfbaa7d0e9f6265f70bb924bdb9952a68621844b7c85d",
}
SOURCE_NOTE = """# Driver object provenance

These two third-party upstream ARM64E objects and generated wrapper sources were extracted unchanged
from the public CrossDarwin/macOS-ForSheng CI artifact 12956372647, run
41008552166, source commit a200d204230846bb640b715086d8c627405ba2df.
NativeUfs.o includes the GOP companion. It also retains UFS code.

Original project source, build scripts, license, and patches:
https://github.com/CrossDarwin/macOS-ForSheng/tree/a200d204230846bb640b715086d8c627405ba2df
Dependency pins:
https://github.com/CrossDarwin/macOS-ForSheng/blob/a200d204230846bb640b715086d8c627405ba2df/sources.lock.json
UniXNU patches applied by that project:
https://github.com/CrossDarwin/macOS-ForSheng/tree/a200d204230846bb640b715086d8c627405ba2df/patches/UniXNU
Pinned UniXNU base source:
https://github.com/UniXNU/UniXNU/tree/fb6aad6ab1f50199a4d99d2da1d0ca403b067633

The generated wrappers retain their original public CI runner include paths.
They are provenance aids, not self-contained driver source or a rebuild SDK.
Follow the pinned source lock and patch series to obtain the matching source.
The GPL license is copied unchanged from the pinned project's LICENSE.
Original authors retain their copyrights; this lab does not claim authorship
of the drivers. No Apple firmware, BootKC, BaseSystem, KDK, device dump,
credential, or full upstream artifact ZIP is included.

This bundle only preserves inputs for a linker experiment. It is not a driver
installation package and does not establish runtime or hardware compatibility.
"""


def original_artifact():
    return {"repository": REPO, "source_commit": COMMIT, "run_id": RUN,
            "artifact_id": ARTIFACT, "sha256": ORIGINAL_ZIP_SHA, "bytes": 44377627}


def json_bytes(value):
    return (json.dumps(value, indent=2, sort_keys=True) + "\n").encode("utf-8")


def sha(data):
    return hashlib.sha256(data).hexdigest()


def file_sha(path):
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def read_pinned_member(bundle, name, expected):
    if bundle.namelist().count(name) != 1:
        raise ValueError(f"missing or duplicate member: {name}")
    item = bundle.getinfo(name)
    if item.file_size > 2 * 1024 * 1024:
        raise ValueError("oversized member")
    data = bundle.read(item)
    if sha(data) != expected:
        raise ValueError(f"member SHA256 mismatch: {name}")
    return data


def make_input_bundle(archive, destination):
    """Preserve only allowlisted public driver inputs, with deterministic ZIP metadata."""
    if file_sha(archive) != ORIGINAL_ZIP_SHA:
        raise ValueError("original artifact SHA256 mismatch")
    if destination.exists():
        raise ValueError("bundle destination must not already exist")
    files = {}
    with zipfile.ZipFile(archive) as original:
        for name, digest in {**OBJECTS, **WRAPPERS}.items():
            files[name] = read_pinned_member(original, name, digest)
            if name in OBJECTS:
                commands(files[name], 1)
    license_data = (Path(__file__).resolve().parents[1] / "LICENSE").read_bytes()
    if sha(license_data) != LICENSE_SHA:
        raise ValueError("GPL license SHA256 mismatch")
    files["LICENSE"] = license_data
    files["SOURCE.md"] = SOURCE_NOTE.encode("utf-8")
    manifest = {
        "format": 1, "kind": "persistent_driver_objects",
        "original_artifact": original_artifact(),
        "packing": {"compression": "ZIP_STORED", "timestamp": "1980-01-01T00:00:00", "mode": "0100644", "entry_order": "lexicographic"},
        "members": {name: {"sha256": sha(data), "bytes": len(data)} for name, data in files.items()},
    }
    files["provenance.json"] = json_bytes(manifest)
    destination.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(destination, "x", compression=zipfile.ZIP_STORED) as packaged:
        for name in sorted(files):
            info = zipfile.ZipInfo(name, date_time=(1980, 1, 1, 0, 0, 0))
            info.create_system = 3
            info.external_attr = 0o100644 << 16
            info.compress_type = zipfile.ZIP_STORED
            info.extra = b""
            info.comment = b""
            packaged.writestr(info, files[name])
    return {"path": str(destination), "sha256": file_sha(destination), "bytes": destination.stat().st_size}


def validate_persistent_bundle(bundle):
    expected_names = set(OBJECTS) | set(WRAPPERS) | {"LICENSE", "SOURCE.md", "provenance.json"}
    if len(bundle.namelist()) != len(expected_names) or set(bundle.namelist()) != expected_names:
        raise ValueError("persistent bundle member allowlist mismatch")
    manifest = json.loads(bundle.read("provenance.json"))
    if manifest.get("format") != 1 or manifest.get("kind") != "persistent_driver_objects" or manifest.get("original_artifact") != original_artifact():
        raise ValueError("persistent bundle provenance mismatch")
    if set(manifest.get("members", {})) != expected_names - {"provenance.json"}:
        raise ValueError("persistent bundle manifest member mismatch")
    pins = {**OBJECTS, **WRAPPERS, "LICENSE": LICENSE_SHA, "SOURCE.md": sha(SOURCE_NOTE.encode("utf-8"))}
    for name, digest in pins.items():
        data = read_pinned_member(bundle, name, digest)
        if manifest["members"][name] != {"sha256": digest, "bytes": len(data)}:
            raise ValueError(f"persistent bundle manifest mismatch: {name}")
    return manifest


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
    if digest not in (ORIGINAL_ZIP_SHA, BUNDLE_SHA):
        raise ValueError(f"artifact SHA256 mismatch: {digest}")
    kind = "original_ci_artifact" if digest == ORIGINAL_ZIP_SHA else "persistent_driver_objects"
    inputs = output / "inputs"
    inputs.mkdir(parents=True, exist_ok=True)
    record = {"original_artifact": original_artifact(), "input_archive": {"kind": kind, "sha256": digest, "bytes": archive.stat().st_size}, "objects": {}}
    with zipfile.ZipFile(archive) as bundle:
        if kind == "persistent_driver_objects":
            record["bundle_manifest"] = validate_persistent_bundle(bundle)
        for name, expected in OBJECTS.items():
            data = read_pinned_member(bundle, name, expected)
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
    destination = parser.add_mutually_exclusive_group(required=True)
    destination.add_argument("--output", type=Path)
    destination.add_argument("--make-input-bundle", type=Path, help="repackage the pinned original ZIP using a strict allowlist")
    parser.add_argument("--prepare-only", action="store_true")
    args = parser.parse_args()
    if args.make_input_bundle:
        if args.prepare_only:
            parser.error("--prepare-only cannot be combined with --make-input-bundle")
        print(json.dumps(make_input_bundle(args.artifact, args.make_input_bundle), indent=2))
        return
    args.output.mkdir(parents=True, exist_ok=True)
    if any(args.output.iterdir()):
        raise ValueError("output directory must be empty")
    provenance = prepare(args.artifact, args.output)
    if not args.prepare_only:
        link(args.output, provenance)
    print("Inputs verified" if args.prepare_only else "Three link probes passed; runtime not tested")


if __name__ == "__main__":
    main()
