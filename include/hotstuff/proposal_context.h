/**
 * Exact proposal-context lifecycle and configuration-owned runtime state.
 */

#ifndef HOTSTUFF_PROPOSAL_CONTEXT_H_INCLUDED
#define HOTSTUFF_PROPOSAL_CONTEXT_H_INCLUDED

#include <cstddef>
#include <cstdint>
#include <functional>
#include <map>
#include <memory>
#include <mutex>
#include <optional>
#include <set>
#include <vector>

#include "hotstuff/configuration.h"
#include "hotstuff/crypto.h"

namespace hotstuff
{

enum class ProposalContextStatus
{
    unknown,
    buffered_future,
    admitted_open,
    terminal_closed,
    retired
};

enum class ProposalContextOrigin
{
    remote,
    leader_local
};

enum class ProposalContextEvent
{
    leaf_vote_enqueued,
    non_root_aggregate_enqueued,
    root_qc_published,
    proposal_aborted,
    committed,
    shutdown,
    aggregation_timeout,
    late_contribution_forwarded
};

enum class ProposalTransitionResult
{
    stale_lease,
    retained_open,
    terminal_closed
};

struct ProposalTreeSnapshot
{
    ReplicaID local_replica{0};
    ReplicaID root{0};
    std::optional<ReplicaID> parent;
    std::vector<ReplicaID> direct_children;
    std::vector<ReplicaID> assigned_subtree;
    std::map<ReplicaID, std::set<ReplicaID>> child_subtrees;
    std::uint32_t fanout{0};
    std::uint32_t pipeline_stretch{0};
};

struct ProposalContextMetadata
{
    ProposalKey key;
    ProposalTreeSnapshot tree;
    std::size_t global_quorum{0};
};

std::optional<ProposalContextMetadata>
make_exact_proposal_context_metadata(
    const ProposalKey &key,
    ReplicaID local_replica,
    const std::vector<ReplicaID> &members_breadth_first,
    std::uint32_t fanout,
    std::uint32_t pipeline_stretch,
    std::size_t global_quorum);

struct ProposalContextSnapshot
{
    std::set<ReplicaID> pending_children;
    std::set<ReplicaID> latency_started;
    std::set<ReplicaID> verified_signers;
    std::set<ReplicaID> forwarded_signers;
    bool pass_through{false};
    bool root_qc_progress_claimed{false};
    std::uint64_t timer_generation{0};
};

struct ProposalForwardingClaim
{
    quorum_cert_bt certificate;
    std::set<ReplicaID> signers;
};

struct ProposalContextStorageStats
{
    std::size_t retained_tree_snapshots{0};
    std::size_t retained_runtime_states{0};
    std::size_t retained_accumulators{0};
    std::size_t retained_latency_entries{0};
    std::size_t terminal_tombstones{0};
    std::size_t retired_configuration_tombstones{0};
};

class ProposalContextLease final
{
public:
    const ProposalKey &key() const noexcept;
    ProposalContextOrigin origin() const noexcept;
    std::uint64_t generation() const noexcept;
    const ProposalTreeSnapshot &tree() const noexcept;

private:
    friend class ProposalContextLifecycle;

    ProposalContextLease(
        ProposalKey key,
        ProposalContextOrigin origin,
        std::uint64_t generation,
        std::shared_ptr<const ProposalTreeSnapshot> tree);

    ProposalKey key_;
    ProposalContextOrigin origin_;
    std::uint64_t generation_;
    std::shared_ptr<const ProposalTreeSnapshot> tree_;
};

class ProposalContextLifecycle final
{
public:
    using TimerCancellation = std::function<void()>;
    using TimerCallback = std::function<void(const ProposalContextLease &)>;

    ProposalContextLifecycle();
    ~ProposalContextLifecycle();

    ProposalContextLifecycle(const ProposalContextLifecycle &) = delete;
    ProposalContextLifecycle &operator=(
        const ProposalContextLifecycle &) = delete;

    ProposalContextStatus context_status(const ProposalKey &key) const;
    std::optional<ProposalContextLease> acquire_open_context(
        const ProposalKey &key) const;
    bool revalidate(const ProposalContextLease &lease) const;

    bool buffer_future(const ProposalContextMetadata &metadata);
    std::optional<ProposalContextLease> admit_remote(
        const ProposalContextMetadata &metadata);
    std::optional<ProposalContextLease> admit_local(
        const ProposalContextMetadata &metadata);

    void activate_configuration(const ConfigurationId &configuration);
    std::optional<ConfigurationId> active_configuration() const;

    bool close(const ProposalKey &key, ProposalContextEvent reason);
    std::vector<ProposalKey> close_committed_block(
        const uint256_t &block_hash);
    std::size_t retire_configuration(
        const ConfigurationId &configuration);
    std::size_t advance_retirement_floor(
        std::uint32_t first_live_epoch);
    bool has_open_context_before_epoch(
        std::uint32_t first_live_epoch) const;
    bool is_configuration_retired(
        const ConfigurationId &configuration) const;
    std::size_t proposal_entry_count(
        const ConfigurationId &configuration) const;

    bool mark_child_responded(const ProposalContextLease &lease,
                              ReplicaID child);
    bool record_latency_start(const ProposalContextLease &lease,
                              ReplicaID child);
    std::optional<std::uint64_t> take_latency_us(
        const ProposalContextLease &lease,
        ReplicaID child);
    bool initialize_accumulator(
        const ProposalContextLease &lease,
        quorum_cert_bt accumulator);
    bool record_local_part(
        const ProposalContextLease &lease,
        const ReplicaConfig &config,
        ReplicaID signer,
        const PartCert &part);
    bool record_verified_direct_part(
        const ProposalContextLease &lease,
        const ReplicaConfig &config,
        ReplicaID authenticated_child,
        ReplicaID claimed_voter,
        const PartCert &part);
    bool record_verified_aggregate_certificate(
        const ProposalContextLease &lease,
        ReplicaID authenticated_child,
        const QuorumCert &certificate);
    quorum_cert_bt clone_accumulator(
        const ProposalContextLease &lease) const;
    quorum_cert_bt clone_publishable_root_qc(
        const ProposalContextLease &lease) const;
    bool claim_root_qc_progress(
        const ProposalContextLease &lease);
    bool assigned_subtree_complete(
        const ProposalContextLease &lease) const;
    bool pass_through_enabled(
        const ProposalContextLease &lease) const;
    std::optional<std::set<ReplicaID>> pending_children(
        const ProposalContextLease &lease) const;
    bool record_local_signer(const ProposalContextLease &lease);
    bool record_verified_direct(const ProposalContextLease &lease,
                                ReplicaID authenticated_child,
                                ReplicaID claimed_voter);
    bool record_verified_aggregate(
        const ProposalContextLease &lease,
        ReplicaID authenticated_child,
        const std::set<ReplicaID> &certified_signers);
    bool mark_forwarded_signers(
        const ProposalContextLease &lease,
        const std::set<ReplicaID> &certified_signers);
    std::optional<ProposalForwardingClaim>
    claim_unforwarded_certificate(
        const ProposalContextLease &lease,
        quorum_cert_bt candidate);

    std::uint64_t arm_timer(
        const ProposalContextLease &lease,
        TimerCancellation cancel = TimerCancellation());
    bool dispatch_timer(const ProposalKey &key,
                        std::uint64_t timer_generation,
                        const TimerCallback &callback);

    ProposalTransitionResult transition(
        const ProposalContextLease &lease,
        ProposalContextEvent event);
    std::optional<ProposalContextSnapshot> snapshot(
        const ProposalKey &key) const;
    ProposalContextStorageStats storage_stats() const;
    void shutdown();

private:
    struct Entry;

    std::optional<ProposalContextLease> admit(
        const ProposalContextMetadata &metadata,
        ProposalContextOrigin origin);
    static bool valid_metadata(const ProposalContextMetadata &metadata);
    static bool same_metadata(const Entry &entry,
                              const ProposalContextMetadata &metadata);
    static void run_cancellation(TimerCancellation cancel) noexcept;
    TimerCancellation compact_terminal_unlocked(
        const ProposalKey &key,
        Entry &entry);
    void index_key_unlocked(const ProposalKey &key);
    void unindex_key_unlocked(const ProposalKey &key);
    bool configuration_is_retired_unlocked(
        const ConfigurationId &configuration) const;

    mutable std::mutex mutex_;
    std::map<ProposalKey, std::unique_ptr<Entry>> entries_;
    std::map<uint256_t, std::set<ProposalKey>> keys_by_block_;
    std::set<ConfigurationId> retired_configurations_;
    std::optional<ConfigurationId> active_configuration_;
    std::uint32_t first_live_epoch_{0};
    std::uint64_t next_generation_{1};
    std::uint64_t next_timer_generation_{1};
};

} // namespace hotstuff

#endif
