#include <cctype>
#include <cstddef>
#include <cstdint>
#include <fstream>
#include <map>
#include <mutex>
#include <optional>
#include <set>
#include <sstream>
#include <string>
#include <utility>
#include <vector>

#include "catch.hpp"
#include "hotstuff/configuration.h"
#include "hotstuff/promise.hpp"
#include "hotstuff/type.h"

#ifndef KAURI_PROJECT_SOURCE_DIR
#define KAURI_PROJECT_SOURCE_DIR "."
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

std::string read_optional_source(const std::string &relative_path)
{
    const std::string path =
        std::string(KAURI_PROJECT_SOURCE_DIR) + "/" + relative_path;
    std::ifstream input(path);
    if (!input.good())
        return {};
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

std::string without_whitespace(const std::string &source)
{
    std::string normalized;
    normalized.reserve(source.size());
    for (const unsigned char character : source)
        if (std::isspace(character) == 0)
            normalized.push_back(static_cast<char>(character));
    return normalized;
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

void check_absent(const std::string &source,
                  const std::string &forbidden,
                  const std::string &reason)
{
    INFO(reason);
    CHECK(source.find(forbidden) == std::string::npos);
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

void check_no_partial_block_accumulator(const std::string &source,
                                        const std::string &path)
{
    check_absent(
        source, "self_qc = create_quorum_cert",
        path + " must not install a fresh partial certificate on Block");
    check_absent(
        source, "self_qc.get()",
        path + " must not alias Block::self_qc for partial mutation");
    check_absent(
        source, "self_qc->add_part",
        path + " must add direct votes to the exact context accumulator");
    check_absent(
        source, "self_qc->merge_quorum",
        path + " must merge relays into the exact context accumulator");
    check_absent(
        source, "self_qc->compute",
        path + " must compute only the exact context accumulator");
}

} // namespace

TEST_CASE("fulfilled promises invoke continuations synchronously",
          "[rem-a06-02][promise][deadlock][control]")
{
    std::mutex runtime_mutex;
    bool callback_ran = false;
    bool callback_reacquired = true;

    std::lock_guard<std::mutex> outer_lock(runtime_mutex);
    promise::promise_t([&](promise::promise_t &fulfilled) {
        fulfilled.resolve(7);
    }).then([&](int) {
        callback_ran = true;
        callback_reacquired = runtime_mutex.try_lock();
        if (callback_reacquired)
            runtime_mutex.unlock();
    });

    INFO("the fulfilled callback must run inline while the outer lock is held");
    CHECK(callback_ran);
    CHECK_FALSE(callback_reacquired);
}

TEST_CASE("remote proposal protocol work runs outside the runtime mutex",
          "[rem-a06-02][production-wiring][deadlock][intentional-red]")
{
    const auto source = read_source("src/hotstuff.cpp");
    const auto active = source_slice(
        source,
        "void HotStuffBase::process_active",
        "void HotStuffBase::local_vote_authorized");
    const auto protocol = active.find("on_receive_proposal(parsed)");
    const auto mutex_access = active.find("access->mutex");

    INFO("recursive mutual exclusion would hide rather than fix protocol "
         "re-entry and teardown races");
    CHECK(active.find("recursive_mutex") == std::string::npos);
    REQUIRE(protocol != std::string::npos);
    if (mutex_access != std::string::npos)
    {
        const auto lock_scope_open = active.rfind('{', mutex_access);
        const auto lock_scope_close =
            matching_closing_brace(active, lock_scope_open);
        INFO("ExactRuntimeAccess may guard lifetime lookup, but its "
             "non-recursive mutex must be released before protocol code; "
             "an eagerly fulfilled beat_resp continuation re-enters it");
        REQUIRE(lock_scope_open != std::string::npos);
        REQUIRE(lock_scope_close != std::string::npos);
        CHECK(lock_scope_close < protocol);
    }
}

/*
 * These source-wiring checks stay live even while the new headers are absent.
 * They expose the legacy aliases that the new production seam must actually
 * replace; a green test-only active-epoch predicate cannot satisfy them.
 */
TEST_CASE("HotStuff aggregation ownership is not keyed by a bare block hash",
          "[rem-a06-02][production-wiring][proposal-key][intentional-red]")
{
    const auto header = read_source("include/hotstuff/hotstuff.h");

    check_absent(
        header,
        "pass_trought_blks",
        "pass-through state must live in a ProposalKey-owned runtime entry");
    check_absent(
        header,
        "pending_votes",
        "pending children must live in a ProposalKey-owned runtime entry");
    check_absent(
        header,
        "lat_start",
        "latency starts must include the complete exact ProposalKey");
    check_absent(
        header,
        "proposal_timers",
        "aggregation timers must be owned by ProposalKey plus generation");
}

TEST_CASE("vote and relay handlers delegate the exact-context gate",
          "[rem-a06-02][production-wiring][handlers][intentional-red]")
{
    const auto source = read_source("src/hotstuff.cpp");
    const auto direct = source_slice(
        source,
        "void HotStuffBase::vote_handler",
        "void HotStuffBase::vote_relay_handler");
    const auto relay = source_slice(
        source,
        "void HotStuffBase::vote_relay_handler",
        "void HotStuffBase::req_blk_handler");

    const auto direct_parse = direct.find("postponed_parse");
    const auto direct_tree = direct.find("find_exact_runtime_tree");
    const auto direct_adapter = direct.find("make_exact_direct_envelope");
    const auto direct_gate = direct.find("handle_direct");
    INFO("direct handling must parse and resolve the exact tree before the open-lease gate");
    CHECK(direct_parse != std::string::npos);
    CHECK(direct_tree != std::string::npos);
    CHECK(direct_adapter != std::string::npos);
    CHECK(direct_gate != std::string::npos);
    CHECK(direct_parse < direct_tree);
    CHECK(direct_tree < direct_adapter);
    CHECK(direct_adapter < direct_gate);

    const auto relay_parse = relay.find("postponed_parse");
    const auto relay_tree = relay.find("find_exact_runtime_tree");
    const auto relay_adapter = relay.find("make_exact_relay_envelope");
    const auto relay_gate = relay.find("handle_relay");
    INFO("relay handling must parse and resolve the exact tree before the open-lease gate");
    CHECK(relay_parse != std::string::npos);
    CHECK(relay_tree != std::string::npos);
    CHECK(relay_adapter != std::string::npos);
    CHECK(relay_gate != std::string::npos);
    CHECK(relay_parse < relay_tree);
    CHECK(relay_tree < relay_adapter);
    CHECK(relay_adapter < relay_gate);
    check_absent(
        direct,
        "coordinate_verified_delivery",
        "lease acquisition must precede crypto and delivery orchestration");
    check_absent(
        relay,
        "coordinate_verified_delivery",
        "lease acquisition must precede crypto and delivery orchestration");
}

TEST_CASE("Block owns only a cloned final QC never the partial accumulator",
          "[rem-a06-02][production-wiring][accumulator][intentional-red]")
{
    const auto hotstuff = read_source("src/hotstuff.cpp");
    const auto consensus = read_source("src/consensus.cpp");
    const auto direct = source_slice(
        hotstuff,
        "void HotStuffBase::vote_handler",
        "void HotStuffBase::vote_relay_handler");
    const auto relay = source_slice(
        hotstuff,
        "void HotStuffBase::vote_relay_handler",
        "void HotStuffBase::req_blk_handler");
    const auto local_vote = source_slice(
        hotstuff,
        "void HotStuffBase::do_vote",
        "void HotStuffBase::do_consensus");
    const auto timeout = read_optional_source("src/aggregation.cpp");
    const auto leader_local = source_slice(
        consensus,
        "Proposal HotStuffCore::process_block",
        "void HotStuffCore::on_receive_proposal");

    check_no_partial_block_accumulator(direct, "direct vote");
    check_no_partial_block_accumulator(relay, "aggregate relay");
    check_no_partial_block_accumulator(local_vote, "local vote");
    check_no_partial_block_accumulator(timeout, "timer");
    check_no_partial_block_accumulator(leader_local, "leader-local proposal");

    // Deliberately do not ban `blk->self_qc = final_qc->clone()`: Block may
    // retain a cloned, published final QC for commit/fetch semantics. Only
    // mutable partial accumulation and partial relay ownership are forbidden.
}

TEST_CASE("root QC publication uses atomic frozen-quorum eligibility",
          "[rem-a06-02][production-wiring][root][quorum]"
          "[a06-wiring-audit][control]")
{
    const auto source = read_source("src/hotstuff.cpp");
    const auto finish = without_whitespace(source_slice(
        source,
        "void HotStuffBase::try_finish_exact_context",
        "void HotStuffBase::local_vote_authorized"));
    const auto publication = without_whitespace(source_slice(
        source,
        "bool HotStuffBase::publish_exact_root_qc",
        "void HotStuffBase::drain_ready_piped_qcs"));

    const auto eligibility = finish.find(
        "proposal_contexts->clone_publishable_root_qc(lease)");
    const auto publish = finish.find("publish_exact_root_qc(");
    INFO("try_finish_exact_context must obtain an atomic candidate from the "
         "admitted ProposalContext before crossing the Block/HQC publication "
         "side-effect boundary");
    REQUIRE(eligibility != std::string::npos);
    REQUIRE(publish != std::string::npos);
    CHECK(eligibility < publish);

    check_absent(
        finish,
        "config.nmajority",
        "root eligibility must not read the mutable live configuration");
    check_absent(
        finish,
        "->has_n(",
        "root eligibility and exact cloning must be one lifecycle operation");
    check_absent(
        publication,
        "config.nmajority",
        "the publication side-effect boundary must not reintroduce a live "
        "threshold decision");
    check_absent(
        publication,
        "->has_n(",
        "the publication side-effect boundary accepts only a prequalified "
        "exact candidate");

    for (const std::string side_effect : {
             "self_qc", "update_hqc(", "on_qc_finish("})
    {
        const auto position = finish.find(side_effect);
        INFO("no Block/QC side effect may precede frozen eligibility: "
             << side_effect);
        CHECK((position == std::string::npos || eligibility < position));
    }
}

TEST_CASE("non-root completion cannot depend on mutable reputation",
          "[rem-a06-02][production-wiring][reputation]"
          "[a06-wiring-audit][control]")
{
    const auto source = read_source("src/hotstuff.cpp");
    const auto direct = source_slice(
        source,
        "void HotStuffBase::vote_handler",
        "void HotStuffBase::vote_relay_handler");
    const auto relay = source_slice(
        source,
        "void HotStuffBase::vote_relay_handler",
        "void HotStuffBase::req_blk_handler");

    check_absent(
        direct,
        "effective_required_votes",
        "direct-vote subtree completion must use the immutable admitted tree, "
        "not reputation-filtered children");
    check_absent(
        relay,
        "effective_required_votes",
        "relay subtree completion must use the immutable admitted tree, not "
        "reputation-filtered children");
}

TEST_CASE("remote and leader-local proposal paths open context before protocol work",
          "[rem-a06-02][production-wiring][proposal-order][intentional-red]")
{
    const auto hotstuff = read_source("src/hotstuff.cpp");
    const auto consensus = read_source("src/consensus.cpp");
    const auto remote = source_slice(
        hotstuff,
        "void HotStuffBase::process_active",
        "void HotStuffBase::local_vote_authorized");
    const auto leader_local = source_slice(
        consensus,
        "Proposal HotStuffCore::process_block",
        "void HotStuffCore::on_receive_proposal");

    const auto remote_delivery = remote.find("async_deliver_blk");
    const auto remote_open = remote.find("admit_exact_context");
    const auto remote_protocol = remote.find("on_receive_proposal");
    INFO("remote exact-context admission must occur after delivery but before "
         "proposal processing");
    CHECK(remote_delivery != std::string::npos);
    CHECK(remote_open != std::string::npos);
    CHECK(remote_protocol != std::string::npos);
    CHECK(remote_delivery < remote_open);
    CHECK(remote_open < remote_protocol);

    const auto local_open = leader_local.find("admit_local");
    const auto self_vote = leader_local.find("on_receive_vote");
    INFO("both normal and pipelined leader-local paths cross process_block; "
         "its exact context must open before the self-vote and later broadcast");
    CHECK(local_open != std::string::npos);
    CHECK(self_vote != std::string::npos);
    CHECK(local_open < self_vote);
}

TEST_CASE("remote protocol acceptance initializes aggregation before drain",
          "[a06][production-wiring][non-voting][aggregation-timeout]"
          "[a06-wiring-audit][control]")
{
    const auto hotstuff = read_source("src/hotstuff.cpp");
    const auto admission = read_source("src/proposal_admission.cpp");
    const auto exact_admission = source_slice(
        hotstuff,
        "HotStuffBase::admit_exact_context",
        "void HotStuffBase::activate_proposal_configuration");
    const auto active = source_slice(
        hotstuff,
        "void HotStuffBase::process_active",
        "void HotStuffBase::local_vote_authorized");
    const auto authorize = source_slice(
        admission,
        "bool ProposalAdmissionCoordinator::authorize_local_vote",
        "const ConfigurationId &");

    const auto delivery = active.find("async_deliver_blk");
    const auto admitted = exact_admission.find("admit_remote");
    const auto accumulator = exact_admission.find("initialize_accumulator");
    const auto admission_call = active.find("admit_exact_context");
    const auto child_state = active.find("create_expected_vote_state");
    const auto latency = active.find("start_latency_deadline");
    const auto deadline = active.find("start_aggregation_timer");
    const auto protocol = active.find("on_receive_proposal(parsed)");
    const auto still_open = active.find(
        "acquire_open_context(metadata.key)", protocol);
    const auto drain = active.find(
        "drain_pending_exact_contributions(metadata.key)");

    INFO("a delivered active proposal opens its accumulator before HotStuff "
         "processing, then arms child timing only if protocol processing "
         "leaves the exact context open");
    CHECK(delivery != std::string::npos);
    CHECK(admitted != std::string::npos);
    CHECK(accumulator != std::string::npos);
    CHECK(admission_call != std::string::npos);
    CHECK(protocol != std::string::npos);
    CHECK(still_open != std::string::npos);
    CHECK(child_state != std::string::npos);
    CHECK(latency != std::string::npos);
    CHECK(deadline != std::string::npos);
    CHECK(drain != std::string::npos);
    if (delivery != std::string::npos &&
        admitted != std::string::npos &&
        accumulator != std::string::npos &&
        admission_call != std::string::npos &&
        protocol != std::string::npos &&
        still_open != std::string::npos &&
        child_state != std::string::npos &&
        latency != std::string::npos &&
        deadline != std::string::npos &&
        drain != std::string::npos)
    {
        CHECK(admitted < accumulator);
        CHECK(delivery < admission_call);
        CHECK(admission_call < protocol);
        CHECK(protocol < still_open);
        CHECK(still_open < child_state);
        CHECK(child_state < latency);
        CHECK(latency < deadline);
        CHECK(deadline < drain);
    }

    INFO("local vote authorization owns only the optional local contribution; "
         "it must not own child accounting or aggregation timing");
    CHECK(authorize.find("local_vote_authorized") != std::string::npos);
    check_absent(
        authorize,
        "create_expected_vote_state",
        "child accounting starts after exact context admission, not signing");
    check_absent(
        authorize,
        "start_latency_deadline",
        "latency tracking starts after exact context admission, not signing");
    check_absent(
        authorize,
        "start_aggregation_timer",
        "the aggregation deadline starts even when no local vote is cast");
}

TEST_CASE("live aggregation timeout has no signing or leader-rotation capability",
          "[a06][a06-wiring-audit][production-wiring][aggregation-timeout]"
          "[intentional-red]")
{
    const auto aggregation = read_optional_source("src/aggregation.cpp");
    const auto header = read_optional_source("include/hotstuff/aggregation.h");
    const auto hotstuff = read_source("src/hotstuff.cpp");
    const std::string exact_timeout_marker =
        "void HotStuffBase::on_timer_expired(\n"
        "        const ProposalContextLease &lease";
    std::string live_adapter;
    if (hotstuff.find(exact_timeout_marker) != std::string::npos)
        live_adapter = source_slice(
            hotstuff, exact_timeout_marker, "void HotStuffBase::change_epoch");

    INFO("A06 requires a narrow timeout coordinator whose effects can only "
         "send already accepted signatures upward and report missing children");
    CHECK(header.find("class AggregationTimeoutCoordinator") !=
          std::string::npos);
    CHECK(header.find("AggregationTimeoutEffects") != std::string::npos);
    CHECK(aggregation.find(
              "AggregationTimeoutCoordinator::dispatch_timeout") !=
          std::string::npos);

    for (const std::string forbidden : {
             "create_part_cert",
             "record_local_part",
             "priv_key",
             "pmaker",
             "inc_time",
             "rotate",
             "impeach"})
    {
        check_absent(
            header + aggregation + live_adapter,
            forbidden,
            "the live aggregation-timeout surface cannot sign, suspect, or "
            "rotate a leader: " + forbidden);
    }
}

TEST_CASE("dead signing timeout and reputation-adjusted vote requirements are retired",
          "[a06][a06-wiring-audit][production-wiring][legacy-retirement]"
          "[intentional-red]")
{
    const auto header = read_source("include/hotstuff/hotstuff.h");
    const auto source = read_source("src/hotstuff.cpp");

    check_absent(
        source,
        "create_partial_vote_relay",
        "even disabled timeout code that can manufacture a local signature "
        "must be deleted rather than left available for resurrection");
    check_absent(
        header,
        "effective_required_votes",
        "the reputation-adjusted vote requirement must not remain declared");
    check_absent(
        source,
        "effective_required_votes",
        "the reputation-adjusted requirement definition and every call site "
        "must be removed");
}

TEST_CASE("aggregation timing arms an exact lifecycle lease and includes roots",
          "[a06][a06-wiring-audit][production-wiring][aggregation-timer]"
          "[intentional-red]")
{
    const auto header = read_source("include/hotstuff/hotstuff.h");
    const auto source = read_source("src/hotstuff.cpp");
    const auto timer = source_slice(
        source,
        "void HotStuffBase::start_aggregation_timer",
        "void HotStuffBase::emit_timeout_report");
    const auto normalized = without_whitespace(timer);

    INFO("the admitted ProposalContextLease is the only aggregation identity "
         "passed to the state-free timeout coordinator");
    CHECK(header.find("AggregationTimeoutPolicy") != std::string::npos);
    CHECK(header.find("AggregationTimeoutCoordinator") != std::string::npos);
    CHECK(timer.find("acquire_open_context(key)") != std::string::npos);
    CHECK(timer.find("direct_children.empty()") != std::string::npos);
    CHECK(timer.find("arm_timeout") != std::string::npos);
    CHECK(normalized.find("arm_timeout(*lease") != std::string::npos);
    check_absent(
        timer,
        "parent.has_value()",
        "roots have children and must own an aggregation deadline even though "
        "they never send upward");
    check_absent(
        timer,
        "start_proposal_timer",
        "the live path must not reconstruct exact identity from epoch/tree/hash "
        "integer arguments");
}

TEST_CASE("leader-local broadcast initializes child timing exactly once",
          "[a06][a06-wiring-audit][production-wiring][leader-local]"
          "[intentional-red]")
{
    const auto hotstuff = read_source("src/hotstuff.cpp");
    const auto consensus = read_source("src/consensus.cpp");
    const auto local_proposal = source_slice(
        consensus,
        "block_t HotStuffCore::on_propose",
        "Proposal HotStuffCore::process_block");
    const auto broadcast = source_slice(
        hotstuff,
        "void HotStuffBase::do_broadcast_proposal",
        "void HotStuffBase::inc_time");

    const auto process = local_proposal.find("process_block(");
    const auto invoke_broadcast =
        local_proposal.find("do_broadcast_proposal(prop)");
    REQUIRE(process != std::string::npos);
    REQUIRE(invoke_broadcast != std::string::npos);
    CHECK(process < invoke_broadcast);

    const auto open = broadcast.find("acquire_open_context(prop.key())");
    const auto expected = broadcast.find("create_expected_vote_state");
    const auto latency = broadcast.find("start_latency_deadline");
    const auto timer = broadcast.find("start_aggregation_timer");
    const auto child_sends = broadcast.find("for (const auto child");
    REQUIRE(open != std::string::npos);
    REQUIRE(child_sends != std::string::npos);
    CHECK(count_occurrences(broadcast, "create_expected_vote_state") == 1);
    CHECK(count_occurrences(broadcast, "start_latency_deadline") == 1);
    CHECK(count_occurrences(broadcast, "start_aggregation_timer") == 1);
    check_absent(
        broadcast,
        "record_latency_start",
        "leader-local latency initialization must use the same one-shot exact "
        "context boundary as remote admission");
    if (expected != std::string::npos && latency != std::string::npos &&
        timer != std::string::npos)
    {
        CHECK(open < expected);
        CHECK(expected < latency);
        CHECK(latency < timer);
        CHECK(timer < child_sends);
    }
}

TEST_CASE("exact fallback stages repair and fast-returns root repair votes",
          "[fallback][production-wiring][proposal][vote][bounded]")
{
    const auto source = read_source("src/hotstuff.cpp");
    const auto broadcast = source_slice(
        source,
        "void HotStuffBase::do_broadcast_proposal",
        "void HotStuffBase::inc_time");
    const auto proposal_schedule = source_slice(
        source,
        "void HotStuffBase::schedule_exact_proposal_fallback",
        "void HotStuffBase::dispatch_exact_proposal_fallback");
    const auto proposal_dispatch = source_slice(
        source,
        "void HotStuffBase::dispatch_exact_proposal_fallback",
        "bool HotStuffBase::broadcast_exact_proposal_fallback");
    const auto proposal_send = source_slice(
        source,
        "bool HotStuffBase::broadcast_exact_proposal_fallback",
        "void HotStuffBase::discard_exact_fallbacks");
    const auto vote_schedule = source_slice(
        source,
        "void HotStuffBase::schedule_exact_vote_fallback",
        "void HotStuffBase::observe_exact_root_repair_delivery");
    const auto repair_delivery = source_slice(
        source,
        "void HotStuffBase::observe_exact_root_repair_delivery",
        "void HotStuffBase::dispatch_exact_vote_fallback");
    const auto vote_dispatch = source_slice(
        source,
        "void HotStuffBase::dispatch_exact_vote_fallback",
        "bool HotStuffBase::send_exact_vote_to_root");
    const auto vote_send = source_slice(
        source,
        "bool HotStuffBase::send_exact_vote_to_root",
        "void HotStuffBase::schedule_exact_proposal_fallback");
    const auto purge = source_slice(
        source,
        "void HotStuffBase::purge_pending_exact_contributions",
        "promise_t HotStuffBase::deliver_exact_contribution");
    const auto active_proposal = source_slice(
        source,
        "void HotStuffBase::process_active",
        "promise_t HotStuffBase::verify_exact_contribution");
    const auto fallback_cleanup = source_slice(
        source,
        "void HotStuffBase::discard_exact_fallbacks",
        "void HotStuffBase::cancel_all_exact_fallbacks");
    const auto fallback_shutdown = source_slice(
        source,
        "void HotStuffBase::cancel_all_exact_fallbacks",
        "quorum_cert_bt HotStuffBase::verified_aggregation_candidate");
    const auto proposal_ingress = source_slice(
        source,
        "void HotStuffBase::adaptive_propose_handler",
        "void HotStuffBase::adaptive_vote_handler");

    const auto primary = broadcast.find(
        "for (const auto child : metadata->tree.direct_children)");
    const auto fallback = broadcast.find(
        "schedule_exact_proposal_fallback");
    REQUIRE(primary != std::string::npos);
    REQUIRE(fallback != std::string::npos);
    CHECK(primary < fallback);

    for (const auto &schedule : {proposal_schedule, vote_schedule})
    {
        CHECK(schedule.find("timeout_for(") != std::string::npos);
        CHECK(schedule.find("tree->get_max_level()") !=
              std::string::npos);
        CHECK(schedule.find("schedule_after(") != std::string::npos);
    }
    CHECK(proposal_dispatch.find("acquire_open_context(key)") !=
          std::string::npos);
    CHECK(proposal_dispatch.find("may_drain_exact_context(") !=
          std::string::npos);
    CHECK(proposal_dispatch.find("*active != key.configuration") ==
          std::string::npos);
    CHECK(proposal_dispatch.find("admits_new_proposals()") !=
          std::string::npos);
    CHECK(proposal_send.find(
              "while (target_cursor < targets.size()") !=
          std::string::npos);
    CHECK(proposal_send.find("send_attempts < maximum_attempts") !=
          std::string::npos);
    CHECK(proposal_send.find("!quorum_observed") != std::string::npos);
    CHECK(proposal_send.find("targets[target_cursor++]") !=
          std::string::npos);
    const auto snapshot = proposal_send.find(
        "proposal_contexts->snapshot(proposal.key())");
    const auto skip_verified = proposal_send.find(
        "snapshot->verified_signers.count(member) != 0");
    const auto proposal_send_attempt = proposal_send.find(
        "pn.send_msg(");
    const auto record_attempt = proposal_send.find(
        "attempted_targets.push_back(member)");
    const auto charge_attempt = proposal_send.find(
        "++total_send_attempts");
    REQUIRE(snapshot != std::string::npos);
    REQUIRE(skip_verified != std::string::npos);
    REQUIRE(record_attempt != std::string::npos);
    REQUIRE(charge_attempt != std::string::npos);
    REQUIRE(proposal_send_attempt != std::string::npos);
    CHECK(snapshot < skip_verified);
    CHECK(skip_verified < proposal_send_attempt);
    CHECK(record_attempt < proposal_send_attempt);
    CHECK(charge_attempt < proposal_send_attempt);
    CHECK(proposal_send.find("skipped_verified") != std::string::npos);
    CHECK(proposal_schedule.find("stage_target_limit") !=
          std::string::npos);
    CHECK(proposal_schedule.find("stage_interval") !=
          std::string::npos);
    CHECK(proposal_dispatch.find("frozen_global_quorum") !=
          std::string::npos);
    CHECK(proposal_dispatch.find(
              "schedule_after(\n                job->stage_interval") !=
          std::string::npos);
    CHECK(proposal_dispatch.find(
              "std::numeric_limits<std::size_t>::max()") ==
          std::string::npos);
    CHECK(proposal_dispatch.find(
              "broadcast_exact_proposal_fallback(") ==
          std::string::npos);
    const auto repair_marker = vote_schedule.find(
        "exact_root_repair_deliveries.find(lease.key())");
    const auto fast_return = vote_schedule.find("send_exact_vote_to_root(");
    const auto delayed_return = vote_schedule.find("schedule_after(");
    REQUIRE(repair_marker != std::string::npos);
    REQUIRE(fast_return != std::string::npos);
    REQUIRE(delayed_return != std::string::npos);
    CHECK(repair_marker < fast_return);
    CHECK(fast_return < delayed_return);
    CHECK(vote_schedule.find("if (sent)\n                return;") !=
          std::string::npos);

    CHECK(repair_delivery.find("active != key.configuration") !=
          std::string::npos);
    CHECK(repair_delivery.find(
              "*generation != envelope.view_generation") !=
          std::string::npos);
    CHECK(repair_delivery.find("exact_context_metadata(key)") !=
          std::string::npos);
    CHECK(repair_delivery.find("contains_admitted(key)") !=
          std::string::npos);
    CHECK(repair_delivery.find(
              "authenticated_sender != metadata->tree.root") !=
          std::string::npos);
    CHECK(repair_delivery.find(
              "*metadata->tree.parent == authenticated_sender") !=
          std::string::npos);
    CHECK(repair_delivery.find(
              "found == exact_vote_fallback_jobs.end()") !=
          std::string::npos);
    CHECK(repair_delivery.find(
              "status == ProposalContextStatus::terminal_closed") !=
          std::string::npos);
    CHECK(repair_delivery.find(
              "status == ProposalContextStatus::retired") !=
          std::string::npos);
    CHECK(repair_delivery.find(
              "snapshot->verified_signers.count(get_id())") !=
          std::string::npos);
    CHECK(repair_delivery.find(
              "exact_root_repair_deliveries.insert_or_assign(") !=
          std::string::npos);
    CHECK(repair_delivery.find("acquire_open_context(key)") ==
          std::string::npos);
    CHECK(repair_delivery.find("job->key != key") !=
          std::string::npos);
    CHECK(repair_delivery.find("send_exact_vote_to_root(") !=
          std::string::npos);
    CHECK(repair_delivery.find("exact_vote_fallback_jobs.erase(found)") !=
          std::string::npos);
    CHECK(repair_delivery.find("cancellation()") != std::string::npos);

    CHECK(proposal_ingress.find(
              "ProposalDisposition::admitted_active") !=
          std::string::npos);
    CHECK(proposal_ingress.find("ProposalDisposition::duplicate") !=
          std::string::npos);
    CHECK(proposal_ingress.find(
              "observe_exact_root_repair_delivery(") !=
          std::string::npos);
    CHECK(proposal_ingress.find("ProposalDisposition::buffered_future") ==
          std::string::npos);
    CHECK(vote_dispatch.find("contains_admitted(key)") !=
          std::string::npos);
    CHECK(vote_dispatch.find("*active != key.configuration") !=
          std::string::npos);
    CHECK(vote_send.find("ReplicaID root") != std::string::npos);
    CHECK(vote_send.find("config.get_peer_id(root)") !=
          std::string::npos);
    CHECK(vote_send.find("MsgVote") != std::string::npos);
    CHECK(purge.find("preserve_scheduled_vote_fallback") !=
          std::string::npos);
    CHECK(purge.find("discard_exact_fallbacks(") !=
          std::string::npos);
    CHECK(purge.find("exact_root_repair_deliveries.erase(key)") !=
          std::string::npos);
    CHECK(active_proposal.find(
              "metadata.key, true") != std::string::npos);
    CHECK(fallback_cleanup.find(
              "!preserve_scheduled_vote_fallback") !=
          std::string::npos);
    CHECK(fallback_cleanup.find(
              "exact_proposal_fallback_jobs.find(key)") !=
          std::string::npos);
    CHECK(fallback_shutdown.find("exact_root_repair_deliveries.clear()") !=
          std::string::npos);
}

TEST_CASE("root quorum arms a bounded confirmation repair tail before closure",
          "[fallback][proposal][confirmation-tail][race][bounded]")
{
    const auto source = read_source("src/hotstuff.cpp");
    const auto finish = source_slice(
        source,
        "void HotStuffBase::try_finish_exact_context",
        "void HotStuffBase::local_vote_authorized");
    const auto arm = source_slice(
        source,
        "void HotStuffBase::arm_exact_proposal_repair_tail",
        "void HotStuffBase::dispatch_exact_proposal_repair_tail");
    const auto first_pass = source_slice(
        source,
        "void HotStuffBase::dispatch_exact_proposal_fallback",
        "void HotStuffBase::arm_exact_proposal_repair_tail");

    const auto arm_tail = finish.find(
        "arm_exact_proposal_repair_tail(lease)");
    const auto publish = finish.find("publish_exact_root_qc(");
    const auto close = finish.find(
        "ProposalContextEvent::root_qc_published");
    REQUIRE(arm_tail != std::string::npos);
    REQUIRE(publish != std::string::npos);
    REQUIRE(close != std::string::npos);
    INFO("the immutable tail must be captured before QC callbacks or terminal "
         "compaction can purge the signer snapshot");
    CHECK(arm_tail < publish);
    CHECK(publish < close);

    CHECK(arm.find("proposal_contexts->snapshot(job->key)") !=
          std::string::npos);
    CHECK(arm.find("snapshot->verified_signers.size() < job->global_quorum") !=
          std::string::npos);
    CHECK(arm.find("lease.tree().assigned_subtree") != std::string::npos);
    CHECK(arm.find("job->attempted_targets") != std::string::npos);
    CHECK(arm.find("snapshot->verified_signers.count(member) != 0") !=
          std::string::npos);
    CHECK(arm.find("job->tail_targets.push_back(member)") !=
          std::string::npos);
    CHECK(arm.find("job->total_attempt_budget - job->total_send_attempts") !=
          std::string::npos);
    CHECK(arm.find("schedule_after(") != std::string::npos);

    INFO("a quorum observed after polling a fresh dispatch must still find "
         "the registered immutable job before further reservations");
    CHECK(first_pass.find(
              "exact_proposal_fallback_jobs.erase(found)") ==
          std::string::npos);
    const auto refreshed = first_pass.find(
        "const auto refreshed = proposal_contexts->snapshot(key)");
    const auto raced_tail = first_pass.find(
        "arm_exact_proposal_repair_tail(*lease)", refreshed);
    const auto reserve = first_pass.find(
        "reserve_exact_proposal_pre_quorum_refresh(", raced_tail);
    REQUIRE(refreshed != std::string::npos);
    REQUIRE(raced_tail != std::string::npos);
    REQUIRE(reserve != std::string::npos);
    CHECK(refreshed < raced_tail);
    CHECK(raced_tail < reserve);
    CHECK(first_pass.find("broadcast_exact_proposal_fallback(") ==
          std::string::npos);
    CHECK(first_pass.find("dispatch_exact_proposal_repair_tail(") ==
          std::string::npos);
    const auto observed = arm.find("job->quorum_observed = true");
    const auto candidates = arm.find("job->tail_targets.push_back(member)");
    REQUIRE(observed != std::string::npos);
    REQUIRE(candidates != std::string::npos);
    CHECK(observed < candidates);
}

TEST_CASE("confirmation repair tail shares the original send budget and cancels",
          "[fallback][proposal][confirmation-tail][budget][generation]")
{
    const auto source = read_source("src/hotstuff.cpp");
    const auto job = source_slice(
        source,
        "struct HotStuffBase::ExactProposalFallbackJob",
        "class HotStuffBase::ExactContributionEffects");
    const auto tail_dispatch = source_slice(
        source,
        "void HotStuffBase::dispatch_exact_proposal_repair_tail",
        "bool HotStuffBase::broadcast_exact_proposal_fallback");
    const auto tail_send = source_slice(
        source,
        "bool HotStuffBase::broadcast_exact_proposal_repair_tail",
        "void HotStuffBase::discard_exact_fallbacks");
    const auto cleanup = source_slice(
        source,
        "void HotStuffBase::discard_exact_fallbacks",
        "void HotStuffBase::cancel_all_exact_fallbacks");
    const auto shutdown = source_slice(
        source,
        "void HotStuffBase::cancel_all_exact_fallbacks",
        "quorum_cert_bt HotStuffBase::verified_aggregation_candidate");

    CHECK(job.find("const std::size_t total_attempt_budget") !=
          std::string::npos);
    CHECK(job.find("std::size_t total_send_attempts{0}") !=
          std::string::npos);
    CHECK(job.find("std::vector<ReplicaID> attempted_targets") !=
          std::string::npos);
    CHECK(job.find("std::vector<ReplicaID> tail_targets") !=
          std::string::npos);
    CHECK(job.find("std::size_t tail_target_cursor{0}") !=
          std::string::npos);
    CHECK(job.find("bool quorum_observed{false}") !=
          std::string::npos);

    const auto may_drain = tail_dispatch.find(
        "may_drain_exact_context(");
    const auto generation = tail_dispatch.find(
        "*generation != job->epoch_generation");
    const auto send = tail_dispatch.find(
        "broadcast_exact_proposal_repair_tail(");
    REQUIRE(may_drain != std::string::npos);
    REQUIRE(generation != std::string::npos);
    REQUIRE(send != std::string::npos);
    CHECK(may_drain < send);
    CHECK(generation < send);
    CHECK(tail_dispatch.find("found->second != job") !=
          std::string::npos);
    CHECK(tail_dispatch.find("admits_new_proposals()") !=
          std::string::npos);
    CHECK(tail_dispatch.find("acquire_open_context(") ==
          std::string::npos);
    CHECK(tail_dispatch.find("frozen_global_quorum(") ==
          std::string::npos);
    CHECK(tail_dispatch.find(
              "job->tail_target_cursor == cursor_before") !=
          std::string::npos);
    CHECK(tail_dispatch.find(
              "job->total_send_attempts == attempts_before") !=
          std::string::npos);

    CHECK(tail_send.find(
              "job.tail_target_cursor < job.tail_targets.size()") !=
          std::string::npos);
    CHECK(tail_send.find(
              "job.total_send_attempts < job.total_attempt_budget") !=
          std::string::npos);
    CHECK(tail_send.find(
              "send_attempts < job.stage_target_limit") !=
          std::string::npos);
    CHECK(tail_send.find(
              "job.tail_targets[job.tail_target_cursor++]") !=
          std::string::npos);
    CHECK(tail_send.find("++job.total_send_attempts") !=
          std::string::npos);
    const auto tail_charge = tail_send.find(
        "++job.total_send_attempts");
    const auto tail_enqueue = tail_send.find("pn.send_msg(");
    REQUIRE(tail_charge != std::string::npos);
    REQUIRE(tail_enqueue != std::string::npos);
    CHECK(tail_charge < tail_enqueue);
    CHECK(tail_send.find("try_finish_exact_context") ==
          std::string::npos);
    CHECK(tail_send.find("publish_exact_root_qc") ==
          std::string::npos);

    INFO("ordinary terminal cleanup preserves only an already-armed tail; "
         "shutdown still cancels every outstanding job");
    CHECK(cleanup.find("!proposal->second->tail_armed") !=
          std::string::npos);
    CHECK(shutdown.find("exact_proposal_fallback_jobs.clear()") !=
          std::string::npos);
}

TEST_CASE("pre-QC repair preserves live paths and dispatches by peer identity",
          "[fallback][proposal][pre-qc-repair][connection-repair]"
          "[budget][bounded]")
{
    const auto source = read_source("src/hotstuff.cpp");
    const auto transport = read_source(
        "salticidae/include/salticidae/network.h");
    const auto job = source_slice(
        source,
        "struct HotStuffBase::ExactProposalFallbackJob",
        "class HotStuffBase::ExactContributionEffects");
    const auto arming = source_slice(
        source,
        "void HotStuffBase::schedule_exact_proposal_fallback",
        "void HotStuffBase::dispatch_exact_proposal_fallback");
    const auto dispatch = source_slice(
        source,
        "void HotStuffBase::dispatch_exact_proposal_fallback",
        "void HotStuffBase::arm_exact_proposal_repair_tail");
    const auto tail_arm = source_slice(
        source,
        "void HotStuffBase::arm_exact_proposal_repair_tail",
        "void HotStuffBase::dispatch_exact_proposal_repair_tail");
    const auto tail_dispatch = source_slice(
        source,
        "void HotStuffBase::dispatch_exact_proposal_repair_tail",
        "bool HotStuffBase::broadcast_exact_proposal_fallback");
    const auto refresh = source_slice(
        source,
        "bool HotStuffBase::reserve_exact_proposal_pre_quorum_refresh",
        "bool HotStuffBase::broadcast_exact_proposal_pre_quorum_retry");
    const auto retry_send = source_slice(
        source,
        "bool HotStuffBase::broadcast_exact_proposal_pre_quorum_retry",
        "bool HotStuffBase::broadcast_exact_proposal_repair_tail");

    CHECK(job.find("std::vector<ReplicaID> pre_quorum_retry_targets") !=
          std::string::npos);
    CHECK(job.find("std::size_t pre_quorum_retry_cursor{0}") !=
          std::string::npos);
    CHECK(job.find("struct PendingPreQuorumRefresh") !=
          std::string::npos);
    CHECK(job.find("Net::conn_t old_connection") == std::string::npos);
    CHECK(job.find("observation_deadline") == std::string::npos);
    CHECK(job.find("refresh_observation_window") == std::string::npos);
    CHECK(job.find("pending_pre_quorum_refresh_batch") !=
          std::string::npos);

    INFO("the first repair deadline freezes a deduplicated missing candidate "
         "list and does not run the old blind send pass");
    const auto missing = dispatch.find("std::set<ReplicaID> missing_members");
    const auto assigned = dispatch.find(
        "lease->tree().assigned_subtree", missing);
    const auto skip_verified = dispatch.find(
        "before->verified_signers.count(member) == 0", assigned);
    const auto deduplicate = dispatch.find(
        "missing_members.insert(member).second", skip_verified);
    const auto append = dispatch.find(
        "job->pre_quorum_retry_targets.push_back(member)", deduplicate);
    const auto fresh_first = dispatch.find("reason=fresh_first", append);
    REQUIRE(missing != std::string::npos);
    REQUIRE(assigned != std::string::npos);
    REQUIRE(skip_verified != std::string::npos);
    REQUIRE(deduplicate != std::string::npos);
    REQUIRE(append != std::string::npos);
    REQUIRE(fresh_first != std::string::npos);
    CHECK(dispatch.find("broadcast_exact_proposal_fallback(") ==
          std::string::npos);

    INFO("repair remains at the root-aware deadline with the fixed N-1 "
         "application-send budget");
    CHECK(arming.find("target_count") != std::string::npos);
    CHECK(arming.find("schedule_after(\n                delay") !=
          std::string::npos);
    CHECK(job.find("const std::size_t total_attempt_budget") !=
          std::string::npos);

    INFO("active or immediately draining exact contexts may finish repair, "
         "while blocked admission and generation mismatches remain closed");
    const auto may_drain = dispatch.find("may_drain_exact_context(");
    const auto generation = dispatch.find(
        "*generation != job->epoch_generation", may_drain);
    const auto admission = dispatch.find("admits_new_proposals()", generation);
    REQUIRE(may_drain != std::string::npos);
    REQUIRE(generation != std::string::npos);
    REQUIRE(admission != std::string::npos);
    CHECK(dispatch.find("*active != key.configuration") ==
          std::string::npos);
    CHECK(tail_dispatch.find("may_drain_exact_context(") !=
          std::string::npos);
    CHECK(tail_dispatch.find("admits_new_proposals()") !=
          std::string::npos);

    INFO("each stage dispatches old reservations, reserves up to fanout new "
         "candidates, and immediately dispatches those new reservations");
    const auto poll = dispatch.find(
        "broadcast_exact_proposal_pre_quorum_retry(");
    const auto refreshed = dispatch.find(
        "const auto refreshed = proposal_contexts->snapshot(key)", poll);
    const auto remaining = dispatch.find(
        "job->total_attempt_budget - job->total_send_attempts", refreshed);
    const auto stage_cap = dispatch.find(
        "job->stage_target_limit, remaining_budget", remaining);
    const auto reserve_call = dispatch.find(
        "reserve_exact_proposal_pre_quorum_refresh(", stage_cap);
    const auto immediate_dispatch = dispatch.find(
        "broadcast_exact_proposal_pre_quorum_retry(", reserve_call);
    REQUIRE(poll != std::string::npos);
    REQUIRE(refreshed != std::string::npos);
    REQUIRE(remaining != std::string::npos);
    REQUIRE(stage_cap != std::string::npos);
    REQUIRE(reserve_call != std::string::npos);
    REQUIRE(immediate_dispatch != std::string::npos);
    const auto refresh_loop = refresh.find("std::size_t refresh_attempts");
    REQUIRE(refresh_loop != std::string::npos);
    CHECK(refresh.substr(0, refresh_loop).find(
              "pending_pre_quorum_refresh_batch") ==
          std::string::npos);
    CHECK(refresh.find("refresh_attempts < job.stage_target_limit") !=
          std::string::npos);
    CHECK(refresh.find("refresh_attempts < maximum_attempts") !=
          std::string::npos);

    INFO("reservation is cursor-monotone, preserves healthy connections, "
         "joins existing reconnects, and retains tail accounting");
    const auto capture = refresh.find(
        "const auto current_connection = pn.get_peer_conn(peer)");
    const auto request_required = refresh.find(
        "const bool reconnect_request_required =", capture);
    const auto reconnecting = refresh.find(
        "const bool reconnect_in_progress =", request_required);
    const auto reserve = refresh.find(
        "job.pending_pre_quorum_refresh_batch.push_back(", reconnecting);
    const auto tail_candidate = refresh.find(
        "job.attempted_targets.push_back(member)", reserve);
    const auto reconnect_guard = refresh.find(
        "if (reconnect_request_required)", tail_candidate);
    const auto request = refresh.find("pn.conn_peer(peer)", reconnect_guard);
    REQUIRE(capture != std::string::npos);
    REQUIRE(request_required != std::string::npos);
    REQUIRE(reconnecting != std::string::npos);
    REQUIRE(reserve != std::string::npos);
    REQUIRE(tail_candidate != std::string::npos);
    REQUIRE(reconnect_guard != std::string::npos);
    REQUIRE(request != std::string::npos);
    CHECK(reconnect_guard < request);
    CHECK(refresh.find(
              "current_connection == nullptr", request_required) !=
          std::string::npos);
    CHECK(refresh.find(
              "current_connection->is_terminated()", reconnecting) !=
          std::string::npos);
    CHECK(refresh.find("!old_terminated") == std::string::npos);
    CHECK(refresh.find("pn.conn_peer(peer,") == std::string::npos);
    CHECK(refresh.find("++job.total_send_attempts") == std::string::npos);
    INFO("the post-quorum tail spends its remaining budget on fresh fallback "
         "targets before retrying an uncertain path");
    const auto attempted_set = tail_arm.find(
        "std::set<ReplicaID> attempted_targets(");
    const auto assigned_set = tail_arm.find(
        "std::set<ReplicaID> assigned_targets(", attempted_set);
    const auto fresh_members = tail_arm.find(
        "for (const auto member : lease.tree().assigned_subtree)",
        assigned_set);
    const auto fresh_guard = tail_arm.find(
        "attempted_targets.count(member) == 0", fresh_members);
    const auto fresh_queue = tail_arm.find(
        "queue_tail_target(member)", fresh_guard);
    const auto retry_members = tail_arm.find(
        "for (const auto member : job->attempted_targets)", fresh_queue);
    const auto retry_queue = tail_arm.find(
        "queue_tail_target(member)", retry_members);
    REQUIRE(attempted_set != std::string::npos);
    REQUIRE(assigned_set != std::string::npos);
    REQUIRE(fresh_members != std::string::npos);
    REQUIRE(fresh_guard != std::string::npos);
    REQUIRE(fresh_queue != std::string::npos);
    REQUIRE(retry_members != std::string::npos);
    REQUIRE(retry_queue != std::string::npos);
    CHECK(fresh_queue < retry_queue);
    CHECK(tail_arm.find("std::set<ReplicaID> queued_targets") !=
          std::string::npos);
    CHECK(tail_arm.find("assigned_targets.count(member) == 0") !=
          std::string::npos);
    CHECK(tail_arm.find("job->tail_targets.push_back(member)") !=
          std::string::npos);
    CHECK(tail_arm.find("fresh_candidates=%zu") != std::string::npos);
    CHECK(tail_arm.find("retry_candidates=%zu") != std::string::npos);

    INFO("live and reconnecting paths dispatch by PeerId immediately, while "
         "the N-1 budget is charged directly before the deferred send");
    CHECK(retry_send.find("const bool reconnect_path_observed =") !=
          std::string::npos);
    CHECK(retry_send.find("connection_changed") == std::string::npos);
    CHECK(retry_send.find("replacement_ready") == std::string::npos);
    CHECK(retry_send.find("refresh_wait") == std::string::npos);
    CHECK(retry_send.find("refresh_timeout_no_dispatch") ==
          std::string::npos);
    const auto budget_guard = retry_send.find(
        "job.total_send_attempts >= job.total_attempt_budget");
    const auto charge = retry_send.find(
        "++job.total_send_attempts", budget_guard);
    const auto deferred = retry_send.find("pn.send_msg_deferred(", charge);
    REQUIRE(budget_guard != std::string::npos);
    REQUIRE(charge != std::string::npos);
    REQUIRE(deferred != std::string::npos);
    CHECK(charge < deferred);
    CHECK(retry_send.find(
              "MsgPropose(DataStream(encoded)), peer", deferred) !=
          std::string::npos);
    CHECK(retry_send.find("reconnect_path_dispatch") !=
          std::string::npos);
    CHECK(retry_send.find("live_connection_dispatch") !=
          std::string::npos);
    CHECK(retry_send.find("pn.send_msg(") == std::string::npos);
    INFO("the pinned transport supports migrating buffered bytes from a "
         "terminated peer connection into a successful replacement");
    const auto buffered = transport.find(
        "old_conn->send_buffer.move_pop()");
    const auto migrated = transport.find(
        "new_conn->write(std::move(buff_seg))", buffered);
    REQUIRE(buffered != std::string::npos);
    REQUIRE(migrated != std::string::npos);
    CHECK(buffered < migrated);

    INFO("scheduler failure dispatches only already-reserved bounded work and "
         "cannot reactivate the blind send path");
    const auto rearm_failure = dispatch.find("if (!rearm_failed)");
    const auto emergency_pending = dispatch.find(
        "if (!job->pending_pre_quorum_refresh_batch.empty())",
        rearm_failure);
    const auto emergency_poll = dispatch.find(
        "broadcast_exact_proposal_pre_quorum_retry(", emergency_pending);
    REQUIRE(rearm_failure != std::string::npos);
    REQUIRE(emergency_pending != std::string::npos);
    REQUIRE(emergency_poll != std::string::npos);
    CHECK(dispatch.find("broadcast_exact_proposal_fallback(", rearm_failure) ==
          std::string::npos);
}

TEST_CASE("timed-out root consumes late votes toward its frozen quorum",
          "[a06][a06-wiring-audit][production-wiring][root][late-vote]"
          "[intentional-red]")
{
    const auto source = read_source("src/hotstuff.cpp");
    const auto continuation = source_slice(
        source,
        "void HotStuffBase::continue_exact_contribution",
        "bool HotStuffBase::publish_exact_root_qc");
    const auto pass_marker = continuation.find(
        "if (proposal_contexts->pass_through_enabled(lease))");
    REQUIRE(pass_marker != std::string::npos);
    const auto pass_open = continuation.find('{', pass_marker);
    const auto pass_close = matching_closing_brace(continuation, pass_open);
    REQUIRE(pass_open != std::string::npos);
    REQUIRE(pass_close != std::string::npos);
    const auto pass_through = continuation.substr(
        pass_open, pass_close - pass_open + 1);
    const auto normalized = without_whitespace(pass_through);

    const auto root = normalized.find(
        "!lease.tree().parent.has_value()");
    const auto finish = normalized.find("try_finish_exact_context(lease)");
    CHECK(root != std::string::npos);
    CHECK(finish != std::string::npos);
    CHECK(pass_through.find("forward_exact_direct") != std::string::npos);
    CHECK(pass_through.find("forward_exact_relay") != std::string::npos);
    if (root != std::string::npos && finish != std::string::npos)
    {
        CHECK(root < finish);
        const auto root_open = normalized.find('{', root);
        const auto root_close = matching_closing_brace(normalized, root_open);
        REQUIRE(root_open != std::string::npos);
        REQUIRE(root_close != std::string::npos);
        const auto root_branch = normalized.substr(
            root_open, root_close - root_open + 1);
        check_absent(
            root_branch,
            "forward_exact_",
            "a root keeps accepting toward its QC but has no upward path");
        check_absent(
            root_branch,
            "send_exact_relay",
            "a root timeout or late vote never sends upward");
    }
}

TEST_CASE("late direct and aggregate forwarding claim signers before enqueue",
          "[a06][a06-wiring-audit][production-wiring][late-vote]"
          "[atomic-claim][intentional-red]")
{
    const auto source = read_source("src/hotstuff.cpp");
    const auto direct = source_slice(
        source,
        "bool HotStuffBase::forward_exact_direct",
        "bool HotStuffBase::forward_exact_relay");
    const auto relay = source_slice(
        source,
        "bool HotStuffBase::forward_exact_relay",
        "void HotStuffBase::continue_exact_contribution");

    for (const auto &path : {direct, relay})
    {
        const auto claim = path.find(
            "claim_unforwarded_certificate_reservation");
        const auto send = path.find("send_exact_relay");
        CHECK(claim != std::string::npos);
        REQUIRE(send != std::string::npos);
        if (claim != std::string::npos)
            CHECK(claim < send);
        check_absent(
            path,
            "snapshot(lease.key())",
            "a separate snapshot check cannot reserve signer ownership");
        check_absent(
            path,
            "mark_forwarded_signers",
            "marking after send permits duplicate enqueue before ownership is "
            "recorded");
    }
}

TEST_CASE("WE06-C05 production forwarding retries are transactional and bounded",
          "[we06][c05][production-wiring][reservation][retry]")
{
    const auto source = read_source("src/hotstuff.cpp");
    const auto normalized_source = without_whitespace(source);
    const auto send = without_whitespace(source_slice(
        source,
        "bool HotStuffBase::send_exact_relay_reserved",
        "quorum_cert_bt HotStuffBase::make_exact_direct_forwarding_candidate"));
    const auto retry = without_whitespace(source_slice(
        source,
        "void HotStuffBase::schedule_exact_forwarding_retry",
        "void HotStuffBase::dispatch_exact_forwarding_retry"));
    const auto abort = without_whitespace(source_slice(
        source,
        "void HotStuffBase::abort_exact_forwarding",
        "void HotStuffBase::discard_exact_forwarding_retries"));
    const auto emitter = source_slice(
        source,
        "void HotStuffBase::emit_adaptive_aggregation_event",
        "void HotStuffBase::emit_active_configuration_event");
    const auto finish = without_whitespace(source_slice(
        source,
        "void HotStuffBase::try_finish_exact_context",
        "void HotStuffBase::local_vote_authorized"));
    const auto required_timeout = source_slice(
        source,
        "void HotStuffBase::record_aggregation_timeout",
        "void HotStuffBase::record_optional_aggregation_absence");
    const auto optional_absence = source_slice(
        source,
        "void HotStuffBase::record_optional_aggregation_absence",
        "void HotStuffBase::emit_adaptive_aggregation_event");
    const auto continuation = source_slice(
        source,
        "void HotStuffBase::continue_exact_contribution",
        "bool HotStuffBase::publish_exact_root_qc");

    const auto release = send.find("release_forwarding_claim(");
    const auto schedule = send.find("schedule_exact_forwarding_retry(");
    const auto enqueue = send.find("send_exact_relay(");
    const auto commit = send.find("commit_forwarding_claim(");
    REQUIRE(release != std::string::npos);
    REQUIRE(schedule != std::string::npos);
    REQUIRE(enqueue != std::string::npos);
    REQUIRE(commit != std::string::npos);
    CHECK(release < schedule);
    CHECK(enqueue < commit);
    CHECK(normalized_source.find(
              "exact_forwarding_max_attempts=3") !=
          std::string::npos);
    CHECK(retry.find(
              "retry->attempts>=exact_forwarding_max_attempts") !=
          std::string::npos);
    CHECK(retry.find(
              "proposal_jobs>=lease.tree().assigned_subtree.size()+1") !=
          std::string::npos);
    CHECK(retry.find(
              "abort_exact_forwarding(lease,retry,"
              "\"forwarding_retry_exhausted\")") !=
          std::string::npos);
    CHECK(abort.find("ProposalContextEvent::proposal_aborted") !=
          std::string::npos);
    CHECK(abort.find("purge_pending_exact_contributions(lease.key())") !=
          std::string::npos);
    CHECK(abort.find("pending_exact_contributions.purge(lease.key())") ==
          std::string::npos);
    CHECK(emitter.find("if (adaptive_event_emitter == nullptr)") !=
          std::string::npos);

    const auto required_ready = finish.find(
        "required_subtree_complete(lease)");
    const auto initial_claim = finish.find(
        "claim_initial_certificate_reservation(");
    const auto initial_send = finish.find("send_exact_relay_reserved(");
    REQUIRE(required_ready != std::string::npos);
    REQUIRE(initial_claim != std::string::npos);
    REQUIRE(initial_send != std::string::npos);
    CHECK(required_ready < initial_claim);
    CHECK(initial_claim < initial_send);
    CHECK(required_timeout.find("required_branch_incomplete") !=
          std::string::npos);
    CHECK(optional_absence.find(
              "wait_exempt_absent_at_observation_deadline") !=
          std::string::npos);
    CHECK(continuation.find("rejected_signers") != std::string::npos);
    CHECK(continuation.find("delta_rejected") != std::string::npos);
}

TEST_CASE("normal exact completion keeps frozen subtree and global quorum rules",
          "[a06][a06-wiring-audit][production-wiring][quorum][control]")
{
    const auto hotstuff = read_source("src/hotstuff.cpp");
    const auto contexts = read_source("src/proposal_context.cpp");
    const auto finish = source_slice(
        hotstuff,
        "void HotStuffBase::try_finish_exact_context",
        "void HotStuffBase::local_vote_authorized");
    const auto normalized_contexts = without_whitespace(contexts);

    CHECK(finish.find("clone_publishable_root_qc(lease)") !=
          std::string::npos);
    CHECK(finish.find("assigned_subtree_complete(lease)") !=
          std::string::npos);
    check_absent(
        finish,
        "effective_required_votes",
        "normal non-root completion is fixed by the admitted subtree");
    check_absent(
        finish,
        "reputation",
        "adaptation scores cannot enter current-proposal completion");
    CHECK(normalized_contexts.find(
              "2*((assigned.size()-1)/3)+1") != std::string::npos);
    CHECK(normalized_contexts.find(
              "signers.size()<found->second->global_quorum") !=
          std::string::npos);
}

TEST_CASE("pipelined leaders admit the exact local proposal before broadcast",
          "[rem-a06-02][production-wiring][leader-local-admission]"
          "[pipelined][intentional-red]")
{
    const auto source = read_source("src/hotstuff.cpp");
    const auto beat = source_slice(
        source,
        "void HotStuffBase::beat()",
        "void HotStuffBase::print_pipe_queues");
    const auto piped_branch = without_whitespace(source_slice(
        beat,
        "block_t piped_block;",
        "piped_submitted = false;"));

    const auto configuration = piped_branch.find(
        "constautoconfiguration=exact_configuration("
        "get_cur_epoch_nr(),get_tree_id());");
    const auto queued = piped_branch.find(
        "piped_queue.push_back(piped_block->hash);");
    const auto local_delivery =
        piped_branch.find("on_deliver_blk(piped_block);");
    const auto exact_admission = piped_branch.find(
        "Proposalprop=process_block("
        "piped_block,false,configuration);");
    const auto marked_delivered = piped_branch.find(
        "piped_block->piped_delivered=true;");
    const auto broadcast =
        piped_branch.find("do_broadcast_proposal(prop);");

    INFO("the live piped-block branch must snapshot its exact configuration, "
         "enqueue before processing, deliver locally, create the proposal "
         "through process_block, mark it delivered, and only then broadcast");
    CHECK(configuration != std::string::npos);
    CHECK(queued != std::string::npos);
    CHECK(local_delivery != std::string::npos);
    CHECK(exact_admission != std::string::npos);
    CHECK(marked_delivered != std::string::npos);
    CHECK(broadcast != std::string::npos);
    INFO("the piped branch must not bypass process_block with a manually "
         "constructed Proposal");
    CHECK(piped_branch.find("Proposalprop(") == std::string::npos);
    if (configuration != std::string::npos &&
        queued != std::string::npos &&
        local_delivery != std::string::npos &&
        exact_admission != std::string::npos &&
        marked_delivered != std::string::npos &&
        broadcast != std::string::npos)
    {
        CHECK(configuration < local_delivery);
        CHECK(queued < exact_admission);
        CHECK(local_delivery < exact_admission);
        CHECK(exact_admission < marked_delivered);
        CHECK(marked_delivered < broadcast);
    }
}

TEST_CASE("vote handlers never perform late piped-block admission",
          "[rem-a06-02][production-wiring][leader-local-admission]"
          "[handlers][intentional-red]")
{
    const auto source = read_source("src/hotstuff.cpp");
    const auto direct = without_whitespace(source_slice(
        source,
        "void HotStuffBase::vote_handler",
        "void HotStuffBase::vote_relay_handler"));
    const auto relay = without_whitespace(source_slice(
        source,
        "void HotStuffBase::vote_relay_handler",
        "void HotStuffBase::req_blk_handler"));
    const std::string late_normalization =
        "process_block(blk,false,v->configuration());";

    INFO("a child vote must not be the event that admits a leader's piped "
         "proposal");
    CHECK(direct.find(late_normalization) == std::string::npos);
    INFO("a child relay must not be the event that admits a leader's piped "
         "proposal");
    CHECK(relay.find(late_normalization) == std::string::npos);
}

TEST_CASE("normal leaders admit locally before self-vote and broadcast",
          "[rem-a06-02][production-wiring][leader-local-admission]"
          "[normal-path][intentional-red]")
{
    const auto source = read_source("src/consensus.cpp");
    const auto normal_path = without_whitespace(source_slice(
        source,
        "block_t HotStuffCore::on_propose",
        "Proposal HotStuffCore::process_block"));
    const auto local_processing = without_whitespace(source_slice(
        source,
        "Proposal HotStuffCore::process_block",
        "void HotStuffCore::on_receive_proposal"));

    const auto local_delivery = normal_path.find("on_deliver_blk(bnew);");
    const auto process = normal_path.find("process_block(");
    const auto broadcast =
        normal_path.find("do_broadcast_proposal(prop);");
    INFO("the normal local path must deliver, process, and only then "
         "broadcast");
    CHECK(local_delivery != std::string::npos);
    CHECK(process != std::string::npos);
    CHECK(broadcast != std::string::npos);
    if (local_delivery != std::string::npos &&
        process != std::string::npos &&
        broadcast != std::string::npos)
    {
        CHECK(local_delivery < process);
        CHECK(process < broadcast);
    }

    const auto local_open = local_processing.find("admit_local(");
    const auto self_vote = local_processing.find("on_receive_vote(");
    INFO("process_block must open the exact leader-local context before "
         "recording the self-vote");
    CHECK(local_open != std::string::npos);
    CHECK(self_vote != std::string::npos);
    if (local_open != std::string::npos &&
        self_vote != std::string::npos)
        CHECK(local_open < self_vote);
}

#if __has_include("hotstuff/proposal_context.h") && \
    __has_include("hotstuff/exact_vote_handler.h")
#include "hotstuff/exact_vote_handler.h"
#include "hotstuff/proposal_context.h"
#include "support/bls_fixtures.h"
#define KAURI_HAS_EXACT_VOTE_HANDLER_GATE 1
#else
#define KAURI_HAS_EXACT_VOTE_HANDLER_GATE 0
#endif

#if !KAURI_HAS_EXACT_VOTE_HANDLER_GATE

TEST_CASE("REM-A06-02 requires a production exact vote-handler gate",
          "[rem-a06-02][handler-gate][intentional-red]")
{
    FAIL("missing hotstuff/exact_vote_handler.h and/or proposal_context.h: "
         "the production handlers cannot reject unopened exact contexts before "
         "worker verification and block delivery");
}

#else

using hotstuff::ConfigurationId;
using hotstuff::DataStream;
using hotstuff::ExactContributionEnvelope;
using hotstuff::ExactContributionKind;
using hotstuff::ExactVoteHandlerCoordinator;
using hotstuff::ExactVoteHandlerEffects;
using hotstuff::make_exact_direct_envelope;
using hotstuff::make_exact_relay_envelope;
using hotstuff::ProposalContextEvent;
using hotstuff::ProposalContextLease;
using hotstuff::ProposalContextLifecycle;
using hotstuff::ProposalContextMetadata;
using hotstuff::ProposalContextStatus;
using hotstuff::ProposalKey;
using hotstuff::ProposalTreeSnapshot;
using hotstuff::ReplicaID;
using hotstuff::promise_t;
using hotstuff::uint256_t;
using hotstuff::test::BlsTestCore;
using hotstuff::test::add_valid_signers;

namespace
{

uint256_t gate_digest(const std::string &label)
{
    return DataStream(label).get_hash();
}

ConfigurationId gate_configuration(std::uint32_t epoch,
                                   std::uint32_t tree,
                                   const std::string &definition)
{
    return ConfigurationId{epoch, tree, gate_digest(definition)};
}

ProposalKey gate_key(const ConfigurationId &configuration,
                     const std::string &block)
{
    return ProposalKey{configuration, gate_digest(block)};
}

ProposalContextMetadata gate_metadata(const ProposalKey &key,
                                      std::vector<ReplicaID> children = {1, 2})
{
    ProposalTreeSnapshot tree;
    tree.local_replica = 0;
    tree.root = 0;
    tree.parent = std::nullopt;
    tree.direct_children = std::move(children);
    tree.assigned_subtree = {0, 1, 2, 3, 4, 5, 6};
    tree.child_subtrees = {{1, {1, 3, 4}}, {2, {2, 5, 6}}};
    tree.fanout = 2;
    tree.pipeline_stretch = 2;
    return ProposalContextMetadata{key, std::move(tree), 5};
}

ProposalContextMetadata non_root_gate_metadata(const ProposalKey &key)
{
    ProposalTreeSnapshot tree;
    tree.local_replica = 3;
    tree.root = 0;
    tree.parent = 1;
    tree.assigned_subtree = {3};
    tree.fanout = 2;
    tree.pipeline_stretch = 2;
    return ProposalContextMetadata{key, std::move(tree), 5};
}

ExactContributionEnvelope direct_envelope(
    const ProposalKey &key,
    ReplicaID authenticated_sender = 1,
    ReplicaID claimed_voter = 1)
{
    ExactContributionEnvelope value;
    value.message_key = key;
    value.certificate_key = key;
    value.authenticated_sender = authenticated_sender;
    value.claimed_voter = claimed_voter;
    value.certified_signers = {claimed_voter};
    return value;
}

ExactContributionEnvelope relay_envelope(
    const ProposalKey &key,
    ReplicaID authenticated_sender = 1,
    std::set<ReplicaID> certified_signers = {1, 3, 4})
{
    ExactContributionEnvelope value;
    value.message_key = key;
    value.certificate_key = key;
    value.authenticated_sender = authenticated_sender;
    value.claimed_voter = std::nullopt;
    value.certified_signers = std::move(certified_signers);
    return value;
}

promise_t resolved(bool value)
{
    return promise_t([value](promise_t &promise) {
        promise.resolve(value);
    });
}

struct WatchedCompletion
{
    bool settled{false};
    bool value{false};

    explicit WatchedCompletion(promise_t promise)
    {
        promise.then([this](bool result) {
            settled = true;
            value = result;
        });
        promise.fail([this]() {
            settled = true;
            value = false;
        });
    }
};

class GateEffects final : public ExactVoteHandlerEffects
{
public:
    bool deferred{false};
    bool worker_result{true};
    bool delivery_result{true};
    promise_t worker;
    promise_t delivery;
    std::size_t worker_starts{0};
    std::size_t delivery_starts{0};
    std::size_t latency_mutations{0};
    std::size_t aggregation_mutations{0};
    std::size_t timer_mutations{0};
    std::size_t continuations{0};
    std::map<ProposalKey, std::size_t> continuations_by_key;
    std::vector<ExactContributionKind> accepted_kinds;

    promise_t start_worker_verification(
        ExactContributionKind,
        const ExactContributionEnvelope &) override
    {
        ++worker_starts;
        return deferred ? worker : resolved(worker_result);
    }

    promise_t start_block_delivery(const ProposalKey &) override
    {
        ++delivery_starts;
        return deferred ? delivery : resolved(delivery_result);
    }

    void continue_verified(
        const ProposalContextLease &lease,
        ExactContributionKind kind,
        const ExactContributionEnvelope &contribution) override
    {
        REQUIRE(lease.key() == contribution.message_key);
        ++continuations;
        ++latency_mutations;
        ++aggregation_mutations;
        ++timer_mutations;
        ++continuations_by_key[lease.key()];
        accepted_kinds.push_back(kind);
    }
};

class InconsistentSignerQuorumCert final
    : public hotstuff::QuorumCertDummy
{
public:
    InconsistentSignerQuorumCert(
        const hotstuff::ReplicaConfig &config,
        const ProposalKey &key,
        std::vector<ReplicaID> enumerated_signers,
        std::size_t reported_count)
        : QuorumCertDummy(config, key),
          enumerated_signers_(std::move(enumerated_signers)),
          reported_count_(reported_count)
    {}

    std::size_t get_sigs_n() override
    {
        return reported_count_;
    }

    std::vector<ReplicaID> get_signers() const override
    {
        return enumerated_signers_;
    }

    InconsistentSignerQuorumCert *clone() override
    {
        return new InconsistentSignerQuorumCert(*this);
    }

private:
    std::vector<ReplicaID> enumerated_signers_;
    std::size_t reported_count_{0};
};

void check_no_handler_work(const GateEffects &effects)
{
    CHECK(effects.worker_starts == 0);
    CHECK(effects.delivery_starts == 0);
    CHECK(effects.latency_mutations == 0);
    CHECK(effects.aggregation_mutations == 0);
    CHECK(effects.timer_mutations == 0);
    CHECK(effects.continuations == 0);
}

bool immediate_value(promise_t completion)
{
    WatchedCompletion watched(std::move(completion));
    REQUIRE(watched.settled);
    return watched.value;
}

} // namespace

TEST_CASE("production adapters preserve only representable Vote and VoteRelay identity",
          "[rem-a06-02][handler-gate][adapter][production-seam]")
{
    BlsTestCore core(7);
    const auto config = gate_configuration(39, 4, "adapter-epoch");
    const auto proposal = gate_key(config, "adapter-block");

    auto vote = core.make_vote(1, 1, proposal);
    const auto direct = make_exact_direct_envelope(vote, 1);
    CHECK(direct.message_key == proposal);
    CHECK(direct.certificate_key == proposal);
    CHECK(direct.authenticated_sender == 1);
    CHECK(direct.claimed_voter == std::optional<ReplicaID>{1});
    CHECK(direct.certified_signers == std::set<ReplicaID>{1});

    auto *aggregate =
        new hotstuff::QuorumCertAggBLS(core.get_config(), proposal);
    add_valid_signers(*aggregate, core, {1, 3, 4}, proposal);
    aggregate->compute();
    hotstuff::VoteRelay relay(
        proposal, hotstuff::quorum_cert_bt(aggregate), &core);
    const auto relayed = make_exact_relay_envelope(relay, 1);
    CHECK(relayed.message_key == proposal);
    CHECK(relayed.certificate_key == proposal);
    CHECK(relayed.authenticated_sender == 1);
    CHECK_FALSE(relayed.claimed_voter.has_value());
    CHECK(relayed.certified_signers ==
          std::set<ReplicaID>{1, 3, 4});

    ProposalContextLifecycle contexts;
    REQUIRE(contexts.admit_remote(gate_metadata(proposal)).has_value());
    GateEffects effects;
    ExactVoteHandlerCoordinator handler(contexts, effects);
    CHECK(immediate_value(handler.handle_direct(direct)));
    CHECK(immediate_value(handler.handle_relay(relayed)));
    CHECK(effects.worker_starts == 2);
    CHECK(effects.delivery_starts == 2);
    CHECK(effects.continuations == 2);
}

TEST_CASE("relay adapter rejects inconsistent signer cardinality before effects",
          "[rem-a06-02][handler-gate][adapter][signers][intentional-red]")
{
    BlsTestCore core(7);
    const auto config = gate_configuration(39, 5, "adapter-cardinality");
    const auto proposal = gate_key(config, "adapter-cardinality-block");
    auto *certificate = new InconsistentSignerQuorumCert(
        core.get_config(), proposal, {1, 1}, 2);
    hotstuff::VoteRelay relay(
        proposal, hotstuff::quorum_cert_bt(certificate), &core);

    const auto malformed = make_exact_relay_envelope(relay, 1);
    CHECK(malformed.certified_signers.empty());

    ProposalContextLifecycle contexts;
    REQUIRE(contexts.admit_remote(gate_metadata(proposal)).has_value());
    GateEffects effects;
    ExactVoteHandlerCoordinator handler(contexts, effects);
    CHECK_FALSE(immediate_value(handler.handle_relay(malformed)));
    check_no_handler_work(effects);
}

TEST_CASE("unopened exact contexts reject before every asynchronous or protocol effect",
          "[rem-a06-02][handler-gate][pre-crypto]")
{
    const auto active = gate_configuration(40, 7, "gate-active");
    const auto future = gate_configuration(41, 7, "gate-future");
    const auto active_key = gate_key(active, "active-block");
    const auto future_key = gate_key(future, "future-block");

    SECTION("unknown context")
    {
        ProposalContextLifecycle contexts;
        GateEffects effects;
        ExactVoteHandlerCoordinator handler(contexts, effects);
        CHECK_FALSE(immediate_value(
            handler.handle_direct(direct_envelope(active_key))));
        check_no_handler_work(effects);
    }

    SECTION("known buffered future context")
    {
        ProposalContextLifecycle contexts;
        REQUIRE(contexts.buffer_future(gate_metadata(future_key)));
        GateEffects effects;
        ExactVoteHandlerCoordinator handler(contexts, effects);
        CHECK_FALSE(immediate_value(
            handler.handle_relay(relay_envelope(future_key))));
        check_no_handler_work(effects);
        CHECK(contexts.context_status(future_key) ==
              ProposalContextStatus::buffered_future);
    }

    SECTION("digest mismatch cannot alias an admitted same-hash context")
    {
        ProposalContextLifecycle contexts;
        const auto shared_hash = gate_digest("same-body");
        const ProposalKey admitted{active, shared_hash};
        auto wrong_digest = active;
        wrong_digest.epoch_digest = gate_digest("divergent-active");
        const ProposalKey relabelled{wrong_digest, shared_hash};
        REQUIRE(contexts.admit_remote(gate_metadata(admitted)).has_value());
        GateEffects effects;
        ExactVoteHandlerCoordinator handler(contexts, effects);
        CHECK_FALSE(immediate_value(
            handler.handle_direct(direct_envelope(relabelled))));
        check_no_handler_work(effects);
    }

    SECTION("closed stale context")
    {
        ProposalContextLifecycle contexts;
        REQUIRE(contexts.admit_remote(gate_metadata(active_key)).has_value());
        REQUIRE(contexts.close(
            active_key, ProposalContextEvent::proposal_aborted));
        GateEffects effects;
        ExactVoteHandlerCoordinator handler(contexts, effects);
        CHECK_FALSE(immediate_value(
            handler.handle_relay(relay_envelope(active_key))));
        check_no_handler_work(effects);
    }

    SECTION("activation alone does not admit a context")
    {
        ProposalContextLifecycle contexts;
        contexts.activate_configuration(active);
        GateEffects effects;
        ExactVoteHandlerCoordinator handler(contexts, effects);
        CHECK_FALSE(immediate_value(
            handler.handle_direct(direct_envelope(active_key))));
        check_no_handler_work(effects);
    }

    SECTION("certificate key mismatch is a cheap rejection")
    {
        ProposalContextLifecycle contexts;
        REQUIRE(contexts.admit_remote(gate_metadata(active_key)).has_value());
        auto relabelled = direct_envelope(active_key);
        relabelled.certificate_key = future_key;
        GateEffects effects;
        ExactVoteHandlerCoordinator handler(contexts, effects);
        CHECK_FALSE(immediate_value(
            handler.handle_direct(relabelled)));
        check_no_handler_work(effects);
    }

    SECTION("a direct Vote voter must equal its authenticated child")
    {
        ProposalContextLifecycle contexts;
        REQUIRE(contexts.admit_remote(gate_metadata(active_key)).has_value());
        auto wrong_voter = direct_envelope(active_key, 1, 2);
        GateEffects effects;
        ExactVoteHandlerCoordinator handler(contexts, effects);
        CHECK_FALSE(immediate_value(
            handler.handle_direct(wrong_voter)));
        check_no_handler_work(effects);
    }

    SECTION("a direct certificate signer set must match the claimed voter")
    {
        ProposalContextLifecycle contexts;
        REQUIRE(contexts.admit_remote(gate_metadata(active_key)).has_value());
        auto wrong_signer = direct_envelope(active_key, 1, 1);
        wrong_signer.certified_signers = {2};
        GateEffects effects;
        ExactVoteHandlerCoordinator handler(contexts, effects);
        CHECK_FALSE(immediate_value(
            handler.handle_direct(wrong_signer)));
        check_no_handler_work(effects);
    }

    SECTION("a relay authenticates only its transport child and signer subtree")
    {
        ProposalContextLifecycle contexts;
        REQUIRE(contexts.admit_remote(gate_metadata(active_key)).has_value());
        auto wrong_subtree = relay_envelope(
            active_key, 1, std::set<ReplicaID>{1, 3, 5});
        REQUIRE_FALSE(wrong_subtree.claimed_voter.has_value());
        GateEffects effects;
        ExactVoteHandlerCoordinator handler(contexts, effects);
        CHECK_FALSE(immediate_value(
            handler.handle_relay(wrong_subtree)));
        check_no_handler_work(effects);
    }

    SECTION("the exact root admits an authenticated frozen descendant")
    {
        ProposalContextLifecycle contexts;
        REQUIRE(contexts.admit_remote(gate_metadata(active_key)).has_value());
        GateEffects effects;
        ExactVoteHandlerCoordinator handler(contexts, effects);
        CHECK(immediate_value(
            handler.handle_direct(direct_envelope(active_key, 6, 6))));
        CHECK(effects.worker_starts == 1);
        CHECK(effects.delivery_starts == 1);
        CHECK(effects.continuations == 1);
    }

    SECTION("a non-root still rejects a non-child direct vote")
    {
        ProposalContextLifecycle contexts;
        REQUIRE(contexts.admit_remote(
            non_root_gate_metadata(active_key)).has_value());
        GateEffects effects;
        ExactVoteHandlerCoordinator handler(contexts, effects);
        CHECK_FALSE(immediate_value(
            handler.handle_direct(direct_envelope(active_key, 6, 6))));
        check_no_handler_work(effects);
    }
}

TEST_CASE("an open context starts one worker and one delivery before one continuation",
          "[rem-a06-02][handler-gate][open][once]")
{
    const auto config = gate_configuration(42, 3, "open-gate");
    const auto proposal = gate_key(config, "open-block");

    SECTION("direct vote")
    {
        ProposalContextLifecycle contexts;
        REQUIRE(contexts.admit_remote(gate_metadata(proposal)).has_value());
        GateEffects effects;
        effects.deferred = true;
        ExactVoteHandlerCoordinator handler(contexts, effects);
        WatchedCompletion completion(
            handler.handle_direct(direct_envelope(proposal)));

        CHECK(effects.worker_starts == 1);
        CHECK(effects.delivery_starts == 1);
        CHECK(effects.continuations == 0);
        CHECK_FALSE(completion.settled);

        effects.worker.resolve(true);
        CHECK(effects.continuations == 0);
        CHECK_FALSE(completion.settled);
        effects.delivery.resolve(true);

        CHECK(completion.settled);
        CHECK(completion.value);
        CHECK(effects.worker_starts == 1);
        CHECK(effects.delivery_starts == 1);
        CHECK(effects.continuations == 1);
        CHECK((effects.accepted_kinds ==
               std::vector<ExactContributionKind>{
                   ExactContributionKind::direct_vote}));

        effects.worker.resolve(true);
        effects.delivery.resolve(true);
        CHECK(effects.continuations == 1);
    }

    SECTION("aggregate relay with reverse prerequisite order")
    {
        ProposalContextLifecycle contexts;
        REQUIRE(contexts.admit_remote(gate_metadata(proposal)).has_value());
        GateEffects effects;
        effects.deferred = true;
        ExactVoteHandlerCoordinator handler(contexts, effects);
        WatchedCompletion completion(
            handler.handle_relay(relay_envelope(proposal)));

        CHECK(effects.worker_starts == 1);
        CHECK(effects.delivery_starts == 1);
        effects.delivery.resolve(true);
        CHECK(effects.continuations == 0);
        effects.worker.resolve(true);

        CHECK(completion.settled);
        CHECK(completion.value);
        CHECK(effects.continuations == 1);
        CHECK((effects.accepted_kinds ==
               std::vector<ExactContributionKind>{
                   ExactContributionKind::aggregate_relay}));
    }
}

TEST_CASE("old admitted context drains across activation while new waits for admission",
          "[rem-a06-02][handler-gate][activation][old-drain]")
{
    ProposalContextLifecycle contexts;
    const auto old_config = gate_configuration(43, 2, "old-open");
    const auto new_config = gate_configuration(44, 2, "new-active");
    const auto old_key = gate_key(old_config, "old-draining");
    const auto new_key = gate_key(new_config, "new-unadmitted");
    auto old_lease = contexts.admit_remote(gate_metadata(old_key));
    REQUIRE(old_lease.has_value());
    contexts.activate_configuration(new_config);

    GateEffects effects;
    ExactVoteHandlerCoordinator handler(contexts, effects);

    CHECK(immediate_value(handler.handle_direct(direct_envelope(old_key))));
    CHECK(effects.worker_starts == 1);
    CHECK(effects.delivery_starts == 1);
    CHECK(effects.continuations_by_key[old_key] == 1);
    CHECK(contexts.revalidate(*old_lease));

    CHECK_FALSE(immediate_value(
        handler.handle_relay(relay_envelope(new_key))));
    CHECK(effects.worker_starts == 1);
    CHECK(effects.delivery_starts == 1);
    CHECK(effects.continuations_by_key[new_key] == 0);

    REQUIRE(contexts.admit_remote(gate_metadata(new_key)).has_value());
    CHECK(immediate_value(handler.handle_relay(relay_envelope(new_key))));
    CHECK(effects.worker_starts == 2);
    CHECK(effects.delivery_starts == 2);
    CHECK(effects.continuations_by_key[new_key] == 1);
    CHECK(contexts.context_status(old_key) ==
          ProposalContextStatus::admitted_open);
}

TEST_CASE("failed crypto or delivery never reaches the verified continuation",
          "[rem-a06-02][handler-gate][prerequisite-failure]")
{
    const auto config = gate_configuration(46, 3, "failed-prerequisite");
    const auto proposal = gate_key(config, "failed-prerequisite-block");

    SECTION("worker verification resolves false")
    {
        ProposalContextLifecycle contexts;
        REQUIRE(contexts.admit_remote(gate_metadata(proposal)).has_value());
        GateEffects effects;
        effects.worker_result = false;
        ExactVoteHandlerCoordinator handler(contexts, effects);

        CHECK_FALSE(immediate_value(
            handler.handle_direct(direct_envelope(proposal))));
        CHECK(effects.worker_starts == 1);
        CHECK(effects.delivery_starts == 1);
        CHECK(effects.continuations == 0);
        CHECK(effects.latency_mutations == 0);
        CHECK(effects.aggregation_mutations == 0);
        CHECK(effects.timer_mutations == 0);
    }

    SECTION("block delivery resolves false")
    {
        ProposalContextLifecycle contexts;
        REQUIRE(contexts.admit_remote(gate_metadata(proposal)).has_value());
        GateEffects effects;
        effects.delivery_result = false;
        ExactVoteHandlerCoordinator handler(contexts, effects);

        CHECK_FALSE(immediate_value(
            handler.handle_relay(relay_envelope(proposal))));
        CHECK(effects.worker_starts == 1);
        CHECK(effects.delivery_starts == 1);
        CHECK(effects.continuations == 0);
        CHECK(effects.latency_mutations == 0);
        CHECK(effects.aggregation_mutations == 0);
        CHECK(effects.timer_mutations == 0);
    }
}

TEST_CASE("closing during deferred crypto or delivery suppresses continuation",
          "[rem-a06-02][handler-gate][lease][async-close]")
{
    const auto config = gate_configuration(45, 5, "async-close");
    const auto proposal = gate_key(config, "async-block");

    SECTION("crypto completes first then context closes during delivery")
    {
        ProposalContextLifecycle contexts;
        REQUIRE(contexts.admit_remote(gate_metadata(proposal)).has_value());
        GateEffects effects;
        effects.deferred = true;
        ExactVoteHandlerCoordinator handler(contexts, effects);
        WatchedCompletion completion(
            handler.handle_direct(direct_envelope(proposal)));

        effects.worker.resolve(true);
        REQUIRE(contexts.close(
            proposal, ProposalContextEvent::proposal_aborted));
        effects.delivery.resolve(true);

        CHECK(completion.settled);
        CHECK_FALSE(completion.value);
        CHECK(effects.continuations == 0);
        CHECK(effects.latency_mutations == 0);
        CHECK(effects.aggregation_mutations == 0);
        CHECK(effects.timer_mutations == 0);
    }

    SECTION("delivery completes first then context closes during crypto")
    {
        ProposalContextLifecycle contexts;
        REQUIRE(contexts.admit_remote(gate_metadata(proposal)).has_value());
        GateEffects effects;
        effects.deferred = true;
        ExactVoteHandlerCoordinator handler(contexts, effects);
        WatchedCompletion completion(
            handler.handle_relay(relay_envelope(proposal)));

        effects.delivery.resolve(true);
        REQUIRE(contexts.close(
            proposal, ProposalContextEvent::shutdown));
        effects.worker.resolve(true);

        CHECK(completion.settled);
        CHECK_FALSE(completion.value);
        CHECK(effects.continuations == 0);
        CHECK(effects.latency_mutations == 0);
        CHECK(effects.aggregation_mutations == 0);
        CHECK(effects.timer_mutations == 0);
    }
}

#endif
