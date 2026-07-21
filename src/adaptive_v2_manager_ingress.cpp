#include "hotstuff/adaptive_v2_manager_ingress.h"

#include <algorithm>
#include <limits>
#include <map>
#include <stdexcept>
#include <utility>

#include "hotstuff/epoch_activation.h"

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

struct ReporterCommitBoundary
{
    std::uint64_t evidence_sequence_fence{0};
    bool terminal_seen{false};
};

struct ReporterCommitFrontier
{
    std::vector<ReporterCommitBoundary> reporters;
};

using CorroboratingSources = std::map<ReplicaID, std::uint64_t>;
using ReporterCommitFrontiers =
    std::map<ProposalKey, ReporterCommitFrontier>;

/**
 * Preserve the causal prefix of each authenticated reporter while keeping the
 * proposal's global lifecycle status stale.
 *
 * This relies on the adaptive-v2 transport's pinned single manager peer and
 * one cross-stream AdaptiveV2ReportingOutbox per replica. That outbox sends
 * readiness, lifecycle, and evidence reports through one FIFO connection. If
 * the transport ever permits concurrent connections for one authenticated
 * replica, the wire must first carry and validate one cross-stream sequence.
 * Completed per-reporter fences remain retained for this fixed configuration:
 * a pre-commit observation may still be blocked behind an older unknown FIFO
 * head after every commit marker arrives. The proposal-index exact bound also
 * bounds these frontiers; fixed-configuration shutdown releases them.
 */
class ReporterCausalProposalEvidenceWindow final
    : public ProposalEvidenceWindow
{
public:
    ReporterCausalProposalEvidenceWindow(
        const ProposalEvidenceIndex &global_window,
        const std::vector<ReplicaID> &membership,
        const ReporterCommitFrontiers &frontiers) noexcept
        : global_window_(global_window),
          membership_(membership),
          frontiers_(frontiers)
    {}

    ProposalEvidenceStatus classify(
        const ProposalKey &proposal) const noexcept override
    {
        return global_window_.classify(proposal);
    }

    ProposalEvidenceStatus classify_for_reporter(
        const AuthenticatedReporter &authenticated_reporter,
        const ResponseObservation &observation) const noexcept override
    {
        const auto proposal = observation.proposal_key();
        const auto global = global_window_.classify(proposal);
        if (global != ProposalEvidenceStatus::stale)
            return global;

        const auto frontier = frontiers_.find(proposal);
        if (frontier == frontiers_.end())
            return global;

        const auto member = std::lower_bound(
            membership_.begin(),
            membership_.end(),
            authenticated_reporter.replica_id);
        if (member == membership_.end() ||
            *member != authenticated_reporter.replica_id ||
            frontier->second.reporters.size() != membership_.size())
        {
            return global;
        }
        const auto index = static_cast<std::size_t>(
            member - membership_.begin());
        const auto &boundary = frontier->second.reporters[index];
        if (!boundary.terminal_seen)
            return ProposalEvidenceStatus::unknown;
        return observation.reporter_sequence <=
                       boundary.evidence_sequence_fence
            ? ProposalEvidenceStatus::admissible
            : global;
    }

private:
    const ProposalEvidenceIndex &global_window_;
    const std::vector<ReplicaID> &membership_;
    const ReporterCommitFrontiers &frontiers_;
};

/**
 * Mutable evidence state for exactly one active configuration.
 *
 * Declaration order is significant: borrowed dependencies are constructed
 * before their consumers and destroyed after them. Replacing this owner opens
 * a fresh exact-epoch window without replacing session-global source fences.
 */
struct ExactEpochIngressWindow final
{
    ExactEpochIngressWindow(
        const EpochStore &epochs,
        const std::vector<ReplicaID> &membership,
        const AdaptiveV2ManagerIngressLimits &limits)
        : proposal_index(limits.proposal_index),
          reporter_causal_window(
              proposal_index, membership, reporter_commit_frontiers),
          ledger(epochs, reporter_causal_window, limits.evidence_store),
          coordinator(
              proposal_index,
              reporter_causal_window,
              ledger,
              EvidenceLifecycleAccounting{
                  limits.lifecycle_accounting},
              limits.lifecycle)
    {}

    ProposalEvidenceIndex proposal_index;
    ReporterCommitFrontiers reporter_commit_frontiers;
    ReporterCausalProposalEvidenceWindow reporter_causal_window;
    EvidenceLedger ledger;
    ProposalLifecycleEvidenceCoordinator coordinator;
};

bool valid_limits(
    const AdaptiveV2ManagerIngressLimits &limits,
    std::size_t member_count) noexcept
{
    // validate_bootstrap establishes a nonempty membership first. Division
    // avoids multiplication overflow and reserves one equal record share for
    // every reporter; a global record limit smaller than N therefore fails.
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
           limits.lifecycle.maximum_quarantined_records_per_reporter != 0 &&
           limits.lifecycle.maximum_quarantined_records_per_reporter <=
               limits.lifecycle.maximum_quarantined_records / member_count &&
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
    const EpochDefinitionInput &initial_epoch,
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
    if (initial_epoch.schema_version !=
        kEpochDefinitionSchemaVersionV2)
    {
        throw std::invalid_argument(
            "adaptive-v2 manager requires a schema-v2 initial epoch");
    }
    if (initial_epoch.epoch_number != 0)
    {
        throw std::invalid_argument(
            "adaptive-v2 manager requires an epoch-zero initial "
            "definition");
    }
    if (activation_generation == 0)
    {
        throw std::invalid_argument(
            "adaptive-v2 manager activation generation is zero");
    }
    const auto packed_generation = activation_generation - 1;
    const auto rotation_ordinal = static_cast<std::uint32_t>(
        packed_generation);
    const auto canonical_generation = checked_activation_generation(
        initial_epoch.epoch_number, rotation_ordinal);
    if (!canonical_generation.has_value() ||
        *canonical_generation != activation_generation)
    {
        throw std::invalid_argument(
            "adaptive-v2 manager activation generation does not belong "
            "to the initial epoch");
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
        std::uint64_t highest_received_evidence_sequence{0};
        std::size_t pending_associations{0};
    };

    State(ValidatedBootstrap bootstrap,
          EpochDefinitionInput initial_epoch,
          std::uint32_t active_tree_id,
          std::uint64_t activation_generation_)
        : membership(std::move(bootstrap.membership)),
          quorum(bootstrap.quorum),
          limits(std::move(bootstrap.limits)),
          epochs(membership),
          activation_generation(activation_generation_)
    {
        current_epoch = &epochs.stage(
            initial_epoch, EpochValidationContext{});
        if (epochs.find_tree(
                current_epoch->epoch_number(), active_tree_id) == nullptr)
        {
            throw std::invalid_argument(
                "adaptive-v2 manager active tree is not in initial epoch");
        }
        current_configuration = ConfigurationId{
            current_epoch->epoch_number(),
            active_tree_id,
            current_epoch->epoch_digest()};
        window = std::make_unique<ExactEpochIngressWindow>(
            epochs, membership, limits);
        for (const auto member : membership)
        {
            readiness.emplace(member, ReadinessEntry{});
            lifecycle_sources.emplace(member, LifecycleSourceEntry{});
        }
        prepared_readiness_sources.reserve(membership.size());
        if (!window->proposal_index.healthy() ||
            !window->ledger.healthy() ||
            !window->coordinator.healthy())
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

    bool is_current_epoch_configuration(
        const ConfigurationId &configuration) const noexcept
    {
        return current_epoch != nullptr &&
               configuration.epoch_number ==
                   current_epoch->epoch_number() &&
               configuration.epoch_digest ==
                   current_epoch->epoch_digest() &&
               epochs.find_tree(
                   configuration.epoch_number,
                   configuration.tree_id) != nullptr;
    }

    bool operational() const noexcept
    {
        return locally_healthy && !stopped &&
               window != nullptr &&
               window->proposal_index.healthy() &&
               window->ledger.healthy() &&
               window->coordinator.healthy();
    }

    void fail_closed() noexcept
    {
        locally_healthy = false;
        if (window != nullptr)
        {
            window->coordinator.shutdown();
            window->proposal_index.shutdown();
        }
    }

    std::optional<std::size_t> member_index(
        ReplicaID replica_id) const noexcept
    {
        const auto member = std::lower_bound(
            membership.begin(), membership.end(), replica_id);
        if (member == membership.end() || *member != replica_id)
            return std::nullopt;
        return static_cast<std::size_t>(member - membership.begin());
    }

    bool begin_reporter_commit_frontier(
        const ProposalKey &proposal,
        const CorroboratingSources &corroborating_sources) noexcept
    {
        if (window->reporter_commit_frontiers.count(proposal) != 0 ||
            window->reporter_commit_frontiers.size() >=
                limits.proposal_index.maximum_exact_proposals)
        {
            record(audit.capacity_failures);
            fail_closed();
            return false;
        }
        try
        {
            ReporterCommitFrontier frontier;
            frontier.reporters.resize(membership.size());
            for (const auto &source : corroborating_sources)
            {
                const auto index = member_index(source.first);
                if (!index.has_value())
                {
                    fail_closed();
                    return false;
                }
                auto &boundary = frontier.reporters[*index];
                boundary.evidence_sequence_fence = source.second;
                boundary.terminal_seen = true;
            }
            const auto inserted =
                window->reporter_commit_frontiers.emplace(
                    proposal, std::move(frontier));
            if (!inserted.second)
            {
                fail_closed();
                return false;
            }
        }
        catch (...)
        {
            fail_closed();
            return false;
        }
        return true;
    }

    void cancel_reporter_commit_frontier(
        const ProposalKey &proposal) noexcept
    {
        window->reporter_commit_frontiers.erase(proposal);
    }

    bool close_reporter_commit_frontier(
        const ProposalKey &proposal,
        ReplicaID source,
        std::uint64_t evidence_sequence_fence) noexcept
    {
        const auto frontier =
            window->reporter_commit_frontiers.find(proposal);
        if (frontier == window->reporter_commit_frontiers.end())
        {
            fail_closed();
            return false;
        }
        const auto index = member_index(source);
        if (!index.has_value() ||
            frontier->second.reporters.size() != membership.size())
        {
            fail_closed();
            return false;
        }
        auto &boundary = frontier->second.reporters[*index];
        if (boundary.terminal_seen)
            return true;
        boundary.evidence_sequence_fence =
            evidence_sequence_fence;
        boundary.terminal_seen = true;
        return true;
    }

    enum class ReporterCommitBoundaryState : std::uint8_t
    {
        no_frontier = 1,
        open,
        terminal,
        invalid,
    };

    ReporterCommitBoundaryState reporter_commit_boundary_state(
        const ProposalKey &proposal,
        ReplicaID source) noexcept
    {
        const auto frontier =
            window->reporter_commit_frontiers.find(proposal);
        if (frontier == window->reporter_commit_frontiers.end())
            return ReporterCommitBoundaryState::no_frontier;
        const auto index = member_index(source);
        if (!index.has_value() ||
            frontier->second.reporters.size() != membership.size())
        {
            fail_closed();
            return ReporterCommitBoundaryState::invalid;
        }
        return frontier->second.reporters[*index].terminal_seen
            ? ReporterCommitBoundaryState::terminal
            : ReporterCommitBoundaryState::open;
    }

    std::optional<std::uint64_t> reporter_commit_fence(
        const ProposalKey &proposal,
        ReplicaID source) const noexcept
    {
        const auto frontier =
            window->reporter_commit_frontiers.find(proposal);
        const auto index = member_index(source);
        if (frontier == window->reporter_commit_frontiers.end() ||
            !index.has_value() ||
            frontier->second.reporters.size() != membership.size())
        {
            return std::nullopt;
        }
        return frontier->second.reporters[*index].evidence_sequence_fence;
    }

    enum class ReceivedEvidenceSequenceStatus : std::uint8_t
    {
        accepted = 1,
        rejected,
        failed,
    };

    ReceivedEvidenceSequenceStatus consume_received_evidence_sequence(
        ReplicaID source,
        std::uint64_t reporter_sequence) noexcept
    {
        const auto entry = lifecycle_sources.find(source);
        if (entry == lifecycle_sources.end())
        {
            fail_closed();
            return ReceivedEvidenceSequenceStatus::failed;
        }
        if (reporter_sequence == 0 ||
            reporter_sequence <=
                entry->second.highest_received_evidence_sequence)
        {
            return record(audit.evidence_sequence_rejections)
                ? ReceivedEvidenceSequenceStatus::rejected
                : ReceivedEvidenceSequenceStatus::failed;
        }
        entry->second.highest_received_evidence_sequence =
            reporter_sequence;
        return ReceivedEvidenceSequenceStatus::accepted;
    }

    std::size_t open_reporter_commit_reporters() const noexcept
    {
        std::size_t open = 0;
        for (const auto &proposal : window->reporter_commit_frontiers)
        {
            open += static_cast<std::size_t>(std::count_if(
                proposal.second.reporters.begin(),
                proposal.second.reporters.end(),
                [](const ReporterCommitBoundary &boundary) {
                    return !boundary.terminal_seen;
                }));
        }
        return open;
    }

    bool record(std::uint64_t &counter) noexcept
    {
        if (increment(counter))
            return true;
        increment(audit.capacity_failures);
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
        snapshot.reporter_causal_retained_proposals =
            window->reporter_commit_frontiers.size();
        snapshot.reporter_causal_open_reporters =
            open_reporter_commit_reporters();
        return snapshot;
    }

    bool erase_pending_lifecycle_fact(
        const CorroboratedLifecycleFact &fact) noexcept
    {
        const auto pending = pending_lifecycle_votes.find(fact);
        if (pending == pending_lifecycle_votes.end())
            return true;

        for (const auto &source : pending->second)
        {
            const auto entry = lifecycle_sources.find(source.first);
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

    void publish_window_state() noexcept
    {
        for (auto &source : readiness)
        {
            source.second.ready = std::binary_search(
                prepared_readiness_sources.begin(),
                prepared_readiness_sources.end(),
                source.first);
            source.second.committed_height = source.second.ready
                ? prepared_readiness_height
                : 0;
        }
        for (auto &source : lifecycle_sources)
            source.second.pending_associations = 0;
        pending_lifecycle_votes.clear();
        pending_lifecycle_associations = 0;
        ready_members = prepared_readiness_sources.size();
        readiness_accepted = 0;
        readiness_rejected = 0;
        readiness_height_floor = prepared_readiness_height;
        current_epoch = prepared_epoch;
        current_configuration = prepared_configuration;
        activation_generation = prepared_activation_generation;
        window.swap(prepared_window);
        prepared_window.reset();
        prepared_epoch = nullptr;
        prepared_configuration = ConfigurationId{};
        prepared_activation_generation = 0;
        prepared_readiness_sources.clear();
        prepared_readiness_height = 0;
    }

    std::vector<ReplicaID> membership;
    ByzantineQuorum quorum;
    AdaptiveV2ManagerIngressLimits limits;
    EpochStore epochs;
    std::unique_ptr<ExactEpochIngressWindow> window;
    std::unique_ptr<ExactEpochIngressWindow> prepared_window;
    const EpochDefinition *current_epoch{nullptr};
    const EpochDefinition *prepared_epoch{nullptr};
    ConfigurationId current_configuration;
    ConfigurationId prepared_configuration;
    std::uint64_t activation_generation{0};
    std::uint64_t prepared_activation_generation{0};
    std::vector<ReplicaID> prepared_readiness_sources;
    std::uint64_t prepared_readiness_height{0};
    std::uint64_t readiness_height_floor{0};
    std::map<ReplicaID, ReadinessEntry> readiness;
    std::map<ReplicaID, LifecycleSourceEntry> lifecycle_sources;
    std::map<CorroboratedLifecycleFact, CorroboratingSources>
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
    EpochDefinitionInput initial_epoch,
    std::uint32_t active_tree_id,
    std::uint64_t activation_generation,
    AdaptiveV2ManagerIngressLimits limits)
    : state_(std::make_unique<State>(
          validate_bootstrap(
              std::move(membership),
              initial_epoch,
              activation_generation,
              std::move(limits)),
          std::move(initial_epoch),
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
    auto &entry = state.readiness.at(notice.claimed_source_replica_id);
    if (notice.source_sequence <= entry.source_sequence)
    {
        return rejected(
            AdaptiveV2ManagerIngressStatus::rejected_sequence,
            state.audit.state_rejections);
    }
    if (notice.active_configuration != state.current_configuration)
    {
        entry.source_sequence = notice.source_sequence;
        return rejected(
            AdaptiveV2ManagerIngressStatus::rejected_configuration,
            state.audit.state_rejections);
    }
    if (notice.activation_generation != state.activation_generation)
    {
        entry.source_sequence = notice.source_sequence;
        return rejected(
            AdaptiveV2ManagerIngressStatus::rejected_generation,
            state.audit.state_rejections);
    }

    if (notice.committed_height < state.readiness_height_floor ||
        (entry.ready &&
         notice.committed_height < entry.committed_height))
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
    std::uint64_t claimed_evidence_sequence_fence = 0;
    if (std::holds_alternative<NormalProposalRuntimeInitialized>(notice.fact))
    {
        fact = CorroboratedLifecycleFact{
            CorroboratedLifecycleFactKind::normal_runtime_initialized,
            std::get<NormalProposalRuntimeInitialized>(notice.fact).proposal};
    }
    else if (std::holds_alternative<ProposalCommitted>(notice.fact))
    {
        const auto &committed =
            std::get<ProposalCommitted>(notice.fact);
        fact = CorroboratedLifecycleFact{
            CorroboratedLifecycleFactKind::committed,
            committed.proposal};
        claimed_evidence_sequence_fence =
            committed.evidence_sequence_fence;
    }
    else
    {
        state.fail_closed();
        return {AdaptiveV2ManagerIngressStatus::evidence_unhealthy,
                {},
                {}};
    }

    if (!state.is_current_epoch_configuration(
            fact.proposal.configuration))
    {
        if (!state.record(state.audit.state_rejections))
        {
            return {
                AdaptiveV2ManagerIngressStatus::evidence_unhealthy,
                {},
                {}};
        }
        return {AdaptiveV2ManagerIngressStatus::rejected_configuration,
                {},
                {}};
    }

    const auto classification =
        state.window->proposal_index.classify(fact.proposal);
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
    else if (fact.kind == CorroboratedLifecycleFactKind::committed &&
             classification == ProposalEvidenceStatus::stale)
    {
        const auto boundary = state.reporter_commit_boundary_state(
            fact.proposal, authenticated_source.replica_id);
        if (boundary == State::ReporterCommitBoundaryState::invalid ||
            boundary == State::ReporterCommitBoundaryState::no_frontier)
        {
            state.fail_closed();
            return {AdaptiveV2ManagerIngressStatus::evidence_unhealthy,
                    {},
                    {}};
        }
        if (boundary == State::ReporterCommitBoundaryState::terminal)
        {
            const auto frozen_fence = state.reporter_commit_fence(
                fact.proposal, authenticated_source.replica_id);
            if (!frozen_fence.has_value())
            {
                state.fail_closed();
                return {
                    AdaptiveV2ManagerIngressStatus::evidence_unhealthy,
                    {},
                    {}};
            }
            if (claimed_evidence_sequence_fence != *frozen_fence)
            {
                if (!state.record(
                        state.audit.lifecycle_fence_mismatch_rejections))
                {
                    return {
                        AdaptiveV2ManagerIngressStatus::evidence_unhealthy,
                        {},
                        {}};
                }
                return {
                    AdaptiveV2ManagerIngressStatus::rejected_lifecycle,
                    {},
                    {}};
            }
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
        if (claimed_evidence_sequence_fence ==
                std::numeric_limits<std::uint64_t>::max() ||
            claimed_evidence_sequence_fence !=
            source_entry->second.highest_received_evidence_sequence)
        {
            if (!state.record(
                    state.audit.lifecycle_fence_mismatch_rejections))
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
        if (!state.close_reporter_commit_frontier(
                fact.proposal,
                authenticated_source.replica_id,
                claimed_evidence_sequence_fence) ||
            !state.erase_pending_lifecycle_fact(fact))
        {
            return {AdaptiveV2ManagerIngressStatus::evidence_unhealthy,
                    {},
                    {}};
        }
        const auto retried = state.window->coordinator.retry_reporter(
            authenticated_source);
        if (retried.status == ProposalLifecycleApplyStatus::stopped)
        {
            return {AdaptiveV2ManagerIngressStatus::stopped,
                    {},
                    retried};
        }
        if (retried.status != ProposalLifecycleApplyStatus::applied)
        {
            state.fail_closed();
            return {AdaptiveV2ManagerIngressStatus::evidence_unhealthy,
                    {},
                    retried};
        }
        return {AdaptiveV2ManagerIngressStatus::already_applied,
                {},
                retried};
    }

    auto pending = state.pending_lifecycle_votes.find(fact);
    if (pending != state.pending_lifecycle_votes.end())
    {
        const auto frozen = pending->second.find(
            authenticated_source.replica_id);
        if (frozen != pending->second.end())
        {
            if (fact.kind == CorroboratedLifecycleFactKind::committed &&
                claimed_evidence_sequence_fence != frozen->second)
            {
                if (!state.record(
                        state.audit.lifecycle_fence_mismatch_rejections))
                {
                    return {
                        AdaptiveV2ManagerIngressStatus::evidence_unhealthy,
                        {},
                        {}};
                }
                return {
                    AdaptiveV2ManagerIngressStatus::rejected_lifecycle,
                    {},
                    {}};
            }
            return {
                AdaptiveV2ManagerIngressStatus::awaiting_corroboration,
                {},
                {}};
        }
    }

    if (fact.kind == CorroboratedLifecycleFactKind::committed &&
        (claimed_evidence_sequence_fence ==
             std::numeric_limits<std::uint64_t>::max() ||
         claimed_evidence_sequence_fence !=
            source_entry->second.highest_received_evidence_sequence))
    {
        if (!state.record(
                state.audit.lifecycle_fence_mismatch_rejections))
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
                fact, CorroboratingSources{});
            pending = inserted.first;
            inserted_fact = inserted.second;
        }
        const auto frozen_evidence_sequence =
            fact.kind == CorroboratedLifecycleFactKind::committed
            ? claimed_evidence_sequence_fence
            : 0;
        const auto inserted_source = pending->second.emplace(
            authenticated_source.replica_id,
            frozen_evidence_sequence);
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

    const bool globally_committing =
        fact.kind == CorroboratedLifecycleFactKind::committed;
    if (globally_committing &&
        !state.begin_reporter_commit_frontier(
            fact.proposal, pending->second))
    {
        return {AdaptiveV2ManagerIngressStatus::evidence_unhealthy,
                {},
                {}};
    }

    const auto applied = state.window->coordinator.apply_notice(
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
    else if (globally_committing)
    {
        state.cancel_reporter_commit_frontier(fact.proposal);
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
        result.ledger_high_watermark =
            state.window->ledger.high_watermark();
        return result;
    }
    if (message.serialized.size() >
        state.limits.evidence_wire.maximum_payload_bytes)
    {
        try
        {
            state.window->ledger.reject_wire(
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
                    state.window->ledger.healthy()
                ? AdaptiveV2ManagerIngressStatus::rejected_wire
                : AdaptiveV2ManagerIngressStatus::evidence_unhealthy;
        result.ledger_high_watermark =
            state.window->ledger.high_watermark();
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
        state.window->coordinator.stats().quarantined_records;
    result.ledger_high_watermark =
        state.window->ledger.high_watermark();

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
            state.window->ledger.reject_wire(
                authenticated_reporter, decoded.error);
        }
        catch (...)
        {
            state.fail_closed();
            return result;
        }
        result.status =
            state.record(state.audit.evidence_wire_rejections) &&
                    state.window->ledger.healthy()
                ? AdaptiveV2ManagerIngressStatus::rejected_wire
                : AdaptiveV2ManagerIngressStatus::evidence_unhealthy;
        result.ledger_high_watermark =
            state.window->ledger.high_watermark();
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
        // The authenticated transport sequence is consumed immediately after
        // canonical decoding. A later reporter-id, topology, timing, signer,
        // or proposal rejection cannot make a lower sequence reusable.
        const auto sequence = state.consume_received_evidence_sequence(
            authenticated_reporter.replica_id,
            observation.reporter_sequence);
        if (sequence == State::ReceivedEvidenceSequenceStatus::failed)
        {
            result.status =
                AdaptiveV2ManagerIngressStatus::evidence_unhealthy;
            return result;
        }
        if (sequence == State::ReceivedEvidenceSequenceStatus::rejected)
        {
            ++result.processed_observations;
            ++result.rejected_observations;
            continue;
        }

        if (!state.is_current_epoch_configuration(
                observation.configuration))
        {
            ++result.processed_observations;
            ++result.rejected_observations;
            if (!state.record(state.audit.state_rejections))
            {
                result.status =
                    AdaptiveV2ManagerIngressStatus::evidence_unhealthy;
                return result;
            }
            result.status =
                AdaptiveV2ManagerIngressStatus::rejected_configuration;
            continue;
        }

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
        const auto observed = state.window->coordinator.ingest_observation(
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
        case EvidenceObservationDisposition::rejected_quarantine_capacity:
            ++result.quarantine_capacity_rejections;
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
        state.window->coordinator.stats().quarantined_records;
    result.ledger_high_watermark =
        state.window->ledger.high_watermark();
    return result;
}

AdaptiveV2ManagerIngressStatus
AdaptiveV2ManagerIngress::prepare_same_epoch_window_reset() noexcept
{
    auto &state = *state_;
    if (state.stopped)
        return AdaptiveV2ManagerIngressStatus::stopped;
    if (!state.operational())
    {
        state.fail_closed();
        return AdaptiveV2ManagerIngressStatus::evidence_unhealthy;
    }
    if (state.prepared_window != nullptr)
        return AdaptiveV2ManagerIngressStatus::rejected_configuration;

    std::unique_ptr<ExactEpochIngressWindow> next_window;
    try
    {
        next_window = std::make_unique<ExactEpochIngressWindow>(
            state.epochs, state.membership, state.limits);
    }
    catch (...)
    {
        return AdaptiveV2ManagerIngressStatus::rejected_capacity;
    }
    if (!next_window->proposal_index.healthy() ||
        !next_window->ledger.healthy() ||
        !next_window->coordinator.healthy())
    {
        return AdaptiveV2ManagerIngressStatus::evidence_unhealthy;
    }

    state.prepared_epoch = state.current_epoch;
    state.prepared_configuration = state.current_configuration;
    state.prepared_activation_generation = state.activation_generation;
    state.prepared_window = std::move(next_window);
    return AdaptiveV2ManagerIngressStatus::processed;
}

AdaptiveV2ManagerIngressStatus
AdaptiveV2ManagerIngress::prepare_successor_rotation(
    const EpochDefinitionInput &successor,
    std::uint32_t active_tree_id) noexcept
{
    auto &state = *state_;
    if (state.stopped)
        return AdaptiveV2ManagerIngressStatus::stopped;
    if (!state.operational())
    {
        state.fail_closed();
        return AdaptiveV2ManagerIngressStatus::evidence_unhealthy;
    }
    if (state.prepared_window != nullptr)
        return AdaptiveV2ManagerIngressStatus::rejected_configuration;

    const auto successor_epoch = checked_successor_epoch(
        state.current_epoch->epoch_number());
    if (!successor_epoch.has_value() ||
        successor.schema_version != kEpochDefinitionSchemaVersionV2 ||
        successor.epoch_number != *successor_epoch ||
        successor.previous_epoch_digest !=
            state.current_epoch->epoch_digest() ||
        successor.membership_digest !=
            state.current_epoch->membership_digest())
    {
        return AdaptiveV2ManagerIngressStatus::rejected_configuration;
    }

    const auto next_generation = checked_activation_generation(
        successor.epoch_number, 0);
    if (!next_generation.has_value())
    {
        return AdaptiveV2ManagerIngressStatus::rejected_generation;
    }

    const auto selected_tree = std::find_if(
        successor.trees.begin(),
        successor.trees.end(),
        [active_tree_id](const EpochTreeDefinition &tree) {
            return tree.tree_id == active_tree_id;
        });
    if (selected_tree == successor.trees.end())
    {
        return AdaptiveV2ManagerIngressStatus::rejected_configuration;
    }

    std::unique_ptr<ExactEpochIngressWindow> next_window;
    try
    {
        next_window = std::make_unique<ExactEpochIngressWindow>(
            state.epochs, state.membership, state.limits);
    }
    catch (...)
    {
        return AdaptiveV2ManagerIngressStatus::rejected_capacity;
    }
    if (!next_window->proposal_index.healthy() ||
        !next_window->ledger.healthy() ||
        !next_window->coordinator.healthy())
    {
        return AdaptiveV2ManagerIngressStatus::evidence_unhealthy;
    }

    DefinitionAvailabilityResult availability;
    try
    {
        availability = state.epochs.stage_available_v2(
            successor, *state.current_epoch);
    }
    catch (...)
    {
        return AdaptiveV2ManagerIngressStatus::rejected_capacity;
    }
    if ((availability.disposition !=
             DefinitionAvailabilityDisposition::staged &&
         availability.disposition !=
             DefinitionAvailabilityDisposition::duplicate) ||
        availability.definition == nullptr)
    {
        return AdaptiveV2ManagerIngressStatus::rejected_configuration;
    }

    state.prepared_epoch = availability.definition;
    state.prepared_configuration = ConfigurationId{
        availability.definition->epoch_number(),
        active_tree_id,
        availability.definition->epoch_digest()};
    state.prepared_activation_generation = *next_generation;
    state.prepared_window = std::move(next_window);

    return AdaptiveV2ManagerIngressStatus::processed;
}

AdaptiveV2ManagerIngressStatus
AdaptiveV2ManagerIngress::seed_prepared_successor_readiness(
    const std::vector<ReplicaID> &sources,
    std::uint64_t committed_height) noexcept
{
    auto &state = *state_;
    if (state.stopped)
        return AdaptiveV2ManagerIngressStatus::stopped;
    if (!state.operational())
    {
        state.fail_closed();
        return AdaptiveV2ManagerIngressStatus::evidence_unhealthy;
    }
    if (state.prepared_window == nullptr ||
        state.prepared_epoch == nullptr ||
        state.prepared_epoch == state.current_epoch ||
        !state.prepared_readiness_sources.empty() ||
        state.prepared_readiness_height != 0 ||
        committed_height == 0 ||
        sources.size() !=
            static_cast<std::size_t>(state.quorum.quorum))
    {
        return AdaptiveV2ManagerIngressStatus::rejected_configuration;
    }

    try
    {
        auto canonical_sources = sources;
        std::sort(canonical_sources.begin(), canonical_sources.end());
        if (std::adjacent_find(
                canonical_sources.begin(),
                canonical_sources.end()) != canonical_sources.end() ||
            !std::all_of(
                canonical_sources.begin(),
                canonical_sources.end(),
                [&state](ReplicaID source) {
                    return state.is_member(source);
                }))
        {
            return AdaptiveV2ManagerIngressStatus::
                rejected_configuration;
        }
        state.prepared_readiness_sources =
            std::move(canonical_sources);
        state.prepared_readiness_height = committed_height;
        return AdaptiveV2ManagerIngressStatus::processed;
    }
    catch (...)
    {
        return AdaptiveV2ManagerIngressStatus::rejected_capacity;
    }
}

void AdaptiveV2ManagerIngress::publish_prepared_window() noexcept
{
    auto &state = *state_;
    if (state.prepared_window == nullptr ||
        state.prepared_epoch == nullptr)
    {
        state.fail_closed();
        return;
    }
    state.publish_window_state();
}

void AdaptiveV2ManagerIngress::discard_prepared_window() noexcept
{
    auto &state = *state_;
    state.prepared_window.reset();
    state.prepared_epoch = nullptr;
    state.prepared_configuration = ConfigurationId{};
    state.prepared_activation_generation = 0;
    state.prepared_readiness_sources.clear();
    state.prepared_readiness_height = 0;
}

AdaptiveV2ManagerIngressStatus
AdaptiveV2ManagerIngress::rotate_to_successor(
    const EpochDefinitionInput &successor,
    std::uint32_t active_tree_id) noexcept
{
    const auto prepared = prepare_successor_rotation(
        successor, active_tree_id);
    if (prepared != AdaptiveV2ManagerIngressStatus::processed)
        return prepared;
    publish_prepared_window();

    return AdaptiveV2ManagerIngressStatus::processed;
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
    return state_->window->ledger;
}

AdaptiveV2ManagerReadinessStats
AdaptiveV2ManagerIngress::readiness_stats() const noexcept
{
    return state_->readiness_snapshot();
}

EvidenceLifecycleStats
AdaptiveV2ManagerIngress::lifecycle_stats() const noexcept
{
    return state_->window->coordinator.stats();
}

AdaptiveV2ManagerIngressAuditStats
AdaptiveV2ManagerIngress::audit_stats() const noexcept
{
    return state_->audit_snapshot();
}

bool AdaptiveV2ManagerIngress::operationally_ready() const noexcept
{
    return state_->operational() &&
           state_->ready_members >=
               static_cast<std::size_t>(state_->quorum.quorum);
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
    discard_prepared_window();
    state_->window->coordinator.shutdown();
    state_->window->proposal_index.shutdown();
    state_->window->reporter_commit_frontiers.clear();
}

} // namespace hotstuff
