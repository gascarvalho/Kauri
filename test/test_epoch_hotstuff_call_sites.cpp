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
    const auto tree_switch = function_body(
        implementation, "ReconfigurationType HotStuffBase::isTreeSwitch(");

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
    REQUIRE_FALSE(tree_switch.empty());
    CHECK((contains_in_order(
               tree_switch,
               {"EpochProtocolMode::adaptive_v2",
                "return NO_SWITCH",
                "lastCheckedHeight"}) ||
           contains_in_order(
               tree_switch,
               {"epoch_protocol_mode != EpochProtocolMode::legacy_static",
                "return NO_SWITCH",
                "lastCheckedHeight"})));
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

TEST_CASE("adaptive v2 exposes one fail-closed pre-vote semantic gate",
          "[c08][epoch-change][pre-vote][intentional-red]")
{
    const auto implementation = source("src/hotstuff.cpp");
    const auto gate = function_body(
        implementation,
        "HotStuffBase::pre_vote_epoch_change_gate(");

    REQUIRE_FALSE(gate.empty());
    CHECK(contains_all(
        gate,
        {"EpochProtocolMode::adaptive_v2",
         "evaluate_epoch_change_proposal_chain(",
         "EpochChangeProposalDisposition::accepted",
         "EpochChangeProposalDisposition::duplicate",
         "EpochChangeProposalDisposition::defer",
         "EpochChangeProposalDisposition::rejected"}));
    CHECK(contains_in_order(
        gate,
        {"case EpochChangeProposalDisposition::defer:",
         "if (!result.recovery_request)",
         "return reject_epoch_change_gate();",
         "return result;"}));
}

TEST_CASE("active proposals pass the semantic gate before protocol mutation",
          "[c08][epoch-change][pre-vote][integration][intentional-red]")
{
    const auto implementation = source("src/hotstuff.cpp");
    const auto local = function_body(
        implementation, "bool HotStuffBase::admit_local(");
    const auto remote = function_body(
        implementation, "void HotStuffBase::process_active(");
    const auto ingress = function_body(
        implementation, "void HotStuffBase::propose_handler(");

    REQUIRE_FALSE(local.empty());
    CHECK(contains_in_order(
        local,
        {"pre_vote_epoch_change_gate(", "admit_exact_context("}));

    REQUIRE_FALSE(remote.empty());
    CHECK(contains_in_order(
        remote,
        {"delivered->get_hash() != metadata.key.block_hash",
         "pre_vote_epoch_change_gate(",
         "EpochChangeProposalDisposition::defer",
         "retain_deferred_epoch_change(",
         "return;",
         "EpochChangeProposalDisposition::rejected",
         "abort();",
         "proposal_contexts->admit_remote(",
         "on_receive_proposal(parsed)",
         "create_expected_vote_state(metadata.key)",
         "start_latency_deadline(metadata.key)",
         "start_aggregation_timer(metadata.key)"}));

    const auto gate = remote.find("pre_vote_epoch_change_gate(");
    const auto context = remote.find(
        "proposal_contexts->admit_remote(", gate);
    REQUIRE(gate != std::string::npos);
    REQUIRE(context != std::string::npos);
    const auto fail_closed_path = remote.substr(gate, context - gate);
    CHECK(contains_all(
        fail_closed_path,
        {"gate.recovery_request",
         "retain_deferred_epoch_change(",
         "abort();",
         "return;"}));
    for (const auto *forbidden : {
             "proposal_contexts->admit_remote(",
             "on_receive_proposal(parsed)",
             "create_expected_vote_state(",
             "start_latency_deadline(",
             "start_aggregation_timer("})
    {
        CAPTURE(forbidden);
        CHECK(fail_closed_path.find(forbidden) == std::string::npos);
    }

    REQUIRE_FALSE(ingress.empty());
    CHECK(ingress.find("pre_vote_epoch_change_gate(") ==
          std::string::npos);
}

TEST_CASE("adaptive v2 definition recovery is bounded and digest coalesced",
          "[c08][epoch-change][definition-recovery][intentional-red]")
{
    const auto header = source("include/hotstuff/hotstuff.h");
    const auto implementation = source("src/hotstuff.cpp");
    const auto retain = function_body(
        implementation,
        "HotStuffBase::retain_deferred_epoch_change(");
    const auto request = function_body(
        implementation,
        "HotStuffBase::send_epoch_definition_request(");

    CHECK(contains_all(
        header,
        {"struct DeferredEpochDefinitionRecovery",
         "EpochDefinitionRequest request;",
         "bool request_live",
         "std::map<ProposalKey, BufferedProposal> proposals;",
         "maximum_pending_epoch_definition_digests",
         "maximum_deferred_epoch_change_proposals",
         "deferred_epoch_definition_recoveries"}));

    REQUIRE_FALSE(retain.empty());
    CHECK(contains_all(
        retain,
        {"EpochProtocolMode::adaptive_v2",
         "request.successor_epoch_digest",
         "maximum_pending_epoch_definition_digests",
         "maximum_deferred_epoch_change_proposals",
         ".emplace(",
         "send_epoch_definition_request("}));

    REQUIRE_FALSE(request.empty());
    CHECK(contains_in_order(
        request,
        {"peer_id_map",
         "MsgEpochDefinitionRequest message(",
         "pn.send_msg("}));
    CHECK(request.find("epoch_manager_peer") == std::string::npos);
}

TEST_CASE("adaptive v2 recovery handlers authenticate and retry off ingress",
          "[c08][epoch-change][definition-recovery][network][intentional-red]")
{
    const auto implementation = source("src/hotstuff.cpp");
    const auto constructor = function_body(
        implementation, "HotStuffBase::HotStuffBase(");
    const auto install = function_body(
        implementation,
        "HotStuffBase::install_adaptive_v2_definition_handlers(");
    const auto request = function_body(
        implementation,
        "HotStuffBase::adaptive_definition_request_handler(");
    const auto reply = function_body(
        implementation,
        "HotStuffBase::adaptive_definition_reply_handler(");
    const auto queue = function_body(
        implementation,
        "HotStuffBase::queue_deferred_epoch_change_retries(");
    const auto retry = function_body(
        implementation,
        "HotStuffBase::retry_deferred_epoch_changes(");

    REQUIRE_FALSE(constructor.empty());
    CHECK(contains_in_order(
        constructor,
        {"install_legacy_consensus_handlers();",
         "EpochProtocolMode::adaptive_v2",
         "install_adaptive_v2_definition_handlers();"}));

    REQUIRE_FALSE(install.empty());
    CHECK(contains_all(
        install,
        {"adaptive_definition_request_handler",
         "adaptive_definition_reply_handler"}));
    CHECK(install.find("adaptive_stage_epoch_handler") ==
          std::string::npos);
    CHECK(install.find("adaptive_arm_epoch_handler") ==
          std::string::npos);

    REQUIRE_FALSE(request.empty());
    CHECK(contains_in_order(
        request,
        {"conn->get_peer_id()",
         "peer_id_map.find(peer)",
         "decode_epoch_definition_request(",
         "find_epoch_by_digest(",
         "kEpochDefinitionSchemaVersionV2",
         "MsgEpochDefinitionReply response(",
         "pn.send_msg("}));

    REQUIRE_FALSE(reply.empty());
    CHECK(contains_in_order(
        reply,
        {"conn->get_peer_id()",
         "peer_id_map.find(peer)",
         "decode_epoch_definition_reply(",
         "deferred_epoch_definition_recoveries.find(",
         "request_live",
         "stage_available_v2(",
         "successor_epoch_digest",
         "queue_deferred_epoch_change_retries("}));

    REQUIRE_FALSE(queue.empty());
    CHECK(queue.find("tcall.async_call(") != std::string::npos);
    REQUIRE_FALSE(retry.empty());
    CHECK(retry.find("process_active(") != std::string::npos);
    CHECK(retry.find("process_claimed_active(") == std::string::npos);
    CHECK(contains_in_order(
        retry,
        {"try",
         "proposals.reserve(",
         "process_active(",
         "catch (...)",
         "deferred_epoch_definition_recoveries.find(",
         "request_live = true",
         "send_epoch_definition_request("}));
}

TEST_CASE("deferred recovery is cleared only on deterministic terminal paths",
          "[c08][epoch-change][definition-recovery][lifecycle][intentional-red]")
{
    const auto implementation = source("src/hotstuff.cpp");
    const auto remote = function_body(
        implementation, "void HotStuffBase::process_active(");
    const auto activate = function_body(
        implementation,
        "void HotStuffBase::activate_proposal_configuration(");
    const auto retire = function_body(
        implementation,
        "void HotStuffBase::advance_committed_retirement_floor(");
    const auto retire_block = function_body(
        implementation,
        "void HotStuffBase::retire_deferred_epoch_changes_for_block(");
    const auto consensus = function_body(
        implementation, "void HotStuffBase::do_consensus(");
    const auto reply = function_body(
        implementation,
        "HotStuffBase::adaptive_definition_reply_handler(");
    const auto destructor = function_body(
        implementation, "HotStuffBase::~HotStuffBase(");

    REQUIRE_FALSE(remote.empty());
    CHECK(contains_in_order(
        remote,
        {"case EpochChangeProposalDisposition::accepted:",
         "case EpochChangeProposalDisposition::duplicate:",
         "erase_deferred_epoch_change(",
         "case EpochChangeProposalDisposition::defer:",
         "retain_deferred_epoch_change(",
         "case EpochChangeProposalDisposition::rejected:",
         "abort();"}));

    REQUIRE_FALSE(activate.empty());
    CHECK(activate.find(
              "retire_deferred_epoch_changes_before_epoch(") ==
          std::string::npos);
    REQUIRE_FALSE(retire.empty());
    CHECK(retire.find(
              "retire_deferred_epoch_changes_before_epoch(") !=
          std::string::npos);

    REQUIRE_FALSE(retire_block.empty());
    CHECK(contains_in_order(
        retire_block,
        {"proposal->first.block_hash != block_hash",
         "proposal_admission->retire_proposal(key)",
         "purge_pending_exact_contributions(key)",
         "proposal = proposals.erase(proposal)",
         "deferred_epoch_definition_recoveries.erase(recovery)"}));

    REQUIRE_FALSE(consensus.empty());
    CHECK(contains_in_order(
        consensus,
        {"retire_deferred_epoch_changes_for_block(blk->get_hash())",
         "close_committed_block(blk->get_hash())",
         "for (const auto &key : keys)",
         "erase_deferred_epoch_change(key)",
         "proposal_admission->retire_proposal(key)"}));

    REQUIRE_FALSE(reply.empty());
    CHECK(contains_in_order(
        reply,
        {"deferred_epoch_definition_recoveries.find(",
         "recovery == deferred_epoch_definition_recoveries.end()",
         "return;"}));

    REQUIRE_FALSE(destructor.empty());
    CHECK(contains_in_order(
        destructor,
        {"exact_runtime_access->close_and_wait()",
         "deferred_epoch_definition_recoveries.clear()",
         "deferred_epoch_change_proposal_count = 0",
         "proposal_contexts->shutdown()"}));
}

TEST_CASE("committed epoch history owns one coherent exact head snapshot",
          "[c08][epoch-change][pre-vote][committed-history]")
{
    const auto header = source("include/hotstuff/hotstuff.h");
    const auto implementation = source("src/hotstuff.cpp");
    const auto core = source("src/consensus.cpp");
    const auto constructor = function_body(
        implementation, "HotStuffBase::HotStuffBase(");
    const auto initialize = function_body(
        implementation,
        "HotStuffBase::initialize_committed_epoch_change_history(");
    const auto record = function_body(
        implementation,
        "HotStuffBase::record_committed_epoch_change_history(");
    const auto gate = function_body(
        implementation,
        "HotStuffBase::pre_vote_epoch_change_gate(");
    const auto consensus = function_body(
        implementation, "void HotStuffBase::do_consensus(");

    CHECK(contains_all(
        header,
        {"struct CommittedEpochChangeHistoryState",
         "block_t head;",
         "EpochChangeCommittedHistorySnapshot snapshot;",
         "committed_epoch_change_history;"}));
    REQUIRE_FALSE(constructor.empty());
    CHECK(constructor.find(
              "initialize_committed_epoch_change_history();") !=
          std::string::npos);

    REQUIRE_FALSE(initialize.empty());
    CHECK(contains_in_order(
        initialize,
        {"const auto &genesis = committed_head()",
         "genesis->get_decision()",
         "CommittedEpochChangeHistoryState{",
         "genesis",
         "EpochChangeCommittedHistorySnapshot{",
         "genesis->get_hash()",
         "genesis->get_height()"}));

    REQUIRE_FALSE(record.empty());
    CHECK(contains_in_order(
        record,
        {"const auto &previous = *committed_epoch_change_history",
         "parents.front() != previous.head",
         "extract_epoch_change_block_extra(",
         "committed_epoch_change_history =",
         "CommittedEpochChangeHistoryState{",
         "block",
         "EpochChangeCommittedHistorySnapshot{"}));

    REQUIRE_FALSE(gate.empty());
    CHECK(contains_in_order(
        gate,
        {"const auto &committed = committed_epoch_change_history",
         "*committed->head",
         "committed->snapshot"}));
    CHECK(gate.find("b_exec") == std::string::npos);
    CHECK(gate.find("committed_head()") == std::string::npos);

    REQUIRE_FALSE(consensus.empty());
    CHECK(contains_in_order(
        consensus,
        {"record_committed_epoch_change_history(blk)",
         "close_committed_block(blk->get_hash())"}));
    CHECK(contains_in_order(
        core,
        {"do_consensus(blk);", "b_exec = blk;"}));
}

TEST_CASE("adaptive v2 pre-vote authorization is pinned once",
          "[c08][epoch-change][pre-vote][configuration]")
{
    const auto implementation = source("src/hotstuff.cpp");
    const auto configure = function_body(
        implementation,
        "HotStuffBase::configure_epoch_change_pre_vote_gate(");

    REQUIRE_FALSE(configure.empty());
    CHECK(contains_in_order(
        configure,
        {"epoch_change_verifier != nullptr",
         "configuration is already pinned",
         "std::make_unique<EpochChangeVerifier>(",
         "epoch_change_verifier = std::move(verifier)"}));
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
