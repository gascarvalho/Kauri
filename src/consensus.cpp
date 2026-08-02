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

#include <cassert>
#include <stack>
#include <unordered_set>
#include <include/hotstuff/liveness.h>
#include <salticidae/type.h>

#include "hotstuff/util.h"
#include "hotstuff/consensus.h"

#define LOG_INFO HOTSTUFF_LOG_INFO
#define LOG_DEBUG HOTSTUFF_LOG_DEBUG
#define LOG_WARN HOTSTUFF_LOG_WARN
#define LOG_PROTO HOTSTUFF_LOG_PROTO

namespace hotstuff
{

    /* The core logic of HotStuff, is fairly simple :). */
    /*** begin HotStuff protocol logic ***/
    HotStuffCore::HotStuffCore(ReplicaID id,
                               privkey_bt &&priv_key) : b0(new Block(true, 1)),
                                                        b_lock(b0),
                                                        b_exec(b0),
                                                        vheight(0),
                                                        priv_key(std::move(priv_key)),
                                                        tails{b0},
                                                        vote_disabled(false),
                                                        id(id),
                                                        storage(new EntityStorage())
    {
        storage->add_blk(b0);
    }

    void HotStuffCore::sanity_check_delivered(const block_t &blk)
    {
        if (!blk->delivered)
            throw std::runtime_error("block not delivered");
    }

    block_t HotStuffCore::get_potentially_not_delivered_blk(const uint256_t &blk_hash)
    {
        block_t blk = storage->find_blk(blk_hash);
        if (blk == nullptr)
            throw std::runtime_error("block not delivered " + std::to_string(blk == nullptr));
        // else if (!blk->delivered)
        //     on_deliver_blk(blk);

        return blk;
    }

    block_t HotStuffCore::get_delivered_blk(const uint256_t &blk_hash)
    {
        block_t blk = storage->find_blk(blk_hash);
        if (blk == nullptr || !blk->delivered)
        {
            HOTSTUFF_LOG_PROTO("block %.10s not delivered", get_hex10(blk_hash).c_str());
            throw std::runtime_error("block not delivered ");
        }
        return blk;
    }

    bool HotStuffCore::on_deliver_blk(const block_t &blk)
    {
        HOTSTUFF_LOG_PROTO("Core deliver for %.10s", get_hex10(blk->hash).c_str());

        if (blk->delivered)
        {
            LOG_WARN("attempt to deliver a block twice: %.10s", get_hex10(blk->hash).c_str());
            return false;
        }
        blk->parents.clear();
        for (const auto &hash : blk->parent_hashes)
        {
            if (!piped_queue.empty() && std::find(piped_queue.begin(), piped_queue.end(), hash) != piped_queue.end())
            {
                block_t piped_block = storage->find_blk(hash);
                blk->parents.push_back(piped_block);
            }
            else
            {
                blk->parents.push_back(get_delivered_blk(hash));
            }
        }
        blk->height = blk->parents[0]->height + 1;

        if (blk->qc)
        {
            block_t _blk = storage->find_blk(blk->qc->get_obj_hash());
            if (_blk == nullptr)
                throw std::runtime_error("block referred by qc not fetched");
            blk->qc_ref = std::move(_blk);
        } // otherwise blk->qc_ref remains null

        for (auto pblk : blk->parents)
            tails.erase(pblk);
        tails.insert(blk);

        blk->delivered = true;

        if (blk->height > 5)
        {
            salticidae::ElapsedTime blk_et;
            auto hash = blk->hash;
            proposal_time[blk->hash] = blk_et;
            proposal_time[blk->hash].start();

            if (blk->qc_ref)
            {
                auto it = proposal_time.find(blk->qc_ref->hash);
                if (it != proposal_time.end())
                {
                    it->second.stop();
                    long ms = it->second.elapsed_sec * 1000;
                    processed_blocks++;
                    summed_latency += ms;
                    HOTSTUFF_LOG_PROTO("[TIME] Average latency per block (%lu tx): %d ms", get_blk_size(), summed_latency / processed_blocks);
                    HOTSTUFF_LOG_PROTO("[TIME] Stats for last block: wall: %.4f, cpu: %.4f", it->second.elapsed_sec, it->second.cpu_elapsed_sec);
                }
            }
        }

        HOTSTUFF_LOG_PROTO("deliver %s", std::string(*blk).c_str());
        return true;
    }

    bool HotStuffCore::is_ancestor(const block_t &maybe_ancestor,
                                   const block_t &descendant) const
    {
        if (!maybe_ancestor || !descendant)
            return false;

        std::unordered_set<const Block *> visited;
        for (block_t block = descendant; block;)
        {
            if (!visited.insert(block.get()).second)
                return false;
            if (block == maybe_ancestor)
                return true;
            if (block->parents.empty())
                return false;

            const block_t &parent = block->parents[0];
            if (!parent || parent->height >= block->height)
                return false;
            block = parent;
        }
        return false;
    }

    bool HotStuffCore::has_valid_qc_ancestry(
        const block_t &certifier,
        const block_t &certified) const
    {
        return certifier && certifier->delivered && certifier->qc &&
               certified && certified->delivered &&
               certifier->qc_ref == certified &&
               certifier->qc->get_obj_hash() == certified->hash &&
               certified->height < certifier->height &&
               is_ancestor(certified, certifier);
    }

    void HotStuffCore::update_hqc(const block_t &_hqc, const quorum_cert_bt &qc)
    {
        if (_hqc->height > hqc.first->height)
        {
            hqc = std::make_pair(_hqc, qc->clone());
            on_hqc_update();
        }
    }

    void HotStuffCore::update(const block_t &nblk)
    {
        if (!nblk || !nblk->delivered)
            return;

        std::cout << "Begin update fuction at Head -> nblk: " << std::string(*nblk).c_str() << std::endl;

        /* nblk = b*, blk2 = b'', blk1 = b', blk = b */
#ifndef HOTSTUFF_TWO_STEP
        /* three-step HotStuff */
        const block_t &blk2 = nblk->qc_ref;

        if (!has_valid_qc_ancestry(nblk, blk2))
        {
            std::cout << "nblk has no valid delivered qc_ref." << std::endl;
            return;
        }

        std::cout << "blk2: " << std::string(*blk2).c_str() << std::endl;

        update_hqc(blk2, nblk->qc);
        std::cout << "update: step 1 done (pre-commit/hqc update)" << std::endl;

        const block_t &blk1 = blk2->qc_ref;
        if (!has_valid_qc_ancestry(blk2, blk1))
        {
            std::cout << "blk2 has no valid delivered qc_ref." << std::endl;
            return;
        }

        std::cout << "blk1: " << std::string(*blk1).c_str() << std::endl;

        if (blk1->height > b_lock->height)
            b_lock = blk1;

        std::cout << "update: step 2 done (commit/b_lock update)" << std::endl;

        const block_t &blk = blk1->qc_ref;
        if (!has_valid_qc_ancestry(blk1, blk))
        {
            std::cout << "blk1 has no valid delivered qc_ref." << std::endl;
            return;
        }

        std::cout << "blk: " << std::string(*blk).c_str() << std::endl;

        if (!(blk->height < blk1->height && blk1->height < blk2->height) ||
            !is_ancestor(blk, blk1) ||
            !is_ancestor(blk1, blk2) ||
            !is_ancestor(b_exec, blk))
        {
            std::cout << "update: invalid certified ancestry" << std::endl;
            return;
        }

        std::vector<block_t> commit_queue;
        std::unordered_set<const Block *> visited;
        for (block_t block = blk; block != b_exec;)
        {
            if (!block || block->height <= b_exec->height ||
                !visited.insert(block.get()).second || block->parents.empty())
                return;

            const block_t &parent = block->parents[0];
            if (!parent || parent->height >= block->height)
                return;

            commit_queue.push_back(block);
            block = parent;
        }

        if (blk2->decision || blk1->decision || blk->decision)
        {
            std::cout << "certified block already decided, returning" << std::endl;
            return;
        }

#else
        /* two-step HotStuff */
        const block_t &blk1 = nblk->qc_ref;
        if (blk1 == nullptr)
            return;
        if (blk1->decision)
            return;
        update_hqc(blk1, nblk->qc);
        if (blk1->height > b_lock->height)
            b_lock = blk1;

        const block_t &blk = blk1->qc_ref;
        if (blk == nullptr)
            return;
        if (blk->decision)
            return;

        /* commit requires direct parent */
        if (blk1->parents[0] != blk)
        {
            std::cout << "update: no direct parent for step 3 " << std::endl;
            return;
        }
#endif

        std::cout << "update: step 3 able to decide (decide/b_exec update)" << std::endl;

        /* otherwise commit */
#ifdef HOTSTUFF_TWO_STEP
        std::vector<block_t> commit_queue;
        block_t b;
        for (b = blk; b->height > b_exec->height; b = b->parents[0])
        {
            commit_queue.push_back(b);
        }
        if (b != b_exec)
            throw std::runtime_error("safety breached :( " +
                                     std::string(*blk) + " " +
                                     std::string(*b_exec));
#endif

        std::uint64_t commit_batch_index = 0;
        for (auto it = commit_queue.rbegin(); it != commit_queue.rend(); it++)
        {
            const block_t &blk = *it;
            blk->decision = 1;
            do_consensus(blk);
            LOG_PROTO("commit %s", std::string(*blk).c_str());

            // Clean piped_queue if the clock were undirectly committed 
            auto it2 = std::find(piped_queue.begin(), piped_queue.end(), blk->get_hash());
            if (it2 != piped_queue.end())
            {
                // Element found, safe to erase
                piped_queue.erase(it2);
                HOTSTUFF_LOG_PROTO("[DEBUG] Successfully removed block hash %.10s from piped_queue", blk->get_hash().to_hex().c_str());
            }
            else
            {
                // Element not found, log for debugging
                HOTSTUFF_LOG_PROTO("[DEBUG] Block hash %.10s not found in piped_queue, skipping erase", blk->get_hash().to_hex().c_str());
            }

            decided_blk_counter++;
            for (size_t i = 0; i < blk->cmds.size(); i++)
            {
                do_decide(Finality(id, get_cur_epoch_nr(), get_tree_id(), 1, i, blk->height,
                                   blk->cmds[i], blk->get_hash()));
            }
            do_post_block_commit(blk, commit_batch_index);
            ++commit_batch_index;
        }

        if (!commit_queue.empty() && blk1->qc != nullptr)
            on_verified_commit_progress(
                blk1->qc->get_proposal_key());

        b_exec = blk;
    }

    // Leader proposal
    block_t HotStuffCore::on_propose(const std::vector<uint256_t> &cmds,
                                     const std::vector<block_t> &parents,
                                     bytearray_t &&extra)
    {
        struct timeval timeStart, timeEnd;
        gettimeofday(&timeStart, NULL);

        if (parents.empty())
            throw std::runtime_error("empty parents");
        for (const auto &_ : parents)
            tails.erase(_);
        /* create the new block */

        block_t bnew;
        if (piped_queue.empty())
        {
            LOG_PROTO("b_piped is null");
            bnew = storage->add_blk(
                new Block(parents, cmds,
                          hqc.second->clone(), std::move(extra),
                          parents[0]->height + 1,
                          hqc.first,
                          nullptr));
        }
        else
        {
            auto newParents = std::vector<block_t>(parents);
            block_t piped_block = storage->find_blk(piped_queue.back());

            if (newParents[0]->height <= piped_block->height)
            {
                LOG_PROTO("b_piped is not null");
                newParents.insert(newParents.begin(), piped_block);
            }

            bnew = storage->add_blk(
                new Block(newParents, cmds,
                          hqc.second->clone(), std::move(extra),
                          newParents[0]->height + 1,
                          hqc.first,
                          nullptr));
        }

        b_normal_height = bnew->get_height();

        LOG_PROTO("propose %s", std::string(*bnew).c_str());
        on_deliver_blk(bnew);
        const auto epoch_number = get_cur_epoch_nr();
        const ConfigurationId configuration{
            epoch_number,
            get_tree_id(),
            get_epoch_digest(epoch_number)};
        HOTSTUFF_LOG_INFO(
            "KAURI_LOCAL_PROPOSAL stage=ordinary_process_begin replica=%u "
            "epoch=%u tree=%u block=%s",
            static_cast<unsigned>(id),
            configuration.epoch_number,
            configuration.tree_id,
            bnew->hash.to_hex().c_str());
        Proposal prop = process_block(
            bnew,
            true,
            configuration);
        HOTSTUFF_LOG_INFO(
            "KAURI_LOCAL_PROPOSAL stage=ordinary_process_end replica=%u "
            "epoch=%u tree=%u block=%s",
            static_cast<unsigned>(id),
            configuration.epoch_number,
            configuration.tree_id,
            bnew->hash.to_hex().c_str());

        HOTSTUFF_LOG_INFO(
            "KAURI_LOCAL_PROPOSAL stage=ordinary_hook_begin replica=%u "
            "epoch=%u tree=%u block=%s",
            static_cast<unsigned>(id),
            configuration.epoch_number,
            configuration.tree_id,
            bnew->hash.to_hex().c_str());
        on_local_proposal_processed(prop.key());
        HOTSTUFF_LOG_INFO(
            "KAURI_LOCAL_PROPOSAL stage=ordinary_hook_end replica=%u "
            "epoch=%u tree=%u block=%s",
            static_cast<unsigned>(id),
            configuration.epoch_number,
            configuration.tree_id,
            bnew->hash.to_hex().c_str());

        /* broadcast to other replicas */
        HOTSTUFF_LOG_INFO(
            "KAURI_LOCAL_PROPOSAL stage=ordinary_broadcast_begin "
            "replica=%u epoch=%u tree=%u block=%s",
            static_cast<unsigned>(id),
            configuration.epoch_number,
            configuration.tree_id,
            bnew->hash.to_hex().c_str());
        do_broadcast_proposal(prop);
        HOTSTUFF_LOG_INFO(
            "KAURI_LOCAL_PROPOSAL stage=ordinary_broadcast_end replica=%u "
            "epoch=%u tree=%u block=%s",
            static_cast<unsigned>(id),
            configuration.epoch_number,
            configuration.tree_id,
            bnew->hash.to_hex().c_str());

        // Gather stats
        if (is_proposer(id))
        {
            gettimeofday(&timeEnd, NULL);
            long usec = ((timeEnd.tv_sec - timeStart.tv_sec) * 1000000 + timeEnd.tv_usec - timeStart.tv_usec);
            stats.insert(std::make_pair(bnew->hash, usec));
        }

        // TREE ROTATION FOR PROPOSER CASE 1
        auto switch_type = isTreeSwitch(b_normal_height);

        if (switch_type == TREE_SWITCH)
        {
            LOG_PROTO("[PROPOSER] Forcing a reconfiguration for changing current tree! (block height is now %llu)", b_normal_height);
            inc_time(switch_type);
        }
        else if (switch_type == EPOCH_SWITCH)
        {
            LOG_PROTO("[PROPOSER] Forcing a reconfiguration for changing current epoch! (block height is now %llu)", b_normal_height);
            inc_time(switch_type);
        }
        return bnew;
    }

    Proposal HotStuffCore::process_block(
        const block_t &bnew,
        bool adjustHeight,
        const ConfigurationId &configuration)
    {
        const uint256_t bnew_hash = bnew->get_hash();
        Proposal prop(
            id,
            configuration.epoch_number,
            configuration.tree_id,
            configuration.epoch_digest,
            bnew,
            nullptr);
        if (bnew->self_qc != nullptr &&
            bnew->self_qc->get_proposal_key() != prop.key())
        {
            throw std::invalid_argument(
                "published block certificate does not match the proposal key");
        }
        if (!admit_local(prop))
            throw std::runtime_error(
                "failed to admit the exact leader-local proposal");

        // proposer_base_deliver(bnew);
        // on_deliver_blk(bnew);
        LOG_PROTO("before update");
        update(bnew);
        // std::cout << "prop" << std::endl;
        /* self-vote */
        if (adjustHeight)
        {
            if (bnew->height <= vheight)
                throw std::runtime_error("new block should be higher than vheight");
            vheight = bnew->height;
        }

        if (storage->find_blk(bnew_hash) == nullptr)
        {
            LOG_PROTO("not in storage!");
        }

        // Vote for own proposed block
        HOTSTUFF_LOG_INFO(
            "KAURI_LOCAL_PROPOSAL stage=local_vote_begin replica=%u "
            "epoch=%u tree=%u block=%s",
            static_cast<unsigned>(id),
            configuration.epoch_number,
            configuration.tree_id,
            bnew_hash.to_hex().c_str());
        on_receive_vote(Vote(
            id, prop.key(), create_part_cert(*priv_key, prop.key()), this));
        HOTSTUFF_LOG_INFO(
            "KAURI_LOCAL_PROPOSAL stage=local_vote_end replica=%u "
            "epoch=%u tree=%u block=%s",
            static_cast<unsigned>(id),
            configuration.epoch_number,
            configuration.tree_id,
            bnew_hash.to_hex().c_str());

        HOTSTUFF_LOG_INFO(
            "KAURI_LOCAL_PROPOSAL stage=proposal_notify_begin replica=%u "
            "epoch=%u tree=%u block=%s",
            static_cast<unsigned>(id),
            configuration.epoch_number,
            configuration.tree_id,
            bnew_hash.to_hex().c_str());
        on_propose_(prop);
        HOTSTUFF_LOG_INFO(
            "KAURI_LOCAL_PROPOSAL stage=proposal_notify_end replica=%u "
            "epoch=%u tree=%u block=%s",
            static_cast<unsigned>(id),
            configuration.epoch_number,
            configuration.tree_id,
            bnew_hash.to_hex().c_str());

        return prop;
    }

    bool HotStuffCore::on_receive_proposal(const Proposal &prop)
    {
        LOG_PROTO("[CONSENSUS] Got PROPOSAL in epoch_nr=%d, tid=%d: %s %s", prop.epoch_nr, prop.tid, std::string(prop).c_str(), std::string(*prop.blk).c_str());

        block_t bnew = prop.blk;
        sanity_check_delivered(bnew);
        LOG_PROTO("before update");
        update(bnew);
        bool opinion = false;

        if (bnew->height > vheight)
        {
            if (bnew->qc_ref && bnew->qc_ref->height > b_lock->height)
            {
                opinion = true; // liveness condition
                vheight = bnew->height;
            }
            else
            { // safety condition (extend the locked branch)
                block_t b;
                for (b = bnew;
                     b->height > b_lock->height;
                     b = b->parents[0])
                    ;
                if (b == b_lock) /* on the same branch */
                {
                    opinion = true;
                    vheight = bnew->height;
                }
            }
        }

        LOG_PROTO("x now state: %s", std::string(*this).c_str());
        if (bnew->qc_ref)
        {
            on_qc_finish(bnew->qc_ref);
        }

        on_receive_proposal_(prop);

        if (opinion && !vote_disabled)
        {
            do_vote(prop, Vote(
                id,
                prop.key(),
                create_part_cert(*priv_key, prop.key()),
                this));
        }

        // UNCOMMENT TO TEST TIMEOUT
        //  if(id == 0 && bnew->height == 45) {
        //      sleep(15);
        //  }

        // TREE ROTATION FOR NON-PROPOSERS
        auto switch_type = isTreeSwitch(bnew->height);

        if (switch_type == TREE_SWITCH)
        {
            LOG_PROTO("[PROPOSER] Forcing a reconfiguration for changing current tree! (block height is now %llu)", bnew->height);
            inc_time(switch_type);
        }
        else if (switch_type == EPOCH_SWITCH)
        {
            LOG_PROTO("[PROPOSER] Forcing a reconfiguration for changing current epoch! (block height is now %llu)", bnew->height);
            inc_time(switch_type);
        }
        /**
        if (isTreeSwitch(bnew->height))
        {
            LOG_PROTO("Forcing a reconfiguration! (block height is now %llu)", bnew->height);
            inc_time(true);
        }
        else if (bnew->height > get_total_system_trees()) // WARMUP FINISHED
        {
            inc_time(false);
        }

        */

        // update(bnew);
        return opinion;
    }

    void HotStuffCore::on_receive_vote(const Vote &vote)
    {
        LOG_PROTO("y now state: %s", std::string(*this).c_str());

        block_t blk = get_delivered_blk(vote.blk_hash);
        assert(vote.cert);

        // In current implementation, only the proposer's vote uses this function
        LOG_PROTO("[CONSENSUS] Applying own vote in epoch_nr=%d, tid=%d: %s %s", vote.epoch_nr, vote.tid, std::string(vote).c_str(), std::string(*blk).c_str());

        if (vote.voter != get_id())
            return;
        if (vote.key().block_hash != blk->get_hash() ||
            vote.cert->get_proposal_key() != vote.key())
        {
            LOG_WARN("local vote does not match its exact proposal");
            return;
        }
        apply_local_vote(vote);
    }

    /*** end HotStuff protocol logic ***/
    void HotStuffCore::on_init(uint32_t nfaulty)
    {
        // config.nmajority = config.nreplicas - nfaulty;
        config.nmajority = nfaulty * 2 + 1;
        HOTSTUFF_LOG_PROTO("N_Replicas: %d", config.nreplicas);
        HOTSTUFF_LOG_PROTO("Maximum Faults: %d", nfaulty);
        HOTSTUFF_LOG_PROTO("Majority Necessary for Quorums: %d", config.nmajority);

        b0->qc = create_quorum_cert(
            genesis_certification_key(b0->get_hash()));
        // b0->qc->compute();
        b0->self_qc = b0->qc->clone();
        b0->qc_ref = b0;
        hqc = std::make_pair(b0, b0->qc->clone());
    }

    void HotStuffCore::prune(uint32_t staleness)
    {
        block_t start;
        /* skip the blocks */
        for (start = b_exec; staleness; staleness--, start = start->parents[0])
            if (!start->parents.size())
                return;
        std::stack<block_t> s;
        start->qc_ref = nullptr;
        s.push(start);
        while (!s.empty())
        {
            auto &blk = s.top();
            if (blk->parents.empty())
            {
                storage->try_release_blk(blk);
                s.pop();
                continue;
            }
            blk->qc_ref = nullptr;
            s.push(blk->parents.back());
            blk->parents.pop_back();
        }
    }

    void HotStuffCore::add_replica(ReplicaID rid, const PeerId &peer_id,
                                   pubkey_bt &&pub_key)
    {
        config.add_replica(rid,
                           ReplicaInfo(rid, peer_id, std::move(pub_key)));
        b0->voted.insert(rid);
    }

    promise_t HotStuffCore::async_qc_finish(const block_t &blk)
    {
        // std::cout << "test " << blk->voted.size() << " " << blk->self_qc->has_n(config.nmajority) << std::endl;
        //  HOTSTUFF_LOG_PROTO("[TEST] Entered async_qc_finish with voted size %d", blk->voted.size());

        // if(blk->self_qc != nullptr) {
        //     HOTSTUFF_LOG_PROTO("blk->self_qc NOT null");

        //     if(blk->self_qc->has_n(config.nmajority))
        //         HOTSTUFF_LOG_PROTO("blk->self_qc HAS nmajority");
        //     else
        //         HOTSTUFF_LOG_PROTO("blk->self_qc DOES NOT HAVE nmajority");
        // }
        // else
        //     HOTSTUFF_LOG_PROTO("blk->self_qc null");

        // if(blk->voted.empty())
        //     HOTSTUFF_LOG_PROTO("blk->voted IS empty");
        // else
        //     HOTSTUFF_LOG_PROTO("blk->voted IS NOT empty");

        // if(blk->voted.size() >= config.nmajority)
        //     HOTSTUFF_LOG_PROTO("blk->voted size >= nmajority");
        // else
        //     HOTSTUFF_LOG_PROTO("blk->voted size < nmajority");

        // If this block is already decided (committed), we can immediately resolve.
        if (blk->decision)
        {
            HOTSTUFF_LOG_PROTO("async_qc_finish: block %.10s is already decided, resolving now", blk->get_hash().to_hex().c_str());
            return promise_t([](promise_t &pm)
                             { pm.resolve(); });
        }

        if (blk->self_qc != nullptr &&
            blk->self_qc->has_n(config.nmajority) &&
            blk->self_qc->verify(config))
        {
            HOTSTUFF_LOG_PROTO("async_qc_finish %.10s", blk->get_hash().to_hex().c_str());

            return promise_t([](promise_t &pm)
                             { pm.resolve(); });
        }

        auto it = qc_waiting.find(blk);
        if (it == qc_waiting.end())
        {
            // HOTSTUFF_LOG_PROTO("[TEST] inserting into qc_waiting blk %s", blk->get_hash().to_hex().c_str());
            it = qc_waiting.insert(std::make_pair(blk, promise_t())).first;
        }

        return it->second;
    }

    void HotStuffCore::on_qc_finish(const block_t &blk)
    {
        // HOTSTUFF_LOG_PROTO("[TEST] Entered on_qc_finish");
        auto it = qc_waiting.find(blk);
        if (it != qc_waiting.end())
        {
            if (first)
            {
                gettimeofday(&start_time, NULL);
                first = false;
            }

            HOTSTUFF_LOG_PROTO("on_qc_finish %.10s", blk->get_hash().to_hex().c_str());

            it->second.resolve();
            qc_waiting.erase(it);
        }
    }

    promise_t HotStuffCore::async_wait_proposal()
    {
        return propose_waiting.then([](const Proposal &prop)
                                    { return prop; });
    }

    promise_t HotStuffCore::async_wait_receive_proposal()
    {
        return receive_proposal_waiting.then([](const Proposal &prop)
                                             { return prop; });
    }

    promise_t HotStuffCore::async_hqc_update()
    {
        return hqc_update_waiting.then([this]()
                                       { return hqc.first; });
    }

    void HotStuffCore::on_propose_(const Proposal &prop)
    {
        auto t = std::move(propose_waiting);
        propose_waiting = promise_t();
        t.resolve(prop);
    }

    void HotStuffCore::on_receive_proposal_(const Proposal &prop)
    {
        auto t = std::move(receive_proposal_waiting);
        receive_proposal_waiting = promise_t();
        t.resolve(prop);
    }

    void HotStuffCore::on_hqc_update()
    {
        // auto t = std::move(hqc_update_waiting);
        // hqc_update_waiting = promise_t();
        // t.resolve();

        // standard code to handle the new HQC...
        auto old = std::move(hqc_update_waiting);
        hqc_update_waiting = promise_t();
        old.resolve();

        // Now check overshadowed blocks
        for (auto it = qc_waiting.begin(); it != qc_waiting.end();)
        {
            block_t blk = it->first;
            if (blk->height < hqc.first->height && !is_ancestor(blk, hqc.first))
            {
                HOTSTUFF_LOG_PROTO("Rejecting overshadowed block %.10s from qc_waiting", blk->hash.to_hex().c_str());
                it->second.reject();
                it = qc_waiting.erase(it);
            }
            else
            {
                ++it;
            }
        }
    }

    HotStuffCore::operator std::string() const
    {
        DataStream s;
        s << "<hotstuff "
          << "hqc=" << get_hex10(hqc.first->get_hash()) << " "
          << "hqc.height=" << std::to_string(hqc.first->height) << " "
          << "b_lock=" << get_hex10(b_lock->get_hash()) << " "
          << "b_exec=" << get_hex10(b_exec->get_hash()) << " "
          << "vheight=" << std::to_string(vheight) << " "
          << "tails=" << std::to_string(tails.size()) << ">";
        return s;
    }

    void HotStuffCore::set_fanout(int32_t fanout)
    {
        config.fanout = fanout;
    }

    void HotStuffCore::set_piped_latency(int32_t piped_latency, int32_t async_blocks)
    {
        config.piped_latency = piped_latency;
        config.async_blocks = async_blocks;
    }

    void HotStuffCore::set_tree_period(size_t nblocks)
    {
        config.tree_switch_period = nblocks;
    }

    void HotStuffCore::set_tree_generation(std::string genAlgo, std::string fpath)
    {
        config.treegen_algo = genAlgo;
        config.treegen_fpath = fpath;
    }

    void HotStuffCore::set_new_epoch(std::string new_epoch)
    {
        config.new_epoch = new_epoch;
    }

}
