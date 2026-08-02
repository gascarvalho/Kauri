/**
 * Copyright 2026 Goncalo Carvalho
 *
 * Licensed under the Apache License, Version 2.0 (the "License");
 * you may not use this file except in compliance with the License.
 * You may obtain a copy of the License at
 *
 *     http://www.apache.org/licenses/LICENSE-2.0
 *
 * Unless required by applicable law or agreed to in writing, software
 * distributed under the License is distributed on an "AS IS" BASIS,
 * WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
 * See the License for the specific language governing permissions and
 * limitations under the License.
 */

#include <charconv>
#include <cstdint>
#include <iostream>
#include <stdexcept>
#include <string>
#include <system_error>
#include <vector>

#include "hotstuff/adaptation_manager_profile.h"

namespace
{

std::uint32_t parse_positive_u32(
    const char *raw,
    const char *field)
{
    const std::string text(raw == nullptr ? "" : raw);
    if (text.empty() ||
        (text.size() > 1 && text.front() == '0'))
    {
        throw std::invalid_argument(
            std::string(field) +
            " must be a canonical positive unsigned decimal");
    }

    std::uint32_t value{0};
    const auto parsed = std::from_chars(
        text.data(), text.data() + text.size(), value, 10);
    if (parsed.ec != std::errc{} ||
        parsed.ptr != text.data() + text.size() ||
        value == 0)
    {
        throw std::invalid_argument(
            std::string(field) +
            " must be a canonical positive unsigned decimal");
    }
    return value;
}

void print_members(std::ostream &output,
                   const std::vector<hotstuff::ReplicaID> &members)
{
    output << '[';
    for (std::size_t index = 0; index < members.size(); ++index)
    {
        if (index != 0)
            output << ',';
        output << members[index];
    }
    output << ']';
}

void print_json(
    std::ostream &output,
    const hotstuff::AdaptiveV2ManagerRuntimeShape &shape,
    const std::vector<hotstuff::ReplicaID> &membership,
    const hotstuff::EpochDefinitionInput &epoch)
{
    const auto digest = hotstuff::compute_epoch_digest(epoch);
    const auto canonical = hotstuff::canonical_serialize_epoch(epoch);

    output
        << "{\"schema\":\"kauri-adaptive-v2-epoch-profile-digest-v1\""
        << ",\"replica_count\":" << shape.quorum.replica_count
        << ",\"fault_threshold\":" << shape.quorum.fault_threshold
        << ",\"quorum\":" << shape.quorum.quorum
        << ",\"fanout\":" << shape.tree_shape.fanout
        << ",\"pipeline_stretch\":"
        << shape.tree_shape.pipeline_stretch
        << ",\"membership\":";
    print_members(output, membership);
    output
        << ",\"epoch_zero\":{\"schema_version\":"
        << epoch.schema_version
        << ",\"epoch_number\":" << epoch.epoch_number
        << ",\"previous_epoch_digest\":\""
        << epoch.previous_epoch_digest.to_hex()
        << "\",\"membership_digest\":\""
        << epoch.membership_digest.to_hex()
        << "\",\"activation_height\":" << epoch.activation_height
        << ",\"generation_seed\":" << epoch.generation_seed
        << ",\"policy_version\":\"" << epoch.policy_version
        << "\",\"evidence_snapshot_id\":\""
        << epoch.evidence_snapshot_id
        << "\",\"evidence_cutoff\":" << epoch.evidence_cutoff
        << ",\"canonical_size_bytes\":" << canonical.size()
        << ",\"epoch_digest\":\"" << digest.to_hex()
        << "\",\"tree_count\":" << epoch.trees.size()
        << ",\"trees\":[";

    for (std::size_t index = 0; index < epoch.trees.size(); ++index)
    {
        const auto &tree = epoch.trees[index];
        if (index != 0)
            output << ',';
        output
            << "{\"tree_id\":" << tree.tree_id
            << ",\"fanout\":" << tree.fanout
            << ",\"pipeline_stretch\":" << tree.pipeline_stretch
            << ",\"members_breadth_first\":";
        print_members(output, tree.members_breadth_first);
        output << ",\"wait_exempt_leaves\":";
        print_members(output, tree.wait_exempt_leaves);
        output << '}';
    }
    output << "]}}\n";
}

} // namespace

int main(int argc, char **argv)
{
    if (argc != 4)
    {
        std::cerr
            << "usage: epoch-profile-digest "
            << "<replica-count> <fanout> <pipeline-stretch>\n";
        return 2;
    }

    try
    {
        const auto replica_count =
            parse_positive_u32(argv[1], "replica count");
        const auto fanout = parse_positive_u32(argv[2], "fanout");
        const auto pipeline_stretch =
            parse_positive_u32(argv[3], "pipeline stretch");
        if (replica_count >
            hotstuff::kMaximumAdaptiveV2ManagerMembers)
        {
            throw std::invalid_argument(
                "replica count exceeds the adaptive-v2 manager bound");
        }

        std::vector<hotstuff::ReplicaID> membership;
        membership.reserve(replica_count);
        for (std::uint32_t member = 0;
             member < replica_count;
             ++member)
        {
            membership.push_back(member);
        }

        const auto shape =
            hotstuff::derive_adaptive_v2_manager_runtime_shape(
                membership, fanout, pipeline_stretch);
        const auto epoch =
            hotstuff::derive_adaptive_v2_cyclic_epoch_zero(
                membership, fanout, pipeline_stretch);
        if (!shape.has_value() || !epoch.has_value())
        {
            throw std::invalid_argument(
                "arguments must define a bounded contiguous N=3f+1 "
                "adaptive-v2 manager shape");
        }

        print_json(std::cout, *shape, membership, *epoch);
        return 0;
    }
    catch (const std::exception &error)
    {
        std::cerr << "epoch-profile-digest: " << error.what() << '\n';
        return 2;
    }
}
