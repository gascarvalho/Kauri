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

#ifndef _HOTSTUFF_LIVENESS_H
#define _HOTSTUFF_LIVENESS_H

#include "salticidae/util.h"
#include "hotstuff/hotstuff.h"

namespace hotstuff {

using salticidae::_1;
using salticidae::_2;

/** Abstraction for liveness gadget (oracle). */
class PaceMaker {
    protected:
    HotStuffCore *hsc;
    public:
    virtual ~PaceMaker() = default;
    /** Initialize the PaceMaker. A derived class should also call the
     * default implementation to set `hsc`. */
    virtual void init(HotStuffCore *_hsc) { hsc = _hsc; }
    /** Get a promise resolved when the pace maker thinks it is a *good* time
     * to issue new commands. When promise is resolved, the replica should
     * propose the command. */
    virtual promise_t beat() = 0;
    /** Get the current proposer. */
    virtual ReplicaID get_proposer() = 0;
    /** Select the parent blocks for a new block.
     * @return Parent blocks. The block at index 0 is the direct parent, while
     * the others are uncles/aunts. The returned vector should be non-empty. */
    virtual std::vector<block_t> get_parents() = 0;
    /** Get a promise resolved when the pace maker thinks it is a *good* time
     * to vote for a block. The promise is resolved with the next proposer's ID
     * */
    virtual promise_t beat_resp(ReplicaID last_proposer) = 0;
    /** Impeach the current proposer. */
    virtual void impeach() {}
    virtual void on_consensus(const block_t &) {}
    virtual size_t get_pending_size() = 0;
    virtual void inc_time(bool force) {}
    virtual size_t get_current_tid() {}
    virtual void update_tree_proposer() {}
    virtual void setup() {}

    virtual block_t get_current_proposal() { }
};

using pacemaker_bt = BoxObj<PaceMaker>;

/** Parent selection implementation for PaceMaker: select the highest tail that
 * follows the current hqc block. */
class PMHighTail: public virtual PaceMaker {
    block_t hqc_tail;
    const int32_t parent_limit;         /**< maximum number of parents */

    bool check_ancestry(const block_t &_a, const block_t &_b) {
        block_t b;
        for (b = _b;
            b->get_height() > _a->get_height();
            b = b->get_parents()[0]);
        return b == _a;
    }
    
    void reg_hqc_update() {
        hsc->async_hqc_update().then([this](const block_t &hqc) {
            hqc_tail = hqc;
            for (const auto &tail: hsc->get_tails())
                if (check_ancestry(hqc, tail) && tail->get_height() > hqc_tail->get_height())
                    hqc_tail = tail;
            reg_hqc_update();
        });
    }

    void reg_proposal() {
        hsc->async_wait_proposal().then([this](const Proposal &prop) {
            hqc_tail = prop.blk;
            reg_proposal();
        });
    }

    void reg_receive_proposal() {
        hsc->async_wait_receive_proposal().then([this](const Proposal &prop) {
            const auto &hqc = hsc->get_hqc();
            const auto &blk = prop.blk;
            if (check_ancestry(hqc, blk) && blk->get_height() > hqc_tail->get_height())
                hqc_tail = blk;
            reg_receive_proposal();
        });
    }

    public:
    PMHighTail(int32_t parent_limit): parent_limit(parent_limit) {}
    void init() {
        hqc_tail = hsc->get_genesis();
        reg_hqc_update();
        reg_proposal();
        reg_receive_proposal();
    }

    std::vector<block_t> get_parents() override {
        const auto &tails = hsc->get_tails();
        std::vector<block_t> parents{hqc_tail};
        // TODO: inclusive block chain
        // auto nparents = tails.size();
        // if (parent_limit > 0)
        //     nparents = std::min(nparents, (size_t)parent_limit);
        // nparents--;
        // /* add the rest of tails as "uncles/aunts" */
        // for (const auto &blk: tails)
        // {
        //     if (blk != hqc_tail)
        //     {
        //         parents.push_back(blk);
        //         if (!--nparents) break;
        //     }
        // }
        return parents;
    }
};

/** Beat implementation for PaceMaker: simply wait for the QC of last proposed
 * block.  PaceMakers derived from this class will beat only when the last
 * block proposed by itself gets its QC. */
class PMWaitQC: public virtual PaceMaker {


    public:

    std::queue<promise_t> pending_beats;
    block_t last_proposed;
    bool locked;
    promise_t pm_wait_propose;

    protected:

    /** ORIGINAL IMPLEMENTATION */

    // void schedule_next() {
    //     if (!pending_beats.empty() && !locked)
    //     {
    //         auto pm = pending_beats.front();
    //         pending_beats.pop();
    //         pm_qc_finish.reject();
    //         (pm_qc_finish = hsc->async_qc_finish(last_proposed))
    //             .then([this, pm]() {
    //                 pm.resolve(get_proposer());
    //             });
    //         locked = true;
    //     }
    // }


    virtual void schedule_next() {
        if (!pending_beats.empty())
        {
            if (locked) {
                struct timeval current_time;
                gettimeofday(&current_time, NULL);

                if (hsc->piped_queue.size() < hsc->get_config().async_blocks
                && !hsc->piped_submitted
                && ((current_time.tv_sec - hsc->last_block_time.tv_sec) * 1000000 + current_time.tv_usec - hsc->last_block_time.tv_usec) / 1000 > hsc->get_config().piped_latency) {
                    HOTSTUFF_LOG_PROTO("Extra block");
                    auto pm = pending_beats.front();
                    pending_beats.pop();
                    hsc->piped_submitted = true;
                    pm.resolve(get_proposer());
                    return;
                }

                if (!hsc->piped_queue.empty() && hsc->b_normal_height > 0) {
                    block_t piped_block = hsc->storage->find_blk(hsc->piped_queue.back());
                    if ( piped_block->get_height() > hsc->get_config().async_blocks + 10 && hsc->b_normal_height < piped_block->get_height() - (hsc->get_config().async_blocks + 10)
                            && ((current_time.tv_sec - hsc->last_block_time.tv_sec) * 1000000 + current_time.tv_usec - hsc->last_block_time.tv_usec) / 1000 > hsc->get_config().piped_latency) {
                        HOTSTUFF_LOG_PROTO("Extra recovery block %d %d", hsc->b_normal_height, piped_block->get_height());

                        auto pm = pending_beats.front();
                        pending_beats.pop();
                        hsc->piped_submitted = true;
                        pm.resolve(get_proposer());
                    }
                }
            } else {
                HOTSTUFF_LOG_PROTO("schedule_next: popping beat to async_qc_finish");
                auto pm = pending_beats.front();
                pending_beats.pop();
                hsc->async_qc_finish(last_proposed)
                        .then([this, pm]() {
                            pm.resolve(get_proposer());
                        });
                locked = true;
            }
        }
        else
        {
            std::cout << "not enough client tx" << std::endl;
        }
    }

    void update_last_proposed() {
        pm_wait_propose.reject();
        (pm_wait_propose = hsc->async_wait_proposal()).then(
                [this](const Proposal &prop) {
            last_proposed = prop.blk;
            locked = false;
            schedule_next();
            update_last_proposed();
        });
    }

    public:

    size_t get_pending_size() override { return pending_beats.size(); }

    void init() {
        last_proposed = hsc->get_genesis();
        locked = false;
        update_last_proposed();
    }

    ReplicaID get_proposer() override {
        return hsc->get_id();
    }

    block_t get_current_proposal() {
        return last_proposed;
    }

    promise_t beat() override {
        promise_t pm;
        pending_beats.push(pm);
        schedule_next();
        return pm;
    }

    promise_t beat_resp(ReplicaID last_proposer) override {
        return promise_t([last_proposer](promise_t &pm) {
            pm.resolve(last_proposer);
        });
    }
};

/** Naive PaceMaker where everyone can be a proposer at any moment. */
struct PaceMakerDummy: public PMHighTail, public PMWaitQC {
    PaceMakerDummy(int32_t parent_limit):
        PMHighTail(parent_limit), PMWaitQC() {}
    void init(HotStuffCore *hsc) override {
        PaceMaker::init(hsc);
        PMHighTail::init();
        PMWaitQC::init();
    }
};

/** PaceMakerDummy with a fixed proposer. */
class PaceMakerDummyFixed: public PaceMakerDummy {
    ReplicaID proposer;

    public:
    PaceMakerDummyFixed(ReplicaID proposer,
                        int32_t parent_limit):
        PaceMakerDummy(parent_limit),
        proposer(proposer) {}

    ReplicaID get_proposer() override {
        return proposer;
    }

    promise_t beat_resp(ReplicaID) override {
        return promise_t([this](promise_t &pm) {
            pm.resolve(proposer);
        });
    }
};

/** PaceMakerDummy with a fixed proposer. */
class PaceMakerDummyFixedTwo: public PaceMakerDummy {
    /** timer event.*/
    TimerEvent timer;
    double base_timeout;
    double prop_delay;
    double timeout;

    bool delaying_proposal = false;
    bool already_reconfigured = false;

    EventContext ec;

    ReplicaID proposer;
    size_t current_tid;


public:
    PaceMakerDummyFixedTwo(EventContext ec, int32_t parent_limit,
                         double base_timeout, double prop_delay):
            PaceMakerDummy(parent_limit),
            base_timeout(10),
            timeout(10),
            prop_delay(prop_delay),
            ec(std::move(ec)), proposer(0), 
            current_tid(0) {}

    ReplicaID get_proposer() override {
        return proposer;
    }

    promise_t beat_resp(ReplicaID) override {
        return promise_t([this](promise_t &pm) {
            pm.resolve(proposer);
        });
    }

    void proposer_timeout(TimerEvent &) {
        set_proposer(true);
    }

    void set_proposer(bool isTimeout) {

        /** Rotation happens according to the total trees in the system */
        current_tid = (current_tid + 1) % hsc->get_total_system_trees();
        update_tree_proposer();

        if(isTimeout) {
             timeout *= 2;
            if (timeout > (base_timeout * pow(2,4))) {
                timeout = (base_timeout * pow(2,4));
            }
        }

        HOTSTUFF_LOG_PROTO("-------------------------------");
        HOTSTUFF_LOG_PROTO("[PMAKER] Timeout reached!!!");

        vector<std::tuple<NetAddr, pubkey_bt, uint256_t>> reps;
        hsc->calcTreeForced(std::move(reps), false);

        if (get_proposer() == hsc->get_id()) {
            HOTSTUFF_LOG_PROTO("Elected itself as a new Leader!");
            delaying_proposal = true;
            timer = TimerEvent(ec, salticidae::generic_bind(&PaceMakerDummyFixedTwo::unlock, this, _1));
            timer.add(prop_delay);
        } else {
            timer = TimerEvent(ec, salticidae::generic_bind(&PaceMakerDummyFixedTwo::proposer_timeout, this, _1));
            timer.add(timeout);
        }

        HOTSTUFF_LOG_PROTO("[PMAKER] Finished recalculating tree!");
        HOTSTUFF_LOG_PROTO("-------------------------------");
    }

    void unlock(TimerEvent &) {
        timer.del();
        delaying_proposal = false;
        HOTSTUFF_LOG_PROTO("Unlocking Proposer!!!");
    }

    void inc_time(bool force) override {
        if (force) {
            //already_reconfigured = true;
            set_proposer(false);
        } else {
            HOTSTUFF_LOG_PROTO("Inc time %f", timeout);
            timer.del();
            timer = TimerEvent(ec, salticidae::generic_bind(&PaceMakerDummyFixedTwo::proposer_timeout, this, _1));
            timer.add(timeout);
        }
    }

    void schedule_next() override {
        if (!delaying_proposal) {
            PMWaitQC::schedule_next();
        }
    }

    void on_consensus(const block_t &blk) override {
        timer.del();
        //already_reconfigured = false;
    }

    size_t get_current_tid() override {
        return current_tid;
    }

    void update_tree_proposer() override {
        proposer = hsc->get_system_tree_root(current_tid);
        HOTSTUFF_LOG_PROTO("[PMAKER] Updated tree proposer to %d", proposer);
    }
};

/**
 * Simple long-standing round-robin style proposer liveness gadget.
 */
class PMRoundRobinProposer: virtual public PaceMaker {
    double base_timeout;
    double exp_timeout;
    double prop_delay;
    EventContext ec;
    /** QC timer or randomized timeout */
    TimerEvent timer;
    /** the proposer it believes */
    ReplicaID proposer;
    std::unordered_map<ReplicaID, block_t> prop_blk;
    bool rotating;

    /* extra state needed for a proposer */
    std::queue<promise_t> pending_beats;
    block_t last_proposed;
    bool locked;
    promise_t pm_qc_finish;
    promise_t pm_wait_propose;
    promise_t pm_qc_manual;

    void reg_proposal() {
        hsc->async_wait_proposal().then([this](const Proposal &prop) {
            auto &pblk = prop_blk[hsc->get_id()];
            if (!pblk) pblk = prop.blk;
            if (rotating) reg_proposal();
        });
    }

    void reg_receive_proposal() {
        hsc->async_wait_receive_proposal().then([this](const Proposal &prop) {
            auto &pblk = prop_blk[prop.proposer];
            if (!pblk) pblk = prop.blk;
            if (rotating) reg_receive_proposal();
        });
    }

    void proposer_schedule_next() {
        if (!pending_beats.empty() && !locked)
        {
            auto pm = pending_beats.front();
            pending_beats.pop();
            pm_qc_finish.reject();
            (pm_qc_finish = hsc->async_qc_finish(last_proposed))
                .then([this, pm]() {
                    HOTSTUFF_LOG_PROTO("got QC, propose a new block");
                    pm.resolve(proposer);
                });
            locked = true;
        }
    }

    void proposer_update_last_proposed() {
        pm_wait_propose.reject();
        (pm_wait_propose = hsc->async_wait_proposal()).then(
                [this](const Proposal &prop) {
            last_proposed = prop.blk;
            locked = false;
            proposer_schedule_next();
            proposer_update_last_proposed();
        });
    }

    void do_new_consensus(int x, const std::vector<uint256_t> &cmds) {
        auto blk = hsc->on_propose(cmds, get_parents(), bytearray_t());
        pm_qc_manual.reject();
        (pm_qc_manual = hsc->async_qc_finish(blk))
            .then([this, x]() {
                HOTSTUFF_LOG_PROTO("Pacemaker: got QC for block %d", x);
#ifdef HOTSTUFF_TWO_STEP
                if (x >= 2) return;
#else

                if (x >= 3) return;
#endif
                do_new_consensus(x + 1, std::vector<uint256_t>{});
            });
    }

    void on_exp_timeout(TimerEvent &) {
        if (proposer == hsc->get_id())
            do_new_consensus(0, std::vector<uint256_t>{});
        timer = TimerEvent(ec, [this](TimerEvent &){ rotate(); });
        timer.add(prop_delay);
    }

    /* role transitions */

    void rotate() {
        reg_proposal();
        reg_receive_proposal();
        prop_blk.clear();
        rotating = true;
        proposer = (proposer + 1) % hsc->get_config().nreplicas;
        HOTSTUFF_LOG_PROTO("Pacemaker: rotate to %d", proposer);
        pm_qc_finish.reject();
        pm_wait_propose.reject();
        pm_qc_manual.reject();
        // start timer
        timer = TimerEvent(ec, salticidae::generic_bind(&PMRoundRobinProposer::on_exp_timeout, this, _1));
        timer.add(exp_timeout);
        exp_timeout *= 2;
    }

    void stop_rotate() {
        timer.del();
        HOTSTUFF_LOG_PROTO("Pacemaker: stop rotation at %d", proposer);
        pm_qc_finish.reject();
        pm_wait_propose.reject();
        pm_qc_manual.reject();
        rotating = false;
        locked = false;
        last_proposed = hsc->get_genesis();
        proposer_update_last_proposed();
        if (proposer == hsc->get_id())
        {
            auto hs = static_cast<hotstuff::HotStuffBase *>(hsc);
            hs->do_elected();
            hs->get_tcall().async_call([this, hs](salticidae::ThreadCall::Handle &) {
                auto &pending = hs->get_decision_waiting();
                if (!pending.size()) return;
                HOTSTUFF_LOG_PROTO("reproposing pending commands");
                std::vector<uint256_t> cmds;
                for (auto &p: pending)
                    cmds.push_back(p.first);
                do_new_consensus(0, cmds);
            });
        }
    }

    protected:
    void on_consensus(const block_t &blk) override {
        timer.del();
        exp_timeout = base_timeout;
        if (prop_blk[proposer] == blk) {
            stop_rotate();
        }
    }

    void impeach() override {
        if (rotating) return;
        rotate();
        HOTSTUFF_LOG_INFO("schedule to impeach the proposer");
    }

    public:
    PMRoundRobinProposer(const EventContext &ec,
                        double base_timeout, double prop_delay):
        base_timeout(base_timeout),
        prop_delay(prop_delay),
        ec(ec), proposer(0), rotating(false) {}

    size_t get_pending_size() override { return pending_beats.size(); }

    void init() {
        exp_timeout = base_timeout;
        stop_rotate();
    }

    ReplicaID get_proposer() override {
        return proposer;
    }

    promise_t beat() override {
        if (!rotating && proposer == hsc->get_id())
        {
            promise_t pm;
            pending_beats.push(pm);
            proposer_schedule_next();
            return pm;
        }
        else
            return promise_t([proposer=proposer](promise_t &pm) {
                pm.resolve(proposer);
            });
    }

    promise_t beat_resp(ReplicaID last_proposer) override {
        return promise_t([this](promise_t &pm) {
            pm.resolve(proposer);
        });
    }
};

struct PaceMakerRR: public PMHighTail, public PMRoundRobinProposer {
    PaceMakerRR(EventContext ec, int32_t parent_limit,
                double base_timeout = 1, double prop_delay = 1):
        PMHighTail(parent_limit),
        PMRoundRobinProposer(ec, base_timeout, prop_delay) {}

    void init(HotStuffCore *hsc) override {
        PaceMaker::init(hsc);
        PMHighTail::init();
        PMRoundRobinProposer::init();
    }
};

/** PaceMakerDummy that rotates every X blocks. */
class PaceMakerRotating: public PaceMakerDummy {
    /** timer event.*/
    TimerEvent timer;
    double base_timeout;
    double exp_timeout;
    double prop_delay;
    double timeout;

    bool delaying_proposal = false;

    EventContext ec;

    ReplicaID proposer;
    size_t current_tid;

    std::unordered_map<ReplicaID, block_t> prop_blk;
    bool rotating;

    /* extra state needed for a proposer */
    std::queue<promise_t> pending_beats;
    block_t last_proposed;
    bool locked;
    promise_t pm_qc_finish;
    promise_t pm_wait_propose;
    promise_t pm_qc_manual;
    promise_t pm_qc_piped;

public:
    PaceMakerRotating(EventContext ec, int32_t parent_limit,
                         double base_timeout, double prop_delay):
            PaceMakerDummy(parent_limit),
            rotating(false),
            locked(false),
            base_timeout(10),
            timeout(10),
            exp_timeout(base_timeout),
            prop_delay(3),
            ec(std::move(ec)), proposer(0),
            current_tid(0) {}

    size_t get_pending_size() override { return pending_beats.size(); }

    /**
     * "Stop" state at initial proposer
    */
    void setup() {
        exp_timeout = base_timeout;
        stop_rotate();
    }

    void stop_rotate() {
        timer.del();

        HOTSTUFF_LOG_PROTO("Pacemaker: stop rotation at %d", proposer);
        pm_qc_finish.reject();
        pm_wait_propose.reject();
        pm_qc_manual.reject();
        pm_qc_piped.reject();
        rotating = false;
        locked = false;
        last_proposed = hsc->get_genesis();
        proposer_update_last_proposed();
        if (proposer == hsc->get_id())
        {
            auto hs = static_cast<hotstuff::HotStuffBase *>(hsc);
            hs->do_elected();
            hs->get_tcall().async_call([this, hs](salticidae::ThreadCall::Handle &) {
                auto &pending = hs->get_decision_waiting();
                if (!pending.size()) return;
                HOTSTUFF_LOG_PROTO("reproposing pending commands");
                std::vector<uint256_t> cmds;
                for (auto &p: pending)
                    cmds.push_back(p.first);
                do_new_consensus(0, cmds);
            });
        }
    }

    void do_new_consensus(int x, const std::vector<uint256_t> &cmds) {
        auto blk = hsc->on_propose(cmds, get_parents(), bytearray_t());
        pm_qc_manual.reject();
        (pm_qc_manual = hsc->async_qc_finish(blk))
            .then([this, x]() {
                HOTSTUFF_LOG_PROTO("Pacemaker: got QC for block %d", x);
#ifdef HOTSTUFF_TWO_STEP
                if (x >= 2 + hsc->get_config().async_blocks) return;
#else

                if (x >= 3 + hsc->get_config().async_blocks) return;
#endif
                do_new_consensus(x + 1, std::vector<uint256_t>{});
            });
    }

    void proposer_update_last_proposed() {
        pm_wait_propose.reject();
        (pm_wait_propose = hsc->async_wait_proposal()).then(
                [this](const Proposal &prop) {
            last_proposed = prop.blk;
            locked = false;
            proposer_schedule_next();
            proposer_update_last_proposed();
        });
    }

    void proposer_schedule_next() {
        if (!delaying_proposal) {
            if (!pending_beats.empty())
            {

                if (locked) {
                    struct timeval current_time;
                    gettimeofday(&current_time, NULL);

                    if (hsc->piped_queue.size() < hsc->get_config().async_blocks
                    && !hsc->piped_submitted
                    && ((current_time.tv_sec - hsc->last_block_time.tv_sec) * 1000000 + current_time.tv_usec - hsc->last_block_time.tv_usec) / 1000 > hsc->get_config().piped_latency) {
                        HOTSTUFF_LOG_PROTO("Extra block");
                        auto pm = pending_beats.front();
                        pending_beats.pop();
                        pm_qc_piped.reject();
                        hsc->piped_submitted = true;

                        (pm_qc_piped = hsc->async_qc_finish(last_proposed))
                            .then([this, pm]() {
                                pm.resolve(proposer);
                            });

                        return;
                    }

                    if (!hsc->piped_queue.empty() && hsc->b_normal_height > 0) {
                        block_t piped_block = hsc->storage->find_blk(hsc->piped_queue.back());
                        if ( piped_block->get_height() > hsc->get_config().async_blocks + 10 && hsc->b_normal_height < piped_block->get_height() - (hsc->get_config().async_blocks + 10)
                                && ((current_time.tv_sec - hsc->last_block_time.tv_sec) * 1000000 + current_time.tv_usec - hsc->last_block_time.tv_usec) / 1000 > hsc->get_config().piped_latency) {
                            HOTSTUFF_LOG_PROTO("Extra recovery block %d %d", hsc->b_normal_height, piped_block->get_height());

                            auto pm = pending_beats.front();
                            pending_beats.pop();
                            pm_qc_piped.reject();
                            hsc->piped_submitted = true;

                            (pm_qc_piped = hsc->async_qc_finish(last_proposed))
                                .then([this, pm]() {
                                    pm.resolve(proposer);
                                });
                        }
                    }
                }

                else {
                    auto pm = pending_beats.front();
                    pending_beats.pop();
                    pm_qc_finish.reject();
                    pm_qc_piped.reject();
                    (pm_qc_finish = hsc->async_qc_finish(last_proposed))
                        .then([this, pm]() {
                            HOTSTUFF_LOG_PROTO("got QC, propose a new block");
                            pm.resolve(proposer);
                        });
                    locked = true;
                }
            }
            else
            {
                std::cout << "not enough client tx" << std::endl;
            }
        }
    }


    /**
     * Rotate = reconfiguration call
     * Needs to maintain proper state
    */
    void rotate() {
        reg_proposal();
        reg_receive_proposal();
        prop_blk.clear();
        rotating = true;

        /** Rotation happens according to the total trees in the system */
        current_tid = (current_tid + 1) % hsc->get_total_system_trees();
        update_tree_proposer();

        HOTSTUFF_LOG_PROTO("-------------------------------");
        HOTSTUFF_LOG_PROTO("[PMAKER] Timeout reached!!!");
        HOTSTUFF_LOG_PROTO("[PMAKER] Rotate to %d", proposer);

        /** Actual tree reconfiguration */
        vector<std::tuple<NetAddr, pubkey_bt, uint256_t>> reps;
        hsc->calcTreeForced(std::move(reps), false);

        pm_qc_finish.reject();
        pm_wait_propose.reject();
        pm_qc_manual.reject();
        pm_qc_piped.reject();

        // start timer
        timer = TimerEvent(ec, salticidae::generic_bind(&PaceMakerRotating::on_exp_timeout, this, _1));
        timer.add(exp_timeout);

        /** Hard cap on exp_timeout */
        if (exp_timeout > (base_timeout * pow(2,4))) {
            exp_timeout = (base_timeout * pow(2,4));
        }
        else
            exp_timeout *= 2;

        HOTSTUFF_LOG_PROTO("[PMAKER] Finished recalculating tree!");
        HOTSTUFF_LOG_PROTO("-------------------------------");
    }

    void reg_proposal() {
        hsc->async_wait_proposal().then([this](const Proposal &prop) {
            auto &pblk = prop_blk[hsc->get_id()];
            if (!pblk) pblk = prop.blk;
            if (rotating) reg_proposal();
        });
    }

    void reg_receive_proposal() {
        hsc->async_wait_receive_proposal().then([this](const Proposal &prop) {
            auto &pblk = prop_blk[prop.proposer];
            if (!pblk) pblk = prop.blk;
            if (rotating) reg_receive_proposal();
        });
    }

    void on_exp_timeout(TimerEvent &) {
        if (proposer == hsc->get_id()) {
            do_new_consensus(0, std::vector<uint256_t>{});
        }
        timer = TimerEvent(ec, [this](TimerEvent &){ rotate(); });
        timer.add(prop_delay);
    }

    ReplicaID get_proposer() override {
        return proposer;
    }


    promise_t beat_resp(ReplicaID) override {
        return promise_t([this](promise_t &pm) {
            HOTSTUFF_LOG_PROTO("[TEST] Responding to beat");
            pm.resolve(proposer);
        });
    }


    void inc_time(bool force) override {
        if (force) {
            //if (rotating) stop_rotate(true);
            timer.del();
            rotate();
        } else {
            HOTSTUFF_LOG_PROTO("Inc time %f", exp_timeout);
            timer.add(exp_timeout);
        }
    }

    promise_t beat() override {
        if (proposer == hsc->get_id())
        {
            promise_t pm;
            pending_beats.push(pm);
            proposer_schedule_next();
            return pm;
        }
        else
            return promise_t([proposer=proposer](promise_t &pm) {
                pm.resolve(proposer);
            });
    }

    /**
     * On consensus stop the timer, rotation and reset the exp_timeout
    */
    void on_consensus(const block_t &blk) override {
        timer.del();
        exp_timeout = base_timeout;
        if (prop_blk[proposer] == blk) {
            stop_rotate();
        }
    }

    size_t get_current_tid() override {
        return current_tid;
    }

    void update_tree_proposer() override {
        proposer = hsc->get_system_tree_root(current_tid);
        HOTSTUFF_LOG_PROTO("[PMAKER] Updated tree proposer to %d", proposer);
    }

    // void impeach() override {
    //     if (rotating) return;
    //     rotate();
    //     HOTSTUFF_LOG_INFO("schedule to impeach the proposer");
    // }

};



// struct PaceMakerRR2: public PMHighTail, public PaceMakerRotating {
//     PaceMakerRR2(EventContext ec, int32_t parent_limit,
//                 double base_timeout = 1, double prop_delay = 1):
//         PMHighTail(parent_limit),
//         PaceMakerRotating(ec, base_timeout, prop_delay) {}

//     void init(HotStuffCore *hsc) override {
//         PaceMaker::init(hsc);
//         PMHighTail::init();
//         PaceMakerRotating::init();
//     }
// };


}

#endif
