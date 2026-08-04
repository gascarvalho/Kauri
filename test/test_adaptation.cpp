#include <algorithm>
#include <cstddef>
#include <cstdint>
#include <fstream>
#include <limits>
#include <map>
#include <optional>
#include <stdexcept>
#include <string>
#include <type_traits>
#include <utility>
#include <vector>

#include "catch.hpp"
#include "hotstuff/evidence.h"
#include "hotstuff/epoch_store.h"

/*
 * R09 pure adaptation contract
 * ----------------------------
 * The policy consumes only fixed membership, an exact current-epoch identity,
 * E08-accepted evidence through a bounded view, an ingestion cutoff, policy,
 * and an echoed deterministic seed.  It cannot inspect tree placement,
 * process profiles, crash lists, networking, or manager state.
 *
 * Timeout followed by late is one attempt.  A response is on-time or late;
 * timeout incidence includes timeout-only and late attempts.  The bounded
 * attempt window is selected by first-ingestion sequence before metrics are
 * calculated.  Rates use floor integer parts-per-million and latency uses the
 * configured nearest-rank percentile.
 *
 * The fallback declares the complete public seam so this tests-first target
 * compiles while adaptation.h is absent.  build_adaptation_snapshot remains
 * deliberately undefined, producing a precise intentional-red link failure.
 */
#if __has_include("hotstuff/adaptation.h")
#include "hotstuff/adaptation.h"
#define KAURI_HAS_R09_ADAPTATION_API 1
#else
#define KAURI_HAS_R09_ADAPTATION_API 0

namespace hotstuff
{

constexpr std::uint32_t kAdaptationSchemaVersion = 1;
constexpr std::uint32_t kRatePpmScale = 1'000'000;
constexpr std::size_t kMaximumAdaptationMembers = 65'536;
constexpr std::size_t kMaximumAdaptationEvidenceRecords = 1'048'576;
constexpr std::uint32_t kMaximumAdaptationAttemptWindow = 4'096;
constexpr std::size_t kMaximumAdaptationPolicyVersionBytes = 64;
constexpr std::uint16_t kPercentileBasisPointScale = 10'000;

using RatePpm = std::uint32_t;

enum class ResponsivenessClass : std::uint8_t
{
    responsive = 1,
    insufficient_evidence = 2,
    nonresponsive = 3,
};

enum class ResponsivenessReason : std::uint8_t
{
    insufficient_attempts = 1,
    response_rate_below_minimum = 2,
    timeout_rate_above_maximum = 3,
    persistent_timeout_streak = 4,
};

struct AdaptationEpochId
{
    std::uint32_t epoch_number{0};
    uint256_t epoch_digest;

    bool operator==(const AdaptationEpochId &other) const noexcept
    {
        return epoch_number == other.epoch_number &&
               epoch_digest == other.epoch_digest;
    }

    bool operator!=(const AdaptationEpochId &other) const noexcept
    {
        return !(*this == other);
    }
};

struct AcceptedEvidenceView
{
    const AcceptedEvidenceRecord *data{nullptr};
    std::size_t size{0};
};

struct AdaptationPolicy
{
    std::uint32_t schema_version{kAdaptationSchemaVersion};
    std::string policy_version{"kauri-responsiveness-v1"};
    std::uint32_t attempt_window{32};
    std::uint32_t minimum_attempts{8};
    RatePpm minimum_response_rate_ppm{750'000};
    RatePpm maximum_timeout_rate_ppm{250'000};
    std::uint32_t trailing_timeout_streak{3};
    std::uint16_t latency_percentile_basis_points{5'000};

    bool operator==(const AdaptationPolicy &other) const noexcept
    {
        return schema_version == other.schema_version &&
               policy_version == other.policy_version &&
               attempt_window == other.attempt_window &&
               minimum_attempts == other.minimum_attempts &&
               minimum_response_rate_ppm ==
                   other.minimum_response_rate_ppm &&
               maximum_timeout_rate_ppm ==
                   other.maximum_timeout_rate_ppm &&
               trailing_timeout_streak ==
                   other.trailing_timeout_streak &&
               latency_percentile_basis_points ==
                   other.latency_percentile_basis_points;
    }

    bool operator!=(const AdaptationPolicy &other) const noexcept
    {
        return !(*this == other);
    }
};

struct ReplicaAdaptationResult
{
    ReplicaID replica_id{0};
    std::uint32_t rank{0};
    ResponsivenessClass classification{
        ResponsivenessClass::insufficient_evidence};
    bool eligible{false};
    std::uint32_t attempt_count{0};
    std::uint32_t on_time_count{0};
    std::uint32_t late_count{0};
    std::uint32_t timeout_only_count{0};
    std::uint32_t response_count{0};
    std::uint32_t timeout_count{0};
    std::uint32_t trailing_timeout_count{0};
    RatePpm response_rate_ppm{0};
    RatePpm timeout_rate_ppm{0};
    std::optional<std::uint64_t> latency_percentile_us;
    std::vector<ResponsivenessReason> reasons;

    bool operator==(const ReplicaAdaptationResult &other) const noexcept
    {
        return replica_id == other.replica_id &&
               rank == other.rank &&
               classification == other.classification &&
               eligible == other.eligible &&
               attempt_count == other.attempt_count &&
               on_time_count == other.on_time_count &&
               late_count == other.late_count &&
               timeout_only_count == other.timeout_only_count &&
               response_count == other.response_count &&
               timeout_count == other.timeout_count &&
               trailing_timeout_count ==
                   other.trailing_timeout_count &&
               response_rate_ppm == other.response_rate_ppm &&
               timeout_rate_ppm == other.timeout_rate_ppm &&
               latency_percentile_us == other.latency_percentile_us &&
               reasons == other.reasons;
    }

    bool operator!=(const ReplicaAdaptationResult &other) const noexcept
    {
        return !(*this == other);
    }
};

class AdaptationSnapshot final
{
public:
    AdaptationSnapshot(const AdaptationSnapshot &) = default;
    AdaptationSnapshot(AdaptationSnapshot &&) = default;
    AdaptationSnapshot &operator=(const AdaptationSnapshot &) = delete;
    AdaptationSnapshot &operator=(AdaptationSnapshot &&) = delete;

    std::uint32_t schema_version() const noexcept
    {
        return schema_version_;
    }

    const std::string &snapshot_id() const noexcept
    {
        return snapshot_id_;
    }

    const AdaptationEpochId &epoch() const noexcept
    {
        return epoch_;
    }

    std::uint64_t evidence_cutoff() const noexcept
    {
        return evidence_cutoff_;
    }

    std::size_t accepted_record_count() const noexcept
    {
        return accepted_record_count_;
    }

    const AdaptationPolicy &policy() const noexcept
    {
        return policy_;
    }

    std::uint64_t seed() const noexcept
    {
        return seed_;
    }

    /**
     * Total canonical order used by T10.  Every entry's zero-based rank
     * equals its vector index; no downstream component may re-score it.
     */
    const std::vector<ReplicaAdaptationResult> &ranking() const noexcept
    {
        return ranking_;
    }

private:
    friend AdaptationSnapshot build_adaptation_snapshot(
        const std::vector<ReplicaID> &membership,
        const AdaptationEpochId &current_epoch,
        AcceptedEvidenceView accepted_evidence,
        std::uint64_t evidence_cutoff,
        const AdaptationPolicy &policy,
        std::uint64_t seed);

    AdaptationSnapshot() = default;
    std::uint32_t schema_version_{kAdaptationSchemaVersion};
    std::string snapshot_id_;
    AdaptationEpochId epoch_;
    std::uint64_t evidence_cutoff_{0};
    std::size_t accepted_record_count_{0};
    AdaptationPolicy policy_;
    std::uint64_t seed_{0};
    std::vector<ReplicaAdaptationResult> ranking_;
};

AdaptationSnapshot build_adaptation_snapshot(
    const std::vector<ReplicaID> &membership,
    const AdaptationEpochId &current_epoch,
    AcceptedEvidenceView accepted_evidence,
    std::uint64_t evidence_cutoff,
    const AdaptationPolicy &policy,
    std::uint64_t seed);

} // namespace hotstuff
#endif

namespace
{

using hotstuff::AcceptedEvidenceRecord;
using hotstuff::AcceptedEvidenceView;
using hotstuff::AdaptationEpochId;
using hotstuff::AdaptationPolicy;
using hotstuff::AdaptationSnapshot;
using hotstuff::AuthenticatedReporter;
using hotstuff::ConfigurationId;
using hotstuff::EpochDefinition;
using hotstuff::EpochDefinitionInput;
using hotstuff::EpochStore;
using hotstuff::EpochTreeDefinition;
using hotstuff::EpochValidationContext;
using hotstuff::EvidenceLedger;
using hotstuff::EvidenceStoreLimits;
using hotstuff::ExpectedMessageType;
using hotstuff::ProposalEvidenceStatus;
using hotstuff::ProposalEvidenceWindow;
using hotstuff::ProposalKey;
using hotstuff::RatePpm;
using hotstuff::ReplicaAdaptationResult;
using hotstuff::ReplicaID;
using hotstuff::ResponseObservation;
using hotstuff::ResponseOutcome;
using hotstuff::ResponsivenessClass;
using hotstuff::ResponsivenessReason;
using hotstuff::uint256_t;

uint256_t fixture_digest(const std::string &label)
{
    return hotstuff::DataStream(label).get_hash();
}

AdaptationEpochId epoch_id(
    std::uint32_t epoch_number,
    const std::string &label)
{
    return {epoch_number, fixture_digest(label)};
}

AdaptationEpochId epoch_id(const EpochDefinition &epoch)
{
    return {epoch.epoch_number(), epoch.epoch_digest()};
}

AcceptedEvidenceView evidence_view(
    const std::vector<AcceptedEvidenceRecord> &records)
{
    return {records.empty() ? nullptr : records.data(), records.size()};
}

ConfigurationId configuration(
    const AdaptationEpochId &epoch,
    std::uint32_t tree_id = 1)
{
    return {epoch.epoch_number, tree_id, epoch.epoch_digest};
}

class RecordBuilder final
{
public:
    explicit RecordBuilder(AdaptationEpochId epoch)
        : configuration_(configuration(epoch))
    {}

    explicit RecordBuilder(ConfigurationId configuration)
        : configuration_(std::move(configuration))
    {}

    ResponseObservation add_on_time(
        ReplicaID observed,
        std::uint64_t latency_us,
        ExpectedMessageType type = ExpectedMessageType::direct_vote)
    {
        auto value = new_attempt(observed, type);
        value.outcome = ResponseOutcome::on_time;
        value.response_duration_us = latency_us;
        value.signer_set = {observed};
        append(value);
        return value;
    }

    ResponseObservation add_timeout(
        ReplicaID observed,
        ExpectedMessageType type = ExpectedMessageType::direct_vote)
    {
        auto value = new_attempt(observed, type);
        value.outcome = ResponseOutcome::timeout;
        value.response_duration_us = 0;
        value.signer_set.clear();
        append(value);
        return value;
    }

    void add_late(
        const ResponseObservation &timeout,
        std::uint64_t latency_us)
    {
        auto value = timeout;
        value.outcome = ResponseOutcome::late;
        value.response_duration_us = latency_us;
        value.reporter_sequence = next_reporter_sequence_++;
        value.reporter_monotonic_ns = next_ingestion_sequence_ * 1'000;
        value.signer_set = {value.observed_replica_id};
        append(value);
    }

    void add_record(const ResponseObservation &observation)
    {
        append(observation);
    }

    const std::vector<AcceptedEvidenceRecord> &records() const noexcept
    {
        return records_;
    }

    std::vector<AcceptedEvidenceRecord> copy_records() const
    {
        return records_;
    }

    std::uint64_t cutoff() const noexcept
    {
        return next_ingestion_sequence_ - 1;
    }

private:
    ResponseObservation new_attempt(
        ReplicaID observed,
        ExpectedMessageType type)
    {
        ResponseObservation value;
        value.schema_version =
            hotstuff::kResponseObservationSchemaVersion;
        value.reporter_id = static_cast<ReplicaID>(observed + 100);
        value.observed_replica_id = observed;
        value.configuration = configuration_;
        value.block_hash = fixture_digest(
            "r09-attempt-" + std::to_string(next_attempt_++));
        value.expected_message_type = type;
        value.deadline_duration_us = 100;
        value.reporter_sequence = next_reporter_sequence_++;
        value.reporter_monotonic_ns = next_ingestion_sequence_ * 1'000;
        value.observation_id =
            hotstuff::compute_response_observation_id(
                value.attempt_identity());
        return value;
    }

    void append(const ResponseObservation &observation)
    {
        records_.push_back(
            AcceptedEvidenceRecord{
                next_ingestion_sequence_++, observation});
    }

    ConfigurationId configuration_;
    std::uint64_t next_attempt_{1};
    std::uint64_t next_ingestion_sequence_{1};
    std::uint64_t next_reporter_sequence_{1};
    std::vector<AcceptedEvidenceRecord> records_;
};

AdaptationSnapshot snapshot(
    const std::vector<ReplicaID> &membership,
    const AdaptationEpochId &epoch,
    const std::vector<AcceptedEvidenceRecord> &records,
    std::uint64_t cutoff,
    const AdaptationPolicy &policy = {},
    std::uint64_t seed = 0xA09)
{
    return hotstuff::build_adaptation_snapshot(
        membership,
        epoch,
        evidence_view(records),
        cutoff,
        policy,
        seed);
}

const ReplicaAdaptationResult &result_for(
    const AdaptationSnapshot &value,
    ReplicaID replica)
{
    const auto found = std::find_if(
        value.ranking().begin(),
        value.ranking().end(),
        [replica](const auto &candidate) {
            return candidate.replica_id == replica;
        });
    REQUIRE(found != value.ranking().end());
    return *found;
}

bool has_reason(
    const ReplicaAdaptationResult &result,
    ResponsivenessReason reason)
{
    return std::find(
               result.reasons.begin(), result.reasons.end(), reason) !=
           result.reasons.end();
}

std::vector<ReplicaID> ranked_ids(const AdaptationSnapshot &value)
{
    std::vector<ReplicaID> ids;
    for (const auto &result : value.ranking())
        ids.push_back(result.replica_id);
    return ids;
}

void check_same_snapshot(
    const AdaptationSnapshot &actual,
    const AdaptationSnapshot &expected)
{
    CHECK(actual.schema_version() == expected.schema_version());
    CHECK(actual.snapshot_id() == expected.snapshot_id());
    CHECK(actual.epoch() == expected.epoch());
    CHECK(actual.evidence_cutoff() == expected.evidence_cutoff());
    CHECK(actual.accepted_record_count() ==
          expected.accepted_record_count());
    CHECK(actual.policy() == expected.policy());
    CHECK(actual.seed() == expected.seed());
    CHECK(actual.ranking() == expected.ranking());
}

AdaptationPolicy permissive_policy()
{
    AdaptationPolicy policy;
    policy.minimum_attempts = 1;
    policy.minimum_response_rate_ppm = 0;
    policy.maximum_timeout_rate_ppm = hotstuff::kRatePpmScale;
    policy.trailing_timeout_streak = policy.attempt_window;
    return policy;
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

TEST_CASE("R09 adaptation API pins versioned fixed-point policy defaults",
          "[r09][adaptation][contract][intentional-red]")
{
    static_assert(
        std::is_same<RatePpm, std::uint32_t>::value,
        "adaptation rates must use uint32 fixed-point ppm");
    static_assert(
        !std::is_default_constructible<AdaptationSnapshot>::value,
        "only the validated builder may construct adaptation snapshots");
    static_assert(
        !std::is_copy_assignable<AdaptationSnapshot>::value,
        "frozen snapshots must not support assignment");
    static_assert(
        !std::is_move_assignable<AdaptationSnapshot>::value,
        "frozen snapshots must not support replacement");

    CHECK(KAURI_HAS_R09_ADAPTATION_API == 1);
    CHECK(hotstuff::kAdaptationSchemaVersion == 1);
    CHECK(hotstuff::kRatePpmScale == 1'000'000);
    CHECK(hotstuff::kMaximumAdaptationMembers == 65'536);
    CHECK(hotstuff::kMaximumAdaptationEvidenceRecords == 1'048'576);
    CHECK(hotstuff::kMaximumAdaptationAttemptWindow == 4'096);
    CHECK(hotstuff::kMaximumAdaptationPolicyVersionBytes == 64);
    CHECK(hotstuff::kPercentileBasisPointScale == 10'000);

    CHECK(static_cast<std::uint8_t>(
              ResponsivenessClass::responsive) == 1);
    CHECK(static_cast<std::uint8_t>(
              ResponsivenessClass::insufficient_evidence) == 2);
    CHECK(static_cast<std::uint8_t>(
              ResponsivenessClass::nonresponsive) == 3);
    CHECK(static_cast<std::uint8_t>(
              ResponsivenessReason::insufficient_attempts) == 1);
    CHECK(static_cast<std::uint8_t>(
              ResponsivenessReason::response_rate_below_minimum) == 2);
    CHECK(static_cast<std::uint8_t>(
              ResponsivenessReason::timeout_rate_above_maximum) == 3);
    CHECK(static_cast<std::uint8_t>(
              ResponsivenessReason::persistent_timeout_streak) == 4);

    const AdaptationPolicy policy;
    CHECK(policy.schema_version == 1);
    CHECK(policy.policy_version == "kauri-responsiveness-v1");
    CHECK(policy.attempt_window == 32);
    CHECK(policy.minimum_attempts == 8);
    CHECK(policy.minimum_response_rate_ppm == 750'000);
    CHECK(policy.maximum_timeout_rate_ppm == 250'000);
    CHECK(policy.trailing_timeout_streak == 3);
    CHECK(policy.latency_percentile_basis_points == 5'000);
}

TEST_CASE("three persistent misses are nonresponsive without healthy false positives",
          "[r09][adaptation][classification][persistent]"
          "[intentional-red]")
{
    const auto epoch = epoch_id(9, "r09-three-persistent");
    RecordBuilder builder(epoch);
    const std::vector<ReplicaID> healthy{0, 1, 2};
    const std::vector<ReplicaID> persistent{3, 4, 5};

    for (const auto replica : healthy)
        for (std::uint32_t attempt = 0; attempt < 8; ++attempt)
            builder.add_on_time(replica, 40 + replica);
    for (const auto replica : persistent)
        for (std::uint32_t attempt = 0; attempt < 8; ++attempt)
            builder.add_timeout(replica);

    const auto value = snapshot(
        {0, 1, 2, 3, 4, 5},
        epoch,
        builder.records(),
        builder.cutoff());

    REQUIRE(value.ranking().size() == 6);
    for (std::size_t rank = 0; rank < value.ranking().size(); ++rank)
        CHECK(value.ranking().at(rank).rank == rank);
    for (const auto replica : healthy)
    {
        const auto &result = result_for(value, replica);
        CHECK(result.classification ==
              ResponsivenessClass::responsive);
        CHECK(result.eligible);
        CHECK(result.response_rate_ppm == 1'000'000);
        CHECK(result.timeout_rate_ppm == 0);
        CHECK(result.reasons.empty());
    }
    for (const auto replica : persistent)
    {
        const auto &result = result_for(value, replica);
        CHECK(result.classification ==
              ResponsivenessClass::nonresponsive);
        CHECK_FALSE(result.eligible);
        CHECK(result.attempt_count == 8);
        CHECK(result.response_count == 0);
        CHECK(result.timeout_count == 8);
        CHECK(result.trailing_timeout_count == 8);
        CHECK(result.response_rate_ppm == 0);
        CHECK(result.timeout_rate_ppm == 1'000'000);
        CHECK(has_reason(
            result,
            ResponsivenessReason::response_rate_below_minimum));
        CHECK(has_reason(
            result,
            ResponsivenessReason::timeout_rate_above_maximum));
        CHECK(has_reason(
            result,
            ResponsivenessReason::persistent_timeout_streak));
    }
}

TEST_CASE("one timeout followed by late remains one healthy attempt",
          "[r09][adaptation][late][correlation][intentional-red]")
{
    const auto epoch = epoch_id(10, "r09-isolated-late");
    RecordBuilder builder(epoch);
    for (std::uint32_t attempt = 0; attempt < 7; ++attempt)
        builder.add_on_time(7, 50 + attempt);
    const auto delayed = builder.add_timeout(7);
    builder.add_late(delayed, 150);

    const auto value = snapshot(
        {7}, epoch, builder.records(), builder.cutoff());
    const auto &result = result_for(value, 7);

    CHECK(result.classification == ResponsivenessClass::responsive);
    CHECK(result.eligible);
    CHECK(result.attempt_count == 8);
    CHECK(result.on_time_count == 7);
    CHECK(result.late_count == 1);
    CHECK(result.timeout_only_count == 0);
    CHECK(result.response_count == 8);
    CHECK(result.timeout_count == 1);
    CHECK(result.trailing_timeout_count == 0);
    CHECK(result.response_rate_ppm == 1'000'000);
    CHECK(result.timeout_rate_ppm == 125'000);
    CHECK(result.reasons.empty());
}

TEST_CASE(
    "shape25 policy detects a one-in-ten rotating omission share",
    "[r09][adaptation][shape25][rotating-omission][n31]")
{
    const auto epoch = epoch_id(31, "shape25-rotating-omission");
    RecordBuilder builder(epoch);
    for (std::uint32_t attempt = 0; attempt < 128; ++attempt)
    {
        if (attempt % 10 == 0)
            builder.add_timeout(0);
        else
            builder.add_on_time(0, 40);
        builder.add_on_time(1, 40);
    }

    AdaptationPolicy policy;
    policy.policy_version = "shape25-sensitive-responsiveness-v1";
    policy.attempt_window = 128;
    policy.minimum_attempts = 32;
    policy.minimum_response_rate_ppm = 950'000;
    policy.maximum_timeout_rate_ppm = 50'000;
    policy.trailing_timeout_streak = 2;
    policy.latency_percentile_basis_points = 5'000;

    const auto value = snapshot(
        {0, 1}, epoch, builder.records(), builder.cutoff(), policy);
    const auto &actor = result_for(value, 0);
    const auto &healthy = result_for(value, 1);

    CHECK(actor.attempt_count == 128);
    CHECK(actor.timeout_count == 13);
    CHECK(actor.response_rate_ppm == 898'437);
    CHECK(actor.timeout_rate_ppm == 101'562);
    CHECK(actor.classification == ResponsivenessClass::nonresponsive);
    CHECK_FALSE(actor.eligible);
    CHECK(has_reason(
        actor,
        ResponsivenessReason::response_rate_below_minimum));
    CHECK(has_reason(
        actor,
        ResponsivenessReason::timeout_rate_above_maximum));

    CHECK(healthy.attempt_count == 128);
    CHECK(healthy.response_rate_ppm == 1'000'000);
    CHECK(healthy.timeout_rate_ppm == 0);
    CHECK(healthy.classification == ResponsivenessClass::responsive);
    CHECK(healthy.eligible);
}

TEST_CASE("a late completion recovers a persistent trailing miss streak",
          "[r09][adaptation][streak][recovery][intentional-red]")
{
    const auto epoch = epoch_id(11, "r09-streak-recovery");
    RecordBuilder builder(epoch);
    for (std::uint32_t attempt = 0; attempt < 9; ++attempt)
        builder.add_on_time(8, 30);
    builder.add_timeout(8);
    builder.add_timeout(8);
    const auto latest_timeout = builder.add_timeout(8);

    const auto before = snapshot(
        {8}, epoch, builder.records(), builder.cutoff());
    const auto &before_result = result_for(before, 8);
    CHECK(before_result.response_rate_ppm == 750'000);
    CHECK(before_result.timeout_rate_ppm == 250'000);
    CHECK(before_result.trailing_timeout_count == 3);
    CHECK(before_result.classification ==
          ResponsivenessClass::nonresponsive);
    CHECK(has_reason(
        before_result,
        ResponsivenessReason::persistent_timeout_streak));

    builder.add_late(latest_timeout, 175);
    const auto after = snapshot(
        {8}, epoch, builder.records(), builder.cutoff());
    const auto &after_result = result_for(after, 8);
    CHECK(after_result.attempt_count == 12);
    CHECK(after_result.late_count == 1);
    CHECK(after_result.timeout_only_count == 2);
    CHECK(after_result.response_count == 10);
    CHECK(after_result.timeout_count == 3);
    CHECK(after_result.response_rate_ppm == 833'333);
    CHECK(after_result.timeout_rate_ppm == 250'000);
    CHECK(after_result.trailing_timeout_count == 0);
    CHECK(after_result.classification ==
          ResponsivenessClass::responsive);
    CHECK(after_result.eligible);
    CHECK(after_result.reasons.empty());
}

TEST_CASE("promotion requires eight attempts and accepts exact rate boundaries",
          "[r09][adaptation][boundary][minimum][ppm][intentional-red]")
{
    SECTION("seven versus eight attempts")
    {
        const auto epoch = epoch_id(12, "r09-minimum-boundary");
        RecordBuilder builder(epoch);
        for (std::uint32_t attempt = 0; attempt < 7; ++attempt)
            builder.add_on_time(0, 20);
        for (std::uint32_t attempt = 0; attempt < 8; ++attempt)
            builder.add_on_time(1, 20);

        const auto value = snapshot(
            {0, 1}, epoch, builder.records(), builder.cutoff());
        const auto &seven = result_for(value, 0);
        const auto &eight = result_for(value, 1);

        CHECK(seven.attempt_count == 7);
        CHECK(seven.response_rate_ppm == 1'000'000);
        CHECK(seven.classification ==
              ResponsivenessClass::insufficient_evidence);
        CHECK_FALSE(seven.eligible);
        CHECK(has_reason(
            seven, ResponsivenessReason::insufficient_attempts));
        CHECK(eight.attempt_count == 8);
        CHECK(eight.classification ==
              ResponsivenessClass::responsive);
        CHECK(eight.eligible);
        CHECK(ranked_ids(value).front() == 1);
    }

    SECTION("seventy-five and twenty-five percent are inclusive")
    {
        const auto epoch = epoch_id(13, "r09-rate-boundary");
        RecordBuilder builder(epoch);
        for (std::uint32_t attempt = 0; attempt < 3; ++attempt)
            builder.add_on_time(0, 20);
        builder.add_timeout(0);
        for (std::uint32_t attempt = 0; attempt < 3; ++attempt)
            builder.add_on_time(0, 20);
        builder.add_timeout(0);

        const auto value = snapshot(
            {0}, epoch, builder.records(), builder.cutoff());
        const auto &boundary = result_for(value, 0);
        CHECK(boundary.attempt_count == 8);
        CHECK(boundary.response_count == 6);
        CHECK(boundary.timeout_count == 2);
        CHECK(boundary.response_rate_ppm == 750'000);
        CHECK(boundary.timeout_rate_ppm == 250'000);
        CHECK(boundary.trailing_timeout_count == 1);
        CHECK(boundary.classification ==
              ResponsivenessClass::responsive);
        CHECK(boundary.eligible);
        CHECK(boundary.reasons.empty());
    }
}

TEST_CASE("snapshot freezes only exact-current-epoch evidence at an inclusive cutoff",
          "[r09][adaptation][snapshot][epoch][cutoff]"
          "[intentional-red]")
{
    const auto current = epoch_id(14, "r09-current-epoch");
    const auto previous = epoch_id(13, "r09-previous-epoch");
    const auto wrong_digest = epoch_id(14, "r09-wrong-digest");
    RecordBuilder builder(configuration(previous));

    for (std::uint32_t attempt = 0; attempt < 8; ++attempt)
        builder.add_timeout(0);

    auto records = builder.copy_records();
    RecordBuilder wrong_builder(configuration(wrong_digest));
    for (std::uint32_t attempt = 0; attempt < 8; ++attempt)
        wrong_builder.add_timeout(0);
    for (auto record : wrong_builder.records())
    {
        record.ingestion_sequence = records.size() + 1;
        records.push_back(std::move(record));
    }

    RecordBuilder current_builder(configuration(current));
    for (std::uint32_t attempt = 0; attempt < 8; ++attempt)
        current_builder.add_on_time(0, 40);
    for (auto record : current_builder.records())
    {
        record.ingestion_sequence = records.size() + 1;
        records.push_back(std::move(record));
    }
    const auto cutoff = records.back().ingestion_sequence;

    const auto frozen = snapshot(
        {0}, current, records, cutoff, AdaptationPolicy{}, 101);
    const auto frozen_ranking = frozen.ranking();
    const auto frozen_id = frozen.snapshot_id();
    const auto &at_cutoff = result_for(frozen, 0);

    REQUIRE_FALSE(frozen_id.empty());
    CHECK(frozen.schema_version() == 1);
    CHECK(frozen.epoch() == current);
    CHECK(frozen.evidence_cutoff() == cutoff);
    CHECK(frozen.accepted_record_count() == 8);
    CHECK(frozen.policy() == AdaptationPolicy{});
    CHECK(frozen.seed() == 101);
    CHECK(at_cutoff.attempt_count == 8);
    CHECK(at_cutoff.classification == ResponsivenessClass::responsive);

    RecordBuilder future(configuration(current, 2));
    for (std::uint32_t attempt = 0; attempt < 8; ++attempt)
        future.add_timeout(0);
    for (auto record : future.records())
    {
        record.ingestion_sequence = records.size() + 1;
        records.push_back(std::move(record));
    }

    CHECK(frozen.ranking() == frozen_ranking);
    CHECK(frozen.snapshot_id() == frozen_id);

    const auto rebuilt = snapshot(
        {0}, current, records, cutoff, AdaptationPolicy{}, 101);
    CHECK(rebuilt.ranking() == frozen_ranking);
    CHECK(rebuilt.snapshot_id() == frozen_id);
    CHECK(rebuilt.accepted_record_count() == 8);

    const auto later = snapshot(
        {0},
        current,
        records,
        records.back().ingestion_sequence,
        AdaptationPolicy{},
        101);
    CHECK(later.accepted_record_count() == 16);
    CHECK(result_for(later, 0).attempt_count == 16);
    CHECK(result_for(later, 0).response_rate_ppm == 500'000);
    CHECK(result_for(later, 0).classification ==
          ResponsivenessClass::nonresponsive);
}

TEST_CASE("cutoff is applied before sequence observation and transition validation",
          "[r09][adaptation][snapshot][cutoff][validation]"
          "[intentional-red]")
{
    const auto epoch = epoch_id(28, "r09-cutoff-validation-boundary");
    RecordBuilder builder(epoch);
    for (std::uint32_t attempt = 0; attempt < 8; ++attempt)
        builder.add_on_time(0, 30 + attempt);

    const auto records = builder.copy_records();
    const auto cutoff = builder.cutoff();
    const auto frozen = snapshot(
        {0}, epoch, records, cutoff, AdaptationPolicy{}, 2801);
    const auto future_record = [&](const std::string &label,
                                   std::uint64_t sequence) {
        auto record = records.back();
        record.ingestion_sequence = sequence;
        record.observation.block_hash = fixture_digest(label);
        record.observation.reporter_sequence = sequence;
        record.observation.reporter_monotonic_ns = sequence * 1'000;
        record.observation.observation_id =
            hotstuff::compute_response_observation_id(
                record.observation.attempt_identity());
        return record;
    };

    SECTION("duplicate future ingestion sequences are ignored")
    {
        auto extended = records;
        extended.push_back(future_record(
            "r09-future-sequence-a", cutoff + 1));
        extended.push_back(future_record(
            "r09-future-sequence-b", cutoff + 1));

        const auto rebuilt = snapshot(
            {0}, epoch, extended, cutoff, AdaptationPolicy{}, 2801);
        check_same_snapshot(rebuilt, frozen);
    }

    SECTION("a duplicate fact after the cutoff is ignored")
    {
        auto extended = records;
        auto duplicate = records.front();
        duplicate.ingestion_sequence = cutoff + 1;
        extended.push_back(std::move(duplicate));

        const auto rebuilt = snapshot(
            {0}, epoch, extended, cutoff, AdaptationPolicy{}, 2801);
        check_same_snapshot(rebuilt, frozen);
    }

    SECTION("a malformed observation after the cutoff is ignored")
    {
        auto extended = records;
        auto malformed = future_record(
            "r09-future-malformed", cutoff + 1);
        ++malformed.observation.schema_version;
        extended.push_back(std::move(malformed));

        const auto rebuilt = snapshot(
            {0}, epoch, extended, cutoff, AdaptationPolicy{}, 2801);
        check_same_snapshot(rebuilt, frozen);
    }

    SECTION("an orphan late transition after the cutoff is ignored")
    {
        auto extended = records;
        auto orphan = future_record(
            "r09-future-orphan-late", cutoff + 1);
        orphan.observation.outcome = ResponseOutcome::late;
        orphan.observation.response_duration_us =
            orphan.observation.deadline_duration_us + 50;
        extended.push_back(std::move(orphan));

        const auto rebuilt = snapshot(
            {0}, epoch, extended, cutoff, AdaptationPolicy{}, 2801);
        check_same_snapshot(rebuilt, frozen);
    }
}

TEST_CASE("non-current evidence is filtered before observation validation",
          "[r09][adaptation][snapshot][epoch][validation]"
          "[intentional-red]")
{
    const auto current = epoch_id(29, "r09-filter-current");
    const auto previous = epoch_id(28, "r09-filter-previous");
    const auto wrong_digest = epoch_id(29, "r09-filter-wrong-digest");
    RecordBuilder builder(current);
    for (std::uint32_t attempt = 0; attempt < 8; ++attempt)
        builder.add_on_time(0, 40 + attempt);

    auto current_records = builder.copy_records();
    for (auto &record : current_records)
        record.ingestion_sequence += 2;
    const auto cutoff = current_records.back().ingestion_sequence;
    const auto frozen = snapshot(
        {0},
        current,
        current_records,
        cutoff,
        AdaptationPolicy{},
        2901);

    SECTION("malformed wrong-digest evidence cannot poison the snapshot")
    {
        auto wrong = current_records.front();
        wrong.ingestion_sequence = 1;
        wrong.observation.configuration = configuration(wrong_digest);
        wrong.observation.block_hash = fixture_digest(
            "r09-wrong-digest-malformed");
        wrong.observation.observation_id =
            hotstuff::compute_response_observation_id(
                wrong.observation.attempt_identity());
        ++wrong.observation.schema_version;

        auto extended = current_records;
        extended.push_back(std::move(wrong));
        const auto rebuilt = snapshot(
            {0},
            current,
            extended,
            cutoff,
            AdaptationPolicy{},
            2901);
        check_same_snapshot(rebuilt, frozen);
    }

    SECTION("orphan late prior-epoch evidence cannot poison the snapshot")
    {
        auto prior = current_records.front();
        prior.ingestion_sequence = 2;
        prior.observation.configuration = configuration(previous);
        prior.observation.block_hash = fixture_digest(
            "r09-prior-epoch-orphan-late");
        prior.observation.observation_id =
            hotstuff::compute_response_observation_id(
                prior.observation.attempt_identity());
        prior.observation.outcome = ResponseOutcome::late;
        prior.observation.response_duration_us =
            prior.observation.deadline_duration_us + 50;

        auto extended = current_records;
        extended.push_back(std::move(prior));
        const auto rebuilt = snapshot(
            {0},
            current,
            extended,
            cutoff,
            AdaptationPolicy{},
            2901);
        check_same_snapshot(rebuilt, frozen);
    }
}

TEST_CASE("canonical ingestion order makes shuffled input byte-stable",
          "[r09][adaptation][determinism][shuffle][intentional-red]")
{
    const auto epoch = epoch_id(15, "r09-shuffle");
    RecordBuilder builder(epoch);
    for (std::uint32_t attempt = 0; attempt < 8; ++attempt)
    {
        builder.add_on_time(2, 20 + attempt);
        if (attempt % 2 == 0)
            builder.add_on_time(1, 40 + attempt);
        else
        {
            const auto delayed = builder.add_timeout(1);
            builder.add_late(delayed, 140 + attempt);
        }
    }

    auto shuffled = builder.copy_records();
    std::reverse(shuffled.begin(), shuffled.end());
    const auto canonical = snapshot(
        {1, 2}, epoch, builder.records(), builder.cutoff(), {}, 222);
    const auto reordered = snapshot(
        {1, 2}, epoch, shuffled, builder.cutoff(), {}, 222);

    CHECK(reordered.snapshot_id() == canonical.snapshot_id());
    CHECK(reordered.epoch() == canonical.epoch());
    CHECK(reordered.evidence_cutoff() == canonical.evidence_cutoff());
    CHECK(reordered.accepted_record_count() ==
          canonical.accepted_record_count());
    CHECK(reordered.policy() == canonical.policy());
    CHECK(reordered.seed() == canonical.seed());
    CHECK(reordered.ranking() == canonical.ranking());
}

TEST_CASE("seed is echoed without changing deterministic classification order",
          "[r09][adaptation][determinism][seed][intentional-red]")
{
    const auto epoch = epoch_id(16, "r09-seed");
    RecordBuilder builder(epoch);
    for (std::uint32_t attempt = 0; attempt < 8; ++attempt)
    {
        builder.add_on_time(3, 20);
        builder.add_on_time(4, 40);
    }

    const auto first = snapshot(
        {3, 4}, epoch, builder.records(), builder.cutoff(), {}, 7);
    const auto second = snapshot(
        {3, 4}, epoch, builder.records(), builder.cutoff(), {}, 99);

    CHECK(first.seed() == 7);
    CHECK(second.seed() == 99);
    CHECK(first.snapshot_id() != second.snapshot_id());
    CHECK(first.ranking() == second.ranking());
    CHECK(ranked_ids(first) == std::vector<ReplicaID>{3, 4});
}

TEST_CASE("snapshot identity binds complete accepted current-epoch evidence",
          "[r09][adaptation][snapshot][identity][evidence]"
          "[intentional-red]")
{
    const auto epoch = epoch_id(30, "r09-complete-snapshot-identity");
    RecordBuilder builder(epoch);
    for (std::uint32_t attempt = 0; attempt < 8; ++attempt)
    {
        builder.add_on_time(
            0,
            30 + attempt,
            ExpectedMessageType::aggregate_relay);
    }

    const auto records = builder.copy_records();
    const auto canonical = snapshot(
        {0, 1}, epoch, records, builder.cutoff(), {}, 3001);

    SECTION("reporter sequence participates in snapshot identity")
    {
        auto changed = records;
        ++changed.back().observation.reporter_sequence;
        const auto variant = snapshot(
            {0, 1}, epoch, changed, builder.cutoff(), {}, 3001);

        CHECK(variant.ranking() == canonical.ranking());
        CHECK(variant.snapshot_id() != canonical.snapshot_id());
    }

    SECTION("reporter monotonic time participates in snapshot identity")
    {
        auto changed = records;
        ++changed.back().observation.reporter_monotonic_ns;
        const auto variant = snapshot(
            {0, 1}, epoch, changed, builder.cutoff(), {}, 3001);

        CHECK(variant.ranking() == canonical.ranking());
        CHECK(variant.snapshot_id() != canonical.snapshot_id());
    }

    SECTION("canonical signer set participates in snapshot identity")
    {
        auto changed = records;
        changed.back().observation.signer_set = {0, 1};
        const auto variant = snapshot(
            {0, 1}, epoch, changed, builder.cutoff(), {}, 3001);

        CHECK(variant.ranking() == canonical.ranking());
        CHECK(variant.snapshot_id() != canonical.snapshot_id());
    }
}

TEST_CASE("rates floor in uint64 and p50 uses integer nearest rank",
          "[r09][adaptation][fixed-point][percentile][intentional-red]")
{
    const auto epoch = epoch_id(17, "r09-fixed-point");
    RecordBuilder builder(epoch);
    builder.add_on_time(0, 4);
    builder.add_on_time(0, 1);
    builder.add_timeout(0);

    const auto value = snapshot(
        {0},
        epoch,
        builder.records(),
        builder.cutoff(),
        permissive_policy());
    const auto &result = result_for(value, 0);

    CHECK(result.attempt_count == 3);
    CHECK(result.response_count == 2);
    CHECK(result.timeout_count == 1);
    CHECK(result.response_rate_ppm == 666'666);
    CHECK(result.timeout_rate_ppm == 333'333);
    REQUIRE(result.latency_percentile_us.has_value());
    CHECK(*result.latency_percentile_us == 1);

    const auto large_epoch = epoch_id(18, "r09-wide-integer");
    RecordBuilder large_builder(large_epoch);
    for (std::uint32_t attempt = 0; attempt < 32; ++attempt)
        large_builder.add_on_time(
            0,
            std::numeric_limits<std::uint64_t>::max() - attempt);
    const auto large = snapshot(
        {0},
        large_epoch,
        large_builder.records(),
        large_builder.cutoff());
    CHECK(result_for(large, 0).response_rate_ppm == 1'000'000);
    REQUIRE(result_for(large, 0).latency_percentile_us.has_value());
    CHECK(*result_for(large, 0).latency_percentile_us ==
          std::numeric_limits<std::uint64_t>::max() - 16);
}

TEST_CASE("latest window is selected by attempt first-ingestion sequence",
          "[r09][adaptation][window][first-ingestion]"
          "[intentional-red]")
{
    const auto epoch = epoch_id(19, "r09-latest-window");
    RecordBuilder builder(epoch);
    const auto oldest_timeout = builder.add_timeout(0);
    for (std::uint32_t attempt = 0; attempt < 32; ++attempt)
        builder.add_on_time(0, 50);
    builder.add_late(oldest_timeout, 150);

    const auto value = snapshot(
        {0}, epoch, builder.records(), builder.cutoff());
    const auto &result = result_for(value, 0);

    CHECK(value.accepted_record_count() == 34);
    CHECK(result.attempt_count == 32);
    CHECK(result.on_time_count == 32);
    CHECK(result.late_count == 0);
    CHECK(result.timeout_only_count == 0);
    CHECK(result.response_count == 32);
    CHECK(result.timeout_count == 0);
    CHECK(result.response_rate_ppm == 1'000'000);
    CHECK(result.timeout_rate_ppm == 0);
    REQUIRE(result.latency_percentile_us.has_value());
    CHECK(*result.latency_percentile_us == 50);
    CHECK(result.classification == ResponsivenessClass::responsive);
}

TEST_CASE("ranking applies every deterministic key in the documented order",
          "[r09][adaptation][ranking][ties][intentional-red]")
{
    SECTION("eligible precedes a faster insufficient candidate")
    {
        const auto epoch = epoch_id(20, "r09-rank-eligible");
        RecordBuilder builder(epoch);
        for (std::uint32_t attempt = 0; attempt < 8; ++attempt)
            builder.add_on_time(9, 500);
        for (std::uint32_t attempt = 0; attempt < 7; ++attempt)
            builder.add_on_time(1, 1);

        const auto value = snapshot(
            {1, 9}, epoch, builder.records(), builder.cutoff());
        CHECK(ranked_ids(value) == std::vector<ReplicaID>{9, 1});
    }

    SECTION("response rate precedes timeout rate")
    {
        const auto epoch = epoch_id(21, "r09-rank-response");
        RecordBuilder builder(epoch);
        for (std::uint32_t attempt = 0; attempt < 8; ++attempt)
        {
            const auto late = builder.add_timeout(0);
            builder.add_late(late, 150);
        }
        for (std::uint32_t attempt = 0; attempt < 6; ++attempt)
            builder.add_on_time(1, 10);
        builder.add_timeout(1);
        builder.add_timeout(1);

        const auto value = snapshot(
            {0, 1},
            epoch,
            builder.records(),
            builder.cutoff(),
            permissive_policy());
        CHECK(result_for(value, 0).response_rate_ppm == 1'000'000);
        CHECK(result_for(value, 0).timeout_rate_ppm == 1'000'000);
        CHECK(result_for(value, 1).response_rate_ppm == 750'000);
        CHECK(result_for(value, 1).timeout_rate_ppm == 250'000);
        CHECK(ranked_ids(value) == std::vector<ReplicaID>{0, 1});
    }

    SECTION("lower timeout rate breaks an equal response rate")
    {
        const auto epoch = epoch_id(22, "r09-rank-timeout");
        RecordBuilder builder(epoch);
        for (std::uint32_t attempt = 0; attempt < 8; ++attempt)
            builder.add_on_time(0, 40);
        for (std::uint32_t attempt = 0; attempt < 7; ++attempt)
            builder.add_on_time(1, 10);
        const auto late = builder.add_timeout(1);
        builder.add_late(late, 150);

        const auto value = snapshot(
            {0, 1}, epoch, builder.records(), builder.cutoff());
        CHECK(result_for(value, 0).response_rate_ppm == 1'000'000);
        CHECK(result_for(value, 1).response_rate_ppm == 1'000'000);
        CHECK(result_for(value, 0).timeout_rate_ppm == 0);
        CHECK(result_for(value, 1).timeout_rate_ppm == 125'000);
        CHECK(ranked_ids(value) == std::vector<ReplicaID>{0, 1});
    }

    SECTION("lower latency follows equal response and timeout rates")
    {
        const auto epoch = epoch_id(23, "r09-rank-latency");
        RecordBuilder builder(epoch);
        for (std::uint32_t attempt = 0; attempt < 8; ++attempt)
        {
            builder.add_on_time(7, 80);
            builder.add_on_time(8, 20);
        }

        const auto value = snapshot(
            {7, 8}, epoch, builder.records(), builder.cutoff());
        CHECK(result_for(value, 7).response_rate_ppm ==
              result_for(value, 8).response_rate_ppm);
        CHECK(result_for(value, 7).timeout_rate_ppm ==
              result_for(value, 8).timeout_rate_ppm);
        CHECK(ranked_ids(value) == std::vector<ReplicaID>{8, 7});
    }

    SECTION("more attempts follow equal rates and latency")
    {
        const auto epoch = epoch_id(24, "r09-rank-attempts");
        RecordBuilder builder(epoch);
        for (std::uint32_t attempt = 0; attempt < 16; ++attempt)
            builder.add_on_time(5, 30);
        for (std::uint32_t attempt = 0; attempt < 8; ++attempt)
            builder.add_on_time(6, 30);

        const auto value = snapshot(
            {5, 6}, epoch, builder.records(), builder.cutoff());
        CHECK(result_for(value, 5).response_rate_ppm ==
              result_for(value, 6).response_rate_ppm);
        CHECK(result_for(value, 5).timeout_rate_ppm ==
              result_for(value, 6).timeout_rate_ppm);
        CHECK(result_for(value, 5).latency_percentile_us ==
              result_for(value, 6).latency_percentile_us);
        CHECK(ranked_ids(value) == std::vector<ReplicaID>{5, 6});
    }

    SECTION("ReplicaID is the final tie break")
    {
        const auto epoch = epoch_id(25, "r09-rank-id");
        RecordBuilder builder(epoch);
        for (std::uint32_t attempt = 0; attempt < 8; ++attempt)
        {
            builder.add_on_time(12, 30);
            builder.add_on_time(2, 30);
        }

        const auto value = snapshot(
            {12, 2}, epoch, builder.records(), builder.cutoff());
        CHECK(result_for(value, 2).attempt_count ==
              result_for(value, 12).attempt_count);
        CHECK(result_for(value, 2).response_rate_ppm ==
              result_for(value, 12).response_rate_ppm);
        CHECK(result_for(value, 2).timeout_rate_ppm ==
              result_for(value, 12).timeout_rate_ppm);
        CHECK(result_for(value, 2).latency_percentile_us ==
              result_for(value, 12).latency_percentile_us);
        CHECK(ranked_ids(value) == std::vector<ReplicaID>{2, 12});
    }
}

namespace
{

class MutableEvidenceWindow final : public ProposalEvidenceWindow
{
public:
    void accept(const ProposalKey &proposal)
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

class AcceptedEvidenceFixture final
{
public:
    AcceptedEvidenceFixture()
        : epochs_(members()),
          ledger_(epochs_, window_, EvidenceStoreLimits{128, 128})
    {
        EpochDefinitionInput input;
        input.schema_version = hotstuff::kEpochDefinitionSchemaVersion;
        input.epoch_number = 0;
        input.previous_epoch_digest = {};
        input.membership_digest =
            hotstuff::canonical_membership_digest(members());
        input.trees = {
            EpochTreeDefinition{7, 2, 2, members()},
        };
        input.activation_height = 5;
        input.generation_seed = 0xE0809;
        input.policy_version = "r09-e08-fixture-v1";
        input.evidence_snapshot_id = "r09-e08-source";
        input.evidence_cutoff = 0;
        input.epoch_digest.reset();
        epoch_ = &epochs_.stage(
            input, EpochValidationContext{0, 5, {}});
    }

    static std::vector<ReplicaID> members()
    {
        return {0, 1, 2, 3, 4, 5, 6};
    }

    void ingest_on_time(
        ReplicaID reporter,
        ReplicaID observed,
        ExpectedMessageType type,
        std::uint64_t latency_us,
        std::vector<ReplicaID> signers)
    {
        ResponseObservation observation;
        observation.schema_version =
            hotstuff::kResponseObservationSchemaVersion;
        observation.reporter_id = reporter;
        observation.observed_replica_id = observed;
        observation.configuration = {
            epoch_->epoch_number(), 7, epoch_->epoch_digest()};
        observation.block_hash = fixture_digest(
            "r09-e08-block-" + std::to_string(next_block_++));
        observation.expected_message_type = type;
        observation.outcome = ResponseOutcome::on_time;
        observation.response_duration_us = latency_us;
        observation.deadline_duration_us = 100;
        observation.reporter_monotonic_ns = next_monotonic_++;
        observation.reporter_sequence = ++reporter_sequences_[reporter];
        observation.signer_set = std::move(signers);
        observation.observation_id =
            hotstuff::compute_response_observation_id(
                observation.attempt_identity());
        window_.accept(observation.proposal_key());
        ledger_.ingest(AuthenticatedReporter{reporter}, observation);
    }

    const EpochDefinition &epoch() const
    {
        REQUIRE(epoch_ != nullptr);
        return *epoch_;
    }

    const EvidenceLedger &ledger() const noexcept
    {
        return ledger_;
    }

private:
    EpochStore epochs_;
    MutableEvidenceWindow window_;
    EvidenceLedger ledger_;
    const EpochDefinition *epoch_{nullptr};
    std::uint64_t next_block_{1};
    std::uint64_t next_monotonic_{1'000};
    std::map<ReplicaID, std::uint64_t> reporter_sequences_;
};

} // namespace

TEST_CASE("R09 consumes real accepted direct votes and aggregate relays",
          "[r09][adaptation][e08][integration][intentional-red]")
{
    AcceptedEvidenceFixture fixture;
    for (std::uint32_t attempt = 0; attempt < 8; ++attempt)
    {
        fixture.ingest_on_time(
            0,
            1,
            ExpectedMessageType::aggregate_relay,
            40 + attempt,
            {3});
        fixture.ingest_on_time(
            1,
            3,
            ExpectedMessageType::direct_vote,
            20 + attempt,
            {3});
    }

    REQUIRE(fixture.ledger().healthy());
    REQUIRE(fixture.ledger().rejected().empty());
    REQUIRE(fixture.ledger().accepted().size() == 16);

    const auto value = snapshot(
        AcceptedEvidenceFixture::members(),
        epoch_id(fixture.epoch()),
        fixture.ledger().accepted(),
        fixture.ledger().high_watermark());
    const auto &aggregate = result_for(value, 1);
    const auto &direct = result_for(value, 3);

    CHECK(value.accepted_record_count() == 16);
    CHECK(aggregate.attempt_count == 8);
    CHECK(aggregate.response_count == 8);
    CHECK(aggregate.classification == ResponsivenessClass::responsive);
    CHECK(direct.attempt_count == 8);
    CHECK(direct.response_count == 8);
    CHECK(direct.classification == ResponsivenessClass::responsive);
    CHECK(ranked_ids(value).at(0) == 3);
    CHECK(ranked_ids(value).at(1) == 1);
}

TEST_CASE("adaptation rejects every bounded-input violation",
          "[r09][adaptation][validation][bounds][intentional-red]")
{
    const auto epoch = epoch_id(26, "r09-invalid");
    const std::vector<AcceptedEvidenceRecord> no_records;
    const auto call = [&](const std::vector<ReplicaID> &membership,
                          AcceptedEvidenceView evidence,
                          const AdaptationPolicy &policy) {
        return hotstuff::build_adaptation_snapshot(
            membership, epoch, evidence, 0, policy, 0);
    };

    SECTION("membership must be nonempty and unique")
    {
        CHECK_THROWS_AS(
            call({}, evidence_view(no_records), {}),
            std::invalid_argument);
        CHECK_THROWS_AS(
            call({0, 0}, evidence_view(no_records), {}),
            std::invalid_argument);
    }

    SECTION("membership count is bounded before canonicalization")
    {
        std::vector<ReplicaID> too_many(
            hotstuff::kMaximumAdaptationMembers + 1, 0);
        CHECK_THROWS_AS(
            call(too_many, evidence_view(no_records), {}),
            std::invalid_argument);
    }

    SECTION("evidence view must be valid and bounded before reading")
    {
        CHECK_THROWS_AS(
            call({0}, AcceptedEvidenceView{nullptr, 1}, {}),
            std::invalid_argument);
        CHECK_THROWS_AS(
            call(
                {0},
                AcceptedEvidenceView{
                    nullptr,
                    hotstuff::kMaximumAdaptationEvidenceRecords + 1},
                {}),
            std::invalid_argument);
    }

    SECTION("policy schema and version are bounded")
    {
        auto policy = AdaptationPolicy{};
        ++policy.schema_version;
        CHECK_THROWS_AS(
            call({0}, evidence_view(no_records), policy),
            std::invalid_argument);

        policy = AdaptationPolicy{};
        policy.policy_version.assign(
            hotstuff::kMaximumAdaptationPolicyVersionBytes + 1, 'x');
        CHECK_THROWS_AS(
            call({0}, evidence_view(no_records), policy),
            std::invalid_argument);
    }

    SECTION("attempt window and minimum evidence are bounded")
    {
        auto policy = AdaptationPolicy{};
        policy.attempt_window = 0;
        CHECK_THROWS_AS(
            call({0}, evidence_view(no_records), policy),
            std::invalid_argument);

        policy = AdaptationPolicy{};
        policy.minimum_attempts = 0;
        CHECK_THROWS_AS(
            call({0}, evidence_view(no_records), policy),
            std::invalid_argument);

        policy = AdaptationPolicy{};
        policy.attempt_window =
            hotstuff::kMaximumAdaptationAttemptWindow + 1;
        CHECK_THROWS_AS(
            call({0}, evidence_view(no_records), policy),
            std::invalid_argument);

        policy = AdaptationPolicy{};
        policy.minimum_attempts = policy.attempt_window + 1;
        CHECK_THROWS_AS(
            call({0}, evidence_view(no_records), policy),
            std::invalid_argument);
    }

    SECTION("trailing miss streak lies in two through window")
    {
        auto policy = AdaptationPolicy{};
        policy.trailing_timeout_streak = 1;
        CHECK_THROWS_AS(
            call({0}, evidence_view(no_records), policy),
            std::invalid_argument);

        policy = AdaptationPolicy{};
        policy.trailing_timeout_streak = policy.attempt_window + 1;
        CHECK_THROWS_AS(
            call({0}, evidence_view(no_records), policy),
            std::invalid_argument);
    }

    SECTION("ppm thresholds cannot exceed one million")
    {
        auto policy = AdaptationPolicy{};
        policy.minimum_response_rate_ppm =
            hotstuff::kRatePpmScale + 1;
        CHECK_THROWS_AS(
            call({0}, evidence_view(no_records), policy),
            std::invalid_argument);

        policy = AdaptationPolicy{};
        policy.maximum_timeout_rate_ppm =
            hotstuff::kRatePpmScale + 1;
        CHECK_THROWS_AS(
            call({0}, evidence_view(no_records), policy),
            std::invalid_argument);
    }

    SECTION("percentile basis points lie in one through ten thousand")
    {
        auto policy = AdaptationPolicy{};
        policy.latency_percentile_basis_points = 0;
        CHECK_THROWS_AS(
            call({0}, evidence_view(no_records), policy),
            std::invalid_argument);

        policy = AdaptationPolicy{};
        policy.latency_percentile_basis_points =
            hotstuff::kPercentileBasisPointScale + 1;
        CHECK_THROWS_AS(
            call({0}, evidence_view(no_records), policy),
            std::invalid_argument);
    }
}

TEST_CASE("members without attempts remain explicitly unmeasured",
          "[r09][adaptation][latency][insufficient][intentional-red]")
{
    const auto epoch = epoch_id(27, "r09-unmeasured");
    const std::vector<AcceptedEvidenceRecord> records;
    const auto value = snapshot({4}, epoch, records, 0);
    const auto &result = result_for(value, 4);

    CHECK(result.classification ==
          ResponsivenessClass::insufficient_evidence);
    CHECK_FALSE(result.eligible);
    CHECK(result.attempt_count == 0);
    CHECK(result.response_rate_ppm == 0);
    CHECK(result.timeout_rate_ppm == 0);
    CHECK_FALSE(result.latency_percentile_us.has_value());
    CHECK(result.reasons == std::vector<ResponsivenessReason>{
                                ResponsivenessReason::insufficient_attempts});
}

TEST_CASE("adaptation source remains a pure evidence ranking policy",
          "[r09][adaptation][source-audit][purity][intentional-red]")
{
    const auto header = read_source("include/hotstuff/adaptation.h");
    const auto source = read_source("src/adaptation.cpp");
    const auto implementation = header + "\n" + source;

    CHECK(header.find("AcceptedEvidenceRecord") != std::string::npos);
    CHECK(header.find("AdaptationEpochId") != std::string::npos);
    CHECK(header.find("AdaptationPolicy") != std::string::npos);
    CHECK(header.find("AdaptationSnapshot") != std::string::npos);
    CHECK(header.find("build_adaptation_snapshot") !=
          std::string::npos);

    INFO("Accepted evidence is a synchronous borrowed ledger view");
    for (const auto *required_contract :
         {"directly from EvidenceLedger::accepted()",
          "healthy EvidenceLedger",
          "externally serialized",
          "stable for the call duration",
          "not retained"})
    {
        INFO("Missing API contract phrase: " << required_contract);
        CHECK(header.find(required_contract) != std::string::npos);
    }

    INFO("R09 uses fixed-width integer rates and nearest-rank arithmetic");
    CHECK(implementation.find("double") == std::string::npos);
    CHECK(implementation.find("float") == std::string::npos);

    INFO("R09 cannot own tree generation or consensus behavior");
    for (const auto *forbidden :
         {"EpochTreeDefinition",
          "members_breadth_first",
          "LeaderViewId",
          "HotStuffBase",
          "HotStuffCore",
          "AggregationContext"})
    {
        CHECK(implementation.find(forbidden) == std::string::npos);
    }

    INFO("R09 cannot inspect manager, network, crash, or profile state");
    for (const auto *forbidden :
         {"AdaptationManager",
          "adaptation_manager",
          "PeerNetwork",
          "MsgNetwork",
          "EventContext",
          "crash",
          "Crash",
          "profile",
          "Profile",
          "pid_t"})
    {
        CHECK(implementation.find(forbidden) == std::string::npos);
    }

    INFO("The pure policy implementation has no dependency on runtime owners");
    CHECK(source.find("#include \"hotstuff/hotstuff.h\"") ==
          std::string::npos);
    CHECK(source.find("#include \"hotstuff/aggregation.h\"") ==
          std::string::npos);
    CHECK(source.find("#include \"hotstuff/leader_progress.h\"") ==
          std::string::npos);
    CHECK(source.find("#include \"hotstuff/epoch_store.h\"") ==
          std::string::npos);
}
