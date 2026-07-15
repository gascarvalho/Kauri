#include <fstream>
#include <sstream>
#include <string>

#include "catch.hpp"

#ifndef KAURI_PROJECT_SOURCE_DIR
#error "KAURI_PROJECT_SOURCE_DIR must name the repository root"
#endif

namespace
{

std::string read_file(const std::string &path)
{
    std::ifstream input(path);
    REQUIRE(input.good());
    std::ostringstream contents;
    contents << input.rdbuf();
    return contents.str();
}

std::string function_body(const std::string &source,
                          const std::string &begin,
                          const std::string &end)
{
    const auto first = source.find(begin);
    REQUIRE(first != std::string::npos);
    const auto last = source.find(end, first + begin.size());
    REQUIRE(last != std::string::npos);
    return source.substr(first, last - first);
}

} // namespace

TEST_CASE("HotStuffBase routes delivery through the production orchestrator",
          "[rem-a06-01][orchestration][hotstuff][integration][red]")
{
    const std::string root = KAURI_PROJECT_SOURCE_DIR;
    const auto header = read_file(root + "/include/hotstuff/hotstuff.h");
    const auto source = read_file(root + "/src/hotstuff.cpp");

    INFO("HotStuffBase must own the extracted production orchestrator");
    CHECK(header.find(
              "BlockDeliveryOrchestrator blk_delivery_orchestrator") !=
          std::string::npos);

    const auto external = function_body(
        source,
        "bool HotStuffBase::on_deliver_blk",
        "promise_t HotStuffBase::async_fetch_blk");
    INFO("The real external path must delegate core delivery and exception "
         "cleanup to BlockDeliveryOrchestrator::external_delivery");
    CHECK(external.find("blk_delivery_orchestrator.external_delivery") !=
          std::string::npos);
    CHECK(external.find("blk_delivery_finalizer.external_result") ==
          std::string::npos);

    const auto asynchronous = function_body(
        source,
        "promise_t HotStuffBase::async_deliver_blk",
        "void HotStuffBase::propose_handler");
    INFO("The real async path must delegate fetch, verification, QC, parents, "
         "terminal competition, and timing to async_delivery");
    CHECK(asynchronous.find("blk_delivery_orchestrator.async_delivery") !=
          std::string::npos);
    INFO("The runtime plan must use Block's asynchronous verification "
         "overload before applying the later null-QC fetch guard");
    CHECK(asynchronous.find("block->verify(this, vpool)") !=
          std::string::npos);
    CHECK(asynchronous.find(
              "fetched block has no quorum certificate") !=
          std::string::npos);
    const auto delivered_fast_path = function_body(
        asynchronous,
        "if (storage->is_blk_delivered(blk_hash))",
        "const auto elapsed");
    INFO("The already-delivered fast path must retain the block hash by value");
    CHECK(delivered_fast_path.find("[this, blk_hash]") !=
          std::string::npos);
    CHECK(delivered_fast_path.find("[this, &blk_hash]") ==
          std::string::npos);
    CHECK(asynchronous.find("blk_delivery_lifecycle.request") ==
          std::string::npos);
    CHECK(asynchronous.find("blk_delivery_finalizer.async_success") ==
          std::string::npos);
}
