#include <cerrno>
#include <cctype>
#include <cstddef>
#include <fstream>
#include <initializer_list>
#include <optional>
#include <sstream>
#include <stdexcept>
#include <string>
#include <utility>
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
    enum class LexicalState
    {
        code,
        line_comment,
        block_comment,
        string_literal,
        character_literal,
    };

    std::string result(contents.size(), ' ');
    auto state = LexicalState::code;
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
                result[cursor] = character;
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
                escaped = false;
            else if (character == '\\')
                escaped = true;
            else if ((state == LexicalState::string_literal &&
                      character == '"') ||
                     (state == LexicalState::character_literal &&
                      character == '\''))
                state = LexicalState::code;
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

std::string without_whitespace(const std::string &contents)
{
    std::string compact;
    compact.reserve(contents.size());
    for (const auto character : contents)
        if (std::isspace(static_cast<unsigned char>(character)) == 0)
            compact.push_back(character);
    return compact;
}

std::size_t count_occurrences(
    const std::string &contents,
    const std::string &needle)
{
    if (needle.empty())
        return 0;
    std::size_t count = 0;
    for (std::size_t cursor = 0;
         (cursor = contents.find(needle, cursor)) != std::string::npos;
         cursor += needle.size())
        ++count;
    return count;
}

bool contains_all(
    const std::string &contents,
    std::initializer_list<const char *> needles)
{
    for (const auto *needle : needles)
        if (contents.find(needle) == std::string::npos)
            return false;
    return true;
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
        const auto character =
            static_cast<unsigned char>(contents[cursor]);
        if (std::isalnum(character) == 0 && contents[cursor] != '_')
            break;
        ++cursor;
    }
    if (cursor == begin)
        return std::nullopt;
    return contents.substr(begin, cursor - begin);
}

std::optional<std::string> call_expression(
    const std::string &contents,
    const std::string &callee,
    std::size_t start = 0)
{
    const auto call = contents.find(callee, start);
    if (call == std::string::npos)
        return std::nullopt;
    const auto opening = contents.find('(', call);
    if (opening == std::string::npos)
        return std::nullopt;

    std::size_t depth = 0;
    for (std::size_t cursor = opening; cursor < contents.size(); ++cursor)
    {
        if (contents[cursor] == '(')
            ++depth;
        else if (contents[cursor] == ')' && --depth == 0)
            return contents.substr(call, cursor - call + 1);
    }
    return std::nullopt;
}

std::vector<std::string> call_arguments(const std::string &call)
{
    const auto opening = call.find('(');
    if (opening == std::string::npos || call.empty() || call.back() != ')')
        return {};

    std::vector<std::string> arguments;
    std::size_t begin = opening + 1;
    std::size_t parentheses = 0;
    std::size_t braces = 0;
    std::size_t brackets = 0;
    for (std::size_t cursor = begin; cursor + 1 < call.size(); ++cursor)
    {
        switch (call[cursor])
        {
        case '(':
            ++parentheses;
            break;
        case ')':
            if (parentheses > 0)
                --parentheses;
            break;
        case '{':
            ++braces;
            break;
        case '}':
            if (braces > 0)
                --braces;
            break;
        case '[':
            ++brackets;
            break;
        case ']':
            if (brackets > 0)
                --brackets;
            break;
        case ',':
            if (parentheses == 0 && braces == 0 && brackets == 0)
            {
                arguments.push_back(call.substr(begin, cursor - begin));
                begin = cursor + 1;
            }
            break;
        default:
            break;
        }
    }
    arguments.push_back(call.substr(begin, call.size() - begin - 1));
    return arguments;
}

struct ProcessResult
{
    int status{0};
    std::string output;
};

ProcessResult run_hotstuff_app(const std::vector<std::string> &arguments)
{
    int output_pipe[2];
    if (::pipe(output_pipe) != 0)
        throw std::runtime_error("failed to create subprocess pipe");

    const auto child = ::fork();
    if (child < 0)
    {
        ::close(output_pipe[0]);
        ::close(output_pipe[1]);
        throw std::runtime_error("failed to fork hotstuff-app");
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
        owned_arguments.emplace_back(KAURI_HOTSTUFF_APP_PATH);
        owned_arguments.insert(
            owned_arguments.end(), arguments.begin(), arguments.end());
        std::vector<char *> raw_arguments;
        raw_arguments.reserve(owned_arguments.size() + 1);
        for (auto &argument : owned_arguments)
            raw_arguments.push_back(&argument[0]);
        raw_arguments.push_back(nullptr);
        ::execv(KAURI_HOTSTUFF_APP_PATH, raw_arguments.data());
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
            throw std::runtime_error("failed to wait for hotstuff-app");
    result.status = WIFEXITED(wait_status)
                        ? WEXITSTATUS(wait_status)
                        : 128 + WTERMSIG(wait_status);
    return result;
}

} // namespace

TEST_CASE(
    "adaptive-v2 manager bundle ingress wiring authenticates before exact inbox ingestion",
    "[we08][adaptive-v2][live-successor][manager-ingress]"
    "[wiring][intentional-red]")
{
    const auto header = source("include/hotstuff/hotstuff.h");
    const auto implementation = code_without_comments_or_literals(
        source("src/hotstuff.cpp"));
    const auto install = function_body(
        implementation,
        "void HotStuffBase::install_adaptive_v2_definition_handlers(");
    const auto handler = function_body(
        implementation,
        "void HotStuffBase::adaptive_v2_epoch_change_bundle_handler(");

    SECTION("one adaptive-v2-only network handler owns the bundle opcode")
    {
        CHECK(header.find("AdaptiveV2CommandInbox") != std::string::npos);
        CHECK(without_whitespace(
                  code_without_comments_or_literals(header))
                  .find("MsgAdaptiveV2EpochChangeBundle&&") !=
              std::string::npos);
        REQUIRE_FALSE(install.empty());
        CHECK(count_occurrences(
                  install,
                  "adaptive_v2_epoch_change_bundle_handler") == 1);
    }

    SECTION("manager authentication precedes decoding and exact ingestion")
    {
        REQUIRE_FALSE(handler.empty());
        CHECK(contains_in_order(
            handler,
            {"conn->get_peer_id()",
             "authorize_manager_peer",
             "pn.get_peer_conn(",
             "conn->get_peer_cert()",
             "pinned_connection != conn",
             "PeerId(*certificate)",
             "decode_adaptive_v2_epoch_change_bundle",
             ".ingest("}));
        CHECK(count_occurrences(
                  handler,
                  "decode_adaptive_v2_epoch_change_bundle") == 1);
        CHECK(count_occurrences(handler, ".ingest(") == 1);
        CHECK(contains_all(
            handler,
            {"active_configuration",
             "exact_epochs->find_epoch(",
             "epoch_digest()",
             "epoch_change_verifier",
             "exact_epochs"}));

        const auto ingest = call_expression(handler, ".ingest(");
        REQUIRE(ingest.has_value());
        const auto arguments = call_arguments(*ingest);
        REQUIRE(arguments.size() == 4);
        CHECK(without_whitespace(arguments[0]).find('*') !=
              std::string::npos);
        CHECK(without_whitespace(arguments[1]).find('*') !=
              std::string::npos);
        CHECK(arguments[2].find("epoch_change_verifier") !=
              std::string::npos);
        CHECK(arguments[3].find("exact_epochs") != std::string::npos);
    }
}

TEST_CASE(
    "manager bundle ingress wiring has no staging activation or consensus authority",
    "[we08][adaptive-v2][live-successor][authority-boundary]"
    "[wiring][intentional-red]")
{
    const auto implementation = code_without_comments_or_literals(
        source("src/hotstuff.cpp"));
    const auto handler = function_body(
        implementation,
        "void HotStuffBase::adaptive_v2_epoch_change_bundle_handler(");
    REQUIRE_FALSE(handler.empty());

    for (const auto *forbidden : {
             "stage_available_v2(",
             "handle_stage(",
             "handle_arm(",
             "prepare_committed_v2(",
             "record_committed_v2(",
             "on_v2_post_block_commit(",
             "observe_activation(",
             "process_block(",
             "on_receive_vote(",
             "do_consensus("})
    {
        CAPTURE(forbidden);
        CHECK(handler.find(forbidden) == std::string::npos);
    }
}

TEST_CASE(
    "both beat proposal wiring paths carry only an exact-root inbox reservation",
    "[we08][adaptive-v2][live-successor][proposal-wiring]"
    "[wiring][intentional-red]")
{
    const auto header = source("include/hotstuff/hotstuff.h");
    const auto implementation = code_without_comments_or_literals(
        source("src/hotstuff.cpp"));
    const auto beat = function_body(
        implementation, "void HotStuffBase::beat()");
    const auto local_hook = function_body(
        implementation,
        "void HotStuffBase::on_local_proposal_processed(");

    REQUIRE_FALSE(beat.empty());

    SECTION("HotStuff owns the live successor inbox")
    {
        CHECK(header.find("AdaptiveV2CommandInbox") !=
              std::string::npos);
    }

    SECTION("reservation identity comes from the exact active view and root")
    {
        CHECK(contains_all(
            beat,
            {"AdaptiveV2ProposalPreparation",
             "active_effect()",
             "find_exact_runtime_tree(",
             "get_tree_root()",
             "get_id()",
             ".prepare_for_proposal(",
             "canonical_block_extra"}));
        const auto preparation = beat.find("AdaptiveV2ProposalPreparation");
        const auto reserve = beat.find(".prepare_for_proposal(");
        REQUIRE(preparation != std::string::npos);
        REQUIRE(reserve != std::string::npos);
        CHECK(preparation < reserve);
    }

    SECTION("empty or retired inboxes skip proposal-history construction")
    {
        const auto snapshot = beat.find(".snapshot(");
        const auto history_probe = beat.find("Block history_probe(");
        REQUIRE(snapshot != std::string::npos);
        REQUIRE(history_probe != std::string::npos);
        CHECK(snapshot < history_probe);
        CHECK(contains_all(
            beat.substr(snapshot, history_probe - snapshot),
            {"AdaptiveV2CommandInboxState::available",
             "AdaptiveV2CommandInboxState::in_flight"}));
    }

    SECTION("manual pipelining no longer hard-codes empty block extra")
    {
        const auto stored = call_expression(beat, "storage->add_blk(");
        REQUIRE(stored.has_value());
        const auto block_call = call_expression(*stored, "new Block(");
        REQUIRE(block_call.has_value());
        const auto arguments = call_arguments(*block_call);
        REQUIRE(arguments.size() >= 4);
        CHECK(without_whitespace(arguments[3]) != "bytearray_t()");
    }

    SECTION("ordinary proposal production receives explicit reserved extra")
    {
        const auto ordinary_call = call_expression(beat, "on_propose(");
        REQUIRE(ordinary_call.has_value());
        const auto arguments = call_arguments(*ordinary_call);
        REQUIRE(arguments.size() == 3);
        CHECK(without_whitespace(arguments[2]) != "bytearray_t()");
    }

    SECTION("exceptions release the exact reservation")
    {
        const auto manual_construction = beat.find("storage->add_blk(");
        const auto ordinary_construction = beat.find("on_propose(");
        REQUIRE(manual_construction != std::string::npos);
        REQUIRE(ordinary_construction != std::string::npos);

        const auto manual_copy = beat.rfind(
            "canonical_block_extra", manual_construction);
        const auto manual_try = beat.rfind("try", manual_copy);
        const auto manual_catch = beat.find("catch", manual_construction);
        const auto manual_release = beat.find(".release(", manual_catch);
        REQUIRE(manual_copy != std::string::npos);
        REQUIRE(manual_try != std::string::npos);
        REQUIRE(manual_catch != std::string::npos);
        REQUIRE(manual_release != std::string::npos);
        CHECK(manual_try < manual_copy);
        CHECK(manual_copy < manual_construction);
        CHECK(manual_construction < manual_catch);
        CHECK(manual_catch < manual_release);

        const auto ordinary_copy = beat.rfind(
            "canonical_block_extra", ordinary_construction);
        const auto ordinary_try = beat.rfind("try", ordinary_copy);
        const auto ordinary_catch = beat.find("catch", ordinary_construction);
        const auto ordinary_release = beat.find(".release(", ordinary_catch);
        REQUIRE(ordinary_copy != std::string::npos);
        REQUIRE(ordinary_try != std::string::npos);
        REQUIRE(ordinary_catch != std::string::npos);
        REQUIRE(ordinary_release != std::string::npos);
        CHECK(ordinary_try < ordinary_copy);
        CHECK(ordinary_copy < ordinary_construction);
        CHECK(ordinary_construction < ordinary_catch);
        CHECK(ordinary_catch < ordinary_release);
    }

    SECTION("the pre-broadcast exact-key hook marks only the reservation")
    {
        REQUIRE_FALSE(local_hook.empty());
        CHECK(local_hook.find("pmaker->record_verified_progress(") ==
              std::string::npos);
        CHECK(local_hook.find("LeaderProgressEvent") == std::string::npos);
        CHECK(count_occurrences(local_hook, ".mark_proposed(") == 1);
        const auto mark = call_expression(local_hook, ".mark_proposed(");
        REQUIRE(mark.has_value());
        const auto arguments = call_arguments(*mark);
        REQUIRE(arguments.size() == 2);
        CHECK(arguments[1].find("key") != std::string::npos);
    }
}

TEST_CASE(
    "live successor source wiring remains outside the normal HotStuff quorum path",
    "[we08][adaptive-v2][live-successor][quorum-control][wiring]")
{
    const auto consensus = code_without_comments_or_literals(
        source("src/consensus.cpp"));
    const auto ordinary = function_body(
        consensus, "block_t HotStuffCore::on_propose(");
    const auto processing = function_body(
        consensus, "Proposal HotStuffCore::process_block(");

    REQUIRE_FALSE(ordinary.empty());
    CHECK(contains_in_order(
        ordinary,
        {"process_block(",
         "on_local_proposal_processed(prop.key())",
         "do_broadcast_proposal(prop)"}));
    REQUIRE_FALSE(processing.empty());
    CHECK(contains_in_order(
        processing,
        {"admit_local(prop)",
         "on_receive_vote(Vote(",
         "on_propose_(prop)"}));
    CHECK(processing.find("AdaptiveV2CommandInbox") == std::string::npos);
    CHECK(processing.find("MsgAdaptiveV2EpochChangeBundle") ==
          std::string::npos);
}

TEST_CASE(
    "adaptive v3 epoch commands receive one pre-QC priority exposure",
    "[we08][adaptive-v3][live-successor][proposal-wiring][liveness]")
{
    const auto header = source("include/hotstuff/hotstuff.h");
    const auto implementation = code_without_comments_or_literals(
        source("src/hotstuff.cpp"));
    const auto local_hook = function_body(
        implementation,
        "void HotStuffBase::on_local_proposal_processed(");
    const auto broadcast = function_body(
        implementation,
        "void HotStuffBase::do_broadcast_proposal(");

    REQUIRE_FALSE(local_hook.empty());
    REQUIRE_FALSE(broadcast.empty());
    CHECK(header.find("adaptive_v3_pending_command_priority_fanout") !=
          std::string::npos);
    CHECK(contains_in_order(
        local_hook,
        {".mark_proposed(",
         "epoch_protocol_mode == EpochProtocolMode::adaptive_v3",
         "adaptive_v3_pending_command_priority_fanout = key"}));

    const auto claim = broadcast.find(
        "*adaptive_v3_pending_command_priority_fanout == prop.key()");
    const auto consume = broadcast.find(
        "adaptive_v3_pending_command_priority_fanout.reset()", claim);
    const auto guard = broadcast.find(
        "if (adaptive_v3_command_priority_fanout", consume);
    const auto assigned = broadcast.find(
        "metadata->tree.assigned_subtree", guard);
    const auto priority = broadcast.find(
        "pn.send_msg_urgent(", assigned);
    const auto ordinary = broadcast.find(
        "metadata->tree.direct_children", priority);
    REQUIRE(claim != std::string::npos);
    REQUIRE(consume != std::string::npos);
    REQUIRE(guard != std::string::npos);
    REQUIRE(assigned != std::string::npos);
    REQUIRE(priority != std::string::npos);
    REQUIRE(ordinary != std::string::npos);
    CHECK(claim < consume);
    CHECK(consume < guard);
    CHECK(guard < assigned);
    CHECK(assigned < priority);
    CHECK(priority < ordinary);
    CHECK(contains_all(
        broadcast.substr(guard, ordinary - guard),
        {"metadata->tree.root == get_id()",
         "!metadata->tree.parent.has_value()",
         "member == get_id()",
         "MsgPropose(DataStream(adaptive_payload))"}));
}

TEST_CASE(
    "commit and activation wiring retires live successor material exactly",
    "[we08][adaptive-v2][live-successor][commit][activation]"
    "[wiring][intentional-red]")
{
    const auto implementation = code_without_comments_or_literals(
        source("src/hotstuff.cpp"));
    const auto consensus = function_body(
        implementation,
        "void HotStuffBase::do_consensus_with_identity_provenance(");
    const auto committed_history = function_body(
        implementation,
        "void HotStuffBase::record_committed_epoch_change_history(");
    const auto committed_digest = function_body(
        implementation,
        "HotStuffBase::adaptive_v2_committed_epoch_change_payload_digest(");
    const auto activation = function_body(
        implementation,
        "void HotStuffBase::finish_adaptive_epoch_commit(");

    SECTION("commit observation uses the authoritative key and payload digest")
    {
        REQUIRE_FALSE(consensus.empty());
        CHECK(contains_in_order(
            consensus,
            {"close_committed_block(",
             "resolve_committed_proposal_identity(",
             "observe_authoritative_commit("}));
        CHECK(contains_in_order(
            committed_history,
            {"extract_epoch_change_block_extra(",
             "extracted.payload_digest",
             "PendingCommittedEpochChange"}));
        CHECK(committed_digest.find(
                  "pending_committed_epoch_change->payload_digest") !=
              std::string::npos);
        CHECK(committed_digest.find("epoch_change_payload_digest(") ==
              std::string::npos);
        const auto observe = call_expression(
            consensus, "observe_authoritative_commit(");
        REQUIRE(observe.has_value());
        const auto arguments = call_arguments(*observe);
        REQUIRE(arguments.size() == 2);
        CHECK_FALSE(without_whitespace(arguments[0]).empty());
        CHECK(arguments[1].find("payload_digest") != std::string::npos);
        const auto cleanup = consensus.find(
            "forget_proposal_view_generation(");
        REQUIRE(cleanup != std::string::npos);
        CHECK(consensus.find("observe_authoritative_commit(") < cleanup);
    }

    SECTION("activation observation consumes one exact configuration generation")
    {
        REQUIRE_FALSE(activation.empty());
        CHECK(activation.find("ActivationTransition::activated") !=
              std::string::npos);
        const auto observe = call_expression(
            activation, "observe_activation(");
        REQUIRE(observe.has_value());
        const auto arguments = call_arguments(*observe);
        REQUIRE(arguments.size() == 2);
        CHECK(arguments[0].find("configuration") != std::string::npos);
        CHECK(arguments[1].find("generation") != std::string::npos);
    }
}

TEST_CASE(
    "adaptive-v2 replica CLI wiring pins the manager address and TLS-derived peer",
    "[we08][adaptive-v2][live-successor][cli][tls]"
    "[wiring][intentional-red]")
{
    const auto app = source("examples/hotstuff_app.cpp");
    const auto code = code_without_comments_or_literals(app);
    const auto main = function_body(code, "int main(");
    REQUIRE_FALSE(main.empty());

    SECTION("the public options bind independent address and certificate values")
    {
        const auto address_binding = option_binding(
            app, "epoch-manager-address");
        const auto certificate_binding = option_binding(
            app, "epoch-manager-tls-cert");
        REQUIRE(address_binding.has_value());
        REQUIRE(certificate_binding.has_value());
        CHECK(*address_binding != *certificate_binding);
        CHECK(count_occurrences(code, *address_binding) >= 2);
        CHECK(count_occurrences(code, *certificate_binding) >= 2);
    }

    SECTION("the built CLI exposes both manager pins")
    {
        const auto help = run_hotstuff_app({"--help"});
        REQUIRE(help.status == 0);
        CHECK(help.output.find("epoch-manager-address") !=
              std::string::npos);
        CHECK(help.output.find("epoch-manager-tls-cert") !=
              std::string::npos);
    }

    SECTION("adaptive-v2 derives PeerId from TLS and configures before start")
    {
        const auto configure = main.find("papp->configure_epoch_manager(");
        const auto start = main.find("papp->start(reps)");
        REQUIRE(configure != std::string::npos);
        REQUIRE(start != std::string::npos);
        CHECK(configure < start);
        CHECK(count_occurrences(
                  main, "papp->configure_epoch_manager(") == 1);

        const auto adaptive_branch = main.rfind(
            "epoch_protocol_mode == EpochProtocolMode::adaptive_v2",
            configure);
        REQUIRE(adaptive_branch != std::string::npos);

        const auto configure_call = call_expression(
            main, "papp->configure_epoch_manager(");
        REQUIRE(configure_call.has_value());
        CHECK(call_arguments(*configure_call).size() == 2);

        const auto manager_certificate = code.find(
            "X509::create_from_der");
        const auto manager_peer = code.find("PeerId", manager_certificate);
        const auto global_configure = code.find(
            "papp->configure_epoch_manager(");
        REQUIRE(manager_certificate != std::string::npos);
        REQUIRE(manager_peer != std::string::npos);
        REQUIRE(global_configure != std::string::npos);
        CHECK(manager_certificate < manager_peer);
        CHECK(manager_peer < global_configure);
    }
}
