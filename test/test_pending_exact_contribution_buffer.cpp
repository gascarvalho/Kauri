#include <cctype>
#include <cstddef>
#include <cstdint>
#include <fstream>
#include <sstream>
#include <string>
#include <type_traits>
#include <utility>

#include "catch.hpp"
#include "hotstuff/exact_vote_handler.h"

#ifndef KAURI_PROJECT_SOURCE_DIR
#define KAURI_PROJECT_SOURCE_DIR "."
#endif

/*
 * REM-A06-02 admission-window contribution contract
 * -------------------------------------------------
 * A proposal becomes visible in ProposalAdmissionCoordinator before the
 * asynchronous block-delivery continuation opens its ProposalContext. A vote
 * or relay received in that narrow interval must not be discarded and must
 * not start crypto or delivery early. The production contract is:
 *
 *   struct PendingExactContribution {
 *       ExactContributionKind kind;
 *       ExactContributionEnvelope envelope;
 *       PeerId authenticated_source;
 *       uint256_t fingerprint;
 *   };
 *
 *   struct PendingExactContributionBufferLimits {
 *       std::size_t global_capacity;
 *       std::size_t per_key_capacity;
 *   };
 *
 *   enum class PendingExactContributionInsertResult {
 *       inserted,
 *       duplicate,
 *       per_key_full,
 *       global_full
 *   };
 *
 *   class PendingExactContributionBuffer {
 *   public:
 *       explicit PendingExactContributionBuffer(
 *           PendingExactContributionBufferLimits);
 *       PendingExactContributionInsertResult insert(
 *           PendingExactContribution);
 *       std::size_t size() const noexcept;
 *       std::size_t size(const ProposalKey &) const noexcept;
 *       std::vector<PendingExactContribution> drain(const ProposalKey &);
 *       std::size_t purge(const ProposalKey &);
 *       std::size_t purge_block(const uint256_t &);
 *       std::size_t purge_configuration(const ConfigurationId &);
 *       std::size_t purge_epoch(std::uint32_t);
 *       std::size_t purge_before_epoch(std::uint32_t);
 *       void clear() noexcept;
 *   };
 *
 * Duplicate identity is (exact ProposalKey, fingerprint), never a bare block
 * hash. Bounds reject the
 * newest item and never evict or reorder accepted items. Entries retain the
 * owned Vote/VoteRelay envelope and authenticated transport source. Handler
 * eligibility is deliberately outside this storage class: only a contribution
 * for proposal_admission.contains_admitted(key), with a valid frozen-tree
 * child envelope and a not-closed/not-retired lifecycle, may be inserted.
 */

namespace
{

std::string read_source(const std::string &relative_path)
{
    const std::string path =
        std::string(KAURI_PROJECT_SOURCE_DIR) + "/" + relative_path;
    std::ifstream input(path);
    REQUIRE(input.good());
    std::ostringstream contents;
    contents << input.rdbuf();
    return contents.str();
}

std::string source_slice(const std::string &source,
                         const std::string &begin_marker,
                         const std::string &end_marker)
{
    const auto begin = source.find(begin_marker);
    INFO("missing source marker: " << begin_marker);
    REQUIRE(begin != std::string::npos);
    const auto end = source.find(end_marker, begin + begin_marker.size());
    INFO("missing source marker: " << end_marker);
    REQUIRE(end != std::string::npos);
    return source.substr(begin, end - begin);
}

std::string without_whitespace(const std::string &source)
{
    std::string normalized;
    normalized.reserve(source.size());
    for (const unsigned char character : source)
        if (std::isspace(character) == 0)
            normalized.push_back(static_cast<char>(character));
    return normalized;
}

std::size_t occurrence_count(const std::string &source,
                             const std::string &needle)
{
    std::size_t count = 0;
    std::size_t cursor = 0;
    while ((cursor = source.find(needle, cursor)) != std::string::npos)
    {
        ++count;
        cursor += needle.size();
    }
    return count;
}

template<typename Envelope, typename = void>
struct HasExactContributionFingerprint : std::false_type
{};

template<typename Envelope>
struct HasExactContributionFingerprint<
    Envelope,
    std::void_t<decltype(exact_contribution_fingerprint(
        std::declval<hotstuff::ExactContributionKind>(),
        std::declval<const Envelope &>()))>> : std::true_type
{};

template<typename Envelope>
hotstuff::uint256_t semantic_fingerprint(
    hotstuff::ExactContributionKind kind,
    const Envelope &envelope)
{
    if constexpr (HasExactContributionFingerprint<Envelope>::value)
        return exact_contribution_fingerprint(kind, envelope);

    FAIL("Exact contribution deduplication must expose a semantic "
         "fingerprint helper");
    return {};
}

} // namespace

TEST_CASE("exact contribution fingerprint is a production API",
          "[rem-a06-02][pending-contribution][fingerprint]"
          "[semantic][intentional-red]")
{
    INFO("The helper must be callable from the shared HotStuff admission "
         "seam without hashing a wire DataStream");
    CHECK(HasExactContributionFingerprint<
          hotstuff::ExactContributionEnvelope>::value);
}

TEST_CASE("shared admission seam derives one semantic fingerprint",
          "[rem-a06-02][pending-contribution][fingerprint]"
          "[production-wiring][intentional-red]")
{
    const auto source = read_source("src/hotstuff.cpp");
    const auto seam = without_whitespace(source_slice(
        source,
        "void HotStuffBase::buffer_or_dispatch_exact_contribution",
        "void HotStuffBase::dispatch_exact_contribution"));
    const auto direct = without_whitespace(source_slice(
        source,
        "void HotStuffBase::vote_handler",
        "void HotStuffBase::vote_relay_handler"));
    const auto relay = without_whitespace(source_slice(
        source,
        "void HotStuffBase::vote_relay_handler",
        "void HotStuffBase::req_blk_handler"));

    const auto cheap_gate = seam.find(
        "passes_exact_contribution_cheap_gate(");
    const auto fingerprint = seam.find(
        "exact_contribution_fingerprint(");
    const auto contribution = seam.find(
        "PendingExactContributioncontribution{");

    INFO("the semantic identity is derived once, after the shared cheap "
         "gate and before either dispatch or retention");
    REQUIRE(cheap_gate != std::string::npos);
    REQUIRE(fingerprint != std::string::npos);
    REQUIRE(contribution != std::string::npos);
    CHECK(occurrence_count(
              seam, "exact_contribution_fingerprint(") == 1);
    CHECK(cheap_gate < fingerprint);
    CHECK(fingerprint < contribution);

    INFO("neither wire handler may deduplicate serialized certificate bytes");
    CHECK(direct.find("msg.serialized.get_hash()") == std::string::npos);
    CHECK(relay.find("msg.serialized.get_hash()") == std::string::npos);
}

TEST_CASE("both contribution handlers cross one admission-window seam",
          "[rem-a06-02][pending-contribution][production-wiring]"
          "[intentional-red]")
{
    const auto header = read_source("include/hotstuff/hotstuff.h");
    const auto source = read_source("src/hotstuff.cpp");
    const auto direct = without_whitespace(source_slice(
        source,
        "void HotStuffBase::vote_handler",
        "void HotStuffBase::vote_relay_handler"));
    const auto relay = without_whitespace(source_slice(
        source,
        "void HotStuffBase::vote_relay_handler",
        "void HotStuffBase::req_blk_handler"));
    const std::string seam = "buffer_or_dispatch_exact_contribution(";

    INFO("HotStuffBase must own one bounded production buffer");
    CHECK(header.find(
              "hotstuff/pending_exact_contribution_buffer.h") !=
          std::string::npos);
    CHECK(header.find(
              "PendingExactContributionBuffer pending_exact_contributions") !=
          std::string::npos);

    const auto direct_adapter = direct.find("make_exact_direct_envelope(");
    const auto direct_seam = direct.find(seam);
    const auto relay_adapter = relay.find("make_exact_relay_envelope(");
    const auto relay_seam = relay.find(seam);
    INFO("direct and relay handlers must use the same buffer-or-dispatch "
         "decision after constructing an owned authenticated envelope");
    REQUIRE(direct_adapter != std::string::npos);
    REQUIRE(relay_adapter != std::string::npos);
    CHECK(direct_seam != std::string::npos);
    CHECK(relay_seam != std::string::npos);
    if (direct_seam != std::string::npos)
        CHECK(direct_adapter < direct_seam);
    if (relay_seam != std::string::npos)
        CHECK(relay_adapter < relay_seam);

    INFO("handlers may not bypass the shared admission-window seam");
    CHECK(direct.find("coordinator.handle_direct(") == std::string::npos);
    CHECK(relay.find("coordinator.handle_relay(") == std::string::npos);
}

TEST_CASE("active proposal timing starts after protocol acceptance before drain",
          "[rem-a06-02][pending-contribution][production-wiring]"
          "[proposal-order][intentional-red]")
{
    const auto source = read_source("src/hotstuff.cpp");
    const auto active = without_whitespace(source_slice(
        source,
        "void HotStuffBase::process_active",
        "promise_t HotStuffBase::verify_exact_contribution"));

    const auto delivery = active.find("async_deliver_blk(");
    const auto admit = active.find("proposal_contexts->admit_remote(");
    const auto initialize =
        active.find("proposal_contexts->initialize_accumulator(");
    const auto normal = active.find("on_receive_proposal(parsed)");
    const auto acceptance_if = active.rfind("if(", normal);
    const auto acceptance_body = active.find('{', acceptance_if);
    const auto still_open = active.find(
        "acquire_open_context(metadata.key)", normal);
    const auto expected = active.find(
        "create_expected_vote_state(metadata.key);");
    const auto latency = active.find(
        "start_latency_deadline(metadata.key);");
    const auto timer = active.find(
        "start_aggregation_timer(metadata.key);");
    const auto drain = active.find(
        "drain_pending_exact_contributions(metadata.key);");

    INFO("delivery opens the lifecycle and accumulator before normal "
         "HotStuff processing; only an exact context that remains open may "
         "arm timing and drain retained contributions");
    REQUIRE(delivery != std::string::npos);
    REQUIRE(admit != std::string::npos);
    REQUIRE(initialize != std::string::npos);
    REQUIRE(normal != std::string::npos);
    REQUIRE(acceptance_if != std::string::npos);
    REQUIRE(acceptance_body != std::string::npos);
    CHECK(acceptance_if < normal);
    CHECK(normal < acceptance_body);
    CHECK(active.find("on_receive_proposal(parsed);") ==
          std::string::npos);
    REQUIRE(still_open != std::string::npos);
    REQUIRE(expected != std::string::npos);
    REQUIRE(latency != std::string::npos);
    REQUIRE(timer != std::string::npos);
    REQUIRE(drain != std::string::npos);
    CHECK(delivery < admit);
    CHECK(admit < initialize);
    CHECK(initialize < normal);
    CHECK(normal < still_open);
    CHECK(still_open < expected);
    CHECK(expected < latency);
    CHECK(latency < timer);
    CHECK(timer < drain);

    INFO("an initialization abort must purge its exact pending window");
    CHECK(active.find(
              "purge_pending_exact_contributions(metadata.key)") !=
          std::string::npos);
}

TEST_CASE("pending contribution cleanup is exact and terminally reachable",
          "[rem-a06-02][pending-contribution][production-wiring]"
          "[cleanup][intentional-red]")
{
    const auto header = read_source("include/hotstuff/hotstuff.h");
    const auto source = read_source("src/hotstuff.cpp");

    INFO("one HotStuffBase seam must own exact-key drain and purge policy");
    CHECK(header.find("drain_pending_exact_contributions(") !=
          std::string::npos);
    CHECK(header.find("purge_pending_exact_contributions(") !=
          std::string::npos);
    CHECK(source.find("HotStuffBase::drain_pending_exact_contributions(") !=
          std::string::npos);
    CHECK(source.find("HotStuffBase::purge_pending_exact_contributions(") !=
          std::string::npos);

    INFO("purge is used by failure/terminal paths, not only declared");
    CHECK(occurrence_count(
              source, "purge_pending_exact_contributions(") >= 2);
}

#if __has_include("hotstuff/pending_exact_contribution_buffer.h") && \
    __has_include("hotstuff/exact_vote_handler.h") && \
    __has_include("hotstuff/proposal_admission.h")
#define KAURI_HAS_PENDING_EXACT_CONTRIBUTION_BUFFER 1

#include <algorithm>
#include <map>
#include <memory>
#include <optional>
#include <set>
#include <stdexcept>
#include <type_traits>
#include <utility>
#include <vector>

#include "hotstuff/exact_vote_handler.h"
#include "hotstuff/future_proposal_buffer.h"
#include "hotstuff/pending_exact_contribution_buffer.h"
#include "hotstuff/proposal_admission.h"
#include "support/fixtures.h"

#else
#define KAURI_HAS_PENDING_EXACT_CONTRIBUTION_BUFFER 0
#endif

#if !KAURI_HAS_PENDING_EXACT_CONTRIBUTION_BUFFER

TEST_CASE("bounded pending exact contribution buffer is available",
          "[rem-a06-02][pending-contribution][contract]"
          "[intentional-red]")
{
    INFO("Missing hotstuff/pending_exact_contribution_buffer.h. The exact "
         "vote/relay admission window currently loses one-shot traffic.");
    REQUIRE(KAURI_HAS_PENDING_EXACT_CONTRIBUTION_BUFFER == 1);
}

#else

namespace
{

using hotstuff::BufferedProposal;
using hotstuff::ConfigurationId;
using hotstuff::DataStream;
using hotstuff::EpochDefinition;
using hotstuff::EpochDefinitionInput;
using hotstuff::EpochStore;
using hotstuff::EpochTreeDefinition;
using hotstuff::EpochValidationContext;
using hotstuff::ExactContributionEnvelope;
using hotstuff::ExactContributionKind;
using hotstuff::ExactVoteHandlerCoordinator;
using hotstuff::ExactVoteHandlerEffects;
using hotstuff::FutureProposalBuffer;
using hotstuff::NetAddr;
using hotstuff::PeerId;
using hotstuff::PendingExactContribution;
using hotstuff::PendingExactContributionBuffer;
using hotstuff::PendingExactContributionBufferLimits;
using hotstuff::PendingExactContributionInsertResult;
using hotstuff::ProposalAdmissionCoordinator;
using hotstuff::ProposalAdmissionEffects;
using hotstuff::ProposalContextEvent;
using hotstuff::ProposalContextLease;
using hotstuff::ProposalContextLifecycle;
using hotstuff::ProposalContextMetadata;
using hotstuff::ProposalContextStatus;
using hotstuff::ProposalDisposition;
using hotstuff::ProposalKey;
using hotstuff::ProposalMetadata;
using hotstuff::ProposalTreeSnapshot;
using hotstuff::ReplicaConfig;
using hotstuff::ReplicaID;
using hotstuff::Vote;
using hotstuff::VoteRelay;
using hotstuff::bytearray_t;
using hotstuff::promise_t;
using hotstuff::quorum_cert_bt;
using hotstuff::uint256_t;

uint256_t digest(const std::string &label)
{
    return DataStream(label).get_hash();
}

ConfigurationId configuration(std::uint32_t epoch,
                              std::uint32_t tree,
                              const std::string &definition)
{
    return ConfigurationId{epoch, tree, digest(definition)};
}

ProposalKey key(const ConfigurationId &configuration_id,
                const std::string &block)
{
    return ProposalKey{configuration_id, digest(block)};
}

PeerId peer(std::uint16_t port)
{
    return PeerId(NetAddr(
        static_cast<std::uint32_t>(0x7f000001), port));
}

ProposalContextMetadata metadata(const ProposalKey &proposal)
{
    ProposalTreeSnapshot tree;
    tree.local_replica = 0;
    tree.root = 0;
    tree.parent = std::nullopt;
    tree.direct_children = {1, 2};
    tree.assigned_subtree = {0, 1, 2, 3, 4, 5, 6};
    tree.child_subtrees = {{1, {1, 3, 4}}, {2, {2, 5, 6}}};
    tree.fanout = 2;
    tree.pipeline_stretch = 2;
    return ProposalContextMetadata{proposal, std::move(tree), 5};
}

ProposalContextMetadata internal_metadata(const ProposalKey &proposal)
{
    ProposalTreeSnapshot tree;
    tree.local_replica = 1;
    tree.root = 0;
    tree.parent = 0;
    tree.direct_children = {3, 4};
    tree.assigned_subtree = {1, 3, 4};
    tree.child_subtrees = {{3, {3}}, {4, {4}}};
    tree.fanout = 2;
    tree.pipeline_stretch = 2;
    return ProposalContextMetadata{proposal, std::move(tree), 5};
}

ExactContributionEnvelope direct_envelope(
    const ProposalKey &proposal,
    ReplicaID authenticated_sender = 1,
    ReplicaID claimed_voter = 1,
    bool own_message = false)
{
    ExactContributionEnvelope envelope;
    envelope.message_key = proposal;
    envelope.certificate_key = proposal;
    envelope.authenticated_sender = authenticated_sender;
    envelope.claimed_voter = claimed_voter;
    envelope.certified_signers = {claimed_voter};
    if (own_message)
        envelope.direct_vote = std::make_shared<const Vote>();
    return envelope;
}

ExactContributionEnvelope relay_envelope(
    const ProposalKey &proposal,
    ReplicaID authenticated_sender = 1,
    std::set<ReplicaID> signers = {1, 3, 4},
    bool own_message = false)
{
    ExactContributionEnvelope envelope;
    envelope.message_key = proposal;
    envelope.certificate_key = proposal;
    envelope.authenticated_sender = authenticated_sender;
    envelope.claimed_voter = std::nullopt;
    envelope.certified_signers = std::move(signers);
    if (own_message)
        envelope.aggregate_relay = std::make_shared<const VoteRelay>();
    return envelope;
}

PendingExactContribution pending(
    ExactContributionKind kind,
    ExactContributionEnvelope envelope,
    std::uint16_t source_port,
    const std::string &fingerprint)
{
    return PendingExactContribution{
        kind,
        std::move(envelope),
        peer(source_port),
        digest(fingerprint)};
}

promise_t resolved(bool value)
{
    return promise_t([value](promise_t &promise) {
        promise.resolve(value);
    });
}

struct CompletionState
{
    bool settled{false};
    bool value{false};
};

std::shared_ptr<CompletionState> watch(promise_t completion)
{
    auto state = std::make_shared<CompletionState>();
    completion.then([state](bool value) {
        state->settled = true;
        state->value = value;
    });
    completion.fail([state]() {
        state->settled = true;
        state->value = false;
    });
    return state;
}

class GateEffects final : public ExactVoteHandlerEffects
{
public:
    bool deferred{false};
    promise_t worker;
    promise_t delivery;
    std::size_t worker_starts{0};
    std::size_t delivery_starts{0};
    std::size_t continuations{0};
    std::vector<std::string> *sequence{nullptr};

    promise_t start_worker_verification(
        ExactContributionKind,
        const ExactContributionEnvelope &) override
    {
        ++worker_starts;
        if (sequence != nullptr)
            sequence->emplace_back("worker");
        return deferred ? worker : resolved(true);
    }

    promise_t start_block_delivery(const ProposalKey &) override
    {
        ++delivery_starts;
        if (sequence != nullptr)
            sequence->emplace_back("delivery");
        return deferred ? delivery : resolved(true);
    }

    void continue_verified(
        const ProposalContextLease &,
        ExactContributionKind,
        const ExactContributionEnvelope &) override
    {
        ++continuations;
        if (sequence != nullptr)
            sequence->emplace_back("continuation");
    }
};

class AdmissionEffects final : public ProposalAdmissionEffects
{
public:
    std::size_t relays{0};
    std::size_t active_callbacks{0};

    void relay_once(const BufferedProposal &) override { ++relays; }
    void process_active(const BufferedProposal &) override
    {
        ++active_callbacks;
    }
    void local_vote_authorized(const ProposalKey &) override {}
    void create_expected_vote_state(const ProposalKey &) override {}
    void start_latency_deadline(const ProposalKey &) override {}
    void start_aggregation_timer(const ProposalKey &) override {}
    void emit_timeout_report(const ProposalKey &) override {}
};

std::vector<ReplicaID> membership()
{
    return {0, 1, 2, 3, 4, 5, 6};
}

EpochTreeDefinition epoch_tree(std::uint32_t tree_id,
                               std::vector<ReplicaID> members)
{
    return EpochTreeDefinition{tree_id, 2, 2, std::move(members)};
}

EpochDefinitionInput epoch_zero_input()
{
    EpochDefinitionInput input;
    input.schema_version = hotstuff::kEpochDefinitionSchemaVersion;
    input.epoch_number = 0;
    input.membership_digest =
        hotstuff::canonical_membership_digest(membership());
    input.trees = {epoch_tree(7, membership())};
    input.activation_height = 10;
    input.generation_seed = 0xA602;
    input.policy_version = "pending-contribution-v1";
    input.evidence_snapshot_id = "pending-epoch-0";
    input.evidence_cutoff = 10;
    return input;
}

EpochDefinitionInput successor_input(const EpochDefinition &predecessor)
{
    auto input = epoch_zero_input();
    input.epoch_number = 1;
    input.previous_epoch_digest = predecessor.epoch_digest();
    input.trees = {epoch_tree(7, {2, 0, 1, 3, 4, 5, 6})};
    input.activation_height = 20;
    input.generation_seed = 0xA603;
    input.evidence_snapshot_id = "pending-epoch-1";
    input.evidence_cutoff = 20;
    return input;
}

EpochValidationContext validation_context()
{
    EpochValidationContext context;
    context.minimum_activation_grace = 1;
    return context;
}

ConfigurationId configuration(const EpochDefinition &epoch)
{
    return ConfigurationId{
        epoch.epoch_number(), 7, epoch.epoch_digest()};
}

BufferedProposal proposal(const ProposalKey &proposal_key,
                          ReplicaID proposer)
{
    return BufferedProposal{
        ProposalMetadata{
            proposal_key.configuration,
            proposal_key.block_hash,
            proposer},
        bytearray_t{0xA6, 0x02},
        peer(19000)};
}

struct StagedEpochs
{
    EpochStore store{membership()};
    const EpochDefinition *epoch0{nullptr};
    const EpochDefinition *epoch1{nullptr};

    StagedEpochs()
    {
        epoch0 = &store.stage(epoch_zero_input(), validation_context());
        epoch1 = &store.stage(
            successor_input(*epoch0), validation_context());
    }
};

bool passes_frozen_tree_gate(
    ExactContributionKind kind,
    const ExactContributionEnvelope &contribution,
    const ProposalContextMetadata &context)
{
    if (contribution.message_key != context.key ||
        contribution.certificate_key != context.key)
        return false;
    const auto child = context.tree.child_subtrees.find(
        contribution.authenticated_sender);
    if (child == context.tree.child_subtrees.end())
        return false;
    if (kind == ExactContributionKind::direct_vote)
    {
        return contribution.claimed_voter.has_value() &&
               *contribution.claimed_voter ==
                   contribution.authenticated_sender &&
               contribution.certified_signers ==
                   std::set<ReplicaID>{*contribution.claimed_voter};
    }
    if (contribution.claimed_voter.has_value() ||
        contribution.certified_signers.empty())
        return false;
    return std::all_of(
        contribution.certified_signers.begin(),
        contribution.certified_signers.end(),
        [&child](ReplicaID signer) {
            return child->second.count(signer) != 0;
        });
}

template<typename Admission, typename = void>
struct HasContainsAdmitted : std::false_type
{};

template<typename Admission>
struct HasContainsAdmitted<
    Admission,
    std::void_t<decltype(std::declval<const Admission &>()
                             .contains_admitted(
                                 std::declval<const ProposalKey &>()))>>
    : std::true_type
{};

template<typename Admission>
bool admission_contains(const Admission &admission,
                        const ProposalKey &proposal)
{
    if constexpr (HasContainsAdmitted<Admission>::value)
        return admission.contains_admitted(proposal);
    static_cast<void>(admission);
    static_cast<void>(proposal);
    return false;
}

enum class OfferResult
{
    dispatched,
    buffered,
    duplicate,
    rejected
};

enum class ProtocolOutcome
{
    accepted_open,
    rejected_closed,
    throws
};

class AdmissionWindowHarness
{
public:
    AdmissionWindowHarness(
        ProposalAdmissionCoordinator &admission,
        PendingExactContributionBufferLimits limits = {16, 4})
        : admission_(admission), pending_(limits), config_(
              hotstuff::test::make_replica_config(7))
    {
        effects_.sequence = &sequence;
    }

    void register_context(ProposalContextMetadata context)
    {
        metadata_[context.key] = std::move(context);
    }

    OfferResult offer(PendingExactContribution contribution)
    {
        const auto key = contribution.envelope.message_key;
        const auto known = metadata_.find(key);
        if (known == metadata_.end() ||
            !passes_frozen_tree_gate(
                contribution.kind, contribution.envelope, known->second) ||
            contexts_.is_configuration_retired(key.configuration) ||
            contexts_.context_status(key) ==
                ProposalContextStatus::terminal_closed)
            return OfferResult::rejected;

        if (contexts_.acquire_open_context(key).has_value())
        {
            dispatch(std::move(contribution));
            return OfferResult::dispatched;
        }
        if (!admission_contains(admission_, key))
            return OfferResult::rejected;

        const auto result = pending_.insert(std::move(contribution));
        if (result == PendingExactContributionInsertResult::inserted)
            return OfferResult::buffered;
        if (result == PendingExactContributionInsertResult::duplicate)
            return OfferResult::duplicate;
        return OfferResult::rejected;
    }

    std::vector<std::shared_ptr<CompletionState>> open_process_and_drain(
        const ProposalKey &key,
        ProtocolOutcome outcome = ProtocolOutcome::accepted_open)
    {
        const auto found = metadata_.find(key);
        if (found == metadata_.end())
            throw std::logic_error("missing exact context metadata");
        auto lease = contexts_.admit_remote(found->second);
        if (!lease.has_value())
            throw std::logic_error("exact context admission failed");
        sequence.emplace_back("admit");
        if (!contexts_.initialize_accumulator(
                *lease,
                quorum_cert_bt(new hotstuff::QuorumCertDummy(config_, key))))
            throw std::logic_error("exact accumulator initialization failed");
        sequence.emplace_back("initialize");

        try
        {
            sequence.emplace_back("normal-processing");
            if (outcome == ProtocolOutcome::throws)
                throw std::runtime_error("protocol processing failed");
            if (outcome == ProtocolOutcome::rejected_closed)
            {
                static_cast<void>(contexts_.close(
                    key, ProposalContextEvent::proposal_aborted));
                sequence.emplace_back("protocol-closed");
            }

            auto timing_lease = contexts_.acquire_open_context(key);
            if (!timing_lease.has_value())
            {
                static_cast<void>(pending_.purge(key));
                sequence.emplace_back("closed-before-timing");
                return {};
            }

            if (!contexts_.pending_children(*timing_lease).has_value())
                throw std::logic_error("expected child state is unavailable");
            sequence.emplace_back("expected-votes");
            for (const auto child : timing_lease->tree().direct_children)
                static_cast<void>(
                    contexts_.record_latency_start(*timing_lease, child));
            sequence.emplace_back("latency-deadline");
            if (timing_lease->tree().parent.has_value() &&
                !timing_lease->tree().direct_children.empty())
                static_cast<void>(contexts_.arm_timer(*timing_lease));
            sequence.emplace_back("aggregation-timer");

            auto drained = pending_.drain(key);
            sequence.emplace_back("drain");
            std::vector<std::shared_ptr<CompletionState>> completions;
            for (auto &contribution : drained)
                completions.push_back(dispatch(std::move(contribution)));
            return completions;
        }
        catch (...)
        {
            static_cast<void>(pending_.purge(key));
            static_cast<void>(contexts_.close(
                key, ProposalContextEvent::proposal_aborted));
            sequence.emplace_back("abort");
            return {};
        }
    }

    std::size_t abort(const ProposalKey &key)
    {
        const auto purged = pending_.purge(key);
        contexts_.close(key, ProposalContextEvent::proposal_aborted);
        return purged;
    }

    std::size_t fail(const ProposalKey &key)
    {
        return pending_.purge(key);
    }

    PendingExactContributionBuffer &pending_buffer() { return pending_; }
    ProposalContextLifecycle &contexts() { return contexts_; }
    GateEffects &effects() { return effects_; }

    std::vector<std::string> sequence;

private:
    std::shared_ptr<CompletionState> dispatch(
        PendingExactContribution contribution)
    {
        ExactVoteHandlerCoordinator coordinator(contexts_, effects_);
        return watch(
            contribution.kind == ExactContributionKind::direct_vote
                ? coordinator.handle_direct(contribution.envelope)
                : coordinator.handle_relay(contribution.envelope));
    }

    ProposalAdmissionCoordinator &admission_;
    ProposalContextLifecycle contexts_;
    PendingExactContributionBuffer pending_;
    ReplicaConfig config_;
    GateEffects effects_;
    std::map<ProposalKey, ProposalContextMetadata> metadata_;
};

void check_no_handler_effects(const GateEffects &effects)
{
    CHECK(effects.worker_starts == 0);
    CHECK(effects.delivery_starts == 0);
    CHECK(effects.continuations == 0);
}

} // namespace

TEST_CASE("semantic fingerprints cover exact contribution identity only",
          "[rem-a06-02][pending-contribution][fingerprint]"
          "[semantic][contract]")
{
    const auto proposal = key(
        configuration(17, 4, "fingerprint-epoch"),
        "fingerprint-block");

    Vote first_wire;
    first_wire.voter = 1;
    first_wire.epoch_nr = proposal.configuration.epoch_number;
    first_wire.tid = proposal.configuration.tree_id;
    first_wire.epoch_digest = proposal.configuration.epoch_digest;
    first_wire.blk_hash = proposal.block_hash;
    Vote second_wire;
    second_wire.voter = first_wire.voter;
    second_wire.epoch_nr = first_wire.epoch_nr;
    second_wire.tid = first_wire.tid;
    second_wire.epoch_digest = first_wire.epoch_digest;
    second_wire.blk_hash = first_wire.blk_hash;

    const auto first = hotstuff::make_exact_direct_envelope(first_wire, 1);
    const auto second = hotstuff::make_exact_direct_envelope(second_wire, 1);
    REQUIRE(first.direct_vote != nullptr);
    REQUIRE(second.direct_vote != nullptr);
    REQUIRE(first.direct_vote.get() != second.direct_vote.get());

    const auto direct_fingerprint = semantic_fingerprint(
        ExactContributionKind::direct_vote, first);
    INFO("separately owned envelopes for the same wire semantics must not "
         "inherit pointer or serialized-certificate identity");
    CHECK(direct_fingerprint == semantic_fingerprint(
          ExactContributionKind::direct_vote, second));

    std::set<ReplicaID> first_insertion_order;
    for (const auto signer : {ReplicaID{4}, ReplicaID{1}, ReplicaID{3}})
        first_insertion_order.insert(signer);
    std::set<ReplicaID> second_insertion_order;
    for (const auto signer : {ReplicaID{3}, ReplicaID{4}, ReplicaID{1}})
        second_insertion_order.insert(signer);
    const auto first_relay = relay_envelope(
        proposal, 1, std::move(first_insertion_order), true);
    const auto second_relay = relay_envelope(
        proposal, 1, std::move(second_insertion_order), true);
    REQUIRE(first_relay.aggregate_relay != nullptr);
    REQUIRE(second_relay.aggregate_relay != nullptr);
    REQUIRE(first_relay.aggregate_relay.get() !=
            second_relay.aggregate_relay.get());
    const auto relay_fingerprint = semantic_fingerprint(
        ExactContributionKind::aggregate_relay, first_relay);
    INFO("certified signer encoding is canonical, not insertion ordered");
    CHECK(relay_fingerprint == semantic_fingerprint(
          ExactContributionKind::aggregate_relay, second_relay));

    auto changed_sender = first;
    changed_sender.authenticated_sender = 2;
    CHECK(direct_fingerprint != semantic_fingerprint(
          ExactContributionKind::direct_vote, changed_sender));

    auto missing_voter = first;
    missing_voter.claimed_voter.reset();
    CHECK(direct_fingerprint != semantic_fingerprint(
          ExactContributionKind::direct_vote, missing_voter));

    auto changed_voter = first;
    changed_voter.claimed_voter = 2;
    CHECK(direct_fingerprint != semantic_fingerprint(
          ExactContributionKind::direct_vote, changed_voter));

    auto changed_signers = first_relay;
    changed_signers.certified_signers = {1, 3, 5};
    CHECK(relay_fingerprint != semantic_fingerprint(
          ExactContributionKind::aggregate_relay, changed_signers));

    INFO("contribution kind is a domain separator");
    CHECK(direct_fingerprint != semantic_fingerprint(
          ExactContributionKind::aggregate_relay, first));

    const auto fingerprint_with_key = [&first](const ProposalKey &changed) {
        auto envelope = first;
        envelope.message_key = changed;
        envelope.certificate_key = changed;
        return semantic_fingerprint(
            ExactContributionKind::direct_vote, envelope);
    };
    CHECK(direct_fingerprint != fingerprint_with_key(key(
          configuration(18, 4, "fingerprint-epoch"),
          "fingerprint-block")));
    CHECK(direct_fingerprint != fingerprint_with_key(key(
          configuration(17, 5, "fingerprint-epoch"),
          "fingerprint-block")));
    CHECK(direct_fingerprint != fingerprint_with_key(key(
          configuration(17, 4, "different-epoch-digest"),
          "fingerprint-block")));
    CHECK(direct_fingerprint != fingerprint_with_key(key(
          proposal.configuration, "different-block")));
}

TEST_CASE("pending buffer owns direct relay and authenticated source values",
          "[rem-a06-02][pending-contribution][buffer][ownership]"
          "[contract]")
{
    PendingExactContributionBuffer buffer({8, 4});
    const auto config = configuration(5, 7, "owned-config");
    const auto direct_key = key(config, "owned-direct");
    const auto relay_key = key(config, "owned-relay");
    auto direct = pending(
        ExactContributionKind::direct_vote,
        direct_envelope(direct_key, 1, 1, true),
        19001,
        "owned-direct-fingerprint");
    auto relay = pending(
        ExactContributionKind::aggregate_relay,
        relay_envelope(relay_key, 1, {1, 3, 4}, true),
        19002,
        "owned-relay-fingerprint");
    const std::weak_ptr<const Vote> direct_owner =
        direct.envelope.direct_vote;
    const std::weak_ptr<const VoteRelay> relay_owner =
        relay.envelope.aggregate_relay;
    const auto direct_source = direct.authenticated_source;
    const auto relay_source = relay.authenticated_source;

    CHECK(buffer.insert(std::move(direct)) ==
          PendingExactContributionInsertResult::inserted);
    CHECK(buffer.insert(std::move(relay)) ==
          PendingExactContributionInsertResult::inserted);
    CHECK_FALSE(direct_owner.expired());
    CHECK_FALSE(relay_owner.expired());

    const auto drained_direct = buffer.drain(direct_key);
    const auto drained_relay = buffer.drain(relay_key);
    REQUIRE(drained_direct.size() == 1);
    REQUIRE(drained_relay.size() == 1);
    CHECK(drained_direct.front().authenticated_source == direct_source);
    CHECK(drained_relay.front().authenticated_source == relay_source);
    CHECK(drained_direct.front().envelope.direct_vote != nullptr);
    CHECK(drained_direct.front().envelope.aggregate_relay == nullptr);
    CHECK(drained_relay.front().envelope.direct_vote == nullptr);
    CHECK(drained_relay.front().envelope.aggregate_relay != nullptr);
}

TEST_CASE("duplicate fingerprints and bounded drop-newest policy are exact",
          "[rem-a06-02][pending-contribution][buffer][bounds]"
          "[deduplication][contract]")
{
    const auto config = configuration(6, 7, "bounded-config");
    const auto key_a = key(config, "bounded-a");
    const auto key_b = key(config, "bounded-b");

    SECTION("duplicates are suppressed per exact key")
    {
        PendingExactContributionBuffer buffer({8, 4});
        const auto make_a = [&]() {
            return pending(
                ExactContributionKind::direct_vote,
                direct_envelope(key_a),
                19101,
                "same-fingerprint");
        };
        CHECK(buffer.insert(make_a()) ==
              PendingExactContributionInsertResult::inserted);
        CHECK(buffer.insert(make_a()) ==
              PendingExactContributionInsertResult::duplicate);
        CHECK(buffer.insert(pending(
                  ExactContributionKind::direct_vote,
                  direct_envelope(key_b),
                  19101,
                  "same-fingerprint")) ==
              PendingExactContributionInsertResult::inserted);
        CHECK(buffer.size() == 2);
        CHECK(buffer.size(key_a) == 1);
        CHECK(buffer.size(key_b) == 1);
    }

    SECTION("per-key capacity drops the newest item")
    {
        PendingExactContributionBuffer buffer({8, 2});
        CHECK(buffer.insert(pending(
                  ExactContributionKind::direct_vote,
                  direct_envelope(key_a), 19111, "first")) ==
              PendingExactContributionInsertResult::inserted);
        CHECK(buffer.insert(pending(
                  ExactContributionKind::aggregate_relay,
                  relay_envelope(key_a), 19112, "second")) ==
              PendingExactContributionInsertResult::inserted);
        CHECK(buffer.insert(pending(
                  ExactContributionKind::direct_vote,
                  direct_envelope(key_a, 2, 2), 19113, "newest")) ==
              PendingExactContributionInsertResult::per_key_full);
        const auto drained = buffer.drain(key_a);
        REQUIRE(drained.size() == 2);
        CHECK(drained[0].fingerprint == digest("first"));
        CHECK(drained[1].fingerprint == digest("second"));
    }

    SECTION("global capacity drops the newest item")
    {
        PendingExactContributionBuffer buffer({2, 2});
        CHECK(buffer.insert(pending(
                  ExactContributionKind::direct_vote,
                  direct_envelope(key_a), 19121, "global-first")) ==
              PendingExactContributionInsertResult::inserted);
        CHECK(buffer.insert(pending(
                  ExactContributionKind::direct_vote,
                  direct_envelope(key_b), 19122, "global-second")) ==
              PendingExactContributionInsertResult::inserted);
        const auto key_c = key(config, "bounded-c");
        CHECK(buffer.insert(pending(
                  ExactContributionKind::direct_vote,
                  direct_envelope(key_c), 19123, "global-newest")) ==
              PendingExactContributionInsertResult::global_full);
        CHECK(buffer.size() == 2);
        CHECK(buffer.size(key_c) == 0);
    }
}

TEST_CASE("same hash contexts drain and purge without aliasing",
          "[rem-a06-02][pending-contribution][buffer][proposal-key]"
          "[cleanup][contract]")
{
    PendingExactContributionBuffer buffer({32, 8});
    const auto shared_hash = digest("shared-block");
    const auto config_a = configuration(7, 7, "config-a");
    const auto config_b = configuration(7, 7, "config-b");
    const auto config_c = configuration(8, 7, "config-c");
    const ProposalKey key_a{config_a, shared_hash};
    const ProposalKey key_b{config_b, shared_hash};
    const ProposalKey key_c{config_c, shared_hash};
    const auto other = key(config_c, "other-block");

    for (const auto &proposal_key : {key_a, key_b, key_c, other})
    {
        CHECK(buffer.insert(pending(
                  ExactContributionKind::direct_vote,
                  direct_envelope(proposal_key),
                  static_cast<std::uint16_t>(19200 + buffer.size()),
                  proposal_key.block_hash.to_hex() +
                      proposal_key.configuration.epoch_digest.to_hex())) ==
              PendingExactContributionInsertResult::inserted);
    }

    CHECK(buffer.drain(key_a).size() == 1);
    CHECK(buffer.size(key_b) == 1);
    CHECK(buffer.size(key_c) == 1);
    CHECK(buffer.purge_configuration(config_b) == 1);
    CHECK(buffer.size(key_c) == 1);
    CHECK(buffer.purge_block(shared_hash) == 1);
    CHECK(buffer.size(other) == 1);

    CHECK(buffer.insert(pending(
              ExactContributionKind::direct_vote,
              direct_envelope(key_a), 19210, "epoch-seven-a")) ==
          PendingExactContributionInsertResult::inserted);
    CHECK(buffer.insert(pending(
              ExactContributionKind::direct_vote,
              direct_envelope(key_b), 19211, "epoch-seven-b")) ==
          PendingExactContributionInsertResult::inserted);
    CHECK(buffer.purge_epoch(7) == 2);
    CHECK(buffer.size(other) == 1);
    CHECK(buffer.purge(other) == 1);
    CHECK(buffer.size() == 0);

    const auto retired = key(
        configuration(6, 7, "retired-config"),
        "retired-block");
    CHECK(buffer.insert(pending(
              ExactContributionKind::direct_vote,
              direct_envelope(retired), 19212, "retired")) ==
          PendingExactContributionInsertResult::inserted);
    CHECK(buffer.insert(pending(
              ExactContributionKind::direct_vote,
              direct_envelope(key_a), 19213, "first-live")) ==
          PendingExactContributionInsertResult::inserted);
    CHECK(buffer.insert(pending(
              ExactContributionKind::direct_vote,
              direct_envelope(key_c), 19214, "later-live")) ==
          PendingExactContributionInsertResult::inserted);
    CHECK(buffer.purge_before_epoch(7) == 1);
    CHECK(buffer.size(retired) == 0);
    CHECK(buffer.size(key_a) == 1);
    CHECK(buffer.size(key_c) == 1);
    CHECK(buffer.size() == 2);
    buffer.clear();
    CHECK(buffer.size() == 0);
}

TEST_CASE("admitted unopened contribution drains through exact coordinator once",
          "[rem-a06-02][pending-contribution][admission-window]"
          "[coordinator][once][contract]")
{
    StagedEpochs epochs;
    FutureProposalBuffer future;
    AdmissionEffects admission_effects;
    const auto active = configuration(*epochs.epoch0);
    ProposalAdmissionCoordinator admission{
        epochs.store, active, future, admission_effects};

    const auto active_key = key(active, "admission-window-block");
    const auto admitted = admission.receive(proposal(active_key, 0));
    REQUIRE(admitted.disposition == ProposalDisposition::admitted_active);
    REQUIRE(admission_contains(admission, active_key));

    AdmissionWindowHarness harness(admission);
    harness.register_context(internal_metadata(active_key));

    auto kind = ExactContributionKind::direct_vote;
    SECTION("direct vote") {}
    SECTION("aggregate relay")
    {
        kind = ExactContributionKind::aggregate_relay;
    }

    auto contribution = pending(
        kind,
        kind == ExactContributionKind::direct_vote
            ? direct_envelope(active_key, 3, 3)
            : relay_envelope(active_key, 3, {3}),
        19301,
        kind == ExactContributionKind::direct_vote
            ? "window-direct"
            : "window-relay");
    auto duplicate = contribution;

    CHECK(harness.offer(std::move(contribution)) ==
          OfferResult::buffered);
    CHECK(harness.offer(std::move(duplicate)) ==
          OfferResult::duplicate);
    CHECK(harness.pending_buffer().size(active_key) == 1);
    check_no_handler_effects(harness.effects());

    const auto completions = harness.open_process_and_drain(active_key);
    REQUIRE(completions.size() == 1);
    CHECK(completions.front()->settled);
    CHECK(completions.front()->value);
    CHECK(harness.pending_buffer().size(active_key) == 0);
    CHECK(harness.effects().worker_starts == 1);
    CHECK(harness.effects().delivery_starts == 1);
    CHECK(harness.effects().continuations == 1);
    const auto timing = harness.contexts().snapshot(active_key);
    REQUIRE(timing.has_value());
    CHECK(timing->latency_started == std::set<ReplicaID>{3, 4});
    CHECK(timing->timer_generation != 0);
    INFO("a non-voting internal node still arms child timing after normal "
         "protocol processing, before replaying an early contribution");
    CHECK((std::vector<std::string>(
               harness.sequence.begin(),
               harness.sequence.begin() + 7) ==
           std::vector<std::string>{
               "admit",
               "initialize",
               "normal-processing",
               "expected-votes",
               "latency-deadline",
               "aggregation-timer",
               "drain"}));

    CHECK(harness.open_process_and_drain(active_key).empty());
    CHECK(harness.effects().worker_starts == 1);
    CHECK(harness.effects().delivery_starts == 1);
    CHECK(harness.effects().continuations == 1);
}

TEST_CASE("protocol rejection cannot leave proposal timing armed",
          "[rem-a06-02][pending-contribution][proposal-order]"
          "[timer-fairness][intentional-red]")
{
    StagedEpochs epochs;
    FutureProposalBuffer future;
    AdmissionEffects admission_effects;
    const auto active = configuration(*epochs.epoch0);
    ProposalAdmissionCoordinator admission{
        epochs.store, active, future, admission_effects};
    const auto proposal_key = key(active, "protocol-rejection");
    REQUIRE(admission.receive(proposal(proposal_key, 0)).disposition ==
            ProposalDisposition::admitted_active);

    AdmissionWindowHarness harness(admission);
    harness.register_context(internal_metadata(proposal_key));
    REQUIRE(harness.offer(pending(
                ExactContributionKind::direct_vote,
                direct_envelope(proposal_key, 3, 3),
                19311,
                "rejected-early")) == OfferResult::buffered);

    auto outcome = ProtocolOutcome::rejected_closed;
    SECTION("protocol closes the exact context") {}
    SECTION("protocol throws")
    {
        outcome = ProtocolOutcome::throws;
    }

    CHECK(harness.open_process_and_drain(proposal_key, outcome).empty());
    CHECK(harness.contexts().context_status(proposal_key) ==
          ProposalContextStatus::terminal_closed);
    CHECK_FALSE(harness.contexts().snapshot(proposal_key).has_value());
    const auto storage = harness.contexts().storage_stats();
    CHECK(storage.retained_runtime_states == 0);
    CHECK(storage.retained_latency_entries == 0);
    CHECK(harness.pending_buffer().size(proposal_key) == 0);
    CHECK(std::find(
              harness.sequence.begin(),
              harness.sequence.end(),
              "expected-votes") == harness.sequence.end());
    CHECK(std::find(
              harness.sequence.begin(),
              harness.sequence.end(),
              "latency-deadline") == harness.sequence.end());
    CHECK(std::find(
              harness.sequence.begin(),
              harness.sequence.end(),
              "aggregation-timer") == harness.sequence.end());
    CHECK(std::find(
              harness.sequence.begin(),
              harness.sequence.end(),
              "drain") == harness.sequence.end());
    check_no_handler_effects(harness.effects());
}

TEST_CASE("only an admitted valid child can enter the unopened window",
          "[rem-a06-02][pending-contribution][admission-window]"
          "[reject][contract]")
{
    StagedEpochs epochs;
    FutureProposalBuffer future;
    AdmissionEffects admission_effects;
    const auto active = configuration(*epochs.epoch0);
    const auto future_configuration = configuration(*epochs.epoch1);
    ProposalAdmissionCoordinator admission{
        epochs.store, active, future, admission_effects};

    const auto active_key = key(active, "valid-active");
    REQUIRE(admission.receive(proposal(active_key, 0)).disposition ==
            ProposalDisposition::admitted_active);
    AdmissionWindowHarness harness(admission);
    harness.register_context(metadata(active_key));

    SECTION("unknown proposal")
    {
        const auto unknown = key(active, "unknown");
        CHECK(harness.offer(pending(
                  ExactContributionKind::direct_vote,
                  direct_envelope(unknown), 19401, "unknown")) ==
              OfferResult::rejected);
    }

    SECTION("known but unactivated future proposal")
    {
        const auto future_key = key(future_configuration, "future");
        REQUIRE(admission.receive(proposal(future_key, 2)).disposition ==
                ProposalDisposition::buffered_future);
        harness.register_context(metadata(future_key));
        CHECK_FALSE(admission_contains(admission, future_key));
        CHECK(harness.offer(pending(
                  ExactContributionKind::aggregate_relay,
                  relay_envelope(future_key), 19402, "future")) ==
              OfferResult::rejected);
        CHECK(harness.contexts().context_status(future_key) ==
              ProposalContextStatus::unknown);
        CHECK_FALSE(harness.contexts().snapshot(future_key).has_value());
        CHECK(harness.contexts().storage_stats()
                  .retained_latency_entries == 0);
        CHECK(harness.sequence.empty());
    }

    SECTION("wrong digest cannot alias an admitted same-hash proposal")
    {
        auto wrong_configuration = active;
        wrong_configuration.epoch_digest = digest("wrong-digest");
        const ProposalKey wrong{
            wrong_configuration, active_key.block_hash};
        harness.register_context(metadata(wrong));
        CHECK_FALSE(admission_contains(admission, wrong));
        CHECK(harness.offer(pending(
                  ExactContributionKind::direct_vote,
                  direct_envelope(wrong), 19403, "wrong-digest")) ==
              OfferResult::rejected);
    }

    SECTION("wrong authenticated child is rejected before buffering")
    {
        CHECK(harness.offer(pending(
                  ExactContributionKind::direct_vote,
                  direct_envelope(active_key, 6, 6),
                  19404,
                  "wrong-child")) == OfferResult::rejected);
    }

    SECTION("closed context cannot reopen an admission window")
    {
        auto lease = harness.contexts().admit_remote(metadata(active_key));
        REQUIRE(lease.has_value());
        REQUIRE(harness.contexts().close(
            active_key, ProposalContextEvent::proposal_aborted));
        CHECK(harness.offer(pending(
                  ExactContributionKind::direct_vote,
                  direct_envelope(active_key), 19405, "closed")) ==
              OfferResult::rejected);
    }

    SECTION("retired configuration cannot reopen an admission window")
    {
        REQUIRE(harness.contexts().retire_configuration(active) == 0);
        CHECK(harness.contexts().is_configuration_retired(active));
        CHECK(harness.offer(pending(
                  ExactContributionKind::direct_vote,
                  direct_envelope(active_key), 19406, "retired")) ==
              OfferResult::rejected);
    }

    CHECK(harness.pending_buffer().size() == 0);
    check_no_handler_effects(harness.effects());
}

TEST_CASE("proposal abort and delivery failure purge retained traffic",
          "[rem-a06-02][pending-contribution][admission-window]"
          "[cleanup][contract]")
{
    StagedEpochs epochs;
    FutureProposalBuffer future;
    AdmissionEffects admission_effects;
    const auto active = configuration(*epochs.epoch0);
    ProposalAdmissionCoordinator admission{
        epochs.store, active, future, admission_effects};
    const auto abort_key = key(active, "abort-window");
    const auto failure_key = key(active, "failure-window");
    REQUIRE(admission.receive(proposal(abort_key, 0)).disposition ==
            ProposalDisposition::admitted_active);
    REQUIRE(admission.receive(proposal(failure_key, 0)).disposition ==
            ProposalDisposition::admitted_active);

    AdmissionWindowHarness harness(admission);
    harness.register_context(metadata(abort_key));
    harness.register_context(metadata(failure_key));
    REQUIRE(harness.offer(pending(
                ExactContributionKind::direct_vote,
                direct_envelope(abort_key), 19501, "abort")) ==
            OfferResult::buffered);
    REQUIRE(harness.offer(pending(
                ExactContributionKind::aggregate_relay,
                relay_envelope(failure_key), 19502, "failure")) ==
            OfferResult::buffered);

    CHECK(harness.abort(abort_key) == 1);
    CHECK(harness.pending_buffer().size(abort_key) == 0);
    CHECK(harness.pending_buffer().size(failure_key) == 1);
    CHECK(harness.fail(failure_key) == 1);
    CHECK(harness.pending_buffer().size() == 0);
    check_no_handler_effects(harness.effects());
}

TEST_CASE("closing after drain suppresses the retained continuation",
          "[rem-a06-02][pending-contribution][admission-window]"
          "[lease][async-close][contract]")
{
    StagedEpochs epochs;
    FutureProposalBuffer future;
    AdmissionEffects admission_effects;
    const auto active = configuration(*epochs.epoch0);
    ProposalAdmissionCoordinator admission{
        epochs.store, active, future, admission_effects};
    const auto proposal_key = key(active, "drain-close");
    REQUIRE(admission.receive(proposal(proposal_key, 0)).disposition ==
            ProposalDisposition::admitted_active);

    AdmissionWindowHarness harness(admission);
    harness.register_context(metadata(proposal_key));
    harness.effects().deferred = true;
    REQUIRE(harness.offer(pending(
                ExactContributionKind::direct_vote,
                direct_envelope(proposal_key), 19601, "drain-close")) ==
            OfferResult::buffered);

    const auto completions =
        harness.open_process_and_drain(proposal_key);
    REQUIRE(completions.size() == 1);
    CHECK(harness.effects().worker_starts == 1);
    CHECK(harness.effects().delivery_starts == 1);
    CHECK_FALSE(completions.front()->settled);

    harness.effects().worker.resolve(true);
    REQUIRE(harness.contexts().close(
        proposal_key, ProposalContextEvent::proposal_aborted));
    harness.effects().delivery.resolve(true);

    CHECK(completions.front()->settled);
    CHECK_FALSE(completions.front()->value);
    CHECK(harness.effects().continuations == 0);
    CHECK(harness.pending_buffer().size() == 0);
}

#endif
