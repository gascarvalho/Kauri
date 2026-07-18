#include <cstdint>
#include <vector>

#include "catch.hpp"
#include "hotstuff/simple_reputation.h"
#include "hotstuff/type.h"

/*
 * Bounded reputation prototype contract
 * -------------------------------------
 * This component records only one reporter's observation of one target. A
 * response rewards the target, a timeout penalizes the target, and neither
 * event changes consensus or quorum state.
 *
 * The test uses the public production seam directly so its class layout and
 * behavior cannot drift from the implementation.
 */

namespace
{

using hotstuff::ReplicaID;
using hotstuff::SimpleReputation;
using hotstuff::SimpleReputationDisposition;
using hotstuff::SimpleReputationOutcome;

constexpr ReplicaID kReporter = 0;
constexpr ReplicaID kTarget = 1;
constexpr ReplicaID kUnobserved = 2;
constexpr ReplicaID kUnknown = 99;

std::vector<ReplicaID> membership()
{
    return {kReporter, kTarget, kUnobserved};
}

void require_zero_scores(const SimpleReputation &subject)
{
    REQUIRE(subject.score(kReporter) == 0);
    REQUIRE(subject.score(kTarget) == 0);
    REQUIRE(subject.score(kUnobserved) == 0);
}

} // namespace

TEST_CASE(
    "simple reputation starts every fixed member at zero",
    "[adaptive][reputation][prototype]")
{
    const SimpleReputation subject(membership());

    require_zero_scores(subject);
}

TEST_CASE(
    "a response rewards only the observed target",
    "[adaptive][reputation][prototype]")
{
    SimpleReputation subject(membership());

    const auto update = subject.observe_response(kReporter, kTarget);

    REQUIRE(update.disposition == SimpleReputationDisposition::applied);
    REQUIRE(update.reporter_id == kReporter);
    REQUIRE(update.target_id == kTarget);
    REQUIRE(update.outcome == SimpleReputationOutcome::response);
    REQUIRE(update.delta == 1);
    REQUIRE(update.score == 1);
    REQUIRE(subject.score(kReporter) == 0);
    REQUIRE(subject.score(kTarget) == 1);
    REQUIRE(subject.score(kUnobserved) == 0);
}

TEST_CASE(
    "a timeout penalizes only the observed target",
    "[adaptive][reputation][prototype]")
{
    SimpleReputation subject(membership());

    const auto update = subject.observe_timeout(kReporter, kTarget);

    REQUIRE(update.disposition == SimpleReputationDisposition::applied);
    REQUIRE(update.reporter_id == kReporter);
    REQUIRE(update.target_id == kTarget);
    REQUIRE(update.outcome == SimpleReputationOutcome::timeout);
    REQUIRE(update.delta == -1);
    REQUIRE(update.score == -1);
    REQUIRE(subject.score(kReporter) == 0);
    REQUIRE(subject.score(kTarget) == -1);
    REQUIRE(subject.score(kUnobserved) == 0);
}

TEST_CASE(
    "a response after a timeout corrects the same pair to net zero",
    "[adaptive][reputation][prototype][late]")
{
    SimpleReputation subject(membership());

    const auto timeout = subject.observe_timeout(kReporter, kTarget);
    const auto response = subject.observe_response(kReporter, kTarget);

    REQUIRE(timeout.reporter_id == kReporter);
    REQUIRE(timeout.target_id == kTarget);
    REQUIRE(timeout.delta == -1);
    REQUIRE(timeout.score == -1);
    REQUIRE(response.reporter_id == kReporter);
    REQUIRE(response.target_id == kTarget);
    REQUIRE(response.delta == 1);
    REQUIRE(response.score == 0);
    REQUIRE(subject.score(kTarget) == 0);
}

TEST_CASE(
    "unknown and self observations are rejected without score changes",
    "[adaptive][reputation][prototype][validation]")
{
    SimpleReputation subject(membership());

    const auto unknown_reporter =
        subject.observe_response(kUnknown, kTarget);
    const auto unknown_target =
        subject.observe_timeout(kReporter, kUnknown);
    const auto self_observation =
        subject.observe_response(kTarget, kTarget);

    REQUIRE(
        unknown_reporter.disposition ==
        SimpleReputationDisposition::unknown_reporter);
    REQUIRE(unknown_reporter.reporter_id == kUnknown);
    REQUIRE(unknown_reporter.target_id == kTarget);
    REQUIRE(unknown_reporter.delta == 0);
    REQUIRE(unknown_reporter.score == 0);
    REQUIRE(
        unknown_target.disposition ==
        SimpleReputationDisposition::unknown_target);
    REQUIRE(unknown_target.reporter_id == kReporter);
    REQUIRE(unknown_target.target_id == kUnknown);
    REQUIRE(unknown_target.delta == 0);
    REQUIRE(unknown_target.score == 0);
    REQUIRE(
        self_observation.disposition ==
        SimpleReputationDisposition::self_observation);
    REQUIRE(self_observation.reporter_id == kTarget);
    REQUIRE(self_observation.target_id == kTarget);
    REQUIRE(self_observation.delta == 0);
    REQUIRE(self_observation.score == 0);
    require_zero_scores(subject);
}
