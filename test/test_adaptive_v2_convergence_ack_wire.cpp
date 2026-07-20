#include <cstddef>
#include <cstdint>
#include <limits>
#include <stdexcept>
#include <string>
#include <type_traits>
#include <utility>

#include "catch.hpp"
#include "hotstuff/adaptive_v2_convergence_wire.h"

#if __has_include("hotstuff/adaptive_v2_convergence_ack_wire.h")
#include "hotstuff/adaptive_v2_convergence_ack_wire.h"
#define KAURI_HAS_ADAPTIVE_V2_CONVERGENCE_ACK_WIRE 1
#else
#define KAURI_HAS_ADAPTIVE_V2_CONVERGENCE_ACK_WIRE 0
#endif

namespace
{

using hotstuff::AdaptiveV2EpochActivatedObservation;
using hotstuff::AdaptiveV2EpochChangeCommittedObservation;
using hotstuff::AdaptiveV2EpochChangeIdentity;
using hotstuff::DataStream;
using hotstuff::ReplicaID;
using hotstuff::bytearray_t;
using hotstuff::uint256_t;

uint256_t digest(const std::string &label)
{
    return DataStream(label).get_hash();
}

AdaptiveV2EpochChangeIdentity identity()
{
    return {
        0,
        digest("ack-predecessor"),
        1,
        digest("ack-successor"),
        digest("ack-command-payload"),
        760,
        digest("ack-command-block"),
        5,
        765};
}

AdaptiveV2EpochChangeCommittedObservation committed(ReplicaID source)
{
    return {
        hotstuff::kAdaptiveV2ConvergenceObservationSchemaVersionV1,
        source,
        identity()};
}

AdaptiveV2EpochActivatedObservation activated(ReplicaID source)
{
    const auto exact = identity();
    return {
        hotstuff::kAdaptiveV2ConvergenceObservationSchemaVersionV1,
        source,
        exact,
        exact.successor_epoch_number,
        exact.successor_epoch_digest};
}

#if KAURI_HAS_ADAPTIVE_V2_CONVERGENCE_ACK_WIRE

using hotstuff::AdaptiveV2ConvergenceAckDecodeResult;
using hotstuff::AdaptiveV2ConvergenceAckDisposition;
using hotstuff::AdaptiveV2ConvergenceAckWireError;
using hotstuff::AdaptiveV2ConvergenceObservationAck;
using hotstuff::AdaptiveV2ConvergenceObservationKind;
using hotstuff::AdaptiveV2ConvergenceWireLimits;

AdaptiveV2ConvergenceObservationAck acknowledgement(
    ReplicaID target,
    AdaptiveV2ConvergenceObservationKind kind,
    const bytearray_t &observation_bytes,
    AdaptiveV2ConvergenceAckDisposition disposition =
        AdaptiveV2ConvergenceAckDisposition::positive)
{
    const auto opcode =
        kind == AdaptiveV2ConvergenceObservationKind::commit
            ? hotstuff::MsgAdaptiveV2EpochChangeCommittedObservation::opcode
            : hotstuff::MsgAdaptiveV2EpochActivatedObservation::opcode;
    AdaptiveV2ConvergenceObservationAck value;
    value.schema_version =
        hotstuff::kAdaptiveV2ConvergenceAckSchemaVersionV1;
    value.target_replica_id = target;
    value.observation_kind = kind;
    value.identity = identity();
    value.observation_digest =
        hotstuff::adaptive_v2_convergence_observation_digest(
            opcode, observation_bytes);
    value.disposition = disposition;
    return value;
}

void check_exact_ack(
    const AdaptiveV2ConvergenceObservationAck &actual,
    const AdaptiveV2ConvergenceObservationAck &expected)
{
    CHECK(actual.schema_version == expected.schema_version);
    CHECK(actual.target_replica_id == expected.target_replica_id);
    CHECK(actual.observation_kind == expected.observation_kind);
    CHECK(actual.identity == expected.identity);
    CHECK(actual.observation_digest == expected.observation_digest);
    CHECK(actual.disposition == expected.disposition);
}

#endif

} // namespace

TEST_CASE(
    "convergence acknowledgement binds target kind full identity and exact observation bytes",
    "[adaptive-v2][convergence][ack][wire][canonical]")
{
#if KAURI_HAS_ADAPTIVE_V2_CONVERGENCE_ACK_WIRE
    const AdaptiveV2ConvergenceWireLimits limits{512};
    const auto commit_bytes =
        hotstuff::encode_adaptive_v2_epoch_change_committed_observation(
            committed(2), limits);
    const auto activation_bytes =
        hotstuff::encode_adaptive_v2_epoch_activated_observation(
            activated(2), limits);
    const auto commit_digest =
        hotstuff::adaptive_v2_convergence_observation_digest(
            hotstuff::MsgAdaptiveV2EpochChangeCommittedObservation::opcode,
            commit_bytes);
    const auto activation_digest =
        hotstuff::adaptive_v2_convergence_observation_digest(
            hotstuff::MsgAdaptiveV2EpochActivatedObservation::opcode,
            activation_bytes);

    CHECK(commit_digest != uint256_t{});
    CHECK(activation_digest != uint256_t{});
    CHECK(commit_digest != activation_digest);
    CHECK(commit_digest ==
          hotstuff::adaptive_v2_convergence_observation_digest(
              hotstuff::MsgAdaptiveV2EpochChangeCommittedObservation::opcode,
              commit_bytes));
    CHECK_THROWS_AS(
        hotstuff::adaptive_v2_convergence_observation_digest(
            static_cast<hotstuff::opcode_t>(0), commit_bytes),
        std::invalid_argument);

    const auto expected = acknowledgement(
        2,
        AdaptiveV2ConvergenceObservationKind::activation,
        activation_bytes);
    const auto canonical =
        hotstuff::encode_adaptive_v2_convergence_observation_ack(
            expected, limits);
    CHECK(canonical ==
          hotstuff::encode_adaptive_v2_convergence_observation_ack(
              expected, limits));

    static_assert(noexcept(
        hotstuff::decode_adaptive_v2_convergence_observation_ack(
            std::declval<const bytearray_t &>(),
            std::declval<const AdaptiveV2ConvergenceWireLimits &>())));
    const AdaptiveV2ConvergenceAckDecodeResult decoded =
        hotstuff::decode_adaptive_v2_convergence_observation_ack(
            canonical, limits);
    REQUIRE(decoded);
    REQUIRE(decoded.acknowledgement.has_value());
    check_exact_ack(*decoded.acknowledgement, expected);

    CHECK(hotstuff::adaptive_v2_convergence_observation_ack_domain() !=
          hotstuff::adaptive_v2_epoch_change_committed_observation_domain());
    CHECK(hotstuff::adaptive_v2_convergence_observation_ack_domain() !=
          hotstuff::adaptive_v2_epoch_activated_observation_domain());
    static_assert(
        hotstuff::MsgAdaptiveV2ConvergenceObservationAck::opcode == 0x1E,
        "the ACK must have one distinct registered opcode");
    const hotstuff::MsgAdaptiveV2ConvergenceObservationAck message(
        expected, limits);
    CHECK(static_cast<bytearray_t>(message.serialized) == canonical);
#else
    FAIL("canonical convergence observation ACK wire contract is missing");
#endif
}

TEST_CASE(
    "convergence acknowledgement rejects malformed or semantically incomplete correlation",
    "[adaptive-v2][convergence][ack][wire][validation]")
{
#if KAURI_HAS_ADAPTIVE_V2_CONVERGENCE_ACK_WIRE
    const AdaptiveV2ConvergenceWireLimits limits{512};
    const auto activation_bytes =
        hotstuff::encode_adaptive_v2_epoch_activated_observation(
            activated(2), limits);
    const auto valid = acknowledgement(
        2,
        AdaptiveV2ConvergenceObservationKind::activation,
        activation_bytes);
    const auto canonical =
        hotstuff::encode_adaptive_v2_convergence_observation_ack(
            valid, limits);

    auto truncated = canonical;
    truncated.pop_back();
    CHECK(hotstuff::decode_adaptive_v2_convergence_observation_ack(
              truncated, limits)
              .error == AdaptiveV2ConvergenceAckWireError::truncated);

    auto wrong_domain = canonical;
    wrong_domain.front() ^= 0x01;
    CHECK(hotstuff::decode_adaptive_v2_convergence_observation_ack(
              wrong_domain, limits)
              .error == AdaptiveV2ConvergenceAckWireError::invalid_domain);

    auto trailing = canonical;
    trailing.push_back(0);
    CHECK(hotstuff::decode_adaptive_v2_convergence_observation_ack(
              trailing, limits)
              .error == AdaptiveV2ConvergenceAckWireError::trailing_bytes);

    auto invalid = valid;
    invalid.observation_digest = uint256_t{};
    CHECK_THROWS_AS(
        hotstuff::encode_adaptive_v2_convergence_observation_ack(
            invalid, limits),
        std::invalid_argument);

    invalid = valid;
    invalid.observation_kind =
        static_cast<AdaptiveV2ConvergenceObservationKind>(0);
    CHECK_THROWS_AS(
        hotstuff::encode_adaptive_v2_convergence_observation_ack(
            invalid, limits),
        std::invalid_argument);

    invalid = valid;
    invalid.disposition =
        static_cast<AdaptiveV2ConvergenceAckDisposition>(0);
    CHECK_THROWS_AS(
        hotstuff::encode_adaptive_v2_convergence_observation_ack(
            invalid, limits),
        std::invalid_argument);

    const auto permanent = acknowledgement(
        2,
        AdaptiveV2ConvergenceObservationKind::activation,
        activation_bytes,
        AdaptiveV2ConvergenceAckDisposition::permanent_rejection);
    const auto permanent_decoded =
        hotstuff::decode_adaptive_v2_convergence_observation_ack(
            hotstuff::encode_adaptive_v2_convergence_observation_ack(
                permanent, limits),
            limits);
    REQUIRE(permanent_decoded);
    REQUIRE(permanent_decoded.acknowledgement.has_value());
    check_exact_ack(*permanent_decoded.acknowledgement, permanent);
#else
    FAIL("convergence ACK validation contract is missing");
#endif
}
