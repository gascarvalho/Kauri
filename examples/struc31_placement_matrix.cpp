/*
 * Deterministic component/model matrix for STRUC31.
 *
 * This executable deliberately exercises only tree_policy.  It does not
 * launch replicas or describe constrained members as observed failures.
 */

#include "hotstuff/adaptation.h"
#include "hotstuff/tree_policy.h"

#include <algorithm>
#include <cstdint>
#include <exception>
#include <iomanip>
#include <iostream>
#include <map>
#include <numeric>
#include <optional>
#include <set>
#include <sstream>
#include <stdexcept>
#include <string>
#include <vector>

#include <sodium.h>

namespace
{
using hotstuff::AcceptedEvidenceRecord;
using hotstuff::AcceptedEvidenceView;
using hotstuff::AdaptationEpochId;
using hotstuff::AdaptationPolicy;
using hotstuff::AdaptationSnapshot;
using hotstuff::BaselineRoot;
using hotstuff::FaultContainmentPolicy;
using hotstuff::ReplicaID;
using hotstuff::ResponseObservation;
using hotstuff::ResponseOutcome;
using hotstuff::ExpectedMessageType;
using hotstuff::TreePlacementInput;
using hotstuff::TreePlacementResult;
using hotstuff::TreeShape;
using hotstuff::uint256_t;

constexpr std::size_t kMembers = 31;
constexpr std::uint32_t kTrees = 21;
constexpr std::uint32_t kQuorum = 21;
constexpr std::uint32_t kPipelineStretch = 2;
constexpr std::uint64_t kGenerationSeed = 0x53545255433331ULL;

uint256_t digest(const std::string &text)
{
    return hotstuff::DataStream(text).get_hash();
}

std::vector<ReplicaID> membership()
{
    std::vector<ReplicaID> result(kMembers);
    std::iota(result.begin(), result.end(), ReplicaID{0});
    return result;
}

AcceptedEvidenceView evidence_view(const std::vector<AcceptedEvidenceRecord> &records)
{
    return {records.empty() ? nullptr : records.data(), records.size()};
}

AdaptationSnapshot responsive_snapshot()
{
    const auto members = membership();
    const AdaptationEpochId epoch{31, digest("struc31-model-epoch")};
    AdaptationPolicy policy;
    policy.attempt_window = 8;
    policy.minimum_attempts = 8;
    policy.trailing_timeout_streak = 3;

    std::vector<AcceptedEvidenceRecord> records;
    records.reserve(kMembers * 8);
    std::uint64_t sequence = 0;
    for (const auto replica : members)
    {
        for (std::uint32_t attempt = 0; attempt < 8; ++attempt)
        {
            ResponseObservation observation;
            observation.schema_version = hotstuff::kResponseObservationSchemaVersion;
            observation.reporter_id = 0;
            observation.observed_replica_id = replica;
            observation.configuration = {epoch.epoch_number, attempt % 3, epoch.epoch_digest};
            observation.block_hash = digest("struc31-model-block-" + std::to_string(sequence));
            observation.expected_message_type = ExpectedMessageType::direct_vote;
            observation.deadline_duration_us = 1000;
            observation.reporter_sequence = sequence + 1;
            observation.reporter_monotonic_ns = (sequence + 1) * 1000;
            observation.outcome = ResponseOutcome::on_time;
            observation.response_duration_us = 10 + replica;
            observation.signer_set = {replica};
            observation.observation_id = hotstuff::compute_response_observation_id(
                observation.attempt_identity());
            records.push_back({++sequence, observation});
        }
    }
    return hotstuff::build_adaptation_snapshot(
        members, epoch, evidence_view(records), sequence, policy, kGenerationSeed);
}

std::vector<BaselineRoot> baseline_roots()
{
    std::vector<BaselineRoot> roots;
    roots.reserve(kTrees);
    for (std::uint32_t tree = 0; tree < kTrees; ++tree)
        roots.push_back({tree, static_cast<ReplicaID>(tree)});
    return roots;
}

std::vector<ReplicaID> cohort(const std::string &kind, std::uint32_t k)
{
    std::vector<ReplicaID> result;
    result.reserve(k);
    const ReplicaID start = kind == "baseline" ? 0 : 21;
    for (std::uint32_t index = 0; index < k; ++index)
        result.push_back(start + index);
    return result;
}

std::size_t first_leaf_index(std::size_t count, std::uint32_t fanout)
{
    return count == 1 ? 0 : ((count - 2) / fanout) + 1;
}

std::string hex_sha256(const std::string &bytes)
{
    unsigned char digest_bytes[crypto_hash_sha256_BYTES];
    crypto_hash_sha256(digest_bytes,
                       reinterpret_cast<const unsigned char *>(bytes.data()),
                       bytes.size());
    std::ostringstream output;
    for (const auto value : digest_bytes)
        output << std::hex << std::setw(2) << std::setfill('0')
               << static_cast<unsigned int>(value);
    return output.str();
}

std::string json_string(const std::string &value)
{
    std::ostringstream output;
    output << '"';
    for (const unsigned char character : value)
    {
        switch (character)
        {
        case '"': output << "\\\""; break;
        case '\\': output << "\\\\"; break;
        case '\n': output << "\\n"; break;
        case '\r': output << "\\r"; break;
        case '\t': output << "\\t"; break;
        default:
            if (character < 0x20)
                throw std::invalid_argument("STRUC31 strings must be printable");
            output << character;
        }
    }
    output << '"';
    return output.str();
}

template <typename T>
void json_array(std::ostringstream &output, const std::vector<T> &values)
{
    output << '[';
    for (std::size_t index = 0; index < values.size(); ++index)
    {
        if (index != 0)
            output << ',';
        output << values.at(index);
    }
    output << ']';
}

std::string source_hash_placeholder(const std::string &name)
{
    // A runtime binary cannot safely assert the bytes of a source file that
    // may have changed after compilation. The build wrapper supplies these
    // two values through --source-hash below; self-test binds stable markers.
    return "unbound:" + name;
}

struct Arguments
{
    bool self_test{false};
    bool canonical_stdout{false};
    std::string revision;
    std::string producer_source_sha256;
    std::string tree_policy_source_sha256;
    std::string producer_binary_sha256;
};

Arguments parse_arguments(int argc, char **argv)
{
    Arguments arguments;
    for (int index = 1; index < argc; ++index)
    {
        const std::string value(argv[index]);
        if (value == "--self-test")
        {
            if (arguments.self_test)
                throw std::invalid_argument("--self-test may appear only once");
            arguments.self_test = true;
        }
        else if (value == "--canonical-stdout-v1")
        {
            if (arguments.canonical_stdout)
                throw std::invalid_argument("--canonical-stdout-v1 may appear only once");
            arguments.canonical_stdout = true;
        }
        else if (value == "--revision" || value == "--producer-source-sha256" ||
                 value == "--tree-policy-source-sha256" || value == "--producer-binary-sha256")
        {
            if (++index == argc)
                throw std::invalid_argument(value + " requires a value");
            const std::string supplied(argv[index]);
            if (value == "--revision") {
                if (!arguments.revision.empty()) throw std::invalid_argument("--revision may appear only once");
                arguments.revision = supplied;
            }
            if (value == "--producer-source-sha256") {
                if (!arguments.producer_source_sha256.empty()) throw std::invalid_argument("--producer-source-sha256 may appear only once");
                arguments.producer_source_sha256 = supplied;
            }
            if (value == "--tree-policy-source-sha256") {
                if (!arguments.tree_policy_source_sha256.empty()) throw std::invalid_argument("--tree-policy-source-sha256 may appear only once");
                arguments.tree_policy_source_sha256 = supplied;
            }
            if (value == "--producer-binary-sha256") {
                if (!arguments.producer_binary_sha256.empty()) throw std::invalid_argument("--producer-binary-sha256 may appear only once");
                arguments.producer_binary_sha256 = supplied;
            }
        }
        else
        {
            throw std::invalid_argument("unknown option: " + value);
        }
    }
    if (!arguments.self_test && (!arguments.canonical_stdout || arguments.revision.empty() ||
        arguments.producer_source_sha256.empty() || arguments.tree_policy_source_sha256.empty() ||
        arguments.producer_binary_sha256.empty()))
        throw std::invalid_argument("--revision and all --*-sha256 values are required");
    return arguments;
}

void append_valid_cell(std::ostringstream &output, const AdaptationSnapshot &snapshot,
                       std::uint32_t fanout, const std::string &kind, std::uint32_t k,
                       const std::vector<ReplicaID> &constraints)
{
    const auto members = membership();
    const TreePlacementInput input{members, TreeShape{fanout, kPipelineStretch, kTrees},
                                   kGenerationSeed, "struc31-model-v1"};
    const auto result = [&]() {
        try
        {
            return hotstuff::build_tree_placement(
                input, snapshot, FaultContainmentPolicy{baseline_roots(), constraints});
        }
        catch (const std::invalid_argument &error)
        {
            throw std::runtime_error("STRUC31 valid cell fanout=" + std::to_string(fanout) +
                                     " cohort=" + kind + " k=" + std::to_string(k) +
                                     ": " + error.what());
        }
    }();
    const auto leaf_start = first_leaf_index(kMembers, fanout);
    auto canonical_constraints = constraints;
    std::sort(canonical_constraints.begin(), canonical_constraints.end());
    std::set<ReplicaID> constrained(canonical_constraints.begin(), canonical_constraints.end());
    std::vector<ReplicaID> roots;
    std::vector<ReplicaID> fallback_roots;
    std::size_t preserved = 0;
    for (const auto &decision : result.explanation().root_decisions)
    {
        roots.push_back(decision.chosen_root);
        if (decision.chosen_root == decision.tree_id)
            ++preserved;
        else
            fallback_roots.push_back(decision.chosen_root);
    }
    std::ostringstream topology;
    std::ostringstream explanation;
    for (const auto root : roots) explanation << root << ',';
    explanation << ';';
    for (const auto fallback : fallback_roots) explanation << fallback << ',';
    for (const auto &tree : result.trees())
    {
        topology << tree.tree_id << ':';
        for (const auto member : tree.members_breadth_first) topology << member << ',';
        topology << ';';
        std::vector<std::pair<ReplicaID, std::size_t>> constrained_positions;
        for (std::size_t position = 0; position < tree.members_breadth_first.size(); ++position)
            if (constrained.count(tree.members_breadth_first.at(position)) != 0)
                constrained_positions.emplace_back(tree.members_breadth_first.at(position), position);
        std::sort(constrained_positions.begin(), constrained_positions.end());
        for (const auto &[replica, position] : constrained_positions)
            explanation << tree.tree_id << ':' << replica << ':' << position << ';';
    }
    output << "{\"fanout\":" << fanout << ",\"cohort\":" << json_string(kind)
           << ",\"k\":" << k << ",\"ineligible_ids\":";
    json_array(output, canonical_constraints);
    output << ",\"eligible_count\":" << (kMembers - k)
           << ",\"first_leaf_index\":" << leaf_start
           << ",\"leaf_capacity\":" << (kMembers - leaf_start)
           << ",\"status\":\"valid\",\"roots\":";
    json_array(output, roots);
    output << ",\"preserved_baseline_count\":" << preserved
           << ",\"fallback_roots\":";
    json_array(output, fallback_roots);
    output << ",\"topology_sha256\":" << json_string(hex_sha256(topology.str()))
           << ",\"explanation_sha256\":" << json_string(hex_sha256(explanation.str()));
    output << ",\"trees\":[";
    for (std::size_t tree_index = 0; tree_index < result.trees().size(); ++tree_index)
    {
        if (tree_index != 0) output << ',';
        const auto &tree = result.trees().at(tree_index);
        output << "{\"tree_id\":" << tree.tree_id << ",\"members_breadth_first\":";
        json_array(output, tree.members_breadth_first);
        output << ",\"constrained_positions\":[";
        std::vector<std::pair<ReplicaID, std::size_t>> constrained_positions;
        for (std::size_t position = 0; position < tree.members_breadth_first.size(); ++position)
            if (constrained.count(tree.members_breadth_first.at(position)) != 0)
                constrained_positions.emplace_back(tree.members_breadth_first.at(position), position);
        std::sort(constrained_positions.begin(), constrained_positions.end());
        bool first = true;
        for (const auto &[replica, position] : constrained_positions)
        {
            if (!first) output << ',';
            first = false;
            output << "{\"replica_id\":" << replica
                   << ",\"position\":" << position << '}';
        }
        output << "]}";
    }
    output << "]}";
}

void append_boundary_cell(std::ostringstream &output, const AdaptationSnapshot &snapshot,
                          std::uint32_t fanout, const std::string &kind)
{
    const auto constraints = cohort(kind, 11);
    const TreePlacementInput input{membership(), TreeShape{fanout, kPipelineStretch, kTrees},
                                   kGenerationSeed, "struc31-model-v1"};
    bool rejected = false;
    try
    {
        static_cast<void>(hotstuff::build_tree_placement(
            input, snapshot, FaultContainmentPolicy{baseline_roots(), constraints}));
    }
    catch (const std::invalid_argument &error)
    {
        rejected = std::string(error.what()) == "tree policy constraints leave insufficient eligible roots";
        if (!rejected) throw;
    }
    if (!rejected)
        throw std::runtime_error("STRUC31 k=11 boundary did not fail closed");
    output << "{\"fanout\":" << fanout << ",\"cohort\":" << json_string(kind)
           << ",\"k\":11,\"ineligible_ids\":";
    json_array(output, constraints);
    output << ",\"eligible_count\":20,\"first_leaf_index\":"
           << first_leaf_index(kMembers, fanout) << ",\"leaf_capacity\":"
           << (kMembers - first_leaf_index(kMembers, fanout))
           << ",\"status\":\"insufficient_eligible_roots\",\"trees\":[]}";
}

std::string matrix_json(const Arguments &arguments, bool reverse_constraints = false)
{
    const auto snapshot = responsive_snapshot();
    std::ostringstream output;
    output << "{\"schema\":\"struc31-placement-matrix-v1\",\"producer\":{\"revision\":"
           << json_string(arguments.revision.empty() ? "self-test" : arguments.revision)
           << ",\"producer_source_sha256\":" << json_string(
                  arguments.producer_source_sha256.empty() ? source_hash_placeholder("producer") : arguments.producer_source_sha256)
           << ",\"tree_policy_source_sha256\":" << json_string(
                  arguments.tree_policy_source_sha256.empty() ? source_hash_placeholder("tree_policy") : arguments.tree_policy_source_sha256)
           << ",\"producer_binary_sha256\":" << json_string(
                  arguments.producer_binary_sha256.empty() ? source_hash_placeholder("binary") : arguments.producer_binary_sha256)
           << ",\"command\":" << json_string(
                  "struc31-placement-matrix --canonical-stdout-v1 --revision " +
                  (arguments.revision.empty() ? std::string("self-test") : arguments.revision) +
                  " --producer-source-sha256 " +
                  (arguments.producer_source_sha256.empty() ? source_hash_placeholder("producer") : arguments.producer_source_sha256) +
                  " --tree-policy-source-sha256 " +
                  (arguments.tree_policy_source_sha256.empty() ? source_hash_placeholder("tree_policy") : arguments.tree_policy_source_sha256) +
                  " --producer-binary-sha256 " +
                  (arguments.producer_binary_sha256.empty() ? source_hash_placeholder("binary") : arguments.producer_binary_sha256))
           << "},\"input_manifest\":{\"membership\":";
    json_array(output, membership());
    output << ",\"required_distinct_tree_roots\":21,\"consensus_quorum_Q\":21"
           << ",\"pipeline_stretch\":2,\"fanouts\":[2,5]"
           << ",\"constraint_basis\":\"model_policy_constrained_leaves\""
           << ",\"coverage_scope\":\"declared-40-cell-matrix-not-all-subsets-v1\"}"
           << ",\"cells\":[";
    bool first = true;
    for (const auto fanout : {2U, 5U})
    {
        for (const auto &kind : {std::string("nonbaseline"), std::string("baseline")})
        {
            for (std::uint32_t k = 1; k <= 10; ++k)
            {
                if (!first) output << ',';
                first = false;
                auto constraints = cohort(kind, k);
                if (reverse_constraints) std::reverse(constraints.begin(), constraints.end());
                append_valid_cell(output, snapshot, fanout, kind, k, constraints);
            }
        }
    }
    output << "],\"boundary_cells\":[";
    first = true;
    for (const auto fanout : {2U, 5U})
    {
        if (!first) output << ',';
        first = false;
        append_boundary_cell(output, snapshot, fanout, "baseline");
    }
    output << "]}";
    return output.str();
}

} // namespace

int main(int argc, char **argv)
{
    try
    {
        if (sodium_init() < 0)
            throw std::runtime_error("libsodium initialization failed");
        const auto arguments = parse_arguments(argc, argv);
        const auto first = matrix_json(arguments);
        if (arguments.self_test)
        {
            const auto second = matrix_json(arguments);
            if (first != second)
                throw std::runtime_error("STRUC31 matrix is not byte deterministic");
            // Policy constraints are set-valued: ordering must not change output.
            if (first != matrix_json(arguments, true))
                throw std::runtime_error("STRUC31 reordered constraints changed output");
            return 0;
        }
        std::cout << first << '\n';
        return 0;
    }
    catch (const std::exception &error)
    {
        std::cerr << "struc31-placement-matrix: " << error.what() << '\n';
        return 2;
    }
}
