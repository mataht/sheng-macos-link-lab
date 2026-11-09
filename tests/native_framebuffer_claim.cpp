// SPDX-License-Identifier: GPL-3.0-or-later
// sheng_fb_claim_gui_bounded: retries while the boot panel holds the writer, stops at once after a panic.
#include <assert.h>
#include <stdint.h>
#include "../platform/sheng/macos26/log/framebuffer_owner.h"

struct sheng_framebuffer_owner sheng_framebuffer{};
static unsigned waits;
static uint64_t saved_boot;

static void wait_and_release_after_three(void)
{
    if (++waits == 3)
        sheng_fb_end(saved_boot);
}

static void wait_only(void) { ++waits; }

int main()
{
    // Immediate success.
    assert(sheng_fb_claim_gui_bounded(5, wait_only));
    assert(waits == 0 && sheng_framebuffer.owner == SHENG_FB_GUI);

    // The boot panel holds the writer for three waits, then the claim succeeds.
    sheng_framebuffer = {};
    waits = 0;
    assert(sheng_fb_begin(SHENG_FB_BOOT, &saved_boot));
    assert(sheng_fb_claim_gui_bounded(10, wait_and_release_after_three));
    assert(waits == 3 && sheng_framebuffer.owner == SHENG_FB_GUI);

    // Attempts run out while the writer stays held: the claim gives up after the bound.
    sheng_framebuffer = {};
    waits = 0;
    assert(sheng_fb_begin(SHENG_FB_BOOT, &saved_boot));
    assert(!sheng_fb_claim_gui_bounded(4, wait_only));
    assert(waits == 4 && sheng_framebuffer.owner == SHENG_FB_BOOT);
    sheng_fb_end(saved_boot);

    // After a panic the owner never returns to boot, so no wait happens.
    sheng_framebuffer = {};
    waits = 0;
    sheng_fb_enter_panic();
    assert(!sheng_fb_claim_gui_bounded(100, wait_only));
    assert(waits == 0 && sheng_framebuffer.owner == SHENG_FB_PANIC);
    return 0;
}
