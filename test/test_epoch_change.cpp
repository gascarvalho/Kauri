#include <algorithm>
#include <cstdint>
#include <limits>
#include <optional>
#include <utility>
#include <vector>

#include "catch.hpp"
#include "hotstuff/epoch_change.h"

namespace
{

using hotstuff::AuthorizedEpochChange;
using hotstuff::EpochChangeDelayBounds;
using hotstuff::EpochChangeDisposition;
using hotstuff::EpochChangeHistoryView;
using hotstuff::EpochChangeIssuer;
using hotstuff::EpochChangePayload;
using hotstuff::EpochChangeVerifier;
using hotstuff::EpochDefinitionInput;
using hotstuff::EpochDefinitionReply;
using hotstuff::EpochDefinitionRequest;
using hotstuff::EpochProtocolMode;
using hotstuff::EpochStore;
using hotstuff::EpochTreeDefinition;
using hotstuff::EpochValidationContext;
using hotstuff::EpochWireLimits;
using hotstuff::PrivKeySecp256k1;
using hotstuff::ReplicaID;
using hotstuff::uint256_t;

constexpr std::uint32_t kIssuerId = 17;

uint256_t digest(const char *label)
{
    return hotstuff::DataStream(std::string(label)).get_hash();
}

uint256_t sequential_digest(std::uint8_t first)
{
    hotstuff::bytearray_t bytes(32);
    for (std::size_t index = 0; index < bytes.size(); ++index)
    {
        bytes[index] = static_cast<std::uint8_t>(first + index);
    }
    return uint256_t(bytes.data());
}

std::vector<ReplicaID> membership()
{
    return {0, 1, 2, 3, 4, 5, 6};
}

EpochTreeDefinition tree(
    std::uint32_t id,
    std::vector<ReplicaID> members,
    std::vector<ReplicaID> wait_exempt = {})
{
    EpochTreeDefinition value;
    value.tree_id = id;
    value.fanout = 2;
    value.pipeline_stretch = 2;
    value.members_breadth_first = std::move(members);
    value.wait_exempt_leaves = std::move(wait_exempt);
    return value;
}

EpochDefinitionInput epoch_zero()
{
    EpochDefinitionInput input;
    input.schema_version = hotstuff::kEpochDefinitionSchemaVersionV2;
    input.epoch_number = 0;
    input.previous_epoch_digest = {};
    input.membership_digest =
        hotstuff::canonical_membership_digest(membership());
    input.trees = {
        tree(0, {0, 1, 2, 3, 4, 5, 6}),
        tree(1, {1, 2, 3, 4, 5, 6, 0})};
    input.activation_height = 0;
    input.generation_seed = 7;
    input.policy_version = "adaptive-v2-test";
    input.evidence_snapshot_id = "baseline";
    input.evidence_cutoff = 10;
    return input;
}

EpochDefinitionInput successor(const uint256_t &predecessor)
{
    auto input = epoch_zero();
    input.epoch_number = 1;
    input.previous_epoch_digest = predecessor;
    input.trees = {
        tree(0, {2, 3, 4, 0, 1, 5, 6}, {0, 1}),
        tree(1, {3, 4, 5, 0, 1, 2, 6}, {0, 1})};
    input.generation_seed = 8;
    input.evidence_snapshot_id = "containment";
    input.evidence_cutoff = 20;
    return input;
}

PrivKeySecp256k1 private_key(const char *hex)
{
    PrivKeySecp256k1 key;
    key.from_hex(hex);
    return key;
}

EpochWireLimits limits()
{
    return {4096, 8, 16, 128, 2};
}

std::size_t definition_reply_tree_offset(
    const EpochDefinitionInput &definition)
{
    constexpr std::size_t wire_header_bytes =
        sizeof(std::uint32_t) + 2 * sizeof(std::uint8_t);
    constexpr std::size_t declared_digest_bytes = 32;
    constexpr std::size_t fixed_definition_bytes =
        2 * sizeof(std::uint32_t) +
        2 * 32 +
        sizeof(std::uint64_t);
    return wire_header_bytes +
           declared_digest_bytes +
           fixed_definition_bytes +
           sizeof(std::uint32_t) + definition.policy_version.size() +
           sizeof(std::uint32_t) + definition.evidence_snapshot_id.size() +
           sizeof(std::uint64_t) +
           sizeof(std::uint32_t);
}

std::size_t wait_exempt_data_offset(
    std::size_t tree_offset,
    const EpochTreeDefinition &definition)
{
    constexpr std::size_t tree_header_bytes = 4 * sizeof(std::uint32_t);
    return tree_offset +
           tree_header_bytes +
           definition.members_breadth_first.size() * sizeof(ReplicaID) +
           sizeof(std::uint32_t);
}

std::size_t definition_tree_wire_size(
    const EpochTreeDefinition &definition)
{
    constexpr std::size_t tree_header_bytes = 4 * sizeof(std::uint32_t);
    return tree_header_bytes +
           definition.members_breadth_first.size() * sizeof(ReplicaID) +
           sizeof(std::uint32_t) +
           definition.wait_exempt_leaves.size() * sizeof(ReplicaID);
}

} // namespace

TEST_CASE("C07 freezes the authorized epoch-change signing bytes",
          "[c07][epoch-change][signature][golden]")
{
    static_assert(
        sizeof(hotstuff::EpochChangeIssuerId) == sizeof(std::uint32_t),
        "epoch-change issuer identity must remain an external uint32");
    auto key = private_key(
        "4aede145d13021fb43c938bced67511a7740c05786d3e0b94ffbdaa7f15afc57");
    const EpochChangePayload payload{
        0x0A0B0C0D,
        sequential_digest(0x00),
        sequential_digest(0xE0),
        0x0102030405060708ULL};
    const auto command = hotstuff::authorize_epoch_change(
        payload, 0x01020304U, key);

    const auto signing_bytes =
        hotstuff::canonical_epoch_change_signing_bytes(command);
    CHECK(hotstuff::DataStream(signing_bytes).get_hex() ==
          "6b617572692d617574686f72697a65642d65706f63682d6368616e67652d7631"
          "0000000102010203040a0b0c0d"
          "000102030405060708090a0b0c0d0e0f"
          "101112131415161718191a1b1c1d1e1f"
          "e0e1e2e3e4e5e6e7e8e9eaebecedeeef"
          "f0f1f2f3f4f5f6f7f8f9fafbfcfdfeff"
          "0102030405060708");

    const auto encoded = hotstuff::encode_authorized_epoch_change(command);
    REQUIRE(encoded.size() == signing_bytes.size() + 64);
    CHECK(std::equal(
        signing_bytes.begin(), signing_bytes.end(), encoded.begin()));
    CHECK(hotstuff::epoch_change_envelope_digest(command) ==
          hotstuff::DataStream(encoded).get_hash());

    auto wrong_mode = command;
    wrong_mode.protocol_mode = EpochProtocolMode::adaptive_v1;
    CHECK_FALSE(hotstuff::verify_epoch_change_signature(
        wrong_mode,
        EpochChangeIssuer{
            0x01020304U, hotstuff::PubKeySecp256k1(key)}));
}

TEST_CASE("C07 signs a canonical adaptive epoch change",
          "[c07][epoch-change][signature]")
{
    auto key = private_key(
        "4aede145d13021fb43c938bced67511a7740c05786d3e0b94ffbdaa7f15afc57");
    EpochStore store(membership());
    const auto &active = store.stage(epoch_zero(), EpochValidationContext{});
    const auto next = successor(active.epoch_digest());
    const auto next_digest = hotstuff::compute_epoch_digest(next);

    const EpochChangePayload payload{
        1, active.epoch_digest(), next_digest, 5};
    const auto command = hotstuff::authorize_epoch_change(
        payload, kIssuerId, key);

    CHECK(command.schema_version == hotstuff::kEpochChangeSchemaVersionV1);
    CHECK(hotstuff::verify_epoch_change_signature(
        command,
        EpochChangeIssuer{kIssuerId, hotstuff::PubKeySecp256k1(key)}));

    auto tampered = command;
    ++tampered.payload.activation_delay_blocks;
    CHECK_FALSE(hotstuff::verify_epoch_change_signature(
        tampered,
        EpochChangeIssuer{kIssuerId, hotstuff::PubKeySecp256k1(key)}));

    const auto encoded = hotstuff::encode_authorized_epoch_change(command);
    const auto decoded = hotstuff::decode_authorized_epoch_change(
        encoded, limits().maximum_payload_bytes);
    REQUIRE(decoded);
    CHECK(decoded.value->payload == payload);
    CHECK(hotstuff::encode_authorized_epoch_change(*decoded.value) == encoded);
}

TEST_CASE("C07 authorization failures never request definition recovery",
          "[c07][epoch-change][signature][negative]")
{
    auto key = private_key(
        "4aede145d13021fb43c938bced67511a7740c05786d3e0b94ffbdaa7f15afc57");
    auto other_key = private_key(
        "4aede145d13021fb43c938bced67511a7740c05786d3e0b94ffbdaa7f15afc58");
    EpochStore store(membership());
    const auto &active = store.stage(epoch_zero(), EpochValidationContext{});
    const auto next_digest = hotstuff::compute_epoch_digest(
        successor(active.epoch_digest()));
    const auto command = hotstuff::authorize_epoch_change(
        EpochChangePayload{1, active.epoch_digest(), next_digest, 5},
        kIssuerId,
        key);
    EpochChangeVerifier verifier(
        EpochChangeIssuer{kIssuerId, hotstuff::PubKeySecp256k1(key)},
        EpochChangeDelayBounds{2, 20});
    const auto reject_without_recovery = [&](
        const AuthorizedEpochChange &candidate,
        EpochChangeDisposition expected) {
        const auto result = verifier.validate(
            candidate, active, store, EpochChangeHistoryView{});
        CHECK(result.disposition == expected);
        CHECK_FALSE(result.recovery_request.has_value());
    };

    auto wrong_issuer = command;
    ++wrong_issuer.issuer_id;
    reject_without_recovery(
        wrong_issuer, EpochChangeDisposition::unauthorized_issuer);

    EpochChangeVerifier wrong_key_verifier(
        EpochChangeIssuer{kIssuerId, hotstuff::PubKeySecp256k1(other_key)},
        EpochChangeDelayBounds{2, 20});
    const auto wrong_key = wrong_key_verifier.validate(
        command, active, store, EpochChangeHistoryView{});
    CHECK(wrong_key.disposition == EpochChangeDisposition::invalid_signature);
    CHECK_FALSE(wrong_key.recovery_request.has_value());

    auto tampered = command;
    ++tampered.payload.successor_epoch_number;
    reject_without_recovery(
        tampered, EpochChangeDisposition::invalid_signature);
    tampered = command;
    tampered.payload.predecessor_epoch_digest = digest("tampered-predecessor");
    reject_without_recovery(
        tampered, EpochChangeDisposition::invalid_signature);
    tampered = command;
    tampered.payload.successor_epoch_digest = digest("tampered-successor");
    reject_without_recovery(
        tampered, EpochChangeDisposition::invalid_signature);
    tampered = command;
    ++tampered.payload.activation_delay_blocks;
    reject_without_recovery(
        tampered, EpochChangeDisposition::invalid_signature);
    tampered = command;
    ++tampered.schema_version;
    reject_without_recovery(
        tampered, EpochChangeDisposition::unsupported_schema);
    tampered = command;
    tampered.protocol_mode = EpochProtocolMode::adaptive_v1;
    reject_without_recovery(
        tampered, EpochChangeDisposition::unsupported_mode);
}

TEST_CASE("C07 semantic command failures never request definition recovery",
          "[c07][epoch-change][validation][negative]")
{
    auto key = private_key(
        "4aede145d13021fb43c938bced67511a7740c05786d3e0b94ffbdaa7f15afc57");
    EpochStore store(membership());
    const auto &active = store.stage(epoch_zero(), EpochValidationContext{});
    const auto next_digest = hotstuff::compute_epoch_digest(
        successor(active.epoch_digest()));
    EpochChangeVerifier verifier(
        EpochChangeIssuer{kIssuerId, hotstuff::PubKeySecp256k1(key)},
        EpochChangeDelayBounds{2, 20});
    const auto reject_payload = [&](
        const EpochChangePayload &payload,
        EpochChangeDisposition expected) {
        const auto result = verifier.validate(
            hotstuff::authorize_epoch_change(payload, kIssuerId, key),
            active,
            store,
            EpochChangeHistoryView{});
        CHECK(result.disposition == expected);
        CHECK_FALSE(result.recovery_request.has_value());
    };

    reject_payload(
        EpochChangePayload{1, active.epoch_digest(), next_digest, 0},
        EpochChangeDisposition::invalid_delay);
    reject_payload(
        EpochChangePayload{1, active.epoch_digest(), next_digest, 1},
        EpochChangeDisposition::invalid_delay);
    reject_payload(
        EpochChangePayload{1, active.epoch_digest(), next_digest, 21},
        EpochChangeDisposition::invalid_delay);
    reject_payload(
        EpochChangePayload{0, active.epoch_digest(), next_digest, 5},
        EpochChangeDisposition::stale);
    reject_payload(
        EpochChangePayload{2, active.epoch_digest(), next_digest, 5},
        EpochChangeDisposition::invalid_successor);
    reject_payload(
        EpochChangePayload{1, digest("wrong-predecessor"), next_digest, 5},
        EpochChangeDisposition::wrong_predecessor);
}

TEST_CASE("C07 bounded command decoder rejects malformed envelopes",
          "[c07][epoch-change][wire][negative]")
{
    auto key = private_key(
        "4aede145d13021fb43c938bced67511a7740c05786d3e0b94ffbdaa7f15afc57");
    const auto command = hotstuff::authorize_epoch_change(
        EpochChangePayload{1, digest("predecessor"), digest("successor"), 5},
        kIssuerId,
        key);
    const auto encoded = hotstuff::encode_authorized_epoch_change(command);
    const auto signing_bytes =
        hotstuff::canonical_epoch_change_signing_bytes(command);
    constexpr std::size_t fixed_signing_fields =
        sizeof(std::uint32_t) +
        sizeof(std::uint8_t) +
        sizeof(hotstuff::EpochChangeIssuerId) +
        sizeof(std::uint32_t) +
        2 * 32 +
        sizeof(std::uint64_t);
    REQUIRE(signing_bytes.size() > fixed_signing_fields);
    const auto domain_bytes = signing_bytes.size() - fixed_signing_fields;
    const auto expect_error = [&](
        const hotstuff::bytearray_t &wire,
        std::size_t maximum,
        hotstuff::EpochChangeWireError expected) {
        const auto result = hotstuff::decode_authorized_epoch_change(
            wire, maximum);
        CHECK(result.error == expected);
        CHECK_FALSE(result.value.has_value());
    };

    expect_error(
        encoded, 0, hotstuff::EpochChangeWireError::invalid_limit);
    expect_error(
        encoded,
        encoded.size() - 1,
        hotstuff::EpochChangeWireError::payload_too_large);

    auto malformed = encoded;
    malformed.pop_back();
    expect_error(
        malformed,
        limits().maximum_payload_bytes,
        hotstuff::EpochChangeWireError::truncated);
    malformed = encoded;
    malformed.push_back(0);
    expect_error(
        malformed,
        limits().maximum_payload_bytes,
        hotstuff::EpochChangeWireError::trailing_bytes);
    malformed = encoded;
    malformed.front() ^= 0x01;
    expect_error(
        malformed,
        limits().maximum_payload_bytes,
        hotstuff::EpochChangeWireError::invalid_domain);
    malformed = encoded;
    malformed[domain_bytes + sizeof(std::uint32_t) - 1] = 2;
    expect_error(
        malformed,
        limits().maximum_payload_bytes,
        hotstuff::EpochChangeWireError::unsupported_schema);
    malformed = encoded;
    malformed[domain_bytes + sizeof(std::uint32_t)] =
        static_cast<std::uint8_t>(EpochProtocolMode::adaptive_v1);
    expect_error(
        malformed,
        limits().maximum_payload_bytes,
        hotstuff::EpochChangeWireError::unsupported_mode);
    malformed = encoded;
    std::fill(
        malformed.begin() + signing_bytes.size(),
        malformed.end(),
        0xFF);
    expect_error(
        malformed,
        limits().maximum_payload_bytes,
        hotstuff::EpochChangeWireError::malformed_signature);
}

TEST_CASE("C07 retrieves adaptive definitions by digest",
          "[c07][epoch-change][availability]")
{
    EpochStore store(membership());
    const auto &active = store.stage(epoch_zero(), EpochValidationContext{});
    auto next = successor(active.epoch_digest());
    const auto next_digest = hotstuff::compute_epoch_digest(next);

    const EpochDefinitionReply reply{
        hotstuff::kEpochWireSchemaVersionV2,
        EpochProtocolMode::adaptive_v2,
        next_digest,
        next};
    const auto encoded = hotstuff::encode_epoch_wire(reply, limits());
    const auto decoded = hotstuff::decode_epoch_definition_reply(
        encoded, EpochProtocolMode::adaptive_v2, limits());
    REQUIRE(decoded);

    const auto staged = store.stage_available_v2(
        decoded.value->definition, active);
    CHECK(staged.disposition ==
          hotstuff::DefinitionAvailabilityDisposition::staged);
    REQUIRE(store.find_epoch_by_digest(next_digest) != nullptr);

    const auto duplicate = store.stage_available_v2(
        decoded.value->definition, active);
    CHECK(duplicate.disposition ==
          hotstuff::DefinitionAvailabilityDisposition::duplicate);

    auto noncanonical_duplicate = decoded.value->definition;
    std::reverse(
        noncanonical_duplicate.trees.begin(),
        noncanonical_duplicate.trees.end());
    const auto rejected_duplicate = store.stage_available_v2(
        noncanonical_duplicate, active);
    CHECK(rejected_duplicate.disposition ==
          hotstuff::DefinitionAvailabilityDisposition::conflicting);
    CHECK(rejected_duplicate.definition == nullptr);

    const EpochDefinitionRequest request{
        hotstuff::kEpochWireSchemaVersionV2,
        EpochProtocolMode::adaptive_v2,
        next_digest};
    const auto request_wire = hotstuff::encode_epoch_wire(request, limits());
    const auto request_round_trip = hotstuff::decode_epoch_definition_request(
        request_wire, EpochProtocolMode::adaptive_v2, limits());
    REQUIRE(request_round_trip);
    CHECK(request_round_trip.value->successor_epoch_digest == next_digest);
}

TEST_CASE("C07 definition reply decoder rejects mismatched and noncanonical bytes",
          "[c07][epoch-change][availability][wire][negative]")
{
    EpochStore store(membership());
    const auto &active = store.stage(epoch_zero(), EpochValidationContext{});
    const auto next = successor(active.epoch_digest());
    const auto next_digest = hotstuff::compute_epoch_digest(next);
    const auto valid_wire = hotstuff::encode_epoch_wire(
        EpochDefinitionReply{
            hotstuff::kEpochWireSchemaVersionV2,
            EpochProtocolMode::adaptive_v2,
            next_digest,
            next},
        limits());
    const auto expect_invalid_definition = [&](
        const hotstuff::bytearray_t &wire) {
        const auto result = hotstuff::decode_epoch_definition_reply(
            wire, EpochProtocolMode::adaptive_v2, limits());
        CHECK(result.error ==
              hotstuff::EpochWireError::invalid_definition_digest);
        CHECK_FALSE(result.value.has_value());
    };

    constexpr std::size_t wire_header_bytes =
        sizeof(std::uint32_t) + 2 * sizeof(std::uint8_t);
    auto malformed = valid_wire;
    malformed[wire_header_bytes] ^= 0x01;
    expect_invalid_definition(malformed);

    const auto tree_offset = definition_reply_tree_offset(next);
    const auto first_tree_size = definition_tree_wire_size(next.trees[0]);
    const auto second_tree_size = definition_tree_wire_size(next.trees[1]);
    REQUIRE(first_tree_size == second_tree_size);
    REQUIRE(tree_offset + first_tree_size + second_tree_size ==
            valid_wire.size());
    malformed = valid_wire;
    std::swap_ranges(
        malformed.begin() + tree_offset,
        malformed.begin() + tree_offset + first_tree_size,
        malformed.begin() + tree_offset + first_tree_size);
    expect_invalid_definition(malformed);

    const auto wait_offset = wait_exempt_data_offset(
        tree_offset, next.trees[0]);
    REQUIRE(next.trees[0].wait_exempt_leaves.size() == 2);
    malformed = valid_wire;
    for (std::size_t byte = 0; byte < sizeof(ReplicaID); ++byte)
    {
        std::swap(
            malformed[wait_offset + byte],
            malformed[wait_offset + sizeof(ReplicaID) + byte]);
    }
    expect_invalid_definition(malformed);
}

TEST_CASE("C07 definition availability rejects stale and same-epoch conflicts",
          "[c07][epoch-change][availability][replay]")
{
    EpochStore store(membership());
    const auto &active = store.stage(epoch_zero(), EpochValidationContext{});

    auto stale = epoch_zero();
    ++stale.generation_seed;
    const auto stale_result = store.stage_available_v2(stale, active);
    CHECK(stale_result.disposition ==
          hotstuff::DefinitionAvailabilityDisposition::stale);
    CHECK(stale_result.definition == nullptr);

    const auto next = successor(active.epoch_digest());
    const auto staged = store.stage_available_v2(next, active);
    REQUIRE(staged.disposition ==
            hotstuff::DefinitionAvailabilityDisposition::staged);

    auto conflicting = next;
    ++conflicting.generation_seed;
    const auto conflict_result = store.stage_available_v2(
        conflicting, active);
    CHECK(conflict_result.disposition ==
          hotstuff::DefinitionAvailabilityDisposition::conflicting);
    CHECK(conflict_result.definition == nullptr);
}

TEST_CASE("C07 rejects self-consistent but invalid definition replies",
          "[c07][epoch-change][availability][byzantine]")
{
    EpochStore store(membership());
    const auto &active = store.stage(epoch_zero(), EpochValidationContext{});
    auto malformed = successor(active.epoch_digest());
    malformed.membership_digest = digest("wrong-membership");
    const auto malformed_digest = hotstuff::compute_epoch_digest(malformed);

    const auto wire = hotstuff::encode_epoch_wire(
        EpochDefinitionReply{
            hotstuff::kEpochWireSchemaVersionV2,
            EpochProtocolMode::adaptive_v2,
            malformed_digest,
            malformed},
        limits());
    const auto decoded = hotstuff::decode_epoch_definition_reply(
        wire, EpochProtocolMode::adaptive_v2, limits());
    REQUIRE(decoded);

    hotstuff::DefinitionAvailabilityResult rejected;
    CHECK_NOTHROW(rejected = store.stage_available_v2(
                      decoded.value->definition, active));
    CHECK(rejected.disposition ==
          hotstuff::DefinitionAvailabilityDisposition::conflicting);
    CHECK(rejected.definition == nullptr);
    CHECK(store.size() == 1);
}

TEST_CASE("C07 defers a valid command until its definition is available",
          "[c07][epoch-change][validation]")
{
    auto key = private_key(
        "4aede145d13021fb43c938bced67511a7740c05786d3e0b94ffbdaa7f15afc57");
    EpochStore store(membership());
    const auto &active = store.stage(epoch_zero(), EpochValidationContext{});
    auto next = successor(active.epoch_digest());
    const auto next_digest = hotstuff::compute_epoch_digest(next);
    const auto command = hotstuff::authorize_epoch_change(
        EpochChangePayload{1, active.epoch_digest(), next_digest, 5},
        kIssuerId,
        key);
    EpochChangeVerifier verifier(
        EpochChangeIssuer{kIssuerId, hotstuff::PubKeySecp256k1(key)},
        EpochChangeDelayBounds{2, 20});

    const auto missing = verifier.validate(
        command, active, store, EpochChangeHistoryView{});
    CHECK(missing.disposition ==
          EpochChangeDisposition::defer_missing_definition);
    REQUIRE(missing.recovery_request.has_value());
    CHECK(missing.recovery_request->successor_epoch_digest == next_digest);

    REQUIRE(store.stage_available_v2(next, active).disposition ==
            hotstuff::DefinitionAvailabilityDisposition::staged);
    const auto accepted = verifier.validate(
        command, active, store, EpochChangeHistoryView{});
    CHECK(accepted.disposition == EpochChangeDisposition::accepted);
    REQUIRE(accepted.successor_definition != nullptr);
    CHECK(accepted.envelope_digest ==
          hotstuff::epoch_change_envelope_digest(command));

    const auto duplicate = verifier.validate(
        command,
        active,
        store,
        EpochChangeHistoryView{accepted.payload_digest});
    CHECK(duplicate.disposition == EpochChangeDisposition::duplicate);

    const auto committed_duplicate = verifier.validate(
        command,
        active,
        store,
        EpochChangeHistoryView{
            std::nullopt, accepted.payload_digest});
    CHECK(committed_duplicate.disposition ==
          EpochChangeDisposition::duplicate);

    const auto contradictory_history = verifier.validate(
        command,
        active,
        store,
        EpochChangeHistoryView{
            accepted.payload_digest, digest("other-committed-command")});
    CHECK(contradictory_history.disposition ==
          EpochChangeDisposition::conflicting_successor);

    auto conflicting = command;
    conflicting.payload.successor_epoch_digest = digest("other-successor");
    conflicting = hotstuff::authorize_epoch_change(
        conflicting.payload, kIssuerId, key);
    const auto conflict = verifier.validate(
        conflicting,
        active,
        store,
        EpochChangeHistoryView{accepted.payload_digest});
    CHECK(conflict.disposition ==
          EpochChangeDisposition::conflicting_successor);
}
