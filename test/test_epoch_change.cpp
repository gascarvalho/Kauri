#include <algorithm>
#include <cstdint>
#include <limits>
#include <optional>
#include <utility>
#include <vector>

#include "catch.hpp"
#include "hotstuff/entity.h"
#include "hotstuff/epoch_change.h"

namespace
{

using hotstuff::AuthorizedEpochChange;
using hotstuff::EpochChangeDelayBounds;
using hotstuff::EpochChangeDisposition;
using hotstuff::EpochChangeHistoryView;
using hotstuff::EpochChangeIssuer;
using hotstuff::EpochChangePayload;
using hotstuff::EpochChangeExtraDisposition;
using hotstuff::EpochChangeProposalDisposition;
using hotstuff::EpochChangeProposalHistoryDisposition;
using hotstuff::EpochChangeProposalHistoryError;
using hotstuff::EpochChangeCommittedHistoryEntry;
using hotstuff::EpochChangeCommittedHistorySnapshot;
using hotstuff::EpochChangeProposalHistoryResult;
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
using hotstuff::QuorumCertDummy;
using hotstuff::ReplicaID;
using hotstuff::block_t;
using hotstuff::bytearray_t;
using hotstuff::quorum_cert_bt;
using hotstuff::uint256_t;

constexpr std::uint32_t kIssuerId = 17;

static_assert(noexcept(hotstuff::evaluate_epoch_change_proposal_control(
    std::declval<const bytearray_t &>(),
    std::declval<std::size_t>(),
    std::declval<const EpochChangeVerifier &>(),
    std::declval<const hotstuff::EpochDefinition &>(),
    std::declval<const EpochStore &>(),
    std::declval<const EpochChangeHistoryView &>())),
    "proposal-control evaluation must reject internal failures, not throw");

static_assert(noexcept(hotstuff::build_epoch_change_proposal_history(
    std::declval<const hotstuff::Block &>(),
    std::declval<const hotstuff::Block &>(),
    std::declval<const uint256_t &>(),
    std::declval<const EpochChangeCommittedHistorySnapshot &>(),
    std::declval<std::size_t>(),
    std::declval<std::size_t>())),
    "proposal-history construction must reject internal failures, not throw");

static_assert(
    EpochChangeProposalHistoryError::allocation_failure !=
        EpochChangeProposalHistoryError::internal_failure,
    "allocation and internal builder failures must remain distinguishable");

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

block_t proposal_with_extra(
    bytearray_t extra,
    std::vector<uint256_t> commands = {digest("application-command")})
{
    const std::vector<block_t> parents;
    quorum_cert_bt qc = new QuorumCertDummy();
    return block_t(new hotstuff::Block(
        parents,
        commands,
        qc->clone(),
        std::move(extra),
        1,
        nullptr,
        nullptr));
}

block_t history_block(
    const char *label,
    std::uint32_t height,
    std::vector<block_t> parents = {},
    bytearray_t extra = {},
    std::int8_t decision = 0)
{
    quorum_cert_bt qc = new QuorumCertDummy();
    return block_t(new hotstuff::Block(
        parents,
        {digest(label)},
        qc->clone(),
        std::move(extra),
        height,
        nullptr,
        nullptr,
        decision));
}

block_t committed_history_block(
    const char *label,
    std::uint32_t height,
    bytearray_t extra = {})
{
    return history_block(label, height, {}, std::move(extra), 1);
}

EpochChangeCommittedHistorySnapshot history_snapshot(
    const block_t &committed_head,
    std::optional<EpochChangeCommittedHistoryEntry> command = std::nullopt)
{
    return {
        committed_head->get_hash(),
        committed_head->get_height(),
        std::move(command)};
}

void check_history_rejected(
    const EpochChangeProposalHistoryResult &result,
    EpochChangeProposalHistoryError error,
    hotstuff::EpochChangeWireError wire_error =
        hotstuff::EpochChangeWireError::none)
{
    CHECK(result.disposition ==
          EpochChangeProposalHistoryDisposition::rejected);
    CHECK(result.error == error);
    CHECK(result.wire_error == wire_error);
    CHECK_FALSE(result.history.ancestry_payload_digest.has_value());
    CHECK_FALSE(result.history.committed_payload_digest.has_value());
}

AuthorizedEpochChange history_command(
    const uint256_t &predecessor,
    const char *successor_label)
{
    auto key = private_key(
        "4aede145d13021fb43c938bced67511a7740c05786d3e0b94ffbdaa7f15afc57");
    return hotstuff::authorize_epoch_change(
        EpochChangePayload{1, predecessor, digest(successor_label), 5},
        kIssuerId,
        key);
}

bytearray_t high_s_epoch_change_wire(bytearray_t wire)
{
    hotstuff::DataStream order_stream;
    order_stream.load_hex(
        "fffffffffffffffffffffffffffffffebaaedce6af48a03bbfd25e8cd0364141");
    const bytearray_t order = static_cast<bytearray_t>(order_stream);
    REQUIRE(order.size() == 32);
    REQUIRE(wire.size() >= order.size());
    const auto s_offset = wire.size() - order.size();
    unsigned borrow = 0;
    for (std::size_t remaining = order.size(); remaining > 0; --remaining)
    {
        const auto index = remaining - 1;
        int difference = static_cast<int>(order[index]) -
                         static_cast<int>(wire[s_offset + index]) -
                         static_cast<int>(borrow);
        if (difference < 0)
        {
            difference += 256;
            borrow = 1;
        }
        else
        {
            borrow = 0;
        }
        wire[s_offset + index] = static_cast<std::uint8_t>(difference);
    }
    REQUIRE(borrow == 0);
    return wire;
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

TEST_CASE("C08b1 extracts zero or one canonical epoch command from block extra",
          "[c08b1][epoch-change][block-extra][canonical]")
{
    auto key = private_key(
        "4aede145d13021fb43c938bced67511a7740c05786d3e0b94ffbdaa7f15afc57");
    const auto command = hotstuff::authorize_epoch_change(
        EpochChangePayload{1, digest("predecessor"), digest("successor"), 5},
        kIssuerId,
        key);
    const auto wire = hotstuff::encode_epoch_change_block_extra(command);

    const auto empty = hotstuff::extract_epoch_change_block_extra(
        bytearray_t{}, limits().maximum_payload_bytes);
    CHECK(empty.disposition == EpochChangeExtraDisposition::absent);
    CHECK(empty.wire_error == hotstuff::EpochChangeWireError::none);
    CHECK_FALSE(empty.command.has_value());
    CHECK_FALSE(empty.payload_digest.has_value());
    CHECK_FALSE(empty.envelope_digest.has_value());

    const auto invalid_limit = hotstuff::extract_epoch_change_block_extra(
        bytearray_t{}, 0);
    CHECK(invalid_limit.disposition ==
          EpochChangeExtraDisposition::rejected);
    CHECK(invalid_limit.wire_error ==
          hotstuff::EpochChangeWireError::invalid_limit);

    const auto canonical = hotstuff::extract_epoch_change_block_extra(
        wire, wire.size());
    CHECK(canonical.disposition == EpochChangeExtraDisposition::present);
    CHECK(canonical.wire_error == hotstuff::EpochChangeWireError::none);
    REQUIRE(canonical.command.has_value());
    CHECK(canonical.command->payload == command.payload);
    REQUIRE(canonical.payload_digest.has_value());
    CHECK(*canonical.payload_digest ==
          hotstuff::epoch_change_payload_digest(command.payload));
    REQUIRE(canonical.envelope_digest.has_value());
    CHECK(*canonical.envelope_digest ==
          hotstuff::epoch_change_envelope_digest(command));
    CHECK(hotstuff::encode_epoch_change_block_extra(*canonical.command) ==
          wire);

    const auto oversized = hotstuff::extract_epoch_change_block_extra(
        wire, wire.size() - 1);
    CHECK(oversized.disposition == EpochChangeExtraDisposition::rejected);
    CHECK(oversized.wire_error ==
          hotstuff::EpochChangeWireError::payload_too_large);

    auto malformed = wire;
    malformed.front() ^= 0x01;
    const auto invalid_domain = hotstuff::extract_epoch_change_block_extra(
        malformed, limits().maximum_payload_bytes);
    CHECK(invalid_domain.disposition ==
          EpochChangeExtraDisposition::rejected);
    CHECK(invalid_domain.wire_error ==
          hotstuff::EpochChangeWireError::invalid_domain);

    malformed = wire;
    malformed.pop_back();
    const auto truncated = hotstuff::extract_epoch_change_block_extra(
        malformed, limits().maximum_payload_bytes);
    CHECK(truncated.disposition == EpochChangeExtraDisposition::rejected);
    CHECK(truncated.wire_error ==
          hotstuff::EpochChangeWireError::truncated);

    malformed = wire;
    malformed.push_back(0);
    const auto trailing = hotstuff::extract_epoch_change_block_extra(
        malformed, limits().maximum_payload_bytes);
    CHECK(trailing.disposition == EpochChangeExtraDisposition::rejected);
    CHECK(trailing.wire_error ==
          hotstuff::EpochChangeWireError::trailing_bytes);

    const auto two_commands = [&]() {
        auto value = wire;
        value.insert(value.end(), wire.begin(), wire.end());
        return value;
    }();
    const auto multiple = hotstuff::extract_epoch_change_block_extra(
        two_commands, 2 * limits().maximum_payload_bytes);
    CHECK(multiple.disposition == EpochChangeExtraDisposition::rejected);
    CHECK(multiple.wire_error ==
          hotstuff::EpochChangeWireError::trailing_bytes);

    const auto noncanonical = hotstuff::extract_epoch_change_block_extra(
        high_s_epoch_change_wire(wire), limits().maximum_payload_bytes);
    CHECK(noncanonical.disposition == EpochChangeExtraDisposition::rejected);
    CHECK(noncanonical.wire_error ==
          hotstuff::EpochChangeWireError::noncanonical_encoding);
}

TEST_CASE("C08b1 proposal control authenticates before definition retrieval",
          "[c08b1][epoch-change][proposal-control][auth-first]")
{
    auto key = private_key(
        "4aede145d13021fb43c938bced67511a7740c05786d3e0b94ffbdaa7f15afc57");
    EpochStore store(membership());
    const auto &active = store.stage(epoch_zero(), EpochValidationContext{});
    const auto next_digest = hotstuff::compute_epoch_digest(
        successor(active.epoch_digest()));
    const EpochChangeVerifier verifier(
        EpochChangeIssuer{kIssuerId, hotstuff::PubKeySecp256k1(key)},
        EpochChangeDelayBounds{2, 20});
    const auto evaluate = [&](
        const AuthorizedEpochChange &candidate,
        const EpochChangeHistoryView &history = {}) {
        const auto extra = hotstuff::encode_epoch_change_block_extra(candidate);
        return hotstuff::evaluate_epoch_change_proposal_control(
            extra,
            limits().maximum_payload_bytes,
            verifier,
            active,
            store,
            history);
    };
    const auto reject_without_request = [&](
        const AuthorizedEpochChange &candidate,
        EpochChangeDisposition expected) {
        const auto result = evaluate(candidate);
        CHECK(result.disposition == EpochChangeProposalDisposition::rejected);
        CHECK(result.wire_error == hotstuff::EpochChangeWireError::none);
        REQUIRE(result.command.has_value());
        REQUIRE(result.validation.has_value());
        CHECK(result.validation->disposition == expected);
        CHECK_FALSE(result.validation->recovery_request.has_value());
    };

    const auto empty = hotstuff::evaluate_epoch_change_proposal_control(
        bytearray_t{},
        limits().maximum_payload_bytes,
        verifier,
        active,
        store,
        EpochChangeHistoryView{});
    CHECK(empty.disposition == EpochChangeProposalDisposition::accepted);
    CHECK_FALSE(empty.command.has_value());
    CHECK_FALSE(empty.validation.has_value());

    auto invalid_signature = hotstuff::authorize_epoch_change(
        EpochChangePayload{1, active.epoch_digest(), next_digest, 5},
        kIssuerId,
        key);
    invalid_signature.payload.successor_epoch_digest =
        digest("attacker-selected-missing-definition");
    reject_without_request(
        invalid_signature, EpochChangeDisposition::invalid_signature);

    const auto wrong_issuer = hotstuff::authorize_epoch_change(
        EpochChangePayload{1, active.epoch_digest(), next_digest, 5},
        kIssuerId + 1,
        key);
    reject_without_request(
        wrong_issuer, EpochChangeDisposition::unauthorized_issuer);
    reject_without_request(
        hotstuff::authorize_epoch_change(
            EpochChangePayload{1, active.epoch_digest(), next_digest, 1},
            kIssuerId,
            key),
        EpochChangeDisposition::invalid_delay);
    reject_without_request(
        hotstuff::authorize_epoch_change(
            EpochChangePayload{1, digest("wrong-predecessor"), next_digest, 5},
            kIssuerId,
            key),
        EpochChangeDisposition::wrong_predecessor);
    reject_without_request(
        hotstuff::authorize_epoch_change(
            EpochChangePayload{2, active.epoch_digest(), next_digest, 5},
            kIssuerId,
            key),
        EpochChangeDisposition::invalid_successor);

    const auto valid = hotstuff::authorize_epoch_change(
        EpochChangePayload{1, active.epoch_digest(), next_digest, 5},
        kIssuerId,
        key);
    const auto conflict = evaluate(
        valid, EpochChangeHistoryView{digest("different-command")});
    CHECK(conflict.disposition == EpochChangeProposalDisposition::rejected);
    REQUIRE(conflict.validation.has_value());
    CHECK(conflict.validation->disposition ==
          EpochChangeDisposition::conflicting_successor);
    CHECK_FALSE(conflict.validation->recovery_request.has_value());

    const auto valid_wire = hotstuff::encode_epoch_change_block_extra(valid);
    const auto expect_wire_rejected = [&](
        const bytearray_t &wire,
        hotstuff::EpochChangeWireError expected) {
        const auto result = hotstuff::evaluate_epoch_change_proposal_control(
            wire,
            limits().maximum_payload_bytes,
            verifier,
            active,
            store,
            EpochChangeHistoryView{});
        CHECK(result.disposition ==
              EpochChangeProposalDisposition::rejected);
        CHECK(result.wire_error == expected);
        CHECK_FALSE(result.command.has_value());
        CHECK_FALSE(result.validation.has_value());
    };

    auto malformed = valid_wire;
    malformed.push_back(0);
    expect_wire_rejected(
        malformed, hotstuff::EpochChangeWireError::trailing_bytes);

    const auto signing_bytes =
        hotstuff::canonical_epoch_change_signing_bytes(valid);
    constexpr std::size_t fixed_signing_fields =
        sizeof(std::uint32_t) +
        sizeof(std::uint8_t) +
        sizeof(hotstuff::EpochChangeIssuerId) +
        sizeof(std::uint32_t) +
        2 * 32 +
        sizeof(std::uint64_t);
    REQUIRE(signing_bytes.size() > fixed_signing_fields);
    const auto domain_bytes = signing_bytes.size() - fixed_signing_fields;
    malformed = valid_wire;
    malformed[domain_bytes + sizeof(std::uint32_t) - 1] = 2;
    expect_wire_rejected(
        malformed, hotstuff::EpochChangeWireError::unsupported_schema);
    malformed = valid_wire;
    malformed[domain_bytes + sizeof(std::uint32_t)] =
        static_cast<std::uint8_t>(EpochProtocolMode::adaptive_v1);
    expect_wire_rejected(
        malformed, hotstuff::EpochChangeWireError::unsupported_mode);
}

TEST_CASE("C08b1 defers until the exact definition and then preserves history",
          "[c08b1][epoch-change][proposal-control][availability][history]")
{
    auto key = private_key(
        "4aede145d13021fb43c938bced67511a7740c05786d3e0b94ffbdaa7f15afc57");
    EpochStore store(membership());
    const auto &active = store.stage(epoch_zero(), EpochValidationContext{});
    const auto next = successor(active.epoch_digest());
    const auto next_digest = hotstuff::compute_epoch_digest(next);
    const auto command = hotstuff::authorize_epoch_change(
        EpochChangePayload{1, active.epoch_digest(), next_digest, 5},
        kIssuerId,
        key);
    const auto extra = hotstuff::encode_epoch_change_block_extra(command);
    const EpochChangeVerifier verifier(
        EpochChangeIssuer{kIssuerId, hotstuff::PubKeySecp256k1(key)},
        EpochChangeDelayBounds{2, 20});
    const auto evaluate = [&](const EpochChangeHistoryView &history = {}) {
        return hotstuff::evaluate_epoch_change_proposal_control(
            extra,
            limits().maximum_payload_bytes,
            verifier,
            active,
            store,
            history);
    };
    const auto payload_digest =
        hotstuff::epoch_change_payload_digest(command.payload);

    const auto missing = evaluate();
    CHECK(missing.disposition == EpochChangeProposalDisposition::defer);
    REQUIRE(missing.command.has_value());
    REQUIRE(missing.validation.has_value());
    CHECK(missing.validation->payload_digest == payload_digest);
    CHECK(missing.validation->envelope_digest ==
          hotstuff::epoch_change_envelope_digest(command));
    REQUIRE(missing.validation->recovery_request.has_value());
    CHECK(missing.validation->recovery_request->successor_epoch_digest ==
          next_digest);

    const auto duplicate_missing = evaluate(
        EpochChangeHistoryView{payload_digest});
    CHECK(duplicate_missing.disposition ==
          EpochChangeProposalDisposition::defer);
    REQUIRE(duplicate_missing.validation.has_value());
    REQUIRE(duplicate_missing.validation->recovery_request.has_value());
    CHECK(duplicate_missing.validation->recovery_request
              ->successor_epoch_digest == next_digest);

    const auto reply_wire = hotstuff::encode_epoch_wire(
        EpochDefinitionReply{
            hotstuff::kEpochWireSchemaVersionV2,
            EpochProtocolMode::adaptive_v2,
            next_digest,
            next},
        limits());
    const auto reply = hotstuff::decode_epoch_definition_reply(
        reply_wire, EpochProtocolMode::adaptive_v2, limits());
    REQUIRE(reply);
    REQUIRE(store.stage_available_v2(
                reply.value->definition, active)
                .disposition ==
            hotstuff::DefinitionAvailabilityDisposition::staged);
    const auto accepted = evaluate();
    CHECK(accepted.disposition == EpochChangeProposalDisposition::accepted);
    REQUIRE(accepted.validation.has_value());
    CHECK(accepted.validation->successor_definition ==
          store.find_epoch_by_digest(next_digest));
    CHECK_FALSE(accepted.validation->recovery_request.has_value());

    const auto ancestry_duplicate = evaluate(
        EpochChangeHistoryView{payload_digest});
    CHECK(ancestry_duplicate.disposition ==
          EpochChangeProposalDisposition::duplicate);
    REQUIRE(ancestry_duplicate.validation.has_value());
    CHECK(ancestry_duplicate.validation->disposition ==
          EpochChangeDisposition::duplicate);
    CHECK(ancestry_duplicate.validation->successor_definition ==
          store.find_epoch_by_digest(next_digest));
    CHECK_FALSE(ancestry_duplicate.validation->recovery_request.has_value());

    const auto committed_duplicate = evaluate(
        EpochChangeHistoryView{std::nullopt, payload_digest});
    CHECK(committed_duplicate.disposition ==
          EpochChangeProposalDisposition::duplicate);

    const auto conflict = evaluate(EpochChangeHistoryView{
        payload_digest, digest("different-committed-command")});
    CHECK(conflict.disposition == EpochChangeProposalDisposition::rejected);
    REQUIRE(conflict.validation.has_value());
    CHECK(conflict.validation->disposition ==
          EpochChangeDisposition::conflicting_successor);
    CHECK_FALSE(conflict.validation->recovery_request.has_value());
}

TEST_CASE("C08b1 block extra changes identity without becoming an app command",
          "[c08b1][epoch-change][block-extra][block-hash]")
{
    auto key = private_key(
        "4aede145d13021fb43c938bced67511a7740c05786d3e0b94ffbdaa7f15afc57");
    const auto command = hotstuff::authorize_epoch_change(
        EpochChangePayload{1, digest("predecessor"), digest("successor"), 5},
        kIssuerId,
        key);
    const std::vector<uint256_t> application_commands = {
        digest("application-1"), digest("application-2")};
    const auto without_control = proposal_with_extra(
        bytearray_t{}, application_commands);
    const auto with_control = proposal_with_extra(
        hotstuff::encode_epoch_change_block_extra(command),
        application_commands);

    hotstuff::DataStream without_control_qc;
    hotstuff::DataStream with_control_qc;
    without_control->get_qc()->serialize(without_control_qc);
    with_control->get_qc()->serialize(with_control_qc);

    CHECK(without_control->get_parent_hashes() ==
          with_control->get_parent_hashes());
    CHECK(without_control->get_cmds() == with_control->get_cmds());
    CHECK(without_control->get_cmds() == application_commands);
    CHECK(static_cast<bytearray_t>(without_control_qc) ==
          static_cast<bytearray_t>(with_control_qc));
    CHECK(without_control->get_hash() != with_control->get_hash());
    CHECK(without_control->get_extra().empty());
    CHECK_FALSE(with_control->get_extra().empty());
}

TEST_CASE("C08b2b proposal history accepts a clean immediate boundary",
          "[c08b2b][epoch-change][proposal-history][boundary]")
{
    const auto predecessor = digest("history-predecessor");
    const auto candidate_command = history_command(
        predecessor, "candidate-is-not-history");
    const auto committed = committed_history_block("committed", 10);
    const auto candidate = history_block(
        "candidate",
        11,
        {committed},
        hotstuff::encode_epoch_change_block_extra(candidate_command));

    const auto result = hotstuff::build_epoch_change_proposal_history(
        *candidate,
        *committed,
        predecessor,
        history_snapshot(committed),
        limits().maximum_payload_bytes,
        0);

    CHECK(result.disposition ==
          EpochChangeProposalHistoryDisposition::complete);
    CHECK(result.error == EpochChangeProposalHistoryError::none);
    CHECK(result.wire_error == hotstuff::EpochChangeWireError::none);
    CHECK_FALSE(result.history.ancestry_payload_digest.has_value());
    CHECK_FALSE(result.history.committed_payload_digest.has_value());
}

TEST_CASE("C08b2b proposal history treats repeated ancestor commands idempotently",
          "[c08b2b][epoch-change][proposal-history][duplicate]")
{
    const auto predecessor = digest("history-predecessor");
    const auto command = history_command(predecessor, "same-successor");
    const auto wire = hotstuff::encode_epoch_change_block_extra(command);
    const auto payload_digest =
        hotstuff::epoch_change_payload_digest(command.payload);
    const auto committed = committed_history_block("committed", 10);
    const auto older = history_block("older", 11, {committed}, wire);
    const auto newer = history_block("newer", 12, {older}, wire);
    const auto candidate = history_block("candidate", 13, {newer});

    const auto result = hotstuff::build_epoch_change_proposal_history(
        *candidate,
        *committed,
        predecessor,
        history_snapshot(committed),
        limits().maximum_payload_bytes,
        2);

    REQUIRE(result.disposition ==
            EpochChangeProposalHistoryDisposition::complete);
    REQUIRE(result.history.ancestry_payload_digest.has_value());
    CHECK(*result.history.ancestry_payload_digest == payload_digest);
    CHECK_FALSE(result.history.committed_payload_digest.has_value());
}

TEST_CASE("C08b2b proposal history rejects conflicting ancestor commands",
          "[c08b2b][epoch-change][proposal-history][conflict]")
{
    const auto predecessor = digest("history-predecessor");
    const auto first = history_command(predecessor, "first-successor");
    const auto second = history_command(predecessor, "second-successor");
    const auto committed = committed_history_block("committed", 10);
    const auto older = history_block(
        "older",
        11,
        {committed},
        hotstuff::encode_epoch_change_block_extra(first));
    const auto newer = history_block(
        "newer",
        12,
        {older},
        hotstuff::encode_epoch_change_block_extra(second));
    const auto candidate = history_block("candidate", 13, {newer});

    const auto result = hotstuff::build_epoch_change_proposal_history(
        *candidate,
        *committed,
        predecessor,
        history_snapshot(committed),
        limits().maximum_payload_bytes,
        2);

    check_history_rejected(
        result, EpochChangeProposalHistoryError::conflicting_history);
}

TEST_CASE("C08b2b proposal history ignores conflicting uncles",
          "[c08b2b][epoch-change][proposal-history][first-parent]")
{
    const auto predecessor = digest("history-predecessor");
    const auto accepted_command = history_command(
        predecessor, "first-parent-successor");
    const auto uncle_command = history_command(
        predecessor, "uncle-successor");
    const auto accepted_digest =
        hotstuff::epoch_change_payload_digest(accepted_command.payload);
    const auto committed = committed_history_block("committed", 10);
    const auto first_parent = history_block(
        "first-parent",
        11,
        {committed},
        hotstuff::encode_epoch_change_block_extra(accepted_command));
    const auto uncle = history_block(
        "uncle",
        11,
        {committed},
        hotstuff::encode_epoch_change_block_extra(uncle_command));
    const auto candidate = history_block(
        "candidate", 12, {first_parent, uncle});

    const auto result = hotstuff::build_epoch_change_proposal_history(
        *candidate,
        *committed,
        predecessor,
        history_snapshot(committed),
        limits().maximum_payload_bytes,
        1);

    REQUIRE(result.disposition ==
            EpochChangeProposalHistoryDisposition::complete);
    REQUIRE(result.history.ancestry_payload_digest.has_value());
    CHECK(*result.history.ancestry_payload_digest == accepted_digest);
}

TEST_CASE("C08b2b proposal history ignores older-predecessor commands",
          "[c08b2b][epoch-change][proposal-history][predecessor]")
{
    const auto candidate_predecessor = digest("history-predecessor");
    const auto older_command = history_command(
        digest("older-predecessor"), "older-successor");
    const auto committed = committed_history_block("committed", 10);
    const auto ancestor = history_block(
        "ancestor",
        11,
        {committed},
        hotstuff::encode_epoch_change_block_extra(older_command));
    const auto candidate = history_block("candidate", 12, {ancestor});

    const auto result = hotstuff::build_epoch_change_proposal_history(
        *candidate,
        *committed,
        candidate_predecessor,
        history_snapshot(committed),
        limits().maximum_payload_bytes,
        1);

    CHECK(result.disposition ==
          EpochChangeProposalHistoryDisposition::complete);
    CHECK_FALSE(result.history.ancestry_payload_digest.has_value());
    CHECK_FALSE(result.history.committed_payload_digest.has_value());
}

TEST_CASE("C08b2b proposal history exposes malformed ancestor wire errors",
          "[c08b2b][epoch-change][proposal-history][wire]")
{
    const auto predecessor = digest("history-predecessor");
    const auto older_command = history_command(
        digest("older-predecessor"), "older-successor");
    const auto canonical =
        hotstuff::encode_epoch_change_block_extra(older_command);
    const auto committed = committed_history_block("committed", 10);

    SECTION("trailing bytes")
    {
        auto malformed = canonical;
        malformed.push_back(0);
        const auto ancestor = history_block(
            "ancestor", 11, {committed}, std::move(malformed));
        const auto candidate = history_block("candidate", 12, {ancestor});
        const auto result = hotstuff::build_epoch_change_proposal_history(
            *candidate,
            *committed,
            predecessor,
            history_snapshot(committed),
            limits().maximum_payload_bytes,
            1);
        check_history_rejected(
            result,
            EpochChangeProposalHistoryError::malformed_extra,
            hotstuff::EpochChangeWireError::trailing_bytes);
    }

    SECTION("noncanonical signature")
    {
        const auto ancestor = history_block(
            "ancestor",
            11,
            {committed},
            high_s_epoch_change_wire(canonical));
        const auto candidate = history_block("candidate", 12, {ancestor});
        const auto result = hotstuff::build_epoch_change_proposal_history(
            *candidate,
            *committed,
            predecessor,
            history_snapshot(committed),
            limits().maximum_payload_bytes,
            1);
        check_history_rejected(
            result,
            EpochChangeProposalHistoryError::malformed_extra,
            hotstuff::EpochChangeWireError::noncanonical_encoding);
    }
}

TEST_CASE("C08b2b proposal history rejects incomplete or invalid ancestry",
          "[c08b2b][epoch-change][proposal-history][structure]")
{
    const auto predecessor = digest("history-predecessor");
    const auto committed = committed_history_block("committed", 10);

    SECTION("committed boundary is not decided")
    {
        const auto undecided = history_block("undecided", 10);
        const auto candidate = history_block("candidate", 11, {undecided});
        const auto result = hotstuff::build_epoch_change_proposal_history(
            *candidate,
            *undecided,
            predecessor,
            history_snapshot(undecided),
            limits().maximum_payload_bytes,
            0);
        check_history_rejected(
            result,
            EpochChangeProposalHistoryError::invalid_committed_boundary);
    }

    SECTION("committed snapshot is bound to another head")
    {
        const auto candidate = history_block(
            "candidate", 11, {committed});
        auto snapshot = history_snapshot(committed);
        snapshot.committed_head_height += 1;
        const auto result = hotstuff::build_epoch_change_proposal_history(
            *candidate,
            *committed,
            predecessor,
            snapshot,
            limits().maximum_payload_bytes,
            0);
        check_history_rejected(
            result,
            EpochChangeProposalHistoryError::incoherent_committed_snapshot);
    }

    SECTION("missing first parent")
    {
        const auto candidate = history_block("candidate", 11);
        const auto result = hotstuff::build_epoch_change_proposal_history(
            *candidate,
            *committed,
            predecessor,
            history_snapshot(committed),
            limits().maximum_payload_bytes,
            1);
        check_history_rejected(
            result, EpochChangeProposalHistoryError::missing_parent);
    }

    SECTION("null first parent")
    {
        const auto candidate = history_block(
            "candidate", 11, {committed});
        auto &resolved_parents =
            const_cast<std::vector<block_t> &>(candidate->get_parents());
        resolved_parents[0] = block_t{};
        const auto result = hotstuff::build_epoch_change_proposal_history(
            *candidate,
            *committed,
            predecessor,
            history_snapshot(committed),
            limits().maximum_payload_bytes,
            1);
        check_history_rejected(
            result, EpochChangeProposalHistoryError::missing_parent);
    }

    SECTION("resolved first parent differs from its declared hash")
    {
        const auto original_parent = history_block(
            "original-parent", 11, {committed});
        const auto replacement_parent = history_block(
            "replacement-parent", 11, {committed});
        const auto candidate = history_block(
            "candidate", 12, {original_parent});
        auto &resolved_parents =
            const_cast<std::vector<block_t> &>(candidate->get_parents());
        resolved_parents[0] = replacement_parent;
        const auto result = hotstuff::build_epoch_change_proposal_history(
            *candidate,
            *committed,
            predecessor,
            history_snapshot(committed),
            limits().maximum_payload_bytes,
            1);
        check_history_rejected(
            result, EpochChangeProposalHistoryError::parent_hash_mismatch);
    }

    SECTION("nondecreasing first-parent height")
    {
        const auto parent = history_block("parent", 11, {committed});
        const auto candidate = history_block("candidate", 11, {parent});
        const auto result = hotstuff::build_epoch_change_proposal_history(
            *candidate,
            *committed,
            predecessor,
            history_snapshot(committed),
            limits().maximum_payload_bytes,
            1);
        check_history_rejected(
            result,
            EpochChangeProposalHistoryError::nondecreasing_height);
    }

    SECTION("exact committed boundary is not reached")
    {
        const auto conflicting_boundary = history_block(
            "same-height-different-boundary", 10);
        const auto candidate = history_block(
            "candidate", 11, {conflicting_boundary});
        const auto result = hotstuff::build_epoch_change_proposal_history(
            *candidate,
            *committed,
            predecessor,
            history_snapshot(committed),
            limits().maximum_payload_bytes,
            1);
        check_history_rejected(
            result, EpochChangeProposalHistoryError::boundary_not_reached);
    }

    SECTION("zero block-extra limit is invalid")
    {
        const auto candidate = history_block(
            "candidate", 11, {committed});
        const auto result = hotstuff::build_epoch_change_proposal_history(
            *candidate,
            *committed,
            predecessor,
            history_snapshot(committed),
            0,
            0);
        check_history_rejected(
            result, EpochChangeProposalHistoryError::invalid_limit);
    }
}

TEST_CASE("C08b2b proposal history enforces the ancestry walk bound",
          "[c08b2b][epoch-change][proposal-history][bound]")
{
    const auto predecessor = digest("history-predecessor");
    const auto committed = committed_history_block("committed", 10);
    const auto older = history_block("older", 11, {committed});
    const auto newer = history_block("newer", 12, {older});
    const auto candidate = history_block("candidate", 13, {newer});

    const auto exact = hotstuff::build_epoch_change_proposal_history(
        *candidate,
        *committed,
        predecessor,
        history_snapshot(committed),
        limits().maximum_payload_bytes,
        2);
    CHECK(exact.disposition ==
          EpochChangeProposalHistoryDisposition::complete);

    const auto one_over = hotstuff::build_epoch_change_proposal_history(
        *candidate,
        *committed,
        predecessor,
        history_snapshot(committed),
        limits().maximum_payload_bytes,
        1);

    check_history_rejected(
        one_over,
        EpochChangeProposalHistoryError::ancestry_limit_exceeded);
}

TEST_CASE("C08b2b proposal history reconciles the committed boundary command",
          "[c08b2b][epoch-change][proposal-history][committed-boundary]")
{
    const auto predecessor = digest("history-predecessor");
    const auto command = history_command(predecessor, "boundary-successor");
    const auto payload_digest =
        hotstuff::epoch_change_payload_digest(command.payload);
    const auto committed = committed_history_block(
        "committed",
        10,
        hotstuff::encode_epoch_change_block_extra(command));
    const auto candidate = history_block("candidate", 11, {committed});

    SECTION("boundary fills an empty snapshot")
    {
        const auto result = hotstuff::build_epoch_change_proposal_history(
            *candidate,
            *committed,
            predecessor,
            history_snapshot(committed),
            limits().maximum_payload_bytes,
            0);
        REQUIRE(result.disposition ==
                EpochChangeProposalHistoryDisposition::complete);
        CHECK_FALSE(result.history.ancestry_payload_digest.has_value());
        REQUIRE(result.history.committed_payload_digest.has_value());
        CHECK(*result.history.committed_payload_digest == payload_digest);
    }

    SECTION("boundary repeats the matching snapshot idempotently")
    {
        const auto result = hotstuff::build_epoch_change_proposal_history(
            *candidate,
            *committed,
            predecessor,
            history_snapshot(
                committed,
                EpochChangeCommittedHistoryEntry{
                    predecessor, payload_digest}),
            limits().maximum_payload_bytes,
            0);
        REQUIRE(result.disposition ==
                EpochChangeProposalHistoryDisposition::complete);
        REQUIRE(result.history.committed_payload_digest.has_value());
        CHECK(*result.history.committed_payload_digest == payload_digest);
    }

    SECTION("boundary conflicts with the matching snapshot")
    {
        const auto result = hotstuff::build_epoch_change_proposal_history(
            *candidate,
            *committed,
            predecessor,
            history_snapshot(
                committed,
                EpochChangeCommittedHistoryEntry{
                    predecessor, digest("different-payload")}),
            limits().maximum_payload_bytes,
            0);
        check_history_rejected(
            result, EpochChangeProposalHistoryError::conflicting_history);
    }

    SECTION("malformed boundary extra is rejected")
    {
        auto malformed =
            hotstuff::encode_epoch_change_block_extra(command);
        malformed.push_back(0);
        const auto malformed_committed = committed_history_block(
            "malformed-committed", 10, std::move(malformed));
        const auto malformed_candidate = history_block(
            "malformed-candidate", 11, {malformed_committed});
        const auto result = hotstuff::build_epoch_change_proposal_history(
            *malformed_candidate,
            *malformed_committed,
            predecessor,
            history_snapshot(malformed_committed),
            limits().maximum_payload_bytes,
            0);
        check_history_rejected(
            result,
            EpochChangeProposalHistoryError::malformed_extra,
            hotstuff::EpochChangeWireError::trailing_bytes);
    }
}

TEST_CASE("C08b2b proposal history merges only a matching committed snapshot",
          "[c08b2b][epoch-change][proposal-history][committed-snapshot]")
{
    const auto predecessor = digest("history-predecessor");
    const auto command = history_command(predecessor, "successor");
    const auto payload_digest =
        hotstuff::epoch_change_payload_digest(command.payload);
    const auto committed = committed_history_block("committed", 10);

    SECTION("matching snapshot entry contributes committed history")
    {
        const auto candidate = history_block(
            "candidate", 11, {committed});
        const auto result = hotstuff::build_epoch_change_proposal_history(
            *candidate,
            *committed,
            predecessor,
            history_snapshot(
                committed,
                EpochChangeCommittedHistoryEntry{
                    predecessor, payload_digest}),
            limits().maximum_payload_bytes,
            0);
        REQUIRE(result.disposition ==
                EpochChangeProposalHistoryDisposition::complete);
        CHECK_FALSE(result.history.ancestry_payload_digest.has_value());
        REQUIRE(result.history.committed_payload_digest.has_value());
        CHECK(*result.history.committed_payload_digest == payload_digest);
    }

    SECTION("older snapshot entry is ignored")
    {
        const auto candidate = history_block(
            "candidate", 11, {committed});
        const auto result = hotstuff::build_epoch_change_proposal_history(
            *candidate,
            *committed,
            predecessor,
            history_snapshot(
                committed,
                EpochChangeCommittedHistoryEntry{
                    digest("older-predecessor"), digest("older-payload")}),
            limits().maximum_payload_bytes,
            0);
        REQUIRE(result.disposition ==
                EpochChangeProposalHistoryDisposition::complete);
        CHECK_FALSE(result.history.ancestry_payload_digest.has_value());
        CHECK_FALSE(result.history.committed_payload_digest.has_value());
    }

    SECTION("matching snapshot entry repeats idempotently in ancestry")
    {
        const auto ancestor = history_block(
            "ancestor",
            11,
            {committed},
            hotstuff::encode_epoch_change_block_extra(command));
        const auto candidate = history_block("candidate", 12, {ancestor});
        const auto result = hotstuff::build_epoch_change_proposal_history(
            *candidate,
            *committed,
            predecessor,
            history_snapshot(
                committed,
                EpochChangeCommittedHistoryEntry{
                    predecessor, payload_digest}),
            limits().maximum_payload_bytes,
            1);
        REQUIRE(result.disposition ==
                EpochChangeProposalHistoryDisposition::complete);
        REQUIRE(result.history.ancestry_payload_digest.has_value());
        REQUIRE(result.history.committed_payload_digest.has_value());
        CHECK(*result.history.ancestry_payload_digest == payload_digest);
        CHECK(*result.history.committed_payload_digest == payload_digest);
    }

    SECTION("matching snapshot entry conflicts with ancestry")
    {
        const auto conflicting = history_command(
            predecessor, "conflicting-successor");
        const auto ancestor = history_block(
            "ancestor",
            11,
            {committed},
            hotstuff::encode_epoch_change_block_extra(conflicting));
        const auto candidate = history_block("candidate", 12, {ancestor});
        const auto result = hotstuff::build_epoch_change_proposal_history(
            *candidate,
            *committed,
            predecessor,
            history_snapshot(
                committed,
                EpochChangeCommittedHistoryEntry{
                    predecessor, payload_digest}),
            limits().maximum_payload_bytes,
            1);
        check_history_rejected(
            result, EpochChangeProposalHistoryError::conflicting_history);
    }
}
