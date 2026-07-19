#include <cstddef>
#include <cstdint>
#include <optional>
#include <string>
#include <utility>
#include <vector>

#include "catch.hpp"
#include "hotstuff/epoch_runtime_wiring.h"
#include "hotstuff/hotstuff.h"

#if __has_include("hotstuff/epoch_live_binding.h")
#include "hotstuff/epoch_live_binding.h"
#else
namespace hotstuff
{

struct PreparedEpochLiveRuntime
{
    std::uint64_t token{0};
    uint256_t canonical_plan_digest;
};

class EpochLiveEffects
{
public:
    virtual ~EpochLiveEffects() = default;
    virtual std::optional<PreparedEpochLiveRuntime> prepare(
        const EpochRuntimePlan &plan) = 0;
    virtual void discard(PreparedEpochLiveRuntime prepared) noexcept = 0;
    virtual void arm(
        PreparedEpochLiveRuntime prepared,
        const EpochRuntimeUpdate &update) noexcept = 0;
    virtual void arm_rotation(
        const EpochRuntimeRotation &update) noexcept = 0;
    virtual void apply_update(const EpochRuntimeUpdate &update) noexcept = 0;
};

class EpochManagerEgress
{
public:
    virtual ~EpochManagerEgress() = default;
    virtual void send_stage_ack(const StageAck &acknowledgement) noexcept = 0;
    virtual void send_activation_status(const ActivationStatus &status) noexcept = 0;
};

class EpochContributionContinuations
{
public:
    virtual ~EpochContributionContinuations() = default;
    virtual void continue_vote(
        const bytearray_t &body,
        const AuthenticatedEpochPeer &peer) noexcept = 0;
    virtual void continue_relay(
        const bytearray_t &body,
        const AuthenticatedEpochPeer &peer) noexcept = 0;
};

class HotStuffEpochLiveBinding final
{
public:
    HotStuffEpochLiveBinding(
        HotStuffEpochRuntimeAdapter &adapter,
        ReplicaEpochActivation &activation,
        EpochLiveEffects &live_effects,
        EpochManagerEgress &manager_egress,
        EpochContributionContinuations &continuations) noexcept;

    ReplicaStageIngressResult handle_stage(
        MsgStageEpochDefinition &&message,
        const AuthenticatedEpochPeer &peer,
        const EpochValidationContext &validation_context);
    ReplicaArmIngressResult handle_arm(
        MsgArmActivation &&message,
        const AuthenticatedEpochPeer &peer);
    EpochCommitIngressResult on_predecessor_commit(
        std::uint64_t height,
        const uint256_t &predecessor_digest) noexcept;
    EpochCommitIngressResult replay_blocked_commit() noexcept;
    EpochRotationResult rotate_to_tree(std::uint32_t tree_id) noexcept;
    EpochConsensusIngressResult handle_proposal(
        MsgPropose &&message,
        const AuthenticatedEpochPeer &peer) noexcept;
    EpochConsensusIngressResult handle_vote(
        MsgVote &&message,
        const AuthenticatedEpochPeer &peer) noexcept;
    EpochConsensusIngressResult handle_relay(
        MsgRelay &&message,
        const AuthenticatedEpochPeer &peer) noexcept;

private:
    HotStuffEpochRuntimeAdapter &adapter_;
    ReplicaEpochActivation &activation_;
    EpochLiveEffects &live_effects_;
    EpochManagerEgress &manager_egress_;
    EpochContributionContinuations &continuations_;
};

bytearray_t adaptive_epoch_consensus_message(
    const EpochActivationEffect &active,
    EpochConsensusWireKind kind,
    const ProposalKey &key,
    ReplicaID originator,
    ReplicaID proposer,
    const bytearray_t &body,
    const EpochWireLimits &limits);

} // namespace hotstuff
#endif

namespace
{

using namespace hotstuff;

constexpr std::uint64_t kActivationHeight = 1200;

uint256_t digest(const char *label)
{
    return DataStream(std::string(label)).get_hash();
}

std::vector<ReplicaID> membership()
{
    return {0, 1, 2, 3, 4, 5, 6};
}

EpochDefinitionInput epoch_input(
    std::uint32_t epoch_number,
    const uint256_t &previous = {})
{
    EpochDefinitionInput input;
    input.epoch_number = epoch_number;
    input.previous_epoch_digest = previous;
    input.membership_digest = canonical_membership_digest(membership());
    input.trees = epoch_number == 0
                      ? std::vector<EpochTreeDefinition>{
                            {0, 2, 2, membership()},
                            {1, 2, 2, {1, 0, 2, 3, 4, 5, 6}}}
                      : std::vector<EpochTreeDefinition>{
                            {0, 2, 2, {2, 0, 1, 3, 4, 5, 6}},
                            {1, 2, 2, {3, 0, 1, 2, 4, 5, 6}}};
    input.activation_height = epoch_number == 0 ? 0 : kActivationHeight;
    input.generation_seed = 0xD110 + epoch_number;
    input.policy_version = "d11-live-binding-v1";
    input.evidence_snapshot_id = "d11-live-binding-window";
    input.evidence_cutoff = epoch_number == 0 ? 0 : 50;
    return input;
}

EpochDefinitionInput epoch_v2_input(
    std::uint32_t epoch_number,
    const uint256_t &previous = {})
{
    auto input = epoch_input(epoch_number, previous);
    input.schema_version = kEpochDefinitionSchemaVersionV2;
    input.activation_height = 0;
    input.generation_seed = 0;
    input.policy_version = "c08-runtime-v2";
    input.evidence_snapshot_id =
        "c08-runtime-v2-" + std::to_string(epoch_number);
    input.evidence_cutoff = epoch_number;
    input.epoch_digest.reset();
    return input;
}

EpochValidationContext validation_context(std::uint32_t epoch_number)
{
    return epoch_number == 0 ? EpochValidationContext{0, 0, {}}
                             : EpochValidationContext{1000, 100, {}};
}

EpochWireLimits limits()
{
    return {8192, 8, 16, 128};
}

StageEpochDefinition successor_stage(const EpochDefinition &active)
{
    auto input = epoch_input(active.epoch_number() + 1, active.epoch_digest());
    const auto successor_digest = compute_epoch_digest(input);
    input.epoch_digest = successor_digest;
    return {
        kEpochWireSchemaVersion,
        EpochProtocolMode::adaptive_v1,
        {active.epoch_number(),
         active.epoch_digest(),
         input.epoch_number,
         successor_digest,
         input.activation_height},
        std::move(input)};
}

ArmActivation arm_for(const StageEpochDefinition &stage)
{
    return {
        kEpochWireSchemaVersion,
        EpochProtocolMode::adaptive_v1,
        stage.activation};
}

EpochConsensusEnvelope envelope(
    const EpochActivationEffect &active,
    EpochConsensusWireKind kind,
    ReplicaID originator,
    bytearray_t body,
    const char *label)
{
    return {
        active.configuration,
        active.generation,
        digest(label),
        originator,
        0,
        std::move(body),
        kEpochConsensusWireSchemaVersion,
        EpochProtocolMode::adaptive_v1,
        kind};
}

template <typename Message>
Message consensus_message(const EpochConsensusEnvelope &value)
{
    return Message(DataStream(encode_epoch_consensus_envelope(value, limits())));
}

class ProposalEffectsSpy final : public ProposalAdmissionEffects
{
public:
    std::size_t relay_count{0};
    std::size_t process_count{0};
    bytearray_t processed_body;

    void relay_once(const BufferedProposal &) override { ++relay_count; }
    void process_active(const BufferedProposal &proposal) override
    {
        ++process_count;
        processed_body = hotstuff_epoch_processing_payload(proposal);
    }
    void local_vote_authorized(const ProposalKey &) override {}
    void create_expected_vote_state(const ProposalKey &) override {}
    void start_latency_deadline(const ProposalKey &) override {}
    void start_aggregation_timer(const ProposalKey &) override {}
    void emit_timeout_report(const ProposalKey &) override {}
};

class BodyValidatorSpy final : public EpochConsensusBodyValidator
{
    static bool valid(const EpochConsensusEnvelope &value)
    {
        return value.body.empty() || value.body.front() != 0xEE;
    }

public:
    bool validate_proposal(
        const EpochConsensusEnvelope &value,
        const AuthenticatedEpochPeer &) override { return valid(value); }
    bool validate_vote(
        const EpochConsensusEnvelope &value,
        const AuthenticatedEpochPeer &peer) override
    {
        return valid(value) && peer.replica_id == value.originator;
    }
    bool validate_relay(
        const EpochConsensusEnvelope &value,
        const AuthenticatedEpochPeer &peer) override
    {
        return valid(value) && peer.replica_id == value.originator;
    }
};

class EmptyFutureStore final : public RetryableFutureProposalStore
{
public:
    bool insert(BufferedProposal) override { return false; }
    std::optional<FutureProposalClaim> claim_next(
        const ConfigurationId &) override { return std::nullopt; }
    void process_active(const FutureProposalClaim &) override {}
    void acknowledge(std::uint64_t) noexcept override {}
    void release(std::uint64_t) noexcept override {}
    void complete(const ConfigurationId &) noexcept override {}
    std::size_t size() const noexcept override { return 0; }
};

class LiveEffectsSpy final : public EpochLiveEffects
{
public:
    bool prepare_allowed{true};
    std::size_t prepare_count{0};
    std::size_t discard_count{0};
    std::size_t arm_count{0};
    std::size_t rotation_arm_count{0};
    std::size_t apply_count{0};
    bool armed_before_apply{false};
    bool active_plan_armed{false};
    bool rotation_armed{false};
    std::optional<PreparedEpochLiveRuntime> prepared;
    std::optional<EpochRuntimePlan> prepared_plan;
    std::optional<PreparedEpochLiveRuntime> armed;
    std::optional<EpochRuntimeUpdate> update;

    std::optional<PreparedEpochLiveRuntime> prepare(
        const EpochRuntimePlan &plan) override
    {
        ++prepare_count;
        if (!prepare_allowed)
            return std::nullopt;
        prepared_plan.emplace(plan);
        prepared.emplace(PreparedEpochLiveRuntime{
            prepare_count, plan.canonical_digest});
        return prepared;
    }

    void discard(PreparedEpochLiveRuntime value) noexcept override
    {
        ++discard_count;
        if (prepared && prepared->token == value.token)
            prepared.reset();
    }

    void arm(
        PreparedEpochLiveRuntime value,
        const EpochRuntimeUpdate &) noexcept override
    {
        ++arm_count;
        if (prepared && prepared->token == value.token)
        {
            armed.emplace(value);
            active_plan_armed = true;
        }
    }

    void arm_rotation(const EpochRuntimeRotation &) noexcept override
    {
        ++rotation_arm_count;
        rotation_armed = active_plan_armed;
    }

    void apply_update(const EpochRuntimeUpdate &value) noexcept override
    {
        ++apply_count;
        armed_before_apply = armed.has_value() || rotation_armed;
        armed.reset();
        rotation_armed = false;
        update.emplace(value);
    }
};

class ManagerEgressSpy final : public EpochManagerEgress
{
public:
    std::vector<StageAck> acknowledgements;
    std::vector<ActivationStatus> statuses;

    void send_stage_ack(const StageAck &value) noexcept override
    {
        acknowledgements.push_back(value);
    }
    void send_activation_status(const ActivationStatus &value) noexcept override
    {
        statuses.push_back(value);
    }
};

class ContinuationsSpy final : public EpochContributionContinuations
{
public:
    std::vector<bytearray_t> votes;
    std::vector<bytearray_t> relays;

    void continue_vote(
        const bytearray_t &body,
        const AuthenticatedEpochPeer &) noexcept override
    {
        votes.push_back(body);
    }
    void continue_relay(
        const bytearray_t &body,
        const AuthenticatedEpochPeer &) noexcept override
    {
        relays.push_back(body);
    }
};

struct Harness
{
    EpochStore store{membership()};
    const EpochDefinition &epoch0;
    ReplicaEpochActivation activation;
    FutureProposalBuffer future;
    ProposalContextLifecycle contexts;
    ProposalEffectsSpy proposal_effects;
    ProposalAdmissionCoordinator admission;
    EmptyFutureStore retryable_future;
    BodyValidatorSpy validator;
    LiveEffectsSpy live_effects;
    HotStuffEpochRuntimeTransaction transaction;
    HotStuffEpochRuntimeAdapter adapter;
    ManagerEgressSpy manager_egress;
    ContinuationsSpy continuations;
    HotStuffEpochLiveBinding binding;

    Harness()
        : epoch0(store.stage(epoch_input(0), validation_context(0))),
          activation(store, epoch0, 0, 0),
          admission(
              store,
              activation.active_effect().configuration,
              future,
              proposal_effects),
          transaction(admission, contexts, live_effects),
          adapter(
              activation,
              contexts,
              admission,
              retryable_future,
              validator,
              EpochProtocolMode::adaptive_v1,
              limits(),
              transaction),
          binding(
              adapter,
              activation,
              live_effects,
              manager_egress,
              continuations)
    {
        contexts.activate_configuration(activation.active_effect().configuration);
    }

    ReplicaStageIngressResult stage(const StageEpochDefinition &value)
    {
        return binding.handle_stage(
            MsgStageEpochDefinition(value, limits()),
            AuthenticatedEpochPeer::manager(),
            validation_context(value.definition.epoch_number));
    }

    ReplicaArmIngressResult arm(const StageEpochDefinition &value)
    {
        return binding.handle_arm(
            MsgArmActivation(arm_for(value), limits()),
            AuthenticatedEpochPeer::manager());
    }
};

struct V2Harness
{
    static const EpochDefinition &stage_successor(
        EpochStore &store,
        const EpochDefinition &active)
    {
        const auto staged = store.stage_available_v2(
            epoch_v2_input(
                active.epoch_number() + 1,
                active.epoch_digest()),
            active);
        if (staged.definition == nullptr)
            throw std::logic_error("failed to stage v2 successor fixture");
        return *staged.definition;
    }

    EpochStore store{membership()};
    const EpochDefinition &epoch0;
    const EpochDefinition &epoch1;
    ReplicaEpochActivation activation;
    FutureProposalBuffer future;
    ProposalContextLifecycle contexts;
    ProposalEffectsSpy proposal_effects;
    ProposalAdmissionCoordinator admission;
    EmptyFutureStore retryable_future;
    BodyValidatorSpy validator;
    LiveEffectsSpy live_effects;
    HotStuffEpochRuntimeTransaction transaction;
    HotStuffEpochRuntimeAdapter adapter;
    ManagerEgressSpy manager_egress;
    ContinuationsSpy continuations;
    HotStuffEpochLiveBinding binding;

    V2Harness()
        : epoch0(store.stage(epoch_v2_input(0), validation_context(0))),
          epoch1(stage_successor(store, epoch0)),
          activation(store, epoch0, 0, 0),
          admission(
              store,
              activation.active_effect().configuration,
              future,
              proposal_effects),
          transaction(admission, contexts, live_effects),
          adapter(
              activation,
              contexts,
              admission,
              retryable_future,
              validator,
              EpochProtocolMode::adaptive_v2,
              limits(),
              transaction),
          binding(
              adapter,
              activation,
              live_effects,
              manager_egress,
              continuations)
    {
        contexts.activate_configuration(
            activation.active_effect().configuration);
    }

    AuthorizedEpochChange command(std::uint64_t delay) const
    {
        AuthorizedEpochChange value;
        value.payload.successor_epoch_number = epoch1.epoch_number();
        value.payload.predecessor_epoch_digest = epoch0.epoch_digest();
        value.payload.successor_epoch_digest = epoch1.epoch_digest();
        value.payload.activation_delay_blocks = delay;
        return value;
    }
};

} // namespace

TEST_CASE("live binding emits only a successful stage acknowledgement",
          "[rem-d11][epoch-live-binding][stage][intentional-red]")
{
    Harness harness;
    const auto stage = successor_stage(harness.epoch0);

    const auto accepted = harness.stage(stage);
    REQUIRE(accepted.error == EpochIngressError::none);
    REQUIRE(accepted.acknowledgement.has_value());
    REQUIRE(harness.manager_egress.acknowledgements.size() == 1);
    CHECK(harness.manager_egress.acknowledgements.front().replica_id == 0);
    CHECK(harness.manager_egress.acknowledgements.front().activation ==
          stage.activation);

    const auto rejected = harness.binding.handle_stage(
        MsgStageEpochDefinition(stage, limits()),
        AuthenticatedEpochPeer::replica(1),
        validation_context(1));
    CHECK(rejected.error == EpochIngressError::unauthorized_peer);
    CHECK(harness.manager_egress.acknowledgements.size() == 1);
    CHECK(harness.live_effects.prepare_count == 1);
}

TEST_CASE("v2 runtime preparation is schedule-free and never arms",
          "[c08][epoch-live-binding][adaptive-v2][prepare]")
{
    V2Harness harness;

    REQUIRE(harness.adapter.prepare_committed_v2(harness.epoch1) ==
            EpochIngressError::none);
    CHECK(harness.live_effects.prepare_count == 1);
    CHECK(harness.live_effects.arm_count == 0);
    CHECK(harness.live_effects.apply_count == 0);
    CHECK(harness.manager_egress.acknowledgements.empty());
    CHECK(harness.manager_egress.statuses.empty());
    CHECK_FALSE(harness.activation.committed_v2_record().has_value());

    REQUIRE(harness.live_effects.prepared_plan.has_value());
    const auto &plan = *harness.live_effects.prepared_plan;
    CHECK(plan.protocol_mode == EpochProtocolMode::adaptive_v2);
    CHECK(plan.epoch_number == harness.epoch1.epoch_number());
    CHECK(plan.epoch_digest == harness.epoch1.epoch_digest());
    CHECK(plan.canonical_stage ==
          harness.epoch1.canonical_serialization());
    CHECK(plan.canonical_digest == harness.epoch1.epoch_digest());
    REQUIRE(plan.trees.size() == harness.epoch1.trees().size());
    for (const auto &tree : plan.trees)
    {
        CHECK(tree.configuration.epoch_number ==
              harness.epoch1.epoch_number());
        CHECK(tree.configuration.epoch_digest ==
              harness.epoch1.epoch_digest());
    }

    CHECK(harness.adapter.prepare_committed_v2(harness.epoch1) ==
          EpochIngressError::none);
    CHECK(harness.live_effects.prepare_count == 1);
}

TEST_CASE("failed v2 preparation has no activation side effects",
          "[c08][epoch-live-binding][adaptive-v2][prepare][failure]")
{
    V2Harness harness;
    harness.live_effects.prepare_allowed = false;

    CHECK(harness.adapter.prepare_committed_v2(harness.epoch1) ==
          EpochIngressError::runtime_preparation_failed);
    CHECK(harness.live_effects.prepare_count == 1);
    CHECK(harness.live_effects.arm_count == 0);
    CHECK(harness.live_effects.apply_count == 0);
    CHECK_FALSE(harness.activation.committed_v2_record().has_value());
    CHECK(harness.activation.active_effect().definition == &harness.epoch0);

    harness.live_effects.prepare_allowed = true;
    CHECK(harness.adapter.prepare_committed_v2(harness.epoch1) ==
          EpochIngressError::none);
    CHECK(harness.live_effects.prepare_count == 2);
    CHECK_FALSE(harness.activation.committed_v2_record().has_value());
}

TEST_CASE("committed v2 failure discards preparation and pauses admission",
          "[c08][epoch-live-binding][adaptive-v2][prepare][fail-closed]")
{
    V2Harness harness;
    REQUIRE(harness.adapter.prepare_committed_v2(harness.epoch1) ==
            EpochIngressError::none);
    REQUIRE(harness.live_effects.prepared.has_value());

    harness.adapter.fail_committed_v2(
        ActivationBlockReason::invalid_activation_record);

    CHECK(harness.live_effects.discard_count == 1);
    CHECK_FALSE(harness.live_effects.prepared.has_value());
    CHECK(harness.activation.blocked_reason() ==
          ActivationBlockReason::invalid_activation_record);
    CHECK_FALSE(harness.activation.admits_new_proposals());

    harness.adapter.fail_committed_v2(
        ActivationBlockReason::invalid_activation_record);
    CHECK(harness.live_effects.discard_count == 1);
    CHECK_FALSE(harness.activation.admits_new_proposals());
}

TEST_CASE("v2 activation requires the exact prepared successor runtime",
          "[c08][epoch-live-binding][adaptive-v2][prepare][identity]")
{
    constexpr std::uint64_t commit_height = 40;
    constexpr std::uint64_t delay = 5;
    V2Harness harness;
    EpochStore other_store{membership()};
    const auto &other_epoch0 = other_store.stage(
        epoch_v2_input(0), validation_context(0));
    auto other_input = epoch_v2_input(1, other_epoch0.epoch_digest());
    other_input.policy_version = "c08-runtime-v2-other";
    other_input.evidence_snapshot_id = "c08-runtime-v2-other-1";
    const auto other_staged = other_store.stage_available_v2(
        other_input, other_epoch0);
    REQUIRE(other_staged.definition != nullptr);
    REQUIRE(other_staged.definition->epoch_digest() !=
            harness.epoch1.epoch_digest());
    REQUIRE(harness.adapter.prepare_committed_v2(
                *other_staged.definition) == EpochIngressError::none);
    REQUIRE(harness.activation.record_committed_v2(
                harness.command(delay), commit_height)
                .disposition == ActivationRecordDisposition::recorded);

    const auto result = harness.binding.on_v2_post_block_commit(
        commit_height + delay, harness.epoch0.epoch_digest());
    CHECK(result.error == EpochIngressError::missing_prepared_runtime);
    CHECK(result.transition == ActivationTransition::waiting);
    CHECK(harness.activation.active_effect().definition == &harness.epoch0);
    CHECK(harness.live_effects.arm_count == 0);
    CHECK(harness.live_effects.apply_count == 0);
    CHECK(harness.live_effects.discard_count == 1);

    REQUIRE(harness.adapter.prepare_committed_v2(harness.epoch1) ==
            EpochIngressError::none);
    const auto retried = harness.binding.on_v2_post_block_commit(
        commit_height + delay, harness.epoch0.epoch_digest());
    CHECK(retried.error == EpochIngressError::none);
    CHECK(retried.transition == ActivationTransition::activated);
    CHECK(harness.activation.active_effect().definition == &harness.epoch1);
    CHECK(harness.live_effects.arm_count == 1);
    CHECK(harness.live_effects.apply_count == 1);
}

TEST_CASE("missing v2 runtime leaves the exact boundary retryable",
          "[c08][epoch-live-binding][adaptive-v2][post-block]")
{
    constexpr std::uint64_t commit_height = 40;
    constexpr std::uint64_t delay = 5;
    constexpr std::uint64_t activation_height = commit_height + delay;
    V2Harness harness;
    const auto command = harness.command(delay);
    REQUIRE(harness.activation.record_committed_v2(
                command, commit_height)
                .disposition == ActivationRecordDisposition::recorded);

    const auto missing = harness.binding.on_v2_post_block_commit(
        activation_height, harness.epoch0.epoch_digest());
    CHECK(missing.error == EpochIngressError::missing_prepared_runtime);
    CHECK(missing.transition == ActivationTransition::waiting);
    CHECK(harness.activation.active_effect().definition == &harness.epoch0);
    CHECK(harness.live_effects.arm_count == 0);
    CHECK(harness.live_effects.apply_count == 0);

    REQUIRE(harness.adapter.prepare_committed_v2(harness.epoch1) ==
            EpochIngressError::none);
    const auto activated = harness.binding.on_v2_post_block_commit(
        activation_height, harness.epoch0.epoch_digest());
    REQUIRE(activated.error == EpochIngressError::none);
    REQUIRE(activated.transition == ActivationTransition::activated);
    REQUIRE(activated.update.has_value());
    CHECK(activated.update->activation.definition == &harness.epoch1);
    CHECK(harness.activation.active_effect().definition == &harness.epoch1);
    CHECK(harness.live_effects.arm_count == 1);
    CHECK(harness.live_effects.apply_count == 1);
    CHECK(harness.live_effects.armed_before_apply);
    CHECK(harness.manager_egress.statuses.empty());

    const auto repeated = harness.binding.on_v2_post_block_commit(
        activation_height, harness.epoch0.epoch_digest());
    CHECK(repeated.error == EpochIngressError::none);
    CHECK(repeated.transition == ActivationTransition::already_active);
    CHECK(harness.live_effects.arm_count == 1);
    CHECK(harness.live_effects.apply_count == 1);
}

TEST_CASE("exact commit applies the prepared update and reports activation once",
          "[rem-d11][epoch-live-binding][commit][intentional-red]")
{
    Harness harness;
    const auto stage = successor_stage(harness.epoch0);
    REQUIRE(harness.stage(stage).acknowledgement.has_value());
    REQUIRE(harness.arm(stage).disposition == ReplicaArmDisposition::armed);
    REQUIRE(harness.live_effects.prepare_count == 1);
    REQUIRE(harness.live_effects.arm_count == 0);

    const auto early = harness.binding.on_predecessor_commit(
        kActivationHeight - 1, harness.epoch0.epoch_digest());
    CHECK(early.transition == ActivationTransition::waiting);
    CHECK(harness.live_effects.arm_count == 0);
    CHECK(harness.live_effects.apply_count == 0);
    CHECK(harness.manager_egress.statuses.empty());

    const auto activated = harness.binding.on_predecessor_commit(
        kActivationHeight, harness.epoch0.epoch_digest());
    REQUIRE(activated.transition == ActivationTransition::activated);
    REQUIRE(activated.update.has_value());
    CHECK(harness.live_effects.arm_count == 1);
    CHECK(harness.live_effects.apply_count == 1);
    CHECK(harness.live_effects.armed_before_apply);
    REQUIRE(harness.live_effects.update.has_value());
    CHECK(harness.live_effects.update->activation.configuration ==
          activated.update->activation.configuration);
    REQUIRE(harness.manager_egress.statuses.size() == 1);
    CHECK(harness.manager_egress.statuses.front().activation == stage.activation);
    CHECK(harness.manager_egress.statuses.front().recovery_need ==
          ActivationRecoveryNeed::none);

    const auto repeated = harness.binding.on_predecessor_commit(
        kActivationHeight, harness.epoch0.epoch_digest());
    CHECK(repeated.transition == ActivationTransition::already_active);
    const auto replayed = harness.binding.replay_blocked_commit();
    CHECK(replayed.transition != ActivationTransition::activated);
    CHECK(harness.live_effects.arm_count == 1);
    CHECK(harness.live_effects.apply_count == 1);
    CHECK(harness.manager_egress.statuses.size() == 1);
}

TEST_CASE("blocked commit reports nothing and a recovered replay reports once",
          "[rem-d11][epoch-live-binding][replay][intentional-red]")
{
    SECTION("permanent block never applies or reports")
    {
        Harness harness;
        const auto stage = successor_stage(harness.epoch0);
        REQUIRE(harness.stage(stage).acknowledgement.has_value());
        REQUIRE(harness.arm(stage).disposition == ReplicaArmDisposition::armed);

        const auto blocked = harness.binding.on_predecessor_commit(
            kActivationHeight, digest("wrong-predecessor"));
        CHECK(blocked.transition == ActivationTransition::blocked);
        CHECK(harness.live_effects.arm_count == 0);
        CHECK(harness.live_effects.apply_count == 0);
        CHECK(harness.live_effects.discard_count == 1);
        CHECK(harness.manager_egress.statuses.empty());
        CHECK(harness.binding.replay_blocked_commit().transition !=
              ActivationTransition::activated);
        CHECK(harness.manager_egress.statuses.empty());
    }

    SECTION("missing arm can recover through replay")
    {
        Harness harness;
        const auto stage = successor_stage(harness.epoch0);
        REQUIRE(harness.stage(stage).acknowledgement.has_value());

        const auto blocked = harness.binding.on_predecessor_commit(
            kActivationHeight, harness.epoch0.epoch_digest());
        REQUIRE(blocked.transition == ActivationTransition::blocked);
        CHECK(blocked.blocked_reason == ActivationBlockReason::missing_arm);
        CHECK(harness.live_effects.apply_count == 0);
        CHECK(harness.manager_egress.statuses.empty());

        REQUIRE(harness.arm(stage).disposition == ReplicaArmDisposition::armed);
        const auto recovered = harness.binding.replay_blocked_commit();
        REQUIRE(recovered.transition == ActivationTransition::activated);
        CHECK(harness.live_effects.arm_count == 1);
        CHECK(harness.live_effects.apply_count == 1);
        CHECK(harness.manager_egress.statuses.size() == 1);
        CHECK(harness.binding.replay_blocked_commit().transition !=
              ActivationTransition::activated);
        CHECK(harness.manager_egress.statuses.size() == 1);
    }
}

TEST_CASE("tree rotation applies a prepared view without activation egress",
          "[rem-d11][epoch-live-binding][rotation][intentional-red]")
{
    Harness harness;
    const auto stage = successor_stage(harness.epoch0);
    REQUIRE(harness.stage(stage).acknowledgement.has_value());
    REQUIRE(harness.arm(stage).disposition == ReplicaArmDisposition::armed);
    REQUIRE(harness.binding.on_predecessor_commit(
                kActivationHeight, harness.epoch0.epoch_digest())
                .transition == ActivationTransition::activated);
    REQUIRE(harness.live_effects.prepare_count == 1);
    REQUIRE(harness.live_effects.apply_count == 1);
    REQUIRE(harness.manager_egress.statuses.size() == 1);

    const auto before = harness.activation.active_effect();
    const auto rotated = harness.binding.rotate_to_tree(1);
    REQUIRE(rotated.error == EpochIngressError::none);
    REQUIRE(rotated.update.has_value());
    CHECK(rotated.update->activation.configuration.tree_id == 1);
    CHECK(rotated.update->activation.generation > before.generation);
    CHECK(harness.live_effects.prepare_count == 1);
    CHECK(harness.live_effects.rotation_arm_count == 1);
    CHECK(harness.live_effects.apply_count == 2);
    CHECK(harness.live_effects.armed_before_apply);
    CHECK(harness.manager_egress.statuses.size() == 1);

    const auto duplicate = harness.binding.rotate_to_tree(1);
    CHECK_FALSE(duplicate.update.has_value());
    CHECK(harness.live_effects.rotation_arm_count == 1);
    CHECK(harness.live_effects.apply_count == 2);
    CHECK(harness.manager_egress.statuses.size() == 1);

    const auto rejected = harness.binding.rotate_to_tree(99);
    CHECK(rejected.error == EpochIngressError::state_rejected);
    CHECK_FALSE(rejected.update.has_value());
    CHECK(harness.live_effects.rotation_arm_count == 1);
    CHECK(harness.live_effects.apply_count == 2);
    CHECK(harness.manager_egress.statuses.size() == 1);
}

TEST_CASE("accepted adaptive bodies enter native continuations exactly once",
          "[rem-d11][epoch-live-binding][consensus][intentional-red]")
{
    Harness harness;
    const auto active = harness.activation.active_effect();
    const bytearray_t proposal_body{0xA1, 0xB2};
    const auto proposal = envelope(
        active,
        EpochConsensusWireKind::proposal,
        4,
        proposal_body,
        "live-proposal");

    const auto admitted = harness.binding.handle_proposal(
        consensus_message<MsgPropose>(proposal),
        AuthenticatedEpochPeer::replica(4));
    REQUIRE(admitted.permission == EpochConsensusPermission::admit_or_buffer);
    CHECK(admitted.admission_disposition == ProposalDisposition::admitted_active);
    CHECK(harness.proposal_effects.relay_count == 1);
    CHECK(harness.proposal_effects.process_count == 1);
    CHECK(harness.proposal_effects.processed_body == proposal_body);

    const auto duplicate = harness.binding.handle_proposal(
        consensus_message<MsgPropose>(proposal),
        AuthenticatedEpochPeer::replica(4));
    CHECK(duplicate.admission_disposition == ProposalDisposition::duplicate);
    CHECK(harness.proposal_effects.process_count == 1);

    const bytearray_t vote_body{0xC1, 0xD2};
    const auto vote = envelope(
        active, EpochConsensusWireKind::vote, 4, vote_body, "live-vote");
    REQUIRE(harness.binding.handle_vote(
                consensus_message<MsgVote>(vote),
                AuthenticatedEpochPeer::replica(4))
                .permission == EpochConsensusPermission::accept_contribution);
    REQUIRE(harness.continuations.votes.size() == 1);
    CHECK(harness.continuations.votes.front() == vote_body);

    auto rejected_vote = vote;
    rejected_vote.body = {0xEE};
    CHECK(harness.binding.handle_vote(
              consensus_message<MsgVote>(rejected_vote),
              AuthenticatedEpochPeer::replica(4))
              .permission == EpochConsensusPermission::rejected_identity);
    CHECK(harness.continuations.votes.size() == 1);

    const bytearray_t relay_body{0xE1, 0xF2};
    const auto relay = envelope(
        active, EpochConsensusWireKind::relay, 5, relay_body, "live-relay");
    REQUIRE(harness.binding.handle_relay(
                consensus_message<MsgRelay>(relay),
                AuthenticatedEpochPeer::replica(5))
                .permission == EpochConsensusPermission::accept_contribution);
    REQUIRE(harness.continuations.relays.size() == 1);
    CHECK(harness.continuations.relays.front() == relay_body);

    CHECK(harness.binding.handle_relay(
              consensus_message<MsgRelay>(relay),
              AuthenticatedEpochPeer::replica(6))
              .permission == EpochConsensusPermission::rejected_identity);
    CHECK(harness.continuations.relays.size() == 1);
}

TEST_CASE("boundary contributions retain the draining epoch generation",
          "[rem-d11][epoch-live-binding][draining][intentional-red]")
{
    Harness harness;
    const auto predecessor = harness.activation.active_effect();
    const ProposalKey predecessor_key{
        predecessor.configuration,
        digest("draining-boundary")};
    const auto *predecessor_tree = harness.store.find_tree(
        predecessor.configuration.epoch_number,
        predecessor.configuration.tree_id);
    REQUIRE(predecessor_tree != nullptr);
    const auto metadata = make_exact_proposal_context_metadata(
        predecessor_key,
        0,
        predecessor_tree->members_breadth_first,
        predecessor_tree->fanout,
        predecessor_tree->pipeline_stretch,
        5);
    REQUIRE(metadata.has_value());
    REQUIRE(harness.contexts.admit_remote(*metadata).has_value());

    const auto stage = successor_stage(harness.epoch0);
    REQUIRE(harness.stage(stage).acknowledgement.has_value());
    REQUIRE(harness.arm(stage).disposition == ReplicaArmDisposition::armed);
    REQUIRE(harness.binding.on_predecessor_commit(
                kActivationHeight, harness.epoch0.epoch_digest())
                .transition == ActivationTransition::activated);
    REQUIRE(harness.activation.active_effect().configuration !=
            predecessor.configuration);

    const auto active = harness.activation.active_effect();
    auto active_vote = envelope(
        active,
        EpochConsensusWireKind::vote,
        4,
        {0xD1, 0x6F},
        "active-pre-proposal-vote");
    REQUIRE(active.definition != nullptr);
    active_vote.proposer = active.definition->trees()
                               .front()
                               .members_breadth_first.front();
    CHECK(harness.binding.handle_vote(
              consensus_message<MsgVote>(active_vote),
              AuthenticatedEpochPeer::replica(4))
              .permission == EpochConsensusPermission::accept_contribution);
    REQUIRE(harness.continuations.votes.size() == 1);

    const bytearray_t vote_body{0xD1, 0x70};
    auto vote = envelope(
        predecessor,
        EpochConsensusWireKind::vote,
        4,
        vote_body,
        "draining-boundary");
    vote.block_hash = predecessor_key.block_hash;
    const auto accepted_vote = harness.binding.handle_vote(
        consensus_message<MsgVote>(vote),
        AuthenticatedEpochPeer::replica(4));
    CHECK(accepted_vote.error == EpochIngressError::none);
    CHECK(accepted_vote.permission ==
          EpochConsensusPermission::accept_contribution);
    REQUIRE(harness.continuations.votes.size() == 2);
    CHECK(harness.continuations.votes.back() == vote_body);

    const bytearray_t relay_body{0xD1, 0x71};
    auto relay = vote;
    relay.kind = EpochConsensusWireKind::relay;
    relay.originator = 5;
    relay.body = relay_body;
    const auto accepted_relay = harness.binding.handle_relay(
        consensus_message<MsgRelay>(relay),
        AuthenticatedEpochPeer::replica(5));
    CHECK(accepted_relay.error == EpochIngressError::none);
    CHECK(accepted_relay.permission ==
          EpochConsensusPermission::accept_contribution);
    REQUIRE(harness.continuations.relays.size() == 1);
    CHECK(harness.continuations.relays.front() == relay_body);

    auto forged = vote;
    forged.view_generation = harness.activation.active_effect().generation;
    CHECK(harness.binding.handle_vote(
              consensus_message<MsgVote>(forged),
              AuthenticatedEpochPeer::replica(4))
              .permission == EpochConsensusPermission::rejected_identity);
    CHECK(harness.continuations.votes.size() == 2);

    REQUIRE(harness.contexts.close(
        predecessor_key,
        ProposalContextEvent::proposal_aborted));
    auto closed_vote = vote;
    closed_vote.body = {0xD1, 0x72};
    CHECK(harness.binding.handle_vote(
              consensus_message<MsgVote>(closed_vote),
              AuthenticatedEpochPeer::replica(4))
              .permission == EpochConsensusPermission::rejected_identity);
    CHECK(harness.continuations.votes.size() == 2);
}

TEST_CASE("adaptive outbound helper binds the active configuration and generation",
          "[rem-d11][epoch-live-binding][outbound][intentional-red]")
{
    Harness harness;
    const auto active = harness.activation.active_effect();
    const ProposalKey key{active.configuration, digest("outbound-exact")};
    const bytearray_t body{0xA7, 0xD1};

    const auto encoded = adaptive_epoch_consensus_message(
        active,
        EpochConsensusWireKind::vote,
        key,
        4,
        0,
        body,
        limits());
    const auto decoded = decode_epoch_consensus_envelope(
        encoded,
        EpochConsensusWireKind::vote,
        EpochProtocolMode::adaptive_v1,
        limits());
    REQUIRE(decoded);
    CHECK(decoded.value->configuration == active.configuration);
    CHECK(decoded.value->view_generation == active.generation);
    CHECK(decoded.value->block_hash == key.block_hash);
    CHECK(decoded.value->originator == 4);
    CHECK(decoded.value->proposer == 0);
    CHECK(decoded.value->body == body);

    auto foreign = key;
    foreign.configuration.tree_id = 1;
    CHECK(adaptive_epoch_consensus_message(
              active,
              EpochConsensusWireKind::vote,
              foreign,
              4,
              0,
              body,
              limits())
              .empty());
}
