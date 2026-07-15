/**
 * Copyright 2026 Goncalo Carvalho
 *
 * Licensed under the Apache License, Version 2.0 (the "License");
 * you may not use this file except in compliance with the License.
 */

#ifndef _HOTSTUFF_BLOCK_DELIVERY_ORCHESTRATION_H
#define _HOTSTUFF_BLOCK_DELIVERY_ORCHESTRATION_H

#include <cstddef>
#include <exception>
#include <functional>
#include <memory>
#include <vector>

#include "hotstuff/block_delivery_completion.h"

namespace hotstuff
{

struct BlockDeliveryAsyncPlan
{
    std::function<promise_t()> fetch_block;
    std::function<promise_t(const block_t &)> verify_block;
    std::function<promise_t(const block_t &)> fetch_qc_reference;
    std::function<std::vector<promise_t>(const block_t &)> deliver_parents;
    std::function<bool(const block_t &)> deliver_core;
};

using ExternalBlockDelivery = std::function<bool(const block_t &)>;

namespace block_delivery_orchestration_detail
{
    class OrchestratorState;
}

/** Owns the complete production lifecycle for block delivery. */
class BlockDeliveryOrchestrator
{
public:
    BlockDeliveryOrchestrator();
    ~BlockDeliveryOrchestrator();

    BlockDeliveryOrchestrator(const BlockDeliveryOrchestrator &) = delete;
    BlockDeliveryOrchestrator &operator=(
        const BlockDeliveryOrchestrator &) = delete;
    BlockDeliveryOrchestrator(BlockDeliveryOrchestrator &&) = delete;
    BlockDeliveryOrchestrator &operator=(BlockDeliveryOrchestrator &&) = delete;

    promise_t async_delivery(const uint256_t &block_hash,
                             BlockDeliveryTimingHooks timing,
                             BlockDeliveryAsyncPlan plan);
    bool external_delivery(const uint256_t &block_hash,
                           const block_t &block,
                           ExternalBlockDelivery deliver_core);
    void cancel(std::exception_ptr reason) noexcept;
    bool contains(const uint256_t &block_hash) const noexcept;
    std::size_t size() const noexcept;
    bool is_cancelled() const noexcept;

private:
    std::shared_ptr<
        block_delivery_orchestration_detail::OrchestratorState> state_;
};

} // namespace hotstuff

#endif
