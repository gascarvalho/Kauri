#include "hotstuff/evidence_ingress.h"

#include <new>
#include <stdexcept>
#include <utility>

namespace hotstuff
{

const opcode_t MsgEvidenceReport::opcode;

MsgEvidenceReport::MsgEvidenceReport(
    const bytearray_t &canonical_payload)
    : serialized(canonical_payload)
{}

MsgEvidenceReport::MsgEvidenceReport(
    DataStream &&serialized_payload)
    : serialized(std::move(serialized_payload))
{}

struct EvidenceIngress::State
{
    State(EvidenceLedger &owned_ledger, EvidenceWireLimits wire_limits)
        : ledger(owned_ledger), limits(wire_limits)
    {
        configured_limits =
            limits.maximum_payload_bytes != 0 &&
            limits.maximum_observations != 0 &&
            limits.maximum_signers_per_observation != 0;
        if (!configured_limits)
            locally_healthy = false;
    }

    EvidenceLedger &ledger;
    EvidenceWireLimits limits;
    bool configured_limits{false};
    bool locally_healthy{true};
};

EvidenceIngress::EvidenceIngress(
    EvidenceLedger &ledger,
    EvidenceWireLimits limits)
    : state_(std::make_unique<State>(ledger, limits))
{}

EvidenceIngress::~EvidenceIngress() = default;

EvidenceIngressResult EvidenceIngress::ingest(
    const AuthenticatedReporter &authenticated_reporter,
    const MsgEvidenceReport &message)
{
    if (!state_->configured_limits)
    {
        return {0,
                0,
                0,
                0,
                state_->ledger.high_watermark(),
                std::nullopt};
    }

    if (message.serialized.size() >
        state_->limits.maximum_payload_bytes)
    {
        try
        {
            state_->ledger.reject_wire(
                authenticated_reporter,
                EvidenceWireError::payload_too_large);
        }
        catch (...)
        {
            state_->locally_healthy = false;
            throw;
        }
        return {0,
                0,
                0,
                1,
                state_->ledger.high_watermark(),
                EvidenceWireError::payload_too_large};
    }

    try
    {
        const bytearray_t canonical_payload =
            static_cast<bytearray_t>(message.serialized);
        return ingest(authenticated_reporter, canonical_payload);
    }
    catch (...)
    {
        state_->locally_healthy = false;
        throw;
    }
}

EvidenceIngressResult EvidenceIngress::ingest(
    const AuthenticatedReporter &authenticated_reporter,
    const bytearray_t &canonical_payload)
{
    if (!state_->configured_limits)
    {
        return {0,
                0,
                0,
                0,
                state_->ledger.high_watermark(),
                std::nullopt};
    }

    const auto decoded = decode_evidence_batch(
        canonical_payload, state_->limits);
    if (!decoded)
    {
        if (decoded.error == EvidenceWireError::allocation_failure)
        {
            state_->locally_healthy = false;
            throw std::bad_alloc();
        }
        if (decoded.error == EvidenceWireError::internal_failure)
        {
            state_->locally_healthy = false;
            throw std::runtime_error(
                "internal evidence decoder failure");
        }
        try
        {
            state_->ledger.reject_wire(
                authenticated_reporter, decoded.error);
        }
        catch (...)
        {
            state_->locally_healthy = false;
            throw;
        }
        return {0,
                0,
                0,
                1,
                state_->ledger.high_watermark(),
                decoded.error};
    }

    const auto accepted_before = state_->ledger.accepted().size();
    const auto rejected_before = state_->ledger.rejected().size();
    try
    {
        for (const auto &observation : decoded.batch->observations)
            state_->ledger.ingest(authenticated_reporter, observation);
    }
    catch (...)
    {
        state_->locally_healthy = false;
        throw;
    }

    return {
        decoded.batch->observations.size(),
        state_->ledger.accepted().size() - accepted_before,
        state_->ledger.rejected().size() - rejected_before,
        0,
        state_->ledger.high_watermark(),
        std::nullopt};
}

bool EvidenceIngress::healthy() const noexcept
{
    return state_->locally_healthy && state_->ledger.healthy();
}

} // namespace hotstuff
