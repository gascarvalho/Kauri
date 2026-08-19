#include "hotstuff/proposal_context.h"

#include <algorithm>
#include <chrono>
#include <iterator>
#include <utility>

namespace hotstuff
{
namespace
{

bool is_zero(const uint256_t &value)
{
    return value == uint256_t{};
}

std::set<ReplicaID> as_set(const std::vector<ReplicaID> &values)
{
    return std::set<ReplicaID>(values.begin(), values.end());
}

std::set<ReplicaID> set_intersection(
    const std::set<ReplicaID> &left,
    const std::set<ReplicaID> &right)
{
    std::set<ReplicaID> result;
    std::set_intersection(
        left.begin(), left.end(),
        right.begin(), right.end(),
        std::inserter(result, result.end()));
    return result;
}

std::set<ReplicaID> set_difference(
    const std::set<ReplicaID> &left,
    const std::set<ReplicaID> &right)
{
    std::set<ReplicaID> result;
    std::set_difference(
        left.begin(), left.end(),
        right.begin(), right.end(),
        std::inserter(result, result.end()));
    return result;
}

void collect_subtree(
    const std::vector<ReplicaID> &members,
    std::size_t root_index,
    std::size_t fanout,
    std::vector<ReplicaID> &ordered,
    std::set<ReplicaID> *members_set = nullptr)
{
    const auto member = members[root_index];
    ordered.push_back(member);
    if (members_set != nullptr)
        members_set->insert(member);
    for (std::size_t offset = 1; offset <= fanout; ++offset)
    {
        const auto child_index = fanout * root_index + offset;
        if (child_index >= members.size())
            break;
        collect_subtree(
            members,
            child_index,
            fanout,
            ordered,
            members_set);
    }
}

bool tree_equal(const ProposalTreeSnapshot &left,
                const ProposalTreeSnapshot &right)
{
    return left.local_replica == right.local_replica &&
           left.root == right.root &&
           left.parent == right.parent &&
           left.direct_children == right.direct_children &&
           left.assigned_subtree == right.assigned_subtree &&
           left.child_subtrees == right.child_subtrees &&
           left.required_subtree == right.required_subtree &&
           left.optional_subtree == right.optional_subtree &&
           left.required_child_subtrees ==
               right.required_child_subtrees &&
           left.fanout == right.fanout &&
           left.pipeline_stretch == right.pipeline_stretch;
}

void derive_frozen_wait_policy(
    ProposalTreeSnapshot &tree,
    const std::set<ReplicaID> &wait_exempt_leaves)
{
    const auto assigned = as_set(tree.assigned_subtree);
    tree.optional_subtree =
        set_intersection(assigned, wait_exempt_leaves);
    tree.required_subtree =
        set_difference(assigned, tree.optional_subtree);
    tree.required_child_subtrees.clear();
    for (const auto &branch : tree.child_subtrees)
        tree.required_child_subtrees.emplace(
            branch.first,
            set_intersection(branch.second, tree.required_subtree));
}

ProposalContextMetadata normalized_metadata(
    const ProposalContextMetadata &metadata)
{
    auto normalized = metadata;
    if (normalized.tree.required_subtree.empty() &&
        normalized.tree.optional_subtree.empty() &&
        normalized.tree.required_child_subtrees.empty())
    {
        // Pre-WE03 callers expressed the empty wait-exempt policy by omitting
        // these derived fields. Freeze that legacy meaning before admission.
        derive_frozen_wait_policy(normalized.tree, {});
    }
    return normalized;
}

void initialize_runtime(
    ProposalContextSnapshot &runtime,
    const ProposalTreeSnapshot &tree)
{
    const auto children = as_set(tree.direct_children);
    runtime.pending_observation_children = children;
    runtime.pending_children = children;
    for (const auto &branch : tree.required_child_subtrees)
        if (!branch.second.empty())
            runtime.pending_required_child_branches.insert(branch.first);
}

void mark_observed(
    ProposalContextSnapshot &runtime,
    ReplicaID child)
{
    runtime.pending_observation_children.erase(child);
    runtime.pending_children.erase(child);
}

void update_required_branch_completion(
    const ProposalTreeSnapshot &tree,
    ProposalContextSnapshot &runtime,
    ReplicaID child)
{
    const auto branch = tree.required_child_subtrees.find(child);
    if (branch == tree.required_child_subtrees.end())
        return;
    if (std::includes(
            runtime.verified_signers.begin(),
            runtime.verified_signers.end(),
            branch->second.begin(), branch->second.end()))
        runtime.pending_required_child_branches.erase(child);
}

bool is_deterministic_terminal(ProposalContextEvent event)
{
    return event == ProposalContextEvent::proposal_aborted ||
           event == ProposalContextEvent::committed ||
           event == ProposalContextEvent::shutdown;
}

bool exact_signer_set(
    const QuorumCert &certificate,
    std::set<ReplicaID> &signers)
{
    const auto enumerated = certificate.get_signers();
    signers = {enumerated.begin(), enumerated.end()};
    return signers.size() == enumerated.size() &&
           signers.size() ==
               const_cast<QuorumCert &>(certificate).get_sigs_n();
}

std::optional<ProposalContextMetadata>
make_exact_proposal_context_metadata_impl(
    const ProposalKey &key,
    ReplicaID local_replica,
    const std::vector<ReplicaID> &members_breadth_first,
    std::uint32_t fanout,
    std::uint32_t pipeline_stretch,
    const std::vector<ReplicaID> &wait_exempt_leaves,
    std::size_t global_quorum)
{
    if (members_breadth_first.empty() || fanout == 0 ||
        global_quorum == 0 ||
        as_set(members_breadth_first).size() !=
            members_breadth_first.size() ||
        as_set(wait_exempt_leaves).size() !=
            wait_exempt_leaves.size())
        return std::nullopt;

    for (const auto wait_exempt : wait_exempt_leaves)
    {
        const auto member = std::find(
            members_breadth_first.begin(),
            members_breadth_first.end(),
            wait_exempt);
        if (member == members_breadth_first.end())
            return std::nullopt;
        const auto index = static_cast<std::size_t>(std::distance(
            members_breadth_first.begin(), member));
        if (fanout * index + 1 < members_breadth_first.size())
            return std::nullopt;
    }

    const auto local = std::find(
        members_breadth_first.begin(),
        members_breadth_first.end(),
        local_replica);
    if (local == members_breadth_first.end())
        return std::nullopt;

    const auto local_index = static_cast<std::size_t>(
        std::distance(members_breadth_first.begin(), local));
    ProposalTreeSnapshot tree;
    tree.local_replica = local_replica;
    tree.root = members_breadth_first.front();
    tree.fanout = fanout;
    tree.pipeline_stretch = pipeline_stretch;
    if (local_index != 0)
        tree.parent = members_breadth_first[(local_index - 1) / fanout];

    collect_subtree(
        members_breadth_first,
        local_index,
        fanout,
        tree.assigned_subtree);
    for (std::size_t offset = 1; offset <= fanout; ++offset)
    {
        const auto child_index = fanout * local_index + offset;
        if (child_index >= members_breadth_first.size())
            break;
        const auto child = members_breadth_first[child_index];
        tree.direct_children.push_back(child);
        std::vector<ReplicaID> child_order;
        std::set<ReplicaID> child_subtree;
        collect_subtree(
            members_breadth_first,
            child_index,
            fanout,
            child_order,
            &child_subtree);
        tree.child_subtrees.emplace(child, std::move(child_subtree));
    }
    derive_frozen_wait_policy(tree, as_set(wait_exempt_leaves));

    ProposalContextMetadata metadata{
        key, std::move(tree), global_quorum};
    return metadata;
}

} // namespace

std::optional<ProposalContextMetadata>
make_exact_proposal_context_metadata(
    const ProposalKey &key,
    ReplicaID local_replica,
    const std::vector<ReplicaID> &members_breadth_first,
    std::uint32_t fanout,
    std::uint32_t pipeline_stretch,
    std::size_t global_quorum)
{
    return make_exact_proposal_context_metadata_impl(
        key,
        local_replica,
        members_breadth_first,
        fanout,
        pipeline_stretch,
        {},
        global_quorum);
}

std::optional<ProposalContextMetadata>
make_exact_proposal_context_metadata(
    const ProposalKey &key,
    ReplicaID local_replica,
    const EpochTreeDefinition &tree,
    std::size_t global_quorum)
{
    return make_exact_proposal_context_metadata_impl(
        key,
        local_replica,
        tree.members_breadth_first,
        tree.fanout,
        tree.pipeline_stretch,
        tree.wait_exempt_leaves,
        global_quorum);
}

struct ProposalContextLifecycle::Entry
{
    struct PendingForwardingCandidate
    {
        quorum_cert_bt certificate;
        std::set<ReplicaID> signers;
    };

    struct ForwardingReservation
    {
        std::set<ReplicaID> signers;
        std::optional<std::uint64_t> pending_candidate_id;
        bool initial_forwarding{false};
    };

    struct InitialForwardingOwner
    {
        quorum_cert_bt certificate;
        std::set<ReplicaID> signers;
    };

    std::shared_ptr<const ProposalTreeSnapshot> tree;
    std::size_t global_quorum{0};
    ProposalContextStatus status{ProposalContextStatus::buffered_future};
    ProposalContextOrigin origin{ProposalContextOrigin::remote};
    std::uint64_t generation{0};
    std::optional<ProposalContextSnapshot> runtime;
    quorum_cert_bt accumulator;
    std::map<std::uint64_t, PendingForwardingCandidate>
        pending_forwarding_candidates;
    std::map<std::uint64_t, ForwardingReservation>
        forwarding_reservations;
    std::optional<InitialForwardingOwner> initial_forwarding_owner;
    std::map<ReplicaID, std::chrono::steady_clock::time_point>
        latency_starts;
    TimerCancellation timer_cancellation;
};

namespace
{

template<typename EntryType>
bool retain_pending_forwarding_candidate(
    EntryType &entry,
    std::uint64_t &next_candidate_id,
    quorum_cert_bt certificate,
    const std::set<ReplicaID> &signers)
{
    if (!entry.tree->parent.has_value() || certificate == nullptr)
        return true;

    const auto assigned = as_set(entry.tree->assigned_subtree);
    if (signers.empty() ||
        !std::includes(
            assigned.begin(), assigned.end(),
            signers.begin(), signers.end()))
        return false;

    std::size_t retained_signers = 0;
    for (const auto &pending : entry.pending_forwarding_candidates)
    {
        retained_signers += pending.second.signers.size();
        if (!set_intersection(pending.second.signers, signers).empty())
            return false;
    }
    if (retained_signers + signers.size() > assigned.size())
        return false;

    auto candidate_id = next_candidate_id++;
    if (candidate_id == 0)
        candidate_id = next_candidate_id++;
    entry.pending_forwarding_candidates.emplace(
        candidate_id,
        typename EntryType::PendingForwardingCandidate{
            std::move(certificate), signers});
    return true;
}

} // namespace

ProposalContextLease::ProposalContextLease(
    ProposalKey key,
    ProposalContextOrigin origin,
    std::uint64_t generation,
    std::shared_ptr<const ProposalTreeSnapshot> tree)
    : key_(std::move(key)),
      origin_(origin),
      generation_(generation),
      tree_(std::move(tree))
{}

const ProposalKey &ProposalContextLease::key() const noexcept
{
    return key_;
}

ProposalContextOrigin ProposalContextLease::origin() const noexcept
{
    return origin_;
}

std::uint64_t ProposalContextLease::generation() const noexcept
{
    return generation_;
}

const ProposalTreeSnapshot &ProposalContextLease::tree() const noexcept
{
    return *tree_;
}

ProposalContextLifecycle::ProposalContextLifecycle() = default;

ProposalContextLifecycle::~ProposalContextLifecycle()
{
    std::vector<TimerCancellation> cancellations;
    {
        std::lock_guard<std::mutex> lock(mutex_);
        cancellations.reserve(entries_.size());
        for (auto &item : entries_)
            if (item.second->timer_cancellation)
                cancellations.push_back(
                    std::move(item.second->timer_cancellation));
        entries_.clear();
        keys_by_block_.clear();
    }
    for (auto &cancel : cancellations)
        run_cancellation(std::move(cancel));
}

ProposalContextStatus ProposalContextLifecycle::context_status(
    const ProposalKey &key) const
{
    std::lock_guard<std::mutex> lock(mutex_);
    if (configuration_is_retired_unlocked(key.configuration))
        return ProposalContextStatus::retired;
    const auto found = entries_.find(key);
    return found == entries_.end()
               ? ProposalContextStatus::unknown
               : found->second->status;
}

std::optional<ProposalContextLease>
ProposalContextLifecycle::acquire_open_context(
    const ProposalKey &key) const
{
    std::lock_guard<std::mutex> lock(mutex_);
    const auto found = entries_.find(key);
    if (found == entries_.end() ||
        found->second->status != ProposalContextStatus::admitted_open ||
        found->second->tree == nullptr ||
        !found->second->runtime.has_value())
        return std::nullopt;
    const auto &entry = *found->second;
    return ProposalContextLease(
        key, entry.origin, entry.generation, entry.tree);
}

bool ProposalContextLifecycle::revalidate(
    const ProposalContextLease &lease) const
{
    std::lock_guard<std::mutex> lock(mutex_);
    const auto found = entries_.find(lease.key());
    return found != entries_.end() &&
           found->second->status == ProposalContextStatus::admitted_open &&
           found->second->generation == lease.generation() &&
           found->second->tree != nullptr &&
           found->second->runtime.has_value();
}

bool ProposalContextLifecycle::buffer_future(
    const ProposalContextMetadata &metadata)
{
    const auto frozen = normalized_metadata(metadata);
    if (!valid_metadata(frozen))
        return false;

    std::lock_guard<std::mutex> lock(mutex_);
    if (configuration_is_retired_unlocked(frozen.key.configuration) ||
        entries_.count(frozen.key) != 0)
        return false;

    auto entry = std::make_unique<Entry>();
    entry->tree = std::make_shared<const ProposalTreeSnapshot>(frozen.tree);
    entry->global_quorum = frozen.global_quorum;
    entry->status = ProposalContextStatus::buffered_future;
    entries_.emplace(frozen.key, std::move(entry));
    index_key_unlocked(frozen.key);
    return true;
}

std::optional<ProposalContextLease> ProposalContextLifecycle::admit_remote(
    const ProposalContextMetadata &metadata)
{
    return admit(metadata, ProposalContextOrigin::remote);
}

std::optional<ProposalContextLease> ProposalContextLifecycle::admit_local(
    const ProposalContextMetadata &metadata)
{
    return admit(metadata, ProposalContextOrigin::leader_local);
}

std::optional<ProposalContextLease> ProposalContextLifecycle::admit(
    const ProposalContextMetadata &metadata,
    ProposalContextOrigin origin)
{
    const auto frozen = normalized_metadata(metadata);
    if (!valid_metadata(frozen))
        return std::nullopt;

    std::lock_guard<std::mutex> lock(mutex_);
    if (configuration_is_retired_unlocked(frozen.key.configuration))
        return std::nullopt;

    auto found = entries_.find(frozen.key);
    if (found != entries_.end())
    {
        auto &entry = *found->second;
        if (entry.status == ProposalContextStatus::terminal_closed ||
            !same_metadata(entry, frozen))
            return std::nullopt;
        if (entry.status == ProposalContextStatus::admitted_open)
        {
            index_key_unlocked(frozen.key);
            return ProposalContextLease(
                frozen.key, entry.origin, entry.generation, entry.tree);
        }

        entry.status = ProposalContextStatus::admitted_open;
        entry.origin = origin;
        entry.generation = next_generation_++;
        entry.runtime.emplace();
        initialize_runtime(*entry.runtime, *entry.tree);
        index_key_unlocked(frozen.key);
        return ProposalContextLease(
            frozen.key, entry.origin, entry.generation, entry.tree);
    }

    auto entry = std::make_unique<Entry>();
    entry->tree = std::make_shared<const ProposalTreeSnapshot>(frozen.tree);
    entry->global_quorum = frozen.global_quorum;
    entry->status = ProposalContextStatus::admitted_open;
    entry->origin = origin;
    entry->generation = next_generation_++;
    entry->runtime.emplace();
    initialize_runtime(*entry->runtime, frozen.tree);
    auto lease = ProposalContextLease(
        frozen.key, origin, entry->generation, entry->tree);
    entries_.emplace(frozen.key, std::move(entry));
    index_key_unlocked(frozen.key);
    return lease;
}

void ProposalContextLifecycle::activate_configuration(
    const ConfigurationId &configuration)
{
    std::lock_guard<std::mutex> lock(mutex_);
    active_configuration_ = configuration;
}

std::optional<ConfigurationId>
ProposalContextLifecycle::active_configuration() const
{
    std::lock_guard<std::mutex> lock(mutex_);
    return active_configuration_;
}

bool ProposalContextLifecycle::close(const ProposalKey &key,
                                     ProposalContextEvent reason)
{
    if (!is_deterministic_terminal(reason))
        return false;

    TimerCancellation cancellation;
    {
        std::lock_guard<std::mutex> lock(mutex_);
        const auto found = entries_.find(key);
        if (found == entries_.end() ||
            found->second->status == ProposalContextStatus::terminal_closed)
            return false;
        cancellation = compact_terminal_unlocked(key, *found->second);
    }
    run_cancellation(std::move(cancellation));
    return true;
}

std::vector<ProposalKey> ProposalContextLifecycle::close_committed_block(
    const uint256_t &block_hash)
{
    std::vector<ProposalKey> closed;
    std::vector<TimerCancellation> cancellations;
    {
        std::lock_guard<std::mutex> lock(mutex_);
        const auto indexed = keys_by_block_.find(block_hash);
        if (indexed == keys_by_block_.end())
            return closed;

        auto keys = std::move(indexed->second);
        keys_by_block_.erase(indexed);
        closed.reserve(keys.size());
        cancellations.reserve(keys.size());
        for (const auto &key : keys)
        {
            const auto found = entries_.find(key);
            if (found == entries_.end())
                continue;

            if (found->second->status ==
                ProposalContextStatus::terminal_closed)
            {
                closed.push_back(key);
                continue;
            }

            auto cancellation =
                compact_terminal_unlocked(key, *found->second);
            closed.push_back(key);
            if (cancellation)
                cancellations.push_back(std::move(cancellation));
        }
    }
    for (auto &cancel : cancellations)
        run_cancellation(std::move(cancel));
    return closed;
}

std::size_t ProposalContextLifecycle::retire_configuration(
    const ConfigurationId &configuration)
{
    std::vector<TimerCancellation> cancellations;
    std::size_t retired = 0;
    {
        std::lock_guard<std::mutex> lock(mutex_);
        if (configuration.epoch_number < first_live_epoch_)
            return 0;
        if (!retired_configurations_.insert(configuration).second)
            return 0;

        for (auto item = entries_.begin(); item != entries_.end();)
        {
            if (item->first.configuration != configuration)
            {
                ++item;
                continue;
            }
            unindex_key_unlocked(item->first);
            if (item->second->timer_cancellation)
                cancellations.push_back(
                    std::move(item->second->timer_cancellation));
            item = entries_.erase(item);
            ++retired;
        }
    }
    for (auto &cancel : cancellations)
        run_cancellation(std::move(cancel));
    return retired;
}

std::size_t ProposalContextLifecycle::advance_retirement_floor(
    std::uint32_t first_live_epoch)
{
    std::vector<TimerCancellation> cancellations;
    std::size_t compacted_tombstones = 0;
    std::size_t compacted_entries = 0;
    {
        std::lock_guard<std::mutex> lock(mutex_);
        if (first_live_epoch <= first_live_epoch_)
            return 0;
        first_live_epoch_ = first_live_epoch;

        for (auto item = retired_configurations_.begin();
             item != retired_configurations_.end();)
        {
            if (item->epoch_number >= first_live_epoch_)
            {
                ++item;
                continue;
            }
            item = retired_configurations_.erase(item);
            ++compacted_tombstones;
        }

        for (auto item = entries_.begin(); item != entries_.end();)
        {
            if (item->first.configuration.epoch_number >= first_live_epoch_)
            {
                ++item;
                continue;
            }
            unindex_key_unlocked(item->first);
            if (item->second->timer_cancellation)
                cancellations.push_back(
                    std::move(item->second->timer_cancellation));
            item = entries_.erase(item);
            ++compacted_entries;
        }

        if (active_configuration_.has_value() &&
            active_configuration_->epoch_number < first_live_epoch_)
            active_configuration_.reset();
    }
    for (auto &cancel : cancellations)
        run_cancellation(std::move(cancel));
    return compacted_tombstones + compacted_entries;
}

bool ProposalContextLifecycle::has_open_context_before_epoch(
    std::uint32_t first_live_epoch) const
{
    std::lock_guard<std::mutex> lock(mutex_);
    return std::any_of(
        entries_.begin(),
        entries_.end(),
        [first_live_epoch](const auto &item) {
            return item.first.configuration.epoch_number <
                       first_live_epoch &&
                   item.second->status ==
                       ProposalContextStatus::admitted_open;
        });
}

bool ProposalContextLifecycle::is_configuration_retired(
    const ConfigurationId &configuration) const
{
    std::lock_guard<std::mutex> lock(mutex_);
    return configuration_is_retired_unlocked(configuration);
}

std::size_t ProposalContextLifecycle::proposal_entry_count(
    const ConfigurationId &configuration) const
{
    std::lock_guard<std::mutex> lock(mutex_);
    return static_cast<std::size_t>(std::count_if(
        entries_.begin(), entries_.end(),
        [&configuration](const auto &item) {
            return item.first.configuration == configuration;
        }));
}

bool ProposalContextLifecycle::mark_child_responded(
    const ProposalContextLease &lease,
    ReplicaID child)
{
    std::lock_guard<std::mutex> lock(mutex_);
    const auto found = entries_.find(lease.key());
    if (found == entries_.end() ||
        found->second->status != ProposalContextStatus::admitted_open ||
        found->second->generation != lease.generation() ||
        !found->second->runtime.has_value() ||
        as_set(found->second->tree->direct_children).count(child) == 0)
        return false;
    const bool first_observation =
        found->second->runtime->pending_observation_children.erase(child) != 0;
    found->second->runtime->pending_children.erase(child);
    return first_observation;
}

bool ProposalContextLifecycle::record_latency_start(
    const ProposalContextLease &lease,
    ReplicaID child)
{
    std::lock_guard<std::mutex> lock(mutex_);
    const auto found = entries_.find(lease.key());
    if (found == entries_.end() ||
        found->second->status != ProposalContextStatus::admitted_open ||
        found->second->generation != lease.generation() ||
        !found->second->runtime.has_value() ||
        as_set(found->second->tree->direct_children).count(child) == 0)
        return false;
    if (!found->second->runtime->latency_started.insert(child).second)
        return false;
    found->second->latency_starts.emplace(
        child, std::chrono::steady_clock::now());
    return true;
}

std::optional<std::uint64_t>
ProposalContextLifecycle::take_latency_us(
    const ProposalContextLease &lease,
    ReplicaID child)
{
    std::lock_guard<std::mutex> lock(mutex_);
    const auto found = entries_.find(lease.key());
    if (found == entries_.end() ||
        found->second->status != ProposalContextStatus::admitted_open ||
        found->second->generation != lease.generation() ||
        !found->second->runtime.has_value())
        return std::nullopt;
    const auto started = found->second->latency_starts.find(child);
    if (started == found->second->latency_starts.end())
        return std::nullopt;
    const auto elapsed = std::chrono::duration_cast<
        std::chrono::microseconds>(
        std::chrono::steady_clock::now() - started->second);
    found->second->latency_starts.erase(started);
    found->second->runtime->latency_started.erase(child);
    return static_cast<std::uint64_t>(elapsed.count());
}

bool ProposalContextLifecycle::initialize_accumulator(
    const ProposalContextLease &lease,
    quorum_cert_bt accumulator)
{
    if (accumulator == nullptr ||
        accumulator->get_proposal_key() != lease.key())
        return false;

    std::set<ReplicaID> signers;
    if (!exact_signer_set(*accumulator, signers) || !signers.empty())
        return false;

    std::lock_guard<std::mutex> lock(mutex_);
    const auto found = entries_.find(lease.key());
    if (found == entries_.end() ||
        found->second->status != ProposalContextStatus::admitted_open ||
        found->second->generation != lease.generation() ||
        !found->second->runtime.has_value())
        return false;
    if (found->second->accumulator != nullptr)
        return found->second->accumulator->get_proposal_key() ==
               lease.key();
    found->second->accumulator = std::move(accumulator);
    return true;
}

bool ProposalContextLifecycle::record_local_part(
    const ProposalContextLease &lease,
    const ReplicaConfig &config,
    ReplicaID signer,
    const PartCert &part,
    quorum_cert_bt forwarding_candidate)
{
    if (part.get_proposal_key() != lease.key())
        return false;
    std::set<ReplicaID> candidate_signers;
    if (forwarding_candidate != nullptr)
    {
        if (forwarding_candidate->get_proposal_key() != lease.key() ||
            !exact_signer_set(*forwarding_candidate, candidate_signers) ||
            candidate_signers != std::set<ReplicaID>{signer})
            return false;
        HOTSTUFF_LOG_INFO(
            "KAURI_LOCAL_PROPOSAL stage=record_verify_begin replica=%u "
            "epoch=%u tree=%u block=%s",
            static_cast<unsigned>(signer),
            lease.key().configuration.epoch_number,
            lease.key().configuration.tree_id,
            lease.key().block_hash.to_hex().c_str());
        const bool verified = forwarding_candidate->verify(config);
        HOTSTUFF_LOG_INFO(
            "KAURI_LOCAL_PROPOSAL stage=record_verify_end replica=%u "
            "epoch=%u tree=%u block=%s verified=%u",
            static_cast<unsigned>(signer),
            lease.key().configuration.epoch_number,
            lease.key().configuration.tree_id,
            lease.key().block_hash.to_hex().c_str(),
            static_cast<unsigned>(verified));
        if (!verified)
            return false;
    }

    HOTSTUFF_LOG_INFO(
        "KAURI_LOCAL_PROPOSAL stage=record_lock_begin replica=%u "
        "epoch=%u tree=%u block=%s",
        static_cast<unsigned>(signer),
        lease.key().configuration.epoch_number,
        lease.key().configuration.tree_id,
        lease.key().block_hash.to_hex().c_str());
    {
        std::lock_guard<std::mutex> lock(mutex_);
        HOTSTUFF_LOG_INFO(
            "KAURI_LOCAL_PROPOSAL stage=record_lock_end replica=%u "
            "epoch=%u tree=%u block=%s",
            static_cast<unsigned>(signer),
            lease.key().configuration.epoch_number,
            lease.key().configuration.tree_id,
            lease.key().block_hash.to_hex().c_str());
        const auto found = entries_.find(lease.key());
        if (found == entries_.end() ||
            found->second->status != ProposalContextStatus::admitted_open ||
            found->second->generation != lease.generation() ||
            !found->second->runtime.has_value() ||
            found->second->accumulator == nullptr ||
            found->second->tree->local_replica != signer ||
            found->second->runtime->verified_signers.count(signer) != 0)
            return false;

        auto expected = found->second->runtime->verified_signers;
        expected.insert(signer);
        quorum_cert_bt next(found->second->accumulator->clone());
        try
        {
            next->add_verified_part(config, signer, part);
        }
        catch (...)
        {
            return false;
        }
        std::set<ReplicaID> next_signers;
        if (!exact_signer_set(*next, next_signers) ||
            next_signers != expected)
            return false;
        if (!retain_pending_forwarding_candidate(
                *found->second,
                next_pending_forwarding_candidate_,
                std::move(forwarding_candidate),
                candidate_signers))
            return false;

        found->second->accumulator = std::move(next);
        found->second->runtime->verified_signers = std::move(expected);
    }
    HOTSTUFF_LOG_INFO(
        "KAURI_LOCAL_PROPOSAL stage=record_return replica=%u epoch=%u "
        "tree=%u block=%s recorded=1",
        static_cast<unsigned>(signer),
        lease.key().configuration.epoch_number,
        lease.key().configuration.tree_id,
        lease.key().block_hash.to_hex().c_str());
    return true;
}

bool ProposalContextLifecycle::record_verified_direct_part(
    const ProposalContextLease &lease,
    const ReplicaConfig &config,
    ReplicaID authenticated_child,
    ReplicaID claimed_voter,
    const PartCert &part,
    quorum_cert_bt forwarding_candidate)
{
    if (part.get_proposal_key() != lease.key() ||
        authenticated_child != claimed_voter)
        return false;
    std::set<ReplicaID> candidate_signers;
    if (forwarding_candidate != nullptr &&
        (forwarding_candidate->get_proposal_key() != lease.key() ||
         !exact_signer_set(*forwarding_candidate, candidate_signers) ||
         candidate_signers != std::set<ReplicaID>{claimed_voter} ||
         !forwarding_candidate->verify(config)))
        return false;

    std::lock_guard<std::mutex> lock(mutex_);
    const auto found = entries_.find(lease.key());
    if (found == entries_.end() ||
        found->second->status != ProposalContextStatus::admitted_open ||
        found->second->generation != lease.generation() ||
        !found->second->runtime.has_value() ||
        found->second->accumulator == nullptr)
        return false;

    const auto subtree =
        found->second->tree->child_subtrees.find(authenticated_child);
    if (subtree == found->second->tree->child_subtrees.end() ||
        subtree->second.count(claimed_voter) == 0 ||
        found->second->runtime->verified_signers.count(claimed_voter) != 0)
        return false;

    auto expected = found->second->runtime->verified_signers;
    expected.insert(claimed_voter);
    quorum_cert_bt next(found->second->accumulator->clone());
    try
    {
        next->add_verified_part(config, claimed_voter, part);
    }
    catch (...)
    {
        return false;
    }
    std::set<ReplicaID> next_signers;
    if (!exact_signer_set(*next, next_signers) ||
        next_signers != expected)
        return false;
    if (!retain_pending_forwarding_candidate(
            *found->second,
            next_pending_forwarding_candidate_,
            std::move(forwarding_candidate),
            candidate_signers))
        return false;

    found->second->accumulator = std::move(next);
    found->second->runtime->verified_signers = std::move(expected);
    mark_observed(*found->second->runtime, authenticated_child);
    update_required_branch_completion(
        *found->second->tree,
        *found->second->runtime,
        authenticated_child);
    return true;
}

bool ProposalContextLifecycle::record_verified_root_fallback_part(
    const ProposalContextLease &lease,
    const ReplicaConfig &config,
    ReplicaID authenticated_sender,
    ReplicaID claimed_voter,
    const PartCert &part,
    quorum_cert_bt verified_candidate)
{
    if (part.get_proposal_key() != lease.key() ||
        authenticated_sender != claimed_voter)
        return false;
    std::set<ReplicaID> candidate_signers;
    if (verified_candidate == nullptr ||
        verified_candidate->get_proposal_key() != lease.key() ||
        !exact_signer_set(*verified_candidate, candidate_signers) ||
        candidate_signers != std::set<ReplicaID>{claimed_voter} ||
        !verified_candidate->verify(config))
        return false;

    std::lock_guard<std::mutex> lock(mutex_);
    const auto found = entries_.find(lease.key());
    if (found == entries_.end() ||
        found->second->status != ProposalContextStatus::admitted_open ||
        found->second->generation != lease.generation() ||
        !found->second->runtime.has_value() ||
        found->second->accumulator == nullptr ||
        found->second->tree == nullptr ||
        found->second->tree->local_replica !=
            found->second->tree->root ||
        found->second->tree->parent.has_value() ||
        as_set(found->second->tree->assigned_subtree).count(
            claimed_voter) == 0 ||
        claimed_voter == found->second->tree->local_replica ||
        found->second->runtime->verified_signers.count(
            claimed_voter) != 0)
        return false;

    auto expected = found->second->runtime->verified_signers;
    expected.insert(claimed_voter);
    quorum_cert_bt next(found->second->accumulator->clone());
    try
    {
        next->add_verified_part(config, claimed_voter, part);
    }
    catch (...)
    {
        return false;
    }
    std::set<ReplicaID> next_signers;
    if (!exact_signer_set(*next, next_signers) ||
        next_signers != expected)
        return false;

    found->second->accumulator = std::move(next);
    found->second->runtime->verified_signers = std::move(expected);
    // A descendant fallback proves only that signer. It must not clear the
    // missing direct-child observation or response-evidence state.
    return true;
}

bool ProposalContextLifecycle::record_verified_aggregate_certificate(
    const ProposalContextLease &lease,
    ReplicaID authenticated_child,
    const QuorumCert &certificate)
{
    return record_verified_aggregate_certificate_with_disposition(
               lease, authenticated_child, certificate) ==
           VerifiedAggregateCertificateDisposition::accepted;
}

VerifiedAggregateCertificateDisposition
ProposalContextLifecycle::
record_verified_aggregate_certificate_with_disposition(
    const ProposalContextLease &lease,
    ReplicaID authenticated_child,
    const QuorumCert &certificate)
{
    if (certificate.get_proposal_key() != lease.key())
        return VerifiedAggregateCertificateDisposition::rejected;
    std::set<ReplicaID> certified_signers;
    if (!exact_signer_set(certificate, certified_signers) ||
        certified_signers.empty())
        return VerifiedAggregateCertificateDisposition::rejected;
    quorum_cert_bt forwarding_candidate;
    try
    {
        // QuorumCert::clone is logically const but predates const-correctness
        // in the crypto interface.
        forwarding_candidate =
            const_cast<QuorumCert &>(certificate).clone();
    }
    catch (...)
    {
        return VerifiedAggregateCertificateDisposition::rejected;
    }

    std::lock_guard<std::mutex> lock(mutex_);
    const auto found = entries_.find(lease.key());
    if (found == entries_.end() ||
        found->second->status != ProposalContextStatus::admitted_open ||
        found->second->generation != lease.generation() ||
        !found->second->runtime.has_value() ||
        found->second->accumulator == nullptr)
        return VerifiedAggregateCertificateDisposition::rejected;

    const auto subtree =
        found->second->tree->child_subtrees.find(authenticated_child);
    if (subtree == found->second->tree->child_subtrees.end())
        return VerifiedAggregateCertificateDisposition::rejected;
    for (const auto signer : certified_signers)
        if (subtree->second.count(signer) == 0)
            return VerifiedAggregateCertificateDisposition::rejected;
    for (const auto signer : certified_signers)
        if (found->second->runtime->verified_signers.count(signer) != 0)
            return VerifiedAggregateCertificateDisposition::redundant;

    auto expected = found->second->runtime->verified_signers;
    expected.insert(
        certified_signers.begin(), certified_signers.end());
    quorum_cert_bt next(found->second->accumulator->clone());
    try
    {
        next->merge_verified_quorum(certificate);
    }
    catch (...)
    {
        return VerifiedAggregateCertificateDisposition::rejected;
    }
    std::set<ReplicaID> next_signers;
    if (!exact_signer_set(*next, next_signers) ||
        next_signers != expected)
        return VerifiedAggregateCertificateDisposition::rejected;
    if (!retain_pending_forwarding_candidate(
            *found->second,
            next_pending_forwarding_candidate_,
            std::move(forwarding_candidate),
            certified_signers))
        return VerifiedAggregateCertificateDisposition::rejected;

    found->second->accumulator = std::move(next);
    found->second->runtime->verified_signers = std::move(expected);
    mark_observed(*found->second->runtime, authenticated_child);
    update_required_branch_completion(
        *found->second->tree,
        *found->second->runtime,
        authenticated_child);
    return VerifiedAggregateCertificateDisposition::accepted;
}

quorum_cert_bt ProposalContextLifecycle::clone_accumulator(
    const ProposalContextLease &lease) const
{
    std::lock_guard<std::mutex> lock(mutex_);
    const auto found = entries_.find(lease.key());
    if (found == entries_.end() ||
        found->second->status != ProposalContextStatus::admitted_open ||
        found->second->generation != lease.generation() ||
        !found->second->runtime.has_value() ||
        found->second->accumulator == nullptr)
        return nullptr;
    return found->second->accumulator->clone();
}

quorum_cert_bt ProposalContextLifecycle::clone_publishable_root_qc(
    const ProposalContextLease &lease) const
{
    std::lock_guard<std::mutex> lock(mutex_);
    const auto found = entries_.find(lease.key());
    if (found == entries_.end() ||
        found->second->status != ProposalContextStatus::admitted_open ||
        found->second->generation != lease.generation() ||
        !found->second->runtime.has_value() ||
        found->second->tree == nullptr ||
        found->second->tree->parent.has_value() ||
        found->second->tree->local_replica !=
            found->second->tree->root ||
        found->second->global_quorum == 0 ||
        found->second->accumulator == nullptr ||
        found->second->accumulator->get_proposal_key() != lease.key())
        return nullptr;

    std::set<ReplicaID> signers;
    if (!exact_signer_set(*found->second->accumulator, signers) ||
        signers != found->second->runtime->verified_signers ||
        signers.size() < found->second->global_quorum)
        return nullptr;
    return found->second->accumulator->clone();
}

bool ProposalContextLifecycle::claim_root_qc_progress(
    const ProposalContextLease &lease)
{
    std::lock_guard<std::mutex> lock(mutex_);
    const auto found = entries_.find(lease.key());
    if (found == entries_.end() ||
        found->second->status != ProposalContextStatus::admitted_open ||
        found->second->generation != lease.generation() ||
        !found->second->runtime.has_value() ||
        found->second->tree == nullptr ||
        found->second->tree != lease.tree_ ||
        found->second->tree->parent.has_value() ||
        found->second->tree->local_replica !=
            found->second->tree->root ||
        found->second->runtime->root_qc_progress_claimed)
        return false;

    found->second->runtime->root_qc_progress_claimed = true;
    return true;
}

bool ProposalContextLifecycle::assigned_subtree_complete(
    const ProposalContextLease &lease) const
{
    std::lock_guard<std::mutex> lock(mutex_);
    const auto found = entries_.find(lease.key());
    if (found == entries_.end() ||
        found->second->status != ProposalContextStatus::admitted_open ||
        found->second->generation != lease.generation() ||
        !found->second->runtime.has_value())
        return false;
    const auto assigned = as_set(found->second->tree->assigned_subtree);
    return std::includes(
        found->second->runtime->verified_signers.begin(),
        found->second->runtime->verified_signers.end(),
        assigned.begin(), assigned.end());
}

bool ProposalContextLifecycle::required_subtree_complete(
    const ProposalContextLease &lease) const
{
    std::lock_guard<std::mutex> lock(mutex_);
    const auto found = entries_.find(lease.key());
    if (found == entries_.end() ||
        found->second->status != ProposalContextStatus::admitted_open ||
        found->second->generation != lease.generation() ||
        !found->second->runtime.has_value())
        return false;
    const auto &required = found->second->tree->required_subtree;
    return std::includes(
        found->second->runtime->verified_signers.begin(),
        found->second->runtime->verified_signers.end(),
        required.begin(), required.end());
}

bool ProposalContextLifecycle::pass_through_enabled(
    const ProposalContextLease &lease) const
{
    std::lock_guard<std::mutex> lock(mutex_);
    const auto found = entries_.find(lease.key());
    return found != entries_.end() &&
           found->second->status == ProposalContextStatus::admitted_open &&
           found->second->generation == lease.generation() &&
           found->second->runtime.has_value() &&
           found->second->runtime->pass_through;
}

bool ProposalContextLifecycle::delta_open_enabled(
    const ProposalContextLease &lease) const
{
    std::lock_guard<std::mutex> lock(mutex_);
    const auto found = entries_.find(lease.key());
    return found != entries_.end() &&
           found->second->status == ProposalContextStatus::admitted_open &&
           found->second->generation == lease.generation() &&
           found->second->runtime.has_value() &&
           found->second->runtime->phase ==
               ProposalContextPhase::delta_open;
}

std::optional<std::set<ReplicaID>>
ProposalContextLifecycle::pending_children(
    const ProposalContextLease &lease) const
{
    std::lock_guard<std::mutex> lock(mutex_);
    const auto found = entries_.find(lease.key());
    if (found == entries_.end() ||
        found->second->status != ProposalContextStatus::admitted_open ||
        found->second->generation != lease.generation() ||
        !found->second->runtime.has_value())
        return std::nullopt;
    return found->second->runtime->pending_observation_children;
}

std::optional<std::set<ReplicaID>>
ProposalContextLifecycle::pending_required_child_branches(
    const ProposalContextLease &lease) const
{
    std::lock_guard<std::mutex> lock(mutex_);
    const auto found = entries_.find(lease.key());
    if (found == entries_.end() ||
        found->second->status != ProposalContextStatus::admitted_open ||
        found->second->generation != lease.generation() ||
        !found->second->runtime.has_value())
        return std::nullopt;
    return found->second->runtime->pending_required_child_branches;
}

std::optional<std::map<ReplicaID, std::set<ReplicaID>>>
ProposalContextLifecycle::missing_required_signers_by_child(
    const ProposalContextLease &lease) const
{
    std::lock_guard<std::mutex> lock(mutex_);
    const auto found = entries_.find(lease.key());
    if (found == entries_.end() ||
        found->second->status != ProposalContextStatus::admitted_open ||
        found->second->generation != lease.generation() ||
        !found->second->runtime.has_value())
        return std::nullopt;

    std::map<ReplicaID, std::set<ReplicaID>> missing;
    for (const auto &branch :
         found->second->tree->required_child_subtrees)
    {
        auto gap = set_difference(
            branch.second,
            found->second->runtime->verified_signers);
        if (!gap.empty())
            missing.emplace(branch.first, std::move(gap));
    }
    return missing;
}

std::optional<std::set<ReplicaID>>
ProposalContextLifecycle::missing_optional_signers(
    const ProposalContextLease &lease) const
{
    std::lock_guard<std::mutex> lock(mutex_);
    const auto found = entries_.find(lease.key());
    if (found == entries_.end() ||
        found->second->status != ProposalContextStatus::admitted_open ||
        found->second->generation != lease.generation() ||
        !found->second->runtime.has_value())
        return std::nullopt;
    return set_difference(
        found->second->tree->optional_subtree,
        found->second->runtime->verified_signers);
}

std::optional<std::set<ReplicaID>>
ProposalContextLifecycle::pending_optional_direct_children(
    const ProposalContextLease &lease) const
{
    std::lock_guard<std::mutex> lock(mutex_);
    const auto found = entries_.find(lease.key());
    if (found == entries_.end() ||
        found->second->status != ProposalContextStatus::admitted_open ||
        found->second->generation != lease.generation() ||
        !found->second->runtime.has_value())
        return std::nullopt;
    return set_intersection(
        found->second->runtime->pending_observation_children,
        found->second->tree->optional_subtree);
}

std::optional<std::size_t>
ProposalContextLifecycle::frozen_global_quorum(
    const ProposalContextLease &lease) const
{
    std::lock_guard<std::mutex> lock(mutex_);
    const auto found = entries_.find(lease.key());
    if (found == entries_.end() ||
        found->second->status != ProposalContextStatus::admitted_open ||
        found->second->generation != lease.generation() ||
        !found->second->runtime.has_value())
        return std::nullopt;
    return found->second->global_quorum;
}

bool ProposalContextLifecycle::record_local_signer(
    const ProposalContextLease &lease)
{
    std::lock_guard<std::mutex> lock(mutex_);
    const auto found = entries_.find(lease.key());
    if (found == entries_.end() ||
        found->second->status != ProposalContextStatus::admitted_open ||
        found->second->generation != lease.generation() ||
        !found->second->runtime.has_value())
        return false;
    return found->second->runtime->verified_signers.insert(
        found->second->tree->local_replica).second;
}

bool ProposalContextLifecycle::record_verified_direct(
    const ProposalContextLease &lease,
    ReplicaID authenticated_child,
    ReplicaID claimed_voter)
{
    std::lock_guard<std::mutex> lock(mutex_);
    const auto found = entries_.find(lease.key());
    if (found == entries_.end() ||
        found->second->status != ProposalContextStatus::admitted_open ||
        found->second->generation != lease.generation() ||
        !found->second->runtime.has_value() ||
        authenticated_child != claimed_voter)
        return false;

    const auto subtree =
        found->second->tree->child_subtrees.find(authenticated_child);
    if (subtree == found->second->tree->child_subtrees.end() ||
        subtree->second.count(claimed_voter) == 0 ||
        found->second->runtime->verified_signers.count(claimed_voter) != 0)
        return false;

    found->second->runtime->verified_signers.insert(claimed_voter);
    mark_observed(*found->second->runtime, authenticated_child);
    update_required_branch_completion(
        *found->second->tree,
        *found->second->runtime,
        authenticated_child);
    return true;
}

bool ProposalContextLifecycle::record_verified_aggregate(
    const ProposalContextLease &lease,
    ReplicaID authenticated_child,
    const std::set<ReplicaID> &certified_signers)
{
    if (certified_signers.empty())
        return false;

    std::lock_guard<std::mutex> lock(mutex_);
    const auto found = entries_.find(lease.key());
    if (found == entries_.end() ||
        found->second->status != ProposalContextStatus::admitted_open ||
        found->second->generation != lease.generation() ||
        !found->second->runtime.has_value())
        return false;

    const auto subtree =
        found->second->tree->child_subtrees.find(authenticated_child);
    if (subtree == found->second->tree->child_subtrees.end())
        return false;
    for (const auto signer : certified_signers)
        if (subtree->second.count(signer) == 0 ||
            found->second->runtime->verified_signers.count(signer) != 0)
            return false;

    found->second->runtime->verified_signers.insert(
        certified_signers.begin(), certified_signers.end());
    mark_observed(*found->second->runtime, authenticated_child);
    update_required_branch_completion(
        *found->second->tree,
        *found->second->runtime,
        authenticated_child);
    return true;
}

bool ProposalContextLifecycle::mark_forwarded_signers(
    const ProposalContextLease &lease,
    const std::set<ReplicaID> &certified_signers)
{
    if (certified_signers.empty())
        return false;

    std::lock_guard<std::mutex> lock(mutex_);
    const auto found = entries_.find(lease.key());
    if (found == entries_.end() ||
        found->second->status != ProposalContextStatus::admitted_open ||
        found->second->generation != lease.generation() ||
        !found->second->runtime.has_value() ||
        found->second->initial_forwarding_owner.has_value())
        return false;
    for (const auto signer : certified_signers)
        if (found->second->runtime->verified_signers.count(signer) == 0 ||
            found->second->runtime->reserved_signers.count(signer) != 0 ||
            found->second->runtime->forwarded_signers.count(signer) != 0)
            return false;

    found->second->runtime->forwarded_signers.insert(
        certified_signers.begin(), certified_signers.end());
    return true;
}

std::optional<ProposalForwardingClaim>
ProposalContextLifecycle::claim_initial_certificate_reservation(
    const ProposalContextLease &lease,
    quorum_cert_bt candidate)
{
    if (candidate == nullptr ||
        candidate->get_proposal_key() != lease.key())
        return std::nullopt;

    std::set<ReplicaID> signers;
    if (!exact_signer_set(*candidate, signers) || signers.empty())
        return std::nullopt;
    quorum_cert_bt owned_certificate;
    try
    {
        owned_certificate = candidate->clone();
    }
    catch (...)
    {
        return std::nullopt;
    }

    std::lock_guard<std::mutex> lock(mutex_);
    const auto found = entries_.find(lease.key());
    if (found == entries_.end() ||
        found->second->status != ProposalContextStatus::admitted_open ||
        found->second->generation != lease.generation() ||
        !found->second->runtime.has_value() ||
        !found->second->tree->parent.has_value() ||
        found->second->runtime->phase != ProposalContextPhase::collecting ||
        found->second->initial_forwarding_owner.has_value() ||
        !found->second->forwarding_reservations.empty() ||
        !found->second->runtime->reserved_signers.empty() ||
        !found->second->runtime->forwarded_signers.empty() ||
        signers != found->second->runtime->verified_signers)
        return std::nullopt;

    auto reservation_id = next_forwarding_reservation_++;
    if (reservation_id == 0)
        reservation_id = next_forwarding_reservation_++;
    std::optional<std::uint64_t> pending_candidate_id;
    for (const auto &pending :
         found->second->pending_forwarding_candidates)
        if (pending.second.signers == signers)
        {
            pending_candidate_id = pending.first;
            break;
        }
    found->second->initial_forwarding_owner =
        Entry::InitialForwardingOwner{
            std::move(owned_certificate), signers};
    found->second->runtime->initial_forwarding_signers = signers;
    found->second->runtime->reserved_signers.insert(
        signers.begin(), signers.end());
    found->second->forwarding_reservations.emplace(
        reservation_id,
        Entry::ForwardingReservation{
            signers, pending_candidate_id, true});
    return ProposalForwardingClaim{
        std::move(candidate), std::move(signers), reservation_id,
        pending_candidate_id};
}

std::optional<ProposalForwardingClaim>
ProposalContextLifecycle::claim_initial_forwarding_reservation(
    const ProposalContextLease &lease)
{
    std::lock_guard<std::mutex> lock(mutex_);
    const auto found = entries_.find(lease.key());
    if (found == entries_.end() ||
        found->second->status != ProposalContextStatus::admitted_open ||
        found->second->generation != lease.generation() ||
        !found->second->runtime.has_value() ||
        !found->second->initial_forwarding_owner.has_value())
        return std::nullopt;

    const auto &owner = *found->second->initial_forwarding_owner;
    for (const auto signer : owner.signers)
        if (found->second->runtime->verified_signers.count(signer) == 0 ||
            found->second->runtime->reserved_signers.count(signer) != 0 ||
            found->second->runtime->forwarded_signers.count(signer) != 0)
            return std::nullopt;

    quorum_cert_bt certificate;
    try
    {
        certificate = owner.certificate->clone();
    }
    catch (...)
    {
        return std::nullopt;
    }
    auto reservation_id = next_forwarding_reservation_++;
    if (reservation_id == 0)
        reservation_id = next_forwarding_reservation_++;
    std::optional<std::uint64_t> pending_candidate_id;
    for (const auto &pending :
         found->second->pending_forwarding_candidates)
        if (pending.second.signers == owner.signers)
        {
            pending_candidate_id = pending.first;
            break;
        }
    const auto signers = owner.signers;
    found->second->runtime->reserved_signers.insert(
        signers.begin(), signers.end());
    found->second->forwarding_reservations.emplace(
        reservation_id,
        Entry::ForwardingReservation{
            signers, pending_candidate_id, true});
    return ProposalForwardingClaim{
        std::move(certificate), signers, reservation_id,
        pending_candidate_id};
}

bool ProposalContextLifecycle::initial_forwarding_owned(
    const ProposalContextLease &lease) const
{
    std::lock_guard<std::mutex> lock(mutex_);
    const auto found = entries_.find(lease.key());
    return found != entries_.end() &&
           found->second->status ==
               ProposalContextStatus::admitted_open &&
           found->second->generation == lease.generation() &&
           found->second->runtime.has_value() &&
           found->second->initial_forwarding_owner.has_value();
}

bool ProposalContextLifecycle::retire_overlapped_initial_forwarding(
    const ProposalContextLease &lease,
    const std::set<ReplicaID> &expected_signers)
{
    if (expected_signers.empty())
        return false;

    std::lock_guard<std::mutex> lock(mutex_);
    const auto found = entries_.find(lease.key());
    if (found == entries_.end() ||
        found->second->status != ProposalContextStatus::admitted_open ||
        found->second->generation != lease.generation() ||
        !found->second->runtime.has_value() ||
        found->second->runtime->phase != ProposalContextPhase::delta_open)
        return false;

    if (found->second->initial_forwarding_owner.has_value() &&
        found->second->initial_forwarding_owner->signers !=
            expected_signers)
        return false;
    for (const auto &reservation : found->second->forwarding_reservations)
        if (reservation.second.initial_forwarding)
            return false;
    const auto already_forwarded = set_intersection(
        expected_signers,
        found->second->runtime->forwarded_signers);
    const auto uncovered = set_difference(
        expected_signers,
        found->second->runtime->forwarded_signers);
    if (already_forwarded.empty() || uncovered.empty())
        return false;

    std::set<ReplicaID> canonically_owned;
    for (const auto &pending :
         found->second->pending_forwarding_candidates)
        canonically_owned.insert(
            pending.second.signers.begin(), pending.second.signers.end());
    for (const auto &reservation : found->second->forwarding_reservations)
        canonically_owned.insert(
            reservation.second.signers.begin(),
            reservation.second.signers.end());
    if (!std::includes(
            canonically_owned.begin(), canonically_owned.end(),
            uncovered.begin(), uncovered.end()))
        return false;

    if (found->second->initial_forwarding_owner.has_value())
    {
        found->second->initial_forwarding_owner.reset();
        found->second->runtime->initial_forwarding_signers.clear();
    }
    return true;
}

std::optional<ProposalForwardingClaim>
ProposalContextLifecycle::claim_unforwarded_certificate_reservation(
    const ProposalContextLease &lease,
    quorum_cert_bt candidate)
{
    if (candidate == nullptr ||
        candidate->get_proposal_key() != lease.key())
        return std::nullopt;

    std::set<ReplicaID> signers;
    if (!exact_signer_set(*candidate, signers) || signers.empty())
        return std::nullopt;

    std::lock_guard<std::mutex> lock(mutex_);
    const auto found = entries_.find(lease.key());
    if (found == entries_.end() ||
        found->second->status != ProposalContextStatus::admitted_open ||
        found->second->generation != lease.generation() ||
        !found->second->runtime.has_value() ||
        found->second->initial_forwarding_owner.has_value())
        return std::nullopt;

    for (const auto signer : signers)
        if (found->second->runtime->verified_signers.count(signer) == 0 ||
            found->second->runtime->reserved_signers.count(signer) != 0 ||
            found->second->runtime->forwarded_signers.count(signer) != 0)
            return std::nullopt;

    auto reservation_id = next_forwarding_reservation_++;
    if (reservation_id == 0)
        reservation_id = next_forwarding_reservation_++;
    std::optional<std::uint64_t> pending_candidate_id;
    for (const auto &pending :
         found->second->pending_forwarding_candidates)
        if (pending.second.signers == signers)
        {
            pending_candidate_id = pending.first;
            break;
        }
    found->second->runtime->reserved_signers.insert(
        signers.begin(), signers.end());
    found->second->forwarding_reservations.emplace(
        reservation_id,
        Entry::ForwardingReservation{
            signers, pending_candidate_id, false});
    return ProposalForwardingClaim{
        std::move(candidate), std::move(signers), reservation_id,
        pending_candidate_id};
}

std::vector<std::uint64_t>
ProposalContextLifecycle::pending_forwarding_candidate_ids(
    const ProposalContextLease &lease) const
{
    std::lock_guard<std::mutex> lock(mutex_);
    const auto found = entries_.find(lease.key());
    if (found == entries_.end() ||
        found->second->status != ProposalContextStatus::admitted_open ||
        found->second->generation != lease.generation() ||
        !found->second->runtime.has_value())
        return {};

    std::vector<std::uint64_t> ids;
    ids.reserve(found->second->pending_forwarding_candidates.size());
    for (const auto &pending :
         found->second->pending_forwarding_candidates)
        ids.push_back(pending.first);
    return ids;
}

std::optional<ProposalForwardingClaim>
ProposalContextLifecycle::claim_pending_certificate_reservation(
    const ProposalContextLease &lease,
    std::uint64_t pending_candidate_id)
{
    if (pending_candidate_id == 0)
        return std::nullopt;

    std::lock_guard<std::mutex> lock(mutex_);
    const auto found = entries_.find(lease.key());
    if (found == entries_.end() ||
        found->second->status != ProposalContextStatus::admitted_open ||
        found->second->generation != lease.generation() ||
        !found->second->runtime.has_value() ||
        found->second->initial_forwarding_owner.has_value())
        return std::nullopt;
    const auto pending =
        found->second->pending_forwarding_candidates.find(
            pending_candidate_id);
    if (pending ==
        found->second->pending_forwarding_candidates.end())
        return std::nullopt;
    for (const auto signer : pending->second.signers)
        if (found->second->runtime->verified_signers.count(signer) == 0 ||
            found->second->runtime->reserved_signers.count(signer) != 0 ||
            found->second->runtime->forwarded_signers.count(signer) != 0)
            return std::nullopt;

    auto reservation_id = next_forwarding_reservation_++;
    if (reservation_id == 0)
        reservation_id = next_forwarding_reservation_++;
    auto certificate = pending->second.certificate->clone();
    auto signers = pending->second.signers;
    found->second->runtime->reserved_signers.insert(
        signers.begin(), signers.end());
    found->second->forwarding_reservations.emplace(
        reservation_id,
        Entry::ForwardingReservation{
            signers, pending_candidate_id, false});
    return ProposalForwardingClaim{
        std::move(certificate), std::move(signers), reservation_id,
        pending_candidate_id};
}

bool ProposalContextLifecycle::commit_forwarding_claim(
    const ProposalContextLease &lease,
    std::uint64_t reservation_id)
{
    if (reservation_id == 0)
        return false;

    std::lock_guard<std::mutex> lock(mutex_);
    const auto found = entries_.find(lease.key());
    if (found == entries_.end() ||
        found->second->status != ProposalContextStatus::admitted_open ||
        found->second->generation != lease.generation() ||
        !found->second->runtime.has_value())
        return false;
    const auto reservation =
        found->second->forwarding_reservations.find(reservation_id);
    if (reservation == found->second->forwarding_reservations.end())
        return false;
    const auto reserved = reservation->second;
    if (reserved.initial_forwarding &&
        (!found->second->initial_forwarding_owner.has_value() ||
         found->second->initial_forwarding_owner->signers !=
             reserved.signers))
        return false;
    for (const auto signer : reserved.signers)
        if (found->second->runtime->reserved_signers.count(signer) == 0 ||
            found->second->runtime->forwarded_signers.count(signer) != 0)
            return false;

    for (const auto signer : reserved.signers)
        found->second->runtime->reserved_signers.erase(signer);
    found->second->runtime->forwarded_signers.insert(
        reserved.signers.begin(), reserved.signers.end());
    found->second->forwarding_reservations.erase(reservation);
    if (reserved.initial_forwarding)
    {
        found->second->initial_forwarding_owner.reset();
        found->second->runtime->initial_forwarding_signers.clear();
    }
    for (auto pending =
             found->second->pending_forwarding_candidates.begin();
         pending != found->second->pending_forwarding_candidates.end();)
    {
        if (std::includes(
                found->second->runtime->forwarded_signers.begin(),
                found->second->runtime->forwarded_signers.end(),
                pending->second.signers.begin(),
                pending->second.signers.end()))
            pending = found->second->pending_forwarding_candidates.erase(
                pending);
        else
            ++pending;
    }
    return true;
}

bool ProposalContextLifecycle::release_forwarding_claim(
    const ProposalContextLease &lease,
    std::uint64_t reservation_id)
{
    if (reservation_id == 0)
        return false;

    std::lock_guard<std::mutex> lock(mutex_);
    const auto found = entries_.find(lease.key());
    if (found == entries_.end() ||
        found->second->status != ProposalContextStatus::admitted_open ||
        found->second->generation != lease.generation() ||
        !found->second->runtime.has_value())
        return false;
    const auto reservation =
        found->second->forwarding_reservations.find(reservation_id);
    if (reservation == found->second->forwarding_reservations.end())
        return false;

    for (const auto signer : reservation->second.signers)
        found->second->runtime->reserved_signers.erase(signer);
    found->second->forwarding_reservations.erase(reservation);
    return true;
}

std::optional<ProposalForwardingClaim>
ProposalContextLifecycle::claim_unforwarded_certificate(
    const ProposalContextLease &lease,
    quorum_cert_bt candidate)
{
    auto claim = claim_unforwarded_certificate_reservation(
        lease, std::move(candidate));
    if (!claim.has_value() ||
        !commit_forwarding_claim(lease, claim->reservation_id))
        return std::nullopt;
    return claim;
}

std::uint64_t ProposalContextLifecycle::arm_timer(
    const ProposalContextLease &lease,
    TimerCancellation cancel)
{
    TimerCancellation previous;
    std::uint64_t generation = 0;
    bool installed = false;
    {
        std::lock_guard<std::mutex> lock(mutex_);
        const auto found = entries_.find(lease.key());
        if (found == entries_.end() ||
            found->second->status != ProposalContextStatus::admitted_open ||
            found->second->generation != lease.generation() ||
            !found->second->runtime.has_value())
        {
            installed = false;
        }
        else
        {
            auto &entry = *found->second;
            previous = std::move(entry.timer_cancellation);
            generation = next_timer_generation_++;
            entry.runtime->timer_generation = generation;
            entry.timer_cancellation = std::move(cancel);
            installed = true;
        }
    }
    if (!installed)
    {
        run_cancellation(std::move(cancel));
        return 0;
    }

    run_cancellation(std::move(previous));

    std::lock_guard<std::mutex> lock(mutex_);
    const auto found = entries_.find(lease.key());
    if (found == entries_.end() ||
        found->second->status != ProposalContextStatus::admitted_open ||
        found->second->generation != lease.generation() ||
        !found->second->runtime.has_value() ||
        found->second->runtime->timer_generation != generation)
        return 0;
    return generation;
}

bool ProposalContextLifecycle::dispatch_timer(
    const ProposalKey &key,
    std::uint64_t timer_generation,
    const TimerCallback &callback)
{
    std::optional<ProposalContextLease> lease;
    TimerCancellation release_owner;
    {
        std::lock_guard<std::mutex> lock(mutex_);
        const auto found = entries_.find(key);
        if (found == entries_.end() ||
            found->second->status != ProposalContextStatus::admitted_open ||
            !found->second->runtime.has_value() ||
            found->second->runtime->timer_generation != timer_generation ||
            timer_generation == 0)
            return false;
        auto &entry = *found->second;
        entry.runtime->timer_generation = 0;
        release_owner = std::move(entry.timer_cancellation);
        lease = ProposalContextLease(
            key, entry.origin, entry.generation, entry.tree);
    }

    // A fired timer releases its owner without invoking the cancellation hook.
    release_owner = TimerCancellation();
    {
        std::lock_guard<std::mutex> lock(mutex_);
        const auto found = entries_.find(key);
        if (found == entries_.end() ||
            found->second->status != ProposalContextStatus::admitted_open ||
            found->second->generation != lease->generation() ||
            !found->second->runtime.has_value() ||
            found->second->runtime->timer_generation != 0)
            return false;
    }
    callback(*lease);
    return true;
}

ProposalTransitionResult ProposalContextLifecycle::transition(
    const ProposalContextLease &lease,
    ProposalContextEvent event)
{
    TimerCancellation cancellation;
    ProposalTransitionResult result = ProposalTransitionResult::retained_open;
    {
        std::lock_guard<std::mutex> lock(mutex_);
        const auto found = entries_.find(lease.key());
        if (found == entries_.end() ||
            found->second->status != ProposalContextStatus::admitted_open ||
            found->second->generation != lease.generation() ||
            !found->second->runtime.has_value() ||
            found->second->tree == nullptr)
            return ProposalTransitionResult::stale_lease;

        auto &entry = *found->second;
        bool terminal = false;
        const auto enter_delta_open = [&entry, &cancellation]() {
            entry.runtime->phase = ProposalContextPhase::delta_open;
            entry.runtime->pass_through = true;
            // Preserve the original deadline when optional signers exist so
            // their absence can be recorded as neutral observation telemetry.
            if (entry.tree->optional_subtree.empty())
            {
                entry.runtime->timer_generation = 0;
                if (entry.timer_cancellation)
                    cancellation = std::move(entry.timer_cancellation);
            }
        };
        switch (event)
        {
        case ProposalContextEvent::aggregation_timeout:
            entry.runtime->pass_through = true;
            if (entry.tree->parent.has_value())
                entry.runtime->phase = ProposalContextPhase::delta_open;
            break;
        case ProposalContextEvent::late_contribution_forwarded:
        {
            const auto assigned = as_set(entry.tree->assigned_subtree);
            terminal = entry.tree->parent.has_value() &&
                       std::includes(
                           entry.runtime->forwarded_signers.begin(),
                           entry.runtime->forwarded_signers.end(),
                           assigned.begin(), assigned.end());
            if (entry.tree->parent.has_value() && !terminal)
                enter_delta_open();
            break;
        }
        case ProposalContextEvent::leaf_vote_enqueued:
            terminal = entry.tree->direct_children.empty() &&
                       entry.tree->parent.has_value() &&
                       entry.runtime->verified_signers.count(
                           entry.tree->local_replica) != 0;
            break;
        case ProposalContextEvent::non_root_aggregate_enqueued:
        {
            if (entry.accumulator == nullptr &&
                entry.forwarding_reservations.empty())
            {
                // The non-cryptographic lifecycle compatibility API models
                // this event as the enqueue boundary itself. Production exact
                // contexts always own an accumulator and commit forwarded
                // signer ownership before emitting the event.
                entry.runtime->forwarded_signers =
                    entry.runtime->verified_signers;
            }
            const auto assigned = as_set(entry.tree->assigned_subtree);
            terminal = entry.tree->parent.has_value() &&
                       !entry.tree->direct_children.empty() &&
                       std::includes(
                           entry.runtime->forwarded_signers.begin(),
                           entry.runtime->forwarded_signers.end(),
                           assigned.begin(), assigned.end());
            if (entry.tree->parent.has_value() && !terminal)
                enter_delta_open();
            break;
        }
        case ProposalContextEvent::root_qc_published:
            terminal = !entry.tree->parent.has_value() &&
                       entry.tree->local_replica == entry.tree->root &&
                       entry.runtime->verified_signers.size() >=
                           entry.global_quorum;
            break;
        case ProposalContextEvent::proposal_aborted:
        case ProposalContextEvent::committed:
        case ProposalContextEvent::shutdown:
            terminal = true;
            break;
        }

        if (terminal)
        {
            cancellation = compact_terminal_unlocked(
                lease.key(), entry);
            result = ProposalTransitionResult::terminal_closed;
        }
    }
    run_cancellation(std::move(cancellation));
    return result;
}

std::optional<ProposalContextSnapshot> ProposalContextLifecycle::snapshot(
    const ProposalKey &key) const
{
    std::lock_guard<std::mutex> lock(mutex_);
    const auto found = entries_.find(key);
    if (found == entries_.end() ||
        !found->second->runtime.has_value())
        return std::nullopt;
    return *found->second->runtime;
}

bool ProposalContextLifecycle::valid_metadata(
    const ProposalContextMetadata &metadata)
{
    if (is_zero(metadata.key.configuration.epoch_digest) ||
        is_zero(metadata.key.block_hash) || metadata.tree.fanout == 0 ||
        metadata.global_quorum == 0)
        return false;

    const auto assigned = as_set(metadata.tree.assigned_subtree);
    const auto children = as_set(metadata.tree.direct_children);
    const auto &required = metadata.tree.required_subtree;
    const auto &optional = metadata.tree.optional_subtree;
    const bool local_is_root =
        metadata.tree.local_replica == metadata.tree.root;
    if (assigned.size() != metadata.tree.assigned_subtree.size() ||
        children.size() != metadata.tree.direct_children.size() ||
        assigned.count(metadata.tree.local_replica) == 0 ||
        local_is_root !=
            !metadata.tree.parent.has_value() ||
        metadata.tree.child_subtrees.size() != children.size() ||
        metadata.tree.required_child_subtrees.size() != children.size())
        return false;

    const auto required_optional_overlap =
        set_intersection(required, optional);
    auto reconstructed_policy = required;
    reconstructed_policy.insert(optional.begin(), optional.end());
    if (!required_optional_overlap.empty() ||
        reconstructed_policy != assigned ||
        (!metadata.tree.direct_children.empty() &&
         optional.count(metadata.tree.local_replica) != 0))
        return false;

    if (local_is_root)
    {
        // This lifecycle is shared by legacy/adaptive-v1 contexts, whose
        // historical membership is not required to be exactly 3f+1.
        // Adaptive-v2 enforces exact membership when validating its epoch
        // definition before this frozen proposal metadata is constructed.
        const auto authoritative_quorum =
            2 * ((assigned.size() - 1) / 3) + 1;
        if (metadata.global_quorum != authoritative_quorum)
            return false;
    }

    if (metadata.tree.parent.has_value())
    {
        const auto parent = *metadata.tree.parent;
        if (parent == metadata.tree.local_replica ||
            assigned.count(parent) != 0 ||
            assigned.count(metadata.tree.root) != 0)
            return false;
    }

    std::set<ReplicaID> reconstructed{metadata.tree.local_replica};
    for (const auto child : children)
    {
        const auto subtree = metadata.tree.child_subtrees.find(child);
        const auto required_branch =
            metadata.tree.required_child_subtrees.find(child);
        if (subtree == metadata.tree.child_subtrees.end() ||
            required_branch ==
                metadata.tree.required_child_subtrees.end() ||
            subtree->second.empty() || subtree->second.count(child) == 0 ||
            required_branch->second !=
                set_intersection(subtree->second, required) ||
            (optional.count(child) != 0 && subtree->second.size() != 1))
            return false;
        for (const auto member : subtree->second)
        {
            if (member == metadata.tree.local_replica ||
                assigned.count(member) == 0 ||
                !reconstructed.insert(member).second)
                return false;
        }
    }
    return reconstructed == assigned;
}

bool ProposalContextLifecycle::same_metadata(
    const Entry &entry,
    const ProposalContextMetadata &metadata)
{
    return entry.tree != nullptr &&
           entry.global_quorum == metadata.global_quorum &&
           tree_equal(*entry.tree, metadata.tree);
}

ProposalContextLifecycle::TimerCancellation
ProposalContextLifecycle::compact_terminal_unlocked(
    const ProposalKey &key,
    Entry &entry)
{
    auto cancellation = std::move(entry.timer_cancellation);
    entry.status = ProposalContextStatus::terminal_closed;
    entry.tree.reset();
    entry.global_quorum = 0;
    entry.runtime.reset();
    entry.accumulator = nullptr;
    entry.pending_forwarding_candidates.clear();
    entry.forwarding_reservations.clear();
    entry.initial_forwarding_owner.reset();
    entry.latency_starts.clear();
    return cancellation;
}

void ProposalContextLifecycle::index_key_unlocked(
    const ProposalKey &key)
{
    keys_by_block_[key.block_hash].insert(key);
}

void ProposalContextLifecycle::unindex_key_unlocked(
    const ProposalKey &key)
{
    const auto indexed = keys_by_block_.find(key.block_hash);
    if (indexed == keys_by_block_.end())
        return;
    indexed->second.erase(key);
    if (indexed->second.empty())
        keys_by_block_.erase(indexed);
}

bool ProposalContextLifecycle::configuration_is_retired_unlocked(
    const ConfigurationId &configuration) const
{
    return configuration.epoch_number < first_live_epoch_ ||
           retired_configurations_.count(configuration) != 0;
}

ProposalContextStorageStats ProposalContextLifecycle::storage_stats() const
{
    std::lock_guard<std::mutex> lock(mutex_);
    ProposalContextStorageStats stats;
    stats.retired_configuration_tombstones =
        retired_configurations_.size();
    for (const auto &item : entries_)
    {
        const auto &entry = *item.second;
        if (entry.tree != nullptr)
            ++stats.retained_tree_snapshots;
        if (entry.runtime.has_value())
            ++stats.retained_runtime_states;
        if (entry.accumulator != nullptr)
            ++stats.retained_accumulators;
        stats.retained_latency_entries += entry.latency_starts.size();
        if (entry.status == ProposalContextStatus::terminal_closed)
            ++stats.terminal_tombstones;
    }
    return stats;
}

void ProposalContextLifecycle::shutdown()
{
    std::vector<TimerCancellation> cancellations;
    {
        std::lock_guard<std::mutex> lock(mutex_);
        cancellations.reserve(entries_.size());
        for (auto &item : entries_)
        {
            if (item.second->status ==
                ProposalContextStatus::terminal_closed)
                continue;
            auto cancellation = compact_terminal_unlocked(
                item.first, *item.second);
            if (cancellation)
                cancellations.push_back(std::move(cancellation));
        }
        keys_by_block_.clear();
    }
    for (auto &cancel : cancellations)
        run_cancellation(std::move(cancel));
}

void ProposalContextLifecycle::run_cancellation(
    TimerCancellation cancel) noexcept
{
    if (!cancel)
        return;
    try
    {
        cancel();
    }
    catch (...)
    {
    }
}

} // namespace hotstuff
