/**
 * Copyright 2018 VMware
 * Copyright 2018 Ted Yin
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

#ifndef _HOTSTUFF_CONSENSUS_H
#define _HOTSTUFF_CONSENSUS_H

#include <cassert>
#include <cstdint>
#include <set>
#include <unordered_map>

#include "hotstuff/promise.hpp"
#include "hotstuff/type.h"
#include "hotstuff/entity.h"
#include "hotstuff/crypto.h"
#include "hotstuff/future_proposal_buffer.h"

namespace hotstuff
{

    enum class LeaderTimeoutRotationDisposition : std::uint8_t
    {
        legacy_fallback = 0,
        rejected,
        rotated,
    };

    struct Proposal;
    struct Vote;
    struct Finality;
    struct VoteRelay;

    enum ReconfigurationType
    {
        NO_SWITCH, // No switch happened

        TREE_SWITCH, // A tree switch happened
        EPOCH_SWITCH // An epoch switch happened
    };

    /** Abstraction for HotStuff protocol state machine (without network implementation). */
    class HotStuffCore
    {
        block_t b0; /** the genesis block */
        /* === state variables === */
        /**< highest QC */
        block_t b_lock; /**< locked block */
        block_t b_exec; /**< last executed block */
        /**< height of the block last voted for */
        /**< private key for signing votes */
        std::set<block_t> tails; /**< set of tail blocks */
        /**< replica configuration */

        uint64_t decided_blk_counter = 0; /** for simple stat print */

        /* === async event queues === */

        std::unordered_map<block_t, promise_t> qc_waiting;
        promise_t propose_waiting;
        promise_t receive_proposal_waiting;
        promise_t hqc_update_waiting;
        /* == feature switches == */
        /** always vote negatively, useful for some PaceMakers */
        bool vote_disabled;

        void sanity_check_delivered(const block_t &blk);

        void on_hqc_update();

        void on_receive_proposal_(const Proposal &prop);

    protected:
        ReplicaID id; /**< identity of the replica itself */

        const block_t &committed_head() const noexcept { return b_exec; }

        block_t get_delivered_blk(const uint256_t &blk_hash);

        block_t get_potentially_not_delivered_blk(const uint256_t &blk_hash);

        ReplicaConfig config;

        void update_hqc(const block_t &_hqc, const quorum_cert_bt &qc);

        bool is_ancestor(const block_t &maybe_ancestor,
                         const block_t &descendant) const;

        bool has_valid_qc_ancestry(const block_t &certifier,
                                   const block_t &certified) const;

        void on_qc_finish(const block_t &blk);

        /* === auxilliary variables === */
        privkey_bt priv_key;
        /** block containing the QC for the highest block having one */
        std::pair<block_t, quorum_cert_bt> hqc;
        /** Add an additional block commit.*/
        bool rdy = false;

        void update(const block_t &nblk);

        uint32_t vheight;

        void on_propose_(const Proposal &prop);

    public:
        BoxObj<EntityStorage> storage;
        // uint16_t numberOfChildren;

        HotStuffCore(ReplicaID id, privkey_bt &&priv_key);
        virtual ~HotStuffCore()
        {
            b0->qc_ref = nullptr;
        }

        /* Inputs of the state machine triggered by external events, should called
         * by the class user, with proper invariants. */

        /** Call to initialize the protocol, should be called once before all other
         * functions. */
        void on_init(uint32_t nfaulty);

        /**
         * Tree calculation.
         * @param replicas necessary set of processes.
         * @param startup if being called during startup.
         */
        virtual void tree_scheduler(std::vector<std::tuple<NetAddr, pubkey_bt, uint256_t>> &&replicas, bool startup) {}
        virtual void close_client(ReplicaID rid) {}
        virtual void open_client(ReplicaID rid) {}
        virtual void tree_config() {}
        virtual ReconfigurationType isTreeSwitch(int bheight) {}
        virtual uint32_t get_blk_size() {}
        virtual size_t get_total_system_trees() {}
        virtual ReplicaID get_system_tree_root(int tid) {}
        virtual ReplicaID get_current_system_tree_root() {}
        virtual LeaderTimeoutRotationDisposition
        rotate_tree_on_leader_timeout(const LeaderViewId &) noexcept
        {
            return LeaderTimeoutRotationDisposition::legacy_fallback;
        }
        virtual ConfigurationId get_exact_tree_configuration(
            std::uint32_t, std::uint32_t) const
        {
            return {};
        }
        virtual ReplicaID get_exact_tree_root(
            std::uint32_t, std::uint32_t) const
        {
            return 0;
        }

        virtual void update_system_trees() {}

        /** Call to set the fanout. */
        void set_fanout(int32_t fanout);

        /** Call to set the piped latency */
        void set_piped_latency(int32_t piped_latency, int32_t async_blocks);

        /** Call to set when the tree is switched (every x blocks) */
        void set_tree_period(size_t nblocks);

        /** Call to set how the trees will be generated */
        void set_tree_generation(std::string genAlgo, std::string fpath);

        void set_new_epoch(std::string new_epoch);

        /**
         * A block is only delivered if itself is fetched, the block for the
         * contained qc is fetched and all parents are delivered. The user should
         * always ensure this invariant. The invalid blocks will be dropped by this
         * function.
         * @return true if valid */
        bool on_deliver_blk(const block_t &blk);

        /** Call upon the delivery of a proposal message.
         * The block mentioned in the message should be already delivered. */
        bool on_receive_proposal(const Proposal &prop);

        /** Call upon the delivery of a vote message.
         * The block mentioned in the message should be already delivered. */
        void on_receive_vote(const Vote &vote);

        /** Call to submit new commands to be decided (executed). "Parents" must
         * contain at least one block, and the first block is the actual parent,
         * while the others are uncles/aunts */
        block_t on_propose(const std::vector<uint256_t> &cmds,
                           const std::vector<block_t> &parents,
                           bytearray_t &&extra = bytearray_t());

        /* Functions required to construct concrete instances for abstract classes.
         * */

        /* Outputs of the state machine triggering external events.  The virtual
         * functions should be implemented by the user to specify the behavior upon
         * the events. */

        // Pipelined block hash.
        std::deque<uint256_t> piped_queue;

        // Pipelined block hash.
        std::deque<uint256_t> rdy_queue;

        // Last regular block height.
        int b_normal_height = 0;

        /* Block hex to us time spent on block*/
        std::map<uint256_t, long> stats;

        // If already a piped block was submitted.
        bool piped_submitted = false;

        // Last sent out block time.
        mutable struct timeval last_block_time;

        // Start time.
        mutable struct timeval start_time;

        uint64_t summed_latency;
        uint64_t processed_blocks;
        salticidae::ElapsedTime et;

        std::unordered_map<const uint256_t, salticidae::ElapsedTime> proposal_time;

    protected:
        /** Called by HotStuffCore upon the decision being made for cmd. */
        virtual void do_decide(Finality &&fin) = 0;
        virtual void do_consensus(const block_t &blk) = 0;
        /**
         * Commit a block with the exact quorum certificate that directly
         * certifies it when the commit rule has proved that ancestry.  The
         * compatibility overload keeps non-adaptive applications unchanged;
         * evidence-aware implementations may override this seam without
         * changing consensus authority.
         */
        virtual void do_consensus(
            const block_t &blk,
            const quorum_cert_bt &verified_direct_certifier)
        {
            static_cast<void>(verified_direct_certifier);
            do_consensus(blk);
        }
        /** Called once per committed block after all application decisions.
         * The index is zero-based within the current commit queue, ordered
         * from the oldest committed block to the newest. */
        virtual void do_post_block_commit(
            const block_t &,
            std::uint64_t commit_batch_index) {}
        /** Called by HotStuffCore upon broadcasting a new proposal.
         * The user should send the proposal message to all replicas except for
         * itself. */
        virtual void do_broadcast_proposal(const Proposal &prop) = 0;
        /** Called upon sending out a new vote to the next proposer.  The user
         * should send the vote message to a *good* proposer to have good liveness,
         * while safety is always guaranteed by HotStuffCore. */
        virtual void do_vote(Proposal last_proposer, const Vote &vote) = 0;

        /** Open exact leader-local state before any protocol mutation. */
        virtual bool admit_local(const Proposal &) { return true; }

        /** Record an already-created local vote in derived runtime state. */
        virtual void apply_local_vote(const Vote &) {}

        /** Report local proposal processing with an exact proposal key. */
        virtual void on_local_proposal_processed(
            const ProposalKey &) {}
        virtual void on_verified_commit_progress(
            const ProposalKey &) {}

        /**
         * Increment timer to mark receival.
         */
        virtual void inc_time(ReconfigurationType reconfig_type) {};

        virtual bool is_proposer(int id) {};

        virtual void proposer_base_deliver(const block_t &blk) {};

        /**
         * Get the id of the current tree
         */
        virtual uint32_t get_tree_id() {};

        /**
         * Get the current epoch number
         */
        virtual uint32_t get_cur_epoch_nr() {};

        /** Return the exact staged digest for a locally-created proposal. */
        virtual uint256_t get_epoch_digest(uint32_t) { return {}; };

        // Compatibility seam for legacy test cores. Runtime aggregation
        // deadlines are owned by ProposalContextLifecycle.
        virtual void start_proposal_timer(
            size_t, size_t, uint256_t, double, size_t)
        {}

        /* The user plugs in the detailed instances for those
         * polymorphic data types. */
    public:
        /** Create a partial certificate for one exact proposal identity. */
        virtual part_cert_bt create_part_cert(
            const PrivKey &priv_key, const ProposalKey &key) = 0;
        /** Create a partial certificate from its seralized form. */
        virtual part_cert_bt parse_part_cert(DataStream &s) = 0;
        /** Create a quorum certificate for one exact proposal identity. */
        virtual quorum_cert_bt create_quorum_cert(
            const ProposalKey &key) = 0;
        /** Create a quorum certificate from its serialized form. */
        virtual quorum_cert_bt parse_quorum_cert(DataStream &s) = 0;
        /** Create a command object from its serialized form. */
        // virtual command_t parse_cmd(DataStream &s) = 0;

    public:
        /** Add a replica to the current configuration. This should only be called
         * before running HotStuffCore protocol. */
        void add_replica(ReplicaID rid, const PeerId &peer_id, pubkey_bt &&pub_key);
        /** Try to prune blocks lower than last committed height - staleness. */
        void prune(uint32_t staleness);

        /* PaceMaker can use these functions to monitor the core protocol state
         * transition */
        /** Get a promise resolved when the block gets a QC. */
        promise_t async_qc_finish(const block_t &blk);
        /** Get a promise resolved when a new block is proposed. */
        promise_t async_wait_proposal();
        /** Get a promise resolved when a new proposal is received. */
        promise_t async_wait_receive_proposal();
        /** Get a promise resolved when hqc is updated. */
        promise_t async_hqc_update();

        /* Other useful functions */
        const block_t &get_genesis() const { return b0; }
        const block_t &get_hqc() { return hqc.first; }
        const ReplicaConfig &get_config() const { return config; }
        ReplicaID get_id() const { return id; }
        const std::set<block_t> get_tails() const { return tails; }
        operator std::string() const;
        void set_vote_disabled(bool f) { vote_disabled = f; }

        Proposal process_block(const block_t &bnew,
                               bool adjustHeight,
                               const ConfigurationId &configuration);

        void tree_config(bool b);
        void tree_scheduler(bool b);
        void close_client(bool b);
        void open_client(bool b);

        bool first = true;
    };

    /** Abstraction for proposal messages. */
    struct Proposal : public Serializable
    {
        ReplicaID proposer{0};
        /* epoch that the given proposal refers to */
        uint32_t epoch_nr{0};
        /** tree used for the message*/
        uint32_t tid{0};
        /** digest of the exact epoch definition used by this proposal */
        uint256_t epoch_digest{};
        /** block being proposed */
        block_t blk;
        /** handle of the core object to allow polymorphism. The user should use
         * a pointer to the object of the class derived from HotStuffCore */
        HotStuffCore *hsc;

        Proposal() : blk(nullptr), hsc(nullptr) {}
        Proposal(ReplicaID proposer,
                 uint32_t epoch_nr,
                 uint32_t tid,
                 const uint256_t &epoch_digest,
                 const block_t &blk,
                 HotStuffCore *hsc) : proposer(proposer),
                                      epoch_nr(epoch_nr),
                                      tid(tid),
                                      epoch_digest(epoch_digest),
                                      blk(blk),
                                      hsc(hsc) {}

        ConfigurationId configuration() const
        {
            return ConfigurationId{epoch_nr, tid, epoch_digest};
        }

        ProposalMetadata metadata() const
        {
            return ProposalMetadata{
                configuration(), blk ? blk->get_hash() : uint256_t{}, proposer};
        }

        ProposalKey key() const
        {
            return metadata().key();
        }

        void serialize(DataStream &s) const override
        {
            metadata().serialize(s);
            s << *blk;
        }

        void unserialize(DataStream &s) override
        {
            assert(hsc != nullptr);
            ProposalMetadata parsed_metadata;
            parsed_metadata.unserialize(s);
            proposer = parsed_metadata.proposer;
            epoch_nr = parsed_metadata.configuration.epoch_number;
            tid = parsed_metadata.configuration.tree_id;
            epoch_digest = parsed_metadata.configuration.epoch_digest;
            Block _blk;
            _blk.unserialize(s, hsc);
            if (_blk.get_hash() != parsed_metadata.block_hash)
                throw HotStuffInvalidEntity(
                    "proposal block hash does not match wire metadata");
            blk = hsc->storage->add_blk(std::move(_blk), hsc->get_config());
        }

        operator std::string() const
        {
            DataStream s;
            s << "<proposal "
              << "rid=" << std::to_string(proposer) << " "
              << "blk=" << get_hex10(blk->get_hash()) << " "
              << "tid=" << std::to_string(tid) << " "
              << "epoch_nr=" << std::to_string(epoch_nr) << " "
              << "epoch_digest=" << get_hex10(epoch_digest) << ">";
            return s;
        }
    };

    /** Abstraction for vote messages. */
    struct Vote : public Serializable
    {
        ReplicaID voter;
        /** epoch used for the message*/
        uint32_t epoch_nr;
        /** tree used for the message*/
        uint32_t tid;
        /** exact epoch-definition digest used for the message */
        uint256_t epoch_digest;
        /** block being voted */
        uint256_t blk_hash;
        /** proof of validity for the vote */
        part_cert_bt cert;

        /** handle of the core object to allow polymorphism */
        HotStuffCore *hsc;

        Vote() : cert(nullptr), hsc(nullptr) {}
        Vote(ReplicaID voter,
             const ProposalKey &key,
             part_cert_bt &&cert,
             HotStuffCore *hsc) : voter(voter),
                                  epoch_nr(key.configuration.epoch_number),
                                  tid(key.configuration.tree_id),
                                  epoch_digest(key.configuration.epoch_digest),
                                  blk_hash(key.block_hash),
                                  cert(std::move(cert)), hsc(hsc) {}

        Vote(const Vote &other) : voter(other.voter),
                                  epoch_nr(other.epoch_nr),
                                  tid(other.tid),
                                  epoch_digest(other.epoch_digest),
                                  blk_hash(other.blk_hash),
                                  cert(other.cert ? other.cert->clone() : nullptr),
                                  hsc(other.hsc) {}

        Vote(Vote &&other) = default;
        Vote &operator=(Vote &&other) = default;

        ConfigurationId configuration() const
        {
            return ConfigurationId{epoch_nr, tid, epoch_digest};
        }

        ProposalKey key() const
        {
            return ProposalKey{configuration(), blk_hash};
        }

        void serialize(DataStream &s) const override
        {
            s << voter << epoch_nr << tid << epoch_digest
              << blk_hash << *cert;
        }

        void unserialize(DataStream &s) override
        {
            assert(hsc != nullptr);
            s >> voter >> epoch_nr >> tid >> epoch_digest >> blk_hash;
            cert = hsc->parse_part_cert(s);
        }

        bool verify() const
        {
            assert(hsc != nullptr);
            return cert->verify(hsc->get_config().get_pubkey(voter)) &&
                   cert->get_proposal_key() == key();
        }

        promise_t verify(VeriPool &vpool) const
        {
            assert(hsc != nullptr);
            return cert->verify(hsc->get_config().get_pubkey(voter), vpool).then([this](bool result)
                                                                                 { return result && cert->get_proposal_key() == key(); });
        }

        operator std::string() const
        {
            DataStream s;
            s << "<vote "
              << "rid=" << std::to_string(voter) << " "
              << "blk=" << get_hex10(blk_hash) << " "
              << "tid=" << std::to_string(tid) << " "
              << "epoch_nr=" << std::to_string(epoch_nr) << ">";
            return s;
        }
    };

    struct Finality : public Serializable
    {
        ReplicaID rid;
        /** epoch used for the message*/
        uint32_t epoch_nr;
        /** tree used for the message*/
        uint32_t tid;
        int8_t decision;
        uint32_t cmd_idx;
        uint32_t cmd_height;
        uint256_t cmd_hash;
        uint256_t blk_hash;

    public:
        Finality() = default;
        Finality(ReplicaID rid,
                 uint32_t epoch_nr,
                 uint32_t tid,
                 int8_t decision,
                 uint32_t cmd_idx,
                 uint32_t cmd_height,
                 uint256_t cmd_hash,
                 uint256_t blk_hash) : rid(rid), epoch_nr(epoch_nr), tid(tid), decision(decision),
                                       cmd_idx(cmd_idx), cmd_height(cmd_height),
                                       cmd_hash(cmd_hash), blk_hash(blk_hash) {}

        void serialize(DataStream &s) const override
        {
            s << rid << epoch_nr << tid << decision
              << cmd_idx << cmd_height
              << cmd_hash;
            if (decision == 1)
                s << blk_hash;
        }

        void unserialize(DataStream &s) override
        {
            s >> rid >> epoch_nr >> tid >> decision >> cmd_idx >> cmd_height >> cmd_hash;
            if (decision == 1)
                s >> blk_hash;
        }

        operator std::string() const
        {
            DataStream s;
            s << "<fin "
              << "decision=" << std::to_string(decision) << " "
              << "cmd_idx=" << std::to_string(cmd_idx) << " "
              << "cmd_height=" << std::to_string(cmd_height) << " "
              << "cmd=" << get_hex10(cmd_hash) << " "
              << "blk=" << get_hex10(blk_hash) << " "
              << "tid=" << std::to_string(tid) << " "
              << "epoch_nr=" << std::to_string(epoch_nr) << ">";
            return s;
        }
    };

    /** Abstraction for vote relay messages. */
    struct VoteRelay : public Serializable
    {
        /** epoch used for the message*/
        uint32_t epoch_nr;
        /** tree used for the message*/
        uint32_t tid;
        /** exact epoch-definition digest used for the message */
        uint256_t epoch_digest;
        /** block being voted */
        uint256_t blk_hash;
        /** proof of validity for the vote */
        quorum_cert_bt cert;

        /** handle of the core object to allow polymorphism */
        HotStuffCore *hsc;

        VoteRelay() : cert(nullptr), hsc(nullptr) {}
        VoteRelay(const ProposalKey &key,
                  quorum_cert_bt &&cert,
                  HotStuffCore *hsc)
            : epoch_nr(key.configuration.epoch_number),
              tid(key.configuration.tree_id),
              epoch_digest(key.configuration.epoch_digest),
              blk_hash(key.block_hash),
                                       cert(std::move(cert)), hsc(hsc) {}

        VoteRelay(const VoteRelay &other) : epoch_nr(other.epoch_nr),
                                            tid(other.tid),
                                            epoch_digest(other.epoch_digest),
                                            blk_hash(other.blk_hash),
                                            cert(other.cert ? other.cert->clone() : nullptr),
                                            hsc(other.hsc) {}

        VoteRelay(VoteRelay &&other) = default;
        VoteRelay &operator=(VoteRelay &&other) = default;

        ConfigurationId configuration() const
        {
            return ConfigurationId{epoch_nr, tid, epoch_digest};
        }

        ProposalKey key() const
        {
            return ProposalKey{configuration(), blk_hash};
        }

        void serialize(DataStream &s) const override
        {
            s << epoch_nr << tid << epoch_digest << blk_hash << *cert;
        }

        void unserialize(DataStream &s) override
        {
            assert(hsc != nullptr);
            s >> epoch_nr >> tid >> epoch_digest >> blk_hash;
            cert = hsc->parse_quorum_cert(s);
        }

        operator std::string() const
        {
            DataStream s;
            s << "<voterelay "
              << "blk=" << get_hex10(blk_hash) << " "
              << "tid=" << std::to_string(tid) << " "
              << "epoch_nr=" << std::to_string(epoch_nr) << ">";
            return s;
        }
    };

    /** Cheap socket-free envelope checks used before worker scheduling. */
    bool validate_authenticated_vote(const ReplicaConfig &config,
                                     const PeerId &authenticated_peer,
                                     const Vote &vote) noexcept;

    bool validate_relay_envelope(const ReplicaConfig &config,
                                 const VoteRelay &relay) noexcept;

    /**
     * Synchronous compatibility helpers. Network handlers deliberately use
     * the validate_* functions above and schedule cryptography in VeriPool.
     */
    bool verify_authenticated_vote(const ReplicaConfig &config,
                                   const PeerId &authenticated_peer,
                                   const Vote &vote) noexcept;

    bool verify_relay_certificate(const ReplicaConfig &config,
                                  const VoteRelay &relay) noexcept;

}

#endif
