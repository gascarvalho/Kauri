/**
 * Byzantine-guarded, observational adaptive-v2 candidate selection.
 */

#ifndef HOTSTUFF_ADAPTIVE_V2_SELECTION_H_INCLUDED
#define HOTSTUFF_ADAPTIVE_V2_SELECTION_H_INCLUDED

#include <cstddef>
#include <cstdint>
#include <memory>
#include <vector>

#include "hotstuff/adaptation.h"
#include "hotstuff/evidence_reputation.h"

namespace hotstuff
{

constexpr std::uint32_t kAdaptiveV2SelectionSchemaVersion = 1;

struct AdaptiveV2SelectionConfig
{
    std::uint32_t schema_version{kAdaptiveV2SelectionSchemaVersion};
    /** Adaptation target count in [1, f]; this never changes consensus f/Q. */
    std::uint32_t required_nonresponsive{0};
    std::uint32_t minimum_score_drop{1};
    std::uint32_t minimum_timeouts_per_reporter{1};
    std::size_t maximum_post_baseline_timeout_attempts{4096};
    AdaptationPolicy responsiveness_policy;
    std::uint64_t snapshot_seed{0};
    /**
     * Optional prospective fault-window anchor. Zero preserves the legacy
     * v1-v14 selection behavior. When enabled, it must be paired with the
     * exact canonical predecessor tree count below.
     */
    std::uint64_t fault_containment_evidence_start_monotonic_ns{0};
    std::uint32_t fault_containment_required_tree_coverage{0};
};

enum class AdaptiveV2FaultContainmentCoverageStatus : std::uint8_t
{
    disabled = 1,
    incomplete,
    ready,
    invalid,
};

/** Actor-blind audit of exact post-fault predecessor-tree coverage. */
struct AdaptiveV2FaultContainmentCoverage
{
    AdaptiveV2FaultContainmentCoverageStatus status{
        AdaptiveV2FaultContainmentCoverageStatus::disabled};
    std::uint64_t fault_evidence_start_monotonic_ns{0};
    std::uint64_t evidence_cutoff{0};
    std::vector<std::uint32_t> required_tree_ids;
    std::vector<std::uint32_t> observed_tree_ids;
};

/**
 * Require one accepted on-time direct-vote proposal from every canonical
 * tree. A proposal qualifies only when its conservative attempt-start lower
 * bound is at or after the sealed CLOCK_MONOTONIC_RAW fault-open timestamp.
 */
AdaptiveV2FaultContainmentCoverage
evaluate_adaptive_v2_fault_containment_coverage(
    const std::vector<AcceptedEvidenceRecord> &accepted,
    const AdaptationEpochId &current_epoch,
    std::uint64_t evidence_cutoff,
    std::uint64_t fault_evidence_start_monotonic_ns,
    std::uint32_t required_tree_count) noexcept;

AdaptiveV2FaultContainmentCoverage
evaluate_adaptive_v2_fault_containment_coverage(
    const std::vector<AcceptedEvidenceRecord> &accepted,
    const AdaptationEpochId &current_epoch,
    std::uint64_t evidence_cutoff,
    std::uint64_t fault_evidence_start_monotonic_ns,
    const std::vector<std::uint32_t> &required_tree_ids) noexcept;

enum class AdaptiveV2SelectionStatus : std::uint8_t
{
    baseline_frozen = 1,
    selected,
    insufficient_guarded_candidates,
    insufficient_eligible_roots,
    invalid_state,
    invalid_cutoff,
    ledger_unhealthy,
    mixed_epoch,
    nonmember_evidence,
    projection_failed,
    capacity_exceeded,
    snapshot_failed,
    internal_failure,
};

/** Authority for the exact constrained replica set in a selection result. */
enum class AdaptiveV2SelectionConstraintBasis : std::uint8_t
{
    guarded_evidence = 1,
    inherited_consensus_wait_exempt,
};

struct AdaptiveV2SelectionMetadata
{
    std::uint32_t replica_count{0};
    std::uint32_t fault_threshold{0};
    std::uint32_t quorum{0};
    std::uint32_t required_nonresponsive{0};
    std::uint32_t required_qualifying_reporters{0};
    std::uint32_t minimum_timeouts_per_reporter{0};
    std::uint32_t minimum_score_drop{0};
    std::uint64_t baseline_cutoff{0};
    std::uint64_t evidence_cutoff{0};
};

struct AdaptiveV2ReplicaScore
{
    ReplicaID replica_id{0};
    int score{0};
};

/** Explainable evidence supporting one guarded candidate. */
struct AdaptiveV2CandidateAudit
{
    ReplicaID replica_id{0};
    ResponsivenessClass snapshot_classification{
        ResponsivenessClass::insufficient_evidence};
    int baseline_score{0};
    int current_score{0};
    std::int64_t baseline_score_delta{0};
    /**
     * Post-baseline score relative to its running high-water mark.
     *
     * The value starts at zero, timeouts decrease it, and on-time responses
     * move it toward (but never above) zero. A late response compensates only
     * its correlated post-baseline timeout. Unlike the raw score delta,
     * healthy history cannot bank credit against a later failure.
     */
    std::int64_t guard_drawdown{0};
    std::uint64_t total_uncompensated_timeouts{0};
    std::vector<ReplicaID> qualifying_reporters;
    bool snapshot_nonresponsive{false};
    bool score_drop_satisfied{false};
    bool reporter_guard_satisfied{false};
    bool guarded_eligible{false};
};

struct AdaptiveV2SelectionResult
{
    AdaptiveV2SelectionStatus status{
        AdaptiveV2SelectionStatus::invalid_state};
    AdaptiveV2SelectionConstraintBasis constraint_basis{
        AdaptiveV2SelectionConstraintBasis::guarded_evidence};
    AdaptiveV2SelectionMetadata metadata;
    std::unique_ptr<AdaptationSnapshot> snapshot;
    std::vector<AdaptiveV2CandidateAudit> eligible_candidates;
    std::vector<ReplicaID> selected_replicas;
    std::vector<ReplicaID> eligible_roots;
};

/**
 * Single-writer selection over one healthy, externally serialized ledger.
 *
 * The ledger is borrowed and must outlive this object. This class owns the
 * target-only score table and its accepted-prefix projection. Baseline and
 * later cutoffs are monotonic and belong to one exact epoch. Guarded selection
 * snapshots use the full accepted prefix, while inherited optimization ranks
 * only fresh attempts after the frozen baseline. A legal late response whose
 * exact timeout precedes the baseline is correlated against the full accepted
 * prefix and excluded from that fresh-attempt snapshot.
 *
 * A reporter qualifies for a target only after at least the configured K
 * timeout-only attempts after baseline. At least f+1 independently
 * authenticated qualifying reporters are required. A legal timeout-to-late
 * transition compensates the score and removes that attempt from the guard.
 * The score-drop guard uses a high-water-normalized post-baseline drawdown:
 * on-time responses heal an existing drawdown but cannot create positive
 * credit; a late response heals only its correlated post-baseline timeout.
 * On-time observations never contribute to timeout persistence.
 *
 * The result is observational input only. This class cannot modify topology,
 * membership, quorum, epoch state, readiness, votes, certificates, signatures,
 * or wait-exemption policy.
 */
class AdaptiveV2ByzantineSelection final
{
public:
    AdaptiveV2ByzantineSelection(
        const EvidenceLedger &ledger,
        std::vector<ReplicaID> membership,
        AdaptationEpochId current_epoch,
        AdaptiveV2SelectionConfig config,
        EvidenceReputationLimits reputation_limits = {});
    ~AdaptiveV2ByzantineSelection();

    AdaptiveV2ByzantineSelection(
        const AdaptiveV2ByzantineSelection &) = delete;
    AdaptiveV2ByzantineSelection &operator=(
        const AdaptiveV2ByzantineSelection &) = delete;
    AdaptiveV2ByzantineSelection(
        AdaptiveV2ByzantineSelection &&) = delete;
    AdaptiveV2ByzantineSelection &operator=(
        AdaptiveV2ByzantineSelection &&) = delete;

    AdaptiveV2SelectionStatus freeze_baseline(
        std::uint64_t evidence_cutoff) noexcept;

    AdaptiveV2SelectionResult select_through(
        std::uint64_t evidence_cutoff) noexcept;

    /**
     * Rank fresh responsive roots while preserving an exact constraint set
     * already authorized by the consensus-ordered predecessor epoch.
     *
     * The inherited input must contain exactly the configured bounded target
     * count of unique members. Every unconstrained replica must be responsive
     * and eligible in the accepted suffix
     * `(baseline_cutoff, evidence_cutoff]` under the configured snapshot
     * policy. An exact timeout-to-late transition crossing the baseline is
     * validated against the full accepted prefix, then excluded: it is not a
     * fresh responsiveness attempt. Invalid correlation fails closed. An
     * incomplete suffix does not advance the cutoff or reputation projection.
     * This path never creates guarded candidates.
     */
    AdaptiveV2SelectionResult rank_inheriting_constraints_through(
        std::uint64_t evidence_cutoff,
        const std::vector<ReplicaID> &inherited_wait_exempt) noexcept;

    const std::vector<AdaptiveV2ReplicaScore> &
    baseline_scores() const noexcept;

    const std::vector<EvidenceReputationAuditUpdate> &
    score_trajectory() const noexcept;

    const std::vector<ReplicaID> &membership() const noexcept;
    const ByzantineQuorum &quorum_metadata() const noexcept;
    const AdaptationEpochId &current_epoch() const noexcept;

    std::uint64_t baseline_cutoff() const noexcept;
    std::uint64_t current_cutoff() const noexcept;
    bool baseline_frozen() const noexcept;
    bool healthy() const noexcept;

private:
    struct State;
    std::unique_ptr<State> state_;
};

} // namespace hotstuff

#endif
