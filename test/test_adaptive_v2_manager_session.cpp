#include <algorithm>
#include <array>
#include <cstddef>
#include <cstdint>
#include <limits>
#include <set>
#include <string>
#include <utility>
#include <vector>

#include "catch.hpp"
#include "hotstuff/epoch_activation.h"

#if __has_include("hotstuff/adaptive_v2_manager_session.h")
#include "hotstuff/adaptive_v2_manager_session.h"
#define KAURI_HAS_ADAPTIVE_V2_MANAGER_SESSION 1
#else
#define KAURI_HAS_ADAPTIVE_V2_MANAGER_SESSION 0
#endif

#if !KAURI_HAS_ADAPTIVE_V2_MANAGER_SESSION

TEST_CASE(
    "M12-R01 exposes one reusable recurring adaptive-v2 manager session",
    "[adaptive-v2][manager-session][recurring][n7][contract]")
{
    FAIL(
        "M12-R01 RED: hotstuff/adaptive_v2_manager_session.h is absent; "
        "the one-shot manager cannot drive E0 -> E1 -> E2 -> E3");
}

#else

namespace
{

using hotstuff::AdaptiveV2EpochActivatedObservation;
using hotstuff::AdaptiveV2EpochChangeIdentity;
using hotstuff::AdaptiveV2ManagerControllerStatus;
using hotstuff::AdaptiveV2ManagerConvergenceDisposition;
using hotstuff::AdaptiveV2ManagerIngressStatus;
using hotstuff::AdaptiveV2ManagerSession;
using hotstuff::AdaptiveV2ManagerSessionConfig;
using hotstuff::AdaptiveV2ReadinessNotice;
using hotstuff::AdaptiveV2TransitionPolicy;
using hotstuff::AuthenticatedReporter;
using hotstuff::BaselineRoot;
using hotstuff::ConfigurationId;
using hotstuff::DataStream;
using hotstuff::EpochChangeBundleLimits;
using hotstuff::EpochDefinitionInput;
using hotstuff::EpochTreeDefinition;
using hotstuff::EpochWireLimits;
using hotstuff::ExpectedMessageType;
using hotstuff::NormalProposalRuntimeInitialized;
using hotstuff::PrivKeySecp256k1;
using hotstuff::ProposalLifecycleFact;
using hotstuff::ProposalLifecycleNotice;
using hotstuff::ReplicaID;
using hotstuff::ResponseObservation;
using hotstuff::ResponseObservationBatch;
using hotstuff::ResponseOutcome;
using hotstuff::TreePlacementInput;
using hotstuff::TreePolicyKind;
using hotstuff::TreeShape;
using hotstuff::uint256_t;

constexpr std::uint32_t kIssuerId = 17;
constexpr std::uint64_t kInitialGeneration = 3;
constexpr std::uint64_t kSnapshotSeed = 0xA2F7;
const std::vector<ReplicaID> kMembers{0, 1, 2, 3, 4, 5, 6};
const std::vector<ReplicaID> kSurvivors{2, 3, 4, 5, 6};

uint256_t digest(const std::string &label)
{
    return DataStream(label).get_hash();
}

EpochDefinitionInput epoch_zero()
{
    std::vector<EpochTreeDefinition> trees;
    for (std::uint32_t root = 0; root < kMembers.size(); ++root)
    {
        std::vector<ReplicaID> breadth_first;
        for (std::size_t offset = 0; offset < kMembers.size(); ++offset)
        {
            breadth_first.push_back(static_cast<ReplicaID>(
                (root + offset) % kMembers.size()));
        }
        trees.push_back({root, 2, 2, std::move(breadth_first), {}});
    }
    return hotstuff::adaptive_v2_epoch_zero_input(
        kMembers, std::move(trees));
}

PrivKeySecp256k1 private_key()
{
    PrivKeySecp256k1 key;
    key.from_hex(
        "4aede145d13021fb43c938bced67511a7740c05786d3e0b94ffbdaa7f15afc57");
    return key;
}

EpochChangeBundleLimits bundle_limits()
{
    return {
        64 * 1024,
        4096,
        EpochWireLimits{32 * 1024, 8, 8, 128, 2}};
}

AdaptiveV2ManagerSessionConfig session_config(
    std::uint64_t initial_generation = kInitialGeneration)
{
    AdaptiveV2ManagerSessionConfig config;
    config.active_tree_id = 0;
    config.activation_generation = initial_generation;
    config.ingress_limits.maximum_members = 7;
    config.ingress_limits.readiness_wire.maximum_payload_bytes = 256;
    config.ingress_limits.lifecycle_wire.maximum_payload_bytes = 512;
    config.ingress_limits.evidence_wire = {4096, 8, 7};
    config.ingress_limits.proposal_index = {512, 32};
    config.ingress_limits.evidence_store = {1024, 256};
    config.ingress_limits.lifecycle = {
        128, 64 * 1024, 7, 1024, 512, 7, 16};
    config.ingress_limits.lifecycle_accounting = {
        128, 64 * 1024, 1024};
    config.ingress_limits.maximum_pending_lifecycle_facts_per_source =
        64;

    config.controller.selection.required_nonresponsive = 2;
    config.controller.selection.minimum_score_drop = 6;
    config.controller.selection.minimum_timeouts_per_reporter = 2;
    config.controller.selection.maximum_post_baseline_timeout_attempts =
        128;
    config.controller.selection.responsiveness_policy.policy_version =
        "adaptive-v2-session-responsiveness-v1";
    config.controller.selection.responsiveness_policy.attempt_window = 32;
    config.controller.selection.responsiveness_policy.minimum_attempts = 2;
    config.controller.selection.responsiveness_policy
        .minimum_response_rate_ppm = 750'000;
    config.controller.selection.responsiveness_policy
        .maximum_timeout_rate_ppm = 250'000;
    config.controller.selection.responsiveness_policy
        .trailing_timeout_streak = 2;
    config.controller.selection.responsiveness_policy
        .latency_percentile_basis_points = 5'000;
    config.controller.selection.snapshot_seed = kSnapshotSeed;
    config.controller.placement = TreePlacementInput{
        kMembers,
        TreeShape{2, 2, 5},
        kSnapshotSeed,
        "adaptive-v2-recurring-v1"};
    config.controller.activation_delay_blocks = 5;
    config.controller.issuer_id = kIssuerId;
    config.controller.issuer_private_key = private_key();
    config.controller.bundle_limits = bundle_limits();

    config.retry_interval_ticks = 2;
    config.maximum_attempts_per_recipient = 3;
    config.convergence_window_ticks = 100;
    return config;
}

AdaptiveV2TransitionPolicy containment_policy()
{
    AdaptiveV2TransitionPolicy policy;
    policy.intent = TreePolicyKind::fault_containment;
    policy.containment_baseline_roots = {
        BaselineRoot{0, 2},
        BaselineRoot{1, 3},
        BaselineRoot{2, 4},
        BaselineRoot{3, 5},
        BaselineRoot{4, 6}};
    return policy;
}

AdaptiveV2TransitionPolicy optimization_policy()
{
    AdaptiveV2TransitionPolicy policy;
    policy.intent = TreePolicyKind::performance_optimization;
    return policy;
}

std::size_t first_leaf_index(
    std::size_t member_count,
    std::uint32_t fanout)
{
    REQUIRE(fanout != 0);
    return member_count == 1
               ? 0
               : ((member_count - 2) / fanout) + 1;
}

struct Edge
{
    std::uint32_t tree_id{0};
    ReplicaID reporter{0};
};

Edge leaf_edge(
    const AdaptiveV2ManagerSession &session,
    ReplicaID target,
    std::size_t occurrence)
{
    std::size_t found = 0;
    std::set<ReplicaID> reporters;
    for (const auto &tree : session.ingress().current_epoch().trees())
    {
        const auto position = std::find(
            tree.members_breadth_first.begin(),
            tree.members_breadth_first.end(),
            target);
        REQUIRE(position != tree.members_breadth_first.end());
        const auto index = static_cast<std::size_t>(std::distance(
            tree.members_breadth_first.begin(), position));
        if (index < first_leaf_index(
                        tree.members_breadth_first.size(), tree.fanout))
        {
            continue;
        }
        const auto parent = (index - 1) / tree.fanout;
        const auto reporter = tree.members_breadth_first[parent];
        if (!reporters.insert(reporter).second ||
            found++ != occurrence)
        {
            continue;
        }
        return {tree.tree_id, reporter};
    }
    FAIL("target has too few leaf placements for guarded evidence");
    return {};
}

AdaptiveV2EpochChangeIdentity identity_for(
    const hotstuff::AdaptiveV2EpochChangeBundle &bundle,
    std::uint64_t command_height,
    const std::string &label)
{
    const auto &payload = bundle.command().payload;
    return {
        payload.successor_epoch_number - 1,
        payload.predecessor_epoch_digest,
        payload.successor_epoch_number,
        payload.successor_epoch_digest,
        hotstuff::epoch_change_payload_digest(payload),
        command_height,
        digest(label),
        payload.activation_delay_blocks,
        command_height + payload.activation_delay_blocks};
}

struct Fixture
{
    AdaptiveV2ManagerSession session{
        kMembers, epoch_zero(), session_config()};
    std::array<std::uint64_t, 7> readiness_sequences{};
    std::array<std::uint64_t, 7> lifecycle_sequences{};
    std::array<std::uint64_t, 7> evidence_sequences{};
    std::uint64_t proposal_counter{0};

    void ready_all()
    {
        for (const auto source : kMembers)
        {
            const AdaptiveV2ReadinessNotice notice{
                hotstuff::kAdaptiveV2ReadinessNoticeSchemaVersionV1,
                source,
                ++readiness_sequences[source],
                session.ingress().current_configuration(),
                session.ingress().activation_generation(),
                static_cast<std::uint64_t>(100 + source)};
            CHECK(session.ingress()
                      .ingest_readiness(
                          AuthenticatedReporter{source},
                          hotstuff::encode_adaptive_v2_readiness_notice(
                              notice,
                              session_config().ingress_limits
                                  .readiness_wire))
                      .status ==
                  AdaptiveV2ManagerIngressStatus::processed);
        }
        REQUIRE(session.ingress().all_members_ready());
    }

    ResponseObservation make_observation(
        ReplicaID target,
        std::size_t reporter_occurrence,
        ResponseOutcome outcome,
        const std::string &label)
    {
        const auto edge = leaf_edge(
            session, target, reporter_occurrence);
        ResponseObservation value;
        value.reporter_id = edge.reporter;
        value.observed_replica_id = target;
        value.configuration = ConfigurationId{
            session.ingress().current_epoch().epoch_number(),
            edge.tree_id,
            session.ingress().current_epoch().epoch_digest()};
        value.block_hash = digest(
            label + "-" + std::to_string(++proposal_counter));
        value.expected_message_type = ExpectedMessageType::direct_vote;
        value.outcome = outcome;
        value.response_duration_us =
            outcome == ResponseOutcome::timeout ? 0 : 20 + target;
        value.deadline_duration_us = 100;
        value.reporter_sequence =
            ++evidence_sequences[value.reporter_id];
        value.reporter_monotonic_ns =
            value.reporter_sequence * 1'000;
        if (outcome != ResponseOutcome::timeout)
            value.signer_set = {target};
        value.observation_id =
            hotstuff::compute_response_observation_id(
                value.attempt_identity());
        return value;
    }

    void record(ResponseObservation observation)
    {
        for (ReplicaID source = 0; source < 3; ++source)
        {
            const ProposalLifecycleNotice notice{
                hotstuff::kProposalLifecycleNoticeSchemaVersion,
                source,
                ++lifecycle_sequences[source],
                ProposalLifecycleFact{NormalProposalRuntimeInitialized{
                    observation.proposal_key()}}};
            const auto result = session.ingress().ingest_lifecycle(
                AuthenticatedReporter{source},
                hotstuff::encode_proposal_lifecycle_notice(
                    notice,
                    session_config().ingress_limits.lifecycle_wire));
            CHECK(result.status ==
                  (source < 2
                       ? AdaptiveV2ManagerIngressStatus::
                             awaiting_corroboration
                       : AdaptiveV2ManagerIngressStatus::processed));
        }
        const auto result = session.ingress().ingest_evidence(
            AuthenticatedReporter{observation.reporter_id},
            hotstuff::encode_evidence_batch(
                ResponseObservationBatch{
                    hotstuff::kEvidenceBatchSchemaVersion,
                    {std::move(observation)}},
                session_config().ingress_limits.evidence_wire));
        REQUIRE(result.status ==
                AdaptiveV2ManagerIngressStatus::processed);
        REQUIRE(result.accepted_observations == 1);
    }

    void responsive_baseline()
    {
        for (const auto target : kMembers)
        {
            for (std::size_t attempt = 0; attempt < 2; ++attempt)
            {
                record(make_observation(
                    target,
                    attempt,
                    ResponseOutcome::on_time,
                    "baseline"));
            }
        }
    }

    void persistent_timeouts()
    {
        for (const auto target : {ReplicaID{0}, ReplicaID{1}})
        {
            for (std::size_t reporter = 0; reporter < 3; ++reporter)
            {
                for (std::size_t attempt = 0; attempt < 2; ++attempt)
                {
                    record(make_observation(
                        target,
                        reporter,
                        ResponseOutcome::timeout,
                        "timeout"));
                }
            }
        }
    }

    AdaptiveV2EpochChangeIdentity complete_cycle(
        const AdaptiveV2TransitionPolicy &policy,
        std::uint64_t command_height)
    {
        REQUIRE(session.begin_cycle(policy));
        CHECK(session.evaluate() ==
              AdaptiveV2ManagerControllerStatus::awaiting_readiness);
        ready_all();
        responsive_baseline();
        REQUIRE(session.evaluate() ==
                AdaptiveV2ManagerControllerStatus::baseline_frozen);
        persistent_timeouts();
        REQUIRE(session.evaluate() ==
                AdaptiveV2ManagerControllerStatus::successor_ready);
        REQUIRE(session.successor_bundle() != nullptr);
        const auto identity = identity_for(
            *session.successor_bundle(),
            command_height,
            "command-" + std::to_string(command_height));
        REQUIRE(session.start_convergence(command_height));
        for (const auto source : kSurvivors)
        {
            const AdaptiveV2EpochActivatedObservation activated{
                hotstuff::kAdaptiveV2ConvergenceObservationSchemaVersionV1,
                source,
                identity,
                identity.successor_epoch_number,
                identity.successor_epoch_digest};
            CHECK(session.observe_activation(source, activated) ==
                  AdaptiveV2ManagerConvergenceDisposition::accepted);
        }
        REQUIRE(session.consume_ready_and_rotate());
        return identity;
    }
};

std::vector<ReplicaID> roots(
    const hotstuff::EpochDefinition &epoch)
{
    std::vector<ReplicaID> result;
    for (const auto &tree : epoch.trees())
    {
        REQUIRE_FALSE(tree.members_breadth_first.empty());
        result.push_back(tree.members_breadth_first.front());
    }
    return result;
}

void check_fixed_authority(const AdaptiveV2ManagerSession &session)
{
    CHECK(session.ingress().membership() == kMembers);
    CHECK(session.ingress().quorum_metadata().replica_count == 7);
    CHECK(session.ingress().quorum_metadata().fault_threshold == 2);
    CHECK(session.ingress().quorum_metadata().quorum == 5);
    CHECK(session.ingress().current_epoch().membership_digest() ==
          hotstuff::canonical_membership_digest(kMembers));
}

} // namespace

TEST_CASE(
    "one session drives containment then two explicit optimization cycles",
    "[adaptive-v2][manager-session][recurring][n7][integration]")
{
    Fixture fixture;
    check_fixed_authority(fixture.session);
    const auto membership_digest =
        fixture.session.ingress().current_epoch().membership_digest();

    const std::array<AdaptiveV2TransitionPolicy, 3> policies{{
        containment_policy(),
        optimization_policy(),
        optimization_policy()}};
    const std::array<TreePolicyKind, 3> expected_intents{{
        TreePolicyKind::fault_containment,
        TreePolicyKind::performance_optimization,
        TreePolicyKind::performance_optimization}};

    for (std::uint32_t cycle = 0; cycle < policies.size(); ++cycle)
    {
        INFO("recurring cycle " << cycle);
        const auto predecessor_number =
            fixture.session.ingress().current_epoch().epoch_number();
        const auto predecessor_digest =
            fixture.session.ingress().current_epoch().epoch_digest();
        const auto identity = fixture.complete_cycle(
            policies[cycle], 100 + cycle * 10);

        CHECK(identity.predecessor_epoch_number == predecessor_number);
        CHECK(identity.predecessor_epoch_digest == predecessor_digest);
        CHECK(identity.successor_epoch_number == predecessor_number + 1);
        CHECK(fixture.session.ingress().current_epoch().epoch_number() ==
              cycle + 1);
        CHECK(fixture.session.ingress().current_epoch().epoch_digest() ==
              identity.successor_epoch_digest);
        CHECK(fixture.session.ingress().current_epoch()
                  .previous_epoch_digest() == predecessor_digest);
        CHECK(fixture.session.ingress().current_epoch()
                  .membership_digest() == membership_digest);
        CHECK(fixture.session.ingress().ledger().high_watermark() == 0);
        CHECK(fixture.session.ingress().ledger().accepted().empty());
        CHECK_FALSE(fixture.session.ingress().all_members_ready());
        check_fixed_authority(fixture.session);

        REQUIRE(fixture.session.terminal_records().size() == cycle + 1);
        const auto &record = fixture.session.terminal_records().back();
        CHECK(record.cycle_ordinal == cycle);
        CHECK(record.policy_intent == expected_intents[cycle]);
        CHECK(record.predecessor_epoch_number == predecessor_number);
        CHECK(record.predecessor_epoch_digest == predecessor_digest);
        CHECK(record.successor_epoch_number == cycle + 1);
        CHECK(record.successor_epoch_digest ==
              identity.successor_epoch_digest);
        CHECK(record.command_payload_digest ==
              identity.command_payload_digest);
        CHECK(record.winning_activation == identity);

        const auto record_count =
            fixture.session.terminal_records().size();
        CHECK_FALSE(fixture.session.consume_ready_and_rotate());
        CHECK(fixture.session.terminal_records().size() == record_count);
        const AdaptiveV2EpochActivatedObservation duplicate{
            hotstuff::kAdaptiveV2ConvergenceObservationSchemaVersionV1,
            2,
            identity,
            identity.successor_epoch_number,
            identity.successor_epoch_digest};
        CHECK(fixture.session.observe_activation(2, duplicate) ==
              AdaptiveV2ManagerConvergenceDisposition::terminal);
        CHECK(fixture.session.terminal_records().size() == record_count);
    }

    CHECK(fixture.session.ingress().current_epoch().epoch_number() == 3);
    CHECK(roots(fixture.session.ingress().current_epoch()) ==
          std::vector<ReplicaID>{2, 3, 4, 5, 6});
    for (const auto &tree :
         fixture.session.ingress().current_epoch().trees())
    {
        CHECK(tree.wait_exempt_leaves ==
              std::vector<ReplicaID>{0, 1});
    }
}

TEST_CASE(
    "counter exhaustion creates no command record or partial epoch window",
    "[adaptive-v2][manager-session][overflow][fail-closed][n7]")
{
    AdaptiveV2ManagerSession session{
        kMembers,
        epoch_zero(),
        session_config()};
    const auto epoch_digest = session.ingress().current_epoch().epoch_digest();
    const auto generation = session.ingress().activation_generation();

    // Sessions bootstrap only from trusted E0 and reach later epochs through
    // exact rotations. Numeric exhaustion therefore remains at the public
    // checked factory/activation seam rather than a history-free max-epoch
    // session fixture.
    CHECK_FALSE(hotstuff::checked_successor_epoch(
        std::numeric_limits<std::uint32_t>::max()));
    CHECK_FALSE(hotstuff::checked_activation_generation(
        std::numeric_limits<std::uint32_t>::max(),
        std::numeric_limits<std::uint64_t>::max()));
    CHECK(session.successor_bundle() == nullptr);
    CHECK(session.terminal_records().empty());
    CHECK(session.ingress().current_epoch().epoch_number() == 0);
    CHECK(session.ingress().current_epoch().epoch_digest() == epoch_digest);
    CHECK(session.ingress().activation_generation() == generation);
    CHECK(session.ingress().ledger().accepted().empty());
}

#endif
