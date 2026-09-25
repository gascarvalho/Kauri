/* S1 component test: define and activate one predecessor-bound N31 sham epoch.
 * This is not a 31-process live-run claim. */

#include <algorithm>
#include <cstdint>
#include <numeric>
#include <vector>

#include "catch.hpp"
#include "hotstuff/epoch_activation.h"
#include "hotstuff/epoch_change_bundle.h"

namespace
{
using hotstuff::EpochChangeBundleLimits;
using hotstuff::EpochChangeIssuer;
using hotstuff::EpochChangeIssuerId;
using hotstuff::EpochChangePayload;
using hotstuff::EpochDefinition;
using hotstuff::EpochDefinitionInput;
using hotstuff::EpochProtocolMode;
using hotstuff::EpochStore;
using hotstuff::EpochTreeDefinition;
using hotstuff::EpochValidationContext;
using hotstuff::EpochWireLimits;
using hotstuff::PrivKeySecp256k1;
using hotstuff::ReplicaEpochActivation;
using hotstuff::ReplicaID;

constexpr EpochChangeIssuerId kIssuerId = 17;

std::vector<ReplicaID> members()
{
    std::vector<ReplicaID> result(31);
    std::iota(result.begin(), result.end(), ReplicaID{0});
    return result;
}

EpochDefinitionInput epoch_zero()
{
    EpochDefinitionInput input;
    input.schema_version = hotstuff::kEpochDefinitionSchemaVersionV2;
    input.epoch_number = 0;
    input.membership_digest = hotstuff::canonical_membership_digest(members());
    for (std::uint32_t tree_id = 0; tree_id < 21; ++tree_id)
    {
        std::vector<ReplicaID> ordered = members();
        std::rotate(ordered.begin(), ordered.begin() + tree_id, ordered.end());
        input.trees.push_back({tree_id, 5, 2, std::move(ordered), {}});
    }
    input.activation_height = 0;
    input.generation_seed = 0x53544154494331ULL;
    input.policy_version = "static-resource-e0-v1";
    input.evidence_snapshot_id = "static-resource-all-live-e0";
    input.evidence_cutoff = 0;
    return input;
}

PrivKeySecp256k1 issuer_key()
{
    PrivKeySecp256k1 key;
    key.from_hex("4aede145d13021fb43c938bced67511a7740c05786d3e0b94ffbdaa7f15afc57");
    return key;
}

EpochChangeBundleLimits limits()
{
    return {128 * 1024, 4096, EpochWireLimits{96 * 1024, 32, 64, 128, 31}};
}

EpochDefinitionInput sham_successor(const EpochDefinition &predecessor)
{
    EpochDefinitionInput input;
    input.schema_version = hotstuff::kEpochDefinitionSchemaVersionV2;
    input.epoch_number = predecessor.epoch_number() + 1;
    input.previous_epoch_digest = predecessor.epoch_digest();
    input.membership_digest = predecessor.membership_digest();
    input.trees = predecessor.trees(); // exact ordered tree and role projection
    input.activation_height = 0;
    input.generation_seed = predecessor.generation_seed();
    input.policy_version = "static-resource-sham-e1-v1";
    input.evidence_snapshot_id = "static-resource-all-live-e1";
    input.evidence_cutoff = 248;
    return input;
}

bool same_ordered_trees(const std::vector<EpochTreeDefinition> &left,
                        const std::vector<EpochTreeDefinition> &right)
{
    if (left.size() != right.size()) return false;
    for (std::size_t index = 0; index < left.size(); ++index)
    {
        const auto &a = left.at(index);
        const auto &b = right.at(index);
        if (a.tree_id != b.tree_id || a.fanout != b.fanout ||
            a.pipeline_stretch != b.pipeline_stretch ||
            a.members_breadth_first != b.members_breadth_first ||
            a.wait_exempt_leaves != b.wait_exempt_leaves)
            return false;
    }
    return true;
}

} // namespace

TEST_CASE("static-resource N31 sham definition activates an exact tree copy",
          "[integration][static-resource][sham][n31]")
{
    EpochStore store(members());
    const auto &epoch0 = store.stage(epoch_zero(), EpochValidationContext{0, 0, {}});
    auto input = sham_successor(epoch0);
    const auto sham_digest = hotstuff::compute_epoch_digest(input);
    input.epoch_digest = sham_digest;

    const auto staged = store.stage_available_v2(input, epoch0);
    REQUIRE(staged.disposition == hotstuff::DefinitionAvailabilityDisposition::staged);
    REQUIRE(staged.definition != nullptr);
    const auto &epoch1 = *staged.definition;
    REQUIRE(epoch1.epoch_number() == 1);
    CHECK(epoch1.previous_epoch_digest() == epoch0.epoch_digest());
    CHECK(epoch1.epoch_digest() == sham_digest);
    CHECK(epoch1.epoch_digest() != epoch0.epoch_digest());
    CHECK(same_ordered_trees(epoch1.trees(), epoch0.trees()));

    const auto key = issuer_key();
    const auto command = hotstuff::authorize_epoch_change(
        EpochChangePayload{1, epoch0.epoch_digest(), epoch1.epoch_digest(), 5}, kIssuerId, key);
    CHECK(command.protocol_mode == EpochProtocolMode::adaptive_v2);
    CHECK(hotstuff::verify_epoch_change_signature(
        command, EpochChangeIssuer{kIssuerId, hotstuff::PubKeySecp256k1(key)}));
    const hotstuff::AdaptiveV2EpochChangeBundle bundle(command, input, limits());
    const auto decoded = hotstuff::decode_adaptive_v2_epoch_change_bundle(
        bundle.canonical_bytes(), limits());
    REQUIRE(decoded);
    CHECK(same_ordered_trees(decoded.value->definition().trees, epoch0.trees()));
    CHECK(decoded.value->command().payload.predecessor_epoch_digest == epoch0.epoch_digest());
    CHECK(decoded.value->command().payload.successor_epoch_digest == epoch1.epoch_digest());

    ReplicaEpochActivation replica(store, epoch0, 0);
    REQUIRE(replica.record_committed_v2(command, 100).disposition ==
            hotstuff::ActivationRecordDisposition::recorded);
    const auto activation = replica.on_v2_post_block_commit(105, epoch0.epoch_digest());
    REQUIRE(activation.transition == hotstuff::ActivationTransition::activated);
    REQUIRE(activation.effect.has_value());
    CHECK(activation.effect->definition == &epoch1);
    CHECK(activation.effect->configuration.epoch_digest == epoch1.epoch_digest());
    CHECK(replica.admits_new_proposals());
}
