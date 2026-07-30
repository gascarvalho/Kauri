/**
 * Bounded, experiment-only Byzantine fault decisions.
 */

#ifndef HOTSTUFF_EXPERIMENT_BYZANTINE_ADAPTER_H_INCLUDED
#define HOTSTUFF_EXPERIMENT_BYZANTINE_ADAPTER_H_INCLUDED

#include <cstddef>
#include <memory>
#include <optional>
#include <string>

#include "hotstuff/configuration.h"

namespace hotstuff
{

struct ExperimentByzantineContext final
{
    ProposalKey proposal;
    std::string diagnostic_window;
};

struct ExperimentByzantineOptions final
{
    bool enabled{false};
    ConfigurationId configuration;
    std::string diagnostic_window;
    std::optional<ReplicaID> false_report_target;
    bool omit_outbound_aggregate{false};
    std::size_t maximum_false_report_contexts{0};
    std::size_t maximum_omission_contexts{0};
};

/**
 * Pure local fault selector.
 *
 * The adapter owns no timers, transport, evidence, membership, quorum, keys,
 * or manager state. Runtime callers keep verified contributions on the normal
 * consensus path and use these decisions only at explicit experiment seams.
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
    bool should_retain_response_evidence(
        const ExperimentByzantineContext &context) const noexcept;
    bool cancel_false_report(
        const ExperimentByzantineContext &context,
        ReplicaID target) noexcept;
    bool consume_false_timeout(
        const ExperimentByzantineContext &context,
        ReplicaID target) noexcept;
    bool consume_outbound_aggregate(
        const ExperimentByzantineContext &context);

private:
    struct State;
    std::unique_ptr<State> state_;
};

} // namespace hotstuff

#endif
