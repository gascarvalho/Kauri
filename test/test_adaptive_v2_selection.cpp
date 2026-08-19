#include <algorithm>
#include <cstdint>
#include <limits>
#include <map>
#include <memory>
#include <stdexcept>
#include <string>
#include <type_traits>
#include <utility>
#include <vector>

#include "catch.hpp"
#include "hotstuff/adaptation.h"
#include "hotstuff/adaptive_v2_selection.h"
#include "hotstuff/configuration.h"
#include "hotstuff/epoch_store.h"
#include "hotstuff/evidence.h"

namespace
{

using hotstuff::AdaptationEpochId;
using hotstuff::AdaptiveV2ByzantineSelection;
using hotstuff::AdaptiveV2CandidateAudit;
using hotstuff::AdaptiveV2ReplicaScore;
using hotstuff::AdaptiveV2SelectionConfig;
using hotstuff::AdaptiveV2SelectionConstraintBasis;
using hotstuff::AdaptiveV2SelectionResult;
using hotstuff::AdaptiveV2SelectionStatus;
using hotstuff::AdaptiveV2TimeoutAuditBasis;
using hotstuff::AdaptiveV2FaultContainmentCoverageStatus;
using hotstuff::AdaptiveV2FaultWindowArm;
using hotstuff::AcceptedEvidenceRecord;
using hotstuff::AuthenticatedReporter;
using hotstuff::ConfigurationId;
using hotstuff::EpochDefinitionInput;
using hotstuff::EpochStore;
using hotstuff::EpochTreeDefinition;
using hotstuff::EpochValidationContext;
using hotstuff::EvidenceLedger;
using hotstuff::EvidenceReputationAuditUpdate;
using hotstuff::EvidenceReputationLimits;
using hotstuff::EvidenceStoreLimits;
using hotstuff::ExpectedMessageType;
using hotstuff::ProposalEvidenceStatus;
using hotstuff::ProposalEvidenceWindow;
using hotstuff::ProposalKey;
using hotstuff::ReplicaID;
using hotstuff::ResponseObservation;
using hotstuff::ResponseOutcome;
using hotstuff::ResponsivenessClass;
using hotstuff::uint256_t;

constexpr std::size_t kReplicaCount = 7;

std::vector<ReplicaID> fixed_membership()
{
    return {0, 1, 2, 3, 4, 5, 6};
}

uint256_t digest(const std::string &label)
{
    hotstuff::DataStream stream(label);
    return stream.get_hash();
}

std::uint32_t tree_id(ReplicaID reporter, ReplicaID target)
{
    return static_cast<std::uint32_t>(
        1U + static_cast<std::uint32_t>(reporter) *
                 kReplicaCount +
        static_cast<std::uint32_t>(target));
}

std::vector<EpochTreeDefinition> all_reporter_target_trees()
{
    std::vector<EpochTreeDefinition> trees;
    trees.reserve(kReplicaCount * (kReplicaCount - 1) + 2);
    trees.push_back(EpochTreeDefinition{
        0, 2, 2, {0, 1, 2, 3, 4, 5, 6}, {}});
    trees.push_back(EpochTreeDefinition{
        1, 2, 2, {1, 2, 3, 4, 5, 6, 0}, {}});
    for (const auto reporter : fixed_membership())
    {
        for (const auto target : fixed_membership())
        {
            if (reporter == target)
                continue;
            std::vector<ReplicaID> ordered{reporter, target};
            for (const auto replica : fixed_membership())
            {
                if (replica != reporter && replica != target)
                    ordered.push_back(replica);
            }
            trees.push_back(EpochTreeDefinition{
                tree_id(reporter, target),
                2,
                2,
                std::move(ordered),
                {}});
        }
    }
    return trees;
}

std::vector<EpochTreeDefinition> production_n7_trees()
{
    return {
        {0, 2, 2, {0, 1, 2, 3, 4, 5, 6}, {}},
        {1, 2, 2, {1, 2, 3, 4, 5, 6, 0}, {}},
        {2, 2, 2, {2, 3, 4, 5, 6, 0, 1}, {}},
        {3, 2, 2, {3, 4, 5, 6, 0, 1, 2}, {}},
        {4, 2, 2, {4, 5, 6, 0, 1, 2, 3}, {}},
        {5, 2, 2, {5, 6, 0, 1, 2, 3, 4}, {}},
        {6, 2, 2, {6, 0, 1, 2, 3, 4, 5}, {}}};
}

EpochDefinitionInput epoch_input(
    std::uint32_t epoch_number,
    const uint256_t &previous_epoch_digest,
    std::uint64_t activation_height)
{
    EpochDefinitionInput input;
    input.schema_version = hotstuff::kEpochDefinitionSchemaVersion;
    input.epoch_number = epoch_number;
    input.previous_epoch_digest = previous_epoch_digest;
    input.membership_digest =
        hotstuff::canonical_membership_digest(fixed_membership());
    input.trees = all_reporter_target_trees();
    input.activation_height = activation_height;
    input.generation_seed = 0xA2'0000 + epoch_number;
    input.policy_version = "adaptive-v2-selection-test-v1";
    input.evidence_snapshot_id =
        "adaptive-v2-selection-fixture-" +
        std::to_string(epoch_number);
    input.evidence_cutoff = 0;
    return input;
}

EpochDefinitionInput production_epoch_input(
    std::uint32_t epoch_number,
    const uint256_t &previous_epoch_digest,
    std::uint64_t activation_height)
{
    auto input = epoch_input(
        epoch_number, previous_epoch_digest, activation_height);
    input.trees = production_n7_trees();
    return input;
}

EpochValidationContext validation_context(
    std::uint64_t current_height)
{
    EpochValidationContext context;
    context.current_height = current_height;
    context.minimum_activation_grace = 5;
    return context;
}

class MutableEvidenceWindow final : public ProposalEvidenceWindow
{
public:
    void admit(const ProposalKey &proposal)
    {
        statuses_[proposal] = ProposalEvidenceStatus::admissible;
    }

    ProposalEvidenceStatus classify(
        const ProposalKey &proposal) const noexcept override
    {
        const auto found = statuses_.find(proposal);
        return found == statuses_.end()
                   ? ProposalEvidenceStatus::unknown
                   : found->second;
    }

private:
    std::map<ProposalKey, ProposalEvidenceStatus> statuses_;
};

AdaptiveV2SelectionConfig selection_config(
    std::uint32_t minimum_score_drop = 6,
    std::uint32_t minimum_timeouts_per_reporter = 2,
    std::size_t maximum_attempts = 128)
{
    AdaptiveV2SelectionConfig config;
    config.required_nonresponsive = 2;
    config.minimum_score_drop = minimum_score_drop;
    config.minimum_timeouts_per_reporter =
        minimum_timeouts_per_reporter;
    config.maximum_post_baseline_timeout_attempts =
        maximum_attempts;
    config.snapshot_seed = 0xA2'5EED;
    config.responsiveness_policy.policy_version =
        "adaptive-v2-selection-test-v1";
    config.responsiveness_policy.attempt_window = 64;
    config.responsiveness_policy.minimum_attempts = 1;
    config.responsiveness_policy.minimum_response_rate_ppm = 600'000;
    config.responsiveness_policy.maximum_timeout_rate_ppm = 400'000;
    config.responsiveness_policy.trailing_timeout_streak = 2;
    config.responsiveness_policy.latency_percentile_basis_points = 5'000;
    return config;
}

struct Fixture
{
    std::vector<ReplicaID> members{fixed_membership()};
    EpochStore epochs{members};
    MutableEvidenceWindow window;
    AdaptationEpochId epoch;
    std::unique_ptr<EvidenceLedger> ledger;
    std::map<ReplicaID, std::uint64_t> reporter_sequences;
    std::uint64_t monotonic_clock{0};
    std::uint64_t attempt_number{0};

    explicit Fixture(
        std::size_t accepted_capacity = 256,
        bool production_n7_topology = false)
    {
        const auto &definition = epochs.stage(
            production_n7_topology
                ? production_epoch_input(0, uint256_t{}, 15)
                : epoch_input(0, uint256_t{}, 15),
            validation_context(10));
        epoch = {definition.epoch_number(), definition.epoch_digest()};
        ledger = std::make_unique<EvidenceLedger>(
            epochs,
            window,
            EvidenceStoreLimits{accepted_capacity, 64});
    }

    ConfigurationId configuration(
        ReplicaID reporter,
        ReplicaID target,
        const AdaptationEpochId &for_epoch) const
    {
        return {
            for_epoch.epoch_number,
            tree_id(reporter, target),
            for_epoch.epoch_digest};
    }

    ResponseObservation observation(
        ReplicaID reporter,
        ReplicaID target,
        ResponseOutcome outcome,
        const AdaptationEpochId &for_epoch)
    {
        ResponseObservation value;
        value.reporter_id = reporter;
        value.observed_replica_id = target;
        value.configuration =
            configuration(reporter, target, for_epoch);
        value.block_hash = digest(
            "adaptive-v2-attempt-" +
            std::to_string(++attempt_number));
        value.expected_message_type =
            ExpectedMessageType::aggregate_relay;
        value.outcome = outcome;
        value.response_duration_us =
            outcome == ResponseOutcome::timeout
                ? 0
                : (outcome == ResponseOutcome::late ? 150 : 50);
        value.deadline_duration_us = 100;
        value.reporter_monotonic_ns = ++monotonic_clock * 1'000;
        value.reporter_sequence = ++reporter_sequences[reporter];
        if (outcome != ResponseOutcome::timeout)
            value.signer_set = {target};
        value.observation_id =
            hotstuff::compute_response_observation_id(
                value.attempt_identity());
        window.admit(value.proposal_key());
        return value;
    }

    ResponseObservation observation(
        ReplicaID reporter,
        ReplicaID target,
        ResponseOutcome outcome)
    {
        return observation(reporter, target, outcome, epoch);
    }

    void ingest(const ResponseObservation &observation)
    {
        const auto accepted_before = ledger->accepted().size();
        ledger->ingest(
            AuthenticatedReporter{observation.reporter_id},
            observation);
        REQUIRE(ledger->accepted().size() == accepted_before + 1);
    }

    ResponseObservation timeout(ReplicaID reporter, ReplicaID target)
    {
        auto value = observation(
            reporter, target, ResponseOutcome::timeout);
        ingest(value);
        return value;
    }

    ResponseObservation timeout_in_tree(
        ReplicaID target, std::uint32_t tree_id)
    {
        const auto &definition = tree(tree_id);
        const auto target_position = std::find(
            definition.members_breadth_first.begin(),
            definition.members_breadth_first.end(), target);
        REQUIRE(target_position != definition.members_breadth_first.end());
        const auto position = static_cast<std::size_t>(
            target_position - definition.members_breadth_first.begin());
        REQUIRE(position != 0);
        const auto reporter = definition.members_breadth_first[
            (position - 1) / definition.fanout];
        ResponseObservation value;
        value.reporter_id = reporter;
        value.observed_replica_id = target;
        value.configuration = {
            epoch.epoch_number, tree_id, epoch.epoch_digest};
        value.block_hash = digest(
            "adaptive-v2-tree-timeout-" +
            std::to_string(++attempt_number));
        value.expected_message_type = ExpectedMessageType::direct_vote;
        value.outcome = ResponseOutcome::timeout;
        value.deadline_duration_us = 100;
        value.reporter_monotonic_ns = ++monotonic_clock * 1'000;
        value.reporter_sequence = ++reporter_sequences[reporter];
        value.observation_id = hotstuff::compute_response_observation_id(
            value.attempt_identity());
        window.admit(value.proposal_key());
        ingest(value);
        return value;
    }

    ResponseObservation timeout_v3_in_tree(
        ReplicaID target, std::uint32_t tree_id,
        std::uint64_t attempt_start_monotonic_ns)
    {
        const auto &definition = tree(tree_id);
        const auto position = static_cast<std::size_t>(
            std::find(definition.members_breadth_first.begin(),
                      definition.members_breadth_first.end(), target) -
            definition.members_breadth_first.begin());
        REQUIRE(position != 0);
        REQUIRE(position < definition.members_breadth_first.size());
        const auto reporter = definition.members_breadth_first[
            (position - 1) / definition.fanout];
        ResponseObservation value;
        value.schema_version = hotstuff::kResponseObservationSchemaVersionV3;
        value.reporter_id = reporter; value.observed_replica_id = target;
        value.configuration = {epoch.epoch_number, tree_id, epoch.epoch_digest};
        value.block_hash = digest("adaptive-v2-v3-timeout-" + std::to_string(++attempt_number));
        value.expected_message_type = ExpectedMessageType::direct_vote;
        value.outcome = ResponseOutcome::timeout; value.deadline_duration_us = 100;
        value.attempt_start_monotonic_ns = attempt_start_monotonic_ns;
        value.reporter_monotonic_ns = attempt_start_monotonic_ns + 100'000;
        value.reporter_sequence = ++reporter_sequences[reporter];
        value.observation_id = hotstuff::compute_response_observation_id(value);
        window.admit(value.proposal_key()); ingest(value); return value;
    }

    ResponseObservation on_time_v3_in_tree(
        ReplicaID target, std::uint32_t tree_id,
        std::uint64_t attempt_start_monotonic_ns)
    {
        const auto &definition = tree(tree_id);
        const auto position = static_cast<std::size_t>(
            std::find(definition.members_breadth_first.begin(),
                      definition.members_breadth_first.end(), target) -
            definition.members_breadth_first.begin());
        REQUIRE(position != 0);
        REQUIRE(position < definition.members_breadth_first.size());
        const auto reporter = definition.members_breadth_first[
            (position - 1) / definition.fanout];
        ResponseObservation value;
        value.schema_version = hotstuff::kResponseObservationSchemaVersionV3;
        value.reporter_id = reporter;
        value.observed_replica_id = target;
        value.configuration = {epoch.epoch_number, tree_id, epoch.epoch_digest};
        value.block_hash = digest("adaptive-v2-v3-on-time-" +
                                  std::to_string(++attempt_number));
        value.expected_message_type = ExpectedMessageType::direct_vote;
        value.outcome = ResponseOutcome::on_time;
        value.response_duration_us = 50;
        value.deadline_duration_us = 100;
        value.attempt_start_monotonic_ns = attempt_start_monotonic_ns;
        value.reporter_monotonic_ns = attempt_start_monotonic_ns + 50'000U;
        value.reporter_sequence = ++reporter_sequences[reporter];
        value.signer_set = {target};
        value.observation_id = hotstuff::compute_response_observation_id(value);
        window.admit(value.proposal_key());
        ingest(value);
        return value;
    }

    ResponseObservation late_v3(
        const ResponseObservation &timeout_observation,
        std::uint64_t reporter_monotonic_ns)
    {
        auto value = timeout_observation;
        REQUIRE(value.schema_version ==
                hotstuff::kResponseObservationSchemaVersionV3);
        REQUIRE(reporter_monotonic_ns >= value.attempt_start_monotonic_ns);
        value.outcome = ResponseOutcome::late;
        value.reporter_monotonic_ns = reporter_monotonic_ns;
        value.response_duration_us =
            (reporter_monotonic_ns - value.attempt_start_monotonic_ns) / 1'000U;
        value.reporter_sequence = ++reporter_sequences[value.reporter_id];
        value.signer_set = {value.observed_replica_id};
        REQUIRE(hotstuff::compute_response_observation_id(value) ==
                timeout_observation.observation_id);
        ingest(value);
        return value;
    }

    void on_time_in_tree(ReplicaID target, std::uint32_t tree_id)
    {
        const auto &definition = tree(tree_id);
        const auto target_position = std::find(
            definition.members_breadth_first.begin(),
            definition.members_breadth_first.end(), target);
        REQUIRE(target_position != definition.members_breadth_first.end());
        const auto position = static_cast<std::size_t>(
            target_position - definition.members_breadth_first.begin());
        REQUIRE(position != 0);
        const auto reporter = definition.members_breadth_first[
            (position - 1) / definition.fanout];
        ResponseObservation value;
        value.reporter_id = reporter;
        value.observed_replica_id = target;
        value.configuration = {
            epoch.epoch_number, tree_id, epoch.epoch_digest};
        value.block_hash = digest(
            "adaptive-v2-tree-on-time-" +
            std::to_string(++attempt_number));
        value.expected_message_type = ExpectedMessageType::direct_vote;
        value.outcome = ResponseOutcome::on_time;
        value.response_duration_us = 50;
        value.deadline_duration_us = 100;
        value.reporter_monotonic_ns = ++monotonic_clock * 1'000;
        value.reporter_sequence = ++reporter_sequences[reporter];
        value.signer_set = {target};
        value.observation_id = hotstuff::compute_response_observation_id(
            value.attempt_identity());
        window.admit(value.proposal_key());
        ingest(value);
    }

    ResponseObservation timeout_v2(
        ReplicaID reporter, ReplicaID target)
    {
        auto value = observation(
            reporter, target, ResponseOutcome::timeout);
        value.schema_version =
            hotstuff::kResponseObservationSchemaVersionV2;
        value.deadline_duration_us = 1;
        REQUIRE(value.reporter_monotonic_ns > 1'000);
        value.attempt_start_monotonic_ns =
            value.reporter_monotonic_ns - 1'000;
        value.reporter_local_commit_monotonic_ns =
            value.attempt_start_monotonic_ns + 500;
        ingest(value);
        return value;
    }

    ResponseObservation aggregate_timeout_in_tree(
        ReplicaID reporter,
        ReplicaID target,
        std::uint32_t tree_id,
        std::uint64_t reporter_monotonic_ns,
        std::uint64_t deadline_duration_us)
    {
        ResponseObservation value;
        value.schema_version =
            hotstuff::kResponseObservationSchemaVersionV3;
        value.reporter_id = reporter;
        value.observed_replica_id = target;
        value.configuration = {
            epoch.epoch_number, tree_id, epoch.epoch_digest};
        value.block_hash = digest(
            "adaptive-v2-aggregate-timeout-" +
            std::to_string(++attempt_number));
        value.expected_message_type = ExpectedMessageType::aggregate_relay;
        value.outcome = ResponseOutcome::timeout;
        value.deadline_duration_us = deadline_duration_us;
        value.reporter_monotonic_ns = reporter_monotonic_ns;
        value.attempt_start_monotonic_ns =
            reporter_monotonic_ns - deadline_duration_us * 1'000U;
        value.reporter_sequence = ++reporter_sequences[reporter];
        value.observation_id =
            hotstuff::compute_response_observation_id(value);
        window.admit(value.proposal_key());
        ingest(value);
        return value;
    }

    void late(
        const ResponseObservation &timeout_observation,
        std::uint64_t response_duration_us = 150)
    {
        auto value = timeout_observation;
        value.schema_version =
            hotstuff::kResponseObservationSchemaVersionV1;
        value.attempt_start_monotonic_ns = 0;
        value.reporter_local_commit_monotonic_ns = 0;
        value.outcome = ResponseOutcome::late;
        value.response_duration_us = response_duration_us;
        value.signer_set = {value.observed_replica_id};
        value.reporter_monotonic_ns = ++monotonic_clock * 1'000;
        value.reporter_sequence =
            ++reporter_sequences[value.reporter_id];
        // The attempt identity, and therefore observation ID, is unchanged.
        ingest(value);
    }

    void on_time(ReplicaID reporter, ReplicaID target)
    {
        ingest(observation(reporter, target, ResponseOutcome::on_time));
    }

    void on_time(
        ReplicaID reporter,
        ReplicaID target,
        std::uint64_t response_duration_us)
    {
        auto value = observation(
            reporter, target, ResponseOutcome::on_time);
        value.response_duration_us = response_duration_us;
        ingest(value);
    }

    const EpochTreeDefinition &tree(std::uint32_t id) const
    {
        const auto *const definition =
            epochs.find_epoch(epoch.epoch_number);
        if (definition == nullptr)
            throw std::logic_error("missing fixture epoch");
        const auto &trees = definition->trees();
        const auto found = std::find_if(
            trees.begin(), trees.end(),
            [id](const auto &value) { return value.tree_id == id; });
        if (found == trees.end())
            throw std::logic_error("missing fixture tree");
        return *found;
    }

    ResponseObservation direct_vote_for_proposal(
        const ProposalKey &proposal,
        std::optional<std::pair<ReplicaID, ReplicaID>> avoid =
            std::nullopt)
    {
        const auto &definition = tree(
            proposal.configuration.tree_id);
        const auto first_leaf =
            ((definition.members_breadth_first.size() - 2) /
             definition.fanout) + 1;
        for (std::size_t position = first_leaf;
             position < definition.members_breadth_first.size();
             ++position)
        {
            const auto parent_position =
                (position - 1) / definition.fanout;
            const auto reporter =
                definition.members_breadth_first[parent_position];
            const auto observed =
                definition.members_breadth_first[position];
            if (avoid.has_value() &&
                avoid->first == reporter &&
                avoid->second == observed)
            {
                continue;
            }
            ResponseObservation value;
            value.reporter_id = reporter;
            value.observed_replica_id = observed;
            value.configuration = proposal.configuration;
            value.block_hash = proposal.block_hash;
            value.expected_message_type =
                ExpectedMessageType::direct_vote;
            value.outcome = ResponseOutcome::on_time;
            value.response_duration_us = 50;
            value.deadline_duration_us = 100;
            value.reporter_monotonic_ns =
                ++monotonic_clock * 1'000;
            value.reporter_sequence =
                ++reporter_sequences[reporter];
            value.signer_set = {observed};
            value.observation_id =
                hotstuff::compute_response_observation_id(
                    value.attempt_identity());
            window.admit(value.proposal_key());
            return value;
        }
        throw std::logic_error(
            "fixture tree lacks a distinct direct-vote edge");
    }

    void cover_tree(std::uint32_t id)
    {
        const ProposalKey proposal{
            ConfigurationId{
                epoch.epoch_number, id, epoch.epoch_digest},
            digest("coverage-tree-" + std::to_string(id))};
        ingest(direct_vote_for_proposal(proposal));
    }

    void anchor_timeout_proposal(
        const ResponseObservation &timeout_observation)
    {
        ingest(direct_vote_for_proposal(
            timeout_observation.proposal_key(),
            std::make_pair(
                timeout_observation.reporter_id,
                timeout_observation.observed_replica_id)));
    }

    void advance_monotonic_clock(std::uint64_t ticks)
    {
        monotonic_clock = std::max(monotonic_clock, ticks);
    }

    void baseline_all()
    {
        for (const auto target : members)
        {
            on_time(
                static_cast<ReplicaID>((target + 1U) % kReplicaCount),
                target);
        }
    }

    void baseline_live_survivors()
    {
        for (const auto target :
             std::vector<ReplicaID>{2, 3, 4, 5, 6})
        {
            const auto reporter = target == 6
                                      ? ReplicaID{2}
                                      : static_cast<ReplicaID>(target + 1U);
            for (std::size_t attempt = 0; attempt < 2; ++attempt)
                on_time(reporter, target, 90);
        }
    }

    void ranked_live_survivor_suffix()
    {
        const std::vector<std::uint64_t> latencies{50, 40, 30, 20, 10};
        const std::vector<std::size_t> attempt_counts{2, 3, 4, 2, 4};
        for (std::size_t index = 0; index < latencies.size(); ++index)
        {
            const auto target = static_cast<ReplicaID>(index + 2U);
            const auto reporter = target == 6
                                      ? ReplicaID{2}
                                      : static_cast<ReplicaID>(target + 1U);
            for (std::size_t attempt = 0;
                 attempt < attempt_counts[index];
                 ++attempt)
            {
                on_time(reporter, target, latencies[index]);
            }
        }
    }

    void persistent_timeouts(
        ReplicaID target,
        const std::vector<ReplicaID> &reporters,
        std::uint32_t attempts_per_reporter)
    {
        for (const auto reporter : reporters)
        {
            for (std::uint32_t attempt = 0;
                 attempt < attempts_per_reporter;
                 ++attempt)
            {
                timeout(reporter, target);
            }
        }
    }

    AdaptationEpochId stage_next_epoch()
    {
        const auto &definition = epochs.stage(
            epoch_input(1, epoch.epoch_digest, 25),
            validation_context(20));
        return {
            definition.epoch_number(), definition.epoch_digest()};
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

std::vector<EpochTreeDefinition> production_n31_trees()
{
    std::vector<EpochTreeDefinition> trees;
    trees.reserve(31);
    for (std::uint32_t root = 0; root < 31; ++root)
    {
        std::vector<ReplicaID> breadth_first;
        breadth_first.reserve(31);
        for (std::uint32_t position = 0; position < 31; ++position)
            breadth_first.push_back((root + position) % 31U);
        trees.push_back({root, 5, 2, std::move(breadth_first), {}});
    }
    return trees;
}

struct N31Fixture
{
    std::vector<ReplicaID> members{n31_membership()};
    EpochStore epochs{members};
    MutableEvidenceWindow window;
    AdaptationEpochId epoch;
    std::unique_ptr<EvidenceLedger> ledger;
    std::map<ReplicaID, std::uint64_t> reporter_sequences;
    std::uint64_t attempt_number{0};

    explicit N31Fixture(std::size_t accepted_capacity = 512)
    {
        EpochDefinitionInput input;
        input.schema_version = hotstuff::kEpochDefinitionSchemaVersion;
        input.epoch_number = 0;
        input.membership_digest = hotstuff::canonical_membership_digest(members);
        input.trees = production_n31_trees();
        input.activation_height = 15;
        input.generation_seed = 0x31'0000;
        input.policy_version = "adaptive-v2-selection-n31-v8-test";
        input.evidence_snapshot_id = "adaptive-v2-selection-n31-v8";
        const auto &definition = epochs.stage(input, validation_context(10));
        epoch = {definition.epoch_number(), definition.epoch_digest()};
        ledger = std::make_unique<EvidenceLedger>(
            epochs, window,
            EvidenceStoreLimits{accepted_capacity, 64});
    }

    const EpochTreeDefinition &tree(std::uint32_t id) const
    {
        const auto *const definition = epochs.find_epoch(epoch.epoch_number);
        REQUIRE(definition != nullptr);
        const auto found = std::find_if(
            definition->trees().begin(), definition->trees().end(),
            [id](const auto &value) { return value.tree_id == id; });
        REQUIRE(found != definition->trees().end());
        return *found;
    }

    ResponseObservation v3_in_tree(
        ReplicaID target, std::uint32_t tree_id, ResponseOutcome outcome,
        std::uint64_t attempt_start_ns)
    {
        const auto &definition = tree(tree_id);
        const auto target_found = std::find(
            definition.members_breadth_first.begin(),
            definition.members_breadth_first.end(), target);
        REQUIRE(target_found != definition.members_breadth_first.end());
        const auto position = static_cast<std::size_t>(
            target_found - definition.members_breadth_first.begin());
        REQUIRE(position != 0);
        const auto reporter = definition.members_breadth_first[
            (position - 1U) / definition.fanout];
        const bool internal =
            ((position * definition.fanout) + 1U) <
            definition.members_breadth_first.size();

        ResponseObservation value;
        value.schema_version = hotstuff::kResponseObservationSchemaVersionV3;
        value.reporter_id = reporter;
        value.observed_replica_id = target;
        value.configuration = {epoch.epoch_number, tree_id, epoch.epoch_digest};
        value.block_hash = digest(
            "adaptive-v2-n31-v8-" + std::to_string(++attempt_number));
        value.expected_message_type = internal
            ? ExpectedMessageType::aggregate_relay
            : ExpectedMessageType::direct_vote;
        value.outcome = outcome;
        value.deadline_duration_us = 100;
        value.attempt_start_monotonic_ns = attempt_start_ns;
        value.reporter_monotonic_ns = attempt_start_ns +
            (outcome == ResponseOutcome::on_time ? 50'000U : 100'000U);
        value.response_duration_us = outcome == ResponseOutcome::on_time ? 50 : 0;
        if (outcome == ResponseOutcome::on_time)
            value.signer_set = {target};
        value.reporter_sequence = ++reporter_sequences[reporter];
        value.observation_id = hotstuff::compute_response_observation_id(value);
        window.admit(value.proposal_key());
        const auto accepted_before = ledger->accepted().size();
        ledger->ingest(AuthenticatedReporter{reporter}, value);
        REQUIRE(ledger->accepted().size() == accepted_before + 1U);
        return value;
    }

    ResponseObservation timeout(
        ReplicaID target, std::uint32_t tree_id,
        std::uint64_t attempt_start_ns)
    {
        return v3_in_tree(target, tree_id, ResponseOutcome::timeout,
                          attempt_start_ns);
    }

    void on_time(ReplicaID target, std::uint32_t tree_id,
                 std::uint64_t attempt_start_ns)
    {
        (void)v3_in_tree(target, tree_id, ResponseOutcome::on_time,
                         attempt_start_ns);
    }

    void late(const ResponseObservation &timeout,
              std::uint64_t reporter_monotonic_ns)
    {
        auto value = timeout;
        value.outcome = ResponseOutcome::late;
        value.reporter_monotonic_ns = reporter_monotonic_ns;
        value.response_duration_us =
            (reporter_monotonic_ns - value.attempt_start_monotonic_ns) /
            1'000U;
        value.signer_set = {value.observed_replica_id};
        value.reporter_sequence = ++reporter_sequences[value.reporter_id];
        REQUIRE(value.observation_id ==
                hotstuff::compute_response_observation_id(value));
        const auto accepted_before = ledger->accepted().size();
        ledger->ingest(AuthenticatedReporter{value.reporter_id}, value);
        REQUIRE(ledger->accepted().size() == accepted_before + 1U);
    }
};

AdaptiveV2FaultWindowArm full_n31_fault_window_arm(
    const AdaptationEpochId &epoch,
    std::uint64_t evidence_start_monotonic_ns)
{
    AdaptiveV2FaultWindowArm arm;
    arm.predecessor_epoch_number = epoch.epoch_number;
    arm.predecessor_epoch_digest = epoch.epoch_digest;
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

struct N31FullCycleSelectionOutcome
{
    AdaptiveV2SelectionResult result;
    bool healthy{false};
};

N31FullCycleSelectionOutcome run_n31_full_cycle_selection(
    const std::vector<ReplicaID> &guarded,
    const std::vector<ReplicaID> &unguarded_nonresponsive = {})
{
    constexpr std::uint64_t kEvidenceStartNs = 1'000'000;
    N31Fixture fixture(2048);
    for (const auto member : fixture.members)
    {
        fixture.on_time(
            member, (member + 1U) % 31U,
            kEvidenceStartNs - 100'000U);
    }

    auto config = selection_config(22, 2, 1024);
    config.required_nonresponsive = 3;
    config.fault_window_arm_required = true;
    config.cardinality_policy =
        hotstuff::AdaptiveV2FaultWindowCardinalityPolicy::
            all_guarded_up_to_fault_bound_v1;
    AdaptiveV2ByzantineSelection selector(
        *fixture.ledger, fixture.members, fixture.epoch, config);
    REQUIRE(selector.freeze_baseline(fixture.ledger->high_watermark()) ==
            AdaptiveV2SelectionStatus::baseline_frozen);
    REQUIRE(selector.arm_fault_window(full_n31_fault_window_arm(
        fixture.epoch, kEvidenceStartNs)));

    const auto is_nonresponsive = [&](ReplicaID member) {
        return std::find(guarded.begin(), guarded.end(), member) !=
                   guarded.end() ||
               std::find(
                   unguarded_nonresponsive.begin(),
                   unguarded_nonresponsive.end(),
                   member) != unguarded_nonresponsive.end();
    };
    std::uint64_t attempt_start_ns = kEvidenceStartNs + 10'000U;
    for (const auto member : fixture.members)
    {
        if (is_nonresponsive(member))
            continue;
        fixture.on_time(
            member, (member + 1U) % 31U, attempt_start_ns++);
        fixture.on_time(
            member, (member + 1U) % 31U, attempt_start_ns++);
    }

    for (const auto target : guarded)
    {
        for (std::uint32_t tree = 0; tree < 31; ++tree)
        {
            if (tree == target)
                continue;
            (void)fixture.timeout(target, tree, attempt_start_ns++);
            (void)fixture.timeout(target, tree, attempt_start_ns++);
        }
    }
    for (const auto target : unguarded_nonresponsive)
    {
        const auto tree = (target + 1U) % 31U;
        for (std::uint32_t attempt = 0; attempt < 30; ++attempt)
            (void)fixture.timeout(target, tree, attempt_start_ns++);
    }

    auto result = selector.select_through(
        fixture.ledger->high_watermark());
    return {std::move(result), selector.healthy()};
}

int baseline_score(
    const std::vector<AdaptiveV2ReplicaScore> &scores,
    ReplicaID replica_id)
{
    for (const auto &entry : scores)
    {
        if (entry.replica_id == replica_id)
            return entry.score;
    }
    throw std::logic_error("missing baseline score");
}

const AdaptiveV2CandidateAudit *candidate(
    const std::vector<AdaptiveV2CandidateAudit> &candidates,
    ReplicaID replica_id)
{
    for (const auto &entry : candidates)
    {
        if (entry.replica_id == replica_id)
            return &entry;
    }
    return nullptr;
}

AcceptedEvidenceRecord containment_coverage_record(
    std::uint64_t ingestion_sequence,
    std::uint32_t tree,
    const AdaptationEpochId &epoch,
    std::uint64_t reporter_monotonic_ns,
    std::uint64_t response_duration_us,
    ReplicaID reporter = 0,
    ReplicaID observed = 1)
{
    ResponseObservation observation;
    observation.reporter_id = reporter;
    observation.observed_replica_id = observed;
    observation.configuration = {
        epoch.epoch_number, tree, epoch.epoch_digest};
    observation.block_hash = digest(
        "fault-containment-coverage-" +
        std::to_string(ingestion_sequence));
    observation.expected_message_type =
        ExpectedMessageType::direct_vote;
    observation.outcome = ResponseOutcome::on_time;
    observation.response_duration_us = response_duration_us;
    observation.deadline_duration_us = 100;
    observation.reporter_monotonic_ns = reporter_monotonic_ns;
    observation.reporter_sequence = ingestion_sequence;
    observation.signer_set = {observed};
    observation.observation_id =
        hotstuff::compute_response_observation_id(
            observation.attempt_identity());
    return {ingestion_sequence, std::move(observation)};
}

static_assert(
    std::is_same<
        decltype(std::declval<const AdaptiveV2ByzantineSelection &>()
                     .score_trajectory()),
        const std::vector<EvidenceReputationAuditUpdate> &>::value,
    "score trajectory must be an immutable borrowed audit view");

} // namespace

TEST_CASE(
    "adaptive-v2 freezes baseline scores and immutable score history",
    "[adaptive-v2][selection][baseline]")
{
    Fixture fixture;
    fixture.on_time(1, 0);
    fixture.timeout(2, 1);
    AdaptiveV2ByzantineSelection selector(
        *fixture.ledger,
        fixture.members,
        fixture.epoch,
        selection_config());

    REQUIRE(selector.freeze_baseline(fixture.ledger->high_watermark()) ==
            AdaptiveV2SelectionStatus::baseline_frozen);
    CHECK(selector.baseline_frozen());
    CHECK(selector.baseline_cutoff() == 2);
    CHECK(selector.current_cutoff() == 2);
    CHECK(baseline_score(selector.baseline_scores(), 0) == 1);
    CHECK(baseline_score(selector.baseline_scores(), 1) == -1);
    REQUIRE(selector.score_trajectory().size() == 2);
    CHECK(selector.score_trajectory()[0].delta == 1);
    CHECK(selector.score_trajectory()[1].delta == -1);
    CHECK(selector.healthy());
}

TEST_CASE(
    "fault containment coverage requires every canonical post-fault tree",
    "[adaptive-v2][selection][fault-containment][coverage]")
{
    constexpr std::uint64_t fault_open_ns = 1'000'000;
    constexpr std::uint32_t required_tree_count = 13;
    const AdaptationEpochId epoch{
        0, digest("fault-containment-coverage-epoch")};
    std::vector<AcceptedEvidenceRecord> records;
    for (std::uint32_t tree = 0; tree < required_tree_count; ++tree)
    {
        records.push_back(containment_coverage_record(
            records.size() + 1,
            tree,
            epoch,
            fault_open_ns + 2'000'000 + tree,
            1'000));
    }

    SECTION("thirteen-tree gate rejects an incomplete prefix")
    {
        const auto result =
            hotstuff::evaluate_adaptive_v2_fault_containment_coverage(
                records,
                epoch,
                records.size() - 1,
                fault_open_ns,
                required_tree_count);
        CHECK(result.status ==
              AdaptiveV2FaultContainmentCoverageStatus::incomplete);
        CHECK(result.required_tree_ids.size() == required_tree_count);
        CHECK(result.observed_tree_ids ==
              std::vector<std::uint32_t>{
                  0, 1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11});
    }

    SECTION("all exact tree IDs pass once and duplicates add nothing")
    {
        auto duplicate = records.front();
        duplicate.ingestion_sequence = records.size() + 1;
        duplicate.observation.reporter_id = 4;
        duplicate.observation.observed_replica_id = 5;
        duplicate.observation.reporter_sequence = 1;
        duplicate.observation.observation_id =
            hotstuff::compute_response_observation_id(
                duplicate.observation.attempt_identity());
        records.push_back(std::move(duplicate));

        const auto result =
            hotstuff::evaluate_adaptive_v2_fault_containment_coverage(
                records,
                epoch,
                records.size(),
                fault_open_ns,
                required_tree_count);
        CHECK(result.status ==
              AdaptiveV2FaultContainmentCoverageStatus::ready);
        CHECK(result.required_tree_ids == result.observed_tree_ids);
        CHECK(result.observed_tree_ids.size() == required_tree_count);
    }

    SECTION("response completion after fault-open does not admit a pre-fault attempt")
    {
        records.front().observation.reporter_monotonic_ns =
            fault_open_ns + 1'000'000;
        records.front().observation.response_duration_us = 1'000;
        const auto result =
            hotstuff::evaluate_adaptive_v2_fault_containment_coverage(
                records,
                epoch,
                records.size(),
                fault_open_ns,
                required_tree_count);
        CHECK(result.status ==
              AdaptiveV2FaultContainmentCoverageStatus::incomplete);
        CHECK(std::find(
                  result.observed_tree_ids.begin(),
                  result.observed_tree_ids.end(),
                  0) == result.observed_tree_ids.end());
    }

    SECTION("a valid sub-microsecond response keeps the conservative boundary")
    {
        records.front().observation.reporter_monotonic_ns =
            fault_open_ns + 999;
        records.front().observation.response_duration_us = 0;
        const auto result =
            hotstuff::evaluate_adaptive_v2_fault_containment_coverage(
                records,
                epoch,
                records.size(),
                fault_open_ns,
                required_tree_count);
        CHECK(result.status ==
              AdaptiveV2FaultContainmentCoverageStatus::ready);
        CHECK(result.required_tree_ids == result.observed_tree_ids);
    }

    SECTION("changing actor identities cannot change tree readiness")
    {
        const auto original =
            hotstuff::evaluate_adaptive_v2_fault_containment_coverage(
                records,
                epoch,
                records.size(),
                fault_open_ns,
                required_tree_count);
        for (auto &record : records)
        {
            record.observation.reporter_id = 5;
            record.observation.observed_replica_id = 6;
            record.observation.observation_id =
                hotstuff::compute_response_observation_id(
                    record.observation.attempt_identity());
        }
        const auto mutated =
            hotstuff::evaluate_adaptive_v2_fault_containment_coverage(
                records,
                epoch,
                records.size(),
                fault_open_ns,
                required_tree_count);
        CHECK(mutated.status == original.status);
        CHECK(mutated.required_tree_ids == original.required_tree_ids);
        CHECK(mutated.observed_tree_ids == original.observed_tree_ids);
    }
}

TEST_CASE(
    "N31 containment coverage cannot be confused with Q21",
    "[adaptive-v2][selection][fault-containment][n31]")
{
    constexpr std::uint64_t fault_open_ns = 5'000'000;
    const AdaptationEpochId epoch{
        0, digest("fault-containment-n31-epoch")};
    std::vector<AcceptedEvidenceRecord> records;
    for (std::uint32_t tree = 0; tree < 31; ++tree)
    {
        records.push_back(containment_coverage_record(
            records.size() + 1,
            tree,
            epoch,
            fault_open_ns + 2'000'000 + tree,
            1'000));
    }

    const auto q21_prefix =
        hotstuff::evaluate_adaptive_v2_fault_containment_coverage(
            records, epoch, 21, fault_open_ns, 31);
    CHECK(q21_prefix.status ==
          AdaptiveV2FaultContainmentCoverageStatus::incomplete);
    CHECK(q21_prefix.observed_tree_ids.size() == 21);
    CHECK(q21_prefix.required_tree_ids.size() == 31);

    const auto n31_prefix =
        hotstuff::evaluate_adaptive_v2_fault_containment_coverage(
            records, epoch, 31, fault_open_ns, 31);
    CHECK(n31_prefix.status ==
          AdaptiveV2FaultContainmentCoverageStatus::ready);
    CHECK(n31_prefix.observed_tree_ids.size() == 31);
}

TEST_CASE(
    "guarded selection uses only exact post-fault proposal keys",
    "[adaptive-v2][selection][fault-containment][reporter-guard]")
{
    constexpr std::uint64_t fault_open_ns = 100'000;
    Fixture fixture;
    fixture.baseline_all();
    auto config = selection_config(3, 1, 128);
    config.required_nonresponsive = 1;
    config.fault_containment_evidence_start_monotonic_ns =
        fault_open_ns;
    config.fault_containment_required_tree_coverage = 7;
    AdaptiveV2ByzantineSelection selector(
        *fixture.ledger,
        fixture.members,
        fixture.epoch,
        config);
    REQUIRE(selector.freeze_baseline(
                fixture.ledger->high_watermark()) ==
            AdaptiveV2SelectionStatus::baseline_frozen);

    // Delayed callbacks for pre-fault proposal keys may arrive after the
    // boundary. Their reporter timestamps cannot qualify those keys.
    fixture.advance_monotonic_clock(200);
    fixture.timeout(2, 0);
    fixture.timeout(3, 0);
    fixture.timeout(4, 0);
    for (std::uint32_t tree = 0; tree < 7; ++tree)
        fixture.cover_tree(tree);

    const auto unqualified = selector.select_through(
        fixture.ledger->high_watermark());
    CHECK(unqualified.status ==
          AdaptiveV2SelectionStatus::insufficient_guarded_candidates);
    CHECK(unqualified.eligible_candidates.empty());

    fixture.advance_monotonic_clock(400);
    std::vector<ResponseObservation> qualifying_timeouts;
    for (const auto reporter : std::vector<ReplicaID>{2, 3, 4})
    {
        auto timeout = fixture.timeout(reporter, 0);
        fixture.anchor_timeout_proposal(timeout);
        qualifying_timeouts.push_back(std::move(timeout));
    }

    const auto selected = selector.select_through(
        fixture.ledger->high_watermark());
    REQUIRE(selected.status == AdaptiveV2SelectionStatus::selected);
    CHECK(selected.selected_replicas ==
          std::vector<ReplicaID>{0});
    REQUIRE(selected.eligible_candidates.size() == 1);
    CHECK(selected.metadata.timeout_audit_basis ==
          AdaptiveV2TimeoutAuditBasis::post_fault_proposal_filtered);
    CHECK(selected.eligible_candidates.front().qualifying_reporters ==
          std::vector<ReplicaID>{2, 3, 4});
    CHECK(selected.eligible_candidates.front().guard_drawdown == -6);
    CHECK(selected.eligible_candidates.front()
              .total_uncompensated_timeouts == 3);

    fixture.late(qualifying_timeouts.front(), 1'000'000);
    for (std::size_t index = 1;
         index < qualifying_timeouts.size();
         ++index)
    {
        fixture.late(qualifying_timeouts[index]);
    }
    const auto compensated = selector.select_through(
        fixture.ledger->high_watermark());
    CHECK(compensated.status ==
          AdaptiveV2SelectionStatus::insufficient_guarded_candidates);
    CHECK(compensated.eligible_candidates.empty());
}

TEST_CASE(
    "v4 fault-window arm is one-shot and binds the wrapped predecessor prefix",
    "[adaptive-v2][selection][fault-window-arm][v4][n7]")
{
    constexpr std::uint64_t kEvidenceStartNs = 100'000;
    Fixture fixture;
    fixture.baseline_all();

    auto config = selection_config(1, 1, 128);
    config.required_nonresponsive = 1;
    config.fault_window_arm_required = true;
    AdaptiveV2ByzantineSelection selector(
        *fixture.ledger, fixture.members, fixture.epoch, config);
    REQUIRE(selector.freeze_baseline(fixture.ledger->high_watermark()) ==
            AdaptiveV2SelectionStatus::baseline_frozen);

    // Evidence that would ordinarily be sufficient remains unusable until
    // the immutable prospective boundary has been armed.
    // The conservative direct-vote start lower bound subtracts the deadline
    // interval, so leave an ample post-boundary margin.
    fixture.advance_monotonic_clock(kEvidenceStartNs + 1'000'000);
    for (const auto tree_id : std::vector<std::uint32_t>{6, 0, 1, 2, 3, 4})
        fixture.cover_tree(tree_id);
    for (const auto reporter : std::vector<ReplicaID>{2, 3, 4})
    {
        for (std::size_t attempt = 0; attempt < 2; ++attempt)
        {
            const auto timeout = fixture.timeout(reporter, 0);
            fixture.anchor_timeout_proposal(timeout);
        }
    }
    const auto pre_arm = selector.select_through(fixture.ledger->high_watermark());
    CHECK(pre_arm.status ==
          AdaptiveV2SelectionStatus::insufficient_guarded_candidates);

    AdaptiveV2FaultWindowArm arm;
    arm.predecessor_epoch_number = fixture.epoch.epoch_number;
    arm.predecessor_epoch_digest = fixture.epoch.epoch_digest;
    arm.evidence_start_monotonic_ns = kEvidenceStartNs;
    arm.prefault_tree_id = 6;
    arm.required_tree_ids = {6, 0, 1, 2, 3, 4};
    SECTION("reject malformed or noncanonical arms")
    {
        auto malformed = arm;
        malformed.required_tree_ids = {6, 0, 1, 2, 3, 3};
        CHECK_FALSE(selector.arm_fault_window(malformed));

        malformed = arm;
        malformed.required_tree_ids = {6, 1, 0, 2, 3, 4};
        CHECK_FALSE(selector.arm_fault_window(malformed));

        malformed = arm;
        malformed.prefault_tree_id = 5;
        CHECK_FALSE(selector.arm_fault_window(malformed));

        malformed = arm;
        malformed.predecessor_epoch_number++;
        CHECK_FALSE(selector.arm_fault_window(malformed));

        malformed = arm;
        malformed.evidence_start_monotonic_ns = 0;
        CHECK_FALSE(selector.arm_fault_window(malformed));

        malformed = arm;
        malformed.required_tree_ids = {6, 0, 1, 2, 3, 4, 5, 6};
        CHECK_FALSE(selector.arm_fault_window(malformed));
    }

    SECTION("accept exact arm once and select already-complete coverage")
    {
        CHECK(selector.arm_fault_window(arm));
        CHECK_FALSE(selector.arm_fault_window(arm));

        // The arm happens after all prefix coverage is already accepted. It
        // must inspect that frozen prefix immediately rather than waiting for
        // another observation to arrive.
        const auto coverage =
            hotstuff::evaluate_adaptive_v2_fault_containment_coverage(
                fixture.ledger->accepted(),
                fixture.epoch,
                fixture.ledger->high_watermark(),
                kEvidenceStartNs,
                arm.required_tree_ids);
        REQUIRE(coverage.status ==
                AdaptiveV2FaultContainmentCoverageStatus::ready);
        const auto replayed = selector.select_through(
            fixture.ledger->high_watermark());
        CHECK(replayed.status ==
              AdaptiveV2SelectionStatus::insufficient_guarded_candidates);
        CHECK(replayed.eligible_candidates.empty());
        CHECK(replayed.metadata.timeout_audit_basis ==
              AdaptiveV2TimeoutAuditBasis::post_fault_proposal_filtered);
    }
}

TEST_CASE(
    "v4 selector permits crashed prefix gaps but filters unanchored timeouts",
    "[adaptive-v2][selection][fault-window-arm][v4][n7][guard]")
{
    constexpr std::uint64_t kEvidenceStartNs = 100'000;
    Fixture fixture;
    fixture.baseline_all();
    auto config = selection_config(1, 1, 128);
    config.required_nonresponsive = 1;
    config.fault_window_arm_required = true;
    AdaptiveV2ByzantineSelection selector(
        *fixture.ledger, fixture.members, fixture.epoch, config);
    REQUIRE(selector.freeze_baseline(fixture.ledger->high_watermark()) ==
            AdaptiveV2SelectionStatus::baseline_frozen);

    fixture.advance_monotonic_clock(kEvidenceStartNs + 1'000'000);
    // Tree 6 models the crashed-root prefix position and intentionally has no
    // post-boundary direct-vote anchor. Responsive positions 0..4 do.
    for (const auto tree : std::vector<std::uint32_t>{0, 1, 2, 3, 4})
        fixture.cover_tree(tree);
    for (const auto tree : std::vector<std::uint32_t>{0, 1, 3})
    {
        const auto timeout = fixture.timeout_in_tree(6, tree);
        fixture.anchor_timeout_proposal(timeout);
    }
    const auto unanchored = fixture.timeout_in_tree(5, 4);
    fixture.on_time(0, 5);

    AdaptiveV2FaultWindowArm arm;
    arm.predecessor_epoch_number = fixture.epoch.epoch_number;
    arm.predecessor_epoch_digest = fixture.epoch.epoch_digest;
    arm.evidence_start_monotonic_ns = kEvidenceStartNs;
    arm.prefault_tree_id = 6;
    arm.required_tree_ids = {6, 0, 1, 2, 3, 4};
    REQUIRE(selector.arm_fault_window(arm));

    const auto selected = selector.select_through(fixture.ledger->high_watermark());
    REQUIRE(selected.status == AdaptiveV2SelectionStatus::selected);
    CHECK(selected.selected_replicas == std::vector<ReplicaID>{6});
    CHECK(candidate(selected.eligible_candidates, 5) == nullptr);
    CHECK(selected.metadata.timeout_audit_basis ==
          AdaptiveV2TimeoutAuditBasis::post_fault_proposal_filtered);
    (void)unanchored;
}

TEST_CASE(
    "v6 arm admits only exact post-boundary prefix aggregate timeout guards",
    "[adaptive-v2][selection][fault-window-arm][v6][n7][aggregate][intentional-red]")
{
    constexpr std::uint64_t kEvidenceStartNs = 100'000;
    Fixture fixture(256, true);
    // Use the frozen N7 production tree order.  Baseline direct-votes must
    // originate at leaves; the two crashed roots are internal children of
    // reporter 6 in tree 6 and are exercised below as aggregate relays.
    for (const auto [target, tree] :
         std::vector<std::pair<ReplicaID, std::uint32_t>>{
             {0, 2}, {1, 3}, {2, 6}, {3, 6}, {4, 6}, {5, 6}, {6, 0}})
        fixture.on_time_in_tree(target, tree);

    auto config = selection_config(1, 1, 128);
    config.required_nonresponsive = 2;
    config.fault_window_arm_required = true;
    AdaptiveV2ByzantineSelection selector(
        *fixture.ledger, fixture.members, fixture.epoch, config);
    REQUIRE(selector.freeze_baseline(fixture.ledger->high_watermark()) ==
            AdaptiveV2SelectionStatus::baseline_frozen);

    AdaptiveV2FaultWindowArm arm;
    arm.predecessor_epoch_number = fixture.epoch.epoch_number;
    arm.predecessor_epoch_digest = fixture.epoch.epoch_digest;
    arm.evidence_start_monotonic_ns = kEvidenceStartNs;
    arm.prefault_tree_id = 6;
    arm.required_tree_ids = {6, 0, 1, 2, 3, 4};
    arm.evidence_basis =
        hotstuff::AdaptiveV2FaultWindowEvidenceBasis::
            exact_timeout_attempt_id_v1;
    // Schema-2/v6 arms retain the legacy all-accepted snapshot basis.
    arm.snapshot_evidence_basis =
        hotstuff::AdaptiveV2FaultWindowSnapshotEvidenceBasis::
            legacy_all_accepted_v1;
    REQUIRE(selector.arm_fault_window(arm));

    fixture.advance_monotonic_clock(kEvidenceStartNs / 1'000U + 1'000U);
    for (const auto [target, tree] :
         std::vector<std::pair<ReplicaID, std::uint32_t>>{
             {0, 2}, {0, 4}, {1, 2}, {1, 3}})
    {
        fixture.timeout_v3_in_tree(
            target, tree, kEvidenceStartNs + 1'000'000U);
    }

    // Both poisons are authenticated and accepted, but cannot supply the
    // third guard: the first can conservatively start before R; the second
    // is outside the immutable wrapped prefix.
    fixture.aggregate_timeout_in_tree(
        6, 0, 6, kEvidenceStartNs + 50'000U, 100);
    fixture.aggregate_timeout_in_tree(
        6, 1, 6, kEvidenceStartNs + 60'000U, 100);
    fixture.aggregate_timeout_in_tree(
        5, 0, 5, kEvidenceStartNs + 3'000'000U, 100);
    const auto poisoned = selector.select_through(fixture.ledger->high_watermark());
    CHECK(poisoned.selected_replicas.empty());
    CHECK(poisoned.eligible_candidates.empty());

    // These source-bound aggregate-relay timeout attempts begin
    // conservatively after R and occupy the prefault tree 6. Together with
    // the two direct-vote reporters they provide exactly f+1 guards for each
    // crashed target.
    fixture.aggregate_timeout_in_tree(
        6, 0, 6, kEvidenceStartNs + 2'000'000U, 100);
    fixture.aggregate_timeout_in_tree(
        6, 1, 6, kEvidenceStartNs + 2'100'000U, 100);
    const auto selected = selector.select_through(fixture.ledger->high_watermark());
    REQUIRE(selected.status == AdaptiveV2SelectionStatus::selected);
    CHECK(selected.selected_replicas == std::vector<ReplicaID>{0, 1});
    REQUIRE(selected.eligible_candidates.size() == 2);
    for (const auto &candidate : selected.eligible_candidates)
    {
        CHECK(candidate.qualifying_reporters ==
              std::vector<ReplicaID>{4, 5, 6});
        CHECK(candidate.total_uncompensated_timeouts == 3);
    }
}

TEST_CASE(
    "v6 exact timeout attempts cannot bleed across one proposal key",
    "[adaptive-v2][selection][fault-window-arm][v6][attempt-id][late]")
{
    constexpr std::uint64_t kEvidenceStartNs = 100'000;
    Fixture fixture(256, true);
    for (const auto [target, tree] :
         std::vector<std::pair<ReplicaID, std::uint32_t>>{
             {0, 2}, {1, 3}, {2, 6}, {3, 6}, {4, 6}, {5, 6}, {6, 0}})
    {
        fixture.on_time_in_tree(target, tree);
    }

    auto config = selection_config(1, 1, 128);
    config.required_nonresponsive = 1;
    config.fault_window_arm_required = true;
    config.responsiveness_policy.minimum_response_rate_ppm = 700'000;
    AdaptiveV2ByzantineSelection selector(
        *fixture.ledger, fixture.members, fixture.epoch, config);
    REQUIRE(selector.freeze_baseline(fixture.ledger->high_watermark()) ==
            AdaptiveV2SelectionStatus::baseline_frozen);

    AdaptiveV2FaultWindowArm arm;
    arm.predecessor_epoch_number = fixture.epoch.epoch_number;
    arm.predecessor_epoch_digest = fixture.epoch.epoch_digest;
    arm.evidence_start_monotonic_ns = kEvidenceStartNs;
    arm.prefault_tree_id = 6;
    arm.required_tree_ids = {6, 0, 1, 2, 3, 4};
    arm.evidence_basis =
        hotstuff::AdaptiveV2FaultWindowEvidenceBasis::
            exact_timeout_attempt_id_v1;
    REQUIRE(selector.arm_fault_window(arm));

    const auto timeout = [&fixture](
                             ReplicaID reporter,
                             ReplicaID target,
                             const ProposalKey &proposal,
                             ExpectedMessageType message_type,
                             std::uint64_t attempt_start_ns,
                             std::uint64_t deadline_us) {
        ResponseObservation value;
        value.schema_version =
            hotstuff::kResponseObservationSchemaVersionV3;
        value.reporter_id = reporter;
        value.observed_replica_id = target;
        value.configuration = proposal.configuration;
        value.block_hash = proposal.block_hash;
        value.expected_message_type = message_type;
        value.outcome = ResponseOutcome::timeout;
        value.deadline_duration_us = deadline_us;
        value.attempt_start_monotonic_ns = attempt_start_ns;
        value.reporter_monotonic_ns =
            attempt_start_ns + deadline_us * 1'000U;
        value.reporter_sequence =
            ++fixture.reporter_sequences[reporter];
        value.observation_id =
            hotstuff::compute_response_observation_id(value);
        fixture.window.admit(proposal);
        fixture.ingest(value);
        return value;
    };
    const auto late = [&fixture](
                          const ResponseObservation &timed_out,
                          std::uint64_t reporter_monotonic_ns) {
        auto value = timed_out;
        value.outcome = ResponseOutcome::late;
        value.reporter_monotonic_ns = reporter_monotonic_ns;
        value.response_duration_us =
            (reporter_monotonic_ns -
             value.attempt_start_monotonic_ns) /
            1'000U;
        value.reporter_sequence =
            ++fixture.reporter_sequences[value.reporter_id];
        value.signer_set = {value.observed_replica_id};
        REQUIRE(
            hotstuff::compute_response_observation_id(value) ==
            timed_out.observation_id);
        fixture.ingest(value);
    };

    const ProposalKey shared_proposal{
        {fixture.epoch.epoch_number, 6, fixture.epoch.epoch_digest},
        digest("v6-shared-proposal-key")};
    const auto pre_fault_attempt = timeout(
        6,
        0,
        shared_proposal,
        ExpectedMessageType::aggregate_relay,
        kEvidenceStartNs - 2'000U,
        1);
    const auto post_fault_attempt = timeout(
        6,
        1,
        shared_proposal,
        ExpectedMessageType::aggregate_relay,
        kEvidenceStartNs + 1'000U,
        1);
    REQUIRE(pre_fault_attempt.proposal_key() ==
            post_fault_attempt.proposal_key());
    REQUIRE(pre_fault_attempt.attempt_identity() !=
            post_fault_attempt.attempt_identity());
    REQUIRE(pre_fault_attempt.observation_id !=
            post_fault_attempt.observation_id);

    timeout(
        4,
        0,
        ProposalKey{
            {fixture.epoch.epoch_number, 2, fixture.epoch.epoch_digest},
            digest("v6-reporter-4-timeout")},
        ExpectedMessageType::direct_vote,
        kEvidenceStartNs + 2'000U,
        1);
    timeout(
        5,
        0,
        ProposalKey{
            {fixture.epoch.epoch_number, 4, fixture.epoch.epoch_digest},
            digest("v6-reporter-5-timeout")},
        ExpectedMessageType::direct_vote,
        kEvidenceStartNs + 3'000U,
        1);

    // Keep target 0 exactly below the response-rate threshold while its
    // pre-R timeout remains open. Once its own late arrives, target 0 becomes
    // a valid survivor without changing target 1's independent guard.
    fixture.advance_monotonic_clock(
        (kEvidenceStartNs + 10'000U) / 1'000U);
    for (std::size_t attempt = 0; attempt < 4; ++attempt)
        fixture.on_time_in_tree(0, 2);

    timeout(
        4,
        1,
        ProposalKey{
            {fixture.epoch.epoch_number, 2, fixture.epoch.epoch_digest},
            digest("v6-reporter-4-target-1-timeout")},
        ExpectedMessageType::direct_vote,
        kEvidenceStartNs + 20'000U,
        1);
    timeout(
        5,
        1,
        ProposalKey{
            {fixture.epoch.epoch_number, 3, fixture.epoch.epoch_digest},
            digest("v6-reporter-5-target-1-timeout")},
        ExpectedMessageType::direct_vote,
        kEvidenceStartNs + 21'000U,
        1);

    const auto selected =
        selector.select_through(fixture.ledger->high_watermark());
    REQUIRE(selected.status ==
            AdaptiveV2SelectionStatus::insufficient_eligible_roots);
    REQUIRE(selected.selected_replicas.empty());
    REQUIRE(selected.eligible_candidates.size() == 1);
    CHECK(candidate(selected.eligible_candidates, 0) == nullptr);
    CHECK(selected.eligible_candidates.front().qualifying_reporters ==
          std::vector<ReplicaID>{4, 5, 6});
    CHECK(selected.eligible_candidates.front().total_uncompensated_timeouts ==
          3);

    late(pre_fault_attempt, kEvidenceStartNs + 7'000U);
    const auto unrelated_late =
        selector.select_through(fixture.ledger->high_watermark());
    REQUIRE(unrelated_late.status == AdaptiveV2SelectionStatus::selected);
    REQUIRE(unrelated_late.selected_replicas ==
            std::vector<ReplicaID>{1});
    REQUIRE(unrelated_late.eligible_candidates.size() == 1);
    CHECK(unrelated_late.eligible_candidates.front()
              .total_uncompensated_timeouts == 3);

    late(post_fault_attempt, kEvidenceStartNs + 8'000U);
    const auto matching_late =
        selector.select_through(fixture.ledger->high_watermark());
    CHECK(matching_late.status ==
          AdaptiveV2SelectionStatus::insufficient_guarded_candidates);
    CHECK(matching_late.selected_replicas.empty());
    CHECK(matching_late.eligible_candidates.empty());
}

TEST_CASE(
    "v7 causal selection replays only post-arm schema3 attempts",
    "[adaptive-v2][selection][fault-window-arm][v7][causal]")
{
    constexpr std::uint64_t kEvidenceStartNs = 100'000;
    Fixture fixture(256, true);
    // These accepted schema3 observations establish the frozen baseline, but
    // v7 must not allow them to heal a target or enter the causal snapshot.
    for (const auto [target, tree] :
         std::vector<std::pair<ReplicaID, std::uint32_t>>{
             {0, 2}, {1, 3}, {2, 6}, {3, 6}, {4, 6}, {5, 6}, {6, 0}})
    {
        fixture.on_time_v3_in_tree(target, tree, kEvidenceStartNs - 10'000U);
    }

    auto config = selection_config(1, 1, 128);
    config.required_nonresponsive = 1;
    config.fault_window_arm_required = true;
    AdaptiveV2ByzantineSelection selector(
        *fixture.ledger, fixture.members, fixture.epoch, config);
    REQUIRE(selector.freeze_baseline(fixture.ledger->high_watermark()) ==
            AdaptiveV2SelectionStatus::baseline_frozen);

    AdaptiveV2FaultWindowArm arm;
    arm.predecessor_epoch_number = fixture.epoch.epoch_number;
    arm.predecessor_epoch_digest = fixture.epoch.epoch_digest;
    arm.evidence_start_monotonic_ns = kEvidenceStartNs;
    arm.prefault_tree_id = 6;
    arm.required_tree_ids = {6, 0, 1, 2, 3, 4};
    arm.evidence_basis =
        hotstuff::AdaptiveV2FaultWindowEvidenceBasis::
            exact_timeout_attempt_id_v1;
    arm.snapshot_evidence_basis =
        hotstuff::AdaptiveV2FaultWindowSnapshotEvidenceBasis::
            exact_post_fault_attempt_start_v1;
    REQUIRE(selector.arm_fault_window(arm));

    // This fact arrives after arming, but its immutable start precedes R.
    // It must leave no trace in the causal snapshot, score, or drawdown.
    fixture.on_time_v3_in_tree(0, 2, kEvidenceStartNs - 1'000U);

    // Every survivor is only responsive in the causal, all-tree domain.
    // The prefix guard remains narrower: its three timeout witnesses for
    // target 0 are two direct-vote leaves plus tree-6 aggregate relay.
    for (const auto [target, tree] :
         std::vector<std::pair<ReplicaID, std::uint32_t>>{
             {1, 2}, {2, 3}, {3, 4}, {4, 5}, {5, 6}, {6, 0},
             {4, 1}})
    {
        fixture.on_time_v3_in_tree(target, tree, kEvidenceStartNs + 1'000U);
    }
    const auto first = fixture.timeout_v3_in_tree(
        0, 2, kEvidenceStartNs + 2'000U);
    const auto second = fixture.timeout_v3_in_tree(
        0, 4, kEvidenceStartNs + 3'000U);
    const auto third = fixture.aggregate_timeout_in_tree(
        6, 0, 6, kEvidenceStartNs + 100'000U, 1);

    const auto selected = selector.select_through(fixture.ledger->high_watermark());
    REQUIRE(selected.status == AdaptiveV2SelectionStatus::selected);
    REQUIRE(selected.snapshot != nullptr);
    CHECK(selected.selected_replicas == std::vector<ReplicaID>{0});
    CHECK(selected.snapshot->accepted_record_count() == 10);
    const auto *const target = candidate(selected.eligible_candidates, 0);
    REQUIRE(target != nullptr);
    CHECK(target->snapshot_nonresponsive);
    CHECK(target->guard_drawdown == -3);
    CHECK(target->current_score == -3);

    // The filtered vector preserves its original accepted ingestion sequence
    // numbers and the original cutoff.  Renumbering its intentional gaps is
    // a distinct snapshot identity.
    std::vector<AcceptedEvidenceRecord> causal_records;
    for (const auto &record : fixture.ledger->accepted())
    {
        if (record.observation.schema_version ==
                hotstuff::kResponseObservationSchemaVersionV3 &&
            record.observation.attempt_start_monotonic_ns >= kEvidenceStartNs)
        {
            causal_records.push_back(record);
        }
    }
    const auto expected = hotstuff::build_adaptation_snapshot(
        fixture.members, fixture.epoch,
        hotstuff::AcceptedEvidenceView{causal_records.data(),
                                       causal_records.size()},
        fixture.ledger->high_watermark(), config.responsiveness_policy,
        config.snapshot_seed);
    CHECK(selected.snapshot->snapshot_id() == expected.snapshot_id());
    for (std::size_t index = 0; index < causal_records.size(); ++index)
        causal_records[index].ingestion_sequence = index + 1U;
    const auto renumbered = hotstuff::build_adaptation_snapshot(
        fixture.members, fixture.epoch,
        hotstuff::AcceptedEvidenceView{causal_records.data(),
                                       causal_records.size()},
        fixture.ledger->high_watermark(), config.responsiveness_policy,
        config.snapshot_seed);
    CHECK(selected.snapshot->snapshot_id() != renumbered.snapshot_id());

    // A single post-R timeout for an otherwise-live replica is causal
    // snapshot evidence even though it cannot meet the f+1 prefix guard.
    // It leaves an unselected nonresponsive survivor and therefore blocks
    // successor roots without changing the selected fault target.
    fixture.timeout_v3_in_tree(6, 0, kEvidenceStartNs + 200'000U);
    const auto pending = selector.select_through(fixture.ledger->high_watermark());
    CHECK(pending.status ==
          AdaptiveV2SelectionStatus::insufficient_eligible_roots);
    CHECK(pending.selected_replicas.empty());
    CHECK(pending.eligible_roots.empty());

    // The exact late shares the timeout ID and compensates only that causal
    // attempt.  It removes the guard rather than reviving the pre-R baseline.
    const auto late = fixture.late_v3(third, kEvidenceStartNs + 102'000U);
    REQUIRE(late.observation_id == third.observation_id);
    const auto compensated = selector.select_through(fixture.ledger->high_watermark());
    CHECK(compensated.status ==
          AdaptiveV2SelectionStatus::insufficient_guarded_candidates);
    REQUIRE(compensated.snapshot != nullptr);
    CHECK(compensated.snapshot->accepted_record_count() == 12);

    // A late fact without its exact timeout and a target-mutated late sharing
    // another timeout's ID are both stopped at the evidence boundary; neither
    // can enter the causal selector replay.
    const auto accepted_before = fixture.ledger->accepted().size();
    auto missing_late = third;
    missing_late.block_hash = digest("v7-missing-timeout");
    missing_late.outcome = ResponseOutcome::late;
    missing_late.reporter_monotonic_ns = kEvidenceStartNs + 300'000U;
    missing_late.response_duration_us =
        (missing_late.reporter_monotonic_ns -
         missing_late.attempt_start_monotonic_ns) / 1'000U;
    missing_late.reporter_sequence =
        ++fixture.reporter_sequences[missing_late.reporter_id];
    missing_late.signer_set = {missing_late.observed_replica_id};
    missing_late.observation_id =
        hotstuff::compute_response_observation_id(missing_late);
    fixture.window.admit(missing_late.proposal_key());
    fixture.ledger->ingest(
        AuthenticatedReporter{missing_late.reporter_id}, missing_late);
    CHECK(fixture.ledger->accepted().size() == accepted_before);
    REQUIRE_FALSE(fixture.ledger->rejected().empty());
    CHECK(fixture.ledger->rejected().back().reason ==
          hotstuff::EvidenceRejectionReason::invalid_transition);

    auto mismatched_late = third;
    mismatched_late.observed_replica_id = 1;
    mismatched_late.outcome = ResponseOutcome::late;
    mismatched_late.reporter_monotonic_ns = kEvidenceStartNs + 301'000U;
    mismatched_late.response_duration_us =
        (mismatched_late.reporter_monotonic_ns -
         mismatched_late.attempt_start_monotonic_ns) / 1'000U;
    mismatched_late.reporter_sequence =
        ++fixture.reporter_sequences[mismatched_late.reporter_id];
    mismatched_late.signer_set = {mismatched_late.observed_replica_id};
    // Deliberately retain third's ID: v3 binds target/start/deadline.
    fixture.ledger->ingest(
        AuthenticatedReporter{mismatched_late.reporter_id}, mismatched_late);
    CHECK(fixture.ledger->accepted().size() == accepted_before);
    CHECK(fixture.ledger->rejected().back().reason ==
          hotstuff::EvidenceRejectionReason::observation_id_mismatch);
    (void)first;
    (void)second;
}

TEST_CASE(
    "v7 causal arm rejects any legacy current-epoch record",
    "[adaptive-v2][selection][fault-window-arm][v7][schema3][poison]")
{
    constexpr std::uint64_t kEvidenceStartNs = 100'000;
    Fixture fixture;
    fixture.baseline_all(); // Deliberately schema1: valid archive, invalid v7 arm input.

    auto config = selection_config(1, 1, 128);
    config.required_nonresponsive = 1;
    config.fault_window_arm_required = true;
    AdaptiveV2ByzantineSelection selector(
        *fixture.ledger, fixture.members, fixture.epoch, config);
    REQUIRE(selector.freeze_baseline(fixture.ledger->high_watermark()) ==
            AdaptiveV2SelectionStatus::baseline_frozen);

    AdaptiveV2FaultWindowArm arm;
    arm.predecessor_epoch_number = fixture.epoch.epoch_number;
    arm.predecessor_epoch_digest = fixture.epoch.epoch_digest;
    arm.evidence_start_monotonic_ns = kEvidenceStartNs;
    arm.prefault_tree_id = 0;
    arm.required_tree_ids = {0};
    arm.evidence_basis =
        hotstuff::AdaptiveV2FaultWindowEvidenceBasis::
            exact_timeout_attempt_id_v1;
    arm.snapshot_evidence_basis =
        hotstuff::AdaptiveV2FaultWindowSnapshotEvidenceBasis::
            exact_post_fault_attempt_start_v1;
    REQUIRE(selector.arm_fault_window(arm));

    fixture.timeout_v3_in_tree(3, 0, kEvidenceStartNs + 1'000U);
    const auto poisoned = selector.select_through(fixture.ledger->high_watermark());
    CHECK(poisoned.status == AdaptiveV2SelectionStatus::projection_failed);
    CHECK_FALSE(selector.healthy());
}

TEST_CASE(
    "v12 full-cycle N31 selection preserves the all-guarded safety bound",
    "[adaptive-v2][selection][fault-window-arm][v12][n31][full-cycle]")
{
    const std::vector<ReplicaID> guarded_three{21, 22, 23};
    const std::vector<ReplicaID> guarded_ten{
        4, 5, 8, 9, 10, 14, 16, 21, 22, 23};

    SECTION("three guarded replicas select without widening the cohort")
    {
        auto outcome = run_n31_full_cycle_selection(guarded_three);
        REQUIRE(outcome.result.status ==
                AdaptiveV2SelectionStatus::selected);
        auto selected = outcome.result.selected_replicas;
        std::sort(selected.begin(), selected.end());
        CHECK(selected == guarded_three);
        CHECK(outcome.result.eligible_candidates.size() == 3);
        CHECK(outcome.result.eligible_roots.size() == 21);
        CHECK(outcome.healthy);
    }

    SECTION("the full N minus Q cohort remains selectable")
    {
        auto outcome = run_n31_full_cycle_selection(guarded_ten);
        REQUIRE(outcome.result.status ==
                AdaptiveV2SelectionStatus::selected);
        auto selected = outcome.result.selected_replicas;
        std::sort(selected.begin(), selected.end());
        CHECK(selected == guarded_ten);
        CHECK(outcome.result.eligible_candidates.size() == 10);
        CHECK(outcome.result.eligible_roots.size() == 21);
        CHECK(outcome.healthy);
    }

    SECTION("eleven guarded replicas fail closed above N minus Q")
    {
        auto over_bound = guarded_ten;
        over_bound.push_back(24);
        auto outcome = run_n31_full_cycle_selection(over_bound);
        CHECK(outcome.result.status ==
              AdaptiveV2SelectionStatus::
                  guarded_candidate_bound_exceeded);
        CHECK(outcome.result.eligible_candidates.size() == 11);
        CHECK(outcome.result.selected_replicas.empty());
        CHECK(outcome.result.eligible_roots.empty());
        CHECK_FALSE(outcome.healthy);
    }

    SECTION("an unselected nonresponsive replica blocks root eligibility")
    {
        auto outcome = run_n31_full_cycle_selection(
            guarded_three, {24});
        CHECK(outcome.result.status ==
              AdaptiveV2SelectionStatus::insufficient_eligible_roots);
        CHECK(outcome.result.eligible_candidates.size() == 3);
        CHECK(outcome.result.selected_replicas.empty());
        CHECK(outcome.result.eligible_roots.empty());
        CHECK(outcome.healthy);
    }
}

TEST_CASE(
    "v8 H17 N31 guard admits every topology-valid prefix reporter",
    "[adaptive-v2][selection][fault-window-arm][v8][n31][h17]")
{
    constexpr std::uint64_t kEvidenceStartNs = 1'000'000;
    N31Fixture fixture;
    const std::vector<ReplicaID> targets{21, 22, 23};

    // A v7 causal arm requires schema3 throughout the predecessor epoch.
    // Freeze that baseline before the post-fault rows below.
    for (const auto member : fixture.members)
    {
        fixture.on_time(member, (member + 1U) % 31U,
                        kEvidenceStartNs - 100'000U);
    }

    auto config = selection_config(22, 2, 512);
    config.required_nonresponsive = 3;
    config.fault_window_arm_required = true;
    AdaptiveV2ByzantineSelection h16(
        *fixture.ledger, fixture.members, fixture.epoch, config);
    AdaptiveV2ByzantineSelection h17(
        *fixture.ledger, fixture.members, fixture.epoch, config);
    auto v9_config = config;
    v9_config.cardinality_policy =
        hotstuff::AdaptiveV2FaultWindowCardinalityPolicy::
            all_guarded_up_to_fault_bound_v1;
    AdaptiveV2ByzantineSelection v9(
        *fixture.ledger, fixture.members, fixture.epoch, v9_config);
    const auto baseline_cutoff = fixture.ledger->high_watermark();
    REQUIRE(h16.freeze_baseline(baseline_cutoff) ==
            AdaptiveV2SelectionStatus::baseline_frozen);
    REQUIRE(h17.freeze_baseline(baseline_cutoff) ==
            AdaptiveV2SelectionStatus::baseline_frozen);
    REQUIRE(v9.freeze_baseline(baseline_cutoff) ==
            AdaptiveV2SelectionStatus::baseline_frozen);

    const auto arm = [&](std::uint32_t horizon) {
        AdaptiveV2FaultWindowArm value;
        value.predecessor_epoch_number = fixture.epoch.epoch_number;
        value.predecessor_epoch_digest = fixture.epoch.epoch_digest;
        value.evidence_start_monotonic_ns = kEvidenceStartNs;
        value.prefault_tree_id = 20;
        for (std::uint32_t offset = 0; offset < horizon; ++offset)
            value.required_tree_ids.push_back((20U + offset) % 31U);
        value.evidence_basis =
            hotstuff::AdaptiveV2FaultWindowEvidenceBasis::
                exact_timeout_attempt_id_v1;
        value.snapshot_evidence_basis =
            hotstuff::AdaptiveV2FaultWindowSnapshotEvidenceBasis::
                exact_post_fault_attempt_start_v1;
        return value;
    };
    REQUIRE(h16.arm_fault_window(arm(16)));
    REQUIRE(h17.arm_fault_window(arm(17)));
    auto v9_arm = arm(17);
    v9_arm.cardinality_policy =
        hotstuff::AdaptiveV2FaultWindowCardinalityPolicy::
            all_guarded_up_to_fault_bound_v1;
    REQUIRE(v9.arm_fault_window(std::move(v9_arm)));

    // Causal snapshot evidence is deliberately wider than the guard prefix.
    // Every survivor is responsive; only 21/22/23 can become candidates.
    for (const auto member : fixture.members)
    {
        if (std::find(targets.begin(), targets.end(), member) == targets.end())
            fixture.on_time(member, (member + 1U) % 31U,
                            kEvidenceStartNs + 10'000U);
    }

    std::uint64_t start = kEvidenceStartNs + 100'000U;
    const auto emit_twice = [&](ReplicaID target, std::uint32_t tree) {
        const auto first = fixture.timeout(target, tree, start);
        start += 1'000U;
        const auto second = fixture.timeout(target, tree, start);
        start += 1'000U;
        return std::pair<ResponseObservation, ResponseObservation>{
            first, second};
    };

    // H16 has 11/10/10 qualifying reporters.  These include aggregate and
    // direct observations from any topology-valid prefix tree, rather than a
    // profile-preselected reporter/tree subset.
    for (const auto tree : std::vector<std::uint32_t>{
             20, 24, 25, 26, 28, 29, 30, 0, 2, 3, 4})
        (void)emit_twice(21, tree);
    for (const auto tree : std::vector<std::uint32_t>{
             20, 24, 25, 26, 27, 29, 30, 0, 1, 3})
        (void)emit_twice(22, tree);
    for (const auto tree : std::vector<std::uint32_t>{
             20, 24, 25, 26, 27, 28, 30, 0, 1, 2})
        (void)emit_twice(23, tree);

    // A pre-R schema3 row is authenticated but cannot enter either v7/H17
    // causal replay or exact timeout-ID guard replay.
    (void)fixture.timeout(22, 5, kEvidenceStartNs - 1'000U);
    (void)fixture.timeout(22, 5, kEvidenceStartNs - 500U);
    const auto h16_pending = h16.select_through(fixture.ledger->high_watermark());
    const auto h17_before_tree5 = h17.select_through(fixture.ledger->high_watermark());
    CHECK(h16_pending.status ==
          AdaptiveV2SelectionStatus::insufficient_guarded_candidates);
    CHECK(h17_before_tree5.status ==
          AdaptiveV2SelectionStatus::insufficient_guarded_candidates);
    const auto v9_below_minimum = v9.select_through(
        fixture.ledger->high_watermark());
    CHECK(v9_below_minimum.status ==
          AdaptiveV2SelectionStatus::insufficient_guarded_candidates);
    CHECK(v9_below_minimum.eligible_candidates.size() < 3);
    CHECK(v9_below_minimum.selected_replicas.empty());
    REQUIRE(candidate(h17_before_tree5.eligible_candidates, 21) != nullptr);
    CHECK(candidate(h17_before_tree5.eligible_candidates, 21)
              ->qualifying_reporters.size() == 11);

    // Tree 5 is outside H16 but inside H17. Its live parent 8 directly
    // observes all three targets; two exact attempts provide K=2.
    const auto tree5_21 = emit_twice(21, 5);
    const auto tree5_22 = emit_twice(22, 5);
    const auto tree5_23 = emit_twice(23, 5);
    (void)tree5_21;
    (void)tree5_23;
    const auto still_h16 = h16.select_through(fixture.ledger->high_watermark());
    CHECK(still_h16.status ==
          AdaptiveV2SelectionStatus::insufficient_guarded_candidates);
    const auto selected = h17.select_through(fixture.ledger->high_watermark());
    REQUIRE(selected.status == AdaptiveV2SelectionStatus::selected);
    CHECK(selected.selected_replicas == targets);
    const auto v9_targets = v9.select_through(
        fixture.ledger->high_watermark());
    REQUIRE(v9_targets.status == AdaptiveV2SelectionStatus::selected);
    CHECK(v9_targets.selected_replicas == targets);
    REQUIRE(candidate(selected.eligible_candidates, 21) != nullptr);
    REQUIRE(candidate(selected.eligible_candidates, 22) != nullptr);
    REQUIRE(candidate(selected.eligible_candidates, 23) != nullptr);
    CHECK(candidate(selected.eligible_candidates, 21)
              ->qualifying_reporters.size() == 12);
    CHECK(candidate(selected.eligible_candidates, 22)
              ->qualifying_reporters.size() == 11);
    CHECK(candidate(selected.eligible_candidates, 23)
              ->qualifying_reporters.size() == 11);

    // Guard eligibility is not sufficient when causal snapshot evidence also
    // makes an unselected survivor nonresponsive.
    const std::vector<ReplicaID> dependent_nonresponsive{10, 11, 12, 15};
    for (const auto replica : dependent_nonresponsive)
    {
        for (std::uint32_t offset = 0; offset < 17; ++offset)
        {
            const auto tree = (20U + offset) % 31U;
            if (tree != replica)
                (void)emit_twice(replica, tree);
        }
    }
    const auto extra_survivor = h17.select_through(fixture.ledger->high_watermark());
    CHECK(extra_survivor.status ==
          AdaptiveV2SelectionStatus::insufficient_eligible_roots);
    CHECK(extra_survivor.selected_replicas.empty());
    CHECK(extra_survivor.eligible_roots.empty());
    CHECK(extra_survivor.eligible_candidates.size() == 7);
    const auto slot05_shaped = v9.select_through(
        fixture.ledger->high_watermark());
    REQUIRE(slot05_shaped.status == AdaptiveV2SelectionStatus::selected);
    CHECK(slot05_shaped.eligible_candidates.size() == 7);
    auto selected_slot05 = slot05_shaped.selected_replicas;
    std::sort(selected_slot05.begin(), selected_slot05.end());
    CHECK(selected_slot05 ==
          std::vector<ReplicaID>{10, 11, 12, 15, 21, 22, 23});
    CHECK(slot05_shaped.eligible_roots.size() == 21);

    // More than N-Q guarded candidates is outside the consensus-safe
    // containment envelope and is terminal, never a wider placement.
    for (const auto replica : std::vector<ReplicaID>{16, 17, 18, 19})
    {
        for (std::uint32_t offset = 0; offset < 17; ++offset)
        {
            const auto tree = (20U + offset) % 31U;
            if (tree != replica)
                (void)emit_twice(replica, tree);
        }
    }
    const auto over_bound = v9.select_through(
        fixture.ledger->high_watermark());
    CHECK(over_bound.status == AdaptiveV2SelectionStatus::
          guarded_candidate_bound_exceeded);
    CHECK(over_bound.eligible_candidates.size() == 11);
    CHECK(over_bound.selected_replicas.empty());
    CHECK_FALSE(v9.healthy());

    // Exact late compensation applies only to the matching schema3 ID. It
    // removes reporter 8's two tree-5 attempts for target 22 and reopens the
    // H17 guard; a target-mutated late carrying that ID is rejected.
    fixture.late(tree5_22.first, tree5_22.first.reporter_monotonic_ns + 200'000U);
    fixture.late(tree5_22.second, tree5_22.second.reporter_monotonic_ns + 200'000U);
    const auto late_pending = h17.select_through(fixture.ledger->high_watermark());
    CHECK(late_pending.status ==
          AdaptiveV2SelectionStatus::insufficient_eligible_roots);
    const auto accepted_before_bad_late = fixture.ledger->accepted().size();
    auto mismatched_late = tree5_22.first;
    mismatched_late.outcome = ResponseOutcome::late;
    mismatched_late.observed_replica_id = 23;
    mismatched_late.reporter_monotonic_ns += 300'000U;
    mismatched_late.response_duration_us =
        (mismatched_late.reporter_monotonic_ns -
         mismatched_late.attempt_start_monotonic_ns) / 1'000U;
    mismatched_late.signer_set = {23};
    mismatched_late.reporter_sequence =
        ++fixture.reporter_sequences[mismatched_late.reporter_id];
    fixture.ledger->ingest(
        AuthenticatedReporter{mismatched_late.reporter_id}, mismatched_late);
    CHECK(fixture.ledger->accepted().size() == accepted_before_bad_late);
    CHECK(fixture.ledger->rejected().back().reason ==
          hotstuff::EvidenceRejectionReason::observation_id_mismatch);

    // This fixture already has a schema3 stream for reporter 8. A later
    // legacy timestamp is therefore rejected by the ledger before it can
    // reach the selector; the preceding v7 test covers an accepted legacy
    // current-epoch record poisoning the causal projection.
    auto legacy = tree5_21.first;
    legacy.schema_version = hotstuff::kResponseObservationSchemaVersionV1;
    legacy.attempt_start_monotonic_ns = 0;
    legacy.block_hash = digest("adaptive-v2-n31-v8-legacy-poison");
    legacy.reporter_sequence = ++fixture.reporter_sequences[legacy.reporter_id];
    legacy.observation_id = hotstuff::compute_response_observation_id(
        legacy.attempt_identity());
    fixture.window.admit(legacy.proposal_key());
    const auto accepted_before_legacy = fixture.ledger->accepted().size();
    fixture.ledger->ingest(AuthenticatedReporter{legacy.reporter_id}, legacy);
    CHECK(fixture.ledger->accepted().size() == accepted_before_legacy);
    CHECK(fixture.ledger->rejected().back().reason ==
          hotstuff::EvidenceRejectionReason::reporter_timestamp_regression);
}

TEST_CASE(
    "f Byzantine reporters cannot select a responsive replica",
    "[adaptive-v2][selection][byzantine-guard]")
{
    Fixture fixture;
    fixture.baseline_all();
    AdaptiveV2ByzantineSelection selector(
        *fixture.ledger,
        fixture.members,
        fixture.epoch,
        selection_config(4, 2));
    REQUIRE(selector.freeze_baseline(fixture.ledger->high_watermark()) ==
            AdaptiveV2SelectionStatus::baseline_frozen);

    fixture.persistent_timeouts(0, {2, 3}, 2);
    const auto result =
        selector.select_through(fixture.ledger->high_watermark());

    REQUIRE(result.status ==
            AdaptiveV2SelectionStatus::insufficient_guarded_candidates);
    REQUIRE(result.snapshot != nullptr);
    CHECK(result.selected_replicas.empty());
    CHECK(candidate(result.eligible_candidates, 0) == nullptr);
    const auto &ranking = result.snapshot->ranking();
    const auto target = std::find_if(
        ranking.begin(), ranking.end(), [](const auto &entry) {
            return entry.replica_id == 0;
        });
    REQUIRE(target != ranking.end());
    CHECK(target->classification == ResponsivenessClass::nonresponsive);
}

TEST_CASE(
    "f plus one reporters require per-reporter persistence and score drop",
    "[adaptive-v2][selection][persistence][score]")
{
    SECTION("one timeout from each reporter is below K")
    {
        Fixture fixture;
        fixture.baseline_all();
        AdaptiveV2ByzantineSelection selector(
            *fixture.ledger,
            fixture.members,
            fixture.epoch,
            selection_config(3, 2));
        REQUIRE(selector.freeze_baseline(
                    fixture.ledger->high_watermark()) ==
                AdaptiveV2SelectionStatus::baseline_frozen);
        fixture.persistent_timeouts(0, {2, 3, 4}, 1);

        const auto result = selector.select_through(
            fixture.ledger->high_watermark());
        CHECK(result.status == AdaptiveV2SelectionStatus::
                                   insufficient_guarded_candidates);
        CHECK(candidate(result.eligible_candidates, 0) == nullptr);
    }

    SECTION("persistent reports below the score threshold do not qualify")
    {
        Fixture fixture;
        fixture.baseline_all();
        AdaptiveV2ByzantineSelection selector(
            *fixture.ledger,
            fixture.members,
            fixture.epoch,
            selection_config(7, 2));
        REQUIRE(selector.freeze_baseline(
                    fixture.ledger->high_watermark()) ==
                AdaptiveV2SelectionStatus::baseline_frozen);
        fixture.persistent_timeouts(0, {2, 3, 4}, 2);

        const auto result = selector.select_through(
            fixture.ledger->high_watermark());
        CHECK(result.status == AdaptiveV2SelectionStatus::
                                   insufficient_guarded_candidates);
        CHECK(candidate(result.eligible_candidates, 0) == nullptr);
    }

    SECTION("three reporters with two attempts produce one guarded candidate")
    {
        Fixture fixture;
        fixture.baseline_all();
        AdaptiveV2ByzantineSelection selector(
            *fixture.ledger,
            fixture.members,
            fixture.epoch,
            selection_config());
        REQUIRE(selector.freeze_baseline(
                    fixture.ledger->high_watermark()) ==
                AdaptiveV2SelectionStatus::baseline_frozen);
        fixture.persistent_timeouts(0, {2, 3, 4}, 2);

        const auto result = selector.select_through(
            fixture.ledger->high_watermark());
        REQUIRE(result.status == AdaptiveV2SelectionStatus::
                                     insufficient_guarded_candidates);
        const auto *audit = candidate(result.eligible_candidates, 0);
        REQUIRE(audit != nullptr);
        CHECK(audit->qualifying_reporters ==
              std::vector<ReplicaID>{2, 3, 4});
        CHECK(audit->total_uncompensated_timeouts == 6);
        CHECK(audit->guard_drawdown == -6);
        CHECK(audit->baseline_score_delta == -6);
        CHECK(audit->guarded_eligible);
        CHECK(result.selected_replicas.empty());
    }
}

TEST_CASE(
    "healthy post-baseline evidence cannot bank adaptive guard credit",
    "[adaptive-v2][selection][drawdown][high-water]")
{
    Fixture fixture(512);
    fixture.baseline_all();
    AdaptiveV2ByzantineSelection selector(
        *fixture.ledger,
        fixture.members,
        fixture.epoch,
        selection_config());
    REQUIRE(selector.freeze_baseline(fixture.ledger->high_watermark()) ==
            AdaptiveV2SelectionStatus::baseline_frozen);

    for (std::size_t attempt = 0; attempt < 40; ++attempt)
        fixture.on_time(1, 0);
    fixture.persistent_timeouts(0, {2, 3, 4}, 2);

    const auto result =
        selector.select_through(fixture.ledger->high_watermark());
    REQUIRE(result.status ==
            AdaptiveV2SelectionStatus::insufficient_guarded_candidates);
    const auto *audit = candidate(result.eligible_candidates, 0);
    REQUIRE(audit != nullptr);
    CHECK(audit->baseline_score == 1);
    CHECK(audit->current_score == 35);
    CHECK(audit->baseline_score_delta == 34);
    CHECK(audit->guard_drawdown == -6);
    CHECK(audit->score_drop_satisfied);
    CHECK(audit->guarded_eligible);
}

TEST_CASE(
    "raw credit cannot change deterministic guarded candidate order",
    "[adaptive-v2][selection][drawdown][ranking]")
{
    Fixture fixture(512);
    fixture.baseline_all();
    AdaptiveV2ByzantineSelection selector(
        *fixture.ledger,
        fixture.members,
        fixture.epoch,
        selection_config());
    REQUIRE(selector.freeze_baseline(fixture.ledger->high_watermark()) ==
            AdaptiveV2SelectionStatus::baseline_frozen);

    for (std::size_t attempt = 0; attempt < 40; ++attempt)
        fixture.on_time(1, 0);
    for (const auto target : {ReplicaID{0}, ReplicaID{1}, ReplicaID{2}})
        fixture.persistent_timeouts(target, {3, 4, 5}, 2);

    const auto result =
        selector.select_through(fixture.ledger->high_watermark());
    REQUIRE(result.status ==
            AdaptiveV2SelectionStatus::insufficient_eligible_roots);
    REQUIRE(result.eligible_candidates.size() == 3);
    CHECK(result.eligible_candidates[0].replica_id == 0);
    CHECK(result.eligible_candidates[1].replica_id == 1);
    CHECK(result.eligible_candidates[2].replica_id == 2);
    CHECK(result.eligible_candidates[0].baseline_score_delta == 34);
    CHECK(result.eligible_candidates[1].baseline_score_delta == -6);
    CHECK(result.eligible_candidates[2].baseline_score_delta == -6);
    for (const auto &audit : result.eligible_candidates)
        CHECK(audit.guard_drawdown == -6);
}

TEST_CASE(
    "late evidence before and after the baseline preserves drawdown causality",
    "[adaptive-v2][selection][drawdown][late][incremental]")
{
    SECTION("a pre-baseline timeout cannot bank late-response credit")
    {
        Fixture fixture;
        const auto pre_baseline_timeout = fixture.timeout(2, 0);
        fixture.baseline_all();
        AdaptiveV2ByzantineSelection selector(
            *fixture.ledger,
            fixture.members,
            fixture.epoch,
            selection_config());
        REQUIRE(selector.freeze_baseline(
                    fixture.ledger->high_watermark()) ==
                AdaptiveV2SelectionStatus::baseline_frozen);

        fixture.timeout(3, 0);
        fixture.late(pre_baseline_timeout);
        fixture.timeout(3, 0);
        fixture.persistent_timeouts(0, {2, 4}, 2);
        const auto result = selector.select_through(
            fixture.ledger->high_watermark());

        const auto *audit = candidate(result.eligible_candidates, 0);
        REQUIRE(audit != nullptr);
        CHECK(audit->baseline_score_delta == -5);
        CHECK(audit->guard_drawdown == -6);
        CHECK(audit->total_uncompensated_timeouts == 6);
    }

    SECTION("a later response compensates one prior drawdown exactly once")
    {
        Fixture fixture;
        fixture.baseline_all();
        AdaptiveV2ByzantineSelection selector(
            *fixture.ledger,
            fixture.members,
            fixture.epoch,
            selection_config(5, 1));
        REQUIRE(selector.freeze_baseline(
                    fixture.ledger->high_watermark()) ==
                AdaptiveV2SelectionStatus::baseline_frozen);

        const auto compensated = fixture.timeout(2, 0);
        fixture.timeout(2, 0);
        fixture.persistent_timeouts(0, {3, 4}, 2);
        const auto first = selector.select_through(
            fixture.ledger->high_watermark());
        const auto *first_audit = candidate(
            first.eligible_candidates, 0);
        REQUIRE(first_audit != nullptr);
        CHECK(first_audit->guard_drawdown == -6);

        fixture.late(compensated);
        const auto second = selector.select_through(
            fixture.ledger->high_watermark());
        const auto *second_audit = candidate(
            second.eligible_candidates, 0);
        REQUIRE(second_audit != nullptr);
        CHECK(second_audit->guard_drawdown == -5);
        CHECK(second_audit->total_uncompensated_timeouts == 5);
        CHECK(selector.current_cutoff() ==
              fixture.ledger->high_watermark());
    }
}

TEST_CASE(
    "late evidence compensates score and removes a timeout attempt",
    "[adaptive-v2][selection][late]")
{
    Fixture fixture;
    fixture.baseline_all();
    AdaptiveV2ByzantineSelection selector(
        *fixture.ledger,
        fixture.members,
        fixture.epoch,
        selection_config(5, 2));
    REQUIRE(selector.freeze_baseline(fixture.ledger->high_watermark()) ==
            AdaptiveV2SelectionStatus::baseline_frozen);

    const auto compensated = fixture.timeout(2, 0);
    fixture.timeout(2, 0);
    fixture.persistent_timeouts(0, {3, 4}, 2);
    fixture.late(compensated);
    const auto result =
        selector.select_through(fixture.ledger->high_watermark());

    CHECK(result.status == AdaptiveV2SelectionStatus::
                               insufficient_guarded_candidates);
    CHECK(candidate(result.eligible_candidates, 0) == nullptr);
    REQUIRE(selector.score_trajectory().size() == 14);
    CHECK(selector.score_trajectory().back().evidence_outcome ==
          ResponseOutcome::late);
    CHECK(selector.score_trajectory().back().delta == 1);
    CHECK(selector.score_trajectory().back().score == -4);
}

TEST_CASE(
    "two deterministic crash targets preserve N f Q metadata",
    "[adaptive-v2][selection][n7][deterministic]")
{
    Fixture fixture;
    fixture.baseline_all();
    AdaptiveV2ByzantineSelection selector(
        *fixture.ledger,
        fixture.members,
        fixture.epoch,
        selection_config());
    REQUIRE(selector.freeze_baseline(fixture.ledger->high_watermark()) ==
            AdaptiveV2SelectionStatus::baseline_frozen);

    // Target 0 has more qualifying reporters but fewer total timeouts than 1.
    fixture.persistent_timeouts(0, {2, 3, 4, 5}, 2);
    fixture.persistent_timeouts(1, {2, 3, 4}, 3);
    const auto cutoff = fixture.ledger->high_watermark();
    const auto result = selector.select_through(cutoff);

    REQUIRE(result.status == AdaptiveV2SelectionStatus::selected);
    CHECK(result.selected_replicas ==
          std::vector<ReplicaID>{0, 1});
    CHECK(result.eligible_roots ==
          std::vector<ReplicaID>{2, 3, 4, 5, 6});
    REQUIRE(result.eligible_candidates.size() == 2);
    CHECK(result.eligible_candidates[0].replica_id == 0);
    CHECK(result.eligible_candidates[0].qualifying_reporters.size() == 4);
    CHECK(result.eligible_candidates[0].total_uncompensated_timeouts == 8);
    CHECK(result.eligible_candidates[1].replica_id == 1);
    CHECK(result.eligible_candidates[1].qualifying_reporters.size() == 3);
    CHECK(result.eligible_candidates[1].total_uncompensated_timeouts == 9);
    CHECK(result.metadata.replica_count == 7);
    CHECK(result.metadata.fault_threshold == 2);
    CHECK(result.metadata.quorum == 5);
    CHECK(result.metadata.required_nonresponsive == 2);
    CHECK(result.metadata.required_qualifying_reporters == 3);
    CHECK(result.metadata.minimum_timeouts_per_reporter == 2);
    CHECK(result.metadata.minimum_score_drop == 6);
    CHECK(result.metadata.baseline_cutoff == 7);
    CHECK(result.metadata.evidence_cutoff == cutoff);
    CHECK(selector.quorum_metadata().replica_count == 7);
    CHECK(selector.quorum_metadata().fault_threshold == 2);
    CHECK(selector.quorum_metadata().quorum == 5);
    CHECK(selector.membership() == fixed_membership());
    CHECK(selector.current_epoch() == fixture.epoch);
    CHECK(fixture.epochs.size() == 1);
    CHECK(fixture.ledger->healthy());
}

TEST_CASE(
    "a bounded minority target leaves a real choice among responsive roots",
    "[adaptive-v2][selection][minority][roots]")
{
    Fixture fixture;
    fixture.baseline_all();
    auto config = selection_config();
    config.required_nonresponsive = 1;
    AdaptiveV2ByzantineSelection selector(
        *fixture.ledger,
        fixture.members,
        fixture.epoch,
        config);
    REQUIRE(selector.freeze_baseline(fixture.ledger->high_watermark()) ==
            AdaptiveV2SelectionStatus::baseline_frozen);

    fixture.persistent_timeouts(6, {0, 1, 2}, 2);
    const auto result =
        selector.select_through(fixture.ledger->high_watermark());

    REQUIRE(result.status == AdaptiveV2SelectionStatus::selected);
    CHECK(result.selected_replicas == std::vector<ReplicaID>{6});
    CHECK(result.metadata.required_nonresponsive == 1);
    CHECK(result.metadata.fault_threshold == 2);
    CHECK(result.metadata.quorum == 5);
    CHECK(result.eligible_roots.size() == 5);
    CHECK(std::find(
              result.eligible_roots.begin(),
              result.eligible_roots.end(),
              ReplicaID{6}) == result.eligible_roots.end());
    CHECK(result.snapshot->ranking().size() == 7);
}

TEST_CASE(
    "containment remains recoverable when an unguarded member is nonresponsive",
    "[adaptive-v2][selection][fault-containment][eligibility][n7]")
{
    Fixture fixture;
    fixture.baseline_all();
    auto config = selection_config();
    config.required_nonresponsive = 1;
    AdaptiveV2ByzantineSelection selector(
        *fixture.ledger,
        fixture.members,
        fixture.epoch,
        config);
    REQUIRE(selector.freeze_baseline(fixture.ledger->high_watermark()) ==
            AdaptiveV2SelectionStatus::baseline_frozen);

    // Replica 0 meets the f+1 reporter guard. Replica 6 is separately
    // snapshot-nonresponsive, but one reporter cannot place it in the
    // evidence-driven containment set.
    fixture.persistent_timeouts(0, {2, 3, 4}, 2);
    fixture.persistent_timeouts(6, {1}, 2);

    const auto result = selector.select_through(
        fixture.ledger->high_watermark());

    REQUIRE(result.snapshot != nullptr);
    CHECK(result.status ==
          AdaptiveV2SelectionStatus::insufficient_eligible_roots);
    CHECK(result.selected_replicas.empty());
    CHECK(result.eligible_roots.empty());
    CHECK(std::count_if(
              result.snapshot->ranking().begin(),
              result.snapshot->ranking().end(),
              [](const auto &entry) { return entry.eligible; }) == 5);
    const auto *guarded = candidate(result.eligible_candidates, 0);
    REQUIRE(guarded != nullptr);
    CHECK(guarded->guarded_eligible);
    CHECK(candidate(result.eligible_candidates, 6) == nullptr);

    for (std::size_t attempt = 0; attempt < 4; ++attempt)
        fixture.on_time(1, 6);

    const auto recovered = selector.select_through(
        fixture.ledger->high_watermark());
    REQUIRE(recovered.status == AdaptiveV2SelectionStatus::selected);
    CHECK(recovered.selected_replicas == std::vector<ReplicaID>{0});
    CHECK(recovered.eligible_roots.size() == 5);
    CHECK(std::find(
              recovered.eligible_roots.begin(),
              recovered.eligible_roots.end(),
              ReplicaID{0}) == recovered.eligible_roots.end());
}

TEST_CASE(
    "optimization inherits exact constraints and ranks only fresh live roots",
    "[adaptive-v2][selection][inheritance][optimization][n7]")
{
    Fixture fixture;
    fixture.baseline_live_survivors();
    auto config = selection_config(1, 1);
    config.responsiveness_policy.minimum_attempts = 2;
    AdaptiveV2ByzantineSelection selector(
        *fixture.ledger,
        fixture.members,
        fixture.epoch,
        config);
    CHECK(selector.rank_inheriting_constraints_through(
              fixture.ledger->high_watermark(), {0, 1})
              .status == AdaptiveV2SelectionStatus::invalid_state);
    REQUIRE(selector.freeze_baseline(fixture.ledger->high_watermark()) ==
            AdaptiveV2SelectionStatus::baseline_frozen);
    const auto baseline_cutoff = selector.current_cutoff();
    const auto baseline_trajectory_size =
        selector.score_trajectory().size();

    CHECK(selector.rank_inheriting_constraints_through(
              baseline_cutoff, {0, 1})
              .status == AdaptiveV2SelectionStatus::invalid_cutoff);
    CHECK(selector.current_cutoff() == baseline_cutoff);
    CHECK(selector.score_trajectory().size() ==
          baseline_trajectory_size);

    fixture.on_time(2, 6, 10);
    const auto incomplete_cutoff = fixture.ledger->high_watermark();
    REQUIRE(incomplete_cutoff > baseline_cutoff);
    const auto incomplete =
        selector.rank_inheriting_constraints_through(
            incomplete_cutoff, {0, 1});
    CHECK(incomplete.status ==
          AdaptiveV2SelectionStatus::insufficient_eligible_roots);
    REQUIRE(incomplete.snapshot != nullptr);
    CHECK(incomplete.snapshot->accepted_record_count() == 1);
    CHECK(incomplete.selected_replicas.empty());
    CHECK(incomplete.eligible_roots.empty());
    CHECK(selector.current_cutoff() == baseline_cutoff);
    CHECK(selector.score_trajectory().size() ==
          baseline_trajectory_size);

    fixture.ranked_live_survivor_suffix();
    const auto suffix_cutoff = fixture.ledger->high_watermark();
    const auto suffix_record_count = suffix_cutoff - baseline_cutoff;
    REQUIRE(suffix_cutoff > incomplete_cutoff);
    REQUIRE(suffix_record_count >
            static_cast<std::uint64_t>(
                selector.quorum_metadata().quorum *
                config.responsiveness_policy.minimum_attempts));
    for (const auto &invalid :
         std::vector<std::vector<ReplicaID>>{
             {}, {0}, {0, 0}, {0, 7}})
    {
        const auto rejected =
            selector.rank_inheriting_constraints_through(
                suffix_cutoff, invalid);
        CHECK(rejected.status ==
              AdaptiveV2SelectionStatus::invalid_state);
        CHECK(selector.current_cutoff() == baseline_cutoff);
        CHECK(selector.score_trajectory().size() ==
              baseline_trajectory_size);
    }

    const auto result = selector.rank_inheriting_constraints_through(
        suffix_cutoff, {1, 0});
    REQUIRE(result.status == AdaptiveV2SelectionStatus::selected);
    CHECK(result.constraint_basis ==
          AdaptiveV2SelectionConstraintBasis::
              inherited_consensus_wait_exempt);
    CHECK(result.selected_replicas ==
          std::vector<ReplicaID>{0, 1});
    CHECK(result.eligible_candidates.empty());
    CHECK(result.eligible_roots ==
          std::vector<ReplicaID>{6, 5, 4, 3, 2});
    CHECK(result.metadata.replica_count == 7);
    CHECK(result.metadata.fault_threshold == 2);
    CHECK(result.metadata.quorum == 5);
    CHECK(result.metadata.required_nonresponsive == 2);
    CHECK(result.metadata.baseline_cutoff == baseline_cutoff);
    CHECK(result.metadata.evidence_cutoff == suffix_cutoff);
    REQUIRE(result.snapshot != nullptr);
    CHECK(result.snapshot->accepted_record_count() ==
          suffix_record_count);
    for (const auto constrained :
         std::vector<ReplicaID>{0, 1})
    {
        const auto found = std::find_if(
            result.snapshot->ranking().begin(),
            result.snapshot->ranking().end(),
            [constrained](const auto &entry) {
                return entry.replica_id == constrained;
            });
        REQUIRE(found != result.snapshot->ranking().end());
        CHECK(found->classification ==
              ResponsivenessClass::insufficient_evidence);
        CHECK_FALSE(found->eligible);
    }
    for (const auto live :
         std::vector<ReplicaID>{2, 3, 4, 5, 6})
    {
        const auto found = std::find_if(
            result.snapshot->ranking().begin(),
            result.snapshot->ranking().end(),
            [live](const auto &entry) {
                return entry.replica_id == live;
            });
        REQUIRE(found != result.snapshot->ranking().end());
        CHECK(found->classification == ResponsivenessClass::responsive);
        CHECK(found->eligible);
        CHECK(found->attempt_count >=
              config.responsiveness_policy.minimum_attempts);
    }
    CHECK(selector.current_cutoff() == suffix_cutoff);
    CHECK(selector.score_trajectory().size() ==
          baseline_trajectory_size + suffix_record_count);
    CHECK(std::all_of(
        fixture.ledger->accepted().begin(),
        fixture.ledger->accepted().end(),
        [](const auto &record) {
            return record.observation.outcome ==
                   ResponseOutcome::on_time;
        }));
    CHECK(selector.healthy());
}

TEST_CASE(
    "optimization chooses Q roots when fewer than f leaves are constrained",
    "[adaptive-v2][selection][inheritance][minority][roots]")
{
    Fixture fixture;
    fixture.baseline_all();
    auto config = selection_config();
    config.required_nonresponsive = 1;
    AdaptiveV2ByzantineSelection selector(
        *fixture.ledger,
        fixture.members,
        fixture.epoch,
        config);
    REQUIRE(selector.freeze_baseline(fixture.ledger->high_watermark()) ==
            AdaptiveV2SelectionStatus::baseline_frozen);

    for (const auto target : fixture.members)
    {
        fixture.on_time(
            static_cast<ReplicaID>((target + 1U) % kReplicaCount),
            target,
            static_cast<std::uint64_t>(10 + target));
    }
    const auto result = selector.rank_inheriting_constraints_through(
        fixture.ledger->high_watermark(), {ReplicaID{6}});

    REQUIRE(result.status == AdaptiveV2SelectionStatus::selected);
    CHECK(result.selected_replicas == std::vector<ReplicaID>{6});
    CHECK(result.metadata.required_nonresponsive == 1);
    CHECK(result.eligible_roots.size() == 5);
    CHECK(std::find(
              result.eligible_roots.begin(),
              result.eligible_roots.end(),
              ReplicaID{6}) == result.eligible_roots.end());
}

TEST_CASE(
    "inherited optimization waits for every unconstrained replica",
    "[adaptive-v2][selection][inheritance][minority][complete-evidence][n7]")
{
    Fixture fixture;
    fixture.baseline_all();
    auto config = selection_config();
    config.required_nonresponsive = 1;
    AdaptiveV2ByzantineSelection selector(
        *fixture.ledger,
        fixture.members,
        fixture.epoch,
        config);
    REQUIRE(selector.freeze_baseline(fixture.ledger->high_watermark()) ==
            AdaptiveV2SelectionStatus::baseline_frozen);
    const auto baseline_cutoff = selector.current_cutoff();
    const auto baseline_trajectory_size =
        selector.score_trajectory().size();

    for (const auto target :
         std::vector<ReplicaID>{0, 1, 2, 3, 4})
    {
        fixture.on_time(
            static_cast<ReplicaID>(target + 1U),
            target,
            static_cast<std::uint64_t>(10 + target));
    }
    const auto incomplete_cutoff = fixture.ledger->high_watermark();
    const auto incomplete =
        selector.rank_inheriting_constraints_through(
            incomplete_cutoff, {ReplicaID{6}});

    REQUIRE(incomplete.snapshot != nullptr);
    REQUIRE(incomplete.status ==
            AdaptiveV2SelectionStatus::insufficient_eligible_roots);
    CHECK(incomplete.selected_replicas.empty());
    CHECK(incomplete.eligible_roots.empty());
    CHECK(std::count_if(
              incomplete.snapshot->ranking().begin(),
              incomplete.snapshot->ranking().end(),
              [](const auto &entry) { return entry.eligible; }) == 5);
    const auto missing = std::find_if(
        incomplete.snapshot->ranking().begin(),
        incomplete.snapshot->ranking().end(),
        [](const auto &entry) { return entry.replica_id == 5; });
    REQUIRE(missing != incomplete.snapshot->ranking().end());
    CHECK(missing->classification ==
          ResponsivenessClass::insufficient_evidence);
    CHECK_FALSE(missing->eligible);
    CHECK(selector.current_cutoff() == baseline_cutoff);
    CHECK(selector.score_trajectory().size() ==
          baseline_trajectory_size);
    CHECK(selector.healthy());

    fixture.on_time(0, 5, 15);
    const auto complete_cutoff = fixture.ledger->high_watermark();
    const auto complete =
        selector.rank_inheriting_constraints_through(
            complete_cutoff, {ReplicaID{6}});

    REQUIRE(complete.status == AdaptiveV2SelectionStatus::selected);
    CHECK(complete.selected_replicas ==
          std::vector<ReplicaID>{6});
    CHECK(complete.eligible_roots ==
          std::vector<ReplicaID>{0, 1, 2, 3, 4});
    CHECK(complete.metadata.fault_threshold == 2);
    CHECK(complete.metadata.quorum == 5);
    CHECK(complete.metadata.required_nonresponsive == 1);
    CHECK(selector.current_cutoff() == complete_cutoff);
    CHECK(selector.score_trajectory().size() ==
          baseline_trajectory_size + 6);
    CHECK(selector.healthy());
}

TEST_CASE(
    "inherited optimization excludes an exactly correlated cross-baseline late",
    "[adaptive-v2][selection][inheritance][optimization][late][n7]")
{
    Fixture fixture;
    fixture.baseline_all();
    const auto boundary_timeout = fixture.timeout_v2(2, 0);
    fixture.baseline_live_survivors();
    auto config = selection_config(1, 1);
    config.responsiveness_policy.minimum_attempts = 2;
    AdaptiveV2ByzantineSelection selector(
        *fixture.ledger,
        fixture.members,
        fixture.epoch,
        config);
    REQUIRE(selector.freeze_baseline(fixture.ledger->high_watermark()) ==
            AdaptiveV2SelectionStatus::baseline_frozen);
    const auto baseline_cutoff = selector.current_cutoff();
    const auto baseline_trajectory_size =
        selector.score_trajectory().size();

    fixture.late(boundary_timeout);
    const auto incomplete_cutoff = fixture.ledger->high_watermark();
    REQUIRE(incomplete_cutoff == baseline_cutoff + 1);
    const auto incomplete =
        selector.rank_inheriting_constraints_through(
            incomplete_cutoff, {0, 1});
    CHECK(incomplete.status ==
          AdaptiveV2SelectionStatus::insufficient_eligible_roots);
    REQUIRE(incomplete.snapshot != nullptr);
    CHECK(incomplete.snapshot->accepted_record_count() == 0);
    CHECK(incomplete.selected_replicas.empty());
    CHECK(incomplete.eligible_roots.empty());
    CHECK(selector.current_cutoff() == baseline_cutoff);
    CHECK(selector.score_trajectory().size() ==
          baseline_trajectory_size);
    CHECK(selector.healthy());

    fixture.ranked_live_survivor_suffix();
    const auto suffix_cutoff = fixture.ledger->high_watermark();
    const auto fresh_attempt_count = suffix_cutoff - incomplete_cutoff;
    REQUIRE(fresh_attempt_count >
            static_cast<std::uint64_t>(
                selector.quorum_metadata().quorum *
                config.responsiveness_policy.minimum_attempts));

    const auto result = selector.rank_inheriting_constraints_through(
        suffix_cutoff, {0, 1});
    REQUIRE(result.status == AdaptiveV2SelectionStatus::selected);
    REQUIRE(result.snapshot != nullptr);
    CHECK(result.snapshot->accepted_record_count() ==
          fresh_attempt_count);
    CHECK(suffix_cutoff - baseline_cutoff ==
          fresh_attempt_count + 1);
    CHECK(result.selected_replicas ==
          std::vector<ReplicaID>{0, 1});
    CHECK(result.eligible_roots ==
          std::vector<ReplicaID>{6, 5, 4, 3, 2});
    CHECK(selector.current_cutoff() == suffix_cutoff);
    CHECK(selector.score_trajectory().size() ==
          baseline_trajectory_size +
              (suffix_cutoff - baseline_cutoff));
    CHECK(selector.healthy());
}

TEST_CASE(
    "inherited eligibility waits at forty-nine and fifty-nine then admits sixty",
    "[adaptive-v2][selection][inheritance][eligibility][threshold]"
    "[v40][intentional-red]")
{
    Fixture fixture(1'024);
    fixture.baseline_all();
    auto config = selection_config(1, 1, 1'024);
    config.required_nonresponsive = 1;
    config.responsiveness_policy.minimum_attempts = 60;
    config.responsiveness_policy.attempt_window = 64;
    AdaptiveV2ByzantineSelection selector(
        *fixture.ledger,
        fixture.members,
        fixture.epoch,
        config);
    REQUIRE(selector.freeze_baseline(fixture.ledger->high_watermark()) ==
            AdaptiveV2SelectionStatus::baseline_frozen);
    const auto baseline = selector.current_cutoff();
    const auto add_rounds = [&](std::size_t count) {
        for (std::size_t round = 0; round < count; ++round)
        {
            for (const auto target : fixture.members)
            {
                fixture.on_time(
                    target == 0 ? ReplicaID{1} : ReplicaID{0},
                    target,
                    50);
            }
        }
    };

    add_rounds(49);
    const auto at_forty_nine =
        selector.rank_inheriting_constraints_through(
            fixture.ledger->high_watermark(), {ReplicaID{6}});
    REQUIRE(at_forty_nine.snapshot != nullptr);
    CHECK(at_forty_nine.status ==
          AdaptiveV2SelectionStatus::insufficient_eligible_roots);
    const auto inherited_at_forty_nine = std::find_if(
        at_forty_nine.snapshot->ranking().begin(),
        at_forty_nine.snapshot->ranking().end(),
        [](const auto &entry) { return entry.replica_id == 6; });
    REQUIRE(inherited_at_forty_nine !=
            at_forty_nine.snapshot->ranking().end());
    CHECK(inherited_at_forty_nine->classification ==
          ResponsivenessClass::insufficient_evidence);
    CHECK_FALSE(inherited_at_forty_nine->eligible);
    CHECK(selector.current_cutoff() == baseline);

    add_rounds(10);
    const auto at_fifty_nine =
        selector.rank_inheriting_constraints_through(
            fixture.ledger->high_watermark(), {ReplicaID{6}});
    REQUIRE(at_fifty_nine.snapshot != nullptr);
    CHECK(at_fifty_nine.status ==
          AdaptiveV2SelectionStatus::insufficient_eligible_roots);
    const auto inherited_at_fifty_nine = std::find_if(
        at_fifty_nine.snapshot->ranking().begin(),
        at_fifty_nine.snapshot->ranking().end(),
        [](const auto &entry) { return entry.replica_id == 6; });
    REQUIRE(inherited_at_fifty_nine !=
            at_fifty_nine.snapshot->ranking().end());
    CHECK(inherited_at_fifty_nine->classification ==
          ResponsivenessClass::insufficient_evidence);
    CHECK_FALSE(inherited_at_fifty_nine->eligible);
    CHECK(selector.current_cutoff() == baseline);

    add_rounds(1);
    const auto at_sixty =
        selector.rank_inheriting_constraints_through(
            fixture.ledger->high_watermark(), {ReplicaID{6}});
    REQUIRE(at_sixty.status == AdaptiveV2SelectionStatus::selected);
    CHECK(at_sixty.selected_replicas ==
          std::vector<ReplicaID>{6});
    CHECK(at_sixty.eligible_roots.size() == 5);
    const auto inherited_at_sixty = std::find_if(
        at_sixty.snapshot->ranking().begin(),
        at_sixty.snapshot->ranking().end(),
        [](const auto &entry) { return entry.replica_id == 6; });
    REQUIRE(inherited_at_sixty != at_sixty.snapshot->ranking().end());
    CHECK(inherited_at_sixty->classification ==
          ResponsivenessClass::responsive);
    CHECK(inherited_at_sixty->eligible);
    CHECK(selector.current_cutoff() == fixture.ledger->high_watermark());
}

TEST_CASE(
    "schema-v2 retained timeouts are selection-equivalent to schema-v1",
    "[adaptive-v2][selection][schema][v1][v2][equivalence]"
    "[v40][intentional-red]")
{
    Fixture v1;
    Fixture v2;
    v1.baseline_all();
    v2.baseline_all();
    auto config = selection_config(1, 1);
    config.required_nonresponsive = 1;
    AdaptiveV2ByzantineSelection v1_selector(
        *v1.ledger, v1.members, v1.epoch, config);
    AdaptiveV2ByzantineSelection v2_selector(
        *v2.ledger, v2.members, v2.epoch, config);
    REQUIRE(v1_selector.freeze_baseline(v1.ledger->high_watermark()) ==
            AdaptiveV2SelectionStatus::baseline_frozen);
    REQUIRE(v2_selector.freeze_baseline(v2.ledger->high_watermark()) ==
            AdaptiveV2SelectionStatus::baseline_frozen);
    for (const auto reporter : std::vector<ReplicaID>{0, 1, 2})
    {
        v1.timeout(reporter, 6);
        v2.timeout_v2(reporter, 6);
    }

    const auto first =
        v1_selector.select_through(v1.ledger->high_watermark());
    const auto second =
        v2_selector.select_through(v2.ledger->high_watermark());
    REQUIRE(first.status == AdaptiveV2SelectionStatus::selected);
    REQUIRE(second.status == first.status);
    CHECK(second.selected_replicas == first.selected_replicas);
    CHECK(second.eligible_roots == first.eligible_roots);
    REQUIRE(first.snapshot != nullptr);
    REQUIRE(second.snapshot != nullptr);
    CHECK(second.snapshot->ranking().size() ==
          first.snapshot->ranking().size());
}

TEST_CASE(
    "unsupported schema-v4 evidence is rejected before selection",
    "[adaptive-v2][selection][schema][unsupported][fail-closed]"
    "[v40][intentional-red]")
{
    Fixture fixture;
    fixture.baseline_all();
    AdaptiveV2ByzantineSelection selector(
        *fixture.ledger,
        fixture.members,
        fixture.epoch,
        selection_config());
    REQUIRE(selector.freeze_baseline(fixture.ledger->high_watermark()) ==
            AdaptiveV2SelectionStatus::baseline_frozen);
    auto unsupported = fixture.observation(
        2, 0, ResponseOutcome::timeout);
    unsupported.schema_version = 4;
    const auto accepted_before = fixture.ledger->accepted().size();
    fixture.ledger->ingest(
        AuthenticatedReporter{2}, unsupported);
    CHECK(fixture.ledger->accepted().size() == accepted_before);
    REQUIRE_FALSE(fixture.ledger->rejected().empty());
    CHECK(fixture.ledger->rejected().back().reason ==
          hotstuff::EvidenceRejectionReason::unsupported_schema);
    CHECK(fixture.ledger->healthy());
    CHECK(selector.current_cutoff() < fixture.ledger->high_watermark());
}

TEST_CASE(
    "selection fails closed when fewer than Q responsive roots remain",
    "[adaptive-v2][selection][roots][fail-closed]")
{
    Fixture fixture;
    fixture.baseline_all();
    AdaptiveV2ByzantineSelection selector(
        *fixture.ledger,
        fixture.members,
        fixture.epoch,
        selection_config());
    REQUIRE(selector.freeze_baseline(fixture.ledger->high_watermark()) ==
            AdaptiveV2SelectionStatus::baseline_frozen);

    fixture.persistent_timeouts(0, {2, 3, 4}, 2);
    fixture.persistent_timeouts(1, {2, 3, 4}, 2);
    fixture.persistent_timeouts(2, {3}, 2);
    const auto result =
        selector.select_through(fixture.ledger->high_watermark());

    CHECK(result.status ==
          AdaptiveV2SelectionStatus::insufficient_eligible_roots);
    CHECK(result.selected_replicas.empty());
    CHECK(result.eligible_roots.empty());
    CHECK(result.eligible_candidates.size() == 2);
}

TEST_CASE(
    "selection enforces cutoffs exact epoch and bounded capacity",
    "[adaptive-v2][selection][bounds][epoch]")
{
    SECTION("cutoffs are monotonic after a single baseline freeze")
    {
        Fixture fixture;
        fixture.baseline_all();
        AdaptiveV2ByzantineSelection selector(
            *fixture.ledger,
            fixture.members,
            fixture.epoch,
            selection_config());
        CHECK(selector.select_through(
                  fixture.ledger->high_watermark())
                  .status == AdaptiveV2SelectionStatus::invalid_state);
        REQUIRE(selector.freeze_baseline(
                    fixture.ledger->high_watermark()) ==
                AdaptiveV2SelectionStatus::baseline_frozen);
        CHECK(selector.freeze_baseline(
                  fixture.ledger->high_watermark()) ==
              AdaptiveV2SelectionStatus::invalid_state);
        CHECK(selector.select_through(
                  fixture.ledger->high_watermark())
                  .status == AdaptiveV2SelectionStatus::invalid_cutoff);
        CHECK(selector.select_through(
                  fixture.ledger->high_watermark() + 1)
                  .status == AdaptiveV2SelectionStatus::invalid_cutoff);
        CHECK(selector.healthy());
    }

    SECTION("a later epoch cannot enter the fixed selection prefix")
    {
        Fixture fixture;
        fixture.baseline_all();
        AdaptiveV2ByzantineSelection selector(
            *fixture.ledger,
            fixture.members,
            fixture.epoch,
            selection_config());
        REQUIRE(selector.freeze_baseline(
                    fixture.ledger->high_watermark()) ==
                AdaptiveV2SelectionStatus::baseline_frozen);
        const auto next_epoch = fixture.stage_next_epoch();
        fixture.ingest(fixture.observation(
            2, 0, ResponseOutcome::timeout, next_epoch));

        CHECK(selector.select_through(
                  fixture.ledger->high_watermark())
                  .status == AdaptiveV2SelectionStatus::mixed_epoch);
        CHECK(selector.current_cutoff() == 7);
    }

    SECTION("timeout replay capacity is a terminal fail-closed error")
    {
        Fixture fixture;
        fixture.baseline_all();
        AdaptiveV2ByzantineSelection selector(
            *fixture.ledger,
            fixture.members,
            fixture.epoch,
            selection_config(5, 2, 5));
        REQUIRE(selector.freeze_baseline(
                    fixture.ledger->high_watermark()) ==
                AdaptiveV2SelectionStatus::baseline_frozen);
        fixture.persistent_timeouts(0, {2, 3, 4}, 2);

        CHECK(selector.select_through(
                  fixture.ledger->high_watermark())
                  .status == AdaptiveV2SelectionStatus::capacity_exceeded);
        CHECK_FALSE(selector.healthy());
        CHECK(selector.current_cutoff() == 7);
    }

    SECTION("compensated timeouts still consume total replay capacity")
    {
        Fixture fixture;
        fixture.baseline_all();
        AdaptiveV2ByzantineSelection selector(
            *fixture.ledger,
            fixture.members,
            fixture.epoch,
            selection_config(5, 2, 2));
        REQUIRE(selector.freeze_baseline(
                    fixture.ledger->high_watermark()) ==
                AdaptiveV2SelectionStatus::baseline_frozen);

        for (std::size_t attempt = 0; attempt < 3; ++attempt)
        {
            const auto timed_out = fixture.timeout(2, 0);
            fixture.late(timed_out);
        }

        CHECK(selector.select_through(
                  fixture.ledger->high_watermark())
                  .status == AdaptiveV2SelectionStatus::capacity_exceeded);
        CHECK_FALSE(selector.healthy());
        CHECK(selector.current_cutoff() == 7);
    }

    SECTION("projection audit capacity is a terminal failure")
    {
        Fixture fixture;
        fixture.baseline_all();
        AdaptiveV2ByzantineSelection selector(
            *fixture.ledger,
            fixture.members,
            fixture.epoch,
            selection_config(),
            EvidenceReputationLimits{6});

        CHECK(selector.freeze_baseline(
                  fixture.ledger->high_watermark()) ==
              AdaptiveV2SelectionStatus::projection_failed);
        CHECK_FALSE(selector.healthy());
        CHECK_FALSE(selector.baseline_frozen());
    }
}

TEST_CASE(
    "adaptive-v2 constructor rejects invalid Byzantine and guard bounds",
    "[adaptive-v2][selection][validation][overflow]")
{
    Fixture fixture;

    SECTION("membership must satisfy exact N equals 3f plus 1")
    {
        REQUIRE_THROWS_AS(
            AdaptiveV2ByzantineSelection(
                *fixture.ledger,
                std::vector<ReplicaID>{0, 1, 2, 3, 4, 5},
                fixture.epoch,
                selection_config()),
            std::invalid_argument);
    }

    SECTION("membership must be unique")
    {
        REQUIRE_THROWS_AS(
            AdaptiveV2ByzantineSelection(
                *fixture.ledger,
                std::vector<ReplicaID>{0, 1, 2, 3, 4, 5, 5},
                fixture.epoch,
                selection_config()),
            std::invalid_argument);
    }

    SECTION("the required selection count cannot be zero")
    {
        auto config = selection_config();
        config.required_nonresponsive = 0;
        REQUIRE_THROWS_AS(
            AdaptiveV2ByzantineSelection(
                *fixture.ledger,
                fixture.members,
                fixture.epoch,
                config),
            std::invalid_argument);
    }

    SECTION("the required selection count cannot exceed derived f")
    {
        auto config = selection_config();
        config.required_nonresponsive = 3;
        REQUIRE_THROWS_AS(
            AdaptiveV2ByzantineSelection(
                *fixture.ledger,
                fixture.members,
                fixture.epoch,
                config),
            std::invalid_argument);
    }

    SECTION("K cannot exceed bounded replay capacity")
    {
        auto config = selection_config();
        config.minimum_timeouts_per_reporter =
            std::numeric_limits<std::uint32_t>::max();
        REQUIRE_THROWS_AS(
            AdaptiveV2ByzantineSelection(
                *fixture.ledger,
                fixture.members,
                fixture.epoch,
                config),
            std::invalid_argument);
    }

    SECTION("replay capacity cannot overflow snapshot evidence bounds")
    {
        auto config = selection_config();
        config.maximum_post_baseline_timeout_attempts =
            hotstuff::kMaximumAdaptationEvidenceRecords + 1U;
        REQUIRE_THROWS_AS(
            AdaptiveV2ByzantineSelection(
                *fixture.ledger,
                fixture.members,
                fixture.epoch,
                config),
            std::invalid_argument);
    }

    SECTION("the exact epoch digest is mandatory")
    {
        auto missing_epoch = fixture.epoch;
        missing_epoch.epoch_digest = {};
        REQUIRE_THROWS_AS(
            AdaptiveV2ByzantineSelection(
                *fixture.ledger,
                fixture.members,
                missing_epoch,
                selection_config()),
            std::invalid_argument);
    }
}
