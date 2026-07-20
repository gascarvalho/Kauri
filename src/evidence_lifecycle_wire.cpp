#include "hotstuff/evidence_lifecycle_wire.h"

#include <limits>
#include <new>
#include <stdexcept>
#include <type_traits>
#include <utility>

#include "detail/canonical_wire_codec.h"

namespace hotstuff
{
namespace
{

const std::string kLifecycleNoticeDomain =
    "kauri-proposal-lifecycle-notice-v2";
constexpr std::uint8_t kCanonicalFlags = 0;

struct WireFailure
{
    ProposalLifecycleWireError error;
};

using Writer = detail::CanonicalWireWriter;
using Reader = detail::CanonicalWireReader<
    WireFailure,
    ProposalLifecycleWireError>;

bool valid_limits(const ProposalLifecycleWireLimits &limits) noexcept
{
    return limits.maximum_payload_bytes != 0;
}

bool zero_digest(const uint256_t &digest) noexcept
{
    return digest == uint256_t{};
}

bool valid_configuration(const ConfigurationId &configuration) noexcept
{
    return !zero_digest(configuration.epoch_digest);
}

bool valid_proposal(const ProposalKey &proposal) noexcept
{
    return valid_configuration(proposal.configuration) &&
           !zero_digest(proposal.block_hash);
}

ProposalLifecycleFactTag fact_tag(const ProposalLifecycleFact &fact)
{
    if (fact.valueless_by_exception())
        throw std::invalid_argument(
            "proposal lifecycle fact has no canonical alternative");
    switch (fact.index())
    {
    case 0:
        return ProposalLifecycleFactTag::normal_runtime_initialized;
    case 1:
        return ProposalLifecycleFactTag::runtime_aborted;
    case 2:
        return ProposalLifecycleFactTag::committed;
    case 3:
        return ProposalLifecycleFactTag::configuration_retired;
    case 4:
        return ProposalLifecycleFactTag::retirement_floor_advanced;
    default:
        throw std::invalid_argument(
            "proposal lifecycle fact tag is unsupported");
    }
}

bool valid_tag(ProposalLifecycleFactTag tag) noexcept
{
    switch (tag)
    {
    case ProposalLifecycleFactTag::normal_runtime_initialized:
    case ProposalLifecycleFactTag::runtime_aborted:
    case ProposalLifecycleFactTag::committed:
    case ProposalLifecycleFactTag::configuration_retired:
    case ProposalLifecycleFactTag::retirement_floor_advanced:
        return true;
    }
    return false;
}

ProposalLifecycleWireError validate_notice(
    const ProposalLifecycleNotice &notice) noexcept
{
    if (notice.schema_version != kProposalLifecycleNoticeSchemaVersion)
        return ProposalLifecycleWireError::unsupported_schema;
    if (notice.source_sequence == 0)
        return ProposalLifecycleWireError::zero_sequence;
    if (notice.source_sequence ==
        std::numeric_limits<std::uint64_t>::max())
    {
        return ProposalLifecycleWireError::integer_overflow;
    }

    try
    {
        return std::visit(
            [](const auto &fact) noexcept {
                using Fact = std::decay_t<decltype(fact)>;
                if constexpr (
                    std::is_same<
                        Fact,
                        NormalProposalRuntimeInitialized>::value ||
                    std::is_same<Fact, ProposalRuntimeAborted>::value ||
                    std::is_same<Fact, ProposalCommitted>::value)
                {
                    if (!valid_configuration(
                            fact.proposal.configuration))
                    {
                        return ProposalLifecycleWireError::
                            invalid_configuration_identity;
                    }
                    return zero_digest(fact.proposal.block_hash)
                               ? ProposalLifecycleWireError::
                                     invalid_proposal_identity
                               : ProposalLifecycleWireError::none;
                }
                else if constexpr (
                    std::is_same<
                        Fact,
                        ProposalConfigurationRetired>::value)
                {
                    return valid_configuration(fact.configuration)
                               ? ProposalLifecycleWireError::none
                               : ProposalLifecycleWireError::
                                     invalid_configuration_identity;
                }
                else
                {
                    return fact.first_live_epoch == 0
                               ? ProposalLifecycleWireError::
                                     invalid_retirement_floor
                               : ProposalLifecycleWireError::none;
                }
            },
            notice.fact);
    }
    catch (...)
    {
        return ProposalLifecycleWireError::invalid_fact_tag;
    }
}

[[noreturn]] void throw_encoding_error(ProposalLifecycleWireError error)
{
    switch (error)
    {
    case ProposalLifecycleWireError::integer_overflow:
        throw std::overflow_error(
            "proposal lifecycle sequence space is exhausted");
    case ProposalLifecycleWireError::unsupported_schema:
    case ProposalLifecycleWireError::invalid_fact_tag:
    case ProposalLifecycleWireError::zero_sequence:
    case ProposalLifecycleWireError::invalid_configuration_identity:
    case ProposalLifecycleWireError::invalid_proposal_identity:
    case ProposalLifecycleWireError::invalid_retirement_floor:
        throw std::invalid_argument(
            "proposal lifecycle notice is not canonical");
    default:
        throw std::logic_error(
            "unexpected proposal lifecycle validation error");
    }
}

void encode_configuration(Writer &writer, const ConfigurationId &value)
{
    writer.integer(value.epoch_number);
    writer.integer(value.tree_id);
    writer.digest(
        value.epoch_digest,
        "proposal lifecycle digest is not 32 bytes");
}

void encode_proposal(Writer &writer, const ProposalKey &value)
{
    encode_configuration(writer, value.configuration);
    writer.digest(
        value.block_hash,
        "proposal lifecycle digest is not 32 bytes");
}

ConfigurationId decode_configuration(Reader &reader)
{
    ConfigurationId value;
    value.epoch_number = reader.integer<std::uint32_t>();
    value.tree_id = reader.integer<std::uint32_t>();
    value.epoch_digest = reader.digest();
    return value;
}

ProposalKey decode_proposal(Reader &reader)
{
    ProposalKey value;
    value.configuration = decode_configuration(reader);
    value.block_hash = reader.digest();
    return value;
}

ProposalLifecycleDecodeResult rejected(
    ProposalLifecycleWireError error) noexcept
{
    return {error, std::nullopt};
}

ProposalLifecycleDecodeResult decode_impl(
    const bytearray_t &payload,
    const ProposalLifecycleWireLimits &limits)
{
    if (!valid_limits(limits))
        return rejected(ProposalLifecycleWireError::invalid_limits);
    if (payload.size() > limits.maximum_payload_bytes)
        return rejected(ProposalLifecycleWireError::payload_too_large);

    Reader reader(payload, ProposalLifecycleWireError::truncated);
    reader.domain(
        kLifecycleNoticeDomain,
        ProposalLifecycleWireError::invalid_domain);

    ProposalLifecycleNotice notice;
    notice.schema_version = reader.integer<std::uint32_t>();
    if (notice.schema_version != kProposalLifecycleNoticeSchemaVersion)
        return rejected(ProposalLifecycleWireError::unsupported_schema);
    notice.source_replica_id = reader.integer<ReplicaID>();
    notice.source_sequence = reader.integer<std::uint64_t>();
    if (notice.source_sequence == 0)
        return rejected(ProposalLifecycleWireError::zero_sequence);
    if (notice.source_sequence ==
        std::numeric_limits<std::uint64_t>::max())
    {
        return rejected(ProposalLifecycleWireError::integer_overflow);
    }

    const auto tag = static_cast<ProposalLifecycleFactTag>(
        reader.integer<std::uint8_t>());
    if (!valid_tag(tag))
        return rejected(ProposalLifecycleWireError::invalid_fact_tag);
    if (reader.integer<std::uint8_t>() != kCanonicalFlags)
        return rejected(ProposalLifecycleWireError::noncanonical_encoding);

    switch (tag)
    {
    case ProposalLifecycleFactTag::normal_runtime_initialized:
        notice.fact = NormalProposalRuntimeInitialized{
            decode_proposal(reader)};
        break;
    case ProposalLifecycleFactTag::runtime_aborted:
        notice.fact = ProposalRuntimeAborted{decode_proposal(reader)};
        break;
    case ProposalLifecycleFactTag::committed:
        notice.fact = ProposalCommitted{
            decode_proposal(reader),
            reader.integer<std::uint64_t>()};
        break;
    case ProposalLifecycleFactTag::configuration_retired:
        notice.fact = ProposalConfigurationRetired{
            decode_configuration(reader)};
        break;
    case ProposalLifecycleFactTag::retirement_floor_advanced:
        notice.fact = ProposalRetirementFloorAdvanced{
            reader.integer<std::uint32_t>()};
        break;
    }

    const auto validation = validate_notice(notice);
    if (validation != ProposalLifecycleWireError::none)
        return rejected(validation);
    if (!reader.empty())
        return rejected(ProposalLifecycleWireError::trailing_bytes);

    const auto canonical = encode_proposal_lifecycle_notice(notice, limits);
    if (canonical != payload)
        return rejected(ProposalLifecycleWireError::noncanonical_encoding);
    return {ProposalLifecycleWireError::none, std::move(notice)};
}

} // namespace

const std::string &proposal_lifecycle_notice_domain() noexcept
{
    return kLifecycleNoticeDomain;
}

bytearray_t encode_proposal_lifecycle_notice(
    const ProposalLifecycleNotice &notice,
    const ProposalLifecycleWireLimits &limits)
{
    if (!valid_limits(limits))
        throw std::invalid_argument(
            "proposal lifecycle wire limit must be nonzero");
    const auto validation = validate_notice(notice);
    if (validation != ProposalLifecycleWireError::none)
        throw_encoding_error(validation);

    Writer writer(
        limits.maximum_payload_bytes,
        "proposal lifecycle payload exceeds byte limit");
    writer.domain(kLifecycleNoticeDomain);
    writer.integer(notice.schema_version);
    writer.integer(notice.source_replica_id);
    writer.integer(notice.source_sequence);
    writer.integer(static_cast<std::uint8_t>(fact_tag(notice.fact)));
    writer.integer(kCanonicalFlags);
    std::visit(
        [&writer](const auto &fact) {
            using Fact = std::decay_t<decltype(fact)>;
            if constexpr (
                std::is_same<
                    Fact,
                    NormalProposalRuntimeInitialized>::value ||
                std::is_same<Fact, ProposalRuntimeAborted>::value)
            {
                encode_proposal(writer, fact.proposal);
            }
            else if constexpr (
                std::is_same<Fact, ProposalCommitted>::value)
            {
                encode_proposal(writer, fact.proposal);
                writer.integer(fact.evidence_sequence_fence);
            }
            else if constexpr (
                std::is_same<
                    Fact,
                    ProposalConfigurationRetired>::value)
            {
                encode_configuration(writer, fact.configuration);
            }
            else
            {
                writer.integer(fact.first_live_epoch);
            }
        },
        notice.fact);
    return std::move(writer).finish();
}

ProposalLifecycleDecodeResult decode_proposal_lifecycle_notice(
    const bytearray_t &payload,
    const ProposalLifecycleWireLimits &limits) noexcept
{
    try
    {
        return decode_impl(payload, limits);
    }
    catch (const WireFailure &failure)
    {
        return rejected(failure.error);
    }
    catch (const std::bad_alloc &)
    {
        return rejected(ProposalLifecycleWireError::allocation_failure);
    }
    catch (const std::overflow_error &)
    {
        return rejected(ProposalLifecycleWireError::integer_overflow);
    }
    catch (...)
    {
        return rejected(ProposalLifecycleWireError::internal_failure);
    }
}

const opcode_t MsgProposalLifecycleNotice::opcode;

MsgProposalLifecycleNotice::MsgProposalLifecycleNotice(
    const ProposalLifecycleNotice &notice,
    const ProposalLifecycleWireLimits &limits)
    : serialized(encode_proposal_lifecycle_notice(notice, limits))
{}

MsgProposalLifecycleNotice::MsgProposalLifecycleNotice(
    DataStream &&serialized_payload)
    : serialized(std::move(serialized_payload))
{}

} // namespace hotstuff
