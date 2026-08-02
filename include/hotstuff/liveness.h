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

#include <algorithm>
#include <chrono>
#include <condition_variable>
#include <memory>
#include <mutex>
#include <stdexcept>
#include <unordered_map>

#include "salticidae/util.h"
#include "hotstuff/hotstuff.h"
#include "hotstuff/leader_progress.h"

namespace hotstuff
{

    using salticidae::_1;
    using salticidae::_2;

    /** Abstraction for liveness gadget (oracle). */
    class PaceMaker
    {
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
        virtual void inc_time(ReconfigurationType reconfig_type) {}
        virtual size_t get_current_tid() {}
        virtual size_t get_current_epoch() {}
        virtual void update_tree_proposer() {}
        virtual void setup() {}
        virtual bool configure_leader_progress(
            LeaderProgressConfig::Duration)
        {
            return true;
        }
        virtual bool activate_leader_view(
            const ConfigurationId &, ReplicaID)
        {
            return true;
        }
        virtual bool activate_leader_view(const LeaderViewId &)
        {
            return true;
        }
        virtual bool activate_runtime_view(const LeaderViewId &view)
        {
            return activate_leader_view(view);
        }
        virtual bool record_verified_progress(
            const ConfigurationId &, LeaderProgressEvent)
        {
            return false;
        }
        virtual std::optional<LeaderViewId> active_leader_view() const
        {
            return std::nullopt;
        }
        virtual void shutdown() {}

        virtual block_t get_current_proposal() {}
    };

    using pacemaker_bt = BoxObj<PaceMaker>;

    /** Parent selection implementation for PaceMaker: select the highest tail that
     * follows the current hqc block. */
    class PMHighTail : public virtual PaceMaker
    {
    public:
        block_t hqc_tail;
        const int32_t parent_limit; /**< maximum number of parents */

        bool check_ancestry(const block_t &_a, const block_t &_b)
        {
            block_t b;
            for (b = _b;
                 b->get_height() > _a->get_height();
                 b = b->get_parents()[0])
                ;
            return b == _a;
        }

        void reg_hqc_update()
        {
            hsc->async_hqc_update().then([this](const block_t &hqc)
                                         {
            hqc_tail = hqc;
            for (const auto &tail: hsc->get_tails())
                if (check_ancestry(hqc, tail) && tail->get_height() > hqc_tail->get_height())
                    hqc_tail = tail;
            reg_hqc_update(); });
        }

        void reg_proposal()
        {
            hsc->async_wait_proposal().then([this](const Proposal &prop)
                                            {
            hqc_tail = prop.blk;
            reg_proposal(); });
        }

        void reg_receive_proposal()
        {
            hsc->async_wait_receive_proposal().then([this](const Proposal &prop)
                                                    {
            const auto &hqc = hsc->get_hqc();
            const auto &blk = prop.blk;
            if (check_ancestry(hqc, blk) && blk->get_height() > hqc_tail->get_height())
                hqc_tail = blk;
            reg_receive_proposal(); });
        }

    public:
        PMHighTail(int32_t parent_limit) : parent_limit(parent_limit) {}
        void init()
        {
            hqc_tail = hsc->get_genesis();
            reg_hqc_update();
            reg_proposal();
            reg_receive_proposal();
        }

        std::vector<block_t> get_parents() override
        {
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
    class PMWaitQC : public virtual PaceMaker
    {

    public:
        std::queue<promise_t> pending_beats;
        block_t last_proposed;
        bool locked;
        promise_t pm_wait_propose;

    protected:
        std::uint64_t beat_lane_generation{0};

        void retire_beat_lane(const block_t &rebase)
        {
            ++beat_lane_generation;
            while (!pending_beats.empty())
            {
                pending_beats.front().reject();
                pending_beats.pop();
            }
            if (rebase != nullptr)
            {
                auto &piped = hsc->piped_queue;
                piped.erase(
                    std::remove_if(
                        piped.begin(), piped.end(),
                        [this, &rebase](const uint256_t &hash)
                        {
                            const auto block = hsc->storage->find_blk(hash);
                            return block == nullptr ||
                                   block->get_height() <=
                                       rebase->get_height();
                        }),
                    piped.end());
                auto &ready = hsc->rdy_queue;
                ready.erase(
                    std::remove_if(
                        ready.begin(), ready.end(),
                        [&piped](const uint256_t &hash)
                        {
                            return std::find(
                                       piped.begin(), piped.end(), hash) ==
                                   piped.end();
                        }),
                    ready.end());
            }
            hsc->piped_submitted = false;
            last_proposed = rebase;
            locked = false;
        }

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

        virtual void schedule_next()
        {
            if (!pending_beats.empty())
            {
                if (locked)
                {
                    struct timeval current_time;
                    gettimeofday(&current_time, NULL);

                    if (hsc->piped_queue.size() < hsc->get_config().async_blocks && !hsc->piped_submitted && ((current_time.tv_sec - hsc->last_block_time.tv_sec) * 1000000 + current_time.tv_usec - hsc->last_block_time.tv_usec) / 1000 > hsc->get_config().piped_latency)
                    {
                        HOTSTUFF_LOG_PROTO("schedule_next: popping beat as piped block");
                        HOTSTUFF_LOG_PROTO("Extra block");
                        auto pm = pending_beats.front();
                        pending_beats.pop();
                        hsc->piped_submitted = true;
                        pm.resolve(get_proposer());
                        return;
                    }

                    if (!hsc->piped_queue.empty() && hsc->b_normal_height > 0)
                    {
                        block_t piped_block = hsc->storage->find_blk(hsc->piped_queue.back());
                        if (piped_block->get_height() > hsc->get_config().async_blocks + 10 && hsc->b_normal_height < piped_block->get_height() - (hsc->get_config().async_blocks + 10) && ((current_time.tv_sec - hsc->last_block_time.tv_sec) * 1000000 + current_time.tv_usec - hsc->last_block_time.tv_usec) / 1000 > hsc->get_config().piped_latency)
                        {
                            HOTSTUFF_LOG_PROTO("schedule_next: popping beat as piped recovery block");
                            HOTSTUFF_LOG_PROTO("Extra recovery block %d %d", hsc->b_normal_height, piped_block->get_height());

                            auto pm = pending_beats.front();
                            pending_beats.pop();
                            hsc->piped_submitted = true;
                            pm.resolve(get_proposer());
                        }
                    }
                }
                else
                {
                    HOTSTUFF_LOG_PROTO("schedule_next: popping beat as normal block");
                    auto pm = pending_beats.front();
                    pending_beats.pop();
                    const auto generation = beat_lane_generation;
                    hsc->async_qc_finish(last_proposed).then(
                        [this, pm, generation]()
                        {
                            if (generation != beat_lane_generation)
                            {
                                pm.reject();
                                return;
                            }
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

        void update_last_proposed()
        {
            pm_wait_propose.reject();
            (pm_wait_propose = hsc->async_wait_proposal()).then([this](const Proposal &prop)
                                                                {
            last_proposed = prop.blk;
            locked = false;
            schedule_next();
            update_last_proposed(); });
        }

    public:
        size_t get_pending_size() override { return pending_beats.size(); }

        void init()
        {
            last_proposed = hsc->get_genesis();
            locked = false;
            update_last_proposed();
        }

        ReplicaID get_proposer() override
        {
            return hsc->get_id();
        }

        block_t get_current_proposal()
        {
            return last_proposed;
        }

        promise_t beat() override
        {
            promise_t pm;
            pending_beats.push(pm);
            schedule_next();
            return pm;
        }

        promise_t beat_resp(ReplicaID last_proposer) override
        {
            return promise_t([last_proposer](promise_t &pm)
                             { pm.resolve(last_proposer); });
        }
    };

    /** Naive PaceMaker where everyone can be a proposer at any moment. */
    struct PaceMakerDummy : public PMHighTail, public PMWaitQC
    {
        PaceMakerDummy(int32_t parent_limit) : PMHighTail(parent_limit), PMWaitQC() {}
        void init(HotStuffCore *hsc) override
        {
            PaceMaker::init(hsc);
            PMHighTail::init();
            PMWaitQC::init();
        }
    };

    /** PaceMakerDummy with a fixed proposer. */
    class PaceMakerDummyFixed : public PaceMakerDummy
    {
        ReplicaID proposer;

    public:
        PaceMakerDummyFixed(ReplicaID proposer,
                            int32_t parent_limit) : PaceMakerDummy(parent_limit),
                                                    proposer(proposer) {}

        ReplicaID get_proposer() override
        {
            return proposer;
        }

        promise_t beat_resp(ReplicaID) override
        {
            return promise_t([this](promise_t &pm)
                             { pm.resolve(proposer); });
        }
    };

    class SalticidaeLeaderProgressScheduler final
        : public LeaderProgressScheduler
    {
        struct State
        {
            std::mutex mutex;
            std::condition_variable idle;
            std::uint64_t next_id{0};
            std::unordered_map<
                std::uint64_t,
                std::shared_ptr<TimerEvent>> timers;
            bool closed{false};
            std::size_t active_callbacks{0};
        };

        EventContext ec;
        std::shared_ptr<State> state;

    public:
        explicit SalticidaeLeaderProgressScheduler(
            const EventContext &ec)
            : ec(ec), state(std::make_shared<State>())
        {}

        ~SalticidaeLeaderProgressScheduler() override
        {
            shutdown();
        }

        Cancellation schedule_after(
            Duration delay, Callback callback) override
        {
            if (delay <= Duration::zero() || !callback)
                return {};

            std::uint64_t timer_id;
            {
                std::lock_guard<std::mutex> lock(state->mutex);
                if (state->closed)
                    return {};
                timer_id = ++state->next_id;
            }

            const std::weak_ptr<State> weak_state(state);
            auto timer = std::make_shared<TimerEvent>(
                ec,
                [weak_state,
                 timer_id,
                 callback = std::move(callback)](TimerEvent &) mutable
                {
                    const auto active_state = weak_state.lock();
                    if (active_state == nullptr)
                        return;
                    std::shared_ptr<TimerEvent> keep_alive;
                    {
                        std::lock_guard<std::mutex> lock(
                            active_state->mutex);
                        const auto found =
                            active_state->timers.find(timer_id);
                        if (found == active_state->timers.end())
                            return;
                        keep_alive = std::move(found->second);
                        active_state->timers.erase(found);
                        ++active_state->active_callbacks;
                    }
                    try
                    {
                        callback();
                    }
                    catch (...)
                    {
                        std::lock_guard<std::mutex> lock(
                            active_state->mutex);
                        if (--active_state->active_callbacks == 0)
                            active_state->idle.notify_all();
                        throw;
                    }
                    {
                        std::lock_guard<std::mutex> lock(
                            active_state->mutex);
                        if (--active_state->active_callbacks == 0)
                            active_state->idle.notify_all();
                    }
                });
            {
                std::lock_guard<std::mutex> lock(state->mutex);
                if (state->closed)
                    return {};
                state->timers.emplace(timer_id, timer);
                timer->add(
                    std::chrono::duration<double>(delay).count());
            }

            return [weak_state, timer_id]()
            {
                const auto active_state = weak_state.lock();
                if (active_state == nullptr)
                    return;
                std::shared_ptr<TimerEvent> timer;
                {
                    std::lock_guard<std::mutex> lock(
                        active_state->mutex);
                    const auto found =
                        active_state->timers.find(timer_id);
                    if (found == active_state->timers.end())
                        return;
                    timer = std::move(found->second);
                    active_state->timers.erase(found);
                }
                timer->del();
            };
        }

        void shutdown()
        {
            std::vector<std::shared_ptr<TimerEvent>> timers;
            {
                std::lock_guard<std::mutex> lock(state->mutex);
                if (state->closed)
                    return;
                state->closed = true;
                timers.reserve(state->timers.size());
                for (auto &entry : state->timers)
                    timers.push_back(std::move(entry.second));
                state->timers.clear();
            }
            for (const auto &timer : timers)
                timer->del();
            std::unique_lock<std::mutex> lock(state->mutex);
            state->idle.wait(lock, [this]()
                             { return state->active_callbacks == 0; });
        }
    };

    /** PaceMaker that switches alongside the scheduled trees. */
    class PaceMakerMultitree : public PaceMakerDummy
    {
        TimerEvent timer;
        double base_timeout;
        double prop_delay;
        double leader_progress_timeout_seconds;
        double leader_activation_grace_seconds;
        bool delaying_proposal{false};
        EventContext ec;
        ReplicaID proposer{0};
        size_t current_tid{0};
        size_t current_epoch{0};
        std::uint64_t view_generation{0};
        bool runtime_leader_handoff_ready{false};
        SalticidaeLeaderProgressScheduler leader_progress_scheduler;
        std::unique_ptr<LeaderProgressMonitor> leader_progress;
        promise_t pm_qc_manual;

        static LeaderProgressConfig::Duration seconds(double value)
        {
            if (!(value > 0.0))
                throw std::invalid_argument(
                    "leader progress durations must be positive");
            return std::chrono::duration_cast<
                LeaderProgressConfig::Duration>(
                std::chrono::duration<double>(value));
        }

        void arm_proposal_delay()
        {
            timer.del();
            if (get_proposer() == hsc->get_id())
            {
                delaying_proposal = true;
                timer = TimerEvent(
                    ec,
                    salticidae::generic_bind(
                        &PaceMakerMultitree::unlock, this, _1));
                timer.add(prop_delay);
            }
            else
            {
                delaying_proposal = false;
            }
        }

        void arm_runtime_leader_handoff()
        {
            retire_beat_lane(hsc->get_hqc());
            runtime_leader_handoff_ready =
                proposer == hsc->get_id();
            arm_proposal_delay();
        }

    public:
        PaceMakerMultitree(EventContext ec,
                           int32_t parent_limit,
                           double base_timeout,
                           double prop_delay,
                           double leader_progress_timeout,
                           double leader_activation_grace)
            : PaceMakerDummy(parent_limit),
              base_timeout(base_timeout),
              prop_delay(prop_delay),
              leader_progress_timeout_seconds(leader_progress_timeout),
              leader_activation_grace_seconds(leader_activation_grace),
              ec(std::move(ec)),
              leader_progress_scheduler(this->ec)
        {
            if (!(this->base_timeout > 0.0) ||
                !(this->prop_delay >= 0.0))
                throw std::invalid_argument(
                    "pacemaker timing values are invalid");
        }

        bool configure_leader_progress(
            LeaderProgressConfig::Duration maximum_aggregation_timeout)
            override
        {
            if (leader_progress != nullptr)
                return false;
            LeaderProgressEffects effects;
            effects.rotate_active_view =
                [this](const LeaderViewId &expired_view)
                {
                    rotate_active_tree_on_timeout(expired_view);
                };
            leader_progress = std::make_unique<LeaderProgressMonitor>(
                LeaderProgressConfig{
                    seconds(leader_activation_grace_seconds),
                    seconds(leader_progress_timeout_seconds),
                    maximum_aggregation_timeout,
                    {LeaderProgressEvent::verified_proposal,
                     LeaderProgressEvent::quorum_certificate,
                     LeaderProgressEvent::commit}},
                std::move(effects));
            return true;
        }

        bool activate_leader_view(const LeaderViewId &view) override
        {
            if (leader_progress == nullptr)
                return false;
            const auto active = leader_progress->active_view();
            if (active.has_value() && *active == view)
                return true;
            if (!leader_progress->activate(
                    view, leader_progress_scheduler))
                return false;
            view_generation = view.view_generation;
            return true;
        }

        bool activate_leader_view(
            const ConfigurationId &configuration,
            ReplicaID leader) override
        {
            if (leader_progress == nullptr)
                return false;
            const auto active = leader_progress->active_view();
            if (active.has_value() &&
                active->configuration == configuration &&
                active->leader_id == leader)
                return true;
            return activate_leader_view(LeaderViewId{
                configuration, view_generation + 1, leader});
        }

        bool activate_runtime_view(const LeaderViewId &view) override
        {
            const bool already_active =
                current_epoch == view.configuration.epoch_number &&
                current_tid == view.configuration.tree_id &&
                proposer == view.leader_id &&
                view_generation == view.view_generation;
            if (!activate_leader_view(view))
                return false;
            current_epoch = view.configuration.epoch_number;
            current_tid = view.configuration.tree_id;
            proposer = view.leader_id;
            view_generation = view.view_generation;
            if (!already_active)
                arm_runtime_leader_handoff();
            return true;
        }

        bool record_verified_progress(
            const ConfigurationId &configuration,
            LeaderProgressEvent event) override
        {
            if (leader_progress == nullptr)
                return false;
            const auto active = leader_progress->active_view();
            if (!active.has_value() ||
                active->configuration != configuration)
                return false;
            return leader_progress->record_verified_progress(
                *active, event, leader_progress_scheduler);
        }

        std::optional<LeaderViewId> active_leader_view() const override
        {
            if (leader_progress == nullptr)
                return std::nullopt;
            return leader_progress->active_view();
        }

        void rotate_active_tree_on_timeout(
            const LeaderViewId &expired_view)
        {
            if (hsc == nullptr || leader_progress == nullptr ||
                hsc->get_total_system_trees() == 0)
                return;
            const auto active = leader_progress->active_view();
            if (!active.has_value() || *active != expired_view)
                return;

            const auto delegated =
                hsc->rotate_tree_on_leader_timeout(expired_view);
            if (delegated !=
                LeaderTimeoutRotationDisposition::legacy_fallback)
                return;

            const auto next_tid =
                (current_tid + 1) % hsc->get_total_system_trees();
            const auto configuration =
                hsc->get_exact_tree_configuration(
                    static_cast<std::uint32_t>(current_epoch),
                    static_cast<std::uint32_t>(next_tid));
            LeaderViewId next_view{
                configuration,
                expired_view.view_generation + 1,
                hsc->get_exact_tree_root(
                    static_cast<std::uint32_t>(current_epoch),
                    static_cast<std::uint32_t>(next_tid))};
            if (!activate_leader_view(next_view))
                return;

            current_tid = next_tid;
            update_tree_proposer();
            vector<std::tuple<NetAddr, pubkey_bt, uint256_t>> replicas;
            hsc->tree_scheduler(std::move(replicas), false);
            arm_proposal_delay();
        }

        bool set_proposer(bool is_timeout, bool epoch_change)
        {
            if (is_timeout)
            {
                const auto active = active_leader_view();
                if (!active.has_value())
                    return false;
                rotate_active_tree_on_timeout(*active);
                return active_leader_view() != active;
            }
            if (hsc == nullptr || hsc->get_total_system_trees() == 0)
                return false;

            const auto target_epoch =
                current_epoch + (epoch_change ? 1 : 0);
            const auto target_tid = epoch_change
                                        ? size_t{0}
                                        : (current_tid + 1) %
                                              hsc->get_total_system_trees();
            const auto configuration =
                hsc->get_exact_tree_configuration(
                    static_cast<std::uint32_t>(target_epoch),
                    static_cast<std::uint32_t>(target_tid));
            LeaderViewId next_view{
                configuration,
                view_generation + 1,
                hsc->get_exact_tree_root(
                    static_cast<std::uint32_t>(target_epoch),
                    static_cast<std::uint32_t>(target_tid))};
            if (!activate_leader_view(next_view))
                return false;

            current_epoch = target_epoch;
            current_tid = target_tid;
            if (epoch_change)
                hsc->update_system_trees();
            update_tree_proposer();
            vector<std::tuple<NetAddr, pubkey_bt, uint256_t>> replicas;
            hsc->tree_scheduler(std::move(replicas), false);
            arm_proposal_delay();
            return true;
        }

        ReplicaID get_proposer() override
        {
            return proposer;
        }

        promise_t beat_resp(ReplicaID) override
        {
            return promise_t([this](promise_t &pm)
                             { pm.resolve(proposer); });
        }

        void unlock(TimerEvent &)
        {
            timer.del();
            delaying_proposal = false;
            locked = false;
            HOTSTUFF_LOG_INFO(
                "KAURI_LOCAL_PROPOSAL "
                "stage=unlock_schedule_begin replica=%u epoch=%zu "
                "tree=%zu proposer=%u handoff_ready=%u pending=%zu",
                static_cast<unsigned>(hsc->get_id()),
                current_epoch,
                current_tid,
                static_cast<unsigned>(proposer),
                runtime_leader_handoff_ready ? 1U : 0U,
                pending_beats.size());
            schedule_next();
            HOTSTUFF_LOG_INFO(
                "KAURI_LOCAL_PROPOSAL "
                "stage=unlock_schedule_end replica=%u epoch=%zu "
                "tree=%zu proposer=%u handoff_ready=%u pending=%zu",
                static_cast<unsigned>(hsc->get_id()),
                current_epoch,
                current_tid,
                static_cast<unsigned>(proposer),
                runtime_leader_handoff_ready ? 1U : 0U,
                pending_beats.size());
        }

        void inc_time(ReconfigurationType reconfig_type) override
        {
            auto *hsb = dynamic_cast<HotStuffBase *>(hsc);
            if (hsb == nullptr)
                return;
            switch (reconfig_type)
            {
            case TREE_SWITCH:
                if (set_proposer(false, false))
                    hsb->increment_reconfig_count();
                break;
            case EPOCH_SWITCH:
                if (set_proposer(false, true))
                    hsb->increment_reconfig_count();
                break;
            case NO_SWITCH:
                break;
            default:
                HOTSTUFF_LOG_PROTO("Unknown Reconfiguration Type");
                break;
            }
        }

        void schedule_next() override
        {
            if (delaying_proposal)
                return;
            if (runtime_leader_handoff_ready &&
                !pending_beats.empty())
            {
                auto pm = pending_beats.front();
                pending_beats.pop();
                runtime_leader_handoff_ready = false;
                locked = true;
                pm.resolve(get_proposer());
                return;
            }
            PMWaitQC::schedule_next();
        }

        void on_consensus(const block_t &) override {}

        size_t get_current_tid() override
        {
            return current_tid;
        }

        size_t get_current_epoch() override
        {
            return current_epoch;
        }

        void update_tree_proposer() override
        {
            proposer = hsc->get_system_tree_root(current_tid);
            HOTSTUFF_LOG_PROTO(
                "[PMAKER] Updated tree proposer to %d", proposer);
        }

        void shutdown() override
        {
            if (leader_progress != nullptr)
                leader_progress->shutdown();
            leader_progress_scheduler.shutdown();
            timer.del();
        }

        void do_new_consensus(
            int x, const std::vector<uint256_t> &cmds)
        {
            auto blk = hsc->on_propose(
                cmds, get_parents(), bytearray_t());
            pm_qc_manual.reject();
            (pm_qc_manual = hsc->async_qc_finish(blk))
                .then([this, x]()
                      {
                HOTSTUFF_LOG_PROTO("Pacemaker: got QC for block %d", x);
#ifdef HOTSTUFF_TWO_STEP
                if (x >= 2) return;
#else
                if (x >= 3) return;
#endif
                do_new_consensus(x + 1, std::vector<uint256_t>{}); });
        }
    };

    /**
     * Simple long-standing round-robin style proposer liveness gadget.
     */
    class PMRoundRobinProposer : virtual public PaceMaker
    {
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

        void reg_proposal()
        {
            hsc->async_wait_proposal().then([this](const Proposal &prop)
                                            {
            auto &pblk = prop_blk[hsc->get_id()];
            if (!pblk) pblk = prop.blk;
            if (rotating) reg_proposal(); });
        }

        void reg_receive_proposal()
        {
            hsc->async_wait_receive_proposal().then([this](const Proposal &prop)
                                                    {
            auto &pblk = prop_blk[prop.proposer];
            if (!pblk) pblk = prop.blk;
            if (rotating) reg_receive_proposal(); });
        }

        void proposer_schedule_next()
        {
            if (!pending_beats.empty() && !locked)
            {
                auto pm = pending_beats.front();
                pending_beats.pop();
                pm_qc_finish.reject();
                (pm_qc_finish = hsc->async_qc_finish(last_proposed))
                    .then([this, pm]()
                          {
                    HOTSTUFF_LOG_PROTO("got QC, propose a new block");
                    pm.resolve(proposer); });
                locked = true;
            }
        }

        void proposer_update_last_proposed()
        {
            pm_wait_propose.reject();
            (pm_wait_propose = hsc->async_wait_proposal()).then([this](const Proposal &prop)
                                                                {
            last_proposed = prop.blk;
            locked = false;
            proposer_schedule_next();
            proposer_update_last_proposed(); });
        }

        void do_new_consensus(int x, const std::vector<uint256_t> &cmds)
        {
            auto blk = hsc->on_propose(cmds, get_parents(), bytearray_t());
            pm_qc_manual.reject();
            (pm_qc_manual = hsc->async_qc_finish(blk))
                .then([this, x]()
                      {
                HOTSTUFF_LOG_PROTO("Pacemaker: got QC for block %d", x);
#ifdef HOTSTUFF_TWO_STEP
                if (x >= 2) return;
#else

                if (x >= 3) return;
#endif
                do_new_consensus(x + 1, std::vector<uint256_t>{}); });
        }

        void on_exp_timeout(TimerEvent &)
        {
            if (proposer == hsc->get_id())
                do_new_consensus(0, std::vector<uint256_t>{});
            timer = TimerEvent(ec, [this](TimerEvent &)
                               { rotate(); });
            timer.add(prop_delay);
        }

        /* role transitions */

        void rotate()
        {
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

        void stop_rotate()
        {
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
                hs->get_tcall().async_call([this, hs](salticidae::ThreadCall::Handle &)
                                           {
                auto &pending = hs->get_decision_waiting();
                if (!pending.size()) return;
                HOTSTUFF_LOG_PROTO("reproposing pending commands");
                std::vector<uint256_t> cmds;
                for (auto &p: pending)
                    cmds.push_back(p.first);
                do_new_consensus(0, cmds); });
            }
        }

    protected:
        void on_consensus(const block_t &blk) override
        {
            timer.del();
            exp_timeout = base_timeout;
            if (prop_blk[proposer] == blk)
            {
                stop_rotate();
            }
        }

        void impeach() override
        {
            if (rotating)
                return;
            rotate();
            HOTSTUFF_LOG_INFO("schedule to impeach the proposer");
        }

    public:
        PMRoundRobinProposer(const EventContext &ec,
                             double base_timeout, double prop_delay) : base_timeout(base_timeout),
                                                                       prop_delay(prop_delay),
                                                                       ec(ec), proposer(0), rotating(false) {}

        size_t get_pending_size() override { return pending_beats.size(); }

        void init()
        {
            exp_timeout = base_timeout;
            stop_rotate();
        }

        ReplicaID get_proposer() override
        {
            return proposer;
        }

        promise_t beat() override
        {
            if (!rotating && proposer == hsc->get_id())
            {
                promise_t pm;
                pending_beats.push(pm);
                proposer_schedule_next();
                return pm;
            }
            else
                return promise_t([proposer = proposer](promise_t &pm)
                                 { pm.resolve(proposer); });
        }

        promise_t beat_resp(ReplicaID last_proposer) override
        {
            return promise_t([this](promise_t &pm)
                             { pm.resolve(proposer); });
        }
    };

    struct PaceMakerRR : public PMHighTail, public PMRoundRobinProposer
    {
        PaceMakerRR(EventContext ec, int32_t parent_limit,
                    double base_timeout = 1, double prop_delay = 1) : PMHighTail(parent_limit),
                                                                      PMRoundRobinProposer(ec, base_timeout, prop_delay) {}

        void init(HotStuffCore *hsc) override
        {
            PaceMaker::init(hsc);
            PMHighTail::init();
            PMRoundRobinProposer::init();
        }
    };

}

#endif
