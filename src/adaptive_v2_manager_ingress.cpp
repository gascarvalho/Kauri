#include "hotstuff/adaptive_v2_manager_ingress.h"

#include <algorithm>
#include <limits>
#include <map>
#include <set>
#include <stdexcept>
#include <utility>

namespace hotstuff
{
namespace
{

struct ValidatedBootstrap
{
    std::vector<ReplicaID> membership;
    ByzantineQuorum quorum;
    AdaptiveV2ManagerIngressLimits limits;
};

enum class CorroboratedLifecycleFactKind : std::uint8_t
{
    normal_runtime_initialized = 1,
    committed = 2,
};

struct CorroboratedLifecycleFact
{
    CorroboratedLifecycleFactKind kind;
    ProposalKey proposal;

    bool operator<(
        const CorroboratedLifecycleFact &other) const noexcept
    {
        if (kind != other.kind)
            return kind < other.kind;
        return proposal < other.proposal;
    }
};

bool valid_limits(
    const AdaptiveV2ManagerIngressLimits &limits,
    std::size_t member_count) noexcept
{
    return limits.maximum_members != 0 &&
           limits.maximum_members <= kMaximumAdaptiveV2ManagerMembers &&
           member_count <= limits.maximum_members &&
           limits.readiness_wire.maximum_payload_bytes != 0 &&
           limits.lifecycle_wire.maximum_payload_bytes != 0 &&
           limits.evidence_wire.maximum_payload_bytes != 0 &&
           limits.evidence_wire.maximum_observations != 0 &&
           limits.evidence_wire.maximum_signers_per_observation != 0 &&
           limits.proposal_index.maximum_exact_proposals != 0 &&
           limits.proposal_index.maximum_retired_configurations != 0 &&
           limits.evidence_store.maximum_accepted_records != 0 &&
           limits.evidence_store.maximum_rejected_records != 0 &&
           limits.lifecycle.maximum_quarantined_records != 0 &&
           limits.lifecycle.maximum_quarantined_bytes != 0 &&
           limits.lifecycle.maximum_reporter_queues >= member_count &&
           limits.lifecycle.maximum_signer_entries != 0 &&
           limits.lifecycle.maximum_deduplication_entries != 0 &&
           limits.lifecycle.maximum_lifecycle_sources >= member_count &&
           limits.maximum_pending_lifecycle_facts_per_source != 0 &&
           limits.maximum_pending_lifecycle_facts_per_source <=
               kMaximumAdaptiveV2PendingLifecycleFactsPerSource &&
           limits.lifecycle_accounting.maximum_records >=
               limits.lifecycle.maximum_quarantined_records &&
           limits.lifecycle_accounting.maximum_canonical_bytes >=
               limits.lifecycle.maximum_quarantined_bytes &&
           limits.lifecycle_accounting.maximum_signer_entries >=
               limits.lifecycle.maximum_signer_entries;
}

ValidatedBootstrap validate_bootstrap(
    std::vector<ReplicaID> membership,
    const EpochDefinitionInput &epoch_zero,
    std::uint64_t activation_generation,
    AdaptiveV2ManagerIngressLimits limits)
{
    std::sort(membership.begin(), membership.end());
    if (membership.empty() ||
        std::adjacent_find(membership.begin(), membership.end()) !=
            membership.end())
    {
        throw std::invalid_argument(
            "adaptive-v2 manager membership must be nonempty and unique");
    }
    const auto quorum = derive_byzantine_quorum(membership.size());
    if (!quorum.has_value() || quorum->fault_threshold == 0)
    {
        throw std::invalid_argument(
            "adaptive-v2 manager requires exact N=3f+1 with f>0");
    }
    if (epoch_zero.schema_version !=
            kEpochDefinitionSchemaVersionV2 ||
        epoch_zero.epoch_number != 0 ||
        activation_generation == 0)
    {
        throw std::invalid_argument(
            "adaptive-v2 manager requires schema-v2 epoch zero and generation");
    }
    if (!valid_limits(limits, membership.size()))
    {
        throw std::invalid_argument(
            "adaptive-v2 manager ingress limits are invalid");
    }
    return {std::move(membership), *quorum, std::move(limits)};
}

bool increment(std::uint64_t &value) noexcept
{
    if (value == std::numeric_limits<std::uint64_t>::max())
        return false;
    ++value;
    return true;
}

} // namespace

struct AdaptiveV2ManagerIngress::State
{
    struct ReadinessEntry
    {
        std::uint64_t source_sequence{0};
        std::uint64_t committed_height{0};
        bool ready{false};
    };

    struct LifecycleSourceEntry
    {
        std::uint64_t source_sequence{0};
        std::size_t pending_associations{0};
    };

    State(ValidatedBootstrap bootstrap,
          EpochDefinitionInput epoch_zero,
          std::uint32_t active_tree_id,
          std::uint64_t activation_generation_)
        : membership(std::move(bootstrap.membership)),
          quorum(bootstrap.quorum),
          limits(std::move(bootstrap.limits)),
          epochs(membership),
          proposal_index(limits.proposal_index),
          ledger(epochs, proposal_index, limits.evidence_store),
          coordinator(
              proposal_index,
              ledger,
              EvidenceLifecycleAccounting{
                  limits.lifecycle_accounting},
              limits.lifecycle),
          activation_generation(activation_generation_)
    {
        current_epoch = &epochs.stage(
            epoch_zero, EpochValidationContext{});
        if (epochs.find_tree(0, active_tree_id) == nullptr)
        {
            throw std::invalid_argument(
                "adaptive-v2 manager active tree is not in epoch zero");
        }
        current_configuration = ConfigurationId{
            0, active_tree_id, current_epoch->epoch_digest()};
        for (const auto member : membership)
        {
            readiness.emplace(member, ReadinessEntry{});
            lifecycle_sources.emplace(member, LifecycleSourceEntry{});
        }
        if (!proposal_index.healthy() || !ledger.healthy() ||
            !coordinator.healthy())
        {
            throw std::invalid_argument(
                "adaptive-v2 manager owned evidence state is unhealthy");
        }
    }

    bool is_member(ReplicaID replica_id) const noexcept
    {
        return std::binary_search(
            membership.begin(), membership.end(), replica_id);
    }

    bool operational() const noexcept
    {
        return locally_healthy && !stopped &&
               proposal_index.healthy() && ledger.healthy() &&
               coordinator.healthy();
    }

    void fail_closed() noexcept
    {
        locally_healthy = false;
        coordinator.shutdown();
        proposal_index.shutdown();
    }

    bool record(std::uint64_t &counter) noexcept
    {
        if (increment(counter))
            return true;
        if (increment(audit.capacity_failures))
            fail_closed();
        else
            fail_closed();
        return false;
    }

    bool record_readiness_rejection(
        std::uint64_t &audit_counter) noexcept
    {
        if (readiness_rejected ==
                std::numeric_limits<std::uint64_t>::max() ||
            audit_counter == std::numeric_limits<std::uint64_t>::max())
        {
            record(audit.capacity_failures);
            fail_closed();
            return false;
        }
        ++readiness_rejected;
        ++audit_counter;
        return true;
    }

    AdaptiveV2ManagerReadinessStats readiness_snapshot() const noexcept
    {
        return {
            membership.size(),
            ready_members,
            readiness_accepted,
            readiness_rejected,
            ready_members == membership.size()};
    }

    AdaptiveV2ManagerIngressAuditStats audit_snapshot() const noexcept
    {
        auto snapshot = audit;
        snapshot.lifecycle_corroboration_threshold =
            static_cast<std::size_t>(quorum.fault_threshold) + 1;
        snapshot.pending_lifecycle_facts = pending_lifecycle_votes.size();
        snapshot.pending_lifecycle_associations =
            pending_lifecycle_associations;
        return snapshot;
    }

    bool erase_pending_lifecycle_fact(
        const CorroboratedLifecycleFact &fact) noexcept
    {
        const auto pending = pending_lifecycle_votes.find(fact);
        if (pending == pending_lifecycle_votes.end())
            return true;

        for (const auto source : pending->second)
        {
            const auto entry = lifecycle_sources.find(source);
            if (entry == lifecycle_sources.end() ||
                entry->second.pending_associations == 0 ||
                pending_lifecycle_associations == 0)
            {
                fail_closed();
                return false;
            }
            --entry->second.pending_associations;
            --pending_lifecycle_associations;
        }
        pending_lifecycle_votes.erase(pending);
        return true;
    }

    std::vector<ReplicaID> membership;
    ByzantineQuorum quorum;
    AdaptiveV2ManagerIngressLimits limits;
    EpochStore epochs;
    ProposalEvidenceIndex proposal_index;
    EvidenceLedger ledger;
    ProposalLifecycleEvidenceCoordinator coordinator;
    const EpochDefinition *current_epoch{nullptr};
    ConfigurationId current_configuration;
    std::uint64_t activation_generation{0};
    std::map<ReplicaID, ReadinessEntry> readiness;
    std::map<ReplicaID, LifecycleSourceEntry> lifecycle_sources;
    std::map<CorroboratedLifecycleFact, std::set<ReplicaID>>
        pending_lifecycle_votes;
    std::size_t pending_lifecycle_associations{0};
    std::size_t ready_members{0};
    std::uint64_t readiness_accepted{0};
    std::uint64_t readiness_rejected{0};
    AdaptiveV2ManagerIngressAuditStats audit;
    bool locally_healthy{true};
    bool stopped{false};
};

AdaptiveV2ManagerIngress::AdaptiveV2ManagerIngress(
    std::vector<ReplicaID> membership,
    EpochDefinitionInput epoch_zero,
    std::uint32_t active_tree_id,
    std::uint64_t activation_generation,
    AdaptiveV2ManagerIngressLimits limits)
    : state_(std::make_unique<State>(
          validate_bootstrap(
              std::move(membership),
              epoch_zero,
              activation_generation,
              std::move(limits)),
          std::move(epoch_zero),
          active_tree_id,
          activation_generation))
{}

AdaptiveV2ManagerIngress::~AdaptiveV2ManagerIngress() = default;

AdaptiveV2ManagerReadinessResult
AdaptiveV2ManagerIngress::ingest_readiness(
    const AuthenticatedReporter &authenticated_source,
    const MsgAdaptiveV2ReadinessNotice &message) noexcept
{
    auto &state = *state_;
    if (state.stopped)
    {
        return {AdaptiveV2ManagerIngressStatus::stopped,
                std::nullopt,
                state.readiness_snapshot()};
    }
    if (!state.operational())
    {
        state.fail_closed();
        return {AdaptiveV2ManagerIngressStatus::evidence_unhealthy,
                std::nullopt,
                state.readiness_snapshot()};
    }
    if (!state.is_member(authenticated_source.replica_id))
    {
        if (!state.record_readiness_rejection(
                state.audit.nonmember_rejections))
        {
            return {AdaptiveV2ManagerIngressStatus::evidence_unhealthy,
                    std::nullopt,
                    state.readiness_snapshot()};
        }
        return {AdaptiveV2ManagerIngressStatus::rejected_nonmember,
                std::nullopt,
                state.readiness_snapshot()};
    }
    if (message.serialized.size() >
        state.limits.readiness_wire.maximum_payload_bytes)
    {
        if (!state.record_readiness_rejection(
                state.audit.readiness_wire_rejections))
        {
            return {AdaptiveV2ManagerIngressStatus::evidence_unhealthy,
                    AdaptiveV2ReadinessWireError::payload_too_large,
                    state.readiness_snapshot()};
        }
        return {AdaptiveV2ManagerIngressStatus::rejected_wire,
                AdaptiveV2ReadinessWireError::payload_too_large,
                state.readiness_snapshot()};
    }
    try
    {
        return ingest_readiness(
            authenticated_source,
            static_cast<bytearray_t>(message.serialized));
    }
    catch (...)
    {
        state.fail_closed();
        return {AdaptiveV2ManagerIngressStatus::evidence_unhealthy,
                AdaptiveV2ReadinessWireError::allocation_failure,
                state.readiness_snapshot()};
    }
}

AdaptiveV2ManagerReadinessResult
AdaptiveV2ManagerIngress::ingest_readiness(
    const AuthenticatedReporter &authenticated_source,
    const bytearray_t &canonical_payload) noexcept
{
    auto &state = *state_;
    const auto rejected = [&](AdaptiveV2ManagerIngressStatus status,
                              std::uint64_t &counter,
                              std::optional<AdaptiveV2ReadinessWireError>
                                  wire_error = std::nullopt) {
        if (!state.record_readiness_rejection(counter))
            status = AdaptiveV2ManagerIngressStatus::evidence_unhealthy;
        return AdaptiveV2ManagerReadinessResult{
            status, wire_error, state.readiness_snapshot()};
    };

    if (state.stopped)
    {
        return {AdaptiveV2ManagerIngressStatus::stopped,
                std::nullopt,
                state.readiness_snapshot()};
    }
    if (!state.operational())
    {
        state.fail_closed();
        return {AdaptiveV2ManagerIngressStatus::evidence_unhealthy,
                std::nullopt,
                state.readiness_snapshot()};
    }
    if (!state.is_member(authenticated_source.replica_id))
    {
        return rejected(
            AdaptiveV2ManagerIngressStatus::rejected_nonmember,
            state.audit.nonmember_rejections);
    }

    const auto decoded = decode_adaptive_v2_readiness_notice(
        canonical_payload, state.limits.readiness_wire);
    if (!decoded)
    {
        if (decoded.error ==
                AdaptiveV2ReadinessWireError::allocation_failure ||
            decoded.error ==
                AdaptiveV2ReadinessWireError::internal_failure)
        {
            state.record(state.audit.readiness_wire_rejections);
            state.fail_closed();
            return {AdaptiveV2ManagerIngressStatus::evidence_unhealthy,
                    decoded.error,
                    state.readiness_snapshot()};
        }
        return rejected(
            AdaptiveV2ManagerIngressStatus::rejected_wire,
            state.audit.readiness_wire_rejections,
            decoded.error);
    }

    const auto &notice = *decoded.notice;
    if (!state.is_member(notice.claimed_source_replica_id))
    {
        return rejected(
            AdaptiveV2ManagerIngressStatus::rejected_nonmember,
            state.audit.nonmember_rejections);
    }
    if (authenticated_source.replica_id !=
        notice.claimed_source_replica_id)
    {
        return rejected(
            AdaptiveV2ManagerIngressStatus::rejected_spoofed_source,
            state.audit.spoofed_source_rejections);
    }
    if (notice.active_configuration != state.current_configuration)
    {
        return rejected(
            AdaptiveV2ManagerIngressStatus::rejected_configuration,
            state.audit.state_rejections);
    }
    if (notice.activation_generation != state.activation_generation)
    {
        return rejected(
            AdaptiveV2ManagerIngressStatus::rejected_generation,
            state.audit.state_rejections);
    }

    auto &entry = state.readiness.at(notice.claimed_source_replica_id);
    if (notice.source_sequence <= entry.source_sequence)
    {
        return rejected(
            AdaptiveV2ManagerIngressStatus::rejected_sequence,
            state.audit.state_rejections);
    }
    if (entry.ready &&
        notice.committed_height < entry.committed_height)
    {
        return rejected(
            AdaptiveV2ManagerIngressStatus::rejected_height_regression,
            state.audit.state_rejections);
    }
    if (state.readiness_accepted ==
        std::numeric_limits<std::uint64_t>::max())
    {
        state.record(state.audit.capacity_failures);
        state.fail_closed();
        return {AdaptiveV2ManagerIngressStatus::evidence_unhealthy,
                std::nullopt,
                state.readiness_snapshot()};
    }
    if (!entry.ready)
    {
        if (state.ready_members == state.membership.size())
        {
            state.record(state.audit.capacity_failures);
            state.fail_closed();
            return {AdaptiveV2ManagerIngressStatus::evidence_unhealthy,
                    std::nullopt,
                    state.readiness_snapshot()};
        }
        entry.ready = true;
        ++state.ready_members;
    }
    entry.source_sequence = notice.source_sequence;
    entry.committed_height = notice.committed_height;
    ++state.readiness_accepted;
    return {AdaptiveV2ManagerIngressStatus::processed,
            std::nullopt,
            state.readiness_snapshot()};
}

AdaptiveV2ManagerLifecycleResult
AdaptiveV2ManagerIngress::ingest_lifecycle(
    const AuthenticatedReporter &authenticated_source,
    const MsgProposalLifecycleNotice &message) noexcept
{
    auto &state = *state_;
    if (state.stopped)
        return {AdaptiveV2ManagerIngressStatus::stopped, {}, {}};
    if (!state.operational())
    {
        state.fail_closed();
        return {AdaptiveV2ManagerIngressStatus::evidence_unhealthy,
                {},
                {}};
    }
    if (!state.is_member(authenticated_source.replica_id))
    {
        if (!state.record(state.audit.nonmember_rejections))
            return {AdaptiveV2ManagerIngressStatus::evidence_unhealthy,
                    {},
                    {}};
        return {AdaptiveV2ManagerIngressStatus::rejected_nonmember,
                {},
                {}};
    }
    if (message.serialized.size() >
        state.limits.lifecycle_wire.maximum_payload_bytes)
    {
        if (!state.record(state.audit.lifecycle_wire_rejections))
            return {AdaptiveV2ManagerIngressStatus::evidence_unhealthy,
                    ProposalLifecycleWireError::payload_too_large,
                    {}};
        return {AdaptiveV2ManagerIngressStatus::rejected_wire,
                ProposalLifecycleWireError::payload_too_large,
                {}};
    }
    try
    {
        return ingest_lifecycle(
            authenticated_source,
            static_cast<bytearray_t>(message.serialized));
    }
    catch (...)
    {
        state.fail_closed();
        return {AdaptiveV2ManagerIngressStatus::evidence_unhealthy,
                ProposalLifecycleWireError::allocation_failure,
                {}};
    }
}

AdaptiveV2ManagerLifecycleResult
AdaptiveV2ManagerIngress::ingest_lifecycle(
    const AuthenticatedReporter &authenticated_source,
    const bytearray_t &canonical_payload) noexcept
{
    auto &state = *state_;
    if (state.stopped)
        return {AdaptiveV2ManagerIngressStatus::stopped, {}, {}};
    if (!state.operational())
    {
        state.fail_closed();
        return {AdaptiveV2ManagerIngressStatus::evidence_unhealthy,
                {},
                {}};
    }
    if (!state.is_member(authenticated_source.replica_id))
    {
        if (!state.record(state.audit.nonmember_rejections))
            return {AdaptiveV2ManagerIngressStatus::evidence_unhealthy,
                    {},
                    {}};
        return {AdaptiveV2ManagerIngressStatus::rejected_nonmember,
                {},
                {}};
    }

    const auto decoded = decode_proposal_lifecycle_notice(
        canonical_payload, state.limits.lifecycle_wire);
    if (!decoded)
    {
        state.record(state.audit.lifecycle_wire_rejections);
        if (decoded.error == ProposalLifecycleWireError::allocation_failure ||
            decoded.error == ProposalLifecycleWireError::internal_failure ||
            !state.operational())
        {
            state.fail_closed();
            return {AdaptiveV2ManagerIngressStatus::evidence_unhealthy,
                    decoded.error,
                    {}};
        }
        return {AdaptiveV2ManagerIngressStatus::rejected_wire,
                decoded.error,
                {}};
    }

    const auto &notice = *decoded.notice;
    if (!state.is_member(notice.source_replica_id))
    {
        if (!state.record(state.audit.nonmember_rejections))
            return {AdaptiveV2ManagerIngressStatus::evidence_unhealthy,
                    {},
                    {}};
        return {AdaptiveV2ManagerIngressStatus::rejected_nonmember,
                {},
                {}};
    }
    if (authenticated_source.replica_id != notice.source_replica_id)
    {
        if (!state.record(state.audit.spoofed_source_rejections))
            return {AdaptiveV2ManagerIngressStatus::evidence_unhealthy,
                    {},
                    {}};
        return {
            AdaptiveV2ManagerIngressStatus::rejected_spoofed_source,
            {},
            {}};
    }

    const auto source_entry = state.lifecycle_sources.find(
        authenticated_source.replica_id);
    if (source_entry == state.lifecycle_sources.end())
    {
        state.fail_closed();
        return {AdaptiveV2ManagerIngressStatus::evidence_unhealthy,
                {},
                {}};
    }
    if (notice.source_sequence <= source_entry->second.source_sequence)
    {
        if (!state.record(state.audit.state_rejections))
        {
            return {
                AdaptiveV2ManagerIngressStatus::evidence_unhealthy,
                {},
                {}};
        }
        return {AdaptiveV2ManagerIngressStatus::rejected_sequence,
                {},
                {}};
    }

    // A decoded, authenticated sequence is consumed before fact admission.
    // Replays, invalid fact classes, quota excess, and same-source duplicate
    // votes therefore cannot be corrected by reusing the same sequence.
    source_entry->second.source_sequence = notice.source_sequence;

    if (std::holds_alternative<ProposalRuntimeAborted>(notice.fact) ||
        std::holds_alternative<ProposalConfigurationRetired>(notice.fact) ||
        std::holds_alternative<ProposalRetirementFloorAdvanced>(notice.fact))
    {
        if (!state.record(state.audit.state_rejections))
        {
            return {
                AdaptiveV2ManagerIngressStatus::evidence_unhealthy,
                {},
                {}};
        }
        return {AdaptiveV2ManagerIngressStatus::rejected_lifecycle,
                {},
                {}};
    }

    CorroboratedLifecycleFact fact;
    if (std::holds_alternative<NormalProposalRuntimeInitialized>(notice.fact))
    {
        fact = CorroboratedLifecycleFact{
            CorroboratedLifecycleFactKind::normal_runtime_initialized,
            std::get<NormalProposalRuntimeInitialized>(notice.fact).proposal};
    }
    else if (std::holds_alternative<ProposalCommitted>(notice.fact))
    {
        fact = CorroboratedLifecycleFact{
            CorroboratedLifecycleFactKind::committed,
            std::get<ProposalCommitted>(notice.fact).proposal};
    }
    else
    {
        state.fail_closed();
        return {AdaptiveV2ManagerIngressStatus::evidence_unhealthy,
                {},
                {}};
    }

    const auto classification = state.proposal_index.classify(fact.proposal);
    if (fact.kind ==
        CorroboratedLifecycleFactKind::normal_runtime_initialized)
    {
        if (classification == ProposalEvidenceStatus::admissible)
        {
            if (!state.erase_pending_lifecycle_fact(fact))
            {
                return {
                    AdaptiveV2ManagerIngressStatus::evidence_unhealthy,
                    {},
                    {}};
            }
            return {AdaptiveV2ManagerIngressStatus::already_applied,
                    {},
                    {}};
        }
        if (classification == ProposalEvidenceStatus::stale)
        {
            if (!state.erase_pending_lifecycle_fact(fact) ||
                !state.record(state.audit.state_rejections))
            {
                return {
                    AdaptiveV2ManagerIngressStatus::evidence_unhealthy,
                    {},
                    {}};
            }
            return {AdaptiveV2ManagerIngressStatus::rejected_lifecycle,
                    {},
                    {}};
        }
    }
    else if (classification == ProposalEvidenceStatus::stale)
    {
        if (!state.erase_pending_lifecycle_fact(fact))
        {
            return {AdaptiveV2ManagerIngressStatus::evidence_unhealthy,
                    {},
                    {}};
        }
        return {AdaptiveV2ManagerIngressStatus::already_applied,
                {},
                {}};
    }

    auto pending = state.pending_lifecycle_votes.find(fact);
    if (pending != state.pending_lifecycle_votes.end() &&
        pending->second.count(authenticated_source.replica_id) != 0)
    {
        return {
            AdaptiveV2ManagerIngressStatus::awaiting_corroboration,
            {},
            {}};
    }

    if (source_entry->second.pending_associations >=
        state.limits.maximum_pending_lifecycle_facts_per_source)
    {
        if (!state.record(state.audit.lifecycle_quota_rejections))
        {
            return {
                AdaptiveV2ManagerIngressStatus::evidence_unhealthy,
                {},
                {}};
        }
        return {AdaptiveV2ManagerIngressStatus::rejected_capacity,
                {},
                {}};
    }

    bool inserted_fact = false;
    try
    {
        if (pending == state.pending_lifecycle_votes.end())
        {
            const auto inserted = state.pending_lifecycle_votes.emplace(
                fact, std::set<ReplicaID>{});
            pending = inserted.first;
            inserted_fact = inserted.second;
        }
        const auto inserted_source = pending->second.insert(
            authenticated_source.replica_id);
        if (!inserted_source.second)
        {
            return {
                AdaptiveV2ManagerIngressStatus::awaiting_corroboration,
                {},
                {}};
        }
        ++source_entry->second.pending_associations;
        ++state.pending_lifecycle_associations;
    }
    catch (...)
    {
        if (inserted_fact && pending != state.pending_lifecycle_votes.end() &&
            pending->second.empty())
        {
            state.pending_lifecycle_votes.erase(pending);
        }
        state.fail_closed();
        return {AdaptiveV2ManagerIngressStatus::evidence_unhealthy,
                {},
                {}};
    }

    const auto corroboration_threshold =
        static_cast<std::size_t>(state.quorum.fault_threshold) + 1;
    if (pending->second.size() < corroboration_threshold)
    {
        return {
            AdaptiveV2ManagerIngressStatus::awaiting_corroboration,
            {},
            {}};
    }

    const auto applied = state.coordinator.apply_notice(
        authenticated_source, notice);
    if (applied.status == ProposalLifecycleApplyStatus::applied)
    {
        if (!state.erase_pending_lifecycle_fact(fact))
        {
            return {AdaptiveV2ManagerIngressStatus::evidence_unhealthy,
                    {},
                    applied};
        }
        if (fact.kind == CorroboratedLifecycleFactKind::committed)
        {
            const CorroboratedLifecycleFact pending_initialization{
                CorroboratedLifecycleFactKind::normal_runtime_initialized,
                fact.proposal};
            if (!state.erase_pending_lifecycle_fact(
                    pending_initialization))
            {
                return {
                    AdaptiveV2ManagerIngressStatus::evidence_unhealthy,
                    {},
                    applied};
            }
        }
    }
    switch (applied.status)
    {
    case ProposalLifecycleApplyStatus::applied:
        return {AdaptiveV2ManagerIngressStatus::processed,
                {},
                applied};
    case ProposalLifecycleApplyStatus::rejected_sequence:
        if (!state.record(state.audit.state_rejections))
        {
            return {
                AdaptiveV2ManagerIngressStatus::evidence_unhealthy,
                {},
                applied};
        }
        return {AdaptiveV2ManagerIngressStatus::rejected_sequence,
                {},
                applied};
    case ProposalLifecycleApplyStatus::rejected_authentication:
        if (!state.record(state.audit.spoofed_source_rejections))
        {
            return {
                AdaptiveV2ManagerIngressStatus::evidence_unhealthy,
                {},
                applied};
        }
        return {
            AdaptiveV2ManagerIngressStatus::rejected_spoofed_source,
            {},
            applied};
    case ProposalLifecycleApplyStatus::rejected_schema:
        if (!state.record(state.audit.state_rejections))
        {
            return {
                AdaptiveV2ManagerIngressStatus::evidence_unhealthy,
                {},
                applied};
        }
        return {AdaptiveV2ManagerIngressStatus::rejected_lifecycle,
                {},
                applied};
    case ProposalLifecycleApplyStatus::stopped:
        return {AdaptiveV2ManagerIngressStatus::stopped, {}, applied};
    case ProposalLifecycleApplyStatus::evidence_unhealthy:
        state.fail_closed();
        return {AdaptiveV2ManagerIngressStatus::evidence_unhealthy,
                {},
                applied};
    }
    state.fail_closed();
    return {AdaptiveV2ManagerIngressStatus::evidence_unhealthy,
            {},
            applied};
}

AdaptiveV2ManagerEvidenceResult
AdaptiveV2ManagerIngress::ingest_evidence(
    const AuthenticatedReporter &authenticated_reporter,
    const MsgEvidenceReport &message) noexcept
{
    auto &state = *state_;
    if (state.stopped)
    {
        AdaptiveV2ManagerEvidenceResult result;
        result.status = AdaptiveV2ManagerIngressStatus::stopped;
        return result;
    }
    if (!state.operational())
    {
        state.fail_closed();
        return {};
    }
    if (!state.is_member(authenticated_reporter.replica_id))
    {
        AdaptiveV2ManagerEvidenceResult result;
        result.status = state.record(state.audit.nonmember_rejections)
            ? AdaptiveV2ManagerIngressStatus::rejected_nonmember
            : AdaptiveV2ManagerIngressStatus::evidence_unhealthy;
        result.ledger_high_watermark = state.ledger.high_watermark();
        return result;
    }
    if (message.serialized.size() >
        state.limits.evidence_wire.maximum_payload_bytes)
    {
        try
        {
            state.ledger.reject_wire(
                authenticated_reporter,
                EvidenceWireError::payload_too_large);
        }
        catch (...)
        {
            state.fail_closed();
            return {};
        }
        AdaptiveV2ManagerEvidenceResult result;
        result.wire_error = EvidenceWireError::payload_too_large;
        result.status =
            state.record(state.audit.evidence_wire_rejections) &&
                    state.ledger.healthy()
                ? AdaptiveV2ManagerIngressStatus::rejected_wire
                : AdaptiveV2ManagerIngressStatus::evidence_unhealthy;
        result.ledger_high_watermark = state.ledger.high_watermark();
        if (result.status ==
            AdaptiveV2ManagerIngressStatus::evidence_unhealthy)
        {
            state.fail_closed();
        }
        return result;
    }
    try
    {
        return ingest_evidence(
            authenticated_reporter,
            static_cast<bytearray_t>(message.serialized));
    }
    catch (...)
    {
        state.fail_closed();
        return {};
    }
}

AdaptiveV2ManagerEvidenceResult
AdaptiveV2ManagerIngress::ingest_evidence(
    const AuthenticatedReporter &authenticated_reporter,
    const bytearray_t &canonical_payload) noexcept
{
    auto &state = *state_;
    AdaptiveV2ManagerEvidenceResult result;
    result.remaining_quarantined_observations =
        state.coordinator.stats().quarantined_records;
    result.ledger_high_watermark = state.ledger.high_watermark();

    if (state.stopped)
    {
        result.status = AdaptiveV2ManagerIngressStatus::stopped;
        return result;
    }
    if (!state.operational())
    {
        state.fail_closed();
        return result;
    }
    if (!state.is_member(authenticated_reporter.replica_id))
    {
        result.status = state.record(state.audit.nonmember_rejections)
            ? AdaptiveV2ManagerIngressStatus::rejected_nonmember
            : AdaptiveV2ManagerIngressStatus::evidence_unhealthy;
        return result;
    }

    const auto decoded = decode_evidence_batch(
        canonical_payload, state.limits.evidence_wire);
    if (!decoded)
    {
        result.wire_error = decoded.error;
        if (decoded.error == EvidenceWireError::allocation_failure ||
            decoded.error == EvidenceWireError::internal_failure)
        {
            state.record(state.audit.evidence_wire_rejections);
            state.fail_closed();
            return result;
        }
        try
        {
            state.ledger.reject_wire(
                authenticated_reporter, decoded.error);
        }
        catch (...)
        {
            state.fail_closed();
            return result;
        }
        result.status =
            state.record(state.audit.evidence_wire_rejections) &&
                    state.ledger.healthy()
                ? AdaptiveV2ManagerIngressStatus::rejected_wire
                : AdaptiveV2ManagerIngressStatus::evidence_unhealthy;
        result.ledger_high_watermark = state.ledger.high_watermark();
        if (result.status ==
            AdaptiveV2ManagerIngressStatus::evidence_unhealthy)
        {
            state.fail_closed();
        }
        return result;
    }

    result.status = AdaptiveV2ManagerIngressStatus::processed;
    result.decoded_observations = decoded.batch->observations.size();
    for (const auto &observation : decoded.batch->observations)
    {
        if (observation.reporter_id !=
            authenticated_reporter.replica_id)
        {
            if (state.is_member(observation.reporter_id))
            {
                if (!state.record(
                        state.audit.spoofed_source_rejections))
                {
                    result.status = AdaptiveV2ManagerIngressStatus::
                        evidence_unhealthy;
                    return result;
                }
                result.status = AdaptiveV2ManagerIngressStatus::
                    rejected_spoofed_source;
            }
            else
            {
                if (!state.record(state.audit.nonmember_rejections))
                {
                    result.status = AdaptiveV2ManagerIngressStatus::
                        evidence_unhealthy;
                    return result;
                }
                result.status =
                    AdaptiveV2ManagerIngressStatus::rejected_nonmember;
            }
        }

        const auto observed = state.coordinator.ingest_observation(
            authenticated_reporter, observation);
        ++result.processed_observations;
        result.accepted_observations +=
            observed.accepted_observations;
        result.rejected_observations +=
            observed.rejected_observations;
        result.remaining_quarantined_observations =
            observed.quarantined_observations;
        switch (observed.disposition)
        {
        case EvidenceObservationDisposition::ingested:
            break;
        case EvidenceObservationDisposition::quarantined_unknown:
        case EvidenceObservationDisposition::quarantined_behind_unknown:
            ++result.newly_quarantined_observations;
            break;
        case EvidenceObservationDisposition::duplicate_quarantined:
            ++result.duplicate_quarantined_observations;
            break;
        case EvidenceObservationDisposition::stopped:
            result.status = AdaptiveV2ManagerIngressStatus::stopped;
            return result;
        case EvidenceObservationDisposition::evidence_unhealthy:
            state.fail_closed();
            result.status =
                AdaptiveV2ManagerIngressStatus::evidence_unhealthy;
            return result;
        }
    }
    result.remaining_quarantined_observations =
        state.coordinator.stats().quarantined_records;
    result.ledger_high_watermark = state.ledger.high_watermark();
    return result;
}

const std::vector<ReplicaID> &
AdaptiveV2ManagerIngress::membership() const noexcept
{
    return state_->membership;
}

const ByzantineQuorum &
AdaptiveV2ManagerIngress::quorum_metadata() const noexcept
{
    return state_->quorum;
}

const ConfigurationId &
AdaptiveV2ManagerIngress::current_configuration() const noexcept
{
    return state_->current_configuration;
}

std::uint64_t
AdaptiveV2ManagerIngress::activation_generation() const noexcept
{
    return state_->activation_generation;
}

const EpochDefinition &
AdaptiveV2ManagerIngress::current_epoch() const noexcept
{
    return *state_->current_epoch;
}

const EvidenceLedger &
AdaptiveV2ManagerIngress::ledger() const noexcept
{
    return state_->ledger;
}

AdaptiveV2ManagerReadinessStats
AdaptiveV2ManagerIngress::readiness_stats() const noexcept
{
    return state_->readiness_snapshot();
}

EvidenceLifecycleStats
AdaptiveV2ManagerIngress::lifecycle_stats() const noexcept
{
    return state_->coordinator.stats();
}

AdaptiveV2ManagerIngressAuditStats
AdaptiveV2ManagerIngress::audit_stats() const noexcept
{
    return state_->audit_snapshot();
}

bool AdaptiveV2ManagerIngress::all_members_ready() const noexcept
{
    return state_->ready_members == state_->membership.size();
}

bool AdaptiveV2ManagerIngress::healthy() const noexcept
{
    return state_->operational();
}

void AdaptiveV2ManagerIngress::shutdown() noexcept
{
    state_->stopped = true;
    state_->coordinator.shutdown();
    state_->proposal_index.shutdown();
}

} // namespace hotstuff
