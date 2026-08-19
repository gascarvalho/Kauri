#include <algorithm>
#include <cstdint>
#include <fstream>
#include <functional>
#include <iterator>
#include <limits>
#include <set>
#include <stdexcept>
#include <string>
#include <utility>
#include <variant>
#include <vector>

#include "catch.hpp"
#include "hotstuff/adaptive_v2_reporting_outbox.h"
#include "hotstuff/adaptive_v2_response_evidence.h"

#ifndef KAURI_PROJECT_SOURCE_DIR
#error "KAURI_PROJECT_SOURCE_DIR must name the repository root"
#endif

namespace
{

using hotstuff::AdaptiveV2ResponseEvidenceBridge;
using hotstuff::AdaptiveV2ResponseEvidenceLimits;
using hotstuff::AdaptiveV2CrossCommitRetentionAdmissionPolicy;
using hotstuff::AdaptiveV2CrossCommitRetentionAdmissionStatus;
using hotstuff::AcceptedEvidenceRecord;
using hotstuff::AdaptationEpochId;
using hotstuff::AdaptiveV2ReportingAttemptStatus;
using hotstuff::AdaptiveV2ReportingDeliveryResult;
using hotstuff::AdaptiveV2ReportingEnqueueStatus;
using hotstuff::AdaptiveV2ReportingOutbox;
using hotstuff::AdaptiveV2ReportingOutboxConfig;
using hotstuff::AdaptiveV2ReportingReleaseStatus;
using hotstuff::AdaptiveV2ReportingStream;
using hotstuff::AdaptiveV2ReportingTransitionStatus;
using hotstuff::AdaptiveV2EpochChangeIdentity;
using hotstuff::ConfigurationId;
using hotstuff::DataStream;
using hotstuff::EvidenceReportEnvelope;
using hotstuff::EvidenceDeadlineResult;
using hotstuff::EvidenceTransportResult;
using hotstuff::ExpectedMessageType;
using hotstuff::NormalProposalRuntimeInitialized;
using hotstuff::ProposalCommitted;
using hotstuff::ProposalKey;
using hotstuff::ProposalLifecycleFact;
using hotstuff::ProposalTreeSnapshot;
using hotstuff::ReplicaID;
using hotstuff::ResponseOutcome;
using hotstuff::uint256_t;

constexpr std::uint64_t kStartNs = 1'000'000;
constexpr std::uint64_t kDeadlineUs = 100;
constexpr std::uint64_t kNanosecondsPerMicrosecond = 1000;

uint256_t digest(const std::string &label)
{
    return DataStream(label).get_hash();
}

ProposalKey proposal(const std::string &label = "adaptive-v2-response")
{
    return ProposalKey{
        ConfigurationId{7, 3, digest(label + "-epoch")},
        digest(label + "-block")};
}

AdaptiveV2EpochChangeIdentity convergence_identity(
    const std::string &label)
{
    return AdaptiveV2EpochChangeIdentity{
        7,
        digest(label + "-predecessor"),
        8,
        digest(label + "-successor"),
        digest(label + "-payload"),
        10,
        digest(label + "-command-block"),
        2,
        12};
}

ProposalTreeSnapshot response_tree()
{
    ProposalTreeSnapshot tree;
    tree.local_replica = 0;
    tree.root = 0;
    tree.direct_children = {1, 2, 5};
    tree.assigned_subtree = {0, 1, 3, 4, 2, 5};
    tree.child_subtrees = {
        {1, {1, 3, 4}},
        {2, {2}},
        {5, {5}}};
    tree.required_subtree = {0, 1, 2, 3, 4};
    tree.optional_subtree = {5};
    tree.required_child_subtrees = {
        {1, {1, 3, 4}},
        {2, {2}},
        {5, {}}};
    tree.fanout = 3;
    return tree;
}

AdaptiveV2ResponseEvidenceLimits limits(
    std::size_t maximum_handles = 16,
    std::size_t maximum_pending_reports = 16,
    std::size_t maximum_retained_facts = 16)
{
    AdaptiveV2ResponseEvidenceLimits result;
    result.attempts.maximum_attempts = 16;
    result.attempts.maximum_signers_per_response = 16;
    result.attempts.maximum_generation =
        std::numeric_limits<std::uint64_t>::max();
    result.reporter.maximum_pending_reports = maximum_pending_reports;
    result.wire.maximum_payload_bytes = 64 * 1024;
    result.wire.maximum_observations = 16;
    result.wire.maximum_signers_per_observation = 16;
    result.maximum_handles = maximum_handles;
    result.maximum_retained_facts = maximum_retained_facts;
    result.maximum_late_compensations = maximum_handles;
    return result;
}

AdaptiveV2ReportingOutboxConfig reporting_config(
    std::size_t maximum_pending_reports = 1)
{
    AdaptiveV2ReportingOutboxConfig config;
    config.source_replica_id = 0;
    config.limits.maximum_pending_reports = maximum_pending_reports;
    config.limits.maximum_pending_payload_bytes = 64 * 1024;
    config.limits.maximum_delivery_attempts = 3;
    config.limits.initial_retry_backoff_ns = 1;
    config.limits.maximum_retry_backoff_ns = 8;
    return config;
}

void deliver_and_release(
    AdaptiveV2ReportingOutbox &outbox,
    std::uint64_t now)
{
    const auto attempt = outbox.begin_delivery(now);
    REQUIRE(attempt.status == AdaptiveV2ReportingAttemptStatus::started);
    REQUIRE(attempt.token.has_value());
    REQUIRE(attempt.report != nullptr);
    const auto report_id = attempt.report->report_id;
    REQUIRE(outbox.acknowledge_delivery(
                *attempt.token,
                AdaptiveV2ReportingDeliveryResult::delivered,
                now) ==
            AdaptiveV2ReportingTransitionStatus::delivered);
    REQUIRE(outbox.release_terminal(report_id) ==
            AdaptiveV2ReportingReleaseStatus::released);
}

std::uint64_t after_us(std::uint64_t microseconds)
{
    return kStartNs + microseconds * kNanosecondsPerMicrosecond;
}

AcceptedEvidenceRecord retained_timeout_record(
    std::uint64_t ingestion_sequence,
    ReplicaID reporter,
    ReplicaID target,
    ExpectedMessageType message_type,
    const AdaptationEpochId &epoch,
    const std::string &label)
{
    hotstuff::ResponseObservation observation;
    observation.schema_version =
        hotstuff::kResponseObservationSchemaVersionV2;
    observation.reporter_id = reporter;
    observation.observed_replica_id = target;
    observation.configuration = hotstuff::ConfigurationId{
        epoch.epoch_number,
        static_cast<std::uint32_t>(target % 5),
        epoch.epoch_digest};
    observation.block_hash = digest(label + "-block");
    observation.expected_message_type = message_type;
    observation.outcome = ResponseOutcome::timeout;
    observation.response_duration_us = 0;
    observation.deadline_duration_us = 100;
    observation.attempt_start_monotonic_ns =
        1'000'000 + ingestion_sequence * 1'000'000;
    observation.reporter_local_commit_monotonic_ns =
        observation.attempt_start_monotonic_ns + 40'000;
    observation.reporter_monotonic_ns =
        observation.attempt_start_monotonic_ns + 100'000;
    observation.reporter_sequence = ingestion_sequence;
    observation.observation_id =
        hotstuff::compute_response_observation_id(
            observation.attempt_identity());
    REQUIRE(reporter != target);
    REQUIRE(hotstuff::valid_response_observation_retention_witness(
        observation));
    return {ingestion_sequence, std::move(observation)};
}

AcceptedEvidenceRecord retained_late_record(
    std::uint64_t ingestion_sequence,
    const AcceptedEvidenceRecord &timeout)
{
    auto observation = timeout.observation;
    observation.schema_version =
        hotstuff::kResponseObservationSchemaVersionV1;
    observation.outcome = ResponseOutcome::late;
    observation.response_duration_us =
        observation.deadline_duration_us + 1;
    observation.reporter_monotonic_ns += 1'000'000;
    observation.reporter_sequence = ingestion_sequence;
    observation.signer_set = {observation.observed_replica_id};
    observation.attempt_start_monotonic_ns = 0;
    observation.reporter_local_commit_monotonic_ns = 0;
    return {ingestion_sequence, std::move(observation)};
}

std::string source(const std::string &relative_path)
{
    std::ifstream input(
        std::string(KAURI_PROJECT_SOURCE_DIR) + "/" + relative_path);
    REQUIRE(input.good());
    return std::string(
        std::istreambuf_iterator<char>(input),
        std::istreambuf_iterator<char>());
}

std::string function_slice(
    const std::string &implementation,
    const std::string &start,
    const std::string &next)
{
    const auto begin = implementation.find(start);
    REQUIRE(begin != std::string::npos);
    const auto end = implementation.find(next, begin + start.size());
    REQUIRE(end != std::string::npos);
    return implementation.substr(begin, end - begin);
}

class ManualEvidenceDeadlineScheduler final
{
public:
    struct Job
    {
        std::uint64_t duration_us{0};
        hotstuff::EvidenceDeadlineCallback deadline;
        hotstuff::EvidenceDeadlineFailureCallback failure;
        bool active{true};
    };

    void bind(AdaptiveV2ResponseEvidenceBridge &bridge)
    {
        bridge.bind_deadline_scheduler(
            [this](
                const ProposalKey &,
                std::uint64_t duration_us,
                hotstuff::EvidenceDeadlineCallback deadline,
                hotstuff::EvidenceDeadlineFailureCallback failure) {
                const auto index = jobs.size();
                jobs.push_back(Job{
                    duration_us,
                    std::move(deadline),
                    std::move(failure),
                    true});
                return [this, index] {
                    if (index < jobs.size())
                        jobs[index].active = false;
                };
            });
    }

    bool fire(std::size_t index, std::uint64_t now_ns)
    {
        if (index >= jobs.size() || !jobs[index].active)
            return false;
        jobs[index].active = false;
        auto callback = std::move(jobs[index].deadline);
        callback(now_ns);
        return true;
    }

    bool fail(std::size_t index)
    {
        if (index >= jobs.size() || !jobs[index].active)
            return false;
        jobs[index].active = false;
        auto callback = std::move(jobs[index].failure);
        callback();
        return true;
    }

    std::size_t active_jobs() const
    {
        return static_cast<std::size_t>(std::count_if(
            jobs.begin(), jobs.end(), [](const Job &job) {
                return job.active;
            }));
    }

    std::vector<Job> jobs;
};

} // namespace

TEST_CASE(
    "adaptive-v2 bridge arms exact direct children and reports accepted responses",
    "[adaptive-v2][response-evidence][bridge]")
{
    AdaptiveV2ResponseEvidenceBridge bridge(0, limits());
    const auto key = proposal();
    const auto tree = response_tree();

    REQUIRE(bridge.arm(key, tree, kStartNs, kDeadlineUs));
    const auto armed = bridge.diagnostics();
    CHECK(armed.active_handles == 3);
    CHECK(armed.armed_attempts == 3);
    CHECK(armed.pending_reports == 0);
    CHECK(armed.healthy);

    REQUIRE(bridge.record_verified_response(
        key,
        2,
        ExpectedMessageType::direct_vote,
        {2},
        after_us(40)));
    REQUIRE(bridge.front() != nullptr);
    CHECK(bridge.front()->envelope.observation.observed_replica_id == 2);
    CHECK(bridge.front()->envelope.observation.expected_message_type ==
          ExpectedMessageType::direct_vote);
    CHECK(bridge.front()->envelope.observation.outcome ==
          ResponseOutcome::on_time);
    CHECK(bridge.front()->envelope.observation.signer_set ==
          std::vector<hotstuff::ReplicaID>{2});

    std::vector<EvidenceReportEnvelope> delivered;
    bridge.bind_transport(
        [&delivered](const EvidenceReportEnvelope &envelope) {
            delivered.push_back(envelope);
            return EvidenceTransportResult::accepted;
        });
    REQUIRE(delivered.size() == 1);
    CHECK(bridge.flush() == 0);
    CHECK(bridge.front() == nullptr);
    CHECK(bridge.diagnostics().pending_reports == 0);
}

TEST_CASE(
    "duplicate on-time verified response is an idempotent no-op",
    "[adaptive-v2][response-evidence][duplicate][on-time]")
{
    ManualEvidenceDeadlineScheduler scheduler;
    AdaptiveV2ResponseEvidenceBridge bridge(0, limits());
    scheduler.bind(bridge);
    std::vector<EvidenceDeadlineResult> results;
    bridge.bind_deadline_result_callback(
        [&results](const ProposalKey &, EvidenceDeadlineResult result) {
            results.push_back(result);
        });
    std::vector<EvidenceReportEnvelope> delivered;
    bridge.bind_transport(
        [&delivered](const EvidenceReportEnvelope &envelope) {
            delivered.push_back(envelope);
            return EvidenceTransportResult::accepted;
        });

    const auto key = proposal("duplicate-on-time-response");
    REQUIRE(bridge.arm_with_deadline(
        key, response_tree(), kStartNs, kDeadlineUs));
    REQUIRE(bridge.record_verified_response(
        key,
        1,
        ExpectedMessageType::aggregate_relay,
        {1, 3},
        after_us(20)));
    REQUIRE(delivered.size() == 1);

    CHECK_FALSE(bridge.record_verified_response(
        key,
        1,
        ExpectedMessageType::aggregate_relay,
        {1, 3, 4},
        after_us(21)));
    auto diagnostics = bridge.diagnostics();
    CHECK(delivered.size() == 1);
    CHECK(diagnostics.response_facts == 1);
    CHECK(diagnostics.idempotent_duplicate_responses == 1);
    CHECK(diagnostics.rejected_operations == 0);
    CHECK(diagnostics.deadline_delivery_failures == 0);
    CHECK(diagnostics.healthy);
    CHECK(results.empty());

    CHECK_FALSE(bridge.record_verified_response(
        key,
        1,
        ExpectedMessageType::direct_vote,
        {1},
        after_us(22)));
    CHECK_FALSE(bridge.record_verified_response(
        key,
        99,
        ExpectedMessageType::aggregate_relay,
        {99},
        after_us(22)));
    diagnostics = bridge.diagnostics();
    CHECK(diagnostics.rejected_operations == 2);
    CHECK(diagnostics.deadline_delivery_failures == 0);
    CHECK(diagnostics.healthy);

    REQUIRE(bridge.close_consensus_context(key));
    REQUIRE(scheduler.fire(0, after_us(kDeadlineUs)));
    CHECK(results ==
          std::vector<EvidenceDeadlineResult>{
              EvidenceDeadlineResult::evidence_accepted});
    CHECK(bridge.diagnostics().healthy);
}

TEST_CASE(
    "adaptive-v2 commit cleanup preserves unanswered attempts until their deadline",
    "[adaptive-v2][response-evidence][deadline][commit]")
{
    ManualEvidenceDeadlineScheduler scheduler;
    AdaptiveV2ResponseEvidenceBridge bridge(0, limits());
    scheduler.bind(bridge);
    std::vector<EvidenceDeadlineResult> deadline_results;
    bridge.bind_deadline_result_callback(
        [&deadline_results](const ProposalKey &,
                            EvidenceDeadlineResult result) {
            deadline_results.push_back(result);
        });
    std::vector<EvidenceReportEnvelope> delivered;
    bridge.bind_transport(
        [&delivered](const EvidenceReportEnvelope &envelope) {
            delivered.push_back(envelope);
            return EvidenceTransportResult::accepted;
        });

    const auto key = proposal("commit-before-deadline");
    REQUIRE(bridge.arm_with_deadline(
        key, response_tree(), kStartNs, kDeadlineUs));
    REQUIRE(scheduler.jobs.size() == 1);
    CHECK(scheduler.jobs[0].duration_us == kDeadlineUs);
    CHECK(bridge.diagnostics().active_deadlines == 1);
    CHECK(bridge.diagnostics().active_handles == 3);
    CHECK(bridge.should_defer_commit_report(key));

    REQUIRE(bridge.close_consensus_context(key));
    CHECK(bridge.diagnostics().active_handles == 3);
    CHECK(delivered.empty());

    REQUIRE(scheduler.fire(0, after_us(kDeadlineUs)));
    CHECK(deadline_results ==
          std::vector<EvidenceDeadlineResult>{
              EvidenceDeadlineResult::evidence_accepted});
    REQUIRE(delivered.size() == 2);
    CHECK(delivered[0].observation.observed_replica_id == 1);
    CHECK(delivered[1].observation.observed_replica_id == 2);
    CHECK(delivered[0].observation.outcome == ResponseOutcome::timeout);
    CHECK(delivered[1].observation.outcome == ResponseOutcome::timeout);
    const auto diagnostics = bridge.diagnostics();
    CHECK(diagnostics.active_deadlines == 0);
    CHECK(diagnostics.active_handles == 0);
    CHECK(diagnostics.timeout_facts == 2);
    CHECK(diagnostics.retired_attempts == 3);
    CHECK(diagnostics.fired_deadlines == 1);
    CHECK(diagnostics.closed_contexts_retained == 1);
    CHECK_FALSE(bridge.should_defer_commit_report(key));
    CHECK(diagnostics.healthy);
}

TEST_CASE(
    "repair-only retention mode emits schema-v2 timeout facts after local commit",
    "[adaptive-v2][response-evidence][v2][retention][intentional-red]")
{
    ManualEvidenceDeadlineScheduler scheduler;
    AdaptiveV2ResponseEvidenceBridge bridge(0, limits());
    REQUIRE(bridge.enable_cross_commit_retention_v2());
    scheduler.bind(bridge);
    std::vector<EvidenceReportEnvelope> delivered;
    bridge.bind_transport(
        [&delivered](const EvidenceReportEnvelope &envelope) {
            delivered.push_back(envelope);
            return EvidenceTransportResult::accepted;
        });

    const auto key = proposal("retained-v2-timeout");
    REQUIRE(bridge.arm_with_deadline(
        key, response_tree(), kStartNs, kDeadlineUs));
    REQUIRE(bridge.record_reporter_local_commit(key, after_us(40)));
    REQUIRE(bridge.record_reporter_local_commit(key, after_us(40)));
    REQUIRE(bridge.close_consensus_context(key));
    REQUIRE(scheduler.fire(0, after_us(kDeadlineUs)));

    REQUIRE(delivered.size() == 2);
    for (const auto &envelope : delivered)
    {
        const auto &observation = envelope.observation;
        CHECK(observation.schema_version ==
              hotstuff::kResponseObservationSchemaVersionV2);
        CHECK(observation.outcome == ResponseOutcome::timeout);
        CHECK(observation.attempt_start_monotonic_ns == kStartNs);
        CHECK(observation.reporter_local_commit_monotonic_ns ==
              after_us(40));
        CHECK(observation.reporter_local_commit_monotonic_ns <
              observation.attempt_start_monotonic_ns +
                  observation.deadline_duration_us * 1'000);
    }
}

TEST_CASE(
    "public v3 enablement stamps exact timeout attempt identity",
    "[adaptive-v2][response-evidence][v3][enablement]")
{
    AdaptiveV2ResponseEvidenceBridge bridge(0, limits());
    REQUIRE(bridge.enable_exact_timeout_attempt_evidence_v3());
    REQUIRE(bridge.enable_exact_timeout_attempt_evidence_v3());
    const auto key = proposal("v3-public-enable");
    REQUIRE(bridge.arm(key, response_tree(), kStartNs, kDeadlineUs));
    CHECK(bridge.record_timeouts(key, {1}, after_us(kDeadlineUs)) == 1);
    REQUIRE(bridge.front() != nullptr);
    const auto &observation = bridge.front()->envelope.observation;
    CHECK(observation.schema_version ==
          hotstuff::kResponseObservationSchemaVersionV3);
    CHECK(observation.attempt_start_monotonic_ns == kStartNs);
    CHECK(observation.observation_id ==
          hotstuff::compute_response_observation_id(observation));
    CHECK(hotstuff::valid_response_observation_retention_witness(
        observation));
}

TEST_CASE(
    "conflicting reporter-local commit timestamp fails retention closed",
    "[adaptive-v2][response-evidence][v2][retention][conflict]"
    "[intentional-red]")
{
    ManualEvidenceDeadlineScheduler scheduler;
    AdaptiveV2ResponseEvidenceBridge bridge(0, limits());
    REQUIRE(bridge.enable_cross_commit_retention_v2());
    scheduler.bind(bridge);
    const auto key = proposal("conflicting-retained-commit");
    REQUIRE(bridge.arm_with_deadline(
        key, response_tree(), kStartNs, kDeadlineUs));
    REQUIRE(bridge.record_reporter_local_commit(key, after_us(40)));
    CHECK_FALSE(
        bridge.record_reporter_local_commit(key, after_us(41)));
    CHECK_FALSE(bridge.diagnostics().healthy);
    CHECK(bridge.diagnostics().rejected_operations == 1);
}

TEST_CASE(
    "retention mode keeps ordinary timeout schema-v1 without a local commit",
    "[adaptive-v2][response-evidence][v2][retention][negative]"
    "[intentional-red]")
{
    AdaptiveV2ResponseEvidenceBridge bridge(0, limits());
    REQUIRE(bridge.enable_cross_commit_retention_v2());
    const auto key = proposal("ordinary-v1-timeout");
    REQUIRE(bridge.arm(key, response_tree(), kStartNs, kDeadlineUs));
    CHECK(bridge.record_timeouts(key, {1}, after_us(kDeadlineUs)) == 1);
    REQUIRE(bridge.front() != nullptr);
    const auto &observation = bridge.front()->envelope.observation;
    CHECK(observation.schema_version ==
          hotstuff::kResponseObservationSchemaVersionV1);
    CHECK(observation.attempt_start_monotonic_ns == 0);
    CHECK(observation.reporter_local_commit_monotonic_ns == 0);
}

TEST_CASE(
    "cycle-one retention admission selects outstanding target facts canonically",
    "[adaptive-v2][response-evidence][retention][admission][v40]"
    "[intentional-red]")
{
    const AdaptationEpochId epoch{
        1, digest("retention-admission-epoch-one")};
    const std::vector<ReplicaID> actors{1, 7, 8, 12, 16, 19, 20};
    std::vector<AcceptedEvidenceRecord> accepted;
    accepted.push_back(retained_timeout_record(
        1, 2, 1, ExpectedMessageType::direct_vote,
        epoch, "actor-1-direct"));
    accepted.push_back(retained_timeout_record(
        2, 3, 1, ExpectedMessageType::aggregate_relay,
        epoch, "actor-1-aggregate-earliest"));
    accepted.push_back(retained_timeout_record(
        3, 4, 1, ExpectedMessageType::aggregate_relay,
        epoch, "actor-1-aggregate-later"));
    accepted.push_back(retained_timeout_record(
        4, 9, 7, ExpectedMessageType::direct_vote,
        epoch, "actor-7"));
    accepted.push_back(retained_timeout_record(
        5, 10, 8, ExpectedMessageType::direct_vote,
        epoch, "actor-8"));
    accepted.push_back(retained_timeout_record(
        6, 14, 12, ExpectedMessageType::direct_vote,
        epoch, "actor-12"));
    accepted.push_back(retained_timeout_record(
        7, 18, 16, ExpectedMessageType::direct_vote,
        epoch, "actor-16"));
    accepted.push_back(retained_timeout_record(
        8, 21, 19, ExpectedMessageType::direct_vote,
        epoch, "actor-19"));
    accepted.push_back(retained_timeout_record(
        9, 22, 20, ExpectedMessageType::direct_vote,
        epoch, "actor-20"));
    for (const auto &record : accepted)
        CHECK(record.observation.reporter_id !=
              record.observation.observed_replica_id);

    SECTION("six of seven and a post-cutoff seventh fact still wait")
    {
        const auto incomplete = hotstuff::
            select_adaptive_v2_cross_commit_retention_admission(
                accepted, epoch, 8, actors);
        CHECK(incomplete.status ==
              AdaptiveV2CrossCommitRetentionAdmissionStatus::incomplete);
        CHECK(incomplete.admitted_observation_ids.empty());
    }

    SECTION("older epoch rows do not hide a complete epoch-one prefix")
    {
        const AdaptationEpochId epoch_zero{
            0, digest("retention-admission-epoch-zero")};
        std::vector<AcceptedEvidenceRecord> mixed;
        mixed.push_back(retained_timeout_record(
            1, 30, 29, ExpectedMessageType::aggregate_relay,
            epoch_zero, "old-epoch-row"));
        std::uint64_t sequence = 2;
        for (const auto actor : actors)
        {
            mixed.push_back(retained_timeout_record(
                sequence,
                static_cast<ReplicaID>(actor + 2),
                actor,
                actor == actors.front()
                    ? ExpectedMessageType::aggregate_relay
                    : ExpectedMessageType::direct_vote,
                epoch,
                "mixed-current-" + std::to_string(actor)));
            ++sequence;
        }
        const auto mixed_ready = hotstuff::
            select_adaptive_v2_cross_commit_retention_admission(
                mixed, epoch, 8, actors);
        CHECK(mixed_ready.status ==
              AdaptiveV2CrossCommitRetentionAdmissionStatus::ready);
    }

    SECTION("accepted gaps and a trailing rejected high-watermark stay ready")
    {
        std::vector<AcceptedEvidenceRecord> gapped;
        std::uint64_t sequence = 1;
        for (const auto actor : actors)
        {
            gapped.push_back(retained_timeout_record(
                sequence,
                static_cast<ReplicaID>(actor + 2),
                actor,
                actor == actors.front()
                    ? ExpectedMessageType::aggregate_relay
                    : ExpectedMessageType::direct_vote,
                epoch,
                "gapped-current-" + std::to_string(actor)));
            sequence += 2;
        }

        // EvidenceLedger::high_watermark() also counts rejected ingests, so
        // the manager cutoff may exceed the final accepted sequence.
        const auto gapped_ready = hotstuff::
            select_adaptive_v2_cross_commit_retention_admission(
                gapped, epoch, sequence, actors);
        CHECK(gapped_ready.status ==
              AdaptiveV2CrossCommitRetentionAdmissionStatus::ready);
        CHECK(gapped_ready.evidence_cutoff == sequence);
    }

    const auto ready = hotstuff::
        select_adaptive_v2_cross_commit_retention_admission(
            accepted, epoch, 9, actors);
    REQUIRE(ready.status ==
            AdaptiveV2CrossCommitRetentionAdmissionStatus::ready);
    CHECK(ready.evidence_cutoff == 9);
    CHECK(ready.responsive_degraded_actor_ids == actors);
    REQUIRE(ready.admitted_observation_ids.size() == actors.size());
    CHECK(ready.admitted_observation_ids.front() ==
          accepted[1].observation.observation_id);
    CHECK(ready.admitted_observation_ids.front() !=
          accepted[0].observation.observation_id);
    CHECK(ready.admitted_observation_ids.front() !=
          accepted[2].observation.observation_id);

    SECTION("facts retained before the cycle-one baseline remain admissible")
    {
        // A manager baseline frozen at cutoff 9 must not erase the Epoch1
        // retention prefix. The replay API deliberately has no lower bound.
        CHECK(ready.status ==
              AdaptiveV2CrossCommitRetentionAdmissionStatus::ready);
    }

    SECTION("timeout to late before cutoff removes the target witness")
    {
        accepted.push_back(retained_late_record(10, accepted[4]));
        const auto after_late = hotstuff::
            select_adaptive_v2_cross_commit_retention_admission(
                accepted, epoch, 10, actors);
        CHECK(after_late.status ==
              AdaptiveV2CrossCommitRetentionAdmissionStatus::incomplete);

        const auto held_cutoff = hotstuff::
            select_adaptive_v2_cross_commit_retention_admission(
                accepted, epoch, 9, actors);
        REQUIRE(held_cutoff.status ==
                AdaptiveV2CrossCommitRetentionAdmissionStatus::ready);
        CHECK(held_cutoff.admitted_observation_ids ==
              ready.admitted_observation_ids);
    }

    SECTION("a malformed schema-v2 late transition fails replay closed")
    {
        auto malformed_late = retained_late_record(10, accepted[4]);
        malformed_late.observation.schema_version =
            hotstuff::kResponseObservationSchemaVersionV2;
        accepted.push_back(std::move(malformed_late));
        const auto invalid = hotstuff::
            select_adaptive_v2_cross_commit_retention_admission(
                accepted, epoch, 10, actors);
        CHECK(invalid.status ==
              AdaptiveV2CrossCommitRetentionAdmissionStatus::invalid);
    }
}

TEST_CASE(
    "cycle-one aggregate retention admission waits for every actor",
    "[adaptive-v2][response-evidence][retention][admission][v42]"
    "[intentional-red]")
{
    const AdaptationEpochId epoch{
        1, digest("aggregate-retention-admission-epoch-one")};
    const std::vector<ReplicaID> actors{7, 8};
    std::vector<AcceptedEvidenceRecord> accepted;
    accepted.push_back(retained_timeout_record(
        1, 3, 7, ExpectedMessageType::direct_vote,
        epoch, "actor-7-direct"));
    accepted.push_back(retained_timeout_record(
        2, 4, 8, ExpectedMessageType::aggregate_relay,
        epoch, "actor-8-aggregate"));

    const auto legacy_ready = hotstuff::
        select_adaptive_v2_cross_commit_retention_admission(
            accepted,
            epoch,
            2,
            actors,
            AdaptiveV2CrossCommitRetentionAdmissionPolicy::
                one_per_actor_with_global_aggregate_v1);
    REQUIRE(legacy_ready.status ==
            AdaptiveV2CrossCommitRetentionAdmissionStatus::ready);
    REQUIRE(legacy_ready.admitted_observation_ids.size() == 2);
    CHECK(legacy_ready.admitted_observation_ids.front() ==
          accepted.front().observation.observation_id);
    const auto default_legacy_ready = hotstuff::
        select_adaptive_v2_cross_commit_retention_admission(
            accepted, epoch, 2, actors);
    CHECK(default_legacy_ready.status == legacy_ready.status);
    CHECK(default_legacy_ready.admitted_observation_ids ==
          legacy_ready.admitted_observation_ids);

    const auto aggregate_incomplete = hotstuff::
        select_adaptive_v2_cross_commit_retention_admission(
            accepted,
            epoch,
            2,
            actors,
            AdaptiveV2CrossCommitRetentionAdmissionPolicy::
                aggregate_relay_per_actor_v1);
    CHECK(aggregate_incomplete.status ==
          AdaptiveV2CrossCommitRetentionAdmissionStatus::incomplete);
    CHECK(aggregate_incomplete.admitted_observation_ids.empty());

    accepted.push_back(retained_timeout_record(
        3, 5, 7, ExpectedMessageType::aggregate_relay,
        epoch, "actor-7-aggregate"));
    const auto aggregate_ready = hotstuff::
        select_adaptive_v2_cross_commit_retention_admission(
            accepted,
            epoch,
            3,
            actors,
            AdaptiveV2CrossCommitRetentionAdmissionPolicy::
                aggregate_relay_per_actor_v1);
    REQUIRE(aggregate_ready.status ==
            AdaptiveV2CrossCommitRetentionAdmissionStatus::ready);
    REQUIRE(aggregate_ready.admitted_observation_ids.size() == 2);
    CHECK(aggregate_ready.admitted_observation_ids.front() ==
          accepted.back().observation.observation_id);
    CHECK(aggregate_ready.admitted_observation_ids.back() ==
          accepted[1].observation.observation_id);

    accepted.push_back(retained_late_record(4, accepted.back()));
    const auto aggregate_cancelled = hotstuff::
        select_adaptive_v2_cross_commit_retention_admission(
            accepted,
            epoch,
            4,
            actors,
            AdaptiveV2CrossCommitRetentionAdmissionPolicy::
                aggregate_relay_per_actor_v1);
    CHECK(aggregate_cancelled.status ==
          AdaptiveV2CrossCommitRetentionAdmissionStatus::incomplete);
    CHECK(aggregate_cancelled.admitted_observation_ids.empty());

    std::vector<AcceptedEvidenceRecord> malformed;
    malformed.push_back(retained_timeout_record(
        2, 5, 7, ExpectedMessageType::aggregate_relay,
        epoch, "malformed-later"));
    malformed.push_back(retained_timeout_record(
        1, 4, 8, ExpectedMessageType::aggregate_relay,
        epoch, "malformed-earlier"));
    const auto invalid = hotstuff::
        select_adaptive_v2_cross_commit_retention_admission(
            malformed,
            epoch,
            2,
            actors,
            AdaptiveV2CrossCommitRetentionAdmissionPolicy::
                aggregate_relay_per_actor_v1);
    CHECK(invalid.status ==
          AdaptiveV2CrossCommitRetentionAdmissionStatus::invalid);

    const auto unknown_policy = hotstuff::
        select_adaptive_v2_cross_commit_retention_admission(
            malformed,
            epoch,
            2,
            actors,
            static_cast<AdaptiveV2CrossCommitRetentionAdmissionPolicy>(0));
    CHECK(unknown_policy.status ==
          AdaptiveV2CrossCommitRetentionAdmissionStatus::invalid);
}

TEST_CASE(
    "adaptive-v2 verified response completes before a retained commit deadline",
    "[adaptive-v2][response-evidence][deadline][response]")
{
    ManualEvidenceDeadlineScheduler scheduler;
    AdaptiveV2ResponseEvidenceBridge bridge(0, limits());
    scheduler.bind(bridge);
    std::vector<EvidenceReportEnvelope> delivered;
    bridge.bind_transport(
        [&delivered](const EvidenceReportEnvelope &envelope) {
            delivered.push_back(envelope);
            return EvidenceTransportResult::accepted;
        });

    const auto key = proposal("response-before-deadline");
    REQUIRE(bridge.arm_with_deadline(
        key, response_tree(), kStartNs, kDeadlineUs));
    REQUIRE(bridge.record_verified_response(
        key,
        2,
        ExpectedMessageType::direct_vote,
        {2},
        after_us(40)));
    REQUIRE(bridge.close_consensus_context(key));
    REQUIRE(scheduler.fire(0, after_us(kDeadlineUs)));

    REQUIRE(delivered.size() == 2);
    CHECK(delivered[0].observation.observed_replica_id == 2);
    CHECK(delivered[0].observation.outcome == ResponseOutcome::on_time);
    CHECK(delivered[1].observation.observed_replica_id == 1);
    CHECK(delivered[1].observation.outcome == ResponseOutcome::timeout);
    CHECK(bridge.diagnostics().timeout_facts == 1);
    CHECK(bridge.diagnostics().timeout_tracker_rejections == 0);
    CHECK(bridge.diagnostics().active_handles == 0);
}

TEST_CASE(
    "deadline evidence and deferred commit cross shared-outbox backpressure in order",
    "[adaptive-v2][response-evidence][deadline][outbox][backpressure][commit]")
{
    ManualEvidenceDeadlineScheduler scheduler;
    AdaptiveV2ResponseEvidenceBridge bridge(0, limits());
    scheduler.bind(bridge);
    AdaptiveV2ReportingOutbox outbox(reporting_config(1));
    const auto key = proposal("deadline-outbox-backpressure");
    REQUIRE(outbox.enqueue_lifecycle(
                ProposalLifecycleFact{
                    NormalProposalRuntimeInitialized{key}}) ==
            AdaptiveV2ReportingEnqueueStatus::queued);

    bool commit_ready = false;
    bool commit_suppressed = false;
    std::size_t commit_enqueue_attempts = 0;
    std::vector<EvidenceDeadlineResult> results;
    std::function<void()> try_commit;
    try_commit = [&] {
        if (!commit_ready || commit_suppressed)
            return;
        ++commit_enqueue_attempts;
        const auto status = outbox.enqueue_lifecycle(
            ProposalLifecycleFact{ProposalCommitted{key}});
        if (status == AdaptiveV2ReportingEnqueueStatus::queued)
            commit_ready = false;
        else if (status !=
                 AdaptiveV2ReportingEnqueueStatus::capacity_exceeded)
        {
            commit_ready = false;
            commit_suppressed = true;
        }
    };
    bridge.bind_deadline_result_callback(
        [&](const ProposalKey &completed,
            EvidenceDeadlineResult result) {
            CHECK(completed == key);
            results.push_back(result);
            if (result == EvidenceDeadlineResult::evidence_accepted)
            {
                commit_ready = true;
                try_commit();
            }
            else
                commit_suppressed = true;
        });
    bridge.bind_transport(
        [&outbox](const EvidenceReportEnvelope &envelope) {
            const auto status = outbox.enqueue_evidence(
                envelope.canonical_payload);
            if (status == AdaptiveV2ReportingEnqueueStatus::queued)
                return EvidenceTransportResult::accepted;
            if (status ==
                AdaptiveV2ReportingEnqueueStatus::capacity_exceeded)
                return EvidenceTransportResult::temporary_failure;
            return EvidenceTransportResult::permanent_failure;
        });

    REQUIRE(bridge.arm_with_deadline(
        key, response_tree(), kStartNs, kDeadlineUs));
    REQUIRE(bridge.close_consensus_context(key));
    REQUIRE(scheduler.fire(0, after_us(kDeadlineUs)));
    CHECK(results.empty());
    CHECK(bridge.should_defer_commit_report(key));

    deliver_and_release(outbox, 1);
    CHECK(bridge.flush() == 1);
    CHECK(results.empty());
    REQUIRE(outbox.front() != nullptr);
    CHECK(outbox.front()->stream == AdaptiveV2ReportingStream::evidence);
    deliver_and_release(outbox, 2);

    CHECK(bridge.flush() == 1);
    CHECK(results ==
          std::vector<EvidenceDeadlineResult>{
              EvidenceDeadlineResult::evidence_accepted});
    CHECK(commit_ready);
    CHECK_FALSE(commit_suppressed);
    CHECK(commit_enqueue_attempts == 1);
    REQUIRE(outbox.front() != nullptr);
    CHECK(outbox.front()->stream == AdaptiveV2ReportingStream::evidence);
    deliver_and_release(outbox, 3);

    try_commit();
    CHECK_FALSE(commit_ready);
    CHECK(commit_enqueue_attempts == 2);
    REQUIRE(outbox.front() != nullptr);
    CHECK(outbox.front()->stream == AdaptiveV2ReportingStream::lifecycle);
    const auto committed = hotstuff::decode_proposal_lifecycle_notice(
        outbox.front()->canonical_payload,
        reporting_config().limits.lifecycle_wire);
    REQUIRE(committed);
    REQUIRE(std::holds_alternative<ProposalCommitted>(
        committed.notice->fact));
    CHECK(std::get<ProposalCommitted>(committed.notice->fact)
              .evidence_sequence_fence == 2);
    CHECK(bridge.diagnostics().completed_deadlines == 1);
    CHECK(bridge.diagnostics().active_deadlines == 0);
}

TEST_CASE(
    "fired deadline holds a late compensation ahead of the committed lifecycle fact",
    "[adaptive-v2][response-evidence][deadline][late][outbox][backpressure][commit]")
{
    ManualEvidenceDeadlineScheduler scheduler;
    AdaptiveV2ResponseEvidenceBridge bridge(0, limits());
    scheduler.bind(bridge);
    AdaptiveV2ReportingOutbox outbox(reporting_config(1));
    const auto key = proposal("fired-deadline-late-backpressure");
    REQUIRE(outbox.enqueue_lifecycle(
                ProposalLifecycleFact{
                    NormalProposalRuntimeInitialized{key}}) ==
            AdaptiveV2ReportingEnqueueStatus::queued);
    deliver_and_release(outbox, 1);

    auto tree = response_tree();
    tree.direct_children = {1};
    tree.assigned_subtree = {0, 1, 3, 4};
    tree.child_subtrees = {{1, {1, 3, 4}}};
    tree.required_subtree = {0, 1, 3, 4};
    tree.optional_subtree.clear();
    tree.required_child_subtrees = {{1, {1, 3, 4}}};

    bool commit_requested = false;
    bool commit_ready = false;
    std::size_t commit_enqueue_attempts = 0;
    std::vector<EvidenceDeadlineResult> results;
    const auto try_commit = [&] {
        if (!commit_ready)
            return;
        ++commit_enqueue_attempts;
        const auto status = outbox.enqueue_lifecycle(
            ProposalLifecycleFact{ProposalCommitted{key}});
        if (status == AdaptiveV2ReportingEnqueueStatus::queued)
            commit_ready = false;
    };
    bridge.bind_deadline_result_callback(
        [&](const ProposalKey &completed,
            EvidenceDeadlineResult result) {
            CHECK(completed == key);
            results.push_back(result);
            if (commit_requested &&
                result == EvidenceDeadlineResult::evidence_accepted)
            {
                commit_ready = true;
                try_commit();
            }
        });
    bridge.bind_transport(
        [&outbox](const EvidenceReportEnvelope &envelope) {
            const auto status = outbox.enqueue_evidence(
                envelope.canonical_payload);
            if (status == AdaptiveV2ReportingEnqueueStatus::queued)
                return EvidenceTransportResult::accepted;
            if (status ==
                AdaptiveV2ReportingEnqueueStatus::capacity_exceeded)
                return EvidenceTransportResult::temporary_failure;
            return EvidenceTransportResult::permanent_failure;
        });

    REQUIRE(bridge.arm_with_deadline(
        key, tree, kStartNs, kDeadlineUs));
    REQUIRE(scheduler.fire(0, after_us(kDeadlineUs)));
    REQUIRE(outbox.front() != nullptr);
    CHECK(outbox.front()->stream == AdaptiveV2ReportingStream::evidence);
    CHECK(results.empty());
    CHECK(bridge.should_defer_commit_report(key));

    REQUIRE(bridge.record_verified_response(
        key,
        1,
        ExpectedMessageType::aggregate_relay,
        {1, 3, 4},
        after_us(kDeadlineUs + 30)));
    CHECK(bridge.diagnostics().pending_reports == 1);

    commit_requested = true;
    if (!bridge.should_defer_commit_report(key))
        commit_ready = true;
    try_commit();
    REQUIRE(bridge.close_consensus_context(key));
    CHECK(results.empty());

    deliver_and_release(outbox, 2);
    try_commit();
    CHECK(outbox.front() == nullptr);
    CHECK(commit_enqueue_attempts == 0);

    CHECK(bridge.flush() == 1);
    CHECK(results ==
          std::vector<EvidenceDeadlineResult>{
              EvidenceDeadlineResult::evidence_accepted});
    CHECK(commit_ready);
    CHECK(commit_enqueue_attempts == 1);
    REQUIRE(outbox.front() != nullptr);
    CHECK(outbox.front()->stream == AdaptiveV2ReportingStream::evidence);
    deliver_and_release(outbox, 3);

    try_commit();
    CHECK_FALSE(commit_ready);
    CHECK(commit_enqueue_attempts == 2);
    REQUIRE(outbox.front() != nullptr);
    CHECK(outbox.front()->stream == AdaptiveV2ReportingStream::lifecycle);
    const auto committed = hotstuff::decode_proposal_lifecycle_notice(
        outbox.front()->canonical_payload,
        reporting_config().limits.lifecycle_wire);
    REQUIRE(committed);
    REQUIRE(std::holds_alternative<ProposalCommitted>(
        committed.notice->fact));
    CHECK(std::get<ProposalCommitted>(committed.notice->fact)
              .evidence_sequence_fence == 2);
    CHECK(bridge.diagnostics().completed_deadlines == 1);
    CHECK(bridge.diagnostics().active_deadlines == 0);
    CHECK(bridge.diagnostics().active_handles == 0);
    CHECK_FALSE(scheduler.fire(0, after_us(kDeadlineUs + 100)));
    CHECK(results.size() == 1);
}

TEST_CASE(
    "adaptive-v2 completed required responses need no commit-report deferral",
    "[adaptive-v2][response-evidence][deadline][commit][complete]")
{
    ManualEvidenceDeadlineScheduler scheduler;
    AdaptiveV2ResponseEvidenceBridge bridge(0, limits());
    scheduler.bind(bridge);
    bridge.bind_transport(
        [](const EvidenceReportEnvelope &) {
            return EvidenceTransportResult::accepted;
        });
    const auto key = proposal("complete-before-commit");
    REQUIRE(bridge.arm_with_deadline(
        key, response_tree(), kStartNs, kDeadlineUs));
    REQUIRE(bridge.record_verified_response(
        key,
        1,
        ExpectedMessageType::aggregate_relay,
        {1, 3, 4},
        after_us(30)));
    REQUIRE(bridge.record_verified_response(
        key,
        2,
        ExpectedMessageType::direct_vote,
        {2},
        after_us(40)));

    CHECK_FALSE(bridge.should_defer_commit_report(key));
    CHECK_FALSE(bridge.close_consensus_context(key));
    CHECK(scheduler.active_jobs() == 0);
    CHECK(bridge.diagnostics().active_deadlines == 0);
    CHECK(bridge.diagnostics().active_handles == 0);
    CHECK(bridge.diagnostics().timeout_facts == 0);
}

TEST_CASE(
    "direct authoritative commit remains durable across shared-outbox capacity",
    "[adaptive-v2][response-evidence][outbox][lifecycle][direct][capacity]")
{
    AdaptiveV2ReportingOutbox outbox(reporting_config(1));
    const auto key = proposal("direct-commit-capacity");
    REQUIRE(outbox.enqueue_lifecycle(
                ProposalLifecycleFact{
                    NormalProposalRuntimeInitialized{key}}) ==
            AdaptiveV2ReportingEnqueueStatus::queued);

    bool commit_ready = true;
    const auto retry_commit = [&] {
        if (!commit_ready)
            return AdaptiveV2ReportingEnqueueStatus::queued;
        const auto status = outbox.enqueue_lifecycle(
            ProposalLifecycleFact{ProposalCommitted{key}});
        if (status == AdaptiveV2ReportingEnqueueStatus::queued)
            commit_ready = false;
        return status;
    };

    CHECK(retry_commit() ==
          AdaptiveV2ReportingEnqueueStatus::capacity_exceeded);
    CHECK(commit_ready);
    deliver_and_release(outbox, 1);
    REQUIRE(retry_commit() == AdaptiveV2ReportingEnqueueStatus::queued);
    CHECK_FALSE(commit_ready);
    REQUIRE(outbox.front() != nullptr);
    const auto committed = hotstuff::decode_proposal_lifecycle_notice(
        outbox.front()->canonical_payload,
        reporting_config().limits.lifecycle_wire);
    REQUIRE(committed);
    REQUIRE(std::holds_alternative<ProposalCommitted>(
        committed.notice->fact));
    CHECK(std::get<ProposalCommitted>(committed.notice->fact)
              .proposal == key);
}

TEST_CASE(
    "false timeout before commit fails closed while retained evidence retries",
    "[adaptive-v2][response-evidence][false-report][outbox][capacity][order]")
{
    AdaptiveV2ReportingOutbox outbox(reporting_config(1));
    const auto key = proposal("false-timeout-before-commit-capacity");
    REQUIRE(outbox.enqueue_lifecycle(
                ProposalLifecycleFact{
                    NormalProposalRuntimeInitialized{key}}) ==
            AdaptiveV2ReportingEnqueueStatus::queued);

    AdaptiveV2ResponseEvidenceBridge bridge(0, limits());
    REQUIRE(bridge.arm(
        key, response_tree(), kStartNs, kDeadlineUs));
    std::vector<std::function<void()>> scheduled_retries;
    bridge.bind_retry_scheduler(
        [&scheduled_retries](std::function<void()> retry) {
            scheduled_retries.push_back(std::move(retry));
            return [] {};
        });
    bridge.bind_transport(
        [&outbox](const EvidenceReportEnvelope &envelope) {
            const auto status = outbox.enqueue_evidence(
                envelope.canonical_payload);
            if (status == AdaptiveV2ReportingEnqueueStatus::queued)
                return EvidenceTransportResult::accepted;
            if (status ==
                AdaptiveV2ReportingEnqueueStatus::capacity_exceeded)
                return EvidenceTransportResult::temporary_failure;
            return EvidenceTransportResult::permanent_failure;
        });

    REQUIRE(bridge.record_timeouts(
                key,
                std::set<hotstuff::ReplicaID>{1},
                after_us(kDeadlineUs)) ==
            1);
    REQUIRE(scheduled_retries.size() == 1);
    CHECK(bridge.diagnostics().pending_reports == 1);
    CHECK(outbox.diagnostics().pending_reports == 1);

    // This models the production suppressed commit tombstone created when
    // the fabricated timeout fires before commit but cannot enter the FIFO.
    const bool suppressed_commit_tombstone = true;
    const auto report_later_commit = [&] {
        if (suppressed_commit_tombstone)
            return false;
        return outbox.enqueue_lifecycle(
                   ProposalLifecycleFact{ProposalCommitted{key}}) ==
               AdaptiveV2ReportingEnqueueStatus::queued;
    };
    CHECK_FALSE(report_later_commit());

    deliver_and_release(outbox, 1);
    auto retry = std::move(scheduled_retries.front());
    scheduled_retries.clear();
    retry();
    CHECK(bridge.diagnostics().pending_reports == 0);
    REQUIRE(outbox.front() != nullptr);
    CHECK(outbox.front()->stream == AdaptiveV2ReportingStream::evidence);
    CHECK(outbox.diagnostics().pending_reports == 1);
    CHECK_FALSE(report_later_commit());
}

TEST_CASE(
    "terminal shared-outbox failure stops an already queued lifecycle tail",
    "[adaptive-v2][response-evidence][outbox][terminal][fail-closed][order]")
{
    AdaptiveV2ReportingOutbox outbox(reporting_config(4));
    const auto key = proposal("terminal-front-lifecycle-tail");
    REQUIRE(outbox.enqueue_lifecycle(
                ProposalLifecycleFact{
                    NormalProposalRuntimeInitialized{key}}) ==
            AdaptiveV2ReportingEnqueueStatus::queued);
    REQUIRE(outbox.enqueue_lifecycle(
                ProposalLifecycleFact{ProposalCommitted{key}}) ==
            AdaptiveV2ReportingEnqueueStatus::queued);

    const auto attempt = outbox.begin_delivery(1);
    REQUIRE(attempt.status == AdaptiveV2ReportingAttemptStatus::started);
    REQUIRE(attempt.token.has_value());
    REQUIRE(attempt.report != nullptr);
    const auto failed_report_id = attempt.report->report_id;
    REQUIRE(outbox.acknowledge_delivery(
                *attempt.token,
                AdaptiveV2ReportingDeliveryResult::permanent_failure,
                1) ==
            AdaptiveV2ReportingTransitionStatus::failed);
    outbox.shutdown();

    const auto diagnostics = outbox.diagnostics();
    CHECK(diagnostics.stopped);
    CHECK_FALSE(diagnostics.healthy);
    CHECK(diagnostics.pending_reports == 2);
    REQUIRE(outbox.front() != nullptr);
    CHECK(outbox.front()->report_id == failed_report_id);
    CHECK(outbox.begin_delivery(2).status ==
          AdaptiveV2ReportingAttemptStatus::unhealthy);
}

TEST_CASE(
    "awaiting evidence blocks convergence with free shared-outbox capacity",
    "[adaptive-v2][response-evidence][deadline][outbox][lifecycle][convergence][order]")
{
    AdaptiveV2ReportingOutbox outbox(reporting_config(8));
    const auto key = proposal("awaiting-evidence-convergence-fence");
    const auto identity = convergence_identity(
        "awaiting-evidence-convergence-fence");
    REQUIRE(outbox.enqueue_lifecycle(
                ProposalLifecycleFact{
                    NormalProposalRuntimeInitialized{key}}) ==
            AdaptiveV2ReportingEnqueueStatus::queued);

    ManualEvidenceDeadlineScheduler scheduler;
    AdaptiveV2ResponseEvidenceBridge bridge(0, limits());
    scheduler.bind(bridge);
    bridge.bind_transport(
        [&outbox](const EvidenceReportEnvelope &envelope) {
            const auto status = outbox.enqueue_evidence(
                envelope.canonical_payload);
            if (status == AdaptiveV2ReportingEnqueueStatus::queued)
                return EvidenceTransportResult::accepted;
            if (status ==
                AdaptiveV2ReportingEnqueueStatus::capacity_exceeded)
                return EvidenceTransportResult::temporary_failure;
            return EvidenceTransportResult::permanent_failure;
        });

    bool commit_fence = true;
    bool commit_queued = false;
    bool commit_enqueue_failed = false;
    std::vector<EvidenceDeadlineResult> results;
    bridge.bind_deadline_result_callback(
        [&](const ProposalKey &result_key,
            EvidenceDeadlineResult result) {
            results.push_back(result);
            if (result_key != key ||
                result != EvidenceDeadlineResult::evidence_accepted)
            {
                commit_enqueue_failed = true;
                return;
            }
            const auto status = outbox.enqueue_lifecycle(
                ProposalLifecycleFact{ProposalCommitted{key}});
            commit_queued =
                status == AdaptiveV2ReportingEnqueueStatus::queued;
            commit_enqueue_failed = !commit_queued;
            if (commit_queued)
                commit_fence = false;
        });

    REQUIRE(bridge.arm_with_deadline(
        key, response_tree(), kStartNs, kDeadlineUs));
    REQUIRE(bridge.should_defer_commit_report(key));
    REQUIRE(bridge.close_consensus_context(key));

    const auto enqueue_convergence_if_unfenced = [&] {
        if (commit_fence)
            return false;
        return outbox.enqueue_convergence_observation(
                   hotstuff::AdaptiveV2ConvergenceObservationKind::commit,
                   identity) ==
               AdaptiveV2ReportingEnqueueStatus::queued;
    };
    CHECK_FALSE(enqueue_convergence_if_unfenced());
    CHECK(outbox.diagnostics().pending_reports == 1);

    REQUIRE(scheduler.fire(0, after_us(kDeadlineUs)));
    REQUIRE(results.size() == 1);
    CHECK(results.front() == EvidenceDeadlineResult::evidence_accepted);
    CHECK_FALSE(commit_enqueue_failed);
    REQUIRE(commit_queued);
    CHECK_FALSE(commit_fence);
    REQUIRE(enqueue_convergence_if_unfenced());
    REQUIRE(outbox.enqueue_epoch_activated(identity) ==
            AdaptiveV2ReportingEnqueueStatus::queued);
    REQUIRE(outbox.diagnostics().pending_reports == 6);

    REQUIRE(outbox.front() != nullptr);
    CHECK(outbox.front()->stream == AdaptiveV2ReportingStream::lifecycle);
    const auto initialized = hotstuff::decode_proposal_lifecycle_notice(
        outbox.front()->canonical_payload,
        reporting_config().limits.lifecycle_wire);
    REQUIRE(initialized);
    REQUIRE(std::holds_alternative<NormalProposalRuntimeInitialized>(
        initialized.notice->fact));
    deliver_and_release(outbox, 1);

    REQUIRE(outbox.front() != nullptr);
    CHECK(outbox.front()->stream == AdaptiveV2ReportingStream::evidence);
    deliver_and_release(outbox, 2);
    REQUIRE(outbox.front() != nullptr);
    CHECK(outbox.front()->stream == AdaptiveV2ReportingStream::evidence);
    deliver_and_release(outbox, 3);

    REQUIRE(outbox.front() != nullptr);
    CHECK(outbox.front()->stream == AdaptiveV2ReportingStream::lifecycle);
    const auto committed = hotstuff::decode_proposal_lifecycle_notice(
        outbox.front()->canonical_payload,
        reporting_config().limits.lifecycle_wire);
    REQUIRE(committed);
    REQUIRE(std::holds_alternative<ProposalCommitted>(
        committed.notice->fact));
    deliver_and_release(outbox, 4);

    REQUIRE(outbox.front() != nullptr);
    CHECK(outbox.front()->stream == AdaptiveV2ReportingStream::convergence);
    REQUIRE(outbox.front()->convergence_observation_kind.has_value());
    CHECK(*outbox.front()->convergence_observation_kind ==
          hotstuff::AdaptiveV2ConvergenceObservationKind::commit);
    CHECK(outbox.diagnostics().pending_reports == 2);
}

TEST_CASE(
    "single FIFO keeps initialization evidence commit convergence and activation order",
    "[adaptive-v2][response-evidence][outbox][lifecycle][false-report][capacity][order]")
{
    AdaptiveV2ReportingOutbox outbox(reporting_config(2));
    const auto blocker = proposal("initialization-capacity-blocker");
    const auto key = proposal("false-report-full-order");
    REQUIRE(outbox.enqueue_lifecycle(
                ProposalLifecycleFact{
                    NormalProposalRuntimeInitialized{blocker}}) ==
            AdaptiveV2ReportingEnqueueStatus::queued);
    REQUIRE(outbox.enqueue_lifecycle(
                ProposalLifecycleFact{
                    ProposalCommitted{blocker}}) ==
            AdaptiveV2ReportingEnqueueStatus::queued);

    bool initialization_ready = true;
    const auto retry_initialization = [&] {
        if (!initialization_ready)
            return AdaptiveV2ReportingEnqueueStatus::queued;
        const auto status = outbox.enqueue_lifecycle(
            ProposalLifecycleFact{
                NormalProposalRuntimeInitialized{key}});
        if (status == AdaptiveV2ReportingEnqueueStatus::queued)
            initialization_ready = false;
        return status;
    };
    CHECK(retry_initialization() ==
          AdaptiveV2ReportingEnqueueStatus::capacity_exceeded);
    CHECK(initialization_ready);

    deliver_and_release(outbox, 1);
    REQUIRE(retry_initialization() ==
            AdaptiveV2ReportingEnqueueStatus::queued);
    CHECK_FALSE(initialization_ready);
    deliver_and_release(outbox, 2);
    REQUIRE(outbox.front() != nullptr);
    const auto initialized = hotstuff::decode_proposal_lifecycle_notice(
        outbox.front()->canonical_payload,
        reporting_config().limits.lifecycle_wire);
    REQUIRE(initialized);
    REQUIRE(std::holds_alternative<NormalProposalRuntimeInitialized>(
        initialized.notice->fact));
    CHECK(std::get<NormalProposalRuntimeInitialized>(
              initialized.notice->fact).proposal == key);

    AdaptiveV2ResponseEvidenceBridge bridge(0, limits());
    bridge.bind_transport(
        [&outbox](const EvidenceReportEnvelope &envelope) {
            const auto status = outbox.enqueue_evidence(
                envelope.canonical_payload);
            if (status == AdaptiveV2ReportingEnqueueStatus::queued)
                return EvidenceTransportResult::accepted;
            if (status ==
                AdaptiveV2ReportingEnqueueStatus::capacity_exceeded)
                return EvidenceTransportResult::temporary_failure;
            return EvidenceTransportResult::permanent_failure;
        });
    REQUIRE(bridge.arm(
        key, response_tree(), kStartNs, kDeadlineUs));
    REQUIRE(bridge.record_timeouts(
                key,
                std::set<hotstuff::ReplicaID>{1},
                after_us(kDeadlineUs)) ==
            1);
    REQUIRE(outbox.diagnostics().pending_reports == 2);

    bool commit_ready = true;
    const auto retry_commit = [&] {
        if (!commit_ready)
            return AdaptiveV2ReportingEnqueueStatus::queued;
        const auto status = outbox.enqueue_lifecycle(
            ProposalLifecycleFact{ProposalCommitted{key}});
        if (status == AdaptiveV2ReportingEnqueueStatus::queued)
            commit_ready = false;
        return status;
    };
    CHECK(retry_commit() ==
          AdaptiveV2ReportingEnqueueStatus::capacity_exceeded);
    CHECK(commit_ready);

    deliver_and_release(outbox, 3);
    REQUIRE(outbox.front() != nullptr);
    CHECK(outbox.front()->stream == AdaptiveV2ReportingStream::evidence);
    REQUIRE(retry_commit() == AdaptiveV2ReportingEnqueueStatus::queued);
    CHECK_FALSE(commit_ready);

    deliver_and_release(outbox, 4);
    REQUIRE(outbox.front() != nullptr);
    CHECK(outbox.front()->stream == AdaptiveV2ReportingStream::lifecycle);
    REQUIRE(outbox.enqueue_convergence_observation(
                hotstuff::AdaptiveV2ConvergenceObservationKind::commit,
                convergence_identity("full-order")) ==
            AdaptiveV2ReportingEnqueueStatus::queued);

    deliver_and_release(outbox, 5);
    REQUIRE(outbox.front() != nullptr);
    CHECK(outbox.front()->stream == AdaptiveV2ReportingStream::convergence);
    REQUIRE(outbox.enqueue_epoch_activated(
                convergence_identity("full-order")) ==
            AdaptiveV2ReportingEnqueueStatus::queued);
    CHECK(outbox.diagnostics().pending_reports == 2);
    CHECK(outbox.front()->stream == AdaptiveV2ReportingStream::convergence);
}

TEST_CASE(
    "accepted response backpressure completes a closed fence before deadline",
    "[adaptive-v2][response-evidence][deadline][commit][backpressure][complete]")
{
    ManualEvidenceDeadlineScheduler scheduler;
    AdaptiveV2ResponseEvidenceBridge bridge(0, limits());
    scheduler.bind(bridge);
    std::vector<EvidenceDeadlineResult> results;
    bridge.bind_deadline_result_callback(
        [&results](const ProposalKey &, EvidenceDeadlineResult result) {
            results.push_back(result);
        });
    bridge.bind_transport(
        [](const EvidenceReportEnvelope &) {
            return EvidenceTransportResult::temporary_failure;
        });
    const auto key = proposal("response-backpressure-before-deadline");
    REQUIRE(bridge.arm_with_deadline(
        key, response_tree(), kStartNs, kDeadlineUs));
    REQUIRE(bridge.record_verified_response(
        key,
        1,
        ExpectedMessageType::aggregate_relay,
        {1, 3, 4},
        after_us(30)));
    REQUIRE(bridge.record_verified_response(
        key,
        2,
        ExpectedMessageType::direct_vote,
        {2},
        after_us(40)));
    REQUIRE(bridge.should_defer_commit_report(key));
    REQUIRE(bridge.close_consensus_context(key));
    CHECK(results.empty());
    REQUIRE(scheduler.active_jobs() == 1);

    std::size_t accepted = 0;
    bridge.bind_transport(
        [&accepted](const EvidenceReportEnvelope &) {
            ++accepted;
            return EvidenceTransportResult::accepted;
        });

    CHECK(accepted == 2);
    CHECK(results ==
          std::vector<EvidenceDeadlineResult>{
              EvidenceDeadlineResult::evidence_accepted});
    CHECK(scheduler.active_jobs() == 0);
    CHECK_FALSE(scheduler.fire(0, after_us(kDeadlineUs)));
    CHECK(bridge.diagnostics().active_deadlines == 0);
    CHECK(bridge.diagnostics().active_handles == 0);
    CHECK(bridge.diagnostics().timeout_facts == 0);
}

TEST_CASE(
    "adaptive-v2 normal and observation deadlines record each timeout once",
    "[adaptive-v2][response-evidence][deadline][idempotent]")
{
    ManualEvidenceDeadlineScheduler scheduler;
    AdaptiveV2ResponseEvidenceBridge bridge(0, limits());
    scheduler.bind(bridge);
    bridge.bind_transport(
        [](const EvidenceReportEnvelope &) {
            return EvidenceTransportResult::accepted;
        });
    const auto key = proposal("idempotent-deadline");
    REQUIRE(bridge.arm_with_deadline(
        key, response_tree(), kStartNs, kDeadlineUs));

    CHECK(bridge.record_timeouts(
              key, {1}, after_us(kDeadlineUs)) == 1);
    REQUIRE(bridge.close_consensus_context(key));
    REQUIRE(scheduler.fire(0, after_us(kDeadlineUs)));
    CHECK(bridge.record_timeouts(
              key, {1, 2}, after_us(kDeadlineUs + 1)) == 0);

    const auto diagnostics = bridge.diagnostics();
    CHECK(diagnostics.timeout_facts == 2);
    CHECK(diagnostics.timeout_tracker_rejections == 0);
    CHECK(diagnostics.active_handles == 0);
    CHECK(diagnostics.healthy);
}

TEST_CASE(
    "adaptive-v2 abort retirement cancels the observation deadline without evidence",
    "[adaptive-v2][response-evidence][deadline][abort]")
{
    ManualEvidenceDeadlineScheduler scheduler;
    AdaptiveV2ResponseEvidenceBridge bridge(0, limits());
    scheduler.bind(bridge);
    const auto key = proposal("abort-before-deadline");
    REQUIRE(bridge.arm_with_deadline(
        key, response_tree(), kStartNs, kDeadlineUs));
    REQUIRE(scheduler.active_jobs() == 1);

    CHECK(bridge.retire(key) == 3);
    CHECK(scheduler.active_jobs() == 0);
    CHECK_FALSE(scheduler.fire(0, after_us(kDeadlineUs)));
    const auto diagnostics = bridge.diagnostics();
    CHECK(diagnostics.deadline_cancellations == 1);
    CHECK(diagnostics.timeout_facts == 0);
    CHECK(diagnostics.pending_reports == 0);
    CHECK(diagnostics.active_handles == 0);
    CHECK(diagnostics.active_deadlines == 0);
}

TEST_CASE(
    "adaptive-v2 deadline scheduling failure retires the attempt fail closed",
    "[adaptive-v2][response-evidence][deadline][failure]")
{
    SECTION("initial scheduling rejection")
    {
        AdaptiveV2ResponseEvidenceBridge bridge(0, limits());
        std::vector<EvidenceDeadlineResult> results;
        bridge.bind_deadline_result_callback(
            [&results](const ProposalKey &,
                       EvidenceDeadlineResult result) {
                results.push_back(result);
            });
        bridge.bind_deadline_scheduler(
            [](const ProposalKey &,
               std::uint64_t,
               hotstuff::EvidenceDeadlineCallback,
               hotstuff::EvidenceDeadlineFailureCallback) {
                return hotstuff::EvidenceDeadlineCancellation{};
            });

        CHECK_FALSE(bridge.arm_with_deadline(
            proposal("schedule-failure"),
            response_tree(),
            kStartNs,
            kDeadlineUs));
        const auto diagnostics = bridge.diagnostics();
        CHECK(diagnostics.active_handles == 0);
        CHECK(diagnostics.active_deadlines == 0);
        CHECK(diagnostics.deadline_schedule_failures == 1);
        CHECK_FALSE(diagnostics.healthy);
        CHECK(results ==
              std::vector<EvidenceDeadlineResult>{
                  EvidenceDeadlineResult::failed});
    }

    SECTION("deferred scheduler failure callback")
    {
        ManualEvidenceDeadlineScheduler scheduler;
        AdaptiveV2ResponseEvidenceBridge bridge(0, limits());
        scheduler.bind(bridge);
        std::vector<EvidenceDeadlineResult> results;
        bridge.bind_deadline_result_callback(
            [&results](const ProposalKey &,
                       EvidenceDeadlineResult result) {
                results.push_back(result);
            });
        REQUIRE(bridge.arm_with_deadline(
            proposal("deferred-schedule-failure"),
            response_tree(),
            kStartNs,
            kDeadlineUs));
        REQUIRE(scheduler.fail(0));
        const auto diagnostics = bridge.diagnostics();
        CHECK(diagnostics.active_handles == 0);
        CHECK(diagnostics.active_deadlines == 0);
        CHECK(diagnostics.deadline_schedule_failures == 1);
        CHECK_FALSE(diagnostics.healthy);
        CHECK(results ==
              std::vector<EvidenceDeadlineResult>{
                  EvidenceDeadlineResult::failed});
    }

    SECTION("synchronous scheduler failure callback is diagnosed once")
    {
        AdaptiveV2ResponseEvidenceBridge bridge(0, limits());
        std::vector<EvidenceDeadlineResult> results;
        bridge.bind_deadline_result_callback(
            [&results](const ProposalKey &,
                       EvidenceDeadlineResult result) {
                results.push_back(result);
            });
        bridge.bind_deadline_scheduler(
            [](const ProposalKey &,
               std::uint64_t,
               hotstuff::EvidenceDeadlineCallback,
               hotstuff::EvidenceDeadlineFailureCallback failure) {
                failure();
                return hotstuff::EvidenceDeadlineCancellation{};
            });

        CHECK_FALSE(bridge.arm_with_deadline(
            proposal("synchronous-schedule-failure"),
            response_tree(),
            kStartNs,
            kDeadlineUs));
        const auto diagnostics = bridge.diagnostics();
        CHECK(diagnostics.active_handles == 0);
        CHECK(diagnostics.active_deadlines == 0);
        CHECK(diagnostics.deadline_schedule_failures == 1);
        CHECK_FALSE(diagnostics.healthy);
        CHECK(results ==
              std::vector<EvidenceDeadlineResult>{
                  EvidenceDeadlineResult::failed});
    }

    SECTION("synchronous failure callback followed by throw is diagnosed once")
    {
        AdaptiveV2ResponseEvidenceBridge bridge(0, limits());
        std::vector<EvidenceDeadlineResult> results;
        bridge.bind_deadline_result_callback(
            [&results](const ProposalKey &,
                       EvidenceDeadlineResult result) {
                results.push_back(result);
            });
        bridge.bind_deadline_scheduler(
            [](const ProposalKey &,
               std::uint64_t,
               hotstuff::EvidenceDeadlineCallback,
               hotstuff::EvidenceDeadlineFailureCallback failure)
                -> hotstuff::EvidenceDeadlineCancellation {
                failure();
                throw std::runtime_error(
                    "scheduler throws after synchronous failure");
            });

        CHECK_FALSE(bridge.arm_with_deadline(
            proposal("synchronous-failure-then-throw"),
            response_tree(),
            kStartNs,
            kDeadlineUs));
        const auto diagnostics = bridge.diagnostics();
        CHECK(diagnostics.active_handles == 0);
        CHECK(diagnostics.active_deadlines == 0);
        CHECK(diagnostics.deadline_schedule_failures == 1);
        CHECK_FALSE(diagnostics.healthy);
        CHECK(results ==
              std::vector<EvidenceDeadlineResult>{
                  EvidenceDeadlineResult::failed});
    }
}

TEST_CASE(
    "throwing cancellations leave only lifetime-guarded scheduler callbacks",
    "[adaptive-v2][response-evidence][deadline][retry][shutdown][lifetime]")
{
    hotstuff::EvidenceDeadlineCallback retained_deadline;
    hotstuff::EvidenceDeadlineFailureCallback retained_failure;
    std::function<void()> retained_retry;
    std::vector<EvidenceDeadlineResult> results;
    {
        AdaptiveV2ResponseEvidenceBridge bridge(0, limits());
        bridge.bind_deadline_result_callback(
            [&results](const ProposalKey &,
                       EvidenceDeadlineResult result) {
                results.push_back(result);
            });
        bridge.bind_deadline_scheduler(
            [&](const ProposalKey &,
                std::uint64_t,
                hotstuff::EvidenceDeadlineCallback deadline,
                hotstuff::EvidenceDeadlineFailureCallback failure) {
                retained_deadline = std::move(deadline);
                retained_failure = std::move(failure);
                return [] {
                    throw std::runtime_error(
                        "deadline cancellation retained callback");
                };
            });
        bridge.bind_retry_scheduler(
            [&](std::function<void()> retry) {
                retained_retry = std::move(retry);
                return [] {
                    throw std::runtime_error(
                        "retry cancellation retained callback");
                };
            });
        bridge.bind_transport(
            [](const EvidenceReportEnvelope &) {
                return EvidenceTransportResult::temporary_failure;
            });
        const auto key = proposal("throwing-cancellation-lifetime");
        REQUIRE(bridge.arm_with_deadline(
            key, response_tree(), kStartNs, kDeadlineUs));
        REQUIRE(bridge.record_verified_response(
            key,
            2,
            ExpectedMessageType::direct_vote,
            {2},
            after_us(10)));
        REQUIRE(retained_deadline);
        REQUIRE(retained_failure);
        REQUIRE(retained_retry);
    }

    REQUIRE(results.size() == 1);
    CHECK(results.front() == EvidenceDeadlineResult::failed);
    retained_deadline(after_us(kDeadlineUs));
    retained_failure();
    retained_retry();
    CHECK(results.size() == 1);
}

TEST_CASE(
    "permanent deadline evidence failure suppresses completion",
    "[adaptive-v2][response-evidence][deadline][outbox][permanent-failure]")
{
    ManualEvidenceDeadlineScheduler scheduler;
    AdaptiveV2ResponseEvidenceBridge bridge(0, limits());
    scheduler.bind(bridge);
    std::vector<EvidenceDeadlineResult> results;
    bridge.bind_deadline_result_callback(
        [&results](const ProposalKey &, EvidenceDeadlineResult result) {
            results.push_back(result);
        });
    bridge.bind_transport(
        [](const EvidenceReportEnvelope &) {
            return EvidenceTransportResult::permanent_failure;
        });

    const auto key = proposal("permanent-deadline-delivery-failure");
    REQUIRE(bridge.arm_with_deadline(
        key, response_tree(), kStartNs, kDeadlineUs));
    REQUIRE(bridge.close_consensus_context(key));
    REQUIRE(scheduler.fire(0, after_us(kDeadlineUs)));

    CHECK(results ==
          std::vector<EvidenceDeadlineResult>{
              EvidenceDeadlineResult::failed});
    const auto diagnostics = bridge.diagnostics();
    CHECK(diagnostics.deadline_delivery_failures == 1);
    CHECK(diagnostics.completed_deadlines == 0);
    CHECK(diagnostics.active_deadlines == 0);
    CHECK(diagnostics.active_handles == 0);
    CHECK_FALSE(diagnostics.healthy);
    CHECK_FALSE(bridge.should_defer_commit_report(key));

    const auto rejected_after_terminal =
        proposal("arm-after-permanent-delivery-failure");
    CHECK_FALSE(bridge.arm_with_deadline(
        rejected_after_terminal,
        response_tree(),
        kStartNs,
        kDeadlineUs));
    CHECK(results ==
          std::vector<EvidenceDeadlineResult>{
              EvidenceDeadlineResult::failed,
              EvidenceDeadlineResult::failed});
    CHECK(scheduler.active_jobs() == 0);
    CHECK(scheduler.jobs.size() == 1);
    CHECK(bridge.diagnostics().active_deadlines == 0);
    CHECK(bridge.diagnostics().active_handles == 0);
}

TEST_CASE(
    "normal arm rejection reports one failed evidence fence",
    "[adaptive-v2][response-evidence][deadline][arm][failure]")
{
    ManualEvidenceDeadlineScheduler scheduler;
    AdaptiveV2ResponseEvidenceBridge bridge(0, limits(2));
    scheduler.bind(bridge);
    std::vector<std::pair<ProposalKey, EvidenceDeadlineResult>> results;
    bridge.bind_deadline_result_callback(
        [&results](const ProposalKey &key,
                   EvidenceDeadlineResult result) {
            results.emplace_back(key, result);
        });

    const auto key = proposal("normal-arm-capacity-failure");
    CHECK_FALSE(bridge.arm_with_deadline(
        key, response_tree(), kStartNs, kDeadlineUs));
    REQUIRE(results.size() == 1);
    CHECK(results[0].first == key);
    CHECK(results[0].second == EvidenceDeadlineResult::failed);
    CHECK(scheduler.jobs.empty());
    CHECK(bridge.diagnostics().active_deadlines == 0);
    CHECK(bridge.diagnostics().active_handles == 0);
}

TEST_CASE(
    "arm-with-deadline rejects handles that have no matching normal deadline",
    "[adaptive-v2][response-evidence][deadline][duplicate]")
{
    ManualEvidenceDeadlineScheduler scheduler;
    AdaptiveV2ResponseEvidenceBridge bridge(0, limits());
    scheduler.bind(bridge);
    std::vector<std::pair<ProposalKey, EvidenceDeadlineResult>> results;
    bridge.bind_deadline_result_callback(
        [&results](const ProposalKey &proposal,
                   EvidenceDeadlineResult result) {
            results.emplace_back(proposal, result);
        });
    const auto key = proposal("plain-arm-then-normal-duplicate");
    REQUIRE(bridge.arm(
        key, response_tree(), kStartNs, kDeadlineUs));
    CHECK_FALSE(bridge.arm_with_deadline(
        key, response_tree(), kStartNs, kDeadlineUs));
    CHECK(scheduler.jobs.empty());
    CHECK(bridge.diagnostics().active_handles == 0);
    REQUIRE(results.size() == 1);
    CHECK(results[0].first == key);
    CHECK(results[0].second == EvidenceDeadlineResult::failed);

    const auto normal_key = proposal("normal-deadline-duplicate");
    REQUIRE(bridge.arm_with_deadline(
        normal_key, response_tree(), kStartNs, kDeadlineUs));
    REQUIRE(bridge.arm_with_deadline(
        normal_key, response_tree(), kStartNs, kDeadlineUs));
    CHECK(scheduler.jobs.size() == 1);

    auto mismatched_tree = response_tree();
    mismatched_tree.required_child_subtrees.at(1).clear();
    CHECK_FALSE(bridge.arm_with_deadline(
        normal_key, mismatched_tree, kStartNs, kDeadlineUs));
    CHECK(scheduler.active_jobs() == 0);
    CHECK(bridge.diagnostics().active_handles == 0);
    CHECK(bridge.diagnostics().active_deadlines == 0);
    REQUIRE(results.size() == 2);
    CHECK(results[1].first == normal_key);
    CHECK(results[1].second == EvidenceDeadlineResult::failed);
    CHECK_FALSE(scheduler.fire(0, after_us(kDeadlineUs)));
    CHECK(results.size() == 2);
    bridge.shutdown();
}

TEST_CASE(
    "epoch activation leaves deadlines live and shutdown fails them closed",
    "[adaptive-v2][response-evidence][deadline][epoch][shutdown]")
{
    ManualEvidenceDeadlineScheduler scheduler;
    AdaptiveV2ResponseEvidenceBridge bridge(0, limits());
    scheduler.bind(bridge);
    std::vector<std::pair<ProposalKey, EvidenceDeadlineResult>> results;
    bridge.bind_deadline_result_callback(
        [&results](const ProposalKey &key,
                   EvidenceDeadlineResult result) {
            results.emplace_back(key, result);
        });
    bridge.bind_transport(
        [](const EvidenceReportEnvelope &) {
            return EvidenceTransportResult::accepted;
        });
    auto old_key = proposal("old-epoch-deadline");
    old_key.configuration.epoch_number = 6;
    auto live_key = proposal("live-epoch-deadline");
    live_key.configuration.epoch_number = 7;
    REQUIRE(bridge.arm_with_deadline(
        old_key, response_tree(), kStartNs, kDeadlineUs));
    REQUIRE(bridge.arm_with_deadline(
        live_key, response_tree(), kStartNs, kDeadlineUs));
    REQUIRE(scheduler.active_jobs() == 2);

    // Advancing the protocol epoch does not retire this independent bounded
    // observation fence. The old authoritative deadline completes normally.
    REQUIRE(bridge.close_consensus_context(old_key));
    REQUIRE(scheduler.fire(0, after_us(kDeadlineUs)));
    REQUIRE(results.size() == 1);
    CHECK(results[0].first == old_key);
    CHECK(results[0].second ==
          EvidenceDeadlineResult::evidence_accepted);
    CHECK(bridge.diagnostics().active_handles == 3);
    CHECK(bridge.diagnostics().active_deadlines == 1);
    CHECK(scheduler.active_jobs() == 1);

    bridge.shutdown();
    REQUIRE(results.size() == 2);
    CHECK(results[1].first == live_key);
    CHECK(results[1].second == EvidenceDeadlineResult::failed);
    CHECK(bridge.diagnostics().active_handles == 0);
    CHECK(bridge.diagnostics().active_deadlines == 0);
    CHECK(scheduler.active_jobs() == 0);
    CHECK_FALSE(bridge.arm_with_deadline(
        proposal("after-shutdown"),
        response_tree(),
        kStartNs,
        kDeadlineUs));
}

TEST_CASE(
    "adaptive-v2 timeout keeps the exact attempt open for a late compensation fact",
    "[adaptive-v2][response-evidence][late]")
{
    AdaptiveV2ResponseEvidenceBridge bridge(6, limits());
    const auto key = proposal("late-response");
    REQUIRE(bridge.arm(key, response_tree(), kStartNs, kDeadlineUs));

    CHECK(bridge.record_timeouts(key, {1}, after_us(100)) == 1);
    CHECK(bridge.record_timeouts(key, {1}, after_us(101)) == 0);
    CHECK(bridge.diagnostics().timeout_tracker_rejections == 0);
    REQUIRE(bridge.record_verified_response(
        key,
        1,
        ExpectedMessageType::aggregate_relay,
        {1, 3, 4},
        after_us(130)));

    std::vector<EvidenceReportEnvelope> delivered;
    bridge.bind_transport(
        [&delivered](const EvidenceReportEnvelope &envelope) {
            delivered.push_back(envelope);
            return EvidenceTransportResult::accepted;
        });
    REQUIRE(delivered.size() == 2);
    CHECK(bridge.flush() == 0);
    CHECK(delivered[0].observation.observed_replica_id == 1);
    CHECK(delivered[0].observation.expected_message_type ==
          ExpectedMessageType::aggregate_relay);
    CHECK(delivered[0].observation.outcome == ResponseOutcome::timeout);
    CHECK(delivered[0].observation.signer_set.empty());
    CHECK(delivered[1].observation.outcome == ResponseOutcome::late);
    CHECK((delivered[1].observation.signer_set ==
           std::vector<hotstuff::ReplicaID>{1, 3, 4}));
    CHECK(delivered[0].observation.observation_id ==
          delivered[1].observation.observation_id);
}

TEST_CASE(
    "duplicate late verified response is an idempotent no-op",
    "[adaptive-v2][response-evidence][duplicate][late]")
{
    ManualEvidenceDeadlineScheduler scheduler;
    AdaptiveV2ResponseEvidenceBridge bridge(0, limits());
    scheduler.bind(bridge);
    std::vector<EvidenceDeadlineResult> results;
    bridge.bind_deadline_result_callback(
        [&results](const ProposalKey &, EvidenceDeadlineResult result) {
            results.push_back(result);
        });
    std::vector<EvidenceReportEnvelope> delivered;
    bridge.bind_transport(
        [&delivered](const EvidenceReportEnvelope &envelope) {
            delivered.push_back(envelope);
            return EvidenceTransportResult::accepted;
        });

    const auto key = proposal("duplicate-late-response");
    REQUIRE(bridge.arm_with_deadline(
        key, response_tree(), kStartNs, kDeadlineUs));
    REQUIRE(scheduler.fire(0, after_us(kDeadlineUs)));
    REQUIRE(delivered.size() == 2);
    CHECK(results.empty());

    REQUIRE(bridge.record_verified_response(
        key,
        1,
        ExpectedMessageType::aggregate_relay,
        {1, 3},
        after_us(130)));
    REQUIRE(delivered.size() == 3);
    CHECK(delivered.back().observation.outcome == ResponseOutcome::late);

    CHECK_FALSE(bridge.record_verified_response(
        key,
        1,
        ExpectedMessageType::aggregate_relay,
        {1, 3, 4},
        after_us(131)));
    const auto diagnostics = bridge.diagnostics();
    CHECK(delivered.size() == 3);
    CHECK(diagnostics.timeout_facts == 2);
    CHECK(diagnostics.response_facts == 1);
    CHECK(diagnostics.idempotent_duplicate_responses == 1);
    CHECK(diagnostics.rejected_operations == 0);
    CHECK(diagnostics.deadline_delivery_failures == 0);
    CHECK(diagnostics.healthy);
    CHECK(results.empty());

    REQUIRE(bridge.close_consensus_context(key));
    CHECK(results ==
          std::vector<EvidenceDeadlineResult>{
              EvidenceDeadlineResult::evidence_accepted});
    CHECK(bridge.diagnostics().healthy);
}

TEST_CASE(
    "adaptive-v2 wait-exempt absence and rejected response inputs produce no fact",
    "[adaptive-v2][response-evidence][neutral]")
{
    AdaptiveV2ResponseEvidenceBridge bridge(0, limits());
    const auto key = proposal("neutral-absence");
    REQUIRE(bridge.arm(key, response_tree(), kStartNs, kDeadlineUs));

    CHECK(bridge.record_timeouts(key, {5}, after_us(100)) == 0);
    CHECK(bridge.diagnostics().timeout_ineligible_attempts == 1);
    CHECK_FALSE(bridge.record_verified_response(
        key,
        1,
        ExpectedMessageType::direct_vote,
        {1},
        after_us(20)));
    CHECK_FALSE(bridge.record_verified_response(
        key,
        99,
        ExpectedMessageType::aggregate_relay,
        {99},
        after_us(20)));
    CHECK(bridge.front() == nullptr);
    CHECK(bridge.diagnostics().pending_reports == 0);
}

TEST_CASE(
    "adaptive-v2 retirement removes exact handles and prevents stale facts",
    "[adaptive-v2][response-evidence][retirement]")
{
    AdaptiveV2ResponseEvidenceBridge bridge(0, limits());
    const auto key = proposal("retirement");
    REQUIRE(bridge.arm(key, response_tree(), kStartNs, kDeadlineUs));

    CHECK(bridge.retire(key) == 3);
    CHECK(bridge.diagnostics().active_handles == 0);
    CHECK(bridge.diagnostics().retired_attempts == 3);
    CHECK(bridge.record_timeouts(key, {1, 2}, after_us(100)) == 0);
    CHECK(bridge.diagnostics().timeout_missing_handles == 2);
    CHECK_FALSE(bridge.record_verified_response(
        key,
        2,
        ExpectedMessageType::direct_vote,
        {2},
        after_us(20)));
    CHECK(bridge.front() == nullptr);
}

TEST_CASE(
    "adaptive-v2 bounded arm failure leaves no partial attempt state",
    "[adaptive-v2][response-evidence][bounds]")
{
    AdaptiveV2ResponseEvidenceBridge bridge(0, limits(2));
    const auto key = proposal("bounded-failure");

    CHECK_FALSE(bridge.arm(
        key, response_tree(), kStartNs, kDeadlineUs));
    const auto diagnostics = bridge.diagnostics();
    CHECK(diagnostics.active_handles == 0);
    CHECK(diagnostics.armed_attempts == 0);
    CHECK(diagnostics.capacity_failures == 1);
    CHECK_FALSE(diagnostics.healthy);
}

TEST_CASE(
    "adaptive-v2 evidence transport is explicit and temporary failure retains the outbox",
    "[adaptive-v2][response-evidence][transport]")
{
    AdaptiveV2ResponseEvidenceBridge bridge(0, limits());
    const auto key = proposal("transport");
    REQUIRE(bridge.arm(key, response_tree(), kStartNs, kDeadlineUs));
    REQUIRE(bridge.record_verified_response(
        key,
        2,
        ExpectedMessageType::direct_vote,
        {2},
        after_us(10)));

    CHECK(bridge.flush() == 0);
    CHECK(bridge.diagnostics().pending_reports == 1);
    bridge.bind_transport(
        [](const EvidenceReportEnvelope &) {
            return EvidenceTransportResult::temporary_failure;
        });
    REQUIRE(bridge.front() != nullptr);
    CHECK(bridge.front()->delivery_attempts == 1);
    CHECK(bridge.front()->temporary_failures == 1);
    CHECK(bridge.flush() == 0);
    CHECK(bridge.front()->delivery_attempts == 2);
    CHECK(bridge.front()->temporary_failures == 2);

    bridge.bind_transport(
        [](const EvidenceReportEnvelope &) {
            return EvidenceTransportResult::accepted;
        });
    CHECK(bridge.front() == nullptr);
    CHECK(bridge.flush() == 0);
    bridge.unbind_transport();
    CHECK_FALSE(bridge.diagnostics().transport_bound);
}

TEST_CASE(
    "adaptive-v2 bound transport receives response and timeout facts immediately",
    "[adaptive-v2][response-evidence][transport][automatic]")
{
    AdaptiveV2ResponseEvidenceBridge bridge(0, limits());
    const auto key = proposal("automatic-transport");
    REQUIRE(bridge.arm(key, response_tree(), kStartNs, kDeadlineUs));

    std::vector<EvidenceReportEnvelope> delivered;
    bridge.bind_transport(
        [&delivered](const EvidenceReportEnvelope &envelope) {
            delivered.push_back(envelope);
            return EvidenceTransportResult::accepted;
        });
    REQUIRE(bridge.record_verified_response(
        key,
        2,
        ExpectedMessageType::direct_vote,
        {2},
        after_us(10)));
    REQUIRE(delivered.size() == 1);
    CHECK(delivered[0].observation.outcome == ResponseOutcome::on_time);

    CHECK(bridge.record_timeouts(key, {1}, after_us(100)) == 1);
    REQUIRE(delivered.size() == 2);
    CHECK(delivered[1].observation.outcome == ResponseOutcome::timeout);
    CHECK(bridge.diagnostics().pending_reports == 0);
    CHECK(bridge.diagnostics().retained_facts == 0);
}

TEST_CASE(
    "adaptive-v2 reporter backpressure preserves timeout before late compensation",
    "[adaptive-v2][response-evidence][retention][fifo]")
{
    AdaptiveV2ResponseEvidenceBridge bridge(0, limits(16, 1, 2));
    const auto key = proposal("retention-fifo");
    REQUIRE(bridge.arm(key, response_tree(), kStartNs, kDeadlineUs));

    REQUIRE(bridge.record_verified_response(
        key,
        2,
        ExpectedMessageType::direct_vote,
        {2},
        after_us(10)));
    CHECK(bridge.diagnostics().pending_reports == 1);
    CHECK(bridge.diagnostics().retained_facts == 0);

    CHECK(bridge.record_timeouts(key, {1}, after_us(100)) == 1);
    REQUIRE(bridge.record_verified_response(
        key,
        1,
        ExpectedMessageType::aggregate_relay,
        {1, 3, 4},
        after_us(130)));
    CHECK(bridge.diagnostics().pending_reports == 1);
    CHECK(bridge.diagnostics().retained_facts == 2);
    CHECK(bridge.diagnostics().retention_capacity == 2);

    std::vector<EvidenceReportEnvelope> delivered;
    bridge.bind_transport(
        [&delivered](const EvidenceReportEnvelope &envelope) {
            delivered.push_back(envelope);
            return EvidenceTransportResult::accepted;
        });
    REQUIRE(delivered.size() == 3);
    CHECK(delivered[0].observation.outcome == ResponseOutcome::on_time);
    CHECK(delivered[1].observation.outcome == ResponseOutcome::timeout);
    CHECK(delivered[2].observation.outcome == ResponseOutcome::late);
    CHECK(delivered[1].observation.observation_id ==
          delivered[2].observation.observation_id);
    CHECK(bridge.diagnostics().retained_facts == 0);
}

TEST_CASE(
    "adaptive-v2 reserved capacity persists a late fact when both queues are full",
    "[adaptive-v2][response-evidence][retention][capacity]")
{
    AdaptiveV2ResponseEvidenceBridge bridge(0, limits(16, 1, 1));
    const auto key = proposal("retention-capacity");
    REQUIRE(bridge.arm(key, response_tree(), kStartNs, kDeadlineUs));

    REQUIRE(bridge.record_verified_response(
        key,
        2,
        ExpectedMessageType::direct_vote,
        {2},
        after_us(10)));
    CHECK(bridge.record_timeouts(key, {1}, after_us(100)) == 1);
    CHECK(bridge.diagnostics().pending_reports == 1);
    CHECK(bridge.diagnostics().retained_facts == 1);

    REQUIRE(bridge.record_verified_response(
        key,
        1,
        ExpectedMessageType::aggregate_relay,
        {1, 3, 4},
        after_us(130)));
    const auto blocked = bridge.diagnostics();
    CHECK(blocked.pending_reports == 1);
    CHECK(blocked.retained_facts == 1);
    CHECK(blocked.pending_late_compensations == 1);
    CHECK(blocked.retention_capacity_failures == 0);
    CHECK(blocked.response_facts == 2);

    std::vector<EvidenceReportEnvelope> delivered;
    bridge.bind_transport(
        [&delivered](const EvidenceReportEnvelope &envelope) {
            delivered.push_back(envelope);
            return EvidenceTransportResult::accepted;
        });
    REQUIRE(delivered.size() == 3);
    CHECK(delivered[0].observation.outcome == ResponseOutcome::on_time);
    CHECK(delivered[1].observation.outcome == ResponseOutcome::timeout);
    CHECK(delivered[2].observation.outcome == ResponseOutcome::late);
    CHECK(delivered[1].observation.observation_id ==
          delivered[2].observation.observation_id);
    CHECK(bridge.diagnostics().pending_late_compensations == 0);
}

TEST_CASE(
    "adaptive-v2 temporary transport failure schedules an automatic retry",
    "[adaptive-v2][response-evidence][transport][retry]")
{
    AdaptiveV2ResponseEvidenceBridge bridge(0, limits());
    const auto key = proposal("automatic-retry");
    REQUIRE(bridge.arm(key, response_tree(), kStartNs, kDeadlineUs));

    std::vector<std::function<void()>> scheduled_retries;
    bridge.bind_retry_scheduler(
        [&scheduled_retries](std::function<void()> retry) {
            scheduled_retries.push_back(std::move(retry));
            return [] {};
        });

    std::size_t delivery_attempts = 0;
    bridge.bind_transport(
        [&delivery_attempts](const EvidenceReportEnvelope &) {
            ++delivery_attempts;
            return delivery_attempts == 1
                       ? EvidenceTransportResult::temporary_failure
                       : EvidenceTransportResult::accepted;
        });

    REQUIRE(bridge.record_verified_response(
        key,
        2,
        ExpectedMessageType::direct_vote,
        {2},
        after_us(10)));
    REQUIRE(delivery_attempts == 1);
    REQUIRE(scheduled_retries.size() == 1);
    CHECK(bridge.diagnostics().retry_scheduled);
    CHECK(bridge.diagnostics().pending_reports == 1);

    auto retry = std::move(scheduled_retries.front());
    scheduled_retries.clear();
    retry();

    CHECK(delivery_attempts == 2);
    CHECK(bridge.diagnostics().pending_reports == 0);
    CHECK_FALSE(bridge.diagnostics().retry_scheduled);
    CHECK(bridge.front() == nullptr);
}

TEST_CASE(
    "adaptive-v2 HotStuff wiring observes only verified exact runtime seams",
    "[adaptive-v2][response-evidence][wiring]")
{
    const auto implementation = source("src/hotstuff.cpp");
    const auto hotstuff_header = source("include/hotstuff/hotstuff.h");
    const auto experiment_header =
        source("include/hotstuff/experiment_byzantine_adapter.h");

    const auto evidence_enqueue = function_slice(
        implementation,
        "EvidenceTransportResult HotStuffBase::"
        "enqueue_adaptive_v2_evidence_report",
        "void HotStuffBase::enqueue_initial_adaptive_v2_readiness");
    const auto initialization_retry = evidence_enqueue.find(
        "try_enqueue_adaptive_v2_runtime_initialized_report");
    const auto evidence_outbox = evidence_enqueue.find(
        "enqueue_evidence", initialization_retry);
    REQUIRE(initialization_retry != std::string::npos);
    REQUIRE(evidence_outbox != std::string::npos);
    CHECK(initialization_retry < evidence_outbox);
    CHECK(evidence_enqueue.find(
              "adaptive_v2_lifecycle_reporting_suppressed") <
          evidence_outbox);

    const auto runtime_initialized = function_slice(
        implementation,
        "void HotStuffBase::report_adaptive_v2_runtime_initialized",
        "void HotStuffBase::report_adaptive_v2_committed");
    const auto durable_initialization = runtime_initialized.find(
        "adaptive_v2_durable_initialization_reports.emplace");
    const auto initialization_enqueue = runtime_initialized.find(
        "try_enqueue_adaptive_v2_runtime_initialized_report",
        durable_initialization);
    REQUIRE(durable_initialization != std::string::npos);
    REQUIRE(initialization_enqueue != std::string::npos);
    CHECK(durable_initialization < initialization_enqueue);
    CHECK(runtime_initialized.find(
              "runtime_initialization_capacity_exceeded") !=
          std::string::npos);

    const auto start_deadline = function_slice(
        implementation,
        "bool HotStuffBase::start_latency_deadline",
        "void HotStuffBase::start_aggregation_timer");
    CHECK(start_deadline.find("aggregation_timeout_policy.timeout_for") !=
          std::string::npos);
    CHECK(start_deadline.find("adaptive_evidence_monotonic_now_ns()") !=
          std::string::npos);
    CHECK(start_deadline.find("->arm_with_deadline") !=
          std::string::npos);
    const auto false_report_branch =
        start_deadline.find("false_report_target.has_value()");
    const auto experiment_arm = start_deadline.find(
        "adaptive_v2_response_evidence->arm(", false_report_branch);
    const auto normal_arm = start_deadline.find(
        "->arm_with_deadline", false_report_branch);
    REQUIRE(false_report_branch != std::string::npos);
    REQUIRE(experiment_arm != std::string::npos);
    REQUIRE(normal_arm != std::string::npos);
    CHECK(experiment_arm < normal_arm);
    const auto arm_marker = start_deadline.find(
        "KAURI_EVIDENCE response_attempt_armed", normal_arm);
    REQUIRE(arm_marker != std::string::npos);
    CHECK(normal_arm < arm_marker);
    CHECK(start_deadline.find(
              "is_tiered_responsive_degraded_actor", normal_arm) <
          arm_marker);
    CHECK(start_deadline.find(
              "required_child_subtrees.find(child)", normal_arm) <
          arm_marker);
    CHECK(start_deadline.find("reporter=%u child=%u", arm_marker) !=
          std::string::npos);
    CHECK(start_deadline.find("epoch_digest=%s block=%s", arm_marker) !=
          std::string::npos);
    CHECK(start_deadline.find(
              "expected_message_type=%s", arm_marker) !=
          std::string::npos);
    CHECK(start_deadline.find(
              "start_monotonic_ns=%llu", arm_marker) !=
          std::string::npos);
    CHECK(start_deadline.find(
              "deadline_duration_us=%llu", arm_marker) !=
          std::string::npos);
    CHECK(start_deadline.find(
              "absolute_deadline_ns=%llu", arm_marker) !=
          std::string::npos);
    CHECK(start_deadline.find("response_attempt_arm_duplicate") !=
          std::string::npos);
    CHECK(start_deadline.find("response_attempt_arm_marker_failed") !=
          std::string::npos);

    const auto contribution = function_slice(
        implementation,
        "void HotStuffBase::continue_exact_contribution",
        "bool HotStuffBase::publish_exact_root_qc");
    const auto rejected = contribution.find("if (!accepted)");
    const auto redundant_guard = contribution.find(
        "VerifiedAggregateCertificateDisposition::redundant");
    const auto evidence_only_call = contribution.find(
        "record_verified_response", redundant_guard);
    const auto evidence_only_marker = contribution.find(
        "verified_redundant_aggregate_evidence_only");
    REQUIRE(rejected != std::string::npos);
    REQUIRE(redundant_guard != std::string::npos);
    REQUIRE(evidence_only_call != std::string::npos);
    REQUIRE(evidence_only_marker != std::string::npos);
    CHECK(rejected < redundant_guard);
    CHECK(redundant_guard < evidence_only_call);
    CHECK(evidence_only_call < evidence_only_marker);
    CHECK(contribution.find("consensus_accepted=0", evidence_only_marker) !=
          std::string::npos);
    CHECK(contribution.find(
              "response_fact_recorded=%u", evidence_only_marker) !=
          std::string::npos);
    CHECK(contribution.find("positive_suppressed=%u", evidence_only_marker) !=
          std::string::npos);
    const auto evidence_only_end = contribution.find(
        "if (proposal_contexts->delta_open_enabled", evidence_only_marker);
    REQUIRE(evidence_only_end != std::string::npos);
    const auto evidence_only = contribution.substr(
        redundant_guard, evidence_only_end - redundant_guard);
    CHECK(evidence_only.find("may_suppress") != std::string::npos);
    CHECK(evidence_only.find("!suppress_positive_observation") !=
          std::string::npos);
    CHECK(evidence_only.find("synchronize_experiment_post_qc_audit") ==
          std::string::npos);
    CHECK(evidence_only.find("record_exact_latency") == std::string::npos);
    CHECK(evidence_only.find("try_finish") == std::string::npos);
    CHECK(evidence_only.find("publish_exact_root_qc") == std::string::npos);
    CHECK(evidence_only.find("send_exact_relay") == std::string::npos);
    CHECK(evidence_only.find("claim_unforwarded_certificate") ==
          std::string::npos);

    const auto first_probe_call = contribution.find(
        "record_verified_response", evidence_only_call + 1);
    const auto second_probe_call = contribution.find(
        "record_verified_response",
        first_probe_call + 1);
    const auto probe_marker = contribution.find(
        "KAURI_EXPERIMENT response_duplicate_probe");
    REQUIRE(first_probe_call != std::string::npos);
    REQUIRE(second_probe_call != std::string::npos);
    REQUIRE(probe_marker != std::string::npos);
    CHECK(first_probe_call < second_probe_call);
    CHECK(second_probe_call < probe_marker);
    CHECK(contribution.find(
              "record_verified_response",
              second_probe_call + 1) == std::string::npos);
    CHECK(contribution.find("first_call_recorded &&") !=
          std::string::npos);
    CHECK(contribution.find("first_call_recorded") != std::string::npos);
    CHECK(contribution.find("second_call_recorded") != std::string::npos);
    CHECK(contribution.find("consensus_accepted=1") != std::string::npos);
    CHECK(contribution.find("message_type=aggregate_relay") !=
          std::string::npos);
    CHECK(contribution.find("response_monotonic_ns=%llu") !=
          std::string::npos);
    CHECK(contribution.find("window_end_monotonic_ns=%llu") !=
          std::string::npos);
    CHECK(experiment_header.find(
              "exact_once_post_fault_epoch1_responsive_internal_child_v1") !=
          std::string::npos);
    CHECK(contribution.find(
              "kExperimentResponseEvidenceDuplicateProbeMode") !=
          std::string::npos);
    CHECK(contribution.find(
              "lease.key().configuration.epoch_number == 1") !=
          std::string::npos);
    CHECK(contribution.find(
              "kind == ExactContributionKind::aggregate_relay") !=
          std::string::npos);
    CHECK(contribution.find(
              "is_tiered_responsive_degraded_actor") !=
          std::string::npos);
    CHECK(contribution.find("child_subtree->second.size() > 1") !=
          std::string::npos);
    CHECK(contribution.find("response_monotonic_ns >=") !=
          std::string::npos);
    CHECK(contribution.find("compare_exchange_strong") !=
          std::string::npos);
    const auto aggregate_record = contribution.find(
        "record_verified_aggregate_certificate");
    REQUIRE(aggregate_record != std::string::npos);
    CHECK(contribution.find(
              "record_verified_aggregate_certificate",
              aggregate_record + 1) == std::string::npos);
    CHECK(contribution.find("lease.key()", first_probe_call) <
          second_probe_call);
    CHECK(contribution.find("lease.key()", second_probe_call) <
          probe_marker);
    CHECK(contribution.find(
              "contribution.authenticated_sender", first_probe_call) <
          second_probe_call);
    CHECK(contribution.find(
              "contribution.authenticated_sender", second_probe_call) <
          probe_marker);
    CHECK(contribution.find("contribution_signers", first_probe_call) <
          second_probe_call);
    CHECK(contribution.find("contribution_signers", second_probe_call) <
          probe_marker);
    CHECK(contribution.find("response_monotonic_ns", first_probe_call) <
          second_probe_call);
    CHECK(contribution.find("response_monotonic_ns", second_probe_call) <
          probe_marker);
    CHECK(contribution.find("epoch_digest.to_hex()", probe_marker) !=
          std::string::npos);
    CHECK(contribution.find("block_hash.to_hex()", probe_marker) !=
          std::string::npos);

    CHECK(hotstuff_header.find(
              "experiment_response_evidence_duplicate_probe_consumed") !=
          std::string::npos);
    const auto configure_probe = function_slice(
        implementation,
        "void HotStuffBase::configure_experiment_byzantine_faults",
        "void HotStuffBase::configure_experiment_post_qc_audit");
    CHECK(configure_probe.find(
              "kExperimentResponseEvidenceDuplicateProbeMode") !=
          std::string::npos);
    CHECK(configure_probe.find(
              "tiered_persistent_responsive_omission_v2") !=
          std::string::npos);
    CHECK(configure_probe.find("window_end_monotonic_ns") !=
          std::string::npos);
    CHECK(configure_probe.find(
              "experiment_response_evidence_duplicate_probe_consumed.store(false)") !=
          std::string::npos);

    const auto timeout = function_slice(
        implementation,
        "void HotStuffBase::record_aggregation_timeout",
        "void HotStuffBase::record_optional_aggregation_absence");
    const auto exact_fact = timeout.find(
        "adaptive_v2_response_evidence->record_timeouts");
    const auto v2_guard = timeout.find(
        "epoch_protocol_mode == EpochProtocolMode::adaptive_v2");
    const auto legacy_send = timeout.find("MsgTimeoutReport");
    REQUIRE(exact_fact != std::string::npos);
    REQUIRE(v2_guard != std::string::npos);
    REQUIRE(legacy_send != std::string::npos);
    CHECK(exact_fact < v2_guard);
    CHECK(v2_guard < legacy_send);
    CHECK(timeout.find("const auto recorded") != std::string::npos);
    CHECK(timeout.find("diagnostics()") != std::string::npos);
    CHECK(timeout.find("[EVIDENCE] Timeout bridge") !=
          std::string::npos);

    const auto optional_absence = function_slice(
        implementation,
        "void HotStuffBase::record_optional_aggregation_absence",
        "void HotStuffBase::emit_adaptive_aggregation_event");
    CHECK(optional_absence.find("record_timeouts") == std::string::npos);
    CHECK(optional_absence.find("MsgTimeoutReport") == std::string::npos);

    const auto purge = function_slice(
        implementation,
        "void HotStuffBase::purge_pending_exact_contributions",
        "promise_t HotStuffBase::deliver_exact_contribution");
    CHECK(purge.find(
              "preserve_response_evidence_until_deadline") !=
          std::string::npos);
    CHECK(purge.find("close_consensus_context") !=
          std::string::npos);
    CHECK(purge.find("adaptive_v2_response_evidence->retire") !=
          std::string::npos);
    const auto durable_lookup = purge.find(
        "has_durable_adaptive_v2_commit_report");
    const auto durable_close = purge.find(
        "close_consensus_context", durable_lookup);
    const auto generic_retire = purge.find(
        "adaptive_v2_response_evidence->retire", durable_close);
    REQUIRE(durable_lookup != std::string::npos);
    REQUIRE(durable_close != std::string::npos);
    REQUIRE(generic_retire != std::string::npos);
    CHECK(durable_lookup < durable_close);
    CHECK(durable_close < generic_retire);
    CHECK(purge.find("durable_false_report_commit") !=
          std::string::npos);

    const auto commit = function_slice(
        implementation,
        "void HotStuffBase::do_consensus_with_identity_provenance(",
        "void HotStuffBase::do_decide");
    const auto authoritative_guard = commit.find(
        "authoritative_key.has_value() &&");
    const auto exact_authoritative_match = commit.find(
        "key == *authoritative_key", authoritative_guard);
    const auto preserve_cleanup = commit.find(
        "preserve_authoritative_response_evidence",
        exact_authoritative_match);
    REQUIRE(authoritative_guard != std::string::npos);
    REQUIRE(exact_authoritative_match != std::string::npos);
    REQUIRE(preserve_cleanup != std::string::npos);
    CHECK(authoritative_guard < exact_authoritative_match);
    CHECK(exact_authoritative_match < preserve_cleanup);

    const auto commit_report = function_slice(
        implementation,
        "void HotStuffBase::report_adaptive_v2_committed",
        "void HotStuffBase::observe_adaptive_v2_response_deadline_result");
    const auto defer_check = commit_report.find(
        "should_defer_commit_report");
    const auto durable_commit = commit_report.find(
        "persist_adaptive_v2_commit_report");
    const auto committed_enqueue = commit_report.find(
        "try_enqueue_adaptive_v2_commit_report", durable_commit);
    REQUIRE(defer_check != std::string::npos);
    REQUIRE(durable_commit != std::string::npos);
    REQUIRE(committed_enqueue != std::string::npos);
    CHECK(defer_check < durable_commit);
    CHECK(durable_commit < committed_enqueue);
    CHECK(commit_report.find("enqueue_lifecycle") ==
          std::string::npos);

    const auto deadline_result = function_slice(
        implementation,
        "void HotStuffBase::observe_adaptive_v2_response_deadline_result",
        "bool HotStuffBase::persist_adaptive_v2_commit_report");
    const auto failed_tombstone = deadline_result.find(
        "AdaptiveV2DurableCommitPhase::suppressed");
    const auto persist_tombstone = deadline_result.find(
        "persist_adaptive_v2_commit_report", failed_tombstone);
    const auto failed_convergence_poison = deadline_result.find(
        "mark_adaptive_v2_convergence_evidence_unhealthy",
        persist_tombstone);
    const auto failed_convergence_reason = deadline_result.find(
        "response_deadline_evidence_failed",
        failed_convergence_poison);
    REQUIRE(failed_tombstone != std::string::npos);
    REQUIRE(persist_tombstone != std::string::npos);
    REQUIRE(failed_convergence_poison != std::string::npos);
    REQUIRE(failed_convergence_reason != std::string::npos);
    CHECK(failed_tombstone < persist_tombstone);
    CHECK(persist_tombstone < failed_convergence_poison);
    CHECK(failed_convergence_poison < failed_convergence_reason);

    const auto enqueue_deferred = function_slice(
        implementation,
        "bool HotStuffBase::try_enqueue_adaptive_v2_commit_report",
        "void HotStuffBase::\n"
        "    retry_ready_adaptive_v2_commit_reports");
    const auto enqueue_lifecycle = enqueue_deferred.find(
        "enqueue_lifecycle");
    const auto queued_status = enqueue_deferred.find(
        "AdaptiveV2ReportingEnqueueStatus::queued", enqueue_lifecycle);
    const auto erase_deferred = enqueue_deferred.find(
        "adaptive_v2_durable_commit_reports.erase",
        queued_status);
    const auto capacity_status = enqueue_deferred.find(
        "AdaptiveV2ReportingEnqueueStatus::capacity_exceeded",
        erase_deferred);
    REQUIRE(enqueue_lifecycle != std::string::npos);
    REQUIRE(queued_status != std::string::npos);
    REQUIRE(erase_deferred != std::string::npos);
    REQUIRE(capacity_status != std::string::npos);
    CHECK(enqueue_lifecycle < queued_status);
    CHECK(queued_status < erase_deferred);
    CHECK(erase_deferred < capacity_status);

    const auto false_release = function_slice(
        implementation,
        "void HotStuffBase::release_experiment_false_report_commit",
        "void HotStuffBase::start_aggregation_timer");
    const auto false_ready = false_release.find(
        "ready.experiment_false_report = true");
    const auto false_persist = false_release.find(
        "persist_adaptive_v2_commit_report", false_ready);
    const auto false_erase = false_release.find(
        "experiment_false_timeout_states.erase", false_persist);
    const auto false_enqueue = false_release.find(
        "try_enqueue_adaptive_v2_commit_report", false_erase);
    REQUIRE(false_ready != std::string::npos);
    REQUIRE(false_persist != std::string::npos);
    REQUIRE(false_erase != std::string::npos);
    REQUIRE(false_enqueue != std::string::npos);
    CHECK(false_ready < false_persist);
    CHECK(false_persist < false_erase);
    CHECK(false_erase < false_enqueue);
    CHECK(false_release.find("enqueue_lifecycle") == std::string::npos);

    const auto no_deferred_commit = false_release.find(
        "ExperimentFalseTimeoutCompletionAction::\n"
        "                    no_deferred_commit");
    const auto precommit_evidence_missing = false_release.find(
        "!evidence_queued_before_commit", no_deferred_commit);
    const auto precommit_tombstone = false_release.find(
        "AdaptiveV2DurableCommitPhase::suppressed",
        precommit_evidence_missing);
    const auto precommit_persist = false_release.find(
        "persist_adaptive_v2_commit_report", precommit_tombstone);
    const auto precommit_erase = false_release.find(
        "experiment_false_timeout_states.erase", precommit_persist);
    const auto precommit_fail_closed = false_release.find(
        "mark_adaptive_v2_convergence_evidence_unhealthy",
        precommit_erase);
    REQUIRE(no_deferred_commit != std::string::npos);
    REQUIRE(precommit_evidence_missing != std::string::npos);
    REQUIRE(precommit_tombstone != std::string::npos);
    REQUIRE(precommit_persist != std::string::npos);
    REQUIRE(precommit_erase != std::string::npos);
    REQUIRE(precommit_fail_closed != std::string::npos);
    CHECK(no_deferred_commit < precommit_evidence_missing);
    CHECK(precommit_evidence_missing < precommit_tombstone);
    CHECK(precommit_tombstone < precommit_persist);
    CHECK(precommit_persist < precommit_erase);
    CHECK(precommit_erase < precommit_fail_closed);

    const auto convergence_commit = function_slice(
        implementation,
        "void HotStuffBase::enqueue_pending_adaptive_v2_commit_observation",
        "void HotStuffBase::enqueue_pending_adaptive_v2_activation_observation");
    const auto lifecycle_gate = convergence_commit.find(
        "has_pending_adaptive_v2_lifecycle_fence");
    const auto convergence_enqueue = convergence_commit.find(
        "enqueue_convergence_observation", lifecycle_gate);
    REQUIRE(lifecycle_gate != std::string::npos);
    REQUIRE(convergence_enqueue != std::string::npos);
    CHECK(lifecycle_gate < convergence_enqueue);

    const auto post_commit = function_slice(
        implementation,
        "void HotStuffBase::do_post_block_commit",
        "void HotStuffBase::rotate_adaptive_v2_after_commit");
    const auto retained_convergence_identity = post_commit.find(
        "adaptive_v2_committed_convergence_identity");
    const auto gated_convergence_call = post_commit.find(
        "enqueue_pending_adaptive_v2_commit_observation",
        retained_convergence_identity);
    REQUIRE(retained_convergence_identity != std::string::npos);
    REQUIRE(gated_convergence_call != std::string::npos);
    CHECK(retained_convergence_identity < gated_convergence_call);
    CHECK(post_commit.find("enqueue_epoch_change_committed") ==
          std::string::npos);

    const auto convergence_activation = function_slice(
        implementation,
        "void HotStuffBase::enqueue_pending_adaptive_v2_activation_observation",
        "ReplicaStageIngressResult HotStuffBase::trusted_local_stage_epoch");
    const auto activation_gate = convergence_activation.find(
        "has_pending_adaptive_v2_lifecycle_fence");
    const auto activation_enqueue = convergence_activation.find(
        "enqueue_epoch_activated", activation_gate);
    REQUIRE(activation_gate != std::string::npos);
    REQUIRE(activation_enqueue != std::string::npos);
    CHECK(activation_gate < activation_enqueue);

    const auto reporting_flush = function_slice(
        implementation,
        "void HotStuffBase::flush_adaptive_v2_reporting",
        "void HotStuffBase::mark_adaptive_v2_convergence_evidence_unhealthy");
    const auto retry_initializations = reporting_flush.find(
        "retry_ready_adaptive_v2_runtime_initialized_reports");
    const auto retry_commits = reporting_flush.find(
        "retry_ready_adaptive_v2_commit_reports", retry_initializations);
    const auto retry_convergence = reporting_flush.find(
        "enqueue_pending_adaptive_v2_commit_observation", retry_commits);
    REQUIRE(retry_initializations != std::string::npos);
    REQUIRE(retry_commits != std::string::npos);
    REQUIRE(retry_convergence != std::string::npos);
    CHECK(retry_initializations < retry_commits);
    CHECK(retry_commits < retry_convergence);

    const auto failed_terminal = reporting_flush.find(
        "AdaptiveV2ReportingDeliveryState::failed");
    const auto terminal_poison = reporting_flush.find(
        "poison_adaptive_v2_reporting(", failed_terminal);
    const auto delivery_failed_transition = reporting_flush.rfind(
        "AdaptiveV2ReportingTransitionStatus::failed");
    const auto delivery_poison = reporting_flush.find(
        "poison_adaptive_v2_reporting(", delivery_failed_transition);
    REQUIRE(failed_terminal != std::string::npos);
    REQUIRE(terminal_poison != std::string::npos);
    REQUIRE(delivery_failed_transition != std::string::npos);
    REQUIRE(delivery_poison != std::string::npos);
    CHECK(failed_terminal < terminal_poison);
    CHECK(delivery_failed_transition < delivery_poison);

    const auto reporting_poison = function_slice(
        implementation,
        "void HotStuffBase::poison_adaptive_v2_reporting",
        "HotStuffBase::transmit_adaptive_v2_report");
    const auto poison_suppression = reporting_poison.find(
        "suppress_adaptive_v2_lifecycle_reporting(reason)");
    const auto poison_shutdown = reporting_poison.find(
        "adaptive_v2_reporting_outbox->shutdown()", poison_suppression);
    const auto poison_cancel = reporting_poison.find(
        "cancel_adaptive_v2_reporting_flush()", poison_shutdown);
    REQUIRE(poison_suppression != std::string::npos);
    REQUIRE(poison_shutdown != std::string::npos);
    REQUIRE(poison_cancel != std::string::npos);
    CHECK(poison_suppression < poison_shutdown);
    CHECK(poison_shutdown < poison_cancel);

    const auto convergence_ack = function_slice(
        implementation,
        "void HotStuffBase::adaptive_v2_convergence_ack_handler",
        "void HotStuffBase::configure_epoch_manager");
    const auto rejected_ack = convergence_ack.find(
        "AdaptiveV2ReportingTransitionStatus::failed");
    const auto ack_fail_closed = convergence_ack.find(
        "poison_adaptive_v2_reporting", rejected_ack);
    REQUIRE(rejected_ack != std::string::npos);
    REQUIRE(ack_fail_closed != std::string::npos);
    CHECK(rejected_ack < ack_fail_closed);

    const auto retirement = function_slice(
        implementation,
        "void HotStuffBase::advance_committed_retirement_floor",
        "void HotStuffBase::retire_deferred_epoch_changes_for_block");
    CHECK(retirement.find("retire_before_epoch") == std::string::npos);
    CHECK(retirement.find(
              "release_adaptive_v2_response_commit") ==
          std::string::npos);

    const auto forwarding_abort = function_slice(
        implementation,
        "void HotStuffBase::abort_exact_forwarding",
        "void HotStuffBase::discard_exact_forwarding_retries");
    CHECK(forwarding_abort.find("purge_pending_exact_contributions") !=
          std::string::npos);
    CHECK(forwarding_abort.find(
              "purge_pending_exact_contributions(lease.key(), true)") ==
          std::string::npos);
    CHECK(forwarding_abort.find("pending_exact_contributions.purge") ==
          std::string::npos);
}

TEST_CASE(
    "HotStuff exposes an adaptive-v2-only no-authority evidence transport seam",
    "[adaptive-v2][response-evidence][api]")
{
    const auto header = source("include/hotstuff/hotstuff.h");
    const auto bridge_header = source(
        "include/hotstuff/adaptive_v2_response_evidence.h");
    const auto implementation = source("src/hotstuff.cpp");

    CHECK(header.find("bind_adaptive_v2_evidence_transport") !=
          std::string::npos);
    CHECK(header.find("unbind_adaptive_v2_evidence_transport") !=
          std::string::npos);
    CHECK(header.find("flush_adaptive_v2_evidence") !=
          std::string::npos);
    CHECK(bridge_header.find("EvidenceRetryScheduler") !=
          std::string::npos);
    CHECK(bridge_header.find("EvidenceDeadlineScheduler") !=
          std::string::npos);

    const auto bind = function_slice(
        implementation,
        "void HotStuffBase::bind_adaptive_v2_evidence_transport",
        "void HotStuffBase::unbind_adaptive_v2_evidence_transport");
    CHECK(bind.find(
              "epoch_protocol_mode != EpochProtocolMode::adaptive_v2") !=
          std::string::npos);
    CHECK(bind.find("bind_transport") != std::string::npos);
    CHECK(bind.find("rn.send_msg") == std::string::npos);
    CHECK(bind.find("stage_epoch") == std::string::npos);
    CHECK(bind.find("change_epoch") == std::string::npos);

    const auto constructor = function_slice(
        implementation,
        "HotStuffBase::HotStuffBase",
        "void HotStuffBase::install_legacy_consensus_handlers");
    CHECK(constructor.find("bind_retry_scheduler") !=
          std::string::npos);
    CHECK(constructor.find("bind_deadline_scheduler") !=
          std::string::npos);
    CHECK(constructor.find("bind_deadline_result_callback") !=
          std::string::npos);
    CHECK(constructor.find("schedule_at_or_after_deadline") !=
          std::string::npos);
    const auto deadline_callback = constructor.find(
        "deadline(");
    REQUIRE(deadline_callback != std::string::npos);
    CHECK(constructor.find(
              "release_adaptive_v2_response_commit_report") ==
          std::string::npos);
    CHECK(constructor.find("aggregation_scheduler->schedule_after") !=
          std::string::npos);
    CHECK(constructor.find("exact_runtime_access") !=
          std::string::npos);

    const auto destructor = function_slice(
        implementation,
        "HotStuffBase::~HotStuffBase()",
        "void HotStuffBase::tree_config");
    const auto close_gate = destructor.find(
        "exact_runtime_access->close_and_wait()");
    const auto bridge_shutdown = destructor.find(
        "adaptive_v2_response_evidence->shutdown()");
    REQUIRE(close_gate != std::string::npos);
    REQUIRE(bridge_shutdown != std::string::npos);
    CHECK(close_gate < bridge_shutdown);
}
