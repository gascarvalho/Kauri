#include <algorithm>
#include <cstddef>
#include <cstdint>
#include <fstream>
#include <iterator>
#include <memory>
#include <stdexcept>
#include <string>
#include <type_traits>
#include <utility>
#include <vector>

#include "catch.hpp"
#include "hotstuff/adaptive_v2_manager_convergence.h"
#include "hotstuff/adaptive_v2_reporting_outbox.h"
#include "hotstuff/epoch_change_bundle.h"

#if __has_include("hotstuff/adaptive_v2_convergence_ack_wire.h")
#include "hotstuff/adaptive_v2_convergence_ack_wire.h"
#define KAURI_HAS_ADAPTIVE_V2_CONVERGENCE_ACK_WIRE 1
#else
#define KAURI_HAS_ADAPTIVE_V2_CONVERGENCE_ACK_WIRE 0
#endif

namespace
{

using hotstuff::AdaptiveV2EpochActivatedObservation;
using hotstuff::AdaptiveV2EpochChangeBundle;
using hotstuff::AdaptiveV2EpochChangeCommittedObservation;
using hotstuff::AdaptiveV2EpochChangeIdentity;
using hotstuff::AdaptiveV2ManagerConvergence;
using hotstuff::AdaptiveV2ManagerConvergenceConfig;
using hotstuff::AdaptiveV2ManagerConvergenceDisposition;
using hotstuff::AdaptiveV2ManagerConvergenceStatus;
using hotstuff::AdaptiveV2ManagerDeliveryRequest;
using hotstuff::AuthorizedEpochChange;
using hotstuff::DataStream;
using hotstuff::EpochChangeBundleLimits;
using hotstuff::EpochChangePayload;
using hotstuff::EpochDefinitionInput;
using hotstuff::EpochTreeDefinition;
using hotstuff::EpochWireLimits;
using hotstuff::PrivKeySecp256k1;
using hotstuff::ReplicaID;
using hotstuff::bytearray_t;
using hotstuff::uint256_t;

constexpr std::uint32_t kIssuerId = 17;
const std::vector<ReplicaID> kMembership{0, 1, 2, 3, 4, 5, 6};
const std::vector<ReplicaID> kSurvivors{2, 3, 4, 5, 6};

uint256_t digest(const std::string &label)
{
    return DataStream(label).get_hash();
}

EpochTreeDefinition tree(
    std::uint32_t id,
    std::vector<ReplicaID> members,
    std::vector<ReplicaID> wait_exempt)
{
    return {id, 2, 2, std::move(members), std::move(wait_exempt)};
}

EpochDefinitionInput successor_definition()
{
    EpochDefinitionInput input;
    input.schema_version = hotstuff::kEpochDefinitionSchemaVersionV2;
    input.epoch_number = 1;
    input.previous_epoch_digest = digest("convergence-epoch-zero");
    input.membership_digest =
        hotstuff::canonical_membership_digest(kMembership);
    input.trees = {
        tree(0, {2, 3, 4, 5, 6, 0, 1}, {0, 1}),
        tree(1, {3, 4, 5, 2, 6, 0, 1}, {0, 1})};
    input.activation_height = 0;
    input.generation_seed = 88;
    input.policy_version = "adaptive-v2-containment";
    input.evidence_snapshot_id = "accepted-cutoff-20";
    input.evidence_cutoff = 20;
    return input;
}

PrivKeySecp256k1 private_key()
{
    PrivKeySecp256k1 key;
    key.from_hex(
        "4aede145d13021fb43c938bced67511a7740c05786d3e0b94ffbdaa7f15afc57");
    return key;
}

EpochChangeBundleLimits bundle_limits()
{
    return {
        8192,
        1024,
        EpochWireLimits{4096, 8, 16, 128, 2}};
}

AuthorizedEpochChange command_for(const EpochDefinitionInput &definition)
{
    return hotstuff::authorize_epoch_change(
        EpochChangePayload{
            definition.epoch_number,
            definition.previous_epoch_digest,
            hotstuff::compute_epoch_digest(definition),
            5},
        kIssuerId,
        private_key());
}

AdaptiveV2EpochChangeBundle canonical_bundle()
{
    auto definition = successor_definition();
    return AdaptiveV2EpochChangeBundle(
        command_for(definition), definition, bundle_limits());
}

AdaptiveV2EpochChangeIdentity committed_identity(
    const AdaptiveV2EpochChangeBundle &bundle,
    std::uint64_t command_height = 760,
    const std::string &block_label = "convergence-command-block")
{
    const auto &payload = bundle.command().payload;
    return {
        payload.successor_epoch_number - 1,
        payload.predecessor_epoch_digest,
        payload.successor_epoch_number,
        payload.successor_epoch_digest,
        hotstuff::epoch_change_payload_digest(payload),
        command_height,
        digest(block_label),
        payload.activation_delay_blocks,
        command_height + payload.activation_delay_blocks};
}

AdaptiveV2EpochChangeCommittedObservation committed(
    ReplicaID source,
    const AdaptiveV2EpochChangeIdentity &identity)
{
    return {
        hotstuff::kAdaptiveV2ConvergenceObservationSchemaVersionV1,
        source,
        identity};
}

AdaptiveV2EpochActivatedObservation activated(
    ReplicaID source,
    const AdaptiveV2EpochChangeIdentity &identity)
{
    return {
        hotstuff::kAdaptiveV2ConvergenceObservationSchemaVersionV1,
        source,
        identity,
        identity.successor_epoch_number,
        identity.successor_epoch_digest};
}

template<typename Value, typename = void>
struct has_required_activations : std::false_type
{};

template<typename Value>
struct has_required_activations<
    Value,
    std::void_t<decltype(std::declval<Value>().required_activations)>>
    : std::true_type
{};

template<typename Value, typename = void>
struct has_winning_identity : std::false_type
{};

template<typename Value>
struct has_winning_identity<
    Value,
    std::void_t<decltype(
        std::declval<const Value &>().winning_identity())>>
    : std::true_type
{};

AdaptiveV2ManagerConvergenceConfig config(
    std::vector<ReplicaID> membership = kMembership)
{
    return {std::move(membership), 2, 2, 20};
}

using BundleConstructor = std::is_constructible<
    AdaptiveV2ManagerConvergence,
    const AdaptiveV2EpochChangeBundle &,
    AdaptiveV2ManagerConvergenceConfig,
    std::uint64_t>;
using LegacyIdentityConstructor = std::is_constructible<
    AdaptiveV2ManagerConvergence,
    bytearray_t,
    AdaptiveV2EpochChangeIdentity,
    AdaptiveV2ManagerConvergenceConfig,
    std::uint64_t>;

static_assert(
    !std::is_nothrow_constructible<
        AdaptiveV2ManagerConvergence,
        const AdaptiveV2EpochChangeBundle &,
        AdaptiveV2ManagerConvergenceConfig,
        std::uint64_t>::value,
    "bundle validation must propagate allocation failures");
static_assert(
    !std::is_nothrow_constructible<
        AdaptiveV2ManagerConvergence,
        bytearray_t,
        AdaptiveV2EpochChangeIdentity,
        AdaptiveV2ManagerConvergenceConfig,
        std::uint64_t>::value,
    "legacy validation must not terminate on allocation failure");

std::unique_ptr<AdaptiveV2ManagerConvergence> make_convergence(
    const AdaptiveV2EpochChangeBundle &bundle,
    AdaptiveV2ManagerConvergenceConfig configured = config(),
    std::uint64_t start_tick = 0)
{
    return std::make_unique<AdaptiveV2ManagerConvergence>(
        bundle, std::move(configured), start_tick);
}

const AdaptiveV2ManagerDeliveryRequest &request_for(
    const std::vector<AdaptiveV2ManagerDeliveryRequest> &requests,
    ReplicaID recipient)
{
    const auto found = std::find_if(
        requests.begin(), requests.end(),
        [recipient](const auto &request) {
            return request.recipient == recipient;
        });
    REQUIRE(found != requests.end());
    return *found;
}

void record_all_enqueues(
    AdaptiveV2ManagerConvergence &convergence,
    const std::vector<AdaptiveV2ManagerDeliveryRequest> &requests,
    bool enqueued)
{
    for (const auto &request : requests)
    {
        CHECK(convergence.record_enqueue_result(
                  request.recipient, request.attempt, enqueued) ==
              AdaptiveV2ManagerConvergenceDisposition::
                  advisory_enqueue_recorded);
    }
}

std::unique_ptr<AdaptiveV2ManagerConvergence> ready_convergence()
{
    const auto bundle = canonical_bundle();
    const auto identity = committed_identity(bundle);
    auto convergence = make_convergence(bundle);
    for (const auto replica : kSurvivors)
        convergence->observe_activation(replica, activated(replica, identity));
    return convergence;
}

template<typename Convergence>
void exercise_winning_identity_access()
{
    static_assert(
        noexcept(std::declval<const Convergence &>().winning_identity()),
        "winning convergence identity inspection must be observational");
    static_assert(
        std::is_same<
            decltype(std::declval<const Convergence &>()
                         .winning_identity()),
            const AdaptiveV2EpochChangeIdentity *>::value,
        "winning identity must be an immutable borrowed exact identity");

    const auto bundle = canonical_bundle();
    const auto winner = committed_identity(
        bundle, 811, "winning-full-identity");
    Convergence convergence(bundle, config(), 0);
    CHECK(convergence.winning_identity() == nullptr);

    for (const auto replica : kSurvivors)
    {
        CHECK(convergence.observe_activation(
                  replica, activated(replica, winner)) ==
              AdaptiveV2ManagerConvergenceDisposition::accepted);
    }

    REQUIRE(convergence.status() ==
            AdaptiveV2ManagerConvergenceStatus::ready_for_optimization);
    REQUIRE(convergence.winning_identity() != nullptr);
    CHECK(*convergence.winning_identity() == winner);
    CHECK(convergence.consume_ready_for_optimization());
    REQUIRE(convergence.winning_identity() != nullptr);
    CHECK(*convergence.winning_identity() == winner);
}

std::string read_source(const std::string &relative_path)
{
    const std::string path =
        std::string(KAURI_PROJECT_SOURCE_DIR) + "/" + relative_path;
    std::ifstream source(path);
    REQUIRE(source.good());
    return {
        std::istreambuf_iterator<char>(source),
        std::istreambuf_iterator<char>()};
}

} // namespace

TEST_CASE(
    "convergence starts from one canonical N7 bundle and derives Q5",
    "[adaptive-v2][manager-convergence][c3][bundle][n7][quorum]")
{
    CHECK(BundleConstructor::value);
    CHECK_FALSE(LegacyIdentityConstructor::value);
    CHECK_FALSE(has_required_activations<
                AdaptiveV2ManagerConvergenceConfig>::value);

    const auto bundle = canonical_bundle();
    auto convergence = make_convergence(bundle);
    const auto first = convergence->due_deliveries(0);
    REQUIRE(first.size() == kMembership.size());
    for (const auto replica : kMembership)
    {
        const auto &request = request_for(first, replica);
        REQUIRE(request.canonical_bundle_bytes != nullptr);
        CHECK(*request.canonical_bundle_bytes == bundle.canonical_bytes());
    }

    CHECK_THROWS_AS(
        make_convergence(
            bundle,
            config(kSurvivors)),
        std::invalid_argument);
}

TEST_CASE(
    "commit observations never bind the activation schedule",
    "[adaptive-v2][manager-convergence][c3][commit][advisory]")
{
    const auto bundle = canonical_bundle();
    const auto bogus_commit = committed_identity(
        bundle, 900, "structurally-valid-bogus-commit");
    const auto activated_identity = committed_identity(
        bundle, 760, "activation-quorum-command-block");
    auto convergence = make_convergence(bundle);

    CHECK(convergence->observe_commit(
              0, committed(0, bogus_commit)) ==
          AdaptiveV2ManagerConvergenceDisposition::accepted);
    CHECK(convergence->accepted_commit_count() == 1);
    CHECK(convergence->status() ==
          AdaptiveV2ManagerConvergenceStatus::awaiting_activations);

    for (const auto replica : kSurvivors)
    {
        CHECK(convergence->observe_activation(
                  replica, activated(replica, activated_identity)) ==
              AdaptiveV2ManagerConvergenceDisposition::accepted);
    }
    CHECK(convergence->accepted_activation_count() == 5);
    CHECK(convergence->status() ==
          AdaptiveV2ManagerConvergenceStatus::ready_for_optimization);
    CHECK(convergence->consume_ready_for_optimization());
    CHECK_FALSE(convergence->consume_ready_for_optimization());
}

TEST_CASE(
    "adaptive v2 convergence retries the identical frozen bundle",
    "[adaptive-v2][manager-convergence][c3][retry]")
{
    const auto bundle = canonical_bundle();
    auto convergence = make_convergence(bundle);

    const auto first = convergence->due_deliveries(0);
    REQUIRE(first.size() == kMembership.size());
    const auto &first_to_zero = request_for(first, 0);
    REQUIRE(first_to_zero.canonical_bundle_bytes != nullptr);
    CHECK(first_to_zero.attempt == 1);
    CHECK(*first_to_zero.canonical_bundle_bytes == bundle.canonical_bytes());
    const auto *const frozen_address =
        first_to_zero.canonical_bundle_bytes;

    CHECK(convergence->record_enqueue_result(0, 1, false) ==
          AdaptiveV2ManagerConvergenceDisposition::
              advisory_enqueue_recorded);
    CHECK(convergence->due_deliveries(1).empty());

    const auto retry = convergence->due_deliveries(2);
    const auto &retry_to_zero = request_for(retry, 0);
    REQUIRE(retry_to_zero.canonical_bundle_bytes != nullptr);
    CHECK(retry_to_zero.attempt == 2);
    CHECK(retry_to_zero.canonical_bundle_bytes == frozen_address);
    CHECK(*retry_to_zero.canonical_bundle_bytes == bundle.canonical_bytes());
}

TEST_CASE(
    "enqueue and commit observations remain advisory",
    "[adaptive-v2][manager-convergence][c3][authority]")
{
    const auto bundle = canonical_bundle();
    const auto identity = committed_identity(bundle);
    auto convergence = make_convergence(bundle);
    record_all_enqueues(
        *convergence, convergence->due_deliveries(0), true);

    for (const auto replica : kSurvivors)
    {
        CHECK(convergence->observe_commit(
                  replica, committed(replica, identity)) ==
              AdaptiveV2ManagerConvergenceDisposition::accepted);
    }
    CHECK(convergence->accepted_commit_count() == 5);
    CHECK(convergence->accepted_activation_count() == 0);
    CHECK(convergence->status() ==
          AdaptiveV2ManagerConvergenceStatus::awaiting_activations);
    CHECK_FALSE(convergence->consume_ready_for_optimization());
}

TEST_CASE(
    "exactly five matching N7 activations converge",
    "[adaptive-v2][manager-convergence][c3][n7][quorum][ready]")
{
    const auto bundle = canonical_bundle();
    const auto identity = committed_identity(bundle);
    auto convergence = make_convergence(bundle);

    for (std::size_t index = 0; index < 4; ++index)
    {
        const auto replica = kSurvivors[index];
        CHECK(convergence->observe_activation(
                  replica, activated(replica, identity)) ==
              AdaptiveV2ManagerConvergenceDisposition::accepted);
    }
    CHECK(convergence->accepted_activation_count() == 4);
    CHECK(convergence->status() ==
          AdaptiveV2ManagerConvergenceStatus::awaiting_activations);
    CHECK_FALSE(convergence->consume_ready_for_optimization());

    CHECK(convergence->observe_activation(
              2, activated(2, identity)) ==
          AdaptiveV2ManagerConvergenceDisposition::duplicate);
    CHECK(convergence->accepted_activation_count() == 4);

    CHECK(convergence->observe_activation(
              6, activated(6, identity)) ==
          AdaptiveV2ManagerConvergenceDisposition::accepted);
    CHECK(convergence->accepted_activation_count() == 5);
    CHECK(convergence->status() ==
          AdaptiveV2ManagerConvergenceStatus::ready_for_optimization);
    CHECK(convergence->consume_ready_for_optimization());
    CHECK_FALSE(convergence->consume_ready_for_optimization());
}

TEST_CASE(
    "one alternate activation identity cannot poison a clean Q5 group",
    "[adaptive-v2][manager-convergence][c3][groups][isolation]")
{
    const auto bundle = canonical_bundle();
    const auto alternate = committed_identity(
        bundle, 900, "alternate-activation-block");
    const auto authoritative = committed_identity(
        bundle, 760, "authoritative-activation-block");
    auto convergence = make_convergence(bundle);

    CHECK(convergence->observe_activation(
              0, activated(0, alternate)) ==
          AdaptiveV2ManagerConvergenceDisposition::accepted);
    CHECK(convergence->accepted_activation_count() == 1);
    CHECK(convergence->status() ==
          AdaptiveV2ManagerConvergenceStatus::awaiting_activations);

    for (const auto replica : kSurvivors)
    {
        CHECK(convergence->observe_activation(
                  replica, activated(replica, authoritative)) ==
              AdaptiveV2ManagerConvergenceDisposition::accepted);
    }
    CHECK(convergence->accepted_activation_count() == 6);
    CHECK(convergence->status() ==
          AdaptiveV2ManagerConvergenceStatus::ready_for_optimization);
    CHECK(convergence->consume_ready_for_optimization());
}

TEST_CASE(
    "fewer than Q in every full activation identity group stays awaiting",
    "[adaptive-v2][manager-convergence][c3][groups][below-quorum]")
{
    const auto bundle = canonical_bundle();
    const auto identity_a = committed_identity(
        bundle, 760, "split-group-a");
    const auto identity_b = committed_identity(
        bundle, 761, "split-group-b");
    auto convergence = make_convergence(bundle);

    for (const auto replica : {ReplicaID{0}, ReplicaID{1}, ReplicaID{2}})
    {
        CHECK(convergence->observe_activation(
                  replica, activated(replica, identity_a)) ==
              AdaptiveV2ManagerConvergenceDisposition::accepted);
    }
    for (const auto replica :
         {ReplicaID{3}, ReplicaID{4}, ReplicaID{5}, ReplicaID{6}})
    {
        CHECK(convergence->observe_activation(
                  replica, activated(replica, identity_b)) ==
              AdaptiveV2ManagerConvergenceDisposition::accepted);
    }

    CHECK(convergence->accepted_activation_count() == 7);
    CHECK(convergence->status() ==
          AdaptiveV2ManagerConvergenceStatus::awaiting_activations);
    CHECK_FALSE(convergence->consume_ready_for_optimization());
}

TEST_CASE(
    "convergence rejects spoofed stale and wrong pre-commit identities",
    "[adaptive-v2][manager-convergence][c3][identity]")
{
    const auto bundle = canonical_bundle();
    const auto identity = committed_identity(bundle);

    SECTION("spoofed authenticated source")
    {
        auto convergence = make_convergence(bundle);
        CHECK(convergence->observe_activation(
                  3, activated(2, identity)) ==
              AdaptiveV2ManagerConvergenceDisposition::
                  rejected_spoofed_source);
    }

    SECTION("stale successor")
    {
        auto convergence = make_convergence(bundle);
        auto stale = identity;
        stale.successor_epoch_number = 0;
        CHECK(convergence->observe_activation(
                  2, activated(2, stale)) ==
              AdaptiveV2ManagerConvergenceDisposition::rejected_stale);
    }

    SECTION("wrong successor digest")
    {
        auto convergence = make_convergence(bundle);
        auto wrong = identity;
        wrong.successor_epoch_digest = digest("wrong-successor");
        CHECK(convergence->observe_activation(
                  2, activated(2, wrong)) ==
              AdaptiveV2ManagerConvergenceDisposition::
                  rejected_wrong_identity);
    }
}

TEST_CASE(
    "same-source activation equivocation is quarantined without global failure",
    "[adaptive-v2][manager-convergence][c3][groups][equivocation]")
{
    const auto bundle = canonical_bundle();
    const auto identity_a = committed_identity(
        bundle, 760, "equivocation-group-a");
    const auto identity_b = committed_identity(
        bundle, 761, "equivocation-group-b");
    auto convergence = make_convergence(bundle);

    CHECK(convergence->observe_activation(
              2, activated(2, identity_a)) ==
          AdaptiveV2ManagerConvergenceDisposition::accepted);
    CHECK(convergence->accepted_activation_count() == 1);

    CHECK(convergence->observe_activation(
              2, activated(2, identity_b)) ==
          AdaptiveV2ManagerConvergenceDisposition::
              conflicting_observation);
    CHECK(convergence->accepted_activation_count() == 0);
    CHECK(convergence->status() ==
          AdaptiveV2ManagerConvergenceStatus::awaiting_activations);

    for (const auto replica :
         {ReplicaID{3}, ReplicaID{4}, ReplicaID{5}, ReplicaID{6}})
    {
        CHECK(convergence->observe_activation(
                  replica, activated(replica, identity_a)) ==
              AdaptiveV2ManagerConvergenceDisposition::accepted);
    }
    CHECK(convergence->accepted_activation_count() == 4);
    CHECK(convergence->status() ==
          AdaptiveV2ManagerConvergenceStatus::awaiting_activations);
    CHECK_FALSE(convergence->consume_ready_for_optimization());

    CHECK(convergence->observe_activation(
              0, activated(0, identity_a)) ==
          AdaptiveV2ManagerConvergenceDisposition::accepted);
    CHECK(convergence->accepted_activation_count() == 5);
    CHECK(convergence->status() ==
          AdaptiveV2ManagerConvergenceStatus::ready_for_optimization);
    CHECK(convergence->consume_ready_for_optimization());
}

TEST_CASE(
    "overlapping raw Q claims fail closed after equivocation quarantine",
    "[adaptive-v2][manager-convergence][c3][groups][two-quorums]")
{
    const auto bundle = canonical_bundle();
    const auto identity_a = committed_identity(
        bundle, 760, "defensive-group-a");
    const auto identity_b = committed_identity(
        bundle, 761, "defensive-group-b");
    auto convergence = make_convergence(bundle);

    for (const auto replica : {ReplicaID{0}, ReplicaID{1}})
    {
        CHECK(convergence->observe_activation(
                  replica, activated(replica, identity_a)) ==
              AdaptiveV2ManagerConvergenceDisposition::accepted);
    }
    for (const auto replica : {ReplicaID{5}, ReplicaID{6}})
    {
        CHECK(convergence->observe_activation(
                  replica, activated(replica, identity_b)) ==
              AdaptiveV2ManagerConvergenceDisposition::accepted);
    }

    for (const auto replica :
         {ReplicaID{2}, ReplicaID{3}, ReplicaID{4}})
    {
        CHECK(convergence->observe_activation(
                  replica, activated(replica, identity_a)) ==
              AdaptiveV2ManagerConvergenceDisposition::accepted);
        CHECK(convergence->observe_activation(
                  replica, activated(replica, identity_b)) ==
              AdaptiveV2ManagerConvergenceDisposition::
                  conflicting_observation);
    }

    // Raw claims support A={0,1,2,3,4} and B={2,3,4,5,6}. Removing the
    // overlapping equivocators leaves only two clean reporters per group.
    CHECK(convergence->accepted_activation_count() == 4);
    CHECK(convergence->status() ==
          AdaptiveV2ManagerConvergenceStatus::awaiting_activations);
    CHECK_FALSE(convergence->consume_ready_for_optimization());
}

TEST_CASE(
    "ready for optimization is terminal and consumable exactly once",
    "[adaptive-v2][manager-convergence][c3][terminal][ready]")
{
    SECTION("duplicates remain idempotent")
    {
        auto convergence = ready_convergence();
        const auto bundle = canonical_bundle();
        const auto identity = committed_identity(bundle);
        REQUIRE(convergence->status() ==
                AdaptiveV2ManagerConvergenceStatus::
                    ready_for_optimization);
        CHECK(convergence->consume_ready_for_optimization());
        CHECK(convergence->observe_activation(
                  2, activated(2, identity)) ==
              AdaptiveV2ManagerConvergenceDisposition::duplicate);
        CHECK(convergence->accepted_activation_count() == 5);
        CHECK(convergence->status() ==
              AdaptiveV2ManagerConvergenceStatus::
                  ready_for_optimization);
        CHECK_FALSE(convergence->consume_ready_for_optimization());
    }

    SECTION("late full-membership winner reports remain positively ACKable")
    {
        auto convergence = ready_convergence();
        const auto bundle = canonical_bundle();
        const auto identity = committed_identity(bundle);
        CHECK(convergence->consume_ready_for_optimization());
        for (const auto replica : {ReplicaID{0}, ReplicaID{1}})
        {
            CAPTURE(replica);
            CHECK(convergence->observe_commit(
                      replica, committed(replica, identity)) ==
                  AdaptiveV2ManagerConvergenceDisposition::duplicate);
            CHECK(convergence->observe_activation(
                      replica, activated(replica, identity)) ==
                  AdaptiveV2ManagerConvergenceDisposition::duplicate);
        }
        CHECK(convergence->accepted_activation_count() == 5);
        CHECK(convergence->status() ==
              AdaptiveV2ManagerConvergenceStatus::
                  ready_for_optimization);
        REQUIRE(convergence->winning_identity() != nullptr);
        CHECK(*convergence->winning_identity() == identity);
        CHECK_FALSE(convergence->consume_ready_for_optimization());
    }

    SECTION("later conflicts cannot retract readiness")
    {
        auto convergence = ready_convergence();
        const auto bundle = canonical_bundle();
        auto conflict = committed_identity(bundle);
        conflict.command_block_hash = digest("late-conflict");
        CHECK(convergence->consume_ready_for_optimization());
        CHECK(convergence->observe_activation(
                  2, activated(2, conflict)) ==
              AdaptiveV2ManagerConvergenceDisposition::terminal);
        CHECK(convergence->accepted_activation_count() == 5);
        CHECK(convergence->status() ==
              AdaptiveV2ManagerConvergenceStatus::
                  ready_for_optimization);
        CHECK_FALSE(convergence->consume_ready_for_optimization());
    }

    SECTION("late conflicting full-membership reports stay non-positive")
    {
        auto convergence = ready_convergence();
        const auto bundle = canonical_bundle();
        const auto identity = committed_identity(bundle);
        auto conflict = identity;
        conflict.command_block_hash = digest("late-unused-member-conflict");
        CHECK(convergence->consume_ready_for_optimization());
        CHECK(convergence->observe_commit(
                  0, committed(0, conflict)) ==
              AdaptiveV2ManagerConvergenceDisposition::terminal);
        CHECK(convergence->observe_activation(
                  1, activated(1, conflict)) ==
              AdaptiveV2ManagerConvergenceDisposition::terminal);
        CHECK(convergence->accepted_activation_count() == 5);
        CHECK(convergence->status() ==
              AdaptiveV2ManagerConvergenceStatus::
                  ready_for_optimization);
        REQUIRE(convergence->winning_identity() != nullptr);
        CHECK(*convergence->winning_identity() == identity);
        CHECK_FALSE(convergence->consume_ready_for_optimization());
    }

    SECTION("later stale reports cannot retract readiness")
    {
        auto convergence = ready_convergence();
        const auto bundle = canonical_bundle();
        auto stale = committed_identity(bundle);
        stale.successor_epoch_number = 0;
        CHECK(convergence->consume_ready_for_optimization());
        CHECK(convergence->observe_activation(
                  0, activated(0, stale)) ==
              AdaptiveV2ManagerConvergenceDisposition::terminal);
        CHECK(convergence->accepted_activation_count() == 5);
        CHECK(convergence->status() ==
              AdaptiveV2ManagerConvergenceStatus::
                  ready_for_optimization);
        CHECK_FALSE(convergence->consume_ready_for_optimization());
    }
}

TEST_CASE(
    "ready convergence exposes the exact Q-winning full identity",
    "[adaptive-v2][manager-convergence][identity][quorum][audit]")
{
    if constexpr (has_winning_identity<
                      AdaptiveV2ManagerConvergence>::value)
    {
        exercise_winning_identity_access<
            AdaptiveV2ManagerConvergence>();
    }
    else
    {
        FAIL("convergence does not expose its exact Q-winning identity");
    }
}

TEST_CASE(
    "successful send attempts remain awaiting status until the activation deadline",
    "[adaptive-v2][manager-convergence][retry][liveness][deadline]")
{
    const auto bundle = canonical_bundle();
    const auto exact_identity = committed_identity(
        bundle, 812, "delayed-authoritative-status");
    AdaptiveV2ManagerConvergenceConfig delayed_status_config{
        kMembership,
        2,
        5,
        100};
    auto convergence = make_convergence(
        bundle, delayed_status_config, 0);

    for (std::uint64_t tick = 0; tick <= 8; tick += 2)
    {
        const auto attempt = convergence->due_deliveries(tick);
        REQUIRE(attempt.size() == kMembership.size());
        record_all_enqueues(*convergence, attempt, true);
        CHECK(convergence->status() ==
              AdaptiveV2ManagerConvergenceStatus::awaiting_activations);
    }

    // A nonblocking send-buffer acceptance is advisory. Spending the five
    // configured sends cannot prove that an authoritative status is
    // impossible, so the manager must not fail around the next retry tick.
    CHECK(convergence->due_deliveries(10).empty());
    CHECK(convergence->status() ==
          AdaptiveV2ManagerConvergenceStatus::awaiting_activations);
    CHECK(convergence->due_deliveries(50).empty());
    CHECK(convergence->status() ==
          AdaptiveV2ManagerConvergenceStatus::awaiting_activations);

    // Commit observations remain advisory even when delayed and at Q.
    for (const auto replica : kSurvivors)
    {
        CHECK(convergence->observe_commit(
                  replica, committed(replica, exact_identity)) ==
              AdaptiveV2ManagerConvergenceDisposition::accepted);
    }
    CHECK(convergence->accepted_commit_count() == kSurvivors.size());
    CHECK(convergence->status() ==
          AdaptiveV2ManagerConvergenceStatus::awaiting_activations);

    // The only readiness authority is still Q=5 matching exact activation
    // observations, which may arrive after every configured send was used.
    for (const auto replica : kSurvivors)
    {
        CHECK(convergence->observe_activation(
                  replica, activated(replica, exact_identity)) ==
              AdaptiveV2ManagerConvergenceDisposition::accepted);
    }
    CHECK(convergence->accepted_activation_count() == kSurvivors.size());
    CHECK(convergence->status() ==
          AdaptiveV2ManagerConvergenceStatus::ready_for_optimization);
    CHECK(convergence->consume_ready_for_optimization());

    SECTION("without Q the activation deadline is the terminal boundary")
    {
        auto deadline = make_convergence(
            bundle, delayed_status_config, 0);
        for (std::uint64_t tick = 0; tick <= 8; tick += 2)
            record_all_enqueues(
                *deadline, deadline->due_deliveries(tick), true);

        CHECK(deadline->due_deliveries(99).empty());
        CHECK(deadline->status() ==
              AdaptiveV2ManagerConvergenceStatus::awaiting_activations);
        CHECK(deadline->due_deliveries(100).empty());
        CHECK(deadline->status() ==
              AdaptiveV2ManagerConvergenceStatus::retry_exhausted);
    }
}

TEST_CASE(
    "lost first activation observations retransmit exactly and still reach Q5",
    "[adaptive-v2][manager-convergence][activation][ack][loss][quorum]")
{
#if KAURI_HAS_ADAPTIVE_V2_CONVERGENCE_ACK_WIRE
    const auto bundle = canonical_bundle();
    const auto exact_identity = committed_identity(
        bundle, 813, "ack-loss-recovery");
    auto convergence = make_convergence(
        bundle,
        AdaptiveV2ManagerConvergenceConfig{kMembership, 2, 5, 100},
        0);

    hotstuff::AdaptiveV2ReportingOutboxLimits outbox_limits;
    outbox_limits.maximum_pending_reports = 4;
    outbox_limits.maximum_pending_payload_bytes = 4096;
    outbox_limits.maximum_delivery_attempts = 5;
    outbox_limits.initial_retry_backoff_ns = 10;
    outbox_limits.maximum_retry_backoff_ns = 40;
    outbox_limits.convergence_wire.maximum_payload_bytes = 512;

    for (const auto replica : kSurvivors)
    {
        hotstuff::AdaptiveV2ReportingOutbox outbox(
            hotstuff::AdaptiveV2ReportingOutboxConfig{
                replica, 0, 0, 0, 0, outbox_limits});
        REQUIRE(outbox.enqueue_epoch_activated(exact_identity) ==
                hotstuff::AdaptiveV2ReportingEnqueueStatus::queued);
        REQUIRE(outbox.front() != nullptr);
        const auto canonical = outbox.front()->canonical_payload;

        // Locally accepted into the nonblocking buffer, deliberately not
        // delivered to convergence.
        const auto lost = outbox.begin_delivery(100);
        REQUIRE(lost.token.has_value());
        REQUIRE(outbox.acknowledge_delivery(
                    *lost.token,
                    hotstuff::AdaptiveV2ReportingDeliveryResult::delivered,
                    100) ==
                hotstuff::AdaptiveV2ReportingTransitionStatus::
                    retry_scheduled);

        const auto retransmission = outbox.begin_delivery(110);
        REQUIRE(retransmission.token.has_value());
        REQUIRE(retransmission.report != nullptr);
        CHECK(retransmission.report->canonical_payload == canonical);
        const auto decoded =
            hotstuff::decode_adaptive_v2_epoch_activated_observation(
                retransmission.report->canonical_payload,
                outbox_limits.convergence_wire);
        REQUIRE(decoded);
        REQUIRE(decoded.observation.has_value());
        CHECK(convergence->observe_activation(
                  replica, *decoded.observation) ==
              AdaptiveV2ManagerConvergenceDisposition::accepted);
        REQUIRE(outbox.acknowledge_delivery(
                    *retransmission.token,
                    hotstuff::AdaptiveV2ReportingDeliveryResult::delivered,
                    110) ==
                hotstuff::AdaptiveV2ReportingTransitionStatus::
                    retry_scheduled);

        hotstuff::AdaptiveV2ConvergenceObservationAck acknowledgement;
        acknowledgement.schema_version =
            hotstuff::kAdaptiveV2ConvergenceAckSchemaVersionV1;
        acknowledgement.target_replica_id = replica;
        acknowledgement.observation_kind =
            hotstuff::AdaptiveV2ConvergenceObservationKind::activation;
        acknowledgement.identity = exact_identity;
        acknowledgement.observation_digest =
            hotstuff::adaptive_v2_convergence_observation_digest(
                hotstuff::MsgAdaptiveV2EpochActivatedObservation::opcode,
                canonical);
        acknowledgement.disposition =
            hotstuff::AdaptiveV2ConvergenceAckDisposition::positive;
        CHECK(outbox.acknowledge_convergence_observation(
                  acknowledgement) ==
              hotstuff::AdaptiveV2ReportingTransitionStatus::delivered);
        REQUIRE(outbox.front() != nullptr);
        CHECK(outbox.release_terminal(outbox.front()->report_id) ==
              hotstuff::AdaptiveV2ReportingReleaseStatus::released);
    }

    CHECK(convergence->accepted_activation_count() == 5);
    CHECK(convergence->status() ==
          AdaptiveV2ManagerConvergenceStatus::ready_for_optimization);
    CHECK(convergence->consume_ready_for_optimization());
#else
    FAIL("end-to-end activation observation ACK loss recovery is missing");
#endif
}

TEST_CASE(
    "adaptive v2 convergence exhausts its bounded full-membership retries",
    "[adaptive-v2][manager-convergence][c3][n7][exhaustion]")
{
    const auto bundle = canonical_bundle();
    auto convergence = make_convergence(bundle);

    const auto first = convergence->due_deliveries(0);
    REQUIRE(first.size() == 7);
    record_all_enqueues(*convergence, first, false);

    const auto second = convergence->due_deliveries(2);
    REQUIRE(second.size() == 7);
    record_all_enqueues(*convergence, second, false);

    CHECK(convergence->due_deliveries(4).empty());
    CHECK(convergence->status() ==
          AdaptiveV2ManagerConvergenceStatus::retry_exhausted);
    CHECK_FALSE(convergence->consume_ready_for_optimization());
}

TEST_CASE(
    "config validation cannot allocate from inside noexcept",
    "[adaptive-v2][manager-convergence][c3][noexcept][allocation]")
{
    const auto source = read_source(
        "src/adaptive_v2_manager_convergence.cpp");
    const auto begin = source.find("bool valid_config(");
    const auto state = source.find(
        "struct AdaptiveV2ManagerConvergence::State", begin);
    REQUIRE(begin != std::string::npos);
    REQUIRE(state != std::string::npos);
    const auto validation = source.substr(begin, state - begin);
    const auto signature_end = validation.find('{');
    REQUIRE(signature_end != std::string::npos);
    const auto signature = validation.substr(0, signature_end);
    const bool declared_noexcept =
        signature.find("noexcept") != std::string::npos;
    const bool copies_membership =
        validation.find("auto membership = config.membership") !=
        std::string::npos;
    const bool allocation_can_escape_noexcept =
        declared_noexcept && copies_membership;

    CHECK_FALSE(allocation_can_escape_noexcept);
}
