// SPDX-License-Identifier: MIT
#pragma once

#include <arm/caches_internal.h>
#include <libkern/crypto/sha2.h>
#include <sys/csr.h>
#include <vm/sheng_recovery_ui.h>
#include <vm/sheng_recovery_ui_pages.h>

// Keep the VM translation unit independent of the BSD vnode header's type namespace.
extern const char *vnode_getname(struct vnode *);
extern void vnode_putname(const char *);

TUNABLE(uint32_t, sheng_recovery_ui_enabled, "sheng-recovery-ui", 0);
// Same engine for the installed full system: its SystemOS cache has different pages, so it gets its own pinned
// table. Exactly one table may be enabled; both or a value other than 1 stop at the first matching page.
TUNABLE(uint32_t, sheng_full_ui_enabled, "sheng-full-ui", 0);
volatile uint32_t sheng_recovery_ui_pages_applied;
volatile uint32_t sheng_recovery_ui_page_mask;

static void
sheng_recovery_ui_digest(const void *data, size_t size, uint8_t result[32])
{
    SHA256_CTX context;
    SHA256_Init(&context);
    SHA256_Update(&context, data, size);
    SHA256_Final(result, &context);
}

#include <vm/sheng_language_pages_kernel.h>

static void
sheng_recovery_ui_page(vm_page_t page, struct vnode *vnode, uint64_t offset, bool original_validated)
{
    if (!sheng_recovery_ui_enabled && !sheng_full_ui_enabled)
        return;
    const bool full = sheng_full_ui_enabled != 0;
    if (sheng_recovery_ui_enabled == 1 && !full)
        sheng_language_trace_page(page, vnode, offset, original_validated);
    const struct sheng_ui_page *pages = full ? sheng_full_ui_pages : sheng_ui_pages;
    const size_t count = full ? sizeof(sheng_full_ui_pages) / sizeof(sheng_full_ui_pages[0])
                              : sizeof(sheng_ui_pages) / sizeof(sheng_ui_pages[0]);
    const bool one_table = full ? sheng_full_ui_enabled == 1 && !sheng_recovery_ui_enabled
                                : sheng_recovery_ui_enabled == 1;
    bool candidate = false;
    for (size_t i = 0; i < count; i++)
        candidate |= offset == pages[i].file_offset;
    if (!candidate)
        return;
    const char *name = vnode_getname(vnode);
    if (!name)
        return;
    for (size_t i = 0; i < count; i++) {
        const struct sheng_ui_page *rule = &pages[i];
        if (offset != rule->file_offset || strcmp(name, rule->file))
            continue;
        vnode_putname(name);
        // Never turn a failed signature, writable page, or production boot into an accepted image.
        if (!one_table || PAGE_SIZE != SHENG_UI_PAGE_BYTES ||
            csr_check(CSR_ALLOW_UNAUTHENTICATED_ROOT) != 0 || !original_validated ||
            page->vmp_cs_validated != VMP_CS_ALL_TRUE || page->vmp_cs_tainted != VMP_CS_ALL_FALSE ||
            page->vmp_cs_nx != VMP_CS_ALL_FALSE || page->vmp_dirty || page->vmp_wpmapped)
            panic("SHENG: %s UI policy %zu requires an authenticated immutable development page",
                full ? "Full-system" : "Recovery", i);
        // The validation mapping can be read-only; write through the owned busy physical page instead.
        uint8_t *bytes = (uint8_t *)phystokv((pmap_paddr_t)VM_PAGE_GET_PHYS_PAGE(page) << PAGE_SHIFT);
        int result = sheng_ui_apply_page(bytes, PAGE_SIZE, rule, sheng_recovery_ui_digest);
        if (result < 0)
            panic("SHENG: %s UI policy %zu page digest or instructions differ", full ? "Full-system" : "Recovery", i);
        if (result) {
            // Use the same physical-page coherence primitive as UniXNU's native text fixups.
            cache_sync_page(VM_PAGE_GET_PHYS_PAGE(page));
            OSAddAtomic(1, (volatile SInt32 *)&sheng_recovery_ui_pages_applied);
            OSBitOrAtomic((uint32_t)1 << i, &sheng_recovery_ui_page_mask);
            printf("SHENG: %s UI policy %zu applied - authenticated memory only\n", full ? "Full-system" : "Recovery", i);
        }
        return;
    }
    vnode_putname(name);
}
