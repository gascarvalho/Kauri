#include "hotstuff/operator_capacity_epoch1_finalizer.h"

namespace hotstuff
{

std::optional<OperatorCapacityEpoch1Finalization>
finalize_operator_capacity_epoch1(
    const VerifiedOperatorCapacityLabelEnvelope &envelope,
    const EpochDefinition &current_e0,
    const std::vector<ReplicaID> &membership,
    const AdaptationSnapshot &manager_frozen_snapshot,
    std::uint64_t checked_raw_now_ns,
    const OperatorCapacityEpoch1FinalizerIssuer &issuer) noexcept
{
    try
    {
        const AdaptationEpochId epoch{
            current_e0.epoch_number(), current_e0.epoch_digest()};
        if (issuer.issuer_id == 0 || checked_raw_now_ns == 0 ||
            epoch.epoch_number != 0 || envelope.membership() != membership ||
            envelope.epoch0() != epoch ||
            manager_frozen_snapshot.epoch() != epoch ||
            manager_frozen_snapshot.snapshot_id().empty() ||
            manager_frozen_snapshot.evidence_cutoff() == 0 ||
            current_e0.trees().empty() ||
            envelope.baseline_topology_digest() !=
                operator_capacity_baseline_topology_digest(
                    epoch, membership, current_e0.trees()))
            return std::nullopt;

        const auto &capacity = envelope.capacity_snapshot();
        if (checked_raw_now_ns < capacity.valid_from_monotonic_ns ||
            checked_raw_now_ns > capacity.valid_until_monotonic_ns)
            return std::nullopt;

        OperatorCapacityPolicyConfig policy;
        policy.arm = envelope.arm();
        policy.policy_version = "operator-capacity-v1";
        policy.decision_clock_domain =
            OperatorCapacityClockDomain::monotonic_raw_ns;
        policy.decision_monotonic_ns = checked_raw_now_ns;
        policy.approved_capacity_digest = capacity.canonical_digest;
        policy.expected_responsiveness_snapshot_id =
            manager_frozen_snapshot.snapshot_id();
        policy.expected_evidence_cutoff =
            manager_frozen_snapshot.evidence_cutoff();
        policy.capacity_snapshot = capacity;
        policy.fanout = current_e0.trees().front().fanout;
        policy.tree_count =
            static_cast<std::uint32_t>(current_e0.trees().size());
        policy.baseline_trees = current_e0.trees();

        // The finalizer is a strict boundary, not merely a signing helper.
        // Do not sign a policy that the all-responsive selector rejects.
        if (!build_operator_capacity_placement(
                membership, manager_frozen_snapshot, policy))
            return std::nullopt;

        return OperatorCapacityEpoch1Finalization{
            authorize_operator_capacity(
                membership, policy, issuer.issuer_id, issuer.private_key),
            envelope.digest()};
    }
    catch (...)
    {
        return std::nullopt;
    }
}

} // namespace hotstuff
