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
        "\",\"required_tree_ids\":[6,0,1,2,3,4],"
        "\"required_tree_positions\":6,\"request_sha256\":\"" +
        kDigestC + "\",\"run_id\":\"run-v4\",\"schema_version\":1,"
        "\"topology_proof_sha256\":\"" + kDigestB + "\"}\n";
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

void require_invalid(const std::string &document)
{
    CHECK_THROWS_AS(
        FaultWindowArmJsonParser(document, bindings()).parse(),
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
          "[adaptive-v2][fault-window-arm][v4][parser][n31]")
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
