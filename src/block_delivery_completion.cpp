/**
 * Copyright 2026 Goncalo Carvalho
 *
 * Licensed under the Apache License, Version 2.0 (the "License");
 * you may not use this file except in compliance with the License.
 */

#include "hotstuff/block_delivery_completion.h"

#include <utility>

namespace hotstuff
{

BlockDeliveryFinalizer::BlockDeliveryFinalizer(
    BlockDeliveryLifecycle &lifecycle) noexcept
    : lifecycle_(lifecycle)
{}

bool BlockDeliveryFinalizer::external_result(
    const uint256_t &block_hash,
    const block_t &block,
    bool valid,
    std::exception_ptr failure) noexcept
{
    if (valid)
        return lifecycle_.succeed_pending(block_hash, block);
    return lifecycle_.fail_pending(block_hash, std::move(failure));
}

void BlockDeliveryFinalizer::async_success(
    BlockDeliveryTerminal terminal,
    const block_t &block) noexcept
{
    terminal.succeed(block);
}

void BlockDeliveryFinalizer::async_failure(
    BlockDeliveryTerminal terminal,
    std::exception_ptr failure) noexcept
{
    terminal.fail(std::move(failure));
}

} // namespace hotstuff
