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

#ifndef _HOTSTUFF_CRYPTO_H
#define _HOTSTUFF_CRYPTO_H

#include <algorithm>
#include <cstdint>
#include <limits>
#include <memory>
#include <set>
#include <vector>

#include <openssl/rand.h>

#include "secp256k1.h"
#include "salticidae/crypto.h"
#include "hotstuff/type.h"
#include "hotstuff/task.h"
#include "hotstuff/vote_identity.h"
#include "bls/src/bls.hpp"

namespace hotstuff
{

    using salticidae::SHA256;

    class PubKey : public Serializable, Cloneable
    {
    public:
        virtual ~PubKey() = default;
        virtual PubKey *clone() override = 0;
    };

    using pubkey_bt = BoxObj<PubKey>;

    class PrivKey : public Serializable
    {
    public:
        virtual ~PrivKey() = default;
        virtual pubkey_bt get_pubkey() const = 0;
        virtual void from_rand() = 0;
    };

    using privkey_bt = BoxObj<PrivKey>;

    class PartCert : public Serializable, public Cloneable
    {
    public:
        virtual ~PartCert() = default;
        virtual promise_t verify(const PubKey &pubkey, VeriPool &vpool) = 0;
        virtual bool verify(const PubKey &pubkey) = 0;
        virtual const ProposalKey &get_proposal_key() const = 0;
        virtual const uint256_t &get_obj_hash() const = 0;
        virtual PartCert *clone() override = 0;
    };

    class ReplicaConfig;
    class HotStuffBase;
    class ProposalContextLifecycle;

    class QuorumCert : public Serializable, public Cloneable
    {
        friend class HotStuffBase;
        friend class ProposalContextLifecycle;

    public:
        virtual ~QuorumCert() = default;
        virtual void add_part(const ReplicaConfig &config, ReplicaID replica, const PartCert &pc) = 0;
        void merge_quorum(const ReplicaConfig &config, QuorumCert &qc)
        {
            bool valid = false;
            try
            {
                valid = qc.verify(config);
            }
            catch (const std::exception &)
            {
                valid = false;
            }
            if (!valid)
                throw std::invalid_argument("cannot merge an invalid quorum certificate");
            merge_verified_quorum(static_cast<const QuorumCert &>(qc));
        }
        virtual bool has_n(uint32_t n) = 0;
        virtual size_t get_sigs_n() = 0;
        virtual void compute() = 0;
        virtual promise_t verify(const ReplicaConfig &config, VeriPool &vpool) = 0;
        virtual bool verify(const ReplicaConfig &config) = 0;
        virtual std::vector<ReplicaID> get_signers() const = 0;
        virtual const ProposalKey &get_proposal_key() const = 0;
        virtual const uint256_t &get_obj_hash() const = 0;
        virtual QuorumCert *clone() override = 0;

    protected:
        /**
         * Event-loop-only mutation boundaries. HotStuffBase may call these
         * only after the corresponding immutable worker verification result
         * has been accepted. Keeping them non-public prevents untrusted BLS
         * certificates from bypassing the checked public API above.
         */
        virtual void add_verified_part(const ReplicaConfig &config,
                                       ReplicaID replica,
                                       const PartCert &pc) = 0;
        virtual void merge_verified_quorum(const QuorumCert &qc) = 0;
    };

    using part_cert_bt = BoxObj<PartCert>;
    using quorum_cert_bt = BoxObj<QuorumCert>;

    vector<uint8_t> arrToVec(const bytearray_t &arr);

    class PrivKeyDummy;
    class PubKeyDummy : public PubKey
    {
        static const auto _olen = 33;
        secp256k1_pubkey data;

    public:
        PubKeyDummy() : PubKey() {}

        PubKeyDummy(const bytearray_t &raw_bytes) { from_bytes(raw_bytes); }

        inline PubKeyDummy(const PrivKeyDummy &priv_key);

        void serialize(DataStream &s) const override {}
        void unserialize(DataStream &s) override {}

        PubKeyDummy *clone() override
        {
            return new PubKeyDummy(*this);
        }
    };

    class PrivKeyDummy : public PrivKey
    {
    public:
        PrivKeyDummy() : PrivKey() {}

        PrivKeyDummy(const bytearray_t &raw_bytes) : PrivKeyDummy() { from_bytes(raw_bytes); }

        void serialize(DataStream &s) const override {}
        void unserialize(DataStream &s) override {}
        void from_rand() override {}
        inline pubkey_bt get_pubkey() const override;
    };

    pubkey_bt PrivKeyDummy::get_pubkey() const
    {
        return new PubKeyDummy(*this);
    }

    class SigSecDummy : public Serializable
    {
        uint256_t signed_digest;

    public:
        SigSecDummy() : Serializable() {}
        SigSecDummy(const uint256_t &digest,
                    const PrivKeyDummy &priv_key) : Serializable()
        {
            sign(digest, priv_key);
        }

        SigSecDummy(const SigSecDummy &obj)
            : signed_digest(obj.signed_digest) {}
        SigSecDummy(bls::G2Element sig) : Serializable() {}
        void serialize(DataStream &s) const override
        {
            s << signed_digest;
        }
        void unserialize(DataStream &s) override
        {
            s >> signed_digest;
        }
        void sign(const bytearray_t &msg, const PrivKeyDummy &)
        {
            signed_digest = uint256_t(msg);
        }
        bool verify(const bytearray_t &msg, const PubKeyDummy &) const
        {
            return signed_digest == uint256_t(msg);
        }
    };

    PubKeyDummy::PubKeyDummy(const PrivKeyDummy &priv_key) : PubKey() {}

    class PartCertDummy : public SigSecDummy, public PartCert
    {
        ProposalKey proposal_key;

    public:
        PartCertDummy() = default;
        PartCertDummy(const PrivKeyDummy &priv_key,
                      const ProposalKey &proposal_key)
            : SigSecDummy(exact_vote_authentication_digest(proposal_key),
                          priv_key),
              PartCert(),
              proposal_key(proposal_key) {}

        bool verify(const PubKey &pub_key) override
        {
            return SigSecDummy::verify(
                                       exact_vote_authentication_digest(proposal_key),
                                       static_cast<const PubKeyDummy &>(pub_key));
        }

        promise_t verify(const PubKey &pub_key, VeriPool &vpool) override
        {
            const bool valid = SigSecDummy::verify(
                exact_vote_authentication_digest(proposal_key),
                static_cast<const PubKeyDummy &>(pub_key));
            return promise_t([valid](promise_t &pm)
                             { pm.resolve(valid); });
        }

        const ProposalKey &get_proposal_key() const override
        {
            return proposal_key;
        }

        const uint256_t &get_obj_hash() const override
        {
            return proposal_key.block_hash;
        }

        PartCertDummy *clone() override
        {
            return new PartCertDummy(*this);
        }

        void serialize(DataStream &s) const override
        {
            serialize_proposal_key(s, proposal_key);
            this->SigSecDummy::serialize(s);
        }

        void unserialize(DataStream &s) override
        {
            unserialize_proposal_key(s, proposal_key);
            this->SigSecDummy::unserialize(s);
        }
    };

    class QuorumCertDummy : public QuorumCert
    {
        ProposalKey proposal_key;
        uint256_t authentication_digest;
        size_t qty = 0;
        std::set<ReplicaID> signers;

    public:
        QuorumCertDummy() = default;
        QuorumCertDummy(const QuorumCertDummy &other)
            : proposal_key(other.proposal_key),
              authentication_digest(other.authentication_digest),
              qty(other.qty),
              signers(other.signers) {}
        QuorumCertDummy(const ReplicaConfig &,
                        const ProposalKey &proposal_key)
            : proposal_key(proposal_key),
              authentication_digest(
                  exact_vote_authentication_digest(proposal_key))
        {}

        void serialize(DataStream &s) const override
        {
            s << (uint32_t)2;
            serialize_proposal_key(s, proposal_key);
            s << authentication_digest << qty;
            s << htole(static_cast<std::uint32_t>(signers.size()));
            for (const auto signer : signers)
                s << signer;
        }

        void unserialize(DataStream &s) override
        {
            uint32_t tmp;
            s >> tmp;
            if (tmp != 2)
                throw std::invalid_argument(
                    "unsupported dummy quorum certificate version");
            unserialize_proposal_key(s, proposal_key);
            s >> authentication_digest >> qty;
            std::uint32_t signer_count{0};
            s >> signer_count;
            signer_count = letoh(signer_count);
            constexpr auto max_replica_count =
                static_cast<std::size_t>(
                    std::numeric_limits<ReplicaID>::max()) + 1;
            if (signer_count > max_replica_count ||
                signer_count > s.size() / sizeof(ReplicaID))
                throw std::invalid_argument(
                    "dummy quorum signer set exceeds the remaining payload");
            signers.clear();
            for (std::uint32_t index = 0; index < signer_count; ++index)
            {
                ReplicaID signer{0};
                s >> signer;
                if (!signers.insert(signer).second)
                    throw std::invalid_argument(
                        "dummy quorum signer set contains a duplicate");
            }
            if (qty != signers.size())
                throw std::invalid_argument(
                    "dummy quorum signer count does not match its signer set");
        }

        QuorumCert *clone() override
        {
            return new QuorumCertDummy(*this);
        }

        void add_part(const ReplicaConfig &, ReplicaID replica,
                      const PartCert &part) override
        {
            if (part.get_proposal_key() != proposal_key)
                throw std::invalid_argument(
                    "part certificate does not match the proposal key");
            if (signers.insert(replica).second)
                qty = signers.size();
        }

    protected:
        void add_verified_part(const ReplicaConfig &config,
                               ReplicaID replica,
                               const PartCert &pc) override
        {
            add_part(config, replica, pc);
        }
        void merge_verified_quorum(const QuorumCert &qc) override
        {
            if (qc.get_proposal_key() != proposal_key)
                throw std::invalid_argument(
                    "quorum certificate does not match the proposal key");
            const auto *incoming =
                dynamic_cast<const QuorumCertDummy *>(&qc);
            if (incoming == nullptr ||
                incoming->qty != incoming->signers.size())
                throw std::invalid_argument(
                    "dummy quorum certificate has inconsistent signers");
            for (const auto signer : incoming->signers)
                if (signers.count(signer) != 0)
                    throw std::invalid_argument(
                        "dummy quorum certificates have overlapping signers");
            signers.insert(
                incoming->signers.begin(), incoming->signers.end());
            qty = signers.size();
        }

    public:
        bool has_n(const uint32_t n) override
        {
            return qty >= n;
        }
        size_t get_sigs_n() override
        {
            return qty;
        }
        void compute() override {}
        bool verify(const ReplicaConfig &) override
        {
            return qty == signers.size() &&
                   authentication_digest ==
                   exact_vote_authentication_digest(proposal_key);
        }
        promise_t verify(const ReplicaConfig &, VeriPool &) override
        {
            const bool valid = qty == signers.size() &&
                               authentication_digest ==
                               exact_vote_authentication_digest(proposal_key);
            return promise_t([valid](promise_t &pm)
                             { pm.resolve(valid); });
        }

        std::vector<ReplicaID> get_signers() const override
        {
            return {signers.begin(), signers.end()};
        }

        const ProposalKey &get_proposal_key() const override
        {
            return proposal_key;
        }

        const uint256_t &get_obj_hash() const override
        {
            return proposal_key.block_hash;
        }
    };

    class Secp256k1Context
    {
        secp256k1_context *ctx;
        friend class PubKeySecp256k1;
        friend class SigSecp256k1;

    public:
        Secp256k1Context(bool sign = false) : ctx(secp256k1_context_create(
                                                  sign ? SECP256K1_CONTEXT_SIGN : SECP256K1_CONTEXT_VERIFY)) {}

        Secp256k1Context(const Secp256k1Context &) = delete;

        Secp256k1Context(Secp256k1Context &&other) : ctx(other.ctx)
        {
            other.ctx = nullptr;
        }

        ~Secp256k1Context()
        {
            if (ctx)
                secp256k1_context_destroy(ctx);
        }
    };

    using secp256k1_context_t = ArcObj<Secp256k1Context>;

    extern secp256k1_context_t secp256k1_default_sign_ctx;
    extern secp256k1_context_t secp256k1_default_verify_ctx;

    class PrivKeySecp256k1;

    class PubKeySecp256k1 : public PubKey
    {
        static const auto _olen = 33;
        friend class SigSecp256k1;
        secp256k1_pubkey data;
        secp256k1_context_t ctx;

    public:
        PubKeySecp256k1(const secp256k1_context_t &ctx =
                            secp256k1_default_sign_ctx) : PubKey(), ctx(ctx) {}

        PubKeySecp256k1(const bytearray_t &raw_bytes,
                        const secp256k1_context_t &ctx =
                            secp256k1_default_sign_ctx) : PubKeySecp256k1(ctx) { from_bytes(raw_bytes); }

        inline PubKeySecp256k1(const PrivKeySecp256k1 &priv_key,
                               const secp256k1_context_t &ctx =
                                   secp256k1_default_sign_ctx);

        void serialize(DataStream &s) const override
        {
            static uint8_t output[_olen];
            size_t olen = _olen;
            (void)secp256k1_ec_pubkey_serialize(
                ctx->ctx, (unsigned char *)output,
                &olen, &data, SECP256K1_EC_COMPRESSED);
            s.put_data(output, output + _olen);
        }

        void unserialize(DataStream &s) override
        {
            static const auto _exc = std::invalid_argument("ill-formed public key");
            try
            {
                if (!secp256k1_ec_pubkey_parse(
                        ctx->ctx, &data, s.get_data_inplace(_olen), _olen))
                    throw _exc;
            }
            catch (std::ios_base::failure &)
            {
                throw _exc;
            }
        }

        PubKeySecp256k1 *clone() override
        {
            return new PubKeySecp256k1(*this);
        }
    };

    class PrivKeySecp256k1 : public PrivKey
    {
        static const auto nbytes = 32;
        friend class PubKeySecp256k1;
        friend class SigSecp256k1;
        uint8_t data[nbytes];
        secp256k1_context_t ctx;

    public:
        PrivKeySecp256k1(const secp256k1_context_t &ctx =
                             secp256k1_default_sign_ctx) : PrivKey(), ctx(ctx) {}

        PrivKeySecp256k1(const bytearray_t &raw_bytes,
                         const secp256k1_context_t &ctx =
                             secp256k1_default_sign_ctx) : PrivKeySecp256k1(ctx) { from_bytes(raw_bytes); }

        void serialize(DataStream &s) const override
        {
            s.put_data(data, data + nbytes);
        }

        void unserialize(DataStream &s) override
        {
            static const auto _exc = std::invalid_argument("ill-formed private key");
            try
            {
                memmove(data, s.get_data_inplace(nbytes), nbytes);
            }
            catch (std::ios_base::failure &)
            {
                throw _exc;
            }
        }

        void from_rand() override
        {
            if (!RAND_pseudo_bytes(data, nbytes))
                throw std::runtime_error("cannot get rand bytes from openssl");
        }

        inline pubkey_bt get_pubkey() const override;
    };

    pubkey_bt PrivKeySecp256k1::get_pubkey() const
    {
        return new PubKeySecp256k1(*this, ctx);
    }

    PubKeySecp256k1::PubKeySecp256k1(
        const PrivKeySecp256k1 &priv_key,
        const secp256k1_context_t &ctx) : PubKey(), ctx(ctx)
    {
        if (!secp256k1_ec_pubkey_create(ctx->ctx, &data, priv_key.data))
            throw std::invalid_argument("invalid secp256k1 private key");
    }

    class SigSecp256k1 : public Serializable
    {
        secp256k1_ecdsa_signature data;
        secp256k1_context_t ctx;

        static void check_msg_length(const bytearray_t &msg)
        {
            if (msg.size() != 32)
                throw std::invalid_argument("the message should be 32-bytes");
        }

    public:
        SigSecp256k1(const secp256k1_context_t &ctx =
                         secp256k1_default_sign_ctx) : Serializable(), ctx(ctx) {}
        SigSecp256k1(const uint256_t &digest,
                     const PrivKeySecp256k1 &priv_key,
                     secp256k1_context_t &ctx =
                         secp256k1_default_sign_ctx) : Serializable(), ctx(ctx)
        {
            sign(digest, priv_key);
        }

        void serialize(DataStream &s) const override
        {
            static uint8_t output[64];
            (void)secp256k1_ecdsa_signature_serialize_compact(
                ctx->ctx, (unsigned char *)output,
                &data);
            s.put_data(output, output + 64);
        }

        void unserialize(DataStream &s) override
        {
            static const auto _exc = std::invalid_argument("ill-formed signature");
            try
            {
                if (!secp256k1_ecdsa_signature_parse_compact(
                        ctx->ctx, &data, s.get_data_inplace(64)))
                    throw _exc;
            }
            catch (std::ios_base::failure &)
            {
                throw _exc;
            }
        }

        void sign(const bytearray_t &msg, const PrivKeySecp256k1 &priv_key)
        {
            // struct timeval timeStart, timeEnd;
            // gettimeofday(&timeStart, nullptr);

            check_msg_length(msg);
            if (!secp256k1_ecdsa_sign(
                    ctx->ctx, &data,
                    (unsigned char *)&*msg.begin(),
                    (unsigned char *)priv_key.data,
                    nullptr, // default nonce function
                    nullptr))
                throw std::invalid_argument("failed to create secp256k1 signature");

            // gettimeofday(&timeEnd, nullptr);

            // std::cout << "This signing slow piece of code took "
            //           << ((timeEnd.tv_sec - timeStart.tv_sec) * 1000000 + timeEnd.tv_usec - timeStart.tv_usec)
            //           << " us to execute."
            //           << std::endl;
        }

        bool verify(const bytearray_t &msg, const PubKeySecp256k1 &pub_key,
                    const secp256k1_context_t &_ctx) const
        {
            // struct timeval timeStart, timeEnd;
            // gettimeofday(&timeStart, nullptr);

            check_msg_length(msg);
            bool td = secp256k1_ecdsa_verify(
                          _ctx->ctx, &data,
                          (unsigned char *)&*msg.begin(),
                          &pub_key.data) == 1;

            // gettimeofday(&timeEnd, nullptr);

            // std::cout << "This verifying slow piece of code took "
            //           << ((timeEnd.tv_sec - timeStart.tv_sec) * 1000000 + timeEnd.tv_usec - timeStart.tv_usec)
            //           << " us to execute."
            //           << std::endl;
            return td;
        }

        bool verify(const bytearray_t &msg, const PubKeySecp256k1 &pub_key)
        {
            return verify(msg, pub_key, ctx);
        }
    };

    class Secp256k1VeriTask : public VeriTask
    {
        uint256_t msg;
        PubKeySecp256k1 pubkey;
        SigSecp256k1 sig;

    public:
        Secp256k1VeriTask(const uint256_t &msg,
                          const PubKeySecp256k1 &pubkey,
                          const SigSecp256k1 &sig) : msg(msg), pubkey(pubkey), sig(sig) {}
        virtual ~Secp256k1VeriTask() = default;

        bool verify() override
        {
            return sig.verify(msg, pubkey, secp256k1_default_verify_ctx);
        }
    };

    class PartCertSecp256k1 : public SigSecp256k1, public PartCert
    {
        ProposalKey proposal_key;

    public:
        PartCertSecp256k1() = default;
        PartCertSecp256k1(const PrivKeySecp256k1 &priv_key,
                         const ProposalKey &proposal_key)
            : SigSecp256k1(exact_vote_authentication_digest(proposal_key),
                           priv_key),
              PartCert(),
              proposal_key(proposal_key) {}

        bool verify(const PubKey &pub_key) override
        {
            return SigSecp256k1::verify(
                                        exact_vote_authentication_digest(proposal_key),
                                        static_cast<const PubKeySecp256k1 &>(pub_key),
                                        secp256k1_default_verify_ctx);
        }

        promise_t verify(const PubKey &pub_key, VeriPool &vpool) override
        {
            return vpool.verify(new Secp256k1VeriTask(
                                                      exact_vote_authentication_digest(proposal_key),
                                                      static_cast<const PubKeySecp256k1 &>(pub_key),
                                                      static_cast<const SigSecp256k1 &>(*this)));
        }

        const ProposalKey &get_proposal_key() const override
        {
            return proposal_key;
        }

        const uint256_t &get_obj_hash() const override
        {
            return proposal_key.block_hash;
        }

        PartCertSecp256k1 *clone() override
        {
            return new PartCertSecp256k1(*this);
        }

        void serialize(DataStream &s) const override
        {
            serialize_proposal_key(s, proposal_key);
            this->SigSecp256k1::serialize(s);
        }

        void unserialize(DataStream &s) override
        {
            unserialize_proposal_key(s, proposal_key);
            this->SigSecp256k1::unserialize(s);
        }
    };

    class QuorumCertSecp256k1 : public QuorumCert
    {
        ProposalKey proposal_key;
        std::unique_ptr<salticidae::Bits> rids;
        std::unordered_map<ReplicaID, SigSecp256k1> sigs;

    public:
        QuorumCertSecp256k1();
        QuorumCertSecp256k1(const ReplicaConfig &config,
                            const ProposalKey &proposal_key);
        QuorumCertSecp256k1(const QuorumCertSecp256k1 &other);

        void add_part(const ReplicaConfig &config,
                      ReplicaID rid,
                      const PartCert &pc) override;

    protected:
        void add_verified_part(const ReplicaConfig &config,
                               ReplicaID rid,
                               const PartCert &pc) override
        {
            add_part(config, rid, pc);
        }

        void merge_verified_quorum(const QuorumCert &qc) override
        {
            if (qc.get_proposal_key() != proposal_key)
                throw std::invalid_argument(
                    "quorum certificate does not match the proposal key");
            const auto &incoming =
                dynamic_cast<const QuorumCertSecp256k1 &>(qc);
            if (rids == nullptr || incoming.rids == nullptr ||
                rids->size() != incoming.rids->size())
                throw std::invalid_argument(
                    "quorum certificates use different replica sets");
            for (const auto &sig : incoming.sigs)
                if (sigs.count(sig.first) != 0)
                    throw std::invalid_argument(
                        "quorum certificates have overlapping signers");
            for (const auto &sig : incoming.sigs)
            {
                sigs.insert(std::make_pair(
                    sig.first, sig.second));
                rids->set(sig.first);
            }
        }

    public:
        bool has_n(const uint32_t n) override
        {
            // std::cout << std::to_string(sigs.size()) << " " << std::to_string(n) << std::endl;
            return sigs.size() >= n;
        }
        size_t get_sigs_n() override
        {
            return sigs.size();
        }

        std::vector<ReplicaID> get_signers() const override
        {
            std::vector<ReplicaID> signers;
            signers.reserve(sigs.size());
            for (const auto &signature : sigs)
                signers.push_back(signature.first);
            std::sort(signers.begin(), signers.end());
            return signers;
        }

        void compute() override {}

        bool verify(const ReplicaConfig &config) override;
        promise_t verify(const ReplicaConfig &config, VeriPool &vpool) override;

        const ProposalKey &get_proposal_key() const override
        {
            return proposal_key;
        }

        const uint256_t &get_obj_hash() const override
        {
            return proposal_key.block_hash;
        }

        QuorumCertSecp256k1 *clone() override
        {
            return new QuorumCertSecp256k1(*this);
        }

        void serialize(DataStream &s) const override
        {
            if (rids == nullptr)
                throw std::logic_error(
                    "quorum certificate has no signer bitmap");
            serialize_proposal_key(s, proposal_key);
            s << *rids;
            for (size_t i = 0; i < rids->size(); i++)
                if (rids->get(i))
                    s << sigs.at(i);
        }

        void unserialize(DataStream &s) override;
    };

    class PrivKeyBLS;
    class PubKeyBLS : public PubKey
    {
        static const auto _olen = bls::G1Element::SIZE;
        friend class SigSecBLS;
        friend class SigSecBLSAgg;
        friend class QuorumCertAggBLS;

        bls::G1Element *data = nullptr;

    public:

        PubKeyBLS() :
                PubKey() {}

        PubKeyBLS(const bytearray_t &raw_bytes) :
                PubKeyBLS() {
            data = new bls::G1Element(bls::G1Element::FromBytes(&raw_bytes[0]));
        }

        PubKeyBLS(const PubKeyBLS &obj)
        {
            data = new bls::G1Element(*(obj.data));
        }

        ~PubKeyBLS() override
        {
            delete data;
            data = nullptr;
        }

        inline PubKeyBLS(const PrivKeyBLS &priv_key);

        void serialize(DataStream &s) const override
        {
            static uint8_t output[bls::G1Element::SIZE];

            int i = 0;
            for (auto in : data->Serialize())
            {
                output[i++] = in;
            }
            s.put_data(output, output + bls::G1Element::SIZE);
        }

        void unserialize(DataStream &s) override
        {
            static const auto _exc = std::invalid_argument("ill-formed public key");

            try
            {
                data = new bls::G1Element(bls::G1Element::FromBytes(s.get_data_inplace(_olen)));
            }
            catch (std::ios_base::failure &)
            {
                throw _exc;
            }
        }

        PubKeyBLS *clone() override
        {
            return new PubKeyBLS(*this);
        }
    };

    class PrivKeyBLS : public PrivKey
    {
        static const auto nbytes = bls::PrivateKey::PRIVATE_KEY_SIZE;
        friend class SigSecBLS;

    public:
        bls::PrivateKey *data = nullptr;

        PrivKeyBLS() : PrivKey() {}

        PrivKeyBLS(const bytearray_t &raw_bytes) : PrivKeyBLS()
        {
            static const auto _exc = std::invalid_argument("ill-formed public key");
            try
            {
                data = new bls::PrivateKey(bls::PrivateKey::FromBytes(&raw_bytes[0]));
            }
            catch (std::ios_base::failure &)
            {
                throw _exc;
            }
        }

        ~PrivKeyBLS()
        {
            delete data;
            data = nullptr;
        }

        void serialize(DataStream &s) const override
        {
            static uint8_t output[nbytes];
            data->Serialize(output);
            s.put_data(output, output + nbytes);
        }

        void unserialize(DataStream &s) override
        {
            static const auto _exc = std::invalid_argument("ill-formed public key");
            try
            {
                const uint8_t *dat = s.get_data_inplace(bls::PrivateKey::PRIVATE_KEY_SIZE);
                data = new bls::PrivateKey(bls::PrivateKey(bls::PrivateKey::FromBytes(dat)));
            }
            catch (std::ios_base::failure &)
            {
                throw _exc;
            }
        }

        void from_rand() override
        {
            vector<uint8_t> seed = {0, 50, 6, static_cast<unsigned char>(rand() % 250), 24, 199, 1, 25, 52, 88, 192,
                                    19, 18, 12, 89, 6, static_cast<unsigned char>(rand() % 250), 18, 102, 58, 209, 82,
                                    12, 62, 89, 110, 182, static_cast<unsigned char>(rand() % 250), 44, 20, 254, 22};

            data = new bls::PrivateKey(bls::PopSchemeMPL::KeyGen(seed));
        }

        inline pubkey_bt get_pubkey() const override;
    };

    pubkey_bt PrivKeyBLS::get_pubkey() const
    {
        return new PubKeyBLS(*this);
    }

    PubKeyBLS::PubKeyBLS(const PrivKeyBLS &priv_key) : PubKey()
    {
        data = new bls::G1Element(priv_key.data->GetG1Element());
    }

    class SigSecBLS : public Serializable
    {

        static void check_msg_length(const bytearray_t &msg)
        {
            if (msg.size() != 32)
                throw std::invalid_argument("the message should be 32-bytes");
        }

    public:
        bls::G2Element *data = nullptr;

        SigSecBLS() : Serializable() {}
        SigSecBLS(const uint256_t &digest,
                  const PrivKeyBLS &priv_key) : Serializable()
        {
            sign(digest, priv_key);
        }

        SigSecBLS(const SigSecBLS &obj)
        {
            data = new bls::G2Element(*(obj.data));
        }

        SigSecBLS(bls::G2Element sig) : Serializable()
        {
            data = new bls::G2Element(sig);
        }

        ~SigSecBLS() override
        {
            delete data;
            data = nullptr;
        }

        void serialize(DataStream &s) const override
        {
            static uint8_t output[bls::G2Element::SIZE];

            int i = 0;
            for (auto in : data->Serialize())
            {
                output[i++] = in;
            }
            s.put_data(output, output + bls::G2Element::SIZE);
        }

        void unserialize(DataStream &s) override
        {
            static const auto _exc = std::invalid_argument("ill-formed signature");
            try
            {
                data = new bls::G2Element(bls::G2Element::FromBytes(s.get_data_inplace(bls::G2Element::SIZE)));
            }
            catch (std::ios_base::failure &)
            {
                throw _exc;
            }
        }

        void sign(const bytearray_t &msg, const PrivKeyBLS &priv_key)
        {
            // struct timeval timeStart, timeEnd;
            // gettimeofday(&timeStart, nullptr);

            check_msg_length(msg);
            data = new bls::G2Element(bls::PopSchemeMPL::Sign(*priv_key.data, arrToVec(msg)));

            // gettimeofday(&timeEnd, nullptr);

            // std::cout << "This signing slow piece of code took "
            //            << ((timeEnd.tv_sec - timeStart.tv_sec) * 1000000 + timeEnd.tv_usec - timeStart.tv_usec)
            //<< " us to execute."
            //         << std::endl;
        }

        bool verify(const bytearray_t &msg, const PubKeyBLS &pub_key) const
        {

            check_msg_length(msg);

            // struct timeval timeStart, timeEnd;
            // gettimeofday(&timeStart, nullptr);
            bool td = bls::PopSchemeMPL::Verify(*(pub_key.data), arrToVec(msg), *data);

            /*gettimeofday(&timeEnd, nullptr);

            std::cout << "This verifying slow piece of code took "
                      << ((timeEnd.tv_sec - timeStart.tv_sec) * 1000000 + timeEnd.tv_usec - timeStart.tv_usec)
                      << " us to execute."
                      << std::endl;*/

            return td;
        }
    };

    class SigVeriTaskBLS : public VeriTask
    {
        uint256_t msg;
        PubKeyBLS pubkey;
        SigSecBLS sig;

    public:
        SigVeriTaskBLS(const uint256_t &msg,
                       const PubKeyBLS &pubkey,
                       const SigSecBLS &sig) : msg(msg), pubkey(pubkey), sig(sig) {}
        virtual ~SigVeriTaskBLS() = default;

        bool verify() override
        {
            return sig.verify(msg, pubkey);
        }
    };

    class PartCertBLS : public SigSecBLS, public PartCert
    {
        ProposalKey proposal_key;

    public:
        PartCertBLS() = default;
        PartCertBLS(const PrivKeyBLS &priv_key,
                    const ProposalKey &proposal_key)
            : SigSecBLS(exact_vote_authentication_digest(proposal_key),
                        priv_key),
              PartCert(),
              proposal_key(proposal_key) {}

        bool verify(const PubKey &pub_key) override
        {
            return SigSecBLS::verify(
                                     exact_vote_authentication_digest(proposal_key),
                                     static_cast<const PubKeyBLS &>(pub_key));
        }

        promise_t verify(const PubKey &pub_key, VeriPool &vpool) override
        {
            return vpool.verify(new SigVeriTaskBLS(
                                                   exact_vote_authentication_digest(proposal_key),
                                                   static_cast<const PubKeyBLS &>(pub_key),
                                                   static_cast<const SigSecBLS &>(*this)));
        }

        const ProposalKey &get_proposal_key() const override
        {
            return proposal_key;
        }

        const uint256_t &get_obj_hash() const override
        {
            return proposal_key.block_hash;
        }

        PartCertBLS *clone() override
        {
            return new PartCertBLS(*this);
        }

        void serialize(DataStream &s) const override
        {
            serialize_proposal_key(s, proposal_key);
            this->SigSecBLS::serialize(s);
        }

        void unserialize(DataStream &s) override
        {
            unserialize_proposal_key(s, proposal_key);
            this->SigSecBLS::unserialize(s);
        }
    };

    class SigSecBLSAgg : public Serializable
    {

        static void check_msg_length(const bytearray_t &msg)
        {
            if (msg.size() != 32)
                throw std::invalid_argument("the message should be 32-bytes");
        }

    public:
        bls::G2Element *data = nullptr;

        SigSecBLSAgg() : Serializable() {}
        SigSecBLSAgg(const uint256_t &digest,
                     const PrivKeyBLS &priv_key) : Serializable()
        {
            sign(digest, priv_key);
        }

        SigSecBLSAgg(const SigSecBLSAgg &obj)
        {
            data = new bls::G2Element(*(obj.data));
        }

        SigSecBLSAgg(bls::G2Element sig) : Serializable()
        {
            data = new bls::G2Element(sig);
        }

        ~SigSecBLSAgg() override
        {
            delete data;
            data = nullptr;
        }

        void serialize(DataStream &s) const override
        {
            static uint8_t output[bls::G2Element::SIZE];

            int i = 0;
            for (auto in : data->Serialize())
            {
                output[i++] = in;
            }
            s.put_data(output, output + bls::G2Element::SIZE);
        }

        void unserialize(DataStream &s) override
        {
            static const auto _exc = std::invalid_argument("ill-formed signature");
            try
            {
                data = new bls::G2Element(bls::G2Element::FromBytes(s.get_data_inplace(bls::G2Element::SIZE)));
            }
            catch (std::ios_base::failure &)
            {
                throw _exc;
            }
        }

        void sign(const bytearray_t &msg, const PrivKeyBLS &priv_key)
        {
            // struct timeval timeStart, timeEnd;
            // gettimeofday(&timeStart, nullptr);

            check_msg_length(msg);
            data = new bls::G2Element(bls::PopSchemeMPL::Sign(*priv_key.data, arrToVec(msg)));

            // gettimeofday(&timeEnd, nullptr);

            // std::cout << "The signing took: "
            //           << ((timeEnd.tv_sec - timeStart.tv_sec) * 1000000 + timeEnd.tv_usec - timeStart.tv_usec)
            //           << " us to execute."
            //           << std::endl;
        }

        bool verify(const bytearray_t &msg, const PubKeyBLS &pub_key) const
        {
            check_msg_length(msg);
            return bls::PopSchemeMPL::Verify(
                    *pub_key.data, arrToVec(msg), *data);
        }
    };

    class SigVeriTaskBLSAgg : public VeriTask
    {
        uint256_t msg;
        vector<bls::G1Element> pubs;
        SigSecBLSAgg sig;

    public:
        SigVeriTaskBLSAgg(uint256_t msg,
                          vector<bls::G1Element> pubs,
                          const SigSecBLSAgg &sig) : msg(std::move(msg)), pubs(std::move(pubs)), sig(sig) {}
        virtual ~SigVeriTaskBLSAgg() = default;

        bool verify() override
        {

            // struct timeval timeStart, timeEnd;
            // gettimeofday(&timeStart, nullptr);

            bool valid = bls::PopSchemeMPL::FastAggregateVerify(pubs, arrToVec(msg.to_bytes()), *sig.data);

            // gettimeofday(&timeEnd, nullptr);

            // std::cout << "Fast Aggregate Verify:  "
            //           << ((timeEnd.tv_sec - timeStart.tv_sec) * 1000000 + timeEnd.tv_usec - timeStart.tv_usec)
            //           << " us to execute."
            //           << std::endl;
            return valid;
        }
    };

    class PartCertBLSAgg : public SigSecBLSAgg, public PartCert
    {
        ProposalKey proposal_key;

    public:
        PartCertBLSAgg() = default;
        PartCertBLSAgg(const PrivKeyBLS &priv_key,
                       const ProposalKey &proposal_key)
            : SigSecBLSAgg(exact_vote_authentication_digest(proposal_key),
                           priv_key),
              PartCert(),
              proposal_key(proposal_key) {}

        bool verify(const PubKey &pub_key) override
        {
            return SigSecBLSAgg::verify(
                                        exact_vote_authentication_digest(proposal_key),
                                        dynamic_cast<const PubKeyBLS &>(pub_key));
        }

        promise_t verify(const PubKey &pub_key, VeriPool &vpool) override
        {
            return vpool.verify(new SigVeriTaskBLS(
                                                   exact_vote_authentication_digest(proposal_key),
                                                   dynamic_cast<const PubKeyBLS &>(pub_key),
                                                   SigSecBLS(*this->data)));
        }

        const ProposalKey &get_proposal_key() const override
        {
            return proposal_key;
        }

        const uint256_t &get_obj_hash() const override
        {
            return proposal_key.block_hash;
        }

        PartCertBLSAgg *clone() override
        {
            return new PartCertBLSAgg(*this);
        }

        void serialize(DataStream &s) const override
        {
            serialize_proposal_key(s, proposal_key);
            this->SigSecBLSAgg::serialize(s);
        }

        void unserialize(DataStream &s) override
        {
            unserialize_proposal_key(s, proposal_key);
            this->SigSecBLSAgg::unserialize(s);
        }
    };

    class QuorumCertAggBLS : public QuorumCert
    {
        ProposalKey proposal_key;
        salticidae::Bits rids;
        SigSecBLSAgg *theSig = nullptr;
        vector<bls::G2Element> sigs;
        uint32_t n = 0;

    public:
        using QuorumCert::merge_quorum;

        QuorumCertAggBLS() = default;
        QuorumCertAggBLS(const ReplicaConfig &config,
                         const ProposalKey &proposal_key);
        QuorumCertAggBLS(const QuorumCertAggBLS &other);

        ~QuorumCertAggBLS() override
        {
            delete theSig;
            theSig = nullptr;
        }

        void calculateN()
        {
            n = 0;
            for (size_t i = 0; i < rids.size(); i++)
            {
                if (rids.get(i))
                {
                    n++;
                }
            }
        }

        void add_part(const ReplicaConfig &config, ReplicaID rid, const PartCert &pc) override;

        bool has_signer(ReplicaID rid) const
        {
            return rid < rids.size() && rids.get(rid);
        }

        bool has_disjoint_signers(const QuorumCertAggBLS &other) const
        {
            if (rids.size() != other.rids.size())
                return false;
            for (size_t rid = 0; rid < rids.size(); ++rid)
                if (rids.get(rid) && other.rids.get(rid))
                    return false;
            return true;
        }

        std::vector<ReplicaID> get_signers() const override
        {
            std::vector<ReplicaID> signers;
            signers.reserve(n);
            for (size_t rid = 0; rid < rids.size(); ++rid)
                if (rids.get(rid))
                    signers.push_back(static_cast<ReplicaID>(rid));
            return signers;
        }

    protected:
        void add_verified_part(const ReplicaConfig &config,
                               ReplicaID rid,
                               const PartCert &pc) override;
        void merge_verified_quorum(const QuorumCert &qc) override;

    public:

        bool has_n(const uint32_t t) override
        {
            // HOTSTUFF_LOG_PROTO("check %d of %d, sigs: %d", n, t, sigs.size());
            return n >= t;
        }

        size_t get_sigs_n() override
        {
            return n;
        }

        void compute() override
        {
            if (theSig == nullptr && !sigs.empty())
            {
                struct timeval timeStart, timeEnd;
                gettimeofday(&timeStart, nullptr);

                theSig = new SigSecBLSAgg(bls::PopSchemeMPL::Aggregate(sigs));
                sigs.clear();

                gettimeofday(&timeEnd, nullptr);

                std::cout << "Aggregating Sigs: "
                          << ((timeEnd.tv_sec - timeStart.tv_sec) * 1000000 + timeEnd.tv_usec - timeStart.tv_usec)
                          << " us to execute."
                          << std::endl;
            }
        }

        bool verify(const ReplicaConfig &config) override;
        promise_t verify(const ReplicaConfig &config, VeriPool &vpool) override;

        const ProposalKey &get_proposal_key() const override
        {
            return proposal_key;
        }

        const uint256_t &get_obj_hash() const override
        {
            return proposal_key.block_hash;
        }

        QuorumCertAggBLS *clone() override
        {
            return new QuorumCertAggBLS(*this);
        }

        void serialize(DataStream &s) const override
        {
            bool combined = (theSig != nullptr);
            serialize_proposal_key(s, proposal_key);
            s << rids << combined;
            if (combined)
            {
                if (theSig == nullptr || !sigs.empty())
                {
                    throw std::runtime_error("sigs not aggregated before sending!");
                }
                theSig->serialize(s);
            }
        }

        void unserialize(DataStream &s) override
        {
            bool combined;
            delete theSig;
            theSig = nullptr;
            sigs.clear();
            unserialize_proposal_key(s, proposal_key);
            s >> rids >> combined;
            calculateN();
            if (combined)
            {
                theSig = new SigSecBLSAgg();
                theSig->unserialize(s);
            }
        }

    private:
        // Helper function to convert bytes to a hex string
        std::string bytes_to_hex(const std::vector<uint8_t> &bytes) const
        {
            std::ostringstream oss;
            oss << std::hex << std::setfill('0');
            for (auto byte : bytes)
            {
                oss << std::setw(2) << static_cast<int>(byte);
            }
            return oss.str();
        }

        std::string serialize_g2_to_hex(const bls::G2Element &sig) const
        {
            std::vector<uint8_t> bytes = sig.Serialize();
            return bytes_to_hex(bytes);
        }
    };
}

#endif
