# sheng macOS ARM64E link lab

Minimal Apple-linker comparison for Xiaomi Pad 6S Pro macOS research. This is a **build experiment**, not an installable macOS image or a device flashing tool.

The workflow uses the standard GitHub-hosted Apple Silicon `xcode-27` runner. Standard runners in public repositories are free under [GitHub's documented policy](https://docs.github.com/en/actions/reference/runners/github-hosted-runners). Each run has a 15-minute limit and keeps small result artifacts for three days.

## Inputs and reproducibility

The repository preserves two third-party upstream open-source driver objects in `inputs/driver-objects-a200d204.zip`, originally extracted from the public [CrossDarwin/macOS-ForSheng CI run 41008552166](https://github.com/CrossDarwin/macOS-ForSheng/actions/runs/41008552166), source commit `a200d204230846bb640b715086d8c627405ba2df`, artifact `12956372647`. The objects are unchanged: `NativeVirtioBlock.o` and `NativeUfs.o`; the latter contains the GOP companion. The bundle also holds the original two generated wrappers, GPL license, source links, and provenance manifest. These drivers are not authored by this lab.

CI reads the checked-in bundle and therefore remains rerunnable after the original Actions artifact expires. It does not download artifacts across repositories, use `gh`, or request `actions:read` or a personal token. The original full artifact ZIP is not committed. The original ZIP, persistent bundle, and individual objects have distinct pinned SHA256 values. `inputs.json` records the archive actually read under `input_archive` and the upstream source archive under `original_artifact`; it never presents the bundle digest as the original artifact digest.

The bundle uses sorted entries, a fixed 1980 timestamp, fixed Unix file modes, and `ZIP_STORED` without compression. This makes repackaging independent of zlib versions. Its manifest hashes every payload member and identifies the exact source commit and upstream artifact. Preparation rejects any unexpected bundle member, altered payload, or mismatched provenance. Only the two object files are extracted for linking.

Three probes link the original VirtIO object, original UFS/GOP object, and both together. In a copy of the UFS object, the unique external `_kmod_info` definition is renamed to equal-length `_ufs_kmod_` within the Mach-O string table. All other bytes remain unchanged; the script rejects shared suffixes or unexpected symbols. The combined probe keeps the VirtIO module entry.

Successful CI means Apple ld accepted the inputs and produced ARM64E `MH_KEXT_BUNDLE` outputs with chained fixups. It **does not** establish resolved runtime imports, class/vtable/PAC compatibility, successful driver startup, graphical acceleration, or bootability. These require local review against the paired kernel and original system components. The combined object still contains UFS code; no combined Info.plist or boot package is generated here.

No Apple BootKC, BaseSystem, KDK, device data, credentials, or full upstream ZIP is committed to this repository. Result artifacts contain link probes and diagnostics.

## Local input check

With Python 3.9 or later:

```sh
python3 scripts/link_probe.py --artifact inputs/driver-objects-a200d204.zip --output probe-inputs --prepare-only
```

On an Apple Silicon Mac, omit `--prepare-only` to run the links. Use a new empty output directory each time. The same command also accepts the SHA-pinned original CI artifact ZIP for independent comparison.

To reproduce the preserved bundle from a saved original artifact and the unchanged repository license, choose a new destination:

```sh
python3 scripts/link_probe.py --artifact /path/to/original-ci-artifact.zip --make-input-bundle /path/to/repacked-driver-objects.zip
```

The command prints the result size and SHA256. It verifies the original ZIP and each selected member before packaging; it never copies other archive entries. The generated wrappers retain their public CI include paths and are provenance aids, not self-contained source or a rebuild SDK. Obtain matching complete upstream sources using the pinned repository, `sources.lock.json`, and patch series linked below.

The script is provided under GPL-3.0-or-later. Upstream source, patches, license, build scripts, and pinned dependencies are available at [the exact source commit](https://github.com/CrossDarwin/macOS-ForSheng/tree/a200d204230846bb640b715086d8c627405ba2df), including `sources.lock.json` and the `patches/UniXNU` series. This project makes no claim to authorship of those drivers.
