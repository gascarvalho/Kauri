/**
 * Prepared adaptive-epoch runtime and versioned consensus ingress.
 *
 * Transport authentication is supplied separately from bounded wire bytes.
 * Runtime preparation is fallible; activation and tree rotation consume only
 * preprepared state through nofail transaction callbacks.
 */

#ifndef HOTSTUFF_EPOCH_RUNTIME_H_INCLUDED
#define HOTSTUFF_EPOCH_RUNTIME_H_INCLUDED

#include <cstddef>
#include <cstdint>
#include <memory>
#include <mutex>
#include <optional>
#include <vector>

#include "hotstuff/adaptive_v3_activation_readiness.h"
#include "hotstuff/epoch_activation.h"
#include "hotstuff/future_proposal_buffer.h"
#include "hotstuff/proposal_admission.h"
#include "hotstuff/proposal_context.h"

namespace hotstuff
{

struct MsgPropose;
struct MsgVote;
struct MsgRelay;

enum class EpochPeerRole : std::uint8_t
{
    manager = 1,
    replica,
};

struct AuthenticatedEpochPeer
{
    EpochPeerRole role{EpochPeerRole::manager};
    std::optional<ReplicaID> replica_id;
    PeerId source_peer;

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
    std::uint32_t wire_schema_version{kEpochConsensusWireSchemaVersion};
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
    EpochProtocolMode protocol_mode{EpochProtocolMode::adaptive_v1};
    std::uint32_t epoch_number{0};
    uint256_t epoch_digest;
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
    virtual void rotate(const EpochRuntimeRotation &update) noexcept = 0;
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
    virtual bool process_active(
        const FutureProposalClaim &claim,
        ProposalProcessingCompletion completion)
    {
        process_active(claim);
        if (completion)
            completion(ProposalProcessingOutcome::completed_exposed);
        return true;
    }
    virtual void acknowledge(std::uint64_t token) noexcept = 0;
    virtual void release(std::uint64_t token) noexcept = 0;
    virtual void complete(const ConfigurationId &configuration) noexcept = 0;
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

/**
 * Commit-count cadence for deterministic adaptive-v2 tree rotation.
 *
 * The caller supplies the authoritative commit identity and the exact active
 * configuration after post-commit activation processing. A due cadence stays
 * due until the caller confirms a successful rotation by calling reset().
 */
class AdaptiveV2CommitCadence final
{
public:
    explicit AdaptiveV2CommitCadence(std::size_t period);

    bool observe(
        const std::optional<ProposalKey> &committed_key,
        const ConfigurationId &active_configuration) noexcept;
    void reset() noexcept;
    std::size_t period() const noexcept;
    std::size_t observed_commits() const noexcept;

private:
    std::size_t period_;
    std::size_t observed_commits_{0};
};

enum class AdaptiveV2RotationDisposition : std::uint8_t
{
    not_due = 1,
    rotated,
    stale_view,
    rejected,
};

struct AdaptiveV2RotationResult
{
    AdaptiveV2RotationDisposition disposition{
        AdaptiveV2RotationDisposition::not_due};
    std::optional<EpochRuntimeUpdate> update;
};

class AdaptiveV2RotationEffects
{
public:
    virtual ~AdaptiveV2RotationEffects() = default;

    virtual std::optional<EpochActivationEffect> active_view()
        const noexcept = 0;
    virtual std::optional<std::uint32_t> next_tree_id() const noexcept = 0;
    virtual EpochRotationResult rotate_to_tree(
        std::uint32_t tree_id) noexcept = 0;
};

/**
 * Single serialized owner for adaptive-v2 periodic and timeout rotations.
 * Every trigger carries the exact view it observed. The coordinator compares
 * that view again while holding its lock immediately before selecting and
 * applying the next configured tree.
 */
class AdaptiveV2RotationCoordinator final
{
public:
    AdaptiveV2RotationCoordinator(
        std::size_t period,
        AdaptiveV2RotationEffects &effects);

    AdaptiveV2RotationResult on_commit(
        const std::optional<ProposalKey> &committed_key,
        const ConfigurationId &expected_configuration,
        std::uint64_t expected_generation) noexcept;
    AdaptiveV2RotationResult on_timeout(
        const ConfigurationId &expected_configuration,
        std::uint64_t expected_generation) noexcept;
    void reset_for_activation() noexcept;
    std::size_t period() const noexcept;
    std::size_t observed_commits() const noexcept;

private:
    bool matches_expected_view(
        const ConfigurationId &expected_configuration,
        std::uint64_t expected_generation) const noexcept;
    AdaptiveV2RotationResult compare_and_rotate(
        const ConfigurationId &expected_configuration,
        std::uint64_t expected_generation) noexcept;

    mutable std::mutex mutex_;
    AdaptiveV2CommitCadence cadence_;
    AdaptiveV2RotationEffects &effects_;
};

struct EpochCommitIngressResult
{
    EpochIngressError error{EpochIngressError::none};
    ActivationTransition transition{ActivationTransition::waiting};
    ActivationBlockReason blocked_reason{ActivationBlockReason::none};
    std::optional<EpochRuntimeUpdate> update;
};

struct AdaptiveV3CommitIngressResult
{
    EpochIngressError error{EpochIngressError::none};
    AdaptiveV3BoundaryResult boundary;
    std::optional<EpochRuntimeUpdate> update;
};

struct AdaptiveV3CertificateIngressResult
{
    EpochIngressError error{EpochIngressError::none};
    AdaptiveV3CertificateDisposition disposition{
        AdaptiveV3CertificateDisposition::terminal};
    std::optional<EpochRuntimeUpdate> update;
};

enum class EpochFutureDrainStatus : std::uint8_t
{
    complete = 1,
    in_progress,
    retry_exhausted,
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
    EpochIngressError prepare_committed_v2(
        const EpochDefinition &successor_definition) noexcept;
    EpochIngressError prepare_committed_v3(
        const EpochDefinition &successor_definition) noexcept;
    void fail_committed_v2(ActivationBlockReason reason) noexcept;
    ReplicaArmIngressResult handle_arm(
        MsgArmActivation &&message,
        const AuthenticatedEpochPeer &authenticated_peer);
    EpochCommitIngressResult on_predecessor_commit(
        std::uint64_t height,
        const uint256_t &predecessor_digest) noexcept;
    EpochCommitIngressResult on_v2_post_block_commit(
        std::uint64_t height,
        const uint256_t &predecessor_digest) noexcept;
    AdaptiveV3CommitIngressResult on_v3_post_block_commit(
        AdaptiveV3CertifiedActivationGate &gate,
        std::uint64_t height,
        const ConfigurationId &predecessor_configuration,
        std::uint64_t predecessor_generation,
        const uint256_t &block_hash,
        std::uint64_t source_sequence,
        std::uint64_t monotonic_raw_ns) noexcept;
    AdaptiveV3CertificateIngressResult apply_v3_readiness_certificate(
        AdaptiveV3CertifiedActivationGate &gate,
        const AdaptiveV3ActivationReadinessCertificate &certificate)
        noexcept;
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
    std::shared_ptr<State> state_;
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
