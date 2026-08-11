#include <algorithm>
#include <cstddef>
#include <cstdint>
#include <cstdlib>
#include <limits>
#include <map>
#include <memory>
#include <new>
#include <optional>
#include <string>
#include <type_traits>
#include <utility>
#include <vector>

#include "catch.hpp"
#include "hotstuff/configuration.h"
#include "hotstuff/epoch_store.h"

namespace evidence_allocation_failure
{

constexpr std::size_t disabled = std::numeric_limits<std::size_t>::max();
thread_local std::size_t allocations_before_failure = disabled;

bool consume_failure() noexcept
{
    if (allocations_before_failure == disabled)
        return false;
    if (allocations_before_failure == 0)
    {
        allocations_before_failure = disabled;
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
        allocations_before_failure = successful_allocations;
    }

    ~OneShot()
    {
        disable();
    }

    OneShot(const OneShot &) = delete;
    OneShot &operator=(const OneShot &) = delete;
};

} // namespace evidence_allocation_failure

void *operator new(std::size_t size)
{
    if (evidence_allocation_failure::consume_failure())
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
 * E08 pure evidence contract
 * --------------------------
 * This target deliberately covers only the two dependency-free E08 lanes:
 *
 *   1. canonical observation identity plus bounded, versioned batch wire I/O;
 *   2. ledger validation against immutable epochs and an injected proposal
 *      evidence window.
 *
 * Runtime observation emission, client message handlers, adaptation ranking,
 * and the manager executable belong to later E08/R09/M12 lanes.  The fallback
 * mirrors the public seam so this tests-first commit compiles before
 * include/hotstuff/evidence.h exists; its inert behavior keeps the target
 * intentionally red.
 *
 * Canonical wire layout is big-endian and bounded before allocation:
 *
 *   batch schema:u32, count:u32,
 *   repeated observation schema:u32, observation id:32 bytes,
 *   reporter:u16, observed:u16, epoch:u32, tree:u32,
 *   epoch digest:32 bytes, block hash:32 bytes,
 *   message type:u8, outcome:u8,
 *   response/deadline/monotonic/sequence:u64 each,
 *   signer count:u32, signer ids:u16 each.
 */
#if __has_include("hotstuff/evidence.h")
#include "hotstuff/evidence.h"
#define KAURI_HAS_E08_EVIDENCE_API 1
#else
#define KAURI_HAS_E08_EVIDENCE_API 0

namespace hotstuff
{

constexpr std::uint32_t kResponseObservationSchemaVersion = 1;
constexpr std::uint32_t kEvidenceBatchSchemaVersion = 1;

enum class ExpectedMessageType : std::uint8_t
{
    direct_vote = 1,
    aggregate_relay = 2,
    leader_progress = 3,
};

enum class ResponseOutcome : std::uint8_t
{
    on_time = 1,
    timeout = 2,
    late = 3,
};

struct ResponseAttemptIdentity
{
    ReplicaID reporter_id{0};
    ReplicaID observed_replica_id{0};
    ProposalKey proposal;
    ExpectedMessageType expected_message_type{
        ExpectedMessageType::direct_vote};

    bool operator==(const ResponseAttemptIdentity &other) const noexcept
    {
        return reporter_id == other.reporter_id &&
               observed_replica_id == other.observed_replica_id &&
               proposal == other.proposal &&
               expected_message_type == other.expected_message_type;
    }

    bool operator!=(const ResponseAttemptIdentity &other) const noexcept
    {
        return !(*this == other);
    }
};

struct ResponseObservation
{
    std::uint32_t schema_version{kResponseObservationSchemaVersion};
    uint256_t observation_id;
    ReplicaID reporter_id{0};
    ReplicaID observed_replica_id{0};
    ConfigurationId configuration;
    uint256_t block_hash;
    ExpectedMessageType expected_message_type{
        ExpectedMessageType::direct_vote};
    ResponseOutcome outcome{ResponseOutcome::on_time};
    std::uint64_t response_duration_us{0};
    std::uint64_t deadline_duration_us{0};
    std::uint64_t reporter_monotonic_ns{0};
    std::uint64_t reporter_sequence{0};
    std::vector<ReplicaID> signer_set;

    ProposalKey proposal_key() const
    {
        return {configuration, block_hash};
    }

    ResponseAttemptIdentity attempt_identity() const
    {
        return {reporter_id,
                observed_replica_id,
                proposal_key(),
                expected_message_type};
    }
};

inline uint256_t compute_response_observation_id(
    const ResponseAttemptIdentity &)
{
    return {};
}

struct ResponseObservationBatch
{
    std::uint32_t schema_version{kEvidenceBatchSchemaVersion};
    std::vector<ResponseObservation> observations;
};

struct EvidenceWireLimits
{
    std::size_t maximum_payload_bytes{1024 * 1024};
    std::uint32_t maximum_observations{1024};
    std::uint32_t maximum_signers_per_observation{4096};
};

enum class EvidenceWireError : std::uint8_t
{
    none = 0,
    payload_too_large,
    unsupported_batch_schema,
    batch_count_exceeded,
    truncated,
    trailing_bytes,
    unsupported_observation_schema,
    invalid_expected_message_type,
    invalid_outcome,
    signer_count_exceeded,
    noncanonical_signer_set,
};

struct EvidenceDecodeResult
{
    EvidenceWireError error{EvidenceWireError::none};
    std::optional<ResponseObservationBatch> batch;

    explicit operator bool() const noexcept
    {
        return error == EvidenceWireError::none && batch.has_value();
    }
};

inline bytearray_t encode_evidence_batch(
    const ResponseObservationBatch &,
    const EvidenceWireLimits &)
{
    return {};
}

inline EvidenceDecodeResult decode_evidence_batch(
    const bytearray_t &,
    const EvidenceWireLimits &) noexcept
{
    return {EvidenceWireError::truncated, std::nullopt};
}

enum class ProposalEvidenceStatus : std::uint8_t
{
    admissible = 1,
    stale = 2,
    unknown = 3,
};

class ProposalEvidenceWindow
{
public:
    virtual ~ProposalEvidenceWindow() = default;

    virtual ProposalEvidenceStatus classify(
        const ProposalKey &proposal) const noexcept = 0;
};

struct AuthenticatedReporter
{
    ReplicaID replica_id{0};
};

enum class EvidenceRejectionReason : std::uint8_t
{
    unsupported_schema = 1,
    observation_id_mismatch,
    reporter_mismatch,
    unknown_configuration,
    unknown_block,
    stale_block,
    impossible_topology,
    invalid_expected_message_type,
    invalid_outcome,
    invalid_timing,
    invalid_signer_set,
    duplicate_fact,
    invalid_transition,
    reporter_sequence_regression,
    reporter_timestamp_regression,
    accepted_capacity_exceeded,
    wire_error,
};

struct AcceptedEvidenceRecord
{
    std::uint64_t ingestion_sequence{0};
    ResponseObservation observation;
};

struct RejectedEvidenceRecord
{
    std::uint64_t ingestion_sequence{0};
    AuthenticatedReporter authenticated_reporter;
    EvidenceRejectionReason reason{
        EvidenceRejectionReason::unsupported_schema};
    std::optional<ResponseObservation> observation;
    std::optional<EvidenceWireError> wire_error;
};

struct EvidenceStoreLimits
{
    std::size_t maximum_accepted_records{4096};
    std::size_t maximum_rejected_records{4096};
};

class EvidenceLedger
{
public:
    EvidenceLedger(const EpochStore &,
                   const ProposalEvidenceWindow &,
                   EvidenceStoreLimits)
    {}

    void ingest(const AuthenticatedReporter &, const ResponseObservation &) {}

    bool ingest_if_proposal_independent_rejected(
        const AuthenticatedReporter &,
        const ResponseObservation &)
    {
        return false;
    }

    void reject_wire(const AuthenticatedReporter &, EvidenceWireError) {}

    std::uint64_t high_watermark() const noexcept
    {
        return 0;
    }

    const std::vector<AcceptedEvidenceRecord> &accepted() const noexcept
    {
        return accepted_;
    }

    const std::vector<RejectedEvidenceRecord> &rejected() const noexcept
    {
        return rejected_;
    }

    bool healthy() const noexcept
    {
        return false;
    }

private:
    std::vector<AcceptedEvidenceRecord> accepted_;
    std::vector<RejectedEvidenceRecord> rejected_;
};

} // namespace hotstuff
#endif

namespace
{

using hotstuff::AcceptedEvidenceRecord;
using hotstuff::AuthenticatedReporter;
using hotstuff::ConfigurationId;
using hotstuff::EpochDefinitionInput;
using hotstuff::EpochStore;
using hotstuff::EpochTreeDefinition;
using hotstuff::EpochValidationContext;
using hotstuff::EvidenceDecodeResult;
using hotstuff::EvidenceLedger;
using hotstuff::EvidenceRejectionReason;
using hotstuff::EvidenceStoreLimits;
using hotstuff::EvidenceWireError;
using hotstuff::EvidenceWireLimits;
using hotstuff::ExpectedMessageType;
using hotstuff::ProposalEvidenceStatus;
using hotstuff::ProposalEvidenceWindow;
using hotstuff::ProposalKey;
using hotstuff::ReplicaID;
using hotstuff::ResponseAttemptIdentity;
using hotstuff::ResponseObservation;
using hotstuff::ResponseObservationBatch;
using hotstuff::ResponseOutcome;
using hotstuff::bytearray_t;
using hotstuff::uint256_t;

constexpr std::uint32_t kTreeId = 7;

uint256_t fixture_digest(const std::string &label)
{
    hotstuff::DataStream stream(label);
    return stream.get_hash();
}

std::vector<ReplicaID> membership()
{
    return {0, 1, 2, 3, 4, 5, 6};
}

EpochDefinitionInput epoch_zero_input()
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
    input.generation_seed = 0xE08;
    input.policy_version = "e08-test-policy-v1";
    input.evidence_snapshot_id = "e08-window-0001";
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

template <typename UInt>
void append_big_endian(bytearray_t &bytes, UInt value)
{
    static_assert(std::is_unsigned<UInt>::value,
                  "canonical wire integers are unsigned");
    for (std::size_t shift = sizeof(UInt); shift > 0; --shift)
    {
        bytes.push_back(static_cast<std::uint8_t>(
            value >> ((shift - 1) * 8)));
    }
}

void append_digest(bytearray_t &bytes, const uint256_t &digest)
{
    const auto raw = static_cast<bytearray_t>(digest);
    REQUIRE(raw.size() == 32);
    bytes.insert(bytes.end(), raw.begin(), raw.end());
}

void overwrite_u32(bytearray_t &bytes,
                   std::size_t offset,
                   std::uint32_t value)
{
    REQUIRE(offset + sizeof(value) <= bytes.size());
    for (std::size_t index = 0; index < sizeof(value); ++index)
    {
        bytes[offset + index] = static_cast<std::uint8_t>(
            value >> ((sizeof(value) - index - 1) * 8));
    }
}

uint256_t canonical_observation_id(
    const ResponseAttemptIdentity &identity)
{
    constexpr char domain[] = "kauri-response-observation-v1";
    bytearray_t bytes(domain, domain + sizeof(domain) - 1);
    append_big_endian(bytes, identity.reporter_id);
    append_big_endian(bytes, identity.observed_replica_id);
    append_big_endian(
        bytes, identity.proposal.configuration.epoch_number);
    append_big_endian(bytes, identity.proposal.configuration.tree_id);
    append_digest(bytes, identity.proposal.configuration.epoch_digest);
    append_digest(bytes, identity.proposal.block_hash);
    append_big_endian(
        bytes,
        static_cast<std::underlying_type_t<ExpectedMessageType>>(
            identity.expected_message_type));
    return hotstuff::DataStream(bytes).get_hash();
}

bytearray_t canonical_wire(const ResponseObservationBatch &batch)
{
    bytearray_t bytes;
    append_big_endian(bytes, batch.schema_version);
    append_big_endian(
        bytes, static_cast<std::uint32_t>(batch.observations.size()));

    for (const auto &observation : batch.observations)
    {
        append_big_endian(bytes, observation.schema_version);
        append_digest(bytes, observation.observation_id);
        append_big_endian(bytes, observation.reporter_id);
        append_big_endian(bytes, observation.observed_replica_id);
        append_big_endian(
            bytes, observation.configuration.epoch_number);
        append_big_endian(bytes, observation.configuration.tree_id);
        append_digest(bytes, observation.configuration.epoch_digest);
        append_digest(bytes, observation.block_hash);
        append_big_endian(
            bytes,
            static_cast<std::underlying_type_t<ExpectedMessageType>>(
                observation.expected_message_type));
        append_big_endian(
            bytes,
            static_cast<std::underlying_type_t<ResponseOutcome>>(
                observation.outcome));
        append_big_endian(bytes, observation.response_duration_us);
        append_big_endian(bytes, observation.deadline_duration_us);
        append_big_endian(bytes, observation.reporter_monotonic_ns);
        append_big_endian(bytes, observation.reporter_sequence);
        if (observation.schema_version ==
            hotstuff::kResponseObservationSchemaVersionV2)
        {
            append_big_endian(
                bytes, observation.attempt_start_monotonic_ns);
            append_big_endian(
                bytes,
                observation.reporter_local_commit_monotonic_ns);
        }
        append_big_endian(
            bytes,
            static_cast<std::uint32_t>(observation.signer_set.size()));
        for (const auto signer : observation.signer_set)
        {
            append_big_endian(bytes, signer);
        }
    }
    return bytes;
}

ResponseObservation observation(
    const ConfigurationId &configuration,
    const uint256_t &block_hash,
    ReplicaID reporter,
    ReplicaID observed,
    ExpectedMessageType type,
    ResponseOutcome outcome,
    std::vector<ReplicaID> signers,
    std::uint64_t reporter_sequence = 1,
    std::uint64_t reporter_monotonic_ns = 1'000)
{
    ResponseObservation value;
    value.schema_version = hotstuff::kResponseObservationSchemaVersion;
    value.reporter_id = reporter;
    value.observed_replica_id = observed;
    value.configuration = configuration;
    value.block_hash = block_hash;
    value.expected_message_type = type;
    value.outcome = outcome;
    value.deadline_duration_us = 100;
    value.response_duration_us =
        outcome == ResponseOutcome::timeout
            ? 0
            : (outcome == ResponseOutcome::late ? 150 : 50);
    value.reporter_monotonic_ns = reporter_monotonic_ns;
    value.reporter_sequence = reporter_sequence;
    value.signer_set = std::move(signers);
    value.observation_id = hotstuff::compute_response_observation_id(
        value.attempt_identity());
    return value;
}

class MutableEvidenceWindow final : public ProposalEvidenceWindow
{
public:
    void set(const ProposalKey &proposal, ProposalEvidenceStatus status)
    {
        statuses_[proposal] = status;
    }

    ProposalEvidenceStatus classify(
        const ProposalKey &proposal) const noexcept override
    {
        const auto found = statuses_.find(proposal);
        return found == statuses_.end()
                   ? ProposalEvidenceStatus::unknown
                   : found->second;
    }

private:
    std::map<ProposalKey, ProposalEvidenceStatus> statuses_;
};

struct EvidenceFixture
{
    EpochStore epochs{membership()};
    MutableEvidenceWindow window;
    ConfigurationId configuration;
    uint256_t block_a{fixture_digest("block-a")};
    uint256_t block_b{fixture_digest("block-b")};
    uint256_t block_c{fixture_digest("block-c")};

    EvidenceFixture()
    {
        const auto &epoch = epochs.stage(
            epoch_zero_input(), validation_context());
        configuration =
            ConfigurationId{0, kTreeId, epoch.epoch_digest()};
        window.set(
            ProposalKey{configuration, block_a},
            ProposalEvidenceStatus::admissible);
        window.set(
            ProposalKey{configuration, block_b},
            ProposalEvidenceStatus::admissible);
        window.set(
            ProposalKey{configuration, block_c},
            ProposalEvidenceStatus::admissible);
    }
};

EvidenceStoreLimits generous_store_limits()
{
    return EvidenceStoreLimits{128, 128};
}

const AcceptedEvidenceRecord &only_accepted(const EvidenceLedger &ledger)
{
    REQUIRE(ledger.accepted().size() == 1);
    return ledger.accepted().front();
}

EvidenceRejectionReason only_rejection(const EvidenceLedger &ledger)
{
    REQUIRE(ledger.accepted().empty());
    REQUIRE(ledger.rejected().size() == 1);
    return ledger.rejected().front().reason;
}

ResponseObservation aggregate_on_time(
    const EvidenceFixture &fixture,
    const uint256_t &block,
    std::uint64_t sequence = 1,
    std::uint64_t monotonic_ns = 1'000)
{
    return observation(
        fixture.configuration,
        block,
        0,
        1,
        ExpectedMessageType::aggregate_relay,
        ResponseOutcome::on_time,
        {3, 4},
        sequence,
        monotonic_ns);
}

ResponseObservation leaf_on_time(
    const EvidenceFixture &fixture,
    const uint256_t &block,
    std::uint64_t sequence = 1,
    std::uint64_t monotonic_ns = 1'000)
{
    return observation(
        fixture.configuration,
        block,
        1,
        3,
        ExpectedMessageType::direct_vote,
        ResponseOutcome::on_time,
        {3},
        sequence,
        monotonic_ns);
}

template <typename Callable>
bool throws_injected_bad_alloc(
    std::size_t successful_allocations,
    Callable &&callable)
{
    try
    {
        evidence_allocation_failure::OneShot fault(
            successful_allocations);
        callable();
    }
    catch (const std::bad_alloc &)
    {
        return true;
    }
    return false;
}

} // namespace

TEST_CASE("E08 evidence API is available with pinned protocol values",
          "[e08][evidence][contract][intentional-red]")
{
    CHECK(KAURI_HAS_E08_EVIDENCE_API == 1);
    CHECK(hotstuff::kResponseObservationSchemaVersion == 1);
    CHECK(hotstuff::kEvidenceBatchSchemaVersion == 1);
    CHECK(static_cast<std::uint8_t>(ExpectedMessageType::direct_vote) == 1);
    CHECK(static_cast<std::uint8_t>(ExpectedMessageType::aggregate_relay) == 2);
    CHECK(static_cast<std::uint8_t>(ExpectedMessageType::leader_progress) == 3);
    CHECK(static_cast<std::uint8_t>(ResponseOutcome::on_time) == 1);
    CHECK(static_cast<std::uint8_t>(ResponseOutcome::timeout) == 2);
    CHECK(static_cast<std::uint8_t>(ResponseOutcome::late) == 3);
    CHECK(static_cast<std::uint8_t>(EvidenceWireError::allocation_failure) ==
          11);
    CHECK(static_cast<std::uint8_t>(EvidenceWireError::internal_failure) ==
          12);
    CHECK(static_cast<std::uint8_t>(
              EvidenceWireError::invalid_retention_witness) == 13);
}

TEST_CASE("E08 fixture preserves exact breadth-first topology and window keys",
          "[e08][evidence][control]")
{
    EvidenceFixture fixture;
    const auto *tree = fixture.epochs.find_tree(0, kTreeId);
    REQUIRE(tree != nullptr);
    CHECK(tree->fanout == 2);
    CHECK(tree->members_breadth_first == membership());
    CHECK(fixture.window.classify(
              ProposalKey{fixture.configuration, fixture.block_a}) ==
          ProposalEvidenceStatus::admissible);
    CHECK(fixture.window.classify(
              ProposalKey{fixture.configuration,
                          fixture_digest("never-proposed")}) ==
          ProposalEvidenceStatus::unknown);
}

TEST_CASE("response observation ID is canonical over the exact attempt identity",
          "[e08][evidence][identity][intentional-red]")
{
    EvidenceFixture fixture;
    const ResponseAttemptIdentity identity{
        0,
        1,
        ProposalKey{fixture.configuration, fixture.block_a},
        ExpectedMessageType::aggregate_relay};
    const auto expected = canonical_observation_id(identity);

    CHECK(hotstuff::compute_response_observation_id(identity) == expected);
    CHECK(expected != uint256_t{});

    auto changed = identity;
    changed.reporter_id = 2;
    CHECK(hotstuff::compute_response_observation_id(changed) != expected);

    changed = identity;
    changed.observed_replica_id = 2;
    CHECK(hotstuff::compute_response_observation_id(changed) != expected);

    changed = identity;
    ++changed.proposal.configuration.epoch_number;
    CHECK(hotstuff::compute_response_observation_id(changed) != expected);

    changed = identity;
    ++changed.proposal.configuration.tree_id;
    CHECK(hotstuff::compute_response_observation_id(changed) != expected);

    changed = identity;
    changed.proposal.configuration.epoch_digest =
        fixture_digest("divergent-epoch");
    CHECK(hotstuff::compute_response_observation_id(changed) != expected);

    changed = identity;
    changed.proposal.block_hash = fixture.block_b;
    CHECK(hotstuff::compute_response_observation_id(changed) != expected);

    changed = identity;
    changed.expected_message_type = ExpectedMessageType::direct_vote;
    CHECK(hotstuff::compute_response_observation_id(changed) != expected);
}

TEST_CASE("timeout and late facts retain one attempt identity",
          "[e08][evidence][identity][timeout][late][intentional-red]")
{
    EvidenceFixture fixture;
    auto timeout = observation(
        fixture.configuration,
        fixture.block_a,
        0,
        1,
        ExpectedMessageType::aggregate_relay,
        ResponseOutcome::timeout,
        {},
        10,
        1'000);
    auto late = timeout;
    late.outcome = ResponseOutcome::late;
    late.response_duration_us = 150;
    late.reporter_sequence = 11;
    late.reporter_monotonic_ns = 2'000;
    late.signer_set = {3, 4};

    CHECK(timeout.proposal_key() ==
          ProposalKey{fixture.configuration, fixture.block_a});
    CHECK(timeout.attempt_identity() == late.attempt_identity());
    CHECK(timeout.observation_id == late.observation_id);
    CHECK(timeout.observation_id == canonical_observation_id(
                                       timeout.attempt_identity()));
}

TEST_CASE("evidence batch wire is canonical bounded and round trips",
          "[e08][evidence][wire][roundtrip][intentional-red]")
{
    EvidenceFixture fixture;
    const ResponseObservationBatch batch{
        hotstuff::kEvidenceBatchSchemaVersion,
        {aggregate_on_time(fixture, fixture.block_a)}};
    const EvidenceWireLimits limits{4096, 4, 7};
    const auto expected = canonical_wire(batch);

    static_assert(
        noexcept(hotstuff::decode_evidence_batch(
            std::declval<const bytearray_t &>(),
            std::declval<const EvidenceWireLimits &>())),
        "untrusted evidence decoding must be noexcept");

    const auto encoded = hotstuff::encode_evidence_batch(batch, limits);
    CHECK(encoded == expected);
    CHECK(encoded.size() == 162);

    const EvidenceDecodeResult decoded =
        hotstuff::decode_evidence_batch(expected, limits);
    REQUIRE(static_cast<bool>(decoded));
    REQUIRE(decoded.error == EvidenceWireError::none);
    REQUIRE(decoded.batch.has_value());
    REQUIRE(decoded.batch->observations.size() == 1);

    const auto &actual = decoded.batch->observations.front();
    const auto &wanted = batch.observations.front();
    CHECK(decoded.batch->schema_version == batch.schema_version);
    CHECK(actual.schema_version == wanted.schema_version);
    CHECK(actual.observation_id == wanted.observation_id);
    CHECK(actual.reporter_id == wanted.reporter_id);
    CHECK(actual.observed_replica_id == wanted.observed_replica_id);
    CHECK(actual.configuration == wanted.configuration);
    CHECK(actual.block_hash == wanted.block_hash);
    CHECK(actual.expected_message_type == wanted.expected_message_type);
    CHECK(actual.outcome == wanted.outcome);
    CHECK(actual.response_duration_us == wanted.response_duration_us);
    CHECK(actual.deadline_duration_us == wanted.deadline_duration_us);
    CHECK(actual.reporter_monotonic_ns == wanted.reporter_monotonic_ns);
    CHECK(actual.reporter_sequence == wanted.reporter_sequence);
    CHECK(actual.signer_set == wanted.signer_set);
}

TEST_CASE(
    "response observation v2 round trips strict retained commit chronology",
    "[adaptive-v2][evidence][wire][v2][retention][intentional-red]")
{
    EvidenceFixture fixture;
    auto retained = observation(
        fixture.configuration,
        fixture.block_a,
        0,
        1,
        ExpectedMessageType::aggregate_relay,
        ResponseOutcome::timeout,
        {},
        10,
        1'100'000);
    retained.schema_version =
        hotstuff::kResponseObservationSchemaVersionV2;
    retained.attempt_start_monotonic_ns = 1'000'000;
    retained.reporter_local_commit_monotonic_ns = 1'050'000;

    const ResponseObservationBatch batch{
        hotstuff::kEvidenceBatchSchemaVersion, {retained}};
    const EvidenceWireLimits limits{4096, 4, 7};
    const auto expected = canonical_wire(batch);
    const auto encoded = hotstuff::encode_evidence_batch(batch, limits);
    CHECK(encoded == expected);

    const auto decoded = hotstuff::decode_evidence_batch(encoded, limits);
    REQUIRE(static_cast<bool>(decoded));
    REQUIRE(decoded.batch->observations.size() == 1);
    const auto &actual = decoded.batch->observations.front();
    CHECK(actual.schema_version ==
          hotstuff::kResponseObservationSchemaVersionV2);
    CHECK(actual.attempt_start_monotonic_ns == 1'000'000);
    CHECK(actual.reporter_local_commit_monotonic_ns == 1'050'000);
    CHECK(actual.observation_id == retained.observation_id);
    CHECK(actual.observation_id ==
          hotstuff::compute_response_observation_id(
              retained.attempt_identity()));

    EvidenceLedger ledger(
        fixture.epochs, fixture.window, generous_store_limits());
    ledger.ingest(AuthenticatedReporter{0}, actual);
    REQUIRE(ledger.accepted().size() == 1);
    CHECK(ledger.accepted().front().observation.schema_version ==
          hotstuff::kResponseObservationSchemaVersionV2);
}

TEST_CASE(
    "response observation v2 rejects partial overflow and unordered retention",
    "[adaptive-v2][evidence][wire][v2][retention][invalid]"
    "[intentional-red]")
{
    EvidenceFixture fixture;
    auto retained = observation(
        fixture.configuration,
        fixture.block_a,
        0,
        1,
        ExpectedMessageType::aggregate_relay,
        ResponseOutcome::timeout,
        {},
        10,
        1'100'000);
    retained.schema_version =
        hotstuff::kResponseObservationSchemaVersionV2;
    retained.attempt_start_monotonic_ns = 1'000'000;
    retained.reporter_local_commit_monotonic_ns = 0;
    const EvidenceWireLimits limits{4096, 4, 7};
    CHECK_THROWS_AS(
        hotstuff::encode_evidence_batch(
            ResponseObservationBatch{
                hotstuff::kEvidenceBatchSchemaVersion, {retained}},
            limits),
        std::invalid_argument);

    retained.reporter_local_commit_monotonic_ns = 1'100'001;
    CHECK_THROWS_AS(
        hotstuff::encode_evidence_batch(
            ResponseObservationBatch{
                hotstuff::kEvidenceBatchSchemaVersion, {retained}},
            limits),
        std::invalid_argument);

    retained.reporter_local_commit_monotonic_ns = 1'050'000;
    retained.deadline_duration_us =
        std::numeric_limits<std::uint64_t>::max();
    CHECK_THROWS_AS(
        hotstuff::encode_evidence_batch(
            ResponseObservationBatch{
                hotstuff::kEvidenceBatchSchemaVersion, {retained}},
            limits),
        std::invalid_argument);
}

TEST_CASE("evidence wire accepts every version-one message and outcome enum",
          "[e08][evidence][wire][enum][intentional-red]")
{
    EvidenceFixture fixture;
    const EvidenceWireLimits limits{4096, 4, 7};

    for (const auto type :
         {ExpectedMessageType::direct_vote,
          ExpectedMessageType::aggregate_relay,
          ExpectedMessageType::leader_progress})
    {
        auto value = aggregate_on_time(fixture, fixture.block_a);
        value.expected_message_type = type;
        value.observation_id = hotstuff::compute_response_observation_id(
            value.attempt_identity());
        const ResponseObservationBatch batch{
            hotstuff::kEvidenceBatchSchemaVersion, {value}};
        const auto decoded = hotstuff::decode_evidence_batch(
            canonical_wire(batch), limits);
        REQUIRE(static_cast<bool>(decoded));
        REQUIRE(decoded.batch->observations.size() == 1);
        CHECK(decoded.batch->observations.front().expected_message_type ==
              type);
    }

    for (const auto outcome :
         {ResponseOutcome::on_time,
          ResponseOutcome::timeout,
          ResponseOutcome::late})
    {
        auto value = aggregate_on_time(fixture, fixture.block_a);
        value.outcome = outcome;
        value.response_duration_us =
            outcome == ResponseOutcome::timeout
                ? 0
                : (outcome == ResponseOutcome::late ? 150 : 50);
        value.signer_set =
            outcome == ResponseOutcome::timeout
                ? std::vector<ReplicaID>{}
                : std::vector<ReplicaID>{3, 4};
        const ResponseObservationBatch batch{
            hotstuff::kEvidenceBatchSchemaVersion, {value}};
        const auto decoded = hotstuff::decode_evidence_batch(
            canonical_wire(batch), limits);
        REQUIRE(static_cast<bool>(decoded));
        REQUIRE(decoded.batch->observations.size() == 1);
        CHECK(decoded.batch->observations.front().outcome == outcome);
    }
}

TEST_CASE("evidence decoder rejects unsupported schemas and enum values",
          "[e08][evidence][wire][schema][enum][intentional-red]")
{
    EvidenceFixture fixture;
    const ResponseObservationBatch batch{
        hotstuff::kEvidenceBatchSchemaVersion,
        {aggregate_on_time(fixture, fixture.block_a)}};
    const EvidenceWireLimits limits{4096, 4, 7};

    SECTION("batch schema")
    {
        auto wire = canonical_wire(batch);
        overwrite_u32(wire, 0, hotstuff::kEvidenceBatchSchemaVersion + 1);
        CHECK(hotstuff::decode_evidence_batch(wire, limits).error ==
              EvidenceWireError::unsupported_batch_schema);
    }

    SECTION("observation schema")
    {
        auto wire = canonical_wire(batch);
        overwrite_u32(
            wire,
            8,
            hotstuff::kResponseObservationSchemaVersionV2 + 1);
        CHECK(hotstuff::decode_evidence_batch(wire, limits).error ==
              EvidenceWireError::unsupported_observation_schema);
    }

    SECTION("message type")
    {
        auto wire = canonical_wire(batch);
        wire.at(120) = 0x7f;
        CHECK(hotstuff::decode_evidence_batch(wire, limits).error ==
              EvidenceWireError::invalid_expected_message_type);
    }

    SECTION("outcome")
    {
        auto wire = canonical_wire(batch);
        wire.at(121) = 0x7f;
        CHECK(hotstuff::decode_evidence_batch(wire, limits).error ==
              EvidenceWireError::invalid_outcome);
    }
}

TEST_CASE("evidence decoder rejects truncation trailing bytes and bounds",
          "[e08][evidence][wire][bounds][intentional-red]")
{
    EvidenceFixture fixture;
    const ResponseObservationBatch batch{
        hotstuff::kEvidenceBatchSchemaVersion,
        {aggregate_on_time(fixture, fixture.block_a)}};
    const auto valid = canonical_wire(batch);
    const EvidenceWireLimits limits{4096, 4, 7};

    SECTION("empty and truncated payloads")
    {
        CHECK_NOTHROW(hotstuff::decode_evidence_batch({}, limits));
        CHECK(hotstuff::decode_evidence_batch({}, limits).error ==
              EvidenceWireError::truncated);

        auto truncated = valid;
        truncated.pop_back();
        CHECK_NOTHROW(
            hotstuff::decode_evidence_batch(truncated, limits));
        CHECK(hotstuff::decode_evidence_batch(truncated, limits).error ==
              EvidenceWireError::truncated);
    }

    SECTION("trailing bytes")
    {
        auto trailing = valid;
        trailing.push_back(0);
        CHECK(hotstuff::decode_evidence_batch(trailing, limits).error ==
              EvidenceWireError::trailing_bytes);
    }

    SECTION("payload byte bound")
    {
        auto small = limits;
        small.maximum_payload_bytes = valid.size() - 1;
        CHECK(hotstuff::decode_evidence_batch(valid, small).error ==
              EvidenceWireError::payload_too_large);
    }

    SECTION("batch count bound is checked before allocation")
    {
        auto oversized = valid;
        overwrite_u32(oversized, 4, limits.maximum_observations + 1);
        CHECK(hotstuff::decode_evidence_batch(oversized, limits).error ==
              EvidenceWireError::batch_count_exceeded);
    }

    SECTION("signer count bound is checked before allocation")
    {
        auto oversized = valid;
        overwrite_u32(
            oversized,
            154,
            limits.maximum_signers_per_observation + 1);
        CHECK(hotstuff::decode_evidence_batch(oversized, limits).error ==
              EvidenceWireError::signer_count_exceeded);
    }
}

TEST_CASE("evidence encoder enforces canonical form and all wire bounds",
          "[e08][evidence][wire][encode][canonical][bounds][intentional-red]")
{
    EvidenceFixture fixture;
    const ResponseObservationBatch batch{
        hotstuff::kEvidenceBatchSchemaVersion,
        {aggregate_on_time(fixture, fixture.block_a)}};
    const auto wire_size = canonical_wire(batch).size();

    SECTION("payload bytes")
    {
        CHECK_THROWS(hotstuff::encode_evidence_batch(
            batch, EvidenceWireLimits{wire_size - 1, 4, 7}));
    }

    SECTION("observation count")
    {
        CHECK_THROWS(hotstuff::encode_evidence_batch(
            batch, EvidenceWireLimits{4096, 0, 7}));
    }

    SECTION("signer count")
    {
        CHECK_THROWS(hotstuff::encode_evidence_batch(
            batch, EvidenceWireLimits{4096, 4, 1}));
    }

    SECTION("batch schema")
    {
        auto invalid = batch;
        ++invalid.schema_version;
        CHECK_THROWS(hotstuff::encode_evidence_batch(
            invalid, EvidenceWireLimits{4096, 4, 7}));
    }

    SECTION("observation schema")
    {
        auto invalid = batch;
        ++invalid.observations.front().schema_version;
        CHECK_THROWS(hotstuff::encode_evidence_batch(
            invalid, EvidenceWireLimits{4096, 4, 7}));
    }

    SECTION("message type")
    {
        auto invalid = batch;
        invalid.observations.front().expected_message_type =
            static_cast<ExpectedMessageType>(0x7f);
        CHECK_THROWS(hotstuff::encode_evidence_batch(
            invalid, EvidenceWireLimits{4096, 4, 7}));
    }

    SECTION("outcome")
    {
        auto invalid = batch;
        invalid.observations.front().outcome =
            static_cast<ResponseOutcome>(0x7f);
        CHECK_THROWS(hotstuff::encode_evidence_batch(
            invalid, EvidenceWireLimits{4096, 4, 7}));
    }

    SECTION("signers must already be sorted and unique")
    {
        for (const auto &signers :
             std::vector<std::vector<ReplicaID>>{{4, 3}, {3, 3}})
        {
            auto invalid = batch;
            invalid.observations.front().signer_set = signers;
            CHECK_THROWS(hotstuff::encode_evidence_batch(
                invalid, EvidenceWireLimits{4096, 4, 7}));
        }
    }
}

TEST_CASE("evidence decoder requires unique sorted signer vectors",
          "[e08][evidence][wire][signers][intentional-red]")
{
    EvidenceFixture fixture;
    const EvidenceWireLimits limits{4096, 4, 7};

    SECTION("out of order")
    {
        auto value = aggregate_on_time(fixture, fixture.block_a);
        value.signer_set = {4, 3};
        const ResponseObservationBatch batch{
            hotstuff::kEvidenceBatchSchemaVersion, {value}};
        CHECK(hotstuff::decode_evidence_batch(
                  canonical_wire(batch), limits)
                  .error == EvidenceWireError::noncanonical_signer_set);
    }

    SECTION("duplicate")
    {
        auto value = aggregate_on_time(fixture, fixture.block_a);
        value.signer_set = {3, 3};
        const ResponseObservationBatch batch{
            hotstuff::kEvidenceBatchSchemaVersion, {value}};
        CHECK(hotstuff::decode_evidence_batch(
                  canonical_wire(batch), limits)
                  .error == EvidenceWireError::noncanonical_signer_set);
    }
}

TEST_CASE("ledger accepts authenticated exact admissible evidence",
          "[e08][evidence][ledger][accept][intentional-red]")
{
    EvidenceFixture fixture;
    EvidenceLedger ledger(
        fixture.epochs, fixture.window, generous_store_limits());
    const auto value = aggregate_on_time(fixture, fixture.block_a);

    ledger.ingest(AuthenticatedReporter{0}, value);

    const auto &accepted = only_accepted(ledger);
    CHECK(accepted.ingestion_sequence == 1);
    CHECK(accepted.observation.observation_id == value.observation_id);
    CHECK(accepted.observation.configuration == fixture.configuration);
    CHECK(ledger.rejected().empty());
    CHECK(ledger.high_watermark() == 1);
    CHECK(ledger.healthy());
}

TEST_CASE("ledger rejects zero reporter sequence before proposal state",
          "[e08][evidence][ledger][ordering][sequence]")
{
    EvidenceFixture fixture;

    SECTION("normal ingest rejects the first zero sequence")
    {
        EvidenceLedger ledger(
            fixture.epochs, fixture.window, generous_store_limits());
        ledger.ingest(
            AuthenticatedReporter{0},
            aggregate_on_time(fixture, fixture.block_a, 0, 1'000));
        CHECK(only_rejection(ledger) ==
              EvidenceRejectionReason::reporter_sequence_regression);
        CHECK(ledger.high_watermark() == 1);
        CHECK(ledger.healthy());
    }

    SECTION("proposal independent validation rejects zero")
    {
        EvidenceLedger ledger(
            fixture.epochs, fixture.window, generous_store_limits());
        const auto unknown_block = fixture_digest("zero-sequence-unknown");
        const auto value = aggregate_on_time(
            fixture, unknown_block, 0, 1'000);
        CHECK(ledger.ingest_if_proposal_independent_rejected(
            AuthenticatedReporter{0}, value));
        CHECK(only_rejection(ledger) ==
              EvidenceRejectionReason::reporter_sequence_regression);
        CHECK(ledger.high_watermark() == 1);
        CHECK(ledger.healthy());
    }
}

TEST_CASE("ledger applies deterministic trust and correlation rejection order",
          "[e08][evidence][ledger][rejection-order][intentional-red]")
{
    EvidenceFixture fixture;

    SECTION("schema precedes identity reporter and configuration")
    {
        EvidenceLedger ledger(
            fixture.epochs, fixture.window, generous_store_limits());
        auto value = aggregate_on_time(fixture, fixture.block_a);
        value.schema_version =
            hotstuff::kResponseObservationSchemaVersionV2 + 1;
        value.observation_id = fixture_digest("forged-id");
        value.reporter_id = 6;
        ++value.configuration.epoch_number;
        ledger.ingest(AuthenticatedReporter{5}, value);
        CHECK(only_rejection(ledger) ==
              EvidenceRejectionReason::unsupported_schema);
    }

    SECTION("identity precedes authenticated reporter")
    {
        EvidenceLedger ledger(
            fixture.epochs, fixture.window, generous_store_limits());
        auto value = aggregate_on_time(fixture, fixture.block_a);
        value.observation_id = fixture_digest("forged-id");
        ledger.ingest(AuthenticatedReporter{6}, value);
        CHECK(only_rejection(ledger) ==
              EvidenceRejectionReason::observation_id_mismatch);
    }

    SECTION("authenticated reporter precedes configuration and topology")
    {
        EvidenceLedger ledger(
            fixture.epochs, fixture.window, generous_store_limits());
        auto value = aggregate_on_time(fixture, fixture.block_a);
        value.reporter_id = 6;
        value.observed_replica_id = 5;
        ++value.configuration.epoch_number;
        value.observation_id = hotstuff::compute_response_observation_id(
            value.attempt_identity());
        ledger.ingest(AuthenticatedReporter{0}, value);
        CHECK(only_rejection(ledger) ==
              EvidenceRejectionReason::reporter_mismatch);
        REQUIRE(ledger.rejected().front().observation.has_value());
        CHECK(ledger.rejected().front().authenticated_reporter.replica_id ==
              0);
    }

    SECTION("configuration precedes window topology and semantic fields")
    {
        EvidenceLedger ledger(
            fixture.epochs, fixture.window, generous_store_limits());
        auto value = aggregate_on_time(fixture, fixture.block_a);
        ++value.configuration.epoch_number;
        value.block_hash = fixture_digest("unknown-block");
        value.observed_replica_id = 6;
        value.expected_message_type =
            static_cast<ExpectedMessageType>(0x7f);
        value.outcome = static_cast<ResponseOutcome>(0x7f);
        value.observation_id = hotstuff::compute_response_observation_id(
            value.attempt_identity());
        ledger.ingest(AuthenticatedReporter{0}, value);
        CHECK(only_rejection(ledger) ==
              EvidenceRejectionReason::unknown_configuration);
    }

    SECTION("window precedes topology and semantic fields")
    {
        EvidenceLedger ledger(
            fixture.epochs, fixture.window, generous_store_limits());
        auto value = aggregate_on_time(
            fixture, fixture_digest("unknown-block"));
        value.observed_replica_id = 6;
        value.expected_message_type =
            static_cast<ExpectedMessageType>(0x7f);
        value.outcome = static_cast<ResponseOutcome>(0x7f);
        value.observation_id = hotstuff::compute_response_observation_id(
            value.attempt_identity());
        ledger.ingest(AuthenticatedReporter{0}, value);
        CHECK(only_rejection(ledger) ==
              EvidenceRejectionReason::unknown_block);
    }

    SECTION("topology precedes type outcome timing and signers")
    {
        EvidenceLedger ledger(
            fixture.epochs, fixture.window, generous_store_limits());
        auto value = aggregate_on_time(fixture, fixture.block_a);
        value.observed_replica_id = 6;
        value.expected_message_type =
            static_cast<ExpectedMessageType>(0x7f);
        value.outcome = static_cast<ResponseOutcome>(0x7f);
        value.deadline_duration_us = 0;
        value.signer_set = {6, 6};
        value.observation_id = hotstuff::compute_response_observation_id(
            value.attempt_identity());
        ledger.ingest(AuthenticatedReporter{0}, value);
        CHECK(only_rejection(ledger) ==
              EvidenceRejectionReason::impossible_topology);
    }

    SECTION("type precedes outcome timing and signers")
    {
        EvidenceLedger ledger(
            fixture.epochs, fixture.window, generous_store_limits());
        auto value = aggregate_on_time(fixture, fixture.block_a);
        value.expected_message_type = ExpectedMessageType::direct_vote;
        value.outcome = static_cast<ResponseOutcome>(0x7f);
        value.deadline_duration_us = 0;
        value.signer_set = {2};
        value.observation_id = hotstuff::compute_response_observation_id(
            value.attempt_identity());
        ledger.ingest(AuthenticatedReporter{0}, value);
        CHECK(only_rejection(ledger) ==
              EvidenceRejectionReason::invalid_expected_message_type);
    }

    SECTION("outcome precedes timing and signers")
    {
        EvidenceLedger ledger(
            fixture.epochs, fixture.window, generous_store_limits());
        auto value = aggregate_on_time(fixture, fixture.block_a);
        value.outcome = static_cast<ResponseOutcome>(0x7f);
        value.deadline_duration_us = 0;
        value.signer_set = {2};
        ledger.ingest(AuthenticatedReporter{0}, value);
        CHECK(only_rejection(ledger) ==
              EvidenceRejectionReason::invalid_outcome);
    }

    SECTION("timing precedes signer and state validation")
    {
        EvidenceLedger ledger(
            fixture.epochs, fixture.window, generous_store_limits());
        auto value = aggregate_on_time(fixture, fixture.block_a);
        value.deadline_duration_us = 0;
        value.signer_set = {2};
        ledger.ingest(AuthenticatedReporter{0}, value);
        CHECK(only_rejection(ledger) ==
              EvidenceRejectionReason::invalid_timing);
    }

    SECTION("signers precede attempt transition and reporter ordering")
    {
        EvidenceLedger ledger(
            fixture.epochs, fixture.window, generous_store_limits());
        auto value = observation(
            fixture.configuration,
            fixture.block_a,
            0,
            1,
            ExpectedMessageType::aggregate_relay,
            ResponseOutcome::late,
            {2},
            1,
            0);
        ledger.ingest(AuthenticatedReporter{0}, value);
        CHECK(only_rejection(ledger) ==
              EvidenceRejectionReason::invalid_signer_set);
    }

    SECTION("reporter sequence precedes reporter timestamp")
    {
        EvidenceLedger ledger(
            fixture.epochs, fixture.window, generous_store_limits());
        ledger.ingest(
            AuthenticatedReporter{0},
            aggregate_on_time(fixture, fixture.block_a, 2, 2'000));
        ledger.ingest(
            AuthenticatedReporter{0},
            aggregate_on_time(fixture, fixture.block_b, 1, 1'000));
        REQUIRE(ledger.accepted().size() == 1);
        REQUIRE(ledger.rejected().size() == 1);
        CHECK(ledger.rejected().front().reason ==
              EvidenceRejectionReason::reporter_sequence_regression);
    }
}

TEST_CASE("ledger validates exact epoch tree digest and direct child",
          "[e08][evidence][ledger][topology][intentional-red]")
{
    EvidenceFixture fixture;

    const auto reject_configuration = [&](ConfigurationId configuration) {
        EvidenceLedger ledger(
            fixture.epochs, fixture.window, generous_store_limits());
        auto value = aggregate_on_time(fixture, fixture.block_a);
        value.configuration = std::move(configuration);
        value.observation_id = hotstuff::compute_response_observation_id(
            value.attempt_identity());
        fixture.window.set(
            value.proposal_key(), ProposalEvidenceStatus::admissible);
        ledger.ingest(AuthenticatedReporter{0}, value);
        return only_rejection(ledger);
    };

    auto wrong_epoch = fixture.configuration;
    ++wrong_epoch.epoch_number;
    CHECK(reject_configuration(wrong_epoch) ==
          EvidenceRejectionReason::unknown_configuration);

    auto wrong_tree = fixture.configuration;
    ++wrong_tree.tree_id;
    CHECK(reject_configuration(wrong_tree) ==
          EvidenceRejectionReason::unknown_configuration);

    auto wrong_digest = fixture.configuration;
    wrong_digest.epoch_digest = fixture_digest("wrong-epoch-digest");
    CHECK(reject_configuration(wrong_digest) ==
          EvidenceRejectionReason::unknown_configuration);

    EvidenceLedger ledger(
        fixture.epochs, fixture.window, generous_store_limits());
    auto impossible = aggregate_on_time(fixture, fixture.block_a);
    impossible.observed_replica_id = 6;
    impossible.signer_set = {6};
    impossible.observation_id = hotstuff::compute_response_observation_id(
        impossible.attempt_identity());
    ledger.ingest(AuthenticatedReporter{0}, impossible);
    CHECK(only_rejection(ledger) ==
          EvidenceRejectionReason::impossible_topology);
}

TEST_CASE("ledger uses the injected admissible stale and unknown block window",
          "[e08][evidence][ledger][window][intentional-red]")
{
    EvidenceFixture fixture;
    const auto stale_block = fixture_digest("stale-block");
    const auto unknown_block = fixture_digest("unknown-block");
    fixture.window.set(
        ProposalKey{fixture.configuration, stale_block},
        ProposalEvidenceStatus::stale);

    SECTION("stale")
    {
        EvidenceLedger ledger(
            fixture.epochs, fixture.window, generous_store_limits());
        ledger.ingest(
            AuthenticatedReporter{0},
            aggregate_on_time(fixture, stale_block));
        CHECK(only_rejection(ledger) ==
              EvidenceRejectionReason::stale_block);
    }

    SECTION("unknown")
    {
        EvidenceLedger ledger(
            fixture.epochs, fixture.window, generous_store_limits());
        ledger.ingest(
            AuthenticatedReporter{0},
            aggregate_on_time(fixture, unknown_block));
        CHECK(only_rejection(ledger) ==
              EvidenceRejectionReason::unknown_block);
    }

    SECTION("admissible")
    {
        EvidenceLedger ledger(
            fixture.epochs, fixture.window, generous_store_limits());
        ledger.ingest(
            AuthenticatedReporter{0},
            aggregate_on_time(fixture, fixture.block_a));
        CHECK(ledger.accepted().size() == 1);
        CHECK(ledger.rejected().empty());
    }
}

TEST_CASE("ledger derives expected message type from exact child role",
          "[e08][evidence][ledger][message-type][intentional-red]")
{
    EvidenceFixture fixture;

    SECTION("internal direct child relays an aggregate")
    {
        EvidenceLedger ledger(
            fixture.epochs, fixture.window, generous_store_limits());
        ledger.ingest(
            AuthenticatedReporter{0},
            aggregate_on_time(fixture, fixture.block_a));
        CHECK(ledger.accepted().size() == 1);
    }

    SECTION("leaf direct child sends a direct vote")
    {
        EvidenceLedger ledger(
            fixture.epochs, fixture.window, generous_store_limits());
        ledger.ingest(
            AuthenticatedReporter{1},
            leaf_on_time(fixture, fixture.block_a));
        CHECK(ledger.accepted().size() == 1);
    }

    SECTION("direct vote cannot describe an internal child")
    {
        EvidenceLedger ledger(
            fixture.epochs, fixture.window, generous_store_limits());
        auto value = aggregate_on_time(fixture, fixture.block_a);
        value.expected_message_type = ExpectedMessageType::direct_vote;
        value.signer_set = {1};
        value.observation_id = hotstuff::compute_response_observation_id(
            value.attempt_identity());
        ledger.ingest(AuthenticatedReporter{0}, value);
        CHECK(only_rejection(ledger) ==
              EvidenceRejectionReason::invalid_expected_message_type);
    }

    SECTION("aggregate relay cannot describe a leaf child")
    {
        EvidenceLedger ledger(
            fixture.epochs, fixture.window, generous_store_limits());
        auto value = leaf_on_time(fixture, fixture.block_a);
        value.expected_message_type = ExpectedMessageType::aggregate_relay;
        value.observation_id = hotstuff::compute_response_observation_id(
            value.attempt_identity());
        ledger.ingest(AuthenticatedReporter{1}, value);
        CHECK(only_rejection(ledger) ==
              EvidenceRejectionReason::invalid_expected_message_type);
    }

    SECTION("leader progress remains wire-reserved until its own topology lane")
    {
        EvidenceLedger ledger(
            fixture.epochs, fixture.window, generous_store_limits());
        auto value = aggregate_on_time(fixture, fixture.block_a);
        value.expected_message_type = ExpectedMessageType::leader_progress;
        value.observation_id = hotstuff::compute_response_observation_id(
            value.attempt_identity());
        ledger.ingest(AuthenticatedReporter{0}, value);
        CHECK(only_rejection(ledger) ==
              EvidenceRejectionReason::invalid_expected_message_type);
    }
}

TEST_CASE("ledger validates leaf aggregate and timeout signer semantics",
          "[e08][evidence][ledger][signers][intentional-red]")
{
    EvidenceFixture fixture;

    SECTION("leaf contribution is exactly the observed signer")
    {
        EvidenceLedger ledger(
            fixture.epochs, fixture.window, generous_store_limits());
        ledger.ingest(
            AuthenticatedReporter{1},
            leaf_on_time(fixture, fixture.block_a));
        CHECK(ledger.accepted().size() == 1);

        EvidenceLedger invalid(
            fixture.epochs, fixture.window, generous_store_limits());
        auto wrong = leaf_on_time(fixture, fixture.block_a);
        wrong.signer_set = {3, 4};
        invalid.ingest(AuthenticatedReporter{1}, wrong);
        CHECK(only_rejection(invalid) ==
              EvidenceRejectionReason::invalid_signer_set);
    }

    SECTION("aggregate is a nonempty sorted unique child-subtree subset")
    {
        for (const auto &valid_signers :
             std::vector<std::vector<ReplicaID>>{
                 {1}, {1, 3}, {3, 4}, {1, 3, 4}})
        {
            EvidenceLedger valid(
                fixture.epochs, fixture.window, generous_store_limits());
            auto contribution =
                aggregate_on_time(fixture, fixture.block_a);
            contribution.signer_set = valid_signers;
            valid.ingest(AuthenticatedReporter{0}, contribution);
            CHECK(valid.accepted().size() == 1);
            CHECK(valid.rejected().empty());
        }

        for (const auto &bad_signers :
             std::vector<std::vector<ReplicaID>>{
                 {}, {2}, {4, 3}, {3, 3}})
        {
            EvidenceLedger invalid(
                fixture.epochs, fixture.window, generous_store_limits());
            auto wrong = aggregate_on_time(fixture, fixture.block_a);
            wrong.signer_set = bad_signers;
            invalid.ingest(AuthenticatedReporter{0}, wrong);
            CHECK(only_rejection(invalid) ==
                  EvidenceRejectionReason::invalid_signer_set);
        }
    }

    SECTION("timeout carries no signer claim")
    {
        EvidenceLedger valid(
            fixture.epochs, fixture.window, generous_store_limits());
        auto timeout = observation(
            fixture.configuration,
            fixture.block_a,
            0,
            1,
            ExpectedMessageType::aggregate_relay,
            ResponseOutcome::timeout,
            {},
            1,
            1'000);
        valid.ingest(AuthenticatedReporter{0}, timeout);
        CHECK(valid.accepted().size() == 1);

        EvidenceLedger invalid(
            fixture.epochs, fixture.window, generous_store_limits());
        timeout.signer_set = {3};
        invalid.ingest(AuthenticatedReporter{0}, timeout);
        CHECK(only_rejection(invalid) ==
              EvidenceRejectionReason::invalid_signer_set);
    }
}

TEST_CASE("ledger treats outcomes as protocol transitions, not clock comparisons",
          "[e08][evidence][ledger][timing][intentional-red]")
{
    EvidenceFixture fixture;

    const auto rejection_for = [&](ResponseObservation value) {
        EvidenceLedger ledger(
            fixture.epochs, fixture.window, generous_store_limits());
        ledger.ingest(AuthenticatedReporter{value.reporter_id}, value);
        return only_rejection(ledger);
    };

    auto invalid_outcome = aggregate_on_time(fixture, fixture.block_a);
    invalid_outcome.outcome = static_cast<ResponseOutcome>(0x7f);
    CHECK(rejection_for(invalid_outcome) ==
          EvidenceRejectionReason::invalid_outcome);

    auto on_time_after_deadline =
        aggregate_on_time(fixture, fixture.block_a);
    on_time_after_deadline.response_duration_us = 101;
    {
        EvidenceLedger ledger(
            fixture.epochs, fixture.window, generous_store_limits());
        ledger.ingest(AuthenticatedReporter{0}, on_time_after_deadline);
        REQUIRE(ledger.accepted().size() == 1);
        CHECK(ledger.rejected().empty());
    }

    auto timeout_with_response = observation(
        fixture.configuration,
        fixture.block_a,
        0,
        1,
        ExpectedMessageType::aggregate_relay,
        ResponseOutcome::timeout,
        {},
        1,
        1'000);
    timeout_with_response.response_duration_us = 1;
    CHECK(rejection_for(timeout_with_response) ==
          EvidenceRejectionReason::invalid_timing);

    auto timeout = observation(
        fixture.configuration,
        fixture.block_a,
        0,
        1,
        ExpectedMessageType::aggregate_relay,
        ResponseOutcome::timeout,
        {},
        1,
        1'000);
    auto late_at_deadline = observation(
        fixture.configuration,
        fixture.block_a,
        0,
        1,
        ExpectedMessageType::aggregate_relay,
        ResponseOutcome::late,
        {3, 4},
        2,
        2'000);
    late_at_deadline.response_duration_us =
        late_at_deadline.deadline_duration_us;
    {
        EvidenceLedger ledger(
            fixture.epochs, fixture.window, generous_store_limits());
        ledger.ingest(AuthenticatedReporter{0}, timeout);
        ledger.ingest(AuthenticatedReporter{0}, late_at_deadline);
        REQUIRE(ledger.accepted().size() == 2);
        CHECK(ledger.rejected().empty());
    }

    auto late_before_deadline = late_at_deadline;
    --late_before_deadline.response_duration_us;
    {
        EvidenceLedger ledger(
            fixture.epochs, fixture.window, generous_store_limits());
        ledger.ingest(AuthenticatedReporter{0}, timeout);
        ledger.ingest(AuthenticatedReporter{0}, late_before_deadline);
        REQUIRE(ledger.accepted().size() == 1);
        REQUIRE(ledger.rejected().size() == 1);
        CHECK(ledger.rejected().front().reason ==
              EvidenceRejectionReason::invalid_timing);
    }

    for (const auto outcome :
         {ResponseOutcome::on_time,
          ResponseOutcome::timeout,
          ResponseOutcome::late})
    {
        auto zero_deadline = observation(
            fixture.configuration,
            fixture.block_a,
            0,
            1,
            ExpectedMessageType::aggregate_relay,
            outcome,
            outcome == ResponseOutcome::timeout
                ? std::vector<ReplicaID>{}
                : std::vector<ReplicaID>{3, 4},
            1,
            1'000);
        zero_deadline.deadline_duration_us = 0;
        CHECK(rejection_for(zero_deadline) ==
              EvidenceRejectionReason::invalid_timing);
    }
}

TEST_CASE("ledger accepts only timeout to late ordered attempt transitions",
          "[e08][evidence][ledger][state-machine][intentional-red]")
{
    EvidenceFixture fixture;

    auto timeout = observation(
        fixture.configuration,
        fixture.block_a,
        0,
        1,
        ExpectedMessageType::aggregate_relay,
        ResponseOutcome::timeout,
        {},
        1,
        1'000);
    auto late = observation(
        fixture.configuration,
        fixture.block_a,
        0,
        1,
        ExpectedMessageType::aggregate_relay,
        ResponseOutcome::late,
        {3, 4},
        2,
        2'000);
    REQUIRE(timeout.observation_id == late.observation_id);

    SECTION("timeout then late is one correlated attempt")
    {
        EvidenceLedger ledger(
            fixture.epochs, fixture.window, generous_store_limits());
        ledger.ingest(AuthenticatedReporter{0}, timeout);
        ledger.ingest(AuthenticatedReporter{0}, late);
        REQUIRE(ledger.accepted().size() == 2);
        CHECK(ledger.accepted()[0].observation.outcome ==
              ResponseOutcome::timeout);
        CHECK(ledger.accepted()[1].observation.outcome ==
              ResponseOutcome::late);
        CHECK(ledger.accepted()[0].observation.observation_id ==
              ledger.accepted()[1].observation.observation_id);
        CHECK(ledger.rejected().empty());
    }

    SECTION("same fact is a duplicate")
    {
        EvidenceLedger ledger(
            fixture.epochs, fixture.window, generous_store_limits());
        ledger.ingest(AuthenticatedReporter{0}, timeout);
        ledger.ingest(AuthenticatedReporter{0}, timeout);
        REQUIRE(ledger.accepted().size() == 1);
        REQUIRE(ledger.rejected().size() == 1);
        CHECK(ledger.rejected().front().reason ==
              EvidenceRejectionReason::duplicate_fact);
    }

    SECTION("late fact cannot rewrite the correlated attempt deadline")
    {
        EvidenceLedger ledger(
            fixture.epochs, fixture.window, generous_store_limits());
        auto ambiguous_late = late;
        ambiguous_late.deadline_duration_us = 200;
        ambiguous_late.response_duration_us = 250;
        ledger.ingest(AuthenticatedReporter{0}, timeout);
        ledger.ingest(AuthenticatedReporter{0}, ambiguous_late);
        REQUIRE(ledger.accepted().size() == 1);
        REQUIRE(ledger.rejected().size() == 1);
        CHECK(ledger.rejected().front().reason ==
              EvidenceRejectionReason::invalid_transition);
        CHECK(ledger.accepted().front().observation.deadline_duration_us ==
              100);
    }

    SECTION("late cannot arrive first")
    {
        EvidenceLedger ledger(
            fixture.epochs, fixture.window, generous_store_limits());
        ledger.ingest(AuthenticatedReporter{0}, late);
        CHECK(only_rejection(ledger) ==
              EvidenceRejectionReason::invalid_transition);
    }

    SECTION("on-time is terminal")
    {
        EvidenceLedger ledger(
            fixture.epochs, fixture.window, generous_store_limits());
        auto on_time = aggregate_on_time(fixture, fixture.block_a, 1, 1'000);
        auto later_timeout = timeout;
        later_timeout.reporter_sequence = 2;
        later_timeout.reporter_monotonic_ns = 2'000;
        ledger.ingest(AuthenticatedReporter{0}, on_time);
        ledger.ingest(AuthenticatedReporter{0}, later_timeout);
        REQUIRE(ledger.accepted().size() == 1);
        REQUIRE(ledger.rejected().size() == 1);
        CHECK(ledger.rejected().front().reason ==
              EvidenceRejectionReason::invalid_transition);
    }
}

TEST_CASE("ledger ordering is per reporter and invalid high values do not poison",
          "[e08][evidence][ledger][ordering][intentional-red]")
{
    EvidenceFixture fixture;
    EvidenceLedger ledger(
        fixture.epochs, fixture.window, generous_store_limits());

    ledger.ingest(
        AuthenticatedReporter{0},
        aggregate_on_time(fixture, fixture.block_a, 1, 1'000));

    auto invalid_high =
        aggregate_on_time(fixture, fixture.block_b, 999, 999'000);
    invalid_high.observed_replica_id = 6;
    invalid_high.signer_set = {6};
    invalid_high.observation_id = hotstuff::compute_response_observation_id(
        invalid_high.attempt_identity());
    ledger.ingest(AuthenticatedReporter{0}, invalid_high);
    REQUIRE(ledger.rejected().size() == 1);
    CHECK(ledger.rejected().back().reason ==
          EvidenceRejectionReason::impossible_topology);

    ledger.ingest(
        AuthenticatedReporter{0},
        aggregate_on_time(fixture, fixture.block_b, 2, 2'000));
    REQUIRE(ledger.accepted().size() == 2);

    ledger.ingest(
        AuthenticatedReporter{0},
        aggregate_on_time(fixture, fixture.block_c, 2, 3'000));
    REQUIRE(ledger.rejected().size() == 2);
    CHECK(ledger.rejected().back().reason ==
          EvidenceRejectionReason::reporter_sequence_regression);

    ledger.ingest(
        AuthenticatedReporter{0},
        aggregate_on_time(fixture, fixture.block_c, 3, 2'000));
    REQUIRE(ledger.accepted().size() == 3);
    CHECK(ledger.accepted().back().observation.reporter_monotonic_ns ==
          2'000);

    const auto block_d = fixture_digest("ordering-block-d");
    fixture.window.set(
        ProposalKey{fixture.configuration, block_d},
        ProposalEvidenceStatus::admissible);
    ledger.ingest(
        AuthenticatedReporter{0},
        aggregate_on_time(fixture, block_d, 4, 1'500));
    REQUIRE(ledger.rejected().size() == 3);
    CHECK(ledger.rejected().back().reason ==
          EvidenceRejectionReason::reporter_timestamp_regression);

    ledger.ingest(
        AuthenticatedReporter{1},
        leaf_on_time(fixture, fixture.block_c, 1, 5));
    REQUIRE(ledger.accepted().size() == 4);
    CHECK(ledger.accepted().back().observation.reporter_id == 1);
    CHECK(ledger.accepted().back().observation.reporter_monotonic_ns == 5);
}

TEST_CASE("ledger assigns manager ingestion sequence to accepted and rejected facts",
          "[e08][evidence][ledger][ingestion-sequence][intentional-red]")
{
    EvidenceFixture fixture;
    EvidenceLedger ledger(
        fixture.epochs, fixture.window, generous_store_limits());

    ledger.ingest(
        AuthenticatedReporter{0},
        aggregate_on_time(fixture, fixture.block_a));

    auto forged_reporter =
        aggregate_on_time(fixture, fixture.block_b, 2, 2'000);
    ledger.ingest(AuthenticatedReporter{6}, forged_reporter);
    ledger.reject_wire(
        AuthenticatedReporter{5}, EvidenceWireError::truncated);

    REQUIRE(ledger.accepted().size() == 1);
    REQUIRE(ledger.rejected().size() == 2);
    CHECK(ledger.accepted().front().ingestion_sequence == 1);
    CHECK(ledger.rejected()[0].ingestion_sequence == 2);
    CHECK(ledger.rejected()[0].reason ==
          EvidenceRejectionReason::reporter_mismatch);
    CHECK(ledger.rejected()[1].ingestion_sequence == 3);
    CHECK(ledger.rejected()[1].reason ==
          EvidenceRejectionReason::wire_error);
    CHECK_FALSE(ledger.rejected()[1].observation.has_value());
    REQUIRE(ledger.rejected()[1].wire_error.has_value());
    CHECK(*ledger.rejected()[1].wire_error ==
          EvidenceWireError::truncated);
    CHECK(ledger.rejected()[1].authenticated_reporter.replica_id == 5);
    CHECK(ledger.high_watermark() == 3);
    CHECK(ledger.healthy());
}

TEST_CASE("ledger stores are bounded and capacity loss marks evidence unhealthy",
          "[e08][evidence][ledger][capacity][intentional-red]")
{
    EvidenceFixture fixture;
    EvidenceLedger ledger(
        fixture.epochs,
        fixture.window,
        EvidenceStoreLimits{1, 1});

    ledger.ingest(
        AuthenticatedReporter{0},
        aggregate_on_time(fixture, fixture.block_a, 1, 1'000));
    CHECK(ledger.healthy());

    ledger.ingest(
        AuthenticatedReporter{0},
        aggregate_on_time(fixture, fixture.block_b, 2, 2'000));
    REQUIRE(ledger.accepted().size() == 1);
    REQUIRE(ledger.rejected().size() == 1);
    CHECK(ledger.rejected().front().reason ==
          EvidenceRejectionReason::accepted_capacity_exceeded);
    CHECK_FALSE(ledger.healthy());

    ledger.reject_wire(
        AuthenticatedReporter{0}, EvidenceWireError::truncated);
    CHECK(ledger.accepted().size() == 1);
    CHECK(ledger.rejected().size() == 1);
    CHECK(ledger.high_watermark() == 3);
    CHECK_FALSE(ledger.healthy());
}

TEST_CASE("rejected evidence overflow alone marks the ledger unhealthy",
          "[e08][evidence][ledger][capacity][rejected][intentional-red]")
{
    EvidenceFixture fixture;
    EvidenceLedger ledger(
        fixture.epochs,
        fixture.window,
        EvidenceStoreLimits{4, 1});

    ledger.reject_wire(
        AuthenticatedReporter{0}, EvidenceWireError::truncated);
    REQUIRE(ledger.rejected().size() == 1);
    CHECK(ledger.high_watermark() == 1);
    CHECK(ledger.healthy());

    ledger.reject_wire(
        AuthenticatedReporter{1}, EvidenceWireError::trailing_bytes);
    CHECK(ledger.accepted().empty());
    CHECK(ledger.rejected().size() == 1);
    CHECK(ledger.high_watermark() == 2);
    CHECK_FALSE(ledger.healthy());
}

TEST_CASE("accepted ingest allocation failures are fail closed and atomic",
          "[e08][evidence][ledger][exception-atomicity]"
          "[accepted][intentional-red]")
{
    constexpr std::size_t allocation_sweep = 32;
    std::size_t injected_failures = 0;
    std::vector<std::size_t> visible_partial_state;
    std::vector<std::size_t> inconsistent_retry;
    std::vector<std::size_t> lost_reporter_ordering;

    for (std::size_t allocation = 0;
         allocation < allocation_sweep;
         ++allocation)
    {
        EvidenceFixture fixture;
        EvidenceLedger ledger(
            fixture.epochs, fixture.window, generous_store_limits());
        const auto value = aggregate_on_time(
            fixture, fixture.block_a, 10, 10'000);
        const auto regressed = aggregate_on_time(
            fixture, fixture.block_b, 9, 9'000);

        const auto failed = throws_injected_bad_alloc(
            allocation,
            [&]() { ledger.ingest(AuthenticatedReporter{0}, value); });
        if (!failed)
            continue;
        ++injected_failures;

        if (ledger.healthy() || ledger.high_watermark() != 1 ||
            !ledger.accepted().empty() || !ledger.rejected().empty())
        {
            visible_partial_state.push_back(allocation);
        }

        ledger.ingest(AuthenticatedReporter{0}, value);
        const auto retry_is_consistent =
            !ledger.healthy() && ledger.high_watermark() == 2 &&
            ledger.accepted().size() == 1 &&
            ledger.rejected().empty() &&
            ledger.accepted().front().ingestion_sequence == 2;
        if (!retry_is_consistent)
            inconsistent_retry.push_back(allocation);

        ledger.ingest(AuthenticatedReporter{0}, regressed);
        const auto ordering_is_preserved =
            !ledger.healthy() && ledger.high_watermark() == 3 &&
            ledger.accepted().size() == 1 &&
            ledger.rejected().size() == 1 &&
            ledger.rejected().front().reason ==
                EvidenceRejectionReason::reporter_sequence_regression;
        if (!ordering_is_preserved)
            lost_reporter_ordering.push_back(allocation);
    }

    CAPTURE(injected_failures);
    CAPTURE(visible_partial_state);
    CAPTURE(inconsistent_retry);
    CAPTURE(lost_reporter_ordering);
    REQUIRE(injected_failures > 0);
    CHECK(visible_partial_state.empty());
    CHECK(inconsistent_retry.empty());
    CHECK(lost_reporter_ordering.empty());
}

TEST_CASE("rejected ingest allocation failures consume sequence and fail closed",
          "[e08][evidence][ledger][exception-atomicity]"
          "[rejected][intentional-red]")
{
    constexpr std::size_t allocation_sweep = 32;
    std::size_t injected_failures = 0;
    std::vector<std::size_t> missing_fail_closed_state;
    std::vector<std::size_t> inconsistent_retry;
    std::vector<std::size_t> poisoned_accepted_state;

    for (std::size_t allocation = 0;
         allocation < allocation_sweep;
         ++allocation)
    {
        EvidenceFixture fixture;
        EvidenceLedger ledger(
            fixture.epochs, fixture.window, generous_store_limits());
        const auto value = aggregate_on_time(
            fixture, fixture.block_a, 10, 10'000);
        const auto regressed = aggregate_on_time(
            fixture, fixture.block_b, 9, 9'000);

        const auto failed = throws_injected_bad_alloc(
            allocation,
            [&]() { ledger.ingest(AuthenticatedReporter{6}, value); });
        if (!failed)
            continue;
        ++injected_failures;

        if (ledger.healthy() || ledger.high_watermark() != 1 ||
            !ledger.accepted().empty() || !ledger.rejected().empty())
        {
            missing_fail_closed_state.push_back(allocation);
        }

        ledger.ingest(AuthenticatedReporter{6}, value);
        const auto retry_is_consistent =
            !ledger.healthy() && ledger.high_watermark() == 2 &&
            ledger.accepted().empty() &&
            ledger.rejected().size() == 1 &&
            ledger.rejected().front().ingestion_sequence == 2 &&
            ledger.rejected().front().reason ==
                EvidenceRejectionReason::reporter_mismatch;
        if (!retry_is_consistent)
            inconsistent_retry.push_back(allocation);

        ledger.ingest(AuthenticatedReporter{0}, value);
        ledger.ingest(AuthenticatedReporter{0}, regressed);
        const auto accepted_state_is_clean =
            !ledger.healthy() && ledger.high_watermark() == 4 &&
            ledger.accepted().size() == 1 &&
            ledger.accepted().front().ingestion_sequence == 3 &&
            ledger.rejected().size() == 2 &&
            ledger.rejected().back().reason ==
                EvidenceRejectionReason::reporter_sequence_regression;
        if (!accepted_state_is_clean)
            poisoned_accepted_state.push_back(allocation);
    }

    CAPTURE(injected_failures);
    CAPTURE(missing_fail_closed_state);
    CAPTURE(inconsistent_retry);
    CAPTURE(poisoned_accepted_state);
    REQUIRE(injected_failures > 0);
    CHECK(missing_fail_closed_state.empty());
    CHECK(inconsistent_retry.empty());
    CHECK(poisoned_accepted_state.empty());
}
