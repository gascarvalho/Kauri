#include <cctype>
#include <cstddef>
#include <fstream>
#include <sstream>
#include <string>

#include "catch.hpp"

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

std::size_t count_occurrences(const std::string &source,
                              const std::string &needle)
{
    std::size_t count = 0;
    for (std::size_t cursor = 0;
         (cursor = source.find(needle, cursor)) != std::string::npos;
         cursor += needle.size())
        ++count;
    return count;
}

} // namespace

TEST_CASE("configuration activation never retires proposal state",
          "[proposal-retirement][production-wiring][activation][control]")
{
    const auto source = read_source("src/hotstuff.cpp");
    const auto activation = source_slice(
        source,
        "void HotStuffBase::activate_proposal_configuration",
        "void HotStuffBase::relay_once");

    INFO("activation routes exact proposals but cannot infer deterministic "
         "finality or retire an old in-flight configuration");
    CHECK(activation.find("retire_proposal") == std::string::npos);
    CHECK(activation.find("retire_configuration") == std::string::npos);
    CHECK(activation.find("advance_retirement_floor") ==
          std::string::npos);
    CHECK(activation.find("purge_before_epoch") == std::string::npos);
}

TEST_CASE("proposal retirement is reached from the deterministic commit loop",
          "[proposal-retirement][production-wiring][commit][control]")
{
    const auto source = without_whitespace(
        read_source("src/consensus.cpp"));
    const auto commit_loop = source_slice(
        source,
        "for(std::size_tqueue_index=commit_queue.size();",
        "b_exec=blk;");
    const auto decided = commit_loop.find("blk->decision=1;");
    const auto certified = commit_loop.find(
        "CommitCertifierDisposition::verified_direct_certifier");
    const auto legal_skip = commit_loop.find(
        "CommitCertifierDisposition::legal_qc_skipped_ancestor");
    const auto unproven = commit_loop.find(
        "CommitCertifierDisposition::unproven");

    REQUIRE(decided != std::string::npos);
    REQUIRE(certified != std::string::npos);
    REQUIRE(legal_skip != std::string::npos);
    REQUIRE(unproven != std::string::npos);
    CHECK(decided < certified);
    CHECK(certified < legal_skip);
    CHECK(legal_skip < unproven);
}

TEST_CASE("commit pruning and floor advancement are deterministically ordered",
          "[proposal-retirement][production-wiring][commit]"
          "[ordering][intentional-red]")
{
    const auto source = read_source("src/hotstuff.cpp");
    const auto consensus = without_whitespace(source_slice(
        source,
        "void HotStuffBase::do_consensus_with_identity_provenance(",
        "void HotStuffBase::do_decide"));

    const auto lifecycle_close = consensus.find(
        "proposal_contexts->close_committed_block(blk->get_hash())");
    const auto ingress_proposal =
        consensus.find("proposal_admission->retire_proposal(key)");
    const auto committed_floor = consensus.find(
        "advance_committed_retirement_floor(blk,authoritative_key)");
    const auto pacemaker = consensus.find("pmaker->on_consensus(blk)");
    INFO("the lifecycle's block-hash index returns every exact committed "
         "key; all corresponding admission/dedup entries are pruned before "
         "the pacemaker observes the commit");
    REQUIRE(lifecycle_close != std::string::npos);
    REQUIRE(ingress_proposal != std::string::npos);
    REQUIRE(committed_floor != std::string::npos);
    REQUIRE(pacemaker != std::string::npos);
    CHECK(lifecycle_close < ingress_proposal);
    CHECK(ingress_proposal < committed_floor);
    CHECK(committed_floor < pacemaker);

    // Keep the activation-height condition out of do_consensus itself. Its
    // unconditional cleanup remains compatible with exact same-hash context
    // closure; the helper is still called only from this commit boundary.
    CHECK(consensus.find("if(") == std::string::npos);
    CHECK(consensus.find("self_qc") == std::string::npos);

    const auto floor_helper = without_whitespace(source_slice(
        source,
        "void HotStuffBase::advance_committed_retirement_floor",
        "void HotStuffBase::do_consensus"));

    const auto height = floor_helper.find("blk->height");
    const auto activation_height =
        floor_helper.find("activation_height()");
    const auto comparison = floor_helper.find(">=", height);
    const auto ingress_floor = floor_helper.find(
        "proposal_admission->advance_retirement_floor(");
    const auto pending_floor = floor_helper.find(
        "pending_exact_contributions.purge_before_epoch(");
    const auto lifecycle_floor = floor_helper.find(
        "proposal_contexts->advance_retirement_floor(");

    INFO("only a committed block at or beyond the active epoch's "
         "activation_height may advance the exclusive first-live floor");
    REQUIRE(height != std::string::npos);
    REQUIRE(activation_height != std::string::npos);
    REQUIRE(comparison != std::string::npos);
    REQUIRE(ingress_floor != std::string::npos);
    REQUIRE(pending_floor != std::string::npos);
    REQUIRE(lifecycle_floor != std::string::npos);
    CHECK(height < comparison);
    CHECK(comparison < activation_height);
    CHECK(activation_height < ingress_floor);
    CHECK(ingress_floor < pending_floor);
    CHECK(pending_floor < lifecycle_floor);

    INFO("all production floor advancement originates at deterministic "
         "commit, not proposal receipt or mere activation");
    CHECK(count_occurrences(
              source,
              "proposal_admission->advance_retirement_floor(") == 1);
    CHECK(count_occurrences(
              source,
              "proposal_contexts->advance_retirement_floor(") == 1);
    CHECK(count_occurrences(
              source,
              "pending_exact_contributions.purge_before_epoch(") == 1);
}

TEST_CASE("commit floor waits for predecessor proposal contexts to drain",
          "[proposal-retirement][production-wiring][draining][regression]")
{
    const auto source = read_source("src/hotstuff.cpp");
    const auto floor_helper = without_whitespace(source_slice(
        source,
        "void HotStuffBase::advance_committed_retirement_floor",
        "void HotStuffBase::do_consensus"));

    const auto open_guard = floor_helper.find(
        "proposal_contexts->has_open_context_before_epoch("
        "first_live_epoch)");
    const auto committed_key = floor_helper.find(
        "committed_key.has_value()");
    const auto exact_configuration = floor_helper.find(
        "committed_key->configuration!=active_configuration");
    const auto ingress_floor = floor_helper.find(
        "proposal_admission->advance_retirement_floor(");
    REQUIRE(committed_key != std::string::npos);
    REQUIRE(exact_configuration != std::string::npos);
    REQUIRE(open_guard != std::string::npos);
    REQUIRE(ingress_floor != std::string::npos);
    CHECK(committed_key < exact_configuration);
    CHECK(exact_configuration < open_guard);
    CHECK(open_guard < ingress_floor);
}
