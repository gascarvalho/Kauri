#include <cctype>
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

} // namespace

TEST_CASE("consensus cleanup precedes pacemaker notification",
          "[rem-a06-02][production-wiring][commit][secondary-index]"
          "[intentional-red]")
{
    const auto source = read_source("src/hotstuff.cpp");
    const auto body = source_slice(
        source,
        "void HotStuffBase::do_consensus(\n"
        "        const block_t &blk,\n"
        "        const quorum_cert_bt &verified_direct_certifier)",
        "void HotStuffBase::do_decide");
    const auto normalized = without_whitespace(body);
    const auto cleanup = normalized.find(
        "proposal_contexts->close_committed_block(blk->get_hash())");
    const auto pacemaker = normalized.find("pmaker->on_consensus(blk)");

    INFO("every exact context for a committed block hash must be closed "
         "before the pacemaker observes that commit");
    REQUIRE(cleanup != std::string::npos);
    REQUIRE(pacemaker != std::string::npos);
    CHECK(cleanup < pacemaker);

    INFO("commit cleanup must not depend on whichever final QC happens to be "
         "stored on the Block object");
    CHECK(body.find("self_qc") == std::string::npos);
    CHECK(normalized.find("if(") == std::string::npos);
}
