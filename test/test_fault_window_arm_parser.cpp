// Test-only inclusion exposes the otherwise translation-unit-private parser
// without adding a production header or callable API.
#define KAURI_ADAPTATION_MANAGER_TESTING 1
#include "../examples/adaptation_manager.cpp"

#include <string>

#include "catch.hpp"

namespace
{

constexpr char kDigestA[] =
    "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa";
constexpr char kDigestB[] =
    "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb";
constexpr char kDigestC[] =
    "cccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccc";
constexpr char kDigestD[] =
    "dddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddd";
constexpr char kDigestE[] =
    "eeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeee";

std::string replace_once(std::string value, const std::string &from,
                         const std::string &to);

FaultWindowArmBindings bindings()
{
    FaultWindowArmBindings result;
    result.schema_version = 1;
    result.domain = "kauri-focused-fault-window-arm-v1";
    result.run_id = "run-v4";
    result.profile_id = "n7-f2-q5-two-crash-pair-smoke-v4";
    result.profile_sha256 = kDigestA;
    result.topology_proof_sha256 = kDigestB;
    result.request_sha256 = kDigestC;
    result.epoch_number = 0;
    result.epoch_digest = kDigestD;
    result.prefault_tree_id = 6;
    result.required_tree_positions = 6;
    result.tree_count = 7;
    return result;
}

std::string canonical_arm()
{
    return std::string{"{\"epoch_digest\":\""} + kDigestD +
        "\",\"epoch_number\":0,\"evidence_start_monotonic_ns\":42,"
        "\"fault_receipt_sha256\":\"" + kDigestE +
        "\",\"kind\":\"kauri-focused-fault-window-arm-v1\","
        "\"prefault_tree_id\":6,\"profile_id\":\"n7-f2-q5-two-crash-pair-smoke-v4\","
        "\"profile_sha256\":\"" + kDigestA +
        "\",\"request_sha256\":\"" + kDigestC +
        "\",\"required_tree_ids\":[6,0,1,2,3,4],"
        "\"required_tree_positions\":6,\"run_id\":\"run-v4\",\"schema_version\":1,"
        "\"topology_proof_sha256\":\"" + kDigestB + "\"}\n";
}

FaultWindowArmBindings v2_bindings()
{
    auto result = bindings();
    result.schema_version = 2;
    result.domain = "kauri-focused-fault-window-arm-v2";
    result.timeout_evidence_basis = "exact_timeout_attempt_id_v1";
    result.required_observation_schema = 3;
    result.clock_domain = "same_host_clock_monotonic_raw";
    return result;
}

std::string canonical_v2_arm()
{
    return std::string{"{\"clock_domain\":\"same_host_clock_monotonic_raw\",\"epoch_digest\":\""} + kDigestD +
        "\",\"epoch_number\":0,\"evidence_start_monotonic_ns\":42,\"fault_receipt_sha256\":\"" + kDigestE +
        "\",\"kind\":\"kauri-focused-fault-window-arm-v2\",\"prefault_tree_id\":6,\"profile_id\":\"n7-f2-q5-two-crash-pair-smoke-v4\",\"profile_sha256\":\"" + kDigestA +
        "\",\"request_sha256\":\"" + kDigestC +
        "\",\"required_observation_schema\":3,\"required_tree_ids\":[6,0,1,2,3,4],\"required_tree_positions\":6,\"run_id\":\"run-v4\",\"schema_version\":2,\"timeout_evidence_basis\":\"exact_timeout_attempt_id_v1\",\"topology_proof_sha256\":\"" + kDigestB + "\"}\n";
}

FaultWindowArmBindings v3_bindings()
{
    auto result = v2_bindings();
    result.schema_version = 3;
    result.domain = "kauri-focused-fault-window-arm-v3";
    result.snapshot_evidence_basis = "exact_post_fault_attempt_start_v1";
    return result;
}

std::string canonical_v3_arm()
{
    return std::string{"{\"clock_domain\":\"same_host_clock_monotonic_raw\",\"epoch_digest\":\""} + kDigestD +
        "\",\"epoch_number\":0,\"evidence_start_monotonic_ns\":42,\"fault_receipt_sha256\":\"" + kDigestE +
        "\",\"kind\":\"kauri-focused-fault-window-arm-v3\",\"prefault_tree_id\":6,\"profile_id\":\"n7-f2-q5-two-crash-pair-smoke-v4\",\"profile_sha256\":\"" + kDigestA +
        "\",\"request_sha256\":\"" + kDigestC +
        "\",\"required_observation_schema\":3,\"required_tree_ids\":[6,0,1,2,3,4],\"required_tree_positions\":6,\"run_id\":\"run-v4\",\"schema_version\":3,\"snapshot_evidence_basis\":\"exact_post_fault_attempt_start_v1\",\"timeout_evidence_basis\":\"exact_timeout_attempt_id_v1\",\"topology_proof_sha256\":\"" + kDigestB + "\"}\n";
}

FaultWindowArmBindings v4_bindings()
{
    auto result = v3_bindings();
    result.schema_version = 4;
    result.domain = "kauri-focused-fault-window-arm-v4";
    result.selection_cardinality_policy =
        "all_guarded_up_to_fault_bound_v1";
    return result;
}

std::string canonical_v4_arm()
{
    auto result = canonical_v3_arm();
    result = replace_once(result,
                          "kauri-focused-fault-window-arm-v3",
                          "kauri-focused-fault-window-arm-v4");
    result = replace_once(
        result, "\"schema_version\":3,",
        "\"schema_version\":4,\"selection_cardinality_policy\":"
        "\"all_guarded_up_to_fault_bound_v1\",");
    return result;
}

FaultWindowArmBindings n31_bindings()
{
    auto result = bindings();
    result.profile_id = "n31-f5-q21-three-crash-pair-v4";
    result.epoch_number = 0;
    result.prefault_tree_id = 20;
    result.required_tree_positions = 16;
    result.tree_count = 31;
    return result;
}

FaultWindowArmBindings n31_v4_bindings()
{
    auto result = v4_bindings();
    result.profile_id = "n31-f5-q21-three-crash-pair-v12";
    result.prefault_tree_id = 20;
    result.required_tree_positions = 31;
    result.tree_count = 31;
    return result;
}

const std::string &full_n31_prefix()
{
    static const std::string value =
        "[20,21,22,23,24,25,26,27,28,29,30,0,1,2,3,4,5,6,7,8,9,"
        "10,11,12,13,14,15,16,17,18,19]";
    return value;
}

std::string canonical_n31_v4_arm()
{
    auto result = canonical_v4_arm();
    result = replace_once(
        result,
        "n7-f2-q5-two-crash-pair-smoke-v4",
        "n31-f5-q21-three-crash-pair-v12");
    result = replace_once(
        result, "\"prefault_tree_id\":6", "\"prefault_tree_id\":20");
    result = replace_once(
        result, "[6,0,1,2,3,4]", full_n31_prefix());
    result = replace_once(
        result,
        "\"required_tree_positions\":6",
        "\"required_tree_positions\":31");
    return result;
}

void require_invalid(const std::string &document)
{
    CHECK_THROWS_AS(
        FaultWindowArmJsonParser(document, bindings()).parse(),
        std::invalid_argument);
}

void require_invalid(const std::string &document,
                     const FaultWindowArmBindings &arm_bindings)
{
    CHECK_THROWS_AS(
        FaultWindowArmJsonParser(document, arm_bindings).parse(),
        std::invalid_argument);
}

std::string replace_once(std::string value, const std::string &from,
                         const std::string &to)
{
    const auto offset = value.find(from);
    REQUIRE(offset != std::string::npos);
    value.replace(offset, from.size(), to);
    return value;
}

} // namespace

TEST_CASE("fault-window parser accepts exact canonical publisher bytes",
          "[adaptive-v2][fault-window-arm][v4][parser]")
{
    const auto text = canonical_arm();
    const auto document = FaultWindowArmJsonParser(text, bindings()).parse();

    CHECK(document.arm.predecessor_epoch_number == 0);
    CHECK(document.arm.evidence_start_monotonic_ns == 42);
    CHECK(document.arm.prefault_tree_id == 6);
    CHECK(document.arm.required_tree_ids ==
          std::vector<std::uint32_t>{6, 0, 1, 2, 3, 4});
    CHECK(document.event.fault_window_arm_sha256 == fault_window_sha256(text));
}

TEST_CASE("fault-window v2 parser binds exact evidence fields",
          "[adaptive-v2][fault-window-arm][v6][parser]")
{
    const auto text = canonical_v2_arm();
    const auto document = FaultWindowArmJsonParser(text, v2_bindings()).parse();
    CHECK(document.arm.evidence_basis ==
          hotstuff::AdaptiveV2FaultWindowEvidenceBasis::exact_timeout_attempt_id_v1);
    CHECK(document.event.clock_domain == "same_host_clock_monotonic_raw");
    CHECK(document.event.required_observation_schema == 3);
    CHECK(document.event.timeout_evidence_basis == "exact_timeout_attempt_id_v1");
    for (const auto &bad : {replace_once(text, "same_host_clock_monotonic_raw", "unknown"),
                            replace_once(text, "\"required_observation_schema\":3", "\"required_observation_schema\":2"),
                            replace_once(text, "exact_timeout_attempt_id_v1", "unknown"),
                            replace_once(text, "\"schema_version\":2,", "\"schema_version\":2,\"snapshot_evidence_basis\":\"\",")})
    {
        require_invalid(bad, v2_bindings());
    }
    auto contaminated = v2_bindings();
    contaminated.snapshot_evidence_basis =
        "exact_post_fault_attempt_start_v1";
    require_invalid(text, contaminated);
}

TEST_CASE("fault-window v3 parser binds the causal snapshot basis",
          "[adaptive-v2][fault-window-arm][v7][parser]")
{
    const auto text = canonical_v3_arm();
    const auto document = FaultWindowArmJsonParser(text, v3_bindings()).parse();
    CHECK(document.arm.snapshot_evidence_basis ==
          hotstuff::AdaptiveV2FaultWindowSnapshotEvidenceBasis::
              exact_post_fault_attempt_start_v1);
    CHECK(document.event.snapshot_evidence_basis ==
          "exact_post_fault_attempt_start_v1");
    CHECK_THROWS_AS(FaultWindowArmJsonParser(
                        replace_once(text, "exact_post_fault_attempt_start_v1", "unknown"),
                        v3_bindings()).parse(), std::invalid_argument);
    require_invalid(replace_once(
                        text,
                        ",\"snapshot_evidence_basis\":\"exact_post_fault_attempt_start_v1\"",
                        ""),
                    v3_bindings());
    require_invalid(replace_once(
                        text,
                        ",\"schema_version\":3,\"snapshot_evidence_basis\"",
                        ",\"snapshot_evidence_basis\",\"schema_version\":3"),
                    v3_bindings());
}

TEST_CASE("fault-window v4 parser binds full guarded cohort selection",
          "[adaptive-v2][fault-window-arm][v9][parser]")
{
    const auto text = canonical_v4_arm();
    const auto document = FaultWindowArmJsonParser(
        text, v4_bindings()).parse();
    CHECK(document.arm.cardinality_policy ==
          hotstuff::AdaptiveV2FaultWindowCardinalityPolicy::
              all_guarded_up_to_fault_bound_v1);
    CHECK(document.event.selection_cardinality_policy ==
          "all_guarded_up_to_fault_bound_v1");
    require_invalid(replace_once(
                        text, "all_guarded_up_to_fault_bound_v1",
                        "exact_required_v1"),
                    v4_bindings());
    require_invalid(replace_once(
                        text,
                        ",\"selection_cardinality_policy\":"
                        "\"all_guarded_up_to_fault_bound_v1\"",
                        ""),
                    v4_bindings());

    auto contaminated = v3_bindings();
    contaminated.selection_cardinality_policy =
        "all_guarded_up_to_fault_bound_v1";
    require_invalid(canonical_v3_arm(), contaminated);
}

TEST_CASE("fault-window v1 parser rejects newer binding contamination",
          "[adaptive-v2][fault-window-arm][v4][parser][partition]")
{
    auto contaminated = bindings();
    contaminated.clock_domain = "same_host_clock_monotonic_raw";
    require_invalid(canonical_arm(), contaminated);
    contaminated = bindings();
    contaminated.required_observation_schema = 3;
    require_invalid(canonical_arm(), contaminated);
    contaminated = bindings();
    contaminated.timeout_evidence_basis = "exact_timeout_attempt_id_v1";
    require_invalid(canonical_arm(), contaminated);
    contaminated = bindings();
    contaminated.snapshot_evidence_basis =
        "exact_post_fault_attempt_start_v1";
    require_invalid(canonical_arm(), contaminated);
    contaminated = bindings();
    contaminated.selection_cardinality_policy =
        "all_guarded_up_to_fault_bound_v1";
    require_invalid(canonical_arm(), contaminated);
}

TEST_CASE("fault-window parser rejects noncanonical JSON syntax",
          "[adaptive-v2][fault-window-arm][v4][parser]")
{
    const auto canonical = canonical_arm();
    require_invalid(replace_once(canonical, "\"run-v4\"", "\"run\\\\v4\""));
    require_invalid(replace_once(canonical, "\"epoch_number\":0", "\"epoch_number\":00"));
    require_invalid(replace_once(canonical, "\"epoch_number\":0", "\"epoch_number\":true"));
    require_invalid(replace_once(canonical, "\"schema_version\":1", "\"schema_version\":01"));
    require_invalid(replace_once(canonical, "\"required_tree_ids\":[6,0,1,2,3,4]", "\"required_tree_ids\":[6,0,1,2,3,4,]"));
    require_invalid(replace_once(canonical, "\"epoch_digest\"", "\"extra\":0,\"epoch_digest\""));
    require_invalid(replace_once(canonical, "\"epoch_digest\"", "\"epoch_number\""));
    require_invalid(canonical.substr(0, canonical.size() - 1));
}

TEST_CASE("fault-window parser rejects binding and digest mutations",
          "[adaptive-v2][fault-window-arm][v4][parser]")
{
    const auto canonical = canonical_arm();
    require_invalid(replace_once(canonical, "run-v4", "other"));
    require_invalid(replace_once(canonical, "n7-f2-q5-two-crash-pair-smoke-v4", "n31-f5-q21-three-crash-pair-v4"));
    require_invalid(replace_once(canonical, kDigestA, kDigestE));
    require_invalid(replace_once(canonical, kDigestB, kDigestE));
    require_invalid(replace_once(canonical, kDigestC, kDigestE));
    require_invalid(replace_once(canonical, "\"epoch_number\":0", "\"epoch_number\":8"));
    require_invalid(replace_once(canonical, kDigestD, kDigestE));
    require_invalid(replace_once(canonical, "\"prefault_tree_id\":6", "\"prefault_tree_id\":5"));
    require_invalid(replace_once(canonical, "\"required_tree_positions\":6", "\"required_tree_positions\":2"));
    require_invalid(replace_once(canonical, "[6,0,1,2,3,4]", "[6,1,0,2,3,4]"));
    require_invalid(replace_once(canonical, kDigestE, "A" + std::string(kDigestE + 1)));
}

TEST_CASE("fault-window parser rejects duplicate and incomplete prefix evidence",
          "[adaptive-v2][fault-window-arm][v4][parser]")
{
    const auto canonical = canonical_arm();
    require_invalid(replace_once(canonical, "[6,0,1,2,3,4]", "[6,0,0,2,3,4]"));
    require_invalid(replace_once(canonical, "[6,0,1,2,3,4]", "[6,0,1,2,3]"));
    require_invalid(replace_once(canonical, "\"schema_version\":1", "\"schema_version\":1,\"schema_version\":1"));
}

TEST_CASE("fault-window parser accepts the frozen N31 cyclic prefix",
          "[adaptive-v2][fault-window-arm][v4][parser][n31][archive]")
{
    auto text = canonical_arm();
    text = replace_once(text, "\"prefault_tree_id\":6", "\"prefault_tree_id\":20");
    text = replace_once(text,
                        "\"profile_id\":\"n7-f2-q5-two-crash-pair-smoke-v4\"",
                        "\"profile_id\":\"n31-f5-q21-three-crash-pair-v4\"");
    text = replace_once(text, "[6,0,1,2,3,4]", "[20,21,22,23,24,25,26,27,28,29,30,0,1,2,3,4]");
    text = replace_once(text, "\"required_tree_positions\":6", "\"required_tree_positions\":16");
    const auto document = FaultWindowArmJsonParser(text, n31_bindings()).parse();

    CHECK(document.arm.required_tree_ids == std::vector<std::uint32_t>{
        20, 21, 22, 23, 24, 25, 26, 27,
        28, 29, 30, 0, 1, 2, 3, 4});
    CHECK_THROWS_AS(
        FaultWindowArmJsonParser(
            replace_once(text,
                         "[20,21,22,23,24,25,26,27,28,29,30,0,1,2,3,4]",
                         "[20,0,1,2,3,4,5,6,7,8,9,10,11,12,13,14]"),
            n31_bindings()).parse(),
        std::invalid_argument);
}

TEST_CASE(
    "fault-window schema4 parser binds the complete N31 cyclic prefix",
    "[adaptive-v2][fault-window-arm][v12][parser][n31][schema4]")
{
    const auto text = canonical_n31_v4_arm();
    const auto document = FaultWindowArmJsonParser(
        text, n31_v4_bindings()).parse();

    std::vector<std::uint32_t> expected;
    expected.reserve(31);
    for (std::uint32_t offset = 0; offset < 31; ++offset)
        expected.push_back((20U + offset) % 31U);
    CHECK(document.arm.required_tree_ids == expected);
    CHECK(document.arm.cardinality_policy ==
          hotstuff::AdaptiveV2FaultWindowCardinalityPolicy::
              all_guarded_up_to_fault_bound_v1);

    const auto shortened = replace_once(
        text,
        full_n31_prefix(),
        "[20,21,22,23,24,25,26,27,28,29,30,0,1,2,3,4,5,6,7,8,9,"
        "10,11,12,13,14,15,16,17,18]");
    require_invalid(shortened, n31_v4_bindings());

    const auto oversized = replace_once(
        text,
        full_n31_prefix(),
        "[20,21,22,23,24,25,26,27,28,29,30,0,1,2,3,4,5,6,7,8,9,"
        "10,11,12,13,14,15,16,17,18,19,20]");
    require_invalid(oversized, n31_v4_bindings());

    require_invalid(
        replace_once(text, ",4,5,6,", ",4,4,6,"),
        n31_v4_bindings());
    require_invalid(
        replace_once(text, ",4,5,6,", ",4,6,7,"),
        n31_v4_bindings());
    require_invalid(
        replace_once(text, ",29,30,0,1,", ",29,30,1,0,"),
        n31_v4_bindings());
    require_invalid(
        replace_once(
            text,
            full_n31_prefix(),
            "[20,0,1,2,3,4,5,6,7,8,9,10,11,12,13,14,15,16,17,18,"
            "19,21,22,23,24,25,26,27,28,29,30]"),
        n31_v4_bindings());
}
