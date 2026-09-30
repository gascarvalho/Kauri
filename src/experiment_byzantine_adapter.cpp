#include "hotstuff/experiment_byzantine_adapter.h"

#include <algorithm>
#include <array>
#include <cstdint>
#include <fcntl.h>
#include <limits>
#include <map>
#include <set>
#include <sstream>
#include <stdexcept>
#include <tuple>
#include <utility>
#include <unistd.h>

#include <sys/stat.h>

#include <openssl/sha.h>

namespace hotstuff
{
namespace
{

struct ContextLess
{
    bool operator()(
        const ExperimentByzantineContext &left,
        const ExperimentByzantineContext &right) const noexcept
    {
        if (left.proposal < right.proposal)
            return true;
        if (right.proposal < left.proposal)
            return false;
        return left.diagnostic_window < right.diagnostic_window;
    }
};

bool exact_context(
    const ExperimentByzantineOptions &options,
    const ExperimentByzantineContext &context) noexcept
{
    return options.enabled &&
           context.proposal.configuration == options.configuration &&
           context.diagnostic_window == options.diagnostic_window;
}

bool exact_omission_context(
    const ExperimentByzantineOptions &options,
    const ExperimentByzantineContext &context) noexcept
{
    return options.enabled &&
           context.diagnostic_window == options.diagnostic_window &&
           (context.proposal.configuration == options.configuration ||
            (options.additional_omission_configuration.has_value() &&
             context.proposal.configuration ==
                 *options.additional_omission_configuration) ||
            std::find(
                options.additional_omission_configurations.begin(),
                options.additional_omission_configurations.end(),
                context.proposal.configuration) !=
                options.additional_omission_configurations.end());
}

constexpr const char *kRotatingOmissionMode =
    "rotating_intermittent_omission_v1";
constexpr const char *kPersistentOmissionMode =
    "persistent_selected_omission_v1";
// This is intentionally distinct from the two-cohort tiered modes. It is a
// single hard-actor schedule whose marker binds the physical role on every
// exact proposal, allowing an experiment to retain the fault across a move
// from an internal relay to a leaf without introducing another fault cohort.
constexpr const char *kRoleScopedPersistentOmissionMode =
    "role_scoped_persistent_selected_omission_v1";
constexpr const char *kTieredOmissionModeV1 =
    "tiered_persistent_responsive_omission_v1";
constexpr const char *kTieredOmissionModeV2 =
    "tiered_persistent_responsive_omission_v2";
constexpr const char *kN7StaticAggregateGateKind =
    "kauri-n7-static-aggregate-omission-gate-v1";
constexpr const char *kN7ThreeReporterProfileV2 =
    "n7-three-reporter-relay-omission-v2";
constexpr const char *kN7PathTimeoutQuorumProfileV3 =
    "n7-path-local-timeout-quorum-v3";
constexpr const char *kN7PathTimeoutQuorumProfileV4 =
    "n7-path-local-timeout-quorum-v4";
constexpr const char *kExactPostFaultAttemptStartBasis =
    "exact_post_fault_attempt_start_v1";
constexpr const char *kExactPostFaultPathTimeoutQuorumBasis =
    "exact_post_fault_path_timeout_quorum_v1";
constexpr std::size_t kMaximumActivationGateBytes = 4096;

bool valid_sha256_hex(const std::string &value) noexcept
{
    return value.size() == 64 && std::all_of(
        value.begin(), value.end(), [](unsigned char character) {
            return (character >= '0' && character <= '9') ||
                   (character >= 'a' && character <= 'f');
        });
}

std::string sha256_hex(const std::string &bytes)
{
    std::array<unsigned char, SHA256_DIGEST_LENGTH> digest{};
    SHA256(reinterpret_cast<const unsigned char *>(bytes.data()), bytes.size(),
           digest.data());
    static constexpr char digits[] = "0123456789abcdef";
    std::string result;
    result.reserve(digest.size() * 2);
    for (const auto byte : digest)
    {
        result.push_back(digits[byte >> 4]);
        result.push_back(digits[byte & 0x0f]);
    }
    return result;
}

std::optional<std::string> read_regular_file_no_follow(
    const std::string &path, std::size_t maximum_bytes) noexcept
{
    const auto fd = ::open(path.c_str(), O_RDONLY | O_CLOEXEC | O_NOFOLLOW);
    if (fd < 0)
        return std::nullopt;
    struct stat metadata {};
    if (::fstat(fd, &metadata) != 0 || !S_ISREG(metadata.st_mode) ||
        metadata.st_size < 0 ||
        static_cast<std::uintmax_t>(metadata.st_size) > maximum_bytes)
    {
        ::close(fd);
        return std::nullopt;
    }
    std::string bytes(static_cast<std::size_t>(metadata.st_size), '\0');
    std::size_t offset = 0;
    while (offset < bytes.size())
    {
        const auto count = ::read(fd, bytes.data() + offset, bytes.size() - offset);
        if (count <= 0)
        {
            ::close(fd);
            return std::nullopt;
        }
        offset += static_cast<std::size_t>(count);
    }
    const bool stable = ::fstat(fd, &metadata) == 0 &&
        static_cast<std::size_t>(metadata.st_size) == bytes.size();
    ::close(fd);
    return stable ? std::optional<std::string>{std::move(bytes)} : std::nullopt;
}

bool is_rotating_omission_mode(const std::string &mode) noexcept
{
    return mode == kRotatingOmissionMode;
}

bool is_persistent_omission_mode(const std::string &mode) noexcept
{
    return mode == kPersistentOmissionMode ||
           mode == kRoleScopedPersistentOmissionMode;
}

bool is_tiered_omission_mode(const std::string &mode) noexcept
{
    return mode == kTieredOmissionModeV1 || mode == kTieredOmissionModeV2;
}

bool is_role_scoped_tiered_omission_mode(const std::string &mode) noexcept
{
    return mode == kTieredOmissionModeV2;
}

bool is_role_scoped_omission_mode(const std::string &mode) noexcept
{
    return is_role_scoped_tiered_omission_mode(mode) ||
           mode == kRoleScopedPersistentOmissionMode;
}

bool is_scheduled_omission_mode(const std::string &mode) noexcept
{
    return is_rotating_omission_mode(mode) ||
           is_persistent_omission_mode(mode) ||
           is_tiered_omission_mode(mode);
}

const char *omission_cohort_name(ExperimentOmissionCohort cohort) noexcept
{
    switch (cohort)
    {
    case ExperimentOmissionCohort::none:
        return "none";
    case ExperimentOmissionCohort::hard:
        return "hard";
    case ExperimentOmissionCohort::responsive_degraded:
        return "responsive_degraded";
    }
    return "unknown";
}

const char *omission_action_name(ExperimentOmissionAction action) noexcept
{
    switch (action)
    {
    case ExperimentOmissionAction::forward:
        return "forward";
    case ExperimentOmissionAction::omit_aggregate:
        return "omit_aggregate";
    case ExperimentOmissionAction::omit_direct_vote:
        return "omit_direct_vote";
    case ExperimentOmissionAction::capacity_exhausted:
        return "capacity_exhausted";
    }
    return "unknown";
}

const char *replica_role_name(ExperimentReplicaRole role) noexcept
{
    switch (role)
    {
    case ExperimentReplicaRole::root:
        return "root";
    case ExperimentReplicaRole::internal:
        return "internal";
    case ExperimentReplicaRole::leaf:
        return "leaf";
    }
    return "unknown";
}

void fnv1a_byte(std::uint64_t &hash, std::uint8_t byte) noexcept
{
    hash ^= byte;
    hash *= UINT64_C(1099511628211);
}

void fnv1a_u32(std::uint64_t &hash, std::uint32_t value) noexcept
{
    for (int shift = 24; shift >= 0; shift -= 8)
        fnv1a_byte(hash, static_cast<std::uint8_t>(value >> shift));
}

void fnv1a_uint256(std::uint64_t &hash, const uint256_t &value)
{
    const bytearray_t bytes = value;
    for (const auto byte : bytes)
        fnv1a_byte(hash, byte);
}

std::size_t proposal_actor_index(
    const ProposalKey &proposal,
    std::size_t actor_count)
{
    std::uint64_t hash = UINT64_C(14695981039346656037);
    fnv1a_u32(hash, proposal.configuration.epoch_number);
    fnv1a_u32(hash, proposal.configuration.tree_id);
    fnv1a_uint256(hash, proposal.configuration.epoch_digest);
    fnv1a_uint256(hash, proposal.block_hash);
    return static_cast<std::size_t>(hash % actor_count);
}

} // namespace

std::string format_experiment_omission_marker(
    const ExperimentOmissionMarker &marker)
{
    std::ostringstream encoded;
    encoded << "KAURI_FAULT"
            << " fault=" << marker.fault_mode
            << " proposal_epoch="
            << marker.proposal.configuration.epoch_number
            << " proposal_tree="
            << marker.proposal.configuration.tree_id
            << " proposal_epoch_digest="
            << get_hex(marker.proposal.configuration.epoch_digest)
            << " proposal_block_hash=" << get_hex(marker.proposal.block_hash)
            << " window=" << marker.diagnostic_window
            << " window_start_monotonic_ns="
            << marker.window_start_monotonic_ns
            << " window_end_monotonic_ns="
            << marker.window_end_monotonic_ns
            << " actor=" << marker.actor
            << " action=" << omission_action_name(marker.action)
            << " monotonic_ns=" << marker.monotonic_ns;
    if (is_tiered_omission_mode(marker.fault_mode) ||
        marker.fault_mode == kRoleScopedPersistentOmissionMode)
    {
        encoded << " cohort=" << omission_cohort_name(marker.cohort)
                << " hard_actor_count=" << marker.hard_actor_count
                << " responsive_degraded_actor_count="
                << marker.responsive_degraded_actor_count
                << " fault_threshold=" << marker.fault_threshold
                << " max_omissions_per_proposal="
                << marker.max_omissions_per_proposal
                << " responsive_omission_period="
                << marker.responsive_omission_period
                << " contribution_ordinal="
                << marker.contribution_ordinal;
        if (is_role_scoped_omission_mode(marker.fault_mode))
            encoded << " contribution_role="
                    << replica_role_name(marker.contribution_role)
                    << " role_contribution_ordinal="
                    << marker.role_contribution_ordinal
                    << " authenticated_proposal_source_replica="
                    << marker.authenticated_proposal_source_replica
                           .value_or(marker.actor);
    }
    return encoded.str();
}

struct ExperimentByzantineAdapter::State
{
    struct FalseReportState
    {
        bool verified_response_observed{false};
        bool positive_marker_consumed{false};
        bool timeout_consumed{false};
    };

    struct ScheduledDecisionState
    {
        ExperimentOmissionAction action{ExperimentOmissionAction::forward};
        bool marker_emitted{false};
        bool direct_vote_omitted{false};
        ExperimentOmissionCohort cohort{ExperimentOmissionCohort::none};
        bool auditable{false};
        std::uint64_t contribution_ordinal{0};
        ExperimentReplicaRole contribution_role{ExperimentReplicaRole::root};
        std::uint64_t role_contribution_ordinal{0};
        ExperimentReplicaRole physical_role{ExperimentReplicaRole::root};
    };

    struct RoleContributionOrdinals
    {
        std::uint64_t internal{0};
        std::uint64_t leaf{0};
    };

    struct EpochContributionIdentity
    {
        std::uint32_t epoch_number{0};
        uint256_t epoch_digest;

        bool operator<(
            const EpochContributionIdentity &other) const noexcept
        {
            return std::tie(epoch_number, epoch_digest) <
                   std::tie(other.epoch_number, other.epoch_digest);
        }
    };

    explicit State(ExperimentByzantineOptions configured)
        : options(std::move(configured))
    {
        if ((options.additional_omission_configuration.has_value() ||
             !options.additional_omission_configurations.empty()) &&
            options.omit_outbound_direct_vote)
            throw std::invalid_argument(
                "direct-vote omission does not accept an additional "
                "configuration");
        if ((options.additional_omission_configuration.has_value() ||
             !options.additional_omission_configurations.empty()) &&
            (!options.enabled || !options.omit_outbound_aggregate))
            throw std::invalid_argument(
                "additional omission configuration requires enabled "
                "aggregate omission");
        if (options.maximum_omission_contexts_per_configuration != 0 &&
            !options.enabled)
            throw std::invalid_argument(
                "per-configuration omission requires an enabled adapter");
        if (!options.enabled)
            return;
        if (options.diagnostic_window.empty())
            throw std::invalid_argument(
                "diagnostic window must be non-empty");
        const auto fault_mode_count =
            static_cast<unsigned>(
                options.false_report_target.has_value()) +
            static_cast<unsigned>(options.omit_outbound_aggregate) +
            static_cast<unsigned>(options.omit_outbound_direct_vote) +
            static_cast<unsigned>(options.rotating_omission.has_value());
        if (fault_mode_count != 1)
            throw std::invalid_argument(
                "enabled Byzantine adapter requires exactly one fault mode");
        if (options.false_report_target.has_value() &&
            options.maximum_false_report_contexts == 0)
            throw std::invalid_argument(
                "false-report context bound must be positive");
        if (options.omit_outbound_aggregate &&
            options.maximum_omission_contexts == 0)
            throw std::invalid_argument(
                "omission context bound must be positive");
        if (options.omit_outbound_direct_vote &&
            options.maximum_direct_vote_omission_contexts == 0)
            throw std::invalid_argument(
                "direct-vote omission context bound must be positive");
        std::vector<ConfigurationId> omission_configurations;
        if (options.additional_omission_configuration.has_value())
            omission_configurations.push_back(
                *options.additional_omission_configuration);
        omission_configurations.insert(
            omission_configurations.end(),
            options.additional_omission_configurations.begin(),
            options.additional_omission_configurations.end());
        if (omission_configurations.size() > 2)
            throw std::invalid_argument(
                "aggregate omission supports at most three contexts");
        for (const auto &additional : omission_configurations)
        {
            if (additional.epoch_number != options.configuration.epoch_number ||
                additional.epoch_digest != options.configuration.epoch_digest)
                throw std::invalid_argument(
                    "additional omission configuration must share the primary epoch and digest");
            if (additional.tree_id == options.configuration.tree_id)
                throw std::invalid_argument(
                    "additional omission configuration must use a distinct tree");
        }
        std::sort(omission_configurations.begin(), omission_configurations.end(),
            [](const ConfigurationId &left, const ConfigurationId &right)
            { return left.tree_id < right.tree_id; });
        if (std::adjacent_find(omission_configurations.begin(), omission_configurations.end(),
            [](const ConfigurationId &left, const ConfigurationId &right)
            { return left.tree_id == right.tree_id; }) != omission_configurations.end())
            throw std::invalid_argument("additional omission configurations must be distinct");
        const bool three_by_two_limits =
            options.maximum_omission_contexts_per_configuration == 2 &&
            options.maximum_omission_contexts == 6;
        const bool three_by_three_limits =
            options.maximum_omission_contexts_per_configuration == 3 &&
            options.maximum_omission_contexts == 9;
        if (options.maximum_omission_contexts_per_configuration != 0 &&
            (!options.omit_outbound_aggregate ||
             omission_configurations.size() != 2 ||
             (!three_by_two_limits && !three_by_three_limits)))
            throw std::invalid_argument(
                "per-configuration omission requires exactly three "
                "configurations with a supported static quota");
        const bool n7_static =
            options.omit_outbound_aggregate &&
            options.configuration.epoch_number == 0 &&
            options.configuration.tree_id == 4 &&
            omission_configurations.size() == 2 &&
            omission_configurations[0].tree_id == 5 &&
            omission_configurations[1].tree_id == 6 &&
            (three_by_two_limits || three_by_three_limits);
        if (n7_static && !options.activation_gate.has_value())
            throw std::invalid_argument(
                "N7 three-by-two static aggregate omission requires an activation gate");
        if (options.activation_gate.has_value())
        {
            const auto &gate = *options.activation_gate;
            if (!options.omit_outbound_aggregate ||
                options.rotating_omission.has_value() ||
                (!three_by_two_limits && !three_by_three_limits) ||
                gate.local_replica != 1 || gate.path.empty() ||
                gate.manager_event_path.empty() || gate.run_id.empty() ||
                gate.manager_source_instance.empty() ||
                !valid_sha256_hex(gate.profile_sha256) ||
                !valid_sha256_hex(gate.tree_file_sha256) ||
                !valid_sha256_hex(gate.launch_argv_sha256))
                throw std::invalid_argument(
                    "activation gate is limited to a supported N7 static aggregate omission profile");
        }
        if (options.rotating_omission.has_value())
        {
            auto &scheduled = *options.rotating_omission;
            if (!is_scheduled_omission_mode(scheduled.mode))
                throw std::invalid_argument(
                    "unsupported scheduled omission mode");
            if (options.additional_omission_configuration.has_value() ||
                !options.additional_omission_configurations.empty() ||
                options.maximum_false_report_contexts != 0 ||
                options.maximum_omission_contexts != 0 ||
                options.maximum_omission_contexts_per_configuration != 0 ||
                options.maximum_direct_vote_omission_contexts != 0)
                throw std::invalid_argument(
                    "scheduled omission cannot be combined with static "
                    "fault configuration");
            if (scheduled.replica_count == 0 ||
                scheduled.local_replica >= scheduled.replica_count)
                throw std::invalid_argument(
                    "scheduled omission requires an in-range local replica");
            const auto quorum =
                derive_byzantine_quorum(scheduled.replica_count);
            if (!quorum.has_value() || scheduled.expected_actor_count == 0 ||
                scheduled.expected_actor_count > quorum->fault_threshold ||
                scheduled.actor_ids.size() != scheduled.expected_actor_count)
                throw std::invalid_argument(
                    "scheduled omission actor count must be within the "
                    "derived fault threshold");
            std::sort(scheduled.actor_ids.begin(), scheduled.actor_ids.end());
            if (std::adjacent_find(
                    scheduled.actor_ids.begin(), scheduled.actor_ids.end()) !=
                scheduled.actor_ids.end())
                throw std::invalid_argument(
                    "scheduled omission actors must be unique");
            if (std::any_of(
                    scheduled.actor_ids.begin(),
                    scheduled.actor_ids.end(),
                    [&scheduled](ReplicaID actor)
                    { return actor >= scheduled.replica_count; }))
                throw std::invalid_argument(
                    "scheduled omission actor is outside membership");
            if (scheduled.mode == kRoleScopedPersistentOmissionMode &&
                (scheduled.actor_ids.size() != 1 ||
                 scheduled.expected_actor_count != 1))
                throw std::invalid_argument(
                    "role-scoped persistent omission requires exactly one hard actor");
            if (scheduled.first_omission_tree.has_value() &&
                (scheduled.mode != kRoleScopedPersistentOmissionMode ||
                 *scheduled.first_omission_tree != 4 ||
                 *scheduled.first_omission_tree >= scheduled.replica_count))
                throw std::invalid_argument(
                    "first omission tree requires frozen in-range role-scoped persistent tree 4");
            std::sort(
                scheduled.responsive_degraded_actor_ids.begin(),
                scheduled.responsive_degraded_actor_ids.end());
            if (is_tiered_omission_mode(scheduled.mode))
            {
                if (scheduled.responsive_degraded_actor_ids.empty() ||
                    scheduled.responsive_omission_period < 2)
                    throw std::invalid_argument(
                        "tiered omission requires responsive-degraded actors "
                        "and a period greater than one");
                if (std::adjacent_find(
                        scheduled.responsive_degraded_actor_ids.begin(),
                        scheduled.responsive_degraded_actor_ids.end()) !=
                    scheduled.responsive_degraded_actor_ids.end())
                    throw std::invalid_argument(
                        "responsive-degraded omission actors must be unique");
                if (std::any_of(
                        scheduled.responsive_degraded_actor_ids.begin(),
                        scheduled.responsive_degraded_actor_ids.end(),
                        [&scheduled](ReplicaID actor)
                        { return actor >= scheduled.replica_count; }))
                    throw std::invalid_argument(
                        "responsive-degraded omission actor is outside "
                        "membership");
                if (std::any_of(
                        scheduled.responsive_degraded_actor_ids.begin(),
                        scheduled.responsive_degraded_actor_ids.end(),
                        [&scheduled](ReplicaID actor)
                        {
                            return std::binary_search(
                                scheduled.actor_ids.begin(),
                                scheduled.actor_ids.end(),
                                actor);
                        }))
                    throw std::invalid_argument(
                        "tiered omission actor cohorts must be disjoint");
                const auto total_actor_count =
                    scheduled.actor_ids.size() +
                    scheduled.responsive_degraded_actor_ids.size();
                if (total_actor_count > quorum->fault_threshold)
                    throw std::invalid_argument(
                        "tiered omission cohort exceeds the derived fault "
                        "threshold");
            }
            else if (!scheduled.responsive_degraded_actor_ids.empty() ||
                     scheduled.responsive_omission_period != 0)
                throw std::invalid_argument(
                    "responsive-degraded omission configuration requires "
                    "the tiered mode");
            if (scheduled.window_start_monotonic_ns == 0 ||
                scheduled.window_end_monotonic_ns <=
                    scheduled.window_start_monotonic_ns)
                throw std::invalid_argument(
                    "scheduled omission window must be a non-empty future "
                    "monotonic interval");
            const auto expected_maximum_omissions =
                is_rotating_omission_mode(scheduled.mode)
                    ? std::size_t{1}
                    : is_tiered_omission_mode(scheduled.mode)
                          ? scheduled.actor_ids.size() +
                                scheduled.responsive_degraded_actor_ids.size()
                          : scheduled.expected_actor_count;
            if (scheduled.max_omissions_per_proposal !=
                expected_maximum_omissions)
                throw std::invalid_argument(
                    "scheduled omission maximum must match its actor "
                    "schedule");
            if (scheduled.maximum_contexts == 0)
                throw std::invalid_argument(
                    "scheduled omission context bound must be positive");
            scheduled_fault_threshold = quorum->fault_threshold;
        }
    }

    std::optional<ReplicaID> rotating_actor(
        const ProposalKey &proposal) const
    {
        if (!options.enabled || !options.rotating_omission.has_value())
            return std::nullopt;
        const auto &scheduled = *options.rotating_omission;
        if (!is_rotating_omission_mode(scheduled.mode))
            return std::nullopt;
        const auto &actors = scheduled.actor_ids;
        return actors[proposal_actor_index(proposal, actors.size())];
    }

    ExperimentOmissionCohort local_actor_cohort() const
    {
        const auto &scheduled = *options.rotating_omission;
        if ((is_persistent_omission_mode(scheduled.mode) ||
             is_tiered_omission_mode(scheduled.mode)) &&
            std::binary_search(
                scheduled.actor_ids.begin(),
                scheduled.actor_ids.end(),
                scheduled.local_replica))
            return ExperimentOmissionCohort::hard;
        if (is_tiered_omission_mode(scheduled.mode) &&
            std::binary_search(
                scheduled.responsive_degraded_actor_ids.begin(),
                scheduled.responsive_degraded_actor_ids.end(),
                scheduled.local_replica))
            return ExperimentOmissionCohort::responsive_degraded;
        return ExperimentOmissionCohort::none;
    }

    bool local_actor_selected(const ProposalKey &proposal) const
    {
        const auto &scheduled = *options.rotating_omission;
        if (is_persistent_omission_mode(scheduled.mode) ||
            is_tiered_omission_mode(scheduled.mode))
            return local_actor_cohort() != ExperimentOmissionCohort::none;
        return rotating_actor(proposal) ==
               std::optional<ReplicaID>{scheduled.local_replica};
    }

    ScheduledDecisionState *scheduled_decision(
        const ExperimentByzantineContext &context,
        ExperimentReplicaRole role,
        std::uint64_t monotonic_ns)
    {
        const auto found = scheduled_decisions.find(context.proposal);
        if (found != scheduled_decisions.end())
            return &found->second;

        const auto &scheduled = *options.rotating_omission;
        if (scheduled_decisions.size() >= scheduled.maximum_contexts)
        {
            emit_scheduled_capacity_marker(context, role, monotonic_ns);
            return nullptr;
        }

        ScheduledDecisionState decision;
        decision.physical_role = role;
        const bool inside_window =
            monotonic_ns >= scheduled.window_start_monotonic_ns &&
            monotonic_ns < scheduled.window_end_monotonic_ns;
        if (is_tiered_omission_mode(scheduled.mode) ||
            scheduled.mode == kRoleScopedPersistentOmissionMode)
        {
            decision.cohort = local_actor_cohort();
            bool phase_latch_allows_omission = true;
            if (scheduled.mode == kRoleScopedPersistentOmissionMode &&
                scheduled.first_omission_tree.has_value() &&
                !first_omission_tree_reached)
            {
                phase_latch_allows_omission =
                    inside_window &&
                    decision.cohort == ExperimentOmissionCohort::hard &&
                    role != ExperimentReplicaRole::root &&
                    context.proposal.configuration.epoch_number == 0 &&
                    context.proposal.configuration.tree_id ==
                        *scheduled.first_omission_tree;
                if (phase_latch_allows_omission)
                    first_omission_tree_reached = true;
            }
            decision.auditable =
                inside_window && phase_latch_allows_omission &&
                role != ExperimentReplicaRole::root &&
                decision.cohort != ExperimentOmissionCohort::none;
            if (decision.auditable)
            {
                if (is_role_scoped_omission_mode(scheduled.mode))
                    decision.contribution_role = role;
                bool omit =
                    decision.cohort == ExperimentOmissionCohort::hard;
                if (decision.cohort ==
                    ExperimentOmissionCohort::responsive_degraded)
                {
                    if (responsive_contribution_ordinal ==
                        std::numeric_limits<std::uint64_t>::max())
                    {
                        emit_scheduled_capacity_marker(
                            context, role, monotonic_ns);
                        return nullptr;
                    }
                    decision.contribution_ordinal =
                        ++responsive_contribution_ordinal;
                    if (is_role_scoped_tiered_omission_mode(scheduled.mode))
                    {
                        auto &role_ordinals =
                            responsive_epoch_role_contribution_ordinals
                                [EpochContributionIdentity{
                                    context.proposal.configuration
                                        .epoch_number,
                                    context.proposal.configuration
                                        .epoch_digest}];
                        auto &role_ordinal =
                            role == ExperimentReplicaRole::internal
                                ? role_ordinals.internal
                                : role_ordinals.leaf;
                        if (role_ordinal ==
                            std::numeric_limits<std::uint64_t>::max())
                        {
                            --responsive_contribution_ordinal;
                            emit_scheduled_capacity_marker(
                                context, role, monotonic_ns);
                            return nullptr;
                        }
                        decision.role_contribution_ordinal = ++role_ordinal;
                        omit = decision.role_contribution_ordinal %
                                   scheduled.responsive_omission_period ==
                               0;
                    }
                    else
                    {
                        omit = decision.contribution_ordinal %
                                   scheduled.responsive_omission_period ==
                               0;
                    }
                }
                if (omit && role == ExperimentReplicaRole::internal)
                    decision.action =
                        ExperimentOmissionAction::omit_aggregate;
                else if (omit && role == ExperimentReplicaRole::leaf)
                    decision.action =
                        ExperimentOmissionAction::omit_direct_vote;
            }
        }
        else if (inside_window && local_actor_selected(context.proposal))
        {
            if (role == ExperimentReplicaRole::internal)
                decision.action = ExperimentOmissionAction::omit_aggregate;
            else if (role == ExperimentReplicaRole::leaf)
                decision.action = ExperimentOmissionAction::omit_direct_vote;
        }
        return &scheduled_decisions
                    .emplace(
                        context.proposal,
                        decision)
                    .first->second;
    }

    void populate_tiered_marker(
        ExperimentOmissionMarker &marker,
        ExperimentOmissionCohort cohort,
        std::uint64_t contribution_ordinal,
        ExperimentReplicaRole contribution_role,
        std::uint64_t role_contribution_ordinal) const
    {
        const auto &scheduled = *options.rotating_omission;
        if (!is_tiered_omission_mode(scheduled.mode) &&
            scheduled.mode != kRoleScopedPersistentOmissionMode)
            return;
        marker.cohort = cohort;
        marker.hard_actor_count = scheduled.actor_ids.size();
        marker.responsive_degraded_actor_count =
            scheduled.responsive_degraded_actor_ids.size();
        marker.fault_threshold = scheduled_fault_threshold;
        marker.max_omissions_per_proposal =
            scheduled.max_omissions_per_proposal;
        marker.responsive_omission_period =
            scheduled.responsive_omission_period;
        marker.contribution_ordinal = contribution_ordinal;
        if (is_role_scoped_omission_mode(scheduled.mode))
        {
            marker.contribution_role = contribution_role;
            marker.role_contribution_ordinal = role_contribution_ordinal;
        }
    }

    void populate_role_scoped_marker_identity(
        ExperimentOmissionMarker &marker,
        const ExperimentByzantineContext &context,
        ExperimentReplicaRole role) const
    {
        const auto &scheduled = *options.rotating_omission;
        if (!is_role_scoped_omission_mode(scheduled.mode))
            return;
        marker.view_generation = context.view_generation;
        marker.physical_parent = context.physical_parent;
        marker.authenticated_proposal_source_replica =
            context.authenticated_proposal_source_replica;
        marker.expected_message_type = context.expected_message_type;
        marker.physical_role = role;
    }

    void emit_scheduled_capacity_marker(
        const ExperimentByzantineContext &context,
        ExperimentReplicaRole role,
        std::uint64_t monotonic_ns)
    {
        const auto &scheduled = *options.rotating_omission;
        if (is_role_scoped_omission_mode(scheduled.mode) &&
            role == ExperimentReplicaRole::root)
            return;
        if (scheduled_capacity_marker_emitted)
            return;
        scheduled_capacity_marker_emitted = true;
        if (!options.omission_marker_emitter)
            return;
        ExperimentOmissionMarker marker{
            context.proposal,
            context.diagnostic_window,
            scheduled.mode,
            scheduled.local_replica,
            ExperimentOmissionAction::capacity_exhausted,
            scheduled.window_start_monotonic_ns,
            scheduled.window_end_monotonic_ns,
            monotonic_ns};
        populate_tiered_marker(
            marker, local_actor_cohort(), 0, role, 0);
        populate_role_scoped_marker_identity(marker, context, role);
        options.omission_marker_emitter(marker);
    }

    void emit_scheduled_marker(
        const ExperimentByzantineContext &context,
        ScheduledDecisionState &decision,
        std::uint64_t monotonic_ns)
    {
        if (decision.marker_emitted)
            return;
        decision.marker_emitted = true;
        if (!options.omission_marker_emitter)
            return;
        const auto &scheduled = *options.rotating_omission;
        ExperimentOmissionMarker marker{
            context.proposal,
            context.diagnostic_window,
            scheduled.mode,
            scheduled.local_replica,
            decision.action,
            scheduled.window_start_monotonic_ns,
            scheduled.window_end_monotonic_ns,
            monotonic_ns};
        populate_tiered_marker(
            marker,
            decision.cohort,
            decision.contribution_ordinal,
            decision.contribution_role,
            decision.role_contribution_ordinal);
        populate_role_scoped_marker_identity(
            marker, context, decision.physical_role);
        options.omission_marker_emitter(marker);
    }

    bool static_gate_allows_omission() noexcept
    {
        if (!options.activation_gate.has_value())
            return true;
        const auto &gate = *options.activation_gate;
        const auto gate_bytes = read_regular_file_no_follow(
            gate.path, kMaximumActivationGateBytes);
        if (!gate_bytes.has_value())
            return false;
        const auto required_prefix = std::string{"{\"schema_version\":1,\"kind\":\""} +
            kN7StaticAggregateGateKind + "\",\"profile_sha256\":\"" +
            gate.profile_sha256 + "\",\"tree_file_sha256\":\"" +
            gate.tree_file_sha256 + "\",\"epoch_digest\":\"" +
            options.configuration.epoch_digest.to_hex() + "\",\"replica_id\":" +
            std::to_string(gate.local_replica) + ",\"launch_argv_sha256\":\"" +
            gate.launch_argv_sha256 + "\",\"manager_run_id\":\"" +
            gate.run_id + "\",\"manager_source_instance\":\"" +
            gate.manager_source_instance + "\",\"manager_source_sequence\":";
        const auto hash_prefix = std::string{
            ",\"fault_window_arm_event_sha256\":\""};
        if (gate_bytes->size() < required_prefix.size() + hash_prefix.size() + 97 ||
            gate_bytes->compare(0, required_prefix.size(), required_prefix) != 0 ||
            gate_bytes->back() != '\n')
            return false;
        const auto sequence_end = gate_bytes->find(hash_prefix, required_prefix.size());
        if (sequence_end == std::string::npos)
            return false;
        const auto sequence_text = gate_bytes->substr(
            required_prefix.size(), sequence_end - required_prefix.size());
        if (sequence_text.empty() || sequence_text.front() == '0' || !std::all_of(
                sequence_text.begin(), sequence_text.end(), [](unsigned char value) {
                    return value >= '0' && value <= '9';
                }))
            return false;
        const auto event_hash_start = sequence_end + hash_prefix.size();
        const auto event_hash = gate_bytes->substr(event_hash_start, 64);
        const auto suffix = gate_bytes->substr(event_hash_start + 64);
        const auto activation_prefix = std::string{"\",\"activation_monotonic_ns\":"};
        if (!valid_sha256_hex(event_hash) || suffix.rfind(activation_prefix, 0) != 0 ||
            suffix.size() <= activation_prefix.size() + 1 || suffix.back() != '\n' ||
            suffix[suffix.size() - 2] != '}')
            return false;
        const auto timestamp = suffix.substr(
            activation_prefix.size(), suffix.size() - activation_prefix.size() - 2);
        if (timestamp.empty() || timestamp.front() == '0' || !std::all_of(
                timestamp.begin(), timestamp.end(), [](unsigned char value) {
                    return value >= '0' && value <= '9';
                }))
            return false;
        std::uint64_t activation_ns = 0;
        for (const auto digit : timestamp)
        {
            const auto value = static_cast<std::uint64_t>(digit - '0');
            if (activation_ns >
                (std::numeric_limits<std::uint64_t>::max() - value) / 10)
                return false;
            activation_ns = activation_ns * 10 + value;
        }
        const auto manager = read_regular_file_no_follow(
            gate.manager_event_path, 16 * 1024 * 1024);
        if (!manager.has_value())
            return false;
        bool found = false;
        std::size_t offset = 0;
        while (offset < manager->size())
        {
            const auto end = manager->find('\n', offset);
            if (end == std::string::npos)
                return false;
            const auto line = manager->substr(offset, end - offset);
            const auto envelope_prefix = std::string{
                "{\"event_schema_version\":1,\"run_id\":\""} + gate.run_id +
                "\",\"source_kind\":\"adaptation_manager\",\"source_id\":\"adaptive-manager\",\"source_instance\":\"" +
                gate.manager_source_instance + "\",\"source_sequence\":" +
                sequence_text + ",\"source_monotonic_ns\":";
            const auto payload_prefix = std::string{
                ",\"event_type\":\"fault_window_armed\",\"payload\":"};
            const auto monotonic_start = envelope_prefix.size();
            const auto monotonic_end = line.find(payload_prefix, monotonic_start);
            const bool canonical_envelope = line.rfind(envelope_prefix, 0) == 0 &&
                monotonic_end != std::string::npos && monotonic_end > monotonic_start &&
                std::all_of(line.begin() + static_cast<std::ptrdiff_t>(monotonic_start),
                    line.begin() + static_cast<std::ptrdiff_t>(monotonic_end),
                    [](unsigned char value) { return value >= '0' && value <= '9'; }) &&
                line[monotonic_start] != '0' &&
                line.size() > monotonic_end + payload_prefix.size() && line.back() == '}';
            if (!canonical_envelope)
            {
                offset = end + 1;
                continue;
            }
            const auto payload = line.substr(monotonic_end +
                payload_prefix.size());
            const auto arm_contract = [&]()
                -> std::optional<std::pair<std::string, const char *>> {
                const auto prefix_for = [&gate](const char *profile_id) {
                    return std::string{
                        "{\"schema_version\":4,\"kind\":\"kauri-focused-fault-window-arm-v4\",\"run_id\":\""} +
                        gate.run_id + "\",\"profile_id\":\"" + profile_id +
                        "\",\"profile_sha256\":\"" + gate.profile_sha256 +
                        "\",\"topology_proof_sha256\":\"";
                };
                const auto v2 = prefix_for(kN7ThreeReporterProfileV2);
                if (payload.rfind(v2, 0) == 0)
                    return std::make_pair(
                        std::move(v2), kExactPostFaultAttemptStartBasis);
                const auto v3 = prefix_for(kN7PathTimeoutQuorumProfileV3);
                if (payload.rfind(v3, 0) == 0)
                    return std::make_pair(
                        std::move(v3), kExactPostFaultPathTimeoutQuorumBasis);
                // v4 preserves v3's bounded 3-by-3 timeout evidence quota,
                // while its stronger physical-omission causality requirement
                // is validated from the sealed receipt downstream.  Keep the
                // profile identity explicit so it cannot silently inherit a
                // legacy v2/v3 authorization.
                const auto v4 = prefix_for(kN7PathTimeoutQuorumProfileV4);
                if (payload.rfind(v4, 0) == 0)
                    return std::make_pair(
                        std::move(v4), kExactPostFaultPathTimeoutQuorumBasis);
                return std::nullopt;
            }();
            const auto after_topology = std::string{
                "\",\"request_sha256\":\""};
            const auto after_request = std::string{
                "\",\"epoch_number\":0,\"epoch_digest\":\""} +
                options.configuration.epoch_digest.to_hex() +
                "\",\"fault_receipt_sha256\":\"";
            const auto after_receipt = std::string{
                "\",\"evidence_start_monotonic_ns\":"};
            const auto after_evidence = std::string{
                ",\"prefault_tree_id\":4,\"required_tree_positions\":3,\"required_tree_ids\":[4,5,6],\"clock_domain\":\"same_host_clock_monotonic_raw\",\"required_observation_schema\":3,\"snapshot_evidence_basis\":\""} +
                (arm_contract.has_value() ? arm_contract->second : "") +
                "\",\"selection_cardinality_policy\":\"all_guarded_up_to_fault_bound_v1\",\"timeout_evidence_basis\":\"exact_timeout_attempt_id_v1\",\"fault_window_arm_sha256\":\"";
            const auto canonical_payload = [&]() {
                if (!arm_contract.has_value())
                    return false;
                const bool quota_matches_profile =
                    (arm_contract->second ==
                         kExactPostFaultAttemptStartBasis &&
                     options.maximum_omission_contexts_per_configuration == 2 &&
                     options.maximum_omission_contexts == 6) ||
                    (arm_contract->second ==
                         kExactPostFaultPathTimeoutQuorumBasis &&
                     options.maximum_omission_contexts_per_configuration == 3 &&
                     options.maximum_omission_contexts == 9);
                if (!quota_matches_profile)
                    return false;
                auto position = arm_contract->first.size();
                const auto consume_sha256 = [&payload, &position]() {
                    if (position + 64 > payload.size() ||
                        !valid_sha256_hex(payload.substr(position, 64)))
                        return false;
                    position += 64;
                    return true;
                };
                const auto consume = [&payload, &position](const std::string &literal) {
                    if (payload.compare(position, literal.size(), literal) != 0)
                        return false;
                    position += literal.size();
                    return true;
                };
                if (!consume_sha256() || !consume(after_topology) ||
                    !consume_sha256() || !consume(after_request) ||
                    !consume_sha256() || !consume(after_receipt))
                    return false;
                const auto evidence_start = position;
                while (position < payload.size() &&
                       payload[position] >= '0' && payload[position] <= '9')
                    ++position;
                if (position == evidence_start || payload[evidence_start] == '0' ||
                    !consume(after_evidence) || !consume_sha256())
                    return false;
                return position + 3 == payload.size() &&
                       payload[position] == '"' && payload[position + 1] == '}' &&
                       payload[position + 2] == '}';
            }();
            if (sha256_hex(line) == event_hash && canonical_payload)
            {
                found = true;
                break;
            }
            offset = end + 1;
        }
        if (!found)
            return false;
        const auto gate_sha256 = sha256_hex(*gate_bytes);
        if (static_gate_sha256.has_value() && *static_gate_sha256 != gate_sha256)
            return false;
        if (!static_gate_sha256.has_value())
        {
            if (!options.omission_activation_emitter)
                return false;
            const ExperimentOmissionActivation activation{
                gate.local_replica, gate_sha256, event_hash, activation_ns};
            try { options.omission_activation_emitter(activation); }
            catch (...) { return false; }
            static_gate_sha256 = gate_sha256;
            static_gate_activation = activation;
        }
        return true;
    }

    ExperimentByzantineOptions options;
    std::map<
        ExperimentByzantineContext,
        FalseReportState,
        ContextLess>
        false_reports;
    std::set<ExperimentByzantineContext, ContextLess> omissions;
    std::optional<std::string> static_gate_sha256;
    std::optional<ExperimentOmissionActivation> static_gate_activation;
    std::map<std::uint32_t, std::size_t>
        omission_contexts_by_tree;
    std::set<ExperimentByzantineContext, ContextLess> omission_markers;
    std::set<ExperimentByzantineContext, ContextLess>
        direct_vote_omissions;
    std::map<ProposalKey, ScheduledDecisionState> scheduled_decisions;
    std::uint64_t responsive_contribution_ordinal{0};
    // Tree rotations in one epoch share the two physical-role streams. Late
    // predecessor and future epochs remain isolated by number plus digest.
    std::map<EpochContributionIdentity, RoleContributionOrdinals>
        responsive_epoch_role_contribution_ordinals;
    std::size_t scheduled_fault_threshold{0};
    bool scheduled_capacity_marker_emitted{false};
    bool first_omission_tree_reached{false};
};

ExperimentByzantineAdapter::ExperimentByzantineAdapter(
    ExperimentByzantineOptions options)
    : state_(std::make_unique<State>(std::move(options)))
{}

ExperimentByzantineAdapter::~ExperimentByzantineAdapter() = default;

bool ExperimentByzantineAdapter::arm_false_report(
    const ExperimentByzantineContext &context,
    ReplicaID target)
{
    if (!exact_context(state_->options, context) ||
        !state_->options.false_report_target.has_value() ||
        *state_->options.false_report_target != target)
        return false;

    const auto existing = state_->false_reports.find(context);
    if (existing != state_->false_reports.end())
        return true;
    if (state_->false_reports.size() >=
        state_->options.maximum_false_report_contexts)
        return false;
    return state_->false_reports
        .emplace(context, State::FalseReportState{})
        .second;
}

bool ExperimentByzantineAdapter::on_verified_response(
    const ExperimentByzantineContext &context,
    ReplicaID target) noexcept
{
    if (!state_->options.false_report_target.has_value() ||
        *state_->options.false_report_target != target)
        return false;
    const auto found = state_->false_reports.find(context);
    if (found == state_->false_reports.end())
        return false;
    found->second.verified_response_observed = true;
    return true;
}

bool ExperimentByzantineAdapter::consume_false_report_positive_marker(
    const ExperimentByzantineContext &context,
    ReplicaID target) noexcept
{
    if (!state_->options.false_report_target.has_value() ||
        *state_->options.false_report_target != target)
        return false;
    const auto found = state_->false_reports.find(context);
    if (found == state_->false_reports.end() ||
        !found->second.verified_response_observed ||
        found->second.positive_marker_consumed)
        return false;
    found->second.positive_marker_consumed = true;
    return true;
}

bool ExperimentByzantineAdapter::should_retain_response_evidence(
    const ExperimentByzantineContext &context) const noexcept
{
    const auto found = state_->false_reports.find(context);
    return found != state_->false_reports.end() &&
           found->second.verified_response_observed &&
           !found->second.timeout_consumed;
}

bool ExperimentByzantineAdapter::cancel_false_report(
    const ExperimentByzantineContext &context,
    ReplicaID target) noexcept
{
    if (!state_->options.false_report_target.has_value() ||
        *state_->options.false_report_target != target)
        return false;
    const auto found = state_->false_reports.find(context);
    if (found == state_->false_reports.end())
        return false;
    state_->false_reports.erase(found);
    return true;
}

bool ExperimentByzantineAdapter::consume_false_timeout(
    const ExperimentByzantineContext &context,
    ReplicaID target) noexcept
{
    if (!state_->options.false_report_target.has_value() ||
        *state_->options.false_report_target != target)
        return false;
    const auto found = state_->false_reports.find(context);
    if (found == state_->false_reports.end() ||
        !found->second.verified_response_observed ||
        found->second.timeout_consumed)
        return false;
    found->second.timeout_consumed = true;
    return true;
}

bool ExperimentByzantineAdapter::consume_outbound_aggregate(
    const ExperimentByzantineContext &context,
    ExperimentReplicaRole role,
    std::uint64_t monotonic_ns)
{
    if (state_->options.rotating_omission.has_value())
    {
        if (!state_->options.enabled ||
            context.diagnostic_window != state_->options.diagnostic_window)
            return false;
        auto *decision =
            state_->scheduled_decision(context, role, monotonic_ns);
        if (decision == nullptr)
            return false;
        const bool tiered_audit =
            is_tiered_omission_mode(
                state_->options.rotating_omission->mode) &&
            decision->auditable;
        if (tiered_audit)
            state_->emit_scheduled_marker(
                context, *decision, monotonic_ns);
        if (decision->action != ExperimentOmissionAction::omit_aggregate)
            return false;
        if (!tiered_audit)
            state_->emit_scheduled_marker(
                context, *decision, monotonic_ns);
        return true;
    }
    if (!state_->options.omit_outbound_aggregate ||
        role != ExperimentReplicaRole::internal ||
        !exact_omission_context(state_->options, context))
        return false;
    // The gate is deliberately checked before recording a context: a missing,
    // malformed, replaced, or unbound gate forwards the contribution and does
    // not consume the frozen two-per-tree quota.
    if (!state_->static_gate_allows_omission())
        return false;
    if (state_->omissions.find(context) != state_->omissions.end())
        return true;
    if (state_->omissions.size() >=
        state_->options.maximum_omission_contexts)
        return false;
    if (state_->options.maximum_omission_contexts_per_configuration != 0)
    {
        const auto found = state_->omission_contexts_by_tree.find(
            context.proposal.configuration.tree_id);
        if (found != state_->omission_contexts_by_tree.end() &&
            found->second >=
                state_->options.maximum_omission_contexts_per_configuration)
            return false;
    }
    if (!state_->omissions.insert(context).second)
        return false;
    if (state_->options.maximum_omission_contexts_per_configuration != 0)
        ++state_->omission_contexts_by_tree[
            context.proposal.configuration.tree_id];
    return true;
}

bool ExperimentByzantineAdapter::consume_outbound_aggregate_marker(
    const ExperimentByzantineContext &context) noexcept
{
    if (state_->omissions.find(context) == state_->omissions.end())
        return false;
    return state_->omission_markers.insert(context).second;
}

ExperimentDirectVoteDisposition
ExperimentByzantineAdapter::consume_outbound_direct_vote(
    const ExperimentByzantineContext &context,
    ExperimentReplicaRole role,
    std::uint64_t monotonic_ns)
{
    if (state_->options.rotating_omission.has_value())
    {
        if (!state_->options.enabled ||
            context.diagnostic_window != state_->options.diagnostic_window)
            return ExperimentDirectVoteDisposition::forward;
        auto *decision =
            state_->scheduled_decision(context, role, monotonic_ns);
        if (decision == nullptr)
            return ExperimentDirectVoteDisposition::forward;
        const bool tiered_audit =
            is_tiered_omission_mode(
                state_->options.rotating_omission->mode) &&
            decision->auditable;
        if (tiered_audit)
            state_->emit_scheduled_marker(
                context, *decision, monotonic_ns);
        if (decision->action != ExperimentOmissionAction::omit_direct_vote)
            return ExperimentDirectVoteDisposition::forward;
        if (decision->direct_vote_omitted)
            return ExperimentDirectVoteDisposition::omit_repeat;
        decision->direct_vote_omitted = true;
        if (!tiered_audit)
            state_->emit_scheduled_marker(
                context, *decision, monotonic_ns);
        return ExperimentDirectVoteDisposition::omit_first;
    }
    if (!state_->options.omit_outbound_direct_vote ||
        role != ExperimentReplicaRole::leaf ||
        !exact_context(state_->options, context))
        return ExperimentDirectVoteDisposition::forward;
    if (state_->direct_vote_omissions.find(context) !=
        state_->direct_vote_omissions.end())
        return ExperimentDirectVoteDisposition::omit_repeat;
    if (state_->direct_vote_omissions.size() >=
        state_->options.maximum_direct_vote_omission_contexts)
        return ExperimentDirectVoteDisposition::forward;
    if (!state_->direct_vote_omissions.insert(context).second)
        return ExperimentDirectVoteDisposition::forward;
    return ExperimentDirectVoteDisposition::omit_first;
}

bool ExperimentByzantineAdapter::outbound_direct_vote_omitted(
    const ExperimentByzantineContext &context) const noexcept
{
    const auto scheduled =
        state_->scheduled_decisions.find(context.proposal);
    if (scheduled != state_->scheduled_decisions.end() &&
        scheduled->second.direct_vote_omitted)
        return true;
    return state_->direct_vote_omissions.find(context) !=
           state_->direct_vote_omissions.end();
}

std::optional<ReplicaID>
ExperimentByzantineAdapter::rotating_omission_actor(
    const ProposalKey &proposal) const
{
    return state_->rotating_actor(proposal);
}

bool ExperimentByzantineAdapter::rotating_omission_enabled() const noexcept
{
    return state_->options.enabled &&
           state_->options.rotating_omission.has_value() &&
           is_rotating_omission_mode(
               state_->options.rotating_omission->mode);
}

bool ExperimentByzantineAdapter::scheduled_omission_enabled() const noexcept
{
    return state_->options.enabled &&
           state_->options.rotating_omission.has_value() &&
           is_scheduled_omission_mode(
               state_->options.rotating_omission->mode);
}

std::optional<ExperimentOmissionActivation>
ExperimentByzantineAdapter::active_static_omission_gate() const noexcept
{
    return state_->static_gate_activation;
}

bool ExperimentByzantineAdapter::
is_tiered_responsive_degraded_actor(ReplicaID replica) const noexcept
{
    if (!state_->options.enabled ||
        !state_->options.rotating_omission.has_value())
        return false;
    const auto &scheduled = *state_->options.rotating_omission;
    return is_tiered_omission_mode(scheduled.mode) &&
           std::binary_search(
               scheduled.responsive_degraded_actor_ids.begin(),
               scheduled.responsive_degraded_actor_ids.end(),
               replica);
}

} // namespace hotstuff
