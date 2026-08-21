#include "hotstuff/structured_event.h"
#include "hotstuff/epoch_activation.h"

#include <algorithm>
#include <cerrno>
#include <charconv>
#include <ctime>
#include <deque>
#include <fcntl.h>
#include <limits>
#include <new>
#include <stdexcept>
#include <string>
#include <string_view>
#include <sys/stat.h>
#include <system_error>
#include <unistd.h>
#include <utility>

namespace hotstuff
{

namespace
{

class LineLimitExceeded final
{};

class ReentrancyScope final
{
public:
    explicit ReentrancyScope(bool &active) noexcept
        : active_(active)
    {
        active_ = true;
    }

    ~ReentrancyScope() noexcept
    {
        active_ = false;
    }

    ReentrancyScope(const ReentrancyScope &) = delete;
    ReentrancyScope &operator=(const ReentrancyScope &) = delete;

private:
    bool &active_;
};

class JsonLineBuilder final
{
public:
    explicit JsonLineBuilder(std::size_t maximum_bytes)
        : maximum_bytes_(maximum_bytes)
    {
    }

    void append(std::string_view text)
    {
        ensure(text.size());
        value_.append(text.data(), text.size());
    }

    void append(char value)
    {
        ensure(1);
        value_.push_back(value);
    }

    template<typename Integer>
    void append_integer(Integer value)
    {
        char buffer[std::numeric_limits<Integer>::digits10 + 4];
        const auto converted = std::to_chars(
            buffer, buffer + sizeof(buffer), value);
        if (converted.ec != std::errc{})
            throw LineLimitExceeded{};
        append(std::string_view{
            buffer,
            static_cast<std::size_t>(converted.ptr - buffer)});
    }

    void append_escaped(std::string_view value)
    {
        static constexpr char hexadecimal[] = "0123456789abcdef";
        append('"');
        for (const unsigned char byte : value)
        {
            switch (byte)
            {
                case '"':
                    append("\\\"");
                    break;
                case '\\':
                    append("\\\\");
                    break;
                case '\b':
                    append("\\b");
                    break;
                case '\f':
                    append("\\f");
                    break;
                case '\n':
                    append("\\n");
                    break;
                case '\r':
                    append("\\r");
                    break;
                case '\t':
                    append("\\t");
                    break;
                default:
                    if (byte < 0x20)
                    {
                        char escape[]{
                            '\\',
                            'u',
                            '0',
                            '0',
                            hexadecimal[(byte >> 4) & 0x0f],
                            hexadecimal[byte & 0x0f]};
                        append(std::string_view{escape, sizeof(escape)});
                    }
                    else
                    {
                        append(static_cast<char>(byte));
                    }
                    break;
            }
        }
        append('"');
    }

    std::string finish()
    {
        append('\n');
        return std::move(value_);
    }

    std::string finish_value()
    {
        return std::move(value_);
    }

private:
    void ensure(std::size_t additional) const
    {
        if (additional > maximum_bytes_ - std::min(
                maximum_bytes_, value_.size()))
            throw LineLimitExceeded{};
    }

    const std::size_t maximum_bytes_;
    std::string value_;
};

const char *source_kind_name(StructuredEventSourceKind kind) noexcept
{
    switch (kind)
    {
        case StructuredEventSourceKind::replica:
            return "replica";
        case StructuredEventSourceKind::adaptation_manager:
            return "adaptation_manager";
        case StructuredEventSourceKind::orchestrator:
            return "orchestrator";
        case StructuredEventSourceKind::workload_client:
            return "workload_client";
    }
    return nullptr;
}

bool valid_source_kind(StructuredEventSourceKind kind) noexcept
{
    return source_kind_name(kind) != nullptr;
}

bool same_source(const StructuredEventSource &left,
                 const StructuredEventSource &right) noexcept
{
    return left.kind == right.kind &&
           left.logical_id == right.logical_id &&
           left.instance_id == right.instance_id;
}

bool valid_utf8(std::string_view value) noexcept
{
    std::size_t index = 0;
    while (index < value.size())
    {
        const auto lead = static_cast<std::uint8_t>(value[index++]);
        if (lead < 0x80)
            continue;

        std::size_t continuation_bytes = 0;
        std::uint8_t second_minimum = 0x80;
        std::uint8_t second_maximum = 0xbf;
        if (lead >= 0xc2 && lead <= 0xdf)
        {
            continuation_bytes = 1;
        }
        else if (lead >= 0xe0 && lead <= 0xef)
        {
            continuation_bytes = 2;
            if (lead == 0xe0)
                second_minimum = 0xa0;
            else if (lead == 0xed)
                second_maximum = 0x9f;
        }
        else if (lead >= 0xf0 && lead <= 0xf4)
        {
            continuation_bytes = 3;
            if (lead == 0xf0)
                second_minimum = 0x90;
            else if (lead == 0xf4)
                second_maximum = 0x8f;
        }
        else
        {
            return false;
        }

        for (std::size_t continuation_index = 0;
             continuation_index < continuation_bytes;
             ++continuation_index)
        {
            if (index == value.size())
                return false;
            const auto continuation =
                static_cast<std::uint8_t>(value[index++]);
            if (continuation_index == 0)
            {
                if (continuation < second_minimum ||
                    continuation > second_maximum)
                    return false;
            }
            else if (continuation < 0x80 || continuation > 0xbf)
            {
                return false;
            }
        }
    }
    return true;
}

bool valid_identity(const std::string &value) noexcept
{
    return !value.empty() && valid_utf8(value);
}

bool valid_source_identity(const StructuredEventSource &source) noexcept
{
    return valid_source_kind(source.kind) &&
           valid_identity(source.logical_id) &&
           valid_identity(source.instance_id);
}

bool same_source_token(const StructuredEventSourceToken &token,
                       const StructuredEventConfig &config) noexcept
{
    return token.run_id == config.run_id &&
           same_source(token.source, config.source);
}

bool valid_cursor(const StructuredEventCursor &cursor,
                  const StructuredEventConfig &config) noexcept
{
    const bool has_sequence = cursor.last_source_sequence != 0;
    const bool has_time = cursor.has_last_monotonic_ns;
    const bool has_token = cursor.source_token.has_value();

    if (!has_sequence && !has_time && !has_token)
        return cursor.last_monotonic_ns == 0;
    if (!has_sequence || !has_time || !has_token)
        return false;
    return same_source_token(*cursor.source_token, config);
}

bool payload_type(const StructuredEventPayload &payload,
                  StructuredEventType &type) noexcept
{
    switch (payload.index())
    {
        case 0:
            switch (std::get<ProcessLifecycleEvent>(payload).state)
            {
                case ProcessLifecycleState::started:
                    type = StructuredEventType::process_started;
                    return true;
                case ProcessLifecycleState::ready:
                    type = StructuredEventType::process_ready;
                    return true;
                case ProcessLifecycleState::stopping:
                    type = StructuredEventType::process_stopping;
                    return true;
                case ProcessLifecycleState::stopped:
                    type = StructuredEventType::process_stopped;
                    return true;
                case ProcessLifecycleState::forced_crash_requested:
                    type = StructuredEventType::process_forced_crash_requested;
                    return true;
                case ProcessLifecycleState::exited:
                    type = StructuredEventType::process_exited;
                    return true;
            }
            return false;
        case 1:
        {
            const auto &event = std::get<EpochLifecycleEvent>(payload);
            const bool has_v3_apply =
                event.certificate_apply_committed_height.has_value();
            const bool has_v3_digest =
                event.activation_readiness_certificate_digest.has_value();
            if (has_v3_apply != has_v3_digest ||
                (has_v3_apply &&
                 (event.transition != EpochLifecycleTransition::activated ||
                  *event.certificate_apply_committed_height <
                      event.activation_height ||
                  *event.activation_readiness_certificate_digest ==
                      uint256_t{})))
            {
                return false;
            }
            switch (std::get<EpochLifecycleEvent>(payload).transition)
            {
                case EpochLifecycleTransition::generated:
                    type = StructuredEventType::epoch_generated;
                    return true;
                case EpochLifecycleTransition::staged:
                    type = StructuredEventType::epoch_staged;
                    return true;
                case EpochLifecycleTransition::acknowledged:
                    type = StructuredEventType::epoch_acknowledged;
                    return true;
                case EpochLifecycleTransition::activation_armed:
                    type = StructuredEventType::epoch_activation_armed;
                    return true;
                case EpochLifecycleTransition::activated:
                    type = StructuredEventType::epoch_activated;
                    return true;
            }
            return false;
        }
        case 2:
            if (const auto &event =
                    std::get<CommitStructuredEvent>(payload);
                event.reporter_local_commit_monotonic_ns.has_value() &&
                *event.reporter_local_commit_monotonic_ns == 0)
            {
                return false;
            }
            type = StructuredEventType::block_committed;
            return true;
        case 3:
            type = StructuredEventType::block_commit_observed;
            return true;
        case 4:
        {
            const auto &event =
                std::get<CommitIdentityUnavailableStructuredEvent>(payload);
            if (event.reason != CommitIdentityUnavailableReason::
                                    no_authenticated_exact_identity_source ||
                event.convergence_identity_pending)
                return false;
            type = StructuredEventType::block_commit_identity_unavailable;
            return true;
        }
        case 5:
        {
            const auto &event =
                std::get<CommitIdentityWitnessStructuredEvent>(payload);
            if (event.block_hash == uint256_t{} ||
                event.decision_proof.block_hash != event.block_hash ||
                event.view_generation == 0)
                return false;
            type = StructuredEventType::block_commit_identity_witness;
            return true;
        }
        default:
            return false;
    }
}

bool convergence_payload_type(
    const AdaptiveV2ConvergenceStructuredEvent &event,
    StructuredEventType &type) noexcept
{
    switch (event.transition)
    {
        case AdaptiveV2ConvergenceTransition::delivery_attempt:
            type = StructuredEventType::adaptive_v2_delivery_attempt;
            return true;
        case AdaptiveV2ConvergenceTransition::commit_observed:
            type = StructuredEventType::adaptive_v2_commit_observed;
            return true;
        case AdaptiveV2ConvergenceTransition::activation_observed:
            type = StructuredEventType::adaptive_v2_activation_observed;
            return true;
        case AdaptiveV2ConvergenceTransition::converged:
            type = StructuredEventType::adaptive_v2_converged;
            return true;
        case AdaptiveV2ConvergenceTransition::ready:
            type = StructuredEventType::adaptive_v2_ready;
            return true;
        case AdaptiveV2ConvergenceTransition::failure:
            type = StructuredEventType::adaptive_v2_convergence_failure;
            return true;
    }
    return false;
}

bool adaptive_v3_readiness_payload_type(
    const AdaptiveV3ReadinessStructuredEvent &event,
    StructuredEventType &type) noexcept
{
    switch (event.transition)
    {
        case AdaptiveV3ReadinessTransition::activation_prepared:
            type = StructuredEventType::adaptive_v3_activation_prepared;
            return true;
        case AdaptiveV3ReadinessTransition::activation_ready_signed:
            type = StructuredEventType::adaptive_v3_activation_ready_signed;
            return true;
        case AdaptiveV3ReadinessTransition::observation_accepted:
            type = StructuredEventType::adaptive_v3_observation_accepted;
            return true;
        case AdaptiveV3ReadinessTransition::observation_rejected:
            type = StructuredEventType::adaptive_v3_observation_rejected;
            return true;
        case AdaptiveV3ReadinessTransition::source_quarantined:
            type = StructuredEventType::adaptive_v3_source_quarantined;
            return true;
        case AdaptiveV3ReadinessTransition::certificate_assembled:
            type = StructuredEventType::adaptive_v3_certificate_assembled;
            return true;
        case AdaptiveV3ReadinessTransition::certificate_delivery:
            type = StructuredEventType::adaptive_v3_certificate_delivery;
            return true;
        case AdaptiveV3ReadinessTransition::certificate_accepted:
            type = StructuredEventType::adaptive_v3_certificate_accepted;
            return true;
        case AdaptiveV3ReadinessTransition::certificate_rejected:
            type = StructuredEventType::adaptive_v3_certificate_rejected;
            return true;
        case AdaptiveV3ReadinessTransition::certificate_acknowledged:
            type = StructuredEventType::
                adaptive_v3_certificate_acknowledged;
            return true;
        case AdaptiveV3ReadinessTransition::e2_eligibility:
            type = StructuredEventType::adaptive_v3_e2_eligibility;
            return true;
        case AdaptiveV3ReadinessTransition::terminal:
            type = StructuredEventType::adaptive_v3_terminal;
            return true;
        case AdaptiveV3ReadinessTransition::wire_rejected:
            type = StructuredEventType::adaptive_v3_wire_rejected;
            return true;
        case AdaptiveV3ReadinessTransition::observation_retry_exhausted:
            type = StructuredEventType::
                adaptive_v3_observation_retry_exhausted;
            return true;
    }
    return false;
}

const char *adaptive_v3_command_terminal_reason_name(
    AdaptiveV3CommandTerminalReason reason) noexcept
{
    switch (reason)
    {
        case AdaptiveV3CommandTerminalReason::invalid_committed_command:
            return "invalid_committed_command";
        case AdaptiveV3CommandTerminalReason::wrong_active_predecessor:
            return "wrong_active_predecessor";
        case AdaptiveV3CommandTerminalReason::invalid_successor_generation:
            return "invalid_successor_generation";
        case AdaptiveV3CommandTerminalReason::
                successor_runtime_preparation_failed:
            return "successor_runtime_preparation_failed";
        case AdaptiveV3CommandTerminalReason::
                committed_definition_recovery_failed:
            return "committed_definition_recovery_failed";
        case AdaptiveV3CommandTerminalReason::
                committed_definition_retry_schedule_failed:
            return "committed_definition_retry_schedule_failed";
        case AdaptiveV3CommandTerminalReason::
                readiness_source_sequence_exhausted:
            return "readiness_source_sequence_exhausted";
        case AdaptiveV3CommandTerminalReason::readiness_boundary_rejected:
            return "readiness_boundary_rejected";
        case AdaptiveV3CommandTerminalReason::readiness_internal_failure:
            return "readiness_internal_failure";
    }
    return nullptr;
}

bool audit_payload_type(const AuditStructuredEventPayload &payload,
                        StructuredEventType &type) noexcept
{
    switch (payload.index())
    {
        case 0:
            type = StructuredEventType::epoch_command_committed;
            return true;
        case 1:
            type = StructuredEventType::reputation_evidence_applied;
            return true;
        case 2:
            return convergence_payload_type(
                std::get<AdaptiveV2ConvergenceStructuredEvent>(payload),
                type);
        case 3:
            type = StructuredEventType::adaptive_v2_evidence_snapshot;
            return true;
        case 4:
            type = StructuredEventType::adaptive_v2_session_terminal;
            return true;
        case 5:
            type = StructuredEventType::evidence_observation_accepted;
            return true;
        case 6:
            type = StructuredEventType::adaptive_v2_shape_decision;
            return true;
        case 7:
            type = StructuredEventType::fault_contribution_opportunity;
            return true;
        case 8:
            type = StructuredEventType::pipeline_root_qc_queue_blocked;
            return true;
        case 9:
            type = StructuredEventType::
                adaptive_v2_fault_containment_coverage_ready;
            return true;
        case 10:
            type = StructuredEventType::
                adaptive_v2_cross_commit_retention_ready;
            return true;
        case 11:
            type = StructuredEventType::fault_window_armed;
            return true;
        case 12:
            return adaptive_v3_readiness_payload_type(
                std::get<AdaptiveV3ReadinessStructuredEvent>(payload),
                type);
        case 13:
            type = StructuredEventType::adaptive_v3_command_terminal;
            return true;
        default:
            return false;
    }
}

const char *tree_policy_kind_name(TreePolicyKind policy) noexcept
{
    switch (policy)
    {
        case TreePolicyKind::fault_containment:
            return "fault_containment";
        case TreePolicyKind::performance_optimization:
            return "performance_optimization";
    }
    return nullptr;
}

const char *manager_cycle_outcome_name(
    AdaptiveV2ManagerCycleOutcome outcome) noexcept
{
    switch (outcome)
    {
        case AdaptiveV2ManagerCycleOutcome::advanced:
            return "advanced";
        case AdaptiveV2ManagerCycleOutcome::no_op:
            return "no_op";
        case AdaptiveV2ManagerCycleOutcome::failed:
            return "failed";
    }
    return nullptr;
}

const char *manager_cycle_reason_name(
    AdaptiveV2ManagerCycleTerminalReason reason) noexcept
{
    switch (reason)
    {
        case AdaptiveV2ManagerCycleTerminalReason::successor_converged:
            return "successor_converged";
        case AdaptiveV2ManagerCycleTerminalReason::explicit_no_op:
            return "explicit_no_op";
        case AdaptiveV2ManagerCycleTerminalReason::controller_unhealthy:
            return "controller_unhealthy";
        case AdaptiveV2ManagerCycleTerminalReason::convergence_start_failed:
            return "convergence_start_failed";
        case AdaptiveV2ManagerCycleTerminalReason::convergence_retry_exhausted:
            return "convergence_retry_exhausted";
        case AdaptiveV2ManagerCycleTerminalReason::convergence_conflicting_observation:
            return "convergence_conflicting_observation";
        case AdaptiveV2ManagerCycleTerminalReason::invalid_terminal_identity:
            return "invalid_terminal_identity";
        case AdaptiveV2ManagerCycleTerminalReason::successor_rotation_failed:
            return "successor_rotation_failed";
        case AdaptiveV2ManagerCycleTerminalReason::evidence_window_reset_failed:
            return "evidence_window_reset_failed";
        case AdaptiveV2ManagerCycleTerminalReason::caller_failed:
            return "caller_failed";
        case AdaptiveV2ManagerCycleTerminalReason::fault_window_arm_missing:
            return "fault_window_arm_missing";
        case AdaptiveV2ManagerCycleTerminalReason::fault_window_arm_invalid:
            return "fault_window_arm_invalid";
        case AdaptiveV2ManagerCycleTerminalReason::fault_window_arm_io_failure:
            return "fault_window_arm_io_failure";
    }
    return nullptr;
}

const char *controller_failure_stage_name(
    AdaptiveV2ManagerControllerFailureStage stage) noexcept
{
    switch (stage)
    {
        case AdaptiveV2ManagerControllerFailureStage::operational_precondition:
            return "operational_precondition";
        case AdaptiveV2ManagerControllerFailureStage::baseline_selection:
            return "baseline_selection";
        case AdaptiveV2ManagerControllerFailureStage::guarded_selection:
            return "guarded_selection";
        case AdaptiveV2ManagerControllerFailureStage::successor_factory:
            return "successor_factory";
    }
    return nullptr;
}

const char *selection_status_name(
    AdaptiveV2SelectionStatus status) noexcept
{
    switch (status)
    {
        case AdaptiveV2SelectionStatus::baseline_frozen: return "baseline_frozen";
        case AdaptiveV2SelectionStatus::selected: return "selected";
        case AdaptiveV2SelectionStatus::insufficient_guarded_candidates: return "insufficient_guarded_candidates";
        case AdaptiveV2SelectionStatus::guarded_candidate_bound_exceeded: return "guarded_candidate_bound_exceeded";
        case AdaptiveV2SelectionStatus::insufficient_eligible_roots: return "insufficient_eligible_roots";
        case AdaptiveV2SelectionStatus::invalid_state: return "invalid_state";
        case AdaptiveV2SelectionStatus::invalid_cutoff: return "invalid_cutoff";
        case AdaptiveV2SelectionStatus::ledger_unhealthy: return "ledger_unhealthy";
        case AdaptiveV2SelectionStatus::mixed_epoch: return "mixed_epoch";
        case AdaptiveV2SelectionStatus::nonmember_evidence: return "nonmember_evidence";
        case AdaptiveV2SelectionStatus::projection_failed: return "projection_failed";
        case AdaptiveV2SelectionStatus::capacity_exceeded: return "capacity_exceeded";
        case AdaptiveV2SelectionStatus::snapshot_failed: return "snapshot_failed";
        case AdaptiveV2SelectionStatus::internal_failure: return "internal_failure";
    }
    return nullptr;
}

const char *epoch_factory_status_name(
    AdaptiveV2EpochFactoryStatus status) noexcept
{
    switch (status)
    {
        case AdaptiveV2EpochFactoryStatus::success: return "success";
        case AdaptiveV2EpochFactoryStatus::invalid_current_epoch: return "invalid_current_epoch";
        case AdaptiveV2EpochFactoryStatus::epoch_number_exhausted: return "epoch_number_exhausted";
        case AdaptiveV2EpochFactoryStatus::epoch_mismatch: return "epoch_mismatch";
        case AdaptiveV2EpochFactoryStatus::membership_mismatch: return "membership_mismatch";
        case AdaptiveV2EpochFactoryStatus::invalid_selection: return "invalid_selection";
        case AdaptiveV2EpochFactoryStatus::root_mismatch: return "root_mismatch";
        case AdaptiveV2EpochFactoryStatus::tree_count_mismatch: return "tree_count_mismatch";
        case AdaptiveV2EpochFactoryStatus::invalid_activation_delay: return "invalid_activation_delay";
        case AdaptiveV2EpochFactoryStatus::capacity_exceeded: return "capacity_exceeded";
        case AdaptiveV2EpochFactoryStatus::placement_failed: return "placement_failed";
        case AdaptiveV2EpochFactoryStatus::insufficient_leaf_capacity: return "insufficient_leaf_capacity";
        case AdaptiveV2EpochFactoryStatus::authorization_failed: return "authorization_failed";
        case AdaptiveV2EpochFactoryStatus::bundle_failed: return "bundle_failed";
        case AdaptiveV2EpochFactoryStatus::internal_failure: return "internal_failure";
    }
    return nullptr;
}

const char *response_outcome_name(ResponseOutcome outcome) noexcept
{
    switch (outcome)
    {
        case ResponseOutcome::on_time:
            return "on_time";
        case ResponseOutcome::timeout:
            return "timeout";
        case ResponseOutcome::late:
            return "late";
    }
    return nullptr;
}

const char *expected_message_type_name(
    ExpectedMessageType type) noexcept
{
    switch (type)
    {
        case ExpectedMessageType::direct_vote:
            return "direct_vote";
        case ExpectedMessageType::aggregate_relay:
            return "aggregate_relay";
        case ExpectedMessageType::leader_progress:
            return "leader_progress";
    }
    return nullptr;
}

const char *experiment_replica_role_name(
    ExperimentReplicaRole role) noexcept
{
    switch (role)
    {
        case ExperimentReplicaRole::root:
            return "root";
        case ExperimentReplicaRole::internal:
            return "internal";
        case ExperimentReplicaRole::leaf:
            return "leaf";
    }
    return nullptr;
}

const char *experiment_omission_cohort_name(
    ExperimentOmissionCohort cohort) noexcept
{
    switch (cohort)
    {
        case ExperimentOmissionCohort::none:
            return "none";
        case ExperimentOmissionCohort::hard:
            return "hard";
        case ExperimentOmissionCohort::responsive_degraded:
            return "responsive_degraded";
    }
    return nullptr;
}

const char *experiment_omission_action_name(
    ExperimentOmissionAction action) noexcept
{
    switch (action)
    {
        case ExperimentOmissionAction::forward:
            return "forward";
        case ExperimentOmissionAction::omit_aggregate:
            return "omit_aggregate";
        case ExperimentOmissionAction::omit_direct_vote:
            return "omit_direct_vote";
        case ExperimentOmissionAction::capacity_exhausted:
            return "capacity_exhausted";
    }
    return nullptr;
}

bool replica_source_matches(
    const StructuredEventConfig &config,
    ReplicaID replica) noexcept
{
    if (config.source.kind != StructuredEventSourceKind::replica)
        return false;
    try
    {
        return config.source.logical_id ==
               "replica-" + std::to_string(replica);
    }
    catch (...)
    {
        return false;
    }
}

bool valid_fault_contribution_opportunity(
    const FaultContributionOpportunityStructuredEvent &event,
    const StructuredEventConfig &config) noexcept
{
    if (!replica_source_matches(config, event.actor) ||
        event.fault_mode !=
            "tiered_persistent_responsive_omission_v2" ||
        event.proposal.configuration.epoch_digest == uint256_t{} ||
        event.proposal.block_hash == uint256_t{} ||
        event.view_generation == 0 ||
        event.physical_role == ExperimentReplicaRole::root ||
        event.parent_replica == event.actor ||
        event.authenticated_proposal_source_replica !=
            event.parent_replica ||
        event.cohort == ExperimentOmissionCohort::none ||
        !valid_identity(event.diagnostic_window) ||
        event.diagnostic_window.size() >
            config.limits.maximum_identity_bytes ||
        event.fault_mode.size() > config.limits.maximum_identity_bytes ||
        event.window_start_monotonic_ns == 0 ||
        event.window_end_monotonic_ns <=
            event.window_start_monotonic_ns ||
        event.decision_monotonic_ns <
            event.window_start_monotonic_ns ||
        event.decision_monotonic_ns >=
            event.window_end_monotonic_ns ||
        event.responsive_omission_period < 2 ||
        event.fault_threshold == 0 || event.hard_actor_count == 0 ||
        event.responsive_degraded_actor_count == 0 ||
        event.hard_actor_count >
            std::numeric_limits<std::size_t>::max() -
                event.responsive_degraded_actor_count ||
        event.hard_actor_count +
                event.responsive_degraded_actor_count >
            event.fault_threshold)
        return false;

    const bool internal =
        event.physical_role == ExperimentReplicaRole::internal;
    const bool leaf = event.physical_role == ExperimentReplicaRole::leaf;
    if (!internal && !leaf)
        return false;

    const auto expected_message_type =
        internal ? ExpectedMessageType::aggregate_relay
                 : ExpectedMessageType::direct_vote;
    if (event.expected_message_type != expected_message_type)
        return false;

    const auto omission_action =
        internal ? ExperimentOmissionAction::omit_aggregate
                 : ExperimentOmissionAction::omit_direct_vote;
    if (event.scheduled_action != ExperimentOmissionAction::forward &&
        event.scheduled_action != omission_action)
        return false;

    if (event.cohort == ExperimentOmissionCohort::hard)
        return event.contribution_ordinal == 0 &&
               event.role_contribution_ordinal == 0 &&
               event.scheduled_action == omission_action;

    if (event.cohort !=
            ExperimentOmissionCohort::responsive_degraded ||
        event.contribution_ordinal == 0 ||
        event.role_contribution_ordinal == 0 ||
        event.role_contribution_ordinal > event.contribution_ordinal)
        return false;

    const bool scheduled_omission =
        event.role_contribution_ordinal %
                event.responsive_omission_period ==
            0;
    const auto expected_action =
        scheduled_omission ? omission_action
                           : ExperimentOmissionAction::forward;
    return event.scheduled_action == expected_action;
}

bool valid_root_qc_queue_blocked(
    const RootQcQueueBlockedStructuredEvent &event,
    const StructuredEventConfig &config) noexcept
{
    if (!replica_source_matches(config, event.observer_replica) ||
        event.configuration.epoch_digest == uint256_t{} ||
        event.global_quorum == 0 || event.queue_head_position != 0 ||
        event.queued_candidate_position != 1 ||
        event.queue_head_context_generation == 0 ||
        event.queued_candidate_context_generation <=
            event.queue_head_context_generation ||
        event.queue_head_block_height == 0 ||
        event.queue_head_block_height ==
            std::numeric_limits<std::uint64_t>::max() ||
        event.queued_candidate_block_height !=
            event.queue_head_block_height + 1 ||
        event.queue_head_block_hash == uint256_t{} ||
        event.queued_candidate_block_hash == uint256_t{} ||
        event.queued_candidate_block_hash ==
            event.queue_head_block_hash ||
        event.queued_candidate_parent_hash !=
            event.queue_head_block_hash ||
        event.queue_head_signer_count >= event.global_quorum ||
        event.queued_candidate_signer_count < event.global_quorum)
        return false;

    return event.queued_candidate_qc_ready &&
           !event.queued_candidate_qc_published;
}

const char *reputation_outcome_name(
    SimpleReputationOutcome outcome) noexcept
{
    switch (outcome)
    {
        case SimpleReputationOutcome::response:
            return "response";
        case SimpleReputationOutcome::timeout:
            return "timeout";
    }
    return nullptr;
}

const char *shape_status_name(ShapeV1Status status) noexcept
{
    switch (status)
    {
        case ShapeV1Status::selected:
            return "selected";
        case ShapeV1Status::invalid_input:
            return "invalid_input";
        case ShapeV1Status::no_feasible_candidate:
            return "no_feasible_candidate";
    }
    return nullptr;
}

const char *shape_rejection_name(
    ShapeV1CandidateRejection rejection) noexcept
{
    switch (rejection)
    {
        case ShapeV1CandidateRejection::none:
            return "none";
        case ShapeV1CandidateRejection::fanout_out_of_range:
            return "fanout_out_of_range";
        case ShapeV1CandidateRejection::wait_exempt_would_influence:
            return "wait_exempt_would_influence";
        case ShapeV1CandidateRejection::missing_influential_evidence:
            return "missing_influential_evidence";
        case ShapeV1CandidateRejection::score_overflow:
            return "score_overflow";
    }
    return nullptr;
}

bool valid_epoch_command_payload(
    const EpochCommandCommittedStructuredEvent &event,
    StructuredEventSourceKind source_kind) noexcept
{
    if (source_kind != StructuredEventSourceKind::replica ||
        event.command_block_height == 0 ||
        event.command_block_hash == uint256_t{} ||
        event.predecessor_epoch_digest == uint256_t{} ||
        event.successor_epoch_digest == uint256_t{} ||
        event.payload_digest == uint256_t{} ||
        event.predecessor_epoch_digest == event.successor_epoch_digest ||
        event.predecessor_epoch_number ==
            std::numeric_limits<std::uint32_t>::max() ||
        event.successor_epoch_number !=
            event.predecessor_epoch_number + 1 ||
        event.activation_delay_blocks == 0 ||
        event.activation_delay_blocks >
            std::numeric_limits<std::uint64_t>::max() -
                event.command_block_height)
        return false;

    return event.activation_height ==
        event.command_block_height + event.activation_delay_blocks;
}

bool valid_adaptive_v3_command_terminal_payload(
    const AdaptiveV3CommandTerminalStructuredEvent &event,
    StructuredEventSourceKind source_kind) noexcept
{
    const auto reason =
        adaptive_v3_command_terminal_reason_name(event.reason);
    const auto &command = event.command;
    if (source_kind != StructuredEventSourceKind::replica ||
        reason == nullptr || command.command_block_height == 0 ||
        command.command_block_hash == uint256_t{} ||
        command.predecessor_epoch_digest == uint256_t{} ||
        command.successor_epoch_digest == uint256_t{} ||
        command.payload_digest == uint256_t{})
        return false;
    const EpochChangePayload payload{
        command.successor_epoch_number,
        command.predecessor_epoch_digest,
        command.successor_epoch_digest,
        command.activation_delay_blocks};
    if (epoch_change_payload_digest(payload) != command.payload_digest)
        return false;
    if (event.reason ==
        AdaptiveV3CommandTerminalReason::invalid_committed_command)
        return true;
    return valid_epoch_command_payload(command, source_kind);
}

bool valid_reputation_payload(
    const ReputationEvidenceAppliedStructuredEvent &event,
    StructuredEventSourceKind source_kind) noexcept
{
    const auto &update = event.update;
    if (source_kind != StructuredEventSourceKind::adaptation_manager ||
        event.evidence_cutoff == 0 ||
        update.ingestion_sequence == 0 ||
        update.ingestion_sequence > event.evidence_cutoff ||
        update.observation_id == uint256_t{} ||
        update.reporter_id == update.target_id ||
        response_outcome_name(update.evidence_outcome) == nullptr ||
        reputation_outcome_name(update.reputation_outcome) == nullptr)
        return false;

    SimpleReputationOutcome expected_outcome{
        SimpleReputationOutcome::response};
    int expected_delta = 1;
    if (update.evidence_outcome == ResponseOutcome::timeout)
    {
        expected_outcome = SimpleReputationOutcome::timeout;
        expected_delta = -1;
    }
    if (update.reputation_outcome != expected_outcome ||
        update.delta != expected_delta)
        return false;

    if ((update.delta == 1 &&
         update.score == std::numeric_limits<int>::min()) ||
        (update.delta == -1 &&
         update.score == std::numeric_limits<int>::max()))
        return false;
    return true;
}

bool valid_observation_accepted_payload(
    const EvidenceObservationAcceptedStructuredEvent &event,
    StructuredEventSourceKind source_kind) noexcept
{
    const auto &record = event.record;
    const auto &observation = record.observation;
    if (source_kind != StructuredEventSourceKind::adaptation_manager ||
        record.ingestion_sequence == 0 ||
        !is_supported_response_observation_schema(
            observation.schema_version) ||
        observation.observation_id == uint256_t{} ||
        observation.reporter_id ==
            observation.observed_replica_id ||
        observation.configuration.epoch_digest == uint256_t{} ||
        observation.block_hash == uint256_t{} ||
        expected_message_type_name(
            observation.expected_message_type) == nullptr ||
        response_outcome_name(observation.outcome) == nullptr ||
        observation.deadline_duration_us == 0 ||
        observation.reporter_sequence == 0 ||
        std::adjacent_find(
            observation.signer_set.begin(),
            observation.signer_set.end(),
            [](ReplicaID left, ReplicaID right) {
                return left >= right;
            }) != observation.signer_set.end())
    {
        return false;
    }

    if (!valid_response_observation_retention_witness(observation))
        return false;

    if (observation.outcome == ResponseOutcome::timeout)
    {
        if (observation.response_duration_us != 0 ||
            !observation.signer_set.empty())
        {
            return false;
        }
    }
    else
    {
        if (observation.signer_set.empty() ||
            (observation.outcome == ResponseOutcome::late &&
             observation.response_duration_us <
                 observation.deadline_duration_us))
        {
            return false;
        }
    }

    try
    {
        return observation.observation_id ==
            compute_response_observation_id(observation);
    }
    catch (...)
    {
        return false;
    }
}

bool valid_convergence_identity(
    const AdaptiveV2EpochChangeIdentity &identity) noexcept
{
    if (identity.command_block_height == 0 ||
        identity.command_block_hash == uint256_t{} ||
        identity.predecessor_epoch_digest == uint256_t{} ||
        identity.successor_epoch_digest == uint256_t{} ||
        identity.command_payload_digest == uint256_t{} ||
        identity.predecessor_epoch_digest == identity.successor_epoch_digest ||
        identity.predecessor_epoch_number ==
            std::numeric_limits<std::uint32_t>::max() ||
        identity.successor_epoch_number !=
            identity.predecessor_epoch_number + 1 ||
        identity.activation_delay_blocks == 0 ||
        identity.activation_delay_blocks >
            std::numeric_limits<std::uint64_t>::max() -
                identity.command_block_height)
    {
        return false;
    }

    return identity.activation_height ==
        identity.command_block_height + identity.activation_delay_blocks;
}

bool valid_convergence_payload(
    const AdaptiveV2ConvergenceStructuredEvent &event,
    const StructuredEventConfig &config) noexcept
{
    const auto delivery_disposition = [&event]() noexcept {
        return event.disposition == "enqueued" ||
            event.disposition == "enqueue_failed" ||
            event.disposition == "injected_drop";
    };
    const auto observation_disposition = [&event]() noexcept {
        return event.disposition == "rejected_unauthenticated_source" ||
            event.disposition == "rejected_wire_decode" ||
            event.disposition == "accepted" ||
            event.disposition == "duplicate" ||
            event.disposition == "rejected_nonmember" ||
            event.disposition == "rejected_spoofed_source" ||
            event.disposition == "rejected_stale" ||
            event.disposition == "rejected_wrong_identity" ||
            event.disposition == "conflicting_observation" ||
            event.disposition == "terminal" ||
            event.disposition == "ack_sent" ||
            event.disposition == "ack_injected_drop" ||
            event.disposition == "ack_send_failed";
    };

    StructuredEventType ignored{};
    if (config.source.kind !=
            StructuredEventSourceKind::adaptation_manager ||
        !convergence_payload_type(event, ignored) ||
        event.required_activation_count == 0 ||
        !valid_utf8(event.disposition) ||
        event.disposition.size() >
            config.limits.maximum_identity_bytes ||
        !valid_utf8(event.failure_reason) ||
        event.failure_reason.size() >
            config.limits.maximum_identity_bytes ||
        (event.canonical_payload_digest.has_value() &&
         *event.canonical_payload_digest == uint256_t{}) ||
        (event.identity.has_value() &&
         !valid_convergence_identity(*event.identity)))
    {
        return false;
    }

    if (event.transition != AdaptiveV2ConvergenceTransition::failure &&
        !event.failure_reason.empty())
        return false;

    switch (event.transition)
    {
        case AdaptiveV2ConvergenceTransition::delivery_attempt:
            return event.replica_id.has_value() &&
                event.delivery_attempt != 0 &&
                delivery_disposition() &&
                event.canonical_payload_digest.has_value() &&
                !event.identity.has_value();
        case AdaptiveV2ConvergenceTransition::commit_observed:
        case AdaptiveV2ConvergenceTransition::activation_observed:
            if (event.delivery_attempt != 0 ||
                !observation_disposition())
                return false;
            if (event.disposition ==
                "rejected_unauthenticated_source")
            {
                return !event.replica_id.has_value() &&
                    !event.identity.has_value() &&
                    !event.canonical_payload_digest.has_value();
            }
            if (event.disposition == "rejected_wire_decode")
            {
                return event.replica_id.has_value() &&
                    !event.identity.has_value() &&
                    !event.canonical_payload_digest.has_value();
            }
            return event.replica_id.has_value() &&
                event.identity.has_value() &&
                (event.canonical_payload_digest.has_value() ||
                 event.disposition == "ack_send_failed");
        case AdaptiveV2ConvergenceTransition::converged:
        case AdaptiveV2ConvergenceTransition::ready:
            return !event.replica_id.has_value() &&
                event.delivery_attempt == 0 &&
                event.disposition.empty() &&
                event.identity.has_value() &&
                !event.canonical_payload_digest.has_value() &&
                event.accepted_activation_count >=
                    event.required_activation_count;
        case AdaptiveV2ConvergenceTransition::failure:
            return event.disposition.empty() &&
                !event.canonical_payload_digest.has_value() &&
                !event.failure_reason.empty() &&
                (event.delivery_attempt == 0 ||
                 event.replica_id.has_value());
    }
    return false;
}

bool valid_evidence_snapshot_payload(
    const AdaptiveV2EvidenceSnapshotStructuredEvent &event) noexcept
{
    if (event.activation_generation == 0)
        return false;
    const auto packed_generation = event.activation_generation - 1;
    const auto rotation_ordinal = static_cast<std::uint32_t>(
        packed_generation & std::numeric_limits<std::uint32_t>::max());
    const auto expected_generation = checked_activation_generation(
        event.predecessor_epoch_number, rotation_ordinal);
    if (event.schema_version !=
            kAdaptiveV2EvidenceSnapshotSchemaVersion ||
        tree_policy_kind_name(event.policy_intent) == nullptr ||
        event.transition_artifact_id.empty() ||
        !valid_utf8(event.transition_artifact_id) ||
        event.predecessor_epoch_digest == uint256_t{} ||
        !expected_generation.has_value() ||
        event.activation_generation != *expected_generation ||
        event.baseline_cutoff == 0 ||
        event.current_cutoff <= event.baseline_cutoff ||
        event.full_prefix_snapshot_id == uint256_t{} ||
        event.evidence_snapshot_id == uint256_t{} ||
        event.accepted_prefix_count == 0 ||
        event.accepted_prefix_count > event.current_cutoff ||
        event.eligible_ranking.empty() ||
        event.eligible_ranking.size() > kMaximumTreePolicyTrees)
    {
        return false;
    }

    for (std::size_t index = 0;
         index < event.eligible_ranking.size();
         ++index)
    {
        if (std::find(
                event.eligible_ranking.begin(),
                event.eligible_ranking.begin() + index,
                event.eligible_ranking[index]) !=
            event.eligible_ranking.begin() + index)
        {
            return false;
        }
    }
    return true;
}

bool valid_manager_session_terminal_payload(
    const AdaptiveV2ManagerSessionTerminalStructuredEvent &event,
    const StructuredEventConfig &config) noexcept
{
    const auto valid_failure = [&event]() noexcept {
        const auto fatal_selection = [](AdaptiveV2SelectionStatus status) noexcept {
            switch (status)
            {
                case AdaptiveV2SelectionStatus::invalid_state:
                case AdaptiveV2SelectionStatus::guarded_candidate_bound_exceeded:
                case AdaptiveV2SelectionStatus::invalid_cutoff:
                case AdaptiveV2SelectionStatus::ledger_unhealthy:
                case AdaptiveV2SelectionStatus::mixed_epoch:
                case AdaptiveV2SelectionStatus::nonmember_evidence:
                case AdaptiveV2SelectionStatus::projection_failed:
                case AdaptiveV2SelectionStatus::capacity_exceeded:
                case AdaptiveV2SelectionStatus::snapshot_failed:
                case AdaptiveV2SelectionStatus::internal_failure:
                    return true;
                default: return false;
            }
        };
        if (!event.controller_failure.has_value())
            return event.reason !=
                AdaptiveV2ManagerCycleTerminalReason::controller_unhealthy;
        const auto &detail = *event.controller_failure;
        if (event.reason !=
                AdaptiveV2ManagerCycleTerminalReason::controller_unhealthy ||
            controller_failure_stage_name(detail.stage) == nullptr)
            return false;
        switch (detail.stage)
        {
            case AdaptiveV2ManagerControllerFailureStage::operational_precondition:
                return !detail.selection_status.has_value() &&
                    !detail.epoch_factory_status.has_value();
            case AdaptiveV2ManagerControllerFailureStage::baseline_selection:
                return detail.selection_status.has_value() &&
                    (fatal_selection(*detail.selection_status) ||
                     *detail.selection_status ==
                         AdaptiveV2SelectionStatus::baseline_frozen) &&
                    !detail.epoch_factory_status.has_value();
            case AdaptiveV2ManagerControllerFailureStage::guarded_selection:
                return detail.selection_status.has_value() &&
                    fatal_selection(*detail.selection_status) &&
                    !detail.epoch_factory_status.has_value();
            case AdaptiveV2ManagerControllerFailureStage::successor_factory:
                return detail.selection_status ==
                        AdaptiveV2SelectionStatus::selected &&
                    detail.epoch_factory_status.has_value() &&
                    *detail.epoch_factory_status !=
                        AdaptiveV2EpochFactoryStatus::success &&
                    epoch_factory_status_name(
                        *detail.epoch_factory_status) != nullptr;
        }
        return false;
    };
    if (!valid_failure())
        return false;
    if (event.evidence_window_activation_generation == 0)
        return false;
    const auto packed_generation =
        event.evidence_window_activation_generation - 1;
    const auto rotation_ordinal = static_cast<std::uint32_t>(
        packed_generation & std::numeric_limits<std::uint32_t>::max());
    const auto expected_generation = checked_activation_generation(
        event.predecessor_epoch_number, rotation_ordinal);
    if (config.source.kind !=
            StructuredEventSourceKind::adaptation_manager ||
        tree_policy_kind_name(event.policy_intent) == nullptr ||
        manager_cycle_outcome_name(event.outcome) == nullptr ||
        manager_cycle_reason_name(event.reason) == nullptr ||
        event.transition_artifact_id.empty() ||
        !valid_utf8(event.transition_artifact_id) ||
        event.transition_artifact_id.size() >
            config.limits.maximum_identity_bytes ||
        event.predecessor_epoch_digest == uint256_t{} ||
        !expected_generation.has_value() ||
        event.evidence_window_activation_generation !=
            *expected_generation ||
        event.baseline_evidence_cutoff > event.current_evidence_cutoff)
    {
        return false;
    }

    const bool has_successor =
        event.successor_epoch_number.has_value() &&
        event.successor_epoch_digest.has_value() &&
        event.command_payload_digest.has_value();
    const bool has_partial_successor =
        event.successor_epoch_number.has_value() ||
        event.successor_epoch_digest.has_value() ||
        event.command_payload_digest.has_value();
    if (has_partial_successor != has_successor ||
        (event.successor_epoch_digest.has_value() &&
         *event.successor_epoch_digest == uint256_t{}) ||
        (event.command_payload_digest.has_value() &&
         *event.command_payload_digest == uint256_t{}))
    {
        return false;
    }

    if (has_successor &&
        (event.predecessor_epoch_number ==
             std::numeric_limits<std::uint32_t>::max() ||
         *event.successor_epoch_number !=
             event.predecessor_epoch_number + 1 ||
         *event.successor_epoch_digest ==
             event.predecessor_epoch_digest))
    {
        return false;
    }

    if (event.winning_activation.has_value())
    {
        const auto &identity = *event.winning_activation;
        if (!has_successor || !valid_convergence_identity(identity) ||
            identity.predecessor_epoch_number !=
                event.predecessor_epoch_number ||
            identity.predecessor_epoch_digest !=
                event.predecessor_epoch_digest ||
            identity.successor_epoch_number !=
                *event.successor_epoch_number ||
            identity.successor_epoch_digest !=
                *event.successor_epoch_digest ||
            identity.command_payload_digest !=
                *event.command_payload_digest)
        {
            return false;
        }
    }

    switch (event.outcome)
    {
        case AdaptiveV2ManagerCycleOutcome::advanced:
            return event.reason ==
                    AdaptiveV2ManagerCycleTerminalReason::successor_converged &&
                has_successor && event.winning_activation.has_value() &&
                event.baseline_evidence_cutoff != 0 &&
                event.current_evidence_cutoff >
                    event.baseline_evidence_cutoff;
        case AdaptiveV2ManagerCycleOutcome::no_op:
            return event.reason ==
                    AdaptiveV2ManagerCycleTerminalReason::explicit_no_op &&
                !has_successor && !event.winning_activation.has_value();
        case AdaptiveV2ManagerCycleOutcome::failed:
            return event.reason !=
                    AdaptiveV2ManagerCycleTerminalReason::successor_converged &&
                event.reason !=
                    AdaptiveV2ManagerCycleTerminalReason::explicit_no_op;
    }
    return false;
}

bool valid_shape_decision_payload(
    const AdaptiveV2ShapeDecisionStructuredEvent &event,
    const StructuredEventConfig &config) noexcept
{
    return config.source.kind ==
            StructuredEventSourceKind::adaptation_manager &&
        !event.transition_artifact_id.empty() &&
        valid_utf8(event.transition_artifact_id) &&
        event.transition_artifact_id.size() <=
            config.limits.maximum_identity_bytes &&
        valid_shape_decision_record(event.decision);
}

bool valid_fault_containment_coverage_ready_payload(
    const AdaptiveV2FaultContainmentCoverageReadyStructuredEvent &event,
    const StructuredEventConfig &config) noexcept
{
    if (config.source.kind !=
            StructuredEventSourceKind::adaptation_manager ||
        event.transition_artifact_id.empty() ||
        !valid_utf8(event.transition_artifact_id) ||
        event.transition_artifact_id.size() >
            config.limits.maximum_identity_bytes ||
        event.predecessor_epoch_digest == uint256_t{} ||
        event.fault_evidence_start_monotonic_ns == 0 ||
        event.evidence_cutoff == 0 ||
        event.required_tree_ids.empty() ||
        event.required_tree_ids.size() >
            kMaximumAdaptationEvidenceRecords ||
        event.required_tree_ids != event.observed_tree_ids)
    {
        return false;
    }
    return std::adjacent_find(
               event.required_tree_ids.begin(),
               event.required_tree_ids.end(),
               [](std::uint32_t left, std::uint32_t right) {
                   return left >= right;
               }) == event.required_tree_ids.end();
}

bool valid_cross_commit_retention_ready_payload(
    const AdaptiveV2CrossCommitRetentionReadyStructuredEvent &event,
    const StructuredEventConfig &config) noexcept
{
    if (config.source.kind !=
            StructuredEventSourceKind::adaptation_manager ||
        event.cycle_ordinal != 1 ||
        event.predecessor_epoch_number != 1 ||
        event.predecessor_epoch_digest == uint256_t{} ||
        event.evidence_cutoff == 0 ||
        event.responsive_degraded_actor_ids.empty() ||
        event.responsive_degraded_actor_ids.size() !=
            event.admitted_observation_ids.size() ||
        event.responsive_degraded_actor_ids.size() >
            kMaximumAdaptationEvidenceRecords)
    {
        return false;
    }
    if (std::adjacent_find(
            event.responsive_degraded_actor_ids.begin(),
            event.responsive_degraded_actor_ids.end(),
            [](ReplicaID left, ReplicaID right) {
                return left >= right;
            }) != event.responsive_degraded_actor_ids.end())
    {
        return false;
    }
    for (std::size_t index = 0;
         index < event.admitted_observation_ids.size();
         ++index)
    {
        const auto &observation_id =
            event.admitted_observation_ids[index];
        if (observation_id == uint256_t{} ||
            std::find(
                event.admitted_observation_ids.begin(),
                event.admitted_observation_ids.begin() + index,
                observation_id) !=
                event.admitted_observation_ids.begin() + index)
            return false;
    }
    return true;
}

bool valid_fault_window_armed_payload(
    const FaultWindowArmedStructuredEvent &event,
    const StructuredEventConfig &config) noexcept
{
    const auto valid_digest = [](const std::string &value) {
        return value.size() == 64 && std::all_of(
            value.begin(), value.end(), [](unsigned char character) {
                return (character >= '0' && character <= '9') ||
                    (character >= 'a' && character <= 'f');
            });
    };
    const bool v1 = event.schema_version == 1 &&
        event.kind == "kauri-focused-fault-window-arm-v1" &&
        event.clock_domain.empty() &&
        event.required_observation_schema == 0 &&
        event.timeout_evidence_basis.empty() &&
        event.snapshot_evidence_basis.empty() &&
        event.selection_cardinality_policy.empty();
    const bool v2 = event.schema_version == 2 &&
        event.kind == "kauri-focused-fault-window-arm-v2" &&
        event.clock_domain == "same_host_clock_monotonic_raw" &&
        event.required_observation_schema == 3 &&
        event.timeout_evidence_basis == "exact_timeout_attempt_id_v1" &&
        event.snapshot_evidence_basis.empty() &&
        event.selection_cardinality_policy.empty();
    const bool v3 = event.schema_version == 3 &&
        event.kind == "kauri-focused-fault-window-arm-v3" &&
        event.clock_domain == "same_host_clock_monotonic_raw" &&
        event.required_observation_schema == 3 &&
        event.timeout_evidence_basis == "exact_timeout_attempt_id_v1" &&
        event.snapshot_evidence_basis ==
            "exact_post_fault_attempt_start_v1" &&
        event.selection_cardinality_policy.empty();
    const bool v4 = event.schema_version == 4 &&
        event.kind == "kauri-focused-fault-window-arm-v4" &&
        event.clock_domain == "same_host_clock_monotonic_raw" &&
        event.required_observation_schema == 3 &&
        event.timeout_evidence_basis == "exact_timeout_attempt_id_v1" &&
        event.snapshot_evidence_basis ==
            "exact_post_fault_attempt_start_v1" &&
        event.selection_cardinality_policy ==
            "all_guarded_up_to_fault_bound_v1";
    return config.source.kind == StructuredEventSourceKind::adaptation_manager &&
        (v1 || v2 || v3 || v4) &&
        !event.run_id.empty() && !event.profile_id.empty() &&
        event.epoch_digest != uint256_t{} &&
        event.evidence_start_monotonic_ns != 0 &&
        event.required_tree_positions != 0 &&
        event.required_tree_positions == event.required_tree_ids.size() &&
        !event.required_tree_ids.empty() &&
        valid_digest(event.profile_sha256) &&
        valid_digest(event.topology_proof_sha256) &&
        valid_digest(event.request_sha256) &&
        valid_digest(event.fault_receipt_sha256) &&
        valid_digest(event.fault_window_arm_sha256);
}

bool valid_adaptive_v3_identity(
    const ActivationReadyIdentityV1 &identity,
    const StructuredEventConfig &config) noexcept
{
    try
    {
        const ActivationReadinessWireLimits limits{
            config.limits.maximum_line_bytes, 31};
        return !encode_activation_ready_identity_v1(identity, limits).empty();
    }
    catch (...)
    {
        return false;
    }
}

bool valid_adaptive_v3_readiness_payload(
    const AdaptiveV3ReadinessStructuredEvent &event,
    const StructuredEventConfig &config) noexcept
{
    const bool session_terminal = event.transition ==
        AdaptiveV3ReadinessTransition::terminal &&
        event.terminal_cycle_ordinal.has_value();
    const bool wire_rejected = event.transition ==
        AdaptiveV3ReadinessTransition::wire_rejected;
    if ((!session_terminal && !wire_rejected &&
         !valid_adaptive_v3_identity(event.identity, config)) ||
        (session_terminal && event.terminal_identity.has_value() &&
         !valid_adaptive_v3_identity(*event.terminal_identity, config)) ||
        !valid_utf8(event.disposition) ||
        event.disposition.size() > config.limits.maximum_identity_bytes ||
        std::adjacent_find(
            event.observed_signers.begin(),
            event.observed_signers.end(),
            [](ReplicaID left, ReplicaID right) {
                return left >= right;
            }) != event.observed_signers.end() ||
        event.observed_signers.size() > 31)
    {
        return false;
    }

    const ActivationReadinessWireLimits limits{
        config.limits.maximum_line_bytes, 31};
    const auto no_signer_clock =
        !event.signer_source_sequence.has_value() &&
        !event.signer_monotonic_raw_ns.has_value();
    const auto no_digests = !event.observation_digest.has_value() &&
        !event.certificate_digest.has_value() &&
        !event.payload_digest.has_value();
    const auto no_collection = event.observed_signers.empty() &&
        event.required_release_count == 0;
    const auto no_e2_audit = !event.e2_cycle_ordinal.has_value() &&
        !event.e1_bundle_digest.has_value() &&
        !event.e2_final_ack_raw_ns.has_value() &&
        !event.e2_common_commit.has_value() &&
        event.e2_common_commit_sources.empty() &&
        !event.e2_common_commit_raw_ns.has_value() &&
        !event.e2_earliest_raw_ns.has_value() &&
        !event.e2_actual_begin_raw_ns.has_value() &&
        !event.e2_hard_deadline_raw_ns.has_value() &&
        !event.e2_reserve_raw_ns.has_value();
    const auto no_terminal_audit = !event.terminal_cycle_ordinal.has_value() &&
        !event.terminal_reason.has_value() &&
        !event.terminal_identity.has_value() &&
        !event.terminal_bundle_digest.has_value();
    if (event.transition != AdaptiveV3ReadinessTransition::e2_eligibility &&
        !no_e2_audit)
        return false;
    if (event.transition != AdaptiveV3ReadinessTransition::terminal &&
        !no_terminal_audit)
        return false;
    if (!wire_rejected && (event.wire_opcode.has_value() ||
                           event.wire_payload_size.has_value()))
        return false;

    switch (event.transition)
    {
        case AdaptiveV3ReadinessTransition::wire_rejected:
        {
            if ((config.source.kind != StructuredEventSourceKind::replica &&
                 config.source.kind != StructuredEventSourceKind::adaptation_manager) ||
                !event.replica_id.has_value() || !no_signer_clock ||
                event.observation_digest.has_value() ||
                event.certificate_digest.has_value() ||
                !event.payload_digest.has_value() ||
                *event.payload_digest == uint256_t{} || !no_collection ||
                event.delivery_attempt != 0 || event.delivery_enqueued ||
                !event.canonical_wire_payload.has_value() ||
                !event.wire_opcode.has_value() ||
                !event.wire_payload_size.has_value() ||
                *event.wire_payload_size != event.canonical_wire_payload->size() ||
                *event.wire_payload_size > config.limits.maximum_line_bytes ||
                !no_e2_audit || !no_terminal_audit ||
                event.identity != ActivationReadyIdentityV1{})
                return false;
            const bool manager_wire = config.source.kind ==
                StructuredEventSourceKind::adaptation_manager;
            const bool observation = event.disposition == "observation_decode" &&
                *event.wire_opcode == MsgActivationReadyObservation::opcode;
            const bool ack = event.disposition == "ack_decode" &&
                *event.wire_opcode == MsgActivationReadinessAck::opcode;
            const bool certificate = event.disposition == "certificate_decode" &&
                *event.wire_opcode == MsgActivationReadinessCertificate::opcode;
            return (manager_wire ? (observation || ack) : certificate) &&
                DataStream(*event.canonical_wire_payload).get_hash() ==
                    *event.payload_digest;
        }

        case AdaptiveV3ReadinessTransition::activation_prepared:
            return config.source.kind == StructuredEventSourceKind::replica &&
                event.replica_id.has_value() && no_signer_clock &&
                no_digests && no_collection && event.delivery_attempt == 0 &&
                !event.canonical_wire_payload.has_value() &&
                event.disposition.empty();

        case AdaptiveV3ReadinessTransition::activation_ready_signed:
        case AdaptiveV3ReadinessTransition::observation_retry_exhausted:
        case AdaptiveV3ReadinessTransition::observation_accepted:
        case AdaptiveV3ReadinessTransition::source_quarantined:
        {
            const bool replica_event = event.transition ==
                    AdaptiveV3ReadinessTransition::activation_ready_signed ||
                event.transition == AdaptiveV3ReadinessTransition::
                    observation_retry_exhausted;
            if (config.source.kind !=
                    (replica_event
                         ? StructuredEventSourceKind::replica
                         : StructuredEventSourceKind::adaptation_manager) ||
                !event.replica_id.has_value() ||
                !event.signer_source_sequence.has_value() ||
                *event.signer_source_sequence == 0 ||
                !event.signer_monotonic_raw_ns.has_value() ||
                !event.observation_digest.has_value() ||
                *event.observation_digest == uint256_t{} ||
                event.certificate_digest.has_value() ||
                event.payload_digest.has_value() || !no_collection ||
                event.delivery_attempt != 0 ||
                !event.canonical_wire_payload.has_value())
            {
                return false;
            }
            const auto decoded = decode_activation_ready_observation(
                *event.canonical_wire_payload, limits);
            if (!decoded || decoded.value->identity != event.identity ||
                decoded.value->signer_replica_id != *event.replica_id ||
                decoded.value->signer_source_sequence !=
                    *event.signer_source_sequence ||
                decoded.value->signer_monotonic_raw_ns !=
                    *event.signer_monotonic_raw_ns ||
                activation_ready_observation_digest(*decoded.value) !=
                    *event.observation_digest)
            {
                return false;
            }
            if (replica_event)
                return event.transition == AdaptiveV3ReadinessTransition::
                        activation_ready_signed
                    ? event.disposition.empty()
                    : event.disposition == "retry_exhausted";
            if (event.transition ==
                AdaptiveV3ReadinessTransition::source_quarantined)
                return event.disposition == "rejected_conflict";
            return event.disposition == "accepted" ||
                event.disposition == "duplicate" ||
                event.disposition == "released";
        }

        case AdaptiveV3ReadinessTransition::observation_rejected:
        {
            if (config.source.kind !=
                    StructuredEventSourceKind::adaptation_manager ||
                event.disposition.empty() ||
                event.certificate_digest.has_value() ||
                event.payload_digest.has_value() || !no_collection ||
                event.delivery_attempt != 0)
            {
                return false;
            }
            if (!event.replica_id.has_value() ||
                !event.canonical_wire_payload.has_value() ||
                !event.observation_digest.has_value() ||
                !event.signer_source_sequence.has_value() ||
                !event.signer_monotonic_raw_ns.has_value())
            {
                return false;
            }
            const auto decoded = decode_activation_ready_observation(
                *event.canonical_wire_payload, limits);
            const bool collector_disposition =
                event.disposition == "quarantined" ||
                event.disposition == "rejected_peer_binding" ||
                event.disposition == "rejected_nonmember" ||
                event.disposition == "rejected_invalid_observation" ||
                event.disposition == "rejected_wrong_identity";
            return decoded && decoded.value->identity == event.identity &&
                collector_disposition &&
                ((event.disposition == "rejected_peer_binding" &&
                  decoded.value->signer_replica_id != *event.replica_id) ||
                 (event.disposition != "rejected_peer_binding" &&
                  decoded.value->signer_replica_id == *event.replica_id)) &&
                decoded.value->signer_source_sequence ==
                    *event.signer_source_sequence &&
                decoded.value->signer_monotonic_raw_ns ==
                    *event.signer_monotonic_raw_ns &&
                activation_ready_observation_digest(*decoded.value) ==
                    *event.observation_digest;
        }

        case AdaptiveV3ReadinessTransition::certificate_assembled:
        case AdaptiveV3ReadinessTransition::certificate_delivery:
        case AdaptiveV3ReadinessTransition::certificate_accepted:
        case AdaptiveV3ReadinessTransition::certificate_rejected:
        {
            const bool manager_event = event.transition ==
                    AdaptiveV3ReadinessTransition::certificate_assembled ||
                event.transition ==
                    AdaptiveV3ReadinessTransition::certificate_delivery;
            if (config.source.kind !=
                    (manager_event
                         ? StructuredEventSourceKind::adaptation_manager
                         : StructuredEventSourceKind::replica) ||
                !no_signer_clock || event.observation_digest.has_value() ||
                !event.certificate_digest.has_value() ||
                *event.certificate_digest == uint256_t{} ||
                !event.payload_digest.has_value() ||
                *event.payload_digest == uint256_t{} ||
                !event.canonical_wire_payload.has_value())
            {
                return false;
            }
            const auto decoded = decode_activation_readiness_certificate(
                *event.canonical_wire_payload, limits);
            if (!decoded || decoded.value->identity != event.identity ||
                decoded.value->certificate_digest !=
                    *event.certificate_digest ||
                activation_readiness_ack_payload_digest(
                    MsgActivationReadinessCertificate::opcode,
                    *event.canonical_wire_payload) != *event.payload_digest)
            {
                return false;
            }
            if (event.transition ==
                AdaptiveV3ReadinessTransition::certificate_assembled)
            {
                std::vector<ReplicaID> certificate_signers;
                certificate_signers.reserve(
                    decoded.value->observations.size());
                for (const auto &observation : decoded.value->observations)
                    certificate_signers.push_back(
                        observation.signer_replica_id);
                return !event.replica_id.has_value() &&
                    event.delivery_attempt == 0 &&
                    event.required_release_count != 0 &&
                    event.required_release_count ==
                        event.observed_signers.size() &&
                    event.observed_signers == certificate_signers &&
                    event.disposition.empty();
            }
            if (event.transition ==
                AdaptiveV3ReadinessTransition::certificate_delivery)
            {
                return event.replica_id.has_value() &&
                    event.delivery_attempt != 0 && no_collection &&
                    ((event.disposition == "queued" &&
                      event.delivery_enqueued) ||
                     (event.disposition == "retry_scheduled" &&
                      !event.delivery_enqueued) ||
                     event.disposition == "retry_exhausted" ||
                     event.disposition == "deadline_expired");
            }
            return event.replica_id.has_value() &&
                event.delivery_attempt == 0 && no_collection &&
                ((event.transition ==
                      AdaptiveV3ReadinessTransition::certificate_accepted &&
                  event.disposition.empty()) ||
                 (event.transition ==
                      AdaptiveV3ReadinessTransition::certificate_rejected &&
                  !event.disposition.empty()));
        }

        case AdaptiveV3ReadinessTransition::certificate_acknowledged:
        {
            if (config.source.kind !=
                    StructuredEventSourceKind::adaptation_manager ||
                !event.replica_id.has_value() || !no_signer_clock ||
                event.observation_digest.has_value() ||
                !event.certificate_digest.has_value() ||
                !event.payload_digest.has_value() || !no_collection ||
                event.delivery_attempt != 0 ||
                !event.canonical_wire_payload.has_value())
            {
                return false;
            }
            const auto decoded = decode_activation_readiness_ack(
                *event.canonical_wire_payload, limits);
            return decoded && decoded.value->identity == event.identity &&
                decoded.value->recipient_replica_id == *event.replica_id &&
                decoded.value->certificate_digest ==
                    *event.certificate_digest &&
                decoded.value->payload_digest == *event.payload_digest &&
                event.disposition == "acknowledged";
        }

        case AdaptiveV3ReadinessTransition::e2_eligibility:
        {
            constexpr std::uint64_t kE1ResidenceNs = 65'000'000'000ULL;
            constexpr std::uint64_t kCommonStabilizationNs =
                60'000'000'000ULL;
            constexpr std::uint64_t kReserveNs = 90'000'000'000ULL;
            if (config.source.kind !=
                    StructuredEventSourceKind::adaptation_manager ||
                event.replica_id.has_value() || !no_signer_clock ||
                !no_digests || event.delivery_attempt != 0 ||
                event.delivery_enqueued || event.canonical_wire_payload.has_value() ||
                event.disposition != "eligible" ||
                !no_terminal_audit ||
                !event.e2_cycle_ordinal.has_value() ||
                *event.e2_cycle_ordinal != 1 ||
                !event.e1_bundle_digest.has_value() ||
                *event.e1_bundle_digest == uint256_t{} ||
                !event.e2_final_ack_raw_ns.has_value() ||
                *event.e2_final_ack_raw_ns == 0 ||
                !event.e2_common_commit.has_value() ||
                event.e2_common_commit->configuration !=
                    event.identity.successor_configuration ||
                event.e2_common_commit->block_hash == uint256_t{} ||
                !event.e2_common_commit_raw_ns.has_value() ||
                !event.e2_earliest_raw_ns.has_value() ||
                !event.e2_actual_begin_raw_ns.has_value() ||
                !event.e2_hard_deadline_raw_ns.has_value() ||
                !event.e2_reserve_raw_ns.has_value() ||
                *event.e2_reserve_raw_ns != kReserveNs ||
                event.e2_common_commit_sources.empty() ||
                event.e2_common_commit_sources.size() > 31 ||
                std::adjacent_find(event.e2_common_commit_sources.begin(),
                    event.e2_common_commit_sources.end(),
                    [](ReplicaID left, ReplicaID right) {
                        return left >= right;
                    }) != event.e2_common_commit_sources.end() ||
                event.e2_common_commit_sources != event.observed_signers ||
                event.required_release_count !=
                    event.e2_common_commit_sources.size() ||
                *event.e2_final_ack_raw_ns >
                    std::numeric_limits<std::uint64_t>::max() - kE1ResidenceNs ||
                *event.e2_common_commit_raw_ns >
                    std::numeric_limits<std::uint64_t>::max() -
                        kCommonStabilizationNs ||
                *event.e2_final_ack_raw_ns >
                    std::numeric_limits<std::uint64_t>::max() -
                        5'000'000'000ULL ||
                *event.e2_common_commit_raw_ns <= *event.e2_final_ack_raw_ns ||
                *event.e2_common_commit_raw_ns >=
                    *event.e2_final_ack_raw_ns + 5'000'000'000ULL)
                return false;
            const auto earliest = std::max(
                *event.e2_final_ack_raw_ns + kE1ResidenceNs,
                *event.e2_common_commit_raw_ns + kCommonStabilizationNs);
            return *event.e2_earliest_raw_ns == earliest &&
                *event.e2_actual_begin_raw_ns >= earliest &&
                *event.e2_actual_begin_raw_ns < *event.e2_hard_deadline_raw_ns &&
                *event.e2_actual_begin_raw_ns <=
                    std::numeric_limits<std::uint64_t>::max() - kReserveNs &&
                *event.e2_actual_begin_raw_ns + kReserveNs <
                    *event.e2_hard_deadline_raw_ns;
        }

        case AdaptiveV3ReadinessTransition::terminal:
            if (config.source.kind ==
                    StructuredEventSourceKind::replica)
            {
                return event.replica_id.has_value() && no_signer_clock &&
                    no_digests && no_collection &&
                    event.delivery_attempt == 0 &&
                    !event.canonical_wire_payload.has_value() &&
                    (event.disposition ==
                         "observation_schedule_failed" ||
                     event.disposition ==
                         "observation_encoding_failed" ||
                     event.disposition ==
                         "observation_internal_failure");
            }
            return config.source.kind ==
                    StructuredEventSourceKind::adaptation_manager &&
                !event.replica_id.has_value() && no_signer_clock &&
                !event.observation_digest.has_value() && event.delivery_attempt == 0 &&
                !event.canonical_wire_payload.has_value() &&
                ((event.terminal_cycle_ordinal.has_value() &&
                  event.terminal_reason.has_value() &&
                  *event.terminal_reason >= 1 &&
                  *event.terminal_reason <= 9 &&
                  (!event.terminal_identity.has_value() ||
                   event.identity == *event.terminal_identity) &&
                  !event.certificate_digest.has_value() &&
                  !event.payload_digest.has_value() &&
                  (event.terminal_bundle_digest == std::nullopt ||
                   *event.terminal_bundle_digest != uint256_t{}) &&
                  !event.disposition.empty()) ||
                 // Preserve the established certificate terminal shape for
                 // replica-originated readiness until its producer migrates.
                 (event.certificate_digest.has_value() &&
                  *event.certificate_digest != uint256_t{} &&
                  !event.payload_digest.has_value() &&
                  event.required_release_count != 0 &&
                  event.required_release_count == event.observed_signers.size() &&
                  (event.disposition == "complete" ||
                   event.disposition == "incomplete")));
    }
    return false;
}

bool valid_audit_payload(const AuditStructuredEventPayload &payload,
                         const StructuredEventConfig &config) noexcept
{
    switch (payload.index())
    {
        case 0:
            return valid_epoch_command_payload(
                std::get<EpochCommandCommittedStructuredEvent>(payload),
                config.source.kind);
        case 1:
            return valid_reputation_payload(
                std::get<ReputationEvidenceAppliedStructuredEvent>(payload),
                config.source.kind);
        case 2:
            return valid_convergence_payload(
                std::get<AdaptiveV2ConvergenceStructuredEvent>(payload),
                config);
        case 3:
            return config.source.kind ==
                    StructuredEventSourceKind::adaptation_manager &&
                std::get<AdaptiveV2EvidenceSnapshotStructuredEvent>(
                    payload).transition_artifact_id.size() <=
                    config.limits.maximum_identity_bytes &&
                valid_evidence_snapshot_payload(
                    std::get<
                        AdaptiveV2EvidenceSnapshotStructuredEvent>(
                            payload));
        case 4:
            return valid_manager_session_terminal_payload(
                std::get<
                    AdaptiveV2ManagerSessionTerminalStructuredEvent>(
                        payload),
                config);
        case 5:
            return valid_observation_accepted_payload(
                std::get<
                    EvidenceObservationAcceptedStructuredEvent>(
                        payload),
                config.source.kind);
        case 6:
            return valid_shape_decision_payload(
                std::get<AdaptiveV2ShapeDecisionStructuredEvent>(
                    payload),
                config);
        case 7:
            return valid_fault_contribution_opportunity(
                std::get<
                    FaultContributionOpportunityStructuredEvent>(
                        payload),
                config);
        case 8:
            return valid_root_qc_queue_blocked(
                std::get<RootQcQueueBlockedStructuredEvent>(payload),
                config);
        case 9:
            return valid_fault_containment_coverage_ready_payload(
                std::get<
                    AdaptiveV2FaultContainmentCoverageReadyStructuredEvent>(
                        payload),
                config);
        case 10:
            return valid_cross_commit_retention_ready_payload(
                std::get<
                    AdaptiveV2CrossCommitRetentionReadyStructuredEvent>(
                        payload),
                config);
        case 11:
            return valid_fault_window_armed_payload(
                std::get<FaultWindowArmedStructuredEvent>(payload), config);
        case 12:
            return valid_adaptive_v3_readiness_payload(
                std::get<AdaptiveV3ReadinessStructuredEvent>(payload),
                config);
        case 13:
            return valid_adaptive_v3_command_terminal_payload(
                std::get<AdaptiveV3CommandTerminalStructuredEvent>(payload),
                config.source.kind);
        default:
            return false;
    }
}

struct AdaptiveEventDescriptor
{
    AdaptiveAggregationTransition transition;
    StructuredEventType type;
    const char *name;
};

constexpr AdaptiveEventDescriptor kAdaptiveEventDescriptors[] = {
    {AdaptiveAggregationTransition::configuration_active,
     StructuredEventType::adaptive_configuration_active,
     "adaptive.configuration_active"},
    {AdaptiveAggregationTransition::required_set_ready,
     StructuredEventType::aggregation_required_set_ready,
     "aggregation.required_set_ready"},
    {AdaptiveAggregationTransition::initial_reserved,
     StructuredEventType::aggregation_initial_reserved,
     "aggregation.initial_reserved"},
    {AdaptiveAggregationTransition::initial_enqueued,
     StructuredEventType::aggregation_initial_enqueued,
     "aggregation.initial_enqueued"},
    {AdaptiveAggregationTransition::initial_committed,
     StructuredEventType::aggregation_initial_committed,
     "aggregation.initial_committed"},
    {AdaptiveAggregationTransition::initial_released,
     StructuredEventType::aggregation_initial_released,
     "aggregation.initial_released"},
    {AdaptiveAggregationTransition::delta_reserved,
     StructuredEventType::aggregation_delta_reserved,
     "aggregation.delta_reserved"},
    {AdaptiveAggregationTransition::delta_enqueued,
     StructuredEventType::aggregation_delta_enqueued,
     "aggregation.delta_enqueued"},
    {AdaptiveAggregationTransition::delta_committed,
     StructuredEventType::aggregation_delta_committed,
     "aggregation.delta_committed"},
    {AdaptiveAggregationTransition::delta_released,
     StructuredEventType::aggregation_delta_released,
     "aggregation.delta_released"},
    {AdaptiveAggregationTransition::delta_rejected,
     StructuredEventType::aggregation_delta_rejected,
     "aggregation.delta_rejected"},
    {AdaptiveAggregationTransition::required_branch_incomplete,
     StructuredEventType::aggregation_required_branch_incomplete,
     "aggregation.required_branch_incomplete"},
    {AdaptiveAggregationTransition::
         wait_exempt_absent_at_observation_deadline,
     StructuredEventType::aggregation_wait_exempt_absent,
     "aggregation.wait_exempt_absent_at_observation_deadline"},
    {AdaptiveAggregationTransition::wait_exempt_late_accepted,
     StructuredEventType::aggregation_wait_exempt_late_accepted,
     "aggregation.wait_exempt_late_accepted"},
    {AdaptiveAggregationTransition::retry_exhausted,
     StructuredEventType::aggregation_retry_exhausted,
     "aggregation.retry_exhausted"},
    {AdaptiveAggregationTransition::proposal_aborted,
     StructuredEventType::aggregation_proposal_aborted,
     "aggregation.proposal_aborted"},
    {AdaptiveAggregationTransition::root_quorum_progress,
     StructuredEventType::aggregation_root_quorum_progress,
     "aggregation.root_quorum_progress"},
    {AdaptiveAggregationTransition::root_qc_published,
     StructuredEventType::aggregation_root_qc_published,
     "aggregation.root_qc_published"},
};

constexpr std::size_t kAdaptiveEventDescriptorCount =
    sizeof(kAdaptiveEventDescriptors) /
    sizeof(kAdaptiveEventDescriptors[0]);

bool adaptive_payload_type(
    const AdaptiveAggregationStructuredEvent &event,
    StructuredEventType &type) noexcept
{
    const auto value = static_cast<std::uint8_t>(event.transition);
    if (value == 0 || value > kAdaptiveEventDescriptorCount)
        return false;
    const auto &descriptor = kAdaptiveEventDescriptors[value - 1];
    if (descriptor.transition != event.transition)
        return false;
    type = descriptor.type;
    return true;
}

const char *adaptive_event_type_name(StructuredEventType type) noexcept
{
    const auto first = static_cast<std::uint8_t>(
        StructuredEventType::adaptive_configuration_active);
    const auto value = static_cast<std::uint8_t>(type);
    if (value < first || value - first >= kAdaptiveEventDescriptorCount)
        return nullptr;
    const auto &descriptor =
        kAdaptiveEventDescriptors[value - first];
    return descriptor.type == type ? descriptor.name : nullptr;
}

bool strictly_increasing(const std::vector<ReplicaID> &values) noexcept
{
    return std::adjacent_find(
               values.begin(), values.end(),
               [](ReplicaID left, ReplicaID right) {
                   return left >= right;
               }) == values.end();
}

bool valid_adaptive_payload(
    const AdaptiveAggregationStructuredEvent &event,
    StructuredEventType &type) noexcept
{
    if (!adaptive_payload_type(event, type) ||
        event.configuration.epoch_digest == uint256_t{} ||
        !valid_utf8(event.rejection_reason) ||
        !strictly_increasing(event.wait_exempt_signers) ||
        !strictly_increasing(event.accepted_signers) ||
        !strictly_increasing(event.absent_direct_children) ||
        !strictly_increasing(event.missing_optional_signers))
        return false;

    if (event.transition !=
            AdaptiveAggregationTransition::configuration_active &&
        (!event.block_hash.has_value() ||
         *event.block_hash == uint256_t{} ||
         !event.context_generation.has_value() ||
         *event.context_generation == 0))
        return false;
    if (event.transition ==
            AdaptiveAggregationTransition::configuration_active &&
        (event.block_hash.has_value() ||
         event.context_generation.has_value()))
        return false;
    if ((event.transition ==
             AdaptiveAggregationTransition::delta_rejected ||
         event.transition ==
             AdaptiveAggregationTransition::proposal_aborted) &&
        event.rejection_reason.empty())
        return false;
    const bool root_observation =
        event.transition ==
            AdaptiveAggregationTransition::root_quorum_progress ||
        event.transition ==
            AdaptiveAggregationTransition::root_qc_published;
    if ((root_observation ||
         event.transition ==
             AdaptiveAggregationTransition::configuration_active) &&
        event.global_quorum == 0)
        return false;
    if (root_observation &&
        event.root_signer_count != event.accepted_signers.size())
        return false;
    if (event.transition ==
            AdaptiveAggregationTransition::root_qc_published &&
        event.root_signer_count < event.global_quorum)
        return false;

    ReplicaID previous_child{0};
    bool first_child = true;
    for (const auto &gap : event.required_branch_gaps)
    {
        if (gap.missing_required_signers.empty() ||
            !strictly_increasing(gap.missing_required_signers) ||
            (!first_child && gap.direct_child <= previous_child))
            return false;
        previous_child = gap.direct_child;
        first_child = false;
    }
    return true;
}

void append_configuration(JsonLineBuilder &builder,
                          const ConfigurationId &configuration)
{
    builder.append("\"epoch_number\":");
    builder.append_integer(configuration.epoch_number);
    builder.append(",\"tree_id\":");
    builder.append_integer(configuration.tree_id);
    builder.append(",\"epoch_digest\":");
    builder.append_escaped(configuration.epoch_digest.to_hex());
}

std::string hexadecimal_bytes(const bytearray_t &bytes)
{
    static constexpr char digits[] = "0123456789abcdef";
    std::string result;
    if (bytes.size() > std::numeric_limits<std::size_t>::max() / 2)
        throw LineLimitExceeded{};
    result.reserve(bytes.size() * 2);
    for (const auto byte : bytes)
    {
        result.push_back(digits[(byte >> 4) & 0x0f]);
        result.push_back(digits[byte & 0x0f]);
    }
    return result;
}

void append_activation_ready_identity(
    JsonLineBuilder &builder,
    const ActivationReadyIdentityV1 &identity)
{
    builder.append("{\"schema_version\":");
    builder.append_integer(identity.schema_version);
    builder.append(",\"membership_digest\":");
    builder.append_escaped(identity.membership_digest.to_hex());
    builder.append(",\"predecessor_boundary_configuration\":{");
    append_configuration(
        builder, identity.predecessor_boundary_configuration);
    builder.append("},\"predecessor_boundary_generation\":");
    builder.append_integer(identity.predecessor_boundary_generation);
    builder.append(",\"successor_configuration\":{");
    append_configuration(builder, identity.successor_configuration);
    builder.append("},\"successor_activation_generation\":");
    builder.append_integer(identity.successor_activation_generation);
    builder.append(",\"command_payload_digest\":");
    builder.append_escaped(identity.command_payload_digest.to_hex());
    builder.append(",\"command_block_height\":");
    builder.append_integer(identity.command_block_height);
    builder.append(",\"command_block_hash\":");
    builder.append_escaped(identity.command_block_hash.to_hex());
    builder.append(",\"activation_delay_blocks\":");
    builder.append_integer(identity.activation_delay_blocks);
    builder.append(",\"activation_height\":");
    builder.append_integer(identity.activation_height);
    builder.append(",\"activation_boundary_block_hash\":");
    builder.append_escaped(identity.activation_boundary_block_hash.to_hex());
    builder.append('}');
}

void append_process_payload(JsonLineBuilder &builder,
                            const ProcessLifecycleEvent &event)
{
    builder.append("{\"exit_status\":");
    if (event.exit_status)
        builder.append_integer(*event.exit_status);
    else
        builder.append("null");
    builder.append('}');
}

void append_epoch_payload(JsonLineBuilder &builder,
                          const EpochLifecycleEvent &event)
{
    builder.append('{');
    append_configuration(builder, event.configuration);
    builder.append(",\"activation_height\":");
    builder.append_integer(event.activation_height);
    if (event.certificate_apply_committed_height.has_value())
    {
        builder.append(",\"certificate_apply_committed_height\":");
        builder.append_integer(*event.certificate_apply_committed_height);
        builder.append(",\"activation_readiness_certificate_digest\":");
        builder.append_escaped(
            event.activation_readiness_certificate_digest->to_hex());
    }
    builder.append('}');
}

void append_commit_payload(JsonLineBuilder &builder,
                           const StructuredEventConfig &config,
                           const CommitStructuredEvent &event)
{
    builder.append("{\"block_height\":");
    builder.append_integer(event.block_height);
    builder.append(",\"block_hash\":");
    builder.append_escaped(event.block_hash.to_hex());
    builder.append(",\"parent_hash\":");
    if (event.parent_hash)
        builder.append_escaped(event.parent_hash->to_hex());
    else
        builder.append("null");
    builder.append(",\"transaction_count\":");
    builder.append_integer(event.transaction_count);
    builder.append(",\"designated_observer\":");
    const bool designated = config.designated_commit_observer &&
        same_source(config.source, *config.designated_commit_observer);
    builder.append(designated ? "true" : "false");
    builder.append(",\"decision_proof\":{");
    append_configuration(builder, event.decision_proof.configuration);
    builder.append(",\"block_hash\":");
    builder.append_escaped(event.decision_proof.block_hash.to_hex());
    builder.append("},\"view_generation\":");
    if (event.view_generation)
        builder.append_integer(*event.view_generation);
    else
        builder.append("null");
    builder.append(",\"commit_batch_index\":");
    builder.append_integer(event.commit_batch_index);
    if (event.reporter_local_commit_monotonic_ns.has_value())
    {
        builder.append(
            ",\"reporter_local_commit_monotonic_ns\":");
        builder.append_integer(
            *event.reporter_local_commit_monotonic_ns);
    }
    builder.append('}');
}

void append_commit_observed_payload(
    JsonLineBuilder &builder,
    const CommitObservedStructuredEvent &event)
{
    builder.append("{\"block_height\":");
    builder.append_integer(event.block_height);
    builder.append(",\"block_hash\":");
    builder.append_escaped(event.block_hash.to_hex());
    builder.append(",\"parent_hash\":");
    if (event.parent_hash)
        builder.append_escaped(event.parent_hash->to_hex());
    else
        builder.append("null");
    builder.append(",\"transaction_count\":");
    builder.append_integer(event.transaction_count);
    builder.append(",\"commit_batch_index\":");
    builder.append_integer(event.commit_batch_index);
    builder.append('}');
}

void append_commit_identity_unavailable_payload(
    JsonLineBuilder &builder,
    const CommitIdentityUnavailableStructuredEvent &event)
{
    builder.append("{\"block_height\":");
    builder.append_integer(event.block_height);
    builder.append(",\"block_hash\":");
    builder.append_escaped(event.block_hash.to_hex());
    builder.append(",\"parent_hash\":");
    if (event.parent_hash)
        builder.append_escaped(event.parent_hash->to_hex());
    else
        builder.append("null");
    builder.append(",\"transaction_count\":");
    builder.append_integer(event.transaction_count);
    builder.append(",\"commit_batch_index\":");
    builder.append_integer(event.commit_batch_index);
    builder.append(",\"reason\":");
    builder.append_escaped("no_authenticated_exact_identity_source");
    builder.append(",\"convergence_identity_pending\":false}");
}

void append_commit_identity_witness_payload(
    JsonLineBuilder &builder,
    const CommitIdentityWitnessStructuredEvent &event)
{
    builder.append("{\"block_height\":");
    builder.append_integer(event.block_height);
    builder.append(",\"block_hash\":");
    builder.append_escaped(event.block_hash.to_hex());
    builder.append(",\"parent_hash\":");
    if (event.parent_hash)
        builder.append_escaped(event.parent_hash->to_hex());
    else
        builder.append("null");
    builder.append(",\"transaction_count\":");
    builder.append_integer(event.transaction_count);
    builder.append(",\"decision_proof\":{");
    append_configuration(builder, event.decision_proof.configuration);
    builder.append(",\"block_hash\":");
    builder.append_escaped(event.decision_proof.block_hash.to_hex());
    builder.append("},\"view_generation\":");
    builder.append_integer(event.view_generation);
    builder.append(",\"commit_batch_index\":");
    builder.append_integer(event.commit_batch_index);
    builder.append('}');
}

void append_fault_contribution_opportunity_payload(
    JsonLineBuilder &builder,
    const FaultContributionOpportunityStructuredEvent &event)
{
    builder.append("{\"actor\":");
    builder.append_integer(event.actor);
    builder.append(",\"proposal\":{");
    append_configuration(builder, event.proposal.configuration);
    builder.append(",\"block_hash\":");
    builder.append_escaped(event.proposal.block_hash.to_hex());
    builder.append("},\"view_generation\":");
    builder.append_integer(event.view_generation);
    builder.append(",\"physical_role\":");
    builder.append_escaped(
        experiment_replica_role_name(event.physical_role));
    builder.append(",\"parent_replica\":");
    builder.append_integer(event.parent_replica);
    builder.append(",\"authenticated_proposal_source_replica\":");
    builder.append_integer(
        event.authenticated_proposal_source_replica);
    builder.append(",\"expected_message_type\":");
    builder.append_escaped(
        expected_message_type_name(event.expected_message_type));
    builder.append(",\"cohort\":");
    builder.append_escaped(
        experiment_omission_cohort_name(event.cohort));
    builder.append(",\"diagnostic_window\":");
    builder.append_escaped(event.diagnostic_window);
    builder.append(",\"window_start_monotonic_ns\":");
    builder.append_integer(event.window_start_monotonic_ns);
    builder.append(",\"window_end_monotonic_ns\":");
    builder.append_integer(event.window_end_monotonic_ns);
    builder.append(",\"decision_monotonic_ns\":");
    builder.append_integer(event.decision_monotonic_ns);
    builder.append(",\"contribution_ordinal\":");
    builder.append_integer(event.contribution_ordinal);
    builder.append(",\"role_contribution_ordinal\":");
    builder.append_integer(event.role_contribution_ordinal);
    builder.append(",\"scheduled_action\":");
    builder.append_escaped(
        experiment_omission_action_name(event.scheduled_action));
    builder.append(",\"responsive_omission_period\":");
    builder.append_integer(event.responsive_omission_period);
    builder.append(",\"fault_threshold\":");
    builder.append_integer(event.fault_threshold);
    builder.append(",\"hard_actor_count\":");
    builder.append_integer(event.hard_actor_count);
    builder.append(",\"responsive_degraded_actor_count\":");
    builder.append_integer(event.responsive_degraded_actor_count);
    builder.append(",\"fault_mode\":");
    builder.append_escaped(event.fault_mode);
    builder.append('}');
}

void append_root_qc_queue_blocked_payload(
    JsonLineBuilder &builder,
    const RootQcQueueBlockedStructuredEvent &event)
{
    builder.append('{');
    append_configuration(builder, event.configuration);
    builder.append(",\"observer_replica\":");
    builder.append_integer(event.observer_replica);
    builder.append(",\"global_quorum\":");
    builder.append_integer(event.global_quorum);
    builder.append(",\"queue_head_position\":");
    builder.append_integer(event.queue_head_position);
    builder.append(",\"queued_candidate_position\":");
    builder.append_integer(event.queued_candidate_position);
    builder.append(",\"queue_head_context_generation\":");
    builder.append_integer(event.queue_head_context_generation);
    builder.append(",\"queued_candidate_context_generation\":");
    builder.append_integer(event.queued_candidate_context_generation);
    builder.append(",\"queue_head_block_height\":");
    builder.append_integer(event.queue_head_block_height);
    builder.append(",\"queue_head_block_hash\":");
    builder.append_escaped(event.queue_head_block_hash.to_hex());
    builder.append(",\"queued_candidate_block_height\":");
    builder.append_integer(event.queued_candidate_block_height);
    builder.append(",\"queued_candidate_block_hash\":");
    builder.append_escaped(event.queued_candidate_block_hash.to_hex());
    builder.append(",\"queued_candidate_parent_hash\":");
    builder.append_escaped(event.queued_candidate_parent_hash.to_hex());
    builder.append(",\"queue_head_signer_count\":");
    builder.append_integer(event.queue_head_signer_count);
    builder.append(",\"queued_candidate_signer_count\":");
    builder.append_integer(event.queued_candidate_signer_count);
    builder.append(",\"queued_candidate_qc_ready\":");
    builder.append(event.queued_candidate_qc_ready ? "true" : "false");
    builder.append(",\"queued_candidate_qc_published\":");
    builder.append(
        event.queued_candidate_qc_published ? "true" : "false");
    builder.append('}');
}

void append_epoch_command_payload(
    JsonLineBuilder &builder,
    const EpochCommandCommittedStructuredEvent &event)
{
    builder.append("{\"command_block_height\":");
    builder.append_integer(event.command_block_height);
    builder.append(",\"command_block_hash\":");
    builder.append_escaped(event.command_block_hash.to_hex());
    builder.append(",\"payload_digest\":");
    builder.append_escaped(event.payload_digest.to_hex());
    builder.append(",\"predecessor_epoch_number\":");
    builder.append_integer(event.predecessor_epoch_number);
    builder.append(",\"predecessor_epoch_digest\":");
    builder.append_escaped(event.predecessor_epoch_digest.to_hex());
    builder.append(",\"successor_epoch_number\":");
    builder.append_integer(event.successor_epoch_number);
    builder.append(",\"successor_epoch_digest\":");
    builder.append_escaped(event.successor_epoch_digest.to_hex());
    builder.append(",\"activation_delay_blocks\":");
    builder.append_integer(event.activation_delay_blocks);
    builder.append(",\"activation_height\":");
    builder.append_integer(event.activation_height);
    builder.append('}');
}

void append_adaptive_v3_command_terminal_payload(
    JsonLineBuilder &builder,
    const AdaptiveV3CommandTerminalStructuredEvent &event)
{
    const auto &command = event.command;
    builder.append("{\"command_block_height\":");
    builder.append_integer(command.command_block_height);
    builder.append(",\"command_block_hash\":");
    builder.append_escaped(command.command_block_hash.to_hex());
    builder.append(",\"payload_digest\":");
    builder.append_escaped(command.payload_digest.to_hex());
    builder.append(",\"predecessor_epoch_number\":");
    builder.append_integer(command.predecessor_epoch_number);
    builder.append(",\"predecessor_epoch_digest\":");
    builder.append_escaped(command.predecessor_epoch_digest.to_hex());
    builder.append(",\"successor_epoch_number\":");
    builder.append_integer(command.successor_epoch_number);
    builder.append(",\"successor_epoch_digest\":");
    builder.append_escaped(command.successor_epoch_digest.to_hex());
    builder.append(",\"activation_delay_blocks\":");
    builder.append_integer(command.activation_delay_blocks);
    builder.append(",\"activation_height\":");
    builder.append_integer(command.activation_height);
    builder.append(",\"disposition\":");
    builder.append_escaped(
        adaptive_v3_command_terminal_reason_name(event.reason));
    builder.append('}');
}

void append_reputation_payload(
    JsonLineBuilder &builder,
    const ReputationEvidenceAppliedStructuredEvent &event)
{
    const auto &update = event.update;
    builder.append("{\"evidence_cutoff\":");
    builder.append_integer(event.evidence_cutoff);
    builder.append(",\"ingestion_sequence\":");
    builder.append_integer(update.ingestion_sequence);
    builder.append(",\"observation_id\":");
    builder.append_escaped(update.observation_id.to_hex());
    builder.append(",\"reporter_id\":");
    builder.append_integer(update.reporter_id);
    builder.append(",\"target_id\":");
    builder.append_integer(update.target_id);
    builder.append(",\"evidence_outcome\":");
    builder.append_escaped(response_outcome_name(update.evidence_outcome));
    builder.append(",\"reputation_outcome\":");
    builder.append_escaped(
        reputation_outcome_name(update.reputation_outcome));
    builder.append(",\"delta\":");
    builder.append_integer(update.delta);
    builder.append(",\"resulting_score\":");
    builder.append_integer(update.score);
    builder.append('}');
}

void append_observation_accepted_payload(
    JsonLineBuilder &builder,
    const EvidenceObservationAcceptedStructuredEvent &event)
{
    const auto &record = event.record;
    const auto &observation = record.observation;
    builder.append("{\"ingestion_sequence\":");
    builder.append_integer(record.ingestion_sequence);
    builder.append(",\"observation\":{\"schema_version\":");
    builder.append_integer(observation.schema_version);
    builder.append(",\"observation_id\":");
    builder.append_escaped(observation.observation_id.to_hex());
    builder.append(",\"reporter_id\":");
    builder.append_integer(observation.reporter_id);
    builder.append(",\"observed_replica_id\":");
    builder.append_integer(observation.observed_replica_id);
    builder.append(",\"configuration\":{");
    append_configuration(builder, observation.configuration);
    builder.append("},\"block_hash\":");
    builder.append_escaped(observation.block_hash.to_hex());
    builder.append(",\"expected_message_type\":");
    builder.append_escaped(expected_message_type_name(
        observation.expected_message_type));
    builder.append(",\"outcome\":");
    builder.append_escaped(response_outcome_name(
        observation.outcome));
    builder.append(",\"response_duration_us\":");
    builder.append_integer(observation.response_duration_us);
    builder.append(",\"deadline_duration_us\":");
    builder.append_integer(observation.deadline_duration_us);
    builder.append(",\"reporter_monotonic_ns\":");
    builder.append_integer(observation.reporter_monotonic_ns);
    builder.append(",\"reporter_sequence\":");
    builder.append_integer(observation.reporter_sequence);
    if (observation.schema_version ==
            kResponseObservationSchemaVersionV2 ||
        observation.schema_version ==
            kResponseObservationSchemaVersionV3)
    {
        builder.append(",\"attempt_start_monotonic_ns\":");
        builder.append_integer(
            observation.attempt_start_monotonic_ns);
        builder.append(
            ",\"reporter_local_commit_monotonic_ns\":");
        builder.append_integer(
            observation.reporter_local_commit_monotonic_ns);
    }
    builder.append(",\"signer_set\":[");
    bool first = true;
    for (const auto signer : observation.signer_set)
    {
        if (!first)
            builder.append(',');
        builder.append_integer(signer);
        first = false;
    }
    builder.append("]}}");
}

void append_u32_ids(
    JsonLineBuilder &builder,
    const std::vector<std::uint32_t> &values)
{
    builder.append('[');
    bool first = true;
    for (const auto value : values)
    {
        if (!first)
            builder.append(',');
        builder.append_integer(value);
        first = false;
    }
    builder.append(']');
}

void append_fault_containment_coverage_ready_payload(
    JsonLineBuilder &builder,
    const AdaptiveV2FaultContainmentCoverageReadyStructuredEvent &event)
{
    builder.append("{\"cycle_ordinal\":");
    builder.append_integer(event.cycle_ordinal);
    builder.append(",\"transition_artifact_id\":");
    builder.append_escaped(event.transition_artifact_id);
    builder.append(",\"predecessor_epoch_number\":");
    builder.append_integer(event.predecessor_epoch_number);
    builder.append(",\"predecessor_epoch_digest\":");
    builder.append_escaped(event.predecessor_epoch_digest.to_hex());
    builder.append(",\"fault_evidence_start_monotonic_ns\":");
    builder.append_integer(
        event.fault_evidence_start_monotonic_ns);
    builder.append(",\"evidence_cutoff\":");
    builder.append_integer(event.evidence_cutoff);
    builder.append(",\"required_tree_ids\":");
    append_u32_ids(builder, event.required_tree_ids);
    builder.append(",\"observed_tree_ids\":");
    append_u32_ids(builder, event.observed_tree_ids);
    builder.append('}');
}

void append_cross_commit_retention_ready_payload(
    JsonLineBuilder &builder,
    const AdaptiveV2CrossCommitRetentionReadyStructuredEvent &event)
{
    builder.append("{\"cycle_ordinal\":");
    builder.append_integer(event.cycle_ordinal);
    builder.append(",\"predecessor_epoch_number\":");
    builder.append_integer(event.predecessor_epoch_number);
    builder.append(",\"predecessor_epoch_digest\":");
    builder.append_escaped(event.predecessor_epoch_digest.to_hex());
    builder.append(",\"evidence_cutoff\":");
    builder.append_integer(event.evidence_cutoff);
    builder.append(",\"responsive_degraded_actor_ids\":[");
    bool first = true;
    for (const auto actor : event.responsive_degraded_actor_ids)
    {
        if (!first)
            builder.append(',');
        builder.append_integer(actor);
        first = false;
    }
    builder.append("],\"admitted_observation_ids\":[");
    first = true;
    for (const auto &observation_id : event.admitted_observation_ids)
    {
        if (!first)
            builder.append(',');
        builder.append_escaped(observation_id.to_hex());
        first = false;
    }
    builder.append("]}");
}

void append_fault_window_armed_payload(
    JsonLineBuilder &builder, const FaultWindowArmedStructuredEvent &event)
{
    builder.append("{\"schema_version\":");
    builder.append_integer(event.schema_version);
    builder.append(",\"kind\":"); builder.append_escaped(event.kind);
    builder.append(",\"run_id\":"); builder.append_escaped(event.run_id);
    builder.append(",\"profile_id\":"); builder.append_escaped(event.profile_id);
    builder.append(",\"profile_sha256\":"); builder.append_escaped(event.profile_sha256);
    builder.append(",\"topology_proof_sha256\":"); builder.append_escaped(event.topology_proof_sha256);
    builder.append(",\"request_sha256\":"); builder.append_escaped(event.request_sha256);
    builder.append(",\"epoch_number\":"); builder.append_integer(event.epoch_number);
    builder.append(",\"epoch_digest\":"); builder.append_escaped(event.epoch_digest.to_hex());
    builder.append(",\"fault_receipt_sha256\":"); builder.append_escaped(event.fault_receipt_sha256);
    builder.append(",\"evidence_start_monotonic_ns\":"); builder.append_integer(event.evidence_start_monotonic_ns);
    builder.append(",\"prefault_tree_id\":"); builder.append_integer(event.prefault_tree_id);
    builder.append(",\"required_tree_positions\":"); builder.append_integer(event.required_tree_positions);
    builder.append(",\"required_tree_ids\":"); append_u32_ids(builder, event.required_tree_ids);
    if (event.schema_version == 2 || event.schema_version == 3 ||
        event.schema_version == 4)
    {
        builder.append(",\"clock_domain\":"); builder.append_escaped(event.clock_domain);
        builder.append(",\"required_observation_schema\":"); builder.append_integer(event.required_observation_schema);
        if (event.schema_version == 3 || event.schema_version == 4)
        {
            builder.append(",\"snapshot_evidence_basis\":");
            builder.append_escaped(event.snapshot_evidence_basis);
        }
        if (event.schema_version == 4)
        {
            builder.append(",\"selection_cardinality_policy\":");
            builder.append_escaped(event.selection_cardinality_policy);
        }
        builder.append(",\"timeout_evidence_basis\":"); builder.append_escaped(event.timeout_evidence_basis);
    }
    builder.append(",\"fault_window_arm_sha256\":"); builder.append_escaped(event.fault_window_arm_sha256);
    builder.append('}');
}

void append_evidence_snapshot_payload(
    JsonLineBuilder &builder,
    const AdaptiveV2EvidenceSnapshotStructuredEvent &event)
{
    builder.append("{\"schema_version\":");
    builder.append_integer(event.schema_version);
    builder.append(",\"cycle_ordinal\":");
    builder.append_integer(event.cycle_ordinal);
    builder.append(",\"policy_intent\":");
    builder.append_escaped(tree_policy_kind_name(event.policy_intent));
    builder.append(",\"transition_artifact_id\":");
    builder.append_escaped(event.transition_artifact_id);
    builder.append(",\"predecessor_epoch_number\":");
    builder.append_integer(event.predecessor_epoch_number);
    builder.append(",\"predecessor_epoch_digest\":");
    builder.append_escaped(event.predecessor_epoch_digest.to_hex());
    builder.append(",\"activation_generation\":");
    builder.append_integer(event.activation_generation);
    builder.append(",\"baseline_cutoff\":");
    builder.append_integer(event.baseline_cutoff);
    builder.append(",\"current_cutoff\":");
    builder.append_integer(event.current_cutoff);
    builder.append(",\"full_prefix_snapshot_id\":");
    builder.append_escaped(event.full_prefix_snapshot_id.to_hex());
    builder.append(",\"evidence_snapshot_id\":");
    builder.append_escaped(event.evidence_snapshot_id.to_hex());
    builder.append(",\"accepted_prefix_count\":");
    builder.append_integer(event.accepted_prefix_count);
    builder.append(",\"eligible_ranking\":[");
    bool first = true;
    for (const auto replica : event.eligible_ranking)
    {
        if (!first)
            builder.append(',');
        builder.append_integer(replica);
        first = false;
    }
    builder.append("]}");
}

void append_shape_decision_payload(
    JsonLineBuilder &builder,
    const AdaptiveV2ShapeDecisionStructuredEvent &event)
{
    const auto &decision = event.decision;
    builder.append("{\"cycle_ordinal\":");
    builder.append_integer(event.cycle_ordinal);
    builder.append(",\"transition_artifact_id\":");
    builder.append_escaped(event.transition_artifact_id);
    builder.append(",\"decision\":{\"schema_version\":");
    builder.append_integer(decision.schema_version);
    builder.append(",\"status\":");
    builder.append_escaped(shape_status_name(decision.status));
    builder.append(",\"selector_version\":");
    builder.append_escaped(decision.selector_version);
    builder.append(",\"tie_rule\":");
    builder.append_escaped(decision.tie_rule);
    builder.append(",\"epoch_number\":");
    builder.append_integer(decision.epoch_number);
    builder.append(",\"epoch_digest\":");
    builder.append_escaped(decision.epoch_digest.to_hex());
    builder.append(",\"current_topology_digest\":");
    builder.append_escaped(decision.current_topology_digest.to_hex());
    builder.append(",\"evidence_cutoff\":");
    builder.append_integer(decision.evidence_cutoff);
    builder.append(",\"evidence_digest\":");
    builder.append_escaped(decision.evidence_digest.to_hex());
    builder.append(",\"predecessor_tree_count\":");
    builder.append_integer(decision.predecessor_tree_count);
    builder.append(",\"tree_count\":");
    builder.append_integer(decision.tree_count);
    builder.append(",\"fixed_pipeline_stretch\":");
    builder.append_integer(decision.fixed_pipeline_stretch);
    builder.append(",\"deterministic_seed\":");
    builder.append_integer(decision.deterministic_seed);
    builder.append(",\"current_fanout\":");
    builder.append_integer(decision.current_fanout);
    builder.append(",\"selected_fanout\":");
    builder.append_integer(decision.selected_fanout);
    builder.append(",\"applied_fanout\":");
    builder.append_integer(decision.applied_fanout);
    builder.append(",\"reference_tree_rule\":");
    builder.append_escaped(decision.reference_tree_rule);
    builder.append(",\"candidates\":[");
    bool first = true;
    for (const auto &candidate : decision.candidates)
    {
        if (!first)
            builder.append(',');
        builder.append("{\"fanout\":");
        builder.append_integer(candidate.fanout);
        builder.append(",\"rejection\":");
        builder.append_escaped(
            shape_rejection_name(candidate.rejection));
        builder.append(",\"depth\":");
        builder.append_integer(candidate.depth);
        builder.append(",\"risk\":");
        builder.append_integer(candidate.risk);
        builder.append(",\"latency\":");
        builder.append_integer(candidate.latency);
        builder.append(",\"churn\":");
        builder.append_integer(candidate.churn);
        builder.append(",\"switch_threshold_satisfied\":");
        builder.append(
            candidate.switch_threshold_satisfied ? "true" : "false");
        builder.append('}');
        first = false;
    }
    builder.append("],\"decision_digest\":");
    builder.append_escaped(decision.decision_digest.to_hex());
    builder.append("}}");
}

void append_convergence_identity(
    JsonLineBuilder &builder,
    const AdaptiveV2EpochChangeIdentity &identity)
{
    builder.append("{\"predecessor_epoch_number\":");
    builder.append_integer(identity.predecessor_epoch_number);
    builder.append(",\"predecessor_epoch_digest\":");
    builder.append_escaped(identity.predecessor_epoch_digest.to_hex());
    builder.append(",\"successor_epoch_number\":");
    builder.append_integer(identity.successor_epoch_number);
    builder.append(",\"successor_epoch_digest\":");
    builder.append_escaped(identity.successor_epoch_digest.to_hex());
    builder.append(",\"command_payload_digest\":");
    builder.append_escaped(identity.command_payload_digest.to_hex());
    builder.append(",\"command_block_height\":");
    builder.append_integer(identity.command_block_height);
    builder.append(",\"command_block_hash\":");
    builder.append_escaped(identity.command_block_hash.to_hex());
    builder.append(",\"activation_delay_blocks\":");
    builder.append_integer(identity.activation_delay_blocks);
    builder.append(",\"activation_height\":");
    builder.append_integer(identity.activation_height);
    builder.append('}');
}

void append_convergence_payload(
    JsonLineBuilder &builder,
    const AdaptiveV2ConvergenceStructuredEvent &event)
{
    builder.append("{\"replica_id\":");
    if (event.replica_id.has_value())
        builder.append_integer(*event.replica_id);
    else
        builder.append("null");
    builder.append(",\"delivery_attempt\":");
    if (event.delivery_attempt != 0)
        builder.append_integer(event.delivery_attempt);
    else
        builder.append("null");
    builder.append(",\"disposition\":");
    if (event.disposition.empty())
        builder.append("null");
    else
        builder.append_escaped(event.disposition);
    builder.append(",\"identity\":");
    if (event.identity.has_value())
        append_convergence_identity(builder, *event.identity);
    else
        builder.append("null");
    builder.append(",\"accepted_commit_count\":");
    builder.append_integer(event.accepted_commit_count);
    builder.append(",\"accepted_activation_count\":");
    builder.append_integer(event.accepted_activation_count);
    builder.append(",\"required_activation_count\":");
    builder.append_integer(event.required_activation_count);
    builder.append(",\"canonical_payload_digest\":");
    if (event.canonical_payload_digest.has_value())
        builder.append_escaped(
            event.canonical_payload_digest->to_hex());
    else
        builder.append("null");
    builder.append(",\"failure_reason\":");
    if (event.failure_reason.empty())
        builder.append("null");
    else
        builder.append_escaped(event.failure_reason);
    builder.append('}');
}

void append_manager_session_terminal_payload(
    JsonLineBuilder &builder,
    const AdaptiveV2ManagerSessionTerminalStructuredEvent &event)
{
    builder.append("{\"cycle_ordinal\":");
    builder.append_integer(event.cycle_ordinal);
    builder.append(",\"policy_intent\":");
    builder.append_escaped(tree_policy_kind_name(event.policy_intent));
    builder.append(",\"outcome\":");
    builder.append_escaped(manager_cycle_outcome_name(event.outcome));
    builder.append(",\"reason\":");
    builder.append_escaped(manager_cycle_reason_name(event.reason));
    builder.append(",\"transition_artifact_id\":");
    builder.append_escaped(event.transition_artifact_id);
    builder.append(",\"predecessor_epoch_number\":");
    builder.append_integer(event.predecessor_epoch_number);
    builder.append(",\"predecessor_epoch_digest\":");
    builder.append_escaped(event.predecessor_epoch_digest.to_hex());
    builder.append(",\"successor_epoch_number\":");
    if (event.successor_epoch_number.has_value())
        builder.append_integer(*event.successor_epoch_number);
    else
        builder.append("null");
    builder.append(",\"successor_epoch_digest\":");
    if (event.successor_epoch_digest.has_value())
        builder.append_escaped(event.successor_epoch_digest->to_hex());
    else
        builder.append("null");
    builder.append(",\"command_payload_digest\":");
    if (event.command_payload_digest.has_value())
        builder.append_escaped(event.command_payload_digest->to_hex());
    else
        builder.append("null");
    builder.append(",\"winning_activation\":");
    if (event.winning_activation.has_value())
        append_convergence_identity(builder, *event.winning_activation);
    else
        builder.append("null");
    builder.append(",\"evidence_window_activation_generation\":");
    builder.append_integer(event.evidence_window_activation_generation);
    builder.append(",\"baseline_evidence_cutoff\":");
    builder.append_integer(event.baseline_evidence_cutoff);
    builder.append(",\"current_evidence_cutoff\":");
    builder.append_integer(event.current_evidence_cutoff);
    builder.append(",\"controller_failure\":");
    if (!event.controller_failure.has_value())
        builder.append("null");
    else
    {
        const auto &detail = *event.controller_failure;
        builder.append("{\"stage\":");
        builder.append_escaped(controller_failure_stage_name(detail.stage));
        builder.append(",\"selection_status\":");
        if (detail.selection_status.has_value())
            builder.append_escaped(selection_status_name(*detail.selection_status));
        else
            builder.append("null");
        builder.append(",\"epoch_factory_status\":");
        if (detail.epoch_factory_status.has_value())
            builder.append_escaped(
                epoch_factory_status_name(*detail.epoch_factory_status));
        else
            builder.append("null");
        builder.append('}');
    }
    builder.append('}');
}

void append_replica_ids(
    JsonLineBuilder &builder,
    const std::vector<ReplicaID> &values);

void append_optional_digest(
    JsonLineBuilder &builder,
    const std::optional<uint256_t> &digest)
{
    if (digest.has_value())
        builder.append_escaped(digest->to_hex());
    else
        builder.append("null");
}

void append_adaptive_v3_readiness_payload(
    JsonLineBuilder &builder,
    const AdaptiveV3ReadinessStructuredEvent &event)
{
    builder.append("{\"identity\":");
    if (event.transition == AdaptiveV3ReadinessTransition::terminal &&
        event.terminal_cycle_ordinal.has_value())
    {
        if (event.terminal_identity.has_value())
            append_activation_ready_identity(builder, *event.terminal_identity);
        else
            builder.append("null");
    }
    else if (event.transition == AdaptiveV3ReadinessTransition::wire_rejected)
        builder.append("null");
    else
        append_activation_ready_identity(builder, event.identity);
    builder.append(",\"replica_id\":");
    if (event.replica_id.has_value())
        builder.append_integer(*event.replica_id);
    else
        builder.append("null");
    builder.append(",\"signer_source_sequence\":");
    if (event.signer_source_sequence.has_value())
        builder.append_integer(*event.signer_source_sequence);
    else
        builder.append("null");
    builder.append(",\"signer_monotonic_raw_ns\":");
    if (event.signer_monotonic_raw_ns.has_value())
        builder.append_integer(*event.signer_monotonic_raw_ns);
    else
        builder.append("null");
    builder.append(",\"observation_digest\":");
    append_optional_digest(builder, event.observation_digest);
    builder.append(",\"certificate_digest\":");
    append_optional_digest(builder, event.certificate_digest);
    builder.append(",\"payload_digest\":");
    append_optional_digest(builder, event.payload_digest);
    builder.append(",\"observed_signers\":");
    append_replica_ids(builder, event.observed_signers);
    builder.append(",\"required_release_count\":");
    builder.append_integer(event.required_release_count);
    builder.append(",\"delivery_attempt\":");
    builder.append_integer(event.delivery_attempt);
    builder.append(",\"delivery_enqueued\":");
    builder.append(event.delivery_enqueued ? "true" : "false");
    builder.append(",\"canonical_wire_payload_hex\":");
    if (event.canonical_wire_payload.has_value())
        builder.append_escaped(
            hexadecimal_bytes(*event.canonical_wire_payload));
    else
        builder.append("null");
    builder.append(",\"wire_opcode\":");
    if (event.wire_opcode.has_value()) builder.append_integer(*event.wire_opcode); else builder.append("null");
    builder.append(",\"wire_payload_size\":");
    if (event.wire_payload_size.has_value()) builder.append_integer(*event.wire_payload_size); else builder.append("null");
    builder.append(",\"disposition\":");
    if (event.disposition.empty())
        builder.append("null");
    else
        builder.append_escaped(event.disposition);
    builder.append(",\"terminal_cycle_ordinal\":");
    if (event.terminal_cycle_ordinal.has_value())
        builder.append_integer(*event.terminal_cycle_ordinal);
    else
        builder.append("null");
    builder.append(",\"terminal_reason\":");
    if (event.terminal_reason.has_value())
        builder.append_integer(*event.terminal_reason);
    else
        builder.append("null");
    builder.append(",\"terminal_identity\":");
    if (event.terminal_identity.has_value())
        append_activation_ready_identity(builder, *event.terminal_identity);
    else
        builder.append("null");
    builder.append(",\"terminal_bundle_digest\":");
    append_optional_digest(builder, event.terminal_bundle_digest);
    builder.append(",\"e2_cycle_ordinal\":");
    if (event.e2_cycle_ordinal.has_value()) builder.append_integer(*event.e2_cycle_ordinal); else builder.append("null");
    builder.append(",\"e1_bundle_digest\":");
    append_optional_digest(builder, event.e1_bundle_digest);
    builder.append(",\"e2_final_ack_raw_ns\":");
    if (event.e2_final_ack_raw_ns.has_value()) builder.append_integer(*event.e2_final_ack_raw_ns); else builder.append("null");
    builder.append(",\"e2_common_commit\":");
    if (event.e2_common_commit.has_value()) {
        builder.append("{"); append_configuration(builder, event.e2_common_commit->configuration);
        builder.append(",\"block_hash\":"); builder.append_escaped(event.e2_common_commit->block_hash.to_hex()); builder.append("}");
    } else builder.append("null");
    builder.append(",\"e2_common_commit_sources\":"); append_replica_ids(builder, event.e2_common_commit_sources);
    builder.append(",\"e2_common_commit_raw_ns\":");
    if (event.e2_common_commit_raw_ns.has_value()) builder.append_integer(*event.e2_common_commit_raw_ns); else builder.append("null");
    builder.append(",\"e2_earliest_raw_ns\":");
    if (event.e2_earliest_raw_ns.has_value()) builder.append_integer(*event.e2_earliest_raw_ns); else builder.append("null");
    builder.append(",\"e2_actual_begin_raw_ns\":");
    if (event.e2_actual_begin_raw_ns.has_value()) builder.append_integer(*event.e2_actual_begin_raw_ns); else builder.append("null");
    builder.append(",\"e2_hard_deadline_raw_ns\":");
    if (event.e2_hard_deadline_raw_ns.has_value()) builder.append_integer(*event.e2_hard_deadline_raw_ns); else builder.append("null");
    builder.append(",\"e2_reserve_raw_ns\":");
    if (event.e2_reserve_raw_ns.has_value()) builder.append_integer(*event.e2_reserve_raw_ns); else builder.append("null");
    builder.append('}');
}

void append_replica_ids(JsonLineBuilder &builder,
                        const std::vector<ReplicaID> &values)
{
    builder.append('[');
    bool first = true;
    for (const auto value : values)
    {
        if (!first)
            builder.append(',');
        builder.append_integer(value);
        first = false;
    }
    builder.append(']');
}

void append_adaptive_payload(
    JsonLineBuilder &builder,
    const AdaptiveAggregationStructuredEvent &event)
{
    builder.append('{');
    append_configuration(builder, event.configuration);
    builder.append(",\"block_hash\":");
    if (event.block_hash)
        builder.append_escaped(event.block_hash->to_hex());
    else
        builder.append("null");
    builder.append(",\"context_generation\":");
    if (event.context_generation)
        builder.append_integer(*event.context_generation);
    else
        builder.append("null");
    builder.append(",\"observer_replica\":");
    builder.append_integer(event.observer_replica);
    builder.append(",\"wait_exempt_signers\":");
    append_replica_ids(builder, event.wait_exempt_signers);
    builder.append(",\"accepted_signers\":");
    append_replica_ids(builder, event.accepted_signers);
    builder.append(",\"absent_direct_children\":");
    append_replica_ids(builder, event.absent_direct_children);
    builder.append(",\"missing_optional_signers\":");
    append_replica_ids(builder, event.missing_optional_signers);
    builder.append(",\"required_branch_gaps\":[");
    bool first = true;
    for (const auto &gap : event.required_branch_gaps)
    {
        if (!first)
            builder.append(',');
        builder.append("{\"direct_child\":");
        builder.append_integer(gap.direct_child);
        builder.append(",\"missing_required_signers\":");
        append_replica_ids(builder, gap.missing_required_signers);
        builder.append('}');
        first = false;
    }
    builder.append("]");
    builder.append(",\"root_signer_count\":");
    builder.append_integer(event.root_signer_count);
    builder.append(",\"global_quorum\":");
    builder.append_integer(event.global_quorum);
    builder.append(",\"rejection_reason\":");
    if (event.rejection_reason.empty())
        builder.append("null");
    else
        builder.append_escaped(event.rejection_reason);
    builder.append('}');
}

std::string serialize_event(const StructuredEventConfig &config,
                            const StructuredEventPayload &payload,
                            StructuredEventType type,
                            std::uint64_t sequence,
                            std::uint64_t monotonic_ns)
{
    JsonLineBuilder builder(config.limits.maximum_line_bytes);
    builder.append("{\"event_schema_version\":");
    builder.append_integer(kStructuredEventSchemaVersion);
    builder.append(",\"run_id\":");
    builder.append_escaped(config.run_id);
    builder.append(",\"source_kind\":");
    builder.append_escaped(source_kind_name(config.source.kind));
    builder.append(",\"source_id\":");
    builder.append_escaped(config.source.logical_id);
    builder.append(",\"source_instance\":");
    builder.append_escaped(config.source.instance_id);
    builder.append(",\"source_sequence\":");
    builder.append_integer(sequence);
    builder.append(",\"source_monotonic_ns\":");
    builder.append_integer(monotonic_ns);
    builder.append(",\"event_type\":");
    builder.append_escaped(structured_event_type_name(type));
    builder.append(",\"payload\":");

    switch (payload.index())
    {
        case 0:
            append_process_payload(
                builder, std::get<ProcessLifecycleEvent>(payload));
            break;
        case 1:
            append_epoch_payload(
                builder, std::get<EpochLifecycleEvent>(payload));
            break;
        case 2:
            append_commit_payload(
                builder, config, std::get<CommitStructuredEvent>(payload));
            break;
        case 3:
            append_commit_observed_payload(
                builder,
                std::get<CommitObservedStructuredEvent>(payload));
            break;
        case 4:
            append_commit_identity_unavailable_payload(
                builder,
                std::get<CommitIdentityUnavailableStructuredEvent>(payload));
            break;
        case 5:
            append_commit_identity_witness_payload(
                builder,
                std::get<CommitIdentityWitnessStructuredEvent>(payload));
            break;
        default:
            throw std::bad_variant_access{};
    }
    builder.append('}');
    return builder.finish();
}

void append_new_event_envelope(
    JsonLineBuilder &builder,
    const StructuredEventConfig &config,
    StructuredEventType type,
    std::uint64_t sequence,
    std::uint64_t monotonic_ns)
{
    builder.append("{\"event_schema_version\":");
    builder.append_integer(kStructuredEventSchemaVersion);
    builder.append(",\"run_id\":");
    builder.append_escaped(config.run_id);
    builder.append(",\"source_kind\":");
    builder.append_escaped(source_kind_name(config.source.kind));
    builder.append(",\"source_id\":");
    builder.append_escaped(config.source.logical_id);
    builder.append(",\"source_instance\":");
    builder.append_escaped(config.source.instance_id);
    builder.append(",\"source_sequence\":");
    builder.append_integer(sequence);
    builder.append(",\"source_monotonic_ns\":");
    builder.append_integer(monotonic_ns);
    builder.append(",\"event_type\":");
    builder.append_escaped(structured_event_type_name(type));
    builder.append(",\"payload\":");
}

std::string serialize_adaptive_event(
    const StructuredEventConfig &config,
    const AdaptiveAggregationStructuredEvent &event,
    StructuredEventType type,
    std::uint64_t sequence,
    std::uint64_t monotonic_ns)
{
    JsonLineBuilder builder(config.limits.maximum_line_bytes);
    append_new_event_envelope(
        builder, config, type, sequence, monotonic_ns);
    append_adaptive_payload(builder, event);
    builder.append('}');
    return builder.finish();
}

std::string serialize_audit_event(
    const StructuredEventConfig &config,
    const AuditStructuredEventPayload &event,
    StructuredEventType type,
    std::uint64_t sequence,
    std::uint64_t monotonic_ns)
{
    JsonLineBuilder builder(config.limits.maximum_line_bytes);
    append_new_event_envelope(
        builder, config, type, sequence, monotonic_ns);
    switch (event.index())
    {
        case 0:
            append_epoch_command_payload(
                builder,
                std::get<EpochCommandCommittedStructuredEvent>(event));
            break;
        case 1:
            append_reputation_payload(
                builder,
                std::get<ReputationEvidenceAppliedStructuredEvent>(event));
            break;
        case 2:
            append_convergence_payload(
                builder,
                std::get<AdaptiveV2ConvergenceStructuredEvent>(event));
            break;
        case 3:
            builder.append(
                serialize_adaptive_v2_evidence_snapshot_payload(
                    std::get<
                        AdaptiveV2EvidenceSnapshotStructuredEvent>(
                            event),
                    config.limits.maximum_line_bytes));
            break;
        case 4:
            append_manager_session_terminal_payload(
                builder,
                std::get<
                    AdaptiveV2ManagerSessionTerminalStructuredEvent>(
                        event));
            break;
        case 5:
            append_observation_accepted_payload(
                builder,
                std::get<
                    EvidenceObservationAcceptedStructuredEvent>(
                        event));
            break;
        case 6:
            append_shape_decision_payload(
                builder,
                std::get<AdaptiveV2ShapeDecisionStructuredEvent>(event));
            break;
        case 7:
            append_fault_contribution_opportunity_payload(
                builder,
                std::get<
                    FaultContributionOpportunityStructuredEvent>(event));
            break;
        case 8:
            append_root_qc_queue_blocked_payload(
                builder,
                std::get<RootQcQueueBlockedStructuredEvent>(event));
            break;
        case 9:
            append_fault_containment_coverage_ready_payload(
                builder,
                std::get<
                    AdaptiveV2FaultContainmentCoverageReadyStructuredEvent>(
                        event));
            break;
        case 10:
            append_cross_commit_retention_ready_payload(
                builder,
                std::get<
                    AdaptiveV2CrossCommitRetentionReadyStructuredEvent>(
                event));
            break;
        case 11:
            append_fault_window_armed_payload(
                builder, std::get<FaultWindowArmedStructuredEvent>(event));
            break;
        case 12:
            append_adaptive_v3_readiness_payload(
                builder,
                std::get<AdaptiveV3ReadinessStructuredEvent>(event));
            break;
        case 13:
            append_adaptive_v3_command_terminal_payload(
                builder,
                std::get<AdaptiveV3CommandTerminalStructuredEvent>(event));
            break;
        default:
            throw std::bad_variant_access{};
    }
    builder.append('}');
    return builder.finish();
}

bool add_identity_bytes(std::size_t value,
                        std::size_t maximum,
                        std::size_t &total) noexcept
{
    if (value > maximum || value > std::numeric_limits<std::size_t>::max() - total)
        return false;
    total += value;
    return true;
}

bool valid_identity_limits(const StructuredEventConfig &config) noexcept
{
    std::size_t total = 0;
    const auto per_identity = config.limits.maximum_identity_bytes;
    if (!add_identity_bytes(config.run_id.size(), per_identity, total) ||
        !add_identity_bytes(
            config.source.logical_id.size(), per_identity, total) ||
        !add_identity_bytes(
            config.source.instance_id.size(), per_identity, total))
        return false;

    if (config.designated_commit_observer &&
        (!add_identity_bytes(
             config.designated_commit_observer->logical_id.size(),
             per_identity,
             total) ||
         !add_identity_bytes(
             config.designated_commit_observer->instance_id.size(),
             per_identity,
             total)))
        return false;

    return total <= config.limits.maximum_total_identity_bytes;
}

bool valid_configuration(const StructuredEventConfig &config) noexcept
{
    return config.limits.maximum_line_bytes != 0 &&
           config.limits.maximum_queued_events != 0 &&
           config.limits.maximum_queued_bytes != 0 &&
           config.limits.maximum_identity_bytes != 0 &&
           config.limits.maximum_total_identity_bytes != 0 &&
           valid_identity(config.run_id) &&
           valid_source_identity(config.source) &&
           (!config.designated_commit_observer ||
            valid_source_identity(*config.designated_commit_observer));
}

bool is_hexadecimal(std::uint8_t value) noexcept
{
    return (value >= '0' && value <= '9') ||
           (value >= 'a' && value <= 'f') ||
           (value >= 'A' && value <= 'F');
}

std::uint16_t hexadecimal_value(std::uint8_t value) noexcept
{
    if (value >= '0' && value <= '9')
        return static_cast<std::uint16_t>(value - '0');
    if (value >= 'a' && value <= 'f')
        return static_cast<std::uint16_t>(value - 'a' + 10);
    return static_cast<std::uint16_t>(value - 'A' + 10);
}

class JsonSyntaxParser final
{
public:
    JsonSyntaxParser(const std::uint8_t *begin,
                     const std::uint8_t *end) noexcept
        : cursor_(begin), end_(end)
    {
    }

    bool parse() noexcept
    {
        skip_whitespace();
        if (!parse_value(0))
            return false;
        skip_whitespace();
        return cursor_ == end_;
    }

private:
    static constexpr std::size_t maximum_depth = 256;

    void skip_whitespace() noexcept
    {
        while (cursor_ != end_ &&
               (*cursor_ == ' ' || *cursor_ == '\t' ||
                *cursor_ == '\r' || *cursor_ == '\n'))
            ++cursor_;
    }

    bool consume(std::uint8_t expected) noexcept
    {
        if (cursor_ == end_ || *cursor_ != expected)
            return false;
        ++cursor_;
        return true;
    }

    bool consume_literal(const char *literal) noexcept
    {
        for (const char *character = literal; *character != '\0'; ++character)
        {
            if (!consume(static_cast<std::uint8_t>(*character)))
                return false;
        }
        return true;
    }

    bool parse_value(std::size_t depth) noexcept
    {
        if (cursor_ == end_)
            return false;
        switch (*cursor_)
        {
            case '{':
                return parse_object(depth);
            case '[':
                return parse_array(depth);
            case '"':
                return parse_string();
            case 't':
                return consume_literal("true");
            case 'f':
                return consume_literal("false");
            case 'n':
                return consume_literal("null");
            default:
                return parse_number();
        }
    }

    bool parse_object(std::size_t depth) noexcept
    {
        if (depth >= maximum_depth || !consume('{'))
            return false;
        skip_whitespace();
        if (consume('}'))
            return true;
        while (true)
        {
            if (!parse_string())
                return false;
            skip_whitespace();
            if (!consume(':'))
                return false;
            skip_whitespace();
            if (!parse_value(depth + 1))
                return false;
            skip_whitespace();
            if (consume('}'))
                return true;
            if (!consume(','))
                return false;
            skip_whitespace();
        }
    }

    bool parse_array(std::size_t depth) noexcept
    {
        if (depth >= maximum_depth || !consume('['))
            return false;
        skip_whitespace();
        if (consume(']'))
            return true;
        while (true)
        {
            if (!parse_value(depth + 1))
                return false;
            skip_whitespace();
            if (consume(']'))
                return true;
            if (!consume(','))
                return false;
            skip_whitespace();
        }
    }

    bool parse_unicode_escape() noexcept
    {
        std::uint16_t code_unit = 0;
        for (std::size_t index = 0; index < 4; ++index)
        {
            if (cursor_ == end_ || !is_hexadecimal(*cursor_))
                return false;
            code_unit = static_cast<std::uint16_t>(
                code_unit * 16 + hexadecimal_value(*cursor_++));
        }

        if (code_unit >= 0xdc00 && code_unit <= 0xdfff)
            return false;
        if (code_unit < 0xd800 || code_unit > 0xdbff)
            return true;
        if (!consume('\\') || !consume('u'))
            return false;

        std::uint16_t low_surrogate = 0;
        for (std::size_t index = 0; index < 4; ++index)
        {
            if (cursor_ == end_ || !is_hexadecimal(*cursor_))
                return false;
            low_surrogate = static_cast<std::uint16_t>(
                low_surrogate * 16 + hexadecimal_value(*cursor_++));
        }
        return low_surrogate >= 0xdc00 && low_surrogate <= 0xdfff;
    }

    bool parse_utf8() noexcept
    {
        const auto lead = *cursor_++;
        std::size_t continuation_bytes = 0;
        std::uint8_t second_minimum = 0x80;
        std::uint8_t second_maximum = 0xbf;
        if (lead >= 0xc2 && lead <= 0xdf)
        {
            continuation_bytes = 1;
        }
        else if (lead >= 0xe0 && lead <= 0xef)
        {
            continuation_bytes = 2;
            if (lead == 0xe0)
                second_minimum = 0xa0;
            else if (lead == 0xed)
                second_maximum = 0x9f;
        }
        else if (lead >= 0xf0 && lead <= 0xf4)
        {
            continuation_bytes = 3;
            if (lead == 0xf0)
                second_minimum = 0x90;
            else if (lead == 0xf4)
                second_maximum = 0x8f;
        }
        else
        {
            return false;
        }

        for (std::size_t index = 0; index < continuation_bytes; ++index)
        {
            if (cursor_ == end_)
                return false;
            const auto continuation = *cursor_++;
            if (index == 0)
            {
                if (continuation < second_minimum ||
                    continuation > second_maximum)
                    return false;
            }
            else if (continuation < 0x80 || continuation > 0xbf)
            {
                return false;
            }
        }
        return true;
    }

    bool parse_string() noexcept
    {
        if (!consume('"'))
            return false;
        while (cursor_ != end_)
        {
            const auto value = *cursor_;
            if (value == '"')
            {
                ++cursor_;
                return true;
            }
            if (value == '\\')
            {
                ++cursor_;
                if (cursor_ == end_)
                    return false;
                const auto escape = *cursor_++;
                if (escape == 'u')
                {
                    if (!parse_unicode_escape())
                        return false;
                }
                else if (escape != '"' && escape != '\\' && escape != '/' &&
                         escape != 'b' && escape != 'f' && escape != 'n' &&
                         escape != 'r' && escape != 't')
                {
                    return false;
                }
                continue;
            }
            if (value < 0x20)
                return false;
            if (value < 0x80)
            {
                ++cursor_;
                continue;
            }
            if (!parse_utf8())
                return false;
        }
        return false;
    }

    bool parse_digits() noexcept
    {
        const auto *const begin = cursor_;
        while (cursor_ != end_ && *cursor_ >= '0' && *cursor_ <= '9')
            ++cursor_;
        return cursor_ != begin;
    }

    bool parse_number() noexcept
    {
        consume('-');
        if (cursor_ == end_)
            return false;
        if (*cursor_ == '0')
        {
            ++cursor_;
            if (cursor_ != end_ && *cursor_ >= '0' && *cursor_ <= '9')
                return false;
        }
        else if (*cursor_ >= '1' && *cursor_ <= '9')
        {
            if (!parse_digits())
                return false;
        }
        else
        {
            return false;
        }

        if (cursor_ != end_ && *cursor_ == '.')
        {
            ++cursor_;
            if (!parse_digits())
                return false;
        }
        if (cursor_ != end_ && (*cursor_ == 'e' || *cursor_ == 'E'))
        {
            ++cursor_;
            if (cursor_ != end_ && (*cursor_ == '+' || *cursor_ == '-'))
                ++cursor_;
            if (!parse_digits())
                return false;
        }
        return true;
    }

    const std::uint8_t *cursor_;
    const std::uint8_t *const end_;
};

bool valid_json_record(const std::uint8_t *begin,
                       const std::uint8_t *end) noexcept
{
    return JsonSyntaxParser(begin, end).parse();
}

} // namespace

std::string serialize_adaptive_v2_evidence_snapshot_payload(
    const AdaptiveV2EvidenceSnapshotStructuredEvent &event,
    std::size_t maximum_bytes)
{
    if (maximum_bytes == 0 ||
        !valid_evidence_snapshot_payload(event))
    {
        throw std::invalid_argument(
            "invalid adaptive-v2 evidence snapshot payload");
    }
    try
    {
        JsonLineBuilder builder(maximum_bytes);
        append_evidence_snapshot_payload(builder, event);
        return builder.finish_value();
    }
    catch (const LineLimitExceeded &)
    {
        throw std::length_error(
            "adaptive-v2 evidence snapshot payload exceeds its bound");
    }
}

std::string serialize_adaptive_v2_shape_decision_payload(
    const AdaptiveV2ShapeDecisionStructuredEvent &event,
    std::size_t maximum_bytes)
{
    StructuredEventConfig validation_config;
    validation_config.source.kind =
        StructuredEventSourceKind::adaptation_manager;
    validation_config.limits.maximum_identity_bytes = maximum_bytes;
    if (maximum_bytes == 0 ||
        !valid_shape_decision_payload(event, validation_config))
    {
        throw std::invalid_argument(
            "invalid adaptive-v2 shape decision payload");
    }
    try
    {
        JsonLineBuilder builder(maximum_bytes);
        append_shape_decision_payload(builder, event);
        return builder.finish_value();
    }
    catch (const LineLimitExceeded &)
    {
        throw std::length_error(
            "adaptive-v2 shape decision payload exceeds its bound");
    }
}

StructuredEventType structured_event_type(
    const StructuredEventPayload &payload) noexcept
{
    StructuredEventType type{};
    if (payload_type(payload, type))
        return type;
    return static_cast<StructuredEventType>(0);
}

StructuredEventType structured_event_type(
    const AuditStructuredEventPayload &payload) noexcept
{
    StructuredEventType type{};
    if (audit_payload_type(payload, type))
        return type;
    return static_cast<StructuredEventType>(0);
}

const char *structured_event_type_name(StructuredEventType type) noexcept
{
    if (const auto *const adaptive_name =
            adaptive_event_type_name(type))
    {
        return adaptive_name;
    }

    switch (type)
    {
        case StructuredEventType::process_started:
            return "process.started";
        case StructuredEventType::process_ready:
            return "process.ready";
        case StructuredEventType::process_stopping:
            return "process.stopping";
        case StructuredEventType::process_stopped:
            return "process.stopped";
        case StructuredEventType::process_forced_crash_requested:
            return "process.forced_crash_requested";
        case StructuredEventType::process_exited:
            return "process.exited";
        case StructuredEventType::epoch_generated:
            return "epoch.generated";
        case StructuredEventType::epoch_staged:
            return "epoch.staged";
        case StructuredEventType::epoch_acknowledged:
            return "epoch.acknowledged";
        case StructuredEventType::epoch_activation_armed:
            return "epoch.activation_armed";
        case StructuredEventType::epoch_activated:
            return "epoch.activated";
        case StructuredEventType::block_committed:
            return "block.committed";
        case StructuredEventType::block_commit_observed:
            return "block.commit_observed";
        case StructuredEventType::block_commit_identity_unavailable:
            return "block.commit_identity_unavailable";
        case StructuredEventType::block_commit_identity_witness:
            return "block.commit_identity_witness";
        case StructuredEventType::epoch_command_committed:
            return "epoch.command_committed";
        case StructuredEventType::reputation_evidence_applied:
            return "reputation.evidence_applied";
        case StructuredEventType::adaptive_v2_delivery_attempt:
            return "adaptive_v2_delivery_attempt";
        case StructuredEventType::adaptive_v2_commit_observed:
            return "adaptive_v2_commit_observed";
        case StructuredEventType::adaptive_v2_activation_observed:
            return "adaptive_v2_activation_observed";
        case StructuredEventType::adaptive_v2_converged:
            return "adaptive_v2_converged";
        case StructuredEventType::adaptive_v2_ready:
            return "adaptive_v2_ready";
        case StructuredEventType::adaptive_v2_convergence_failure:
            return "adaptive_v2_convergence_failure";
        case StructuredEventType::adaptive_v2_evidence_snapshot:
            return "adaptive_v2_evidence_snapshot";
        case StructuredEventType::adaptive_v2_session_terminal:
            return "adaptive_v2_session_terminal";
        case StructuredEventType::evidence_observation_accepted:
            return "evidence.observation_accepted";
        case StructuredEventType::adaptive_v2_shape_decision:
            return "adaptive_v2_shape_decision";
        case StructuredEventType::fault_contribution_opportunity:
            return "fault.contribution_opportunity";
        case StructuredEventType::pipeline_root_qc_queue_blocked:
            return "pipeline.root_qc_queue_blocked";
        case StructuredEventType::
            adaptive_v2_fault_containment_coverage_ready:
            return "adaptive_v2.fault_containment_coverage_ready";
        case StructuredEventType::
            adaptive_v2_cross_commit_retention_ready:
            return "adaptive_v2.cross_commit_retention_ready";
        case StructuredEventType::fault_window_armed:
            return "fault_window_armed";
        case StructuredEventType::adaptive_v3_activation_prepared:
            return "epoch.activation_prepared";
        case StructuredEventType::adaptive_v3_activation_ready_signed:
            return "epoch.activation_ready_signed";
        case StructuredEventType::adaptive_v3_observation_accepted:
            return "adaptive_v3.readiness_observation_accepted";
        case StructuredEventType::adaptive_v3_observation_rejected:
            return "adaptive_v3.readiness_observation_rejected";
        case StructuredEventType::adaptive_v3_source_quarantined:
            return "adaptive_v3.readiness_source_quarantined";
        case StructuredEventType::adaptive_v3_certificate_assembled:
            return "adaptive_v3.readiness_certificate_assembled";
        case StructuredEventType::adaptive_v3_certificate_delivery:
            return "adaptive_v3.readiness_certificate_delivery";
        case StructuredEventType::adaptive_v3_certificate_accepted:
            return "adaptive_v3.readiness_certificate_accepted";
        case StructuredEventType::adaptive_v3_certificate_rejected:
            return "adaptive_v3.readiness_certificate_rejected";
        case StructuredEventType::adaptive_v3_certificate_acknowledged:
            return "adaptive_v3.readiness_certificate_acknowledged";
        case StructuredEventType::adaptive_v3_e2_eligibility:
            return "adaptive_v3.e2_eligibility";
        case StructuredEventType::adaptive_v3_terminal:
            return "adaptive_v3.readiness_terminal";
        case StructuredEventType::adaptive_v3_wire_rejected:
            return "adaptive_v3.readiness_wire_rejected";
        case StructuredEventType::adaptive_v3_command_terminal:
            return "adaptive_v3.command_terminal";
        case StructuredEventType::adaptive_v3_observation_retry_exhausted:
            return "adaptive_v3.readiness_observation_retry_exhausted";
        default:
            break;
    }
    return "unknown";
}

std::uint64_t MonotonicRawStructuredEventClock::now_ns() noexcept
{
    if (!healthy_)
        return 0;

    struct timespec timestamp{};
    if (::clock_gettime(CLOCK_MONOTONIC_RAW, &timestamp) != 0 ||
        timestamp.tv_sec < 0 || timestamp.tv_nsec < 0 ||
        timestamp.tv_nsec >= 1'000'000'000)
    {
        healthy_ = false;
        return 0;
    }

    constexpr std::uint64_t nanoseconds_per_second = 1'000'000'000;
    const auto seconds = static_cast<std::uint64_t>(timestamp.tv_sec);
    const auto nanoseconds = static_cast<std::uint64_t>(timestamp.tv_nsec);
    if (seconds >
        (std::numeric_limits<std::uint64_t>::max() - nanoseconds) /
            nanoseconds_per_second)
    {
        healthy_ = false;
        return 0;
    }
    const auto result = seconds * nanoseconds_per_second + nanoseconds;
    if (result == 0)
        healthy_ = false;
    return healthy_ ? result : 0;
}

bool MonotonicRawStructuredEventClock::healthy() const noexcept
{
    return healthy_;
}

ExclusiveFileStructuredEventOutput::ExclusiveFileStructuredEventOutput(
    const std::string &path)
{
    if (path.empty() || path.find('\0') != std::string::npos)
        throw std::invalid_argument("structured-event output path is invalid");

    int flags = O_WRONLY | O_CREAT | O_EXCL;
#ifdef O_CLOEXEC
    flags |= O_CLOEXEC;
#endif
#ifdef O_NOFOLLOW
    flags |= O_NOFOLLOW;
#endif
    descriptor_ = ::open(path.c_str(), flags, S_IRUSR | S_IWUSR);
    if (descriptor_ < 0)
        throw std::system_error(
            errno,
            std::generic_category(),
            "cannot exclusively create structured-event output");

    if (::fchmod(descriptor_, S_IRUSR | S_IWUSR) != 0)
    {
        const auto error = errno;
        ::close(descriptor_);
        descriptor_ = -1;
        ::unlink(path.c_str());
        throw std::system_error(
            error,
            std::generic_category(),
            "cannot set structured-event output permissions");
    }

#ifndef O_CLOEXEC
    if (::fcntl(descriptor_, F_SETFD, FD_CLOEXEC) != 0)
    {
        const auto error = errno;
        ::close(descriptor_);
        descriptor_ = -1;
        ::unlink(path.c_str());
        throw std::system_error(
            error,
            std::generic_category(),
            "cannot protect structured-event output from exec inheritance");
    }
#endif
}

ExclusiveFileStructuredEventOutput::~ExclusiveFileStructuredEventOutput()
    noexcept
{
    if (!closed_)
    {
        if (healthy_)
            sync();
        close();
    }
}

StructuredEventWriteResult
ExclusiveFileStructuredEventOutput::write_some(
    const std::uint8_t *data,
    std::size_t size) noexcept
{
    if (!healthy_ || closed_ || descriptor_ < 0 || data == nullptr ||
        size == 0)
        return {StructuredEventWriteStatus::failure, 0};

    const auto maximum = static_cast<std::size_t>(
        std::numeric_limits<ssize_t>::max());
    const auto requested = std::min(size, maximum);
    const auto written = ::write(descriptor_, data, requested);
    if (written > 0)
    {
        return {
            StructuredEventWriteStatus::progress,
            static_cast<std::size_t>(written)};
    }
    if (written < 0 && errno == EINTR)
        return {StructuredEventWriteStatus::interrupted, 0};

    healthy_ = false;
    return {StructuredEventWriteStatus::failure, 0};
}

bool ExclusiveFileStructuredEventOutput::sync() noexcept
{
    if (!healthy_ || closed_ || descriptor_ < 0)
        return false;
    while (::fsync(descriptor_) != 0)
    {
        if (errno == EINTR)
            continue;
        healthy_ = false;
        return false;
    }
    return true;
}

bool ExclusiveFileStructuredEventOutput::close() noexcept
{
    if (closed_)
        return close_result_;
    closed_ = true;

    const auto descriptor = descriptor_;
    descriptor_ = -1;
    if (descriptor < 0 || ::close(descriptor) != 0)
        healthy_ = false;
    close_result_ = healthy_;
    return close_result_;
}

bool ExclusiveFileStructuredEventOutput::is_open() const noexcept
{
    return !closed_ && descriptor_ >= 0;
}

bool ExclusiveFileStructuredEventOutput::healthy() const noexcept
{
    return healthy_;
}

struct StructuredEventSink::State final
{
    State(StructuredEventConfig value,
          StructuredEventClock &event_clock,
          StructuredEventOutput &event_output,
          StructuredEventCursor cursor)
        : config(std::move(value)),
          clock(event_clock),
          output(event_output)
    {
        if (!valid_configuration(config))
            fail(StructuredEventFailure::invalid_configuration);
        else if (!valid_identity_limits(config))
            fail(StructuredEventFailure::identity_too_large);
        else if (!valid_cursor(cursor, config))
            fail(StructuredEventFailure::invalid_configuration);
        else
        {
            status.last_assigned_sequence = cursor.last_source_sequence;
            status.has_last_monotonic_ns = cursor.has_last_monotonic_ns;
            status.last_monotonic_ns = cursor.last_monotonic_ns;
        }
    }

    void fail(StructuredEventFailure failure) noexcept
    {
        if (status.first_failure == StructuredEventFailure::none)
            status.first_failure = failure;
        status.healthy = false;
        status.stopped = true;
    }

    void fail_admission(StructuredEventFailure failure) noexcept
    {
        if (status.dropped_records !=
            std::numeric_limits<std::uint64_t>::max())
            ++status.dropped_records;
        fail(failure);
    }

    void fail_reentrant() noexcept
    {
        reentrant_call_detected = true;
        fail(StructuredEventFailure::reentrant_call);
    }

    void discard_queue() noexcept
    {
        queue.clear();
        write_offset = 0;
        status.queued_events = 0;
        status.queued_bytes = 0;
    }

    template <typename Serializer>
    void admit(bool valid_payload, Serializer &&serializer) noexcept
    {
        if (active_call)
        {
            fail_reentrant();
            return;
        }
        if (!status.healthy || closed)
            return;
        ReentrancyScope scope(active_call);

        if (!valid_payload)
        {
            fail_admission(StructuredEventFailure::invalid_payload);
            return;
        }
        if (status.last_assigned_sequence ==
            std::numeric_limits<std::uint64_t>::max())
        {
            fail_admission(StructuredEventFailure::sequence_exhausted);
            return;
        }

        const auto sequence = status.last_assigned_sequence + 1;
        const auto monotonic_ns = clock.now_ns();
        if (!status.healthy)
        {
            output_failed = true;
            discard_queue();
            return;
        }
        if (!clock.healthy() || monotonic_ns == 0)
        {
            fail_admission(StructuredEventFailure::clock_failure);
            return;
        }
        if (status.has_last_monotonic_ns &&
            monotonic_ns < status.last_monotonic_ns)
        {
            fail_admission(StructuredEventFailure::clock_regression);
            return;
        }

        try
        {
            auto record = serializer(sequence, monotonic_ns);
            if (record.size() > config.limits.maximum_line_bytes)
            {
                fail_admission(StructuredEventFailure::line_too_large);
                return;
            }
            if (status.queued_events >=
                    config.limits.maximum_queued_events ||
                record.size() >
                    config.limits.maximum_queued_bytes -
                        std::min(config.limits.maximum_queued_bytes,
                                 status.queued_bytes))
            {
                fail_admission(StructuredEventFailure::queue_full);
                return;
            }

            const auto record_size = record.size();
            queue.push_back(std::move(record));
            ++status.queued_events;
            status.queued_bytes += record_size;
            status.last_assigned_sequence = sequence;
            status.has_last_monotonic_ns = true;
            status.last_monotonic_ns = monotonic_ns;
        }
        catch (const LineLimitExceeded &)
        {
            fail_admission(StructuredEventFailure::line_too_large);
        }
        catch (...)
        {
            fail_admission(StructuredEventFailure::allocation_failure);
        }
    }

    StructuredEventConfig config;
    StructuredEventClock &clock;
    StructuredEventOutput &output;
    StructuredEventHealth status;
    std::deque<std::string> queue;
    std::size_t write_offset{0};
    bool active_call{false};
    bool reentrant_call_detected{false};
    bool output_failed{false};
    bool closed{false};
};

StructuredEventSink::StructuredEventSink(
    StructuredEventConfig config,
    StructuredEventClock &clock,
    StructuredEventOutput &output,
    StructuredEventCursor cursor)
    : state_(std::make_unique<State>(
          std::move(config), clock, output, cursor))
{
}

StructuredEventSink::~StructuredEventSink() noexcept
{
    shutdown();
}

void StructuredEventSink::emit(
    const StructuredEventPayload &payload) noexcept
{
    auto &state = *state_;
    StructuredEventType type{};
    const auto valid = payload_type(payload, type);
    state.admit(valid, [&state, &payload, type](
                           std::uint64_t sequence,
                           std::uint64_t monotonic_ns) {
        return serialize_event(
            state.config, payload, type, sequence, monotonic_ns);
    });
}

bool StructuredEventSink::is_designated_commit_observer() const noexcept
{
    const auto &config = state_->config;
    return config.designated_commit_observer &&
           same_source(config.source, *config.designated_commit_observer);
}

void StructuredEventSink::emit_adaptive(
    const AdaptiveAggregationStructuredEvent &event) noexcept
{
    auto &state = *state_;
    StructuredEventType type{};
    const auto valid = valid_adaptive_payload(event, type);
    state.admit(valid, [&state, &event, type](
                           std::uint64_t sequence,
                           std::uint64_t monotonic_ns) {
        return serialize_adaptive_event(
            state.config, event, type, sequence, monotonic_ns);
    });
}

void StructuredEventSink::emit_audit(
    const AuditStructuredEventPayload &event) noexcept
{
    auto &state = *state_;
    StructuredEventType type{};
    const auto valid =
        valid_audit_payload(event, state.config) &&
        audit_payload_type(event, type);
    state.admit(valid, [&state, &event, type](
                           std::uint64_t sequence,
                           std::uint64_t monotonic_ns) {
        return serialize_audit_event(
            state.config, event, type, sequence, monotonic_ns);
    });
}

void StructuredEventSink::drain() noexcept
{
    auto &state = *state_;
    if (state.active_call)
    {
        state.fail_reentrant();
        return;
    }
    if (state.closed || state.output_failed)
        return;
    ReentrancyScope scope(state.active_call);

    while (!state.queue.empty())
    {
        const auto &record = state.queue.front();
        if (state.write_offset > record.size())
        {
            state.output_failed = true;
            state.fail(StructuredEventFailure::write_failure);
            state.discard_queue();
            return;
        }
        const auto remaining = record.size() - state.write_offset;
        if (remaining == 0)
        {
            state.queue.pop_front();
            state.write_offset = 0;
            if (state.status.queued_events != 0)
                --state.status.queued_events;
            if (state.status.complete_records !=
                std::numeric_limits<std::uint64_t>::max())
                ++state.status.complete_records;
            continue;
        }

        const auto *const bytes = reinterpret_cast<const std::uint8_t *>(
            record.data() + state.write_offset);
        const auto result = state.output.write_some(bytes, remaining);
        if (state.reentrant_call_detected)
        {
            state.status.interrupted_tail =
                state.write_offset != 0 || result.bytes_written != 0;
            state.output_failed = true;
            state.discard_queue();
            return;
        }
        if (result.status == StructuredEventWriteStatus::interrupted &&
            result.bytes_written == 0)
            continue;
        if (result.status != StructuredEventWriteStatus::progress ||
            result.bytes_written == 0 ||
            result.bytes_written > remaining)
        {
            state.status.interrupted_tail = state.write_offset != 0;
            state.output_failed = true;
            state.fail(StructuredEventFailure::write_failure);
            state.discard_queue();
            return;
        }

        state.write_offset += result.bytes_written;
        state.status.queued_bytes -= result.bytes_written;
        if (state.write_offset == record.size())
        {
            state.queue.pop_front();
            state.write_offset = 0;
            --state.status.queued_events;
            if (state.status.complete_records !=
                std::numeric_limits<std::uint64_t>::max())
                ++state.status.complete_records;
        }
    }
}

void StructuredEventSink::shutdown() noexcept
{
    auto &state = *state_;
    if (state.active_call)
    {
        state.fail_reentrant();
        return;
    }
    if (state.closed)
        return;
    drain();
    state.closed = true;
    state.status.stopped = true;
    ReentrancyScope scope(state.active_call);
    const bool synced_cleanly =
        state.output_failed || state.status.complete_records == 0
        ? true
        : state.output.sync();
    const bool closed_cleanly = state.output.close();
    if (!synced_cleanly)
        state.fail(StructuredEventFailure::sync_failure);
    else if (!closed_cleanly)
        state.fail(StructuredEventFailure::close_failure);
}

StructuredEventHealth StructuredEventSink::health() const noexcept
{
    auto &state = *state_;
    if (state.active_call)
        state.fail_reentrant();
    return state.status;
}

StructuredEventPrefixResult parse_structured_event_prefix(
    const bytearray_t &bytes) noexcept
{
    StructuredEventPrefixResult result;
    std::size_t record_begin = 0;
    try
    {
        for (std::size_t index = 0; index < bytes.size(); ++index)
        {
            if (bytes[index] != static_cast<std::uint8_t>('\n'))
                continue;
            if (!valid_json_record(
                    bytes.data() + record_begin, bytes.data() + index))
            {
                result.status = StructuredEventPrefixStatus::malformed_record;
                return result;
            }
            ++result.complete_records;
            result.complete_bytes = index + 1;
            record_begin = index + 1;
        }
        if (record_begin != bytes.size())
            result.status = StructuredEventPrefixStatus::interrupted_tail;
    }
    catch (...)
    {
        result.status = StructuredEventPrefixStatus::allocation_failure;
    }
    return result;
}

} // namespace hotstuff
