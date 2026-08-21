#include <cstdint>
#include <csignal>
#include <limits>
#include <memory>
#include <optional>
#include <set>
#include <stdexcept>
#include <string>
#include <vector>

#include "catch.hpp"
#include "hotstuff/hotstuff.h"
#include "hotstuff/liveness.h"

namespace hotstuff
{

class ExperimentByzantineRuntimeIntegrationTestAccess final
{
public:
    using RetainedIdentityRollback =
        HotStuffBase::RetainedCommitEventIdentityRollback;

    struct CachedCommitIdentity
    {
        std::optional<ProposalKey> key;
        std::optional<std::uint64_t> generation;
        bool unavailable{false};
        bool conflicted{false};
        std::optional<ProposalKey> event_key;
        std::optional<std::uint64_t> event_generation;
        bool event_unavailable{false};
        bool event_conflicted{false};
    };

    static bool consume_direct_vote(
        HotStuffBase &runtime,
        const ProposalKey &key,
        const ProposalTreeSnapshot &tree)
    {
        return runtime.consume_experiment_outbound_direct_vote(key, tree);
    }

    static bool consume_aggregate(
        HotStuffBase &runtime,
        const ProposalKey &key,
        const ProposalTreeSnapshot &tree)
    {
        return runtime.consume_experiment_outbound_aggregate(key, tree);
    }

    static void seed_runtime_initialization(
        HotStuffBase &runtime,
        const ProposalKey &key)
    {
        runtime.adaptive_v2_durable_initialization_reports.insert_or_assign(
            key,
            HotStuffBase::AdaptiveV2DurableInitializationPhase::queued);
    }

    static void seed_view_generation(
        HotStuffBase &runtime,
        const ProposalKey &key,
        std::uint64_t generation = 1,
        ReplicaID authenticated_proposal_source_replica = 0)
    {
        runtime.proposal_view_generations.insert_or_assign(
            key, generation);
        runtime.authenticated_proposal_ingress.insert_or_assign(
            key,
            HotStuffBase::AuthenticatedProposalIngress{
                generation,
                authenticated_proposal_source_replica});
    }

    static bool retain_commit_event_identity(
        HotStuffBase &runtime,
        const ProposalKey &key,
        std::uint64_t generation)
    {
        return runtime.retain_commit_event_identity(key, generation);
    }

    static bool retain_commit_event_identity_through_epoch(
        HotStuffBase &runtime,
        const ProposalKey &key,
        std::uint64_t generation,
        std::uint32_t last_live_epoch)
    {
        return runtime.retain_commit_event_identity_through_epoch(
            key, generation, last_live_epoch);
    }

    static std::optional<std::uint32_t>
    retained_commit_event_identity_last_live_epoch(
        const HotStuffBase &runtime,
        const uint256_t &block_hash)
    {
        const auto retained =
            runtime.retained_commit_event_identities.find(block_hash);
        if (retained == runtime.retained_commit_event_identities.end())
            return std::nullopt;
        return retained->second.max_observed_epoch;
    }

    static bool retain_authenticated_proposal_commit_event_identities(
        HotStuffBase &runtime,
        const Proposal &proposal,
        std::uint64_t generation,
        RetainedIdentityRollback *rollback = nullptr,
        bool locally_constructed_certifier = false)
    {
        return runtime.retain_authenticated_proposal_commit_event_identities(
            proposal,
            generation,
            rollback,
            locally_constructed_certifier);
    }

    static void rollback_retained_commit_event_identity_mutations(
        HotStuffBase &runtime,
        const RetainedIdentityRollback &rollback)
    {
        runtime.rollback_retained_commit_event_identity_mutations(rollback);
    }

    static std::size_t retained_commit_event_identity_count(
        const HotStuffBase &runtime)
    {
        return runtime.retained_commit_event_identities.size();
    }

    static void replace_retained_commit_event_identity(
        HotStuffBase &runtime,
        const ProposalKey &key,
        std::uint64_t generation)
    {
        runtime.retained_commit_event_identities.insert_or_assign(
            key.block_hash,
            HotStuffBase::RetainedCommitEventIdentity{
                key,
                generation,
                key.configuration.epoch_number});
    }

    static bool retained_commit_event_identity_is_exact(
        const HotStuffBase &runtime,
        const ProposalKey &key,
        std::uint64_t generation)
    {
        const auto retained = runtime.retained_commit_event_identities.find(
            key.block_hash);
        return retained != runtime.retained_commit_event_identities.end() &&
               retained->second.key == key &&
               retained->second.view_generation == generation;
    }

    static bool retained_commit_event_identity_is_conflict(
        const HotStuffBase &runtime,
        const uint256_t &block_hash)
    {
        const auto retained = runtime.retained_commit_event_identities.find(
            block_hash);
        return retained != runtime.retained_commit_event_identities.end() &&
               !retained->second.key.has_value() &&
               !retained->second.view_generation.has_value();
    }

    static std::size_t rollback_owned_mutation_count(
        const RetainedIdentityRollback &rollback)
    {
        return rollback.owned_mutation_count;
    }

    static bool has_retained_commit_event_identity(
        const HotStuffBase &runtime,
        const uint256_t &block_hash)
    {
        return runtime.retained_commit_event_identities.find(block_hash) !=
               runtime.retained_commit_event_identities.end();
    }

    static bool has_adjacent_proposal_commit_event_bridge_heights(
        std::uint32_t alternate_height,
        std::uint32_t skipped_height,
        std::uint32_t certifier_height)
    {
        return HotStuffBase::
            has_adjacent_proposal_commit_event_bridge_heights(
                alternate_height,
                skipped_height,
                certifier_height);
    }

    static bool has_bounded_proposal_commit_event_bridge_intermediates(
        EpochProtocolMode mode,
        const ConfigurationId &alternate_configuration,
        const ConfigurationId &certifier_configuration,
        std::size_t intermediate_count)
    {
        return HotStuffBase::
            has_bounded_proposal_commit_event_bridge_intermediates(
                mode,
                alternate_configuration,
                certifier_configuration,
                intermediate_count);
    }

    static std::optional<std::pair<ConfigurationId, std::uint64_t>>
    authenticated_proposal_commit_event_bridge_configuration(
        EpochProtocolMode mode,
        const ConfigurationId &alternate_configuration,
        const ConfigurationId &certifier_configuration,
        std::uint64_t certifier_generation,
        std::optional<std::uint64_t> alternate_runtime_generation,
        std::optional<std::uint64_t> certifier_runtime_generation,
        std::optional<std::uint64_t> alternate_ingress_generation,
        std::optional<std::uint64_t> certifier_ingress_generation)
    {
        return HotStuffBase::
            authenticated_proposal_commit_event_bridge_configuration(
                mode,
                alternate_configuration,
                certifier_configuration,
                certifier_generation,
                alternate_runtime_generation,
                certifier_runtime_generation,
                alternate_ingress_generation,
                certifier_ingress_generation);
    }

    static void forget_retained_commit_event_identities_before_epoch(
        HotStuffBase &runtime,
        std::uint32_t first_live_epoch)
    {
        runtime.forget_retained_commit_event_identities_before_epoch(
            first_live_epoch);
    }

    static void cancel_all_exact_fallbacks(HotStuffBase &runtime)
    {
        runtime.cancel_all_exact_fallbacks();
    }

    static std::size_t maximum_retained_commit_event_identities()
    {
        return HotStuffBase::maximum_proposal_view_generation_observations;
    }

    static std::optional<std::uint64_t> exact_runtime_generation(
        const HotStuffBase &runtime,
        const ConfigurationId &configuration)
    {
        return runtime.find_exact_runtime_generation(configuration);
    }

    static void capture_local_proposal_before_admission(
        HotStuffBase &runtime,
        const Proposal &proposal)
    {
        runtime.on_local_proposal_constructed(proposal);
    }

    static void seed_view_generation_without_source(
        HotStuffBase &runtime,
        const ProposalKey &key,
        std::uint64_t generation = 1)
    {
        runtime.proposal_view_generations.insert_or_assign(
            key, generation);
    }

    static void seed_authenticated_proposal_ingress(
        HotStuffBase &runtime,
        const ProposalKey &key,
        std::uint64_t generation,
        ReplicaID authenticated_proposal_source_replica)
    {
        runtime.authenticated_proposal_ingress.insert_or_assign(
            key,
            HotStuffBase::AuthenticatedProposalIngress{
                generation,
                authenticated_proposal_source_replica});
    }

    static void seed_physical_parent_ingress(
        HotStuffBase &runtime,
        const ProposalKey &key,
        const ProposalTreeSnapshot &tree,
        std::uint64_t generation = 1)
    {
        if (!tree.parent.has_value())
            throw std::invalid_argument(
                "physical-parent ingress requires a non-root tree");
        seed_view_generation(runtime, key, generation, *tree.parent);
    }

    static void seed_unsuppressed_commit(
        HotStuffBase &runtime,
        const ProposalKey &key)
    {
        HotStuffBase::AdaptiveV2DurableCommitReportState state;
        state.phase =
            HotStuffBase::AdaptiveV2DurableCommitPhase::awaiting_evidence;
        runtime.adaptive_v2_durable_commit_reports.insert_or_assign(
            key, state);
    }

    static void seed_deferred_false_report(
        HotStuffBase &runtime,
        const ProposalKey &key)
    {
        runtime.experiment_false_timeout_states.insert_or_assign(
            key,
            HotStuffBase::ExperimentFalseTimeoutState{7, true, true});
    }

    static void forget_key(
        HotStuffBase &runtime,
        const ProposalKey &key)
    {
        runtime.forget_proposal_view_generation(key);
    }

    static void forget_block(
        HotStuffBase &runtime,
        const uint256_t &block_hash)
    {
        runtime.forget_proposal_view_generations_for_block(block_hash);
    }

    static void forget_before_epoch(
        HotStuffBase &runtime,
        std::uint32_t first_live_epoch)
    {
        runtime.forget_proposal_view_generations_before_epoch(
            first_live_epoch);
    }

    static bool has_runtime_initialization(
        const HotStuffBase &runtime,
        const ProposalKey &key)
    {
        return runtime.adaptive_v2_durable_initialization_reports.find(key) !=
               runtime.adaptive_v2_durable_initialization_reports.end();
    }

    static bool has_view_generation(
        const HotStuffBase &runtime,
        const ProposalKey &key)
    {
        return runtime.proposal_view_generations.find(key) !=
               runtime.proposal_view_generations.end();
    }

    static bool has_authenticated_proposal_ingress(
        const HotStuffBase &runtime,
        const ProposalKey &key)
    {
        return runtime.authenticated_proposal_ingress.find(key) !=
               runtime.authenticated_proposal_ingress.end();
    }

    static void purge_exact_runtime_state(
        HotStuffBase &runtime,
        const ProposalKey &key,
        bool preserve_scheduled_vote_fallback,
        bool preserve_response_evidence_until_deadline)
    {
        runtime.purge_pending_exact_contributions(
            key,
            preserve_scheduled_vote_fallback,
            preserve_response_evidence_until_deadline);
    }

    static std::size_t runtime_initialization_count(
        const HotStuffBase &runtime)
    {
        return runtime.adaptive_v2_durable_initialization_reports.size();
    }

    static std::size_t maximum_runtime_initializations()
    {
        return HotStuffBase::maximum_proposal_view_generation_observations;
    }

    static void report_runtime_initialized(
        HotStuffBase &runtime,
        const ProposalKey &key)
    {
        runtime.report_adaptive_v2_runtime_initialized(key);
    }

    static void suppress_lifecycle_reporting(HotStuffBase &runtime)
    {
        runtime.suppress_adaptive_v2_lifecycle_reporting("test");
    }

    static AdaptiveV2ReportingOutbox &reset_reporting_outbox(
        HotStuffBase &runtime,
        std::size_t maximum_pending_reports = 4)
    {
        AdaptiveV2ReportingOutboxConfig config;
        config.source_replica_id = 1;
        config.limits.maximum_pending_reports = maximum_pending_reports;
        config.limits.maximum_pending_payload_bytes = 64 * 1024;
        config.limits.maximum_delivery_attempts = 3;
        config.limits.initial_retry_backoff_ns = 1;
        config.limits.maximum_retry_backoff_ns = 8;
        runtime.adaptive_v2_reporting_outbox =
            std::make_unique<AdaptiveV2ReportingOutbox>(config);
        runtime.adaptive_v2_lifecycle_reporting_suppressed = false;
        runtime.adaptive_v2_convergence_evidence_healthy = true;
        runtime.epoch_manager_peer = PeerId(NetAddr(
            static_cast<std::uint32_t>(0x7f000001),
            static_cast<std::uint16_t>(19001)));
        return *runtime.adaptive_v2_reporting_outbox;
    }

    static const AdaptiveV2PendingReport *reporting_front(
        const HotStuffBase &runtime)
    {
        return runtime.adaptive_v2_reporting_outbox == nullptr
            ? nullptr
            : runtime.adaptive_v2_reporting_outbox->front();
    }

    static void poison_reporting(HotStuffBase &runtime)
    {
        runtime.poison_adaptive_v2_reporting("test_terminal_front");
    }

    static bool lifecycle_reporting_suppressed(
        const HotStuffBase &runtime)
    {
        return runtime.adaptive_v2_lifecycle_reporting_suppressed;
    }

    static bool convergence_evidence_healthy(
        const HotStuffBase &runtime)
    {
        return runtime.adaptive_v2_convergence_evidence_healthy;
    }

    static void seed_pending_commit(
        HotStuffBase &runtime,
        const uint256_t &block_hash,
        std::optional<ProposalKey> committed_key)
    {
        runtime.pending_adaptive_v2_commit =
            HotStuffBase::PendingAdaptiveV2Commit{
                block_hash,
                std::move(committed_key),
                std::uint64_t{1},
                HotStuffBase::CommittedProposalIdentityDisposition::exact};
    }

    static AdaptiveV2EpochChangeIdentity seed_convergence_identity(
        HotStuffBase &runtime,
        const ConfigurationId &predecessor,
        std::uint32_t successor_epoch_number = 1,
        std::uint64_t command_block_height = 41,
        std::uint64_t activation_delay_blocks = 5,
        std::optional<uint256_t> command_block_hash = std::nullopt)
    {
        const AdaptiveV2EpochChangeIdentity identity{
            predecessor.epoch_number,
            predecessor.epoch_digest,
            successor_epoch_number,
            DataStream("pending-convergence-successor").get_hash(),
            DataStream("pending-convergence-payload").get_hash(),
            command_block_height,
            command_block_hash.value_or(
                DataStream("pending-convergence-command").get_hash()),
            activation_delay_blocks,
            command_block_height + activation_delay_blocks};
        runtime.adaptive_v2_committed_convergence_identity = identity;
        return identity;
    }

    static bool has_convergence_identity(
        const HotStuffBase &runtime,
        const AdaptiveV2EpochChangeIdentity &identity)
    {
        return runtime.adaptive_v2_committed_convergence_identity == identity;
    }

    static bool has_any_convergence_identity(const HotStuffBase &runtime)
    {
        return runtime.adaptive_v2_committed_convergence_identity.has_value();
    }

    static void seed_pending_epoch_command(
        HotStuffBase &runtime,
        const uint256_t &block_hash)
    {
        runtime.pending_committed_epoch_change.emplace(
            HotStuffBase::PendingCommittedEpochChange{
                block_hash, AuthorizedEpochChange{}, uint256_t{}});
    }

    static void poison_convergence_evidence(HotStuffBase &runtime)
    {
        runtime.mark_adaptive_v2_convergence_evidence_unhealthy(
            "test_pre_poisoned_convergence");
    }

    static void enqueue_matching_activation(
        HotStuffBase &runtime,
        const AdaptiveV2EpochChangeIdentity &identity)
    {
        REQUIRE(runtime.adaptive_v2_committed_convergence_identity == identity);
        runtime.adaptive_v2_activation_observation_pending = true;
        runtime.enqueue_pending_adaptive_v2_activation_observation();
    }

    static void report_committed(
        HotStuffBase &runtime,
        std::optional<ProposalKey> committed_key,
        bool initialization_predecessor_required = true)
    {
        runtime.report_adaptive_v2_committed(
            committed_key, initialization_predecessor_required);
    }

    static void retry_ready_commits(HotStuffBase &runtime)
    {
        runtime.retry_ready_adaptive_v2_commit_reports();
    }

    static bool has_durable_commit(
        const HotStuffBase &runtime,
        const ProposalKey &key)
    {
        return runtime.has_durable_adaptive_v2_commit_report(key);
    }

    static EvidenceTransportResult enqueue_evidence(
        HotStuffBase &runtime,
        const EvidenceReportEnvelope &report)
    {
        return runtime.enqueue_adaptive_v2_evidence_report(report);
    }

    static bool contains_admitted(
        const HotStuffBase &runtime,
        const ProposalKey &key)
    {
        return runtime.proposal_admission != nullptr &&
               runtime.proposal_admission->contains_admitted(key);
    }

    static ProposalContextStatus context_status(
        const HotStuffBase &runtime,
        const ProposalKey &key)
    {
        return runtime.proposal_contexts->context_status(key);
    }

    static void reject_response_deadline_scheduling(
        HotStuffBase &runtime)
    {
        if (runtime.adaptive_v2_response_evidence == nullptr)
            throw std::invalid_argument(
                "adaptive-v2 response evidence is unavailable");
        runtime.adaptive_v2_response_evidence->bind_deadline_scheduler(
            [](const ProposalKey &,
               std::uint64_t,
               EvidenceDeadlineCallback,
               EvidenceDeadlineFailureCallback) {
                return EvidenceDeadlineCancellation{};
            });
    }

    static void remove_response_evidence_bridge(
        HotStuffBase &runtime)
    {
        runtime.adaptive_v2_response_evidence.reset();
    }

    static bool admit_exact_context(
        HotStuffBase &runtime,
        const ProposalKey &key)
    {
        const auto metadata = runtime.exact_context_metadata(key);
        return metadata.has_value() &&
               runtime.proposal_contexts->admit_remote(*metadata)
                   .has_value();
    }

    static bool admit_context_with_tree(
        HotStuffBase &runtime,
        const ProposalKey &key,
        ProposalTreeSnapshot tree)
    {
        return runtime.proposal_contexts
            ->admit_remote(ProposalContextMetadata{
                key, std::move(tree), 5})
            .has_value();
    }

    static bool seed_verified_aggregate(
        HotStuffBase &runtime,
        const ProposalKey &key,
        ReplicaID authenticated_child,
        const QuorumCert &certificate)
    {
        const auto lease = runtime.proposal_contexts->acquire_open_context(key);
        return lease.has_value() &&
               runtime.proposal_contexts
                   ->record_verified_aggregate_certificate(
                       *lease, authenticated_child, certificate);
    }

    static bool initialize_accumulator(
        HotStuffBase &runtime,
        const ProposalKey &key)
    {
        const auto lease = runtime.proposal_contexts->acquire_open_context(key);
        return lease.has_value() &&
               runtime.proposal_contexts->initialize_accumulator(
                   *lease, runtime.create_quorum_cert(key));
    }

    static std::optional<ProposalContextSnapshot> context_snapshot(
        const HotStuffBase &runtime,
        const ProposalKey &key)
    {
        return runtime.proposal_contexts->snapshot(key);
    }

    static bool arm_response_attempt(
        HotStuffBase &runtime,
        const ProposalKey &key,
        const ProposalTreeSnapshot &tree,
        std::uint64_t start_ns,
        std::uint64_t duration_us)
    {
        return runtime.adaptive_v2_response_evidence != nullptr &&
               runtime.adaptive_v2_response_evidence->arm(
                   key, tree, start_ns, duration_us);
    }

    static ProposalTreeSnapshot context_tree(
        HotStuffBase &runtime,
        const ProposalKey &key)
    {
        const auto lease = runtime.proposal_contexts->acquire_open_context(key);
        if (!lease.has_value())
            throw std::invalid_argument("exact context is unavailable");
        return lease->tree();
    }

    static std::size_t record_response_timeout(
        HotStuffBase &runtime,
        const ProposalKey &key,
        ReplicaID child,
        std::uint64_t timeout_ns)
    {
        return runtime.adaptive_v2_response_evidence == nullptr
            ? 0
            : runtime.adaptive_v2_response_evidence->record_timeouts(
                  key, {child}, timeout_ns);
    }

    static void continue_verified_aggregate(
        HotStuffBase &runtime,
        const ProposalKey &key,
        const VoteRelay &relay,
        ReplicaID authenticated_child,
        std::uint64_t received_ns)
    {
        const auto lease = runtime.proposal_contexts->acquire_open_context(key);
        if (!lease.has_value())
            throw std::invalid_argument("exact context is unavailable");
        runtime.continue_exact_contribution(
            *lease,
            ExactContributionKind::aggregate_relay,
            make_exact_relay_envelope(relay, authenticated_child),
            received_ns);
    }

    static quorum_cert_bt aggregate_certificate(
        HotStuffBase &runtime,
        const ProposalKey &key,
        const std::set<ReplicaID> &signers)
    {
        auto certificate = runtime.create_quorum_cert(key);
        for (const auto signer : signers)
        {
            PrivKeyDummy private_key;
            PartCertDummy part(private_key, key);
            certificate->add_part(runtime.config, signer, part);
        }
        return certificate;
    }

    static void close_exact_context(
        HotStuffBase &runtime,
        const ProposalKey &key)
    {
        runtime.proposal_contexts->close(
            key, ProposalContextEvent::proposal_aborted);
    }

    static bool start_response_attempt_arm(
        HotStuffBase &runtime,
        const ProposalKey &key)
    {
        return runtime.start_latency_deadline(key);
    }

    static void start_aggregation_timer(
        HotStuffBase &runtime,
        const ProposalKey &key)
    {
        runtime.start_aggregation_timer(key);
    }

    static void remove_aggregation_timeout_coordinator(
        HotStuffBase &runtime)
    {
        runtime.aggregation_timeout_coordinator.reset();
    }

    static void attempt_evidence_before_exposure(
        HotStuffBase &runtime,
        const ProposalKey &key)
    {
        runtime.attempt_proposal_evidence_before_exposure(
            key, "test_response_attempt_arm_failed");
    }

    static void ensure_finalized_evidence_before_exposure(
        HotStuffBase &runtime,
        const ProposalKey &key)
    {
        runtime.ensure_finalized_proposal_evidence_before_exposure(
            key, "test_finalized_response_attempt_arm_failed");
    }

    static bool has_successful_response_attempt_arm(
        const HotStuffBase &runtime,
        const ProposalKey &key)
    {
        return runtime.has_successful_response_attempt_arm(key);
    }

    static bool has_response_attempt_arm_failure(
        const HotStuffBase &runtime,
        const ProposalKey &key)
    {
        return runtime.response_attempt_arm_failure_markers.count(key) != 0;
    }

    static std::size_t response_attempt_arm_failure_count(
        const HotStuffBase &runtime)
    {
        return runtime.response_attempt_arm_failure_markers.size();
    }

    static void replace_aggregation_scheduler(
        HotStuffBase &runtime,
        std::unique_ptr<AggregationScheduler> scheduler)
    {
        runtime.aggregation_scheduler = std::move(scheduler);
    }

    static ProposalAdmissionResult receive_delayed_proposal(
        HotStuffBase &runtime,
        const ConfigurationId &configuration,
        ReplicaID proposer,
        const block_t &block,
        std::uint64_t view_generation = 1)
    {
        if (runtime.proposal_admission == nullptr || block == nullptr)
            throw std::invalid_argument(
                "delayed proposal admission is unavailable");
        Proposal proposal(
            proposer,
            configuration.epoch_number,
            configuration.tree_id,
            configuration.epoch_digest,
            block,
            &runtime);
        MsgPropose native(proposal);
        const auto body = static_cast<bytearray_t>(native.serialized);
        BufferedProposal buffered{
            proposal.metadata(),
            body,
            runtime.config.get_peer_id(proposer),
            view_generation,
            DataStream(body).get_hash(),
            body,
            proposer};
        return runtime.proposal_admission->receive(std::move(buffered));
    }

    static void consensus_with_direct_certifier(
        HotStuffBase &runtime,
        const block_t &block,
        const quorum_cert_bt &certifier)
    {
        runtime.do_consensus_with_identity_provenance(
            block,
            certifier,
            HotStuffBase::CommittedProposalIdentityProvenance::
                verified_direct_certifier);
    }

    static void resolve_fetched_block(
        HotStuffBase &runtime,
        const block_t &block)
    {
        const auto fetched = runtime.storage->add_blk(block);
        runtime.on_fetch_blk(fetched);
    }

    static bool is_block_delivered(
        const HotStuffBase &runtime,
        const uint256_t &block_hash)
    {
        return runtime.storage->is_blk_delivered(block_hash);
    }

    static ConfigurationId initialize_active_runtime(
        HotStuffBase &runtime)
    {
        runtime.set_fanout(2);
        runtime.set_piped_latency(2, 2);
        runtime.set_tree_generation("default", "");
        runtime.set_tree_period(8);

        std::vector<std::tuple<NetAddr, pubkey_bt, uint256_t>> replicas;
        replicas.reserve(4);
        for (ReplicaID replica = 0; replica < 4; ++replica)
        {
            const auto address = replica == runtime.get_id()
                ? runtime.listen_addr
                : NetAddr(
                    static_cast<std::uint32_t>(0x7f000001),
                    static_cast<std::uint16_t>(18000 + replica));
            PrivKeyDummy private_key;
            replicas.emplace_back(
                address,
                private_key.get_pubkey(),
                DataStream(
                    "indirect-commit-tls-" + std::to_string(replica))
                    .get_hash());
        }

        runtime.tree_scheduler(std::move(replicas), true);
        runtime.on_init(1);
        runtime.get_pace_maker()->init(&runtime);
        runtime.get_pace_maker()->update_tree_proposer();
        runtime.activate_initial_leader_view();
        runtime.initialize_adaptive_epoch_runtime();
        return runtime.exact_configuration(0, 0);
    }

    static quorum_cert_bt direct_certifier(
        HotStuffBase &runtime,
        const ProposalKey &key,
        std::size_t signer_count = 3)
    {
        auto certificate = runtime.create_quorum_cert(key);
        for (std::size_t signer = 0; signer < signer_count; ++signer)
        {
            const auto replica = static_cast<ReplicaID>(signer);
            PrivKeyDummy private_key;
            PartCertDummy part(private_key, key);
            certificate->add_part(runtime.config, replica, part);
        }
        return certificate;
    }

    static quorum_cert_bt genesis_parent_certificate(
        HotStuffBase &runtime)
    {
        return runtime.create_quorum_cert(genesis_certification_key(
            runtime.get_genesis()->get_hash()));
    }

    static quorum_cert_bt invalid_direct_certifier(
        const ProposalKey &key)
    {
        DataStream encoded;
        encoded << static_cast<std::uint32_t>(2);
        serialize_proposal_key(encoded, key);
        encoded << DataStream("invalid-certificate-authentication").get_hash();
        encoded << static_cast<std::size_t>(3);
        encoded << htole(static_cast<std::uint32_t>(3));
        for (ReplicaID signer = 0; signer < 3; ++signer)
            encoded << signer;
        quorum_cert_bt certificate = new QuorumCertDummy();
        certificate->unserialize(encoded);
        return certificate;
    }

    static void replace_commit_rule_certificate(
        const block_t &block,
        const quorum_cert_bt &replacement)
    {
        if (block == nullptr || block->get_qc() == nullptr ||
            replacement == nullptr)
            throw std::invalid_argument(
                "commit-rule certificate replacement is incomplete");
        DataStream encoded;
        encoded << *replacement;
        block->get_qc()->unserialize(encoded);
    }

    static std::optional<ProposalKey> resolve_committed_key(
        const HotStuffBase &runtime,
        const block_t &block,
        const std::vector<ProposalKey> &closed_context_keys,
        const quorum_cert_bt &verified_direct_certifier)
    {
        return runtime.committed_proposal_key(
            block,
            closed_context_keys,
            verified_direct_certifier);
    }

    static CachedCommitIdentity resolve_and_cache_commit(
        HotStuffBase &runtime,
        const block_t &block,
        const std::vector<ProposalKey> &closed_context_keys,
        const quorum_cert_bt &verified_direct_certifier,
        bool legal_qc_skipped_ancestor = false)
    {
        const auto provenance = verified_direct_certifier != nullptr
            ? HotStuffBase::CommittedProposalIdentityProvenance::
                  verified_direct_certifier
            : legal_qc_skipped_ancestor
                ? HotStuffBase::CommittedProposalIdentityProvenance::
                      legal_qc_skipped_ancestor
                : HotStuffBase::CommittedProposalIdentityProvenance::
                      compatibility_unknown;
        const auto resolution =
            runtime.resolve_committed_proposal_identity(
                block,
                closed_context_keys,
                verified_direct_certifier,
                provenance);
        const auto key = resolution.key;
        runtime.cache_adaptive_v2_commit(
            block,
            resolution,
            verified_direct_certifier != nullptr);
        if (!runtime.pending_adaptive_v2_commit.has_value())
            return {};
        return CachedCommitIdentity{
            runtime.pending_adaptive_v2_commit->committed_key,
            runtime.pending_adaptive_v2_commit->view_generation,
            runtime.pending_adaptive_v2_commit->identity_disposition ==
                HotStuffBase::CommittedProposalIdentityDisposition::
                    unavailable,
            runtime.pending_adaptive_v2_commit->identity_disposition ==
                HotStuffBase::CommittedProposalIdentityDisposition::
                    conflicting,
            runtime.pending_adaptive_v2_commit->event_committed_key,
            runtime.pending_adaptive_v2_commit->event_view_generation,
            runtime.pending_adaptive_v2_commit->event_identity_disposition ==
                HotStuffBase::CommittedProposalIdentityDisposition::
                    unavailable,
            runtime.pending_adaptive_v2_commit->event_identity_disposition ==
                HotStuffBase::CommittedProposalIdentityDisposition::
                    conflicting};
    }

    static CachedCommitIdentity resolve_and_cache_unproven_commit(
        HotStuffBase &runtime,
        const block_t &block)
    {
        const auto resolution = runtime.resolve_committed_proposal_identity(
            block,
            {},
            nullptr,
            HotStuffBase::CommittedProposalIdentityProvenance::core_unproven);
        runtime.cache_adaptive_v2_commit(block, resolution, false);
        if (!runtime.pending_adaptive_v2_commit.has_value())
            return {};
        return CachedCommitIdentity{
            runtime.pending_adaptive_v2_commit->committed_key,
            runtime.pending_adaptive_v2_commit->view_generation,
            runtime.pending_adaptive_v2_commit->identity_disposition ==
                HotStuffBase::CommittedProposalIdentityDisposition::
                    unavailable,
            runtime.pending_adaptive_v2_commit->identity_disposition ==
                HotStuffBase::CommittedProposalIdentityDisposition::
                    conflicting,
            runtime.pending_adaptive_v2_commit->event_committed_key,
            runtime.pending_adaptive_v2_commit->event_view_generation,
            runtime.pending_adaptive_v2_commit->event_identity_disposition ==
                HotStuffBase::CommittedProposalIdentityDisposition::
                    unavailable,
            runtime.pending_adaptive_v2_commit->event_identity_disposition ==
                HotStuffBase::CommittedProposalIdentityDisposition::
                    conflicting};
    }

    static void report_and_post_commit(
        HotStuffBase &runtime,
        const block_t &block,
        std::uint64_t commit_batch_index = 0)
    {
        runtime.report_adaptive_v2_committed(
            runtime.pending_adaptive_v2_commit.has_value()
                ? runtime.pending_adaptive_v2_commit->committed_key
                : std::nullopt);
        runtime.do_post_block_commit(block, commit_batch_index);
    }

    static void verified_consensus_and_post(
        HotStuffBase &runtime,
        const block_t &block,
        const quorum_cert_bt &verified_direct_certifier,
        std::uint64_t commit_batch_index = 0)
    {
        runtime.do_consensus(block, verified_direct_certifier);
        runtime.do_post_block_commit(block, commit_batch_index);
    }

    static block_t add_custom_commit_rule_block(
        HotStuffBase &runtime,
        const block_t &parent,
        const block_t &qc_reference,
        quorum_cert_bt certificate,
        const std::string &label,
        std::uint32_t height,
        std::size_t transaction_count = 1,
        int8_t decision = 0)
    {
        if (parent == nullptr || qc_reference == nullptr ||
            certificate == nullptr || transaction_count == 0)
            throw std::invalid_argument(
                "custom commit-rule block is incomplete");
        std::vector<uint256_t> commands;
        commands.reserve(transaction_count);
        for (std::size_t index = 0; index < transaction_count; ++index)
            commands.push_back(DataStream(
                label + "-" + std::to_string(index)).get_hash());
        block_t block = new Block(
            std::vector<block_t>{parent},
            std::move(commands),
            std::move(certificate),
            bytearray_t{},
            height,
            qc_reference,
            nullptr,
            decision);
        runtime.storage->add_blk(block);
        if (!runtime.HotStuffCore::on_deliver_blk(block))
            throw std::runtime_error(
                "custom commit-rule block delivery failed");
        return block;
    }

    static block_t add_commit_rule_block(
        HotStuffBase &runtime,
        const ConfigurationId &configuration,
        const block_t &parent,
        const block_t &qc_reference,
        const std::string &label,
        std::size_t transaction_count = 1,
        std::size_t signer_count = 3,
        int8_t decision = 0)
    {
        if (parent == nullptr || qc_reference == nullptr)
            throw std::invalid_argument(
                "commit-rule block requires parent and QC reference");
        auto certificate = qc_reference == runtime.get_genesis()
            ? genesis_parent_certificate(runtime)
            : direct_certifier(
                  runtime,
                  ProposalKey{configuration, qc_reference->get_hash()},
                  signer_count);
        return add_custom_commit_rule_block(
            runtime,
            parent,
            qc_reference,
            std::move(certificate),
            label,
            parent->get_height() + 1,
            transaction_count,
            decision);
    }

    static void apply_update(HotStuffBase &runtime, const block_t &block)
    {
        runtime.update(block);
    }

    static void compatibility_consensus_and_post(
        HotStuffBase &runtime,
        const block_t &block,
        std::uint64_t commit_batch_index = 0)
    {
        runtime.do_consensus(block);
        runtime.do_post_block_commit(block, commit_batch_index);
    }

    static std::optional<std::uint64_t> view_generation(
        const HotStuffBase &runtime,
        const ProposalKey &key)
    {
        return runtime.proposal_view_generation(key);
    }

    static bool observe_view_generation(
        HotStuffBase &runtime,
        const ProposalKey &key,
        std::uint64_t generation)
    {
        return runtime.observe_proposal_view_generation(key, generation);
    }

    static EpochRotationResult rotate_to_tree(
        HotStuffBase &runtime,
        std::uint32_t tree_id)
    {
        if (runtime.epoch_live_binding == nullptr)
            return {EpochIngressError::state_rejected, std::nullopt};
        return runtime.epoch_live_binding->rotate_to_tree(tree_id);
    }

    static void seed_adaptive_v3_observation_retry(
        HotStuffBase &runtime,
        const AdaptiveV3ActivationReadyObservation &observation,
        const bytearray_t &canonical_payload,
        std::size_t attempts)
    {
        runtime.adaptive_v3_signed_observation = observation;
        runtime.adaptive_v3_pending_observation = canonical_payload;
        runtime.adaptive_v3_observation_attempts = attempts;
        runtime.adaptive_v3_observation_retry_exhausted = false;
        runtime.adaptive_v3_observation_terminal = false;
    }

    static void transmit_adaptive_v3_observation(HotStuffBase &runtime)
    {
        runtime.transmit_adaptive_v3_observation();
    }

    static bool adaptive_v3_observation_retry_exhausted(
        const HotStuffBase &runtime)
    {
        return runtime.adaptive_v3_observation_retry_exhausted;
    }

    static bool adaptive_v3_observation_terminal(
        const HotStuffBase &runtime)
    {
        return runtime.adaptive_v3_observation_terminal;
    }

    static bool has_adaptive_v3_pending_observation(
        const HotStuffBase &runtime)
    {
        return runtime.adaptive_v3_pending_observation.has_value() &&
            runtime.adaptive_v3_signed_observation.has_value();
    }

};

} // namespace hotstuff

namespace
{

using namespace hotstuff;

class TestHotStuff final : public HotStuffNoSig
{
public:
    using HotStuffNoSig::HotStuffNoSig;

protected:
    void state_machine_execute(const Finality &) override {}
};

class RelayRecordingHotStuff final : public HotStuffNoSig
{
public:
    using HotStuffNoSig::HotStuffNoSig;

    std::size_t relay_count() const noexcept { return relay_count_; }

protected:
    void state_machine_execute(const Finality &) override {}

private:
    void relay_once(const BufferedProposal &) override { ++relay_count_; }

    std::size_t relay_count_{0};
};

class ActiveRuntimePaceMaker final : public PaceMakerDummy
{
public:
    explicit ActiveRuntimePaceMaker(int32_t parent_limit)
        : PaceMakerDummy(parent_limit) {}

    size_t get_current_tid() override { return 0; }
    size_t get_current_epoch() override { return 0; }
};

class InconsistentAggregateCertificate final
    : public QuorumCertDummy
{
public:
    InconsistentAggregateCertificate(
        const ReplicaConfig &config,
        const ProposalKey &key,
        std::vector<ReplicaID> enumerated_signers,
        std::size_t reported_count)
        : QuorumCertDummy(config, key),
          enumerated_signers_(std::move(enumerated_signers)),
          reported_count_(reported_count)
    {}

    std::vector<ReplicaID> get_signers() const override
    {
        return enumerated_signers_;
    }

    std::size_t get_sigs_n() override
    {
        return reported_count_;
    }

    InconsistentAggregateCertificate *clone() override
    {
        return new InconsistentAggregateCertificate(*this);
    }

private:
    std::vector<ReplicaID> enumerated_signers_;
    std::size_t reported_count_{0};
};

class ThrowingAggregationScheduler final : public AggregationScheduler
{
public:
    Duration monotonic_now() const noexcept override
    {
        return Duration::zero();
    }

    Cancellation schedule_after(Duration, Callback) override
    {
        ++attempts;
        throw std::runtime_error("test aggregation scheduler failure");
    }

    std::size_t attempts{0};
};

class RecordingOpportunityAuditEmitter final
    : public AuditStructuredEventEmitter
{
public:
    explicit RecordingOpportunityAuditEmitter(
        std::vector<std::string> *order = nullptr)
        : order_(order)
    {}

    void emit_audit(
        const AuditStructuredEventPayload &payload) noexcept override
    {
        try
        {
            const auto *event = std::get_if<
                FaultContributionOpportunityStructuredEvent>(&payload);
            if (event == nullptr)
                return;
            events.push_back(*event);
            if (order_ != nullptr)
                order_->push_back("opportunity");
        }
        catch (...)
        {
        }
    }

    std::vector<FaultContributionOpportunityStructuredEvent> events;

private:
    std::vector<std::string> *order_{nullptr};
};

class RecordingProtocolEmitter final : public StructuredEventEmitter
{
public:
    explicit RecordingProtocolEmitter(bool designated = true)
        : designated_(designated)
    {}

    bool is_designated_commit_observer() const noexcept override
    {
        return designated_;
    }
    void emit(const StructuredEventPayload &payload) noexcept override
    {
        try
        {
            events.push_back(payload);
        }
        catch (...)
        {
        }
    }

    std::vector<StructuredEventPayload> events;

private:
    bool designated_{true};
};

class RecordingAdaptiveEmitter final : public AdaptiveStructuredEventEmitter
{
public:
    void emit_adaptive(
        const AdaptiveAggregationStructuredEvent &event) noexcept override
    {
        try
        {
            events.push_back(event);
        }
        catch (...)
        {
        }
    }

    std::vector<AdaptiveAggregationStructuredEvent> events;
};

class RecordingReadinessAuditEmitter final
    : public AuditStructuredEventEmitter
{
public:
    void emit_audit(
        const AuditStructuredEventPayload &payload) noexcept override
    {
        try
        {
            const auto *event = std::get_if<
                AdaptiveV3ReadinessStructuredEvent>(&payload);
            if (event != nullptr)
                events.push_back(*event);
        }
        catch (...)
        {}
    }

    std::vector<AdaptiveV3ReadinessStructuredEvent> events;
};

class ScopedSigpipeIgnore final
{
public:
    ScopedSigpipeIgnore()
        : previous_(std::signal(SIGPIPE, SIG_IGN))
    {}
    ~ScopedSigpipeIgnore() { std::signal(SIGPIPE, previous_); }

private:
    using Handler = void (*)(int);
    Handler previous_{SIG_DFL};
};

uint256_t digest(const std::string &label)
{
    return DataStream(label).get_hash();
}

AdaptiveV3RuntimeConfig adaptive_v3_runtime_config(ReplicaID local_replica)
{
    AdaptiveV3RuntimeConfig config;
    const auto manager_address = NetAddr("127.0.0.1:19001");
    config.manager_peer = PeerId(manager_address);
    config.manager_address = manager_address;
    for (ReplicaID replica = 0; replica < 4; ++replica)
    {
        auto key = std::make_shared<PrivKeyBLS>();
        key->from_rand();
        config.readiness_membership.push_back(
            AdaptiveV3ReadinessMember{replica, PubKeyBLS(*key)});
        if (replica == local_replica)
            config.local_readiness_private_key = std::move(key);
    }
    const bytearray_t issuer_secret(32, 1);
    const PrivKeySecp256k1 issuer_key(issuer_secret);
    config.epoch_change_issuer =
        EpochChangeIssuer{17, PubKeySecp256k1(issuer_key)};
    config.epoch_change_delay_bounds = EpochChangeDelayBounds{1, 20};
    config.readiness_wire_limits.maximum_members = 4;
    config.readiness_wire_limits.maximum_payload_bytes = 4U * 1024U * 1024U;
    config.maximum_block_extra_bytes = 4U * 1024U * 1024U;
    config.maximum_ancestry_blocks = 64;
    config.maximum_bundle_bytes = 4U * 1024U * 1024U;
    config.maximum_observation_attempts = 3;
    config.observation_retry_interval_ms = 1;
    return config;
}

ExperimentByzantineOptions rotating_options(
    ReplicaID local_replica,
    std::vector<ExperimentOmissionMarker> *markers = nullptr)
{
    ExperimentByzantineOptions options;
    options.enabled = true;
    options.diagnostic_window = "native-runtime-window";
    options.rotating_omission = ExperimentRotatingOmissionOptions{
        "rotating_intermittent_omission_v1",
        local_replica,
        7,
        {1, 3},
        2,
        1,
        std::numeric_limits<std::uint64_t>::max(),
        1,
        100'000};
    if (markers != nullptr)
    {
        options.omission_marker_emitter =
            [markers](const ExperimentOmissionMarker &marker)
            { markers->push_back(marker); };
    }
    return options;
}

ExperimentByzantineOptions persistent_options(
    ReplicaID local_replica,
    std::vector<ExperimentOmissionMarker> *markers = nullptr)
{
    auto options = rotating_options(local_replica, markers);
    options.rotating_omission->mode =
        "persistent_selected_omission_v1";
    options.rotating_omission->max_omissions_per_proposal =
        options.rotating_omission->actor_ids.size();
    return options;
}

ExperimentByzantineOptions tiered_options(
    ReplicaID local_replica,
    std::size_t responsive_period = 32,
    std::vector<ExperimentOmissionMarker> *markers = nullptr)
{
    auto options = rotating_options(local_replica, markers);
    options.rotating_omission->mode =
        "tiered_persistent_responsive_omission_v1";
    options.rotating_omission->replica_count = 31;
    options.rotating_omission->actor_ids = {1};
    options.rotating_omission->expected_actor_count = 1;
    options.rotating_omission->responsive_degraded_actor_ids = {2};
    options.rotating_omission->responsive_omission_period = responsive_period;
    options.rotating_omission->max_omissions_per_proposal = 2;
    return options;
}

ExperimentByzantineOptions tiered_v2_options(
    ReplicaID local_replica,
    std::size_t responsive_period = 41,
    std::vector<ExperimentOmissionMarker> *markers = nullptr)
{
    auto options = tiered_options(
        local_replica, responsive_period, markers);
    options.rotating_omission->mode =
        "tiered_persistent_responsive_omission_v2";
    return options;
}

ProposalKey selected_proposal(
    ReplicaID actor,
    const std::string &label)
{
    ExperimentByzantineAdapter selector(rotating_options(1));
    const ConfigurationId configuration{7, 3, digest("native-epoch")};
    for (std::uint32_t index = 0; index < 10'000; ++index)
    {
        const ProposalKey candidate{
            configuration,
            digest(label + "-" + std::to_string(index))};
        if (selector.rotating_omission_actor(candidate) ==
            std::optional<ReplicaID>{actor})
        {
            return candidate;
        }
    }
    throw std::runtime_error("could not derive selected proposal");
}

ProposalTreeSnapshot tree(
    ExperimentReplicaRole role,
    ReplicaID local_replica = 1)
{
    ProposalTreeSnapshot snapshot;
    snapshot.local_replica = local_replica;
    snapshot.root = role == ExperimentReplicaRole::root
                        ? local_replica
                        : ReplicaID{0};
    if (role != ExperimentReplicaRole::root)
        snapshot.parent = 0;
    if (role == ExperimentReplicaRole::internal)
        snapshot.direct_children = {2, 4};
    snapshot.fanout = 2;
    snapshot.pipeline_stretch = 2;
    return snapshot;
}

ProposalKey proposal_key(
    std::uint32_t epoch,
    const std::string &configuration_label,
    const std::string &block_label)
{
    return ProposalKey{
        ConfigurationId{
            epoch, 0, digest(configuration_label + "-configuration")},
        digest(block_label)};
}

EvidenceReportEnvelope evidence_report(
    const ProposalKey &key,
    ReplicaID observed_replica = 2,
    std::uint64_t initial_reporter_sequence = 0)
{
    EvidenceReporterConfig config;
    config.trusted_reporter_id = 1;
    config.initial_reporter_sequence = initial_reporter_sequence;
    config.initial_reporter_monotonic_ns =
        initial_reporter_sequence == 0 ? 0 : 999;
    EvidenceReporter reporter(config);
    ResponseAttemptFact fact;
    fact.key = ResponseAttemptKey{
        key, observed_replica, ExpectedMessageType::direct_vote};
    fact.outcome = ResponseOutcome::on_time;
    fact.response_duration_us = 50;
    fact.deadline_duration_us = 100;
    fact.fact_monotonic_ns = 1'000;
    fact.signer_set = {observed_replica};
    REQUIRE(reporter.enqueue(fact));
    REQUIRE(reporter.front() != nullptr);
    return reporter.front()->envelope;
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

void acknowledge_and_release_convergence(
    AdaptiveV2ReportingOutbox &outbox,
    std::uint64_t now)
{
    const auto attempt = outbox.begin_delivery(now);
    REQUIRE(attempt.status == AdaptiveV2ReportingAttemptStatus::started);
    REQUIRE(attempt.token.has_value());
    REQUIRE(attempt.report != nullptr);
    const auto report_id = attempt.report->report_id;
    CHECK(outbox.acknowledge_delivery(
              *attempt.token,
              AdaptiveV2ReportingDeliveryResult::delivered,
              now) ==
          AdaptiveV2ReportingTransitionStatus::retry_scheduled);
    REQUIRE(outbox.front() != nullptr);
    REQUIRE(outbox.front()->convergence_observation_kind.has_value());
    REQUIRE(outbox.front()->convergence_identity.has_value());
    AdaptiveV2ConvergenceObservationAck acknowledgement;
    acknowledgement.target_replica_id = 1;
    acknowledgement.observation_kind =
        *outbox.front()->convergence_observation_kind;
    acknowledgement.identity = *outbox.front()->convergence_identity;
    acknowledgement.observation_digest =
        outbox.front()->convergence_observation_digest;
    acknowledgement.disposition = AdaptiveV2ConvergenceAckDisposition::positive;
    CHECK(outbox.acknowledge_convergence_observation(acknowledgement) ==
          AdaptiveV2ReportingTransitionStatus::delivered);
    CHECK(outbox.release_terminal(report_id) ==
          AdaptiveV2ReportingReleaseStatus::released);
}

struct DelayedProposalBlocks
{
    block_t missing_parent;
    block_t proposal;
};

DelayedProposalBlocks delayed_proposal_blocks(
    HotStuffBase &runtime,
    const std::string &label)
{
    const auto genesis = runtime.get_genesis();
    block_t missing_parent = new Block(
        std::vector<block_t>{genesis},
        std::vector<uint256_t>{digest(label + "-parent-command")},
        ExperimentByzantineRuntimeIntegrationTestAccess::
            genesis_parent_certificate(runtime),
        bytearray_t{},
        1,
        genesis,
        nullptr);
    block_t proposal = new Block(
        std::vector<block_t>{missing_parent},
        std::vector<uint256_t>{digest(label + "-proposal-command")},
        ExperimentByzantineRuntimeIntegrationTestAccess::
            genesis_parent_certificate(runtime),
        bytearray_t{},
        2,
        genesis,
        nullptr);
    return {missing_parent, proposal};
}

block_t indirect_commit_block(
    TestHotStuff &runtime,
    const std::string &label)
{
    return new Block(
        std::vector<block_t>{runtime.get_genesis()},
        std::vector<uint256_t>{digest(label)},
        ExperimentByzantineRuntimeIntegrationTestAccess::
            genesis_parent_certificate(runtime),
        bytearray_t{},
        1,
        runtime.get_genesis(),
        nullptr);
}

TEST_CASE(
    "consensus-redundant authenticated aggregate records late evidence only",
    "[adaptive-v2][evidence][late][aggregate][production-continuation]"
    "[mutation]")
{
    EventContext event_context;
    TestHotStuff runtime(
        1,
        1,
        bytearray_t{},
        NetAddr("127.0.0.1:0"),
        new ActiveRuntimePaceMaker(1),
        event_context,
        0,
        HotStuffBase::Net::Config(),
        NetAddr(),
        EpochProtocolMode::adaptive_v2);
    using Access = ExperimentByzantineRuntimeIntegrationTestAccess;

    const auto configuration = Access::initialize_active_runtime(runtime);
    const ProposalKey key{
        configuration, digest("redundant-aggregate-late-evidence")};
    ProposalTreeSnapshot tree;
    tree.local_replica = 1;
    tree.root = 0;
    tree.parent = 0;
    tree.direct_children = {2};
    tree.assigned_subtree = {1, 2, 3};
    tree.child_subtrees = {{2, {2, 3}}};
    tree.required_subtree = {1, 2, 3};
    tree.required_child_subtrees = {{2, {2, 3}}};
    tree.fanout = 2;
    tree.pipeline_stretch = 2;
    REQUIRE(Access::admit_context_with_tree(runtime, key, tree));
    REQUIRE(Access::initialize_accumulator(runtime, key));

    auto first = Access::aggregate_certificate(runtime, key, {2});
    REQUIRE(Access::seed_verified_aggregate(runtime, key, 2, *first));
    const auto before = Access::context_snapshot(runtime, key);
    REQUIRE(before.has_value());
    REQUIRE(before->verified_signers == std::set<ReplicaID>{2});

    std::vector<ResponseObservation> observations;
    runtime.bind_adaptive_v2_evidence_transport(
        [&observations](const EvidenceReportEnvelope &report) {
            observations.push_back(report.observation);
            return EvidenceTransportResult::accepted;
        });
    constexpr std::uint64_t start_ns = 1'000'000;
    constexpr std::uint64_t deadline_us = 1'000;
    REQUIRE(Access::arm_response_attempt(
        runtime, key, tree, start_ns, deadline_us));
    REQUIRE(Access::record_response_timeout(
                runtime, key, 2, start_ns + deadline_us * 1000) == 1);

    VoteRelay malformed_cardinality(
        key,
        quorum_cert_bt(new InconsistentAggregateCertificate(
            runtime.get_config(), key, {2, 3}, 3)),
        &runtime);
    Access::continue_verified_aggregate(
        runtime,
        key,
        malformed_cardinality,
        2,
        start_ns + deadline_us * 1000 + 1);
    static_cast<void>(runtime.flush_adaptive_v2_evidence());
    REQUIRE(observations.size() == 1);
    CHECK(observations.front().outcome == ResponseOutcome::timeout);

    VoteRelay noncanonical_duplicate(
        key,
        quorum_cert_bt(new InconsistentAggregateCertificate(
            runtime.get_config(), key, {2, 2}, 2)),
        &runtime);
    Access::continue_verified_aggregate(
        runtime,
        key,
        noncanonical_duplicate,
        2,
        start_ns + deadline_us * 1000 + 2);
    static_cast<void>(runtime.flush_adaptive_v2_evidence());
    REQUIRE(observations.size() == 1);
    CHECK(observations.front().outcome == ResponseOutcome::timeout);

    auto overlapping = Access::aggregate_certificate(runtime, key, {2, 3});
    VoteRelay relay(key, overlapping->clone(), &runtime);
    Access::continue_verified_aggregate(
        runtime,
        key,
        relay,
        2,
        start_ns + deadline_us * 1000 + 3);
    static_cast<void>(runtime.flush_adaptive_v2_evidence());

    REQUIRE(observations.size() == 2);
    CHECK(observations[0].outcome == ResponseOutcome::timeout);
    CHECK(observations[1].outcome == ResponseOutcome::late);
    CHECK(observations[1].observed_replica_id == 2);
    CHECK(observations[1].expected_message_type ==
          ExpectedMessageType::aggregate_relay);
    CHECK(observations[1].signer_set == std::vector<ReplicaID>{2, 3});
    CHECK(observations[1].observation_id == observations[0].observation_id);

    const auto after = Access::context_snapshot(runtime, key);
    REQUIRE(after.has_value());
    CHECK(after->verified_signers == before->verified_signers);
    CHECK(after->forwarded_signers == before->forwarded_signers);
}

TEST_CASE(
    "native lifecycle retirement is bounded and preserves durable references",
    "[adaptive-v2][evidence][lifecycle][retirement][runtime-integration]")
{
    EventContext event_context;
    TestHotStuff runtime(
        1,
        1,
        bytearray_t{},
        NetAddr("127.0.0.1:0"),
        new PaceMakerDummy(1),
        event_context,
        0,
        HotStuffBase::Net::Config(),
        NetAddr(),
        EpochProtocolMode::adaptive_v2);
    using Access = ExperimentByzantineRuntimeIntegrationTestAccess;

    const auto orphan = proposal_key(1, "orphan", "orphan");
    Access::seed_runtime_initialization(runtime, orphan);
    Access::seed_view_generation(runtime, orphan);
    Access::forget_key(runtime, orphan);
    CHECK_FALSE(Access::has_runtime_initialization(runtime, orphan));
    CHECK_FALSE(Access::has_view_generation(runtime, orphan));

    const auto shared_block = "shared-retirement-block";
    const auto block_orphan = proposal_key(2, "block-orphan", shared_block);
    const auto block_durable = proposal_key(3, "block-durable", shared_block);
    for (const auto &key : {block_orphan, block_durable})
    {
        Access::seed_runtime_initialization(runtime, key);
        Access::seed_view_generation(runtime, key);
    }
    Access::seed_unsuppressed_commit(runtime, block_durable);
    Access::forget_block(runtime, block_orphan.block_hash);
    CHECK_FALSE(Access::has_runtime_initialization(runtime, block_orphan));
    CHECK(Access::has_runtime_initialization(runtime, block_durable));
    CHECK_FALSE(Access::has_view_generation(runtime, block_orphan));
    CHECK_FALSE(Access::has_view_generation(runtime, block_durable));

    const auto epoch_orphan = proposal_key(4, "epoch-orphan", "epoch-orphan");
    const auto epoch_deferred =
        proposal_key(4, "epoch-deferred", "epoch-deferred");
    for (const auto &key : {epoch_orphan, epoch_deferred})
    {
        Access::seed_runtime_initialization(runtime, key);
        Access::seed_view_generation(runtime, key);
    }
    Access::seed_deferred_false_report(runtime, epoch_deferred);
    Access::forget_before_epoch(runtime, 5);
    CHECK_FALSE(Access::has_runtime_initialization(runtime, epoch_orphan));
    CHECK(Access::has_runtime_initialization(runtime, epoch_deferred));
    CHECK_FALSE(Access::has_view_generation(runtime, epoch_orphan));
    CHECK_FALSE(Access::has_view_generation(runtime, epoch_deferred));

    Access::suppress_lifecycle_reporting(runtime);
    Access::forget_key(runtime, block_durable);
    Access::forget_key(runtime, epoch_deferred);
    CHECK_FALSE(Access::has_runtime_initialization(runtime, block_durable));
    CHECK_FALSE(Access::has_runtime_initialization(runtime, epoch_deferred));
}

TEST_CASE(
    "retired initialization records do not exhaust the bounded reporter",
    "[adaptive-v2][evidence][lifecycle][capacity][runtime-integration]")
{
    EventContext event_context;
    TestHotStuff runtime(
        1,
        1,
        bytearray_t{},
        NetAddr("127.0.0.1:0"),
        new PaceMakerDummy(1),
        event_context,
        0,
        HotStuffBase::Net::Config(),
        NetAddr(),
        EpochProtocolMode::adaptive_v2);
    using Access = ExperimentByzantineRuntimeIntegrationTestAccess;

    for (std::size_t index = 0;
         index < Access::maximum_runtime_initializations();
         ++index)
    {
        Access::seed_runtime_initialization(
            runtime,
            proposal_key(
                1,
                "retired-capacity",
                "retired-capacity-" + std::to_string(index)));
    }
    REQUIRE(
        Access::runtime_initialization_count(runtime) ==
        Access::maximum_runtime_initializations());

    Access::forget_before_epoch(runtime, 2);
    CHECK(Access::runtime_initialization_count(runtime) == 0);

    Access::reset_reporting_outbox(runtime);
    const auto fresh = proposal_key(2, "fresh", "fresh");
    Access::report_runtime_initialized(runtime, fresh);
    CHECK(Access::has_runtime_initialization(runtime, fresh));
    CHECK_FALSE(Access::lifecycle_reporting_suppressed(runtime));
}

TEST_CASE(
    "missing authoritative commit identity disables convergence only",
    "[adaptive-v2][evidence][commit][fail-closed][runtime-integration]")
{
    EventContext event_context;
    TestHotStuff runtime(
        1,
        1,
        bytearray_t{},
        NetAddr("127.0.0.1:0"),
        new PaceMakerDummy(1),
        event_context,
        0,
        HotStuffBase::Net::Config(),
        NetAddr(),
        EpochProtocolMode::adaptive_v2);
    using Access = ExperimentByzantineRuntimeIntegrationTestAccess;

    auto *outbox = &Access::reset_reporting_outbox(runtime);
    Access::report_committed(runtime, std::nullopt);
    CHECK_FALSE(Access::convergence_evidence_healthy(runtime));
    CHECK_FALSE(Access::lifecycle_reporting_suppressed(runtime));
    CHECK(outbox->diagnostics().pending_reports == 0);

    outbox = &Access::reset_reporting_outbox(runtime);
    const auto expected = proposal_key(7, "expected", "expected");
    const auto mismatched = proposal_key(7, "mismatched", "mismatched");
    Access::seed_pending_commit(runtime, expected.block_hash, expected);
    Access::report_committed(runtime, mismatched);
    CHECK_FALSE(Access::convergence_evidence_healthy(runtime));
    CHECK_FALSE(Access::lifecycle_reporting_suppressed(runtime));
    CHECK(outbox->diagnostics().pending_reports == 0);
}

TEST_CASE(
    "exact authoritative commit without a local context stays proposal local",
    "[adaptive-v2][evidence][commit][no-local-context]"
    "[runtime-integration]")
{
    EventContext event_context;
    TestHotStuff runtime(
        1,
        1,
        bytearray_t{},
        NetAddr("127.0.0.1:0"),
        new PaceMakerDummy(1),
        event_context,
        0,
        HotStuffBase::Net::Config(),
        NetAddr(),
        EpochProtocolMode::adaptive_v2);
    using Access = ExperimentByzantineRuntimeIntegrationTestAccess;

    auto &outbox = Access::reset_reporting_outbox(runtime);
    const auto prior_evidence = evidence_report(
        proposal_key(7, "prior-evidence", "prior-evidence"));
    REQUIRE(outbox.enqueue_evidence(prior_evidence.canonical_payload) ==
            AdaptiveV2ReportingEnqueueStatus::queued);
    deliver_and_release(outbox, 1);
    const auto committed =
        proposal_key(7, "commit-without-context", "commit-without-context");
    Access::seed_pending_commit(runtime, committed.block_hash, committed);

    Access::report_committed(runtime, committed, false);

    CHECK_FALSE(Access::lifecycle_reporting_suppressed(runtime));
    CHECK(Access::convergence_evidence_healthy(runtime));
    CHECK_FALSE(Access::has_runtime_initialization(runtime, committed));
    REQUIRE(outbox.diagnostics().pending_reports == 1);
    REQUIRE(outbox.front() != nullptr);
    REQUIRE(outbox.front()->stream ==
            AdaptiveV2ReportingStream::lifecycle);
    const auto decoded = decode_proposal_lifecycle_notice(
        outbox.front()->canonical_payload,
        ProposalLifecycleWireLimits{});
    REQUIRE(decoded);
    REQUIRE(std::holds_alternative<ProposalCommitted>(
        decoded.notice->fact));
    const auto &fact =
        std::get<ProposalCommitted>(decoded.notice->fact);
    CHECK(fact.proposal == committed);
    CHECK(fact.evidence_sequence_fence == 1);

    const auto later =
        proposal_key(7, "later-context", "later-context");
    Access::report_runtime_initialized(runtime, later);
    REQUIRE(Access::enqueue_evidence(runtime, evidence_report(later, 2, 1)) ==
            EvidenceTransportResult::accepted);
    CHECK_FALSE(Access::lifecycle_reporting_suppressed(runtime));
    CHECK(Access::convergence_evidence_healthy(runtime));
    CHECK(outbox.diagnostics().pending_reports == 3);
}

TEST_CASE(
    "commit initialization exceptions preserve the fail-closed boundary",
    "[adaptive-v2][evidence][commit][initialization][fail-closed]"
    "[runtime-integration]")
{
    EventContext event_context;
    TestHotStuff runtime(
        1,
        1,
        bytearray_t{},
        NetAddr("127.0.0.1:0"),
        new PaceMakerDummy(1),
        event_context,
        0,
        HotStuffBase::Net::Config(),
        NetAddr(),
        EpochProtocolMode::adaptive_v2);
    using Access = ExperimentByzantineRuntimeIntegrationTestAccess;

    auto &outbox = Access::reset_reporting_outbox(runtime);
    const auto committed =
        proposal_key(7, "missing-init", "missing-init");
    Access::seed_pending_commit(runtime, committed.block_hash, committed);

    SECTION("ordinary local-context commits still require initialization")
    {
        Access::report_committed(runtime, committed, true);
    }
    SECTION("a false-report state contradicts an absent local context")
    {
        Access::seed_deferred_false_report(runtime, committed);
        Access::report_committed(runtime, committed, false);
    }
    SECTION("a deferred response state contradicts an absent local context")
    {
        Access::seed_unsuppressed_commit(runtime, committed);
        Access::report_committed(runtime, committed, false);
    }

    CHECK(Access::lifecycle_reporting_suppressed(runtime));
    CHECK_FALSE(Access::convergence_evidence_healthy(runtime));
    CHECK(outbox.diagnostics().pending_reports == 0);
}

TEST_CASE(
    "absent-context commit survives outbox backpressure exactly once",
    "[adaptive-v2][evidence][commit][no-local-context][backpressure]"
    "[runtime-integration]")
{
    EventContext event_context;
    TestHotStuff runtime(
        1,
        1,
        bytearray_t{},
        NetAddr("127.0.0.1:0"),
        new PaceMakerDummy(1),
        event_context,
        0,
        HotStuffBase::Net::Config(),
        NetAddr(),
        EpochProtocolMode::adaptive_v2);
    using Access = ExperimentByzantineRuntimeIntegrationTestAccess;

    auto &outbox = Access::reset_reporting_outbox(runtime, 1);
    const ConfigurationId active{
        7, 0, digest("backpressure-active")};
    REQUIRE(outbox.enqueue_readiness(active, 1, 0) ==
            AdaptiveV2ReportingEnqueueStatus::queued);
    const auto committed = proposal_key(
        7, "backpressure-commit", "backpressure-commit");
    Access::seed_pending_commit(runtime, committed.block_hash, committed);

    Access::report_committed(runtime, committed, false);

    CHECK_FALSE(Access::lifecycle_reporting_suppressed(runtime));
    CHECK(Access::convergence_evidence_healthy(runtime));
    CHECK(Access::has_durable_commit(runtime, committed));
    CHECK(outbox.diagnostics().pending_reports == 1);

    deliver_and_release(outbox, 1);
    Access::retry_ready_commits(runtime);

    CHECK_FALSE(Access::has_durable_commit(runtime, committed));
    REQUIRE(outbox.diagnostics().pending_reports == 1);
    REQUIRE(outbox.front() != nullptr);
    const auto decoded = decode_proposal_lifecycle_notice(
        outbox.front()->canonical_payload,
        ProposalLifecycleWireLimits{});
    REQUIRE(decoded);
    REQUIRE(std::holds_alternative<ProposalCommitted>(
        decoded.notice->fact));
    CHECK(std::get<ProposalCommitted>(decoded.notice->fact).proposal ==
          committed);

    Access::retry_ready_commits(runtime);
    CHECK(outbox.diagnostics().pending_reports == 1);
    CHECK_FALSE(Access::lifecycle_reporting_suppressed(runtime));
}

TEST_CASE(
    "authoritative absent-context consensus retires delayed admission",
    "[adaptive-v2][evidence][commit][no-local-context][proposal-retirement]"
    "[runtime-integration]")
{
    EventContext event_context;
    RelayRecordingHotStuff runtime(
        1,
        1,
        bytearray_t{},
        NetAddr("127.0.0.1:0"),
        new ActiveRuntimePaceMaker(1),
        event_context,
        0,
        HotStuffBase::Net::Config(),
        NetAddr(),
        EpochProtocolMode::adaptive_v2);
    using Access = ExperimentByzantineRuntimeIntegrationTestAccess;

    const auto configuration = Access::initialize_active_runtime(runtime);
    auto &outbox = Access::reset_reporting_outbox(runtime, 4);
    const auto blocks = delayed_proposal_blocks(
        runtime, "commit-before-runtime-initialization");
    const ProposalKey committed{
        configuration, blocks.proposal->get_hash()};
    const auto generation = checked_activation_generation(0, 0);
    REQUIRE(generation.has_value());

    const auto admitted = Access::receive_delayed_proposal(
        runtime,
        configuration,
        0,
        blocks.proposal,
        *generation);
    REQUIRE(admitted.disposition == ProposalDisposition::admitted_active);
    REQUIRE(Access::contains_admitted(runtime, committed));
    REQUIRE(Access::context_status(runtime, committed) ==
            ProposalContextStatus::unknown);
    CHECK(runtime.relay_count() == 0);

    const auto certifier = Access::direct_certifier(runtime, committed);
    Access::consensus_with_direct_certifier(
        runtime, blocks.proposal, certifier);

    CHECK_FALSE(Access::contains_admitted(runtime, committed));
    CHECK(Access::context_status(runtime, committed) ==
          ProposalContextStatus::unknown);
    CHECK_FALSE(Access::has_runtime_initialization(runtime, committed));
    CHECK(runtime.relay_count() == 0);
    CHECK_FALSE(Access::lifecycle_reporting_suppressed(runtime));
    CHECK(Access::convergence_evidence_healthy(runtime));
    REQUIRE(outbox.diagnostics().pending_reports == 1);
    REQUIRE(outbox.front() != nullptr);
    const auto decoded = decode_proposal_lifecycle_notice(
        outbox.front()->canonical_payload,
        ProposalLifecycleWireLimits{});
    REQUIRE(decoded);
    REQUIRE(std::holds_alternative<ProposalCommitted>(
        decoded.notice->fact));
    CHECK(std::get<ProposalCommitted>(decoded.notice->fact).proposal ==
          committed);

    Access::resolve_fetched_block(runtime, blocks.missing_parent);
    CHECK(Access::is_block_delivered(
        runtime, blocks.missing_parent->get_hash()));
    CHECK(Access::is_block_delivered(
        runtime, blocks.proposal->get_hash()));
    CHECK_FALSE(Access::contains_admitted(runtime, committed));
    CHECK(Access::context_status(runtime, committed) ==
          ProposalContextStatus::unknown);
    CHECK_FALSE(Access::has_runtime_initialization(runtime, committed));
    CHECK(runtime.relay_count() == 0);
    CHECK(outbox.diagnostics().pending_reports == 1);

    const auto replay = Access::receive_delayed_proposal(
        runtime,
        configuration,
        0,
        blocks.proposal,
        *generation);
    CHECK(replay.disposition == ProposalDisposition::duplicate);
    CHECK_FALSE(Access::contains_admitted(runtime, committed));
    CHECK_FALSE(Access::has_runtime_initialization(runtime, committed));
    CHECK(outbox.diagnostics().pending_reports == 1);
}

TEST_CASE(
    "adaptive v2 deadline arm rejection poisons evidence but preserves exposure",
    "[adaptive-v2][evidence][deadline-arm][relay-order]"
    "[runtime-integration]")
{
    EventContext event_context;
    RelayRecordingHotStuff runtime(
        1,
        1,
        bytearray_t{},
        NetAddr("127.0.0.1:0"),
        new ActiveRuntimePaceMaker(1),
        event_context,
        0,
        HotStuffBase::Net::Config(),
        NetAddr(),
        EpochProtocolMode::adaptive_v2);
    using Access = ExperimentByzantineRuntimeIntegrationTestAccess;

    const bytearray_t issuer_secret(32, 1);
    const PrivKeySecp256k1 issuer_key(issuer_secret);
    runtime.configure_epoch_change_pre_vote_gate(
        EpochChangeIssuer{17, PubKeySecp256k1(issuer_key)},
        EpochChangeDelayBounds{1, 20},
        4U * 1024U * 1024U,
        64);
    const auto configuration = Access::initialize_active_runtime(runtime);
    Access::reset_reporting_outbox(runtime, 4);
    Access::reject_response_deadline_scheduling(runtime);
    const auto blocks = delayed_proposal_blocks(
        runtime, "deadline-arm-rejection");
    const ProposalKey key{configuration, blocks.proposal->get_hash()};
    const auto generation = checked_activation_generation(0, 0);
    REQUIRE(generation.has_value());

    const auto admitted = Access::receive_delayed_proposal(
        runtime,
        configuration,
        0,
        blocks.proposal,
        *generation);
    REQUIRE(admitted.disposition == ProposalDisposition::admitted_active);
    CHECK(runtime.relay_count() == 0);

    Access::resolve_fetched_block(runtime, blocks.missing_parent);

    CHECK(runtime.relay_count() == 1);
    CHECK(Access::contains_admitted(runtime, key));
    CHECK_FALSE(Access::convergence_evidence_healthy(runtime));
    CHECK(Access::has_response_attempt_arm_failure(runtime, key));
    CHECK(Access::response_attempt_arm_failure_count(runtime) == 1);
}

TEST_CASE(
    "adaptive v2 missing response bridge poisons once without gating relay",
    "[adaptive-v2][evidence][deadline-arm][canonical-poison]"
    "[runtime-integration]")
{
    EventContext event_context;
    RelayRecordingHotStuff runtime(
        1,
        1,
        bytearray_t{},
        NetAddr("127.0.0.1:0"),
        new ActiveRuntimePaceMaker(1),
        event_context,
        0,
        HotStuffBase::Net::Config(),
        NetAddr(),
        EpochProtocolMode::adaptive_v2);
    using Access = ExperimentByzantineRuntimeIntegrationTestAccess;

    const bytearray_t issuer_secret(32, 1);
    const PrivKeySecp256k1 issuer_key(issuer_secret);
    runtime.configure_epoch_change_pre_vote_gate(
        EpochChangeIssuer{17, PubKeySecp256k1(issuer_key)},
        EpochChangeDelayBounds{1, 20},
        4U * 1024U * 1024U,
        64);
    const auto configuration = Access::initialize_active_runtime(runtime);
    Access::reset_reporting_outbox(runtime, 4);
    Access::remove_response_evidence_bridge(runtime);
    const auto blocks = delayed_proposal_blocks(
        runtime, "missing-response-bridge");
    const ProposalKey key{configuration, blocks.proposal->get_hash()};
    const auto generation = checked_activation_generation(0, 0);
    REQUIRE(generation.has_value());

    REQUIRE(Access::receive_delayed_proposal(
                runtime,
                configuration,
                0,
                blocks.proposal,
                *generation)
                .disposition == ProposalDisposition::admitted_active);
    Access::resolve_fetched_block(runtime, blocks.missing_parent);

    CHECK(runtime.relay_count() == 1);
    CHECK_FALSE(Access::convergence_evidence_healthy(runtime));
    CHECK(Access::has_response_attempt_arm_failure(runtime, key));
    CHECK(Access::response_attempt_arm_failure_count(runtime) == 1);
}

TEST_CASE(
    "adaptive v2 missing exact tree emits canonical arm poison once",
    "[adaptive-v2][evidence][deadline-arm][canonical-poison]"
    "[runtime-integration]")
{
    EventContext event_context;
    TestHotStuff runtime(
        1,
        1,
        bytearray_t{},
        NetAddr("127.0.0.1:0"),
        new ActiveRuntimePaceMaker(1),
        event_context,
        0,
        HotStuffBase::Net::Config(),
        NetAddr(),
        EpochProtocolMode::adaptive_v2);
    using Access = ExperimentByzantineRuntimeIntegrationTestAccess;

    static_cast<void>(Access::initialize_active_runtime(runtime));
    Access::reset_reporting_outbox(runtime, 4);
    const ProposalKey key{
        ConfigurationId{99, 7, digest("missing-exact-tree")},
        digest("missing-exact-tree-proposal")};
    auto context_tree = tree(ExperimentReplicaRole::internal);
    context_tree.assigned_subtree = {1, 2, 4};
    context_tree.child_subtrees = {{2, {2}}, {4, {4}}};
    REQUIRE(Access::admit_context_with_tree(
        runtime, key, std::move(context_tree)));

    Access::attempt_evidence_before_exposure(runtime, key);
    Access::attempt_evidence_before_exposure(runtime, key);

    CHECK_FALSE(Access::convergence_evidence_healthy(runtime));
    CHECK(Access::has_response_attempt_arm_failure(runtime, key));
    CHECK(Access::response_attempt_arm_failure_count(runtime) == 1);
}

TEST_CASE(
    "adaptive v2 scheduler exceptions poison once without gating relay",
    "[adaptive-v2][evidence][deadline-arm][canonical-poison]"
    "[runtime-integration]")
{
    EventContext event_context;
    RelayRecordingHotStuff runtime(
        1,
        1,
        bytearray_t{},
        NetAddr("127.0.0.1:0"),
        new ActiveRuntimePaceMaker(1),
        event_context,
        0,
        HotStuffBase::Net::Config(),
        NetAddr(),
        EpochProtocolMode::adaptive_v2);
    using Access = ExperimentByzantineRuntimeIntegrationTestAccess;

    const bytearray_t issuer_secret(32, 1);
    const PrivKeySecp256k1 issuer_key(issuer_secret);
    runtime.configure_epoch_change_pre_vote_gate(
        EpochChangeIssuer{17, PubKeySecp256k1(issuer_key)},
        EpochChangeDelayBounds{1, 20},
        4U * 1024U * 1024U,
        64);
    const auto configuration = Access::initialize_active_runtime(runtime);
    Access::reset_reporting_outbox(runtime, 4);
    auto scheduler = std::make_unique<ThrowingAggregationScheduler>();
    auto *scheduler_observer = scheduler.get();
    Access::replace_aggregation_scheduler(runtime, std::move(scheduler));
    const auto blocks = delayed_proposal_blocks(
        runtime, "throwing-arm-and-timer-scheduler");
    const ProposalKey key{configuration, blocks.proposal->get_hash()};
    const auto generation = checked_activation_generation(0, 0);
    REQUIRE(generation.has_value());

    REQUIRE(Access::receive_delayed_proposal(
                runtime,
                configuration,
                0,
                blocks.proposal,
                *generation)
                .disposition == ProposalDisposition::admitted_active);
    Access::resolve_fetched_block(runtime, blocks.missing_parent);

    CHECK(scheduler_observer->attempts >= 1);
    CHECK(runtime.relay_count() == 1);
    CHECK_FALSE(Access::convergence_evidence_healthy(runtime));
    CHECK(Access::has_response_attempt_arm_failure(runtime, key));
    CHECK(Access::response_attempt_arm_failure_count(runtime) == 1);
}

TEST_CASE(
    "finalized root reuses only exact successful arm provenance",
    "[adaptive-v2][evidence][deadline-arm][finalized-retransmit]"
    "[runtime-integration]")
{
    EventContext event_context;
    TestHotStuff runtime(
        1,
        1,
        bytearray_t{},
        NetAddr("127.0.0.1:0"),
        new ActiveRuntimePaceMaker(1),
        event_context,
        0,
        HotStuffBase::Net::Config(),
        NetAddr(),
        EpochProtocolMode::adaptive_v2);
    using Access = ExperimentByzantineRuntimeIntegrationTestAccess;

    const auto configuration = Access::initialize_active_runtime(runtime);
    Access::reset_reporting_outbox(runtime, 4);
    const ProposalKey armed{
        configuration, digest("finalized-prior-successful-arm")};
    REQUIRE(Access::admit_exact_context(runtime, armed));
    REQUIRE(Access::start_response_attempt_arm(runtime, armed));
    REQUIRE(Access::has_successful_response_attempt_arm(runtime, armed));
    Access::close_exact_context(runtime, armed);

    Access::ensure_finalized_evidence_before_exposure(runtime, armed);
    CHECK(Access::convergence_evidence_healthy(runtime));
    CHECK_FALSE(Access::has_response_attempt_arm_failure(runtime, armed));

    const ProposalKey missing{
        configuration, digest("finalized-missing-prior-arm")};
    Access::ensure_finalized_evidence_before_exposure(runtime, missing);
    Access::ensure_finalized_evidence_before_exposure(runtime, missing);
    CHECK_FALSE(Access::convergence_evidence_healthy(runtime));
    CHECK(Access::has_response_attempt_arm_failure(runtime, missing));
    CHECK(Access::response_attempt_arm_failure_count(runtime) == 1);
}

TEST_CASE(
    "adaptive v2 aggregation timer rejection canonically poisons evidence",
    "[adaptive-v2][evidence][aggregation-timer][canonical-poison]"
    "[runtime-integration]")
{
    EventContext event_context;
    TestHotStuff runtime(
        1,
        1,
        bytearray_t{},
        NetAddr("127.0.0.1:0"),
        new ActiveRuntimePaceMaker(1),
        event_context,
        0,
        HotStuffBase::Net::Config(),
        NetAddr(),
        EpochProtocolMode::adaptive_v2);
    using Access = ExperimentByzantineRuntimeIntegrationTestAccess;

    const auto configuration = Access::initialize_active_runtime(runtime);
    Access::reset_reporting_outbox(runtime, 4);
    const ProposalKey key{
        configuration, digest("aggregation-timer-rejection")};
    REQUIRE(Access::admit_exact_context(runtime, key));
    REQUIRE(Access::start_response_attempt_arm(runtime, key));

    Access::remove_aggregation_timeout_coordinator(runtime);
    Access::start_aggregation_timer(runtime, key);

    CHECK_FALSE(Access::convergence_evidence_healthy(runtime));
    CHECK(Access::has_response_attempt_arm_failure(runtime, key));
    CHECK(Access::response_attempt_arm_failure_count(runtime) == 1);
}

TEST_CASE(
    "adaptive-v3 commit reporting preserves exact lifecycle and one authority",
    "[adaptive-v3][evidence][commit][lifecycle][runtime-integration]")
{
    using Access = ExperimentByzantineRuntimeIntegrationTestAccess;
    ScopedSigpipeIgnore ignore_sigpipe;

    SECTION("a designated exact commit emits one authoritative event")
    {
        EventContext event_context;
        TestHotStuff runtime(
            1, 1, bytearray_t{}, NetAddr("127.0.0.1:0"),
            new ActiveRuntimePaceMaker(1), event_context, 0,
            HotStuffBase::Net::Config(), NetAddr(),
            EpochProtocolMode::adaptive_v3,
            adaptive_v3_runtime_config(1));
        const auto configuration = Access::initialize_active_runtime(runtime);
        Access::replace_aggregation_scheduler(
            runtime, std::make_unique<ThrowingAggregationScheduler>());
        auto &outbox = Access::reset_reporting_outbox(runtime, 4);
        RecordingProtocolEmitter emitter;
        runtime.bind_structured_event_emitters(&emitter, nullptr, nullptr);
        const auto block = indirect_commit_block(runtime, "v3-designated");
        const ProposalKey key{configuration, block->get_hash()};
        Access::seed_view_generation(runtime, key, 17);
        REQUIRE(Access::retain_commit_event_identity(runtime, key, 17));
        const auto cached = Access::resolve_and_cache_commit(
            runtime, block, {}, Access::direct_certifier(runtime, key));
        REQUIRE(cached.key == key);
        REQUIRE(cached.event_key == key);

        Access::report_and_post_commit(runtime, block);

        REQUIRE(emitter.events.size() == 2);
        REQUIRE(std::get_if<CommitObservedStructuredEvent>(
                    &emitter.events[0]) != nullptr);
        const auto *committed =
            std::get_if<CommitStructuredEvent>(&emitter.events[1]);
        REQUIRE(committed != nullptr);
        CHECK(committed->decision_proof == key);
        CHECK(committed->view_generation == 17);
        REQUIRE(outbox.front() != nullptr);
        CHECK(outbox.front()->stream == AdaptiveV2ReportingStream::lifecycle);
        const auto decoded = decode_proposal_lifecycle_notice(
            outbox.front()->canonical_payload, ProposalLifecycleWireLimits{});
        REQUIRE(decoded);
        CHECK(decoded.notice->source_replica_id == 1);
        CHECK(decoded.notice->source_sequence == 1);
        REQUIRE(std::holds_alternative<ProposalCommitted>(
            decoded.notice->fact));
        const auto &fact = std::get<ProposalCommitted>(decoded.notice->fact);
        CHECK(fact.proposal == key);
        CHECK(fact.evidence_sequence_fence == 0);
    }

    SECTION(
        "a locally constructed proposal is retained before admission can stall")
    {
        EventContext event_context;
        TestHotStuff runtime(
            1, 1, bytearray_t{}, NetAddr("127.0.0.1:0"),
            new ActiveRuntimePaceMaker(1), event_context, 0,
            HotStuffBase::Net::Config(), NetAddr(),
            EpochProtocolMode::adaptive_v3,
            adaptive_v3_runtime_config(1));
        const auto configuration = Access::initialize_active_runtime(runtime);
        const auto generation =
            Access::exact_runtime_generation(runtime, configuration);
        REQUIRE(generation.has_value());
        REQUIRE(*generation != 0);
        RecordingProtocolEmitter emitter(false);
        runtime.bind_structured_event_emitters(&emitter, nullptr, nullptr);
        const auto block = indirect_commit_block(runtime, "v3-local-before-admit");
        const Proposal proposal(
            runtime.get_id(),
            configuration.epoch_number,
            configuration.tree_id,
            configuration.epoch_digest,
            block,
            &runtime);

        Access::capture_local_proposal_before_admission(runtime, proposal);

        CHECK(Access::retained_commit_event_identity_count(runtime) == 1);
        const auto cached = Access::resolve_and_cache_commit(
            runtime, block, {}, nullptr, true);
        CHECK(cached.key == std::nullopt);
        CHECK(cached.unavailable);
        REQUIRE(cached.event_key == proposal.key());
        REQUIRE(cached.event_generation == generation);

        Access::report_and_post_commit(runtime, block);

        REQUIRE(emitter.events.size() == 2);
        REQUIRE(std::get_if<CommitObservedStructuredEvent>(
                    &emitter.events[0]) != nullptr);
        const auto *witness =
            std::get_if<CommitIdentityWitnessStructuredEvent>(
                &emitter.events[1]);
        REQUIRE(witness != nullptr);
        CHECK(witness->decision_proof == proposal.key());
        CHECK(witness->view_generation == generation);
    }

    SECTION("non-designated and inexact commits never become authoritative")
    {
        for (const bool designated : {false, true})
        {
            EventContext event_context;
            TestHotStuff runtime(
                1, 1, bytearray_t{}, NetAddr("127.0.0.1:0"),
                new ActiveRuntimePaceMaker(1), event_context, 0,
                HotStuffBase::Net::Config(), NetAddr(),
                EpochProtocolMode::adaptive_v3,
                adaptive_v3_runtime_config(1));
            const auto configuration =
                Access::initialize_active_runtime(runtime);
            Access::replace_aggregation_scheduler(
                runtime, std::make_unique<ThrowingAggregationScheduler>());
            auto &outbox = Access::reset_reporting_outbox(runtime, 4);
            RecordingProtocolEmitter emitter(designated);
            runtime.bind_structured_event_emitters(&emitter, nullptr, nullptr);
            const auto block = indirect_commit_block(
                runtime,
                designated ? "v3-conflicting" : "v3-nondesignated");
            if (!designated)
            {
                const ProposalKey key{configuration, block->get_hash()};
                Access::seed_view_generation(runtime, key, 23);
                REQUIRE(Access::retain_commit_event_identity(runtime, key, 23));
                const auto cached = Access::resolve_and_cache_commit(
                    runtime,
                    block,
                    {},
                    Access::direct_certifier(runtime, key));
                REQUIRE(cached.event_key == key);
            }
            else
            {
                const auto cached = Access::resolve_and_cache_commit(
                    runtime, block, {}, nullptr);
                CHECK(cached.event_conflicted);
            }

            Access::report_and_post_commit(runtime, block);

            REQUIRE(emitter.events.size() == (designated ? 1 : 2));
            CHECK(std::get_if<CommitObservedStructuredEvent>(
                      &emitter.events[0]) != nullptr);
            if (!designated)
            {
                const auto *witness =
                    std::get_if<CommitIdentityWitnessStructuredEvent>(
                        &emitter.events[1]);
                REQUIRE(witness != nullptr);
                CHECK(witness->decision_proof ==
                      ProposalKey{configuration, block->get_hash()});
                CHECK(witness->view_generation == 23);
            }
            CHECK(std::none_of(
                emitter.events.begin(), emitter.events.end(),
                [](const auto &event) {
                    return std::get_if<CommitStructuredEvent>(&event) !=
                           nullptr;
                }));
            CHECK(outbox.diagnostics().pending_reports ==
                  (designated ? 0 : 1));
        }
    }

    SECTION("a legal unavailable identity remains explicitly non-authoritative")
    {
        EventContext event_context;
        TestHotStuff runtime(
            1, 1, bytearray_t{}, NetAddr("127.0.0.1:0"),
            new ActiveRuntimePaceMaker(1), event_context, 0,
            HotStuffBase::Net::Config(), NetAddr(),
            EpochProtocolMode::adaptive_v3,
            adaptive_v3_runtime_config(1));
        static_cast<void>(Access::initialize_active_runtime(runtime));
        Access::replace_aggregation_scheduler(
            runtime, std::make_unique<ThrowingAggregationScheduler>());
        auto &outbox = Access::reset_reporting_outbox(runtime, 4);
        RecordingProtocolEmitter emitter;
        runtime.bind_structured_event_emitters(&emitter, nullptr, nullptr);
        const auto block = indirect_commit_block(runtime, "v3-unavailable");
        const auto cached =
            Access::resolve_and_cache_commit(runtime, block, {}, nullptr, true);
        CHECK(cached.unavailable);
        CHECK(cached.event_unavailable);

        Access::report_and_post_commit(runtime, block);

        CHECK(outbox.diagnostics().pending_reports == 0);
        REQUIRE(emitter.events.size() == 2);
        CHECK(std::get_if<CommitObservedStructuredEvent>(
                  &emitter.events[0]) != nullptr);
        CHECK(std::get_if<CommitIdentityUnavailableStructuredEvent>(
                  &emitter.events[1]) != nullptr);
        CHECK(std::none_of(
            emitter.events.begin(), emitter.events.end(),
            [](const auto &event) {
                return std::get_if<CommitStructuredEvent>(&event) != nullptr;
            }));
    }

    SECTION("lifecycle capacity never mutates or substitutes an exact report")
    {
        EventContext event_context;
        TestHotStuff runtime(
            1, 1, bytearray_t{}, NetAddr("127.0.0.1:0"),
            new ActiveRuntimePaceMaker(1), event_context, 0,
            HotStuffBase::Net::Config(), NetAddr(),
            EpochProtocolMode::adaptive_v3,
            adaptive_v3_runtime_config(1));
        const auto configuration = Access::initialize_active_runtime(runtime);
        Access::replace_aggregation_scheduler(
            runtime, std::make_unique<ThrowingAggregationScheduler>());
        auto &outbox = Access::reset_reporting_outbox(runtime, 1);
        const ProposalKey first{configuration, digest("v3-capacity-first")};
        const ProposalKey second{configuration, digest("v3-capacity-second")};

        Access::report_committed(runtime, first);
        Access::report_committed(runtime, second);

        CHECK(outbox.diagnostics().pending_reports == 1);
        REQUIRE(outbox.front() != nullptr);
        CHECK(outbox.front()->stream == AdaptiveV2ReportingStream::lifecycle);
        const auto decoded = decode_proposal_lifecycle_notice(
            outbox.front()->canonical_payload, ProposalLifecycleWireLimits{});
        REQUIRE(decoded);
        REQUIRE(std::holds_alternative<ProposalCommitted>(
            decoded.notice->fact));
        CHECK(std::get<ProposalCommitted>(decoded.notice->fact).proposal ==
              first);
        CHECK(decoded.notice->source_replica_id == 1);
        CHECK(decoded.notice->source_sequence == 1);
        CHECK_FALSE(Access::lifecycle_reporting_suppressed(runtime));
        CHECK(Access::convergence_evidence_healthy(runtime));
    }
}

TEST_CASE(
    "adaptive-v3 exact timeout evidence reaches the shared authenticated outbox",
    "[adaptive-v3][evidence][schema-v3][runtime-integration]")
{
    using Access = ExperimentByzantineRuntimeIntegrationTestAccess;
    ScopedSigpipeIgnore ignore_sigpipe;
    EventContext event_context;
    TestHotStuff runtime(
        1, 1, bytearray_t{}, NetAddr("127.0.0.1:0"),
        new ActiveRuntimePaceMaker(1), event_context, 0,
        HotStuffBase::Net::Config(), NetAddr(),
        EpochProtocolMode::adaptive_v3,
        adaptive_v3_runtime_config(1));
    REQUIRE_NOTHROW(
        runtime.enable_experiment_exact_timeout_attempt_evidence_v3());
    const auto configuration = Access::initialize_active_runtime(runtime);
    auto &outbox = Access::reset_reporting_outbox(runtime, 4);
    const ProposalKey key{
        configuration, digest("v3-exact-timeout-evidence")};
    REQUIRE(Access::admit_exact_context(runtime, key));
    const auto exact_tree = Access::context_tree(runtime, key);
    REQUIRE_FALSE(exact_tree.direct_children.empty());
    const auto child = *exact_tree.direct_children.begin();
    constexpr std::uint64_t start_ns = 1'000'000;
    constexpr std::uint64_t deadline_us = 1'000;
    REQUIRE(Access::arm_response_attempt(
        runtime, key, exact_tree, start_ns, deadline_us));
    REQUIRE(Access::record_response_timeout(
                runtime,
                key,
                child,
                start_ns + deadline_us * 1'000) == 1);

    REQUIRE(outbox.front() != nullptr);
    CHECK(outbox.front()->stream == AdaptiveV2ReportingStream::evidence);
    CHECK(outbox.front()->opcode == MsgEvidenceReport::opcode);
    const auto decoded = decode_evidence_batch(
        outbox.front()->canonical_payload, EvidenceWireLimits{});
    REQUIRE(decoded);
    REQUIRE(decoded.batch->observations.size() == 1);
    const auto &observation = decoded.batch->observations.front();
    CHECK(observation.schema_version == kResponseObservationSchemaVersionV3);
    CHECK(observation.reporter_id == 1);
    CHECK(observation.observed_replica_id == child);
    CHECK(observation.proposal_key() == key);
    CHECK(observation.outcome == ResponseOutcome::timeout);
    CHECK(observation.attempt_start_monotonic_ns == start_ns);
    CHECK(observation.deadline_duration_us == deadline_us);
}

TEST_CASE(
    "adaptive-v3 publishes initial operational readiness to the unified manager",
    "[adaptive-v3][manager][readiness][runtime-integration]")
{
    using Access = ExperimentByzantineRuntimeIntegrationTestAccess;
    ScopedSigpipeIgnore ignore_sigpipe;
    EventContext event_context;
    TestHotStuff runtime(
        1, 1, bytearray_t{}, NetAddr("127.0.0.1:0"),
        new ActiveRuntimePaceMaker(1), event_context, 0,
        HotStuffBase::Net::Config(), NetAddr(),
        EpochProtocolMode::adaptive_v3,
        adaptive_v3_runtime_config(1));

    const auto configuration = Access::initialize_active_runtime(runtime);
    const auto *report = Access::reporting_front(runtime);
    REQUIRE(report != nullptr);
    CHECK(report->stream == AdaptiveV2ReportingStream::readiness);
    CHECK(report->opcode == MsgAdaptiveV2ReadinessNotice::opcode);
    const auto decoded = decode_adaptive_v2_readiness_notice(
        report->canonical_payload, AdaptiveV2ReadinessWireLimits{});
    REQUIRE(decoded);
    CHECK(decoded.notice->claimed_source_replica_id == 1);
    CHECK(decoded.notice->source_sequence == 1);
    CHECK(decoded.notice->active_configuration == configuration);
    CHECK(decoded.notice->activation_generation == 1);
    CHECK(decoded.notice->committed_height == 0);
}

TEST_CASE(
    "adaptive-v3 retry exhaustion preserves matching certificate eligibility",
    "[adaptive-v3][readiness][retry][runtime-integration][regression]")
{
    using Access = ExperimentByzantineRuntimeIntegrationTestAccess;
    ScopedSigpipeIgnore ignore_sigpipe;
    EventContext event_context;
    auto config = adaptive_v3_runtime_config(1);
    config.maximum_observation_attempts = 1;
    const auto local_key = config.local_readiness_private_key;
    std::vector<std::pair<ReplicaID, PubKeyBLS>> membership;
    membership.reserve(config.readiness_membership.size());
    for (const auto &member : config.readiness_membership)
        membership.emplace_back(member.replica_id, member.public_key);
    TestHotStuff runtime(
        1, 1, bytearray_t{}, NetAddr("127.0.0.1:0"),
        new ActiveRuntimePaceMaker(1), event_context, 0,
        HotStuffBase::Net::Config(), NetAddr(),
        EpochProtocolMode::adaptive_v3, config);
    const auto active = Access::initialize_active_runtime(runtime);
    RecordingReadinessAuditEmitter emitter;
    runtime.bind_structured_event_emitters(nullptr, nullptr, &emitter);

    ActivationReadyIdentityV1 identity;
    identity.membership_digest =
        canonical_activation_readiness_membership_digest(membership);
    identity.predecessor_boundary_configuration = active;
    identity.predecessor_boundary_generation =
        *checked_activation_generation(active.epoch_number, 0);
    identity.successor_configuration = {
        static_cast<std::uint32_t>(active.epoch_number + 1),
        0,
        digest("retry-exhausted-successor")};
    identity.successor_activation_generation =
        *checked_activation_generation(
            identity.successor_configuration.epoch_number, 0);
    identity.command_payload_digest = digest("retry-exhausted-command");
    identity.command_block_height = 10;
    identity.command_block_hash = digest("retry-exhausted-command-block");
    identity.activation_delay_blocks = 1;
    identity.activation_height = 11;
    identity.activation_boundary_block_hash =
        digest("retry-exhausted-boundary");
    REQUIRE(local_key != nullptr);
    const auto observation = sign_activation_ready_observation(
        identity, 1, 1, 10'000, *local_key);
    const auto payload = encode_activation_ready_observation(
        observation, config.readiness_wire_limits);
    Access::seed_adaptive_v3_observation_retry(
        runtime, observation, payload, config.maximum_observation_attempts);

    Access::transmit_adaptive_v3_observation(runtime);

    CHECK(Access::adaptive_v3_observation_retry_exhausted(runtime));
    CHECK_FALSE(Access::adaptive_v3_observation_terminal(runtime));
    CHECK(Access::has_adaptive_v3_pending_observation(runtime));
    REQUIRE(emitter.events.size() == 1);
    const auto &event = emitter.events.front();
    CHECK(event.transition ==
          AdaptiveV3ReadinessTransition::observation_retry_exhausted);
    CHECK(event.identity == identity);
    CHECK(event.replica_id == 1);
    CHECK(event.signer_source_sequence == 1);
    CHECK(event.signer_monotonic_raw_ns == 10'000);
    CHECK(event.canonical_wire_payload == payload);
    CHECK(event.disposition == "retry_exhausted");

    // Exhaustion is latched and produces no repeated audit or retransmit.
    Access::transmit_adaptive_v3_observation(runtime);
    CHECK(emitter.events.size() == 1);
    CHECK_FALSE(Access::adaptive_v3_observation_terminal(runtime));
    CHECK(Access::has_adaptive_v3_pending_observation(runtime));
}

TEST_CASE(
    "legal skipped-QC commit gaps emit an unavailable identity disposition",
    "[adaptive-v2][evidence][commit][identity-unavailable][qc-skip][runtime-integration]")
{
    EventContext event_context;
    TestHotStuff runtime(
        1,
        1,
        bytearray_t{},
        NetAddr("127.0.0.1:0"),
        new ActiveRuntimePaceMaker(1),
        event_context,
        0,
        HotStuffBase::Net::Config(),
        NetAddr(),
        EpochProtocolMode::adaptive_v2);
    using Access = ExperimentByzantineRuntimeIntegrationTestAccess;

    const auto configuration = Access::initialize_active_runtime(runtime);
    Access::reset_reporting_outbox(runtime, 16);
    RecordingProtocolEmitter emitter;
    runtime.bind_structured_event_emitters(&emitter, nullptr, nullptr);

    const auto genesis = runtime.get_genesis();
    const auto block1 = Access::add_commit_rule_block(
        runtime, configuration, genesis, genesis, "gap-1");
    const auto block2 = Access::add_commit_rule_block(
        runtime, configuration, block1, block1, "gap-2");
    const auto block3 = Access::add_commit_rule_block(
        runtime, configuration, block2, block1, "gap-3");
    const auto block4 = Access::add_commit_rule_block(
        runtime, configuration, block3, block1, "gap-4");
    const auto block5 = Access::add_commit_rule_block(
        runtime, configuration, block4, block3, "gap-5");
    const auto block6 = Access::add_commit_rule_block(
        runtime, configuration, block5, block3, "gap-6");
    const auto block7 = Access::add_commit_rule_block(
        runtime, configuration, block6, block5, "gap-7");
    const auto block8 = Access::add_commit_rule_block(
        runtime, configuration, block7, block7, "gap-8");
    Access::seed_runtime_initialization(
        runtime, ProposalKey{configuration, block1->get_hash()});
    Access::seed_runtime_initialization(
        runtime, ProposalKey{configuration, block3->get_hash()});

    REQUIRE_NOTHROW(Access::apply_update(runtime, block8));
    CHECK(Access::convergence_evidence_healthy(runtime));

    REQUIRE(emitter.events.size() == 6);
    const auto *observed1 =
        std::get_if<CommitObservedStructuredEvent>(&emitter.events[0]);
    const auto *committed1 =
        std::get_if<CommitStructuredEvent>(&emitter.events[1]);
    const auto *observed2 =
        std::get_if<CommitObservedStructuredEvent>(&emitter.events[2]);
    const auto *unavailable =
        std::get_if<CommitIdentityUnavailableStructuredEvent>(
            &emitter.events[3]);
    const auto *observed3 =
        std::get_if<CommitObservedStructuredEvent>(&emitter.events[4]);
    const auto *committed3 =
        std::get_if<CommitStructuredEvent>(&emitter.events[5]);
    REQUIRE(observed1 != nullptr);
    REQUIRE(committed1 != nullptr);
    REQUIRE(observed2 != nullptr);
    REQUIRE(unavailable != nullptr);
    REQUIRE(observed3 != nullptr);
    REQUIRE(committed3 != nullptr);
    CHECK(observed1->block_hash == block1->get_hash());
    CHECK(committed1->block_hash == block1->get_hash());
    CHECK(observed2->block_hash == block2->get_hash());
    CHECK(unavailable->block_height == block2->get_height());
    CHECK(unavailable->block_hash == block2->get_hash());
    REQUIRE(unavailable->parent_hash.has_value());
    CHECK(*unavailable->parent_hash == block1->get_hash());
    CHECK(unavailable->transaction_count == block2->get_cmds().size());
    CHECK(unavailable->commit_batch_index == 1);
    CHECK(unavailable->reason ==
          CommitIdentityUnavailableReason::
              no_authenticated_exact_identity_source);
    CHECK_FALSE(unavailable->convergence_identity_pending);
    CHECK(observed3->block_hash == block3->get_hash());
    CHECK(committed3->block_hash == block3->get_hash());
    runtime.bind_structured_event_emitters(nullptr, nullptr, nullptr);
}

TEST_CASE(
    "legal skipped-QC ancestors recover only one retained event identity",
    "[adaptive-v2][evidence][commit][identity-unavailable][qc-skip][retained][runtime-integration]")
{
    using Access = ExperimentByzantineRuntimeIntegrationTestAccess;

    SECTION("a closed live context retains its exact event identity")
    {
        EventContext event_context;
        TestHotStuff runtime(
            1,
            1,
            bytearray_t{},
            NetAddr("127.0.0.1:0"),
            new ActiveRuntimePaceMaker(1),
            event_context,
            0,
            HotStuffBase::Net::Config(),
            NetAddr(),
            EpochProtocolMode::adaptive_v2);
        const auto configuration = Access::initialize_active_runtime(runtime);
        Access::reset_reporting_outbox(runtime, 4);
        RecordingProtocolEmitter emitter;
        runtime.bind_structured_event_emitters(&emitter, nullptr, nullptr);

        const auto block = indirect_commit_block(runtime, "retained-qc-skip");
        const ProposalKey key{configuration, block->get_hash()};
        Access::seed_view_generation(runtime, key, 77);
        REQUIRE(Access::retain_commit_event_identity(runtime, key, 77));
        REQUIRE(Access::admit_exact_context(runtime, key));
        Access::close_exact_context(runtime, key);
        CHECK(Access::context_status(runtime, key) ==
              ProposalContextStatus::terminal_closed);

        const auto cached = Access::resolve_and_cache_commit(
            runtime, block, {}, nullptr, true);
        CHECK_FALSE(cached.key.has_value());
        CHECK_FALSE(cached.generation.has_value());
        CHECK(cached.unavailable);
        CHECK_FALSE(cached.conflicted);
        REQUIRE(cached.event_key == key);
        CHECK(cached.event_generation == 77);
        CHECK_FALSE(cached.event_unavailable);
        CHECK_FALSE(cached.event_conflicted);

        Access::report_and_post_commit(runtime, block);
        REQUIRE(emitter.events.size() == 2);
        CHECK(std::get_if<CommitObservedStructuredEvent>(&emitter.events[0]) !=
              nullptr);
        const auto *committed =
            std::get_if<CommitStructuredEvent>(&emitter.events[1]);
        REQUIRE(committed != nullptr);
        CHECK(committed->block_hash == block->get_hash());
        CHECK(committed->decision_proof == key);
        CHECK(committed->view_generation == 77);
    }

    SECTION("an absent retained identity remains unavailable")
    {
        EventContext event_context;
        TestHotStuff runtime(
            1,
            1,
            bytearray_t{},
            NetAddr("127.0.0.1:0"),
            new ActiveRuntimePaceMaker(1),
            event_context,
            0,
            HotStuffBase::Net::Config(),
            NetAddr(),
            EpochProtocolMode::adaptive_v2);
        const auto configuration = Access::initialize_active_runtime(runtime);
        Access::reset_reporting_outbox(runtime, 4);
        RecordingProtocolEmitter emitter;
        runtime.bind_structured_event_emitters(&emitter, nullptr, nullptr);
        const auto block = indirect_commit_block(runtime, "absent-qc-skip");
        const ProposalKey key{configuration, block->get_hash()};
        REQUIRE(Access::admit_exact_context(runtime, key));
        Access::close_exact_context(runtime, key);

        const auto cached = Access::resolve_and_cache_commit(
            runtime, block, {}, nullptr, true);
        CHECK_FALSE(cached.key.has_value());
        CHECK_FALSE(cached.generation.has_value());
        CHECK(cached.unavailable);
        CHECK_FALSE(cached.conflicted);
        CHECK_FALSE(cached.event_key.has_value());
        CHECK_FALSE(cached.event_generation.has_value());
        CHECK(cached.event_unavailable);
        CHECK_FALSE(cached.event_conflicted);

        Access::report_and_post_commit(runtime, block);
        REQUIRE(emitter.events.size() == 2);
        CHECK(std::get_if<CommitObservedStructuredEvent>(&emitter.events[0]) !=
              nullptr);
        const auto *unavailable = std::get_if<
            CommitIdentityUnavailableStructuredEvent>(&emitter.events[1]);
        REQUIRE(unavailable != nullptr);
        CHECK(unavailable->block_hash == block->get_hash());
    }

    SECTION("conflicting retained identities fail closed")
    {
        EventContext event_context;
        TestHotStuff runtime(
            1,
            1,
            bytearray_t{},
            NetAddr("127.0.0.1:0"),
            new ActiveRuntimePaceMaker(1),
            event_context,
            0,
            HotStuffBase::Net::Config(),
            NetAddr(),
            EpochProtocolMode::adaptive_v2);
        const auto configuration = Access::initialize_active_runtime(runtime);
        Access::reset_reporting_outbox(runtime, 4);
        RecordingProtocolEmitter emitter;
        runtime.bind_structured_event_emitters(&emitter, nullptr, nullptr);
        const auto block = indirect_commit_block(runtime, "conflicting-qc-skip");
        const ProposalKey key{configuration, block->get_hash()};
        const ProposalKey conflict{
            ConfigurationId{
                configuration.epoch_number,
                configuration.tree_id,
                digest("conflicting-retained-identity")},
            block->get_hash()};
        Access::seed_view_generation(runtime, key, 81);
        Access::seed_view_generation(runtime, conflict, 82);
        REQUIRE(Access::retain_commit_event_identity(runtime, key, 81));
        CHECK_FALSE(
            Access::retain_commit_event_identity(runtime, conflict, 82));
        REQUIRE(Access::admit_exact_context(runtime, key));
        Access::close_exact_context(runtime, key);

        const auto cached = Access::resolve_and_cache_commit(
            runtime, block, {}, nullptr, true);
        CHECK_FALSE(cached.key.has_value());
        CHECK_FALSE(cached.generation.has_value());
        CHECK(cached.unavailable);
        CHECK_FALSE(cached.conflicted);
        CHECK_FALSE(cached.event_key.has_value());
        CHECK_FALSE(cached.event_generation.has_value());
        CHECK_FALSE(cached.event_unavailable);
        CHECK(cached.event_conflicted);

        Access::report_and_post_commit(runtime, block);
        REQUIRE(emitter.events.size() == 1);
        CHECK(std::get_if<CommitObservedStructuredEvent>(&emitter.events[0]) !=
              nullptr);
        CHECK_FALSE(Access::convergence_evidence_healthy(runtime));
    }
}

TEST_CASE(
    "retained commit event identities preserve proof authority and fail closed",
    "[adaptive-v2][evidence][commit][identity-unavailable][retained][runtime-integration]")
{
    using Access = ExperimentByzantineRuntimeIntegrationTestAccess;
    ScopedSigpipeIgnore ignore_sigpipe;

    SECTION("v3 recovers an authenticated ancestor in a commit batch")
    {
        EventContext event_context;
        TestHotStuff runtime(
            1, 1, bytearray_t{}, NetAddr("127.0.0.1:0"),
            new ActiveRuntimePaceMaker(1), event_context, 0,
            HotStuffBase::Net::Config(), NetAddr(),
            EpochProtocolMode::adaptive_v3,
            adaptive_v3_runtime_config(1));
        const auto configuration = Access::initialize_active_runtime(runtime);
        Access::replace_aggregation_scheduler(
            runtime, std::make_unique<ThrowingAggregationScheduler>());
        RecordingProtocolEmitter emitter;
        runtime.bind_structured_event_emitters(&emitter, nullptr, nullptr);
        const auto block = indirect_commit_block(runtime, "v3-unproven-retained");
        const ProposalKey key{configuration, block->get_hash()};
        REQUIRE(Access::retain_commit_event_identity(runtime, key, 29));

        const auto cached =
            Access::resolve_and_cache_unproven_commit(runtime, block);
        CHECK_FALSE(cached.key.has_value());
        CHECK(cached.conflicted);
        REQUIRE(cached.event_key == key);
        CHECK(cached.event_generation == 29);
        CHECK_FALSE(cached.event_unavailable);
        CHECK_FALSE(cached.event_conflicted);

        Access::report_and_post_commit(runtime, block, 1);
        REQUIRE(emitter.events.size() == 2);
        CHECK(std::get_if<CommitObservedStructuredEvent>(&emitter.events[0]) !=
              nullptr);
        const auto *committed =
            std::get_if<CommitStructuredEvent>(&emitter.events[1]);
        REQUIRE(committed != nullptr);
        CHECK(committed->block_hash == block->get_hash());
        CHECK(committed->decision_proof == key);
        CHECK(committed->view_generation == 29);
        CHECK(committed->commit_batch_index == 1);
    }

    SECTION("a verified direct certifier remains protocol and event exact")
    {
        EventContext event_context;
        TestHotStuff runtime(
            1, 1, bytearray_t{}, NetAddr("127.0.0.1:0"),
            new ActiveRuntimePaceMaker(1), event_context, 0,
            HotStuffBase::Net::Config(), NetAddr(),
            EpochProtocolMode::adaptive_v2);
        const auto configuration = Access::initialize_active_runtime(runtime);
        Access::reset_reporting_outbox(runtime, 4);
        RecordingProtocolEmitter emitter;
        runtime.bind_structured_event_emitters(&emitter, nullptr, nullptr);
        const auto block = indirect_commit_block(runtime, "direct-retained");
        const ProposalKey key{configuration, block->get_hash()};
        Access::seed_view_generation(runtime, key, 19);
        REQUIRE(Access::retain_commit_event_identity(runtime, key, 19));

        const auto cached = Access::resolve_and_cache_commit(
            runtime, block, {}, Access::direct_certifier(runtime, key));
        REQUIRE(cached.key == key);
        CHECK(cached.generation == 19);
        CHECK_FALSE(cached.unavailable);
        CHECK_FALSE(cached.conflicted);
        REQUIRE(cached.event_key == key);
        CHECK(cached.event_generation == 19);
        CHECK_FALSE(cached.event_unavailable);
        CHECK_FALSE(cached.event_conflicted);

        Access::report_and_post_commit(runtime, block);
        REQUIRE(emitter.events.size() == 2);
        CHECK(std::get_if<CommitObservedStructuredEvent>(&emitter.events[0]) !=
              nullptr);
        CHECK(std::get_if<CommitStructuredEvent>(&emitter.events[1]) !=
              nullptr);
    }

    SECTION("non-legal provenance cannot recover retained event identity")
    {
        EventContext event_context;
        TestHotStuff runtime(
            1, 1, bytearray_t{}, NetAddr("127.0.0.1:0"),
            new ActiveRuntimePaceMaker(1), event_context, 0,
            HotStuffBase::Net::Config(), NetAddr(),
            EpochProtocolMode::adaptive_v2);
        const auto configuration = Access::initialize_active_runtime(runtime);
        Access::reset_reporting_outbox(runtime, 4);
        RecordingProtocolEmitter emitter;
        runtime.bind_structured_event_emitters(&emitter, nullptr, nullptr);
        const auto block = indirect_commit_block(runtime, "nonlegal-retained");
        const ProposalKey key{configuration, block->get_hash()};
        REQUIRE(Access::retain_commit_event_identity(runtime, key, 23));

        const auto cached = Access::resolve_and_cache_commit(
            runtime, block, {}, nullptr);
        CHECK_FALSE(cached.key.has_value());
        CHECK_FALSE(cached.generation.has_value());
        CHECK_FALSE(cached.unavailable);
        CHECK(cached.conflicted);
        CHECK_FALSE(cached.event_key.has_value());
        CHECK_FALSE(cached.event_generation.has_value());
        CHECK_FALSE(cached.event_unavailable);
        CHECK(cached.event_conflicted);

        Access::report_and_post_commit(runtime, block);
        REQUIRE(emitter.events.size() == 1);
        CHECK(std::get_if<CommitObservedStructuredEvent>(&emitter.events[0]) !=
              nullptr);
    }

    SECTION("duplicate retention is idempotent and a conflict is permanent")
    {
        EventContext event_context;
        TestHotStuff runtime(
            1, 1, bytearray_t{}, NetAddr("127.0.0.1:0"),
            new ActiveRuntimePaceMaker(1), event_context, 0,
            HotStuffBase::Net::Config(), NetAddr(),
            EpochProtocolMode::adaptive_v2);
        const auto configuration = Access::initialize_active_runtime(runtime);
        const auto block = indirect_commit_block(runtime, "retention-conflict");
        const ProposalKey key{configuration, block->get_hash()};
        const ProposalKey conflict{
            ConfigurationId{configuration.epoch_number, configuration.tree_id,
                            digest("retention-conflict-digest")},
            block->get_hash()};
        REQUIRE(Access::retain_commit_event_identity(runtime, key, 29));
        REQUIRE(Access::retain_commit_event_identity(runtime, key, 29));
        CHECK_FALSE(Access::retain_commit_event_identity(runtime, conflict, 31));
        CHECK_FALSE(Access::retain_commit_event_identity(runtime, key, 29));

        const auto cached = Access::resolve_and_cache_commit(
            runtime, block, {}, nullptr, true);
        CHECK(cached.unavailable);
        CHECK_FALSE(cached.conflicted);
        CHECK_FALSE(cached.event_key.has_value());
        CHECK_FALSE(cached.event_generation.has_value());
        CHECK_FALSE(cached.event_unavailable);
        CHECK(cached.event_conflicted);
    }

    SECTION("retirement, shutdown, and capacity leave no recoverable event identity")
    {
        EventContext event_context;
        TestHotStuff runtime(
            1, 1, bytearray_t{}, NetAddr("127.0.0.1:0"),
            new ActiveRuntimePaceMaker(1), event_context, 0,
            HotStuffBase::Net::Config(), NetAddr(),
            EpochProtocolMode::adaptive_v2);
        const auto configuration = Access::initialize_active_runtime(runtime);
        const auto block = indirect_commit_block(runtime, "retention-retire");
        const ProposalKey key{configuration, block->get_hash()};
        REQUIRE(Access::retain_commit_event_identity(runtime, key, 37));
        Access::forget_retained_commit_event_identities_before_epoch(
            runtime, configuration.epoch_number + 1);
        auto cached = Access::resolve_and_cache_commit(
            runtime, block, {}, nullptr, true);
        CHECK(cached.event_unavailable);

        REQUIRE(Access::retain_commit_event_identity(runtime, key, 37));
        Access::cancel_all_exact_fallbacks(runtime);
        cached = Access::resolve_and_cache_commit(runtime, block, {}, nullptr, true);
        CHECK(cached.event_unavailable);

        for (std::size_t index = 0;
             index < Access::maximum_retained_commit_event_identities();
             ++index)
        {
            const ProposalKey retained{configuration,
                                       digest("retained-capacity-" +
                                              std::to_string(index))};
            REQUIRE(Access::retain_commit_event_identity(
                runtime, retained, index + 1));
        }
        const ProposalKey overflow{configuration, digest("retained-overflow")};
        CHECK_FALSE(Access::retain_commit_event_identity(runtime, overflow, 1));
    }

    SECTION("four legal skips retain an authoritative event chain")
    {
        EventContext event_context;
        TestHotStuff runtime(
            1, 1, bytearray_t{}, NetAddr("127.0.0.1:0"),
            new ActiveRuntimePaceMaker(1), event_context, 0,
            HotStuffBase::Net::Config(), NetAddr(),
            EpochProtocolMode::adaptive_v2);
        const auto configuration = Access::initialize_active_runtime(runtime);
        Access::reset_reporting_outbox(runtime, 8);
        RecordingProtocolEmitter emitter;
        runtime.bind_structured_event_emitters(&emitter, nullptr, nullptr);

        const auto genesis = runtime.get_genesis();
        const auto block1 = Access::add_commit_rule_block(
            runtime, configuration, genesis, genesis, "four-skips-1");
        const auto block2 = Access::add_commit_rule_block(
            runtime, configuration, block1, genesis, "four-skips-2");
        const auto block3 = Access::add_commit_rule_block(
            runtime, configuration, block2, genesis, "four-skips-3");
        const auto block4 = Access::add_commit_rule_block(
            runtime, configuration, block3, genesis, "four-skips-4");
        const std::vector<block_t> blocks{block1, block2, block3, block4};
        for (std::size_t index = 0; index < blocks.size(); ++index)
        {
            const ProposalKey key{configuration, blocks[index]->get_hash()};
            REQUIRE(Access::retain_commit_event_identity(
                runtime, key, 101 + index));
            const auto cached = Access::resolve_and_cache_commit(
                runtime, blocks[index], {}, nullptr, true);
            CHECK(cached.unavailable);
            REQUIRE(cached.event_key == key);
            CHECK(cached.event_generation == 101 + index);
            Access::report_and_post_commit(runtime, blocks[index], index);
        }

        REQUIRE(emitter.events.size() == blocks.size() * 2);
        for (std::size_t index = 0; index < blocks.size(); ++index)
        {
            const auto *observed = std::get_if<CommitObservedStructuredEvent>(
                &emitter.events[index * 2]);
            const auto *committed = std::get_if<CommitStructuredEvent>(
                &emitter.events[index * 2 + 1]);
            REQUIRE(observed != nullptr);
            REQUIRE(committed != nullptr);
            CHECK(committed->block_height == blocks[index]->get_height());
            CHECK(committed->block_hash == blocks[index]->get_hash());
            CHECK(committed->view_generation == 101 + index);
            if (index != 0)
            {
                REQUIRE(committed->parent_hash.has_value());
                CHECK(*committed->parent_hash == blocks[index - 1]->get_hash());
            }
        }
    }
}

TEST_CASE(
    "committed alternate authentication survives synchronous skipped-parent recovery",
    "[adaptive-v2][evidence][commit][identity-unavailable][qc-skip][proposal-bridge][runtime-integration]")
{
    using Access = ExperimentByzantineRuntimeIntegrationTestAccess;

    EventContext event_context;
    RelayRecordingHotStuff runtime(
        1, 1, bytearray_t{}, NetAddr("127.0.0.1:0"),
        new ActiveRuntimePaceMaker(1), event_context, 0,
        HotStuffBase::Net::Config(), NetAddr(),
        EpochProtocolMode::adaptive_v2);
    const bytearray_t issuer_secret(32, 1);
    const PrivKeySecp256k1 issuer_key(issuer_secret);
    runtime.configure_epoch_change_pre_vote_gate(
        EpochChangeIssuer{17, PubKeySecp256k1(issuer_key)},
        EpochChangeDelayBounds{1, 20},
        4U * 1024U * 1024U,
        64);
    const auto configuration = Access::initialize_active_runtime(runtime);
    Access::reset_reporting_outbox(runtime, 4);
    RecordingProtocolEmitter emitter;
    runtime.bind_structured_event_emitters(&emitter, nullptr, nullptr);

    const auto genesis = runtime.get_genesis();
    const auto alternate = Access::add_commit_rule_block(
        runtime,
        configuration,
        genesis,
        genesis,
        "live-order-alternate",
        1,
        3,
        1);
    const auto skipped = Access::add_commit_rule_block(
        runtime,
        configuration,
        alternate,
        genesis,
        "live-order-skipped",
        1000);
    const auto certifier = Access::add_commit_rule_block(
        runtime, configuration, skipped, alternate, "live-order-certifier");
    const ProposalKey alternate_key{configuration, alternate->get_hash()};
    const ProposalKey skipped_key{configuration, skipped->get_hash()};
    constexpr std::uint64_t generation = 77;

    Access::seed_view_generation(runtime, alternate_key, generation, 2);
    REQUIRE(Access::retain_commit_event_identity(
        runtime, alternate_key, generation));
    REQUIRE(Access::admit_exact_context(runtime, alternate_key));
    Access::seed_runtime_initialization(runtime, alternate_key);
    Access::verified_consensus_and_post(
        runtime,
        alternate,
        Access::direct_certifier(runtime, alternate_key));

    // The exact alternate commit consumes both retained event state and the
    // protocol generation index. Only authenticated ingress deliberately
    // preserved through the response-evidence deadline remains available
    // when the next proposal synchronously commits its skipped parent.
    CHECK_FALSE(Access::has_retained_commit_event_identity(
        runtime, alternate->get_hash()));
    CHECK_FALSE(Access::view_generation(runtime, alternate_key).has_value());
    CHECK(Access::has_authenticated_proposal_ingress(
        runtime, alternate_key));

    // Exercise the production remote callback. It authenticates the
    // certifier ingress and retains the evidence bridge before entering
    // on_receive_proposal, so an immediately following commit can consume it.
    const auto admitted = Access::receive_delayed_proposal(
        runtime,
        configuration,
        0,
        certifier,
        generation);
    REQUIRE(admitted.disposition == ProposalDisposition::admitted_active);
    CHECK(runtime.relay_count() == 1);

    REQUIRE(Access::has_authenticated_proposal_ingress(
        runtime, ProposalKey{configuration, certifier->get_hash()}));
    REQUIRE(Access::has_retained_commit_event_identity(
        runtime, skipped_key.block_hash));

    // Evidence-only recovery never populates the protocol/cadence generation
    // index. The commit consumes only the retained event projection.
    CHECK_FALSE(Access::view_generation(runtime, skipped_key).has_value());
    const auto cached = Access::resolve_and_cache_commit(
        runtime, skipped, {}, nullptr, true);
    CHECK(cached.unavailable);
    CHECK_FALSE(cached.conflicted);
    REQUIRE(cached.event_key == skipped_key);
    CHECK(cached.event_generation == generation);
    CHECK_FALSE(cached.event_unavailable);
    CHECK_FALSE(cached.event_conflicted);
    CHECK_FALSE(Access::has_retained_commit_event_identity(
        runtime, skipped_key.block_hash));
    Access::report_and_post_commit(runtime, skipped);
    REQUIRE(emitter.events.size() == 4);
    CHECK(std::get_if<CommitObservedStructuredEvent>(&emitter.events[2]) !=
          nullptr);
    const auto *committed =
        std::get_if<CommitStructuredEvent>(&emitter.events[3]);
    REQUIRE(committed != nullptr);
    CHECK(committed->decision_proof == skipped_key);
    CHECK(committed->view_generation == generation);
    CHECK(committed->transaction_count == 1000);
}

TEST_CASE(
    "proposal evidence retention rolls back only its exact unconsumed mutations after processing throws",
    "[adaptive-v2][evidence][commit][identity-unavailable][qc-skip][proposal-bridge][rollback][runtime-integration]")
{
    using Access = ExperimentByzantineRuntimeIntegrationTestAccess;

    EventContext event_context;
    TestHotStuff runtime(
        1, 1, bytearray_t{}, NetAddr("127.0.0.1:0"),
        new ActiveRuntimePaceMaker(1), event_context, 0,
        HotStuffBase::Net::Config(), NetAddr(),
        EpochProtocolMode::adaptive_v2);
    const auto configuration = Access::initialize_active_runtime(runtime);
    const auto genesis = runtime.get_genesis();
    constexpr std::uint64_t generation = 91;

    const auto make_chain = [&](const std::string &label) {
        const auto alternate = Access::add_commit_rule_block(
            runtime,
            configuration,
            genesis,
            genesis,
            label + "-alternate");
        const auto skipped = Access::add_commit_rule_block(
            runtime,
            configuration,
            alternate,
            genesis,
            label + "-skipped");
        const auto certifier = Access::add_commit_rule_block(
            runtime,
            configuration,
            skipped,
            alternate,
            label + "-certifier");
        return std::array<block_t, 3>{alternate, skipped, certifier};
    };
    const auto authenticate_endpoints = [&](
        const std::array<block_t, 3> &chain) {
        Access::seed_view_generation(
            runtime,
            ProposalKey{configuration, chain[0]->get_hash()},
            generation,
            2);
        Access::seed_view_generation(
            runtime,
            ProposalKey{configuration, chain[2]->get_hash()},
            generation,
            3);
    };
    const auto proposal_for = [&](const block_t &certifier) {
        return Proposal(
            0,
            configuration.epoch_number,
            configuration.tree_id,
            configuration.epoch_digest,
            certifier,
            nullptr);
    };
    const auto rollback_after_processing_exception = [&](
        const Access::RetainedIdentityRollback &rollback) {
        bool caught = false;
        try
        {
            throw std::runtime_error(
                "injected on_receive_proposal failure");
        }
        catch (const std::runtime_error &)
        {
            caught = true;
            Access::rollback_retained_commit_event_identity_mutations(
                runtime, rollback);
        }
        REQUIRE(caught);
    };

    const auto fresh = make_chain("rollback-fresh");
    authenticate_endpoints(fresh);
    const ProposalKey fresh_skipped{
        configuration, fresh[1]->get_hash()};
    const ProposalKey fresh_certifier{
        configuration, fresh[2]->get_hash()};
    Access::RetainedIdentityRollback fresh_rollback;
    REQUIRE(Access::retain_authenticated_proposal_commit_event_identities(
        runtime,
        proposal_for(fresh[2]),
        generation,
        &fresh_rollback));
    REQUIRE(Access::has_retained_commit_event_identity(
        runtime, fresh_skipped.block_hash));
    REQUIRE(Access::has_retained_commit_event_identity(
        runtime, fresh_certifier.block_hash));
    rollback_after_processing_exception(fresh_rollback);
    CHECK_FALSE(Access::has_retained_commit_event_identity(
        runtime, fresh_skipped.block_hash));
    CHECK_FALSE(Access::has_retained_commit_event_identity(
        runtime, fresh_certifier.block_hash));
    CHECK(Access::retained_commit_event_identity_count(runtime) == 0);

    const auto prior = make_chain("rollback-prior");
    authenticate_endpoints(prior);
    const ProposalKey prior_skipped{
        configuration, prior[1]->get_hash()};
    const ProposalKey prior_certifier{
        configuration, prior[2]->get_hash()};
    REQUIRE(Access::retain_commit_event_identity(
        runtime, prior_skipped, generation));
    Access::RetainedIdentityRollback prior_rollback;
    REQUIRE(Access::retain_authenticated_proposal_commit_event_identities(
        runtime,
        proposal_for(prior[2]),
        generation,
        &prior_rollback));
    rollback_after_processing_exception(prior_rollback);
    CHECK(Access::has_retained_commit_event_identity(
        runtime, prior_skipped.block_hash));
    CHECK_FALSE(Access::has_retained_commit_event_identity(
        runtime, prior_certifier.block_hash));
    CHECK(Access::retained_commit_event_identity_count(runtime) == 1);

    const auto consumed = make_chain("rollback-consumed");
    authenticate_endpoints(consumed);
    const ProposalKey consumed_skipped{
        configuration, consumed[1]->get_hash()};
    const ProposalKey consumed_certifier{
        configuration, consumed[2]->get_hash()};
    Access::RetainedIdentityRollback consumed_rollback;
    REQUIRE(Access::retain_authenticated_proposal_commit_event_identities(
        runtime,
        proposal_for(consumed[2]),
        generation,
        &consumed_rollback));
    const auto cached = Access::resolve_and_cache_commit(
        runtime, consumed[1], {}, nullptr, true);
    REQUIRE(cached.event_key == consumed_skipped);
    REQUIRE(cached.event_generation == generation);
    CHECK_FALSE(Access::has_retained_commit_event_identity(
        runtime, consumed_skipped.block_hash));
    rollback_after_processing_exception(consumed_rollback);
    CHECK_FALSE(Access::has_retained_commit_event_identity(
        runtime, consumed_skipped.block_hash));
    CHECK_FALSE(Access::has_retained_commit_event_identity(
        runtime, consumed_certifier.block_hash));
    CHECK(Access::has_retained_commit_event_identity(
        runtime, prior_skipped.block_hash));
    CHECK(Access::retained_commit_event_identity_count(runtime) == 1);

    const auto tombstoned = make_chain("rollback-tombstone");
    authenticate_endpoints(tombstoned);
    const ProposalKey tombstoned_certifier{
        configuration, tombstoned[2]->get_hash()};
    const ProposalKey conflicting_certifier{
        ConfigurationId{
            configuration.epoch_number,
            configuration.tree_id,
            digest("rollback-tombstone-conflict")},
        tombstoned_certifier.block_hash};
    REQUIRE(Access::retain_commit_event_identity(
        runtime, tombstoned_certifier, generation));
    CHECK_FALSE(Access::retain_commit_event_identity(
        runtime, conflicting_certifier, generation + 1));
    REQUIRE(Access::retained_commit_event_identity_is_conflict(
        runtime, tombstoned_certifier.block_hash));

    Access::RetainedIdentityRollback tombstone_rollback;
    CHECK_FALSE(
        Access::retain_authenticated_proposal_commit_event_identities(
            runtime,
            proposal_for(tombstoned[2]),
            generation,
            &tombstone_rollback));
    CHECK(Access::rollback_owned_mutation_count(tombstone_rollback) == 0);
    rollback_after_processing_exception(tombstone_rollback);
    CHECK(Access::retained_commit_event_identity_is_conflict(
        runtime, tombstoned_certifier.block_hash));

    const auto tombstone_cached = Access::resolve_and_cache_commit(
        runtime, tombstoned[2], {}, nullptr, true);
    CHECK_FALSE(tombstone_cached.event_key.has_value());
    CHECK_FALSE(tombstone_cached.event_generation.has_value());
    CHECK(tombstone_cached.event_conflicted);
    CHECK_FALSE(Access::has_retained_commit_event_identity(
        runtime, tombstoned_certifier.block_hash));
    CHECK(Access::retained_commit_event_identity_count(runtime) == 1);

    const auto replaced = make_chain("rollback-replaced");
    authenticate_endpoints(replaced);
    const ProposalKey replaced_skipped{
        configuration, replaced[1]->get_hash()};
    const ProposalKey replaced_certifier{
        configuration, replaced[2]->get_hash()};
    Access::RetainedIdentityRollback replaced_rollback;
    REQUIRE(Access::retain_authenticated_proposal_commit_event_identities(
        runtime,
        proposal_for(replaced[2]),
        generation,
        &replaced_rollback));
    const ProposalKey successor_exact_state{
        ConfigurationId{
            configuration.epoch_number + 1,
            configuration.tree_id,
            digest("rollback-successor-exact-state")},
        replaced_certifier.block_hash};
    Access::replace_retained_commit_event_identity(
        runtime, successor_exact_state, generation + 1);
    rollback_after_processing_exception(replaced_rollback);
    CHECK_FALSE(Access::has_retained_commit_event_identity(
        runtime, replaced_skipped.block_hash));
    CHECK(Access::retained_commit_event_identity_is_exact(
        runtime, successor_exact_state, generation + 1));
    CHECK(Access::has_retained_commit_event_identity(
        runtime, prior_skipped.block_hash));
    CHECK(Access::retained_commit_event_identity_count(runtime) == 2);

    Access::forget_retained_commit_event_identities_before_epoch(
        runtime, configuration.epoch_number + 2);
    CHECK(Access::retained_commit_event_identity_count(runtime) == 0);

    const auto capacity = make_chain("rollback-capacity");
    authenticate_endpoints(capacity);
    const ProposalKey capacity_skipped{
        configuration, capacity[1]->get_hash()};
    const ProposalKey capacity_certifier{
        configuration, capacity[2]->get_hash()};
    for (std::size_t index = 0;
         index + 1 < Access::maximum_retained_commit_event_identities();
         ++index)
    {
        const ProposalKey retained{
            configuration,
            digest("rollback-capacity-retained-" +
                   std::to_string(index))};
        REQUIRE(Access::retain_commit_event_identity(
            runtime, retained, index + 1));
    }
    Access::RetainedIdentityRollback capacity_rollback;
    CHECK_FALSE(
        Access::retain_authenticated_proposal_commit_event_identities(
            runtime,
            proposal_for(capacity[2]),
            generation,
            &capacity_rollback));
    CHECK(Access::has_retained_commit_event_identity(
        runtime, capacity_certifier.block_hash));
    CHECK_FALSE(Access::has_retained_commit_event_identity(
        runtime, capacity_skipped.block_hash));
    CHECK(Access::retained_commit_event_identity_count(runtime) ==
          Access::maximum_retained_commit_event_identities());
    rollback_after_processing_exception(capacity_rollback);
    CHECK_FALSE(Access::has_retained_commit_event_identity(
        runtime, capacity_certifier.block_hash));
    CHECK(Access::retained_commit_event_identity_count(runtime) + 1 ==
          Access::maximum_retained_commit_event_identities());
}

TEST_CASE(
    "proposal QC bridge rejects every unauthenticated or ambiguous inference",
    "[adaptive-v2][evidence][commit][identity-unavailable][qc-skip][proposal-bridge][runtime-integration]")
{
    using Access = ExperimentByzantineRuntimeIntegrationTestAccess;

    EventContext event_context;
    TestHotStuff runtime(
        1, 1, bytearray_t{}, NetAddr("127.0.0.1:0"),
        new ActiveRuntimePaceMaker(1), event_context, 0,
        HotStuffBase::Net::Config(), NetAddr(),
        EpochProtocolMode::adaptive_v2);
    const auto configuration = Access::initialize_active_runtime(runtime);
    const auto genesis = runtime.get_genesis();
    const auto predecessor = Access::add_commit_rule_block(
        runtime, configuration, genesis, genesis, "guard-predecessor");
    const auto alternate = Access::add_commit_rule_block(
        runtime,
        configuration,
        predecessor,
        predecessor,
        "guard-alternate");
    const auto skipped = Access::add_commit_rule_block(
        runtime, configuration, alternate, predecessor, "guard-skipped");
    const ProposalKey predecessor_key{
        configuration, predecessor->get_hash()};
    const ProposalKey alternate_key{configuration, alternate->get_hash()};
    const ProposalKey skipped_key{configuration, skipped->get_hash()};
    constexpr std::uint64_t generation = 77;

    const auto assert_unavailable = [&](
        const Proposal &proposal,
        std::uint64_t proposal_generation) {
        REQUIRE(
            Access::retain_authenticated_proposal_commit_event_identities(
                runtime, proposal, proposal_generation));
        CHECK_FALSE(
            Access::view_generation(runtime, skipped_key).has_value());
        const auto cached = Access::resolve_and_cache_commit(
            runtime, skipped, {}, nullptr, true);
        CHECK(cached.unavailable);
        CHECK_FALSE(cached.event_key.has_value());
        CHECK(cached.event_unavailable);
        CHECK_FALSE(cached.event_conflicted);
    };

    SECTION("a sub-quorum QC cannot authenticate the bridge")
    {
        const auto certifier = Access::add_commit_rule_block(
            runtime,
            configuration,
            skipped,
            alternate,
            "guard-bad-qc",
            1,
            2);
        const ProposalKey certifier_key{
            configuration, certifier->get_hash()};
        Access::seed_view_generation(runtime, alternate_key, generation, 2);
        Access::seed_view_generation(runtime, certifier_key, generation, 3);
        assert_unavailable(
            Proposal(
                0,
                configuration.epoch_number,
                configuration.tree_id,
                configuration.epoch_digest,
                certifier,
                nullptr),
            generation);
    }

    SECTION("a QC key for the wrong block cannot authenticate the bridge")
    {
        const auto certifier = Access::add_custom_commit_rule_block(
            runtime,
            skipped,
            alternate,
            Access::direct_certifier(runtime, skipped_key),
            "guard-wrong-qc-key",
            skipped->get_height() + 1);
        const ProposalKey certifier_key{
            configuration, certifier->get_hash()};
        Access::seed_view_generation(runtime, alternate_key, generation, 2);
        Access::seed_authenticated_proposal_ingress(
            runtime, skipped_key, generation, 2);
        Access::seed_view_generation(runtime, certifier_key, generation, 3);
        assert_unavailable(
            Proposal(
                0,
                configuration.epoch_number,
                configuration.tree_id,
                configuration.epoch_digest,
                certifier,
                nullptr),
            generation);
    }

    SECTION("a non-parent QC reference cannot authenticate the bridge")
    {
        const auto certifier = Access::add_custom_commit_rule_block(
            runtime,
            skipped,
            predecessor,
            Access::direct_certifier(runtime, predecessor_key),
            "guard-wrong-qc-reference",
            skipped->get_height() + 1);
        const ProposalKey certifier_key{
            configuration, certifier->get_hash()};
        Access::seed_view_generation(runtime, predecessor_key, generation, 2);
        Access::seed_view_generation(runtime, certifier_key, generation, 3);
        assert_unavailable(
            Proposal(
                0,
                configuration.epoch_number,
                configuration.tree_id,
                configuration.epoch_digest,
                certifier,
                nullptr),
            generation);
    }

    SECTION("a non-adjacent physical chain cannot authenticate the bridge")
    {
        const auto alternate_height = alternate->get_height();
        CHECK(Access::has_adjacent_proposal_commit_event_bridge_heights(
            alternate_height,
            alternate_height + 1,
            alternate_height + 2));
        CHECK_FALSE(
            Access::has_adjacent_proposal_commit_event_bridge_heights(
                alternate_height,
                alternate_height + 2,
                alternate_height + 3));
        CHECK_FALSE(
            Access::has_adjacent_proposal_commit_event_bridge_heights(
                std::numeric_limits<std::uint32_t>::max(),
                0,
                1));
    }

    SECTION("configuration drift cannot authenticate the bridge")
    {
        const auto certifier = Access::add_commit_rule_block(
            runtime,
            configuration,
            skipped,
            alternate,
            "guard-config-drift");
        const ConfigurationId drifted_configuration{
            configuration.epoch_number + 1,
            configuration.tree_id,
            DataStream("guard-config-drift").get_hash()};
        const Proposal drifted_proposal(
            0,
            drifted_configuration.epoch_number,
            drifted_configuration.tree_id,
            drifted_configuration.epoch_digest,
            certifier,
            nullptr);
        Access::seed_view_generation(runtime, alternate_key, generation, 2);
        Access::seed_view_generation(
            runtime, drifted_proposal.key(), generation, 3);
        assert_unavailable(drifted_proposal, generation);
    }

    SECTION("generation drift cannot authenticate the bridge")
    {
        const auto certifier = Access::add_commit_rule_block(
            runtime,
            configuration,
            skipped,
            alternate,
            "guard-generation-drift");
        const ProposalKey certifier_key{
            configuration, certifier->get_hash()};
        Access::seed_view_generation(runtime, alternate_key, generation - 1, 2);
        Access::seed_view_generation(runtime, certifier_key, generation, 3);
        assert_unavailable(
            Proposal(
                0,
                configuration.epoch_number,
                configuration.tree_id,
                configuration.epoch_digest,
                certifier,
                nullptr),
            generation);
    }

    SECTION("missing alternate authenticated ingress remains unavailable")
    {
        const auto certifier = Access::add_commit_rule_block(
            runtime,
            configuration,
            skipped,
            alternate,
            "guard-missing-alternate-ingress");
        const ProposalKey certifier_key{
            configuration, certifier->get_hash()};
        Access::seed_view_generation(runtime, certifier_key, generation, 3);
        assert_unavailable(
            Proposal(
                0,
                configuration.epoch_number,
                configuration.tree_id,
                configuration.epoch_digest,
                certifier,
                nullptr),
            generation);
    }

    SECTION("missing certifier authenticated ingress remains unavailable")
    {
        const auto certifier = Access::add_commit_rule_block(
            runtime,
            configuration,
            skipped,
            alternate,
            "guard-missing-certifier-ingress");
        Access::seed_view_generation(runtime, alternate_key, generation, 2);
        assert_unavailable(
            Proposal(
                0,
                configuration.epoch_number,
                configuration.tree_id,
                configuration.epoch_digest,
                certifier,
                nullptr),
            generation);
    }

    SECTION("certifier ingress generation drift remains unavailable")
    {
        const auto certifier = Access::add_commit_rule_block(
            runtime,
            configuration,
            skipped,
            alternate,
            "guard-certifier-generation-drift");
        const ProposalKey certifier_key{
            configuration, certifier->get_hash()};
        Access::seed_view_generation(runtime, alternate_key, generation, 2);
        Access::seed_view_generation(
            runtime, certifier_key, generation - 1, 3);
        assert_unavailable(
            Proposal(
                0,
                configuration.epoch_number,
                configuration.tree_id,
                configuration.epoch_digest,
                certifier,
                nullptr),
            generation);
    }
}

TEST_CASE(
    "adaptive-v3 retains the certified successor bridge without alternate ingress",
    "[adaptive-v3][evidence][commit][qc-skip][proposal-bridge][runtime-integration]")
{
    using Access = ExperimentByzantineRuntimeIntegrationTestAccess;
    ScopedSigpipeIgnore ignore_sigpipe;

    EventContext event_context;
    TestHotStuff runtime(
        1, 1, bytearray_t{}, NetAddr("127.0.0.1:0"),
        new ActiveRuntimePaceMaker(1), event_context, 0,
        HotStuffBase::Net::Config(), NetAddr(),
        EpochProtocolMode::adaptive_v3,
        adaptive_v3_runtime_config(1));
    const auto configuration = Access::initialize_active_runtime(runtime);
    const auto generation =
        Access::exact_runtime_generation(runtime, configuration);
    REQUIRE(generation.has_value());

    const auto genesis = runtime.get_genesis();
    const auto alternate = Access::add_commit_rule_block(
        runtime,
        configuration,
        genesis,
        genesis,
        "v3-certified-bridge-alternate");
    const auto skipped = Access::add_commit_rule_block(
        runtime,
        configuration,
        alternate,
        genesis,
        "v3-certified-bridge-skipped");
    const auto certifier = Access::add_commit_rule_block(
        runtime,
        configuration,
        skipped,
        alternate,
        "v3-certified-bridge-certifier");
    const ProposalKey alternate_key{
        configuration, alternate->get_hash()};
    const ProposalKey skipped_key{
        configuration, skipped->get_hash()};
    const ProposalKey certifier_key{
        configuration, certifier->get_hash()};

    // Only the certifier proposal crossed this replica's authenticated
    // ingress. The verified QC itself is the exact source for alternate_key.
    Access::seed_view_generation(
        runtime, certifier_key, *generation, 3);
    Access::RetainedIdentityRollback rollback;
    REQUIRE(Access::retain_authenticated_proposal_commit_event_identities(
        runtime,
        Proposal(
            0,
            configuration.epoch_number,
            configuration.tree_id,
            configuration.epoch_digest,
            certifier,
            nullptr),
        *generation,
        &rollback));
    CHECK(Access::rollback_owned_mutation_count(rollback) == 3);
    CHECK(Access::has_retained_commit_event_identity(
        runtime, alternate_key.block_hash));
    CHECK(Access::has_retained_commit_event_identity(
        runtime, skipped_key.block_hash));
    CHECK(Access::has_retained_commit_event_identity(
        runtime, certifier_key.block_hash));

    RecordingProtocolEmitter emitter;
    runtime.bind_structured_event_emitters(&emitter, nullptr, nullptr);
    const auto alternate_cached =
        Access::resolve_and_cache_unproven_commit(runtime, alternate);
    CHECK(alternate_cached.conflicted);
    CHECK(alternate_cached.event_key == alternate_key);
    CHECK(alternate_cached.event_generation == generation);
    CHECK_FALSE(alternate_cached.event_conflicted);
    Access::report_and_post_commit(runtime, alternate, 0);

    const auto skipped_cached = Access::resolve_and_cache_commit(
        runtime, skipped, {}, nullptr, true);
    CHECK(skipped_cached.unavailable);
    CHECK(skipped_cached.event_key == skipped_key);
    CHECK(skipped_cached.event_generation == generation);
    CHECK_FALSE(skipped_cached.event_unavailable);
    CHECK_FALSE(skipped_cached.event_conflicted);
    Access::report_and_post_commit(runtime, skipped, 1);

    REQUIRE(emitter.events.size() == 4);
    const auto *alternate_event =
        std::get_if<CommitStructuredEvent>(&emitter.events[1]);
    const auto *skipped_event =
        std::get_if<CommitStructuredEvent>(&emitter.events[3]);
    REQUIRE(alternate_event != nullptr);
    REQUIRE(skipped_event != nullptr);
    CHECK(alternate_event->decision_proof == alternate_key);
    CHECK(alternate_event->view_generation == generation);
    CHECK(skipped_event->decision_proof == skipped_key);
    CHECK(skipped_event->view_generation == generation);

    const auto bounded_alternate = Access::add_commit_rule_block(
        runtime,
        configuration,
        certifier,
        certifier,
        "v3-bounded-bridge-alternate");
    const auto bounded_first = Access::add_commit_rule_block(
        runtime,
        configuration,
        bounded_alternate,
        certifier,
        "v3-bounded-bridge-first");
    const auto bounded_second = Access::add_commit_rule_block(
        runtime,
        configuration,
        bounded_first,
        certifier,
        "v3-bounded-bridge-second");
    const auto bounded_certifier = Access::add_commit_rule_block(
        runtime,
        configuration,
        bounded_second,
        bounded_alternate,
        "v3-bounded-bridge-certifier");
    const ProposalKey bounded_alternate_key{
        configuration, bounded_alternate->get_hash()};
    const ProposalKey bounded_first_key{
        configuration, bounded_first->get_hash()};
    const ProposalKey bounded_second_key{
        configuration, bounded_second->get_hash()};
    const ProposalKey bounded_certifier_key{
        configuration, bounded_certifier->get_hash()};
    Access::seed_view_generation(
        runtime, bounded_certifier_key, *generation, 3);
    REQUIRE(Access::retain_authenticated_proposal_commit_event_identities(
        runtime,
        Proposal(
            0,
            configuration.epoch_number,
            configuration.tree_id,
            configuration.epoch_digest,
            bounded_certifier,
            nullptr),
        *generation));
    CHECK(Access::has_retained_commit_event_identity(
        runtime, bounded_certifier_key.block_hash));
    CHECK(Access::has_retained_commit_event_identity(
        runtime, bounded_alternate_key.block_hash));
    CHECK(Access::has_retained_commit_event_identity(
        runtime, bounded_first_key.block_hash));
    CHECK(Access::has_retained_commit_event_identity(
        runtime, bounded_second_key.block_hash));

    const auto bounded_first_cached =
        Access::resolve_and_cache_unproven_commit(runtime, bounded_first);
    CHECK(bounded_first_cached.conflicted);
    CHECK(bounded_first_cached.event_key == bounded_first_key);
    CHECK(bounded_first_cached.event_generation == generation);
    CHECK_FALSE(bounded_first_cached.event_conflicted);

    const auto bounded_second_cached = Access::resolve_and_cache_commit(
        runtime, bounded_second, {}, nullptr, true);
    CHECK(bounded_second_cached.unavailable);
    CHECK(bounded_second_cached.event_key == bounded_second_key);
    CHECK(bounded_second_cached.event_generation == generation);
    CHECK_FALSE(bounded_second_cached.event_unavailable);
    CHECK_FALSE(bounded_second_cached.event_conflicted);

    const auto local_alternate = Access::add_commit_rule_block(
        runtime,
        configuration,
        certifier,
        certifier,
        "v3-local-certified-bridge-alternate");
    const auto local_skipped = Access::add_commit_rule_block(
        runtime,
        configuration,
        local_alternate,
        certifier,
        "v3-local-certified-bridge-skipped");
    const auto local_certifier = Access::add_commit_rule_block(
        runtime,
        configuration,
        local_skipped,
        local_alternate,
        "v3-local-certified-bridge-certifier");
    const ProposalKey local_alternate_key{
        configuration, local_alternate->get_hash()};
    const ProposalKey local_skipped_key{
        configuration, local_skipped->get_hash()};
    const ProposalKey local_certifier_key{
        configuration, local_certifier->get_hash()};
    REQUIRE(Access::retain_authenticated_proposal_commit_event_identities(
        runtime,
        Proposal(
            0,
            configuration.epoch_number,
            configuration.tree_id,
            configuration.epoch_digest,
            local_certifier,
            nullptr),
        *generation,
        nullptr,
        true));
    CHECK(Access::has_retained_commit_event_identity(
        runtime, local_alternate_key.block_hash));
    CHECK(Access::has_retained_commit_event_identity(
        runtime, local_skipped_key.block_hash));
    CHECK(Access::has_retained_commit_event_identity(
        runtime, local_certifier_key.block_hash));

    const auto drift_alternate = Access::add_commit_rule_block(
        runtime,
        configuration,
        certifier,
        certifier,
        "v3-certified-bridge-drift-alternate");
    const auto drift_skipped = Access::add_commit_rule_block(
        runtime,
        configuration,
        drift_alternate,
        certifier,
        "v3-certified-bridge-drift-skipped");
    const auto drift_certifier = Access::add_commit_rule_block(
        runtime,
        configuration,
        drift_skipped,
        drift_alternate,
        "v3-certified-bridge-drift-certifier");
    const ProposalKey drift_alternate_key{
        configuration, drift_alternate->get_hash()};
    const ProposalKey drift_skipped_key{
        configuration, drift_skipped->get_hash()};
    const ProposalKey drift_certifier_key{
        configuration, drift_certifier->get_hash()};
    REQUIRE(*generation != std::numeric_limits<std::uint64_t>::max());
    const auto drifted_generation = *generation + 1;
    Access::seed_view_generation(
        runtime, drift_certifier_key, drifted_generation, 3);
    REQUIRE(Access::retain_authenticated_proposal_commit_event_identities(
        runtime,
        Proposal(
            0,
            configuration.epoch_number,
            configuration.tree_id,
            configuration.epoch_digest,
            drift_certifier,
            nullptr),
        drifted_generation));
    CHECK_FALSE(Access::has_retained_commit_event_identity(
        runtime, drift_alternate_key.block_hash));
    CHECK_FALSE(Access::has_retained_commit_event_identity(
        runtime, drift_skipped_key.block_hash));
}

TEST_CASE(
    "adaptive-v3 bridge preserves predecessor configuration at activation",
    "[adaptive-v3][evidence][commit][qc-skip][activation-boundary]")
{
    using Access = ExperimentByzantineRuntimeIntegrationTestAccess;
    const ConfigurationId predecessor{
        7, 4, digest("v3-certified-bridge-predecessor")};
    const ConfigurationId successor{
        8, 0, digest("v3-certified-bridge-successor")};
    constexpr std::uint64_t predecessor_generation = 125;
    constexpr std::uint64_t successor_generation = 4'294'967'297ULL;

    const auto without_predecessor_ingress =
        Access::authenticated_proposal_commit_event_bridge_configuration(
            EpochProtocolMode::adaptive_v3,
            predecessor,
            successor,
            successor_generation,
            predecessor_generation,
            successor_generation,
            std::nullopt,
            successor_generation);
    REQUIRE(without_predecessor_ingress.has_value());
    CHECK(without_predecessor_ingress->first == predecessor);
    CHECK(without_predecessor_ingress->second == predecessor_generation);

    const auto with_predecessor_ingress =
        Access::authenticated_proposal_commit_event_bridge_configuration(
            EpochProtocolMode::adaptive_v3,
            predecessor,
            successor,
            successor_generation,
            predecessor_generation,
            successor_generation,
            predecessor_generation,
            successor_generation);
    REQUIRE(with_predecessor_ingress.has_value());
    CHECK(with_predecessor_ingress->first == predecessor);
    CHECK(with_predecessor_ingress->second == predecessor_generation);

    CHECK(
        Access::has_bounded_proposal_commit_event_bridge_intermediates(
            EpochProtocolMode::adaptive_v3,
            predecessor,
            successor,
            1));
    CHECK(
        Access::has_bounded_proposal_commit_event_bridge_intermediates(
            EpochProtocolMode::adaptive_v3,
            predecessor,
            successor,
            2));
    CHECK_FALSE(
        Access::has_bounded_proposal_commit_event_bridge_intermediates(
            EpochProtocolMode::adaptive_v2,
            predecessor,
            successor,
            2));
    CHECK(
        Access::has_bounded_proposal_commit_event_bridge_intermediates(
            EpochProtocolMode::adaptive_v3,
            predecessor,
            predecessor,
            2));
    CHECK_FALSE(
        Access::has_bounded_proposal_commit_event_bridge_intermediates(
            EpochProtocolMode::adaptive_v3,
            predecessor,
            successor,
            0));
    CHECK_FALSE(
        Access::has_bounded_proposal_commit_event_bridge_intermediates(
            EpochProtocolMode::adaptive_v3,
            predecessor,
            successor,
            3));

    CHECK_FALSE(
        Access::authenticated_proposal_commit_event_bridge_configuration(
            EpochProtocolMode::adaptive_v2,
            predecessor,
            successor,
            successor_generation,
            predecessor_generation,
            successor_generation,
            predecessor_generation,
            successor_generation)
            .has_value());
    CHECK_FALSE(
        Access::authenticated_proposal_commit_event_bridge_configuration(
            EpochProtocolMode::adaptive_v3,
            predecessor,
            successor,
            successor_generation,
            std::nullopt,
            successor_generation,
            std::nullopt,
            successor_generation)
            .has_value());
    CHECK_FALSE(
        Access::authenticated_proposal_commit_event_bridge_configuration(
            EpochProtocolMode::adaptive_v3,
            predecessor,
            successor,
            successor_generation,
            predecessor_generation,
            successor_generation,
            predecessor_generation + 1,
            successor_generation)
            .has_value());
    CHECK_FALSE(
        Access::authenticated_proposal_commit_event_bridge_configuration(
            EpochProtocolMode::adaptive_v3,
            predecessor,
            successor,
            successor_generation,
            predecessor_generation,
            successor_generation,
            predecessor_generation,
            std::nullopt)
            .has_value());

    const ConfigurationId noncanonical_successor{
        successor.epoch_number, 1, successor.epoch_digest};
    CHECK_FALSE(
        Access::authenticated_proposal_commit_event_bridge_configuration(
            EpochProtocolMode::adaptive_v3,
            predecessor,
            noncanonical_successor,
            successor_generation,
            predecessor_generation,
            successor_generation,
            predecessor_generation,
            successor_generation)
            .has_value());

    ScopedSigpipeIgnore sigpipe_ignore;
    EventContext event_context;
    TestHotStuff runtime(
        1,
        1,
        bytearray_t{},
        NetAddr("127.0.0.1:0"),
        new ActiveRuntimePaceMaker(1),
        event_context,
        0,
        HotStuffBase::Net::Config(),
        NetAddr(),
        EpochProtocolMode::adaptive_v3,
        adaptive_v3_runtime_config(1));
    const ProposalKey predecessor_intermediate{
        predecessor, digest("v3-boundary-retained-intermediate")};
    REQUIRE(Access::retain_commit_event_identity_through_epoch(
        runtime,
        predecessor_intermediate,
        predecessor_generation,
        successor.epoch_number));
    CHECK(
        Access::retained_commit_event_identity_last_live_epoch(
            runtime, predecessor_intermediate.block_hash) ==
        successor.epoch_number);
    Access::forget_retained_commit_event_identities_before_epoch(
        runtime, successor.epoch_number);
    CHECK(Access::has_retained_commit_event_identity(
        runtime, predecessor_intermediate.block_hash));
    Access::forget_retained_commit_event_identities_before_epoch(
        runtime, successor.epoch_number + 1);
    CHECK_FALSE(Access::has_retained_commit_event_identity(
        runtime, predecessor_intermediate.block_hash));
    const ProposalKey overextended_intermediate{
        predecessor, digest("v3-boundary-overextended-intermediate")};
    CHECK_FALSE(Access::retain_commit_event_identity_through_epoch(
        runtime,
        overextended_intermediate,
        predecessor_generation,
        successor.epoch_number + 1));
    CHECK_FALSE(Access::has_retained_commit_event_identity(
        runtime, overextended_intermediate.block_hash));
}

TEST_CASE(
    "unavailable and conflicting commit identities retain fail-closed convergence boundaries",
    "[adaptive-v2][evidence][commit][identity-unavailable][fail-closed][runtime-integration]")
{
    EventContext event_context;
    TestHotStuff runtime(
        1,
        1,
        bytearray_t{},
        NetAddr("127.0.0.1:0"),
        new ActiveRuntimePaceMaker(1),
        event_context,
        0,
        HotStuffBase::Net::Config(),
        NetAddr(),
        EpochProtocolMode::adaptive_v2);
    using Access = ExperimentByzantineRuntimeIntegrationTestAccess;

    const auto configuration = Access::initialize_active_runtime(runtime);
    RecordingProtocolEmitter emitter;
    runtime.bind_structured_event_emitters(&emitter, nullptr, nullptr);

    SECTION(
        "a legal unavailable QC-skipped commit preserves a pending exact "
        "identity for the matching activation")
    {
        auto &outbox = Access::reset_reporting_outbox(runtime, 1);
        const auto block =
            indirect_commit_block(runtime, "pending-convergence-gap");
        const auto cached = Access::resolve_and_cache_commit(
            runtime, block, {}, nullptr, true);
        REQUIRE(cached.unavailable);
        const auto identity = Access::seed_convergence_identity(
            runtime, configuration);

        Access::report_and_post_commit(runtime, block);

        CHECK(Access::convergence_evidence_healthy(runtime));
        CHECK(Access::has_convergence_identity(runtime, identity));
        REQUIRE(emitter.events.size() == 2);
        CHECK(std::get_if<CommitObservedStructuredEvent>(
                  &emitter.events.front()) != nullptr);
        CHECK(std::get_if<CommitIdentityUnavailableStructuredEvent>(
                  &emitter.events.back()) != nullptr);

        // The outbox has capacity for one record only.  The matching live
        // activation first retains its exact identity behind the commit
        // report, then succeeds when the durable queue is retried.
        Access::enqueue_matching_activation(runtime, identity);
        REQUIRE(outbox.front() != nullptr);
        CHECK(outbox.front()->stream == AdaptiveV2ReportingStream::convergence);
        const auto committed =
            decode_adaptive_v2_epoch_change_committed_observation(
                outbox.front()->canonical_payload,
                AdaptiveV2ConvergenceWireLimits{});
        REQUIRE(committed);
        CHECK(committed.observation->identity == identity);
        acknowledge_and_release_convergence(outbox, 1);

        Access::enqueue_matching_activation(runtime, identity);
        REQUIRE(outbox.front() != nullptr);
        CHECK(outbox.front()->stream == AdaptiveV2ReportingStream::convergence);
        const auto activated =
            decode_adaptive_v2_epoch_activated_observation(
                outbox.front()->canonical_payload,
                AdaptiveV2ConvergenceWireLimits{});
        REQUIRE(activated);
        CHECK(activated.observation->identity == identity);
        CHECK(activated.observation->activated_epoch_number ==
              identity.successor_epoch_number);
        CHECK(activated.observation->activated_epoch_digest ==
              identity.successor_epoch_digest);
        CHECK(Access::convergence_evidence_healthy(runtime));
    }

    SECTION("an exact key without authenticated generation remains fatal")
    {
        Access::reset_reporting_outbox(runtime);
        const auto block =
            indirect_commit_block(runtime, "missing-generation-gap");
        const ProposalKey key{configuration, block->get_hash()};
        const auto cached = Access::resolve_and_cache_commit(
            runtime, block, {key}, nullptr);
        REQUIRE(cached.conflicted);

        Access::report_and_post_commit(runtime, block);

        CHECK_FALSE(Access::convergence_evidence_healthy(runtime));
        REQUIRE(emitter.events.size() == 1);
        CHECK(std::get_if<CommitObservedStructuredEvent>(
                  &emitter.events[0]) != nullptr);
    }

    SECTION("an unavailable commit at the exact epoch-command hash is fatal")
    {
        Access::reset_reporting_outbox(runtime);
        const auto block =
            indirect_commit_block(runtime, "convergence-command-hash-gap");
        const auto cached = Access::resolve_and_cache_commit(
            runtime, block, {}, nullptr, true);
        REQUIRE(cached.unavailable);
        const auto identity = Access::seed_convergence_identity(
            runtime, configuration, 1, 41, 5, block->get_hash());

        Access::report_and_post_commit(runtime, block);

        CHECK_FALSE(Access::convergence_evidence_healthy(runtime));
        CHECK_FALSE(Access::has_convergence_identity(runtime, identity));
    }

    SECTION("an unavailable second pending epoch command is fatal")
    {
        Access::reset_reporting_outbox(runtime);
        const auto block =
            indirect_commit_block(runtime, "second-pending-command-gap");
        const auto cached = Access::resolve_and_cache_commit(
            runtime, block, {}, nullptr, true);
        REQUIRE(cached.unavailable);
        const auto identity = Access::seed_convergence_identity(
            runtime, configuration);
        Access::seed_pending_epoch_command(runtime, block->get_hash());

        Access::report_and_post_commit(runtime, block);

        CHECK_FALSE(Access::convergence_evidence_healthy(runtime));
        CHECK_FALSE(Access::has_convergence_identity(runtime, identity));
    }

    SECTION("a legal unavailable commit cannot heal pre-poisoned evidence")
    {
        Access::reset_reporting_outbox(runtime);
        const auto block =
            indirect_commit_block(runtime, "pre-poisoned-unavailable-gap");
        const auto cached = Access::resolve_and_cache_commit(
            runtime, block, {}, nullptr, true);
        REQUIRE(cached.unavailable);
        const auto identity = Access::seed_convergence_identity(
            runtime, configuration);
        Access::poison_convergence_evidence(runtime);

        Access::report_and_post_commit(runtime, block);

        CHECK_FALSE(Access::convergence_evidence_healthy(runtime));
        CHECK_FALSE(Access::has_any_convergence_identity(runtime));
        CHECK_FALSE(Access::has_convergence_identity(runtime, identity));
    }

    SECTION("repeat distinct legal unavailable commits preserve the identity")
    {
        Access::reset_reporting_outbox(runtime);
        const auto identity = Access::seed_convergence_identity(
            runtime, configuration);
        const auto first =
            indirect_commit_block(runtime, "first-distinct-unavailable-gap");
        const auto second =
            indirect_commit_block(runtime, "second-distinct-unavailable-gap");
        REQUIRE(first->get_hash() != second->get_hash());

        for (const auto &block : {first, second})
        {
            const auto cached = Access::resolve_and_cache_commit(
                runtime, block, {}, nullptr, true);
            REQUIRE(cached.unavailable);
            Access::report_and_post_commit(runtime, block);
            CHECK(Access::convergence_evidence_healthy(runtime));
            CHECK(Access::has_convergence_identity(runtime, identity));
        }
    }

    SECTION("the one-argument compatibility path cannot claim a legal QC skip")
    {
        Access::reset_reporting_outbox(runtime);
        const auto block =
            indirect_commit_block(runtime, "compatibility-unknown-gap");

        Access::compatibility_consensus_and_post(runtime, block);

        CHECK_FALSE(Access::convergence_evidence_healthy(runtime));
        REQUIRE(emitter.events.size() == 1);
        CHECK(std::get_if<CommitObservedStructuredEvent>(
                  &emitter.events.front()) != nullptr);
    }

    SECTION("conflicting exact sources remain fatal outside convergence")
    {
        Access::reset_reporting_outbox(runtime);
        const auto block = indirect_commit_block(runtime, "identity-conflict");
        const ProposalKey proof_key{configuration, block->get_hash()};
        const ProposalKey drifted_key{
            ConfigurationId{
                configuration.epoch_number,
                configuration.tree_id,
                digest("identity-conflict-drift")},
            block->get_hash()};
        const auto proof = Access::direct_certifier(runtime, proof_key);
        const auto cached = Access::resolve_and_cache_commit(
            runtime, block, {drifted_key}, proof);
        REQUIRE(cached.conflicted);

        Access::report_and_post_commit(runtime, block);

        CHECK_FALSE(Access::convergence_evidence_healthy(runtime));
        REQUIRE(emitter.events.size() == 1);
        CHECK(std::get_if<CommitObservedStructuredEvent>(
                  &emitter.events.front()) != nullptr);
    }

    runtime.bind_structured_event_emitters(nullptr, nullptr, nullptr);
}

TEST_CASE(
    "an unverified alternate certifier poisons evidence without emitting a gap event",
    "[adaptive-v2][evidence][commit][identity-unavailable][qc-skip][negative][runtime-integration]")
{
    EventContext event_context;
    TestHotStuff runtime(
        1,
        1,
        bytearray_t{},
        NetAddr("127.0.0.1:0"),
        new ActiveRuntimePaceMaker(1),
        event_context,
        0,
        HotStuffBase::Net::Config(),
        NetAddr(),
        EpochProtocolMode::adaptive_v2);
    using Access = ExperimentByzantineRuntimeIntegrationTestAccess;

    const auto configuration = Access::initialize_active_runtime(runtime);
    Access::reset_reporting_outbox(runtime, 16);
    RecordingProtocolEmitter emitter;
    runtime.bind_structured_event_emitters(&emitter, nullptr, nullptr);

    const auto genesis = runtime.get_genesis();
    const auto block1 = Access::add_commit_rule_block(
        runtime, configuration, genesis, genesis, "bad-skip-1");
    const auto block2 = Access::add_commit_rule_block(
        runtime, configuration, block1, block1, "bad-skip-2");
    const auto block3 = Access::add_commit_rule_block(
        runtime, configuration, block2, block1, "bad-skip-3");
    const auto block4 = Access::add_commit_rule_block(
        runtime, configuration, block3, block1, "bad-skip-4");
    const auto block5 = Access::add_commit_rule_block(
        runtime, configuration, block4, block3, "bad-skip-5");
    const auto block6 = Access::add_commit_rule_block(
        runtime, configuration, block5, block3, "bad-skip-6");
    const auto block7 = Access::add_commit_rule_block(
        runtime, configuration, block6, block5, "bad-skip-7");
    const auto block8 = Access::add_commit_rule_block(
        runtime, configuration, block7, block7, "bad-skip-8");
    Access::replace_commit_rule_certificate(
        block3,
        Access::invalid_direct_certifier(
            ProposalKey{configuration, block1->get_hash()}));
    REQUIRE(block3->get_qc()->has_n(runtime.get_config().nmajority));
    REQUIRE_FALSE(block3->get_qc()->verify(runtime.get_config()));
    Access::seed_runtime_initialization(
        runtime, ProposalKey{configuration, block1->get_hash()});
    Access::seed_runtime_initialization(
        runtime, ProposalKey{configuration, block3->get_hash()});

    REQUIRE_NOTHROW(Access::apply_update(runtime, block8));
    CHECK_FALSE(Access::convergence_evidence_healthy(runtime));
    CHECK(block2->get_decision() == 1);

    std::size_t observed = 0;
    std::size_t unavailable = 0;
    std::size_t committed = 0;
    for (const auto &event : emitter.events)
    {
        if (const auto *value =
                std::get_if<CommitObservedStructuredEvent>(&event);
            value != nullptr && value->block_hash == block2->get_hash())
            ++observed;
        if (const auto *value =
                std::get_if<CommitIdentityUnavailableStructuredEvent>(
                    &event);
            value != nullptr && value->block_hash == block2->get_hash())
            ++unavailable;
        if (const auto *value =
                std::get_if<CommitStructuredEvent>(&event);
            value != nullptr && value->block_hash == block2->get_hash())
            ++committed;
    }
    CHECK(observed == 1);
    CHECK(unavailable == 0);
    CHECK(committed == 0);
    runtime.bind_structured_event_emitters(nullptr, nullptr, nullptr);
}

TEST_CASE(
    "verified direct certifier recovers an unobserved indirect commit identity",
    "[adaptive-v2][evidence][commit][indirect][certifier][runtime-integration]")
{
    EventContext event_context;
    TestHotStuff runtime(
        1,
        1,
        bytearray_t{},
        NetAddr("127.0.0.1:0"),
        new ActiveRuntimePaceMaker(1),
        event_context,
        0,
        HotStuffBase::Net::Config(),
        NetAddr(),
        EpochProtocolMode::adaptive_v2);
    using Access = ExperimentByzantineRuntimeIntegrationTestAccess;

    const auto configuration =
        Access::initialize_active_runtime(runtime);
    const auto block = indirect_commit_block(runtime, "indirect-recovery");
    const ProposalKey key{configuration, block->get_hash()};
    const auto certifier = Access::direct_certifier(runtime, key);

    REQUIRE(Access::view_generation(runtime, key) == std::nullopt);
    const auto resolved = Access::resolve_committed_key(
        runtime, block, {}, certifier);

    REQUIRE(resolved.has_value());
    CHECK(*resolved == key);
    const auto cached = Access::resolve_and_cache_commit(
        runtime, block, {}, certifier);
    REQUIRE(cached.key == key);
    const auto expected_generation = checked_activation_generation(0, 0);
    REQUIRE(expected_generation.has_value());
    CHECK(cached.generation == expected_generation);
    CHECK(Access::view_generation(runtime, key) == expected_generation);
}

TEST_CASE(
    "indirect commit identity sources and certifier proof fail closed",
    "[adaptive-v2][evidence][commit][indirect][certifier][negative][runtime-integration]")
{
    EventContext event_context;
    TestHotStuff runtime(
        1,
        1,
        bytearray_t{},
        NetAddr("127.0.0.1:0"),
        new ActiveRuntimePaceMaker(1),
        event_context,
        0,
        HotStuffBase::Net::Config(),
        NetAddr(),
        EpochProtocolMode::adaptive_v2);
    using Access = ExperimentByzantineRuntimeIntegrationTestAccess;

    const auto configuration =
        Access::initialize_active_runtime(runtime);
    const auto block = indirect_commit_block(runtime, "indirect-negative");
    const ProposalKey key{configuration, block->get_hash()};
    const auto valid = Access::direct_certifier(runtime, key);

    SECTION("an under-quorum proof is not authoritative")
    {
        const auto under_quorum =
            Access::direct_certifier(runtime, key, 2);
        CHECK_FALSE(Access::resolve_committed_key(
            runtime, block, {key}, under_quorum).has_value());
    }

    SECTION("a quorum-sized certificate with invalid authentication fails")
    {
        const auto invalid =
            Access::invalid_direct_certifier(key);
        REQUIRE(invalid->has_n(runtime.get_config().nmajority));
        REQUIRE_FALSE(invalid->verify(runtime.get_config()));
        CHECK_FALSE(Access::resolve_committed_key(
            runtime, block, {key}, invalid).has_value());
    }

    SECTION("a proof replayed onto another block is rejected")
    {
        const auto other = indirect_commit_block(runtime, "proof-replay");
        const ProposalKey other_key{
            configuration, other->get_hash()};
        CHECK_FALSE(Access::resolve_committed_key(
            runtime, other, {other_key}, valid).has_value());
    }

    SECTION("a closed context cannot disagree with the proof")
    {
        const ProposalKey drifted{
            ConfigurationId{
                configuration.epoch_number,
                configuration.tree_id,
                digest("drifted-commit-configuration")},
            block->get_hash()};
        CHECK_FALSE(Access::resolve_committed_key(
            runtime, block, {drifted}, valid).has_value());
    }

    SECTION("a self-QC cannot disagree with the verified proof")
    {
        const auto prototype =
            indirect_commit_block(runtime, "self-qc-conflict");
        const ProposalKey self_key{
            ConfigurationId{
                configuration.epoch_number,
                configuration.tree_id,
                digest("self-qc-drifted-configuration")},
            prototype->get_hash()};
        auto self_certificate =
            Access::direct_certifier(runtime, self_key);
        const block_t conflicted = new Block(
            std::vector<block_t>{runtime.get_genesis()},
            std::vector<uint256_t>{digest("self-qc-conflict")},
            Access::genesis_parent_certificate(runtime),
            bytearray_t{},
            1,
            runtime.get_genesis(),
            std::move(self_certificate));
        REQUIRE(conflicted->get_hash() == prototype->get_hash());
        const ProposalKey proof_key{
            configuration, conflicted->get_hash()};
        const auto proof =
            Access::direct_certifier(runtime, proof_key);
        CHECK_FALSE(Access::resolve_committed_key(
            runtime, conflicted, {}, proof).has_value());
    }

    SECTION("a self-QC cannot disagree with a closed context")
    {
        const auto prototype =
            indirect_commit_block(runtime, "self-context-conflict");
        const ProposalKey self_key{
            configuration, prototype->get_hash()};
        auto self_certificate =
            Access::direct_certifier(runtime, self_key);
        const block_t conflicted = new Block(
            std::vector<block_t>{runtime.get_genesis()},
            std::vector<uint256_t>{digest("self-context-conflict")},
            Access::genesis_parent_certificate(runtime),
            bytearray_t{},
            1,
            runtime.get_genesis(),
            std::move(self_certificate));
        REQUIRE(conflicted->get_hash() == prototype->get_hash());
        const ProposalKey closed_key{
            ConfigurationId{
                configuration.epoch_number,
                configuration.tree_id,
                digest("closed-context-drifted-configuration")},
            conflicted->get_hash()};
        CHECK_FALSE(Access::resolve_committed_key(
            runtime, conflicted, {closed_key}, nullptr).has_value());
    }

    SECTION("all three exact identity sources agree")
    {
        const auto prototype =
            indirect_commit_block(runtime, "all-sources-agree");
        const ProposalKey exact_key{
            configuration, prototype->get_hash()};
        auto self_certificate =
            Access::direct_certifier(runtime, exact_key);
        const block_t agreed = new Block(
            std::vector<block_t>{runtime.get_genesis()},
            std::vector<uint256_t>{digest("all-sources-agree")},
            Access::genesis_parent_certificate(runtime),
            bytearray_t{},
            1,
            runtime.get_genesis(),
            std::move(self_certificate));
        REQUIRE(agreed->get_hash() == prototype->get_hash());
        const auto proof =
            Access::direct_certifier(runtime, exact_key);
        CHECK(Access::resolve_committed_key(
            runtime, agreed, {exact_key}, proof) == exact_key);
    }

    SECTION("two closed identities for one block remain ambiguous")
    {
        const ProposalKey drifted{
            ConfigurationId{
                configuration.epoch_number,
                configuration.tree_id,
                digest("ambiguous-commit-configuration")},
            block->get_hash()};
        CHECK_FALSE(Access::resolve_committed_key(
            runtime, block, {key, drifted}, nullptr).has_value());
    }

    SECTION("missing proof and context retain no inferred identity")
    {
        CHECK_FALSE(Access::resolve_committed_key(
            runtime, block, {}, nullptr).has_value());
    }

    SECTION("one exact closed context remains a valid legacy source")
    {
        CHECK(
            Access::resolve_committed_key(
                runtime, block, {key}, nullptr) == key);
    }
}

TEST_CASE(
    "certifier recovery preserves authenticated generations and tombstones",
    "[adaptive-v2][evidence][commit][indirect][generation][runtime-integration]")
{
    EventContext event_context;
    TestHotStuff runtime(
        1,
        1,
        bytearray_t{},
        NetAddr("127.0.0.1:0"),
        new ActiveRuntimePaceMaker(1),
        event_context,
        0,
        HotStuffBase::Net::Config(),
        NetAddr(),
        EpochProtocolMode::adaptive_v2);
    using Access = ExperimentByzantineRuntimeIntegrationTestAccess;

    const auto configuration =
        Access::initialize_active_runtime(runtime);

    const auto observed_block =
        indirect_commit_block(runtime, "observed-generation");
    const ProposalKey observed_key{
        configuration, observed_block->get_hash()};
    Access::seed_view_generation(runtime, observed_key, 77);
    const auto observed_proof =
        Access::direct_certifier(runtime, observed_key);
    const auto observed = Access::resolve_and_cache_commit(
        runtime, observed_block, {}, observed_proof);
    CHECK(observed.key == observed_key);
    CHECK(observed.generation == 77);
    CHECK(Access::view_generation(runtime, observed_key) == 77);

    const auto tombstoned_block =
        indirect_commit_block(runtime, "tombstoned-generation");
    const ProposalKey tombstoned_key{
        configuration, tombstoned_block->get_hash()};
    Access::seed_view_generation(runtime, tombstoned_key, 81);
    REQUIRE_FALSE(Access::observe_view_generation(
        runtime, tombstoned_key, 82));
    const auto tombstoned_proof =
        Access::direct_certifier(runtime, tombstoned_key);
    const auto tombstoned = Access::resolve_and_cache_commit(
        runtime, tombstoned_block, {}, tombstoned_proof);
    CHECK_FALSE(tombstoned.key.has_value());
    CHECK_FALSE(tombstoned.generation.has_value());
    CHECK_FALSE(Access::view_generation(
        runtime, tombstoned_key).has_value());

    const auto first_rotation = Access::rotate_to_tree(runtime, 1);
    REQUIRE(first_rotation.error == EpochIngressError::none);
    REQUIRE(first_rotation.update.has_value());
    REQUIRE(
        first_rotation.update->activation.configuration.tree_id == 1);
    const auto draining_block =
        indirect_commit_block(runtime, "draining-generation");
    const ProposalKey draining_key{
        configuration, draining_block->get_hash()};
    const auto draining_proof =
        Access::direct_certifier(runtime, draining_key);
    const auto draining = Access::resolve_and_cache_commit(
        runtime, draining_block, {}, draining_proof);
    REQUIRE(draining.key == draining_key);
    CHECK(
        draining.generation == checked_activation_generation(0, 0));

    const auto second_rotation = Access::rotate_to_tree(runtime, 2);
    REQUIRE(second_rotation.error == EpochIngressError::none);
    REQUIRE(second_rotation.update.has_value());
    REQUIRE(
        second_rotation.update->activation.configuration.tree_id == 2);
    const auto retired_block =
        indirect_commit_block(runtime, "retired-generation");
    const ProposalKey retired_key{
        configuration, retired_block->get_hash()};
    const auto retired_proof =
        Access::direct_certifier(runtime, retired_key);
    const auto retired = Access::resolve_and_cache_commit(
        runtime, retired_block, {}, retired_proof);
    CHECK_FALSE(retired.key.has_value());
    CHECK_FALSE(retired.generation.has_value());

    const ConfigurationId retired_configuration{
        9, 0, digest("retired-exact-configuration")};
    const auto draining_expired_block =
        indirect_commit_block(runtime, "stored-after-drain");
    const ProposalKey draining_expired_key{
        retired_configuration, draining_expired_block->get_hash()};
    Access::seed_view_generation(runtime, draining_expired_key, 91);
    const auto draining_expired_proof =
        Access::direct_certifier(runtime, draining_expired_key);
    const auto draining_expired = Access::resolve_and_cache_commit(
        runtime,
        draining_expired_block,
        {},
        draining_expired_proof);
    CHECK(draining_expired.key == draining_expired_key);
    CHECK(draining_expired.generation == 91);

    const auto missing_block =
        indirect_commit_block(runtime, "missing-after-drain");
    const ProposalKey missing_key{
        retired_configuration, missing_block->get_hash()};
    const auto missing_proof =
        Access::direct_certifier(runtime, missing_key);
    const auto missing = Access::resolve_and_cache_commit(
        runtime, missing_block, {}, missing_proof);
    CHECK_FALSE(missing.key.has_value());
    CHECK_FALSE(missing.generation.has_value());
}

TEST_CASE(
    "adaptive v3 tree rotation publishes the exact active configuration",
    "[adaptive-v3][rotation][structured-event][runtime-integration]")
{
    ScopedSigpipeIgnore ignore_sigpipe;
    EventContext event_context;
    TestHotStuff runtime(
        1,
        1,
        bytearray_t{},
        NetAddr("127.0.0.1:0"),
        new ActiveRuntimePaceMaker(1),
        event_context,
        0,
        HotStuffBase::Net::Config(),
        NetAddr(),
        EpochProtocolMode::adaptive_v3,
        adaptive_v3_runtime_config(1));
    using Access = ExperimentByzantineRuntimeIntegrationTestAccess;

    static_cast<void>(Access::initialize_active_runtime(runtime));
    RecordingAdaptiveEmitter emitter;
    runtime.bind_structured_event_emitters(nullptr, &emitter, nullptr);

    const auto rotation = Access::rotate_to_tree(runtime, 1);
    REQUIRE(rotation.error == EpochIngressError::none);
    REQUIRE(rotation.update.has_value());
    REQUIRE(emitter.events.size() == 1);
    const auto &event = emitter.events.front();
    CHECK(
        event.transition ==
        AdaptiveAggregationTransition::configuration_active);
    CHECK(event.configuration == rotation.update->activation.configuration);
    CHECK(event.configuration.tree_id == 1);
    CHECK(event.observer_replica == 1);
}

TEST_CASE(
    "terminal shared reporting poison retains the complete fifo",
    "[adaptive-v2][evidence][reporting][fail-closed][runtime-integration]")
{
    EventContext event_context;
    TestHotStuff runtime(
        1,
        1,
        bytearray_t{},
        NetAddr("127.0.0.1:0"),
        new PaceMakerDummy(1),
        event_context,
        0,
        HotStuffBase::Net::Config(),
        NetAddr(),
        EpochProtocolMode::adaptive_v2);
    using Access = ExperimentByzantineRuntimeIntegrationTestAccess;

    auto &outbox = Access::reset_reporting_outbox(runtime);
    const ConfigurationId configuration{
        7, 0, digest("poison-configuration")};
    REQUIRE(
        outbox.enqueue_readiness(configuration, 1, 0) ==
        AdaptiveV2ReportingEnqueueStatus::queued);
    REQUIRE(
        outbox.enqueue_lifecycle(
            NormalProposalRuntimeInitialized{
                ProposalKey{configuration, digest("poison-proposal")}}) ==
        AdaptiveV2ReportingEnqueueStatus::queued);
    REQUIRE(outbox.diagnostics().pending_reports == 2);

    Access::poison_reporting(runtime);
    const auto diagnostics = outbox.diagnostics();
    CHECK(diagnostics.stopped);
    CHECK(diagnostics.pending_reports == 2);
    CHECK(Access::lifecycle_reporting_suppressed(runtime));
    CHECK_FALSE(Access::convergence_evidence_healthy(runtime));
    CHECK(outbox.front() != nullptr);
    CHECK(outbox.front()->stream == AdaptiveV2ReportingStream::readiness);
}

TEST_CASE(
    "native HotStuff outbound hooks enforce rotating omission roles",
    "[adaptive-v2][experiment][byzantine][rotating][runtime-integration]")
{
    EventContext event_context;
    TestHotStuff runtime(
        1,
        1,
        bytearray_t{},
        NetAddr("127.0.0.1:0"),
        new PaceMakerDummy(1),
        event_context,
        0,
        HotStuffBase::Net::Config(),
        NetAddr(),
        EpochProtocolMode::adaptive_v2);
    std::vector<ExperimentOmissionMarker> markers;
    runtime.configure_experiment_byzantine_faults(
        rotating_options(1, &markers));

    const auto internal_key = selected_proposal(1, "native-internal");
    const auto internal = tree(ExperimentReplicaRole::internal);
    ExperimentByzantineRuntimeIntegrationTestAccess::
        seed_physical_parent_ingress(runtime, internal_key, internal);
    CHECK(ExperimentByzantineRuntimeIntegrationTestAccess::consume_aggregate(
        runtime, internal_key, internal));
    CHECK(ExperimentByzantineRuntimeIntegrationTestAccess::consume_aggregate(
        runtime, internal_key, internal));

    const auto leaf_key = selected_proposal(1, "native-leaf");
    const auto leaf = tree(ExperimentReplicaRole::leaf);
    ExperimentByzantineRuntimeIntegrationTestAccess::
        seed_physical_parent_ingress(runtime, leaf_key, leaf);
    CHECK(ExperimentByzantineRuntimeIntegrationTestAccess::consume_direct_vote(
        runtime, leaf_key, leaf));
    CHECK(ExperimentByzantineRuntimeIntegrationTestAccess::consume_direct_vote(
        runtime, leaf_key, leaf));

    const auto root_key = selected_proposal(1, "native-root");
    const auto root = tree(ExperimentReplicaRole::root);
    CHECK_FALSE(
        ExperimentByzantineRuntimeIntegrationTestAccess::consume_direct_vote(
            runtime, root_key, root));
    CHECK_FALSE(
        ExperimentByzantineRuntimeIntegrationTestAccess::consume_aggregate(
            runtime, root_key, root));

    const auto nonselected_key = selected_proposal(3, "native-nonselected");
    ExperimentByzantineRuntimeIntegrationTestAccess::
        seed_physical_parent_ingress(runtime, nonselected_key, internal);
    CHECK_FALSE(
        ExperimentByzantineRuntimeIntegrationTestAccess::consume_aggregate(
            runtime, nonselected_key, internal));
    CHECK_FALSE(
        ExperimentByzantineRuntimeIntegrationTestAccess::consume_direct_vote(
            runtime, nonselected_key, leaf));

    REQUIRE(markers.size() == 2);
    CHECK(markers[0].proposal == internal_key);
    CHECK(markers[0].action == ExperimentOmissionAction::omit_aggregate);
    CHECK(markers[0].monotonic_ns > 0);
    CHECK(markers[1].proposal == leaf_key);
    CHECK(markers[1].action == ExperimentOmissionAction::omit_direct_vote);
    CHECK(markers[1].monotonic_ns > 0);
}

TEST_CASE(
    "native HotStuff outbound hooks provide persistent omission clock",
    "[adaptive-v2][experiment][byzantine][persistent][runtime-integration]")
{
    EventContext event_context;
    TestHotStuff runtime(
        1,
        1,
        bytearray_t{},
        NetAddr("127.0.0.1:0"),
        new PaceMakerDummy(1),
        event_context,
        0,
        HotStuffBase::Net::Config(),
        NetAddr(),
        EpochProtocolMode::adaptive_v2);
    std::vector<ExperimentOmissionMarker> markers;
    runtime.configure_experiment_byzantine_faults(
        persistent_options(1, &markers));

    const ConfigurationId configuration{7, 3, digest("persistent-epoch")};
    const ProposalKey internal_key{
        configuration, digest("persistent-internal")};
    const ProposalKey leaf_key{
        configuration, digest("persistent-leaf")};
    const ProposalKey root_key{
        configuration, digest("persistent-root")};
    const auto internal = tree(ExperimentReplicaRole::internal);
    const auto leaf = tree(ExperimentReplicaRole::leaf);
    const auto root = tree(ExperimentReplicaRole::root);
    ExperimentByzantineRuntimeIntegrationTestAccess::
        seed_physical_parent_ingress(runtime, internal_key, internal);
    ExperimentByzantineRuntimeIntegrationTestAccess::
        seed_physical_parent_ingress(runtime, leaf_key, leaf);

    CHECK(ExperimentByzantineRuntimeIntegrationTestAccess::consume_aggregate(
        runtime, internal_key, internal));
    CHECK(ExperimentByzantineRuntimeIntegrationTestAccess::consume_direct_vote(
        runtime, leaf_key, leaf));
    CHECK_FALSE(
        ExperimentByzantineRuntimeIntegrationTestAccess::consume_aggregate(
            runtime, root_key, root));

    REQUIRE(markers.size() == 2);
    CHECK(markers[0].fault_mode == "persistent_selected_omission_v1");
    CHECK(markers[0].action == ExperimentOmissionAction::omit_aggregate);
    CHECK(markers[0].monotonic_ns > 0);
    CHECK(markers[1].fault_mode == "persistent_selected_omission_v1");
    CHECK(markers[1].action == ExperimentOmissionAction::omit_direct_vote);
    CHECK(markers[1].monotonic_ns > 0);
}

TEST_CASE(
    "native HotStuff hooks enforce tiered responsive ordinals and retries",
    "[adaptive-v2][experiment][byzantine][tiered][runtime-integration]")
{
    EventContext event_context;
    TestHotStuff runtime(
        1,
        2,
        bytearray_t{},
        NetAddr("127.0.0.1:0"),
        new PaceMakerDummy(1),
        event_context,
        0,
        HotStuffBase::Net::Config(),
        NetAddr(),
        EpochProtocolMode::adaptive_v2);
    std::vector<ExperimentOmissionMarker> markers;
    runtime.configure_experiment_byzantine_faults(
        tiered_options(2, 32, &markers));

    const ConfigurationId configuration{7, 3, digest("tiered-native-epoch")};
    const auto internal = tree(ExperimentReplicaRole::internal, 2);
    const auto root = tree(ExperimentReplicaRole::root, 2);
    CHECK_FALSE(
        ExperimentByzantineRuntimeIntegrationTestAccess::consume_aggregate(
            runtime,
            ProposalKey{configuration, digest("tiered-native-root")},
            root));

    for (std::uint32_t ordinal = 1; ordinal <= 31; ++ordinal)
    {
        const ProposalKey key{
            configuration,
            digest(
                "tiered-native-forward-" +
                std::to_string(ordinal))};
        ExperimentByzantineRuntimeIntegrationTestAccess::
            seed_physical_parent_ingress(runtime, key, internal);
        CHECK_FALSE(
            ExperimentByzantineRuntimeIntegrationTestAccess::consume_aggregate(
                runtime,
                key,
                internal));
    }
    REQUIRE(markers.size() == 31);
    CHECK(markers.back().contribution_ordinal == 31);

    const ProposalKey thirty_second{
        configuration, digest("tiered-native-omit-32")};
    ExperimentByzantineRuntimeIntegrationTestAccess::
        seed_physical_parent_ingress(runtime, thirty_second, internal);
    CHECK(ExperimentByzantineRuntimeIntegrationTestAccess::consume_aggregate(
        runtime, thirty_second, internal));
    CHECK(ExperimentByzantineRuntimeIntegrationTestAccess::consume_aggregate(
        runtime, thirty_second, internal));
    REQUIRE(markers.size() == 32);
    CHECK(markers.back().contribution_ordinal == 32);
    CHECK(markers.back().action == ExperimentOmissionAction::omit_aggregate);

    const ProposalKey thirty_third{
        configuration, digest("tiered-native-forward-33")};
    ExperimentByzantineRuntimeIntegrationTestAccess::
        seed_physical_parent_ingress(runtime, thirty_third, internal);
    CHECK_FALSE(
        ExperimentByzantineRuntimeIntegrationTestAccess::consume_aggregate(
            runtime, thirty_third, internal));
    REQUIRE(markers.size() == 33);
    CHECK(markers.back().contribution_ordinal == 33);
    CHECK(markers.back().action == ExperimentOmissionAction::forward);
}

TEST_CASE(
    "native HotStuff hooks keep tiered hard actors persistent by role",
    "[adaptive-v2][experiment][byzantine][tiered][hard][runtime-integration]")
{
    EventContext event_context;
    TestHotStuff runtime(
        1,
        1,
        bytearray_t{},
        NetAddr("127.0.0.1:0"),
        new PaceMakerDummy(1),
        event_context,
        0,
        HotStuffBase::Net::Config(),
        NetAddr(),
        EpochProtocolMode::adaptive_v2);
    std::vector<ExperimentOmissionMarker> markers;
    runtime.configure_experiment_byzantine_faults(
        tiered_options(1, 32, &markers));

    const ConfigurationId configuration{7, 3, digest("tiered-hard-epoch")};
    const auto internal = tree(ExperimentReplicaRole::internal, 1);
    const auto leaf = tree(ExperimentReplicaRole::leaf, 1);
    const auto root = tree(ExperimentReplicaRole::root, 1);
    const ProposalKey internal_key{
        configuration, digest("tiered-hard-internal")};
    const ProposalKey leaf_key{
        configuration, digest("tiered-hard-leaf")};
    const ProposalKey root_key{
        configuration, digest("tiered-hard-root")};
    ExperimentByzantineRuntimeIntegrationTestAccess::
        seed_physical_parent_ingress(runtime, internal_key, internal);
    ExperimentByzantineRuntimeIntegrationTestAccess::
        seed_physical_parent_ingress(runtime, leaf_key, leaf);
    CHECK(ExperimentByzantineRuntimeIntegrationTestAccess::consume_aggregate(
        runtime, internal_key, internal));
    CHECK(ExperimentByzantineRuntimeIntegrationTestAccess::consume_direct_vote(
        runtime, leaf_key, leaf));
    CHECK_FALSE(
        ExperimentByzantineRuntimeIntegrationTestAccess::consume_aggregate(
            runtime, root_key, root));

    REQUIRE(markers.size() == 2);
    CHECK(markers[0].cohort == ExperimentOmissionCohort::hard);
    CHECK(markers[0].contribution_ordinal == 0);
    CHECK(markers[1].cohort == ExperimentOmissionCohort::hard);
    CHECK(markers[1].action == ExperimentOmissionAction::omit_direct_vote);
}

TEST_CASE(
    "native hooks share tiered v2 epoch role clocks across tree rotations",
    "[adaptive-v2][experiment][byzantine][tiered-v2][runtime-integration]")
{
    EventContext event_context;
    TestHotStuff runtime(
        1,
        2,
        bytearray_t{},
        NetAddr("127.0.0.1:0"),
        new PaceMakerDummy(1),
        event_context,
        0,
        HotStuffBase::Net::Config(),
        NetAddr(),
        EpochProtocolMode::adaptive_v2);
    std::vector<ExperimentOmissionMarker> markers;
    runtime.configure_experiment_byzantine_faults(
        tiered_v2_options(2, 2, &markers));

    const ConfigurationId active{7, 3, digest("tiered-v2-native-active")};
    const ConfigurationId active_other_tree{
        7, 4, active.epoch_digest};
    const ConfigurationId predecessor{
        6, 3, digest("tiered-v2-native-predecessor")};
    const ConfigurationId predecessor_other_tree{
        6, 4, predecessor.epoch_digest};
    const ConfigurationId future{
        8, 3, digest("tiered-v2-native-future")};
    const ConfigurationId future_other_tree{
        8, 4, future.epoch_digest};
    const auto internal = tree(ExperimentReplicaRole::internal, 2);
    const auto leaf = tree(ExperimentReplicaRole::leaf, 2);
    const ProposalKey active_internal_1{
        active, digest("tiered-v2-native-active-internal-1")};
    const ProposalKey predecessor_internal_1{
        predecessor,
        digest("tiered-v2-native-predecessor-internal-1")};
    const ProposalKey future_internal_1{
        future, digest("tiered-v2-native-future-internal-1")};
    const ProposalKey active_internal_2{
        active_other_tree,
        digest("tiered-v2-native-active-internal-2")};
    const ProposalKey predecessor_internal_2{
        predecessor_other_tree,
        digest("tiered-v2-native-predecessor-internal-2")};
    const ProposalKey future_internal_2{
        future_other_tree,
        digest("tiered-v2-native-future-internal-2")};
    const ProposalKey active_leaf_1{
        active_other_tree,
        digest("tiered-v2-native-active-leaf-1")};
    const ProposalKey active_leaf_2{
        active, digest("tiered-v2-native-active-leaf-2")};
    const std::vector<ProposalKey> authenticated_internal_keys{
        active_internal_1,
        predecessor_internal_1,
        future_internal_1,
        active_internal_2,
        predecessor_internal_2,
        future_internal_2};
    for (const auto &key : authenticated_internal_keys)
        ExperimentByzantineRuntimeIntegrationTestAccess::
            seed_physical_parent_ingress(runtime, key, internal);
    ExperimentByzantineRuntimeIntegrationTestAccess::
        seed_physical_parent_ingress(runtime, active_leaf_1, leaf);
    ExperimentByzantineRuntimeIntegrationTestAccess::
        seed_physical_parent_ingress(runtime, active_leaf_2, leaf);

    CHECK_FALSE(
        ExperimentByzantineRuntimeIntegrationTestAccess::consume_aggregate(
            runtime, active_internal_1, internal));
    CHECK_FALSE(
        ExperimentByzantineRuntimeIntegrationTestAccess::consume_aggregate(
            runtime, predecessor_internal_1, internal));
    CHECK_FALSE(
        ExperimentByzantineRuntimeIntegrationTestAccess::consume_aggregate(
            runtime, future_internal_1, internal));
    CHECK(ExperimentByzantineRuntimeIntegrationTestAccess::consume_aggregate(
        runtime, active_internal_2, internal));
    CHECK(ExperimentByzantineRuntimeIntegrationTestAccess::consume_aggregate(
        runtime, predecessor_internal_2, internal));
    CHECK(ExperimentByzantineRuntimeIntegrationTestAccess::consume_aggregate(
        runtime, future_internal_2, internal));

    CHECK_FALSE(
        ExperimentByzantineRuntimeIntegrationTestAccess::consume_direct_vote(
            runtime, active_leaf_1, leaf));
    CHECK(ExperimentByzantineRuntimeIntegrationTestAccess::consume_direct_vote(
        runtime, active_leaf_2, leaf));

    REQUIRE(markers.size() == 8);
    for (std::size_t index = 0; index < markers.size(); ++index)
        CHECK(markers[index].contribution_ordinal == index + 1);
    CHECK(markers[0].contribution_role == ExperimentReplicaRole::internal);
    CHECK(markers[0].role_contribution_ordinal == 1);
    CHECK(markers[1].role_contribution_ordinal == 1);
    CHECK(markers[2].role_contribution_ordinal == 1);
    CHECK(markers[3].role_contribution_ordinal == 2);
    CHECK(markers[3].action == ExperimentOmissionAction::omit_aggregate);
    CHECK(markers[4].role_contribution_ordinal == 2);
    CHECK(markers[5].role_contribution_ordinal == 2);
    CHECK(markers[6].contribution_role == ExperimentReplicaRole::leaf);
    CHECK(markers[6].role_contribution_ordinal == 1);
    CHECK(markers[7].contribution_role == ExperimentReplicaRole::leaf);
    CHECK(markers[7].role_contribution_ordinal == 2);
    CHECK(markers[7].action ==
          ExperimentOmissionAction::omit_direct_vote);
}

TEST_CASE(
    "native tiered decisions emit exact contribution opportunities once",
    "[adaptive-v2][experiment][fault-opportunity][runtime-integration]"
    "[intentional-red]")
{
    SECTION("responsive internal and leaf forward then omit")
    {
        EventContext event_context;
        TestHotStuff runtime(
            1,
            2,
            bytearray_t{},
            NetAddr("127.0.0.1:0"),
            new PaceMakerDummy(1),
            event_context,
            0,
            HotStuffBase::Net::Config(),
            NetAddr(),
            EpochProtocolMode::adaptive_v2);
        std::vector<std::string> order;
        RecordingOpportunityAuditEmitter emitter(&order);
        std::vector<ExperimentOmissionMarker> markers;
        auto options = tiered_v2_options(2, 2);
        options.omission_marker_emitter =
            [&markers, &order](const ExperimentOmissionMarker &marker)
            {
                order.push_back("marker");
                markers.push_back(marker);
            };
        runtime.bind_structured_event_emitters(nullptr, nullptr, &emitter);
        runtime.configure_experiment_byzantine_faults(std::move(options));

        const ConfigurationId exact_configuration{
            7, 3, digest("opportunity-responsive-epoch")};
        const auto internal = tree(ExperimentReplicaRole::internal, 2);
        const auto leaf = tree(ExperimentReplicaRole::leaf, 2);
        const ProposalKey internal_forward{
            exact_configuration, digest("opportunity-internal-forward")};
        const ProposalKey internal_omit{
            exact_configuration, digest("opportunity-internal-omit")};
        const ProposalKey leaf_forward{
            exact_configuration, digest("opportunity-leaf-forward")};
        const ProposalKey leaf_omit{
            exact_configuration, digest("opportunity-leaf-omit")};
        using Access = ExperimentByzantineRuntimeIntegrationTestAccess;
        Access::seed_view_generation(runtime, internal_forward, 17);
        Access::seed_view_generation(runtime, internal_omit, 18);
        Access::seed_view_generation(runtime, leaf_forward, 19);
        Access::seed_view_generation(runtime, leaf_omit, 20);

        CHECK_FALSE(Access::consume_aggregate(
            runtime, internal_forward, internal));
        CHECK_FALSE(Access::consume_aggregate(
            runtime, internal_forward, internal));
        CHECK(Access::consume_aggregate(
            runtime, internal_omit, internal));
        CHECK(Access::consume_aggregate(
            runtime, internal_omit, internal));
        CHECK_FALSE(Access::consume_direct_vote(
            runtime, leaf_forward, leaf));
        CHECK_FALSE(Access::consume_direct_vote(
            runtime, leaf_forward, leaf));
        CHECK(Access::consume_direct_vote(runtime, leaf_omit, leaf));
        CHECK(Access::consume_direct_vote(runtime, leaf_omit, leaf));

        REQUIRE(emitter.events.size() == 4);
        REQUIRE(markers.size() == 4);
        CHECK(order == std::vector<std::string>{
                           "opportunity", "marker",
                           "opportunity", "marker",
                           "opportunity", "marker",
                           "opportunity", "marker"});

        const auto &first = emitter.events[0];
        CHECK(first.actor == 2);
        CHECK(first.proposal == internal_forward);
        CHECK(first.view_generation == 17);
        CHECK(first.physical_role == ExperimentReplicaRole::internal);
        CHECK(first.parent_replica == 0);
        CHECK(first.authenticated_proposal_source_replica == 0);
        CHECK(first.expected_message_type ==
              ExpectedMessageType::aggregate_relay);
        CHECK(first.cohort ==
              ExperimentOmissionCohort::responsive_degraded);
        CHECK(first.scheduled_action == ExperimentOmissionAction::forward);
        CHECK(first.contribution_ordinal == 1);
        CHECK(first.role_contribution_ordinal == 1);
        CHECK(first.diagnostic_window == "native-runtime-window");
        CHECK(first.window_start_monotonic_ns == 1);
        CHECK(first.window_end_monotonic_ns ==
              std::numeric_limits<std::uint64_t>::max());
        CHECK(first.decision_monotonic_ns == markers[0].monotonic_ns);
        CHECK(first.decision_monotonic_ns >=
              first.window_start_monotonic_ns);
        CHECK(first.decision_monotonic_ns <
              first.window_end_monotonic_ns);
        CHECK(first.responsive_omission_period == 2);
        CHECK(first.fault_threshold == 10);
        CHECK(first.hard_actor_count == 1);
        CHECK(first.responsive_degraded_actor_count == 1);
        CHECK(first.fault_mode ==
              "tiered_persistent_responsive_omission_v2");

        CHECK(emitter.events[1].proposal == internal_omit);
        CHECK(emitter.events[1].scheduled_action ==
              ExperimentOmissionAction::omit_aggregate);
        CHECK(emitter.events[1].contribution_ordinal == 2);
        CHECK(emitter.events[1].role_contribution_ordinal == 2);
        CHECK(emitter.events[2].proposal == leaf_forward);
        CHECK(emitter.events[2].physical_role ==
              ExperimentReplicaRole::leaf);
        CHECK(emitter.events[2].expected_message_type ==
              ExpectedMessageType::direct_vote);
        CHECK(emitter.events[2].scheduled_action ==
              ExperimentOmissionAction::forward);
        CHECK(emitter.events[2].contribution_ordinal == 3);
        CHECK(emitter.events[2].role_contribution_ordinal == 1);
        CHECK(emitter.events[3].proposal == leaf_omit);
        CHECK(emitter.events[3].scheduled_action ==
              ExperimentOmissionAction::omit_direct_vote);
        CHECK(emitter.events[3].contribution_ordinal == 4);
        CHECK(emitter.events[3].role_contribution_ordinal == 2);
    }

    SECTION("hard internal and leaf actions remain persistent")
    {
        EventContext event_context;
        TestHotStuff runtime(
            1,
            1,
            bytearray_t{},
            NetAddr("127.0.0.1:0"),
            new PaceMakerDummy(1),
            event_context,
            0,
            HotStuffBase::Net::Config(),
            NetAddr(),
            EpochProtocolMode::adaptive_v2);
        RecordingOpportunityAuditEmitter emitter;
        runtime.bind_structured_event_emitters(nullptr, nullptr, &emitter);
        runtime.configure_experiment_byzantine_faults(
            tiered_v2_options(1, 41));
        const ConfigurationId exact_configuration{
            7, 3, digest("opportunity-hard-epoch")};
        const ProposalKey internal_key{
            exact_configuration, digest("opportunity-hard-internal")};
        const ProposalKey leaf_key{
            exact_configuration, digest("opportunity-hard-leaf")};
        using Access = ExperimentByzantineRuntimeIntegrationTestAccess;
        Access::seed_view_generation(runtime, internal_key, 21);
        Access::seed_view_generation(runtime, leaf_key, 22);

        CHECK(Access::consume_aggregate(
            runtime,
            internal_key,
            tree(ExperimentReplicaRole::internal, 1)));
        CHECK(Access::consume_direct_vote(
            runtime,
            leaf_key,
            tree(ExperimentReplicaRole::leaf, 1)));

        REQUIRE(emitter.events.size() == 2);
        CHECK(emitter.events[0].cohort == ExperimentOmissionCohort::hard);
        CHECK(emitter.events[0].scheduled_action ==
              ExperimentOmissionAction::omit_aggregate);
        CHECK(emitter.events[0].contribution_ordinal == 0);
        CHECK(emitter.events[0].role_contribution_ordinal == 0);
        CHECK(emitter.events[1].cohort == ExperimentOmissionCohort::hard);
        CHECK(emitter.events[1].scheduled_action ==
              ExperimentOmissionAction::omit_direct_vote);
        CHECK(emitter.events[1].contribution_ordinal == 0);
        CHECK(emitter.events[1].role_contribution_ordinal == 0);
    }
}

TEST_CASE(
    "contribution opportunity exclusions never alter the fault decision",
    "[adaptive-v2][experiment][fault-opportunity][exclusions]"
    "[intentional-red]")
{
    SECTION("roots and nonactors emit nothing")
    {
        EventContext root_context;
        TestHotStuff root_runtime(
            1, 2, bytearray_t{}, NetAddr("127.0.0.1:0"),
            new PaceMakerDummy(1), root_context, 0,
            HotStuffBase::Net::Config(), NetAddr(),
            EpochProtocolMode::adaptive_v2);
        RecordingOpportunityAuditEmitter root_emitter;
        root_runtime.bind_structured_event_emitters(
            nullptr, nullptr, &root_emitter);
        root_runtime.configure_experiment_byzantine_faults(
            tiered_v2_options(2, 2));
        const ProposalKey root_key{
            ConfigurationId{7, 3, digest("opportunity-root-epoch")},
            digest("opportunity-root")};
        ExperimentByzantineRuntimeIntegrationTestAccess::seed_view_generation(
            root_runtime, root_key, 23);
        CHECK_FALSE(
            ExperimentByzantineRuntimeIntegrationTestAccess::
                consume_aggregate(
                    root_runtime,
                    root_key,
                    tree(ExperimentReplicaRole::root, 2)));
        CHECK(root_emitter.events.empty());

        EventContext nonactor_context;
        TestHotStuff nonactor_runtime(
            1, 3, bytearray_t{}, NetAddr("127.0.0.1:0"),
            new PaceMakerDummy(1), nonactor_context, 0,
            HotStuffBase::Net::Config(), NetAddr(),
            EpochProtocolMode::adaptive_v2);
        RecordingOpportunityAuditEmitter nonactor_emitter;
        nonactor_runtime.bind_structured_event_emitters(
            nullptr, nullptr, &nonactor_emitter);
        nonactor_runtime.configure_experiment_byzantine_faults(
            tiered_v2_options(3, 2));
        const ProposalKey nonactor_key{
            ConfigurationId{7, 3, digest("opportunity-nonactor-epoch")},
            digest("opportunity-nonactor")};
        ExperimentByzantineRuntimeIntegrationTestAccess::seed_view_generation(
            nonactor_runtime, nonactor_key, 24);
        CHECK_FALSE(
            ExperimentByzantineRuntimeIntegrationTestAccess::
                consume_aggregate(
                    nonactor_runtime,
                    nonactor_key,
                    tree(ExperimentReplicaRole::internal, 3)));
        CHECK(nonactor_emitter.events.empty());
    }

    SECTION("outside-window and capacity decisions emit nothing new")
    {
        EventContext outside_context;
        TestHotStuff outside_runtime(
            1, 2, bytearray_t{}, NetAddr("127.0.0.1:0"),
            new PaceMakerDummy(1), outside_context, 0,
            HotStuffBase::Net::Config(), NetAddr(),
            EpochProtocolMode::adaptive_v2);
        RecordingOpportunityAuditEmitter outside_emitter;
        auto outside_options = tiered_v2_options(2, 2);
        outside_options.rotating_omission->window_start_monotonic_ns =
            std::numeric_limits<std::uint64_t>::max() - 1;
        outside_options.rotating_omission->window_end_monotonic_ns =
            std::numeric_limits<std::uint64_t>::max();
        outside_runtime.bind_structured_event_emitters(
            nullptr, nullptr, &outside_emitter);
        outside_runtime.configure_experiment_byzantine_faults(
            std::move(outside_options));
        const ProposalKey outside_key{
            ConfigurationId{7, 3, digest("opportunity-outside-epoch")},
            digest("opportunity-outside")};
        ExperimentByzantineRuntimeIntegrationTestAccess::seed_view_generation(
            outside_runtime, outside_key, 25);
        CHECK_FALSE(
            ExperimentByzantineRuntimeIntegrationTestAccess::
                consume_aggregate(
                    outside_runtime,
                    outside_key,
                    tree(ExperimentReplicaRole::internal, 2)));
        CHECK(outside_emitter.events.empty());

        EventContext capacity_context;
        TestHotStuff capacity_runtime(
            1, 2, bytearray_t{}, NetAddr("127.0.0.1:0"),
            new PaceMakerDummy(1), capacity_context, 0,
            HotStuffBase::Net::Config(), NetAddr(),
            EpochProtocolMode::adaptive_v2);
        RecordingOpportunityAuditEmitter capacity_emitter;
        std::vector<ExperimentOmissionMarker> capacity_markers;
        auto capacity_options = tiered_v2_options(2, 2);
        capacity_options.rotating_omission->maximum_contexts = 1;
        capacity_options.omission_marker_emitter =
            [&capacity_markers](const ExperimentOmissionMarker &marker)
            { capacity_markers.push_back(marker); };
        capacity_runtime.bind_structured_event_emitters(
            nullptr, nullptr, &capacity_emitter);
        capacity_runtime.configure_experiment_byzantine_faults(
            std::move(capacity_options));
        const ConfigurationId capacity_configuration{
            7, 3, digest("opportunity-capacity-epoch")};
        const ProposalKey retained{
            capacity_configuration, digest("opportunity-capacity-retained")};
        const ProposalKey rejected{
            capacity_configuration, digest("opportunity-capacity-rejected")};
        ExperimentByzantineRuntimeIntegrationTestAccess::seed_view_generation(
            capacity_runtime, retained, 26);
        ExperimentByzantineRuntimeIntegrationTestAccess::seed_view_generation(
            capacity_runtime, rejected, 27);
        CHECK_FALSE(
            ExperimentByzantineRuntimeIntegrationTestAccess::
                consume_aggregate(
                    capacity_runtime,
                    retained,
                    tree(ExperimentReplicaRole::internal, 2)));
        CHECK_FALSE(
            ExperimentByzantineRuntimeIntegrationTestAccess::
                consume_aggregate(
                    capacity_runtime,
                    rejected,
                    tree(ExperimentReplicaRole::internal, 2)));
        REQUIRE(capacity_emitter.events.size() == 1);
        REQUIRE(capacity_markers.size() == 2);
        CHECK(capacity_markers.back().action ==
              ExperimentOmissionAction::capacity_exhausted);
    }

    SECTION("missing emitter and throwing marker callback preserve action")
    {
        EventContext no_hook_context;
        TestHotStuff no_hook_runtime(
            1, 1, bytearray_t{}, NetAddr("127.0.0.1:0"),
            new PaceMakerDummy(1), no_hook_context, 0,
            HotStuffBase::Net::Config(), NetAddr(),
            EpochProtocolMode::adaptive_v2);
        std::vector<ExperimentOmissionMarker> no_hook_markers;
        no_hook_runtime.configure_experiment_byzantine_faults(
            tiered_v2_options(1, 41, &no_hook_markers));
        const ProposalKey no_hook_key{
            ConfigurationId{7, 3, digest("opportunity-no-hook-epoch")},
            digest("opportunity-no-hook")};
        ExperimentByzantineRuntimeIntegrationTestAccess::seed_view_generation(
            no_hook_runtime, no_hook_key, 28);
        CHECK(
            ExperimentByzantineRuntimeIntegrationTestAccess::
                consume_aggregate(
                    no_hook_runtime,
                    no_hook_key,
                    tree(ExperimentReplicaRole::internal, 1)));
        REQUIRE(no_hook_markers.size() == 1);

        EventContext throwing_context;
        TestHotStuff throwing_runtime(
            1, 1, bytearray_t{}, NetAddr("127.0.0.1:0"),
            new PaceMakerDummy(1), throwing_context, 0,
            HotStuffBase::Net::Config(), NetAddr(),
            EpochProtocolMode::adaptive_v2);
        RecordingOpportunityAuditEmitter throwing_emitter;
        auto throwing_options = tiered_v2_options(1, 41);
        throwing_options.omission_marker_emitter =
            [](const ExperimentOmissionMarker &)
            { throw std::runtime_error("injected marker callback failure"); };
        throwing_runtime.bind_structured_event_emitters(
            nullptr, nullptr, &throwing_emitter);
        throwing_runtime.configure_experiment_byzantine_faults(
            std::move(throwing_options));
        const ProposalKey throwing_key{
            ConfigurationId{7, 3, digest("opportunity-throwing-epoch")},
            digest("opportunity-throwing")};
        ExperimentByzantineRuntimeIntegrationTestAccess::seed_view_generation(
            throwing_runtime, throwing_key, 29);
        bool consumed = false;
        CHECK_NOTHROW(
            consumed = ExperimentByzantineRuntimeIntegrationTestAccess::
                consume_aggregate(
                    throwing_runtime,
                    throwing_key,
                    tree(ExperimentReplicaRole::internal, 1)));
        CHECK(consumed);
        REQUIRE(throwing_emitter.events.size() == 1);
    }

    SECTION("tiered v1 keeps its marker callback and emits no opportunity")
    {
        EventContext event_context;
        TestHotStuff runtime(
            1, 2, bytearray_t{}, NetAddr("127.0.0.1:0"),
            new PaceMakerDummy(1), event_context, 0,
            HotStuffBase::Net::Config(), NetAddr(),
            EpochProtocolMode::adaptive_v2);
        RecordingOpportunityAuditEmitter emitter;
        std::vector<ExperimentOmissionMarker> markers;
        runtime.bind_structured_event_emitters(nullptr, nullptr, &emitter);
        runtime.configure_experiment_byzantine_faults(
            tiered_options(2, 2, &markers));
        const ProposalKey key{
            ConfigurationId{7, 3, digest("opportunity-v1-epoch")},
            digest("opportunity-v1-internal")};
        ExperimentByzantineRuntimeIntegrationTestAccess::seed_view_generation(
            runtime, key, 30);

        CHECK_FALSE(
            ExperimentByzantineRuntimeIntegrationTestAccess::
                consume_aggregate(
                    runtime,
                    key,
                    tree(ExperimentReplicaRole::internal, 2)));
        CHECK_FALSE(
            ExperimentByzantineRuntimeIntegrationTestAccess::
                consume_aggregate(
                    runtime,
                    key,
                    tree(ExperimentReplicaRole::internal, 2)));

        CHECK(emitter.events.empty());
        REQUIRE(markers.size() == 1);
        CHECK_FALSE(markers.front().view_generation.has_value());
        CHECK_FALSE(markers.front().physical_parent.has_value());
        CHECK_FALSE(markers.front().expected_message_type.has_value());
        CHECK(markers.front().physical_role ==
              ExperimentReplicaRole::root);

        const auto encoded =
            format_experiment_omission_marker(markers.front());
        CHECK(encoded.find("view_generation=") == std::string::npos);
        CHECK(encoded.find("physical_parent=") == std::string::npos);
        CHECK(encoded.find("expected_message_type=") ==
              std::string::npos);
        CHECK(encoded.find("physical_role=") == std::string::npos);
    }
}

TEST_CASE(
    "scheduled omission ignores root repair ingress before consuming ordinal",
    "[adaptive-v2][experiment][fault-opportunity][physical-parent]"
    "[runtime-integration][intentional-red]")
{
    EventContext event_context;
    TestHotStuff runtime(
        1,
        2,
        bytearray_t{},
        NetAddr("127.0.0.1:0"),
        new PaceMakerDummy(1),
        event_context,
        0,
        HotStuffBase::Net::Config(),
        NetAddr(),
        EpochProtocolMode::adaptive_v2);
    RecordingOpportunityAuditEmitter emitter;
    std::vector<ExperimentOmissionMarker> markers;
    auto options = tiered_v2_options(2, 2, &markers);
    runtime.bind_structured_event_emitters(nullptr, nullptr, &emitter);
    runtime.configure_experiment_byzantine_faults(std::move(options));

    const ConfigurationId configuration{
        7, 3, digest("physical-parent-ingress-epoch")};
    const ProposalKey missing_source{
        configuration, digest("missing-source")};
    const ProposalKey root_repair{
        configuration, digest("root-repair-source")};
    const ProposalKey physical_parent{
        configuration, digest("physical-parent-source")};
    const auto internal = tree(ExperimentReplicaRole::internal, 2);
    using Access = ExperimentByzantineRuntimeIntegrationTestAccess;

    Access::seed_view_generation_without_source(
        runtime, missing_source, 71);
    Access::seed_view_generation(runtime, root_repair, 72, 7);
    Access::seed_view_generation(runtime, physical_parent, 73, 0);

    CHECK_FALSE(Access::consume_aggregate(
        runtime, missing_source, internal));
    CHECK_FALSE(Access::consume_aggregate(
        runtime, root_repair, internal));
    CHECK(markers.empty());
    CHECK(emitter.events.empty());

    CHECK_FALSE(Access::consume_aggregate(
        runtime, physical_parent, internal));
    REQUIRE(markers.size() == 1);
    REQUIRE(emitter.events.size() == 1);
    CHECK(markers.front().contribution_ordinal == 1);
    CHECK(markers.front().role_contribution_ordinal == 1);
    CHECK(markers.front().authenticated_proposal_source_replica ==
          std::optional<ReplicaID>{0});
    CHECK(emitter.events.front().authenticated_proposal_source_replica == 0);
    CHECK(emitter.events.front().parent_replica == 0);
}

TEST_CASE(
    "proposal source qualification survives evidence preserving cleanup",
    "[adaptive-v2][experiment][fault-opportunity][physical-parent]"
    "[cleanup][runtime-integration][intentional-red]")
{
    EventContext event_context;
    TestHotStuff runtime(
        1,
        2,
        bytearray_t{},
        NetAddr("127.0.0.1:0"),
        new PaceMakerDummy(1),
        event_context,
        0,
        HotStuffBase::Net::Config(),
        NetAddr(),
        EpochProtocolMode::adaptive_v2);
    std::vector<ExperimentOmissionMarker> markers;
    runtime.configure_experiment_byzantine_faults(
        tiered_v2_options(2, 2, &markers));

    const ConfigurationId configuration{
        7, 3, digest("preserved-ingress-epoch")};
    const ProposalKey vote_fallback{
        configuration, digest("preserved-vote-fallback")};
    const ProposalKey response_evidence{
        configuration, digest("preserved-response-evidence")};
    const ProposalKey terminal{
        configuration, digest("terminal-ingress")};
    const auto internal = tree(ExperimentReplicaRole::internal, 2);
    using Access = ExperimentByzantineRuntimeIntegrationTestAccess;
    Access::seed_view_generation(runtime, vote_fallback, 81, 0);
    Access::seed_view_generation(runtime, response_evidence, 82, 0);
    Access::seed_view_generation(runtime, terminal, 83, 0);

    Access::purge_exact_runtime_state(
        runtime, vote_fallback, true, false);
    Access::purge_exact_runtime_state(
        runtime, response_evidence, false, true);
    CHECK(Access::has_authenticated_proposal_ingress(
        runtime, vote_fallback));
    CHECK(Access::has_authenticated_proposal_ingress(
        runtime, response_evidence));
    CHECK_FALSE(Access::consume_aggregate(
        runtime, vote_fallback, internal));
    CHECK(Access::consume_aggregate(
        runtime, response_evidence, internal));
    REQUIRE(markers.size() == 2);

    Access::purge_exact_runtime_state(runtime, terminal, false, false);
    CHECK_FALSE(Access::has_authenticated_proposal_ingress(
        runtime, terminal));
    CHECK_FALSE(Access::consume_aggregate(runtime, terminal, internal));
    CHECK(markers.size() == 2);
}

} // namespace
