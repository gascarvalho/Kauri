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
using hotstuff::AdaptiveV2SelectionConstraintBasis;
using hotstuff::AdaptiveV2SelectionStatus;
using hotstuff::AuthenticatedReporter;
using hotstuff::BaselineRoot;
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
using hotstuff::TreePolicyKind;
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
    config.transition_policy.intent =
        TreePolicyKind::fault_containment;
    config.transition_policy.containment_baseline_roots = {
        BaselineRoot{0, 0},
        BaselineRoot{1, 1},
        BaselineRoot{2, 2},
        BaselineRoot{3, 3},
        BaselineRoot{4, 4}};
    return config;
}

struct LeafEdge
{
    std::uint32_t tree_id{0};
    ReplicaID reporter{0};
};

std::size_t first_leaf_index(
    std::size_t member_count,
    std::uint32_t fanout)
{
    REQUIRE(fanout != 0);
    return member_count == 1
               ? 0
               : ((member_count - 2) / fanout) + 1;
}

LeafEdge leaf_edge(
    const hotstuff::EpochDefinition &epoch,
    ReplicaID target,
    std::size_t reporter_index)
{
    std::vector<LeafEdge> edges;
    std::set<ReplicaID> reporters;
    for (const auto &tree : epoch.trees())
    {
        const auto found = std::find(
            tree.members_breadth_first.begin(),
            tree.members_breadth_first.end(),
            target);
        if (found == tree.members_breadth_first.end())
            continue;
        const auto position = static_cast<std::size_t>(std::distance(
            tree.members_breadth_first.begin(), found));
        if (position < first_leaf_index(
                           tree.members_breadth_first.size(),
                           tree.fanout))
        {
            continue;
        }
        const auto parent_position = (position - 1U) / tree.fanout;
        const auto reporter =
            tree.members_breadth_first[parent_position];
        if (reporters.insert(reporter).second)
            edges.push_back({tree.tree_id, reporter});
    }
    REQUIRE(reporter_index < edges.size());
    return edges[reporter_index];
}

LeafEdge internal_edge(
    const hotstuff::EpochDefinition &epoch,
    ReplicaID target)
{
    std::vector<LeafEdge> edges;
    for (const auto &tree : epoch.trees())
    {
        const auto found = std::find(
            tree.members_breadth_first.begin(),
            tree.members_breadth_first.end(),
            target);
        if (found == tree.members_breadth_first.end())
            continue;
        const auto position = static_cast<std::size_t>(std::distance(
            tree.members_breadth_first.begin(), found));
        if (position == 0 ||
            position >= first_leaf_index(
                            tree.members_breadth_first.size(),
                            tree.fanout))
        {
            continue;
        }
        const auto parent_position = (position - 1U) / tree.fanout;
        edges.push_back(
            {tree.tree_id,
             tree.members_breadth_first[parent_position]});
    }
    REQUIRE_FALSE(edges.empty());
    return edges.front();
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

    explicit Fixture(
        std::uint64_t activation_delay_blocks = 5,
        std::size_t maximum_audit_updates = 4096,
        std::uint32_t required_nonresponsive = 2)
        : config(controller_config(key, activation_delay_blocks))
    {
        config.selection.required_nonresponsive =
            required_nonresponsive;
        config.reputation_limits.maximum_audit_updates =
            maximum_audit_updates;
        controller = std::make_unique<AdaptiveV2ManagerController>(
            ingress, config);
    }

    void ready(ReplicaID source)
    {
        const AdaptiveV2ReadinessNotice notice{
            hotstuff::kAdaptiveV2ReadinessNoticeSchemaVersionV1,
            source,
            ++readiness_sequences[source],
            ingress.current_configuration(),
            ingress.activation_generation(),
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
        for (ReplicaID source = 2; source < 5; ++source)
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
            if (source < 4)
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
        const auto edge = leaf_edge(
            ingress.current_epoch(), target, reporter_index);
        ResponseObservation value;
        value.reporter_id = edge.reporter;
        value.observed_replica_id = target;
        value.configuration = ConfigurationId{
            ingress.current_epoch().epoch_number(),
            edge.tree_id,
            ingress.current_epoch().epoch_digest()};
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

    void record_aggregate_timeout(
        ReplicaID target,
        const std::string &label)
    {
        const auto edge = internal_edge(
            ingress.current_epoch(), target);
        ResponseObservation value;
        value.reporter_id = edge.reporter;
        value.observed_replica_id = target;
        value.configuration = ConfigurationId{
            ingress.current_epoch().epoch_number(),
            edge.tree_id,
            ingress.current_epoch().epoch_digest()};
        value.block_hash = digest(
            label + "-" + std::to_string(++proposal_counter));
        value.expected_message_type =
            ExpectedMessageType::aggregate_relay;
        value.outcome = ResponseOutcome::timeout;
        value.response_duration_us = 0;
        value.deadline_duration_us = 100;
        value.reporter_sequence =
            ++evidence_sequences[value.reporter_id];
        value.reporter_monotonic_ns =
            value.reporter_sequence * 1'000;
        value.observation_id =
            hotstuff::compute_response_observation_id(
                value.attempt_identity());
        admit(value);
        ingest(value);
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

    void responsive_attempt_with_latency(
        ReplicaID target,
        std::size_t reporter_index,
        std::uint64_t latency,
        const std::string &label)
    {
        auto value = observation(
            target,
            reporter_index,
            ResponseOutcome::on_time,
            label);
        value.response_duration_us = latency;
        admit(value);
        ingest(value);
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

enum class InheritedEpochShape : std::uint8_t
{
    exact = 1,
    undersized,
    inconsistent,
};

EpochDefinitionInput exact_epoch_one(
    const AdaptiveV2ManagerIngress &ingress,
    InheritedEpochShape inherited_shape = InheritedEpochShape::exact)
{
    auto input = epoch_zero();
    input.epoch_number = 1;
    input.previous_epoch_digest =
        ingress.current_epoch().epoch_digest();
    input.membership_digest =
        ingress.current_epoch().membership_digest();
    input.policy_version = "adaptive-v2-live-survivor-baseline-v1";
    input.evidence_snapshot_id = "fresh-exact-e1";
    input.evidence_cutoff = 0;
    input.trees.clear();
    const std::vector<ReplicaID> live{2, 3, 4, 5, 6};
    for (std::size_t root_index = 0;
         root_index < live.size();
         ++root_index)
    {
        std::vector<ReplicaID> ordered;
        ordered.reserve(membership().size());
        for (std::size_t offset = 0; offset < live.size(); ++offset)
        {
            ordered.push_back(
                live[(root_index + offset) % live.size()]);
        }
        ordered.push_back(0);
        ordered.push_back(1);
        std::vector<ReplicaID> inherited{0, 1};
        if (inherited_shape == InheritedEpochShape::undersized)
            inherited = {0};
        else if (inherited_shape == InheritedEpochShape::inconsistent &&
                 root_index == 1)
            inherited = {0, 2};
        input.trees.push_back(EpochTreeDefinition{
            static_cast<std::uint32_t>(root_index),
            2,
            2,
            std::move(ordered),
            std::move(inherited)});
    }
    input.epoch_digest.reset();
    input.epoch_digest = hotstuff::compute_epoch_digest(input);
    return input;
}

void rotate_to_exact_epoch_one(
    Fixture &fixture,
    TreePolicyKind policy,
    InheritedEpochShape inherited_shape = InheritedEpochShape::exact)
{
    fixture.controller.reset();
    const auto successor = exact_epoch_one(
        fixture.ingress, inherited_shape);
    REQUIRE(fixture.ingress.rotate_to_successor(successor, 0) ==
            AdaptiveV2ManagerIngressStatus::processed);
    fixture.config.transition_policy.intent = policy;
    if (policy == TreePolicyKind::fault_containment)
    {
        fixture.config.transition_policy
            .containment_baseline_roots.clear();
        for (const auto &tree : fixture.ingress.current_epoch().trees())
        {
            REQUIRE_FALSE(tree.members_breadth_first.empty());
            fixture.config.transition_policy
                .containment_baseline_roots.push_back(BaselineRoot{
                    tree.tree_id,
                    tree.members_breadth_first.front()});
        }
    }
    else
    {
        fixture.config.transition_policy
            .containment_baseline_roots.clear();
    }
    fixture.controller =
        std::make_unique<AdaptiveV2ManagerController>(
            fixture.ingress, fixture.config);
}

void ready_live_survivors(Fixture &fixture)
{
    for (const auto source :
         std::vector<ReplicaID>{2, 3, 4, 5, 6})
    {
        fixture.ready(source);
    }
    REQUIRE(fixture.ingress.operationally_ready());
    REQUIRE_FALSE(fixture.ingress.all_members_ready());
}

void record_live_survivor_baseline(Fixture &fixture)
{
    for (const auto target :
         std::vector<ReplicaID>{2, 3, 4, 5, 6})
    {
        std::size_t reporter_index = 0;
        while (reporter_index < 3 &&
               leaf_edge(
                   fixture.ingress.current_epoch(),
                   target,
                   reporter_index)
                       .reporter < 2)
        {
            ++reporter_index;
        }
        REQUIRE(reporter_index < 3);
        for (std::uint32_t attempt = 0; attempt < 2; ++attempt)
        {
            fixture.record(
                target,
                reporter_index,
                ResponseOutcome::on_time,
                "e1-live-baseline-" + std::to_string(target));
        }
    }
}

void record_ranked_live_survivor_suffix(Fixture &fixture)
{
    const std::vector<std::uint64_t> latencies{50, 40, 30, 20, 10};
    const std::vector<std::size_t> attempt_counts{2, 3, 4, 2, 4};
    for (std::size_t index = 0; index < latencies.size(); ++index)
    {
        const auto target = static_cast<ReplicaID>(index + 2U);
        std::size_t reporter_index = 0;
        while (reporter_index < 3 &&
               leaf_edge(
                   fixture.ingress.current_epoch(),
                   target,
                   reporter_index)
                       .reporter < 2)
        {
            ++reporter_index;
        }
        REQUIRE(reporter_index < 3);
        for (std::size_t attempt = 0;
             attempt < attempt_counts[index];
             ++attempt)
        {
            fixture.responsive_attempt_with_latency(
                target,
                reporter_index,
                latencies[index],
                "e1-live-suffix-" + std::to_string(target));
        }
    }
}

void record_one_live_survivor_suffix_observation(
    Fixture &fixture,
    ReplicaID target,
    std::uint64_t latency)
{
    std::size_t reporter_index = 0;
    while (reporter_index < 3 &&
           leaf_edge(
               fixture.ingress.current_epoch(), target, reporter_index)
                   .reporter < 2)
    {
        ++reporter_index;
    }
    REQUIRE(reporter_index < 3);
    fixture.responsive_attempt_with_latency(
        target,
        reporter_index,
        latency,
        "e1-live-incomplete-suffix-" + std::to_string(target));
}

template<typename Config, typename = void>
struct has_controller_transition_policy : std::false_type
{};

template<typename Config>
struct has_controller_transition_policy<
    Config,
    std::void_t<decltype(
        std::declval<Config &>().transition_policy)>> : std::true_type
{};

std::vector<ReplicaID> successor_roots(
    const AdaptiveV2ManagerController &controller)
{
    REQUIRE(controller.successor_bundle() != nullptr);
    std::vector<ReplicaID> roots;
    for (const auto &tree :
         controller.successor_bundle()->definition().trees)
    {
        REQUIRE_FALSE(tree.members_breadth_first.empty());
        roots.push_back(tree.members_breadth_first.front());
    }
    return roots;
}

void check_controller_bundle_authority(
    const Fixture &fixture)
{
    REQUIRE(fixture.controller->successor_bundle() != nullptr);
    const auto &definition =
        fixture.controller->successor_bundle()->definition();
    CHECK(definition.epoch_number ==
          fixture.ingress.current_epoch().epoch_number() + 1U);
    CHECK(definition.previous_epoch_digest ==
          fixture.ingress.current_epoch().epoch_digest());
    CHECK(definition.membership_digest ==
          fixture.ingress.current_epoch().membership_digest());
    CHECK(fixture.ingress.membership() == membership());
    CHECK(fixture.ingress.quorum_metadata().replica_count == 7);
    CHECK(fixture.ingress.quorum_metadata().fault_threshold == 2);
    CHECK(fixture.ingress.quorum_metadata().quorum == 5);
    for (const auto &tree : definition.trees)
    {
        CHECK(tree.wait_exempt_leaves ==
              std::vector<ReplicaID>{0, 1});
        const auto leaf_start = first_leaf_index(
            tree.members_breadth_first.size(), tree.fanout);
        for (const auto selected : {ReplicaID{0}, ReplicaID{1}})
        {
            const auto position = std::find(
                tree.members_breadth_first.begin(),
                tree.members_breadth_first.end(),
                selected);
            REQUIRE(position != tree.members_breadth_first.end());
            CHECK(static_cast<std::size_t>(std::distance(
                      tree.members_breadth_first.begin(),
                      position)) >= leaf_start);
        }
    }
}

template<typename Config>
void verify_controller_transition_policy_contract()
{
    if constexpr (!has_controller_transition_policy<Config>::value)
    {
        FAIL(
            "M12-R01 RED: AdaptiveV2ManagerControllerConfig has no "
            "explicit transition_policy");
    }
    else
    {
        Fixture containment;
        containment.controller.reset();
        Config containment_config = containment.config;
        containment_config.transition_policy.intent =
            TreePolicyKind::fault_containment;
        containment_config.transition_policy
            .containment_baseline_roots = {
                BaselineRoot{0, 0},
                BaselineRoot{1, 1},
                BaselineRoot{2, 2},
                BaselineRoot{3, 3},
                BaselineRoot{4, 4}};
        containment.config = std::move(containment_config);
        containment.controller =
            std::make_unique<AdaptiveV2ManagerController>(
                containment.ingress, containment.config);
        containment.freeze_baseline();
        containment.persistent_timeouts(0);
        containment.persistent_timeouts(1);
        REQUIRE(containment.controller->evaluate() ==
                AdaptiveV2ManagerControllerStatus::successor_ready);
        CHECK(successor_roots(*containment.controller) ==
              std::vector<ReplicaID>{5, 6, 2, 3, 4});
        check_controller_bundle_authority(containment);

        Fixture optimization;
        optimization.controller.reset();
        Config optimization_config = optimization.config;
        optimization_config.transition_policy.intent =
            TreePolicyKind::performance_optimization;
        optimization_config.transition_policy
            .containment_baseline_roots.clear();
        optimization.config = std::move(optimization_config);
        optimization.controller =
            std::make_unique<AdaptiveV2ManagerController>(
                optimization.ingress, optimization.config);
        optimization.freeze_baseline();
        optimization.persistent_timeouts(0);
        optimization.persistent_timeouts(1);
        REQUIRE(optimization.controller->evaluate() ==
                AdaptiveV2ManagerControllerStatus::successor_ready);
        REQUIRE(optimization.controller->selection_audit() != nullptr);
        CHECK(optimization.controller->selection_audit()
                  ->constraint_basis ==
              AdaptiveV2SelectionConstraintBasis::guarded_evidence);
        CHECK(successor_roots(*optimization.controller) ==
              std::vector<ReplicaID>{2, 3, 4, 5, 6});
        check_controller_bundle_authority(optimization);

        CHECK(successor_roots(*containment.controller) !=
              successor_roots(*optimization.controller));
    }
}

} // namespace

TEST_CASE(
    "N7 controller freezes only one exact all-responsive baseline",
    "[adaptive-v2][manager-controller][baseline][n7]")
{
    Fixture fixture;
    fixture.controller.reset();
    fixture.config.transition_policy.intent =
        TreePolicyKind::fault_containment;
    fixture.config.transition_policy.containment_baseline_roots = {
        BaselineRoot{0, 0},
        BaselineRoot{1, 1},
        BaselineRoot{2, 2},
        BaselineRoot{3, 3},
        BaselineRoot{4, 4}};
    fixture.controller =
        std::make_unique<AdaptiveV2ManagerController>(
            fixture.ingress, fixture.config);
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
    "N7 v13 baseline ignores aggregate relay timeout noise",
    "[adaptive-v2][manager-controller][baseline][direct-vote][v13][n7]")
{
    Fixture fixture;
    fixture.controller.reset();
    fixture.config.selection.responsiveness_policy.policy_version =
        "shape25-direct-vote-responsiveness-v2";
    fixture.controller =
        std::make_unique<AdaptiveV2ManagerController>(
            fixture.ingress, fixture.config);

    fixture.ready_all();
    fixture.complete_responsive_baseline();
    fixture.record_aggregate_timeout(0, "baseline-aggregate-noise");
    const auto baseline_cutoff =
        fixture.ingress.ledger().high_watermark();

    REQUIRE(fixture.controller->evaluate() ==
            AdaptiveV2ManagerControllerStatus::baseline_frozen);
    REQUIRE(fixture.controller->baseline_audit_snapshot() != nullptr);
    const auto &baseline =
        *fixture.controller->baseline_audit_snapshot();
    const auto *target = ranking_entry(baseline, 0);
    REQUIRE(target != nullptr);

    CHECK(baseline_cutoff == 15);
    CHECK(baseline.accepted_record_count() == 15);
    CHECK(target->attempt_count == 2);
    CHECK(target->response_count == 2);
    CHECK(target->timeout_count == 0);
    CHECK(target->classification == ResponsivenessClass::responsive);
    CHECK(target->eligible);
}

TEST_CASE(
    "controller progresses at operational Q5 and waits below quorum",
    "[adaptive-v2][manager-controller][readiness][operational][quorum]"
    "[n7][intentional-red]")
{
    Fixture fixture;
    for (const auto source :
         std::vector<ReplicaID>{2, 3, 4, 5})
    {
        fixture.ready(source);
    }
    CHECK_FALSE(fixture.ingress.operationally_ready());
    CHECK(fixture.controller->evaluate() ==
          AdaptiveV2ManagerControllerStatus::awaiting_readiness);

    fixture.ready(6);
    REQUIRE(fixture.ingress.operationally_ready());
    REQUIRE_FALSE(fixture.ingress.all_members_ready());
    CHECK(fixture.ingress.quorum_metadata().replica_count == 7);
    CHECK(fixture.ingress.quorum_metadata().fault_threshold == 2);
    CHECK(fixture.ingress.quorum_metadata().quorum == 5);
    CHECK(fixture.controller->evaluate() ==
          AdaptiveV2ManagerControllerStatus::
              awaiting_responsive_baseline);
}

TEST_CASE(
    "recurring policies inherit an exact E1 containment set",
    "[adaptive-v2][manager-controller][baseline][transition-policy][n7]"
    "[live-feasibility][intentional-red]")
{
    SECTION(
        "fault containment preserves current roots without fresh actor evidence")
    {
        Fixture containment;
        rotate_to_exact_epoch_one(
            containment, TreePolicyKind::fault_containment);
        ready_live_survivors(containment);
        record_live_survivor_baseline(containment);

        REQUIRE(containment.controller->evaluate() ==
                AdaptiveV2ManagerControllerStatus::baseline_frozen);
        REQUIRE(containment.controller->baseline_audit_snapshot() !=
                nullptr);
        for (const auto actor : std::vector<ReplicaID>{0, 1})
        {
            const auto *entry = ranking_entry(
                *containment.controller->baseline_audit_snapshot(),
                actor);
            REQUIRE(entry != nullptr);
            CHECK(entry->classification ==
                  ResponsivenessClass::insufficient_evidence);
            CHECK_FALSE(entry->eligible);
        }

        record_ranked_live_survivor_suffix(containment);
        REQUIRE(containment.controller->evaluate() ==
                AdaptiveV2ManagerControllerStatus::successor_ready);
        REQUIRE(containment.controller->selection_audit() != nullptr);
        const auto &selection =
            *containment.controller->selection_audit();
        CHECK(selection.constraint_basis ==
              AdaptiveV2SelectionConstraintBasis::
                  inherited_consensus_wait_exempt);
        CHECK(selection.metadata.replica_count == 7);
        CHECK(selection.metadata.fault_threshold == 2);
        CHECK(selection.metadata.quorum == 5);
        CHECK(selection.metadata.required_nonresponsive == 2);
        CHECK(selection.selected_replicas ==
              std::vector<ReplicaID>{0, 1});
        CHECK(selection.eligible_candidates.empty());
        CHECK(selection.eligible_roots ==
              std::vector<ReplicaID>{6, 5, 4, 3, 2});
        CHECK(std::all_of(
            containment.ingress.ledger().accepted().begin(),
            containment.ingress.ledger().accepted().end(),
            [](const auto &record) {
                return record.observation.observed_replica_id >= 2 &&
                       record.observation.outcome ==
                           ResponseOutcome::on_time;
            }));

        REQUIRE(containment.controller->successor_bundle() != nullptr);
        const auto &definition =
            containment.controller->successor_bundle()->definition();
        CHECK(definition.membership_digest ==
              containment.ingress.current_epoch().membership_digest());
        CHECK(successor_roots(*containment.controller) ==
              std::vector<ReplicaID>{2, 3, 4, 5, 6});
        for (const auto &tree : definition.trees)
        {
            CHECK(tree.wait_exempt_leaves ==
                  std::vector<ReplicaID>{0, 1});
            const auto leaf_start = first_leaf_index(
                tree.members_breadth_first.size(), tree.fanout);
            for (const auto actor : std::vector<ReplicaID>{0, 1})
            {
                const auto position = std::find(
                    tree.members_breadth_first.begin(),
                    tree.members_breadth_first.end(),
                    actor);
                REQUIRE(position != tree.members_breadth_first.end());
                CHECK(static_cast<std::size_t>(std::distance(
                          tree.members_breadth_first.begin(),
                          position)) >= leaf_start);
            }
        }
    }

    SECTION(
        "performance optimization inherits containment and uses a fresh suffix")
    {
        Fixture optimization;
        rotate_to_exact_epoch_one(
            optimization, TreePolicyKind::performance_optimization);
        ready_live_survivors(optimization);
        record_live_survivor_baseline(optimization);

        const auto baseline_cutoff =
            optimization.ingress.ledger().high_watermark();
        REQUIRE(baseline_cutoff == 10);
        REQUIRE(optimization.controller->evaluate() ==
                AdaptiveV2ManagerControllerStatus::baseline_frozen);
        REQUIRE(optimization.controller->baseline_audit_snapshot() !=
                nullptr);
        CHECK(optimization.controller->baseline_cutoff() ==
              baseline_cutoff);
        for (const auto live :
             std::vector<ReplicaID>{2, 3, 4, 5, 6})
        {
            const auto *entry = ranking_entry(
                *optimization.controller->baseline_audit_snapshot(),
                live);
            REQUIRE(entry != nullptr);
            CHECK(entry->classification ==
                  ResponsivenessClass::responsive);
            CHECK(entry->eligible);
        }
        for (const auto crashed :
             std::vector<ReplicaID>{0, 1})
        {
            const auto *entry = ranking_entry(
                *optimization.controller->baseline_audit_snapshot(),
                crashed);
            REQUIRE(entry != nullptr);
            CHECK((entry->classification ==
                       ResponsivenessClass::insufficient_evidence ||
                   entry->classification ==
                       ResponsivenessClass::nonresponsive));
            CHECK_FALSE(entry->eligible);
        }

        CHECK(optimization.controller->evaluate() ==
              AdaptiveV2ManagerControllerStatus::
                  awaiting_guarded_selection);
        CHECK(optimization.controller->successor_bundle() == nullptr);
        CHECK(optimization.controller->current_cutoff() ==
              baseline_cutoff);
        const auto baseline_trajectory_size =
            optimization.controller->score_trajectory().size();

        record_one_live_survivor_suffix_observation(
            optimization, 6, 10);
        const auto incomplete_cutoff =
            optimization.ingress.ledger().high_watermark();
        REQUIRE(incomplete_cutoff > baseline_cutoff);
        REQUIRE(optimization.controller->evaluate() ==
                AdaptiveV2ManagerControllerStatus::
                    awaiting_guarded_selection);
        REQUIRE(optimization.controller->selection_audit() != nullptr);
        CHECK(optimization.controller->selection_audit()->status ==
              AdaptiveV2SelectionStatus::insufficient_eligible_roots);
        REQUIRE(optimization.controller->selection_audit()->snapshot !=
                nullptr);
        CHECK(optimization.controller->selection_audit()
                  ->snapshot->accepted_record_count() == 1);
        CHECK(optimization.controller->current_cutoff() ==
              baseline_cutoff);
        CHECK(optimization.controller->score_trajectory().size() ==
              baseline_trajectory_size);
        CHECK(optimization.controller->successor_bundle() == nullptr);

        record_ranked_live_survivor_suffix(optimization);
        const auto suffix_cutoff =
            optimization.ingress.ledger().high_watermark();
        const auto suffix_record_count =
            suffix_cutoff - baseline_cutoff;
        REQUIRE(suffix_cutoff > incomplete_cutoff);
        REQUIRE(suffix_record_count >
                static_cast<std::uint64_t>(
                    optimization.ingress.quorum_metadata().quorum *
                    optimization.config.selection
                        .responsiveness_policy.minimum_attempts));
        REQUIRE(optimization.controller->evaluate() ==
                AdaptiveV2ManagerControllerStatus::successor_ready);
        REQUIRE(optimization.controller->selection_audit() != nullptr);
        CHECK(optimization.controller->selection_audit()
                  ->metadata.baseline_cutoff == baseline_cutoff);
        CHECK(optimization.controller->selection_audit()
                  ->metadata.evidence_cutoff > baseline_cutoff);
        REQUIRE(optimization.controller->selection_audit()->snapshot !=
                nullptr);
        CHECK(optimization.controller->selection_audit()
                  ->snapshot->accepted_record_count() ==
              suffix_record_count);
        CHECK(optimization.controller->selection_audit()
                  ->selected_replicas ==
              std::vector<ReplicaID>{0, 1});
        CHECK(optimization.controller->selection_audit()
                  ->constraint_basis ==
              AdaptiveV2SelectionConstraintBasis::
                  inherited_consensus_wait_exempt);
        CHECK(optimization.controller->selection_audit()
                  ->eligible_candidates.empty());
        CHECK(optimization.controller->selection_audit()
                  ->eligible_roots ==
              std::vector<ReplicaID>{6, 5, 4, 3, 2});
        CHECK(std::all_of(
            optimization.ingress.ledger().accepted().begin(),
            optimization.ingress.ledger().accepted().end(),
            [](const auto &record) {
                return record.observation.outcome ==
                       ResponseOutcome::on_time;
            }));
        REQUIRE(optimization.controller->successor_bundle() != nullptr);
        CHECK(successor_roots(*optimization.controller) ==
              std::vector<ReplicaID>{6, 5, 4, 3, 2});
        for (const auto &tree :
             optimization.controller->successor_bundle()
                 ->definition()
                 .trees)
        {
            CHECK(tree.wait_exempt_leaves ==
                  std::vector<ReplicaID>{0, 1});
        }
    }
}

TEST_CASE(
    "recurring policies reject malformed predecessor containment sets",
    "[adaptive-v2][manager-controller][inheritance][fail-closed][n7]")
{
    for (const auto policy :
         std::vector<TreePolicyKind>{
             TreePolicyKind::fault_containment,
             TreePolicyKind::performance_optimization})
    {
        for (const auto inherited_shape :
             std::vector<InheritedEpochShape>{
                 InheritedEpochShape::undersized,
                 InheritedEpochShape::inconsistent})
        {
            Fixture fixture;
            rotate_to_exact_epoch_one(
                fixture, policy, inherited_shape);
            CHECK_FALSE(fixture.controller->healthy());
            CHECK(fixture.controller->evaluate() ==
                  AdaptiveV2ManagerControllerStatus::unhealthy);
            CHECK(fixture.controller->successor_bundle() == nullptr);
            CHECK(fixture.controller->selection_audit() == nullptr);
        }
    }
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
    CHECK(selection->constraint_basis ==
          AdaptiveV2SelectionConstraintBasis::guarded_evidence);
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
    const std::vector<ReplicaID> expected_roots{5, 6, 2, 3, 4};
    for (std::size_t index = 0;
         index < definition.trees.size();
         ++index)
    {
        const auto &tree = definition.trees[index];
        REQUIRE_FALSE(tree.members_breadth_first.empty());
        CHECK(tree.tree_id == index);
        CHECK(tree.members_breadth_first.front() ==
              expected_roots[index]);
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
    "controller contains a configured minority without changing quorum",
    "[adaptive-v2][manager-controller][minority][roots]")
{
    Fixture fixture(5, 4096, 1);
    fixture.freeze_baseline();

    fixture.persistent_timeouts(6, 3);
    REQUIRE(fixture.controller->evaluate() ==
            AdaptiveV2ManagerControllerStatus::successor_ready);

    const auto *selection = fixture.controller->selection_audit();
    REQUIRE(selection != nullptr);
    CHECK(selection->metadata.fault_threshold == 2);
    CHECK(selection->metadata.quorum == 5);
    CHECK(selection->metadata.required_nonresponsive == 1);
    CHECK(selection->selected_replicas ==
          std::vector<ReplicaID>{6});
    CHECK(selection->eligible_roots.size() == 5);

    const auto *bundle = fixture.controller->successor_bundle();
    REQUIRE(bundle != nullptr);
    REQUIRE(bundle->definition().trees.size() == 5);
    for (const auto &tree : bundle->definition().trees)
    {
        CHECK(tree.wait_exempt_leaves ==
              std::vector<ReplicaID>{6});
        CHECK(tree.members_breadth_first.front() != 6);
    }
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
    "controller propagates its bounded reputation audit capacity",
    "[adaptive-v2][manager-controller][capacity][reputation][n7]")
{
    Fixture fixture(5, 13);
    fixture.ready_all();
    fixture.complete_responsive_baseline();

    CHECK(fixture.ingress.ledger().accepted().size() == 14);
    CHECK(fixture.controller->evaluate() ==
          AdaptiveV2ManagerControllerStatus::unhealthy);
    CHECK_FALSE(fixture.controller->healthy());
    CHECK(fixture.controller->score_trajectory().empty());
    CHECK(fixture.controller->successor_bundle() == nullptr);
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

TEST_CASE(
    "controller routes explicit policy without consulting epoch ordinal",
    "[adaptive-v2][manager-controller][transition-policy][n7][intentional-red]")
{
    verify_controller_transition_policy_contract<
        AdaptiveV2ManagerControllerConfig>();
}

TEST_CASE(
    "legacy positional controller config keeps optimization default",
    "[adaptive-v2][manager-controller][transition-policy][compatibility]")
{
    Fixture fixture;
    fixture.controller.reset();
    const auto configured = fixture.config;
    AdaptiveV2ManagerControllerConfig legacy{
        configured.selection,
        configured.reputation_limits,
        configured.placement,
        configured.activation_delay_blocks,
        configured.issuer_id,
        configured.issuer_private_key,
        configured.bundle_limits};

    CHECK(legacy.transition_policy.intent ==
          TreePolicyKind::performance_optimization);
    CHECK(legacy.transition_policy.containment_baseline_roots.empty());

    fixture.config = std::move(legacy);
    fixture.controller =
        std::make_unique<AdaptiveV2ManagerController>(
            fixture.ingress, fixture.config);
    fixture.freeze_baseline();
    fixture.persistent_timeouts(0);
    fixture.persistent_timeouts(1);
    REQUIRE(fixture.controller->evaluate() ==
            AdaptiveV2ManagerControllerStatus::successor_ready);
    REQUIRE(fixture.controller->selection_audit() != nullptr);
    CHECK(fixture.controller->selection_audit()->constraint_basis ==
          AdaptiveV2SelectionConstraintBasis::guarded_evidence);
    CHECK(successor_roots(*fixture.controller) ==
          std::vector<ReplicaID>{2, 3, 4, 5, 6});
    check_controller_bundle_authority(fixture);
}

TEST_CASE(
    "controller maps a production N-tree predecessor to a Q-tree successor",
    "[shape25][adaptive-v2][manager-controller][shape-v1][n7]")
{
    Fixture fixture;
    fixture.freeze_baseline();
    fixture.persistent_timeouts(5);
    fixture.persistent_timeouts(6);

    REQUIRE(fixture.controller->evaluate() ==
            AdaptiveV2ManagerControllerStatus::successor_ready);
    REQUIRE(fixture.controller->shape_decision() != nullptr);
    const auto decision = *fixture.controller->shape_decision();
    CHECK(decision.evidence_cutoff ==
          fixture.controller->current_cutoff());
    CHECK(decision.selected_fanout == 5);
    CHECK(decision.applied_fanout == 2);
    CHECK(decision.fixed_pipeline_stretch == 2);
    CHECK(fixture.ingress.current_epoch().trees().size() == 7);
    CHECK(decision.predecessor_tree_count == 7);
    CHECK(decision.tree_count ==
          fixture.ingress.quorum_metadata().quorum);
    CHECK(decision.reference_tree_rule ==
          hotstuff::kShapeV1ReferenceTreeRule);
    CHECK(hotstuff::valid_shape_decision_record(decision));

    const auto *bundle = fixture.controller->successor_bundle();
    REQUIRE(bundle != nullptr);
    CHECK(bundle->definition().trees.size() ==
          fixture.ingress.quorum_metadata().quorum);
    for (const auto &tree : bundle->definition().trees)
    {
        CHECK(tree.fanout == decision.current_fanout);
        CHECK(tree.pipeline_stretch == 2);
        CHECK(tree.wait_exempt_leaves ==
              std::vector<ReplicaID>{5, 6});
    }

    fixture.record(2, 0, ResponseOutcome::on_time, "after-shape-cutoff");
    CHECK(fixture.controller->evaluate() ==
          AdaptiveV2ManagerControllerStatus::already_ready);
    REQUIRE(fixture.controller->shape_decision() != nullptr);
    CHECK(fixture.controller->shape_decision()->decision_digest ==
          decision.decision_digest);
    CHECK(fixture.controller->shape_decision()->evidence_cutoff ==
          decision.evidence_cutoff);
}

TEST_CASE(
    "shape factor controls application without entering shape-v1",
    "[shape25][adaptive-v2][manager-controller][shape-factor][n7]")
{
    Fixture fixture;
    fixture.controller.reset();
    fixture.config.shape_adaptation_enabled = true;
    fixture.config.shape_selection.candidate_fanouts = {5, 2, 3, 5};
    fixture.controller =
        std::make_unique<AdaptiveV2ManagerController>(
            fixture.ingress, fixture.config);
    fixture.freeze_baseline();
    fixture.persistent_timeouts(5);
    fixture.persistent_timeouts(6);

    REQUIRE(fixture.controller->evaluate() ==
            AdaptiveV2ManagerControllerStatus::successor_ready);
    REQUIRE(fixture.controller->shape_decision() != nullptr);
    const auto &decision = *fixture.controller->shape_decision();
    CHECK(decision.selected_fanout == 5);
    CHECK(decision.applied_fanout == decision.selected_fanout);
    REQUIRE(fixture.controller->successor_bundle() != nullptr);
    for (const auto &tree :
         fixture.controller->successor_bundle()->definition().trees)
    {
        CHECK(tree.fanout == decision.selected_fanout);
        CHECK(tree.pipeline_stretch == 2);
    }
}

TEST_CASE(
    "later shape cycle derives current fanout from the exact epoch",
    "[shape25][adaptive-v2][manager-controller][shape-v1][recurring][n7]")
{
    Fixture fixture;
    fixture.config.placement.shape.fanout = 5;
    fixture.config.shape_selection.deterministic_seed =
        kSnapshotSeed + 12;
    rotate_to_exact_epoch_one(
        fixture, TreePolicyKind::performance_optimization);
    ready_live_survivors(fixture);
    record_live_survivor_baseline(fixture);
    REQUIRE(fixture.controller->evaluate() ==
            AdaptiveV2ManagerControllerStatus::baseline_frozen);
    record_ranked_live_survivor_suffix(fixture);

    REQUIRE(fixture.controller->evaluate() ==
            AdaptiveV2ManagerControllerStatus::successor_ready);
    REQUIRE(fixture.controller->shape_decision() != nullptr);
    const auto &decision = *fixture.controller->shape_decision();
    CHECK(fixture.config.placement.shape.fanout == 5);
    CHECK(decision.current_fanout == 2);
    CHECK(decision.applied_fanout == 2);
    CHECK(decision.predecessor_tree_count == 5);
    CHECK(decision.tree_count == 5);
    CHECK(decision.deterministic_seed == kSnapshotSeed + 12);
    REQUIRE(fixture.controller->successor_bundle() != nullptr);
    for (const auto &tree :
         fixture.controller->successor_bundle()->definition().trees)
    {
        CHECK(tree.fanout == 2);
        CHECK(tree.pipeline_stretch ==
              fixture.config.placement.shape.pipeline_stretch);
    }
}
