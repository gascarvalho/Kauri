#include <chrono>
#include <cctype>
#include <cstddef>
#include <cstdint>
#include <fstream>
#include <functional>
#include <future>
#include <initializer_list>
#include <limits>
#include <memory>
#include <optional>
#include <set>
#include <sstream>
#include <stdexcept>
#include <string>
#include <thread>
#include <type_traits>
#include <utility>
#include <vector>

#include "catch.hpp"
#include "hotstuff/configuration.h"
#include "hotstuff/hotstuff.h"
#include "support/commit_rule_fixture.h"
#include "support/fake_clock.h"

#ifndef KAURI_PROJECT_SOURCE_DIR
#define KAURI_PROJECT_SOURCE_DIR "."
#endif

/*
 * L07 independent leader-progress contract.
 *
 * The monitor owns one exact LeaderViewId at a time.  That identity already
 * contains the exact epoch/tree/digest tuple, a monotonic activation
 * generation, and the leader.  The monitor is deliberately independent of
 * aggregation and epoch deployment: its only expiry effect is a request to
 * rotate the currently active view.
 *
 * The public header is optional in this tests-first commit.  Its absence gives
 * one compile-safe intentional red while the complete behavioral contract
 * below becomes active as soon as the production seam is introduced.
 */
#if __has_include("hotstuff/leader_progress.h")
#define KAURI_HAS_LEADER_PROGRESS 1
#include "hotstuff/leader_progress.h"
#else
#define KAURI_HAS_LEADER_PROGRESS 0

namespace hotstuff
{

enum class LeaderProgressEvent
{
    verified_proposal,
    quorum_certificate,
    commit
};

struct LeaderProgressConfig
{
    using Duration = std::chrono::nanoseconds;

    Duration activation_grace;
    Duration progress_timeout;
    Duration maximum_aggregation_timeout;
    std::set<LeaderProgressEvent> reset_on;
};

class LeaderProgressScheduler
{
public:
    using Duration = LeaderProgressConfig::Duration;
    using Callback = std::function<void()>;
    using Cancellation = std::function<void()>;

    virtual ~LeaderProgressScheduler() = default;
    virtual Cancellation schedule_after(Duration, Callback) = 0;
};

struct LeaderProgressEffects
{
    std::function<void(const LeaderViewId &)> rotate_active_view;
};

class LeaderProgressMonitor
{
public:
    LeaderProgressMonitor(LeaderProgressConfig, LeaderProgressEffects) {}

    bool activate(const LeaderViewId &, LeaderProgressScheduler &)
    {
        return false;
    }

    bool record_verified_progress(const LeaderViewId &,
                                  LeaderProgressEvent,
                                  LeaderProgressScheduler &)
    {
        return false;
    }

    bool dispatch_grace(const LeaderViewId &,
                        std::uint64_t,
                        LeaderProgressScheduler &)
    {
        return false;
    }

    bool dispatch_timeout(const LeaderViewId &, std::uint64_t)
    {
        return false;
    }

    std::optional<LeaderViewId> active_view() const
    {
        return std::nullopt;
    }

    void shutdown() {}
};

} // namespace hotstuff
#endif

namespace
{

std::string read_source(const std::string &relative_path)
{
    const std::string path =
        std::string(KAURI_PROJECT_SOURCE_DIR) + "/" + relative_path;
    std::ifstream input(path);
    REQUIRE(input.good());
    std::ostringstream contents;
    contents << input.rdbuf();
    return contents.str();
}

std::string source_slice(const std::string &source,
                         const std::string &begin_marker,
                         const std::string &end_marker)
{
    const auto begin = source.find(begin_marker);
    INFO("missing source marker: " << begin_marker);
    REQUIRE(begin != std::string::npos);
    const auto end = source.find(end_marker, begin + begin_marker.size());
    INFO("missing source marker: " << end_marker);
    REQUIRE(end != std::string::npos);
    return source.substr(begin, end - begin);
}

bool contains_in_order(
    const std::string &source,
    std::initializer_list<const char *> markers)
{
    std::size_t cursor = 0;
    for (const auto *marker : markers)
    {
        const auto found = source.find(marker, cursor);
        if (found == std::string::npos)
            return false;
        cursor = found + std::char_traits<char>::length(marker);
    }
    return true;
}

std::string without_whitespace(const std::string &source)
{
    std::string normalized;
    normalized.reserve(source.size());
    for (const unsigned char character : source)
    {
        if (character != ' ' && character != '\t' &&
            character != '\r' && character != '\n')
            normalized.push_back(static_cast<char>(character));
    }
    return normalized;
}

std::string code_without_comments_or_literals(const std::string &source)
{
    enum class LexicalState
    {
        code,
        line_comment,
        block_comment,
        string_literal,
        character_literal
    };

    std::string result(source.size(), ' ');
    auto state = LexicalState::code;
    bool escaped = false;
    for (std::size_t cursor = 0; cursor < source.size(); ++cursor)
    {
        const auto character = source[cursor];
        const auto next = cursor + 1 < source.size()
                              ? source[cursor + 1]
                              : '\0';

        if (character == '\n')
            result[cursor] = '\n';

        switch (state)
        {
        case LexicalState::code:
            if (character == '/' && next == '/')
            {
                state = LexicalState::line_comment;
                ++cursor;
            }
            else if (character == '/' && next == '*')
            {
                state = LexicalState::block_comment;
                ++cursor;
            }
            else if (character == '"')
            {
                state = LexicalState::string_literal;
                escaped = false;
            }
            else if (character == '\'')
            {
                state = LexicalState::character_literal;
                escaped = false;
            }
            else
            {
                result[cursor] = character;
            }
            break;
        case LexicalState::line_comment:
            if (character == '\n')
                state = LexicalState::code;
            break;
        case LexicalState::block_comment:
            if (character == '*' && next == '/')
            {
                state = LexicalState::code;
                ++cursor;
            }
            break;
        case LexicalState::string_literal:
        case LexicalState::character_literal:
            if (escaped)
            {
                escaped = false;
            }
            else if (character == '\\')
            {
                escaped = true;
            }
            else if ((state == LexicalState::string_literal &&
                      character == '"') ||
                     (state == LexicalState::character_literal &&
                      character == '\''))
            {
                state = LexicalState::code;
            }
            break;
        }
    }
    return result;
}

std::size_t count_occurrences(const std::string &source,
                              const std::string &needle)
{
    if (needle.empty())
        return 0;
    std::size_t count = 0;
    for (std::size_t position = 0;
         (position = source.find(needle, position)) != std::string::npos;
         position += needle.size())
        ++count;
    return count;
}

std::size_t matching_closing_brace(const std::string &source,
                                   std::size_t opening)
{
    if (opening == std::string::npos || source[opening] != '{')
        return std::string::npos;
    std::size_t depth = 0;
    for (std::size_t cursor = opening; cursor < source.size(); ++cursor)
    {
        if (source[cursor] == '{')
            ++depth;
        else if (source[cursor] == '}' && --depth == 0)
            return cursor;
    }
    return std::string::npos;
}

std::optional<std::string> option_binding(
    const std::string &source,
    const std::string &option)
{
    const auto marker = "config.add_opt(\"" + option + "\",";
    const auto declaration = source.find(marker);
    if (declaration == std::string::npos)
        return std::nullopt;
    auto cursor = declaration + marker.size();
    while (cursor < source.size() &&
           std::isspace(static_cast<unsigned char>(source[cursor])) != 0)
        ++cursor;
    const auto begin = cursor;
    while (cursor < source.size())
    {
        const auto character = static_cast<unsigned char>(source[cursor]);
        if (std::isalnum(character) == 0 && source[cursor] != '_')
            break;
        ++cursor;
    }
    if (cursor == begin)
        return std::nullopt;
    return source.substr(begin, cursor - begin);
}

std::string function_body(const std::string &source,
                          const std::string &marker)
{
    const auto method = source.find(marker);
    INFO("missing source marker: " << marker);
    REQUIRE(method != std::string::npos);
    const auto opening = source.find('{', method + marker.size());
    REQUIRE(opening != std::string::npos);
    const auto closing = matching_closing_brace(source, opening);
    REQUIRE(closing != std::string::npos);
    return source.substr(method, closing - method + 1);
}

hotstuff::uint256_t digest(const std::string &label)
{
    return hotstuff::DataStream(label).get_hash();
}

hotstuff::ConfigurationId configuration(
    std::uint32_t epoch,
    std::uint32_t tree,
    const std::string &label)
{
    return hotstuff::ConfigurationId{epoch, tree, digest(label)};
}

hotstuff::ProposalTreeSnapshot root_tree_snapshot()
{
    hotstuff::ProposalTreeSnapshot tree;
    tree.local_replica = 0;
    tree.root = 0;
    tree.parent = std::nullopt;
    tree.direct_children = {1, 2};
    tree.assigned_subtree = {0, 1, 2, 3, 4, 5, 6};
    tree.child_subtrees = {
        {1, {1, 3, 4}},
        {2, {2, 5, 6}}};
    tree.fanout = 2;
    tree.pipeline_stretch = 2;
    return tree;
}

hotstuff::ProposalContextMetadata root_context_metadata(
    const hotstuff::ProposalKey &key,
    std::size_t global_quorum = 5)
{
    return hotstuff::ProposalContextMetadata{
        key, root_tree_snapshot(), global_quorum};
}

hotstuff::LeaderViewId view(
    const hotstuff::ConfigurationId &config,
    std::uint64_t generation,
    hotstuff::ReplicaID leader)
{
    return hotstuff::LeaderViewId{config, generation, leader};
}

} // namespace

#if !KAURI_HAS_LEADER_PROGRESS

TEST_CASE("L07 leader progress monitor is available",
          "[l07][leader-progress][contract][intentional-red]")
{
    INFO("Missing include/hotstuff/leader_progress.h. L07 requires one "
         "state-owning, exact-view, monotonic leader-progress monitor.");
    REQUIRE(KAURI_HAS_LEADER_PROGRESS == 1);
}

#endif

using hotstuff::LeaderProgressConfig;
using hotstuff::LeaderProgressEffects;
using hotstuff::LeaderProgressEvent;
using hotstuff::LeaderProgressMonitor;
using hotstuff::LeaderProgressScheduler;
using hotstuff::LeaderViewId;
using hotstuff::test::FakeClock;

namespace
{

using Duration = LeaderProgressConfig::Duration;

class FakeLeaderProgressScheduler final : public LeaderProgressScheduler
{
public:
    Cancellation schedule_after(Duration delay, Callback callback) override
    {
        if (delay <= Duration::zero())
            throw std::invalid_argument("fake deadline must be positive");
        if (fail_next_schedule_)
        {
            fail_next_schedule_ = false;
            throw std::runtime_error("injected schedule failure");
        }

        auto task = std::make_shared<Task>();
        task->id = tasks_.size();
        task->deadline = clock_.now() + delay;
        task->callback = std::move(callback);
        tasks_.push_back(task);
        return [this, weak = std::weak_ptr<Task>(task)]() {
            if (const auto current = weak.lock();
                current && !current->fired)
            {
                current->cancelled = true;
                if (cancellation_observer_)
                    cancellation_observer_();
                if (current->throw_on_cancellation)
                    throw std::runtime_error(
                        "injected cancellation failure");
            }
        };
    }

    void fail_next_schedule() noexcept
    {
        fail_next_schedule_ = true;
    }

    void throw_on_cancellation(std::size_t id)
    {
        if (id >= tasks_.size())
            throw std::out_of_range("unknown scheduled task");
        tasks_[id]->throw_on_cancellation = true;
    }

    FakeClock::time_point now() const noexcept
    {
        return clock_.now();
    }

    std::size_t scheduled_count() const noexcept
    {
        return tasks_.size();
    }

    std::size_t pending_count() const noexcept
    {
        std::size_t pending = 0;
        for (const auto &task : tasks_)
            if (!task->cancelled && !task->fired)
                ++pending;
        return pending;
    }

    std::size_t cancelled_count() const noexcept
    {
        std::size_t cancelled = 0;
        for (const auto &task : tasks_)
            if (task->cancelled)
                ++cancelled;
        return cancelled;
    }

    std::optional<FakeClock::time_point> pending_deadline() const
    {
        std::optional<FakeClock::time_point> deadline;
        for (const auto &task : tasks_)
        {
            if (task->cancelled || task->fired)
                continue;
            if (!deadline.has_value() || task->deadline < *deadline)
                deadline = task->deadline;
        }
        return deadline;
    }

    std::size_t latest_task_id() const
    {
        if (tasks_.empty())
            throw std::logic_error("no scheduled task");
        return tasks_.back()->id;
    }

    void observe_cancellation(std::function<void()> observer)
    {
        cancellation_observer_ = std::move(observer);
    }

    void advance_by(Duration amount)
    {
        clock_.advance(amount);
        run_due();
    }

    void fire_even_if_cancelled(std::size_t id)
    {
        if (id >= tasks_.size())
            throw std::out_of_range("unknown scheduled task");
        const auto callback = tasks_[id]->callback;
        callback();
    }

private:
    struct Task
    {
        std::size_t id{0};
        FakeClock::time_point deadline;
        Callback callback;
        bool cancelled{false};
        bool fired{false};
        bool throw_on_cancellation{false};
    };

    void run_due()
    {
        while (true)
        {
            std::shared_ptr<Task> next;
            for (const auto &task : tasks_)
            {
                if (task->cancelled || task->fired ||
                    task->deadline > clock_.now())
                    continue;
                if (!next || task->deadline < next->deadline ||
                    (task->deadline == next->deadline &&
                     task->id < next->id))
                    next = task;
            }
            if (!next)
                return;
            next->fired = true;
            const auto callback = next->callback;
            callback();
        }
    }

    FakeClock clock_;
    std::vector<std::shared_ptr<Task>> tasks_;
    std::function<void()> cancellation_observer_;
    bool fail_next_schedule_{false};
};

LeaderProgressConfig progress_config(
    std::set<LeaderProgressEvent> reset_on = {
        LeaderProgressEvent::verified_proposal,
        LeaderProgressEvent::quorum_certificate,
        LeaderProgressEvent::commit})
{
    return LeaderProgressConfig{
        Duration(100), Duration(200), Duration(40), std::move(reset_on)};
}

LeaderProgressEffects effects_for(std::vector<LeaderViewId> &rotations)
{
    LeaderProgressEffects effects;
    effects.rotate_active_view = [&rotations](const LeaderViewId &expired) {
        rotations.push_back(expired);
    };
    return effects;
}

struct MonitorHarness
{
    explicit MonitorHarness(LeaderProgressConfig config = progress_config())
        : effects(effects_for(rotations)),
          monitor(std::move(config), effects)
    {}

    std::vector<LeaderViewId> rotations;
    FakeLeaderProgressScheduler scheduler;
    LeaderProgressEffects effects;
    LeaderProgressMonitor monitor;
};

template<typename Effects, typename = void>
struct has_epoch_activation_effect : std::false_type
{};

template<typename Effects>
struct has_epoch_activation_effect<
    Effects,
    std::void_t<decltype(std::declval<Effects &>().activate_epoch)>>
    : std::true_type
{};

template<typename Effects, typename = void>
struct has_epoch_staging_effect : std::false_type
{};

template<typename Effects>
struct has_epoch_staging_effect<
    Effects,
    std::void_t<decltype(std::declval<Effects &>().stage_epoch)>>
    : std::true_type
{};

template<typename Effects, typename = void>
struct has_signing_effect : std::false_type
{};

template<typename Effects>
struct has_signing_effect<
    Effects,
    std::void_t<decltype(std::declval<Effects &>().sign_local)>>
    : std::true_type
{};

template<typename Effects, typename = void>
struct has_aggregation_effect : std::false_type
{};

template<typename Effects>
struct has_aggregation_effect<
    Effects,
    std::void_t<decltype(std::declval<Effects &>().flush_aggregation)>>
    : std::true_type
{};

} // namespace

TEST_CASE("leader progress configuration is explicit and overflow safe",
          "[l07][leader-progress][configuration][intentional-red]")
{
    const auto make_monitor = [](LeaderProgressConfig config) {
        LeaderProgressEffects effects;
        LeaderProgressMonitor monitor(std::move(config), std::move(effects));
        static_cast<void>(monitor);
    };

    REQUIRE_NOTHROW(make_monitor(progress_config()));

    auto invalid = progress_config();
    invalid.activation_grace = Duration::zero();
    REQUIRE_THROWS_AS(make_monitor(invalid), std::invalid_argument);
    invalid = progress_config();
    invalid.progress_timeout = Duration::zero();
    REQUIRE_THROWS_AS(make_monitor(invalid), std::invalid_argument);
    invalid = progress_config();
    invalid.maximum_aggregation_timeout = Duration::zero();
    REQUIRE_THROWS_AS(make_monitor(invalid), std::invalid_argument);
    invalid = progress_config();
    invalid.activation_grace = Duration(-1);
    REQUIRE_THROWS_AS(make_monitor(invalid), std::invalid_argument);
    invalid = progress_config();
    invalid.progress_timeout = Duration(-1);
    REQUIRE_THROWS_AS(make_monitor(invalid), std::invalid_argument);
    invalid = progress_config();
    invalid.maximum_aggregation_timeout = Duration(-1);
    REQUIRE_THROWS_AS(make_monitor(invalid), std::invalid_argument);

    invalid = progress_config();
    invalid.maximum_aggregation_timeout =
        invalid.activation_grace + invalid.progress_timeout;
    REQUIRE_THROWS_AS(make_monitor(invalid), std::invalid_argument);
    invalid.maximum_aggregation_timeout += Duration(1);
    REQUIRE_THROWS_AS(make_monitor(invalid), std::invalid_argument);

    auto near_limit = progress_config();
    near_limit.activation_grace = Duration::max();
    near_limit.progress_timeout = Duration(1);
    near_limit.maximum_aggregation_timeout = Duration::max();
    REQUIRE_NOTHROW(make_monitor(near_limit));
}

TEST_CASE("monitor is inert until a nonzero exact view becomes active",
          "[l07][leader-progress][activation][intentional-red]")
{
    MonitorHarness harness;
    const auto active = view(configuration(7, 3, "active"), 1, 2);

    CHECK_FALSE(harness.monitor.active_view().has_value());
    CHECK(harness.scheduler.pending_count() == 0);
    CHECK_FALSE(harness.monitor.record_verified_progress(
        active,
        LeaderProgressEvent::verified_proposal,
        harness.scheduler));
    CHECK_FALSE(harness.monitor.dispatch_grace(active, 1, harness.scheduler));
    CHECK_FALSE(harness.monitor.dispatch_timeout(active, 1));

    const auto zero_generation =
        view(configuration(7, 3, "active"), 0, 2);
    CHECK_FALSE(harness.monitor.activate(
        zero_generation, harness.scheduler));
    CHECK(harness.scheduler.pending_count() == 0);
    CHECK(harness.rotations.empty());
}

TEST_CASE("activation grace always precedes leader suspicion",
          "[l07][leader-progress][grace][timeout][intentional-red]")
{
    MonitorHarness harness;
    const auto active = view(configuration(8, 5, "grace"), 11, 4);
    REQUIRE(harness.monitor.activate(active, harness.scheduler));
    REQUIRE(harness.monitor.active_view() == active);
    REQUIRE(harness.scheduler.pending_count() == 1);
    CHECK(harness.scheduler.pending_deadline() ==
          harness.scheduler.now() + Duration(100));

    harness.scheduler.advance_by(Duration(99));
    CHECK(harness.rotations.empty());
    CHECK(harness.scheduler.pending_count() == 1);

    const auto grace_deadline = harness.scheduler.pending_deadline();
    const auto scheduled_before_progress =
        harness.scheduler.scheduled_count();
    CHECK(harness.monitor.record_verified_progress(
        active,
        LeaderProgressEvent::verified_proposal,
        harness.scheduler));
    CHECK(harness.scheduler.scheduled_count() ==
          scheduled_before_progress);
    CHECK(harness.scheduler.pending_deadline() == grace_deadline);

    harness.scheduler.advance_by(Duration(1));
    CHECK(harness.rotations.empty());
    REQUIRE(harness.scheduler.pending_count() == 1);
    CHECK(harness.scheduler.pending_deadline() ==
          harness.scheduler.now() + Duration(200));

    harness.scheduler.advance_by(Duration(199));
    CHECK(harness.rotations.empty());
    harness.scheduler.advance_by(Duration(1));
    REQUIRE(harness.rotations.size() == 1);
    CHECK(harness.rotations.front() == active);
    CHECK(harness.scheduler.pending_count() == 0);
}

TEST_CASE("one bounded epoch command window preserves exact view liveness",
          "[l07][leader-progress][epoch-command][bounded]")
{
    MonitorHarness harness;
    const auto active = view(configuration(8, 6, "command"), 12, 6);
    REQUIRE(harness.monitor.activate(active, harness.scheduler));

    harness.scheduler.advance_by(Duration(50));
    REQUIRE(harness.monitor.grant_bounded_epoch_command_window(
        active, harness.scheduler));
    CHECK_FALSE(harness.monitor.grant_bounded_epoch_command_window(
        active, harness.scheduler));
    CHECK(harness.scheduler.pending_count() == 1);
    CHECK(harness.scheduler.pending_deadline() ==
          harness.scheduler.now() + Duration(400));

    harness.scheduler.advance_by(Duration(399));
    CHECK(harness.rotations.empty());
    harness.scheduler.advance_by(Duration(1));
    REQUIRE(harness.rotations.size() == 1);
    CHECK(harness.rotations.front() == active);
}

TEST_CASE("verified command-view progress restores the normal timeout",
          "[l07][leader-progress][epoch-command][progress]")
{
    MonitorHarness harness;
    const auto active = view(configuration(8, 7, "command-progress"), 13, 7);
    REQUIRE(harness.monitor.activate(active, harness.scheduler));
    REQUIRE(harness.monitor.grant_bounded_epoch_command_window(
        active, harness.scheduler));

    harness.scheduler.advance_by(Duration(250));
    REQUIRE(harness.monitor.record_verified_progress(
        active,
        LeaderProgressEvent::quorum_certificate,
        harness.scheduler));
    CHECK(harness.scheduler.pending_count() == 1);
    CHECK(harness.scheduler.pending_deadline() ==
          harness.scheduler.now() + Duration(200));

    harness.scheduler.advance_by(Duration(199));
    CHECK(harness.rotations.empty());
    harness.scheduler.advance_by(Duration(1));
    REQUIRE(harness.rotations.size() == 1);
    CHECK(harness.rotations.front() == active);
}

TEST_CASE("bounded command windows reject stale foreign and expired views",
          "[l07][leader-progress][epoch-command][exact-view]")
{
    MonitorHarness harness;
    FakeLeaderProgressScheduler foreign;
    const auto active =
        view(configuration(8, 8, "command-exact"), 14, 1);
    const auto stale =
        view(active.configuration, active.view_generation - 1, 1);
    REQUIRE(harness.monitor.activate(active, harness.scheduler));
    const auto original_deadline = harness.scheduler.pending_deadline();

    CHECK_FALSE(harness.monitor.grant_bounded_epoch_command_window(
        stale, harness.scheduler));
    CHECK_FALSE(harness.monitor.grant_bounded_epoch_command_window(
        active, foreign));
    CHECK(harness.scheduler.pending_deadline() == original_deadline);
    CHECK(foreign.scheduled_count() == 0);

    harness.scheduler.advance_by(Duration(100));
    harness.scheduler.advance_by(Duration(200));
    REQUIRE(harness.rotations == std::vector<LeaderViewId>{active});
    CHECK_FALSE(harness.monitor.grant_bounded_epoch_command_window(
        active, harness.scheduler));
    CHECK(harness.scheduler.pending_count() == 0);
}

TEST_CASE("bounded command window scheduling is atomic and retryable",
          "[l07][leader-progress][epoch-command][schedule-failure]")
{
    MonitorHarness harness;
    const auto active =
        view(configuration(8, 9, "command-retry"), 15, 2);
    REQUIRE(harness.monitor.activate(active, harness.scheduler));
    const auto original_deadline = harness.scheduler.pending_deadline();

    harness.scheduler.fail_next_schedule();
    CHECK_THROWS_AS(
        harness.monitor.grant_bounded_epoch_command_window(
            active, harness.scheduler),
        std::runtime_error);
    CHECK(harness.monitor.active_view() == active);
    CHECK(harness.scheduler.pending_count() == 1);
    CHECK(harness.scheduler.pending_deadline() == original_deadline);

    REQUIRE(harness.monitor.grant_bounded_epoch_command_window(
        active, harness.scheduler));
    CHECK(harness.scheduler.pending_count() == 1);
    CHECK(harness.scheduler.pending_deadline() ==
          harness.scheduler.now() + Duration(400));
}

TEST_CASE("bounded command window is overflow safe and resets per activation",
          "[l07][leader-progress][epoch-command][overflow][activation]")
{
    SECTION("overflow leaves the active deadline unchanged")
    {
        const auto excessive = Duration::max() / 2 + Duration(1);
        MonitorHarness harness(LeaderProgressConfig{
            Duration(1), excessive, Duration(1),
            {LeaderProgressEvent::quorum_certificate}});
        const auto active =
            view(configuration(8, 10, "command-overflow"), 16, 3);
        REQUIRE(harness.monitor.activate(active, harness.scheduler));
        const auto original_deadline = harness.scheduler.pending_deadline();

        CHECK_FALSE(harness.monitor.grant_bounded_epoch_command_window(
            active, harness.scheduler));
        CHECK(harness.scheduler.pending_count() == 1);
        CHECK(harness.scheduler.pending_deadline() == original_deadline);
    }

    SECTION("a newer exact activation receives its own one-shot window")
    {
        MonitorHarness harness;
        const auto first =
            view(configuration(8, 11, "command-first"), 17, 4);
        const auto second =
            view(configuration(8, 12, "command-second"), 18, 5);
        REQUIRE(harness.monitor.activate(first, harness.scheduler));
        REQUIRE(harness.monitor.grant_bounded_epoch_command_window(
            first, harness.scheduler));
        CHECK_FALSE(harness.monitor.grant_bounded_epoch_command_window(
            first, harness.scheduler));

        REQUIRE(harness.monitor.activate(second, harness.scheduler));
        REQUIRE(harness.monitor.grant_bounded_epoch_command_window(
            second, harness.scheduler));
        CHECK_FALSE(harness.monitor.grant_bounded_epoch_command_window(
            second, harness.scheduler));
        CHECK(harness.scheduler.pending_count() == 1);
    }
}

TEST_CASE("only configured verified active progress resets timeout",
          "[l07][leader-progress][policy][exact-view][intentional-red]")
{
    MonitorHarness harness(progress_config(
        {LeaderProgressEvent::quorum_certificate}));
    const auto config = configuration(9, 4, "policy");
    const auto active = view(config, 21, 6);
    REQUIRE(harness.monitor.activate(active, harness.scheduler));
    const auto grace_deadline = harness.scheduler.pending_deadline();
    CHECK_FALSE(harness.monitor.record_verified_progress(
        active,
        LeaderProgressEvent::verified_proposal,
        harness.scheduler));
    CHECK(harness.scheduler.pending_deadline() == grace_deadline);
    harness.scheduler.advance_by(Duration(100));
    harness.scheduler.advance_by(Duration(70));
    const auto original_deadline = harness.scheduler.pending_deadline();

    CHECK_FALSE(harness.monitor.record_verified_progress(
        active,
        LeaderProgressEvent::verified_proposal,
        harness.scheduler));
    CHECK_FALSE(harness.monitor.record_verified_progress(
        active,
        LeaderProgressEvent::commit,
        harness.scheduler));
    CHECK(harness.scheduler.pending_deadline() == original_deadline);

    const std::vector<LeaderViewId> non_active = {
        view(configuration(10, 4, "future"), 22, 6),
        view(configuration(9, 4, "other-digest"), 21, 6),
        view(config, 20, 6),
        view(config, 21, 5)};
    for (const auto &candidate : non_active)
    {
        CHECK_FALSE(harness.monitor.record_verified_progress(
            candidate,
            LeaderProgressEvent::quorum_certificate,
            harness.scheduler));
        CHECK(harness.scheduler.pending_deadline() == original_deadline);
    }

    CHECK(harness.monitor.record_verified_progress(
        active,
        LeaderProgressEvent::quorum_certificate,
        harness.scheduler));
    CHECK(harness.scheduler.pending_deadline() ==
          harness.scheduler.now() + Duration(200));
    CHECK(harness.scheduler.cancelled_count() >= 1);
}

TEST_CASE("proposal QC and commit progress are independently configurable",
          "[l07][leader-progress][policy][matrix][intentional-red]")
{
    const std::vector<LeaderProgressEvent> events = {
        LeaderProgressEvent::verified_proposal,
        LeaderProgressEvent::quorum_certificate,
        LeaderProgressEvent::commit};

    std::uint64_t generation = 30;
    for (const auto configured : events)
    {
        MonitorHarness harness(progress_config({configured}));
        const auto current_generation = generation++;
        const auto active = view(
            configuration(11, 2, std::to_string(current_generation)),
            current_generation,
            1);
        REQUIRE(harness.monitor.activate(active, harness.scheduler));
        harness.scheduler.advance_by(Duration(100));
        harness.scheduler.advance_by(Duration(10));

        for (const auto observed : events)
        {
            const auto deadline_before =
                harness.scheduler.pending_deadline();
            const auto accepted = harness.monitor.record_verified_progress(
                active, observed, harness.scheduler);
            CHECK(accepted == (observed == configured));
            if (observed != configured)
                CHECK(harness.scheduler.pending_deadline() ==
                      deadline_before);
        }
        CHECK(harness.scheduler.pending_deadline() ==
              harness.scheduler.now() + Duration(200));
    }
}

TEST_CASE("stale callbacks cannot rotate a reactivated monitor",
          "[l07][leader-progress][generation][stale-callback]"
          "[intentional-red]")
{
    MonitorHarness harness;
    const auto first = view(configuration(12, 1, "first"), 41, 0);
    const auto second = view(configuration(12, 2, "second"), 42, 3);
    REQUIRE(harness.monitor.activate(first, harness.scheduler));
    const auto first_grace = harness.scheduler.latest_task_id();

    REQUIRE(harness.monitor.activate(second, harness.scheduler));
    CHECK(harness.scheduler.pending_count() == 1);
    CHECK(harness.scheduler.cancelled_count() == 1);
    harness.scheduler.fire_even_if_cancelled(first_grace);
    CHECK(harness.rotations.empty());
    CHECK(harness.scheduler.pending_count() == 1);

    harness.scheduler.advance_by(Duration(100));
    const auto second_timeout = harness.scheduler.latest_task_id();
    harness.scheduler.fire_even_if_cancelled(first_grace);
    CHECK(harness.rotations.empty());

    harness.scheduler.advance_by(Duration(200));
    REQUIRE(harness.rotations.size() == 1);
    CHECK(harness.rotations.front() == second);
    harness.scheduler.fire_even_if_cancelled(second_timeout);
    CHECK(harness.rotations.size() == 1);
}

TEST_CASE("stale reset deadline cannot expire the same active view",
          "[l07][leader-progress][deadline-generation][stale-callback]"
          "[intentional-red]")
{
    MonitorHarness harness;
    const auto active = view(configuration(12, 3, "same-view"), 43, 1);
    REQUIRE(harness.monitor.activate(active, harness.scheduler));
    harness.scheduler.advance_by(Duration(100));
    const auto old_timeout = harness.scheduler.latest_task_id();
    harness.scheduler.advance_by(Duration(10));

    REQUIRE(harness.monitor.record_verified_progress(
        active,
        LeaderProgressEvent::quorum_certificate,
        harness.scheduler));
    REQUIRE(harness.scheduler.pending_count() == 1);
    CHECK(harness.scheduler.latest_task_id() != old_timeout);
    harness.scheduler.fire_even_if_cancelled(old_timeout);
    CHECK(harness.rotations.empty());
    CHECK(harness.monitor.active_view() == active);
    CHECK(harness.scheduler.pending_count() == 1);

    harness.scheduler.advance_by(Duration(200));
    REQUIRE(harness.rotations == std::vector<LeaderViewId>{active});
}

TEST_CASE("configuration reuse requires a strictly newer generation",
          "[l07][leader-progress][activation][d11-boundary]"
          "[intentional-red]")
{
    MonitorHarness harness;
    const auto first_config = configuration(13, 1, "first-config");
    const auto second_config = configuration(13, 2, "second-config");
    const auto first = view(first_config, 50, 0);
    const auto second = view(second_config, 51, 2);
    REQUIRE(harness.monitor.activate(first, harness.scheduler));
    const auto first_deadline = harness.scheduler.latest_task_id();
    const auto scheduled_once = harness.scheduler.scheduled_count();

    CHECK_FALSE(harness.monitor.activate(first, harness.scheduler));
    CHECK_FALSE(harness.monitor.activate(
        view(second_config, 50, 2), harness.scheduler));
    CHECK(harness.scheduler.scheduled_count() == scheduled_once);

    REQUIRE(harness.monitor.activate(second, harness.scheduler));
    const auto second_deadline = harness.scheduler.latest_task_id();
    const auto scheduled_twice = harness.scheduler.scheduled_count();
    const auto reused = view(first_config, 52, 0);
    REQUIRE(harness.monitor.activate(reused, harness.scheduler));
    CHECK(harness.monitor.active_view() == reused);
    CHECK(harness.scheduler.scheduled_count() == scheduled_twice + 1);
    CHECK(harness.scheduler.pending_count() == 1);

    const auto require_monotonic_rejection =
        [&](const LeaderViewId &candidate) {
            const auto active_before = harness.monitor.active_view();
            const auto deadline_before = harness.scheduler.pending_deadline();
            const auto scheduled_before =
                harness.scheduler.scheduled_count();
            const auto pending_before = harness.scheduler.pending_count();
            CHECK_FALSE(harness.monitor.activate(
                candidate, harness.scheduler));
            CHECK(harness.monitor.active_view() == active_before);
            CHECK(harness.scheduler.pending_deadline() == deadline_before);
            CHECK(harness.scheduler.scheduled_count() == scheduled_before);
            CHECK(harness.scheduler.pending_count() == pending_before);
        };
    require_monotonic_rejection(view(first_config, 51, 0));
    require_monotonic_rejection(view(second_config, 52, 2));
    require_monotonic_rejection(view(second_config, 50, 2));

    CHECK_FALSE(harness.monitor.record_verified_progress(
        first, LeaderProgressEvent::commit, harness.scheduler));
    CHECK_FALSE(harness.monitor.record_verified_progress(
        second, LeaderProgressEvent::commit, harness.scheduler));
    harness.scheduler.fire_even_if_cancelled(first_deadline);
    harness.scheduler.fire_even_if_cancelled(second_deadline);
    CHECK(harness.monitor.active_view() == reused);
    CHECK(harness.rotations.empty());
    CHECK(harness.scheduler.pending_count() == 1);
}

TEST_CASE("aggregation deadlines cannot reset or expire leader progress",
          "[l07][leader-progress][aggregation-independence]"
          "[intentional-red]")
{
    MonitorHarness harness;
    const auto active = view(configuration(14, 7, "independent"), 60, 5);
    REQUIRE(harness.monitor.activate(active, harness.scheduler));
    harness.scheduler.advance_by(Duration(100));
    const auto leader_deadline = harness.scheduler.pending_deadline();
    const auto scheduled = harness.scheduler.scheduled_count();

    // Advancing by the configured maximum aggregation interval is only the
    // passage of monotonic time.  There is deliberately no aggregation event
    // or effect on the leader monitor.
    harness.scheduler.advance_by(Duration(40));
    CHECK(harness.rotations.empty());
    CHECK(harness.scheduler.pending_deadline() == leader_deadline);
    CHECK(harness.scheduler.scheduled_count() == scheduled);

    CHECK_FALSE(has_aggregation_effect<LeaderProgressEffects>::value);
    CHECK_FALSE(has_signing_effect<LeaderProgressEffects>::value);
    CHECK_FALSE(has_epoch_staging_effect<LeaderProgressEffects>::value);
    CHECK_FALSE(has_epoch_activation_effect<LeaderProgressEffects>::value);
}

TEST_CASE("timer effects and cancellation execute outside monitor lock",
          "[l07][leader-progress][reentrant][lock-safety]"
          "[intentional-red]")
{
    FakeLeaderProgressScheduler scheduler;
    const auto first = view(configuration(17, 1, "reentrant-first"), 80, 0);
    const auto second =
        view(configuration(17, 2, "reentrant-second"), 81, 2);
    const auto third = view(configuration(17, 3, "reentrant-third"), 82, 4);
    std::vector<LeaderViewId> rotations;
    LeaderProgressMonitor *owner = nullptr;
    bool expiry_reentered = false;
    bool cancellation_reentered = false;

    LeaderProgressEffects effects;
    effects.rotate_active_view = [&](const LeaderViewId &expired) {
        rotations.push_back(expired);
        expiry_reentered = owner->activate(second, scheduler);
    };
    LeaderProgressMonitor monitor(progress_config(), std::move(effects));
    owner = &monitor;

    REQUIRE(monitor.activate(first, scheduler));
    scheduler.advance_by(Duration(100));
    scheduler.advance_by(Duration(200));
    REQUIRE(expiry_reentered);
    REQUIRE(rotations == std::vector<LeaderViewId>{first});
    CHECK(monitor.active_view() == second);
    CHECK(scheduler.pending_count() == 1);

    scheduler.observe_cancellation([&]() {
        cancellation_reentered = monitor.active_view().has_value();
    });
    REQUIRE(monitor.activate(third, scheduler));
    CHECK(cancellation_reentered);
    CHECK(monitor.active_view() == third);
    scheduler.observe_cancellation({});
    monitor.shutdown();
}

TEST_CASE("scheduler cancellation can wait for a cross-thread monitor callback",
          "[l07][leader-progress][cross-thread][cancellation]"
          "[intentional-red]")
{
    const auto exercise = [](const std::string &label,
                             const std::function<void(
                                 LeaderProgressMonitor &,
                                 FakeLeaderProgressScheduler &,
                                 const LeaderViewId &)> &operation) {
        FakeLeaderProgressScheduler scheduler;
        const auto first =
            view(configuration(17, 10, label + "-first"), 83, 0);
        std::vector<LeaderViewId> rotations;
        LeaderProgressMonitor monitor(
            progress_config(), effects_for(rotations));
        REQUIRE(monitor.activate(first, scheduler));

        std::vector<std::thread> callback_threads;
        bool cancellation_timed_out = false;
        scheduler.observe_cancellation([&]() {
            auto completed = std::make_shared<std::promise<void>>();
            auto completion = completed->get_future();
            callback_threads.emplace_back([&monitor, completed]() {
                static_cast<void>(monitor.active_view());
                completed->set_value();
            });
            cancellation_timed_out =
                completion.wait_for(std::chrono::milliseconds(250)) !=
                std::future_status::ready;
        });

        operation(monitor, scheduler, first);
        scheduler.observe_cancellation({});
        for (auto &callback_thread : callback_threads)
            callback_thread.join();

        CHECK_FALSE(cancellation_timed_out);
        monitor.shutdown();
    };

    SECTION("activation")
    {
        exercise(
            "cross-thread-activate",
            [](LeaderProgressMonitor &monitor,
               FakeLeaderProgressScheduler &scheduler,
               const LeaderViewId &) {
                const auto replacement = view(
                    configuration(17, 11, "cross-thread-activate-next"),
                    84,
                    1);
                REQUIRE(monitor.activate(replacement, scheduler));
            });
    }

    SECTION("progress reset")
    {
        exercise(
            "cross-thread-reset",
            [](LeaderProgressMonitor &monitor,
               FakeLeaderProgressScheduler &scheduler,
               const LeaderViewId &active) {
                scheduler.observe_cancellation({});
                scheduler.advance_by(Duration(100));
                bool cancellation_timed_out = false;
                std::thread callback_thread;
                scheduler.observe_cancellation([&]() {
                    auto completed = std::make_shared<std::promise<void>>();
                    auto completion = completed->get_future();
                    callback_thread = std::thread([&monitor, completed]() {
                        static_cast<void>(monitor.active_view());
                        completed->set_value();
                    });
                    cancellation_timed_out =
                        completion.wait_for(std::chrono::milliseconds(250)) !=
                        std::future_status::ready;
                });
                REQUIRE(monitor.record_verified_progress(
                    active,
                    LeaderProgressEvent::quorum_certificate,
                    scheduler));
                scheduler.observe_cancellation({});
                callback_thread.join();
                CHECK_FALSE(cancellation_timed_out);
            });
    }

    SECTION("shutdown")
    {
        exercise(
            "cross-thread-shutdown",
            [](LeaderProgressMonitor &monitor,
               FakeLeaderProgressScheduler &,
               const LeaderViewId &) { monitor.shutdown(); });
    }
}

TEST_CASE("leader rotation can wait for a cross-thread monitor callback",
          "[l07][leader-progress][cross-thread][rotation]"
          "[intentional-red]")
{
    FakeLeaderProgressScheduler scheduler;
    const auto active =
        view(configuration(17, 12, "cross-thread-rotation"), 85, 2);
    LeaderProgressMonitor *owner = nullptr;
    std::thread callback_thread;
    bool rotation_timed_out = false;

    LeaderProgressEffects effects;
    effects.rotate_active_view = [&](const LeaderViewId &) {
        auto completed = std::make_shared<std::promise<void>>();
        auto completion = completed->get_future();
        callback_thread = std::thread([&owner, completed]() {
            static_cast<void>(owner->active_view());
            completed->set_value();
        });
        rotation_timed_out =
            completion.wait_for(std::chrono::milliseconds(250)) !=
            std::future_status::ready;
    };
    LeaderProgressMonitor monitor(progress_config(), std::move(effects));
    owner = &monitor;

    REQUIRE(monitor.activate(active, scheduler));
    scheduler.advance_by(Duration(100));
    scheduler.advance_by(Duration(200));
    callback_thread.join();

    CHECK_FALSE(rotation_timed_out);
    monitor.shutdown();
}

TEST_CASE("leader expiry revalidates ownership after cancellation",
          "[l07][leader-progress][expiry-race][reentrant]"
          "[intentional-red]")
{
    SECTION("reactivating during cancellation suppresses the stale effect")
    {
        FakeLeaderProgressScheduler scheduler;
        const auto expiring =
            view(configuration(18, 1, "expiry-owner"), 90, 0);
        const auto replacement =
            view(configuration(18, 2, "replacement-owner"), 91, 2);
        std::vector<LeaderViewId> rotations;
        LeaderProgressMonitor monitor(
            progress_config(), effects_for(rotations));

        REQUIRE(monitor.activate(expiring, scheduler));
        scheduler.advance_by(Duration(100));
        REQUIRE(scheduler.pending_count() == 1);

        bool reactivated = false;
        scheduler.observe_cancellation([&]() {
            reactivated = monitor.activate(replacement, scheduler);
        });
        const auto dispatched = monitor.dispatch_timeout(expiring, 2);
        scheduler.observe_cancellation({});

        CHECK(dispatched);
        REQUIRE(reactivated);
        CHECK(monitor.active_view() == replacement);
        CHECK(rotations.empty());
        monitor.shutdown();
    }

    SECTION("shutdown during cancellation suppresses the stale effect")
    {
        FakeLeaderProgressScheduler scheduler;
        const auto expiring =
            view(configuration(18, 3, "shutdown-owner"), 92, 4);
        std::vector<LeaderViewId> rotations;
        LeaderProgressMonitor monitor(
            progress_config(), effects_for(rotations));

        REQUIRE(monitor.activate(expiring, scheduler));
        scheduler.advance_by(Duration(100));
        REQUIRE(scheduler.pending_count() == 1);

        bool shutdown_reentered = false;
        scheduler.observe_cancellation([&]() {
            shutdown_reentered = true;
            monitor.shutdown();
        });
        const auto dispatched = monitor.dispatch_timeout(expiring, 2);
        scheduler.observe_cancellation({});

        CHECK(dispatched);
        REQUIRE(shutdown_reentered);
        CHECK_FALSE(monitor.active_view().has_value());
        CHECK(rotations.empty());
    }
}

TEST_CASE("schedule failures preserve an atomic leader deadline",
          "[l07][leader-progress][schedule-failure][atomicity]"
          "[intentional-red]")
{
    SECTION("failed initial activation is absent and retryable")
    {
        MonitorHarness harness;
        const auto active =
            view(configuration(19, 1, "activation-retry"), 100, 1);

        harness.scheduler.fail_next_schedule();
        CHECK_THROWS_AS(
            harness.monitor.activate(active, harness.scheduler),
            std::runtime_error);
        CHECK_FALSE(harness.monitor.active_view().has_value());
        CHECK(harness.scheduler.pending_count() == 0);

        CHECK(harness.monitor.activate(active, harness.scheduler));
        CHECK(harness.monitor.active_view() == active);
        CHECK(harness.scheduler.pending_count() == 1);
        harness.monitor.shutdown();
    }

    SECTION("failed progress reset preserves the prior timeout")
    {
        MonitorHarness harness;
        const auto active =
            view(configuration(19, 2, "reset-preserve"), 101, 3);
        REQUIRE(harness.monitor.activate(active, harness.scheduler));
        harness.scheduler.advance_by(Duration(100));
        REQUIRE(harness.scheduler.pending_count() == 1);
        const auto original_deadline =
            harness.scheduler.pending_deadline();

        harness.scheduler.fail_next_schedule();
        CHECK_THROWS_AS(
            harness.monitor.record_verified_progress(
                active,
                LeaderProgressEvent::quorum_certificate,
                harness.scheduler),
            std::runtime_error);
        CHECK(harness.monitor.active_view() == active);
        CHECK(harness.scheduler.pending_count() == 1);
        CHECK(harness.scheduler.pending_deadline() == original_deadline);

        harness.scheduler.advance_by(Duration(200));
        CHECK(harness.rotations == std::vector<LeaderViewId>{active});
    }
}

TEST_CASE("throwing cancellation cannot strand a new active view",
          "[l07][leader-progress][cancellation-failure][atomicity]"
          "[intentional-red]")
{
    FakeLeaderProgressScheduler scheduler;
    const auto first =
        view(configuration(20, 1, "cancel-first"), 110, 0);
    const auto second =
        view(configuration(20, 2, "cancel-second"), 111, 2);
    std::vector<LeaderViewId> rotations;
    LeaderProgressMonitor monitor(
        progress_config(), effects_for(rotations));

    REQUIRE(monitor.activate(first, scheduler));
    scheduler.throw_on_cancellation(scheduler.latest_task_id());

    CHECK_NOTHROW(monitor.activate(second, scheduler));
    CHECK(monitor.active_view() == second);
    CHECK(scheduler.pending_count() == 1);
    CHECK(rotations.empty());
    monitor.shutdown();
}

TEST_CASE("one stable scheduler owns every monitor deadline",
          "[l07][leader-progress][scheduler-identity]"
          "[intentional-red]")
{
    FakeLeaderProgressScheduler owner;
    FakeLeaderProgressScheduler foreign;
    const auto active =
        view(configuration(21, 1, "scheduler-owner"), 120, 5);
    std::vector<LeaderViewId> rotations;
    LeaderProgressMonitor monitor(
        progress_config(), effects_for(rotations));

    REQUIRE(monitor.activate(active, owner));
    owner.advance_by(Duration(100));
    REQUIRE(owner.pending_count() == 1);
    const auto owner_deadline = owner.pending_deadline();

    CHECK_FALSE(monitor.record_verified_progress(
        active,
        LeaderProgressEvent::quorum_certificate,
        foreign));
    CHECK(owner.pending_count() == 1);
    CHECK(owner.pending_deadline() == owner_deadline);
    CHECK(foreign.scheduled_count() == 0);
    CHECK(monitor.active_view() == active);
    monitor.shutdown();
}

TEST_CASE("shutdown cancels and permanently closes leader progress",
          "[l07][leader-progress][shutdown][bounded][intentional-red]")
{
    MonitorHarness harness;
    const auto active = view(configuration(15, 8, "shutdown"), 70, 7);
    REQUIRE(harness.monitor.activate(active, harness.scheduler));
    const auto callback = harness.scheduler.latest_task_id();

    harness.monitor.shutdown();
    harness.monitor.shutdown();
    CHECK_FALSE(harness.monitor.active_view().has_value());
    CHECK(harness.scheduler.pending_count() == 0);
    harness.scheduler.fire_even_if_cancelled(callback);
    CHECK(harness.rotations.empty());
    CHECK_FALSE(harness.monitor.record_verified_progress(
        active, LeaderProgressEvent::commit, harness.scheduler));
    CHECK_FALSE(harness.monitor.activate(
        view(configuration(16, 1, "after-shutdown"), 71, 1),
        harness.scheduler));
    CHECK(harness.scheduler.pending_count() == 0);
}

TEST_CASE("unverified proposal admission remains leader-monitor inert",
          "[l07][leader-progress][source-audit][proposal-admission]")
{
    const auto admission = read_source("src/proposal_admission.cpp");
    const auto hotstuff = read_source("src/hotstuff.cpp");
    const auto wire_handler = source_slice(
        hotstuff,
        "void HotStuffBase::propose_handler",
        "void HotStuffBase::legacy_vote_handler");

    INFO("future, unknown, rejected, stale, malformed, and duplicate wire "
         "traffic must not be translated into verified leader progress");
    CHECK(admission.find("LeaderProgressEvent") == std::string::npos);
    CHECK(admission.find("record_verified_progress") == std::string::npos);
    CHECK(wire_handler.find("LeaderProgressEvent") == std::string::npos);
    CHECK(wire_handler.find("record_verified_progress") ==
          std::string::npos);
}

TEST_CASE("verified proposal progress is gated by protocol acceptance",
          "[l07][leader-progress][source-audit][proposal]"
          "[intentional-red]")
{
    const auto hotstuff = read_source("src/hotstuff.cpp");
    const auto active = source_slice(
        hotstuff,
        "void HotStuffBase::process_active",
        "void HotStuffBase::local_vote_authorized");
    const auto compact = without_whitespace(active);
    const auto assigned_protocol_result = compact.find(
        "constboolproposal_accepted="
        "owner.on_receive_proposal(parsed);");
    const auto protocol = compact.find(
        "owner.on_receive_proposal(parsed)",
        assigned_protocol_result);
    const auto acceptance_gate = compact.find(
        "if(proposal_accepted)", protocol);
    const auto progress =
        compact.find(
            "LeaderProgressEvent::verified_proposal",
            acceptance_gate);

    REQUIRE(assigned_protocol_result != std::string::npos);
    REQUIRE(protocol != std::string::npos);
    REQUIRE(acceptance_gate != std::string::npos);
    REQUIRE(progress != std::string::npos);
    INFO("discarding the protocol result would reset on a well-formed but "
         "low/stale proposal whose existing opinion decision is false");
    CHECK(assigned_protocol_result <= protocol);
    CHECK(protocol < acceptance_gate);
    CHECK(acceptance_gate < progress);
    CHECK(compact.find(
              "owner.on_receive_proposal(parsed)",
              protocol + 1U) == std::string::npos);
    CHECK(compact.find("metadata.key.configuration") !=
          std::string::npos);
    CHECK(compact.find("boolopinion") == std::string::npos);
}

TEST_CASE("locally authored proposals do not reset leader suspicion",
          "[l07][leader-progress][source-audit][local-proposal]"
          "[intentional-red]")
{
    const auto consensus = read_source("src/consensus.cpp");
    const auto hotstuff = read_source("src/hotstuff.cpp");
    const auto local = source_slice(
        consensus,
        "block_t HotStuffCore::on_propose",
        "Proposal HotStuffCore::process_block");
    const auto processed = local.find("process_block(");
    const auto local_hook =
        local.find("on_local_proposal_processed");
    const auto broadcast = local.find("do_broadcast_proposal(prop)");

    REQUIRE(processed != std::string::npos);
    REQUIRE(local_hook != std::string::npos);
    REQUIRE(broadcast != std::string::npos);
    CHECK(processed < local_hook);
    CHECK(local_hook < broadcast);
    CHECK(count_occurrences(
              local, "on_local_proposal_processed") == 1);
    const auto hook_end = local.find(';', local_hook);
    REQUIRE(hook_end != std::string::npos);
    const auto hook_call =
        local.substr(local_hook, hook_end - local_hook);
    CHECK(hook_call.find("prop.key()") != std::string::npos);

    const auto owner = source_slice(
        hotstuff,
        "void HotStuffBase::on_local_proposal_processed",
        "void HotStuffBase::on_verified_commit_progress");
    REQUIRE_FALSE(owner.empty());
    INFO("a proposer cannot keep its own view alive with self-authored "
         "traffic; quorum certificates and commits remain valid progress");
    CHECK(owner.find("record_verified_progress") == std::string::npos);
    CHECK(owner.find("LeaderProgressEvent") == std::string::npos);
}

TEST_CASE("every leader-local proposal path records local processing only",
          "[l07][rem-l07-02][leader-progress][source-audit]"
          "[local-proposal][pipelined-proposal][intentional-red]")
{
    const auto consensus = code_without_comments_or_literals(
        read_source("src/consensus.cpp"));
    const auto hotstuff = code_without_comments_or_literals(
        read_source("src/hotstuff.cpp"));
    const auto ordinary = function_body(
        consensus, "block_t HotStuffCore::on_propose");
    const auto pipelined = function_body(
        hotstuff, "void HotStuffBase::beat()");

    const auto require_hook_between_processing_and_broadcast = [](
        const std::string &path,
        const std::string &path_name) {
        INFO("leader-local proposal path: " << path_name);
        const auto processed = path.find("Proposal prop = process_block(");
        REQUIRE(processed != std::string::npos);
        REQUIRE(count_occurrences(
                    path, "Proposal prop = process_block(") == 1);
        const auto processing_complete = path.find(';', processed);
        REQUIRE(processing_complete != std::string::npos);
        const auto broadcast = path.find(
            "do_broadcast_proposal(prop);", processing_complete);
        REQUIRE(broadcast != std::string::npos);

        const auto after_processing = path.substr(
            processing_complete + 1,
            broadcast - processing_complete - 1);
        const auto normalized = without_whitespace(after_processing);
        const std::string exact_hook =
            "on_local_proposal_processed(prop.key());";
        CHECK(count_occurrences(normalized, exact_hook) == 1);
    };

    SECTION("ordinary proposal production")
    {
        require_hook_between_processing_and_broadcast(
            ordinary, "HotStuffCore::on_propose");
    }

    SECTION("pipelined proposal production")
    {
        const auto queued = pipelined.find(
            "piped_queue.push_back(piped_block->hash);");
        const auto processed = pipelined.find(
            "Proposal prop = process_block(");
        const auto marked_delivered = pipelined.find(
            "piped_block->piped_delivered = true;", processed);
        REQUIRE(queued != std::string::npos);
        REQUIRE(processed != std::string::npos);
        REQUIRE(marked_delivered != std::string::npos);
        CHECK(queued < processed);
        CHECK(processed < marked_delivered);
        require_hook_between_processing_and_broadcast(
            pipelined, "HotStuffBase::beat pipelined branch");
    }
}

TEST_CASE("QC and commit progress use one exact establishing certificate",
          "[l07][leader-progress][source-audit][qc][commit]"
          "[intentional-red]")
{
    const auto hotstuff = read_source("src/hotstuff.cpp");
    const auto consensus = read_source("src/consensus.cpp");
    const auto finish = source_slice(
        hotstuff,
        "void HotStuffBase::try_finish_exact_context",
        "void HotStuffBase::local_vote_authorized");
    const auto publish = source_slice(
        hotstuff,
        "bool HotStuffBase::publish_exact_root_qc",
        "void HotStuffBase::drain_ready_piped_qcs");
    const auto drain = source_slice(
        hotstuff,
        "void HotStuffBase::drain_ready_piped_qcs",
        "void HotStuffBase::try_finish_exact_context");
    const auto commit = source_slice(
        hotstuff,
        "void HotStuffBase::do_consensus_with_identity_provenance(",
        "void HotStuffBase::do_decide");
    const auto update = source_slice(
        consensus,
        "void HotStuffCore::update(const block_t &nblk)",
        "void HotStuffCore::on_receive_proposal");

    const auto verified = finish.find("final_qc->verify(config)");
    const auto claimed = finish.find("claim_root_qc_progress");
    const auto qc_progress =
        finish.find("LeaderProgressEvent::quorum_certificate");
    const auto publication = finish.find("publish_exact_root_qc");
    REQUIRE(verified != std::string::npos);
    REQUIRE(claimed != std::string::npos);
    REQUIRE(qc_progress != std::string::npos);
    REQUIRE(publication != std::string::npos);
    CHECK(verified < claimed);
    CHECK(claimed < qc_progress);
    CHECK(qc_progress < publication);
    const auto claim_gate = finish.substr(
        claimed, qc_progress - claimed);
    CHECK(claim_gate.find("if") != std::string::npos);
    const auto exact_qc_window = finish.substr(
        claimed, publication - claimed);
    const bool carries_exact_qc_identity =
        exact_qc_window.find("lease.key().configuration") !=
            std::string::npos ||
        exact_qc_window.find("lease.key()") != std::string::npos;
    CHECK(carries_exact_qc_identity);
    CHECK(count_occurrences(
              finish, "LeaderProgressEvent::quorum_certificate") == 1);
    CHECK(publish.find("LeaderProgressEvent::quorum_certificate") ==
          std::string::npos);
    CHECK(drain.find("LeaderProgressEvent::quorum_certificate") ==
          std::string::npos);

    const auto commit_hook =
        update.find("on_verified_commit_progress");
    const auto backlog =
        update.find(
            "for (std::size_t queue_index = commit_queue.size()");
    REQUIRE(commit_hook != std::string::npos);
    REQUIRE(backlog != std::string::npos);
    const auto backlog_open = update.find('{', backlog);
    const auto backlog_close =
        matching_closing_brace(update, backlog_open);
    REQUIRE(backlog_close != std::string::npos);
    CHECK(commit_hook > backlog_close);
    CHECK(count_occurrences(update, "on_verified_commit_progress") == 1);
    const auto hook_end = update.find(';', commit_hook);
    REQUIRE(hook_end != std::string::npos);
    const auto hook_call =
        update.substr(commit_hook, hook_end - commit_hook);
    CHECK(hook_call.find("blk1->qc") != std::string::npos);
    CHECK(hook_call.find("get_proposal_key") != std::string::npos);
    CHECK(commit.find("LeaderProgressEvent::commit") ==
          std::string::npos);
    CHECK(commit.find("on_verified_commit_progress") ==
          std::string::npos);

    const auto commit_owner = hotstuff.find(
        "HotStuffBase::on_verified_commit_progress");
    REQUIRE(commit_owner != std::string::npos);
    const auto commit_event = hotstuff.find(
        "LeaderProgressEvent::commit", commit_owner);
    REQUIRE(commit_event != std::string::npos);
    const auto commit_owner_window = hotstuff.substr(
        commit_owner, commit_event - commit_owner + 200);
    CHECK(commit_owner_window.find("key.configuration") !=
          std::string::npos);
}

TEST_CASE("aggregation implementation cannot drive leader progress",
          "[l07][leader-progress][source-audit][aggregation]")
{
    const auto aggregation = read_source("src/aggregation.cpp");
    CHECK(aggregation.find("LeaderProgressEvent") == std::string::npos);
    CHECK(aggregation.find("record_verified_progress") ==
          std::string::npos);
    CHECK(aggregation.find("rotate_active_view") == std::string::npos);
}

TEST_CASE("proposal admission exposes no dead leader timer callbacks",
          "[l07][leader-progress][source-audit][single-owner]"
          "[intentional-red]")
{
    const auto admission =
        read_source("include/hotstuff/proposal_admission.h");
    const auto hotstuff_header = read_source("include/hotstuff/hotstuff.h");
    const auto hotstuff_source = read_source("src/hotstuff.cpp");

    CHECK(admission.find("reset_leader_progress") == std::string::npos);
    CHECK(admission.find("expire_leader_progress") == std::string::npos);
    CHECK(hotstuff_header.find("reset_leader_progress") ==
          std::string::npos);
    CHECK(hotstuff_header.find("expire_leader_progress") ==
          std::string::npos);
    CHECK(hotstuff_source.find("HotStuffBase::reset_leader_progress") ==
          std::string::npos);
    CHECK(hotstuff_source.find("HotStuffBase::expire_leader_progress") ==
          std::string::npos);
}

TEST_CASE("view activation succeeds before exact proposal state mutates",
          "[l07][leader-progress][source-audit][activation-order]"
          "[intentional-red]")
{
    const auto hotstuff = read_source("src/hotstuff.cpp");
    const auto activation = source_slice(
        hotstuff,
        "void HotStuffBase::activate_proposal_configuration",
        "void HotStuffBase::relay_once");
    const auto view_activation = activation.find("activate_leader_view");
    const auto context_activation =
        activation.find("proposal_contexts->activate_configuration");
    const auto admission_activation =
        activation.find("proposal_admission->activate(configuration)");

    REQUIRE(view_activation != std::string::npos);
    REQUIRE(context_activation != std::string::npos);
    REQUIRE(admission_activation != std::string::npos);
    CHECK(view_activation < context_activation);
    CHECK(context_activation < admission_activation);
    const auto fail_closed_gate = activation.substr(
        view_activation, context_activation - view_activation);
    CHECK(fail_closed_gate.find("if") != std::string::npos);
    CHECK(fail_closed_gate.find("return") != std::string::npos);
}

TEST_CASE("startup aligns pacemaker before arming initial grace",
          "[l07][leader-progress][source-audit][startup]"
          "[intentional-red]")
{
    const auto hotstuff = read_source("src/hotstuff.cpp");
    const auto startup = source_slice(
        hotstuff,
        "void HotStuffBase::start(",
        "void HotStuffBase::beat()");
    const auto initialize = startup.find("pmaker->init(this)");
    const auto align = startup.find("update_tree_proposer()");
    auto activate = startup.find("activate_initial_leader_view");
    if (activate == std::string::npos)
        activate = startup.find("activate_proposal_configuration", align);
    REQUIRE(initialize != std::string::npos);
    REQUIRE(align != std::string::npos);
    REQUIRE(activate != std::string::npos);
    CHECK(initialize < align);
    CHECK(align < activate);

    const auto tree_scheduler = source_slice(
        hotstuff,
        "void HotStuffBase::tree_scheduler",
        "ReconfigurationType HotStuffBase::isTreeSwitch");
    const auto non_startup_activation =
        tree_scheduler.find("activate_proposal_configuration");
    REQUIRE(non_startup_activation != std::string::npos);
    const auto guard = tree_scheduler.rfind(
        "if (!startup", non_startup_activation);
    REQUIRE(guard != std::string::npos);
    const auto guard_open = tree_scheduler.find('{', guard);
    const auto guard_close =
        matching_closing_brace(tree_scheduler, guard_open);
    REQUIRE(guard_close != std::string::npos);
    CHECK(guard < non_startup_activation);
    CHECK(non_startup_activation < guard_close);
}

TEST_CASE("leader expiry owns only same-epoch active-tree rotation",
          "[l07][leader-progress][source-audit][rotation-owner]"
          "[intentional-red]")
{
    const auto liveness = read_source("include/hotstuff/liveness.h");
    const auto multitree = source_slice(
        liveness,
        "class PaceMakerMultitree",
        "class PMRoundRobinProposer");
    const auto rotation = function_body(
        multitree, "void rotate_active_tree_on_timeout");
    const auto activate = rotation.find("activate_leader_view");
    const auto tree_assignment = rotation.find("current_tid =");
    const auto schedule = rotation.find("tree_scheduler");

    REQUIRE(rotation.find("LeaderViewId") != std::string::npos);
    REQUIRE(activate != std::string::npos);
    REQUIRE(tree_assignment != std::string::npos);
    REQUIRE(schedule != std::string::npos);
    CHECK(activate < tree_assignment);
    CHECK(activate < schedule);
    CHECK(rotation.find("set_new_epoch") == std::string::npos);
    CHECK(rotation.find("EPOCH_SWITCH") == std::string::npos);
    CHECK(rotation.find("update_system_trees") == std::string::npos);
    CHECK(rotation.find("inc_time") == std::string::npos);

    const auto compact = without_whitespace(multitree);
    CHECK(compact.find("proposer_timeout") == std::string::npos);
    CHECK(compact.find("timeout_timer") == std::string::npos);
    CHECK(compact.find("timer.add(timeout)") == std::string::npos);
}

TEST_CASE("adaptive runtime activation releases the new leader's first beat",
          "[l07][leader-progress][source-audit][runtime-handoff]")
{
    const auto liveness = read_source("include/hotstuff/liveness.h");
    const auto multitree = source_slice(
        liveness,
        "class PaceMakerMultitree",
        "class PMRoundRobinProposer");
    const auto activation = function_body(
        multitree, "bool activate_runtime_view(const LeaderViewId &view)");
    const auto handoff = function_body(
        multitree, "void arm_runtime_leader_handoff()");
    const auto scheduling = function_body(
        multitree, "void schedule_next() override");

    REQUIRE_FALSE(activation.empty());
    const auto proposer = activation.find("proposer = view.leader_id");
    const auto arm = activation.find("arm_runtime_leader_handoff()");
    REQUIRE(proposer != std::string::npos);
    REQUIRE(arm != std::string::npos);
    CHECK(proposer < arm);

    REQUIRE_FALSE(handoff.empty());
    const auto retire = handoff.find("retire_beat_lane(hsc->get_hqc())");
    const auto ready_assignment =
        handoff.find("runtime_leader_handoff_ready =");
    const auto delay = handoff.find("arm_proposal_delay()");
    REQUIRE(retire != std::string::npos);
    REQUIRE(ready_assignment != std::string::npos);
    REQUIRE(delay != std::string::npos);
    CHECK(retire < ready_assignment);
    CHECK(ready_assignment < delay);

    REQUIRE_FALSE(scheduling.empty());
    const auto ready = scheduling.find("runtime_leader_handoff_ready");
    const auto pending = scheduling.find("pending_beats.empty()");
    const auto resolve = scheduling.find("pm.resolve(get_proposer())");
    REQUIRE(ready != std::string::npos);
    REQUIRE(pending != std::string::npos);
    REQUIRE(resolve != std::string::npos);
    CHECK(ready < pending);
    CHECK(pending < resolve);

    const auto wait_qc = source_slice(
        liveness, "class PMWaitQC", "class PaceMakerDummy");
    CHECK(wait_qc.find("++beat_lane_generation") != std::string::npos);
    CHECK(wait_qc.find("pending_beats.front().reject()") !=
          std::string::npos);
    CHECK(wait_qc.find("generation != beat_lane_generation") !=
          std::string::npos);
    CHECK(wait_qc.find("piped.erase(") != std::string::npos);
    CHECK(wait_qc.find("rebase->get_height()") != std::string::npos);
    CHECK(wait_qc.find("hsc->piped_submitted = false") !=
          std::string::npos);
}

TEST_CASE("local proposal diagnostics bracket the pre-broadcast wedge",
          "[l07][leader-progress][source-audit][handoff-diagnostic]")
{
    const auto consensus = read_source("src/consensus.cpp");
    const auto hotstuff = read_source("src/hotstuff.cpp");
    const auto liveness = read_source("include/hotstuff/liveness.h");
    const auto proposal_context = read_source("src/proposal_context.cpp");
    const auto ordinary = function_body(
        consensus, "block_t HotStuffCore::on_propose(");
    const auto process = function_body(
        consensus, "Proposal HotStuffCore::process_block(");
    const auto candidate = function_body(
        hotstuff,
        "HotStuffBase::make_exact_direct_forwarding_candidate(");
    const auto local_vote = function_body(
        hotstuff, "void HotStuffBase::apply_local_vote(");
    const auto record = function_body(
        proposal_context,
        "bool ProposalContextLifecycle::record_local_part(");
    const auto beat = function_body(
        hotstuff, "void HotStuffBase::beat()");
    const auto broadcast = function_body(
        hotstuff, "void HotStuffBase::do_broadcast_proposal(");
    const auto aggregation_timer = function_body(
        hotstuff, "void HotStuffBase::start_aggregation_timer(");
    const auto fallback = function_body(
        hotstuff,
        "void HotStuffBase::schedule_exact_proposal_fallback(");
    const auto ingress = function_body(
        hotstuff, "void HotStuffBase::adaptive_propose_handler(");
    const auto multitree = source_slice(
        liveness,
        "class PaceMakerMultitree",
        "class PMRoundRobinProposer");
    const auto unlock = function_body(multitree, "void unlock(TimerEvent &)");

    REQUIRE_FALSE(ordinary.empty());
    CHECK(contains_in_order(
        ordinary,
        {"stage=ordinary_process_begin",
         "process_block(",
         "stage=ordinary_process_end",
         "stage=ordinary_hook_begin",
         "on_local_proposal_processed(prop.key())",
         "stage=ordinary_hook_end",
         "stage=ordinary_broadcast_begin",
         "do_broadcast_proposal(prop)",
         "stage=ordinary_broadcast_end"}));

    REQUIRE_FALSE(process.empty());
    CHECK(contains_in_order(
        process,
        {"stage=local_vote_begin",
         "on_receive_vote(",
         "stage=local_vote_end",
         "stage=proposal_notify_begin",
         "on_propose_(prop)",
         "stage=proposal_notify_end"}));

    REQUIRE_FALSE(candidate.empty());
    CHECK(contains_in_order(
        candidate,
        {"stage=candidate_compute_begin",
         "certificate->compute()",
         "stage=candidate_compute_end",
         "stage=candidate_verify_begin",
         "certificate->verify(config)",
         "stage=candidate_verify_end"}));

    REQUIRE_FALSE(local_vote.empty());
    CHECK(contains_in_order(
        local_vote,
        {"stage=candidate_begin",
         "make_exact_direct_forwarding_candidate",
         "stage=candidate_end",
         "stage=record_begin",
         "record_local_part",
         "stage=record_end",
         "stage=finish_begin",
         "try_finish_exact_context",
         "stage=finish_end"}));

    REQUIRE_FALSE(record.empty());
    CHECK(contains_in_order(
        record,
        {"stage=record_verify_begin",
         "forwarding_candidate->verify(config)",
         "stage=record_verify_end",
         "stage=record_lock_begin",
         "std::lock_guard<std::mutex> lock(mutex_)",
         "stage=record_lock_end"}));

    REQUIRE_FALSE(beat.empty());
    CHECK(contains_in_order(
        beat,
        {"stage=piped_process_begin",
         "process_block(",
         "stage=piped_process_end",
         "stage=piped_broadcast_begin",
         "do_broadcast_proposal(prop)",
         "stage=piped_broadcast_end",
         "stage=piped_scope_tail",
         "piped_submitted = false",
         "stage=piped_tail_released",
         "stage=piped_tail_end",
         "stage=beat_callback_exit"}));

    REQUIRE_FALSE(unlock.empty());
    CHECK(contains_in_order(
        unlock,
        {"stage=unlock_schedule_begin",
         "schedule_next()",
         "stage=unlock_schedule_end"}));

    REQUIRE_FALSE(broadcast.empty());
    CHECK(contains_in_order(
        broadcast,
        {"stage=entry outcome=begin",
         "reason=exact_tree_missing",
         "reason=inadmissible_context",
         "stage=context outcome=ready",
         "stage=local_timers",
         "start_aggregation_timer(prop.key())",
         "stage=payload outcome=ready",
         "bool enqueued = false",
         "++send_attempts",
         "++send_successes",
         "stage=direct_children",
         "stage=fallback_call",
         "schedule_exact_proposal_fallback(*lease, prop)",
         "stage=summary outcome=complete"}));
    CHECK(broadcast.find("reason=runtime_missing") != std::string::npos);
    CHECK(broadcast.find("reason=generation_missing") != std::string::npos);
    CHECK(broadcast.find("reason=encode_exception") != std::string::npos);
    CHECK(broadcast.find("reason=empty_payload") != std::string::npos);
    CHECK(broadcast.find("generation=%llu") != std::string::npos);
    CHECK(broadcast.find("payload_bytes=%zu") != std::string::npos);

    REQUIRE_FALSE(aggregation_timer.empty());
    CHECK(contains_in_order(
        aggregation_timer,
        {"reason=precondition",
         "arm_timeout(",
         "timer_generation == 0 ? \"skipped\" : \"armed\""}));

    REQUIRE_FALSE(fallback.empty());
    CHECK(contains_in_order(
        fallback,
        {"reason=precondition",
         "reason=exact_runtime",
         "schedule_after(",
         "reason=schedule_rejected",
         "outcome=armed",
         "reason=schedule_exception"}));

    REQUIRE_FALSE(ingress.empty());
    CHECK(contains_in_order(
        ingress,
        {"const auto authenticated_peer",
         "KAURI_PROPOSAL_INGRESS stage=begin",
         "const auto result = epoch_live_binding->handle_proposal(",
         "result.decoded_envelope",
         "KAURI_PROPOSAL_INGRESS stage=result"}));
    CHECK(ingress.find("result.error") != std::string::npos);
    CHECK(ingress.find("result.permission") != std::string::npos);
    CHECK(ingress.find("result.admission_disposition") !=
          std::string::npos);
    CHECK(ingress.find("recipient=%u") != std::string::npos);
    CHECK(ingress.find("source_replica=%u") != std::string::npos);
    CHECK(ingress.find("source_peer=%s") != std::string::npos);
    CHECK(ingress.find("envelope->configuration.epoch_number") !=
          std::string::npos);
    CHECK(ingress.find("envelope->configuration.tree_id") !=
          std::string::npos);
    CHECK(ingress.find("envelope->block_hash.to_hex()") !=
          std::string::npos);
}

TEST_CASE("proposal tail diagnostics bracket relay and callback completion",
          "[l07][leader-progress][source-audit][tail-diagnostic]")
{
    const auto hotstuff = read_source("src/hotstuff.cpp");
    const auto proposal_context = read_source("src/proposal_context.cpp");
    const auto record = function_body(
        proposal_context,
        "bool ProposalContextLifecycle::record_local_part(");
    const auto relay_send = function_body(
        hotstuff, "bool HotStuffBase::send_exact_relay_reserved(");
    const auto relay_ingress = function_body(
        hotstuff, "void HotStuffBase::adaptive_relay_handler(");
    const auto fallback_dispatch = function_body(
        hotstuff,
        "bool HotStuffBase::broadcast_exact_proposal_fallback(");
    const auto active_processing = function_body(
        hotstuff, "bool HotStuffBase::process_active(");

    REQUIRE_FALSE(record.empty());
    CHECK(contains_in_order(
        record,
        {"found->second->accumulator = std::move(next)",
         "found->second->runtime->verified_signers = std::move(expected)",
         "stage=record_return",
         "return true"}));

    REQUIRE_FALSE(relay_send.empty());
    CHECK(contains_in_order(
        relay_send,
        {"stage=send_begin",
         "send_exact_relay(",
         "stage=send_return",
         "stage=claim_commit_begin",
         "commit_forwarding_claim(",
         "stage=claim_commit_result",
         "stage=complete_begin",
         "complete_exact_forwarding(",
         "stage=complete_end"}));
    CHECK(relay_send.find("trace_non_root") != std::string::npos);
    CHECK(relay_send.find("parent=%u") != std::string::npos);
    CHECK(relay_send.find("root=%u") != std::string::npos);

    REQUIRE_FALSE(relay_ingress.empty());
    CHECK(contains_in_order(
        relay_ingress,
        {"KAURI_RELAY_INGRESS stage=begin",
         "const auto result = epoch_live_binding->handle_relay(",
         "KAURI_RELAY_INGRESS stage=result",
         "KAURI_RELAY_INGRESS stage=dispatch_complete"}));
    CHECK(relay_ingress.find("recipient=%u") != std::string::npos);
    CHECK(relay_ingress.find("source_replica=%u") != std::string::npos);
    CHECK(relay_ingress.find("root=%u") != std::string::npos);
    CHECK(relay_ingress.find("source_peer") == std::string::npos);

    REQUIRE_FALSE(fallback_dispatch.empty());
    CHECK(contains_in_order(
        fallback_dispatch,
        {"bool enqueued = false",
         "proposal_contexts->snapshot(proposal.key())",
         "snapshot->verified_signers.count(member) != 0",
         "++send_attempts",
         "pn.send_msg(",
         "stage=fallback_target_result",
         "stage=fallback_dispatch_summary",
         "return enqueued"}));
    const auto target_result = source_slice(
        fallback_dispatch,
        "stage=fallback_target_result",
        "enqueued = sent || enqueued");
    CHECK(target_result.find("root=%u") != std::string::npos);
    CHECK(target_result.find("target=%u") != std::string::npos);
    CHECK(target_result.find("epoch=%u") != std::string::npos);
    CHECK(target_result.find("tree=%u") != std::string::npos);
    CHECK(target_result.find("block=%s") != std::string::npos);
    CHECK(target_result.find("peer") == std::string::npos);
    CHECK(target_result.find("payload") == std::string::npos);

    REQUIRE_FALSE(active_processing.empty());
    CHECK(contains_in_order(
        active_processing,
        {"delivery.then(",
         "log_callback(\"callback_begin\"",
         "owner.on_receive_proposal(parsed)",
         "log_callback(\"callback_end\""}));
    CHECK(active_processing.find(
              "log_callback(\"callback_abort\"") !=
          std::string::npos);
    CHECK(active_processing.find("recipient=%u") != std::string::npos);
    CHECK(active_processing.find("root=%u") != std::string::npos);
    const auto callback_marker = source_slice(
        active_processing,
        "KAURI_PROPOSAL_PROCESS stage=%s",
        "log_callback(\"callback_begin\"");
    CHECK(callback_marker.find("payload") == std::string::npos);
}

TEST_CASE("pacemaker shuts leader monitor down before runtime teardown",
          "[l07][leader-progress][source-audit][shutdown-order]"
          "[intentional-red]")
{
    const auto hotstuff = read_source("src/hotstuff.cpp");
    const auto destructor = function_body(
        hotstuff, "HotStuffBase::~HotStuffBase()");
    const auto pacemaker = destructor.find("pmaker->shutdown()");
    const auto callbacks =
        destructor.find("exact_runtime_access->close_and_wait()");
    const auto contexts =
        destructor.find("proposal_contexts->shutdown()");
    REQUIRE(pacemaker != std::string::npos);
    REQUIRE(callbacks != std::string::npos);
    REQUIRE(contexts != std::string::npos);
    CHECK(pacemaker < callbacks);
    CHECK(callbacks < contexts);

    const auto liveness = read_source("include/hotstuff/liveness.h");
    const auto multitree = source_slice(
        liveness,
        "class PaceMakerMultitree",
        "class PMRoundRobinProposer");
    const auto shutdown = function_body(multitree, "void shutdown()");
    CHECK(shutdown.find("leader_progress") != std::string::npos);
    CHECK(count_occurrences(shutdown, "shutdown()") >= 2);
    CHECK(shutdown.find(".del()") != std::string::npos);
}

TEST_CASE("adaptive leader progress options replace the application timer",
          "[l07][leader-progress][source-audit][cli][intentional-red]")
{
    const auto app = read_source("examples/hotstuff_app.cpp");
    const auto compact = without_whitespace(app);
    const auto hotstuff = read_source("src/hotstuff.cpp");

    const std::vector<std::string> options = {
        "aggregation-timeout",
        "leader-progress-timeout",
        "leader-activation-grace"};
    for (const auto &option : options)
    {
        const auto binding = option_binding(app, option);
        INFO("missing adaptive option or binding: " << option);
        REQUIRE(binding.has_value());
        CHECK(app.find(*binding + "->get()") != std::string::npos);
    }
    CHECK(hotstuff.find("std::chrono::milliseconds(500)") ==
          std::string::npos);

    INFO("adaptive mode must not retain the application-owned impeachment "
         "timer; imp-timeout may remain only for legacy static behavior");
    CHECK(app.find("impeach_timer") == std::string::npos);
    CHECK(app.find("reset_imp_timer") == std::string::npos);
    CHECK(compact.find("->impeach()") == std::string::npos);
}

TEST_CASE("multitree leader liveness honors constructor configuration",
          "[l07][leader-progress][source-audit][configuration]"
          "[intentional-red]")
{
    const auto liveness = read_source("include/hotstuff/liveness.h");
    const auto multitree = source_slice(
        liveness,
        "class PaceMakerMultitree",
        "class PMRoundRobinProposer");
    const auto compact = without_whitespace(multitree);

    INFO("PaceMakerMultitree currently discards the constructor values and "
         "replaces them with 10, 20, and 0");
    CHECK(compact.find("base_timeout(10)") == std::string::npos);
    CHECK(compact.find("timeout(20)") == std::string::npos);
    CHECK(compact.find("prop_delay(0)") == std::string::npos);
    CHECK(compact.find("timeout=20;") == std::string::npos);
}

TEST_CASE("a later descendant QC consumes the stalled pipelined prefix",
          "[l07][pipeline][qc-supersession][descendant]"
          "[intentional-red]")
{
    hotstuff::test::CommitRuleCore core;
    const auto genesis = core.get_genesis();
    const auto predecessor = core.add_block(genesis, genesis);
    const auto descendant = core.add_block(predecessor, predecessor);
    const auto retained = core.add_block(descendant, descendant);
    std::deque<hotstuff::uint256_t> piped{
        predecessor->get_hash(), descendant->get_hash()};
    std::deque<hotstuff::uint256_t> ready{
        predecessor->get_hash(), descendant->get_hash(),
        retained->get_hash()};

    REQUIRE(hotstuff::detail::consume_delivered_ancestor_piped_prefix(
        piped, ready, descendant, *core.storage));

    CHECK(piped.empty());
    REQUIRE(ready.size() == 1);
    CHECK(ready.front() == retained->get_hash());
}

TEST_CASE("a later fork QC cannot consume the pipelined head",
          "[l07][pipeline][qc-supersession][fork][safety]"
          "[intentional-red]")
{
    hotstuff::test::CommitRuleCore core;
    const auto genesis = core.get_genesis();
    const auto predecessor = core.add_block(genesis, genesis);
    const auto competing_parent = core.add_block(genesis, genesis);
    const auto fork = core.add_block(competing_parent, competing_parent);
    std::deque<hotstuff::uint256_t> piped{
        predecessor->get_hash(), fork->get_hash()};
    std::deque<hotstuff::uint256_t> ready{fork->get_hash()};
    const auto original_piped = piped;
    const auto original_ready = ready;

    CHECK_FALSE(
        hotstuff::detail::consume_delivered_ancestor_piped_prefix(
            piped, ready, fork, *core.storage));

    CHECK(piped == original_piped);
    CHECK(ready == original_ready);
}

TEST_CASE("pipelined QC queue cleanup covers one and two async blocks",
          "[l07][pipeline][qc-supersession][queue-cleanup]"
          "[intentional-red]")
{
    SECTION("one queued block publishes directly")
    {
        hotstuff::test::CommitRuleCore core;
        const auto block =
            core.add_block(core.get_genesis(), core.get_genesis());
        std::deque<hotstuff::uint256_t> piped{block->get_hash()};
        std::deque<hotstuff::uint256_t> ready{block->get_hash()};

        REQUIRE(
            hotstuff::detail::consume_delivered_ancestor_piped_prefix(
                piped, ready, block, *core.storage));
        CHECK(piped.empty());
        CHECK(ready.empty());
    }

    SECTION("two queued blocks publish the ready descendant")
    {
        hotstuff::test::CommitRuleCore core;
        const auto genesis = core.get_genesis();
        const auto first = core.add_block(genesis, genesis);
        const auto second = core.add_block(first, first);
        std::deque<hotstuff::uint256_t> piped{
            first->get_hash(), second->get_hash()};
        std::deque<hotstuff::uint256_t> ready{second->get_hash()};

        REQUIRE(
            hotstuff::detail::consume_delivered_ancestor_piped_prefix(
                piped, ready, second, *core.storage));
        CHECK(piped.empty());
        CHECK(ready.empty());
    }
}

TEST_CASE("a superseded predecessor remains eligible for a late QC",
          "[l07][pipeline][qc-supersession][late-predecessor]"
          "[intentional-red]")
{
    hotstuff::test::CommitRuleCore core;
    const auto genesis = core.get_genesis();
    const auto predecessor = core.add_block(genesis, genesis);
    const auto descendant = core.add_block(predecessor, predecessor);
    std::deque<hotstuff::uint256_t> piped{
        predecessor->get_hash(), descendant->get_hash()};
    std::deque<hotstuff::uint256_t> ready;

    REQUIRE(hotstuff::detail::consume_delivered_ancestor_piped_prefix(
        piped, ready, descendant, *core.storage));
    REQUIRE(piped.empty());
    REQUIRE(hotstuff::detail::consume_delivered_ancestor_piped_prefix(
        piped, ready, predecessor, *core.storage));
    CHECK(piped.empty());
    CHECK(ready.empty());
}

TEST_CASE("blocked root QC evidence uses both retained exact contexts",
          "[l07][pipeline][root-qc-queue-blocked][context]"
          "[intentional-red]")
{
    hotstuff::test::CommitRuleCore core;
    const auto genesis = core.get_genesis();
    const auto head = core.add_block(genesis, genesis);
    const auto candidate = core.add_block(head, head);
    const auto active = configuration(2, 0, "blocked-root-qc");
    const hotstuff::ProposalKey head_key{active, head->get_hash()};
    const hotstuff::ProposalKey candidate_key{
        active, candidate->get_hash()};

    hotstuff::ProposalContextLifecycle contexts;
    const auto head_lease = contexts.admit_local(
        root_context_metadata(head_key));
    const auto candidate_lease = contexts.admit_local(
        root_context_metadata(candidate_key));
    REQUIRE(head_lease.has_value());
    REQUIRE(candidate_lease.has_value());
    contexts.activate_configuration(active);

    REQUIRE(contexts.record_local_signer(*head_lease));
    REQUIRE(contexts.record_verified_aggregate(
        *head_lease, 1, std::set<hotstuff::ReplicaID>{1, 3, 4}));
    REQUIRE(contexts.record_local_signer(*candidate_lease));
    REQUIRE(contexts.record_verified_aggregate(
        *candidate_lease,
        1,
        std::set<hotstuff::ReplicaID>{1, 3, 4}));
    REQUIRE(contexts.record_verified_aggregate(
        *candidate_lease, 2, std::set<hotstuff::ReplicaID>{2}));

    const std::deque<hotstuff::uint256_t> piped{
        head->get_hash(), candidate->get_hash()};
    const auto event =
        hotstuff::detail::make_root_qc_queue_blocked_event(
            *candidate_lease,
            contexts,
            piped,
            candidate,
            *core.storage,
            0);
    REQUIRE(event.has_value());
    CHECK(event->configuration == active);
    CHECK(event->observer_replica == 0);
    CHECK(event->global_quorum == 5);
    CHECK(event->queue_head_position == 0);
    CHECK(event->queued_candidate_position == 1);
    CHECK(event->queue_head_context_generation ==
          head_lease->generation());
    CHECK(event->queued_candidate_context_generation ==
          candidate_lease->generation());
    CHECK(event->queue_head_block_height == head->get_height());
    CHECK(event->queue_head_block_hash == head->get_hash());
    CHECK(event->queued_candidate_block_height ==
          candidate->get_height());
    CHECK(event->queued_candidate_block_hash ==
          candidate->get_hash());
    CHECK(event->queued_candidate_parent_hash == head->get_hash());
    CHECK(event->queue_head_signer_count == 4);
    CHECK(event->queued_candidate_signer_count == 5);
    CHECK(event->queued_candidate_qc_ready);
    CHECK_FALSE(event->queued_candidate_qc_published);

    CHECK_FALSE(hotstuff::detail::make_root_qc_queue_blocked_event(
        *candidate_lease,
        contexts,
        piped,
        candidate,
        *core.storage,
        1));
}

TEST_CASE("exact root QC supersession stays synchronous and fail closed",
          "[l07][pipeline][qc-supersession][integration]"
          "[intentional-red]")
{
    const auto hotstuff = read_source("src/hotstuff.cpp");
    const auto finish = source_slice(
        hotstuff,
        "void HotStuffBase::try_finish_exact_context",
        "void HotStuffBase::local_vote_authorized");
    const auto publish = source_slice(
        hotstuff,
        "bool HotStuffBase::publish_exact_root_qc",
        "void HotStuffBase::drain_ready_piped_qcs");

    const auto eligibility = finish.find(
        "proposal_contexts->clone_publishable_root_qc(lease)");
    const auto verify = finish.find("final_qc->verify(config)");
    const auto publication = finish.find("publish_exact_root_qc");
    const auto revalidate = publish.find("proposal_contexts->revalidate");
    const auto queued = publish.find("queued_behind_head");
    const auto active = publish.find("active_configuration", queued);
    const auto active_guard = publish.rfind(
        "if (queued_behind_head)", active);
    const auto consume = publish.find(
        "consume_delivered_ancestor_piped_prefix");
    const auto blocked_evidence = publish.find(
        "emit_root_qc_queue_blocked_event");
    const auto ready_queue = publish.find("rdy_queue.push_back");
    const auto update = publish.find("update_hqc");
    const auto resolve = publish.find("on_qc_finish");
    const auto success = publish.rfind("return true");

    REQUIRE(eligibility != std::string::npos);
    REQUIRE(verify != std::string::npos);
    REQUIRE(publication != std::string::npos);
    REQUIRE(revalidate != std::string::npos);
    REQUIRE(queued != std::string::npos);
    REQUIRE(active != std::string::npos);
    REQUIRE(active_guard != std::string::npos);
    REQUIRE(consume != std::string::npos);
    REQUIRE(blocked_evidence != std::string::npos);
    REQUIRE(ready_queue != std::string::npos);
    REQUIRE(update != std::string::npos);
    REQUIRE(resolve != std::string::npos);
    REQUIRE(success != std::string::npos);
    CHECK(eligibility < verify);
    CHECK(verify < publication);
    CHECK(revalidate < queued);
    CHECK(queued <= active_guard);
    CHECK(active_guard < active);
    CHECK(active < consume);
    CHECK(count_occurrences(
              publish,
              "proposal_contexts->active_configuration()") == 1);
    CHECK(publish.find("frozen_global_quorum") == std::string::npos);
    CHECK(publish.find("->has_n(") == std::string::npos);
    CHECK(publish.find("config.nmajority") == std::string::npos);
    CHECK(consume < blocked_evidence);
    CHECK(blocked_evidence < ready_queue);
    CHECK(consume < update);
    CHECK(update < resolve);
    CHECK(resolve < success);
    CHECK(publish.find("timeout") == std::string::npos);
    CHECK(publish.find("transition(") == std::string::npos);

    const auto evidence = source_slice(
        hotstuff,
        "void HotStuffBase::emit_root_qc_queue_blocked_event",
        "bool HotStuffBase::publish_exact_root_qc");
    CHECK(contains_in_order(
        evidence,
        {"epoch_protocol_mode != EpochProtocolMode::adaptive_v2",
         "make_root_qc_queue_blocked_event(",
         "audit_event_emitter->emit_audit("}));
    CHECK(evidence.find("final_qc->verify") == std::string::npos);
    CHECK(evidence.find("->has_n(") == std::string::npos);
    CHECK(evidence.find("config.nmajority") == std::string::npos);
}
