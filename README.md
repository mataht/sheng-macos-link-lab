# sheng macOS ARM64E link lab

Minimal Apple-linker comparison for Xiaomi Pad 6S Pro macOS research. This is a **build experiment**, not an installable macOS image or a device flashing tool.

The workflow uses the standard GitHub-hosted Apple Silicon `xcode-27` runner. Standard runners in public repositories are free under [GitHub's documented policy](https://docs.github.com/en/actions/reference/runners/github-hosted-runners). Each run has a 15-minute limit and keeps small result artifacts for three days.

## Inputs and reproducibility

Inputs are fetched from the public [CrossDarwin/macOS-ForSheng CI run 41008552166](https://github.com/CrossDarwin/macOS-ForSheng/actions/runs/41008552166), source commit `a200d204230846bb640b715086d8c627405ba2df`, artifact `12956372647`. The full ZIP and both ARM64E objects are pinned by SHA256 in the script. Only `NativeVirtioBlock.o` and `NativeUfs.o` are extracted. The latter contains the GOP companion.

Upstream Actions artifacts expire. If downloading fails or the archive is gone, this workflow fails rather than silently using a different build. A future immutable input release needs separate provenance verification. The job attempts the public artifact download with its short-lived read-only `GITHUB_TOKEN`; cross-repository access must be confirmed by an actual run. It does not request or store a personal token.

Three probes link the original VirtIO object, original UFS/GOP object, and both together. In a copy of the UFS object, the unique external `_kmod_info` definition is renamed to equal-length `_ufs_kmod_` within the Mach-O string table. All other bytes remain unchanged; the script rejects shared suffixes or unexpected symbols. The combined probe keeps the VirtIO module entry.

Successful CI means Apple ld accepted the inputs and produced ARM64E `MH_KEXT_BUNDLE` outputs with chained fixups. It **does not** establish resolved runtime imports, class/vtable/PAC compatibility, successful driver startup, graphical acceleration, or bootability. These require local review against the paired kernel and original system components. The combined object still contains UFS code; no combined Info.plist or boot package is generated here.

No Apple BootKC, BaseSystem, KDK, device data, or credentials are committed to this repository. Only scripts are stored here. Result artifacts contain link probes and diagnostics; the original downloaded ZIP is not uploaded.

## Local input check

With Python 3.9 or later:

```sh
python3 scripts/link_probe.py --artifact /path/to/original-ci-artifact.zip --output probe-inputs --prepare-only
```

On an Apple Silicon Mac, omit `--prepare-only` to run the links. Use a new empty output directory each time.

The script is provided under GPL-3.0-or-later. Upstream source, patches, license, build scripts, and pinned dependencies are available at [the exact source commit](https://github.com/CrossDarwin/macOS-ForSheng/tree/a200d204230846bb640b715086d8c627405ba2df), including `sources.lock.json` and the `patches/UniXNU` series. This project makes no claim to authorship of those drivers.
