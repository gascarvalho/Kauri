#include "hotstuff/adaptive_v3_manager_session.h"

#include <algorithm>
#include <limits>
#include <stdexcept>

namespace hotstuff {
namespace {
std::uint64_t deadline(std::uint64_t start, std::uint64_t span) noexcept {
    return span > std::numeric_limits<std::uint64_t>::max() - start
        ? std::numeric_limits<std::uint64_t>::max() : start + span;
}
}

struct AdaptiveV3ManagerSession::State {
    AdaptiveV3ManagerSessionConfig config;
    AdaptiveV2ManagerIngress ingress;
    std::unique_ptr<AdaptiveV2ManagerController> controller;
    std::optional<AdaptiveV3TransitionProjection> projection;
    std::unique_ptr<AdaptiveV3ManagerReadinessCollector> collector;
    std::unique_ptr<AdaptiveV3CertificateOutbox> outbox;
    AdaptiveV3ManagerSessionStatus phase{AdaptiveV3ManagerSessionStatus::idle};
    std::uint64_t cycle_ordinal{0}, last_tick{0}, readiness_deadline{0},
        delivery_deadline{0}, residency_until{0};
    std::uint64_t final_ack_tick{0};
    std::optional<ProposalKey> common_commit;
    std::uint64_t common_commit_tick{0};
    std::optional<std::uint64_t> hard_deadline_tick;
    struct CommitCandidate { ProposalKey key; std::vector<ReplicaID> sources; };
    std::vector<CommitCandidate> commit_candidates;
    std::optional<ConfigurationId> expected_commit_configuration;
    std::vector<ReplicaID> required_commit_sources;
    std::vector<ReplicaID> observed_commit_sources;
    std::optional<std::size_t> terminal_index;
    std::vector<AdaptiveV3ManagerSessionTerminalRecord> terminal_records;

    State(std::vector<ReplicaID> members, EpochDefinitionInput initial,
          AdaptiveV3ManagerSessionConfig input)
        : config(std::move(input)), ingress(std::move(members), std::move(initial),
          config.active_tree_id, config.activation_generation, config.ingress_limits) {
        if (config.active_tree_id != 0 ||
            config.controller.successor_protocol_mode != EpochProtocolMode::adaptive_v3 ||
            config.pre_certificate_window_ticks == 0 ||
            config.delivery_window_ticks == 0 || config.residency_ticks == 0 ||
            config.common_commit_window_ticks == 0 ||
            config.common_commit_stabilization_ticks == 0 ||
            config.e2_reserve_ticks == 0 ||
            (config.expected_cycle_count != 1 && config.expected_cycle_count != 2) ||
            config.maximum_delivery_attempts == 0 || config.retry_interval_ticks == 0 ||
            config.readiness_membership.size() != ingress.membership().size())
            throw std::invalid_argument("invalid adaptive-v3 manager session configuration");
        for (std::size_t i = 0; i < config.readiness_membership.size(); ++i)
            if (config.readiness_membership[i].first != ingress.membership()[i])
                throw std::invalid_argument("v3 readiness membership differs from ingress");
        terminal_records.reserve(2);
        required_commit_sources.reserve(ingress.membership().size());
        observed_commit_sources.reserve(ingress.membership().size());
        commit_candidates.reserve(ingress.membership().size());
    }
    void clear_cycle(AdaptiveV3ManagerSessionStatus next) noexcept {
        collector.reset(); outbox.reset(); projection.reset(); controller.reset();
        phase = next;
    }
    bool append_terminal(const AdaptiveV3ManagerSessionTerminalRecord &record) noexcept {
        if (terminal_records.size() >= terminal_records.capacity()) return false;
        try { terminal_records.push_back(record); terminal_index = terminal_records.size() - 1; return true; }
        catch (...) { return false; }
    }
    bool has_terminal_for_current_cycle() const noexcept {
        return terminal_index && *terminal_index < terminal_records.size() &&
            terminal_records[*terminal_index].cycle_ordinal == cycle_ordinal;
    }
    bool expire_hard_deadline(std::uint64_t tick) noexcept {
        if (!hard_deadline_tick || phase == AdaptiveV3ManagerSessionStatus::terminal ||
            tick < *hard_deadline_tick)
            return false;
        if (!has_terminal_for_current_cycle()) {
            const AdaptiveV3ManagerSessionTerminalRecord record{
                cycle_ordinal, AdaptiveV3ManagerSessionTerminalReason::hard_deadline_exhausted,
                projection ? projection->canonical_bundle_digest : uint256_t{},
                std::nullopt, {}, {}};
            static_cast<void>(append_terminal(record));
        }
        clear_cycle(AdaptiveV3ManagerSessionStatus::terminal);
        return true;
    }
};

AdaptiveV3ManagerSession::AdaptiveV3ManagerSession(
    std::vector<ReplicaID> members, EpochDefinitionInput initial,
    AdaptiveV3ManagerSessionConfig config)
    : state_(new State(std::move(members), std::move(initial), std::move(config))) {}
AdaptiveV3ManagerSession::~AdaptiveV3ManagerSession() = default;
const AdaptiveV2ManagerIngress &AdaptiveV3ManagerSession::ingress() const noexcept { return state_->ingress; }
AdaptiveV2ManagerReadinessResult AdaptiveV3ManagerSession::ingest_readiness(
    const AuthenticatedReporter &source, const MsgAdaptiveV2ReadinessNotice &message) noexcept {
    return state_->ingress.ingest_readiness(source, message);
}
AdaptiveV2ManagerReadinessResult AdaptiveV3ManagerSession::ingest_readiness(
    const AuthenticatedReporter &source, const bytearray_t &payload) noexcept {
    return state_->ingress.ingest_readiness(source, payload);
}
AdaptiveV2ManagerLifecycleResult AdaptiveV3ManagerSession::ingest_lifecycle(
    const AuthenticatedReporter &source, const MsgProposalLifecycleNotice &message) noexcept {
    return state_->ingress.ingest_lifecycle(source, message);
}
AdaptiveV2ManagerLifecycleResult AdaptiveV3ManagerSession::ingest_lifecycle(
    const AuthenticatedReporter &source, const bytearray_t &payload) noexcept {
    return state_->ingress.ingest_lifecycle(source, payload);
}
AdaptiveV2ManagerLifecycleResult AdaptiveV3ManagerSession::ingest_timed_lifecycle(
    const AuthenticatedReporter &source, const MsgProposalLifecycleNotice &message,
    std::uint64_t tick) noexcept {
    auto &s = *state_;
    if (tick < s.last_tick) return {};
    const auto decoded = decode_proposal_lifecycle_notice(
        static_cast<bytearray_t>(message.serialized), s.config.ingress_limits.lifecycle_wire);
    if (!decoded) return s.ingress.ingest_lifecycle(source, message);
    s.last_tick = tick;
    auto result = s.ingress.ingest_lifecycle(source, message);
    if (s.phase != AdaptiveV3ManagerSessionStatus::residency ||
        (result.status != AdaptiveV2ManagerIngressStatus::awaiting_corroboration &&
         result.status != AdaptiveV2ManagerIngressStatus::processed &&
         result.status != AdaptiveV2ManagerIngressStatus::already_applied))
        return result;
    const auto *committed = std::get_if<ProposalCommitted>(&decoded.notice->fact);
    if (committed == nullptr ||
        std::find(s.required_commit_sources.begin(), s.required_commit_sources.end(),
                  source.replica_id) == s.required_commit_sources.end() ||
        !s.expected_commit_configuration ||
        committed->proposal.configuration != *s.expected_commit_configuration)
        return result;
    // tC1 is immutable: later duplicates and distinct candidates are inert.
    if (s.common_commit) return result;
    auto candidate = std::find_if(s.commit_candidates.begin(), s.commit_candidates.end(),
        [&committed](const auto &value) { return value.key == committed->proposal; });
    try {
        if (candidate == s.commit_candidates.end()) {
            if (s.commit_candidates.size() >= s.required_commit_sources.size()) return result;
            s.commit_candidates.push_back({committed->proposal, {}});
            candidate = std::prev(s.commit_candidates.end());
        }
        if (std::find(candidate->sources.begin(), candidate->sources.end(), source.replica_id) == candidate->sources.end())
            candidate->sources.push_back(source.replica_id);
        if (candidate->sources.size() == s.required_commit_sources.size() &&
            tick > s.final_ack_tick && tick < deadline(
                s.final_ack_tick, s.config.common_commit_window_ticks)) {
            std::sort(candidate->sources.begin(), candidate->sources.end());
            s.common_commit = candidate->key; s.common_commit_tick = tick;
            s.observed_commit_sources = candidate->sources;
        }
    } catch (...) { }
    return result;
}
AdaptiveV2ManagerEvidenceResult AdaptiveV3ManagerSession::ingest_evidence(
    const AuthenticatedReporter &source, const MsgEvidenceReport &message) noexcept {
    return state_->ingress.ingest_evidence(source, message);
}
AdaptiveV2ManagerEvidenceResult AdaptiveV3ManagerSession::ingest_evidence(
    const AuthenticatedReporter &source, const bytearray_t &payload) noexcept {
    return state_->ingress.ingest_evidence(source, payload);
}

bool AdaptiveV3ManagerSession::begin_cycle(const AdaptiveV2TransitionPolicy &policy) noexcept {
    try {
        auto &s=*state_;
        if (s.expire_hard_deadline(s.last_tick)) return false;
        if (s.phase == AdaptiveV3ManagerSessionStatus::residency && s.last_tick < s.residency_until)
            return false;
        if (s.phase == AdaptiveV3ManagerSessionStatus::residency) {
            if (!e2_eligible(s.last_tick)) return false;
            s.phase=AdaptiveV3ManagerSessionStatus::idle;
        }
        if (s.phase != AdaptiveV3ManagerSessionStatus::idle || s.cycle_ordinal >= s.config.expected_cycle_count) return false;
        if ((s.cycle_ordinal == 0 && policy.intent != TreePolicyKind::fault_containment) ||
            (s.cycle_ordinal == 1 && policy.intent != TreePolicyKind::performance_optimization)) return false;
        s.terminal_index.reset();
        auto controller_config=s.config.controller; controller_config.transition_policy=policy;
        if (policy.intent != TreePolicyKind::fault_containment ||
            s.cycle_ordinal != 0 || s.ingress.current_epoch().epoch_number() != 0) {
            controller_config.selection.fault_containment_evidence_start_monotonic_ns = 0;
            controller_config.selection.fault_containment_required_tree_coverage = 0;
            controller_config.selection.fault_window_arm_required = false;
            controller_config.selection.fault_window_arm.reset();
        }
        controller_config.shape_adaptation_enabled = policy.apply_shape_selection;
        controller_config.successor_protocol_mode=EpochProtocolMode::adaptive_v3;
        s.controller=std::make_unique<AdaptiveV2ManagerController>(s.ingress, std::move(controller_config)); s.phase=AdaptiveV3ManagerSessionStatus::selecting;
        return true;
    } catch (...) { return false; }
}

bool AdaptiveV3ManagerSession::arm_fault_window(AdaptiveV2FaultWindowArm arm) noexcept {
    auto &s = *state_;
    return s.phase == AdaptiveV3ManagerSessionStatus::selecting &&
           s.controller != nullptr && s.controller->arm_fault_window(std::move(arm));
}
bool AdaptiveV3ManagerSession::arm_hard_deadline(std::uint64_t tick) noexcept {
    auto &s = *state_;
    if (tick == 0 || s.hard_deadline_tick || s.cycle_ordinal != 0 ||
        s.phase != AdaptiveV3ManagerSessionStatus::selecting)
        return false;
    s.hard_deadline_tick = tick;
    return true;
}

AdaptiveV2ManagerControllerStatus AdaptiveV3ManagerSession::evaluate() noexcept {
    auto &s=*state_;
    if (s.expire_hard_deadline(s.last_tick)) return AdaptiveV2ManagerControllerStatus::unhealthy;
    if (!s.controller || s.phase != AdaptiveV3ManagerSessionStatus::selecting) return AdaptiveV2ManagerControllerStatus::unhealthy;
    const auto result=s.controller->evaluate();
    if ((result != AdaptiveV2ManagerControllerStatus::successor_ready && result != AdaptiveV2ManagerControllerStatus::already_ready) || s.projection) return result;
    const auto *bundle=s.controller->successor_bundle_v3();
    if (!bundle) return AdaptiveV2ManagerControllerStatus::unhealthy;
    s.projection=make_adaptive_v3_transition_projection(*bundle,
        s.ingress.current_epoch(), s.cycle_ordinal,
        s.config.readiness_membership);
    if (!s.projection) return AdaptiveV2ManagerControllerStatus::unhealthy;
    s.phase=AdaptiveV3ManagerSessionStatus::successor_available; return result;
}
std::optional<AdaptiveV2ManagerControllerAuditSnapshot>
AdaptiveV3ManagerSession::controller_audit() const noexcept {
    const auto *controller = state_->controller.get();
    if (controller == nullptr) return std::nullopt;
    try {
        AdaptiveV2ManagerControllerAuditSnapshot snapshot;
        snapshot.baseline_cutoff = controller->baseline_cutoff();
        snapshot.current_cutoff = controller->current_cutoff();
        snapshot.baseline_frozen = controller->baseline_frozen();
        snapshot.score_trajectory = controller->score_trajectory();
        if (const auto *decision = controller->shape_decision()) snapshot.shape_decision = *decision;
        if (const auto *detail = controller->failure_detail()) snapshot.controller_failure = *detail;
        return snapshot;
    } catch (...) { return std::nullopt; }
}
const AdaptiveV2ManagerControllerFailureDetail *
AdaptiveV3ManagerSession::controller_failure_detail() const noexcept {
    return state_->controller ? state_->controller->failure_detail() : nullptr;
}
const AdaptiveV3EpochChangeBundle *AdaptiveV3ManagerSession::successor_bundle() const noexcept {
    return state_->controller ? state_->controller->successor_bundle_v3() : nullptr;
}
bool AdaptiveV3ManagerSession::begin_readiness(std::uint64_t tick) noexcept {
    try { auto &s=*state_; if (s.expire_hard_deadline(tick) || s.phase != AdaptiveV3ManagerSessionStatus::successor_available || !s.projection || !s.hard_deadline_tick || tick < s.last_tick) return false;
        if (s.cycle_ordinal == 1 &&
            (tick > std::numeric_limits<std::uint64_t>::max() - s.config.e2_reserve_ticks ||
             tick + s.config.e2_reserve_ticks >= *s.hard_deadline_tick)) {
            static_cast<void>(s.expire_hard_deadline(*s.hard_deadline_tick));
            return false;
        }
        s.last_tick=tick; s.readiness_deadline=deadline(tick,s.config.pre_certificate_window_ticks);
        s.collector=std::make_unique<AdaptiveV3ManagerReadinessCollector>(*s.projection,
            AdaptiveV3ManagerReadinessConfig{s.config.readiness_membership,s.config.required_release_count});
        s.phase=AdaptiveV3ManagerSessionStatus::collecting; return true;
    } catch (...) { return false; }
}
AdaptiveV3ManagerObservationResult AdaptiveV3ManagerSession::observe_readiness(ReplicaID peer,std::uint64_t tick,const bytearray_t &payload) noexcept {
    AdaptiveV3ManagerObservationResult r; auto &s=*state_;
    if (s.expire_hard_deadline(tick) || s.phase != AdaptiveV3ManagerSessionStatus::collecting || !s.collector || tick < s.last_tick || tick >= s.readiness_deadline) return r;
    s.last_tick=tick;
    const auto decoded=decode_activation_ready_observation(payload,s.config.wire_limits); r.wire_error=decoded.error; if(!decoded) return r;
    r.observation_digest=activation_ready_observation_digest(*decoded.value); r.disposition=s.collector->ingest(peer,*decoded.value);
    // A signer conflict is quarantined by the collector. Other authenticated
    // sources remain eligible to form Q/R; only bounded capacity or a clock
    // deadline can terminate this cycle.
    if (r.disposition == AdaptiveV3ManagerReadinessDisposition::released) {
        const auto *certificate=s.collector->certificate(); if(!certificate) return r;
        try { std::vector<ReplicaID> recipients; recipients.reserve(certificate->observations.size()); for(const auto &o:certificate->observations) recipients.push_back(o.signer_replica_id);
          s.outbox=std::make_unique<AdaptiveV3CertificateOutbox>(*certificate,AdaptiveV3CertificateDeliveryConfig{std::move(recipients),s.config.maximum_delivery_attempts,s.config.retry_interval_ticks,s.config.wire_limits});
          s.delivery_deadline=deadline(s.last_tick,s.config.delivery_window_ticks); s.phase=AdaptiveV3ManagerSessionStatus::distributing; r.certificate_assembled=true; } catch (...) { r.disposition=AdaptiveV3ManagerReadinessDisposition::rejected_invalid_observation; }
    }
    return r;
}
std::optional<AdaptiveV3CertificateDelivery> AdaptiveV3ManagerSession::begin_delivery(ReplicaID id,std::uint64_t tick) noexcept {
    auto &s=*state_;
    if (s.expire_hard_deadline(tick) || s.phase != AdaptiveV3ManagerSessionStatus::distributing || !s.outbox ||
        tick < s.last_tick || tick >= s.delivery_deadline) return std::nullopt;
    s.last_tick=tick;
    return s.outbox->begin(id,tick);
}
AdaptiveV3CertificateDeliveryDisposition AdaptiveV3ManagerSession::record_delivery_result(ReplicaID id,std::uint32_t attempt,bool ok,std::uint64_t tick) noexcept {
    auto &s=*state_;
    if (s.expire_hard_deadline(tick) || s.phase != AdaptiveV3ManagerSessionStatus::distributing || !s.outbox ||
        tick < s.last_tick || tick >= s.delivery_deadline)
        return AdaptiveV3CertificateDeliveryDisposition::invalid_ack;
    s.last_tick=tick;
    const auto result = s.outbox->result(id,attempt,ok,tick);
    if (result != AdaptiveV3CertificateDeliveryDisposition::retry_exhausted)
        return result;
    if (!s.has_terminal_for_current_cycle()) {
        const AdaptiveV3ManagerSessionTerminalRecord record{
            s.cycle_ordinal,
            AdaptiveV3ManagerSessionTerminalReason::delivery_retry_exhausted,
            s.projection ? s.projection->canonical_bundle_digest : uint256_t{},
            std::nullopt, {}, {}};
        static_cast<void>(s.append_terminal(record));
    }
    s.clear_cycle(AdaptiveV3ManagerSessionStatus::terminal);
    return result;
}

AdaptiveV3CertificateDeliveryDisposition AdaptiveV3ManagerSession::acknowledge(ReplicaID peer,std::uint64_t tick,const bytearray_t &payload) noexcept {
    auto &s=*state_; if(s.expire_hard_deadline(tick) || s.phase != AdaptiveV3ManagerSessionStatus::distributing || !s.outbox || tick < s.last_tick || tick >= s.delivery_deadline) return AdaptiveV3CertificateDeliveryDisposition::invalid_ack;
    s.last_tick=tick;
    const auto decoded=decode_activation_readiness_ack(payload,s.config.wire_limits);
    if(!decoded || decoded.value->recipient_replica_id != peer) return AdaptiveV3CertificateDeliveryDisposition::invalid_ack;
    const auto result=s.outbox->acknowledge(*decoded.value, tick); if(result != AdaptiveV3CertificateDeliveryDisposition::acknowledged || !s.outbox->terminal()) return result;
    const auto *bundle=successor_bundle(); const auto *cert=s.collector ? s.collector->certificate() : nullptr;
    const auto q=static_cast<std::size_t>(s.ingress.quorum_metadata().quorum);
    const auto fail_invalid_rotation = [&]() noexcept {
        if (!s.has_terminal_for_current_cycle()) {
            const AdaptiveV3ManagerSessionTerminalRecord record{
                s.cycle_ordinal, AdaptiveV3ManagerSessionTerminalReason::invalid_rotation,
                s.projection ? s.projection->canonical_bundle_digest : uint256_t{},
                std::nullopt, {}, {}};
            static_cast<void>(s.append_terminal(record));
        }
        s.ingress.discard_prepared_window();
        s.clear_cycle(AdaptiveV3ManagerSessionStatus::terminal);
    };
    if(!bundle || !cert || cert->observations.size()<q || s.terminal_index) { fail_invalid_rotation(); return AdaptiveV3CertificateDeliveryDisposition::invalid_ack; }
    try {
        std::vector<ReplicaID> r_sources; r_sources.reserve(cert->observations.size());
        for(const auto &o:cert->observations) r_sources.push_back(o.signer_replica_id);
        std::vector<ReplicaID> q_sources; q_sources.reserve(q);
        q_sources.assign(r_sources.begin(),r_sources.begin()+q);
        s.required_commit_sources = r_sources;
        s.observed_commit_sources.clear(); s.commit_candidates.clear();
        s.common_commit.reset(); s.common_commit_tick = 0;
        s.expected_commit_configuration = cert->identity.successor_configuration;
        s.final_ack_tick = s.last_tick;
        const auto prepared=s.ingress.prepare_successor_rotation(bundle->definition(),s.config.active_tree_id);
        if(prepared != AdaptiveV2ManagerIngressStatus::processed || s.ingress.seed_prepared_successor_readiness(q_sources,cert->identity.activation_height) != AdaptiveV2ManagerIngressStatus::processed) throw std::runtime_error("rotation");
        const AdaptiveV3ManagerSessionTerminalRecord record{s.cycle_ordinal,AdaptiveV3ManagerSessionTerminalReason::acknowledgements_complete,s.projection->canonical_bundle_digest,cert->identity,std::move(q_sources),std::move(r_sources)};
        if (!s.append_terminal(record)) throw std::runtime_error("terminal");
    } catch (...) { fail_invalid_rotation(); return AdaptiveV3CertificateDeliveryDisposition::invalid_ack; }
    s.ingress.publish_prepared_window(); ++s.cycle_ordinal;
    if (s.cycle_ordinal == s.config.expected_cycle_count) { s.clear_cycle(AdaptiveV3ManagerSessionStatus::terminal); return result; }
    s.residency_until=deadline(s.last_tick,s.config.residency_ticks); s.clear_cycle(AdaptiveV3ManagerSessionStatus::residency); return result;
}
void AdaptiveV3ManagerSession::advance(std::uint64_t tick) noexcept {
    auto &s=*state_; if (tick < s.last_tick) return; s.last_tick=tick;
    const auto fail=[&](AdaptiveV3ManagerSessionTerminalReason reason) {
        if (!s.has_terminal_for_current_cycle()) {
            const AdaptiveV3ManagerSessionTerminalRecord record{
                s.cycle_ordinal, reason,
                s.projection ? s.projection->canonical_bundle_digest : uint256_t{},
                std::nullopt, {}, {}};
            static_cast<void>(s.append_terminal(record));
        }
        s.clear_cycle(AdaptiveV3ManagerSessionStatus::terminal);
    };
    if (s.expire_hard_deadline(tick)) return;
    if(s.phase==AdaptiveV3ManagerSessionStatus::collecting && tick>=s.readiness_deadline)
        fail(AdaptiveV3ManagerSessionTerminalReason::pre_certificate_deadline);
    else if(s.phase==AdaptiveV3ManagerSessionStatus::distributing && tick>=s.delivery_deadline)
        fail(AdaptiveV3ManagerSessionTerminalReason::delivery_deadline);
    else if(s.phase==AdaptiveV3ManagerSessionStatus::distributing && s.outbox&&s.outbox->retry_exhausted(tick))
        fail(AdaptiveV3ManagerSessionTerminalReason::delivery_retry_exhausted);
    else if(s.phase==AdaptiveV3ManagerSessionStatus::residency &&
            (!s.hard_deadline_tick || tick >= *s.hard_deadline_tick ||
             tick > std::numeric_limits<std::uint64_t>::max() -
                 s.config.e2_reserve_ticks ||
             tick + s.config.e2_reserve_ticks >= *s.hard_deadline_tick))
        fail(AdaptiveV3ManagerSessionTerminalReason::hard_deadline_exhausted);
    else if(s.phase==AdaptiveV3ManagerSessionStatus::residency && !s.common_commit &&
            tick >= deadline(
                s.final_ack_tick, s.config.common_commit_window_ticks))
        fail(AdaptiveV3ManagerSessionTerminalReason::common_commit_missing);
}
AdaptiveV3ManagerSessionStatus AdaptiveV3ManagerSession::status() const noexcept{return state_->phase;}
bool AdaptiveV3ManagerSession::e2_eligible(std::uint64_t now) const noexcept {
    const auto &s = *state_;
    if (s.cycle_ordinal != 1 || !s.hard_deadline_tick || !s.common_commit ||
        s.required_commit_sources.empty() ||
        s.observed_commit_sources.size() != s.required_commit_sources.size() ||
        now < s.residency_until || s.final_ack_tick == 0 ||
        now > std::numeric_limits<std::uint64_t>::max() -
            s.config.e2_reserve_ticks ||
        now + s.config.e2_reserve_ticks >= *s.hard_deadline_tick)
        return false;
    const auto earliest = std::max(
        deadline(s.final_ack_tick, s.config.residency_ticks),
        deadline(s.common_commit_tick,
                 s.config.common_commit_stabilization_ticks));
    return now >= earliest;
}
std::optional<AdaptiveV3E2EligibilityAuditSnapshot>
AdaptiveV3ManagerSession::e2_eligibility_audit(std::uint64_t now) const noexcept {
    const auto &s = *state_;
    if (!e2_eligible(now) || !s.common_commit || !s.hard_deadline_tick ||
        s.terminal_records.empty())
        return std::nullopt;
    // The preceding E1 terminal record is append-only and is the only source
    // of the now-cleared E1 identity/bundle after residency begins.
    const auto &e1 = s.terminal_records.back();
    if (e1.cycle_ordinal != 0 || !e1.identity ||
        e1.bundle_digest == uint256_t{})
        return std::nullopt;
    try {
        AdaptiveV3E2EligibilityAuditSnapshot snapshot;
        snapshot.cycle_ordinal = s.cycle_ordinal;
        snapshot.e1_identity = e1.identity;
        snapshot.e1_bundle_digest = e1.bundle_digest;
        snapshot.final_ack_tick = s.final_ack_tick;
        snapshot.common_commit = *s.common_commit;
        snapshot.common_commit_sources = s.observed_commit_sources;
        std::sort(snapshot.common_commit_sources.begin(),
                  snapshot.common_commit_sources.end());
        snapshot.common_commit_tick = s.common_commit_tick;
        snapshot.earliest_e2_tick = std::max(
            deadline(s.final_ack_tick, s.config.residency_ticks),
            deadline(s.common_commit_tick,
                     s.config.common_commit_stabilization_ticks));
        snapshot.actual_e2_begin_tick = now;
        snapshot.hard_deadline_tick = *s.hard_deadline_tick;
        snapshot.reserve_ticks = s.config.e2_reserve_ticks;
        return snapshot;
    } catch (...) { return std::nullopt; }
}
std::optional<AdaptiveV3E2EligibilityAuditSnapshot>
AdaptiveV3ManagerSession::begin_e2_at(
    std::uint64_t tick, const AdaptiveV2TransitionPolicy &policy) noexcept {
    auto &s = *state_;
    if (tick < s.last_tick || !e2_eligible(tick))
        return std::nullopt;
    s.last_tick = tick;
    if (!begin_cycle(policy))
        return std::nullopt;
    return e2_eligibility_audit(tick);
}
const AdaptiveV3ManagerSessionTerminalRecord *AdaptiveV3ManagerSession::terminal_audit() const noexcept {
    return state_->terminal_index ?
        &state_->terminal_records[*state_->terminal_index] : nullptr;
}
const std::vector<AdaptiveV3ManagerSessionTerminalRecord> &AdaptiveV3ManagerSession::terminal_records() const noexcept { return state_->terminal_records; }
const ActivationReadinessCertificateV1 *AdaptiveV3ManagerSession::certificate() const noexcept{return state_->collector?state_->collector->certificate():nullptr;}
} // namespace hotstuff
