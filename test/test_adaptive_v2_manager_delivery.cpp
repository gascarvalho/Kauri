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
