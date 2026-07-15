/**
 * Copyright 2026 Goncalo Carvalho
 *
 * Licensed under the Apache License, Version 2.0 (the "License");
 * you may not use this file except in compliance with the License.
 */

#include "hotstuff/block_delivery_orchestration.h"

#include <iterator>
#include <stdexcept>
#include <utility>

namespace hotstuff
{

namespace block_delivery_orchestration_detail
{

class OrchestratorState
{
public:
    OrchestratorState() : finalizer(lifecycle) {}

    BlockDeliveryLifecycle lifecycle;
    BlockDeliveryFinalizer finalizer;
    bool cancelled{false};
    std::exception_ptr cancellation_reason;
};

class AsyncAttempt
{
public:
    AsyncAttempt(
        const uint256_t &hash,
        BlockDeliveryTerminal value,
        BlockDeliveryAsyncPlan async_plan,
        std::weak_ptr<OrchestratorState> owner)
        : block_hash(hash),
          terminal(std::move(value)),
          plan(std::move(async_plan)),
          state(std::move(owner))
    {}

    uint256_t block_hash;
    BlockDeliveryTerminal terminal;
    BlockDeliveryAsyncPlan plan;
    std::weak_ptr<OrchestratorState> state;
    block_t block;
    std::vector<promise_t> prerequisites;
    std::size_t prerequisites_remaining{0};
    bool terminal_signalled{false};
};

} // namespace block_delivery_orchestration_detail

namespace
{

using block_delivery_orchestration_detail::AsyncAttempt;
using block_delivery_orchestration_detail::OrchestratorState;

std::exception_ptr delivery_error(const char *message) noexcept
{
    try
    {
        throw std::runtime_error(message);
    }
    catch (...)
    {
        return std::current_exception();
    }
}

void release_attempt(const std::shared_ptr<AsyncAttempt> &attempt) noexcept
{
    attempt->block = nullptr;
    attempt->prerequisites.clear();
    attempt->plan = {};
}

bool is_active(const std::shared_ptr<AsyncAttempt> &attempt) noexcept
{
    if (attempt == nullptr || attempt->terminal_signalled)
        return false;
    const auto state = attempt->state.lock();
    return state != nullptr && !state->cancelled;
}

void fail_attempt(const std::shared_ptr<AsyncAttempt> &attempt,
                  std::exception_ptr reason) noexcept
{
    if (!is_active(attempt))
        return;

    attempt->terminal_signalled = true;
    const auto state = attempt->state.lock();
    const auto terminal = attempt->terminal;
    release_attempt(attempt);
    if (state != nullptr)
        state->finalizer.async_failure(terminal, std::move(reason));
}

void succeed_attempt(const std::shared_ptr<AsyncAttempt> &attempt,
                     const block_t &block) noexcept
{
    if (!is_active(attempt))
        return;

    attempt->terminal_signalled = true;
    const auto state = attempt->state.lock();
    const auto terminal = attempt->terminal;
    release_attempt(attempt);
    if (state != nullptr)
        state->finalizer.async_success(terminal, block);
}

void complete_prerequisite(
    const std::shared_ptr<AsyncAttempt> &attempt) noexcept
{
    if (!is_active(attempt))
        return;
    if (attempt->prerequisites_remaining == 0)
    {
        fail_attempt(
            attempt,
            delivery_error("block delivery prerequisite completed twice"));
        return;
    }

    --attempt->prerequisites_remaining;
    if (attempt->prerequisites_remaining != 0)
        return;

    try
    {
        const auto block = attempt->block;
        if (block == nullptr)
            throw std::runtime_error("delivered block is unavailable");
        if (!attempt->plan.deliver_core(block))
        {
            fail_attempt(
                attempt,
                delivery_error("block delivery validation failed"));
            return;
        }
        succeed_attempt(attempt, block);
    }
    catch (...)
    {
        fail_attempt(attempt, std::current_exception());
    }
}

void attach_prerequisites(const std::shared_ptr<AsyncAttempt> &attempt,
                          const block_t &block)
{
    std::vector<promise_t> prerequisites;
    prerequisites.push_back(attempt->plan.verify_block(block));
    prerequisites.push_back(attempt->plan.fetch_qc_reference(block));
    auto parents = attempt->plan.deliver_parents(block);
    prerequisites.insert(
        prerequisites.end(),
        std::make_move_iterator(parents.begin()),
        std::make_move_iterator(parents.end()));

    attempt->block = block;
    attempt->prerequisites = prerequisites;
    attempt->prerequisites_remaining = prerequisites.size();

    const std::weak_ptr<AsyncAttempt> weak_attempt = attempt;
    prerequisites.front().then(
        [weak_attempt](promise::pm_any_t result)
        {
            const auto active = weak_attempt.lock();
            if (!is_active(active))
                return;
            try
            {
                if (!promise::any_cast<bool>(result))
                {
                    fail_attempt(
                        active,
                        delivery_error(
                            "block QC verification returned false"));
                    return;
                }
                complete_prerequisite(active);
            }
            catch (...)
            {
                fail_attempt(active, std::current_exception());
            }
        },
        [weak_attempt]()
        {
            fail_attempt(
                weak_attempt.lock(),
                delivery_error("block QC verification rejected"));
        });

    for (std::size_t index = 1; index < prerequisites.size(); ++index)
    {
        if (!is_active(attempt))
            break;
        prerequisites[index].then(
            [weak_attempt]()
            {
                complete_prerequisite(weak_attempt.lock());
            },
            [weak_attempt]()
            {
                fail_attempt(
                    weak_attempt.lock(),
                    delivery_error("block delivery prerequisite rejected"));
            });
    }
}

} // namespace

BlockDeliveryOrchestrator::BlockDeliveryOrchestrator()
    : state_(std::make_shared<OrchestratorState>())
{}

BlockDeliveryOrchestrator::~BlockDeliveryOrchestrator()
{
    cancel(delivery_error("block delivery orchestrator destroyed"));
}

promise_t BlockDeliveryOrchestrator::async_delivery(
    const uint256_t &block_hash,
    BlockDeliveryTimingHooks timing,
    BlockDeliveryAsyncPlan plan)
{
    const auto state = state_;
    if (state->cancelled)
    {
        promise_t rejected;
        rejected.reject(state->cancellation_reason);
        return rejected;
    }

    const std::weak_ptr<OrchestratorState> weak_state = state;
    return state->lifecycle.request(
        block_hash,
        [block_hash,
         weak_state,
         plan = std::move(plan)](BlockDeliveryTerminal terminal) mutable
        {
            const auto state = weak_state.lock();
            if (state == nullptr || state->cancelled)
            {
                terminal.fail(
                    state == nullptr
                        ? delivery_error("block delivery orchestrator unavailable")
                        : state->cancellation_reason);
                return;
            }

            const auto attempt = std::make_shared<AsyncAttempt>(
                block_hash,
                std::move(terminal),
                std::move(plan),
                weak_state);
            attempt->terminal.retain_attempt_owner(attempt);
            const std::weak_ptr<AsyncAttempt> weak_attempt = attempt;

            try
            {
                auto fetched = attempt->plan.fetch_block();
                attempt->prerequisites = {fetched};
                fetched.then(
                    [weak_attempt](const block_t &block)
                    {
                        const auto active = weak_attempt.lock();
                        if (!is_active(active))
                            return;
                        try
                        {
                            if (block == nullptr ||
                                block->get_hash() != active->block_hash)
                                throw std::runtime_error(
                                    "fetched block does not match request");
                            attach_prerequisites(active, block);
                        }
                        catch (...)
                        {
                            fail_attempt(active, std::current_exception());
                        }
                    },
                    [weak_attempt]()
                    {
                        fail_attempt(
                            weak_attempt.lock(),
                            delivery_error("block fetch rejected"));
                    });
            }
            catch (...)
            {
                fail_attempt(attempt, std::current_exception());
            }
        },
        std::move(timing));
}

bool BlockDeliveryOrchestrator::external_delivery(
    const uint256_t &block_hash,
    const block_t &block,
    ExternalBlockDelivery deliver_core)
{
    const auto state = state_;
    if (state->cancelled)
        return false;

    try
    {
        const bool valid = deliver_core(block);
        state->finalizer.external_result(
            block_hash,
            block,
            valid,
            valid
                ? std::exception_ptr{}
                : delivery_error("external block delivery validation failed"));
        return valid;
    }
    catch (...)
    {
        const auto failure = std::current_exception();
        state->finalizer.external_result(
            block_hash, block, false, failure);
        std::rethrow_exception(failure);
    }
}

void BlockDeliveryOrchestrator::cancel(
    std::exception_ptr reason) noexcept
{
    const auto state = state_;
    if (state == nullptr || state->cancelled)
        return;

    state->cancelled = true;
    if (reason == nullptr)
        reason = delivery_error("block delivery orchestrator cancelled");
    state->cancellation_reason = reason;
    state->lifecycle.fail_all_pending(std::move(reason));
}

bool BlockDeliveryOrchestrator::contains(
    const uint256_t &block_hash) const noexcept
{
    return state_ != nullptr && state_->lifecycle.contains(block_hash);
}

std::size_t BlockDeliveryOrchestrator::size() const noexcept
{
    return state_ == nullptr ? 0 : state_->lifecycle.size();
}

bool BlockDeliveryOrchestrator::is_cancelled() const noexcept
{
    return state_ == nullptr || state_->cancelled;
}

} // namespace hotstuff
