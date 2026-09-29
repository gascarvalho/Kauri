#include "catch.hpp"

#include <stdexcept>
#include <string>

#include "hotstuff/operator_capacity_consumption_record.h"

namespace
{

hotstuff::OperatorCapacityConsumptionRecord example()
{
    hotstuff::OperatorCapacityConsumptionRecord record;
    record.run_id = "run-1";
    record.source_instance = "manager-1";
    record.stage_a_wire_sha256 = std::string(64, 'a');
    record.stage_a_semantic_digest = std::string(64, 'b');
    record.stage_b_authorization_wire_sha256 = std::string(64, 'c');
    record.label_issuer_id = 74;
    record.label_issuer_reference = "label-issuer";
    record.label_issuer_public_key_fingerprint = std::string(64, 'd');
    record.epoch_change_issuer_id = 73;
    record.arm = hotstuff::OperatorCapacityArm::fast_priority_treatment;
    record.capacity_digest = std::string(64, 'e');
    record.epoch0_digest = std::string(64, 'f');
    record.epoch0_topology_digest = std::string(64, 'a');
    record.baseline_snapshot_id = std::string(64, 'b');
    record.baseline_evidence_cutoff = 62;
    record.decision_monotonic_raw_ns = 500;
    record.hard_deadline_monotonic_raw_ns = 1000;
    record.successor_policy_snapshot_id = std::string(64, 'c');
    record.successor_bundle_sha256 = std::string(64, 'd');
    return record;
}

} // namespace

TEST_CASE("operator-capacity consumption record has stable exact JSON bytes",
          "[operator-capacity][consumption]")
{
    const auto record = example();
    const auto actual =
        hotstuff::serialize_operator_capacity_consumption_record(record);
    const auto expected =
        std::string("{\"arm\":\"fast_priority_treatment\","
                    "\"baseline_evidence_cutoff\":62,"
                    "\"baseline_snapshot_id\":\"") +
        std::string(64, 'b') +
        "\",\"capacity_digest\":\"" + std::string(64, 'e') +
        "\",\"decision_monotonic_raw_ns\":500,\"epoch0_digest\":\"" +
        std::string(64, 'f') +
        "\",\"epoch0_topology_digest\":\"" + std::string(64, 'a') +
        "\",\"epoch_change_issuer_id\":73,"
        "\"hard_deadline_monotonic_raw_ns\":1000,"
        "\"kind\":\"kauri-operator-capacity-consumption-v1\","
        "\"label_issuer_id\":74,"
        "\"label_issuer_public_key_fingerprint\":\"" +
        std::string(64, 'd') +
        "\",\"label_issuer_reference\":\"label-issuer\","
        "\"run_id\":\"run-1\",\"schema_version\":1,"
        "\"source_instance\":\"manager-1\","
        "\"stage_a_semantic_digest\":\"" + std::string(64, 'b') +
        "\",\"stage_a_wire_sha256\":\"" + std::string(64, 'a') +
        "\",\"stage_b_authorization_wire_sha256\":\"" +
        std::string(64, 'c') +
        "\",\"successor_bundle_sha256\":\"" + std::string(64, 'd') +
        "\",\"successor_policy_snapshot_id\":\"" +
        std::string(64, 'c') + "\"}\n";
    CHECK(actual == expected);

    auto sham = record;
    sham.arm = hotstuff::OperatorCapacityArm::exact_copy_sham;
    CHECK(hotstuff::serialize_operator_capacity_consumption_record(sham).find(
              "\"arm\":\"exact_copy_sham\"") != std::string::npos);
}

TEST_CASE("operator-capacity consumption record fails closed on malformed identity",
          "[operator-capacity][consumption]")
{
    auto record = example();
    record.stage_a_wire_sha256[0] = 'G';
    CHECK_THROWS_AS(hotstuff::serialize_operator_capacity_consumption_record(
                        record), std::invalid_argument);
    record = example();
    record.hard_deadline_monotonic_raw_ns = 500;
    CHECK_THROWS_AS(hotstuff::serialize_operator_capacity_consumption_record(
                        record), std::invalid_argument);
    record = example();
    record.arm = static_cast<hotstuff::OperatorCapacityArm>(99);
    CHECK_THROWS_AS(hotstuff::serialize_operator_capacity_consumption_record(
                        record), std::invalid_argument);
    record = example();
    record.run_id = "bad\nrun";
    CHECK_THROWS_AS(hotstuff::serialize_operator_capacity_consumption_record(
                        record), std::invalid_argument);
    record = example();
    record.label_issuer_reference = "label\"issuer";
    CHECK(hotstuff::serialize_operator_capacity_consumption_record(record).find(
              "label\\\"issuer") != std::string::npos);
}
