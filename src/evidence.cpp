#include "hotstuff/evidence.h"

#include <algorithm>
#include <limits>
#include <map>
#include <new>
#include <stdexcept>
#include <type_traits>
#include <utility>

#include "hotstuff/epoch_store.h"

namespace hotstuff
{
namespace
{

constexpr char kObservationDomain[] =
    "kauri-response-observation-v1";
constexpr std::size_t kDigestSize = 32;
constexpr std::size_t kBatchFixedSize = 8;
constexpr std::size_t kObservationFixedSize = 150;

template <typename UInt>
void append_big_endian(bytearray_t &output, UInt value)
{
    static_assert(std::is_unsigned<UInt>::value,
                  "canonical evidence integers must be unsigned");
    for (std::size_t shift = sizeof(UInt); shift > 0; --shift)
    {
        output.push_back(static_cast<std::uint8_t>(
            value >> ((shift - 1) * 8)));
    }
}

void append_digest(bytearray_t &output, const uint256_t &digest)
{
    const bytearray_t bytes = static_cast<bytearray_t>(digest);
    if (bytes.size() != kDigestSize)
    {
        throw std::logic_error(
            "canonical evidence digest is not 32 bytes");
    }
    output.insert(output.end(), bytes.begin(), bytes.end());
}

bool valid_message_type(ExpectedMessageType type) noexcept
{
    switch (type)
    {
    case ExpectedMessageType::direct_vote:
    case ExpectedMessageType::aggregate_relay:
    case ExpectedMessageType::leader_progress:
        return true;
    }
    return false;
}

bool valid_outcome(ResponseOutcome outcome) noexcept
{
    switch (outcome)
    {
    case ResponseOutcome::on_time:
    case ResponseOutcome::timeout:
    case ResponseOutcome::late:
        return true;
    }
    return false;
}

bool canonical_signers(const std::vector<ReplicaID> &signers) noexcept
{
    return std::adjacent_find(
               signers.begin(), signers.end(),
               [](ReplicaID left, ReplicaID right) {
                   return left >= right;
               }) == signers.end();
}

void add_encoded_size(std::size_t &size,
                      std::size_t amount,
                      std::size_t maximum)
{
    if (size > maximum || amount > maximum - size)
        throw std::length_error("evidence payload exceeds byte limit");
    size += amount;
}

class EvidenceReader final
{
public:
    explicit EvidenceReader(const bytearray_t &payload)
        : payload_(payload)
    {}

    template <typename UInt>
    bool read(UInt &value) noexcept
    {
        static_assert(std::is_unsigned<UInt>::value,
                      "canonical evidence integers must be unsigned");
        if (remaining() < sizeof(UInt))
            return false;
        value = 0;
        for (std::size_t index = 0; index < sizeof(UInt); ++index)
        {
            value = static_cast<UInt>(
                (value << 8) | payload_[offset_++]);
        }
        return true;
    }

    bool read_digest(uint256_t &digest)
    {
        if (remaining() < kDigestSize)
            return false;
        digest = uint256_t(payload_.data() + offset_);
        offset_ += kDigestSize;
        return true;
    }

    std::size_t remaining() const noexcept
    {
        return payload_.size() - offset_;
    }

private:
    const bytearray_t &payload_;
    std::size_t offset_{0};
};

EvidenceDecodeResult decode_failure(EvidenceWireError error)
{
    return {error, std::nullopt};
}

EvidenceDecodeResult decode_evidence_batch_impl(
    const bytearray_t &payload,
    const EvidenceWireLimits &limits)
{
    if (payload.size() > limits.maximum_payload_bytes)
        return decode_failure(EvidenceWireError::payload_too_large);

    EvidenceReader reader(payload);
    std::uint32_t batch_schema = 0;
    if (!reader.read(batch_schema))
        return decode_failure(EvidenceWireError::truncated);
    if (batch_schema != kEvidenceBatchSchemaVersion)
    {
        return decode_failure(
            EvidenceWireError::unsupported_batch_schema);
    }

    std::uint32_t observation_count = 0;
    if (!reader.read(observation_count))
        return decode_failure(EvidenceWireError::truncated);
    if (observation_count > limits.maximum_observations)
        return decode_failure(EvidenceWireError::batch_count_exceeded);

    if (observation_count > reader.remaining() / kObservationFixedSize)
        return decode_failure(EvidenceWireError::truncated);

    ResponseObservationBatch batch;
    batch.schema_version = batch_schema;
    batch.observations.reserve(observation_count);

    for (std::uint32_t index = 0; index < observation_count; ++index)
    {
        if (reader.remaining() < kObservationFixedSize)
            return decode_failure(EvidenceWireError::truncated);

        ResponseObservation observation;
        if (!reader.read(observation.schema_version))
            return decode_failure(EvidenceWireError::truncated);
        if (observation.schema_version !=
            kResponseObservationSchemaVersion)
        {
            return decode_failure(
                EvidenceWireError::unsupported_observation_schema);
        }
        if (!reader.read_digest(observation.observation_id) ||
            !reader.read(observation.reporter_id) ||
            !reader.read(observation.observed_replica_id) ||
            !reader.read(observation.configuration.epoch_number) ||
            !reader.read(observation.configuration.tree_id) ||
            !reader.read_digest(
                observation.configuration.epoch_digest) ||
            !reader.read_digest(observation.block_hash))
        {
            return decode_failure(EvidenceWireError::truncated);
        }

        std::uint8_t message_type = 0;
        if (!reader.read(message_type))
            return decode_failure(EvidenceWireError::truncated);
        observation.expected_message_type =
            static_cast<ExpectedMessageType>(message_type);
        if (!valid_message_type(observation.expected_message_type))
        {
            return decode_failure(
                EvidenceWireError::invalid_expected_message_type);
        }

        std::uint8_t outcome = 0;
        if (!reader.read(outcome))
            return decode_failure(EvidenceWireError::truncated);
        observation.outcome = static_cast<ResponseOutcome>(outcome);
        if (!valid_outcome(observation.outcome))
            return decode_failure(EvidenceWireError::invalid_outcome);

        if (!reader.read(observation.response_duration_us) ||
            !reader.read(observation.deadline_duration_us) ||
            !reader.read(observation.reporter_monotonic_ns) ||
            !reader.read(observation.reporter_sequence))
        {
            return decode_failure(EvidenceWireError::truncated);
        }

        std::uint32_t signer_count = 0;
        if (!reader.read(signer_count))
            return decode_failure(EvidenceWireError::truncated);
        if (signer_count > limits.maximum_signers_per_observation)
        {
            return decode_failure(
                EvidenceWireError::signer_count_exceeded);
        }
        if (signer_count > reader.remaining() / sizeof(ReplicaID))
            return decode_failure(EvidenceWireError::truncated);

        observation.signer_set.reserve(signer_count);
        for (std::uint32_t signer_index = 0;
             signer_index < signer_count;
             ++signer_index)
        {
            ReplicaID signer = 0;
            if (!reader.read(signer))
                return decode_failure(EvidenceWireError::truncated);
            if (!observation.signer_set.empty() &&
                observation.signer_set.back() >= signer)
            {
                return decode_failure(
                    EvidenceWireError::noncanonical_signer_set);
            }
            observation.signer_set.push_back(signer);
        }
        batch.observations.push_back(std::move(observation));
    }

    if (reader.remaining() != 0)
        return decode_failure(EvidenceWireError::trailing_bytes);
    return {EvidenceWireError::none, std::move(batch)};
}

struct DirectChildTopology
{
    const EpochTreeDefinition *tree{nullptr};
    std::size_t observed_position{0};
    bool observed_is_internal{false};
};

std::optional<std::size_t> first_child_position(
    std::size_t parent_position,
    std::uint32_t fanout,
    std::size_t member_count) noexcept
{
    if (fanout == 0 ||
        parent_position >
            (std::numeric_limits<std::size_t>::max() - 1) / fanout)
    {
        return std::nullopt;
    }
    const auto first = parent_position * fanout + 1;
    if (first >= member_count)
        return std::nullopt;
    return first;
}

std::optional<DirectChildTopology> direct_child_topology(
    const EpochTreeDefinition &tree,
    ReplicaID reporter,
    ReplicaID observed)
{
    const auto reporter_iterator = std::find(
        tree.members_breadth_first.begin(),
        tree.members_breadth_first.end(),
        reporter);
    const auto observed_iterator = std::find(
        tree.members_breadth_first.begin(),
        tree.members_breadth_first.end(),
        observed);
    if (reporter_iterator == tree.members_breadth_first.end() ||
        observed_iterator == tree.members_breadth_first.end())
    {
        return std::nullopt;
    }

    const auto reporter_position = static_cast<std::size_t>(
        reporter_iterator - tree.members_breadth_first.begin());
    const auto observed_position = static_cast<std::size_t>(
        observed_iterator - tree.members_breadth_first.begin());
    const auto first = first_child_position(
        reporter_position,
        tree.fanout,
        tree.members_breadth_first.size());
    if (!first.has_value())
        return std::nullopt;
    const auto child_count = std::min<std::size_t>(
        tree.fanout,
        tree.members_breadth_first.size() - *first);
    if (observed_position < *first ||
        observed_position >= *first + child_count)
    {
        return std::nullopt;
    }

    return DirectChildTopology{
        &tree,
        observed_position,
        first_child_position(
            observed_position,
            tree.fanout,
            tree.members_breadth_first.size())
            .has_value()};
}

bool position_is_in_subtree(std::size_t position,
                            std::size_t subtree_root,
                            std::uint32_t fanout) noexcept
{
    if (fanout == 0 || position < subtree_root)
        return false;
    while (position > subtree_root)
        position = (position - 1) / fanout;
    return position == subtree_root;
}

bool valid_timing(const ResponseObservation &observation) noexcept
{
    if (observation.deadline_duration_us == 0)
        return false;
    switch (observation.outcome)
    {
    case ResponseOutcome::on_time:
        // The observed protocol transition is authoritative. A delayed event
        // loop can run the local timeout callback after an on-time response.
        return true;
    case ResponseOutcome::timeout:
        return observation.response_duration_us == 0;
    case ResponseOutcome::late:
        return observation.response_duration_us >=
               observation.deadline_duration_us;
    }
    return false;
}

bool valid_signer_claim(const ResponseObservation &observation,
                        const DirectChildTopology &topology)
{
    if (!canonical_signers(observation.signer_set))
        return false;
    if (observation.outcome == ResponseOutcome::timeout)
        return observation.signer_set.empty();

    if (!topology.observed_is_internal)
    {
        return observation.signer_set.size() == 1 &&
               observation.signer_set.front() ==
                   observation.observed_replica_id;
    }

    if (observation.signer_set.empty())
        return false;
    for (const auto signer : observation.signer_set)
    {
        const auto signer_iterator = std::find(
            topology.tree->members_breadth_first.begin(),
            topology.tree->members_breadth_first.end(),
            signer);
        if (signer_iterator ==
            topology.tree->members_breadth_first.end())
        {
            return false;
        }
        const auto signer_position = static_cast<std::size_t>(
            signer_iterator -
            topology.tree->members_breadth_first.begin());
        if (!position_is_in_subtree(
                signer_position,
                topology.observed_position,
                topology.tree->fanout))
        {
            return false;
        }
    }
    return true;
}

enum class AttemptPhase
{
    on_time,
    timeout,
    late,
};

struct AttemptProgress
{
    AttemptPhase phase{AttemptPhase::on_time};
    std::uint64_t deadline_duration_us{0};
};

struct ReporterProgress
{
    std::uint64_t sequence{0};
    std::uint64_t monotonic_ns{0};
};

static_assert(
    std::is_nothrow_move_constructible<AcceptedEvidenceRecord>::value,
    "accepted evidence commits require a no-throw record move");
static_assert(
    std::is_nothrow_move_constructible<RejectedEvidenceRecord>::value,
    "rejected evidence commits require a no-throw record move");

std::optional<EvidenceRejectionReason> transition_rejection(
    const std::map<uint256_t, AttemptProgress> &attempts,
    const ResponseObservation &observation)
{
    const auto found = attempts.find(observation.observation_id);
    if (found == attempts.end())
    {
        return observation.outcome == ResponseOutcome::late
                   ? std::optional<EvidenceRejectionReason>(
                         EvidenceRejectionReason::invalid_transition)
                   : std::nullopt;
    }

    const auto same_fact =
        (found->second.phase == AttemptPhase::on_time &&
         observation.outcome == ResponseOutcome::on_time) ||
        (found->second.phase == AttemptPhase::timeout &&
         observation.outcome == ResponseOutcome::timeout) ||
        (found->second.phase == AttemptPhase::late &&
         observation.outcome == ResponseOutcome::late);
    if (same_fact)
        return EvidenceRejectionReason::duplicate_fact;

    if (found->second.phase == AttemptPhase::timeout &&
        observation.outcome == ResponseOutcome::late &&
        observation.deadline_duration_us ==
            found->second.deadline_duration_us)
    {
        return std::nullopt;
    }
    return EvidenceRejectionReason::invalid_transition;
}

} // namespace

struct EvidenceLedger::State
{
    using AttemptMap = std::map<uint256_t, AttemptProgress>;
    using ReporterMap = std::map<ReplicaID, ReporterProgress>;

    static_assert(
        std::is_nothrow_move_constructible<AttemptMap::node_type>::value,
        "attempt commits require a no-throw node move");
    static_assert(
        std::is_nothrow_move_constructible<ReporterMap::node_type>::value,
        "reporter commits require a no-throw node move");

    State(const EpochStore &epochs_,
          const ProposalEvidenceWindow &window_,
          EvidenceStoreLimits limits_)
        : epochs(epochs_), window(window_), limits(limits_)
    {}

    std::optional<EvidenceRejectionReason>
    proposal_independent_rejection(
        const AuthenticatedReporter &authenticated_reporter,
        const ResponseObservation &observation) const
    {
        if (observation.schema_version !=
            kResponseObservationSchemaVersion)
        {
            return EvidenceRejectionReason::unsupported_schema;
        }
        if (observation.observation_id !=
            compute_response_observation_id(
                observation.attempt_identity()))
        {
            return EvidenceRejectionReason::observation_id_mismatch;
        }
        if (authenticated_reporter.replica_id != observation.reporter_id)
            return EvidenceRejectionReason::reporter_mismatch;

        const auto *epoch = epochs.find_epoch(
            observation.configuration.epoch_number);
        const auto *tree = epochs.find_tree(
            observation.configuration.epoch_number,
            observation.configuration.tree_id);
        if (epoch == nullptr || tree == nullptr ||
            epoch->epoch_digest() != observation.configuration.epoch_digest)
        {
            return EvidenceRejectionReason::unknown_configuration;
        }

        const auto topology = direct_child_topology(
            *tree,
            observation.reporter_id,
            observation.observed_replica_id);
        if (!topology.has_value())
            return EvidenceRejectionReason::impossible_topology;

        const auto expected_message_type =
            topology->observed_is_internal
                ? ExpectedMessageType::aggregate_relay
                : ExpectedMessageType::direct_vote;
        if (observation.expected_message_type != expected_message_type)
        {
            return EvidenceRejectionReason::invalid_expected_message_type;
        }
        if (!valid_outcome(observation.outcome))
            return EvidenceRejectionReason::invalid_outcome;
        if (!valid_timing(observation))
            return EvidenceRejectionReason::invalid_timing;
        if (!canonical_signers(observation.signer_set))
            return EvidenceRejectionReason::invalid_signer_set;
        return std::nullopt;
    }

    std::optional<std::uint64_t> claim_ingestion_sequence() noexcept
    {
        if (high_watermark == std::numeric_limits<std::uint64_t>::max())
        {
            healthy = false;
            return std::nullopt;
        }
        return ++high_watermark;
    }

    void store_rejection(
        std::uint64_t ingestion_sequence,
        const AuthenticatedReporter &authenticated_reporter,
        EvidenceRejectionReason reason,
        const ResponseObservation *observation,
        std::optional<EvidenceWireError> wire_error)
    {
        if (rejected_records.size() >=
            limits.maximum_rejected_records)
        {
            healthy = false;
            return;
        }

        RejectedEvidenceRecord staged_record{
            ingestion_sequence,
            authenticated_reporter,
            reason,
            std::nullopt,
            wire_error};
        if (observation != nullptr)
            staged_record.observation.emplace(*observation);

        rejected_records.reserve(rejected_records.size() + 1);
        rejected_records.push_back(std::move(staged_record));
    }

    void store_acceptance(
        std::uint64_t ingestion_sequence,
        const ResponseObservation &observation)
    {
        AcceptedEvidenceRecord staged_record{
            ingestion_sequence, observation};

        const auto attempt = attempts.find(observation.observation_id);
        const bool insert_attempt = attempt == attempts.end();
        AttemptMap staged_attempts;
        if (insert_attempt)
        {
            const auto phase =
                observation.outcome == ResponseOutcome::timeout
                    ? AttemptPhase::timeout
                    : AttemptPhase::on_time;
            staged_attempts.emplace(
                observation.observation_id,
                AttemptProgress{
                    phase, observation.deadline_duration_us});
        }

        const auto reporter = reporters.find(observation.reporter_id);
        const bool insert_reporter = reporter == reporters.end();
        ReporterMap staged_reporters;
        if (insert_reporter)
        {
            staged_reporters.emplace(
                observation.reporter_id,
                ReporterProgress{
                    observation.reporter_sequence,
                    observation.reporter_monotonic_ns});
        }

        accepted_records.reserve(accepted_records.size() + 1);

        auto committed_attempt = attempts.end();
        auto committed_reporter = reporters.end();
        try
        {
            if (insert_attempt)
            {
                auto result = attempts.insert(
                    staged_attempts.extract(staged_attempts.begin()));
                if (!result.inserted)
                {
                    throw std::logic_error(
                        "evidence attempt changed during commit");
                }
                committed_attempt = result.position;
            }

            if (insert_reporter)
            {
                auto result = reporters.insert(
                    staged_reporters.extract(staged_reporters.begin()));
                if (!result.inserted)
                {
                    throw std::logic_error(
                        "evidence reporter changed during commit");
                }
                committed_reporter = result.position;
            }

            accepted_records.push_back(std::move(staged_record));
        }
        catch (...)
        {
            if (committed_reporter != reporters.end())
                reporters.erase(committed_reporter);
            if (committed_attempt != attempts.end())
                attempts.erase(committed_attempt);
            throw;
        }

        if (!insert_attempt)
            attempt->second.phase = AttemptPhase::late;
        if (!insert_reporter)
        {
            reporter->second.sequence = observation.reporter_sequence;
            reporter->second.monotonic_ns =
                observation.reporter_monotonic_ns;
        }
    }

    const EpochStore &epochs;
    const ProposalEvidenceWindow &window;
    EvidenceStoreLimits limits;
    std::uint64_t high_watermark{0};
    bool healthy{true};
    std::vector<AcceptedEvidenceRecord> accepted_records;
    std::vector<RejectedEvidenceRecord> rejected_records;
    AttemptMap attempts;
    ReporterMap reporters;
};

uint256_t compute_response_observation_id(
    const ResponseAttemptIdentity &identity)
{
    bytearray_t bytes(
        kObservationDomain,
        kObservationDomain + sizeof(kObservationDomain) - 1);
    append_big_endian(bytes, identity.reporter_id);
    append_big_endian(bytes, identity.observed_replica_id);
    append_big_endian(
        bytes, identity.proposal.configuration.epoch_number);
    append_big_endian(
        bytes, identity.proposal.configuration.tree_id);
    append_digest(
        bytes, identity.proposal.configuration.epoch_digest);
    append_digest(bytes, identity.proposal.block_hash);
    append_big_endian(
        bytes,
        static_cast<std::underlying_type_t<ExpectedMessageType>>(
            identity.expected_message_type));
    return DataStream(bytes).get_hash();
}

bytearray_t encode_evidence_batch(
    const ResponseObservationBatch &batch,
    const EvidenceWireLimits &limits)
{
    if (batch.schema_version != kEvidenceBatchSchemaVersion)
        throw std::invalid_argument("unsupported evidence batch schema");
    if (batch.observations.size() >
            std::numeric_limits<std::uint32_t>::max() ||
        batch.observations.size() > limits.maximum_observations)
    {
        throw std::length_error("evidence observation count exceeds limit");
    }

    std::size_t encoded_size = 0;
    add_encoded_size(
        encoded_size, kBatchFixedSize, limits.maximum_payload_bytes);
    for (const auto &observation : batch.observations)
    {
        if (observation.schema_version !=
            kResponseObservationSchemaVersion)
        {
            throw std::invalid_argument(
                "unsupported response observation schema");
        }
        if (!valid_message_type(observation.expected_message_type))
            throw std::invalid_argument("invalid expected message type");
        if (!valid_outcome(observation.outcome))
            throw std::invalid_argument("invalid response outcome");
        if (observation.signer_set.size() >
                std::numeric_limits<std::uint32_t>::max() ||
            observation.signer_set.size() >
                limits.maximum_signers_per_observation)
        {
            throw std::length_error("evidence signer count exceeds limit");
        }
        if (!canonical_signers(observation.signer_set))
            throw std::invalid_argument("noncanonical evidence signer set");
        if (observation.signer_set.size() >
            std::numeric_limits<std::size_t>::max() / sizeof(ReplicaID))
        {
            throw std::length_error("evidence signer bytes overflow");
        }
        add_encoded_size(
            encoded_size,
            kObservationFixedSize,
            limits.maximum_payload_bytes);
        add_encoded_size(
            encoded_size,
            observation.signer_set.size() * sizeof(ReplicaID),
            limits.maximum_payload_bytes);
    }

    bytearray_t payload;
    payload.reserve(encoded_size);
    append_big_endian(payload, batch.schema_version);
    append_big_endian(
        payload,
        static_cast<std::uint32_t>(batch.observations.size()));
    for (const auto &observation : batch.observations)
    {
        append_big_endian(payload, observation.schema_version);
        append_digest(payload, observation.observation_id);
        append_big_endian(payload, observation.reporter_id);
        append_big_endian(payload, observation.observed_replica_id);
        append_big_endian(
            payload, observation.configuration.epoch_number);
        append_big_endian(payload, observation.configuration.tree_id);
        append_digest(payload, observation.configuration.epoch_digest);
        append_digest(payload, observation.block_hash);
        append_big_endian(
            payload,
            static_cast<std::underlying_type_t<ExpectedMessageType>>(
                observation.expected_message_type));
        append_big_endian(
            payload,
            static_cast<std::underlying_type_t<ResponseOutcome>>(
                observation.outcome));
        append_big_endian(payload, observation.response_duration_us);
        append_big_endian(payload, observation.deadline_duration_us);
        append_big_endian(payload, observation.reporter_monotonic_ns);
        append_big_endian(payload, observation.reporter_sequence);
        append_big_endian(
            payload,
            static_cast<std::uint32_t>(observation.signer_set.size()));
        for (const auto signer : observation.signer_set)
            append_big_endian(payload, signer);
    }
    return payload;
}

EvidenceDecodeResult decode_evidence_batch(
    const bytearray_t &payload,
    const EvidenceWireLimits &limits) noexcept
{
    try
    {
        return decode_evidence_batch_impl(payload, limits);
    }
    catch (const std::bad_alloc &)
    {
        return decode_failure(EvidenceWireError::allocation_failure);
    }
    catch (...)
    {
        return decode_failure(EvidenceWireError::internal_failure);
    }
}

EvidenceLedger::EvidenceLedger(
    const EpochStore &epochs,
    const ProposalEvidenceWindow &window,
    EvidenceStoreLimits limits)
    : state_(std::make_unique<State>(epochs, window, limits))
{}

EvidenceLedger::~EvidenceLedger() = default;

void EvidenceLedger::ingest(
    const AuthenticatedReporter &authenticated_reporter,
    const ResponseObservation &observation)
{
    const auto ingestion_sequence =
        state_->claim_ingestion_sequence();
    if (!ingestion_sequence.has_value())
        return;

    try
    {
        const auto reject = [&](EvidenceRejectionReason reason) {
            state_->store_rejection(
                *ingestion_sequence,
                authenticated_reporter,
                reason,
                &observation,
                std::nullopt);
        };

        if (observation.schema_version !=
            kResponseObservationSchemaVersion)
        {
            reject(EvidenceRejectionReason::unsupported_schema);
            return;
        }
        if (observation.observation_id !=
            compute_response_observation_id(
                observation.attempt_identity()))
        {
            reject(EvidenceRejectionReason::observation_id_mismatch);
            return;
        }
        if (authenticated_reporter.replica_id != observation.reporter_id)
        {
            reject(EvidenceRejectionReason::reporter_mismatch);
            return;
        }

        const auto *epoch = state_->epochs.find_epoch(
            observation.configuration.epoch_number);
        const auto *tree = state_->epochs.find_tree(
            observation.configuration.epoch_number,
            observation.configuration.tree_id);
        if (epoch == nullptr || tree == nullptr ||
            epoch->epoch_digest() != observation.configuration.epoch_digest)
        {
            reject(EvidenceRejectionReason::unknown_configuration);
            return;
        }

        switch (state_->window.classify(observation.proposal_key()))
        {
        case ProposalEvidenceStatus::admissible:
            break;
        case ProposalEvidenceStatus::stale:
            reject(EvidenceRejectionReason::stale_block);
            return;
        case ProposalEvidenceStatus::unknown:
        default:
            reject(EvidenceRejectionReason::unknown_block);
            return;
        }

        const auto topology = direct_child_topology(
            *tree,
            observation.reporter_id,
            observation.observed_replica_id);
        if (!topology.has_value())
        {
            reject(EvidenceRejectionReason::impossible_topology);
            return;
        }

        const auto expected_message_type =
            topology->observed_is_internal
                ? ExpectedMessageType::aggregate_relay
                : ExpectedMessageType::direct_vote;
        if (observation.expected_message_type != expected_message_type)
        {
            reject(EvidenceRejectionReason::invalid_expected_message_type);
            return;
        }
        if (!valid_outcome(observation.outcome))
        {
            reject(EvidenceRejectionReason::invalid_outcome);
            return;
        }
        if (!valid_timing(observation))
        {
            reject(EvidenceRejectionReason::invalid_timing);
            return;
        }
        if (!valid_signer_claim(observation, *topology))
        {
            reject(EvidenceRejectionReason::invalid_signer_set);
            return;
        }

        if (const auto reason =
                transition_rejection(state_->attempts, observation))
        {
            reject(*reason);
            return;
        }

        const auto reporter =
            state_->reporters.find(observation.reporter_id);
        if (reporter != state_->reporters.end())
        {
            if (observation.reporter_sequence <=
                reporter->second.sequence)
            {
                reject(
                    EvidenceRejectionReason::reporter_sequence_regression);
                return;
            }
            if (observation.reporter_monotonic_ns <
                reporter->second.monotonic_ns)
            {
                reject(
                    EvidenceRejectionReason::reporter_timestamp_regression);
                return;
            }
        }

        if (state_->accepted_records.size() >=
            state_->limits.maximum_accepted_records)
        {
            state_->healthy = false;
            reject(EvidenceRejectionReason::accepted_capacity_exceeded);
            return;
        }

        state_->store_acceptance(*ingestion_sequence, observation);
    }
    catch (...)
    {
        state_->healthy = false;
        throw;
    }
}

bool EvidenceLedger::ingest_if_proposal_independent_rejected(
    const AuthenticatedReporter &authenticated_reporter,
    const ResponseObservation &observation)
{
    const auto reason = state_->proposal_independent_rejection(
        authenticated_reporter, observation);
    if (!reason.has_value())
        return false;

    const auto ingestion_sequence = state_->claim_ingestion_sequence();
    if (!ingestion_sequence.has_value())
        return true;

    try
    {
        state_->store_rejection(
            *ingestion_sequence,
            authenticated_reporter,
            *reason,
            &observation,
            std::nullopt);
    }
    catch (...)
    {
        state_->healthy = false;
        throw;
    }
    return true;
}

void EvidenceLedger::reject_wire(
    const AuthenticatedReporter &authenticated_reporter,
    EvidenceWireError error)
{
    const auto ingestion_sequence =
        state_->claim_ingestion_sequence();
    if (!ingestion_sequence.has_value())
        return;

    try
    {
        state_->store_rejection(
            *ingestion_sequence,
            authenticated_reporter,
            EvidenceRejectionReason::wire_error,
            nullptr,
            error);
    }
    catch (...)
    {
        state_->healthy = false;
        throw;
    }
}

std::uint64_t EvidenceLedger::high_watermark() const noexcept
{
    return state_->high_watermark;
}

const std::vector<AcceptedEvidenceRecord> &
EvidenceLedger::accepted() const noexcept
{
    return state_->accepted_records;
}

const std::vector<RejectedEvidenceRecord> &
EvidenceLedger::rejected() const noexcept
{
    return state_->rejected_records;
}

bool EvidenceLedger::healthy() const noexcept
{
    return state_->healthy;
}

} // namespace hotstuff
