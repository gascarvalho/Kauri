/**
 * Deterministic responsiveness classification from accepted evidence.
 */

#ifndef HOTSTUFF_ADAPTATION_H_INCLUDED
#define HOTSTUFF_ADAPTATION_H_INCLUDED

#include <cstddef>
#include <cstdint>
#include <optional>
#include <string>
#include <vector>

#include "hotstuff/evidence.h"

namespace hotstuff
{

constexpr std::uint32_t kAdaptationSchemaVersion = 1;
constexpr std::uint32_t kRatePpmScale = 1'000'000;
constexpr std::size_t kMaximumAdaptationMembers = 65'536;
constexpr std::size_t kMaximumAdaptationEvidenceRecords = 1'048'576;
constexpr std::uint32_t kMaximumAdaptationAttemptWindow = 4'096;
constexpr std::size_t kMaximumAdaptationPolicyVersionBytes = 64;
constexpr std::uint16_t kPercentileBasisPointScale = 10'000;

using RatePpm = std::uint32_t;

/**
 * Versioned reputation mechanism used to order one immutable assessment.
 *
 * Every mechanism shares the same evidence validation and eligibility
 * boundary.  A mechanism may change only the deterministic order of the
 * resulting replica assessments; it cannot change membership, quorum, vote,
 * certificate, commit, or epoch-activation authority.
 */
enum class ReputationMechanism : std::uint8_t
{
    responsiveness = 1,
    latency_priority = 2,
};

enum class ResponsivenessClass : std::uint8_t
{
    responsive = 1,
    insufficient_evidence = 2,
    nonresponsive = 3,
};

enum class ResponsivenessReason : std::uint8_t
{
    insufficient_attempts = 1,
    response_rate_below_minimum = 2,
    timeout_rate_above_maximum = 3,
    persistent_timeout_streak = 4,
};

struct AdaptationEpochId
{
    std::uint32_t epoch_number{0};
    uint256_t epoch_digest;

    bool operator==(const AdaptationEpochId &other) const noexcept
    {
        return epoch_number == other.epoch_number &&
               epoch_digest == other.epoch_digest;
    }

    bool operator!=(const AdaptationEpochId &other) const noexcept
    {
        return !(*this == other);
    }
};

struct AcceptedEvidenceView
{
    /**
     * Build this borrowed view directly from EvidenceLedger::accepted() on a
     * healthy EvidenceLedger. Access must be externally serialized, storage
     * must remain stable for the call duration, and the view is not retained.
     */
    const AcceptedEvidenceRecord *data{nullptr};
    std::size_t size{0};
};

struct AdaptationPolicy
{
    std::uint32_t schema_version{kAdaptationSchemaVersion};
    std::string policy_version{"kauri-responsiveness-v1"};
    ReputationMechanism reputation_mechanism{
        ReputationMechanism::responsiveness};
    std::uint32_t attempt_window{32};
    std::uint32_t minimum_attempts{8};
    RatePpm minimum_response_rate_ppm{750'000};
    RatePpm maximum_timeout_rate_ppm{250'000};
    std::uint32_t trailing_timeout_streak{3};
    std::uint16_t latency_percentile_basis_points{5'000};

    bool operator==(const AdaptationPolicy &other) const noexcept
    {
        return schema_version == other.schema_version &&
               policy_version == other.policy_version &&
               reputation_mechanism == other.reputation_mechanism &&
               attempt_window == other.attempt_window &&
               minimum_attempts == other.minimum_attempts &&
               minimum_response_rate_ppm ==
                   other.minimum_response_rate_ppm &&
               maximum_timeout_rate_ppm ==
                   other.maximum_timeout_rate_ppm &&
               trailing_timeout_streak ==
                   other.trailing_timeout_streak &&
               latency_percentile_basis_points ==
                   other.latency_percentile_basis_points;
    }

    bool operator!=(const AdaptationPolicy &other) const noexcept
    {
        return !(*this == other);
    }
};

struct ReplicaAdaptationResult
{
    ReplicaID replica_id{0};
    std::uint32_t rank{0};
    ResponsivenessClass classification{
        ResponsivenessClass::insufficient_evidence};
    bool eligible{false};
    std::uint32_t attempt_count{0};
    std::uint32_t on_time_count{0};
    std::uint32_t late_count{0};
    std::uint32_t timeout_only_count{0};
    std::uint32_t response_count{0};
    std::uint32_t timeout_count{0};
    std::uint32_t trailing_timeout_count{0};
    RatePpm response_rate_ppm{0};
    RatePpm timeout_rate_ppm{0};
    std::optional<std::uint64_t> latency_percentile_us;
    std::vector<ResponsivenessReason> reasons;

    bool operator==(const ReplicaAdaptationResult &other) const noexcept
    {
        return replica_id == other.replica_id &&
               rank == other.rank &&
               classification == other.classification &&
               eligible == other.eligible &&
               attempt_count == other.attempt_count &&
               on_time_count == other.on_time_count &&
               late_count == other.late_count &&
               timeout_only_count == other.timeout_only_count &&
               response_count == other.response_count &&
               timeout_count == other.timeout_count &&
               trailing_timeout_count ==
                   other.trailing_timeout_count &&
               response_rate_ppm == other.response_rate_ppm &&
               timeout_rate_ppm == other.timeout_rate_ppm &&
               latency_percentile_us == other.latency_percentile_us &&
               reasons == other.reasons;
    }

    bool operator!=(const ReplicaAdaptationResult &other) const noexcept
    {
        return !(*this == other);
    }
};

class AdaptationSnapshot final
{
public:
    AdaptationSnapshot(const AdaptationSnapshot &) = default;
    AdaptationSnapshot(AdaptationSnapshot &&) = default;
    AdaptationSnapshot &operator=(const AdaptationSnapshot &) = delete;
    AdaptationSnapshot &operator=(AdaptationSnapshot &&) = delete;

    std::uint32_t schema_version() const noexcept
    {
        return schema_version_;
    }

    const std::string &snapshot_id() const noexcept
    {
        return snapshot_id_;
    }

    const AdaptationEpochId &epoch() const noexcept
    {
        return epoch_;
    }

    std::uint64_t evidence_cutoff() const noexcept
    {
        return evidence_cutoff_;
    }

    std::size_t accepted_record_count() const noexcept
    {
        return accepted_record_count_;
    }

    const AdaptationPolicy &policy() const noexcept
    {
        return policy_;
    }

    std::uint64_t seed() const noexcept
    {
        return seed_;
    }

    const std::vector<ReplicaAdaptationResult> &ranking() const noexcept
    {
        return ranking_;
    }

private:
    friend AdaptationSnapshot build_adaptation_snapshot(
        const std::vector<ReplicaID> &membership,
        const AdaptationEpochId &current_epoch,
        AcceptedEvidenceView accepted_evidence,
        std::uint64_t evidence_cutoff,
        const AdaptationPolicy &policy,
        std::uint64_t seed);

    AdaptationSnapshot() = default;

    std::uint32_t schema_version_{kAdaptationSchemaVersion};
    std::string snapshot_id_;
    AdaptationEpochId epoch_;
    std::uint64_t evidence_cutoff_{0};
    std::size_t accepted_record_count_{0};
    AdaptationPolicy policy_;
    std::uint64_t seed_{0};
    std::vector<ReplicaAdaptationResult> ranking_;
};

AdaptationSnapshot build_adaptation_snapshot(
    const std::vector<ReplicaID> &membership,
    const AdaptationEpochId &current_epoch,
    AcceptedEvidenceView accepted_evidence,
    std::uint64_t evidence_cutoff,
    const AdaptationPolicy &policy,
    std::uint64_t seed);

} // namespace hotstuff

#endif
