#include <algorithm>
#include <array>
#include <cstddef>
#include <cstdint>
#include <memory>
#include <map>
#include <optional>
#include <set>
#include <stdexcept>
#include <string>
#include <type_traits>
#include <utility>
#include <vector>

#include "catch.hpp"
#include "hotstuff/adaptation_manager_profile.h"
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
using hotstuff::AdaptiveV2TimeoutAuditBasis;
using hotstuff::AuthenticatedReporter;
using hotstuff::BaselineRoot;
using hotstuff::ConfigurationId;
using hotstuff::DataStream;
using hotstuff::EpochChangeBundleLimits;
using hotstuff::EpochChangeIssuer;
using hotstuff::EpochDefinitionInput;
using hotstuff::EpochProtocolMode;
using hotstuff::EpochTreeDefinition;
using hotstuff::EpochWireLimits;
using hotstuff::ExpectedMessageType;
using hotstuff::NormalProposalRuntimeInitialized;
using hotstuff::PrivKeySecp256k1;
using hotstuff::ProposalKey;
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

    ResponseObservation direct_vote_anchor(
        const ProposalKey &proposal,
        std::optional<std::pair<ReplicaID, ReplicaID>> avoid =
            std::nullopt)
    {
        const auto &trees = ingress.current_epoch().trees();
        const auto tree = std::find_if(
            trees.begin(), trees.end(), [&proposal](const auto &entry) {
                return entry.tree_id ==
                       proposal.configuration.tree_id;
            });
        REQUIRE(tree != trees.end());
        const auto leaf_start = first_leaf_index(
            tree->members_breadth_first.size(), tree->fanout);
        for (std::size_t position = leaf_start;
             position < tree->members_breadth_first.size();
             ++position)
        {
            const auto parent_position =
                (position - 1U) / tree->fanout;
            const auto reporter =
                tree->members_breadth_first[parent_position];
            const auto target =
                tree->members_breadth_first[position];
            if (avoid.has_value() &&
                avoid->first == reporter &&
                avoid->second == target)
            {
                continue;
            }

            ResponseObservation value;
            value.reporter_id = reporter;
            value.observed_replica_id = target;
            value.configuration = proposal.configuration;
            value.block_hash = proposal.block_hash;
            value.expected_message_type =
                ExpectedMessageType::direct_vote;
            value.outcome = ResponseOutcome::on_time;
            value.response_duration_us = 0;
            value.deadline_duration_us = 100;
            value.reporter_sequence =
                ++evidence_sequences[value.reporter_id];
            value.reporter_monotonic_ns =
                value.reporter_sequence * 1'000;
            value.signer_set = {target};
            value.observation_id =
                hotstuff::compute_response_observation_id(
                    value.attempt_identity());
            return value;
        }
        throw std::logic_error(
            "test tree has no distinct direct-vote anchor");
    }

    void cover_tree(std::uint32_t tree_id)
    {
        const ProposalKey proposal{
            ConfigurationId{
                ingress.current_epoch().epoch_number(),
                tree_id,
                ingress.current_epoch().epoch_digest()},
            digest("post-fault-coverage-" +
                   std::to_string(++proposal_counter))};
        auto anchor = direct_vote_anchor(proposal);
        admit(anchor);
        ingest(anchor);
    }

    void anchor_timeout_proposal(
        const ResponseObservation &timeout)
    {
        auto anchor = direct_vote_anchor(
            timeout.proposal_key(),
            std::make_pair(
                timeout.reporter_id,
                timeout.observed_replica_id));
        ingest(anchor);
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

std::vector<ReplicaID> n31_membership()
{
    std::vector<ReplicaID> members;
    members.reserve(31);
    for (ReplicaID replica = 0; replica < 31; ++replica)
        members.push_back(replica);
    return members;
}

struct N31ControllerFixture
{
    std::vector<ReplicaID> members{n31_membership()};
    hotstuff::AdaptiveV2ManagerRuntimeShape shape;
    AdaptiveV2ManagerIngress ingress;
    PrivKeySecp256k1 key{private_key()};
    AdaptiveV2ManagerControllerConfig config;
    std::unique_ptr<AdaptiveV2ManagerController> controller;
    std::vector<std::uint64_t> readiness_sequences;
    std::vector<std::uint64_t> lifecycle_sequences;
    std::vector<std::uint64_t> evidence_sequences;
    std::map<std::pair<std::string, std::uint32_t>, uint256_t>
        admitted_proposals;
    std::uint64_t next_attempt_start_ns{1'000};

    N31ControllerFixture()
        : shape(*hotstuff::derive_adaptive_v2_manager_runtime_shape(
              members, 5, 2)),
          ingress(
              members,
              *hotstuff::derive_adaptive_v2_cyclic_epoch_zero(
                  members, 5, 2),
              0,
              kActivationGeneration,
              shape.ingress_limits),
          readiness_sequences(members.size()),
          lifecycle_sequences(members.size()),
          evidence_sequences(members.size())
    {
        config.selection.required_nonresponsive = 3;
        config.selection.minimum_score_drop = 1;
        config.selection.minimum_timeouts_per_reporter = 1;
        config.selection.maximum_post_baseline_timeout_attempts =
            shape.maximum_post_baseline_timeout_attempts;
        config.selection.responsiveness_policy.policy_version =
            "adaptive-v2-v9-n31-controller-v1";
        config.selection.responsiveness_policy.attempt_window = 64;
        config.selection.responsiveness_policy.minimum_attempts = 2;
        config.selection.responsiveness_policy.minimum_response_rate_ppm =
            1'000'000;
        config.selection.responsiveness_policy.maximum_timeout_rate_ppm = 0;
        config.selection.responsiveness_policy.trailing_timeout_streak = 2;
        config.selection.responsiveness_policy
            .latency_percentile_basis_points = 5'000;
        config.selection.snapshot_seed = kSnapshotSeed;
        config.selection.fault_window_arm_required = true;
        config.selection.cardinality_policy =
            hotstuff::AdaptiveV2FaultWindowCardinalityPolicy::
                all_guarded_up_to_fault_bound_v1;
        config.reputation_limits.maximum_audit_updates =
            shape.ingress_limits.evidence_store.maximum_accepted_records;
        config.placement = TreePlacementInput{
            members,
            shape.tree_shape,
            kSnapshotSeed,
            "adaptive-v2-v9-n31-controller-v1"};
        config.activation_delay_blocks = 5;
        config.issuer_id = kIssuerId;
        config.issuer_private_key = key;
        config.bundle_limits = shape.bundle_limits;
        config.transition_policy.intent = TreePolicyKind::fault_containment;
        for (std::uint32_t tree_id = 0;
             tree_id < shape.tree_shape.tree_count;
             ++tree_id)
        {
            const auto &tree = ingress.current_epoch().trees()[tree_id];
            config.transition_policy.containment_baseline_roots.push_back(
                BaselineRoot{
                    tree.tree_id,
                    tree.members_breadth_first.front()});
        }
        controller = std::make_unique<AdaptiveV2ManagerController>(
            ingress, config);
    }

    void ready_quorum(const std::set<ReplicaID> &excluded = {})
    {
        std::size_t admitted = 0;
        for (const auto source : members)
        {
            if (excluded.count(source) != 0)
                continue;
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
                    notice, shape.ingress_limits.readiness_wire));
            REQUIRE(result.status ==
                    AdaptiveV2ManagerIngressStatus::processed);
            if (++admitted == ingress.quorum_metadata().quorum)
                break;
        }
        REQUIRE(ingress.operationally_ready());
    }

    std::uint32_t nonroot_tree(ReplicaID target) const
    {
        for (const auto &tree : ingress.current_epoch().trees())
        {
            if (!tree.members_breadth_first.empty() &&
                tree.members_breadth_first.front() != target)
            {
                return tree.tree_id;
            }
        }
        throw std::logic_error("N31 fixture has no nonroot tree");
    }

    uint256_t proposal(
        const std::string &phase,
        std::uint32_t tree_id)
    {
        const auto key = std::make_pair(phase, tree_id);
        const auto existing = admitted_proposals.find(key);
        if (existing != admitted_proposals.end())
            return existing->second;

        const auto block_hash = digest(
            phase + "-" + std::to_string(tree_id));
        const ProposalKey proposal_key{
            ConfigurationId{
                ingress.current_epoch().epoch_number(),
                tree_id,
                ingress.current_epoch().epoch_digest()},
            block_hash};
        for (ReplicaID source = 0;
             source <= ingress.quorum_metadata().fault_threshold;
             ++source)
        {
            const ProposalLifecycleNotice notice{
                hotstuff::kProposalLifecycleNoticeSchemaVersion,
                source,
                ++lifecycle_sequences[source],
                ProposalLifecycleFact{
                    NormalProposalRuntimeInitialized{proposal_key}}};
            const auto result = ingress.ingest_lifecycle(
                AuthenticatedReporter{source},
                hotstuff::encode_proposal_lifecycle_notice(
                    notice, shape.ingress_limits.lifecycle_wire));
            CHECK(result.status ==
                  (source < ingress.quorum_metadata().fault_threshold
                       ? AdaptiveV2ManagerIngressStatus::
                             awaiting_corroboration
                       : AdaptiveV2ManagerIngressStatus::processed));
        }
        admitted_proposals.emplace(key, block_hash);
        return block_hash;
    }

    void record(
        ReplicaID target,
        std::uint32_t tree_id,
        ResponseOutcome outcome,
        const std::string &phase,
        std::uint64_t attempt_start_ns)
    {
        const auto &trees = ingress.current_epoch().trees();
        const auto tree = std::find_if(
            trees.begin(), trees.end(),
            [tree_id](const auto &candidate) {
                return candidate.tree_id == tree_id;
            });
        REQUIRE(tree != trees.end());
        const auto found = std::find(
            tree->members_breadth_first.begin(),
            tree->members_breadth_first.end(),
            target);
        REQUIRE(found != tree->members_breadth_first.end());
        const auto position = static_cast<std::size_t>(std::distance(
            tree->members_breadth_first.begin(), found));
        REQUIRE(position != 0);
        const auto reporter = tree->members_breadth_first[
            (position - 1U) / tree->fanout];
        const bool internal =
            ((position * tree->fanout) + 1U) <
            tree->members_breadth_first.size();

        ResponseObservation observation;
        observation.schema_version =
            hotstuff::kResponseObservationSchemaVersionV3;
        observation.reporter_id = reporter;
        observation.observed_replica_id = target;
        observation.configuration = {
            ingress.current_epoch().epoch_number(),
            tree_id,
            ingress.current_epoch().epoch_digest()};
        observation.block_hash = proposal(phase, tree_id);
        observation.expected_message_type = internal
            ? ExpectedMessageType::aggregate_relay
            : ExpectedMessageType::direct_vote;
        observation.outcome = outcome;
        observation.response_duration_us =
            outcome == ResponseOutcome::on_time ? 50 : 0;
        observation.deadline_duration_us = 100;
        observation.attempt_start_monotonic_ns = attempt_start_ns;
        observation.reporter_monotonic_ns = attempt_start_ns +
            (outcome == ResponseOutcome::on_time ? 50'000U : 100'000U);
        observation.reporter_sequence =
            ++evidence_sequences[reporter];
        if (outcome == ResponseOutcome::on_time)
            observation.signer_set = {target};
        observation.observation_id =
            hotstuff::compute_response_observation_id(observation);
        const auto result = ingress.ingest_evidence(
            AuthenticatedReporter{reporter},
            hotstuff::encode_evidence_batch(
                ResponseObservationBatch{
                    hotstuff::kEvidenceBatchSchemaVersion,
                    {observation}},
                shape.ingress_limits.evidence_wire));
        REQUIRE(result.status ==
                AdaptiveV2ManagerIngressStatus::processed);
        REQUIRE(result.accepted_observations == 1);
    }

    void record_responsive(
        const std::vector<ReplicaID> &targets,
        const std::string &phase,
        std::uint64_t minimum_attempt_start_ns = 0)
    {
        next_attempt_start_ns = std::max(
            next_attempt_start_ns, minimum_attempt_start_ns);
        for (const auto target : targets)
        {
            for (std::uint32_t attempt = 0; attempt < 2; ++attempt)
            {
                record(
                    target,
                    nonroot_tree(target),
                    ResponseOutcome::on_time,
                    phase,
                    next_attempt_start_ns++);
            }
        }
    }
};

hotstuff::AdaptiveV2FaultWindowArm full_n31_controller_arm(
    const hotstuff::EpochDefinition &epoch,
    std::uint64_t evidence_start_monotonic_ns)
{
    hotstuff::AdaptiveV2FaultWindowArm arm;
    arm.predecessor_epoch_number = epoch.epoch_number();
    arm.predecessor_epoch_digest = epoch.epoch_digest();
    arm.evidence_start_monotonic_ns = evidence_start_monotonic_ns;
    arm.prefault_tree_id = 20;
    for (std::uint32_t offset = 0; offset < 31; ++offset)
        arm.required_tree_ids.push_back((20U + offset) % 31U);
    arm.evidence_basis =
        hotstuff::AdaptiveV2FaultWindowEvidenceBasis::
            exact_timeout_attempt_id_v1;
    arm.snapshot_evidence_basis =
        hotstuff::AdaptiveV2FaultWindowSnapshotEvidenceBasis::
            exact_post_fault_attempt_start_v1;
    arm.cardinality_policy =
        hotstuff::AdaptiveV2FaultWindowCardinalityPolicy::
            all_guarded_up_to_fault_bound_v1;
    return arm;
}

struct N31FullCycleControllerOutcome
{
    AdaptiveV2ManagerControllerStatus controller_status{
        AdaptiveV2ManagerControllerStatus::unhealthy};
    AdaptiveV2SelectionStatus selection_status{
        AdaptiveV2SelectionStatus::invalid_state};
    std::vector<ReplicaID> selected;
    std::size_t eligible_candidates{0};
    std::size_t eligible_roots{0};
    bool healthy{false};
    bool has_successor{false};
    bool exact_wait_exempt{false};
};

N31FullCycleControllerOutcome run_n31_full_cycle_controller(
    const std::vector<ReplicaID> &guarded,
    const std::vector<ReplicaID> &unguarded_nonresponsive = {})
{
    constexpr std::uint64_t kEvidenceStartNs = 1'000'000;
    N31ControllerFixture fixture;
    fixture.controller.reset();
    fixture.config.selection.minimum_score_drop = 22;
    fixture.config.selection.minimum_timeouts_per_reporter = 2;
    fixture.controller =
        std::make_unique<AdaptiveV2ManagerController>(
            fixture.ingress, fixture.config);
    fixture.ready_quorum();
    fixture.record_responsive(
        fixture.members, "v12-full-cycle-baseline");
    REQUIRE(fixture.controller->evaluate() ==
            AdaptiveV2ManagerControllerStatus::baseline_frozen);
    REQUIRE(fixture.controller->arm_fault_window(
        full_n31_controller_arm(
            fixture.ingress.current_epoch(), kEvidenceStartNs)));

    const auto is_nonresponsive = [&](ReplicaID member) {
        return std::find(guarded.begin(), guarded.end(), member) !=
                   guarded.end() ||
               std::find(
                   unguarded_nonresponsive.begin(),
                   unguarded_nonresponsive.end(),
                   member) != unguarded_nonresponsive.end();
    };
    std::vector<ReplicaID> survivors;
    for (const auto member : fixture.members)
    {
        if (!is_nonresponsive(member))
            survivors.push_back(member);
    }
    fixture.record_responsive(
        survivors,
        "v12-full-cycle-survivors",
        kEvidenceStartNs + 10'000U);

    std::uint64_t attempt_start_ns = kEvidenceStartNs + 100'000U;
    for (const auto target : guarded)
    {
        for (std::uint32_t tree = 0; tree < 31; ++tree)
        {
            if (tree == target)
                continue;
            fixture.record(
                target,
                tree,
                ResponseOutcome::timeout,
                "v12-full-cycle-timeout",
                attempt_start_ns++);
            fixture.record(
                target,
                tree,
                ResponseOutcome::timeout,
                "v12-full-cycle-timeout",
                attempt_start_ns++);
        }
    }
    for (const auto target : unguarded_nonresponsive)
    {
        const auto tree = (target + 1U) % 31U;
        for (std::uint32_t attempt = 0; attempt < 30; ++attempt)
        {
            fixture.record(
                target,
                tree,
                ResponseOutcome::timeout,
                "v12-full-cycle-unselected-timeout",
                attempt_start_ns++);
        }
    }

    N31FullCycleControllerOutcome outcome;
    outcome.controller_status = fixture.controller->evaluate();
    outcome.healthy = fixture.controller->healthy();
    const auto *const audit = fixture.controller->selection_audit();
    if (audit != nullptr)
    {
        outcome.selection_status = audit->status;
        outcome.selected = audit->selected_replicas;
        outcome.eligible_candidates = audit->eligible_candidates.size();
        outcome.eligible_roots = audit->eligible_roots.size();
    }
    const auto *const successor = fixture.controller->successor_bundle();
    outcome.has_successor = successor != nullptr;
    if (successor != nullptr)
    {
        auto expected = guarded;
        std::sort(expected.begin(), expected.end());
        outcome.exact_wait_exempt = std::all_of(
            successor->definition().trees.begin(),
            successor->definition().trees.end(),
            [&expected](const auto &tree) {
                auto wait_exempt = tree.wait_exempt_leaves;
                std::sort(wait_exempt.begin(), wait_exempt.end());
                return wait_exempt == expected;
            });
    }
    return outcome;
}

enum class InheritedEpochShape : std::uint8_t
{
    exact = 1,
    undersized,
    oversized,
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
        else if (inherited_shape == InheritedEpochShape::oversized)
        {
            const auto replica_two = std::find(
                ordered.begin(), ordered.end(), ReplicaID{2});
            REQUIRE(replica_two != ordered.end());
            ordered.erase(replica_two);
            ordered.push_back(2);
            inherited = {0, 1, 2};
        }
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

TEST_CASE("successor factory keeps v2 canonical bytes and isolates v3 bundle mode",
          "[cert13][checkpoint1][epoch-factory][controller]")
{
    Fixture v2;
    v2.freeze_baseline();
    v2.persistent_timeouts(0);
    v2.persistent_timeouts(1);
    REQUIRE(v2.controller->evaluate() ==
            AdaptiveV2ManagerControllerStatus::successor_ready);
    REQUIRE(v2.controller->successor_bundle() != nullptr);
    REQUIRE(v2.controller->successor_bundle_v3() == nullptr);
    const auto v2_bytes = v2.controller->successor_bundle()->canonical_bytes();
    const auto v2_digest = DataStream(v2_bytes).get_hash().to_hex();
    INFO(v2_digest);
    CHECK(v2_digest ==
          "5ea1776f860f99c0f712d17587f0d92628e11a77f0268bcb339f1bf6c5222b55");

    Fixture v3;
    v3.controller.reset();
    v3.config.successor_protocol_mode = EpochProtocolMode::adaptive_v3;
    v3.controller = std::make_unique<AdaptiveV2ManagerController>(
        v3.ingress, v3.config);
    v3.freeze_baseline();
    v3.persistent_timeouts(0);
    v3.persistent_timeouts(1);
    REQUIRE(v3.controller->evaluate() ==
            AdaptiveV2ManagerControllerStatus::successor_ready);
    REQUIRE(v3.controller->successor_bundle() == nullptr);
    REQUIRE(v3.controller->successor_bundle_v3() != nullptr);
    const auto *bundle = v3.controller->successor_bundle_v3();
    CHECK(bundle->protocol_mode() == EpochProtocolMode::adaptive_v3);
    CHECK(bundle->command().protocol_mode == EpochProtocolMode::adaptive_v3);
    CHECK(bundle->definition().epoch_digest ==
          v2.controller->successor_bundle()->definition().epoch_digest);
    CHECK(bundle->canonical_bytes() != v2_bytes);
    CHECK_FALSE(hotstuff::decode_adaptive_v2_epoch_change_bundle(
        bundle->canonical_bytes(), bundle_limits()));
    CHECK(hotstuff::decode_adaptive_v3_epoch_change_bundle(
        bundle->canonical_bytes(), bundle_limits()));

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
    "N31 v12 full-cycle controller preserves the guarded cohort bound",
    "[adaptive-v2][manager-controller][all-guarded][v12][n31]"
    "[full-cycle]")
{
    const std::vector<ReplicaID> guarded_three{21, 22, 23};
    const std::vector<ReplicaID> guarded_ten{
        4, 5, 8, 9, 10, 14, 16, 21, 22, 23};

    SECTION("three guarded replicas produce an exact containment bundle")
    {
        auto outcome = run_n31_full_cycle_controller(guarded_three);
        CHECK(outcome.controller_status ==
              AdaptiveV2ManagerControllerStatus::successor_ready);
        CHECK(outcome.selection_status ==
              AdaptiveV2SelectionStatus::selected);
        std::sort(outcome.selected.begin(), outcome.selected.end());
        CHECK(outcome.selected == guarded_three);
        CHECK(outcome.eligible_candidates == 3);
        CHECK(outcome.eligible_roots == 21);
        CHECK(outcome.healthy);
        CHECK(outcome.has_successor);
        CHECK(outcome.exact_wait_exempt);
    }

    SECTION("ten guarded replicas retain Q eligible roots")
    {
        auto outcome = run_n31_full_cycle_controller(guarded_ten);
        CHECK(outcome.controller_status ==
              AdaptiveV2ManagerControllerStatus::successor_ready);
        CHECK(outcome.selection_status ==
              AdaptiveV2SelectionStatus::selected);
        std::sort(outcome.selected.begin(), outcome.selected.end());
        CHECK(outcome.selected == guarded_ten);
        CHECK(outcome.eligible_candidates == 10);
        CHECK(outcome.eligible_roots == 21);
        CHECK(outcome.healthy);
        CHECK(outcome.has_successor);
        CHECK(outcome.exact_wait_exempt);
    }

    SECTION("eleven guarded replicas are terminal above N minus Q")
    {
        auto over_bound = guarded_ten;
        over_bound.push_back(24);
        const auto outcome = run_n31_full_cycle_controller(over_bound);
        CHECK(outcome.controller_status ==
              AdaptiveV2ManagerControllerStatus::unhealthy);
        CHECK(outcome.selection_status ==
              AdaptiveV2SelectionStatus::
                  guarded_candidate_bound_exceeded);
        CHECK(outcome.eligible_candidates == 11);
        CHECK(outcome.selected.empty());
        CHECK(outcome.eligible_roots == 0);
        CHECK_FALSE(outcome.healthy);
        CHECK_FALSE(outcome.has_successor);
    }

    SECTION("an unselected nonresponsive replica blocks containment")
    {
        const auto outcome = run_n31_full_cycle_controller(
            guarded_three, {24});
        CHECK(outcome.controller_status ==
              AdaptiveV2ManagerControllerStatus::
                  awaiting_guarded_selection);
        CHECK(outcome.selection_status ==
              AdaptiveV2SelectionStatus::insufficient_eligible_roots);
        CHECK(outcome.eligible_candidates == 3);
        CHECK(outcome.selected.empty());
        CHECK(outcome.eligible_roots == 0);
        CHECK(outcome.healthy);
        CHECK_FALSE(outcome.has_successor);
    }
}

TEST_CASE(
    "N31 v9 controller carries the slot05 guarded cohort through E2",
    "[adaptive-v2][manager-controller][all-guarded][v9][n31][recurring]")
{
    N31ControllerFixture fixture;
    fixture.ready_quorum();
    fixture.record_responsive(
        fixture.members, "v9-cycle0-baseline");
    REQUIRE(fixture.controller->evaluate() ==
            AdaptiveV2ManagerControllerStatus::baseline_frozen);

    constexpr std::uint64_t kEvidenceStartNs = 1'000'000;
    hotstuff::AdaptiveV2FaultWindowArm arm;
    arm.predecessor_epoch_number =
        fixture.ingress.current_epoch().epoch_number();
    arm.predecessor_epoch_digest =
        fixture.ingress.current_epoch().epoch_digest();
    arm.evidence_start_monotonic_ns = kEvidenceStartNs;
    arm.prefault_tree_id = 20;
    for (std::uint32_t offset = 0; offset < 17; ++offset)
        arm.required_tree_ids.push_back((20U + offset) % 31U);
    arm.evidence_basis =
        hotstuff::AdaptiveV2FaultWindowEvidenceBasis::
            exact_timeout_attempt_id_v1;
    arm.snapshot_evidence_basis =
        hotstuff::AdaptiveV2FaultWindowSnapshotEvidenceBasis::
            exact_post_fault_attempt_start_v1;
    arm.cardinality_policy =
        hotstuff::AdaptiveV2FaultWindowCardinalityPolicy::
            all_guarded_up_to_fault_bound_v1;
    REQUIRE(fixture.controller->arm_fault_window(arm));

    const std::vector<ReplicaID> guarded{
        10, 11, 12, 15, 21, 22, 23};
    const std::set<ReplicaID> guarded_set(
        guarded.begin(), guarded.end());
    std::vector<ReplicaID> survivors;
    for (const auto member : fixture.members)
    {
        if (guarded_set.count(member) == 0)
            survivors.push_back(member);
    }
    fixture.record_responsive(
        survivors,
        "v9-cycle0-survivors",
        kEvidenceStartNs + 10'000U);

    std::uint64_t start = kEvidenceStartNs + 100'000U;
    const auto timeout_on =
        [&fixture, &start](
            ReplicaID target,
            const std::vector<std::uint32_t> &trees) {
            for (const auto tree : trees)
            {
                fixture.record(
                    target,
                    tree,
                    ResponseOutcome::timeout,
                    "v9-cycle0-timeout",
                    start++);
            }
        };
    timeout_on(21, {20, 24, 25, 26, 28, 29, 30, 0, 2, 3, 4, 5});
    timeout_on(22, {20, 24, 25, 26, 27, 29, 30, 0, 1, 3, 5});
    timeout_on(23, {20, 24, 25, 26, 27, 28, 30, 0, 1, 2, 5});
    for (const auto target : std::vector<ReplicaID>{10, 11, 12, 15})
    {
        std::vector<std::uint32_t> trees;
        for (std::uint32_t offset = 0; offset < 17; ++offset)
        {
            const auto tree = (20U + offset) % 31U;
            if (tree != target)
                trees.push_back(tree);
        }
        timeout_on(target, trees);
    }

    REQUIRE(fixture.controller->evaluate() ==
            AdaptiveV2ManagerControllerStatus::successor_ready);
    REQUIRE(fixture.controller->selection_audit() != nullptr);
    auto selected_cycle_zero =
        fixture.controller->selection_audit()->selected_replicas;
    std::sort(selected_cycle_zero.begin(), selected_cycle_zero.end());
    CHECK(selected_cycle_zero == guarded);
    REQUIRE(fixture.controller->successor_bundle() != nullptr);
    const auto epoch_one =
        fixture.controller->successor_bundle()->definition();
    for (const auto &tree : epoch_one.trees)
        CHECK(tree.wait_exempt_leaves == guarded);

    fixture.controller.reset();
    REQUIRE(fixture.ingress.rotate_to_successor(epoch_one, 0) ==
            AdaptiveV2ManagerIngressStatus::processed);
    fixture.config.selection.fault_window_arm_required = false;
    fixture.config.transition_policy.intent =
        TreePolicyKind::performance_optimization;
    fixture.config.transition_policy.containment_baseline_roots.clear();
    fixture.admitted_proposals.clear();
    fixture.next_attempt_start_ns = start + 200'000U;
    fixture.controller =
        std::make_unique<AdaptiveV2ManagerController>(
            fixture.ingress, fixture.config);
    REQUIRE(fixture.controller->healthy());

    fixture.ready_quorum(guarded_set);
    fixture.record_responsive(
        survivors, "v9-cycle1-baseline");
    REQUIRE(fixture.controller->evaluate() ==
            AdaptiveV2ManagerControllerStatus::baseline_frozen);
    fixture.record_responsive(
        survivors, "v9-cycle1-ranking");
    REQUIRE(fixture.controller->evaluate() ==
            AdaptiveV2ManagerControllerStatus::successor_ready);
    REQUIRE(fixture.controller->selection_audit() != nullptr);
    CHECK(fixture.controller->selection_audit()->constraint_basis ==
          AdaptiveV2SelectionConstraintBasis::
              inherited_consensus_wait_exempt);
    CHECK(fixture.controller->selection_audit()->selected_replicas ==
          guarded);
    REQUIRE(fixture.controller->successor_bundle() != nullptr);
    CHECK(fixture.controller->successor_bundle()
              ->definition()
              .epoch_number == 2);
    for (const auto &tree :
         fixture.controller->successor_bundle()->definition().trees)
    {
        CHECK(tree.wait_exempt_leaves == guarded);
    }
}

TEST_CASE(
    "v9 recurring selection preserves an inherited cohort above its minimum",
    "[adaptive-v2][manager-controller][inheritance][all-guarded][v9][n7]")
{
    Fixture fixture(5, 4096, 1);
    fixture.controller.reset();
    fixture.config.selection.cardinality_policy =
        hotstuff::AdaptiveV2FaultWindowCardinalityPolicy::
            all_guarded_up_to_fault_bound_v1;
    rotate_to_exact_epoch_one(
        fixture, TreePolicyKind::performance_optimization);
    ready_live_survivors(fixture);
    record_live_survivor_baseline(fixture);

    REQUIRE(fixture.controller->evaluate() ==
            AdaptiveV2ManagerControllerStatus::baseline_frozen);
    record_ranked_live_survivor_suffix(fixture);
    REQUIRE(fixture.controller->evaluate() ==
            AdaptiveV2ManagerControllerStatus::successor_ready);
    REQUIRE(fixture.controller->selection_audit() != nullptr);
    const auto &selection = *fixture.controller->selection_audit();
    CHECK(selection.constraint_basis ==
          AdaptiveV2SelectionConstraintBasis::
              inherited_consensus_wait_exempt);
    CHECK(selection.metadata.required_nonresponsive == 1);
    CHECK(selection.metadata.cardinality_policy ==
          hotstuff::AdaptiveV2FaultWindowCardinalityPolicy::
              all_guarded_up_to_fault_bound_v1);
    CHECK(selection.selected_replicas ==
          std::vector<ReplicaID>{0, 1});
    REQUIRE(fixture.controller->successor_bundle() != nullptr);
    for (const auto &tree :
         fixture.controller->successor_bundle()->definition().trees)
    {
        CHECK(tree.wait_exempt_leaves ==
              std::vector<ReplicaID>{0, 1});
    }
}

TEST_CASE(
    "N7 recurring containment keeps freshly responsive inherited actors "
    "as leaves",
    "[adaptive-v2][manager-controller][inheritance][recovered]"
    "[fault-containment][n7]")
{
    Fixture fixture;
    rotate_to_exact_epoch_one(
        fixture, TreePolicyKind::fault_containment);
    fixture.ready_all();
    fixture.complete_responsive_baseline();
    REQUIRE(fixture.controller->evaluate() ==
            AdaptiveV2ManagerControllerStatus::baseline_frozen);

    fixture.complete_responsive_baseline();
    REQUIRE(fixture.controller->evaluate() ==
            AdaptiveV2ManagerControllerStatus::successor_ready);
    REQUIRE(fixture.controller->selection_audit() != nullptr);
    const auto &selection = *fixture.controller->selection_audit();
    REQUIRE(selection.snapshot != nullptr);
    CHECK(selection.constraint_basis ==
          AdaptiveV2SelectionConstraintBasis::
              inherited_consensus_wait_exempt);
    CHECK(selection.selected_replicas ==
          std::vector<ReplicaID>{0, 1});
    CHECK(selection.metadata.replica_count == 7);
    CHECK(selection.metadata.fault_threshold == 2);
    CHECK(selection.metadata.quorum == 5);
    for (const auto actor : selection.selected_replicas)
    {
        const auto *entry = ranking_entry(*selection.snapshot, actor);
        REQUIRE(entry != nullptr);
        CHECK(entry->classification == ResponsivenessClass::responsive);
        CHECK(entry->eligible);
    }

    REQUIRE(fixture.controller->successor_bundle() != nullptr);
    CHECK(successor_roots(*fixture.controller) ==
          std::vector<ReplicaID>{2, 3, 4, 5, 6});
    for (const auto &tree :
         fixture.controller->successor_bundle()->definition().trees)
    {
        CHECK(tree.wait_exempt_leaves ==
              std::vector<ReplicaID>{0, 1});
        const auto leaf_start = first_leaf_index(
            tree.members_breadth_first.size(), tree.fanout);
        for (const auto actor : selection.selected_replicas)
        {
            const auto position = std::find(
                tree.members_breadth_first.begin(),
                tree.members_breadth_first.end(),
                actor);
            REQUIRE(position != tree.members_breadth_first.end());
            CHECK(static_cast<std::size_t>(std::distance(
                      tree.members_breadth_first.begin(), position)) >=
                  leaf_start);
        }
    }
    CHECK(fixture.controller->healthy());
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
    "v9 recurring selection rejects inherited cohorts outside its safe range",
    "[adaptive-v2][manager-controller][inheritance][all-guarded]"
    "[fail-closed][v9][n7]")
{
    SECTION("below minimum")
    {
        Fixture fixture;
        fixture.controller.reset();
        fixture.config.selection.cardinality_policy =
            hotstuff::AdaptiveV2FaultWindowCardinalityPolicy::
                all_guarded_up_to_fault_bound_v1;
        rotate_to_exact_epoch_one(
            fixture,
            TreePolicyKind::performance_optimization,
            InheritedEpochShape::undersized);
        CHECK_FALSE(fixture.controller->healthy());
        CHECK(fixture.controller->evaluate() ==
              AdaptiveV2ManagerControllerStatus::unhealthy);
    }

    SECTION("above N minus Q")
    {
        Fixture fixture(5, 4096, 1);
        fixture.controller.reset();
        const auto successor = exact_epoch_one(
            fixture.ingress, InheritedEpochShape::oversized);
        CHECK(fixture.ingress.rotate_to_successor(successor, 0) ==
              AdaptiveV2ManagerIngressStatus::rejected_configuration);
        CHECK(fixture.ingress.current_epoch().epoch_number() == 0);
    }

    SECTION("tree mutation")
    {
        Fixture fixture(5, 4096, 1);
        fixture.controller.reset();
        fixture.config.selection.cardinality_policy =
            hotstuff::AdaptiveV2FaultWindowCardinalityPolicy::
                all_guarded_up_to_fault_bound_v1;
        rotate_to_exact_epoch_one(
            fixture,
            TreePolicyKind::performance_optimization,
            InheritedEpochShape::inconsistent);
        CHECK_FALSE(fixture.controller->healthy());
        CHECK(fixture.controller->evaluate() ==
              AdaptiveV2ManagerControllerStatus::unhealthy);
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
    CHECK(fixture.controller->failure_detail() == nullptr);
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
    CHECK(selection->metadata.timeout_audit_basis ==
          AdaptiveV2TimeoutAuditBasis::unfiltered_post_baseline);
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
    "controller waits for an unguarded nonresponsive member to recover",
    "[adaptive-v2][manager-controller][selection][eligibility][n7]")
{
    Fixture fixture(5, 4096, 1);
    fixture.freeze_baseline();

    fixture.persistent_timeouts(0, 3);
    fixture.persistent_timeouts(6, 1);
    REQUIRE(fixture.controller->evaluate() ==
            AdaptiveV2ManagerControllerStatus::awaiting_guarded_selection);
    REQUIRE(fixture.controller->selection_audit() != nullptr);
    CHECK(fixture.controller->selection_audit()->status ==
          AdaptiveV2SelectionStatus::insufficient_eligible_roots);
    CHECK(fixture.controller->selection_audit()->selected_replicas.empty());
    CHECK(fixture.controller->selection_audit()->eligible_roots.empty());
    CHECK(fixture.controller->successor_bundle() == nullptr);
    CHECK(fixture.controller->failure_detail() == nullptr);
    CHECK(fixture.controller->healthy());

    for (std::size_t attempt = 0; attempt < 6; ++attempt)
    {
        fixture.record(
            6, 0, ResponseOutcome::on_time,
            "recover-unselected-member");
    }

    REQUIRE(fixture.controller->evaluate() ==
            AdaptiveV2ManagerControllerStatus::successor_ready);
    REQUIRE(fixture.controller->selection_audit() != nullptr);
    CHECK(fixture.controller->selection_audit()->status ==
          AdaptiveV2SelectionStatus::selected);
    CHECK(fixture.controller->selection_audit()->selected_replicas ==
          std::vector<ReplicaID>{0});
    CHECK(fixture.controller->selection_audit()->eligible_roots.size() == 5);
    REQUIRE(fixture.controller->successor_bundle() != nullptr);
    CHECK(fixture.controller->healthy());
}

TEST_CASE(
    "controller composes raw drawdown with proposal-filtered timeout audit",
    "[adaptive-v2][manager-controller][selection][epoch-factory]"
    "[fault-containment][audit-domain]")
{
    Fixture fixture(5, 4096, 1);
    fixture.controller.reset();
    fixture.config.selection.minimum_score_drop = 3;
    fixture.config.selection.minimum_timeouts_per_reporter = 1;
    fixture.config.selection
        .fault_containment_evidence_start_monotonic_ns = 1;
    fixture.config.selection
        .fault_containment_required_tree_coverage = 7;
    fixture.controller =
        std::make_unique<AdaptiveV2ManagerController>(
            fixture.ingress, fixture.config);
    fixture.freeze_baseline();

    for (std::uint32_t tree_id = 0; tree_id < 7; ++tree_id)
        fixture.cover_tree(tree_id);

    // This timeout remains in the raw post-baseline drawdown, but its
    // proposal has no independent post-fault direct-vote anchor.
    fixture.record(
        6, 0, ResponseOutcome::timeout, "excluded-proposal");

    for (std::size_t reporter_index = 0;
         reporter_index < 3;
         ++reporter_index)
    {
        const auto timeout = fixture.record(
            6,
            reporter_index,
            ResponseOutcome::timeout,
            "eligible-proposal");
        fixture.anchor_timeout_proposal(timeout);
    }

    REQUIRE(fixture.controller->evaluate() ==
            AdaptiveV2ManagerControllerStatus::successor_ready);
    const auto *selection = fixture.controller->selection_audit();
    REQUIRE(selection != nullptr);
    REQUIRE(selection->status == AdaptiveV2SelectionStatus::selected);
    CHECK(selection->metadata.timeout_audit_basis ==
          AdaptiveV2TimeoutAuditBasis::post_fault_proposal_filtered);
    REQUIRE(selection->eligible_candidates.size() == 1);
    CHECK(selection->eligible_candidates.front().replica_id == 6);
    CHECK(selection->eligible_candidates.front().guard_drawdown == -4);
    CHECK(selection->eligible_candidates.front()
              .total_uncompensated_timeouts == 3);
    CHECK(fixture.controller->successor_bundle() != nullptr);
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
    REQUIRE(fixture.controller->failure_detail() != nullptr);
    CHECK(fixture.controller->failure_detail()->stage ==
          hotstuff::AdaptiveV2ManagerControllerFailureStage::successor_factory);
    CHECK(fixture.controller->failure_detail()->selection_status ==
          AdaptiveV2SelectionStatus::selected);
    CHECK(fixture.controller->failure_detail()->epoch_factory_status ==
          hotstuff::AdaptiveV2EpochFactoryStatus::invalid_activation_delay);
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
    "initial containment preserves shape without latency evidence when "
    "shape selection is disabled",
    "[shape25][adaptive-v2][manager-controller][shape-v1][n7]"
    "[containment]")
{
    Fixture fixture;
    fixture.freeze_baseline();
    for (std::size_t attempt_window = 0;
         attempt_window < 6;
         ++attempt_window)
    {
        fixture.persistent_timeouts(0);
        fixture.persistent_timeouts(1);
    }

    REQUIRE(fixture.controller->evaluate() ==
            AdaptiveV2ManagerControllerStatus::successor_ready);
    REQUIRE(fixture.controller->selection_audit() != nullptr);
    REQUIRE(fixture.controller->selection_audit()->snapshot != nullptr);
    for (const auto replica_id : std::vector<ReplicaID>{0, 1})
    {
        const auto *entry = ranking_entry(
            *fixture.controller->selection_audit()->snapshot,
            replica_id);
        REQUIRE(entry != nullptr);
        CHECK(entry->classification ==
              ResponsivenessClass::nonresponsive);
        CHECK_FALSE(entry->latency_percentile_us.has_value());
    }
    CHECK(fixture.controller->shape_decision() == nullptr);

    const auto *bundle = fixture.controller->successor_bundle();
    REQUIRE(bundle != nullptr);
    for (const auto &tree : bundle->definition().trees)
    {
        CHECK(tree.fanout == fixture.config.placement.shape.fanout);
        CHECK(tree.pipeline_stretch ==
              fixture.config.placement.shape.pipeline_stretch);
        CHECK(tree.wait_exempt_leaves ==
              std::vector<ReplicaID>{0, 1});
    }
}

TEST_CASE(
    "initial containment remains fail closed without latency evidence when "
    "shape selection is enabled",
    "[shape25][adaptive-v2][manager-controller][shape-v1][n7]"
    "[containment][fail-closed]")
{
    Fixture fixture;
    fixture.controller.reset();
    fixture.config.transition_policy.apply_shape_selection = true;
    fixture.config.shape_adaptation_enabled = true;
    fixture.controller =
        std::make_unique<AdaptiveV2ManagerController>(
            fixture.ingress, fixture.config);
    fixture.freeze_baseline();
    for (std::size_t attempt_window = 0;
         attempt_window < 6;
         ++attempt_window)
    {
        fixture.persistent_timeouts(0);
        fixture.persistent_timeouts(1);
    }

    CHECK(fixture.controller->evaluate() ==
          AdaptiveV2ManagerControllerStatus::unhealthy);
    REQUIRE(fixture.controller->selection_audit() != nullptr);
    REQUIRE(fixture.controller->selection_audit()->snapshot != nullptr);
    for (const auto replica_id : std::vector<ReplicaID>{0, 1})
    {
        const auto *entry = ranking_entry(
            *fixture.controller->selection_audit()->snapshot,
            replica_id);
        REQUIRE(entry != nullptr);
        CHECK(entry->classification ==
              ResponsivenessClass::nonresponsive);
        CHECK_FALSE(entry->latency_percentile_us.has_value());
    }
    CHECK(fixture.controller->shape_decision() == nullptr);
    CHECK(fixture.controller->successor_bundle() == nullptr);
    CHECK_FALSE(fixture.controller->healthy());
}

TEST_CASE(
    "initial containment rejects a configured shape that does not match "
    "the predecessor",
    "[shape25][adaptive-v2][manager-controller][shape-preservation][n7]"
    "[containment][fail-closed]")
{
    Fixture fixture;
    fixture.controller.reset();
    SECTION("fanout")
    {
        fixture.config.placement.shape.fanout = 3;
    }
    SECTION("pipeline stretch")
    {
        fixture.config.placement.shape.pipeline_stretch = 3;
    }
    fixture.controller =
        std::make_unique<AdaptiveV2ManagerController>(
            fixture.ingress, fixture.config);
    fixture.freeze_baseline();
    fixture.persistent_timeouts(5);
    fixture.persistent_timeouts(6);

    CHECK(fixture.controller->evaluate() ==
          AdaptiveV2ManagerControllerStatus::unhealthy);
    CHECK(fixture.controller->shape_decision() == nullptr);
    CHECK(fixture.controller->successor_bundle() == nullptr);
    CHECK_FALSE(fixture.controller->healthy());
}

TEST_CASE(
    "initial containment maps N trees to Q while preserving configured shape",
    "[shape25][adaptive-v2][manager-controller][shape-preservation][n7]")
{
    Fixture fixture;
    fixture.freeze_baseline();
    fixture.persistent_timeouts(5);
    fixture.persistent_timeouts(6);

    REQUIRE(fixture.controller->evaluate() ==
            AdaptiveV2ManagerControllerStatus::successor_ready);
    CHECK(fixture.controller->shape_decision() == nullptr);
    CHECK(fixture.ingress.current_epoch().trees().size() == 7);

    const auto *bundle = fixture.controller->successor_bundle();
    REQUIRE(bundle != nullptr);
    CHECK(bundle->definition().trees.size() ==
          fixture.ingress.quorum_metadata().quorum);
    for (const auto &tree : bundle->definition().trees)
    {
        CHECK(tree.fanout ==
              fixture.config.placement.shape.fanout);
        CHECK(tree.pipeline_stretch ==
              fixture.config.placement.shape.pipeline_stretch);
        CHECK(tree.wait_exempt_leaves ==
              std::vector<ReplicaID>{5, 6});
    }

    fixture.record(2, 0, ResponseOutcome::on_time, "after-shape-cutoff");
    CHECK(fixture.controller->evaluate() ==
          AdaptiveV2ManagerControllerStatus::already_ready);
    CHECK(fixture.controller->shape_decision() == nullptr);
}

TEST_CASE(
    "shape factor enables shape-v1 application for initial containment",
    "[shape25][adaptive-v2][manager-controller][shape-factor][n7]")
{
    Fixture fixture;
    fixture.controller.reset();
    fixture.config.transition_policy.apply_shape_selection = true;
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

TEST_CASE(
    "fault-window arm cannot be preseeded before baseline",
    "[adaptive-v2][manager-controller][fault-window-arm]")
{
    Fixture fixture;
    hotstuff::AdaptiveV2FaultWindowArm arm;
    arm.predecessor_epoch_number = 0;
    arm.predecessor_epoch_digest = fixture.ingress.current_epoch().epoch_digest();
    arm.evidence_start_monotonic_ns = 1;
    arm.prefault_tree_id = 0;
    arm.required_tree_ids = {0};
    fixture.config.selection.fault_window_arm = arm;
    CHECK_THROWS_AS(
        AdaptiveV2ManagerController(fixture.ingress, fixture.config),
        std::invalid_argument);
}

TEST_CASE(
    "v4 arm releases already-complete guarded evidence exactly once",
    "[adaptive-v2][manager-controller][fault-window-arm][v4][n7]")
{
    Fixture fixture(5, 4096, 1);
    fixture.controller.reset();
    fixture.config.selection.minimum_score_drop = 1;
    fixture.config.selection.minimum_timeouts_per_reporter = 1;
    fixture.config.selection.fault_window_arm_required = true;
    fixture.controller = std::make_unique<AdaptiveV2ManagerController>(
        fixture.ingress, fixture.config);
    fixture.freeze_baseline();

    // Crashed prefix positions have no direct-vote observation. The arm
    // admits the responsive proposal anchors below; selection owns readiness.
    for (const auto reporter : std::vector<std::size_t>{0, 1, 2})
    {
        const auto timeout = fixture.record(
            6, reporter, ResponseOutcome::timeout, "v4-complete");
        fixture.anchor_timeout_proposal(timeout);
    }

    // A complete ledger cannot cause a legacy unfiltered selection before the
    // one-shot prospective arm is accepted.
    CHECK(fixture.controller->evaluate() ==
          AdaptiveV2ManagerControllerStatus::awaiting_guarded_selection);
    CHECK(fixture.controller->successor_bundle() == nullptr);

    hotstuff::AdaptiveV2FaultWindowArm arm;
    arm.predecessor_epoch_number =
        fixture.ingress.current_epoch().epoch_number();
    arm.predecessor_epoch_digest =
        fixture.ingress.current_epoch().epoch_digest();
    arm.evidence_start_monotonic_ns = 1;
    arm.prefault_tree_id = 0;
    arm.required_tree_ids = {0, 1, 2, 3, 4, 5, 6};
    REQUIRE(fixture.controller->arm_fault_window(arm));
    CHECK_FALSE(fixture.controller->arm_fault_window(arm));

    REQUIRE(fixture.controller->evaluate() ==
            AdaptiveV2ManagerControllerStatus::successor_ready);
    REQUIRE(fixture.controller->successor_bundle() != nullptr);
    REQUIRE(fixture.controller->selection_audit() != nullptr);
    CHECK(fixture.controller->selection_audit()->metadata.timeout_audit_basis ==
          AdaptiveV2TimeoutAuditBasis::post_fault_proposal_filtered);
}
