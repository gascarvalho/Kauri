#include "hotstuff/vote_identity.h"

#include <cstddef>
#include <cstdint>
#include <stdexcept>
#include <type_traits>

namespace hotstuff
{
namespace
{

constexpr char kExactVoteDomain[] = "KAURI_EXACT_VOTE_V1";
constexpr char kGenesisCertificationDomain[] =
    "KAURI_GENESIS_CERTIFICATION_V1";

template<typename UInt>
void append_big_endian(bytearray_t &output, UInt value)
{
    static_assert(std::is_unsigned<UInt>::value,
                  "canonical integers must be unsigned");
    for (std::size_t shift = sizeof(UInt); shift > 0; --shift)
    {
        output.push_back(static_cast<std::uint8_t>(
            value >> ((shift - 1) * 8)));
    }
}

void append_digest(bytearray_t &output, const uint256_t &digest)
{
    const bytearray_t bytes = static_cast<bytearray_t>(digest);
    if (bytes.size() != 32)
        throw std::logic_error("vote digest serialization is not 32 bytes");
    output.insert(output.end(), bytes.begin(), bytes.end());
}

} // namespace

bytearray_t canonical_serialize_exact_vote(const ProposalKey &key)
{
    bytearray_t bytes;
    bytes.reserve((sizeof(kExactVoteDomain) - 1) +
                  (2 * sizeof(std::uint32_t)) + 64);
    bytes.insert(bytes.end(),
                 kExactVoteDomain,
                 kExactVoteDomain + sizeof(kExactVoteDomain) - 1);
    append_big_endian(bytes, key.configuration.epoch_number);
    append_big_endian(bytes, key.configuration.tree_id);
    append_digest(bytes, key.configuration.epoch_digest);
    append_digest(bytes, key.block_hash);
    return bytes;
}

uint256_t exact_vote_authentication_digest(const ProposalKey &key)
{
    return DataStream(canonical_serialize_exact_vote(key)).get_hash();
}

void serialize_proposal_key(DataStream &stream, const ProposalKey &key)
{
    stream << key.configuration.epoch_number
           << key.configuration.tree_id
           << key.configuration.epoch_digest
           << key.block_hash;
}

void unserialize_proposal_key(DataStream &stream, ProposalKey &key)
{
    stream >> key.configuration.epoch_number
           >> key.configuration.tree_id
           >> key.configuration.epoch_digest
           >> key.block_hash;
}

ProposalKey genesis_certification_key(const uint256_t &genesis_block_hash)
{
    const bytearray_t domain(
        kGenesisCertificationDomain,
        kGenesisCertificationDomain +
            sizeof(kGenesisCertificationDomain) - 1);
    return ProposalKey{
        ConfigurationId{0, 0, DataStream(domain).get_hash()},
        genesis_block_hash};
}

} // namespace hotstuff
