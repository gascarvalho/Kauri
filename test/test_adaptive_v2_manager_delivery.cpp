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
    "manager joins network workers before ingress shutdown",
    "[adaptive-v2][manager-delivery][lifetime]")
{
    const auto source = read_source("examples/adaptation_manager.cpp");
    const auto dispatch = source.find("event_context_.dispatch();");
    const auto network_stop = source.find("network_.stop();", dispatch);
    const auto ingress_shutdown =
        source.find("ingress_.shutdown();", dispatch);

    REQUIRE(dispatch != std::string::npos);
    REQUIRE(network_stop != std::string::npos);
    REQUIRE(ingress_shutdown != std::string::npos);
    CHECK(dispatch < network_stop);
    CHECK(network_stop < ingress_shutdown);
}
