#include "hotstuff/adaptation.h"

#include <algorithm>
#include <limits>
#include <map>
#include <stdexcept>
#include <type_traits>
#include <utility>

namespace hotstuff
{
namespace
{

constexpr char kSnapshotDomain[] = "kauri-adaptation-snapshot-v1";
constexpr char kDirectVoteResponsivenessPolicy[] =
    "shape25-direct-vote-responsiveness-v2";
constexpr std::size_t kDigestBytes = 32;
constexpr std::size_t kMaximumAcceptedSigners = 4'096;

enum class AttemptState : std::uint8_t
{
    on_time,
    timeout_only,
    late,
};

struct Attempt
{
    ResponseAttemptIdentity identity;
    std::uint64_t first_ingestion_sequence{0};
    std::uint64_t deadline_duration_us{0};
    std::uint64_t response_duration_us{0};
    AttemptState state{AttemptState::on_time};
};

template <typename UInt>
void append_big_endian(bytearray_t &output, UInt value)
{
    static_assert(std::is_unsigned<UInt>::value,
                  "canonical integers must be unsigned");
    for (std::size_t shift = sizeof(UInt); shift > 0; --shift)
    {
        output.push_back(static_cast<std::uint8_t>(
            value >> ((shift - 1) * 8)));
    }
}

void append_digest(bytearray_t &output, const uint256_t &digest)
{
    const bytearray_t bytes = static_cast<bytearray_t>(digest);
    if (bytes.size() != kDigestBytes)
        throw std::logic_error("adaptation digest is not 32 bytes");
    output.insert(output.end(), bytes.begin(), bytes.end());
}

void append_string(bytearray_t &output, const std::string &value)
{
    append_big_endian(
        output, static_cast<std::uint32_t>(value.size()));
    output.insert(output.end(), value.begin(), value.end());
}

void validate_fixed_bounds(
    const std::vector<ReplicaID> &membership,
    const AdaptationEpochId &current_epoch,
    AcceptedEvidenceView accepted_evidence,
    const AdaptationPolicy &policy)
{
    if (membership.empty() ||
        membership.size() > kMaximumAdaptationMembers)
    {
        throw std::invalid_argument(
            "adaptation membership count is out of bounds");
    }
    if (accepted_evidence.size >
        kMaximumAdaptationEvidenceRecords)
    {
        throw std::invalid_argument(
            "adaptation evidence count is out of bounds");
    }
    if (accepted_evidence.size != 0 &&
        accepted_evidence.data == nullptr)
    {
        throw std::invalid_argument(
            "adaptation evidence view has no data");
    }
    if (policy.schema_version != kAdaptationSchemaVersion)
        throw std::invalid_argument("unsupported adaptation schema");
    if (policy.policy_version.empty() ||
        policy.policy_version.size() >
            kMaximumAdaptationPolicyVersionBytes)
    {
        throw std::invalid_argument(
            "adaptation policy version is out of bounds");
    }
    if (policy.attempt_window == 0 ||
        policy.minimum_attempts == 0 ||
        policy.attempt_window > kMaximumAdaptationAttemptWindow ||
        policy.minimum_attempts > policy.attempt_window)
    {
        throw std::invalid_argument(
            "adaptation attempt bounds are invalid");
    }
    if (policy.trailing_timeout_streak < 2 ||
        policy.trailing_timeout_streak > policy.attempt_window)
    {
        throw std::invalid_argument(
            "adaptation timeout streak is out of bounds");
    }
    if (policy.minimum_response_rate_ppm > kRatePpmScale ||
        policy.maximum_timeout_rate_ppm > kRatePpmScale)
    {
        throw std::invalid_argument(
            "adaptation rate threshold is out of bounds");
    }
    if (policy.latency_percentile_basis_points == 0 ||
        policy.latency_percentile_basis_points >
            kPercentileBasisPointScale)
    {
        throw std::invalid_argument(
            "adaptation percentile is out of bounds");
    }
    if (current_epoch.epoch_digest.is_null())
        throw std::invalid_argument("adaptation epoch digest is missing");
}

std::vector<ReplicaID> canonical_membership(
    const std::vector<ReplicaID> &membership)
{
    auto ordered = membership;
    std::sort(ordered.begin(), ordered.end());
    if (std::adjacent_find(ordered.begin(), ordered.end()) !=
        ordered.end())
    {
        throw std::invalid_argument(
            "adaptation membership contains duplicates");
    }
    return ordered;
}

bool belongs_to_epoch(
    const ResponseObservation &observation,
    const AdaptationEpochId &epoch) noexcept
{
    return observation.configuration.epoch_number ==
               epoch.epoch_number &&
           observation.configuration.epoch_digest ==
               epoch.epoch_digest;
}

std::vector<const AcceptedEvidenceRecord *> canonical_evidence(
    AcceptedEvidenceView accepted_evidence,
    const AdaptationEpochId &current_epoch,
    std::uint64_t evidence_cutoff)
{
    std::vector<const AcceptedEvidenceRecord *> ordered;
    ordered.reserve(accepted_evidence.size);
    for (std::size_t index = 0; index < accepted_evidence.size; ++index)
    {
        const auto *record = accepted_evidence.data + index;
        if (record->ingestion_sequence <= evidence_cutoff &&
            belongs_to_epoch(record->observation, current_epoch))
        {
            ordered.push_back(record);
        }
    }
    std::sort(
        ordered.begin(), ordered.end(),
        [](const auto *left, const auto *right) {
            return left->ingestion_sequence <
                   right->ingestion_sequence;
        });
    for (std::size_t index = 0; index < ordered.size(); ++index)
    {
        if (ordered[index]->ingestion_sequence == 0)
        {
            throw std::invalid_argument(
                "adaptation evidence has a zero ingestion sequence");
        }
        if (index != 0 &&
            ordered[index - 1]->ingestion_sequence ==
                ordered[index]->ingestion_sequence)
        {
            throw std::invalid_argument(
                "adaptation evidence has duplicate ingestion sequences");
        }
    }
    return ordered;
}

bool valid_message_type(ExpectedMessageType type) noexcept
{
    return type == ExpectedMessageType::direct_vote ||
           type == ExpectedMessageType::aggregate_relay;
}

bool valid_outcome(ResponseOutcome outcome) noexcept
{
    return outcome == ResponseOutcome::on_time ||
           outcome == ResponseOutcome::timeout ||
           outcome == ResponseOutcome::late;
}

bool canonical_signers(const std::vector<ReplicaID> &signers) noexcept
{
    return std::adjacent_find(
               signers.begin(), signers.end(),
               [](ReplicaID left, ReplicaID right) {
                   return left >= right;
               }) == signers.end();
}

void validate_observation(const ResponseObservation &observation)
{
    if (observation.schema_version !=
        kResponseObservationSchemaVersion)
    {
        throw std::invalid_argument(
            "unsupported adaptation evidence schema");
    }
    if (observation.observation_id !=
        compute_response_observation_id(
            observation.attempt_identity()))
    {
        throw std::invalid_argument(
            "adaptation evidence identity does not match");
    }
    if (!valid_message_type(observation.expected_message_type))
    {
        throw std::invalid_argument(
            "adaptation evidence message type is invalid");
    }
    if (!valid_outcome(observation.outcome))
        throw std::invalid_argument("adaptation evidence outcome is invalid");
    if (observation.deadline_duration_us == 0)
        throw std::invalid_argument("adaptation evidence deadline is zero");
    if (observation.outcome == ResponseOutcome::timeout &&
        observation.response_duration_us != 0)
    {
        throw std::invalid_argument(
            "adaptation timeout has a response duration");
    }
    if (observation.outcome == ResponseOutcome::late &&
        observation.response_duration_us <
            observation.deadline_duration_us)
    {
        throw std::invalid_argument(
            "adaptation late response precedes its deadline");
    }
    if (observation.signer_set.size() > kMaximumAcceptedSigners ||
        !canonical_signers(observation.signer_set))
    {
        throw std::invalid_argument(
            "adaptation evidence signer set is invalid");
    }
    if (observation.outcome == ResponseOutcome::timeout)
    {
        if (!observation.signer_set.empty())
        {
            throw std::invalid_argument(
                "adaptation timeout contains signers");
        }
    }
    else if (observation.signer_set.empty())
    {
        throw std::invalid_argument(
            "adaptation response has no signers");
    }
}

void validate_current_members(
    const ResponseObservation &observation,
    const std::vector<ReplicaID> &membership)
{
    if (!std::binary_search(
            membership.begin(), membership.end(),
            observation.observed_replica_id))
    {
        throw std::invalid_argument(
            "adaptation evidence observes a nonmember");
    }
    for (const auto signer : observation.signer_set)
    {
        if (!std::binary_search(
                membership.begin(), membership.end(), signer))
        {
            throw std::invalid_argument(
                "adaptation evidence contains a nonmember signer");
        }
    }
}

void apply_observation(
    std::map<uint256_t, Attempt> &attempts,
    const AcceptedEvidenceRecord &record)
{
    const auto &observation = record.observation;
    const auto found = attempts.find(observation.observation_id);
    if (found == attempts.end())
    {
        if (observation.outcome == ResponseOutcome::late)
        {
            throw std::invalid_argument(
                "adaptation evidence starts with a late response");
        }
        Attempt attempt;
        attempt.identity = observation.attempt_identity();
        attempt.first_ingestion_sequence = record.ingestion_sequence;
        attempt.deadline_duration_us =
            observation.deadline_duration_us;
        attempt.response_duration_us =
            observation.response_duration_us;
        attempt.state = observation.outcome == ResponseOutcome::timeout
                            ? AttemptState::timeout_only
                            : AttemptState::on_time;
        attempts.emplace(observation.observation_id, std::move(attempt));
        return;
    }

    auto &attempt = found->second;
    if (attempt.identity != observation.attempt_identity() ||
        attempt.state != AttemptState::timeout_only ||
        observation.outcome != ResponseOutcome::late ||
        attempt.deadline_duration_us !=
            observation.deadline_duration_us)
    {
        throw std::invalid_argument(
            "adaptation evidence transition is invalid");
    }
    attempt.state = AttemptState::late;
    attempt.response_duration_us = observation.response_duration_us;
}

RatePpm rate_ppm(std::uint32_t count, std::uint32_t total) noexcept
{
    if (total == 0)
        return 0;
    return static_cast<RatePpm>(
        (static_cast<std::uint64_t>(count) * kRatePpmScale) /
        total);
}

std::optional<std::uint64_t> nearest_rank_latency(
    std::vector<std::uint64_t> values,
    std::uint16_t percentile_basis_points)
{
    if (values.empty())
        return std::nullopt;
    std::sort(values.begin(), values.end());
    const auto numerator =
        static_cast<std::uint64_t>(percentile_basis_points) *
        values.size();
    const auto rank =
        (numerator + kPercentileBasisPointScale - 1) /
        kPercentileBasisPointScale;
    return values.at(static_cast<std::size_t>(rank - 1));
}

ReplicaAdaptationResult score_replica(
    ReplicaID replica,
    std::vector<const Attempt *> attempts,
    const AdaptationPolicy &policy)
{
    std::sort(
        attempts.begin(), attempts.end(),
        [](const auto *left, const auto *right) {
            return left->first_ingestion_sequence <
                   right->first_ingestion_sequence;
        });
    if (attempts.size() > policy.attempt_window)
    {
        attempts.erase(
            attempts.begin(),
            attempts.end() - policy.attempt_window);
    }

    ReplicaAdaptationResult result;
    result.replica_id = replica;
    result.attempt_count =
        static_cast<std::uint32_t>(attempts.size());
    std::vector<std::uint64_t> latencies;
    latencies.reserve(attempts.size());
    for (const auto *attempt : attempts)
    {
        switch (attempt->state)
        {
        case AttemptState::on_time:
            ++result.on_time_count;
            ++result.response_count;
            latencies.push_back(attempt->response_duration_us);
            break;
        case AttemptState::timeout_only:
            ++result.timeout_only_count;
            ++result.timeout_count;
            break;
        case AttemptState::late:
            ++result.late_count;
            ++result.response_count;
            ++result.timeout_count;
            latencies.push_back(attempt->response_duration_us);
            break;
        }
    }
    for (auto iterator = attempts.rbegin();
         iterator != attempts.rend() &&
         (*iterator)->state == AttemptState::timeout_only;
         ++iterator)
    {
        ++result.trailing_timeout_count;
    }

    result.response_rate_ppm =
        rate_ppm(result.response_count, result.attempt_count);
    result.timeout_rate_ppm =
        rate_ppm(result.timeout_count, result.attempt_count);
    result.latency_percentile_us = nearest_rank_latency(
        std::move(latencies),
        policy.latency_percentile_basis_points);

    if (result.attempt_count < policy.minimum_attempts)
    {
        result.classification =
            ResponsivenessClass::insufficient_evidence;
        result.reasons.push_back(
            ResponsivenessReason::insufficient_attempts);
        return result;
    }

    if (result.response_rate_ppm <
        policy.minimum_response_rate_ppm)
    {
        result.reasons.push_back(
            ResponsivenessReason::response_rate_below_minimum);
    }
    if (result.timeout_rate_ppm >
        policy.maximum_timeout_rate_ppm)
    {
        result.reasons.push_back(
            ResponsivenessReason::timeout_rate_above_maximum);
    }
    if (result.trailing_timeout_count >=
        policy.trailing_timeout_streak)
    {
        result.reasons.push_back(
            ResponsivenessReason::persistent_timeout_streak);
    }

    if (result.reasons.empty())
    {
        result.classification = ResponsivenessClass::responsive;
        result.eligible = true;
    }
    else
    {
        result.classification = ResponsivenessClass::nonresponsive;
    }
    return result;
}

bool ranks_before(const ReplicaAdaptationResult &left,
                  const ReplicaAdaptationResult &right) noexcept
{
    if (left.eligible != right.eligible)
        return left.eligible;
    if (left.response_rate_ppm != right.response_rate_ppm)
        return left.response_rate_ppm > right.response_rate_ppm;
    if (left.timeout_rate_ppm != right.timeout_rate_ppm)
        return left.timeout_rate_ppm < right.timeout_rate_ppm;
    if (left.latency_percentile_us.has_value() !=
        right.latency_percentile_us.has_value())
    {
        return left.latency_percentile_us.has_value();
    }
    if (left.latency_percentile_us.has_value() &&
        left.latency_percentile_us != right.latency_percentile_us)
    {
        return *left.latency_percentile_us <
               *right.latency_percentile_us;
    }
    if (left.attempt_count != right.attempt_count)
        return left.attempt_count > right.attempt_count;
    return left.replica_id < right.replica_id;
}

std::string compute_snapshot_id(
    const std::vector<ReplicaID> &membership,
    const AdaptationEpochId &epoch,
    const std::vector<const AcceptedEvidenceRecord *> &records,
    std::uint64_t cutoff,
    const AdaptationPolicy &policy,
    std::uint64_t seed)
{
    bytearray_t bytes(
        kSnapshotDomain,
        kSnapshotDomain + sizeof(kSnapshotDomain) - 1);
    append_big_endian(bytes, kAdaptationSchemaVersion);
    append_big_endian(bytes, epoch.epoch_number);
    append_digest(bytes, epoch.epoch_digest);
    append_big_endian(bytes, cutoff);
    append_big_endian(bytes, seed);
    append_big_endian(
        bytes, static_cast<std::uint32_t>(membership.size()));
    for (const auto replica : membership)
        append_big_endian(bytes, replica);

    append_big_endian(bytes, policy.schema_version);
    append_string(bytes, policy.policy_version);
    append_big_endian(bytes, policy.attempt_window);
    append_big_endian(bytes, policy.minimum_attempts);
    append_big_endian(bytes, policy.minimum_response_rate_ppm);
    append_big_endian(bytes, policy.maximum_timeout_rate_ppm);
    append_big_endian(bytes, policy.trailing_timeout_streak);
    append_big_endian(
        bytes, policy.latency_percentile_basis_points);

    append_big_endian(
        bytes, static_cast<std::uint32_t>(records.size()));
    for (const auto *record : records)
    {
        const auto &observation = record->observation;
        append_big_endian(bytes, record->ingestion_sequence);
        append_big_endian(bytes, observation.schema_version);
        append_digest(bytes, observation.observation_id);
        append_big_endian(bytes, observation.reporter_id);
        append_big_endian(bytes, observation.observed_replica_id);
        append_big_endian(
            bytes, observation.configuration.epoch_number);
        append_big_endian(bytes, observation.configuration.tree_id);
        append_digest(
            bytes, observation.configuration.epoch_digest);
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
        append_big_endian(
            bytes,
            static_cast<std::uint32_t>(observation.signer_set.size()));
        for (const auto signer : observation.signer_set)
            append_big_endian(bytes, signer);
    }
    return DataStream(bytes).get_hash().to_hex();
}

} // namespace

AdaptationSnapshot build_adaptation_snapshot(
    const std::vector<ReplicaID> &membership,
    const AdaptationEpochId &current_epoch,
    AcceptedEvidenceView accepted_evidence,
    std::uint64_t evidence_cutoff,
    const AdaptationPolicy &policy,
    std::uint64_t seed)
{
    validate_fixed_bounds(
        membership, current_epoch, accepted_evidence, policy);
    const auto members = canonical_membership(membership);
    const auto records = canonical_evidence(
        accepted_evidence, current_epoch, evidence_cutoff);

    std::map<uint256_t, Attempt> attempts;
    for (const auto *record : records)
    {
        validate_observation(record->observation);
        validate_current_members(record->observation, members);
        apply_observation(attempts, *record);
    }

    std::map<ReplicaID, std::vector<const Attempt *>> by_replica;
    const bool direct_vote_only =
        policy.policy_version == kDirectVoteResponsivenessPolicy;
    for (const auto &entry : attempts)
    {
        const auto &attempt = entry.second;
        if (direct_vote_only &&
            attempt.identity.expected_message_type !=
                ExpectedMessageType::direct_vote)
        {
            continue;
        }
        by_replica[attempt.identity.observed_replica_id]
            .push_back(&attempt);
    }

    std::vector<ReplicaAdaptationResult> ranking;
    ranking.reserve(members.size());
    for (const auto replica : members)
    {
        const auto found = by_replica.find(replica);
        ranking.push_back(score_replica(
            replica,
            found == by_replica.end()
                ? std::vector<const Attempt *>{}
                : found->second,
            policy));
    }
    std::sort(ranking.begin(), ranking.end(), ranks_before);
    for (std::size_t rank = 0; rank < ranking.size(); ++rank)
        ranking[rank].rank = static_cast<std::uint32_t>(rank);

    AdaptationSnapshot snapshot;
    snapshot.schema_version_ = kAdaptationSchemaVersion;
    snapshot.snapshot_id_ = compute_snapshot_id(
        members,
        current_epoch,
        records,
        evidence_cutoff,
        policy,
        seed);
    snapshot.epoch_ = current_epoch;
    snapshot.evidence_cutoff_ = evidence_cutoff;
    snapshot.accepted_record_count_ = records.size();
    snapshot.policy_ = policy;
    snapshot.seed_ = seed;
    snapshot.ranking_ = std::move(ranking);
    return snapshot;
}

} // namespace hotstuff
