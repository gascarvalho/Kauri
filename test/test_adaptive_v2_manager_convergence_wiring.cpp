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
    "manager remains advisory below Q and terminates only on convergence or failure",
    "[adaptive-v2][convergence][c6][manager][terminal][no-epoch2][wiring]")
{
    const auto manager = code_without_comments_or_literals(
        source("examples/adaptation_manager.cpp"));
    const auto run = function_body(manager, "int run()");
    REQUIRE_FALSE(run.empty());

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
    CHECK(manager.find(
              "AdaptiveV2ManagerConvergenceStatus::retry_exhausted") !=
          std::string::npos);
    CHECK(manager.find(
              "AdaptiveV2ManagerConvergenceStatus::conflicting_observation") !=
          std::string::npos);
    CHECK(manager.find("fail(") != std::string::npos);
    CHECK(run.find("convergence_succeeded_") != std::string::npos);

    const auto ready = manager.find("consume_ready_for_optimization(");
    REQUIRE(ready != std::string::npos);
    CHECK(manager.find("controller_.evaluate()", ready) ==
          std::string::npos);
    CHECK(count_occurrences(
              manager, "controller_.successor_bundle()") == 1);
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
