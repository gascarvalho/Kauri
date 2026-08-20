#include "catch.hpp"

#include <algorithm>
#include <memory>
#include <string>
#include <utility>
#include <vector>

#include "hotstuff/adaptive_v3_activation_readiness.h"
#include "hotstuff/adaptive_v3_manager_session.h"
#include "support/adaptive_v3_manager_session_fixture.h"

namespace {

using namespace hotstuff;
using namespace kauri::test_support::cert13;

std::uint32_t survivor_reported_tree(const AdaptiveV3ManagerSession &session,
                                     ReplicaID replica,
                                     const std::vector<ReplicaID> &survivors)
{
    for (const auto &tree : session.ingress().current_epoch().trees()) {
        const auto found = std::find(tree.members_breadth_first.begin(),
            tree.members_breadth_first.end(), replica);
        if (found == tree.members_breadth_first.end()) continue;
        const auto position = static_cast<std::size_t>(std::distance(
            tree.members_breadth_first.begin(), found));
        if (position == 0) continue;
        const auto reporter = tree.members_breadth_first[(position - 1U) / tree.fanout];
        if (std::find(survivors.begin(), survivors.end(), reporter) != survivors.end())
            return tree.tree_id;
    }
    FAIL("replica has no tree with a surviving exact-edge reporter");
    return 0;
}

} // namespace

TEST_CASE("CERT13 manager session drives two certified N7 transitions without preseeded identities",
          "[cert13][adaptive-v3][manager-session][n7][q5][r5][integration]")
{
    const std::vector<ReplicaID> survivors{2, 3, 4, 5, 6};
    Fixture fixture(7, survivors);
    fixture.select_containment({0, 1});
    complete_readiness(fixture, survivors, 10, 1000, "e1");
    record_common_commit(fixture, survivors, 16, "n7-e1-common-commit");
    REQUIRE(fixture.session.status() == AdaptiveV3ManagerSessionStatus::residency);
    REQUIRE(fixture.session.terminal_records().size() == 1);
    // This direct session fixture deliberately uses its documented millisecond
    // defaults.  The live facade contract separately supplies raw ns; this
    // pins the half-open boundary so it cannot open one fixture tick early.
    REQUIRE_FALSE(fixture.session.e2_eligible(65014));
    fixture.session.advance(65015);
    REQUIRE(fixture.session.status() == AdaptiveV3ManagerSessionStatus::residency);
    REQUIRE(fixture.session.e2_eligible(65015));

    // The second certified cycle must be possible in the same manager process.
    AdaptiveV2TransitionPolicy optimization;
    optimization.intent = TreePolicyKind::performance_optimization;
    const auto e2 = fixture.session.begin_e2_at(65015, optimization);
    REQUIRE(e2.has_value());
    CHECK(e2->cycle_ordinal == 1);
    CHECK(e2->final_ack_tick == 15);
    CHECK(e2->common_commit_tick == 16);
    CHECK(e2->earliest_e2_tick == 65015);
    CHECK(e2->actual_e2_begin_tick == 65015);
    CHECK(e2->common_commit_sources == survivors);
    CHECK(e2->reserve_ticks == fixture.config.e2_reserve_ticks);
    // E1's two failed replicas do not rejoin manager readiness. The surviving
    // cohort supplies fresh, source-authenticated E1 baseline observations.
    fixture.evidence.ready(survivors);
    for (const auto survivor : survivors)
        for (std::size_t attempt = 0; attempt < 2; ++attempt)
            fixture.evidence.record(fixture.evidence.make_observation(
                survivor, 0, ResponseOutcome::on_time, "e1-baseline"));
    REQUIRE(fixture.session.evaluate() == AdaptiveV2ManagerControllerStatus::baseline_frozen);
    fixture.evidence.responsive_optimization_suffix();
    REQUIRE(fixture.session.evaluate() == AdaptiveV2ManagerControllerStatus::successor_ready);
    complete_readiness(fixture, survivors, 65020, 2000, "e2");
    REQUIRE(fixture.session.status() == AdaptiveV3ManagerSessionStatus::terminal);
    REQUIRE(fixture.session.terminal_records().size() == 2);
    const auto *terminal = fixture.session.terminal_audit();
    REQUIRE(terminal != nullptr);
    fixture.session.advance(65021);
    REQUIRE(fixture.session.terminal_audit() == terminal);
}

TEST_CASE("CERT13 session rejects a nonzero active tree at construction")
{
    Fixture fixture(7, {2, 3, 4, 5, 6});
    auto invalid = fixture.config;
    invalid.active_tree_id = 1;
    REQUIRE_THROWS_AS(AdaptiveV3ManagerSession(
        fixture.replicas, fixture.initial, std::move(invalid)), std::invalid_argument);
}

TEST_CASE("CERT13 one-cycle control terminates after its containment R ACKs",
          "[cert13][adaptive-v3][manager-session][control]")
{
    const std::vector<ReplicaID> survivors{2, 3, 4, 5, 6};
    Fixture fixture(7, survivors, 1,
                    BlsMembershipConstruction::deterministic_fixed_scalars);
    fixture.select_containment({0, 1});
    const auto artifacts = complete_readiness_capture(
        fixture, survivors, 10, 1000, "control-e1");
    const auto bundle = decode_adaptive_v3_epoch_change_bundle(
        artifacts.bundle_canonical_bytes, fixture.config.controller.bundle_limits);
    REQUIRE(bundle);
    const auto identity = decode_activation_ready_identity_v1(
        artifacts.identity_bytes, fixture.config.wire_limits);
    REQUIRE(identity);
    const auto certificate = decode_activation_readiness_certificate(
        artifacts.certificate_bytes, fixture.config.wire_limits);
    REQUIRE(certificate);
    REQUIRE(certificate.value->certificate_digest == artifacts.certificate_digest);
    REQUIRE(verify_activation_readiness_certificate(
        *certificate.value, *identity.value, identity.value->membership_digest,
        fixture.bls.public_keys));
    REQUIRE(artifacts.observations.size() == survivors.size());
    REQUIRE(artifacts.observation_payloads.size() == survivors.size());
    REQUIRE(artifacts.acknowledgement_payloads.size() == survivors.size());
    REQUIRE(artifacts.final_ack_tick == 15);
    REQUIRE(fixture.session.status() == AdaptiveV3ManagerSessionStatus::terminal);
    REQUIRE(fixture.session.terminal_records().size() == 1);
    REQUIRE(fixture.session.terminal_audit() != nullptr);
    CHECK(fixture.session.terminal_audit()->reason ==
          AdaptiveV3ManagerSessionTerminalReason::acknowledgements_complete);
    CHECK_FALSE(fixture.session.e2_eligibility_audit(65'015).has_value());
}

TEST_CASE("CERT13 first all-R timed common commit is immutable", "[cert13][adaptive-v3][manager-session]")
{
    const std::vector<ReplicaID> survivors{2, 3, 4, 5, 6};
    Fixture fixture(7, survivors);
    fixture.select_containment({0, 1});
    complete_readiness(fixture, survivors, 10, 1000, "e1");
    record_common_commit(fixture, survivors, 16, "first-common-commit");
    const auto frozen = fixture.session.e2_eligibility_audit(65015);
    REQUIRE(frozen.has_value());
    // A later all-R, distinct ProposalKey cannot replace the frozen first key
    // or postpone E2's exact final-ACK residence boundary.
    record_common_commit(fixture, survivors, 17, "second-common-commit");
    REQUIRE(fixture.session.e2_eligible(65015));
    const auto unchanged = fixture.session.e2_eligibility_audit(65015);
    REQUIRE(unchanged.has_value());
    CHECK(unchanged->common_commit == frozen->common_commit);
    CHECK(unchanged->common_commit_tick == frozen->common_commit_tick);
}

TEST_CASE("CERT13 residency terminal bounds are fail closed", "[.][intentional-red][cert13][adaptive-v3][manager-session]")
{
    const std::vector<ReplicaID> survivors{2, 3, 4, 5, 6};
    SECTION("E1 cannot publish without an armed hard deadline") {
        Fixture fixture(7, survivors);
        fixture.select_containment({0, 1}, false);
        REQUIRE_FALSE(fixture.session.begin_readiness(10));
    }
    SECTION("missing all-R commit terminates at final ACK plus five seconds") {
        Fixture fixture(7, survivors);
        fixture.select_containment({0, 1});
        complete_readiness(fixture, survivors, 10, 1000, "missing-commit");
        fixture.session.advance(5015);
        REQUIRE(fixture.session.status() == AdaptiveV3ManagerSessionStatus::terminal);
    }
    SECTION("90-second reserve equality terminates") {
        Fixture fixture(7, survivors);
        fixture.select_containment({0, 1}, true, 155015);
        complete_readiness(fixture, survivors, 10, 1000, "reserve-equality");
        record_common_commit(fixture, survivors, 16, "reserve-common");
        fixture.session.advance(65015);
        REQUIRE(fixture.session.status() == AdaptiveV3ManagerSessionStatus::terminal);
    }
    SECTION("hard deadline equality terminates") {
        Fixture fixture(7, survivors);
        fixture.select_containment({0, 1}, true, 65015);
        complete_readiness(fixture, survivors, 10, 1000, "deadline-equality");
        record_common_commit(fixture, survivors, 16, "deadline-common");
        fixture.session.advance(65015);
        REQUIRE(fixture.session.status() == AdaptiveV3ManagerSessionStatus::terminal);
    }
}

TEST_CASE("CERT13 E2 readiness rechecks the full hard-deadline reserve",
          "[cert13][adaptive-v3][manager-session][deadline]")
{
    const std::vector<ReplicaID> survivors{2, 3, 4, 5, 6};
    // E1's final ACK is tick 15.  At 65015 E2 can enter selection with one
    // tick of reserve remaining; at 65016 the strict 90s reserve is equal.
    Fixture fixture(7, survivors);
    fixture.select_containment({0, 1}, true, 155016);
    complete_readiness(fixture, survivors, 10, 1000, "reserve-anchor");
    record_common_commit(fixture, survivors, 16, "reserve-commit");
    fixture.session.advance(65015);
    REQUIRE(fixture.session.e2_eligible(65015));
    AdaptiveV2TransitionPolicy optimization;
    optimization.intent = TreePolicyKind::performance_optimization;
    REQUIRE(fixture.session.begin_cycle(optimization));
    fixture.evidence.ready(survivors);
    for (const auto survivor : survivors)
        for (std::size_t attempt = 0; attempt < 2; ++attempt)
            fixture.evidence.record(fixture.evidence.make_observation(
                survivor, 0, ResponseOutcome::on_time, "reserve-e2"));
    REQUIRE(fixture.session.evaluate() ==
            AdaptiveV2ManagerControllerStatus::baseline_frozen);
    fixture.evidence.responsive_optimization_suffix();
    REQUIRE(fixture.session.evaluate() ==
            AdaptiveV2ManagerControllerStatus::successor_ready);
    REQUIRE_FALSE(fixture.session.begin_readiness(65016));
    REQUIRE(fixture.session.status() == AdaptiveV3ManagerSessionStatus::terminal);
    REQUIRE(fixture.session.certificate() == nullptr);
    REQUIRE(fixture.session.terminal_records().size() == 2);
    REQUIRE(fixture.session.terminal_records().back().cycle_ordinal == 1);
    REQUIRE(fixture.session.terminal_audit()->reason ==
            AdaptiveV3ManagerSessionTerminalReason::hard_deadline_exhausted);
}

TEST_CASE("CERT13 N31 manager release remains R28 while certificate quorum is Q21",
          "[cert13][adaptive-v3][manager-session][n31][q21][r28][seam]"
          "[intentional-red]")
{
    std::vector<ReplicaID> survivors;
    for (ReplicaID replica = 3; replica < 31; ++replica) survivors.push_back(replica);
    Fixture fixture(31, survivors);
    fixture.select_containment({0, 1, 2});
    complete_readiness(fixture, survivors, 10, 1000, "n31-e1");
    REQUIRE(fixture.session.terminal_records().front().q_seed_sources.size() == 21);
    REQUIRE(fixture.session.terminal_records().front().r_audit_sources.size() == 28);
    record_common_commit(fixture, survivors, 39, "n31-e1-common-commit");
    fixture.session.advance(65038); // final R ACK at tick 38, plus exact 65 s.
    REQUIRE(fixture.session.status() == AdaptiveV3ManagerSessionStatus::residency);
    REQUIRE(fixture.session.e2_eligible(65038));
    AdaptiveV2TransitionPolicy optimization;
    optimization.intent = TreePolicyKind::performance_optimization;
    REQUIRE(fixture.session.begin_cycle(optimization));
    fixture.evidence.ready(survivors);
    std::uint64_t e2_baseline_attempt = 70'000;
    for (const auto survivor : survivors)
        for (std::uint32_t attempt = 0; attempt < 2; ++attempt)
            fixture.evidence.record_for_tree(survivor,
                survivor_reported_tree(fixture.session, survivor, survivors),
                ResponseOutcome::on_time, "n31-e2-baseline",
                e2_baseline_attempt++);
    REQUIRE(fixture.session.evaluate() ==
            AdaptiveV2ManagerControllerStatus::baseline_frozen);
    // A second v3 transition remains evidence-driven; no crashed reporter is
    // reintroduced merely to satisfy management readiness.
    std::uint64_t e2_suffix_attempt = 80'000;
    for (const auto survivor : survivors)
        for (std::uint32_t attempt = 0; attempt < 2; ++attempt)
            fixture.evidence.record_for_tree(survivor,
                survivor_reported_tree(fixture.session, survivor, survivors),
                ResponseOutcome::on_time, "n31-e2-suffix",
                e2_suffix_attempt++);
    REQUIRE(fixture.session.evaluate() ==
            AdaptiveV2ManagerControllerStatus::successor_ready);
    complete_readiness(fixture, survivors, 65050, 2000, "n31-e2");
    REQUIRE(fixture.session.status() == AdaptiveV3ManagerSessionStatus::terminal);
    REQUIRE(fixture.session.terminal_records().size() == 2);
}
