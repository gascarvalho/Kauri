#include <stdexcept>
#include <string>
#include <optional>

#include "catch.hpp"

/*
 * P05 proposal-handler admission contract
 * ---------------------------------------
 * The public headers are deliberately optional in this red commit. Their
 * absence produces one explicit runtime failure while keeping the target and
 * the full project buildable. Once both seams exist, the production
 * coordinator behavior matrix below compiles automatically.
 *
 * include/hotstuff/proposal_admission.h must expose:
 *
 *   enum class ProposalDisposition {
 *       rejected_malformed,
 *       rejected_unknown_configuration,
 *       rejected_digest_mismatch,
 *       rejected_stale_configuration,
 *       rejected_invalid_proposer,
 *       duplicate,
 *       buffered_future,
 *       admitted_active
 *   };
 *
 *   struct ProposalAdmissionResult {
 *       ProposalDisposition disposition;
 *       ProposalKey key;
 *   };
 *
 *   class ProposalAdmissionEffects {
 *   public:
 *       virtual ~ProposalAdmissionEffects() = default;
 *       virtual void relay_once(const BufferedProposal &) = 0;
 *       virtual void process_active(const BufferedProposal &) = 0;
 *       virtual void local_vote_authorized(const ProposalKey &) = 0;
 *       virtual void create_expected_vote_state(const ProposalKey &) = 0;
 *       virtual bool start_latency_deadline(const ProposalKey &) = 0;
 *       virtual void start_aggregation_timer(const ProposalKey &) = 0;
 *       virtual void emit_timeout_report(const ProposalKey &) = 0;
 *   };
 *
 *   class ProposalAdmissionCoordinator {
 *   public:
 *       ProposalAdmissionCoordinator(const EpochStore &,
 *                                    ConfigurationId active,
 *                                    FutureProposalBuffer &,
 *                                    ProposalAdmissionEffects &);
 *       ProposalAdmissionResult receive(BufferedProposal);
 *       std::vector<ProposalAdmissionResult>
 *           activate(const ConfigurationId &);
 *       bool authorize_local_vote(const ProposalKey &);
 *       const ConfigurationId &active_configuration() const noexcept;
 *   };
 *
 * receive is the factored production entry point called exactly once by
 * HotStuffBase::propose_handler after wire parsing. It must validate the epoch,
 * tree, digest, block identity, and exact tree root before deduplication,
 * buffer, cache, active processing, or relay mutation. process_active is the only
 * operation allowed to enter HotStuffCore::on_receive_proposal.
 *
 * process_active owns delivered exact-context initialization, including child
 * accounting, latency tracking, and the aggregation deadline, and relays only
 * after that initialization succeeds. authorize_local_vote
 * is the narrower completion boundary called only after safety processing
 * authorizes the optional local contribution. P05 never owns leader liveness;
 * future/rejected traffic remains inert because the admission effects expose
 * no leader reset or expiry capability. activate is a local activation hook
 * only—transport/acks remain D11 scope.
 */
#if __has_include("hotstuff/proposal_admission.h") && \
    __has_include("hotstuff/future_proposal_buffer.h")
#define KAURI_HAS_P05_PROPOSAL_ADMISSION_API 1
#include <cstdint>
#include <type_traits>
#include <utility>
#include <vector>

#include "hotstuff/epoch_store.h"
#include "hotstuff/future_proposal_buffer.h"
#include "hotstuff/proposal_admission.h"
#include "hotstuff/proposal_context.h"
#else
#define KAURI_HAS_P05_PROPOSAL_ADMISSION_API 0
#endif

#if !KAURI_HAS_P05_PROPOSAL_ADMISSION_API

TEST_CASE("P05 proposal admission coordinator is available",
          "[p05][proposal-admission][contract][red]")
{
    INFO("Missing include/hotstuff/proposal_admission.h and/or "
         "include/hotstuff/future_proposal_buffer.h. P05 requires the real "
         "handler-facing admission coordinator and exact future buffer.");
    REQUIRE(KAURI_HAS_P05_PROPOSAL_ADMISSION_API == 1);
}

#else

namespace
{

using hotstuff::BufferedProposal;
using hotstuff::ConfigurationId;
using hotstuff::DataStream;
using hotstuff::EpochDefinition;
using hotstuff::EpochDefinitionInput;
using hotstuff::EpochStore;
using hotstuff::EpochTreeDefinition;
using hotstuff::EpochValidationContext;
using hotstuff::FutureProposalBuffer;
using hotstuff::FutureProposalBufferLimits;
using hotstuff::ProposalAdmissionCoordinator;
using hotstuff::ProposalAdmissionEffects;
using hotstuff::ProposalRelayPolicy;
using hotstuff::ProposalContextEvent;
using hotstuff::ProposalContextLifecycle;
using hotstuff::ProposalContextStatus;
using hotstuff::ProposalDisposition;
using hotstuff::ProposalKey;
using hotstuff::ProposalMetadata;
using hotstuff::ProposalProcessingOutcome;
using hotstuff::ProposalTransitionResult;
using hotstuff::ReplicaID;
using hotstuff::bytearray_t;
using hotstuff::uint256_t;

template<typename Effects, typename = void>
struct has_leader_reset_effect : std::false_type
{};

template<typename Effects>
struct has_leader_reset_effect<
    Effects,
    std::void_t<decltype(
        std::declval<Effects &>().reset_leader_progress(
            std::declval<const ConfigurationId &>()))>> : std::true_type
{};

template<typename Effects, typename = void>
struct has_leader_expiry_effect : std::false_type
{};

template<typename Effects>
struct has_leader_expiry_effect<
    Effects,
    std::void_t<decltype(
        std::declval<Effects &>().expire_leader_progress(
            std::declval<const ConfigurationId &>()))>> : std::true_type
{};

uint256_t digest(const std::string &label)
{
    return DataStream(label).get_hash();
}

std::vector<ReplicaID> membership()
{
    return {0, 1, 2, 3, 4, 5, 6};
}

EpochTreeDefinition tree(std::uint32_t tree_id,
                         std::vector<ReplicaID> members)
{
    return EpochTreeDefinition{tree_id, 2, 2, std::move(members)};
}

EpochDefinitionInput epoch_zero_input()
{
    EpochDefinitionInput input;
    input.schema_version = hotstuff::kEpochDefinitionSchemaVersion;
    input.epoch_number = 0;
    input.previous_epoch_digest = uint256_t{};
    input.membership_digest =
        hotstuff::canonical_membership_digest(membership());
    input.trees = {
        tree(7, {0, 1, 2, 3, 4, 5, 6}),
        tree(11, {1, 0, 2, 3, 4, 5, 6})};
    input.activation_height = 10;
    input.generation_seed = 0x500;
    input.policy_version = "p05-policy-v1";
    input.evidence_snapshot_id = "p05-epoch-0";
    input.evidence_cutoff = 10;
    return input;
}

EpochDefinitionInput successor_input(const EpochDefinition &predecessor,
                                     std::uint32_t epoch_number)
{
    EpochDefinitionInput input;
    input.schema_version = hotstuff::kEpochDefinitionSchemaVersion;
    input.epoch_number = epoch_number;
    input.previous_epoch_digest = predecessor.epoch_digest();
    input.membership_digest = predecessor.membership_digest();
    input.trees = {
        tree(7, {2, 0, 1, 3, 4, 5, 6}),
        tree(11, {3, 0, 1, 2, 4, 5, 6})};
    input.activation_height = 10 + (epoch_number * 10);
    input.generation_seed = 0x500 + epoch_number;
    input.policy_version = "p05-policy-v1";
    input.evidence_snapshot_id =
        "p05-epoch-" + std::to_string(epoch_number);
    input.evidence_cutoff = 10 + (epoch_number * 10);
    return input;
}

EpochValidationContext validation_context()
{
    EpochValidationContext context;
    context.current_height = 0;
    context.minimum_activation_grace = 1;
    return context;
}

ConfigurationId configuration(const EpochDefinition &epoch,
                              std::uint32_t tree_id)
{
    return ConfigurationId{
        epoch.epoch_number(), tree_id, epoch.epoch_digest()};
}

BufferedProposal proposal(const ConfigurationId &configuration_id,
                          const std::string &block_label,
                          ReplicaID proposer,
                          bytearray_t payload = {})
{
    return BufferedProposal{
        ProposalMetadata{
            configuration_id, digest(block_label), proposer},
        std::move(payload)};
}

struct StagedEpochs
{
    EpochStore store{membership()};
    const EpochDefinition *epoch0{nullptr};
    const EpochDefinition *epoch1{nullptr};
    const EpochDefinition *epoch2{nullptr};

    StagedEpochs()
    {
        epoch0 = &store.stage(epoch_zero_input(), validation_context());
        epoch1 = &store.stage(
            successor_input(*epoch0, 1), validation_context());
        epoch2 = &store.stage(
            successor_input(*epoch1, 2), validation_context());
    }
};

class EffectSpy final : public ProposalAdmissionEffects
{
public:
    bool throw_on_relay{false};
    bool relay_during_processing{true};
    std::size_t relays{0};
    std::size_t active_processing{0};
    std::size_t local_votes{0};
    std::size_t expected_vote_states{0};
    std::size_t latency_deadlines{0};
    std::size_t aggregation_timers{0};
    std::size_t timeout_reports{0};
    std::vector<std::string> sequence;
    std::vector<ProposalKey> processed;

    void relay_once(const BufferedProposal &value) override
    {
        if (throw_on_relay)
            throw std::runtime_error("relay failure");
        ++relays;
        sequence.emplace_back("relay");
        relayed.push_back(value.metadata.key());
    }

    void process_active(const BufferedProposal &value) override
    {
        ++active_processing;
        sequence.emplace_back("normal-processing");
        processed.push_back(value.metadata.key());
        if (relay_during_processing)
            relay_once(value);
    }

    void local_vote_authorized(const ProposalKey &) override
    {
        ++local_votes;
        sequence.emplace_back("local-vote");
    }

    void create_expected_vote_state(const ProposalKey &) override
    {
        ++expected_vote_states;
        sequence.emplace_back("expected-votes");
    }

    bool start_latency_deadline(const ProposalKey &) override
    {
        ++latency_deadlines;
        sequence.emplace_back("latency-deadline");
        return true;
    }

    void start_aggregation_timer(const ProposalKey &) override
    {
        ++aggregation_timers;
        sequence.emplace_back("aggregation-timer");
    }

    void emit_timeout_report(const ProposalKey &) override
    {
        ++timeout_reports;
        sequence.emplace_back("timeout-report");
    }

    // Compile-only compatibility while the production interface still
    // declares these pure virtual methods. Once L07 removes that capability,
    // these are ordinary unused spy helpers.
    void reset_leader_progress(const ConfigurationId &) {}
    void expire_leader_progress(const ConfigurationId &) {}

    std::vector<ProposalKey> relayed;
};

std::vector<hotstuff::ProposalAdmissionResult>
activate_deferred_configuration(
    ProposalAdmissionCoordinator &coordinator,
    FutureProposalBuffer &buffer,
    const ConfigurationId &configuration)
{
    if (!coordinator.activate_without_draining(configuration))
        return {};

    std::vector<hotstuff::ProposalAdmissionResult> results;
    std::set<ProposalKey> claimed;
    while (const auto *next =
               buffer.first_unclaimed(configuration, claimed))
    {
        const auto proposal = *next;
        const auto key = proposal.metadata.key();
        bool completed = false;
        ProposalProcessingOutcome outcome =
            ProposalProcessingOutcome::terminal_pre_relay;
        const auto accepted = coordinator.process_claimed_active(
            proposal,
            [&](ProposalProcessingOutcome value) {
                completed = true;
                outcome = value;
            });
        if (!accepted)
        {
            claimed.insert(key);
            continue;
        }
        REQUIRE(completed);
        REQUIRE(outcome ==
                ProposalProcessingOutcome::completed_exposed);
        REQUIRE(buffer.erase(key));
        results.push_back({ProposalDisposition::admitted_active, key});
    }
    return results;
}

/*
 * Bounded admission-retirement contract
 * -------------------------------------
 * ProposalAdmissionCoordinator owns ingress deduplication independently from
 * ProposalContextLifecycle's leases. It therefore needs its own deterministic
 * pruning surface:
 *
 *   bool retire_proposal(const ProposalKey &);
 *   std::size_t retire_configuration(const ConfigurationId &);
 *   std::size_t advance_retirement_floor(std::uint32_t first_live_epoch);
 *   bool contains_admitted(const ProposalKey &) const;
 *   ProposalAdmissionStorageStats storage_stats() const;
 *
 * ProposalAdmissionStorageStats exposes retained_received,
 * retained_admitted, retained_local_authorizations, and
 * retired_configuration_tombstones. Equivalent return types are acceptable;
 * the tests use only boolean success and integral counts.
 */
template<typename Coordinator, typename = void>
struct has_bounded_retirement_api : std::false_type
{};

template<typename Coordinator>
struct has_bounded_retirement_api<
    Coordinator,
    std::void_t<
        decltype(std::declval<Coordinator &>().retire_proposal(
            std::declval<const ProposalKey &>())),
        decltype(std::declval<Coordinator &>().retire_configuration(
            std::declval<const ConfigurationId &>())),
        decltype(std::declval<Coordinator &>().advance_retirement_floor(
            std::declval<std::uint32_t>())),
        decltype(std::declval<const Coordinator &>().contains_admitted(
            std::declval<const ProposalKey &>())),
        decltype(std::declval<const Coordinator &>()
                     .storage_stats()
                     .retained_received),
        decltype(std::declval<const Coordinator &>()
                     .storage_stats()
                     .retained_admitted),
        decltype(std::declval<const Coordinator &>()
                     .storage_stats()
                     .retained_local_authorizations),
        decltype(std::declval<const Coordinator &>()
                     .storage_stats()
                     .retired_configuration_tombstones)>> : std::true_type
{};

template<typename Coordinator>
void require_bounded_retirement_api()
{
    if constexpr (!has_bounded_retirement_api<Coordinator>::value)
    {
        FAIL("ProposalAdmissionCoordinator must expose exact proposal/config "
             "retirement, a monotonic first-live epoch floor, admitted-key "
             "lookup, and bounded storage statistics");
    }
}

template<typename Coordinator>
void check_terminal_proposal_pruning()
{
    if constexpr (!has_bounded_retirement_api<Coordinator>::value)
    {
        require_bounded_retirement_api<Coordinator>();
    }
    else
    {
        StagedEpochs fixture;
        FutureProposalBuffer buffer;
        EffectSpy effects;
        const auto active = configuration(*fixture.epoch0, 7);
        Coordinator coordinator{
            fixture.store,
            active,
            buffer,
            effects,
            ProposalRelayPolicy::adaptive_v2_deferred_until_arm_attempt};
        const auto candidate = proposal(active, "terminal-prune", 0);
        const auto key = candidate.metadata.key();

        REQUIRE(coordinator.receive(candidate).disposition ==
                ProposalDisposition::admitted_active);
        REQUIRE(coordinator.authorize_local_vote(key));
        REQUIRE(coordinator.contains_admitted(key));
        auto stats = coordinator.storage_stats();
        CHECK(stats.retained_received == 1);
        CHECK(stats.retained_admitted == 1);
        CHECK(stats.retained_local_authorizations == 1);

        CHECK(coordinator.retire_proposal(key));
        CHECK_FALSE(coordinator.contains_admitted(key));
        CHECK_FALSE(coordinator.authorize_local_vote(key));
        CHECK_FALSE(coordinator.retire_proposal(key));
        CHECK_FALSE(buffer.contains(key));

        stats = coordinator.storage_stats();
        CHECK(stats.retained_received == 1);
        CHECK(stats.retained_admitted == 0);
        CHECK(stats.retained_local_authorizations == 0);
        CHECK(stats.retired_configuration_tombstones == 0);

        const auto effects_before_replay = effects.sequence;
        const auto replay = coordinator.receive(candidate);
        CHECK(replay.disposition == ProposalDisposition::duplicate);
        CHECK_FALSE(coordinator.contains_admitted(key));
        CHECK_FALSE(coordinator.authorize_local_vote(key));
        CHECK(effects.sequence == effects_before_replay);
        CHECK(effects.relays == 1);
        CHECK(effects.active_processing == 1);
        CHECK(effects.local_votes == 1);
        CHECK(effects.expected_vote_states == 0);
        CHECK(effects.latency_deadlines == 0);
        CHECK(effects.aggregation_timers == 0);
        CHECK(effects.timeout_reports == 0);

        // Exact proposal tombstones live only for the containing
        // configuration. Configuration retirement rejects ABA replay and the
        // monotonic floor subsequently compacts the configuration tombstone.
        CHECK(coordinator.retire_configuration(active) == 1);
        stats = coordinator.storage_stats();
        CHECK(stats.retained_received == 0);
        CHECK(stats.retained_admitted == 0);
        CHECK(stats.retained_local_authorizations == 0);
        CHECK(stats.retired_configuration_tombstones == 1);

        const auto effects_before_retired_replay = effects.sequence;
        CHECK(coordinator.receive(candidate).disposition ==
              ProposalDisposition::rejected_stale_configuration);
        CHECK(effects.sequence == effects_before_retired_replay);

        CHECK(coordinator.advance_retirement_floor(1) == 1);
        stats = coordinator.storage_stats();
        CHECK(stats.retained_received == 0);
        CHECK(stats.retained_admitted == 0);
        CHECK(stats.retained_local_authorizations == 0);
        CHECK(stats.retired_configuration_tombstones == 0);
    }
}

template<typename Coordinator>
void check_exact_configuration_retirement()
{
    if constexpr (!has_bounded_retirement_api<Coordinator>::value)
    {
        require_bounded_retirement_api<Coordinator>();
    }
    else
    {
        StagedEpochs fixture;
        FutureProposalBuffer buffer;
        EffectSpy effects;
        const auto active = configuration(*fixture.epoch0, 7);
        const auto retired = configuration(*fixture.epoch1, 7);
        const auto retained = configuration(*fixture.epoch1, 11);
        Coordinator coordinator{
            fixture.store,
            active,
            buffer,
            effects,
            ProposalRelayPolicy::adaptive_v2_deferred_until_arm_attempt};

        const auto retired_one = proposal(retired, "retired-one", 2);
        const auto retired_two = proposal(retired, "retired-two", 2);
        const auto retained_one = proposal(retained, "retained-one", 3);
        REQUIRE(coordinator.receive(retired_one).disposition ==
                ProposalDisposition::buffered_future);
        REQUIRE(coordinator.receive(retired_two).disposition ==
                ProposalDisposition::buffered_future);
        REQUIRE(coordinator.receive(retained_one).disposition ==
                ProposalDisposition::buffered_future);
        REQUIRE(buffer.size() == 3);

        CHECK(coordinator.retire_configuration(retired) == 2);
        CHECK(coordinator.retire_configuration(retired) == 0);
        CHECK_FALSE(buffer.contains(retired_one.metadata.key()));
        CHECK_FALSE(buffer.contains(retired_two.metadata.key()));
        CHECK(buffer.contains(retained_one.metadata.key()));
        CHECK(buffer.size() == 1);
        CHECK_FALSE(
            coordinator.contains_admitted(retired_one.metadata.key()));

        const auto active_before = coordinator.active_configuration();
        CHECK(coordinator.activate(retired).empty());
        CHECK(coordinator.active_configuration() == active_before);

        const auto relay_count = effects.relays;
        const auto recycled =
            coordinator.receive(proposal(retired, "retired-aba", 2));
        CHECK(recycled.disposition ==
              ProposalDisposition::rejected_stale_configuration);
        CHECK(effects.relays == relay_count);
        CHECK(buffer.size() == 1);

        const auto stats = coordinator.storage_stats();
        CHECK(stats.retained_received == 1);
        CHECK(stats.retained_admitted == 0);
        CHECK(stats.retained_local_authorizations == 0);
        CHECK(stats.retired_configuration_tombstones == 1);
    }
}

template<typename Coordinator>
void check_retirement_floor_contract()
{
    if constexpr (!has_bounded_retirement_api<Coordinator>::value)
    {
        require_bounded_retirement_api<Coordinator>();
    }
    else
    {
        StagedEpochs fixture;
        FutureProposalBuffer buffer;
        EffectSpy effects;
        effects.relay_during_processing = false;
        const auto old = configuration(*fixture.epoch0, 7);
        const auto below_floor = configuration(*fixture.epoch1, 7);
        const auto first_live = configuration(*fixture.epoch2, 7);
        const auto first_live_other_tree =
            configuration(*fixture.epoch2, 11);
        Coordinator coordinator{fixture.store, old, buffer, effects};

        const auto old_proposal = proposal(old, "old-active", 0);
        const auto buffered_old =
            proposal(below_floor, "old-buffered", 2);
        const auto current =
            proposal(first_live, "first-live-current", 2);
        REQUIRE(coordinator.receive(old_proposal).disposition ==
                ProposalDisposition::admitted_active);
        REQUIRE(coordinator.authorize_local_vote(
            old_proposal.metadata.key()));
        REQUIRE(coordinator.receive(buffered_old).disposition ==
                ProposalDisposition::buffered_future);
        REQUIRE(coordinator.receive(current).disposition ==
                ProposalDisposition::buffered_future);
        REQUIRE(coordinator.activate(first_live).size() == 1);

        // Activation changes routing only. Old in-flight admission state is
        // retained until the deterministic retirement floor advances.
        REQUIRE(coordinator.contains_admitted(
            old_proposal.metadata.key()));
        REQUIRE(coordinator.contains_admitted(current.metadata.key()));
        REQUIRE(buffer.contains(buffered_old.metadata.key()));

        coordinator.advance_retirement_floor(2);
        CHECK_FALSE(coordinator.contains_admitted(
            old_proposal.metadata.key()));
        CHECK_FALSE(buffer.contains(buffered_old.metadata.key()));
        CHECK(coordinator.contains_admitted(current.metadata.key()));
        CHECK(coordinator.active_configuration() == first_live);

        const auto relays_before_aba = effects.relays;
        const auto old_aba =
            coordinator.receive(proposal(below_floor, "old-aba", 2));
        CHECK(old_aba.disposition ==
              ProposalDisposition::rejected_stale_configuration);
        CHECK(effects.relays == relays_before_aba);

        // The floor is exclusive. Another exact configuration in the first
        // live epoch remains legal and can be activated later.
        const auto first_live_candidate = proposal(
            first_live_other_tree, "first-live-other-tree", 3);
        CHECK(coordinator.receive(first_live_candidate).disposition ==
              ProposalDisposition::buffered_future);
        CHECK(buffer.contains(first_live_candidate.metadata.key()));
        CHECK(coordinator.contains_admitted(current.metadata.key()));

        const auto before_stale_floor = coordinator.storage_stats();
        CHECK(coordinator.advance_retirement_floor(1) == 0);
        const auto after_stale_floor = coordinator.storage_stats();
        CHECK(after_stale_floor.retained_received ==
              before_stale_floor.retained_received);
        CHECK(after_stale_floor.retained_admitted ==
              before_stale_floor.retained_admitted);
        CHECK(after_stale_floor.retained_local_authorizations ==
              before_stale_floor.retained_local_authorizations);
        CHECK(after_stale_floor.retired_configuration_tombstones ==
              before_stale_floor.retired_configuration_tombstones);
    }
}

template<typename Coordinator>
void check_same_epoch_reactivation_contract()
{
    if constexpr (!has_bounded_retirement_api<Coordinator>::value)
    {
        require_bounded_retirement_api<Coordinator>();
    }
    else
    {
        StagedEpochs fixture;
        FutureProposalBuffer buffer;
        EffectSpy effects;
        effects.relay_during_processing = false;
        const auto config_a = configuration(*fixture.epoch0, 7);
        const auto config_b = configuration(*fixture.epoch0, 11);
        Coordinator coordinator{
            fixture.store, config_a, buffer, effects};

        const auto a_in_flight =
            proposal(config_a, "a-in-flight", 0);
        const auto b = proposal(config_b, "b", 1);
        REQUIRE(coordinator.receive(a_in_flight).disposition ==
                ProposalDisposition::admitted_active);
        REQUIRE(coordinator.authorize_local_vote(
            a_in_flight.metadata.key()));
        REQUIRE(coordinator.receive(b).disposition ==
                ProposalDisposition::buffered_future);
        REQUIRE(coordinator.activate(config_b).size() == 1);

        CHECK(coordinator.active_configuration() == config_b);
        CHECK(coordinator.contains_admitted(
            a_in_flight.metadata.key()));
        CHECK(coordinator.contains_admitted(b.metadata.key()));

        const auto a_return = proposal(config_a, "a-return", 0);
        REQUIRE(coordinator.receive(a_return).disposition ==
                ProposalDisposition::buffered_future);
        REQUIRE(buffer.contains(a_return.metadata.key()));
        REQUIRE(coordinator.activate(config_a).size() == 1);

        CHECK(coordinator.active_configuration() == config_a);
        CHECK(coordinator.contains_admitted(
            a_in_flight.metadata.key()));
        CHECK(coordinator.contains_admitted(a_return.metadata.key()));
        CHECK(coordinator.contains_admitted(b.metadata.key()));
        CHECK(buffer.size() == 0);

        const auto stats = coordinator.storage_stats();
        CHECK(stats.retained_received == 3);
        CHECK(stats.retained_admitted == 3);
        CHECK(stats.retained_local_authorizations == 1);
        CHECK(stats.retired_configuration_tombstones == 0);
        CHECK(effects.active_processing == 3);
        CHECK(effects.relays == 3);
    }
}

void check_no_protocol_side_effects(const EffectSpy &effects)
{
    CHECK(effects.active_processing == 0);
    CHECK(effects.local_votes == 0);
    CHECK(effects.expected_vote_states == 0);
    CHECK(effects.latency_deadlines == 0);
    CHECK(effects.aggregation_timers == 0);
    CHECK(effects.timeout_reports == 0);
}

void check_rejected_without_mutation(
    ProposalAdmissionCoordinator &coordinator,
    FutureProposalBuffer &buffer,
    EffectSpy &effects,
    const BufferedProposal &candidate,
    ProposalDisposition expected)
{
    const auto buffer_size = buffer.size();
    const auto result = coordinator.receive(candidate);
    CHECK(result.disposition == expected);
    CHECK(buffer.size() == buffer_size);
    CHECK(effects.relays == 0);
    check_no_protocol_side_effects(effects);
}

} // namespace

TEST_CASE("proposal admission has no leader liveness capability",
          "[l07][p05][proposal-admission][single-owner]"
          "[intentional-red]")
{
    CHECK_FALSE(has_leader_reset_effect<ProposalAdmissionEffects>::value);
    CHECK_FALSE(has_leader_expiry_effect<ProposalAdmissionEffects>::value);
}

TEST_CASE("configuration validation fails closed before lookup or mutation",
          "[p05][proposal-admission][validation]")
{
    StagedEpochs fixture;
    FutureProposalBuffer buffer;
    EffectSpy effects;
    const auto active = configuration(*fixture.epoch0, 7);
    ProposalAdmissionCoordinator coordinator{
        fixture.store,
        active,
        buffer,
        effects,
        ProposalRelayPolicy::adaptive_v2_deferred_until_arm_attempt};

    auto malformed = proposal(active, "malformed", 0);
    malformed.metadata.configuration.epoch_digest = uint256_t{};
    check_rejected_without_mutation(
        coordinator,
        buffer,
        effects,
        malformed,
        ProposalDisposition::rejected_malformed);

    auto malformed_block = proposal(active, "placeholder", 0);
    malformed_block.metadata.block_hash = uint256_t{};
    check_rejected_without_mutation(
        coordinator,
        buffer,
        effects,
        malformed_block,
        ProposalDisposition::rejected_malformed);

    const ConfigurationId unknown_epoch{
        99, 7, digest("unknown-epoch")};
    check_rejected_without_mutation(
        coordinator,
        buffer,
        effects,
        proposal(unknown_epoch, "unknown-epoch-block", 0),
        ProposalDisposition::rejected_unknown_configuration);

    const ConfigurationId unknown_tree{
        fixture.epoch0->epoch_number(),
        999,
        fixture.epoch0->epoch_digest()};
    check_rejected_without_mutation(
        coordinator,
        buffer,
        effects,
        proposal(unknown_tree, "unknown-tree-block", 0),
        ProposalDisposition::rejected_unknown_configuration);

    auto wrong_digest = active;
    wrong_digest.tree_id = 999;
    wrong_digest.epoch_digest = digest("wrong-digest");
    check_rejected_without_mutation(
        coordinator,
        buffer,
        effects,
        proposal(wrong_digest, "digest-before-tree", 0),
        ProposalDisposition::rejected_digest_mismatch);

    CHECK(fixture.store.size() == 3);

    // Rejection must not poison a bare-block dedup cache. The corrected exact
    // identity with the same block hash is still admitted.
    const auto corrected = proposal(active, "digest-before-tree", 0);
    const auto corrected_result = coordinator.receive(corrected);
    CHECK(corrected_result.disposition ==
          ProposalDisposition::admitted_active);
    CHECK(effects.relays == 1);
    CHECK(effects.active_processing == 1);
}

TEST_CASE("stale known configuration is rejected without relaying",
          "[p05][proposal-admission][stale]")
{
    StagedEpochs fixture;
    FutureProposalBuffer buffer;
    EffectSpy effects;
    const auto active = configuration(*fixture.epoch1, 7);
    ProposalAdmissionCoordinator coordinator{
        fixture.store,
        active,
        buffer,
        effects,
        ProposalRelayPolicy::adaptive_v2_deferred_until_arm_attempt};

    const auto stale = proposal(
        configuration(*fixture.epoch0, 7), "stale-block", 0);
    check_rejected_without_mutation(
        coordinator,
        buffer,
        effects,
        stale,
        ProposalDisposition::rejected_stale_configuration);
}

TEST_CASE("proposer must equal the exact configuration tree root",
          "[p05][proposal-admission][proposer]")
{
    StagedEpochs fixture;
    FutureProposalBuffer buffer;
    EffectSpy effects;
    const auto active = configuration(*fixture.epoch0, 7);
    ProposalAdmissionCoordinator coordinator{
        fixture.store,
        active,
        buffer,
        effects,
        ProposalRelayPolicy::adaptive_v2_deferred_until_arm_attempt};

    // Replica 1 is the other epoch-0 tree root and could satisfy the legacy
    // current-proposer half of the buggy AND predicate. It is not tree 7's
    // exact root (replica 0), so admission must reject it.
    check_rejected_without_mutation(
        coordinator,
        buffer,
        effects,
        proposal(active, "wrong-root", 1),
        ProposalDisposition::rejected_invalid_proposer);

    const auto valid = coordinator.receive(
        proposal(active, "right-root", 0));
    CHECK(valid.disposition == ProposalDisposition::admitted_active);
    CHECK(effects.relays == 1);
    CHECK(effects.active_processing == 1);
}

TEST_CASE("relay policy preserves eager modes and defers only adaptive v2",
          "[proposal-admission][relay-policy][adaptive-v2]")
{
    StagedEpochs fixture;
    const auto active = configuration(*fixture.epoch0, 7);
    const auto future = configuration(*fixture.epoch1, 7);

    SECTION("legacy and adaptive v1 relay active proposals before processing")
    {
        FutureProposalBuffer buffer;
        EffectSpy effects;
        effects.relay_during_processing = false;
        ProposalAdmissionCoordinator coordinator{
            fixture.store,
            active,
            buffer,
            effects,
            ProposalRelayPolicy::eager_before_processing};
        const auto candidate = proposal(active, "eager-active", 0);

        REQUIRE(coordinator.receive(candidate).disposition ==
                ProposalDisposition::admitted_active);
        CHECK((effects.sequence ==
               std::vector<std::string>{"relay", "normal-processing"}));
        CHECK(effects.relays == 1);
        CHECK(effects.active_processing == 1);
    }

    SECTION("legacy and adaptive v1 relay future proposals at ingress")
    {
        FutureProposalBuffer buffer;
        EffectSpy effects;
        effects.relay_during_processing = false;
        ProposalAdmissionCoordinator coordinator{
            fixture.store,
            active,
            buffer,
            effects,
            ProposalRelayPolicy::eager_before_processing};
        const auto candidate = proposal(future, "eager-future", 2);

        REQUIRE(coordinator.receive(candidate).disposition ==
                ProposalDisposition::buffered_future);
        CHECK((effects.sequence == std::vector<std::string>{"relay"}));
        REQUIRE(coordinator.activate(future).size() == 1);
        CHECK((effects.sequence ==
               std::vector<std::string>{"relay", "normal-processing"}));
        CHECK(effects.relays == 1);
        CHECK(effects.active_processing == 1);
    }

    SECTION("adaptive v2 exposes no future proposal before activation")
    {
        FutureProposalBuffer buffer;
        EffectSpy effects;
        ProposalAdmissionCoordinator coordinator{
            fixture.store,
            active,
            buffer,
            effects,
            ProposalRelayPolicy::
                adaptive_v2_deferred_until_arm_attempt};
        const auto candidate = proposal(future, "deferred-future", 2);

        REQUIRE(coordinator.receive(candidate).disposition ==
                ProposalDisposition::buffered_future);
        CHECK(effects.sequence.empty());
        CHECK(coordinator.activate(future).empty());
        CHECK(coordinator.active_configuration() == active);
        CHECK(buffer.contains(candidate.metadata.key()));
        REQUIRE(activate_deferred_configuration(
                    coordinator, buffer, future)
                    .size() == 1);
        CHECK((effects.sequence ==
               std::vector<std::string>{"normal-processing", "relay"}));
        CHECK(effects.relays == 1);
        CHECK(effects.active_processing == 1);
    }
}

TEST_CASE("local vote authorization does not own aggregation initialization",
          "[p05][proposal-admission][active][authorization]"
          "[a06][non-voting][intentional-red]")
{
    StagedEpochs fixture;
    FutureProposalBuffer buffer;
    EffectSpy effects;
    const auto active = configuration(*fixture.epoch0, 7);
    ProposalAdmissionCoordinator coordinator{
        fixture.store,
        active,
        buffer,
        effects,
        ProposalRelayPolicy::adaptive_v2_deferred_until_arm_attempt};
    const auto candidate = proposal(active, "active-block", 0);

    const auto result = coordinator.receive(candidate);
    REQUIRE(result.disposition == ProposalDisposition::admitted_active);
    CHECK((effects.sequence ==
           std::vector<std::string>{"normal-processing", "relay"}));
    CHECK(effects.expected_vote_states == 0);
    CHECK(effects.latency_deadlines == 0);
    CHECK(effects.aggregation_timers == 0);

    REQUIRE(coordinator.authorize_local_vote(candidate.metadata.key()));
    CHECK((effects.sequence == std::vector<std::string>{
                                   "normal-processing",
                                   "relay",
                                   "local-vote"}));
    CHECK(effects.expected_vote_states == 0);
    CHECK(effects.local_votes == 1);
    CHECK(effects.latency_deadlines == 0);
    CHECK(effects.aggregation_timers == 0);
    CHECK_FALSE(
        coordinator.authorize_local_vote(candidate.metadata.key()));
    CHECK(effects.local_votes == 1);
    CHECK(effects.aggregation_timers == 0);
    CHECK(effects.timeout_reports == 0);
}

TEST_CASE("known future proposal remains inert until activation then relays once",
          "[p05][proposal-admission][future][effects]")
{
    StagedEpochs fixture;
    FutureProposalBuffer buffer;
    EffectSpy effects;
    const auto active = configuration(*fixture.epoch0, 7);
    const auto future = configuration(*fixture.epoch1, 7);
    ProposalAdmissionCoordinator coordinator{
        fixture.store,
        active,
        buffer,
        effects,
        ProposalRelayPolicy::adaptive_v2_deferred_until_arm_attempt};
    const auto candidate = proposal(future, "future-block", 2, {0xfa});

    const auto result = coordinator.receive(candidate);
    CHECK(result.disposition == ProposalDisposition::buffered_future);
    CHECK(result.key == candidate.metadata.key());
    CHECK(buffer.size() == 1);
    CHECK(buffer.contains(candidate.metadata.key()));
    CHECK(effects.relays == 0);
    check_no_protocol_side_effects(effects);
    CHECK_FALSE(
        coordinator.authorize_local_vote(candidate.metadata.key()));

    const auto duplicate = coordinator.receive(candidate);
    CHECK(duplicate.disposition == ProposalDisposition::duplicate);
    CHECK(buffer.size() == 1);
    CHECK(effects.relays == 0);
    check_no_protocol_side_effects(effects);

    CHECK(coordinator.activate(future).empty());
    CHECK(coordinator.active_configuration() == active);
    CHECK(buffer.contains(candidate.metadata.key()));
    const auto activated = activate_deferred_configuration(
        coordinator, buffer, future);
    REQUIRE(activated.size() == 1);
    CHECK(activated.front().disposition ==
          ProposalDisposition::admitted_active);
    CHECK(activated.front().key == candidate.metadata.key());
    CHECK(buffer.size() == 0);
    CHECK(effects.relays == 1);
    CHECK(effects.active_processing == 1);
    CHECK(effects.local_votes == 0);
    CHECK(effects.expected_vote_states == 0);
    CHECK(effects.latency_deadlines == 0);
    CHECK(effects.aggregation_timers == 0);
    CHECK(effects.timeout_reports == 0);

    const auto activated_again = activate_deferred_configuration(
        coordinator, buffer, future);
    CHECK(activated_again.empty());
    CHECK(effects.active_processing == 1);
    CHECK(effects.relays == 1);
}

TEST_CASE("future capacity rejection preserves inert buffered proposals",
          "[proposal-admission][future][capacity]")
{
    StagedEpochs fixture;
    FutureProposalBufferLimits limits;
    limits.max_entries = 1;
    limits.max_wire_bytes = 1;
    limits.max_entries_per_configuration_generation = 1;
    limits.max_wire_bytes_per_configuration_generation = 1;
    FutureProposalBuffer buffer{limits};
    EffectSpy effects;
    const auto active = configuration(*fixture.epoch0, 7);
    const auto future_a = configuration(*fixture.epoch1, 7);
    const auto future_b = configuration(*fixture.epoch1, 11);
    ProposalAdmissionCoordinator coordinator{
        fixture.store,
        active,
        buffer,
        effects,
        ProposalRelayPolicy::adaptive_v2_deferred_until_arm_attempt};
    auto admitted = proposal(future_a, "capacity-first", 2, {0x01});
    auto overflow = proposal(future_b, "capacity-overflow", 3, {0x02});
    admitted.view_generation = 1;
    overflow.view_generation = 2;

    REQUIRE(coordinator.receive(admitted).disposition ==
            ProposalDisposition::buffered_future);
    REQUIRE(buffer.size() == 1);
    REQUIRE(effects.relays == 0);
    REQUIRE(coordinator.storage_stats().retained_received == 1);

    const auto rejected = coordinator.receive(overflow);
    CHECK(rejected.disposition ==
          ProposalDisposition::rejected_capacity);
    CHECK_FALSE(buffer.contains(overflow.metadata.key()));
    CHECK(buffer.size() == 1);
    CHECK(effects.relays == 0);
    CHECK(coordinator.storage_stats().retained_received == 1);
    check_no_protocol_side_effects(effects);

    // Duplicate classification is stable even while the buffer is full.
    CHECK(coordinator.receive(admitted).disposition ==
          ProposalDisposition::duplicate);
    CHECK(effects.relays == 0);
    CHECK(coordinator.storage_stats().retained_received == 1);

    // Releasing the retained entry makes the previously rejected exact key
    // admissible, proving overload did not leave a received tombstone.
    REQUIRE(coordinator.retire_proposal(admitted.metadata.key()));
    CHECK(coordinator.receive(overflow).disposition ==
          ProposalDisposition::buffered_future);
    CHECK(buffer.contains(overflow.metadata.key()));
    CHECK(effects.relays == 0);
    CHECK(coordinator.storage_stats().retained_received == 2);

    // Configuration retirement uses the same exact accounting path and
    // releases capacity for an unrelated later configuration.
    REQUIRE(coordinator.retire_configuration(future_b) == 1);
    const auto later = configuration(*fixture.epoch2, 7);
    auto after_retirement =
        proposal(later, "capacity-after-retirement", 2, {0x03});
    after_retirement.view_generation = 3;
    CHECK(coordinator.receive(after_retirement).disposition ==
          ProposalDisposition::buffered_future);
    CHECK(buffer.contains(after_retirement.metadata.key()));
    CHECK(effects.relays == 0);
}

TEST_CASE("future buffering does not execute relay effects before activation",
          "[proposal-admission][future][capacity][exception]")
{
    StagedEpochs fixture;
    FutureProposalBufferLimits limits;
    limits.max_entries = 1;
    limits.max_wire_bytes = 1;
    limits.max_entries_per_configuration_generation = 1;
    limits.max_wire_bytes_per_configuration_generation = 1;
    FutureProposalBuffer buffer{limits};
    EffectSpy effects;
    effects.throw_on_relay = true;
    const auto active = configuration(*fixture.epoch0, 7);
    const auto future = configuration(*fixture.epoch1, 7);
    ProposalAdmissionCoordinator coordinator{
        fixture.store,
        active,
        buffer,
        effects,
        ProposalRelayPolicy::adaptive_v2_deferred_until_arm_attempt};
    auto candidate = proposal(future, "relay-rollback", 2, {0x01});
    candidate.view_generation = 1;

    CHECK(coordinator.receive(candidate).disposition ==
          ProposalDisposition::buffered_future);
    CHECK(buffer.size() == 1);
    CHECK(buffer.contains(candidate.metadata.key()));
    CHECK(coordinator.storage_stats().retained_received == 1);
    CHECK(effects.relays == 0);

    effects.throw_on_relay = false;
    REQUIRE(activate_deferred_configuration(
                coordinator, buffer, future)
                .size() == 1);
    CHECK(buffer.size() == 0);
    CHECK(coordinator.storage_stats().retained_received == 1);
    CHECK(effects.relays == 1);
}

TEST_CASE("same tree ID in a future epoch is never treated active",
          "[p05][proposal-admission][future][cross-epoch]")
{
    StagedEpochs fixture;
    FutureProposalBuffer buffer;
    EffectSpy effects;
    const auto active = configuration(*fixture.epoch0, 7);
    const auto future_same_tree_id = configuration(*fixture.epoch1, 7);
    ProposalAdmissionCoordinator coordinator{
        fixture.store,
        active,
        buffer,
        effects,
        ProposalRelayPolicy::adaptive_v2_deferred_until_arm_attempt};

    const auto result = coordinator.receive(
        proposal(future_same_tree_id, "same-tree-future", 2));
    CHECK(result.disposition == ProposalDisposition::buffered_future);
    CHECK(coordinator.active_configuration() == active);
    CHECK(buffer.size() == 1);
    CHECK(effects.relays == 0);
    check_no_protocol_side_effects(effects);
}

TEST_CASE("unknown future configuration is rejected rather than guessed",
          "[p05][proposal-admission][future][unknown]")
{
    StagedEpochs fixture;
    FutureProposalBuffer buffer;
    EffectSpy effects;
    const auto active = configuration(*fixture.epoch0, 7);
    ProposalAdmissionCoordinator coordinator{
        fixture.store,
        active,
        buffer,
        effects,
        ProposalRelayPolicy::adaptive_v2_deferred_until_arm_attempt};

    const ConfigurationId unknown_future{
        3, 7, digest("not-staged")};
    check_rejected_without_mutation(
        coordinator,
        buffer,
        effects,
        proposal(unknown_future, "unknown-future", 0),
        ProposalDisposition::rejected_unknown_configuration);
}

TEST_CASE("activation drains exact configuration once without head blocking",
          "[p05][proposal-admission][activation][a06][intentional-red]")
{
    StagedEpochs fixture;
    FutureProposalBuffer buffer;
    EffectSpy effects;
    const auto active = configuration(*fixture.epoch0, 7);
    const auto next = configuration(*fixture.epoch1, 7);
    const auto next_other_tree = configuration(*fixture.epoch1, 11);
    const auto later = configuration(*fixture.epoch2, 7);
    ProposalAdmissionCoordinator coordinator{
        fixture.store,
        active,
        buffer,
        effects,
        ProposalRelayPolicy::adaptive_v2_deferred_until_arm_attempt};

    const auto later_first = proposal(later, "later-first", 2, {0x21});
    const auto next_first = proposal(next, "next-first", 2, {0x11});
    const auto next_second = proposal(next, "next-second", 2, {0x12});
    const auto next_other =
        proposal(next_other_tree, "next-other-tree", 3, {0x13});
    REQUIRE(coordinator.receive(later_first).disposition ==
            ProposalDisposition::buffered_future);
    REQUIRE(coordinator.receive(next_first).disposition ==
            ProposalDisposition::buffered_future);
    REQUIRE(coordinator.receive(next_second).disposition ==
            ProposalDisposition::buffered_future);
    REQUIRE(coordinator.receive(next_other).disposition ==
            ProposalDisposition::buffered_future);
    REQUIRE(buffer.size() == 4);
    REQUIRE(effects.relays == 0);

    auto wrong_digest = next;
    wrong_digest.epoch_digest = digest("divergent-next-definition");
    const auto refused_activation = coordinator.activate(wrong_digest);
    CHECK(refused_activation.empty());
    CHECK(coordinator.active_configuration() == active);
    CHECK(buffer.size() == 4);
    CHECK(effects.processed.empty());

    CHECK(coordinator.activate(next).empty());
    CHECK(coordinator.active_configuration() == active);
    CHECK(buffer.size() == 4);
    const auto activated = activate_deferred_configuration(
        coordinator, buffer, next);
    REQUIRE(activated.size() == 2);
    CHECK(activated[0].disposition ==
          ProposalDisposition::admitted_active);
    CHECK(activated[0].key == next_first.metadata.key());
    CHECK(activated[1].disposition ==
          ProposalDisposition::admitted_active);
    CHECK(activated[1].key == next_second.metadata.key());
    CHECK(coordinator.active_configuration() == next);
    CHECK(buffer.size() == 2);
    CHECK(buffer.contains(later_first.metadata.key()));
    CHECK(buffer.contains(next_other.metadata.key()));
    CHECK(effects.relays == 2);
    REQUIRE(effects.processed.size() == 2);
    CHECK(effects.processed[0] == next_first.metadata.key());
    CHECK(effects.processed[1] == next_second.metadata.key());
    CHECK(effects.expected_vote_states == 0);
    CHECK(effects.local_votes == 0);
    CHECK(effects.latency_deadlines == 0);
    CHECK(effects.aggregation_timers == 0);
    CHECK(effects.timeout_reports == 0);

    const auto activated_again = coordinator.activate(next);
    CHECK(activated_again.empty());
    CHECK(effects.processed.size() == 2);
    CHECK(effects.relays == 2);

    REQUIRE(coordinator.authorize_local_vote(
        next_first.metadata.key()));
    CHECK(effects.local_votes == 1);
    CHECK(effects.expected_vote_states == 0);
    CHECK(effects.aggregation_timers == 0);
}

TEST_CASE("terminal proposal retirement preserves a bounded replay guard",
          "[proposal-retirement][proposal-admission][terminal]"
          "[intentional-red]")
{
    check_terminal_proposal_pruning<ProposalAdmissionCoordinator>();
}

TEST_CASE("commit retires admission after an earlier terminal transition",
          "[proposal-retirement][proposal-admission][terminal-before-commit]"
          "[integration][intentional-red]")
{
    StagedEpochs fixture;
    FutureProposalBuffer buffer;
    EffectSpy effects;
    const auto active = configuration(*fixture.epoch0, 7);
    ProposalAdmissionCoordinator coordinator{
        fixture.store,
        active,
        buffer,
        effects,
        ProposalRelayPolicy::adaptive_v2_deferred_until_arm_attempt};
    ProposalContextLifecycle contexts;
    const auto candidate = proposal(
        active, "terminal-before-commit-admission", 0);
    const auto key = candidate.metadata.key();

    REQUIRE(coordinator.receive(candidate).disposition ==
            ProposalDisposition::admitted_active);
    REQUIRE(coordinator.authorize_local_vote(key));
    REQUIRE(coordinator.contains_admitted(key));

    const auto context_metadata =
        hotstuff::make_exact_proposal_context_metadata(
            key, 3, membership(), 2, 2, 5);
    REQUIRE(context_metadata.has_value());
    auto lease = contexts.admit_remote(*context_metadata);
    REQUIRE(lease.has_value());
    REQUIRE(contexts.record_local_signer(*lease));
    REQUIRE(contexts.transition(
                *lease, ProposalContextEvent::leaf_vote_enqueued) ==
            ProposalTransitionResult::terminal_closed);
    REQUIRE(contexts.context_status(key) ==
            ProposalContextStatus::terminal_closed);

    // The terminal transition may release runtime state, but commit discovery
    // must still return the exact key so the admission owner can retire it.
    const auto committed = contexts.close_committed_block(key.block_hash);
    CHECK(committed == std::vector<ProposalKey>{key});
    for (const auto &committed_key : committed)
        coordinator.retire_proposal(committed_key);

    const auto after_commit = coordinator.storage_stats();
    CHECK_FALSE(coordinator.contains_admitted(key));
    CHECK_FALSE(coordinator.authorize_local_vote(key));
    CHECK(after_commit.retained_received == 1);
    CHECK(after_commit.retained_admitted == 0);
    CHECK(after_commit.retained_local_authorizations == 0);

    const auto effects_before_replay = effects.sequence;
    CHECK(coordinator.receive(candidate).disposition ==
          ProposalDisposition::duplicate);
    CHECK(effects.sequence == effects_before_replay);
    CHECK(effects.relays == 1);
    CHECK(effects.active_processing == 1);
    CHECK(effects.local_votes == 1);

    CHECK(coordinator.retire_configuration(active) == 1);
    CHECK(coordinator.advance_retirement_floor(1) == 1);
    const auto compacted = coordinator.storage_stats();
    CHECK(compacted.retained_received == 0);
    CHECK(compacted.retained_admitted == 0);
    CHECK(compacted.retained_local_authorizations == 0);
    CHECK(compacted.retired_configuration_tombstones == 0);
}

TEST_CASE("exact configuration retirement purges admission and future state",
          "[proposal-retirement][proposal-admission][configuration]"
          "[intentional-red]")
{
    check_exact_configuration_retirement<ProposalAdmissionCoordinator>();
}

TEST_CASE("retirement floor prunes old epochs without removing first-live",
          "[proposal-retirement][proposal-admission][floor]"
          "[intentional-red]")
{
    check_retirement_floor_contract<ProposalAdmissionCoordinator>();
}

TEST_CASE("same-epoch A to B to A activation preserves in-flight admission",
          "[proposal-retirement][proposal-admission][activation]"
          "[intentional-red]")
{
    check_same_epoch_reactivation_contract<
        ProposalAdmissionCoordinator>();
}

#endif
