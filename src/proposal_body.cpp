#include "hotstuff/proposal_body.h"

#include <cstddef>
#include <cstdint>
#include <stdexcept>
#include <string>
#include <utility>

#include "hotstuff/consensus.h"

namespace hotstuff
{
namespace
{

constexpr std::size_t kHashWireSize = 256 / 8;
constexpr std::size_t kProposalMetadataWireSize =
    (2 * sizeof(std::uint32_t)) +
    (2 * kHashWireSize) +
    sizeof(ReplicaID);

ProposalAdmissionResult rejected_malformed(const ProposalKey &key)
{
    return ProposalAdmissionResult{
        ProposalDisposition::rejected_malformed, key};
}

void consume_bounded_hash_vector(
    DataStream &stream,
    const char *field_name)
{
    std::uint32_t encoded_count{0};
    stream >> encoded_count;
    const auto count = static_cast<std::size_t>(letoh(encoded_count));
    if (count > stream.size() / kHashWireSize)
    {
        throw HotStuffInvalidEntity(
            std::string("proposal ") + field_name +
            " count exceeds the remaining payload");
    }
    if (count != 0)
        stream.get_data_inplace(count * kHashWireSize);
}

void validate_certificate_bitmap_bounds(
    DataStream stream,
    HotStuffCore &structural_decoder)
{
    auto certificate = structural_decoder.create_quorum_cert(ProposalKey{});
    const bool has_signer_bitmap =
        dynamic_cast<QuorumCertAggBLS *>(certificate.get()) != nullptr ||
        dynamic_cast<QuorumCertSecp256k1 *>(certificate.get()) != nullptr;
    if (!has_signer_bitmap)
        return;

    ProposalKey certification_key;
    std::uint32_t encoded_bits{0};
    unserialize_proposal_key(stream, certification_key);
    stream >> encoded_bits;
    const auto bit_count =
        static_cast<std::size_t>(letoh(encoded_bits));
    if (bit_count != structural_decoder.get_config().nreplicas)
    {
        throw HotStuffInvalidEntity(
            "proposal quorum signer bitmap does not match membership");
    }

    constexpr std::size_t kBitsPerWord = sizeof(std::uint64_t) * 8;
    const auto word_count =
        (bit_count + kBitsPerWord - 1) / kBitsPerWord;
    if (word_count > stream.size() / sizeof(std::uint64_t))
    {
        throw HotStuffInvalidEntity(
            "proposal quorum signer bitmap exceeds the remaining payload");
    }
}

void validate_dynamic_length_bounds(
    DataStream body,
    HotStuffCore &structural_decoder)
{
    consume_bounded_hash_vector(body, "parent");
    consume_bounded_hash_vector(body, "command");
    validate_certificate_bitmap_bounds(
        std::move(body), structural_decoder);
}

void decode_detached_proposal_body_or_throw(
    const bytearray_t &wire_payload,
    HotStuffCore &structural_decoder,
    ProposalMetadata &metadata)
{
    if (wire_payload.size() < kProposalMetadataWireSize)
        throw HotStuffInvalidEntity("proposal metadata is truncated");

    DataStream stream(wire_payload);
    metadata.unserialize(stream);
    validate_dynamic_length_bounds(stream, structural_decoder);

    Block detached_block;
    detached_block.unserialize(stream, &structural_decoder);
    if (stream.size() != 0)
        throw HotStuffInvalidEntity(
            "proposal payload contains trailing bytes");
    if (detached_block.get_hash() != metadata.block_hash)
        throw HotStuffInvalidEntity(
            "proposal block hash does not match wire metadata");
}

} // namespace

std::optional<ProposalMetadata> decode_detached_proposal_body(
    const bytearray_t &wire_payload,
    HotStuffCore &structural_decoder) noexcept
{
    try
    {
        ProposalMetadata metadata;
        decode_detached_proposal_body_or_throw(
            wire_payload, structural_decoder, metadata);
        return metadata;
    }
    catch (...)
    {
        return std::nullopt;
    }
}

ProposalAdmissionResult admit_proposal_payload(
    const bytearray_t &wire_payload,
    const PeerId &source_peer,
    HotStuffCore &structural_decoder,
    ProposalAdmissionCoordinator &coordinator)
{
    ProposalMetadata metadata{};
    bool metadata_parsed = false;

    try
    {
        if (wire_payload.size() >= kProposalMetadataWireSize)
        {
            DataStream metadata_stream(wire_payload);
            metadata.unserialize(metadata_stream);
            metadata_parsed = true;
        }
        decode_detached_proposal_body_or_throw(
            wire_payload, structural_decoder, metadata);
        metadata_parsed = true;
    }
    catch (const std::exception &)
    {
        return rejected_malformed(
            metadata_parsed ? metadata.key() : ProposalKey{});
    }
    catch (...)
    {
        return rejected_malformed(
            metadata_parsed ? metadata.key() : ProposalKey{});
    }

    return coordinator.receive(
        BufferedProposal{metadata, wire_payload, source_peer});
}

} // namespace hotstuff
