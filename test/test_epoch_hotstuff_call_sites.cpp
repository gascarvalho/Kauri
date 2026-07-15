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

} // namespace

TEST_CASE("HotStuffBase owns one explicitly selected epoch protocol binding",
          "[rem-d11][epoch-live-binding][hotstuff][intentional-red]")
{
    const auto header = source("include/hotstuff/hotstuff.h");
    const auto implementation = source("src/hotstuff.cpp");
    const auto constructor = function_body(
        implementation, "HotStuffBase::HotStuffBase(");

    CHECK(header.find("HotStuffEpochLiveBinding") != std::string::npos);
    CHECK(header.find(
              "EpochProtocolMode::legacy_static") != std::string::npos);
    REQUIRE_FALSE(constructor.empty());
    CHECK(contains_in_order(
        constructor,
        {"EpochProtocolMode::adaptive_v1",
         "install_adaptive_epoch_handlers",
         "else",
         "install_legacy_consensus_handlers"}));
    CHECK(count_occurrences(
              constructor, "install_adaptive_epoch_handlers") == 1);
    CHECK(count_occurrences(
              constructor, "install_legacy_consensus_handlers") == 1);
}

TEST_CASE("adaptive pn handlers authenticate the connection before delegation",
          "[rem-d11][epoch-live-binding][network][intentional-red]")
{
    const auto implementation = source("src/hotstuff.cpp");

    for (const auto &expected : {
             std::pair<const char *, const char *>{
                 "void HotStuffBase::adaptive_stage_epoch_handler(",
                 "handle_stage("},
             {"void HotStuffBase::adaptive_arm_epoch_handler(",
              "handle_arm("}})
    {
        const auto handler = function_body(implementation, expected.first);
        CAPTURE(expected.first);
        REQUIRE_FALSE(handler.empty());
        CHECK(contains_in_order(
            handler,
            {"conn->get_peer_id()",
             "authorize_manager_peer",
             expected.second}));
        CHECK(count_occurrences(handler, expected.second) == 1);
    }

    for (const auto &expected : {
             std::pair<const char *, const char *>{
                 "void HotStuffBase::adaptive_propose_handler(",
                 "handle_proposal("},
             {"void HotStuffBase::adaptive_vote_handler(", "handle_vote("},
             {"void HotStuffBase::adaptive_relay_handler(", "handle_relay("}})
    {
        const auto handler = function_body(implementation, expected.first);
        CAPTURE(expected.first);
        REQUIRE_FALSE(handler.empty());
        CHECK(contains_in_order(
            handler,
            {"conn->get_peer_id()",
             "peer_id_map.find",
             "authenticated_epoch_replica",
             expected.second}));
        CHECK(count_occurrences(handler, expected.second) == 1);
    }
}

TEST_CASE("live topology is prepared before the nofail exact-height swap",
          "[rem-d11][epoch-live-binding][two-phase][intentional-red]")
{
    const auto transaction = source("src/epoch_runtime_wiring.cpp");
    const auto live = source("src/epoch_live_binding.cpp");
    const auto prepare = function_body(
        transaction, "HotStuffEpochRuntimeTransaction::prepare(");
    const auto commit = function_body(
        transaction, "void HotStuffEpochRuntimeTransaction::commit(");
    const auto apply = function_body(
        live, "void HotStuffEpochLiveState::apply_update(");

    REQUIRE_FALSE(prepare.empty());
    REQUIRE_FALSE(commit.empty());
    REQUIRE_FALSE(apply.empty());
    CHECK(prepare.find("live_state") != std::string::npos);
    CHECK(prepare.find(".prepare(") != std::string::npos);
    CHECK(commit.find("live_state") != std::string::npos);
    CHECK(commit.find(".arm(") != std::string::npos);
    for (const auto *forbidden : {
             "find_exact_runtime_tree",
             "TreeNetwork(",
             "push_back(",
             "emplace(",
             "activate_leader_view"})
    {
        CAPTURE(forbidden);
        CHECK(apply.find(forbidden) == std::string::npos);
    }
}

TEST_CASE("the actual commit path delegates once to the live binding",
          "[rem-d11][epoch-live-binding][commit][intentional-red]")
{
    const auto implementation = source("src/hotstuff.cpp");
    const auto consensus = function_body(
        implementation, "void HotStuffBase::do_consensus(");

    REQUIRE_FALSE(consensus.empty());
    const std::string delegation =
        "epoch_live_binding->on_predecessor_commit(";
    const auto first = consensus.find(delegation);
    REQUIRE(first != std::string::npos);
    CHECK(consensus.find(delegation, first + delegation.size()) ==
          std::string::npos);
}

TEST_CASE("adaptive outbound consensus uses exact envelopes and legacy bytes remain",
          "[rem-d11][epoch-live-binding][outbound][intentional-red]")
{
    const auto header = source("include/hotstuff/hotstuff.h");
    const auto implementation = source("src/hotstuff.cpp");
    const auto wiring = source("src/epoch_live_binding.cpp");
    const auto encoder = function_body(
        wiring, "bytearray_t adaptive_epoch_consensus_message(");
    const auto generation_lookup = function_body(
        implementation,
        "HotStuffBase::find_exact_runtime_generation(");

    REQUIRE_FALSE(encoder.empty());
    CHECK(contains_all(
        encoder,
        {"configuration",
         "generation",
         "encode_epoch_consensus_envelope"}));
    CHECK(header.find("find_exact_runtime_generation") !=
          std::string::npos);
    REQUIRE_FALSE(generation_lookup.empty());
    CHECK(contains_all(
        generation_lookup,
        {"may_drain_exact_context", "topology.find_generation"}));

    for (const auto &expected : {
             std::pair<const char *, const char *>{
                 "void HotStuffBase::do_broadcast_proposal(",
                 "MsgPropose("},
             {"void HotStuffBase::do_vote(", "MsgVote("},
             {"bool HotStuffBase::send_exact_relay(", "MsgRelay("}})
    {
        const auto outbound = function_body(implementation, expected.first);
        CAPTURE(expected.first);
        REQUIRE_FALSE(outbound.empty());
        CHECK(contains_all(
            outbound,
            {"EpochProtocolMode::adaptive_v1",
             "find_exact_runtime_generation(",
             "adaptive_epoch_consensus_message(",
             expected.second}));
        CHECK(outbound.find("activation.active_effect()") ==
              std::string::npos);
    }

    const auto relay = function_body(
        implementation, "void HotStuffBase::relay_once(");
    REQUIRE_FALSE(relay.empty());
    CHECK(contains_all(
        relay,
        {"EpochProtocolMode::adaptive_v1",
         "proposal.wire_payload",
         "MsgPropose("}));
}

TEST_CASE("adaptive commit markers retain the committed proposal configuration",
          "[rem-d11][epoch-live-binding][markers][intentional-red]")
{
    const auto header = source("include/hotstuff/hotstuff.h");
    const auto implementation = source("src/hotstuff.cpp");
    const auto marker = function_body(
        implementation,
        "void HotStuffBase::record_adaptive_commit_marker(");
    const auto consensus = function_body(
        implementation, "void HotStuffBase::do_consensus(");

    CHECK(header.find(
              "const std::vector<ProposalKey> &committed_keys") !=
          std::string::npos);
    REQUIRE_FALSE(marker.empty());
    CHECK(contains_all(
        marker,
        {"committed_keys",
         "key.configuration",
         "find_exact_runtime_tree(key.configuration)"}));
    CHECK(marker.find("activation.active_effect()") == std::string::npos);

    REQUIRE_FALSE(consensus.empty());
    CHECK(contains_in_order(
        consensus,
        {"close_committed_block(blk->get_hash())",
         "record_adaptive_commit_marker(blk, keys)",
         "proposal_admission->retire_proposal(key)"}));
}

TEST_CASE("local executables ignore SIGPIPE before opening network sockets",
          "[rem-d11][local-demo][sigpipe][intentional-red]")
{
    for (const auto *path : {
             "examples/hotstuff_app.cpp",
             "examples/hotstuff_client.cpp"})
    {
        const auto contents = source(path);
        const auto main = function_body(contents, "int main(");
        CAPTURE(path);
        REQUIRE_FALSE(main.empty());
        CHECK(contains_in_order(
            main,
            {"std::signal(SIGPIPE, SIG_IGN)", "Config config("}));
        CHECK(count_occurrences(
                  main, "std::signal(SIGPIPE, SIG_IGN)") == 1);
    }
}

TEST_CASE("leader timeout rotates adaptively without falling into legacy mutation",
          "[rem-d11][epoch-live-binding][leader-timeout][intentional-red]")
{
    const auto implementation = source("src/hotstuff.cpp");
    const auto liveness = source("include/hotstuff/liveness.h");
    const auto adaptive = function_body(
        implementation,
        "HotStuffBase::rotate_tree_on_leader_timeout(");
    const auto timeout = function_body(
        liveness, "void rotate_active_tree_on_timeout(");

    REQUIRE_FALSE(adaptive.empty());
    CHECK(contains_all(
        adaptive,
        {"EpochProtocolMode::adaptive_v1",
         "epoch_live_binding",
         "rotate_to_tree(",
         "legacy_fallback",
         "rejected",
         "rotated"}));

    REQUIRE_FALSE(timeout.empty());
    CHECK(contains_in_order(
        timeout,
        {"hsc->rotate_tree_on_leader_timeout(",
         "legacy_fallback",
         "return",
         "activate_leader_view("}));
}
