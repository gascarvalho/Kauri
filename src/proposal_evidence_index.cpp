#include "hotstuff/proposal_evidence_index.h"

#include <map>
#include <set>
#include <stdexcept>
#include <utility>

namespace hotstuff
{

struct ProposalEvidenceIndex::State
{
    explicit State(ProposalEvidenceIndexLimits limits_) noexcept
        : limits(limits_),
          healthy(limits.maximum_exact_proposals != 0 &&
                  limits.maximum_retired_configurations != 0)
    {
    }

    ProposalEvidenceIndexLimits limits;
    std::map<ProposalKey, ProposalEvidenceStatus> exact_proposals;
    std::set<ConfigurationId> retired_configurations;
    std::uint32_t first_live_epoch{0};
    std::uint64_t capacity_failures{0};
    bool healthy{true};
    bool stopped{false};

    bool stage_admit(const ProposalKey &proposal)
    {
        if (stopped || !healthy ||
            proposal.configuration.epoch_number < first_live_epoch ||
            retired_configurations.count(proposal.configuration) != 0)
        {
            return false;
        }

        const auto existing = exact_proposals.find(proposal);
        if (existing != exact_proposals.end())
            return false;
        if (exact_proposals.size() >= limits.maximum_exact_proposals)
            throw std::length_error("proposal evidence index is full");
        exact_proposals.emplace(
            proposal, ProposalEvidenceStatus::admissible);
        return true;
    }

    bool stage_stale_if_admitted(const ProposalKey &proposal) noexcept
    {
        if (stopped)
            return false;
        const auto existing = exact_proposals.find(proposal);
        if (existing == exact_proposals.end() ||
            existing->second != ProposalEvidenceStatus::admissible)
        {
            return false;
        }
        existing->second = ProposalEvidenceStatus::stale;
        return true;
    }

    bool stage_mark_stale(const ProposalKey &proposal)
    {
        if (stopped ||
            proposal.configuration.epoch_number < first_live_epoch ||
            retired_configurations.count(proposal.configuration) != 0)
        {
            return false;
        }

        const auto existing = exact_proposals.find(proposal);
        if (existing != exact_proposals.end())
        {
            if (existing->second == ProposalEvidenceStatus::stale)
                return false;
            existing->second = ProposalEvidenceStatus::stale;
            return true;
        }
        if (!healthy)
            return false;
        if (exact_proposals.size() >= limits.maximum_exact_proposals)
            throw std::length_error("proposal evidence index is full");
        exact_proposals.emplace(proposal, ProposalEvidenceStatus::stale);
        return true;
    }

    bool stage_retire_configuration(
        const ConfigurationId &configuration)
    {
        if (stopped || !healthy ||
            configuration.epoch_number < first_live_epoch ||
            retired_configurations.count(configuration) != 0)
        {
            return false;
        }
        if (retired_configurations.size() >=
            limits.maximum_retired_configurations)
        {
            throw std::length_error(
                "proposal evidence retirement index is full");
        }

        retired_configurations.insert(configuration);
        for (auto proposal = exact_proposals.begin();
             proposal != exact_proposals.end();)
        {
            if (proposal->first.configuration != configuration)
            {
                ++proposal;
                continue;
            }
            proposal = exact_proposals.erase(proposal);
        }
        return true;
    }

    bool stage_advance_retirement_floor(
        std::uint32_t next_first_live_epoch) noexcept
    {
        if (stopped || next_first_live_epoch <= first_live_epoch)
            return false;

        first_live_epoch = next_first_live_epoch;
        for (auto proposal = exact_proposals.begin();
             proposal != exact_proposals.end();)
        {
            if (proposal->first.configuration.epoch_number >=
                next_first_live_epoch)
            {
                ++proposal;
                continue;
            }
            proposal = exact_proposals.erase(proposal);
        }
        for (auto configuration = retired_configurations.begin();
             configuration != retired_configurations.end();)
        {
            if (configuration->epoch_number >= next_first_live_epoch)
            {
                ++configuration;
                continue;
            }
            configuration = retired_configurations.erase(configuration);
        }
        return true;
    }
};

ProposalEvidenceIndex::ProposalEvidenceIndex(
    ProposalEvidenceIndexLimits limits)
    : state_(new State(limits))
{
}

ProposalEvidenceIndex::~ProposalEvidenceIndex() = default;

ProposalEvidenceIndex::PreparedMutation::PreparedMutation(
    ProposalEvidenceIndex &owner,
    std::unique_ptr<State> staged,
    bool changed) noexcept
    : owner_(&owner), staged_(std::move(staged)), changed_(changed)
{}

ProposalEvidenceIndex::PreparedMutation::~PreparedMutation() = default;

ProposalEvidenceIndex::PreparedMutation::PreparedMutation(
    PreparedMutation &&other) noexcept
    : owner_(other.owner_),
      staged_(std::move(other.staged_)),
      changed_(other.changed_)
{
    other.owner_ = nullptr;
    other.changed_ = false;
}

ProposalEvidenceIndex::PreparedMutation &
ProposalEvidenceIndex::PreparedMutation::operator=(
    PreparedMutation &&other) noexcept
{
    if (this == &other)
        return *this;
    discard();
    owner_ = other.owner_;
    staged_ = std::move(other.staged_);
    changed_ = other.changed_;
    other.owner_ = nullptr;
    other.changed_ = false;
    return *this;
}

bool ProposalEvidenceIndex::PreparedMutation::changed() const noexcept
{
    return owner_ != nullptr && staged_ != nullptr && changed_;
}

bool ProposalEvidenceIndex::PreparedMutation::commit() noexcept
{
    if (owner_ == nullptr || staged_ == nullptr)
        return false;
    owner_->state_.swap(staged_);
    owner_ = nullptr;
    staged_.reset();
    return true;
}

void ProposalEvidenceIndex::PreparedMutation::discard() noexcept
{
    owner_ = nullptr;
    staged_.reset();
    changed_ = false;
}

ProposalEvidenceIndex::PreparedMutation
ProposalEvidenceIndex::prepare_admit(const ProposalKey &proposal)
{
    auto staged = std::make_unique<State>(*state_);
    const auto changed = staged->stage_admit(proposal);
    return PreparedMutation(*this, std::move(staged), changed);
}

ProposalEvidenceIndex::PreparedMutation
ProposalEvidenceIndex::prepare_stale_if_admitted(
    const ProposalKey &proposal)
{
    auto staged = std::make_unique<State>(*state_);
    const auto changed = staged->stage_stale_if_admitted(proposal);
    return PreparedMutation(*this, std::move(staged), changed);
}

ProposalEvidenceIndex::PreparedMutation
ProposalEvidenceIndex::prepare_mark_stale(const ProposalKey &proposal)
{
    auto staged = std::make_unique<State>(*state_);
    const auto changed = staged->stage_mark_stale(proposal);
    return PreparedMutation(*this, std::move(staged), changed);
}

ProposalEvidenceIndex::PreparedMutation
ProposalEvidenceIndex::prepare_retire_configuration(
    const ConfigurationId &configuration)
{
    auto staged = std::make_unique<State>(*state_);
    const auto changed =
        staged->stage_retire_configuration(configuration);
    return PreparedMutation(*this, std::move(staged), changed);
}

ProposalEvidenceIndex::PreparedMutation
ProposalEvidenceIndex::prepare_advance_retirement_floor(
    std::uint32_t first_live_epoch)
{
    auto staged = std::make_unique<State>(*state_);
    const auto changed =
        staged->stage_advance_retirement_floor(first_live_epoch);
    return PreparedMutation(*this, std::move(staged), changed);
}

bool ProposalEvidenceIndex::admit(const ProposalKey &proposal) noexcept
{
    auto &state = *state_;
    if (state.stopped || !state.healthy)
        return false;
    if (proposal.configuration.epoch_number < state.first_live_epoch ||
        state.retired_configurations.count(proposal.configuration) != 0)
        return false;

    const auto existing = state.exact_proposals.find(proposal);
    if (existing != state.exact_proposals.end())
    {
        return existing->second == ProposalEvidenceStatus::admissible;
    }

    if (state.exact_proposals.size() >=
        state.limits.maximum_exact_proposals)
    {
        ++state.capacity_failures;
        state.healthy = false;
        return false;
    }

    try
    {
        return state.exact_proposals.emplace(
            proposal, ProposalEvidenceStatus::admissible).second;
    }
    catch (...)
    {
        state.healthy = false;
        return false;
    }
}

bool ProposalEvidenceIndex::stale_if_admitted(
    const ProposalKey &proposal) noexcept
{
    auto &state = *state_;
    if (state.stopped)
        return false;

    const auto existing = state.exact_proposals.find(proposal);
    if (existing == state.exact_proposals.end() ||
        existing->second != ProposalEvidenceStatus::admissible)
        return false;

    existing->second = ProposalEvidenceStatus::stale;
    return true;
}

bool ProposalEvidenceIndex::mark_stale(
    const ProposalKey &proposal) noexcept
{
    auto &state = *state_;
    if (state.stopped)
        return false;
    if (proposal.configuration.epoch_number < state.first_live_epoch ||
        state.retired_configurations.count(proposal.configuration) != 0)
        return false;

    const auto existing = state.exact_proposals.find(proposal);
    if (existing != state.exact_proposals.end())
    {
        if (existing->second == ProposalEvidenceStatus::stale)
            return false;
        existing->second = ProposalEvidenceStatus::stale;
        return true;
    }
    if (!state.healthy)
        return false;

    if (state.exact_proposals.size() >=
        state.limits.maximum_exact_proposals)
    {
        ++state.capacity_failures;
        state.healthy = false;
        return false;
    }

    try
    {
        return state.exact_proposals.emplace(
            proposal, ProposalEvidenceStatus::stale).second;
    }
    catch (...)
    {
        state.healthy = false;
        return false;
    }
}

std::size_t ProposalEvidenceIndex::retire_configuration(
    const ConfigurationId &configuration) noexcept
{
    auto &state = *state_;
    if (state.stopped ||
        configuration.epoch_number < state.first_live_epoch ||
        state.retired_configurations.count(configuration) != 0 ||
        !state.healthy)
        return 0;

    if (state.retired_configurations.size() >=
        state.limits.maximum_retired_configurations)
    {
        ++state.capacity_failures;
        state.healthy = false;
        return 0;
    }

    try
    {
        const auto inserted =
            state.retired_configurations.insert(configuration).second;
        if (!inserted)
            return 0;
    }
    catch (...)
    {
        state.healthy = false;
        return 0;
    }

    std::size_t retired = 0;
    for (auto proposal = state.exact_proposals.begin();
         proposal != state.exact_proposals.end();)
    {
        if (proposal->first.configuration != configuration)
        {
            ++proposal;
            continue;
        }
        proposal = state.exact_proposals.erase(proposal);
        ++retired;
    }
    return retired;
}

std::size_t ProposalEvidenceIndex::advance_retirement_floor(
    std::uint32_t first_live_epoch) noexcept
{
    auto &state = *state_;
    if (state.stopped || first_live_epoch <= state.first_live_epoch)
        return 0;

    state.first_live_epoch = first_live_epoch;
    std::size_t compacted = 0;
    for (auto proposal = state.exact_proposals.begin();
         proposal != state.exact_proposals.end();)
    {
        if (proposal->first.configuration.epoch_number >= first_live_epoch)
        {
            ++proposal;
            continue;
        }
        proposal = state.exact_proposals.erase(proposal);
        ++compacted;
    }
    for (auto configuration = state.retired_configurations.begin();
         configuration != state.retired_configurations.end();)
    {
        if (configuration->epoch_number >= first_live_epoch)
        {
            ++configuration;
            continue;
        }
        configuration = state.retired_configurations.erase(configuration);
        ++compacted;
    }
    return compacted;
}

ProposalEvidenceStatus ProposalEvidenceIndex::classify(
    const ProposalKey &proposal) const noexcept
{
    const auto &state = *state_;
    if (proposal.configuration.epoch_number < state.first_live_epoch ||
        state.retired_configurations.count(proposal.configuration) != 0)
        return ProposalEvidenceStatus::stale;

    const auto existing = state.exact_proposals.find(proposal);
    if (existing != state.exact_proposals.end() &&
        existing->second == ProposalEvidenceStatus::stale)
        return ProposalEvidenceStatus::stale;
    if (state.stopped || !state.healthy ||
        existing == state.exact_proposals.end())
        return ProposalEvidenceStatus::unknown;
    return ProposalEvidenceStatus::admissible;
}

ProposalEvidenceIndexStats ProposalEvidenceIndex::stats() const noexcept
{
    const auto &state = *state_;
    std::size_t admissible = 0;
    std::size_t stale = 0;
    for (const auto &proposal : state.exact_proposals)
    {
        if (proposal.second == ProposalEvidenceStatus::admissible)
            ++admissible;
        else
            ++stale;
    }
    return ProposalEvidenceIndexStats{
        admissible,
        stale,
        state.retired_configurations.size(),
        state.first_live_epoch,
        state.capacity_failures,
        state.healthy,
        state.stopped};
}

bool ProposalEvidenceIndex::healthy() const noexcept
{
    return state_->healthy;
}

void ProposalEvidenceIndex::shutdown() noexcept
{
    state_->stopped = true;
}

} // namespace hotstuff
