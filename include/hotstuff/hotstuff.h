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

#include <deque>
#include <queue>
#include <unordered_map>
#include <unordered_set>
#include <future>
#include <functional>
#include <map>

#include "salticidae/util.h"
#include "salticidae/network.h"
#include "salticidae/msg.h"
#include "hotstuff/util.h"
#include "hotstuff/adaptive_v2_reporting_outbox.h"
#include "hotstuff/adaptive_v2_response_evidence.h"
#include "hotstuff/aggregation.h"
#include "hotstuff/block_delivery_orchestration.h"
#include "hotstuff/consensus.h"
#include "hotstuff/exact_vote_handler.h"
#include "hotstuff/epoch_change_inbox.h"
#include "hotstuff/epoch_live_binding.h"
#include "hotstuff/epoch_runtime_wiring.h"
#include "hotstuff/experiment_byzantine_adapter.h"
#include "hotstuff/experiment_post_qc_audit.h"
#include "hotstuff/pending_exact_contribution_buffer.h"
#include "hotstuff/proposal_admission.h"
#include "hotstuff/structured_event.h"

namespace hotstuff
{

    using salticidae::_1;
    using salticidae::_2;
    using salticidae::ElapsedTime;
    using salticidae::PeerNetwork;

    const double ent_waiting_timeout = 10;
    const double double_inf = 1e10;

    namespace detail
    {
        /**
         * Consume the local pipelined prefix made redundant by a delivered
         * descendant. This is only the structural queue gate: callers must
         * first authenticate the candidate's exact active proposal identity
         * and verify its fixed-quorum certificate.
         *
         * A candidate at the queue head consumes only itself. A later
         * candidate consumes the prefix through itself only when every
         * skipped entry is a delivered first-parent ancestor. Candidates not
         * in the local pipeline are publishable without queue mutation.
         */
        bool consume_delivered_ancestor_piped_prefix(
            std::deque<uint256_t> &piped,
            std::deque<uint256_t> &ready,
            const block_t &candidate,
            EntityStorage &storage);

        /**
         * Snapshot exact, read-only evidence for a root QC publication that
         * the caller has already determined is blocked behind the pipeline
         * head. Missing or contradictory retained state yields no event.
         */
        std::optional<RootQcQueueBlockedStructuredEvent>
        make_root_qc_queue_blocked_event(
            const ProposalContextLease &candidate_lease,
            const ProposalContextLifecycle &proposal_contexts,
            const std::deque<uint256_t> &piped,
            const block_t &candidate,
            EntityStorage &storage,
            ReplicaID observer_replica);
    }

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

        size_t get_node_position(ReplicaID node) const
        {
            for (size_t i = 0; i < tree_array.size(); i++)
            {
                if (tree_array[i] == node)
                    return i;
            }
            return tree_array.size();
        }

        bool is_parent_of(ReplicaID potentialParent, ReplicaID potentialChild) const
        {
            size_t childPos = get_node_position(potentialChild);

            if (childPos == 0 || childPos >= tree_array.size())
                return false;

            size_t parentPos = (childPos - 1) / fanout;

            return tree_array[parentPos] == potentialParent;
        }

        bool violates_votes_constraint(const std::set<std::pair<ReplicaID, ReplicaID>> &constraints) const
        {

            for (const auto &pair : constraints)
            {
                ReplicaID reporter = pair.first;
                ReplicaID target = pair.second;

                if (is_parent_of(reporter, target) || is_parent_of(target, reporter))
                    return true;
            }

            return false;
        }

        size_t get_height() const
        {
            if (tree_array.empty())
                return 0;

            size_t height = 0;
            size_t capacity = 1;

            while (capacity < tree_array.size())
            {
                height++;
                capacity *= fanout;
            }

            return height;
        }

        operator std::string() const
        {
            DataStream s;
            s << "<tree "
              << "tid=" << std::to_string(tid) << " "
              << "tree_size=" << std::to_string(tree_array.size()) << " "
              << "fanout=" << std::to_string(fanout) << " "
              << "pipe_stretch=" << std::to_string(pipe_stretch) << " "
              << "root_node=" << std::to_string(tree_array[0]) << " "
              << "tree_array=[ ";

            // Add each element of tree_array to the string
            for (size_t i = 0; i < tree_array.size(); i++)
            {
                s << std::to_string(tree_array[i]);
                if (i != tree_array.size() - 1)
                    s << ", "; // Add a comma between elements, but not after the last one
            }

            s << " ]>";
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
        std::set<ReplicaID> childrenSet;

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

        TreeNetwork(const Tree &t,
                    const ReplicaConfig &replicas,
                    const ReplicaID myReplicaId) : tree(t)
        {
            initializeTreeNetwork(
                [&replicas](ReplicaID replica) {
                    return replicas.get_peer_id(replica);
                },
                myReplicaId);
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

        const std::set<ReplicaID> &get_childrenSet() const { return childrenSet; }

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

        std::set<ReplicaID> collectChildren(int index, int treeSize)
        {
            std::set<ReplicaID> childrenSet;
            auto fanout = tree.get_fanout();

            for (int i = 1; i <= fanout; i++)
            {
                auto child_idx = fanout * index + i;

                // If within bounds of array, child exists
                if (child_idx < treeSize)
                {
                    // Add the child's ReplicaID to the set
                    childrenSet.insert(tree_array[child_idx]);

                    // Recursively collect the children's subtree ReplicaIDs
                    std::set<ReplicaID> subtreeChildren = collectChildren(child_idx, treeSize);
                    childrenSet.insert(subtreeChildren.begin(), subtreeChildren.end());
                }
            }

            return childrenSet;
        }

        template<typename PeerResolver>
        void initializeTreeNetwork(
            PeerResolver peer_for,
            const ReplicaID myReplicaId)
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
                parentPeer = peer_for(tree_array[parent_idx]);
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
                    childPeers.insert(peer_for(tree_array[child_idx]));
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
            childrenSet = collectChildren(myTreeId, size);
            info << "\tTotal children in my subtree: " << std::to_string(numberOfChildren) << "\n";

            info << "\tMy ReplicaID: " << std::to_string(myReplicaId) << "\n";
            info << "\tMy ID in the tree: " << std::to_string(myTreeId) << "\n";
        }

        void initializeTreeNetwork(
            const std::vector<std::tuple<NetAddr, pubkey_bt, uint256_t>> &replicas,
            const ReplicaID myReplicaId)
        {
            initializeTreeNetwork(
                [&replicas](ReplicaID replica) {
                    return PeerId{std::get<2>(replicas.at(replica))};
                },
                myReplicaId);
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

    /**
     * Find a message tree without indexing untrusted identifiers or
     * populating Epoch::system_trees. The returned pointer refers to the
     * immutable tree_networks entry owned by epochs.
     */
    const TreeNetwork *find_message_tree(const std::vector<Epoch> &epochs,
                                         uint32_t epoch_nr,
                                         uint32_t tree_id) noexcept;

    /**
     * Event-loop admission checks shared by the real handlers and socket-free
     * tests. cryptographically_verified is the immutable result returned by
     * the verification worker; these functions never perform cryptography or
     * mutate consensus state.
     */
    bool admit_verified_vote(const ReplicaConfig &config,
                             uint32_t expected_epoch,
                             const TreeNetwork &tree,
                             const PeerId &authenticated_peer,
                             const Vote &vote,
                             bool cryptographically_verified) noexcept;

    bool admit_verified_relay(const ReplicaConfig &config,
                              uint32_t expected_epoch,
                              const TreeNetwork &tree,
                              const PeerId &authenticated_peer,
                              const VoteRelay &relay,
                              bool cryptographically_verified) noexcept;

    /**
     * Start vote/certificate verification and referenced-block delivery once,
     * then enter the event-loop continuation only after both succeed. The
     * overloads are intentionally narrow so tests can exercise the same
     * asynchronous boundary used by the production handlers without sockets.
     */
    promise_t coordinate_verified_delivery(
        const Vote &message,
        std::function<promise_t()> start_worker_verification,
        std::function<promise_t()> start_block_delivery,
        std::function<void(const block_t &)> continuation);

    promise_t coordinate_verified_delivery(
        const VoteRelay &message,
        std::function<promise_t()> start_worker_verification,
        std::function<promise_t()> start_block_delivery,
        std::function<void(const block_t &)> continuation);

    struct EpochReputation : public Serializable
    {

        Epoch epoch;
        std::unordered_map<ReplicaID, int> repScore;

    public:
        EpochReputation() = default;

        EpochReputation(const Epoch &epoch, const std::unordered_map<ReplicaID, int> &repScore)
            : epoch(epoch), repScore(repScore) {}

        const std::unordered_map<ReplicaID, int> &get_reputation() const { return repScore; }

        void serialize(DataStream &s) const override
        {
            epoch.serialize(s);
            uint32_t mapSize = repScore.size();
            s << htole(mapSize);
            for (const auto &p : repScore)
            {
                s << p.first << p.second;
            }
        }

        void unserialize(DataStream &s) override
        {
            epoch.unserialize(s);

            uint32_t mapSize;
            s >> mapSize;
            mapSize = letoh(mapSize);

            repScore.clear();
            for (uint32_t i = 0; i < mapSize; i++)
            {
                ReplicaID id;
                int score;
                s >> id >> score;

                repScore[id] = score;
            }
        }

        operator std::string() const
        {
            DataStream s;

            s << "Epoch number: " << epoch.get_epoch_num() << "\nReputation: ";

            for (const auto &p : repScore)
            {
                s << "(" << p.first << ": " << p.second << ") ";
            }
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

        ReplicaID missing_voter;
        // Possibly store how long we waited, or a timestamp, or # of attempts, etc.

        TimeoutMeasure() = default;

        TimeoutMeasure(ReplicaID non_responsive_replica, uint32_t epoch_nr, uint32_t tid, ReplicaID missing_voter)
            : non_responsive_replica(non_responsive_replica), epoch_nr(epoch_nr), tid(tid), missing_voter(missing_voter) {}
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
                s << tm.missing_voter;
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
                s >> timeouts[i].missing_voter;
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
        bool postponed_parse(HotStuffCore *hsc) noexcept;
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
        bool postponed_parse(HotStuffCore *hsc) noexcept;
    };

    /** Dedicated experiment-only PQAR wire message. */
    struct MsgExperimentPostQcAuditRelay
    {
        static const opcode_t opcode = 0x1F;
        DataStream serialized;
        ExperimentPostQcAuditRelay relay;
        std::size_t wire_bytes{0};
        MsgExperimentPostQcAuditRelay(
            const ExperimentPostQcAuditRelay &relay);
        MsgExperimentPostQcAuditRelay(DataStream &&stream)
            : serialized(std::move(stream)),
              wire_bytes(serialized.size()) {}
        bool postponed_parse(HotStuffCore *hsc) noexcept;
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

    /** HotStuff protocol (with network implementation). */
    class HotStuffBase : public HotStuffCore,
                         private ProposalAdmissionEffects
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
        const EpochProtocolMode epoch_protocol_mode;
        std::optional<PeerId> epoch_manager_peer;
        std::optional<NetAddr> epoch_manager_address;
        EpochWireLimits epoch_wire_limits{4 << 20, 128, 4096, 4096};
        std::uint64_t epoch_activation_grace_blocks{1};
        bool adaptive_demo_markers{false};
        // Borrowed observational capabilities. The caller must unbind them
        // before any emitter is destroyed.
        StructuredEventEmitter *structured_event_emitter{nullptr};
        AdaptiveStructuredEventEmitter *adaptive_event_emitter{nullptr};
        AuditStructuredEventEmitter *audit_event_emitter{nullptr};
        std::unordered_set<uint256_t> valid_tls_certs;
#ifdef HOTSTUFF_BLK_PROFILE
        BlockProfiler blk_profiler;
#endif
        pacemaker_bt pmaker;
        TimerEvent ev_beat_timer;
        TimerEvent ev_end_warmup;

        TimerEvent ev_report_timer;
        double report_period = 1.0;

        size_t warmup_counter = 0;

        /* queues for async tasks */

        std::unordered_map<const uint256_t, BlockFetchContext> blk_fetch_waiting;
        BlockDeliveryOrchestrator blk_delivery_orchestrator;
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
        std::vector<ReplicaID> fixed_membership;

        // std::unordered_map<size_t, Tree> system_trees;
        // TODO: deprecated
        std::unordered_map<size_t, TreeNetwork> system_trees;

        // Reports stuff
        std::mutex metrics_lock;
        std::vector<LatMeasure> peer_latencies;
        std::vector<TimeoutMeasure> child_timeouts;

        std::unordered_map<ReplicaID, int> reputation;
        std::unordered_map<ReplicaID, int> reputation_on_hold;

        NetAddr reputation_addr;
        Net::MsgNet::conn_t reputation_server_conn;
        Net rn = Net(ec, Net::Config());

        std::vector<Epoch> epochs;
        std::unique_ptr<EpochStore> exact_epochs;
        FutureProposalBuffer future_proposals;
        std::unique_ptr<ProposalAdmissionCoordinator> proposal_admission;
        std::unique_ptr<AggregationScheduler> aggregation_scheduler;
        std::shared_ptr<ProposalContextLifecycle> proposal_contexts;
        PendingExactContributionBuffer pending_exact_contributions{
            PendingExactContributionBufferLimits{4096, 64}};
        struct ExactRuntimeAccess;
        class ExactContributionEffects;
        std::shared_ptr<ExactRuntimeAccess> exact_runtime_access;
        AggregationTimeoutPolicy aggregation_timeout_policy;
        std::unique_ptr<AggregationTimeoutCoordinator>
            aggregation_timeout_coordinator;
        std::unique_ptr<AdaptiveV2ResponseEvidenceBridge>
            adaptive_v2_response_evidence;
        std::unique_ptr<ExperimentByzantineAdapter>
            experiment_byzantine_adapter;
        std::unique_ptr<ExperimentPostQcAudit>
            experiment_post_qc_audit;
        AggregationScheduler::Cancellation
            experiment_post_qc_audit_deadline_cancellation;
        AggregationScheduler::Cancellation
            experiment_post_qc_audit_expiry_cancellation;
        std::string experiment_diagnostic_window;
        // Experiment-only ordering state. The configured adapter bound is
        // copied before startup and caps this exact-proposal map.
        enum class ExperimentFalseTimeoutCommitAction
        {
            report_now,
            deferred,
            already_deferred,
        };
        enum class ExperimentFalseTimeoutCompletionAction
        {
            no_deferred_commit,
            release_commit,
            fail_closed,
        };
        struct ExperimentFalseTimeoutState
        {
            ReplicaID target{0};
            bool scheduled{false};
            bool commit_deferred{false};

            bool may_suppress(ReplicaID exact_target) const noexcept
            {
                return scheduled && target == exact_target;
            }

            ExperimentFalseTimeoutCommitAction observe_commit(
                bool retain_response_evidence) noexcept
            {
                if (!scheduled || !retain_response_evidence)
                    return ExperimentFalseTimeoutCommitAction::report_now;
                if (commit_deferred)
                    return ExperimentFalseTimeoutCommitAction::
                        already_deferred;
                commit_deferred = true;
                return ExperimentFalseTimeoutCommitAction::deferred;
            }

            ExperimentFalseTimeoutCompletionAction complete(
                bool evidence_queued_before_commit) const noexcept
            {
                if (!commit_deferred)
                    return ExperimentFalseTimeoutCompletionAction::
                        no_deferred_commit;
                return evidence_queued_before_commit
                    ? ExperimentFalseTimeoutCompletionAction::release_commit
                    : ExperimentFalseTimeoutCompletionAction::fail_closed;
            }
        };
        friend class ExperimentFalseTimeoutFenceTestAccess;
        friend class ExperimentByzantineRuntimeIntegrationTestAccess;
        std::size_t maximum_experiment_false_timeout_contexts{0};
        std::map<ProposalKey, ExperimentFalseTimeoutState>
            experiment_false_timeout_states;
        std::unique_ptr<AdaptiveV2ReportingOutbox>
            adaptive_v2_reporting_outbox;
        AggregationScheduler::Cancellation
            adaptive_v2_reporting_flush_cancellation;
        bool adaptive_v2_readiness_enqueued{false};
        enum class AdaptiveV2DurableInitializationPhase
        {
            ready_to_enqueue,
            queued,
            suppressed,
        };
        // Initialization must enter the shared FIFO before evidence for the
        // same exact proposal. Retaining the queued state also deduplicates
        // repeated initialization callbacks until commit admission.
        std::map<ProposalKey, AdaptiveV2DurableInitializationPhase>
            adaptive_v2_durable_initialization_reports;
        enum class AdaptiveV2DurableCommitPhase
        {
            awaiting_evidence,
            ready_to_enqueue,
            suppressed,
        };
        struct AdaptiveV2DurableCommitReportState
        {
            AdaptiveV2DurableCommitPhase phase{
                AdaptiveV2DurableCommitPhase::awaiting_evidence};
            bool experiment_false_report{false};
            ReplicaID experiment_false_target{0};
            std::size_t experiment_false_recorded_evidence{0};
        };
        // Consensus is never delayed. The exact observational commit notice
        // remains durable until its evidence fence and shared-outbox enqueue
        // both complete, or until the observation fails closed.
        std::map<ProposalKey, AdaptiveV2DurableCommitReportState>
            adaptive_v2_durable_commit_reports;
        bool adaptive_v2_lifecycle_reporting_suppressed{false};
        enum class ExactForwardingRole
        {
            initial_aggregate,
            delta
        };
        struct ExactForwardingRetryJob;
        std::uint64_t next_exact_forwarding_retry_id{1};
        std::map<std::uint64_t,
                 std::shared_ptr<ExactForwardingRetryJob>>
            exact_forwarding_retry_jobs;
        std::set<std::pair<ProposalKey, std::uint64_t>>
            exact_forwarding_sweeps;
        struct ExactVoteFallbackJob;
        struct ExactProposalFallbackJob;
        std::map<ProposalKey,
                 std::shared_ptr<ExactVoteFallbackJob>>
            exact_vote_fallback_jobs;
        // Active exact proposals delivered directly from their authenticated
        // root to a non-child descendant are repair traffic.  The descendant
        // may return its already-authorized vote immediately instead of
        // waiting for the ordinary full-tree vote fallback deadline.
        std::map<ProposalKey, std::uint64_t>
            exact_root_repair_deliveries;
        std::map<ProposalKey,
                 std::shared_ptr<ExactProposalFallbackJob>>
            exact_proposal_fallback_jobs;
        struct AdaptiveEpochRuntime;
        std::unique_ptr<AdaptiveEpochRuntime> adaptive_epoch_runtime;
        HotStuffEpochLiveBinding *epoch_live_binding{nullptr};
        std::unique_ptr<EpochChangeVerifier> epoch_change_verifier;
        std::optional<EpochChangeBundleLimits>
            adaptive_v2_epoch_change_bundle_limits;
        std::unique_ptr<AdaptiveV2CommandInbox>
            adaptive_v2_command_inbox;
        std::optional<std::uint64_t>
            adaptive_v2_pending_command_reservation;
        std::size_t epoch_change_maximum_block_extra_bytes{0};
        std::size_t epoch_change_maximum_ancestry_blocks{0};
        struct CommittedEpochChangeHistoryState
        {
            block_t head;
            EpochChangeCommittedHistorySnapshot snapshot;
        };
        std::optional<CommittedEpochChangeHistoryState>
            committed_epoch_change_history;
        struct PendingCommittedEpochChange
        {
            uint256_t block_hash;
            AuthorizedEpochChange command;
            uint256_t payload_digest;
        };
        std::optional<PendingCommittedEpochChange>
            pending_committed_epoch_change;
        struct CommittedEpochDefinitionRecovery
        {
            EpochDefinitionRequest request;
            AuthorizedEpochChange command;
            uint256_t command_block_hash;
            uint256_t payload_digest;
            std::uint64_t command_commit_height{0};
            std::uint64_t activation_height{0};
            block_t activation_block;
            bool definition_recovered{false};
            std::uint64_t retry_generation{0};
            std::uint64_t retry_attempts{0};
            AggregationScheduler::Cancellation retry_cancellation;
        };
        std::optional<CommittedEpochDefinitionRecovery>
            committed_epoch_definition_recovery;
        std::uint64_t
            next_committed_epoch_definition_retry_generation{1};
        std::optional<AdaptiveV2EpochChangeIdentity>
            adaptive_v2_committed_convergence_identity;
        bool adaptive_v2_commit_observation_enqueued{false};
        bool adaptive_v2_activation_observation_pending{false};
        bool adaptive_v2_convergence_evidence_healthy{true};
        struct PendingAdaptiveV2Commit
        {
            uint256_t block_hash;
            std::optional<ProposalKey> committed_key;
            std::optional<std::uint64_t> view_generation;
        };
        std::optional<PendingAdaptiveV2Commit>
            pending_adaptive_v2_commit;
        static constexpr std::size_t
            maximum_proposal_view_generation_observations{4096};
        // Evidence-only identity captured at proposal production/processing.
        // A disengaged mapped value permanently marks an exact-key conflict.
        std::map<ProposalKey, std::optional<std::uint64_t>>
            proposal_view_generations;
        std::optional<std::size_t> adaptive_v2_tree_switch_period;
        std::unique_ptr<AdaptiveV2RotationCoordinator>
            adaptive_v2_rotation_coordinator;
        static constexpr std::size_t
            maximum_pending_epoch_definition_digests{8};
        static constexpr std::size_t
            maximum_deferred_epoch_change_proposals{256};
        struct DeferredEpochDefinitionRecovery
        {
            EpochDefinitionRequest request;
            bool request_live{true};
            std::map<ProposalKey, BufferedProposal> proposals;
        };
        std::map<uint256_t, DeferredEpochDefinitionRecovery>
            deferred_epoch_definition_recoveries;
        std::size_t deferred_epoch_change_proposal_count{0};
        mutable TreeNetwork current_tree_network;
        mutable Tree current_tree;
        uint32_t lastCheckedHeight;

        /* Epoch */

        // TODO: also becoming deprecated as we just need to store the indexes
        mutable Epoch cur_epoch;
        mutable Epoch on_hold_epoch;

        size_t reconfig_count;
        bool warmup_finished;

        /* communication */

        void on_fetch_cmd(const command_t &cmd);
        void on_fetch_blk(const block_t &blk);
        bool deliver_blk_without_finalization(const block_t &blk);
        bool on_deliver_blk(const block_t &blk);

        const EpochDefinition &register_initial_epoch(const Epoch &epoch);
        const EpochDefinition &register_legacy_epoch(const Epoch &epoch);
        ConfigurationId exact_configuration(
            uint32_t epoch_number, uint32_t tree_id) const;
        const TreeNetwork *find_exact_runtime_tree(
            const ConfigurationId &configuration) const noexcept;
        std::optional<std::uint64_t> find_exact_runtime_generation(
            const ConfigurationId &configuration) const noexcept;
        std::optional<ProposalContextMetadata> exact_context_metadata(
            const ProposalKey &key) const;
        std::optional<ProposalContextLease> admit_exact_context(
            const ProposalContextMetadata &metadata,
            ProposalContextOrigin origin);
        void activate_proposal_configuration(
            const ConfigurationId &configuration);
        void activate_initial_leader_view();
        void initialize_adaptive_epoch_runtime();
        EpochChangeProposalChainResult pre_vote_epoch_change_gate(
            const Proposal &proposal) const noexcept;
        bool retain_deferred_epoch_change(
            BufferedProposal proposal,
            const EpochDefinitionRequest &request) noexcept;
        void erase_deferred_epoch_change(
            const ProposalKey &key) noexcept;
        void retire_deferred_epoch_changes_for_block(
            const uint256_t &block_hash) noexcept;
        void retire_deferred_epoch_changes_before_epoch(
            std::uint32_t first_live_epoch) noexcept;
        void send_epoch_definition_request(
            const EpochDefinitionRequest &request) noexcept;
        bool queue_deferred_epoch_change_retries(
            const uint256_t &successor_epoch_digest) noexcept;
        void retry_deferred_epoch_changes(
            const uint256_t &successor_epoch_digest) noexcept;
        bool retain_committed_epoch_definition_recovery(
            const block_t &block,
            const AuthorizedEpochChange &command,
            const ActivationRecord &record) noexcept;
        bool schedule_committed_epoch_definition_retry() noexcept;
        void dispatch_committed_epoch_definition_retry(
            std::uint64_t retry_generation,
            const uint256_t &command_block_hash,
            const uint256_t &successor_epoch_digest) noexcept;
        void cancel_committed_epoch_definition_retry() noexcept;
        void reset_committed_epoch_definition_recovery() noexcept;
        bool recover_committed_epoch_definition(
            const EpochDefinition &definition) noexcept;
        void initialize_committed_epoch_change_history() noexcept;
        void record_committed_epoch_change_history(
            const block_t &block) noexcept;
        void install_legacy_consensus_handlers();
        void install_adaptive_epoch_handlers();
        void install_adaptive_consensus_handlers();
        void install_adaptive_v2_definition_handlers();
        void experiment_post_qc_audit_relay_handler(
            MsgExperimentPostQcAuditRelay &&message,
            const Net::conn_t &connection);
        void adaptive_definition_request_handler(
            MsgEpochDefinitionRequest &&message,
            const Net::conn_t &conn);
        void adaptive_definition_reply_handler(
            MsgEpochDefinitionReply &&message,
            const Net::conn_t &conn);
        void adaptive_v2_epoch_change_bundle_handler(
            MsgAdaptiveV2EpochChangeBundle &&message,
            const Net::conn_t &conn);
        void adaptive_v2_convergence_ack_handler(
            MsgAdaptiveV2ConvergenceObservationAck &&message,
            const Net::conn_t &connection);
        bool authorize_manager_peer(const PeerId &peer) const noexcept;
        void bind_adaptive_v2_manager_reporting_transport();
        EvidenceTransportResult enqueue_adaptive_v2_evidence_report(
            const EvidenceReportEnvelope &report) noexcept;
        void enqueue_initial_adaptive_v2_readiness() noexcept;
        void report_adaptive_v2_runtime_initialized(
            const ProposalKey &key) noexcept;
        void report_adaptive_v2_committed(
            const std::optional<ProposalKey> &key) noexcept;
        bool try_enqueue_adaptive_v2_runtime_initialized_report(
            const ProposalKey &key) noexcept;
        void retry_ready_adaptive_v2_runtime_initialized_reports()
            noexcept;
        void observe_adaptive_v2_response_deadline_result(
            const ProposalKey &key,
            EvidenceDeadlineResult result) noexcept;
        bool persist_adaptive_v2_commit_report(
            const ProposalKey &key,
            AdaptiveV2DurableCommitReportState state,
            const char *failure_reason) noexcept;
        bool try_enqueue_adaptive_v2_commit_report(
            const ProposalKey &key) noexcept;
        void retry_ready_adaptive_v2_commit_reports() noexcept;
        bool has_durable_adaptive_v2_commit_report(
            const ProposalKey &key) const noexcept;
        bool has_pending_adaptive_v2_lifecycle_fence() const noexcept;
        void suppress_adaptive_v2_lifecycle_reporting(
            const char *reason) noexcept;
        void poison_adaptive_v2_reporting(const char *reason) noexcept;
        AdaptiveV2ReportingDeliveryResult
        transmit_adaptive_v2_report(
            const AdaptiveV2PendingReport &report) noexcept;
        void schedule_adaptive_v2_reporting_flush(
            AggregationScheduler::Duration delay) noexcept;
        void cancel_adaptive_v2_reporting_flush() noexcept;
        void flush_adaptive_v2_reporting() noexcept;
        void enqueue_pending_adaptive_v2_commit_observation() noexcept;
        void enqueue_pending_adaptive_v2_activation_observation()
            noexcept;
        void mark_adaptive_v2_convergence_evidence_unhealthy(
            const char *reason) noexcept;
        void rebuild_aggregation_timeout_coordinator();
        void schedule_experiment_false_timeout(
            const ProposalKey &key,
            ReplicaID target,
            AggregationScheduler::Duration delay);
        void dispatch_experiment_false_timeout(
            const ProposalKey &key,
            ReplicaID target,
            std::string window);
        void cancel_experiment_false_timeout(
            const ProposalKey &key,
            ReplicaID target,
            const char *reason) noexcept;
        void release_experiment_false_report_commit(
            const ProposalKey &key,
            std::size_t recorded_evidence,
            bool evidence_queued_before_commit) noexcept;
        void arm_experiment_post_qc_audit(
            const ProposalContextLease &lease) noexcept;
        void synchronize_experiment_post_qc_audit(
            const ProposalContextLease &lease,
            ReplicaID authenticated_sender,
            std::uint64_t arrival_ns) noexcept;
        bool observe_experiment_post_qc_audit_terminal_vote(
            const Vote &vote,
            ReplicaID authenticated_sender,
            std::uint64_t arrival_ns);
        void dispatch_experiment_post_qc_audit_deadline() noexcept;
        void prepare_experiment_post_qc_audit_root(
            const ProposalContextLease &lease,
            const QuorumCert &verified_qc) noexcept;
        void activate_experiment_post_qc_audit_root(
            const ProposalKey &key) noexcept;
        void expire_experiment_post_qc_audit_root() noexcept;
        void emit_experiment_post_qc_audit_target(
            const ExperimentPostQcAuditTargetObservation &observation)
            const noexcept;
        void emit_experiment_post_qc_audit_root_prepared(
            const ExperimentPostQcAuditRootSnapshot &snapshot)
            const noexcept;
        void emit_experiment_post_qc_audit_root_snapshot(
            const ExperimentPostQcAuditRootSnapshot &snapshot)
            const noexcept;
        std::optional<ProposalKey> committed_proposal_key(
            const block_t &blk,
            const std::vector<ProposalKey> &committed_keys) const;
        std::optional<uint256_t>
        adaptive_v2_committed_epoch_change_payload_digest(
            const block_t &blk) const noexcept;
        void observe_authoritative_commit(
            const std::optional<ProposalKey> &committed_proposal,
            const std::optional<uint256_t> &committed_payload_digest)
            noexcept;
        bool observe_proposal_view_generation(
            const ProposalKey &key,
            std::uint64_t generation) noexcept;
        std::optional<std::uint64_t> proposal_view_generation(
            const ProposalKey &key) const noexcept;
        bool adaptive_v2_runtime_initialization_is_referenced(
            const ProposalKey &key) const noexcept;
        void retire_adaptive_v2_runtime_initialized_report(
            const ProposalKey &key) noexcept;
        void forget_proposal_view_generation(
            const ProposalKey &key) noexcept;
        void forget_proposal_view_generations_for_block(
            const uint256_t &block_hash) noexcept;
        void forget_proposal_view_generations_before_epoch(
            std::uint32_t first_live_epoch) noexcept;
        void cache_adaptive_v2_commit(
            const block_t &blk,
            const std::vector<ProposalKey> &committed_keys) noexcept;
        void rotate_adaptive_v2_after_commit(
            const std::optional<ProposalKey> &committed_key) noexcept;
        void record_adaptive_commit_marker(
            const block_t &blk,
            const std::vector<ProposalKey> &committed_keys) const;
        void finish_adaptive_epoch_commit(
            const block_t &blk,
            const EpochCommitIngressResult &activation);
        void advance_committed_retirement_floor(
            const block_t &blk,
            const std::vector<ProposalKey> &committed_keys);

        promise_t verify_exact_contribution(
            ExactContributionKind kind,
            const ExactContributionEnvelope &contribution);
        void buffer_or_dispatch_exact_contribution(
            ExactContributionKind kind,
            ExactContributionEnvelope envelope,
            PeerId authenticated_source,
            std::uint64_t received_ns = 0);
        void dispatch_exact_contribution(
            PendingExactContribution contribution);
        void drain_pending_exact_contributions(const ProposalKey &key);
        void purge_pending_exact_contributions(
            const ProposalKey &key,
            bool preserve_scheduled_vote_fallback = false,
            bool preserve_response_evidence_until_deadline = false);
        promise_t deliver_exact_contribution(
            const ProposalKey &key,
            const PeerId &source_peer);
        void continue_exact_contribution(
            const ProposalContextLease &lease,
            ExactContributionKind kind,
            const ExactContributionEnvelope &contribution,
            std::uint64_t received_ns);
        void record_exact_latency(
            const ProposalContextLease &lease,
            ReplicaID child);
        bool send_exact_relay(
            const ProposalContextLease &lease,
            quorum_cert_bt certificate);
        bool send_exact_relay_reserved(
            const ProposalContextLease &lease,
            ProposalForwardingClaim claim,
            ExactForwardingRole role,
            std::shared_ptr<ExactForwardingRetryJob> retry = nullptr);
        quorum_cert_bt make_exact_direct_forwarding_candidate(
            const ProposalContextLease &lease,
            const Vote &vote);
        void complete_exact_forwarding(
            const ProposalContextLease &lease,
            ExactForwardingRole role);
        void drain_pending_exact_forwarding_candidates(
            const ProposalContextLease &lease);
        void schedule_exact_forwarding_retry(
            const ProposalContextLease &lease,
            quorum_cert_bt certificate,
            const std::set<ReplicaID> &signers,
            std::optional<std::uint64_t> pending_candidate_id,
            ExactForwardingRole role,
            std::uint32_t attempts);
        void dispatch_exact_forwarding_retry(
            const std::shared_ptr<ExactForwardingRetryJob> &retry);
        void abort_exact_forwarding(
            const ProposalContextLease &lease,
            const std::shared_ptr<ExactForwardingRetryJob> &retry,
            const char *reason);
        void discard_exact_forwarding_retries(
            const ProposalKey &key,
            std::optional<std::uint64_t> generation = std::nullopt);
        void cancel_all_exact_forwarding_retries() noexcept;
        void schedule_exact_vote_fallback(
            const ProposalContextLease &lease,
            const Vote &vote);
        void observe_exact_root_repair_delivery(
            const EpochConsensusEnvelope &envelope,
            ReplicaID authenticated_sender,
            ProposalDisposition disposition);
        void dispatch_exact_vote_fallback(
            const ProposalKey &key,
            std::uint64_t context_generation);
        bool send_exact_vote_to_root(
            const ProposalKey &key,
            std::uint64_t epoch_generation,
            ReplicaID root,
            const Vote &vote);
        void schedule_exact_proposal_fallback(
            const ProposalContextLease &lease,
            const Proposal &proposal);
        void dispatch_exact_proposal_fallback(
            const ProposalKey &key,
            std::uint64_t context_generation);
        void arm_exact_proposal_repair_tail(
            const ProposalContextLease &lease) noexcept;
        void dispatch_exact_proposal_repair_tail(
            const std::shared_ptr<ExactProposalFallbackJob> &job);
        bool broadcast_exact_proposal_fallback(
            const ProposalContextLease &lease,
            std::uint64_t epoch_generation,
            const Proposal &proposal,
            std::size_t &target_cursor,
            std::size_t &total_send_attempts,
            std::vector<ReplicaID> &attempted_targets,
            const bool &quorum_observed,
            std::size_t total_attempt_budget,
            std::size_t maximum_attempts,
            std::uint32_t repair_stage);
        bool reserve_exact_proposal_pre_quorum_refresh(
            const ProposalContextLease &lease,
            ExactProposalFallbackJob &job,
            std::size_t maximum_attempts);
        bool broadcast_exact_proposal_pre_quorum_retry(
            const ProposalContextLease &lease,
            ExactProposalFallbackJob &job);
        bool broadcast_exact_proposal_repair_tail(
            ExactProposalFallbackJob &job);
        void discard_exact_fallbacks(
            const ProposalKey &key,
            bool preserve_scheduled_vote_fallback = false);
        void discard_exact_fallbacks_before_epoch(
            std::uint32_t first_live_epoch) noexcept;
        void cancel_all_exact_fallbacks() noexcept;
        bool consume_experiment_outbound_direct_vote(
            const ProposalKey &key,
            const ProposalTreeSnapshot &tree);
        bool consume_experiment_outbound_aggregate(
            const ProposalKey &key,
            const ProposalTreeSnapshot &tree);
        bool forward_exact_direct(
            const ProposalContextLease &lease,
            const Vote &vote);
        bool forward_exact_relay(
            const ProposalContextLease &lease,
            const VoteRelay &relay);
        quorum_cert_bt verified_aggregation_candidate(
            const ProposalContextLease &lease);
        void record_aggregation_timeout(
            const ProposalContextLease &lease,
            const std::set<ReplicaID> &missing);
        void record_optional_aggregation_absence(
            const ProposalContextLease &lease,
            const std::set<ReplicaID> &missing);
        void emit_adaptive_aggregation_event(
            AdaptiveAggregationTransition transition,
            const ProposalContextLease &lease,
            const std::set<ReplicaID> *accepted_signers = nullptr,
            const std::set<ReplicaID> *missing_optional = nullptr,
            const std::map<ReplicaID, std::set<ReplicaID>> *
                required_gaps = nullptr,
            std::size_t root_signer_count = 0,
            std::size_t global_quorum = 0,
            const char *reason = nullptr) noexcept;
        void emit_active_configuration_event(
            const ConfigurationId &configuration) noexcept;
        void emit_fault_contribution_opportunity(
            const ExperimentOmissionMarker &marker) noexcept;
        void emit_root_qc_queue_blocked_event(
            const ProposalContextLease &candidate_lease,
            const block_t &candidate) noexcept;
        void emit_committed_block_event(
            const block_t &blk,
            const std::optional<ProposalKey> &committed_key,
            const std::optional<std::uint64_t> &view_generation,
            std::uint64_t commit_batch_index) noexcept;
        void emit_commit_observed_event(
            const block_t &blk,
            std::uint64_t commit_batch_index) noexcept;
        void emit_epoch_command_committed_event(
            const block_t &blk,
            const AuthorizedEpochChange &command,
            const ActivationRecord &record) noexcept;
        void emit_epoch_lifecycle_event(
            EpochLifecycleTransition transition,
            const ConfigurationId &configuration,
            std::uint64_t activation_height) noexcept;
        void try_finish_exact_context(
            const ProposalContextLease &lease);
        bool publish_exact_root_qc(
            const ProposalContextLease &lease,
            quorum_cert_bt final_qc);
        void drain_ready_piped_qcs();

        void relay_once(const BufferedProposal &proposal) override;
        void process_active(const BufferedProposal &proposal) override;
        void local_vote_authorized(const ProposalKey &key) override;
        void create_expected_vote_state(const ProposalKey &key) override;
        void start_latency_deadline(const ProposalKey &key) override;
        void start_aggregation_timer(const ProposalKey &key) override;
        void emit_timeout_report(const ProposalKey &key) override;

        /** deliver consensus message: <propose> */
        inline void propose_handler(MsgPropose &&, const Net::conn_t &);
        /** deliver consensus message: <vote> */
        inline void vote_handler(MsgVote &&, const Net::conn_t &);
        /** deliver consensus relay message: <vote_relay> */
        inline void vote_relay_handler(MsgRelay &&, const Net::conn_t &);
        inline void adaptive_stage_epoch_handler(
            MsgStageEpochDefinition &&, const Net::conn_t &);
        inline void adaptive_arm_epoch_handler(
            MsgArmActivation &&, const Net::conn_t &);
        inline void adaptive_propose_handler(
            MsgPropose &&, const Net::conn_t &);
        inline void adaptive_vote_handler(
            MsgVote &&, const Net::conn_t &);
        inline void adaptive_relay_handler(
            MsgRelay &&, const Net::conn_t &);
        /** fetches full block data */
        inline void req_blk_handler(MsgReqBlock &&, const Net::conn_t &);
        /** receives a block */
        inline void resp_blk_handler(MsgRespBlock &&, const Net::conn_t &);

        inline bool conn_handler(const salticidae::ConnPool::conn_t &, bool);

        void do_broadcast_proposal(const Proposal &) override;
        void do_vote(Proposal, const Vote &) override;
        bool admit_local(const Proposal &) override;
        void apply_local_vote(const Vote &) override;
        void on_local_proposal_processed(
            const ProposalKey &key) override;
        void on_verified_commit_progress(
            const ProposalKey &key) override;
        void inc_time(ReconfigurationType reconfig_type) override;
        bool is_proposer(int id) override;
        void proposer_base_deliver(const block_t &blk) override;
        void do_decide(Finality &&) override;
        void do_consensus(const block_t &blk) override;
        void do_post_block_commit(
            const block_t &blk,
            std::uint64_t commit_batch_index) override;
        uint32_t get_tree_id() override;
        uint32_t get_cur_epoch_nr() override;
        uint256_t get_epoch_digest(uint32_t epoch_number) override;
        ConfigurationId get_exact_tree_configuration(
            std::uint32_t epoch_number,
            std::uint32_t tree_id) const override;
        ReplicaID get_exact_tree_root(
            std::uint32_t epoch_number,
            std::uint32_t tree_id) const override;
        LeaderTimeoutRotationDisposition rotate_tree_on_leader_timeout(
            const LeaderViewId &expired_view) noexcept override;

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
                     NetAddr reputation_addr,
                     EpochProtocolMode protocol_mode =
                         EpochProtocolMode::legacy_static);

        ~HotStuffBase();

        /* the API for HotStuffBase */

        /* Submit the command to be decided. */
        void exec_command(uint256_t cmd_hash, commit_cb_t callback);
        void start(std::vector<std::tuple<NetAddr, pubkey_bt, uint256_t>> &&replicas,
                   bool ec_loop = false);
        void set_tree_period(size_t nblocks);
        void set_aggregation_timeout(double timeout_seconds);
        void configure_experiment_byzantine_faults(
            ExperimentByzantineOptions options);
        void configure_experiment_post_qc_audit(
            ExperimentPostQcAuditOptions options);
        /**
         * Pin adaptive-v2 authorization and resource bounds before startup.
         * Until this is configured, adaptive-v2 proposal voting fails closed.
         */
        void configure_epoch_change_pre_vote_gate(
            EpochChangeIssuer issuer,
            EpochChangeDelayBounds delay_bounds,
            std::size_t maximum_block_extra_bytes,
            std::size_t maximum_ancestry_blocks,
            std::size_t maximum_bundle_bytes = 4 << 20);
        /**
         * Borrow event emitters without taking ownership. Passing null
         * unbinds a capability; bound emitters must outlive HotStuffBase.
         */
        void bind_structured_event_emitters(
            StructuredEventEmitter *lifecycle_emitter,
            AdaptiveStructuredEventEmitter *aggregation_emitter,
            AuditStructuredEventEmitter *audit_emitter = nullptr) noexcept;
        /**
         * Bind a local adaptive-v2 evidence outbox transport capability.
         * Delivery acceptance removes an outbox item only; it grants no
         * manager, epoch, topology, quorum, or consensus authority.
         */
        void bind_adaptive_v2_evidence_transport(
            EvidenceTransportCallback transport);
        void unbind_adaptive_v2_evidence_transport() noexcept;
        std::size_t flush_adaptive_v2_evidence() noexcept;
        void configure_epoch_manager(
            const PeerId &manager_peer,
            const NetAddr &manager_address);
        ReplicaStageIngressResult trusted_local_stage_epoch(
            StageEpochDefinition definition,
            const EpochValidationContext &validation_context);
        ReplicaArmIngressResult trusted_local_arm_epoch(
            ArmActivation activation);
        bool bootstrap_adaptive_epoch_from_file(
            const std::string &configuration_path,
            std::uint64_t activation_height);
        void tree_config(std::vector<std::tuple<NetAddr, pubkey_bt, uint256_t>> &&replicas);
        void read_epoch_from_file(std::vector<std::tuple<NetAddr, pubkey_bt, uint256_t>> &&replicas);
        void tree_scheduler(std::vector<std::tuple<NetAddr, pubkey_bt, uint256_t>> &&replicas, bool startup);
        // Updates vars related to epoch and trees
        void change_epoch();

        void stage_epoch(EpochReputation &epoch_reputation);

        // Reports functions
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
        size_t get_total_system_trees() override;
        ReplicaID get_system_tree_root(int tid) override;
        ReplicaID get_current_system_tree_root() override;
        TreeNetwork get_current_tree_network();
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
        part_cert_bt create_part_cert(const PrivKey &priv_key,
                                      const ProposalKey &key) override
        {
            HOTSTUFF_LOG_DEBUG("create part cert with priv=%s, blk_hash=%s",
                               get_hex10(priv_key).c_str(),
                               get_hex10(key.block_hash).c_str());
            return new PartCertType(
                static_cast<const PrivKeyType &>(priv_key),
                key);
        }

        part_cert_bt parse_part_cert(DataStream &s) override
        {
            part_cert_bt pc(new PartCertType());
            s >> *pc;
            return pc;
        }

        quorum_cert_bt create_quorum_cert(const ProposalKey &key) override
        {
            return new QuorumCertType(get_config(), key);
        }

        quorum_cert_bt parse_quorum_cert(DataStream &s) override
        {
            quorum_cert_bt qc(new QuorumCertType());
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
                 NetAddr reputation_addr = NetAddr(),
                 EpochProtocolMode protocol_mode =
                     EpochProtocolMode::legacy_static) : HotStuffBase(blk_size,
                                                                     rid,
                                                                     new PrivKeyType(raw_privkey),
                                                                     listen_addr,
                                                                     std::move(pmaker),
                                                                     ec,
                                                                     nworker,
                                                                     netconfig,
                                                                     reputation_addr,
                                                                     protocol_mode) {}

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

        void set_aggregation_timeout(double timeout_seconds)
        {
            HotStuffBase::set_aggregation_timeout(timeout_seconds);
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
