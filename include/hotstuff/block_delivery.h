/**
 * Copyright 2026 Goncalo Carvalho
 *
 * Licensed under the Apache License, Version 2.0 (the "License");
 * you may not use this file except in compliance with the License.
 */

#ifndef _HOTSTUFF_BLOCK_DELIVERY_H
#define _HOTSTUFF_BLOCK_DELIVERY_H

#include <cstddef>
#include <exception>
#include <functional>
#include <memory>

#include "hotstuff/entity.h"
#include "hotstuff/promise.hpp"

namespace hotstuff
{

class HotStuffBase;
class BlockDeliveryOrchestrator;

namespace block_delivery_detail
{
    class LifecycleState;
    class AttemptState;
}

struct BlockDeliveryTimingHooks
{
    std::function<double()> elapsed_seconds;
    std::function<void(double)> record_success;
};

/** A weak, generation-bound handle for one block-delivery attempt. */
class BlockDeliveryTerminal
{
public:
    BlockDeliveryTerminal() = default;

    void succeed(const block_t &block) const noexcept;
    void fail(std::exception_ptr reason) const noexcept;

private:
    friend class BlockDeliveryLifecycle;
    friend class BlockDeliveryOrchestrator;
    friend class HotStuffBase;

    BlockDeliveryTerminal(
        std::weak_ptr<block_delivery_detail::LifecycleState> lifecycle,
        std::weak_ptr<block_delivery_detail::AttemptState> attempt) noexcept;
    void retain_attempt_owner(std::shared_ptr<void> owner) const noexcept;

    std::weak_ptr<block_delivery_detail::LifecycleState> lifecycle_;
    std::weak_ptr<block_delivery_detail::AttemptState> attempt_;
};

using StartBlockDeliveryAttempt =
    std::function<void(BlockDeliveryTerminal)>;

/**
 * Owns one shared completion promise per block hash.
 *
 * Terminal handles refer weakly to the exact attempt they were created for.
 * Settling removes that attempt before invoking promise callbacks, allowing a
 * callback to start a fresh attempt for the same hash safely.
 */
class BlockDeliveryLifecycle
{
public:
    BlockDeliveryLifecycle();
    ~BlockDeliveryLifecycle();

    BlockDeliveryLifecycle(const BlockDeliveryLifecycle &) = delete;
    BlockDeliveryLifecycle &operator=(const BlockDeliveryLifecycle &) = delete;
    BlockDeliveryLifecycle(BlockDeliveryLifecycle &&) = delete;
    BlockDeliveryLifecycle &operator=(BlockDeliveryLifecycle &&) = delete;

    promise_t request(const uint256_t &block_hash,
                      StartBlockDeliveryAttempt start_attempt);
    promise_t request(const uint256_t &block_hash,
                      StartBlockDeliveryAttempt start_attempt,
                      BlockDeliveryTimingHooks timing_hooks);
    /** Settles the current generation when another delivery path wins. */
    bool succeed_pending(const uint256_t &block_hash,
                         const block_t &block) noexcept;
    bool fail_pending(const uint256_t &block_hash,
                      std::exception_ptr reason) noexcept;
    bool contains(const uint256_t &block_hash) const noexcept;
    std::size_t size() const noexcept;

private:
    friend class BlockDeliveryOrchestrator;

    void fail_all_pending(std::exception_ptr reason) noexcept;
    std::shared_ptr<block_delivery_detail::LifecycleState> state_;
};

} // namespace hotstuff

#endif
