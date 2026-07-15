#include <algorithm>
#include <array>
#include <cstddef>
#include <cstdint>
#include <memory>
#include <optional>
#include <string>
#include <type_traits>
#include <utility>
#include <vector>

#include "catch.hpp"
#include "hotstuff/adaptation.h"
#include "hotstuff/epoch_activation.h"
#include "hotstuff/epoch_store.h"
#include "hotstuff/tree_policy.h"

/*
 * M12 phase-one pure coordination contract
 * -----------------------------------------
 * The coordinator consumes only pinned authenticated identities, immutable
 * E08 accepted evidence, R09/T10 policy values, and D11 epoch messages. Live
 * sockets, TLS handshakes, structured events, process control, and optimized
 * epoch generation remain outside this first slice.
 *
 * The guarded fallback keeps this tests-first target object-compilable while
 * adaptation_manager.h is absent. Its methods are deliberately undefined so
 * the executable has one precise missing-production link boundary.
 */
#if __has_include("hotstuff/adaptation_manager.h")
#include "hotstuff/adaptation_manager.h"
#define KAURI_HAS_M12_ADAPTATION_MANAGER_API 1
#else
#define KAURI_HAS_M12_ADAPTATION_MANAGER_API 0

namespace hotstuff
{

constexpr std::uint32_t kAdaptationManagerSchemaVersion = 1;
constexpr std::size_t kAdaptationCertificateFingerprintBytes = 32;

using AdaptationCertificateFingerprint =
    std::array<std::uint8_t, kAdaptationCertificateFingerprintBytes>;

enum class AdaptationIdentityRole : std::uint8_t
{
    manager = 1,
    replica = 2,
};

struct PinnedAdaptationIdentity
{
    ReplicaID replica_id{0};
    AdaptationIdentityRole role{AdaptationIdentityRole::replica};
    AdaptationCertificateFingerprint certificate_fingerprint;
};

enum class AdaptationManagerState : std::uint8_t
{
    waiting_for_readiness = 1,
    collecting_baseline_evidence,
    containment_generated,
    containment_armed,
    waiting_for_containment_activation,
    containment_converged,
};

enum class AdaptationManagerRecordDisposition : std::uint8_t
{
    accepted = 1,
    duplicate,
    unknown_identity,
    wrong_role,
    payload_identity_mismatch,
    wrong_configuration,
    wrong_generation,
    wrong_state,
    incomplete_evidence,
    mixed_evidence_configuration,
    stale_evidence_cutoff,
    acknowledgement_not_required,
};

struct AdaptationManagerRecordResult
{
    AdaptationManagerRecordDisposition disposition{
        AdaptationManagerRecordDisposition::accepted};
    AdaptationManagerState state{
        AdaptationManagerState::waiting_for_readiness};
};

struct AdaptationManagerDecisionView
{
    const AdaptationSnapshot *snapshot{nullptr};
    const std::vector<ReplicaID> *responsive_replicas{nullptr};
    const TreePlacementResult *placement{nullptr};
    const StageEpochDefinition *stage{nullptr};
};

class AdaptationManagerCoordinator final
{
public:
    AdaptationManagerCoordinator(
        const EpochDefinition &current_epoch,
        ConfigurationId current_configuration,
        std::uint64_t current_activation_generation,
        std::vector<PinnedAdaptationIdentity> pinned_identities,
        AdaptationPolicy adaptation_policy,
        TreePlacementInput placement_input,
        FaultContainmentPolicy containment_policy,
        std::uint32_t tolerated_faults,
        std::uint64_t activation_height,
        std::uint64_t minimum_activation_grace);
    ~AdaptationManagerCoordinator();

    AdaptationManagerCoordinator(
        const AdaptationManagerCoordinator &) = delete;
    AdaptationManagerCoordinator &operator=(
        const AdaptationManagerCoordinator &) = delete;
    AdaptationManagerCoordinator(
        AdaptationManagerCoordinator &&) = delete;
    AdaptationManagerCoordinator &operator=(
        AdaptationManagerCoordinator &&) = delete;

    AdaptationManagerRecordResult record_authenticated_readiness(
        const AdaptationCertificateFingerprint &certificate_fingerprint,
        ReplicaID payload_replica_id,
        const ConfigurationId &active_configuration,
        std::uint64_t activation_generation,
        std::uint64_t committed_height) noexcept;

    AdaptationManagerRecordResult freeze_baseline_evidence(
        AcceptedEvidenceView accepted_evidence,
        std::uint64_t evidence_cutoff);

    std::optional<AdaptationManagerDecisionView>
    containment_decision() const noexcept;

    AdaptationManagerRecordResult record_authenticated_stage_ack(
        const AdaptationCertificateFingerprint &certificate_fingerprint,
        const StageAck &acknowledgement) noexcept;

    std::optional<ArmActivation> arm_activation() const;

    AdaptationManagerRecordResult record_authenticated_activation(
        const AdaptationCertificateFingerprint &certificate_fingerprint,
        const ActivationStatus &status) noexcept;

    AdaptationManagerState state() const noexcept;
    bool converged() const noexcept;
};

} // namespace hotstuff
#endif

namespace
{

using hotstuff::AcceptedEvidenceRecord;
using hotstuff::AcceptedEvidenceView;
using hotstuff::ActivationRecoveryNeed;
using hotstuff::ActivationStatus;
using hotstuff::ActivationTransition;
using hotstuff::AdaptationCertificateFingerprint;
using hotstuff::AdaptationIdentityRole;
using hotstuff::AdaptationManagerCoordinator;
using hotstuff::AdaptationManagerRecordDisposition;
using hotstuff::AdaptationManagerState;
using hotstuff::AdaptationPolicy;
using hotstuff::ConfigurationId;
using hotstuff::EpochDefinition;
using hotstuff::EpochDefinitionInput;
using hotstuff::EpochProtocolMode;
using hotstuff::EpochStore;
using hotstuff::EpochTreeDefinition;
using hotstuff::EpochValidationContext;
using hotstuff::ExpectedMessageType;
using hotstuff::FaultContainmentPolicy;
using hotstuff::PinnedAdaptationIdentity;
using hotstuff::ReplicaArmDisposition;
using hotstuff::ReplicaEpochActivation;
using hotstuff::ReplicaID;
using hotstuff::ResponseObservation;
using hotstuff::ResponseOutcome;
using hotstuff::ResponsivenessClass;
using hotstuff::ReplicaStageDisposition;
using hotstuff::StageAck;
using hotstuff::TreePlacementInput;
using hotstuff::TreeReplicaRole;
using hotstuff::TreeShape;

template <typename Value, typename = void>
struct has_crash_ids : std::false_type
{};

template <typename Value>
struct has_crash_ids<
    Value,
    std::void_t<decltype(std::declval<Value>().crash_ids)>>
    : std::true_type
{};

template <typename Value, typename = void>
struct has_process_ids : std::false_type
{};

template <typename Value>
struct has_process_ids<
    Value,
    std::void_t<decltype(std::declval<Value>().process_ids)>>
    : std::true_type
{};

template <typename Value, typename = void>
struct has_profile_labels : std::false_type
{};

template <typename Value>
struct has_profile_labels<
    Value,
    std::void_t<decltype(std::declval<Value>().profile_labels)>>
    : std::true_type
{};

std::vector<ReplicaID> membership()
{
    return {0, 1, 2, 3, 4, 5, 6};
}

hotstuff::uint256_t digest(const std::string &label)
{
    return hotstuff::DataStream(label).get_hash();
}

EpochDefinitionInput baseline_input()
{
    const auto members = membership();
    EpochDefinitionInput input;
    input.epoch_number = 0;
    input.previous_epoch_digest = {};
    input.membership_digest =
        hotstuff::canonical_membership_digest(members);
    input.trees = {
        EpochTreeDefinition{0, 2, 2, members},
        EpochTreeDefinition{1, 2, 2, {1, 2, 3, 4, 5, 6, 0}},
    };
    input.activation_height = 0;
    input.generation_seed = 0x1200;
    input.policy_version = "m12-baseline-v1";
    input.evidence_snapshot_id = "m12-bootstrap";
    input.evidence_cutoff = 0;
    input.epoch_digest.reset();
    return input;
}

AdaptationCertificateFingerprint fingerprint(std::uint8_t identity)
{
    AdaptationCertificateFingerprint value{};
    for (std::size_t index = 0; index < value.size(); ++index)
        value[index] = static_cast<std::uint8_t>(identity + index);
    return value;
}

std::vector<PinnedAdaptationIdentity> pinned_identities()
{
    std::vector<PinnedAdaptationIdentity> identities{
        {100, AdaptationIdentityRole::manager, fingerprint(100)}};
    for (const auto replica : membership())
    {
        identities.push_back(
            {replica,
             AdaptationIdentityRole::replica,
             fingerprint(static_cast<std::uint8_t>(replica))});
    }
    return identities;
}

AdaptationPolicy adaptation_policy()
{
    AdaptationPolicy policy;
    policy.policy_version = "m12-responsiveness-v1";
    policy.attempt_window = 3;
    policy.minimum_attempts = 3;
    policy.minimum_response_rate_ppm = 750'000;
    policy.maximum_timeout_rate_ppm = 250'000;
    policy.trailing_timeout_streak = 3;
    policy.latency_percentile_basis_points = 5'000;
    return policy;
}

std::vector<AcceptedEvidenceRecord> baseline_evidence(
    const EpochDefinition &epoch,
    const std::vector<ReplicaID> &nonresponsive_replicas = {6})
{
    std::vector<AcceptedEvidenceRecord> records;
    std::uint64_t ingestion_sequence = 0;
    std::uint64_t reporter_sequence = 0;
    for (const auto replica : membership())
    {
        for (std::uint32_t attempt = 0; attempt < 3; ++attempt)
        {
            ResponseObservation observation;
            observation.reporter_id = 0;
            observation.observed_replica_id = replica;
            observation.configuration = {
                epoch.epoch_number(),
                attempt % 2,
                epoch.epoch_digest()};
            observation.block_hash = digest(
                "m12-baseline-" + std::to_string(replica) + "-" +
                std::to_string(attempt));
            observation.expected_message_type =
                ExpectedMessageType::direct_vote;
            observation.deadline_duration_us = 100;
            observation.reporter_sequence = ++reporter_sequence;
            observation.reporter_monotonic_ns =
                reporter_sequence * 1'000;
            if (std::find(
                    nonresponsive_replicas.begin(),
                    nonresponsive_replicas.end(),
                    replica) != nonresponsive_replicas.end())
            {
                observation.outcome = ResponseOutcome::timeout;
            }
            else
            {
                observation.outcome = ResponseOutcome::on_time;
                observation.response_duration_us = 10 + replica * 10;
                observation.signer_set = {replica};
            }
            observation.observation_id =
                hotstuff::compute_response_observation_id(
                    observation.attempt_identity());
            records.push_back(
                {++ingestion_sequence, std::move(observation)});
        }
    }
    return records;
}

bool is_leaf(
    const EpochTreeDefinition &tree,
    ReplicaID replica)
{
    const auto found = std::find(
        tree.members_breadth_first.begin(),
        tree.members_breadth_first.end(),
        replica);
    if (found == tree.members_breadth_first.end())
        return false;
    const auto position = static_cast<std::size_t>(
        found - tree.members_breadth_first.begin());
    const auto first_leaf = tree.members_breadth_first.size() == 1
        ? 0
        : ((tree.members_breadth_first.size() - 2) / tree.fanout) + 1;
    return position >= first_leaf;
}

} // namespace

TEST_CASE("M12 coordinates authenticated containment through survivor convergence",
          "[m12][adaptation-manager][containment][intentional-red]")
{
    static_assert(
        !has_crash_ids<PinnedAdaptationIdentity>::value,
        "manager identities cannot expose crash targets");
    static_assert(
        !has_process_ids<PinnedAdaptationIdentity>::value,
        "manager identities cannot expose process ids");
    static_assert(
        !has_profile_labels<PinnedAdaptationIdentity>::value,
        "manager identities cannot expose performance labels");
    static_assert(
        !has_crash_ids<AdaptationPolicy>::value &&
            !has_crash_ids<TreePlacementInput>::value &&
            !has_crash_ids<FaultContainmentPolicy>::value,
        "policy inputs cannot expose crash targets");
    static_assert(
        !has_process_ids<AdaptationPolicy>::value &&
            !has_process_ids<TreePlacementInput>::value &&
            !has_process_ids<FaultContainmentPolicy>::value,
        "policy inputs cannot expose process ids");
    static_assert(
        !has_profile_labels<AdaptationPolicy>::value &&
            !has_profile_labels<TreePlacementInput>::value &&
            !has_profile_labels<FaultContainmentPolicy>::value,
        "policy inputs cannot expose performance labels");
    static_assert(
        !std::is_copy_constructible<
            AdaptationManagerCoordinator>::value,
        "copying would fork deployment ordering");

    CHECK(KAURI_HAS_M12_ADAPTATION_MANAGER_API == 1);
    CHECK(hotstuff::kAdaptationManagerSchemaVersion == 1);

    EpochStore store{membership()};
    const auto &baseline = store.stage(
        baseline_input(), EpochValidationContext{});
    const ConfigurationId baseline_configuration{
        baseline.epoch_number(), 0, baseline.epoch_digest()};
    const auto baseline_generation =
        hotstuff::checked_activation_generation(
            baseline.epoch_number(), 0);
    REQUIRE(baseline_generation.has_value());

    TreePlacementInput placement_input{
        membership(),
        TreeShape{2, 2, 2},
        0xC012,
        "m12-containment-v1"};
    FaultContainmentPolicy containment_policy{{
        {0, 0},
        {1, 1},
    }};
    constexpr std::uint64_t activation_height = 120;
    constexpr std::uint64_t minimum_activation_grace = 5;

    AdaptationManagerCoordinator manager{
        baseline,
        baseline_configuration,
        *baseline_generation,
        pinned_identities(),
        adaptation_policy(),
        placement_input,
        containment_policy,
        2,
        activation_height,
        minimum_activation_grace};
    CHECK(manager.state() ==
          AdaptationManagerState::waiting_for_readiness);

    const auto identity_mismatch =
        manager.record_authenticated_readiness(
            fingerprint(0),
            1,
            baseline_configuration,
            *baseline_generation,
            100);
    CHECK(identity_mismatch.disposition ==
          AdaptationManagerRecordDisposition::
              payload_identity_mismatch);

    for (const auto replica : membership())
    {
        const auto ready = manager.record_authenticated_readiness(
            fingerprint(static_cast<std::uint8_t>(replica)),
            replica,
            baseline_configuration,
            *baseline_generation,
            100 + replica);
        REQUIRE(ready.disposition ==
                AdaptationManagerRecordDisposition::accepted);
    }
    CHECK(manager.state() ==
          AdaptationManagerState::collecting_baseline_evidence);

    auto evidence = baseline_evidence(baseline);
    const auto cutoff = evidence.back().ingestion_sequence;
    const AcceptedEvidenceView evidence_view{
        evidence.data(), evidence.size()};
    const auto frozen = manager.freeze_baseline_evidence(
        evidence_view, cutoff);
    REQUIRE(frozen.disposition ==
            AdaptationManagerRecordDisposition::accepted);
    REQUIRE(manager.state() ==
            AdaptationManagerState::containment_generated);
    evidence.clear();
    evidence.shrink_to_fit();

    const auto decision = manager.containment_decision();
    REQUIRE(decision.has_value());
    REQUIRE(decision->snapshot != nullptr);
    REQUIRE(decision->responsive_replicas != nullptr);
    REQUIRE(decision->placement != nullptr);
    REQUIRE(decision->stage != nullptr);
    CHECK(decision->snapshot->evidence_cutoff() == cutoff);
    CHECK(*decision->responsive_replicas ==
          std::vector<ReplicaID>{0, 1, 2, 3, 4, 5});
    REQUIRE(decision->snapshot->ranking().size() == membership().size());
    CHECK(decision->snapshot->ranking().back().replica_id == 6);
    CHECK(decision->snapshot->ranking().back().classification ==
          ResponsivenessClass::nonresponsive);

    REQUIRE(decision->placement->trees().size() == 2);
    for (const auto &tree : decision->placement->trees())
        CHECK(is_leaf(tree, 6));
    REQUIRE(decision->stage->definition.trees.size() ==
            decision->placement->trees().size());
    for (std::size_t index = 0;
         index < decision->stage->definition.trees.size();
         ++index)
    {
        const auto &staged_tree =
            decision->stage->definition.trees.at(index);
        const auto &placed_tree =
            decision->placement->trees().at(index);
        CHECK(staged_tree.tree_id == placed_tree.tree_id);
        CHECK(staged_tree.fanout == placed_tree.fanout);
        CHECK(staged_tree.pipeline_stretch ==
              placed_tree.pipeline_stretch);
        CHECK(staged_tree.members_breadth_first ==
              placed_tree.members_breadth_first);
    }
    CHECK(decision->stage->definition.evidence_cutoff == cutoff);
    CHECK(decision->stage->definition.evidence_snapshot_id ==
          decision->snapshot->snapshot_id());
    CHECK(decision->stage->activation.activation_height ==
          activation_height);

    StageAck forged_ack{
        hotstuff::kEpochWireSchemaVersion,
        EpochProtocolMode::adaptive_v1,
        0,
        decision->stage->activation};
    forged_ack.activation.successor_epoch_digest = digest(
        "m12-forged-stage-ack");
    const auto rejected_ack =
        manager.record_authenticated_stage_ack(
            fingerprint(0), forged_ack);
    CHECK(rejected_ack.disposition ==
          AdaptationManagerRecordDisposition::wrong_configuration);
    CHECK_FALSE(manager.arm_activation().has_value());

    for (const auto replica : *decision->responsive_replicas)
    {
        const StageAck acknowledgement{
            hotstuff::kEpochWireSchemaVersion,
            EpochProtocolMode::adaptive_v1,
            replica,
            decision->stage->activation};
        const auto recorded = manager.record_authenticated_stage_ack(
            fingerprint(static_cast<std::uint8_t>(replica)),
            acknowledgement);
        REQUIRE(recorded.disposition ==
                AdaptationManagerRecordDisposition::accepted);
        if (replica != decision->responsive_replicas->back())
            CHECK_FALSE(manager.arm_activation().has_value());
    }

    const auto arm = manager.arm_activation();
    REQUIRE(arm.has_value());
    CHECK(arm->activation == decision->stage->activation);
    CHECK(manager.state() ==
          AdaptationManagerState::containment_armed);

    ActivationStatus wrong_activation{
        hotstuff::kEpochWireSchemaVersion,
        EpochProtocolMode::adaptive_v1,
        0,
        arm->activation,
        ActivationRecoveryNeed::none};
    wrong_activation.activation.successor_epoch_digest = digest(
        "m12-wrong-activation");
    const auto rejected_activation =
        manager.record_authenticated_activation(
            fingerprint(0), wrong_activation);
    CHECK(rejected_activation.disposition ==
          AdaptationManagerRecordDisposition::wrong_configuration);
    CHECK_FALSE(manager.converged());

    for (const auto replica : *decision->responsive_replicas)
    {
        EpochStore replica_store{membership()};
        const auto &replica_baseline = replica_store.stage(
            baseline_input(), EpochValidationContext{});
        ReplicaEpochActivation replica_activation{
            replica_store, replica_baseline, replica};
        CHECK_FALSE(replica_activation.active_status().has_value());

        const auto staged = replica_activation.stage(
            *decision->stage,
            EpochValidationContext{
                106, minimum_activation_grace, {6}});
        REQUIRE(staged.disposition == ReplicaStageDisposition::staged);
        REQUIRE(staged.acknowledgement.has_value());
        CHECK_FALSE(replica_activation.active_status().has_value());
        REQUIRE(replica_activation.arm(*arm) ==
                ReplicaArmDisposition::armed);
        CHECK_FALSE(replica_activation.active_status().has_value());

        const auto activated_commit =
            replica_activation.on_predecessor_commit(
                activation_height, baseline.epoch_digest());
        REQUIRE(activated_commit.transition ==
                ActivationTransition::activated);
        const auto status = replica_activation.active_status();
        REQUIRE(status.has_value());
        CHECK(status->wire_schema_version ==
              hotstuff::kEpochWireSchemaVersion);
        CHECK(status->protocol_mode == EpochProtocolMode::adaptive_v1);
        CHECK(status->replica_id == replica);
        CHECK(status->activation == arm->activation);
        CHECK(status->recovery_need == ActivationRecoveryNeed::none);
        CHECK_FALSE(replica_activation.recovery_status().has_value());

        const auto activated =
            manager.record_authenticated_activation(
                fingerprint(static_cast<std::uint8_t>(replica)),
                *status);
        REQUIRE(activated.disposition ==
                AdaptationManagerRecordDisposition::accepted);
        if (replica != decision->responsive_replicas->back())
            CHECK_FALSE(manager.converged());
    }

    CHECK(manager.converged());
    CHECK(manager.state() ==
          AdaptationManagerState::containment_converged);
}

TEST_CASE("M12 fixed fault quorum fails closed below two f plus one",
          "[m12][adaptation-manager][quorum][intentional-red]")
{
    EpochStore store{membership()};
    const auto &baseline = store.stage(
        baseline_input(), EpochValidationContext{});
    const ConfigurationId baseline_configuration{
        baseline.epoch_number(), 0, baseline.epoch_digest()};
    const auto baseline_generation =
        hotstuff::checked_activation_generation(
            baseline.epoch_number(), 0);
    REQUIRE(baseline_generation.has_value());

    constexpr std::uint32_t tolerated_faults = 2;
    AdaptationManagerCoordinator manager{
        baseline,
        baseline_configuration,
        *baseline_generation,
        pinned_identities(),
        adaptation_policy(),
        TreePlacementInput{
            membership(),
            TreeShape{2, 2, 2},
            0xC012,
            "m12-containment-v1"},
        FaultContainmentPolicy{{
            {0, 0},
            {1, 1},
        }},
        tolerated_faults,
        120,
        5};

    for (const auto replica : membership())
    {
        const auto ready = manager.record_authenticated_readiness(
            fingerprint(static_cast<std::uint8_t>(replica)),
            replica,
            baseline_configuration,
            *baseline_generation,
            100 + replica);
        REQUIRE(ready.disposition ==
                AdaptationManagerRecordDisposition::accepted);
    }
    REQUIRE(manager.state() ==
            AdaptationManagerState::collecting_baseline_evidence);

    auto evidence = baseline_evidence(baseline, {4, 5, 6});
    const auto cutoff = evidence.back().ingestion_sequence;
    const AcceptedEvidenceView evidence_view{
        evidence.data(), evidence.size()};
    const auto frozen = manager.freeze_baseline_evidence(
        evidence_view, cutoff);

    CHECK(frozen.disposition ==
          AdaptationManagerRecordDisposition::incomplete_evidence);
    CHECK(manager.state() ==
          AdaptationManagerState::collecting_baseline_evidence);
    CHECK_FALSE(manager.containment_decision().has_value());
    CHECK_FALSE(manager.arm_activation().has_value());
}

TEST_CASE("M12 accepts readiness for the explicit rotated generation",
          "[m12][adaptation-manager][generation][rotation]"
          "[intentional-red]")
{
    EpochStore store{membership()};
    const auto &baseline = store.stage(
        baseline_input(), EpochValidationContext{});
    constexpr std::uint32_t active_tree = 1;
    constexpr std::uint64_t rotation_ordinal = 1;
    const ConfigurationId rotated_configuration{
        baseline.epoch_number(), active_tree, baseline.epoch_digest()};
    const auto initial_generation =
        hotstuff::checked_activation_generation(
            baseline.epoch_number(), 0);
    const auto rotated_generation =
        hotstuff::checked_activation_generation(
            baseline.epoch_number(), rotation_ordinal);
    REQUIRE(initial_generation.has_value());
    REQUIRE(rotated_generation.has_value());

    AdaptationManagerCoordinator manager{
        baseline,
        rotated_configuration,
        *rotated_generation,
        pinned_identities(),
        adaptation_policy(),
        TreePlacementInput{
            membership(),
            TreeShape{2, 2, 2},
            0xC012,
            "m12-containment-v1"},
        FaultContainmentPolicy{{
            {0, 0},
            {1, 1},
        }},
        2,
        120,
        5};

    const auto stale_generation =
        manager.record_authenticated_readiness(
            fingerprint(0),
            0,
            rotated_configuration,
            *initial_generation,
            100);
    CHECK(stale_generation.disposition ==
          AdaptationManagerRecordDisposition::wrong_generation);

    for (const auto replica : membership())
    {
        const auto ready = manager.record_authenticated_readiness(
            fingerprint(static_cast<std::uint8_t>(replica)),
            replica,
            rotated_configuration,
            *rotated_generation,
            100 + replica);
        REQUIRE(ready.disposition ==
                AdaptationManagerRecordDisposition::accepted);
    }
    CHECK(manager.state() ==
          AdaptationManagerState::collecting_baseline_evidence);
}
