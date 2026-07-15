/**
 * Copyright 2018 VMware
 *
 * Licensed under the Apache License, Version 2.0 (the "License");
 * you may not use this file except in compliance with the License.
 * You may obtain a copy of the License at
 *
 *     http://www.apache.org/licenses/LICENSE-2.0
 *
 * Unless required by applicable law or agreed to in writing, software
 * distributed under the License is distributed on an "AS IS" BASIS,
 * WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
 * See the License for the specific language governing permissions and
 * limitations under the License.
 */

#include "hotstuff/entity.h"
#include "hotstuff/crypto.h"

#include <limits>

namespace hotstuff {

    namespace {

        uint32_t count_signers(const salticidae::Bits &rids) {
            uint32_t count = 0;
            for (size_t rid = 0; rid < rids.size(); ++rid)
                if (rids.get(rid))
                    ++count;
            return count;
        }

    }

    vector<uint8_t> arrToVec(const bytearray_t &arr)
    {
        return std::vector<uint8_t>(arr.begin(), arr.end());
    }

    secp256k1_context_t secp256k1_default_sign_ctx = new Secp256k1Context(true);
    secp256k1_context_t secp256k1_default_verify_ctx = new Secp256k1Context(false);

    QuorumCertSecp256k1::QuorumCertSecp256k1() :
            QuorumCert(),
            rids(std::make_unique<salticidae::Bits>(0)) {}

    QuorumCertSecp256k1::QuorumCertSecp256k1(
            const ReplicaConfig &config,
            const ProposalKey &proposal_key) :
            QuorumCert(), proposal_key(proposal_key),
            rids(std::make_unique<salticidae::Bits>(config.nreplicas)) {
        rids->clear();
    }

    QuorumCertSecp256k1::QuorumCertSecp256k1(
            const QuorumCertSecp256k1 &other) :
            QuorumCert(other),
            proposal_key(other.proposal_key),
            rids(other.rids == nullptr
                     ? nullptr
                     : std::make_unique<salticidae::Bits>(*other.rids)),
            sigs(other.sigs) {}

    void QuorumCertSecp256k1::add_part(
            const ReplicaConfig &config,
            ReplicaID rid,
            const PartCert &pc) {
        if (rids == nullptr || rids->size() != config.nreplicas ||
            rid >= config.nreplicas)
            throw std::invalid_argument(
                    "part certificate signer is outside the replica set");
        if (pc.get_proposal_key() != proposal_key)
            throw std::invalid_argument(
                    "part certificate does not match the proposal key");
        sigs.insert(std::make_pair(
                rid, dynamic_cast<const PartCertSecp256k1 &>(pc)));
        rids->set(rid);
    }

    void QuorumCertSecp256k1::unserialize(DataStream &s) {
        DataStream serialized(s);
        ProposalKey next_key;
        unserialize_proposal_key(serialized, next_key);

        DataStream bitmap_preflight(serialized);
        std::uint32_t encoded_bits{0};
        bitmap_preflight >> encoded_bits;
        const auto bit_count =
                static_cast<std::size_t>(letoh(encoded_bits));
        constexpr auto max_replica_count =
                static_cast<std::size_t>(
                        std::numeric_limits<ReplicaID>::max()) + 1;
        if (bit_count > max_replica_count)
            throw std::invalid_argument(
                    "quorum signer bitmap exceeds the replica id space");

        constexpr std::size_t bits_per_word = sizeof(std::uint64_t) * 8;
        const auto word_count =
                (bit_count + bits_per_word - 1) / bits_per_word;
        if (word_count >
            bitmap_preflight.size() / sizeof(std::uint64_t))
            throw std::invalid_argument(
                    "quorum signer bitmap exceeds the remaining payload");

        auto next_rids = std::make_unique<salticidae::Bits>();
        serialized >> *next_rids;
        if (next_rids->size() != bit_count)
            throw std::invalid_argument(
                    "quorum signer bitmap changed during parsing");

        const auto signer_count = count_signers(*next_rids);
        constexpr std::size_t signature_wire_size = 64;
        if (signer_count > serialized.size() / signature_wire_size)
            throw std::invalid_argument(
                    "quorum signatures exceed the remaining payload");

        std::unordered_map<ReplicaID, SigSecp256k1> next_sigs;
        next_sigs.reserve(signer_count);
        for (std::size_t rid = 0; rid < next_rids->size(); ++rid) {
            if (!next_rids->get(rid))
                continue;
            SigSecp256k1 signature;
            serialized >> signature;
            next_sigs.emplace(static_cast<ReplicaID>(rid),
                              std::move(signature));
        }
        if (next_sigs.size() != signer_count)
            throw std::invalid_argument(
                    "quorum signer bitmap and signatures diverged");

        s = std::move(serialized);
        proposal_key = next_key;
        rids = std::move(next_rids);
        sigs = std::move(next_sigs);
    }

    bool QuorumCertSecp256k1::verify(const ReplicaConfig &config) {
        if (rids == nullptr || rids->size() != config.nreplicas ||
            sigs.empty() || count_signers(*rids) != sigs.size())
            return false;
        try {
            for (size_t i = 0; i < rids->size(); i++)
                if (rids->get(i)) {
                    const auto signature = sigs.find(
                            static_cast<ReplicaID>(i));
                    if (signature == sigs.end())
                        return false;
                HOTSTUFF_LOG_DEBUG("checking cert(%d), obj_hash=%s",
                                   i, get_hex10(get_obj_hash()).c_str());
                if (!signature->second.verify(
                                    exact_vote_authentication_digest(proposal_key),
                                    static_cast<const PubKeySecp256k1 &>(config.get_pubkey(i)),
                                    secp256k1_default_verify_ctx))
                    return false;
                }
            return true;
        }
        catch (const std::exception &) {
            return false;
        }
    }

    promise_t QuorumCertSecp256k1::verify(const ReplicaConfig &config, VeriPool &vpool) {
        if (rids == nullptr || rids->size() != config.nreplicas ||
            sigs.empty() || count_signers(*rids) != sigs.size())
            return promise_t([](promise_t &pm) { pm.resolve(false); });
        std::vector<promise_t> vpm;
        try {
            for (size_t i = 0; i < rids->size(); i++)
                if (rids->get(i)) {
                    const auto signature = sigs.find(
                            static_cast<ReplicaID>(i));
                    if (signature == sigs.end())
                        return promise_t(
                                [](promise_t &pm) { pm.resolve(false); });
                HOTSTUFF_LOG_DEBUG("checking cert(%d), obj_hash=%s",
                                   i, get_hex10(get_obj_hash()).c_str());
                vpm.push_back(vpool.verify(new Secp256k1VeriTask(
                                                                 exact_vote_authentication_digest(proposal_key),
                                                                 static_cast<const PubKeySecp256k1 &>(config.get_pubkey(
                                                                         i)),
                                                                 signature->second)));
                }
        }
        catch (const std::exception &) {
            return promise_t([](promise_t &pm) { pm.resolve(false); });
        }
        return promise::all(vpm).then([](const promise::values_t &values) {
            for (const auto &v: values)
                if (!promise::any_cast<bool>(v)) return false;
            return true;
        });
    }

    QuorumCertAggBLS::QuorumCertAggBLS(
            const ReplicaConfig &config,
            const ProposalKey &proposal_key) :
            QuorumCert(), proposal_key(proposal_key),
            rids(config.nreplicas){
        rids.clear();
    }

    QuorumCertAggBLS::QuorumCertAggBLS(const QuorumCertAggBLS &other) :
            QuorumCert(other),
            proposal_key(other.proposal_key),
            rids(other.rids),
            theSig(nullptr),
            sigs(other.sigs),
            n(other.n) {
        if (other.theSig != nullptr)
            theSig = new SigSecBLSAgg(*other.theSig);
    }

    void QuorumCertAggBLS::add_part(const ReplicaConfig &config,
                                    ReplicaID rid,
                                    const PartCert &pc) {
        if (rids.size() != config.nreplicas || rid >= config.nreplicas)
            throw std::invalid_argument("part certificate signer is outside the replica set");
        if (pc.get_proposal_key() != proposal_key)
            throw std::invalid_argument(
                    "part certificate does not match the proposal key");

        const auto *part = dynamic_cast<const PartCertBLSAgg *>(&pc);
        if (part == nullptr || part->data == nullptr)
            throw std::invalid_argument("part certificate is not a valid BLS certificate");

        bool valid = false;
        try {
            const auto &pubkey =
                    dynamic_cast<const PubKeyBLS &>(config.get_pubkey(rid));
            valid = part->SigSecBLSAgg::verify(
                    exact_vote_authentication_digest(proposal_key), pubkey);
        }
        catch (const std::exception &) {
            valid = false;
        }
        if (!valid)
            throw std::invalid_argument("part certificate signer does not match the claimed replica");

        add_verified_part(config, rid, pc);
    }

    void QuorumCertAggBLS::add_verified_part(const ReplicaConfig &config,
                                             ReplicaID rid,
                                             const PartCert &pc) {
        if (rids.size() != config.nreplicas || rid >= config.nreplicas)
            throw std::invalid_argument("part certificate signer is outside the replica set");
        if (pc.get_proposal_key() != proposal_key)
            throw std::invalid_argument(
                    "part certificate does not match the proposal key");

        const auto *part = dynamic_cast<const PartCertBLSAgg *>(&pc);
        if (part == nullptr || part->data == nullptr)
            throw std::invalid_argument("part certificate is not a valid BLS certificate");

        if (has_signer(rid))
            return;

        const auto counted_signers = count_signers(rids);
        const bool has_pending_signatures = !sigs.empty();
        if (counted_signers != n ||
            (theSig != nullptr && has_pending_signatures) ||
            (n == 0 && (theSig != nullptr || has_pending_signatures)) ||
            (n > 0 && theSig == nullptr && !has_pending_signatures))
            throw std::logic_error("BLS accumulator signer and signature state diverged");

        // Allocate and copy all cryptographic state before changing the bitmap.
        // If any allocation fails, the existing accumulator remains unchanged.
        vector<bls::G2Element> next_sigs(sigs);
        if (theSig != nullptr) {
            if (theSig->data == nullptr)
                throw std::logic_error("BLS accumulator contains an empty aggregate signature");
            next_sigs.push_back(*theSig->data);
        }
        next_sigs.push_back(*part->data);

        delete theSig;
        theSig = nullptr;
        sigs = std::move(next_sigs);
        rids.set(rid);
        calculateN();
    }

    void QuorumCertAggBLS::merge_verified_quorum(const QuorumCert &qc) {
        if (qc.get_proposal_key() != proposal_key)
            throw std::invalid_argument(
                    "quorum certificate does not match the proposal key");

        const auto *incoming = dynamic_cast<const QuorumCertAggBLS *>(&qc);
        if (incoming == nullptr)
            throw std::invalid_argument("quorum certificate is not a BLS aggregate");
        if (rids.size() != incoming->rids.size())
            throw std::invalid_argument("quorum certificates use different replica sets");
        if (!has_disjoint_signers(*incoming))
            throw std::invalid_argument("quorum certificate signer sets overlap");

        const auto current_count = count_signers(rids);
        const auto incoming_count = count_signers(incoming->rids);
        const bool current_pending = !sigs.empty();
        const bool incoming_pending = !incoming->sigs.empty();
        if (current_count != n || incoming_count != incoming->n ||
            (theSig != nullptr && current_pending) ||
            (incoming->theSig != nullptr && incoming_pending) ||
            (n == 0 && (theSig != nullptr || current_pending)) ||
            (n > 0 && theSig == nullptr && !current_pending) ||
            (incoming->n == 0 &&
             (incoming->theSig != nullptr || incoming_pending)) ||
            (incoming->n > 0 &&
             incoming->theSig == nullptr && !incoming_pending))
            throw std::invalid_argument("quorum certificate signer and signature state diverged");

        // Prebuild the complete signature list. Bitmap mutation happens only
        // after every validation and potentially-throwing allocation succeeds.
        vector<bls::G2Element> next_sigs(sigs);
        if (theSig != nullptr) {
            if (theSig->data == nullptr)
                throw std::logic_error("BLS accumulator contains an empty aggregate signature");
            next_sigs.push_back(*theSig->data);
        }
        next_sigs.insert(next_sigs.end(),
                         incoming->sigs.begin(), incoming->sigs.end());
        if (incoming->theSig != nullptr) {
            if (incoming->theSig->data == nullptr)
                throw std::invalid_argument("incoming BLS aggregate signature is empty");
            next_sigs.push_back(*incoming->theSig->data);
        }

        delete theSig;
        theSig = nullptr;
        sigs = std::move(next_sigs);
        for (size_t rid = 0; rid < rids.size(); ++rid)
            if (incoming->rids.get(rid))
                rids.set(rid);
        calculateN();
    }

    bool QuorumCertAggBLS::verify(const ReplicaConfig &config) {
        if (rids.size() != config.nreplicas ||
            theSig == nullptr || theSig->data == nullptr || !sigs.empty() ||
            n == 0 || count_signers(rids) != n)
            return false;

        try {
            vector<bls::G1Element> pubs;
            pubs.reserve(n);
            for (size_t rid = 0; rid < rids.size(); ++rid)
                if (rids.get(rid))
                    pubs.push_back(*dynamic_cast<const PubKeyBLS &>(
                            config.get_pubkey(static_cast<ReplicaID>(rid))).data);
            return pubs.size() == n &&
                   bls::PopSchemeMPL::FastAggregateVerify(
                           pubs,
                           arrToVec(exact_vote_authentication_digest(
                               proposal_key).to_bytes()),
                           *theSig->data);
        }
        catch (const std::exception &) {
            return false;
        }
    }

    promise_t QuorumCertAggBLS::verify(const ReplicaConfig &config, VeriPool &vpool) {
        if (rids.size() != config.nreplicas ||
            theSig == nullptr || theSig->data == nullptr || !sigs.empty() ||
            n == 0 || count_signers(rids) != n) {
            return promise_t([](promise_t &pm) { pm.resolve(false); });
        }

        try {
            vector<bls::G1Element> pubs;
            pubs.reserve(n);
            for (size_t rid = 0; rid < rids.size(); ++rid)
                if (rids.get(rid))
                    pubs.push_back(*dynamic_cast<const PubKeyBLS &>(
                            config.get_pubkey(static_cast<ReplicaID>(rid))).data);
            if (pubs.size() != n)
                return promise_t([](promise_t &pm) { pm.resolve(false); });
            return vpool.verify(new SigVeriTaskBLSAgg(
                    exact_vote_authentication_digest(proposal_key),
                    pubs,
                    *theSig));
        }
        catch (const std::exception &) {
            return promise_t([](promise_t &pm) { pm.resolve(false); });
        }
    }
}
