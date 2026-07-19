#include <algorithm>
#include <cstddef>
#include <cstdint>
#include <utility>
#include <vector>

#include "catch.hpp"
#include "hotstuff/epoch_change_bundle.h"

namespace
{

using hotstuff::AdaptiveV2EpochChangeBundle;
using hotstuff::AuthorizedEpochChange;
using hotstuff::EpochChangeBundleLimits;
using hotstuff::EpochChangeBundleWireError;
using hotstuff::EpochChangePayload;
using hotstuff::EpochDefinitionInput;
using hotstuff::EpochProtocolMode;
using hotstuff::EpochTreeDefinition;
using hotstuff::EpochWireLimits;
using hotstuff::PrivKeySecp256k1;
using hotstuff::ReplicaID;
using hotstuff::bytearray_t;
using hotstuff::uint256_t;

constexpr std::uint32_t kIssuerId = 17;

std::vector<ReplicaID> membership()
{
    return {0, 1, 2, 3, 4, 5, 6};
}

EpochTreeDefinition tree(
    std::uint32_t id,
    std::vector<ReplicaID> members,
    std::vector<ReplicaID> wait_exempt)
{
    return {id, 2, 2, std::move(members), std::move(wait_exempt)};
}

EpochDefinitionInput successor_definition()
{
    EpochDefinitionInput input;
    input.schema_version = hotstuff::kEpochDefinitionSchemaVersionV2;
    input.epoch_number = 1;
    input.previous_epoch_digest =
        hotstuff::DataStream(std::string("epoch-zero")).get_hash();
    input.membership_digest =
        hotstuff::canonical_membership_digest(membership());
    // Deliberately use the two canonicalizable orders supported by the
    // definition-reply encoder.
    input.trees = {
        tree(1, {3, 4, 5, 2, 6, 0, 1}, {1, 0}),
        tree(0, {2, 3, 4, 5, 6, 0, 1}, {1, 0})};
    input.activation_height = 0;
    input.generation_seed = 88;
    input.policy_version = "adaptive-v2-containment";
    input.evidence_snapshot_id = "accepted-cutoff-20";
    input.evidence_cutoff = 20;
    return input;
}

PrivKeySecp256k1 private_key()
{
    PrivKeySecp256k1 key;
    key.from_hex(
        "4aede145d13021fb43c938bced67511a7740c05786d3e0b94ffbdaa7f15afc57");
    return key;
}

AuthorizedEpochChange command_for(const EpochDefinitionInput &definition)
{
    return hotstuff::authorize_epoch_change(
        EpochChangePayload{
            definition.epoch_number,
            definition.previous_epoch_digest,
            hotstuff::compute_epoch_digest(definition),
            5},
        kIssuerId,
        private_key());
}

EpochChangeBundleLimits limits()
{
    return {
        8192,
        1024,
        EpochWireLimits{4096, 8, 16, 128, 2}};
}

std::size_t domain_size()
{
    return hotstuff::epoch_change_bundle_domain().size();
}

void write_u32(bytearray_t &bytes, std::size_t offset, std::uint32_t value)
{
    REQUIRE(offset + sizeof(value) <= bytes.size());
    for (std::size_t index = 0; index < sizeof(value); ++index)
    {
        bytes[offset + index] = static_cast<std::uint8_t>(
            value >> ((sizeof(value) - index - 1) * 8));
    }
}

} // namespace

TEST_CASE("adaptive v2 bundle is canonical immutable and retry stable",
          "[epoch-change][bundle][canonical]")
{
    auto definition = successor_definition();
    const auto command = command_for(definition);
    const AdaptiveV2EpochChangeBundle bundle(
        command, definition, limits());

    CHECK(bundle.schema_version() ==
          hotstuff::kEpochChangeBundleSchemaVersionV1);
    CHECK(bundle.protocol_mode() == EpochProtocolMode::adaptive_v2);
    REQUIRE(bundle.definition().trees.size() == 2);
    CHECK(bundle.definition().trees[0].tree_id == 0);
    CHECK(bundle.definition().trees[1].tree_id == 1);
    for (const auto &candidate : bundle.definition().trees)
        CHECK(candidate.wait_exempt_leaves == std::vector<ReplicaID>{0, 1});
    REQUIRE(bundle.definition().epoch_digest.has_value());
    CHECK(*bundle.definition().epoch_digest ==
          command.payload.successor_epoch_digest);

    const AdaptiveV2EpochChangeBundle retry(
        command, definition, limits());
    CHECK(retry.canonical_bytes() == bundle.canonical_bytes());

    const auto decoded = hotstuff::decode_adaptive_v2_epoch_change_bundle(
        bundle.canonical_bytes(), limits());
    REQUIRE(decoded);
    CHECK(decoded.value->canonical_bytes() == bundle.canonical_bytes());
    CHECK(decoded.value->definition().trees[0].tree_id == 0);

    const hotstuff::MsgAdaptiveV2EpochChangeBundle message(bundle);
    CHECK(static_cast<bytearray_t>(message.serialized) ==
          bundle.canonical_bytes());
    CHECK(hotstuff::MsgAdaptiveV2EpochChangeBundle::opcode == 0x19);
    CHECK(hotstuff::MsgAdaptiveV2EpochChangeBundle::opcode !=
          hotstuff::MsgEpochDefinitionReply::opcode);
}

TEST_CASE("adaptive v2 bundle constructor rejects inconsistent identities",
          "[epoch-change][bundle][identity]")
{
    auto definition = successor_definition();
    const auto command = command_for(definition);

    SECTION("definition schema")
    {
        definition.schema_version = hotstuff::kEpochDefinitionSchemaVersionV1;
        CHECK_THROWS_AS(
            AdaptiveV2EpochChangeBundle(command, definition, limits()),
            std::invalid_argument);
    }
    SECTION("absolute activation schedule")
    {
        definition.activation_height = 9;
        CHECK_THROWS_AS(
            AdaptiveV2EpochChangeBundle(command, definition, limits()),
            std::invalid_argument);
    }
    SECTION("claimed definition digest")
    {
        definition.epoch_digest = uint256_t{};
        CHECK_THROWS_AS(
            AdaptiveV2EpochChangeBundle(command, definition, limits()),
            std::invalid_argument);
    }
    SECTION("predecessor")
    {
        auto mismatched = command;
        mismatched.payload.predecessor_epoch_digest = uint256_t{};
        CHECK_THROWS_AS(
            AdaptiveV2EpochChangeBundle(mismatched, definition, limits()),
            std::invalid_argument);
    }
    SECTION("successor number")
    {
        auto mismatched = command;
        ++mismatched.payload.successor_epoch_number;
        CHECK_THROWS_AS(
            AdaptiveV2EpochChangeBundle(mismatched, definition, limits()),
            std::invalid_argument);
    }
    SECTION("successor digest")
    {
        auto mismatched = command;
        mismatched.payload.successor_epoch_digest = uint256_t{};
        CHECK_THROWS_AS(
            AdaptiveV2EpochChangeBundle(mismatched, definition, limits()),
            std::invalid_argument);
    }
    SECTION("command mode")
    {
        auto mismatched = command;
        mismatched.protocol_mode = EpochProtocolMode::adaptive_v1;
        CHECK_THROWS_AS(
            AdaptiveV2EpochChangeBundle(mismatched, definition, limits()),
            std::invalid_argument);
    }
}

TEST_CASE("adaptive v2 bundle decoder rejects malformed envelopes",
          "[epoch-change][bundle][decode]")
{
    const auto definition = successor_definition();
    const AdaptiveV2EpochChangeBundle bundle(
        command_for(definition), definition, limits());
    const auto canonical = bundle.canonical_bytes();

    SECTION("oversized")
    {
        auto bounded = limits();
        bounded.maximum_payload_bytes = canonical.size() - 1;
        const auto decoded = hotstuff::decode_adaptive_v2_epoch_change_bundle(
            canonical, bounded);
        CHECK(decoded.error == EpochChangeBundleWireError::payload_too_large);
    }
    SECTION("truncated")
    {
        auto malformed = canonical;
        malformed.pop_back();
        const auto decoded = hotstuff::decode_adaptive_v2_epoch_change_bundle(
            malformed, limits());
        CHECK(decoded.error == EpochChangeBundleWireError::truncated);
    }
    SECTION("trailing")
    {
        auto malformed = canonical;
        malformed.push_back(0);
        const auto decoded = hotstuff::decode_adaptive_v2_epoch_change_bundle(
            malformed, limits());
        CHECK(decoded.error == EpochChangeBundleWireError::trailing_bytes);
    }
    SECTION("domain")
    {
        auto malformed = canonical;
        malformed.front() ^= 0xff;
        const auto decoded = hotstuff::decode_adaptive_v2_epoch_change_bundle(
            malformed, limits());
        CHECK(decoded.error == EpochChangeBundleWireError::invalid_domain);
    }
    SECTION("schema")
    {
        auto malformed = canonical;
        write_u32(malformed, domain_size(), 2);
        const auto decoded = hotstuff::decode_adaptive_v2_epoch_change_bundle(
            malformed, limits());
        CHECK(decoded.error == EpochChangeBundleWireError::unsupported_schema);
    }
    SECTION("mode")
    {
        auto malformed = canonical;
        malformed[domain_size() + sizeof(std::uint32_t)] =
            static_cast<std::uint8_t>(EpochProtocolMode::adaptive_v1);
        const auto decoded = hotstuff::decode_adaptive_v2_epoch_change_bundle(
            malformed, limits());
        CHECK(decoded.error == EpochChangeBundleWireError::mode_mismatch);
    }
    SECTION("declared command length")
    {
        auto malformed = canonical;
        const auto command_length_offset =
            domain_size() + sizeof(std::uint32_t) + sizeof(std::uint8_t);
        write_u32(malformed, command_length_offset, 0xffffffffU);
        const auto decoded = hotstuff::decode_adaptive_v2_epoch_change_bundle(
            malformed, limits());
        CHECK(decoded.error ==
              EpochChangeBundleWireError::component_too_large);
    }
    SECTION("inner command")
    {
        auto malformed = canonical;
        const auto command_offset = domain_size() +
            sizeof(std::uint32_t) + sizeof(std::uint8_t) +
            sizeof(std::uint32_t);
        malformed[command_offset] ^= 0xff;
        const auto decoded = hotstuff::decode_adaptive_v2_epoch_change_bundle(
            malformed, limits());
        CHECK(decoded.error == EpochChangeBundleWireError::invalid_command);
        CHECK(decoded.command_error ==
              hotstuff::EpochChangeWireError::invalid_domain);
    }
}
