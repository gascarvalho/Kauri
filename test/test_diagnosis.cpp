#include <algorithm>
#include <cstdint>
#include <string>
#include <vector>

#include "catch.hpp"
#include "hotstuff/diagnosis.h"

namespace
{

using hotstuff::CompatibleHypothesis;
using hotstuff::ConfigurationId;
using hotstuff::DataStream;
using hotstuff::DiagnosisBuildStatus;
using hotstuff::DiagnosisLimits;
using hotstuff::DiagnosisSnapshot;
using hotstuff::DiagnosticClassification;
using hotstuff::DiagnosticObservation;
using hotstuff::DiagnosticOutcome;
using hotstuff::ExpectedMessageType;
using hotstuff::ProposalKey;
using hotstuff::ReplicaHypothesisMass;
using hotstuff::ReplicaID;
using hotstuff::ResponseAttemptIdentity;
using hotstuff::uint256_t;

uint256_t digest(const std::string &label)
{
    return DataStream(label).get_hash();
}

DiagnosticObservation observation(
    ReplicaID reporter,
    ReplicaID target,
    DiagnosticOutcome outcome,
    const std::string &attempt_label)
{
    return DiagnosticObservation{
        ResponseAttemptIdentity{
            reporter,
            target,
            ProposalKey{
                ConfigurationId{7, 3, digest("diagnostic-epoch")},
                digest(attempt_label)},
            ExpectedMessageType::direct_vote},
        outcome};
}

const DiagnosisSnapshot &require_snapshot(
    const hotstuff::DiagnosisBuildResult &result)
{
    REQUIRE(result.status == DiagnosisBuildStatus::built);
    REQUIRE(result.snapshot.has_value());
    return *result.snapshot;
}

const ReplicaHypothesisMass &mass(
    const DiagnosisSnapshot &snapshot,
    ReplicaID replica)
{
    const auto found = std::find_if(
        snapshot.mode_mass.begin(),
        snapshot.mode_mass.end(),
        [replica](const ReplicaHypothesisMass &entry) {
            return entry.replica_id == replica;
        });
    REQUIRE(found != snapshot.mode_mass.end());
    return *found;
}

bool contains(
    const std::vector<ReplicaID> &replicas,
    ReplicaID replica)
{
    return std::binary_search(
        replicas.begin(), replicas.end(), replica);
}

std::uint64_t choose(std::size_t n, std::size_t k)
{
    if (k > n)
        return 0;
    k = std::min(k, n - k);
    std::uint64_t result = 1;
    for (std::size_t index = 1; index <= k; ++index)
        result = result * (n - k + index) / index;
    return result;
}

std::uint64_t expected_hypotheses(
    std::size_t n,
    std::uint32_t diagnostic_bound)
{
    std::uint64_t result = 0;
    for (std::uint32_t faults = 0;
         faults <= diagnostic_bound;
         ++faults)
    {
        result += choose(n, faults) * (std::uint64_t{1} << faults);
    }
    return result;
}

} // namespace

TEST_CASE(
    "one negative observation preserves liar and omitter explanations",
    "[diagnosis][f09a][ambiguity]")
{
    const auto result = hotstuff::build_diagnosis_snapshot(
        {0, 1},
        1,
        {observation(
            0,
            1,
            DiagnosticOutcome::timeout,
            "single-negative")});
    const auto &snapshot = require_snapshot(result);

    REQUIRE(snapshot.initial_hypothesis_count == 5);
    REQUIRE(snapshot.compatible_hypotheses.size() == 2);
    CHECK(snapshot.model_consistent());
    CHECK(snapshot.compatible_hypotheses[0] ==
          CompatibleHypothesis{{0}, {}});
    CHECK(snapshot.compatible_hypotheses[1] ==
          CompatibleHypothesis{{}, {1}});

    const auto &reporter = mass(snapshot, 0);
    CHECK(reporter.false_reporter_count == 1);
    CHECK(reporter.persistent_omitter_count == 0);
    CHECK(reporter.compatible_count == 2);
    CHECK(reporter.classification ==
          DiagnosticClassification::unresolved);

    const auto &target = mass(snapshot, 1);
    CHECK(target.false_reporter_count == 0);
    CHECK(target.persistent_omitter_count == 1);
    CHECK(target.compatible_count == 2);
    CHECK(target.classification ==
          DiagnosticClassification::unresolved);
}

TEST_CASE(
    "an independent positive observation identifies the false reporter",
    "[diagnosis][f09a][corroboration]")
{
    const auto result = hotstuff::build_diagnosis_snapshot(
        {0, 1, 2},
        1,
        {
            observation(
                0,
                1,
                DiagnosticOutcome::timeout,
                "false-report"),
            observation(
                2,
                1,
                DiagnosticOutcome::on_time_valid,
                "corroborating-positive"),
        });
    const auto &snapshot = require_snapshot(result);

    REQUIRE(snapshot.compatible_hypotheses.size() == 1);
    CHECK(snapshot.compatible_hypotheses.front() ==
          CompatibleHypothesis{{0}, {}});
    CHECK(mass(snapshot, 0).classification ==
          DiagnosticClassification::false_reporter);
    CHECK(mass(snapshot, 1).classification ==
          DiagnosticClassification::correct);
    CHECK(mass(snapshot, 2).classification ==
          DiagnosticClassification::correct);
}

TEST_CASE(
    "distinct reporter majority identifies persistent omission",
    "[diagnosis][f09a][majority]")
{
    const auto result = hotstuff::build_diagnosis_snapshot(
        {0, 1, 2, 3},
        1,
        {
            observation(
                0,
                3,
                DiagnosticOutcome::timeout,
                "omission-0"),
            observation(
                1,
                3,
                DiagnosticOutcome::timeout,
                "omission-1"),
            observation(
                2,
                3,
                DiagnosticOutcome::timeout,
                "omission-2"),
        });
    const auto &snapshot = require_snapshot(result);

    CHECK(mass(snapshot, 3).classification ==
          DiagnosticClassification::persistent_omitter);
    CHECK(mass(snapshot, 3).persistent_omitter_count ==
          snapshot.compatible_hypotheses.size());
}

TEST_CASE(
    "five reporters distinguish two liars from a responsive target",
    "[diagnosis][f09a][majority][td2]")
{
    std::vector<DiagnosticObservation> history;
    for (ReplicaID reporter = 0; reporter < 5; ++reporter)
    {
        history.push_back(observation(
            reporter,
            5,
            reporter < 2
                ? DiagnosticOutcome::timeout
                : DiagnosticOutcome::on_time_valid,
            "td2-" + std::to_string(reporter)));
    }
    const auto result = hotstuff::build_diagnosis_snapshot(
        {0, 1, 2, 3, 4, 5}, 2, std::move(history));
    const auto &snapshot = require_snapshot(result);

    CHECK(mass(snapshot, 0).classification ==
          DiagnosticClassification::false_reporter);
    CHECK(mass(snapshot, 1).classification ==
          DiagnosticClassification::false_reporter);
    CHECK(mass(snapshot, 5).classification ==
          DiagnosticClassification::correct);
}

TEST_CASE(
    "production-size hypothesis space is exact and capacity bounded",
    "[diagnosis][f09a][bounds][n31]")
{
    std::vector<ReplicaID> membership;
    for (ReplicaID replica = 0; replica < 31; ++replica)
        membership.push_back(replica);

    const auto built = hotstuff::build_diagnosis_snapshot(
        membership, 3, {});
    const auto &snapshot = require_snapshot(built);
    CHECK(snapshot.initial_hypothesis_count == 37'883);
    CHECK(snapshot.compatible_hypotheses.size() == 37'883);

    DiagnosisLimits too_small;
    too_small.maximum_hypotheses = 37'882;
    const auto rejected = hotstuff::build_diagnosis_snapshot(
        membership, 3, {}, too_small);
    CHECK(
        rejected.status ==
        DiagnosisBuildStatus::hypothesis_capacity_exceeded);
    CHECK_FALSE(rejected.snapshot.has_value());
}

TEST_CASE(
    "membership and history permutations produce one canonical snapshot",
    "[diagnosis][f09a][determinism]")
{
    const auto negative = observation(
        0,
        1,
        DiagnosticOutcome::timeout,
        "deterministic-negative");
    const auto positive = observation(
        2,
        1,
        DiagnosticOutcome::on_time_valid,
        "deterministic-positive");

    const auto first = hotstuff::build_diagnosis_snapshot(
        {0, 1, 2, 3}, 2, {negative, positive, positive});
    const auto second = hotstuff::build_diagnosis_snapshot(
        {3, 1, 0, 2}, 2, {positive, negative});
    const auto &left = require_snapshot(first);
    const auto &right = require_snapshot(second);

    CHECK(left == right);
    CHECK(left.canonical_history.size() == 2);
}

TEST_CASE(
    "invalid or conflicting observations fail without a partial snapshot",
    "[diagnosis][f09a][validation]")
{
    const auto nonmember = hotstuff::build_diagnosis_snapshot(
        {0, 1},
        1,
        {observation(
            0,
            2,
            DiagnosticOutcome::on_time_valid,
            "nonmember")});
    CHECK(nonmember.status ==
          DiagnosisBuildStatus::invalid_observation);
    CHECK_FALSE(nonmember.snapshot.has_value());

    const auto self = hotstuff::build_diagnosis_snapshot(
        {0, 1},
        1,
        {observation(
            0,
            0,
            DiagnosticOutcome::on_time_valid,
            "self")});
    CHECK(self.status ==
          DiagnosisBuildStatus::invalid_observation);
    CHECK_FALSE(self.snapshot.has_value());

    const auto exact = observation(
        0,
        1,
        DiagnosticOutcome::on_time_valid,
        "conflict");
    auto conflict = exact;
    conflict.outcome = DiagnosticOutcome::timeout;
    const auto conflicting = hotstuff::build_diagnosis_snapshot(
        {0, 1}, 1, {exact, conflict});
    CHECK(
        conflicting.status ==
        DiagnosisBuildStatus::conflicting_observation);
    CHECK_FALSE(conflicting.snapshot.has_value());
}

TEST_CASE(
    "empty hypothesis set is an explicit model inconsistency",
    "[diagnosis][f09a][model-boundary]")
{
    const auto result = hotstuff::build_diagnosis_snapshot(
        {0, 1},
        0,
        {observation(
            0,
            1,
            DiagnosticOutcome::timeout,
            "impossible-with-zero-faults")});
    const auto &snapshot = require_snapshot(result);

    CHECK_FALSE(snapshot.model_consistent());
    CHECK(snapshot.compatible_hypotheses.empty());
    CHECK(mass(snapshot, 0).classification ==
          DiagnosticClassification::model_inconsistent);
    CHECK(mass(snapshot, 1).classification ==
          DiagnosticClassification::model_inconsistent);
}

TEST_CASE(
    "small bounds match the independent mathematical predicate",
    "[diagnosis][f09a][exhaustive]")
{
    for (std::size_t member_count = 2;
         member_count <= 7;
         ++member_count)
    {
        std::vector<ReplicaID> membership;
        for (ReplicaID replica = 0; replica < member_count; ++replica)
            membership.push_back(replica);

        for (std::uint32_t diagnostic_bound = 0;
             diagnostic_bound <=
                 std::min<std::uint32_t>(2, member_count);
             ++diagnostic_bound)
        {
            const auto empty = hotstuff::build_diagnosis_snapshot(
                membership, diagnostic_bound, {});
            const auto &initial = require_snapshot(empty);
            REQUIRE(
                initial.compatible_hypotheses.size() ==
                expected_hypotheses(
                    member_count, diagnostic_bound));

            for (const auto &hypothesis :
                 initial.compatible_hypotheses)
            {
                CHECK(std::is_sorted(
                    hypothesis.false_reporters.begin(),
                    hypothesis.false_reporters.end()));
                CHECK(std::is_sorted(
                    hypothesis.persistent_omitters.begin(),
                    hypothesis.persistent_omitters.end()));
                CHECK(
                    hypothesis.false_reporters.size() +
                        hypothesis.persistent_omitters.size() <=
                    diagnostic_bound);
                for (const auto replica :
                     hypothesis.false_reporters)
                {
                    CHECK_FALSE(contains(
                        hypothesis.persistent_omitters,
                        replica));
                }
            }

            for (ReplicaID reporter = 0;
                 reporter < member_count;
                 ++reporter)
            {
                for (ReplicaID target = 0;
                     target < member_count;
                     ++target)
                {
                    if (reporter == target)
                        continue;
                    for (const auto outcome : {
                             DiagnosticOutcome::on_time_valid,
                             DiagnosticOutcome::timeout})
                    {
                        const auto one =
                            hotstuff::build_diagnosis_snapshot(
                                membership,
                                diagnostic_bound,
                                {observation(
                                    reporter,
                                    target,
                                    outcome,
                                    "predicate-" +
                                        std::to_string(reporter) +
                                        "-" +
                                        std::to_string(target) +
                                        "-" +
                                        std::to_string(
                                            static_cast<int>(
                                                outcome)))});
                        const auto &actual =
                            require_snapshot(one);
                        std::vector<CompatibleHypothesis> expected;
                        for (const auto &hypothesis :
                             initial.compatible_hypotheses)
                        {
                            const bool reporter_lies = contains(
                                hypothesis.false_reporters,
                                reporter);
                            const bool target_omits = contains(
                                hypothesis.persistent_omitters,
                                target);
                            const bool missing =
                                outcome ==
                                DiagnosticOutcome::timeout;
                            if (reporter_lies ||
                                missing == target_omits)
                            {
                                expected.push_back(hypothesis);
                            }
                        }
                        CHECK(
                            actual.compatible_hypotheses ==
                            expected);
                    }
                }
            }
        }
    }
}
