#include <algorithm>
#include <cstdint>
#include <stdexcept>
#include <string>
#include <type_traits>
#include <utility>
#include <vector>

#include "catch.hpp"
#include "hotstuff/adaptive_v2_manager_ingress.h"

namespace
{

using hotstuff::AdaptiveV2ManagerIngress;
using hotstuff::AdaptiveV2ManagerIngressLimits;
using hotstuff::AdaptiveV2ManagerLifecycleResult;
using hotstuff::AdaptiveV2ManagerIngressStatus;
using hotstuff::AdaptiveV2ReadinessNotice;
using hotstuff::AuthenticatedReporter;
using hotstuff::ConfigurationId;
using hotstuff::DataStream;
using hotstuff::EpochDefinitionInput;
using hotstuff::EpochTreeDefinition;
using hotstuff::EvidenceLedger;
using hotstuff::ExpectedMessageType;
using hotstuff::MsgAdaptiveV2ReadinessNotice;
using hotstuff::MsgEvidenceReport;
using hotstuff::MsgProposalLifecycleNotice;
using hotstuff::NormalProposalRuntimeInitialized;
using hotstuff::ProposalCommitted;
using hotstuff::ProposalConfigurationRetired;
using hotstuff::ProposalKey;
using hotstuff::ProposalLifecycleApplyStatus;
using hotstuff::ProposalLifecycleFact;
using hotstuff::ProposalLifecycleNotice;
using hotstuff::ProposalRetirementFloorAdvanced;
using hotstuff::ProposalRuntimeAborted;
using hotstuff::ReplicaID;
using hotstuff::ResponseObservation;
using hotstuff::ResponseObservationBatch;
using hotstuff::ResponseOutcome;
using hotstuff::bytearray_t;
using hotstuff::uint256_t;

static_assert(!std::is_copy_constructible<AdaptiveV2ManagerIngress>::value);
static_assert(!std::is_move_constructible<AdaptiveV2ManagerIngress>::value);
static_assert(std::is_same<
    decltype(std::declval<const AdaptiveV2ManagerIngress &>().ledger()),
    const EvidenceLedger &>::value);
static_assert(std::is_same<
    decltype(std::declval<const AdaptiveV2ManagerIngress &>().current_epoch()),
    const hotstuff::EpochDefinition &>::value);

uint256_t digest(const std::string &label)
{
    return DataStream(label).get_hash();
}

std::vector<ReplicaID> membership(std::size_t count = 7)
{
    std::vector<ReplicaID> members;
    for (std::size_t index = 0; index < count; ++index)
        members.push_back(static_cast<ReplicaID>(index));
    return members;
}

EpochDefinitionInput epoch_zero(
    const std::vector<ReplicaID> &members = membership())
{
    return hotstuff::adaptive_v2_epoch_zero_input(
        members,
        {EpochTreeDefinition{0, 2, 2, members, {}}});
}

AdaptiveV2ManagerIngressLimits limits()
{
    AdaptiveV2ManagerIngressLimits configured;
    configured.maximum_members = 7;
    configured.readiness_wire.maximum_payload_bytes = 256;
    configured.lifecycle_wire.maximum_payload_bytes = 512;
    configured.evidence_wire = {4096, 8, 7};
    configured.proposal_index = {32, 8};
    configured.evidence_store = {32, 32};
    configured.lifecycle = {16, 4096, 7, 64, 16, 7};
    configured.lifecycle_accounting = {16, 4096, 64};
    configured.maximum_pending_lifecycle_facts_per_source = 4;
    return configured;
}

AdaptiveV2ReadinessNotice readiness(
    const AdaptiveV2ManagerIngress &manager,
    ReplicaID source,
    std::uint64_t sequence = 1,
    std::uint64_t generation = 3)
{
    return AdaptiveV2ReadinessNotice{
        hotstuff::kAdaptiveV2ReadinessNoticeSchemaVersionV1,
        source,
        sequence,
        manager.current_configuration(),
        generation,
        static_cast<std::uint64_t>(100 + source)};
}

ResponseObservation observation(
    const AdaptiveV2ManagerIngress &manager,
    const std::string &label = "quarantined")
{
    ResponseObservation value;
    value.reporter_id = 1;
    value.observed_replica_id = 3;
    value.configuration = manager.current_configuration();
    value.block_hash = digest(label + "-block");
    value.expected_message_type = ExpectedMessageType::direct_vote;
    value.outcome = ResponseOutcome::on_time;
    value.response_duration_us = 20;
    value.deadline_duration_us = 100;
    value.reporter_monotonic_ns = 1'000;
    value.reporter_sequence = 1;
    value.signer_set = {3};
    value.observation_id = hotstuff::compute_response_observation_id(
        value.attempt_identity());
    return value;
}

ProposalLifecycleNotice admission(
    const ProposalKey &proposal,
    ReplicaID source = 0,
    std::uint64_t sequence = 1)
{
    return ProposalLifecycleNotice{
        hotstuff::kProposalLifecycleNoticeSchemaVersion,
        source,
        sequence,
        ProposalLifecycleFact{
            NormalProposalRuntimeInitialized{proposal}}};
}

ProposalLifecycleNotice lifecycle(
    ProposalLifecycleFact fact,
    ReplicaID source,
    std::uint64_t sequence)
{
    return ProposalLifecycleNotice{
        hotstuff::kProposalLifecycleNoticeSchemaVersion,
        source,
        sequence,
        std::move(fact)};
}

AdaptiveV2ManagerLifecycleResult ingest_lifecycle(
    AdaptiveV2ManagerIngress &manager,
    const AdaptiveV2ManagerIngressLimits &configured,
    const ProposalLifecycleNotice &notice)
{
    return manager.ingest_lifecycle(
        AuthenticatedReporter{notice.source_replica_id},
        hotstuff::encode_proposal_lifecycle_notice(
            notice, configured.lifecycle_wire));
}

} // namespace

TEST_CASE(
    "N7 manager ingress preserves fixed f2 Q5 metadata and epoch zero",
    "[adaptive-v2][manager-ingress][n7][authority]")
{
    AdaptiveV2ManagerIngress manager(
        membership(), epoch_zero(), 0, 3, limits());

    CHECK(manager.membership() == membership());
    CHECK(manager.quorum_metadata().replica_count == 7);
    CHECK(manager.quorum_metadata().fault_threshold == 2);
    CHECK(manager.quorum_metadata().quorum == 5);
    CHECK(manager.current_epoch().schema_version() ==
          hotstuff::kEpochDefinitionSchemaVersionV2);
    CHECK(manager.current_epoch().epoch_number() == 0);
    CHECK(manager.current_epoch().generation_seed() == 0);
    CHECK(manager.current_epoch().policy_version() ==
          "adaptive-v2-bootstrap");
    CHECK(manager.current_epoch().evidence_snapshot_id() ==
          "adaptive-v2-bootstrap-epoch-zero");
    CHECK(manager.current_configuration().tree_id == 0);
    CHECK(manager.current_configuration().epoch_digest ==
          manager.current_epoch().epoch_digest());
    CHECK(manager.current_epoch().epoch_digest() ==
          hotstuff::compute_epoch_digest(epoch_zero()));
    CHECK(manager.activation_generation() == 3);
    CHECK(manager.audit_stats().lifecycle_corroboration_threshold == 3);
    CHECK(manager.audit_stats().pending_lifecycle_facts == 0);
    CHECK(manager.audit_stats().pending_lifecycle_associations == 0);
    CHECK(manager.ledger().healthy());
    CHECK(manager.healthy());

    auto six = membership(6);
    CHECK_THROWS_AS(
        AdaptiveV2ManagerIngress(
            six, epoch_zero(six), 0, 3, limits()),
        std::invalid_argument);

    auto duplicate = membership();
    duplicate.back() = duplicate.front();
    CHECK_THROWS_AS(
        AdaptiveV2ManagerIngress(
            duplicate, epoch_zero(), 0, 3, limits()),
        std::invalid_argument);

    auto legacy = epoch_zero();
    legacy.schema_version = hotstuff::kEpochDefinitionSchemaVersionV1;
    CHECK_THROWS_AS(
        AdaptiveV2ManagerIngress(
            membership(), legacy, 0, 3, limits()),
        std::invalid_argument);
    CHECK_THROWS_AS(
        AdaptiveV2ManagerIngress(
            membership(), epoch_zero(), 0, 0, limits()),
        std::invalid_argument);

    auto zero_pending_quota = limits();
    zero_pending_quota.maximum_pending_lifecycle_facts_per_source = 0;
    CHECK_THROWS_AS(
        AdaptiveV2ManagerIngress(
            membership(), epoch_zero(), 0, 3, zero_pending_quota),
        std::invalid_argument);

    auto excessive_pending_quota = limits();
    excessive_pending_quota.maximum_pending_lifecycle_facts_per_source =
        hotstuff::kMaximumAdaptiveV2PendingLifecycleFactsPerSource + 1;
    CHECK_THROWS_AS(
        AdaptiveV2ManagerIngress(
            membership(), epoch_zero(), 0, 3, excessive_pending_quota),
        std::invalid_argument);
}

TEST_CASE(
    "all seven exact readiness notices become ready",
    "[adaptive-v2][manager-ingress][readiness][n7]")
{
    const auto configured = limits();
    AdaptiveV2ManagerIngress manager(
        membership(), epoch_zero(), 0, 3, configured);

    for (const auto member : membership())
    {
        const auto value = readiness(manager, member);
        if (member % 2 == 0)
        {
            const MsgAdaptiveV2ReadinessNotice message(
                value, configured.readiness_wire);
            const auto result = manager.ingest_readiness(
                AuthenticatedReporter{member}, message);
            CHECK(result.status ==
                  AdaptiveV2ManagerIngressStatus::processed);
        }
        else
        {
            const auto payload =
                hotstuff::encode_adaptive_v2_readiness_notice(
                    value, configured.readiness_wire);
            const auto result = manager.ingest_readiness(
                AuthenticatedReporter{member}, payload);
            CHECK(result.status ==
                  AdaptiveV2ManagerIngressStatus::processed);
        }
    }

    const auto stats = manager.readiness_stats();
    CHECK(stats.total_members == 7);
    CHECK(stats.ready_members == 7);
    CHECK(stats.accepted_notices == 7);
    CHECK(stats.rejected_notices == 0);
    CHECK(stats.all_members_ready);
    CHECK(manager.all_members_ready());
}

TEST_CASE(
    "readiness rejects spoof replay configuration and generation drift",
    "[adaptive-v2][manager-ingress][readiness][negative]")
{
    const auto configured = limits();
    AdaptiveV2ManagerIngress manager(
        membership(), epoch_zero(), 0, 3, configured);

    auto spoofed = readiness(manager, 1);
    CHECK(manager.ingest_readiness(
              AuthenticatedReporter{0},
              hotstuff::encode_adaptive_v2_readiness_notice(
                  spoofed, configured.readiness_wire))
              .status ==
          AdaptiveV2ManagerIngressStatus::rejected_spoofed_source);

    const auto valid = readiness(manager, 0);
    const auto valid_payload =
        hotstuff::encode_adaptive_v2_readiness_notice(
            valid, configured.readiness_wire);
    CHECK(manager.ingest_readiness(
              AuthenticatedReporter{0}, valid_payload)
              .status == AdaptiveV2ManagerIngressStatus::processed);
    CHECK(manager.ingest_readiness(
              AuthenticatedReporter{0}, valid_payload)
              .status ==
          AdaptiveV2ManagerIngressStatus::rejected_sequence);

    auto wrong_configuration = readiness(manager, 2);
    ++wrong_configuration.active_configuration.tree_id;
    CHECK(manager.ingest_readiness(
              AuthenticatedReporter{2},
              hotstuff::encode_adaptive_v2_readiness_notice(
                  wrong_configuration, configured.readiness_wire))
              .status ==
          AdaptiveV2ManagerIngressStatus::rejected_configuration);

    auto wrong_generation = readiness(manager, 3);
    ++wrong_generation.activation_generation;
    CHECK(manager.ingest_readiness(
              AuthenticatedReporter{3},
              hotstuff::encode_adaptive_v2_readiness_notice(
                  wrong_generation, configured.readiness_wire))
              .status ==
          AdaptiveV2ManagerIngressStatus::rejected_generation);

    const auto stats = manager.readiness_stats();
    CHECK(stats.ready_members == 1);
    CHECK(stats.accepted_notices == 1);
    CHECK(stats.rejected_notices == 4);
    CHECK_FALSE(stats.all_members_ready);
}

TEST_CASE(
    "readiness rejects committed height regression before state mutation",
    "[adaptive-v2][manager-ingress][readiness][height][n7]")
{
    const auto configured = limits();
    AdaptiveV2ManagerIngress manager(
        membership(), epoch_zero(), 0, 3, configured);

    auto initial = readiness(manager, 2, 1);
    initial.committed_height = 120;
    CHECK(manager.ingest_readiness(
              AuthenticatedReporter{2},
              hotstuff::encode_adaptive_v2_readiness_notice(
                  initial, configured.readiness_wire))
              .status == AdaptiveV2ManagerIngressStatus::processed);

    auto regression = readiness(manager, 2, 2);
    regression.committed_height = 119;
    CHECK(manager.ingest_readiness(
              AuthenticatedReporter{2},
              hotstuff::encode_adaptive_v2_readiness_notice(
                  regression, configured.readiness_wire))
              .status == AdaptiveV2ManagerIngressStatus::
                             rejected_height_regression);

    auto corrected = regression;
    corrected.committed_height = 121;
    CHECK(manager.ingest_readiness(
              AuthenticatedReporter{2},
              hotstuff::encode_adaptive_v2_readiness_notice(
                  corrected, configured.readiness_wire))
              .status == AdaptiveV2ManagerIngressStatus::processed);

    const auto stats = manager.readiness_stats();
    CHECK(stats.ready_members == 1);
    CHECK(stats.accepted_notices == 2);
    CHECK(stats.rejected_notices == 1);
    CHECK(manager.audit_stats().state_rejections == 1);
}

TEST_CASE(
    "N7 admission requires f plus one distinct corroborating members",
    "[adaptive-v2][manager-ingress][evidence][lifecycle][corroboration][n7]")
{
    const auto configured = limits();
    AdaptiveV2ManagerIngress manager(
        membership(), epoch_zero(), 0, 3, configured);
    const auto expected = observation(manager);
    const auto evidence_payload = hotstuff::encode_evidence_batch(
        ResponseObservationBatch{
            hotstuff::kEvidenceBatchSchemaVersion, {expected}},
        configured.evidence_wire);

    const MsgEvidenceReport evidence_message(evidence_payload);
    const auto before = manager.ingest_evidence(
        AuthenticatedReporter{1}, evidence_message);
    CHECK(before.status == AdaptiveV2ManagerIngressStatus::processed);
    CHECK(before.decoded_observations == 1);
    CHECK(before.processed_observations == 1);
    CHECK(before.accepted_observations == 0);
    CHECK(before.rejected_observations == 0);
    CHECK(before.newly_quarantined_observations == 1);
    CHECK(before.remaining_quarantined_observations == 1);
    CHECK(manager.ledger().accepted().empty());

    const auto first = ingest_lifecycle(
        manager, configured, admission(expected.proposal_key(), 0, 1));
    CHECK(first.status ==
          AdaptiveV2ManagerIngressStatus::awaiting_corroboration);
    CHECK_FALSE(first.lifecycle.index_changed);
    CHECK(manager.lifecycle_stats().applied_lifecycle_notices == 0);
    CHECK(manager.lifecycle_stats().quarantined_records == 1);
    CHECK(manager.audit_stats().pending_lifecycle_facts == 1);
    CHECK(manager.audit_stats().pending_lifecycle_associations == 1);

    const auto same_source_duplicate = ingest_lifecycle(
        manager, configured, admission(expected.proposal_key(), 0, 2));
    CHECK(same_source_duplicate.status ==
          AdaptiveV2ManagerIngressStatus::awaiting_corroboration);
    CHECK(manager.audit_stats().pending_lifecycle_facts == 1);
    CHECK(manager.audit_stats().pending_lifecycle_associations == 1);

    CHECK(ingest_lifecycle(
              manager,
              configured,
              admission(expected.proposal_key(), 0, 2))
              .status == AdaptiveV2ManagerIngressStatus::rejected_sequence);

    const auto second = ingest_lifecycle(
        manager, configured, admission(expected.proposal_key(), 1, 1));
    CHECK(second.status ==
          AdaptiveV2ManagerIngressStatus::awaiting_corroboration);
    CHECK_FALSE(second.lifecycle.index_changed);
    CHECK(manager.lifecycle_stats().applied_lifecycle_notices == 0);
    CHECK(manager.lifecycle_stats().quarantined_records == 1);
    CHECK(manager.audit_stats().pending_lifecycle_facts == 1);
    CHECK(manager.audit_stats().pending_lifecycle_associations == 2);

    const auto admitted = ingest_lifecycle(
        manager, configured, admission(expected.proposal_key(), 2, 1));
    CHECK(admitted.status == AdaptiveV2ManagerIngressStatus::processed);
    CHECK(admitted.lifecycle.status ==
          ProposalLifecycleApplyStatus::applied);
    CHECK(admitted.lifecycle.retried_observations == 1);
    CHECK(admitted.lifecycle.accepted_observations == 1);
    CHECK(admitted.lifecycle.remaining_quarantined == 0);
    REQUIRE(manager.ledger().accepted().size() == 1);
    CHECK(manager.ledger().accepted().front().observation.observation_id ==
          expected.observation_id);
    CHECK(manager.lifecycle_stats().quarantined_records == 0);
    CHECK(manager.lifecycle_stats().applied_lifecycle_notices == 1);
    CHECK(manager.audit_stats().pending_lifecycle_facts == 0);
    CHECK(manager.audit_stats().pending_lifecycle_associations == 0);

    const auto late = ingest_lifecycle(
        manager, configured, admission(expected.proposal_key(), 3, 1));
    CHECK(late.status == AdaptiveV2ManagerIngressStatus::already_applied);
    CHECK_FALSE(late.lifecycle.index_changed);
    CHECK(manager.lifecycle_stats().applied_lifecycle_notices == 1);
    CHECK(manager.audit_stats().pending_lifecycle_facts == 0);
    CHECK(manager.audit_stats().pending_lifecycle_associations == 0);
    CHECK(manager.healthy());
}

TEST_CASE(
    "one authenticated member cannot retire the manager evidence window",
    "[adaptive-v2][manager-ingress][lifecycle][authority][n7]")
{
    const auto configured = limits();

    const auto check_rejected_global_fact =
        [&](ProposalLifecycleFact global_fact,
            const std::string &label) {
            AdaptiveV2ManagerIngress manager(
                membership(), epoch_zero(), 0, 3, configured);
            const auto expected = observation(manager, label);
            const auto global_notice = lifecycle(
                std::move(global_fact), 6, 1);

            const auto rejected = manager.ingest_lifecycle(
                AuthenticatedReporter{6},
                hotstuff::encode_proposal_lifecycle_notice(
                    global_notice, configured.lifecycle_wire));
            CHECK(rejected.status ==
                  AdaptiveV2ManagerIngressStatus::rejected_lifecycle);
            CHECK_FALSE(rejected.lifecycle.index_changed);
            CHECK(manager.lifecycle_stats().lifecycle_sources == 0);
            CHECK(manager.lifecycle_stats().applied_lifecycle_notices == 0);

            CHECK(ingest_lifecycle(
                      manager,
                      configured,
                      admission(expected.proposal_key(), 0, 1))
                      .status == AdaptiveV2ManagerIngressStatus::
                                     awaiting_corroboration);
            CHECK(ingest_lifecycle(
                      manager,
                      configured,
                      admission(expected.proposal_key(), 1, 1))
                      .status == AdaptiveV2ManagerIngressStatus::
                                     awaiting_corroboration);
            const auto admitted = ingest_lifecycle(
                manager,
                configured,
                admission(expected.proposal_key(), 2, 1));
            REQUIRE(admitted.status ==
                    AdaptiveV2ManagerIngressStatus::processed);
            CHECK(admitted.lifecycle.index_changed);

            const auto evidence = manager.ingest_evidence(
                AuthenticatedReporter{1},
                hotstuff::encode_evidence_batch(
                    ResponseObservationBatch{
                        hotstuff::kEvidenceBatchSchemaVersion,
                        {expected}},
                    configured.evidence_wire));
            CHECK(evidence.status ==
                  AdaptiveV2ManagerIngressStatus::processed);
            CHECK(evidence.accepted_observations == 1);
            CHECK(evidence.rejected_observations == 0);
            CHECK(evidence.remaining_quarantined_observations == 0);
            REQUIRE(manager.ledger().accepted().size() == 1);
            CHECK(manager.ledger().rejected().empty());
            CHECK(manager.lifecycle_stats().lifecycle_sources == 1);
            CHECK(manager.lifecycle_stats().applied_lifecycle_notices == 1);
            CHECK(manager.audit_stats().state_rejections == 1);
            CHECK(manager.audit_stats().pending_lifecycle_facts == 0);
            CHECK(manager.audit_stats().pending_lifecycle_associations == 0);
            CHECK(manager.healthy());
        };

    SECTION("replica abort cannot stale a proposal globally")
    {
        AdaptiveV2ManagerIngress identity(
            membership(), epoch_zero(), 0, 3, configured);
        check_rejected_global_fact(
            ProposalRuntimeAborted{
                observation(identity, "runtime-abort").proposal_key()},
            "runtime-abort");
    }

    SECTION("replica cannot retire the active configuration")
    {
        const auto current = epoch_zero();
        AdaptiveV2ManagerIngress identity(
            membership(), current, 0, 3, configured);
        check_rejected_global_fact(
            ProposalConfigurationRetired{
                identity.current_configuration()},
            "configuration-retirement");
    }

    SECTION("replica cannot advance the global retirement floor")
    {
        check_rejected_global_fact(
            ProposalRetirementFloorAdvanced{1},
            "retirement-floor");
    }
}

TEST_CASE(
    "N7 commit supersedes pending initialization and applies only once",
    "[adaptive-v2][manager-ingress][lifecycle][commit][corroboration][n7]")
{
    const auto configured = limits();
    AdaptiveV2ManagerIngress manager(
        membership(), epoch_zero(), 0, 3, configured);
    const auto committed_key =
        observation(manager, "committed-directly").proposal_key();

    for (ReplicaID source = 0; source < 2; ++source)
    {
        const auto pending_initialization = ingest_lifecycle(
            manager,
            configured,
            admission(committed_key, source, 1));
        CHECK(pending_initialization.status ==
              AdaptiveV2ManagerIngressStatus::awaiting_corroboration);
    }
    CHECK(manager.audit_stats().pending_lifecycle_facts == 1);
    CHECK(manager.audit_stats().pending_lifecycle_associations == 2);

    for (ReplicaID source = 2; source < 4; ++source)
    {
        const auto pending = ingest_lifecycle(
            manager,
            configured,
            lifecycle(ProposalCommitted{committed_key}, source, 1));
        CHECK(pending.status ==
              AdaptiveV2ManagerIngressStatus::awaiting_corroboration);
        CHECK_FALSE(pending.lifecycle.index_changed);
        CHECK(manager.lifecycle_stats().applied_lifecycle_notices == 0);
    }

    const auto committed = ingest_lifecycle(
        manager,
        configured,
        lifecycle(ProposalCommitted{committed_key}, 4, 1));
    CHECK(committed.status == AdaptiveV2ManagerIngressStatus::processed);
    CHECK(committed.lifecycle.index_changed);
    CHECK(manager.audit_stats().pending_lifecycle_facts == 0);
    CHECK(manager.audit_stats().pending_lifecycle_associations == 0);

    const auto late = ingest_lifecycle(
        manager,
        configured,
        lifecycle(ProposalCommitted{committed_key}, 5, 1));
    CHECK(late.status == AdaptiveV2ManagerIngressStatus::already_applied);
    CHECK_FALSE(late.lifecycle.index_changed);

    const auto late_initialization = ingest_lifecycle(
        manager,
        configured,
        admission(committed_key, 6, 1));
    CHECK(late_initialization.status ==
          AdaptiveV2ManagerIngressStatus::rejected_lifecycle);
    CHECK_FALSE(late_initialization.lifecycle.index_changed);
    CHECK(manager.lifecycle_stats().applied_lifecycle_notices == 1);
    CHECK(manager.audit_stats().pending_lifecycle_facts == 0);
    CHECK(manager.audit_stats().pending_lifecycle_associations == 0);

    const auto stale_evidence = manager.ingest_evidence(
        AuthenticatedReporter{1},
        hotstuff::encode_evidence_batch(
            ResponseObservationBatch{
                hotstuff::kEvidenceBatchSchemaVersion,
                {observation(manager, "committed-directly")}},
            configured.evidence_wire));
    CHECK(stale_evidence.accepted_observations == 0);
    CHECK(stale_evidence.rejected_observations == 1);

    const auto stats = manager.lifecycle_stats();
    CHECK(stats.lifecycle_sources == 1);
    CHECK(stats.applied_lifecycle_notices == 1);
    CHECK(manager.audit_stats().state_rejections == 1);
    CHECK(manager.audit_stats().pending_lifecycle_facts == 0);
    CHECK(manager.audit_stats().pending_lifecycle_associations == 0);
    CHECK(manager.healthy());
}

TEST_CASE(
    "pending lifecycle consumes sequence and correction needs the next value",
    "[adaptive-v2][manager-ingress][lifecycle][sequence][corroboration][n7]")
{
    const auto configured = limits();
    AdaptiveV2ManagerIngress manager(
        membership(), epoch_zero(), 0, 3, configured);
    const auto first = observation(manager, "sequence-first").proposal_key();
    const auto corrected =
        observation(manager, "sequence-corrected").proposal_key();

    CHECK(ingest_lifecycle(manager, configured, admission(first, 0, 1))
              .status == AdaptiveV2ManagerIngressStatus::
                             awaiting_corroboration);
    CHECK(ingest_lifecycle(manager, configured, admission(corrected, 0, 1))
              .status == AdaptiveV2ManagerIngressStatus::rejected_sequence);
    CHECK(ingest_lifecycle(manager, configured, admission(corrected, 0, 2))
              .status == AdaptiveV2ManagerIngressStatus::
                             awaiting_corroboration);
    CHECK(ingest_lifecycle(manager, configured, admission(corrected, 0, 3))
              .status == AdaptiveV2ManagerIngressStatus::
                             awaiting_corroboration);

    const auto audit = manager.audit_stats();
    CHECK(audit.pending_lifecycle_facts == 2);
    CHECK(audit.pending_lifecycle_associations == 2);
    CHECK(manager.lifecycle_stats().applied_lifecycle_notices == 0);
    CHECK(manager.healthy());
}

TEST_CASE(
    "f Byzantine unique spam cannot consume honest corroboration capacity",
    "[adaptive-v2][manager-ingress][lifecycle][capacity][byzantine][n7]")
{
    auto configured = limits();
    configured.maximum_pending_lifecycle_facts_per_source = 2;
    AdaptiveV2ManagerIngress manager(
        membership(), epoch_zero(), 0, 3, configured);

    for (ReplicaID source = 0; source < 2; ++source)
    {
        for (std::uint64_t sequence = 1; sequence <= 2; ++sequence)
        {
            const auto key = observation(
                manager,
                "spam-" + std::to_string(source) + "-" +
                    std::to_string(sequence)).proposal_key();
            CHECK(ingest_lifecycle(
                      manager,
                      configured,
                      admission(key, source, sequence))
                      .status == AdaptiveV2ManagerIngressStatus::
                                     awaiting_corroboration);
        }

        const auto excess = observation(
            manager,
            "spam-excess-" + std::to_string(source)).proposal_key();
        CHECK(ingest_lifecycle(
                  manager, configured, admission(excess, source, 3))
                  .status ==
              AdaptiveV2ManagerIngressStatus::rejected_capacity);
        CHECK(ingest_lifecycle(
                  manager, configured, admission(excess, source, 3))
                  .status ==
              AdaptiveV2ManagerIngressStatus::rejected_sequence);
    }

    auto audit = manager.audit_stats();
    CHECK(audit.pending_lifecycle_facts == 4);
    CHECK(audit.pending_lifecycle_associations == 4);
    CHECK(audit.lifecycle_quota_rejections == 2);
    CHECK(audit.capacity_failures == 0);

    const auto expected = observation(manager, "honest-after-spam");
    const auto quarantined = manager.ingest_evidence(
        AuthenticatedReporter{1},
        hotstuff::encode_evidence_batch(
            ResponseObservationBatch{
                hotstuff::kEvidenceBatchSchemaVersion, {expected}},
            configured.evidence_wire));
    CHECK(quarantined.remaining_quarantined_observations == 1);

    for (ReplicaID source = 2; source < 4; ++source)
    {
        CHECK(ingest_lifecycle(
                  manager,
                  configured,
                  admission(expected.proposal_key(), source, 1))
                  .status == AdaptiveV2ManagerIngressStatus::
                                 awaiting_corroboration);
    }
    const auto admitted = ingest_lifecycle(
        manager,
        configured,
        admission(expected.proposal_key(), 4, 1));
    CHECK(admitted.status == AdaptiveV2ManagerIngressStatus::processed);
    CHECK(admitted.lifecycle.index_changed);
    CHECK(admitted.lifecycle.accepted_observations == 1);
    REQUIRE(manager.ledger().accepted().size() == 1);

    audit = manager.audit_stats();
    CHECK(audit.pending_lifecycle_facts == 4);
    CHECK(audit.pending_lifecycle_associations == 4);
    CHECK(audit.lifecycle_quota_rejections == 2);
    CHECK(audit.capacity_failures == 0);
    CHECK(manager.lifecycle_stats().applied_lifecycle_notices == 1);
    CHECK(manager.healthy());
}

TEST_CASE(
    "invalid wire and nonmembers are audited and fail closed from authority",
    "[adaptive-v2][manager-ingress][wire][membership]")
{
    const auto configured = limits();
    AdaptiveV2ManagerIngress manager(
        membership(), epoch_zero(), 0, 3, configured);
    const bytearray_t malformed{0};

    CHECK(manager.ingest_readiness(
              AuthenticatedReporter{0}, malformed)
              .status == AdaptiveV2ManagerIngressStatus::rejected_wire);
    CHECK(manager.ingest_lifecycle(
              AuthenticatedReporter{0}, malformed)
              .status == AdaptiveV2ManagerIngressStatus::rejected_wire);
    const auto evidence = manager.ingest_evidence(
        AuthenticatedReporter{0}, malformed);
    CHECK(evidence.status ==
          AdaptiveV2ManagerIngressStatus::rejected_wire);
    REQUIRE(manager.ledger().rejected().size() == 1);
    CHECK(manager.ledger().rejected().front().wire_error.has_value());

    const auto outsider = readiness(manager, 0);
    const MsgAdaptiveV2ReadinessNotice message(
        outsider, configured.readiness_wire);
    CHECK(manager.ingest_readiness(
              AuthenticatedReporter{7}, message)
              .status ==
          AdaptiveV2ManagerIngressStatus::rejected_nonmember);

    const auto audit = manager.audit_stats();
    CHECK(audit.readiness_wire_rejections == 1);
    CHECK(audit.lifecycle_wire_rejections == 1);
    CHECK(audit.evidence_wire_rejections == 1);
    CHECK(audit.nonmember_rejections == 1);
    CHECK(manager.healthy());

    manager.shutdown();
    CHECK_FALSE(manager.healthy());
    CHECK(manager.ingest_readiness(
              AuthenticatedReporter{0}, message)
              .status == AdaptiveV2ManagerIngressStatus::stopped);
}
