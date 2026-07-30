#include <cstddef>
#include <cstdint>
#include <fstream>
#include <iterator>
#include <memory>
#include <optional>
#include <string>
#include <type_traits>

#include "catch.hpp"
#include "hotstuff/configuration.h"

#ifndef KAURI_PROJECT_SOURCE_DIR
#error "KAURI_PROJECT_SOURCE_DIR must name the repository root"
#endif

/*
 * Experiment-only Byzantine adapter contract
 * ------------------------------------------
 * The adapter is a pure local fault seam. It does not own consensus state,
 * certificates, membership, quorum, timers, transport, or manager output.
 * Callers arm an exact proposal context, suppress only its normal positive
 * evidence observation, and ask separately at the real deadline whether one
 * false timeout should be recorded. Aggregate omission consumes the outbound
 * aggregate as a successful experiment drop so the consensus path does not
 * schedule a retry.
 *
 * This fallback keeps the missing production API compile-visible. Its methods
 * are deliberately undefined so the target fails to link until the adapter is
 * implemented.
 */
#if __has_include("hotstuff/experiment_byzantine_adapter.h")
#include "hotstuff/experiment_byzantine_adapter.h"
#define KAURI_HAS_EXPERIMENT_BYZANTINE_ADAPTER 1
#else
#define KAURI_HAS_EXPERIMENT_BYZANTINE_ADAPTER 0

namespace hotstuff
{

struct ExperimentByzantineContext final
{
    ProposalKey proposal;
    std::string diagnostic_window;
};

struct ExperimentByzantineOptions final
{
    bool enabled{false};
    ConfigurationId configuration;
    std::string diagnostic_window;
    std::optional<ReplicaID> false_report_target;
    bool omit_outbound_aggregate{false};
    std::size_t maximum_false_report_contexts{0};
    std::size_t maximum_omission_contexts{0};
};

class ExperimentByzantineAdapter final
{
public:
    explicit ExperimentByzantineAdapter(
        ExperimentByzantineOptions options = {});
    ~ExperimentByzantineAdapter();

    ExperimentByzantineAdapter(const ExperimentByzantineAdapter &) = delete;
    ExperimentByzantineAdapter &operator=(
        const ExperimentByzantineAdapter &) = delete;
    ExperimentByzantineAdapter(ExperimentByzantineAdapter &&) = delete;
    ExperimentByzantineAdapter &operator=(
        ExperimentByzantineAdapter &&) = delete;

    bool arm_false_report(
        const ExperimentByzantineContext &context,
        ReplicaID target);
    bool on_verified_response(
        const ExperimentByzantineContext &context,
        ReplicaID target) noexcept;
    bool should_retain_response_evidence(
        const ExperimentByzantineContext &context) const noexcept;
    bool consume_false_timeout(
        const ExperimentByzantineContext &context,
        ReplicaID target) noexcept;
    bool consume_outbound_aggregate(
        const ExperimentByzantineContext &context);

private:
    struct State;
    std::unique_ptr<State> state_;
};

} // namespace hotstuff
#endif

namespace
{

using hotstuff::ConfigurationId;
using hotstuff::DataStream;
using hotstuff::ExperimentByzantineAdapter;
using hotstuff::ExperimentByzantineContext;
using hotstuff::ExperimentByzantineOptions;
using hotstuff::ProposalKey;
using hotstuff::ReplicaID;
using hotstuff::uint256_t;

uint256_t digest(const std::string &label)
{
    return DataStream(label).get_hash();
}

ConfigurationId configuration(
    std::uint32_t epoch = 7,
    std::uint32_t tree = 3,
    const std::string &label = "diagnostic-epoch")
{
    return ConfigurationId{epoch, tree, digest(label)};
}

ExperimentByzantineContext context(
    const std::string &block,
    const ConfigurationId &exact_configuration = configuration(),
    const std::string &window = "diagnostic-window-1")
{
    return ExperimentByzantineContext{
        ProposalKey{exact_configuration, digest(block)},
        window};
}

ExperimentByzantineOptions enabled_options()
{
    ExperimentByzantineOptions options;
    options.enabled = true;
    options.configuration = configuration();
    options.diagnostic_window = "diagnostic-window-1";
    options.false_report_target = ReplicaID{4};
    options.omit_outbound_aggregate = true;
    options.maximum_false_report_contexts = 2;
    options.maximum_omission_contexts = 2;
    return options;
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

std::string source_slice(
    const std::string &implementation,
    const std::string &start,
    const std::string &next)
{
    const auto begin = implementation.find(start);
    REQUIRE(begin != std::string::npos);
    const auto end = implementation.find(
        next, begin + start.size());
    REQUIRE(end != std::string::npos);
    return implementation.substr(begin, end - begin);
}

std::size_t occurrences(
    const std::string &text,
    const std::string &token)
{
    std::size_t count = 0;
    std::size_t offset = 0;
    while ((offset = text.find(token, offset)) != std::string::npos)
    {
        ++count;
        offset += token.size();
    }
    return count;
}

} // namespace

TEST_CASE(
    "experiment Byzantine adapter is inert unless explicitly configured",
    "[adaptive-v2][experiment][byzantine][disabled]")
{
    ExperimentByzantineAdapter adapter;
    const auto exact = context("disabled");

    CHECK_FALSE(adapter.arm_false_report(exact, 4));
    CHECK_FALSE(adapter.on_verified_response(exact, 4));
    CHECK_FALSE(adapter.consume_false_timeout(exact, 4));
    CHECK_FALSE(adapter.consume_outbound_aggregate(exact));
}

TEST_CASE(
    "authenticated false report suppresses only the positive and waits for the deadline",
    "[adaptive-v2][experiment][byzantine][false-report]")
{
    ExperimentByzantineAdapter adapter(enabled_options());
    const auto exact = context("false-report");

    REQUIRE(adapter.arm_false_report(exact, 4));
    CHECK_FALSE(adapter.should_retain_response_evidence(exact));
    CHECK_FALSE(adapter.consume_false_timeout(exact, 4));

    // The verified contribution continues through consensus. This return
    // value controls only the separate positive evidence observation.
    CHECK(adapter.on_verified_response(exact, 4));
    CHECK(adapter.should_retain_response_evidence(exact));

    // The runtime invokes this method from its real deadline callback. Exactly
    // one evidence-only false timeout is consumed for the exact context.
    CHECK(adapter.consume_false_timeout(exact, 4));
    CHECK_FALSE(adapter.should_retain_response_evidence(exact));
    CHECK_FALSE(adapter.consume_false_timeout(exact, 4));
    CHECK(adapter.on_verified_response(exact, 4));
}

TEST_CASE(
    "false reporting is exact to target configuration window and bounded contexts",
    "[adaptive-v2][experiment][byzantine][false-report][isolation]")
{
    ExperimentByzantineAdapter adapter(enabled_options());
    const auto wrong_target = context("wrong-target");
    const auto wrong_configuration = context(
        "wrong-configuration",
        configuration(8, 3, "other-epoch"));
    const auto wrong_window = context(
        "wrong-window",
        configuration(),
        "diagnostic-window-2");

    CHECK_FALSE(adapter.arm_false_report(wrong_target, 5));
    CHECK_FALSE(adapter.arm_false_report(wrong_configuration, 4));
    CHECK_FALSE(adapter.arm_false_report(wrong_window, 4));

    const auto first = context("bounded-first");
    const auto second = context("bounded-second");
    const auto over_limit = context("bounded-third");
    REQUIRE(adapter.arm_false_report(first, 4));
    REQUIRE(adapter.arm_false_report(second, 4));
    CHECK_FALSE(adapter.arm_false_report(over_limit, 4));

    CHECK(adapter.on_verified_response(first, 4));
    CHECK(adapter.consume_false_timeout(first, 4));
    CHECK_FALSE(adapter.on_verified_response(wrong_configuration, 4));
    CHECK_FALSE(adapter.consume_false_timeout(wrong_window, 4));
}

TEST_CASE(
    "persistent omitter consumes one aggregate per exact bounded context",
    "[adaptive-v2][experiment][byzantine][omission]")
{
    ExperimentByzantineAdapter adapter(enabled_options());
    const auto first = context("omission-first");
    const auto second = context("omission-second");
    const auto over_limit = context("omission-third");

    // True means the experiment consumed the outbound aggregate. The caller
    // treats that as an accepted send and therefore schedules no retry.
    CHECK(adapter.consume_outbound_aggregate(first));
    // Once selected, the exact context remains omitted so late or delta
    // aggregates cannot escape after the first dropped send.
    CHECK(adapter.consume_outbound_aggregate(first));
    CHECK(adapter.consume_outbound_aggregate(second));
    CHECK_FALSE(adapter.consume_outbound_aggregate(over_limit));

    CHECK_FALSE(adapter.consume_outbound_aggregate(context(
        "wrong-configuration",
        configuration(8, 3, "other-epoch"))));
    CHECK_FALSE(adapter.consume_outbound_aggregate(context(
        "wrong-window",
        configuration(),
        "diagnostic-window-2")));
}

TEST_CASE(
    "experiment Byzantine ground truth has no adaptation-manager seam",
    "[adaptive-v2][experiment][byzantine][manager-isolation]")
{
    static_assert(std::is_same_v<
                  decltype(std::declval<ExperimentByzantineAdapter &>()
                               .on_verified_response(
                                   std::declval<
                                       const ExperimentByzantineContext &>(),
                                   std::declval<ReplicaID>())),
                  bool>);
    static_assert(std::is_same_v<
                  decltype(std::declval<ExperimentByzantineAdapter &>()
                               .consume_outbound_aggregate(
                                   std::declval<
                                       const ExperimentByzantineContext &>())),
                  bool>);

    const auto manager = source("examples/adaptation_manager.cpp");
    CHECK(manager.find("ExperimentByzantineAdapter") == std::string::npos);
    CHECK(
        manager.find("experiment-false-report") ==
        std::string::npos);
    CHECK(
        manager.find("experiment-omit-outbound-aggregate") ==
        std::string::npos);
}

TEST_CASE(
    "replica CLI keeps Byzantine injection disabled unless explicitly configured",
    "[adaptive-v2][experiment][byzantine][runtime][cli]")
{
    const auto application = source("examples/hotstuff_app.cpp");
    const auto declarations = source_slice(
        application,
        "auto opt_blk_size",
        "config.add_opt(\"block-size\"");

    CHECK(
        declarations.find(
            "opt_experiment_byzantine_configuration") !=
        std::string::npos);
    CHECK(
        declarations.find(
            "opt_experiment_byzantine_window") !=
        std::string::npos);
    CHECK(
        declarations.find(
            "opt_experiment_false_report_target") !=
        std::string::npos);
    CHECK(
        declarations.find(
            "opt_experiment_omit_outbound_aggregate") !=
        std::string::npos);
    CHECK(
        declarations.find(
            "opt_experiment_byzantine_context_limit") !=
        std::string::npos);
    CHECK(
        declarations.find("Config::OptValStr::create(\"\")") !=
        std::string::npos);
    CHECK(
        declarations.find("Config::OptValFlag::create(false)") !=
        std::string::npos);
    CHECK(
        declarations.find("Config::OptValInt::create(0)") !=
        std::string::npos);

    CHECK(
        application.find("\"experiment-byzantine-configuration\"") !=
        std::string::npos);
    CHECK(
        application.find("\"experiment-byzantine-window\"") !=
        std::string::npos);
    CHECK(
        application.find("\"experiment-false-report-target\"") !=
        std::string::npos);
    CHECK(
        application.find("\"experiment-omit-outbound-aggregate\"") !=
        std::string::npos);
    CHECK(
        application.find("\"experiment-byzantine-context-limit\"") !=
        std::string::npos);

    const auto configuration = source_slice(
        application,
        "papp->set_aggregation_timeout",
        "HOTSTUFF_LOG_INFO(\"*** thread info ***\")");
    const auto guard = configuration.find(
        "if (experiment_byzantine_options.has_value())");
    const auto configure = configuration.find(
        "configure_experiment_byzantine");
    REQUIRE(guard != std::string::npos);
    REQUIRE(configure != std::string::npos);
    CHECK(guard < configure);
}

TEST_CASE(
    "runtime false report changes evidence only at exact response deadlines",
    "[adaptive-v2][experiment][byzantine][runtime][false-report]")
{
    const auto header = source("include/hotstuff/hotstuff.h");
    const auto implementation = source("src/hotstuff.cpp");

    CHECK(
        header.find(
            "#include \"hotstuff/experiment_byzantine_adapter.h\"") !=
        std::string::npos);
    CHECK(
        header.find(
            "std::unique_ptr<ExperimentByzantineAdapter>") !=
        std::string::npos);
    CHECK(
        header.find(
            "configure_experiment_byzantine") !=
        std::string::npos);
    CHECK(
        header.find(
            "schedule_experiment_false_timeout") !=
        std::string::npos);

    const auto deadline_arm = source_slice(
        implementation,
        "void HotStuffBase::start_latency_deadline",
        "void HotStuffBase::start_aggregation_timer");
    const auto expected_targets =
        deadline_arm.find("lease->tree().direct_children");
    const auto arm = deadline_arm.find("arm_false_report");
    const auto schedule =
        deadline_arm.find("schedule_experiment_false_timeout");
    REQUIRE(expected_targets != std::string::npos);
    REQUIRE(arm != std::string::npos);
    REQUIRE(schedule != std::string::npos);
    CHECK(expected_targets < arm);
    CHECK(arm < schedule);
    CHECK(
        deadline_arm.find("KAURI_FAULT false_report_armed") !=
        std::string::npos);

    const auto contribution = source_slice(
        implementation,
        "void HotStuffBase::continue_exact_contribution",
        "bool HotStuffBase::publish_exact_root_qc");
    const auto accepted = contribution.find("if (!accepted)");
    const auto suppress =
        contribution.find("on_verified_response");
    const auto positive =
        contribution.find("record_verified_response");
    REQUIRE(accepted != std::string::npos);
    REQUIRE(suppress != std::string::npos);
    REQUIRE(positive != std::string::npos);
    CHECK(accepted < suppress);
    CHECK(suppress < positive);
    CHECK(
        contribution.find("suppress_positive_observation") !=
        std::string::npos);
    CHECK(
        contribution.find(
            "KAURI_FAULT false_report_positive_suppressed") !=
        std::string::npos);
    CHECK(
        contribution.find("consume_false_timeout") ==
        std::string::npos);

    const auto cleanup = source_slice(
        implementation,
        "void HotStuffBase::purge_pending_exact_contributions",
        "promise_t HotStuffBase::deliver_exact_contribution");
    const auto retain =
        cleanup.find("should_retain_response_evidence");
    const auto retire = cleanup.find(
        "adaptive_v2_response_evidence->retire");
    REQUIRE(retain != std::string::npos);
    REQUIRE(retire != std::string::npos);
    CHECK(retain < retire);

    const auto false_timeout = source_slice(
        implementation,
        "void HotStuffBase::schedule_experiment_false_timeout",
        "void HotStuffBase::start_aggregation_timer");
    const auto real_deadline =
        false_timeout.find("aggregation_scheduler->schedule_after");
    const auto consume =
        false_timeout.find("consume_false_timeout");
    const auto record = false_timeout.find("record_timeouts");
    const auto deadline_retire = false_timeout.find("->retire(key)");
    REQUIRE(real_deadline != std::string::npos);
    REQUIRE(consume != std::string::npos);
    REQUIRE(record != std::string::npos);
    REQUIRE(deadline_retire != std::string::npos);
    CHECK(real_deadline < consume);
    CHECK(consume < record);
    CHECK(record < deadline_retire);
    CHECK(
        false_timeout.find("KAURI_FAULT false_timeout_emitted") !=
        std::string::npos);
    CHECK(occurrences(implementation, "consume_false_timeout") == 1);
}

TEST_CASE(
    "runtime aggregate omission completes the claim without transport or retry",
    "[adaptive-v2][experiment][byzantine][runtime][omission]")
{
    const auto implementation = source("src/hotstuff.cpp");
    const auto aggregation = source("src/aggregation.cpp");
    const auto coordinator = source_slice(
        implementation,
        "void HotStuffBase::rebuild_aggregation_timeout_coordinator",
        "void HotStuffBase::set_aggregation_timeout");

    const auto consume =
        coordinator.find("consume_outbound_aggregate");
    const auto audit =
        coordinator.find("KAURI_FAULT aggregate_omitted");
    const auto transport = coordinator.find("send_exact_relay");
    const auto retry =
        coordinator.find("schedule_exact_forwarding_retry");
    REQUIRE(consume != std::string::npos);
    REQUIRE(audit != std::string::npos);
    REQUIRE(transport != std::string::npos);
    REQUIRE(retry != std::string::npos);
    CHECK(consume < audit);
    CHECK(audit < transport);
    CHECK(transport < retry);

    const auto omitted_branch =
        coordinator.substr(consume, transport - consume);
    CHECK(omitted_branch.find("return true") != std::string::npos);

    const auto timeout_application = source_slice(
        aggregation,
        "void AggregationTimeoutCoordinator::apply_timeout",
        "} // namespace hotstuff");
    const auto send =
        timeout_application.find("effects_.try_send_upward");
    const auto accepted =
        timeout_application.find("if (enqueued)");
    const auto committed =
        timeout_application.find("commit_forwarding_claim");
    REQUIRE(send != std::string::npos);
    REQUIRE(accepted != std::string::npos);
    REQUIRE(committed != std::string::npos);
    CHECK(send < accepted);
    CHECK(accepted < committed);

    const auto manager = source("examples/adaptation_manager.cpp");
    CHECK(manager.find("KAURI_FAULT") == std::string::npos);
    CHECK(
        manager.find("experiment-byzantine") ==
        std::string::npos);
    CHECK(
        manager.find("ExperimentByzantineAdapter") ==
        std::string::npos);
}
