// Test-only inclusion exposes the otherwise translation-unit-private parser
// without adding a production header or callable API.
#define KAURI_ADAPTATION_MANAGER_TESTING 1
#include "../examples/adaptation_manager.cpp"

#include <string>
#include <iterator>
#include <memory>
#include <vector>

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

template <typename Result, typename Operation>
Result with_arguments(
    std::vector<std::string> arguments,
    Operation operation)
{
    std::vector<char *> raw;
    raw.reserve(arguments.size());
    for (auto &argument : arguments)
        raw.push_back(argument.data());
    return operation(static_cast<int>(raw.size()), raw.data());
}

AdaptiveV3ManagerOptions parse_adaptive_v3_test_options(
    std::vector<std::string> arguments)
{
    optind = 1;
#if defined(__APPLE__)
    optreset = 1;
#endif
    return with_arguments<AdaptiveV3ManagerOptions>(
        std::move(arguments),
        [](int argc, char **argv) {
            return parse_adaptive_v3_options(argc, argv);
        });
}

struct AdaptiveV3CliFixture
{
    std::vector<std::unique_ptr<hotstuff::PrivKeyBLS>> readiness_keys;
    std::vector<std::pair<ReplicaID, hotstuff::PubKeyBLS>> members;
    std::vector<std::string> replica_certificates;
    std::string manager_private_key;
    std::string manager_certificate;

    AdaptiveV3CliFixture()
    {
        auto tls_key = salticidae::PKey::create_privkey_rsa(1024);
        manager_private_key = salticidae::get_hex(tls_key.get_privkey_der());
        manager_certificate = salticidae::get_hex(
            salticidae::X509::create_self_signed_from_pubkey(
                tls_key, "PT", "adaptive-v3-manager").get_der());
        for (ReplicaID id = 0; id < 7; ++id)
        {
            auto readiness_key = std::make_unique<hotstuff::PrivKeyBLS>();
            readiness_key->from_rand();
            members.emplace_back(id, hotstuff::PubKeyBLS(*readiness_key));
            readiness_keys.emplace_back(std::move(readiness_key));
            const auto common_name =
                std::string{"adaptive-v3-replica-"} + std::to_string(id);
            replica_certificates.push_back(salticidae::get_hex(
                salticidae::X509::create_self_signed_from_pubkey(
                    tls_key, "PT", common_name.c_str()).get_der()));
        }
    }

    std::string member(ReplicaID id) const
    {
        return std::to_string(id) + "," +
            salticidae::get_hex(members.at(id).second.to_bytes());
    }

    std::vector<std::string> arguments() const
    {
        std::vector<std::string> result{
            "adaptation-manager",
            "--protocol-mode", "adaptive_v3",
            "--listen", "127.0.0.1:19000"};
        for (ReplicaID id = 0; id < 7; ++id)
        {
            result.insert(result.end(), {
                "--replica",
                std::to_string(id) + ",127.0.0.1:" +
                    std::to_string(19001 + id) + "," +
                    replica_certificates.at(id),
                "--activation-readiness-member", member(id)});
        }
        result.insert(result.end(), {
            "--activation-readiness-release-count", "5",
            "--activation-readiness-maximum-delivery-attempts", "2",
            "--activation-readiness-retry-interval-ticks", "10",
            "--tls-privkey", manager_private_key,
            "--tls-cert", manager_certificate,
            "--issuer-id", "17",
            "--issuer-private-key",
            "4aede145d13021fb43c938bced67511a7740c05786d3e0b94ffbdaa7f15afc57",
            "--transition-request",
            R"({"policy_intent":"fault_containment","evidence_window_rule":"fresh_exact_predecessor_after_common_commit","transition_artifact_id":"facade-fixture","bundle_path":"transitions/facade-fixture/successor.bundle","evidence_snapshot_path":"transitions/facade-fixture/evidence-snapshot.json","predecessor_epoch_number":0,"successor_epoch_number":1,"minimum_predecessor_residency_ms":0,"policy_parameters":{"containment_baseline_roots":[{"tree_id":0,"replica_id":0},{"tree_id":1,"replica_id":1},{"tree_id":2,"replica_id":2},{"tree_id":3,"replica_id":3},{"tree_id":4,"replica_id":4}]}})",
            "--bundle-output", "/tmp/transitions/facade-fixture/successor.bundle",
            "--structured-event-run-id", "cert13-m1",
            "--structured-event-source-instance", "manager-1",
            "--structured-event-output", "/tmp/cert13-m1-unused.ndjson"});
        return result;
    }
};

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

TEST_CASE(
    "adaptive-v3 manager CLI accepts only its canonical membership schema",
    "[cert13][m1][adaptive-v3][manager][cli][parser]")
{
    AdaptiveV3CliFixture fixture;
    const auto options = parse_adaptive_v3_test_options(
        fixture.arguments());
    REQUIRE(options.replicas.size() == 7);
    REQUIRE(options.readiness_membership.size() == 7);
    CHECK(options.required_release_count == 5);
    CHECK(options.maximum_delivery_attempts == 2);
    CHECK(options.retry_interval_ticks == 10);
    CHECK(options.manager.responsiveness_policy.reputation_mechanism ==
          hotstuff::ReputationMechanism::responsiveness);
    CHECK_NOTHROW(network_config(options));

    auto latency_arguments = fixture.arguments();
    latency_arguments.insert(
        latency_arguments.end(),
        {"--reputation-mechanism", "latency-priority",
         "--responsiveness-policy-version",
         "kauri-latency-priority-v1"});
    const auto latency_options = parse_adaptive_v3_test_options(
        latency_arguments);
    CHECK(latency_options.manager.responsiveness_policy
              .reputation_mechanism ==
          hotstuff::ReputationMechanism::latency_priority);
    CHECK(latency_options.manager.responsiveness_policy.policy_version ==
          "kauri-latency-priority-v1");
    CHECK(manager_controller_config(latency_options.manager)
              .selection.responsiveness_policy.reputation_mechanism ==
          hotstuff::ReputationMechanism::latency_priority);

    auto invalid_mechanism = fixture.arguments();
    invalid_mechanism.insert(
        invalid_mechanism.end(),
        {"--reputation-mechanism", "fastest-self-reported"});
    CHECK_THROWS_AS(
        parse_adaptive_v3_test_options(invalid_mechanism),
        std::invalid_argument);

    auto precomputed_identity = fixture.arguments();
    precomputed_identity.insert(
        precomputed_identity.end(),
        {"--activation-readiness-identity", "00"});
    CHECK_THROWS(parse_adaptive_v3_test_options(precomputed_identity));

    CHECK(with_arguments<bool>(
        {"adaptation-manager", "--protocol-mode", "adaptive_v3"},
        [](int argc, char **argv) {
            return adaptive_v3_requested(argc, argv);
        }));
    CHECK_FALSE(with_arguments<bool>(
        {"adaptation-manager", "--help"},
        [](int argc, char **argv) {
            return adaptive_v3_requested(argc, argv);
        }));
    CHECK_THROWS_AS(with_arguments<bool>(
        {"adaptation-manager", "--protocol-mode", "adaptive_v2"},
        [](int argc, char **argv) {
            return adaptive_v3_requested(argc, argv);
        }), std::invalid_argument);

    // The common parser remains v3-only when --protocol-mode is present;
    // its v2 route must not acquire the v3 readiness policy by accident.
    CHECK_THROWS_AS(
        parse_adaptive_v3_test_options(
            {"adaptation-manager", "--protocol-mode", "adaptive_v2"}),
        std::invalid_argument);
    CHECK_THROWS_AS(with_arguments<bool>(
        {"adaptation-manager", "--protocol-mode=adaptive_v3",
         "--protocol-mode", "adaptive_v3"},
        [](int argc, char **argv) {
            return adaptive_v3_requested(argc, argv);
        }), std::invalid_argument);

    CHECK_THROWS_AS(
        parse_adaptive_v3_readiness_member(
            std::string{"00,"} + fixture.member(0).substr(2)),
        std::invalid_argument);
    auto uppercase = fixture.member(0);
    const auto letter = uppercase.find_first_of("abcdef");
    REQUIRE(letter != std::string::npos);
    uppercase[letter] = static_cast<char>(
        std::toupper(static_cast<unsigned char>(uppercase[letter])));
    CHECK_THROWS_AS(
        parse_adaptive_v3_readiness_member(uppercase),
        std::invalid_argument);

    auto mixed = fixture.arguments();
    mixed.insert(mixed.end(), {"--required-nonresponsive", "2"});
    const auto common_v3 = parse_adaptive_v3_test_options(mixed);
    CHECK(common_v3.manager.required_nonresponsive == 2);

    auto noncanonical_release = fixture.arguments();
    const auto release_option = std::find(
        noncanonical_release.begin(), noncanonical_release.end(),
        "--activation-readiness-release-count");
    REQUIRE(release_option != noncanonical_release.end());
    REQUIRE(std::next(release_option) != noncanonical_release.end());
    *std::next(release_option) = "05";
    CHECK_THROWS_AS(
        parse_adaptive_v3_test_options(noncanonical_release),
        std::invalid_argument);

}

TEST_CASE(
    "adaptive-v3 manager rejects duplicate BLS public identities",
    "[.][intentional-red][cert13][adaptive-v3][manager][cli][parser]")
{
    AdaptiveV3CliFixture fixture;
    auto duplicate_key = fixture.arguments();
    std::size_t member_ordinal = 0;
    for (std::size_t index = 0; index + 1 < duplicate_key.size(); ++index)
    {
        if (duplicate_key[index] != "--activation-readiness-member")
            continue;
        if (member_ordinal == 6)
            duplicate_key[index + 1] =
                std::string{"6,"} + fixture.member(5).substr(2);
        ++member_ordinal;
    }
    REQUIRE(member_ordinal == 7);
    CHECK_THROWS_AS(parse_adaptive_v3_test_options(duplicate_key),
                    std::invalid_argument);
}

TEST_CASE(
    "adaptive-v3 TLS certificate identity maps to one signed member source",
    "[cert13][m1][adaptive-v3][manager][transport][tls]")
{
    AdaptiveV3CliFixture fixture;
    const auto options = parse_adaptive_v3_test_options(
        fixture.arguments());
    std::unordered_map<PeerId, ReplicaID> peer_to_replica;
    for (const auto &replica : options.replicas)
        peer_to_replica.emplace(replica.peer_id, replica.replica_id);

    CHECK(lookup_adaptive_v3_tls_source(
              peer_to_replica, options.replicas[3].peer_id) ==
          std::optional<ReplicaID>{3});
    CHECK_FALSE(lookup_adaptive_v3_tls_source(
        peer_to_replica, options.local_peer_id).has_value());
}
