#include "hotstuff/adaptive_manager_session_facade.h"
#include <stdexcept>
namespace hotstuff {
struct AdaptiveManagerSessionFacade::State {
    AdaptiveManagerSessionMode mode;
    std::unique_ptr<AdaptiveV2ManagerSession> v2;
    std::unique_ptr<AdaptiveV3ManagerSession> v3;
    State(std::vector<ReplicaID> members, EpochDefinitionInput initial,
          AdaptiveManagerSessionFacadeConfig config) : mode(config.mode) {
        if ((mode != AdaptiveManagerSessionMode::adaptive_v2 &&
             mode != AdaptiveManagerSessionMode::adaptive_v3) ||
            (mode == AdaptiveManagerSessionMode::adaptive_v2 &&
             (!config.v2 || config.v3)) ||
            (mode == AdaptiveManagerSessionMode::adaptive_v3 &&
             (!config.v3 || config.v2)))
            throw std::invalid_argument("adaptive manager session facade requires exactly one mode config");
        if (mode == AdaptiveManagerSessionMode::adaptive_v2)
            v2 = std::make_unique<AdaptiveV2ManagerSession>(std::move(members), std::move(initial), std::move(*config.v2));
        else v3 = std::make_unique<AdaptiveV3ManagerSession>(std::move(members), std::move(initial), std::move(*config.v3));
    }
};
AdaptiveManagerSessionFacade::AdaptiveManagerSessionFacade(std::vector<ReplicaID> m, EpochDefinitionInput e, AdaptiveManagerSessionFacadeConfig c):state_(new State(std::move(m),std::move(e),std::move(c))){}
AdaptiveManagerSessionFacade::~AdaptiveManagerSessionFacade()=default;
AdaptiveManagerSessionMode AdaptiveManagerSessionFacade::mode() const noexcept{return state_->mode;}
const AdaptiveV2ManagerIngress &AdaptiveManagerSessionFacade::ingress() const noexcept{return state_->v2?state_->v2->ingress():state_->v3->ingress();}
AdaptiveV2ManagerReadinessResult AdaptiveManagerSessionFacade::ingest_readiness(const AuthenticatedReporter &s,const MsgAdaptiveV2ReadinessNotice&m)noexcept{return state_->v2?state_->v2->ingest_readiness(s,m):state_->v3->ingest_readiness(s,m);}
AdaptiveV2ManagerLifecycleResult AdaptiveManagerSessionFacade::ingest_lifecycle(const AuthenticatedReporter&s,const MsgProposalLifecycleNotice&m)noexcept{return state_->v2?state_->v2->ingest_lifecycle(s,m):state_->v3->ingest_lifecycle(s,m);}
AdaptiveV2ManagerEvidenceResult AdaptiveManagerSessionFacade::ingest_evidence(const AuthenticatedReporter&s,const MsgEvidenceReport&m)noexcept{return state_->v2?state_->v2->ingest_evidence(s,m):state_->v3->ingest_evidence(s,m);}
bool AdaptiveManagerSessionFacade::begin_cycle(const AdaptiveV2TransitionPolicy&p)noexcept{return state_->v2?state_->v2->begin_cycle(p):state_->v3->begin_cycle(p);}
bool AdaptiveManagerSessionFacade::arm_fault_window(AdaptiveV2FaultWindowArm a)noexcept{return state_->v2?state_->v2->arm_fault_window(std::move(a)):state_->v3->arm_fault_window(std::move(a));}
AdaptiveV2ManagerControllerStatus AdaptiveManagerSessionFacade::evaluate()noexcept{return state_->v2?state_->v2->evaluate():state_->v3->evaluate();}
std::optional<AdaptiveV2ManagerControllerAuditSnapshot> AdaptiveManagerSessionFacade::controller_audit()const noexcept{return state_->v2?state_->v2->controller_audit():state_->v3->controller_audit();}
bool AdaptiveManagerSessionFacade::v2_start_convergence(std::uint64_t t) noexcept { return state_->v2 && state_->v2->start_convergence(t); }
std::vector<AdaptiveV2ManagerDeliveryRequest> AdaptiveManagerSessionFacade::v2_due_deliveries(std::uint64_t t) noexcept { return state_->v2 ? state_->v2->due_deliveries(t) : std::vector<AdaptiveV2ManagerDeliveryRequest>{}; }
AdaptiveV2ManagerConvergenceDisposition AdaptiveManagerSessionFacade::v2_record_enqueue_result(ReplicaID r,std::uint32_t a,bool e) noexcept { return state_->v2 ? state_->v2->record_enqueue_result(r,a,e) : AdaptiveV2ManagerConvergenceDisposition::terminal; }
AdaptiveV2ManagerConvergenceDisposition AdaptiveManagerSessionFacade::v2_observe_commit(ReplicaID r,const AdaptiveV2EpochChangeCommittedObservation &o) noexcept { return state_->v2 ? state_->v2->observe_commit(r,o) : AdaptiveV2ManagerConvergenceDisposition::terminal; }
AdaptiveV2ManagerConvergenceDisposition AdaptiveManagerSessionFacade::v2_observe_activation(ReplicaID r,const AdaptiveV2EpochActivatedObservation &o) noexcept { return state_->v2 ? state_->v2->observe_activation(r,o) : AdaptiveV2ManagerConvergenceDisposition::terminal; }
std::optional<AdaptiveV2ManagerConvergenceStatus> AdaptiveManagerSessionFacade::v2_convergence_status() const noexcept { return state_->v2 ? state_->v2->convergence_status() : std::nullopt; }
std::optional<AdaptiveV2ManagerConvergenceAuditSnapshot> AdaptiveManagerSessionFacade::v2_convergence_audit() const noexcept { return state_->v2 ? state_->v2->convergence_audit() : std::nullopt; }
bool AdaptiveManagerSessionFacade::v2_consume_ready_and_rotate() noexcept { return state_->v2 && state_->v2->consume_ready_and_rotate(); }
bool AdaptiveManagerSessionFacade::v2_finalize_noop_cycle(AdaptiveV2ManagerCycleTerminalReason r) noexcept { return state_->v2 && state_->v2->finalize_noop_cycle(r); }
bool AdaptiveManagerSessionFacade::v2_finalize_failed_cycle(AdaptiveV2ManagerCycleTerminalReason r) noexcept { return state_->v2 && state_->v2->finalize_failed_cycle(r); }
const std::vector<AdaptiveV2ManagerSessionTerminalRecord> *AdaptiveManagerSessionFacade::v2_terminal_records() const noexcept { return state_->v2 ? &state_->v2->terminal_records() : nullptr; }
bool AdaptiveManagerSessionFacade::v3_arm_hard_deadline(std::uint64_t t) noexcept { return state_->v3 && state_->v3->arm_hard_deadline(t); }
AdaptiveV2ManagerLifecycleResult AdaptiveManagerSessionFacade::v3_ingest_timed_lifecycle(const AuthenticatedReporter&s,const MsgProposalLifecycleNotice&m,std::uint64_t t) noexcept { return state_->v3 ? state_->v3->ingest_timed_lifecycle(s,m,t) : AdaptiveV2ManagerLifecycleResult{}; }
bool AdaptiveManagerSessionFacade::v3_begin_readiness(std::uint64_t t) noexcept { return state_->v3 && state_->v3->begin_readiness(t); }
AdaptiveV3ManagerObservationResult AdaptiveManagerSessionFacade::v3_observe_readiness(ReplicaID p,std::uint64_t t,const bytearray_t &b) noexcept { return state_->v3 ? state_->v3->observe_readiness(p,t,b) : AdaptiveV3ManagerObservationResult{}; }
std::optional<AdaptiveV3CertificateDelivery> AdaptiveManagerSessionFacade::v3_begin_delivery(ReplicaID p,std::uint64_t t) noexcept { return state_->v3 ? state_->v3->begin_delivery(p,t) : std::nullopt; }
AdaptiveV3CertificateDeliveryDisposition AdaptiveManagerSessionFacade::v3_record_delivery_result(ReplicaID p,std::uint32_t a,bool e,std::uint64_t t) noexcept { return state_->v3 ? state_->v3->record_delivery_result(p,a,e,t) : AdaptiveV3CertificateDeliveryDisposition::rejected_ack; }
AdaptiveV3CertificateDeliveryDisposition AdaptiveManagerSessionFacade::v3_acknowledge(ReplicaID p,std::uint64_t t,const bytearray_t &b) noexcept { return state_->v3 ? state_->v3->acknowledge(p,t,b) : AdaptiveV3CertificateDeliveryDisposition::rejected_ack; }
bool AdaptiveManagerSessionFacade::v3_e2_eligible(std::uint64_t t) const noexcept { return state_->v3 && state_->v3->e2_eligible(t); }
std::optional<AdaptiveV3E2EligibilityAuditSnapshot> AdaptiveManagerSessionFacade::v3_e2_eligibility_audit(std::uint64_t t) const noexcept { return state_->v3 ? state_->v3->e2_eligibility_audit(t) : std::nullopt; }
std::optional<AdaptiveV3E2EligibilityAuditSnapshot> AdaptiveManagerSessionFacade::v3_begin_e2_at(std::uint64_t t,const AdaptiveV2TransitionPolicy &p) noexcept { return state_->v3 ? state_->v3->begin_e2_at(t,p) : std::nullopt; }
void AdaptiveManagerSessionFacade::v3_advance(std::uint64_t t) noexcept { if (state_->v3) state_->v3->advance(t); }
std::optional<AdaptiveV3ManagerSessionStatus> AdaptiveManagerSessionFacade::v3_status() const noexcept { return state_->v3 ? std::optional<AdaptiveV3ManagerSessionStatus>(state_->v3->status()) : std::nullopt; }
const AdaptiveV3ManagerSessionTerminalRecord *AdaptiveManagerSessionFacade::v3_terminal_audit() const noexcept { return state_->v3 ? state_->v3->terminal_audit() : nullptr; }
const std::vector<AdaptiveV3ManagerSessionTerminalRecord> *AdaptiveManagerSessionFacade::v3_terminal_records() const noexcept { return state_->v3 ? &state_->v3->terminal_records() : nullptr; }
const ActivationReadinessCertificateV1 *AdaptiveManagerSessionFacade::v3_certificate() const noexcept { return state_->v3 ? state_->v3->certificate() : nullptr; }
const AdaptiveV2EpochChangeBundle *AdaptiveManagerSessionFacade::v2_successor_bundle()const noexcept{return state_->v2?state_->v2->successor_bundle():nullptr;}
const AdaptiveV3EpochChangeBundle *AdaptiveManagerSessionFacade::v3_successor_bundle()const noexcept{return state_->v3?state_->v3->successor_bundle():nullptr;}
AdaptiveV2ManagerSession *AdaptiveManagerSessionFacade::v2()noexcept{return state_->v2.get();}
const AdaptiveV2ManagerSession *AdaptiveManagerSessionFacade::v2()const noexcept{return state_->v2.get();}
AdaptiveV3ManagerSession *AdaptiveManagerSessionFacade::v3()noexcept{return state_->v3.get();}
const AdaptiveV3ManagerSession *AdaptiveManagerSessionFacade::v3()const noexcept{return state_->v3.get();}
} // namespace hotstuff
