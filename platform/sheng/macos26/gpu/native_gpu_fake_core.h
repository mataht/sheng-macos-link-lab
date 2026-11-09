// SPDX-License-Identifier: GPL-3.0-or-later
// Bookkeeping of the QEMU-only fake A740: what Turnip's Darwin backend asks over the NativeAdreno ABI, answered with memory and fences but
// no GPU. A submission is accepted and counted as finished at once and no command in it is read, so nothing the GPU would write ever
// appears in memory. No IOKit types here, so the host tests run this same code.
#pragma once
#include <stddef.h>
#include <stdint.h>
#include <string.h>
#include "NativeAdrenoABI.h"

enum : uint32_t {
    FakeA740Ok = 0,
    FakeA740NoMemory = 0xe00002bdu,     // kIOReturnNoMemory
    FakeA740NoResources = 0xe00002beu,  // kIOReturnNoResources
    FakeA740BadArgument = 0xe00002c2u,  // kIOReturnBadArgument
    FakeA740Unsupported = 0xe00002c7u,  // kIOReturnUnsupported
    FakeA740Busy = 0xe00002d5u,         // kIOReturnBusy
};

static constexpr uint32_t kFakeA740TunnelMagic = 0x4e544e4cu;  // 'NTNL', Turnip's DW_TUNNEL_MAGIC
static constexpr uint32_t kFakeA740TunnelProbe = 0xffffffffu;

struct FakeA740Entry {
    void *memory;       // the allocator's object; null for an import
    uint64_t bytes, iova;
    uint32_t leases, metadata_bytes;
    bool used, imported, mapped;
    char metadata[NativeAdrenoAbiMetadataBytes];
};

struct FakeA740Hooks {
    void *(*allocate)(uint64_t bytes, uint64_t *actual_bytes);
    void (*release)(void *memory);
    uint64_t (*now)();
};

// The facts of the A740 in UniXNU-sheng's GpuPowerProbe.cpp, gDeviceInfo. The address space is msm's per-process one above 4 GiB.
static constexpr uint64_t kFakeA740VaStart = UINT64_C(1) << 32;
static constexpr uint32_t kFakeA740InputBits = 48, kFakeA740OutputBits = 40;
static constexpr NativeAdrenoAbiDeviceInfo kFakeA740DeviceInfo = {
    0x43050a01u, 3u << 20, UINT64_C(0x1000000), UINT64_C(0x1fffffffff000), kFakeA740VaStart, (UINT64_C(1) << kFakeA740InputBits) - kFakeA740VaStart,
    16, 6, 1, 4, NativeAdrenoDeviceClientIova, 0};

class FakeA740Core {
public:
    void init(FakeA740Entry *entries, uint32_t capacity, const FakeA740Hooks &hooks) {
        entries_ = entries;
        capacity_ = capacity;
        hooks_ = hooks;
    }

    // One external method. A tunnel call carries {magic, method} and then the method's record; any other call uses the selector as the method.
    uint32_t call(uint32_t selector, const void *input, size_t input_size, void *output, size_t output_size) {
        if (input && input_size >= 8) {
            uint32_t header[2];
            memcpy(header, input, sizeof(header));
            if (header[0] == kFakeA740TunnelMagic) {
                if (header[1] == kFakeA740TunnelProbe) {
                    return FakeA740Ok;
                }
                return run(header[1], static_cast<const char *>(input) + 8, input_size - 8, output, output_size);
            }
        }
        return run(selector, input, input_size, output, output_size);
    }

    // The object behind a CPU-map token while a lease is held.
    void *memoryForToken(uint32_t token) const {
        const FakeA740Entry *entry = find(token);
        return entry && !entry->imported && entry->leases ? entry->memory : nullptr;
    }

    void releaseAll() {
        for (uint32_t index = 0; index < capacity_; ++index) {
            if (entries_[index].used && entries_[index].memory) {
                hooks_.release(entries_[index].memory);
            }
            entries_[index] = FakeA740Entry{};
        }
    }

    uint64_t submissions() const { return fence_; }
    uint32_t live() const { return live_; }
    bool firstOfPowerOfTwoSubmission() const { return fence_ && !(fence_ & (fence_ - 1)); }

private:
    FakeA740Entry *entries_ = nullptr;
    uint32_t capacity_ = 0, live_ = 0, next_queue_ = 1;
    FakeA740Hooks hooks_ = {};
    uint64_t fence_ = 0;
    NativeAdrenoAbiSubmit submit_request_;   // a member because it is 3 KiB; the caller holds the client's lock

    FakeA740Entry *find(uint64_t handle) const {
        if (!handle || handle > capacity_ || !entries_[handle - 1].used) {
            return nullptr;
        }
        return &entries_[handle - 1];
    }

    FakeA740Entry *freeEntry(uint64_t *handle) {
        for (uint32_t index = 0; index < capacity_; ++index) {
            if (!entries_[index].used) {
                *handle = index + UINT64_C(1);
                return &entries_[index];
            }
        }
        return nullptr;
    }

    template <typename T>
    static uint32_t answer(void *output, size_t output_size, const T &value) {
        if (!output || output_size != sizeof(T)) {
            return FakeA740BadArgument;
        }
        memcpy(output, &value, sizeof(T));
        return FakeA740Ok;
    }

    template <typename T>
    static bool request(const void *input, size_t input_size, T *value) {
        if (!input || input_size != sizeof(T)) {
            return false;
        }
        memcpy(value, input, sizeof(T));
        return true;
    }

    uint32_t allocBo(const NativeAdrenoAbiBoAlloc &in, void *output, size_t output_size) {
        if (!in.bytes || (in.bytes & 4095) || in.align_log2 < 12 || in.align_log2 > 24 || in.contiguous > 1) {
            return FakeA740BadArgument;
        }
        uint64_t handle = 0, actual = 0;
        FakeA740Entry *entry = freeEntry(&handle);
        if (!entry) {
            return FakeA740NoResources;
        }
        void *memory = hooks_.allocate(in.bytes, &actual);
        if (!memory) {
            return FakeA740NoMemory;
        }
        *entry = FakeA740Entry{};
        entry->memory = memory;
        entry->bytes = actual;
        entry->used = true;
        ++live_;
        return answer(output, output_size, NativeAdrenoAbiBoAllocReply{handle});
    }

    // The caller's own pages are never read or written, since nothing executes, so an import only records that the handle exists.
    uint32_t importBo(const NativeAdrenoAbiBoImport &in, void *output, size_t output_size) {
        if (!in.address || !in.bytes || ((in.address | in.bytes) & 4095)) {
            return FakeA740BadArgument;
        }
        uint64_t handle = 0;
        FakeA740Entry *entry = freeEntry(&handle);
        if (!entry) {
            return FakeA740NoResources;
        }
        *entry = FakeA740Entry{};
        entry->bytes = in.bytes;
        entry->used = entry->imported = true;
        ++live_;
        return answer(output, output_size, NativeAdrenoAbiBoAllocReply{handle});
    }

    uint32_t mapBo(const NativeAdrenoAbiBoMap &in, void *output, size_t output_size) {
        if (!(in.permissions & 1) || (in.permissions & ~3u) || in.writeback > 1 || (in.shareability != 2 && in.shareability != 3) || in.reserved || (in.iova & 4095)) {
            return FakeA740BadArgument;
        }
        FakeA740Entry *entry = find(in.handle);
        if (!entry) {
            return FakeA740BadArgument;
        }
        if (entry->mapped) {
            return FakeA740Busy;
        }
        entry->mapped = true;
        entry->iova = in.iova;
        return answer(output, output_size, NativeAdrenoAbiBoMapReply{in.iova});
    }

    uint32_t freeBo(const NativeAdrenoAbiBoFree &in) {
        FakeA740Entry *entry = find(in.handle);
        if (!entry) {
            return FakeA740BadArgument;
        }
        if (entry->leases) {
            return FakeA740Busy;
        }
        if (entry->memory) {
            hooks_.release(entry->memory);
        }
        *entry = FakeA740Entry{};
        --live_;
        return FakeA740Ok;
    }

    uint32_t submit(const NativeAdrenoAbiSubmit &in, void *output, size_t output_size) {
        if (!in.buffer_count || in.buffer_count > NativeAdrenoAbiMaxBuffers || in.reserved || in.queue >= next_queue_) {
            return FakeA740BadArgument;
        }
        for (uint32_t index = 0; index < in.buffer_count; ++index) {
            const FakeA740Entry *entry = find(in.buffers[index].handle);
            if (!in.buffers[index].bytes || (in.buffers[index].bytes & 3) || !entry || !entry->mapped) {
                return FakeA740BadArgument;
            }
        }
        return answer(output, output_size, NativeAdrenoAbiSubmitReply{++fence_});
    }

    uint32_t cpuMap(const NativeAdrenoAbiBoCpuMap &in, void *output, size_t output_size) {
        FakeA740Entry *entry = find(in.handle);
        if (!entry) {
            return FakeA740BadArgument;
        }
        if (entry->imported) {
            return FakeA740Unsupported;
        }
        ++entry->leases;
        NativeAdrenoAbiBoCpuMapReply reply = {};
        reply.token = static_cast<uint32_t>(in.handle);
        reply.bytes = entry->bytes;
        return answer(output, output_size, reply);
    }

    uint32_t cpuUnmap(const NativeAdrenoAbiBoCpuUnmap &in) {
        FakeA740Entry *entry = find(in.handle);
        if (!entry || in.reserved || in.token != in.handle) {
            return FakeA740BadArgument;
        }
        if (!entry->leases) {
            return FakeA740Busy;
        }
        --entry->leases;
        return FakeA740Ok;
    }

    uint32_t setMetadata(const NativeAdrenoAbiMetadata &in) {
        FakeA740Entry *entry = find(in.handle);
        if (!entry || in.reserved || in.bytes > NativeAdrenoAbiMetadataBytes) {
            return FakeA740BadArgument;
        }
        memcpy(entry->metadata, in.text, in.bytes);
        entry->metadata_bytes = in.bytes;
        return FakeA740Ok;
    }

    uint32_t getMetadata(const NativeAdrenoAbiMetadata &in, void *output, size_t output_size) {
        const FakeA740Entry *entry = find(in.handle);
        if (!entry || in.reserved || in.bytes > NativeAdrenoAbiMetadataBytes || in.bytes < entry->metadata_bytes) {
            return FakeA740BadArgument;
        }
        NativeAdrenoAbiMetadata reply = {};
        reply.handle = in.handle;
        reply.bytes = entry->metadata_bytes;
        memcpy(reply.text, entry->metadata, entry->metadata_bytes);
        return answer(output, output_size, reply);
    }

    uint32_t run(uint32_t method, const void *input, size_t input_size, void *output, size_t output_size) {
        switch (method) {
        case NativeAdrenoMethodQuery: {
            NativeAdrenoAbiParams params = {};
            params.abi_version = NativeAdrenoAbiVersion;
            params.input_bits = kFakeA740InputBits;
            params.output_bits = kFakeA740OutputBits;
            params.flags = NativeAdrenoParamsHostImport;
            params.max_bo_bytes = UINT64_C(1) << 32;
            return input_size ? uint32_t(FakeA740BadArgument) : answer(output, output_size, params);
        }
        case NativeAdrenoMethodGetDeviceInfo:
            return input_size ? uint32_t(FakeA740BadArgument) : answer(output, output_size, kFakeA740DeviceInfo);
        case NativeAdrenoMethodBoAlloc: {
            NativeAdrenoAbiBoAlloc in;
            return request(input, input_size, &in) ? allocBo(in, output, output_size) : uint32_t(FakeA740BadArgument);
        }
        case NativeAdrenoMethodBoImport: {
            NativeAdrenoAbiBoImport in;
            return request(input, input_size, &in) ? importBo(in, output, output_size) : uint32_t(FakeA740BadArgument);
        }
        case NativeAdrenoMethodBoMap: {
            NativeAdrenoAbiBoMap in;
            return request(input, input_size, &in) ? mapBo(in, output, output_size) : uint32_t(FakeA740BadArgument);
        }
        case NativeAdrenoMethodBoFree: {
            NativeAdrenoAbiBoFree in;
            return request(input, input_size, &in) && !output_size ? freeBo(in) : uint32_t(FakeA740BadArgument);
        }
        case NativeAdrenoMethodSubmit:
            return request(input, input_size, &submit_request_) ? submit(submit_request_, output, output_size) : uint32_t(FakeA740BadArgument);
        case NativeAdrenoMethodWaitFence: {
            NativeAdrenoAbiWaitFence in;
            return request(input, input_size, &in) && in.fence && !output_size ? uint32_t(FakeA740Ok) : uint32_t(FakeA740BadArgument);
        }
        case NativeAdrenoMethodGetTimestamp:
            return input_size ? uint32_t(FakeA740BadArgument) : answer(output, output_size, NativeAdrenoAbiTimestamp{hooks_.now()});
        case NativeAdrenoMethodGetStatus:
            return input_size ? uint32_t(FakeA740BadArgument) : answer(output, output_size, NativeAdrenoAbiStatus{0, 0});
        case NativeAdrenoMethodBoCpuMap: {
            NativeAdrenoAbiBoCpuMap in;
            return request(input, input_size, &in) ? cpuMap(in, output, output_size) : uint32_t(FakeA740BadArgument);
        }
        case NativeAdrenoMethodBoCpuUnmap: {
            NativeAdrenoAbiBoCpuUnmap in;
            return request(input, input_size, &in) && !output_size ? cpuUnmap(in) : uint32_t(FakeA740BadArgument);
        }
        case NativeAdrenoMethodSetMetadata: {
            NativeAdrenoAbiMetadata in;
            return request(input, input_size, &in) && !output_size ? setMetadata(in) : uint32_t(FakeA740BadArgument);
        }
        case NativeAdrenoMethodGetMetadata: {
            NativeAdrenoAbiMetadata in;
            return request(input, input_size, &in) ? getMetadata(in, output, output_size) : uint32_t(FakeA740BadArgument);
        }
        case NativeAdrenoMethodCreateQueue: {
            NativeAdrenoAbiQueue in;
            return request(input, input_size, &in) ? answer(output, output_size, NativeAdrenoAbiQueueReply{next_queue_++}) : uint32_t(FakeA740BadArgument);
        }
        case NativeAdrenoMethodCloseQueue: {
            NativeAdrenoAbiQueue in;
            return request(input, input_size, &in) && in.queue && in.queue < next_queue_ && !output_size ? uint32_t(FakeA740Ok) : uint32_t(FakeA740BadArgument);
        }
        case NativeAdrenoMethodSetPriority: {
            NativeAdrenoAbiPriority in;
            return request(input, input_size, &in) && !output_size ? uint32_t(FakeA740Ok) : uint32_t(FakeA740BadArgument);
        }
        case NativeAdrenoMethodNote: {
            NativeAdrenoAbiNote in;
            return request(input, input_size, &in) && !output_size ? uint32_t(FakeA740Ok) : uint32_t(FakeA740BadArgument);
        }
        default:
            return FakeA740Unsupported;
        }
    }
};
