// SPDX-License-Identifier: MIT
#pragma once

// The root IOSurface can have a different backing scale from the scanout.
// Select source pixels before format conversion; never relabel its storage.
// The caller validates nonzero extents and supplies a complete source row.
template <typename Pixel>
static inline void nativeGopScanoutRowInto(Pixel *destination, uint64_t destinationWidth,
                                         const uint16_t *source, uint64_t sourceWidth,
                                         const uint8_t *halfToByte)
{
    uint64_t sourceX = 0, remainder = 0;
    const uint64_t step = sourceWidth / destinationWidth;
    const uint64_t fraction = sourceWidth % destinationWidth;
    for (uint64_t x = 0; x < destinationWidth; x++) {
        if (halfToByte) {
            const uint16_t *rgba = source + sourceX * 4;
            destination[x] = 0xff000000u | static_cast<uint32_t>(halfToByte[rgba[0]]) << 16 |
                             static_cast<uint32_t>(halfToByte[rgba[1]]) << 8 | halfToByte[rgba[2]];
        } else {
            destination[x] = reinterpret_cast<const uint32_t *>(source)[sourceX];
        }
        sourceX += step;
        remainder += fraction;
        if (remainder >= destinationWidth) {
            sourceX++;
            remainder -= destinationWidth;
        }
    }
}

static inline void nativeGopScanoutRow(volatile uint32_t *destination, uint64_t destinationWidth,
                                     const uint16_t *source, uint64_t sourceWidth,
                                     const uint8_t *halfToByte)
{
    nativeGopScanoutRowInto(destination, destinationWidth, source, sourceWidth, halfToByte);
}

// Row-granular scanout update for a CPU-copied display.
//
// Each destination row is first produced in ordinary memory (or taken directly from a
// same-size BGRA source row) and compared with the shadow copy of what this driver last
// wrote to the scanout. Unchanged rows are not rewritten and take no pixel lease. A changed
// row is written to the scanout inside the caller's lease, then recorded in the shadow.
// A refused lease stops the frame; rows already written stay consistent with the shadow.
//
// The source is a complete mapping of `sourceHeight` rows, `sourceRowBytes` apart, of which
// the first `sourceWidth * pixelBytes` bytes are read. `rowScratch` holds one source row
// (aligned for the half-float path); `converted` and each shadow row hold `copyWidth` pixels.
// `onRow(y, row)` receives every converted source row for optional sampling (may be a no-op).
struct NativeGopBlitResult {
    uint64_t written, skipped;
    bool complete;
};

template <typename Lease, typename OnRow>
static inline NativeGopBlitResult nativeGopBlitRows(uint8_t *scanout, uint64_t scanoutRowBytes,
        uint64_t copyWidth, uint64_t copyHeight, const uint8_t *source, uint64_t sourceRowBytes,
        uint64_t sourceWidth, uint64_t sourceHeight, uint64_t pixelBytes, const uint8_t *halfToByte,
        uint16_t *rowScratch, uint32_t *converted, uint32_t *shadow, bool shadowValid,
        Lease &lease, OnRow &&onRow)
{
    NativeGopBlitResult result{0, 0, true};
    const uint64_t destinationBytes = copyWidth * 4;
    const uint64_t sourceBytes = sourceWidth * pixelBytes;
    const bool identity = !halfToByte && pixelBytes == 4 && sourceWidth == copyWidth;
    for (uint64_t y = 0; y < copyHeight; y++) {
        const uint8_t *sourceRow = source + (y * sourceHeight / copyHeight) * sourceRowBytes;
        const void *pixels;
        if (identity) {
            // A kernel IOSurface mapping can fault on first access. Stage the full row
            // into owned wired scratch before the pixel lease masks interrupts.
            __builtin_memcpy(rowScratch, sourceRow, sourceBytes);
            pixels = rowScratch;
        } else {
            __builtin_memcpy(rowScratch, sourceRow, sourceBytes);
            nativeGopScanoutRowInto(converted, copyWidth, rowScratch, sourceWidth, halfToByte);
            onRow(y, rowScratch);
            pixels = converted;
        }
        uint32_t *shadowRow = shadow + y * copyWidth;
        if (shadowValid && __builtin_memcmp(pixels, shadowRow, destinationBytes) == 0) {
            result.skipped++;
            continue;
        }
        if (!lease.begin()) {
            result.complete = false;
            return result;
        }
        __builtin_memcpy(scanout + y * scanoutRowBytes, pixels, destinationBytes);
        lease.end();
        __builtin_memcpy(shadowRow, pixels, destinationBytes);
        result.written++;
    }
    return result;
}
