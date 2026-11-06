#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""Stage15 GOP copy-path rebuild. No device access, firmware packaging, or boot claim.

check:   verify the reviewed display overlay hashes and run its host tests (Apple clang, ASan+UBSan).
rebuild: after the unmodified upstream build has produced NativeUfs.o, record that object, apply the
         overlay, regenerate the display source with the upstream preparer, re-run the recorded upstream
         compile command for NativeUfs.o, and link both objects with the pinned VirtIO object exactly as
         link_probe.py does.
"""
import argparse
import json
import os
from pathlib import Path
import platform
import shutil
import subprocess
import sys
import zipfile

LAB = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(LAB / "scripts"))
import link_probe as lp  # noqa: E402

OVERLAY = LAB / "gop-copy-path"
EXPECTED = json.loads((OVERLAY / "expected.json").read_text())
NATIVE_OUT = Path("out/macos26-xnu-arm64e")


def verify_overlay():
    found = {}
    for relative, digest in EXPECTED["overlay"].items():
        actual = lp.file_sha(OVERLAY / relative)
        if actual != digest:
            raise ValueError(f"overlay SHA256 mismatch: {relative}")
        found[relative] = actual
    return found


def check(output):
    output.mkdir(parents=True, exist_ok=True)
    record = {"overlay": verify_overlay(), "tests": {}}
    compiler = shutil.which("clang++")
    if not compiler:
        raise RuntimeError("host clang++ is required")
    for name in ("native_gop_blit", "native_gop_scanout_upstream"):
        binary = output / name
        command = [compiler, "-std=c++17", "-Wall", "-Wextra", "-Werror", "-O1", "-fsanitize=address,undefined",
                   "-fno-sanitize-recover=all", str(OVERLAY / "tests" / (name + ".cpp")), "-o", str(binary)]
        subprocess.run(command, check=True, timeout=180)
        subprocess.run([str(binary)], check=True, timeout=120)
        record["tests"][name] = {"command": command, "passed": True}
    (output / "check.json").write_text(json.dumps(record, indent=2) + "\n")
    print("overlay verified; host tests passed")


def recorded(build, log):
    matches = [entry for entry in build["commands"] if entry.get("log") == log]
    if len(matches) != 1 or matches[0].get("returncode") != 0:
        raise ValueError(f"expected one successful recorded command: {log}")
    return matches[0]


def link_variant(output, ufs_object, virtio, provenance):
    inputs = output / "inputs"
    inputs.mkdir(parents=True, exist_ok=False)
    (inputs / "NativeVirtioBlock.o").write_bytes(virtio)
    data = ufs_object.read_bytes()
    lp.commands(data, 1)
    (inputs / "NativeUfs.o").write_bytes(data)
    renamed, modification = lp.rename_kmod(data)
    (inputs / "NativeUfs-renamed.o").write_bytes(renamed)
    provenance = dict(provenance, rename=modification, ufs_object={"sha256": lp.sha(data), "bytes": len(data)})
    (output / "inputs.json").write_text(json.dumps(provenance, indent=2) + "\n")
    lp.link(output, provenance)
    return json.loads((output / "result.json").read_text())["outputs"]


def rebuild(upstream, output):
    if platform.system() != "Darwin" or platform.machine() != "arm64":
        raise RuntimeError("the rebuild requires an Apple Silicon macOS runner")
    verify_overlay()
    upstream = upstream.resolve()
    output.mkdir(parents=True, exist_ok=True)
    if any(output.iterdir()):
        raise ValueError("output directory must be empty")
    head = subprocess.run(["git", "-C", str(upstream), "rev-parse", "HEAD"], capture_output=True, text=True, check=True).stdout.strip()
    if head != EXPECTED["upstream"]["commit"]:
        raise ValueError("upstream checkout is not the pinned commit")
    native = upstream / NATIVE_OUT
    build = json.loads((native / "build.json").read_text())
    display = build["ufs_driver"]["native_gop_display"]
    original = EXPECTED["original"]
    if (display["source_sha256"] != original["display_source_sha256"] or display["patch_sha256"] != original["display_patch_sha256"]
            or display["scanout_sha256"] != original["scanout_header_sha256"]):
        raise ValueError("the first build did not compile the unmodified upstream display source")
    compile_entry = recorded(build, "logs/ufs-driver-compile.log")
    ufs_object = native / "ufs-driver/NativeUfs.o"
    baseline = output / "rebuilt-original-NativeUfs.o"
    shutil.copyfile(ufs_object, baseline)
    record = {
        "upstream_commit": head, "toolchain": build.get("toolchain"),
        "rebuilt_original": {"sha256": lp.file_sha(baseline), "bytes": baseline.stat().st_size,
                             "matches_upstream_ci_object": lp.file_sha(baseline) == original["ufs_object_sha256"]},
        "overlay": EXPECTED["overlay"],
    }
    # Apply the reviewed overlay and regenerate the display source with the unchanged upstream preparer.
    display_dir = upstream / "platform/sheng/macos26/display"
    for name in ("0001-native-gop-display.patch", "gop_scanout.h"):
        shutil.copyfile(OVERLAY / "display" / name, display_dir / name)
    sys.path.insert(0, str(upstream / "scripts"))
    from prepare_macos26_gop import prepare  # noqa: E402
    evidence = prepare(upstream, native / "ufs-driver/gop-display")
    if (evidence["source_sha256"] != EXPECTED["stage15_display_source_sha256"]
            or evidence["header_sha256"] != original["framebuffer_header_sha256"]
            or evidence["scanout_sha256"] != EXPECTED["overlay"]["display/gop_scanout.h"]):
        raise ValueError("prepared Stage15 display source differs from the reviewed overlay")
    record["stage15_display"] = evidence
    ufs_object.unlink()
    log = output / "stage15-ufs-driver-compile.log"
    with log.open("w") as stream:
        subprocess.run(compile_entry["command"], cwd=compile_entry["cwd"], stdout=stream, stderr=subprocess.STDOUT,
                       check=True, timeout=1800)
    stage15 = output / "stage15-NativeUfs.o"
    shutil.copyfile(ufs_object, stage15)
    record["stage15_object"] = {"sha256": lp.file_sha(stage15), "bytes": stage15.stat().st_size,
                                "compile_command": compile_entry["command"], "compile_cwd": compile_entry["cwd"]}
    with zipfile.ZipFile(LAB / "inputs/driver-objects-a200d204.zip") as bundle:
        manifest = lp.validate_persistent_bundle(bundle)
        virtio = lp.read_pinned_member(bundle, "storage-driver/NativeVirtioBlock.o", lp.OBJECTS["storage-driver/NativeVirtioBlock.o"])
    provenance = {"original_artifact": lp.original_artifact(), "virtio_from_bundle_sha256": lp.BUNDLE_SHA,
                  "bundle_manifest": manifest, "workflow_commit": os.environ.get("GITHUB_SHA"),
                  "run_id": os.environ.get("GITHUB_RUN_ID")}
    record["links"] = {
        "rebuilt_original": link_variant(output / "link-rebuilt-original", baseline, virtio, dict(provenance, variant="rebuilt-original")),
        "stage15": link_variant(output / "link-stage15", stage15, virtio, dict(provenance, variant="stage15")),
    }
    record["links"]["rebuilt_original_combined_matches_upstream"] = (
        record["links"]["rebuilt_original"]["NativeVirtioGop"]["sha256"] == original["combined_module_sha256"])
    record.update(runtime_tested=False, abi_pac_audited=False, hardware_loadable=False,
                  notes="Stage15 object and combined module still require local ABI/PAC admission and a bounded QEMU run.")
    (output / "rebuild.json").write_text(json.dumps(record, indent=2) + "\n")
    print(json.dumps({k: record[k] for k in ("rebuilt_original", "stage15_object")}, indent=2))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="mode", required=True)
    c = sub.add_parser("check")
    c.add_argument("--output", type=Path, default=Path("out/check"))
    r = sub.add_parser("rebuild")
    r.add_argument("--upstream", type=Path, required=True)
    r.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if args.mode == "check":
        check(args.output)
    else:
        rebuild(args.upstream, args.output)


if __name__ == "__main__":
    main()
