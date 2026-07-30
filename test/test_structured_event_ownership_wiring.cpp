#include <algorithm>
#include <array>
#include <cerrno>
#include <cctype>
#include <cstddef>
#include <fstream>
#include <initializer_list>
#include <optional>
#include <sstream>
#include <stdexcept>
#include <string>
#include <vector>

#include <sys/types.h>
#include <sys/wait.h>
#include <unistd.h>

#include "catch.hpp"

#ifndef KAURI_PROJECT_SOURCE_DIR
#error "KAURI_PROJECT_SOURCE_DIR must name the repository root"
#endif

#ifndef KAURI_HOTSTUFF_APP_PATH
#error "KAURI_HOTSTUFF_APP_PATH must name the hotstuff-app executable"
#endif

#ifndef KAURI_ADAPTATION_MANAGER_PATH
#error "KAURI_ADAPTATION_MANAGER_PATH must name adaptation-manager"
#endif

namespace
{

std::string source(const char *relative_path)
{
    std::ifstream input(
        std::string(KAURI_PROJECT_SOURCE_DIR) + "/" + relative_path);
    REQUIRE(input.good());
    std::ostringstream contents;
    contents << input.rdbuf();
    return contents.str();
}

std::string function_body(
    const std::string &contents,
    const std::string &signature)
{
    const auto declaration = contents.find(signature);
    if (declaration == std::string::npos)
        return {};
    const auto opening = contents.find('{', declaration + signature.size());
    if (opening == std::string::npos)
        return {};

    std::size_t depth = 0;
    for (std::size_t cursor = opening; cursor < contents.size(); ++cursor)
    {
        if (contents[cursor] == '{')
            ++depth;
        else if (contents[cursor] == '}' && --depth == 0)
            return contents.substr(opening, cursor - opening + 1);
    }
    return {};
}

std::size_t count_occurrences(
    const std::string &contents,
    const std::string &needle)
{
    std::size_t count = 0;
    for (std::size_t cursor = 0;
         (cursor = contents.find(needle, cursor)) != std::string::npos;
         cursor += needle.size())
        ++count;
    return count;
}

std::optional<std::string> option_binding(
    const std::string &contents,
    const std::string &option)
{
    const auto literal = "\"" + option + "\"";
    const auto option_position = contents.find(literal);
    if (option_position == std::string::npos)
        return std::nullopt;
    const auto add_option = contents.rfind("config.add_opt(", option_position);
    if (add_option == std::string::npos)
        return std::nullopt;
    auto cursor = contents.find(',', option_position + literal.size());
    if (cursor == std::string::npos)
        return std::nullopt;
    ++cursor;
    while (cursor < contents.size() &&
           std::isspace(static_cast<unsigned char>(contents[cursor])) != 0)
        ++cursor;
    const auto begin = cursor;
    while (cursor < contents.size())
    {
        const auto character = static_cast<unsigned char>(contents[cursor]);
        if (std::isalnum(character) == 0 && contents[cursor] != '_')
            break;
        ++cursor;
    }
    if (cursor == begin)
        return std::nullopt;
    return contents.substr(begin, cursor - begin);
}

std::string without_whitespace(const std::string &contents)
{
    std::string compact;
    compact.reserve(contents.size());
    for (const auto character : contents)
        if (std::isspace(static_cast<unsigned char>(character)) == 0)
            compact.push_back(character);
    return compact;
}

bool has_structured_event_timer(const std::string &contents)
{
    for (std::size_t cursor = 0;
         (cursor = contents.find("TimerEvent", cursor)) != std::string::npos;
         ++cursor)
    {
        const auto begin = cursor > 240 ? cursor - 240 : 0;
        const auto length = std::min<std::size_t>(
            contents.size() - begin, 720);
        const auto nearby = contents.substr(begin, length);
        if (nearby.find("structured_event") != std::string::npos ||
            nearby.find("structured-event") != std::string::npos)
            return true;
    }
    return false;
}

struct ProcessResult
{
    int status{0};
    std::string output;
};

ProcessResult run_program(
    const char *path,
    const std::vector<std::string> &arguments)
{
    int output_pipe[2];
    if (::pipe(output_pipe) != 0)
        throw std::runtime_error("failed to create subprocess pipe");

    const auto child = ::fork();
    if (child < 0)
    {
        ::close(output_pipe[0]);
        ::close(output_pipe[1]);
        throw std::runtime_error("failed to fork executable");
    }
    if (child == 0)
    {
        ::close(output_pipe[0]);
        if (::dup2(output_pipe[1], STDOUT_FILENO) < 0 ||
            ::dup2(output_pipe[1], STDERR_FILENO) < 0)
            _exit(126);
        ::close(output_pipe[1]);

        std::vector<std::string> owned_arguments;
        owned_arguments.reserve(arguments.size() + 1);
        owned_arguments.emplace_back(path);
        owned_arguments.insert(
            owned_arguments.end(), arguments.begin(), arguments.end());
        std::vector<char *> raw_arguments;
        raw_arguments.reserve(owned_arguments.size() + 1);
        for (auto &argument : owned_arguments)
            raw_arguments.push_back(&argument[0]);
        raw_arguments.push_back(nullptr);
        ::execv(path, raw_arguments.data());
        _exit(127);
    }

    ::close(output_pipe[1]);
    ProcessResult result;
    char buffer[4096];
    while (true)
    {
        const auto count = ::read(output_pipe[0], buffer, sizeof(buffer));
        if (count > 0)
        {
            result.output.append(buffer, static_cast<std::size_t>(count));
            continue;
        }
        if (count < 0 && errno == EINTR)
            continue;
        break;
    }
    ::close(output_pipe[0]);

    int wait_status = 0;
    while (::waitpid(child, &wait_status, 0) < 0)
        if (errno != EINTR)
            throw std::runtime_error("failed to wait for executable");
    result.status = WIFEXITED(wait_status)
                        ? WEXITSTATUS(wait_status)
                        : 128 + WTERMSIG(wait_status);
    return result;
}

std::vector<std::string> valid_adaptive_v2_arguments()
{
    return {
        "--epoch-protocol-mode", "adaptive_v2",
        "--epoch-change-issuer-id", "0",
        "--epoch-change-issuer-public-key",
        "022543a7f8dd080a3e44c4fac62194129ac260a3896ee9d546bfb08bbb379067c1",
        "--epoch-change-minimum-activation-delay", "2",
        "--epoch-change-maximum-activation-delay", "20",
        "--epoch-change-maximum-block-extra-bytes", "4096",
        "--epoch-change-maximum-ancestry-blocks", "128"};
}

} // namespace

TEST_CASE(
    "adaptive v2 event CLI is explicit while legacy modes stay compatible",
    "[we06-c10][structured-event][cli][integration][intentional-red]")
{
    const auto app = source("examples/hotstuff_app.cpp");
    const auto manager = source("examples/adaptation_manager.cpp");
    const std::array<const char *, 5> replica_options{{
        "structured-event-run-id",
        "structured-event-source-instance",
        "structured-event-output",
        "structured-event-commit-observer-id",
        "structured-event-commit-observer-instance"}};
    const std::array<const char *, 3> manager_options{{
        "structured-event-run-id",
        "structured-event-source-instance",
        "structured-event-output"}};

    const auto app_help = run_program(KAURI_HOTSTUFF_APP_PATH, {"--help"});
    REQUIRE(app_help.status == 0);
    for (const auto *option : replica_options)
    {
        CAPTURE(option);
        CHECK(option_binding(app, option).has_value());
        CHECK(app_help.output.find(option) != std::string::npos);
    }

    const auto manager_help = run_program(
        KAURI_ADAPTATION_MANAGER_PATH, {"--help"});
    REQUIRE(manager_help.status == 0);
    for (const auto *option : manager_options)
    {
        CAPTURE(option);
        CHECK(option_binding(manager, option).has_value());
        CHECK(manager_help.output.find(option) != std::string::npos);
    }

    const auto rejected = run_program(
        KAURI_HOTSTUFF_APP_PATH, valid_adaptive_v2_arguments());
    CHECK(rejected.status != 0);
    CHECK(rejected.output.find("structured-event") != std::string::npos);
    CHECK(rejected.output.find("replica idx out of range") ==
          std::string::npos);

    for (const auto *mode : {"legacy_static", "adaptive_v1"})
    {
        const auto compatible = run_program(
            KAURI_HOTSTUFF_APP_PATH,
            {"--epoch-protocol-mode", mode});
        CAPTURE(mode);
        CHECK(compatible.status != 0);
        CHECK(compatible.output.find("replica idx out of range") !=
              std::string::npos);
        CHECK(compatible.output.find("structured-event") ==
              std::string::npos);
    }

    CHECK(app.find("StructuredEventSourceKind::replica") !=
          std::string::npos);
    CHECK(app.find("designated_commit_observer") != std::string::npos);
    CHECK(manager.find(
              "StructuredEventSourceKind::adaptation_manager") !=
          std::string::npos);
}

TEST_CASE(
    "replica owns one sink across startup dispatch and durable shutdown",
    "[we06-c10][structured-event][replica][ownership][intentional-red]")
{
    const auto app = source("examples/hotstuff_app.cpp");
    const auto main = function_body(app, "int main(");
    const auto constructor = function_body(
        app, "HotStuffApp::HotStuffApp(");
    const auto start = function_body(app, "void HotStuffApp::start(");
    const auto stop = function_body(app, "void HotStuffApp::stop(");
    REQUIRE_FALSE(main.empty());
    REQUIRE_FALSE(constructor.empty());
    REQUIRE_FALSE(start.empty());
    REQUIRE_FALSE(stop.empty());

    SECTION("owned adapters outlive the synchronous event loop")
    {
        const auto clock = main.find("MonotonicRawStructuredEventClock");
        const auto output = main.find("ExclusiveFileStructuredEventOutput");
        const auto sink = main.find("StructuredEventSink");
        const auto bind = main.find("papp->bind_structured_event_emitters(");
        const auto run = main.find("papp->start(reps)");
        const auto unbind = bind == std::string::npos
                                ? std::string::npos
                                : main.find(
                                      "papp->bind_structured_event_emitters(",
                                      bind + 1);
        const auto shutdown = unbind == std::string::npos
                                  ? std::string::npos
                                  : main.find(".shutdown()", unbind);
        const auto health = shutdown == std::string::npos
                                ? std::string::npos
                                : main.find(".health()", shutdown);

        REQUIRE(clock != std::string::npos);
        REQUIRE(output != std::string::npos);
        REQUIRE(sink != std::string::npos);
        REQUIRE(bind != std::string::npos);
        REQUIRE(run != std::string::npos);
        REQUIRE(unbind != std::string::npos);
        REQUIRE(shutdown != std::string::npos);
        REQUIRE(health != std::string::npos);
        CHECK(clock < output);
        CHECK(output < sink);
        CHECK(sink < bind);
        CHECK(bind < run);
        CHECK(run < unbind);
        CHECK(unbind < shutdown);
        CHECK(shutdown < health);

        const auto destroy = main.find(
            "papp = salticidae::BoxObj<HotStuffApp>()", unbind);
        const auto stopped = main.find(
            "ProcessLifecycleState::stopped", unbind);
        REQUIRE(destroy != std::string::npos);
        REQUIRE(stopped != std::string::npos);
        CHECK(unbind < destroy);
        CHECK(destroy < stopped);
        CHECK(stopped < shutdown);

        const auto bind_end = main.find(';', bind);
        REQUIRE(bind_end != std::string::npos);
        const auto bind_call = main.substr(bind, bind_end - bind);
        CHECK(bind_call.find("nullptr") == std::string::npos);
        CHECK(count_occurrences(bind_call, ",") >= 2);

        const auto unbind_end = main.find(';', unbind);
        REQUIRE(unbind_end != std::string::npos);
        const auto unbind_call = without_whitespace(
            main.substr(unbind, unbind_end - unbind));
        CHECK(unbind_call.find("nullptr,nullptr,nullptr") !=
              std::string::npos);
        const bool rejects_unhealthy_sink =
            main.find("return 1", health) != std::string::npos ||
            main.find("throw HotStuffError", health) !=
                std::string::npos;
        CHECK(rejects_unhealthy_sink);
    }

    SECTION("lifecycle records sit on real process boundaries")
    {
        CHECK(constructor.find("cn.start()") == std::string::npos);
        CHECK(constructor.find("cn.listen(") == std::string::npos);

        const auto start_network = start.find("HotStuff::start(reps)");
        const auto client_start = start.find("cn.start()");
        const auto client_listen = start.find("cn.listen(");
        const auto ready = start.find("ProcessLifecycleState::ready");
        const auto dispatch = start.find("ec.dispatch()");
        REQUIRE(start_network != std::string::npos);
        REQUIRE(client_start != std::string::npos);
        REQUIRE(client_listen != std::string::npos);
        REQUIRE(ready != std::string::npos);
        REQUIRE(dispatch != std::string::npos);
        CHECK(start_network < ready);
        CHECK(start_network < client_start);
        CHECK(client_start < client_listen);
        CHECK(client_listen < ready);
        CHECK(ready < dispatch);

        const auto stopping = stop.find("ProcessLifecycleState::stopping");
        const auto event_stop = stop.find("ec.stop()");
        REQUIRE(stopping != std::string::npos);
        REQUIRE(event_stop != std::string::npos);
        CHECK(stopping < event_stop);

        const auto run = main.find("papp->start(reps)");
        const auto bind = main.find(
            "papp->bind_structured_event_emitters(");
        const auto started_main = main.find("ProcessLifecycleState::started");
        const auto stopped_main = main.find("ProcessLifecycleState::stopped");
        const auto started_live = start.find("ProcessLifecycleState::started");
        const auto stopped_live = start.find("ProcessLifecycleState::stopped");
        const bool started_before_live_start =
            (started_main != std::string::npos && started_main < run) ||
            (started_live != std::string::npos &&
             started_live < start_network);
        const bool stopped_after_dispatch =
            (stopped_main != std::string::npos && stopped_main > run) ||
            (stopped_live != std::string::npos &&
             stopped_live > dispatch);
        CHECK(started_before_live_start);
        CHECK(stopped_after_dispatch);
        REQUIRE(bind != std::string::npos);
        REQUIRE(started_main != std::string::npos);
        CHECK(bind < started_main);
        CHECK(started_main < run);
    }

    SECTION("one same-context timer drains and rejects sink failure")
    {
        CHECK(has_structured_event_timer(app));
        const auto drain = app.find(".drain()");
        const auto health = drain == std::string::npos
                                ? std::string::npos
                                : app.find(".health()", drain);
        REQUIRE(drain != std::string::npos);
        REQUIRE(health != std::string::npos);
        CHECK(drain < health);
        const auto begin = drain > 1200 ? drain - 1200 : 0;
        const auto nearby = app.substr(
            begin,
            std::min<std::size_t>(app.size() - begin, 2600));
        CHECK(nearby.find("TimerEvent") != std::string::npos);
        CHECK(nearby.find("ec") != std::string::npos);
        CHECK(nearby.find("stop()") != std::string::npos);
        CHECK(nearby.find(".add(") != std::string::npos);
    }
}

TEST_CASE(
    "manager owns its sink across dispatch and emits each score update once",
    "[we06-c10][structured-event][manager][ownership][intentional-red]")
{
    const auto manager = source("examples/adaptation_manager.cpp");
    const auto main = function_body(manager, "int main(");
    const auto run = function_body(manager, "int run()");
    const auto stop_runtime = function_body(
        manager, "void stop_runtime()");
    const auto evaluate = function_body(manager, "void evaluate()");
    REQUIRE_FALSE(main.empty());
    REQUIRE_FALSE(run.empty());
    REQUIRE_FALSE(stop_runtime.empty());
    REQUIRE_FALSE(evaluate.empty());

    SECTION("borrowed clock and output outlive manager callbacks")
    {
        const auto clock = main.find("MonotonicRawStructuredEventClock");
        const auto output = main.find("ExclusiveFileStructuredEventOutput");
        const auto sink = main.find("StructuredEventSink");
        const auto manager_owner = main.find(
            "std::make_unique<AdaptationManager>(");
        const auto manager_run = main.find("manager->run()");
        const auto manager_destroy = manager_run == std::string::npos
                                         ? std::string::npos
                                         : main.find(
                                               "manager.reset()",
                                               manager_run);
        const auto shutdown = manager_run == std::string::npos
                                  ? std::string::npos
                                  : main.find(".shutdown()", manager_run);
        const auto health = shutdown == std::string::npos
                                ? std::string::npos
                                : main.find(".health()", shutdown);
        REQUIRE(clock != std::string::npos);
        REQUIRE(output != std::string::npos);
        REQUIRE(sink != std::string::npos);
        REQUIRE(manager_owner != std::string::npos);
        REQUIRE(manager_run != std::string::npos);
        REQUIRE(manager_destroy != std::string::npos);
        REQUIRE(shutdown != std::string::npos);
        REQUIRE(health != std::string::npos);
        CHECK(clock < output);
        CHECK(output < sink);
        CHECK(sink < manager_owner);
        CHECK(manager_owner < manager_run);
        CHECK(manager_run < manager_destroy);
        CHECK(manager_destroy < shutdown);
        CHECK(shutdown < health);
        const bool rejects_unhealthy_sink =
            main.find("return 1", health) != std::string::npos ||
            main.find("return 2", health) != std::string::npos;
        CHECK(rejects_unhealthy_sink);
    }

    SECTION("network and evidence lifecycle have durable boundaries")
    {
        const auto started = run.find("ProcessLifecycleState::started");
        const auto network_start = run.find("network_.start()");
        const auto listen = run.find("network_.listen(");
        const auto ready = run.find("ProcessLifecycleState::ready");
        const auto dispatch = run.find("event_context_.dispatch()");
        const auto runtime_stop = run.find("stop_runtime()", dispatch);
        const auto stopped = run.find(
            "ProcessLifecycleState::stopped", runtime_stop);
        REQUIRE(started != std::string::npos);
        REQUIRE(network_start != std::string::npos);
        REQUIRE(listen != std::string::npos);
        REQUIRE(ready != std::string::npos);
        REQUIRE(dispatch != std::string::npos);
        REQUIRE(runtime_stop != std::string::npos);
        REQUIRE(stopped != std::string::npos);
        CHECK(started < network_start);
        CHECK(listen < ready);
        CHECK(ready < dispatch);
        CHECK(dispatch < runtime_stop);
        CHECK(runtime_stop < stopped);

        const auto stopping = run.find("ProcessLifecycleState::stopping");
        REQUIRE(stopping != std::string::npos);
        const auto stop_after_stopping = run.find(
            "event_context_.stop()", stopping);
        REQUIRE(stop_after_stopping != std::string::npos);
        CHECK(stopping < stop_after_stopping);

        CHECK(has_structured_event_timer(manager));
        const auto drain = run.find(".drain()");
        const auto health = drain == std::string::npos
                                ? std::string::npos
                                : run.find(".health()", drain);
        REQUIRE(drain != std::string::npos);
        REQUIRE(health != std::string::npos);
        CHECK(drain < health);
        CHECK(run.find("event_context_", drain) != std::string::npos);
        const bool failure_stops_manager =
            run.find("fail(", health) != std::string::npos ||
            run.find("event_context_.stop()", health) !=
                std::string::npos;
        CHECK(failure_stops_manager);

        const auto stop_context = stop_runtime.find(
            "event_context_.stop()");
        const auto network_stop = stop_runtime.find("network_.stop()");
        const auto session_shutdown = stop_runtime.find(
            "session_.shutdown()");
        REQUIRE(stop_context != std::string::npos);
        REQUIRE(network_stop != std::string::npos);
        REQUIRE(session_shutdown != std::string::npos);
        CHECK(stop_context < network_stop);
        CHECK(network_stop < session_shutdown);

        const auto stop_required = run.find(
            "network_stop_required_ = true");
        REQUIRE(stop_required != std::string::npos);
        CHECK(stop_required < network_start);

        const auto catch_all = run.find("catch (...)");
        REQUIRE(catch_all != std::string::npos);
        const auto catch_stop_context = run.find(
            "event_context_.stop()", catch_all);
        const auto catch_runtime_stop = run.find(
            "stop_runtime()", catch_all);
        REQUIRE(catch_stop_context != std::string::npos);
        REQUIRE(catch_runtime_stop != std::string::npos);
        CHECK(catch_stop_context < catch_runtime_stop);

        const auto main_shutdown = main.find(".shutdown()");
        REQUIRE(main_shutdown != std::string::npos);
        CHECK(main.find("manager->run()") < main_shutdown);
        CHECK(main.find("manager.reset()") < main_shutdown);
    }

    SECTION("score trajectory uses one monotonic emission cursor")
    {
        const auto emit_trajectory = function_body(
            manager, "void emit_new_score_trajectory() noexcept");
        REQUIRE_FALSE(emit_trajectory.empty());
        CHECK(count_occurrences(
                  manager, "audit->score_trajectory") == 1);
        CHECK(count_occurrences(
                  emit_trajectory, "session_.controller_audit()") == 1);
        CHECK(count_occurrences(
                  emit_trajectory, "audit->score_trajectory") == 1);
        const auto trajectory = emit_trajectory.find(
            "audit->score_trajectory");
        const auto reputation = emit_trajectory.find(
            "ReputationEvidenceAppliedStructuredEvent", trajectory);
        const auto audit = emit_trajectory.find("emit_audit(", trajectory);
        const auto cursor = emit_trajectory.find(
            "emitted_score_trajectory_");
        REQUIRE(trajectory != std::string::npos);
        REQUIRE(reputation != std::string::npos);
        REQUIRE(audit != std::string::npos);
        REQUIRE(cursor != std::string::npos);
        CHECK(trajectory < reputation);
        CHECK(reputation < audit);
        CHECK(count_occurrences(
                  emit_trajectory, "emitted_score_trajectory_") >= 3);

        const auto trajectory_window = emit_trajectory.substr(
            trajectory,
            std::min<std::size_t>(
                emit_trajectory.size() - trajectory, 2400));
        CHECK(trajectory_window.find("trajectory.size()") !=
              std::string::npos);
        CHECK(trajectory_window.find(
                  "trajectory[emitted_score_trajectory_]") !=
              std::string::npos);
        CHECK(trajectory_window.find("audit->current_cutoff") !=
              std::string::npos);
        const bool increments_cursor =
            trajectory_window.find("++emitted_score_trajectory_") !=
                std::string::npos ||
            trajectory_window.find("emitted_score_trajectory_++") !=
                std::string::npos;
        CHECK(increments_cursor);

        const auto controller_evaluate = evaluate.find(
            "session_.evaluate()");
        const auto emit_new = evaluate.find("emit_new_score_trajectory(");
        const auto emit_short = evaluate.find("emit_score_trajectory(");
        const auto direct = evaluate.find("audit->score_trajectory");
        CHECK(count_occurrences(
                  evaluate, "emit_new_score_trajectory();") == 1);
        const auto emission = std::min(
            direct,
            std::min(emit_new, emit_short));
        REQUIRE(controller_evaluate != std::string::npos);
        REQUIRE(emission != std::string::npos);
        CHECK(controller_evaluate < emission);
    }

    SECTION(
        "accepted ledger cursor emits once per window and restarts after rotation")
    {
        const auto emit_observations = function_body(
            manager,
            "void emit_new_accepted_observations() noexcept");
        const auto ingest = function_body(
            manager, "void ingest(");
        const auto begin_cycle = function_body(
            manager, "bool begin_current_cycle() noexcept");
        const auto rotate = function_body(
            manager, "void begin_convergence_ack_drain() noexcept");
        REQUIRE_FALSE(emit_observations.empty());
        REQUIRE_FALSE(ingest.empty());
        REQUIRE_FALSE(begin_cycle.empty());
        REQUIRE_FALSE(rotate.empty());

        const auto accepted = emit_observations.find(
            "session_.ingress().ledger().accepted()");
        const auto cursor = emit_observations.find(
            "emitted_accepted_observations_");
        const auto suffix_loop = emit_observations.find(
            "while (emitted_accepted_observations_ < records.size())");
        const auto event = emit_observations.find(
            "EvidenceObservationAcceptedStructuredEvent");
        const auto emit = emit_observations.find(
            "emit_audit(", event);
        const auto increment = emit_observations.find(
            "++emitted_accepted_observations_", emit);
        REQUIRE(accepted != std::string::npos);
        REQUIRE(cursor != std::string::npos);
        REQUIRE(suffix_loop != std::string::npos);
        REQUIRE(event != std::string::npos);
        REQUIRE(emit != std::string::npos);
        REQUIRE(increment != std::string::npos);
        CHECK(accepted < suffix_loop);
        CHECK(suffix_loop < event);
        CHECK(event < emit);
        CHECK(emit < increment);
        CHECK(count_occurrences(
                  emit_observations, "emit_audit(") == 1);
        CHECK(count_occurrences(
                  emit_observations,
                  "++emitted_accepted_observations_") == 1);
        CHECK(emit_observations.find(
                  "record}}", event) != std::string::npos);
        CHECK(emit_observations.find(
                  "records.size()", cursor) !=
              std::string::npos);

        const auto operation = ingest.find("operation(");
        const auto audit_suffix = ingest.find(
            "emit_new_accepted_observations()", operation);
        const auto status_check = ingest.find(
            "result.status", operation);
        const auto controller_evaluate = ingest.find(
            "evaluate()", audit_suffix);
        REQUIRE(operation != std::string::npos);
        REQUIRE(audit_suffix != std::string::npos);
        REQUIRE(status_check != std::string::npos);
        REQUIRE(controller_evaluate != std::string::npos);
        CHECK(operation < audit_suffix);
        CHECK(audit_suffix < status_check);
        CHECK(audit_suffix < controller_evaluate);

        CHECK(manager.find("ingest(\"lifecycle\"") !=
              std::string::npos);
        CHECK(begin_cycle.find(
                  "emitted_accepted_observations_ = 0") ==
              std::string::npos);
        CHECK(count_occurrences(
                  manager,
                  "emitted_accepted_observations_ = 0;") == 1);
        const auto publish = rotate.find(
            "session_.consume_ready_and_rotate()");
        const auto failed_rotation_return = rotate.find(
            "return;", publish);
        const auto reset = rotate.find(
            "emitted_accepted_observations_ = 0", publish);
        const auto continue_after_reset = rotate.find(
            "emit_new_session_terminals()", reset);
        REQUIRE(publish != std::string::npos);
        REQUIRE(failed_rotation_return != std::string::npos);
        REQUIRE(reset != std::string::npos);
        REQUIRE(continue_after_reset != std::string::npos);
        CHECK(publish < failed_rotation_return);
        CHECK(failed_rotation_return < reset);
        CHECK(reset < continue_after_reset);
    }
}
