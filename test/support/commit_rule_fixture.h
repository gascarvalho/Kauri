#ifndef KAURI_TEST_SUPPORT_COMMIT_RULE_FIXTURE_H
#define KAURI_TEST_SUPPORT_COMMIT_RULE_FIXTURE_H

#include <cstddef>
#include <cstdint>
#include <optional>
#include <stdexcept>
#include <string>
#include <vector>

#include "hotstuff/consensus.h"
#include "hotstuff/crypto.h"
#include "support/bls_fixtures.h"

namespace hotstuff::test
{

struct CommittedBlock
{
    std::uint32_t height;
    uint256_t hash;
};

enum class CommitCallbackKind : std::uint8_t
{
    consensus = 0,
    decide,
    post_block_commit,
};

struct CommitCallbackObservation
{
    CommitCallbackKind kind;
    std::uint32_t height;
    uint256_t hash;
    std::optional<std::uint64_t> commit_batch_index;
    std::optional<ProposalKey> certifying_proposal;
};

class CommitRuleCore final : public HotStuffCore
{
public:
    explicit CommitRuleCore(std::size_t replica_count = 4,
                            ReplicaID local_id = 0)
        : HotStuffCore(
              local_id,
              new PrivKeyBLS(make_bls_private_key_bytes(local_id)))
    {
        if (replica_count == 0 || local_id >= replica_count)
            throw std::invalid_argument("invalid commit-rule membership");

        for (std::size_t index = 0; index < replica_count; ++index)
        {
            const auto replica_id = static_cast<ReplicaID>(index);
            auto private_key = make_bls_private_key(replica_id);
            const NetAddr address(
                static_cast<std::uint32_t>(0x7f000001),
                static_cast<std::uint16_t>(13000 + replica_id));
            add_replica(replica_id,
                        PeerId(address),
                        private_key.get_pubkey());
        }

        on_init(static_cast<std::uint32_t>((replica_count - 1) / 3));
    }

    block_t add_block(const block_t &parent, const block_t &qc_reference)
    {
        return add_block(parent, qc_reference, false);
    }

    block_t add_empty_block(const block_t &parent,
                            const block_t &qc_reference)
    {
        return add_block(parent, qc_reference, true);
    }

    block_t make_undelivered_block(const block_t &parent,
                                   const block_t &qc_reference)
    {
        return make_block(parent, qc_reference, false);
    }

    const std::vector<CommitCallbackObservation> &callbacks() const
    {
        return callbacks_;
    }

private:
    block_t add_block(const block_t &parent,
                      const block_t &qc_reference,
                      bool empty_commands)
    {
        block_t block = make_block(
            parent, qc_reference, empty_commands);

        storage->add_blk(block);
        if (!on_deliver_blk(block))
            throw std::runtime_error("fixture block was not delivered");
        return block;
    }

public:

    void corrupt_qc_object_hash(const block_t &block,
                                const uint256_t &wrong_hash)
    {
        if (!block || !block->get_qc())
            throw std::invalid_argument("test block requires a QC");

        auto replacement_key = block->get_qc()->get_proposal_key();
        replacement_key.block_hash = wrong_hash;
        quorum_cert_bt replacement = create_quorum_cert(replacement_key);
        DataStream stream;
        stream << *replacement;
        block->get_qc()->unserialize(stream);
    }

    bool lock_is(const block_t &block) const
    {
        if (!block)
            return false;

        // b_lock is private; the production diagnostic is its read-only seam.
        const std::string marker =
            "b_lock=" + get_hex10(block->get_hash()) + " ";
        return static_cast<std::string>(*this).find(marker) !=
               std::string::npos;
    }

    void apply_update(const block_t &block)
    {
        update(block);
    }

    const std::vector<CommittedBlock> &committed() const
    {
        return committed_;
    }

    bool committed_hash(const uint256_t &hash) const
    {
        for (const auto &block : committed_)
            if (block.hash == hash)
                return true;
        return false;
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

    quorum_cert_bt create_quorum_cert(
        const ProposalKey &key) override
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
    void do_decide(Finality &&finality) override
    {
        callbacks_.push_back(CommitCallbackObservation{
            CommitCallbackKind::decide,
            finality.cmd_height,
            finality.blk_hash,
            std::nullopt,
            std::nullopt});
    }

    void do_consensus(const block_t &block) override
    {
        do_consensus(block, nullptr);
    }

    void do_consensus(
        const block_t &block,
        const quorum_cert_bt &verified_direct_certifier) override
    {
        callbacks_.push_back(CommitCallbackObservation{
            CommitCallbackKind::consensus,
            block->get_height(),
            block->get_hash(),
            std::nullopt,
            verified_direct_certifier == nullptr
                ? std::nullopt
                : std::optional<ProposalKey>{
                      verified_direct_certifier->get_proposal_key()}});
        committed_.push_back(
            CommittedBlock{block->get_height(), block->get_hash()});
    }

    void do_post_block_commit(
        const block_t &block,
        std::uint64_t commit_batch_index) override
    {
        callbacks_.push_back(CommitCallbackObservation{
            CommitCallbackKind::post_block_commit,
            block->get_height(),
            block->get_hash(),
            commit_batch_index,
            std::nullopt});
    }

    void do_broadcast_proposal(const Proposal &) override {}
    void do_vote(Proposal, const Vote &) override {}

    void start_proposal_timer(std::size_t,
                              std::size_t,
                              uint256_t,
                              double,
                              std::size_t) override {}

private:
    block_t make_block(const block_t &parent,
                       const block_t &qc_reference,
                       bool empty_commands)
    {
        if (!parent)
            throw std::invalid_argument("test block requires a parent");

        quorum_cert_bt certificate;
        if (qc_reference)
        {
            const auto key = qc_reference == get_genesis()
                ? genesis_certification_key(qc_reference->get_hash())
                : make_test_proposal_key(qc_reference->get_hash());
            certificate = create_quorum_cert(key);
        }
        std::vector<uint256_t> commands;
        if (!empty_commands)
        {
            commands.push_back(
                make_digest(static_cast<std::uint8_t>(next_marker_++)));
        }

        block_t block = new Block(
            std::vector<block_t>{parent},
            commands,
            std::move(certificate),
            bytearray_t(),
            parent->get_height() + 1,
            qc_reference,
            nullptr);
        return block;
    }

    std::uint16_t next_marker_ = 1;
    std::vector<CommittedBlock> committed_;
    std::vector<CommitCallbackObservation> callbacks_;
};

inline std::vector<block_t> add_direct_chain(CommitRuleCore &core,
                                             const block_t &parent,
                                             std::size_t length)
{
    std::vector<block_t> chain;
    chain.reserve(length);

    block_t previous = parent;
    for (std::size_t index = 0; index < length; ++index)
    {
        previous = core.add_block(previous, previous);
        chain.push_back(previous);
    }
    return chain;
}

} // namespace hotstuff::test

#endif
