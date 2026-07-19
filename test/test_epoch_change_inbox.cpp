#include <cstdint>
#include <limits>
#include <optional>
#include <string>
#include <utility>
#include <vector>

#include "catch.hpp"
#include "hotstuff/epoch_change_inbox.h"

namespace
{

using hotstuff::AdaptiveV2CommandInbox;
using hotstuff::AdaptiveV2CommandInboxLimits;
using hotstuff::AdaptiveV2CommandInboxState;
using hotstuff::AdaptiveV2CommandIngestDisposition;
using hotstuff::AdaptiveV2CommandPrepareDisposition;
using hotstuff::AdaptiveV2EpochChangeBundle;
using hotstuff::AdaptiveV2ProposalPreparation;
using hotstuff::AuthorizedEpochChange;
using hotstuff::ConfigurationId;
using hotstuff::DataStream;
using hotstuff::EpochChangeBundleLimits;
using hotstuff::EpochChangeDelayBounds;
using hotstuff::EpochChangeDisposition;
using hotstuff::EpochChangeHistoryView;
using hotstuff::EpochChangeIssuer;
using hotstuff::EpochChangePayload;
using hotstuff::EpochChangeVerifier;
using hotstuff::EpochDefinition;
using hotstuff::EpochDefinitionInput;
using hotstuff::EpochStore;
using hotstuff::EpochTreeDefinition;
using hotstuff::EpochValidationContext;
using hotstuff::EpochWireLimits;
using hotstuff::PrivKeySecp256k1;
using hotstuff::ProposalKey;
using hotstuff::ReplicaID;
using hotstuff::uint256_t;

constexpr std::uint32_t kIssuerId = 17;

static_assert(noexcept(std::declval<AdaptiveV2CommandInbox &>().ingest(
    std::declval<const AdaptiveV2EpochChangeBundle &>(),
    std::declval<const EpochDefinition &>(),
    std::declval<const EpochChangeVerifier &>(),
    std::declval<EpochStore &>())));

static_assert(noexcept(
    std::declval<AdaptiveV2CommandInbox &>().prepare_for_proposal(
        std::declval<const AdaptiveV2ProposalPreparation &>())));

uint256_t digest(const std::string &label)
{
    return DataStream(label).get_hash();
}

std::vector<ReplicaID> membership()
{
    return {0, 1, 2, 3, 4, 5, 6};
}

EpochTreeDefinition tree(
    std::uint32_t tree_id,
    std::vector<ReplicaID> members,
    std::vector<ReplicaID> wait_exempt = {})
{
    return EpochTreeDefinition{
        tree_id,
        2,
        2,
        std::move(members),
        std::move(wait_exempt)};
}

EpochDefinitionInput epoch_zero()
{
    EpochDefinitionInput input;
    input.schema_version = hotstuff::kEpochDefinitionSchemaVersionV2;
    input.epoch_number = 0;
    input.membership_digest =
        hotstuff::canonical_membership_digest(membership());
    input.trees = {
        tree(0, {0, 1, 2, 3, 4, 5, 6}),
        tree(1, {1, 2, 3, 4, 5, 6, 0}),
        tree(2, {2, 3, 4, 5, 6, 0, 1})};
    input.activation_height = 0;
    input.generation_seed = 11;
    input.policy_version = "adaptive-v2-baseline";
    input.evidence_snapshot_id = "baseline-cutoff";
    input.evidence_cutoff = 10;
    return input;
}

EpochDefinitionInput successor(
    const EpochDefinition &active,
    const std::string &snapshot = "containment-cutoff")
{
    EpochDefinitionInput input;
    input.schema_version = hotstuff::kEpochDefinitionSchemaVersionV2;
    input.epoch_number = active.epoch_number() + 1;
    input.previous_epoch_digest = active.epoch_digest();
    input.membership_digest =
        hotstuff::canonical_membership_digest(membership());
    input.trees = {
        tree(0, {2, 3, 4, 5, 6, 0, 1}, {0, 1}),
        tree(1, {3, 4, 5, 6, 2, 0, 1}, {0, 1}),
        tree(2, {4, 5, 6, 2, 3, 0, 1}, {0, 1})};
    input.activation_height = 0;
    input.generation_seed = 12;
    input.policy_version = "adaptive-v2-containment";
    input.evidence_snapshot_id = snapshot;
    input.evidence_cutoff = snapshot == "containment-cutoff" ? 20 : 21;
    return input;
}

PrivKeySecp256k1 private_key(
    const char *hex =
        "4aede145d13021fb43c938bced67511a7740c05786d3e0b94ffbdaa7f15afc57")
{
    PrivKeySecp256k1 key;
    key.from_hex(hex);
    return key;
}

EpochChangeBundleLimits bundle_limits()
{
    return {
        64 * 1024,
        4096,
        EpochWireLimits{32 * 1024, 8, 16, 128, 2}};
}

std::uint64_t generation(
    std::uint32_t epoch_number,
    std::uint32_t rotation_ordinal)
{
    return ((static_cast<std::uint64_t>(epoch_number) << 32) |
            rotation_ordinal) +
           1;
}

struct Fixture
{
    Fixture()
        : key(private_key()),
          store(membership()),
          active(store.stage(epoch_zero(), EpochValidationContext{})),
          verifier(
              EpochChangeIssuer{
                  kIssuerId, hotstuff::PubKeySecp256k1(key)},
              EpochChangeDelayBounds{2, 20})
    {}

    AuthorizedEpochChange command_for(
        const EpochDefinitionInput &definition,
        std::uint64_t delay = 5,
        const PrivKeySecp256k1 *signing_key = nullptr) const
    {
        return hotstuff::authorize_epoch_change(
            EpochChangePayload{
                definition.epoch_number,
                definition.previous_epoch_digest,
                hotstuff::compute_epoch_digest(definition),
                delay},
            kIssuerId,
            signing_key == nullptr ? key : *signing_key);
    }

    AdaptiveV2EpochChangeBundle bundle_for(
        EpochDefinitionInput definition,
        std::uint64_t delay = 5,
        const PrivKeySecp256k1 *signing_key = nullptr) const
    {
        auto command = command_for(definition, delay, signing_key);
        return AdaptiveV2EpochChangeBundle(
            std::move(command), std::move(definition), bundle_limits());
    }

    AdaptiveV2EpochChangeBundle valid_bundle() const
    {
        return bundle_for(successor(active));
    }

    AdaptiveV2ProposalPreparation preparation(
        std::uint32_t tree_id = 0,
        std::uint32_t ordinal = 0,
        ReplicaID local = 0,
        ReplicaID root = 0,
        EpochChangeHistoryView history = {}) const
    {
        return {
            ConfigurationId{
                active.epoch_number(), tree_id, active.epoch_digest()},
            generation(active.epoch_number(), ordinal),
            local,
            root,
            std::move(history)};
    }

    PrivKeySecp256k1 key;
    EpochStore store;
    const EpochDefinition &active;
    EpochChangeVerifier verifier;
};

ProposalKey proposal(
    const ConfigurationId &configuration,
    const std::string &label)
{
    return ProposalKey{configuration, digest(label)};
}

} // namespace

TEST_CASE(
    "adaptive-v2 inbox authenticates before staging and revalidates the exact definition",
    "[adaptive-v2][epoch-change][inbox][ingress]")
{
    Fixture fixture;
    AdaptiveV2CommandInbox inbox;
    const auto valid = fixture.valid_bundle();
    const auto result = inbox.ingest(
        valid, fixture.active, fixture.verifier, fixture.store);

    CHECK(result.disposition ==
          AdaptiveV2CommandIngestDisposition::accepted);
    CHECK(result.initial_validation ==
          EpochChangeDisposition::defer_missing_definition);
    CHECK(result.definition_staging ==
          hotstuff::DefinitionAvailabilityDisposition::staged);
    CHECK(result.final_validation == EpochChangeDisposition::accepted);
    REQUIRE(result.material != nullptr);
    CHECK(result.material->canonical_bundle == valid.canonical_bytes());
    CHECK(result.material->canonical_block_extra ==
          hotstuff::encode_epoch_change_block_extra(valid.command()));
    CHECK(result.material->payload_digest ==
          hotstuff::epoch_change_payload_digest(valid.command().payload));
    CHECK(result.material->envelope_digest ==
          hotstuff::epoch_change_envelope_digest(valid.command()));
    CHECK(result.material->successor.epoch_digest ==
          valid.command().payload.successor_epoch_digest);
    CHECK(fixture.store.size() == 2);
    CHECK(inbox.snapshot().state ==
          AdaptiveV2CommandInboxState::available);

    SECTION("invalid signature cannot stage its bundled definition")
    {
        Fixture rejected_fixture;
        AdaptiveV2CommandInbox rejected_inbox;
        const auto attacker = private_key(
            "8f2a5594909253c36281ac87bd5521566173008b678446afab3389f0d3f78d1d");
        const auto invalid = rejected_fixture.bundle_for(
            successor(rejected_fixture.active), 5, &attacker);
        const auto rejected = rejected_inbox.ingest(
            invalid,
            rejected_fixture.active,
            rejected_fixture.verifier,
            rejected_fixture.store);
        CHECK(rejected.disposition ==
              AdaptiveV2CommandIngestDisposition::rejected);
        CHECK(rejected.initial_validation ==
              EpochChangeDisposition::invalid_signature);
        CHECK_FALSE(rejected.definition_staging.has_value());
        CHECK(rejected_fixture.store.size() == 1);
        CHECK(rejected_fixture.store.find_epoch_by_digest(
                  invalid.command().payload.successor_epoch_digest) ==
              nullptr);
    }

    SECTION("invalid delay cannot stage its bundled definition")
    {
        Fixture rejected_fixture;
        AdaptiveV2CommandInbox rejected_inbox;
        const auto invalid = rejected_fixture.bundle_for(
            successor(rejected_fixture.active), 1);
        const auto rejected = rejected_inbox.ingest(
            invalid,
            rejected_fixture.active,
            rejected_fixture.verifier,
            rejected_fixture.store);
        CHECK(rejected.initial_validation ==
              EpochChangeDisposition::invalid_delay);
        CHECK_FALSE(rejected.definition_staging.has_value());
        CHECK(rejected_fixture.store.size() == 1);
    }

    SECTION("wrong predecessor context cannot stage a valid foreign bundle")
    {
        Fixture rejected_fixture;
        AdaptiveV2CommandInbox rejected_inbox;
        EpochStore foreign_store(membership());
        auto foreign_zero = epoch_zero();
        foreign_zero.evidence_snapshot_id = "foreign-baseline";
        ++foreign_zero.evidence_cutoff;
        const auto &foreign_active = foreign_store.stage(
            foreign_zero, EpochValidationContext{});
        const auto foreign = rejected_fixture.bundle_for(
            successor(foreign_active, "foreign-successor"));
        const auto rejected = rejected_inbox.ingest(
            foreign,
            rejected_fixture.active,
            rejected_fixture.verifier,
            rejected_fixture.store);
        CHECK(rejected.initial_validation ==
              EpochChangeDisposition::wrong_predecessor);
        CHECK_FALSE(rejected.definition_staging.has_value());
        CHECK(rejected_fixture.store.size() == 1);
    }
}

TEST_CASE(
    "adaptive-v2 inbox is idempotent for exact retries and rejects conflicts",
    "[adaptive-v2][epoch-change][inbox][deduplication]")
{
    Fixture fixture;
    AdaptiveV2CommandInbox inbox;
    const auto accepted_bundle = fixture.valid_bundle();
    const auto accepted = inbox.ingest(
        accepted_bundle, fixture.active, fixture.verifier, fixture.store);
    REQUIRE(accepted.disposition ==
            AdaptiveV2CommandIngestDisposition::accepted);

    const auto duplicate = inbox.ingest(
        accepted_bundle, fixture.active, fixture.verifier, fixture.store);
    CHECK(duplicate.disposition ==
          AdaptiveV2CommandIngestDisposition::duplicate);
    CHECK(duplicate.material == accepted.material);
    CHECK(fixture.store.size() == 2);

    const auto conflicting_bundle = fixture.bundle_for(
        successor(fixture.active, "conflicting-cutoff"));
    const auto conflicting_digest =
        conflicting_bundle.command().payload.successor_epoch_digest;
    const auto conflict = inbox.ingest(
        conflicting_bundle,
        fixture.active,
        fixture.verifier,
        fixture.store);
    CHECK(conflict.disposition ==
          AdaptiveV2CommandIngestDisposition::rejected);
    CHECK(conflict.initial_validation ==
          EpochChangeDisposition::defer_missing_definition);
    CHECK_FALSE(conflict.definition_staging.has_value());
    CHECK(fixture.store.find_epoch_by_digest(conflicting_digest) == nullptr);
    CHECK(inbox.snapshot().material == accepted.material);

    auto stale_definition = epoch_zero();
    stale_definition.previous_epoch_digest = fixture.active.epoch_digest();
    const auto stale_bundle = fixture.bundle_for(stale_definition);
    Fixture stale_fixture;
    AdaptiveV2CommandInbox stale_inbox;
    const auto stale = stale_inbox.ingest(
        stale_bundle,
        stale_fixture.active,
        stale_fixture.verifier,
        stale_fixture.store);
    CHECK(stale.initial_validation == EpochChangeDisposition::stale);
    CHECK(stale_fixture.store.size() == 1);
}

TEST_CASE(
    "adaptive-v2 inbox reserves only for the exact active root view",
    "[adaptive-v2][epoch-change][inbox][root]")
{
    Fixture fixture;
    AdaptiveV2CommandInbox inbox;
    REQUIRE(inbox.ingest(
                fixture.valid_bundle(),
                fixture.active,
                fixture.verifier,
                fixture.store)
                .disposition ==
            AdaptiveV2CommandIngestDisposition::accepted);

    auto wrong_epoch = fixture.preparation();
    ++wrong_epoch.active_configuration.epoch_number;
    CHECK(inbox.prepare_for_proposal(wrong_epoch).disposition ==
          AdaptiveV2CommandPrepareDisposition::wrong_configuration);

    auto wrong_digest = fixture.preparation();
    wrong_digest.active_configuration.epoch_digest = digest("wrong-epoch");
    CHECK(inbox.prepare_for_proposal(wrong_digest).disposition ==
          AdaptiveV2CommandPrepareDisposition::wrong_configuration);

    auto zero_generation = fixture.preparation();
    zero_generation.active_generation = 0;
    CHECK(inbox.prepare_for_proposal(zero_generation).disposition ==
          AdaptiveV2CommandPrepareDisposition::wrong_generation);

    auto wrong_generation_epoch = fixture.preparation();
    wrong_generation_epoch.active_generation = generation(1, 0);
    CHECK(inbox.prepare_for_proposal(wrong_generation_epoch).disposition ==
          AdaptiveV2CommandPrepareDisposition::wrong_generation);

    CHECK(inbox.prepare_for_proposal(
              fixture.preparation(1, 0, 1, 1))
              .disposition ==
          AdaptiveV2CommandPrepareDisposition::wrong_generation);
    CHECK(inbox.prepare_for_proposal(
              fixture.preparation(1, 1, 0, 1))
              .disposition ==
          AdaptiveV2CommandPrepareDisposition::not_exact_root);
    CHECK(inbox.prepare_for_proposal(
              fixture.preparation(1, 1, 1, 0))
              .disposition ==
          AdaptiveV2CommandPrepareDisposition::not_exact_root);

    const auto reserved = inbox.prepare_for_proposal(
        fixture.preparation(1, 1, 1, 1));
    REQUIRE(reserved.disposition ==
            AdaptiveV2CommandPrepareDisposition::reserved);
    REQUIRE(reserved.reservation.has_value());
    CHECK(reserved.reservation->configuration.tree_id == 1);
    CHECK(reserved.reservation->generation == generation(0, 1));
}

TEST_CASE(
    "adaptive-v2 inbox release is token-exact and preserves immutable bytes",
    "[adaptive-v2][epoch-change][inbox][reservation]")
{
    Fixture fixture;
    AdaptiveV2CommandInbox inbox;
    const auto ingested = inbox.ingest(
        fixture.valid_bundle(),
        fixture.active,
        fixture.verifier,
        fixture.store);
    REQUIRE(ingested.material != nullptr);

    const auto first = inbox.prepare_for_proposal(fixture.preparation());
    REQUIRE(first.reservation.has_value());
    CHECK(first.reservation->token == 1);
    CHECK(first.reservation->material == ingested.material);
    CHECK(inbox.prepare_for_proposal(fixture.preparation()).disposition ==
          AdaptiveV2CommandPrepareDisposition::busy);
    CHECK_FALSE(inbox.release(first.reservation->token + 1));
    CHECK(inbox.snapshot().state ==
          AdaptiveV2CommandInboxState::reserved);
    REQUIRE(inbox.release(first.reservation->token));
    CHECK(inbox.snapshot().state ==
          AdaptiveV2CommandInboxState::available);

    const auto second = inbox.prepare_for_proposal(fixture.preparation());
    REQUIRE(second.reservation.has_value());
    CHECK(second.reservation->token == 2);
    CHECK(second.reservation->material->canonical_block_extra ==
          first.reservation->material->canonical_block_extra);
}

TEST_CASE(
    "adaptive-v2 inbox suppresses covered history and rejects any conflict",
    "[adaptive-v2][epoch-change][inbox][history]")
{
    Fixture fixture;
    AdaptiveV2CommandInbox inbox;
    const auto ingested = inbox.ingest(
        fixture.valid_bundle(),
        fixture.active,
        fixture.verifier,
        fixture.store);
    REQUIRE(ingested.material != nullptr);
    const auto payload = ingested.material->payload_digest;

    CHECK(inbox.prepare_for_proposal(fixture.preparation(
              0, 0, 0, 0, EpochChangeHistoryView{payload}))
              .disposition ==
          AdaptiveV2CommandPrepareDisposition::covered_by_history);
    CHECK(inbox.snapshot().state ==
          AdaptiveV2CommandInboxState::available);

    CHECK(inbox.prepare_for_proposal(fixture.preparation(
              0,
              0,
              0,
              0,
              EpochChangeHistoryView{std::nullopt, payload}))
              .disposition ==
          AdaptiveV2CommandPrepareDisposition::covered_by_history);
    CHECK(inbox.snapshot().state ==
          AdaptiveV2CommandInboxState::available);

    CHECK(inbox.prepare_for_proposal(fixture.preparation(
              0,
              0,
              0,
              0,
              EpochChangeHistoryView{digest("conflicting-ancestor")}))
              .disposition ==
          AdaptiveV2CommandPrepareDisposition::conflicting_history);
    CHECK(inbox.prepare_for_proposal(fixture.preparation(
              0,
              0,
              0,
              0,
              EpochChangeHistoryView{
                  payload, digest("conflicting-commit")}))
              .disposition ==
          AdaptiveV2CommandPrepareDisposition::conflicting_history);
}

TEST_CASE(
    "adaptive-v2 in-flight command retries identical bytes only on a command-free fork",
    "[adaptive-v2][epoch-change][inbox][fork]")
{
    Fixture fixture;
    AdaptiveV2CommandInbox inbox;
    const auto ingested = inbox.ingest(
        fixture.valid_bundle(),
        fixture.active,
        fixture.verifier,
        fixture.store);
    REQUIRE(ingested.material != nullptr);

    const auto first = inbox.prepare_for_proposal(fixture.preparation());
    REQUIRE(first.reservation.has_value());
    const auto first_proposal = proposal(
        first.reservation->configuration, "first-fork");
    REQUIRE(inbox.mark_proposed(
        first.reservation->token, first_proposal));
    REQUIRE(inbox.snapshot().in_flight_proposal == first_proposal);

    const auto covered = inbox.prepare_for_proposal(fixture.preparation(
        0,
        0,
        0,
        0,
        EpochChangeHistoryView{ingested.material->payload_digest}));
    CHECK(covered.disposition ==
          AdaptiveV2CommandPrepareDisposition::covered_by_history);
    REQUIRE(inbox.snapshot().in_flight_proposal == first_proposal);

    const auto retry = inbox.prepare_for_proposal(fixture.preparation());
    REQUIRE(retry.disposition ==
            AdaptiveV2CommandPrepareDisposition::reserved);
    REQUIRE(retry.reservation.has_value());
    CHECK(retry.reservation->token != first.reservation->token);
    CHECK(retry.reservation->material == first.reservation->material);
    CHECK(retry.reservation->material->canonical_block_extra ==
          first.reservation->material->canonical_block_extra);
    const auto retry_proposal = proposal(
        retry.reservation->configuration, "retry-fork");
    REQUIRE(inbox.mark_proposed(
        retry.reservation->token, retry_proposal));
    CHECK(inbox.snapshot().in_flight_proposal == retry_proposal);
    CHECK_FALSE(inbox.mark_proposed(
        first.reservation->token, first_proposal));
}

TEST_CASE(
    "adaptive-v2 inbox retires only an exact authoritative commit and clears on exact activation",
    "[adaptive-v2][epoch-change][inbox][retirement]")
{
    Fixture fixture;
    AdaptiveV2CommandInbox inbox;
    const auto ingested = inbox.ingest(
        fixture.valid_bundle(),
        fixture.active,
        fixture.verifier,
        fixture.store);
    REQUIRE(ingested.material != nullptr);
    const auto reserved = inbox.prepare_for_proposal(fixture.preparation());
    REQUIRE(reserved.reservation.has_value());
    const auto proposed = proposal(
        reserved.reservation->configuration, "command-proposal");
    REQUIRE(inbox.mark_proposed(reserved.reservation->token, proposed));

    const auto successor_configuration = ConfigurationId{
        ingested.material->successor.epoch_number,
        0,
        ingested.material->successor.epoch_digest};
    CHECK_FALSE(inbox.observe_activation(
        successor_configuration,
        generation(successor_configuration.epoch_number, 0)));
    CHECK_FALSE(inbox.observe_authoritative_commit(proposed, std::nullopt));
    CHECK_FALSE(inbox.observe_authoritative_commit(
        proposed, digest("different-payload")));
    auto wrong_configuration = proposed;
    wrong_configuration.configuration.epoch_digest = digest("wrong-active");
    CHECK_FALSE(inbox.observe_authoritative_commit(
        wrong_configuration, ingested.material->payload_digest));
    CHECK(inbox.snapshot().state ==
          AdaptiveV2CommandInboxState::in_flight);

    const auto committed_on_older_fork = proposal(
        proposed.configuration, "older-authoritative-fork");
    REQUIRE(inbox.observe_authoritative_commit(
        committed_on_older_fork, ingested.material->payload_digest));
    CHECK(inbox.snapshot().state ==
          AdaptiveV2CommandInboxState::retired);
    CHECK(inbox.prepare_for_proposal(fixture.preparation()).disposition ==
          AdaptiveV2CommandPrepareDisposition::retired);
    CHECK(inbox.prepare_for_proposal(fixture.preparation(
              0,
              0,
              0,
              0,
              EpochChangeHistoryView{
                  std::nullopt, ingested.material->payload_digest}))
              .disposition ==
          AdaptiveV2CommandPrepareDisposition::covered_by_history);
    CHECK(inbox.snapshot().state ==
          AdaptiveV2CommandInboxState::retired);

    auto wrong_successor = successor_configuration;
    wrong_successor.epoch_digest = digest("wrong-successor");
    CHECK_FALSE(inbox.observe_activation(
        wrong_successor,
        generation(wrong_successor.epoch_number, 0)));
    CHECK_FALSE(inbox.observe_activation(
        successor_configuration,
        generation(successor_configuration.epoch_number, 1)));
    CHECK(inbox.snapshot().state ==
          AdaptiveV2CommandInboxState::retired);

    REQUIRE(inbox.observe_activation(
        successor_configuration,
        generation(successor_configuration.epoch_number, 0)));
    CHECK(inbox.snapshot().state == AdaptiveV2CommandInboxState::empty);
    CHECK(inbox.prepare_for_proposal(fixture.preparation()).disposition ==
          AdaptiveV2CommandPrepareDisposition::unavailable);
}

TEST_CASE(
    "adaptive-v2 inbox bounds payloads and fails closed when tokens are exhausted",
    "[adaptive-v2][epoch-change][inbox][bounds]")
{
    CHECK_THROWS_AS(
        AdaptiveV2CommandInbox(AdaptiveV2CommandInboxLimits{0, 1, 1}),
        std::invalid_argument);
    CHECK_THROWS_AS(
        AdaptiveV2CommandInbox(AdaptiveV2CommandInboxLimits{1, 0, 1}),
        std::invalid_argument);
    CHECK_THROWS_AS(
        AdaptiveV2CommandInbox(AdaptiveV2CommandInboxLimits{1, 1, 0}),
        std::invalid_argument);

    SECTION("bundle bound is checked before store mutation")
    {
        Fixture fixture;
        const auto bundle = fixture.valid_bundle();
        AdaptiveV2CommandInbox inbox(AdaptiveV2CommandInboxLimits{
            bundle.canonical_bytes().size() - 1,
            4096,
            std::numeric_limits<std::uint64_t>::max()});
        const auto result = inbox.ingest(
            bundle, fixture.active, fixture.verifier, fixture.store);
        CHECK(result.disposition ==
              AdaptiveV2CommandIngestDisposition::limit_exceeded);
        CHECK(fixture.store.size() == 1);
    }

    SECTION("block-extra bound is checked before store mutation")
    {
        Fixture fixture;
        const auto bundle = fixture.valid_bundle();
        const auto extra =
            hotstuff::encode_epoch_change_block_extra(bundle.command());
        AdaptiveV2CommandInbox inbox(AdaptiveV2CommandInboxLimits{
            bundle.canonical_bytes().size(),
            extra.size() - 1,
            std::numeric_limits<std::uint64_t>::max()});
        const auto result = inbox.ingest(
            bundle, fixture.active, fixture.verifier, fixture.store);
        CHECK(result.disposition ==
              AdaptiveV2CommandIngestDisposition::limit_exceeded);
        CHECK(result.initial_validation ==
              EpochChangeDisposition::defer_missing_definition);
        CHECK(fixture.store.size() == 1);
    }

    SECTION("reservation token exhaustion cannot reuse an old token")
    {
        Fixture fixture;
        AdaptiveV2CommandInbox inbox(
            AdaptiveV2CommandInboxLimits{64 * 1024, 4096, 1});
        REQUIRE(inbox.ingest(
                    fixture.valid_bundle(),
                    fixture.active,
                    fixture.verifier,
                    fixture.store)
                    .disposition ==
                AdaptiveV2CommandIngestDisposition::accepted);
        const auto first =
            inbox.prepare_for_proposal(fixture.preparation());
        REQUIRE(first.reservation.has_value());
        REQUIRE(inbox.release(first.reservation->token));
        CHECK(inbox.prepare_for_proposal(fixture.preparation()).disposition ==
              AdaptiveV2CommandPrepareDisposition::token_exhausted);
        CHECK(inbox.snapshot().state ==
              AdaptiveV2CommandInboxState::available);
    }
}
