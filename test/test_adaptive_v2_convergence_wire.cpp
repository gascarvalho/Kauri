#include <algorithm>
#include <cstddef>
#include <cstdint>
#include <limits>
#include <stdexcept>
#include <string>
#include <type_traits>
#include <utility>
#include <vector>

#include "catch.hpp"
#include "hotstuff/adaptive_v2_convergence_wire.h"

namespace
{

using hotstuff::AdaptiveV2ConvergenceWireError;
using hotstuff::AdaptiveV2ConvergenceWireLimits;
using hotstuff::AdaptiveV2EpochActivatedObservation;
using hotstuff::AdaptiveV2EpochChangeCommittedObservation;
using hotstuff::AdaptiveV2EpochChangeIdentity;
using hotstuff::DataStream;
using hotstuff::ReplicaID;
using hotstuff::bytearray_t;
using hotstuff::uint256_t;

static_assert(noexcept(
    hotstuff::decode_adaptive_v2_epoch_change_committed_observation(
        std::declval<const bytearray_t &>(),
        std::declval<const AdaptiveV2ConvergenceWireLimits &>())));
static_assert(noexcept(
    hotstuff::decode_adaptive_v2_epoch_activated_observation(
        std::declval<const bytearray_t &>(),
        std::declval<const AdaptiveV2ConvergenceWireLimits &>())));
static_assert(
    hotstuff::MsgAdaptiveV2EpochChangeCommittedObservation::opcode == 0x1C);
static_assert(
    hotstuff::MsgAdaptiveV2EpochActivatedObservation::opcode == 0x1D);
static_assert(
    hotstuff::MsgAdaptiveV2EpochChangeCommittedObservation::opcode < 0x12 ||
    hotstuff::MsgAdaptiveV2EpochChangeCommittedObservation::opcode > 0x1B);
static_assert(
    hotstuff::MsgAdaptiveV2EpochActivatedObservation::opcode < 0x12 ||
    hotstuff::MsgAdaptiveV2EpochActivatedObservation::opcode > 0x1B);

uint256_t digest(const std::string &label)
{
    return DataStream(label).get_hash();
}

AdaptiveV2ConvergenceWireLimits limits()
{
    return {512};
}

AdaptiveV2EpochChangeIdentity identity()
{
    return {
        0,
        digest("convergence-predecessor"),
        1,
        digest("convergence-successor"),
        digest("convergence-command"),
        760,
        digest("convergence-command-block"),
        5,
        765};
}

AdaptiveV2EpochChangeCommittedObservation committed(
    ReplicaID source = 2)
{
    return {
        hotstuff::kAdaptiveV2ConvergenceObservationSchemaVersionV1,
        source,
        identity()};
}

AdaptiveV2EpochActivatedObservation activated(
    ReplicaID source = 2)
{
    const auto expected = identity();
    return {
        hotstuff::kAdaptiveV2ConvergenceObservationSchemaVersionV1,
        source,
        expected,
        expected.successor_epoch_number,
        expected.successor_epoch_digest};
}

bool same_identity(
    const AdaptiveV2EpochChangeIdentity &left,
    const AdaptiveV2EpochChangeIdentity &right)
{
    return left.predecessor_epoch_number ==
               right.predecessor_epoch_number &&
           left.predecessor_epoch_digest ==
               right.predecessor_epoch_digest &&
           left.successor_epoch_number == right.successor_epoch_number &&
           left.successor_epoch_digest == right.successor_epoch_digest &&
           left.command_payload_digest == right.command_payload_digest &&
           left.command_block_height == right.command_block_height &&
           left.command_block_hash == right.command_block_hash &&
           left.activation_delay_blocks == right.activation_delay_blocks &&
           left.activation_height == right.activation_height;
}

template<typename UInt>
void append_integer(bytearray_t &bytes, UInt value)
{
    static_assert(std::is_unsigned<UInt>::value, "unsigned wire integer");
    for (std::size_t shift = sizeof(UInt); shift > 0; --shift)
    {
        bytes.push_back(static_cast<std::uint8_t>(
            value >> ((shift - 1) * 8)));
    }
}

void append_digest(bytearray_t &bytes, const uint256_t &value)
{
    const auto encoded = static_cast<bytearray_t>(value);
    REQUIRE(encoded.size() == 32);
    bytes.insert(bytes.end(), encoded.begin(), encoded.end());
}

bytearray_t raw_committed(
    const AdaptiveV2EpochChangeCommittedObservation &observation)
{
    const auto &domain =
        hotstuff::adaptive_v2_epoch_change_committed_observation_domain();
    bytearray_t bytes(domain.begin(), domain.end());
    append_integer(bytes, observation.schema_version);
    append_integer<std::uint8_t>(bytes, 0);
    append_integer(bytes, observation.claimed_source_replica_id);
    const auto &value = observation.identity;
    append_integer(bytes, value.predecessor_epoch_number);
    append_digest(bytes, value.predecessor_epoch_digest);
    append_integer(bytes, value.successor_epoch_number);
    append_digest(bytes, value.successor_epoch_digest);
    append_digest(bytes, value.command_payload_digest);
    append_integer(bytes, value.command_block_height);
    append_digest(bytes, value.command_block_hash);
    append_integer(bytes, value.activation_delay_blocks);
    append_integer(bytes, value.activation_height);
    return bytes;
}

} // namespace

TEST_CASE(
    "adaptive v2 convergence observations are canonical and domain separated",
    "[adaptive-v2][convergence][wire][c1][canonical]")
{
    const auto expected_commit = committed();
    const auto expected_activation = activated();

    const auto commit_bytes =
        hotstuff::encode_adaptive_v2_epoch_change_committed_observation(
            expected_commit, limits());
    const auto activation_bytes =
        hotstuff::encode_adaptive_v2_epoch_activated_observation(
            expected_activation, limits());

    CHECK(commit_bytes ==
          hotstuff::encode_adaptive_v2_epoch_change_committed_observation(
              expected_commit, limits()));
    CHECK(activation_bytes ==
          hotstuff::encode_adaptive_v2_epoch_activated_observation(
              expected_activation, limits()));
    CHECK(commit_bytes != activation_bytes);
    CHECK(DataStream(commit_bytes).get_hash() !=
          DataStream(activation_bytes).get_hash());
    CHECK(
        hotstuff::adaptive_v2_epoch_change_committed_observation_domain() !=
        hotstuff::adaptive_v2_epoch_activated_observation_domain());

    const auto decoded_commit =
        hotstuff::decode_adaptive_v2_epoch_change_committed_observation(
            commit_bytes, limits());
    REQUIRE(decoded_commit);
    REQUIRE(decoded_commit.observation.has_value());
    CHECK(decoded_commit.observation->claimed_source_replica_id == 2);
    CHECK(same_identity(
        decoded_commit.observation->identity, expected_commit.identity));

    const auto decoded_activation =
        hotstuff::decode_adaptive_v2_epoch_activated_observation(
            activation_bytes, limits());
    REQUIRE(decoded_activation);
    REQUIRE(decoded_activation.observation.has_value());
    CHECK(decoded_activation.observation->activated_epoch_number == 1);
    CHECK(decoded_activation.observation->activated_epoch_digest ==
          expected_activation.activated_epoch_digest);
    CHECK(same_identity(
        decoded_activation.observation->identity,
        expected_activation.identity));

    const auto wrong_kind =
        hotstuff::decode_adaptive_v2_epoch_change_committed_observation(
            activation_bytes, limits());
    CHECK(wrong_kind.error ==
          AdaptiveV2ConvergenceWireError::invalid_domain);
    CHECK_FALSE(wrong_kind.observation.has_value());
}

TEST_CASE(
    "adaptive v2 convergence message wrappers preserve canonical wire bytes",
    "[adaptive-v2][convergence][wire][c5][message-wrapper]")
{
    const auto expected_commit = committed();
    const auto canonical_commit =
        hotstuff::encode_adaptive_v2_epoch_change_committed_observation(
            expected_commit, limits());
    const hotstuff::MsgAdaptiveV2EpochChangeCommittedObservation
        commit_message(expected_commit, limits());
    CHECK(static_cast<bytearray_t>(commit_message.serialized) ==
          canonical_commit);

    const auto expected_activation = activated();
    const auto canonical_activation =
        hotstuff::encode_adaptive_v2_epoch_activated_observation(
            expected_activation, limits());
    const hotstuff::MsgAdaptiveV2EpochActivatedObservation
        activation_message(expected_activation, limits());
    CHECK(static_cast<bytearray_t>(activation_message.serialized) ==
          canonical_activation);

    const hotstuff::MsgAdaptiveV2EpochChangeCommittedObservation
        received_commit{DataStream(canonical_commit)};
    const hotstuff::MsgAdaptiveV2EpochActivatedObservation
        received_activation{DataStream(canonical_activation)};
    CHECK(static_cast<bytearray_t>(received_commit.serialized) ==
          canonical_commit);
    CHECK(static_cast<bytearray_t>(received_activation.serialized) ==
          canonical_activation);
}

TEST_CASE(
    "adaptive v2 convergence wire enforces commit-derived activation height",
    "[adaptive-v2][convergence][wire][c1][activation-height]")
{
    auto inconsistent = committed();
    inconsistent.identity.activation_height = 766;
    CHECK_THROWS_AS(
        hotstuff::encode_adaptive_v2_epoch_change_committed_observation(
            inconsistent, limits()),
        std::invalid_argument);

    auto overflow = committed();
    overflow.identity.command_block_height =
        std::numeric_limits<std::uint64_t>::max();
    overflow.identity.activation_delay_blocks = 1;
    overflow.identity.activation_height = 0;
    const auto decoded_overflow =
        hotstuff::decode_adaptive_v2_epoch_change_committed_observation(
            raw_committed(overflow), limits());
    CHECK(decoded_overflow.error ==
          AdaptiveV2ConvergenceWireError::activation_height_overflow);
    CHECK_FALSE(decoded_overflow.observation.has_value());

    auto zero_height = committed();
    zero_height.identity.command_block_height = 0;
    zero_height.identity.activation_height = 5;
    const auto decoded_zero =
        hotstuff::decode_adaptive_v2_epoch_change_committed_observation(
            raw_committed(zero_height), limits());
    CHECK(decoded_zero.error ==
          AdaptiveV2ConvergenceWireError::zero_command_block_height);
    CHECK_FALSE(decoded_zero.observation.has_value());
}

TEST_CASE(
    "adaptive v2 convergence decoders reject malformed and trailing bytes",
    "[adaptive-v2][convergence][wire][c1][malformed]")
{
    auto canonical =
        hotstuff::encode_adaptive_v2_epoch_change_committed_observation(
            committed(), limits());

    auto truncated = canonical;
    truncated.pop_back();
    const auto truncated_result =
        hotstuff::decode_adaptive_v2_epoch_change_committed_observation(
            truncated, limits());
    CHECK(truncated_result.error ==
          AdaptiveV2ConvergenceWireError::truncated);
    CHECK_FALSE(truncated_result.observation.has_value());

    auto trailing = canonical;
    trailing.push_back(0);
    const auto trailing_result =
        hotstuff::decode_adaptive_v2_epoch_change_committed_observation(
            trailing, limits());
    CHECK(trailing_result.error ==
          AdaptiveV2ConvergenceWireError::trailing_bytes);
    CHECK_FALSE(trailing_result.observation.has_value());

    const auto oversized_result =
        hotstuff::decode_adaptive_v2_epoch_change_committed_observation(
            canonical,
            AdaptiveV2ConvergenceWireLimits{canonical.size() - 1});
    CHECK(oversized_result.error ==
          AdaptiveV2ConvergenceWireError::payload_too_large);
    CHECK_FALSE(oversized_result.observation.has_value());
}
