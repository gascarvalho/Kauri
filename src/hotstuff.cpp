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

#include "hotstuff/hotstuff.h"

#include <random>
#include <future>
#include <iostream>
#include <fstream>
#include "hotstuff/client.h"
#include "hotstuff/liveness.h"
#include <thread>
#include <chrono>
#include <spawn.h>

using salticidae::static_pointer_cast;

#define LOG_PROTO HOTSTUFF_LOG_PROTO
#define LOG_INFO HOTSTUFF_LOG_INFO
#define LOG_DEBUG HOTSTUFF_LOG_DEBUG
#define LOG_WARN HOTSTUFF_LOG_WARN

namespace hotstuff
{

    const opcode_t MsgPropose::opcode;
    MsgPropose::MsgPropose(const Proposal &proposal) { serialized << proposal; }
    void MsgPropose::postponed_parse(HotStuffCore *hsc)
    {
        proposal.hsc = hsc;
        HOTSTUFF_LOG_PROTO("Size of the block: %lld", serialized.size());
        serialized >> proposal;
    }

    const opcode_t MsgRelay::opcode;
    MsgRelay::MsgRelay(const VoteRelay &proposal) { serialized << proposal; }
    void MsgRelay::postponed_parse(HotStuffCore *hsc)
    {
        vote.hsc = hsc;
        serialized >> vote;
    }

    const opcode_t MsgVote::opcode;
    MsgVote::MsgVote(const Vote &vote) { serialized << vote; }
    void MsgVote::postponed_parse(HotStuffCore *hsc)
    {
        vote.hsc = hsc;
        serialized >> vote;
    }

    const opcode_t MsgReqBlock::opcode;
    MsgReqBlock::MsgReqBlock(const std::vector<uint256_t> &blk_hashes)
    {
        serialized << htole((uint32_t)blk_hashes.size());
        for (const auto &h : blk_hashes)
            serialized << h;
    }

    MsgReqBlock::MsgReqBlock(DataStream &&s)
    {
        uint32_t size;
        s >> size;
        size = letoh(size);
        blk_hashes.resize(size);
        for (auto &h : blk_hashes)
            s >> h;
    }

    const opcode_t MsgRespBlock::opcode;
    MsgRespBlock::MsgRespBlock(const std::vector<block_t> &blks)
    {
        serialized << htole((uint32_t)blks.size());
        for (auto blk : blks)
            serialized << *blk;
    }

    void MsgRespBlock::postponed_parse(HotStuffCore *hsc)
    {
        uint32_t size;
        serialized >> size;
        size = letoh(size);
        blks.resize(size);
        for (auto &blk : blks)
        {
            Block _blk;
            _blk.unserialize(serialized, hsc);
            blk = hsc->storage->add_blk(std::move(_blk), hsc->get_config());
        }
    }

    void HotStuffBase::exec_command(uint256_t cmd_hash, commit_cb_t callback)
    {
        cmd_pending.enqueue(std::make_pair(cmd_hash, callback));
    }

    void HotStuffBase::on_fetch_blk(const block_t &blk)
    {
#ifdef HOTSTUFF_BLK_PROFILE
        blk_profiler.get_tx(blk->get_hash());
#endif
        LOG_DEBUG("fetched %.10s", get_hex(blk->get_hash()).c_str());
        part_fetched++;
        fetched++;
        // for (auto cmd: blk->get_cmds()) on_fetch_cmd(cmd);
        const uint256_t &blk_hash = blk->get_hash();
        auto it = blk_fetch_waiting.find(blk_hash);
        if (it != blk_fetch_waiting.end())
        {
            it->second.resolve(blk);
            blk_fetch_waiting.erase(it);
        }
    }

    bool HotStuffBase::on_deliver_blk(const block_t &blk)
    {
        HOTSTUFF_LOG_PROTO("Base deliver for %.10s", get_hex10(blk->hash).c_str());

        const uint256_t &blk_hash = blk->get_hash();
        bool valid;
        /* sanity check: all parents must be delivered */
        for (const auto &p : blk->get_parent_hashes())
            if (!storage->is_blk_delivered(p))
                // std::cout << "PARENT ASSERT FAILED" << std::endl;
                assert(storage->is_blk_delivered(p));
        if ((valid = HotStuffCore::on_deliver_blk(blk)))
        {
            LOG_DEBUG("block %.10s delivered",
                      get_hex(blk_hash).c_str());
            part_parent_size += blk->get_parent_hashes().size();
            part_delivered++;
            delivered++;
        }
        else
        {
            LOG_WARN("dropping invalid block");
        }

        bool res = true;
        auto it = blk_delivery_waiting.find(blk_hash);
        if (it != blk_delivery_waiting.end())
        {
            auto &pm = it->second;
            if (valid)
            {
                pm.elapsed.stop(false);
                auto sec = pm.elapsed.elapsed_sec;
                part_delivery_time += sec;
                part_delivery_time_min = std::min(part_delivery_time_min, sec);
                part_delivery_time_max = std::max(part_delivery_time_max, sec);

                pm.resolve(blk);
            }
            else
            {
                pm.reject(blk);
                res = false;
            }
            blk_delivery_waiting.erase(it);
        }
        return res;
    }

    promise_t HotStuffBase::async_fetch_blk(const uint256_t &blk_hash,
                                            const PeerId *replica,
                                            bool fetch_now)
    {
        if (storage->is_blk_fetched(blk_hash))
            return promise_t([this, &blk_hash](promise_t pm)
                             { pm.resolve(storage->find_blk(blk_hash)); });
        auto it = blk_fetch_waiting.find(blk_hash);
        if (it == blk_fetch_waiting.end())
        {
#ifdef HOTSTUFF_BLK_PROFILE
            blk_profiler.rec_tx(blk_hash, false);
#endif
            it = blk_fetch_waiting.insert(
                                      std::make_pair(
                                          blk_hash,
                                          BlockFetchContext(blk_hash, this)))
                     .first;
        }
        if (replica != nullptr)
            it->second.add_replica(*replica, fetch_now);
        return static_cast<promise_t &>(it->second);
    }

    promise_t HotStuffBase::async_deliver_blk(const uint256_t &blk_hash, const PeerId &replica)
    {
        if (storage->is_blk_delivered(blk_hash))
            return promise_t([this, &blk_hash](promise_t pm)
                             { pm.resolve(storage->find_blk(blk_hash)); });
        auto it = blk_delivery_waiting.find(blk_hash);
        if (it != blk_delivery_waiting.end())
            return static_cast<promise_t &>(it->second);
        BlockDeliveryContext pm{[](promise_t) {}};
        it = blk_delivery_waiting.insert(std::make_pair(blk_hash, pm)).first;
        /* otherwise the on_deliver_batch will resolve */
        async_fetch_blk(blk_hash, &replica).then([this, replica](block_t blk)
                                                 {
        /* qc_ref should be fetched */
        std::vector<promise_t> pms;
        const auto &qc = blk->get_qc();
        assert(qc);
        if (blk == get_genesis())
            pms.push_back(promise_t([](promise_t &pm){ pm.resolve(true); }));
        else
            pms.push_back(blk->verify(this, vpool));
        pms.push_back(async_fetch_blk(qc->get_obj_hash(), &replica));
        /* the parents should be delivered */
        for (const auto &phash: blk->get_parent_hashes())
            pms.push_back(async_deliver_blk(phash, replica));
        promise::all(pms).then([this, blk](const promise::values_t values) {
            auto ret = promise::any_cast<bool>(values[0]) && this->on_deliver_blk(blk);
            if (!ret)
                HOTSTUFF_LOG_WARN("verification failed during async delivery");
        }); });
        return static_cast<promise_t &>(pm);
    }

    void HotStuffBase::propose_handler(MsgPropose &&msg, const Net::conn_t &conn)
    {
        const PeerId &peer = conn->get_peer_id();
        if (peer.is_null())
            return;
        auto stream = msg.serialized;
        auto &prop = msg.proposal;
        msg.postponed_parse(this);

        /** Treat proposal in relation to its tree */
        auto msg_tree = system_trees[prop.tid];
        auto childPeers = msg_tree.get_childPeers();
        auto tree_proposer = msg_tree.get_tree().get_tree_root();

        /* A VALID proposal has to be from the respective tree's AND additionally has to be the CURRENT proposer.
        This catches the case where it is neither. */
        if (prop.proposer != tree_proposer && !is_proposer(prop.proposer))
        {
            HOTSTUFF_LOG_PROTO("PROPOSAL RECEIVED FROM NON-PROPOSER REPLICA!");
            HOTSTUFF_LOG_PROTO("Proposal from: %d but current tree's proposer is: %llu", prop.proposer, current_tree.get_tree_root());
            return;
        }

        LOG_PROTO("[PROP HANDLER] Got PROPOSAL in epoch_nr=%d, tid=%d: %s %s", prop.epoch_nr, prop.tid, std::string(prop).c_str(), std::string(*prop.blk).c_str());

        /** We can resume pending proposals assuming previous proposal triggered tree switch */
        while (!pending_proposals.empty())
        {
            auto pending_proposal = std::move(pending_proposals.front());

            if (pending_proposal.first.proposal.tid != get_tree_id())
            {
                break;
            }

            LOG_PROTO("[PROP HANDLER] Popping pending proposal: %s", std::string(pending_proposal.first.proposal).c_str());
            pending_proposals.erase(pending_proposals.begin());
            auto &pending_prop = pending_proposal.first.proposal;

            auto pending_msg_tree = system_trees[pending_prop.tid];
            auto pending_childPeers = pending_msg_tree.get_childPeers();

            // if (!pending_childPeers.empty()) {
            //     LOG_PROTO("[PROP HANDLER] Relaying pending proposal proposal to children in tid=%d", pending_prop.tid);
            //     MsgPropose relay = MsgPropose(pending_proposal.first.serialized, true);
            //     for (const PeerId &peerId : pending_childPeers) {
            //         pn.send_msg(relay, peerId);
            //     }
            // }

            block_t pending_blk = pending_prop.blk;
            if (!pending_blk)
            {
                LOG_PROTO("[PROP HANDLER] Pending block is null!");
                break;
            }

            const PeerId &pending_peer = pending_proposal.second->get_peer_id();

            promise::all(std::vector<promise_t>{
                             async_deliver_blk(pending_blk->get_hash(), pending_peer)})
                .then([this, pending_prop = std::move(pending_prop)]()
                      { on_receive_proposal(pending_prop); });
        }

        if (!childPeers.empty())
        {
            LOG_PROTO("[PROP HANDLER] Relaying current proposal to children in epoch_nr=%d, tid=%d", prop.epoch_nr, prop.tid);
            MsgPropose relay = MsgPropose(stream, true);
            for (const PeerId &peerId : childPeers)
            {
                pn.send_msg(relay, peerId);
            }
        }

        /** Edge-case: Valid tree proposer but not the current tree proposer
         * Received a valid propose from a valid proposer in their system tree (passes propose handler safety checks)
         * However, it has arrived earlier than lower height blocks that would trigger a reconfiguration into the new tree
         * Solution: wait for the system to process missing blocks and reconfigure
         */

        if (prop.tid != get_tree_id())
        {
            LOG_PROTO("[PROP HANDLER] Proposal from future tree: %s Waiting for reconfiguration to complete.", std::string(prop).c_str());
            pending_proposals.emplace_back(std::move(msg), conn);
            return;
        }

        block_t blk = prop.blk;
        if (!blk)
        {
            LOG_PROTO("[PROP HANDLER] Proposal block is null!");
            return;
        }

        promise::all(std::vector<promise_t>{
                         async_deliver_blk(blk->get_hash(), peer)})
            .then([this, prop = std::move(prop)]()
                  { on_receive_proposal(prop); });

        /** We can resume pending proposals assuming previous proposal triggered tree switch */
        while (!pending_proposals.empty())
        {
            auto pending_proposal = std::move(pending_proposals.front());

            if (pending_proposal.first.proposal.tid != get_tree_id())
            {
                return;
            }

            LOG_PROTO("[PROP HANDLER] Popping pending proposal: %s", std::string(pending_proposal.first.proposal).c_str());
            pending_proposals.erase(pending_proposals.begin());

            auto &pending_prop = pending_proposal.first.proposal;

            auto pending_msg_tree = system_trees[pending_prop.tid];
            auto pending_childPeers = pending_msg_tree.get_childPeers();

            // if (!pending_childPeers.empty()) {
            //     LOG_PROTO("[PROP HANDLER] Relaying pending proposal proposal to children in tid=%d", pending_prop.tid);
            //     MsgPropose relay = MsgPropose(pending_proposal.first.serialized, true);
            //     for (const PeerId &peerId : pending_childPeers) {
            //         pn.send_msg(relay, peerId);
            //     }
            // }

            block_t pending_blk = pending_prop.blk;
            if (!pending_blk)
            {
                LOG_PROTO("[PROP HANDLER] Pending block is null!");
                return;
            }

            const PeerId &pending_peer = pending_proposal.second->get_peer_id();

            promise::all(std::vector<promise_t>{
                             async_deliver_blk(pending_blk->get_hash(), pending_peer)})
                .then([this, pending_prop = std::move(pending_prop)]()
                      { on_receive_proposal(pending_prop); });
        }
    }

    void HotStuffBase::vote_handler(MsgVote &&msg, const Net::conn_t &conn)
    {
        struct timeval timeStart, timeEnd;
        gettimeofday(&timeStart, NULL);

        const auto &peer = conn->get_peer_id();
        if (peer.is_null())
            return;
        msg.postponed_parse(this);
        // HOTSTUFF_LOG_PROTO("received vote");

        /** Treat vote in relation to its tree */
        auto msg_epoch_nr = msg.vote.epoch_nr;
        auto msg_tree_id = msg.vote.tid;

        // TODO change the underneath code
        auto msg_tree = system_trees[msg_tree_id];
        auto parentPeer = msg_tree.get_parentPeer();
        auto childPeers = msg_tree.get_childPeers();
        auto numberOfChildren = msg_tree.get_numberOfChildren();
        auto tree_proposer = msg_tree.get_tree().get_tree_root();

        HOTSTUFF_LOG_PROTO("[VOTE HANDLER] Received VOTE message in epoch_nr=%d, tid=%d from ReplicaId %d", msg_epoch_nr, msg_tree_id, peer_id_map.at(peer));

        if (id == tree_proposer && !piped_queue.empty() && std::find(piped_queue.begin(), piped_queue.end(), msg.vote.blk_hash) != piped_queue.end())
        {
            HOTSTUFF_LOG_PROTO("piped block");
            block_t blk = storage->find_blk(msg.vote.blk_hash);
            if (!blk->piped_delivered)
            {
                process_block(blk, false, msg_tree_id, msg_epoch_nr);
                blk->piped_delivered = true;
                HOTSTUFF_LOG_PROTO("Normalized piped block");
            }
        }

        block_t blk = get_potentially_not_delivered_blk(msg.vote.blk_hash);
        // AFTER HERE, BLOCK MUST BE DELIVERED ALREADY!!

        if (blk->self_qc == nullptr)
        {
            blk->self_qc = create_quorum_cert(blk->get_hash());
            part_cert_bt part = create_part_cert(*priv_key, blk->get_hash());
            blk->self_qc->add_part(config, id, *part);

            std::cout << "create cert: " << msg.vote.blk_hash.to_hex() << " " << &blk->self_qc << std::endl;
        }

        std::cout << "vote handler: received vote for block " << msg.vote.blk_hash.to_hex() << " " << std::endl;
        // HOTSTUFF_LOG_PROTO("vote handler: Majority Necessary: %d | Total Replicas: %d", config.nmajority, config.nreplicas);

        if (blk->self_qc->has_n(config.nmajority))
        {
            HOTSTUFF_LOG_PROTO("bye vote handler");
            // std::cout << "bye vote handler: " << msg.vote.blk_hash.to_hex() << " " << &blk->self_qc << std::endl;
            /*if (id == get_pace_maker()->get_proposer()) {
                gettimeofday(&timeEnd, NULL);
                long usec = ((timeEnd.tv_sec - timeStart.tv_sec) * 1000000 + timeEnd.tv_usec - timeStart.tv_usec);
                stats[blk->hash] = stats[blk->hash] + usec;
                HOTSTUFF_LOG_PROTO("result: %s, %s ", blk->hash.to_hex().c_str(), std::to_string(stats[blk->parent_hashes[0]]).c_str());
            }*/
            return;
        }

        if (id != tree_proposer)
        {
            auto &cert = blk->self_qc;

            if (cert->has_n(numberOfChildren + 1))
            {
                return;
            }

            cert->add_part(config, msg.vote.voter, *msg.vote.cert);

            if (!cert->has_n(numberOfChildren + 1))
            {
                HOTSTUFF_LOG_PROTO("[VOTE HANDLER] Don't have all my children's votes, returning...");
                return;
            }

            HOTSTUFF_LOG_PROTO("[VOTE HANDLER] Got all children votes (%d) + my own! Total: %d", numberOfChildren, cert->get_sigs_n());
            std::cout << " got enough votes: " << msg.vote.blk_hash.to_hex().c_str() << std::endl;

            if (!piped_queue.empty())
            {

                for (auto hash = std::begin(piped_queue); hash != std::end(piped_queue); ++hash)
                {
                    block_t b = storage->find_blk(*hash);
                    if (b->delivered && b->qc->has_n(config.nmajority))
                    {
                        piped_queue.erase(hash);
                        HOTSTUFF_LOG_PROTO("Confirm Piped block %.10s", b->hash.to_hex().c_str());
                        HOTSTUFF_LOG_PROTO("[PIPELINING] Removed piped block from queue! Piped queue size now: %d", piped_queue.size());
                    }
                }

                if (blk->hash == piped_queue.front())
                {
                    piped_queue.pop_front();
                    HOTSTUFF_LOG_PROTO("Reset Piped block %.10s", blk->hash.to_hex().c_str());
                    HOTSTUFF_LOG_PROTO("[PIPELINING] Removed piped block from queue! Piped queue size now: %d", piped_queue.size());
                }
                else
                {
                    HOTSTUFF_LOG_PROTO("Failed resetting piped block, wasn't front!!!");
                }
            }

            cert->compute();
            if (!cert->verify(config))
            {
                HOTSTUFF_LOG_PROTO("Error, Invalid Sig!!!");
                return;
            }

            std::cout << " send relay message: " << msg.vote.blk_hash.to_hex().c_str() << std::endl;
            if (!parentPeer.is_null())
            {

                HOTSTUFF_LOG_PROTO("[VOTE HANDLER] Got enough votes, sending VOTE-RELAY in epoch_nr=%d, tid=%d to ReplicaId %d with a cert of size %d", msg_epoch_nr, msg_tree_id, peer_id_map.at(parentPeer), cert->get_sigs_n());

                pn.send_msg(MsgRelay(VoteRelay(msg_epoch_nr, msg_tree_id, msg.vote.blk_hash, blk->self_qc->clone(), this)), parentPeer);
            }
            async_deliver_blk(msg.vote.blk_hash, peer);

            return;
        }

        // auto &vote = msg.vote;
        RcObj<Vote> v(new Vote(std::move(msg.vote)));
        promise::all(std::vector<promise_t>{
                         async_deliver_blk(v->blk_hash, peer),
                         id == tree_proposer ? v->verify(vpool) : promise_t([](promise_t &pm)
                                                                            { pm.resolve(true); }),
                     })
            .then([this, blk, v = std::move(v), timeStart, tree_proposer](const promise::values_t values)
                  {
                      if (!promise::any_cast<bool>(values[1]))
                          LOG_WARN("invalid vote from %d", v->voter);
                      auto &cert = blk->self_qc;
                      // struct timeval timeEnd;

                      cert->add_part(config, v->voter, *v->cert);
                      if (cert != nullptr && cert->get_obj_hash() == blk->get_hash())
                      {
                          if (cert->has_n(config.nmajority))
                          {
                              cert->compute();
                              if (id != tree_proposer && !cert->verify(config))
                              {
                                  throw std::runtime_error("Invalid Sigs in intermediate signature!");
                              }
                              // HOTSTUFF_LOG_PROTO("Majority reached, go");
                              update_hqc(blk, cert);
                              on_qc_finish(blk);
                          }
                      }

                      /*if (id == get_pace_maker()->get_proposer()) {
                          gettimeofday(&timeEnd, NULL);
                          long usec = ((timeEnd.tv_sec - timeStart.tv_sec) * 1000000 + timeEnd.tv_usec - timeStart.tv_usec);
                          std::cout << usec << " a:a " << stats[blk->hash] << std::endl;
                          stats[blk->hash] = stats[blk->hash] + usec;
                          std::cout << usec << " b:b " << stats[blk->hash] << std::endl;
                      }*/

                      /*struct timeval timeEnd;
                      gettimeofday(&timeEnd, NULL);

                      std::cout << "Vote handling cost partially threaded: "
                                << ((timeEnd.tv_sec - timeStart.tv_sec) * 1000000 + timeEnd.tv_usec - timeStart.tv_usec)
                                << " us to execute."
                                << std::endl;*/ });

        /*
        gettimeofday(&timeEnd, NULL);

        std::cout << "Vote handling cost: "
                  << ((timeEnd.tv_sec - timeStart.tv_sec) * 1000000 + timeEnd.tv_usec - timeStart.tv_usec)
                  << " us to execute."
                  << std::endl;*/
    }

    void HotStuffBase::vote_relay_handler(MsgRelay &&msg, const Net::conn_t &conn)
    {
        struct timeval timeStart, timeEnd;
        gettimeofday(&timeStart, NULL);

        const auto &peer = conn->get_peer_id();
        if (peer.is_null())
            return;
        msg.postponed_parse(this);
        // std::cout << "vote relay handler: " << msg.vote.blk_hash.to_hex() << std::endl;

        /** Treat vote relay in relation to its tree */
        auto msg_epoch_nr = msg.vote.epoch_nr;
        auto msg_tree_id = msg.vote.tid;

        // TODO: change this underneath
        auto msg_tree = system_trees[msg_tree_id];
        auto parentPeer = msg_tree.get_parentPeer();
        auto childPeers = msg_tree.get_childPeers();
        auto numberOfChildren = msg_tree.get_numberOfChildren();
        auto tree_proposer = msg_tree.get_tree().get_tree_root();

        HOTSTUFF_LOG_PROTO("[RELAY HANDLER] Received VOTE-RELAY message in epoch_nr=%d, tid=%d from ReplicaId %d with a cert of size %d", msg_epoch_nr, msg_tree_id, peer_id_map.at(peer), msg.vote.cert->get_sigs_n());

        if (id == tree_proposer && !piped_queue.empty() && std::find(piped_queue.begin(), piped_queue.end(), msg.vote.blk_hash) != piped_queue.end())
        {
            HOTSTUFF_LOG_PROTO("piped block");
            block_t blk = storage->find_blk(msg.vote.blk_hash);
            if (!blk->piped_delivered)
            {
                process_block(blk, false, msg_tree_id, msg_epoch_nr);
                blk->piped_delivered = true;
                HOTSTUFF_LOG_PROTO("Normalized piped block");
            }
        }

        block_t blk = get_potentially_not_delivered_blk(msg.vote.blk_hash);
        // AFTER HERE, BLOCK MUST BE DELIVERED ALREADY!!

        if (blk->self_qc == nullptr)
        {
            blk->self_qc = create_quorum_cert(blk->get_hash());
            part_cert_bt part = create_part_cert(*priv_key, blk->get_hash());
            blk->self_qc->add_part(config, id, *part);

            std::cout << "create cert: " << msg.vote.blk_hash.to_hex() << " " << &blk->self_qc << std::endl;
        }

        if (blk->self_qc->has_n(config.nmajority))
        {
            std::cout << "bye vote relay handler: " << msg.vote.blk_hash.to_hex() << " " << &blk->self_qc << std::endl;
            if (id == tree_proposer && blk->hash == piped_queue.front())
            {
                piped_queue.pop_front();
                HOTSTUFF_LOG_PROTO("Reset Piped block %.10s", blk->hash.to_hex().c_str());
                HOTSTUFF_LOG_PROTO("[PIPELINING] Removed piped block from queue! Piped queue size now: %d", piped_queue.size());

                auto curr_blk = blk;
                if (!rdy_queue.empty())
                {
                    HOTSTUFF_LOG_PROTO("Resolving rdy queue (case 1)");

                    bool frontsMatch = true;
                    while (frontsMatch && !rdy_queue.empty())
                    {
                        if (rdy_queue.front() == piped_queue.front())
                        {
                            HOTSTUFF_LOG_PROTO("Resolved block in rdy queue %.10s", rdy_queue.front().to_hex().c_str());
                            rdy_queue.pop_front();
                            piped_queue.pop_front();
                            HOTSTUFF_LOG_PROTO("[PIPELINING] Removed piped block from queue! Piped queue size now: %d", piped_queue.size());

                            update_hqc(blk, blk->self_qc);
                            on_qc_finish(blk);
                        }
                        else
                        {
                            frontsMatch = false;
                        }
                    }

                    bool foundChildren;
                    if (rdy_queue.empty())
                    {
                        foundChildren = false; // Job is done
                    }
                    else
                    {
                        foundChildren = true;
                    }

                    while (foundChildren)
                    {
                        foundChildren = false;
                        for (const auto &hash : rdy_queue)
                        {
                            block_t rdy_blk = storage->find_blk(hash);
                            if (rdy_blk->get_parent_hashes()[0] == curr_blk->hash)
                            {
                                HOTSTUFF_LOG_PROTO("Resolved block in rdy queue %.10s", hash.to_hex().c_str());
                                rdy_queue.erase(std::find(rdy_queue.begin(), rdy_queue.end(), hash));
                                piped_queue.erase(std::find(piped_queue.begin(), piped_queue.end(), hash));
                                HOTSTUFF_LOG_PROTO("[PIPELINING] Removed piped block from queue! Piped queue size now: %d", piped_queue.size());

                                update_hqc(rdy_blk, rdy_blk->self_qc);
                                on_qc_finish(rdy_blk);
                                foundChildren = true;
                                curr_blk = rdy_blk;
                                break;
                            }
                        }
                    }
                }
            }

            /*if (id == get_pace_maker()->get_proposer()) {
                gettimeofday(&timeEnd, NULL);
                long usec = ((timeEnd.tv_sec - timeStart.tv_sec) * 1000000 + timeEnd.tv_usec - timeStart.tv_usec);
                stats[blk->hash] = stats[blk->hash] + usec;
                HOTSTUFF_LOG_PROTO("result: %s, %s ", blk->hash.to_hex().c_str(), std::to_string(stats[blk->parent_hashes[0]]).c_str());
            }*/

            return;
        }

        std::cout << "vote relay handler: " << msg.vote.blk_hash.to_hex() << " " << std::endl;

        RcObj<VoteRelay> v(new VoteRelay(std::move(msg.vote)));
        promise::all(std::vector<promise_t>{
                         async_deliver_blk(v->blk_hash, peer),
                         v->cert->verify(config, vpool),
                     })
            .then([this, blk, v = std::move(v), timeStart, tree_proposer, parentPeer, numberOfChildren, msg_tree_id, msg_epoch_nr](const promise::values_t &values)
                  {
        struct timeval timeEnd;

        if (!promise::any_cast<bool>(values[1]))
            LOG_WARN ("invalid vote-relay");
        auto &cert = blk->self_qc;

        if (cert != nullptr && cert->get_obj_hash() == blk->get_hash() && !cert->has_n(config.nmajority)) {


            if (id != tree_proposer && cert->has_n(numberOfChildren + 1))
            {
                return;
            }

            cert->merge_quorum(*v->cert);

            if (id != tree_proposer) {
                if (!cert->has_n(numberOfChildren + 1)) return;
                cert->compute();
                if (!cert->verify(config)) {
                    throw std::runtime_error("Invalid Sigs in intermediate signature!");
                }
                std::cout << "Send Vote Relay: " << v->blk_hash.to_hex() << std::endl;
                if(!parentPeer.is_null()) {

                    HOTSTUFF_LOG_PROTO("[RELAY HANDLER] Sending VOTE-RELAY in epoch_nr=%d, tid=%d to ReplicaId %d with a cert of size %d", msg_epoch_nr, msg_tree_id, peer_id_map.at(parentPeer), cert->get_sigs_n());

                    pn.send_msg(MsgRelay(VoteRelay(msg_epoch_nr, msg_tree_id, v->blk_hash, cert.get()->clone(), this)), parentPeer);
                }

                return;
            }

            //HOTSTUFF_LOG_PROTO("got %s", std::string(*v).c_str());
            HOTSTUFF_LOG_PROTO("[RELAY HANDLER] Checking if our certificate has a majority of %llu", config.nmajority);

            if (!cert->has_n(config.nmajority)) {
                HOTSTUFF_LOG_PROTO("[RELAY HANDLER] No majority in cert! Current: %llu | Necessary: %llu", cert->get_sigs_n(), config.nmajority);
                /*if (id == get_pace_maker()->get_proposer()) {
                    gettimeofday(&timeEnd, NULL);
                    long usec = ((timeEnd.tv_sec - timeStart.tv_sec) * 1000000 + timeEnd.tv_usec - timeStart.tv_usec);
                    std::cout << usec << " a:a " << stats[blk->hash] << std::endl;
                    stats[blk->hash] = stats[blk->hash] + usec;
                    std::cout << usec << " b:b " << stats[blk->hash] << std::endl;
                }*/
                return;
            }

            HOTSTUFF_LOG_PROTO("[RELAY HANDLER] Majority in cert reached! Current: %llu | Necessary: %llu", cert->get_sigs_n(), config.nmajority);

            cert->compute();
            if (!cert->verify(config)) {
                HOTSTUFF_LOG_PROTO("Error, Invalid Sig!!!");
                return;
            }

            if (!piped_queue.empty()) {
                if (blk->hash == piped_queue.front()) {
                    piped_queue.pop_front();
                    HOTSTUFF_LOG_PROTO("Reset Piped block %.10s", blk->hash.to_hex().c_str());
                    HOTSTUFF_LOG_PROTO("[PIPELINING] Removed piped block from queue! Piped queue size now: %d", piped_queue.size());

                    std::cout << "go to town: " << std::endl;

                    update_hqc(blk, cert);
                    on_qc_finish(blk);

                    auto curr_blk = blk;
                    if (!rdy_queue.empty()) {
                        HOTSTUFF_LOG_PROTO("Resolving rdy queue (case 2)");

                        bool frontsMatch = true;
                        while(frontsMatch && !rdy_queue.empty()) {
                            if(rdy_queue.front() == piped_queue.front()) {
                                HOTSTUFF_LOG_PROTO("Resolved block in rdy queue %.10s", rdy_queue.front().to_hex().c_str());
                                rdy_queue.pop_front();
                                piped_queue.pop_front();
                                HOTSTUFF_LOG_PROTO("[PIPELINING] Removed piped block from queue! Piped queue size now: %d", piped_queue.size());

                                update_hqc(blk, blk->self_qc);
                                on_qc_finish(blk);
                            }
                            else {
                                frontsMatch = false;
                            }
                        }

                        bool foundChildren;
                        if(rdy_queue.empty()) {
                            foundChildren = false; // Job is done
                        }
                        else {
                            foundChildren = true;
                        }

                        while (foundChildren) {
                            foundChildren = false;
                            for (const auto &hash : rdy_queue) {
                                block_t rdy_blk = storage->find_blk(hash);
                                if (rdy_blk->get_parent_hashes()[0] == curr_blk->hash) {
                                    HOTSTUFF_LOG_PROTO("Resolved block in rdy queue %.10s", hash.to_hex().c_str());
                                    rdy_queue.erase(std::find(rdy_queue.begin(), rdy_queue.end(), hash));
                                    piped_queue.erase(std::find(piped_queue.begin(), piped_queue.end(), hash));
                                    HOTSTUFF_LOG_PROTO("[PIPELINING] Removed piped block from queue! Piped queue size now: %d", piped_queue.size());

                                    update_hqc(rdy_blk, rdy_blk->self_qc);
                                    on_qc_finish(rdy_blk);
                                    foundChildren = true;
                                    curr_blk = rdy_blk;
                                    break;
                                }
                            }
                        }
                    }
                }
                else {
                    auto place = std::find(piped_queue.begin(), piped_queue.end(), blk->hash);
                    if (place != piped_queue.end()) {
                        HOTSTUFF_LOG_PROTO("Failed resetting piped block, wasn't front! Adding to rdy_queue %.10s", blk->hash.to_hex().c_str());

                        std::string piped_queue_str = "";
                        for(auto &hash : piped_queue) {
                            piped_queue_str += "|" + hash.to_hex().substr(0, 10) + "| ";
                        }

                        HOTSTUFF_LOG_PROTO("Piped queue has size %d: Front-> %s", piped_queue.size(), piped_queue_str.c_str());

                        rdy_queue.push_back(blk->hash);

                        std::string rdy_queue_str = "";
                        for(auto &hash : rdy_queue) {
                            rdy_queue_str += "|" + hash.to_hex().substr(0, 10) + "| ";
                        }

                        HOTSTUFF_LOG_PROTO("Rdy queue is now: Front-> %s", rdy_queue_str.c_str());

                        // Don't finish this block until the previous one was finished.
                        return;
                    }
                    else {
                        std::cout << "go to town: " << std::endl;

                        update_hqc(blk, cert);
                        on_qc_finish(blk);

                        auto curr_blk = blk;
                        if (!rdy_queue.empty()) {
                            HOTSTUFF_LOG_PROTO("Resolving rdy queue (case 3)");

                           bool frontsMatch = true;
                            while(frontsMatch && !rdy_queue.empty()) {
                                if(rdy_queue.front() == piped_queue.front()) {
                                    HOTSTUFF_LOG_PROTO("Resolved block in rdy queue %.10s", rdy_queue.front().to_hex().c_str());
                                    rdy_queue.pop_front();
                                    piped_queue.pop_front();
                                    HOTSTUFF_LOG_PROTO("[PIPELINING] Removed piped block from queue! Piped queue size now: %d", piped_queue.size());

                                    update_hqc(blk, blk->self_qc);
                                    on_qc_finish(blk);
                                }
                                else {
                                    frontsMatch = false;
                                }
                            }

                            bool foundChildren;
                            if(rdy_queue.empty()) {
                                foundChildren = false; // Job is done
                            }
                            else {
                                foundChildren = true;
                            }

                            while (foundChildren) {
                                foundChildren = false;
                                for (const auto &hash : rdy_queue) {
                                    block_t rdy_blk = storage->find_blk(hash);
                                    if (rdy_blk->get_parent_hashes()[0] == curr_blk->hash) {
                                        HOTSTUFF_LOG_PROTO("Resolved block in rdy queue %.10s", hash.to_hex().c_str());
                                        rdy_queue.erase(std::find(rdy_queue.begin(), rdy_queue.end(), hash));
                                        piped_queue.erase(std::find(piped_queue.begin(), piped_queue.end(), hash));
                                        HOTSTUFF_LOG_PROTO("[PIPELINING] Removed piped block from queue! Piped queue size now: %d", piped_queue.size());

                                        update_hqc(rdy_blk, rdy_blk->self_qc);
                                        on_qc_finish(rdy_blk);
                                        foundChildren = true;
                                        curr_blk = rdy_blk;
                                        break;
                                    }
                                }
                            }
                        }
                    }
                }
            }
            else
            {
                std::cout << "go to town: " << std::endl;

                update_hqc(blk, cert);
                on_qc_finish(blk);

                auto curr_blk = blk;
                if (!rdy_queue.empty()) {
                    HOTSTUFF_LOG_PROTO("Resolving rdy queue (case 4)");

                    
                    bool frontsMatch = true;
                    while(frontsMatch && !rdy_queue.empty()) {
                        if(rdy_queue.front() == piped_queue.front()) {
                            HOTSTUFF_LOG_PROTO("Resolved block in rdy queue %.10s", rdy_queue.front().to_hex().c_str());
                            rdy_queue.pop_front();
                            piped_queue.pop_front();
                            HOTSTUFF_LOG_PROTO("[PIPELINING] Removed piped block from queue! Piped queue size now: %d", piped_queue.size());

                            update_hqc(blk, blk->self_qc);
                            on_qc_finish(blk);
                        }
                        else {
                            frontsMatch = false;
                        }
                    }

                    bool foundChildren;
                    if(rdy_queue.empty()) {
                        foundChildren = false; // Job is done
                    }
                    else {
                        foundChildren = true;
                    }

                    while (foundChildren) {
                        foundChildren = false;
                        for (const auto &hash : rdy_queue) {
                            block_t rdy_blk = storage->find_blk(hash);
                            if (rdy_blk->get_parent_hashes()[0] == curr_blk->hash) {
                                HOTSTUFF_LOG_PROTO("Resolved block in rdy queue %.10s", hash.to_hex().c_str());
                                rdy_queue.erase(std::find(rdy_queue.begin(), rdy_queue.end(), hash));
                                piped_queue.erase(std::find(piped_queue.begin(), piped_queue.end(), hash));
                                HOTSTUFF_LOG_PROTO("[PIPELINING] Removed piped block from queue! Piped queue size now: %d", piped_queue.size());

                                update_hqc(rdy_blk, rdy_blk->self_qc);
                                on_qc_finish(rdy_blk);
                                foundChildren = true;
                                curr_blk = rdy_blk;
                                break;
                            }
                        }
                    }
                }
            }
            
            /*if (id == get_pace_maker()->get_proposer()) {
                gettimeofday(&timeEnd, NULL);
                long usec = ((timeEnd.tv_sec - timeStart.tv_sec) * 1000000 + timeEnd.tv_usec - timeStart.tv_usec);
                stats[blk->hash] = stats[blk->hash] + usec;
                HOTSTUFF_LOG_PROTO("result: %s, %s ", blk->hash.to_hex().c_str(), std::to_string(stats[blk->hash]).c_str());
            }*/

            /*
            struct timeval timeEnd;
            gettimeofday(&timeEnd, NULL);

            std::cout << "Vote relay handling cost partially threaded: "
                      << ((timeEnd.tv_sec - timeStart.tv_sec) * 1000000 + timeEnd.tv_usec - timeStart.tv_usec)
                      << " us to execute."
                      << std::endl;*/
        }
        /*else {
            if (id == get_pace_maker()->get_proposer()) {
                gettimeofday(&timeEnd, NULL);
                long usec = ((timeEnd.tv_sec - timeStart.tv_sec) * 1000000 + timeEnd.tv_usec - timeStart.tv_usec);
                stats[blk->hash] = stats[blk->hash] + usec;
                HOTSTUFF_LOG_PROTO("result: %s, %s ", blk->hash.to_hex().c_str(), std::to_string(stats[blk->parent_hashes[0]]).c_str());
            }
        }*/ });

        /*gettimeofday(&timeEnd, NULL);

        std::cout << "Vote relay handling cost: "
                  << ((timeEnd.tv_sec - timeStart.tv_sec) * 1000000 + timeEnd.tv_usec - timeStart.tv_usec)
                  << " us to execute."
                  << std::endl;*/
    }

    void HotStuffBase::req_blk_handler(MsgReqBlock &&msg, const Net::conn_t &conn)
    {
        const PeerId replica = conn->get_peer_id();
        if (replica.is_null())
            return;
        auto &blk_hashes = msg.blk_hashes;
        std::vector<promise_t> pms;
        for (const auto &h : blk_hashes)
            pms.push_back(async_fetch_blk(h, nullptr));
        promise::all(pms).then([replica, this](const promise::values_t values)
                               {
        std::vector<block_t> blks;
        for (auto &v: values)
        {
            auto blk = promise::any_cast<block_t>(v);
            blks.push_back(blk);
        }
        pn.send_msg(MsgRespBlock(blks), replica); });
    }

    void HotStuffBase::resp_blk_handler(MsgRespBlock &&msg, const Net::conn_t &)
    {
        msg.postponed_parse(this);
        for (const auto &blk : msg.blks)
            if (blk)
                on_fetch_blk(blk);
    }

    bool HotStuffBase::conn_handler(const salticidae::ConnPool::conn_t &conn, bool connected)
    {
        if (connected)
        {
            auto cert = conn->get_peer_cert();
            // SALTICIDAE_LOG_INFO("%s", salticidae::get_hash(cert->get_der()).to_hex().c_str());
            return (!cert) || valid_tls_certs.count(salticidae::get_hash(cert->get_der()));
        }
        return true;
    }

    void HotStuffBase::print_stat() const
    {
        LOG_INFO("===== begin stats =====");
        LOG_INFO("-------- queues -------");
        LOG_INFO("blk_fetch_waiting: %lu", blk_fetch_waiting.size());
        LOG_INFO("blk_delivery_waiting: %lu", blk_delivery_waiting.size());
        LOG_INFO("decision_waiting: %lu", decision_waiting.size());
        LOG_INFO("-------- misc ---------");
        LOG_INFO("fetched: %lu", fetched);
        LOG_INFO("delivered: %lu", delivered);
        LOG_INFO("cmd_cache: %lu", storage->get_cmd_cache_size());
        LOG_INFO("blk_cache: %lu", storage->get_blk_cache_size());
        LOG_INFO("------ misc (10s) -----");
        LOG_INFO("fetched: %lu", part_fetched);
        LOG_INFO("delivered: %lu", part_delivered);
        LOG_INFO("decided: %lu", part_decided);
        LOG_INFO("gened: %lu", part_gened);
        LOG_INFO("avg. parent_size: %.3f",
                 part_delivered ? part_parent_size / double(part_delivered) : 0);
        LOG_INFO("delivery time: %.3f avg, %.3f min, %.3f max",
                 part_delivered ? part_delivery_time / double(part_delivered) : 0,
                 part_delivery_time_min == double_inf ? 0 : part_delivery_time_min,
                 part_delivery_time_max);

        part_parent_size = 0;
        part_fetched = 0;
        part_delivered = 0;
        part_decided = 0;
        part_gened = 0;
        part_delivery_time = 0;
        part_delivery_time_min = double_inf;
        part_delivery_time_max = 0;
#ifdef HOTSTUFF_MSG_STAT
        LOG_INFO("--- replica msg. (10s) ---");
        size_t _nsent = 0;
        size_t _nrecv = 0;
        for (const auto &replica : peers)
        {
            try
            {
                auto conn = pn.get_peer_conn(replica);
                if (conn == nullptr)
                    continue;
                size_t ns = conn->get_nsent();
                size_t nr = conn->get_nrecv();
                size_t nsb = conn->get_nsentb();
                size_t nrb = conn->get_nrecvb();
                conn->clear_msgstat();
                // LOG_INFO("%s: %u(%u), %u(%u), %u", get_hex10(replica).c_str(), ns, nsb, nr, nrb, part_fetched_replica[replica]);
                _nsent += ns;
                _nrecv += nr;
                part_fetched_replica[replica] = 0;
            }
            catch (...)
            {
            }
        }
        nsent += _nsent;
        nrecv += _nrecv;
        LOG_INFO("sent: %lu", _nsent);
        LOG_INFO("recv: %lu", _nrecv);
        LOG_INFO("--- replica msg. total ---");
        LOG_INFO("sent: %lu", nsent);
        LOG_INFO("recv: %lu", nrecv);
#endif
        LOG_INFO("====== end stats ======");
    }

    HotStuffBase::HotStuffBase(uint32_t blk_size,
                               ReplicaID rid,
                               privkey_bt &&priv_key,
                               NetAddr listen_addr,
                               pacemaker_bt pmaker,
                               EventContext ec,
                               size_t nworker,
                               const Net::Config &netconfig) : HotStuffCore(rid, std::move(priv_key)),
                                                               listen_addr(listen_addr),
                                                               blk_size(blk_size),
                                                               ec(ec),
                                                               tcall(ec),
                                                               vpool(ec, nworker),
                                                               pn(ec, netconfig),
                                                               pmaker(std::move(pmaker)),

                                                               fetched(0), delivered(0),
                                                               nsent(0), nrecv(0),
                                                               part_parent_size(0),
                                                               part_fetched(0),
                                                               part_delivered(0),
                                                               part_decided(0),
                                                               part_gened(0),
                                                               part_delivery_time(0),
                                                               part_delivery_time_min(double_inf),
                                                               part_delivery_time_max(0)
    {
        /* register the handlers for msg from replicas */
        pn.reg_handler(salticidae::generic_bind(&HotStuffBase::propose_handler, this, _1, _2));
        pn.reg_handler(salticidae::generic_bind(&HotStuffBase::vote_handler, this, _1, _2));
        pn.reg_handler(salticidae::generic_bind(&HotStuffBase::req_blk_handler, this, _1, _2));
        pn.reg_handler(salticidae::generic_bind(&HotStuffBase::resp_blk_handler, this, _1, _2));
        pn.reg_handler(salticidae::generic_bind(&HotStuffBase::vote_relay_handler, this, _1, _2));
        pn.reg_conn_handler(salticidae::generic_bind(&HotStuffBase::conn_handler, this, _1, _2));
        pn.start();
        pn.listen(listen_addr);
    }

    void HotStuffBase::do_broadcast_proposal(const Proposal &prop)
    {
        // HOTSTUFF_LOG_PROTO("Broadcasting proposal of size %llu bytes.", sizeof(prop));
        auto childPeers = current_tree_network.get_childPeers();
        pn.multicast_msg(MsgPropose(prop), std::vector(childPeers.begin(), childPeers.end()));
    }

    void HotStuffBase::inc_time(bool force)
    {
        pmaker->inc_time(force);
    }

    bool HotStuffBase::is_proposer(int rid)
    {
        return rid == pmaker->get_proposer();
    }

    void HotStuffBase::proposer_base_deliver(const block_t &blk)
    {
        this->on_deliver_blk(blk);
    }

    void HotStuffBase::do_vote(Proposal prop, const Vote &vote)
    {
        pmaker->beat_resp(prop.proposer).then([this, vote, prop](ReplicaID proposer)
                                              {

        /** Treat vote to proposal in relation to its tree */
        auto msg_epoch_nr = prop.epoch_nr;
        auto msg_tree_id = prop.tid;


        auto msg_tree = system_trees[msg_tree_id];
        auto parentPeer = msg_tree.get_parentPeer();
        auto childPeers = msg_tree.get_childPeers();
        auto tree_proposer = msg_tree.get_tree().get_tree_root();

        if (tree_proposer == get_id())
        {
            return;
        }

        if (childPeers.empty()) {
            //HOTSTUFF_LOG_PROTO("send vote");
            if(!parentPeer.is_null())  {

                HOTSTUFF_LOG_PROTO("[CONSENSUS] Sending VOTE in epoch_nr=%d, tid=%d to ReplicaId %d for block %s",msg_epoch_nr ,msg_tree_id, peer_id_map.at(parentPeer), std::string(*prop.blk).c_str());
                pn.send_msg(MsgVote(vote), parentPeer);
            }
        } else {
            block_t blk = get_delivered_blk(vote.blk_hash);
            if (blk->self_qc == nullptr)
            {
                blk->self_qc = create_quorum_cert(prop.blk->get_hash());
                blk->self_qc->add_part(config, vote.voter, *vote.cert);
            }
        } });
    }

    void HotStuffBase::do_consensus(const block_t &blk)
    {
        pmaker->on_consensus(blk);
    }

    void HotStuffBase::do_decide(Finality &&fin)
    {
        part_decided++;
        state_machine_execute(fin);
        auto it = decision_waiting.find(fin.cmd_hash);
        if (it != decision_waiting.end())
        {
            it->second(std::move(fin));
            decision_waiting.erase(it);
        }
        else
        {
            decision_made[fin.cmd_hash] = fin.cmd_height;
        }
    }

    /**
     * Get the current tree id. Used for proposals.
     */
    uint32_t HotStuffBase::get_tree_id()
    {
        return current_tree.get_tid();
    }

    /**
     * Get the current tree id. Used for proposals.
     */
    uint32_t HotStuffBase::get_cur_epoch_nr()
    {
        return cur_epoch.get_epoch_num();
    }

    HotStuffBase::~HotStuffBase() {}

    void HotStuffBase::tree_config(std::vector<std::tuple<NetAddr, pubkey_bt, uint256_t>> &&replicas)
    {

        LOG_PROTO("\n=========================== READING DEFAULT EPOCH =================================\n");

        std::vector<TreeNetwork> default_trees;

        /** Default algorithm will create 1 tree for each replica.
         * Each replica will be the proposer of its own tree.
         * Trees are filled from left to right with the replica vector.
         * Replica vector is a vector from smallest to largest id.
         * Each subsequent tree will be shifted by 1 to the left.
         */
        if (config.treegen_algo == "default")
        {
            size_t system_size = replicas.size();

            /* A tree for each replica, where they are the proposer */
            for (size_t tid = 0; tid < system_size; tid++)
            {

                std::vector<uint32_t> new_tree_array;
                for (int i = 0; i < system_size; ++i)
                {
                    new_tree_array.push_back(((i + tid) % system_size));
                }

                default_trees.push_back(TreeNetwork(Tree(tid, config.fanout, config.async_blocks, new_tree_array),
                                                    std::move(replicas), id));

                // This algorithm assumes constant fanout and pipeline-stretch
                // system_trees[tid] = new_tree;
                // trees.push_back(new_tree);
            }

            // auto new_epoch = Epoch(0, trees);

            // LOG_PROTO("DELIVERING EPOCH %d", new_epoch.get_epoch_num());
        }

        /** File algorithm will obtain the trees from a file.
         * File trees are formatted so:
         * - 1st arg: fan:m where m is the tree's fanout
         * - 2nd arg: pipe:k where k is the tree's pipeline-stretch
         * - Remainder of line: sequential ids of tree's replicas
         *
         * Each line is a new TID
         * TIDs are attributted in order from 0 to N
         */
        else if (config.treegen_algo == "file")
        {
            std::ifstream file(config.treegen_fpath);
            std::string line;
            size_t tid = 0;

            if (!file.is_open())
            {
                std::string str = "tree_config: Provided treegen file path is invalid! Failed to open file " + config.treegen_fpath;
                throw std::runtime_error(str);
            }

            while (std::getline(file, line))
            {
                std::istringstream iss(line);
                std::string tmp;
                std::string delimiter = ":";
                std::string token;
                std::vector<uint32_t> new_tree_array;
                uint32_t replica_id;
                uint8_t fanout;
                uint8_t pipe_stretch;

                /* Fanout */
                iss >> tmp;
                token = tmp.substr(0, tmp.find(delimiter));
                if (token == "fan")
                {
                    fanout = std::stoi(tmp.substr(tmp.find(delimiter) + delimiter.length()));
                }
                else
                {
                    throw std::runtime_error("tree_config: Provided treegen file has invalid tree fanout!");
                }

                /* Pipeline Stretch */
                iss >> tmp;
                token = tmp.substr(0, tmp.find(delimiter));
                if (token == "pipe")
                {
                    pipe_stretch = std::stoi(tmp.substr(tmp.find(delimiter) + delimiter.length()));
                }
                else
                {
                    throw std::runtime_error("tree_config: Provided treegen file has invalid tree pipeline-stretch!");
                }

                while (iss >> replica_id)
                {
                    new_tree_array.push_back(replica_id);
                }

                default_trees.push_back(TreeNetwork(Tree(tid, fanout, pipe_stretch, new_tree_array), std::move(replicas), id));
                tid++;
                // system_trees[tid] = TreeNetwork(Tree(tid, fanout, pipe_stretch, new_tree_array), std::move(replicas), id);
                // tid++;
            }
        }

        else
        {
            throw std::runtime_error("tree_config: Invalid tree generation algorithm!");
        }

        cur_epoch = Epoch(0, default_trees);
        system_trees = cur_epoch.get_system_trees();
    }

    // TO BE REMOVED JUST TEST
    void HotStuffBase::read_epoch_from_file(std::vector<std::tuple<NetAddr, pubkey_bt, uint256_t>> &&replicas)
    {

        std::vector<TreeNetwork> default_trees;
        std::ifstream file(config.new_epoch);
        std::string line;
        size_t tid = 0;

        if (!file.is_open())
        {
            std::string str = "tree_config: Provided treegen file path is invalid! Failed to open file " + config.new_epoch;
            throw std::runtime_error(str);
        }

        while (std::getline(file, line))
        {
            std::istringstream iss(line);
            std::string tmp;
            std::string delimiter = ":";
            std::string token;
            std::vector<uint32_t> new_tree_array;
            uint32_t replica_id;
            uint8_t fanout;
            uint8_t pipe_stretch;

            /* Fanout */
            iss >> tmp;
            token = tmp.substr(0, tmp.find(delimiter));
            if (token == "fan")
            {
                fanout = std::stoi(tmp.substr(tmp.find(delimiter) + delimiter.length()));
            }
            else
            {
                throw std::runtime_error("tree_config: Provided treegen file has invalid tree fanout!");
            }

            /* Pipeline Stretch */
            iss >> tmp;
            token = tmp.substr(0, tmp.find(delimiter));
            if (token == "pipe")
            {
                pipe_stretch = std::stoi(tmp.substr(tmp.find(delimiter) + delimiter.length()));
            }
            else
            {
                throw std::runtime_error("tree_config: Provided treegen file has invalid tree pipeline-stretch!");
            }

            while (iss >> replica_id)
            {
                new_tree_array.push_back(replica_id);
            }

            default_trees.push_back(TreeNetwork(Tree(tid, fanout, pipe_stretch, new_tree_array), std::move(replicas), id));
            tid++;
            // system_trees[tid] = TreeNetwork(Tree(tid, fanout, pipe_stretch, new_tree_array), std::move(replicas), id);
            // tid++;
        }

        on_hold_epoch = Epoch(cur_epoch.get_epoch_num() + 1, default_trees);
    }

    void HotStuffBase::tree_scheduler(std::vector<std::tuple<NetAddr, pubkey_bt, uint256_t>> &&replicas, bool startup)
    {

        LOG_PROTO("\n=========================== Kauri Tree Scheduler =================================\n");
        LOG_PROTO("[ReplicaID %lld] SCHEDULING A NEW TREE %s", id, startup ? "(STARTUP)" : "");

        auto offset = 0;

        if (startup)
        {

            // STARTUP
            global_replicas = std::move(replicas);
            auto size = global_replicas.size();
            lastCheckedHeight = 0;

            for (size_t i = 0; i < size; i++)
            {
                auto x = 0;

                // Get the certificate hash from the replica vector
                auto cert_hash = std::move(std::get<2>(global_replicas[i]));

                // Get the peer id from the certificate hash
                salticidae::PeerId peer{cert_hash};

                // Add the certificate hash to the set of valid TLS certificates
                valid_tls_certs.insert(cert_hash);

                // Get the net address from the replica vector
                auto &addr = std::get<0>(global_replicas[i]);

                /*
                    Add the replica to the system's config
                    i = replica id (0 to N)
                    peer = peer id
                    3rd arg = PubKey
                */
                HotStuffCore::add_replica(i, peer, std::move(std::get<1>(global_replicas[i])));
                // Add all replicas except myself to my peer array and peer network/stack
                if (addr != listen_addr)
                {
                    HOTSTUFF_LOG_PROTO("[STARTUP] Adding Peer with PeerId %s, ReplicaId %llu and IP %s to network.", peer.to_hex().c_str(), i, std::string(addr).c_str());
                    peers.push_back(peer);
                    pn.add_peer(peer);
                    pn.set_peer_addr(peer, addr);
                    peer_id_map.insert(std::make_pair(peer, i));
                }
            }

            // Creates a system_tree object based on a file or a an algorithm
            tree_config(std::move(global_replicas));
            // Change from here justs reads a new epoch from file
            read_epoch_from_file(std::move(global_replicas));
        }

        /* Update the current tree */
        offset = get_pace_maker()->get_current_tid();
        current_tree_network = system_trees[offset];
        current_tree = current_tree_network.get_tree();

        /* Adjust fanout and pipeline stretch accordingly */
        config.async_blocks = current_tree.get_pipeline_stretch();
        config.fanout = current_tree.get_fanout();

        /* Set when the tree will change */

        // if(startup) { //TODO: WARMUP PARAMETER
        //     current_tree_network.set_target(300);

        //     // ev_end_warmup = TimerEvent(ec, [this](TimerEvent &){
        //     //     HOTSTUFF_LOG_PROTO("[WARMUP] Ended warmup, tree switch will happen at %d", lastCheckedHeight + config.tree_switch_period);
        //     //     current_tree_network.set_target(lastCheckedHeight + config.tree_switch_period);
        //     // });
        //     // ev_end_warmup.add(90);
        // }
        // else
        //     current_tree_network.set_target(lastCheckedHeight + config.tree_switch_period);

        if (warmup_counter < system_trees.size())
        {
            // Do 1 block for each tree in schedule to warmup
            current_tree_network.set_target(lastCheckedHeight + 1);
            warmup_counter++;
        }
        else
            current_tree_network.set_target(lastCheckedHeight + config.tree_switch_period);

        /*See if the epochs are being initialized correctly */
        HOTSTUFF_LOG_PROTO("%s", std::string(cur_epoch).c_str());
        HOTSTUFF_LOG_PROTO("%s", std::string(on_hold_epoch).c_str());
        /* ---------------------------------------------------------*/
        HOTSTUFF_LOG_PROTO("%s", std::string(current_tree_network).c_str());
        HOTSTUFF_LOG_PROTO("Next tree switch will happen at block %llu.", current_tree_network.get_target());

        LOG_PROTO("\n=========================== Finished Tree Switch =================================\n");

        /* Proposer opens client for himself */
        // open_client(get_system_tree_root(offset));
    }

    void HotStuffBase::change_epoch()
    {

        LOG_PROTO("\n=========================== Strating Epoch Change =================================\n");

        size_t offset = 0;

        // Updates epoch
        cur_epoch = on_hold_epoch;
        on_hold_epoch = Epoch(cur_epoch.get_epoch_num() + 1);
        ;

        // Updates system trees
        system_trees = cur_epoch.get_system_trees();
        current_tree_network = system_trees[offset];

        HOTSTUFF_LOG_PROTO("%s", std::string(cur_epoch).c_str());
        HOTSTUFF_LOG_PROTO("%s", std::string(current_tree_network).c_str());

        LOG_PROTO("\n=========================== Finished Epoch Switch =================================\n");
    }

    void HotStuffBase::close_client(ReplicaID rid)
    {

        // I was the previous proposer, kill client
        if (rid == get_id())
        {
            int retval = kill(client_pid, SIGTERM);
            if (retval == -1)
            {
                perror("Error on killing client");
            }
            else
            {
                HOTSTUFF_LOG_PROTO("Killed client.");
            }
        }
    }

    void HotStuffBase::open_client(ReplicaID rid)
    {

        // If I am proposer, start client
        if (rid == get_id())
        {

            std::string id_str = std::to_string(get_id());

            const char *program = "./examples/hotstuff-client"; // Adjust the path as necessary
            char *const argv[] = {
                (char *)program,
                "--idx",
                (char *)id_str.c_str(),
                "--iter",
                "-900",
                "--max-async",
                "20",
                NULL};

            // char *const argv[] = {(char *)client_prog, NULL};
            char *const empty_environ[] = {NULL};

            if (posix_spawn(&client_pid, program, NULL, NULL, argv, empty_environ) != 0)
            {
                perror("posix_spawn: error starting up program");
            }

            HOTSTUFF_LOG_PROTO("Successfully started client with pid=%d", client_pid);
        }
    }

    // Tree switch could be either a a normal tree switch or a epoch change (that is nothing more than also a tree switch)
    bool HotStuffBase::isTreeSwitch(int bheight)
    {
        if (bheight > lastCheckedHeight)
        {
            lastCheckedHeight = bheight;
        }

        // First condition to change epoch at block 150
        return lastCheckedHeight == 150 || lastCheckedHeight == current_tree_network.get_target();
    }

    void HotStuffBase::start(std::vector<std::tuple<NetAddr, pubkey_bt, uint256_t>> &&replicas, bool ec_loop)
    {

        /* ./examples/hotstuff-client */
        // snprintf(client_prog, sizeof(client_prog), "./examples/hotstuff-client --idx %d --iter -1 --max-async 50 > clientlog%d &", get_id(), get_id());

        /* Initial tree config */

        HotStuffBase::tree_scheduler(std::move(replicas), true);
        for (const PeerId &peer : peers)
        {
            pn.conn_peer(peer);
        }

        /* ((n - 1) + 1 - 1) / 3 */
        uint32_t nfaulty = peers.size() / 3;
        if (nfaulty == 0)
            LOG_WARN("too few replicas in the system to tolerate any failure");
        on_init(nfaulty);
        pmaker->init(this);

        // TODO: Make this less ugly
        // Due to how the system is deployed, this is an alternative to correctly setup the PM's first proposer
        get_pace_maker()->update_tree_proposer();
        get_pace_maker()->setup();

        if (ec_loop)
            ec.dispatch();

        // max_cmd_pending_size = blk_size * 100; // Hold up till 100 block worth of commands
        max_cmd_pending_size = blk_size; // Hold up till 1 block worth of commands
        final_buffer.reserve(blk_size);
        cmd_pending_buffer.reserve(max_cmd_pending_size);

        ev_beat_timer = TimerEvent(ec, [this](TimerEvent &)
                                   {

        if(final_buffer.empty()) {
            for(size_t i = 0; i < blk_size; i++) {
                uint256_t hash = salticidae::get_hash(i);
                final_buffer.push_back(hash);
            }
        }

        if(pmaker->get_proposer() == get_id()) beat();

        ev_beat_timer.add(0.05); });
        ev_beat_timer.add(10);

        ev_check_pending = TimerEvent(ec, [this](TimerEvent &)
                                      {
        /*Take care of pending proposals*/
        while(!pending_proposals.empty()) {
            auto pending_proposal = std::move(pending_proposals.front());

            if(pending_proposal.first.proposal.tid != get_tree_id()) {
                break;
            }

            LOG_PROTO("[PROP HANDLER] Popping pending proposal: %s", std::string(pending_proposal.first.proposal).c_str());
            pending_proposal = std::move(pending_proposals.front());
            pending_proposals.erase(pending_proposals.begin());
            auto &pending_prop = pending_proposal.first.proposal;

            auto pending_msg_tree = system_trees[pending_prop.tid];
            auto pending_childPeers = pending_msg_tree.get_childPeers();

            // if (!pending_childPeers.empty()) {
            //     LOG_PROTO("[PROP HANDLER] Relaying pending proposal proposal to children in tid=%d", pending_prop.tid);
            //     MsgPropose relay = MsgPropose(pending_proposal.first.serialized, true);
            //     for (const PeerId &peerId : pending_childPeers) {
            //         pn.send_msg(relay, peerId);
            //     }
            // }

            block_t pending_blk = pending_prop.blk;
            if (!pending_blk){
                LOG_PROTO("[PROP HANDLER] Pending block is null!");
                break;
            }

            const PeerId &pending_peer = pending_proposal.second->get_peer_id();

            promise::all(std::vector<promise_t>{
                async_deliver_blk(pending_blk->get_hash(), pending_peer)
            }).then([this, pending_prop = std::move(pending_prop)]() {
                on_receive_proposal(pending_prop);
            });
        }
        ev_check_pending.add(1); });
        ev_check_pending.add(1);

        // /** Alternative to clients: Locally generated blocks */
        // sleep(15); // Wait for connections to setup
        // while(true) {

        //     if(beating) continue;

        //     ReplicaID proposer = pmaker->get_proposer();
        //     if (proposer != get_id()) {
        //             continue;
        //     }

        //     if(final_buffer.empty()) {
        //         for(size_t i = 0; i < blk_size; i++) {
        //             uint256_t hash = salticidae::get_hash(i);
        //             final_buffer.push_back(hash);
        //         }
        //         if(pmaker->get_proposer() == get_id()) beat();
        //     }
        //     else {
        //         continue;
        //     }

        // }

        // cmd_pending.reg_handler(ec, [this](cmd_queue_t &q) {
        //     std::pair<uint256_t, commit_cb_t> e; // e.first = cmd_hash, e.second = finality callback function

        //     while (q.try_dequeue(e))
        //     {

        //         /** Note: We have to send temporary Finality messages to clients
        //          * so that they send more commands to fill our blocks!*/

        //         ReplicaID proposer = pmaker->get_proposer();

        //         // Reply with -1 if we're not the proposer
        //         if (proposer != get_id()) {
        //             e.second(Finality(id, get_tree_id(), -1, 0, 0, e.first, uint256_t()));
        //             continue;
        //         }

        //         // Check if the command has already been processed or is waiting to be processed
        //         if (cmd_pending_buffer.size() < max_cmd_pending_size) {
        //             const auto &cmd_hash = e.first;
        //             auto it = decision_waiting.find(cmd_hash);

        //             if (decision_made.count(cmd_hash)) {
        //                 // Reply with -2 if we already know the height of the command
        //                 uint32_t height = decision_made[cmd_hash];
        //                 e.second(Finality(id, get_tree_id(), -2, 0, height, cmd_hash, uint256_t()));
        //                 continue;
        //             }

        //             // If the command is not in the decision_waiting map, insert it
        //             if (it == decision_waiting.end())
        //                 it = decision_waiting.insert(std::make_pair(cmd_hash, e.second)).first;

        //             // Reply with -3 if we're proposer and now the command is now pending
        //             e.second(Finality(id, get_tree_id(), -3, 0, 0, cmd_hash, uint256_t()));
        //             cmd_pending_buffer.push_back(cmd_hash);
        //         }
        //         else {
        //             // Reply with -4 otherwise (max pending size reached, command won't be processed, client must resubmit if they wish)
        //             e.second(Finality(id, get_tree_id(), -4, 0, 0, e.first, uint256_t()));
        //         }

        //         // Transfer the pending buffer into the final buffer. Beat while final buffer has commands
        //         if (cmd_pending_buffer.size() >= blk_size || !final_buffer.empty()) {

        //             // Pass a block of commands to the final buffer
        //             if (final_buffer.empty()) {
        //                 std::move(std::make_move_iterator(cmd_pending_buffer.begin()),
        //                           std::make_move_iterator(cmd_pending_buffer.begin() + blk_size), std::back_inserter(final_buffer));
        //                 cmd_pending_buffer.erase(cmd_pending_buffer.begin(), cmd_pending_buffer.begin() + blk_size);
        //                 HOTSTUFF_LOG_PROTO("Filled Propose Final Buffer (%lu commands); Commands Still Pending: %lu", final_buffer.size(), cmd_pending_buffer.size());

        //                 if(pmaker->get_proposer() == get_id()) beat();
        //                 return true;
        //             }
        //         }
        //     }
        //     return false;
        // });
    }

    void HotStuffBase::beat()
    {

        /** Ask pmaker to know if we're a proposer or not. If we are, we propose */
        pmaker->beat().then([this](ReplicaID proposer)
                            {
        if (piped_queue.size() > get_config().async_blocks + 1) {
            HOTSTUFF_LOG_PROTO("[PIPELINING] Piped queue is full! Current size: %d, Max Async Blocks: %d", piped_queue.size(), get_config().async_blocks);
            return;
        }

        // HOTSTUFF_LOG_PROTO("[INSIDE] Current proposer: %d", proposer);
        // HOTSTUFF_LOG_PROTO("[INSIDE] get_id: %d", get_id());

        if (proposer == get_id()) {
            //HOTSTUFF_LOG_PROTO("BEAT AS PROPOSER");
            struct timeval timeStart, timeEnd;
            gettimeofday(&timeStart, NULL);

            auto parents = pmaker->get_parents();

            struct timeval current_time;
            gettimeofday(&current_time, NULL);
            block_t current = pmaker->get_current_proposal();

            if (piped_queue.size() < get_config().async_blocks && current != get_genesis()) {
                HOTSTUFF_LOG_PROTO("[PIPELINING] Current piped queue: %d, Max Async Blocks: %d", piped_queue.size(), get_config().async_blocks);

                if (piped_queue.empty() && ((current_time.tv_sec - last_block_time.tv_sec) * 1000000 + current_time.tv_usec -last_block_time.tv_usec) / 1000 < config.piped_latency) {
                    HOTSTUFF_LOG_PROTO("omitting propose");
                } else {
                    block_t highest = current;
                    for (auto p_hash : piped_queue) {
                        block_t block = storage->find_blk(p_hash);
                        if (block->height > highest->height) {
                            highest = block;
                        }
                    }

                    if (parents[0]->height < highest->height) {
                        parents.insert(parents.begin(), highest);
                    }
                    auto cmds = std::move(final_buffer);
                    block_t piped_block = storage->add_blk(new Block(parents, cmds,
                                                             hqc.second->clone(), bytearray_t(),
                                                             parents[0]->height + 1,
                                                             current,
                                                             nullptr));
                    piped_queue.push_back(piped_block->hash);
                    HOTSTUFF_LOG_PROTO("[PIPELINING] Pushed piped block into queue: %.10s", piped_block->hash.to_hex().c_str());
                    print_pipe_queues(true, false);


                    Proposal prop(id, get_cur_epoch_nr() ,get_tree_id(), piped_block, nullptr);
                    HOTSTUFF_LOG_PROTO("propose piped %s", std::string(*piped_block).c_str());
                    /* broadcast to other replicas */
                    gettimeofday(&last_block_time, NULL);
                    on_deliver_blk(piped_block);
                    do_broadcast_proposal(prop);
                    /*if (id == get_pace_maker()->get_proposer()) {
                        gettimeofday(&timeEnd, NULL);
                        long usec = ((timeEnd.tv_sec - timeStart.tv_sec) * 1000000 + timeEnd.tv_usec - timeStart.tv_usec);
                        stats.insert(std::make_pair(piped_block->hash, usec));
                    }*/
                    piped_submitted = false;


                    // TREE ROTATION FOR PROPOSER CASE 2
                    if (isTreeSwitch(piped_block->get_height())) {
                        LOG_PROTO("[PROPOSER] Forcing a reconfiguration! (piped block height is now %llu)", piped_block->get_height());
                        inc_time(true);
                    }
                    else if (piped_block->get_height() >  get_total_system_trees()) {
                        inc_time(false);
                    }
                }
            } else {
                gettimeofday(&last_block_time, NULL);
                auto cmds = std::move(final_buffer);
                on_propose(cmds, std::move(parents));
            }
        } });
    }

    void HotStuffBase::print_pipe_queues(bool printPiped, bool printRdy)
    {

        if (printPiped)
        {
            std::string piped_queue_str = "";
            for (auto &hash : piped_queue)
            {
                piped_queue_str += "|" + hash.to_hex().substr(0, 10) + "| ";
            }

            HOTSTUFF_LOG_PROTO("Piped queue has size %d: Front-> %s", piped_queue.size(), piped_queue_str.c_str());
        }

        if (printRdy)
        {
            std::string rdy_queue_str = "";
            for (auto &hash : rdy_queue)
            {
                rdy_queue_str += "|" + hash.to_hex().substr(0, 10) + "| ";
            }

            HOTSTUFF_LOG_PROTO("Rdy queue has size %d: Front-> %s", rdy_queue.size(), rdy_queue_str.c_str());
        }
    }

    // block_t HotStuffBase::repropose_beat(const std::vector<uint256_t> &cmds) {

    //     pmaker->beat();

    //     struct timeval timeStart, timeEnd;
    //     gettimeofday(&timeStart, NULL);

    //     auto parents = pmaker->get_parents();

    //     struct timeval current_time;
    //     gettimeofday(&current_time, NULL);
    //     block_t current = pmaker->get_current_proposal();

    //     if (piped_queue.size() < get_config().async_blocks && current != get_genesis()) {
    //         HOTSTUFF_LOG_PROTO("[PIPELINING] Current piped queue: %d, Max Async Blocks: %d", piped_queue.size(), get_config().async_blocks);

    //         if (piped_queue.empty() && ((current_time.tv_sec - last_block_time.tv_sec) * 1000000 + current_time.tv_usec -last_block_time.tv_usec) / 1000 < config.piped_latency) {
    //             HOTSTUFF_LOG_PROTO("omitting propose");
    //         } else {
    //             block_t highest = current;
    //             for (auto p_hash : piped_queue) {
    //                 block_t block = storage->find_blk(p_hash);
    //                 if (block->height > highest->height) {
    //                     highest = block;
    //                 }
    //             }

    //             if (parents[0]->height < highest->height) {
    //                 parents.insert(parents.begin(), highest);
    //             }

    //             block_t piped_block = storage->add_blk(new Block(parents, cmds,
    //                                                     hqc.second->clone(), bytearray_t(),
    //                                                     parents[0]->height + 1,
    //                                                     current,
    //                                                     nullptr));
    //             piped_queue.push_back(piped_block->hash);

    //             Proposal prop(id, get_tree_id(), piped_block, nullptr);
    //             HOTSTUFF_LOG_PROTO("propose piped %s", std::string(*piped_block).c_str());
    //             /* broadcast to other replicas */
    //             gettimeofday(&last_block_time, NULL);
    //             do_broadcast_proposal(prop);

    //             /*if (id == get_pace_maker()->get_proposer()) {
    //                 gettimeofday(&timeEnd, NULL);
    //                 long usec = ((timeEnd.tv_sec - timeStart.tv_sec) * 1000000 + timeEnd.tv_usec - timeStart.tv_usec);
    //                 stats.insert(std::make_pair(piped_block->hash, usec));
    //             }*/
    //             piped_submitted = false;

    //             return piped_block;
    //         }
    //     }
    //     else {
    //         gettimeofday(&last_block_time, NULL);
    //         return on_propose(cmds, std::move(parents));
    //     }
    // }

}
