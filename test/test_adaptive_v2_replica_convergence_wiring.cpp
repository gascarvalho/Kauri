#include <cctype>
#include <cstddef>
#include <fstream>
#include <initializer_list>
#include <sstream>
#include <string>

#include "catch.hpp"

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

std::string block_after(
    const std::string &contents,
    const std::string &needle)
{
    const auto marker = contents.find(needle);
    if (marker == std::string::npos)
        return {};
    const auto opening = contents.find('{', marker + needle.size());
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

} // namespace

TEST_CASE(
    "replica fences commit observation after authoritative v2 recording",
    "[adaptive-v2][convergence][c5][replica][commit][wiring]")
{
    const auto implementation = code_without_comments_or_literals(
        source("src/hotstuff.cpp"));
    const auto post_commit = function_body(
        implementation, "void HotStuffBase::do_post_block_commit(");
    const auto ordinary_commit = function_body(
        implementation, "void HotStuffBase::do_consensus(");
    const auto enqueue_pending = function_body(
        implementation,
        "void HotStuffBase::enqueue_pending_adaptive_v2_commit_observation(");
    REQUIRE_FALSE(post_commit.empty());
    REQUIRE_FALSE(enqueue_pending.empty());

    CHECK(count_occurrences(
              implementation,
              "enqueue_epoch_change_committed(") == 0);
    CHECK(count_occurrences(
              post_commit,
              "enqueue_convergence_observation(") == 0);
    CHECK(contains_in_order(
        post_commit,
        {"record_committed_v2(",
         "ActivationRecordDisposition::recorded",
         "recorded.record.has_value()",
         "AdaptiveV2EpochChangeIdentity",
         "recorded.record->predecessor_epoch_number",
         "recorded.record->predecessor_epoch_digest",
         "recorded.record->successor_epoch_number",
         "recorded.record->successor_epoch_digest",
         "recorded.record->payload_digest",
         "blk->get_height()",
         "blk->get_hash()",
         "recorded.record->activation_delay_blocks",
         "recorded.record->activation_height",
         "adaptive_v2_committed_convergence_identity",
         "enqueue_pending_adaptive_v2_commit_observation("}));
    CHECK(ordinary_commit.find("enqueue_epoch_change_committed(") ==
          std::string::npos);
    CHECK(contains_in_order(
        enqueue_pending,
        {"has_pending_adaptive_v2_lifecycle_fence()",
         "enqueue_convergence_observation(",
         "AdaptiveV2ReportingEnqueueStatus::queued",
         "schedule_adaptive_v2_reporting_flush("}));
}

TEST_CASE(
    "replica emits one activation observation only after atomic live install",
    "[adaptive-v2][convergence][c5][replica][activation][wiring]")
{
    const auto implementation = code_without_comments_or_literals(
        source("src/hotstuff.cpp"));
    const auto live_binding = code_without_comments_or_literals(
        source("src/epoch_live_binding.cpp"));
    const auto finish_live = function_body(
        live_binding, "HotStuffEpochLiveBinding::finish_commit(");
    const auto finish = function_body(
        implementation,
        "void HotStuffBase::finish_adaptive_epoch_commit(");
    const auto enqueue_pending = function_body(
        implementation,
        "void HotStuffBase::enqueue_pending_adaptive_v2_activation_observation(");
    const auto post_commit = function_body(
        implementation, "void HotStuffBase::do_post_block_commit(");
    REQUIRE_FALSE(finish_live.empty());
    REQUIRE_FALSE(finish.empty());
    REQUIRE_FALSE(post_commit.empty());

    CHECK(contains_in_order(
        finish_live,
        {"ActivationTransition::activated",
         "live_effects_.apply_update(",
         "return result"}));
    CHECK(contains_in_order(
        post_commit,
        {"epoch_live_binding->on_v2_post_block_commit(",
         "finish_adaptive_epoch_commit("}));
    CHECK(count_occurrences(
              implementation,
              "enqueue_epoch_activated(") == 1);
    CHECK(count_occurrences(finish, "enqueue_epoch_activated(") == 0);
    CHECK(count_occurrences(
              finish,
              "enqueue_pending_adaptive_v2_activation_observation(") == 1);
    CHECK(contains_in_order(
        finish,
        {"ActivationTransition::activated",
         "EpochIngressError::none",
         "activation.update.has_value()",
         "epoch_protocol_mode",
         "EpochProtocolMode::adaptive_v2",
         "configuration",
         "successor_epoch_number",
         "blk->get_height()",
         "activation_height",
         "enqueue_pending_adaptive_v2_activation_observation("}));
    REQUIRE_FALSE(enqueue_pending.empty());
    CHECK(contains_in_order(
        enqueue_pending,
        {"enqueue_epoch_activated(",
         "AdaptiveV2ReportingEnqueueStatus::queued",
         "adaptive_v2_committed_convergence_identity.reset()",
         "schedule_adaptive_v2_reporting_flush("}));
}

TEST_CASE(
    "activation observation enqueue failure retains and retries the exact committed identity",
    "[adaptive-v2][convergence][replica][activation][enqueue][retry]")
{
    const auto raw_implementation = source("src/hotstuff.cpp");
    const auto implementation = code_without_comments_or_literals(
        raw_implementation);
    const auto enqueue_pending = function_body(
        implementation,
        "void HotStuffBase::enqueue_pending_adaptive_v2_activation_observation(");
    const auto flush = function_body(
        implementation,
        "void HotStuffBase::flush_adaptive_v2_reporting(");
    REQUIRE_FALSE(enqueue_pending.empty());
    REQUIRE_FALSE(flush.empty());

    const auto queued = block_after(
        enqueue_pending,
        "AdaptiveV2ReportingEnqueueStatus::queued");
    REQUIRE_FALSE(queued.empty());
    CHECK(queued.find(
              "adaptive_v2_committed_convergence_identity.reset()") !=
          std::string::npos);
    CHECK(count_occurrences(
              enqueue_pending,
              "adaptive_v2_committed_convergence_identity.reset()") == 1);

    // A full FIFO is the reproducible non-queued result. The exact identity
    // stays latched and a later flush re-enters the enqueue helper after
    // freeing capacity; a permanent local failure is explicitly surfaced.
    CHECK(enqueue_pending.find(
              "AdaptiveV2ReportingEnqueueStatus::capacity_exceeded") !=
          std::string::npos);
    CHECK(enqueue_pending.find(
              "schedule_adaptive_v2_reporting_flush(") !=
          std::string::npos);
    CHECK(flush.find(
              "enqueue_pending_adaptive_v2_activation_observation(") !=
          std::string::npos);
    CHECK(raw_implementation.find(
              "activation_observation_enqueue_failed") !=
          std::string::npos);
}

TEST_CASE(
    "replica accepts convergence ACK only from the pinned manager and exact outbox correlation",
    "[adaptive-v2][convergence][replica][ack][tls][correlation]")
{
    const auto implementation = code_without_comments_or_literals(
        source("src/hotstuff.cpp"));
    const auto install = function_body(
        implementation,
        "void HotStuffBase::install_adaptive_v2_definition_handlers(");
    const auto handler = function_body(
        implementation,
        "void HotStuffBase::adaptive_v2_convergence_ack_handler(");
    REQUIRE_FALSE(install.empty());
    REQUIRE_FALSE(handler.empty());

    CHECK(install.find(
              "MsgAdaptiveV2ConvergenceObservationAck") !=
          std::string::npos);
    CHECK(contains_in_order(
        handler,
        {"epoch_manager_peer",
         "authorize_manager_peer(",
         "connection->get_peer_cert()",
         "PeerId(*manager_certificate)",
         "decode_adaptive_v2_convergence_observation_ack(",
         "target_replica_id",
         "get_id()",
         "acknowledge_convergence_observation("}));
    CHECK(handler.find("record_committed_v2(") == std::string::npos);
    CHECK(handler.find("finish_adaptive_epoch_commit(") ==
          std::string::npos);
    CHECK(handler.find("apply_update(") == std::string::npos);
}

TEST_CASE(
    "replica pre-bounds convergence ACKs before copying network payloads",
    "[adaptive-v2][convergence][replica][ack][wire][payload-bound][allocation]")
{
    const auto implementation = code_without_comments_or_literals(
        source("src/hotstuff.cpp"));
    const auto handler = function_body(
        implementation,
        "void HotStuffBase::adaptive_v2_convergence_ack_handler(");
    REQUIRE_FALSE(handler.empty());

    CHECK(contains_in_order(
        handler,
        {"message.serialized.size()",
         "maximum_payload_bytes",
         "static_cast<bytearray_t>(message.serialized)",
         "decode_adaptive_v2_convergence_observation_ack("}));
}

TEST_CASE(
    "replica convergence observations reuse the authenticated canonical reporting transport",
    "[adaptive-v2][convergence][c5][replica][transport][wiring]")
{
    const auto implementation = code_without_comments_or_literals(
        source("src/hotstuff.cpp"));
    const auto transmit = without_whitespace(function_body(
        implementation,
        "HotStuffBase::transmit_adaptive_v2_report("));
    REQUIRE_FALSE(transmit.empty());

    CHECK(transmit.find(
              "MsgAdaptiveV2EpochChangeCommittedObservation::opcode") !=
          std::string::npos);
    CHECK(transmit.find(
              "MsgAdaptiveV2EpochActivatedObservation::opcode") !=
          std::string::npos);
    CHECK(transmit.find(
              "MsgAdaptiveV2EpochChangeCommittedObservation(DataStream(report.canonical_payload))") !=
          std::string::npos);
    CHECK(transmit.find(
              "MsgAdaptiveV2EpochActivatedObservation(DataStream(report.canonical_payload))") !=
          std::string::npos);
    CHECK(contains_in_order(
        transmit,
        {"authorize_manager_peer(",
         "get_peer_cert()",
         "PeerId(*manager_certificate)",
         "report.canonical_payload"}));
}
