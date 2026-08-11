#include <string>

#include "catch.hpp"

/*
 * REM-P05-01 proposal-body preflight contract
 * -------------------------------------------
 * Keep this target compiling while the production seam is absent. Once
 * include/hotstuff/proposal_body.h exists, the complete behavior matrix below
 * compiles and runs through the same entry point required by
 * HotStuffBase::propose_handler.
 *
 * The production header must expose:
 *
 *   ProposalAdmissionResult admit_proposal_payload(
 *       const bytearray_t &wire_payload,
 *       const PeerId &source_peer,
 *       HotStuffCore &structural_decoder,
 *       ProposalAdmissionCoordinator &coordinator);
 *
 * The function must parse bounded ProposalMetadata, structurally decode one
 * complete Block into detached temporary state, require exact end-of-input,
 * and require the decoded block hash to match ProposalMetadata::block_hash.
 * It may call coordinator.receive(...) only after every preflight check
 * succeeds. Structural decoding must not add the block to EntityStorage,
 * invoke HotStuff safety processing, authorize a vote, or cause any admission
 * effect. The real propose_handler must call this seam instead of calling the
 * coordinator directly. No Salticidae conn_t is needed by this contract.
 */
#if __has_include("hotstuff/proposal_body.h")
#define KAURI_HAS_REM_P05_PROPOSAL_BODY_API 1
#include <array>
#include <cstdint>
#include <stdexcept>
#include <utility>
#include <vector>

#include "hotstuff/proposal_body.h"
#include "support/commit_rule_fixture.h"
#else
#define KAURI_HAS_REM_P05_PROPOSAL_BODY_API 0
#endif

#if !KAURI_HAS_REM_P05_PROPOSAL_BODY_API

TEST_CASE("REM-P05-01 proposal body preflight is available",
          "[rem-p05-01][proposal-body][contract][red]")
{
    INFO("Missing include/hotstuff/proposal_body.h. REM-P05-01 requires "
         "a handler-facing structural decode seam before coordinator "
         "deduplication, relay, buffering, or active processing.");
    REQUIRE(KAURI_HAS_REM_P05_PROPOSAL_BODY_API == 1);
}

#else

namespace
{

using hotstuff::BufferedProposal;
using hotstuff::ConfigurationId;
using hotstuff::DataStream;
using hotstuff::EpochDefinition;
using hotstuff::EpochDefinitionInput;
using hotstuff::EpochStore;
using hotstuff::EpochTreeDefinition;
using hotstuff::EpochValidationContext;
using hotstuff::FutureProposalBuffer;
using hotstuff::PeerId;
using hotstuff::ProposalAdmissionCoordinator;
using hotstuff::ProposalAdmissionEffects;
using hotstuff::ProposalDisposition;
using hotstuff::ProposalKey;
using hotstuff::ProposalMetadata;
using hotstuff::ReplicaID;
using hotstuff::admit_proposal_payload;
using hotstuff::block_t;
using hotstuff::bytearray_t;
using hotstuff::test::CommitRuleCore;
using hotstuff::uint256_t;

uint256_t digest(const std::string &label)
{
    return DataStream(label).get_hash();
}

std::vector<ReplicaID> membership()
{
    return {0, 1, 2, 3, 4, 5, 6};
}

EpochTreeDefinition tree(std::uint32_t tree_id,
                         std::vector<ReplicaID> members)
{
    return EpochTreeDefinition{tree_id, 2, 2, std::move(members)};
}

EpochDefinitionInput epoch_zero_input()
{
    EpochDefinitionInput input;
    input.schema_version = hotstuff::kEpochDefinitionSchemaVersion;
    input.epoch_number = 0;
    input.previous_epoch_digest = uint256_t{};
    input.membership_digest =
        hotstuff::canonical_membership_digest(membership());
    input.trees = {
        tree(7, {0, 1, 2, 3, 4, 5, 6}),
        tree(11, {1, 0, 2, 3, 4, 5, 6})};
    input.activation_height = 10;
    input.generation_seed = 0x501;
    input.policy_version = "rem-p05-01-policy-v1";
    input.evidence_snapshot_id = "rem-p05-01-epoch-0";
    input.evidence_cutoff = 10;
    return input;
}

EpochDefinitionInput successor_input(const EpochDefinition &predecessor)
{
    EpochDefinitionInput input;
    input.schema_version = hotstuff::kEpochDefinitionSchemaVersion;
    input.epoch_number = predecessor.epoch_number() + 1;
    input.previous_epoch_digest = predecessor.epoch_digest();
    input.membership_digest = predecessor.membership_digest();
    input.trees = {
        tree(7, {2, 0, 1, 3, 4, 5, 6}),
        tree(11, {3, 0, 1, 2, 4, 5, 6})};
    input.activation_height = predecessor.activation_height() + 10;
    input.generation_seed = 0x502;
    input.policy_version = "rem-p05-01-policy-v1";
    input.evidence_snapshot_id = "rem-p05-01-epoch-1";
    input.evidence_cutoff = 20;
    return input;
}

EpochValidationContext validation_context()
{
    EpochValidationContext context;
    context.current_height = 0;
    context.minimum_activation_grace = 1;
    return context;
}

ConfigurationId configuration(const EpochDefinition &epoch,
                              std::uint32_t tree_id)
{
    return ConfigurationId{
        epoch.epoch_number(), tree_id, epoch.epoch_digest()};
}

struct StagedEpochs
{
    EpochStore store{membership()};
    const EpochDefinition *epoch0{nullptr};
    const EpochDefinition *epoch1{nullptr};

    StagedEpochs()
    {
        epoch0 = &store.stage(epoch_zero_input(), validation_context());
        epoch1 = &store.stage(
            successor_input(*epoch0), validation_context());
    }
};

class EffectSpy final : public ProposalAdmissionEffects
{
public:
    std::size_t relays{0};
    std::size_t active_processing{0};
    std::size_t local_votes{0};
    std::size_t expected_vote_states{0};
    std::size_t latency_deadlines{0};
    std::size_t aggregation_timers{0};
    std::size_t timeout_reports{0};
    std::vector<ProposalKey> relayed;
    std::vector<ProposalKey> processed;

    void relay_once(const BufferedProposal &proposal) override
    {
        ++relays;
        relayed.push_back(proposal.metadata.key());
    }

    void process_active(const BufferedProposal &proposal) override
    {
        ++active_processing;
        processed.push_back(proposal.metadata.key());
        relay_once(proposal);
    }

    void local_vote_authorized(const ProposalKey &) override
    {
        ++local_votes;
    }

    void create_expected_vote_state(const ProposalKey &) override
    {
        ++expected_vote_states;
    }

    bool start_latency_deadline(const ProposalKey &) override
    {
        ++latency_deadlines;
        return true;
    }

    void start_aggregation_timer(const ProposalKey &) override
    {
        ++aggregation_timers;
    }

    void emit_timeout_report(const ProposalKey &) override
    {
        ++timeout_reports;
    }
};

enum class InvalidBody
{
    truncated,
    malformed_encoding,
    trailing_bytes,
    hash_mismatch
};

const char *invalid_body_name(InvalidBody kind)
{
    switch (kind)
    {
    case InvalidBody::truncated:
        return "truncated";
    case InvalidBody::malformed_encoding:
        return "malformed encoding";
    case InvalidBody::trailing_bytes:
        return "trailing bytes";
    case InvalidBody::hash_mismatch:
        return "metadata/body hash mismatch";
    }
    return "unknown";
}

constexpr std::array<InvalidBody, 4> kInvalidBodies{
    InvalidBody::truncated,
    InvalidBody::malformed_encoding,
    InvalidBody::trailing_bytes,
    InvalidBody::hash_mismatch};

struct PayloadHarness
{
    StagedEpochs epochs;
    FutureProposalBuffer buffer;
    EffectSpy effects;
    CommitRuleCore source{7, 0};
    CommitRuleCore decoder{7, 0};
    ConfigurationId active;
    ProposalAdmissionCoordinator coordinator;
    block_t expected_block;
    block_t alternate_block;

    PayloadHarness()
        : active(configuration(*epochs.epoch0, 7)),
          coordinator(epochs.store, active, buffer, effects)
    {
        expected_block = source.add_block(
            source.get_genesis(), source.get_genesis());
        alternate_block = source.add_block(
            source.get_genesis(), source.get_genesis());
        if (expected_block->get_hash() == alternate_block->get_hash())
            throw std::logic_error("payload fixture blocks must differ");
    }

    ProposalMetadata metadata(const ConfigurationId &config,
                              ReplicaID proposer) const
    {
        return ProposalMetadata{
            config, expected_block->get_hash(), proposer};
    }

    bytearray_t wire(const ProposalMetadata &proposal_metadata,
                     const block_t &body) const
    {
        DataStream stream;
        proposal_metadata.serialize(stream);
        stream << *body;
        return static_cast<bytearray_t>(stream);
    }

    bytearray_t valid_wire(const ConfigurationId &config,
                           ReplicaID proposer) const
    {
        return wire(metadata(config, proposer), expected_block);
    }

    bytearray_t invalid_wire(InvalidBody kind,
                             const ConfigurationId &config,
                             ReplicaID proposer) const
    {
        const auto proposal_metadata = metadata(config, proposer);
        auto payload = valid_wire(config, proposer);
        switch (kind)
        {
        case InvalidBody::truncated:
            payload.pop_back();
            return payload;
        case InvalidBody::malformed_encoding:
        {
            DataStream malformed;
            proposal_metadata.serialize(malformed);
            malformed << hotstuff::htole(std::uint32_t{2})
                      << source.get_genesis()->get_hash();
            return static_cast<bytearray_t>(malformed);
        }
        case InvalidBody::trailing_bytes:
            payload.push_back(0xa5);
            return payload;
        case InvalidBody::hash_mismatch:
            return wire(proposal_metadata, alternate_block);
        }
        throw std::logic_error("unknown invalid body fixture");
    }

    const PeerId &peer(ReplicaID replica) const
    {
        return decoder.get_config().get_peer_id(replica);
    }

    bool decoder_has_fixture_block() const
    {
        return decoder.storage->is_blk_fetched(
                   expected_block->get_hash()) ||
               decoder.storage->is_blk_fetched(
                   alternate_block->get_hash());
    }
};

void check_no_effects(const PayloadHarness &harness)
{
    CHECK(harness.buffer.size() == 0);
    CHECK(harness.effects.relays == 0);
    CHECK(harness.effects.active_processing == 0);
    CHECK(harness.effects.local_votes == 0);
    CHECK(harness.effects.expected_vote_states == 0);
    CHECK(harness.effects.latency_deadlines == 0);
    CHECK(harness.effects.aggregation_timers == 0);
    CHECK(harness.effects.timeout_reports == 0);
    CHECK(harness.effects.relayed.empty());
    CHECK(harness.effects.processed.empty());
    CHECK_FALSE(harness.decoder_has_fixture_block());
}

void check_rejected_body_is_not_poisoning(
    PayloadHarness &harness,
    InvalidBody kind,
    const ConfigurationId &configuration_id,
    ReplicaID proposer,
    ProposalDisposition valid_disposition)
{
    INFO("invalid proposal body: " << invalid_body_name(kind));
    const auto metadata = harness.metadata(configuration_id, proposer);
    const auto key = metadata.key();
    const auto bad_payload =
        harness.invalid_wire(kind, configuration_id, proposer);

    const auto rejected = admit_proposal_payload(
        bad_payload,
        harness.peer(proposer),
        harness.decoder,
        harness.coordinator);
    CHECK(rejected.disposition == ProposalDisposition::rejected_malformed);
    CHECK(rejected.key == key);
    CHECK_FALSE(harness.buffer.contains(key));
    CHECK_FALSE(harness.coordinator.authorize_local_vote(key));
    check_no_effects(harness);

    const auto valid_payload =
        harness.valid_wire(configuration_id, proposer);
    const auto admitted = admit_proposal_payload(
        valid_payload,
        harness.peer(proposer),
        harness.decoder,
        harness.coordinator);
    CHECK(admitted.disposition == valid_disposition);
    CHECK(admitted.key == key);
    const std::size_t expected_relays =
        valid_disposition == ProposalDisposition::admitted_active ? 1 : 0;
    CHECK(harness.effects.relays == expected_relays);
    CHECK_FALSE(harness.decoder_has_fixture_block());

    const auto duplicate = admit_proposal_payload(
        valid_payload,
        harness.peer(proposer),
        harness.decoder,
        harness.coordinator);
    CHECK(duplicate.disposition == ProposalDisposition::duplicate);
    CHECK(duplicate.key == key);
    CHECK(harness.effects.relays == expected_relays);
    CHECK_FALSE(harness.decoder_has_fixture_block());
}

} // namespace

TEST_CASE("malformed future bodies reject before relay buffer or dedup",
          "[rem-p05-01][proposal-body][future]")
{
    for (const auto kind : kInvalidBodies)
    {
        PayloadHarness harness;
        const auto future = configuration(*harness.epochs.epoch1, 7);
        const auto key = harness.metadata(future, 2).key();

        check_rejected_body_is_not_poisoning(
            harness,
            kind,
            future,
            2,
            ProposalDisposition::buffered_future);

        CHECK(harness.buffer.size() == 1);
        CHECK(harness.buffer.contains(key));
        CHECK(harness.effects.active_processing == 0);
        CHECK(harness.effects.local_votes == 0);
        CHECK(harness.effects.expected_vote_states == 0);
        CHECK(harness.effects.latency_deadlines == 0);
        CHECK(harness.effects.aggregation_timers == 0);
        CHECK(harness.effects.timeout_reports == 0);
    }
}

TEST_CASE("malformed active bodies reject before active processing or vote",
          "[rem-p05-01][proposal-body][active]")
{
    for (const auto kind : kInvalidBodies)
    {
        PayloadHarness harness;
        const auto key = harness.metadata(harness.active, 0).key();

        check_rejected_body_is_not_poisoning(
            harness,
            kind,
            harness.active,
            0,
            ProposalDisposition::admitted_active);

        CHECK(harness.buffer.size() == 0);
        CHECK_FALSE(harness.buffer.contains(key));
        CHECK(harness.effects.active_processing == 1);
        CHECK(harness.effects.processed ==
              std::vector<ProposalKey>{key});
        CHECK(harness.effects.local_votes == 0);
        CHECK(harness.effects.expected_vote_states == 0);
        CHECK(harness.effects.latency_deadlines == 0);
        CHECK(harness.effects.aggregation_timers == 0);
        CHECK(harness.effects.timeout_reports == 0);

        REQUIRE(harness.coordinator.authorize_local_vote(key));
        CHECK(harness.effects.local_votes == 1);
        CHECK(harness.effects.expected_vote_states == 0);
        CHECK(harness.effects.latency_deadlines == 0);
        CHECK(harness.effects.aggregation_timers == 0);
    }
}

TEST_CASE("metadata parsing is bounded and cannot reserve a proposal key",
          "[rem-p05-01][proposal-body][metadata][bounds]")
{
    PayloadHarness harness;
    const auto future = configuration(*harness.epochs.epoch1, 7);
    const auto metadata = harness.metadata(future, 2);
    DataStream metadata_stream;
    metadata.serialize(metadata_stream);
    const auto prefix = static_cast<bytearray_t>(metadata_stream);
    REQUIRE(prefix.size() > 1);

    const std::array<std::size_t, 3> lengths{
        0, 1, prefix.size() - 1};
    for (const auto length : lengths)
    {
        INFO("truncated metadata length: " << length);
        const bytearray_t truncated(prefix.begin(), prefix.begin() + length);
        const auto rejected = admit_proposal_payload(
            truncated,
            harness.peer(2),
            harness.decoder,
            harness.coordinator);
        CHECK(rejected.disposition ==
              ProposalDisposition::rejected_malformed);
        check_no_effects(harness);
    }

    const auto key = metadata.key();
    const auto accepted = admit_proposal_payload(
        harness.valid_wire(future, 2),
        harness.peer(2),
        harness.decoder,
        harness.coordinator);
    CHECK(accepted.disposition == ProposalDisposition::buffered_future);
    CHECK(accepted.key == key);
    CHECK(harness.buffer.size() == 1);
    CHECK(harness.buffer.contains(key));
    CHECK(harness.effects.relays == 0);
}

TEST_CASE("unknown and digest mismatched metadata controls still reject",
          "[rem-p05-01][proposal-body][metadata][controls]")
{
    PayloadHarness harness;
    const ConfigurationId unknown{
        99, 7, digest("unknown-epoch-definition")};
    auto wrong_digest = harness.active;
    wrong_digest.epoch_digest = digest("divergent-epoch-zero");

    const auto unknown_result = admit_proposal_payload(
        harness.valid_wire(unknown, 0),
        harness.peer(0),
        harness.decoder,
        harness.coordinator);
    CHECK(unknown_result.disposition ==
          ProposalDisposition::rejected_unknown_configuration);
    CHECK(unknown_result.key == harness.metadata(unknown, 0).key());
    check_no_effects(harness);

    const auto digest_result = admit_proposal_payload(
        harness.valid_wire(wrong_digest, 0),
        harness.peer(0),
        harness.decoder,
        harness.coordinator);
    CHECK(digest_result.disposition ==
          ProposalDisposition::rejected_digest_mismatch);
    CHECK(digest_result.key ==
          harness.metadata(wrong_digest, 0).key());
    check_no_effects(harness);

    const auto valid = admit_proposal_payload(
        harness.valid_wire(harness.active, 0),
        harness.peer(0),
        harness.decoder,
        harness.coordinator);
    CHECK(valid.disposition == ProposalDisposition::admitted_active);
    CHECK(harness.effects.relays == 1);
    CHECK(harness.effects.active_processing == 1);
    CHECK(harness.effects.local_votes == 0);
    CHECK(harness.effects.expected_vote_states == 0);
    CHECK(harness.effects.latency_deadlines == 0);
    CHECK(harness.effects.aggregation_timers == 0);
    CHECK_FALSE(harness.decoder_has_fixture_block());
}

#endif
