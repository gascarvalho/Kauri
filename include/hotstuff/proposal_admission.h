/**
 * Proposal admission state machine shared by the network handler and tests.
 */

#ifndef HOTSTUFF_PROPOSAL_ADMISSION_H_INCLUDED
#define HOTSTUFF_PROPOSAL_ADMISSION_H_INCLUDED

#include <cstddef>
#include <cstdint>
#include <functional>
#include <set>
#include <vector>

#include "hotstuff/epoch_store.h"
#include "hotstuff/future_proposal_buffer.h"

namespace hotstuff
{

enum class ProposalDisposition
{
    rejected_malformed,
    rejected_unknown_configuration,
    rejected_digest_mismatch,
    rejected_stale_configuration,
    rejected_invalid_proposer,
    rejected_capacity,
    duplicate,
    buffered_future,
    admitted_active
};

enum class ProposalRelayPolicy
{
    eager_before_processing,
    adaptive_v2_deferred_until_arm_attempt
};

// Completion of adaptive-v2 active processing is asynchronous. The outcome
// records the proposal transport exposure boundary so buffered bytes are
// never replayed after any child may have observed them.
enum class ProposalProcessingOutcome
{
    completed_exposed,
    completed_ownership_transferred,
    retryable_pre_relay_failure,
    terminal_pre_relay,
    terminal_post_relay
};

using ProposalProcessingCompletion =
    std::function<void(ProposalProcessingOutcome)>;

struct ProposalAdmissionResult
{
    ProposalDisposition disposition;
    ProposalKey key;
};

struct ProposalAdmissionStorageStats
{
    std::size_t retained_received{0};
    std::size_t retained_admitted{0};
    std::size_t retained_local_authorizations{0};
    std::size_t retired_configuration_tombstones{0};
};

class ProposalAdmissionEffects
{
public:
    virtual ~ProposalAdmissionEffects() = default;

    // Adaptive-v2 process_active attempts exact-context initialization and
    // response-evidence arming before relay. Evidence failure poisons the run
    // but never gates consensus dissemination.
    virtual void relay_once(const BufferedProposal &proposal) = 0;
    virtual void process_active(const BufferedProposal &proposal) = 0;
    virtual bool process_active(
        const BufferedProposal &proposal,
        ProposalProcessingCompletion completion)
    {
        process_active(proposal);
        if (completion)
            completion(ProposalProcessingOutcome::completed_exposed);
        return true;
    }
    virtual void local_vote_authorized(const ProposalKey &key) = 0;
    virtual void create_expected_vote_state(const ProposalKey &key) = 0;
    virtual bool start_latency_deadline(const ProposalKey &key) = 0;
    virtual void start_aggregation_timer(const ProposalKey &key) = 0;
    virtual void emit_timeout_report(const ProposalKey &key) = 0;
};

class ProposalAdmissionCoordinator final
{
public:
    ProposalAdmissionCoordinator(
        const EpochStore &epochs,
        ConfigurationId active_configuration,
        FutureProposalBuffer &future_proposals,
        ProposalAdmissionEffects &effects,
        ProposalRelayPolicy relay_policy =
            ProposalRelayPolicy::eager_before_processing);

    ProposalAdmissionResult receive(BufferedProposal proposal);
    std::vector<ProposalAdmissionResult> activate(
        const ConfigurationId &configuration);
    bool activate_without_draining(
        const ConfigurationId &configuration) noexcept;
    bool process_claimed_active(const BufferedProposal &proposal);
    bool process_claimed_active(
        const BufferedProposal &proposal,
        ProposalProcessingCompletion completion);
    bool rollback_claimed_active(const ProposalKey &key) noexcept;
    bool authorize_local_vote(const ProposalKey &key);
    bool retire_proposal(const ProposalKey &key);
    std::size_t retire_configuration(
        const ConfigurationId &configuration);
    std::size_t advance_retirement_floor(
        std::uint32_t first_live_epoch);
    bool contains_admitted(const ProposalKey &key) const;
    ProposalAdmissionStorageStats storage_stats() const;

    const ConfigurationId &active_configuration() const noexcept;

private:
    ProposalAdmissionResult validate(
        const BufferedProposal &proposal) const;
    bool is_known_exact_configuration(
        const ConfigurationId &configuration) const noexcept;
    bool is_configuration_retired(
        const ConfigurationId &configuration) const noexcept;

    const EpochStore &epochs_;
    ConfigurationId active_configuration_;
    FutureProposalBuffer &future_proposals_;
    ProposalAdmissionEffects &effects_;
    ProposalRelayPolicy relay_policy_;
    std::set<ProposalKey> received_;
    std::set<ProposalKey> admitted_;
    std::set<ProposalKey> locally_authorized_;
    std::set<ConfigurationId> retired_configurations_;
    std::uint32_t first_live_epoch_{0};
};

} // namespace hotstuff

#endif
