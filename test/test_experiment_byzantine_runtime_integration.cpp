#include <cstdint>
#include <limits>
#include <memory>
#include <optional>
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
    struct CachedCommitIdentity
    {
        std::optional<ProposalKey> key;
        std::optional<std::uint64_t> generation;
        bool unavailable{false};
        bool conflicted{false};
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
        std::uint64_t generation = 1)
    {
        runtime.proposal_view_generations.insert_or_assign(
            key, generation);
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
                std::nullopt,
                HotStuffBase::CommittedProposalIdentityDisposition::exact};
    }

    static void seed_convergence_identity(HotStuffBase &runtime)
    {
        runtime.adaptive_v2_committed_convergence_identity =
            AdaptiveV2EpochChangeIdentity{};
    }

    static void report_committed(
        HotStuffBase &runtime,
        std::optional<ProposalKey> committed_key)
    {
        runtime.report_adaptive_v2_committed(committed_key);
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

    static block_t add_commit_rule_block(
        HotStuffBase &runtime,
        const ConfigurationId &configuration,
        const block_t &parent,
        const block_t &qc_reference,
        const std::string &label)
    {
        if (parent == nullptr || qc_reference == nullptr)
            throw std::invalid_argument(
                "commit-rule block requires parent and QC reference");
        auto certificate = qc_reference == runtime.get_genesis()
            ? genesis_parent_certificate(runtime)
            : direct_certifier(
                  runtime,
                  ProposalKey{configuration, qc_reference->get_hash()});
        block_t block = new Block(
            std::vector<block_t>{parent},
            std::vector<uint256_t>{DataStream(label).get_hash()},
            std::move(certificate),
            bytearray_t{},
            parent->get_height() + 1,
            qc_reference,
            nullptr);
        runtime.storage->add_blk(block);
        if (!runtime.HotStuffCore::on_deliver_blk(block))
            throw std::runtime_error(
                "commit-rule block delivery failed");
        return block;
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

class ActiveRuntimePaceMaker final : public PaceMakerDummy
{
public:
    explicit ActiveRuntimePaceMaker(int32_t parent_limit)
        : PaceMakerDummy(parent_limit) {}

    size_t get_current_tid() override { return 0; }
    size_t get_current_epoch() override { return 0; }
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
};

uint256_t digest(const std::string &label)
{
    return DataStream(label).get_hash();
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

    SECTION("missing identity while convergence is pending remains fatal")
    {
        Access::reset_reporting_outbox(runtime);
        const auto block =
            indirect_commit_block(runtime, "pending-convergence-gap");
        const auto cached = Access::resolve_and_cache_commit(
            runtime, block, {}, nullptr, true);
        REQUIRE(cached.unavailable);
        Access::seed_convergence_identity(runtime);

        Access::report_and_post_commit(runtime, block);

        CHECK_FALSE(Access::convergence_evidence_healthy(runtime));
        REQUIRE(emitter.events.size() == 1);
        CHECK(std::get_if<CommitObservedStructuredEvent>(
                  &emitter.events.front()) != nullptr);
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
    CHECK(ExperimentByzantineRuntimeIntegrationTestAccess::consume_aggregate(
        runtime, internal_key, internal));
    CHECK(ExperimentByzantineRuntimeIntegrationTestAccess::consume_aggregate(
        runtime, internal_key, internal));

    const auto leaf_key = selected_proposal(1, "native-leaf");
    const auto leaf = tree(ExperimentReplicaRole::leaf);
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
        CHECK_FALSE(
            ExperimentByzantineRuntimeIntegrationTestAccess::consume_aggregate(
                runtime,
                ProposalKey{
                    configuration,
                    digest(
                        "tiered-native-forward-" +
                        std::to_string(ordinal))},
                internal));
    }
    REQUIRE(markers.size() == 31);
    CHECK(markers.back().contribution_ordinal == 31);

    const ProposalKey thirty_second{
        configuration, digest("tiered-native-omit-32")};
    CHECK(ExperimentByzantineRuntimeIntegrationTestAccess::consume_aggregate(
        runtime, thirty_second, internal));
    CHECK(ExperimentByzantineRuntimeIntegrationTestAccess::consume_aggregate(
        runtime, thirty_second, internal));
    REQUIRE(markers.size() == 32);
    CHECK(markers.back().contribution_ordinal == 32);
    CHECK(markers.back().action == ExperimentOmissionAction::omit_aggregate);

    CHECK_FALSE(
        ExperimentByzantineRuntimeIntegrationTestAccess::consume_aggregate(
            runtime,
            ProposalKey{configuration, digest("tiered-native-forward-33")},
            internal));
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
    CHECK(ExperimentByzantineRuntimeIntegrationTestAccess::consume_aggregate(
        runtime,
        ProposalKey{configuration, digest("tiered-hard-internal")},
        internal));
    CHECK(ExperimentByzantineRuntimeIntegrationTestAccess::consume_direct_vote(
        runtime,
        ProposalKey{configuration, digest("tiered-hard-leaf")},
        leaf));
    CHECK_FALSE(
        ExperimentByzantineRuntimeIntegrationTestAccess::consume_aggregate(
            runtime,
            ProposalKey{configuration, digest("tiered-hard-root")},
            root));

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

    CHECK_FALSE(
        ExperimentByzantineRuntimeIntegrationTestAccess::consume_aggregate(
            runtime,
            ProposalKey{active, digest("tiered-v2-native-active-internal-1")},
            internal));
    CHECK_FALSE(
        ExperimentByzantineRuntimeIntegrationTestAccess::consume_aggregate(
            runtime,
            ProposalKey{
                predecessor,
                digest("tiered-v2-native-predecessor-internal-1")},
            internal));
    CHECK_FALSE(
        ExperimentByzantineRuntimeIntegrationTestAccess::consume_aggregate(
            runtime,
            ProposalKey{future, digest("tiered-v2-native-future-internal-1")},
            internal));
    CHECK(ExperimentByzantineRuntimeIntegrationTestAccess::consume_aggregate(
        runtime,
        ProposalKey{
            active_other_tree,
            digest("tiered-v2-native-active-internal-2")},
        internal));
    CHECK(ExperimentByzantineRuntimeIntegrationTestAccess::consume_aggregate(
        runtime,
        ProposalKey{
            predecessor_other_tree,
            digest("tiered-v2-native-predecessor-internal-2")},
        internal));
    CHECK(ExperimentByzantineRuntimeIntegrationTestAccess::consume_aggregate(
        runtime,
        ProposalKey{
            future_other_tree,
            digest("tiered-v2-native-future-internal-2")},
        internal));

    CHECK_FALSE(
        ExperimentByzantineRuntimeIntegrationTestAccess::consume_direct_vote(
            runtime,
            ProposalKey{
                active_other_tree,
                digest("tiered-v2-native-active-leaf-1")},
            leaf));
    CHECK(ExperimentByzantineRuntimeIntegrationTestAccess::consume_direct_vote(
        runtime,
        ProposalKey{active, digest("tiered-v2-native-active-leaf-2")},
        leaf));

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

} // namespace
