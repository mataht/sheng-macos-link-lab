// SPDX-License-Identifier: GPL-3.0-or-later
// Host test of upstream's staged GPU probe (GpuPowerBoard.h), compiled as it is: stage 1 must write nothing, and must not read a block
// whose clock or power domain the previous read showed off, because such a read can hang the bus on the device.
#include <stdint.h>
#include <stdio.h>
#include "GpuPowerBoard.h"

static unsigned reads[5], writes, write_window, write_offset;
static uint32_t cfg_ahb, cx, gx, smmu_vote;
static uint64_t noise;

static uint32_t next_noise(void)
{
    noise = noise * 6364136223846793005ULL + 1442695040888963407ULL;
    return (uint32_t)(noise >> 32);
}

static int read32(void *cookie, unsigned window, uint32_t offset, uint32_t *out)
{
    (void)cookie;
    reads[window]++;
    if (noise) {
        *out = next_noise();
    } else if (window == GPB_WIN_GCC_GPU && offset == GPB_GCC_GPU_CFG_AHB) {
        *out = cfg_ahb;
    } else if (window == GPB_WIN_GPUCC && offset == GPB_CC_CX_GDSCR) {
        *out = cx;
    } else if (window == GPB_WIN_GPUCC && offset == GPB_CC_GX_GDSCR) {
        *out = gx;
    } else if (window == GPB_WIN_GPUCC && offset == GPB_CC_SMMU_VOTE_CLK) {
        *out = smmu_vote;
    } else {
        *out = 0;
    }
    return 0;
}

static int write32(void *cookie, unsigned window, uint32_t offset, uint32_t value)
{
    (void)cookie;
    (void)value;
    writes++;
    write_window = window;
    write_offset = offset;
    return 0;
}

static struct gpb_result run(unsigned mode, uint32_t ahb, uint32_t cx_word, uint32_t gx_word, uint32_t vote)
{
    const struct gpb_io io = {0, read32, write32, 0};
    struct gpb_result result;
    for (unsigned window = 0; window < 5; ++window) {
        reads[window] = 0;
    }
    writes = 0;
    cfg_ahb = ahb;
    cx = cx_word;
    gx = gx_word;
    smmu_vote = vote;
    gpb_run(&io, mode, 0x1f, &result);
    return result;
}

#define CHECK(condition) do { if (!(condition)) { printf("line %d: %s\n", __LINE__, #condition); return 1; } } while (0)

int main(void)
{
    const uint32_t on = GPB_GDSC_PWR_ON, off = 0, clock_off = GPB_CBCR_CLK_OFF;
    struct gpb_result r;

    r = run(0, GPB_CBCR_ENABLE, on, on, 0);
    CHECK(!reads[0] && !reads[1] && !reads[2] && !reads[3] && !reads[4] && !writes);

    r = run(1, 0, on, on, 0);    // cfg AHB clock not enabled: GPUCC and everything behind it stays untouched
    CHECK(reads[GPB_WIN_GCC_GPU] && !reads[GPB_WIN_GPUCC] && !reads[GPB_WIN_SMMU] && !reads[GPB_WIN_GPU] && !writes);
    CHECK(r.gcc == GPB_DONE && r.cc == GPB_SKIPPED_PREREQ && r.smmu == GPB_SKIPPED_PREREQ && r.gpu == GPB_SKIPPED_PREREQ);

    r = run(1, GPB_CBCR_ENABLE, off, on, 0);    // CX off: neither the SMMU nor the GPU window
    CHECK(reads[GPB_WIN_GPUCC] && !reads[GPB_WIN_SMMU] && !reads[GPB_WIN_GPU] && !writes);
    CHECK(r.cc == GPB_DONE && r.smmu == GPB_SKIPPED_PREREQ && r.gpu == GPB_SKIPPED_PREREQ);

    r = run(1, GPB_CBCR_ENABLE, on, off, clock_off);    // SMMU vote clock off, GX off
    CHECK(!reads[GPB_WIN_SMMU] && !reads[GPB_WIN_GPU] && !writes);

    r = run(1, GPB_CBCR_ENABLE, on, off, 0);    // SMMU readable, GX off: the GPU window stays untouched
    CHECK(reads[GPB_WIN_SMMU] && !reads[GPB_WIN_GPU] && !writes);
    CHECK(r.smmu == GPB_DONE && r.gpu == GPB_SKIPPED_PREREQ);

    r = run(1, GPB_CBCR_ENABLE, on, on, 0);    // everything on: all windows read, still no write
    CHECK(reads[GPB_WIN_GPU] == 1 && !writes && r.gpu == GPB_DONE);

    noise = 0x5eed;
    for (unsigned round = 0; round < 200000; ++round) {
        r = run(1, 0, 0, 0, 0);
        CHECK(!writes);
    }
    noise = 0;

    r = run(2, 0, off, off, 0);    // stage 2 writes exactly the one enable bit
    CHECK(writes == 1 && write_window == GPB_WIN_GCC_GPU && write_offset == GPB_GCC_GPU_CFG_AHB && r.enabled_ahb == 1);

    r = run(2, GPB_CBCR_ENABLE, off, off, 0);    // already enabled: stage 2 writes nothing
    CHECK(!writes);

    puts("ok");
    return 0;
}
