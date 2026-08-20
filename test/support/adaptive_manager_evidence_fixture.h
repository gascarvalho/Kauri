#ifndef KAURI_TEST_SUPPORT_ADAPTIVE_MANAGER_EVIDENCE_FIXTURE_H_INCLUDED
#define KAURI_TEST_SUPPORT_ADAPTIVE_MANAGER_EVIDENCE_FIXTURE_H_INCLUDED

#include <algorithm>
#include <cstddef>
#include <cstdint>
#include <map>
#include <set>
#include <string>
#include <type_traits>
#include <utility>
#include <vector>

#include "catch.hpp"
#include "hotstuff/adaptive_v2_manager_ingress.h"

namespace kauri::test_support
{

using namespace hotstuff;

inline uint256_t evidence_digest(const std::string &label)
{
    return DataStream(label).get_hash();
}

inline std::size_t first_leaf_index(
    std::size_t member_count,
    std::uint32_t fanout)
{
    REQUIRE(fanout != 0);
    return member_count == 1 ? 0 : ((member_count - 2) / fanout) + 1;
}

template<typename Session, typename = void>
struct has_session_ingestion : std::false_type
{};

template<typename Session>
struct has_session_ingestion<
    Session,
    std::void_t<
        decltype(std::declval<Session &>().ingest_readiness(
            std::declval<const AuthenticatedReporter &>(),
            std::declval<const bytearray_t &>())),
        decltype(std::declval<Session &>().ingest_lifecycle(
            std::declval<const AuthenticatedReporter &>(),
            std::declval<const bytearray_t &>())),
        decltype(std::declval<Session &>().ingest_evidence(
            std::declval<const AuthenticatedReporter &>(),
            std::declval<const bytearray_t &>()))>> : std::true_type
{};

template<typename Session>
auto route_readiness(
    Session &session,
    const AuthenticatedReporter &source,
    const bytearray_t &payload)
{
    if constexpr (has_session_ingestion<Session>::value)
        return session.ingest_readiness(source, payload);
    else
        return session.ingress().ingest_readiness(source, payload);
}

template<typename Session>
auto route_lifecycle(
    Session &session,
    const AuthenticatedReporter &source,
    const bytearray_t &payload)
{
    if constexpr (has_session_ingestion<Session>::value)
        return session.ingest_lifecycle(source, payload);
    else
        return session.ingress().ingest_lifecycle(source, payload);
}

template<typename Session>
auto route_evidence(
    Session &session,
    const AuthenticatedReporter &source,
    const bytearray_t &payload)
{
    if constexpr (has_session_ingestion<Session>::value)
        return session.ingest_evidence(source, payload);
    else
        return session.ingress().ingest_evidence(source, payload);
}

template<typename Session>
class AdaptiveManagerEvidenceDriver
{
public:
    AdaptiveManagerEvidenceDriver(
        Session &session,
        std::vector<ReplicaID> members,
        std::vector<ReplicaID> survivors,
        AdaptiveV2ManagerIngressLimits limits)
        : session_(session),
          members_(std::move(members)),
          survivors_(std::move(survivors)),
          limits_(std::move(limits)),
          readiness_sequences_(members_.size()),
          lifecycle_sequences_(members_.size()),
          evidence_sequences_(members_.size())
    {}

    void ready_all()
    {
        ready(members_);
        REQUIRE(session_.ingress().all_members_ready());
    }

    void ready(const std::vector<ReplicaID> &sources)
    {
        for (const auto source : sources)
        {
            const AdaptiveV2ReadinessNotice notice{
                kAdaptiveV2ReadinessNoticeSchemaVersionV1,
                source,
                ++readiness_sequences_.at(source),
                session_.ingress().current_configuration(),
                session_.ingress().activation_generation(),
                static_cast<std::uint64_t>(
                    1'000'000 + readiness_sequences_.at(source) * 100 + source)};
            CHECK(route_readiness(
                      session_,
                      AuthenticatedReporter{source},
                      encode_adaptive_v2_readiness_notice(
                          notice, limits_.readiness_wire))
                      .status == AdaptiveV2ManagerIngressStatus::processed);
        }
        REQUIRE(session_.ingress().operationally_ready());
    }

    ResponseObservation make_observation(
        ReplicaID target,
        std::size_t reporter_occurrence,
        ResponseOutcome outcome,
        const std::string &label)
    {
        const auto edge = leaf_edge(target, reporter_occurrence);
        ResponseObservation value;
        value.reporter_id = edge.reporter;
        value.observed_replica_id = target;
        value.configuration = ConfigurationId{
            session_.ingress().current_epoch().epoch_number(),
            edge.tree_id,
            session_.ingress().current_epoch().epoch_digest()};
        value.block_hash = evidence_digest(
            label + "-" + std::to_string(++proposal_counter_));
        value.expected_message_type = ExpectedMessageType::direct_vote;
        value.outcome = outcome;
        value.response_duration_us =
            outcome == ResponseOutcome::timeout ? 0 : 20 + target;
        value.deadline_duration_us = 100;
        value.reporter_sequence = ++evidence_sequences_.at(value.reporter_id);
        value.reporter_monotonic_ns = value.reporter_sequence * 1'000;
        if (outcome != ResponseOutcome::timeout)
            value.signer_set = {target};
        value.observation_id = compute_response_observation_id(
            value.attempt_identity());
        return value;
    }

    void record(ResponseObservation observation)
    {
        const auto corroborators = static_cast<ReplicaID>(
            session_.ingress().quorum_metadata().fault_threshold + 1);
        for (ReplicaID source = 0; source < corroborators; ++source)
        {
            const ProposalLifecycleNotice notice{
                kProposalLifecycleNoticeSchemaVersion,
                source,
                ++lifecycle_sequences_.at(source),
                ProposalLifecycleFact{NormalProposalRuntimeInitialized{
                    observation.proposal_key()}}};
            const auto result = route_lifecycle(
                session_,
                AuthenticatedReporter{source},
                encode_proposal_lifecycle_notice(
                    notice, limits_.lifecycle_wire));
            CHECK(result.status ==
                  (source + 1 < corroborators
                       ? AdaptiveV2ManagerIngressStatus::awaiting_corroboration
                       : AdaptiveV2ManagerIngressStatus::processed));
        }
        const auto result = route_evidence(
            session_,
            AuthenticatedReporter{observation.reporter_id},
            encode_evidence_batch(
                ResponseObservationBatch{
                    kEvidenceBatchSchemaVersion, {std::move(observation)}},
                limits_.evidence_wire));
        REQUIRE(result.status == AdaptiveV2ManagerIngressStatus::processed);
        REQUIRE(result.accepted_observations == 1);
    }

    /** Emit one authenticated observation for an exact existing tree edge.
     * This is the same proof shape as the N31 controller campaign fixture:
     * lifecycle admission is f+1 authenticated reporters, then evidence is
     * bound to the parent/child edge and its direct-vote or aggregate-relay
     * message type. */
    void record_for_tree(
        ReplicaID target,
        std::uint32_t tree_id,
        ResponseOutcome outcome,
        const std::string &phase,
        std::uint64_t attempt_start_ns)
    {
        const auto &trees = session_.ingress().current_epoch().trees();
        const auto tree = std::find_if(
            trees.begin(), trees.end(), [tree_id](const auto &entry) {
                return entry.tree_id == tree_id;
            });
        REQUIRE(tree != trees.end());
        const auto found = std::find(
            tree->members_breadth_first.begin(),
            tree->members_breadth_first.end(), target);
        REQUIRE(found != tree->members_breadth_first.end());
        const auto position = static_cast<std::size_t>(std::distance(
            tree->members_breadth_first.begin(), found));
        REQUIRE(position != 0);
        const auto reporter = tree->members_breadth_first[
            (position - 1U) / tree->fanout];
        const auto internal =
            ((position * tree->fanout) + 1U) <
            tree->members_breadth_first.size();
        const auto proposal_key = proposal_for_tree(phase, tree_id);

        ResponseObservation observation;
        observation.schema_version = kResponseObservationSchemaVersionV3;
        observation.reporter_id = reporter;
        observation.observed_replica_id = target;
        observation.configuration = ConfigurationId{
            session_.ingress().current_epoch().epoch_number(), tree_id,
            session_.ingress().current_epoch().epoch_digest()};
        observation.block_hash = proposal_key;
        observation.expected_message_type = internal
            ? ExpectedMessageType::aggregate_relay
            : ExpectedMessageType::direct_vote;
        observation.outcome = outcome;
        observation.response_duration_us =
            outcome == ResponseOutcome::on_time ? 50 : 0;
        observation.deadline_duration_us = 100;
        observation.attempt_start_monotonic_ns = attempt_start_ns;
        observation.reporter_monotonic_ns = attempt_start_ns +
            (outcome == ResponseOutcome::on_time ? 50'000U : 100'000U);
        observation.reporter_sequence = ++evidence_sequences_.at(reporter);
        if (outcome == ResponseOutcome::on_time)
            observation.signer_set = {target};
        observation.observation_id = compute_response_observation_id(observation);
        const auto result = route_evidence(
            session_, AuthenticatedReporter{reporter},
            encode_evidence_batch(ResponseObservationBatch{
                kEvidenceBatchSchemaVersion, {observation}},
                limits_.evidence_wire));
        REQUIRE(result.status == AdaptiveV2ManagerIngressStatus::processed);
        REQUIRE(result.accepted_observations == 1);
    }

    void cover_tree(std::uint32_t tree_id)
    {
        const auto &trees = session_.ingress().current_epoch().trees();
        const auto tree = std::find_if(
            trees.begin(), trees.end(), [tree_id](const auto &entry) {
                return entry.tree_id == tree_id;
            });
        REQUIRE(tree != trees.end());
        const auto position = first_leaf_index(
            tree->members_breadth_first.size(), tree->fanout);
        const auto parent = (position - 1U) / tree->fanout;
        ResponseObservation anchor;
        anchor.reporter_id = tree->members_breadth_first[parent];
        anchor.observed_replica_id = tree->members_breadth_first[position];
        anchor.configuration = ConfigurationId{
            session_.ingress().current_epoch().epoch_number(),
            tree_id,
            session_.ingress().current_epoch().epoch_digest()};
        anchor.block_hash = evidence_digest(
            "coverage-" + std::to_string(++proposal_counter_));
        anchor.expected_message_type = ExpectedMessageType::direct_vote;
        anchor.outcome = ResponseOutcome::on_time;
        anchor.response_duration_us = 20;
        anchor.deadline_duration_us = 100;
        anchor.reporter_sequence = ++evidence_sequences_.at(anchor.reporter_id);
        anchor.reporter_monotonic_ns = anchor.reporter_sequence * 1'000;
        anchor.signer_set = {anchor.observed_replica_id};
        anchor.observation_id = compute_response_observation_id(
            anchor.attempt_identity());
        record(std::move(anchor));
    }

    void anchor_timeout_proposal(const ResponseObservation &timeout)
    {
        const auto &trees = session_.ingress().current_epoch().trees();
        const auto tree = std::find_if(
            trees.begin(), trees.end(), [&timeout](const auto &entry) {
                return entry.tree_id == timeout.configuration.tree_id;
            });
        REQUIRE(tree != trees.end());
        const auto first_leaf = first_leaf_index(
            tree->members_breadth_first.size(), tree->fanout);
        for (std::size_t position = first_leaf;
             position < tree->members_breadth_first.size(); ++position)
        {
            const auto parent = (position - 1U) / tree->fanout;
            const auto reporter = tree->members_breadth_first[parent];
            const auto observed = tree->members_breadth_first[position];
            if (reporter == timeout.reporter_id &&
                observed == timeout.observed_replica_id)
                continue;
            ResponseObservation anchor;
            anchor.reporter_id = reporter;
            anchor.observed_replica_id = observed;
            anchor.configuration = timeout.configuration;
            anchor.block_hash = timeout.block_hash;
            anchor.expected_message_type = ExpectedMessageType::direct_vote;
            anchor.outcome = ResponseOutcome::on_time;
            anchor.response_duration_us = 20;
            anchor.deadline_duration_us = timeout.deadline_duration_us;
            anchor.reporter_sequence = ++evidence_sequences_.at(reporter);
            anchor.reporter_monotonic_ns = anchor.reporter_sequence * 1'000;
            anchor.signer_set = {observed};
            anchor.observation_id = compute_response_observation_id(
                anchor.attempt_identity());
            const auto result = route_evidence(
                session_,
                AuthenticatedReporter{anchor.reporter_id},
                encode_evidence_batch(
                    ResponseObservationBatch{
                        kEvidenceBatchSchemaVersion, {anchor}},
                    limits_.evidence_wire));
            REQUIRE(result.status == AdaptiveV2ManagerIngressStatus::processed);
            REQUIRE(result.accepted_observations == 1);
            return;
        }
        FAIL("fixture tree lacks a distinct direct-vote anchor");
    }

    void responsive_baseline(const std::vector<ReplicaID> &targets)
    {
        for (const auto target : targets)
            for (std::size_t attempt = 0; attempt < 2; ++attempt)
                record(make_observation(
                    target, attempt, ResponseOutcome::on_time, "baseline"));
    }

    void responsive_baseline()
    {
        responsive_baseline(members_);
    }

    void responsive_survivor_baseline()
    {
        responsive_baseline(survivors_);
    }

    void persistent_timeouts(const std::vector<ReplicaID> &targets)
    {
        const auto reporters = static_cast<std::size_t>(
            session_.ingress().quorum_metadata().fault_threshold + 1);
        for (const auto target : targets)
            for (std::size_t reporter = 0; reporter < reporters; ++reporter)
                for (std::size_t attempt = 0; attempt < 2; ++attempt)
                    record(make_observation(
                        target, reporter, ResponseOutcome::timeout, "timeout"));
    }

    void responsive_optimization_suffix()
    {
        const std::vector<std::size_t> preserved_n7_attempts{2, 3, 4, 2, 5};
        for (std::size_t index = 0; index < survivors_.size(); ++index)
        {
            const auto attempts = survivors_.size() == preserved_n7_attempts.size()
                                      ? preserved_n7_attempts[index]
                                      : 2 + (index % 4);
            for (std::size_t attempt = 0; attempt < attempts; ++attempt)
                record(make_observation(
                    survivors_[index],
                    0,
                    ResponseOutcome::on_time,
                    "optimization-suffix"));
        }
    }

    std::uint64_t readiness_sequence(ReplicaID source) const
    {
        return readiness_sequences_.at(source);
    }

    std::uint64_t lifecycle_sequence(ReplicaID source) const
    {
        return lifecycle_sequences_.at(source);
    }

    std::uint64_t next_lifecycle_sequence(ReplicaID source)
    {
        return ++lifecycle_sequences_.at(source);
    }

    std::uint64_t evidence_sequence(ReplicaID source) const
    {
        return evidence_sequences_.at(source);
    }

    void set_all_evidence_sequences(std::uint64_t value)
    {
        std::fill(evidence_sequences_.begin(), evidence_sequences_.end(), value);
    }

private:
    uint256_t proposal_for_tree(const std::string &phase, std::uint32_t tree_id)
    {
        const auto key = std::make_pair(phase, tree_id);
        const auto existing = admitted_proposals_.find(key);
        if (existing != admitted_proposals_.end()) return existing->second;
        const auto block_hash = evidence_digest(
            phase + "-" + std::to_string(tree_id));
        const ProposalKey proposal_key{ConfigurationId{
            session_.ingress().current_epoch().epoch_number(), tree_id,
            session_.ingress().current_epoch().epoch_digest()}, block_hash};
        const auto corroborators = static_cast<ReplicaID>(
            session_.ingress().quorum_metadata().fault_threshold + 1);
        for (ReplicaID source = 0; source < corroborators; ++source) {
            const ProposalLifecycleNotice notice{
                kProposalLifecycleNoticeSchemaVersion, source,
                ++lifecycle_sequences_.at(source),
                ProposalLifecycleFact{NormalProposalRuntimeInitialized{proposal_key}}};
            const auto result = route_lifecycle(
                session_, AuthenticatedReporter{source},
                encode_proposal_lifecycle_notice(notice, limits_.lifecycle_wire));
            REQUIRE(result.status ==
                (source + 1 < corroborators
                    ? AdaptiveV2ManagerIngressStatus::awaiting_corroboration
                    : AdaptiveV2ManagerIngressStatus::processed));
        }
        admitted_proposals_.emplace(key, block_hash);
        return block_hash;
    }

    struct Edge
    {
        std::uint32_t tree_id{0};
        ReplicaID reporter{0};
    };

    Edge leaf_edge(ReplicaID target, std::size_t occurrence) const
    {
        std::size_t found = 0;
        std::set<ReplicaID> reporters;
        for (const auto &tree : session_.ingress().current_epoch().trees())
        {
            const auto position = std::find(
                tree.members_breadth_first.begin(),
                tree.members_breadth_first.end(),
                target);
            REQUIRE(position != tree.members_breadth_first.end());
            const auto index = static_cast<std::size_t>(std::distance(
                tree.members_breadth_first.begin(), position));
            if (index < first_leaf_index(
                            tree.members_breadth_first.size(), tree.fanout))
                continue;
            const auto parent = (index - 1) / tree.fanout;
            const auto reporter = tree.members_breadth_first[parent];
            if (!reporters.insert(reporter).second || found++ != occurrence)
                continue;
            return {tree.tree_id, reporter};
        }
        FAIL("target has too few leaf placements for guarded evidence");
        return {};
    }

    Session &session_;
    std::vector<ReplicaID> members_;
    std::vector<ReplicaID> survivors_;
    AdaptiveV2ManagerIngressLimits limits_;
    std::vector<std::uint64_t> readiness_sequences_;
    std::vector<std::uint64_t> lifecycle_sequences_;
    std::vector<std::uint64_t> evidence_sequences_;
    std::map<std::pair<std::string, std::uint32_t>, uint256_t>
        admitted_proposals_;
    std::uint64_t proposal_counter_{0};
};

} // namespace kauri::test_support

#endif
