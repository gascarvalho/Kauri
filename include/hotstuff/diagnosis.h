/**
 * Bounded false-reporter versus persistent-omitter diagnosis.
 */

#ifndef HOTSTUFF_DIAGNOSIS_H_INCLUDED
#define HOTSTUFF_DIAGNOSIS_H_INCLUDED

#include <cstddef>
#include <cstdint>
#include <optional>
#include <vector>

#include "hotstuff/evidence.h"

namespace hotstuff
{

constexpr std::uint32_t kDiagnosisSchemaVersion = 1;
constexpr std::size_t kMaximumDiagnosisMembers = 31;
constexpr std::uint32_t kMaximumDiagnosticFaultBound = 3;
constexpr std::size_t kMaximumDiagnosisObservations = 65'536;
constexpr std::size_t kMaximumDiagnosisHypotheses = 37'883;

enum class DiagnosticOutcome : std::uint8_t
{
    on_time_valid = 0,
    timeout = 1,
};

/**
 * One finalized exact attempt in a frozen diagnostic window.
 *
 * Callers normalize transport-specific transitions before invoking diagnosis.
 * In particular, this type deliberately has no timeout-to-late policy.
 */
struct DiagnosticObservation
{
    ResponseAttemptIdentity attempt;
    DiagnosticOutcome outcome{DiagnosticOutcome::on_time_valid};

    bool operator==(const DiagnosticObservation &other) const noexcept
    {
        return attempt == other.attempt && outcome == other.outcome;
    }

    bool operator!=(const DiagnosticObservation &other) const noexcept
    {
        return !(*this == other);
    }
};

struct CompatibleHypothesis
{
    std::vector<ReplicaID> false_reporters;
    std::vector<ReplicaID> persistent_omitters;

    bool operator==(const CompatibleHypothesis &other) const noexcept
    {
        return false_reporters == other.false_reporters &&
               persistent_omitters == other.persistent_omitters;
    }

    bool operator!=(const CompatibleHypothesis &other) const noexcept
    {
        return !(*this == other);
    }
};

enum class DiagnosticClassification : std::uint8_t
{
    unresolved = 1,
    correct = 2,
    false_reporter = 3,
    persistent_omitter = 4,
    model_inconsistent = 5,
};

/**
 * Exact compatible-hypothesis counts, not calibrated probabilities.
 */
struct ReplicaHypothesisMass
{
    ReplicaID replica_id{0};
    std::uint64_t false_reporter_count{0};
    std::uint64_t persistent_omitter_count{0};
    std::uint64_t compatible_count{0};
    DiagnosticClassification classification{
        DiagnosticClassification::unresolved};

    bool operator==(const ReplicaHypothesisMass &other) const noexcept
    {
        return replica_id == other.replica_id &&
               false_reporter_count == other.false_reporter_count &&
               persistent_omitter_count ==
                   other.persistent_omitter_count &&
               compatible_count == other.compatible_count &&
               classification == other.classification;
    }

    bool operator!=(const ReplicaHypothesisMass &other) const noexcept
    {
        return !(*this == other);
    }
};

struct DiagnosisSnapshot
{
    std::uint32_t schema_version{kDiagnosisSchemaVersion};
    std::vector<ReplicaID> membership;
    std::uint32_t diagnostic_fault_bound{0};
    std::vector<DiagnosticObservation> canonical_history;
    std::vector<CompatibleHypothesis> compatible_hypotheses;
    std::vector<ReplicaHypothesisMass> mode_mass;
    std::uint64_t initial_hypothesis_count{0};
    std::uint64_t eliminated_hypothesis_count{0};

    bool model_consistent() const noexcept
    {
        return !compatible_hypotheses.empty();
    }

    bool operator==(const DiagnosisSnapshot &other) const noexcept
    {
        return schema_version == other.schema_version &&
               membership == other.membership &&
               diagnostic_fault_bound ==
                   other.diagnostic_fault_bound &&
               canonical_history == other.canonical_history &&
               compatible_hypotheses ==
                   other.compatible_hypotheses &&
               mode_mass == other.mode_mass &&
               initial_hypothesis_count ==
                   other.initial_hypothesis_count &&
               eliminated_hypothesis_count ==
                   other.eliminated_hypothesis_count;
    }

    bool operator!=(const DiagnosisSnapshot &other) const noexcept
    {
        return !(*this == other);
    }
};

struct DiagnosisLimits
{
    std::size_t maximum_members{kMaximumDiagnosisMembers};
    std::uint32_t maximum_diagnostic_fault_bound{
        kMaximumDiagnosticFaultBound};
    std::size_t maximum_observations{kMaximumDiagnosisObservations};
    std::size_t maximum_hypotheses{kMaximumDiagnosisHypotheses};
};

enum class DiagnosisBuildStatus : std::uint8_t
{
    built = 1,
    invalid_limits,
    invalid_membership,
    diagnostic_bound_exceeded,
    observation_capacity_exceeded,
    invalid_observation,
    conflicting_observation,
    hypothesis_capacity_exceeded,
    allocation_failure,
    internal_failure,
};

struct DiagnosisBuildResult
{
    DiagnosisBuildStatus status{DiagnosisBuildStatus::internal_failure};
    std::optional<DiagnosisSnapshot> snapshot;

    explicit operator bool() const noexcept
    {
        return status == DiagnosisBuildStatus::built &&
               snapshot.has_value();
    }
};

/**
 * Recompute the complete bounded hypothesis set from canonical history.
 *
 * A hypothesis (L, C) is compatible exactly when L and C are disjoint,
 * |L| + |C| is at most diagnostic_fault_bound, and every observation
 * (u, v, y) satisfies:
 *
 *     u in L || y == (v in C)
 *
 * The function accepts no ground-truth label and owns no consensus, quorum,
 * topology, activation, or tree-policy authority. Capacity exhaustion returns
 * no partial snapshot.
 */
DiagnosisBuildResult build_diagnosis_snapshot(
    std::vector<ReplicaID> membership,
    std::uint32_t diagnostic_fault_bound,
    std::vector<DiagnosticObservation> history,
    DiagnosisLimits limits = {}) noexcept;

} // namespace hotstuff

#endif
