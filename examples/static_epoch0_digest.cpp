#include <fstream>
#include <filesystem>
#include <iostream>
#include <set>
#include <sstream>
#include <regex>
#include <stdexcept>
#include <string>
#include <vector>

#include "hotstuff/configuration.h"

namespace {
constexpr std::uint32_t kN = 31, kFanout = 5, kPipe = 2, kTrees = 21;

hotstuff::EpochDefinitionInput parse(const char *path) {
    const auto status = std::filesystem::status(path);
    if (!std::filesystem::is_regular_file(status) || std::filesystem::file_size(path) > 8192)
        throw std::runtime_error("tree file must be a bounded regular file");
    std::ifstream file(path);
    if (!file) throw std::runtime_error("cannot open tree file");
    std::vector<hotstuff::EpochTreeDefinition> trees;
    std::string line;
    while (std::getline(file, line)) {
        if (line.empty() || line.size() > 256) throw std::runtime_error("tree line is not canonical");
        static const std::regex grammar("fan:5 pipe:2(?: (?:0|[1-9][0-9]*)){31}");
        if (!std::regex_match(line, grammar)) throw std::runtime_error("tree line is not canonical");
        std::istringstream in(line); std::string fan, pipe; in >> fan >> pipe;
        if (fan != "fan:5" || pipe != "pipe:2") throw std::runtime_error("tree shape differs from N31/F5/P2");
        hotstuff::EpochTreeDefinition tree;
        tree.tree_id = static_cast<std::uint32_t>(trees.size()); tree.fanout = kFanout; tree.pipeline_stretch = kPipe;
        std::uint32_t member; while (in >> member) tree.members_breadth_first.push_back(member);
        if (!in.eof() || tree.members_breadth_first.size() != kN) throw std::runtime_error("tree membership is malformed");
        std::set<std::uint32_t> members(tree.members_breadth_first.begin(), tree.members_breadth_first.end());
        if (members.size() != kN || *members.begin() != 0 || *members.rbegin() != kN - 1) throw std::runtime_error("tree is not exact N31 membership");
        trees.push_back(std::move(tree));
    }
    if (file.bad()) throw std::runtime_error("tree file read failed");
    if (trees.size() != kTrees) throw std::runtime_error("tree file must contain exactly 21 trees");
    std::vector<hotstuff::ReplicaID> membership; for (std::uint32_t i = 0; i < kN; ++i) membership.push_back(i);
    return hotstuff::adaptive_v2_epoch_zero_input(membership, std::move(trees));
}
}
int main(int argc, char **argv) {
    if (argc != 3) { std::cerr << "usage: static-epoch0-digest <slow-roots|fast-roots> <tree-file>\n"; return 2; }
    try { const auto input = parse(argv[2]); const auto digest = hotstuff::compute_epoch_digest(input).to_hex();
        const std::string expected = std::string(argv[1]) == "slow-roots" ? "827e7626c74f8d815bca6ae5cbe10e312bc4f00f287e67d41277b8d689b21c0f" : std::string(argv[1]) == "fast-roots" ? "e640d31a0f4c394ca1ed50005c9f387fdc25de0b67c9ae8eae158275189e462e" : "";
        if (expected.empty() || digest != expected) throw std::runtime_error("tree file does not match named frozen W16 arm");
        std::cout << digest << '\n'; return 0; }
    catch (const std::exception &e) { std::cerr << "static-epoch0-digest: " << e.what() << '\n'; return 2; }
}
