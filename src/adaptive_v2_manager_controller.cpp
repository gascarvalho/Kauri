#include "hotstuff/adaptive_v2_manager_controller.h"

#include <algorithm>
#include <stdexcept>
#include <utility>

namespace hotstuff
{
namespace
{

bool is_member(
    const std::vector<ReplicaID> &membership,
    ReplicaID replica_id) noexcept
{
    return std::binary_search(
        membership.begin(), membership.end(), replica_id);
}

std::size_t first_leaf_index(
    std::size_t member_count,
    std::uint32_t fanout) noexcept
{
    return member_count == 1
               ? 0
               : ((member_count - 2) / fanout) + 1;
}

enum class ConsensusConstraintStatus : std::uint8_t
{
    guarded_fallback = 1,
    inherited,
    invalid,
};

struct ConsensusConstraints
{
    ConsensusConstraintStatus status{
        ConsensusConstraintStatus::invalid};
    std::vector<ReplicaID> replicas;
};

ConsensusConstraints
consensus_inherited_wait_exempt(
    const EpochDefinition &current,
    const std::vector<ReplicaID> &membership,
    std::size_t required_count)
{
    if (current.trees().empty() || required_count == 0)
        return {};

    const auto canonical =
        current.trees().front().wait_exempt_leaves;
    if (canonical.empty())
    {
        const auto uniformly_empty = std::all_of(
            current.trees().begin(),
            current.trees().end(),
            [](const auto &tree) {
                return tree.wait_exempt_leaves.empty();
            });
        return {
            uniformly_empty
                ? ConsensusConstraintStatus::guarded_fallback
                : ConsensusConstraintStatus::invalid,
            {}};
    }
    if (canonical.size() != required_count ||
        !std::is_sorted(canonical.begin(), canonical.end()) ||
        std::adjacent_find(canonical.begin(), canonical.end()) !=
            canonical.end())
    {
        return {};
    }
    for (const auto replica_id : canonical)
    {
        if (!is_member(membership, replica_id))
            return {};
    }

    for (const auto &tree : current.trees())
    {
        if (tree.fanout == 0 ||
            tree.wait_exempt_leaves != canonical)
        {
            return {};
        }
        const auto leaf_start = first_leaf_index(
            tree.members_breadth_first.size(), tree.fanout);
        for (const auto replica_id : canonical)
        {
            const auto found = std::find(
                tree.members_breadth_first.begin(),
                tree.members_breadth_first.end(),
                replica_id);
            if (found == tree.members_breadth_first.end() ||
                static_cast<std::size_t>(std::distance(
                    tree.members_breadth_first.begin(), found)) <
                    leaf_start)
            {
                return {};
            }
        }
    }
    return {
        ConsensusConstraintStatus::inherited,
        canonical};
}

AcceptedEvidenceView accepted_prefix(
    const EvidenceLedger &ledger,
    const std::vector<ReplicaID> &membership,
    const AdaptationEpochId &epoch,
    std::uint64_t cutoff)
{
    const auto &accepted = ledger.accepted();
    std::uint64_t previous_sequence = 0;
    std::size_t prefix_size = 0;
    bool past_cutoff = false;
    for (const auto &record : accepted)
    {
        if (record.ingestion_sequence == 0 ||
            record.ingestion_sequence <= previous_sequence)
        {
            throw std::logic_error(
                "manager controller accepted evidence is not ordered");
        }
        previous_sequence = record.ingestion_sequence;
        if (record.ingestion_sequence > cutoff)
        {
            past_cutoff = true;
            continue;
        }
        if (past_cutoff ||
            record.observation.configuration.epoch_number !=
                epoch.epoch_number ||
            record.observation.configuration.epoch_digest !=
                epoch.epoch_digest ||
            !is_member(membership, record.observation.reporter_id) ||
            !is_member(
                membership,
                record.observation.observed_replica_id))
        {
            throw std::logic_error(
                "manager controller accepted evidence prefix is invalid");
        }
        for (const auto signer : record.observation.signer_set)
        {
            if (!is_member(membership, signer))
            {
                throw std::logic_error(
                    "manager controller evidence signer is not a member");
            }
        }
        ++prefix_size;
    }

    return {
        prefix_size == 0 ? nullptr : accepted.data(),
        prefix_size};
}

enum class BaselineSnapshotStatus : std::uint8_t
{
    responsive = 1,
    incomplete,
    invalid,
};

BaselineSnapshotStatus validate_baseline_snapshot(
    const AdaptationSnapshot &snapshot,
    const std::vector<ReplicaID> &membership,
    const AdaptationEpochId &epoch,
    std::uint64_t cutoff,
    std::size_t accepted_record_count,
    const AdaptationPolicy &policy,
    std::uint64_t seed,
    TreePolicyKind transition_intent,
    std::size_t required_responsive)
{
    if (snapshot.schema_version() != kAdaptationSchemaVersion ||
        snapshot.epoch() != epoch ||
        snapshot.evidence_cutoff() != cutoff ||
        snapshot.accepted_record_count() != accepted_record_count ||
        snapshot.policy() != policy || snapshot.seed() != seed ||
        snapshot.ranking().size() != membership.size())
    {
        return BaselineSnapshotStatus::invalid;
    }

    std::vector<ReplicaID> ranked_members;
    ranked_members.reserve(snapshot.ranking().size());
    std::size_t responsive = 0;
    for (std::size_t index = 0;
         index < snapshot.ranking().size();
         ++index)
    {
        const auto &entry = snapshot.ranking()[index];
        if (entry.rank != static_cast<std::uint32_t>(index) ||
            !is_member(membership, entry.replica_id) ||
            (entry.classification != ResponsivenessClass::responsive &&
             entry.classification !=
                 ResponsivenessClass::insufficient_evidence &&
             entry.classification != ResponsivenessClass::nonresponsive) ||
            entry.eligible !=
                (entry.classification == ResponsivenessClass::responsive))
        {
            return BaselineSnapshotStatus::invalid;
        }
        if (entry.eligible)
            ++responsive;
        ranked_members.push_back(entry.replica_id);
    }
    std::sort(ranked_members.begin(), ranked_members.end());
    if (ranked_members != membership)
        return BaselineSnapshotStatus::invalid;
    switch (transition_intent)
    {
    case TreePolicyKind::fault_containment:
        return responsive == membership.size()
                   ? BaselineSnapshotStatus::responsive
                   : BaselineSnapshotStatus::incomplete;
    case TreePolicyKind::performance_optimization:
        if (required_responsive == 0 ||
            required_responsive > membership.size())
        {
            return BaselineSnapshotStatus::invalid;
        }
        return responsive >= required_responsive
                   ? BaselineSnapshotStatus::responsive
                   : BaselineSnapshotStatus::incomplete;
    }
    return BaselineSnapshotStatus::invalid;
}

} // namespace

struct AdaptiveV2ManagerController::State
{
    State(const AdaptiveV2ManagerIngress &ingress_,
          AdaptiveV2ManagerControllerConfig config_)
        : ingress(ingress_),
          config(std::move(config_)),
          epoch{
              ingress.current_epoch().epoch_number(),
              ingress.current_epoch().epoch_digest()},
          selector(
              ingress.ledger(),
              ingress.membership(),
              epoch,
              config.selection,
              config.reputation_limits)
    {
        locally_healthy = ingress.healthy() && selector.healthy();
        switch (config.transition_policy.intent)
        {
        case TreePolicyKind::fault_containment:
            break;
        case TreePolicyKind::performance_optimization:
        {
            const auto inherited = consensus_inherited_wait_exempt(
                ingress.current_epoch(),
                ingress.membership(),
                config.selection.required_nonresponsive);
            if (inherited.status ==
                ConsensusConstraintStatus::invalid)
            {
                locally_healthy = false;
            }
            else if (inherited.status ==
                     ConsensusConstraintStatus::inherited)
            {
                inherit_consensus_wait_exempt = true;
                inherited_wait_exempt = inherited.replicas;
            }
            break;
        }
        default:
            locally_healthy = false;
            break;
        }
    }

    bool operational() const noexcept
    {
        return locally_healthy && ingress.healthy() && selector.healthy();
    }

    AdaptiveV2ManagerControllerStatus fail_closed() noexcept
    {
        locally_healthy = false;
        return AdaptiveV2ManagerControllerStatus::unhealthy;
    }

    AdaptiveV2ManagerControllerStatus freeze_baseline(
        std::uint64_t cutoff)
    {
        if (baseline_examined)
        {
            if (cutoff < last_baseline_examined_cutoff)
                return fail_closed();
            if (cutoff == last_baseline_examined_cutoff)
            {
                return AdaptiveV2ManagerControllerStatus::
                    awaiting_responsive_baseline;
            }
        }
        baseline_examined = true;
        last_baseline_examined_cutoff = cutoff;

        const auto prefix = accepted_prefix(
            ingress.ledger(), ingress.membership(), epoch, cutoff);
        auto candidate = std::make_unique<AdaptationSnapshot>(
            build_adaptation_snapshot(
                ingress.membership(),
                epoch,
                prefix,
                cutoff,
                config.selection.responsiveness_policy,
                config.selection.snapshot_seed));
        const auto baseline = validate_baseline_snapshot(
            *candidate,
            ingress.membership(),
            epoch,
            cutoff,
            prefix.size,
            config.selection.responsiveness_policy,
            config.selection.snapshot_seed,
            config.transition_policy.intent,
            static_cast<std::size_t>(
                ingress.quorum_metadata().quorum));
        if (baseline == BaselineSnapshotStatus::invalid)
            return fail_closed();
        if (baseline == BaselineSnapshotStatus::incomplete)
        {
            return AdaptiveV2ManagerControllerStatus::
                awaiting_responsive_baseline;
        }

        const auto frozen = selector.freeze_baseline(cutoff);
        if (frozen != AdaptiveV2SelectionStatus::baseline_frozen ||
            !selector.healthy() ||
            selector.baseline_cutoff() != cutoff ||
            selector.current_cutoff() != cutoff)
        {
            return fail_closed();
        }
        baseline_snapshot = std::move(candidate);
        return AdaptiveV2ManagerControllerStatus::baseline_frozen;
    }

    AdaptiveV2ManagerControllerStatus build_successor()
    {
        if (factory_attempted)
            return fail_closed();
        factory_attempted = true;
        auto built = build_adaptive_v2_successor_bundle(
            ingress.current_epoch(),
            *latest_selection,
            config.transition_policy,
            config.placement,
            config.activation_delay_blocks,
            config.issuer_id,
            config.issuer_private_key,
            config.bundle_limits);
        if (!built || built.bundle == nullptr)
            return fail_closed();
        successor = std::move(built.bundle);
        return AdaptiveV2ManagerControllerStatus::successor_ready;
    }

    AdaptiveV2ManagerControllerStatus select_successor(
        std::uint64_t cutoff)
    {
        if (cutoff < selector.current_cutoff())
            return fail_closed();
        if (cutoff == selector.current_cutoff())
        {
            return AdaptiveV2ManagerControllerStatus::
                awaiting_guarded_selection;
        }

        AdaptiveV2SelectionResult selected;
        switch (config.transition_policy.intent)
        {
        case TreePolicyKind::fault_containment:
            selected = selector.select_through(cutoff);
            break;
        case TreePolicyKind::performance_optimization:
            selected = inherit_consensus_wait_exempt
                           ? selector
                                 .rank_inheriting_constraints_through(
                                     cutoff,
                                     inherited_wait_exempt)
                           : selector.select_through(cutoff);
            break;
        default:
            return fail_closed();
        }
        latest_selection =
            std::make_unique<AdaptiveV2SelectionResult>(
                std::move(selected));
        if (!selector.healthy())
            return fail_closed();

        switch (latest_selection->status)
        {
        case AdaptiveV2SelectionStatus::insufficient_guarded_candidates:
        case AdaptiveV2SelectionStatus::insufficient_eligible_roots:
            return AdaptiveV2ManagerControllerStatus::
                awaiting_guarded_selection;
        case AdaptiveV2SelectionStatus::selected:
            break;
        case AdaptiveV2SelectionStatus::baseline_frozen:
        case AdaptiveV2SelectionStatus::invalid_state:
        case AdaptiveV2SelectionStatus::invalid_cutoff:
        case AdaptiveV2SelectionStatus::ledger_unhealthy:
        case AdaptiveV2SelectionStatus::mixed_epoch:
        case AdaptiveV2SelectionStatus::nonmember_evidence:
        case AdaptiveV2SelectionStatus::projection_failed:
        case AdaptiveV2SelectionStatus::capacity_exceeded:
        case AdaptiveV2SelectionStatus::snapshot_failed:
        case AdaptiveV2SelectionStatus::internal_failure:
            return fail_closed();
        }
        return build_successor();
    }

    const AdaptiveV2ManagerIngress &ingress;
    AdaptiveV2ManagerControllerConfig config;
    AdaptationEpochId epoch;
    AdaptiveV2ByzantineSelection selector;
    std::unique_ptr<AdaptationSnapshot> baseline_snapshot;
    std::unique_ptr<AdaptiveV2SelectionResult> latest_selection;
    std::unique_ptr<const AdaptiveV2EpochChangeBundle> successor;
    std::vector<ReplicaID> inherited_wait_exempt;
    bool inherit_consensus_wait_exempt{false};
    std::uint64_t last_baseline_examined_cutoff{0};
    bool baseline_examined{false};
    bool factory_attempted{false};
    bool locally_healthy{true};
};

AdaptiveV2ManagerController::AdaptiveV2ManagerController(
    const AdaptiveV2ManagerIngress &ingress,
    AdaptiveV2ManagerControllerConfig config)
    : state_(std::make_unique<State>(ingress, std::move(config)))
{}

AdaptiveV2ManagerController::~AdaptiveV2ManagerController() = default;

AdaptiveV2ManagerControllerStatus
AdaptiveV2ManagerController::evaluate() noexcept
{
    auto &state = *state_;
    try
    {
        if (!state.operational())
            return state.fail_closed();
        if (state.successor != nullptr)
            return AdaptiveV2ManagerControllerStatus::already_ready;
        if (!state.ingress.operationally_ready())
            return AdaptiveV2ManagerControllerStatus::awaiting_readiness;

        const auto cutoff = state.ingress.ledger().high_watermark();
        if (!state.selector.baseline_frozen())
            return state.freeze_baseline(cutoff);
        return state.select_successor(cutoff);
    }
    catch (...)
    {
        return state.fail_closed();
    }
}

const AdaptationSnapshot *
AdaptiveV2ManagerController::baseline_audit_snapshot() const noexcept
{
    return state_->baseline_snapshot.get();
}

const AdaptiveV2SelectionResult *
AdaptiveV2ManagerController::selection_audit() const noexcept
{
    return state_->latest_selection.get();
}

const std::vector<EvidenceReputationAuditUpdate> &
AdaptiveV2ManagerController::score_trajectory() const noexcept
{
    return state_->selector.score_trajectory();
}

const AdaptiveV2EpochChangeBundle *
AdaptiveV2ManagerController::successor_bundle() const noexcept
{
    return state_->successor.get();
}

std::uint64_t
AdaptiveV2ManagerController::baseline_cutoff() const noexcept
{
    return state_->selector.baseline_cutoff();
}

std::uint64_t
AdaptiveV2ManagerController::current_cutoff() const noexcept
{
    return state_->selector.current_cutoff();
}

bool AdaptiveV2ManagerController::baseline_frozen() const noexcept
{
    return state_->selector.baseline_frozen();
}

bool AdaptiveV2ManagerController::healthy() const noexcept
{
    return state_->operational();
}

} // namespace hotstuff
