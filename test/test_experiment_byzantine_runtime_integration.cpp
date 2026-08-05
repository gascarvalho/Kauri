#include <cstdint>
#include <limits>
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

} // namespace
