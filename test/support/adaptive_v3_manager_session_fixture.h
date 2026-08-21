#ifndef KAURI_TEST_SUPPORT_ADAPTIVE_V3_MANAGER_SESSION_FIXTURE_H
#define KAURI_TEST_SUPPORT_ADAPTIVE_V3_MANAGER_SESSION_FIXTURE_H

#include "catch.hpp"

#include <algorithm>
#include <memory>
#include <string>
#include <utility>
#include <vector>

#include "hotstuff/adaptation_manager_profile.h"
#include "hotstuff/adaptive_v3_activation_readiness.h"
#include "hotstuff/adaptive_v3_manager_session.h"
#include "support/adaptive_manager_evidence_fixture.h"

namespace kauri::test_support::cert13 {

using namespace hotstuff;
using kauri::test_support::AdaptiveManagerEvidenceDriver;

constexpr std::uint32_t kIssuerId = 17;
constexpr std::uint64_t kGeneration = 3;
constexpr std::uint64_t kSeed = 0xa2f7;

inline uint256_t digest(const std::string &label) { return DataStream(label).get_hash(); }

inline PrivKeySecp256k1 issuer_key()
{
    PrivKeySecp256k1 key;
    key.from_hex("4aede145d13021fb43c938bced67511a7740c05786d3e0b94ffbdaa7f15afc57");
    return key;
}

inline std::vector<ReplicaID> members(std::size_t count)
{
    std::vector<ReplicaID> result;
    for (ReplicaID id = 0; id < count; ++id) result.push_back(id);
    return result;
}

enum class BlsMembershipConstruction {
    random,
    deterministic_fixed_scalars,
};

inline bytearray_t deterministic_bls_private_scalar(ReplicaID id)
{
    bytearray_t scalar(32, 0);
    scalar.back() = static_cast<std::uint8_t>(id + 1U);
    return scalar;
}

struct BlsMembership {
    std::vector<std::shared_ptr<const PrivKeyBLS>> private_keys;
    std::vector<std::pair<ReplicaID, PubKeyBLS>> public_keys;
    explicit BlsMembership(
        std::size_t count,
        BlsMembershipConstruction construction = BlsMembershipConstruction::random) {
        for (ReplicaID id = 0; id < count; ++id) {
            auto mutable_key = construction == BlsMembershipConstruction::random
                ? std::make_shared<PrivKeyBLS>()
                : std::make_shared<PrivKeyBLS>(deterministic_bls_private_scalar(id));
            if (construction == BlsMembershipConstruction::random)
                mutable_key->from_rand();
            std::shared_ptr<const PrivKeyBLS> key = mutable_key;
            public_keys.emplace_back(id, PubKeyBLS(*key));
            private_keys.push_back(std::move(key));
        }
    }
};

inline AdaptiveV3ManagerSessionConfig config_for(
    const std::vector<ReplicaID> &replicas,
    const AdaptiveV2ManagerRuntimeShape &shape,
    const EpochDefinitionInput &initial,
    const BlsMembership &bls,
    std::size_t release_count,
    std::uint64_t tick_scale = 1,
    bool exact_v13_fault_window_contract = false)
{
    AdaptiveV3ManagerSessionConfig config;
    config.active_tree_id = 0;
    config.activation_generation = exact_v13_fault_window_contract
        ? *checked_activation_generation(0, 0)
        : kGeneration;
    config.ingress_limits = shape.ingress_limits;
    config.controller.selection.required_nonresponsive =
        replicas.size() == 31 ? 3 : shape.required_nonresponsive;
    config.controller.selection.minimum_score_drop =
        replicas.size() == 31 ? 22 : shape.minimum_score_drop;
    config.controller.selection.minimum_timeouts_per_reporter =
        replicas.size() == 31 ? 1 : 2;
    config.controller.selection.maximum_post_baseline_timeout_attempts =
        shape.maximum_post_baseline_timeout_attempts;
    auto &policy = config.controller.selection.responsiveness_policy;
    policy.policy_version = exact_v13_fault_window_contract
        ? "adaptive-v2-controller-responsiveness-v1"
        : "cert13-session-v1";
    policy.attempt_window = exact_v13_fault_window_contract ? 32 : 64;
    policy.minimum_attempts = 2;
    policy.minimum_response_rate_ppm = replicas.size() == 31 ? 1000000 : 750000;
    policy.maximum_timeout_rate_ppm = replicas.size() == 31 ? 0 : 250000;
    policy.trailing_timeout_streak = 2;
    policy.latency_percentile_basis_points = 5000;
    if (replicas.size() == 31 || exact_v13_fault_window_contract)
        config.controller.selection.cardinality_policy =
            AdaptiveV2FaultWindowCardinalityPolicy::all_guarded_up_to_fault_bound_v1;
    config.controller.selection.fault_window_arm_required =
        replicas.size() == 31 || exact_v13_fault_window_contract;
    config.controller.selection.snapshot_seed = kSeed;
    auto placement_shape = shape.tree_shape;
    if (replicas.size() == 7) placement_shape.tree_count = 5;
    config.controller.placement = {replicas, placement_shape, kSeed,
        "cert13-session-v1"};
    config.controller.activation_delay_blocks = 5;
    config.controller.issuer_id = kIssuerId;
    config.controller.issuer_private_key = issuer_key();
    config.controller.bundle_limits = shape.bundle_limits;
    for (std::size_t index = 0; index < placement_shape.tree_count; ++index) {
        const auto &tree = initial.trees.at(index);
        config.controller.transition_policy.containment_baseline_roots.push_back(
            {tree.tree_id, tree.members_breadth_first.front()});
    }
    config.controller.successor_protocol_mode = EpochProtocolMode::adaptive_v3;
    config.readiness_membership = bls.public_keys;
    config.required_release_count = release_count;
    config.maximum_delivery_attempts = 3;
    config.retry_interval_ticks = 2 * tick_scale;
    config.pre_certificate_window_ticks = 100 * tick_scale;
    config.delivery_window_ticks = 100 * tick_scale;
    config.residency_ticks = 65000 * tick_scale;
    config.common_commit_window_ticks = 5000 * tick_scale;
    config.common_commit_stabilization_ticks = 60000 * tick_scale;
    config.e2_reserve_ticks = 90000 * tick_scale;
    config.expected_cycle_count = 2;
    config.wire_limits = {64 * 1024, replicas.size()};
    return config;
}

struct Fixture {
    std::uint64_t tick_scale{1};
    bool exact_v13_fault_window_contract{false};
    std::vector<ReplicaID> replicas;
    AdaptiveV2ManagerRuntimeShape shape;
    EpochDefinitionInput initial;
    BlsMembership bls;
    AdaptiveV3ManagerSessionConfig config;
    AdaptiveV3ManagerSession session;
    AdaptiveManagerEvidenceDriver<AdaptiveV3ManagerSession> evidence;

    Fixture(std::size_t count, std::vector<ReplicaID> surviving,
            std::uint64_t expected_cycle_count = 2,
            BlsMembershipConstruction membership_construction =
                BlsMembershipConstruction::random,
            std::uint64_t tick_scale = 1,
            bool exact_v13_fault_window_contract = false)
        : tick_scale(tick_scale),
          exact_v13_fault_window_contract(exact_v13_fault_window_contract),
          replicas(members(count)),
          shape(*derive_adaptive_v2_manager_runtime_shape(
              replicas, count == 7 ? 2 : 5, 2)),
          initial(*derive_adaptive_v2_cyclic_epoch_zero(
              replicas, count == 7 ? 2 : 5, 2)),
          bls(count, membership_construction), config([&] {
              auto result = config_for(
                  replicas, shape, initial, bls, surviving.size(), tick_scale,
                  exact_v13_fault_window_contract);
              result.expected_cycle_count = expected_cycle_count;
              return result;
          }()),
          session(replicas, initial, config),
          evidence(session, replicas, std::move(surviving), config.ingress_limits) {}

    std::uint32_t tree_with_reporter_in(
        ReplicaID target,
        const std::vector<ReplicaID> &eligible_reporters) const
    {
        for (const auto &tree : session.ingress().current_epoch().trees()) {
            const auto found = std::find(
                tree.members_breadth_first.begin(),
                tree.members_breadth_first.end(), target);
            if (found == tree.members_breadth_first.end()) continue;
            const auto position = static_cast<std::size_t>(std::distance(
                tree.members_breadth_first.begin(), found));
            if (position == 0) continue;
            const auto reporter = tree.members_breadth_first[
                (position - 1U) / tree.fanout];
            if (std::find(eligible_reporters.begin(), eligible_reporters.end(),
                          reporter) != eligible_reporters.end())
                return tree.tree_id;
        }
        FAIL("replica has no exact tree edge with an eligible reporter");
        return 0;
    }

    ReplicaID survivor_child_for_tree(
        std::uint32_t tree_id,
        const std::vector<ReplicaID> &survivors) const
    {
        const auto &trees = session.ingress().current_epoch().trees();
        const auto tree = std::find_if(
            trees.begin(), trees.end(), [tree_id](const auto &entry) {
                return entry.tree_id == tree_id;
            });
        REQUIRE(tree != trees.end());
        for (std::size_t position = 1;
             position < tree->members_breadth_first.size(); ++position) {
            const auto child = tree->members_breadth_first[position];
            const auto reporter = tree->members_breadth_first[
                (position - 1U) / tree->fanout];
            if (std::find(survivors.begin(), survivors.end(), child) !=
                    survivors.end() &&
                std::find(survivors.begin(), survivors.end(), reporter) !=
                    survivors.end())
                return child;
        }
        FAIL("tree lacks an exact surviving reporter-child edge");
        return 0;
    }

    void select_containment(const std::vector<ReplicaID> &failed,
                            bool arm_hard_deadline = true,
                            std::uint64_t hard_deadline_tick = 1'000'000'000) {
        AdaptiveV2TransitionPolicy policy;
        policy.intent = TreePolicyKind::fault_containment;
        policy.containment_baseline_roots =
            config.controller.transition_policy.containment_baseline_roots;
        REQUIRE(session.begin_cycle(policy));
        if (failed.size() <= 2 && arm_hard_deadline)
            REQUIRE(session.arm_hard_deadline(hard_deadline_tick));
        REQUIRE(session.evaluate() == AdaptiveV2ManagerControllerStatus::awaiting_readiness);
        evidence.ready_all(); // E0 observation predates the two crash receipts.
        if (failed.size() > 2) {
            std::uint64_t baseline_attempt = 1;
            for (const auto replica : replicas)
                for (std::uint32_t attempt = 0; attempt < 2; ++attempt)
                    evidence.record_for_tree(replica,
                        (replica + 1U) % 31U,
                        ResponseOutcome::on_time, "n31-prearm-baseline",
                        baseline_attempt++);
        } else {
            evidence.responsive_baseline();
        }
        REQUIRE(session.evaluate() == AdaptiveV2ManagerControllerStatus::baseline_frozen);
        if (failed.size() > 2) {
            AdaptiveV2FaultWindowArm arm;
            arm.predecessor_epoch_number = session.ingress().current_epoch().epoch_number();
            arm.predecessor_epoch_digest = session.ingress().current_epoch().epoch_digest();
            arm.evidence_start_monotonic_ns = 1'000'000;
            arm.prefault_tree_id = 20;
            for (std::uint32_t offset = 0; offset < 31; ++offset)
                arm.required_tree_ids.push_back((20U + offset) % 31U);
            arm.evidence_basis =
                AdaptiveV2FaultWindowEvidenceBasis::exact_timeout_attempt_id_v1;
            arm.snapshot_evidence_basis =
                AdaptiveV2FaultWindowSnapshotEvidenceBasis::exact_post_fault_attempt_start_v1;
            arm.cardinality_policy =
                AdaptiveV2FaultWindowCardinalityPolicy::all_guarded_up_to_fault_bound_v1;
            REQUIRE(session.arm_fault_window(std::move(arm)));
            if (arm_hard_deadline)
                REQUIRE(session.arm_hard_deadline(hard_deadline_tick));
            std::vector<ReplicaID> survivors;
            for (const auto replica : replicas)
                if (std::find(failed.begin(), failed.end(), replica) == failed.end())
                    survivors.push_back(replica);
            std::uint64_t survivor_attempt = 1'010'000;
            for (const auto survivor : survivors)
                for (std::uint32_t attempt = 0; attempt < 2; ++attempt)
                    evidence.record_for_tree(survivor,
                        (survivor + 1U) % 31U,
                        ResponseOutcome::on_time, "n31-survivor-baseline",
                        survivor_attempt++);
        }
        if (failed.size() > 2) {
            std::uint64_t attempt_start = 1'100'000;
            for (const auto target : failed)
                for (std::uint32_t tree = 0; tree < 31; ++tree) {
                    if (tree == target) continue;
                    evidence.record_for_tree(target, tree,
                        ResponseOutcome::timeout, "n31-guarded-timeout",
                        attempt_start++);
                    evidence.record_for_tree(target, tree,
                        ResponseOutcome::timeout, "n31-guarded-timeout",
                        attempt_start++);
                }
        } else {
            evidence.persistent_timeouts(failed);
        }
        const auto selected = session.evaluate();
        const auto *bundle = session.successor_bundle();
        if (bundle == nullptr) {
            INFO("session controller status=" << static_cast<unsigned>(selected));
            REQUIRE(session.controller_failure_detail() != nullptr);
            INFO("failure stage=" << static_cast<unsigned>(
                session.controller_failure_detail()->stage));
            CAPTURE(session.controller_failure_detail()->epoch_factory_status.has_value());
            if (session.controller_failure_detail()->epoch_factory_status)
                CAPTURE(static_cast<unsigned>(
                    *session.controller_failure_detail()->epoch_factory_status));
            if (session.controller_failure_detail()->selection_status)
                INFO("selection=" << static_cast<unsigned>(
                    *session.controller_failure_detail()->selection_status));
            if (session.controller_failure_detail()->epoch_factory_status)
                INFO("factory=" << static_cast<unsigned>(
                    *session.controller_failure_detail()->epoch_factory_status));
            if (!session.controller_failure_detail()->epoch_factory_status) {
                REQUIRE(session.controller_failure_detail()->selection_status.has_value());
                REQUIRE(*session.controller_failure_detail()->selection_status ==
                        AdaptiveV2SelectionStatus::selected);
            } else {
                REQUIRE(*session.controller_failure_detail()->epoch_factory_status ==
                        AdaptiveV2EpochFactoryStatus::success);
            }
            FAIL("v3 bundle unavailable after controller selection");
        }
        REQUIRE(bundle != nullptr);
        INFO("controller status=" << static_cast<unsigned>(selected));
        CHECK(bundle->definition().epoch_digest.has_value());
        CHECK(bundle->definition().epoch_number ==
              bundle->command().payload.successor_epoch_number);
        CHECK(make_adaptive_v3_transition_projection(
                  *bundle, session.ingress().current_epoch(), 0,
                  config.readiness_membership).has_value());
        REQUIRE(selected == AdaptiveV2ManagerControllerStatus::successor_ready);
    }

    AdaptiveV2FaultWindowArm select_containment_exact_v13(
        const std::vector<ReplicaID> &failed,
        const std::vector<ReplicaID> &survivors,
        std::uint64_t evidence_start_tick,
        std::uint64_t hard_deadline_tick)
    {
        REQUIRE(exact_v13_fault_window_contract);
        REQUIRE(replicas.size() == 7);
        REQUIRE(failed == std::vector<ReplicaID>{0, 1});
        REQUIRE(survivors == std::vector<ReplicaID>{2, 3, 4, 5, 6});

        AdaptiveV2TransitionPolicy policy;
        policy.intent = TreePolicyKind::fault_containment;
        policy.containment_baseline_roots =
            config.controller.transition_policy.containment_baseline_roots;
        REQUIRE(session.begin_cycle(policy));
        REQUIRE(session.evaluate() ==
                AdaptiveV2ManagerControllerStatus::awaiting_readiness);
        evidence.ready_all();

        std::uint64_t attempt_start = evidence_start_tick / 5U;
        for (const auto replica : replicas) {
            const auto tree = tree_with_reporter_in(replica, replicas);
            for (std::uint32_t attempt = 0; attempt < 2; ++attempt)
                evidence.record_for_tree(
                    replica, tree, ResponseOutcome::on_time,
                    "v13-prearm-baseline", attempt_start++);
        }
        REQUIRE(session.evaluate() ==
                AdaptiveV2ManagerControllerStatus::baseline_frozen);

        AdaptiveV2FaultWindowArm arm;
        arm.predecessor_epoch_number =
            session.ingress().current_epoch().epoch_number();
        arm.predecessor_epoch_digest =
            session.ingress().current_epoch().epoch_digest();
        arm.evidence_start_monotonic_ns = evidence_start_tick;
        arm.prefault_tree_id = 6;
        arm.required_tree_ids = {6, 0, 1, 2, 3, 4};
        arm.evidence_basis =
            AdaptiveV2FaultWindowEvidenceBasis::exact_timeout_attempt_id_v1;
        arm.snapshot_evidence_basis =
            AdaptiveV2FaultWindowSnapshotEvidenceBasis::
                exact_post_fault_attempt_start_v1;
        arm.cardinality_policy =
            AdaptiveV2FaultWindowCardinalityPolicy::
                all_guarded_up_to_fault_bound_v1;
        REQUIRE(session.arm_fault_window(arm));
        REQUIRE(session.arm_hard_deadline(hard_deadline_tick));

        attempt_start = evidence_start_tick + (tick_scale / 2U);
        for (const auto survivor : survivors) {
            const auto tree = tree_with_reporter_in(survivor, survivors);
            for (std::uint32_t attempt = 0; attempt < 2; ++attempt)
                evidence.record_for_tree(
                    survivor, tree, ResponseOutcome::on_time,
                    "v13-postarm-survivor", attempt_start++);
        }
        for (const auto tree_id : arm.required_tree_ids) {
            if (tree_id == 2 || tree_id == 3 || tree_id == 4 || tree_id == 6)
                continue;
            evidence.record_for_tree(
                survivor_child_for_tree(tree_id, survivors), tree_id,
                ResponseOutcome::on_time, "v13-postarm-coverage",
                attempt_start++);
        }
        const std::vector<std::pair<ReplicaID, std::uint32_t>> guarded_edges{
            {0, 2}, {0, 4}, {0, 6},
            {1, 2}, {1, 3}, {1, 6},
        };
        for (const auto &[target, tree] : guarded_edges)
            for (std::uint32_t attempt = 0; attempt < 2; ++attempt)
                evidence.record_for_tree(
                    target, tree, ResponseOutcome::timeout,
                    "v13-postarm-timeout", attempt_start++);

        REQUIRE(session.evaluate() ==
                AdaptiveV2ManagerControllerStatus::successor_ready);
        REQUIRE(session.successor_bundle() != nullptr);
        return arm;
    }

    void select_optimization_exact_v13(
        const std::vector<ReplicaID> &survivors,
        std::uint64_t evidence_start_tick)
    {
        REQUIRE(exact_v13_fault_window_contract);
        evidence.ready(survivors);
        std::uint64_t attempt_start = evidence_start_tick;
        for (const auto survivor : survivors) {
            const auto tree = tree_with_reporter_in(survivor, survivors);
            for (std::uint32_t attempt = 0; attempt < 2; ++attempt)
                evidence.record_for_tree(
                    survivor, tree, ResponseOutcome::on_time,
                    "v13-e2-baseline", attempt_start++);
        }
        REQUIRE(session.evaluate() ==
                AdaptiveV2ManagerControllerStatus::baseline_frozen);
        const std::vector<std::size_t> suffix_attempts{2, 3, 4, 2, 5};
        REQUIRE(survivors.size() == suffix_attempts.size());
        for (std::size_t index = 0; index < survivors.size(); ++index) {
            const auto survivor = survivors[index];
            const auto tree = tree_with_reporter_in(survivor, survivors);
            for (std::size_t attempt = 0;
                 attempt < suffix_attempts[index]; ++attempt)
                evidence.record_for_tree(
                    survivor, tree, ResponseOutcome::on_time,
                    "v13-e2-selection", attempt_start++);
        }
        REQUIRE(session.evaluate() ==
                AdaptiveV2ManagerControllerStatus::successor_ready);
        REQUIRE(session.successor_bundle() != nullptr);
    }

    AdaptiveV3ActivationSchedule schedule(const AdaptiveV3EpochChangeBundle &bundle,
                                          std::uint64_t height,
                                          const uint256_t &block) const {
        const auto &payload = bundle.command().payload;
        AdaptiveV3ActivationSchedule result;
        // The replica gate validates the immutable consensus membership
        // schedule by replica IDs; it separately derives the BLS-bound
        // readiness-membership digest carried in each signed observation.
        result.membership_digest = canonical_membership_digest(replicas);
        result.predecessor_epoch_number = session.ingress().current_configuration().epoch_number;
        result.predecessor_epoch_digest = session.ingress().current_configuration().epoch_digest;
        result.successor_epoch_number = payload.successor_epoch_number;
        result.successor_epoch_digest = payload.successor_epoch_digest;
        result.successor_activation_generation = *checked_activation_generation(
            payload.successor_epoch_number, 0);
        result.command_payload_digest = epoch_change_payload_digest(payload);
        result.command_block_height = height;
        result.command_block_hash = block;
        result.activation_delay_blocks = payload.activation_delay_blocks;
        result.activation_height = height + payload.activation_delay_blocks;
        return result;
    }
};

inline ActivationReadinessAckV1 ack_for(
    ReplicaID recipient,
    const ActivationReadinessCertificateV1 &certificate,
    const AdaptiveV3CertificateDelivery &delivery)
{
    ActivationReadinessAckV1 result;
    result.acknowledged_opcode = MsgActivationReadinessCertificate::opcode;
    result.recipient_replica_id = recipient;
    result.identity = certificate.identity;
    result.certificate_digest = certificate.certificate_digest;
    result.payload_digest = delivery.payload_digest;
    return result;
}

struct CompletedReadinessArtifacts {
    bytearray_t bundle_canonical_bytes;
    bytearray_t identity_bytes;
    bytearray_t certificate_bytes;
    uint256_t certificate_digest;
    std::vector<bytearray_t> acknowledgement_payloads;
    std::vector<ActivationReadyObservationV1> observations;
    std::vector<bytearray_t> observation_payloads;
    std::uint64_t readiness_tick{0};
    std::uint64_t certificate_tick{0};
    std::uint64_t first_delivery_tick{0};
    std::uint64_t final_ack_tick{0};
};

inline CompletedReadinessArtifacts complete_readiness_capture(
    Fixture &fixture,
    const std::vector<ReplicaID> &survivors,
    std::uint64_t tick,
    std::uint64_t command_height,
    const std::string &label,
    std::uint64_t signer_source_sequence = 1,
    std::uint64_t boundary_raw_ns = 1000)
{
    CompletedReadinessArtifacts artifacts;
    const auto *bundle = fixture.session.successor_bundle();
    REQUIRE(bundle != nullptr);
    artifacts.bundle_canonical_bytes = bundle->canonical_bytes();
    artifacts.readiness_tick = tick;
    artifacts.certificate_tick = tick;
    artifacts.first_delivery_tick = tick + fixture.tick_scale;
    const auto command_hash = digest(label + "-command");
    const auto schedule = fixture.schedule(*bundle, command_height, command_hash);
    std::vector<std::unique_ptr<AdaptiveV3CertifiedActivationGate>> gates;
    gates.reserve(survivors.size());
    REQUIRE(fixture.session.begin_readiness(tick));
    for (const auto replica : survivors) {
        gates.emplace_back(std::make_unique<AdaptiveV3CertifiedActivationGate>(
            schedule, replica, fixture.bls.private_keys.at(replica),
            [&fixture] { std::vector<AdaptiveV3ReadinessMember> out;
                for (const auto &member : fixture.bls.public_keys)
                    out.push_back({member.first, member.second}); return out; }(),
            fixture.shape.quorum.quorum));
        const auto boundary = gates.back()->observe_predecessor_commit(
            schedule.activation_height,
            fixture.session.ingress().current_configuration(),
            fixture.session.ingress().activation_generation(), digest(label + "-boundary"),
            signer_source_sequence, boundary_raw_ns + replica);
        REQUIRE(boundary.observation.has_value());
        REQUIRE(boundary.disposition == AdaptiveV3BoundaryDisposition::prepared);
        const auto encoded = encode_activation_ready_observation(
            *boundary.observation, fixture.config.wire_limits);
        artifacts.observations.push_back(*boundary.observation);
        artifacts.observation_payloads.push_back(encoded);
        const auto observed = fixture.session.observe_readiness(replica, tick, encoded);
        REQUIRE((observed.disposition == AdaptiveV3ManagerReadinessDisposition::accepted ||
                 observed.disposition == AdaptiveV3ManagerReadinessDisposition::released));
    }
    const auto *certificate = fixture.session.certificate();
    REQUIRE(certificate != nullptr);
    REQUIRE(certificate->observations.size() == survivors.size());
    // A retry already in flight when R releases must remain observable as an
    // exact duplicate while delivery is active.  It must not degrade into a
    // digest-less invalid observation merely because the collector advanced
    // to the distributing phase.
    const auto duplicate = fixture.session.observe_readiness(
        survivors.back(), tick, artifacts.observation_payloads.back());
    REQUIRE(duplicate.disposition ==
            AdaptiveV3ManagerReadinessDisposition::duplicate);
    REQUIRE(duplicate.observation_digest.has_value());
    REQUIRE_FALSE(duplicate.certificate_assembled);
    artifacts.identity_bytes = encode_activation_ready_identity_v1(
        certificate->identity, fixture.config.wire_limits);
    artifacts.certificate_bytes = encode_activation_readiness_certificate(
        *certificate, fixture.config.wire_limits);
    artifacts.certificate_digest = certificate->certificate_digest;
    for (auto &gate : gates)
        REQUIRE(gate->observe_certificate(*certificate) == AdaptiveV3CertificateDisposition::accepted);
    auto delivery_tick = tick + fixture.tick_scale;
    for (const auto recipient : survivors) {
        const auto delivery = fixture.session.begin_delivery(recipient, delivery_tick);
        REQUIRE(delivery.has_value());
        REQUIRE(fixture.session.record_delivery_result(recipient, delivery->attempt, true, delivery_tick) ==
                AdaptiveV3CertificateDeliveryDisposition::queued);
        const auto acknowledgement = encode_activation_readiness_ack(
            ack_for(recipient, *certificate, *delivery), fixture.config.wire_limits);
        artifacts.acknowledgement_payloads.push_back(acknowledgement);
        REQUIRE(fixture.session.acknowledge(recipient, delivery_tick, acknowledgement) ==
                AdaptiveV3CertificateDeliveryDisposition::acknowledged);
        artifacts.final_ack_tick = delivery_tick;
        delivery_tick += fixture.tick_scale;
    }
    return artifacts;
}

inline void complete_readiness(Fixture &fixture,
                               const std::vector<ReplicaID> &survivors,
                               std::uint64_t tick,
                               std::uint64_t command_height,
                               const std::string &label)
{
    static_cast<void>(complete_readiness_capture(
        fixture, survivors, tick, command_height, label));
}

inline void record_common_commit(Fixture &fixture,
                                 const std::vector<ReplicaID> &signers,
                                 std::uint64_t tick,
                                 const std::string &label)
{
    const ProposalKey proposal{fixture.session.ingress().current_configuration(),
                               digest(label)};
    const auto fault_threshold = static_cast<std::size_t>(
        fixture.session.ingress().quorum_metadata().fault_threshold);
    // The pre-E2 witness consists only of the R distinct, authenticated,
    // timed lifecycle reports.  In particular, an untimed ingress quorum or
    // duplicate already-applied receipts must not make this gate eligible.
    for (std::size_t index = 0; index < signers.size(); ++index) {
        const auto signer = signers.at(index);
        const ProposalLifecycleNotice notice{
            kProposalLifecycleNoticeSchemaVersion, signer,
            fixture.evidence.next_lifecycle_sequence(signer),
            ProposalLifecycleFact{ProposalCommitted{proposal,
                fixture.evidence.evidence_sequence(signer)}}};
        const auto result = fixture.session.ingest_timed_lifecycle(
            AuthenticatedReporter{signer},
            MsgProposalLifecycleNotice{notice, fixture.config.ingress_limits.lifecycle_wire}, tick);
        REQUIRE(result.status == (index < fault_threshold
            ? AdaptiveV2ManagerIngressStatus::awaiting_corroboration
            : index == fault_threshold ? AdaptiveV2ManagerIngressStatus::processed
                                       : AdaptiveV2ManagerIngressStatus::already_applied));
    }
}

} // namespace kauri::test_support::cert13

#endif // KAURI_TEST_SUPPORT_ADAPTIVE_V3_MANAGER_SESSION_FIXTURE_H
