#include "hotstuff/operator_capacity_consumption_record.h"

#include <algorithm>
#include <stdexcept>
#include <string_view>

namespace hotstuff
{
namespace
{

void require_hex64(std::string_view value, const char *field)
{
    if (value.size() != 64 ||
        !std::all_of(value.begin(), value.end(), [](unsigned char character) {
            return (character >= '0' && character <= '9') ||
                   (character >= 'a' && character <= 'f');
        }))
        throw std::invalid_argument(std::string(field) + " must be lower-case SHA-256");
}

std::string json_string(std::string_view value, const char *field)
{
    if (value.empty() || value.size() > 128)
        throw std::invalid_argument(std::string(field) + " length is invalid");
    std::string result{"\""};
    for (const unsigned char character : value)
    {
        if (character < 0x20 || character > 0x7e)
            throw std::invalid_argument(std::string(field) + " is not printable ASCII");
        if (character == '"' || character == '\\')
            result.push_back('\\');
        result.push_back(static_cast<char>(character));
    }
    result.push_back('"');
    return result;
}

const char *arm_name(OperatorCapacityArm arm)
{
    switch (arm)
    {
        case OperatorCapacityArm::fast_priority_treatment:
            return "fast_priority_treatment";
        case OperatorCapacityArm::exact_copy_sham:
            return "exact_copy_sham";
    }
    throw std::invalid_argument("consumption arm is invalid");
}

} // namespace

std::string serialize_operator_capacity_consumption_record(
    const OperatorCapacityConsumptionRecord &record)
{
    if (record.schema_version != 1 ||
        record.kind != kOperatorCapacityConsumptionKind ||
        record.label_issuer_id == 0 || record.epoch_change_issuer_id == 0 ||
        record.baseline_evidence_cutoff == 0 ||
        record.decision_monotonic_raw_ns == 0 ||
        record.hard_deadline_monotonic_raw_ns <=
            record.decision_monotonic_raw_ns)
        throw std::invalid_argument("consumption record identity or timing is invalid");

    require_hex64(record.stage_a_wire_sha256, "Stage-A wire hash");
    require_hex64(record.stage_a_semantic_digest, "Stage-A semantic digest");
    require_hex64(record.stage_b_authorization_wire_sha256,
                  "Stage-B wire hash");
    require_hex64(record.label_issuer_public_key_fingerprint,
                  "label issuer fingerprint");
    require_hex64(record.capacity_digest, "capacity digest");
    require_hex64(record.epoch0_digest, "Epoch-0 digest");
    require_hex64(record.epoch0_topology_digest, "Epoch-0 topology digest");
    require_hex64(record.baseline_snapshot_id, "baseline snapshot ID");
    require_hex64(record.successor_policy_snapshot_id,
                  "successor policy snapshot ID");
    require_hex64(record.successor_bundle_sha256, "successor bundle hash");

    // Fixed lexicographic key order is the versioned byte contract.
    return std::string{"{"} +
        "\"arm\":" + json_string(arm_name(record.arm), "arm") +
        ",\"baseline_evidence_cutoff\":" +
            std::to_string(record.baseline_evidence_cutoff) +
        ",\"baseline_snapshot_id\":" +
            json_string(record.baseline_snapshot_id, "baseline snapshot ID") +
        ",\"capacity_digest\":" +
            json_string(record.capacity_digest, "capacity digest") +
        ",\"decision_monotonic_raw_ns\":" +
            std::to_string(record.decision_monotonic_raw_ns) +
        ",\"epoch0_digest\":" +
            json_string(record.epoch0_digest, "Epoch-0 digest") +
        ",\"epoch0_topology_digest\":" +
            json_string(record.epoch0_topology_digest, "Epoch-0 topology digest") +
        ",\"epoch_change_issuer_id\":" +
            std::to_string(record.epoch_change_issuer_id) +
        ",\"hard_deadline_monotonic_raw_ns\":" +
            std::to_string(record.hard_deadline_monotonic_raw_ns) +
        ",\"kind\":" + json_string(record.kind, "kind") +
        ",\"label_issuer_id\":" + std::to_string(record.label_issuer_id) +
        ",\"label_issuer_public_key_fingerprint\":" +
            json_string(record.label_issuer_public_key_fingerprint,
                        "label issuer fingerprint") +
        ",\"label_issuer_reference\":" +
            json_string(record.label_issuer_reference, "label issuer reference") +
        ",\"run_id\":" + json_string(record.run_id, "run ID") +
        ",\"schema_version\":1" +
        ",\"source_instance\":" +
            json_string(record.source_instance, "source instance") +
        ",\"stage_a_semantic_digest\":" +
            json_string(record.stage_a_semantic_digest,
                        "Stage-A semantic digest") +
        ",\"stage_a_wire_sha256\":" +
            json_string(record.stage_a_wire_sha256, "Stage-A wire hash") +
        ",\"stage_b_authorization_wire_sha256\":" +
            json_string(record.stage_b_authorization_wire_sha256,
                        "Stage-B wire hash") +
        ",\"successor_bundle_sha256\":" +
            json_string(record.successor_bundle_sha256,
                        "successor bundle hash") +
        ",\"successor_policy_snapshot_id\":" +
            json_string(record.successor_policy_snapshot_id,
                        "successor policy snapshot ID") +
        "}\n";
}

} // namespace hotstuff
