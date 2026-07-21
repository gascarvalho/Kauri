#include <algorithm>
#include <array>
#include <cstddef>
#include <cstdint>
#include <limits>
#include <optional>
#include <set>
#include <string>
#include <type_traits>
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
using hotstuff::AdaptiveV2EpochChangeCommittedObservation;
using hotstuff::AdaptiveV2EpochChangeIdentity;
using hotstuff::AdaptiveV2ManagerControllerStatus;
using hotstuff::AdaptiveV2ManagerConvergenceDisposition;
using hotstuff::AdaptiveV2ManagerConvergenceStatus;
using hotstuff::AdaptiveV2ManagerDeliveryRequest;
using hotstuff::AdaptiveV2ManagerIngressStatus;
using hotstuff::AdaptiveV2ManagerSession;
using hotstuff::AdaptiveV2ManagerSessionConfig;
using hotstuff::AdaptiveV2ManagerSessionTerminalRecord;
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
using hotstuff::bytearray_t;

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

template<typename Session, typename = void>
struct has_session_ingestion : std::false_type
{};

template<typename Session>
struct has_session_ingestion<
    Session,
    std::void_t<
        decltype(std::declval<Session &>().ingest_readiness(
            std::declval<const AuthenticatedReporter &>(),
            std::declval<const bytearray_t &>())),
        decltype(std::declval<Session &>().ingest_lifecycle(
            std::declval<const AuthenticatedReporter &>(),
            std::declval<const bytearray_t &>())),
        decltype(std::declval<Session &>().ingest_evidence(
            std::declval<const AuthenticatedReporter &>(),
            std::declval<const bytearray_t &>()))>> : std::true_type
{};

template<typename Session>
auto route_readiness(
    Session &session,
    const AuthenticatedReporter &source,
    const bytearray_t &payload)
{
    if constexpr (has_session_ingestion<Session>::value)
        return session.ingest_readiness(source, payload);
    else
        return session.ingress().ingest_readiness(source, payload);
}

template<typename Session>
auto route_lifecycle(
    Session &session,
    const AuthenticatedReporter &source,
    const bytearray_t &payload)
{
    if constexpr (has_session_ingestion<Session>::value)
        return session.ingest_lifecycle(source, payload);
    else
        return session.ingress().ingest_lifecycle(source, payload);
}

template<typename Session>
auto route_evidence(
    Session &session,
    const AuthenticatedReporter &source,
    const bytearray_t &payload)
{
    if constexpr (has_session_ingestion<Session>::value)
        return session.ingest_evidence(source, payload);
    else
        return session.ingress().ingest_evidence(source, payload);
}

template<typename Session, typename = void>
struct has_complete_convergence_forwarding : std::false_type
{};

template<typename Session>
struct has_complete_convergence_forwarding<
    Session,
    std::void_t<
        decltype(std::declval<Session &>().due_deliveries(
            std::uint64_t{})),
        decltype(std::declval<Session &>().record_enqueue_result(
            ReplicaID{}, std::uint32_t{}, bool{})),
        decltype(std::declval<Session &>().observe_commit(
            ReplicaID{},
            std::declval<
                const AdaptiveV2EpochChangeCommittedObservation &>())),
        decltype(std::declval<const Session &>().convergence_status())>>
    : std::true_type
{};

template<typename Session, typename = void>
struct has_independent_convergence_clock : std::false_type
{};

template<typename Session>
struct has_independent_convergence_clock<
    Session,
    std::void_t<decltype(
        std::declval<Session &>().start_convergence(
            std::uint64_t{}, std::uint64_t{}))>> : std::true_type
{};

template<typename Session, typename = void>
struct has_precommit_convergence_start : std::false_type
{};

template<typename Session>
struct has_precommit_convergence_start<
    Session,
    std::void_t<decltype(
        std::declval<Session &>().start_convergence(
            std::uint64_t{}))>> : std::true_type
{};

template<typename Record, typename = void>
struct has_complete_terminal_record : std::false_type
{};

template<typename Record>
struct has_complete_terminal_record<
    Record,
    std::void_t<
        decltype(std::declval<const Record &>().outcome),
        decltype(std::declval<const Record &>().reason),
        decltype(std::declval<const Record &>()
                     .successor_epoch_number.has_value()),
        decltype(std::declval<const Record &>()
                     .successor_epoch_digest.has_value()),
        decltype(std::declval<const Record &>()
                     .command_payload_digest.has_value()),
        decltype(std::declval<const Record &>()
                     .winning_activation.has_value())>> : std::true_type
{};

template<typename Session, typename Record, typename = void>
struct has_cycle_finalizers : std::false_type
{};

template<typename Session, typename Record>
struct has_cycle_finalizers<
    Session,
    Record,
    std::void_t<
        decltype(std::declval<Session &>().finalize_noop_cycle(
            std::declval<decltype(
                std::declval<const Record &>().reason)>())),
        decltype(std::declval<Session &>().finalize_failed_cycle(
            std::declval<decltype(
                std::declval<const Record &>().reason)>()))>>
    : std::true_type
{};

template<typename Session, typename = void>
struct has_session_rotation : std::false_type
{};

template<typename Session>
struct has_session_rotation<
    Session,
    std::void_t<decltype(std::declval<Session &>().rotate_to_successor(
        std::declval<const EpochDefinitionInput &>(),
        std::uint32_t{}))>> : std::true_type
{};

template<typename Session, typename = void>
struct has_convergence_accessor : std::false_type
{};

template<typename Session>
struct has_convergence_accessor<
    Session,
    std::void_t<decltype(
        std::declval<Session &>().convergence())>> : std::true_type
{};

template<typename Session, typename = void>
struct has_bounded_execution_audit : std::false_type
{};

template<typename Session>
struct has_bounded_execution_audit<
    Session,
    std::void_t<
        decltype(std::declval<const Session &>()
                     .controller_audit()
                     .has_value()),
        decltype(std::declval<const Session &>()
                     .controller_audit()
                     ->baseline_cutoff),
        decltype(std::declval<const Session &>()
                     .controller_audit()
                     ->current_cutoff),
        decltype(std::declval<const Session &>()
                     .controller_audit()
                     ->score_trajectory.size()),
        decltype(std::declval<const Session &>()
                     .convergence_audit()
                     .has_value()),
        decltype(std::declval<const Session &>()
                     .convergence_audit()
                     ->status),
        decltype(std::declval<const Session &>()
                     .convergence_audit()
                     ->accepted_commit_count),
        decltype(std::declval<const Session &>()
                     .convergence_audit()
                     ->accepted_activation_count),
        decltype(std::declval<const Session &>()
                     .convergence_audit()
                     ->winning_activation_count),
        decltype(std::declval<const Session &>()
                     .convergence_audit()
                     ->winning_identity.has_value()),
        decltype(std::declval<const Session &>()
                     .convergence_audit()
                     ->winning_activation_sources.size()),
        decltype(std::declval<Session &>().shutdown())>>
    : std::true_type
{};

template<typename Value, typename = void>
struct is_optional_like : std::false_type
{};

template<typename Value>
struct is_optional_like<
    Value,
    std::void_t<
        decltype(std::declval<const Value &>().has_value()),
        decltype(*std::declval<const Value &>())>> : std::true_type
{};

template<typename Value>
const auto &terminal_value(const Value &value)
{
    if constexpr (is_optional_like<Value>::value)
    {
        REQUIRE(value.has_value());
        return *value;
    }
    else
    {
        return value;
    }
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
                static_cast<std::uint64_t>(
                    1'000'000 + readiness_sequences[source] * 100 +
                    source)};
            CHECK(route_readiness(
                      session,
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
            const auto result = route_lifecycle(
                session,
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
        const auto result = route_evidence(
            session,
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

    AdaptiveV2EpochChangeIdentity prepare_convergence(
        const AdaptiveV2TransitionPolicy &policy,
        std::uint64_t command_height,
        std::uint64_t logical_start_tick)
    {
        REQUIRE(session.begin_cycle(policy));
        CHECK(session.evaluate() ==
              (session.ingress().operationally_ready()
                   ? AdaptiveV2ManagerControllerStatus::
                         awaiting_responsive_baseline
                   : AdaptiveV2ManagerControllerStatus::
                         awaiting_readiness));
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
        REQUIRE(session.start_convergence(logical_start_tick));
        return identity;
    }

    AdaptiveV2EpochChangeIdentity complete_cycle(
        const AdaptiveV2TransitionPolicy &policy,
        std::uint64_t command_height,
        std::uint64_t logical_start_tick)
    {
        const auto identity = prepare_convergence(
            policy, command_height, logical_start_tick);
        for (const auto source : kSurvivors)
        {
            const AdaptiveV2EpochChangeCommittedObservation committed{
                hotstuff::kAdaptiveV2ConvergenceObservationSchemaVersionV1,
                source,
                identity};
            CHECK(session.observe_commit(source, committed) ==
                  AdaptiveV2ManagerConvergenceDisposition::accepted);
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

template<typename Session>
void verify_read_only_ingress_contract()
{
    using IngressResult =
        decltype(std::declval<Session &>().ingress());
    if constexpr (!std::is_same<
                      IngressResult,
                      const hotstuff::AdaptiveV2ManagerIngress &>::value)
    {
        FAIL(
            "M12-R01 RED: session ingress() is mutable and exposes "
            "rotate_to_successor outside terminal finalization");
    }
    else if constexpr (!has_session_ingestion<Session>::value)
    {
        FAIL(
            "M12-R01 RED: const-only ingress requires session-owned "
            "readiness/lifecycle/evidence forwarding");
    }
    else
    {
        CHECK_FALSE(has_session_rotation<Session>::value);
        CHECK_FALSE(has_convergence_accessor<Session>::value);
        CHECK((std::is_same<
               decltype(std::declval<const Session &>()
                            .terminal_records()),
               const std::vector<
                   AdaptiveV2ManagerSessionTerminalRecord> &>::value));
    }
}

template<typename Session>
void verify_complete_convergence_contract()
{
    if constexpr (!has_complete_convergence_forwarding<Session>::value)
    {
        FAIL(
            "M12-R01 RED: session cannot drive delivery retry, enqueue, "
            "commit, and convergence status through owned forwarding");
    }
    else
    {
        Fixture fixture;
        Session &session = fixture.session;
        const auto identity = fixture.prepare_convergence(
            containment_policy(), 100, 100);
        const auto *bundle = session.successor_bundle();
        REQUIRE(bundle != nullptr);

        const auto first = session.due_deliveries(100);
        REQUIRE(first.size() == kMembers.size());
        for (const auto &request : first)
        {
            CHECK(request.attempt == 1);
            REQUIRE(request.canonical_bundle_bytes != nullptr);
            CHECK(*request.canonical_bundle_bytes ==
                  bundle->canonical_bytes());
            CHECK(session.record_enqueue_result(
                      request.recipient,
                      request.attempt,
                      true) ==
                  AdaptiveV2ManagerConvergenceDisposition::
                      advisory_enqueue_recorded);
        }

        const auto retry = session.due_deliveries(102);
        REQUIRE(retry.size() == kMembers.size());
        for (const auto &request : retry)
        {
            CHECK(request.attempt == 2);
            CHECK(request.canonical_bundle_bytes ==
                  first.front().canonical_bundle_bytes);
        }

        const AdaptiveV2EpochActivatedObservation first_from_zero{
            hotstuff::kAdaptiveV2ConvergenceObservationSchemaVersionV1,
            0,
            identity,
            identity.successor_epoch_number,
            identity.successor_epoch_digest};
        const AdaptiveV2EpochChangeCommittedObservation commit_from_zero{
            hotstuff::kAdaptiveV2ConvergenceObservationSchemaVersionV1,
            0,
            identity};
        CHECK(session.observe_commit(0, commit_from_zero) ==
              AdaptiveV2ManagerConvergenceDisposition::accepted);
        CHECK(session.observe_activation(0, first_from_zero) ==
              AdaptiveV2ManagerConvergenceDisposition::accepted);
        auto conflicting_identity = identity;
        conflicting_identity.command_block_hash =
            digest("quarantined-source-conflict");
        const AdaptiveV2EpochActivatedObservation conflict_from_zero{
            hotstuff::kAdaptiveV2ConvergenceObservationSchemaVersionV1,
            0,
            conflicting_identity,
            conflicting_identity.successor_epoch_number,
            conflicting_identity.successor_epoch_digest};
        CHECK(session.observe_activation(0, conflict_from_zero) ==
              AdaptiveV2ManagerConvergenceDisposition::
                  conflicting_observation);
        REQUIRE(session.convergence_status().has_value());
        CHECK(*session.convergence_status() ==
              AdaptiveV2ManagerConvergenceStatus::awaiting_activations);
        CHECK(session.terminal_records().empty());

        for (const auto source : kSurvivors)
        {
            const AdaptiveV2EpochChangeCommittedObservation committed{
                hotstuff::kAdaptiveV2ConvergenceObservationSchemaVersionV1,
                source,
                identity};
            CHECK(session.observe_commit(source, committed) ==
                  AdaptiveV2ManagerConvergenceDisposition::accepted);
            const AdaptiveV2EpochActivatedObservation activated{
                hotstuff::kAdaptiveV2ConvergenceObservationSchemaVersionV1,
                source,
                identity,
                identity.successor_epoch_number,
                identity.successor_epoch_digest};
            CHECK(session.observe_activation(source, activated) ==
                  AdaptiveV2ManagerConvergenceDisposition::accepted);
        }

        const auto status = session.convergence_status();
        REQUIRE(status.has_value());
        CHECK(*status == AdaptiveV2ManagerConvergenceStatus::
                             ready_for_optimization);
        REQUIRE(session.consume_ready_and_rotate());
        CHECK_FALSE(session.consume_ready_and_rotate());
        CHECK(session.terminal_records().size() == 1);
        CHECK_FALSE(has_convergence_accessor<Session>::value);
    }
}

template<typename Session>
void verify_independent_convergence_clock_contract()
{
    if constexpr (!has_precommit_convergence_start<Session>::value)
    {
        FAIL(
            "M12-R02 RED: start_convergence must depend only on the "
            "logical retry clock");
    }
    else
    {
        CHECK_FALSE(has_independent_convergence_clock<Session>::value);
        constexpr std::uint64_t command_block_height = 1'000;
        constexpr std::uint64_t logical_start_tick = 7;

        Fixture fixture;
        Session &session = fixture.session;
        REQUIRE(session.begin_cycle(containment_policy()));
        CHECK(session.evaluate() ==
              AdaptiveV2ManagerControllerStatus::awaiting_readiness);
        fixture.ready_all();
        fixture.responsive_baseline();
        REQUIRE(session.evaluate() ==
                AdaptiveV2ManagerControllerStatus::baseline_frozen);
        fixture.persistent_timeouts();
        REQUIRE(session.evaluate() ==
                AdaptiveV2ManagerControllerStatus::successor_ready);
        const auto *bundle = session.successor_bundle();
        REQUIRE(bundle != nullptr);
        const auto identity = identity_for(
            *bundle,
            command_block_height,
            "independent-convergence-clock");

        REQUIRE(session.start_convergence(logical_start_tick));
        CHECK(session.due_deliveries(logical_start_tick - 1).empty());
        CHECK(session.terminal_records().empty());

        const auto initial =
            session.due_deliveries(logical_start_tick);
        REQUIRE(initial.size() == kMembers.size());
        for (const auto &request : initial)
            CHECK(request.attempt == 1);

        CHECK(session.due_deliveries(logical_start_tick + 1).empty());
        const auto retry =
            session.due_deliveries(logical_start_tick + 2);
        REQUIRE(retry.size() == kMembers.size());
        for (const auto &request : retry)
            CHECK(request.attempt == 2);

        const AdaptiveV2EpochChangeCommittedObservation committed{
            hotstuff::kAdaptiveV2ConvergenceObservationSchemaVersionV1,
            2,
            identity};
        CHECK(session.observe_commit(2, committed) ==
              AdaptiveV2ManagerConvergenceDisposition::accepted);
        const AdaptiveV2EpochActivatedObservation activated{
            hotstuff::kAdaptiveV2ConvergenceObservationSchemaVersionV1,
            2,
            identity,
            identity.successor_epoch_number,
            identity.successor_epoch_digest};
        CHECK(session.observe_activation(2, activated) ==
              AdaptiveV2ManagerConvergenceDisposition::accepted);
        REQUIRE(session.convergence_status().has_value());
        CHECK(*session.convergence_status() ==
              AdaptiveV2ManagerConvergenceStatus::awaiting_activations);

        const auto deadline = logical_start_tick +
            session_config().convergence_window_ticks;
        CHECK(session.due_deliveries(deadline).empty());
        REQUIRE(session.terminal_records().size() == 1);
        const auto &record = session.terminal_records().front();
        CHECK(record.outcome ==
              hotstuff::AdaptiveV2ManagerCycleOutcome::failed);
        CHECK(record.reason ==
              hotstuff::AdaptiveV2ManagerCycleTerminalReason::
                  convergence_retry_exhausted);
        CHECK(session.ingress().current_epoch().epoch_number() == 0);
    }
}

template<typename Session>
void verify_precommit_delivery_contract()
{
    if constexpr (!has_precommit_convergence_start<Session>::value)
    {
        FAIL(
            "M12-R02 RED: start_convergence(logical_start_tick) is "
            "absent; bundle delivery still requires a guessed pre-commit "
            "command height");
    }
    else
    {
        const auto prepare = [](
                                 Fixture &fixture,
                                 Session &session,
                                 std::uint64_t logical_start_tick) {
            REQUIRE(session.begin_cycle(containment_policy()));
            CHECK(session.evaluate() ==
                  AdaptiveV2ManagerControllerStatus::awaiting_readiness);
            fixture.ready_all();
            fixture.responsive_baseline();
            REQUIRE(session.evaluate() ==
                    AdaptiveV2ManagerControllerStatus::baseline_frozen);
            fixture.persistent_timeouts();
            REQUIRE(session.evaluate() ==
                    AdaptiveV2ManagerControllerStatus::successor_ready);
            REQUIRE(session.successor_bundle() != nullptr);
            REQUIRE(session.start_convergence(logical_start_tick));
        };

        constexpr std::uint64_t logical_start_tick = 40;
        constexpr std::uint64_t command_height = 1'200;
        Fixture successful;
        Session &session = successful.session;
        prepare(successful, session, logical_start_tick);
        const auto *bundle = session.successor_bundle();
        REQUIRE(bundle != nullptr);
        CHECK(bundle->definition().activation_height == 0);
        CHECK(session.terminal_records().empty());
        CHECK(session.due_deliveries(logical_start_tick - 1).empty());

        const auto initial =
            session.due_deliveries(logical_start_tick);
        REQUIRE(initial.size() == kMembers.size());
        const auto canonical_owner =
            initial.front().canonical_bundle_owner;
        REQUIRE(canonical_owner != nullptr);
        for (const auto &request : initial)
        {
            CHECK(request.attempt == 1);
            CHECK(request.canonical_bundle_owner == canonical_owner);
            CHECK(request.canonical_bundle_bytes == canonical_owner.get());
            REQUIRE(request.canonical_bundle_bytes != nullptr);
            CHECK(*request.canonical_bundle_bytes ==
                  bundle->canonical_bytes());
        }

        const auto observed = identity_for(
            *bundle,
            command_height,
            "m12-r02-observed-command-block");
        CHECK(observed.command_block_height != 0);
        CHECK(observed.command_block_hash != uint256_t{});
        for (const auto source : kSurvivors)
        {
            const AdaptiveV2EpochChangeCommittedObservation committed{
                hotstuff::kAdaptiveV2ConvergenceObservationSchemaVersionV1,
                source,
                observed};
            CHECK(session.observe_commit(source, committed) ==
                  AdaptiveV2ManagerConvergenceDisposition::accepted);
        }
        for (const auto source : kSurvivors)
        {
            const AdaptiveV2EpochActivatedObservation activated{
                hotstuff::kAdaptiveV2ConvergenceObservationSchemaVersionV1,
                source,
                observed,
                observed.successor_epoch_number,
                observed.successor_epoch_digest};
            CHECK(session.observe_activation(source, activated) ==
                  AdaptiveV2ManagerConvergenceDisposition::accepted);
        }
        REQUIRE(session.convergence_status().has_value());
        CHECK(*session.convergence_status() ==
              AdaptiveV2ManagerConvergenceStatus::ready_for_optimization);
        REQUIRE(session.consume_ready_and_rotate());
        REQUIRE(session.terminal_records().size() == 1);
        const auto &terminal = session.terminal_records().front();
        REQUIRE(terminal.winning_activation.has_value());
        CHECK(*terminal.winning_activation == observed);
        CHECK(terminal.winning_activation->command_block_height ==
              command_height);
        CHECK(terminal.winning_activation->command_block_hash ==
              observed.command_block_hash);

        Fixture rejected;
        Session &rejected_session = rejected.session;
        constexpr std::uint64_t rejected_start_tick = 80;
        prepare(rejected, rejected_session, rejected_start_tick);
        const auto *rejected_bundle = rejected_session.successor_bundle();
        REQUIRE(rejected_bundle != nullptr);
        auto invalid = identity_for(
            *rejected_bundle,
            command_height,
            "m12-r02-invalid-command-block");

        auto zero_height = invalid;
        zero_height.command_block_height = 0;
        zero_height.activation_height =
            zero_height.activation_delay_blocks;
        CHECK(rejected_session.observe_commit(
                  2,
                  AdaptiveV2EpochChangeCommittedObservation{
                      hotstuff::
                          kAdaptiveV2ConvergenceObservationSchemaVersionV1,
                      2,
                      zero_height}) ==
              AdaptiveV2ManagerConvergenceDisposition::
                  rejected_wrong_identity);

        auto wrong_bundle = invalid;
        wrong_bundle.command_payload_digest =
            digest("m12-r02-wrong-bundle");
        CHECK(rejected_session.observe_commit(
                  3,
                  AdaptiveV2EpochChangeCommittedObservation{
                      hotstuff::
                          kAdaptiveV2ConvergenceObservationSchemaVersionV1,
                      3,
                      wrong_bundle}) ==
              AdaptiveV2ManagerConvergenceDisposition::
                  rejected_wrong_identity);

        auto overflow = invalid;
        overflow.command_block_height =
            std::numeric_limits<std::uint64_t>::max() -
            overflow.activation_delay_blocks + 1;
        overflow.activation_height =
            std::numeric_limits<std::uint64_t>::max();
        CHECK(rejected_session.observe_commit(
                  4,
                  AdaptiveV2EpochChangeCommittedObservation{
                      hotstuff::
                          kAdaptiveV2ConvergenceObservationSchemaVersionV1,
                      4,
                      overflow}) ==
              AdaptiveV2ManagerConvergenceDisposition::
                  rejected_wrong_identity);
        REQUIRE(rejected_session.convergence_status().has_value());
        CHECK(*rejected_session.convergence_status() ==
              AdaptiveV2ManagerConvergenceStatus::awaiting_activations);
        CHECK(rejected_session.terminal_records().empty());
        CHECK(rejected_session.ingress().current_epoch().epoch_number() == 0);

        Fixture conflicting;
        Session &conflicting_session = conflicting.session;
        constexpr std::uint64_t conflicting_start_tick = 120;
        prepare(
            conflicting,
            conflicting_session,
            conflicting_start_tick);
        const auto *conflicting_bundle =
            conflicting_session.successor_bundle();
        REQUIRE(conflicting_bundle != nullptr);
        const auto committed_identity = identity_for(
            *conflicting_bundle,
            command_height,
            "m12-r02-conflicting-height");
        CHECK(conflicting_session.observe_commit(
                  2,
                  AdaptiveV2EpochChangeCommittedObservation{
                      hotstuff::
                          kAdaptiveV2ConvergenceObservationSchemaVersionV1,
                      2,
                      committed_identity}) ==
              AdaptiveV2ManagerConvergenceDisposition::accepted);

        auto conflicting_height = committed_identity;
        ++conflicting_height.command_block_height;
        ++conflicting_height.activation_height;
        CHECK(conflicting_session.observe_commit(
                  2,
                  AdaptiveV2EpochChangeCommittedObservation{
                      hotstuff::
                          kAdaptiveV2ConvergenceObservationSchemaVersionV1,
                      2,
                      conflicting_height}) ==
              AdaptiveV2ManagerConvergenceDisposition::
                  conflicting_observation);

        for (const auto source : kSurvivors)
        {
            if (source != 2)
            {
                const AdaptiveV2EpochChangeCommittedObservation committed{
                    hotstuff::
                        kAdaptiveV2ConvergenceObservationSchemaVersionV1,
                    source,
                    committed_identity};
                CHECK(conflicting_session.observe_commit(
                          source, committed) ==
                      AdaptiveV2ManagerConvergenceDisposition::accepted);
            }
            const AdaptiveV2EpochActivatedObservation activated{
                hotstuff::kAdaptiveV2ConvergenceObservationSchemaVersionV1,
                source,
                committed_identity,
                committed_identity.successor_epoch_number,
                committed_identity.successor_epoch_digest};
            const auto disposition =
                conflicting_session.observe_activation(source, activated);
            CHECK(disposition ==
                  (source == 2
                       ? AdaptiveV2ManagerConvergenceDisposition::
                             conflicting_observation
                       : AdaptiveV2ManagerConvergenceDisposition::accepted));
        }
        REQUIRE(conflicting_session.convergence_status().has_value());
        CHECK(*conflicting_session.convergence_status() ==
              AdaptiveV2ManagerConvergenceStatus::awaiting_activations);
        CHECK_FALSE(conflicting_session.consume_ready_and_rotate());
        CHECK(conflicting_session.due_deliveries(
                  conflicting_start_tick +
                  session_config().convergence_window_ticks)
                  .empty());
        REQUIRE(conflicting_session.terminal_records().size() == 1);
        CHECK(conflicting_session.terminal_records().front().outcome ==
              hotstuff::AdaptiveV2ManagerCycleOutcome::failed);
        CHECK(conflicting_session.ingress().current_epoch().epoch_number() ==
              0);
    }
}

template<typename Session, typename Record>
void verify_terminal_outcome_contract()
{
    if constexpr (!has_complete_terminal_record<Record>::value ||
                  !has_cycle_finalizers<Session, Record>::value ||
                  !has_complete_convergence_forwarding<Session>::value)
    {
        FAIL(
            "M12-R01 RED: session terminal records cannot represent and "
            "finalize advanced, failed, and explicit no-op cycles");
    }
    else
    {
        using Outcome = std::decay_t<decltype(
            std::declval<const Record &>().outcome)>;
        using Reason = std::decay_t<decltype(
            std::declval<const Record &>().reason)>;

        Fixture advanced;
        Session &advanced_session = advanced.session;
        const auto predecessor_digest =
            advanced_session.ingress().current_epoch().epoch_digest();
        const auto identity = advanced.complete_cycle(
            containment_policy(), 100, 100);
        REQUIRE(advanced_session.terminal_records().size() == 1);
        const Record advanced_record =
            advanced_session.terminal_records().front();
        CHECK(advanced_record.outcome == Outcome::advanced);
        CHECK(advanced_record.reason == Reason::successor_converged);
        CHECK(advanced_record.predecessor_epoch_number == 0);
        CHECK(advanced_record.predecessor_epoch_digest ==
              predecessor_digest);
        CHECK(terminal_value(advanced_record.successor_epoch_number) == 1);
        CHECK(terminal_value(advanced_record.winning_activation) ==
              identity);
        CHECK(advanced_session.ingress().current_epoch().epoch_number() == 1);
        CHECK_FALSE(advanced_session.consume_ready_and_rotate());
        CHECK(advanced_session.terminal_records().size() == 1);

        Fixture noop;
        Session &noop_session = noop.session;
        const auto noop_epoch =
            noop_session.ingress().current_epoch().epoch_digest();
        REQUIRE(noop_session.begin_cycle(containment_policy()));
        noop.ready_all();
        noop.responsive_baseline();
        REQUIRE(noop_session.evaluate() ==
                AdaptiveV2ManagerControllerStatus::baseline_frozen);
        REQUIRE(noop_session.ingress().ledger().high_watermark() > 0);
        REQUIRE(noop_session.finalize_noop_cycle(
            Reason::explicit_no_op));
        REQUIRE(noop_session.terminal_records().size() == 1);
        const Record noop_record = noop_session.terminal_records().front();
        CHECK(noop_record.outcome == Outcome::no_op);
        CHECK(noop_record.reason == Reason::explicit_no_op);
        CHECK(noop_record.predecessor_epoch_digest == noop_epoch);
        CHECK_FALSE(noop_record.successor_epoch_number.has_value());
        CHECK_FALSE(noop_record.command_payload_digest.has_value());
        CHECK_FALSE(noop_record.winning_activation.has_value());
        CHECK(noop_session.ingress().current_epoch().epoch_number() == 0);
        CHECK(noop_session.ingress().current_epoch().epoch_digest() ==
              noop_epoch);
        CHECK(noop_session.ingress().ledger().accepted().empty());
        CHECK_FALSE(noop_session.ingress().all_members_ready());
        CHECK_FALSE(noop_session.finalize_noop_cycle(
            Reason::explicit_no_op));
        CHECK(noop_session.terminal_records().size() == 1);

        const AdaptiveV2ReadinessNotice replayed_readiness{
            hotstuff::kAdaptiveV2ReadinessNoticeSchemaVersionV1,
            0,
            noop.readiness_sequences[0],
            noop_session.ingress().current_configuration(),
            noop_session.ingress().activation_generation(),
            999};
        CHECK(route_readiness(
                  noop_session,
                  AuthenticatedReporter{0},
                  hotstuff::encode_adaptive_v2_readiness_notice(
                      replayed_readiness,
                      session_config().ingress_limits.readiness_wire))
                  .status ==
              AdaptiveV2ManagerIngressStatus::rejected_sequence);

        auto replayed_evidence = noop.make_observation(
            0, 0, ResponseOutcome::on_time, "noop-replay");
        const auto replayed_reporter = replayed_evidence.reporter_id;
        const ProposalLifecycleNotice replayed_lifecycle{
            hotstuff::kProposalLifecycleNoticeSchemaVersion,
            0,
            noop.lifecycle_sequences[0],
            ProposalLifecycleFact{NormalProposalRuntimeInitialized{
                replayed_evidence.proposal_key()}}};
        CHECK(route_lifecycle(
                  noop_session,
                  AuthenticatedReporter{0},
                  hotstuff::encode_proposal_lifecycle_notice(
                      replayed_lifecycle,
                      session_config().ingress_limits.lifecycle_wire))
                  .status ==
              AdaptiveV2ManagerIngressStatus::rejected_sequence);

        REQUIRE(noop.evidence_sequences[replayed_reporter] > 1);
        replayed_evidence.reporter_sequence =
            noop.evidence_sequences[replayed_reporter] - 1;
        replayed_evidence.reporter_monotonic_ns =
            replayed_evidence.reporter_sequence * 1'000;
        replayed_evidence.observation_id =
            hotstuff::compute_response_observation_id(
                replayed_evidence.attempt_identity());
        const auto replayed_evidence_result = route_evidence(
            noop_session,
            AuthenticatedReporter{replayed_reporter},
            hotstuff::encode_evidence_batch(
                ResponseObservationBatch{
                    hotstuff::kEvidenceBatchSchemaVersion,
                    {replayed_evidence}},
                session_config().ingress_limits.evidence_wire));
        CHECK(replayed_evidence_result.status ==
              AdaptiveV2ManagerIngressStatus::processed);
        CHECK(replayed_evidence_result.accepted_observations == 0);
        CHECK(replayed_evidence_result.rejected_observations == 1);
        CHECK(noop_session.ingress().ledger().accepted().empty());

        REQUIRE(noop_session.begin_cycle(optimization_policy()));
        CHECK(noop_session.evaluate() ==
              AdaptiveV2ManagerControllerStatus::awaiting_readiness);
        REQUIRE(noop_session.finalize_failed_cycle(
            Reason::caller_failed));
        REQUIRE(noop_session.terminal_records().size() == 2);
        CHECK(noop_session.terminal_records().back().outcome ==
              Outcome::failed);
        CHECK(noop_session.terminal_records().back().reason ==
              Reason::caller_failed);

        Fixture failed;
        Session &failed_session = failed.session;
        const auto failed_epoch =
            failed_session.ingress().current_epoch().epoch_digest();
        failed.prepare_convergence(containment_policy(), 300, 300);
        const auto pending = failed_session.convergence_status();
        REQUIRE(pending.has_value());
        CHECK(*pending == AdaptiveV2ManagerConvergenceStatus::
                              awaiting_activations);
        CHECK(failed_session.due_deliveries(400).empty());
        REQUIRE(failed_session.terminal_records().size() == 1);
        const Record failed_record =
            failed_session.terminal_records().front();
        CHECK(failed_record.outcome == Outcome::failed);
        CHECK(failed_record.reason ==
              Reason::convergence_retry_exhausted);
        CHECK(failed_record.predecessor_epoch_digest == failed_epoch);
        CHECK(failed_session.ingress().current_epoch().epoch_number() == 0);
        CHECK(failed_session.ingress().current_epoch().epoch_digest() ==
              failed_epoch);
        CHECK_FALSE(failed_session.finalize_failed_cycle(
            Reason::convergence_retry_exhausted));
        CHECK(failed_session.terminal_records().size() == 1);
        CHECK_FALSE(failed_session.begin_cycle(optimization_policy()));
    }
}

template<typename Session>
void verify_bounded_execution_audit_contract()
{
    if constexpr (!has_bounded_execution_audit<Session>::value)
    {
        FAIL(
            "M12-R02 RED: the session lacks bounded read-only controller "
            "and convergence audit snapshots plus an owned shutdown seam");
    }
    else
    {
        Fixture fixture;
        Session &session = fixture.session;
        const auto identity = fixture.prepare_convergence(
            containment_policy(), 1'400, 140);
        const Session &read_only = session;

        const auto controller = read_only.controller_audit();
        REQUIRE(controller.has_value());
        CHECK(controller->baseline_cutoff > 0);
        CHECK(controller->current_cutoff >= controller->baseline_cutoff);
        CHECK_FALSE(controller->score_trajectory.empty());
        CHECK(controller->score_trajectory.size() <=
              session_config().controller.reputation_limits
                  .maximum_audit_updates);

        auto convergence = read_only.convergence_audit();
        REQUIRE(convergence.has_value());
        CHECK(convergence->status ==
              AdaptiveV2ManagerConvergenceStatus::awaiting_activations);
        CHECK(convergence->accepted_commit_count == 0);
        CHECK(convergence->accepted_activation_count == 0);
        CHECK(convergence->winning_activation_count == 0);
        CHECK_FALSE(convergence->winning_identity.has_value());
        CHECK(convergence->winning_activation_sources.empty());

        for (const auto source : kSurvivors)
        {
            CHECK(session.observe_commit(
                      source,
                      AdaptiveV2EpochChangeCommittedObservation{
                          hotstuff::
                              kAdaptiveV2ConvergenceObservationSchemaVersionV1,
                          source,
                          identity}) ==
                  AdaptiveV2ManagerConvergenceDisposition::accepted);
            CHECK(session.observe_activation(
                      source,
                      AdaptiveV2EpochActivatedObservation{
                          hotstuff::
                              kAdaptiveV2ConvergenceObservationSchemaVersionV1,
                          source,
                          identity,
                          identity.successor_epoch_number,
                          identity.successor_epoch_digest}) ==
                  AdaptiveV2ManagerConvergenceDisposition::accepted);
        }

        convergence = read_only.convergence_audit();
        REQUIRE(convergence.has_value());
        CHECK(convergence->status ==
              AdaptiveV2ManagerConvergenceStatus::ready_for_optimization);
        CHECK(convergence->accepted_commit_count == 5);
        CHECK(convergence->accepted_activation_count == 5);
        CHECK(convergence->winning_activation_count == 5);
        REQUIRE(convergence->winning_identity.has_value());
        CHECK(*convergence->winning_identity == identity);
        CHECK(convergence->winning_activation_sources == kSurvivors);

        Fixture stopping;
        Session &stopped = stopping.session;
        stopped.shutdown();
        CHECK_FALSE(stopped.ingress().healthy());
        CHECK_FALSE(stopped.begin_cycle(containment_policy()));

        Fixture active_stopping;
        Session &active = active_stopping.session;
        REQUIRE(active.begin_cycle(containment_policy()));
        active.shutdown();
        REQUIRE(active.terminal_records().size() == 1);
        CHECK(active.terminal_records().front().outcome ==
              hotstuff::AdaptiveV2ManagerCycleOutcome::failed);
        CHECK(active.terminal_records().front().reason ==
              hotstuff::AdaptiveV2ManagerCycleTerminalReason::
                  caller_failed);
        CHECK(active.ingress().current_epoch().epoch_number() == 0);
        CHECK_FALSE(active.ingress().healthy());
        active.shutdown();
        CHECK(active.terminal_records().size() == 1);
    }
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
            policies[cycle],
            100 + cycle * 10,
            100 + cycle * 10);

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
        CHECK(terminal_value(record.successor_epoch_number) == cycle + 1);
        CHECK(terminal_value(record.successor_epoch_digest) ==
              identity.successor_epoch_digest);
        CHECK(terminal_value(record.command_payload_digest) ==
              identity.command_payload_digest);
        CHECK(terminal_value(record.winning_activation) == identity);

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
              AdaptiveV2ManagerConvergenceDisposition::duplicate);
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

TEST_CASE(
    "session exposes ingress read-only and owns every mutating route",
    "[adaptive-v2][manager-session][ownership][ingress][intentional-red]")
{
    verify_read_only_ingress_contract<AdaptiveV2ManagerSession>();
}

TEST_CASE(
    "session forwards the complete one-shot convergence lifecycle",
    "[adaptive-v2][manager-session][convergence][lifecycle][intentional-red]")
{
    verify_complete_convergence_contract<
        AdaptiveV2ManagerSession>();
}

TEST_CASE(
    "convergence command height and logical clock are independent",
    "[adaptive-v2][manager-session][convergence][clock][intentional-red]")
{
    verify_independent_convergence_clock_contract<
        AdaptiveV2ManagerSession>();
}

TEST_CASE(
    "session delivers the immutable successor before commit identity exists",
    "[adaptive-v2][manager-session][precommit-delivery][m12-r02]"
    "[intentional-red]")
{
    verify_precommit_delivery_contract<AdaptiveV2ManagerSession>();
}

TEST_CASE(
    "Q-winning activations seed the exact fresh successor readiness window",
    "[adaptive-v2][manager-session][successor-readiness][quorum][n7]"
    "[m12-r02][intentional-red]")
{
    Fixture fixture;
    const auto predecessor_configuration =
        fixture.session.ingress().current_configuration();
    const auto predecessor_generation =
        fixture.session.ingress().activation_generation();
    const auto identity = fixture.complete_cycle(
        containment_policy(), 1'300, 130);

    const auto seeded = fixture.session.ingress().readiness_stats();
    REQUIRE(seeded.ready_members == 5);
    CHECK(seeded.accepted_notices == 0);
    CHECK(seeded.rejected_notices == 0);
    CHECK_FALSE(seeded.all_members_ready);
    CHECK(fixture.session.ingress().operationally_ready());
    CHECK(fixture.session.ingress().membership() == kMembers);
    CHECK(fixture.session.ingress().quorum_metadata().replica_count == 7);
    CHECK(fixture.session.ingress().quorum_metadata().fault_threshold == 2);
    CHECK(fixture.session.ingress().quorum_metadata().quorum == 5);

    const auto successor_configuration =
        fixture.session.ingress().current_configuration();
    const auto successor_generation =
        fixture.session.ingress().activation_generation();
    REQUIRE(identity.activation_height > 0);

    const auto unseeded = [&](std::uint64_t sequence,
                              std::uint64_t height) {
        return route_readiness(
            fixture.session,
            AuthenticatedReporter{0},
            hotstuff::encode_adaptive_v2_readiness_notice(
                AdaptiveV2ReadinessNotice{
                    hotstuff::
                        kAdaptiveV2ReadinessNoticeSchemaVersionV1,
                    0,
                    sequence,
                    successor_configuration,
                    successor_generation,
                    height},
                session_config().ingress_limits.readiness_wire));
    };
    CHECK(unseeded(2, identity.activation_height - 1).status ==
          AdaptiveV2ManagerIngressStatus::rejected_height_regression);
    const auto below_floor =
        fixture.session.ingress().readiness_stats();
    CHECK(below_floor.ready_members == 5);
    CHECK(below_floor.accepted_notices == 0);
    CHECK(below_floor.rejected_notices == 1);
    CHECK_FALSE(below_floor.all_members_ready);
    CHECK(unseeded(2, identity.activation_height).status ==
          AdaptiveV2ManagerIngressStatus::processed);
    const auto accepted_at_floor =
        fixture.session.ingress().readiness_stats();
    CHECK(accepted_at_floor.ready_members == 6);
    CHECK(accepted_at_floor.accepted_notices == 1);
    CHECK(accepted_at_floor.rejected_notices == 1);

    for (const auto source : kSurvivors)
    {
        const auto deliver = [&](std::uint64_t sequence,
                                 std::uint64_t height) {
            return route_readiness(
                fixture.session,
                AuthenticatedReporter{source},
                hotstuff::encode_adaptive_v2_readiness_notice(
                    AdaptiveV2ReadinessNotice{
                        hotstuff::
                            kAdaptiveV2ReadinessNoticeSchemaVersionV1,
                        source,
                        sequence,
                        successor_configuration,
                        successor_generation,
                        height},
                    session_config().ingress_limits.readiness_wire));
        };
        CHECK(deliver(1, identity.activation_height).status ==
              AdaptiveV2ManagerIngressStatus::rejected_sequence);
        CHECK(deliver(2, identity.activation_height - 1).status ==
              AdaptiveV2ManagerIngressStatus::
                  rejected_height_regression);
        CHECK(deliver(2, identity.activation_height).status ==
              AdaptiveV2ManagerIngressStatus::processed);
    }
    CHECK(fixture.session.ingress().readiness_stats().ready_members == 6);

    const AdaptiveV2ReadinessNotice stale_predecessor{
        hotstuff::kAdaptiveV2ReadinessNoticeSchemaVersionV1,
        1,
        2,
        predecessor_configuration,
        predecessor_generation,
        identity.activation_height};
    CHECK(route_readiness(
              fixture.session,
              AuthenticatedReporter{1},
              hotstuff::encode_adaptive_v2_readiness_notice(
                  stale_predecessor,
                  session_config().ingress_limits.readiness_wire))
              .status ==
          AdaptiveV2ManagerIngressStatus::rejected_configuration);
    CHECK(fixture.session.ingress().readiness_stats().ready_members == 6);
    CHECK(fixture.session.ingress().current_epoch().epoch_number() == 1);

    Fixture below_quorum;
    const auto below_identity = below_quorum.prepare_convergence(
        containment_policy(), 1'500, 150);
    for (const auto source :
         std::vector<ReplicaID>{2, 3, 4, 5})
    {
        CHECK(below_quorum.session.observe_commit(
                  source,
                  AdaptiveV2EpochChangeCommittedObservation{
                      hotstuff::
                          kAdaptiveV2ConvergenceObservationSchemaVersionV1,
                      source,
                      below_identity}) ==
              AdaptiveV2ManagerConvergenceDisposition::accepted);
        CHECK(below_quorum.session.observe_activation(
                  source,
                  AdaptiveV2EpochActivatedObservation{
                      hotstuff::
                          kAdaptiveV2ConvergenceObservationSchemaVersionV1,
                      source,
                      below_identity,
                      below_identity.successor_epoch_number,
                      below_identity.successor_epoch_digest}) ==
              AdaptiveV2ManagerConvergenceDisposition::accepted);
    }
    REQUIRE(below_quorum.session.convergence_status().has_value());
    CHECK(*below_quorum.session.convergence_status() ==
          AdaptiveV2ManagerConvergenceStatus::awaiting_activations);
    CHECK_FALSE(below_quorum.session.consume_ready_and_rotate());
    CHECK(below_quorum.session.ingress().current_epoch().epoch_number() == 0);
    CHECK(below_quorum.session.terminal_records().empty());
}

TEST_CASE(
    "session exposes bounded audit snapshots and owned shutdown",
    "[adaptive-v2][manager-session][audit][shutdown][m12-r02]"
    "[intentional-red]")
{
    verify_bounded_execution_audit_contract<AdaptiveV2ManagerSession>();
}

TEST_CASE(
    "every begun cycle appends exactly one immutable terminal outcome",
    "[adaptive-v2][manager-session][terminal][outcome][intentional-red]")
{
    verify_terminal_outcome_contract<
        AdaptiveV2ManagerSession,
        AdaptiveV2ManagerSessionTerminalRecord>();
}

#endif
