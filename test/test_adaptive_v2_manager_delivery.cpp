#include <cstddef>
#include <fstream>
#include <iterator>
#include <string>
#include <vector>

#include "catch.hpp"
#include "hotstuff/adaptive_v2_manager_delivery.h"

namespace
{

using hotstuff::AdaptiveV2BundleDeliveryAttempt;
using hotstuff::AdaptiveV2BundleDeliveryStatus;
using hotstuff::ReplicaID;

const std::vector<ReplicaID> kMembership{0, 1, 2, 3, 4, 5, 6};

std::string read_source(const std::string &relative_path)
{
    const std::string path =
        std::string(KAURI_PROJECT_SOURCE_DIR) + "/" + relative_path;
    std::ifstream source(path);
    REQUIRE(source.good());
    return {
        std::istreambuf_iterator<char>(source),
        std::istreambuf_iterator<char>()};
}

} // namespace

TEST_CASE(
    "one-shot delivery tolerates two unavailable N7 recipients",
    "[adaptive-v2][manager-delivery][one-shot][n7]")
{
    const std::vector<AdaptiveV2BundleDeliveryAttempt> attempts{
        {0, false}, {1, false}, {2, true}, {3, true},
        {4, true},  {5, true},  {6, true}};

    const auto result = hotstuff::assess_adaptive_v2_bundle_delivery(
        kMembership, 5, attempts);

    CHECK(result.status == AdaptiveV2BundleDeliveryStatus::assessed);
    CHECK(result.configured_recipients == 7);
    CHECK(result.attempted_recipients == 7);
    CHECK(result.successful_recipients == 5);
    CHECK(result.required_recipients == 5);
    CHECK(result.delivery_requirement_satisfied);
}

TEST_CASE(
    "one-shot delivery rejects fewer than Q successful enqueues",
    "[adaptive-v2][manager-delivery][one-shot][quorum]")
{
    const std::vector<AdaptiveV2BundleDeliveryAttempt> attempts{
        {0, false}, {1, false}, {2, false}, {3, true},
        {4, true},  {5, true},  {6, true}};

    const auto result = hotstuff::assess_adaptive_v2_bundle_delivery(
        kMembership, 5, attempts);

    CHECK(result.status == AdaptiveV2BundleDeliveryStatus::assessed);
    CHECK(result.successful_recipients == 4);
    CHECK_FALSE(result.delivery_requirement_satisfied);
}

TEST_CASE(
    "one-shot delivery requires one attempt per configured identity",
    "[adaptive-v2][manager-delivery][identity]")
{
    const std::vector<AdaptiveV2BundleDeliveryAttempt> duplicate{
        {0, false}, {1, false}, {2, true}, {3, true},
        {4, true},  {5, true},  {5, true}};
    const auto duplicate_result =
        hotstuff::assess_adaptive_v2_bundle_delivery(
            kMembership, 5, duplicate);
    CHECK(duplicate_result.status ==
          AdaptiveV2BundleDeliveryStatus::invalid_attempt_set);
    CHECK_FALSE(duplicate_result.delivery_requirement_satisfied);

    const std::vector<AdaptiveV2BundleDeliveryAttempt> incomplete{
        {0, false}, {1, false}, {2, true},
        {3, true},  {4, true},  {5, true}};
    const auto incomplete_result =
        hotstuff::assess_adaptive_v2_bundle_delivery(
            kMembership, 5, incomplete);
    CHECK(incomplete_result.status ==
          AdaptiveV2BundleDeliveryStatus::invalid_attempt_set);
    CHECK_FALSE(incomplete_result.delivery_requirement_satisfied);
}

TEST_CASE(
    "manager joins network workers before recurring session shutdown",
    "[adaptive-v2][manager-delivery][lifetime]")
{
    const auto source = read_source("examples/adaptation_manager.cpp");
    const auto dispatch = source.find("event_context_.dispatch();");
    const auto network_stop = source.find("network_.stop();", dispatch);
    const auto session_shutdown =
        source.find("session_.shutdown();", dispatch);

    REQUIRE(dispatch != std::string::npos);
    REQUIRE(network_stop != std::string::npos);
    REQUIRE(session_shutdown != std::string::npos);
    CHECK(dispatch < network_stop);
    CHECK(network_stop < session_shutdown);
}

TEST_CASE(
    "checkpoint4 v3 uses the unified facade route with timed ingress",
    "[.][intentional-red][adaptive-v3][manager-route][checkpoint4]")
{
    const auto source = read_source("examples/adaptation_manager.cpp");
    const auto v3_config_begin = source.find(
        "AdaptiveV3ManagerSessionConfig adaptive_v3_session_config(");
    const auto v3_config_end = source.find(
        "AdaptiveManagerSessionFacadeConfig", v3_config_begin);
    REQUIRE(v3_config_begin != std::string::npos);
    REQUIRE(v3_config_end != std::string::npos);
    const auto v3_config = source.substr(
        v3_config_begin, v3_config_end - v3_config_begin);
    const auto transport_begin = source.find(
        "class AdaptiveV3ManagerTransport final");
    const auto v3_manager_begin = source.find(
        "class AdaptiveV3ManagerModeState final", transport_begin);
    const auto v2_manager_begin = source.find(
        "class AdaptiveV2ManagerModeState final", v3_manager_begin);
    REQUIRE(transport_begin != std::string::npos);
    REQUIRE(v3_manager_begin != std::string::npos);
    REQUIRE(v2_manager_begin != std::string::npos);
    const auto transport = source.substr(
        transport_begin, v3_manager_begin - transport_begin);
    const auto v3_manager = source.substr(
        v3_manager_begin, v2_manager_begin - v3_manager_begin);
    // A caller cannot precompute an identity: the certified v3 successor is
    // selected and emitted by the existing AdaptationManager route.
    CHECK(source.find("activation-readiness-identity") == std::string::npos);
    // The retired side manager must disappear once v3 is held by the existing
    // manager through the explicit mode facade.
    CHECK(source.find("class AdaptiveV3AdaptationManager final") == std::string::npos);
    CHECK(source.find("class AdaptationManager final") != std::string::npos);
    CHECK(source.find("std::make_unique<AdaptationManager>") !=
          std::string::npos);
    CHECK(source.find("AdaptiveManagerSessionFacade") != std::string::npos);
    // Live E2 gating must retain raw-nanosecond precision.  A raw clock
    // divided to milliseconds would admit a sub-millisecond early boundary.
    CHECK(source.find("CLOCK_MONOTONIC_RAW") != std::string::npos);
    CHECK(source.find("manager_tick_ns") != std::string::npos);
    CHECK(source.find("1000000000ULL") != std::string::npos);
    CHECK(source.find("now.tv_nsec") != std::string::npos);
    CHECK(source.find("/ 1000000ULL") == std::string::npos);
    CHECK(source.find("100'000'000ULL") != std::string::npos);
    CHECK(source.find("5'000'000'000ULL") != std::string::npos);
    CHECK(source.find("60'000'000'000ULL") != std::string::npos);
    CHECK(source.find("65'000'000'000ULL") != std::string::npos);
    CHECK(source.find("90'000'000'000ULL") != std::string::npos);
    // CLI retry interval is milliseconds, unlike direct session fixture
    // defaults.  The live path must check then convert it to raw ns.
    CHECK(v3_config.find(
              "std::numeric_limits<std::uint64_t>::max() / 1'000'000ULL") !=
          std::string::npos);
    CHECK(v3_config.find("options.retry_interval_ticks * 1'000'000ULL") !=
          std::string::npos);
    CHECK(source.find("v3_ingest_timed_lifecycle") != std::string::npos);
    CHECK(source.find("v3_arm_hard_deadline") != std::string::npos);
    CHECK(source.find("v3_e2_eligible") != std::string::npos);
    // The one existing arm-deadline binding is the profile-provided hard
    // bound.  It is consumed as raw ns from the verified fault anchor, with
    // overflow and half-open equality rejected before the session is armed.
    CHECK(v3_manager.find("bindings.deadline_seconds") != std::string::npos);
    CHECK(v3_manager.find("fault_anchor_ns") != std::string::npos);
    CHECK(v3_manager.find("kNanosecondsPerSecond") != std::string::npos);
    CHECK(v3_manager.find("max() - duration_ns") != std::string::npos);
    CHECK(v3_manager.find("manager_tick_ns() >= hard_deadline_ns") !=
          std::string::npos);
    CHECK(source.find("read_fault_window_arm") != std::string::npos);
    CHECK(transport.find("register_handlers") != std::string::npos);
    CHECK(transport.find("send_certificate") != std::string::npos);
    CHECK(transport.find("send_bundle") != std::string::npos);
    CHECK(transport.find("AdaptiveManagerSessionFacade") == std::string::npos);
    CHECK(transport.find("AdaptiveV2TransitionPolicy") == std::string::npos);
    CHECK(transport.find("StructuredEventSink") == std::string::npos);
    CHECK(transport.find("TimerEvent") == std::string::npos);
    CHECK(v3_manager.find("AdaptiveV3ManagerTransport transport_") !=
          std::string::npos);
    // Manager-side terminal publication follows the append-only session audit,
    // so E1 and E2 each publish once and a pre-certificate deadline never
    // fabricates a readiness certificate.
    CHECK(v3_manager.find("emit_new_session_terminals") != std::string::npos);
    CHECK(v3_manager.find("emitted_session_terminals_") != std::string::npos);
    CHECK(v3_manager.find("v3_terminal_records") != std::string::npos);
    CHECK(v3_manager.find("terminal_identity = record.identity") !=
          std::string::npos);
    CHECK(v3_manager.find("terminal_bundle_digest = record.bundle_digest") !=
          std::string::npos);
    const auto acknowledged = v3_manager.find("certificate_acknowledged");
    REQUIRE(acknowledged != std::string::npos);
    CHECK(v3_manager.find("emit_new_session_terminals();", acknowledged) !=
          std::string::npos);
    // Terminal-producing callback boundaries mirror before failure or any
    // post-terminal payload dereference/audit.
    CHECK(v3_manager.find("!facade_.v3_begin_readiness(manager_tick_ns())") !=
          std::string::npos);
    CHECK(v3_manager.find("const auto certificate_identity") !=
          std::string::npos);
    CHECK(v3_manager.find("certificate_digest = certificate_digest") !=
          std::string::npos);
    CHECK(v3_manager.find("facade_.v3_status() ==\n                    hotstuff::AdaptiveV3ManagerSessionStatus::terminal") !=
          std::string::npos);
    const auto delivery_result = v3_manager.find(
        "const auto disposition = facade_.v3_record_delivery_result");
    REQUIRE(delivery_result != std::string::npos);
    const auto delivery_audit = v3_manager.find(
        "emit_readiness(std::move(event));", delivery_result);
    const auto delivery_terminal = v3_manager.find(
        "emit_new_session_terminals();", delivery_result);
    REQUIRE(delivery_audit != std::string::npos);
    REQUIRE(delivery_terminal != std::string::npos);
    CHECK(delivery_audit < delivery_terminal);
    const auto delivery_payload_copy = v3_manager.find(
        "event.canonical_wire_payload = *delivery->bytes", delivery_result - 2048);
    REQUIRE(delivery_payload_copy != std::string::npos);
    CHECK(delivery_payload_copy < delivery_result);
    CHECK(v3_manager.find("disposition ==\n                hotstuff::AdaptiveV3CertificateDeliveryDisposition::queued") !=
          std::string::npos);
    CHECK(v3_manager.find("event.disposition = \"deadline_expired\"") !=
          std::string::npos);
    CHECK(v3_manager.find("event.delivery_enqueued = enqueued") !=
          std::string::npos);
    CHECK(v3_manager.find("ManagerNetwork network_") == std::string::npos);
    const auto facade_member = v3_manager.find(
        "hotstuff::AdaptiveManagerSessionFacade facade_");
    const auto transport_member = v3_manager.find(
        "AdaptiveV3ManagerTransport transport_");
    REQUIRE(facade_member != std::string::npos);
    REQUIRE(transport_member != std::string::npos);
    CHECK(facade_member < transport_member);
    const auto transport_stop = transport.find("bool stop() noexcept");
    REQUIRE(transport_stop != std::string::npos);
    const auto callbacks_quiesced = transport.find("callbacks_ = {}", transport_stop);
    const auto network_stop = transport.find("network_.stop()", transport_stop);
    REQUIRE(callbacks_quiesced != std::string::npos);
    REQUIRE(network_stop != std::string::npos);
    CHECK(callbacks_quiesced < network_stop);
    CHECK(transport.find("~AdaptiveV3ManagerTransport") != std::string::npos);
}

TEST_CASE(
    "adaptive v3 fatal paths log bounded controller state",
    "[adaptive-v3][manager][diagnostic]")
{
    const auto source = read_source("examples/adaptation_manager.cpp");
    const auto v3_begin = source.find("class AdaptiveV3ManagerModeState final");
    const auto v2_begin = source.find("class AdaptiveV2ManagerModeState final", v3_begin);
    REQUIRE(v3_begin != std::string::npos);
    REQUIRE(v2_begin != std::string::npos);
    const auto manager = source.substr(v3_begin, v2_begin - v3_begin);
    const auto diagnostic_begin = manager.find("void fail(const char *reason)");
    const auto diagnostic_end = manager.find("void stop_runtime()", diagnostic_begin);
    REQUIRE(diagnostic_begin != std::string::npos);
    REQUIRE(diagnostic_end != std::string::npos);
    const auto diagnostic = manager.substr(
        diagnostic_begin, diagnostic_end - diagnostic_begin);

    for (const auto *field : {
             "KAURI_ADAPTIVE_V3_MANAGER fatal reason=%s",
             "status=%u", "next_policy=%zu", "ready_members=%zu",
             "total_members=%zu", "operational_ready=%d",
             "ledger_high_watermark=%llu", "baseline_frozen=%d",
             "baseline_cutoff=%llu", "current_cutoff=%llu",
             "controller_failure_stage=%u", "selection_status=%u",
             "factory_status=%u", "terminal_count=%zu",
             "terminal_cycle=%llu", "terminal_reason=%u",
             "fault_window_armed=%d",
             "fault_window_arm_timer_pending=%d"})
    {
        CAPTURE(field);
        CHECK(diagnostic.find(field) != std::string::npos);
    }
    CHECK(diagnostic.find("private_key") == std::string::npos);
    CHECK(diagnostic.find("certificate") == std::string::npos);
    CHECK(diagnostic.find("canonical_bytes") == std::string::npos);
    CHECK(manager.find("fail(\"fault_window_arm_session_rejected\")") !=
          std::string::npos);
    CHECK(manager.find(
              "KAURI_ADAPTIVE_V3_MANAGER state=%s cutoff=%llu") !=
          std::string::npos);
    CHECK(manager.find(
              "status == AdaptiveV2ManagerControllerStatus::unhealthy") !=
          std::string::npos);
    CHECK(manager.find("fail(\"controller_unhealthy\")") !=
          std::string::npos);
    CHECK(manager.find("resolved_v3_transition_policy") !=
          std::string::npos);
    CHECK(manager.find(
              "request.resolve_containment_roots_from_predecessor") !=
          std::string::npos);
    CHECK(manager.find("facade_.begin_cycle(*policy)") !=
          std::string::npos);
    CHECK(manager.find(
              "fail(\"fault_window_hard_deadline_session_rejected\")") !=
          std::string::npos);
    CHECK(manager.find("fail(\"fault_window_arm_acquisition_deadline\")") !=
          std::string::npos);
    CHECK(manager.find("fail(\"transport_fatal\")") != std::string::npos);
}

TEST_CASE(
    "fatal manager ingress logs one bounded diagnostic before shutdown",
    "[adaptive-v2][manager-ingress][diagnostic]")
{
    const auto source = read_source("examples/adaptation_manager.cpp");
    const auto diagnostic_begin = source.find(
        "void log_ingress_failure(");
    const auto diagnostic_end = source.find(
        "template <typename Message", diagnostic_begin);
    REQUIRE(diagnostic_begin != std::string::npos);
    REQUIRE(diagnostic_end != std::string::npos);
    const auto diagnostic = source.substr(
        diagnostic_begin, diagnostic_end - diagnostic_begin);

    for (const auto *field : {
             "kind=%s", "status=%s", "status_code=%u", "source=%u",
             "audit_readiness_wire_rejections=%llu",
             "audit_lifecycle_wire_rejections=%llu",
             "audit_evidence_wire_rejections=%llu",
             "audit_nonmember_rejections=%llu",
             "audit_spoofed_source_rejections=%llu",
             "audit_state_rejections=%llu",
             "audit_evidence_sequence_rejections=%llu",
             "audit_lifecycle_fence_mismatch_rejections=%llu",
             "audit_lifecycle_quota_rejections=%llu",
             "audit_capacity_failures=%llu",
             "audit_corroboration_threshold=%zu",
             "audit_pending_facts=%zu",
             "audit_pending_associations=%zu",
             "audit_reporter_causal_retained_proposals=%zu",
             "audit_reporter_causal_open_reporters=%zu",
             "lifecycle_quarantined_records=%zu",
             "lifecycle_quarantined_bytes=%zu",
             "lifecycle_reporter_queues=%zu",
             "lifecycle_signer_entries=%zu",
             "lifecycle_deduplication_entries=%zu",
             "lifecycle_sources=%zu",
             "lifecycle_duplicate_observations=%llu",
             "lifecycle_applied_notices=%llu",
             "lifecycle_quarantine_quota_rejections=%llu",
             "lifecycle_capacity_failures=%llu",
             "lifecycle_healthy=%d",
             "lifecycle_stopped=%d",
             "ledger_accepted=%zu",
             "ledger_rejected=%zu",
             "ledger_high_watermark=%llu"})
    {
        CAPTURE(field);
        CHECK(diagnostic.find(field) != std::string::npos);
    }
    CHECK(diagnostic.find("HOTSTUFF_LOG_WARN") != std::string::npos);
    CHECK(diagnostic.find("options_") == std::string::npos);
    CHECK(diagnostic.find("private_key") == std::string::npos);
    CHECK(diagnostic.find("certificate") == std::string::npos);
    CHECK(diagnostic.find("canonical_bytes") == std::string::npos);

    const auto ingest = source.find("void ingest(");
    const auto log = source.find("log_ingress_failure(", ingest);
    const auto fail = source.find(
        "fail(\"manager_ingress_unhealthy\")", ingest);
    REQUIRE(ingest != std::string::npos);
    REQUIRE(log != std::string::npos);
    REQUIRE(fail != std::string::npos);
    CHECK(log < fail);

    const auto handlers = source.find("void register_handlers()");
    REQUIRE(handlers != std::string::npos);
    CHECK(source.find("ingest(\"readiness\"", handlers) !=
          std::string::npos);
    CHECK(source.find("ingest(\"lifecycle\"", handlers) !=
          std::string::npos);
    CHECK(source.find("ingest(\"evidence\"", handlers) !=
          std::string::npos);
}
