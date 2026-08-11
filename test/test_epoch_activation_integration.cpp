#include <array>
#include <chrono>
#include <cstddef>
#include <cstdint>
#include <cstdlib>
#include <functional>
#include <limits>
#include <memory>
#include <new>
#include <optional>
#include <stdexcept>
#include <string>
#include <type_traits>
#include <utility>
#include <vector>

#include "catch.hpp"
#include "hotstuff/epoch_activation.h"
#include "hotstuff/future_proposal_buffer.h"
#include "hotstuff/hotstuff.h"
#include "hotstuff/leader_progress.h"
#include "hotstuff/proposal_admission.h"
#include "hotstuff/proposal_context.h"
#include "support/bls_fixtures.h"

namespace d11_allocation_guard
{

constexpr std::size_t disabled = std::numeric_limits<std::size_t>::max();
thread_local std::size_t rejected_allocation_minimum = disabled;
thread_local bool rejected_allocation = false;
thread_local bool reject_next_allocation = false;

bool reject(std::size_t size) noexcept
{
    if (reject_next_allocation)
    {
        reject_next_allocation = false;
        rejected_allocation = true;
        return true;
    }
    if (rejected_allocation_minimum == disabled ||
        size < rejected_allocation_minimum)
        return false;
    rejected_allocation = true;
    return true;
}

class RejectAtLeast
{
public:
    explicit RejectAtLeast(std::size_t minimum) noexcept
    {
        rejected_allocation_minimum = minimum;
        rejected_allocation = false;
    }

    ~RejectAtLeast()
    {
        rejected_allocation_minimum = disabled;
    }

    bool triggered() const noexcept
    {
        return rejected_allocation;
    }

    RejectAtLeast(const RejectAtLeast &) = delete;
    RejectAtLeast &operator=(const RejectAtLeast &) = delete;
};

class RejectAll final : public RejectAtLeast
{
public:
    RejectAll() noexcept : RejectAtLeast(0) {}
};

void reject_next() noexcept
{
    reject_next_allocation = true;
    rejected_allocation = false;
}

bool next_rejection_triggered() noexcept
{
    return rejected_allocation && !reject_next_allocation;
}

void clear_pending_rejection() noexcept
{
    reject_next_allocation = false;
}

} // namespace d11_allocation_guard

void *operator new(std::size_t size)
{
    if (d11_allocation_guard::reject(size))
        throw std::bad_alloc();
    if (size == 0)
        size = 1;
    if (auto *const allocation = std::malloc(size))
        return allocation;
    throw std::bad_alloc();
}

void *operator new[](std::size_t size)
{
    return ::operator new(size);
}

void operator delete(void *allocation) noexcept
{
    std::free(allocation);
}

void operator delete(void *allocation, std::size_t) noexcept
{
    std::free(allocation);
}

void operator delete[](void *allocation) noexcept
{
    std::free(allocation);
}

void operator delete[](void *allocation, std::size_t) noexcept
{
    std::free(allocation);
}

/*
 * REM-D11 phase-2 contract
 * ------------------------
 * The fake transport below is executable: it registers typed handlers and
 * passes connection-authenticated identity separately from opaque payload.
 * Live certificate-to-role mapping remains deliberately deferred.
 *
 * The HotStuff-facing adapter performs authentication, DataStream size
 * preflight, bounded decode, and fallible runtime preparation in that order.
 * Only then may phase-1 staging mutate or emit an acknowledgement. Exact-H
 * activation consumes one digest/generation-bound token through a nofail
 * commit. Future proposal draining is separately retryable after that swap.
 *
 * Consensus generation is encoded in one explicit versioned envelope carried
 * only by the existing MsgPropose (0x0), MsgVote (0x1), and MsgRelay (0x4)
 * paths. The same bytes and generation must survive proposal admission,
 * buffering, relay, activation drain, and delayed-message rejection.
 */
#if __has_include("hotstuff/epoch_runtime.h")
#include "hotstuff/epoch_runtime.h"
#define KAURI_HAS_D11_EPOCH_RUNTIME_API 1
#else
#define KAURI_HAS_D11_EPOCH_RUNTIME_API 0

namespace hotstuff
{

enum class EpochPeerRole : std::uint8_t
{
    manager = 1,
    replica,
};

struct AuthenticatedEpochPeer
{
    EpochPeerRole role{EpochPeerRole::manager};
    std::optional<ReplicaID> replica_id;

    static AuthenticatedEpochPeer manager() noexcept;
    static AuthenticatedEpochPeer replica(ReplicaID replica_id) noexcept;
};

enum class EpochIngressError : std::uint8_t
{
    none = 0,
    unauthorized_peer,
    wire_rejected,
    state_rejected,
    runtime_preparation_failed,
    validation_failed,
    missing_prepared_runtime,
    future_drain_failed,
};

enum class EpochConsensusPermission : std::uint8_t
{
    admit_or_buffer = 1,
    drain_existing,
    accept_contribution,
    paused,
    rejected_identity,
};

constexpr std::uint32_t kEpochConsensusWireSchemaVersion = 1;

enum class EpochConsensusWireKind : std::uint8_t
{
    proposal = 1,
    vote,
    relay,
};

struct EpochConsensusEnvelope
{
    ConfigurationId configuration;
    std::uint64_t view_generation{0};
    uint256_t block_hash;
    ReplicaID originator{0};
    ReplicaID proposer{0};
    bytearray_t body;
    std::uint32_t wire_schema_version{
        kEpochConsensusWireSchemaVersion};
    EpochProtocolMode protocol_mode{EpochProtocolMode::adaptive_v1};
    EpochConsensusWireKind kind{EpochConsensusWireKind::proposal};

    ProposalKey key() const;
};

enum class EpochConsensusWireError : std::uint8_t
{
    none = 0,
    invalid_limits,
    payload_too_large,
    unsupported_schema,
    mode_mismatch,
    unexpected_kind,
    truncated,
    trailing_bytes,
    invalid_generation,
    invalid_body,
};

struct EpochConsensusWireDecodeResult
{
    EpochConsensusWireError error{EpochConsensusWireError::none};
    std::optional<EpochConsensusEnvelope> value;

    explicit operator bool() const noexcept
    {
        return error == EpochConsensusWireError::none && value.has_value();
    }
};

bytearray_t encode_epoch_consensus_envelope(
    const EpochConsensusEnvelope &envelope,
    const EpochWireLimits &limits);
EpochConsensusWireDecodeResult decode_epoch_consensus_envelope(
    const bytearray_t &payload,
    EpochConsensusWireKind expected_kind,
    EpochProtocolMode expected_mode,
    const EpochWireLimits &limits) noexcept;

struct EpochBufferedProposalIdentity
{
    ProposalKey key;
    std::uint64_t view_generation{0};
    uint256_t wire_digest;
};

std::optional<std::uint64_t> buffered_proposal_view_generation(
    const BufferedProposal &proposal) noexcept;

class EpochConsensusBodyValidator
{
public:
    virtual ~EpochConsensusBodyValidator() = default;

    virtual bool validate_proposal(
        const EpochConsensusEnvelope &envelope,
        const AuthenticatedEpochPeer &authenticated_peer) = 0;
    virtual bool validate_vote(
        const EpochConsensusEnvelope &envelope,
        const AuthenticatedEpochPeer &authenticated_peer) = 0;
    virtual bool validate_relay(
        const EpochConsensusEnvelope &envelope,
        const AuthenticatedEpochPeer &authenticated_peer) = 0;
};

struct EpochTreeRuntimeInput
{
    ConfigurationId configuration;
    EpochTreeDefinition tree;
    ReplicaID leader{0};
    std::uint64_t activation_generation{0};
};

struct EpochRuntimePlan
{
    StageEpochDefinition stage;
    bytearray_t canonical_stage;
    std::vector<EpochTreeRuntimeInput> trees;
    uint256_t canonical_digest;
};

struct PreparedEpochRuntime
{
    std::uint64_t token{0};
    uint256_t canonical_plan_digest;
};

struct EpochRuntimeUpdate
{
    EpochActivationEffect activation;
    LeaderViewId leader_view;
};

struct EpochRuntimeRotation
{
    EpochActivationEffect activation;
    LeaderViewId leader_view;
};

class EpochRuntimeTransaction
{
public:
    virtual ~EpochRuntimeTransaction() = default;

    virtual std::optional<PreparedEpochRuntime> prepare(
        const EpochRuntimePlan &plan) = 0;
    virtual void discard(PreparedEpochRuntime prepared) noexcept = 0;
    virtual void commit(
        PreparedEpochRuntime prepared,
        const EpochRuntimeUpdate &update) noexcept = 0;
    virtual void rotate(
        const EpochRuntimeRotation &update) noexcept = 0;
};

struct FutureProposalClaim
{
    std::uint64_t token{0};
    BufferedProposal proposal;
};

class RetryableFutureProposalStore
{
public:
    virtual ~RetryableFutureProposalStore() = default;

    virtual bool insert(BufferedProposal proposal) = 0;
    virtual std::optional<FutureProposalClaim> claim_next(
        const ConfigurationId &configuration) = 0;
    virtual void process_active(const FutureProposalClaim &claim) = 0;
    virtual void acknowledge(std::uint64_t token) noexcept = 0;
    virtual void release(std::uint64_t token) noexcept = 0;
    virtual void complete(
        const ConfigurationId &configuration) noexcept = 0;
    virtual std::size_t size() const noexcept = 0;
};

struct ReplicaStageIngressResult
{
    EpochIngressError error{EpochIngressError::none};
    EpochWireError wire_error{EpochWireError::none};
    std::optional<ReplicaStageDisposition> disposition;
    std::optional<StageAck> acknowledgement;
};

struct ReplicaArmIngressResult
{
    EpochIngressError error{EpochIngressError::none};
    EpochWireError wire_error{EpochWireError::none};
    std::optional<ReplicaArmDisposition> disposition;
};

struct ManagerAckIngressResult
{
    EpochIngressError error{EpochIngressError::none};
    EpochWireError wire_error{EpochWireError::none};
    std::optional<StageAckDisposition> disposition;
    std::size_t accepted_acknowledgements{0};
    std::size_t required_acknowledgements{0};
    std::optional<ArmActivation> arm;
};

struct EpochRotationResult
{
    EpochIngressError error{EpochIngressError::none};
    std::optional<EpochRuntimeUpdate> update;
};

struct EpochCommitIngressResult
{
    EpochIngressError error{EpochIngressError::none};
    ActivationTransition transition{ActivationTransition::waiting};
    ActivationBlockReason blocked_reason{ActivationBlockReason::none};
    std::optional<EpochRuntimeUpdate> update;
};

enum class EpochFutureDrainStatus : std::uint8_t
{
    complete = 1,
    process_failed,
    allocation_failed,
    inactive_configuration,
    stopped,
};

struct EpochFutureDrainResult
{
    EpochFutureDrainStatus status{EpochFutureDrainStatus::complete};
    std::size_t processed{0};
    std::size_t remaining{0};
};

struct EpochConsensusIngressResult
{
    EpochIngressError error{EpochIngressError::none};
    EpochConsensusWireError wire_error{EpochConsensusWireError::none};
    EpochConsensusPermission permission{
        EpochConsensusPermission::rejected_identity};
    std::optional<EpochConsensusEnvelope> decoded_envelope;
    std::optional<ProposalDisposition> admission_disposition;
};

class HotStuffEpochRuntimeAdapter final
{
public:
    HotStuffEpochRuntimeAdapter(
        ReplicaEpochActivation &activation,
        ProposalContextLifecycle &contexts,
        ProposalAdmissionCoordinator &admission,
        RetryableFutureProposalStore &future_proposals,
        EpochConsensusBodyValidator &body_validator,
        EpochProtocolMode expected_mode,
        EpochWireLimits wire_limits,
        EpochRuntimeTransaction &transaction);
    ~HotStuffEpochRuntimeAdapter();

    HotStuffEpochRuntimeAdapter(const HotStuffEpochRuntimeAdapter &) = delete;
    HotStuffEpochRuntimeAdapter &operator=(
        const HotStuffEpochRuntimeAdapter &) = delete;

    ReplicaStageIngressResult handle_stage(
        MsgStageEpochDefinition &&message,
        const AuthenticatedEpochPeer &authenticated_peer,
        const EpochValidationContext &validation_context);
    ReplicaArmIngressResult handle_arm(
        MsgArmActivation &&message,
        const AuthenticatedEpochPeer &authenticated_peer);
    EpochCommitIngressResult on_predecessor_commit(
        std::uint64_t height,
        const uint256_t &predecessor_digest) noexcept;
    EpochCommitIngressResult replay_blocked_commit() noexcept;
    EpochRotationResult rotate_to_tree(std::uint32_t tree_id) noexcept;

    EpochConsensusIngressResult handle_proposal(
        MsgPropose &&message,
        const AuthenticatedEpochPeer &authenticated_peer) const noexcept;
    EpochConsensusIngressResult handle_existing_proposal(
        MsgPropose &&message,
        const AuthenticatedEpochPeer &authenticated_peer,
        const ProposalContextLease &lease) const noexcept;
    EpochConsensusIngressResult handle_vote(
        MsgVote &&message,
        const AuthenticatedEpochPeer &authenticated_peer) const noexcept;
    EpochConsensusIngressResult handle_relay(
        MsgRelay &&message,
        const AuthenticatedEpochPeer &authenticated_peer) const noexcept;

    std::optional<EpochBufferedProposalIdentity>
    buffered_proposal_identity(const ProposalKey &key) const noexcept;
    std::optional<EpochBufferedProposalIdentity>
    processed_proposal_identity(const ProposalKey &key) const noexcept;

    EpochFutureDrainResult drain_activated_futures() noexcept;

private:
    struct State;
    std::unique_ptr<State> state_;
};

class ManagerEpochAckEndpoint final
{
public:
    ManagerEpochAckEndpoint(
        EpochAckTracker &tracker,
        EpochProtocolMode expected_mode,
        EpochWireLimits wire_limits);
    ~ManagerEpochAckEndpoint();

    ManagerAckIngressResult handle_ack(
        MsgStageAck &&message,
        const AuthenticatedEpochPeer &authenticated_peer);

private:
    struct State;
    std::unique_ptr<State> state_;
};

} // namespace hotstuff
#endif

/*
 * REM-D11 phase-3b real-runtime wiring contract
 * ------------------------------------------------
 * Keep these declarations link-visible when the production wiring header is
 * absent. This lets the integration translation unit compile while the target
 * remains intentionally RED on only the missing concrete production symbols.
 */
#if __has_include("hotstuff/epoch_runtime_wiring.h")
#include "hotstuff/epoch_runtime_wiring.h"
#define KAURI_HAS_D11_EPOCH_RUNTIME_WIRING_API 1
#else
#define KAURI_HAS_D11_EPOCH_RUNTIME_WIRING_API 0

namespace hotstuff
{

class HotStuffEpochRuntimeTransaction final : public EpochRuntimeTransaction
{
public:
    HotStuffEpochRuntimeTransaction(
        ProposalAdmissionCoordinator &admission,
        ProposalContextLifecycle &contexts);
    ~HotStuffEpochRuntimeTransaction() override;

    std::optional<PreparedEpochRuntime> prepare(
        const EpochRuntimePlan &plan) override;
    void discard(PreparedEpochRuntime prepared) noexcept override;
    void commit(
        PreparedEpochRuntime prepared,
        const EpochRuntimeUpdate &update) noexcept override;
    void rotate(const EpochRuntimeRotation &update) noexcept override;

private:
    struct State;
    std::unique_ptr<State> state_;
};

class HotStuffRetryableFutureProposalStore final
    : public RetryableFutureProposalStore
{
public:
    HotStuffRetryableFutureProposalStore(
        FutureProposalBuffer &buffer,
        ProposalAdmissionCoordinator &admission);
    ~HotStuffRetryableFutureProposalStore() override;

    bool insert(BufferedProposal proposal) override;
    std::optional<FutureProposalClaim> claim_next(
        const ConfigurationId &configuration) override;
    void process_active(const FutureProposalClaim &claim) override;
    void acknowledge(std::uint64_t token) noexcept override;
    void release(std::uint64_t token) noexcept override;
    void complete(
        const ConfigurationId &configuration) noexcept override;
    std::size_t size() const noexcept override;

private:
    struct State;
    std::unique_ptr<State> state_;
};

class HotStuffEpochConsensusBodyValidator final
    : public EpochConsensusBodyValidator
{
public:
    explicit HotStuffEpochConsensusBodyValidator(
        HotStuffCore &structural_decoder);
    HotStuffEpochConsensusBodyValidator(
        HotStuffCore &structural_decoder,
        const EpochStore &epochs);
    ~HotStuffEpochConsensusBodyValidator() override;

    bool validate_proposal(
        const EpochConsensusEnvelope &envelope,
        const AuthenticatedEpochPeer &peer) override;
    bool validate_vote(
        const EpochConsensusEnvelope &envelope,
        const AuthenticatedEpochPeer &peer) override;
    bool validate_relay(
        const EpochConsensusEnvelope &envelope,
        const AuthenticatedEpochPeer &peer) override;

private:
    struct State;
    std::unique_ptr<State> state_;
};

class HotStuffEpochHandlerRegistry
{
public:
    using StageHandler = std::function<ReplicaStageIngressResult(
        MsgStageEpochDefinition &&,
        const AuthenticatedEpochPeer &,
        const EpochValidationContext &)>;
    using ArmHandler = std::function<ReplicaArmIngressResult(
        MsgArmActivation &&,
        const AuthenticatedEpochPeer &)>;
    using ProposalHandler = std::function<EpochConsensusIngressResult(
        MsgPropose &&,
        const AuthenticatedEpochPeer &)>;
    using VoteHandler = std::function<EpochConsensusIngressResult(
        MsgVote &&,
        const AuthenticatedEpochPeer &)>;
    using RelayHandler = std::function<EpochConsensusIngressResult(
        MsgRelay &&,
        const AuthenticatedEpochPeer &)>;

    virtual ~HotStuffEpochHandlerRegistry() = default;

    virtual void register_stage_handler(
        opcode_t opcode, StageHandler handler) = 0;
    virtual void register_arm_handler(
        opcode_t opcode, ArmHandler handler) = 0;
    virtual void register_proposal_handler(
        opcode_t opcode, ProposalHandler handler) = 0;
    virtual void register_vote_handler(
        opcode_t opcode, VoteHandler handler) = 0;
    virtual void register_relay_handler(
        opcode_t opcode, RelayHandler handler) = 0;
};

class HotStuffEpochHandlerInstaller final
{
public:
    static void install(
        HotStuffEpochHandlerRegistry &registry,
        HotStuffEpochRuntimeAdapter &adapter);
};

AuthenticatedEpochPeer authenticated_epoch_replica(
    ReplicaID replica_id,
    const PeerId &source_peer) noexcept;

const bytearray_t &hotstuff_epoch_processing_payload(
    const BufferedProposal &proposal) noexcept;

} // namespace hotstuff
#endif

namespace
{

using hotstuff::ActivationBlockReason;
using hotstuff::ActivationRecoveryNeed;
using hotstuff::ActivationStatus;
using hotstuff::ActivationTransition;
using hotstuff::ArmActivation;
using hotstuff::AuthenticatedEpochPeer;
using hotstuff::BufferedProposal;
using hotstuff::ConfigurationId;
using hotstuff::EpochAckTracker;
using hotstuff::EpochActivationIdentity;
using hotstuff::EpochDefinition;
using hotstuff::EpochDefinitionInput;
using hotstuff::EpochBufferedProposalIdentity;
using hotstuff::EpochConsensusBodyValidator;
using hotstuff::EpochConsensusEnvelope;
using hotstuff::EpochConsensusIngressResult;
using hotstuff::EpochConsensusPermission;
using hotstuff::EpochConsensusWireError;
using hotstuff::EpochConsensusWireKind;
using hotstuff::EpochIngressError;
using hotstuff::EpochPeerRole;
using hotstuff::EpochProtocolMode;
using hotstuff::EpochRotationResult;
using hotstuff::EpochCommitIngressResult;
using hotstuff::EpochFutureDrainResult;
using hotstuff::EpochFutureDrainStatus;
using hotstuff::EpochRuntimePlan;
using hotstuff::EpochRuntimeRotation;
using hotstuff::EpochRuntimeTransaction;
using hotstuff::EpochRuntimeUpdate;
using hotstuff::EpochTreeRuntimeInput;
using hotstuff::EpochStore;
using hotstuff::EpochTreeDefinition;
using hotstuff::EpochValidationContext;
using hotstuff::EpochWireError;
using hotstuff::EpochWireLimits;
using hotstuff::FutureProposalBuffer;
using hotstuff::FutureProposalClaim;
using hotstuff::HotStuffEpochRuntimeAdapter;
using hotstuff::HotStuffEpochConsensusBodyValidator;
using hotstuff::HotStuffEpochHandlerInstaller;
using hotstuff::HotStuffEpochHandlerRegistry;
using hotstuff::HotStuffEpochRuntimeTransaction;
using hotstuff::HotStuffRetryableFutureProposalStore;
using hotstuff::LeaderProgressConfig;
using hotstuff::LeaderProgressEffects;
using hotstuff::LeaderProgressEvent;
using hotstuff::LeaderProgressMonitor;
using hotstuff::LeaderProgressScheduler;
using hotstuff::LeaderViewId;
using hotstuff::ManagerEpochAckEndpoint;
using hotstuff::MsgArmActivation;
using hotstuff::MsgPropose;
using hotstuff::MsgRelay;
using hotstuff::MsgStageAck;
using hotstuff::MsgStageEpochDefinition;
using hotstuff::MsgVote;
using hotstuff::PreparedEpochRuntime;
using hotstuff::Proposal;
using hotstuff::ProposalAdmissionCoordinator;
using hotstuff::ProposalAdmissionEffects;
using hotstuff::ProposalContextLease;
using hotstuff::ProposalContextLifecycle;
using hotstuff::ProposalContextEvent;
using hotstuff::ProposalContextOrigin;
using hotstuff::ProposalDisposition;
using hotstuff::ProposalKey;
using hotstuff::ReplicaArmDisposition;
using hotstuff::ReplicaEpochActivation;
using hotstuff::ReplicaID;
using hotstuff::ReplicaStageDisposition;
using hotstuff::RetryableFutureProposalStore;
using hotstuff::StageAck;
using hotstuff::StageAckDisposition;
using hotstuff::StageEpochDefinition;
using hotstuff::Block;
using hotstuff::PeerId;
using hotstuff::block_t;
using hotstuff::bytearray_t;
using hotstuff::uint256_t;
using hotstuff::test::BlsTestCore;

constexpr std::uint64_t kActivationHeight = 1200;
constexpr std::size_t kFixedSlots = 16;

std::optional<std::uint64_t> reserved_nonzero_generation(
    std::uint32_t epoch_number,
    std::uint64_t rotation_ordinal) noexcept
{
    if (rotation_ordinal > std::numeric_limits<std::uint32_t>::max())
        return std::nullopt;
    const auto packed =
        (static_cast<std::uint64_t>(epoch_number) << 32) |
        rotation_ordinal;
    if (packed == std::numeric_limits<std::uint64_t>::max())
        return std::nullopt;
    return packed + 1;
}

uint256_t digest(const std::string &label)
{
    return hotstuff::DataStream(label).get_hash();
}

std::vector<ReplicaID> membership()
{
    return {0, 1, 2, 3, 4, 5, 6};
}

EpochTreeDefinition tree(
    std::uint32_t tree_id,
    std::vector<ReplicaID> members)
{
    return {tree_id, 2, 2, std::move(members)};
}

EpochDefinitionInput baseline_input()
{
    EpochDefinitionInput input;
    input.epoch_number = 0;
    input.membership_digest =
        hotstuff::canonical_membership_digest(membership());
    input.trees = {
        tree(0, {0, 1, 2, 3, 4, 5, 6}),
        tree(1, {1, 0, 2, 3, 4, 5, 6}),
    };
    input.generation_seed = 0xD110;
    input.policy_version = "d11-rem2-baseline-v1";
    input.evidence_snapshot_id = "d11-rem2-baseline";
    return input;
}

EpochDefinitionInput successor_input(const EpochDefinition &predecessor)
{
    auto input = baseline_input();
    input.epoch_number = predecessor.epoch_number() + 1;
    input.previous_epoch_digest = predecessor.epoch_digest();
    input.trees = {
        tree(0, {2, 0, 1, 3, 4, 5, 6}),
        tree(1, {3, 0, 1, 2, 4, 5, 6}),
    };
    input.activation_height = kActivationHeight;
    input.generation_seed = 0xD111;
    input.policy_version = "d11-rem2-adaptive-v1";
    input.evidence_snapshot_id = "d11-rem2-window-1";
    input.evidence_cutoff = 50;
    return input;
}

EpochValidationContext baseline_context()
{
    return {0, 0, {}};
}

EpochValidationContext successor_context()
{
    return {1000, 100, {}};
}

EpochWireLimits wire_limits()
{
    return {8192, 8, 16, 128};
}

ConfigurationId configuration(
    const EpochDefinition &epoch,
    std::uint32_t tree_id)
{
    return {epoch.epoch_number(), tree_id, epoch.epoch_digest()};
}

StageEpochDefinition stage_message(
    const EpochDefinition &predecessor,
    EpochDefinitionInput input)
{
    const auto successor_digest = hotstuff::compute_epoch_digest(input);
    input.epoch_digest = successor_digest;
    return {
        hotstuff::kEpochWireSchemaVersion,
        EpochProtocolMode::adaptive_v1,
        EpochActivationIdentity{
            predecessor.epoch_number(),
            predecessor.epoch_digest(),
            input.epoch_number,
            successor_digest,
            input.activation_height},
        std::move(input)};
}

StageAck acknowledgement(
    ReplicaID replica,
    const EpochActivationIdentity &activation)
{
    return {
        hotstuff::kEpochWireSchemaVersion,
        EpochProtocolMode::adaptive_v1,
        replica,
        activation};
}

ArmActivation arm_for(const EpochActivationIdentity &activation)
{
    return {
        hotstuff::kEpochWireSchemaVersion,
        EpochProtocolMode::adaptive_v1,
        activation};
}

ActivationStatus recovery_status(
    ReplicaID replica,
    const EpochActivationIdentity &activation)
{
    return {
        hotstuff::kEpochWireSchemaVersion,
        EpochProtocolMode::adaptive_v1,
        replica,
        activation,
        ActivationRecoveryNeed::exact_definition};
}

EpochConsensusEnvelope consensus_envelope(
    const ConfigurationId &configuration,
    std::uint64_t view_generation,
    const std::string &label,
    ReplicaID originator = 0,
    EpochConsensusWireKind kind = EpochConsensusWireKind::proposal,
    bytearray_t body = bytearray_t{0xD1, 0x12})
{
    return {
        configuration,
        view_generation,
        digest(label),
        originator,
        originator,
        std::move(body),
        hotstuff::kEpochConsensusWireSchemaVersion,
        EpochProtocolMode::adaptive_v1,
        kind};
}

block_t actual_proposal_block(
    BlsTestCore &core,
    std::uint8_t marker)
{
    auto certificate = core.create_quorum_cert(
        hotstuff::genesis_certification_key(
            core.get_genesis()->get_hash()));
    std::vector<block_t> parents{core.get_genesis()};
    std::vector<uint256_t> commands{digest(
        "d11-real-proposal-command-" + std::to_string(marker))};
    return block_t(new Block(
        parents,
        commands,
        std::move(certificate),
        bytearray_t{marker},
        1,
        core.get_genesis(),
        hotstuff::quorum_cert_bt{}));
}

bytearray_t actual_proposal_body(
    const ConfigurationId &configuration,
    ReplicaID proposer,
    const block_t &block)
{
    Proposal proposal(
        proposer,
        configuration.epoch_number,
        configuration.tree_id,
        configuration.epoch_digest,
        block,
        nullptr);
    hotstuff::DataStream serialized;
    serialized << proposal;
    return static_cast<bytearray_t>(serialized);
}

bytearray_t actual_proposal_body_with_metadata(
    const ConfigurationId &configuration,
    const uint256_t &claimed_block_hash,
    ReplicaID proposer,
    const block_t &block)
{
    hotstuff::DataStream serialized;
    hotstuff::ProposalMetadata{
        configuration, claimed_block_hash, proposer}
        .serialize(serialized);
    serialized << *block;
    return static_cast<bytearray_t>(serialized);
}

template<
    typename Validator,
    typename std::enable_if<
        std::is_constructible<
            Validator,
            hotstuff::HotStuffCore &,
            const EpochStore &>::value,
        int>::type = 0>
std::unique_ptr<Validator> exact_tree_body_validator(
    hotstuff::HotStuffCore &core,
    const EpochStore &epochs)
{
    return std::make_unique<Validator>(core, epochs);
}

template<
    typename Validator,
    typename std::enable_if<
        !std::is_constructible<
            Validator,
            hotstuff::HotStuffCore &,
            const EpochStore &>::value,
        int>::type = 0>
std::unique_ptr<Validator> exact_tree_body_validator(
    hotstuff::HotStuffCore &core,
    const EpochStore &)
{
    return std::make_unique<Validator>(core);
}

class FixedProposalEffects final : public ProposalAdmissionEffects
{
public:
    std::array<ProposalKey, kFixedSlots> relayed{};
    std::array<ProposalKey, kFixedSlots> processed{};
    std::array<bytearray_t, kFixedSlots> relayed_wire{};
    std::array<bytearray_t, kFixedSlots> processed_wire{};
    std::array<std::optional<std::uint64_t>, kFixedSlots>
        relayed_generation{};
    std::array<std::optional<std::uint64_t>, kFixedSlots>
        processed_generation{};
    std::size_t relay_count{0};
    std::size_t process_count{0};
    bool relay_during_processing{false};

    void relay_once(const BufferedProposal &proposal) override
    {
        REQUIRE(relay_count < relayed.size());
        relayed[relay_count] = proposal.metadata.key();
        relayed_wire[relay_count] = proposal.wire_payload;
        relayed_generation[relay_count] =
            hotstuff::buffered_proposal_view_generation(proposal);
        ++relay_count;
    }

    void process_active(const BufferedProposal &proposal) override
    {
        REQUIRE(process_count < processed.size());
        processed[process_count] = proposal.metadata.key();
        processed_wire[process_count] = proposal.wire_payload;
        processed_generation[process_count] =
            hotstuff::buffered_proposal_view_generation(proposal);
        ++process_count;
        if (relay_during_processing)
            relay_once(proposal);
    }

    void local_vote_authorized(const ProposalKey &) override {}
    void create_expected_vote_state(const ProposalKey &) override {}
    bool start_latency_deadline(const ProposalKey &) override
    {
        return true;
    }
    void start_aggregation_timer(const ProposalKey &) override {}
    void emit_timeout_report(const ProposalKey &) override {}
};

class RealWiringProposalEffects final : public ProposalAdmissionEffects
{
public:
    std::array<ProposalKey, kFixedSlots> relayed{};
    std::array<ProposalKey, kFixedSlots> processed{};
    std::array<bytearray_t, kFixedSlots> relayed_outer_wire{};
    std::array<bytearray_t, kFixedSlots> processed_inner_wire{};
    std::array<std::optional<PeerId>, kFixedSlots> relayed_source{};
    std::array<std::optional<PeerId>, kFixedSlots> processed_source{};
    std::size_t relay_count{0};
    std::size_t process_count{0};

    void relay_once(const BufferedProposal &proposal) override
    {
        REQUIRE(relay_count < relayed.size());
        relayed[relay_count] = proposal.metadata.key();
        relayed_outer_wire[relay_count] = proposal.wire_payload;
        relayed_source[relay_count] = proposal.source_peer;
        ++relay_count;
    }

    void process_active(const BufferedProposal &proposal) override
    {
        REQUIRE(process_count < processed.size());
        processed[process_count] = proposal.metadata.key();
        processed_inner_wire[process_count] =
            hotstuff::hotstuff_epoch_processing_payload(proposal);
        processed_source[process_count] = proposal.source_peer;
        ++process_count;
    }

    void local_vote_authorized(const ProposalKey &) override {}
    void create_expected_vote_state(const ProposalKey &) override {}
    bool start_latency_deadline(const ProposalKey &) override
    {
        return true;
    }
    void start_aggregation_timer(const ProposalKey &) override {}
    void emit_timeout_report(const ProposalKey &) override {}
};

class FixedConsensusBodyValidator final
    : public EpochConsensusBodyValidator
{
public:
    static constexpr std::uint8_t invalid_marker = 0xEE;

    std::size_t proposal_calls{0};
    std::size_t vote_calls{0};
    std::size_t relay_calls{0};
    std::optional<EpochConsensusEnvelope> last_proposal;
    std::optional<EpochConsensusEnvelope> last_vote;
    std::optional<EpochConsensusEnvelope> last_relay;

    bool validate_proposal(
        const EpochConsensusEnvelope &envelope,
        const AuthenticatedEpochPeer &) override
    {
        ++proposal_calls;
        last_proposal = envelope;
        return valid(envelope);
    }

    bool validate_vote(
        const EpochConsensusEnvelope &envelope,
        const AuthenticatedEpochPeer &) override
    {
        ++vote_calls;
        last_vote = envelope;
        return valid(envelope);
    }

    bool validate_relay(
        const EpochConsensusEnvelope &envelope,
        const AuthenticatedEpochPeer &) override
    {
        ++relay_calls;
        last_relay = envelope;
        return valid(envelope);
    }

private:
    static bool valid(const EpochConsensusEnvelope &envelope) noexcept
    {
        return envelope.body.empty() ||
               envelope.body.front() != invalid_marker;
    }
};

class FixedRetryableFutureStore final : public RetryableFutureProposalStore
{
public:
    static constexpr std::size_t tracked_claim_tokens = kFixedSlots * 8 + 1;

    std::array<std::optional<BufferedProposal>, kFixedSlots> entries;
    std::array<std::uint64_t, kFixedSlots> claim_tokens{};
    std::array<ProposalKey, kFixedSlots> completed{};
    std::array<std::uint64_t, tracked_claim_tokens> claimed_tokens{};
    std::array<std::size_t, tracked_claim_tokens> release_calls{};
    std::array<std::size_t, tracked_claim_tokens> acknowledge_calls{};
    std::size_t completed_count{0};
    std::size_t process_attempts{0};
    std::size_t complete_count{0};
    std::size_t claim_next_calls{0};
    std::size_t claimed_count{0};
    std::uint64_t next_claim_token{1};
    bool fail_next_process{false};
    std::optional<std::size_t> fail_process_attempt;
    std::optional<std::size_t> fail_claim_after_reserve_call;
    bool invariant_failed{false};
    std::optional<ConfigurationId> completed_configuration;

    bool insert(BufferedProposal proposal) override
    {
        for (const auto &entry : entries)
            if (entry && entry->metadata.key() == proposal.metadata.key())
                return false;
        for (auto &entry : entries)
        {
            if (entry)
                continue;
            entry.emplace(std::move(proposal));
            return true;
        }
        throw std::length_error("fixed future store exhausted");
    }

    std::optional<FutureProposalClaim> claim_next(
        const ConfigurationId &configuration) override
    {
        ++claim_next_calls;
        for (std::size_t index = 0; index < entries.size(); ++index)
        {
            if (!entries[index] || claim_tokens[index] != 0 ||
                entries[index]->metadata.configuration != configuration)
            {
                continue;
            }
            const auto token = next_claim_token++;
            if (claimed_count >= claimed_tokens.size() ||
                token >= release_calls.size())
            {
                invariant_failed = true;
                throw std::length_error("fixed claim history exhausted");
            }
            claimed_tokens[claimed_count++] = token;
            if (fail_claim_after_reserve_call == claim_next_calls)
            {
                fail_claim_after_reserve_call.reset();
                claim_tokens[index] = token;
                release(token);
                throw std::bad_alloc();
            }
            FutureProposalClaim claim{
                token, *entries[index]};
            claim_tokens[index] = token;
            return claim;
        }
        return std::nullopt;
    }

    void process_active(const FutureProposalClaim &claim) override
    {
        ++process_attempts;
        if (fail_next_process || fail_process_attempt == process_attempts)
        {
            fail_next_process = false;
            fail_process_attempt.reset();
            throw std::runtime_error("injected process_active failure");
        }
        if (completed_count >= completed.size())
            throw std::length_error("fixed completed store exhausted");
        completed[completed_count++] = claim.proposal.metadata.key();
    }

    void acknowledge(std::uint64_t token) noexcept override
    {
        if (token < acknowledge_calls.size())
            ++acknowledge_calls[token];
        else
            invariant_failed = true;
        for (std::size_t index = 0; index < entries.size(); ++index)
        {
            if (claim_tokens[index] != token)
                continue;
            claim_tokens[index] = 0;
            entries[index].reset();
            return;
        }
        invariant_failed = true;
    }

    void release(std::uint64_t token) noexcept override
    {
        if (token < release_calls.size())
            ++release_calls[token];
        else
            invariant_failed = true;
        for (auto &claim_token : claim_tokens)
        {
            if (claim_token != token)
                continue;
            claim_token = 0;
            return;
        }
        invariant_failed = true;
    }

    void complete(
        const ConfigurationId &configuration) noexcept override
    {
        for (const auto &entry : entries)
        {
            if (entry && entry->metadata.configuration == configuration)
            {
                invariant_failed = true;
                return;
            }
        }
        completed_configuration = configuration;
        ++complete_count;
    }

    std::size_t size() const noexcept override
    {
        std::size_t result = 0;
        for (const auto &entry : entries)
            result += entry.has_value() ? 1 : 0;
        return result;
    }

    std::size_t matching_size(
        const ConfigurationId &configuration) const noexcept
    {
        std::size_t result = 0;
        for (const auto &entry : entries)
            if (entry && entry->metadata.configuration == configuration)
                ++result;
        return result;
    }

    bool contains(const ProposalKey &key) const noexcept
    {
        for (const auto &entry : entries)
            if (entry && entry->metadata.key() == key)
                return true;
        return false;
    }

    std::size_t completed_occurrences(const ProposalKey &key) const noexcept
    {
        std::size_t result = 0;
        for (std::size_t index = 0; index < completed_count; ++index)
            result += completed[index] == key ? 1 : 0;
        return result;
    }
};

class FailOnceAfterConcreteClaimStore final
    : public RetryableFutureProposalStore
{
public:
    struct InjectedProcessFailure
    {};

    explicit FailOnceAfterConcreteClaimStore(
        HotStuffRetryableFutureProposalStore &delegate) noexcept
        : delegate_(delegate)
    {}

    bool insert(BufferedProposal proposal) override
    {
        return delegate_.insert(std::move(proposal));
    }

    std::optional<FutureProposalClaim> claim_next(
        const ConfigurationId &configuration) override
    {
        return delegate_.claim_next(configuration);
    }

    void process_active(const FutureProposalClaim &claim) override
    {
        if (fail_next_process_)
        {
            fail_next_process_ = false;
            d11_allocation_guard::reject_next();
            throw InjectedProcessFailure{};
        }
        delegate_.process_active(claim);
    }

    void acknowledge(std::uint64_t token) noexcept override
    {
        delegate_.acknowledge(token);
    }

    void release(std::uint64_t token) noexcept override
    {
        delegate_.release(token);
    }

    void complete(
        const ConfigurationId &configuration) noexcept override
    {
        delegate_.complete(configuration);
    }

    std::size_t size() const noexcept override
    {
        return delegate_.size();
    }

private:
    HotStuffRetryableFutureProposalStore &delegate_;
    bool fail_next_process_{true};
};

bool same_plan(
    const EpochRuntimePlan &left,
    const EpochRuntimePlan &right) noexcept
{
    if (left.canonical_stage != right.canonical_stage ||
        left.canonical_digest != right.canonical_digest ||
        left.trees.size() != right.trees.size())
    {
        return false;
    }
    for (std::size_t index = 0; index < left.trees.size(); ++index)
    {
        const auto &a = left.trees[index];
        const auto &b = right.trees[index];
        if (a.configuration != b.configuration ||
            a.tree.tree_id != b.tree.tree_id ||
            a.tree.fanout != b.tree.fanout ||
            a.tree.pipeline_stretch != b.tree.pipeline_stretch ||
            a.tree.members_breadth_first != b.tree.members_breadth_first ||
            a.leader != b.leader ||
            a.activation_generation != b.activation_generation)
        {
            return false;
        }
    }
    return true;
}

const EpochTreeRuntimeInput *find_runtime_tree(
    const EpochRuntimePlan &plan,
    const ConfigurationId &configuration) noexcept
{
    for (const auto &tree : plan.trees)
        if (tree.configuration == configuration)
            return &tree;
    return nullptr;
}

class FixedPreparedRuntime final : public EpochRuntimeTransaction
{
public:
    struct Slot
    {
        bool occupied{false};
        std::uint64_t token{0};
        std::shared_ptr<const EpochRuntimePlan> plan;
    };

    ProposalContextLifecycle *contexts{nullptr};
    bool prepare_allowed{true};
    bool invariant_failed{false};
    std::uint64_t next_token{1};
    std::size_t prepare_count{0};
    std::size_t discard_count{0};
    std::size_t commit_count{0};
    std::uint64_t last_discarded_token{0};
    std::shared_ptr<const EpochRuntimePlan> last_discarded_plan;
    std::array<Slot, kFixedSlots> slots{};
    std::array<std::uint64_t, kFixedSlots> committed_tokens{};
    std::array<std::uint64_t, kFixedSlots> committed_generations{};
    std::array<std::uint64_t, kFixedSlots> discarded_tokens{};
    std::size_t rotation_count{0};
    std::shared_ptr<const EpochRuntimePlan> active_plan;
    const EpochTreeRuntimeInput *active_tree{nullptr};
    std::optional<ConfigurationId> tree_configuration;
    std::optional<ConfigurationId> context_configuration;
    std::optional<ConfigurationId> admission_configuration;
    std::optional<LeaderViewId> leader_view;
    std::optional<ConfigurationId> leader_deadline_configuration;

    std::optional<PreparedEpochRuntime> prepare(
        const EpochRuntimePlan &plan) override
    {
        ++prepare_count;
        if (!prepare_allowed)
            return std::nullopt;
        auto owned_plan = std::make_shared<const EpochRuntimePlan>(plan);
        for (auto &slot : slots)
        {
            if (slot.occupied)
                continue;
            slot.occupied = true;
            slot.token = next_token++;
            slot.plan = std::move(owned_plan);
            return PreparedEpochRuntime{
                slot.token, slot.plan->canonical_digest};
        }
        throw std::length_error("fixed prepared-runtime slots exhausted");
    }

    void discard(PreparedEpochRuntime prepared) noexcept override
    {
        for (auto &slot : slots)
        {
            if (!slot.occupied || slot.token != prepared.token)
                continue;
            invariant_failed = invariant_failed || !slot.plan ||
                               slot.plan->canonical_digest !=
                                   prepared.canonical_plan_digest;
            last_discarded_token = prepared.token;
            last_discarded_plan = slot.plan;
            if (discard_count < discarded_tokens.size())
                discarded_tokens[discard_count] = prepared.token;
            else
                invariant_failed = true;
            slot.occupied = false;
            slot.plan.reset();
            ++discard_count;
            return;
        }
        invariant_failed = true;
    }

    void commit(
        PreparedEpochRuntime prepared,
        const EpochRuntimeUpdate &update) noexcept override
    {
        Slot *matched = nullptr;
        for (auto &slot : slots)
            if (slot.occupied && slot.token == prepared.token)
                matched = &slot;

        const auto *const tree = matched && matched->plan
                                     ? find_runtime_tree(
                                           *matched->plan,
                                           update.activation.configuration)
                                     : nullptr;
        if (matched == nullptr || !matched->plan || tree == nullptr ||
            matched->plan->canonical_digest !=
                prepared.canonical_plan_digest ||
            tree->activation_generation != update.activation.generation ||
            tree->leader != update.leader_view.leader_id ||
            tree->configuration != update.leader_view.configuration ||
            commit_count >= committed_tokens.size())
        {
            invariant_failed = true;
            return;
        }

        active_plan = matched->plan;
        active_tree = tree;
        tree_configuration = tree->configuration;
        if (contexts != nullptr)
        {
            contexts->activate_configuration(
                update.activation.configuration);
            context_configuration = contexts->active_configuration();
        }
        admission_configuration = update.activation.configuration;
        leader_view = update.leader_view;
        leader_deadline_configuration = update.leader_view.configuration;
        committed_tokens[commit_count] = prepared.token;
        committed_generations[commit_count] =
            update.leader_view.view_generation;
        ++commit_count;
        matched->occupied = false;
        matched->plan.reset();
    }

    void rotate(const EpochRuntimeRotation &update) noexcept override
    {
        const auto *const tree = active_plan
                                     ? find_runtime_tree(
                                           *active_plan,
                                           update.activation.configuration)
                                     : nullptr;
        const auto generation = hotstuff::checked_activation_generation(
            update.activation.configuration.epoch_number,
            update.activation.rotation_ordinal);
        if (tree == nullptr || !generation ||
            *generation != update.activation.generation ||
            update.leader_view.view_generation !=
                update.activation.generation ||
            update.leader_view.configuration != tree->configuration ||
            update.leader_view.leader_id != tree->leader)
        {
            invariant_failed = true;
            return;
        }
        active_tree = tree;
        tree_configuration = tree->configuration;
        context_configuration = tree->configuration;
        admission_configuration = tree->configuration;
        leader_view = update.leader_view;
        leader_deadline_configuration = tree->configuration;
        ++rotation_count;
    }

    std::size_t occupied_tokens() const noexcept
    {
        std::size_t occupied = 0;
        for (const auto &slot : slots)
            occupied += slot.occupied ? 1 : 0;
        return occupied;
    }

    const Slot *only_occupied_slot() const noexcept
    {
        const Slot *result = nullptr;
        for (const auto &slot : slots)
        {
            if (!slot.occupied)
                continue;
            if (result != nullptr)
                return nullptr;
            result = &slot;
        }
        return result;
    }

    bool discarded(std::uint64_t token) const noexcept
    {
        for (std::size_t index = 0; index < discard_count; ++index)
            if (discarded_tokens[index] == token)
                return true;
        return false;
    }
};

class FakeControlTransport final
{
public:
    void register_replica_handlers(HotStuffEpochRuntimeAdapter &adapter)
    {
        replica_ = &adapter;
        stage_registered_ = true;
        arm_registered_ = true;
        commit_registered_ = true;
        proposal_registered_ = true;
        vote_registered_ = true;
        relay_registered_ = true;
    }

    void register_manager_ack_handler(ManagerEpochAckEndpoint &manager)
    {
        manager_ = &manager;
        ack_registered_ = true;
    }

    bool all_replica_handlers_registered() const noexcept
    {
        return stage_registered_ && arm_registered_ &&
               commit_registered_ && proposal_registered_ &&
               vote_registered_ && relay_registered_;
    }

    hotstuff::ReplicaStageIngressResult dispatch_stage(
        MsgStageEpochDefinition &&message,
        const AuthenticatedEpochPeer &peer,
        const EpochValidationContext &context)
    {
        if (!stage_registered_ || replica_ == nullptr)
            throw std::logic_error("replica stage handler is not registered");
        return replica_->handle_stage(
            std::move(message), peer, context);
    }

    hotstuff::ReplicaArmIngressResult dispatch_arm(
        MsgArmActivation &&message,
        const AuthenticatedEpochPeer &peer)
    {
        if (!arm_registered_ || replica_ == nullptr)
            throw std::logic_error("replica arm handler is not registered");
        return replica_->handle_arm(std::move(message), peer);
    }

    hotstuff::ManagerAckIngressResult dispatch_ack(
        MsgStageAck &&message,
        const AuthenticatedEpochPeer &peer)
    {
        if (!ack_registered_ || manager_ == nullptr)
            throw std::logic_error("manager ack handler is not registered");
        return manager_->handle_ack(std::move(message), peer);
    }

    EpochCommitIngressResult dispatch_commit(
        std::uint64_t height,
        const uint256_t &digest)
    {
        if (!commit_registered_ || replica_ == nullptr)
            throw std::logic_error("replica commit handler is not registered");
        return replica_->on_predecessor_commit(height, digest);
    }

    EpochCommitIngressResult dispatch_replay()
    {
        if (!commit_registered_ || replica_ == nullptr)
            throw std::logic_error("replica replay handler is not registered");
        return replica_->replay_blocked_commit();
    }

    EpochRotationResult dispatch_rotation(std::uint32_t tree_id)
    {
        if (!commit_registered_ || replica_ == nullptr)
            throw std::logic_error("replica rotation handler is not registered");
        return replica_->rotate_to_tree(tree_id);
    }

    EpochConsensusIngressResult dispatch_proposal(
        MsgPropose &&message,
        const AuthenticatedEpochPeer &peer)
    {
        if (!proposal_registered_ || replica_ == nullptr)
            throw std::logic_error("replica proposal handler is not registered");
        return replica_->handle_proposal(std::move(message), peer);
    }

    EpochConsensusIngressResult dispatch_existing(
        MsgPropose &&message,
        const AuthenticatedEpochPeer &peer,
        const ProposalContextLease &lease)
    {
        if (!proposal_registered_ || replica_ == nullptr)
            throw std::logic_error("replica proposal handler is not registered");
        return replica_->handle_existing_proposal(
            std::move(message), peer, lease);
    }

    EpochConsensusIngressResult dispatch_vote(
        MsgVote &&message,
        const AuthenticatedEpochPeer &peer)
    {
        if (!vote_registered_ || replica_ == nullptr)
            throw std::logic_error("replica vote handler is not registered");
        return replica_->handle_vote(std::move(message), peer);
    }

    EpochConsensusIngressResult dispatch_relay(
        MsgRelay &&message,
        const AuthenticatedEpochPeer &peer)
    {
        if (!relay_registered_ || replica_ == nullptr)
            throw std::logic_error("replica relay handler is not registered");
        return replica_->handle_relay(std::move(message), peer);
    }

private:
    HotStuffEpochRuntimeAdapter *replica_{nullptr};
    ManagerEpochAckEndpoint *manager_{nullptr};
    bool stage_registered_{false};
    bool arm_registered_{false};
    bool ack_registered_{false};
    bool commit_registered_{false};
    bool proposal_registered_{false};
    bool vote_registered_{false};
    bool relay_registered_{false};
};

class SocketFreeEpochHandlerRegistry final
    : public HotStuffEpochHandlerRegistry
{
public:
    std::optional<hotstuff::opcode_t> stage_opcode;
    std::optional<hotstuff::opcode_t> arm_opcode;
    std::optional<hotstuff::opcode_t> proposal_opcode;
    std::optional<hotstuff::opcode_t> vote_opcode;
    std::optional<hotstuff::opcode_t> relay_opcode;
    std::size_t stage_registration_count{0};
    std::size_t arm_registration_count{0};
    std::size_t proposal_registration_count{0};
    std::size_t vote_registration_count{0};
    std::size_t relay_registration_count{0};
    std::size_t stage_dispatch_count{0};
    std::size_t arm_dispatch_count{0};
    std::size_t proposal_dispatch_count{0};
    std::size_t vote_dispatch_count{0};
    std::size_t relay_dispatch_count{0};
    std::size_t vote_continuation_count{0};
    std::size_t relay_continuation_count{0};
    std::optional<bytearray_t> vote_continuation_body;
    std::optional<bytearray_t> relay_continuation_body;

    void register_stage_handler(
        hotstuff::opcode_t opcode,
        StageHandler handler) override
    {
        stage_opcode = opcode;
        stage_handler_ = std::move(handler);
        ++stage_registration_count;
    }

    void register_arm_handler(
        hotstuff::opcode_t opcode,
        ArmHandler handler) override
    {
        arm_opcode = opcode;
        arm_handler_ = std::move(handler);
        ++arm_registration_count;
    }

    void register_proposal_handler(
        hotstuff::opcode_t opcode,
        ProposalHandler handler) override
    {
        proposal_opcode = opcode;
        proposal_handler_ = std::move(handler);
        ++proposal_registration_count;
    }

    void register_vote_handler(
        hotstuff::opcode_t opcode,
        VoteHandler handler) override
    {
        vote_opcode = opcode;
        vote_handler_ = std::move(handler);
        ++vote_registration_count;
    }

    void register_relay_handler(
        hotstuff::opcode_t opcode,
        RelayHandler handler) override
    {
        relay_opcode = opcode;
        relay_handler_ = std::move(handler);
        ++relay_registration_count;
    }

    hotstuff::ReplicaStageIngressResult dispatch_stage(
        MsgStageEpochDefinition &&message,
        const AuthenticatedEpochPeer &peer,
        const EpochValidationContext &context)
    {
        if (!stage_handler_)
            throw std::logic_error("stage handler is not installed");
        ++stage_dispatch_count;
        return stage_handler_(std::move(message), peer, context);
    }

    hotstuff::ReplicaArmIngressResult dispatch_arm(
        MsgArmActivation &&message,
        const AuthenticatedEpochPeer &peer)
    {
        if (!arm_handler_)
            throw std::logic_error("arm handler is not installed");
        ++arm_dispatch_count;
        return arm_handler_(std::move(message), peer);
    }

    EpochConsensusIngressResult dispatch_proposal(
        MsgPropose &&message,
        const AuthenticatedEpochPeer &peer)
    {
        if (!proposal_handler_)
            throw std::logic_error("proposal handler is not installed");
        ++proposal_dispatch_count;
        return proposal_handler_(std::move(message), peer);
    }

    EpochConsensusIngressResult dispatch_vote_to_existing_continuation(
        MsgVote &&message,
        const AuthenticatedEpochPeer &peer)
    {
        if (!vote_handler_)
            throw std::logic_error("vote handler is not installed");
        ++vote_dispatch_count;
        auto result = vote_handler_(std::move(message), peer);
        if (result.permission ==
                EpochConsensusPermission::accept_contribution &&
            result.decoded_envelope.has_value())
        {
            vote_continuation_body = result.decoded_envelope->body;
            ++vote_continuation_count;
        }
        return result;
    }

    EpochConsensusIngressResult dispatch_relay_to_existing_continuation(
        MsgRelay &&message,
        const AuthenticatedEpochPeer &peer)
    {
        if (!relay_handler_)
            throw std::logic_error("relay handler is not installed");
        ++relay_dispatch_count;
        auto result = relay_handler_(std::move(message), peer);
        if (result.permission ==
                EpochConsensusPermission::accept_contribution &&
            result.decoded_envelope.has_value())
        {
            relay_continuation_body = result.decoded_envelope->body;
            ++relay_continuation_count;
        }
        return result;
    }

private:
    StageHandler stage_handler_;
    ArmHandler arm_handler_;
    ProposalHandler proposal_handler_;
    VoteHandler vote_handler_;
    RelayHandler relay_handler_;
};

struct RuntimeHarness
{
    EpochStore store{membership()};
    const EpochDefinition *epoch0{nullptr};
    std::unique_ptr<ReplicaEpochActivation> activation;
    FutureProposalBuffer future;
    ProposalContextLifecycle contexts;
    FixedProposalEffects proposal_effects;
    FixedConsensusBodyValidator body_validator;
    FixedRetryableFutureStore retryable_future;
    std::unique_ptr<ProposalAdmissionCoordinator> admission;
    FixedPreparedRuntime transaction;
    std::unique_ptr<HotStuffEpochRuntimeAdapter> adapter;
    FakeControlTransport transport;

    explicit RuntimeHarness(
        ReplicaID local_replica = 0,
        EpochWireLimits limits = wire_limits(),
        EpochProtocolMode expected_mode = EpochProtocolMode::adaptive_v1)
    {
        epoch0 = &store.stage(baseline_input(), baseline_context());
        activation = std::make_unique<ReplicaEpochActivation>(
            store, *epoch0, local_replica, 0);
        const auto initial = configuration(*epoch0, 0);
        contexts.activate_configuration(initial);
        const auto relay_policy =
            expected_mode == EpochProtocolMode::adaptive_v2
                ? hotstuff::ProposalRelayPolicy::
                      adaptive_v2_deferred_until_arm_attempt
                : hotstuff::ProposalRelayPolicy::eager_before_processing;
        proposal_effects.relay_during_processing =
            expected_mode == EpochProtocolMode::adaptive_v2;
        admission = std::make_unique<ProposalAdmissionCoordinator>(
            store, initial, future, proposal_effects, relay_policy);
        transaction.contexts = &contexts;
        transaction.tree_configuration = initial;
        transaction.context_configuration = initial;
        transaction.admission_configuration = initial;
        const auto initial_effect = activation->active_effect();
        transaction.leader_view = LeaderViewId{
            initial,
            initial_effect.generation,
            epoch0->trees().front().members_breadth_first.front()};
        transaction.leader_deadline_configuration = initial;
        install_adapter(limits, expected_mode);
    }

    void install_adapter(
        EpochWireLimits limits = wire_limits(),
        EpochProtocolMode expected_mode = EpochProtocolMode::adaptive_v1)
    {
        adapter = std::make_unique<HotStuffEpochRuntimeAdapter>(
            *activation,
            contexts,
            *admission,
            retryable_future,
            body_validator,
            expected_mode,
            limits,
            transaction);
        transport.register_replica_handlers(*adapter);
    }

    void restart_adapter(EpochWireLimits limits = wire_limits())
    {
        adapter.reset();
        install_adapter(limits);
    }

    StageEpochDefinition successor() const
    {
        return stage_message(*epoch0, successor_input(*epoch0));
    }

    hotstuff::ReplicaStageIngressResult stage(
        const StageEpochDefinition &message)
    {
        return transport.dispatch_stage(
            MsgStageEpochDefinition(message, wire_limits()),
            AuthenticatedEpochPeer::manager(),
            successor_context());
    }

    hotstuff::ReplicaArmIngressResult arm(
        const ArmActivation &message)
    {
        return transport.dispatch_arm(
            MsgArmActivation(message, wire_limits()),
            AuthenticatedEpochPeer::manager());
    }
};

BufferedProposal proposal(
    const EpochDefinition &epoch,
    std::uint32_t tree_id,
    const std::string &label)
{
    const EpochTreeDefinition *selected = nullptr;
    for (const auto &candidate : epoch.trees())
        if (candidate.tree_id == tree_id)
            selected = &candidate;
    REQUIRE(selected != nullptr);
    return {
        {configuration(epoch, tree_id),
         digest(label),
         selected->members_breadth_first.front()},
        bytearray_t{0xD1, 0x12},
        {}};
}

MsgStageEpochDefinition oversized_stage(std::size_t size)
{
    return MsgStageEpochDefinition(
        hotstuff::DataStream(bytearray_t(size, 0xA1)));
}

MsgArmActivation oversized_arm(std::size_t size)
{
    return MsgArmActivation(
        hotstuff::DataStream(bytearray_t(size, 0xA2)));
}

MsgStageAck oversized_ack(std::size_t size)
{
    return MsgStageAck(
        hotstuff::DataStream(bytearray_t(size, 0xA3)));
}

MsgPropose proposal_message(const EpochConsensusEnvelope &envelope)
{
    return MsgPropose(hotstuff::DataStream(
        hotstuff::encode_epoch_consensus_envelope(
            envelope, wire_limits())));
}

MsgVote vote_message(const EpochConsensusEnvelope &envelope)
{
    return MsgVote(hotstuff::DataStream(
        hotstuff::encode_epoch_consensus_envelope(
            envelope, wire_limits())));
}

MsgRelay relay_message(const EpochConsensusEnvelope &envelope)
{
    return MsgRelay(hotstuff::DataStream(
        hotstuff::encode_epoch_consensus_envelope(
            envelope, wire_limits())));
}

class FixedLeaderScheduler final : public LeaderProgressScheduler
{
public:
    std::array<Callback, kFixedSlots> callbacks{};
    std::array<bool, kFixedSlots> cancelled{};
    std::size_t scheduled{0};

    Cancellation schedule_after(
        Duration,
        Callback callback) override
    {
        if (scheduled >= callbacks.size())
            throw std::length_error("fixed leader scheduler exhausted");
        const auto slot = scheduled++;
        callbacks[slot] = std::move(callback);
        return [this, slot]() { cancelled[slot] = true; };
    }

    void fire_even_if_cancelled(std::size_t slot)
    {
        REQUIRE(slot < scheduled);
        REQUIRE(static_cast<bool>(callbacks[slot]));
        callbacks[slot]();
    }
};

} // namespace

TEST_CASE("REM-D11 exposes prepared adapters and real consensus handlers",
          "[rem-d11][epoch-runtime][contract][intentional-red]")
{
    CHECK(KAURI_HAS_D11_EPOCH_RUNTIME_API == 1);
    const auto manager = AuthenticatedEpochPeer::manager();
    CHECK(manager.role == EpochPeerRole::manager);
    CHECK_FALSE(manager.replica_id.has_value());
    const auto replica = AuthenticatedEpochPeer::replica(4);
    CHECK(replica.role == EpochPeerRole::replica);
    CHECK(replica.replica_id == 4);

    static_assert(
        std::is_abstract<EpochRuntimeTransaction>::value,
        "runtime preparation and nofail commit are injected boundaries");
    static_assert(
        noexcept(std::declval<EpochRuntimeTransaction &>().discard(
            std::declval<PreparedEpochRuntime>())),
        "rejected stage preparations cannot leak tokens");
    static_assert(
        noexcept(std::declval<EpochRuntimeTransaction &>().commit(
            std::declval<PreparedEpochRuntime>(),
            std::declval<const EpochRuntimeUpdate &>())),
        "prepared runtime commit is one nofail swap");
    static_assert(
        noexcept(std::declval<HotStuffEpochRuntimeAdapter &>()
                     .on_predecessor_commit(
                         std::declval<std::uint64_t>(),
                         std::declval<const uint256_t &>())),
        "the public exact-H adapter boundary must remain nofail");
    static_assert(
        noexcept(std::declval<HotStuffEpochRuntimeAdapter &>()
                     .rotate_to_tree(std::declval<std::uint32_t>())),
        "tree rotation is a preprepared nofail pointer swap");
    static_assert(
        !std::is_copy_constructible<HotStuffEpochRuntimeAdapter>::value,
        "one adapter owns one single-use prepared-token boundary");

    const ConfigurationId exact{7, 3, digest("identity-config")};
    const auto envelope = consensus_envelope(
        exact, 0x700000002ULL, "identity-block", 4);
    CHECK(envelope.configuration == exact);
    CHECK(envelope.view_generation == 0x700000002ULL);
    CHECK(envelope.block_hash == digest("identity-block"));
    CHECK(envelope.originator == 4);
    CHECK(envelope.proposer == 4);
    const ProposalKey expected_key{exact, envelope.block_hash};
    CHECK(envelope.key() == expected_key);
    CHECK(MsgPropose::opcode == 0x0);
    CHECK(MsgVote::opcode == 0x1);
    CHECK(MsgRelay::opcode == 0x4);
}

TEST_CASE("activation generations reserve zero and pin packed boundaries",
          "[rem-d11][epoch-runtime][generation][overflow]"
          "[intentional-red]")
{
    const auto zero_zero = reserved_nonzero_generation(0, 0);
    REQUIRE(zero_zero.has_value());
    CHECK(*zero_zero == 1);
    CHECK(hotstuff::checked_activation_generation(0, 0) == zero_zero);

    const auto epoch_one = reserved_nonzero_generation(1, 0);
    REQUIRE(epoch_one.has_value());
    CHECK(*epoch_one == (1ULL << 32) + 1);
    CHECK(hotstuff::checked_activation_generation(1, 0) == epoch_one);

    const auto maximum_ordinal =
        std::numeric_limits<std::uint32_t>::max();
    const auto last_nonoverflow = reserved_nonzero_generation(
        std::numeric_limits<std::uint32_t>::max() - 1,
        maximum_ordinal);
    REQUIRE(last_nonoverflow.has_value());
    CHECK(*last_nonoverflow == 0xffffffff00000000ULL);
    CHECK(hotstuff::checked_activation_generation(
              std::numeric_limits<std::uint32_t>::max() - 1,
              maximum_ordinal) == last_nonoverflow);

    const auto maximum_valid = reserved_nonzero_generation(
        std::numeric_limits<std::uint32_t>::max(),
        static_cast<std::uint64_t>(maximum_ordinal) - 1);
    REQUIRE(maximum_valid.has_value());
    CHECK(*maximum_valid == std::numeric_limits<std::uint64_t>::max());
    CHECK(hotstuff::checked_activation_generation(
              std::numeric_limits<std::uint32_t>::max(),
              static_cast<std::uint64_t>(maximum_ordinal) - 1) ==
          maximum_valid);

    CHECK_FALSE(reserved_nonzero_generation(
                    1,
                    static_cast<std::uint64_t>(maximum_ordinal) + 1)
                    .has_value());
    CHECK_FALSE(hotstuff::checked_activation_generation(
                    1,
                    static_cast<std::uint64_t>(maximum_ordinal) + 1)
                    .has_value());
    CHECK_FALSE(reserved_nonzero_generation(
                    std::numeric_limits<std::uint32_t>::max(),
                    maximum_ordinal)
                    .has_value());
    CHECK_FALSE(hotstuff::checked_activation_generation(
                    std::numeric_limits<std::uint32_t>::max(),
                    maximum_ordinal)
                    .has_value());
}

TEST_CASE("real consensus opcodes carry one bounded canonical envelope",
          "[rem-d11][epoch-runtime][consensus][wire][generation]"
          "[intentional-red]")
{
    const ConfigurationId configuration_id{
        7, 3, digest("proposal-wire-configuration")};
    const auto current = consensus_envelope(
        configuration_id,
        0x700000002ULL,
        "proposal-wire-block",
        4,
        EpochConsensusWireKind::proposal,
        bytearray_t{0xA1, 0xB2, 0xC3});
    const auto encoded = hotstuff::encode_epoch_consensus_envelope(
        current, wire_limits());

    hotstuff::DataStream expected;
    expected << hotstuff::htole(current.wire_schema_version)
             << static_cast<std::uint8_t>(current.protocol_mode)
             << static_cast<std::uint8_t>(current.kind)
             << hotstuff::htole(current.configuration.epoch_number)
             << hotstuff::htole(current.configuration.tree_id)
             << current.configuration.epoch_digest
             << hotstuff::htole(current.view_generation)
             << current.block_hash
             << hotstuff::htole(current.originator)
             << hotstuff::htole(current.proposer)
             << hotstuff::htole(
                    static_cast<std::uint32_t>(current.body.size()))
             << current.body;
    CHECK(encoded == static_cast<bytearray_t>(expected));

    const auto decoded = hotstuff::decode_epoch_consensus_envelope(
        encoded,
        EpochConsensusWireKind::proposal,
        EpochProtocolMode::adaptive_v1,
        wire_limits());
    REQUIRE(decoded);
    REQUIRE(decoded.value.has_value());
    CHECK(decoded.value->configuration == current.configuration);
    CHECK(decoded.value->view_generation == current.view_generation);
    CHECK(decoded.value->block_hash == current.block_hash);
    CHECK(decoded.value->originator == current.originator);
    CHECK(decoded.value->proposer == current.proposer);
    CHECK(decoded.value->body == current.body);
    CHECK(decoded.value->wire_schema_version ==
          hotstuff::kEpochConsensusWireSchemaVersion);
    CHECK(decoded.value->protocol_mode == EpochProtocolMode::adaptive_v1);
    CHECK(decoded.value->kind == EpochConsensusWireKind::proposal);
    CHECK(hotstuff::encode_epoch_consensus_envelope(
              *decoded.value, wire_limits()) == encoded);

    auto bounded = wire_limits();
    REQUIRE(encoded.size() > 1);
    bounded.maximum_payload_bytes = encoded.size() - 1;
    CHECK(hotstuff::decode_epoch_consensus_envelope(
              encoded,
              EpochConsensusWireKind::proposal,
              EpochProtocolMode::adaptive_v1,
              bounded)
              .error == EpochConsensusWireError::payload_too_large);

    auto trailing = encoded;
    trailing.push_back(0xFF);
    CHECK(hotstuff::decode_epoch_consensus_envelope(
              trailing,
              EpochConsensusWireKind::proposal,
              EpochProtocolMode::adaptive_v1,
              wire_limits())
              .error == EpochConsensusWireError::trailing_bytes);

    auto mixed = current;
    mixed.protocol_mode = EpochProtocolMode::legacy_static;
    CHECK_THROWS_AS(
        hotstuff::encode_epoch_consensus_envelope(mixed, wire_limits()),
        std::invalid_argument);
    CHECK(hotstuff::decode_epoch_consensus_envelope(
              encoded,
              EpochConsensusWireKind::vote,
              EpochProtocolMode::adaptive_v1,
              wire_limits())
              .error == EpochConsensusWireError::unexpected_kind);
    CHECK(hotstuff::decode_epoch_consensus_envelope(
              encoded,
              EpochConsensusWireKind::proposal,
              EpochProtocolMode::adaptive_v1,
              EpochWireLimits{0, 8, 16, 128})
              .error == EpochConsensusWireError::invalid_limits);

    RuntimeHarness harness;
    auto truncated = encoded;
    truncated.pop_back();
    const auto truncated_result = harness.transport.dispatch_proposal(
        MsgPropose(hotstuff::DataStream(truncated)),
        AuthenticatedEpochPeer::replica(3));
    CHECK(truncated_result.error == EpochIngressError::wire_rejected);
    CHECK(truncated_result.wire_error == EpochConsensusWireError::truncated);
    CHECK_FALSE(truncated_result.decoded_envelope.has_value());

    auto zero = current;
    zero.view_generation = 0;
    const auto zero_encoded = hotstuff::encode_epoch_consensus_envelope(
        zero, wire_limits());
    CHECK(hotstuff::decode_epoch_consensus_envelope(
              zero_encoded,
              EpochConsensusWireKind::proposal,
              EpochProtocolMode::adaptive_v1,
              wire_limits())
              .error == EpochConsensusWireError::invalid_generation);
    const auto zero_result = harness.transport.dispatch_proposal(
        MsgPropose(hotstuff::DataStream(zero_encoded)),
        AuthenticatedEpochPeer::replica(3));
    CHECK(zero_result.error == EpochIngressError::wire_rejected);
    CHECK(zero_result.wire_error ==
          EpochConsensusWireError::invalid_generation);
    CHECK(zero_result.permission ==
          EpochConsensusPermission::rejected_identity);
}

TEST_CASE("adaptive consensus rejects a legacy mode even when configured",
          "[rem-d11][epoch-runtime][consensus][mode][gate-order]"
          "[intentional-red]")
{
    RuntimeHarness harness(
        0, wire_limits(), EpochProtocolMode::legacy_static);
    const auto active = harness.activation->active_effect();
    auto legacy = consensus_envelope(
        active.configuration,
        active.generation,
        "configured-legacy-proposal",
        harness.epoch0->trees().front().members_breadth_first.front(),
        EpochConsensusWireKind::proposal,
        bytearray_t{FixedConsensusBodyValidator::invalid_marker});
    legacy.protocol_mode = EpochProtocolMode::legacy_static;

    hotstuff::DataStream forged;
    forged << hotstuff::htole(legacy.wire_schema_version)
           << static_cast<std::uint8_t>(legacy.protocol_mode)
           << static_cast<std::uint8_t>(legacy.kind)
           << hotstuff::htole(legacy.configuration.epoch_number)
           << hotstuff::htole(legacy.configuration.tree_id)
           << legacy.configuration.epoch_digest
           << hotstuff::htole(legacy.view_generation)
           << legacy.block_hash
           << hotstuff::htole(legacy.originator)
           << hotstuff::htole(legacy.proposer)
           << hotstuff::htole(
                  static_cast<std::uint32_t>(legacy.body.size()))
           << legacy.body;
    const auto forged_bytes = static_cast<bytearray_t>(forged);

    const auto rejected = harness.transport.dispatch_proposal(
        MsgPropose(hotstuff::DataStream(forged_bytes)),
        AuthenticatedEpochPeer::replica(4));
    CHECK(rejected.error == EpochIngressError::wire_rejected);
    CHECK(rejected.wire_error == EpochConsensusWireError::mode_mismatch);
    CHECK(rejected.permission ==
          EpochConsensusPermission::rejected_identity);
    CHECK_FALSE(rejected.decoded_envelope.has_value());
    CHECK(harness.body_validator.proposal_calls == 0);
    CHECK(harness.proposal_effects.relay_count == 0);
    CHECK(harness.proposal_effects.process_count == 0);
    CHECK(harness.future.size() == 0);
    CHECK(harness.retryable_future.size() == 0);
}

TEST_CASE("typed transport prepares before staging and discards rejection",
          "[rem-d11][epoch-runtime][transport][prepare][atomic]"
          "[intentional-red]")
{
    RuntimeHarness harness;
    REQUIRE(harness.transport.all_replica_handlers_registered());
    const auto stage = harness.successor();
    const auto baseline = configuration(*harness.epoch0, 0);

    const auto unauthorized = harness.transport.dispatch_stage(
        MsgStageEpochDefinition(stage, wire_limits()),
        AuthenticatedEpochPeer::replica(0),
        successor_context());
    CHECK(unauthorized.error == EpochIngressError::unauthorized_peer);
    CHECK(harness.transaction.prepare_count == 0);
    CHECK(harness.store.size() == 1);

    harness.transaction.prepare_allowed = false;
    const auto failed = harness.stage(stage);
    CHECK(failed.error == EpochIngressError::runtime_preparation_failed);
    CHECK_FALSE(failed.acknowledgement.has_value());
    CHECK(harness.transaction.prepare_count == 1);
    CHECK(harness.transaction.occupied_tokens() == 0);
    CHECK(harness.store.size() == 1);
    CHECK(harness.activation->active_effect().configuration == baseline);
    CHECK(harness.contexts.active_configuration() == baseline);
    CHECK(harness.admission->active_configuration() == baseline);
    CHECK(harness.transaction.leader_view->configuration == baseline);

    harness.transaction.prepare_allowed = true;
    const auto accepted = harness.stage(stage);
    CHECK(accepted.error == EpochIngressError::none);
    REQUIRE(accepted.disposition.has_value());
    CHECK(*accepted.disposition == ReplicaStageDisposition::staged);
    REQUIRE(accepted.acknowledgement.has_value());
    CHECK(accepted.acknowledgement->activation == stage.activation);
    CHECK(harness.store.size() == 2);
    CHECK(harness.transaction.occupied_tokens() == 1);
    CHECK(harness.activation->active_effect().configuration == baseline);
    CHECK(harness.contexts.active_configuration() == baseline);
    CHECK(harness.admission->active_configuration() == baseline);
    const auto *const prepared = harness.transaction.only_occupied_slot();
    REQUIRE(prepared != nullptr);
    REQUIRE(prepared->plan != nullptr);
    const auto accepted_token = prepared->token;
    const auto canonical = hotstuff::encode_epoch_wire(stage, wire_limits());
    CHECK(prepared->plan->canonical_stage == canonical);
    CHECK(prepared->plan->canonical_digest ==
          hotstuff::DataStream(canonical).get_hash());
    CHECK(prepared->plan->stage.activation == stage.activation);
    REQUIRE(prepared->plan->trees.size() == stage.definition.trees.size());
    const auto expected_generation = hotstuff::checked_activation_generation(
        stage.activation.successor_epoch_number, 0);
    REQUIRE(expected_generation.has_value());
    for (std::size_t index = 0; index < prepared->plan->trees.size(); ++index)
    {
        const auto &runtime_tree = prepared->plan->trees[index];
        const auto &definition_tree = stage.definition.trees[index];
        CHECK(runtime_tree.configuration == ConfigurationId{
                  stage.activation.successor_epoch_number,
                  definition_tree.tree_id,
                  stage.activation.successor_epoch_digest});
        CHECK(runtime_tree.tree.tree_id == definition_tree.tree_id);
        CHECK(runtime_tree.tree.fanout == definition_tree.fanout);
        CHECK(runtime_tree.tree.pipeline_stretch ==
              definition_tree.pipeline_stretch);
        CHECK(runtime_tree.tree.members_breadth_first ==
              definition_tree.members_breadth_first);
        REQUIRE_FALSE(runtime_tree.tree.members_breadth_first.empty());
        CHECK(runtime_tree.leader ==
              runtime_tree.tree.members_breadth_first.front());
        CHECK(runtime_tree.activation_generation == *expected_generation);
    }

    const auto exact_duplicate = harness.stage(stage);
    CHECK(exact_duplicate.error == EpochIngressError::none);
    REQUIRE(exact_duplicate.disposition.has_value());
    CHECK(*exact_duplicate.disposition == ReplicaStageDisposition::duplicate);
    CHECK(harness.transaction.prepare_count == 3);
    CHECK(harness.transaction.discard_count == 1);
    REQUIRE(harness.transaction.only_occupied_slot() != nullptr);
    CHECK(harness.transaction.only_occupied_slot()->token == accepted_token);
    CHECK(harness.transaction.last_discarded_token != accepted_token);
    CHECK(harness.transaction.discarded(
        harness.transaction.last_discarded_token));

    auto divergent_input = successor_input(*harness.epoch0);
    divergent_input.policy_version = "d11-rem2-divergent";
    const auto divergent = stage_message(
        *harness.epoch0, std::move(divergent_input));
    const auto rejected = harness.stage(divergent);
    CHECK(rejected.error == EpochIngressError::state_rejected);
    REQUIRE(rejected.disposition.has_value());
    CHECK(*rejected.disposition ==
          ReplicaStageDisposition::divergent_definition);
    CHECK(harness.transaction.prepare_count == 4);
    CHECK(harness.transaction.discard_count == 2);
    CHECK(harness.transaction.occupied_tokens() == 1);
    CHECK(harness.transaction.last_discarded_token != 0);
    REQUIRE(harness.transaction.last_discarded_plan != nullptr);
    CHECK(harness.transaction.last_discarded_plan->stage.activation ==
          divergent.activation);
    CHECK(harness.transaction.last_discarded_plan->canonical_stage ==
          hotstuff::encode_epoch_wire(divergent, wire_limits()));
    CHECK_FALSE(harness.transaction.invariant_failed);

    auto mixed_wire = hotstuff::encode_epoch_wire(stage, wire_limits());
    REQUIRE(mixed_wire.size() > 4);
    mixed_wire[4] =
        static_cast<std::uint8_t>(EpochProtocolMode::legacy_static);
    const auto mixed_result = harness.transport.dispatch_stage(
        MsgStageEpochDefinition(hotstuff::DataStream(std::move(mixed_wire))),
        AuthenticatedEpochPeer::manager(),
        successor_context());
    CHECK(mixed_result.error == EpochIngressError::wire_rejected);
    CHECK(mixed_result.wire_error == EpochWireError::mode_mismatch);
    CHECK(harness.transaction.prepare_count == 4);

    REQUIRE(harness.arm(arm_for(stage.activation)).disposition ==
            ReplicaArmDisposition::armed);
    const auto committed = harness.transport.dispatch_commit(
        kActivationHeight, harness.epoch0->epoch_digest());
    REQUIRE(committed.transition == ActivationTransition::activated);
    REQUIRE(committed.update.has_value());
    REQUIRE(harness.transaction.commit_count == 1);
    CHECK(harness.transaction.committed_tokens[0] == accepted_token);
    CHECK(harness.transaction.committed_tokens[0] !=
          harness.transaction.last_discarded_token);
    CHECK(harness.transaction.occupied_tokens() == 0);
}

TEST_CASE("MsgPropose gate order is auth size version generation body admission",
          "[rem-d11][epoch-runtime][msg-propose][gate-order]"
          "[intentional-red]")
{
    RuntimeHarness harness;
    const auto active = harness.activation->active_effect();
    REQUIRE(active.generation != 0);
    const auto valid = consensus_envelope(
        active.configuration,
        active.generation,
        "gate-order-valid",
        harness.epoch0->trees().front().members_breadth_first.front());

    const auto no_effects = [&]() {
        CHECK(harness.body_validator.proposal_calls == 0);
        CHECK(harness.proposal_effects.relay_count == 0);
        CHECK(harness.proposal_effects.process_count == 0);
        CHECK(harness.future.size() == 0);
    };

    const bytearray_t oversized(
        wire_limits().maximum_payload_bytes + 1, 0xA7);
    const auto unauthorized = harness.transport.dispatch_proposal(
        MsgPropose(hotstuff::DataStream(oversized)),
        AuthenticatedEpochPeer::manager());
    CHECK(unauthorized.error == EpochIngressError::unauthorized_peer);
    no_effects();

    const auto too_large = harness.transport.dispatch_proposal(
        MsgPropose(hotstuff::DataStream(oversized)),
        AuthenticatedEpochPeer::replica(4));
    CHECK(too_large.error == EpochIngressError::wire_rejected);
    CHECK(too_large.wire_error ==
          EpochConsensusWireError::payload_too_large);
    no_effects();

    auto unsupported = valid;
    ++unsupported.wire_schema_version;
    ++unsupported.view_generation;
    unsupported.body = {FixedConsensusBodyValidator::invalid_marker};
    const auto unsupported_result = harness.transport.dispatch_proposal(
        proposal_message(unsupported),
        AuthenticatedEpochPeer::replica(4));
    CHECK(unsupported_result.error == EpochIngressError::wire_rejected);
    CHECK(unsupported_result.wire_error ==
          EpochConsensusWireError::unsupported_schema);
    no_effects();

    auto stale = valid;
    ++stale.view_generation;
    stale.body = {FixedConsensusBodyValidator::invalid_marker};
    const auto stale_result = harness.transport.dispatch_proposal(
        proposal_message(stale),
        AuthenticatedEpochPeer::replica(4));
    CHECK(stale_result.error == EpochIngressError::state_rejected);
    CHECK(stale_result.permission ==
          EpochConsensusPermission::rejected_identity);
    no_effects();

    auto invalid_body = valid;
    invalid_body.body = {FixedConsensusBodyValidator::invalid_marker};
    const auto invalid_body_result = harness.transport.dispatch_proposal(
        proposal_message(invalid_body),
        AuthenticatedEpochPeer::replica(4));
    CHECK(invalid_body_result.error == EpochIngressError::wire_rejected);
    CHECK(invalid_body_result.wire_error ==
          EpochConsensusWireError::invalid_body);
    CHECK(harness.body_validator.proposal_calls == 1);
    CHECK(harness.proposal_effects.relay_count == 0);
    CHECK(harness.proposal_effects.process_count == 0);
    CHECK(harness.future.size() == 0);

    const auto accepted = harness.transport.dispatch_proposal(
        proposal_message(valid),
        AuthenticatedEpochPeer::replica(4));
    CHECK(accepted.error == EpochIngressError::none);
    CHECK(accepted.permission == EpochConsensusPermission::admit_or_buffer);
    CHECK(accepted.admission_disposition ==
          ProposalDisposition::admitted_active);
    CHECK(harness.body_validator.proposal_calls == 2);
    CHECK(harness.proposal_effects.relay_count == 1);
    CHECK(harness.proposal_effects.process_count == 1);
    CHECK(harness.proposal_effects.relayed[0] == valid.key());
    CHECK(harness.proposal_effects.processed[0] == valid.key());

    RuntimeHarness invalid_limits(
        0, EpochWireLimits{0, 8, 16, 128});
    const auto invalid_limits_result =
        invalid_limits.transport.dispatch_proposal(
            proposal_message(valid),
            AuthenticatedEpochPeer::replica(4));
    CHECK(invalid_limits_result.error == EpochIngressError::wire_rejected);
    CHECK(invalid_limits_result.wire_error ==
          EpochConsensusWireError::invalid_limits);
    CHECK(invalid_limits.body_validator.proposal_calls == 0);
}

TEST_CASE("only an explicitly staged successor generation is admissible",
          "[rem-d11][epoch-runtime][msg-propose][generation][staging]"
          "[intentional-red]")
{
    RuntimeHarness harness;
    const auto stage1 = harness.successor();
    REQUIRE(harness.stage(stage1).acknowledgement.has_value());
    const auto *const epoch1 = harness.store.find_epoch(1);
    REQUIRE(epoch1 != nullptr);

    auto epoch2_input = successor_input(*epoch1);
    const auto &epoch2 = harness.store.stage(
        epoch2_input, successor_context());
    const auto epoch2_configuration = configuration(epoch2, 0);
    const auto epoch2_generation = reserved_nonzero_generation(2, 0);
    REQUIRE(epoch2_generation.has_value());
    const auto epoch2_leader =
        epoch2.trees().front().members_breadth_first.front();

    const auto preloaded = consensus_envelope(
        epoch2_configuration,
        *epoch2_generation,
        "preloaded-unstaged-epoch-two",
        epoch2_leader);
    const auto before_successor_activation =
        harness.transport.dispatch_proposal(
            proposal_message(preloaded),
            AuthenticatedEpochPeer::replica(4));
    CHECK(before_successor_activation.error ==
          EpochIngressError::state_rejected);
    CHECK(before_successor_activation.permission ==
          EpochConsensusPermission::rejected_identity);
    CHECK(harness.body_validator.proposal_calls == 0);
    CHECK(harness.proposal_effects.relay_count == 0);
    CHECK(harness.proposal_effects.process_count == 0);
    CHECK(harness.future.size() == 0);
    CHECK(harness.retryable_future.size() == 0);

    REQUIRE(harness.arm(arm_for(stage1.activation)).disposition ==
            ReplicaArmDisposition::armed);
    const auto activated = harness.transport.dispatch_commit(
        kActivationHeight, harness.epoch0->epoch_digest());
    REQUIRE(activated.transition == ActivationTransition::activated);

    const auto still_unstaged = consensus_envelope(
        epoch2_configuration,
        *epoch2_generation,
        "still-unstaged-epoch-two",
        epoch2_leader);
    const auto before_explicit_stage = harness.transport.dispatch_proposal(
        proposal_message(still_unstaged),
        AuthenticatedEpochPeer::replica(4));
    CHECK(before_explicit_stage.error == EpochIngressError::state_rejected);
    CHECK(before_explicit_stage.permission ==
          EpochConsensusPermission::rejected_identity);
    CHECK(harness.body_validator.proposal_calls == 0);
    CHECK(harness.proposal_effects.relay_count == 0);
    CHECK(harness.proposal_effects.process_count == 0);
    CHECK(harness.future.size() == 0);
    CHECK(harness.retryable_future.size() == 0);

    const auto stage2 = stage_message(*epoch1, epoch2_input);
    const auto explicitly_staged = harness.stage(stage2);
    CHECK(explicitly_staged.error == EpochIngressError::none);
    CHECK(explicitly_staged.disposition ==
          ReplicaStageDisposition::duplicate);
    REQUIRE(explicitly_staged.acknowledgement.has_value());

    const auto admitted_after_stage = consensus_envelope(
        epoch2_configuration,
        *epoch2_generation,
        "explicitly-staged-epoch-two",
        epoch2_leader);
    const auto accepted = harness.transport.dispatch_proposal(
        proposal_message(admitted_after_stage),
        AuthenticatedEpochPeer::replica(4));
    CHECK(accepted.error == EpochIngressError::none);
    CHECK(accepted.permission ==
          EpochConsensusPermission::admit_or_buffer);
    CHECK(accepted.admission_disposition ==
          ProposalDisposition::buffered_future);
    CHECK(harness.body_validator.proposal_calls == 1);
    CHECK(harness.proposal_effects.relay_count == 1);
    CHECK(harness.proposal_effects.process_count == 0);
    CHECK(harness.future.size() == 1);
    CHECK(harness.retryable_future.size() == 1);
}

TEST_CASE("manager handler arms once after exact authenticated ack quorum",
          "[rem-d11][epoch-runtime][ack][transport][quorum]"
          "[intentional-red]")
{
    RuntimeHarness harness;
    const auto stage = harness.successor();
    EpochAckTracker tracker(stage.activation, membership(), 2);
    ManagerEpochAckEndpoint manager(
        tracker, EpochProtocolMode::adaptive_v1, wire_limits());
    harness.transport.register_manager_ack_handler(manager);

    const auto forged = harness.transport.dispatch_ack(
        MsgStageAck(acknowledgement(0, stage.activation), wire_limits()),
        AuthenticatedEpochPeer::replica(1));
    CHECK(forged.error == EpochIngressError::state_rejected);
    CHECK(forged.disposition == StageAckDisposition::reporter_mismatch);
    CHECK(tracker.acknowledgement_count() == 0);

    for (ReplicaID replica = 0; replica < 5; ++replica)
    {
        const auto result = harness.transport.dispatch_ack(
            MsgStageAck(
                acknowledgement(replica, stage.activation),
                wire_limits()),
            AuthenticatedEpochPeer::replica(replica));
        CHECK(result.error == EpochIngressError::none);
        CHECK(result.accepted_acknowledgements == replica + 1);
        CHECK(result.required_acknowledgements == 5);
        CHECK(result.arm.has_value() == (replica == 4));
    }

    const auto duplicate = harness.transport.dispatch_ack(
        MsgStageAck(acknowledgement(4, stage.activation), wire_limits()),
        AuthenticatedEpochPeer::replica(4));
    CHECK(duplicate.disposition == StageAckDisposition::duplicate);
    CHECK_FALSE(duplicate.arm.has_value());
    CHECK(tracker.acknowledgement_count() == 5);
}

TEST_CASE("v1 ack tracker accepts membership above the minimum fault bound",
          "[c01][epoch-runtime][ack][quorum][v1][regression]")
{
    const EpochActivationIdentity activation{
        0,
        digest("c01-v1-n8-predecessor"),
        1,
        digest("c01-v1-n8-successor"),
        kActivationHeight};
    EpochAckTracker tracker(
        activation, {0, 1, 2, 3, 4, 5, 6, 7}, 2);

    CHECK(tracker.required_acknowledgements() == 5);
}

TEST_CASE("every post-prepare stage rejection consumes only its candidate",
          "[rem-d11][epoch-runtime][prepare][discard][atomic]"
          "[intentional-red]")
{
    RuntimeHarness harness;
    const auto accepted_stage = harness.successor();
    REQUIRE(harness.stage(accepted_stage).acknowledgement.has_value());
    const auto *accepted_slot = harness.transaction.only_occupied_slot();
    REQUIRE(accepted_slot != nullptr);
    const auto accepted_token = accepted_slot->token;
    const auto store_size = harness.store.size();

    auto wrong_predecessor = accepted_stage;
    wrong_predecessor.activation.predecessor_epoch_digest =
        digest("wrong-predecessor");
    wrong_predecessor.definition.previous_epoch_digest =
        wrong_predecessor.activation.predecessor_epoch_digest;
    const auto wrong_predecessor_digest =
        hotstuff::compute_epoch_digest(wrong_predecessor.definition);
    wrong_predecessor.definition.epoch_digest = wrong_predecessor_digest;
    wrong_predecessor.activation.successor_epoch_digest =
        wrong_predecessor_digest;
    const auto rejected_predecessor = harness.stage(wrong_predecessor);
    CHECK(rejected_predecessor.error == EpochIngressError::state_rejected);
    CHECK(rejected_predecessor.disposition ==
          ReplicaStageDisposition::wrong_predecessor);
    CHECK(harness.transaction.discard_count == 1);
    CHECK(harness.transaction.last_discarded_token != accepted_token);
    REQUIRE(harness.transaction.only_occupied_slot() != nullptr);
    CHECK(harness.transaction.only_occupied_slot()->token == accepted_token);
    CHECK(harness.store.size() == store_size);

    auto sparse_input = successor_input(*harness.epoch0);
    ++sparse_input.epoch_number;
    const auto wrong_successor = stage_message(
        *harness.epoch0, std::move(sparse_input));
    const auto rejected_successor = harness.stage(wrong_successor);
    CHECK(rejected_successor.error == EpochIngressError::state_rejected);
    CHECK(rejected_successor.disposition ==
          ReplicaStageDisposition::wrong_successor);
    CHECK(harness.transaction.discard_count == 2);
    CHECK(harness.transaction.last_discarded_token != accepted_token);
    REQUIRE(harness.transaction.only_occupied_slot() != nullptr);
    CHECK(harness.transaction.only_occupied_slot()->token == accepted_token);
    CHECK(harness.store.size() == store_size);
    CHECK_FALSE(harness.transaction.invariant_failed);

    RuntimeHarness invalid_harness;
    auto invalid_input = successor_input(*invalid_harness.epoch0);
    invalid_input.membership_digest = digest("invalid-membership");
    const auto invalid = stage_message(
        *invalid_harness.epoch0, std::move(invalid_input));
    const auto rejected_validation = invalid_harness.stage(invalid);
    CHECK(rejected_validation.error == EpochIngressError::validation_failed);
    CHECK_FALSE(rejected_validation.disposition.has_value());
    CHECK_FALSE(rejected_validation.acknowledgement.has_value());
    CHECK(invalid_harness.transaction.prepare_count == 1);
    CHECK(invalid_harness.transaction.discard_count == 1);
    CHECK(invalid_harness.transaction.occupied_tokens() == 0);
    CHECK(invalid_harness.store.size() == 1);
    CHECK_FALSE(invalid_harness.transaction.invariant_failed);
}

TEST_CASE("prepared-token ownership survives restart and fails closed",
          "[rem-d11][epoch-runtime][prepare][restart][recovery]"
          "[intentional-red]")
{
    SECTION("restart discards retained ownership and adopts an exact duplicate")
    {
        RuntimeHarness harness;
        const auto stage = harness.successor();
        REQUIRE(harness.stage(stage).acknowledgement.has_value());
        REQUIRE(harness.transaction.only_occupied_slot() != nullptr);
        const auto first_token =
            harness.transaction.only_occupied_slot()->token;

        harness.restart_adapter();
        CHECK(harness.transaction.discarded(first_token));
        CHECK(harness.transaction.occupied_tokens() == 0);
        const auto duplicate = harness.stage(stage);
        REQUIRE(duplicate.disposition == ReplicaStageDisposition::duplicate);
        REQUIRE(harness.transaction.only_occupied_slot() != nullptr);
        const auto adopted_token =
            harness.transaction.only_occupied_slot()->token;
        CHECK(adopted_token != first_token);

        harness.adapter.reset();
        CHECK(harness.transaction.discarded(adopted_token));
        CHECK(harness.transaction.occupied_tokens() == 0);
        CHECK_FALSE(harness.transaction.invariant_failed);
    }

    SECTION("missing retained runtime is detected before phase-one mutation")
    {
        RuntimeHarness harness;
        const auto stage = harness.successor();
        REQUIRE(harness.activation->stage(stage, successor_context())
                    .acknowledgement.has_value());
        REQUIRE(harness.activation->arm(arm_for(stage.activation)) ==
                ReplicaArmDisposition::armed);
        const auto before = harness.activation->active_effect();
        const auto store_size = harness.store.size();

        const auto missing = harness.transport.dispatch_commit(
            kActivationHeight, harness.epoch0->epoch_digest());
        CHECK(missing.error == EpochIngressError::missing_prepared_runtime);
        CHECK(missing.transition == ActivationTransition::waiting);
        CHECK_FALSE(missing.update.has_value());
        CHECK(harness.activation->active_effect().configuration ==
              before.configuration);
        CHECK(harness.activation->active_effect().generation ==
              before.generation);
        CHECK(harness.store.size() == store_size);
        CHECK(harness.transaction.commit_count == 0);

        const auto adopted = harness.stage(stage);
        REQUIRE(adopted.disposition == ReplicaStageDisposition::duplicate);
        REQUIRE(harness.transaction.only_occupied_slot() != nullptr);
        const auto activated = harness.transport.dispatch_commit(
            kActivationHeight, harness.epoch0->epoch_digest());
        CHECK(activated.error == EpochIngressError::none);
        CHECK(activated.transition == ActivationTransition::activated);
        CHECK(harness.transaction.commit_count == 1);
        CHECK_FALSE(harness.transaction.invariant_failed);
    }

    SECTION("missing definition recovery pauses then replays exact H")
    {
        RuntimeHarness harness;
        const auto stage = harness.successor();
        REQUIRE(harness.activation->restore_expectation(
            recovery_status(0, stage.activation)));
        const auto missing = harness.transport.dispatch_commit(
            kActivationHeight, harness.epoch0->epoch_digest());
        CHECK(missing.transition == ActivationTransition::blocked);
        CHECK(missing.blocked_reason ==
              ActivationBlockReason::missing_definition);
        REQUIRE(harness.activation->recovery_status().has_value());
        CHECK(harness.activation->recovery_status()->recovery_need ==
              ActivationRecoveryNeed::exact_definition);

        REQUIRE(harness.stage(stage).acknowledgement.has_value());
        REQUIRE(harness.arm(arm_for(stage.activation)).disposition ==
                ReplicaArmDisposition::armed);
        const auto recovered = harness.transport.dispatch_replay();
        CHECK(recovered.transition == ActivationTransition::activated);
        CHECK(harness.transaction.commit_count == 1);
        CHECK_FALSE(harness.activation->recovery_status().has_value());
        CHECK_FALSE(harness.transaction.invariant_failed);
    }

    SECTION("permanent exact-H block discards the retained token once")
    {
        RuntimeHarness harness;
        const auto stage = harness.successor();
        REQUIRE(harness.stage(stage).acknowledgement.has_value());
        REQUIRE(harness.arm(arm_for(stage.activation)).disposition ==
                ReplicaArmDisposition::armed);
        REQUIRE(harness.transaction.only_occupied_slot() != nullptr);
        const auto retained = harness.transaction.only_occupied_slot()->token;

        const auto blocked = harness.transport.dispatch_commit(
            kActivationHeight, digest("wrong-committed-predecessor"));
        CHECK(blocked.transition == ActivationTransition::blocked);
        CHECK(blocked.blocked_reason ==
              ActivationBlockReason::predecessor_digest_mismatch);
        CHECK(harness.transaction.discarded(retained));
        CHECK(harness.transaction.discard_count == 1);
        CHECK(harness.transaction.occupied_tokens() == 0);

        const auto replay = harness.transport.dispatch_replay();
        CHECK(replay.transition == ActivationTransition::blocked);
        CHECK(harness.transaction.discard_count == 1);
        CHECK(harness.transaction.commit_count == 0);
        CHECK_FALSE(harness.transaction.invariant_failed);
    }
}

TEST_CASE("oversized handlers preflight before payload-sized allocation",
          "[rem-d11][epoch-runtime][wire][allocation][preflight]"
          "[intentional-red]")
{
    constexpr std::size_t oversized = 64 * 1024;
    constexpr std::size_t allocation_ceiling = oversized / 2;
    RuntimeHarness harness;
    const auto stage = harness.successor();
    EpochAckTracker tracker(stage.activation, membership(), 2);
    ManagerEpochAckEndpoint manager(
        tracker, EpochProtocolMode::adaptive_v1, wire_limits());
    harness.transport.register_manager_ack_handler(manager);

    const auto stage_store_before = harness.store.size();
    const auto stage_prepare_before = harness.transaction.prepare_count;
    hotstuff::ReplicaStageIngressResult stage_result;
    bool stage_threw = false;
    bool stage_allocated = false;
    {
        auto message = oversized_stage(oversized);
        d11_allocation_guard::RejectAtLeast guard(allocation_ceiling);
        try
        {
            stage_result = harness.transport.dispatch_stage(
                std::move(message),
                AuthenticatedEpochPeer::manager(),
                successor_context());
        }
        catch (const std::bad_alloc &)
        {
            stage_threw = true;
        }
        stage_allocated = guard.triggered();
    }
    CHECK_FALSE(stage_threw);
    CHECK_FALSE(stage_allocated);
    CHECK(stage_result.error == EpochIngressError::wire_rejected);
    CHECK(stage_result.wire_error == EpochWireError::payload_too_large);
    CHECK(harness.store.size() == stage_store_before);
    CHECK(harness.transaction.prepare_count == stage_prepare_before);

    const auto arm_block_before = harness.activation->blocked_reason();
    const auto arm_active_before = harness.activation->active_effect();
    hotstuff::ReplicaArmIngressResult arm_result;
    bool arm_threw = false;
    bool arm_allocated = false;
    {
        auto message = oversized_arm(oversized);
        d11_allocation_guard::RejectAtLeast guard(allocation_ceiling);
        try
        {
            arm_result = harness.transport.dispatch_arm(
                std::move(message),
                AuthenticatedEpochPeer::manager());
        }
        catch (const std::bad_alloc &)
        {
            arm_threw = true;
        }
        arm_allocated = guard.triggered();
    }
    CHECK_FALSE(arm_threw);
    CHECK_FALSE(arm_allocated);
    CHECK(arm_result.error == EpochIngressError::wire_rejected);
    CHECK(arm_result.wire_error == EpochWireError::payload_too_large);
    CHECK(harness.activation->blocked_reason() == arm_block_before);
    CHECK(harness.activation->active_effect().configuration ==
          arm_active_before.configuration);
    CHECK(harness.activation->active_effect().generation ==
          arm_active_before.generation);

    const auto ack_count_before = tracker.acknowledgement_count();
    hotstuff::ManagerAckIngressResult ack_result;
    bool ack_threw = false;
    bool ack_allocated = false;
    {
        auto message = oversized_ack(oversized);
        d11_allocation_guard::RejectAtLeast guard(allocation_ceiling);
        try
        {
            ack_result = harness.transport.dispatch_ack(
                std::move(message),
                AuthenticatedEpochPeer::replica(0));
        }
        catch (const std::bad_alloc &)
        {
            ack_threw = true;
        }
        ack_allocated = guard.triggered();
    }
    CHECK_FALSE(ack_threw);
    CHECK_FALSE(ack_allocated);
    CHECK(ack_result.error == EpochIngressError::wire_rejected);
    CHECK(ack_result.wire_error == EpochWireError::payload_too_large);
    CHECK(tracker.acknowledgement_count() == ack_count_before);

    const auto prepare_before = harness.transaction.prepare_count;
    const auto discard_before = harness.transaction.discard_count;
    const auto occupied_before = harness.transaction.occupied_tokens();
    const auto active_before = harness.activation->active_effect();
    const auto blocked_before = harness.activation->blocked_reason();
    const auto recovery_before = harness.activation->recovery_status();
    const auto acknowledgements_before = tracker.acknowledgement_count();
    hotstuff::ReplicaStageIngressResult unauthorized_stage;
    bool unauthorized_stage_allocated = false;
    {
        auto message = oversized_stage(oversized);
        const auto before_this_stage = harness.store.size();
        const auto before_this_prepare = harness.transaction.prepare_count;
        d11_allocation_guard::RejectAtLeast guard(allocation_ceiling);
        unauthorized_stage = harness.transport.dispatch_stage(
            std::move(message),
            AuthenticatedEpochPeer::replica(0),
            successor_context());
        unauthorized_stage_allocated = guard.triggered();
        CHECK(harness.store.size() == before_this_stage);
        CHECK(harness.transaction.prepare_count == before_this_prepare);
    }
    CHECK(unauthorized_stage.error == EpochIngressError::unauthorized_peer);
    CHECK_FALSE(unauthorized_stage.disposition.has_value());
    CHECK_FALSE(unauthorized_stage.acknowledgement.has_value());
    CHECK_FALSE(unauthorized_stage_allocated);

    hotstuff::ReplicaArmIngressResult unauthorized_arm;
    bool unauthorized_arm_allocated = false;
    {
        auto message = oversized_arm(oversized);
        const auto before_this_active = harness.activation->active_effect();
        const auto before_this_blocked =
            harness.activation->blocked_reason();
        d11_allocation_guard::RejectAtLeast guard(allocation_ceiling);
        unauthorized_arm = harness.transport.dispatch_arm(
            std::move(message),
            AuthenticatedEpochPeer::replica(0));
        unauthorized_arm_allocated = guard.triggered();
        CHECK(harness.activation->active_effect().configuration ==
              before_this_active.configuration);
        CHECK(harness.activation->active_effect().generation ==
              before_this_active.generation);
        CHECK(harness.activation->blocked_reason() == before_this_blocked);
    }
    CHECK(unauthorized_arm.error == EpochIngressError::unauthorized_peer);
    CHECK_FALSE(unauthorized_arm.disposition.has_value());
    CHECK_FALSE(unauthorized_arm_allocated);

    hotstuff::ManagerAckIngressResult unauthorized_ack;
    bool unauthorized_ack_allocated = false;
    {
        auto message = oversized_ack(oversized);
        const auto before_this_ack = tracker.acknowledgement_count();
        d11_allocation_guard::RejectAtLeast guard(allocation_ceiling);
        unauthorized_ack = harness.transport.dispatch_ack(
            std::move(message),
            AuthenticatedEpochPeer::manager());
        unauthorized_ack_allocated = guard.triggered();
        CHECK(tracker.acknowledgement_count() == before_this_ack);
    }
    CHECK(unauthorized_ack.error == EpochIngressError::unauthorized_peer);
    CHECK_FALSE(unauthorized_ack.disposition.has_value());
    CHECK_FALSE(unauthorized_ack.arm.has_value());
    CHECK_FALSE(unauthorized_ack_allocated);
    CHECK(harness.transaction.prepare_count == prepare_before);
    CHECK(harness.transaction.discard_count == discard_before);
    CHECK(harness.transaction.occupied_tokens() == occupied_before);
    CHECK(harness.activation->active_effect().configuration ==
          active_before.configuration);
    CHECK(harness.activation->active_effect().generation ==
          active_before.generation);
    CHECK(harness.activation->blocked_reason() == blocked_before);
    CHECK(harness.activation->recovery_status().has_value() ==
          recovery_before.has_value());
    CHECK(harness.activation->admits_new_proposals());
    CHECK(tracker.acknowledgement_count() == acknowledgements_before);

    RuntimeHarness invalid_limits(
        0, EpochWireLimits{0, 8, 16, 128});
    const auto invalid = invalid_limits.transport.dispatch_stage(
        MsgStageEpochDefinition(
            invalid_limits.successor(), wire_limits()),
        AuthenticatedEpochPeer::manager(),
        successor_context());
    CHECK(invalid.error == EpochIngressError::wire_rejected);
    CHECK(invalid.wire_error == EpochWireError::invalid_limits);
    CHECK(invalid_limits.transaction.prepare_count == 0);

    auto mixed_arm_wire = hotstuff::encode_epoch_wire(
        arm_for(stage.activation), wire_limits());
    REQUIRE(mixed_arm_wire.size() > 4);
    mixed_arm_wire[4] =
        static_cast<std::uint8_t>(EpochProtocolMode::legacy_static);
    const auto mixed_arm_result = harness.transport.dispatch_arm(
        MsgArmActivation(
            hotstuff::DataStream(std::move(mixed_arm_wire))),
        AuthenticatedEpochPeer::manager());
    CHECK(mixed_arm_result.error == EpochIngressError::wire_rejected);
    CHECK(mixed_arm_result.wire_error == EpochWireError::mode_mismatch);
    CHECK_FALSE(mixed_arm_result.disposition.has_value());

    auto mixed_ack_wire = hotstuff::encode_epoch_wire(
        acknowledgement(0, stage.activation), wire_limits());
    REQUIRE(mixed_ack_wire.size() > 4);
    mixed_ack_wire[4] =
        static_cast<std::uint8_t>(EpochProtocolMode::legacy_static);
    const auto mixed_ack_result = harness.transport.dispatch_ack(
        MsgStageAck(
            hotstuff::DataStream(std::move(mixed_ack_wire))),
        AuthenticatedEpochPeer::replica(0));
    CHECK(mixed_ack_result.error == EpochIngressError::wire_rejected);
    CHECK(mixed_ack_result.wire_error == EpochWireError::mode_mismatch);
    CHECK(tracker.acknowledgement_count() == acknowledgements_before);

    const auto invalid_arm = invalid_limits.transport.dispatch_arm(
        MsgArmActivation(arm_for(invalid_limits.successor().activation),
                         wire_limits()),
        AuthenticatedEpochPeer::manager());
    CHECK(invalid_arm.error == EpochIngressError::wire_rejected);
    CHECK(invalid_arm.wire_error == EpochWireError::invalid_limits);
    CHECK_FALSE(invalid_arm.disposition.has_value());

    EpochAckTracker invalid_tracker(stage.activation, membership(), 2);
    ManagerEpochAckEndpoint invalid_manager(
        invalid_tracker,
        EpochProtocolMode::adaptive_v1,
        EpochWireLimits{0, 8, 16, 128});
    harness.transport.register_manager_ack_handler(invalid_manager);
    const auto invalid_ack = harness.transport.dispatch_ack(
        MsgStageAck(acknowledgement(0, stage.activation), wire_limits()),
        AuthenticatedEpochPeer::replica(0));
    CHECK(invalid_ack.error == EpochIngressError::wire_rejected);
    CHECK(invalid_ack.wire_error == EpochWireError::invalid_limits);
    CHECK(invalid_tracker.acknowledgement_count() == 0);
}

TEST_CASE("exact H performs one prepared swap without ordinary new",
          "[rem-d11][epoch-runtime][commit][transaction][ordinary-new]"
          "[intentional-red]")
{
    RuntimeHarness harness;
    const auto stage = harness.successor();
    REQUIRE(harness.stage(stage).acknowledgement.has_value());
    REQUIRE(harness.arm(arm_for(stage.activation)).disposition ==
            ReplicaArmDisposition::armed);

    const auto at_1000 = harness.transport.dispatch_commit(
        1000, harness.epoch0->epoch_digest());
    CHECK(at_1000.transition == ActivationTransition::waiting);
    CHECK(harness.transaction.commit_count == 0);

    std::optional<EpochCommitIngressResult> activated;
    bool commit_allocated = false;
    {
        // This guard deliberately intercepts ordinary C++ operator new/new[];
        // aligned allocation, malloc, and platform allocators are out of scope.
        d11_allocation_guard::RejectAll no_allocations;
        activated.emplace(harness.transport.dispatch_commit(
            kActivationHeight,
            harness.epoch0->epoch_digest()));
        commit_allocated = no_allocations.triggered();
    }
    CHECK_FALSE(commit_allocated);
    REQUIRE(activated.has_value());
    CHECK(activated->transition == ActivationTransition::activated);
    REQUIRE(activated->update.has_value());
    CHECK(harness.transaction.commit_count == 1);
    CHECK(harness.transaction.occupied_tokens() == 0);
    CHECK_FALSE(harness.transaction.invariant_failed);
    CHECK(harness.transaction.tree_configuration ==
          activated->update->activation.configuration);
    CHECK(harness.transaction.context_configuration ==
          activated->update->activation.configuration);
    CHECK(harness.transaction.admission_configuration ==
          activated->update->activation.configuration);
    REQUIRE(harness.transaction.leader_view.has_value());
    CHECK(harness.transaction.leader_view->configuration ==
          activated->update->activation.configuration);
    CHECK(harness.transaction.leader_view->view_generation ==
          activated->update->activation.generation);
    CHECK(harness.transaction.leader_deadline_configuration ==
          activated->update->activation.configuration);
    CHECK(harness.contexts.active_configuration() ==
          activated->update->activation.configuration);

    const auto empty_drain = harness.adapter->drain_activated_futures();
    CHECK(empty_drain.status == EpochFutureDrainStatus::complete);
    CHECK(empty_drain.processed == 0);
    CHECK(empty_drain.remaining == 0);

    const auto replay = harness.transport.dispatch_commit(
        kActivationHeight,
        harness.epoch0->epoch_digest());
    CHECK(replay.transition == ActivationTransition::already_active);
    CHECK(harness.transaction.commit_count == 1);
    CHECK(harness.transaction.committed_tokens[0] != 0);
    CHECK(harness.transaction.committed_generations[0] ==
          activated->update->activation.generation);
    CHECK_FALSE(harness.transaction.invariant_failed);
}

TEST_CASE("blocked gate requires a revalidated exact lease to drain",
          "[rem-d11][epoch-runtime][proposal][lease][generation]"
          "[intentional-red]")
{
    RuntimeHarness harness;
    const auto stage = harness.successor();
    REQUIRE(harness.stage(stage).acknowledgement.has_value());

    const auto old_configuration = configuration(*harness.epoch0, 0);
    const auto old_identity = consensus_envelope(
        old_configuration,
        harness.activation->active_effect().generation,
        "old-open-context",
        0);
    const auto metadata = hotstuff::make_exact_proposal_context_metadata(
        old_identity.key(),
        0,
        harness.epoch0->trees().front().members_breadth_first,
        2,
        2,
        5);
    REQUIRE(metadata.has_value());
    const auto lease = harness.contexts.admit_remote(*metadata);
    REQUIRE(lease.has_value());
    const auto retired_identity = consensus_envelope(
        old_configuration,
        old_identity.view_generation,
        "old-retired-context",
        0);
    const auto retired_metadata =
        hotstuff::make_exact_proposal_context_metadata(
            retired_identity.key(),
            0,
            harness.epoch0->trees().front().members_breadth_first,
            2,
            2,
            5);
    REQUIRE(retired_metadata.has_value());
    const auto retired_lease =
        harness.contexts.admit_remote(*retired_metadata);
    REQUIRE(retired_lease.has_value());
    const auto proposal_peer = AuthenticatedEpochPeer::replica(5);

    const auto blocked = harness.transport.dispatch_commit(
        kActivationHeight,
        harness.epoch0->epoch_digest());
    CHECK(blocked.transition == ActivationTransition::blocked);
    CHECK(blocked.blocked_reason == ActivationBlockReason::missing_arm);
    CHECK(harness.contexts.revalidate(*lease));
    CHECK(harness.transport.dispatch_existing(
              proposal_message(old_identity), proposal_peer, *lease)
              .permission == EpochConsensusPermission::drain_existing);

    const auto relay_before_close = harness.proposal_effects.relay_count;
    const auto process_before_close = harness.proposal_effects.process_count;
    REQUIRE(harness.contexts.close(
        old_identity.key(), ProposalContextEvent::committed));
    CHECK_FALSE(harness.contexts.revalidate(*lease));
    CHECK(harness.transport.dispatch_existing(
              proposal_message(old_identity), proposal_peer, *lease)
              .permission == EpochConsensusPermission::rejected_identity);
    CHECK(harness.proposal_effects.relay_count == relay_before_close);
    CHECK(harness.proposal_effects.process_count == process_before_close);

    CHECK(harness.contexts.revalidate(*retired_lease));
    REQUIRE(harness.contexts.retire_configuration(old_configuration) >= 1);
    CHECK_FALSE(harness.contexts.revalidate(*retired_lease));
    CHECK(harness.transport.dispatch_existing(
              proposal_message(retired_identity),
              proposal_peer,
              *retired_lease)
              .permission == EpochConsensusPermission::rejected_identity);
    CHECK(harness.proposal_effects.relay_count == relay_before_close);
    CHECK(harness.proposal_effects.process_count == process_before_close);

    auto wrong_generation = old_identity;
    ++wrong_generation.view_generation;
    CHECK(harness.transport.dispatch_existing(
              proposal_message(wrong_generation), proposal_peer, *lease)
              .permission == EpochConsensusPermission::rejected_identity);
    auto wrong_key = old_identity;
    wrong_key.block_hash = digest("not-the-leased-key");
    CHECK(harness.transport.dispatch_existing(
              proposal_message(wrong_key), proposal_peer, *lease)
              .permission == EpochConsensusPermission::rejected_identity);

    const auto new_old = consensus_envelope(
        old_configuration,
        old_identity.view_generation,
        "new-old-key",
        0);
    CHECK(harness.transport.dispatch_proposal(
              proposal_message(new_old), proposal_peer)
              .permission == EpochConsensusPermission::paused);
    const ConfigurationId future_configuration{
        stage.activation.successor_epoch_number,
        0,
        stage.activation.successor_epoch_digest};
    const auto future_identity = consensus_envelope(
        future_configuration,
        *reserved_nonzero_generation(future_configuration.epoch_number, 0),
        "new-future-key",
        2);
    CHECK(harness.transport.dispatch_proposal(
              proposal_message(future_identity), proposal_peer)
              .permission == EpochConsensusPermission::paused);

    REQUIRE(harness.arm(arm_for(stage.activation)).disposition ==
            ReplicaArmDisposition::armed);
    const auto recovered = harness.transport.dispatch_replay();
    REQUIRE(recovered.transition == ActivationTransition::activated);
    REQUIRE(recovered.update.has_value());
    const auto active_identity = consensus_envelope(
        recovered.update->activation.configuration,
        recovered.update->activation.generation,
        "new-active-key",
        2);
    CHECK(harness.transport.dispatch_proposal(
              proposal_message(active_identity), proposal_peer)
              .permission == EpochConsensusPermission::admit_or_buffer);
    const auto stale = harness.transport.dispatch_proposal(
        proposal_message(old_identity), proposal_peer);
    CHECK(stale.error == EpochIngressError::state_rejected);
    CHECK(stale.permission == EpochConsensusPermission::rejected_identity);
}

TEST_CASE("future drain is retryable outside the nofail runtime swap",
          "[rem-d11][epoch-runtime][future][drain][retry]"
          "[intentional-red]")
{
    RuntimeHarness harness;
    const auto stage = harness.successor();
    REQUIRE(harness.stage(stage).acknowledgement.has_value());
    const auto *epoch1 = harness.store.find_epoch(1);
    REQUIRE(epoch1 != nullptr);
    const auto first = proposal(*epoch1, 0, "matching-future-first");
    const auto middle = proposal(*epoch1, 0, "matching-future-middle");
    const auto last = proposal(*epoch1, 0, "matching-future-last");
    const auto other_tree = proposal(*epoch1, 1, "other-tree-future");
    REQUIRE(harness.retryable_future.insert(first));
    REQUIRE(harness.retryable_future.insert(middle));
    REQUIRE(harness.retryable_future.insert(last));
    REQUIRE(harness.retryable_future.insert(other_tree));
    const auto active_configuration = configuration(*epoch1, 0);
    CHECK(harness.retryable_future.matching_size(active_configuration) == 3);
    CHECK(harness.retryable_future.size() == 4);

    REQUIRE(harness.arm(arm_for(stage.activation)).disposition ==
            ReplicaArmDisposition::armed);
    std::optional<EpochCommitIngressResult> activated;
    bool commit_allocated = false;
    {
        d11_allocation_guard::RejectAll no_allocations;
        activated.emplace(harness.transport.dispatch_commit(
            kActivationHeight, harness.epoch0->epoch_digest()));
        commit_allocated = no_allocations.triggered();
    }
    CHECK_FALSE(commit_allocated);
    REQUIRE(activated.has_value());
    REQUIRE(activated->transition == ActivationTransition::activated);
    CHECK(harness.retryable_future.size() == 4);
    CHECK(harness.transaction.admission_configuration ==
          active_configuration);

    harness.retryable_future.fail_next_process = true;
    const auto first_failure =
        harness.adapter->drain_activated_futures();
    CHECK(first_failure.status == EpochFutureDrainStatus::process_failed);
    CHECK(first_failure.processed == 0);
    CHECK(first_failure.remaining == 3);
    CHECK(harness.retryable_future.matching_size(active_configuration) == 3);
    CHECK(harness.retryable_future.completed_count == 0);
    CHECK(harness.retryable_future.complete_count == 0);

    harness.retryable_future.fail_process_attempt =
        harness.retryable_future.process_attempts + 2;
    const auto middle_failure =
        harness.adapter->drain_activated_futures();
    CHECK(middle_failure.status == EpochFutureDrainStatus::process_failed);
    CHECK(middle_failure.processed == 1);
    CHECK(middle_failure.remaining == 2);
    CHECK(harness.retryable_future.matching_size(active_configuration) == 2);
    CHECK(harness.retryable_future.completed_count == 1);
    CHECK(harness.retryable_future.completed[0] == first.metadata.key());
    CHECK(harness.retryable_future.complete_count == 0);

    EpochFutureDrainResult allocation_failure;
    bool allocation_rejected = false;
    {
        d11_allocation_guard::RejectAll no_ordinary_new;
        allocation_failure = harness.adapter->drain_activated_futures();
        allocation_rejected = no_ordinary_new.triggered();
    }
    CHECK(allocation_rejected);
    CHECK(allocation_failure.status ==
          EpochFutureDrainStatus::allocation_failed);
    CHECK(allocation_failure.processed == 0);
    CHECK(allocation_failure.remaining == 2);
    CHECK(harness.retryable_future.matching_size(active_configuration) == 2);
    CHECK(harness.retryable_future.completed_count == 1);
    CHECK(harness.retryable_future.complete_count == 0);

    const auto recovered = harness.adapter->drain_activated_futures();
    CHECK(recovered.status == EpochFutureDrainStatus::complete);
    CHECK(recovered.processed == 2);
    CHECK(recovered.remaining == 0);
    CHECK(harness.retryable_future.matching_size(active_configuration) == 0);
    REQUIRE(harness.retryable_future.completed_count == 3);
    CHECK(harness.retryable_future.completed[0] == first.metadata.key());
    CHECK(harness.retryable_future.completed[1] == middle.metadata.key());
    CHECK(harness.retryable_future.completed[2] == last.metadata.key());
    CHECK(harness.retryable_future.complete_count == 1);
    CHECK(harness.retryable_future.completed_configuration ==
          active_configuration);
    CHECK(harness.retryable_future.size() == 1);
    CHECK(harness.retryable_future.contains(other_tree.metadata.key()));

    const auto idempotent = harness.adapter->drain_activated_futures();
    CHECK(idempotent.status == EpochFutureDrainStatus::complete);
    CHECK(idempotent.processed == 0);
    CHECK(idempotent.remaining == 0);
    CHECK(harness.retryable_future.complete_count == 1);
    CHECK(harness.retryable_future.size() == 1);
    CHECK_FALSE(harness.retryable_future.invariant_failed);
    CHECK_FALSE(harness.transaction.invariant_failed);
}

TEST_CASE("future drain releases each reserved token exactly once on copy failure",
          "[rem-d11][epoch-runtime][future][claim][ownership]"
          "[intentional-red]")
{
    RuntimeHarness harness;
    const auto stage = harness.successor();
    REQUIRE(harness.stage(stage).acknowledgement.has_value());
    const auto *const epoch1 = harness.store.find_epoch(1);
    REQUIRE(epoch1 != nullptr);
    const auto first = proposal(*epoch1, 0, "claim-copy-first");
    const auto middle = proposal(*epoch1, 0, "claim-copy-middle");
    const auto last = proposal(*epoch1, 0, "claim-copy-last");
    REQUIRE(harness.retryable_future.insert(first));
    REQUIRE(harness.retryable_future.insert(middle));
    REQUIRE(harness.retryable_future.insert(last));

    REQUIRE(harness.arm(arm_for(stage.activation)).disposition ==
            ReplicaArmDisposition::armed);
    REQUIRE(harness.transport.dispatch_commit(
                kActivationHeight, harness.epoch0->epoch_digest())
                .transition == ActivationTransition::activated);

    harness.retryable_future.fail_next_process = true;
    harness.retryable_future.fail_claim_after_reserve_call = 3;
    const auto failed = harness.adapter->drain_activated_futures();
    CHECK(failed.status == EpochFutureDrainStatus::allocation_failed);
    CHECK(failed.processed == 0);
    CHECK(failed.remaining == 3);
    REQUIRE(harness.retryable_future.claimed_count == 3);
    for (std::size_t index = 0; index < 3; ++index)
    {
        const auto token = harness.retryable_future.claimed_tokens[index];
        CAPTURE(index);
        CAPTURE(token);
        CHECK(harness.retryable_future.release_calls[token] == 1);
        CHECK(harness.retryable_future.acknowledge_calls[token] == 0);
    }
    CHECK_FALSE(harness.retryable_future.invariant_failed);
    CHECK(harness.retryable_future.completed_count == 0);
    CHECK(harness.retryable_future.complete_count == 0);

    const auto recovered = harness.adapter->drain_activated_futures();
    CHECK(recovered.status == EpochFutureDrainStatus::complete);
    CHECK(recovered.processed == 3);
    CHECK(recovered.remaining == 0);
    CHECK(harness.retryable_future.completed_count == 3);
    CHECK(harness.retryable_future.completed_occurrences(
              first.metadata.key()) == 1);
    CHECK(harness.retryable_future.completed_occurrences(
              middle.metadata.key()) == 1);
    CHECK(harness.retryable_future.completed_occurrences(
              last.metadata.key()) == 1);
    REQUIRE(harness.retryable_future.claimed_count == 6);
    for (std::size_t index = 0;
         index < harness.retryable_future.claimed_count;
         ++index)
    {
        const auto token = harness.retryable_future.claimed_tokens[index];
        CAPTURE(index);
        CAPTURE(token);
        CHECK(harness.retryable_future.release_calls[token] +
                  harness.retryable_future.acknowledge_calls[token] ==
              1);
    }
    CHECK(harness.retryable_future.complete_count == 1);
    CHECK(harness.retryable_future.size() == 0);
    CHECK_FALSE(harness.retryable_future.invariant_failed);
    CHECK_FALSE(harness.transaction.invariant_failed);
}

TEST_CASE("future drain releases concrete first claim when reserve fails",
          "[rem-d11][epoch-runtime][future][claim][allocation]"
          "[intentional-red]")
{
    EpochStore store(membership());
    const auto &epoch0 = store.stage(baseline_input(), baseline_context());
    ReplicaEpochActivation activation(store, epoch0, 0, 0);
    const auto initial_configuration = configuration(epoch0, 0);
    FutureProposalBuffer future;
    ProposalContextLifecycle contexts;
    contexts.activate_configuration(initial_configuration);
    FixedProposalEffects effects;
    ProposalAdmissionCoordinator admission(
        store, initial_configuration, future, effects);
    HotStuffRetryableFutureProposalStore concrete(future, admission);
    FailOnceAfterConcreteClaimStore retryable(concrete);
    HotStuffEpochRuntimeTransaction transaction(admission, contexts);
    FixedConsensusBodyValidator validator;
    HotStuffEpochRuntimeAdapter adapter(
        activation,
        contexts,
        admission,
        retryable,
        validator,
        EpochProtocolMode::adaptive_v1,
        wire_limits(),
        transaction);

    const auto stage = stage_message(epoch0, successor_input(epoch0));
    const auto staged = adapter.handle_stage(
        MsgStageEpochDefinition(stage, wire_limits()),
        AuthenticatedEpochPeer::manager(),
        successor_context());
    REQUIRE(staged.error == EpochIngressError::none);
    REQUIRE(staged.acknowledgement.has_value());
    const auto *const epoch1 = store.find_epoch(1);
    REQUIRE(epoch1 != nullptr);
    const auto future_configuration = configuration(*epoch1, 0);
    const auto future_generation = reserved_nonzero_generation(1, 0);
    REQUIRE(future_generation.has_value());
    const auto leader =
        epoch1->trees().front().members_breadth_first.front();
    const auto envelope = consensus_envelope(
        future_configuration,
        *future_generation,
        "concrete-first-claim-reserve",
        leader);

    const auto buffered = adapter.handle_proposal(
        proposal_message(envelope),
        AuthenticatedEpochPeer::replica(4));
    REQUIRE(buffered.error == EpochIngressError::none);
    REQUIRE(buffered.admission_disposition ==
            ProposalDisposition::buffered_future);
    REQUIRE(future.size() == 1);
    REQUIRE(effects.process_count == 0);

    REQUIRE(adapter.handle_arm(
                MsgArmActivation(
                    arm_for(stage.activation), wire_limits()),
                AuthenticatedEpochPeer::manager())
                .disposition == ReplicaArmDisposition::armed);
    REQUIRE(adapter.on_predecessor_commit(
                kActivationHeight, epoch0.epoch_digest())
                .transition == ActivationTransition::activated);
    REQUIRE(admission.active_configuration() == future_configuration);

    const auto failed = adapter.drain_activated_futures();
    const auto injection_fired =
        d11_allocation_guard::next_rejection_triggered();
    CHECK((failed.status == EpochFutureDrainStatus::allocation_failed ||
           failed.status == EpochFutureDrainStatus::process_failed));
    CHECK(failed.processed == 0);
    CHECK(failed.remaining == 1);
    CHECK(future.size() == 1);
    CHECK(effects.process_count == 0);
    if (!injection_fired)
        d11_allocation_guard::clear_pending_rejection();

    auto recovered_claim = concrete.claim_next(future_configuration);
    REQUIRE(recovered_claim.has_value());
    CHECK(recovered_claim->proposal.metadata.key() == envelope.key());
    concrete.release(recovered_claim->token);

    const auto recovered = adapter.drain_activated_futures();
    CHECK(recovered.status == EpochFutureDrainStatus::complete);
    CHECK(recovered.processed == 1);
    CHECK(recovered.remaining == 0);
    CHECK(future.size() == 0);
    REQUIRE(effects.process_count == 1);
    CHECK(effects.processed[0] == envelope.key());

    const auto idempotent = adapter.drain_activated_futures();
    CHECK(idempotent.status == EpochFutureDrainStatus::complete);
    CHECK(idempotent.processed == 0);
    CHECK(effects.process_count == 1);
}

TEST_CASE("MsgPropose generation survives inert buffer and activation drain",
          "[rem-d11][epoch-runtime][msg-propose][buffer][drain]"
          "[intentional-red]")
{
    RuntimeHarness harness;
    const auto stage = harness.successor();
    REQUIRE(harness.stage(stage).acknowledgement.has_value());
    const auto *epoch1 = harness.store.find_epoch(1);
    REQUIRE(epoch1 != nullptr);
    const auto future_configuration = configuration(*epoch1, 0);
    const auto generation = reserved_nonzero_generation(1, 0);
    REQUIRE(generation.has_value());
    const auto envelope = consensus_envelope(
        future_configuration,
        *generation,
        "wire-buffer-drain",
        epoch1->trees().front().members_breadth_first.front(),
        EpochConsensusWireKind::proposal,
        bytearray_t{0xD1, 0x1B, 0x02});
    const auto encoded = hotstuff::encode_epoch_consensus_envelope(
        envelope, wire_limits());
    auto message = proposal_message(envelope);
    CHECK(static_cast<bytearray_t>(message.serialized) == encoded);

    const auto ingress = harness.transport.dispatch_proposal(
        std::move(message), AuthenticatedEpochPeer::replica(4));
    CHECK(ingress.error == EpochIngressError::none);
    CHECK(ingress.permission == EpochConsensusPermission::admit_or_buffer);
    CHECK(ingress.admission_disposition ==
          ProposalDisposition::buffered_future);
    CHECK(harness.body_validator.proposal_calls == 1);
    REQUIRE(harness.proposal_effects.relay_count == 1);
    CHECK(harness.proposal_effects.process_count == 0);
    CHECK(harness.retryable_future.size() == 1);

    const auto wire_digest = hotstuff::DataStream(encoded).get_hash();
    const auto buffered =
        harness.adapter->buffered_proposal_identity(envelope.key());
    REQUIRE(buffered.has_value());
    CHECK(buffered->key == envelope.key());
    CHECK(buffered->view_generation == *generation);
    CHECK(buffered->wire_digest == wire_digest);

    REQUIRE(harness.arm(arm_for(stage.activation)).disposition ==
            ReplicaArmDisposition::armed);
    const auto activated = harness.transport.dispatch_commit(
        kActivationHeight, harness.epoch0->epoch_digest());
    REQUIRE(activated.transition == ActivationTransition::activated);
    const auto still_buffered =
        harness.adapter->buffered_proposal_identity(envelope.key());
    REQUIRE(still_buffered.has_value());
    CHECK(still_buffered->key == buffered->key);
    CHECK(still_buffered->view_generation == buffered->view_generation);
    CHECK(still_buffered->wire_digest == buffered->wire_digest);

    const auto drained = harness.adapter->drain_activated_futures();
    CHECK(drained.status == EpochFutureDrainStatus::complete);
    CHECK(drained.processed == 1);
    CHECK(drained.remaining == 0);
    CHECK_FALSE(
        harness.adapter->buffered_proposal_identity(envelope.key())
            .has_value());
    const auto processed =
        harness.adapter->processed_proposal_identity(envelope.key());
    REQUIRE(processed.has_value());
    CHECK(processed->key == envelope.key());
    CHECK(processed->view_generation == *generation);
    CHECK(processed->wire_digest == wire_digest);
    CHECK(harness.proposal_effects.relay_count == 1);
    REQUIRE(harness.retryable_future.completed_count == 1);
    CHECK(harness.retryable_future.completed[0] == envelope.key());
    CHECK_FALSE(harness.retryable_future.invariant_failed);
}

TEST_CASE("adapter and real L07 order A-B-A and reject stale generations",
          "[rem-d11][epoch-runtime][leader-progress][generation]"
          "[intentional-red]")
{
    RuntimeHarness harness;
    const auto stage = harness.successor();
    REQUIRE(harness.stage(stage).acknowledgement.has_value());
    REQUIRE(harness.arm(arm_for(stage.activation)).disposition ==
            ReplicaArmDisposition::armed);
    const auto activated = harness.transport.dispatch_commit(
        kActivationHeight, harness.epoch0->epoch_digest());
    REQUIRE(activated.transition == ActivationTransition::activated);
    REQUIRE(activated.update.has_value());
    const auto a = activated.update->leader_view;
    const auto epoch_number = stage.activation.successor_epoch_number;
    REQUIRE(reserved_nonzero_generation(epoch_number, 0).has_value());
    CHECK(a.view_generation ==
          *reserved_nonzero_generation(epoch_number, 0));
    CHECK(a.leader_id ==
          stage.definition.trees[0].members_breadth_first.front());

    FixedLeaderScheduler scheduler;
    std::array<LeaderViewId, kFixedSlots> rotations{};
    std::size_t rotation_count = 0;
    LeaderProgressEffects effects;
    effects.rotate_active_view = [&](const LeaderViewId &view) {
        REQUIRE(rotation_count < rotations.size());
        rotations[rotation_count++] = view;
    };
    LeaderProgressMonitor monitor(
        LeaderProgressConfig{
            std::chrono::nanoseconds(2),
            std::chrono::nanoseconds(10),
            std::chrono::nanoseconds(1),
            {LeaderProgressEvent::verified_proposal,
             LeaderProgressEvent::quorum_certificate,
             LeaderProgressEvent::commit}},
        std::move(effects));

    const auto replayed_block = digest("delayed-a-proposal");
    auto first_a_identity = consensus_envelope(
        a.configuration,
        a.view_generation,
        "delayed-a-proposal",
        a.leader_id);
    REQUIRE(first_a_identity.block_hash == replayed_block);
    CHECK(harness.transport.dispatch_proposal(
              proposal_message(first_a_identity),
              AuthenticatedEpochPeer::replica(4))
              .permission == EpochConsensusPermission::admit_or_buffer);

    REQUIRE(monitor.activate(a, scheduler));
    std::optional<EpochRotationResult> rotated_b;
    bool b_allocated = false;
    {
        d11_allocation_guard::RejectAll no_ordinary_new;
        rotated_b.emplace(harness.transport.dispatch_rotation(1));
        b_allocated = no_ordinary_new.triggered();
    }
    CHECK_FALSE(b_allocated);
    REQUIRE(rotated_b.has_value());
    CHECK(rotated_b->error == EpochIngressError::none);
    REQUIRE(rotated_b->update.has_value());
    const auto b = rotated_b->update->leader_view;
    CHECK(b.configuration.tree_id == 1);
    CHECK(b.view_generation ==
          *reserved_nonzero_generation(epoch_number, 1));
    CHECK(b.leader_id ==
          stage.definition.trees[1].members_breadth_first.front());
    REQUIRE(monitor.activate(b, scheduler));

    std::optional<EpochRotationResult> rotated_a_again;
    bool a_again_allocated = false;
    {
        d11_allocation_guard::RejectAll no_ordinary_new;
        rotated_a_again.emplace(harness.transport.dispatch_rotation(0));
        a_again_allocated = no_ordinary_new.triggered();
    }
    CHECK_FALSE(a_again_allocated);
    REQUIRE(rotated_a_again.has_value());
    CHECK(rotated_a_again->error == EpochIngressError::none);
    REQUIRE(rotated_a_again->update.has_value());
    const auto a_again = rotated_a_again->update->leader_view;
    CHECK(a_again.configuration == a.configuration);
    CHECK(a_again.view_generation ==
          *reserved_nonzero_generation(epoch_number, 2));
    CHECK(a_again.view_generation > b.view_generation);
    CHECK(a_again.leader_id ==
          stage.definition.trees[0].members_breadth_first.front());

    auto current_identity = first_a_identity;
    current_identity.configuration = a_again.configuration;
    current_identity.view_generation = a_again.view_generation;
    auto stale_identity = first_a_identity;
    const auto stale_a_bytes = hotstuff::encode_epoch_consensus_envelope(
        stale_identity, wire_limits());
    const auto current_a_bytes = hotstuff::encode_epoch_consensus_envelope(
        current_identity, wire_limits());
    CHECK(current_identity.view_generation > stale_identity.view_generation);
    CHECK(stale_a_bytes != current_a_bytes);

    INFO("A-B-A requires generation-bearing proposal/control identity; "
         "do not remove the stale-message boundary to make this pass");
    REQUIRE(monitor.activate(a_again, scheduler));
    REQUIRE(monitor.active_view() == a_again);
    REQUIRE(scheduler.scheduled == 3);

    const auto delayed_a = hotstuff::decode_epoch_consensus_envelope(
        stale_a_bytes,
        EpochConsensusWireKind::proposal,
        EpochProtocolMode::adaptive_v1,
        wire_limits());
    REQUIRE(delayed_a);
    REQUIRE(delayed_a.value.has_value());
    CHECK(delayed_a.value->configuration == a.configuration);
    CHECK(delayed_a.value->block_hash == current_identity.block_hash);
    CHECK(delayed_a.value->view_generation == a.view_generation);
    CHECK(delayed_a.value->view_generation != a_again.view_generation);
    CHECK(harness.transport.dispatch_proposal(
              MsgPropose(hotstuff::DataStream(current_a_bytes)),
              AuthenticatedEpochPeer::replica(4))
              .permission == EpochConsensusPermission::admit_or_buffer);
    const auto stale_ingress = harness.transport.dispatch_proposal(
        MsgPropose(hotstuff::DataStream(stale_a_bytes)),
        AuthenticatedEpochPeer::replica(4));
    CHECK(stale_ingress.error == EpochIngressError::state_rejected);
    CHECK(stale_ingress.permission ==
          EpochConsensusPermission::rejected_identity);

    auto current_vote = current_identity;
    current_vote.kind = EpochConsensusWireKind::vote;
    current_vote.originator = 4;
    current_vote.body = {0xD1, 0x56};
    auto stale_vote = current_vote;
    stale_vote.view_generation = a.view_generation;
    const auto current_vote_bytes =
        hotstuff::encode_epoch_consensus_envelope(
            current_vote, wire_limits());
    const auto stale_vote_bytes =
        hotstuff::encode_epoch_consensus_envelope(
            stale_vote, wire_limits());
    CHECK(current_vote_bytes != stale_vote_bytes);
    auto current_vote_message = vote_message(current_vote);
    CHECK(static_cast<bytearray_t>(current_vote_message.serialized) ==
          current_vote_bytes);
    const auto accepted_vote = harness.transport.dispatch_vote(
        std::move(current_vote_message),
        AuthenticatedEpochPeer::replica(4));
    CHECK(accepted_vote.error == EpochIngressError::none);
    CHECK(accepted_vote.permission ==
          EpochConsensusPermission::accept_contribution);
    CHECK(harness.body_validator.vote_calls == 1);
    const auto vote_calls_before_stale = harness.body_validator.vote_calls;
    const auto rejected_vote = harness.transport.dispatch_vote(
        MsgVote(hotstuff::DataStream(stale_vote_bytes)),
        AuthenticatedEpochPeer::replica(4));
    CHECK(rejected_vote.error == EpochIngressError::state_rejected);
    CHECK(rejected_vote.permission ==
          EpochConsensusPermission::rejected_identity);
    CHECK(harness.body_validator.vote_calls == vote_calls_before_stale);

    auto current_relay = current_identity;
    current_relay.kind = EpochConsensusWireKind::relay;
    current_relay.originator = 4;
    current_relay.body = {0xD1, 0xA4};
    auto stale_relay = current_relay;
    stale_relay.view_generation = a.view_generation;
    const auto current_relay_bytes =
        hotstuff::encode_epoch_consensus_envelope(
            current_relay, wire_limits());
    const auto stale_relay_bytes =
        hotstuff::encode_epoch_consensus_envelope(
            stale_relay, wire_limits());
    CHECK(current_relay_bytes != stale_relay_bytes);
    auto current_relay_message = relay_message(current_relay);
    CHECK(static_cast<bytearray_t>(current_relay_message.serialized) ==
          current_relay_bytes);
    const auto accepted_relay = harness.transport.dispatch_relay(
        std::move(current_relay_message),
        AuthenticatedEpochPeer::replica(4));
    CHECK(accepted_relay.error == EpochIngressError::none);
    CHECK(accepted_relay.permission ==
          EpochConsensusPermission::accept_contribution);
    CHECK(harness.body_validator.relay_calls == 1);
    const auto relay_calls_before_stale = harness.body_validator.relay_calls;
    const auto rejected_relay = harness.transport.dispatch_relay(
        MsgRelay(hotstuff::DataStream(stale_relay_bytes)),
        AuthenticatedEpochPeer::replica(4));
    CHECK(rejected_relay.error == EpochIngressError::state_rejected);
    CHECK(rejected_relay.permission ==
          EpochConsensusPermission::rejected_identity);
    CHECK(harness.body_validator.relay_calls == relay_calls_before_stale);

    CHECK(harness.transaction.rotation_count == 2);
    CHECK(harness.transaction.tree_configuration == a_again.configuration);
    CHECK(harness.transaction.context_configuration ==
          a_again.configuration);
    CHECK(harness.transaction.admission_configuration ==
          a_again.configuration);
    CHECK(harness.transaction.leader_deadline_configuration ==
          a_again.configuration);
    CHECK(harness.transaction.leader_view == a_again);
    CHECK_FALSE(harness.transaction.invariant_failed);

    const auto before_stale = scheduler.scheduled;
    CHECK_FALSE(monitor.record_verified_progress(
        a, LeaderProgressEvent::commit, scheduler));
    scheduler.fire_even_if_cancelled(0);
    scheduler.fire_even_if_cancelled(1);
    CHECK(monitor.active_view() == a_again);
    CHECK(scheduler.scheduled == before_stale);
    CHECK(rotation_count == 0);

    scheduler.fire_even_if_cancelled(2);
    REQUIRE(monitor.active_view() == a_again);
    const auto after_current_grace = scheduler.scheduled;
    REQUIRE(after_current_grace > 0);
    const auto superseded_current_timeout = after_current_grace - 1;
    REQUIRE(monitor.record_verified_progress(
        a_again, LeaderProgressEvent::commit, scheduler));
    CHECK(scheduler.scheduled == after_current_grace + 1);
    scheduler.fire_even_if_cancelled(superseded_current_timeout);
    CHECK(monitor.active_view() == a_again);
    CHECK(rotation_count == 0);

    const auto after_current_reset = scheduler.scheduled;
    CHECK_FALSE(monitor.record_verified_progress(
        a, LeaderProgressEvent::verified_proposal, scheduler));
    CHECK(scheduler.scheduled == after_current_reset);
    CHECK(monitor.active_view()->view_generation ==
          current_identity.view_generation);
}

TEST_CASE("activated staging whitelist cannot revive an old tree generation",
          "[rem-d11][epoch-runtime][generation][staged-whitelist]"
          "[intentional-red]")
{
    RuntimeHarness harness;
    const auto stage = harness.successor();
    REQUIRE(harness.stage(stage).acknowledgement.has_value());
    REQUIRE(harness.arm(arm_for(stage.activation)).disposition ==
            ReplicaArmDisposition::armed);
    const auto activated = harness.transport.dispatch_commit(
        kActivationHeight, harness.epoch0->epoch_digest());
    REQUIRE(activated.transition == ActivationTransition::activated);
    REQUIRE(activated.update.has_value());
    const auto retired_view = activated.update->leader_view;

    const auto rotated = harness.transport.dispatch_rotation(1);
    REQUIRE(rotated.error == EpochIngressError::none);
    REQUIRE(rotated.update.has_value());
    REQUIRE(rotated.update->leader_view.configuration !=
            retired_view.configuration);

    const auto relay_before = harness.proposal_effects.relay_count;
    const auto process_before = harness.proposal_effects.process_count;
    const auto buffered_before = harness.retryable_future.size();
    const auto validation_before = harness.body_validator.proposal_calls;
    const auto stale = consensus_envelope(
        retired_view.configuration,
        retired_view.view_generation,
        "retired-staged-ordinal-zero",
        retired_view.leader_id);

    const auto rejected = harness.transport.dispatch_proposal(
        proposal_message(stale), AuthenticatedEpochPeer::replica(4));
    CHECK(rejected.error == EpochIngressError::state_rejected);
    CHECK(rejected.permission ==
          EpochConsensusPermission::rejected_identity);
    CHECK(harness.body_validator.proposal_calls == validation_before);
    CHECK(harness.proposal_effects.relay_count == relay_before);
    CHECK(harness.proposal_effects.process_count == process_before);
    CHECK(harness.retryable_future.size() == buffered_before);
    CHECK_FALSE(harness.retryable_future.contains(stale.key()));
}

TEST_CASE("concrete runtime activation advances admission without draining",
          "[rem-d11][epoch-runtime][real-wiring][transaction][drain]"
          "[intentional-red]")
{
    EpochStore store(membership());
    const auto &epoch0 = store.stage(baseline_input(), baseline_context());
    ReplicaEpochActivation activation(store, epoch0, 0, 0);
    const auto initial_configuration = configuration(epoch0, 0);
    FutureProposalBuffer future;
    ProposalContextLifecycle contexts;
    contexts.activate_configuration(initial_configuration);
    RealWiringProposalEffects effects;
    ProposalAdmissionCoordinator admission(
        store, initial_configuration, future, effects);
    HotStuffRetryableFutureProposalStore retryable(future, admission);
    HotStuffEpochRuntimeTransaction transaction(admission, contexts);
    FixedConsensusBodyValidator validator;
    HotStuffEpochRuntimeAdapter adapter(
        activation,
        contexts,
        admission,
        retryable,
        validator,
        EpochProtocolMode::adaptive_v1,
        wire_limits(),
        transaction);

    const auto stage = stage_message(epoch0, successor_input(epoch0));
    const auto staged = adapter.handle_stage(
        MsgStageEpochDefinition(stage, wire_limits()),
        AuthenticatedEpochPeer::manager(),
        successor_context());
    REQUIRE(staged.error == EpochIngressError::none);
    REQUIRE(staged.acknowledgement.has_value());
    const auto *const epoch1 = store.find_epoch(1);
    REQUIRE(epoch1 != nullptr);
    const auto active_after_commit = configuration(*epoch1, 0);
    const auto generation = reserved_nonzero_generation(1, 0);
    REQUIRE(generation.has_value());
    const auto leader = epoch1->trees().front().members_breadth_first.front();

    BlsTestCore structural_core(membership().size());
    const auto source_peer = structural_core.get_config().get_peer_id(leader);
    const auto authenticated = hotstuff::authenticated_epoch_replica(
        leader, source_peer);
    const auto future_block = actual_proposal_block(structural_core, 0x31);
    const auto future_body = actual_proposal_body(
        active_after_commit, leader, future_block);
    const auto future_envelope = consensus_envelope(
        active_after_commit,
        *generation,
        "real-wiring-future",
        leader,
        EpochConsensusWireKind::proposal,
        future_body);
    auto future_with_real_hash = future_envelope;
    future_with_real_hash.block_hash = future_block->get_hash();
    const auto future_outer = hotstuff::encode_epoch_consensus_envelope(
        future_with_real_hash, wire_limits());
    const auto buffered = adapter.handle_proposal(
        proposal_message(future_with_real_hash), authenticated);
    REQUIRE(buffered.error == EpochIngressError::none);
    CHECK(buffered.admission_disposition ==
          ProposalDisposition::buffered_future);
    CHECK(effects.relay_count == 1);
    CHECK(effects.process_count == 0);
    CHECK(future.contains(future_with_real_hash.key()));
    CHECK(future.size() == 1);
    CHECK(retryable.size() == 1);

    REQUIRE(adapter.handle_arm(
                MsgArmActivation(
                    arm_for(stage.activation), wire_limits()),
                AuthenticatedEpochPeer::manager())
                .disposition == ReplicaArmDisposition::armed);
    std::optional<EpochCommitIngressResult> committed;
    bool exact_height_allocated = false;
    {
        d11_allocation_guard::RejectAll no_ordinary_new;
        committed.emplace(adapter.on_predecessor_commit(
            kActivationHeight, epoch0.epoch_digest()));
        exact_height_allocated = no_ordinary_new.triggered();
    }
    CHECK_FALSE(exact_height_allocated);
    REQUIRE(committed.has_value());
    REQUIRE(committed->transition == ActivationTransition::activated);
    CHECK(admission.active_configuration() == active_after_commit);
    CHECK(contexts.active_configuration() == active_after_commit);
    CHECK(future.contains(future_with_real_hash.key()));
    CHECK(future.size() == 1);
    CHECK(retryable.size() == 1);
    CHECK(effects.process_count == 0);

    const auto direct_block = actual_proposal_block(structural_core, 0x32);
    const auto direct_body = actual_proposal_body(
        active_after_commit, leader, direct_block);
    auto direct_envelope = consensus_envelope(
        active_after_commit,
        *generation,
        "real-wiring-direct",
        leader,
        EpochConsensusWireKind::proposal,
        direct_body);
    direct_envelope.block_hash = direct_block->get_hash();
    const auto direct = adapter.handle_proposal(
        proposal_message(direct_envelope), authenticated);
    REQUIRE(direct.error == EpochIngressError::none);
    CHECK(direct.admission_disposition ==
          ProposalDisposition::admitted_active);
    CHECK(effects.relay_count == 2);
    CHECK(effects.process_count == 1);
    CHECK(effects.processed[0] == direct_envelope.key());
    CHECK(effects.processed_inner_wire[0] == direct_body);
    CHECK(effects.processed_source[0] == source_peer);
    CHECK_FALSE(future.contains(direct_envelope.key()));
    CHECK(future.size() == 1);
    CHECK(retryable.size() == 1);

    const auto drained = adapter.drain_activated_futures();
    CHECK(drained.status == EpochFutureDrainStatus::complete);
    CHECK(drained.processed == 1);
    CHECK(drained.remaining == 0);
    CHECK(effects.process_count == 2);
    CHECK(effects.processed[1] == future_with_real_hash.key());
    CHECK(effects.processed_inner_wire[1] == future_body);
    CHECK(effects.processed_source[1] == source_peer);
    REQUIRE(effects.relay_count == 2);
    CHECK(effects.relayed[0] == future_with_real_hash.key());
    CHECK(effects.relayed[1] == direct_envelope.key());
    CHECK(effects.relayed_outer_wire[0] == future_outer);
    CHECK(effects.relayed_source[0] == source_peer);
    CHECK(future.size() == 0);
    CHECK(retryable.size() == 0);
}

TEST_CASE("concrete proposal body validation binds both representations and source",
          "[rem-d11][epoch-runtime][real-wiring][proposal-body]"
          "[intentional-red]")
{
    EpochStore store(membership());
    const auto &epoch0 = store.stage(baseline_input(), baseline_context());
    ReplicaEpochActivation activation(store, epoch0, 0, 0);
    const auto active_configuration = configuration(epoch0, 0);
    FutureProposalBuffer future;
    ProposalContextLifecycle contexts;
    contexts.activate_configuration(active_configuration);
    RealWiringProposalEffects effects;
    ProposalAdmissionCoordinator admission(
        store, active_configuration, future, effects);
    HotStuffRetryableFutureProposalStore retryable(future, admission);
    FixedPreparedRuntime transaction;
    transaction.contexts = &contexts;
    transaction.tree_configuration = active_configuration;
    transaction.context_configuration = active_configuration;
    transaction.admission_configuration = active_configuration;

    BlsTestCore structural_core(membership().size());
    HotStuffEpochConsensusBodyValidator validator(structural_core);
    HotStuffEpochRuntimeAdapter adapter(
        activation,
        contexts,
        admission,
        retryable,
        validator,
        EpochProtocolMode::adaptive_v1,
        wire_limits(),
        transaction);

    const auto active = activation.active_effect();
    REQUIRE(active.configuration == active_configuration);
    REQUIRE(active.generation != 0);
    const auto proposer =
        epoch0.trees().front().members_breadth_first.front();
    const auto source_peer =
        structural_core.get_config().get_peer_id(proposer);
    const auto authenticated = hotstuff::authenticated_epoch_replica(
        proposer, source_peer);
    const auto block = actual_proposal_block(structural_core, 0x41);
    const auto body = actual_proposal_body(
        active_configuration, proposer, block);
    Proposal native_proposal(
        proposer,
        active_configuration.epoch_number,
        active_configuration.tree_id,
        active_configuration.epoch_digest,
        block,
        nullptr);
    MsgPropose native_message(native_proposal);
    CHECK(static_cast<bytearray_t>(native_message.serialized) == body);

    auto exact = consensus_envelope(
        active_configuration,
        active.generation,
        "real-body-exact",
        proposer,
        EpochConsensusWireKind::proposal,
        body);
    exact.block_hash = block->get_hash();
    const auto exact_outer = hotstuff::encode_epoch_consensus_envelope(
        exact, wire_limits());
    CHECK(exact_outer != body);

    const auto reject_without_protocol_effects = [
        &](const EpochConsensusEnvelope &candidate,
            const AuthenticatedEpochPeer &peer) {
        const auto relay_before = effects.relay_count;
        const auto process_before = effects.process_count;
        const auto future_before = future.size();
        const auto retryable_before = retryable.size();
        const auto rejected = adapter.handle_proposal(
            proposal_message(candidate), peer);
        CHECK(rejected.error == EpochIngressError::wire_rejected);
        CHECK(rejected.wire_error ==
              EpochConsensusWireError::invalid_body);
        CHECK(rejected.permission ==
              EpochConsensusPermission::rejected_identity);
        CHECK(effects.relay_count == relay_before);
        CHECK(effects.process_count == process_before);
        CHECK(future.size() == future_before);
        CHECK(retryable.size() == retryable_before);
    };

    auto inner_hash_mismatch = exact;
    inner_hash_mismatch.body = actual_proposal_body_with_metadata(
        active_configuration,
        digest("real-body-wrong-inner-hash"),
        proposer,
        block);
    reject_without_protocol_effects(inner_hash_mismatch, authenticated);

    auto outer_hash_mismatch = exact;
    outer_hash_mismatch.block_hash =
        digest("real-body-wrong-outer-hash");
    reject_without_protocol_effects(outer_hash_mismatch, authenticated);

    const ReplicaID other_replica = proposer == 0 ? 1 : 0;
    auto proposer_mismatch = exact;
    proposer_mismatch.body = actual_proposal_body_with_metadata(
        active_configuration,
        block->get_hash(),
        other_replica,
        block);
    reject_without_protocol_effects(proposer_mismatch, authenticated);

    auto configuration_mismatch = exact;
    configuration_mismatch.body = actual_proposal_body_with_metadata(
        configuration(epoch0, 1),
        block->get_hash(),
        proposer,
        block);
    reject_without_protocol_effects(configuration_mismatch, authenticated);

    const auto forged_authenticated = hotstuff::authenticated_epoch_replica(
        other_replica, source_peer);
    reject_without_protocol_effects(exact, forged_authenticated);

    const auto accepted = adapter.handle_proposal(
        proposal_message(exact), authenticated);
    REQUIRE(accepted.error == EpochIngressError::none);
    CHECK(accepted.permission ==
          EpochConsensusPermission::admit_or_buffer);
    CHECK(accepted.admission_disposition ==
          ProposalDisposition::admitted_active);
    CHECK(effects.relay_count == 1);
    CHECK(effects.process_count == 1);
    CHECK(effects.relayed_outer_wire[0] == exact_outer);
    CHECK(effects.processed_inner_wire[0] == body);
    CHECK(effects.relayed_source[0] == source_peer);
    CHECK(effects.processed_source[0] == source_peer);
    CHECK(future.size() == 0);
    CHECK(retryable.size() == 0);

    const auto future_stage = stage_message(
        epoch0, successor_input(epoch0));
    const auto staged = adapter.handle_stage(
        MsgStageEpochDefinition(future_stage, wire_limits()),
        AuthenticatedEpochPeer::manager(),
        successor_context());
    REQUIRE(staged.error == EpochIngressError::none);
    const auto *const epoch1 = store.find_epoch(1);
    REQUIRE(epoch1 != nullptr);
    const auto future_configuration = configuration(*epoch1, 0);
    const auto future_generation = reserved_nonzero_generation(1, 0);
    REQUIRE(future_generation.has_value());
    const auto future_proposer =
        epoch1->trees().front().members_breadth_first.front();
    const auto future_source_peer =
        structural_core.get_config().get_peer_id(future_proposer);
    const auto future_authenticated =
        hotstuff::authenticated_epoch_replica(
            future_proposer, future_source_peer);
    const auto future_block = actual_proposal_block(structural_core, 0x42);
    const auto future_body = actual_proposal_body(
        future_configuration, future_proposer, future_block);
    auto future_envelope = consensus_envelope(
        future_configuration,
        *future_generation,
        "real-body-buffered",
        future_proposer,
        EpochConsensusWireKind::proposal,
        future_body);
    future_envelope.block_hash = future_block->get_hash();
    const auto future_outer = hotstuff::encode_epoch_consensus_envelope(
        future_envelope, wire_limits());

    const auto buffered = adapter.handle_proposal(
        proposal_message(future_envelope), future_authenticated);
    REQUIRE(buffered.error == EpochIngressError::none);
    CHECK(buffered.admission_disposition ==
          ProposalDisposition::buffered_future);
    CHECK(effects.relay_count == 2);
    CHECK(effects.process_count == 1);
    CHECK(future.size() == 1);
    CHECK(retryable.size() == 1);

    auto claim = retryable.claim_next(future_configuration);
    REQUIRE(claim.has_value());
    CHECK(claim->proposal.wire_payload == future_outer);
    CHECK(hotstuff::hotstuff_epoch_processing_payload(
              claim->proposal) == future_body);
    CHECK(claim->proposal.source_peer == future_source_peer);
    retryable.release(claim->token);
    CHECK(retryable.size() == 1);
}

TEST_CASE("multi-hop proposal authenticates its parent without rewriting root",
          "[rem-d11][epoch-runtime][real-wiring][proposal-relay]"
          "[intentional-red]")
{
    constexpr ReplicaID local_replica = 3;
    constexpr ReplicaID root_proposer = 0;
    constexpr ReplicaID immediate_parent = 1;

    EpochStore store(membership());
    const auto &epoch0 = store.stage(baseline_input(), baseline_context());
    ReplicaEpochActivation activation(
        store, epoch0, local_replica, 0);
    const auto active_configuration = configuration(epoch0, 0);
    FutureProposalBuffer future;
    ProposalContextLifecycle contexts;
    contexts.activate_configuration(active_configuration);
    RealWiringProposalEffects effects;
    ProposalAdmissionCoordinator admission(
        store, active_configuration, future, effects);
    HotStuffRetryableFutureProposalStore retryable(future, admission);
    FixedPreparedRuntime transaction;
    transaction.contexts = &contexts;
    transaction.tree_configuration = active_configuration;
    transaction.context_configuration = active_configuration;
    transaction.admission_configuration = active_configuration;

    BlsTestCore structural_core(membership().size(), local_replica);
    constexpr bool has_exact_tree_authorization =
        std::is_constructible<
            HotStuffEpochConsensusBodyValidator,
            hotstuff::HotStuffCore &,
            const EpochStore &>::value;
    INFO("proposal relay authorization must resolve the exact active tree");
    CHECK(has_exact_tree_authorization);
    auto validator = exact_tree_body_validator<
        HotStuffEpochConsensusBodyValidator>(structural_core, store);
    HotStuffEpochRuntimeAdapter adapter(
        activation,
        contexts,
        admission,
        retryable,
        *validator,
        EpochProtocolMode::adaptive_v1,
        wire_limits(),
        transaction);

    const auto active = activation.active_effect();
    REQUIRE(active.configuration == active_configuration);
    REQUIRE(epoch0.trees().front().members_breadth_first.front() ==
            root_proposer);
    REQUIRE(epoch0.trees().front().members_breadth_first[1] ==
            immediate_parent);
    REQUIRE(epoch0.trees().front().members_breadth_first[3] ==
            local_replica);
    const auto parent_peer =
        structural_core.get_config().get_peer_id(immediate_parent);
    const auto authenticated_parent =
        hotstuff::authenticated_epoch_replica(
            immediate_parent, parent_peer);

    const auto block = actual_proposal_block(structural_core, 0x43);
    const auto body = actual_proposal_body(
        active_configuration, root_proposer, block);
    auto relayed = consensus_envelope(
        active_configuration,
        active.generation,
        "real-body-multi-hop",
        root_proposer,
        EpochConsensusWireKind::proposal,
        body);
    relayed.block_hash = block->get_hash();
    const auto outer = hotstuff::encode_epoch_consensus_envelope(
        relayed, wire_limits());

    const auto accepted = adapter.handle_proposal(
        proposal_message(relayed), authenticated_parent);
    REQUIRE(accepted.error == EpochIngressError::none);
    CHECK(accepted.permission ==
          EpochConsensusPermission::admit_or_buffer);
    CHECK(accepted.admission_disposition ==
          ProposalDisposition::admitted_active);
    REQUIRE(effects.relay_count == 1);
    REQUIRE(effects.process_count == 1);
    CHECK(effects.relayed[0] == relayed.key());
    CHECK(effects.processed[0] == relayed.key());
    CHECK(effects.relayed_outer_wire[0] == outer);
    CHECK(effects.processed_inner_wire[0] == body);
    CHECK(effects.relayed_source[0] == parent_peer);
    CHECK(effects.processed_source[0] == parent_peer);

    const auto root_peer =
        structural_core.get_config().get_peer_id(root_proposer);
    const auto authenticated_root =
        hotstuff::authenticated_epoch_replica(
            root_proposer, root_peer);
    const auto fallback_block = actual_proposal_block(
        structural_core, 0x44);
    const auto fallback_body = actual_proposal_body(
        active_configuration, root_proposer, fallback_block);
    auto root_fallback = consensus_envelope(
        active_configuration,
        active.generation,
        "real-body-root-fallback",
        root_proposer,
        EpochConsensusWireKind::proposal,
        fallback_body);
    root_fallback.block_hash = fallback_block->get_hash();
    const auto fallback_accepted = adapter.handle_proposal(
        proposal_message(root_fallback), authenticated_root);
    REQUIRE(fallback_accepted.error == EpochIngressError::none);
    CHECK(fallback_accepted.admission_disposition ==
          ProposalDisposition::admitted_active);
    REQUIRE(effects.relay_count == 2);
    REQUIRE(effects.process_count == 2);
    CHECK(effects.relayed[1] == root_fallback.key());
    CHECK(effects.processed[1] == root_fallback.key());
    CHECK(effects.relayed_source[1] == root_peer);
    CHECK(effects.processed_source[1] == root_peer);

    const auto reject_without_protocol_effects = [
        &](const EpochConsensusEnvelope &candidate,
            const AuthenticatedEpochPeer &peer) {
        const auto relay_before = effects.relay_count;
        const auto process_before = effects.process_count;
        const auto future_before = future.size();
        const auto rejected = adapter.handle_proposal(
            proposal_message(candidate), peer);
        CHECK(rejected.error == EpochIngressError::wire_rejected);
        CHECK(rejected.wire_error ==
              EpochConsensusWireError::invalid_body);
        CHECK(rejected.permission ==
              EpochConsensusPermission::rejected_identity);
        CHECK(effects.relay_count == relay_before);
        CHECK(effects.process_count == process_before);
        CHECK(future.size() == future_before);
    };

    const auto forged_peer = structural_core.get_config().get_peer_id(2);
    reject_without_protocol_effects(
        relayed,
        hotstuff::authenticated_epoch_replica(
            immediate_parent, forged_peer));
    reject_without_protocol_effects(
        relayed,
        hotstuff::authenticated_epoch_replica(2, forged_peer));

    auto rewritten_root = relayed;
    rewritten_root.originator = immediate_parent;
    rewritten_root.proposer = immediate_parent;
    reject_without_protocol_effects(
        rewritten_root, authenticated_parent);

    auto split_root = relayed;
    split_root.proposer = immediate_parent;
    reject_without_protocol_effects(split_root, authenticated_parent);
}

TEST_CASE("concrete handler installer binds exact opcodes and continuations",
          "[rem-d11][epoch-runtime][real-wiring][handler-installer]"
          "[intentional-red]")
{
    RuntimeHarness harness;
    SocketFreeEpochHandlerRegistry registry;
    HotStuffEpochHandlerInstaller::install(registry, *harness.adapter);

    CHECK(MsgStageEpochDefinition::opcode == 0x13);
    CHECK(MsgArmActivation::opcode == 0x15);
    CHECK(MsgPropose::opcode == 0x00);
    CHECK(MsgVote::opcode == 0x01);
    CHECK(MsgRelay::opcode == 0x04);
    CHECK(registry.stage_opcode == MsgStageEpochDefinition::opcode);
    CHECK(registry.arm_opcode == MsgArmActivation::opcode);
    CHECK(registry.proposal_opcode == MsgPropose::opcode);
    CHECK(registry.vote_opcode == MsgVote::opcode);
    CHECK(registry.relay_opcode == MsgRelay::opcode);
    CHECK(registry.stage_registration_count == 1);
    CHECK(registry.arm_registration_count == 1);
    CHECK(registry.proposal_registration_count == 1);
    CHECK(registry.vote_registration_count == 1);
    CHECK(registry.relay_registration_count == 1);

    const auto staged_definition = harness.successor();
    const auto staged = registry.dispatch_stage(
        MsgStageEpochDefinition(staged_definition, wire_limits()),
        AuthenticatedEpochPeer::manager(),
        successor_context());
    REQUIRE(staged.error == EpochIngressError::none);
    REQUIRE(staged.disposition.has_value());
    CHECK(*staged.disposition == ReplicaStageDisposition::staged);

    const auto armed = registry.dispatch_arm(
        MsgArmActivation(
            arm_for(staged_definition.activation), wire_limits()),
        AuthenticatedEpochPeer::manager());
    REQUIRE(armed.error == EpochIngressError::none);
    REQUIRE(armed.disposition.has_value());
    CHECK(*armed.disposition == ReplicaArmDisposition::armed);

    const auto active = harness.activation->active_effect();
    const auto proposer =
        harness.epoch0->trees().front().members_breadth_first.front();
    const auto proposal = consensus_envelope(
        active.configuration,
        active.generation,
        "installer-proposal",
        proposer);
    const auto proposal_result = registry.dispatch_proposal(
        proposal_message(proposal),
        AuthenticatedEpochPeer::replica(proposer));
    REQUIRE(proposal_result.error == EpochIngressError::none);
    CHECK(proposal_result.admission_disposition ==
          ProposalDisposition::admitted_active);

    auto vote = proposal;
    vote.kind = EpochConsensusWireKind::vote;
    vote.originator = 4;
    vote.body = {0xD1, 0x51};
    const auto vote_result =
        registry.dispatch_vote_to_existing_continuation(
            vote_message(vote), AuthenticatedEpochPeer::replica(4));
    REQUIRE(vote_result.error == EpochIngressError::none);
    CHECK(vote_result.permission ==
          EpochConsensusPermission::accept_contribution);

    auto relay = proposal;
    relay.kind = EpochConsensusWireKind::relay;
    relay.originator = 5;
    relay.body = {0xD1, 0x52};
    const auto relay_result =
        registry.dispatch_relay_to_existing_continuation(
            relay_message(relay), AuthenticatedEpochPeer::replica(5));
    REQUIRE(relay_result.error == EpochIngressError::none);
    CHECK(relay_result.permission ==
          EpochConsensusPermission::accept_contribution);

    CHECK(registry.stage_dispatch_count == 1);
    CHECK(registry.arm_dispatch_count == 1);
    CHECK(registry.proposal_dispatch_count == 1);
    CHECK(registry.vote_dispatch_count == 1);
    CHECK(registry.relay_dispatch_count == 1);
    CHECK(harness.body_validator.proposal_calls == 1);
    CHECK(harness.body_validator.vote_calls == 1);
    CHECK(harness.body_validator.relay_calls == 1);
    CHECK(registry.vote_continuation_count == 1);
    CHECK(registry.relay_continuation_count == 1);
    CHECK(registry.vote_continuation_body == vote.body);
    CHECK(registry.relay_continuation_body == relay.body);
}

TEST_CASE("D11-LIVE-TLS maps certificates and converges live replicas",
          "[.][d11][epoch-runtime][live-mtls][multi-process][deferred]")
{
    FAIL("Deferred to M12/SMOKE20: prove certificate-to-role mapping, real "
         "manager/replica sockets, and surviving-process convergence. "
         "The executable fake transport does not claim live TLS.");
}
