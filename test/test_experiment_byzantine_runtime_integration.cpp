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
                block_hash, std::move(committed_key), std::nullopt};
    }

    static void report_committed(
        HotStuffBase &runtime,
        std::optional<ProposalKey> committed_key)
    {
        runtime.report_adaptive_v2_committed(committed_key);
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
