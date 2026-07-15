/**
 * Bounded manager-side proposal lifecycle evidence.
 */

#ifndef HOTSTUFF_PROPOSAL_EVIDENCE_INDEX_H_INCLUDED
#define HOTSTUFF_PROPOSAL_EVIDENCE_INDEX_H_INCLUDED

#include <cstddef>
#include <cstdint>
#include <memory>

#include "hotstuff/evidence.h"

namespace hotstuff
{

struct ProposalEvidenceIndexLimits
{
    std::size_t maximum_exact_proposals{4096};
    std::size_t maximum_retired_configurations{64};
};

struct ProposalEvidenceIndexStats
{
    std::size_t admissible_proposals{0};
    std::size_t stale_proposals{0};
    std::size_t retired_configurations{0};
    std::uint32_t first_live_epoch{0};
    std::uint64_t capacity_failures{0};
    bool healthy{true};
    bool stopped{false};
};

/**
 * Mirrors immutable proposal-admission and retirement facts for evidence
 * validation.
 *
 * Callers must externally serialize every operation and must not reenter the
 * index. Capacity or allocation failure is sticky: admissible classifications
 * fail closed, while already-known stale classifications remain available.
 */
class ProposalEvidenceIndex final : public ProposalEvidenceWindow
{
    struct State;

public:
    /**
     * Fallibly prepared index replacement with a no-throw commit point.
     *
     * Preparation operates on a private copy. Until commit(), the borrowed
     * index is byte-for-byte logically unchanged. A prepared mutation is
     * single-use and may instead be explicitly discarded. Callers must obey
     * the index's external serialization contract between prepare and commit.
     */
    class PreparedMutation final
    {
    public:
        ~PreparedMutation();

        PreparedMutation(const PreparedMutation &) = delete;
        PreparedMutation &operator=(const PreparedMutation &) = delete;
        PreparedMutation(PreparedMutation &&other) noexcept;
        PreparedMutation &operator=(PreparedMutation &&other) noexcept;

        bool changed() const noexcept;
        bool commit() noexcept;
        void discard() noexcept;

    private:
        friend class ProposalEvidenceIndex;

        PreparedMutation(
            ProposalEvidenceIndex &owner,
            std::unique_ptr<State> staged,
            bool changed) noexcept;

        ProposalEvidenceIndex *owner_{nullptr};
        std::unique_ptr<State> staged_;
        bool changed_{false};
    };

    explicit ProposalEvidenceIndex(ProposalEvidenceIndexLimits limits = {});
    ~ProposalEvidenceIndex();

    ProposalEvidenceIndex(const ProposalEvidenceIndex &) = delete;
    ProposalEvidenceIndex &operator=(const ProposalEvidenceIndex &) = delete;
    ProposalEvidenceIndex(ProposalEvidenceIndex &&) = delete;
    ProposalEvidenceIndex &operator=(ProposalEvidenceIndex &&) = delete;

    bool admit(const ProposalKey &proposal) noexcept;
    bool stale_if_admitted(const ProposalKey &proposal) noexcept;
    bool mark_stale(const ProposalKey &proposal) noexcept;
    std::size_t retire_configuration(
        const ConfigurationId &configuration) noexcept;
    std::size_t advance_retirement_floor(
        std::uint32_t first_live_epoch) noexcept;

    PreparedMutation prepare_admit(const ProposalKey &proposal);
    PreparedMutation prepare_stale_if_admitted(
        const ProposalKey &proposal);
    PreparedMutation prepare_mark_stale(
        const ProposalKey &proposal);
    PreparedMutation prepare_retire_configuration(
        const ConfigurationId &configuration);
    PreparedMutation prepare_advance_retirement_floor(
        std::uint32_t first_live_epoch);

    ProposalEvidenceStatus classify(
        const ProposalKey &proposal) const noexcept override;

    ProposalEvidenceIndexStats stats() const noexcept;
    bool healthy() const noexcept;
    void shutdown() noexcept;

private:
    std::unique_ptr<State> state_;
};

} // namespace hotstuff

#endif
