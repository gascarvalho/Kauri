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
    const auto remote_open = remote.find("admit_remote");
    const auto remote_protocol = remote.find("on_receive_proposal");
    INFO("remote admission must occur after delivery but before proposal processing");
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
    const auto active = source_slice(
        hotstuff,
        "void HotStuffBase::process_active",
        "void HotStuffBase::local_vote_authorized");
    const auto authorize = source_slice(
        admission,
        "bool ProposalAdmissionCoordinator::authorize_local_vote",
        "const ConfigurationId &");

    const auto delivery = active.find("async_deliver_blk");
    const auto admitted = active.find("admit_remote");
    const auto accumulator = active.find("initialize_accumulator");
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
    CHECK(protocol != std::string::npos);
    CHECK(still_open != std::string::npos);
    CHECK(child_state != std::string::npos);
    CHECK(latency != std::string::npos);
    CHECK(deadline != std::string::npos);
    CHECK(drain != std::string::npos);
    if (delivery != std::string::npos &&
        admitted != std::string::npos &&
        accumulator != std::string::npos &&
        protocol != std::string::npos &&
        still_open != std::string::npos &&
        child_state != std::string::npos &&
        latency != std::string::npos &&
        deadline != std::string::npos &&
        drain != std::string::npos)
    {
        CHECK(delivery < admitted);
        CHECK(admitted < accumulator);
        CHECK(accumulator < protocol);
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
    CHECK(abort.find("pending_exact_contributions.purge(lease.key())") !=
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
        "block_t piped_block = storage->add_blk",
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

    SECTION("authenticated sender must be a direct child in the frozen tree")
    {
        ProposalContextLifecycle contexts;
        REQUIRE(contexts.admit_remote(gate_metadata(active_key)).has_value());
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
