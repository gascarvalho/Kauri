#ifndef KAURI_TEST_SUPPORT_BLS_FIXTURES_H
#define KAURI_TEST_SUPPORT_BLS_FIXTURES_H

#include <cstddef>
#include <cstdint>
#include <stdexcept>
#include <string>
#include <vector>

#include "hotstuff/consensus.h"
#include "hotstuff/crypto.h"

namespace hotstuff::test
{

inline bytearray_t make_bls_private_key_bytes(ReplicaID replica_id)
{
    bytearray_t raw(bls::PrivateKey::PRIVATE_KEY_SIZE, 0);
    const auto scalar = static_cast<std::uint32_t>(replica_id) + 1;
    raw[raw.size() - 4] = static_cast<std::uint8_t>(scalar >> 24);
    raw[raw.size() - 3] = static_cast<std::uint8_t>(scalar >> 16);
    raw[raw.size() - 2] = static_cast<std::uint8_t>(scalar >> 8);
    raw[raw.size() - 1] = static_cast<std::uint8_t>(scalar);
    return raw;
}

inline PrivKeyBLS make_bls_private_key(ReplicaID replica_id)
{
    return PrivKeyBLS(make_bls_private_key_bytes(replica_id));
}

inline uint256_t make_digest(std::uint8_t marker)
{
    bytearray_t bytes(32, marker);
    return uint256_t(bytes);
}

inline ProposalKey make_test_proposal_key(
    const uint256_t &block_hash,
    std::uint8_t epoch_marker = 0xd1,
    std::uint32_t epoch_number = 1,
    std::uint32_t tree_id = 1)
{
    return ProposalKey{
        ConfigurationId{
            epoch_number, tree_id, make_digest(epoch_marker)},
        block_hash};
}

class BlsTestCore final : public HotStuffCore
{
public:
    explicit BlsTestCore(std::size_t replica_count, ReplicaID local_id = 0)
        : HotStuffCore(local_id,
                       new PrivKeyBLS(make_bls_private_key_bytes(local_id)))
    {
        if (replica_count == 0 || local_id >= replica_count)
            throw std::invalid_argument("invalid BLS test membership");

        for (std::size_t index = 0; index < replica_count; ++index)
        {
            const auto replica_id = static_cast<ReplicaID>(index);
            auto private_key = make_bls_private_key(replica_id);
            const NetAddr address(
                static_cast<std::uint32_t>(0x7f000001),
                static_cast<std::uint16_t>(11000 + replica_id));
            add_replica(replica_id,
                        PeerId(address),
                        private_key.get_pubkey());
        }

        const auto fault_threshold = (replica_count - 1) / 3;
        config.nmajority = 2 * fault_threshold + 1;
    }

    Vote make_vote(ReplicaID claimed_voter,
                   ReplicaID signing_replica,
                   const ProposalKey &key)
    {
        auto signing_key = make_bls_private_key(signing_replica);
        return Vote(claimed_voter,
                    key,
                    new PartCertBLSAgg(signing_key, key),
                    this);
    }

    part_cert_bt make_part(ReplicaID signing_replica,
                           const ProposalKey &key)
    {
        auto signing_key = make_bls_private_key(signing_replica);
        return new PartCertBLSAgg(signing_key, key);
    }

    part_cert_bt create_part_cert(const PrivKey &private_key,
                                  const ProposalKey &key) override
    {
        return new PartCertBLSAgg(
            static_cast<const PrivKeyBLS &>(private_key), key);
    }

    part_cert_bt parse_part_cert(DataStream &stream) override
    {
        PartCert *part = new PartCertBLSAgg();
        stream >> *part;
        return part;
    }

    quorum_cert_bt create_quorum_cert(const ProposalKey &key) override
    {
        return new QuorumCertAggBLS(get_config(), key);
    }

    quorum_cert_bt parse_quorum_cert(DataStream &stream) override
    {
        QuorumCert *quorum = new QuorumCertAggBLS();
        stream >> *quorum;
        return quorum;
    }

protected:
    void do_decide(Finality &&) override {}
    void do_consensus(const block_t &) override {}
    void do_broadcast_proposal(const Proposal &) override {}
    void do_vote(Proposal, const Vote &) override {}
    void start_proposal_timer(std::size_t,
                              std::size_t,
                              uint256_t,
                              double,
                              std::size_t) override {}
};

inline void add_vote_without_prescribing_rejection_style(
    QuorumCertAggBLS &accumulator,
    const ReplicaConfig &config,
    const Vote &vote)
{
    try
    {
        accumulator.add_part(config, vote.voter, *vote.cert);
    }
    catch (const std::invalid_argument &)
    {
        // Explicit rejection and an idempotent no-op are both acceptable as long
        // as the accumulator remains unchanged.
    }
}

inline void add_part_without_prescribing_rejection_style(
    QuorumCertAggBLS &accumulator,
    const ReplicaConfig &config,
    ReplicaID claimed_voter,
    const PartCert &part)
{
    try
    {
        accumulator.add_part(config, claimed_voter, part);
    }
    catch (const std::invalid_argument &)
    {
        // See add_vote_without_prescribing_rejection_style.
    }
}

inline void add_valid_signers(QuorumCertAggBLS &certificate,
                              BlsTestCore &core,
                              const std::vector<ReplicaID> &signers,
                              const ProposalKey &key)
{
    for (const auto signer : signers)
    {
        auto part = core.make_part(signer, key);
        certificate.add_part(core.get_config(), signer, *part);
    }
}

inline std::string serialized_hex(const QuorumCertAggBLS &certificate)
{
    DataStream stream;
    stream << certificate;
    return stream.get_hex();
}

} // namespace hotstuff::test

#endif
