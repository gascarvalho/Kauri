/**
 * Bounded, experiment-only Byzantine fault decisions.
 */

#ifndef HOTSTUFF_EXPERIMENT_BYZANTINE_ADAPTER_H_INCLUDED
#define HOTSTUFF_EXPERIMENT_BYZANTINE_ADAPTER_H_INCLUDED

#include <cstddef>
#include <cstdint>
#include <functional>
#include <memory>
#include <optional>
#include <string>
#include <vector>

#include "hotstuff/configuration.h"

namespace hotstuff
{

struct ExperimentByzantineContext final
{
    ProposalKey proposal;
    std::string diagnostic_window;
};

enum class ExperimentDirectVoteDisposition
{
    forward,
    omit_first,
    omit_repeat
};

enum class ExperimentReplicaRole
{
    root,
    internal,
    leaf
};

enum class ExperimentOmissionAction
{
    forward,
    omit_aggregate,
    omit_direct_vote,
    capacity_exhausted
};

enum class ExperimentOmissionCohort
{
    none,
    hard,
    responsive_degraded
};

struct ExperimentRotatingOmissionOptions final
{
    std::string mode;
    ReplicaID local_replica{0};
    std::size_t replica_count{0};
    std::vector<ReplicaID> actor_ids;
    std::size_t expected_actor_count{0};
    std::uint64_t window_start_monotonic_ns{0};
    std::uint64_t window_end_monotonic_ns{0};
    std::size_t max_omissions_per_proposal{0};
    std::size_t maximum_contexts{0};
    std::vector<ReplicaID> responsive_degraded_actor_ids;
    std::size_t responsive_omission_period{0};
};

struct ExperimentOmissionMarker final
{
    ProposalKey proposal;
    std::string diagnostic_window;
    std::string fault_mode;
    ReplicaID actor{0};
    ExperimentOmissionAction action{ExperimentOmissionAction::forward};
    std::uint64_t window_start_monotonic_ns{0};
    std::uint64_t window_end_monotonic_ns{0};
    std::uint64_t monotonic_ns{0};
    ExperimentOmissionCohort cohort{ExperimentOmissionCohort::none};
    std::size_t hard_actor_count{0};
    std::size_t responsive_degraded_actor_count{0};
    std::size_t fault_threshold{0};
    std::size_t max_omissions_per_proposal{0};
    std::size_t responsive_omission_period{0};
    std::uint64_t contribution_ordinal{0};
    ExperimentReplicaRole contribution_role{ExperimentReplicaRole::root};
    std::uint64_t role_contribution_ordinal{0};
};

std::string format_experiment_omission_marker(
    const ExperimentOmissionMarker &marker);

struct ExperimentByzantineOptions final
{
    bool enabled{false};
    ConfigurationId configuration;
    std::optional<ConfigurationId> additional_omission_configuration;
    std::string diagnostic_window;
    std::optional<ReplicaID> false_report_target;
    bool omit_outbound_aggregate{false};
    bool omit_outbound_direct_vote{false};
    std::size_t maximum_false_report_contexts{0};
    std::size_t maximum_omission_contexts{0};
    std::size_t maximum_direct_vote_omission_contexts{0};
    std::optional<ExperimentRotatingOmissionOptions> rotating_omission;
    std::function<void(const ExperimentOmissionMarker &)>
        omission_marker_emitter;
};

/**
 * Pure local fault selector.
 *
 * The adapter owns no timers, transport, evidence, membership, quorum, keys,
 * or manager state. Runtime callers keep verified contributions on the normal
 * consensus path and use these decisions only at explicit experiment seams.
 * In the frozen two-mode static diagnosis model, signer inclusion
 * cryptographically proves a false report; signer exclusion identifies the
 * target omission only within that constrained model, not arbitrary
 * Byzantine attribution.
 */
class ExperimentByzantineAdapter final
{
public:
    explicit ExperimentByzantineAdapter(
        ExperimentByzantineOptions options = {});
    ~ExperimentByzantineAdapter();

    ExperimentByzantineAdapter(
        const ExperimentByzantineAdapter &) = delete;
    ExperimentByzantineAdapter &operator=(
        const ExperimentByzantineAdapter &) = delete;
    ExperimentByzantineAdapter(
        ExperimentByzantineAdapter &&) = delete;
    ExperimentByzantineAdapter &operator=(
        ExperimentByzantineAdapter &&) = delete;

    bool arm_false_report(
        const ExperimentByzantineContext &context,
        ReplicaID target);
    bool on_verified_response(
        const ExperimentByzantineContext &context,
        ReplicaID target) noexcept;
    bool consume_false_report_positive_marker(
        const ExperimentByzantineContext &context,
        ReplicaID target) noexcept;
    bool should_retain_response_evidence(
        const ExperimentByzantineContext &context) const noexcept;
    bool cancel_false_report(
        const ExperimentByzantineContext &context,
        ReplicaID target) noexcept;
    bool consume_false_timeout(
        const ExperimentByzantineContext &context,
        ReplicaID target) noexcept;
    bool consume_outbound_aggregate(
        const ExperimentByzantineContext &context,
        ExperimentReplicaRole role = ExperimentReplicaRole::internal,
        std::uint64_t monotonic_ns = 0);
    bool consume_outbound_aggregate_marker(
        const ExperimentByzantineContext &context) noexcept;
    ExperimentDirectVoteDisposition consume_outbound_direct_vote(
        const ExperimentByzantineContext &context,
        ExperimentReplicaRole role = ExperimentReplicaRole::leaf,
        std::uint64_t monotonic_ns = 0);
    bool outbound_direct_vote_omitted(
        const ExperimentByzantineContext &context) const noexcept;
    std::optional<ReplicaID> rotating_omission_actor(
        const ProposalKey &proposal) const;
    bool rotating_omission_enabled() const noexcept;
    bool scheduled_omission_enabled() const noexcept;
    /**
     * Read-only experiment diagnostic membership. This grants no consensus,
     * topology, timing, or omission authority.
     */
    bool is_tiered_responsive_degraded_actor(
        ReplicaID replica) const noexcept;

private:
    struct State;
    std::unique_ptr<State> state_;
};

} // namespace hotstuff

#endif
