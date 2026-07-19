#include <cstdint>
#include <map>
#include <string>
#include <type_traits>
#include <utility>
#include <vector>

#include "catch.hpp"
#include "hotstuff/configuration.h"
#include "hotstuff/epoch_store.h"
#include "hotstuff/evidence.h"
#include "hotstuff/evidence_reputation.h"
#include "hotstuff/simple_reputation.h"

namespace
{

using hotstuff::AuthenticatedReporter;
using hotstuff::ConfigurationId;
using hotstuff::EpochDefinitionInput;
using hotstuff::EpochStore;
using hotstuff::EpochTreeDefinition;
using hotstuff::EpochValidationContext;
using hotstuff::EvidenceLedger;
using hotstuff::EvidenceReputationApplyStatus;
using hotstuff::EvidenceReputationAuditUpdate;
using hotstuff::EvidenceReputationLimits;
using hotstuff::EvidenceReputationProjection;
using hotstuff::EvidenceStoreLimits;
using hotstuff::ExpectedMessageType;
using hotstuff::ProposalEvidenceStatus;
using hotstuff::ProposalEvidenceWindow;
using hotstuff::ProposalKey;
using hotstuff::ReplicaID;
using hotstuff::ResponseObservation;
using hotstuff::ResponseOutcome;
using hotstuff::SimpleReputation;
using hotstuff::SimpleReputationDisposition;
using hotstuff::SimpleReputationOutcome;
using hotstuff::uint256_t;

constexpr std::uint32_t kTreeId = 9;

std::vector<ReplicaID> membership()
{
    return {0, 1, 2, 3, 4, 5, 6};
}

uint256_t digest(const std::string &label)
{
    hotstuff::DataStream stream(label);
    return stream.get_hash();
}

EpochDefinitionInput epoch_input()
{
    EpochDefinitionInput input;
    input.schema_version = hotstuff::kEpochDefinitionSchemaVersion;
    input.epoch_number = 0;
    input.membership_digest =
        hotstuff::canonical_membership_digest(membership());
    input.trees = {
        EpochTreeDefinition{kTreeId, 2, 2, membership()},
    };
    input.activation_height = 15;
    input.generation_seed = 0xE09;
    input.policy_version = "evidence-reputation-test-v1";
    input.evidence_snapshot_id = "evidence-reputation-window";
    input.evidence_cutoff = 100;
    return input;
}

EpochValidationContext validation_context()
{
    EpochValidationContext context;
    context.current_height = 10;
    context.minimum_activation_grace = 5;
    return context;
}

class MutableEvidenceWindow final : public ProposalEvidenceWindow
{
public:
    void admit(const ProposalKey &proposal)
    {
        statuses_[proposal] = ProposalEvidenceStatus::admissible;
    }

    ProposalEvidenceStatus classify(
        const ProposalKey &proposal) const noexcept override
    {
        const auto found = statuses_.find(proposal);
        return found == statuses_.end()
                   ? ProposalEvidenceStatus::unknown
                   : found->second;
    }

private:
    std::map<ProposalKey, ProposalEvidenceStatus> statuses_;
};

struct Fixture
{
    EpochStore epochs{membership()};
    MutableEvidenceWindow window;
    ConfigurationId configuration;

    Fixture()
    {
        const auto &epoch = epochs.stage(
            epoch_input(), validation_context());
        configuration = {0, kTreeId, epoch.epoch_digest()};
    }

    ResponseObservation observation(
        const std::string &block_label,
        ResponseOutcome outcome,
        std::uint64_t reporter_sequence,
        std::uint64_t reporter_monotonic_ns,
        bool admit = true)
    {
        ResponseObservation value;
        value.reporter_id = 0;
        value.observed_replica_id = 1;
        value.configuration = configuration;
        value.block_hash = digest(block_label);
        value.expected_message_type =
            ExpectedMessageType::aggregate_relay;
        value.outcome = outcome;
        value.deadline_duration_us = 100;
        value.response_duration_us =
            outcome == ResponseOutcome::timeout
                ? 0
                : (outcome == ResponseOutcome::late ? 150 : 50);
        value.reporter_monotonic_ns = reporter_monotonic_ns;
        value.reporter_sequence = reporter_sequence;
        if (outcome != ResponseOutcome::timeout)
            value.signer_set = {1};
        value.observation_id =
            hotstuff::compute_response_observation_id(
                value.attempt_identity());
        if (admit)
            window.admit(value.proposal_key());
        return value;
    }
};

EvidenceStoreLimits store_limits(
    std::size_t accepted = 64,
    std::size_t rejected = 64)
{
    return {accepted, rejected};
}

void ingest(
    EvidenceLedger &ledger,
    const ResponseObservation &observation,
    ReplicaID authenticated_reporter = 0)
{
    ledger.ingest(
        AuthenticatedReporter{authenticated_reporter}, observation);
}

ResponseObservation direct_child_observation(
    Fixture &fixture,
    const std::string &block_label,
    ResponseOutcome outcome,
    std::uint64_t reporter_sequence,
    std::uint64_t reporter_monotonic_ns,
    ReplicaID reporter_id,
    ReplicaID target_id,
    ExpectedMessageType expected_message_type)
{
    auto value = fixture.observation(
        block_label,
        outcome,
        reporter_sequence,
        reporter_monotonic_ns);
    value.reporter_id = reporter_id;
    value.observed_replica_id = target_id;
    value.expected_message_type = expected_message_type;
    value.signer_set.clear();
    if (outcome != ResponseOutcome::timeout)
        value.signer_set = {target_id};
    value.observation_id = hotstuff::compute_response_observation_id(
        value.attempt_identity());
    return value;
}

} // namespace

TEST_CASE(
    "accepted on-time evidence rewards one target exactly once",
    "[adaptive][evidence][reputation][projection]")
{
    Fixture fixture;
    EvidenceLedger ledger(
        fixture.epochs, fixture.window, store_limits());
    auto observation = fixture.observation(
        "on-time", ResponseOutcome::on_time, 1, 1'000);
    ingest(ledger, observation);
    REQUIRE(ledger.accepted().size() == 1);

    SimpleReputation reputation(membership());
    EvidenceReputationProjection projection(ledger, reputation);
    const auto first = projection.apply_through(ledger.high_watermark());
    const auto repeated =
        projection.apply_through(ledger.high_watermark());

    REQUIRE(first.status == EvidenceReputationApplyStatus::applied);
    REQUIRE(first.applied_updates == 1);
    REQUIRE(first.last_applied_ingestion_sequence == 1);
    REQUIRE(repeated.status == EvidenceReputationApplyStatus::no_updates);
    REQUIRE(repeated.applied_updates == 0);
    REQUIRE(reputation.score(0) == 0);
    REQUIRE(reputation.score(1) == 1);
    REQUIRE(projection.audit_updates().size() == 1);

    const auto &audit = projection.audit_updates().front();
    CHECK(audit.ingestion_sequence == 1);
    CHECK(audit.observation_id == observation.observation_id);
    CHECK(audit.reporter_id == 0);
    CHECK(audit.target_id == 1);
    CHECK(audit.evidence_outcome == ResponseOutcome::on_time);
    CHECK(audit.reputation_outcome ==
          SimpleReputationOutcome::response);
    CHECK(audit.delta == 1);
    CHECK(audit.score == 1);
    CHECK(projection.healthy());
}

TEST_CASE(
    "accepted timeout then late evidence applies a compensating response",
    "[adaptive][evidence][reputation][projection][late]")
{
    Fixture fixture;
    EvidenceLedger ledger(
        fixture.epochs, fixture.window, store_limits());
    auto timeout = fixture.observation(
        "late-attempt", ResponseOutcome::timeout, 1, 1'000);
    auto late = fixture.observation(
        "late-attempt", ResponseOutcome::late, 2, 2'000);
    REQUIRE(timeout.observation_id == late.observation_id);
    ingest(ledger, timeout);
    ingest(ledger, late);
    REQUIRE(ledger.accepted().size() == 2);

    SimpleReputation reputation(membership());
    EvidenceReputationProjection projection(ledger, reputation);
    const auto result = projection.apply_through(2);

    REQUIRE(result.status == EvidenceReputationApplyStatus::applied);
    REQUIRE(result.applied_updates == 2);
    REQUIRE(reputation.score(1) == 0);
    REQUIRE(projection.audit_updates().size() == 2);
    CHECK(projection.audit_updates()[0].evidence_outcome ==
          ResponseOutcome::timeout);
    CHECK(projection.audit_updates()[0].delta == -1);
    CHECK(projection.audit_updates()[0].score == -1);
    CHECK(projection.audit_updates()[1].evidence_outcome ==
          ResponseOutcome::late);
    CHECK(projection.audit_updates()[1].reputation_outcome ==
          SimpleReputationOutcome::response);
    CHECK(projection.audit_updates()[1].delta == 1);
    CHECK(projection.audit_updates()[1].score == 0);
}

TEST_CASE(
    "cutoffs apply accepted records incrementally across rejection gaps",
    "[adaptive][evidence][reputation][projection][cutoff]")
{
    Fixture fixture;
    EvidenceLedger ledger(
        fixture.epochs, fixture.window, store_limits());
    auto first = fixture.observation(
        "first", ResponseOutcome::on_time, 1, 1'000);
    auto forged = fixture.observation(
        "forged", ResponseOutcome::timeout, 2, 2'000);
    auto second = fixture.observation(
        "second", ResponseOutcome::on_time, 2, 3'000);
    ingest(ledger, first);
    ingest(ledger, forged, 2);
    ingest(ledger, second);
    REQUIRE(ledger.accepted().size() == 2);
    REQUIRE(ledger.rejected().size() == 1);
    REQUIRE(ledger.accepted()[0].ingestion_sequence == 1);
    REQUIRE(ledger.accepted()[1].ingestion_sequence == 3);

    SimpleReputation reputation(membership());
    EvidenceReputationProjection projection(ledger, reputation);

    const auto through_first = projection.apply_through(1);
    const auto through_gap = projection.apply_through(2);
    const auto through_second = projection.apply_through(3);

    REQUIRE(through_first.status ==
            EvidenceReputationApplyStatus::applied);
    REQUIRE(through_gap.status ==
            EvidenceReputationApplyStatus::no_updates);
    REQUIRE(through_second.status ==
            EvidenceReputationApplyStatus::applied);
    REQUIRE(through_second.last_applied_ingestion_sequence == 3);
    REQUIRE(reputation.score(1) == 2);
    REQUIRE(projection.audit_updates().size() == 2);
    CHECK(projection.audit_updates()[0].ingestion_sequence == 1);
    CHECK(projection.audit_updates()[1].ingestion_sequence == 3);
}

TEST_CASE(
    "projection follows append-only ledger growth without rewriting audit history",
    "[adaptive][evidence][reputation][projection][append-only]")
{
    Fixture fixture;
    EvidenceLedger ledger(
        fixture.epochs, fixture.window, store_limits());
    SimpleReputation reputation(membership());
    EvidenceReputationProjection projection(ledger, reputation);

    const auto empty = projection.apply_through(0);
    REQUIRE(empty.status == EvidenceReputationApplyStatus::no_updates);
    REQUIRE(empty.applied_updates == 0);
    REQUIRE(projection.last_cutoff() == 0);
    REQUIRE(projection.last_applied_ingestion_sequence() == 0);

    auto response = fixture.observation(
        "live-response", ResponseOutcome::on_time, 1, 1'000);
    ingest(ledger, response);
    const auto first = projection.apply_through(1);
    REQUIRE(first.status == EvidenceReputationApplyStatus::applied);
    REQUIRE(first.applied_updates == 1);
    REQUIRE(projection.audit_updates().size() == 1);
    const auto preserved = projection.audit_updates().front();

    auto timeout = fixture.observation(
        "live-timeout", ResponseOutcome::timeout, 2, 2'000);
    ingest(ledger, timeout);
    const auto second = projection.apply_through(2);
    const auto repeated = projection.apply_through(2);

    REQUIRE(second.status == EvidenceReputationApplyStatus::applied);
    REQUIRE(second.applied_updates == 1);
    REQUIRE(second.requested_cutoff == 2);
    REQUIRE(repeated.status == EvidenceReputationApplyStatus::no_updates);
    REQUIRE(repeated.applied_updates == 0);
    REQUIRE(projection.last_cutoff() == 2);
    REQUIRE(projection.last_applied_ingestion_sequence() == 2);
    REQUIRE(projection.audit_updates().size() == 2);
    REQUIRE(reputation.score(1) == 0);

    const auto &first_audit = projection.audit_updates()[0];
    CHECK(first_audit.ingestion_sequence == preserved.ingestion_sequence);
    CHECK(first_audit.observation_id == preserved.observation_id);
    CHECK(first_audit.reporter_id == preserved.reporter_id);
    CHECK(first_audit.target_id == preserved.target_id);
    CHECK(first_audit.evidence_outcome == preserved.evidence_outcome);
    CHECK(first_audit.reputation_outcome == preserved.reputation_outcome);
    CHECK(first_audit.delta == preserved.delta);
    CHECK(first_audit.score == preserved.score);

    const auto &second_audit = projection.audit_updates()[1];
    CHECK(second_audit.ingestion_sequence == 2);
    CHECK(second_audit.observation_id == timeout.observation_id);
    CHECK(second_audit.evidence_outcome == ResponseOutcome::timeout);
    CHECK(second_audit.reputation_outcome ==
          SimpleReputationOutcome::timeout);
    CHECK(second_audit.delta == -1);
    CHECK(second_audit.score == 0);
}

TEST_CASE(
    "only accepted ledger records can influence the score",
    "[adaptive][evidence][reputation][projection][rejection]")
{
    Fixture fixture;
    EvidenceLedger ledger(
        fixture.epochs, fixture.window, store_limits());

    auto forged_identity = fixture.observation(
        "forged-identity", ResponseOutcome::timeout, 1, 1'000);
    ingest(ledger, forged_identity, 2);

    auto wrong_configuration = fixture.observation(
        "wrong-configuration", ResponseOutcome::timeout, 1, 2'000);
    wrong_configuration.configuration.epoch_digest = digest("wrong-epoch");
    wrong_configuration.observation_id =
        hotstuff::compute_response_observation_id(
            wrong_configuration.attempt_identity());
    ingest(ledger, wrong_configuration);

    auto accepted = fixture.observation(
        "accepted", ResponseOutcome::timeout, 1, 3'000);
    ingest(ledger, accepted);
    ingest(ledger, accepted);

    REQUIRE(ledger.accepted().size() == 1);
    REQUIRE(ledger.rejected().size() == 3);
    REQUIRE(ledger.high_watermark() == 4);

    SimpleReputation reputation(membership());
    EvidenceReputationProjection projection(ledger, reputation);
    const auto result = projection.apply_through(4);

    REQUIRE(result.status == EvidenceReputationApplyStatus::applied);
    REQUIRE(result.applied_updates == 1);
    REQUIRE(reputation.score(1) == -1);
    REQUIRE(projection.audit_updates().size() == 1);
    CHECK(projection.audit_updates().front().ingestion_sequence == 3);
    CHECK(projection.audit_updates().front().observation_id ==
          accepted.observation_id);
}

TEST_CASE(
    "unhealthy ledger and bounded audit capacity fail before scoring",
    "[adaptive][evidence][reputation][projection][fail-closed]")
{
    SECTION("ledger health is a hard prerequisite")
    {
        Fixture fixture;
        EvidenceLedger ledger(
            fixture.epochs, fixture.window, store_limits(1, 8));
        ingest(
            ledger,
            fixture.observation(
                "accepted-before-capacity",
                ResponseOutcome::on_time,
                1,
                1'000));
        ingest(
            ledger,
            fixture.observation(
                "capacity-failure",
                ResponseOutcome::on_time,
                2,
                2'000));
        REQUIRE_FALSE(ledger.healthy());

        SimpleReputation reputation(membership());
        EvidenceReputationProjection projection(ledger, reputation);
        const auto result =
            projection.apply_through(ledger.high_watermark());

        REQUIRE(result.status ==
                EvidenceReputationApplyStatus::ledger_unhealthy);
        REQUIRE(result.applied_updates == 0);
        REQUIRE(reputation.score(1) == 0);
        REQUIRE(projection.audit_updates().empty());
        REQUIRE_FALSE(projection.healthy());
    }

    SECTION("audit capacity is checked for the complete pending prefix")
    {
        Fixture fixture;
        EvidenceLedger ledger(
            fixture.epochs, fixture.window, store_limits());
        ingest(
            ledger,
            fixture.observation(
                "capacity-a", ResponseOutcome::on_time, 1, 1'000));
        ingest(
            ledger,
            fixture.observation(
                "capacity-b", ResponseOutcome::on_time, 2, 2'000));

        SimpleReputation reputation(membership());
        EvidenceReputationProjection projection(
            ledger, reputation, EvidenceReputationLimits{1});
        const auto result = projection.apply_through(2);

        REQUIRE(result.status ==
                EvidenceReputationApplyStatus::audit_capacity_exceeded);
        REQUIRE(result.applied_updates == 0);
        REQUIRE(result.last_applied_ingestion_sequence == 0);
        REQUIRE(reputation.score(1) == 0);
        REQUIRE(projection.audit_updates().empty());
        REQUIRE_FALSE(projection.healthy());
    }

    SECTION("zero audit capacity is unhealthy before the first update")
    {
        Fixture fixture;
        EvidenceLedger ledger(
            fixture.epochs, fixture.window, store_limits());
        ingest(
            ledger,
            fixture.observation(
                "zero-capacity", ResponseOutcome::on_time, 1, 1'000));

        SimpleReputation reputation(membership());
        EvidenceReputationProjection projection(
            ledger, reputation, EvidenceReputationLimits{0});
        REQUIRE_FALSE(projection.healthy());

        const auto result = projection.apply_through(1);
        REQUIRE(result.status ==
                EvidenceReputationApplyStatus::projection_unhealthy);
        REQUIRE(result.applied_updates == 0);
        REQUIRE(result.last_applied_ingestion_sequence == 0);
        REQUIRE(reputation.score(1) == 0);
        REQUIRE(projection.audit_updates().empty());
    }
}

TEST_CASE(
    "membership mismatch and invalid cutoff order fail without cursor advance",
    "[adaptive][evidence][reputation][projection][fail-closed]")
{
    SECTION("the score membership must cover accepted reporter and target")
    {
        Fixture fixture;
        EvidenceLedger ledger(
            fixture.epochs, fixture.window, store_limits());
        ingest(
            ledger,
            fixture.observation(
                "membership", ResponseOutcome::on_time, 1, 1'000));

        SimpleReputation incomplete_membership({0, 2, 3, 4, 5, 6});
        EvidenceReputationProjection projection(
            ledger, incomplete_membership);
        const auto result = projection.apply_through(1);

        REQUIRE(result.status ==
                EvidenceReputationApplyStatus::reputation_rejected);
        REQUIRE(result.reputation_disposition ==
                SimpleReputationDisposition::unknown_target);
        REQUIRE(result.applied_updates == 0);
        REQUIRE(result.last_applied_ingestion_sequence == 0);
        REQUIRE(projection.audit_updates().empty());
        REQUIRE_FALSE(projection.healthy());
    }

    SECTION("the score membership must cover the authenticated reporter")
    {
        Fixture fixture;
        EvidenceLedger ledger(
            fixture.epochs, fixture.window, store_limits());
        const auto observation = direct_child_observation(
            fixture,
            "missing-reporter",
            ResponseOutcome::on_time,
            1,
            1'000,
            1,
            3,
            ExpectedMessageType::direct_vote);
        ingest(ledger, observation, 1);
        REQUIRE(ledger.accepted().size() == 1);

        SimpleReputation incomplete_membership({0, 2, 3, 4, 5, 6});
        EvidenceReputationProjection projection(
            ledger, incomplete_membership);
        const auto result = projection.apply_through(1);

        REQUIRE(result.status ==
                EvidenceReputationApplyStatus::reputation_rejected);
        REQUIRE(result.reputation_disposition ==
                SimpleReputationDisposition::unknown_reporter);
        REQUIRE(result.applied_updates == 0);
        REQUIRE(result.last_applied_ingestion_sequence == 0);
        REQUIRE(incomplete_membership.score(3) == 0);
        REQUIRE(projection.audit_updates().empty());
        REQUIRE_FALSE(projection.healthy());
    }

    SECTION("a later membership rejection leaves the whole prefix unapplied")
    {
        Fixture fixture;
        EvidenceLedger ledger(
            fixture.epochs, fixture.window, store_limits());
        const auto first = fixture.observation(
            "valid-prefix", ResponseOutcome::on_time, 1, 1'000);
        const auto missing_target = direct_child_observation(
            fixture,
            "invalid-prefix",
            ResponseOutcome::timeout,
            2,
            2'000,
            0,
            2,
            ExpectedMessageType::aggregate_relay);
        ingest(ledger, first);
        ingest(ledger, missing_target);
        REQUIRE(ledger.accepted().size() == 2);

        SimpleReputation incomplete_membership({0, 1, 3, 4, 5, 6});
        EvidenceReputationProjection projection(
            ledger, incomplete_membership);
        const auto result = projection.apply_through(2);

        REQUIRE(result.status ==
                EvidenceReputationApplyStatus::reputation_rejected);
        REQUIRE(result.reputation_disposition ==
                SimpleReputationDisposition::unknown_target);
        REQUIRE(result.applied_updates == 0);
        REQUIRE(result.last_applied_ingestion_sequence == 0);
        REQUIRE(incomplete_membership.score(1) == 0);
        REQUIRE(projection.audit_updates().empty());
        REQUIRE_FALSE(projection.healthy());

        const auto after_failure = projection.apply_through(2);
        REQUIRE(after_failure.status ==
                EvidenceReputationApplyStatus::projection_unhealthy);
        REQUIRE(after_failure.applied_updates == 0);
        REQUIRE(after_failure.last_applied_ingestion_sequence == 0);
        REQUIRE(incomplete_membership.score(1) == 0);
    }

    SECTION("cutoffs cannot move backwards")
    {
        Fixture fixture;
        EvidenceLedger ledger(
            fixture.epochs, fixture.window, store_limits());
        ingest(
            ledger,
            fixture.observation(
                "ordered-cutoff", ResponseOutcome::on_time, 1, 1'000));

        SimpleReputation reputation(membership());
        EvidenceReputationProjection projection(ledger, reputation);
        REQUIRE(projection.apply_through(1).status ==
                EvidenceReputationApplyStatus::applied);
        const auto regressed = projection.apply_through(0);

        REQUIRE(regressed.status ==
                EvidenceReputationApplyStatus::invalid_cutoff);
        REQUIRE(regressed.applied_updates == 0);
        REQUIRE(regressed.last_applied_ingestion_sequence == 1);
        REQUIRE(reputation.score(1) == 1);
        REQUIRE(projection.audit_updates().size() == 1);
        REQUIRE_FALSE(projection.healthy());
    }

    SECTION("a rejection-only cutoff still advances monotonic order")
    {
        Fixture fixture;
        EvidenceLedger ledger(
            fixture.epochs, fixture.window, store_limits());
        auto accepted = fixture.observation(
            "accepted-before-gap", ResponseOutcome::on_time, 1, 1'000);
        auto rejected = fixture.observation(
            "rejected-gap", ResponseOutcome::timeout, 2, 2'000);
        ingest(ledger, accepted);
        ingest(ledger, rejected, 2);

        SimpleReputation reputation(membership());
        EvidenceReputationProjection projection(ledger, reputation);
        REQUIRE(projection.apply_through(1).status ==
                EvidenceReputationApplyStatus::applied);
        const auto gap = projection.apply_through(2);
        REQUIRE(gap.status == EvidenceReputationApplyStatus::no_updates);
        REQUIRE(projection.last_cutoff() == 2);
        REQUIRE(projection.last_applied_ingestion_sequence() == 1);

        const auto regressed = projection.apply_through(1);
        REQUIRE(regressed.status ==
                EvidenceReputationApplyStatus::invalid_cutoff);
        REQUIRE(regressed.applied_updates == 0);
        REQUIRE(regressed.last_applied_ingestion_sequence == 1);
        REQUIRE(reputation.score(1) == 1);
        REQUIRE(projection.audit_updates().size() == 1);
        REQUIRE_FALSE(projection.healthy());
    }

    SECTION("a cutoff beyond the ledger watermark is rejected")
    {
        Fixture fixture;
        EvidenceLedger ledger(
            fixture.epochs, fixture.window, store_limits());
        ingest(
            ledger,
            fixture.observation(
                "future-cutoff", ResponseOutcome::on_time, 1, 1'000));

        SimpleReputation reputation(membership());
        EvidenceReputationProjection projection(ledger, reputation);
        const auto future = projection.apply_through(2);

        REQUIRE(future.status ==
                EvidenceReputationApplyStatus::invalid_cutoff);
        REQUIRE(future.requested_cutoff == 2);
        REQUIRE(future.applied_updates == 0);
        REQUIRE(future.last_applied_ingestion_sequence == 0);
        REQUIRE(reputation.score(1) == 0);
        REQUIRE(projection.audit_updates().empty());
        REQUIRE_FALSE(projection.healthy());
    }
}

TEST_CASE(
    "corrupted accepted ordering fails before any score mutation",
    "[adaptive][evidence][reputation][projection][order][adversarial]")
{
    Fixture fixture;
    EvidenceLedger ledger(
        fixture.epochs, fixture.window, store_limits());
    ingest(
        ledger,
        fixture.observation(
            "ordered-a", ResponseOutcome::on_time, 1, 1'000));
    ingest(
        ledger,
        fixture.observation(
            "ordered-b", ResponseOutcome::timeout, 2, 2'000));
    REQUIRE(ledger.accepted().size() == 2);

    // Deliberately simulate corrupted borrowed state to exercise the
    // projection's defensive order check without adding a production test
    // hook to EvidenceLedger.
    auto &corrupted = const_cast<std::vector<hotstuff::AcceptedEvidenceRecord> &>(
        ledger.accepted());
    std::swap(corrupted[0], corrupted[1]);

    SimpleReputation reputation(membership());
    EvidenceReputationProjection projection(ledger, reputation);
    const auto result = projection.apply_through(2);

    REQUIRE(result.status ==
            EvidenceReputationApplyStatus::accepted_order_invalid);
    REQUIRE(result.applied_updates == 0);
    REQUIRE(result.last_applied_ingestion_sequence == 0);
    REQUIRE(reputation.score(1) == 0);
    REQUIRE(projection.audit_updates().empty());
    REQUIRE_FALSE(projection.healthy());
}

TEST_CASE(
    "projection is append-only observational state under fixed N equals 3f plus 1",
    "[adaptive][evidence][reputation][projection][bft]")
{
    static_assert(
        !std::is_copy_constructible<EvidenceReputationProjection>::value,
        "the single-writer cursor cannot be copied");
    static_assert(
        !std::is_move_constructible<EvidenceReputationProjection>::value,
        "the single-writer cursor cannot be moved");
    static_assert(
        std::is_same<
            decltype(std::declval<const EvidenceReputationProjection &>()
                         .audit_updates()),
            const std::vector<EvidenceReputationAuditUpdate> &>::value,
        "audit updates must expose only an immutable view");
    static_assert(
        noexcept(std::declval<EvidenceReputationProjection &>()
                     .apply_through(0)),
        "the projection boundary must fail closed without exceptions");

    Fixture fixture;
    const auto *epoch_before = fixture.epochs.find_epoch(0);
    REQUIRE(epoch_before != nullptr);
    const auto epoch_digest_before = epoch_before->epoch_digest();
    const auto activation_height_before = epoch_before->activation_height();
    const auto tree_members_before =
        epoch_before->trees().front().members_breadth_first;

    EvidenceLedger ledger(
        fixture.epochs, fixture.window, store_limits());
    const auto crashed_one = fixture.observation(
        "crashed-one", ResponseOutcome::timeout, 1, 1'000);
    const auto crashed_two = direct_child_observation(
        fixture,
        "crashed-two",
        ResponseOutcome::timeout,
        2,
        2'000,
        0,
        2,
        ExpectedMessageType::aggregate_relay);
    ingest(ledger, crashed_one);
    ingest(ledger, crashed_two);
    REQUIRE(ledger.accepted().size() == 2);

    SimpleReputation reputation(membership());
    EvidenceReputationProjection projection(ledger, reputation);
    const auto result = projection.apply_through(2);
    REQUIRE(result.status == EvidenceReputationApplyStatus::applied);
    REQUIRE(result.applied_updates == 2);
    REQUIRE(reputation.score(1) == -1);
    REQUIRE(reputation.score(2) == -1);

    const auto *epoch_after = fixture.epochs.find_epoch(0);
    REQUIRE(epoch_after != nullptr);
    CHECK(epoch_after == epoch_before);
    CHECK(fixture.epochs.size() == 1);
    CHECK(epoch_after->epoch_digest() == epoch_digest_before);
    CHECK(epoch_after->activation_height() == activation_height_before);
    CHECK(epoch_after->trees().front().members_breadth_first ==
          tree_members_before);
    CHECK(epoch_after->trees().front().wait_exempt_leaves.empty());

    const auto quorum = hotstuff::derive_byzantine_quorum(
        membership().size());
    REQUIRE(quorum.has_value());
    CHECK(quorum->replica_count == 7);
    CHECK(quorum->fault_threshold == 2);
    CHECK(quorum->quorum == 5);
}
