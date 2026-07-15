/**
 * Copyright 2026 Goncalo Carvalho
 *
 * Licensed under the Apache License, Version 2.0 (the "License");
 * you may not use this file except in compliance with the License.
 */

#ifndef _HOTSTUFF_BLOCK_DELIVERY_COMPLETION_H
#define _HOTSTUFF_BLOCK_DELIVERY_COMPLETION_H

#include <exception>

#include "hotstuff/block_delivery.h"

namespace hotstuff
{

/** Routes every production delivery outcome through one terminal lifecycle. */
class BlockDeliveryFinalizer
{
public:
    explicit BlockDeliveryFinalizer(BlockDeliveryLifecycle &lifecycle) noexcept;

    bool external_result(const uint256_t &block_hash,
                         const block_t &block,
                         bool valid,
                         std::exception_ptr failure) noexcept;
    void async_success(BlockDeliveryTerminal terminal,
                       const block_t &block) noexcept;
    void async_failure(BlockDeliveryTerminal terminal,
                       std::exception_ptr failure) noexcept;

private:
    BlockDeliveryLifecycle &lifecycle_;
};

} // namespace hotstuff

#endif
