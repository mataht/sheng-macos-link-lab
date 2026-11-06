// SPDX-License-Identifier: GPL-3.0-or-later
// Host checks for the Stage15 row-granular scanout update (display/gop_scanout.h).
#include <cassert>
#include <cstdint>
#include <cstring>
#include <vector>
#include "../platform/sheng/macos26/display/gop_scanout.h"

namespace {

struct CountingLease {
    unsigned begins = 0, ends = 0, refuseAt = ~0u;
    bool open = false;
    bool begin() {
        assert(!open);
        if (begins == refuseAt) return false;
        begins++;
        open = true;
        return true;
    }
    void end() {
        assert(open);
        open = false;
        ends++;
    }
};

struct Frame {
    uint64_t width, height, rowBytes;
    std::vector<uint8_t> bytes;
    Frame(uint64_t w, uint64_t h, uint64_t pixelBytes, uint64_t pad)
        : width(w), height(h), rowBytes(w * pixelBytes + pad), bytes(rowBytes * h, 0xcd) {}
    uint32_t *row32(uint64_t y) { return reinterpret_cast<uint32_t *>(bytes.data() + y * rowBytes); }
    uint16_t *row16(uint64_t y) { return reinterpret_cast<uint16_t *>(bytes.data() + y * rowBytes); }
};

// Reference: the previous driver loop wrote every row with nativeGopScanoutRow.
std::vector<uint32_t> legacy(Frame &source, uint64_t copyWidth, uint64_t copyHeight, uint64_t pixelBytes,
                             const uint8_t *table)
{
    std::vector<uint32_t> out(copyWidth * copyHeight);
    std::vector<uint16_t> row(source.width * pixelBytes / 2);
    for (uint64_t y = 0; y < copyHeight; y++) {
        memcpy(row.data(), source.bytes.data() + (y * source.height / copyHeight) * source.rowBytes, source.width * pixelBytes);
        nativeGopScanoutRow(out.data() + y * copyWidth, copyWidth, row.data(), source.width, table);
    }
    return out;
}

struct Target {
    uint64_t width, height, rowBytes;
    std::vector<uint8_t> scanout;
    std::vector<uint32_t> shadow, converted;
    std::vector<uint16_t> scratch;
    Target(uint64_t w, uint64_t h, uint64_t pad, uint64_t maxSourceRowBytes)
        : width(w), height(h), rowBytes(w * 4 + pad), scanout(rowBytes * h, 0xee), shadow(w * h, 0x12345678),
          converted(w), scratch(maxSourceRowBytes / 2) {}
    uint32_t pixel(uint64_t x, uint64_t y) const {
        uint32_t value;
        memcpy(&value, scanout.data() + y * rowBytes + x * 4, 4);
        return value;
    }
    void expectEquals(const std::vector<uint32_t> &reference) const {
        for (uint64_t y = 0; y < height; y++) {
            for (uint64_t x = 0; x < width; x++) assert(pixel(x, y) == reference[y * width + x]);
            for (uint64_t pad = width * 4; pad < rowBytes; pad++) assert(scanout[y * rowBytes + pad] == 0xee);
        }
    }
    template <typename Lease, typename OnRow>
    NativeGopBlitResult blit(Frame &source, uint64_t pixelBytes, const uint8_t *table, bool valid, Lease &lease, OnRow &&onRow) {
        return nativeGopBlitRows(scanout.data(), rowBytes, width, height, source.bytes.data(), source.rowBytes, source.width,
                                 source.height, pixelBytes, table, scratch.data(), converted.data(), shadow.data(), valid,
                                 lease, onRow);
    }
};

void fill(Frame &frame, uint32_t seed)
{
    for (uint64_t y = 0; y < frame.height; y++)
        for (uint64_t x = 0; x < frame.width; x++) frame.row32(y)[x] = 0xff000000u | ((seed + y * 977 + x * 31) & 0xffffff);
}

void sameSizeBgra()
{
    Frame source(8, 6, 4, 16);
    fill(source, 1);
    Target target(8, 6, 12, source.rowBytes);
    CountingLease lease;
    unsigned rows = 0;
    auto noRows = [&](uint64_t, const uint16_t *) { rows++; };
    NativeGopBlitResult first = target.blit(source, 4, nullptr, false, lease, noRows);
    assert(first.complete && first.written == 6 && first.skipped == 0 && lease.begins == 6 && lease.ends == 6);
    assert(rows == 0);   // the identity path has no converted half-float rows to sample
    target.expectEquals(legacy(source, 8, 6, 4, nullptr));
    for (uint64_t y = 0; y < 6; y++) assert(!memcmp(target.shadow.data() + y * 8, source.row32(y), 32));

    // An unchanged frame with a valid shadow writes nothing and takes no lease.
    CountingLease idle;
    NativeGopBlitResult second = target.blit(source, 4, nullptr, true, idle, noRows);
    assert(second.complete && second.written == 0 && second.skipped == 6 && idle.begins == 0);

    // A cursor-sized change rewrites only the affected rows.
    source.row32(2)[3] ^= 0x00ffffff;
    source.row32(3)[4] ^= 0x00ffffff;
    CountingLease cursor;
    NativeGopBlitResult third = target.blit(source, 4, nullptr, true, cursor, noRows);
    assert(third.complete && third.written == 2 && third.skipped == 4 && cursor.begins == 2);
    target.expectEquals(legacy(source, 8, 6, 4, nullptr));

    // An invalid shadow (first frame, geometry change, periodic refresh) rewrites every row.
    CountingLease refresh;
    NativeGopBlitResult fourth = target.blit(source, 4, nullptr, false, refresh, noRows);
    assert(fourth.written == 6 && refresh.begins == 6);
}

void scaledBgra()
{
    // A doubled backing surface (1600x1200 for an 800x600 scanout) in miniature.
    Frame source(16, 12, 4, 0);
    fill(source, 7);
    Target target(8, 6, 0, source.rowBytes);
    CountingLease lease;
    unsigned rows = 0;
    NativeGopBlitResult result = target.blit(source, 4, nullptr, false, lease, [&](uint64_t, const uint16_t *) { rows++; });
    assert(result.complete && result.written == 6 && rows == 6);
    target.expectEquals(legacy(source, 8, 6, 4, nullptr));
    source.row32(10)[14] ^= 0x00ffffff;   // maps to destination row 5, column 7
    source.row32(11)[3] ^= 0x00ffffff;    // odd source row: not sampled by the 2x reduction
    CountingLease partial;
    result = target.blit(source, 4, nullptr, true, partial, [&](uint64_t, const uint16_t *) {});
    assert(result.written == 1 && result.skipped == 5);
    target.expectEquals(legacy(source, 8, 6, 4, nullptr));
}

void halfFloat()
{
    std::vector<uint8_t> table(65536, 0);
    table[0x3c00] = 255;
    table[0x3800] = 128;
    Frame source(4, 3, 8, 8);
    for (uint64_t y = 0; y < 3; y++)
        for (uint64_t x = 0; x < 4; x++) {
            uint16_t *p = source.row16(y) + x * 4;
            p[0] = (x + y) & 1 ? 0x3c00 : 0x3800;
            p[1] = x == 2 ? 0x3c00 : 0;
            p[2] = y == 1 ? 0x3800 : 0;
            p[3] = 0x3c00;
        }
    Target target(4, 3, 4, source.rowBytes);
    CountingLease lease;
    std::vector<uint64_t> sampled;
    NativeGopBlitResult result = target.blit(source, 8, table.data(), false, lease,
                                             [&](uint64_t y, const uint16_t *row) {
                                                 sampled.push_back(y);
                                                 assert(!memcmp(row, source.row16(y), source.width * 8));
                                             });
    assert(result.complete && result.written == 3 && sampled == (std::vector<uint64_t>{0, 1, 2}));
    target.expectEquals(legacy(source, 4, 3, 8, table.data()));
}

void refusedLease()
{
    Frame source(8, 6, 4, 0);
    fill(source, 3);
    Target target(8, 6, 0, source.rowBytes);
    const std::vector<uint8_t> before = target.scanout;
    const std::vector<uint32_t> shadowBefore = target.shadow;
    CountingLease lease;
    lease.refuseAt = 2;
    NativeGopBlitResult result = target.blit(source, 4, nullptr, false, lease, [](uint64_t, const uint16_t *) {});
    assert(!result.complete && result.written == 2 && lease.begins == 2 && lease.ends == 2 && !lease.open);
    for (uint64_t y = 0; y < 6; y++) {
        const bool written = y < 2;
        assert(!memcmp(target.scanout.data() + y * target.rowBytes,
                       written ? reinterpret_cast<uint8_t *>(source.row32(y)) : before.data() + y * target.rowBytes, 32) );
        assert(!memcmp(target.shadow.data() + y * 8, written ? source.row32(y) : shadowBefore.data() + y * 8, 32));
    }
}

void panelGeometry()
{
    // The physical sheng panel is 3048x2032 with a doubled 6096x4064 backing; check a reduced copy of
    // the same 2:1 ratio and stride rules, plus an unchanged second frame.
    Frame source(1524 * 2, 4 * 2, 4, 64);
    fill(source, 11);
    Target target(1524, 4, 32, source.rowBytes);
    CountingLease lease;
    NativeGopBlitResult result = target.blit(source, 4, nullptr, false, lease, [](uint64_t, const uint16_t *) {});
    assert(result.complete && result.written == 4);
    target.expectEquals(legacy(source, 1524, 4, 4, nullptr));
    CountingLease idle;
    result = target.blit(source, 4, nullptr, true, idle, [](uint64_t, const uint16_t *) {});
    assert(result.written == 0 && result.skipped == 4 && idle.begins == 0);
}

}  // namespace

int main()
{
    sameSizeBgra();
    scaledBgra();
    halfFloat();
    refusedLease();
    panelGeometry();
    return 0;
}
