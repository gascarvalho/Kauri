#ifndef HOTSTUFF_OPERATOR_CAPACITY_CONSUMPTION_RECORD_H_INCLUDED
#define HOTSTUFF_OPERATOR_CAPACITY_CONSUMPTION_RECORD_H_INCLUDED

#include <cstdint>
#include <string>

#include "hotstuff/operator_capacity_policy.h"

namespace hotstuff
{

constexpr const char kOperatorCapacityConsumptionKind[] =
    "kauri-operator-capacity-consumption-v1";

/** Manager-authored provenance, not a consensus vote or epoch authority. */
struct OperatorCapacityConsumptionRecord
{
    std::uint32_t schema_version{1};
    std::string kind{kOperatorCapacityConsumptionKind};
    std::string run_id;
    std::string source_instance;
    std::string stage_a_wire_sha256;
    std::string stage_a_semantic_digest;
    std::string stage_b_authorization_wire_sha256;
    std::uint32_t label_issuer_id{0};
    std::string label_issuer_reference;
    std::string label_issuer_public_key_fingerprint;
    std::uint32_t epoch_change_issuer_id{0};
    OperatorCapacityArm arm{OperatorCapacityArm::fast_priority_treatment};
    std::string capacity_digest;
    std::string epoch0_digest;
    std::string epoch0_topology_digest;
    std::string baseline_snapshot_id;
    std::uint64_t baseline_evidence_cutoff{0};
    std::uint64_t decision_monotonic_raw_ns{0};
    std::uint64_t hard_deadline_monotonic_raw_ns{0};
    std::string successor_policy_snapshot_id;
    std::string successor_bundle_sha256;
};

/** Validate every field, then emit exact sorted-key ASCII JSON plus newline. */
std::string serialize_operator_capacity_consumption_record(
    const OperatorCapacityConsumptionRecord &record);

} // namespace hotstuff

#endif
