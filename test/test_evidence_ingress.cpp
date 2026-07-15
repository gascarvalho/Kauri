#include <cstddef>
#include <cstdint>
#include <cstdlib>
#include <fstream>
#include <iterator>
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
#include "hotstuff/client.h"
#include "hotstuff/epoch_store.h"
#include "hotstuff/evidence.h"

namespace evidence_ingress_allocation_failure
{

constexpr std::size_t disabled = std::numeric_limits<std::size_t>::max();
thread_local std::size_t allocations_before_failure = disabled;
thread_local std::size_t rejected_allocation_minimum = disabled;
thread_local bool rejected_large_allocation = false;

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

bool consume_large_failure(std::size_t requested_size) noexcept
{
    if (rejected_allocation_minimum == disabled ||
        requested_size < rejected_allocation_minimum)
    {
        return false;
    }
    rejected_large_allocation = true;
    rejected_allocation_minimum = disabled;
    return true;
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

class RejectLargeAllocation final
{
public:
    explicit RejectLargeAllocation(std::size_t minimum_size) noexcept
    {
        rejected_allocation_minimum = minimum_size;
        rejected_large_allocation = false;
    }

    ~RejectLargeAllocation()
    {
        rejected_allocation_minimum = disabled;
    }

    bool triggered() const noexcept
    {
        return rejected_large_allocation;
    }

    RejectLargeAllocation(const RejectLargeAllocation &) = delete;
    RejectLargeAllocation &operator=(const RejectLargeAllocation &) = delete;
};

} // namespace evidence_ingress_allocation_failure

void *operator new(std::size_t size)
{
    if (evidence_ingress_allocation_failure::consume_large_failure(size))
        throw std::bad_alloc();
    if (evidence_ingress_allocation_failure::consume_failure())
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
 * E08 manager evidence ingress contract
 * -------------------------------------
 * The message is an opaque transport wrapper for the already-versioned
 * canonical evidence batch. It neither decodes observations nor establishes
 * reporter identity. A later manager transport integration derives the
 * AuthenticatedReporter from its configured mutual-TLS peer certificate and
 * passes that trusted value separately to this pure ingress seam.
 *
 * The ingress decodes with injected bounds and calls the existing ledger in
 * deterministic wire order. It is deliberately not a batch transaction:
 * completed ledger mutations remain visible if a later observation throws.
 * Reporters normally send one observation per message; batching does not
 * weaken these ordering or exception boundaries.
 */
#if __has_include("hotstuff/evidence_ingress.h")
#include "hotstuff/evidence_ingress.h"
#define KAURI_HAS_EVIDENCE_INGRESS_API 1
#else
#define KAURI_HAS_EVIDENCE_INGRESS_API 0

namespace hotstuff
{

struct MsgEvidenceReport
{
    static const opcode_t opcode = 0x12;
    DataStream serialized;

    explicit MsgEvidenceReport(const bytearray_t &canonical_payload);
    explicit MsgEvidenceReport(DataStream &&serialized_payload);
};

struct EvidenceIngressResult
{
    const std::size_t decoded_observations{0};
    const std::size_t accepted_observations{0};
    const std::size_t rejected_observations{0};
    const std::size_t wire_rejections{0};
    const std::uint64_t ledger_high_watermark{0};
    const std::optional<EvidenceWireError> wire_error;
};

/**
 * The caller derives AuthenticatedReporter from the configured mutual-TLS
 * certificate identity, never payload or source address. Calls are externally
 * serialized and non-reentrant with EvidenceLedger operations.
 */
class EvidenceIngress final
{
public:
    EvidenceIngress(
        EvidenceLedger &ledger,
        EvidenceWireLimits limits);
    ~EvidenceIngress();

    EvidenceIngress(const EvidenceIngress &) = delete;
    EvidenceIngress &operator=(const EvidenceIngress &) = delete;
    EvidenceIngress(EvidenceIngress &&) = delete;
    EvidenceIngress &operator=(EvidenceIngress &&) = delete;

    EvidenceIngressResult ingest(
        const AuthenticatedReporter &authenticated_reporter,
        const MsgEvidenceReport &message);

    EvidenceIngressResult ingest(
        const AuthenticatedReporter &authenticated_reporter,
        const bytearray_t &canonical_payload);

    bool healthy() const noexcept;

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
using hotstuff::EpochDefinitionInput;
using hotstuff::EpochStore;
using hotstuff::EpochTreeDefinition;
using hotstuff::EpochValidationContext;
using hotstuff::EvidenceIngress;
using hotstuff::EvidenceIngressResult;
using hotstuff::EvidenceLedger;
using hotstuff::EvidenceRejectionReason;
using hotstuff::EvidenceStoreLimits;
using hotstuff::EvidenceWireError;
using hotstuff::EvidenceWireLimits;
using hotstuff::ExpectedMessageType;
using hotstuff::MsgEvidenceReport;
using hotstuff::ProposalEvidenceStatus;
using hotstuff::ProposalEvidenceWindow;
using hotstuff::ProposalKey;
using hotstuff::ReplicaID;
using hotstuff::ResponseObservation;
using hotstuff::ResponseObservationBatch;
using hotstuff::ResponseOutcome;
using hotstuff::bytearray_t;
using hotstuff::opcode_t;
using hotstuff::uint256_t;

template <typename Error, typename = void>
struct has_allocation_failure : std::false_type
{};

template <typename Error>
struct has_allocation_failure<
    Error,
    std::void_t<decltype(Error::allocation_failure)>> : std::true_type
{};

template <typename Error, typename = void>
struct has_internal_failure : std::false_type
{};

template <typename Error>
struct has_internal_failure<
    Error,
    std::void_t<decltype(Error::internal_failure)>> : std::true_type
{};

template <typename Error>
bool is_allocation_failure(Error error) noexcept
{
    if constexpr (has_allocation_failure<Error>::value)
        return error == Error::allocation_failure;
    return false;
}

constexpr std::uint32_t kTreeId = 7;

const opcode_t *const evidence_opcode = &MsgEvidenceReport::opcode;
const opcode_t *const epoch_opcode = &hotstuff::MsgDeployEpoch::opcode;
const opcode_t *const reputation_opcode =
    &hotstuff::MsgDeployEpochReputation::opcode;

uint256_t fixture_digest(const std::string &label)
{
    return hotstuff::DataStream(label).get_hash();
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
    input.generation_seed = 0xE08;
    input.policy_version = "e08-ingress-policy-v1";
    input.evidence_snapshot_id = "e08-ingress-window-0001";
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

class MutableEvidenceWindow final : public ProposalEvidenceWindow
{
public:
    void admit(const ProposalKey &proposal)
    {
        statuses_[proposal] = ProposalEvidenceStatus::admissible;
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
    uint256_t block_a{fixture_digest("ingress-block-a")};
    uint256_t block_b{fixture_digest("ingress-block-b")};
    uint256_t block_c{fixture_digest("ingress-block-c")};

    EvidenceFixture()
    {
        const auto &epoch = epochs.stage(
            epoch_input(), validation_context());
        configuration = {
            0, kTreeId, epoch.epoch_digest()};
        window.admit({configuration, block_a});
        window.admit({configuration, block_b});
        window.admit({configuration, block_c});
    }
};

EvidenceWireLimits wire_limits(
    std::size_t maximum_payload = 4096,
    std::uint32_t maximum_observations = 8,
    std::uint32_t maximum_signers = 8)
{
    return {
        maximum_payload,
        maximum_observations,
        maximum_signers};
}

EvidenceStoreLimits store_limits()
{
    return {64, 64};
}

ResponseObservation observation(
    const EvidenceFixture &fixture,
    const uint256_t &block,
    std::uint64_t sequence,
    std::uint64_t monotonic_ns,
    ReplicaID reporter = 0)
{
    ResponseObservation value;
    value.reporter_id = reporter;
    value.observed_replica_id = 1;
    value.configuration = fixture.configuration;
    value.block_hash = block;
    value.expected_message_type =
        ExpectedMessageType::aggregate_relay;
    value.outcome = ResponseOutcome::on_time;
    value.response_duration_us = 50;
    value.deadline_duration_us = 100;
    value.reporter_monotonic_ns = monotonic_ns;
    value.reporter_sequence = sequence;
    value.signer_set = {3, 4};
    value.observation_id = hotstuff::compute_response_observation_id(
        value.attempt_identity());
    return value;
}

bytearray_t encode(
    const std::vector<ResponseObservation> &observations,
    EvidenceWireLimits limits = wire_limits())
{
    return hotstuff::encode_evidence_batch(
        ResponseObservationBatch{
            hotstuff::kEvidenceBatchSchemaVersion,
            observations},
        limits);
}

void overwrite_u32(
    bytearray_t &payload,
    std::size_t offset,
    std::uint32_t value)
{
    REQUIRE(offset + sizeof(value) <= payload.size());
    for (std::size_t index = 0; index < sizeof(value); ++index)
    {
        payload[offset + index] = static_cast<std::uint8_t>(
            value >> ((sizeof(value) - index - 1) * 8));
    }
}

void check_result(
    const EvidenceIngressResult &result,
    std::size_t decoded,
    std::size_t accepted,
    std::size_t rejected,
    std::size_t wire_rejected,
    std::uint64_t high_watermark,
    std::optional<EvidenceWireError> wire_error = std::nullopt)
{
    CHECK(result.decoded_observations == decoded);
    CHECK(result.accepted_observations == accepted);
    CHECK(result.rejected_observations == rejected);
    CHECK(result.wire_rejections == wire_rejected);
    CHECK(result.ledger_high_watermark == high_watermark);
    CHECK(result.wire_error == wire_error);
}

template <typename Callable>
bool throws_injected_bad_alloc(
    std::size_t successful_allocations,
    Callable &&callable)
{
    try
    {
        evidence_ingress_allocation_failure::OneShot fault(
            successful_allocations);
        callable();
    }
    catch (const std::bad_alloc &)
    {
        return true;
    }
    return false;
}

std::string read_source(const std::string &relative_path)
{
#ifdef KAURI_PROJECT_SOURCE_DIR
    const std::string root = KAURI_PROJECT_SOURCE_DIR;
#else
    const std::string root = ".";
#endif
    std::ifstream input(root + "/" + relative_path);
    REQUIRE(input.good());
    return std::string(
        std::istreambuf_iterator<char>(input),
        std::istreambuf_iterator<char>());
}

} // namespace

TEST_CASE("E08 ingress API pins an opaque authenticated manager seam",
          "[e08][evidence-ingress][contract][opcode][intentional-red]")
{
    CHECK(KAURI_HAS_EVIDENCE_INGRESS_API == 1);
    CHECK(*evidence_opcode == 0x12);
    CHECK(*epoch_opcode == 0x10);
    CHECK(*reputation_opcode == 0x11);
    CHECK(*evidence_opcode != *epoch_opcode);
    CHECK(*evidence_opcode != *reputation_opcode);
    CHECK(evidence_opcode != epoch_opcode);
    CHECK(evidence_opcode != reputation_opcode);

    static_assert(
        std::is_constructible<
            MsgEvidenceReport,
            const bytearray_t &>::value,
        "the message must copy immutable canonical payload bytes");
    static_assert(
        std::is_constructible<
            MsgEvidenceReport,
            hotstuff::DataStream &&>::value,
        "network receipt must move the exact serialized stream");
    static_assert(
        !std::is_copy_assignable<EvidenceIngressResult>::value,
        "ingress results expose immutable snapshot values");
    static_assert(
        !std::is_move_assignable<EvidenceIngressResult>::value,
        "ingress results cannot be rewritten after return");
    static_assert(
        !std::is_copy_constructible<EvidenceIngress>::value,
        "one ingress owns one ledger serialization boundary");
    static_assert(
        !std::is_move_constructible<EvidenceIngress>::value,
        "ingress references must keep a stable ledger owner");

    using MessageIngress = EvidenceIngressResult (EvidenceIngress::*)(
        const AuthenticatedReporter &,
        const MsgEvidenceReport &);
    using ByteIngress = EvidenceIngressResult (EvidenceIngress::*)(
        const AuthenticatedReporter &,
        const bytearray_t &);
    static_assert(
        std::is_same<
            decltype(static_cast<MessageIngress>(
                &EvidenceIngress::ingest)),
            MessageIngress>::value,
        "message ingress requires explicit authenticated identity");
    static_assert(
        std::is_same<
            decltype(static_cast<ByteIngress>(
                &EvidenceIngress::ingest)),
            ByteIngress>::value,
        "byte ingress requires explicit authenticated identity");
}

TEST_CASE("evidence message preserves exact opaque bytes without decoding",
          "[e08][evidence-ingress][message][opaque][roundtrip]"
          "[intentional-red]")
{
    EvidenceFixture fixture;
    const auto payload = encode({
        observation(fixture, fixture.block_a, 1, 1'000)});
    const auto original = payload;

    MsgEvidenceReport from_bytes(payload);
    CHECK(static_cast<bytearray_t>(from_bytes.serialized) == payload);
    CHECK(payload == original);

    hotstuff::DataStream stream(payload);
    MsgEvidenceReport from_stream(std::move(stream));
    CHECK(static_cast<bytearray_t>(from_stream.serialized) == payload);

    const bytearray_t malformed{0xA6, 0x02, 0xFF};
    MsgEvidenceReport opaque_malformed(malformed);
    CHECK(static_cast<bytearray_t>(opaque_malformed.serialized) ==
          malformed);
}

TEST_CASE("authenticated canonical message records one accepted observation",
          "[e08][evidence-ingress][accept][authenticated]"
          "[intentional-red]")
{
    EvidenceFixture fixture;
    EvidenceLedger ledger(
        fixture.epochs, fixture.window, store_limits());
    EvidenceIngress ingress(ledger, wire_limits());
    const auto value = observation(
        fixture, fixture.block_a, 1, 1'000);
    const auto payload = encode({value});
    MsgEvidenceReport message(payload);

    const auto result = ingress.ingest(
        AuthenticatedReporter{0}, message);

    check_result(result, 1, 1, 0, 0, 1);
    REQUIRE(ledger.accepted().size() == 1);
    CHECK(ledger.accepted().front().ingestion_sequence == 1);
    CHECK(ledger.accepted().front().observation.observation_id ==
          value.observation_id);
    CHECK(ledger.rejected().empty());
    CHECK(ingress.healthy());
    CHECK(static_cast<bytearray_t>(message.serialized) == payload);
}

TEST_CASE("payload reporter cannot replace authenticated transport identity",
          "[e08][evidence-ingress][authentication][mismatch]"
          "[intentional-red]")
{
    EvidenceFixture fixture;
    EvidenceLedger ledger(
        fixture.epochs, fixture.window, store_limits());
    EvidenceIngress ingress(ledger, wire_limits());
    const auto payload = encode({
        observation(fixture, fixture.block_a, 1, 1'000, 0)});

    const auto result = ingress.ingest(
        AuthenticatedReporter{6}, payload);

    check_result(result, 1, 0, 1, 0, 1);
    CHECK(ledger.accepted().empty());
    REQUIRE(ledger.rejected().size() == 1);
    CHECK(ledger.rejected().front().reason ==
          EvidenceRejectionReason::reporter_mismatch);
    CHECK(ledger.rejected().front().authenticated_reporter.replica_id ==
          6);
    REQUIRE(ledger.rejected().front().observation.has_value());
    CHECK(ledger.rejected().front().observation->reporter_id == 0);
    CHECK(ingress.healthy());
}

TEST_CASE("decoded observations are ingested deterministically in wire order",
          "[e08][evidence-ingress][batch][order][non-transactional]"
          "[intentional-red]")
{
    EvidenceFixture fixture;
    EvidenceLedger ledger(
        fixture.epochs, fixture.window, store_limits());
    EvidenceIngress ingress(ledger, wire_limits());

    const auto first = observation(
        fixture, fixture.block_a, 1, 1'000, 0);
    const auto forged = observation(
        fixture, fixture.block_b, 1, 1'500, 6);
    const auto third = observation(
        fixture, fixture.block_c, 2, 2'000, 0);
    const auto payload = encode({first, forged, third});

    const auto result = ingress.ingest(
        AuthenticatedReporter{0}, payload);

    check_result(result, 3, 2, 1, 0, 3);
    REQUIRE(ledger.accepted().size() == 2);
    REQUIRE(ledger.rejected().size() == 1);
    CHECK(ledger.accepted()[0].ingestion_sequence == 1);
    CHECK(ledger.accepted()[0].observation.block_hash == fixture.block_a);
    CHECK(ledger.rejected()[0].ingestion_sequence == 2);
    CHECK(ledger.rejected()[0].reason ==
          EvidenceRejectionReason::reporter_mismatch);
    CHECK(ledger.accepted()[1].ingestion_sequence == 3);
    CHECK(ledger.accepted()[1].observation.block_hash == fixture.block_c);
    CHECK(ingress.healthy());
}

TEST_CASE("ingress results are call deltas with a cumulative high watermark",
          "[e08][evidence-ingress][result][delta][high-watermark]"
          "[intentional-red]")
{
    EvidenceFixture fixture;
    EvidenceLedger ledger(
        fixture.epochs, fixture.window, store_limits());
    EvidenceIngress ingress(ledger, wire_limits());

    const auto accepted = ingress.ingest(
        AuthenticatedReporter{0},
        encode({observation(
            fixture, fixture.block_a, 1, 1'000, 0)}));
    check_result(accepted, 1, 1, 0, 0, 1);

    const auto rejected = ingress.ingest(
        AuthenticatedReporter{0},
        encode({observation(
            fixture, fixture.block_b, 1, 1'500, 6)}));
    check_result(rejected, 1, 0, 1, 0, 2);

    const auto wire_rejected = ingress.ingest(
        AuthenticatedReporter{0}, bytearray_t{});
    check_result(
        wire_rejected,
        0,
        0,
        0,
        1,
        3,
        EvidenceWireError::truncated);

    CHECK(ledger.accepted().size() == 1);
    CHECK(ledger.rejected().size() == 2);
    CHECK(ledger.high_watermark() == 3);
    CHECK(ledger.healthy());
    CHECK(ingress.healthy());
}

TEST_CASE("each malformed wire input records exactly one healthy rejection",
          "[e08][evidence-ingress][wire][bounded][audit]"
          "[intentional-red]")
{
    EvidenceFixture fixture;
    const auto valid = encode({
        observation(fixture, fixture.block_a, 1, 1'000)});

    const auto run = [&](
        const bytearray_t &payload,
        EvidenceWireLimits limits,
        EvidenceWireError expected_error) {
        EvidenceLedger ledger(
            fixture.epochs, fixture.window, store_limits());
        EvidenceIngress ingress(ledger, limits);
        const auto result = ingress.ingest(
            AuthenticatedReporter{5}, payload);

        check_result(
            result, 0, 0, 0, 1, 1, expected_error);
        CHECK(ledger.accepted().empty());
        REQUIRE(ledger.rejected().size() == 1);
        CHECK(ledger.rejected().front().ingestion_sequence == 1);
        CHECK(ledger.rejected().front().reason ==
              EvidenceRejectionReason::wire_error);
        CHECK(ledger.rejected().front().authenticated_reporter.replica_id ==
              5);
        REQUIRE(ledger.rejected().front().wire_error.has_value());
        CHECK(*ledger.rejected().front().wire_error == expected_error);
        CHECK(ledger.high_watermark() == 1);
        CHECK(ledger.healthy());
        CHECK(ingress.healthy());
    };

    SECTION("empty")
    {
        run({}, wire_limits(), EvidenceWireError::truncated);
    }
    SECTION("truncated")
    {
        auto truncated = valid;
        truncated.pop_back();
        run(truncated, wire_limits(), EvidenceWireError::truncated);
    }
    SECTION("trailing")
    {
        auto trailing = valid;
        trailing.push_back(0);
        run(trailing, wire_limits(), EvidenceWireError::trailing_bytes);
    }
    SECTION("payload bound")
    {
        auto bounded = wire_limits();
        bounded.maximum_payload_bytes = valid.size() - 1;
        run(valid, bounded, EvidenceWireError::payload_too_large);
    }
    SECTION("batch schema")
    {
        auto unsupported = valid;
        overwrite_u32(
            unsupported,
            0,
            hotstuff::kEvidenceBatchSchemaVersion + 1);
        run(
            unsupported,
            wire_limits(),
            EvidenceWireError::unsupported_batch_schema);
    }
    SECTION("observation schema")
    {
        auto unsupported = valid;
        overwrite_u32(
            unsupported,
            8,
            hotstuff::kResponseObservationSchemaVersion + 1);
        run(
            unsupported,
            wire_limits(),
            EvidenceWireError::unsupported_observation_schema);
    }
}

TEST_CASE("zero wire limits construct an unhealthy inert ingress",
          "[e08][evidence-ingress][limits][fail-closed]"
          "[intentional-red]")
{
    EvidenceFixture fixture;
    const auto payload = encode({
        observation(fixture, fixture.block_a, 1, 1'000)});

    const auto run = [&](EvidenceWireLimits invalid_limits) {
        EvidenceLedger ledger(
            fixture.epochs, fixture.window, store_limits());
        EvidenceIngress ingress(ledger, invalid_limits);
        CHECK_FALSE(ingress.healthy());

        const auto result = ingress.ingest(
            AuthenticatedReporter{0}, payload);
        check_result(result, 0, 0, 0, 0, 0);
        CHECK(ledger.high_watermark() == 0);
        CHECK(ledger.accepted().empty());
        CHECK(ledger.rejected().empty());
    };

    SECTION("payload bytes")
    {
        run(wire_limits(0, 8, 8));
    }
    SECTION("observations")
    {
        run(wire_limits(4096, 0, 8));
    }
    SECTION("signers")
    {
        run(wire_limits(4096, 8, 0));
    }
}

TEST_CASE("oversized evidence messages are rejected before payload copying",
          "[e08][rem-e08][evidence-ingress][message][bounded]"
          "[allocation][intentional-red]")
{
    constexpr std::size_t payload_size = 64 * 1024;
    constexpr std::size_t allocation_ceiling = payload_size / 2;
    EvidenceFixture fixture;
    EvidenceLedger ledger(
        fixture.epochs, fixture.window, store_limits());
    EvidenceIngress ingress(ledger, wire_limits(1024, 8, 8));

    bytearray_t payload(payload_size, 0xA5);
    MsgEvidenceReport message(
        hotstuff::DataStream(std::move(payload)));
    REQUIRE(message.serialized.size() == payload_size);

    std::optional<EvidenceIngressResult> result;
    bool threw_bad_alloc = false;
    bool attempted_oversized_allocation = false;
    {
        evidence_ingress_allocation_failure::RejectLargeAllocation guard(
            allocation_ceiling);
        try
        {
            result.emplace(ingress.ingest(
                AuthenticatedReporter{5}, message));
        }
        catch (const std::bad_alloc &)
        {
            threw_bad_alloc = true;
        }
        attempted_oversized_allocation = guard.triggered();
    }

    CHECK_FALSE(attempted_oversized_allocation);
    CHECK_FALSE(threw_bad_alloc);
    REQUIRE(result.has_value());
    check_result(
        *result,
        0,
        0,
        0,
        1,
        1,
        EvidenceWireError::payload_too_large);
    CHECK(ledger.accepted().empty());
    REQUIRE(ledger.rejected().size() == 1);
    CHECK(ledger.rejected().front().reason ==
          EvidenceRejectionReason::wire_error);
    CHECK(ledger.rejected().front().authenticated_reporter.replica_id == 5);
    REQUIRE(ledger.rejected().front().wire_error.has_value());
    CHECK(*ledger.rejected().front().wire_error ==
          EvidenceWireError::payload_too_large);
    CHECK(ledger.high_watermark() == 1);
    CHECK(ledger.healthy());
    CHECK(ingress.healthy());
}

TEST_CASE("decoder distinguishes allocation failures from hostile wire",
          "[e08][rem-e08][evidence][decode][allocation]"
          "[internal][intentional-red]")
{
    EvidenceFixture fixture;
    const auto payload = encode({
        observation(fixture, fixture.block_a, 1, 1'000),
        observation(fixture, fixture.block_b, 2, 2'000)});

    CHECK(has_allocation_failure<EvidenceWireError>::value);
    CHECK(has_internal_failure<EvidenceWireError>::value);

    const auto run = [&](std::size_t allocation, const char *reserve_path) {
        CAPTURE(allocation);
        INFO("decoder allocation path: " << reserve_path);
        hotstuff::EvidenceDecodeResult decoded;
        {
            evidence_ingress_allocation_failure::OneShot fault(allocation);
            decoded = hotstuff::decode_evidence_batch(
                payload, wire_limits());
        }

        CHECK_FALSE(decoded);
        CHECK(is_allocation_failure(decoded.error));
        CHECK(decoded.error != EvidenceWireError::payload_too_large);
        CHECK_FALSE(decoded.batch.has_value());
    };

    SECTION("batch observation storage reserve")
    {
        run(0, "batch.observations.reserve");
    }
    SECTION("first observation signer storage reserve")
    {
        run(1, "first observation.signer_set.reserve");
    }
    SECTION("later observation signer storage reserve")
    {
        run(2, "later observation.signer_set.reserve");
    }
}

TEST_CASE("ingress propagates decoder allocation failures without wire audit",
          "[e08][rem-e08][evidence-ingress][decode][allocation]"
          "[unhealthy][intentional-red]")
{
    EvidenceFixture fixture;
    const auto payload = encode({
        observation(fixture, fixture.block_a, 1, 1'000),
        observation(fixture, fixture.block_b, 2, 2'000)});

    const auto run = [&](std::size_t allocation, const char *reserve_path) {
        CAPTURE(allocation);
        INFO("ingress allocation path: " << reserve_path);
        EvidenceLedger ledger(
            fixture.epochs, fixture.window, store_limits());
        EvidenceIngress ingress(ledger, wire_limits());

        const auto propagated = throws_injected_bad_alloc(
            allocation,
            [&]() {
                ingress.ingest(AuthenticatedReporter{0}, payload);
            });

        CHECK(propagated);
        CHECK_FALSE(ingress.healthy());
        CHECK(ledger.healthy());
        CHECK(ledger.high_watermark() == 0);
        CHECK(ledger.accepted().empty());
        CHECK(ledger.rejected().empty());
    };

    SECTION("batch observation storage reserve")
    {
        run(0, "batch.observations.reserve");
    }
    SECTION("first observation signer storage reserve")
    {
        run(1, "first observation.signer_set.reserve");
    }
    SECTION("later observation signer storage reserve")
    {
        run(2, "later observation.signer_set.reserve");
    }
}

TEST_CASE("allocation exceptions expose only decoder or ledger prefixes",
          "[e08][evidence-ingress][allocation][exception]"
          "[non-transactional][intentional-red]")
{
    constexpr std::size_t allocation_sweep = 96;
    EvidenceFixture fixture;
    const auto first = observation(
        fixture, fixture.block_a, 1, 1'000);
    const auto second = observation(
        fixture, fixture.block_b, 2, 2'000);
    const auto payload = encode({first, second});

    std::size_t injected_failures = 0;
    std::size_t empty_prefix_failures = 0;
    std::size_t visible_prefix_failures = 0;
    std::vector<std::size_t> decoder_failure_indices;
    std::vector<std::size_t> inconsistent_state;

    for (std::size_t allocation = 0;
         allocation < allocation_sweep;
         ++allocation)
    {
        EvidenceLedger ledger(
            fixture.epochs, fixture.window, store_limits());
        EvidenceIngress ingress(ledger, wire_limits());

        const auto failed = throws_injected_bad_alloc(
            allocation,
            [&]() {
                ingress.ingest(
                    AuthenticatedReporter{0}, payload);
            });
        if (!failed)
            continue;
        ++injected_failures;

        const auto accepted = ledger.accepted().size();
        const bool empty_prefix =
            accepted == 0 && ledger.high_watermark() == 1;
        const bool visible_prefix =
            accepted == 1 && ledger.high_watermark() == 2 &&
            ledger.accepted().front().ingestion_sequence == 1 &&
            ledger.accepted().front().observation.block_hash ==
                fixture.block_a;
        const bool decoder_failure =
            allocation <= 2 && !ingress.healthy() && ledger.healthy() &&
            accepted == 0 && ledger.high_watermark() == 0 &&
            ledger.rejected().empty();
        const bool ledger_prefix_failure =
            allocation > 2 && !ingress.healthy() && !ledger.healthy() &&
            ledger.rejected().empty() &&
            (empty_prefix || visible_prefix);
        if (decoder_failure)
            decoder_failure_indices.push_back(allocation);
        if (empty_prefix)
            ++empty_prefix_failures;
        if (visible_prefix)
            ++visible_prefix_failures;

        if (!decoder_failure && !ledger_prefix_failure)
            inconsistent_state.push_back(allocation);
    }

    const std::vector<std::size_t> expected_decoder_failures{0, 1, 2};
    CAPTURE(injected_failures);
    CAPTURE(empty_prefix_failures);
    CAPTURE(visible_prefix_failures);
    CAPTURE(decoder_failure_indices);
    CAPTURE(inconsistent_state);
    REQUIRE(injected_failures > 0);
    CHECK(decoder_failure_indices == expected_decoder_failures);
    REQUIRE(empty_prefix_failures > 0);
    REQUIRE(visible_prefix_failures > 0);
    CHECK(inconsistent_state.empty());
}

TEST_CASE("reject wire exceptions make ingress unhealthy and rethrow",
          "[e08][evidence-ingress][wire][allocation][exception]"
          "[intentional-red]")
{
    constexpr std::size_t allocation_sweep = 16;
    std::size_t injected_failures = 0;
    std::vector<std::size_t> inconsistent_state;

    for (std::size_t allocation = 0;
         allocation < allocation_sweep;
         ++allocation)
    {
        EvidenceFixture fixture;
        EvidenceLedger ledger(
            fixture.epochs, fixture.window, store_limits());
        EvidenceIngress ingress(ledger, wire_limits());

        const auto failed = throws_injected_bad_alloc(
            allocation,
            [&]() {
                ingress.ingest(
                    AuthenticatedReporter{0}, bytearray_t{});
            });
        if (!failed)
            continue;
        ++injected_failures;

        if (ingress.healthy() || ledger.healthy() ||
            ledger.high_watermark() != 1 ||
            !ledger.accepted().empty() ||
            !ledger.rejected().empty())
        {
            inconsistent_state.push_back(allocation);
        }
    }

    CAPTURE(injected_failures);
    CAPTURE(inconsistent_state);
    REQUIRE(injected_failures > 0);
    CHECK(inconsistent_state.empty());
}

TEST_CASE("evidence ingress source pins the later TLS trust boundary only",
          "[e08][rem-e08][evidence-ingress][source-audit][pure]"
          "[intentional-red]")
{
    const auto evidence_header = read_source("include/hotstuff/evidence.h");
    const auto evidence_source = read_source("src/evidence.cpp");
    const auto header = read_source("include/hotstuff/evidence_ingress.h");
    const auto source = read_source("src/evidence_ingress.cpp");
    const auto implementation = header + "\n" + source;

    CHECK(header.find("MsgEvidenceReport") != std::string::npos);
    CHECK(header.find("EvidenceIngressResult") != std::string::npos);
    CHECK(header.find("EvidenceIngress") != std::string::npos);
    CHECK(header.find("EvidenceLedger") != std::string::npos);
    CHECK(header.find("EvidenceWireLimits") != std::string::npos);
    CHECK(header.find("AuthenticatedReporter") != std::string::npos);
    CHECK(header.find("DataStream serialized") != std::string::npos);
    CHECK(source.find("MsgEvidenceReport::opcode") != std::string::npos);
    CHECK(source.find("decode_evidence_batch") != std::string::npos);
    CHECK(source.find("reject_wire") != std::string::npos);

    INFO("decoder internal failures are never attributed to hostile wire");
    CHECK(evidence_header.find("allocation_failure") != std::string::npos);
    CHECK(evidence_header.find("internal_failure") != std::string::npos);
    CHECK(evidence_source.find("EvidenceWireError::allocation_failure") !=
          std::string::npos);
    CHECK(evidence_source.find("EvidenceWireError::internal_failure") !=
          std::string::npos);
    CHECK(source.find("std::bad_alloc") != std::string::npos);
    CHECK(source.find("std::runtime_error") != std::string::npos);

    INFO("transport authentication is an explicit later integration boundary");
    for (const auto *required_contract :
         {"opaque",
          "configured mutual-TLS certificate identity",
          "never payload or source address",
          "externally serialized",
          "non-reentrant",
          "deterministically in wire order",
          "not batch-transactional"})
    {
        INFO("Missing ingress contract phrase: " << required_contract);
        CHECK(header.find(required_contract) != std::string::npos);
    }

    INFO("the pure ingress neither authenticates sockets nor owns a network");
    for (const auto *forbidden :
         {"PeerNetwork",
          "MsgNetwork",
          "EventContext",
          "NetAddr",
          "PeerId",
          "sockaddr",
          "source_address",
          "remote_addr",
          "send_msg(",
          "SSL_",
          "X509"})
    {
        CHECK(implementation.find(forbidden) == std::string::npos);
    }

    INFO("ingress cannot mutate consensus adaptation or timer state");
    for (const auto *forbidden :
         {"HotStuffBase",
          "HotStuffCore",
          "QuorumCert",
          "on_receive_vote",
          "add_verified_part",
          "TimerEvent",
          "schedule_after",
          "LeaderProgressMonitor",
          "rotate_active_view",
          "AdaptationSnapshot",
          "build_adaptation_snapshot"})
    {
        CHECK(implementation.find(forbidden) == std::string::npos);
    }

    INFO("ingress trusts no local wall clock");
    for (const auto *forbidden :
         {"system_clock",
          "steady_clock",
          "high_resolution_clock",
          "gettimeofday",
          "CLOCK_REALTIME",
          "std::time("})
    {
        CHECK(implementation.find(forbidden) == std::string::npos);
    }
}
