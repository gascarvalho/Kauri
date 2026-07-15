#ifndef KAURI_TEST_SUPPORT_FIXTURES_H
#define KAURI_TEST_SUPPORT_FIXTURES_H

#include <cstddef>
#include <cstdint>
#include <stdexcept>
#include <vector>

#include "hotstuff/entity.h"
#include "hotstuff/hotstuff.h"

namespace hotstuff::test
{

inline std::vector<ReplicaID> make_membership(std::size_t count)
{
    if (count == 0 || count > static_cast<std::size_t>(UINT16_MAX))
        throw std::invalid_argument("test membership size is out of range");

    std::vector<ReplicaID> members;
    members.reserve(count);
    for (std::size_t index = 0; index < count; ++index)
        members.push_back(static_cast<ReplicaID>(index));
    return members;
}

inline PrivKeySecp256k1 make_private_key(ReplicaID replica_id)
{
    bytearray_t raw(32, 0);
    const std::uint32_t scalar = static_cast<std::uint32_t>(replica_id) + 1;
    raw[28] = static_cast<std::uint8_t>(scalar >> 24);
    raw[29] = static_cast<std::uint8_t>(scalar >> 16);
    raw[30] = static_cast<std::uint8_t>(scalar >> 8);
    raw[31] = static_cast<std::uint8_t>(scalar);
    return PrivKeySecp256k1(raw);
}

inline ReplicaConfig make_replica_config(std::size_t count)
{
    ReplicaConfig config;
    for (const auto replica_id : make_membership(count))
    {
        auto private_key = make_private_key(replica_id);
        auto public_key = private_key.get_pubkey();
        const NetAddr address(
            static_cast<std::uint32_t>(0x7f000001),
            static_cast<std::uint16_t>(10000 + replica_id));
        config.add_replica(
            replica_id,
            ReplicaInfo(replica_id, PeerId(address), std::move(public_key)));
    }

    const auto fault_threshold = (count - 1) / 3;
    config.nmajority = 2 * fault_threshold + 1;
    return config;
}

inline Tree make_tree(std::size_t count,
                      std::uint32_t tree_id = 0,
                      std::uint8_t fanout = 2,
                      std::uint8_t pipeline_stretch = 2)
{
    if (fanout == 0)
        throw std::invalid_argument("test tree fanout must be positive");

    std::vector<std::uint32_t> members;
    for (const auto replica_id : make_membership(count))
        members.push_back(replica_id);
    return Tree(tree_id, fanout, pipeline_stretch, members);
}

inline block_t make_genesis_block()
{
    return new Block(true, 1);
}

} // namespace hotstuff::test

#endif
