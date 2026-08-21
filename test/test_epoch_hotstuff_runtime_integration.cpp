#include <algorithm>
#include <cstddef>
#include <cstdint>
#include <iterator>
#include <limits>
#include <optional>
#include <set>
#include <stdexcept>
#include <string>
#include <utility>
#include <vector>

#include "catch.hpp"
#include "hotstuff/epoch_change_bundle.h"
#include "hotstuff/epoch_change_inbox.h"
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

std::vector<ReplicaID> adversarial_membership()
{
    return {0, 1, 2, 3};
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

EpochDefinitionInput rooted_epoch_v2_input(
    std::uint32_t epoch_number,
    const std::vector<ReplicaID> &roots,
    const uint256_t &previous = {})
{
    auto input = epoch_v2_input(epoch_number, previous);
    input.trees.clear();
    for (std::size_t index = 0; index < roots.size(); ++index)
    {
        auto members = membership();
        const auto root = std::find(members.begin(), members.end(), roots[index]);
        if (root == members.end())
            throw std::logic_error("fixture root is outside membership");
        std::rotate(members.begin(), root, std::next(root));
        input.trees.push_back(EpochTreeDefinition{
            static_cast<std::uint32_t>(index),
            2,
            2,
            std::move(members)});
    }
    input.epoch_digest.reset();
    return input;
}

EpochDefinitionInput rotating_epoch_v2_input()
{
    auto input = rooted_epoch_v2_input(0, {0, 1, 2});
    input.trees[0].tree_id = 6;
    input.trees[1].tree_id = 42;
    input.trees[2].tree_id = 77;
    input.epoch_digest.reset();
    return input;
}

EpochDefinitionInput wide_rotating_epoch_v2_input()
{
    // Repeat roots only after every member has led once.  Tree identity and
    // canonical definition order, rather than root identity, define the
    // prospective-generation horizon exercised by this fixture.
    return rooted_epoch_v2_input(
        0, {0, 1, 2, 3, 4, 5, 6, 0, 1, 2});
}

EpochDefinitionInput shaped_epoch_v2_input(
    std::uint32_t epoch_number,
    const std::vector<ReplicaID> &roots,
    std::uint32_t fanout,
    std::uint32_t pipeline_stretch,
    const std::vector<ReplicaID> &wait_exempt,
    const uint256_t &previous = {})
{
    auto input = epoch_v2_input(epoch_number, previous);
    std::set<ReplicaID> optional(
        wait_exempt.begin(), wait_exempt.end());
    if (optional.size() != wait_exempt.size())
        throw std::logic_error("fixture wait-exempt set is duplicated");

    input.trees.clear();
    for (std::size_t index = 0; index < roots.size(); ++index)
    {
        std::vector<ReplicaID> members;
        members.push_back(roots[index]);
        for (const auto replica : membership())
        {
            if (replica != roots[index] && optional.count(replica) == 0)
                members.push_back(replica);
        }
        members.insert(
            members.end(), wait_exempt.begin(), wait_exempt.end());
        if (members.size() != membership().size() ||
            optional.count(roots[index]) != 0)
        {
            throw std::logic_error(
                "fixture root/wait-exempt membership is invalid");
        }
        input.trees.push_back(EpochTreeDefinition{
            static_cast<std::uint32_t>(index),
            fanout,
            pipeline_stretch,
            std::move(members),
            wait_exempt});
    }
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

EpochChangeBundleLimits bundle_limits()
{
    return {
        64 * 1024,
        4096,
        EpochWireLimits{32 * 1024, 8, 16, 128, 2}};
}

EpochDefinitionInput adversarial_epoch_v2_input(
    std::uint32_t epoch_number,
    const uint256_t &previous = {},
    const std::string &snapshot = "rem-d11-01-baseline")
{
    EpochDefinitionInput input;
    input.schema_version = kEpochDefinitionSchemaVersionV2;
    input.epoch_number = epoch_number;
    input.previous_epoch_digest = previous;
    input.membership_digest =
        canonical_membership_digest(adversarial_membership());
    input.trees =
        epoch_number == 0
            ? std::vector<EpochTreeDefinition>{
                  {0, 2, 2, {0, 1, 2, 3}},
                  {1, 2, 2, {1, 0, 2, 3}}}
            : std::vector<EpochTreeDefinition>{
                  {0, 2, 2, {1, 0, 2, 3}},
                  {1, 2, 2, {2, 0, 1, 3}}};
    input.activation_height = 0;
    input.generation_seed = 0;
    input.policy_version = "rem-d11-01-adversarial-v1";
    input.evidence_snapshot_id = snapshot;
    input.evidence_cutoff = epoch_number;
    return input;
}

PrivKeySecp256k1 recovery_issuer_key()
{
    PrivKeySecp256k1 key;
    key.from_hex(
        "4aede145d13021fb43c938bced67511a7740c05786d3e0b94ffbdaa7f15afc57");
    return key;
}

AuthorizedEpochChange recovery_command(
    const EpochDefinitionInput &successor,
    std::uint64_t delay,
    const PrivKeySecp256k1 &key)
{
    return authorize_epoch_change(
        EpochChangePayload{
            successor.epoch_number,
            successor.previous_epoch_digest,
            compute_epoch_digest(successor),
            delay},
        17,
        key);
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
    std::size_t process_attempt_count{0};
    std::size_t process_count{0};
    std::size_t local_vote_count{0};
    std::size_t expected_vote_state_count{0};
    std::size_t latency_deadline_count{0};
    std::size_t aggregation_timer_count{0};
    std::size_t timeout_report_count{0};
    std::size_t apply_count_seen_on_process{0};
    const std::size_t *live_apply_count{nullptr};
    bool fail_process{false};
    bytearray_t processed_body;

    void relay_once(const BufferedProposal &) override { ++relay_count; }
    void process_active(const BufferedProposal &proposal) override
    {
        ++process_attempt_count;
        if (live_apply_count != nullptr)
            apply_count_seen_on_process = *live_apply_count;
        if (fail_process)
            throw std::runtime_error("injected proposal processing failure");
        ++process_count;
        processed_body = hotstuff_epoch_processing_payload(proposal);
    }
    void local_vote_authorized(const ProposalKey &) override
    {
        ++local_vote_count;
    }
    void create_expected_vote_state(const ProposalKey &) override
    {
        ++expected_vote_state_count;
    }
    bool start_latency_deadline(const ProposalKey &) override
    {
        ++latency_deadline_count;
        return true;
    }
    void start_aggregation_timer(const ProposalKey &) override
    {
        ++aggregation_timer_count;
    }
    void emit_timeout_report(const ProposalKey &) override
    {
        ++timeout_report_count;
    }
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
        const EpochDefinition &active,
        const std::vector<ReplicaID> &successor_roots,
        std::uint32_t successor_fanout,
        std::uint32_t successor_pipeline_stretch,
        const std::vector<ReplicaID> &successor_wait_exempt)
    {
        const auto staged = store.stage_available_v2(
            shaped_epoch_v2_input(
                active.epoch_number() + 1,
                successor_roots,
                successor_fanout,
                successor_pipeline_stretch,
                successor_wait_exempt,
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

    explicit V2Harness(
        EpochDefinitionInput initial = epoch_v2_input(0),
        std::vector<ReplicaID> successor_roots = {2, 3},
        std::uint32_t successor_fanout = 2,
        std::uint32_t successor_pipeline_stretch = 2,
        std::vector<ReplicaID> successor_wait_exempt = {},
        EpochProtocolMode protocol_mode = EpochProtocolMode::adaptive_v2)
        : epoch0(store.stage(std::move(initial), validation_context(0))),
          epoch1(stage_successor(
              store,
              epoch0,
              successor_roots,
              successor_fanout,
              successor_pipeline_stretch,
              successor_wait_exempt)),
          activation(
              store,
              epoch0,
              0,
              epoch0.trees().front().tree_id),
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
              protocol_mode,
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

struct RotatingProposalHarness
{
    EpochStore store{membership()};
    const EpochDefinition &epoch0;
    ReplicaEpochActivation activation;
    FutureProposalBuffer future;
    ProposalContextLifecycle contexts;
    ProposalEffectsSpy proposal_effects;
    ProposalAdmissionCoordinator admission;
    HotStuffRetryableFutureProposalStore retryable_future;
    BodyValidatorSpy validator;
    LiveEffectsSpy live_effects;
    HotStuffEpochRuntimeTransaction transaction;
    HotStuffEpochRuntimeAdapter adapter;
    ManagerEgressSpy manager_egress;
    ContinuationsSpy continuations;
    HotStuffEpochLiveBinding binding;

    explicit RotatingProposalHarness(
        std::uint32_t rotation_ordinal = 0,
        std::uint32_t active_tree_id = 6)
        : RotatingProposalHarness(
              rotating_epoch_v2_input(),
              rotation_ordinal,
              active_tree_id,
              {})
    {}

    RotatingProposalHarness(
        EpochDefinitionInput initial,
        std::uint32_t rotation_ordinal,
        std::uint32_t active_tree_id,
        FutureProposalBufferLimits future_limits = {})
        : epoch0(store.stage(std::move(initial), validation_context(0))),
          activation(
              store,
              epoch0,
              0,
              active_tree_id,
              rotation_ordinal),
          future(future_limits),
          admission(
              store,
              activation.active_effect().configuration,
              future,
              proposal_effects),
          retryable_future(future, admission),
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
        proposal_effects.live_apply_count = &live_effects.apply_count;
    }
};

EpochConsensusEnvelope rotation_proposal(
    ConfigurationId configuration,
    std::uint64_t generation,
    ReplicaID proposer,
    const char *label)
{
    return {
        std::move(configuration),
        generation,
        digest(label),
        proposer,
        proposer,
        bytearray_t{0xA1, 0xB2},
        kEpochConsensusWireSchemaVersion,
        EpochProtocolMode::adaptive_v2,
        EpochConsensusWireKind::proposal};
}

struct AdversarialV2Replica
{
    EpochStore store{adversarial_membership()};
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
    AdaptiveV2CommandInbox inbox;
    EpochChangeVerifier verifier;

    explicit AdversarialV2Replica(
        ReplicaID replica,
        const PrivKeySecp256k1 &issuer_key)
        : epoch0(store.stage(
              adversarial_epoch_v2_input(0),
              EpochValidationContext{})),
          activation(store, epoch0, replica, 0),
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
              continuations),
          verifier(
              EpochChangeIssuer{
                  17, PubKeySecp256k1(issuer_key)},
              EpochChangeDelayBounds{1, 20})
    {
        contexts.activate_configuration(
            activation.active_effect().configuration);
    }
};

bool recover_exact_committed_bundle(
    AdversarialV2Replica &replica,
    const AuthorizedEpochChange &committed,
    const AdaptiveV2EpochChangeBundle &candidate)
{
    if (encode_authorized_epoch_change(candidate.command()) !=
            encode_authorized_epoch_change(committed) ||
        compute_epoch_digest(candidate.definition()) !=
            committed.payload.successor_epoch_digest)
        return false;

    const auto ingested = replica.inbox.ingest(
        candidate, replica.epoch0, replica.verifier, replica.store);
    return (ingested.disposition ==
                AdaptiveV2CommandIngestDisposition::accepted ||
            ingested.disposition ==
                AdaptiveV2CommandIngestDisposition::duplicate) &&
           replica.store.find_epoch_by_digest(
               committed.payload.successor_epoch_digest) != nullptr;
}

ReplicaID active_root(const EpochActivationEffect &active)
{
    if (active.definition == nullptr)
        throw std::logic_error("fixture has no active definition");
    const auto found = std::find_if(
        active.definition->trees().begin(),
        active.definition->trees().end(),
        [&active](const EpochTreeDefinition &tree) {
            return tree.tree_id == active.configuration.tree_id;
        });
    if (found == active.definition->trees().end() ||
        found->members_breadth_first.empty())
        throw std::logic_error("fixture has no active root");
    return found->members_breadth_first.front();
}

class FailOnceRotationEffects final : public AdaptiveV2RotationEffects
{
public:
    explicit FailOnceRotationEffects(HotStuffEpochLiveBinding &binding)
        : binding_(binding)
    {}

    std::optional<EpochActivationEffect> active_view()
        const noexcept override
    {
        return binding_.active_view();
    }

    std::optional<std::uint32_t> next_tree_id() const noexcept override
    {
        return binding_.next_tree_id();
    }

    EpochRotationResult rotate_to_tree(
        std::uint32_t tree_id) noexcept override
    {
        if (fail_next_)
        {
            fail_next_ = false;
            return {EpochIngressError::state_rejected, std::nullopt};
        }
        return binding_.rotate_to_tree(tree_id);
    }

private:
    HotStuffEpochLiveBinding &binding_;
    bool fail_next_{true};
};

} // namespace

TEST_CASE("adaptive v2 commit cadence counts only the exact active configuration",
          "[c08][adaptive-v2][commit-cadence]")
{
    const ConfigurationId active{0, 7, digest("epoch-zero")};
    const ProposalKey first{active, digest("first")};
    const ProposalKey second{active, digest("second")};
    AdaptiveV2CommitCadence cadence(2);

    CHECK(cadence.period() == 2);
    CHECK(cadence.observed_commits() == 0);
    CHECK_FALSE(cadence.observe(std::nullopt, active));
    CHECK_FALSE(cadence.observe(
        ProposalKey{{1, 7, active.epoch_digest}, digest("wrong-epoch")},
        active));
    CHECK_FALSE(cadence.observe(
        ProposalKey{{0, 42, active.epoch_digest}, digest("draining-tree")},
        active));
    CHECK_FALSE(cadence.observe(
        ProposalKey{{0, 7, digest("wrong-digest")}, digest("wrong-epoch-id")},
        active));
    CHECK(cadence.observed_commits() == 0);

    CHECK_FALSE(cadence.observe(first, active));
    CHECK(cadence.observed_commits() == 1);
    CHECK(cadence.observe(second, active));
    CHECK(cadence.observed_commits() == 2);

    INFO("a failed rotation remains due without overflowing the counter");
    CHECK(cadence.observe(
        ProposalKey{active, digest("retry-after-failure")}, active));
    CHECK(cadence.observed_commits() == 2);

    cadence.reset();
    CHECK(cadence.observed_commits() == 0);
    CHECK_FALSE(cadence.observe(first, active));
}

TEST_CASE("adaptive v2 commit cadence rejects a zero period",
          "[c08][adaptive-v2][commit-cadence][configuration]")
{
    CHECK_THROWS_AS(AdaptiveV2CommitCadence(0), std::invalid_argument);
}

TEST_CASE("adaptive v2 coordinator keeps a failed periodic rotation due",
          "[c08][adaptive-v2][rotation-coordinator][commit]")
{
    auto initial = epoch_v2_input(0);
    initial.trees[0].tree_id = 7;
    initial.trees[1].tree_id = 42;
    V2Harness harness(std::move(initial));
    FailOnceRotationEffects effects(harness.binding);
    AdaptiveV2RotationCoordinator coordinator(2, effects);
    const auto expected = harness.activation.active_effect();

    CHECK(coordinator.on_commit(
              std::nullopt,
              expected.configuration,
              expected.generation)
              .disposition == AdaptiveV2RotationDisposition::not_due);
    CHECK(coordinator.observed_commits() == 0);

    CHECK(coordinator.on_commit(
              ProposalKey{
                  ConfigurationId{
                      expected.configuration.epoch_number,
                      999,
                      expected.configuration.epoch_digest},
                  digest("wrong-configuration")},
              expected.configuration,
              expected.generation)
              .disposition == AdaptiveV2RotationDisposition::not_due);
    CHECK(coordinator.observed_commits() == 0);

    CHECK(coordinator.on_commit(
              ProposalKey{expected.configuration, digest("first")},
              expected.configuration,
              expected.generation)
              .disposition == AdaptiveV2RotationDisposition::not_due);
    CHECK(coordinator.observed_commits() == 1);

    CHECK(coordinator.on_commit(
              ProposalKey{expected.configuration, digest("second")},
              expected.configuration,
              expected.generation)
              .disposition == AdaptiveV2RotationDisposition::rejected);
    CHECK(coordinator.observed_commits() == 2);
    CHECK(harness.activation.active_effect().configuration.tree_id == 7);

    const auto retried = coordinator.on_commit(
        ProposalKey{expected.configuration, digest("retry")},
        expected.configuration,
        expected.generation);
    REQUIRE(retried.disposition ==
            AdaptiveV2RotationDisposition::rotated);
    REQUIRE(retried.update.has_value());
    CHECK(retried.update->activation.configuration.tree_id == 42);
    CHECK(coordinator.observed_commits() == 0);
}

TEST_CASE("adaptive v2 timeout reset rejects a stale expected view",
          "[c08][adaptive-v2][rotation-coordinator][timeout][stale]")
{
    V2Harness harness(rooted_epoch_v2_input(0, {0, 1, 2}));
    AdaptiveV2RotationCoordinator coordinator(3, harness.binding);
    const auto expired = harness.activation.active_effect();

    CHECK(coordinator.on_commit(
              ProposalKey{expired.configuration, digest("partial")},
              expired.configuration,
              expired.generation)
              .disposition == AdaptiveV2RotationDisposition::not_due);
    CHECK(coordinator.observed_commits() == 1);

    const auto rotated = coordinator.on_timeout(
        expired.configuration, expired.generation);
    REQUIRE(rotated.disposition == AdaptiveV2RotationDisposition::rotated);
    CHECK(active_root(harness.activation.active_effect()) == 1);
    CHECK(coordinator.observed_commits() == 0);

    const auto stale = coordinator.on_timeout(
        expired.configuration, expired.generation);
    CHECK(stale.disposition == AdaptiveV2RotationDisposition::stale_view);
    CHECK_FALSE(stale.update.has_value());
    CHECK(active_root(harness.activation.active_effect()) == 1);
    CHECK(coordinator.observed_commits() == 0);
}

TEST_CASE("adaptive v3 shares exact periodic and timeout rotation",
          "[cert13][adaptive-v3][rotation-coordinator][runtime]")
{
    V2Harness harness(
        rooted_epoch_v2_input(0, {0, 1, 2}),
        {2, 3},
        2,
        2,
        {},
        EpochProtocolMode::adaptive_v3);
    AdaptiveV2RotationCoordinator coordinator(2, harness.binding);

    const auto initial = harness.activation.active_effect();
    CHECK(active_root(initial) == 0);
    CHECK(coordinator.on_commit(
              ProposalKey{initial.configuration, digest("v3-first")},
              initial.configuration,
              initial.generation)
              .disposition == AdaptiveV2RotationDisposition::not_due);

    const auto periodic = coordinator.on_commit(
        ProposalKey{initial.configuration, digest("v3-second")},
        initial.configuration,
        initial.generation);
    REQUIRE(periodic.disposition ==
            AdaptiveV2RotationDisposition::rotated);
    REQUIRE(periodic.update.has_value());
    CHECK(active_root(harness.activation.active_effect()) == 1);

    const auto after_periodic = harness.activation.active_effect();
    const auto timeout = coordinator.on_timeout(
        after_periodic.configuration, after_periodic.generation);
    REQUIRE(timeout.disposition ==
            AdaptiveV2RotationDisposition::rotated);
    REQUIRE(timeout.update.has_value());
    CHECK(active_root(harness.activation.active_effect()) == 2);
}

TEST_CASE("adaptive v3 stale root repair is catch-up only",
          "[cert13][adaptive-v3][proposal-repair][catch-up][safety]")
{
    V2Harness harness(
        rooted_epoch_v2_input(0, {0, 1}),
        {2, 3},
        2,
        2,
        {},
        EpochProtocolMode::adaptive_v3);
    const auto initial = harness.activation.active_effect();
    REQUIRE(initial.configuration.tree_id == 0);
    REQUIRE(initial.generation == 1);

    auto current = rotation_proposal(
        initial.configuration,
        initial.generation,
        0,
        "v3-current-repair");
    current.protocol_mode = EpochProtocolMode::adaptive_v3;
    current.kind = EpochConsensusWireKind::proposal_repair;
    const auto admitted = harness.binding.handle_proposal(
        consensus_message<MsgPropose>(current),
        AuthenticatedEpochPeer::replica(0));
    REQUIRE(admitted.error == EpochIngressError::none);
    REQUIRE(admitted.permission ==
            EpochConsensusPermission::admit_or_buffer);
    REQUIRE(admitted.admission_disposition ==
            ProposalDisposition::admitted_active);
    CHECK(harness.proposal_effects.process_count == 1);
    CHECK(harness.proposal_effects.expected_vote_state_count == 0);
    CHECK(harness.proposal_effects.latency_deadline_count == 0);
    CHECK(harness.proposal_effects.aggregation_timer_count == 0);

    const auto rotated = harness.binding.rotate_to_tree(1);
    REQUIRE(rotated.error == EpochIngressError::none);
    REQUIRE(rotated.update.has_value());
    REQUIRE(rotated.update->activation.generation == 2);

    auto stale = rotation_proposal(
        initial.configuration,
        initial.generation,
        0,
        "v3-stale-repair");
    stale.protocol_mode = EpochProtocolMode::adaptive_v3;
    stale.kind = EpochConsensusWireKind::proposal_repair;
    const auto catch_up = harness.binding.handle_proposal(
        consensus_message<MsgPropose>(stale),
        AuthenticatedEpochPeer::replica(0));
    REQUIRE(catch_up.error == EpochIngressError::none);
    REQUIRE(catch_up.permission ==
            EpochConsensusPermission::catch_up_only);
    REQUIRE(catch_up.decoded_envelope.has_value());
    CHECK(catch_up.decoded_envelope->key() == stale.key());
    CHECK_FALSE(catch_up.admission_disposition.has_value());
    CHECK(harness.proposal_effects.relay_count == 1);
    CHECK(harness.proposal_effects.process_count == 1);
    CHECK(harness.proposal_effects.local_vote_count == 0);
    CHECK(harness.proposal_effects.expected_vote_state_count == 0);
    CHECK(harness.proposal_effects.latency_deadline_count == 0);
    CHECK(harness.proposal_effects.aggregation_timer_count == 0);

    const auto wrapped = harness.binding.rotate_to_tree(0);
    REQUIRE(wrapped.error == EpochIngressError::none);
    REQUIRE(wrapped.update.has_value());
    REQUIRE(wrapped.update->activation.generation == 3);
    const auto rejected_too_old = harness.binding.handle_proposal(
        consensus_message<MsgPropose>(stale),
        AuthenticatedEpochPeer::replica(0));
    CHECK(rejected_too_old.error == EpochIngressError::state_rejected);
    CHECK(rejected_too_old.permission ==
          EpochConsensusPermission::rejected_identity);

    auto ordinary_stale = stale;
    ordinary_stale.kind = EpochConsensusWireKind::proposal;
    const auto rejected_ordinary = harness.binding.handle_proposal(
        consensus_message<MsgPropose>(ordinary_stale),
        AuthenticatedEpochPeer::replica(0));
    CHECK(rejected_ordinary.error == EpochIngressError::state_rejected);
    CHECK(rejected_ordinary.permission ==
          EpochConsensusPermission::rejected_identity);

    const auto rejected_nonroot = harness.binding.handle_proposal(
        consensus_message<MsgPropose>(stale),
        AuthenticatedEpochPeer::replica(1));
    CHECK(rejected_nonroot.error == EpochIngressError::state_rejected);
    CHECK(rejected_nonroot.permission ==
          EpochConsensusPermission::rejected_identity);

    auto v2_repair = stale;
    v2_repair.protocol_mode = EpochProtocolMode::adaptive_v2;
    V2Harness v2(rooted_epoch_v2_input(0, {0, 1}));
    const auto rejected_v2 = v2.binding.handle_proposal(
        consensus_message<MsgPropose>(v2_repair),
        AuthenticatedEpochPeer::replica(0));
    CHECK(rejected_v2.error == EpochIngressError::wire_rejected);
    CHECK(rejected_v2.wire_error ==
          EpochConsensusWireError::unexpected_kind);
}

TEST_CASE("adaptive v2 activation resets cadence and excludes its predecessor key",
          "[c08][adaptive-v2][rotation-coordinator][activation]")
{
    constexpr std::uint64_t commit_height = 40;
    constexpr std::uint64_t delay = 5;
    V2Harness harness;
    AdaptiveV2RotationCoordinator coordinator(3, harness.binding);
    const auto predecessor = harness.activation.active_effect();
    const ProposalKey predecessor_key{
        predecessor.configuration, digest("activation-predecessor")};

    CHECK(coordinator.on_commit(
              predecessor_key,
              predecessor.configuration,
              predecessor.generation)
              .disposition == AdaptiveV2RotationDisposition::not_due);
    CHECK(coordinator.observed_commits() == 1);

    REQUIRE(harness.adapter.prepare_committed_v2(harness.epoch1) ==
            EpochIngressError::none);
    REQUIRE(harness.activation.record_committed_v2(
                harness.command(delay), commit_height)
                .disposition == ActivationRecordDisposition::recorded);
    REQUIRE(harness.binding.on_v2_post_block_commit(
                commit_height + delay,
                harness.epoch0.epoch_digest())
                .transition == ActivationTransition::activated);
    coordinator.reset_for_activation();
    const auto successor = harness.activation.active_effect();
    CHECK(coordinator.observed_commits() == 0);

    CHECK(coordinator.on_commit(
              predecessor_key,
              successor.configuration,
              successor.generation)
              .disposition == AdaptiveV2RotationDisposition::not_due);
    CHECK(coordinator.observed_commits() == 0);
    for (std::size_t index = 0; index < 2; ++index)
    {
        CHECK(coordinator.on_commit(
                  ProposalKey{
                      successor.configuration,
                      DataStream(std::to_string(index)).get_hash()},
                  successor.configuration,
                  successor.generation)
                  .disposition == AdaptiveV2RotationDisposition::not_due);
    }
    const auto due = coordinator.on_commit(
        ProposalKey{successor.configuration, digest("successor-third")},
        successor.configuration,
        successor.generation);
    REQUIRE(due.disposition == AdaptiveV2RotationDisposition::rotated);
    CHECK(active_root(harness.activation.active_effect()) == 3);
}

TEST_CASE("adaptive v2 coordinator cycles N7 and the contained successor order",
          "[c08][adaptive-v2][rotation-coordinator][n7][quorum]")
{
    constexpr std::uint64_t commit_height = 60;
    constexpr std::uint64_t delay = 5;
    V2Harness harness(
        rooted_epoch_v2_input(0, {0, 1, 2, 3, 4, 5, 6}),
        {2, 3, 4, 5, 6});
    AdaptiveV2RotationCoordinator coordinator(1, harness.binding);
    const auto quorum = derive_byzantine_quorum(membership().size());
    REQUIRE(quorum.has_value());
    CHECK(quorum->replica_count == 7);
    CHECK(quorum->fault_threshold == 2);
    CHECK(quorum->quorum == 5);
    for (const auto &tree : harness.epoch0.trees())
        CHECK(tree.members_breadth_first.size() == 7);
    for (const auto &tree : harness.epoch1.trees())
        CHECK(tree.members_breadth_first.size() == 7);

    for (const ReplicaID expected_root : {1, 2, 3, 4, 5, 6, 0})
    {
        const auto active = harness.activation.active_effect();
        const auto result = coordinator.on_commit(
            ProposalKey{
                active.configuration,
                DataStream(std::to_string(expected_root)).get_hash()},
            active.configuration,
            active.generation);
        REQUIRE(result.disposition == AdaptiveV2RotationDisposition::rotated);
        CHECK(active_root(harness.activation.active_effect()) == expected_root);
    }

    const auto activation_key = ProposalKey{
        harness.activation.active_effect().configuration,
        digest("n7-activation")};
    REQUIRE(harness.adapter.prepare_committed_v2(harness.epoch1) ==
            EpochIngressError::none);
    REQUIRE(harness.activation.record_committed_v2(
                harness.command(delay), commit_height)
                .disposition == ActivationRecordDisposition::recorded);
    REQUIRE(harness.binding.on_v2_post_block_commit(
                commit_height + delay,
                harness.epoch0.epoch_digest())
                .transition == ActivationTransition::activated);
    coordinator.reset_for_activation();
    CHECK(active_root(harness.activation.active_effect()) == 2);

    const auto successor = harness.activation.active_effect();
    CHECK(coordinator.on_commit(
              activation_key,
              successor.configuration,
              successor.generation)
              .disposition == AdaptiveV2RotationDisposition::not_due);
    for (const ReplicaID expected_root : {3, 4, 5, 6, 2})
    {
        const auto active = harness.activation.active_effect();
        const auto result = coordinator.on_commit(
            ProposalKey{
                active.configuration,
                DataStream(std::to_string(expected_root + 10)).get_hash()},
            active.configuration,
            active.generation);
        REQUIRE(result.disposition == AdaptiveV2RotationDisposition::rotated);
        const auto root = active_root(harness.activation.active_effect());
        CHECK(root == expected_root);
        CHECK(root != 0);
        CHECK(root != 1);
    }

    const auto successor_quorum = derive_byzantine_quorum(membership().size());
    REQUIRE(successor_quorum.has_value());
    CHECK(successor_quorum->replica_count == 7);
    CHECK(successor_quorum->fault_threshold == 2);
    CHECK(successor_quorum->quorum == 5);
}

TEST_CASE("adaptive v2 activation does not count the predecessor commit",
          "[c08][adaptive-v2][commit-cadence][activation]")
{
    const ConfigurationId predecessor{0, 7, digest("epoch-zero")};
    const ConfigurationId successor{1, 42, digest("epoch-one")};
    AdaptiveV2CommitCadence cadence(2);

    CHECK_FALSE(cadence.observe(
        ProposalKey{predecessor, digest("activation-block")}, successor));
    CHECK(cadence.observed_commits() == 0);

    CHECK_FALSE(cadence.observe(
        ProposalKey{successor, digest("first-successor-block")}, successor));
    CHECK(cadence.observed_commits() == 1);
}

TEST_CASE("adaptive v2 live binding cycles non-contiguous tree identifiers",
          "[c08][adaptive-v2][epoch-live-binding][rotation]")
{
    auto initial = epoch_v2_input(0);
    initial.trees[0].tree_id = 7;
    initial.trees[1].tree_id = 42;
    V2Harness harness(std::move(initial));

    const auto first = harness.activation.active_effect();
    REQUIRE(first.configuration.tree_id == 7);

    const auto second = harness.binding.rotate_to_tree(42);
    REQUIRE(second.error == EpochIngressError::none);
    REQUIRE(second.update.has_value());
    CHECK(second.update->activation.configuration.tree_id == 42);
    CHECK(second.update->activation.generation > first.generation);

    const auto cycled = harness.binding.rotate_to_tree(7);
    REQUIRE(cycled.error == EpochIngressError::none);
    REQUIRE(cycled.update.has_value());
    CHECK(cycled.update->activation.configuration.tree_id == 7);
    CHECK(cycled.update->activation.generation >
          second.update->activation.generation);
    CHECK(harness.live_effects.rotation_arm_count == 2);
    CHECK(harness.live_effects.apply_count == 2);
}

TEST_CASE("adaptive v2 buffers only the exact immediate next tree until live rotation",
          "[adaptive-v2][epoch-live-binding][rotation][future-proposal]")
{
    RotatingProposalHarness harness;
    const auto active = harness.activation.active_effect();
    REQUIRE(active.configuration.tree_id == 6);
    const ConfigurationId next{
        active.configuration.epoch_number,
        42,
        active.configuration.epoch_digest};
    const auto next_generation = checked_activation_generation(
        active.configuration.epoch_number,
        static_cast<std::uint64_t>(active.rotation_ordinal) + 1);
    REQUIRE(next_generation.has_value());
    const auto proposal = rotation_proposal(
        next, *next_generation, 1, "next-tree-before-rotation");

    const auto buffered = harness.binding.handle_proposal(
        consensus_message<MsgPropose>(proposal),
        AuthenticatedEpochPeer::replica(1));
    REQUIRE(buffered.error == EpochIngressError::none);
    REQUIRE(buffered.permission ==
            EpochConsensusPermission::admit_or_buffer);
    REQUIRE(buffered.admission_disposition ==
            ProposalDisposition::buffered_future);
    CHECK(harness.future.size() == 1);
    CHECK(harness.proposal_effects.relay_count == 1);
    CHECK(harness.proposal_effects.process_attempt_count == 0);
    CHECK(harness.proposal_effects.process_count == 0);
    CHECK(harness.proposal_effects.local_vote_count == 0);
    CHECK(harness.proposal_effects.expected_vote_state_count == 0);
    CHECK(harness.proposal_effects.latency_deadline_count == 0);
    CHECK(harness.proposal_effects.aggregation_timer_count == 0);
    CHECK(harness.proposal_effects.timeout_report_count == 0);

    const auto duplicate_before = harness.binding.handle_proposal(
        consensus_message<MsgPropose>(proposal),
        AuthenticatedEpochPeer::replica(1));
    REQUIRE(duplicate_before.error == EpochIngressError::none);
    CHECK(duplicate_before.admission_disposition ==
          ProposalDisposition::duplicate);
    CHECK(harness.future.size() == 1);
    CHECK(harness.proposal_effects.relay_count == 1);
    CHECK(harness.proposal_effects.process_attempt_count == 0);

    const auto rotated = harness.binding.rotate_to_tree(42);
    REQUIRE(rotated.error == EpochIngressError::none);
    REQUIRE(rotated.update.has_value());
    CHECK(rotated.update->activation.configuration == next);
    CHECK(rotated.update->activation.generation == *next_generation);
    CHECK(harness.live_effects.apply_count == 1);
    CHECK(harness.proposal_effects.process_attempt_count == 1);
    CHECK(harness.proposal_effects.process_count == 1);
    CHECK(harness.proposal_effects.apply_count_seen_on_process == 1);
    CHECK(harness.proposal_effects.processed_body == proposal.body);
    CHECK(harness.future.size() == 0);
    CHECK_FALSE(
        harness.adapter.buffered_proposal_identity(proposal.key())
            .has_value());
    const auto processed =
        harness.adapter.processed_proposal_identity(proposal.key());
    REQUIRE(processed.has_value());
    CHECK(processed->view_generation == *next_generation);

    const auto duplicate_after = harness.binding.handle_proposal(
        consensus_message<MsgPropose>(proposal),
        AuthenticatedEpochPeer::replica(1));
    REQUIRE(duplicate_after.error == EpochIngressError::none);
    CHECK(duplicate_after.admission_disposition ==
          ProposalDisposition::duplicate);
    CHECK(harness.proposal_effects.relay_count == 1);
    CHECK(harness.proposal_effects.process_attempt_count == 1);
    CHECK(harness.proposal_effects.process_count == 1);

    const auto idempotent = harness.adapter.drain_activated_futures();
    CHECK(idempotent.status == EpochFutureDrainStatus::complete);
    CHECK(idempotent.processed == 0);
    CHECK(idempotent.remaining == 0);
}

TEST_CASE("adaptive v2 buffers distinct canonical future configurations and drains only the active bucket",
          "[adaptive-v2][epoch-live-binding][rotation][future-proposal][horizon]")
{
    RotatingProposalHarness harness;
    const auto active = harness.activation.active_effect();
    REQUIRE(active.configuration.tree_id == 6);
    const auto generation_at = [&active](std::uint64_t offset) {
        return checked_activation_generation(
            active.configuration.epoch_number,
            static_cast<std::uint64_t>(active.rotation_ordinal) + offset);
    };
    const auto tree42_generation = generation_at(1);
    const auto tree77_generation = generation_at(2);
    REQUIRE(tree42_generation.has_value());
    REQUIRE(tree77_generation.has_value());

    const ConfigurationId tree42{
        active.configuration.epoch_number,
        42,
        active.configuration.epoch_digest};
    const ConfigurationId tree77{
        active.configuration.epoch_number,
        77,
        active.configuration.epoch_digest};
    const auto first = rotation_proposal(
        tree42, *tree42_generation, 1, "future-tree-42");
    const auto second = rotation_proposal(
        tree77, *tree77_generation, 2, "future-tree-77");

    const auto first_buffered = harness.binding.handle_proposal(
        consensus_message<MsgPropose>(first),
        AuthenticatedEpochPeer::replica(1));
    REQUIRE(first_buffered.error == EpochIngressError::none);
    REQUIRE(first_buffered.admission_disposition ==
            ProposalDisposition::buffered_future);
    const auto second_buffered = harness.binding.handle_proposal(
        consensus_message<MsgPropose>(second),
        AuthenticatedEpochPeer::replica(2));
    REQUIRE(second_buffered.error == EpochIngressError::none);
    REQUIRE(second_buffered.admission_disposition ==
            ProposalDisposition::buffered_future);

    const auto second_duplicate = harness.binding.handle_proposal(
        consensus_message<MsgPropose>(second),
        AuthenticatedEpochPeer::replica(2));
    REQUIRE(second_duplicate.error == EpochIngressError::none);
    CHECK(second_duplicate.admission_disposition ==
          ProposalDisposition::duplicate);
    CHECK(harness.future.size() == 2);
    CHECK(harness.proposal_effects.relay_count == 2);
    CHECK(harness.proposal_effects.process_attempt_count == 0);
    CHECK(harness.proposal_effects.process_count == 0);
    CHECK(harness.proposal_effects.local_vote_count == 0);
    CHECK(harness.proposal_effects.expected_vote_state_count == 0);
    CHECK(harness.proposal_effects.latency_deadline_count == 0);
    CHECK(harness.proposal_effects.aggregation_timer_count == 0);
    CHECK(harness.proposal_effects.timeout_report_count == 0);

    const auto first_rotation = harness.binding.rotate_to_tree(42);
    REQUIRE(first_rotation.error == EpochIngressError::none);
    REQUIRE(first_rotation.update.has_value());
    CHECK(harness.live_effects.apply_count == 1);
    CHECK(harness.proposal_effects.process_attempt_count == 1);
    CHECK(harness.proposal_effects.process_count == 1);
    CHECK(harness.proposal_effects.apply_count_seen_on_process == 1);
    CHECK(harness.proposal_effects.processed_body == first.body);
    CHECK(harness.future.size() == 1);
    const auto retained =
        harness.adapter.buffered_proposal_identity(second.key());
    REQUIRE(retained.has_value());
    CHECK(retained->view_generation == *tree77_generation);

    const auto second_rotation = harness.binding.rotate_to_tree(77);
    REQUIRE(second_rotation.error == EpochIngressError::none);
    REQUIRE(second_rotation.update.has_value());
    CHECK(harness.live_effects.apply_count == 2);
    CHECK(harness.proposal_effects.process_attempt_count == 2);
    CHECK(harness.proposal_effects.process_count == 2);
    CHECK(harness.proposal_effects.apply_count_seen_on_process == 2);
    CHECK(harness.proposal_effects.processed_body == second.body);
    CHECK(harness.future.size() == 0);

    const auto second_after_activation = harness.binding.handle_proposal(
        consensus_message<MsgPropose>(second),
        AuthenticatedEpochPeer::replica(2));
    REQUIRE(second_after_activation.error == EpochIngressError::none);
    CHECK(second_after_activation.admission_disposition ==
          ProposalDisposition::duplicate);
    CHECK(harness.proposal_effects.relay_count == 2);
    CHECK(harness.proposal_effects.process_attempt_count == 2);
}

TEST_CASE("adaptive v2 admits the seventh canonical future offset without preactivation effects",
          "[adaptive-v2][epoch-runtime][rotation][future-proposal][horizon][offset-seven]")
{
    RotatingProposalHarness harness(
        wide_rotating_epoch_v2_input(), 2, 2);
    const auto active = harness.activation.active_effect();
    const auto generation = checked_activation_generation(
        active.configuration.epoch_number,
        static_cast<std::uint64_t>(active.rotation_ordinal) + 7);
    REQUIRE(generation.has_value());
    const auto &tree9 = harness.epoch0.trees().at(9);
    const auto proposal = rotation_proposal(
        ConfigurationId{
            active.configuration.epoch_number,
            tree9.tree_id,
            active.configuration.epoch_digest},
        *generation,
        tree9.members_breadth_first.front(),
        "future-offset-seven");

    const auto buffered = harness.binding.handle_proposal(
        consensus_message<MsgPropose>(proposal),
        AuthenticatedEpochPeer::replica(proposal.originator));
    REQUIRE(buffered.error == EpochIngressError::none);
    REQUIRE(buffered.admission_disposition ==
            ProposalDisposition::buffered_future);
    CHECK(harness.future.size() == 1);
    CHECK(harness.proposal_effects.relay_count == 1);
    CHECK(harness.proposal_effects.process_attempt_count == 0);
    CHECK(harness.proposal_effects.process_count == 0);
    CHECK(harness.proposal_effects.local_vote_count == 0);
    CHECK(harness.proposal_effects.expected_vote_state_count == 0);
    CHECK(harness.proposal_effects.latency_deadline_count == 0);
    CHECK(harness.proposal_effects.aggregation_timer_count == 0);
    CHECK(harness.proposal_effects.timeout_report_count == 0);
}

TEST_CASE("adaptive v2 future capacity rejection is inert and leaves no received tombstone",
          "[adaptive-v2][epoch-runtime][future-proposal][capacity][fail-closed]")
{
    FutureProposalBufferLimits limits;
    limits.max_entries = 1;
    limits.max_wire_bytes = 1024 * 1024;
    limits.max_entries_per_configuration_generation = 1;
    limits.max_wire_bytes_per_configuration_generation = 1024 * 1024;
    RotatingProposalHarness harness(
        rotating_epoch_v2_input(), 0, 6, limits);
    const auto active = harness.activation.active_effect();
    const auto generation_at = [&active](std::uint64_t offset) {
        return checked_activation_generation(
            active.configuration.epoch_number,
            static_cast<std::uint64_t>(active.rotation_ordinal) + offset);
    };
    const auto first_generation = generation_at(1);
    const auto second_generation = generation_at(2);
    REQUIRE(first_generation.has_value());
    REQUIRE(second_generation.has_value());
    const auto first = rotation_proposal(
        ConfigurationId{
            active.configuration.epoch_number,
            42,
            active.configuration.epoch_digest},
        *first_generation,
        1,
        "capacity-retained-first");
    const auto second = rotation_proposal(
        ConfigurationId{
            active.configuration.epoch_number,
            77,
            active.configuration.epoch_digest},
        *second_generation,
        2,
        "capacity-rejected-second");

    const auto retained = harness.binding.handle_proposal(
        consensus_message<MsgPropose>(first),
        AuthenticatedEpochPeer::replica(1));
    REQUIRE(retained.error == EpochIngressError::none);
    REQUIRE(retained.admission_disposition ==
            ProposalDisposition::buffered_future);
    CHECK(harness.future.size() == 1);
    CHECK(harness.proposal_effects.relay_count == 1);

    for (std::size_t attempt = 0; attempt < 2; ++attempt)
    {
        const auto rejected = harness.binding.handle_proposal(
            consensus_message<MsgPropose>(second),
            AuthenticatedEpochPeer::replica(2));
        CHECK(rejected.error == EpochIngressError::state_rejected);
        CHECK(rejected.permission ==
              EpochConsensusPermission::rejected_identity);
        REQUIRE(rejected.admission_disposition.has_value());
        CHECK(*rejected.admission_disposition ==
              ProposalDisposition::rejected_capacity);
        CHECK(harness.future.size() == 1);
        CHECK(harness.proposal_effects.relay_count == 1);
        CHECK(harness.proposal_effects.process_attempt_count == 0);
        CHECK_FALSE(
            harness.adapter.buffered_proposal_identity(second.key())
                .has_value());
        CHECK_FALSE(
            harness.adapter.processed_proposal_identity(second.key())
                .has_value());
    }

    REQUIRE(harness.binding.rotate_to_tree(42).update.has_value());
    CHECK(harness.future.size() == 0);
    CHECK(harness.proposal_effects.process_count == 1);

    const auto admitted_after_release = harness.binding.handle_proposal(
        consensus_message<MsgPropose>(second),
        AuthenticatedEpochPeer::replica(2));
    REQUIRE(admitted_after_release.error == EpochIngressError::none);
    REQUIRE(admitted_after_release.admission_disposition ==
            ProposalDisposition::buffered_future);
    CHECK(harness.future.size() == 1);
    CHECK(harness.proposal_effects.relay_count == 2);
    CHECK(harness.proposal_effects.process_count == 1);
    REQUIRE(harness.binding.rotate_to_tree(77).update.has_value());
    CHECK(harness.future.size() == 0);
    CHECK(harness.proposal_effects.process_count == 2);
}

TEST_CASE("adaptive v2 buffers the canonical tree3 through tree9 prefix and replays it only after sequential live activation",
          "[adaptive-v2][epoch-live-binding][rotation][future-proposal][horizon][end-to-end]")
{
    RotatingProposalHarness harness(
        wide_rotating_epoch_v2_input(), 2, 2);
    const auto active = harness.activation.active_effect();
    REQUIRE(active.configuration.tree_id == 2);
    REQUIRE(active.rotation_ordinal == 2);
    REQUIRE(harness.epoch0.trees().size() == 10);

    std::vector<EpochConsensusEnvelope> proposals;
    for (std::uint32_t tree_id = 3; tree_id <= 9; ++tree_id)
    {
        const auto offset = static_cast<std::uint64_t>(tree_id - 2);
        const auto generation = checked_activation_generation(
            active.configuration.epoch_number,
            static_cast<std::uint64_t>(active.rotation_ordinal) + offset);
        REQUIRE(generation.has_value());
        const auto &tree = harness.epoch0.trees().at(tree_id);
        const auto label = "future-tree-" + std::to_string(tree_id);
        proposals.push_back(rotation_proposal(
            ConfigurationId{
                active.configuration.epoch_number,
                tree_id,
                active.configuration.epoch_digest},
            *generation,
            tree.members_breadth_first.front(),
            label.c_str()));
    }

    for (const auto &proposal : proposals)
    {
        const auto buffered = harness.binding.handle_proposal(
            consensus_message<MsgPropose>(proposal),
            AuthenticatedEpochPeer::replica(proposal.originator));
        REQUIRE(buffered.error == EpochIngressError::none);
        REQUIRE(buffered.admission_disposition ==
                ProposalDisposition::buffered_future);
    }

    CHECK(harness.future.size() == proposals.size());
    CHECK(harness.proposal_effects.relay_count == proposals.size());
    CHECK(harness.proposal_effects.process_attempt_count == 0);
    CHECK(harness.proposal_effects.process_count == 0);
    CHECK(harness.proposal_effects.local_vote_count == 0);
    CHECK(harness.proposal_effects.expected_vote_state_count == 0);
    CHECK(harness.proposal_effects.latency_deadline_count == 0);
    CHECK(harness.proposal_effects.aggregation_timer_count == 0);
    CHECK(harness.proposal_effects.timeout_report_count == 0);

    for (std::size_t index = 0; index < proposals.size(); ++index)
    {
        const auto tree_id = static_cast<std::uint32_t>(index + 3);
        const auto rotated = harness.binding.rotate_to_tree(tree_id);
        REQUIRE(rotated.error == EpochIngressError::none);
        REQUIRE(rotated.update.has_value());
        CHECK(rotated.update->activation.configuration ==
              proposals[index].configuration);
        CHECK(rotated.update->activation.generation ==
              proposals[index].view_generation);
        CHECK(harness.live_effects.apply_count == index + 1);
        CHECK(harness.proposal_effects.process_attempt_count == index + 1);
        CHECK(harness.proposal_effects.process_count == index + 1);
        CHECK(harness.proposal_effects.apply_count_seen_on_process ==
              index + 1);
        CHECK(harness.proposal_effects.processed_body ==
              proposals[index].body);
        CHECK(harness.future.size() == proposals.size() - index - 1);
        CHECK(harness.proposal_effects.timeout_report_count == 0);
    }
}

TEST_CASE("adaptive v2 prospective tree gate rejects inexact identities",
          "[adaptive-v2][epoch-runtime][rotation][future-proposal][fail-closed]")
{
    RotatingProposalHarness harness;
    const auto active = harness.activation.active_effect();
    const auto next_generation = checked_activation_generation(
        active.configuration.epoch_number,
        static_cast<std::uint64_t>(active.rotation_ordinal) + 1);
    REQUIRE(next_generation.has_value());
    const ConfigurationId next{
        active.configuration.epoch_number,
        42,
        active.configuration.epoch_digest};

    const auto rejects = [&harness](
                             const EpochConsensusEnvelope &proposal,
                             ReplicaID source) {
        const auto rejected = harness.binding.handle_proposal(
            consensus_message<MsgPropose>(proposal),
            AuthenticatedEpochPeer::replica(source));
        CHECK(rejected.error == EpochIngressError::state_rejected);
        CHECK(rejected.permission ==
              EpochConsensusPermission::rejected_identity);
        CHECK_FALSE(rejected.admission_disposition.has_value());
    };

    rejects(
        rotation_proposal(
            active.configuration,
            *next_generation,
            0,
            "current-tree-future-generation"),
        0);
    rejects(
        rotation_proposal(
            next,
            active.generation,
            1,
            "next-tree-stale-generation"),
        1);
    rejects(
        rotation_proposal(
            next,
            *next_generation + 1,
            1,
            "next-tree-nonexact-generation"),
        1);
    rejects(
        rotation_proposal(
            ConfigurationId{
                active.configuration.epoch_number,
                77,
                active.configuration.epoch_digest},
            *next_generation,
            2,
            "non-immediate-tree"),
        2);
    rejects(
        rotation_proposal(
            ConfigurationId{
                active.configuration.epoch_number,
                999,
                active.configuration.epoch_digest},
            *next_generation,
            2,
            "unknown-tree"),
        2);
    rejects(
        rotation_proposal(
            ConfigurationId{
                active.configuration.epoch_number + 1,
                42,
                active.configuration.epoch_digest},
            *next_generation,
            1,
            "wrong-epoch"),
        1);
    rejects(
        rotation_proposal(
            ConfigurationId{
                active.configuration.epoch_number,
                42,
                digest("wrong-current-epoch-digest")},
            *next_generation,
            1,
            "wrong-digest"),
        1);

    CHECK(harness.future.size() == 0);
    CHECK(harness.proposal_effects.relay_count == 0);
    CHECK(harness.proposal_effects.process_attempt_count == 0);

    REQUIRE(harness.binding.rotate_to_tree(42).update.has_value());
    rejects(
        rotation_proposal(
            active.configuration,
            active.generation,
            0,
            "retired-tree-generation"),
        0);
    CHECK(harness.future.size() == 0);
    CHECK(harness.proposal_effects.relay_count == 0);
}

TEST_CASE("adaptive v2 prospective proposal horizon wraps once but never admits the full-wrap current tree",
          "[adaptive-v2][epoch-live-binding][rotation][future-proposal][wrap][nonwrapping]")
{
    RotatingProposalHarness harness(2, 77);
    const auto active = harness.activation.active_effect();
    REQUIRE(active.configuration.tree_id == 77);
    const ConfigurationId wrapped{
        active.configuration.epoch_number,
        6,
        active.configuration.epoch_digest};
    const auto wrapped_generation = checked_activation_generation(
        active.configuration.epoch_number,
        static_cast<std::uint64_t>(active.rotation_ordinal) + 1);
    REQUIRE(wrapped_generation.has_value());
    const auto proposal = rotation_proposal(
        wrapped, *wrapped_generation, 0, "wrapped-next-tree");

    const auto full_wrap_generation = checked_activation_generation(
        active.configuration.epoch_number,
        static_cast<std::uint64_t>(active.rotation_ordinal) +
            harness.epoch0.trees().size());
    REQUIRE(full_wrap_generation.has_value());
    const auto current_tree_future = rotation_proposal(
        active.configuration,
        *full_wrap_generation,
        2,
        "full-wrap-current-tree");
    const auto current_rejected = harness.binding.handle_proposal(
        consensus_message<MsgPropose>(current_tree_future),
        AuthenticatedEpochPeer::replica(2));
    CHECK(current_rejected.error == EpochIngressError::state_rejected);
    CHECK(current_rejected.permission ==
          EpochConsensusPermission::rejected_identity);
    CHECK_FALSE(current_rejected.admission_disposition.has_value());
    CHECK(harness.future.size() == 0);
    CHECK(harness.proposal_effects.relay_count == 0);
    CHECK(harness.live_effects.apply_count == 0);

    const auto buffered = harness.binding.handle_proposal(
        consensus_message<MsgPropose>(proposal),
        AuthenticatedEpochPeer::replica(0));
    REQUIRE(buffered.error == EpochIngressError::none);
    REQUIRE(buffered.admission_disposition ==
            ProposalDisposition::buffered_future);
    CHECK(harness.future.size() == 1);
    CHECK(harness.proposal_effects.relay_count == 1);
    CHECK(harness.proposal_effects.process_attempt_count == 0);

    const auto rotated = harness.binding.rotate_to_tree(6);
    REQUIRE(rotated.error == EpochIngressError::none);
    REQUIRE(rotated.update.has_value());
    CHECK(rotated.update->activation.configuration == wrapped);
    CHECK(rotated.update->activation.generation == *wrapped_generation);
    CHECK(harness.live_effects.apply_count == 1);
    CHECK(harness.proposal_effects.process_attempt_count == 1);
    CHECK(harness.proposal_effects.process_count == 1);
    CHECK(harness.proposal_effects.apply_count_seen_on_process == 1);
    CHECK(harness.future.size() == 0);
}

TEST_CASE("adaptive v2 drain retires stale A-B-A generations without processing",
          "[adaptive-v2][epoch-live-binding][rotation][future-proposal][generation]")
{
    RotatingProposalHarness harness;
    const auto a = harness.activation.active_effect();
    const ConfigurationId b{
        a.configuration.epoch_number,
        42,
        a.configuration.epoch_digest};
    const auto b_generation = checked_activation_generation(
        a.configuration.epoch_number,
        static_cast<std::uint64_t>(a.rotation_ordinal) + 1);
    REQUIRE(b_generation.has_value());
    const auto stale_b = rotation_proposal(
        b, *b_generation, 1, "stale-b-buffered-generation");

    REQUIRE(harness.binding.handle_proposal(
                consensus_message<MsgPropose>(stale_b),
                AuthenticatedEpochPeer::replica(1))
                .admission_disposition ==
            ProposalDisposition::buffered_future);
    harness.proposal_effects.fail_process = true;
    const auto first_b = harness.binding.rotate_to_tree(42);
    REQUIRE(first_b.error == EpochIngressError::none);
    REQUIRE(first_b.update.has_value());
    CHECK(first_b.update->activation.generation == *b_generation);
    CHECK(harness.live_effects.apply_count == 1);
    CHECK(harness.proposal_effects.process_attempt_count == 1);
    CHECK(harness.proposal_effects.process_count == 0);
    CHECK(harness.future.size() == 1);

    harness.proposal_effects.fail_process = false;
    const auto second_a = harness.binding.rotate_to_tree(6);
    REQUIRE(second_a.error == EpochIngressError::none);
    REQUIRE(second_a.update.has_value());
    const auto second_b = harness.binding.rotate_to_tree(42);
    REQUIRE(second_b.error == EpochIngressError::none);
    REQUIRE(second_b.update.has_value());
    CHECK(second_b.update->activation.generation > *b_generation);
    CHECK(harness.live_effects.apply_count == 3);
    CHECK(harness.proposal_effects.process_attempt_count == 1);
    CHECK(harness.proposal_effects.process_count == 0);
    CHECK(harness.future.size() == 0);
    CHECK_FALSE(
        harness.adapter.buffered_proposal_identity(stale_b.key())
            .has_value());
    CHECK_FALSE(
        harness.adapter.processed_proposal_identity(stale_b.key())
            .has_value());

    // ProposalKey deliberately omits the generation.  Keep the received-key
    // tombstone so the same block cannot be revived under the later B
    // generation after its stale claim was retired.
    auto current_b = stale_b;
    current_b.view_generation = second_b.update->activation.generation;
    const auto replay = harness.binding.handle_proposal(
        consensus_message<MsgPropose>(current_b),
        AuthenticatedEpochPeer::replica(1));
    REQUIRE(replay.error == EpochIngressError::none);
    CHECK(replay.admission_disposition == ProposalDisposition::duplicate);
    CHECK(harness.proposal_effects.process_attempt_count == 1);
    CHECK(harness.proposal_effects.process_count == 0);
    CHECK(harness.future.size() == 0);
    CHECK_FALSE(
        harness.adapter.buffered_proposal_identity(stale_b.key())
            .has_value());
    CHECK_FALSE(
        harness.adapter.processed_proposal_identity(stale_b.key())
            .has_value());
}

TEST_CASE("adaptive v2 prospective rotation fails closed at ordinal exhaustion",
          "[adaptive-v2][epoch-runtime][rotation][future-proposal][overflow]")
{
    RotatingProposalHarness harness(
        std::numeric_limits<std::uint32_t>::max());
    const auto active = harness.activation.active_effect();
    const auto proposal = rotation_proposal(
        ConfigurationId{
            active.configuration.epoch_number,
            42,
            active.configuration.epoch_digest},
        active.generation + 1,
        1,
        "ordinal-exhausted-next-tree");

    const auto rejected = harness.binding.handle_proposal(
        consensus_message<MsgPropose>(proposal),
        AuthenticatedEpochPeer::replica(1));
    CHECK(rejected.error == EpochIngressError::state_rejected);
    CHECK(rejected.permission ==
          EpochConsensusPermission::rejected_identity);
    CHECK(harness.future.size() == 0);
    CHECK(harness.proposal_effects.relay_count == 0);

    const auto rotation = harness.binding.rotate_to_tree(42);
    CHECK(rotation.error == EpochIngressError::state_rejected);
    CHECK_FALSE(rotation.update.has_value());
    CHECK(harness.live_effects.apply_count == 0);
}

TEST_CASE("adaptive v2 future horizon admits the last ordinal and rejects an overflowing later offset",
          "[adaptive-v2][epoch-runtime][rotation][future-proposal][overflow][horizon]")
{
    RotatingProposalHarness harness(
        std::numeric_limits<std::uint32_t>::max() - 1);
    const auto active = harness.activation.active_effect();
    const auto final_generation = checked_activation_generation(
        active.configuration.epoch_number,
        std::numeric_limits<std::uint32_t>::max());
    REQUIRE(final_generation.has_value());

    const auto final = rotation_proposal(
        ConfigurationId{
            active.configuration.epoch_number,
            42,
            active.configuration.epoch_digest},
        *final_generation,
        1,
        "last-valid-ordinal");
    const auto final_buffered = harness.binding.handle_proposal(
        consensus_message<MsgPropose>(final),
        AuthenticatedEpochPeer::replica(1));
    REQUIRE(final_buffered.error == EpochIngressError::none);
    REQUIRE(final_buffered.admission_disposition ==
            ProposalDisposition::buffered_future);
    CHECK(harness.future.size() == 1);
    CHECK(harness.proposal_effects.relay_count == 1);

    const auto overflow = rotation_proposal(
        ConfigurationId{
            active.configuration.epoch_number,
            77,
            active.configuration.epoch_digest},
        *final_generation + 1,
        2,
        "overflowing-second-offset");
    const auto rejected = harness.binding.handle_proposal(
        consensus_message<MsgPropose>(overflow),
        AuthenticatedEpochPeer::replica(2));
    CHECK(rejected.error == EpochIngressError::state_rejected);
    CHECK(rejected.permission ==
          EpochConsensusPermission::rejected_identity);
    CHECK_FALSE(rejected.admission_disposition.has_value());
    CHECK(harness.future.size() == 1);
    CHECK(harness.proposal_effects.relay_count == 1);
    CHECK(harness.proposal_effects.process_attempt_count == 0);
}

TEST_CASE("adaptive v1 retains same-epoch future-tree rejection",
          "[adaptive-v1][epoch-runtime][rotation][future-proposal][compatibility]")
{
    Harness harness;
    const auto active = harness.activation.active_effect();
    const auto generation = checked_activation_generation(
        active.configuration.epoch_number,
        static_cast<std::uint64_t>(active.rotation_ordinal) + 1);
    REQUIRE(generation.has_value());
    auto proposal = rotation_proposal(
        ConfigurationId{
            active.configuration.epoch_number,
            1,
            active.configuration.epoch_digest},
        *generation,
        1,
        "adaptive-v1-next-tree");
    proposal.protocol_mode = EpochProtocolMode::adaptive_v1;

    const auto rejected = harness.binding.handle_proposal(
        consensus_message<MsgPropose>(proposal),
        AuthenticatedEpochPeer::replica(1));
    CHECK(rejected.error == EpochIngressError::state_rejected);
    CHECK(rejected.permission ==
          EpochConsensusPermission::rejected_identity);
    CHECK(harness.future.size() == 0);
    CHECK(harness.proposal_effects.relay_count == 0);
    CHECK(harness.proposal_effects.process_attempt_count == 0);
}

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

TEST_CASE("v3 runtime preparation reaches the concrete live topology",
          "[cert13][epoch-live-binding][adaptive-v3][prepare]")
{
    V2Harness harness(
        epoch_v2_input(0),
        {2, 3},
        2,
        2,
        {},
        EpochProtocolMode::adaptive_v3);

    REQUIRE(harness.adapter.prepare_committed_v3(harness.epoch1) ==
            EpochIngressError::none);
    CHECK(harness.live_effects.prepare_count == 1);
    CHECK(harness.live_effects.arm_count == 0);
    CHECK(harness.live_effects.apply_count == 0);
    CHECK(harness.manager_egress.acknowledgements.empty());
    CHECK(harness.manager_egress.statuses.empty());

    REQUIRE(harness.live_effects.prepared_plan.has_value());
    const auto &plan = *harness.live_effects.prepared_plan;
    CHECK(plan.protocol_mode == EpochProtocolMode::adaptive_v3);
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

    CHECK(harness.adapter.prepare_committed_v3(harness.epoch1) ==
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

TEST_CASE(
    "committed successor recovery restores N4 progress without Byzantine votes",
    "[rem-d11-01][adaptive-v2][integration][adversarial][intentional-red]")
{
    constexpr ReplicaID byzantine = 3;
    constexpr ReplicaID missing_correct = 2;
    constexpr std::uint64_t command_commit_height = 40;
    constexpr std::uint64_t activation_delay = 5;
    constexpr std::uint64_t activation_height =
        command_commit_height + activation_delay;

    const auto quorum =
        derive_byzantine_quorum(adversarial_membership().size());
    REQUIRE(quorum.has_value());
    REQUIRE(quorum->replica_count == 4);
    REQUIRE(quorum->fault_threshold == 1);
    REQUIRE(quorum->quorum == 3);

    const auto issuer_key = recovery_issuer_key();
    AdversarialV2Replica correct0(0, issuer_key);
    AdversarialV2Replica correct1(1, issuer_key);
    AdversarialV2Replica correct2(missing_correct, issuer_key);
    REQUIRE(correct0.epoch0.epoch_digest() ==
            correct1.epoch0.epoch_digest());
    REQUIRE(correct1.epoch0.epoch_digest() ==
            correct2.epoch0.epoch_digest());

    const auto exact_definition = adversarial_epoch_v2_input(
        1,
        correct0.epoch0.epoch_digest(),
        "rem-d11-01-exact-successor");
    const auto command = recovery_command(
        exact_definition, activation_delay, issuer_key);
    const AdaptiveV2EpochChangeBundle exact_bundle(
        command, exact_definition, bundle_limits());

    auto wrong_definition = adversarial_epoch_v2_input(
        1,
        correct0.epoch0.epoch_digest(),
        "rem-d11-01-mismatched-successor");
    wrong_definition.evidence_cutoff = 99;
    const auto wrong_command = recovery_command(
        wrong_definition, activation_delay, issuer_key);
    const AdaptiveV2EpochChangeBundle wrong_bundle(
        wrong_command, wrong_definition, bundle_limits());
    REQUIRE(wrong_command.payload.successor_epoch_digest !=
            command.payload.successor_epoch_digest);

    const std::vector<ReplicaID> command_certificate_voters{
        0, 1, byzantine};
    CHECK(command_certificate_voters.size() == quorum->quorum);
    CHECK(std::find(
              command_certificate_voters.begin(),
              command_certificate_voters.end(),
              byzantine) != command_certificate_voters.end());
    CHECK(std::find(
              command_certificate_voters.begin(),
              command_certificate_voters.end(),
              missing_correct) == command_certificate_voters.end());

    std::vector<AdversarialV2Replica *> definition_holders{
        &correct0, &correct1};
    for (auto *replica : definition_holders)
    {
        REQUIRE(recover_exact_committed_bundle(
            *replica, command, exact_bundle));
        const auto *const successor =
            replica->store.find_epoch_by_digest(
                command.payload.successor_epoch_digest);
        REQUIRE(successor != nullptr);
        REQUIRE(replica->adapter.prepare_committed_v2(*successor) ==
                EpochIngressError::none);
        REQUIRE(replica->activation.record_committed_v2(
                    command, command_commit_height)
                    .disposition ==
                ActivationRecordDisposition::recorded);
    }

    const auto missing_observation =
        correct2.activation.record_committed_v2(
            command, command_commit_height);
    CHECK((
        missing_observation.disposition ==
            ActivationRecordDisposition::missing_definition ||
        missing_observation.disposition ==
            ActivationRecordDisposition::recorded));
    CHECK(correct2.activation.committed_v2_record().has_value());
    CHECK(correct2.activation.blocked_reason() ==
          ActivationBlockReason::missing_definition);
    CHECK_FALSE(correct2.activation.admits_new_proposals());

    std::vector<AdversarialV2Replica *> correct_replicas{
        &correct0, &correct1, &correct2};
    const auto available_correct_voters = [&]() {
        std::vector<ReplicaID> voters;
        for (std::size_t index = 0; index < correct_replicas.size(); ++index)
            if (correct_replicas[index]->activation.admits_new_proposals())
                voters.push_back(static_cast<ReplicaID>(index));
        return voters;
    };

    const auto stalled_voters = available_correct_voters();
    CHECK(stalled_voters == std::vector<ReplicaID>{0, 1});
    CHECK(stalled_voters.size() < quorum->quorum);
    CHECK(std::find(
              stalled_voters.begin(), stalled_voters.end(), byzantine) ==
          stalled_voters.end());

    const auto retained_before_wrong =
        correct2.activation.committed_v2_record();
    CHECK_FALSE(recover_exact_committed_bundle(
        correct2, command, wrong_bundle));
    CHECK(correct2.store.find_epoch_by_digest(
              command.payload.successor_epoch_digest) == nullptr);
    CHECK(correct2.activation.active_effect().definition ==
          &correct2.epoch0);
    CHECK_FALSE(correct2.activation.admits_new_proposals());
    CHECK(correct2.activation.committed_v2_record() ==
          retained_before_wrong);

    REQUIRE(recover_exact_committed_bundle(
        correct2, command, exact_bundle));
    const auto *const recovered_definition =
        correct2.store.find_epoch_by_digest(
            command.payload.successor_epoch_digest);
    REQUIRE(recovered_definition != nullptr);
    CHECK(recovered_definition->epoch_digest() ==
          command.payload.successor_epoch_digest);
    const auto *const reference_definition =
        correct0.store.find_epoch_by_digest(
            command.payload.successor_epoch_digest);
    REQUIRE(reference_definition != nullptr);
    CHECK(recovered_definition->canonical_serialization() ==
          reference_definition->canonical_serialization());

    const auto replayed = correct2.activation.record_committed_v2(
        command, command_commit_height);
    CHECK((
        replayed.disposition == ActivationRecordDisposition::recorded ||
        replayed.disposition == ActivationRecordDisposition::duplicate));
    const auto retained = correct2.activation.committed_v2_record();
    CHECK(retained.has_value());
    if (retained.has_value())
    {
        CHECK(retained->predecessor_epoch_digest ==
              command.payload.predecessor_epoch_digest);
        CHECK(retained->successor_epoch_digest ==
              command.payload.successor_epoch_digest);
        CHECK(retained->payload_digest ==
              epoch_change_payload_digest(command.payload));
        CHECK(retained->command_commit_height ==
              command_commit_height);
        CHECK(retained->activation_height == activation_height);
    }
    REQUIRE(correct2.adapter.prepare_committed_v2(
                *recovered_definition) == EpochIngressError::none);
    CHECK(correct2.activation.admits_new_proposals());

    const auto resumed_voters = available_correct_voters();
    CHECK(resumed_voters == std::vector<ReplicaID>{0, 1, 2});
    CHECK(resumed_voters.size() == quorum->quorum);
    CHECK(std::find(
              resumed_voters.begin(), resumed_voters.end(), byzantine) ==
          resumed_voters.end());

    for (std::size_t index = 0; index < correct_replicas.size(); ++index)
    {
        CAPTURE(index);
        const auto activated =
            correct_replicas[index]->binding.on_v2_post_block_commit(
                activation_height,
                correct_replicas[index]->epoch0.epoch_digest());
        CHECK(activated.error == EpochIngressError::none);
        CHECK(activated.transition ==
              ActivationTransition::activated);
        CHECK(correct_replicas[index]
                  ->activation.active_effect()
                  .configuration.epoch_number == 1);
        CHECK(correct_replicas[index]
                  ->activation.active_effect()
                  .configuration.epoch_digest ==
              command.payload.successor_epoch_digest);
    }

    const auto successor = correct0.activation.active_effect().configuration;
    CHECK(correct1.activation.active_effect().configuration == successor);
    CHECK(correct2.activation.active_effect().configuration == successor);

    const auto successor_commit_voters = available_correct_voters();
    CHECK(successor_commit_voters ==
          std::vector<ReplicaID>{0, 1, 2});
    CHECK(successor_commit_voters.size() == quorum->quorum);
    CHECK(std::find(
              successor_commit_voters.begin(),
              successor_commit_voters.end(),
              byzantine) == successor_commit_voters.end());
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

TEST_CASE(
    "shape change preserves an f5 draining context across committed f2 activation",
    "[shape25][adaptive-v2][epoch-live-binding][draining][f5-to-f2]")
{
    constexpr std::uint64_t commit_height = 90;
    constexpr std::uint64_t delay = 3;
    const std::vector<ReplicaID> roots{1, 2, 3, 4, 0};
    const std::vector<ReplicaID> wait_exempt{5, 6};
    const auto quorum = derive_byzantine_quorum(membership().size());
    REQUIRE(quorum.has_value());
    REQUIRE(quorum->quorum == 5);

    V2Harness harness(
        shaped_epoch_v2_input(0, roots, 5, 2, wait_exempt),
        roots,
        2,
        2,
        wait_exempt);
    REQUIRE(harness.epoch0.trees().size() == quorum->quorum);
    REQUIRE(harness.epoch1.trees().size() == quorum->quorum);
    for (const auto &tree : harness.epoch0.trees())
    {
        CHECK(tree.fanout == 5);
        CHECK(tree.pipeline_stretch == 2);
        CHECK(tree.wait_exempt_leaves == wait_exempt);
    }
    for (const auto &tree : harness.epoch1.trees())
    {
        CHECK(tree.fanout == 2);
        CHECK(tree.pipeline_stretch == 2);
        CHECK(tree.wait_exempt_leaves == wait_exempt);
    }

    const auto predecessor = harness.activation.active_effect();
    REQUIRE(predecessor.definition == &harness.epoch0);
    const auto *predecessor_tree = harness.store.find_tree(
        predecessor.configuration.epoch_number,
        predecessor.configuration.tree_id);
    REQUIRE(predecessor_tree != nullptr);
    const ProposalKey predecessor_key{
        predecessor.configuration, digest("shape25-f5-draining")};
    const auto predecessor_metadata =
        make_exact_proposal_context_metadata(
            predecessor_key,
            0,
            *predecessor_tree,
            quorum->quorum);
    REQUIRE(predecessor_metadata.has_value());
    auto predecessor_lease =
        harness.contexts.admit_remote(*predecessor_metadata);
    REQUIRE(predecessor_lease.has_value());
    const auto predecessor_timer =
        harness.contexts.arm_timer(*predecessor_lease);
    REQUIRE(predecessor_timer != 0);

    const auto exact_partition = [](const ProposalTreeSnapshot &tree) {
        const std::set<ReplicaID> assigned(
            tree.assigned_subtree.begin(), tree.assigned_subtree.end());
        std::set<ReplicaID> partition = tree.required_subtree;
        for (const auto replica : tree.optional_subtree)
            CHECK(partition.insert(replica).second);
        CHECK(partition == assigned);
        const std::set<ReplicaID> children(
            tree.direct_children.begin(), tree.direct_children.end());
        CHECK(children.size() == tree.direct_children.size());
        std::set<ReplicaID> child_members;
        for (const auto &[child, subtree] : tree.child_subtrees)
        {
            CHECK(children.count(child) == 1);
            for (const auto replica : subtree)
                CHECK(child_members.insert(replica).second);
        }
        child_members.insert(tree.local_replica);
        CHECK(child_members == assigned);
    };
    exact_partition(predecessor_lease->tree());
    CHECK(predecessor_lease->tree().fanout == 5);
    CHECK(predecessor_lease->tree().pipeline_stretch == 2);
    CHECK(predecessor_lease->tree().direct_children ==
          std::vector<ReplicaID>{6});
    CHECK(predecessor_lease->tree().required_subtree ==
          std::set<ReplicaID>{0});
    CHECK(predecessor_lease->tree().optional_subtree ==
          std::set<ReplicaID>{6});
    CHECK(harness.contexts.frozen_global_quorum(*predecessor_lease) ==
          std::optional<std::size_t>{quorum->quorum});

    REQUIRE(harness.adapter.prepare_committed_v2(harness.epoch1) ==
            EpochIngressError::none);
    REQUIRE(harness.activation.record_committed_v2(
                harness.command(delay), commit_height)
                .disposition == ActivationRecordDisposition::recorded);
    REQUIRE(harness.binding.on_v2_post_block_commit(
                commit_height + delay,
                harness.epoch0.epoch_digest())
                .transition == ActivationTransition::activated);

    const auto successor = harness.activation.active_effect();
    REQUIRE(successor.definition == &harness.epoch1);
    REQUIRE(successor.configuration != predecessor.configuration);
    REQUIRE(harness.contexts.revalidate(*predecessor_lease));
    const auto frozen = harness.contexts.snapshot(predecessor_key);
    REQUIRE(frozen.has_value());
    CHECK(frozen->timer_generation == predecessor_timer);
    CHECK(predecessor_lease->tree().fanout == 5);
    CHECK(predecessor_lease->tree().direct_children ==
          std::vector<ReplicaID>{6});

    const auto *successor_tree = harness.store.find_tree(
        successor.configuration.epoch_number,
        successor.configuration.tree_id);
    REQUIRE(successor_tree != nullptr);
    const ProposalKey successor_key{
        successor.configuration, digest("shape25-f2-new")};
    const auto successor_metadata = make_exact_proposal_context_metadata(
        successor_key, 0, *successor_tree, quorum->quorum);
    REQUIRE(successor_metadata.has_value());
    auto successor_lease =
        harness.contexts.admit_remote(*successor_metadata);
    REQUIRE(successor_lease.has_value());
    const auto successor_timer =
        harness.contexts.arm_timer(*successor_lease);
    REQUIRE(successor_timer != 0);
    REQUIRE(successor_timer != predecessor_timer);
    exact_partition(successor_lease->tree());
    CHECK(successor_lease->tree().fanout == 2);
    CHECK(successor_lease->tree().pipeline_stretch == 2);
    CHECK(successor_lease->tree().direct_children ==
          std::vector<ReplicaID>{3, 4});
    CHECK(successor_lease->tree().required_subtree ==
          std::set<ReplicaID>{0, 3, 4});
    CHECK(successor_lease->tree().optional_subtree.empty());
    CHECK(harness.contexts.frozen_global_quorum(*successor_lease) ==
          std::optional<std::size_t>{quorum->quorum});

    bool predecessor_timeout_bound_to_f5 = false;
    REQUIRE(harness.contexts.dispatch_timer(
        predecessor_key,
        predecessor_timer,
        [&](const ProposalContextLease &lease) {
            predecessor_timeout_bound_to_f5 =
                lease.key().configuration ==
                    predecessor.configuration &&
                lease.tree().fanout == 5 &&
                lease.tree().pipeline_stretch == 2 &&
                lease.tree().direct_children ==
                    std::vector<ReplicaID>{6};
            CHECK(harness.contexts.transition(
                      lease,
                      ProposalContextEvent::aggregation_timeout) ==
                  ProposalTransitionResult::retained_open);
        }));
    CHECK(predecessor_timeout_bound_to_f5);
    const auto after_timeout = harness.contexts.snapshot(predecessor_key);
    REQUIRE(after_timeout.has_value());
    CHECK(after_timeout->timer_generation == 0);
    CHECK(after_timeout->pass_through);
    CHECK(after_timeout->phase == ProposalContextPhase::delta_open);
    CHECK(harness.contexts.snapshot(successor_key)->timer_generation ==
          successor_timer);

    auto late_vote = envelope(
        predecessor,
        EpochConsensusWireKind::vote,
        6,
        {0xF5, 0xF2},
        "shape25-f5-draining");
    late_vote.protocol_mode = EpochProtocolMode::adaptive_v2;
    late_vote.block_hash = predecessor_key.block_hash;
    late_vote.proposer = predecessor_lease->tree().root;
    const auto accepted_late = harness.binding.handle_vote(
        consensus_message<MsgVote>(late_vote),
        AuthenticatedEpochPeer::replica(6));
    CHECK(accepted_late.error == EpochIngressError::none);
    CHECK(accepted_late.permission ==
          EpochConsensusPermission::accept_contribution);
    REQUIRE(harness.continuations.votes.size() == 1);

    REQUIRE(harness.contexts.record_local_signer(*predecessor_lease));
    REQUIRE(harness.contexts.record_verified_direct(
        *predecessor_lease, 6, 6));
    REQUIRE(harness.contexts.mark_forwarded_signers(
        *predecessor_lease, std::set<ReplicaID>{0, 6}));
    CHECK(harness.contexts.transition(
              *predecessor_lease,
              ProposalContextEvent::late_contribution_forwarded) ==
          ProposalTransitionResult::terminal_closed);

    late_vote.body = {0xF5, 0xF3};
    CHECK(harness.binding.handle_vote(
              consensus_message<MsgVote>(late_vote),
              AuthenticatedEpochPeer::replica(6))
              .permission == EpochConsensusPermission::rejected_identity);
    CHECK(harness.continuations.votes.size() == 1);
    CHECK(harness.contexts.context_status(successor_key) ==
          ProposalContextStatus::admitted_open);
    CHECK(harness.contexts.snapshot(successor_key)->timer_generation ==
          successor_timer);
    REQUIRE(harness.contexts.close(
        successor_key, ProposalContextEvent::proposal_aborted));
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

TEST_CASE("consensus envelopes bind the exact adaptive protocol mode",
          "[c08][cert13][adaptive-v2][adaptive-v3][consensus][wire][mode][intentional-red]")
{
    const ConfigurationId configuration{
        0, 0, digest("adaptive-mode-epoch")};
    EpochConsensusEnvelope adaptive_v1{
        configuration,
        1,
        digest("adaptive-mode-block"),
        1,
        0,
        bytearray_t{0xC0, 0x08},
        kEpochConsensusWireSchemaVersion,
        EpochProtocolMode::adaptive_v1,
        EpochConsensusWireKind::proposal};
    auto adaptive_v2 = adaptive_v1;
    adaptive_v2.protocol_mode = EpochProtocolMode::adaptive_v2;
    auto adaptive_v3 = adaptive_v1;
    adaptive_v3.protocol_mode = EpochProtocolMode::adaptive_v3;

    const auto v1_wire = encode_epoch_consensus_envelope(
        adaptive_v1, limits());
    const auto v2_wire = encode_epoch_consensus_envelope(
        adaptive_v2, limits());
    const auto v3_wire = encode_epoch_consensus_envelope(
        adaptive_v3, limits());

    const auto decoded_v1 = decode_epoch_consensus_envelope(
        v1_wire,
        EpochConsensusWireKind::proposal,
        EpochProtocolMode::adaptive_v1,
        limits());
    REQUIRE(decoded_v1);
    CHECK(decoded_v1.value->protocol_mode ==
          EpochProtocolMode::adaptive_v1);

    const auto decoded_v2 = decode_epoch_consensus_envelope(
        v2_wire,
        EpochConsensusWireKind::proposal,
        EpochProtocolMode::adaptive_v2,
        limits());
    CHECK(decoded_v2);
    if (decoded_v2)
        CHECK(decoded_v2.value->protocol_mode ==
              EpochProtocolMode::adaptive_v2);

    const auto decoded_v3 = decode_epoch_consensus_envelope(
        v3_wire,
        EpochConsensusWireKind::proposal,
        EpochProtocolMode::adaptive_v3,
        limits());
    REQUIRE(decoded_v3);
    CHECK(decoded_v3.value->protocol_mode ==
          EpochProtocolMode::adaptive_v3);

    CHECK(decode_epoch_consensus_envelope(
              v1_wire,
              EpochConsensusWireKind::proposal,
              EpochProtocolMode::adaptive_v2,
              limits())
              .error == EpochConsensusWireError::mode_mismatch);
    CHECK(decode_epoch_consensus_envelope(
              v3_wire,
              EpochConsensusWireKind::proposal,
              EpochProtocolMode::adaptive_v2,
              limits())
              .error == EpochConsensusWireError::mode_mismatch);
    CHECK(decode_epoch_consensus_envelope(
              v2_wire,
              EpochConsensusWireKind::proposal,
              EpochProtocolMode::adaptive_v3,
              limits())
              .error == EpochConsensusWireError::mode_mismatch);
    CHECK(decode_epoch_consensus_envelope(
              v2_wire,
              EpochConsensusWireKind::proposal,
              EpochProtocolMode::adaptive_v1,
              limits())
              .error == EpochConsensusWireError::mode_mismatch);

    auto legacy = adaptive_v1;
    legacy.protocol_mode = EpochProtocolMode::legacy_static;
    CHECK_THROWS_AS(
        encode_epoch_consensus_envelope(legacy, limits()),
        std::invalid_argument);
}
