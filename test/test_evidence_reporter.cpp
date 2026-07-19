#include <cstddef>
#include <cstdint>
#include <cstdlib>
#include <fstream>
#include <functional>
#include <iterator>
#include <limits>
#include <memory>
#include <new>
#include <optional>
#include <stdexcept>
#include <string>
#include <type_traits>
#include <utility>
#include <vector>

#include "catch.hpp"
#include "hotstuff/response_attempt.h"

namespace evidence_reporter_allocation_failure
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

} // namespace evidence_reporter_allocation_failure

void *operator new(std::size_t size)
{
    if (evidence_reporter_allocation_failure::consume_failure())
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
 * E08 evidence reporter/outbox contract
 * -------------------------------------
 * One configured reporter consumes immutable ResponseAttemptFact values and
 * derives complete ResponseObservation envelopes. The trusted reporter ID and
 * prior sequence/timestamp are constructor state, never caller-controlled fact
 * fields. A bounded FIFO retains each exact envelope until transport accepts
 * it or records an auditable permanent failure.
 *
 * The fallback declares the complete public seam while the production header
 * is absent. Reporter methods remain deliberately undefined so this target
 * object-compiles but fails to link with an exact missing-production signal.
 */
#if __has_include("hotstuff/evidence_reporter.h")
#include "hotstuff/evidence_reporter.h"
#define KAURI_HAS_EVIDENCE_REPORTER_API 1
#else
#define KAURI_HAS_EVIDENCE_REPORTER_API 0

namespace hotstuff
{

constexpr std::uint32_t kEvidenceReportEnvelopeSchemaVersion = 1;

enum class EvidenceTransportResult : std::uint8_t
{
    accepted = 1,
    temporary_failure = 2,
    permanent_failure = 3,
};

struct EvidenceReportEnvelope
{
    std::uint32_t schema_version{
        kEvidenceReportEnvelopeSchemaVersion};
    ResponseObservation observation;
    bytearray_t canonical_payload;
};

struct PendingEvidenceReport
{
    EvidenceReportEnvelope envelope;
    std::uint64_t delivery_attempts{0};
    std::uint64_t temporary_failures{0};
    std::uint64_t callback_exceptions{0};
    bool permanently_failed{false};
};

struct EvidenceReporterLimits
{
    std::size_t maximum_pending_reports{4096};
};

struct EvidenceReporterConfig
{
    ReplicaID trusted_reporter_id{0};
    std::uint64_t initial_reporter_sequence{0};
    std::uint64_t initial_reporter_monotonic_ns{0};
    EvidenceReporterLimits limits;
    EvidenceWireLimits wire_limits;
};

struct EvidenceReporterDiagnostics
{
    std::size_t pending_reports{0};
    std::uint64_t last_reporter_sequence{0};
    std::uint64_t last_reporter_monotonic_ns{0};
    std::uint64_t accepted_reports{0};
    std::uint64_t temporary_failures{0};
    std::uint64_t permanent_failures{0};
    std::uint64_t rejected_facts{0};
    std::uint64_t capacity_failures{0};
    std::uint64_t payload_failures{0};
    std::uint64_t sequence_overflows{0};
    std::uint64_t timestamp_regressions{0};
    std::uint64_t allocation_failures{0};
    std::uint64_t callback_exceptions{0};
    bool healthy{true};
    bool stopped{false};
};

using EvidenceTransportCallback =
    std::function<EvidenceTransportResult(
        const EvidenceReportEnvelope &)>;

/**
 * A single-writer outbox. Calls must be externally serialized, and the
 * transport callback is non-reentrant with every reporter operation.
 */
class EvidenceReporter final
{
public:
    explicit EvidenceReporter(EvidenceReporterConfig config);
    ~EvidenceReporter();

    EvidenceReporter(const EvidenceReporter &) = delete;
    EvidenceReporter &operator=(const EvidenceReporter &) = delete;
    EvidenceReporter(EvidenceReporter &&) = delete;
    EvidenceReporter &operator=(EvidenceReporter &&) = delete;

    bool enqueue(const ResponseAttemptFact &fact);

    std::optional<EvidenceTransportResult> dispatch_one(
        const EvidenceTransportCallback &transport);

    const PendingEvidenceReport *front() const noexcept;
    std::size_t pending_size() const noexcept;
    EvidenceReporterDiagnostics diagnostics() const noexcept;
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

using hotstuff::ConfigurationId;
using hotstuff::EvidenceReportEnvelope;
using hotstuff::EvidenceReporter;
using hotstuff::EvidenceReporterConfig;
using hotstuff::EvidenceReporterDiagnostics;
using hotstuff::EvidenceReporterLimits;
using hotstuff::EvidenceTransportCallback;
using hotstuff::EvidenceTransportResult;
using hotstuff::EvidenceWireLimits;
using hotstuff::ExpectedMessageType;
using hotstuff::PendingEvidenceReport;
using hotstuff::ProposalKey;
using hotstuff::ReplicaID;
using hotstuff::ResponseAttemptFact;
using hotstuff::ResponseAttemptIdentity;
using hotstuff::ResponseAttemptKey;
using hotstuff::ResponseObservation;
using hotstuff::ResponseObservationBatch;
using hotstuff::ResponseOutcome;
using hotstuff::uint256_t;

constexpr std::uint64_t kDeadlineUs = 100;

uint256_t fixture_digest(const std::string &label)
{
    return hotstuff::DataStream(label).get_hash();
}

ConfigurationId configuration(
    std::uint32_t epoch,
    std::uint32_t tree,
    const std::string &label)
{
    return {epoch, tree, fixture_digest(label + "-epoch")};
}

ProposalKey proposal(
    std::uint32_t epoch,
    std::uint32_t tree,
    const std::string &label)
{
    return {
        configuration(epoch, tree, label),
        fixture_digest(label + "-block")};
}

ResponseAttemptFact fact(
    const std::string &label,
    ReplicaID observed_replica,
    ExpectedMessageType message_type,
    ResponseOutcome outcome,
    std::uint64_t fact_monotonic_ns,
    const std::vector<ReplicaID> &signers,
    std::uint32_t epoch = 20,
    std::uint32_t tree = 2)
{
    const auto response_duration =
        outcome == ResponseOutcome::timeout
            ? std::uint64_t{0}
            : outcome == ResponseOutcome::late
                  ? std::uint64_t{150}
                  : std::uint64_t{50};
    return {
        ResponseAttemptKey{
            proposal(epoch, tree, label),
            observed_replica,
            message_type},
        outcome,
        response_duration,
        kDeadlineUs,
        fact_monotonic_ns,
        signers};
}

EvidenceReporterLimits limits(
    std::size_t maximum_pending = 16)
{
    return {maximum_pending};
}

EvidenceWireLimits wire_limits(
    std::size_t maximum_payload = 1024 * 1024,
    std::uint32_t maximum_signers = 16,
    std::uint32_t maximum_observations = 1)
{
    return {
        maximum_payload,
        maximum_observations,
        maximum_signers};
}

EvidenceReporterConfig config(
    ReplicaID reporter = 7,
    std::uint64_t initial_sequence = 0,
    std::uint64_t initial_monotonic_ns = 0,
    EvidenceReporterLimits reporter_limits = limits(),
    EvidenceWireLimits reporter_wire_limits = wire_limits())
{
    return {
        reporter,
        initial_sequence,
        initial_monotonic_ns,
        reporter_limits,
        reporter_wire_limits};
}

bool same_fact(
    const ResponseAttemptFact &left,
    const ResponseAttemptFact &right)
{
    return left.key == right.key &&
           left.outcome == right.outcome &&
           left.response_duration_us == right.response_duration_us &&
           left.deadline_duration_us == right.deadline_duration_us &&
           left.fact_monotonic_ns == right.fact_monotonic_ns &&
           left.signer_set == right.signer_set;
}

bool same_observation(
    const ResponseObservation &left,
    const ResponseObservation &right)
{
    return left.schema_version == right.schema_version &&
           left.observation_id == right.observation_id &&
           left.reporter_id == right.reporter_id &&
           left.observed_replica_id == right.observed_replica_id &&
           left.configuration == right.configuration &&
           left.block_hash == right.block_hash &&
           left.expected_message_type == right.expected_message_type &&
           left.outcome == right.outcome &&
           left.response_duration_us == right.response_duration_us &&
           left.deadline_duration_us == right.deadline_duration_us &&
           left.reporter_monotonic_ns ==
               right.reporter_monotonic_ns &&
           left.reporter_sequence == right.reporter_sequence &&
           left.signer_set == right.signer_set;
}

bool same_envelope(
    const EvidenceReportEnvelope &left,
    const EvidenceReportEnvelope &right)
{
    return left.schema_version == right.schema_version &&
           same_observation(left.observation, right.observation) &&
           left.canonical_payload == right.canonical_payload;
}

struct InjectedCallResult
{
    bool threw_bad_alloc{false};
    bool returned_value{false};
};

template <typename Callable>
InjectedCallResult inject_allocation_failure(
    std::size_t successful_allocations,
    Callable &&callable)
{
    InjectedCallResult result;
    try
    {
        evidence_reporter_allocation_failure::OneShot fault(
            successful_allocations);
        result.returned_value = callable();
    }
    catch (const std::bad_alloc &)
    {
        result.threw_bad_alloc = true;
    }
    return result;
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

TEST_CASE("E08 reporter API pins one trusted fact-only outbox seam",
          "[e08][evidence-reporter][contract][intentional-red]")
{
    CHECK(KAURI_HAS_EVIDENCE_REPORTER_API == 1);
    CHECK(hotstuff::kEvidenceReportEnvelopeSchemaVersion == 1);
    CHECK(EvidenceReporterLimits{}.maximum_pending_reports > 0);
    CHECK(EvidenceWireLimits{}.maximum_payload_bytes > 0);
    CHECK(EvidenceWireLimits{}.maximum_observations > 0);
    CHECK(EvidenceWireLimits{}.maximum_signers_per_observation > 0);

    CHECK(static_cast<std::uint8_t>(
              EvidenceTransportResult::accepted) == 1);
    CHECK(static_cast<std::uint8_t>(
              EvidenceTransportResult::temporary_failure) == 2);
    CHECK(static_cast<std::uint8_t>(
              EvidenceTransportResult::permanent_failure) == 3);

    using EnqueueMethod =
        bool (EvidenceReporter::*)(const ResponseAttemptFact &);
    static_assert(
        std::is_same<
            decltype(&EvidenceReporter::enqueue),
            EnqueueMethod>::value,
        "the reporter must consume only immutable attempt facts");
    static_assert(
        !std::is_copy_constructible<EvidenceReporter>::value,
        "one reporter must own one ordered outbox");
    static_assert(
        !std::is_move_constructible<EvidenceReporter>::value,
        "transport callbacks must not outlive a moved owner");
    using FrontMethod =
        const PendingEvidenceReport *(EvidenceReporter::*)() const noexcept;
    static_assert(
        std::is_same<
            decltype(&EvidenceReporter::front),
            FrontMethod>::value,
        "pending state must be exposed only through a const view");
}

TEST_CASE("a fact becomes one complete trusted versioned envelope",
          "[e08][evidence-reporter][envelope][identity]"
          "[intentional-red]")
{
    EvidenceReporter reporter(config(9, 41, 900'000));
    const auto input = fact(
        "complete-envelope",
        4,
        ExpectedMessageType::aggregate_relay,
        ResponseOutcome::late,
        1'500'000,
        {4, 6, 8},
        22,
        3);
    const auto original = input;

    REQUIRE(reporter.enqueue(input));
    REQUIRE(reporter.pending_size() == 1);
    const auto *pending = reporter.front();
    REQUIRE(pending != nullptr);
    const auto &envelope = pending->envelope;
    const auto &observation = envelope.observation;

    CHECK(envelope.schema_version ==
          hotstuff::kEvidenceReportEnvelopeSchemaVersion);
    CHECK(observation.schema_version ==
          hotstuff::kResponseObservationSchemaVersion);
    CHECK(observation.reporter_id == 9);
    CHECK(observation.observed_replica_id ==
          input.key.observed_replica_id);
    CHECK(observation.configuration ==
          input.key.proposal.configuration);
    CHECK(observation.configuration.epoch_number == 22);
    CHECK(observation.configuration.tree_id == 3);
    CHECK(observation.block_hash == input.key.proposal.block_hash);
    CHECK(observation.expected_message_type ==
          input.key.expected_message_type);
    CHECK(observation.outcome == input.outcome);
    CHECK(observation.response_duration_us ==
          input.response_duration_us);
    CHECK(observation.deadline_duration_us ==
          input.deadline_duration_us);
    CHECK(observation.reporter_monotonic_ns ==
          input.fact_monotonic_ns);
    CHECK(observation.reporter_sequence == 42);
    CHECK(observation.signer_set == input.signer_set);
    CHECK(observation.observation_id ==
          hotstuff::compute_response_observation_id(
              ResponseAttemptIdentity{
                  9,
                  input.key.observed_replica_id,
                  input.key.proposal,
                  input.key.expected_message_type}));

    ResponseObservationBatch expected_batch;
    expected_batch.observations.push_back(observation);
    const auto expected_payload = hotstuff::encode_evidence_batch(
        expected_batch, wire_limits());
    CHECK(envelope.canonical_payload == expected_payload);
    const auto decoded = hotstuff::decode_evidence_batch(
        envelope.canonical_payload, wire_limits());
    REQUIRE(static_cast<bool>(decoded));
    REQUIRE(decoded.batch->observations.size() == 1);
    CHECK(same_observation(
        decoded.batch->observations.front(), observation));

    CHECK(pending->delivery_attempts == 0);
    CHECK(pending->temporary_failures == 0);
    CHECK(pending->callback_exceptions == 0);
    CHECK_FALSE(pending->permanently_failed);
    CHECK(same_fact(input, original));

    const auto diagnostics = reporter.diagnostics();
    CHECK(diagnostics.pending_reports == 1);
    CHECK(diagnostics.last_reporter_sequence == 42);
    CHECK(diagnostics.last_reporter_monotonic_ns == 1'500'000);
    CHECK(diagnostics.healthy);
    CHECK(reporter.healthy());
}

TEST_CASE("temporary transport failure retries the exact FIFO head",
          "[e08][evidence-reporter][fifo][retry][temporary]"
          "[intentional-red]")
{
    EvidenceReporter reporter(config(10, 100, 1'000'000));
    const auto first = fact(
        "fifo-first",
        2,
        ExpectedMessageType::direct_vote,
        ResponseOutcome::on_time,
        2'000'000,
        {2});
    const auto second = fact(
        "fifo-second",
        3,
        ExpectedMessageType::direct_vote,
        ResponseOutcome::on_time,
        3'000'000,
        {3});
    REQUIRE(reporter.enqueue(first));
    REQUIRE(reporter.enqueue(second));
    REQUIRE(reporter.front() != nullptr);
    const auto first_envelope = reporter.front()->envelope;

    std::vector<EvidenceReportEnvelope> seen;
    const EvidenceTransportCallback transport =
        [&](const EvidenceReportEnvelope &envelope) {
            seen.push_back(envelope);
            return seen.size() == 1
                       ? EvidenceTransportResult::temporary_failure
                       : EvidenceTransportResult::accepted;
        };

    const auto temporary = reporter.dispatch_one(transport);
    REQUIRE(temporary.has_value());
    CHECK(*temporary == EvidenceTransportResult::temporary_failure);
    REQUIRE(reporter.front() != nullptr);
    CHECK(same_envelope(
        reporter.front()->envelope, first_envelope));
    CHECK(reporter.front()->delivery_attempts == 1);
    CHECK(reporter.front()->temporary_failures == 1);
    CHECK(reporter.front()->callback_exceptions == 0);
    CHECK_FALSE(reporter.front()->permanently_failed);

    const auto first_success = reporter.dispatch_one(transport);
    REQUIRE(first_success.has_value());
    CHECK(*first_success == EvidenceTransportResult::accepted);
    REQUIRE(reporter.front() != nullptr);
    CHECK(reporter.front()->envelope.observation.block_hash ==
          second.key.proposal.block_hash);
    CHECK(reporter.front()->envelope.observation.reporter_sequence == 102);

    const auto second_success = reporter.dispatch_one(transport);
    REQUIRE(second_success.has_value());
    CHECK(*second_success == EvidenceTransportResult::accepted);
    CHECK(reporter.pending_size() == 0);
    CHECK(reporter.front() == nullptr);
    CHECK_FALSE(reporter.dispatch_one(transport).has_value());

    REQUIRE(seen.size() == 3);
    CHECK(same_envelope(seen.at(0), first_envelope));
    CHECK(same_envelope(seen.at(1), first_envelope));
    CHECK(seen.at(2).observation.block_hash ==
          second.key.proposal.block_hash);
    CHECK(seen.at(0).observation.reporter_sequence == 101);
    CHECK(seen.at(1).observation.reporter_sequence == 101);
    CHECK(seen.at(2).observation.reporter_sequence == 102);

    const auto diagnostics = reporter.diagnostics();
    CHECK(diagnostics.pending_reports == 0);
    CHECK(diagnostics.accepted_reports == 2);
    CHECK(diagnostics.temporary_failures == 1);
    CHECK(diagnostics.permanent_failures == 0);
    CHECK(diagnostics.last_reporter_sequence == 102);
    CHECK(reporter.healthy());
}

TEST_CASE("permanent transport rejection is retained as an auditable stop",
          "[e08][evidence-reporter][permanent][audit][fail-closed]"
          "[intentional-red]")
{
    EvidenceReporter reporter(config());
    const auto rejected = fact(
        "permanent-rejection",
        2,
        ExpectedMessageType::direct_vote,
        ResponseOutcome::on_time,
        1'000,
        {2});
    const auto queued_after = fact(
        "permanent-queued-after",
        3,
        ExpectedMessageType::direct_vote,
        ResponseOutcome::on_time,
        2'000,
        {3});
    REQUIRE(reporter.enqueue(rejected));
    REQUIRE(reporter.enqueue(queued_after));
    REQUIRE(reporter.front() != nullptr);
    const auto audit_copy = reporter.front()->envelope;

    std::size_t callback_count = 0;
    const EvidenceTransportCallback reject =
        [&](const EvidenceReportEnvelope &) {
            ++callback_count;
            return EvidenceTransportResult::permanent_failure;
        };
    const auto result = reporter.dispatch_one(reject);
    REQUIRE(result.has_value());
    CHECK(*result == EvidenceTransportResult::permanent_failure);

    CHECK_FALSE(reporter.healthy());
    CHECK(reporter.pending_size() == 2);
    REQUIRE(reporter.front() != nullptr);
    CHECK(same_envelope(reporter.front()->envelope, audit_copy));
    CHECK(reporter.front()->delivery_attempts == 1);
    CHECK(reporter.front()->temporary_failures == 0);
    CHECK(reporter.front()->callback_exceptions == 0);
    CHECK(reporter.front()->permanently_failed);

    const EvidenceTransportCallback must_not_run =
        [&](const EvidenceReportEnvelope &) {
            ++callback_count;
            return EvidenceTransportResult::accepted;
        };
    CHECK_FALSE(reporter.dispatch_one(must_not_run).has_value());
    CHECK(callback_count == 1);
    CHECK(reporter.pending_size() == 2);
    CHECK(same_envelope(reporter.front()->envelope, audit_copy));

    const auto diagnostics = reporter.diagnostics();
    CHECK(diagnostics.permanent_failures == 1);
    CHECK(diagnostics.accepted_reports == 0);
    CHECK_FALSE(diagnostics.healthy);
}

TEST_CASE("reporter ordering rejects sequence overflow and clock regression",
          "[e08][evidence-reporter][ordering][overflow][clock]"
          "[intentional-red]")
{
    SECTION("reporter sequence overflow is atomic and fail closed")
    {
        EvidenceReporter reporter(config(
            7,
            std::numeric_limits<std::uint64_t>::max() - 1,
            1'000));
        const auto input = fact(
            "sequence-overflow",
            2,
            ExpectedMessageType::direct_vote,
            ResponseOutcome::on_time,
            2'000,
            {2});

        CHECK_FALSE(reporter.enqueue(input));
        CHECK_FALSE(reporter.healthy());
        CHECK(reporter.pending_size() == 0);
        const auto diagnostics = reporter.diagnostics();
        CHECK(diagnostics.last_reporter_sequence ==
              std::numeric_limits<std::uint64_t>::max() - 1);
        CHECK(diagnostics.last_reporter_monotonic_ns == 1'000);
        CHECK(diagnostics.sequence_overflows == 1);
    }

    SECTION("monotonic timestamp regression consumes no sequence")
    {
        EvidenceReporter reporter(config(7, 0, 1'000));
        const auto first = fact(
            "timestamp-first",
            2,
            ExpectedMessageType::direct_vote,
            ResponseOutcome::on_time,
            2'000,
            {2});
        auto regressed = fact(
            "timestamp-regressed",
            3,
            ExpectedMessageType::direct_vote,
            ResponseOutcome::on_time,
            1'999,
            {3});
        REQUIRE(reporter.enqueue(first));

        CHECK_FALSE(reporter.enqueue(regressed));
        CHECK_FALSE(reporter.healthy());
        CHECK(reporter.pending_size() == 1);
        auto diagnostics = reporter.diagnostics();
        CHECK(diagnostics.last_reporter_sequence == 1);
        CHECK(diagnostics.last_reporter_monotonic_ns == 2'000);
        CHECK(diagnostics.timestamp_regressions == 1);

        regressed.fact_monotonic_ns = 2'000;
        REQUIRE(reporter.enqueue(regressed));
        CHECK(reporter.pending_size() == 2);
        const EvidenceTransportCallback accept =
            [](const EvidenceReportEnvelope &) {
                return EvidenceTransportResult::accepted;
            };
        REQUIRE(reporter.dispatch_one(accept).has_value());
        REQUIRE(reporter.front() != nullptr);
        CHECK(reporter.front()->envelope.observation.reporter_sequence == 2);
        CHECK(reporter.front()->envelope.observation.reporter_monotonic_ns ==
              2'000);
        CHECK_FALSE(reporter.healthy());
    }
}

TEST_CASE("reporter rejects malformed facts and every configured size bound",
          "[e08][evidence-reporter][validation][bounded][signers]"
          "[intentional-red]")
{
    const auto run_invalid_fact = [](
        ResponseAttemptFact invalid,
        EvidenceWireLimits configured_wire_limits = wire_limits()) {
        EvidenceReporter reporter(config(
            7, 0, 0, limits(), configured_wire_limits));
        CHECK_FALSE(reporter.enqueue(invalid));
        CHECK_FALSE(reporter.healthy());
        CHECK(reporter.pending_size() == 0);
        const auto diagnostics = reporter.diagnostics();
        CHECK(diagnostics.rejected_facts == 1);
        CHECK(diagnostics.last_reporter_sequence == 0);
        CHECK(diagnostics.last_reporter_monotonic_ns == 0);
    };

    SECTION("unsupported message type")
    {
        auto invalid = fact(
            "leader-progress",
            2,
            ExpectedMessageType::direct_vote,
            ResponseOutcome::on_time,
            1'000,
            {2});
        invalid.key.expected_message_type =
            ExpectedMessageType::leader_progress;
        run_invalid_fact(std::move(invalid));
    }

    SECTION("unsupported outcome")
    {
        auto invalid = fact(
            "unknown-outcome",
            2,
            ExpectedMessageType::direct_vote,
            ResponseOutcome::on_time,
            1'000,
            {2});
        invalid.outcome = static_cast<ResponseOutcome>(255);
        run_invalid_fact(std::move(invalid));
    }

    SECTION("zero deadline")
    {
        auto invalid = fact(
            "zero-deadline",
            2,
            ExpectedMessageType::direct_vote,
            ResponseOutcome::on_time,
            1'000,
            {2});
        invalid.deadline_duration_us = 0;
        run_invalid_fact(std::move(invalid));
    }

    SECTION("timeout carries neither response duration nor signers")
    {
        auto invalid = fact(
            "timeout-with-payload",
            2,
            ExpectedMessageType::direct_vote,
            ResponseOutcome::timeout,
            1'000,
            {});
        invalid.response_duration_us = 1;
        invalid.signer_set = {2};
        run_invalid_fact(std::move(invalid));
    }

    SECTION("late response reaches its deadline")
    {
        auto invalid = fact(
            "early-late",
            2,
            ExpectedMessageType::direct_vote,
            ResponseOutcome::late,
            1'000,
            {2});
        invalid.response_duration_us = invalid.deadline_duration_us - 1;
        run_invalid_fact(std::move(invalid));
    }

    SECTION("response signers are nonempty and canonical")
    {
        auto empty = fact(
            "empty-signers",
            2,
            ExpectedMessageType::aggregate_relay,
            ResponseOutcome::on_time,
            1'000,
            {});
        run_invalid_fact(std::move(empty));

        auto unordered = fact(
            "unordered-signers",
            2,
            ExpectedMessageType::aggregate_relay,
            ResponseOutcome::on_time,
            1'000,
            {3, 2});
        run_invalid_fact(std::move(unordered));

        auto duplicate = fact(
            "duplicate-signers",
            2,
            ExpectedMessageType::aggregate_relay,
            ResponseOutcome::on_time,
            1'000,
            {2, 2});
        run_invalid_fact(std::move(duplicate));
    }

    SECTION("direct vote signer is exactly the observed replica")
    {
        auto wrong = fact(
            "wrong-direct-signer",
            2,
            ExpectedMessageType::direct_vote,
            ResponseOutcome::on_time,
            1'000,
            {3});
        run_invalid_fact(std::move(wrong));

        auto extra = fact(
            "extra-direct-signer",
            2,
            ExpectedMessageType::direct_vote,
            ResponseOutcome::on_time,
            1'000,
            {2, 3});
        run_invalid_fact(std::move(extra));
    }

    SECTION("signer count is bounded")
    {
        auto invalid = fact(
            "signer-bound",
            2,
            ExpectedMessageType::aggregate_relay,
            ResponseOutcome::on_time,
            1'000,
            {2, 3, 4});
        run_invalid_fact(std::move(invalid), wire_limits(1024 * 1024, 2));
    }

    SECTION("canonical encoded payload is bounded")
    {
        EvidenceReporter reporter(config(
            7, 0, 0, limits(), wire_limits(1, 8)));
        const auto valid = fact(
            "payload-bound",
            2,
            ExpectedMessageType::direct_vote,
            ResponseOutcome::on_time,
            1'000,
            {2});
        CHECK_FALSE(reporter.enqueue(valid));
        CHECK_FALSE(reporter.healthy());
        CHECK(reporter.pending_size() == 0);
        const auto diagnostics = reporter.diagnostics();
        CHECK(diagnostics.payload_failures == 1);
        CHECK(diagnostics.last_reporter_sequence == 0);
        CHECK(diagnostics.last_reporter_monotonic_ns == 0);
    }
}

TEST_CASE("zero reporter limits construct a closed empty outbox",
          "[e08][evidence-reporter][limits][fail-closed]"
          "[intentional-red]")
{
    const auto input = fact(
        "invalid-limits",
        2,
        ExpectedMessageType::direct_vote,
        ResponseOutcome::on_time,
        1'000,
        {2});
    const auto run = [&](EvidenceReporterLimits invalid) {
        EvidenceReporter reporter(config(7, 0, 0, invalid, wire_limits()));
        CHECK_FALSE(reporter.healthy());
        CHECK_FALSE(reporter.enqueue(input));
        CHECK(reporter.pending_size() == 0);
    };

    SECTION("pending capacity")
    {
        run(limits(0));
    }
    SECTION("signer capacity")
    {
        EvidenceReporter reporter(config(
            7, 0, 0, limits(), wire_limits(1024, 0)));
        CHECK_FALSE(reporter.healthy());
        CHECK_FALSE(reporter.enqueue(input));
        CHECK(reporter.pending_size() == 0);
    }
    SECTION("payload capacity")
    {
        EvidenceReporter reporter(config(
            7, 0, 0, limits(), wire_limits(0, 8)));
        CHECK_FALSE(reporter.healthy());
        CHECK_FALSE(reporter.enqueue(input));
        CHECK(reporter.pending_size() == 0);
    }
    SECTION("observation capacity")
    {
        EvidenceReporter reporter(config(
            7, 0, 0, limits(), wire_limits(1024, 8, 0)));
        CHECK_FALSE(reporter.healthy());
        CHECK_FALSE(reporter.enqueue(input));
        CHECK(reporter.pending_size() == 0);
    }
}

TEST_CASE("queue capacity failure retains FIFO state and consumes no sequence",
          "[e08][evidence-reporter][capacity][atomic][fifo]"
          "[intentional-red]")
{
    EvidenceReporter reporter(config(7, 0, 0, limits(1)));
    const auto first = fact(
        "capacity-first",
        2,
        ExpectedMessageType::direct_vote,
        ResponseOutcome::on_time,
        1'000,
        {2});
    const auto second = fact(
        "capacity-second",
        3,
        ExpectedMessageType::direct_vote,
        ResponseOutcome::on_time,
        2'000,
        {3});
    REQUIRE(reporter.enqueue(first));
    REQUIRE(reporter.front() != nullptr);
    const auto retained = reporter.front()->envelope;

    CHECK_FALSE(reporter.enqueue(second));
    CHECK_FALSE(reporter.healthy());
    CHECK(reporter.pending_size() == 1);
    CHECK(same_envelope(reporter.front()->envelope, retained));
    auto diagnostics = reporter.diagnostics();
    CHECK(diagnostics.capacity_failures == 1);
    CHECK(diagnostics.last_reporter_sequence == 1);
    CHECK(diagnostics.last_reporter_monotonic_ns == 1'000);

    const EvidenceTransportCallback accept =
        [](const EvidenceReportEnvelope &) {
            return EvidenceTransportResult::accepted;
        };
    REQUIRE(reporter.dispatch_one(accept).has_value());
    REQUIRE(reporter.enqueue(second));
    REQUIRE(reporter.front() != nullptr);
    CHECK(reporter.front()->envelope.observation.reporter_sequence == 2);
    CHECK(reporter.front()->envelope.observation.block_hash ==
          second.key.proposal.block_hash);
    CHECK_FALSE(reporter.healthy());
}

TEST_CASE("transport exception preserves the exact retryable head",
          "[e08][evidence-reporter][callback][exception][atomic]"
          "[intentional-red]")
{
    EvidenceReporter reporter(config());
    const auto input = fact(
        "callback-exception",
        2,
        ExpectedMessageType::direct_vote,
        ResponseOutcome::on_time,
        1'000,
        {2});
    const auto queued_after = fact(
        "callback-queued-after",
        3,
        ExpectedMessageType::direct_vote,
        ResponseOutcome::on_time,
        2'000,
        {3});
    REQUIRE(reporter.enqueue(input));
    REQUIRE(reporter.enqueue(queued_after));
    REQUIRE(reporter.front() != nullptr);
    const auto retained = reporter.front()->envelope;

    const EvidenceTransportCallback throwing =
        [](const EvidenceReportEnvelope &) -> EvidenceTransportResult {
            throw std::runtime_error("transport callback failed");
        };
    CHECK_THROWS_AS(
        reporter.dispatch_one(throwing), std::runtime_error);

    CHECK_FALSE(reporter.healthy());
    CHECK(reporter.pending_size() == 2);
    REQUIRE(reporter.front() != nullptr);
    CHECK(same_envelope(reporter.front()->envelope, retained));
    CHECK(reporter.front()->delivery_attempts == 1);
    CHECK(reporter.front()->temporary_failures == 0);
    CHECK(reporter.front()->callback_exceptions == 1);
    CHECK_FALSE(reporter.front()->permanently_failed);
    CHECK(reporter.diagnostics().callback_exceptions == 1);

    std::vector<EvidenceReportEnvelope> retried;
    const EvidenceTransportCallback accept =
        [&](const EvidenceReportEnvelope &envelope) {
            retried.push_back(envelope);
            return EvidenceTransportResult::accepted;
        };
    const auto retry = reporter.dispatch_one(accept);
    REQUIRE(retry.has_value());
    CHECK(*retry == EvidenceTransportResult::accepted);
    REQUIRE(retried.size() == 1);
    CHECK(same_envelope(retried.front(), retained));
    CHECK(reporter.pending_size() == 1);
    REQUIRE(reporter.front() != nullptr);
    CHECK(reporter.front()->envelope.observation.block_hash ==
          queued_after.key.proposal.block_hash);
    CHECK_FALSE(reporter.healthy());
}

TEST_CASE("enqueue allocation failure is fail closed and sequence atomic",
          "[e08][evidence-reporter][allocation][atomic]"
          "[intentional-red]")
{
    constexpr std::size_t allocation_sweep = 32;
    std::size_t injected_failures = 0;
    std::vector<std::size_t> partial_state;
    std::vector<std::size_t> inconsistent_retry;

    for (std::size_t allocation = 0;
         allocation < allocation_sweep;
         ++allocation)
    {
        EvidenceReporter reporter(config(7, 7, 700));
        const auto input = fact(
            "allocation-" + std::to_string(allocation),
            2,
            ExpectedMessageType::aggregate_relay,
            ResponseOutcome::on_time,
            1'000,
            {2, 3, 4, 5});

        const auto result = inject_allocation_failure(
            allocation,
            [&]() { return reporter.enqueue(input); });
        const auto failed = result.threw_bad_alloc ||
                            (!result.returned_value &&
                             !reporter.healthy());
        if (!failed)
            continue;
        ++injected_failures;

        const auto diagnostics = reporter.diagnostics();
        if (reporter.healthy() || reporter.pending_size() != 0 ||
            diagnostics.last_reporter_sequence != 7 ||
            diagnostics.last_reporter_monotonic_ns != 700 ||
            diagnostics.allocation_failures != 1)
        {
            partial_state.push_back(allocation);
        }

        if (!reporter.enqueue(input) || reporter.pending_size() != 1 ||
            reporter.front() == nullptr || reporter.healthy() ||
            reporter.front()->envelope.observation.reporter_sequence != 8 ||
            reporter.front()->envelope.observation.reporter_monotonic_ns !=
                1'000)
        {
            inconsistent_retry.push_back(allocation);
        }
    }

    CAPTURE(injected_failures);
    CAPTURE(partial_state);
    CAPTURE(inconsistent_retry);
    REQUIRE(injected_failures > 0);
    CHECK(partial_state.empty());
    CHECK(inconsistent_retry.empty());
}

TEST_CASE("shutdown is inert and preserves pending diagnostic evidence",
          "[e08][evidence-reporter][shutdown][audit][inert]"
          "[intentional-red]")
{
    EvidenceReporter reporter(config());
    const auto first = fact(
        "shutdown-first",
        2,
        ExpectedMessageType::direct_vote,
        ResponseOutcome::on_time,
        1'000,
        {2});
    const auto second = fact(
        "shutdown-second",
        3,
        ExpectedMessageType::direct_vote,
        ResponseOutcome::on_time,
        2'000,
        {3});
    REQUIRE(reporter.enqueue(first));
    REQUIRE(reporter.enqueue(second));
    REQUIRE(reporter.front() != nullptr);
    const auto retained = reporter.front()->envelope;

    reporter.shutdown();
    reporter.shutdown();
    const auto diagnostics = reporter.diagnostics();
    CHECK(diagnostics.stopped);
    CHECK(diagnostics.healthy);
    CHECK(diagnostics.pending_reports == 2);
    CHECK(reporter.pending_size() == 2);
    CHECK(reporter.healthy());
    CHECK(same_envelope(reporter.front()->envelope, retained));

    const auto ignored = fact(
        "shutdown-ignored",
        4,
        ExpectedMessageType::direct_vote,
        ResponseOutcome::on_time,
        3'000,
        {4});
    CHECK_FALSE(reporter.enqueue(ignored));
    CHECK(reporter.pending_size() == 2);

    std::size_t callback_count = 0;
    const EvidenceTransportCallback transport =
        [&](const EvidenceReportEnvelope &) {
            ++callback_count;
            return EvidenceTransportResult::accepted;
        };
    CHECK_FALSE(reporter.dispatch_one(transport).has_value());
    CHECK(callback_count == 0);
    CHECK(reporter.pending_size() == 2);
    CHECK(same_envelope(reporter.front()->envelope, retained));
}

TEST_CASE("evidence reporter source is a pure injected outbox",
          "[e08][evidence-reporter][source-audit][pure]"
          "[intentional-red]")
{
    const auto header = read_source("include/hotstuff/evidence_reporter.h");
    const auto source = read_source("src/evidence_reporter.cpp");
    const auto implementation = header + "\n" + source;

    CHECK(header.find("ResponseAttemptFact") != std::string::npos);
    CHECK(header.find("EvidenceReportEnvelope") != std::string::npos);
    CHECK(header.find("PendingEvidenceReport") != std::string::npos);
    CHECK(header.find("EvidenceReporterDiagnostics") != std::string::npos);
    CHECK(header.find("EvidenceReporter") != std::string::npos);
    CHECK(header.find("EvidenceTransportCallback") != std::string::npos);
    CHECK(header.find("EvidenceWireLimits") != std::string::npos);
    CHECK(header.find("canonical_payload") != std::string::npos);
    CHECK(source.find("encode_evidence_batch") != std::string::npos);
    for (const auto *required_contract :
         {"single-writer", "externally serialized", "non-reentrant"})
    {
        INFO("Missing outbox ownership contract: " << required_contract);
        CHECK(header.find(required_contract) != std::string::npos);
    }

    INFO("report construction trusts no wall clock or source address");
    for (const auto *forbidden :
         {"system_clock",
          "steady_clock",
          "high_resolution_clock",
          "gettimeofday",
          "CLOCK_REALTIME",
          "time_t",
          "std::time(",
          "NetAddr",
          "sockaddr",
          "source_address",
          "remote_addr"})
    {
        CHECK(implementation.find(forbidden) == std::string::npos);
    }

    INFO("the pure outbox owns no network or runtime object");
    for (const auto *forbidden :
         {"PeerNetwork",
          "MsgNetwork",
          "EventContext",
          "send_msg(",
          "HotStuffBase",
          "HotStuffCore"})
    {
        CHECK(implementation.find(forbidden) == std::string::npos);
    }

    INFO("evidence reporting cannot mutate consensus or ranking state");
    for (const auto *forbidden :
         {"QuorumCert",
          "quorum_cert",
          "on_receive_vote",
          "add_verified_part",
          "TimerEvent",
          "schedule_after",
          "LeaderProgressMonitor",
          "rotate_active_view",
          "AdaptationSnapshot",
          "build_adaptation_snapshot",
          "EvidenceLedger"})
    {
        CHECK(implementation.find(forbidden) == std::string::npos);
    }
}
