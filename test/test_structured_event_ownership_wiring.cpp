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
    const std::array<const char *, 9> manager_options{{
        "structured-event-run-id",
        "structured-event-source-instance",
        "structured-event-output",
        "fault-containment-evidence-start-monotonic-ns",
        "fault-containment-required-tree-coverage",
        "fault-window-arm-timeout-evidence-basis",
        "fault-window-arm-required-observation-schema",
        "fault-window-arm-clock-domain",
        "fault-window-arm-snapshot-evidence-basis"}};

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

    const auto adaptive_v3_help = run_program(
        KAURI_ADAPTATION_MANAGER_PATH,
        {"--protocol-mode", "adaptive_v3", "--help"});
    REQUIRE(adaptive_v3_help.status == 0);
    for (const auto *option : {
             "protocol-mode",
             "activation-readiness-member",
             "activation-readiness-release-count",
             "activation-readiness-maximum-delivery-attempts",
             "activation-readiness-retry-interval-ticks"})
    {
        CAPTURE(option);
        CHECK(adaptive_v3_help.output.find(option) != std::string::npos);
    }
    CHECK(adaptive_v3_help.output.find("required-nonresponsive") !=
          std::string::npos);
    CHECK(manager_help.output.find("activation-readiness-member") !=
          std::string::npos);

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
    "adaptive-v3 manager owns authenticated delivery through durable stop",
    "[cert13][m1][adaptive-v3][manager][ownership][transport][wiring]")
{
    const auto manager = source("examples/adaptation_manager.cpp");
    const auto v3_begin = manager.find(
        "class AdaptiveV3ManagerModeState final");
    const auto v2_begin = manager.find(
        "class AdaptiveV2ManagerModeState final", v3_begin);
    REQUIRE(v3_begin != std::string::npos);
    REQUIRE(v2_begin != std::string::npos);
    const auto v3 = manager.substr(v3_begin, v2_begin - v3_begin);
    const auto run = function_body(v3, "int run_adaptive_v3()");
    const auto stop_runtime = function_body(v3, "void stop_runtime()");
    const auto transport_begin = manager.find(
        "class AdaptiveV3ManagerTransport final");
    REQUIRE(transport_begin != std::string::npos);
    const auto transport = manager.substr(transport_begin, v3_begin - transport_begin);
    const auto handlers = function_body(transport, "void register_handlers()");
    const auto transport_start = function_body(transport, "void start()");
    const auto transport_stop = function_body(transport, "bool stop() noexcept");
    const auto observation = function_body(v3, "void handle_observation(");
    const auto acknowledgement = function_body(v3, "void handle_ack(");
    const auto deliveries = function_body(v3, "void drive_deliveries()");
    const auto assembled = function_body(
        v3, "void emit_certificate_assembled() noexcept");
    const auto begin_next = function_body(
        v3, "void begin_next_transition_cycle() noexcept");
    const auto e2_audit = function_body(
        v3, "void emit_e2_eligibility(");
    const auto wire_rejected = function_body(
        v3, "void emit_wire_rejected(");
    const auto evaluate = function_body(
        v3, "void evaluate_transition_cycle()");
    const auto shape_audit = function_body(
        v3, "void emit_v3_shape_decision(");
    const auto evidence_audit = function_body(
        v3, "void emit_v3_evidence_snapshot(");
    const auto accepted_evidence = function_body(
        v3, "void emit_new_v3_accepted_observations()");
    const auto common_ingest = function_body(v3, "void ingest_common(");
    REQUIRE_FALSE(run.empty());
    REQUIRE_FALSE(stop_runtime.empty());
    REQUIRE_FALSE(handlers.empty());
    REQUIRE_FALSE(observation.empty());
    REQUIRE_FALSE(acknowledgement.empty());
    REQUIRE_FALSE(deliveries.empty());
    REQUIRE_FALSE(assembled.empty());
    REQUIRE_FALSE(begin_next.empty());
    REQUIRE_FALSE(e2_audit.empty());
    REQUIRE_FALSE(wire_rejected.empty());
    REQUIRE_FALSE(evaluate.empty());
    REQUIRE_FALSE(shape_audit.empty());
    REQUIRE_FALSE(evidence_audit.empty());
    REQUIRE_FALSE(accepted_evidence.empty());
    REQUIRE_FALSE(common_ingest.empty());

    const auto dispatch = run.find("event_context_.dispatch()");
    const auto runtime_stop = run.find("stop_runtime()", dispatch);
    const auto stopped = run.find(
        "ProcessLifecycleState::stopped", runtime_stop);
    REQUIRE(dispatch != std::string::npos);
    REQUIRE(runtime_stop != std::string::npos);
    REQUIRE(stopped != std::string::npos);
    CHECK(dispatch < runtime_stop);
    CHECK(runtime_stop < stopped);
    CHECK(stop_runtime.find("retry_timer_.del()") != std::string::npos);
    CHECK(stop_runtime.find("transport_.stop()") != std::string::npos);
    CHECK(transport_stop.find("callbacks_ = {}") != std::string::npos);
    CHECK(transport_stop.find("network_.stop()") != std::string::npos);
    CHECK(transport_stop.find("network_.terminate()") == std::string::npos);

    const auto compact_handlers = without_whitespace(handlers);
    CHECK(compact_handlers.find(
              "static_pointer_cast<ManagerNetwork::conn_t::type>") !=
          std::string::npos);
    CHECK(compact_handlers.find("authenticated_source(peer_connection)") !=
          std::string::npos);
    const auto observation_auth = handlers.find(
        "authenticated_source(connection)");
    const auto observation_owner = observation.find(
        "facade_.v3_observe_readiness(");
    REQUIRE(observation_auth != std::string::npos);
    REQUIRE(observation_owner != std::string::npos);
    CHECK(handlers.find("callbacks_.observation") != std::string::npos);
    CHECK(without_whitespace(acknowledgement).find(
              "facade_.v3_acknowledge(source,manager_tick_ns(),payload)") !=
          std::string::npos);
    CHECK(deliveries.find("transport_.send_certificate") !=
          std::string::npos);
    CHECK(deliveries.find("AdaptiveV3ManagerSessionStatus::terminal") !=
          std::string::npos);
    CHECK(deliveries.find("emit_new_session_terminals()") !=
          std::string::npos);

    SECTION("authenticated malformed readiness wire is sealed before return")
    {
        const auto observation_decode = observation.find(
            "decode_activation_ready_observation(");
        const auto observation_audit = observation.find(
            "emit_wire_rejected(", observation_decode);
        const auto observation_owner = observation.find(
            "facade_.v3_observe_readiness(", observation_audit);
        const auto ack_decode = acknowledgement.find(
            "decode_activation_readiness_ack(");
        const auto ack_audit = acknowledgement.find(
            "emit_wire_rejected(", ack_decode);
        const auto ack_owner = acknowledgement.find(
            "facade_.v3_acknowledge(", ack_audit);
        REQUIRE(observation_decode != std::string::npos);
        REQUIRE(observation_audit != std::string::npos);
        REQUIRE(observation_owner != std::string::npos);
        REQUIRE(ack_decode != std::string::npos);
        REQUIRE(ack_audit != std::string::npos);
        REQUIRE(ack_owner != std::string::npos);
        CHECK(observation_decode < observation_audit);
        CHECK(observation_audit < observation_owner);
        CHECK(ack_decode < ack_audit);
        CHECK(ack_audit < ack_owner);
        CHECK(wire_rejected.find("wire_rejected") != std::string::npos);
        CHECK(wire_rejected.find("payload_digest") != std::string::npos);
        CHECK(wire_rejected.find("canonical_wire_payload") != std::string::npos);
        const auto wire_emit = wire_rejected.find("emit_readiness");
        const auto wire_drain = wire_rejected.find("event_sink_.drain()", wire_emit);
        const auto wire_health = wire_rejected.find("event_sink_.health()", wire_drain);
        REQUIRE(wire_emit != std::string::npos);
        REQUIRE(wire_drain != std::string::npos);
        REQUIRE(wire_health != std::string::npos);
        CHECK(wire_emit < wire_drain);
        CHECK(wire_drain < wire_health);
        CHECK(wire_rejected.find("fail()") != std::string::npos);
    }

    SECTION("assembled certificate audits bind the exact delivery wire bytes")
    {
        const auto wire = assembled.find(
            "encode_activation_readiness_certificate(");
        const auto payload = assembled.find("canonical_wire_payload", wire);
        const auto digest = assembled.find(
            "activation_readiness_ack_payload_digest(", payload);
        const auto opcode = assembled.find(
            "MsgActivationReadinessCertificate::opcode", digest);
        const auto emit = assembled.find("emit_readiness", opcode);
        REQUIRE(wire != std::string::npos);
        REQUIRE(payload != std::string::npos);
        REQUIRE(digest != std::string::npos);
        REQUIRE(opcode != std::string::npos);
        REQUIRE(emit != std::string::npos);
        CHECK(wire < payload);
        CHECK(payload < digest);
        CHECK(digest < emit);
        CHECK(assembled.find("payload_digest =\n            certificate->certificate_digest") == std::string::npos);
    }

    SECTION("E2 eligibility is sealed after its atomic session transition")
    {
        const auto atomic_begin = begin_next.find("v3_begin_e2_at(");
        const auto audit = begin_next.find("emit_e2_eligibility(", atomic_begin);
        const auto drain = begin_next.find("event_sink_.drain()", audit);
        const auto evaluate = begin_next.find("evaluate_transition_cycle()", drain);
        REQUIRE(atomic_begin != std::string::npos);
        REQUIRE(audit != std::string::npos);
        REQUIRE(drain != std::string::npos);
        REQUIRE(evaluate != std::string::npos);
        CHECK(atomic_begin < audit);
        CHECK(audit < drain);
        CHECK(drain < evaluate);
        CHECK(e2_audit.find("e1_bundle_digest") != std::string::npos);
        CHECK(e2_audit.find("e2_common_commit") != std::string::npos);
        CHECK(e2_audit.find("e2_actual_begin_raw_ns") != std::string::npos);
    }

    SECTION("selected v3 evidence is durable before bundle publication")
    {
        const auto bundle_write = evaluate.find("write_exclusive_bundle(");
        const auto shape = evaluate.find(
            "emit_v3_shape_decision(", bundle_write);
        const auto evidence = evaluate.find(
            "emit_v3_evidence_snapshot(", shape);
        const auto readiness = evaluate.find(
            "facade_.v3_begin_readiness(", evidence);
        const auto send = evaluate.find("transport_.send_bundle(", readiness);
        REQUIRE(bundle_write != std::string::npos);
        REQUIRE(shape != std::string::npos);
        REQUIRE(evidence != std::string::npos);
        REQUIRE(readiness != std::string::npos);
        REQUIRE(send != std::string::npos);
        CHECK(bundle_write < shape);
        CHECK(shape < evidence);
        CHECK(evidence < readiness);
        CHECK(readiness < send);
        CHECK(shape_audit.find("facade_.controller_audit()") !=
              std::string::npos);
        CHECK(evidence_audit.find("facade_.controller_audit()") !=
              std::string::npos);
        CHECK(evidence_audit.find(
                  "serialize_adaptive_v2_evidence_snapshot_payload(") !=
              std::string::npos);
        const auto emit = evidence_audit.find("event_sink_.emit_audit(");
        const auto drain = evidence_audit.find("event_sink_.drain()", emit);
        const auto artifact = evidence_audit.find(
            "write_exclusive_json(", drain);
        REQUIRE(emit != std::string::npos);
        REQUIRE(drain != std::string::npos);
        REQUIRE(artifact != std::string::npos);
        CHECK(emit < drain);
        CHECK(drain < artifact);
    }

    SECTION("v3 accepted evidence is durable before controller evaluation")
    {
        const auto ingest = common_ingest.find("operation(source, message)");
        const auto accepted = common_ingest.find(
            "emit_new_v3_accepted_observations()", ingest);
        const auto evaluate = common_ingest.find(
            "evaluate_transition_cycle()", accepted);
        REQUIRE(ingest != std::string::npos);
        REQUIRE(accepted != std::string::npos);
        REQUIRE(evaluate != std::string::npos);
        CHECK(ingest < accepted);
        CHECK(accepted < evaluate);
        CHECK(accepted_evidence.find(
                  "EvidenceObservationAcceptedStructuredEvent") !=
              std::string::npos);
        const auto emit = accepted_evidence.find("event_sink_.emit_audit(");
        const auto drain = accepted_evidence.find(
            "event_sink_.drain()", emit);
        REQUIRE(emit != std::string::npos);
        REQUIRE(drain != std::string::npos);
        CHECK(emit < drain);
    }

    CHECK(v3.find("fault_receipt") == std::string::npos);
    CHECK(v3.find("crash") == std::string::npos);
    CHECK(v3.find("process_status") == std::string::npos);
    CHECK(v3.find("quorum") == std::string::npos);

    const auto main = function_body(manager, "int main(");
    const auto owner = main.find("std::make_unique<AdaptationManager>(");
    const auto execute = main.find(
        "manager->run()", owner);
    const auto destroy = main.find("manager.reset()", execute);
    const auto shutdown = main.find("event_sink.shutdown()", destroy);
    REQUIRE(owner != std::string::npos);
    REQUIRE(execute != std::string::npos);
    REQUIRE(destroy != std::string::npos);
    REQUIRE(shutdown != std::string::npos);
    CHECK(owner < execute);
    CHECK(execute < destroy);
    CHECK(destroy < shutdown);
}

TEST_CASE(
    "CERT13 unified v3 manager derives two certified cycles without a preseeded identity",
    "[cert13][v3][manager][lifecycle][n7][n31][intentional-red]")
{
    /*
     * This is deliberately an executable-owner contract, rather than a
     * collector unit test.  The collector can already assemble one fixed
     * identity certificate; the manager must instead derive each identity
     * from the authoritative committed transition, keep running through the
     * first ACK quorum, and arm the next transition in the same process.
     *
     * The assertions are intentionally RED until that lifecycle replaces the
     * one-shot --activation-readiness-identity bootstrap.  They keep the
     * N=7 (Q=5/R=5) and N=31 (Q=21/R=28) policy outside the fixed-quorum
     * verifier: the manager's release threshold is operational only.
     */
    const auto manager = source("examples/adaptation_manager.cpp");
    const auto parse = function_body(
        manager, "AdaptiveV3ManagerOptions parse_adaptive_v3_options(");
    const auto v3_begin = manager.find(
        "class AdaptiveV3ManagerModeState final");
    const auto v2_begin = manager.find("class AdaptiveV2ManagerModeState final", v3_begin);
    REQUIRE_FALSE(parse.empty());
    REQUIRE(v3_begin != std::string::npos);
    REQUIRE(v2_begin != std::string::npos);
    const auto v3 = manager.substr(v3_begin, v2_begin - v3_begin);

    SECTION("the v3 CLI carries public transition authority, not a forged readiness identity")
    {
        const auto manager_help = run_program(
            KAURI_ADAPTATION_MANAGER_PATH,
            {"--protocol-mode", "adaptive_v3", "--help"});
        REQUIRE(manager_help.status == 0);
        CHECK(parse.find("activation-readiness-identity") ==
              std::string::npos);
        CHECK(manager_help.output.find("activation-readiness-identity") ==
              std::string::npos);
        CHECK(manager_help.output.find("issuer-id") !=
              std::string::npos);
        CHECK(manager_help.output.find("transition-request") !=
              std::string::npos);
        CHECK(manager_help.output.find("selection") != std::string::npos);
    }

    SECTION("E0 to E1 keeps Q verification distinct from R release")
    {
        CHECK(v3.find("AdaptiveV3ManagerSession") !=
              std::string::npos);
    CHECK(manager.find("manager_controller_config") != std::string::npos);
        CHECK(v3.find("required_release_count") != std::string::npos);
        CHECK(v3.find("certificate_assembled") != std::string::npos);
        CHECK(v3.find("quarantine") != std::string::npos);
        CHECK(v3.find("ingest_common") != std::string::npos);
    }

    SECTION("ACK quorum rotates the first cycle but does not terminate the process")
    {
        CHECK(v3.find("begin_next_transition_cycle") !=
              std::string::npos);
        CHECK(manager.find("residency_ticks") !=
              std::string::npos);
        CHECK(manager.find("65'000") != std::string::npos);
        CHECK(v3.find("begin_next_transition_cycle") !=
              std::string::npos);
        CHECK(v3.find("AdaptiveV3ManagerSessionStatus::terminal") != std::string::npos);
        CHECK(v3.find("emitted_session_terminals_") != std::string::npos);
    }

    SECTION("both certificate and delivery deadlines are bounded before E2 terminality")
    {
        CHECK(manager.find("pre_certificate_window_ticks") !=
              std::string::npos);
        CHECK(manager.find("delivery_window_ticks") != std::string::npos);
        CHECK(v3.find("kAdaptiveV3TickSeconds") != std::string::npos);
    }
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
    const auto legacy_manager_begin = manager.find(
        "class AdaptationManager final");
    REQUIRE(legacy_manager_begin != std::string::npos);
    const auto v2_begin = manager.find("class AdaptiveV2ManagerModeState final");
    REQUIRE(v2_begin != std::string::npos);
    const auto v2 = manager.substr(v2_begin, legacy_manager_begin - v2_begin);
    const auto run = function_body(v2, "int run()");
    const auto stop_runtime = function_body(v2, "void stop_runtime()");
    const auto evaluate = function_body(v2, "void evaluate()");
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

        const auto legacy_manager_run = main.find("manager->run()");
        const auto main_shutdown = legacy_manager_run == std::string::npos
            ? std::string::npos
            : main.find(".shutdown()", legacy_manager_run);
        REQUIRE(legacy_manager_run != std::string::npos);
        REQUIRE(main_shutdown != std::string::npos);
        CHECK(legacy_manager_run < main_shutdown);
        CHECK(main.find("manager.reset()", legacy_manager_run) <
              main_shutdown);
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
        const auto controller_schedule = ingest.find(
            "schedule_evaluation()", audit_suffix);
        REQUIRE(operation != std::string::npos);
        REQUIRE(audit_suffix != std::string::npos);
        REQUIRE(status_check != std::string::npos);
        REQUIRE(controller_schedule != std::string::npos);
        CHECK(operation < audit_suffix);
        CHECK(audit_suffix < status_check);
        CHECK(audit_suffix < controller_schedule);
        CHECK(ingest.find("evaluate()") == std::string::npos);

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

    SECTION(
        "containment coverage gates selection and emits one ready event")
    {
        const auto gate = function_body(
            manager, "bool fault_containment_coverage_ready(");
        REQUIRE_FALSE(gate.empty());

        const auto containment = gate.find(
            "TreePolicyKind::fault_containment");
        const auto epoch_zero = gate.find(
            "request.predecessor_epoch_number != 0");
        const auto first_cycle = gate.find(
            "request_sequence_.cursor() != 0");
        const auto current_epoch = gate.find(
            "session_.ingress().current_epoch()");
        const auto epoch_trees = gate.find("epoch.trees()");
        const auto ready_latch = gate.find(
            "fault_containment_coverage_ready_emitted");
        const auto no_emit = gate.find("if (!emit_ready_event)");
        const auto emit = gate.find("emit_audit(", no_emit);
        const auto event = gate.find(
            "AdaptiveV2FaultContainmentCoverageReadyStructuredEvent",
            emit);
        const auto drain = gate.find("drain()", event);
        const auto latch_set = gate.find(
            "fault_containment_coverage_ready_emitted = true", drain);
        REQUIRE(containment != std::string::npos);
        REQUIRE(epoch_zero != std::string::npos);
        REQUIRE(first_cycle != std::string::npos);
        REQUIRE(current_epoch != std::string::npos);
        REQUIRE(epoch_trees != std::string::npos);
        REQUIRE(ready_latch != std::string::npos);
        REQUIRE(no_emit != std::string::npos);
        REQUIRE(event != std::string::npos);
        REQUIRE(emit != std::string::npos);
        REQUIRE(drain != std::string::npos);
        REQUIRE(latch_set != std::string::npos);
        CHECK(containment < current_epoch);
        CHECK(epoch_zero < current_epoch);
        CHECK(first_cycle < current_epoch);
        CHECK(current_epoch < epoch_trees);
        CHECK(ready_latch < no_emit);
        CHECK(no_emit < emit);
        CHECK(emit < event);
        CHECK(event < drain);
        CHECK(drain < latch_set);
        CHECK(count_occurrences(gate, "emit_audit(") == 1);
        CHECK(count_occurrences(
                  gate,
                  "fault_containment_coverage_ready_emitted = true") ==
              1);

        const auto coverage_check = evaluate.find(
            "fault_containment_coverage_ready(");
        const auto select = evaluate.find("session_.evaluate()",
                                          coverage_check);
        const auto ready_emit = evaluate.find(
            "fault_containment_coverage_ready(",
            select + std::string("session_.evaluate()").size());
        const auto scores = evaluate.find(
            "emit_new_score_trajectory()", ready_emit);
        REQUIRE(coverage_check != std::string::npos);
        REQUIRE(select != std::string::npos);
        REQUIRE(ready_emit != std::string::npos);
        REQUIRE(scores != std::string::npos);
        CHECK(coverage_check < select);
        CHECK(select < ready_emit);
        CHECK(ready_emit < scores);
    }
}
