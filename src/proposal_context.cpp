#include "hotstuff/proposal_context.h"

#include <algorithm>
#include <chrono>
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
           left.fanout == right.fanout &&
           left.pipeline_stretch == right.pipeline_stretch;
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
    if (members_breadth_first.empty() || fanout == 0 ||
        global_quorum == 0 ||
        as_set(members_breadth_first).size() !=
            members_breadth_first.size())
        return std::nullopt;

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

    ProposalContextMetadata metadata{
        key, std::move(tree), global_quorum};
    return metadata;
}

struct ProposalContextLifecycle::Entry
{
    std::shared_ptr<const ProposalTreeSnapshot> tree;
    std::size_t global_quorum{0};
    ProposalContextStatus status{ProposalContextStatus::buffered_future};
    ProposalContextOrigin origin{ProposalContextOrigin::remote};
    std::uint64_t generation{0};
    std::optional<ProposalContextSnapshot> runtime;
    quorum_cert_bt accumulator;
    std::map<ReplicaID, std::chrono::steady_clock::time_point>
        latency_starts;
    TimerCancellation timer_cancellation;
};

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
    if (!valid_metadata(metadata))
        return false;

    std::lock_guard<std::mutex> lock(mutex_);
    if (configuration_is_retired_unlocked(metadata.key.configuration) ||
        entries_.count(metadata.key) != 0)
        return false;

    auto entry = std::make_unique<Entry>();
    entry->tree = std::make_shared<const ProposalTreeSnapshot>(metadata.tree);
    entry->global_quorum = metadata.global_quorum;
    entry->status = ProposalContextStatus::buffered_future;
    entries_.emplace(metadata.key, std::move(entry));
    index_key_unlocked(metadata.key);
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
    if (!valid_metadata(metadata))
        return std::nullopt;

    std::lock_guard<std::mutex> lock(mutex_);
    if (configuration_is_retired_unlocked(metadata.key.configuration))
        return std::nullopt;

    auto found = entries_.find(metadata.key);
    if (found != entries_.end())
    {
        auto &entry = *found->second;
        if (entry.status == ProposalContextStatus::terminal_closed ||
            !same_metadata(entry, metadata))
            return std::nullopt;
        if (entry.status == ProposalContextStatus::admitted_open)
        {
            index_key_unlocked(metadata.key);
            return ProposalContextLease(
                metadata.key, entry.origin, entry.generation, entry.tree);
        }

        entry.status = ProposalContextStatus::admitted_open;
        entry.origin = origin;
        entry.generation = next_generation_++;
        entry.runtime.emplace();
        entry.runtime->pending_children =
            as_set(entry.tree->direct_children);
        index_key_unlocked(metadata.key);
        return ProposalContextLease(
            metadata.key, entry.origin, entry.generation, entry.tree);
    }

    auto entry = std::make_unique<Entry>();
    entry->tree = std::make_shared<const ProposalTreeSnapshot>(metadata.tree);
    entry->global_quorum = metadata.global_quorum;
    entry->status = ProposalContextStatus::admitted_open;
    entry->origin = origin;
    entry->generation = next_generation_++;
    entry->runtime.emplace();
    entry->runtime->pending_children = as_set(metadata.tree.direct_children);
    auto lease = ProposalContextLease(
        metadata.key, origin, entry->generation, entry->tree);
    entries_.emplace(metadata.key, std::move(entry));
    index_key_unlocked(metadata.key);
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
    return found->second->runtime->pending_children.erase(child) != 0;
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
    const PartCert &part)
{
    if (part.get_proposal_key() != lease.key())
        return false;

    std::lock_guard<std::mutex> lock(mutex_);
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

    found->second->accumulator = std::move(next);
    found->second->runtime->verified_signers = std::move(expected);
    return true;
}

bool ProposalContextLifecycle::record_verified_direct_part(
    const ProposalContextLease &lease,
    const ReplicaConfig &config,
    ReplicaID authenticated_child,
    ReplicaID claimed_voter,
    const PartCert &part)
{
    if (part.get_proposal_key() != lease.key() ||
        authenticated_child != claimed_voter)
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

    found->second->accumulator = std::move(next);
    found->second->runtime->verified_signers = std::move(expected);
    found->second->runtime->pending_children.erase(authenticated_child);
    return true;
}

bool ProposalContextLifecycle::record_verified_aggregate_certificate(
    const ProposalContextLease &lease,
    ReplicaID authenticated_child,
    const QuorumCert &certificate)
{
    if (certificate.get_proposal_key() != lease.key())
        return false;
    std::set<ReplicaID> certified_signers;
    if (!exact_signer_set(certificate, certified_signers) ||
        certified_signers.empty())
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
    if (subtree == found->second->tree->child_subtrees.end())
        return false;
    for (const auto signer : certified_signers)
        if (subtree->second.count(signer) == 0 ||
            found->second->runtime->verified_signers.count(signer) != 0)
            return false;

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
        return false;
    }
    std::set<ReplicaID> next_signers;
    if (!exact_signer_set(*next, next_signers) ||
        next_signers != expected)
        return false;

    found->second->accumulator = std::move(next);
    found->second->runtime->verified_signers = std::move(expected);
    found->second->runtime->pending_children.erase(authenticated_child);
    return true;
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
    return found->second->runtime->pending_children;
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
    found->second->runtime->pending_children.erase(authenticated_child);
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
    found->second->runtime->pending_children.erase(authenticated_child);
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
        !found->second->runtime.has_value())
        return false;
    for (const auto signer : certified_signers)
        if (found->second->runtime->verified_signers.count(signer) == 0 ||
            found->second->runtime->forwarded_signers.count(signer) != 0)
            return false;

    found->second->runtime->forwarded_signers.insert(
        certified_signers.begin(), certified_signers.end());
    return true;
}

std::optional<ProposalForwardingClaim>
ProposalContextLifecycle::claim_unforwarded_certificate(
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
        !found->second->runtime.has_value())
        return std::nullopt;

    for (const auto signer : signers)
        if (found->second->runtime->verified_signers.count(signer) == 0 ||
            found->second->runtime->forwarded_signers.count(signer) != 0)
            return std::nullopt;

    found->second->runtime->forwarded_signers.insert(
        signers.begin(), signers.end());
    return ProposalForwardingClaim{
        std::move(candidate), std::move(signers)};
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
        switch (event)
        {
        case ProposalContextEvent::aggregation_timeout:
            entry.runtime->pass_through = true;
            break;
        case ProposalContextEvent::late_contribution_forwarded:
            break;
        case ProposalContextEvent::leaf_vote_enqueued:
            terminal = entry.tree->direct_children.empty() &&
                       entry.tree->parent.has_value() &&
                       entry.runtime->verified_signers.count(
                           entry.tree->local_replica) != 0;
            break;
        case ProposalContextEvent::non_root_aggregate_enqueued:
        {
            const auto assigned = as_set(entry.tree->assigned_subtree);
            terminal = entry.tree->parent.has_value() &&
                       !entry.tree->direct_children.empty() &&
                       std::includes(
                           entry.runtime->verified_signers.begin(),
                           entry.runtime->verified_signers.end(),
                           assigned.begin(), assigned.end());
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
    const bool local_is_root =
        metadata.tree.local_replica == metadata.tree.root;
    if (assigned.size() != metadata.tree.assigned_subtree.size() ||
        children.size() != metadata.tree.direct_children.size() ||
        assigned.count(metadata.tree.local_replica) == 0 ||
        local_is_root !=
            !metadata.tree.parent.has_value() ||
        metadata.tree.child_subtrees.size() != children.size())
        return false;

    if (local_is_root)
    {
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
        if (subtree == metadata.tree.child_subtrees.end() ||
            subtree->second.empty() || subtree->second.count(child) == 0)
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
