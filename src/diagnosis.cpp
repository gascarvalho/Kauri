#include "hotstuff/diagnosis.h"

#include <algorithm>
#include <limits>
#include <new>
#include <utility>

namespace hotstuff
{
namespace
{

bool valid_limits(const DiagnosisLimits &limits) noexcept
{
    return limits.maximum_members != 0 &&
           limits.maximum_members <= kMaximumDiagnosisMembers &&
           limits.maximum_diagnostic_fault_bound <=
               kMaximumDiagnosticFaultBound &&
           limits.maximum_observations != 0 &&
           limits.maximum_observations <=
               kMaximumDiagnosisObservations &&
           limits.maximum_hypotheses != 0 &&
           limits.maximum_hypotheses <=
               kMaximumDiagnosisHypotheses;
}

bool valid_outcome(DiagnosticOutcome outcome) noexcept
{
    switch (outcome)
    {
    case DiagnosticOutcome::on_time_valid:
    case DiagnosticOutcome::timeout:
        return true;
    }
    return false;
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

bool attempt_less(
    const ResponseAttemptIdentity &left,
    const ResponseAttemptIdentity &right) noexcept
{
    if (left.reporter_id != right.reporter_id)
        return left.reporter_id < right.reporter_id;
    if (left.observed_replica_id != right.observed_replica_id)
    {
        return left.observed_replica_id <
               right.observed_replica_id;
    }
    if (left.proposal < right.proposal)
        return true;
    if (right.proposal < left.proposal)
        return false;
    return static_cast<std::uint8_t>(left.expected_message_type) <
           static_cast<std::uint8_t>(right.expected_message_type);
}

bool observation_less(
    const DiagnosticObservation &left,
    const DiagnosticObservation &right) noexcept
{
    if (attempt_less(left.attempt, right.attempt))
        return true;
    if (attempt_less(right.attempt, left.attempt))
        return false;
    return static_cast<std::uint8_t>(left.outcome) <
           static_cast<std::uint8_t>(right.outcome);
}

std::uint64_t binomial(
    std::size_t member_count,
    std::uint32_t selected) noexcept
{
    if (selected > member_count)
        return 0;
    selected = static_cast<std::uint32_t>(std::min<std::size_t>(
        selected, member_count - selected));
    std::uint64_t result = 1;
    for (std::uint32_t index = 1; index <= selected; ++index)
    {
        result =
            (result * (member_count - selected + index)) / index;
    }
    return result;
}

std::uint64_t initial_hypothesis_count(
    std::size_t member_count,
    std::uint32_t diagnostic_fault_bound) noexcept
{
    std::uint64_t result = 0;
    for (std::uint32_t faults = 0;
         faults <= diagnostic_fault_bound;
         ++faults)
    {
        const auto combinations = binomial(member_count, faults);
        const auto modes = std::uint64_t{1} << faults;
        if (combinations >
                std::numeric_limits<std::uint64_t>::max() / modes ||
            result >
                std::numeric_limits<std::uint64_t>::max() -
                    combinations * modes)
        {
            return std::numeric_limits<std::uint64_t>::max();
        }
        result += combinations * modes;
    }
    return result;
}

void append_mode_assignments(
    const std::vector<ReplicaID> &faulty_replicas,
    std::vector<CompatibleHypothesis> &output)
{
    const auto assignment_count =
        std::size_t{1} << faulty_replicas.size();
    for (std::size_t assignment = 0;
         assignment < assignment_count;
         ++assignment)
    {
        CompatibleHypothesis hypothesis;
        hypothesis.false_reporters.reserve(faulty_replicas.size());
        hypothesis.persistent_omitters.reserve(
            faulty_replicas.size());
        for (std::size_t index = 0;
             index < faulty_replicas.size();
             ++index)
        {
            if ((assignment & (std::size_t{1} << index)) == 0)
            {
                hypothesis.false_reporters.push_back(
                    faulty_replicas[index]);
            }
            else
            {
                hypothesis.persistent_omitters.push_back(
                    faulty_replicas[index]);
            }
        }
        output.push_back(std::move(hypothesis));
    }
}

void enumerate_faulty_sets(
    const std::vector<ReplicaID> &membership,
    std::size_t next_index,
    std::size_t remaining,
    std::vector<ReplicaID> &selected,
    std::vector<CompatibleHypothesis> &output)
{
    if (remaining == 0)
    {
        append_mode_assignments(selected, output);
        return;
    }
    const auto final_start = membership.size() - remaining;
    for (std::size_t index = next_index;
         index <= final_start;
         ++index)
    {
        selected.push_back(membership[index]);
        enumerate_faulty_sets(
            membership,
            index + 1,
            remaining - 1,
            selected,
            output);
        selected.pop_back();
    }
}

std::vector<CompatibleHypothesis> enumerate_hypotheses(
    const std::vector<ReplicaID> &membership,
    std::uint32_t diagnostic_fault_bound,
    std::size_t expected_count)
{
    std::vector<CompatibleHypothesis> hypotheses;
    hypotheses.reserve(expected_count);
    std::vector<ReplicaID> selected;
    selected.reserve(diagnostic_fault_bound);
    for (std::uint32_t fault_count = 0;
         fault_count <= diagnostic_fault_bound;
         ++fault_count)
    {
        enumerate_faulty_sets(
            membership,
            0,
            fault_count,
            selected,
            hypotheses);
    }
    return hypotheses;
}

bool contains(
    const std::vector<ReplicaID> &replicas,
    ReplicaID replica) noexcept
{
    return std::binary_search(
        replicas.begin(), replicas.end(), replica);
}

bool compatible(
    const CompatibleHypothesis &hypothesis,
    const std::vector<DiagnosticObservation> &history) noexcept
{
    for (const auto &observation : history)
    {
        if (contains(
                hypothesis.false_reporters,
                observation.attempt.reporter_id))
        {
            continue;
        }
        const bool target_omits = contains(
            hypothesis.persistent_omitters,
            observation.attempt.observed_replica_id);
        const bool reported_missing =
            observation.outcome ==
            DiagnosticOutcome::timeout;
        if (reported_missing != target_omits)
            return false;
    }
    return true;
}

std::vector<ReplicaHypothesisMass> build_mode_mass(
    const std::vector<ReplicaID> &membership,
    const std::vector<CompatibleHypothesis> &hypotheses)
{
    std::vector<ReplicaHypothesisMass> result;
    result.reserve(membership.size());
    const auto compatible_count =
        static_cast<std::uint64_t>(hypotheses.size());
    for (const auto replica : membership)
    {
        std::uint64_t false_reporter_count = 0;
        std::uint64_t persistent_omitter_count = 0;
        for (const auto &hypothesis : hypotheses)
        {
            if (contains(hypothesis.false_reporters, replica))
                ++false_reporter_count;
            if (contains(hypothesis.persistent_omitters, replica))
                ++persistent_omitter_count;
        }

        auto classification = DiagnosticClassification::unresolved;
        if (compatible_count == 0)
        {
            classification =
                DiagnosticClassification::model_inconsistent;
        }
        else if (false_reporter_count == compatible_count)
        {
            classification =
                DiagnosticClassification::false_reporter;
        }
        else if (persistent_omitter_count == compatible_count)
        {
            classification =
                DiagnosticClassification::persistent_omitter;
        }
        else if (false_reporter_count == 0 &&
                 persistent_omitter_count == 0)
        {
            classification = DiagnosticClassification::correct;
        }

        result.push_back(ReplicaHypothesisMass{
            replica,
            false_reporter_count,
            persistent_omitter_count,
            compatible_count,
            classification});
    }
    return result;
}

DiagnosisBuildResult failure(DiagnosisBuildStatus status) noexcept
{
    return {status, std::nullopt};
}

} // namespace

DiagnosisBuildResult build_diagnosis_snapshot(
    std::vector<ReplicaID> membership,
    std::uint32_t diagnostic_fault_bound,
    std::vector<DiagnosticObservation> history,
    DiagnosisLimits limits) noexcept
{
    if (!valid_limits(limits))
        return failure(DiagnosisBuildStatus::invalid_limits);
    if (membership.empty() ||
        membership.size() > limits.maximum_members)
    {
        return failure(DiagnosisBuildStatus::invalid_membership);
    }
    if (diagnostic_fault_bound >
            limits.maximum_diagnostic_fault_bound ||
        diagnostic_fault_bound > membership.size())
    {
        return failure(
            DiagnosisBuildStatus::diagnostic_bound_exceeded);
    }
    if (history.size() > limits.maximum_observations)
    {
        return failure(
            DiagnosisBuildStatus::observation_capacity_exceeded);
    }

    try
    {
        std::sort(membership.begin(), membership.end());
        if (std::adjacent_find(
                membership.begin(), membership.end()) !=
            membership.end())
        {
            return failure(
                DiagnosisBuildStatus::invalid_membership);
        }

        for (const auto &observation : history)
        {
            if (!valid_outcome(observation.outcome) ||
                !valid_message_type(
                    observation.attempt.expected_message_type) ||
                observation.attempt.reporter_id ==
                    observation.attempt.observed_replica_id ||
                !std::binary_search(
                    membership.begin(),
                    membership.end(),
                    observation.attempt.reporter_id) ||
                !std::binary_search(
                    membership.begin(),
                    membership.end(),
                    observation.attempt.observed_replica_id))
            {
                return failure(
                    DiagnosisBuildStatus::invalid_observation);
            }
        }

        std::sort(
            history.begin(), history.end(), observation_less);
        std::vector<DiagnosticObservation> canonical_history;
        canonical_history.reserve(history.size());
        for (const auto &observation : history)
        {
            if (!canonical_history.empty() &&
                canonical_history.back().attempt ==
                    observation.attempt)
            {
                if (canonical_history.back().outcome !=
                    observation.outcome)
                {
                    return failure(
                        DiagnosisBuildStatus::
                            conflicting_observation);
                }
                continue;
            }
            canonical_history.push_back(observation);
        }

        const auto count = initial_hypothesis_count(
            membership.size(), diagnostic_fault_bound);
        if (count > limits.maximum_hypotheses)
        {
            return failure(
                DiagnosisBuildStatus::
                    hypothesis_capacity_exceeded);
        }

        auto hypotheses = enumerate_hypotheses(
            membership,
            diagnostic_fault_bound,
            static_cast<std::size_t>(count));
        if (hypotheses.size() != count)
            return failure(DiagnosisBuildStatus::internal_failure);

        hypotheses.erase(
            std::remove_if(
                hypotheses.begin(),
                hypotheses.end(),
                [&](const CompatibleHypothesis &hypothesis) {
                    return !compatible(
                        hypothesis, canonical_history);
                }),
            hypotheses.end());

        DiagnosisSnapshot snapshot;
        snapshot.membership = std::move(membership);
        snapshot.diagnostic_fault_bound =
            diagnostic_fault_bound;
        snapshot.canonical_history =
            std::move(canonical_history);
        snapshot.compatible_hypotheses =
            std::move(hypotheses);
        snapshot.mode_mass = build_mode_mass(
            snapshot.membership,
            snapshot.compatible_hypotheses);
        snapshot.initial_hypothesis_count = count;
        snapshot.eliminated_hypothesis_count =
            count - snapshot.compatible_hypotheses.size();
        return {
            DiagnosisBuildStatus::built, std::move(snapshot)};
    }
    catch (const std::bad_alloc &)
    {
        return failure(DiagnosisBuildStatus::allocation_failure);
    }
    catch (...)
    {
        return failure(DiagnosisBuildStatus::internal_failure);
    }
}

} // namespace hotstuff
