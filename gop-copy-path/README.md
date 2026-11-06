# Stage15B: stage source rows before the framebuffer lease

Stage15's same-size BGRA fast path first read its IOSurface mapping after acquiring the
framebuffer pixel lease, which masks interrupts. A local QEMU run panicked on a source
load with a level-3 translation fault. This revision stages the complete source row into
the existing scratch buffer before entering the lease. It does not change source-map
caching, dirty-row detection, destination writes, the class declaration, or the paired ABI.

`display/gop_scanout.h` is the only changed driver source relative to successful build
commit `b5d0787d27b561766fe1c6e5d967e641a52a238a` (Actions run `42152971660`).
The previous header is retained under `original/stage15-gop_scanout.h` only as a negative
host-test input; it is not installed into the rebuilt driver.

The source/lease regression uses a cross-page row and protects the source with
`mprotect(PROT_NONE)` when the lease begins. An isolated original-header child must fault;
the fixed header must complete and preserve pixels, shadow and unchanged-row skipping.
Darwin maps this existing-mapping protection failure to SIGBUS; Linux uses SIGSEGV.
The test uses the exact expected signal for each OS. This tests access ordering, not XNU
paging or device-memory correctness. Existing host pixel/layout tests still use ASan/UBSan.

The workflow remains based on pinned `CrossDarwin/macOS-ForSheng` commit
`a200d204230846bb640b715086d8c627405ba2df`, its sources and patches, the paired 25F71 KDK,
and the successful `xcode-27` build environment. The unmodified original kernel/driver
build establishes generated headers and a recorded compile command; then the unchanged
`scripts/rebuild_gop_driver.py` rebuilds the overlaid NativeUfs object and combines it
with the same pinned VirtIO object. Local experiments adopt only the new driver artifacts
and retain their original native XNU kernel. No new kernel build is claimed to be runtime-qualified.

For compatibility with the unchanged build script, artifact records retain `stage15_*`
keys and the `out/rebuild/link-stage15/` path; `expected.json.stage15b`, workflow identity,
commit/run ID and hashes distinguish this revision. Build artifacts are named
`gop-source-staging-stage15b-<run_id>`.

A successful CI run proves compilation/linking and host tests only. It does not prove
ABI/PAC admission, a working macOS frame, input, hardware loading or GPU acceleration.
Fresh local admission and a fresh bounded QEMU run are required. No Apple firmware,
KDK, BootKC, system image or device data is committed. Upstream copyrights and licenses
remain applicable; this is a build experiment, not an installation or flashing package.
