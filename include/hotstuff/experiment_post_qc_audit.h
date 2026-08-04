/**
 * Bounded, experiment-only post-QC audit state.
 *
 * This component is deliberately isolated from consensus, evidence,
 * reputation, adaptation, and manager state.  Runtime integration supplies
 * only certificates that have already passed the normal exact-context
 * verification path, except for the separately verified post-close target
 * vote and independently verified root audit relay.
 */

#ifndef HOTSTUFF_EXPERIMENT_POST_QC_AUDIT_H_INCLUDED
#define HOTSTUFF_EXPERIMENT_POST_QC_AUDIT_H_INCLUDED

#include <cstddef>
#include <cstdint>
#include <memory>
#include <optional>
#include <set>
#include <string>
#include <vector>

#include "hotstuff/configuration.h"
#include "hotstuff/crypto.h"
#include "hotstuff/proposal_context.h"

namespace hotstuff
{

constexpr std::uint64_t kExperimentPostQcAuditDeadlineMs = 150;
constexpr std::uint64_t kExperimentPostQcAuditRetentionMs = 250;
constexpr std::size_t kExperimentPostQcAuditContextLimit = 1;
constexpr std::size_t kExperimentPostQcAuditMaximumWindowBytes = 128;
constexpr std::size_t kExperimentPostQcAuditMaximumWireBytes = 4096;
constexpr std::uint32_t kExperimentPostQcAuditWireSchemaVersion = 1;

class HotStuffCore;

struct ExperimentPostQcAuditOptions final
{
    bool enabled{false};
    ConfigurationId configuration;
    std::string diagnostic_window;
    ReplicaID reporter{0};
    ReplicaID target{0};
    ReplicaID root{0};
    bool forge_missing_claim{false};
    std::uint64_t deadline_ms{0};
    std::uint64_t retention_ms{0};
    std::size_t maximum_contexts{0};
};

enum class ExperimentPostQcAuditTargetPhase
{
    open,
    post_close,
};

struct ExperimentPostQcAuditTargetObservation final
{
    ExperimentPostQcAuditTargetPhase phase{
        ExperimentPostQcAuditTargetPhase::open};
    ProposalKey proposal;
    std::uint64_t generation{0};
    std::uint64_t armed_ns{0};
    std::uint64_t deadline_ns{0};
    std::uint64_t arrival_ns{0};
    std::set<ReplicaID> signers;
};

struct ExperimentPostQcAuditRelay final
{
    ProposalKey proposal;
    std::uint64_t generation{0};
    std::string diagnostic_window;
    ReplicaID reporter{0};
    ReplicaID target{0};
    ReplicaID root{0};
    std::uint64_t armed_ns{0};
    std::uint64_t deadline_ns{0};
    std::uint64_t emitted_ns{0};
    quorum_cert_bt certificate;

    ExperimentPostQcAuditRelay() = default;
    ExperimentPostQcAuditRelay(
        const ExperimentPostQcAuditRelay &other);
    ExperimentPostQcAuditRelay &operator=(
        const ExperimentPostQcAuditRelay &other);
    ExperimentPostQcAuditRelay(
        ExperimentPostQcAuditRelay &&) noexcept = default;
    ExperimentPostQcAuditRelay &operator=(
        ExperimentPostQcAuditRelay &&) noexcept = default;

    void serialize(DataStream &stream) const;
    bool parse(DataStream &stream, HotStuffCore *core) noexcept;
};

struct ExperimentPostQcAuditMissingClaim final
{
    ExperimentPostQcAuditRelay relay;
    std::set<ReplicaID> signers;
};

struct ExperimentPostQcAuditRootSnapshot final
{
    ProposalKey proposal;
    // Configuration-runtime generation retained for correlation with the
    // reporter's wire identity.
    std::uint64_t generation{0};
    // Root-local exact proposal-context generation captured from the lease.
    std::uint64_t context_generation{0};
    std::uint64_t prepared_ns{0};
    std::uint64_t published_ns{0};
    std::uint64_t expiry_ns{0};
    std::set<ReplicaID> reporter_subtree;
    std::set<ReplicaID> qc_signers;
    bytearray_t frozen_qc;
};

struct ExperimentPostQcAuditRootVerification final
{
    ExperimentPostQcAuditRelay relay;
    ExperimentPostQcAuditRootSnapshot root_snapshot;
    std::set<ReplicaID> audit_signers;
    std::uint64_t received_ns{0};
};

struct ExperimentPostQcAuditDiagnostics final
{
    bool enabled{false};
    bool reporter_armed{false};
    bool reporter_closed{false};
    bool reporter_target_verification_in_flight{false};
    bool reporter_incomplete{false};
    bool reporter_deadline_consumed{false};
    bool reporter_relay_attempted{false};
    bool reporter_relay_sent{false};
    bool reporter_terminal{false};
    bool root_prepared{false};
    bool root_active{false};
    bool root_verification_attempted{false};
    bool root_verification_in_flight{false};
    bool root_accepted{false};
    bool root_terminal{false};
};

/**
 * One-context PQAR state machine.
 *
 * `synchronize_reporter_accumulator` accepts only a clone of the normal
 * verified accumulator. `record_verified_post_close_target` is the sole
 * method that may extend the clone, and requires a separately verified exact
 * target part. Neither method exposes state to consensus callers.
 */
class ExperimentPostQcAudit final
{
public:
    explicit ExperimentPostQcAudit(
        ExperimentPostQcAuditOptions options = {},
        ReplicaID local_replica = 0);
    ~ExperimentPostQcAudit();

    ExperimentPostQcAudit(const ExperimentPostQcAudit &) = delete;
    ExperimentPostQcAudit &operator=(
        const ExperimentPostQcAudit &) = delete;
    ExperimentPostQcAudit(ExperimentPostQcAudit &&) = delete;
    ExperimentPostQcAudit &operator=(
        ExperimentPostQcAudit &&) = delete;

    bool enabled() const noexcept;
    bool is_reporter() const noexcept;
    bool is_root() const noexcept;
    const ExperimentPostQcAuditOptions &options() const noexcept;

    bool arm_reporter(
        const ProposalKey &proposal,
        std::uint64_t generation,
        const ProposalTreeSnapshot &tree,
        const QuorumCert &verified_accumulator,
        std::uint64_t armed_ns);

    std::optional<ExperimentPostQcAuditTargetObservation>
    synchronize_reporter_accumulator(
        const ProposalKey &proposal,
        std::uint64_t generation,
        const QuorumCert &verified_accumulator,
        ReplicaID authenticated_sender,
        std::uint64_t arrival_ns);

    bool close_reporter_context(
        const ProposalKey &proposal,
        std::uint64_t generation,
        std::uint64_t closed_ns) noexcept;

    bool accepts_post_close_target(
        const ProposalKey &proposal,
        std::uint64_t generation,
        ReplicaID authenticated_sender,
        std::uint64_t arrival_ns) const noexcept;

    bool begin_target_verification(
        const ProposalKey &proposal,
        std::uint64_t generation,
        ReplicaID authenticated_sender,
        ExperimentPostQcAuditTargetPhase phase,
        std::uint64_t arrival_ns);

    std::optional<ExperimentPostQcAuditTargetObservation>
    complete_target_verification(
        const ProposalKey &proposal,
        std::uint64_t generation,
        ReplicaID authenticated_sender,
        const ReplicaConfig &configuration,
        const PartCert &part,
        bool verified);

    std::optional<ExperimentPostQcAuditTargetObservation>
    record_verified_post_close_target(
        const ProposalKey &proposal,
        std::uint64_t generation,
        ReplicaID authenticated_sender,
        const ReplicaConfig &configuration,
        const PartCert &verified_part,
        std::uint64_t arrival_ns);

    std::optional<ExperimentPostQcAuditMissingClaim>
    consume_reporter_deadline(std::uint64_t now_ns);

    bool complete_reporter_relay(bool sent) noexcept;

    std::optional<ExperimentPostQcAuditRootSnapshot> prepare_root(
        const ProposalKey &proposal,
        std::uint64_t generation,
        std::uint64_t context_generation,
        const ProposalTreeSnapshot &tree,
        const QuorumCert &verified_qc,
        std::size_t frozen_global_quorum,
        std::uint64_t prepared_ns);

    bool activate_root(
        const ProposalKey &proposal,
        std::uint64_t generation,
        const QuorumCert &published_qc,
        std::uint64_t published_ns,
        bool consensus_context_terminal) noexcept;

    std::optional<ExperimentPostQcAuditRootVerification>
    begin_root_verification(
        const ExperimentPostQcAuditRelay &relay,
        ReplicaID authenticated_sender,
        std::uint64_t received_ns);

    bool complete_root_verification(
        const ProposalKey &proposal,
        std::uint64_t generation,
        std::uint64_t verified_ns,
        bool certificate_verified,
        bool consensus_context_terminal,
        bool qc_unchanged) noexcept;

    bool expire(std::uint64_t now_ns) noexcept;
    std::optional<ExperimentPostQcAuditRootSnapshot>
    root_snapshot() const;
    ExperimentPostQcAuditDiagnostics diagnostics() const noexcept;

private:
    struct State;
    std::unique_ptr<State> state_;
};

const char *to_string(
    ExperimentPostQcAuditTargetPhase phase) noexcept;

} // namespace hotstuff

#endif
