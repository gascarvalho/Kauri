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
using hotstuff::AdaptiveV2SelectionStatus;
using hotstuff::AdaptiveV2TimeoutAuditBasis;
using hotstuff::AdaptiveV2FaultContainmentCoverageStatus;
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

    explicit Fixture(std::size_t accepted_capacity = 256)
    {
        const auto &definition = epochs.stage(
            epoch_input(0, uint256_t{}, 15),
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

    void late(
        const ResponseObservation &timeout_observation,
        std::uint64_t response_duration_us = 150)
    {
        auto value = timeout_observation;
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
    const auto boundary_timeout = fixture.timeout(2, 0);
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
