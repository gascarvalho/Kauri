#include <algorithm>
#include <cstdint>
#include <limits>
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
    configured.lifecycle = {16, 4096, 7, 64, 16, 7, 2};
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
    const std::string &label = "quarantined",
    ReplicaID reporter = 1,
    ReplicaID observed = 3,
    std::uint64_t reporter_sequence = 1)
{
    ResponseObservation value;
    value.reporter_id = reporter;
    value.observed_replica_id = observed;
    value.configuration = manager.current_configuration();
    value.block_hash = digest(label + "-block");
    value.expected_message_type = ExpectedMessageType::direct_vote;
    value.outcome = ResponseOutcome::on_time;
    value.response_duration_us = 20;
    value.deadline_duration_us = 100;
    value.reporter_monotonic_ns = reporter_sequence * 1'000;
    value.reporter_sequence = reporter_sequence;
    value.signer_set = {observed};
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

EpochDefinitionInput strict_successor(
    const AdaptiveV2ManagerIngress &manager)
{
    EpochDefinitionInput input;
    input.schema_version = hotstuff::kEpochDefinitionSchemaVersionV2;
    input.epoch_number = manager.current_epoch().epoch_number() + 1;
    input.previous_epoch_digest = manager.current_epoch().epoch_digest();
    input.membership_digest = manager.current_epoch().membership_digest();
    input.trees = {EpochTreeDefinition{
        0, 2, 2, {2, 3, 4, 5, 6, 0, 1}, {0, 1}}};
    input.activation_height = 0;
    input.generation_seed = manager.activation_generation() + 1;
    input.policy_version = "adaptive-v2-ingress-rotation-v1";
    input.evidence_snapshot_id = "exact-retired-window";
    input.evidence_cutoff = manager.ledger().high_watermark();
    input.epoch_digest = hotstuff::compute_epoch_digest(input);
    return input;
}

template<typename Manager, typename = void>
struct has_strict_successor_rotation : std::false_type
{};

template<typename Manager>
struct has_strict_successor_rotation<
    Manager,
    std::void_t<decltype(std::declval<Manager &>().rotate_to_successor(
        std::declval<const EpochDefinitionInput &>(),
        std::uint32_t{}))>> : std::true_type
{};

template<typename Manager>
void verify_recurring_ingress_contract()
{
    if constexpr (!has_strict_successor_rotation<Manager>::value)
    {
        FAIL(
            "M12-R01 RED: AdaptiveV2ManagerIngress has no "
            "rotate_to_successor(definition, active_tree_id) entry point");
    }
    else
    {
        const auto configured = limits();
        Manager manager(
            membership(), epoch_zero(), 0, 3, configured);
        const auto fixed_membership = manager.membership();
        const auto fixed_membership_digest =
            manager.current_epoch().membership_digest();
        const auto old_configuration = manager.current_configuration();
        const auto old_generation = manager.activation_generation();

        const auto refresh_digest = [](EpochDefinitionInput &input) {
            input.epoch_digest.reset();
            input.epoch_digest = hotstuff::compute_epoch_digest(input);
        };
        auto skipped_epoch = strict_successor(manager);
        ++skipped_epoch.epoch_number;
        refresh_digest(skipped_epoch);
        CHECK(manager.rotate_to_successor(skipped_epoch, 0) ==
              AdaptiveV2ManagerIngressStatus::rejected_configuration);

        auto wrong_predecessor = strict_successor(manager);
        wrong_predecessor.previous_epoch_digest =
            digest("wrong-predecessor");
        refresh_digest(wrong_predecessor);
        CHECK(manager.rotate_to_successor(wrong_predecessor, 0) ==
              AdaptiveV2ManagerIngressStatus::rejected_configuration);

        auto changed_membership = strict_successor(manager);
        changed_membership.membership_digest =
            digest("changed-membership");
        refresh_digest(changed_membership);
        CHECK(manager.rotate_to_successor(changed_membership, 0) ==
              AdaptiveV2ManagerIngressStatus::rejected_configuration);

        auto incomplete_tree = strict_successor(manager);
        incomplete_tree.trees.front().members_breadth_first.pop_back();
        refresh_digest(incomplete_tree);
        CHECK(manager.rotate_to_successor(incomplete_tree, 0) ==
              AdaptiveV2ManagerIngressStatus::rejected_configuration);

        const auto missing_active_tree = strict_successor(manager);
        CHECK(manager.rotate_to_successor(missing_active_tree, 9) ==
              AdaptiveV2ManagerIngressStatus::rejected_configuration);
        CHECK(manager.current_epoch().epoch_number() == 0);
        CHECK(manager.current_epoch().epoch_digest() ==
              old_configuration.epoch_digest);
        CHECK(manager.activation_generation() == old_generation);

        REQUIRE(manager.ingest_readiness(
                    AuthenticatedReporter{0},
                    hotstuff::encode_adaptive_v2_readiness_notice(
                        readiness(manager, 0, 1, old_generation),
                        configured.readiness_wire))
                    .status ==
                AdaptiveV2ManagerIngressStatus::processed);

        const auto accepted_old = observation(
            manager, "before-rotation", 2, 5, 1);
        for (ReplicaID source = 0; source < 3; ++source)
        {
            const auto admitted = ingest_lifecycle(
                manager,
                configured,
                admission(accepted_old.proposal_key(), source, 1));
            CHECK(admitted.status ==
                  (source < 2
                       ? AdaptiveV2ManagerIngressStatus::
                             awaiting_corroboration
                       : AdaptiveV2ManagerIngressStatus::processed));
        }
        REQUIRE(manager.ingest_evidence(
                    AuthenticatedReporter{2},
                    hotstuff::encode_evidence_batch(
                        ResponseObservationBatch{
                            hotstuff::kEvidenceBatchSchemaVersion,
                            {accepted_old}},
                        configured.evidence_wire))
                    .accepted_observations == 1);
        REQUIRE(manager.ledger().high_watermark() == 1);

        auto stale_readiness = readiness(
            manager, 0, 2, old_generation);
        auto stale_evidence = observation(
            manager, "stale-after-rotation", 2, 5, 2);
        const auto stale_lifecycle = admission(
            stale_evidence.proposal_key(), 0, 2);

        const auto successor = strict_successor(manager);
        const auto expected_digest = *successor.epoch_digest;
        REQUIRE(manager.rotate_to_successor(successor, 0) ==
                AdaptiveV2ManagerIngressStatus::processed);
        CHECK(manager.current_epoch().epoch_number() == 1);
        CHECK(manager.current_epoch().previous_epoch_digest() ==
              old_configuration.epoch_digest);
        CHECK(manager.current_epoch().epoch_digest() == expected_digest);
        CHECK(manager.current_configuration().epoch_number == 1);
        CHECK(manager.current_configuration().epoch_digest ==
              expected_digest);
        CHECK(manager.activation_generation() == old_generation + 1);
        CHECK(manager.membership() == fixed_membership);
        CHECK(manager.current_epoch().membership_digest() ==
              fixed_membership_digest);
        CHECK(manager.quorum_metadata().replica_count == 7);
        CHECK(manager.quorum_metadata().fault_threshold == 2);
        CHECK(manager.quorum_metadata().quorum == 5);
        CHECK(manager.ledger().accepted().empty());
        CHECK(manager.ledger().high_watermark() == 0);
        CHECK_FALSE(manager.all_members_ready());
        CHECK(manager.lifecycle_stats().quarantined_records == 0);

        const auto empty_ledger_size = manager.ledger().accepted().size();
        CHECK(manager.ingest_readiness(
                  AuthenticatedReporter{0},
                  hotstuff::encode_adaptive_v2_readiness_notice(
                      stale_readiness, configured.readiness_wire))
                  .status ==
              AdaptiveV2ManagerIngressStatus::rejected_configuration);
        CHECK(manager.ingest_readiness(
                  AuthenticatedReporter{0},
                  hotstuff::encode_adaptive_v2_readiness_notice(
                      readiness(
                          manager,
                          0,
                          2,
                          manager.activation_generation()),
                      configured.readiness_wire))
                  .status ==
              AdaptiveV2ManagerIngressStatus::rejected_sequence);
        CHECK(manager.ingest_readiness(
                  AuthenticatedReporter{0},
                  hotstuff::encode_adaptive_v2_readiness_notice(
                      readiness(
                          manager,
                          0,
                          3,
                          manager.activation_generation()),
                      configured.readiness_wire))
                  .status ==
              AdaptiveV2ManagerIngressStatus::processed);

        CHECK(ingest_lifecycle(
                  manager, configured, stale_lifecycle)
                  .status ==
              AdaptiveV2ManagerIngressStatus::rejected_configuration);
        auto current_evidence = observation(
            manager, "fresh-after-rotation", 2, 3, 2);
        current_evidence.expected_message_type =
            ExpectedMessageType::aggregate_relay;
        current_evidence.observation_id =
            hotstuff::compute_response_observation_id(
                current_evidence.attempt_identity());
        CHECK(ingest_lifecycle(
                  manager,
                  configured,
                  admission(current_evidence.proposal_key(), 0, 2))
                  .status ==
              AdaptiveV2ManagerIngressStatus::rejected_sequence);
        CHECK(ingest_lifecycle(
                  manager,
                  configured,
                  admission(current_evidence.proposal_key(), 0, 3))
                  .status ==
              AdaptiveV2ManagerIngressStatus::awaiting_corroboration);
        CHECK(ingest_lifecycle(
                  manager,
                  configured,
                  admission(current_evidence.proposal_key(), 1, 2))
                  .status ==
              AdaptiveV2ManagerIngressStatus::awaiting_corroboration);
        CHECK(ingest_lifecycle(
                  manager,
                  configured,
                  admission(current_evidence.proposal_key(), 2, 2))
                  .status ==
              AdaptiveV2ManagerIngressStatus::processed);

        CHECK(manager.ingest_evidence(
                  AuthenticatedReporter{2},
                  hotstuff::encode_evidence_batch(
                      ResponseObservationBatch{
                          hotstuff::kEvidenceBatchSchemaVersion,
                          {stale_evidence}},
                      configured.evidence_wire))
                  .status ==
              AdaptiveV2ManagerIngressStatus::rejected_configuration);
        CHECK(manager.ledger().accepted().size() == empty_ledger_size);
        const auto replayed_current = manager.ingest_evidence(
            AuthenticatedReporter{2},
            hotstuff::encode_evidence_batch(
                ResponseObservationBatch{
                    hotstuff::kEvidenceBatchSchemaVersion,
                    {current_evidence}},
                configured.evidence_wire));
        CHECK(replayed_current.status ==
              AdaptiveV2ManagerIngressStatus::processed);
        CHECK(replayed_current.accepted_observations == 0);
        CHECK(replayed_current.rejected_observations == 1);
        CHECK(manager.ledger().high_watermark() == 0);
        auto accepted_current = current_evidence;
        accepted_current.reporter_sequence = 3;
        accepted_current.reporter_monotonic_ns = 3'000;
        accepted_current.observation_id =
            hotstuff::compute_response_observation_id(
                accepted_current.attempt_identity());
        const auto fresh = manager.ingest_evidence(
            AuthenticatedReporter{2},
            hotstuff::encode_evidence_batch(
                ResponseObservationBatch{
                    hotstuff::kEvidenceBatchSchemaVersion,
                    {accepted_current}},
                configured.evidence_wire));
        CHECK(fresh.status ==
              AdaptiveV2ManagerIngressStatus::processed);
        CHECK(fresh.accepted_observations == 1);

        Manager exhausted(
            membership(),
            epoch_zero(),
            0,
            std::numeric_limits<std::uint64_t>::max(),
            configured);
        const auto exhausted_digest =
            exhausted.current_epoch().epoch_digest();
        const auto exhausted_generation =
            exhausted.activation_generation();
        const auto rejected_successor = strict_successor(exhausted);
        CHECK(exhausted.rotate_to_successor(rejected_successor, 0) ==
              AdaptiveV2ManagerIngressStatus::rejected_generation);
        CHECK(exhausted.current_epoch().epoch_number() == 0);
        CHECK(exhausted.current_epoch().epoch_digest() ==
              exhausted_digest);
        CHECK(exhausted.activation_generation() ==
              exhausted_generation);
        CHECK(exhausted.ledger().accepted().empty());
    }
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

    auto zero_reporter_quota = limits();
    zero_reporter_quota.lifecycle.maximum_quarantined_records_per_reporter =
        0;
    CHECK_THROWS_AS(
        AdaptiveV2ManagerIngress(
            membership(), epoch_zero(), 0, 3, zero_reporter_quota),
        std::invalid_argument);

    auto excessive_reporter_share = limits();
    excessive_reporter_share.lifecycle.
        maximum_quarantined_records_per_reporter = 3;
    CHECK_THROWS_AS(
        AdaptiveV2ManagerIngress(
            membership(), epoch_zero(), 0, 3, excessive_reporter_share),
        std::invalid_argument);

    auto undersized_global_quarantine = limits();
    undersized_global_quarantine.lifecycle.maximum_quarantined_records = 6;
    undersized_global_quarantine.lifecycle.
        maximum_quarantined_records_per_reporter = 1;
    undersized_global_quarantine.lifecycle_accounting.maximum_records = 6;
    CHECK_THROWS_AS(
        AdaptiveV2ManagerIngress(
            membership(),
            epoch_zero(),
            0,
            3,
            undersized_global_quarantine),
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
    CHECK(manager.audit_stats().reporter_causal_retained_proposals == 1);
    CHECK(manager.audit_stats().reporter_causal_open_reporters == 4);

    const auto late = ingest_lifecycle(
        manager,
        configured,
        lifecycle(ProposalCommitted{committed_key}, 5, 1));
    CHECK(late.status == AdaptiveV2ManagerIngressStatus::already_applied);
    CHECK_FALSE(late.lifecycle.index_changed);
    CHECK(manager.audit_stats().reporter_causal_open_reporters == 3);

    const auto late_initialization = ingest_lifecycle(
        manager,
        configured,
        admission(committed_key, 6, 1));
    CHECK(late_initialization.status ==
          AdaptiveV2ManagerIngressStatus::rejected_lifecycle);
    CHECK_FALSE(late_initialization.lifecycle.index_changed);
    CHECK(manager.audit_stats().reporter_causal_open_reporters == 3);
    CHECK(manager.lifecycle_stats().applied_lifecycle_notices == 1);
    CHECK(manager.audit_stats().pending_lifecycle_facts == 0);
    CHECK(manager.audit_stats().pending_lifecycle_associations == 0);

    const auto precommit_evidence = manager.ingest_evidence(
        AuthenticatedReporter{1},
        hotstuff::encode_evidence_batch(
            ResponseObservationBatch{
                hotstuff::kEvidenceBatchSchemaVersion,
                {observation(manager, "committed-directly")}},
            configured.evidence_wire));
    CHECK(precommit_evidence.accepted_observations == 0);
    CHECK(precommit_evidence.rejected_observations == 0);
    CHECK(precommit_evidence.newly_quarantined_observations == 1);

    const auto reporter_commit = ingest_lifecycle(
        manager,
        configured,
        lifecycle(ProposalCommitted{committed_key, 1}, 1, 2));
    CHECK(reporter_commit.status ==
          AdaptiveV2ManagerIngressStatus::already_applied);
    CHECK(reporter_commit.lifecycle.retried_observations == 1);
    CHECK(reporter_commit.lifecycle.accepted_observations == 1);
    CHECK(manager.audit_stats().reporter_causal_open_reporters == 2);

    const auto postcommit_evidence = manager.ingest_evidence(
        AuthenticatedReporter{1},
        hotstuff::encode_evidence_batch(
            ResponseObservationBatch{
                hotstuff::kEvidenceBatchSchemaVersion,
                {observation(
                    manager, "committed-directly", 1, 4, 2)}},
            configured.evidence_wire));
    CHECK(postcommit_evidence.accepted_observations == 0);
    CHECK(postcommit_evidence.rejected_observations == 1);

    CHECK(ingest_lifecycle(
              manager,
              configured,
              lifecycle(ProposalCommitted{committed_key}, 0, 2))
              .status == AdaptiveV2ManagerIngressStatus::already_applied);
    CHECK(ingest_lifecycle(
              manager,
              configured,
              lifecycle(ProposalCommitted{committed_key}, 6, 2))
              .status == AdaptiveV2ManagerIngressStatus::already_applied);
    CHECK(manager.audit_stats().reporter_causal_retained_proposals == 1);
    CHECK(manager.audit_stats().reporter_causal_open_reporters == 0);

    const auto stats = manager.lifecycle_stats();
    CHECK(stats.lifecycle_sources == 1);
    CHECK(stats.applied_lifecycle_notices == 1);
    CHECK(manager.ledger().accepted().size() == 1);
    CHECK(manager.ledger().rejected().size() == 1);
    CHECK(manager.audit_stats().state_rejections == 1);
    CHECK(manager.audit_stats().pending_lifecycle_facts == 0);
    CHECK(manager.audit_stats().pending_lifecycle_associations == 0);
    CHECK(manager.healthy());

    manager.shutdown();
    CHECK(manager.audit_stats().reporter_causal_retained_proposals == 0);
}

TEST_CASE(
    "N7 direct commit drains the authenticated precommit evidence prefix",
    "[adaptive-v2][manager-ingress][evidence][commit][causal][n7]")
{
    const auto configured = limits();
    AdaptiveV2ManagerIngress manager(
        membership(), epoch_zero(), 0, 3, configured);
    const auto expected = observation(
        manager, "direct-commit-drain", 2, 5, 1);

    const auto quarantined = manager.ingest_evidence(
        AuthenticatedReporter{2},
        hotstuff::encode_evidence_batch(
            ResponseObservationBatch{
                hotstuff::kEvidenceBatchSchemaVersion, {expected}},
            configured.evidence_wire));
    CHECK(quarantined.newly_quarantined_observations == 1);
    CHECK(quarantined.remaining_quarantined_observations == 1);

    for (ReplicaID source = 2; source < 4; ++source)
    {
        CHECK(ingest_lifecycle(
                  manager,
                  configured,
                  lifecycle(
                      ProposalCommitted{
                          expected.proposal_key(), source == 2 ? 1ULL : 0ULL},
                      source,
                      1))
                  .status == AdaptiveV2ManagerIngressStatus::
                                 awaiting_corroboration);
    }
    const auto committed = ingest_lifecycle(
        manager,
        configured,
        lifecycle(ProposalCommitted{expected.proposal_key(), 0}, 4, 1));
    REQUIRE(committed.status ==
            AdaptiveV2ManagerIngressStatus::processed);
    CHECK(committed.lifecycle.retried_observations == 1);
    CHECK(committed.lifecycle.accepted_observations == 1);
    CHECK(committed.lifecycle.rejected_observations == 0);
    CHECK(committed.lifecycle.remaining_quarantined == 0);

    const auto after_boundary = manager.ingest_evidence(
        AuthenticatedReporter{2},
        hotstuff::encode_evidence_batch(
            ResponseObservationBatch{
                hotstuff::kEvidenceBatchSchemaVersion,
                {observation(
                    manager, "direct-commit-drain", 2, 6, 2)}},
            configured.evidence_wire));
    CHECK(after_boundary.accepted_observations == 0);
    CHECK(after_boundary.rejected_observations == 1);
    CHECK(manager.audit_stats().reporter_causal_retained_proposals == 1);
    CHECK(manager.audit_stats().reporter_causal_open_reporters == 4);
    CHECK(manager.ledger().accepted().size() == 1);
    CHECK(manager.ledger().rejected().size() == 1);
    CHECK(manager.healthy());
}

TEST_CASE(
    "N7 retained commit fence admits a blocked precommit FIFO suffix",
    "[adaptive-v2][manager-ingress][evidence][commit][causal][fifo][n7]")
{
    const auto configured = limits();
    AdaptiveV2ManagerIngress manager(
        membership(), epoch_zero(), 0, 3, configured);
    const auto older = observation(
        manager, "blocked-older", 1, 3, 1);
    const auto committed_suffix = observation(
        manager, "blocked-committed", 1, 4, 2);

    const auto quarantined = manager.ingest_evidence(
        AuthenticatedReporter{1},
        hotstuff::encode_evidence_batch(
            ResponseObservationBatch{
                hotstuff::kEvidenceBatchSchemaVersion,
                {older, committed_suffix}},
            configured.evidence_wire));
    CHECK(quarantined.newly_quarantined_observations == 2);
    CHECK(quarantined.remaining_quarantined_observations == 2);

    for (ReplicaID source = 1; source < 3; ++source)
    {
        CHECK(ingest_lifecycle(
                  manager,
                  configured,
                  lifecycle(
                      ProposalCommitted{
                          committed_suffix.proposal_key(),
                          source == 1 ? 2ULL : 0ULL},
                      source,
                      1))
                  .status == AdaptiveV2ManagerIngressStatus::
                                 awaiting_corroboration);
    }
    const auto committed = ingest_lifecycle(
        manager,
        configured,
        lifecycle(
            ProposalCommitted{committed_suffix.proposal_key(), 0},
            3,
            1));
    REQUIRE(committed.status ==
            AdaptiveV2ManagerIngressStatus::processed);
    CHECK(committed.lifecycle.retried_observations == 0);
    CHECK(committed.lifecycle.remaining_quarantined == 2);
    CHECK(manager.audit_stats().reporter_causal_retained_proposals == 1);
    CHECK(manager.audit_stats().reporter_causal_open_reporters == 4);

    for (ReplicaID source = 4; source < 6; ++source)
    {
        CHECK(ingest_lifecycle(
                  manager,
                  configured,
                  admission(older.proposal_key(), source, 1))
                  .status == AdaptiveV2ManagerIngressStatus::
                                 awaiting_corroboration);
    }
    const auto admitted = ingest_lifecycle(
        manager,
        configured,
        admission(older.proposal_key(), 6, 1));
    REQUIRE(admitted.status ==
            AdaptiveV2ManagerIngressStatus::processed);
    CHECK(admitted.lifecycle.retried_observations == 2);
    CHECK(admitted.lifecycle.accepted_observations == 2);
    CHECK(admitted.lifecycle.rejected_observations == 0);
    CHECK(admitted.lifecycle.remaining_quarantined == 0);

    const auto after_fence = manager.ingest_evidence(
        AuthenticatedReporter{1},
        hotstuff::encode_evidence_batch(
            ResponseObservationBatch{
                hotstuff::kEvidenceBatchSchemaVersion,
                {observation(
                    manager, "blocked-committed", 1, 3, 3)}},
            configured.evidence_wire));
    CHECK(after_fence.accepted_observations == 0);
    CHECK(after_fence.rejected_observations == 1);
    CHECK(manager.ledger().accepted().size() == 2);
    CHECK(manager.ledger().rejected().size() == 1);
    CHECK(manager.lifecycle_stats().quarantined_records == 0);
    CHECK(manager.audit_stats().reporter_causal_retained_proposals == 1);
    CHECK(manager.audit_stats().reporter_causal_open_reporters == 4);
    CHECK(manager.healthy());
}

TEST_CASE(
    "N7 terminal commit fence rejects late compensation beyond its prefix",
    "[adaptive-v2][manager-ingress][evidence][commit][causal][late][n7]")
{
    const auto configured = limits();
    AdaptiveV2ManagerIngress manager(
        membership(), epoch_zero(), 0, 3, configured);

    auto timeout = observation(
        manager, "terminal-timeout-late", 1, 3, 1);
    timeout.outcome = ResponseOutcome::timeout;
    timeout.response_duration_us = 0;
    timeout.signer_set.clear();
    timeout.observation_id = hotstuff::compute_response_observation_id(
        timeout.attempt_identity());

    auto late = timeout;
    late.outcome = ResponseOutcome::late;
    late.response_duration_us = 150;
    late.reporter_monotonic_ns = 2'000;
    late.reporter_sequence = 2;
    late.signer_set = {3};
    late.observation_id = hotstuff::compute_response_observation_id(
        late.attempt_identity());
    REQUIRE(timeout.observation_id == late.observation_id);

    for (ReplicaID source = 4; source < 6; ++source)
    {
        CHECK(ingest_lifecycle(
                  manager,
                  configured,
                  admission(timeout.proposal_key(), source, 1))
                  .status == AdaptiveV2ManagerIngressStatus::
                                 awaiting_corroboration);
    }
    CHECK(ingest_lifecycle(
              manager,
              configured,
              admission(timeout.proposal_key(), 6, 1))
              .status == AdaptiveV2ManagerIngressStatus::processed);

    const auto accepted_timeout = manager.ingest_evidence(
        AuthenticatedReporter{1},
        hotstuff::encode_evidence_batch(
            ResponseObservationBatch{
                hotstuff::kEvidenceBatchSchemaVersion, {timeout}},
            configured.evidence_wire));
    CHECK(accepted_timeout.accepted_observations == 1);
    CHECK(accepted_timeout.rejected_observations == 0);

    for (ReplicaID source = 1; source < 3; ++source)
    {
        CHECK(ingest_lifecycle(
                  manager,
                  configured,
                  lifecycle(
                      ProposalCommitted{
                          timeout.proposal_key(),
                          source == 1 ? 1ULL : 0ULL},
                      source,
                      1))
                  .status == AdaptiveV2ManagerIngressStatus::
                                 awaiting_corroboration);
    }
    const auto committed = ingest_lifecycle(
        manager,
        configured,
        lifecycle(
            ProposalCommitted{timeout.proposal_key(), 0}, 3, 1));
    REQUIRE(committed.status ==
            AdaptiveV2ManagerIngressStatus::processed);
    CHECK(manager.audit_stats().reporter_causal_retained_proposals == 1);
    CHECK(manager.audit_stats().reporter_causal_open_reporters == 4);

    const auto rejected_late = manager.ingest_evidence(
        AuthenticatedReporter{1},
        hotstuff::encode_evidence_batch(
            ResponseObservationBatch{
                hotstuff::kEvidenceBatchSchemaVersion, {late}},
            configured.evidence_wire));
    CHECK(rejected_late.accepted_observations == 0);
    CHECK(rejected_late.rejected_observations == 1);
    CHECK(rejected_late.newly_quarantined_observations == 0);
    CHECK(rejected_late.remaining_quarantined_observations == 0);
    CHECK(manager.audit_stats().reporter_causal_retained_proposals == 1);
    CHECK(manager.audit_stats().reporter_causal_open_reporters == 4);
    CHECK(manager.lifecycle_stats().quarantined_records == 0);
    CHECK(manager.ledger().accepted().size() == 1);
    CHECK(manager.ledger().rejected().size() == 1);
    CHECK(manager.healthy());
}

TEST_CASE(
    "N7 first reporter commit freezes an immutable evidence fence",
    "[adaptive-v2][manager-ingress][evidence][commit][causal][ratchet][n7]")
{
    const auto configured = limits();
    AdaptiveV2ManagerIngress manager(
        membership(), epoch_zero(), 0, 3, configured);
    const auto after_first_commit = observation(
        manager, "immutable-commit-fence", 1, 3, 1);
    const auto committed_key = after_first_commit.proposal_key();

    CHECK(ingest_lifecycle(
              manager,
              configured,
              lifecycle(ProposalCommitted{committed_key}, 1, 1))
              .status == AdaptiveV2ManagerIngressStatus::
                             awaiting_corroboration);

    const auto quarantined = manager.ingest_evidence(
        AuthenticatedReporter{1},
        hotstuff::encode_evidence_batch(
            ResponseObservationBatch{
                hotstuff::kEvidenceBatchSchemaVersion,
                {after_first_commit}},
            configured.evidence_wire));
    CHECK(quarantined.newly_quarantined_observations == 1);
    CHECK(quarantined.remaining_quarantined_observations == 1);

    CHECK(ingest_lifecycle(
              manager,
              configured,
              lifecycle(ProposalCommitted{committed_key}, 1, 2))
              .status == AdaptiveV2ManagerIngressStatus::
                             awaiting_corroboration);
    CHECK(ingest_lifecycle(
              manager,
              configured,
              lifecycle(ProposalCommitted{committed_key, 1}, 1, 3))
              .status == AdaptiveV2ManagerIngressStatus::
                             rejected_lifecycle);
    CHECK(manager.audit_stats().pending_lifecycle_associations == 1);
    CHECK(ingest_lifecycle(
              manager,
              configured,
              lifecycle(ProposalCommitted{committed_key}, 2, 1))
              .status == AdaptiveV2ManagerIngressStatus::
                             awaiting_corroboration);

    const auto globally_committed = ingest_lifecycle(
        manager,
        configured,
        lifecycle(ProposalCommitted{committed_key}, 3, 1));
    REQUIRE(globally_committed.status ==
            AdaptiveV2ManagerIngressStatus::processed);
    CHECK(globally_committed.lifecycle.retried_observations == 1);
    CHECK(globally_committed.lifecycle.accepted_observations == 0);
    CHECK(globally_committed.lifecycle.rejected_observations == 1);
    CHECK(globally_committed.lifecycle.remaining_quarantined == 0);

    const auto zero_sequence_after_commit = manager.ingest_evidence(
        AuthenticatedReporter{1},
        hotstuff::encode_evidence_batch(
            ResponseObservationBatch{
                hotstuff::kEvidenceBatchSchemaVersion,
                {observation(
                    manager, "immutable-commit-fence", 1, 4, 0)}},
            configured.evidence_wire));
    CHECK(zero_sequence_after_commit.accepted_observations == 0);
    CHECK(zero_sequence_after_commit.rejected_observations == 1);

    CHECK(ingest_lifecycle(
              manager,
              configured,
              lifecycle(ProposalCommitted{committed_key}, 1, 4))
              .status == AdaptiveV2ManagerIngressStatus::already_applied);
    CHECK(ingest_lifecycle(
              manager,
              configured,
              lifecycle(ProposalCommitted{committed_key, 1}, 1, 5))
              .status == AdaptiveV2ManagerIngressStatus::
                             rejected_lifecycle);
    const auto replay_after_duplicate_commit = manager.ingest_evidence(
        AuthenticatedReporter{1},
        hotstuff::encode_evidence_batch(
            ResponseObservationBatch{
                hotstuff::kEvidenceBatchSchemaVersion,
                {after_first_commit}},
            configured.evidence_wire));
    CHECK(replay_after_duplicate_commit.accepted_observations == 0);
    CHECK(replay_after_duplicate_commit.rejected_observations == 1);
    CHECK(manager.ledger().accepted().empty());
    CHECK(manager.ledger().rejected().size() == 1);
    CHECK(manager.audit_stats().evidence_sequence_rejections == 2);
    CHECK(manager.audit_stats().lifecycle_fence_mismatch_rejections == 2);
    CHECK(manager.audit_stats().reporter_causal_retained_proposals == 1);
    CHECK(manager.audit_stats().reporter_causal_open_reporters == 4);
    CHECK(manager.healthy());
}

TEST_CASE(
    "N7 counts only finite commit fences equal to authenticated FIFO highwater",
    "[adaptive-v2][manager-ingress][commit][fence][byzantine][n7]")
{
    const auto configured = limits();
    AdaptiveV2ManagerIngress manager(
        membership(), epoch_zero(), 0, 3, configured);
    const auto expected = observation(
        manager, "validated-fence", 1, 3, 2);
    const auto committed_key = expected.proposal_key();

    CHECK(ingest_lifecycle(
              manager,
              configured,
              lifecycle(ProposalCommitted{committed_key, 0}, 0, 1))
              .status == AdaptiveV2ManagerIngressStatus::
                             awaiting_corroboration);

    CHECK(ingest_lifecycle(
              manager,
              configured,
              lifecycle(ProposalCommitted{committed_key, 1}, 1, 1))
              .status == AdaptiveV2ManagerIngressStatus::
                             rejected_lifecycle);
    const auto evidence = manager.ingest_evidence(
        AuthenticatedReporter{1},
        hotstuff::encode_evidence_batch(
            ResponseObservationBatch{
                hotstuff::kEvidenceBatchSchemaVersion, {expected}},
            configured.evidence_wire));
    CHECK(evidence.newly_quarantined_observations == 1);
    CHECK(evidence.remaining_quarantined_observations == 1);
    CHECK(ingest_lifecycle(
              manager,
              configured,
              lifecycle(ProposalCommitted{committed_key, 1}, 1, 2))
              .status == AdaptiveV2ManagerIngressStatus::
                             rejected_lifecycle);
    CHECK(ingest_lifecycle(
              manager,
              configured,
              lifecycle(ProposalCommitted{committed_key, 2}, 1, 3))
              .status == AdaptiveV2ManagerIngressStatus::
                             awaiting_corroboration);

    const auto maximum = observation(
        manager,
        "validated-fence",
        2,
        5,
        std::numeric_limits<std::uint64_t>::max());
    const auto maximum_evidence = manager.ingest_evidence(
        AuthenticatedReporter{2},
        hotstuff::encode_evidence_batch(
            ResponseObservationBatch{
                hotstuff::kEvidenceBatchSchemaVersion, {maximum}},
            configured.evidence_wire));
    CHECK(maximum_evidence.newly_quarantined_observations == 1);
    CHECK(maximum_evidence.remaining_quarantined_observations == 2);
    CHECK(ingest_lifecycle(
              manager,
              configured,
              lifecycle(
                  ProposalCommitted{
                      committed_key,
                      std::numeric_limits<std::uint64_t>::max()},
                  2,
                  1))
              .status == AdaptiveV2ManagerIngressStatus::
                             rejected_lifecycle);

    const auto lower_after_maximum = manager.ingest_evidence(
        AuthenticatedReporter{2},
        hotstuff::encode_evidence_batch(
            ResponseObservationBatch{
                hotstuff::kEvidenceBatchSchemaVersion,
                {observation(manager, "validated-fence", 2, 5, 1)}},
            configured.evidence_wire));
    CHECK(lower_after_maximum.rejected_observations == 1);
    CHECK(lower_after_maximum.newly_quarantined_observations == 0);
    CHECK(lower_after_maximum.remaining_quarantined_observations == 2);

    CHECK(manager.audit_stats().pending_lifecycle_associations == 2);
    const auto committed = ingest_lifecycle(
        manager,
        configured,
        lifecycle(ProposalCommitted{committed_key, 0}, 3, 1));
    REQUIRE(committed.status == AdaptiveV2ManagerIngressStatus::processed);
    CHECK(committed.lifecycle.retried_observations == 1);
    CHECK(committed.lifecycle.accepted_observations == 1);
    CHECK(committed.lifecycle.rejected_observations == 0);
    CHECK(committed.lifecycle.remaining_quarantined == 1);
    CHECK(manager.audit_stats().pending_lifecycle_associations == 0);
    CHECK(manager.audit_stats().lifecycle_fence_mismatch_rejections == 3);
    CHECK(manager.audit_stats().evidence_sequence_rejections == 1);
    CHECK(manager.ledger().accepted().size() == 1);
    CHECK(manager.ledger().rejected().empty());
    CHECK(manager.lifecycle_stats().quarantined_records == 1);
    CHECK(manager.healthy());
}

TEST_CASE(
    "N7 consumes an authenticated evidence sequence before reporter validation",
    "[adaptive-v2][manager-ingress][evidence][sequence][byzantine][n7]")
{
    const auto configured = limits();
    AdaptiveV2ManagerIngress manager(
        membership(), epoch_zero(), 0, 3, configured);
    const auto spoofed = observation(
        manager, "spoofed-sequence", 2, 5, 5);

    const auto rejected_spoof = manager.ingest_evidence(
        AuthenticatedReporter{1},
        hotstuff::encode_evidence_batch(
            ResponseObservationBatch{
                hotstuff::kEvidenceBatchSchemaVersion, {spoofed}},
            configured.evidence_wire));
    CHECK(rejected_spoof.status ==
          AdaptiveV2ManagerIngressStatus::rejected_spoofed_source);
    CHECK(rejected_spoof.processed_observations == 1);
    CHECK(rejected_spoof.rejected_observations == 1);
    REQUIRE(manager.ledger().rejected().size() == 1);
    CHECK(manager.ledger().rejected().front().reason ==
          hotstuff::EvidenceRejectionReason::reporter_mismatch);

    const auto lower_valid = manager.ingest_evidence(
        AuthenticatedReporter{1},
        hotstuff::encode_evidence_batch(
            ResponseObservationBatch{
                hotstuff::kEvidenceBatchSchemaVersion,
                {observation(manager, "lower-valid", 1, 3, 4)}},
            configured.evidence_wire));
    CHECK(lower_valid.status == AdaptiveV2ManagerIngressStatus::processed);
    CHECK(lower_valid.processed_observations == 1);
    CHECK(lower_valid.rejected_observations == 1);
    CHECK(manager.ledger().rejected().size() == 1);

    CHECK(ingest_lifecycle(
              manager,
              configured,
              lifecycle(
                  ProposalCommitted{spoofed.proposal_key(), 4},
                  1,
                  1))
              .status == AdaptiveV2ManagerIngressStatus::rejected_lifecycle);
    CHECK(ingest_lifecycle(
              manager,
              configured,
              lifecycle(
                  ProposalCommitted{spoofed.proposal_key(), 5},
                  1,
                  2))
              .status == AdaptiveV2ManagerIngressStatus::
                             awaiting_corroboration);
    CHECK(manager.audit_stats().evidence_sequence_rejections == 1);
    CHECK(manager.audit_stats().lifecycle_fence_mismatch_rejections == 1);
    CHECK(manager.healthy());
}

TEST_CASE(
    "N7 open reporter prefixes do not consume stale rejection capacity",
    "[adaptive-v2][manager-ingress][evidence][commit][causal][capacity][n7]")
{
    auto configured = limits();
    configured.evidence_store.maximum_rejected_records = 1;
    AdaptiveV2ManagerIngress manager(
        membership(), epoch_zero(), 0, 3, configured);

    for (std::uint64_t index = 1; index <= 2; ++index)
    {
        const auto expected = observation(
            manager,
            "rejection-capacity-" + std::to_string(index),
            1,
            3,
            index);
        for (ReplicaID source = 2; source < 4; ++source)
        {
            CHECK(ingest_lifecycle(
                      manager,
                      configured,
                      lifecycle(
                          ProposalCommitted{expected.proposal_key()},
                          source,
                          index))
                      .status == AdaptiveV2ManagerIngressStatus::
                                     awaiting_corroboration);
        }
        REQUIRE(ingest_lifecycle(
                    manager,
                    configured,
                    lifecycle(
                        ProposalCommitted{expected.proposal_key()},
                        4,
                        index))
                    .status == AdaptiveV2ManagerIngressStatus::processed);

        const auto evidence = manager.ingest_evidence(
            AuthenticatedReporter{1},
            hotstuff::encode_evidence_batch(
                ResponseObservationBatch{
                    hotstuff::kEvidenceBatchSchemaVersion, {expected}},
                configured.evidence_wire));
        CHECK(evidence.status ==
              AdaptiveV2ManagerIngressStatus::processed);
        CHECK(evidence.accepted_observations == 0);
        CHECK(evidence.rejected_observations == 0);
        CHECK(evidence.newly_quarantined_observations == 1);

        const auto reporter_commit = ingest_lifecycle(
            manager,
            configured,
            lifecycle(
                ProposalCommitted{expected.proposal_key(), index},
                1,
                index));
        CHECK(reporter_commit.status ==
              AdaptiveV2ManagerIngressStatus::already_applied);
        CHECK(reporter_commit.lifecycle.retried_observations == 1);
        CHECK(reporter_commit.lifecycle.accepted_observations == 1);
        CHECK(reporter_commit.lifecycle.rejected_observations == 0);
    }

    CHECK(manager.ledger().accepted().size() == 2);
    CHECK(manager.ledger().rejected().empty());
    CHECK(manager.audit_stats().reporter_causal_retained_proposals == 2);
    CHECK(manager.audit_stats().reporter_causal_open_reporters == 6);
    CHECK(manager.healthy());
}

TEST_CASE(
    "N7 reporter quarantine quota rejects overflow without poisoning ingress",
    "[adaptive-v2][manager-ingress][quarantine][capacity][byzantine][n7]")
{
    auto configured = limits();
    configured.lifecycle.maximum_quarantined_records_per_reporter = 2;
    AdaptiveV2ManagerIngress manager(
        membership(), epoch_zero(), 0, 3, configured);

    const auto first = observation(manager, "quota-first", 1, 3, 1);
    const auto second = observation(manager, "quota-second", 1, 3, 2);
    const auto overflow = observation(manager, "quota-overflow", 1, 3, 3);
    const auto saturated = manager.ingest_evidence(
        AuthenticatedReporter{1},
        hotstuff::encode_evidence_batch(
            ResponseObservationBatch{
                hotstuff::kEvidenceBatchSchemaVersion,
                {first, second, overflow}},
            configured.evidence_wire));
    CHECK(saturated.status == AdaptiveV2ManagerIngressStatus::processed);
    CHECK(saturated.processed_observations == 3);
    CHECK(saturated.newly_quarantined_observations == 2);
    CHECK(saturated.quarantine_capacity_rejections == 1);
    CHECK(saturated.rejected_observations == 1);
    CHECK(saturated.remaining_quarantined_observations == 2);
    CHECK(manager.lifecycle_stats().quarantine_quota_rejections == 1);
    CHECK(manager.lifecycle_stats().capacity_failures == 0);
    CHECK(manager.ledger().accepted().empty());
    CHECK(manager.ledger().rejected().empty());
    CHECK(manager.healthy());

    const auto independent = manager.ingest_evidence(
        AuthenticatedReporter{2},
        hotstuff::encode_evidence_batch(
            ResponseObservationBatch{
                hotstuff::kEvidenceBatchSchemaVersion,
                {observation(manager, "quota-independent", 2, 5, 1)}},
            configured.evidence_wire));
    CHECK(independent.newly_quarantined_observations == 1);
    CHECK(independent.quarantine_capacity_rejections == 0);
    CHECK(independent.remaining_quarantined_observations == 3);

    const auto replayed_overflow = manager.ingest_evidence(
        AuthenticatedReporter{1},
        hotstuff::encode_evidence_batch(
            ResponseObservationBatch{
                hotstuff::kEvidenceBatchSchemaVersion, {overflow}},
            configured.evidence_wire));
    CHECK(replayed_overflow.rejected_observations == 1);
    CHECK(replayed_overflow.quarantine_capacity_rejections == 0);
    CHECK(replayed_overflow.remaining_quarantined_observations == 3);
    CHECK(manager.audit_stats().evidence_sequence_rejections == 1);
    CHECK(manager.lifecycle_stats().quarantine_quota_rejections == 1);
    CHECK(manager.lifecycle_stats().capacity_failures == 0);
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

TEST_CASE(
    "recurring ingress rotates exact windows and retains replay fences",
    "[adaptive-v2][manager-ingress][recurring][rotation][n7][contract]")
{
    verify_recurring_ingress_contract<AdaptiveV2ManagerIngress>();
}
