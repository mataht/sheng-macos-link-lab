// SPDX-License-Identifier: GPL-3.0-or-later
// Host test of the fake A740's bookkeeping (native_gpu_fake_core.h), with Turnip's own calling sequences.
#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <vector>
#include "native_gpu_fake_core.h"

static int live_allocations;

static void *allocate(uint64_t bytes, uint64_t *actual)
{
    *actual = (bytes + 16383) & ~UINT64_C(16383);
    ++live_allocations;
    return malloc(static_cast<size_t>(*actual));
}

static void release(void *memory)
{
    --live_allocations;
    free(memory);
}

static uint64_t now()
{
    return 1234;
}

#define CHECK(condition) do { if (!(condition)) { printf("line %d: %s\n", __LINE__, #condition); return 1; } } while (0)

template <typename Out>
static uint32_t tunnel(FakeA740Core &core, uint32_t method, const void *in, size_t in_size, Out *out)
{
    std::vector<uint8_t> buffer(8 + in_size);
    const uint32_t header[2] = {kFakeA740TunnelMagic, method};
    memcpy(buffer.data(), header, sizeof(header));
    if (in_size) {
        memcpy(buffer.data() + 8, in, in_size);
    }
    return core.call(7, buffer.data(), buffer.size(), out, sizeof(Out));
}

static uint32_t tunnelVoid(FakeA740Core &core, uint32_t method, const void *in, size_t in_size)
{
    std::vector<uint8_t> buffer(8 + in_size);
    const uint32_t header[2] = {kFakeA740TunnelMagic, method};
    memcpy(buffer.data(), header, sizeof(header));
    memcpy(buffer.data() + 8, in, in_size);
    return core.call(7, buffer.data(), buffer.size(), nullptr, 0);
}

int main()
{
    std::vector<FakeA740Entry> entries(8);
    static FakeA740Core core;
    core.init(entries.data(), 8, FakeA740Hooks{allocate, release, now});

    // Turnip's selector probe: {magic, 0xffffffff} succeeds for any selector.
    const uint32_t probe[2] = {kFakeA740TunnelMagic, kFakeA740TunnelProbe};
    CHECK(core.call(250, probe, sizeof(probe), nullptr, 0) == FakeA740Ok);

    NativeAdrenoAbiParams params = {};
    CHECK(tunnel(core, NativeAdrenoMethodQuery, nullptr, 0, &params) == FakeA740Ok);
    CHECK(params.abi_version == NativeAdrenoAbiVersion && (params.flags & NativeAdrenoParamsHostImport) && params.input_bits > 32);

    NativeAdrenoAbiDeviceInfo info = {};
    CHECK(tunnel(core, NativeAdrenoMethodGetDeviceInfo, nullptr, 0, &info) == FakeA740Ok);
    CHECK(info.chip_id == 0x43050a01 && info.gmem_bytes == 3u << 20 && (info.flags & NativeAdrenoDeviceClientIova) && info.va_start == UINT64_C(1) << 32 && info.va_size);

    // Allocate, map, CPU-map, submit, unmap, free: a buffer's whole life.
    const NativeAdrenoAbiBoAlloc alloc = {8192, 12, 0};
    NativeAdrenoAbiBoAllocReply handle = {};
    CHECK(tunnel(core, NativeAdrenoMethodBoAlloc, &alloc, sizeof(alloc), &handle) == FakeA740Ok && handle.handle && live_allocations == 1);

    const NativeAdrenoAbiBoCpuMap cpuMap = {handle.handle};
    NativeAdrenoAbiBoCpuMapReply mapped = {};
    CHECK(core.memoryForToken(static_cast<uint32_t>(handle.handle)) == nullptr);   // no lease yet
    CHECK(tunnel(core, NativeAdrenoMethodBoCpuMap, &cpuMap, sizeof(cpuMap), &mapped) == FakeA740Ok);
    CHECK(mapped.bytes == 16384 && core.memoryForToken(mapped.token));

    NativeAdrenoAbiSubmit submit = {};
    submit.buffer_count = 1;
    submit.buffers[0] = {handle.handle, UINT64_C(0x200000000), 4};
    NativeAdrenoAbiSubmitReply fence = {};
    CHECK(tunnel(core, NativeAdrenoMethodSubmit, &submit, sizeof(submit), &fence) == FakeA740BadArgument);   // not mapped into the GPU yet

    const NativeAdrenoAbiBoMap map = {handle.handle, UINT64_C(0x200000000), 3, 0, 3, 0};
    NativeAdrenoAbiBoMapReply iova = {};
    CHECK(tunnel(core, NativeAdrenoMethodBoMap, &map, sizeof(map), &iova) == FakeA740Ok && iova.iova == map.iova);
    CHECK(tunnel(core, NativeAdrenoMethodBoMap, &map, sizeof(map), &iova) == FakeA740Busy);

    CHECK(tunnel(core, NativeAdrenoMethodSubmit, &submit, sizeof(submit), &fence) == FakeA740Ok && fence.fence == 1);
    CHECK(core.firstOfPowerOfTwoSubmission());
    CHECK(tunnel(core, NativeAdrenoMethodSubmit, &submit, sizeof(submit), &fence) == FakeA740Ok && fence.fence == 2);
    CHECK(tunnel(core, NativeAdrenoMethodSubmit, &submit, sizeof(submit), &fence) == FakeA740Ok && fence.fence == 3 && !core.firstOfPowerOfTwoSubmission());

    const NativeAdrenoAbiWaitFence wait = {fence.fence, 100000000};
    CHECK(tunnelVoid(core, NativeAdrenoMethodWaitFence, &wait, sizeof(wait)) == FakeA740Ok);
    const NativeAdrenoAbiWaitFence zero = {0, 0};
    CHECK(tunnelVoid(core, NativeAdrenoMethodWaitFence, &zero, sizeof(zero)) == FakeA740BadArgument);

    NativeAdrenoAbiTimestamp stamp = {};
    CHECK(tunnel(core, NativeAdrenoMethodGetTimestamp, nullptr, 0, &stamp) == FakeA740Ok && stamp.ticks == 1234);
    NativeAdrenoAbiStatus status = {9, 9};
    CHECK(tunnel(core, NativeAdrenoMethodGetStatus, nullptr, 0, &status) == FakeA740Ok && !status.faults && !status.suspends);

    NativeAdrenoAbiMetadata metadata = {};
    metadata.handle = handle.handle;
    metadata.bytes = 3;
    memcpy(metadata.text, "abc", 3);
    CHECK(tunnelVoid(core, NativeAdrenoMethodSetMetadata, &metadata, sizeof(metadata)) == FakeA740Ok);
    NativeAdrenoAbiMetadata asked = {};
    asked.handle = handle.handle;
    asked.bytes = 64;
    NativeAdrenoAbiMetadata got = {};
    CHECK(tunnel(core, NativeAdrenoMethodGetMetadata, &asked, sizeof(asked), &got) == FakeA740Ok && got.bytes == 3 && !memcmp(got.text, "abc", 3));

    const NativeAdrenoAbiBoFree freeBo = {handle.handle};
    CHECK(tunnelVoid(core, NativeAdrenoMethodBoFree, &freeBo, sizeof(freeBo)) == FakeA740Busy);   // a CPU view is still leased
    const NativeAdrenoAbiBoCpuUnmap unmapWrong = {handle.handle, mapped.token + 1, 0};
    CHECK(tunnelVoid(core, NativeAdrenoMethodBoCpuUnmap, &unmapWrong, sizeof(unmapWrong)) == FakeA740BadArgument);
    const NativeAdrenoAbiBoCpuUnmap unmap = {handle.handle, mapped.token, 0};
    CHECK(tunnelVoid(core, NativeAdrenoMethodBoCpuUnmap, &unmap, sizeof(unmap)) == FakeA740Ok);
    CHECK(tunnelVoid(core, NativeAdrenoMethodBoFree, &freeBo, sizeof(freeBo)) == FakeA740Ok && live_allocations == 0 && core.live() == 0);
    CHECK(tunnelVoid(core, NativeAdrenoMethodBoFree, &freeBo, sizeof(freeBo)) == FakeA740BadArgument);

    // Queues: a created one is usable by a submission, an unknown one is not.
    const NativeAdrenoAbiQueue none = {0};
    NativeAdrenoAbiQueueReply queue = {};
    CHECK(tunnel(core, NativeAdrenoMethodCreateQueue, &none, sizeof(none), &queue) == FakeA740Ok && queue.queue == 1);
    const NativeAdrenoAbiQueue created = {queue.queue};
    CHECK(tunnelVoid(core, NativeAdrenoMethodCloseQueue, &created, sizeof(created)) == FakeA740Ok);
    CHECK(tunnelVoid(core, NativeAdrenoMethodCloseQueue, &none, sizeof(none)) == FakeA740BadArgument);

    // An import holds no memory of ours and has no CPU view.
    const NativeAdrenoAbiBoImport import = {UINT64_C(0x100004000), 16384};
    NativeAdrenoAbiBoAllocReply imported = {};
    CHECK(tunnel(core, NativeAdrenoMethodBoImport, &import, sizeof(import), &imported) == FakeA740Ok && live_allocations == 0);
    const NativeAdrenoAbiBoCpuMap importMap = {imported.handle};
    CHECK(tunnel(core, NativeAdrenoMethodBoCpuMap, &importMap, sizeof(importMap), &mapped) == FakeA740Unsupported);
    const NativeAdrenoAbiBoFree freeImport = {imported.handle};
    CHECK(tunnelVoid(core, NativeAdrenoMethodBoFree, &freeImport, sizeof(freeImport)) == FakeA740Ok);

    // Sizes and values the real driver refuses are refused here too.
    const NativeAdrenoAbiBoAlloc odd = {4097, 12, 0};
    CHECK(tunnel(core, NativeAdrenoMethodBoAlloc, &odd, sizeof(odd), &handle) == FakeA740BadArgument);
    CHECK(tunnel(core, NativeAdrenoMethodBoAlloc, &alloc, sizeof(alloc) - 1, &handle) == FakeA740BadArgument);
    CHECK(tunnel(core, 99, nullptr, 0, &handle) == FakeA740Unsupported);
    uint8_t tooSmall[4];
    CHECK(core.call(NativeAdrenoMethodQuery, nullptr, 0, tooSmall, sizeof(tooSmall)) == FakeA740BadArgument);

    // A selector without the tunnel header is the method itself (the unsandboxed form).
    CHECK(core.call(NativeAdrenoMethodQuery, nullptr, 0, &params, sizeof(params)) == FakeA740Ok);

    // When the table is full the caller is told; releaseAll gives everything back.
    for (unsigned index = 0; index < 8; ++index) {
        CHECK(tunnel(core, NativeAdrenoMethodBoAlloc, &alloc, sizeof(alloc), &handle) == FakeA740Ok);
    }
    CHECK(tunnel(core, NativeAdrenoMethodBoAlloc, &alloc, sizeof(alloc), &handle) == FakeA740NoResources && live_allocations == 8);
    core.releaseAll();
    CHECK(live_allocations == 0);

    puts("ok");
    return 0;
}
