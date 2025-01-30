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

#ifndef _HOTSTUFF_CORE_H
#define _HOTSTUFF_CORE_H

#include <queue>
#include <unordered_map>
#include <unordered_set>
#include <future>

#include "salticidae/util.h"
#include "salticidae/network.h"
#include "salticidae/msg.h"
#include "hotstuff/util.h"
#include "hotstuff/consensus.h"

namespace hotstuff
{

    using salticidae::_1;
    using salticidae::_2;
    using salticidae::ElapsedTime;
    using salticidae::PeerNetwork;

    const double ent_waiting_timeout = 10;
    const double double_inf = 1e10;

    /**
     * Kauri tree
     * Abstraction of a tree to be used in Kauri
     * Assumes a balanced tree of constant fanout
     */
    struct Tree : public Serializable
    {

        /** Identifier for the tree */
        uint32_t tid;
        /** Fanout of the tree */
        uint8_t fanout;
        /** Pipeline-stretch to use with tree*/
        uint8_t pipe_stretch;
        /** List containing node arrangement*/
        std::vector<uint32_t> tree_array;

    public:
        Tree() = default;
        Tree(const uint32_t tid,
             const uint8_t fanout,
             const uint8_t pipe_stretch,
             const std::vector<uint32_t> &tree_array) : tid(tid),
                                                        fanout(fanout),
                                                        pipe_stretch(pipe_stretch),
                                                        tree_array(tree_array) {}

        /**
         * Returns the tree identifier
         */
        const uint32_t &get_tid() const { return tid; }

        /**
         * Returns the tree fanout
         */
        const uint8_t &get_fanout() const { return fanout; }

        /**
         * Returns the tree pipeline-stretch
         */
        const uint8_t &get_pipeline_stretch() const { return pipe_stretch; }

        /**
         * Returns the tree array list
         */
        const std::vector<uint32_t> &get_tree_array() const
        {
            return tree_array;
        }

        /**
         * Returns the size of the tree list
         */
        const size_t &get_tree_size() const
        {
            return tree_array.size();
        }

        /**
         * Returns the size of the tree list
         */
        const uint32_t &get_tree_root() const
        {
            return tree_array[0];
        }

        void serialize(DataStream &s) const override
        {
            s << tid << fanout << pipe_stretch;

            // Serialize the vector
            s << htole((uint32_t)tree_array.size());
            for (const auto &elem : tree_array)
                s << elem;
        }

        void unserialize(DataStream &s) override
        {
            s >> tid >> fanout >> pipe_stretch;

            // Unserialize the vector
            uint32_t n;
            s >> n;
            n = letoh(n);
            tree_array.resize(n);
            for (auto &elem : tree_array)
                s >> elem;
        }

        std::string get_tree_array_string()
        {
            DataStream s;
            s << "{ ";
            for (auto &elem : tree_array)
                s << std::to_string(elem) << " ";
            s << "}";
            return std::string(s);
        }

        operator std::string() const
        {
            DataStream s;
            s << "<tree "
              << "tid=" << std::to_string(tid) << " "
              << "tree_size=" << std::to_string(tree_array.size()) << " "
              << "fanout=" << std::to_string(fanout) << " "
              << "pipe_stretch=" << std::to_string(pipe_stretch) << " "
              << "root_node=" << std::to_string(tree_array[0]) << ">";
            return s;
        }
    };

    /** Struct that keeps the node's relative network information of a tree */
    struct TreeNetwork
    {

        /** While a Tree is the same for every replica,
            A TreeNetwork is relative, depending on
            the replica's position in a tree **/

        Tree tree;
        size_t myTreeId;                     // My identifier in the tree array
        mutable PeerId parentPeer;           // My parent peer in the tree
        mutable std::set<PeerId> childPeers; // My children peers in the tree
        uint16_t numberOfChildren;           // How many children I have
        DataStream info;                     // Debug info
        size_t switchTarget;                 // The block height at which to switch this tree

    private:
        std::vector<uint32_t> tree_array;                    // Member variable to store the tree array
        std::unordered_map<ReplicaID, size_t> id_to_pos_map; // Map for ReplicaID to position
    public:
        TreeNetwork() = default;

        TreeNetwork(const Tree &t,
                    const std::vector<std::tuple<NetAddr, pubkey_bt, uint256_t>> &replicas,
                    const uint16_t myReplicaId) : tree(t)
        {
            initializeTreeNetwork(replicas, myReplicaId);
        }

        TreeNetwork(const Tree t,
                    const std::vector<std::tuple<NetAddr, pubkey_bt, uint256_t>> &&replicas,
                    const uint16_t myReplicaId) : tree(t)
        {
            initializeTreeNetwork(replicas, myReplicaId);
        }

        /**
         * Returns the network's Tree
         */
        const Tree &get_tree() const { return tree; }

        const size_t &get_myTreeId() const { return myTreeId; }

        const PeerId &get_parentPeer() const { return parentPeer; }

        const std::set<PeerId> &get_childPeers() const { return childPeers; }

        const uint16_t &get_numberOfChildren() const { return numberOfChildren; }

        const size_t &get_target() const { return switchTarget; }

        const void set_target(const size_t target) { switchTarget = target; };

        size_t get_level(ReplicaID rid) const
        {
            auto it = id_to_pos_map.find(rid);
            if (it == id_to_pos_map.end())
            {
                throw std::invalid_argument("Replica ID not found in the tree network.");
            }
            size_t index = it->second;
            size_t level = 0;
            size_t nodes_in_level = 1;
            size_t nodes_up_to_prev_level = 0;
            size_t fanout = tree.get_fanout();

            while (true)
            {
                if (index < nodes_up_to_prev_level + nodes_in_level)
                    return level;
                nodes_up_to_prev_level += nodes_in_level;
                level++;
                // Prevent overflow or excessive levels
                // If fanout is 0, it's a single-node tree
                if (fanout == 0)
                    break;
                nodes_in_level *= fanout;
                // If nodes_in_level exceeds tree size, cap it
                if (nodes_in_level > tree_array.size())
                    nodes_in_level = tree_array.size() - nodes_up_to_prev_level;
                // If we've covered all nodes, exit
                if (nodes_up_to_prev_level >= tree_array.size())
                    break;
            }

            throw std::logic_error("Failed to determine level.");
        }

        size_t get_max_level() const
        {
            // Get the tree array (assuming it represents the nodes in breadth-first order)
            auto tree_array = tree.get_tree_array();

            // The fanout (number of children per node)
            auto fanout = tree.get_fanout();

            // Calculate the maximum level of the tree
            size_t total_nodes = tree_array.size();
            size_t max_level = 0;

            while (total_nodes > 0)
            {
                total_nodes = (total_nodes - 1) / fanout; // Move up one level
                max_level++;
            }

            return max_level;
        }

        bool is_leaf() const
        {
            return childPeers.empty();
        }

        operator std::string()
        {

            DataStream s;

            s << "\nTree Network {\n";
            s << std::string(info).c_str();
            s << "\tTree Array:" << tree.get_tree_array_string().c_str() << "\n";
            s << "}";

            return s;
        }

    private:
        /**
         * Recursively counts the number of children nodes below a given index, returning the sub-tree total of child nodes
         */
        int countChildren(int index, int treeSize)
        {
            int childrenCount = 0;
            auto fanout = tree.get_fanout();

            for (auto i = 1; i <= fanout; i++)
            {

                auto child_idx = fanout * index + i;

                // If within bounds of array, child exists
                if (child_idx < treeSize)
                {

                    childrenCount++; // Increment count for the child

                    // Recursively count the number of children nodes below the child
                    childrenCount += countChildren(child_idx, treeSize);
                }
            }

            return childrenCount;
        }

        void initializeTreeNetwork(const std::vector<std::tuple<NetAddr, pubkey_bt, uint256_t>> &replicas, const uint16_t myReplicaId)
        {
            info << "\tTree Data: " << std::string(tree) << "\n";

            tree_array = tree.get_tree_array();

            for (size_t i = 0; i < tree_array.size(); ++i)
            {
                id_to_pos_map[tree_array[i]] = i;
            }

            auto fanout = tree.get_fanout();
            auto size = tree_array.size();

            // Find my position in the tree
            auto it = id_to_pos_map.find(myReplicaId);
            if (it == id_to_pos_map.end())
            {
                throw std::invalid_argument("My ReplicaID not found in the tree array.");
            }

            myTreeId = it->second;
            if (myTreeId != 0)
            {
                auto parent_idx = std::floor((myTreeId - 1) / fanout);
                auto parent_cert_hash = std::get<2>(replicas[tree_array[parent_idx]]);
                salticidae::PeerId parent_peer{parent_cert_hash};
                parentPeer = parent_peer;
                info << "\tMy parent: " << std::to_string(tree_array[parent_idx]) << "\n";
            }
            else
                info << "\tI have no parent (am root)\n";

            std::string tmp = "\tMy children are: ";
            // Add every possible child, considering fanout
            for (auto i = 1; i <= fanout; i++)
            {
                auto child_idx = fanout * myTreeId + i;

                // If within bounds of array, child exists
                if (child_idx < size)
                {
                    auto child_cert_hash = std::get<2>(replicas[tree_array[child_idx]]);
                    salticidae::PeerId child_peer{child_cert_hash};
                    childPeers.insert(child_peer);
                    tmp.append(std::to_string(tree_array[child_idx])).append(", ");
                }
            }

            if (childPeers.empty())
            {
                info << "\tI have no children\n";
            }
            else
            {
                tmp = tmp.substr(0, tmp.size() - 2); // Remove trailing ", "
                info << tmp << "\n";
            }

            // Store remainder state
            numberOfChildren = countChildren(myTreeId, size);
            info << "\tTotal children in my subtree: " << std::to_string(numberOfChildren) << "\n";

            info << "\tMy ReplicaID: " << std::to_string(myReplicaId) << "\n";
            info << "\tMy ID in the tree: " << std::to_string(myTreeId) << "\n";
        }
    };

    struct Epoch : public Serializable
    {

        uint32_t epoch_num; // Epoch number

        std::vector<Tree> trees;                // Collection of trees
        std::vector<TreeNetwork> tree_networks; // Collection of trees networks

        mutable std::unordered_map<size_t, TreeNetwork> system_trees;

        DataStream info;

    public:
        Epoch() = default;
        Epoch(uint32_t epoch_num) : epoch_num(epoch_num)
        {
        }

        Epoch(uint32_t epoch_num, const std::vector<Tree> &trees) : epoch_num(epoch_num),
                                                                    trees(trees)
        {
        }
        Epoch(uint32_t epoch_num, const std::vector<TreeNetwork> &tree_networks) : epoch_num(epoch_num),
                                                                                   tree_networks(tree_networks)
        {
        }

        const uint32_t &get_epoch_num() const { return epoch_num; }

        const std::vector<Tree> &get_trees() const { return trees; }

        const std::vector<TreeNetwork> &get_tree_networks() const { return tree_networks; }

        const std::unordered_map<size_t, TreeNetwork> &get_system_trees()
        {
            for (size_t i = 0; i < tree_networks.size(); ++i)
            {
                system_trees[i] = tree_networks[i];
            }

            return system_trees;
        }

        void create_tree_networks(const std::vector<std::tuple<NetAddr, pubkey_bt, uint256_t>> &replicas, const uint16_t replica_id)
        {

            for (const auto &tree : trees)
            {
                TreeNetwork network(tree, replicas, replica_id);

                tree_networks.push_back(network);
            }
        }

        /**
         * Serializes the Epoch instance to a DataStream
         */
        void serialize(DataStream &s) const override
        {
            s << epoch_num;

            // Serialize the vector of trees
            s << htole((uint32_t)trees.size());
            for (const auto &tree : trees)
                s << tree; // Assuming Tree has a serialize method or operator<<
        }

        /**
         * Deserializes an Epoch instance from a DataStream
         */
        void unserialize(DataStream &s) override
        {
            s >> epoch_num;

            // Deserialize the vector of trees
            uint32_t num_trees;
            s >> num_trees;
            num_trees = letoh(num_trees);

            trees.resize(num_trees);
            for (auto &tree : trees)
                s >> tree; // Assuming Tree has an unserialize method or operator>>
        }

        operator std::string()
        {

            DataStream s;

            s << "\nEPOCH  {\n";
            s << "\t Epoch number: " << std::to_string(epoch_num) << "\n";
            s << "\t Trees:\n";

            // Include details from the 'trees' vector
            for (const auto &tree : trees)
            {
                s << "\t\t" << std::string(tree).c_str() << "\n";
            }

            s << "}";

            return s;
        }
    };

    //-----Report Stuff---

    struct BlockPeerKey
    {
        uint256_t blk_hash;
        PeerId peer;

        // Constructor
        BlockPeerKey(const uint256_t &blk_hash, const PeerId &peer)
            : blk_hash(blk_hash), peer(peer) {}

        // Equality operator for map lookups
        bool operator==(const BlockPeerKey &other) const
        {
            return blk_hash == other.blk_hash && peer == other.peer;
        }

        // Hash function for the map
        struct Hash
        {
            std::size_t operator()(const BlockPeerKey &key) const
            {
                // Combine the hashes of blk_hash and peer
                return std::hash<salticidae::uint256_t>()(key.blk_hash) ^
                       (std::hash<salticidae::PeerId>()(key.peer) << 1);
            }
        };
    };

    struct LatMeasure
    {
        ReplicaID child;
        uint32_t epoch_nr; // epoch at the time of measurement
        uint32_t tid;      // tree ID at the time of measurement
        uint32_t latency_us;

        LatMeasure() = default;

        LatMeasure(ReplicaID child, uint32_t epoch_nr, uint32_t tid, uint32_t latency_us)
            : child(child), epoch_nr(epoch_nr), tid(tid), latency_us(latency_us) {}
    };

    struct TimeoutMeasure
    {
        ReplicaID non_responsive_replica;
        uint32_t epoch_nr;
        uint32_t tid;
        // Possibly store how long we waited, or a timestamp, or # of attempts, etc.

        TimeoutMeasure() = default;

        TimeoutMeasure(ReplicaID non_responsive_replica, uint32_t epoch_nr, uint32_t tid)
            : non_responsive_replica(non_responsive_replica), epoch_nr(epoch_nr), tid(tid) {}
    };

    struct LatencyReport : public Serializable
    {
        ReplicaID reporter;
        std::vector<LatMeasure> lats;

        LatencyReport() = default;

        // Convenient constructor
        LatencyReport(ReplicaID reporter,
                      const std::vector<LatMeasure> &lats)
            : reporter(reporter),
              lats(lats)
        {
        }

        // Serialize the data into a DataStream
        void serialize(DataStream &s) const override
        {
            s << reporter;

            uint32_t count = static_cast<uint32_t>(lats.size());
            s << count;
            for (auto &item : lats)
            {
                s << item.child;
                s << item.epoch_nr;
                s << item.tid;
                s << item.latency_us;
            }
        }

        // Unserialize from a DataStream
        void unserialize(DataStream &s) override
        {
            s >> reporter;

            uint32_t count;
            s >> count;
            lats.resize(count);
            for (uint32_t i = 0; i < count; i++)
            {
                s >> lats[i].child;
                s >> lats[i].epoch_nr;
                s >> lats[i].tid;
                s >> lats[i].latency_us;
            }
        }
    };

    struct TimeoutReport : public Serializable
    {
        ReplicaID reporter;
        std::vector<TimeoutMeasure> timeouts;

        TimeoutReport() = default;

        // Convenient constructor
        TimeoutReport(ReplicaID reporter,
                      const std::vector<TimeoutMeasure> &timeouts)
            : reporter(reporter),
              timeouts(timeouts)
        {
        }

        void serialize(DataStream &s) const override
        {
            s << reporter;

            uint32_t count = static_cast<uint32_t>(timeouts.size());
            s << count;

            for (auto &tm : timeouts)
            {
                s << tm.non_responsive_replica;
                s << tm.epoch_nr;
                s << tm.tid;
            }
        }

        // Unserialize from a DataStream
        void unserialize(DataStream &s) override
        {
            s >> reporter;

            uint32_t count;
            s >> count;
            timeouts.resize(count);

            for (uint32_t i = 0; i < count; i++)
            {
                s >> timeouts[i].non_responsive_replica;
                s >> timeouts[i].epoch_nr;
                s >> timeouts[i].tid;
            }
        }
    };

    //-----------------

    /** Network message format for HotStuff. */
    struct MsgPropose
    {
        static const opcode_t opcode = 0x0;
        DataStream serialized;
        Proposal proposal;
        MsgPropose(const Proposal &);
        /** Only move the data to serialized, do not parse immediately. */
        MsgPropose(DataStream &&s) : serialized(std::move(s)) {}
        MsgPropose(DataStream stream, bool wut) : serialized(std::move(stream)) {}

        /** Parse the serialized data to blks now, with `hsc->storage`. */
        void postponed_parse(HotStuffCore *hsc);
    };

    struct MsgVote
    {
        static const opcode_t opcode = 0x1;
        DataStream serialized;
        Vote vote;
        MsgVote(const Vote &);
        MsgVote(DataStream &&s) : serialized(std::move(s)) {}
        void postponed_parse(HotStuffCore *hsc);
    };

    struct MsgReqBlock
    {
        static const opcode_t opcode = 0x2;
        DataStream serialized;
        std::vector<uint256_t> blk_hashes;
        MsgReqBlock() = default;
        MsgReqBlock(const std::vector<uint256_t> &blk_hashes);
        MsgReqBlock(DataStream &&s);
    };

    struct MsgRespBlock
    {
        static const opcode_t opcode = 0x3;
        DataStream serialized;
        std::vector<block_t> blks;
        MsgRespBlock(const std::vector<block_t> &blks);
        MsgRespBlock(DataStream &&s) : serialized(std::move(s)) {}
        void postponed_parse(HotStuffCore *hsc);
    };

    struct MsgRelay
    {
        static const opcode_t opcode = 0x4;
        DataStream serialized;
        VoteRelay vote;
        MsgRelay(const VoteRelay &);
        MsgRelay(DataStream &&s) : serialized(std::move(s)) {}
        void postponed_parse(HotStuffCore *hsc);
    };

    using promise::promise_t;

    class HotStuffBase;
    using pacemaker_bt = BoxObj<class PaceMaker>;

    template <EntityType ent_type>
    class FetchContext : public promise_t
    {
        TimerEvent timeout;
        HotStuffBase *hs;
        MsgReqBlock fetch_msg;
        const uint256_t ent_hash;
        std::unordered_set<PeerId> replicas;
        inline void timeout_cb(TimerEvent &);

    public:
        FetchContext(const FetchContext &) = delete;
        FetchContext &operator=(const FetchContext &) = delete;
        FetchContext(FetchContext &&other);

        FetchContext(const uint256_t &ent_hash, HotStuffBase *hs);
        ~FetchContext() {}

        inline void send(const PeerId &replica);
        inline void reset_timeout();
        inline void add_replica(const PeerId &replica, bool fetch_now = true);
    };

    class BlockDeliveryContext : public promise_t
    {
    public:
        ElapsedTime elapsed;
        BlockDeliveryContext &operator=(const BlockDeliveryContext &) = delete;
        BlockDeliveryContext(const BlockDeliveryContext &other) : promise_t(static_cast<const promise_t &>(other)),
                                                                  elapsed(other.elapsed) {}
        BlockDeliveryContext(BlockDeliveryContext &&other) : promise_t(static_cast<const promise_t &>(other)),
                                                             elapsed(std::move(other.elapsed)) {}
        template <typename Func>
        BlockDeliveryContext(Func callback) : promise_t(callback)
        {
            elapsed.start();
        }
    };

    /** HotStuff protocol (with network implementation). */
    class HotStuffBase : public HotStuffCore
    {
        using BlockFetchContext = FetchContext<ENT_TYPE_BLK>;
        using CmdFetchContext = FetchContext<ENT_TYPE_CMD>;

        friend BlockFetchContext;
        friend CmdFetchContext;

    public:
        using Net = PeerNetwork<opcode_t>;
        using commit_cb_t = std::function<void(const Finality &)>;

    protected:
        /** the binding address in replica network */
        NetAddr listen_addr;
        /** the block size */
        size_t blk_size;
        /** libevent handle */
        EventContext ec;
        salticidae::ThreadCall tcall;
        VeriPool vpool;
        std::vector<PeerId> peers;
        std::unordered_map<PeerId, size_t> peer_id_map; /* PeerId to ReplicaId map*/

        pid_t client_pid;
        char client_prog[256];

        std::string cip;

    private:
        /** whether libevent handle is owned by itself */
        bool ec_loop;
        /** network stack */
        Net pn;
        std::unordered_set<uint256_t> valid_tls_certs;
#ifdef HOTSTUFF_BLK_PROFILE
        BlockProfiler blk_profiler;
#endif
        pacemaker_bt pmaker;
        TimerEvent ev_beat_timer;
        TimerEvent ev_check_pending;
        TimerEvent ev_end_warmup;

        TimerEvent ev_report_timer;
        double report_period = 1.0;

        size_t warmup_counter = 0;

        /* queues for async tasks */

        std::unordered_map<const uint256_t, BlockFetchContext> blk_fetch_waiting;
        std::unordered_map<const uint256_t, BlockDeliveryContext> blk_delivery_waiting;
        std::unordered_map<const uint256_t, commit_cb_t> decision_waiting;
        std::unordered_map<const uint256_t, uint32_t> decision_made;
        using cmd_queue_t = salticidae::MPSCQueueEventDriven<std::pair<uint256_t, commit_cb_t>>;
        cmd_queue_t cmd_pending;
        std::vector<uint256_t> cmd_pending_buffer;
        uint64_t max_cmd_pending_size;
        std::vector<uint256_t> final_buffer;

        /* statistics */

        uint64_t fetched;
        uint64_t delivered;
        uint64_t failures;
        mutable uint64_t nsent;
        mutable uint64_t nrecv;

        mutable uint32_t part_parent_size;
        mutable uint32_t part_fetched;
        mutable uint32_t part_delivered;
        mutable uint32_t part_decided;
        mutable uint32_t part_gened;
        mutable double part_delivery_time;
        mutable double part_delivery_time_min;
        mutable double part_delivery_time_max;
        mutable std::unordered_map<const PeerId, uint32_t> part_fetched_replica;

        /* trees and peers */

        // mutable PeerId parentPeer;
        // mutable PeerId noParent;
        // mutable std::set<PeerId> childPeers;

        vector<std::tuple<NetAddr, pubkey_bt, uint256_t>> global_replicas;

        // std::unordered_map<size_t, Tree> system_trees;
        // TODO: deprecated
        std::unordered_map<size_t, TreeNetwork> system_trees;
        std::unordered_set<uint256_t> pass_trought_blks;
        std::unordered_map<uint256_t, std::set<ReplicaID>> pending_votes;

        // Reports stuff
        std::mutex metrics_lock;
        std::unordered_map<BlockPeerKey, struct timeval, BlockPeerKey::Hash> lat_start; // track proposal time
        std::vector<LatMeasure> peer_latencies;
        std::vector<TimeoutMeasure> child_timeouts;

        NetAddr reputation_addr;
        Net::MsgNet::conn_t reputation_server_conn;
        Net rn = Net(ec, Net::Config());

        std::vector<Epoch> epochs;
        mutable TreeNetwork current_tree_network;
        mutable Tree current_tree;
        uint32_t lastCheckedHeight;
        std::vector<std::pair<MsgPropose, Net::conn_t>> pending_proposals;

        /* Epoch */

        // TODO: also becoming deprecated as we just need to store the indexes
        mutable Epoch cur_epoch;
        mutable Epoch on_hold_epoch;

        size_t reconfig_count;
        bool warmup_finished;

        /* communication */

        void on_fetch_cmd(const command_t &cmd);
        void on_fetch_blk(const block_t &blk);
        bool on_deliver_blk(const block_t &blk);

        /** deliver consensus message: <propose> */
        inline void propose_handler(MsgPropose &&, const Net::conn_t &);
        /** deliver consensus message: <vote> */
        inline void vote_handler(MsgVote &&, const Net::conn_t &);
        /** deliver consensus relay message: <vote_relay> */
        inline void vote_relay_handler(MsgRelay &&, const Net::conn_t &);
        /** fetches full block data */
        inline void req_blk_handler(MsgReqBlock &&, const Net::conn_t &);
        /** receives a block */
        inline void resp_blk_handler(MsgRespBlock &&, const Net::conn_t &);

        inline bool conn_handler(const salticidae::ConnPool::conn_t &, bool);

        void do_broadcast_proposal(const Proposal &) override;
        void do_vote(Proposal, const Vote &) override;
        void inc_time(ReconfigurationType reconfig_type) override;
        bool is_proposer(int id) override;
        void proposer_base_deliver(const block_t &blk) override;
        void do_decide(Finality &&) override;
        void do_consensus(const block_t &blk) override;
        uint32_t get_tree_id() override;
        uint32_t get_cur_epoch_nr() override;

    protected:
        /** Called to replicate the execution of a command, the application should
         * implement this to make transition for the application state. */
        virtual void state_machine_execute(const Finality &) = 0;

    public:
        HotStuffBase(uint32_t blk_size,
                     ReplicaID rid,
                     privkey_bt &&priv_key,
                     NetAddr listen_addr,
                     pacemaker_bt pmaker,
                     EventContext ec,
                     size_t nworker,
                     const Net::Config &netconfi,
                     NetAddr reputation_addr);

        ~HotStuffBase();

        /* the API for HotStuffBase */

        /* Submit the command to be decided. */
        void exec_command(uint256_t cmd_hash, commit_cb_t callback);
        void start(std::vector<std::tuple<NetAddr, pubkey_bt, uint256_t>> &&replicas,
                   bool ec_loop = false);
        void tree_config(std::vector<std::tuple<NetAddr, pubkey_bt, uint256_t>> &&replicas);
        void read_epoch_from_file(std::vector<std::tuple<NetAddr, pubkey_bt, uint256_t>> &&replicas);
        void tree_scheduler(std::vector<std::tuple<NetAddr, pubkey_bt, uint256_t>> &&replicas, bool startup);
        // Updates vars related to epoch and trees
        void change_epoch();

        void stage_epoch(Epoch &epoch);

        // Timer-related members
        mutable std::mutex timers_mutex;                                                        ///< Mutex to protect access to proposal_timers
        std::unordered_map<uint256_t, std::shared_ptr<salticidae::TimerEvent>> proposal_timers; ///< Maps block hash to TimerEvent

        // Helper functions
        void start_proposal_timer(size_t tid, size_t epoch_nr, uint256_t blk_hash, double timeout_duration, size_t tree_level);
        void stop_proposal_timer(const uint256_t &blk_hash);
        void on_timer_expired(size_t tid, size_t epoch_nr, uint256_t blk_hash, uint32_t tree_level);
        std::set<ReplicaID> find_children_who_did_not_respond(const uint256_t &blk_hash);
        RcObj<VoteRelay> create_partial_vote_relay(size_t tid, size_t epoch_nr, const uint256_t &blk_hash);

        // Reports functions
        void record_latency(size_t epoch_nr, size_t tid, const PeerId &peer, const uint256_t &blk_hash);
        void on_report_timer();

        void update_system_trees();

        //------------------------------
        void close_client(ReplicaID rid);
        void open_client(ReplicaID rid);
        ReconfigurationType isTreeSwitch(int bheight);
        void beat();
        void print_pipe_queues(bool printPiped, bool printRdy);
        block_t repropose_beat(const std::vector<uint256_t> &cmds);

        void increment_reconfig_count() { reconfig_count++; };
        size_t size() const { return peers.size(); };
        uint32_t get_blk_size() { return blk_size; };
        const auto &get_decision_waiting() const { return decision_waiting; };
        ThreadCall &get_tcall() { return tcall; };
        PaceMaker *get_pace_maker() { return pmaker.get(); };
        size_t get_total_system_trees() { return system_trees.size(); };
        ReplicaID get_system_tree_root(int tid) { return system_trees[tid].get_tree().get_tree_root(); };
        ReplicaID get_current_system_tree_root() { return current_tree.get_tree_root(); };
        TreeNetwork get_current_tree_network() { return current_tree_network; };
        void print_stat() const;
        virtual void do_elected() {}
        // #ifdef HOTSTUFF_AUTOCLI
        //     virtual void do_demand_commands(size_t) {}
        // #endif

        /* Helper functions */
        /** Returns a promise resolved (with command_t cmd) when Command is fetched. */
        promise_t async_fetch_cmd(const uint256_t &cmd_hash, const PeerId *replica, bool fetch_now = true);
        /** Returns a promise resolved (with block_t blk) when Block is fetched. */
        promise_t async_fetch_blk(const uint256_t &blk_hash, const PeerId *replica, bool fetch_now = true);
        /** Returns a promise resolved (with block_t blk) when Block is delivered (i.e. prefix is fetched). */
        promise_t async_deliver_blk(const uint256_t &blk_hash, const PeerId &replica);
    };

    /** HotStuff protocol (templated by cryptographic implementation). */
    template <typename PrivKeyType = PrivKeyDummy,
              typename PubKeyType = PubKeyDummy,
              typename PartCertType = PartCertDummy,
              typename QuorumCertType = QuorumCertDummy>
    class HotStuff : public HotStuffBase
    {
        using HotStuffBase::HotStuffBase;

    protected:
        part_cert_bt create_part_cert(const PrivKey &priv_key, const uint256_t &blk_hash) override
        {
            HOTSTUFF_LOG_DEBUG("create part cert with priv=%s, blk_hash=%s",
                               get_hex10(priv_key).c_str(), get_hex10(blk_hash).c_str());
            return new PartCertType(
                static_cast<const PrivKeyType &>(priv_key),
                blk_hash);
        }

        part_cert_bt parse_part_cert(DataStream &s) override
        {
            PartCert *pc = new PartCertType();
            s >> *pc;
            return pc;
        }

        quorum_cert_bt create_quorum_cert(const uint256_t &blk_hash) override
        {
            return new QuorumCertType(get_config(), blk_hash);
        }

        quorum_cert_bt parse_quorum_cert(DataStream &s) override
        {
            QuorumCert *qc = new QuorumCertType();
            s >> *qc;
            return qc;
        }

    public:
        HotStuff(uint32_t blk_size,
                 ReplicaID rid,
                 const bytearray_t &raw_privkey,
                 NetAddr listen_addr,
                 pacemaker_bt pmaker,
                 EventContext ec = EventContext(),
                 size_t nworker = 4,
                 const Net::Config &netconfig = Net::Config(),
                 NetAddr reputation_addr = NetAddr()) : HotStuffBase(blk_size,
                                                                     rid,
                                                                     new PrivKeyType(raw_privkey),
                                                                     listen_addr,
                                                                     std::move(pmaker),
                                                                     ec,
                                                                     nworker,
                                                                     netconfig,
                                                                     reputation_addr) {}

        void start(const std::vector<std::tuple<NetAddr, bytearray_t, bytearray_t>> &replicas, bool ec_loop = false)
        {
            std::vector<std::tuple<NetAddr, pubkey_bt, uint256_t>> reps;
            for (auto &r : replicas)
                reps.push_back(
                    std::make_tuple(
                        std::get<0>(r),
                        new PubKeyType(std::get<1>(r)),
                        uint256_t(std::get<2>(r))));
            HotStuffBase::start(std::move(reps), ec_loop);
        }

        void set_fanout(int32_t fanout)
        {
            HotStuffBase::set_fanout(fanout);
        }

        void set_piped_latency(int32_t piped_latency, int32_t async_blocks)
        {
            HotStuffBase::set_piped_latency(piped_latency, async_blocks);
        }

        void set_tree_period(size_t nblocks)
        {
            HotStuffBase::set_tree_period(nblocks);
        }

        void set_tree_generation(std::string genAlgo, std::string fpath)
        {
            HotStuffBase::set_tree_generation(genAlgo, fpath);
        }

        void set_new_epoch(std::string new_epoch)
        {
            HotStuffBase::set_new_epoch(new_epoch);
        }

        void set_client_ip(std::string client_ip)
        {
            cip = client_ip;
        }
    };

    using HotStuffNoSig = HotStuff<>;
    using HotStuffSecp256k1 = HotStuff<PrivKeySecp256k1, PubKeySecp256k1,
                                       PartCertSecp256k1, QuorumCertSecp256k1>;
    using HotStuffAgg = HotStuff<PrivKeyBLS, PubKeyBLS,
                                 PartCertBLSAgg, QuorumCertAggBLS>;

    template <EntityType ent_type>
    FetchContext<ent_type>::FetchContext(FetchContext &&other) : promise_t(static_cast<const promise_t &>(other)),
                                                                 hs(other.hs),
                                                                 fetch_msg(std::move(other.fetch_msg)),
                                                                 ent_hash(other.ent_hash),
                                                                 replicas(std::move(other.replicas))
    {
        other.timeout.del();
        timeout = TimerEvent(hs->ec,
                             std::bind(&FetchContext::timeout_cb, this, _1));
        reset_timeout();
    }

    template <>
    inline void FetchContext<ENT_TYPE_CMD>::timeout_cb(TimerEvent &)
    {
        HOTSTUFF_LOG_WARN("cmd fetching %.10s timeout", get_hex(ent_hash).c_str());
        for (const auto &replica : replicas)
            send(replica);
        reset_timeout();
    }

    template <>
    inline void FetchContext<ENT_TYPE_BLK>::timeout_cb(TimerEvent &)
    {
        HOTSTUFF_LOG_WARN("block fetching %.10s timeout", get_hex(ent_hash).c_str());
        for (const auto &replica : replicas)
            send(replica);
        reset_timeout();
    }

    template <EntityType ent_type>
    FetchContext<ent_type>::FetchContext(
        const uint256_t &ent_hash, HotStuffBase *hs) : promise_t([](promise_t) {}),
                                                       hs(hs), ent_hash(ent_hash)
    {
        fetch_msg = std::vector<uint256_t>{ent_hash};

        timeout = TimerEvent(hs->ec,
                             std::bind(&FetchContext::timeout_cb, this, _1));
        reset_timeout();
    }

    template <EntityType ent_type>
    void FetchContext<ent_type>::send(const PeerId &replica)
    {
        hs->part_fetched_replica[replica]++;
        hs->pn.send_msg(fetch_msg, replica);
    }

    template <EntityType ent_type>
    void FetchContext<ent_type>::reset_timeout()
    {
        timeout.add(salticidae::gen_rand_timeout(ent_waiting_timeout));
    }

    template <EntityType ent_type>
    void FetchContext<ent_type>::add_replica(const PeerId &replica, bool fetch_now)
    {
        if (replicas.empty() && fetch_now)
            send(replica);
        replicas.insert(replica);
    }

}

#endif
