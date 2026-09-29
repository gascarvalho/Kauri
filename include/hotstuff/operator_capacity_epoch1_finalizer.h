#ifndef HOTSTUFF_OPERATOR_CAPACITY_EPOCH1_FINALIZER_H_INCLUDED
#define HOTSTUFF_OPERATOR_CAPACITY_EPOCH1_FINALIZER_H_INCLUDED

#include <cstdint>
#include <optional>
#include <vector>

#include "hotstuff/operator_capacity_authorization.h"
#include "hotstuff/operator_capacity_label_envelope.h"

namespace hotstuff
{

struct OperatorCapacityEpoch1FinalizerIssuer
{
    std::uint32_t issuer_id{0};
    PrivKeySecp256k1 private_key;
};

struct OperatorCapacityEpoch1Finalization
{
    OperatorCapacityAuthorization authorization;
    uint256_t envelope_digest;
};

/** Derive a signed final decision only from verified Stage A and live facts. */
std::optional<OperatorCapacityEpoch1Finalization>
finalize_operator_capacity_epoch1(
    const VerifiedOperatorCapacityLabelEnvelope &envelope,
    const EpochDefinition &current_e0,
    const std::vector<ReplicaID> &membership,
    const AdaptationSnapshot &manager_frozen_snapshot,
    std::uint64_t checked_raw_now_ns,
    const OperatorCapacityEpoch1FinalizerIssuer &issuer) noexcept;

} // namespace hotstuff

#endif
