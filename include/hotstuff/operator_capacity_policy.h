/**
 * Fail-closed, issuer-referenced operator-capacity placement input.
 *
 * This is a native component seam only.  It neither signs nor activates an
 * epoch, and it does not change adaptive-v2 guarded-fault selection.
 */
#ifndef HOTSTUFF_OPERATOR_CAPACITY_POLICY_H_INCLUDED
#define HOTSTUFF_OPERATOR_CAPACITY_POLICY_H_INCLUDED

#include <cstdint>
#include <optional>
#include <string>
#include <vector>

#include "hotstuff/adaptation.h"
#include "hotstuff/tree_policy.h"

namespace hotstuff
{

constexpr std::uint32_t kOperatorCapacitySnapshotSchemaVersion = 1;
constexpr std::size_t kMaximumOperatorCapacityIssuerReferenceBytes = 128;

enum class OperatorCapacityClass : std::uint8_t
{
    slow = 1,
    fast = 2,
};

enum class OperatorCapacityClockDomain : std::uint8_t
{
    monotonic_raw_ns = 1,
};

struct OperatorCapacityLabel
{
    ReplicaID replica_id{0};
    OperatorCapacityClass capacity{OperatorCapacityClass::slow};
};

/** Immutable bytes are represented by their canonical hash plus fields. */
struct OperatorCapacitySnapshot
{
    std::uint32_t schema_version{kOperatorCapacitySnapshotSchemaVersion};
    std::string issuer_reference;
    AdaptationEpochId predecessor;
    OperatorCapacityClockDomain clock_domain{
        OperatorCapacityClockDomain::monotonic_raw_ns};
    std::uint64_t valid_from_monotonic_ns{0};
    std::uint64_t valid_until_monotonic_ns{0};
    std::vector<OperatorCapacityLabel> labels;
    uint256_t canonical_digest;
};

/** Return the digest over every snapshot field except canonical_digest. */
uint256_t operator_capacity_snapshot_digest(
    const OperatorCapacitySnapshot &snapshot);

/** Transport bytes are canonical input bytes; their SHA-256 is external
 * provenance, while operator_capacity_snapshot_digest is the policy identity. */
struct OperatorCapacitySnapshotWireLimits
{
    std::size_t maximum_payload_bytes{16 * 1024};
};
enum class OperatorCapacitySnapshotWireError : std::uint8_t
{
    none = 0, invalid_limits, payload_too_large, truncated, invalid_domain,
    unsupported_schema, trailing_bytes, noncanonical_encoding,
    allocation_failure, internal_failure,
};
struct OperatorCapacitySnapshotDecodeResult
{
    OperatorCapacitySnapshotWireError error{
        OperatorCapacitySnapshotWireError::internal_failure};
    std::optional<OperatorCapacitySnapshot> value;
    explicit operator bool() const noexcept
    { return error == OperatorCapacitySnapshotWireError::none && value.has_value(); }
};
bytearray_t encode_operator_capacity_snapshot(
    const OperatorCapacitySnapshot &snapshot,
    const OperatorCapacitySnapshotWireLimits &limits);
OperatorCapacitySnapshotDecodeResult decode_operator_capacity_snapshot(
    const bytearray_t &payload,
    const OperatorCapacitySnapshotWireLimits &limits) noexcept;

enum class OperatorCapacityArm : std::uint8_t
{
    fast_priority_treatment = 1,
    exact_copy_sham = 2,
};

struct OperatorCapacityPolicyConfig
{
    OperatorCapacityArm arm{OperatorCapacityArm::fast_priority_treatment};
    std::string policy_version{"operator-capacity-v1"};
    OperatorCapacityClockDomain decision_clock_domain{
        OperatorCapacityClockDomain::monotonic_raw_ns};
    std::uint64_t decision_monotonic_ns{0};
    /** These values must be pinned by a separately trusted manager input. */
    uint256_t approved_capacity_digest;
    std::string expected_responsiveness_snapshot_id;
    std::uint64_t expected_evidence_cutoff{0};
    std::uint32_t fanout{0};
    std::uint32_t tree_count{0};
    OperatorCapacitySnapshot capacity_snapshot;
    /** Required by both arms; placement may change, tree metadata may not. */
    std::vector<EpochTreeDefinition> baseline_trees;
};


enum class OperatorCapacityPolicyStatus : std::uint8_t
{
    selected = 1,
    invalid_config,
    invalid_capacity_snapshot,
    snapshot_epoch_mismatch,
    responsiveness_snapshot_mismatch,
    not_all_responsive,
    invalid_exact_copy_baseline,
    insufficient_fast_members,
};

struct OperatorCapacityPlacementResult
{
    OperatorCapacityPolicyStatus status{OperatorCapacityPolicyStatus::invalid_config};
    /** Binds the adaptation snapshot, capacity snapshot, arm, and placement. */
    std::string policy_snapshot_id;
    std::vector<EpochTreeDefinition> trees;

    explicit operator bool() const noexcept
    {
        return status == OperatorCapacityPolicyStatus::selected;
    }
};

/**
 * Choose all-live treatment roles at the same native decision point as the
 * sham.  Treatment orders fast before slow, then ReplicaID; sham returns an
 * exact validated copy of the supplied predecessor placement.  Both arms
 * fail closed unless the immutable responsiveness and capacity snapshots bind
 * exactly to the same predecessor and every member is responsive.
 */
OperatorCapacityPlacementResult build_operator_capacity_placement(
    const std::vector<ReplicaID> &membership,
    const AdaptationSnapshot &responsiveness_snapshot,
    const OperatorCapacityPolicyConfig &config) noexcept;

} // namespace hotstuff

#endif
