#include "hotstuff/experiment_post_qc_audit.h"

#include "hotstuff/consensus.h"

#include <algorithm>
#include <limits>
#include <mutex>
#include <stdexcept>
#include <utility>

namespace hotstuff
{
namespace
{

bool safe_window(const std::string &window) noexcept
{
    if (window.empty() ||
        window.size() > kExperimentPostQcAuditMaximumWindowBytes)
        return false;
    return std::all_of(
        window.begin(),
        window.end(),
        [](unsigned char character)
        {
            return (character >= 'a' && character <= 'z') ||
                   (character >= 'A' && character <= 'Z') ||
                   (character >= '0' && character <= '9') ||
                   character == '-' || character == '_' ||
                   character == '.';
        });
}

std::set<ReplicaID> signer_set(const QuorumCert &certificate)
{
    const auto raw = certificate.get_signers();
    return std::set<ReplicaID>(raw.begin(), raw.end());
}

bool signer_set_is_canonical(
    const QuorumCert &certificate,
    bool allow_empty = false) noexcept
{
    try
    {
        const auto raw = certificate.get_signers();
        if ((!allow_empty && raw.empty()) ||
            !std::is_sorted(raw.begin(), raw.end()) ||
            std::adjacent_find(raw.begin(), raw.end()) != raw.end())
            return false;
        return true;
    }
    catch (...)
    {
        return false;
    }
}

bool subset_of(
    const std::set<ReplicaID> &candidate,
    const std::set<ReplicaID> &bound) noexcept
{
    return std::includes(
        bound.begin(), bound.end(), candidate.begin(), candidate.end());
}

std::uint64_t checked_add_ms(
    std::uint64_t base_ns,
    std::uint64_t milliseconds)
{
    constexpr auto ns_per_ms = std::uint64_t{1'000'000};
    if (milliseconds >
        (std::numeric_limits<std::uint64_t>::max() - base_ns) /
            ns_per_ms)
        throw std::overflow_error("post-QC audit deadline overflow");
    return base_ns + milliseconds * ns_per_ms;
}

bytearray_t serialize_certificate(const QuorumCert &certificate)
{
    DataStream stream;
    const_cast<QuorumCert &>(certificate).serialize(stream);
    return static_cast<bytearray_t>(stream);
}

} // namespace

ExperimentPostQcAuditRelay::ExperimentPostQcAuditRelay(
    const ExperimentPostQcAuditRelay &other)
    : proposal(other.proposal),
      generation(other.generation),
      diagnostic_window(other.diagnostic_window),
      reporter(other.reporter),
      target(other.target),
      root(other.root),
      armed_ns(other.armed_ns),
      deadline_ns(other.deadline_ns),
      emitted_ns(other.emitted_ns),
      certificate(other.certificate == nullptr
                      ? nullptr
                      : other.certificate->clone())
{}

ExperimentPostQcAuditRelay &ExperimentPostQcAuditRelay::operator=(
    const ExperimentPostQcAuditRelay &other)
{
    if (this == &other)
        return *this;
    ExperimentPostQcAuditRelay replacement(other);
    *this = std::move(replacement);
    return *this;
}

void ExperimentPostQcAuditRelay::serialize(DataStream &stream) const
{
    if (certificate == nullptr ||
        diagnostic_window.empty() ||
        diagnostic_window.size() >
            kExperimentPostQcAuditMaximumWindowBytes)
        throw std::invalid_argument(
            "post-QC audit relay is not serializable");
    const auto window_size = static_cast<std::uint16_t>(
        diagnostic_window.size());
    stream << htole(kExperimentPostQcAuditWireSchemaVersion)
           << htole(proposal.configuration.epoch_number)
           << htole(proposal.configuration.tree_id)
           << proposal.configuration.epoch_digest
           << proposal.block_hash
           << htole(generation)
           << htole(reporter)
           << htole(target)
           << htole(root)
           << htole(armed_ns)
           << htole(deadline_ns)
           << htole(emitted_ns)
           << htole(window_size)
           << diagnostic_window
           << *certificate;
    if (stream.size() > kExperimentPostQcAuditMaximumWireBytes)
        throw std::invalid_argument(
            "post-QC audit relay exceeds the wire bound");
}

bool ExperimentPostQcAuditRelay::parse(
    DataStream &stream,
    HotStuffCore *core) noexcept
{
    if (core == nullptr ||
        stream.size() > kExperimentPostQcAuditMaximumWireBytes)
        return false;
    try
    {
        std::uint32_t schema{0};
        std::uint16_t window_size{0};
        stream >> schema
               >> proposal.configuration.epoch_number
               >> proposal.configuration.tree_id
               >> proposal.configuration.epoch_digest
               >> proposal.block_hash
               >> generation
               >> reporter
               >> target
               >> root
               >> armed_ns
               >> deadline_ns
               >> emitted_ns
               >> window_size;
        schema = letoh(schema);
        proposal.configuration.epoch_number =
            letoh(proposal.configuration.epoch_number);
        proposal.configuration.tree_id =
            letoh(proposal.configuration.tree_id);
        generation = letoh(generation);
        reporter = letoh(reporter);
        target = letoh(target);
        root = letoh(root);
        armed_ns = letoh(armed_ns);
        deadline_ns = letoh(deadline_ns);
        emitted_ns = letoh(emitted_ns);
        window_size = letoh(window_size);
        if (schema != kExperimentPostQcAuditWireSchemaVersion ||
            window_size == 0 ||
            window_size > kExperimentPostQcAuditMaximumWindowBytes ||
            window_size > stream.size())
            return false;
        const auto *window = stream.get_data_inplace(window_size);
        diagnostic_window.assign(
            reinterpret_cast<const char *>(window), window_size);
        if (!safe_window(diagnostic_window))
            return false;
        // PQAR deliberately has its own wire message. Bound the BLS signer
        // bitmap before handing untrusted bytes to the certificate parser so
        // an authenticated experiment reporter cannot force an oversized
        // allocation at the root.
        DataStream certificate_wire(stream);
        auto expected_type = core->create_quorum_cert(ProposalKey{});
        if (dynamic_cast<QuorumCertAggBLS *>(expected_type.get()) == nullptr)
            return false;
        ProposalKey certificate_key;
        unserialize_proposal_key(certificate_wire, certificate_key);
        if (certificate_key != proposal)
            return false;
        std::uint32_t encoded_bits{0};
        certificate_wire >> encoded_bits;
        const auto bit_count = static_cast<std::size_t>(
            letoh(encoded_bits));
        if (bit_count == 0 ||
            bit_count != core->get_config().nreplicas)
            return false;
        constexpr std::size_t kBitsPerWord =
            sizeof(std::uint64_t) * 8;
        const auto word_count =
            (bit_count + kBitsPerWord - 1) / kBitsPerWord;
        if (word_count >
            certificate_wire.size() / sizeof(std::uint64_t))
            return false;

        certificate = core->parse_quorum_cert(stream);
        return certificate != nullptr && stream.size() == 0 &&
               certificate->get_proposal_key() == proposal &&
               certificate->get_sigs_n() > 0 &&
               certificate->get_sigs_n() <=
                   core->get_config().nreplicas;
    }
    catch (...)
    {
        certificate = nullptr;
        return false;
    }
}

struct ExperimentPostQcAudit::State
{
    struct ReporterState
    {
        ProposalKey proposal;
        std::uint64_t generation{0};
        ProposalTreeSnapshot tree;
        std::set<ReplicaID> allowed_signers;
        quorum_cert_bt accumulator;
        std::uint64_t armed_ns{0};
        std::uint64_t deadline_ns{0};
        std::uint64_t closed_ns{0};
        bool closed{false};
        bool target_marker_emitted{false};
        bool target_verification_in_flight{false};
        ExperimentPostQcAuditTargetPhase target_verification_phase{
            ExperimentPostQcAuditTargetPhase::open};
        std::uint64_t target_arrival_ns{0};
        bool incomplete{false};
        bool deadline_consumed{false};
        bool relay_attempted{false};
        bool relay_sent{false};
        bool terminal{false};
    };

    struct RootState
    {
        ExperimentPostQcAuditRootSnapshot snapshot;
        bool active{false};
        bool verification_attempted{false};
        bool verification_in_flight{false};
        std::uint64_t verification_received_ns{0};
        bool accepted{false};
        bool terminal{false};
    };

    State(ExperimentPostQcAuditOptions configured, ReplicaID local)
        : options(std::move(configured)), local_replica(local)
    {
        if (!options.enabled)
            return;
        if (options.configuration.epoch_digest.is_null() ||
            !safe_window(options.diagnostic_window))
            throw std::invalid_argument(
                "post-QC audit exact configuration or window is invalid");
        if (options.reporter == options.target ||
            options.reporter == options.root ||
            options.target == options.root)
            throw std::invalid_argument(
                "post-QC audit reporter, target, and root must be distinct");
        if (options.deadline_ms !=
                kExperimentPostQcAuditDeadlineMs ||
            options.retention_ms !=
                kExperimentPostQcAuditRetentionMs ||
            options.maximum_contexts !=
                kExperimentPostQcAuditContextLimit)
            throw std::invalid_argument(
                "post-QC audit requires deadline 150 ms, retention 250 ms, "
                "and context limit 1");
    }

    ExperimentPostQcAuditOptions options;
    ReplicaID local_replica{0};
    mutable std::mutex mutex;
    std::optional<ReporterState> reporter;
    std::optional<RootState> root;
};

ExperimentPostQcAudit::ExperimentPostQcAudit(
    ExperimentPostQcAuditOptions options,
    ReplicaID local_replica)
    : state_(std::make_unique<State>(
          std::move(options), local_replica))
{}

ExperimentPostQcAudit::~ExperimentPostQcAudit() = default;

bool ExperimentPostQcAudit::enabled() const noexcept
{
    return state_->options.enabled;
}

bool ExperimentPostQcAudit::is_reporter() const noexcept
{
    return enabled() &&
           state_->local_replica == state_->options.reporter;
}

bool ExperimentPostQcAudit::is_root() const noexcept
{
    return enabled() && state_->local_replica == state_->options.root;
}

const ExperimentPostQcAuditOptions &
ExperimentPostQcAudit::options() const noexcept
{
    return state_->options;
}

bool ExperimentPostQcAudit::arm_reporter(
    const ProposalKey &proposal,
    std::uint64_t generation,
    const ProposalTreeSnapshot &tree,
    const QuorumCert &verified_accumulator,
    std::uint64_t armed_ns)
{
    if (!is_reporter() ||
        proposal.configuration != state_->options.configuration ||
        tree.local_replica != state_->options.reporter ||
        tree.root != state_->options.root ||
        armed_ns == 0 ||
        tree.child_subtrees.count(state_->options.target) == 0 ||
        verified_accumulator.get_proposal_key() != proposal ||
        !signer_set_is_canonical(verified_accumulator, true))
        return false;

    auto allowed = std::set<ReplicaID>(
        tree.assigned_subtree.begin(), tree.assigned_subtree.end());
    const auto initial_signers = signer_set(verified_accumulator);
    if (allowed.count(state_->options.reporter) == 0 ||
        allowed.count(state_->options.target) == 0 ||
        !subset_of(initial_signers, allowed))
        return false;

    quorum_cert_bt clone;
    std::uint64_t deadline_ns{0};
    try
    {
        clone = const_cast<QuorumCert &>(
                    verified_accumulator)
                    .clone();
        deadline_ns = checked_add_ms(
            armed_ns, state_->options.deadline_ms);
    }
    catch (...)
    {
        return false;
    }
    if (clone == nullptr)
        return false;

    std::lock_guard<std::mutex> lock(state_->mutex);
    if (state_->reporter.has_value())
        return state_->reporter->proposal == proposal &&
               state_->reporter->generation == generation;
    state_->reporter.emplace(State::ReporterState{
        proposal,
        generation,
        tree,
        std::move(allowed),
        std::move(clone),
        armed_ns,
        deadline_ns});
    return true;
}

std::optional<ExperimentPostQcAuditTargetObservation>
ExperimentPostQcAudit::synchronize_reporter_accumulator(
    const ProposalKey &proposal,
    std::uint64_t generation,
    const QuorumCert &verified_accumulator,
    ReplicaID authenticated_sender,
    std::uint64_t arrival_ns)
{
    if (!is_reporter() ||
        verified_accumulator.get_proposal_key() != proposal ||
        !signer_set_is_canonical(verified_accumulator, true))
        return std::nullopt;
    const auto next_signers = signer_set(verified_accumulator);
    quorum_cert_bt clone;
    try
    {
        clone = const_cast<QuorumCert &>(
                    verified_accumulator)
                    .clone();
    }
    catch (...)
    {
        return std::nullopt;
    }
    if (clone == nullptr)
        return std::nullopt;

    std::lock_guard<std::mutex> lock(state_->mutex);
    auto &reporter = state_->reporter;
    if (!reporter.has_value() || reporter->terminal ||
        reporter->closed || reporter->deadline_consumed ||
        reporter->proposal != proposal ||
        reporter->generation != generation ||
        arrival_ns >= reporter->deadline_ns ||
        !subset_of(next_signers, reporter->allowed_signers))
        return std::nullopt;
    const auto previous_signers = signer_set(*reporter->accumulator);
    if (!subset_of(previous_signers, next_signers))
        return std::nullopt;
    reporter->accumulator = std::move(clone);
    if (authenticated_sender != state_->options.target ||
        next_signers.count(state_->options.target) == 0 ||
        reporter->target_marker_emitted)
        return std::nullopt;
    reporter->target_marker_emitted = true;
    return ExperimentPostQcAuditTargetObservation{
        ExperimentPostQcAuditTargetPhase::open,
        proposal,
        generation,
        reporter->armed_ns,
        reporter->deadline_ns,
        arrival_ns,
        std::move(next_signers)};
}

bool ExperimentPostQcAudit::close_reporter_context(
    const ProposalKey &proposal,
    std::uint64_t generation,
    std::uint64_t closed_ns) noexcept
{
    std::lock_guard<std::mutex> lock(state_->mutex);
    if (!state_->reporter.has_value() ||
        state_->reporter->proposal != proposal ||
        state_->reporter->generation != generation ||
        state_->reporter->terminal)
        return false;
    state_->reporter->closed = true;
    state_->reporter->closed_ns = closed_ns;
    return true;
}

bool ExperimentPostQcAudit::accepts_post_close_target(
    const ProposalKey &proposal,
    std::uint64_t generation,
    ReplicaID authenticated_sender,
    std::uint64_t arrival_ns) const noexcept
{
    std::lock_guard<std::mutex> lock(state_->mutex);
    return is_reporter() && state_->reporter.has_value() &&
           state_->reporter->closed &&
           !state_->reporter->terminal &&
           !state_->reporter->deadline_consumed &&
           state_->reporter->proposal == proposal &&
           state_->reporter->generation == generation &&
           authenticated_sender == state_->options.target &&
           arrival_ns < state_->reporter->deadline_ns;
}

bool ExperimentPostQcAudit::begin_target_verification(
    const ProposalKey &proposal,
    std::uint64_t generation,
    ReplicaID authenticated_sender,
    ExperimentPostQcAuditTargetPhase phase,
    std::uint64_t arrival_ns)
{
    std::lock_guard<std::mutex> lock(state_->mutex);
    if (!is_reporter() || !state_->reporter.has_value() ||
        state_->reporter->terminal ||
        state_->reporter->deadline_consumed ||
        state_->reporter->target_verification_in_flight ||
        state_->reporter->proposal != proposal ||
        state_->reporter->generation != generation ||
        authenticated_sender != state_->options.target ||
        arrival_ns >= state_->reporter->deadline_ns ||
        (phase == ExperimentPostQcAuditTargetPhase::open &&
         state_->reporter->closed) ||
        (phase == ExperimentPostQcAuditTargetPhase::post_close &&
         !state_->reporter->closed) ||
        signer_set(*state_->reporter->accumulator).count(
            state_->options.target) != 0)
        return false;
    state_->reporter->target_verification_in_flight = true;
    state_->reporter->target_verification_phase = phase;
    state_->reporter->target_arrival_ns = arrival_ns;
    return true;
}

std::optional<ExperimentPostQcAuditTargetObservation>
ExperimentPostQcAudit::complete_target_verification(
    const ProposalKey &proposal,
    std::uint64_t generation,
    ReplicaID authenticated_sender,
    const ReplicaConfig &configuration,
    const PartCert &part,
    bool verified)
{
    std::lock_guard<std::mutex> lock(state_->mutex);
    auto &reporter = state_->reporter;
    if (!is_reporter() || !reporter.has_value() ||
        !reporter->target_verification_in_flight ||
        reporter->proposal != proposal ||
        reporter->generation != generation ||
        authenticated_sender != state_->options.target)
        return std::nullopt;
    const auto phase = reporter->target_verification_phase;
    const auto arrival_ns = reporter->target_arrival_ns;
    reporter->target_verification_in_flight = false;
    reporter->target_arrival_ns = 0;
    if (!verified || reporter->terminal ||
        reporter->deadline_consumed ||
        part.get_proposal_key() != proposal ||
        arrival_ns >= reporter->deadline_ns)
        return std::nullopt;
    auto before = signer_set(*reporter->accumulator);
    if (before.count(state_->options.target) != 0)
        return std::nullopt;
    quorum_cert_bt next;
    try
    {
        next = reporter->accumulator->clone();
        next->add_part(
            configuration,
            state_->options.target,
            part);
    }
    catch (...)
    {
        return std::nullopt;
    }
    if (next == nullptr || next->get_proposal_key() != proposal ||
        !signer_set_is_canonical(*next))
        return std::nullopt;
    auto after = signer_set(*next);
    before.insert(state_->options.target);
    if (after != before ||
        !subset_of(after, reporter->allowed_signers))
        return std::nullopt;
    reporter->accumulator = std::move(next);
    if (reporter->target_marker_emitted)
        return std::nullopt;
    reporter->target_marker_emitted = true;
    return ExperimentPostQcAuditTargetObservation{
        phase,
        proposal,
        generation,
        reporter->armed_ns,
        reporter->deadline_ns,
        arrival_ns,
        std::move(after)};
}

std::optional<ExperimentPostQcAuditTargetObservation>
ExperimentPostQcAudit::record_verified_post_close_target(
    const ProposalKey &proposal,
    std::uint64_t generation,
    ReplicaID authenticated_sender,
    const ReplicaConfig &configuration,
    const PartCert &verified_part,
    std::uint64_t arrival_ns)
{
    if (!begin_target_verification(
            proposal,
            generation,
            authenticated_sender,
            ExperimentPostQcAuditTargetPhase::post_close,
            arrival_ns))
        return std::nullopt;
    return complete_target_verification(
        proposal,
        generation,
        authenticated_sender,
        configuration,
        verified_part,
        true);
}

std::optional<ExperimentPostQcAuditMissingClaim>
ExperimentPostQcAudit::consume_reporter_deadline(
    std::uint64_t now_ns)
{
    std::lock_guard<std::mutex> lock(state_->mutex);
    auto &reporter = state_->reporter;
    if (!is_reporter() || !reporter.has_value() ||
        reporter->terminal || reporter->deadline_consumed ||
        now_ns < reporter->deadline_ns)
        return std::nullopt;
    reporter->deadline_consumed = true;
    if (reporter->target_verification_in_flight)
    {
        reporter->incomplete = true;
        reporter->terminal = true;
        return std::nullopt;
    }
    auto signers = signer_set(*reporter->accumulator);
    const bool target_present =
        signers.count(state_->options.target) != 0;
    if (target_present && !state_->options.forge_missing_claim)
    {
        reporter->terminal = true;
        return std::nullopt;
    }
    quorum_cert_bt certificate;
    try
    {
        certificate = reporter->accumulator->clone();
    }
    catch (...)
    {
        reporter->terminal = true;
        return std::nullopt;
    }
    if (certificate == nullptr || signers.empty())
    {
        reporter->terminal = true;
        return std::nullopt;
    }
    reporter->relay_attempted = true;
    ExperimentPostQcAuditRelay relay;
    relay.proposal = reporter->proposal;
    relay.generation = reporter->generation;
    relay.diagnostic_window = state_->options.diagnostic_window;
    relay.reporter = state_->options.reporter;
    relay.target = state_->options.target;
    relay.root = state_->options.root;
    relay.armed_ns = reporter->armed_ns;
    relay.deadline_ns = reporter->deadline_ns;
    relay.emitted_ns = now_ns;
    relay.certificate = std::move(certificate);
    return ExperimentPostQcAuditMissingClaim{
        std::move(relay), std::move(signers)};
}

bool ExperimentPostQcAudit::complete_reporter_relay(
    bool sent) noexcept
{
    std::lock_guard<std::mutex> lock(state_->mutex);
    if (!state_->reporter.has_value() ||
        !state_->reporter->relay_attempted ||
        state_->reporter->terminal)
        return false;
    state_->reporter->relay_sent = sent;
    state_->reporter->terminal = true;
    return sent;
}

std::optional<ExperimentPostQcAuditRootSnapshot>
ExperimentPostQcAudit::prepare_root(
    const ProposalKey &proposal,
    std::uint64_t generation,
    const ProposalTreeSnapshot &tree,
    const QuorumCert &verified_qc,
    std::size_t frozen_global_quorum,
    std::uint64_t prepared_ns)
{
    if (!is_root() ||
        proposal.configuration != state_->options.configuration ||
        tree.local_replica != state_->options.root ||
        tree.root != state_->options.root || tree.parent.has_value() ||
        frozen_global_quorum == 0 || prepared_ns == 0 ||
        verified_qc.get_proposal_key() != proposal ||
        !signer_set_is_canonical(verified_qc))
        return std::nullopt;
    const auto reporter_branch =
        tree.child_subtrees.find(state_->options.reporter);
    if (reporter_branch == tree.child_subtrees.end() ||
        reporter_branch->second.count(state_->options.target) == 0)
        return std::nullopt;

    ExperimentPostQcAuditRootSnapshot snapshot;
    try
    {
        snapshot.proposal = proposal;
        snapshot.generation = generation;
        snapshot.prepared_ns = prepared_ns;
        snapshot.reporter_subtree = reporter_branch->second;
        snapshot.reporter_subtree.insert(state_->options.reporter);
        snapshot.qc_signers = signer_set(verified_qc);
        snapshot.frozen_qc = serialize_certificate(verified_qc);
    }
    catch (...)
    {
        return std::nullopt;
    }
    const bool qc_overlaps_reporter_subtree = std::any_of(
        snapshot.qc_signers.begin(),
        snapshot.qc_signers.end(),
        [&snapshot](ReplicaID signer)
        {
            return snapshot.reporter_subtree.count(signer) != 0;
        });
    auto root_members = std::set<ReplicaID>(
        tree.assigned_subtree.begin(), tree.assigned_subtree.end());
    root_members.insert(state_->options.root);
    if (snapshot.qc_signers.size() < frozen_global_quorum ||
        qc_overlaps_reporter_subtree ||
        !subset_of(snapshot.qc_signers, root_members) ||
        snapshot.frozen_qc.empty())
        return std::nullopt;

    std::lock_guard<std::mutex> lock(state_->mutex);
    if (state_->root.has_value())
        return std::nullopt;
    state_->root.emplace(State::RootState{snapshot});
    return snapshot;
}

bool ExperimentPostQcAudit::activate_root(
    const ProposalKey &proposal,
    std::uint64_t generation,
    const QuorumCert &published_qc,
    std::uint64_t published_ns,
    bool consensus_context_terminal) noexcept
{
    bytearray_t published_qc_bytes;
    try
    {
        if (published_qc.get_proposal_key() != proposal ||
            !signer_set_is_canonical(published_qc))
            return false;
        published_qc_bytes = serialize_certificate(published_qc);
    }
    catch (...)
    {
        return false;
    }
    std::lock_guard<std::mutex> lock(state_->mutex);
    if (!is_root() || !state_->root.has_value() ||
        state_->root->snapshot.proposal != proposal ||
        state_->root->snapshot.generation != generation ||
        state_->root->active || state_->root->terminal ||
        !consensus_context_terminal || published_ns == 0 ||
        published_ns < state_->root->snapshot.prepared_ns)
        return false;
    if (published_qc_bytes != state_->root->snapshot.frozen_qc)
    {
        state_->root->terminal = true;
        return false;
    }
    try
    {
        state_->root->snapshot.published_ns = published_ns;
        state_->root->snapshot.expiry_ns = checked_add_ms(
            published_ns, state_->options.retention_ms);
    }
    catch (...)
    {
        state_->root->terminal = true;
        return false;
    }
    state_->root->active = true;
    return true;
}

std::optional<ExperimentPostQcAuditRootVerification>
ExperimentPostQcAudit::begin_root_verification(
    const ExperimentPostQcAuditRelay &relay,
    ReplicaID authenticated_sender,
    std::uint64_t received_ns)
{
    std::uint64_t expected_deadline_ns{0};
    try
    {
        expected_deadline_ns = checked_add_ms(
            relay.armed_ns, state_->options.deadline_ms);
    }
    catch (...)
    {
        return std::nullopt;
    }
    std::lock_guard<std::mutex> lock(state_->mutex);
    auto &root = state_->root;
    if (!is_root() || !root.has_value() || !root->active ||
        root->terminal || root->verification_attempted ||
        relay.certificate == nullptr ||
        relay.proposal != root->snapshot.proposal ||
        relay.generation != root->snapshot.generation ||
        relay.diagnostic_window != state_->options.diagnostic_window ||
        relay.reporter != state_->options.reporter ||
        relay.target != state_->options.target ||
        relay.root != state_->options.root ||
        authenticated_sender != state_->options.reporter ||
        relay.armed_ns == 0 || relay.deadline_ns == 0 ||
        relay.deadline_ns != expected_deadline_ns ||
        // The frozen campaign runs every replica process on one host and
        // therefore shares the kernel monotonic clock domain. Cross-host
        // deployment would require a different chronology representation.
        relay.armed_ns > root->snapshot.prepared_ns ||
        root->snapshot.published_ns >= relay.deadline_ns ||
        relay.emitted_ns < relay.deadline_ns ||
        received_ns < root->snapshot.published_ns ||
        received_ns < relay.emitted_ns ||
        relay.emitted_ns >= root->snapshot.expiry_ns ||
        received_ns >= root->snapshot.expiry_ns ||
        relay.certificate->get_proposal_key() != relay.proposal ||
        !signer_set_is_canonical(*relay.certificate))
        return std::nullopt;
    auto audit_signers = signer_set(*relay.certificate);
    if (audit_signers.empty() ||
        audit_signers.size() > root->snapshot.reporter_subtree.size() ||
        !subset_of(audit_signers, root->snapshot.reporter_subtree))
        return std::nullopt;
    root->verification_attempted = true;
    root->verification_in_flight = true;
    root->verification_received_ns = received_ns;
    return ExperimentPostQcAuditRootVerification{
        relay, root->snapshot, std::move(audit_signers), received_ns};
}

bool ExperimentPostQcAudit::complete_root_verification(
    const ProposalKey &proposal,
    std::uint64_t generation,
    std::uint64_t verified_ns,
    bool certificate_verified,
    bool consensus_context_terminal,
    bool qc_unchanged) noexcept
{
    std::lock_guard<std::mutex> lock(state_->mutex);
    if (!state_->root.has_value() ||
        state_->root->snapshot.proposal != proposal ||
        state_->root->snapshot.generation != generation ||
        !state_->root->verification_in_flight ||
        state_->root->terminal)
        return false;
    state_->root->verification_in_flight = false;
    const bool timing_valid =
        state_->root->verification_received_ns != 0 &&
        state_->root->verification_received_ns <= verified_ns &&
        verified_ns < state_->root->snapshot.expiry_ns;
    state_->root->accepted = certificate_verified &&
        consensus_context_terminal && qc_unchanged && timing_valid;
    state_->root->terminal = true;
    return state_->root->accepted;
}

bool ExperimentPostQcAudit::expire(std::uint64_t now_ns) noexcept
{
    std::lock_guard<std::mutex> lock(state_->mutex);
    if (!state_->root.has_value() || !state_->root->active ||
        state_->root->terminal ||
        now_ns < state_->root->snapshot.expiry_ns)
        return false;
    state_->root->verification_in_flight = false;
    state_->root->terminal = true;
    return true;
}

std::optional<ExperimentPostQcAuditRootSnapshot>
ExperimentPostQcAudit::root_snapshot() const
{
    std::lock_guard<std::mutex> lock(state_->mutex);
    if (!state_->root.has_value())
        return std::nullopt;
    return state_->root->snapshot;
}

ExperimentPostQcAuditDiagnostics
ExperimentPostQcAudit::diagnostics() const noexcept
{
    std::lock_guard<std::mutex> lock(state_->mutex);
    ExperimentPostQcAuditDiagnostics result;
    result.enabled = enabled();
    if (state_->reporter.has_value())
    {
        result.reporter_armed = true;
        result.reporter_closed = state_->reporter->closed;
        result.reporter_target_verification_in_flight =
            state_->reporter->target_verification_in_flight;
        result.reporter_incomplete = state_->reporter->incomplete;
        result.reporter_deadline_consumed =
            state_->reporter->deadline_consumed;
        result.reporter_relay_attempted =
            state_->reporter->relay_attempted;
        result.reporter_relay_sent = state_->reporter->relay_sent;
        result.reporter_terminal = state_->reporter->terminal;
    }
    if (state_->root.has_value())
    {
        result.root_prepared = true;
        result.root_active = state_->root->active;
        result.root_verification_attempted =
            state_->root->verification_attempted;
        result.root_verification_in_flight =
            state_->root->verification_in_flight;
        result.root_accepted = state_->root->accepted;
        result.root_terminal = state_->root->terminal;
    }
    return result;
}

const char *to_string(
    ExperimentPostQcAuditTargetPhase phase) noexcept
{
    switch (phase)
    {
        case ExperimentPostQcAuditTargetPhase::open:
            return "open";
        case ExperimentPostQcAuditTargetPhase::post_close:
            return "post_close";
    }
    return "unknown";
}

} // namespace hotstuff
