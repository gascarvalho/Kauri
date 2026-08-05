#include <cctype>
#include <cstddef>
#include <fstream>
#include <initializer_list>
#include <sstream>
#include <string>

#include "catch.hpp"
#include "hotstuff/adaptive_v2_reporting_outbox.h"

#ifndef KAURI_PROJECT_SOURCE_DIR
#error "KAURI_PROJECT_SOURCE_DIR must name the repository root"
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

std::string code_without_comments_or_literals(const std::string &contents)
{
    enum class State
    {
        code,
        line_comment,
        block_comment,
        string_literal,
        character_literal,
    };

    std::string result(contents.size(), ' ');
    auto state = State::code;
    bool escaped = false;
    for (std::size_t cursor = 0; cursor < contents.size(); ++cursor)
    {
        const auto character = contents[cursor];
        const auto next = cursor + 1 < contents.size()
                              ? contents[cursor + 1]
                              : '\0';
        if (character == '\n')
            result[cursor] = '\n';

        switch (state)
        {
        case State::code:
            if (character == '/' && next == '/')
            {
                state = State::line_comment;
                ++cursor;
            }
            else if (character == '/' && next == '*')
            {
                state = State::block_comment;
                ++cursor;
            }
            else if (character == '"')
                state = State::string_literal;
            else if (character == '\'' &&
                     !(cursor > 0 && cursor + 1 < contents.size() &&
                       std::isdigit(static_cast<unsigned char>(
                           contents[cursor - 1])) != 0 &&
                       std::isdigit(static_cast<unsigned char>(next)) != 0))
                state = State::character_literal;
            else
                result[cursor] = character;
            break;
        case State::line_comment:
            if (character == '\n')
                state = State::code;
            break;
        case State::block_comment:
            if (character == '*' && next == '/')
            {
                state = State::code;
                ++cursor;
            }
            break;
        case State::string_literal:
        case State::character_literal:
            if (escaped)
                escaped = false;
            else if (character == '\\')
                escaped = true;
            else if ((state == State::string_literal && character == '"') ||
                     (state == State::character_literal &&
                      character == '\''))
                state = State::code;
            break;
        }
    }
    return result;
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

bool contains_in_order(
    const std::string &contents,
    std::initializer_list<const char *> needles)
{
    std::size_t cursor = 0;
    for (const auto *needle : needles)
    {
        const auto found = contents.find(needle, cursor);
        if (found == std::string::npos)
            return false;
        cursor = found + std::string(needle).size();
    }
    return true;
}

std::string without_whitespace(const std::string &contents)
{
    std::string result;
    result.reserve(contents.size());
    for (const auto character : contents)
        if (std::isspace(static_cast<unsigned char>(character)) == 0)
            result.push_back(character);
    return result;
}

bool owns_manager_session(const std::string &manager)
{
    const auto compact = without_whitespace(manager);
    return compact.find("AdaptiveV2ManagerSessionsession_") !=
               std::string::npos &&
           compact.find(
               "AdaptiveV2ManagerRequestSequencerequest_sequence_") !=
               std::string::npos;
}

double numeric_constant(
    const std::string &contents,
    const std::string &name)
{
    const auto declaration = contents.find(name);
    REQUIRE(declaration != std::string::npos);
    const auto equals = contents.find('=', declaration + name.size());
    REQUIRE(equals != std::string::npos);
    const auto semicolon = contents.find(';', equals + 1);
    REQUIRE(semicolon != std::string::npos);
    return std::stod(contents.substr(equals + 1, semicolon - equals - 1));
}

} // namespace

TEST_CASE(
    "manager drives bounded convergence delivery from the controller exact bundle",
    "[adaptive-v2][convergence][c6][manager][delivery][wiring]")
{
    const auto raw_manager = source("examples/adaptation_manager.cpp");
    const auto manager = code_without_comments_or_literals(raw_manager);
    const auto compact = without_whitespace(manager);

    if (owns_manager_session(manager))
    {
        CHECK(raw_manager.find("adaptive_v2_manager_session.h") !=
              std::string::npos);
        CHECK(count_occurrences(
                  manager, "session_.successor_bundle()") == 1);
        CHECK(contains_in_order(
            manager,
            {"session_.successor_bundle()",
             "write_exclusive_bundle(",
             "session_.start_convergence(",
             "session_.due_deliveries(",
             "session_.record_enqueue_result("}));
    }
    else
    {
        CHECK(raw_manager.find("adaptive_v2_manager_convergence.h") !=
              std::string::npos);
        CHECK(count_occurrences(
                  manager, "controller_.successor_bundle()") == 1);
        CHECK(contains_in_order(
            manager,
            {"controller_.successor_bundle()",
             "AdaptiveV2ManagerConvergenceConfig",
             "membership",
             "retry_interval_ticks",
             "maximum_attempts_per_recipient",
             "convergence_deadline_tick",
             "AdaptiveV2ManagerConvergence"}));
        CHECK(compact.find(
                  "AdaptiveV2ManagerConvergence>(*bundle,") !=
              std::string::npos);
    }

    CHECK(count_occurrences(manager, "TimerEvent") >= 3);
    CHECK(manager.find("convergence_timer") != std::string::npos);
    CHECK(manager.find("due_deliveries(") != std::string::npos);
    CHECK(manager.find("record_enqueue_result(") != std::string::npos);
    CHECK(compact.find(
              "MsgAdaptiveV2EpochChangeBundle(DataStream(*request.canonical_bundle_bytes))") !=
          std::string::npos);
    CHECK(contains_in_order(
        manager,
        {"due_deliveries(",
         "request.canonical_bundle_bytes",
         "network_.send_msg(",
         "record_enqueue_result(",
         ".add("}));
}

TEST_CASE(
    "manager authenticates and registers both convergence observation handlers",
    "[adaptive-v2][convergence][c6][manager][tls][handlers][wiring]")
{
    const auto manager = code_without_comments_or_literals(
        source("examples/adaptation_manager.cpp"));
    const auto handlers = function_body(manager, "void register_handlers()");
    REQUIRE_FALSE(handlers.empty());

    CHECK(count_occurrences(
              handlers,
              "MsgAdaptiveV2EpochChangeCommittedObservation") == 1);
    CHECK(count_occurrences(
              handlers,
              "MsgAdaptiveV2EpochActivatedObservation") == 1);
    CHECK(handlers.find(
              "decode_adaptive_v2_epoch_change_committed_observation(") !=
          std::string::npos);
    CHECK(handlers.find(
              "decode_adaptive_v2_epoch_activated_observation(") !=
          std::string::npos);
    CHECK(count_occurrences(handlers, "authenticated_source(connection)") >=
          2);
    CHECK(handlers.find("observe_commit(") != std::string::npos);
    CHECK(handlers.find("observe_activation(") != std::string::npos);
    CHECK(contains_in_order(
        manager,
        {"connection->get_peer_cert()",
         "PeerId peer(*certificate)",
         "peer_to_replica_.find(peer)"}));
}

TEST_CASE(
    "manager audits every convergence ingress disposition and quarantines conflicts locally",
    "[adaptive-v2][convergence][manager][ingress][audit][quarantine]")
{
    const auto raw_manager = source("examples/adaptation_manager.cpp");
    const auto manager = code_without_comments_or_literals(raw_manager);
    const auto handlers = function_body(manager, "void register_handlers()");
    REQUIRE_FALSE(handlers.empty());

    const auto commit_begin = handlers.find(
        "MsgAdaptiveV2EpochChangeCommittedObservation");
    const auto activation_begin = handlers.find(
        "MsgAdaptiveV2EpochActivatedObservation", commit_begin);
    const auto error_begin = handlers.find(
        "reg_error_handler", activation_begin);
    REQUIRE(commit_begin != std::string::npos);
    REQUIRE(activation_begin != std::string::npos);
    REQUIRE(error_begin != std::string::npos);
    const auto commit_handler = handlers.substr(
        commit_begin, activation_begin - commit_begin);
    const auto activation_handler = handlers.substr(
        activation_begin, error_begin - activation_begin);

    for (const auto &handler : {commit_handler, activation_handler})
    {
        CAPTURE(handler.size());
        CHECK(count_occurrences(
                  handler, "emit_convergence_event(") >= 3);
        CHECK(contains_in_order(
            handler,
            {"authenticated_source(connection)",
             "if (!source.has_value())",
             "emit_convergence_event(",
             "return"}));
        CHECK(contains_in_order(
            handler,
            {"decode_adaptive_v2_",
             "if (!decoded)",
             "emit_convergence_event(",
             "return"}));
        CHECK(handler.find("convergence_disposition_name(") !=
              std::string::npos);
    }

    for (const auto *disposition : {
             "rejected_unauthenticated_source",
             "rejected_wire_decode",
             "accepted",
             "duplicate",
             "rejected_nonmember",
             "rejected_spoofed_source",
             "rejected_stale",
             "rejected_wrong_identity",
             "conflicting_observation",
             "terminal"})
    {
        CAPTURE(disposition);
        CHECK(raw_manager.find(disposition) != std::string::npos);
    }

    const auto emit = function_body(
        manager, "void emit_convergence_event(");
    REQUIRE_FALSE(emit.empty());
    CHECK(emit.find("event.disposition") != std::string::npos);
}

TEST_CASE(
    "manager remains advisory below Q and advances only on convergence or failure",
    "[adaptive-v2][convergence][c6][manager][terminal][no-epoch2][wiring]")
{
    const auto manager = code_without_comments_or_literals(
        source("examples/adaptation_manager.cpp"));
    const auto run = function_body(manager, "int run()");
    REQUIRE_FALSE(run.empty());

    if (owns_manager_session(manager))
    {
        CHECK(count_occurrences(
                  manager, "session_.consume_ready_and_rotate(") == 1);
        CHECK(contains_in_order(
            manager,
            {"AdaptiveV2ManagerConvergenceStatus::awaiting_activations",
             "return",
             "AdaptiveV2ManagerConvergenceStatus::ready_for_optimization",
             "session_.consume_ready_and_rotate(",
             "request_sequence_.observe_terminal_records(",
             "request_sequence_.shutdown_eligible()"}));
        CHECK(manager.find("session_.begin_cycle(") !=
              std::string::npos);
        CHECK(manager.find("request_sequence_.current_policy()") !=
              std::string::npos);
    }
    else
    {
        CHECK(count_occurrences(
                  manager, "consume_ready_for_optimization(") == 1);
        CHECK(contains_in_order(
            manager,
            {"AdaptiveV2ManagerConvergenceStatus::awaiting_activations",
             "return",
             "AdaptiveV2ManagerConvergenceStatus::ready_for_optimization",
             "consume_ready_for_optimization(",
             "convergence_succeeded_",
             "event_context_.stop()"}));
        const auto ready =
            manager.find("consume_ready_for_optimization(");
        REQUIRE(ready != std::string::npos);
        CHECK(manager.find("controller_.evaluate()", ready) ==
              std::string::npos);
        CHECK(count_occurrences(
                  manager, "controller_.successor_bundle()") == 1);
        CHECK(run.find("convergence_succeeded_") !=
              std::string::npos);
    }
    CHECK(manager.find(
              "AdaptiveV2ManagerConvergenceStatus::retry_exhausted") !=
          std::string::npos);
    CHECK(manager.find(
              "AdaptiveV2ManagerConvergenceStatus::conflicting_observation") !=
          std::string::npos);
    CHECK(manager.find("fail(") != std::string::npos);
    CHECK(manager.find("build_adaptive_v2_successor_bundle(") ==
          std::string::npos);
}

TEST_CASE(
    "manager ACKs accepted and exact duplicate observations before bounded ready drain",
    "[adaptive-v2][convergence][manager][ack][duplicate][drain]")
{
    const auto manager = code_without_comments_or_literals(
        source("examples/adaptation_manager.cpp"));
    const auto handlers = function_body(manager, "void register_handlers()");
    const auto status = function_body(
        manager, "void handle_convergence_status()");
    REQUIRE_FALSE(handlers.empty());
    REQUIRE_FALSE(status.empty());

    CHECK(count_occurrences(
              handlers,
              "MsgAdaptiveV2ConvergenceObservationAck") >= 2);
    CHECK(count_occurrences(
              handlers,
              "adaptive_v2_convergence_observation_digest(") >= 2);
    CHECK(count_occurrences(
              handlers,
              "AdaptiveV2ConvergenceAckDisposition::positive") >= 2);
    CHECK(handlers.find(
              "AdaptiveV2ConvergenceAckDisposition::permanent_rejection") !=
          std::string::npos);
    CHECK(count_occurrences(
              handlers,
              "AdaptiveV2ManagerConvergenceDisposition::duplicate") >= 2);
    CHECK(count_occurrences(handlers, "network_.send_msg(") >= 2);
    CHECK(handlers.find("decoded.observation->identity") !=
          std::string::npos);

    // The final Q-triggering positive ACK and exact late duplicates must be
    // drainable after readiness. Immediate event-loop stop can lose them.
    CHECK(manager.find("convergence_ack_drain_timer") !=
          std::string::npos);
    CHECK(manager.find("begin_convergence_ack_drain(") !=
          std::string::npos);
    const auto ready = status.find(
        "AdaptiveV2ManagerConvergenceStatus::ready_for_optimization");
    const auto failure = status.find(
        "AdaptiveV2ManagerConvergenceStatus::retry_exhausted", ready);
    REQUIRE(ready != std::string::npos);
    REQUIRE(failure != std::string::npos);
    const auto ready_branch = status.substr(ready, failure - ready);
    CHECK(ready_branch.find("begin_convergence_ack_drain(") !=
          std::string::npos);
    CHECK(ready_branch.find("event_context_.stop()") ==
          std::string::npos);
}

TEST_CASE(
    "manager ready drain covers capped convergence ACK retransmit plus scheduling margin",
    "[adaptive-v2][convergence][manager][ack][drain][maximum-backoff]")
{
    const auto manager = source("examples/adaptation_manager.cpp");
    const auto drain_seconds = numeric_constant(
        manager, "kConvergenceAckDrainSeconds");
    const auto maximum_retry_seconds =
        static_cast<double>(
            hotstuff::AdaptiveV2ReportingOutboxLimits{}
                .maximum_retry_backoff_ns) /
        1'000'000'000.0;
    constexpr double scheduling_margin_seconds = 0.1;

    CHECK(maximum_retry_seconds == 1.0);
    CHECK(drain_seconds >=
          maximum_retry_seconds + scheduling_margin_seconds);
}

TEST_CASE(
    "manager exposes a bounded convergence deadline while preserving the default",
    "[adaptive-v2][convergence][manager][deadline][configuration]")
{
    const auto manager = source("examples/adaptation_manager.cpp");
    const auto compact = without_whitespace(
        code_without_comments_or_literals(manager));

    CHECK(manager.find("convergence-deadline-seconds") !=
          std::string::npos);
    CHECK(manager.find("kConvergenceDefaultDeadlineTicks = 120") !=
          std::string::npos);
    if (owns_manager_session(code_without_comments_or_literals(manager)))
    {
        CHECK(compact.find(
                  "config.convergence_window_ticks="
                  "options.convergence_deadline_ticks") !=
              std::string::npos);
        CHECK(compact.find(
                  "session_.start_convergence(convergence_tick_)") !=
              std::string::npos);
    }
    else
    {
        CHECK(compact.find(
                  "convergence_config.convergence_deadline_tick="
                  "convergence_tick_+options_.convergence_deadline_ticks") !=
              std::string::npos);
    }
}

TEST_CASE(
    "manager passes the validated scientific seed into shape-v1",
    "[shape25][adaptive-v2][manager][shape-v1][seed][configuration]")
{
    const auto manager = source("examples/adaptation_manager.cpp");
    const auto compact = without_whitespace(manager);

    CHECK(manager.find("shape-deterministic-seed") !=
          std::string::npos);
    CHECK(compact.find(
              "options.shape_deterministic_seed="
              "parse_unsigned<std::uint64_t>("
              "opt_shape_deterministic_seed->get(),"
              "\"shapedeterministicseed\",false)") !=
          std::string::npos);
    CHECK(compact.find(
              "config.shape_selection.deterministic_seed="
              "options.shape_deterministic_seed") !=
          std::string::npos);
    CHECK(compact.find(
              "config.shape_selection.deterministic_seed="
              "kSnapshotSeed") == std::string::npos);
}

TEST_CASE(
    "manager bounds the adaptation target without changing derived quorum",
    "[shape25][adaptive-v2][manager][minority][configuration]")
{
    const auto manager = source("examples/adaptation_manager.cpp");
    const auto compact = without_whitespace(manager);

    CHECK(manager.find("required-nonresponsive") !=
          std::string::npos);
    CHECK(compact.find(
              "options.required_nonresponsive="
              "opt_required_nonresponsive->get().empty()?"
              "options.runtime_shape.required_nonresponsive:") !=
          std::string::npos);
    CHECK(compact.find(
              "options.required_nonresponsive>"
              "options.runtime_shape.quorum.fault_threshold") !=
          std::string::npos);
    CHECK(compact.find(
              "config.selection.required_nonresponsive="
              "options.required_nonresponsive") !=
          std::string::npos);
    CHECK(compact.find(
              "config.selection.required_nonresponsive="
              "options.runtime_shape.quorum.quorum") ==
          std::string::npos);
}

TEST_CASE(
    "manager drops the ACK for the Q completing accepted activation",
    "[adaptive-v2][convergence][manager][ack][loss][deterministic]")
{
    const auto raw_manager = source("examples/adaptation_manager.cpp");
    const auto manager = code_without_comments_or_literals(raw_manager);
    const auto handler = function_body(
        manager, "MsgAdaptiveV2EpochActivatedObservation &&message");
    const auto compact = without_whitespace(handler);
    REQUIRE_FALSE(handler.empty());

    CHECK(compact.find(
              "if(disposition=="
              "AdaptiveV2ManagerConvergenceDisposition::accepted){"
              "++accepted_activation_ack_ordinal_") !=
          std::string::npos);
    CHECK(compact.find(
              "*options_.experiment_drop_activation_ack=="
              "accepted_activation_ack_ordinal_") !=
          std::string::npos);
    if (owns_manager_session(manager))
    {
        CHECK(compact.find(
                  "session_.convergence_status()=="
                  "std::optional<AdaptiveV2ManagerConvergenceStatus>{"
                  "AdaptiveV2ManagerConvergenceStatus::"
                  "ready_for_optimization}") !=
              std::string::npos);
    }
    else
    {
        CHECK(compact.find(
                  "convergence_->status()=="
                  "AdaptiveV2ManagerConvergenceStatus::"
                  "ready_for_optimization") !=
              std::string::npos);
    }
    CHECK(raw_manager.find(
              "experiment activation ACK ordinal must equal quorum") !=
          std::string::npos);
}

TEST_CASE(
    "manager pre-bounds convergence observations before copying network payloads",
    "[adaptive-v2][convergence][manager][wire][payload-bound][allocation]")
{
    const auto manager = code_without_comments_or_literals(
        source("examples/adaptation_manager.cpp"));
    const auto commit_handler = function_body(
        manager,
        "MsgAdaptiveV2EpochChangeCommittedObservation &&message");
    const auto activation_handler = function_body(
        manager,
        "MsgAdaptiveV2EpochActivatedObservation &&message");
    REQUIRE_FALSE(commit_handler.empty());
    REQUIRE_FALSE(activation_handler.empty());

    for (const auto *handler : {&commit_handler, &activation_handler})
    {
        CHECK(contains_in_order(
            *handler,
            {"message.serialized.size()",
             "convergence_wire_limits_.maximum_payload_bytes",
             "static_cast<bytearray_t>(message.serialized)"}));
    }
}

TEST_CASE(
    "manager contains ACK construction and send exceptions without skipping status handling",
    "[adaptive-v2][convergence][manager][ack][exception][audit][status]")
{
    const auto manager = code_without_comments_or_literals(
        source("examples/adaptation_manager.cpp"));
    const auto commit_handler = without_whitespace(function_body(
        manager,
        "MsgAdaptiveV2EpochChangeCommittedObservation &&message"));
    const auto activation_handler = without_whitespace(function_body(
        manager,
        "MsgAdaptiveV2EpochActivatedObservation &&message"));
    REQUIRE_FALSE(commit_handler.empty());
    REQUIRE_FALSE(activation_handler.empty());

    for (const auto *handler : {&commit_handler, &activation_handler})
    {
        CHECK(contains_in_order(
            *handler,
            {"boolacknowledgement_sent=false;",
             "try{",
             "acknowledgement.observation_digest=",
             "adaptive_v2_convergence_observation_digest(",
             "network_.send_msg(",
             "catch(...){",
             "acknowledgement_sent=false;",
             "if(!acknowledgement_sent){",
             "emit_convergence_event(",
             "handle_convergence_status();"}));
        CHECK(count_occurrences(
                  *handler, "handle_convergence_status();") == 1);
    }
}

TEST_CASE(
    "manager terminal audit carries the exact Q-winning convergence identity",
    "[adaptive-v2][convergence][manager][identity][audit][ready]")
{
    const auto manager = code_without_comments_or_literals(
        source("examples/adaptation_manager.cpp"));
    const auto status = function_body(
        manager, "void handle_convergence_status()");
    REQUIRE_FALSE(status.empty());

    if (owns_manager_session(manager))
    {
        CHECK(status.find("session_.convergence_audit()") !=
              std::string::npos);
        CHECK(contains_in_order(
            status,
            {"convergence->winning_identity",
             "AdaptiveV2ConvergenceTransition::converged",
             "convergence->winning_identity",
             "AdaptiveV2ConvergenceTransition::ready",
             "convergence->winning_identity",
             "begin_convergence_ack_drain()"}));
        const auto drain = function_body(
            manager, "void begin_convergence_ack_drain()");
        CHECK(drain.find("session_.consume_ready_and_rotate(") !=
              std::string::npos);
    }
    else
    {
        CHECK(count_occurrences(status, "winning_identity()") >= 1);
        CHECK(contains_in_order(
            status,
            {"winning_identity()",
             "AdaptiveV2ConvergenceTransition::converged",
             "winning_identity",
             "consume_ready_for_optimization(",
             "AdaptiveV2ConvergenceTransition::ready",
             "winning_identity"}));
    }
}

TEST_CASE(
    "convergence audit distinguishes delivery observations readiness and failure",
    "[adaptive-v2][convergence][c6][structured-event][audit]")
{
    const auto header = code_without_comments_or_literals(
        source("include/hotstuff/structured_event.h"));
    const auto implementation = source("src/structured_event.cpp");
    const auto raw_manager = source("examples/adaptation_manager.cpp");
    const auto manager = code_without_comments_or_literals(
        raw_manager);

    CHECK(header.find("AdaptiveV2ConvergenceTransition") !=
          std::string::npos);
    CHECK(header.find("AdaptiveV2ConvergenceStructuredEvent") !=
          std::string::npos);
    for (const auto *transition : {
             "delivery_attempt",
             "commit_observed",
             "activation_observed",
             "converged",
             "ready",
             "failure"})
    {
        CAPTURE(transition);
        CHECK(header.find(transition) != std::string::npos);
        CHECK(manager.find(
                  std::string("AdaptiveV2ConvergenceTransition::") +
                  transition) != std::string::npos);
    }

    for (const auto *event_name : {
             "adaptive_v2_delivery_attempt",
             "adaptive_v2_commit_observed",
             "adaptive_v2_activation_observed",
             "adaptive_v2_converged",
             "adaptive_v2_ready",
             "adaptive_v2_convergence_failure"})
    {
        CAPTURE(event_name);
        CHECK(implementation.find(event_name) != std::string::npos);
    }
    CHECK(manager.find("emit_audit(") != std::string::npos);

    const auto drive = function_body(
        raw_manager, "void drive_convergence()");
    REQUIRE_FALSE(drive.empty());
    CHECK(drive.find("enqueued") != std::string::npos);
    CHECK(drive.find("enqueue_failed") != std::string::npos);
}

TEST_CASE(
    "ACK drain projects matching immutable terminal convergence counts",
    "[adaptive-v2][convergence][manager][terminal][ack-drain][audit]"
    "[wiring][intentional-red]")
{
    const auto manager = code_without_comments_or_literals(
        source("examples/adaptation_manager.cpp"));
    const auto emit = function_body(
        manager, "void emit_convergence_event(");
    REQUIRE_FALSE(emit.empty());

    const auto terminal_records =
        emit.find("session_.terminal_records()");
    const auto live_convergence =
        emit.find("session_.convergence_audit()");
    REQUIRE(terminal_records != std::string::npos);
    REQUIRE(live_convergence != std::string::npos);
    CHECK(terminal_records < live_convergence);
    CHECK(contains_in_order(
        emit,
        {"session_.terminal_records()",
         "winning_activation",
         "event.identity",
         "accepted_commit_count",
         "accepted_activation_count"}));
    CHECK(emit.find(
              "*candidate.winning_activation == *event.identity") !=
          std::string::npos);
}

TEST_CASE(
    "manager owns one recurring session and emits transition terminal proof",
    "[adaptive-v2][manager][session][request-sequence][wiring]"
    "[structured-event][intentional-red]")
{
    const auto raw_manager = source("examples/adaptation_manager.cpp");
    const auto manager = code_without_comments_or_literals(raw_manager);
    const auto compact = without_whitespace(manager);

    const bool owns_session =
        compact.find("AdaptiveV2ManagerSessionsession_") !=
        std::string::npos;
    const bool owns_request_sequence =
        compact.find("AdaptiveV2ManagerRequestSequencerequest_sequence_") !=
        std::string::npos;
    if (!owns_session || !owns_request_sequence)
    {
        FAIL(
            "M12-R02 RED: the manager example must own one "
            "AdaptiveV2ManagerSession and one "
            "AdaptiveV2ManagerRequestSequence");
    }

    CHECK(compact.find("AdaptiveV2ManagerIngressingress_") ==
          std::string::npos);
    CHECK(compact.find("AdaptiveV2ManagerControllercontroller_") ==
          std::string::npos);
    CHECK(compact.find(
              "unique_ptr<AdaptiveV2ManagerConvergence>convergence_") ==
          std::string::npos);

    for (const auto *seam : {
             "session_.ingest_readiness(",
             "session_.ingest_lifecycle(",
             "session_.ingest_evidence(",
             "session_.evaluate(",
             "session_.successor_bundle(",
             "session_.start_convergence(",
             "session_.due_deliveries(",
             "session_.record_enqueue_result(",
             "session_.observe_commit(",
             "session_.observe_activation(",
             "session_.convergence_status(",
             "session_.consume_ready_and_rotate(",
             "session_.shutdown("})
    {
        CAPTURE(seam);
        CHECK(manager.find(seam) != std::string::npos);
    }

    CHECK(manager.find("request_sequence_.current_policy()") !=
          std::string::npos);
    CHECK(manager.find(
              "request_sequence_.observe_terminal_records(") !=
          std::string::npos);
    CHECK(manager.find("request_sequence_.shutdown_eligible()") !=
          std::string::npos);

    const auto drain = function_body(
        manager, "void begin_convergence_ack_drain()");
    REQUIRE_FALSE(drain.empty());
    CHECK(contains_in_order(
        drain,
        {"session_.consume_ready_and_rotate(",
         "request_sequence_.observe_terminal_records(",
         "request_sequence_.shutdown_eligible()",
         "event_context_.stop()"}));
    CHECK(drain.find("schedule_current_predecessor_residency(") !=
          std::string::npos);

    const auto output_path = function_body(
        manager, "std::string transition_bundle_output_path(");
    REQUIRE_FALSE(output_path.empty());
    CHECK(output_path.find("bundle_output") != std::string::npos);
    CHECK(output_path.find("successor_epoch_number") !=
          std::string::npos);
    CHECK(output_path.find("successor_epoch_digest") !=
          std::string::npos);
    CHECK(compact.find(
              "write_exclusive_bundle(options_.bundle_output,") ==
          std::string::npos);
    CHECK(manager.find("write_exclusive_bundle(") !=
          std::string::npos);
    CHECK(manager.find(
              "request.evidence_snapshot_output =") !=
          std::string::npos);
    CHECK(manager.find(
              "request.evidence_snapshot_path") !=
          std::string::npos);
    CHECK(manager.find("exclusive_artifact_outputs") !=
          std::string::npos);

    const auto snapshot = function_body(
        manager, "void emit_evidence_snapshot(");
    REQUIRE_FALSE(snapshot.empty());
    CHECK(contains_in_order(
        snapshot,
        {"session_.ingress()",
         "ledger.accepted()",
         "definition.evidence_snapshot_id",
         "definition.trees",
         "serialize_adaptive_v2_evidence_snapshot_payload(",
         "structured_event_sink_.emit_audit(",
         "structured_event_sink_.health()",
         "write_exclusive_json("}));
    CHECK(snapshot.find("record.ingestion_sequence >") !=
          std::string::npos);
    CHECK(snapshot.find("observation.configuration.epoch_number") !=
          std::string::npos);
    CHECK(snapshot.find("observation.configuration.epoch_digest") !=
          std::string::npos);
    CHECK(snapshot.find("accepted_prefix_count") !=
          std::string::npos);
    CHECK(snapshot.find("has_post_baseline_observation") !=
          std::string::npos);
    CHECK(snapshot.find("build_adaptation_snapshot(") !=
          std::string::npos);
    CHECK(snapshot.find("full_prefix_snapshot_id") !=
          std::string::npos);
    CHECK(snapshot.find("const auto &definition = bundle.definition()") !=
          std::string::npos);
    CHECK(snapshot.find("definition.evidence_snapshot_id") !=
          std::string::npos);
    CHECK(snapshot.find("event.observations") == std::string::npos);
    CHECK(snapshot.find("AdaptiveV2EvidenceSnapshotObservation") ==
          std::string::npos);
    CHECK(snapshot.find("tree.members_breadth_first.front()") !=
          std::string::npos);

    const auto evaluate = function_body(manager, "void evaluate()");
    REQUIRE_FALSE(evaluate.empty());
    CHECK(contains_in_order(
        evaluate,
        {"session_.successor_bundle()",
         "write_exclusive_bundle(",
         "emit_evidence_snapshot(",
         "session_.start_convergence("}));

    const auto exclusive_json = function_body(
        manager, "void write_exclusive_json(");
    REQUIRE_FALSE(exclusive_json.empty());
    CHECK(exclusive_json.find("O_EXCL") != std::string::npos);
    CHECK(exclusive_json.find("O_NOFOLLOW") != std::string::npos);
    CHECK(exclusive_json.find("fsync(") != std::string::npos);
    CHECK(exclusive_json.find("unlink(") != std::string::npos);

    const auto root_validation = function_body(
        manager, "bool transition_policy_matches_current_roots(");
    REQUIRE_FALSE(root_validation.empty());
    CHECK(contains_in_order(
        root_validation,
        {"current_epoch().trees()",
         "containment_baseline_roots.size()",
         "candidate.tree_id == root.tree_id",
         "tree->members_breadth_first.front()",
         "root.replica_id"}));
    const auto resolved_policy = function_body(
        manager, "resolved_transition_policy(");
    REQUIRE_FALSE(resolved_policy.empty());
    CHECK(contains_in_order(
        resolved_policy,
        {"resolve_containment_roots_from_predecessor",
         "session_.ingress().current_epoch().trees()",
         "tree.tree_id >= tree_count",
         "unique_roots.insert",
         "resolved.containment_baseline_roots.push_back",
         "transition_policy_matches_current_roots(resolved)"}));
    const auto cycle_context = function_body(
        manager, "bool add_cycle_audit_context(");
    REQUIRE_FALSE(cycle_context.empty());
    CHECK(cycle_context.find("predecessor_epoch_number") !=
          std::string::npos);

    const auto header = code_without_comments_or_literals(
        source("include/hotstuff/structured_event.h"));
    const auto implementation =
        source("src/structured_event.cpp");
    CHECK(header.find(
              "AdaptiveV2ManagerSessionTerminalStructuredEvent") !=
          std::string::npos);
    CHECK(implementation.find("adaptive_v2_session_terminal") !=
          std::string::npos);
    CHECK(header.find(
              "AdaptiveV2EvidenceSnapshotStructuredEvent") !=
          std::string::npos);
    CHECK(header.find(
              "AdaptiveV2EvidenceSnapshotObservation") ==
          std::string::npos);
    CHECK(header.find(
              "serialize_adaptive_v2_evidence_snapshot_payload") !=
          std::string::npos);
    CHECK(implementation.find("adaptive_v2_evidence_snapshot") !=
          std::string::npos);

    const auto emit_terminals = function_body(
        manager, "void emit_new_session_terminals()");
    REQUIRE_FALSE(emit_terminals.empty());
    CHECK(contains_in_order(
        emit_terminals,
        {"try",
         "session_.terminal_records()",
         "AdaptiveV2ManagerSessionTerminalStructuredEvent",
         "emit_audit(",
         "catch (...)"}));
    CHECK(contains_in_order(
        emit_terminals,
        {"catch (...)", "failed_ = true", "event_context_.stop()"}));
    CHECK(manager.find("AdaptiveV2ConvergenceTransition::ready") !=
          std::string::npos);
}

TEST_CASE(
    "manager keeps compact snapshots within the default line capacity",
    "[adaptive-v2][manager][snapshot][structured-event][wiring]")
{
    const auto manager = code_without_comments_or_literals(
        source("examples/adaptation_manager.cpp"));

    CHECK(manager.find(
              "kManagerStructuredEventMaximumLineBytes") ==
          std::string::npos);
    CHECK(manager.find(
              "StructuredEventLimits{}.maximum_queued_bytes") ==
          std::string::npos);

    const auto config = function_body(
        manager,
        "hotstuff::StructuredEventConfig manager_structured_event_config(");
    REQUIRE_FALSE(config.empty());
    CHECK(config.find("maximum_line_bytes") == std::string::npos);
    CHECK(config.find("maximum_queued_bytes") == std::string::npos);

    const auto snapshot = function_body(
        manager, "void emit_evidence_snapshot(");
    REQUIRE_FALSE(snapshot.empty());
    const auto serialization = snapshot.find(
        "serialize_adaptive_v2_evidence_snapshot_payload(");
    REQUIRE(serialization != std::string::npos);
    CHECK(snapshot.find(
              "StructuredEventLimits{}.maximum_line_bytes",
              serialization) != std::string::npos);
}

TEST_CASE(
    "manager does not report ready or dispatch after initial cycle start fails",
    "[adaptive-v2][manager][startup][failure][lifecycle][wiring]")
{
    const auto manager = code_without_comments_or_literals(
        source("examples/adaptation_manager.cpp"));
    const auto run = function_body(manager, "int run()");
    REQUIRE_FALSE(run.empty());

    const auto failed_start = run.find("if (!begin_current_cycle())");
    REQUIRE(failed_start != std::string::npos);
    const auto operational_guard = run.find("if (!failed_)", failed_start);
    REQUIRE(operational_guard != std::string::npos);
    CHECK(failed_start < operational_guard);
    CHECK(contains_in_order(
        run.substr(failed_start),
        {"if (!begin_current_cycle())", "fail(", "if (!failed_)"}));

    const auto fail = function_body(
        manager, "void fail(const char *reason) noexcept");
    REQUIRE_FALSE(fail.empty());
    CHECK(contains_in_order(
        fail, {"failed_ = true", "event_context_.stop()"}));

    const auto operational = function_body(
        run.substr(operational_guard), "if (!failed_)");
    REQUIRE_FALSE(operational.empty());
    CHECK(contains_in_order(
        operational,
        {"ProcessLifecycleState::ready",
         "structured_event_drain_timer.add(",
         "event_context_.dispatch()"}));
    CHECK(count_occurrences(
              run, "ProcessLifecycleState::ready") == 1);
    CHECK(count_occurrences(
              run, "event_context_.dispatch()") == 1);
}

TEST_CASE(
    "manager isolates reputation snapshot and convergence audit bursts",
    "[adaptive-v2][manager][reputation][snapshot][convergence]"
    "[structured-event][wiring]")
{
    const auto manager = code_without_comments_or_literals(
        source("examples/adaptation_manager.cpp"));
    const auto trajectory = function_body(
        manager, "void emit_new_score_trajectory() noexcept");
    REQUIRE_FALSE(trajectory.empty());

    CHECK(contains_in_order(
        trajectory,
        {"while (emitted_score_trajectory_ < trajectory.size())",
         "structured_event_sink_.drain()",
         "structured_event_sink_.health()",
         "return",
         "ReputationEvidenceAppliedStructuredEvent",
         "structured_event_sink_.emit_audit(",
         "structured_event_sink_.health()",
         "++emitted_score_trajectory_"}));

    const auto loop = function_body(
        trajectory,
        "while (emitted_score_trajectory_ < trajectory.size())");
    REQUIRE_FALSE(loop.empty());
    const auto loop_position = trajectory.find(loop);
    REQUIRE(loop_position != std::string::npos);
    const auto after_loop =
        trajectory.substr(loop_position + loop.size());
    CHECK(contains_in_order(
        after_loop,
        {"structured_event_sink_.drain()",
         "structured_event_sink_.health()",
         "fail(",
         "return"}));

    const auto snapshot = function_body(
        manager, "void emit_evidence_snapshot(");
    REQUIRE_FALSE(snapshot.empty());
    CHECK(contains_in_order(
        snapshot,
        {"structured_event_sink_.emit_audit(",
         "structured_event_sink_.drain()",
         "structured_event_sink_.health()",
         "write_exclusive_json("}));

    const auto evaluate = function_body(manager, "void evaluate()");
    REQUIRE_FALSE(evaluate.empty());
    CHECK(contains_in_order(
        evaluate,
        {"emit_new_score_trajectory()",
         "if (failed_)",
         "emit_evidence_snapshot(",
         "session_.start_convergence(",
         "drive_convergence()"}));
}

TEST_CASE(
    "manager coalesces ingress evaluation at the latest bounded watermark",
    "[adaptive-v2][manager][evaluation][coalescing][timer][wiring]")
{
    const auto raw_manager = source("examples/adaptation_manager.cpp");
    const auto manager = code_without_comments_or_literals(raw_manager);

    CHECK(numeric_constant(
              raw_manager, "kEvaluationCoalescingSeconds") ==
          Approx(0.05));
    CHECK(manager.find("evaluation_timer") != std::string::npos);
    CHECK(manager.find("evaluation_timer_pending_") !=
          std::string::npos);

    const auto schedule = function_body(
        manager, "void schedule_evaluation() noexcept");
    const auto fire = function_body(
        manager, "void handle_evaluation_timer() noexcept");
    const auto evaluate = function_body(manager, "void evaluate()");
    const auto ingest = function_body(manager, "void ingest(");
    REQUIRE_FALSE(schedule.empty());
    REQUIRE_FALSE(fire.empty());
    REQUIRE_FALSE(evaluate.empty());
    REQUIRE_FALSE(ingest.empty());

    CHECK(contains_in_order(
        schedule,
        {"evaluation_timer_pending_",
         "return",
         "readiness_stats()",
         "ledger().high_watermark()",
         "last_evaluated_ready_members_",
         "last_evaluated_evidence_cutoff_",
         "return",
         "evaluation_timer_pending_ = true",
         "evaluation_timer.add(",
         "kEvaluationCoalescingSeconds"}));
    CHECK(count_occurrences(
              schedule, "evaluation_timer.add(") == 1);
    CHECK(schedule.find("evaluation_timer.del()") ==
          std::string::npos);

    CHECK(contains_in_order(
        fire,
        {"evaluation_timer_pending_ = false", "evaluate()"}));
    CHECK(fire.find("evaluation_timer.add(") == std::string::npos);
    CHECK(fire.find("ledger().high_watermark()") ==
          std::string::npos);
    CHECK(contains_in_order(
        evaluate,
        {"readiness_stats()",
         "ledger().high_watermark()",
         "last_evaluated_ready_members_",
         "last_evaluated_evidence_cutoff_",
         "session_.evaluate()"}));

    CHECK(contains_in_order(
        ingest,
        {"operation(",
         "emit_new_accepted_observations()",
         "result.status",
         "schedule_evaluation()"}));
    CHECK(ingest.find("evaluate()") == std::string::npos);
    CHECK(count_occurrences(
              ingest, "schedule_evaluation()") == 1);
}

TEST_CASE(
    "manager cancels coalesced evaluation across lifecycle boundaries",
    "[adaptive-v2][manager][evaluation][coalescing][cleanup][wiring]")
{
    const auto manager = code_without_comments_or_literals(
        source("examples/adaptation_manager.cpp"));
    const auto cancel = function_body(
        manager, "void cancel_pending_evaluation() noexcept");
    const auto fail = function_body(
        manager, "void fail(const char *reason) noexcept");
    const auto stop = function_body(
        manager, "void stop_runtime() noexcept");
    const auto begin_cycle = function_body(
        manager, "bool begin_current_cycle() noexcept");
    const auto drain = function_body(
        manager, "void begin_convergence_ack_drain() noexcept");
    const auto evaluate = function_body(manager, "void evaluate()");
    const auto run = function_body(manager, "int run()");
    REQUIRE_FALSE(cancel.empty());
    REQUIRE_FALSE(fail.empty());
    REQUIRE_FALSE(stop.empty());
    REQUIRE_FALSE(begin_cycle.empty());
    REQUIRE_FALSE(drain.empty());
    REQUIRE_FALSE(evaluate.empty());
    REQUIRE_FALSE(run.empty());

    CHECK(contains_in_order(
        cancel,
        {"evaluation_timer.del()",
         "evaluation_timer_pending_ = false"}));
    CHECK(fail.find("cancel_pending_evaluation()") !=
          std::string::npos);
    CHECK(stop.find("cancel_pending_evaluation()") !=
          std::string::npos);
    CHECK(fail.find("evaluate()") == std::string::npos);
    CHECK(stop.find("evaluate()") == std::string::npos);
    CHECK(contains_in_order(
        begin_cycle,
        {"cancel_pending_evaluation()",
         "last_evaluated_ready_members_.reset()",
         "last_evaluated_evidence_cutoff_.reset()",
         "session_.begin_cycle("}));
    CHECK(contains_in_order(
        evaluate,
        {"cancel_pending_evaluation()",
         "session_.start_convergence("}));
    CHECK(drain.find("cancel_pending_evaluation()") !=
          std::string::npos);
    CHECK(drain.find("evaluate()") == std::string::npos);

    CHECK(contains_in_order(
        run,
        {"begin_current_cycle()", "evaluate()"}));
}

TEST_CASE(
    "recurring manager delays each rotated predecessor by its explicit residency",
    "[adaptive-v2][manager][session][residency][timer][wiring]")
{
    const auto manager = code_without_comments_or_literals(
        source("examples/adaptation_manager.cpp"));
    REQUIRE(owns_manager_session(manager));

    CHECK(manager.find("minimum_predecessor_residency_ms") !=
          std::string::npos);
    CHECK(manager.find("kMaximumPredecessorResidencyMs") !=
          std::string::npos);
    CHECK(manager.find("predecessor_residency_timer") !=
          std::string::npos);
    CHECK(manager.find("predecessor_residency_pending_") !=
          std::string::npos);

    const auto drain = function_body(
        manager, "void begin_convergence_ack_drain()");
    REQUIRE_FALSE(drain.empty());
    CHECK(contains_in_order(
        drain,
        {"session_.consume_ready_and_rotate(",
         "request_sequence_.observe_terminal_records(",
         "request_sequence_.shutdown_eligible()",
         "schedule_current_predecessor_residency("}));

    const auto schedule = function_body(
        manager,
        "bool schedule_current_predecessor_residency() noexcept");
    REQUIRE_FALSE(schedule.empty());
    CHECK(contains_in_order(
        schedule,
        {"current_transition_request()",
         "minimum_predecessor_residency_ms",
         "predecessor_residency_deadline_",
         "predecessor_residency_pending_ = true",
         "predecessor_residency_timer.add("}));

    const auto fire = function_body(
        manager,
        "void handle_predecessor_residency_timer() noexcept");
    REQUIRE_FALSE(fire.empty());
    CHECK(contains_in_order(
        fire,
        {"steady_clock::now()",
         "now < predecessor_residency_deadline_",
         "predecessor_residency_timer.add(",
         "return",
         "predecessor_residency_pending_ = false",
         "begin_current_cycle()",
         "evaluate()"}));

    const auto evaluate = function_body(manager, "void evaluate()");
    REQUIRE_FALSE(evaluate.empty());
    CHECK(evaluate.find("predecessor_residency_pending_") !=
          std::string::npos);

    const auto stop = function_body(manager, "void stop_runtime()");
    REQUIRE_FALSE(stop.empty());
    CHECK(stop.find("predecessor_residency_timer.del()") !=
          std::string::npos);
    CHECK(count_occurrences(
              manager,
              "schedule_current_predecessor_residency()") == 2);

    const auto parse_options = function_body(
        manager, "ManagerOptions parse_options(");
    REQUIRE_FALSE(parse_options.empty());
    const auto first_request = parse_options.find(
        "options.transition_requests.empty()");
    const auto first_residency = parse_options.find(
        "request.minimum_predecessor_residency_ms", first_request);
    const auto append_request = parse_options.find(
        "options.transition_requests.push_back", first_request);
    REQUIRE(first_request != std::string::npos);
    REQUIRE(first_residency != std::string::npos);
    REQUIRE(append_request != std::string::npos);
    CHECK(first_request < first_residency);
    CHECK(first_residency < append_request);
}

TEST_CASE(
    "recurring session preserves terminal failure audit rollback and timer rearm",
    "[adaptive-v2][manager][session][failure][timer][wiring]")
{
    const auto manager = code_without_comments_or_literals(
        source("examples/adaptation_manager.cpp"));
    REQUIRE(owns_manager_session(manager));

    const auto status = function_body(
        manager, "void handle_convergence_status()");
    const auto drain = function_body(
        manager, "void begin_convergence_ack_drain()");
    const auto drive = function_body(
        manager, "void drive_convergence()");
    const auto begin_cycle = function_body(
        manager, "bool begin_current_cycle()");
    const auto schedule_residency = function_body(
        manager, "bool schedule_current_predecessor_residency()");
    REQUIRE_FALSE(status.empty());
    REQUIRE_FALSE(drain.empty());
    REQUIRE_FALSE(drive.empty());
    REQUIRE_FALSE(begin_cycle.empty());
    REQUIRE_FALSE(schedule_residency.empty());

    CHECK(contains_in_order(
        status,
        {"session_.terminal_records()",
         "convergence_retry_exhausted",
         "AdaptiveV2ConvergenceTransition::failure",
         "emit_new_session_terminals()"}));
    CHECK(contains_in_order(
        begin_cycle,
        {"resolved_transition_policy(",
         "add_cycle_audit_context(",
         "session_.begin_cycle(",
         "cycle_audits_.pop_back()"}));
    CHECK(contains_in_order(
        schedule_residency,
        {"begin_current_cycle()", "evaluate()"}));
    CHECK(contains_in_order(
        manager,
        {"session_.start_convergence(",
         "drive_convergence()",
         "session_.due_deliveries(",
         "convergence_timer.add("}));
}

TEST_CASE(
    "manager freezes baseline before a bounded observation hold",
    "[adaptive-v2][manager][session][baseline-hold][timer][wiring]")
{
    const auto manager = code_without_comments_or_literals(
        source("examples/adaptation_manager.cpp"));
    REQUIRE(owns_manager_session(manager));

    CHECK(manager.find("minimum_post_baseline_observation_ms") !=
          std::string::npos);
    CHECK(manager.find("post_baseline_observation_timer") !=
          std::string::npos);
    CHECK(manager.find("post_baseline_observation_pending_") !=
          std::string::npos);

    const auto begin = function_body(
        manager, "bool begin_current_cycle() noexcept");
    REQUIRE_FALSE(begin.empty());
    CHECK(begin.find("schedule_post_baseline_observation(") ==
          std::string::npos);

    const auto schedule = function_body(
        manager,
        "bool schedule_post_baseline_observation(");
    REQUIRE_FALSE(schedule.empty());
    CHECK(contains_in_order(
        schedule,
        {"session_.controller_audit()",
         "baseline_frozen",
         "minimum_post_baseline_observation_ms",
         "steady_clock::now()",
         "post_baseline_observation_pending_ = true",
         "post_baseline_observation_timer.add("}));
    CHECK(contains_in_order(
        schedule,
        {"minimum_post_baseline_observation_ms == 0",
         "schedule_post_baseline_evaluation()",
         "return true",
         "steady_clock::now()"}));

    const auto follow_up = function_body(
        manager,
        "void schedule_post_baseline_evaluation() noexcept");
    REQUIRE_FALSE(follow_up.empty());
    CHECK(contains_in_order(
        follow_up,
        {"last_evaluated_ready_members_.reset()",
         "last_evaluated_evidence_cutoff_.reset()",
         "schedule_evaluation()"}));

    const auto fire = function_body(
        manager,
        "void handle_post_baseline_observation_timer() noexcept");
    REQUIRE_FALSE(fire.empty());
    CHECK(contains_in_order(
        fire,
        {"now < post_baseline_observation_deadline_",
         "post_baseline_observation_timer.add(",
         "return",
         "post_baseline_observation_pending_ = false",
         "evaluate()"}));

    const auto evaluate = function_body(manager, "void evaluate()");
    const auto coalesce = function_body(
        manager, "void schedule_evaluation() noexcept");
    REQUIRE_FALSE(evaluate.empty());
    REQUIRE_FALSE(coalesce.empty());
    CHECK(contains_in_order(
        evaluate,
        {"session_.evaluate()",
         "AdaptiveV2ManagerControllerStatus::baseline_frozen",
         "schedule_post_baseline_observation("}));
    CHECK(count_occurrences(
              manager,
              "schedule_post_baseline_observation(") == 2);
    CHECK(evaluate.find("post_baseline_observation_pending_") !=
          std::string::npos);
    CHECK(coalesce.find("post_baseline_observation_pending_") !=
          std::string::npos);

    const auto session_header = code_without_comments_or_literals(
        source("include/hotstuff/adaptive_v2_manager_session.h"));
    const auto session_source = code_without_comments_or_literals(
        source("src/adaptive_v2_manager_session.cpp"));
    CHECK(session_header.find("bool baseline_frozen{false}") !=
          std::string::npos);
    const auto audit = function_body(
        session_source,
        "AdaptiveV2ManagerSession::controller_audit() const noexcept");
    REQUIRE_FALSE(audit.empty());
    CHECK(audit.find(
              "state.controller->baseline_frozen()") !=
          std::string::npos);

    const auto stop = function_body(manager, "void stop_runtime()");
    REQUIRE_FALSE(stop.empty());
    CHECK(stop.find("cancel_post_baseline_observation()") !=
          std::string::npos);
}

TEST_CASE(
    "final recurring ACK drain ignores ordinary ingress without disabling duplicates",
    "[adaptive-v2][manager][session][recurring][final-drain][wiring]")
{
    const auto manager = code_without_comments_or_literals(
        source("examples/adaptation_manager.cpp"));
    REQUIRE(owns_manager_session(manager));

    const auto ingest = function_body(manager, "void ingest(");
    const auto evaluate = function_body(manager, "void evaluate()");
    const auto drain = function_body(
        manager, "void begin_convergence_ack_drain()");
    REQUIRE_FALSE(ingest.empty());
    REQUIRE_FALSE(evaluate.empty());
    REQUIRE_FALSE(drain.empty());

    const auto ingest_complete = ingest.find(
        "request_sequence_.shutdown_eligible()");
    const auto ingest_operation = ingest.find("operation(");
    const auto ingest_schedule = ingest.find("schedule_evaluation()");
    REQUIRE(ingest_complete != std::string::npos);
    REQUIRE(ingest_operation != std::string::npos);
    REQUIRE(ingest_schedule != std::string::npos);
    CHECK(ingest_complete < ingest_operation);
    CHECK(ingest_complete < ingest_schedule);
    CHECK(ingest.find("evaluate()") == std::string::npos);

    const auto evaluate_complete = evaluate.find(
        "request_sequence_.shutdown_eligible()");
    const auto session_evaluate = evaluate.find("session_.evaluate()");
    REQUIRE(evaluate_complete != std::string::npos);
    REQUIRE(session_evaluate != std::string::npos);
    CHECK(evaluate_complete < session_evaluate);

    CHECK(contains_in_order(
        drain,
        {"request_sequence_.shutdown_eligible()",
         "convergence_ack_drain_timer",
         "event_context_.stop()"}));
    CHECK(manager.find("session_.observe_commit(") !=
          std::string::npos);
    CHECK(manager.find("session_.observe_activation(") !=
          std::string::npos);
}
