#include <algorithm>
#include <array>
#include <cstddef>
#include <cstdint>
#include <memory>
#include <set>
#include <string>
#include <type_traits>
#include <utility>
#include <vector>

#include "catch.hpp"
#include "hotstuff/adaptive_v2_manager_controller.h"

namespace
{

using hotstuff::AdaptiveV2ManagerController;
using hotstuff::AdaptiveV2ManagerControllerConfig;
using hotstuff::AdaptiveV2ManagerControllerStatus;
using hotstuff::AdaptiveV2ManagerIngress;
using hotstuff::AdaptiveV2ManagerIngressLimits;
using hotstuff::AdaptiveV2ManagerIngressStatus;
using hotstuff::AdaptiveV2ReadinessNotice;
using hotstuff::AdaptiveV2SelectionStatus;
using hotstuff::AuthenticatedReporter;
using hotstuff::ConfigurationId;
using hotstuff::DataStream;
using hotstuff::EpochChangeBundleLimits;
using hotstuff::EpochChangeIssuer;
using hotstuff::EpochDefinitionInput;
using hotstuff::EpochTreeDefinition;
using hotstuff::EpochWireLimits;
using hotstuff::ExpectedMessageType;
using hotstuff::NormalProposalRuntimeInitialized;
using hotstuff::PrivKeySecp256k1;
using hotstuff::ProposalLifecycleFact;
using hotstuff::ProposalLifecycleNotice;
using hotstuff::ReplicaAdaptationResult;
using hotstuff::ReplicaID;
using hotstuff::ResponseObservation;
using hotstuff::ResponseObservationBatch;
using hotstuff::ResponseOutcome;
using hotstuff::ResponsivenessClass;
using hotstuff::TreePlacementInput;
using hotstuff::TreeShape;
using hotstuff::uint256_t;

constexpr std::uint64_t kActivationGeneration = 3;
constexpr std::uint32_t kIssuerId = 17;
constexpr std::uint64_t kSnapshotSeed = 0xA2F7;
constexpr std::uint32_t kTimeoutsPerReporter = 2;
constexpr std::uint32_t kMinimumScoreDrop = 6;

static_assert(!std::is_copy_constructible<
              AdaptiveV2ManagerController>::value);
static_assert(!std::is_move_constructible<
              AdaptiveV2ManagerController>::value);

uint256_t digest(const std::string &label)
{
    return DataStream(label).get_hash();
}

std::vector<ReplicaID> membership()
{
    return {0, 1, 2, 3, 4, 5, 6};
}

EpochDefinitionInput epoch_zero()
{
    const auto members = membership();
    std::vector<EpochTreeDefinition> trees;
    trees.reserve(members.size());
    for (std::uint32_t root = 0; root < members.size(); ++root)
    {
        std::vector<ReplicaID> breadth_first;
        breadth_first.reserve(members.size());
        for (std::size_t offset = 0; offset < members.size(); ++offset)
        {
            breadth_first.push_back(static_cast<ReplicaID>(
                (root + offset) % members.size()));
        }
        trees.push_back(EpochTreeDefinition{
            root, 2, 2, std::move(breadth_first), {}});
    }
    return hotstuff::adaptive_v2_epoch_zero_input(
        members, std::move(trees));
}

AdaptiveV2ManagerIngressLimits ingress_limits()
{
    AdaptiveV2ManagerIngressLimits limits;
    limits.maximum_members = 7;
    limits.readiness_wire.maximum_payload_bytes = 256;
    limits.lifecycle_wire.maximum_payload_bytes = 512;
    limits.evidence_wire = {4096, 8, 7};
    limits.proposal_index = {256, 16};
    limits.evidence_store = {512, 128};
    limits.lifecycle = {64, 32 * 1024, 7, 512, 256, 7, 8};
    limits.lifecycle_accounting = {64, 32 * 1024, 512};
    limits.maximum_pending_lifecycle_facts_per_source = 64;
    return limits;
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

AdaptiveV2ManagerControllerConfig controller_config(
    const PrivKeySecp256k1 &key,
    std::uint64_t activation_delay_blocks = 5)
{
    AdaptiveV2ManagerControllerConfig config;
    config.selection.required_nonresponsive = 2;
    config.selection.minimum_score_drop = kMinimumScoreDrop;
    config.selection.minimum_timeouts_per_reporter =
        kTimeoutsPerReporter;
    config.selection.maximum_post_baseline_timeout_attempts = 128;
    config.selection.responsiveness_policy.policy_version =
        "adaptive-v2-controller-responsiveness-v1";
    config.selection.responsiveness_policy.attempt_window = 32;
    config.selection.responsiveness_policy.minimum_attempts = 2;
    config.selection.responsiveness_policy.minimum_response_rate_ppm =
        750'000;
    config.selection.responsiveness_policy.maximum_timeout_rate_ppm =
        250'000;
    config.selection.responsiveness_policy.trailing_timeout_streak = 2;
    config.selection.responsiveness_policy
        .latency_percentile_basis_points = 5'000;
    config.selection.snapshot_seed = kSnapshotSeed;
    config.placement = TreePlacementInput{
        membership(),
        TreeShape{2, 2, 5},
        kSnapshotSeed,
        "adaptive-v2-performance-optimization-v1"};
    config.activation_delay_blocks = activation_delay_blocks;
    config.issuer_id = kIssuerId;
    config.issuer_private_key = key;
    config.bundle_limits = bundle_limits();
    return config;
}

struct LeafEdge
{
    std::uint32_t tree_id{0};
    ReplicaID reporter{0};
};

LeafEdge leaf_edge(ReplicaID target, std::size_t reporter_index)
{
    static const std::array<std::uint32_t, 3> positions{{3, 4, 6}};
    REQUIRE(reporter_index < positions.size());
    const auto position = positions[reporter_index];
    const auto root = static_cast<std::uint32_t>(
        (target + 7U - position) % 7U);
    const auto parent_position = (position - 1U) / 2U;
    return {
        root,
        static_cast<ReplicaID>((root + parent_position) % 7U)};
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

const ReplicaAdaptationResult *ranking_entry(
    const hotstuff::AdaptationSnapshot &snapshot,
    ReplicaID replica_id)
{
    const auto found = std::find_if(
        snapshot.ranking().begin(),
        snapshot.ranking().end(),
        [replica_id](const auto &entry) {
            return entry.replica_id == replica_id;
        });
    return found == snapshot.ranking().end() ? nullptr : &*found;
}

struct Fixture
{
    std::vector<ReplicaID> members{membership()};
    AdaptiveV2ManagerIngressLimits limits{ingress_limits()};
    AdaptiveV2ManagerIngress ingress{
        members, epoch_zero(), 0, kActivationGeneration, limits};
    PrivKeySecp256k1 key{private_key()};
    AdaptiveV2ManagerControllerConfig config;
    std::unique_ptr<AdaptiveV2ManagerController> controller;
    std::array<std::uint64_t, 7> readiness_sequences{};
    std::array<std::uint64_t, 7> lifecycle_sequences{};
    std::array<std::uint64_t, 7> evidence_sequences{};
    std::uint64_t proposal_counter{0};

    explicit Fixture(std::uint64_t activation_delay_blocks = 5)
        : config(controller_config(key, activation_delay_blocks)),
          controller(std::make_unique<AdaptiveV2ManagerController>(
              ingress, config))
    {}

    void ready(ReplicaID source)
    {
        const AdaptiveV2ReadinessNotice notice{
            hotstuff::kAdaptiveV2ReadinessNoticeSchemaVersionV1,
            source,
            ++readiness_sequences[source],
            ingress.current_configuration(),
            kActivationGeneration,
            static_cast<std::uint64_t>(100 + source)};
        const auto result = ingress.ingest_readiness(
            AuthenticatedReporter{source},
            hotstuff::encode_adaptive_v2_readiness_notice(
                notice, limits.readiness_wire));
        REQUIRE(result.status ==
                AdaptiveV2ManagerIngressStatus::processed);
    }

    void ready_all()
    {
        for (const auto member : members)
            ready(member);
        REQUIRE(ingress.all_members_ready());
    }

    void admit(const ResponseObservation &observation)
    {
        for (ReplicaID source = 0; source < 3; ++source)
        {
            const ProposalLifecycleNotice notice{
                hotstuff::kProposalLifecycleNoticeSchemaVersion,
                source,
                ++lifecycle_sequences[source],
                ProposalLifecycleFact{
                    NormalProposalRuntimeInitialized{
                        observation.proposal_key()}}};
            const auto result = ingress.ingest_lifecycle(
                AuthenticatedReporter{source},
                hotstuff::encode_proposal_lifecycle_notice(
                    notice, limits.lifecycle_wire));
            if (source < 2)
            {
                REQUIRE(result.status ==
                        AdaptiveV2ManagerIngressStatus::
                            awaiting_corroboration);
            }
            else
            {
                REQUIRE(result.status ==
                        AdaptiveV2ManagerIngressStatus::processed);
            }
        }
    }

    void ingest(const ResponseObservation &observation)
    {
        const auto result = ingress.ingest_evidence(
            AuthenticatedReporter{observation.reporter_id},
            hotstuff::encode_evidence_batch(
                ResponseObservationBatch{
                    hotstuff::kEvidenceBatchSchemaVersion,
                    {observation}},
                limits.evidence_wire));
        REQUIRE(result.status ==
                AdaptiveV2ManagerIngressStatus::processed);
        REQUIRE(result.accepted_observations == 1);
        REQUIRE(result.rejected_observations == 0);
    }

    ResponseObservation observation(
        ReplicaID target,
        std::size_t reporter_index,
        ResponseOutcome outcome,
        const std::string &label)
    {
        const auto edge = leaf_edge(target, reporter_index);
        ResponseObservation value;
        value.reporter_id = edge.reporter;
        value.observed_replica_id = target;
        value.configuration = ConfigurationId{
            0, edge.tree_id, ingress.current_epoch().epoch_digest()};
        value.block_hash = digest(
            label + "-" + std::to_string(++proposal_counter));
        value.expected_message_type = ExpectedMessageType::direct_vote;
        value.outcome = outcome;
        value.response_duration_us =
            outcome == ResponseOutcome::timeout
                ? 0
                : static_cast<std::uint64_t>(20 + target);
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

    ResponseObservation record(
        ReplicaID target,
        std::size_t reporter_index,
        ResponseOutcome outcome,
        const std::string &label)
    {
        auto value = observation(
            target, reporter_index, outcome, label);
        admit(value);
        ingest(value);
        return value;
    }

    void record_late(const ResponseObservation &timed_out)
    {
        auto late = timed_out;
        late.outcome = ResponseOutcome::late;
        late.response_duration_us = 150;
        late.reporter_sequence =
            ++evidence_sequences[late.reporter_id];
        late.reporter_monotonic_ns =
            late.reporter_sequence * 1'000;
        late.signer_set = {late.observed_replica_id};
        late.observation_id =
            hotstuff::compute_response_observation_id(
                late.attempt_identity());
        ingest(late);
    }

    void responsive_attempts(ReplicaID target)
    {
        for (std::uint32_t attempt = 0; attempt < 2; ++attempt)
        {
            record(
                target,
                0,
                ResponseOutcome::on_time,
                "baseline-" + std::to_string(target));
        }
    }

    void complete_responsive_baseline()
    {
        for (const auto member : members)
            responsive_attempts(member);
    }

    void freeze_baseline()
    {
        ready_all();
        complete_responsive_baseline();
        REQUIRE(controller->evaluate() ==
                AdaptiveV2ManagerControllerStatus::baseline_frozen);
    }

    std::vector<ResponseObservation> persistent_timeouts(
        ReplicaID target,
        std::size_t reporter_count = 3,
        std::size_t reporter_start = 0)
    {
        std::vector<ResponseObservation> recorded;
        for (std::size_t offset = 0;
             offset < reporter_count;
             ++offset)
        {
            const auto reporter = reporter_start + offset;
            for (std::uint32_t attempt = 0;
                 attempt < kTimeoutsPerReporter;
                 ++attempt)
            {
                recorded.push_back(record(
                    target,
                    reporter,
                    ResponseOutcome::timeout,
                    "timeout-" + std::to_string(target) + "-" +
                        std::to_string(reporter)));
            }
        }
        return recorded;
    }
};

} // namespace

TEST_CASE(
    "N7 controller freezes only one exact all-responsive baseline",
    "[adaptive-v2][manager-controller][baseline][n7]")
{
    Fixture fixture;
    CHECK(fixture.controller->evaluate() ==
          AdaptiveV2ManagerControllerStatus::awaiting_readiness);
    CHECK_FALSE(fixture.controller->baseline_frozen());
    CHECK(fixture.controller->baseline_audit_snapshot() == nullptr);

    fixture.ready_all();
    CHECK(fixture.controller->evaluate() ==
          AdaptiveV2ManagerControllerStatus::
              awaiting_responsive_baseline);
    CHECK(fixture.ingress.ledger().high_watermark() == 0);

    for (ReplicaID target = 0; target < 6; ++target)
        fixture.responsive_attempts(target);
    CHECK(fixture.controller->evaluate() ==
          AdaptiveV2ManagerControllerStatus::
              awaiting_responsive_baseline);
    CHECK_FALSE(fixture.controller->baseline_frozen());
    CHECK(fixture.controller->baseline_audit_snapshot() == nullptr);

    fixture.responsive_attempts(6);
    const auto baseline_cutoff =
        fixture.ingress.ledger().high_watermark();
    REQUIRE(fixture.controller->evaluate() ==
            AdaptiveV2ManagerControllerStatus::baseline_frozen);
    REQUIRE(fixture.controller->baseline_audit_snapshot() != nullptr);
    const auto *baseline =
        fixture.controller->baseline_audit_snapshot();
    CHECK(baseline_cutoff == 14);
    CHECK(fixture.controller->baseline_cutoff() == baseline_cutoff);
    CHECK(fixture.controller->current_cutoff() == baseline_cutoff);
    CHECK(baseline->evidence_cutoff() == baseline_cutoff);
    CHECK(baseline->accepted_record_count() == 14);
    CHECK(baseline->epoch().epoch_number == 0);
    CHECK(baseline->epoch().epoch_digest ==
          fixture.ingress.current_epoch().epoch_digest());
    REQUIRE(baseline->ranking().size() == 7);
    for (const auto &entry : baseline->ranking())
    {
        CHECK(entry.classification ==
              ResponsivenessClass::responsive);
        CHECK(entry.eligible);
    }
    CHECK(fixture.controller->score_trajectory().size() == 14);

    CHECK(fixture.controller->evaluate() ==
          AdaptiveV2ManagerControllerStatus::
              awaiting_guarded_selection);
    CHECK(fixture.controller->baseline_audit_snapshot() == baseline);
    CHECK(fixture.controller->selection_audit() == nullptr);
    CHECK(fixture.controller->healthy());
}

TEST_CASE(
    "N7 controller waits for f plus one reporters then signs once",
    "[adaptive-v2][manager-controller][selection][successor][n7]")
{
    Fixture fixture;
    fixture.freeze_baseline();
    const auto baseline_cutoff = fixture.controller->baseline_cutoff();

    fixture.persistent_timeouts(0, 2);
    fixture.persistent_timeouts(1, 2);
    REQUIRE(fixture.controller->evaluate() ==
            AdaptiveV2ManagerControllerStatus::
                awaiting_guarded_selection);
    REQUIRE(fixture.controller->selection_audit() != nullptr);
    CHECK(fixture.controller->selection_audit()->status ==
          AdaptiveV2SelectionStatus::
              insufficient_guarded_candidates);
    CHECK(fixture.controller->selection_audit()
              ->selected_replicas.empty());
    CHECK(fixture.controller->successor_bundle() == nullptr);

    fixture.persistent_timeouts(0, 1, 2);
    fixture.persistent_timeouts(1, 1, 2);
    REQUIRE(fixture.controller->evaluate() ==
            AdaptiveV2ManagerControllerStatus::successor_ready);
    REQUIRE(fixture.controller->selection_audit() != nullptr);
    const auto *selection = fixture.controller->selection_audit();
    CHECK(selection->status == AdaptiveV2SelectionStatus::selected);
    CHECK(selection->metadata.replica_count == 7);
    CHECK(selection->metadata.fault_threshold == 2);
    CHECK(selection->metadata.quorum == 5);
    CHECK(selection->metadata.required_qualifying_reporters == 3);
    CHECK(selection->metadata.baseline_cutoff == baseline_cutoff);
    CHECK(selection->metadata.evidence_cutoff ==
          fixture.controller->current_cutoff());
    CHECK(selection->selected_replicas ==
          std::vector<ReplicaID>{0, 1});
    CHECK(selection->eligible_roots ==
          std::vector<ReplicaID>{2, 3, 4, 5, 6});
    REQUIRE(selection->eligible_candidates.size() == 2);
    for (const auto &candidate : selection->eligible_candidates)
    {
        CHECK(candidate.qualifying_reporters.size() == 3);
        CHECK(candidate.total_uncompensated_timeouts == 6);
        CHECK(candidate.baseline_score == 2);
        CHECK(candidate.current_score == -4);
        CHECK(candidate.baseline_score_delta == -6);
        CHECK(candidate.snapshot_nonresponsive);
        CHECK(candidate.score_drop_satisfied);
        CHECK(candidate.reporter_guard_satisfied);
        CHECK(candidate.guarded_eligible);
    }

    REQUIRE(fixture.controller->successor_bundle() != nullptr);
    const auto *bundle = fixture.controller->successor_bundle();
    const auto canonical_bytes = bundle->canonical_bytes();
    const auto successor_cutoff = fixture.controller->current_cutoff();
    const auto &definition = bundle->definition();
    CHECK(definition.epoch_number == 1);
    CHECK(definition.previous_epoch_digest ==
          fixture.ingress.current_epoch().epoch_digest());
    CHECK(definition.membership_digest ==
          fixture.ingress.current_epoch().membership_digest());
    CHECK(definition.evidence_cutoff ==
          selection->metadata.evidence_cutoff);
    REQUIRE(definition.trees.size() == 5);

    std::set<ReplicaID> roots;
    for (std::size_t index = 0;
         index < definition.trees.size();
         ++index)
    {
        const auto &tree = definition.trees[index];
        REQUIRE_FALSE(tree.members_breadth_first.empty());
        CHECK(tree.tree_id == index);
        CHECK(tree.members_breadth_first.front() ==
              selection->eligible_roots[index]);
        roots.insert(tree.members_breadth_first.front());
        CHECK(tree.wait_exempt_leaves ==
              std::vector<ReplicaID>{0, 1});
        auto tree_members = tree.members_breadth_first;
        std::sort(tree_members.begin(), tree_members.end());
        CHECK(tree_members == fixture.members);
        const auto leaf_start = first_leaf_index(
            tree.members_breadth_first.size(), tree.fanout);
        for (const auto selected : selection->selected_replicas)
        {
            const auto position = std::find(
                tree.members_breadth_first.begin(),
                tree.members_breadth_first.end(),
                selected);
            REQUIRE(position != tree.members_breadth_first.end());
            CHECK(static_cast<std::size_t>(std::distance(
                      tree.members_breadth_first.begin(), position)) >=
                  leaf_start);
        }
    }
    CHECK(roots == std::set<ReplicaID>{2, 3, 4, 5, 6});
    const auto quorum = hotstuff::derive_byzantine_quorum(
        fixture.members.size());
    REQUIRE(quorum.has_value());
    CHECK(quorum->fault_threshold == 2);
    CHECK(quorum->quorum == 5);
    CHECK(hotstuff::verify_epoch_change_signature(
        bundle->command(),
        EpochChangeIssuer{
            kIssuerId, hotstuff::PubKeySecp256k1(fixture.key)}));

    fixture.record(
        2, 0, ResponseOutcome::on_time, "after-successor-ready");
    CHECK(fixture.ingress.ledger().high_watermark() > successor_cutoff);
    CHECK(fixture.controller->evaluate() ==
          AdaptiveV2ManagerControllerStatus::already_ready);
    CHECK(fixture.controller->successor_bundle() == bundle);
    CHECK(fixture.controller->selection_audit() == selection);
    CHECK(fixture.controller->successor_bundle()->canonical_bytes() ==
          canonical_bytes);
    CHECK(fixture.controller->current_cutoff() == successor_cutoff);
    CHECK(fixture.controller->healthy());
}

TEST_CASE(
    "late compensation removes guarded eligibility at the next cutoff",
    "[adaptive-v2][manager-controller][late][selection][n7]")
{
    Fixture fixture;
    fixture.freeze_baseline();
    const auto target_one = fixture.persistent_timeouts(1);

    REQUIRE(fixture.controller->evaluate() ==
            AdaptiveV2ManagerControllerStatus::
                awaiting_guarded_selection);
    REQUIRE(fixture.controller->selection_audit() != nullptr);
    REQUIRE(fixture.controller->selection_audit()
                ->eligible_candidates.size() == 1);
    CHECK(fixture.controller->selection_audit()
              ->eligible_candidates.front().replica_id == 1);
    const auto eligible_cutoff = fixture.controller->current_cutoff();

    for (const auto &timed_out : target_one)
        fixture.record_late(timed_out);
    CHECK(fixture.ingress.ledger().high_watermark() > eligible_cutoff);

    REQUIRE(fixture.controller->evaluate() ==
            AdaptiveV2ManagerControllerStatus::
                awaiting_guarded_selection);
    REQUIRE(fixture.controller->selection_audit() != nullptr);
    const auto &selection = *fixture.controller->selection_audit();
    CHECK(selection.status == AdaptiveV2SelectionStatus::
                                  insufficient_guarded_candidates);
    REQUIRE(selection.snapshot != nullptr);
    REQUIRE(ranking_entry(*selection.snapshot, 1) != nullptr);
    CHECK(ranking_entry(*selection.snapshot, 1)->classification ==
          ResponsivenessClass::nonresponsive);
    CHECK(selection.eligible_candidates.empty());
    CHECK(selection.selected_replicas.empty());
    CHECK(fixture.controller->successor_bundle() == nullptr);

    const auto &trajectory = fixture.controller->score_trajectory();
    const auto last_target_one = std::find_if(
        trajectory.rbegin(),
        trajectory.rend(),
        [](const auto &update) { return update.target_id == 1; });
    REQUIRE(last_target_one != trajectory.rend());
    CHECK(last_target_one->delta == 1);
    CHECK(last_target_one->score == 2);
    CHECK(fixture.controller->healthy());
}

TEST_CASE(
    "controller fails closed when its borrowed ingress stops",
    "[adaptive-v2][manager-controller][health][shutdown][n7]")
{
    Fixture fixture;
    fixture.freeze_baseline();
    fixture.ingress.shutdown();

    CHECK(fixture.controller->evaluate() ==
          AdaptiveV2ManagerControllerStatus::unhealthy);
    CHECK_FALSE(fixture.controller->healthy());
    CHECK(fixture.controller->successor_bundle() == nullptr);
    CHECK(fixture.controller->evaluate() ==
          AdaptiveV2ManagerControllerStatus::unhealthy);
}

TEST_CASE(
    "controller treats successor factory rejection as terminal",
    "[adaptive-v2][manager-controller][factory][fail-closed][n7]")
{
    Fixture fixture(0);
    fixture.freeze_baseline();
    fixture.persistent_timeouts(0);
    fixture.persistent_timeouts(1);

    CHECK(fixture.controller->evaluate() ==
          AdaptiveV2ManagerControllerStatus::unhealthy);
    REQUIRE(fixture.controller->selection_audit() != nullptr);
    CHECK(fixture.controller->selection_audit()->status ==
          AdaptiveV2SelectionStatus::selected);
    CHECK(fixture.controller->successor_bundle() == nullptr);
    CHECK_FALSE(fixture.controller->healthy());
}
