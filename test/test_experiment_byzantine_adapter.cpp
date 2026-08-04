#include <algorithm>
#include <cstddef>
#include <cstdint>
#include <ctime>
#include <fstream>
#include <iterator>
#include <map>
#include <memory>
#include <optional>
#include <sstream>
#include <stdexcept>
#include <string>
#include <type_traits>
#include <vector>

#include "catch.hpp"
#include "hotstuff/configuration.h"
#include "hotstuff/hotstuff.h"

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
 * The static diagnosis is deliberately constrained to two modes: signer
 * inclusion cryptographically proves a false report, while signer exclusion
 * identifies the target omission only within that frozen two-mode model, not
 * arbitrary Byzantine attribution.
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

enum class ExperimentDirectVoteDisposition
{
    forward,
    omit_first,
    omit_repeat
};

struct ExperimentByzantineOptions final
{
    bool enabled{false};
    ConfigurationId configuration;
    std::optional<ConfigurationId> additional_omission_configuration;
    std::string diagnostic_window;
    std::optional<ReplicaID> false_report_target;
    bool omit_outbound_aggregate{false};
    bool omit_outbound_direct_vote{false};
    std::size_t maximum_false_report_contexts{0};
    std::size_t maximum_omission_contexts{0};
    std::size_t maximum_direct_vote_omission_contexts{0};
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
    bool consume_false_report_positive_marker(
        const ExperimentByzantineContext &context,
        ReplicaID target) noexcept;
    bool should_retain_response_evidence(
        const ExperimentByzantineContext &context) const noexcept;
    bool cancel_false_report(
        const ExperimentByzantineContext &context,
        ReplicaID target) noexcept;
    bool consume_false_timeout(
        const ExperimentByzantineContext &context,
        ReplicaID target) noexcept;
    bool consume_outbound_aggregate(
        const ExperimentByzantineContext &context);
    ExperimentDirectVoteDisposition consume_outbound_direct_vote(
        const ExperimentByzantineContext &context);
    bool outbound_direct_vote_omitted(
        const ExperimentByzantineContext &context) const noexcept;

private:
    struct State;
    std::unique_ptr<State> state_;
};

} // namespace hotstuff
#endif

namespace hotstuff
{

class ExperimentFalseTimeoutFenceTestAccess final
{
public:
    static bool scheduling_failure_reports_normally()
    {
        HotStuffBase::ExperimentFalseTimeoutState state{
            ReplicaID{4}, false, false};
        return !state.may_suppress(ReplicaID{4}) &&
               state.observe_commit(true) ==
                   HotStuffBase::ExperimentFalseTimeoutCommitAction::
                       report_now;
    }

    static bool commit_before_timeout_releases_once()
    {
        HotStuffBase::ExperimentFalseTimeoutState state{
            ReplicaID{4}, true, false};
        const auto first = state.observe_commit(true);
        const auto duplicate = state.observe_commit(true);
        const auto completion = state.complete(true);
        return first ==
                   HotStuffBase::ExperimentFalseTimeoutCommitAction::
                       deferred &&
               duplicate ==
                   HotStuffBase::ExperimentFalseTimeoutCommitAction::
                       already_deferred &&
               completion ==
                   HotStuffBase::ExperimentFalseTimeoutCompletionAction::
                       release_commit;
    }

    static bool timeout_before_commit_needs_no_release()
    {
        HotStuffBase::ExperimentFalseTimeoutState state{
            ReplicaID{4}, true, false};
        const auto completion = state.complete(true);
        const auto later_commit = state.observe_commit(false);
        return completion ==
                   HotStuffBase::ExperimentFalseTimeoutCompletionAction::
                       no_deferred_commit &&
               later_commit ==
                   HotStuffBase::ExperimentFalseTimeoutCommitAction::
                       report_now;
    }

    static bool backpressure_fails_closed()
    {
        HotStuffBase::ExperimentFalseTimeoutState state{
            ReplicaID{4}, true, false};
        if (state.observe_commit(true) !=
            HotStuffBase::ExperimentFalseTimeoutCommitAction::deferred)
            return false;
        return state.complete(false) ==
               HotStuffBase::ExperimentFalseTimeoutCompletionAction::
                   fail_closed;
    }
};

} // namespace hotstuff

namespace
{

using hotstuff::ConfigurationId;
using hotstuff::DataStream;
using hotstuff::ExperimentByzantineAdapter;
using hotstuff::ExperimentByzantineContext;
using hotstuff::ExperimentByzantineOptions;
using hotstuff::ExperimentDirectVoteDisposition;
using hotstuff::ExperimentFalseTimeoutFenceTestAccess;
using hotstuff::ExperimentOmissionAction;
using hotstuff::ExperimentOmissionMarker;
using hotstuff::ExperimentReplicaRole;
using hotstuff::ExperimentRotatingOmissionOptions;
using hotstuff::ProposalKey;
using hotstuff::ReplicaID;
using hotstuff::format_experiment_omission_marker;
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
    options.maximum_false_report_contexts = 2;
    return options;
}

ExperimentByzantineOptions aggregate_omission_options()
{
    auto options = enabled_options();
    options.false_report_target.reset();
    options.maximum_false_report_contexts = 0;
    options.omit_outbound_aggregate = true;
    options.maximum_omission_contexts = 2;
    return options;
}

ExperimentByzantineOptions direct_vote_omission_options()
{
    auto options = enabled_options();
    options.false_report_target.reset();
    options.maximum_false_report_contexts = 0;
    options.omit_outbound_direct_vote = true;
    options.maximum_direct_vote_omission_contexts = 2;
    return options;
}

ExperimentByzantineOptions rotating_omission_options(
    ReplicaID local_replica,
    std::vector<ExperimentOmissionMarker> *markers = nullptr,
    std::vector<std::string> *encoded_markers = nullptr)
{
    ExperimentByzantineOptions options;
    options.enabled = true;
    options.diagnostic_window = "factorial-window-1";
    options.rotating_omission = ExperimentRotatingOmissionOptions{
        "rotating_intermittent_omission_v1",
        local_replica,
        7,
        {1, 3},
        2,
        100,
        200,
        1,
        100'000};
    if (markers != nullptr || encoded_markers != nullptr)
        options.omission_marker_emitter =
            [markers, encoded_markers](
                const ExperimentOmissionMarker &marker)
            {
                if (markers != nullptr)
                    markers->push_back(marker);
                if (encoded_markers != nullptr)
                    encoded_markers->push_back(
                        format_experiment_omission_marker(marker));
            };
    return options;
}

ExperimentByzantineOptions persistent_omission_options(
    ReplicaID local_replica,
    std::vector<ExperimentOmissionMarker> *markers = nullptr,
    std::vector<std::string> *encoded_markers = nullptr)
{
    auto options = rotating_omission_options(
        local_replica, markers, encoded_markers);
    options.rotating_omission->mode =
        "persistent_selected_omission_v1";
    options.rotating_omission->max_omissions_per_proposal =
        options.rotating_omission->actor_ids.size();
    return options;
}

ExperimentByzantineContext selected_context(
    const ExperimentByzantineAdapter &adapter,
    ReplicaID selected_actor,
    const std::string &label)
{
    for (std::uint32_t index = 0; index < 10'000; ++index)
    {
        const auto candidate = context(
            label + "-" + std::to_string(index),
            configuration(),
            "factorial-window-1");
        if (adapter.rotating_omission_actor(candidate.proposal) ==
            std::optional<ReplicaID>{selected_actor})
            return candidate;
    }
    throw std::runtime_error("could not find selected actor context");
}

std::map<std::string, std::string> parse_marker_fields(
    const std::string &encoded)
{
    const std::vector<std::string> expected_order{
        "fault",
        "proposal_epoch",
        "proposal_tree",
        "proposal_epoch_digest",
        "proposal_block_hash",
        "window",
        "window_start_monotonic_ns",
        "window_end_monotonic_ns",
        "actor",
        "action",
        "monotonic_ns"};
    std::istringstream input(encoded);
    std::map<std::string, std::string> fields;
    std::string token;
    if (!(input >> token) || token != "KAURI_FAULT")
        throw std::invalid_argument("missing KAURI_FAULT marker prefix");
    while (input >> token)
    {
        const auto separator = token.find('=');
        if (separator == std::string::npos || separator == 0 ||
            separator + 1 == token.size())
            throw std::invalid_argument("malformed marker field");
        const auto key = token.substr(0, separator);
        if (fields.size() >= expected_order.size() ||
            key != expected_order[fields.size()])
            throw std::invalid_argument("unexpected marker field order");
        if (!fields.emplace(
                key,
                token.substr(separator + 1))
                 .second)
            throw std::invalid_argument("duplicate marker field");
    }
    if (fields.size() != expected_order.size())
        throw std::invalid_argument("incomplete marker fields");
    return fields;
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
    CHECK_FALSE(adapter.consume_false_report_positive_marker(exact, 4));
    CHECK_FALSE(adapter.consume_false_timeout(exact, 4));
    CHECK_FALSE(adapter.consume_outbound_aggregate(exact));
    CHECK(
        adapter.consume_outbound_direct_vote(exact) ==
        ExperimentDirectVoteDisposition::forward);
    CHECK_FALSE(adapter.outbound_direct_vote_omitted(exact));
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
    CHECK(adapter.consume_false_report_positive_marker(exact, 4));
    CHECK_FALSE(adapter.consume_false_report_positive_marker(exact, 4));

    // Duplicate verified contributions remain suppressed without emitting a
    // duplicate ground-truth marker for the same exact context and window.
    CHECK(adapter.on_verified_response(exact, 4));
    CHECK_FALSE(adapter.consume_false_report_positive_marker(exact, 4));

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
    CHECK_FALSE(
        adapter.consume_false_report_positive_marker(wrong_target, 5));

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
    "cancelled false reporting restores the exact context capacity",
    "[adaptive-v2][experiment][byzantine][false-report][rollback]")
{
    ExperimentByzantineAdapter adapter(enabled_options());
    const auto first = context("cancel-first");
    const auto second = context("cancel-second");
    const auto replacement = context("cancel-replacement");

    REQUIRE(adapter.arm_false_report(first, 4));
    REQUIRE(adapter.arm_false_report(second, 4));
    CHECK_FALSE(adapter.arm_false_report(replacement, 4));

    CHECK_FALSE(adapter.cancel_false_report(first, 5));
    CHECK_FALSE(adapter.cancel_false_report(
        context(
            "cancel-first",
            configuration(),
            "diagnostic-window-2"),
        4));
    REQUIRE(adapter.cancel_false_report(first, 4));
    CHECK_FALSE(adapter.cancel_false_report(first, 4));
    CHECK_FALSE(adapter.on_verified_response(first, 4));
    CHECK(adapter.arm_false_report(replacement, 4));
}

TEST_CASE(
    "false timeout fence orders or fails closed without touching consensus",
    "[adaptive-v2][experiment][byzantine][false-report][commit-fence]")
{
    CHECK(
        ExperimentFalseTimeoutFenceTestAccess::
            scheduling_failure_reports_normally());
    CHECK(
        ExperimentFalseTimeoutFenceTestAccess::
            timeout_before_commit_needs_no_release());
    CHECK(
        ExperimentFalseTimeoutFenceTestAccess::
            commit_before_timeout_releases_once());
    CHECK(
        ExperimentFalseTimeoutFenceTestAccess::
            backpressure_fails_closed());
}

TEST_CASE(
    "persistent omitter consumes one aggregate per exact bounded context",
    "[adaptive-v2][experiment][byzantine][omission]")
{
    ExperimentByzantineAdapter role_guard(
        aggregate_omission_options());
    const auto guarded = context("aggregate-role-guard");
    CHECK_FALSE(role_guard.consume_outbound_aggregate(
        guarded, ExperimentReplicaRole::root));
    CHECK(role_guard.consume_outbound_aggregate(
        guarded, ExperimentReplicaRole::internal));

    ExperimentByzantineAdapter adapter(aggregate_omission_options());
    const auto first = context("omission-first");
    const auto second = context("omission-second");
    const auto over_limit = context("omission-third");

    // True means the experiment consumed the outbound aggregate. The caller
    // treats that as an accepted send and therefore schedules no retry.
    CHECK(adapter.consume_outbound_aggregate(first));
    CHECK(adapter.consume_outbound_aggregate_marker(first));
    CHECK_FALSE(adapter.consume_outbound_aggregate_marker(first));
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
    "persistent omission accepts one additional exact configuration only",
    "[adaptive-v2][experiment][byzantine][omission][crosscheck]")
{
    auto options = aggregate_omission_options();
    options.configuration = configuration(7, 6, "shared-epoch");
    options.additional_omission_configuration =
        configuration(7, 0, "shared-epoch");
    options.maximum_omission_contexts = 2;
    ExperimentByzantineAdapter adapter(options);

    const auto primary = context(
        "tree-6-aggregate",
        configuration(7, 6, "shared-epoch"));
    const auto followup = context(
        "tree-0-aggregate",
        configuration(7, 0, "shared-epoch"));

    // The additional tree is independently exact; it does not wait for the
    // primary tree to be omitted first.
    CHECK(adapter.consume_outbound_aggregate(followup));
    CHECK(adapter.consume_outbound_aggregate_marker(followup));
    CHECK_FALSE(adapter.consume_outbound_aggregate_marker(followup));
    // A retry remains consumed without spending another bounded context or
    // emitting another marker, so the primary tree can still be selected.
    CHECK(adapter.consume_outbound_aggregate(followup));
    CHECK_FALSE(adapter.consume_outbound_aggregate_marker(followup));
    CHECK(adapter.consume_outbound_aggregate(primary));
    CHECK(adapter.consume_outbound_aggregate_marker(primary));
    CHECK_FALSE(adapter.consume_outbound_aggregate_marker(primary));

    // Aggregate omission is the adapter's only configured fault mode.
    CHECK_FALSE(adapter.arm_false_report(primary, 4));
    CHECK_FALSE(adapter.arm_false_report(followup, 4));
    CHECK_FALSE(adapter.on_verified_response(followup, 4));
    CHECK_FALSE(adapter.consume_false_timeout(followup, 4));

    CHECK_FALSE(adapter.consume_outbound_aggregate(context(
        "wrong-epoch",
        configuration(8, 0, "shared-epoch"))));
    CHECK_FALSE(adapter.consume_outbound_aggregate(context(
        "wrong-digest",
        configuration(7, 0, "other-epoch"))));
    CHECK_FALSE(adapter.consume_outbound_aggregate(context(
        "wrong-tree",
        configuration(7, 1, "shared-epoch"))));
    CHECK_FALSE(adapter.consume_outbound_aggregate(context(
        "wrong-window",
        configuration(7, 0, "shared-epoch"),
        "diagnostic-window-2")));
}

TEST_CASE(
    "additional omission configuration stays optional",
    "[adaptive-v2][experiment][byzantine][omission][crosscheck]")
{
    auto options = aggregate_omission_options();
    options.configuration = configuration(7, 6, "shared-epoch");
    ExperimentByzantineAdapter adapter(options);

    CHECK(adapter.consume_outbound_aggregate(context(
        "tree-6-aggregate",
        configuration(7, 6, "shared-epoch"))));
    CHECK_FALSE(adapter.consume_outbound_aggregate(context(
        "tree-0-aggregate",
        configuration(7, 0, "shared-epoch"))));
}

TEST_CASE(
    "direct-vote omission is exact bounded and persistent across retries",
    "[adaptive-v2][experiment][byzantine][direct-vote][ordering]")
{
    ExperimentByzantineAdapter role_guard(
        direct_vote_omission_options());
    const auto guarded = context("direct-role-guard");
    CHECK(
        role_guard.consume_outbound_direct_vote(
            guarded, ExperimentReplicaRole::internal) ==
        ExperimentDirectVoteDisposition::forward);
    CHECK_FALSE(role_guard.outbound_direct_vote_omitted(guarded));
    CHECK(
        role_guard.consume_outbound_direct_vote(
            guarded, ExperimentReplicaRole::leaf) ==
        ExperimentDirectVoteDisposition::omit_first);
    CHECK(role_guard.outbound_direct_vote_omitted(guarded));

    ExperimentByzantineAdapter adapter(direct_vote_omission_options());
    const auto first = context("direct-first");
    const auto second = context("direct-second");
    const auto over_limit = context("direct-third");

    CHECK_FALSE(adapter.outbound_direct_vote_omitted(first));
    CHECK(
        adapter.consume_outbound_direct_vote(context(
            "wrong-configuration",
            configuration(8, 3, "other-epoch"))) ==
        ExperimentDirectVoteDisposition::forward);
    CHECK(
        adapter.consume_outbound_direct_vote(context(
            "wrong-window",
            configuration(),
            "diagnostic-window-2")) ==
        ExperimentDirectVoteDisposition::forward);

    CHECK(
        adapter.consume_outbound_direct_vote(first) ==
        ExperimentDirectVoteDisposition::omit_first);
    CHECK(adapter.outbound_direct_vote_omitted(first));
    // A parent retry or a direct-to-root fallback for the same exact proposal
    // remains suppressed without claiming a second audit marker.
    CHECK(
        adapter.consume_outbound_direct_vote(first) ==
        ExperimentDirectVoteDisposition::omit_repeat);
    CHECK(
        adapter.consume_outbound_direct_vote(first) ==
        ExperimentDirectVoteDisposition::omit_repeat);

    CHECK(
        adapter.consume_outbound_direct_vote(second) ==
        ExperimentDirectVoteDisposition::omit_first);
    CHECK(
        adapter.consume_outbound_direct_vote(over_limit) ==
        ExperimentDirectVoteDisposition::forward);
    CHECK_FALSE(adapter.outbound_direct_vote_omitted(over_limit));
}

TEST_CASE(
    "direct-vote omission construction rejects ambiguous fault modes",
    "[adaptive-v2][experiment][byzantine][direct-vote][validation]")
{
    auto missing_bound = direct_vote_omission_options();
    missing_bound.maximum_direct_vote_omission_contexts = 0;
    CHECK_THROWS_AS(
        ExperimentByzantineAdapter(missing_bound),
        std::invalid_argument);

    auto with_false_report = direct_vote_omission_options();
    with_false_report.false_report_target = ReplicaID{4};
    with_false_report.maximum_false_report_contexts = 2;
    CHECK_THROWS_AS(
        ExperimentByzantineAdapter(with_false_report),
        std::invalid_argument);

    auto with_aggregate = direct_vote_omission_options();
    with_aggregate.omit_outbound_aggregate = true;
    with_aggregate.maximum_omission_contexts = 2;
    CHECK_THROWS_AS(
        ExperimentByzantineAdapter(with_aggregate),
        std::invalid_argument);

    auto with_additional = direct_vote_omission_options();
    with_additional.additional_omission_configuration =
        configuration(7, 0, "diagnostic-epoch");
    CHECK_THROWS_AS(
        ExperimentByzantineAdapter(with_additional),
        std::invalid_argument);
}

TEST_CASE(
    "direct-vote omission forwards non-leaves and omits a leaf once",
    "[adaptive-v2][experiment][byzantine][direct-vote][role]")
{
    ExperimentByzantineAdapter adapter(direct_vote_omission_options());
    const auto root = context("static-root");
    const auto internal = context("static-internal");
    const auto leaf = context("static-leaf");

    CHECK(
        adapter.consume_outbound_direct_vote(
            root, ExperimentReplicaRole::root, 1) ==
        ExperimentDirectVoteDisposition::forward);
    CHECK(
        adapter.consume_outbound_direct_vote(
            internal, ExperimentReplicaRole::internal, 1) ==
        ExperimentDirectVoteDisposition::forward);
    CHECK(
        adapter.consume_outbound_direct_vote(
            leaf, ExperimentReplicaRole::leaf, 1) ==
        ExperimentDirectVoteDisposition::omit_first);
    CHECK(
        adapter.consume_outbound_direct_vote(
            leaf, ExperimentReplicaRole::leaf, 2) ==
        ExperimentDirectVoteDisposition::omit_repeat);
}

TEST_CASE(
    "rotating omission configuration is exact bounded and mutually exclusive",
    "[adaptive-v2][experiment][byzantine][rotating][validation]")
{
    auto wrong_mode = rotating_omission_options(1);
    wrong_mode.rotating_omission->mode = "other";
    CHECK_THROWS_AS(
        ExperimentByzantineAdapter(wrong_mode),
        std::invalid_argument);

    auto count_mismatch = rotating_omission_options(1);
    count_mismatch.rotating_omission->expected_actor_count = 3;
    CHECK_THROWS_AS(
        ExperimentByzantineAdapter(count_mismatch),
        std::invalid_argument);

    auto bounded_minority = rotating_omission_options(1);
    bounded_minority.rotating_omission->actor_ids = {1};
    bounded_minority.rotating_omission->expected_actor_count = 1;
    CHECK_NOTHROW(ExperimentByzantineAdapter(bounded_minority));

    auto derived_count_mismatch = rotating_omission_options(1);
    derived_count_mismatch.rotating_omission->actor_ids = {1, 3, 5};
    derived_count_mismatch.rotating_omission->expected_actor_count = 3;
    CHECK_THROWS_AS(
        ExperimentByzantineAdapter(derived_count_mismatch),
        std::invalid_argument);

    auto duplicate = rotating_omission_options(1);
    duplicate.rotating_omission->actor_ids = {1, 1};
    CHECK_THROWS_AS(
        ExperimentByzantineAdapter(duplicate),
        std::invalid_argument);

    auto out_of_range = rotating_omission_options(1);
    out_of_range.rotating_omission->actor_ids = {1, 7};
    CHECK_THROWS_AS(
        ExperimentByzantineAdapter(out_of_range),
        std::invalid_argument);

    auto zero_start = rotating_omission_options(1);
    zero_start.rotating_omission->window_start_monotonic_ns = 0;
    CHECK_THROWS_AS(
        ExperimentByzantineAdapter(zero_start),
        std::invalid_argument);

    auto inverted_window = rotating_omission_options(1);
    inverted_window.rotating_omission->window_end_monotonic_ns = 100;
    CHECK_THROWS_AS(
        ExperimentByzantineAdapter(inverted_window),
        std::invalid_argument);

    auto multiple_omissions = rotating_omission_options(1);
    multiple_omissions.rotating_omission->max_omissions_per_proposal = 2;
    CHECK_THROWS_AS(
        ExperimentByzantineAdapter(multiple_omissions),
        std::invalid_argument);

    auto zero_context_bound = rotating_omission_options(1);
    zero_context_bound.rotating_omission->maximum_contexts = 0;
    CHECK_THROWS_AS(
        ExperimentByzantineAdapter(zero_context_bound),
        std::invalid_argument);

    auto ambiguous = rotating_omission_options(1);
    ambiguous.omit_outbound_aggregate = true;
    ambiguous.maximum_omission_contexts = 1;
    CHECK_THROWS_AS(
        ExperimentByzantineAdapter(ambiguous),
        std::invalid_argument);
}

TEST_CASE(
    "rotating actor selection matches frozen cross-language FNV-1a vectors",
    "[adaptive-v2][experiment][byzantine][rotating][fnv1a]")
{
    struct Vector
    {
        std::uint32_t epoch;
        std::uint32_t tree;
        const char *epoch_digest;
        const char *block_hash;
        std::vector<ReplicaID> actors;
        ReplicaID selected;
    };
    const std::vector<Vector> vectors{
        {0,
         0,
         "0000000000000000000000000000000000000000000000000000000000000000",
         "0000000000000000000000000000000000000000000000000000000000000000",
         {1, 3},
         3},
        {7,
         3,
         "0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef",
         "fedcba9876543210fedcba9876543210fedcba9876543210fedcba9876543210",
         {1, 3},
         3},
        {0xffff'ffff,
         0x1020'3040,
         "ffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffff",
         "000102030405060708090a0b0c0d0e0f101112131415161718191a1b1c1d1e1f",
         {2, 5, 8, 11},
         5},
    };

    for (const auto &vector : vectors)
    {
        auto options = rotating_omission_options(vector.actors.front());
        options.rotating_omission->replica_count =
            vector.actors.size() == 2 ? 7 : 13;
        options.rotating_omission->actor_ids = vector.actors;
        options.rotating_omission->expected_actor_count =
            vector.actors.size();
        ExperimentByzantineAdapter adapter(std::move(options));
        const ProposalKey proposal{
            ConfigurationId{
                vector.epoch,
                vector.tree,
                uint256_t(hotstuff::from_hex(vector.epoch_digest))},
            uint256_t(hotstuff::from_hex(vector.block_hash))};
        CHECK(adapter.rotating_omission_actor(proposal) ==
              std::optional<ReplicaID>{vector.selected});
    }
}

TEST_CASE(
    "proposal-context rotation is deterministic and independent of input order",
    "[adaptive-v2][experiment][byzantine][rotating][selection]")
{
    auto reversed = rotating_omission_options(1);
    reversed.rotating_omission->actor_ids = {3, 1};
    ExperimentByzantineAdapter first(rotating_omission_options(1));
    ExperimentByzantineAdapter second(reversed);
    std::vector<ReplicaID> observed;

    for (std::uint32_t index = 0; index < 128; ++index)
    {
        const auto proposal = context(
            "rotation-" + std::to_string(index)).proposal;
        const auto first_actor = first.rotating_omission_actor(proposal);
        const auto second_actor = second.rotating_omission_actor(proposal);
        REQUIRE(first_actor.has_value());
        CHECK(first_actor == second_actor);
        observed.push_back(*first_actor);
    }
    CHECK(std::find(observed.begin(), observed.end(), ReplicaID{1}) !=
          observed.end());
    CHECK(std::find(observed.begin(), observed.end(), ReplicaID{3}) !=
          observed.end());
}

TEST_CASE(
    "rotating omission obeys window role selection retry and marker contracts",
    "[adaptive-v2][experiment][byzantine][rotating][behavior]")
{
    std::vector<ExperimentOmissionMarker> markers;
    std::vector<std::string> encoded_markers;
    ExperimentByzantineAdapter actor_one(
        rotating_omission_options(1, &markers, &encoded_markers));

    ExperimentByzantineAdapter exact_window(
        rotating_omission_options(1));
    const auto exact = selected_context(exact_window, 1, "exact-window");
    auto wrong_window = exact;
    wrong_window.diagnostic_window = "other-window";
    CHECK(
        exact_window.consume_outbound_direct_vote(
            wrong_window, ExperimentReplicaRole::leaf, 150) ==
        ExperimentDirectVoteDisposition::forward);
    CHECK(
        exact_window.consume_outbound_direct_vote(
            exact, ExperimentReplicaRole::leaf, 150) ==
        ExperimentDirectVoteDisposition::omit_first);

    const auto before = selected_context(actor_one, 1, "before");
    CHECK(
        actor_one.consume_outbound_direct_vote(
            before, ExperimentReplicaRole::leaf, 99) ==
        ExperimentDirectVoteDisposition::forward);
    // The first exact-key decision is stable even after the window opens.
    CHECK(
        actor_one.consume_outbound_direct_vote(
            before, ExperimentReplicaRole::leaf, 150) ==
        ExperimentDirectVoteDisposition::forward);

    const auto after = selected_context(actor_one, 1, "after");
    CHECK(
        actor_one.consume_outbound_direct_vote(
            after, ExperimentReplicaRole::leaf, 200) ==
        ExperimentDirectVoteDisposition::forward);

    const auto root = selected_context(actor_one, 1, "root");
    CHECK_FALSE(actor_one.consume_outbound_aggregate(
        root, ExperimentReplicaRole::root, 150));
    // A retry through another seam cannot turn the cached root decision into
    // an omission.
    CHECK(
        actor_one.consume_outbound_direct_vote(
            root, ExperimentReplicaRole::leaf, 151) ==
        ExperimentDirectVoteDisposition::forward);

    const auto internal = selected_context(actor_one, 1, "internal");
    CHECK(actor_one.consume_outbound_aggregate(
        internal, ExperimentReplicaRole::internal, 100));
    REQUIRE(markers.size() == 1);
    REQUIRE(encoded_markers.size() == 1);
    CHECK(actor_one.consume_outbound_aggregate(
        internal, ExperimentReplicaRole::internal, 101));
    CHECK(markers.size() == 1);
    CHECK(encoded_markers.size() == 1);
    CHECK(
        actor_one.consume_outbound_direct_vote(
            internal, ExperimentReplicaRole::leaf, 102) ==
        ExperimentDirectVoteDisposition::forward);
    CHECK_FALSE(actor_one.outbound_direct_vote_omitted(internal));
    CHECK(markers.size() == 1);
    CHECK(encoded_markers.size() == 1);

    const auto leaf = selected_context(actor_one, 1, "leaf");
    CHECK(
        actor_one.consume_outbound_direct_vote(
            leaf, ExperimentReplicaRole::leaf, 160) ==
        ExperimentDirectVoteDisposition::omit_first);
    REQUIRE(markers.size() == 2);
    REQUIRE(encoded_markers.size() == 2);
    CHECK(
        actor_one.consume_outbound_direct_vote(
            leaf, ExperimentReplicaRole::leaf, 161) ==
        ExperimentDirectVoteDisposition::omit_repeat);
    CHECK(markers.size() == 2);
    CHECK(encoded_markers.size() == 2);
    CHECK(actor_one.outbound_direct_vote_omitted(leaf));
    CHECK_FALSE(actor_one.consume_outbound_aggregate(
        leaf, ExperimentReplicaRole::internal, 162));
    CHECK(markers.size() == 2);
    CHECK(encoded_markers.size() == 2);

    CHECK(markers[0].proposal == internal.proposal);
    CHECK(markers[0].diagnostic_window == "factorial-window-1");
    CHECK(markers[0].actor == 1);
    CHECK(markers[0].action == ExperimentOmissionAction::omit_aggregate);
    CHECK(markers[0].window_start_monotonic_ns == 100);
    CHECK(markers[0].window_end_monotonic_ns == 200);
    CHECK(markers[0].monotonic_ns == 100);
    CHECK(markers[1].proposal == leaf.proposal);
    CHECK(markers[1].actor == 1);
    CHECK(markers[1].action == ExperimentOmissionAction::omit_direct_vote);
    CHECK(markers[1].monotonic_ns == 160);
    CHECK(markers[0].monotonic_ns < markers[1].monotonic_ns);
    CHECK(markers[0].monotonic_ns >=
          markers[0].window_start_monotonic_ns);
    CHECK(markers[1].monotonic_ns < markers[1].window_end_monotonic_ns);

    const auto aggregate_fields =
        parse_marker_fields(encoded_markers[0]);
    CHECK(
        aggregate_fields.at("fault") ==
        "rotating_intermittent_omission_v1");
    CHECK(aggregate_fields.at("proposal_epoch") == "7");
    CHECK(aggregate_fields.at("proposal_tree") == "3");
    CHECK(
        aggregate_fields.at("proposal_epoch_digest") ==
        hotstuff::get_hex(internal.proposal.configuration.epoch_digest));
    CHECK(
        aggregate_fields.at("proposal_block_hash") ==
        hotstuff::get_hex(internal.proposal.block_hash));
    CHECK(aggregate_fields.at("window") == "factorial-window-1");
    CHECK(aggregate_fields.at("window_start_monotonic_ns") == "100");
    CHECK(aggregate_fields.at("window_end_monotonic_ns") == "200");
    CHECK(aggregate_fields.at("actor") == "1");
    CHECK(aggregate_fields.at("action") == "omit_aggregate");
    CHECK(aggregate_fields.at("monotonic_ns") == "100");

    const auto direct_fields = parse_marker_fields(encoded_markers[1]);
    CHECK(direct_fields.at("proposal_epoch") == "7");
    CHECK(direct_fields.at("proposal_tree") == "3");
    CHECK(
        direct_fields.at("proposal_epoch_digest") ==
        hotstuff::get_hex(leaf.proposal.configuration.epoch_digest));
    CHECK(
        direct_fields.at("proposal_block_hash") ==
        hotstuff::get_hex(leaf.proposal.block_hash));
    CHECK(direct_fields.at("action") == "omit_direct_vote");
    CHECK(direct_fields.at("monotonic_ns") == "160");
}

TEST_CASE(
    "rotating omission leaves every nonselected actor correct",
    "[adaptive-v2][experiment][byzantine][rotating][nonselected]")
{
    ExperimentByzantineAdapter selector(rotating_omission_options(1));
    const auto selected = selected_context(selector, 1, "nonselected");
    ExperimentByzantineAdapter nonselected(rotating_omission_options(3));

    REQUIRE(nonselected.rotating_omission_actor(selected.proposal) ==
            std::optional<ReplicaID>{1});
    CHECK_FALSE(nonselected.consume_outbound_aggregate(
        selected, ExperimentReplicaRole::internal, 150));
    CHECK(
        nonselected.consume_outbound_direct_vote(
            selected, ExperimentReplicaRole::leaf, 150) ==
        ExperimentDirectVoteDisposition::forward);
}

TEST_CASE(
    "rotating context exhaustion forwards and emits one terminal marker",
    "[adaptive-v2][experiment][byzantine][rotating][capacity]")
{
    std::vector<ExperimentOmissionMarker> markers;
    std::vector<std::string> encoded_markers;
    auto options =
        rotating_omission_options(1, &markers, &encoded_markers);
    options.rotating_omission->maximum_contexts = 1;
    ExperimentByzantineAdapter adapter(std::move(options));

    const auto retained = selected_context(adapter, 1, "retained");
    REQUIRE(adapter.consume_outbound_aggregate(
        retained, ExperimentReplicaRole::internal, 150));
    CHECK(adapter.consume_outbound_aggregate(
        retained, ExperimentReplicaRole::internal, 151));

    const auto exhausted = selected_context(adapter, 1, "exhausted");
    CHECK(
        adapter.consume_outbound_direct_vote(
            exhausted, ExperimentReplicaRole::leaf, 152) ==
        ExperimentDirectVoteDisposition::forward);
    CHECK(
        adapter.consume_outbound_direct_vote(
            exhausted, ExperimentReplicaRole::leaf, 153) ==
        ExperimentDirectVoteDisposition::forward);
    const auto later = selected_context(adapter, 1, "later");
    CHECK_FALSE(adapter.consume_outbound_aggregate(
        later, ExperimentReplicaRole::internal, 154));

    REQUIRE(markers.size() == 2);
    REQUIRE(encoded_markers.size() == 2);
    CHECK(markers[0].action == ExperimentOmissionAction::omit_aggregate);
    CHECK(markers[1].proposal == exhausted.proposal);
    CHECK(markers[1].action ==
          ExperimentOmissionAction::capacity_exhausted);
    CHECK(markers[1].monotonic_ns == 152);
    const auto fields = parse_marker_fields(encoded_markers[1]);
    CHECK(fields.at("action") == "capacity_exhausted");
    CHECK(fields.at("monotonic_ns") == "152");
}

TEST_CASE(
    "persistent omission requires the exact selected minority bound",
    "[adaptive-v2][experiment][byzantine][persistent][validation]")
{
    CHECK_NOTHROW(
        ExperimentByzantineAdapter(persistent_omission_options(1)));
    CHECK_NOTHROW(
        ExperimentByzantineAdapter(persistent_omission_options(2)));

    ExperimentByzantineAdapter mode_contract(
        persistent_omission_options(1));
    CHECK(mode_contract.scheduled_omission_enabled());
    CHECK_FALSE(mode_contract.rotating_omission_enabled());
    CHECK_FALSE(mode_contract.rotating_omission_actor(
        context("persistent-mode-contract").proposal).has_value());

    auto below_actor_count = persistent_omission_options(1);
    below_actor_count.rotating_omission->max_omissions_per_proposal = 1;
    CHECK_THROWS_AS(
        ExperimentByzantineAdapter(below_actor_count),
        std::invalid_argument);

    auto above_actor_count = persistent_omission_options(1);
    above_actor_count.rotating_omission->max_omissions_per_proposal = 3;
    CHECK_THROWS_AS(
        ExperimentByzantineAdapter(above_actor_count),
        std::invalid_argument);

    auto above_fault_threshold = persistent_omission_options(1);
    above_fault_threshold.rotating_omission->actor_ids = {1, 3, 5};
    above_fault_threshold.rotating_omission->expected_actor_count = 3;
    above_fault_threshold.rotating_omission->max_omissions_per_proposal = 3;
    CHECK_THROWS_AS(
        ExperimentByzantineAdapter(above_fault_threshold),
        std::invalid_argument);
}

TEST_CASE(
    "persistent selected actors omit every eligible proposal and others forward",
    "[adaptive-v2][experiment][byzantine][persistent][behavior]")
{
    std::vector<ExperimentOmissionMarker> actor_one_markers;
    std::vector<std::string> actor_one_encoded;
    ExperimentByzantineAdapter actor_one(persistent_omission_options(
        1, &actor_one_markers, &actor_one_encoded));
    ExperimentByzantineAdapter actor_three(persistent_omission_options(3));
    ExperimentByzantineAdapter nonselected(persistent_omission_options(2));

    const auto internal = context(
        "persistent-internal", configuration(), "factorial-window-1");
    CHECK(actor_one.consume_outbound_aggregate(
        internal, ExperimentReplicaRole::internal, 100));
    CHECK(actor_three.consume_outbound_aggregate(
        internal, ExperimentReplicaRole::internal, 100));
    CHECK_FALSE(nonselected.consume_outbound_aggregate(
        internal, ExperimentReplicaRole::internal, 100));
    CHECK(actor_one.consume_outbound_aggregate(
        internal, ExperimentReplicaRole::internal, 101));

    const auto root = context(
        "persistent-root", configuration(), "factorial-window-1");
    CHECK_FALSE(actor_one.consume_outbound_aggregate(
        root, ExperimentReplicaRole::root, 102));
    CHECK_FALSE(actor_three.consume_outbound_aggregate(
        root, ExperimentReplicaRole::root, 102));

    const auto leaf = context(
        "persistent-leaf", configuration(), "factorial-window-1");
    CHECK(
        actor_one.consume_outbound_direct_vote(
            leaf, ExperimentReplicaRole::leaf, 150) ==
        ExperimentDirectVoteDisposition::omit_first);
    CHECK(
        actor_three.consume_outbound_direct_vote(
            leaf, ExperimentReplicaRole::leaf, 150) ==
        ExperimentDirectVoteDisposition::omit_first);
    CHECK(
        nonselected.consume_outbound_direct_vote(
            leaf, ExperimentReplicaRole::leaf, 150) ==
        ExperimentDirectVoteDisposition::forward);
    CHECK(
        actor_one.consume_outbound_direct_vote(
            leaf, ExperimentReplicaRole::leaf, 151) ==
        ExperimentDirectVoteDisposition::omit_repeat);

    const auto before = context(
        "persistent-before", configuration(), "factorial-window-1");
    const auto after = context(
        "persistent-after", configuration(), "factorial-window-1");
    CHECK_FALSE(actor_one.consume_outbound_aggregate(
        before, ExperimentReplicaRole::internal, 99));
    CHECK_FALSE(actor_one.consume_outbound_aggregate(
        after, ExperimentReplicaRole::internal, 200));

    REQUIRE(actor_one_markers.size() == 2);
    REQUIRE(actor_one_encoded.size() == 2);
    CHECK(
        actor_one_markers[0].fault_mode ==
        "persistent_selected_omission_v1");
    CHECK(
        actor_one_markers[1].fault_mode ==
        "persistent_selected_omission_v1");
    CHECK(
        parse_marker_fields(actor_one_encoded[0]).at("fault") ==
        "persistent_selected_omission_v1");
    CHECK(
        parse_marker_fields(actor_one_encoded[1]).at("fault") ==
        "persistent_selected_omission_v1");
}

TEST_CASE(
    "persistent omission keeps configurations distinct when later trees arrive first",
    "[adaptive-v2][experiment][byzantine][persistent][configuration]")
{
    ExperimentByzantineAdapter adapter(persistent_omission_options(1));
    const auto shared_block = digest("shared-persistent-block");
    const auto epoch_digest = digest("shared-persistent-epoch");
    const ExperimentByzantineContext later_tree{
        ProposalKey{ConfigurationId{8, 5, epoch_digest}, shared_block},
        "factorial-window-1"};
    const ExperimentByzantineContext primary_tree{
        ProposalKey{ConfigurationId{8, 2, epoch_digest}, shared_block},
        "factorial-window-1"};

    CHECK_FALSE(adapter.consume_outbound_aggregate(
        later_tree, ExperimentReplicaRole::root, 150));
    CHECK(adapter.consume_outbound_aggregate(
        primary_tree, ExperimentReplicaRole::internal, 151));
    CHECK_FALSE(adapter.consume_outbound_aggregate(
        later_tree, ExperimentReplicaRole::root, 152));
}

TEST_CASE(
    "experiment marker raw clock is available and positive",
    "[adaptive-v2][experiment][byzantine][clock]")
{
    struct timespec timestamp{};
    REQUIRE(::clock_gettime(CLOCK_MONOTONIC_RAW, &timestamp) == 0);
    CHECK(timestamp.tv_sec >= 0);
    CHECK(timestamp.tv_nsec >= 0);
    CHECK(timestamp.tv_nsec < 1'000'000'000);
    CHECK((timestamp.tv_sec > 0 || timestamp.tv_nsec > 0));
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
                               .consume_false_report_positive_marker(
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
    static_assert(std::is_same_v<
                  decltype(std::declval<ExperimentByzantineAdapter &>()
                               .consume_outbound_direct_vote(
                                   std::declval<
                                       const ExperimentByzantineContext &>())),
                  ExperimentDirectVoteDisposition>);

    const auto manager = source("examples/adaptation_manager.cpp");
    CHECK(manager.find("ExperimentByzantineAdapter") == std::string::npos);
    CHECK(
        manager.find("experiment-false-report") ==
        std::string::npos);
    CHECK(
        manager.find("experiment-omit-outbound-aggregate") ==
        std::string::npos);
    CHECK(
        manager.find("experiment-omit-outbound-direct-vote") ==
        std::string::npos);
    CHECK(
        manager.find("rotating_intermittent_omission_v1") ==
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
        declarations.find("opt_experiment_byzantine_mode") !=
        std::string::npos);
    CHECK(
        declarations.find(
            "opt_experiment_omission_additional_configuration") !=
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
            "opt_experiment_omit_outbound_direct_vote") !=
        std::string::npos);
    CHECK(
        declarations.find(
            "opt_experiment_byzantine_context_limit") !=
        std::string::npos);
    CHECK(
        declarations.find(
            "opt_experiment_rotating_omission_actors") !=
        std::string::npos);
    CHECK(
        declarations.find(
            "opt_experiment_byzantine_window_start_monotonic_ns") !=
        std::string::npos);
    CHECK(
        declarations.find(
            "opt_experiment_byzantine_window_end_monotonic_ns") !=
        std::string::npos);
    CHECK(
        declarations.find(
            "opt_experiment_byzantine_max_omissions_per_proposal") !=
        std::string::npos);
    CHECK(
        declarations.find(
            "opt_experiment_rotating_omission_context_limit") !=
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
        application.find("\"experiment-byzantine-mode\"") !=
        std::string::npos);
    CHECK(
        application.find("\"experiment-byzantine-configuration\"") !=
        std::string::npos);
    CHECK(
        application.find(
            "\"experiment-omission-additional-configuration\"") !=
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
        application.find(
            "\"experiment-omit-outbound-direct-vote\"") !=
        std::string::npos);
    CHECK(
        application.find("\"experiment-byzantine-context-limit\"") !=
        std::string::npos);
    CHECK(
        application.find(
            "\"experiment-rotating-omission-actors\"") !=
        std::string::npos);
    CHECK(
        application.find(
            "\"experiment-byzantine-window-start-monotonic-ns\"") !=
        std::string::npos);
    CHECK(
        application.find(
            "\"experiment-byzantine-window-end-monotonic-ns\"") !=
        std::string::npos);
    CHECK(
        application.find(
            "\"experiment-byzantine-max-omissions-per-proposal\"") !=
        std::string::npos);
    CHECK(
        application.find(
            "\"experiment-rotating-omission-context-limit\"") !=
        std::string::npos);

    const auto parser = source_slice(
        application,
        "parse_experiment_byzantine_options(",
        "hotstuff::PubKeySecp256k1 parse_adaptive_v2_issuer_public_key");
    CHECK(
        parser.find("bool omit_outbound_direct_vote") !=
        std::string::npos);
    CHECK(parser.find("fault_mode_count != 1") != std::string::npos);
    CHECK(
        parser.find("rotating_intermittent_omission_v1") !=
        std::string::npos);
    CHECK(
        parser.find("persistent_selected_omission_v1") !=
        std::string::npos);
    CHECK(
        parser.find("derive_byzantine_quorum(replica_count)") !=
        std::string::npos);
    CHECK(
        parser.find(
            "direct-vote omission does not accept an additional ") !=
        std::string::npos);
    CHECK(
        parser.find("options.omit_outbound_direct_vote = true") !=
        std::string::npos);
    CHECK(
        parser.find(
            "options.maximum_direct_vote_omission_contexts") !=
        std::string::npos);
    CHECK(
        parser.find("additional_omission_after_primary") ==
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
    CHECK(
        header.find(
            "experiment_false_timeout_states") !=
        std::string::npos);
    CHECK(
        header.find(
            "maximum_experiment_false_timeout_contexts") !=
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
    const auto marker_latch = contribution.find(
        "consume_false_report_positive_marker");
    const auto positive =
        contribution.find("record_verified_response");
    REQUIRE(accepted != std::string::npos);
    REQUIRE(suppress != std::string::npos);
    REQUIRE(marker_latch != std::string::npos);
    REQUIRE(positive != std::string::npos);
    CHECK(accepted < suppress);
    CHECK(suppress < marker_latch);
    CHECK(marker_latch < positive);
    CHECK(
        contribution.find("suppress_positive_observation") !=
        std::string::npos);
    CHECK(
        contribution.find(
            "KAURI_FAULT false_report_positive_suppressed") !=
        std::string::npos);
    CHECK(
        occurrences(
            implementation,
            "KAURI_FAULT false_report_positive_suppressed ") == 1);
    CHECK(
        contribution.find("experiment_fault_marker_monotonic_now_ns") <
        contribution.find(
            "KAURI_FAULT false_report_positive_suppressed"));
    CHECK(
        contribution.find("window=%s monotonic_ns=%llu") !=
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
    const auto bounded_state =
        false_timeout.find("experiment_false_timeout_states.size()");
    const auto real_deadline =
        false_timeout.find("schedule_at_or_after_deadline");
    const auto monotonic_deadline =
        false_timeout.find("aggregation_scheduler->monotonic_now()");
    const auto consume =
        false_timeout.find("consume_false_timeout");
    const auto record = false_timeout.find("record_timeouts");
    const auto recorded_guard =
        false_timeout.find("if (recorded == 1)", record);
    const auto deadline_retire = false_timeout.find("->retire(key)");
    const auto failure_boundary =
        false_timeout.find("catch (...)", deadline_retire);
    const auto empty_schedule =
        false_timeout.find("if (!cancellation)");
    const auto rollback =
        false_timeout.find("cancel_experiment_false_timeout");
    const auto release_commit =
        false_timeout.find("release_experiment_false_report_commit");
    const auto admitted_evidence =
        false_timeout.find("last_evidence_sequence");
    const auto fail_closed =
        false_timeout.find("false_report_commit_suppressed");
    const auto nonconsumed_cleanup =
        false_timeout.find(
            "if (!false_timeout_consumed || recorded != 1)",
            release_commit);
    REQUIRE(bounded_state != std::string::npos);
    REQUIRE(real_deadline != std::string::npos);
    REQUIRE(monotonic_deadline != std::string::npos);
    REQUIRE(consume != std::string::npos);
    REQUIRE(record != std::string::npos);
    REQUIRE(recorded_guard != std::string::npos);
    REQUIRE(deadline_retire != std::string::npos);
    REQUIRE(failure_boundary != std::string::npos);
    REQUIRE(empty_schedule != std::string::npos);
    REQUIRE(rollback != std::string::npos);
    REQUIRE(release_commit != std::string::npos);
    REQUIRE(admitted_evidence != std::string::npos);
    REQUIRE(fail_closed != std::string::npos);
    REQUIRE(nonconsumed_cleanup != std::string::npos);
    CHECK(bounded_state < real_deadline);
    CHECK(monotonic_deadline < real_deadline);
    CHECK(real_deadline < consume);
    CHECK(consume < record);
    CHECK(record < recorded_guard);
    CHECK(recorded_guard < deadline_retire);
    CHECK(deadline_retire < failure_boundary);
    CHECK(failure_boundary < release_commit);
    CHECK(release_commit < nonconsumed_cleanup);
    CHECK(
        false_timeout.find("KAURI_FAULT false_timeout_emitted") !=
        std::string::npos);
    CHECK(
        false_timeout.find("experiment_fault_marker_monotonic_now_ns") <
        false_timeout.find("KAURI_FAULT false_timeout_emitted"));
    CHECK(
        false_timeout.find("window=%s monotonic_ns=%llu") !=
        std::string::npos);
    CHECK(occurrences(implementation, "consume_false_timeout") == 1);

    const auto committed = source_slice(
        implementation,
        "void HotStuffBase::report_adaptive_v2_committed",
        "AdaptiveV2ReportingDeliveryResult");
    const auto exact_pending =
        committed.find("experiment_false_timeout_states.find(*key)");
    const auto verified_response =
        committed.find("should_retain_response_evidence");
    const auto defer = committed.find("observe_commit");
    const auto defer_audit =
        committed.find("KAURI_FAULT false_report_commit_deferred");
    const auto defer_exit = committed.find("return;", defer_audit);
    const auto normal_lifecycle =
        committed.find("enqueue_lifecycle");
    REQUIRE(exact_pending != std::string::npos);
    REQUIRE(verified_response != std::string::npos);
    REQUIRE(defer != std::string::npos);
    REQUIRE(defer_audit != std::string::npos);
    REQUIRE(defer_exit != std::string::npos);
    REQUIRE(normal_lifecycle != std::string::npos);
    CHECK(exact_pending < verified_response);
    CHECK(verified_response < defer);
    CHECK(defer < defer_audit);
    CHECK(defer_audit < defer_exit);
    CHECK(defer_exit < normal_lifecycle);
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
    const auto outbound_hook = source_slice(
        implementation,
        "bool HotStuffBase::consume_experiment_outbound_aggregate",
        "bool HotStuffBase::send_exact_relay");

    const auto consume =
        outbound_hook.find("consume_outbound_aggregate");
    const auto audit =
        outbound_hook.find("KAURI_FAULT aggregate_omitted");
    const auto marker_clock =
        outbound_hook.find("experiment_fault_marker_monotonic_now_ns");
    const auto hook = coordinator.find(
        "consume_experiment_outbound_aggregate");
    const auto transport = coordinator.find("send_exact_relay");
    const auto retry =
        coordinator.find("schedule_exact_forwarding_retry");
    REQUIRE(consume != std::string::npos);
    REQUIRE(audit != std::string::npos);
    REQUIRE(marker_clock != std::string::npos);
    REQUIRE(hook != std::string::npos);
    REQUIRE(transport != std::string::npos);
    REQUIRE(retry != std::string::npos);
    CHECK(consume < audit);
    CHECK(consume < marker_clock);
    CHECK(marker_clock < audit);
    CHECK(hook < transport);
    CHECK(transport < retry);

    const auto omitted_branch =
        outbound_hook.substr(consume);
    CHECK(omitted_branch.find("return true") != std::string::npos);
    CHECK(
        omitted_branch.find("monotonic_ns=%llu") !=
        std::string::npos);
    CHECK(
        implementation.find(
            "::clock_gettime(CLOCK_MONOTONIC_RAW") !=
        std::string::npos);
    const auto raw_clock = source_slice(
        implementation,
        "std::uint64_t adaptive_evidence_monotonic_now_ns",
        "std::optional<std::uint64_t>\n"
        "        experiment_fault_marker_monotonic_now_ns");
    CHECK(
        raw_clock.find("::clock_gettime(CLOCK_MONOTONIC_RAW") !=
        std::string::npos);
    const auto marker_clock_wrapper = source_slice(
        implementation,
        "experiment_fault_marker_monotonic_now_ns() noexcept",
        "std::uint64_t adaptive_deadline_duration_us");
    CHECK(
        marker_clock_wrapper.find(
            "adaptive_evidence_monotonic_now_ns()") !=
        std::string::npos);
    const auto zero_guard =
        marker_clock_wrapper.find("if (monotonic_ns == 0)");
    const auto reject_zero =
        marker_clock_wrapper.find("return std::nullopt", zero_guard);
    REQUIRE(zero_guard != std::string::npos);
    REQUIRE(reject_zero != std::string::npos);
    CHECK(zero_guard < reject_zero);

    CHECK(
        occurrences(
            implementation,
            "adaptive_evidence_monotonic_now_ns()") >= 6);
    CHECK(
        occurrences(
            implementation,
            "adaptive_monotonic_now_ns()") == 2);
    const auto reporting_flush = source_slice(
        implementation,
        "void HotStuffBase::flush_adaptive_v2_reporting",
        "void HotStuffBase::mark_adaptive_v2_convergence_evidence_unhealthy");
    CHECK(
        reporting_flush.find("adaptive_monotonic_now_ns()") !=
        std::string::npos);
    CHECK(
        reporting_flush.find("adaptive_evidence_monotonic_now_ns()") ==
        std::string::npos);

    const auto relay = source_slice(
        implementation,
        "bool HotStuffBase::send_exact_relay",
        "void HotStuffBase::schedule_exact_forwarding_retry");
    const auto relay_consume =
        relay.find("consume_experiment_outbound_aggregate");
    const auto relay_transport = relay.find("VoteRelay relay");
    REQUIRE(relay_consume != std::string::npos);
    REQUIRE(relay_transport != std::string::npos);
    CHECK(relay_consume < relay_transport);

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

TEST_CASE(
    "runtime direct-vote omission closes before every outbound path",
    "[adaptive-v2][experiment][byzantine][runtime][direct-vote]")
{
    const auto header = source("include/hotstuff/hotstuff.h");
    const auto adapter_header =
        source("include/hotstuff/experiment_byzantine_adapter.h");
    const auto implementation = source("src/hotstuff.cpp");

    CHECK(
        header.find("consume_experiment_outbound_direct_vote") !=
        std::string::npos);
    CHECK(
        adapter_header.find(
            "cryptographically proves a false report") !=
        std::string::npos);
    CHECK(
        adapter_header.find(
            "Byzantine attribution") !=
        std::string::npos);

    const auto decision = source_slice(
        implementation,
        "bool HotStuffBase::consume_experiment_outbound_direct_vote",
        "bool HotStuffBase::send_exact_relay");
    const auto leaf_guard = decision.find("!tree.direct_children.empty()");
    const auto leaf_guard_return = decision.find("return false;", leaf_guard);
    const auto consume = decision.find("consume_outbound_direct_vote");
    const auto repeat = decision.find(
        "ExperimentDirectVoteDisposition::omit_repeat");
    const auto repeat_return = decision.find("return true;", repeat);
    const auto marker_clock =
        decision.find("experiment_fault_marker_monotonic_now_ns");
    const auto marker = decision.find("KAURI_FAULT direct_vote_omitted");
    REQUIRE(leaf_guard != std::string::npos);
    REQUIRE(leaf_guard_return != std::string::npos);
    REQUIRE(consume != std::string::npos);
    REQUIRE(repeat != std::string::npos);
    REQUIRE(repeat_return != std::string::npos);
    REQUIRE(marker_clock != std::string::npos);
    REQUIRE(marker != std::string::npos);
    CHECK(leaf_guard < consume);
    CHECK(leaf_guard < leaf_guard_return);
    CHECK(leaf_guard_return < consume);
    CHECK(consume < repeat);
    CHECK(repeat < repeat_return);
    CHECK(repeat_return < marker_clock);
    CHECK(marker_clock < marker);
    CHECK(
        decision.find(
            "epoch=%u tree=%u block=%s window=%s monotonic_ns=%llu") !=
        std::string::npos);
    CHECK(
        occurrences(
            implementation,
            "KAURI_FAULT direct_vote_omitted replica=%u parent=%u") == 1);

    const auto assert_local_path =
        [](const std::string &path, const std::string &transport)
        {
            const auto record = path.find("record_local_part");
            const auto omit = path.find(
                "consume_experiment_outbound_direct_vote");
            const auto close = path.find("proposal_contexts->close", omit);
            const auto aborted =
                path.find("ProposalContextEvent::proposal_aborted", close);
            const auto early_return = path.find("return;", aborted);
            const auto fallback =
                path.find("schedule_exact_vote_fallback", omit);
            const auto outbound = path.find(transport, omit);
            REQUIRE(record != std::string::npos);
            REQUIRE(omit != std::string::npos);
            REQUIRE(close != std::string::npos);
            REQUIRE(aborted != std::string::npos);
            REQUIRE(early_return != std::string::npos);
            REQUIRE(fallback != std::string::npos);
            REQUIRE(outbound != std::string::npos);
            CHECK(record < omit);
            CHECK(omit < close);
            CHECK(close < aborted);
            CHECK(aborted < early_return);
            CHECK(early_return < fallback);
            CHECK(fallback < outbound);
        };

    const auto local_proposal_vote = source_slice(
        implementation,
        "void HotStuffBase::apply_local_vote",
        "void HotStuffBase::on_local_proposal_processed");
    assert_local_path(local_proposal_vote, "forward_exact_direct");

    const auto remote_proposal_vote = source_slice(
        implementation,
        "void HotStuffBase::do_vote",
        "std::optional<ProposalKey> HotStuffBase::committed_proposal_key");
    assert_local_path(remote_proposal_vote, "owner.pn.send_msg");

    const auto root_fallback = source_slice(
        implementation,
        "bool HotStuffBase::send_exact_vote_to_root",
        "void HotStuffBase::schedule_exact_proposal_fallback");
    const auto root_guard =
        root_fallback.find("outbound_direct_vote_omitted");
    const auto root_suppressed =
        root_fallback.find("return true;", root_guard);
    const auto root_transport = root_fallback.find("pn.send_msg");
    REQUIRE(root_guard != std::string::npos);
    REQUIRE(root_suppressed != std::string::npos);
    REQUIRE(root_transport != std::string::npos);
    CHECK(root_guard < root_suppressed);
    CHECK(root_suppressed < root_transport);
    CHECK(
        root_fallback.find("consume_outbound_direct_vote") ==
        std::string::npos);

    const auto relay = source_slice(
        implementation,
        "bool HotStuffBase::send_exact_relay",
        "void HotStuffBase::schedule_exact_forwarding_retry");
    const auto relay_guard =
        relay.find("outbound_direct_vote_omitted");
    const auto relay_suppressed =
        relay.find("return true;", relay_guard);
    const auto relay_transport = relay.find("pn.send_msg");
    REQUIRE(relay_guard != std::string::npos);
    REQUIRE(relay_suppressed != std::string::npos);
    REQUIRE(relay_transport != std::string::npos);
    CHECK(relay_guard < relay_suppressed);
    CHECK(relay_suppressed < relay_transport);

    const auto coordinator = source_slice(
        implementation,
        "void HotStuffBase::rebuild_aggregation_timeout_coordinator",
        "void HotStuffBase::set_aggregation_timeout");
    const auto timeout_guard =
        coordinator.find("outbound_direct_vote_omitted");
    const auto timeout_suppressed =
        coordinator.find("return true;", timeout_guard);
    const auto timeout_relay = coordinator.find("send_exact_relay");
    REQUIRE(timeout_guard != std::string::npos);
    REQUIRE(timeout_suppressed != std::string::npos);
    REQUIRE(timeout_relay != std::string::npos);
    CHECK(timeout_guard < timeout_suppressed);
    CHECK(timeout_suppressed < timeout_relay);
}
