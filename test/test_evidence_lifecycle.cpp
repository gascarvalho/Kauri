#include <array>
#include <cstddef>
#include <cstdint>
#include <cstdlib>
#include <limits>
#include <memory>
#include <new>
#include <optional>
#include <stdexcept>
#include <string>
#include <type_traits>
#include <utility>
#include <variant>
#include <vector>

#include "catch.hpp"
#include "hotstuff/epoch_store.h"
#include "hotstuff/evidence.h"
#include "hotstuff/proposal_evidence_index.h"

namespace evidence_lifecycle_allocation_failure
{

constexpr std::size_t disabled = std::numeric_limits<std::size_t>::max();
thread_local std::size_t allocations_before_failure = disabled;
thread_local bool injected_failure = false;

bool consume_failure() noexcept
{
    if (allocations_before_failure == disabled)
        return false;
    if (allocations_before_failure == 0)
    {
        allocations_before_failure = disabled;
        injected_failure = true;
        return true;
    }
    --allocations_before_failure;
    return false;
}

void disable() noexcept
{
    allocations_before_failure = disabled;
}

class OneShot final
{
public:
    explicit OneShot(std::size_t successful_allocations) noexcept
    {
        injected_failure = false;
        allocations_before_failure = successful_allocations;
    }

    ~OneShot()
    {
        disable();
    }

    OneShot(const OneShot &) = delete;
    OneShot &operator=(const OneShot &) = delete;

    bool triggered() const noexcept
    {
        return injected_failure;
    }
};

} // namespace evidence_lifecycle_allocation_failure

void *operator new(std::size_t size)
{
    if (evidence_lifecycle_allocation_failure::consume_failure())
        throw std::bad_alloc();
    if (size == 0)
        size = 1;
    if (auto *const allocation = std::malloc(size))
        return allocation;
    throw std::bad_alloc();
}

void operator delete(void *allocation) noexcept
{
    std::free(allocation);
}

void operator delete(void *allocation, std::size_t) noexcept
{
    std::free(allocation);
}

/*
 * E08 trusted proposal-lifecycle and quarantine contract
 * ------------------------------------------------------
 * This is a pure manager-side seam. The caller supplies a replica identity
 * derived from an authenticated configured connection and externally
 * serializes every operation. This slice does not own certificate mapping,
 * mutual-TLS setup, sockets, or live message delivery.
 *
 * A normal-runtime-initialized fact is emitted only after all exact proposal
 * runtime state has been initialized. Merely staging or activating an epoch is
 * deliberately not a lifecycle fact. Unknown evidence stays outside the
 * EvidenceLedger in a bounded per-reporter FIFO. Lifecycle changes retry FIFO
 * heads through the real ledger, so its accepted/rejected audit and ingestion
 * sequence remain the only evidence audit semantics.
 *
 * If allocation fails while a lifecycle notice opens a quarantined head, the
 * proposal index may expose only one of two states: the exact pre-call state,
 * or one exact newly admitted key in an immediately poisoned/stopped index.
 * The latter is an irreversible prefix, not a usable admission: classify()
 * must return unknown, both coordinator and ledger fail closed, the queued
 * head remains owned, and every later call is an identical no-op.
 */
#if __has_include("hotstuff/evidence_lifecycle.h")
#include "hotstuff/evidence_lifecycle.h"
#define KAURI_HAS_EVIDENCE_LIFECYCLE_API 1
#else
#define KAURI_HAS_EVIDENCE_LIFECYCLE_API 0

namespace hotstuff
{

constexpr std::uint32_t kProposalLifecycleNoticeSchemaVersion = 1;

struct NormalProposalRuntimeInitialized
{
    ProposalKey proposal;
};

struct ProposalRuntimeAborted
{
    ProposalKey proposal;
};

struct ProposalCommitted
{
    ProposalKey proposal;
};

struct ProposalConfigurationRetired
{
    ConfigurationId configuration;
};

struct ProposalRetirementFloorAdvanced
{
    std::uint32_t first_live_epoch{0};
};

using ProposalLifecycleFact = std::variant<
    NormalProposalRuntimeInitialized,
    ProposalRuntimeAborted,
    ProposalCommitted,
    ProposalConfigurationRetired,
    ProposalRetirementFloorAdvanced>;

struct ProposalLifecycleNotice
{
    std::uint32_t schema_version{
        kProposalLifecycleNoticeSchemaVersion};
    ReplicaID source_replica_id{0};
    std::uint64_t source_sequence{0};
    ProposalLifecycleFact fact;
};

struct EvidenceLifecycleLimits
{
    std::size_t maximum_quarantined_records{4096};
    std::size_t maximum_quarantined_bytes{4 * 1024 * 1024};
    std::size_t maximum_reporter_queues{1024};
    std::size_t maximum_signer_entries{65536};
    std::size_t maximum_deduplication_entries{4096};
    std::size_t maximum_lifecycle_sources{1024};
};

struct EvidenceLifecycleAccountingLimits
{
    std::size_t maximum_records{4096};
    std::size_t maximum_canonical_bytes{4 * 1024 * 1024};
    std::size_t maximum_signer_entries{65536};
};

struct EvidenceRetentionCost
{
    std::size_t canonical_bytes{0};
    std::size_t signer_entries{0};
};

struct EvidenceLifecycleAccountingStats
{
    std::size_t retained_records{0};
    std::size_t retained_canonical_bytes{0};
    std::size_t retained_signer_entries{0};
};

/**
 * Coordinator-owned retained-evidence accounting. A retain adds exactly one
 * record plus its canonical byte and signer costs. Both retain and release
 * are checked, all-or-nothing operations: overflow, limit excess, underflow,
 * or a mismatched release returns false without changing any counter.
 */
class EvidenceLifecycleAccounting final
{
public:
    explicit EvidenceLifecycleAccounting(
        EvidenceLifecycleAccountingLimits limits = {}) noexcept;

    bool try_retain(EvidenceRetentionCost cost) noexcept;
    bool release(EvidenceRetentionCost cost) noexcept;
    EvidenceLifecycleAccountingLimits limits() const noexcept;
    EvidenceLifecycleAccountingStats stats() const noexcept;

private:
    EvidenceLifecycleAccountingLimits limits_;
    EvidenceLifecycleAccountingStats stats_;
};

enum class ProposalLifecycleApplyStatus : std::uint8_t
{
    applied = 1,
    rejected_schema,
    rejected_authentication,
    rejected_sequence,
    evidence_unhealthy,
    stopped,
};

enum class EvidenceObservationDisposition : std::uint8_t
{
    ingested = 1,
    quarantined_unknown,
    quarantined_behind_unknown,
    duplicate_quarantined,
    evidence_unhealthy,
    stopped,
};

struct ProposalLifecycleApplyResult
{
    ProposalLifecycleApplyStatus status{
        ProposalLifecycleApplyStatus::applied};
    bool index_changed{false};
    std::size_t retried_observations{0};
    std::size_t accepted_observations{0};
    std::size_t rejected_observations{0};
    std::size_t remaining_quarantined{0};
};

struct EvidenceObservationResult
{
    EvidenceObservationDisposition disposition{
        EvidenceObservationDisposition::ingested};
    std::size_t accepted_observations{0};
    std::size_t rejected_observations{0};
    std::size_t quarantined_observations{0};
};

struct EvidenceLifecycleStats
{
    std::size_t quarantined_records{0};
    std::size_t quarantined_bytes{0};
    std::size_t reporter_queues{0};
    std::size_t signer_entries{0};
    std::size_t deduplication_entries{0};
    std::size_t lifecycle_sources{0};
    std::uint64_t duplicate_observations{0};
    std::uint64_t applied_lifecycle_notices{0};
    std::uint64_t capacity_failures{0};
    bool healthy{true};
    bool stopped{false};
};

/**
 * Deterministic diagnostic view ordered by reporter id and then reporter FIFO.
 * Exactly the first retained observation for each reporter is marked as head.
 */
struct QuarantinedEvidenceObservation
{
    AuthenticatedReporter authenticated_reporter;
    ResponseObservation observation;
    bool reporter_fifo_head{false};
};

class ProposalLifecycleEvidenceCoordinator final
{
public:
    ProposalLifecycleEvidenceCoordinator(
        ProposalEvidenceIndex &proposal_index,
        EvidenceLedger &ledger,
        EvidenceLifecycleAccounting accounting,
        EvidenceLifecycleLimits limits = {});
    ~ProposalLifecycleEvidenceCoordinator();

    ProposalLifecycleEvidenceCoordinator(
        const ProposalLifecycleEvidenceCoordinator &) = delete;
    ProposalLifecycleEvidenceCoordinator &operator=(
        const ProposalLifecycleEvidenceCoordinator &) = delete;
    ProposalLifecycleEvidenceCoordinator(
        ProposalLifecycleEvidenceCoordinator &&) = delete;
    ProposalLifecycleEvidenceCoordinator &operator=(
        ProposalLifecycleEvidenceCoordinator &&) = delete;

    ProposalLifecycleApplyResult apply_notice(
        const AuthenticatedReporter &authenticated_source,
        const ProposalLifecycleNotice &notice) noexcept;

    EvidenceObservationResult ingest_observation(
        const AuthenticatedReporter &authenticated_reporter,
        const ResponseObservation &observation) noexcept;

    EvidenceLifecycleStats stats() const noexcept;
    const EvidenceLifecycleAccounting &accounting() const noexcept;
    std::vector<QuarantinedEvidenceObservation>
    quarantined_observations() const;
    bool healthy() const noexcept;
    void shutdown() noexcept;

private:
    struct State;
    std::unique_ptr<State> state_;
};

} // namespace hotstuff
#endif

namespace
{

using hotstuff::AuthenticatedReporter;
using hotstuff::ConfigurationId;
using hotstuff::DataStream;
using hotstuff::EpochDefinitionInput;
using hotstuff::EpochStore;
using hotstuff::EpochTreeDefinition;
using hotstuff::EpochValidationContext;
using hotstuff::EvidenceLedger;
using hotstuff::EvidenceLifecycleAccounting;
using hotstuff::EvidenceLifecycleAccountingLimits;
using hotstuff::EvidenceLifecycleAccountingStats;
using hotstuff::EvidenceLifecycleLimits;
using hotstuff::EvidenceLifecycleStats;
using hotstuff::EvidenceObservationDisposition;
using hotstuff::EvidenceObservationResult;
using hotstuff::EvidenceRejectionReason;
using hotstuff::EvidenceStoreLimits;
using hotstuff::ExpectedMessageType;
using hotstuff::NormalProposalRuntimeInitialized;
using hotstuff::ProposalCommitted;
using hotstuff::ProposalConfigurationRetired;
using hotstuff::ProposalEvidenceIndex;
using hotstuff::ProposalEvidenceIndexLimits;
using hotstuff::ProposalEvidenceStatus;
using hotstuff::ProposalKey;
using hotstuff::ProposalLifecycleApplyResult;
using hotstuff::ProposalLifecycleApplyStatus;
using hotstuff::ProposalLifecycleEvidenceCoordinator;
using hotstuff::ProposalLifecycleFact;
using hotstuff::ProposalLifecycleNotice;
using hotstuff::ProposalRetirementFloorAdvanced;
using hotstuff::ProposalRuntimeAborted;
using hotstuff::QuarantinedEvidenceObservation;
using hotstuff::ReplicaID;
using hotstuff::EvidenceRetentionCost;
using hotstuff::ResponseObservation;
using hotstuff::ResponseOutcome;
using hotstuff::uint256_t;

constexpr std::uint32_t kTreeId = 7;

uint256_t digest(const std::string &label)
{
    return DataStream(label).get_hash();
}

std::vector<ReplicaID> membership()
{
    return {0, 1, 2, 3, 4, 5, 6};
}

EpochDefinitionInput epoch_input()
{
    EpochDefinitionInput input;
    input.schema_version = hotstuff::kEpochDefinitionSchemaVersion;
    input.epoch_number = 0;
    input.previous_epoch_digest = {};
    input.membership_digest =
        hotstuff::canonical_membership_digest(membership());
    input.trees = {
        EpochTreeDefinition{kTreeId, 2, 2, membership()},
    };
    input.activation_height = 15;
    input.generation_seed = 0xE0811FE;
    input.policy_version = "e08-lifecycle-policy-v1";
    input.evidence_snapshot_id = "e08-lifecycle-window-0001";
    input.evidence_cutoff = 100;
    input.epoch_digest.reset();
    return input;
}

EpochValidationContext validation_context()
{
    EpochValidationContext context;
    context.current_height = 10;
    context.minimum_activation_grace = 5;
    return context;
}

EvidenceLifecycleLimits lifecycle_limits()
{
    return {32, 64 * 1024, 8, 64, 32, 8};
}

EvidenceLifecycleAccountingLimits accounting_limits(
    const EvidenceLifecycleLimits &limits) noexcept
{
    return {
        limits.maximum_quarantined_records,
        limits.maximum_quarantined_bytes,
        limits.maximum_signer_entries};
}

ProposalEvidenceIndexLimits index_limits()
{
    return {64, 16};
}

EvidenceStoreLimits store_limits()
{
    return {128, 128};
}

struct Fixture
{
    EpochStore epochs{membership()};
    ConfigurationId configuration;
    ProposalEvidenceIndex index{index_limits()};
    std::unique_ptr<EvidenceLedger> ledger;
    std::unique_ptr<ProposalLifecycleEvidenceCoordinator> coordinator;

    explicit Fixture(EvidenceLifecycleLimits limits = lifecycle_limits())
    {
        const auto &epoch = epochs.stage(
            epoch_input(), validation_context());
        configuration = {0, kTreeId, epoch.epoch_digest()};
        ledger = std::make_unique<EvidenceLedger>(
            epochs, index, store_limits());
        coordinator =
            std::make_unique<ProposalLifecycleEvidenceCoordinator>(
                index,
                *ledger,
                EvidenceLifecycleAccounting{accounting_limits(limits)},
                limits);
    }

    ProposalKey key(const std::string &block) const
    {
        return {configuration, digest(block)};
    }
};

ResponseObservation observation(
    const ProposalKey &key,
    ReplicaID reporter,
    ReplicaID observed,
    std::uint64_t reporter_sequence,
    ResponseOutcome outcome = ResponseOutcome::on_time)
{
    ResponseObservation value;
    value.reporter_id = reporter;
    value.observed_replica_id = observed;
    value.configuration = key.configuration;
    value.block_hash = key.block_hash;
    value.expected_message_type = ExpectedMessageType::direct_vote;
    value.outcome = outcome;
    value.deadline_duration_us = 100;
    value.response_duration_us = outcome == ResponseOutcome::timeout
                                     ? 0
                                     : outcome == ResponseOutcome::late
                                           ? 150
                                           : 50;
    value.reporter_monotonic_ns = reporter_sequence * 1000;
    value.reporter_sequence = reporter_sequence;
    if (outcome != ResponseOutcome::timeout)
        value.signer_set = {observed};
    value.observation_id = hotstuff::compute_response_observation_id(
        value.attempt_identity());
    return value;
}

template<typename Fact>
ProposalLifecycleNotice notice(
    ReplicaID source,
    std::uint64_t sequence,
    Fact fact)
{
    return ProposalLifecycleNotice{
        hotstuff::kProposalLifecycleNoticeSchemaVersion,
        source,
        sequence,
        ProposalLifecycleFact{std::move(fact)}};
}

ProposalLifecycleApplyResult admit(
    Fixture &fixture,
    ReplicaID source,
    std::uint64_t sequence,
    const ProposalKey &key)
{
    return fixture.coordinator->apply_notice(
        AuthenticatedReporter{source},
        notice(
            source,
            sequence,
            NormalProposalRuntimeInitialized{key}));
}

void check_empty_quarantine(
    const ProposalLifecycleEvidenceCoordinator &coordinator)
{
    const auto stats = coordinator.stats();
    CHECK(stats.quarantined_records == 0);
    CHECK(stats.quarantined_bytes == 0);
    CHECK(stats.reporter_queues == 0);
    CHECK(stats.signer_entries == 0);
    CHECK(stats.deduplication_entries == 0);
    CHECK(coordinator.quarantined_observations().empty());
    const auto accounting = coordinator.accounting().stats();
    CHECK(accounting.retained_records == 0);
    CHECK(accounting.retained_canonical_bytes == 0);
    CHECK(accounting.retained_signer_entries == 0);
}

bool same_retained_state(
    const EvidenceLifecycleStats &left,
    const EvidenceLifecycleStats &right) noexcept
{
    return left.quarantined_records == right.quarantined_records &&
           left.quarantined_bytes == right.quarantined_bytes &&
           left.reporter_queues == right.reporter_queues &&
           left.signer_entries == right.signer_entries &&
           left.deduplication_entries ==
               right.deduplication_entries &&
           left.lifecycle_sources == right.lifecycle_sources &&
           left.duplicate_observations ==
               right.duplicate_observations &&
           left.applied_lifecycle_notices ==
               right.applied_lifecycle_notices &&
           left.stopped == right.stopped;
}

bool same_index_state(
    const hotstuff::ProposalEvidenceIndexStats &left,
    const hotstuff::ProposalEvidenceIndexStats &right) noexcept
{
    return left.admissible_proposals == right.admissible_proposals &&
           left.stale_proposals == right.stale_proposals &&
           left.retired_configurations ==
               right.retired_configurations &&
           left.first_live_epoch == right.first_live_epoch &&
           left.capacity_failures == right.capacity_failures &&
           left.healthy == right.healthy &&
           left.stopped == right.stopped;
}

bool same_lifecycle_state(
    const EvidenceLifecycleStats &left,
    const EvidenceLifecycleStats &right) noexcept
{
    return same_retained_state(left, right) &&
           left.capacity_failures == right.capacity_failures &&
           left.healthy == right.healthy;
}

bool same_accounting_state(
    const EvidenceLifecycleAccountingStats &left,
    const EvidenceLifecycleAccountingStats &right) noexcept
{
    return left.retained_records == right.retained_records &&
           left.retained_canonical_bytes ==
               right.retained_canonical_bytes &&
           left.retained_signer_entries ==
               right.retained_signer_entries;
}

bool same_accounting_limits(
    const EvidenceLifecycleAccountingLimits &left,
    const EvidenceLifecycleAccountingLimits &right) noexcept
{
    return left.maximum_records == right.maximum_records &&
           left.maximum_canonical_bytes ==
               right.maximum_canonical_bytes &&
           left.maximum_signer_entries == right.maximum_signer_entries;
}

bool same_observation(
    const ResponseObservation &left,
    const ResponseObservation &right) noexcept
{
    return left.schema_version == right.schema_version &&
           left.observation_id == right.observation_id &&
           left.reporter_id == right.reporter_id &&
           left.observed_replica_id == right.observed_replica_id &&
           left.proposal_key() == right.proposal_key() &&
           left.expected_message_type == right.expected_message_type &&
           left.outcome == right.outcome &&
           left.response_duration_us == right.response_duration_us &&
           left.deadline_duration_us == right.deadline_duration_us &&
           left.reporter_monotonic_ns == right.reporter_monotonic_ns &&
           left.reporter_sequence == right.reporter_sequence &&
           left.signer_set == right.signer_set;
}

bool same_quarantine_snapshot(
    const std::vector<QuarantinedEvidenceObservation> &left,
    const std::vector<QuarantinedEvidenceObservation> &right) noexcept
{
    if (left.size() != right.size())
        return false;
    for (std::size_t index = 0; index < left.size(); ++index)
    {
        if (left[index].authenticated_reporter.replica_id !=
                right[index].authenticated_reporter.replica_id ||
            left[index].reporter_fifo_head !=
                right[index].reporter_fifo_head ||
            !same_observation(
                left[index].observation, right[index].observation))
        {
            return false;
        }
    }
    return true;
}

void check_quarantine_snapshot(
    const std::vector<QuarantinedEvidenceObservation> &actual,
    const std::vector<QuarantinedEvidenceObservation> &expected)
{
    CHECK(same_quarantine_snapshot(actual, expected));
    REQUIRE(actual.size() == expected.size());
    for (std::size_t index = 0; index < actual.size(); ++index)
    {
        CAPTURE(index);
        const auto &left = actual[index];
        const auto &right = expected[index];
        CHECK(left.authenticated_reporter.replica_id ==
              right.authenticated_reporter.replica_id);
        CHECK(left.reporter_fifo_head == right.reporter_fifo_head);
        CHECK(left.observation.schema_version ==
              right.observation.schema_version);
        CHECK(left.observation.observation_id ==
              right.observation.observation_id);
        CHECK(left.observation.reporter_id == right.observation.reporter_id);
        CHECK(left.observation.observed_replica_id ==
              right.observation.observed_replica_id);
        CHECK(left.observation.proposal_key() ==
              right.observation.proposal_key());
        CHECK(left.observation.expected_message_type ==
              right.observation.expected_message_type);
        CHECK(left.observation.outcome == right.observation.outcome);
        CHECK(left.observation.response_duration_us ==
              right.observation.response_duration_us);
        CHECK(left.observation.deadline_duration_us ==
              right.observation.deadline_duration_us);
        CHECK(left.observation.reporter_monotonic_ns ==
              right.observation.reporter_monotonic_ns);
        CHECK(left.observation.reporter_sequence ==
              right.observation.reporter_sequence);
        CHECK(left.observation.signer_set == right.observation.signer_set);
    }
}

bool exact_single_admission_prefix(
    const hotstuff::ProposalEvidenceIndexStats &before,
    const hotstuff::ProposalEvidenceIndexStats &after) noexcept
{
    return before.admissible_proposals !=
               std::numeric_limits<std::size_t>::max() &&
           after.admissible_proposals ==
               before.admissible_proposals + 1 &&
           after.stale_proposals == before.stale_proposals &&
           after.retired_configurations ==
               before.retired_configurations &&
           after.first_live_epoch == before.first_live_epoch &&
           after.capacity_failures == before.capacity_failures &&
           (!after.healthy || after.stopped);
}

void check_zero_lifecycle_limit(EvidenceLifecycleLimits limits)
{
    Fixture fixture(limits);
    const auto key = fixture.key("independent-zero-limit");
    const auto before = fixture.coordinator->stats();
    const auto accounting_before =
        fixture.coordinator->accounting().stats();
    const auto accounting_limits_before =
        fixture.coordinator->accounting().limits();
    const auto index_before = fixture.index.stats();
    const auto ledger_health_before = fixture.ledger->healthy();

    REQUIRE_FALSE(fixture.coordinator->healthy());
    REQUIRE_FALSE(before.healthy);
    CHECK(before.quarantined_records == 0);
    CHECK(before.quarantined_bytes == 0);
    CHECK(before.reporter_queues == 0);
    CHECK(before.signer_entries == 0);
    CHECK(before.deduplication_entries == 0);
    CHECK(before.lifecycle_sources == 0);

    const auto evidence = fixture.coordinator->ingest_observation(
        AuthenticatedReporter{1}, observation(key, 1, 3, 1));
    CHECK(evidence.disposition ==
          EvidenceObservationDisposition::evidence_unhealthy);
    CHECK(evidence.accepted_observations == 0);
    CHECK(evidence.rejected_observations == 0);
    CHECK(evidence.quarantined_observations == 0);
    const auto lifecycle = admit(fixture, 1, 1, key);
    CHECK(lifecycle.status ==
          ProposalLifecycleApplyStatus::evidence_unhealthy);
    CHECK_FALSE(lifecycle.index_changed);
    CHECK(lifecycle.retried_observations == 0);
    CHECK(lifecycle.accepted_observations == 0);
    CHECK(lifecycle.rejected_observations == 0);
    CHECK(lifecycle.remaining_quarantined == 0);

    CHECK(same_lifecycle_state(
        fixture.coordinator->stats(), before));
    CHECK(same_accounting_state(
        fixture.coordinator->accounting().stats(), accounting_before));
    CHECK(same_accounting_limits(
        fixture.coordinator->accounting().limits(),
        accounting_limits_before));
    CHECK(fixture.coordinator->quarantined_observations().empty());
    CHECK(same_index_state(fixture.index.stats(), index_before));
    CHECK(fixture.index.classify(key) == ProposalEvidenceStatus::unknown);
    CHECK(fixture.ledger->high_watermark() == 0);
    CHECK(fixture.ledger->accepted().empty());
    CHECK(fixture.ledger->rejected().empty());
    CHECK(fixture.ledger->healthy() == ledger_health_before);
}

template<typename T, typename = void>
struct has_activation_mutator : std::false_type
{};

template<typename T>
struct has_activation_mutator<
    T,
    std::void_t<decltype(std::declval<T &>().activate_configuration(
        std::declval<const ConfigurationId &>()))>> : std::true_type
{};

template<typename T, typename = void>
struct has_expiry_mutator : std::false_type
{};

template<typename T>
struct has_expiry_mutator<
    T,
    std::void_t<decltype(std::declval<T &>().expire(
        std::declval<std::uint64_t>()))>> : std::true_type
{};

} // namespace

TEST_CASE("E08 lifecycle coordinator exposes one borrowed bounded seam",
          "[e08][evidence-lifecycle][contract][intentional-red]")
{
    CHECK(KAURI_HAS_EVIDENCE_LIFECYCLE_API == 1);
    CHECK(hotstuff::kProposalLifecycleNoticeSchemaVersion == 1);

    const EvidenceLifecycleLimits defaults;
    CHECK(defaults.maximum_quarantined_records == 4096);
    CHECK(defaults.maximum_quarantined_bytes == 4 * 1024 * 1024);
    CHECK(defaults.maximum_reporter_queues == 1024);
    CHECK(defaults.maximum_signer_entries == 65536);
    CHECK(defaults.maximum_deduplication_entries == 4096);
    CHECK(defaults.maximum_lifecycle_sources == 1024);
    const EvidenceLifecycleAccounting default_accounting;
    CHECK(same_accounting_limits(
        default_accounting.limits(),
        EvidenceLifecycleAccountingLimits{}));
    CHECK(same_accounting_state(
        default_accounting.stats(),
        EvidenceLifecycleAccountingStats{}));

    static_assert(
        std::is_final<ProposalLifecycleEvidenceCoordinator>::value,
        "the manager has one externally serialized lifecycle owner");
    static_assert(
        !std::is_copy_constructible<
            ProposalLifecycleEvidenceCoordinator>::value,
        "copying would fork proposal and quarantine ordering");
    static_assert(
        !std::is_move_constructible<
            ProposalLifecycleEvidenceCoordinator>::value,
        "moving would invalidate the borrowed index and ledger");
    static_assert(
        std::is_constructible<
            ProposalLifecycleEvidenceCoordinator,
            ProposalEvidenceIndex &,
            EvidenceLedger &,
            EvidenceLifecycleAccounting,
            EvidenceLifecycleLimits>::value,
        "the seam borrows index/ledger and owns checked accounting");
    static_assert(
        std::variant_size<ProposalLifecycleFact>::value == 5,
        "only normal init, abort, commit, config retire and floor exist");
    static_assert(
        !has_activation_mutator<
            ProposalLifecycleEvidenceCoordinator>::value,
        "activation alone must never admit an exact proposal");
    static_assert(
        !has_expiry_mutator<
            ProposalLifecycleEvidenceCoordinator>::value,
        "quarantine has no wall-clock expiry path");

    using Apply = ProposalLifecycleApplyResult (
        ProposalLifecycleEvidenceCoordinator::*)(
            const AuthenticatedReporter &,
            const ProposalLifecycleNotice &) noexcept;
    using Ingest = EvidenceObservationResult (
        ProposalLifecycleEvidenceCoordinator::*)(
            const AuthenticatedReporter &,
            const ResponseObservation &) noexcept;
    using Stats = EvidenceLifecycleStats (
        ProposalLifecycleEvidenceCoordinator::*)() const noexcept;
    using Accounting = const EvidenceLifecycleAccounting &(
        ProposalLifecycleEvidenceCoordinator::*)() const noexcept;
    using QuarantineSnapshot =
        std::vector<QuarantinedEvidenceObservation> (
            ProposalLifecycleEvidenceCoordinator::*)() const;

    static_assert(
        std::is_same<
            decltype(&ProposalLifecycleEvidenceCoordinator::apply_notice),
            Apply>::value,
        "lifecycle failures cannot throw into protocol control flow");
    static_assert(
        std::is_same<
            decltype(
                &ProposalLifecycleEvidenceCoordinator::ingest_observation),
            Ingest>::value,
        "quarantine failures cannot throw into protocol control flow");
    static_assert(
        std::is_same<
            decltype(&ProposalLifecycleEvidenceCoordinator::stats),
            Stats>::value,
        "manager diagnostics are an immutable bounded snapshot");
    static_assert(
        std::is_same<
            decltype(&ProposalLifecycleEvidenceCoordinator::accounting),
            Accounting>::value,
        "coordinator exposes its owned accounting seam read-only");
    static_assert(
        std::is_same<
            decltype(
                &ProposalLifecycleEvidenceCoordinator::
                    quarantined_observations),
            QuarantineSnapshot>::value,
        "diagnostics expose deterministic reporter FIFO ownership");
}

TEST_CASE("authenticated lifecycle sequence is separate from observation sequence",
          "[e08][evidence-lifecycle][authentication][ordering]"
          "[intentional-red]")
{
    Fixture fixture;
    const auto first = fixture.key("auth-first");
    const auto second = fixture.key("auth-second");

    const auto mismatch = fixture.coordinator->apply_notice(
        AuthenticatedReporter{1},
        notice(
            2,
            7,
            NormalProposalRuntimeInitialized{first}));
    CHECK(mismatch.status ==
          ProposalLifecycleApplyStatus::rejected_authentication);
    CHECK(fixture.index.classify(first) == ProposalEvidenceStatus::unknown);
    CHECK(fixture.ledger->high_watermark() == 0);
    check_empty_quarantine(*fixture.coordinator);

    const auto applied = admit(fixture, 1, 7, first);
    CHECK(applied.status == ProposalLifecycleApplyStatus::applied);
    CHECK(applied.index_changed);
    CHECK(fixture.index.classify(first) ==
          ProposalEvidenceStatus::admissible);

    const auto replay = fixture.coordinator->apply_notice(
        AuthenticatedReporter{1},
        notice(
            1,
            7,
            NormalProposalRuntimeInitialized{second}));
    CHECK(replay.status ==
          ProposalLifecycleApplyStatus::rejected_sequence);
    CHECK(fixture.index.classify(second) ==
          ProposalEvidenceStatus::unknown);

    const auto regression = fixture.coordinator->apply_notice(
        AuthenticatedReporter{1},
        notice(1, 6, ProposalCommitted{second}));
    CHECK(regression.status ==
          ProposalLifecycleApplyStatus::rejected_sequence);
    CHECK(fixture.index.classify(second) ==
          ProposalEvidenceStatus::unknown);
    CHECK(fixture.ledger->high_watermark() == 0);
    check_empty_quarantine(*fixture.coordinator);

    INFO("observation sequence one remains valid after lifecycle sequence seven");
    const auto evidence = fixture.coordinator->ingest_observation(
        AuthenticatedReporter{1},
        observation(first, 1, 3, 1));
    CHECK(evidence.disposition ==
          EvidenceObservationDisposition::ingested);
    CHECK(evidence.accepted_observations == 1);
    CHECK(evidence.rejected_observations == 0);
    REQUIRE(fixture.ledger->accepted().size() == 1);
    CHECK(fixture.ledger->accepted().front().observation.reporter_sequence ==
          1);

    const auto stats = fixture.coordinator->stats();
    CHECK(stats.applied_lifecycle_notices == 1);
    CHECK(stats.lifecycle_sources == 1);
    CHECK(stats.healthy);
}

TEST_CASE("unsupported lifecycle schema does not consume source sequence",
          "[e08][rem-e08][evidence-lifecycle][schema]"
          "[intentional-red]")
{
    Fixture fixture;
    const auto key = fixture.key("lifecycle-schema");
    auto unsupported = notice(
        1, 1, NormalProposalRuntimeInitialized{key});
    unsupported.schema_version =
        hotstuff::kProposalLifecycleNoticeSchemaVersion + 1;

    const auto rejected = fixture.coordinator->apply_notice(
        AuthenticatedReporter{1}, unsupported);
    CHECK(rejected.status ==
          ProposalLifecycleApplyStatus::rejected_schema);
    CHECK_FALSE(rejected.index_changed);
    CHECK(rejected.retried_observations == 0);
    CHECK(rejected.accepted_observations == 0);
    CHECK(rejected.rejected_observations == 0);
    CHECK(rejected.remaining_quarantined == 0);
    CHECK(fixture.index.classify(key) ==
          ProposalEvidenceStatus::unknown);
    CHECK(fixture.ledger->high_watermark() == 0);
    check_empty_quarantine(*fixture.coordinator);
    const auto after_rejection = fixture.coordinator->stats();
    CHECK(after_rejection.lifecycle_sources == 0);
    CHECK(after_rejection.applied_lifecycle_notices == 0);
    CHECK(after_rejection.healthy);

    INFO("the same source sequence applies once the schema is supported");
    unsupported.schema_version =
        hotstuff::kProposalLifecycleNoticeSchemaVersion;
    const auto applied = fixture.coordinator->apply_notice(
        AuthenticatedReporter{1}, unsupported);
    CHECK(applied.status == ProposalLifecycleApplyStatus::applied);
    CHECK(applied.index_changed);
    CHECK(fixture.index.classify(key) ==
          ProposalEvidenceStatus::admissible);
    const auto after_apply = fixture.coordinator->stats();
    CHECK(after_apply.lifecycle_sources == 1);
    CHECK(after_apply.applied_lifecycle_notices == 1);
    CHECK(fixture.ledger->high_watermark() == 0);
}

TEST_CASE("unknown head preserves per-reporter FIFO and ledger audit order",
          "[e08][evidence-lifecycle][quarantine][fifo]"
          "[intentional-red]")
{
    Fixture fixture;
    const auto unknown = fixture.key("fifo-unknown");
    const auto already_admissible = fixture.key("fifo-admissible");
    REQUIRE(admit(fixture, 6, 1, already_admissible).status ==
            ProposalLifecycleApplyStatus::applied);

    const auto reporter_one_unknown =
        observation(unknown, 1, 3, 1);
    const auto reporter_one_later =
        observation(already_admissible, 1, 4, 2);
    const auto reporter_two_independent =
        observation(already_admissible, 2, 5, 1);

    const auto first = fixture.coordinator->ingest_observation(
        AuthenticatedReporter{1}, reporter_one_unknown);
    CHECK(first.disposition ==
          EvidenceObservationDisposition::quarantined_unknown);
    CHECK(first.quarantined_observations == 1);
    CHECK(fixture.ledger->high_watermark() == 0);
    CHECK(fixture.ledger->accepted().empty());
    CHECK(fixture.ledger->rejected().empty());

    const auto blocked = fixture.coordinator->ingest_observation(
        AuthenticatedReporter{1}, reporter_one_later);
    CHECK(blocked.disposition ==
          EvidenceObservationDisposition::quarantined_behind_unknown);
    CHECK(blocked.quarantined_observations == 2);
    CHECK(fixture.ledger->high_watermark() == 0);

    const auto independent = fixture.coordinator->ingest_observation(
        AuthenticatedReporter{2}, reporter_two_independent);
    CHECK(independent.disposition ==
          EvidenceObservationDisposition::ingested);
    CHECK(independent.accepted_observations == 1);
    CHECK(fixture.ledger->high_watermark() == 1);

    const auto duplicate = fixture.coordinator->ingest_observation(
        AuthenticatedReporter{1}, reporter_one_unknown);
    CHECK(duplicate.disposition ==
          EvidenceObservationDisposition::duplicate_quarantined);
    CHECK(duplicate.quarantined_observations == 2);
    const auto queued = fixture.coordinator->stats();
    CHECK(queued.quarantined_records == 2);
    CHECK(queued.reporter_queues == 1);
    CHECK(queued.deduplication_entries == 2);
    CHECK(queued.duplicate_observations == 1);
    CHECK(fixture.ledger->high_watermark() == 1);

    const auto opened = admit(fixture, 6, 2, unknown);
    CHECK(opened.status == ProposalLifecycleApplyStatus::applied);
    CHECK(opened.retried_observations == 2);
    CHECK(opened.accepted_observations == 2);
    CHECK(opened.rejected_observations == 0);
    CHECK(opened.remaining_quarantined == 0);
    CHECK(fixture.ledger->high_watermark() == 3);
    REQUIRE(fixture.ledger->accepted().size() == 3);

    INFO("other reporters proceed independently, then reporter FIFO drains");
    CHECK(fixture.ledger->accepted()[0].observation.reporter_id == 2);
    CHECK(fixture.ledger->accepted()[1].observation.block_hash ==
          unknown.block_hash);
    CHECK(fixture.ledger->accepted()[2].observation.block_hash ==
          already_admissible.block_hash);
    CHECK(fixture.ledger->accepted()[1].observation.reporter_sequence == 1);
    CHECK(fixture.ledger->accepted()[2].observation.reporter_sequence == 2);
    check_empty_quarantine(*fixture.coordinator);
}

TEST_CASE("payload reporter mismatch bypasses quarantine for ledger rejection",
          "[e08][evidence-lifecycle][authentication][quarantine]"
          "[intentional-red]")
{
    Fixture fixture;
    const auto unknown = fixture.key("reporter-mismatch-unknown");
    const auto forged = observation(unknown, 2, 5, 1);

    const auto result = fixture.coordinator->ingest_observation(
        AuthenticatedReporter{1}, forged);
    CHECK(result.disposition ==
          EvidenceObservationDisposition::ingested);
    CHECK(result.accepted_observations == 0);
    CHECK(result.rejected_observations == 1);
    CHECK(result.quarantined_observations == 0);

    INFO("the real ledger audits authentication before proposal status");
    CHECK(fixture.ledger->high_watermark() == 1);
    CHECK(fixture.ledger->accepted().empty());
    REQUIRE(fixture.ledger->rejected().size() == 1);
    CHECK(fixture.ledger->rejected().front().reason ==
          EvidenceRejectionReason::reporter_mismatch);
    check_empty_quarantine(*fixture.coordinator);
    const auto stats = fixture.coordinator->stats();
    CHECK(stats.duplicate_observations == 0);
    CHECK(stats.capacity_failures == 0);
}

TEST_CASE("proposal-independent malformed facts bypass an unknown FIFO head",
          "[e08][rem-e08][evidence-lifecycle][malformed]"
          "[intentional-red]")
{
    Fixture fixture;
    const auto unknown_head = fixture.key("malformed-unknown-head");
    const auto known = fixture.key("malformed-known");
    REQUIRE(admit(fixture, 6, 1, known).status ==
            ProposalLifecycleApplyStatus::applied);
    REQUIRE(fixture.coordinator->ingest_observation(
                AuthenticatedReporter{1},
                observation(unknown_head, 1, 3, 1))
                .disposition ==
            EvidenceObservationDisposition::quarantined_unknown);
    const auto retained = fixture.coordinator->stats();
    const auto retained_index = fixture.index.stats();
    REQUIRE(retained.quarantined_records == 1);
    REQUIRE(fixture.ledger->high_watermark() == 0);

    std::vector<std::pair<ResponseObservation, EvidenceRejectionReason>>
        malformed;

    auto unsupported_schema = observation(
        fixture.key("malformed-schema-unknown"), 1, 3, 2);
    unsupported_schema.schema_version =
        hotstuff::kResponseObservationSchemaVersion + 1;
    malformed.emplace_back(
        unsupported_schema,
        EvidenceRejectionReason::unsupported_schema);

    auto wrong_id = observation(
        fixture.key("malformed-id-unknown"), 1, 3, 2);
    wrong_id.observation_id = digest("not-the-attempt-id");
    malformed.emplace_back(
        wrong_id,
        EvidenceRejectionReason::observation_id_mismatch);

    auto invalid_message = observation(
        fixture.key("malformed-message-unknown"), 1, 3, 2);
    invalid_message.expected_message_type =
        static_cast<ExpectedMessageType>(0xff);
    invalid_message.observation_id =
        hotstuff::compute_response_observation_id(
            invalid_message.attempt_identity());
    malformed.emplace_back(
        invalid_message,
        EvidenceRejectionReason::invalid_expected_message_type);

    auto invalid_outcome = observation(
        fixture.key("malformed-outcome-unknown"), 1, 3, 2);
    invalid_outcome.outcome = static_cast<ResponseOutcome>(0xff);
    malformed.emplace_back(
        invalid_outcome,
        EvidenceRejectionReason::invalid_outcome);

    auto invalid_timing = observation(
        fixture.key("malformed-timing-unknown"), 1, 3, 2);
    invalid_timing.deadline_duration_us = 0;
    malformed.emplace_back(
        invalid_timing,
        EvidenceRejectionReason::invalid_timing);

    auto invalid_signers = observation(
        fixture.key("malformed-signers-unknown"), 1, 3, 2);
    invalid_signers.signer_set = {3, 3};
    malformed.emplace_back(
        invalid_signers,
        EvidenceRejectionReason::invalid_signer_set);

    auto unknown_configuration = observation(known, 1, 3, 2);
    unknown_configuration.configuration = {
        99, kTreeId, digest("unknown-epoch")};
    unknown_configuration.block_hash = digest("unknown-config-block");
    unknown_configuration.observation_id =
        hotstuff::compute_response_observation_id(
            unknown_configuration.attempt_identity());
    malformed.emplace_back(
        unknown_configuration,
        EvidenceRejectionReason::unknown_configuration);

    for (std::size_t index = 0; index < malformed.size(); ++index)
    {
        CAPTURE(index);
        CHECK(fixture.index.classify(
                  malformed[index].first.proposal_key()) ==
              ProposalEvidenceStatus::unknown);
        const auto result = fixture.coordinator->ingest_observation(
            AuthenticatedReporter{1}, malformed[index].first);
        CHECK(result.disposition ==
              EvidenceObservationDisposition::ingested);
        CHECK(result.accepted_observations == 0);
        CHECK(result.rejected_observations == 1);
        CHECK(result.quarantined_observations == 1);
        CHECK(same_retained_state(
            fixture.coordinator->stats(), retained));
        CHECK(same_index_state(
            fixture.index.stats(), retained_index));
        CHECK(fixture.coordinator->healthy());
        CHECK(fixture.index.classify(unknown_head) ==
              ProposalEvidenceStatus::unknown);
        CHECK(fixture.index.classify(
                  malformed[index].first.proposal_key()) ==
              ProposalEvidenceStatus::unknown);
        REQUIRE(fixture.ledger->rejected().size() == index + 1);
        CHECK(fixture.ledger->rejected().back().reason ==
              malformed[index].second);
        CHECK(fixture.ledger->high_watermark() == index + 1);
    }

    INFO("rejected malformed facts do not consume reporter sequence two");
    const auto later_valid = fixture.coordinator->ingest_observation(
        AuthenticatedReporter{1}, observation(known, 1, 4, 2));
    CHECK(later_valid.disposition ==
          EvidenceObservationDisposition::quarantined_behind_unknown);
    CHECK(later_valid.quarantined_observations == 2);
    CHECK(fixture.ledger->high_watermark() == malformed.size());

    const auto opened = admit(fixture, 6, 2, unknown_head);
    CHECK(opened.retried_observations == 2);
    CHECK(opened.accepted_observations == 2);
    CHECK(opened.rejected_observations == 0);
    CHECK(opened.remaining_quarantined == 0);
    CHECK(fixture.ledger->high_watermark() == malformed.size() + 2);
    REQUIRE(fixture.ledger->accepted().size() == 2);
    CHECK(fixture.ledger->accepted()[0].observation.reporter_sequence == 1);
    CHECK(fixture.ledger->accepted()[1].observation.reporter_sequence == 2);
    check_empty_quarantine(*fixture.coordinator);
}

TEST_CASE("lifecycle retry visits reporter queues in replica-id order",
          "[e08][evidence-lifecycle][quarantine][deterministic]"
          "[intentional-red]")
{
    Fixture fixture;
    const auto shared = fixture.key("deterministic-retry");
    const auto reporter_two = observation(shared, 2, 5, 1);
    const auto reporter_one = observation(shared, 1, 3, 1);

    INFO("arrival order is deliberately the reverse of manager retry order");
    REQUIRE(fixture.coordinator->ingest_observation(
                AuthenticatedReporter{2}, reporter_two)
                .disposition ==
            EvidenceObservationDisposition::quarantined_unknown);
    REQUIRE(fixture.coordinator->ingest_observation(
                AuthenticatedReporter{1}, reporter_one)
                .disposition ==
            EvidenceObservationDisposition::quarantined_unknown);
    CHECK(fixture.ledger->high_watermark() == 0);
    CHECK(fixture.coordinator->stats().reporter_queues == 2);

    const auto opened = admit(fixture, 6, 1, shared);
    CHECK(opened.retried_observations == 2);
    CHECK(opened.accepted_observations == 2);
    CHECK(opened.rejected_observations == 0);
    CHECK(opened.remaining_quarantined == 0);
    CHECK(fixture.ledger->high_watermark() == 2);
    REQUIRE(fixture.ledger->accepted().size() == 2);
    CHECK(fixture.ledger->accepted()[0].observation.reporter_id == 1);
    CHECK(fixture.ledger->accepted()[0].ingestion_sequence == 1);
    CHECK(fixture.ledger->accepted()[1].observation.reporter_id == 2);
    CHECK(fixture.ledger->accepted()[1].ingestion_sequence == 2);
    check_empty_quarantine(*fixture.coordinator);
}

TEST_CASE("timeout and late are distinct queued facts with one observation id",
          "[e08][evidence-lifecycle][quarantine][late]"
          "[intentional-red]")
{
    Fixture fixture;
    const auto key = fixture.key("timeout-late");
    const auto timeout = observation(
        key, 1, 3, 1, ResponseOutcome::timeout);
    const auto late = observation(
        key, 1, 3, 2, ResponseOutcome::late);
    REQUIRE(timeout.observation_id == late.observation_id);

    CHECK(fixture.coordinator->ingest_observation(
              AuthenticatedReporter{1}, timeout)
              .disposition ==
          EvidenceObservationDisposition::quarantined_unknown);
    CHECK(fixture.coordinator->ingest_observation(
              AuthenticatedReporter{1}, timeout)
              .disposition ==
          EvidenceObservationDisposition::duplicate_quarantined);
    CHECK(fixture.coordinator->ingest_observation(
              AuthenticatedReporter{1}, late)
              .disposition ==
          EvidenceObservationDisposition::quarantined_behind_unknown);

    const auto before_duplicate_late = fixture.coordinator->stats();
    const auto accounting_before_duplicate =
        fixture.coordinator->accounting().stats();
    const auto accounting_limits_before_duplicate =
        fixture.coordinator->accounting().limits();
    const auto quarantine_before_duplicate =
        fixture.coordinator->quarantined_observations();
    check_quarantine_snapshot(
        quarantine_before_duplicate,
        {{AuthenticatedReporter{1}, timeout, true},
         {AuthenticatedReporter{1}, late, false}});
    const auto index_before_duplicate = fixture.index.stats();
    const auto classification_before_duplicate =
        fixture.index.classify(key);
    const auto ledger_high_watermark_before_duplicate =
        fixture.ledger->high_watermark();
    const auto ledger_accepted_before_duplicate =
        fixture.ledger->accepted().size();
    const auto ledger_rejected_before_duplicate =
        fixture.ledger->rejected().size();
    const auto ledger_health_before_duplicate = fixture.ledger->healthy();
    CHECK(fixture.coordinator->ingest_observation(
              AuthenticatedReporter{1}, late)
              .disposition ==
          EvidenceObservationDisposition::duplicate_quarantined);
    const auto after_duplicate_late = fixture.coordinator->stats();
    auto expected_after_duplicate = before_duplicate_late;
    ++expected_after_duplicate.duplicate_observations;
    CHECK(same_lifecycle_state(
        after_duplicate_late, expected_after_duplicate));
    CHECK(same_accounting_state(
        fixture.coordinator->accounting().stats(),
        accounting_before_duplicate));
    CHECK(same_accounting_limits(
        fixture.coordinator->accounting().limits(),
        accounting_limits_before_duplicate));
    check_quarantine_snapshot(
        fixture.coordinator->quarantined_observations(),
        quarantine_before_duplicate);
    CHECK(same_index_state(
        fixture.index.stats(), index_before_duplicate));
    CHECK(fixture.index.classify(key) ==
          classification_before_duplicate);
    CHECK(fixture.ledger->high_watermark() ==
          ledger_high_watermark_before_duplicate);
    CHECK(fixture.ledger->accepted().size() ==
          ledger_accepted_before_duplicate);
    CHECK(fixture.ledger->rejected().size() ==
          ledger_rejected_before_duplicate);
    CHECK(fixture.ledger->healthy() == ledger_health_before_duplicate);

    const auto before = fixture.coordinator->stats();
    CHECK(before.quarantined_records == 2);
    CHECK(before.deduplication_entries == 2);
    CHECK(before.duplicate_observations == 2);
    CHECK(fixture.ledger->high_watermark() == 0);

    const auto opened = admit(fixture, 1, 1, key);
    CHECK(opened.retried_observations == 2);
    CHECK(opened.accepted_observations == 2);
    CHECK(opened.rejected_observations == 0);
    REQUIRE(fixture.ledger->accepted().size() == 2);
    CHECK(fixture.ledger->accepted()[0].observation.outcome ==
          ResponseOutcome::timeout);
    CHECK(fixture.ledger->accepted()[1].observation.outcome ==
          ResponseOutcome::late);
    CHECK(fixture.ledger->high_watermark() == 2);
    CHECK(fixture.ledger->rejected().empty());
    check_empty_quarantine(*fixture.coordinator);
}

TEST_CASE("lifecycle facts preserve exact admission and irreversible staleness",
          "[e08][evidence-lifecycle][proposal-index][exact]"
          "[intentional-red]")
{
    SECTION("abort before admission creates no tombstone")
    {
        Fixture fixture;
        const auto key = fixture.key("abort-before-admit");
        const auto result = fixture.coordinator->apply_notice(
            AuthenticatedReporter{1},
            notice(1, 1, ProposalRuntimeAborted{key}));
        CHECK(result.status == ProposalLifecycleApplyStatus::applied);
        CHECK_FALSE(result.index_changed);
        CHECK(fixture.index.classify(key) ==
              ProposalEvidenceStatus::unknown);

        REQUIRE(admit(fixture, 1, 2, key).index_changed);
        CHECK(fixture.index.classify(key) ==
              ProposalEvidenceStatus::admissible);
    }

    SECTION("abort after normal runtime admission is stale and cannot reopen")
    {
        Fixture fixture;
        const auto key = fixture.key("abort-after-admit");
        REQUIRE(admit(fixture, 1, 1, key).index_changed);
        const auto aborted = fixture.coordinator->apply_notice(
            AuthenticatedReporter{1},
            notice(1, 2, ProposalRuntimeAborted{key}));
        CHECK(aborted.index_changed);
        CHECK(fixture.index.classify(key) ==
              ProposalEvidenceStatus::stale);
        CHECK_FALSE(admit(fixture, 1, 3, key).index_changed);
        CHECK(fixture.index.classify(key) ==
              ProposalEvidenceStatus::stale);
    }

    SECTION("commit before admission establishes an exact stale tombstone")
    {
        Fixture fixture;
        const auto key = fixture.key("commit-before-admit");
        const auto committed = fixture.coordinator->apply_notice(
            AuthenticatedReporter{2},
            notice(2, 1, ProposalCommitted{key}));
        CHECK(committed.status == ProposalLifecycleApplyStatus::applied);
        CHECK(committed.index_changed);
        CHECK(fixture.index.classify(key) ==
              ProposalEvidenceStatus::stale);
        CHECK_FALSE(admit(fixture, 2, 2, key).index_changed);
    }

    SECTION("configuration and monotonic floor retire unseen exact keys")
    {
        Fixture fixture;
        const auto unseen = fixture.key("retired-unseen");
        const auto retired = fixture.coordinator->apply_notice(
            AuthenticatedReporter{3},
            notice(
                3,
                1,
                ProposalConfigurationRetired{
                    fixture.configuration}));
        CHECK(retired.status == ProposalLifecycleApplyStatus::applied);
        CHECK(retired.index_changed);
        CHECK(fixture.index.classify(unseen) ==
              ProposalEvidenceStatus::stale);

        const ConfigurationId epoch_four{
            4, kTreeId, digest("epoch-four")};
        const ConfigurationId epoch_five{
            5, kTreeId, digest("epoch-five")};
        const ProposalKey old{epoch_four, digest("old")};
        const ProposalKey live{epoch_five, digest("live")};
        const auto floor = fixture.coordinator->apply_notice(
            AuthenticatedReporter{3},
            notice(
                3,
                2,
                ProposalRetirementFloorAdvanced{5}));
        CHECK(floor.index_changed);
        CHECK(fixture.index.classify(old) ==
              ProposalEvidenceStatus::stale);
        CHECK(fixture.index.classify(live) ==
              ProposalEvidenceStatus::unknown);

        const auto regression = fixture.coordinator->apply_notice(
            AuthenticatedReporter{3},
            notice(
                3,
                3,
                ProposalRetirementFloorAdvanced{4}));
        CHECK_FALSE(regression.index_changed);
        CHECK(fixture.index.classify(live) ==
              ProposalEvidenceStatus::unknown);
    }
}

TEST_CASE("stale quarantine head is audited only by the real ledger",
          "[e08][evidence-lifecycle][quarantine][stale]"
          "[intentional-red]")
{
    Fixture fixture;
    const auto key = fixture.key("stale-head");
    const auto pending = observation(key, 1, 3, 1);

    REQUIRE(fixture.coordinator->ingest_observation(
                AuthenticatedReporter{1}, pending)
                .disposition ==
            EvidenceObservationDisposition::quarantined_unknown);
    CHECK(fixture.ledger->high_watermark() == 0);
    CHECK(fixture.ledger->rejected().empty());

    const auto committed = fixture.coordinator->apply_notice(
        AuthenticatedReporter{1},
        notice(1, 1, ProposalCommitted{key}));
    CHECK(committed.index_changed);
    CHECK(committed.retried_observations == 1);
    CHECK(committed.accepted_observations == 0);
    CHECK(committed.rejected_observations == 1);
    CHECK(committed.remaining_quarantined == 0);

    CHECK(fixture.ledger->high_watermark() == 1);
    CHECK(fixture.ledger->accepted().empty());
    REQUIRE(fixture.ledger->rejected().size() == 1);
    CHECK(fixture.ledger->rejected().front().reason ==
          EvidenceRejectionReason::stale_block);
    CHECK(fixture.ledger->rejected().front().observation.has_value());
    CHECK(fixture.ledger->rejected().front().observation->proposal_key() ==
          key);
    check_empty_quarantine(*fixture.coordinator);
}

TEST_CASE("exact configuration retirement drains unknown queues as stale",
          "[e08][rem-e08][evidence-lifecycle][retirement]"
          "[intentional-red]")
{
    Fixture fixture;
    const auto pending = fixture.key("retire-exact-pending");
    REQUIRE(fixture.coordinator->ingest_observation(
                AuthenticatedReporter{2},
                observation(pending, 2, 5, 1))
                .disposition ==
            EvidenceObservationDisposition::quarantined_unknown);
    REQUIRE(fixture.coordinator->ingest_observation(
                AuthenticatedReporter{1},
                observation(pending, 1, 3, 1))
                .disposition ==
            EvidenceObservationDisposition::quarantined_unknown);
    CHECK(fixture.ledger->high_watermark() == 0);
    REQUIRE(fixture.coordinator->stats().quarantined_records == 2);

    const auto retired = fixture.coordinator->apply_notice(
        AuthenticatedReporter{6},
        notice(
            6,
            1,
            ProposalConfigurationRetired{fixture.configuration}));
    CHECK(retired.status == ProposalLifecycleApplyStatus::applied);
    CHECK(retired.index_changed);
    CHECK(retired.retried_observations == 2);
    CHECK(retired.accepted_observations == 0);
    CHECK(retired.rejected_observations == 2);
    CHECK(retired.remaining_quarantined == 0);
    CHECK(fixture.index.classify(pending) ==
          ProposalEvidenceStatus::stale);
    check_empty_quarantine(*fixture.coordinator);

    REQUIRE(fixture.ledger->rejected().size() == 2);
    CHECK(fixture.ledger->high_watermark() == 2);
    CHECK(fixture.ledger->rejected()[0].ingestion_sequence == 1);
    CHECK(fixture.ledger->rejected()[0].authenticated_reporter.replica_id ==
          1);
    CHECK(fixture.ledger->rejected()[0].reason ==
          EvidenceRejectionReason::stale_block);
    CHECK(fixture.ledger->rejected()[1].ingestion_sequence == 2);
    CHECK(fixture.ledger->rejected()[1].authenticated_reporter.replica_id ==
          2);
    CHECK(fixture.ledger->rejected()[1].reason ==
          EvidenceRejectionReason::stale_block);
}

TEST_CASE("monotonic retirement floor drains below-floor queues as stale",
          "[e08][rem-e08][evidence-lifecycle][retirement-floor]"
          "[intentional-red]")
{
    Fixture fixture;
    const auto pending = fixture.key("retire-floor-pending");
    REQUIRE(fixture.coordinator->ingest_observation(
                AuthenticatedReporter{2},
                observation(pending, 2, 5, 1))
                .disposition ==
            EvidenceObservationDisposition::quarantined_unknown);
    REQUIRE(fixture.coordinator->ingest_observation(
                AuthenticatedReporter{1},
                observation(pending, 1, 3, 1))
                .disposition ==
            EvidenceObservationDisposition::quarantined_unknown);
    CHECK(fixture.ledger->high_watermark() == 0);

    const auto retired = fixture.coordinator->apply_notice(
        AuthenticatedReporter{6},
        notice(
            6,
            1,
            ProposalRetirementFloorAdvanced{1}));
    CHECK(retired.status == ProposalLifecycleApplyStatus::applied);
    CHECK(retired.index_changed);
    CHECK(retired.retried_observations == 2);
    CHECK(retired.accepted_observations == 0);
    CHECK(retired.rejected_observations == 2);
    CHECK(retired.remaining_quarantined == 0);
    CHECK(fixture.index.classify(pending) ==
          ProposalEvidenceStatus::stale);
    check_empty_quarantine(*fixture.coordinator);

    REQUIRE(fixture.ledger->rejected().size() == 2);
    CHECK(fixture.ledger->high_watermark() == 2);
    CHECK(fixture.ledger->rejected()[0].ingestion_sequence == 1);
    CHECK(fixture.ledger->rejected()[0].authenticated_reporter.replica_id ==
          1);
    CHECK(fixture.ledger->rejected()[0].reason ==
          EvidenceRejectionReason::stale_block);
    CHECK(fixture.ledger->rejected()[1].ingestion_sequence == 2);
    CHECK(fixture.ledger->rejected()[1].authenticated_reporter.replica_id ==
          2);
    CHECK(fixture.ledger->rejected()[1].reason ==
          EvidenceRejectionReason::stale_block);
}

TEST_CASE("every retained quarantine dimension is bounded fail closed",
          "[e08][evidence-lifecycle][bounds][intentional-red]")
{
    SECTION("zero record limit")
    {
        auto limits = lifecycle_limits();
        limits.maximum_quarantined_records = 0;
        check_zero_lifecycle_limit(limits);
    }

    SECTION("zero byte limit")
    {
        auto limits = lifecycle_limits();
        limits.maximum_quarantined_bytes = 0;
        check_zero_lifecycle_limit(limits);
    }

    SECTION("zero reporter queue limit")
    {
        auto limits = lifecycle_limits();
        limits.maximum_reporter_queues = 0;
        check_zero_lifecycle_limit(limits);
    }

    SECTION("zero signer-entry limit")
    {
        auto limits = lifecycle_limits();
        limits.maximum_signer_entries = 0;
        check_zero_lifecycle_limit(limits);
    }

    SECTION("zero deduplication limit")
    {
        auto limits = lifecycle_limits();
        limits.maximum_deduplication_entries = 0;
        check_zero_lifecycle_limit(limits);
    }

    SECTION("zero lifecycle-source limit")
    {
        auto limits = lifecycle_limits();
        limits.maximum_lifecycle_sources = 0;
        check_zero_lifecycle_limit(limits);
    }

    SECTION("record capacity is sticky and later lifecycle is a no-op")
    {
        auto limits = lifecycle_limits();
        limits.maximum_quarantined_records = 1;
        Fixture fixture(limits);
        const auto first = fixture.key("record-one");
        const auto second = fixture.key("record-two");
        REQUIRE(fixture.coordinator->ingest_observation(
                    AuthenticatedReporter{1},
                    observation(first, 1, 3, 1))
                    .disposition ==
                EvidenceObservationDisposition::quarantined_unknown);
        const auto overflow = fixture.coordinator->ingest_observation(
            AuthenticatedReporter{1},
            observation(second, 1, 4, 2));
        CHECK(overflow.disposition ==
              EvidenceObservationDisposition::evidence_unhealthy);
        CHECK_FALSE(fixture.coordinator->healthy());
        CHECK(fixture.coordinator->stats().capacity_failures == 1);
        CHECK(fixture.ledger->high_watermark() == 0);

        const auto ignored = admit(fixture, 1, 1, first);
        CHECK(ignored.status ==
              ProposalLifecycleApplyStatus::evidence_unhealthy);
        CHECK(fixture.index.classify(first) ==
              ProposalEvidenceStatus::unknown);
        CHECK(fixture.ledger->high_watermark() == 0);
    }

    SECTION("canonical byte estimate is bounded")
    {
        auto limits = lifecycle_limits();
        limits.maximum_quarantined_bytes = 1;
        Fixture fixture(limits);
        const auto result = fixture.coordinator->ingest_observation(
            AuthenticatedReporter{1},
            observation(fixture.key("bytes"), 1, 3, 1));
        CHECK(result.disposition ==
              EvidenceObservationDisposition::evidence_unhealthy);
        CHECK_FALSE(fixture.coordinator->healthy());
        CHECK(fixture.ledger->high_watermark() == 0);
    }

    SECTION("reporter queues are bounded independently")
    {
        auto limits = lifecycle_limits();
        limits.maximum_reporter_queues = 1;
        Fixture fixture(limits);
        REQUIRE(fixture.coordinator->ingest_observation(
                    AuthenticatedReporter{1},
                    observation(fixture.key("reporter-one"), 1, 3, 1))
                    .disposition ==
                EvidenceObservationDisposition::quarantined_unknown);
        const auto overflow = fixture.coordinator->ingest_observation(
            AuthenticatedReporter{2},
            observation(fixture.key("reporter-two"), 2, 5, 1));
        CHECK(overflow.disposition ==
              EvidenceObservationDisposition::evidence_unhealthy);
        CHECK_FALSE(fixture.coordinator->healthy());
        CHECK(fixture.ledger->high_watermark() == 0);
    }

    SECTION("total signer storage is bounded before ledger admission")
    {
        auto limits = lifecycle_limits();
        limits.maximum_signer_entries = 1;
        Fixture fixture(limits);
        auto oversized = observation(
            fixture.key("signers"), 1, 3, 1);
        oversized.signer_set = {3, 4};
        const auto result = fixture.coordinator->ingest_observation(
            AuthenticatedReporter{1}, oversized);
        CHECK(result.disposition ==
              EvidenceObservationDisposition::evidence_unhealthy);
        CHECK_FALSE(fixture.coordinator->healthy());
        CHECK(fixture.ledger->high_watermark() == 0);
    }

    SECTION("dedup keys are bounded while exact duplicates cost nothing")
    {
        auto limits = lifecycle_limits();
        limits.maximum_deduplication_entries = 1;
        Fixture fixture(limits);
        const auto key = fixture.key("dedup");
        const auto timeout = observation(
            key, 1, 3, 1, ResponseOutcome::timeout);
        const auto late = observation(
            key, 1, 3, 2, ResponseOutcome::late);
        REQUIRE(fixture.coordinator->ingest_observation(
                    AuthenticatedReporter{1}, timeout)
                    .disposition ==
                EvidenceObservationDisposition::quarantined_unknown);
        CHECK(fixture.coordinator->ingest_observation(
                  AuthenticatedReporter{1}, timeout)
                  .disposition ==
              EvidenceObservationDisposition::duplicate_quarantined);
        CHECK(fixture.coordinator->stats().quarantined_records == 1);
        CHECK(fixture.coordinator->stats().deduplication_entries == 1);

        const auto overflow = fixture.coordinator->ingest_observation(
            AuthenticatedReporter{1}, late);
        CHECK(overflow.disposition ==
              EvidenceObservationDisposition::evidence_unhealthy);
        CHECK_FALSE(fixture.coordinator->healthy());
        CHECK(fixture.ledger->high_watermark() == 0);
    }

    SECTION("lifecycle source sequence state is bounded")
    {
        auto limits = lifecycle_limits();
        limits.maximum_lifecycle_sources = 1;
        Fixture fixture(limits);
        const auto first = fixture.key("source-one");
        const auto second = fixture.key("source-two");
        REQUIRE(admit(fixture, 1, 1, first).status ==
                ProposalLifecycleApplyStatus::applied);
        const auto overflow = admit(fixture, 2, 1, second);
        CHECK(overflow.status ==
              ProposalLifecycleApplyStatus::evidence_unhealthy);
        CHECK_FALSE(fixture.coordinator->healthy());
        CHECK(fixture.index.classify(first) ==
              ProposalEvidenceStatus::admissible);
        CHECK(fixture.index.classify(second) ==
              ProposalEvidenceStatus::unknown);
        CHECK(fixture.ledger->high_watermark() == 0);
    }
}

TEST_CASE("coordinator-owned accounting is checked and authoritative",
          "[e08][rem-e08][evidence-lifecycle][bounds][overflow]"
          "[intentional-red]")
{
    constexpr auto maximum = std::numeric_limits<std::size_t>::max();

    SECTION("ordinary retain and release update all counters atomically")
    {
        EvidenceLifecycleAccounting accounting({3, 100, 10});
        CHECK(same_accounting_state(
            accounting.stats(), EvidenceLifecycleAccountingStats{}));

        REQUIRE(accounting.try_retain({40, 2}));
        CHECK(same_accounting_state(
            accounting.stats(), {1, 40, 2}));

        const auto retained = accounting.stats();
        CHECK_FALSE(accounting.release({41, 2}));
        CHECK(same_accounting_state(accounting.stats(), retained));
        CHECK_FALSE(accounting.release({40, 3}));
        CHECK(same_accounting_state(accounting.stats(), retained));
        CHECK_FALSE(accounting.release({39, 1}));
        CHECK(same_accounting_state(accounting.stats(), retained));

        REQUIRE(accounting.release({40, 2}));
        CHECK(same_accounting_state(
            accounting.stats(), EvidenceLifecycleAccountingStats{}));
        CHECK_FALSE(accounting.release({0, 0}));
        CHECK(same_accounting_state(
            accounting.stats(), EvidenceLifecycleAccountingStats{}));
    }

    SECTION("release matches one retained record and its multiplicity")
    {
        EvidenceLifecycleAccounting accounting({4, 200, 10});
        REQUIRE(accounting.try_retain({40, 2}));
        REQUIRE(accounting.try_retain({20, 1}));
        const auto differently_sized = accounting.stats();
        REQUIRE(same_accounting_state(
            differently_sized, {2, 60, 3}));

        CHECK_FALSE(accounting.release({30, 1}));
        CHECK(same_accounting_state(
            accounting.stats(), differently_sized));

        REQUIRE(accounting.release({20, 1}));
        const EvidenceLifecycleAccountingStats one_remaining{1, 40, 2};
        CHECK(same_accounting_state(
            accounting.stats(), one_remaining));
        CHECK_FALSE(accounting.release({20, 1}));
        CHECK(same_accounting_state(
            accounting.stats(), one_remaining));
        REQUIRE(accounting.release({40, 2}));
        CHECK(same_accounting_state(
            accounting.stats(), EvidenceLifecycleAccountingStats{}));

        REQUIRE(accounting.try_retain({15, 1}));
        REQUIRE(accounting.try_retain({15, 1}));
        REQUIRE(accounting.release({15, 1}));
        CHECK(same_accounting_state(
            accounting.stats(), {1, 15, 1}));
        REQUIRE(accounting.release({15, 1}));
        CHECK(same_accounting_state(
            accounting.stats(), EvidenceLifecycleAccountingStats{}));
        CHECK_FALSE(accounting.release({15, 1}));
        CHECK(same_accounting_state(
            accounting.stats(), EvidenceLifecycleAccountingStats{}));
    }

    SECTION("canonical byte overflow is independent and non-mutating")
    {
        EvidenceLifecycleAccounting accounting({3, maximum, maximum});
        REQUIRE(accounting.try_retain({maximum - 1, 1}));
        const auto retained = accounting.stats();

        CHECK_FALSE(accounting.try_retain({2, 0}));
        CHECK(same_accounting_state(accounting.stats(), retained));
        REQUIRE(accounting.try_retain({1, 0}));
        CHECK(same_accounting_state(
            accounting.stats(), {2, maximum, 1}));

        const auto full = accounting.stats();
        CHECK_FALSE(accounting.try_retain({1, 0}));
        CHECK(same_accounting_state(accounting.stats(), full));
    }

    SECTION("signer overflow is independent and non-mutating")
    {
        EvidenceLifecycleAccounting accounting({3, maximum, maximum});
        REQUIRE(accounting.try_retain({1, maximum - 1}));
        const auto retained = accounting.stats();

        CHECK_FALSE(accounting.try_retain({0, 2}));
        CHECK(same_accounting_state(accounting.stats(), retained));
        REQUIRE(accounting.try_retain({0, 1}));
        CHECK(same_accounting_state(
            accounting.stats(), {2, 1, maximum}));

        const auto full = accounting.stats();
        CHECK_FALSE(accounting.try_retain({0, 1}));
        CHECK(same_accounting_state(accounting.stats(), full));
    }

    SECTION("normal coordinator retention and release use owned accounting")
    {
        Fixture fixture;
        const auto key = fixture.key("accounting-normal-path");
        const auto pending = observation(key, 1, 3, 1);
        CHECK(same_accounting_limits(
            fixture.coordinator->accounting().limits(),
            accounting_limits(lifecycle_limits())));

        REQUIRE(fixture.coordinator->ingest_observation(
                    AuthenticatedReporter{1}, pending)
                    .disposition ==
                EvidenceObservationDisposition::quarantined_unknown);
        const auto retained = fixture.coordinator->stats();
        CHECK(same_accounting_state(
            fixture.coordinator->accounting().stats(),
            {retained.quarantined_records,
             retained.quarantined_bytes,
             retained.signer_entries}));
        check_quarantine_snapshot(
            fixture.coordinator->quarantined_observations(),
            {{AuthenticatedReporter{1}, pending, true}});

        const auto accounting_before_duplicate =
            fixture.coordinator->accounting().stats();
        REQUIRE(fixture.coordinator->ingest_observation(
                    AuthenticatedReporter{1}, pending)
                    .disposition ==
                EvidenceObservationDisposition::duplicate_quarantined);
        CHECK(same_accounting_state(
            fixture.coordinator->accounting().stats(),
            accounting_before_duplicate));

        const auto admitted = admit(fixture, 1, 1, key);
        REQUIRE(admitted.status == ProposalLifecycleApplyStatus::applied);
        REQUIRE(admitted.accepted_observations == 1);
        check_empty_quarantine(*fixture.coordinator);
    }

    SECTION("coordinator record limit leaves owned accounting exact")
    {
        auto limits = lifecycle_limits();
        limits.maximum_quarantined_records = 1;
        Fixture fixture(limits);
        const auto first = observation(
            fixture.key("accounting-limit-first"), 1, 3, 1);
        const auto second = observation(
            fixture.key("accounting-limit-second"), 1, 4, 2);
        REQUIRE(fixture.coordinator->ingest_observation(
                    AuthenticatedReporter{1}, first)
                    .disposition ==
                EvidenceObservationDisposition::quarantined_unknown);

        const auto coordinator_before = fixture.coordinator->stats();
        const auto accounting_before =
            fixture.coordinator->accounting().stats();
        const auto quarantine_before =
            fixture.coordinator->quarantined_observations();
        CHECK(same_accounting_state(
            accounting_before,
            {coordinator_before.quarantined_records,
             coordinator_before.quarantined_bytes,
             coordinator_before.signer_entries}));
        check_quarantine_snapshot(
            quarantine_before,
            {{AuthenticatedReporter{1}, first, true}});

        const auto failed = fixture.coordinator->ingest_observation(
            AuthenticatedReporter{1}, second);
        CHECK(failed.disposition ==
              EvidenceObservationDisposition::evidence_unhealthy);
        CHECK_FALSE(fixture.coordinator->healthy());
        CHECK(same_accounting_state(
            fixture.coordinator->accounting().stats(), accounting_before));
        check_quarantine_snapshot(
            fixture.coordinator->quarantined_observations(),
            quarantine_before);
        const auto coordinator_after = fixture.coordinator->stats();
        CHECK(coordinator_after.quarantined_records ==
              accounting_before.retained_records);
        CHECK(coordinator_after.quarantined_bytes ==
              accounting_before.retained_canonical_bytes);
        CHECK(coordinator_after.signer_entries ==
              accounting_before.retained_signer_entries);
        CHECK(coordinator_after.capacity_failures ==
              coordinator_before.capacity_failures + 1);
    }
}

TEST_CASE("coordinator rejects accounting that already owns retention",
          "[e08][rem-e08][evidence-lifecycle][accounting]"
          "[intentional-red]")
{
    const auto limits = lifecycle_limits();
    EpochStore epochs{membership()};
    epochs.stage(epoch_input(), validation_context());
    ProposalEvidenceIndex index{index_limits()};
    EvidenceLedger ledger{epochs, index, store_limits()};

    const auto index_before = index.stats();
    const auto ledger_high_watermark_before = ledger.high_watermark();
    const auto ledger_accepted_before = ledger.accepted().size();
    const auto ledger_rejected_before = ledger.rejected().size();

    EvidenceLifecycleAccounting accounting{accounting_limits(limits)};
    const EvidenceRetentionCost retained_cost{40, 2};
    REQUIRE(accounting.try_retain(retained_cost));
    const auto accounting_limits_before = accounting.limits();
    const auto accounting_before = accounting.stats();

    REQUIRE_THROWS_AS(
        [&]() {
            ProposalLifecycleEvidenceCoordinator rejected(
                index,
                ledger,
                std::move(accounting),
                limits);
        }(),
        std::invalid_argument);

    CHECK(same_index_state(index.stats(), index_before));
    CHECK(index.healthy());
    CHECK(ledger.healthy());
    CHECK(ledger.high_watermark() == ledger_high_watermark_before);
    CHECK(ledger.accepted().size() == ledger_accepted_before);
    CHECK(ledger.rejected().size() == ledger_rejected_before);

    CHECK(same_accounting_limits(
        accounting.limits(), accounting_limits_before));
    CHECK(same_accounting_state(accounting.stats(), accounting_before));
    REQUIRE(accounting.release(retained_cost));
    CHECK(same_accounting_state(
        accounting.stats(), EvidenceLifecycleAccountingStats{}));

    ProposalLifecycleEvidenceCoordinator healthy{
        index,
        ledger,
        std::move(accounting),
        limits};
    REQUIRE(healthy.healthy());
    check_empty_quarantine(healthy);
    CHECK(same_index_state(index.stats(), index_before));
    CHECK(ledger.high_watermark() == ledger_high_watermark_before);
    CHECK(ledger.accepted().size() == ledger_accepted_before);
    CHECK(ledger.rejected().size() == ledger_rejected_before);
}

TEST_CASE("allocation prefix sweeps preserve exact lifecycle boundaries",
          "[e08][rem-e08][evidence-lifecycle][allocation]"
          "[intentional-red]")
{
    constexpr std::size_t allocation_sweep = 48;

    SECTION("new reporter and first queued record")
    {
        std::size_t injected_failures = 0;
        std::size_t sticky_failures = 0;
        std::vector<std::size_t> inconsistent;
        for (std::size_t prefix = 0; prefix < allocation_sweep; ++prefix)
        {
            Fixture fixture;
            const auto pending = observation(
                fixture.key("sweep-new-record"), 1, 3, 1);
            const auto before = fixture.coordinator->stats();
            EvidenceObservationResult result;
            bool injected = false;
            {
                evidence_lifecycle_allocation_failure::OneShot fault(prefix);
                result = fixture.coordinator->ingest_observation(
                    AuthenticatedReporter{1}, pending);
                injected = fault.triggered();
            }
            if (!injected)
                continue;
            ++injected_failures;

            const auto after = fixture.coordinator->stats();
            const bool complete =
                result.disposition ==
                    EvidenceObservationDisposition::quarantined_unknown &&
                fixture.coordinator->healthy() &&
                after.quarantined_records == 1 &&
                after.reporter_queues == 1 &&
                after.deduplication_entries == 1;
            const bool failed_atomically =
                result.disposition ==
                    EvidenceObservationDisposition::evidence_unhealthy &&
                !fixture.coordinator->healthy() &&
                same_retained_state(after, before) &&
                fixture.ledger->high_watermark() == 0 &&
                fixture.ledger->accepted().empty() &&
                fixture.ledger->rejected().empty();
            if (failed_atomically)
            {
                ++sticky_failures;
                CAPTURE(prefix);
                const auto sticky_before = after;
                const auto sticky_index_before = fixture.index.stats();
                const auto sticky_high_watermark =
                    fixture.ledger->high_watermark();
                const auto sticky_accepted =
                    fixture.ledger->accepted().size();
                const auto sticky_rejected =
                    fixture.ledger->rejected().size();
                const auto sticky_ledger_health =
                    fixture.ledger->healthy();

                const auto follow_up =
                    fixture.coordinator->ingest_observation(
                        AuthenticatedReporter{1}, pending);
                CHECK(follow_up.disposition ==
                      EvidenceObservationDisposition::evidence_unhealthy);
                const auto sticky_after = fixture.coordinator->stats();
                CHECK_FALSE(sticky_after.healthy);
                CHECK(same_retained_state(
                    sticky_after, sticky_before));
                CHECK(sticky_after.capacity_failures ==
                      sticky_before.capacity_failures);
                CHECK(same_index_state(
                    fixture.index.stats(), sticky_index_before));
                CHECK(fixture.ledger->high_watermark() ==
                      sticky_high_watermark);
                CHECK(fixture.ledger->accepted().size() ==
                      sticky_accepted);
                CHECK(fixture.ledger->rejected().size() ==
                      sticky_rejected);
                CHECK(fixture.ledger->healthy() ==
                      sticky_ledger_health);
            }
            if (!complete && !failed_atomically)
                inconsistent.push_back(prefix);
        }
        CAPTURE(injected_failures);
        CAPTURE(sticky_failures);
        CAPTURE(inconsistent);
        REQUIRE(injected_failures > 0);
        REQUIRE(sticky_failures > 0);
        CHECK(inconsistent.empty());
    }

    SECTION("append behind an existing reporter head")
    {
        std::size_t injected_failures = 0;
        std::size_t sticky_failures = 0;
        std::vector<std::size_t> inconsistent;
        for (std::size_t prefix = 0; prefix < allocation_sweep; ++prefix)
        {
            Fixture fixture;
            const auto first = fixture.key("sweep-append-first");
            const auto second = fixture.key("sweep-append-second");
            REQUIRE(fixture.coordinator->ingest_observation(
                        AuthenticatedReporter{1},
                        observation(first, 1, 3, 1))
                        .disposition ==
                    EvidenceObservationDisposition::quarantined_unknown);
            const auto before = fixture.coordinator->stats();
            const auto appended = observation(second, 1, 4, 2);
            EvidenceObservationResult result;
            bool injected = false;
            {
                evidence_lifecycle_allocation_failure::OneShot fault(prefix);
                result = fixture.coordinator->ingest_observation(
                    AuthenticatedReporter{1}, appended);
                injected = fault.triggered();
            }
            if (!injected)
                continue;
            ++injected_failures;

            const auto after = fixture.coordinator->stats();
            const bool complete =
                result.disposition ==
                    EvidenceObservationDisposition::
                        quarantined_behind_unknown &&
                fixture.coordinator->healthy() &&
                after.quarantined_records == 2 &&
                after.reporter_queues == 1 &&
                after.deduplication_entries == 2;
            const bool failed_atomically =
                result.disposition ==
                    EvidenceObservationDisposition::evidence_unhealthy &&
                !fixture.coordinator->healthy() &&
                same_retained_state(after, before) &&
                fixture.ledger->high_watermark() == 0 &&
                fixture.ledger->accepted().empty() &&
                fixture.ledger->rejected().empty();
            if (failed_atomically)
            {
                ++sticky_failures;
                CAPTURE(prefix);
                const auto sticky_before = after;
                const auto sticky_index_before = fixture.index.stats();
                const auto sticky_high_watermark =
                    fixture.ledger->high_watermark();
                const auto sticky_accepted =
                    fixture.ledger->accepted().size();
                const auto sticky_rejected =
                    fixture.ledger->rejected().size();
                const auto sticky_ledger_health =
                    fixture.ledger->healthy();

                const auto follow_up =
                    fixture.coordinator->ingest_observation(
                        AuthenticatedReporter{1}, appended);
                CHECK(follow_up.disposition ==
                      EvidenceObservationDisposition::evidence_unhealthy);
                const auto sticky_after = fixture.coordinator->stats();
                CHECK_FALSE(sticky_after.healthy);
                CHECK(same_retained_state(
                    sticky_after, sticky_before));
                CHECK(sticky_after.capacity_failures ==
                      sticky_before.capacity_failures);
                CHECK(same_index_state(
                    fixture.index.stats(), sticky_index_before));
                CHECK(fixture.ledger->high_watermark() ==
                      sticky_high_watermark);
                CHECK(fixture.ledger->accepted().size() ==
                      sticky_accepted);
                CHECK(fixture.ledger->rejected().size() ==
                      sticky_rejected);
                CHECK(fixture.ledger->healthy() ==
                      sticky_ledger_health);
            }
            if (!complete && !failed_atomically)
                inconsistent.push_back(prefix);
        }
        CAPTURE(injected_failures);
        CAPTURE(sticky_failures);
        CAPTURE(inconsistent);
        REQUIRE(injected_failures > 0);
        REQUIRE(sticky_failures > 0);
        CHECK(inconsistent.empty());
    }

    SECTION("new lifecycle source and exact index mutation")
    {
        std::size_t injected_failures = 0;
        std::size_t sticky_failures = 0;
        std::vector<std::size_t> inconsistent;
        for (std::size_t prefix = 0; prefix < allocation_sweep; ++prefix)
        {
            Fixture fixture;
            const auto key = fixture.key("sweep-lifecycle-source");
            const auto initialized = notice(
                1, 1, NormalProposalRuntimeInitialized{key});
            const auto before = fixture.coordinator->stats();
            ProposalLifecycleApplyResult result;
            bool injected = false;
            {
                evidence_lifecycle_allocation_failure::OneShot fault(prefix);
                result = fixture.coordinator->apply_notice(
                    AuthenticatedReporter{1}, initialized);
                injected = fault.triggered();
            }
            if (!injected)
                continue;
            ++injected_failures;

            const auto after = fixture.coordinator->stats();
            const bool complete =
                result.status == ProposalLifecycleApplyStatus::applied &&
                result.index_changed &&
                fixture.coordinator->healthy() &&
                fixture.index.classify(key) ==
                    ProposalEvidenceStatus::admissible &&
                after.lifecycle_sources == 1 &&
                after.applied_lifecycle_notices == 1;
            const bool failed_atomically =
                result.status ==
                    ProposalLifecycleApplyStatus::evidence_unhealthy &&
                !fixture.coordinator->healthy() &&
                same_retained_state(after, before) &&
                fixture.index.classify(key) !=
                    ProposalEvidenceStatus::admissible &&
                fixture.ledger->high_watermark() == 0 &&
                fixture.ledger->accepted().empty() &&
                fixture.ledger->rejected().empty();
            if (failed_atomically)
            {
                ++sticky_failures;
                CAPTURE(prefix);
                const auto sticky_before = after;
                const auto sticky_index_before = fixture.index.stats();
                const auto sticky_high_watermark =
                    fixture.ledger->high_watermark();
                const auto sticky_accepted =
                    fixture.ledger->accepted().size();
                const auto sticky_rejected =
                    fixture.ledger->rejected().size();
                const auto sticky_ledger_health =
                    fixture.ledger->healthy();

                const auto follow_up = fixture.coordinator->apply_notice(
                    AuthenticatedReporter{1}, initialized);
                CHECK(follow_up.status ==
                      ProposalLifecycleApplyStatus::evidence_unhealthy);
                const auto sticky_after = fixture.coordinator->stats();
                CHECK_FALSE(sticky_after.healthy);
                CHECK(same_retained_state(
                    sticky_after, sticky_before));
                CHECK(sticky_after.capacity_failures ==
                      sticky_before.capacity_failures);
                CHECK(same_index_state(
                    fixture.index.stats(), sticky_index_before));
                CHECK(fixture.ledger->high_watermark() ==
                      sticky_high_watermark);
                CHECK(fixture.ledger->accepted().size() ==
                      sticky_accepted);
                CHECK(fixture.ledger->rejected().size() ==
                      sticky_rejected);
                CHECK(fixture.ledger->healthy() ==
                      sticky_ledger_health);
            }
            if (!complete && !failed_atomically)
                inconsistent.push_back(prefix);
        }
        CAPTURE(injected_failures);
        CAPTURE(sticky_failures);
        CAPTURE(inconsistent);
        REQUIRE(injected_failures > 0);
        REQUIRE(sticky_failures > 0);
        CHECK(inconsistent.empty());
    }

    SECTION("retry to the real ledger permits only rollback or a poisoned admission prefix")
    {
        std::size_t injected_failures = 0;
        std::size_t sticky_failures = 0;
        std::vector<std::size_t> inconsistent;
        for (std::size_t prefix = 0; prefix < allocation_sweep; ++prefix)
        {
            Fixture fixture;
            const auto key = fixture.key("sweep-retry-ledger");
            REQUIRE(fixture.coordinator->apply_notice(
                        AuthenticatedReporter{1},
                        notice(1, 1, ProposalRuntimeAborted{key}))
                        .status ==
                    ProposalLifecycleApplyStatus::applied);
            const auto pending = observation(key, 1, 3, 1);
            REQUIRE(fixture.coordinator->ingest_observation(
                        AuthenticatedReporter{1},
                        pending)
                        .disposition ==
                    EvidenceObservationDisposition::quarantined_unknown);
            const auto before = fixture.coordinator->stats();
            const auto quarantine_before =
                fixture.coordinator->quarantined_observations();
            check_quarantine_snapshot(
                quarantine_before,
                {{AuthenticatedReporter{1}, pending, true}});
            const auto accounting_before =
                fixture.coordinator->accounting().stats();
            const auto accounting_limits_before =
                fixture.coordinator->accounting().limits();
            REQUIRE(same_accounting_state(
                accounting_before,
                {before.quarantined_records,
                 before.quarantined_bytes,
                 before.signer_entries}));
            REQUIRE(same_accounting_limits(
                accounting_limits_before,
                accounting_limits(lifecycle_limits())));
            const auto index_before = fixture.index.stats();
            const auto classification_before = fixture.index.classify(key);
            REQUIRE(classification_before ==
                    ProposalEvidenceStatus::unknown);
            const auto initialized = notice(
                1, 2, NormalProposalRuntimeInitialized{key});
            ProposalLifecycleApplyResult result;
            bool injected = false;
            {
                evidence_lifecycle_allocation_failure::OneShot fault(prefix);
                result = fixture.coordinator->apply_notice(
                    AuthenticatedReporter{1}, initialized);
                injected = fault.triggered();
            }
            if (!injected)
                continue;
            ++injected_failures;

            const auto after = fixture.coordinator->stats();
            const auto index_after = fixture.index.stats();
            const auto classification_after = fixture.index.classify(key);
            const auto quarantine_after =
                fixture.coordinator->quarantined_observations();
            const auto accounting_after =
                fixture.coordinator->accounting().stats();
            const auto accounting_limits_after =
                fixture.coordinator->accounting().limits();
            const bool complete =
                result.status == ProposalLifecycleApplyStatus::applied &&
                result.retried_observations == 1 &&
                result.accepted_observations == 1 &&
                result.rejected_observations == 0 &&
                result.remaining_quarantined == 0 &&
                fixture.coordinator->healthy() &&
                after.quarantined_records == 0 &&
                after.reporter_queues == 0 &&
                after.deduplication_entries == 0 &&
                classification_after ==
                    ProposalEvidenceStatus::admissible &&
                fixture.ledger->high_watermark() == 1 &&
                fixture.ledger->accepted().size() == 1 &&
                fixture.ledger->rejected().empty();
            const bool ledger_prefix =
                fixture.ledger->accepted().empty() &&
                fixture.ledger->rejected().empty() &&
                (fixture.ledger->high_watermark() == 0 ||
                 (fixture.ledger->high_watermark() == 1 &&
                  !fixture.ledger->healthy()));
            const bool rolled_back_index =
                same_index_state(index_after, index_before) &&
                classification_after == classification_before;
            const bool irreversible_admission_prefix =
                exact_single_admission_prefix(
                    index_before, index_after) &&
                classification_after ==
                    ProposalEvidenceStatus::unknown &&
                !fixture.ledger->healthy();

            // A failed retry may either leave the exact pre-call index, or
            // retain one exact admission only after fail-closing the index.
            // No third partial mutation is safe: neither outcome may expose a
            // ranking-consumable ledger record or release the queued head.
            const bool safe_failure =
                result.status ==
                    ProposalLifecycleApplyStatus::evidence_unhealthy &&
                !fixture.coordinator->healthy() &&
                !after.healthy &&
                same_retained_state(after, before) &&
                same_quarantine_snapshot(
                    quarantine_after, quarantine_before) &&
                same_accounting_state(
                    accounting_after, accounting_before) &&
                same_accounting_limits(
                    accounting_limits_after,
                    accounting_limits_before) &&
                result.retried_observations == 0 &&
                result.accepted_observations == 0 &&
                result.rejected_observations == 0 &&
                result.remaining_quarantined == 1 &&
                ledger_prefix &&
                (rolled_back_index ||
                 irreversible_admission_prefix);
            if (safe_failure)
            {
                ++sticky_failures;
                CAPTURE(prefix);
                const auto sticky_before = after;
                const auto sticky_quarantine = quarantine_after;
                const auto sticky_accounting = accounting_after;
                const auto sticky_accounting_limits =
                    accounting_limits_after;
                const auto sticky_index_before = fixture.index.stats();
                const auto sticky_classification =
                    fixture.index.classify(key);
                const auto sticky_high_watermark =
                    fixture.ledger->high_watermark();
                const auto sticky_accepted =
                    fixture.ledger->accepted().size();
                const auto sticky_rejected =
                    fixture.ledger->rejected().size();
                const auto sticky_ledger_health =
                    fixture.ledger->healthy();

                const auto check_sticky_state = [&]() {
                    CHECK(same_lifecycle_state(
                        fixture.coordinator->stats(), sticky_before));
                    check_quarantine_snapshot(
                        fixture.coordinator->quarantined_observations(),
                        sticky_quarantine);
                    CHECK(same_accounting_state(
                        fixture.coordinator->accounting().stats(),
                        sticky_accounting));
                    CHECK(same_accounting_limits(
                        fixture.coordinator->accounting().limits(),
                        sticky_accounting_limits));
                    CHECK(same_index_state(
                        fixture.index.stats(), sticky_index_before));
                    CHECK(fixture.index.classify(key) ==
                          sticky_classification);
                    CHECK(fixture.ledger->high_watermark() ==
                          sticky_high_watermark);
                    CHECK(fixture.ledger->accepted().size() ==
                          sticky_accepted);
                    CHECK(fixture.ledger->rejected().size() ==
                          sticky_rejected);
                    CHECK(fixture.ledger->healthy() ==
                          sticky_ledger_health);
                };

                const auto follow_up_notice =
                    fixture.coordinator->apply_notice(
                    AuthenticatedReporter{1}, initialized);
                CHECK(follow_up_notice.status ==
                      ProposalLifecycleApplyStatus::evidence_unhealthy);
                check_sticky_state();
                const auto follow_up_evidence =
                    fixture.coordinator->ingest_observation(
                        AuthenticatedReporter{1},
                        pending);
                CHECK(follow_up_evidence.disposition ==
                      EvidenceObservationDisposition::evidence_unhealthy);
                check_sticky_state();
            }
            if (!complete && !safe_failure)
                inconsistent.push_back(prefix);
        }
        CAPTURE(injected_failures);
        CAPTURE(sticky_failures);
        CAPTURE(inconsistent);
        REQUIRE(injected_failures > 0);
        REQUIRE(sticky_failures > 0);
        CHECK(inconsistent.empty());
    }

    const auto run_retirement_drain_sweep =
        [allocation_sweep](bool advance_floor) {
        std::size_t injected_failures = 0;
        std::size_t sticky_failures = 0;
        std::array<bool, 5> committed_prefix_seen{};
        std::vector<std::size_t> inconsistent;

        for (std::size_t prefix = 0;
             prefix < allocation_sweep * 4;
             ++prefix)
        {
            Fixture fixture;
            const auto pending = fixture.key(
                advance_floor ? "sweep-retirement-floor"
                              : "sweep-retirement-configuration");
            const auto reporter_two_first =
                observation(pending, 2, 5, 1);
            const auto reporter_two_second =
                observation(pending, 2, 6, 2);
            const auto reporter_one_first =
                observation(pending, 1, 3, 1);
            const auto reporter_one_second =
                observation(pending, 1, 4, 2);
            const std::array<ResponseObservation, 4>
                expected_observations{{
                    reporter_one_first,
                    reporter_one_second,
                    reporter_two_first,
                    reporter_two_second}};
            const std::vector<QuarantinedEvidenceObservation>
                initial_quarantine{{AuthenticatedReporter{1},
                                    reporter_one_first,
                                    true},
                                   {AuthenticatedReporter{1},
                                    reporter_one_second,
                                    false},
                                   {AuthenticatedReporter{2},
                                    reporter_two_first,
                                    true},
                                   {AuthenticatedReporter{2},
                                    reporter_two_second,
                                    false}};

            // Insert reporter 2 first so the drain proves that stable replica
            // ID order, rather than map insertion order, chooses reporter 1.
            REQUIRE(fixture.coordinator->ingest_observation(
                        AuthenticatedReporter{2},
                        reporter_two_first)
                        .disposition ==
                    EvidenceObservationDisposition::quarantined_unknown);
            REQUIRE(fixture.coordinator->ingest_observation(
                        AuthenticatedReporter{2},
                        reporter_two_second)
                        .disposition ==
                    EvidenceObservationDisposition::
                        quarantined_behind_unknown);
            REQUIRE(fixture.coordinator->ingest_observation(
                        AuthenticatedReporter{1},
                        reporter_one_first)
                        .disposition ==
                    EvidenceObservationDisposition::quarantined_unknown);
            REQUIRE(fixture.coordinator->ingest_observation(
                        AuthenticatedReporter{1},
                        reporter_one_second)
                        .disposition ==
                    EvidenceObservationDisposition::
                        quarantined_behind_unknown);

            const auto before = fixture.coordinator->stats();
            REQUIRE(before.quarantined_records == 4);
            REQUIRE(before.reporter_queues == 2);
            REQUIRE(before.signer_entries == 4);
            REQUIRE(before.deduplication_entries == 4);
            REQUIRE(before.quarantined_bytes % 4 == 0);
            check_quarantine_snapshot(
                fixture.coordinator->quarantined_observations(),
                initial_quarantine);
            const auto bytes_per_record =
                before.quarantined_bytes / 4;
            const auto accounting_before =
                fixture.coordinator->accounting().stats();
            const auto accounting_limits_before =
                fixture.coordinator->accounting().limits();
            REQUIRE(same_accounting_state(
                accounting_before,
                {4, before.quarantined_bytes, 4}));
            const auto index_before = fixture.index.stats();
            REQUIRE(index_before.healthy);
            REQUIRE_FALSE(index_before.stopped);
            REQUIRE(fixture.index.classify(pending) ==
                    ProposalEvidenceStatus::unknown);

            const auto retirement = advance_floor
                ? notice(
                      6,
                      1,
                      ProposalRetirementFloorAdvanced{1})
                : notice(
                      6,
                      1,
                      ProposalConfigurationRetired{
                          fixture.configuration});
            ProposalLifecycleApplyResult result;
            bool injected = false;
            {
                evidence_lifecycle_allocation_failure::OneShot fault(
                    prefix);
                result = fixture.coordinator->apply_notice(
                    AuthenticatedReporter{6}, retirement);
                injected = fault.triggered();
            }
            if (!injected)
                continue;
            ++injected_failures;

            const auto after = fixture.coordinator->stats();
            const auto index_after = fixture.index.stats();
            const auto classification_after =
                fixture.index.classify(pending);
            const auto committed = fixture.ledger->rejected().size();

            constexpr std::array<ReplicaID, 4> expected_reporters{
                {1, 1, 2, 2}};
            bool exact_rejected_prefix =
                fixture.ledger->accepted().empty() && committed <= 4;
            for (std::size_t index = 0;
                 exact_rejected_prefix && index < committed;
                 ++index)
            {
                const auto &record = fixture.ledger->rejected()[index];
                exact_rejected_prefix =
                    record.ingestion_sequence == index + 1 &&
                    record.authenticated_reporter.replica_id ==
                        expected_reporters[index] &&
                    record.reason ==
                        EvidenceRejectionReason::stale_block &&
                    record.observation.has_value() &&
                    !record.wire_error.has_value() &&
                    same_observation(
                        *record.observation,
                        expected_observations[index]);
            }

            const bool exact_index_unchanged =
                same_index_state(index_after, index_before) &&
                classification_after ==
                    ProposalEvidenceStatus::unknown;
            const bool exact_index_poisoned =
                index_after.admissible_proposals ==
                    index_before.admissible_proposals &&
                index_after.stale_proposals ==
                    index_before.stale_proposals &&
                index_after.retired_configurations ==
                    index_before.retired_configurations &&
                index_after.first_live_epoch ==
                    index_before.first_live_epoch &&
                index_after.capacity_failures ==
                    index_before.capacity_failures &&
                !index_after.healthy &&
                index_after.stopped == index_before.stopped &&
                classification_after ==
                    ProposalEvidenceStatus::unknown;
            const bool retirement_committed = advance_floor
                ? index_after.admissible_proposals ==
                      index_before.admissible_proposals &&
                      index_after.stale_proposals ==
                          index_before.stale_proposals &&
                      index_after.retired_configurations ==
                          index_before.retired_configurations &&
                      index_after.first_live_epoch == 1 &&
                      index_after.capacity_failures ==
                          index_before.capacity_failures &&
                      index_after.healthy == index_before.healthy &&
                      index_after.stopped == index_before.stopped &&
                      classification_after ==
                          ProposalEvidenceStatus::stale
                : index_after.admissible_proposals ==
                      index_before.admissible_proposals &&
                      index_after.stale_proposals ==
                          index_before.stale_proposals &&
                      index_after.retired_configurations ==
                          index_before.retired_configurations + 1 &&
                      index_after.first_live_epoch ==
                          index_before.first_live_epoch &&
                      index_after.capacity_failures ==
                          index_before.capacity_failures &&
                      index_after.healthy == index_before.healthy &&
                      index_after.stopped == index_before.stopped &&
                      classification_after ==
                          ProposalEvidenceStatus::stale;
            const bool exact_index_outcome =
                exact_index_unchanged || exact_index_poisoned ||
                retirement_committed;

            const auto remaining = committed <= 4 ? 4 - committed : 0;
            const auto expected_reporter_queues =
                remaining == 0 ? 0 : committed < 2 ? 2 : 1;
            std::vector<QuarantinedEvidenceObservation>
                expected_remaining;
            if (committed <= expected_observations.size())
            {
                for (std::size_t index = committed;
                     index < expected_observations.size();
                     ++index)
                {
                    const bool is_head =
                        index == committed ||
                        expected_reporters[index] !=
                            expected_reporters[index - 1];
                    expected_remaining.push_back(
                        {AuthenticatedReporter{
                             expected_reporters[index]},
                         expected_observations[index],
                         is_head});
                }
            }
            const auto quarantine_after =
                fixture.coordinator->quarantined_observations();
            const bool exact_queued_suffix =
                same_quarantine_snapshot(
                    quarantine_after, expected_remaining);
            const bool exact_accounting_suffix = committed <= 4 &&
                same_accounting_state(
                    fixture.coordinator->accounting().stats(),
                    {remaining,
                     bytes_per_record * remaining,
                     remaining}) &&
                same_accounting_limits(
                    fixture.coordinator->accounting().limits(),
                    accounting_limits_before);
            const bool exact_retained_suffix = committed <= 4 &&
                after.quarantined_records == remaining &&
                after.quarantined_bytes ==
                    bytes_per_record * remaining &&
                after.reporter_queues == expected_reporter_queues &&
                after.signer_entries == remaining &&
                after.deduplication_entries == remaining &&
                exact_queued_suffix && exact_accounting_suffix;
            const auto high_watermark =
                fixture.ledger->high_watermark();
            const bool exact_ledger_prefix =
                high_watermark == committed ||
                (committed < 4 &&
                 high_watermark == committed + 1 &&
                 !fixture.ledger->healthy());
            const bool exact_complete_coordinator =
                after.lifecycle_sources ==
                    before.lifecycle_sources + 1 &&
                after.duplicate_observations ==
                    before.duplicate_observations &&
                after.applied_lifecycle_notices ==
                    before.applied_lifecycle_notices + 1 &&
                after.capacity_failures == before.capacity_failures &&
                after.healthy && after.stopped == before.stopped;
            const bool exact_failed_coordinator =
                (after.lifecycle_sources == before.lifecycle_sources ||
                 after.lifecycle_sources ==
                     before.lifecycle_sources + 1) &&
                after.duplicate_observations ==
                    before.duplicate_observations &&
                after.applied_lifecycle_notices ==
                    before.applied_lifecycle_notices &&
                after.capacity_failures ==
                    before.capacity_failures + 1 &&
                !after.healthy && after.stopped == before.stopped;

            const bool complete =
                result.status == ProposalLifecycleApplyStatus::applied &&
                result.index_changed && retirement_committed &&
                result.retried_observations == 4 &&
                result.accepted_observations == 0 &&
                result.rejected_observations == 4 &&
                result.remaining_quarantined == 0 &&
                fixture.coordinator->healthy() &&
                committed == 4 && exact_rejected_prefix &&
                exact_retained_suffix && high_watermark == 4 &&
                fixture.ledger->healthy() &&
                exact_complete_coordinator;

            // The ledger record is the commit point for each FIFO head.
            // Therefore reporter 1 drains before reporter 2, and only the
            // exact audited prefix may disappear after an allocation fault.
            const bool safe_failure =
                result.status ==
                    ProposalLifecycleApplyStatus::evidence_unhealthy &&
                !fixture.coordinator->healthy() &&
                exact_failed_coordinator &&
                result.index_changed == retirement_committed &&
                result.retried_observations == committed &&
                result.accepted_observations == 0 &&
                result.rejected_observations == committed &&
                result.remaining_quarantined == remaining &&
                exact_rejected_prefix && exact_retained_suffix &&
                exact_ledger_prefix &&
                exact_index_outcome;

            if (safe_failure)
            {
                ++sticky_failures;
                committed_prefix_seen[committed] = true;
                CAPTURE(prefix);
                CAPTURE(advance_floor);
                CAPTURE(committed);
                const auto sticky_before = after;
                const auto sticky_accounting =
                    fixture.coordinator->accounting().stats();
                const auto sticky_accounting_limits =
                    fixture.coordinator->accounting().limits();
                const auto sticky_quarantine = quarantine_after;
                const auto sticky_index_before = index_after;
                const auto sticky_classification =
                    classification_after;
                const auto sticky_high_watermark = high_watermark;
                const auto sticky_accepted =
                    fixture.ledger->accepted().size();
                const auto sticky_rejected = committed;
                const auto sticky_ledger_health =
                    fixture.ledger->healthy();

                const auto follow_up_notice =
                    fixture.coordinator->apply_notice(
                        AuthenticatedReporter{6}, retirement);
                CHECK(follow_up_notice.status ==
                      ProposalLifecycleApplyStatus::evidence_unhealthy);
                const auto follow_up_evidence =
                    fixture.coordinator->ingest_observation(
                        AuthenticatedReporter{1},
                        observation(
                            fixture.key("retirement-sticky-no-op"),
                            1,
                            3,
                            3));
                CHECK(follow_up_evidence.disposition ==
                      EvidenceObservationDisposition::evidence_unhealthy);

                CHECK(same_lifecycle_state(
                    fixture.coordinator->stats(), sticky_before));
                CHECK(same_accounting_state(
                    fixture.coordinator->accounting().stats(),
                    sticky_accounting));
                CHECK(same_accounting_limits(
                    fixture.coordinator->accounting().limits(),
                    sticky_accounting_limits));
                check_quarantine_snapshot(
                    fixture.coordinator->quarantined_observations(),
                    sticky_quarantine);
                CHECK(same_index_state(
                    fixture.index.stats(), sticky_index_before));
                CHECK(fixture.index.classify(pending) ==
                      sticky_classification);
                CHECK(fixture.ledger->high_watermark() ==
                      sticky_high_watermark);
                CHECK(fixture.ledger->accepted().size() ==
                      sticky_accepted);
                CHECK(fixture.ledger->rejected().size() ==
                      sticky_rejected);
                for (std::size_t index = 0;
                     index < sticky_rejected;
                     ++index)
                {
                    const auto &record =
                        fixture.ledger->rejected()[index];
                    REQUIRE(record.observation.has_value());
                    CHECK(record.ingestion_sequence == index + 1);
                    CHECK(record.authenticated_reporter.replica_id ==
                          expected_reporters[index]);
                    CHECK(record.reason ==
                          EvidenceRejectionReason::stale_block);
                    CHECK_FALSE(record.wire_error.has_value());
                    CHECK(same_observation(
                        *record.observation,
                        expected_observations[index]));
                }
                CHECK(fixture.ledger->healthy() ==
                      sticky_ledger_health);
            }
            if (!complete && !safe_failure)
                inconsistent.push_back(prefix);
        }

        CAPTURE(advance_floor);
        CAPTURE(injected_failures);
        CAPTURE(sticky_failures);
        CAPTURE(committed_prefix_seen);
        CAPTURE(inconsistent);
        REQUIRE(injected_failures > 0);
        REQUIRE(sticky_failures > 0);
        for (std::size_t committed = 0;
             committed < committed_prefix_seen.size();
             ++committed)
        {
            CAPTURE(committed);
            CHECK(committed_prefix_seen[committed]);
        }
        CHECK(inconsistent.empty());
    };

    SECTION("configuration retirement drains two reporters across allocation prefixes")
    {
        run_retirement_drain_sweep(false);
    }

    SECTION("retirement floor drains two reporters across allocation prefixes")
    {
        run_retirement_drain_sweep(true);
    }

    SECTION("direct known evidence exposes only complete or empty ledger audit")
    {
        std::size_t injected_failures = 0;
        std::size_t sticky_failures = 0;
        std::vector<std::size_t> inconsistent;
        for (std::size_t prefix = 0; prefix < allocation_sweep; ++prefix)
        {
            Fixture fixture;
            const auto key = fixture.key("sweep-direct-ledger");
            REQUIRE(admit(fixture, 1, 1, key).status ==
                    ProposalLifecycleApplyStatus::applied);
            const auto before = fixture.coordinator->stats();
            const auto direct = observation(key, 1, 3, 1);
            EvidenceObservationResult result;
            bool injected = false;
            {
                evidence_lifecycle_allocation_failure::OneShot fault(prefix);
                result = fixture.coordinator->ingest_observation(
                    AuthenticatedReporter{1}, direct);
                injected = fault.triggered();
            }
            if (!injected)
                continue;
            ++injected_failures;

            const auto after = fixture.coordinator->stats();
            const bool complete =
                result.disposition ==
                    EvidenceObservationDisposition::ingested &&
                result.accepted_observations == 1 &&
                result.rejected_observations == 0 &&
                fixture.coordinator->healthy() &&
                fixture.ledger->high_watermark() == 1 &&
                fixture.ledger->accepted().size() == 1 &&
                fixture.ledger->rejected().empty();
            const bool failed_atomically =
                result.disposition ==
                    EvidenceObservationDisposition::evidence_unhealthy &&
                result.accepted_observations == 0 &&
                result.rejected_observations == 0 &&
                !fixture.coordinator->healthy() &&
                !fixture.ledger->healthy() &&
                same_retained_state(after, before) &&
                fixture.ledger->high_watermark() == 1 &&
                fixture.ledger->accepted().empty() &&
                fixture.ledger->rejected().empty();
            if (failed_atomically)
            {
                ++sticky_failures;
                CAPTURE(prefix);
                const auto sticky_before = after;
                const auto sticky_index_before = fixture.index.stats();
                const auto sticky_high_watermark =
                    fixture.ledger->high_watermark();
                const auto sticky_accepted =
                    fixture.ledger->accepted().size();
                const auto sticky_rejected =
                    fixture.ledger->rejected().size();
                const auto sticky_ledger_health =
                    fixture.ledger->healthy();

                const auto follow_up =
                    fixture.coordinator->ingest_observation(
                        AuthenticatedReporter{1}, direct);
                CHECK(follow_up.disposition ==
                      EvidenceObservationDisposition::evidence_unhealthy);
                const auto sticky_after = fixture.coordinator->stats();
                CHECK_FALSE(sticky_after.healthy);
                CHECK(same_retained_state(
                    sticky_after, sticky_before));
                CHECK(sticky_after.capacity_failures ==
                      sticky_before.capacity_failures);
                CHECK(same_index_state(
                    fixture.index.stats(), sticky_index_before));
                CHECK(fixture.ledger->high_watermark() ==
                      sticky_high_watermark);
                CHECK(fixture.ledger->accepted().size() ==
                      sticky_accepted);
                CHECK(fixture.ledger->rejected().size() ==
                      sticky_rejected);
                CHECK(fixture.ledger->healthy() ==
                      sticky_ledger_health);
            }
            if (!complete && !failed_atomically)
                inconsistent.push_back(prefix);
        }
        CAPTURE(injected_failures);
        CAPTURE(sticky_failures);
        CAPTURE(inconsistent);
        REQUIRE(injected_failures > 0);
        REQUIRE(sticky_failures > 0);
        CHECK(inconsistent.empty());
    }
}

TEST_CASE("shutdown stops evidence only and preserves ledger state",
          "[e08][evidence-lifecycle][shutdown][intentional-red]")
{
    Fixture fixture;
    const auto key = fixture.key("shutdown");
    REQUIRE(fixture.coordinator->ingest_observation(
                AuthenticatedReporter{1},
                observation(key, 1, 3, 1))
                .disposition ==
            EvidenceObservationDisposition::quarantined_unknown);
    const auto before = fixture.coordinator->stats();

    fixture.coordinator->shutdown();
    const auto stopped = fixture.coordinator->stats();
    CHECK(stopped.stopped);
    CHECK(stopped.quarantined_records == before.quarantined_records);
    CHECK(fixture.ledger->high_watermark() == 0);

    CHECK(admit(fixture, 1, 1, key).status ==
          ProposalLifecycleApplyStatus::stopped);
    CHECK(fixture.coordinator->ingest_observation(
              AuthenticatedReporter{1},
              observation(key, 1, 3, 2))
              .disposition ==
          EvidenceObservationDisposition::stopped);
    CHECK(fixture.index.classify(key) == ProposalEvidenceStatus::unknown);
    CHECK(fixture.ledger->high_watermark() == 0);
}
