#include <cstdint>
#include <fstream>
#include <functional>
#include <iterator>
#include <limits>
#include <set>
#include <string>
#include <vector>

#include "catch.hpp"
#include "hotstuff/adaptive_v2_response_evidence.h"

#ifndef KAURI_PROJECT_SOURCE_DIR
#error "KAURI_PROJECT_SOURCE_DIR must name the repository root"
#endif

namespace
{

using hotstuff::AdaptiveV2ResponseEvidenceBridge;
using hotstuff::AdaptiveV2ResponseEvidenceLimits;
using hotstuff::ConfigurationId;
using hotstuff::DataStream;
using hotstuff::EvidenceReportEnvelope;
using hotstuff::EvidenceTransportResult;
using hotstuff::ExpectedMessageType;
using hotstuff::ProposalKey;
using hotstuff::ProposalTreeSnapshot;
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

std::uint64_t after_us(std::uint64_t microseconds)
{
    return kStartNs + microseconds * kNanosecondsPerMicrosecond;
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
    "adaptive-v2 timeout keeps the exact attempt open for a late compensation fact",
    "[adaptive-v2][response-evidence][late]")
{
    AdaptiveV2ResponseEvidenceBridge bridge(6, limits());
    const auto key = proposal("late-response");
    REQUIRE(bridge.arm(key, response_tree(), kStartNs, kDeadlineUs));

    CHECK(bridge.record_timeouts(key, {1}, after_us(100)) == 1);
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
    "adaptive-v2 wait-exempt absence and rejected response inputs produce no fact",
    "[adaptive-v2][response-evidence][neutral]")
{
    AdaptiveV2ResponseEvidenceBridge bridge(0, limits());
    const auto key = proposal("neutral-absence");
    REQUIRE(bridge.arm(key, response_tree(), kStartNs, kDeadlineUs));

    CHECK(bridge.record_timeouts(key, {5}, after_us(100)) == 0);
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

    const auto start_deadline = function_slice(
        implementation,
        "void HotStuffBase::start_latency_deadline",
        "void HotStuffBase::start_aggregation_timer");
    CHECK(start_deadline.find("aggregation_timeout_policy.timeout_for") !=
          std::string::npos);
    CHECK(start_deadline.find("adaptive_monotonic_now_ns()") !=
          std::string::npos);
    CHECK(start_deadline.find("adaptive_v2_response_evidence->arm") !=
          std::string::npos);

    const auto contribution = function_slice(
        implementation,
        "void HotStuffBase::continue_exact_contribution",
        "bool HotStuffBase::publish_exact_root_qc");
    const auto rejected = contribution.find("if (!accepted)");
    const auto observed = contribution.find(
        "adaptive_v2_response_evidence->record_verified_response");
    REQUIRE(rejected != std::string::npos);
    REQUIRE(observed != std::string::npos);
    CHECK(rejected < observed);
    CHECK(contribution.substr(rejected, observed - rejected).find(
              "return;") != std::string::npos);
    CHECK(contribution.find("contribution.authenticated_sender", observed) !=
          std::string::npos);
    CHECK(contribution.find("contribution_signers", observed) !=
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
    CHECK(purge.find("adaptive_v2_response_evidence->retire") !=
          std::string::npos);

    const auto forwarding_abort = function_slice(
        implementation,
        "void HotStuffBase::abort_exact_forwarding",
        "void HotStuffBase::discard_exact_forwarding_retries");
    CHECK(forwarding_abort.find("purge_pending_exact_contributions") !=
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
    CHECK(constructor.find("aggregation_scheduler->schedule_after") !=
          std::string::npos);
    CHECK(constructor.find("exact_runtime_access") !=
          std::string::npos);
}
