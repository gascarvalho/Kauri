/**
 * Proposal admission state machine shared by the network handler and tests.
 */

#ifndef HOTSTUFF_PROPOSAL_ADMISSION_H_INCLUDED
#define HOTSTUFF_PROPOSAL_ADMISSION_H_INCLUDED

#include <cstddef>
#include <cstdint>
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
    duplicate,
    buffered_future,
    admitted_active
};

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

    virtual void relay_once(const BufferedProposal &proposal) = 0;
    virtual void process_active(const BufferedProposal &proposal) = 0;
    virtual void local_vote_authorized(const ProposalKey &key) = 0;
    virtual void create_expected_vote_state(const ProposalKey &key) = 0;
    virtual void start_latency_deadline(const ProposalKey &key) = 0;
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
        ProposalAdmissionEffects &effects);

    ProposalAdmissionResult receive(BufferedProposal proposal);
    std::vector<ProposalAdmissionResult> activate(
        const ConfigurationId &configuration);
    bool activate_without_draining(
        const ConfigurationId &configuration) noexcept;
    bool process_claimed_active(const BufferedProposal &proposal);
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
    std::set<ProposalKey> received_;
    std::set<ProposalKey> admitted_;
    std::set<ProposalKey> locally_authorized_;
    std::set<ConfigurationId> retired_configurations_;
    std::uint32_t first_live_epoch_{0};
};

} // namespace hotstuff

#endif
