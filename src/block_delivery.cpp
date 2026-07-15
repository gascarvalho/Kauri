/**
 * Copyright 2026 Goncalo Carvalho
 *
 * Licensed under the Apache License, Version 2.0 (the "License");
 * you may not use this file except in compliance with the License.
 */

#include "hotstuff/block_delivery.h"

#include <optional>
#include <unordered_map>
#include <utility>
#include <vector>

namespace hotstuff
{

namespace block_delivery_detail
{

class AttemptState
{
public:
    AttemptState(const uint256_t &hash,
                 BlockDeliveryTimingHooks hooks)
        : block_hash(hash), timing(std::move(hooks))
    {}

    uint256_t block_hash;
    promise_t completion;
    BlockDeliveryTimingHooks timing;
    std::shared_ptr<void> owner;
};

class LifecycleState
{
public:
    using AttemptMap =
        std::unordered_map<uint256_t, std::shared_ptr<AttemptState>>;

    void succeed(const std::shared_ptr<AttemptState> &attempt,
                 const block_t &block) noexcept
    {
        const auto it = find(attempt);
        if (it == attempts.end())
            return;

        auto timing = std::move(attempt->timing);
        std::optional<double> elapsed;
        if (timing.elapsed_seconds && timing.record_success)
        {
            try
            {
                elapsed = timing.elapsed_seconds();
            }
            catch (...)
            {
                timing.record_success = {};
            }
        }

        auto completion = attempt->completion;
        attempt->owner.reset();
        attempts.erase(it);
        if (elapsed && timing.record_success)
        {
            try
            {
                timing.record_success(*elapsed);
            }
            catch (...)
            {
                // Delivery must still settle if metrics publication fails.
            }
        }
        try
        {
            completion.resolve(block);
        }
        catch (...)
        {
            // Promise callbacks are event-loop-owned. A throwing observer must
            // not escape a terminal delivery signal.
        }
    }

    void fail(const std::shared_ptr<AttemptState> &attempt,
              std::exception_ptr reason) noexcept
    {
        const auto it = find(attempt);
        if (it == attempts.end())
            return;

        auto completion = attempt->completion;
        attempt->owner.reset();
        attempts.erase(it);
        try
        {
            completion.reject(std::move(reason));
        }
        catch (...)
        {
            // The context is already erased, so retries remain safe even when
            // a rejected-promise observer throws.
        }
    }

    bool succeed_pending(const uint256_t &block_hash,
                         const block_t &block) noexcept
    {
        const auto it = attempts.find(block_hash);
        if (it == attempts.end())
            return false;

        const auto attempt = it->second;
        succeed(attempt, block);
        return true;
    }

    bool fail_pending(const uint256_t &block_hash,
                      std::exception_ptr reason) noexcept
    {
        const auto it = attempts.find(block_hash);
        if (it == attempts.end())
            return false;

        const auto attempt = it->second;
        fail(attempt, std::move(reason));
        return true;
    }

    void fail_all(std::exception_ptr reason) noexcept
    {
        std::vector<promise_t> completions;
        completions.reserve(attempts.size());
        for (auto &entry : attempts)
        {
            completions.push_back(entry.second->completion);
            entry.second->owner.reset();
        }
        attempts.clear();

        for (const auto &completion : completions)
        {
            try
            {
                completion.reject(reason);
            }
            catch (...)
            {
                // Cancellation has already erased every generation. A
                // throwing observer cannot retain or resurrect one of them.
            }
        }
    }

    AttemptMap attempts;

    void retain(const std::shared_ptr<AttemptState> &attempt,
                std::shared_ptr<void> owner) noexcept
    {
        if (find(attempt) != attempts.end())
            attempt->owner = std::move(owner);
    }

private:
    AttemptMap::iterator find(
        const std::shared_ptr<AttemptState> &attempt) noexcept
    {
        const auto it = attempts.find(attempt->block_hash);
        if (it == attempts.end() || it->second != attempt)
            return attempts.end();
        return it;
    }
};

} // namespace block_delivery_detail

BlockDeliveryTerminal::BlockDeliveryTerminal(
    std::weak_ptr<block_delivery_detail::LifecycleState> lifecycle,
    std::weak_ptr<block_delivery_detail::AttemptState> attempt) noexcept
    : lifecycle_(std::move(lifecycle)), attempt_(std::move(attempt))
{}

void BlockDeliveryTerminal::succeed(const block_t &block) const noexcept
{
    const auto lifecycle = lifecycle_.lock();
    const auto attempt = attempt_.lock();
    if (lifecycle && attempt)
        lifecycle->succeed(attempt, block);
}

void BlockDeliveryTerminal::fail(std::exception_ptr reason) const noexcept
{
    const auto lifecycle = lifecycle_.lock();
    const auto attempt = attempt_.lock();
    if (lifecycle && attempt)
        lifecycle->fail(attempt, std::move(reason));
}

void BlockDeliveryTerminal::retain_attempt_owner(
    std::shared_ptr<void> owner) const noexcept
{
    const auto lifecycle = lifecycle_.lock();
    const auto attempt = attempt_.lock();
    if (lifecycle && attempt)
        lifecycle->retain(attempt, std::move(owner));
}

BlockDeliveryLifecycle::BlockDeliveryLifecycle()
    : state_(std::make_shared<block_delivery_detail::LifecycleState>())
{}

BlockDeliveryLifecycle::~BlockDeliveryLifecycle() = default;

promise_t BlockDeliveryLifecycle::request(
    const uint256_t &block_hash,
    StartBlockDeliveryAttempt start_attempt)
{
    return request(block_hash, std::move(start_attempt), {});
}

promise_t BlockDeliveryLifecycle::request(
    const uint256_t &block_hash,
    StartBlockDeliveryAttempt start_attempt,
    BlockDeliveryTimingHooks timing_hooks)
{
    const auto existing = state_->attempts.find(block_hash);
    if (existing != state_->attempts.end())
        return existing->second->completion;

    const auto attempt =
        std::make_shared<block_delivery_detail::AttemptState>(
            block_hash, std::move(timing_hooks));
    state_->attempts.emplace(block_hash, attempt);

    const BlockDeliveryTerminal terminal(state_, attempt);
    try
    {
        start_attempt(terminal);
    }
    catch (...)
    {
        terminal.fail(std::current_exception());
    }

    return attempt->completion;
}

bool BlockDeliveryLifecycle::succeed_pending(
    const uint256_t &block_hash,
    const block_t &block) noexcept
{
    return state_->succeed_pending(block_hash, block);
}

bool BlockDeliveryLifecycle::fail_pending(
    const uint256_t &block_hash,
    std::exception_ptr reason) noexcept
{
    return state_->fail_pending(block_hash, std::move(reason));
}

void BlockDeliveryLifecycle::fail_all_pending(
    std::exception_ptr reason) noexcept
{
    state_->fail_all(std::move(reason));
}

bool BlockDeliveryLifecycle::contains(
    const uint256_t &block_hash) const noexcept
{
    return state_->attempts.find(block_hash) != state_->attempts.end();
}

std::size_t BlockDeliveryLifecycle::size() const noexcept
{
    return state_->attempts.size();
}

} // namespace hotstuff
