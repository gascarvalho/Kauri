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
#include "hotstuff/proposal_body.h"

#include <algorithm>
#include <condition_variable>
#include <ctime>
#include <cstring>
#include <limits>
#include <random>
#include <future>
#include <iostream>
#include <fstream>
#include <iterator>
#include <sstream>
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

    namespace
    {
        constexpr std::uint32_t exact_forwarding_max_attempts = 3;
        constexpr auto exact_forwarding_retry_base_delay =
            std::chrono::milliseconds(5);

        bool signer_sets_overlap(
            const std::set<ReplicaID> &left,
            const std::set<ReplicaID> &right)
        {
            const auto &smaller = left.size() <= right.size()
                                      ? left
                                      : right;
            const auto &larger = left.size() <= right.size()
                                     ? right
                                     : left;
            return std::any_of(
                smaller.begin(), smaller.end(),
                [&larger](ReplicaID signer) {
                    return larger.count(signer) != 0;
                });
        }

        AggregationScheduler::Duration adaptive_timeout_from_seconds(
            double seconds)
        {
            if (!(seconds > 0.0))
                throw std::invalid_argument(
                    "adaptive timeout must be positive");
            return std::chrono::duration_cast<
                AggregationScheduler::Duration>(
                std::chrono::duration<double>(seconds));
        }

        EpochChangeProposalChainResult bypass_epoch_change_gate() noexcept
        {
            EpochChangeProposalChainResult result;
            result.disposition = EpochChangeProposalDisposition::accepted;
            result.history_error = EpochChangeProposalHistoryError::none;
            result.wire_error = EpochChangeWireError::none;
            return result;
        }

        EpochChangeProposalChainResult reject_epoch_change_gate() noexcept
        {
            EpochChangeProposalChainResult result;
            result.disposition = EpochChangeProposalDisposition::rejected;
            result.history_error = EpochChangeProposalHistoryError::none;
            result.wire_error = EpochChangeWireError::internal_failure;
            return result;
        }

        class SalticidaeAggregationScheduler final
            : public AggregationScheduler
        {
            struct State
            {
                std::mutex mutex;
                std::uint64_t next_id{0};
                std::unordered_map<
                    std::uint64_t,
                    std::shared_ptr<salticidae::TimerEvent>>
                    timers;
            };

            EventContext ec;
            std::shared_ptr<State> state;

        public:
            explicit SalticidaeAggregationScheduler(
                const EventContext &ec)
                : ec(ec), state(std::make_shared<State>())
            {}

            ~SalticidaeAggregationScheduler() override
            {
                std::vector<std::shared_ptr<salticidae::TimerEvent>>
                    timers;
                {
                    std::lock_guard<std::mutex> lock(state->mutex);
                    timers.reserve(state->timers.size());
                    for (auto &entry : state->timers)
                        timers.push_back(std::move(entry.second));
                    state->timers.clear();
                }
                for (const auto &timer : timers)
                    timer->del();
            }

            Cancellation schedule_after(
                Duration delay, Callback callback) override
            {
                if (delay <= Duration::zero() || !callback)
                    return {};

                std::uint64_t timer_id;
                {
                    std::lock_guard<std::mutex> lock(state->mutex);
                    timer_id = ++state->next_id;
                }

                const std::weak_ptr<State> weak_state(state);
                auto timer = std::make_shared<salticidae::TimerEvent>(
                    ec,
                    [weak_state,
                     timer_id,
                     callback = std::move(callback)](
                        salticidae::TimerEvent &) mutable
                    {
                        const auto active_state = weak_state.lock();
                        if (active_state == nullptr)
                            return;
                        std::shared_ptr<salticidae::TimerEvent> keep_alive;
                        {
                            std::lock_guard<std::mutex> lock(
                                active_state->mutex);
                            const auto found =
                                active_state->timers.find(timer_id);
                            if (found == active_state->timers.end())
                                return;
                            keep_alive = std::move(found->second);
                            active_state->timers.erase(found);
                        }
                        callback();
                    });
                {
                    std::lock_guard<std::mutex> lock(state->mutex);
                    state->timers.emplace(timer_id, timer);
                }
                timer->add(
                    std::chrono::duration<double>(delay).count());

                return [weak_state, timer_id]()
                {
                    const auto active_state = weak_state.lock();
                    if (active_state == nullptr)
                        return;
                    std::shared_ptr<salticidae::TimerEvent> timer;
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
        };
    }

    const TreeNetwork *find_message_tree(const std::vector<Epoch> &epochs,
                                         uint32_t epoch_nr,
                                         uint32_t tree_id) noexcept
    {
        if (epoch_nr >= epochs.size())
            return nullptr;

        const auto &epoch = epochs[epoch_nr];
        if (epoch.get_epoch_num() != epoch_nr)
            return nullptr;

        for (const auto &tree : epoch.get_tree_networks())
            if (tree.get_tree().get_tid() == tree_id)
                return &tree;
        return nullptr;
    }

    bool validate_authenticated_vote(const ReplicaConfig &config,
                                     const PeerId &authenticated_peer,
                                     const Vote &vote) noexcept
    {
        try
        {
            return vote.cert != nullptr &&
                   vote.cert->get_proposal_key() == vote.key() &&
                   config.get_peer_id(vote.voter) == authenticated_peer;
        }
        catch (const std::exception &)
        {
            return false;
        }
    }

    bool validate_relay_envelope(const ReplicaConfig &config,
                                 const VoteRelay &relay) noexcept
    {
        try
        {
            return relay.cert != nullptr &&
                   relay.cert->get_proposal_key() == relay.key() &&
                   relay.cert->get_sigs_n() > 0 &&
                   relay.cert->get_sigs_n() <= config.nreplicas;
        }
        catch (const std::exception &)
        {
            return false;
        }
    }

    void validate_relay_wire_bounds(DataStream serialized,
                                    std::size_t replica_count)
    {
        std::uint32_t epoch_number{0};
        std::uint32_t tree_id{0};
        uint256_t epoch_digest;
        uint256_t block_hash;
        serialized >> epoch_number >> tree_id >> epoch_digest >> block_hash;

        ProposalKey certificate_key;
        unserialize_proposal_key(serialized, certificate_key);

        std::uint32_t encoded_bits{0};
        serialized >> encoded_bits;
        const auto bit_count =
            static_cast<std::size_t>(letoh(encoded_bits));
        if (bit_count != replica_count)
            throw std::invalid_argument(
                "relay signer bitmap does not match membership");

        constexpr std::size_t kBitsPerWord = sizeof(std::uint64_t) * 8;
        const auto word_count =
            (bit_count + kBitsPerWord - 1) / kBitsPerWord;
        if (word_count > serialized.size() / sizeof(std::uint64_t))
            throw std::invalid_argument(
                "relay signer bitmap exceeds the remaining payload");
    }

    bool verify_authenticated_vote(const ReplicaConfig &config,
                                   const PeerId &authenticated_peer,
                                   const Vote &vote) noexcept
    {
        if (!validate_authenticated_vote(config, authenticated_peer, vote))
            return false;
        try
        {
            return vote.cert->verify(config.get_pubkey(vote.voter));
        }
        catch (const std::exception &)
        {
            return false;
        }
    }

    bool verify_relay_certificate(const ReplicaConfig &config,
                                  const VoteRelay &relay) noexcept
    {
        if (!validate_relay_envelope(config, relay))
            return false;
        try
        {
            return relay.cert->verify(config);
        }
        catch (const std::exception &)
        {
            return false;
        }
    }

    bool admit_verified_vote(const ReplicaConfig &config,
                             uint32_t expected_epoch,
                             const TreeNetwork &tree,
                             const PeerId &authenticated_peer,
                             const Vote &vote,
                             bool cryptographically_verified) noexcept
    {
        if (!cryptographically_verified ||
            vote.epoch_nr != expected_epoch ||
            vote.tid != tree.get_tree().get_tid() ||
            tree.get_childPeers().find(authenticated_peer) ==
                tree.get_childPeers().end())
            return false;

        return validate_authenticated_vote(
            config, authenticated_peer, vote);
    }

    bool admit_verified_relay(const ReplicaConfig &config,
                              uint32_t expected_epoch,
                              const TreeNetwork &tree,
                              const PeerId &authenticated_peer,
                              const VoteRelay &relay,
                              bool cryptographically_verified) noexcept
    {
        if (!cryptographically_verified ||
            relay.epoch_nr != expected_epoch ||
            relay.tid != tree.get_tree().get_tid() ||
            tree.get_childPeers().find(authenticated_peer) ==
                tree.get_childPeers().end())
            return false;

        return validate_relay_envelope(config, relay);
    }

    namespace
    {
        promise_t start_coordination_prerequisite(
            std::function<promise_t()> start,
            const uint256_t &block_hash,
            const char *name)
        {
            try
            {
                return start();
            }
            catch (const std::exception &error)
            {
                HOTSTUFF_LOG_WARN(
                    "failed to start %s for block %.10s: %s",
                    name, block_hash.to_hex().c_str(), error.what());
            }
            catch (...)
            {
                HOTSTUFF_LOG_WARN(
                    "failed to start %s for block %.10s",
                    name, block_hash.to_hex().c_str());
            }

            promise_t failed;
            failed.reject();
            return failed;
        }

        template<typename Message>
        promise_t coordinate_verified_delivery_impl(
            const Message &message,
            std::function<promise_t()> start_worker_verification,
            std::function<promise_t()> start_block_delivery,
            std::function<void(const block_t &)> continuation)
        {
            const auto block_hash = message.blk_hash;
            auto verification = start_coordination_prerequisite(
                std::move(start_worker_verification),
                block_hash,
                "worker verification");
            auto delivery = start_coordination_prerequisite(
                std::move(start_block_delivery),
                block_hash,
                "block delivery");

            promise_t completion;
            auto prerequisites = promise::all(
                std::vector<promise_t>{verification, delivery});
            prerequisites.then(
                [block_hash,
                 completion,
                 continuation = std::move(continuation)](
                    const promise::values_t &values) mutable
                {
                    bool cryptographically_verified = false;
                    block_t delivered_block;
                    try
                    {
                        if (values.size() == 2)
                        {
                            cryptographically_verified =
                                promise::any_cast<bool>(values[0]);
                            delivered_block =
                                promise::any_cast<block_t>(values[1]);
                        }
                    }
                    catch (const std::exception &error)
                    {
                        HOTSTUFF_LOG_WARN(
                            "verified-delivery result decoding failed for block %.10s: %s",
                            block_hash.to_hex().c_str(), error.what());
                        completion.reject();
                        return;
                    }

                    if (!cryptographically_verified ||
                        delivered_block == nullptr ||
                        delivered_block->get_hash() != block_hash)
                    {
                        HOTSTUFF_LOG_WARN(
                            "verified-delivery coordination rejected block %.10s",
                            block_hash.to_hex().c_str());
                        completion.reject();
                        return;
                    }

                    try
                    {
                        continuation(delivered_block);
                        completion.resolve(true);
                    }
                    catch (const std::exception &error)
                    {
                        HOTSTUFF_LOG_WARN(
                            "verified-delivery continuation failed for block %.10s: %s",
                            block_hash.to_hex().c_str(), error.what());
                        completion.reject();
                    }
                    catch (...)
                    {
                        HOTSTUFF_LOG_WARN(
                            "verified-delivery continuation failed for block %.10s",
                            block_hash.to_hex().c_str());
                        completion.reject();
                    }
                },
                [block_hash, completion]()
                {
                    HOTSTUFF_LOG_WARN(
                        "verified-delivery prerequisite failed for block %.10s",
                        block_hash.to_hex().c_str());
                    completion.reject();
                });
            return completion;
        }
    }

    promise_t coordinate_verified_delivery(
        const Vote &message,
        std::function<promise_t()> start_worker_verification,
        std::function<promise_t()> start_block_delivery,
        std::function<void(const block_t &)> continuation)
    {
        return coordinate_verified_delivery_impl(
            message,
            std::move(start_worker_verification),
            std::move(start_block_delivery),
            std::move(continuation));
    }

    promise_t coordinate_verified_delivery(
        const VoteRelay &message,
        std::function<promise_t()> start_worker_verification,
        std::function<promise_t()> start_block_delivery,
        std::function<void(const block_t &)> continuation)
    {
        return coordinate_verified_delivery_impl(
            message,
            std::move(start_worker_verification),
            std::move(start_block_delivery),
            std::move(continuation));
    }

    struct HotStuffBase::ExactRuntimeAccess final
        : std::enable_shared_from_this<ExactRuntimeAccess>
    {
        // The protocol event context remains the mutation serializer. This
        // gate protects only object lifetime, so its mutex is never held while
        // a continuation runs protocol code or re-enters another continuation.
        class Lease
        {
        public:
            Lease(const Lease &) = delete;
            Lease &operator=(const Lease &) = delete;

            Lease(Lease &&other) noexcept
                : access_(std::move(other.access_)),
                  owner_(other.owner_)
            {
                other.owner_ = nullptr;
            }

            Lease &operator=(Lease &&other) noexcept
            {
                if (this == &other)
                    return *this;
                release();
                access_ = std::move(other.access_);
                owner_ = other.owner_;
                other.owner_ = nullptr;
                return *this;
            }

            ~Lease()
            {
                release();
            }

            HotStuffBase &owner() const
            {
                if (owner_ == nullptr)
                    throw std::logic_error(
                        "exact runtime lease has no owner");
                return *owner_;
            }

        private:
            friend struct ExactRuntimeAccess;

            Lease(std::shared_ptr<ExactRuntimeAccess> access,
                  HotStuffBase *owner)
                : access_(std::move(access)), owner_(owner)
            {}

            void release() noexcept
            {
                if (owner_ == nullptr)
                    return;
                owner_ = nullptr;
                access_->release();
                access_.reset();
            }

            std::shared_ptr<ExactRuntimeAccess> access_;
            HotStuffBase *owner_;
        };

        explicit ExactRuntimeAccess(HotStuffBase *owner)
            : owner(owner)
        {}

        std::optional<Lease> acquire()
        {
            std::lock_guard<std::mutex> lock(mutex);
            if (closing || owner == nullptr)
                return std::nullopt;
            ++active_callbacks;
            return Lease(shared_from_this(), owner);
        }

        void close_and_wait() noexcept
        {
            std::unique_lock<std::mutex> lock(mutex);
            closing = true;
            idle.wait(lock, [this] {
                return active_callbacks == 0;
            });
            owner = nullptr;
        }

    private:
        void release() noexcept
        {
            std::lock_guard<std::mutex> lock(mutex);
            if (active_callbacks == 0)
                std::terminate();
            --active_callbacks;
            if (closing && active_callbacks == 0)
                idle.notify_all();
        }

        std::mutex mutex;
        std::condition_variable idle;
        HotStuffBase *owner;
        std::size_t active_callbacks{0};
        bool closing{false};
    };

    struct HotStuffBase::ExactForwardingRetryJob final
    {
        ExactForwardingRetryJob(
            std::uint64_t id,
            const ProposalContextLease &lease,
            quorum_cert_bt exact_certificate,
            std::set<ReplicaID> exact_signers,
            std::optional<std::uint64_t> pending_id,
            ExactForwardingRole forwarding_role,
            std::uint32_t completed_attempts)
            : id(id),
              key(lease.key()),
              generation(lease.generation()),
              certificate(std::move(exact_certificate)),
              signers(std::move(exact_signers)),
              pending_candidate_id(pending_id),
              role(forwarding_role),
              attempts(completed_attempts)
        {}

        const std::uint64_t id;
        const ProposalKey key;
        const std::uint64_t generation;
        const quorum_cert_bt certificate;
        const std::set<ReplicaID> signers;
        const std::optional<std::uint64_t> pending_candidate_id;
        const ExactForwardingRole role;
        std::uint32_t attempts;
        bool scheduled{false};
        AggregationScheduler::Cancellation cancellation;
    };

    class HotStuffBase::ExactContributionEffects final
        : public ExactVoteHandlerEffects
    {
    public:
        ExactContributionEffects(
            std::shared_ptr<ExactRuntimeAccess> access,
            PeerId source_peer)
            : access_(std::move(access)),
              source_peer_(std::move(source_peer))
        {}

        promise_t start_worker_verification(
            ExactContributionKind kind,
            const ExactContributionEnvelope &contribution) override
        {
            auto runtime = access_->acquire();
            if (!runtime.has_value())
                return resolved(false);
            return runtime->owner().verify_exact_contribution(
                kind, contribution);
        }

        promise_t start_block_delivery(
            const ProposalKey &key) override
        {
            auto runtime = access_->acquire();
            if (!runtime.has_value())
                return resolved(false);
            return runtime->owner().deliver_exact_contribution(
                key, source_peer_);
        }

        void continue_verified(
            const ProposalContextLease &lease,
            ExactContributionKind kind,
            const ExactContributionEnvelope &contribution) override
        {
            auto runtime = access_->acquire();
            if (!runtime.has_value())
                throw std::runtime_error(
                    "exact contribution runtime is unavailable");
            runtime->owner().continue_exact_contribution(
                lease, kind, contribution);
        }

    private:
        static promise_t resolved(bool value)
        {
            return promise_t([value](promise_t &promise) {
                promise.resolve(value);
            });
        }

        std::shared_ptr<ExactRuntimeAccess> access_;
        PeerId source_peer_;
    };

    struct HotStuffBase::AdaptiveEpochRuntime final
    {
        struct Topology
        {
            std::uint64_t token{0};
            uint256_t digest;
            std::vector<ConfigurationId> configurations;
            std::vector<std::uint64_t> generations;
            std::vector<TreeNetwork> trees;
            std::unordered_map<std::uint32_t, std::size_t> tree_indexes;
        };

        struct TopologyState
        {
            TopologyState(
                HotStuffBase &owner_value,
                const EpochDefinition &active_epoch)
                : owner(owner_value)
            {
                active = build(active_epoch, 0, active_epoch.epoch_digest());
                const auto selected = active->tree_indexes.find(
                    owner.current_tree.get_tid());
                if (selected == active->tree_indexes.end())
                    throw std::logic_error(
                        "initial adaptive tree is not prepared");
                active_index = selected->second;
                const auto generation = checked_activation_generation(
                    active_epoch.epoch_number(), 0);
                if (!generation.has_value())
                    throw std::logic_error(
                        "initial adaptive generation is invalid");
                active->generations[active_index] = *generation;
            }

            static Tree runtime_tree(const EpochTreeDefinition &definition)
            {
                if (definition.fanout == 0 ||
                    definition.fanout >
                        std::numeric_limits<std::uint8_t>::max() ||
                    definition.pipeline_stretch >
                        std::numeric_limits<std::uint8_t>::max())
                    throw std::invalid_argument(
                        "adaptive tree exceeds live runtime bounds");
                std::vector<std::uint32_t> members(
                    definition.members_breadth_first.begin(),
                    definition.members_breadth_first.end());
                return Tree(
                    definition.tree_id,
                    static_cast<std::uint8_t>(definition.fanout),
                    static_cast<std::uint8_t>(
                        definition.pipeline_stretch),
                    members);
            }

            std::unique_ptr<Topology> build(
                const EpochDefinition &definition,
                std::uint64_t token,
                const uint256_t &digest)
            {
                auto topology = std::make_unique<Topology>();
                topology->token = token;
                topology->digest = digest;
                topology->configurations.reserve(definition.trees().size());
                topology->generations.reserve(definition.trees().size());
                topology->trees.reserve(definition.trees().size());
                topology->tree_indexes.reserve(definition.trees().size());
                for (const auto &tree : definition.trees())
                {
                    const auto index = topology->trees.size();
                    if (!topology->tree_indexes.emplace(
                            tree.tree_id, index).second)
                        throw std::invalid_argument(
                            "duplicate adaptive runtime tree");
                    topology->configurations.push_back(ConfigurationId{
                        definition.epoch_number(),
                        tree.tree_id,
                        definition.epoch_digest()});
                    topology->generations.push_back(0);
                    topology->trees.emplace_back(
                        runtime_tree(tree), owner.config, owner.get_id());
                }
                if (topology->trees.empty())
                    throw std::invalid_argument(
                        "adaptive runtime contains no trees");
                return topology;
            }

            std::optional<PreparedEpochLiveRuntime> prepare(
                const EpochRuntimePlan &plan)
            {
                if (next_token == 0 || plan.trees.empty())
                    return std::nullopt;

                auto topology = std::make_unique<Topology>();
                topology->token = next_token;
                topology->digest = plan.canonical_digest;
                topology->configurations.reserve(plan.trees.size());
                topology->generations.reserve(plan.trees.size());
                topology->trees.reserve(plan.trees.size());
                topology->tree_indexes.reserve(plan.trees.size());
                for (const auto &input : plan.trees)
                {
                    const auto index = topology->trees.size();
                    if (input.configuration.epoch_number !=
                            plan.stage.activation.successor_epoch_number ||
                        input.configuration.epoch_digest !=
                            plan.stage.activation.successor_epoch_digest ||
                        !topology->tree_indexes.emplace(
                             input.tree.tree_id, index).second)
                        return std::nullopt;
                    topology->configurations.push_back(input.configuration);
                    topology->generations.push_back(0);
                    topology->trees.emplace_back(
                        runtime_tree(input.tree),
                        owner.config,
                        owner.get_id());
                }

                const auto token = next_token++;
                const auto inserted = prepared.emplace(
                    token, std::move(topology));
                if (!inserted.second)
                    throw std::logic_error(
                        "duplicate prepared adaptive runtime token");
                return PreparedEpochLiveRuntime{
                    token, plan.canonical_digest};
            }

            void discard(PreparedEpochLiveRuntime value) noexcept
            {
                const auto found = prepared.find(value.token);
                if (found != prepared.end() &&
                    found->second->digest == value.canonical_plan_digest)
                    prepared.erase(found);
            }

            void arm(
                PreparedEpochLiveRuntime value,
                const EpochRuntimeUpdate &update) noexcept
            {
                const auto found = prepared.find(value.token);
                if (found == prepared.end() ||
                    found->second->digest !=
                        value.canonical_plan_digest)
                    std::terminate();
                arm_topology(
                    *found->second, update.activation.configuration, true);
                armed_token = value.token;
            }

            void arm_rotation(const EpochRuntimeRotation &update) noexcept
            {
                if (!active)
                    std::terminate();
                arm_topology(
                    *active, update.activation.configuration, false);
                armed_token = active->token;
            }

            void arm_topology(
                Topology &topology,
                const ConfigurationId &configuration,
                bool replacement) noexcept
            {
                const auto index = topology.tree_indexes.find(
                    configuration.tree_id);
                if (index == topology.tree_indexes.end() ||
                    topology.configurations[index->second] != configuration)
                    std::terminate();
                armed_topology = &topology;
                armed_index = index->second;
                armed_replacement = replacement;
            }

            void apply(const EpochRuntimeUpdate &update) noexcept
            {
                if (armed_topology == nullptr ||
                    armed_index >= armed_topology->trees.size() ||
                    armed_topology->configurations[armed_index] !=
                        update.activation.configuration ||
                    armed_topology->trees[armed_index]
                            .get_tree().get_tree_root() !=
                        update.leader_view.leader_id ||
                    update.leader_view.configuration !=
                        update.activation.configuration ||
                    update.leader_view.view_generation !=
                        update.activation.generation)
                    std::terminate();

                try
                {
                    if (!owner.pmaker->activate_runtime_view(
                            update.leader_view))
                        std::terminate();
                }
                catch (...)
                {
                    std::terminate();
                }

                armed_topology->generations[armed_index] =
                    update.activation.generation;
                if (armed_replacement)
                {
                    const auto found = prepared.find(armed_token);
                    if (found == prepared.end() ||
                        found->second.get() != armed_topology)
                        std::terminate();
                    draining = std::move(active);
                    active = std::move(found->second);
                    prepared.erase(found);
                }
                active_index = armed_index;
                owner.config.async_blocks =
                    active->trees[active_index]
                        .get_tree().get_pipeline_stretch();
                owner.config.fanout =
                    active->trees[active_index].get_tree().get_fanout();
                armed_topology = nullptr;
                armed_replacement = false;
            }

            const TreeNetwork *find_tree(
                const ConfigurationId &configuration) const noexcept
            {
                const auto in = [&configuration](
                    const std::unique_ptr<Topology> &topology)
                    -> const TreeNetwork *
                {
                    if (!topology)
                        return nullptr;
                    const auto found = topology->tree_indexes.find(
                        configuration.tree_id);
                    if (found == topology->tree_indexes.end() ||
                        topology->configurations[found->second] !=
                            configuration)
                        return nullptr;
                    return &topology->trees[found->second];
                };
                if (const auto *tree = in(active))
                    return tree;
                if (const auto *tree = in(draining))
                    return tree;
                for (const auto &candidate : prepared)
                    if (const auto *tree = in(candidate.second))
                        return tree;
                return nullptr;
            }

            std::optional<std::uint64_t> find_generation(
                const ConfigurationId &configuration) const noexcept
            {
                const auto in = [&configuration](
                    const std::unique_ptr<Topology> &topology)
                    -> std::optional<std::uint64_t>
                {
                    if (!topology)
                        return std::nullopt;
                    const auto found = topology->tree_indexes.find(
                        configuration.tree_id);
                    if (found == topology->tree_indexes.end() ||
                        topology->configurations[found->second] !=
                            configuration ||
                        topology->generations[found->second] == 0)
                        return std::nullopt;
                    return topology->generations[found->second];
                };
                if (const auto generation = in(active))
                    return generation;
                return in(draining);
            }

            const TreeNetwork &current_tree() const
            {
                if (!active || active_index >= active->trees.size())
                    throw std::logic_error(
                        "adaptive runtime has no active tree");
                return active->trees[active_index];
            }

            std::size_t tree_count() const noexcept
            {
                return active == nullptr ? 0 : active->trees.size();
            }

            const TreeNetwork *active_tree(
                std::uint32_t tree_id) const noexcept
            {
                if (!active)
                    return nullptr;
                const auto found = active->tree_indexes.find(tree_id);
                if (found != active->tree_indexes.end())
                    return &active->trees[found->second];
                if (tree_id < active->trees.size())
                    return &active->trees[tree_id];
                return nullptr;
            }

            std::optional<std::uint32_t> next_tree_id() const noexcept
            {
                if (!active || active->trees.size() < 2 ||
                    active_index >= active->trees.size())
                    return std::nullopt;
                return active->trees[
                    (active_index + 1) % active->trees.size()]
                    .get_tree().get_tid();
            }

            HotStuffBase &owner;
            std::map<std::uint64_t, std::unique_ptr<Topology>> prepared;
            std::unique_ptr<Topology> active;
            std::unique_ptr<Topology> draining;
            Topology *armed_topology{nullptr};
            std::uint64_t armed_token{0};
            std::size_t armed_index{0};
            std::size_t active_index{0};
            std::uint64_t next_token{1};
            bool armed_replacement{false};
        };

        class ManagerEgress final : public EpochManagerEgress
        {
        public:
            explicit ManagerEgress(HotStuffBase &owner_value)
                : owner(owner_value)
            {}

            void send_stage_ack(const StageAck &value) noexcept override
            {
                try
                {
                    send(MsgStageAck(value, owner.epoch_wire_limits));
                }
                catch (...)
                {
                    HOTSTUFF_LOG_WARN(
                        "[EPOCH] Failed to encode manager stage acknowledgement");
                }
            }

            void send_activation_status(
                const ActivationStatus &value) noexcept override
            {
                try
                {
                    send(MsgActivationStatus(
                        value, owner.epoch_wire_limits));
                }
                catch (...)
                {
                    HOTSTUFF_LOG_WARN(
                        "[EPOCH] Failed to encode manager activation status");
                }
            }

        private:
            template<typename Message>
            void send(Message message) noexcept
            {
                if (!owner.epoch_manager_peer.has_value())
                    return;
                try
                {
                    owner.pn.send_msg(
                        message, *owner.epoch_manager_peer);
                }
                catch (...)
                {
                    HOTSTUFF_LOG_WARN(
                        "[EPOCH] Failed to send manager status");
                }
            }

            HotStuffBase &owner;
        };

        class Continuations final : public EpochContributionContinuations
        {
        public:
            explicit Continuations(HotStuffBase &owner_value)
                : owner(owner_value)
            {}

            void continue_vote(
                const bytearray_t &body,
                const AuthenticatedEpochPeer &peer) noexcept override
            {
                if (!peer.replica_id.has_value() ||
                    peer.source_peer.is_null())
                    return;
                try
                {
                    MsgVote message{DataStream(body)};
                    if (!message.postponed_parse(&owner))
                        return;
                    owner.buffer_or_dispatch_exact_contribution(
                        ExactContributionKind::direct_vote,
                        make_exact_direct_envelope(
                            message.vote, *peer.replica_id),
                        peer.source_peer);
                }
                catch (...)
                {}
            }

            void continue_relay(
                const bytearray_t &body,
                const AuthenticatedEpochPeer &peer) noexcept override
            {
                if (!peer.replica_id.has_value() ||
                    peer.source_peer.is_null())
                    return;
                try
                {
                    MsgRelay message{DataStream(body)};
                    if (!message.postponed_parse(&owner))
                        return;
                    owner.buffer_or_dispatch_exact_contribution(
                        ExactContributionKind::aggregate_relay,
                        make_exact_relay_envelope(
                            message.vote, *peer.replica_id),
                        peer.source_peer);
                }
                catch (...)
                {}
            }

        private:
            HotStuffBase &owner;
        };

        AdaptiveEpochRuntime(
            HotStuffBase &owner_value,
            const EpochDefinition &active_epoch)
            : owner(owner_value),
              topology(owner, active_epoch),
              live_state(HotStuffEpochLiveStateCallbacks{
                  [this](const EpochRuntimePlan &plan) {
                      return topology.prepare(plan);
                  },
                  [this](PreparedEpochLiveRuntime prepared) {
                      topology.discard(std::move(prepared));
                  },
                  [this](PreparedEpochLiveRuntime prepared,
                         const EpochRuntimeUpdate &update) {
                      topology.arm(std::move(prepared), update);
                  },
                  [this](const EpochRuntimeRotation &update) {
                      topology.arm_rotation(update);
                  },
                  [this](const EpochRuntimeUpdate &update) {
                      topology.apply(update);
                  }}),
              activation(
                  *owner.exact_epochs,
                  active_epoch,
                  owner.get_id(),
                  owner.current_tree.get_tid()),
              retryable(owner.future_proposals, *owner.proposal_admission),
              validator(owner, *owner.exact_epochs),
              transaction(
                  *owner.proposal_admission,
                  *owner.proposal_contexts,
                  live_state),
              adapter(
                  activation,
                  *owner.proposal_contexts,
                  *owner.proposal_admission,
                  retryable,
                  validator,
                  EpochProtocolMode::adaptive_v1,
                  owner.epoch_wire_limits,
                  transaction),
              manager_egress(owner),
              continuations(owner),
              binding(
                  adapter,
                  activation,
                  live_state,
                  manager_egress,
                  continuations)
        {}

        HotStuffBase &owner;
        TopologyState topology;
        HotStuffEpochLiveState live_state;
        ReplicaEpochActivation activation;
        HotStuffRetryableFutureProposalStore retryable;
        HotStuffEpochConsensusBodyValidator validator;
        HotStuffEpochRuntimeTransaction transaction;
        HotStuffEpochRuntimeAdapter adapter;
        ManagerEgress manager_egress;
        Continuations continuations;
        HotStuffEpochLiveBinding binding;
    };

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
    bool MsgRelay::postponed_parse(HotStuffCore *hsc) noexcept
    {
        if (hsc == nullptr)
            return false;

        try
        {
            DataStream serialized(this->serialized);
            VoteRelay vote;
            vote.hsc = hsc;

            auto certificate = hsc->create_quorum_cert(ProposalKey{});
            if (dynamic_cast<QuorumCertAggBLS *>(certificate.get()) != nullptr ||
                dynamic_cast<QuorumCertSecp256k1 *>(certificate.get()) != nullptr)
            {
                validate_relay_wire_bounds(
                    serialized, hsc->get_config().nreplicas);
            }

            serialized >> vote;
            if (serialized.size() != 0)
                return false;

            this->vote = std::move(vote);
            this->serialized = std::move(serialized);
            return true;
        }
        catch (...)
        {
            return false;
        }
    }

    const opcode_t MsgVote::opcode;
    MsgVote::MsgVote(const Vote &vote) { serialized << vote; }
    bool MsgVote::postponed_parse(HotStuffCore *hsc) noexcept
    {
        if (hsc == nullptr)
            return false;

        try
        {
            DataStream serialized(this->serialized);
            Vote vote;
            vote.hsc = hsc;
            serialized >> vote;
            if (serialized.size() != 0)
                return false;

            this->vote = std::move(vote);
            this->serialized = std::move(serialized);
            return true;
        }
        catch (...)
        {
            return false;
        }
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

    void HotStuffBase::stage_epoch(EpochReputation &epoch_repuation)
    {

        HOTSTUFF_LOG_INFO("STORING NEW EPOCH TO BE USED IN THE FUTURE");

        auto epoch = epoch_repuation.epoch;

        epoch.create_tree_networks(global_replicas, get_id());
        epochs.push_back(epoch);
        register_legacy_epoch(epochs.back());

        reputation_on_hold = epoch_repuation.repScore;

        HOTSTUFF_LOG_INFO("STORED NEW EPOCH READY TO DEPLOY IT IN FUTURE BLOCK");
    }

    const EpochDefinition &HotStuffBase::register_legacy_epoch(
        const Epoch &epoch)
    {
        if (exact_epochs == nullptr)
            throw std::logic_error(
                "exact epoch store is not initialized");

        EpochDefinitionInput input;
        input.schema_version = kEpochDefinitionSchemaVersion;
        input.epoch_number = epoch.get_epoch_num();
        input.membership_digest =
            canonical_membership_digest(fixed_membership);
        input.activation_height =
            static_cast<std::uint64_t>(input.epoch_number) * 1000;
        input.generation_seed = 0;
        input.policy_version = "legacy-static-v1";
        input.evidence_snapshot_id =
            "legacy-static-epoch-" + std::to_string(input.epoch_number);
        input.evidence_cutoff = 0;

        if (input.epoch_number == 0)
        {
            input.previous_epoch_digest = uint256_t{};
        }
        else
        {
            const auto *predecessor =
                exact_epochs->find_epoch(input.epoch_number - 1);
            if (predecessor == nullptr)
                throw std::invalid_argument(
                    "legacy epoch has no exact predecessor");
            input.previous_epoch_digest = predecessor->epoch_digest();
        }

        for (const auto &network : epoch.get_tree_networks())
        {
            const auto &tree = network.get_tree();
            EpochTreeDefinition definition;
            definition.tree_id = tree.get_tid();
            definition.fanout = tree.get_fanout();
            definition.pipeline_stretch =
                tree.get_pipeline_stretch();
            for (const auto member : tree.get_tree_array())
                definition.members_breadth_first.push_back(
                    static_cast<ReplicaID>(member));
            input.trees.push_back(std::move(definition));
        }

        if (input.trees.empty())
        {
            for (const auto &tree : epoch.get_trees())
            {
                EpochTreeDefinition definition;
                definition.tree_id = tree.get_tid();
                definition.fanout = tree.get_fanout();
                definition.pipeline_stretch =
                    tree.get_pipeline_stretch();
                for (const auto member : tree.get_tree_array())
                    definition.members_breadth_first.push_back(
                        static_cast<ReplicaID>(member));
                input.trees.push_back(std::move(definition));
            }
        }

        if (const auto *existing =
                exact_epochs->find_epoch(input.epoch_number))
        {
            if (existing->epoch_digest() != compute_epoch_digest(input))
                throw std::invalid_argument(
                    "legacy epoch conflicts with staged exact definition");
            return *existing;
        }

        EpochValidationContext context;
        context.current_height = 0;
        context.minimum_activation_grace = 0;
        return exact_epochs->stage(input, context);
    }

    ConfigurationId HotStuffBase::exact_configuration(
        uint32_t epoch_number,
        uint32_t tree_id) const
    {
        if (exact_epochs == nullptr)
            throw std::logic_error(
                "exact epoch store is not initialized");
        const auto *epoch = exact_epochs->find_epoch(epoch_number);
        if (epoch == nullptr ||
            exact_epochs->find_tree(epoch_number, tree_id) == nullptr)
            throw std::out_of_range(
                "unknown exact epoch/tree configuration");
        return ConfigurationId{
            epoch_number, tree_id, epoch->epoch_digest()};
    }

    const TreeNetwork *HotStuffBase::find_exact_runtime_tree(
        const ConfigurationId &configuration) const noexcept
    {
        if (epoch_protocol_mode == EpochProtocolMode::adaptive_v1 &&
            adaptive_epoch_runtime != nullptr)
            return adaptive_epoch_runtime->topology.find_tree(configuration);

        if (exact_epochs == nullptr)
            return nullptr;
        const auto *epoch =
            exact_epochs->find_epoch(configuration.epoch_number);
        const auto *definition = exact_epochs->find_tree(
            configuration.epoch_number, configuration.tree_id);
        if (epoch == nullptr || definition == nullptr ||
            epoch->epoch_digest() != configuration.epoch_digest)
            return nullptr;

        const auto *runtime = find_message_tree(
            epochs, configuration.epoch_number, configuration.tree_id);
        if (runtime == nullptr)
            return nullptr;
        const auto &runtime_tree = runtime->get_tree();
        const auto &runtime_members = runtime_tree.get_tree_array();
        if (runtime_tree.get_fanout() != definition->fanout ||
            runtime_tree.get_pipeline_stretch() !=
                definition->pipeline_stretch ||
            runtime_members.size() !=
                definition->members_breadth_first.size() ||
            !std::equal(
                runtime_members.begin(),
                runtime_members.end(),
                definition->members_breadth_first.begin(),
                [](std::uint32_t runtime_member, ReplicaID exact_member) {
                    return runtime_member == exact_member;
                }))
            return nullptr;
        return runtime;
    }

    std::optional<std::uint64_t>
    HotStuffBase::find_exact_runtime_generation(
        const ConfigurationId &configuration) const noexcept
    {
        if (epoch_protocol_mode != EpochProtocolMode::adaptive_v1 ||
            adaptive_epoch_runtime == nullptr ||
            !adaptive_epoch_runtime->activation.may_drain_exact_context(
                configuration))
            return std::nullopt;
        return adaptive_epoch_runtime->topology.find_generation(
            configuration);
    }

    std::optional<ProposalContextMetadata>
    HotStuffBase::exact_context_metadata(
        const ProposalKey &key) const
    {
        if (find_exact_runtime_tree(key.configuration) == nullptr ||
            exact_epochs == nullptr)
            return std::nullopt;
        const auto *definition = exact_epochs->find_tree(
            key.configuration.epoch_number,
            key.configuration.tree_id);
        if (definition == nullptr)
            return std::nullopt;
        return make_exact_proposal_context_metadata(
            key,
            get_id(),
            *definition,
            config.nmajority);
    }

    std::optional<ProposalContextLease>
    HotStuffBase::admit_exact_context(
        const ProposalContextMetadata &metadata,
        ProposalContextOrigin origin)
    {
        auto lease = origin == ProposalContextOrigin::leader_local
                         ? proposal_contexts->admit_local(metadata)
                         : proposal_contexts->admit_remote(metadata);
        if (!lease.has_value())
            return std::nullopt;
        if (!proposal_contexts->initialize_accumulator(
                *lease, create_quorum_cert(metadata.key)))
        {
            proposal_contexts->close(
                metadata.key,
                ProposalContextEvent::proposal_aborted);
            return std::nullopt;
        }
        return lease;
    }

    void HotStuffBase::activate_proposal_configuration(
        const ConfigurationId &configuration)
    {
        if (exact_epochs == nullptr)
            throw std::logic_error(
                "cannot activate without an exact epoch store");

        const auto *tree = find_exact_runtime_tree(configuration);
        if (tree == nullptr)
            throw std::logic_error(
                "cannot activate an unavailable exact runtime tree");
        const bool view_activated = pmaker->activate_leader_view(
            configuration, tree->get_tree().get_tree_root());
        if (!view_activated)
        {
            HOTSTUFF_LOG_WARN(
                "[PMAKER] Refusing unsafe exact leader-view activation");
            return;
        }

        proposal_contexts->activate_configuration(configuration);
        emit_active_configuration_event(configuration);

        if (proposal_admission == nullptr)
        {
            proposal_admission =
                std::make_unique<ProposalAdmissionCoordinator>(
                    *exact_epochs,
                    configuration,
                    future_proposals,
                    static_cast<ProposalAdmissionEffects &>(*this));
            return;
        }
        proposal_admission->activate(configuration);
    }

    void HotStuffBase::activate_initial_leader_view()
    {
        activate_proposal_configuration(exact_configuration(
            static_cast<std::uint32_t>(pmaker->get_current_epoch()),
            current_tree.get_tid()));
    }

    void HotStuffBase::initialize_adaptive_epoch_runtime()
    {
        if (adaptive_epoch_runtime != nullptr || exact_epochs == nullptr ||
            proposal_admission == nullptr)
            throw std::logic_error(
                "adaptive epoch runtime cannot be initialized");
        const auto &configuration =
            proposal_admission->active_configuration();
        const auto *active = exact_epochs->find_epoch(
            configuration.epoch_number);
        if (active == nullptr ||
            active->epoch_digest() != configuration.epoch_digest)
            throw std::logic_error(
                "adaptive epoch runtime has no exact active epoch");

        adaptive_epoch_runtime =
            std::make_unique<AdaptiveEpochRuntime>(*this, *active);
        epoch_live_binding = &adaptive_epoch_runtime->binding;
    }

    EpochChangeProposalChainResult
    HotStuffBase::pre_vote_epoch_change_gate(
        const Proposal &proposal) const noexcept
    {
        if (epoch_protocol_mode != EpochProtocolMode::adaptive_v2)
            return bypass_epoch_change_gate();
        try
        {
            if (epoch_change_verifier == nullptr ||
                exact_epochs == nullptr || proposal_contexts == nullptr ||
                proposal.blk == nullptr ||
                epoch_change_maximum_block_extra_bytes == 0 ||
                epoch_change_maximum_ancestry_blocks == 0)
                return reject_epoch_change_gate();

            const auto active_configuration =
                proposal_contexts->active_configuration();
            if (!active_configuration.has_value() ||
                *active_configuration != proposal.configuration())
                return reject_epoch_change_gate();

            const auto *active_epoch = exact_epochs->find_epoch(
                active_configuration->epoch_number);
            if (active_epoch == nullptr ||
                active_epoch->epoch_digest() !=
                    active_configuration->epoch_digest)
                return reject_epoch_change_gate();

            const auto &committed = committed_epoch_change_history;
            if (!committed || committed->head == nullptr)
                return reject_epoch_change_gate();
            if (committed->snapshot.committed_head_hash !=
                    committed->head->get_hash() ||
                committed->snapshot.committed_head_height !=
                    committed->head->get_height())
                return reject_epoch_change_gate();

            auto result = evaluate_epoch_change_proposal_chain(
                *proposal.blk,
                *committed->head,
                committed->snapshot,
                epoch_change_maximum_block_extra_bytes,
                epoch_change_maximum_ancestry_blocks,
                *epoch_change_verifier,
                *active_epoch,
                *exact_epochs);
            switch (result.disposition)
            {
            case EpochChangeProposalDisposition::accepted:
            case EpochChangeProposalDisposition::duplicate:
                if (result.recovery_request)
                    return reject_epoch_change_gate();
                return result;
            case EpochChangeProposalDisposition::defer:
                if (!result.recovery_request)
                    return reject_epoch_change_gate();
                return result;
            case EpochChangeProposalDisposition::rejected:
                return result;
            }
            return reject_epoch_change_gate();
        }
        catch (...)
        {
            return reject_epoch_change_gate();
        }
    }

    void HotStuffBase::initialize_committed_epoch_change_history() noexcept
    {
        committed_epoch_change_history.reset();
        const auto &genesis = committed_head();
        if (genesis == nullptr || genesis->get_decision() != 1)
            return;
        try
        {
            committed_epoch_change_history =
                CommittedEpochChangeHistoryState{
                    genesis,
                    EpochChangeCommittedHistorySnapshot{
                        genesis->get_hash(),
                        genesis->get_height(),
                        std::nullopt}};
        }
        catch (...)
        {
            committed_epoch_change_history.reset();
        }
    }

    void HotStuffBase::record_committed_epoch_change_history(
        const block_t &block) noexcept
    {
        if (epoch_protocol_mode != EpochProtocolMode::adaptive_v2 ||
            !committed_epoch_change_history)
            return;
        try
        {
            const auto &previous = *committed_epoch_change_history;
            if (previous.head == nullptr || block == nullptr ||
                block->get_decision() != 1 ||
                epoch_change_maximum_block_extra_bytes == 0 ||
                previous.snapshot.committed_head_hash !=
                    previous.head->get_hash() ||
                previous.snapshot.committed_head_height !=
                    previous.head->get_height())
            {
                committed_epoch_change_history.reset();
                return;
            }

            const auto &parent_hashes = block->get_parent_hashes();
            const auto &parents = block->get_parents();
            if (parent_hashes.empty() || parents.empty() ||
                parents.front() != previous.head ||
                parent_hashes.front() != previous.head->get_hash())
            {
                committed_epoch_change_history.reset();
                return;
            }

            auto command = previous.snapshot.command;
            const auto extracted = extract_epoch_change_block_extra(
                block->get_extra(),
                epoch_change_maximum_block_extra_bytes);
            if (extracted.disposition ==
                    EpochChangeExtraDisposition::present &&
                extracted.command && extracted.payload_digest)
            {
                command = EpochChangeCommittedHistoryEntry{
                    extracted.command->payload.predecessor_epoch_digest,
                    *extracted.payload_digest};
            }
            else if (extracted.disposition !=
                         EpochChangeExtraDisposition::absent)
            {
                committed_epoch_change_history.reset();
                return;
            }

            committed_epoch_change_history =
                CommittedEpochChangeHistoryState{
                    block,
                    EpochChangeCommittedHistorySnapshot{
                        block->get_hash(),
                        block->get_height(),
                        std::move(command)}};
        }
        catch (...)
        {
            committed_epoch_change_history.reset();
        }
    }

    void HotStuffBase::relay_once(const BufferedProposal &proposal)
    {
        const auto *tree =
            find_exact_runtime_tree(proposal.metadata.configuration);
        if (tree == nullptr)
        {
            HOTSTUFF_LOG_WARN(
                "[PROP HANDLER] Exact runtime tree is unavailable for relay");
            return;
        }

        for (const auto &child : tree->get_childPeers())
        {
            if (epoch_protocol_mode == EpochProtocolMode::adaptive_v1)
            {
                pn.send_msg(
                    MsgPropose(
                        DataStream(proposal.wire_payload), true),
                    child);
                continue;
            }
            pn.send_msg(MsgPropose(
                DataStream(proposal.wire_payload), true), child);
        }
    }

    void HotStuffBase::process_active(const BufferedProposal &proposal)
    {
        const auto proposal_key = proposal.metadata.key();
        if (proposal.source_peer.is_null())
        {
            proposal_contexts->close(
                proposal_key,
                ProposalContextEvent::proposal_aborted);
            if (proposal_admission != nullptr)
                proposal_admission->retire_proposal(proposal_key);
            purge_pending_exact_contributions(proposal_key);
            HOTSTUFF_LOG_WARN(
                "[PROP HANDLER] Active proposal has no authenticated source");
            return;
        }

        try
        {
            MsgPropose message(
                DataStream(hotstuff_epoch_processing_payload(proposal)),
                true);
            message.postponed_parse(this);
            auto parsed = std::move(message.proposal);
            if (parsed.metadata().key() != proposal.metadata.key() ||
                parsed.proposer != proposal.metadata.proposer)
                throw HotStuffInvalidEntity(
                    "proposal metadata changed during full parse");

            const auto source = proposal.source_peer;
            const auto block = parsed.blk;
            if (!block)
                throw HotStuffInvalidEntity("proposal block is null");
            const auto metadata = exact_context_metadata(parsed.key());
            if (!metadata.has_value())
                throw HotStuffInvalidEntity(
                    "proposal exact context metadata is unavailable");

            auto delivery = async_deliver_blk(block->get_hash(), source);
            const auto access = exact_runtime_access;
            const auto delivery_key = metadata->key;
            delivery.then(
                [access,
                 metadata = std::move(*metadata),
                 parsed = std::move(parsed)](
                    const block_t &delivered) mutable
                {
                    auto runtime = access->acquire();
                    if (!runtime.has_value())
                        return;
                    auto &owner = runtime->owner();
                    const auto abort = [&owner, &metadata]() {
                        owner.proposal_contexts->close(
                            metadata.key,
                            ProposalContextEvent::proposal_aborted);
                        if (owner.proposal_admission != nullptr)
                            owner.proposal_admission->retire_proposal(
                                metadata.key);
                        owner.purge_pending_exact_contributions(metadata.key);
                    };

                    if (delivered == nullptr || !delivered->delivered ||
                        delivered->get_hash() != metadata.key.block_hash)
                    {
                        abort();
                        return;
                    }

                    try
                    {
                        const auto gate =
                            owner.pre_vote_epoch_change_gate(parsed);
                        switch (gate.disposition)
                        {
                        case EpochChangeProposalDisposition::accepted:
                        case EpochChangeProposalDisposition::duplicate:
                            break;
                        case EpochChangeProposalDisposition::defer:
                        case EpochChangeProposalDisposition::rejected:
                            abort();
                            return;
                        }

                        auto lease = owner.proposal_contexts->admit_remote(
                            metadata);
                        if (!lease.has_value() ||
                            !owner.proposal_contexts->initialize_accumulator(
                                *lease,
                                owner.create_quorum_cert(metadata.key)))
                        {
                            abort();
                            return;
                        }
                        if (owner.on_receive_proposal(parsed))
                        {
                            owner.pmaker->record_verified_progress(
                                metadata.key.configuration,
                                LeaderProgressEvent::verified_proposal);
                        }
                        const auto timing_lease =
                            owner.proposal_contexts->acquire_open_context(metadata.key);
                        if (!timing_lease.has_value())
                        {
                            owner.purge_pending_exact_contributions(
                                metadata.key);
                            return;
                        }
                        owner.create_expected_vote_state(metadata.key);
                        owner.start_latency_deadline(metadata.key);
                        owner.start_aggregation_timer(metadata.key);
                        owner.drain_pending_exact_contributions(metadata.key);
                    }
                    catch (const std::exception &error)
                    {
                        abort();
                        HOTSTUFF_LOG_WARN(
                            "[PROP HANDLER] Active proposal failed: %s",
                            error.what());
                    }
                    catch (...)
                    {
                        abort();
                        HOTSTUFF_LOG_WARN(
                            "[PROP HANDLER] Active proposal failed");
                    }
                },
                [access, delivery_key]() {
                    auto runtime = access->acquire();
                    if (!runtime.has_value())
                        return;
                    auto &owner = runtime->owner();
                    owner.proposal_contexts->close(
                        delivery_key,
                        ProposalContextEvent::proposal_aborted);
                    if (owner.proposal_admission != nullptr)
                        owner.proposal_admission->retire_proposal(
                            delivery_key);
                    owner.purge_pending_exact_contributions(delivery_key);
                });
        }
        catch (const std::exception &error)
        {
            proposal_contexts->close(
                proposal_key,
                ProposalContextEvent::proposal_aborted);
            if (proposal_admission != nullptr)
                proposal_admission->retire_proposal(proposal_key);
            purge_pending_exact_contributions(proposal_key);
            HOTSTUFF_LOG_WARN(
                "[PROP HANDLER] Rejecting malformed active proposal: %s",
                error.what());
        }
        catch (...)
        {
            proposal_contexts->close(
                proposal_key,
                ProposalContextEvent::proposal_aborted);
            if (proposal_admission != nullptr)
                proposal_admission->retire_proposal(proposal_key);
            purge_pending_exact_contributions(proposal_key);
            HOTSTUFF_LOG_WARN(
                "[PROP HANDLER] Rejecting malformed active proposal");
        }
    }

    promise_t HotStuffBase::verify_exact_contribution(
        ExactContributionKind kind,
        const ExactContributionEnvelope &contribution)
    {
        if (kind == ExactContributionKind::direct_vote)
        {
            const auto &vote = contribution.direct_vote;
            if (vote == nullptr || vote->cert == nullptr)
                throw std::invalid_argument(
                    "exact direct contribution has no certificate");
            return vote->cert->verify(
                config.get_pubkey(vote->voter), vpool);
        }

        const auto &relay = contribution.aggregate_relay;
        if (relay == nullptr || relay->cert == nullptr)
            throw std::invalid_argument(
                "exact relay contribution has no certificate");
        return relay->cert->verify(config, vpool);
    }

    void HotStuffBase::buffer_or_dispatch_exact_contribution(
        ExactContributionKind kind,
        ExactContributionEnvelope envelope,
        PeerId authenticated_source)
    {
        const auto metadata = exact_context_metadata(envelope.message_key);
        if (!metadata.has_value() ||
            !passes_exact_contribution_cheap_gate(
                kind,
                envelope,
                metadata->key,
                metadata->tree))
            return;

        const auto fingerprint = exact_contribution_fingerprint(
            kind, envelope);
        PendingExactContribution contribution{
            kind,
            std::move(envelope),
            std::move(authenticated_source),
            std::move(fingerprint)};
        const auto status = proposal_contexts->context_status(metadata->key);
        if (status == ProposalContextStatus::admitted_open)
        {
            dispatch_exact_contribution(std::move(contribution));
            return;
        }
        if ((status != ProposalContextStatus::unknown &&
             status != ProposalContextStatus::buffered_future) ||
            proposal_admission == nullptr ||
            !proposal_admission->contains_admitted(metadata->key))
            return;

        static_cast<void>(
            pending_exact_contributions.insert(std::move(contribution)));
    }

    void HotStuffBase::dispatch_exact_contribution(
        PendingExactContribution contribution)
    {
        auto effects = std::make_shared<ExactContributionEffects>(
            exact_runtime_access,
            contribution.authenticated_source);
        ExactVoteHandlerCoordinator coordinator(
            proposal_contexts, effects);
        if (contribution.kind == ExactContributionKind::direct_vote)
        {
            static_cast<void>(
                coordinator.handle_direct(contribution.envelope));
            return;
        }
        static_cast<void>(
            coordinator.handle_relay(contribution.envelope));
    }

    void HotStuffBase::drain_pending_exact_contributions(
        const ProposalKey &key)
    {
        auto contributions = pending_exact_contributions.drain(key);
        for (auto &contribution : contributions)
            dispatch_exact_contribution(std::move(contribution));
    }

    void HotStuffBase::purge_pending_exact_contributions(
        const ProposalKey &key)
    {
        discard_exact_forwarding_retries(key);
        static_cast<void>(pending_exact_contributions.purge(key));
    }

    promise_t HotStuffBase::deliver_exact_contribution(
        const ProposalKey &key,
        const PeerId &source_peer)
    {
        promise_t completion;
        auto delivery = async_deliver_blk(key.block_hash, source_peer);
        delivery.then(
            [completion, key](const block_t &block) mutable
            {
                completion.resolve(
                    block != nullptr && block->delivered &&
                    block->get_hash() == key.block_hash);
            },
            [completion]() mutable
            {
                completion.resolve(false);
            });
        return completion;
    }

    void HotStuffBase::record_exact_latency(
        const ProposalContextLease &lease,
        ReplicaID child)
    {
        const auto elapsed = proposal_contexts->take_latency_us(
            lease, child);
        if (!elapsed.has_value())
            return;
        const auto bounded = static_cast<std::uint32_t>(std::min(
            *elapsed,
            static_cast<std::uint64_t>(
                std::numeric_limits<std::uint32_t>::max())));
        std::lock_guard<std::mutex> lock(metrics_lock);
        peer_latencies.emplace_back(
            child,
            lease.key().configuration.epoch_number,
            lease.key().configuration.tree_id,
            bounded);
    }

    bool HotStuffBase::send_exact_relay(
        const ProposalContextLease &lease,
        quorum_cert_bt certificate)
    {
        if (certificate == nullptr ||
            certificate->get_proposal_key() != lease.key() ||
            !lease.tree().parent.has_value())
            return false;
        const auto parent = config.get_peer_id(*lease.tree().parent);
        if (parent.is_null())
            return false;

        try
        {
            VoteRelay relay(
                lease.key(), std::move(certificate), this);
            if (epoch_protocol_mode == EpochProtocolMode::adaptive_v1)
            {
                if (adaptive_epoch_runtime == nullptr)
                    return false;
                const auto generation = find_exact_runtime_generation(
                    lease.key().configuration);
                if (!generation.has_value())
                    return false;
                const MsgRelay native(relay);
                const auto encoded = adaptive_epoch_consensus_message(
                    lease.key().configuration,
                    *generation,
                    EpochConsensusWireKind::relay,
                    lease.key(),
                    get_id(),
                    lease.tree().root,
                    static_cast<bytearray_t>(native.serialized),
                    epoch_wire_limits);
                if (encoded.empty())
                    return false;
                return pn.send_msg(
                    MsgRelay(DataStream(encoded)), parent);
            }

            return pn.send_msg(MsgRelay(relay), parent);
        }
        catch (...)
        {
            return false;
        }
    }

    bool HotStuffBase::send_exact_relay_reserved(
        const ProposalContextLease &lease,
        ProposalForwardingClaim claim,
        ExactForwardingRole role,
        std::shared_ptr<ExactForwardingRetryJob> retry)
    {
        const auto reservation_id = claim.reservation_id;
        if (reservation_id == 0 || claim.certificate == nullptr)
            return false;
        auto retained = claim.certificate->clone();
        const auto signers = claim.signers;
        const auto pending_candidate_id = claim.pending_candidate_id;
        const auto attempts = retry == nullptr ? 1U : ++retry->attempts;
        const auto reserved_event =
            role == ExactForwardingRole::initial_aggregate
                ? AdaptiveAggregationTransition::initial_reserved
                : AdaptiveAggregationTransition::delta_reserved;
        const auto enqueued_event =
            role == ExactForwardingRole::initial_aggregate
                ? AdaptiveAggregationTransition::initial_enqueued
                : AdaptiveAggregationTransition::delta_enqueued;
        const auto committed_event =
            role == ExactForwardingRole::initial_aggregate
                ? AdaptiveAggregationTransition::initial_committed
                : AdaptiveAggregationTransition::delta_committed;
        const auto released_event =
            role == ExactForwardingRole::initial_aggregate
                ? AdaptiveAggregationTransition::initial_released
                : AdaptiveAggregationTransition::delta_released;
        emit_adaptive_aggregation_event(
            reserved_event, lease, &signers);
        const bool enqueued = send_exact_relay(
            lease, std::move(claim.certificate));
        if (!enqueued)
        {
            if (proposal_contexts->release_forwarding_claim(
                    lease, reservation_id))
                emit_adaptive_aggregation_event(
                    released_event,
                    lease,
                    &signers,
                    nullptr,
                    nullptr,
                    0,
                    0,
                    "transport_enqueue_rejected");
            schedule_exact_forwarding_retry(
                lease,
                std::move(retained),
                signers,
                pending_candidate_id,
                role,
                attempts);
            return false;
        }
        emit_adaptive_aggregation_event(
            enqueued_event, lease, &signers);
        if (!proposal_contexts->commit_forwarding_claim(
                lease, reservation_id))
        {
            if (proposal_contexts->release_forwarding_claim(
                    lease, reservation_id))
                emit_adaptive_aggregation_event(
                    released_event,
                    lease,
                    &signers,
                    nullptr,
                    nullptr,
                    0,
                    0,
                    "forwarding_commit_failed");
            auto failure = retry;
            if (failure == nullptr)
                failure = std::make_shared<ExactForwardingRetryJob>(
                    0,
                    lease,
                    std::move(retained),
                    signers,
                    pending_candidate_id,
                    role,
                    attempts);
            abort_exact_forwarding(
                lease, failure, "forwarding_commit_failed");
            return false;
        }
        emit_adaptive_aggregation_event(
            committed_event, lease, &signers);
        if (retry != nullptr)
            exact_forwarding_retry_jobs.erase(retry->id);
        complete_exact_forwarding(lease, role);
        return true;
    }

    quorum_cert_bt HotStuffBase::make_exact_direct_forwarding_candidate(
        const ProposalContextLease &lease,
        const Vote &vote)
    {
        if (vote.cert == nullptr || vote.key() != lease.key())
            return nullptr;
        try
        {
            auto certificate = create_quorum_cert(lease.key());
            certificate->add_verified_part(
                config, vote.voter, *vote.cert);
            certificate->compute();
            if (!certificate->verify(config))
                return nullptr;
            return certificate;
        }
        catch (...)
        {
            return nullptr;
        }
    }

    bool HotStuffBase::forward_exact_direct(
        const ProposalContextLease &lease,
        const Vote &vote)
    {
        if (vote.cert == nullptr || vote.key() != lease.key() ||
            !lease.tree().parent.has_value() ||
            config.get_peer_id(*lease.tree().parent).is_null())
            return false;
        auto certificate = make_exact_direct_forwarding_candidate(
            lease, vote);
        if (certificate == nullptr)
            return false;
        auto claim = proposal_contexts
                         ->claim_unforwarded_certificate_reservation(
            lease, std::move(certificate));
        if (!claim.has_value())
        {
            try
            {
                const std::set<ReplicaID> rejected{vote.voter};
                emit_adaptive_aggregation_event(
                    AdaptiveAggregationTransition::delta_rejected,
                    lease,
                    &rejected,
                    nullptr,
                    nullptr,
                    0,
                    0,
                    "reservation_rejected");
            }
            catch (...)
            {
            }
            return false;
        }
        return send_exact_relay_reserved(
            lease,
            std::move(*claim),
            ExactForwardingRole::delta);
    }

    bool HotStuffBase::forward_exact_relay(
        const ProposalContextLease &lease,
        const VoteRelay &relay)
    {
        if (relay.cert == nullptr || relay.key() != lease.key() ||
            !lease.tree().parent.has_value() ||
            config.get_peer_id(*lease.tree().parent).is_null())
            return false;
        auto claim = proposal_contexts
                         ->claim_unforwarded_certificate_reservation(
            lease, relay.cert->clone());
        if (!claim.has_value())
        {
            try
            {
                const auto enumerated = relay.cert->get_signers();
                const std::set<ReplicaID> rejected(
                    enumerated.begin(), enumerated.end());
                emit_adaptive_aggregation_event(
                    AdaptiveAggregationTransition::delta_rejected,
                    lease,
                    &rejected,
                    nullptr,
                    nullptr,
                    0,
                    0,
                    "reservation_rejected");
            }
            catch (...)
            {
            }
            return false;
        }
        return send_exact_relay_reserved(
            lease,
            std::move(*claim),
            ExactForwardingRole::delta);
    }

    void HotStuffBase::complete_exact_forwarding(
        const ProposalContextLease &lease,
        ExactForwardingRole role)
    {
        const auto event =
            role == ExactForwardingRole::initial_aggregate &&
                    !proposal_contexts->delta_open_enabled(lease)
                ? ProposalContextEvent::non_root_aggregate_enqueued
                : ProposalContextEvent::late_contribution_forwarded;
        const auto transition = proposal_contexts->transition(lease, event);
        if (transition != ProposalTransitionResult::retained_open)
        {
            discard_exact_forwarding_retries(
                lease.key(), lease.generation());
            return;
        }
        if (proposal_contexts->delta_open_enabled(lease))
            drain_pending_exact_forwarding_candidates(lease);
    }

    void HotStuffBase::drain_pending_exact_forwarding_candidates(
        const ProposalContextLease &lease)
    {
        if (!proposal_contexts->revalidate(lease) ||
            !proposal_contexts->delta_open_enabled(lease))
            return;
        const auto sweep_key = std::make_pair(
            lease.key(), lease.generation());
        if (!exact_forwarding_sweeps.insert(sweep_key).second)
            return;

        const auto pending =
            proposal_contexts->pending_forwarding_candidate_ids(lease);
        for (const auto pending_id : pending)
        {
            const bool retry_scheduled = std::any_of(
                exact_forwarding_retry_jobs.begin(),
                exact_forwarding_retry_jobs.end(),
                [&lease, pending_id](const auto &entry) {
                    const auto &retry = entry.second;
                    return retry->key == lease.key() &&
                           retry->generation == lease.generation() &&
                           retry->pending_candidate_id == pending_id;
                });
            if (retry_scheduled)
                continue;
            auto claim =
                proposal_contexts->claim_pending_certificate_reservation(
                    lease, pending_id);
            if (!claim.has_value())
                continue;
            static_cast<void>(send_exact_relay_reserved(
                lease,
                std::move(*claim),
                ExactForwardingRole::delta));
            if (!proposal_contexts->revalidate(lease))
                break;
        }
        exact_forwarding_sweeps.erase(sweep_key);
    }

    void HotStuffBase::schedule_exact_forwarding_retry(
        const ProposalContextLease &lease,
        quorum_cert_bt certificate,
        const std::set<ReplicaID> &signers,
        std::optional<std::uint64_t> pending_candidate_id,
        ExactForwardingRole role,
        std::uint32_t attempts)
    {
        if (certificate == nullptr || signers.empty() ||
            certificate->get_proposal_key() != lease.key())
            return;
        if (role == ExactForwardingRole::delta &&
            proposal_contexts->initial_forwarding_owned(lease))
        {
            HOTSTUFF_LOG_PROTO(
                "[FORWARD] Deferring delta retry behind initial owner for %.10s",
                lease.key().block_hash.to_hex().c_str());
            return;
        }

        std::shared_ptr<ExactForwardingRetryJob> retry;
        for (const auto &entry : exact_forwarding_retry_jobs)
        {
            const auto &candidate = entry.second;
            if (candidate->key != lease.key() ||
                candidate->generation != lease.generation() ||
                !signer_sets_overlap(candidate->signers, signers))
                continue;
            if (candidate->signers == signers &&
                candidate->role == role)
            {
                retry = candidate;
                break;
            }
            if (candidate->role == ExactForwardingRole::initial_aggregate ||
                role == ExactForwardingRole::initial_aggregate)
            {
                HOTSTUFF_LOG_PROTO(
                    "[FORWARD] Deferring overlapping retry behind initial owner for %.10s",
                    lease.key().block_hash.to_hex().c_str());
                return;
            }

            auto overlap = std::make_shared<ExactForwardingRetryJob>(
                0,
                lease,
                std::move(certificate),
                signers,
                pending_candidate_id,
                role,
                attempts);
            abort_exact_forwarding(
                lease, overlap, "forwarding_retry_overlap");
            return;
        }
        if (retry == nullptr)
        {
            std::size_t proposal_jobs = 0;
            for (const auto &entry : exact_forwarding_retry_jobs)
                if (entry.second->key == lease.key() &&
                    entry.second->generation == lease.generation())
                    ++proposal_jobs;
            if (proposal_jobs >= lease.tree().assigned_subtree.size() + 1)
            {
                auto overflow =
                    std::make_shared<ExactForwardingRetryJob>(
                        0,
                        lease,
                        std::move(certificate),
                        signers,
                        pending_candidate_id,
                        role,
                        attempts);
                abort_exact_forwarding(
                    lease, overflow, "forwarding_retry_bound_exceeded");
                return;
            }
            auto retry_id = next_exact_forwarding_retry_id++;
            if (retry_id == 0)
                retry_id = next_exact_forwarding_retry_id++;
            retry = std::make_shared<ExactForwardingRetryJob>(
                retry_id,
                lease,
                std::move(certificate),
                signers,
                pending_candidate_id,
                role,
                attempts);
            exact_forwarding_retry_jobs.emplace(retry_id, retry);
        }
        else
            retry->attempts = std::max(retry->attempts, attempts);

        if (retry->attempts >= exact_forwarding_max_attempts)
        {
            abort_exact_forwarding(
                lease, retry, "forwarding_retry_exhausted");
            return;
        }
        if (retry->scheduled)
            return;
        if (aggregation_scheduler == nullptr)
        {
            abort_exact_forwarding(
                lease, retry, "forwarding_retry_scheduler_unavailable");
            return;
        }

        retry->scheduled = true;
        const auto delay = std::chrono::duration_cast<
            AggregationScheduler::Duration>(
            exact_forwarding_retry_base_delay * retry->attempts);
        auto cancellation = aggregation_scheduler->schedule_after(
            delay,
            [access = exact_runtime_access, retry]() {
                auto runtime = access->acquire();
                if (!runtime.has_value())
                    return;
                runtime->owner().dispatch_exact_forwarding_retry(retry);
            });
        if (!cancellation)
        {
            retry->scheduled = false;
            abort_exact_forwarding(
                lease, retry, "forwarding_retry_schedule_failed");
            return;
        }
        retry->cancellation = std::move(cancellation);
    }

    void HotStuffBase::dispatch_exact_forwarding_retry(
        const std::shared_ptr<ExactForwardingRetryJob> &retry)
    {
        const auto registered =
            exact_forwarding_retry_jobs.find(retry->id);
        if (registered == exact_forwarding_retry_jobs.end() ||
            registered->second != retry)
            return;
        retry->scheduled = false;
        retry->cancellation = {};

        const auto lease =
            proposal_contexts->acquire_open_context(retry->key);
        if (!lease.has_value() ||
            lease->generation() != retry->generation)
        {
            exact_forwarding_retry_jobs.erase(retry->id);
            return;
        }

        std::optional<ProposalForwardingClaim> claim;
        if (retry->role == ExactForwardingRole::initial_aggregate)
            claim = proposal_contexts
                        ->claim_initial_forwarding_reservation(*lease);
        else if (retry->pending_candidate_id.has_value())
            claim = proposal_contexts
                        ->claim_pending_certificate_reservation(
                            *lease, *retry->pending_candidate_id);
        else
            claim = proposal_contexts
                        ->claim_unforwarded_certificate_reservation(
                            *lease, retry->certificate->clone());
        if (!claim.has_value())
        {
            const auto snapshot = proposal_contexts->snapshot(retry->key);
            const bool covered = snapshot.has_value() &&
                                 std::includes(
                                     snapshot->forwarded_signers.begin(),
                                     snapshot->forwarded_signers.end(),
                                     retry->signers.begin(),
                                     retry->signers.end());
            exact_forwarding_retry_jobs.erase(retry->id);
            if (covered)
            {
                if (proposal_contexts->delta_open_enabled(*lease))
                    drain_pending_exact_forwarding_candidates(*lease);
                return;
            }
            if (retry->role == ExactForwardingRole::initial_aggregate &&
                proposal_contexts->retire_overlapped_initial_forwarding(
                    *lease, retry->signers))
            {
                drain_pending_exact_forwarding_candidates(*lease);
                return;
            }
            abort_exact_forwarding(
                *lease, retry, "forwarding_retry_reclaim_failed");
            return;
        }
        if (claim->signers != retry->signers)
        {
            static_cast<void>(proposal_contexts->release_forwarding_claim(
                *lease, claim->reservation_id));
            abort_exact_forwarding(
                *lease, retry, "forwarding_retry_identity_changed");
            return;
        }
        static_cast<void>(send_exact_relay_reserved(
            *lease, std::move(*claim), retry->role, retry));
    }

    void HotStuffBase::abort_exact_forwarding(
        const ProposalContextLease &lease,
        const std::shared_ptr<ExactForwardingRetryJob> &retry,
        const char *reason)
    {
        const auto *signers = retry == nullptr
                                  ? nullptr
                                  : &retry->signers;
        if (reason != nullptr &&
            std::strcmp(reason, "forwarding_retry_exhausted") == 0)
            emit_adaptive_aggregation_event(
                AdaptiveAggregationTransition::retry_exhausted,
                lease,
                signers,
                nullptr,
                nullptr,
                0,
                0,
                reason);
        emit_adaptive_aggregation_event(
            AdaptiveAggregationTransition::proposal_aborted,
            lease,
            signers,
            nullptr,
            nullptr,
            0,
            0,
            reason == nullptr ? "unspecified_forwarding_abort" : reason);
        HOTSTUFF_LOG_WARN(
            "[FORWARD] Exact forwarding aborted for %.10s generation=%llu "
            "signers=%zu attempts=%u reason=%s",
            lease.key().block_hash.to_hex().c_str(),
            static_cast<unsigned long long>(lease.generation()),
            retry == nullptr ? 0 : retry->signers.size(),
            retry == nullptr ? 0 : retry->attempts,
            reason);
        discard_exact_forwarding_retries(
            lease.key(), lease.generation());
        static_cast<void>(proposal_contexts->transition(
            lease, ProposalContextEvent::proposal_aborted));
        pending_exact_contributions.purge(lease.key());
        if (proposal_admission != nullptr)
            proposal_admission->retire_proposal(lease.key());
    }

    void HotStuffBase::discard_exact_forwarding_retries(
        const ProposalKey &key,
        std::optional<std::uint64_t> generation)
    {
        std::vector<AggregationScheduler::Cancellation> cancellations;
        for (auto retry = exact_forwarding_retry_jobs.begin();
             retry != exact_forwarding_retry_jobs.end();)
        {
            if (retry->second->key != key ||
                (generation.has_value() &&
                 retry->second->generation != *generation))
            {
                ++retry;
                continue;
            }
            if (retry->second->cancellation)
                cancellations.push_back(
                    std::move(retry->second->cancellation));
            retry = exact_forwarding_retry_jobs.erase(retry);
        }
        for (auto sweep = exact_forwarding_sweeps.begin();
             sweep != exact_forwarding_sweeps.end();)
            if (sweep->first == key &&
                (!generation.has_value() ||
                 sweep->second == *generation))
                sweep = exact_forwarding_sweeps.erase(sweep);
            else
                ++sweep;
        for (auto &cancel : cancellations)
            cancel();
    }

    void HotStuffBase::cancel_all_exact_forwarding_retries() noexcept
    {
        std::vector<AggregationScheduler::Cancellation> cancellations;
        for (auto &retry : exact_forwarding_retry_jobs)
            if (retry.second->cancellation)
                cancellations.push_back(
                    std::move(retry.second->cancellation));
        exact_forwarding_retry_jobs.clear();
        for (auto &cancel : cancellations)
            try
            {
                cancel();
            }
            catch (...)
            {}
    }

    quorum_cert_bt HotStuffBase::verified_aggregation_candidate(
        const ProposalContextLease &lease)
    {
        if (!lease.tree().parent.has_value() ||
            config.get_peer_id(*lease.tree().parent).is_null())
            return nullptr;
        auto candidate = proposal_contexts->clone_accumulator(lease);
        if (candidate == nullptr || candidate->get_sigs_n() == 0)
            return nullptr;
        candidate->compute();
        if (!candidate->verify(config))
            return nullptr;
        return candidate;
    }

    void HotStuffBase::record_aggregation_timeout(
        const ProposalContextLease &lease,
        const std::set<ReplicaID> &missing)
    {
        HOTSTUFF_LOG_INFO(
            "[TIMER] Exact aggregation timeout for %.10s with %zu missing children",
            lease.key().block_hash.to_hex().c_str(),
            missing.size());

        try
        {
            const auto required_gaps =
                proposal_contexts->missing_required_signers_by_child(lease);
            emit_adaptive_aggregation_event(
                AdaptiveAggregationTransition::required_branch_incomplete,
                lease,
                nullptr,
                nullptr,
                required_gaps.has_value() ? &*required_gaps : nullptr);
        }
        catch (...)
        {
        }

        if (missing.empty())
            return;

        std::vector<TimeoutMeasure> timeouts;
        timeouts.reserve(missing.size());
        for (const auto child : missing)
            timeouts.emplace_back(
                child,
                lease.key().configuration.epoch_number,
                lease.key().configuration.tree_id,
                0);
        if (reputation_server_conn != nullptr)
            rn.send_msg(
                MsgTimeoutReport(
                    TimeoutReport(get_id(), timeouts), false),
                reputation_server_conn);
    }

    void HotStuffBase::record_optional_aggregation_absence(
        const ProposalContextLease &lease,
        const std::set<ReplicaID> &missing)
    {
        if (missing.empty())
            return;
        emit_adaptive_aggregation_event(
            AdaptiveAggregationTransition::
                wait_exempt_absent_at_observation_deadline,
            lease,
            nullptr,
            &missing);
    }

    void HotStuffBase::emit_adaptive_aggregation_event(
        AdaptiveAggregationTransition transition,
        const ProposalContextLease &lease,
        const std::set<ReplicaID> *accepted_signers,
        const std::set<ReplicaID> *missing_optional,
        const std::map<ReplicaID, std::set<ReplicaID>> *required_gaps,
        std::size_t root_signer_count,
        std::size_t global_quorum,
        const char *reason) noexcept
    {
        if (adaptive_event_emitter == nullptr)
            return;
        try
        {
            AdaptiveAggregationStructuredEvent event;
            event.transition = transition;
            event.configuration = lease.key().configuration;
            event.block_hash = lease.key().block_hash;
            event.context_generation = lease.generation();
            event.observer_replica = get_id();
            event.wait_exempt_signers.assign(
                lease.tree().optional_subtree.begin(),
                lease.tree().optional_subtree.end());
            if (accepted_signers != nullptr)
                event.accepted_signers.assign(
                    accepted_signers->begin(), accepted_signers->end());
            if (missing_optional != nullptr)
                event.missing_optional_signers.assign(
                    missing_optional->begin(), missing_optional->end());
            if (transition == AdaptiveAggregationTransition::
                                  wait_exempt_absent_at_observation_deadline)
            {
                const auto pending = proposal_contexts->
                    pending_optional_direct_children(lease);
                if (pending.has_value())
                    event.absent_direct_children.assign(
                        pending->begin(), pending->end());
            }
            if (required_gaps != nullptr)
                for (const auto &gap : *required_gaps)
                    event.required_branch_gaps.push_back(
                        RequiredBranchSignerGap{
                            gap.first,
                            std::vector<ReplicaID>(
                                gap.second.begin(), gap.second.end())});
            event.root_signer_count = root_signer_count;
            event.global_quorum = global_quorum;
            if (reason != nullptr)
                event.rejection_reason = reason;
            adaptive_event_emitter->emit_adaptive(event);
        }
        catch (...)
        {
            // Evidence failure invalidates the run, never protocol behavior.
        }
    }

    void HotStuffBase::emit_active_configuration_event(
        const ConfigurationId &configuration) noexcept
    {
        if (adaptive_event_emitter == nullptr || exact_epochs == nullptr)
            return;
        try
        {
            const auto *definition = exact_epochs->find_tree(
                configuration.epoch_number, configuration.tree_id);
            if (definition == nullptr)
                return;
            const auto byzantine = derive_byzantine_quorum(
                definition->members_breadth_first.size());
            if (!byzantine.has_value())
                return;
            AdaptiveAggregationStructuredEvent event;
            event.transition =
                AdaptiveAggregationTransition::configuration_active;
            event.configuration = configuration;
            event.observer_replica = get_id();
            event.wait_exempt_signers =
                definition->wait_exempt_leaves;
            event.global_quorum = byzantine->quorum;
            adaptive_event_emitter->emit_adaptive(event);
        }
        catch (...)
        {
        }
    }

    void HotStuffBase::emit_epoch_lifecycle_event(
        EpochLifecycleTransition transition,
        const ConfigurationId &configuration,
        std::uint64_t activation_height) noexcept
    {
        if (structured_event_emitter == nullptr)
            return;
        try
        {
            structured_event_emitter->emit(
                StructuredEventPayload{EpochLifecycleEvent{
                    transition, configuration, activation_height}});
        }
        catch (...)
        {
        }
    }

    void HotStuffBase::continue_exact_contribution(
        const ProposalContextLease &lease,
        ExactContributionKind kind,
        const ExactContributionEnvelope &contribution)
    {
        if (!proposal_contexts->revalidate(lease))
            return;

        bool accepted = false;
        if (kind == ExactContributionKind::direct_vote)
        {
            const auto &vote = contribution.direct_vote;
            if (vote == nullptr || vote->cert == nullptr ||
                !contribution.claimed_voter.has_value())
                return;
            auto forwarding_candidate =
                make_exact_direct_forwarding_candidate(lease, *vote);
            if (forwarding_candidate == nullptr)
                return;
            accepted = proposal_contexts->record_verified_direct_part(
                lease,
                config,
                contribution.authenticated_sender,
                *contribution.claimed_voter,
                *vote->cert,
                std::move(forwarding_candidate));
        }
        else
        {
            const auto &relay = contribution.aggregate_relay;
            if (relay == nullptr || relay->cert == nullptr)
                return;
            accepted = proposal_contexts->record_verified_aggregate_certificate(
                lease,
                contribution.authenticated_sender,
                *relay->cert);
        }
        if (!accepted)
        {
            if (proposal_contexts->delta_open_enabled(lease))
                try
                {
                    std::set<ReplicaID> rejected_signers;
                    if (kind == ExactContributionKind::direct_vote &&
                        contribution.claimed_voter.has_value())
                        rejected_signers.insert(
                            *contribution.claimed_voter);
                    else if (kind == ExactContributionKind::aggregate_relay &&
                             contribution.aggregate_relay != nullptr &&
                             contribution.aggregate_relay->cert != nullptr)
                    {
                        const auto signers =
                            contribution.aggregate_relay->cert->get_signers();
                        rejected_signers.insert(
                            signers.begin(), signers.end());
                    }
                    emit_adaptive_aggregation_event(
                        AdaptiveAggregationTransition::delta_rejected,
                        lease,
                        rejected_signers.empty()
                            ? nullptr
                            : &rejected_signers,
                        nullptr,
                        nullptr,
                        0,
                        0,
                        "verification_or_overlap_rejected");
                }
                catch (...)
                {
                }
            return;
        }

        try
        {
            std::set<ReplicaID> contribution_signers;
            if (kind == ExactContributionKind::direct_vote)
                contribution_signers.insert(
                    *contribution.claimed_voter);
            else
            {
                const auto signers =
                    contribution.aggregate_relay->cert->get_signers();
                contribution_signers.insert(
                    signers.begin(), signers.end());
            }
            if (proposal_contexts->delta_open_enabled(lease))
            {
                std::set<ReplicaID> accepted_optional;
                std::set_intersection(
                    contribution_signers.begin(),
                    contribution_signers.end(),
                    lease.tree().optional_subtree.begin(),
                    lease.tree().optional_subtree.end(),
                    std::inserter(
                        accepted_optional, accepted_optional.end()));
                if (!accepted_optional.empty())
                    emit_adaptive_aggregation_event(
                        AdaptiveAggregationTransition::
                            wait_exempt_late_accepted,
                        lease,
                        &accepted_optional);
            }

            if (!lease.tree().parent.has_value())
            {
                const auto snapshot =
                    proposal_contexts->snapshot(lease.key());
                const auto quorum =
                    proposal_contexts->frozen_global_quorum(lease);
                if (snapshot.has_value() && quorum.has_value())
                    emit_adaptive_aggregation_event(
                        AdaptiveAggregationTransition::
                            root_quorum_progress,
                        lease,
                        &snapshot->verified_signers,
                        nullptr,
                        nullptr,
                        snapshot->verified_signers.size(),
                        *quorum);
            }
        }
        catch (...)
        {
        }

        record_exact_latency(
            lease, contribution.authenticated_sender);
        if (proposal_contexts->pass_through_enabled(lease))
        {
            if (!lease.tree().parent.has_value())
            {
                try_finish_exact_context(lease);
                return;
            }
            if (!proposal_contexts->delta_open_enabled(lease))
                return;
            static_cast<void>(
                kind == ExactContributionKind::direct_vote
                    ? forward_exact_direct(
                          lease, *contribution.direct_vote)
                    : forward_exact_relay(
                          lease, *contribution.aggregate_relay));
            return;
        }
        try_finish_exact_context(lease);
    }

    bool HotStuffBase::publish_exact_root_qc(
        const ProposalContextLease &lease,
        quorum_cert_bt final_qc)
    {
        auto block = storage->find_blk(lease.key().block_hash);
        if (block == nullptr || !block->delivered || final_qc == nullptr ||
            final_qc->get_proposal_key() != lease.key())
            return false;
        if (block->self_qc != nullptr &&
            block->self_qc->get_proposal_key() != lease.key())
            return false;

        block->self_qc = final_qc->clone();
        const auto piped = std::find(
            piped_queue.begin(), piped_queue.end(), lease.key().block_hash);
        if (piped != piped_queue.end() &&
            (piped_queue.empty() ||
             piped_queue.front() != lease.key().block_hash))
        {
            if (std::find(
                    rdy_queue.begin(),
                    rdy_queue.end(),
                    lease.key().block_hash) == rdy_queue.end())
                rdy_queue.push_back(lease.key().block_hash);
            return false;
        }

        if (!piped_queue.empty() &&
            piped_queue.front() == lease.key().block_hash)
            piped_queue.pop_front();
        const auto ready = std::find(
            rdy_queue.begin(), rdy_queue.end(), lease.key().block_hash);
        if (ready != rdy_queue.end())
            rdy_queue.erase(ready);
        update_hqc(block, block->self_qc);
        on_qc_finish(block);
        return true;
    }

    void HotStuffBase::drain_ready_piped_qcs()
    {
        while (!piped_queue.empty())
        {
            const auto ready = std::find(
                rdy_queue.begin(),
                rdy_queue.end(),
                piped_queue.front());
            if (ready == rdy_queue.end())
                return;
            const auto hash = *ready;
            auto block = storage->find_blk(hash);
            if (block == nullptr || block->self_qc == nullptr ||
                block->self_qc->get_proposal_key().block_hash != hash)
                return;

            const auto key = block->self_qc->get_proposal_key();
            rdy_queue.erase(ready);
            piped_queue.pop_front();
            update_hqc(block, block->self_qc);
            on_qc_finish(block);
            const auto lease =
                proposal_contexts->acquire_open_context(key);
            if (lease.has_value())
            {
                try
                {
                    const auto snapshot =
                        proposal_contexts->snapshot(key);
                    const auto quorum =
                        proposal_contexts->frozen_global_quorum(*lease);
                    if (snapshot.has_value() && quorum.has_value())
                        emit_adaptive_aggregation_event(
                            AdaptiveAggregationTransition::
                                root_qc_published,
                            *lease,
                            &snapshot->verified_signers,
                            nullptr,
                            nullptr,
                            snapshot->verified_signers.size(),
                            *quorum);
                }
                catch (...)
                {
                }
                proposal_contexts->transition(
                    *lease,
                    ProposalContextEvent::root_qc_published);
            }
        }
    }

    void HotStuffBase::try_finish_exact_context(
        const ProposalContextLease &lease)
    {
        if (!proposal_contexts->revalidate(lease))
            return;
        const auto &tree = lease.tree();
        if (!tree.parent.has_value())
        {
            auto final_qc =
                proposal_contexts->clone_publishable_root_qc(lease);
            if (final_qc == nullptr)
                return;
            final_qc->compute();
            if (!final_qc->verify(config))
                return;
            if (proposal_contexts->claim_root_qc_progress(lease))
                pmaker->record_verified_progress(
                    lease.key().configuration,
                    LeaderProgressEvent::quorum_certificate);
            if (!publish_exact_root_qc(
                    lease, std::move(final_qc)))
                return;
            try
            {
                const auto snapshot =
                    proposal_contexts->snapshot(lease.key());
                const auto quorum =
                    proposal_contexts->frozen_global_quorum(lease);
                if (snapshot.has_value() && quorum.has_value())
                    emit_adaptive_aggregation_event(
                        AdaptiveAggregationTransition::root_qc_published,
                        lease,
                        &snapshot->verified_signers,
                        nullptr,
                        nullptr,
                        snapshot->verified_signers.size(),
                        *quorum);
            }
            catch (...)
            {
            }
            proposal_contexts->transition(
                lease, ProposalContextEvent::root_qc_published);
            drain_ready_piped_qcs();
            return;
        }

        const bool initial_ready = tree.optional_subtree.empty()
                                       ? proposal_contexts
                                             ->assigned_subtree_complete(lease)
                                       : proposal_contexts
                                             ->required_subtree_complete(lease);
        if (proposal_contexts->delta_open_enabled(lease) ||
            !initial_ready)
            return;
        if (config.get_peer_id(*tree.parent).is_null())
            return;
        auto aggregate = proposal_contexts->clone_accumulator(lease);
        if (aggregate == nullptr)
            return;
        aggregate->compute();
        if (!aggregate->verify(config))
            return;
        auto claim = proposal_contexts
                         ->claim_initial_certificate_reservation(
            lease, std::move(aggregate));
        if (!claim.has_value())
            return;
        try
        {
            const auto missing_optional =
                proposal_contexts->missing_optional_signers(lease);
            emit_adaptive_aggregation_event(
                AdaptiveAggregationTransition::required_set_ready,
                lease,
                &claim->signers,
                missing_optional.has_value() ? &*missing_optional : nullptr);
        }
        catch (...)
        {
        }
        if (!send_exact_relay_reserved(
                lease,
                std::move(*claim),
                ExactForwardingRole::initial_aggregate))
            return;
    }

    void HotStuffBase::local_vote_authorized(const ProposalKey &key)
    {
        HOTSTUFF_LOG_PROTO(
            "[PROP HANDLER] Local vote authorized for %.10s",
            key.block_hash.to_hex().c_str());
    }

    void HotStuffBase::create_expected_vote_state(const ProposalKey &key)
    {
        const auto lease = proposal_contexts->acquire_open_context(key);
        if (lease.has_value())
            static_cast<void>(
                proposal_contexts->pending_children(*lease));
    }

    void HotStuffBase::start_latency_deadline(const ProposalKey &key)
    {
        const auto lease = proposal_contexts->acquire_open_context(key);
        if (!lease.has_value())
            return;
        for (const auto child : lease->tree().direct_children)
            proposal_contexts->record_latency_start(*lease, child);
    }

    void HotStuffBase::start_aggregation_timer(const ProposalKey &key)
    {
        const auto *tree = find_exact_runtime_tree(key.configuration);
        const auto lease = proposal_contexts->acquire_open_context(key);
        if (tree == nullptr || !lease.has_value() ||
            lease->tree().direct_children.empty() ||
            aggregation_timeout_coordinator == nullptr ||
            aggregation_scheduler == nullptr)
            return;

        aggregation_timeout_coordinator->arm_timeout(
            *lease,
            *aggregation_scheduler,
            static_cast<std::uint32_t>(tree->get_level(get_id())),
            static_cast<std::uint32_t>(tree->get_max_level()));
    }

    void HotStuffBase::emit_timeout_report(const ProposalKey &)
    {
        // Timeout-report generation remains in the existing timer path. P05
        // exposes this boundary only to prove future proposals never cross it.
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

    bool HotStuffBase::deliver_blk_without_finalization(
        const block_t &blk)
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

        return valid;
    }

    bool HotStuffBase::on_deliver_blk(const block_t &blk)
    {
        return blk_delivery_orchestrator.external_delivery(
            blk->get_hash(),
            blk,
            [this](const block_t &block)
            {
                return deliver_blk_without_finalization(block);
            });
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
            return promise_t([this, blk_hash](promise_t pm)
                             { pm.resolve(storage->find_blk(blk_hash)); });

        const auto elapsed = std::make_shared<ElapsedTime>();
        elapsed->start();
        BlockDeliveryTimingHooks timing{
            [elapsed]()
            {
                elapsed->stop(false);
                return elapsed->elapsed_sec;
            },
            [this](double sec)
            {
                part_delivery_time += sec;
                part_delivery_time_min =
                    std::min(part_delivery_time_min, sec);
                part_delivery_time_max =
                    std::max(part_delivery_time_max, sec);
            }};

        BlockDeliveryAsyncPlan plan{
            [this, blk_hash, replica]()
            {
                return async_fetch_blk(blk_hash, &replica);
            },
            [this](const block_t &block)
            {
                if (block == get_genesis())
                {
                    promise_t verified;
                    verified.resolve(true);
                    return verified;
                }
                return block->verify(this, vpool);
            },
            [this, replica](const block_t &block)
            {
                const auto &qc = block->get_qc();
                if (qc == nullptr)
                    throw std::runtime_error(
                        "fetched block has no quorum certificate");
                return async_fetch_blk(qc->get_obj_hash(), &replica);
            },
            [this, replica](const block_t &block)
            {
                std::vector<promise_t> parents;
                parents.reserve(block->get_parent_hashes().size());
                for (const auto &parent_hash : block->get_parent_hashes())
                    parents.push_back(
                        async_deliver_blk(parent_hash, replica));
                return parents;
            },
            [this](const block_t &block)
            {
                if (storage->is_blk_delivered(block->get_hash()))
                    return true;
                return deliver_blk_without_finalization(block);
            }};

        return blk_delivery_orchestrator.async_delivery(
            blk_hash,
            std::move(timing),
            std::move(plan));
    }

    void HotStuffBase::propose_handler(MsgPropose &&msg, const Net::conn_t &conn)
    {
        const PeerId &peer = conn->get_peer_id();

        if (peer.is_null() || proposal_admission == nullptr)
            return;

        const bytearray_t wire_payload =
            static_cast<bytearray_t>(msg.serialized);
        // Detached structural validation completes before the only call into
        // the admission state machine.
        const auto admission = admit_proposal_payload(
            wire_payload, peer, *this, *proposal_admission);
        const auto &key = admission.key;
        switch (admission.disposition)
        {
        case ProposalDisposition::buffered_future:
            if (const auto metadata = exact_context_metadata(key);
                !metadata.has_value() ||
                !proposal_contexts->buffer_future(*metadata))
            {
                HOTSTUFF_LOG_WARN(
                    "[PROP HANDLER] Failed to retain exact future context");
                break;
            }
            HOTSTUFF_LOG_PROTO(
                "[PROP HANDLER] Buffered exact future proposal epoch=%u "
                "tid=%u block=%.10s",
                key.configuration.epoch_number,
                key.configuration.tree_id,
                key.block_hash.to_hex().c_str());
            break;
        case ProposalDisposition::admitted_active:
        case ProposalDisposition::duplicate:
            break;
        case ProposalDisposition::rejected_malformed:
            HOTSTUFF_LOG_WARN(
                "[PROP HANDLER] Rejected malformed proposal payload");
            break;
        default:
            HOTSTUFF_LOG_WARN(
                "[PROP HANDLER] Rejected proposal for epoch=%u tid=%u",
                key.configuration.epoch_number,
                key.configuration.tree_id);
            break;
        }
    }

    void HotStuffBase::adaptive_stage_epoch_handler(
        MsgStageEpochDefinition &&message,
        const Net::conn_t &conn)
    {
        const auto peer = conn->get_peer_id();
        if (!authorize_manager_peer(peer) || epoch_live_binding == nullptr)
            return;
        EpochValidationContext context;
        const auto current = pmaker->get_current_proposal();
        context.current_height =
            current == nullptr ? 0 : current->get_height();
        context.minimum_activation_grace =
            epoch_activation_grace_blocks;
        static_cast<void>(epoch_live_binding->handle_stage(
            std::move(message),
            AuthenticatedEpochPeer::manager(),
            context));
    }

    void HotStuffBase::adaptive_arm_epoch_handler(
        MsgArmActivation &&message,
        const Net::conn_t &conn)
    {
        const auto peer = conn->get_peer_id();
        if (!authorize_manager_peer(peer) || epoch_live_binding == nullptr)
            return;
        static_cast<void>(epoch_live_binding->handle_arm(
            std::move(message), AuthenticatedEpochPeer::manager()));
    }

    void HotStuffBase::adaptive_propose_handler(
        MsgPropose &&message,
        const Net::conn_t &conn)
    {
        const auto peer = conn->get_peer_id();
        const auto authenticated = peer_id_map.find(peer);
        if (peer.is_null() || authenticated == peer_id_map.end() ||
            epoch_live_binding == nullptr)
            return;
        const auto authenticated_peer = authenticated_epoch_replica(
            authenticated->second, peer);
        static_cast<void>(epoch_live_binding->handle_proposal(
            std::move(message), authenticated_peer));
    }

    void HotStuffBase::adaptive_vote_handler(
        MsgVote &&message,
        const Net::conn_t &conn)
    {
        const auto peer = conn->get_peer_id();
        const auto authenticated = peer_id_map.find(peer);
        if (peer.is_null() || authenticated == peer_id_map.end() ||
            epoch_live_binding == nullptr)
            return;
        const auto authenticated_peer = authenticated_epoch_replica(
            authenticated->second, peer);
        static_cast<void>(epoch_live_binding->handle_vote(
            std::move(message), authenticated_peer));
    }

    void HotStuffBase::adaptive_relay_handler(
        MsgRelay &&message,
        const Net::conn_t &conn)
    {
        const auto peer = conn->get_peer_id();
        const auto authenticated = peer_id_map.find(peer);
        if (peer.is_null() || authenticated == peer_id_map.end() ||
            epoch_live_binding == nullptr)
            return;
        const auto authenticated_peer = authenticated_epoch_replica(
            authenticated->second, peer);
        static_cast<void>(epoch_live_binding->handle_relay(
            std::move(message), authenticated_peer));
    }

#if 0
    void HotStuffBase::legacy_vote_handler(MsgVote &&msg, const Net::conn_t &conn)
    {
        const auto &peer = conn->get_peer_id();
        if (peer.is_null())
            return;

        if (!msg.postponed_parse(this))
        {
            HOTSTUFF_LOG_WARN(
                "[VOTE HANDLER] Rejecting malformed vote payload");
            return;
        }

        const auto *message_tree =
            find_exact_runtime_tree(msg.vote.configuration());
        if (message_tree == nullptr)
        {
            HOTSTUFF_LOG_WARN("[VOTE HANDLER] Rejecting vote for unknown epoch/tree");
            return;
        }

        const auto authenticated_replica = peer_id_map.find(peer);
        if (authenticated_replica == peer_id_map.end() ||
            message_tree->get_childPeers().find(peer) ==
                message_tree->get_childPeers().end() ||
            !validate_authenticated_vote(config, peer, msg.vote))
        {
            HOTSTUFF_LOG_WARN("[VOTE HANDLER] Rejecting unauthenticated or malformed vote for block %.10s",
                              msg.vote.blk_hash.to_hex().c_str());
            return;
        }

        const auto authenticated_id = authenticated_replica->second;
        TreeNetwork tree(*message_tree);
        RcObj<Vote> v(new Vote(std::move(msg.vote)));
        const auto block_hash = v->blk_hash;
        coordinate_verified_delivery(
            *v,
            [this, v]()
            {
                return v->cert->verify(
                    config.get_pubkey(v->voter), vpool);
            },
            [this, block_hash, peer]()
            {
                return async_deliver_blk(block_hash, peer);
            },
            [this,
             v,
             peer,
             authenticated_id,
             tree = std::move(tree)](const block_t &delivered_block)
            {
        if (!admit_verified_vote(config,
                                 v->epoch_nr,
                                 tree,
                                 peer,
                                 *v,
                                 true))
        {
            HOTSTUFF_LOG_WARN("[VOTE HANDLER] Rejecting invalid vote for block %.10s",
                              v->blk_hash.to_hex().c_str());
            return;
        }

        const auto msg_epoch_nr = v->epoch_nr;
        const auto msg_tree_id = v->tid;
        const auto blk_hash = v->blk_hash;
        const auto parentPeer = tree.get_parentPeer();
        const auto childrenSet = tree.get_childrenSet();
        const auto tree_proposer = tree.get_tree().get_tree_root();

        if (!delivered_block->delivered ||
            delivered_block->get_hash() != blk_hash)
        {
            HOTSTUFF_LOG_WARN(
                "[VOTE HANDLER] Rejecting mismatched block delivery for block %.10s",
                blk_hash.to_hex().c_str());
            return;
        }
        block_t blk = delivered_block;

        if (blk->self_qc != nullptr &&
            blk->self_qc->get_proposal_key() != v->key())
        {
            HOTSTUFF_LOG_WARN(
                "[VOTE HANDLER] Rejecting vote for an aliased proposal key");
            return;
        }

        HOTSTUFF_LOG_PROTO("[VOTE HANDLER] Received VOTE message in epoch_nr=%d, tid=%d from ReplicaId %d for block %.10s", msg_epoch_nr, msg_tree_id, authenticated_id, blk_hash.to_hex().c_str());

        // Could be sooner
        record_latency(msg_epoch_nr, msg_tree_id, peer, blk_hash);

        if (id == tree_proposer && !piped_queue.empty() && std::find(piped_queue.begin(), piped_queue.end(), blk_hash) != piped_queue.end())
        {
            HOTSTUFF_LOG_PROTO("piped block");
            if (!blk->piped_delivered)
            {
                process_block(blk, false, v->configuration());
                blk->piped_delivered = true;
                HOTSTUFF_LOG_PROTO("Normalized piped block");
            }
        }

        auto it = pending_votes.find(blk_hash);
        if (it != pending_votes.end())
        {
            it->second.erase(authenticated_id);

            if (it->second.empty())
            {
                pending_votes.erase(it);
            }
        }

        /** PASS-THROUGH MODE CHECK **/
        if (pass_trought_blks.count(blk_hash) > 0)
        {
            HOTSTUFF_LOG_PROTO("[PASS-THRU] Found pass_through tag for blk=%.10s; building single partial QC and forwarding up if possible.", blk_hash.to_hex().c_str());

            quorum_cert_bt single_qc = create_quorum_cert(v->key());
            single_qc->add_verified_part(config, v->voter, *v->cert);
            single_qc->compute();

            // If there's a parent to forward to, do so
            if (!parentPeer.is_null())
            {
                HOTSTUFF_LOG_PROTO("[PASS-THRU] Forwarding single vote to parent for blk=%.10s ...", blk_hash.to_hex().c_str());
                pn.send_msg(MsgRelay(VoteRelay(
                    v->key(), single_qc->clone(), this)), parentPeer);
            }

            // Don’t do local QC merging for this block. Just return.
            return;
        }

        if (blk->self_qc == nullptr)
        {
            blk->self_qc = create_quorum_cert(v->key());
            part_cert_bt part = create_part_cert(*priv_key, v->key());
            blk->self_qc->add_verified_part(config, id, *part);

            std::cout << "[VOTE HANDLER] Created local self_qc for block: " << blk_hash.to_hex() << " " << &blk->self_qc << std::endl;
        }

        if (blk->self_qc->has_n(config.nmajority))
        {
            HOTSTUFF_LOG_PROTO("[VOTE HANDLER] Already has nmajority for blk=%.10s, skipping...", blk_hash.to_hex().c_str());
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

            if (cert->has_n(static_cast<int>(childrenSet.size()) + 1))
            {
                return;
            }

            cert->add_verified_part(config, v->voter, *v->cert);

            if (!cert->has_n(static_cast<int>(childrenSet.size()) + 1))
            {
                HOTSTUFF_LOG_PROTO("[VOTE HANDLER] Not enough child votes yet for blk=%.10s; returning...", blk_hash.to_hex().c_str());
                return;
            }

            HOTSTUFF_LOG_PROTO("[VOTE HANDLER] Received all children votes (%d) + my own for blk=%.10s! Total signatures now: %d", static_cast<int>(childrenSet.size()), blk_hash.to_hex().c_str(), cert->get_sigs_n());

            if (!tree.is_leaf())
                stop_proposal_timer(blk_hash);

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

            std::cout << "[VOTE HANDLER] Sending VOTE-RELAY for blk= " << blk_hash.to_hex() << std::endl;
            if (!parentPeer.is_null())
            {

                HOTSTUFF_LOG_PROTO("[VOTE HANDLER] VOTE-RELAY epoch=%d, tid=%d to parentReplicaId=%d, cert size=%d for blk=%.10s",
                                   msg_epoch_nr, msg_tree_id, peer_id_map.at(parentPeer),
                                   cert->get_sigs_n(), blk_hash.to_hex().c_str());

                pn.send_msg(MsgRelay(VoteRelay(
                    v->key(), blk->self_qc->clone(), this)), parentPeer);
            }

            return;
        }

        auto &cert = blk->self_qc;
        cert->add_verified_part(config, v->voter, *v->cert);
        if (cert != nullptr && cert->get_proposal_key() == v->key())
        {
            if (cert->has_n(config.nmajority))
            {
                cert->compute();
                update_hqc(blk, cert);
                on_qc_finish(blk);
            }
        }

        /*
        gettimeofday(&timeEnd, NULL);

        std::cout << "Vote handling cost: "
                  << ((timeEnd.tv_sec - timeStart.tv_sec) * 1000000 + timeEnd.tv_usec - timeStart.tv_usec)
                  << " us to execute."
                  << std::endl;*/
        });
    }

    void HotStuffBase::legacy_vote_relay_handler(MsgRelay &&msg, const Net::conn_t &conn)
    {
        const auto &peer = conn->get_peer_id();
        if (peer.is_null())
            return;
        if (!msg.postponed_parse(this))
        {
            HOTSTUFF_LOG_WARN(
                "[RELAY HANDLER] Rejecting malformed relay payload");
            return;
        }
        const auto *message_tree =
            find_exact_runtime_tree(msg.vote.configuration());
        if (message_tree == nullptr)
        {
            HOTSTUFF_LOG_WARN("[RELAY HANDLER] Rejecting relay for unknown epoch/tree");
            return;
        }

        const auto authenticated_replica = peer_id_map.find(peer);
        if (authenticated_replica == peer_id_map.end() ||
            message_tree->get_childPeers().find(peer) ==
                message_tree->get_childPeers().end() ||
            !validate_relay_envelope(config, msg.vote))
        {
            HOTSTUFF_LOG_WARN("[RELAY HANDLER] Rejecting unauthenticated or malformed relay for block %.10s",
                              msg.vote.blk_hash.to_hex().c_str());
            return;
        }

        const auto authenticated_id = authenticated_replica->second;
        TreeNetwork tree(*message_tree);
        RcObj<VoteRelay> v(new VoteRelay(std::move(msg.vote)));
        const auto block_hash = v->blk_hash;
        coordinate_verified_delivery(
            *v,
            [this, v]()
            {
                return v->cert->verify(config, vpool);
            },
            [this, block_hash, peer]()
            {
                return async_deliver_blk(block_hash, peer);
            },
            [this,
             v,
             peer,
             authenticated_id,
             tree = std::move(tree)](const block_t &delivered_block)
            {
        if (!admit_verified_relay(config,
                                  v->epoch_nr,
                                  tree,
                                  peer,
                                  *v,
                                  true))
        {
            HOTSTUFF_LOG_WARN("[RELAY HANDLER] Rejecting invalid relay for block %.10s",
                              v->blk_hash.to_hex().c_str());
            return;
        }

        const auto msg_epoch_nr = v->epoch_nr;
        const auto msg_tree_id = v->tid;
        const auto blk_hash = v->blk_hash;
        const auto parentPeer = tree.get_parentPeer();
        const auto childrenSet = tree.get_childrenSet();
        const auto tree_proposer = tree.get_tree().get_tree_root();

        if (!delivered_block->delivered ||
            delivered_block->get_hash() != blk_hash)
        {
            HOTSTUFF_LOG_WARN(
                "[RELAY HANDLER] Rejecting mismatched block delivery for block %.10s",
                blk_hash.to_hex().c_str());
            return;
        }
        block_t blk = delivered_block;

        if (blk->self_qc != nullptr &&
            blk->self_qc->get_proposal_key() != v->key())
        {
            HOTSTUFF_LOG_WARN(
                "[RELAY HANDLER] Rejecting relay for an aliased proposal key");
            return;
        }

        HOTSTUFF_LOG_PROTO("[RELAY HANDLER] Started. From peer=%d, block=%.10s", authenticated_id, v->blk_hash.to_hex().c_str());

        HOTSTUFF_LOG_PROTO("[RELAY HANDLER] Received VOTE-RELAY message in epoch_nr=%d, tid=%d from ReplicaId %d with a cert of size %d", msg_epoch_nr, msg_tree_id, authenticated_id, v->cert->get_sigs_n());

        // Could be sooner
        record_latency(msg_epoch_nr, msg_tree_id, peer, blk_hash);

        if (id == tree_proposer && !piped_queue.empty() && std::find(piped_queue.begin(), piped_queue.end(), blk_hash) != piped_queue.end())
        {
            HOTSTUFF_LOG_PROTO("piped block");
            if (!blk->piped_delivered)
            {
                process_block(blk, false, v->configuration());
                blk->piped_delivered = true;
                HOTSTUFF_LOG_PROTO("Normalized piped block");
            }
        }

        auto it = pending_votes.find(blk_hash);
        if (it != pending_votes.end())
        {
            it->second.erase(authenticated_id);
            if (it->second.empty())
            {
                pending_votes.erase(it);
            }
        }

        // NÃO FAZ SENTIDO SE NÃO NUNCA HAVERIA UM VOTE-RELAY??
        if (blk->self_qc == nullptr)
        {
            HOTSTUFF_LOG_PROTO("[RELAY HANDLER] Creating new self_qc for block=%.10s", blk->hash.to_hex().c_str());
            blk->self_qc = create_quorum_cert(v->key());
            part_cert_bt part = create_part_cert(*priv_key, v->key());
            blk->self_qc->add_verified_part(config, id, *part);

            // Debug printing the pointer, as you do below
            std::cout << "[RELAY HANDLER] Created new self_qc for block="
                      << blk_hash.to_hex() << " pointer=" << &blk->self_qc
                      << std::endl;
        }

        if (blk->self_qc->has_n(config.nmajority))
        {
            HOTSTUFF_LOG_PROTO("[RELAY HANDLER] Already has_n() -> Majority. block=%.10s", blk->hash.to_hex().c_str());

            if (id == tree_proposer && blk->hash == piped_queue.front())
            {
                piped_queue.pop_front();
                HOTSTUFF_LOG_PROTO("[PIPELINING] Popped front block=%.10s, new piped_queue_size=%zu", blk->hash.to_hex().c_str(), piped_queue.size());

                auto curr_blk = blk;
                if (!rdy_queue.empty())
                {
                    HOTSTUFF_LOG_PROTO("[RELAY HANDLER] Resolving rdy_queue for block=%.10s (case 1)", curr_blk->hash.to_hex().c_str());

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

                            if (!tree.is_leaf())
                                stop_proposal_timer(blk->hash);
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

                                if (!tree.is_leaf())
                                    stop_proposal_timer(rdy_blk->hash);

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

        std::cout << "[RELAY HANDLER] Partial QC so far does NOT have a majority. block= "
                  << blk_hash.to_hex() << std::endl;

        // pass_through check:
        if (pass_trought_blks.count(v->blk_hash) > 0)
        {
            HOTSTUFF_LOG_PROTO("[RELAY HANDLER] [PASS-THROUGH] aggregator on blk=%.10s, skipping local merging.",v->blk_hash.to_hex().c_str());
     
            if (!parentPeer.is_null())
            {
                HOTSTUFF_LOG_PROTO("[RELAY HANDLER] [PASS-THROUGH] forwarding aggregator up for blk=%.10s", v->blk_hash.to_hex().c_str());
                pn.send_msg(MsgRelay(VoteRelay(
                    v->key(), v->cert->clone(), this)),parentPeer);
            }
            return;
        }
        
        
        auto &cert = blk->self_qc;

        if (cert != nullptr && cert->get_proposal_key() == v->key() &&
            !cert->has_n(config.nmajority)) {


            if (id != tree_proposer && cert->has_n(static_cast<int>(childrenSet.size()) + 1))
            {
                return;
            }

            HOTSTUFF_LOG_PROTO("[HANDLER] Merging QuorumCert from VOTE-RELAY on block=%.10s. Current sigs_n=%d", blk->hash.to_hex().c_str(), blk->self_qc->get_sigs_n());
            try
            {
                cert->merge_verified_quorum(*v->cert);
            }
            catch (const std::invalid_argument &error)
            {
                HOTSTUFF_LOG_WARN("[RELAY HANDLER] Rejecting relay merge for block %.10s: %s",
                                  blk->hash.to_hex().c_str(), error.what());
                return;
            }
            HOTSTUFF_LOG_PROTO("[HANDLER] After merging, block=%.10s sigs_n=%d", blk->hash.to_hex().c_str(), blk->self_qc->get_sigs_n());

            if (id != tree_proposer) {
                
                // If not enough, just store it and wait
                if (!cert->has_n(static_cast<int>(childrenSet.size()) + 1))
                {
                    HOTSTUFF_LOG_PROTO("[HANDLER] Still not enough child votes for block=%.10s, have %d needed=%d", blk->hash.to_hex().c_str(), cert->get_sigs_n(), static_cast<int>(childrenSet.size()) + 1);
                    return;
                }
                
                cert->compute();

                //Received all child votes
                stop_proposal_timer(v->blk_hash);
                
                std::cout << "[RELAY HANDLER] Sending aggregated VOTE-RELAY upwards. block= " 
                        << v->blk_hash.to_hex() 
                        << std::endl;
                
                if(!parentPeer.is_null()) {

                    HOTSTUFF_LOG_PROTO("[HANDLER] Sending VOTE-RELAY on block=%.10s, new cert_sigs=%d, to parentPeer ID=%d", v->blk_hash.to_hex().c_str(), cert->get_sigs_n(), peer_id_map.at(parentPeer));
                    pn.send_msg(MsgRelay(VoteRelay(
                        v->key(), cert.get()->clone(), this)), parentPeer);
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

            //not sure if it's here
            stop_proposal_timer(v->blk_hash);

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

#endif

    void HotStuffBase::vote_handler(MsgVote &&msg, const Net::conn_t &conn)
    {
        const auto &peer = conn->get_peer_id();
        if (peer.is_null())
            return;
        if (!msg.postponed_parse(this))
            return;

        const auto *message_tree =
            find_exact_runtime_tree(msg.vote.configuration());
        if (message_tree == nullptr)
            return;

        const auto authenticated = peer_id_map.find(peer);
        if (authenticated == peer_id_map.end() ||
            !validate_authenticated_vote(config, peer, msg.vote))
            return;

        auto envelope = make_exact_direct_envelope(
            msg.vote, authenticated->second);
        const auto handle_direct_kind =
            ExactContributionKind::direct_vote;
        buffer_or_dispatch_exact_contribution(
            handle_direct_kind,
            std::move(envelope),
            peer);
    }

    void HotStuffBase::vote_relay_handler(
        MsgRelay &&msg,
        const Net::conn_t &conn)
    {
        const auto &peer = conn->get_peer_id();
        if (peer.is_null())
            return;
        if (!msg.postponed_parse(this))
            return;

        const auto *message_tree =
            find_exact_runtime_tree(msg.vote.configuration());
        if (message_tree == nullptr)
            return;

        const auto authenticated = peer_id_map.find(peer);
        if (authenticated == peer_id_map.end() ||
            !validate_relay_envelope(config, msg.vote))
            return;

        auto envelope = make_exact_relay_envelope(
            msg.vote, authenticated->second);
        const auto handle_relay_kind =
            ExactContributionKind::aggregate_relay;
        buffer_or_dispatch_exact_contribution(
            handle_relay_kind,
            std::move(envelope),
            peer);
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
        LOG_INFO("blk_delivery_waiting: %lu", blk_delivery_orchestrator.size());
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
                               const Net::Config &netconfig,
                               NetAddr reputation_addr,
                               EpochProtocolMode protocol_mode) : HotStuffCore(rid, std::move(priv_key)),
                                                          listen_addr(listen_addr),
                                                          blk_size(blk_size),
                                                          ec(ec),
                                                          tcall(ec),
                                                          vpool(ec, nworker),
                                                          pn(ec, netconfig),
                                                          epoch_protocol_mode(protocol_mode),
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
                                                          part_delivery_time_max(0),
                                                          reputation_addr(reputation_addr),
                                                          aggregation_scheduler(std::make_unique<SalticidaeAggregationScheduler>(ec)),
                                                          proposal_contexts(std::make_shared<ProposalContextLifecycle>()),
                                                          exact_runtime_access(std::make_shared<ExactRuntimeAccess>(this)),
                                                          aggregation_timeout_policy(adaptive_timeout_from_seconds(0.5)),
                                                          reconfig_count(0),
                                                          warmup_finished(false)
    {
        initialize_committed_epoch_change_history();
        rebuild_aggregation_timeout_coordinator();

        /* register the handlers for msg from replicas */
        if (epoch_protocol_mode == EpochProtocolMode::adaptive_v1)
            install_adaptive_epoch_handlers();
        else
            install_legacy_consensus_handlers();
        pn.reg_handler(salticidae::generic_bind(&HotStuffBase::req_blk_handler, this, _1, _2));
        pn.reg_handler(salticidae::generic_bind(&HotStuffBase::resp_blk_handler, this, _1, _2));
        pn.reg_conn_handler(salticidae::generic_bind(&HotStuffBase::conn_handler, this, _1, _2));
        pn.start();
        pn.listen(listen_addr);

        rn.start();
        reputation_server_conn = rn.connect_sync(reputation_addr);
    }

    void HotStuffBase::install_legacy_consensus_handlers()
    {
        pn.reg_handler(salticidae::generic_bind(
            &HotStuffBase::propose_handler, this, _1, _2));
        pn.reg_handler(salticidae::generic_bind(
            &HotStuffBase::vote_handler, this, _1, _2));
        pn.reg_handler(salticidae::generic_bind(
            &HotStuffBase::vote_relay_handler, this, _1, _2));
    }

    void HotStuffBase::install_adaptive_epoch_handlers()
    {
        pn.reg_handler(salticidae::generic_bind(
            &HotStuffBase::adaptive_stage_epoch_handler,
            this, _1, _2));
        pn.reg_handler(salticidae::generic_bind(
            &HotStuffBase::adaptive_arm_epoch_handler,
            this, _1, _2));
        pn.reg_handler(salticidae::generic_bind(
            &HotStuffBase::adaptive_propose_handler,
            this, _1, _2));
        pn.reg_handler(salticidae::generic_bind(
            &HotStuffBase::adaptive_vote_handler,
            this, _1, _2));
        pn.reg_handler(salticidae::generic_bind(
            &HotStuffBase::adaptive_relay_handler,
            this, _1, _2));
    }

    bool HotStuffBase::authorize_manager_peer(
        const PeerId &peer) const noexcept
    {
        return epoch_protocol_mode == EpochProtocolMode::adaptive_v1 &&
               epoch_manager_peer.has_value() && !peer.is_null() &&
               peer == *epoch_manager_peer;
    }

    void HotStuffBase::configure_epoch_manager(
        const PeerId &manager_peer,
        const NetAddr &manager_address)
    {
        if (epoch_protocol_mode != EpochProtocolMode::adaptive_v1 ||
            manager_peer.is_null())
            throw std::logic_error(
                "epoch manager is available only in adaptive mode");
        epoch_manager_peer = manager_peer;
        valid_tls_certs.insert(
            static_cast<const uint256_t &>(manager_peer));
        pn.add_peer(manager_peer);
        pn.set_peer_addr(manager_peer, manager_address);
        pn.conn_peer(manager_peer);
    }

    ReplicaStageIngressResult HotStuffBase::trusted_local_stage_epoch(
        StageEpochDefinition definition,
        const EpochValidationContext &validation_context)
    {
        if (epoch_protocol_mode != EpochProtocolMode::adaptive_v1 ||
            epoch_live_binding == nullptr)
            throw std::logic_error(
                "trusted local staging requires an initialized adaptive runtime");
        const ConfigurationId configuration{
            definition.activation.successor_epoch_number,
            0,
            definition.activation.successor_epoch_digest};
        const auto activation_height =
            definition.activation.activation_height;
        auto result = epoch_live_binding->handle_stage(
            MsgStageEpochDefinition(definition, epoch_wire_limits),
            AuthenticatedEpochPeer::manager(),
            validation_context);
        if (result.error == EpochIngressError::none &&
            result.disposition == ReplicaStageDisposition::staged)
            emit_epoch_lifecycle_event(
                EpochLifecycleTransition::staged,
                configuration,
                activation_height);
        return result;
    }

    ReplicaArmIngressResult HotStuffBase::trusted_local_arm_epoch(
        ArmActivation activation)
    {
        if (epoch_protocol_mode != EpochProtocolMode::adaptive_v1 ||
            epoch_live_binding == nullptr)
            throw std::logic_error(
                "trusted local arming requires an initialized adaptive runtime");
        const ConfigurationId configuration{
            activation.activation.successor_epoch_number,
            0,
            activation.activation.successor_epoch_digest};
        const auto activation_height =
            activation.activation.activation_height;
        auto result = epoch_live_binding->handle_arm(
            MsgArmActivation(activation, epoch_wire_limits),
            AuthenticatedEpochPeer::manager());
        if (result.error == EpochIngressError::none &&
            result.disposition == ReplicaArmDisposition::armed)
            emit_epoch_lifecycle_event(
                EpochLifecycleTransition::activation_armed,
                configuration,
                activation_height);
        return result;
    }

    bool HotStuffBase::bootstrap_adaptive_epoch_from_file(
        const std::string &configuration_path,
        std::uint64_t activation_height)
    {
        if (epoch_protocol_mode != EpochProtocolMode::adaptive_v1 ||
            adaptive_epoch_runtime == nullptr || configuration_path.empty())
            return false;

        try
        {
            std::ifstream file(configuration_path);
            if (!file.is_open())
                throw std::runtime_error(
                    "cannot open adaptive epoch configuration file");
            std::ostringstream contents;
            contents << file.rdbuf();

            const auto predecessor =
                adaptive_epoch_runtime->activation.active_effect();
            const auto successor = checked_successor_epoch(
                predecessor.configuration.epoch_number);
            if (!successor.has_value())
                throw std::overflow_error(
                    "adaptive epoch number overflows");

            auto input = parse_legacy_epoch_zero(
                contents.str(), fixed_membership);
            input.epoch_number = *successor;
            input.previous_epoch_digest =
                predecessor.configuration.epoch_digest;
            input.activation_height = activation_height;
            input.generation_seed = activation_height;
            input.policy_version = "trusted-local-adaptive-v1";
            input.evidence_snapshot_id =
                "trusted-local-epoch-" + std::to_string(*successor);
            input.evidence_cutoff = activation_height;
            input.epoch_digest.reset();
            const auto successor_digest = compute_epoch_digest(input);
            input.epoch_digest = successor_digest;
            const auto activation_tree = std::find_if(
                input.trees.begin(),
                input.trees.end(),
                [](const EpochTreeDefinition &tree) {
                    return tree.tree_id == 0;
                });
            if (activation_tree == input.trees.end() ||
                activation_tree->members_breadth_first.empty())
                throw std::invalid_argument(
                    "trusted-local successor has no activation tree zero");
            const auto successor_root =
                activation_tree->members_breadth_first.front();

            const EpochActivationIdentity identity{
                predecessor.configuration.epoch_number,
                predecessor.configuration.epoch_digest,
                *successor,
                successor_digest,
                activation_height};
            EpochValidationContext validation_context;
            const auto current = pmaker->get_current_proposal();
            validation_context.current_height =
                current == nullptr ? 0 : current->get_height();
            validation_context.minimum_activation_grace =
                epoch_activation_grace_blocks;

            const auto staged = trusted_local_stage_epoch(
                StageEpochDefinition{
                    kEpochWireSchemaVersion,
                    EpochProtocolMode::adaptive_v1,
                    identity,
                    std::move(input)},
                validation_context);
            if (staged.error != EpochIngressError::none ||
                !staged.acknowledgement.has_value())
                return false;

            const auto armed = trusted_local_arm_epoch(ArmActivation{
                kEpochWireSchemaVersion,
                EpochProtocolMode::adaptive_v1,
                identity});
            if (armed.error != EpochIngressError::none ||
                !armed.disposition.has_value())
                return false;

            adaptive_demo_markers = true;
            HOTSTUFF_LOG_INFO(
                "KAURI_DEMO bootstrap_staged replica=%u active_epoch=%u "
                "active_root=%u successor_epoch=%u successor_root=%u "
                "activation_height=%llu epoch_digest=%s",
                get_id(),
                predecessor.configuration.epoch_number,
                adaptive_epoch_runtime->topology.current_tree()
                    .get_tree().get_tree_root(),
                *successor,
                successor_root,
                activation_height,
                successor_digest.to_hex().c_str());
            HOTSTUFF_LOG_INFO(
                "[EPOCH] Trusted-local epoch=%u staged and armed for height=%llu",
                *successor,
                activation_height);
            return true;
        }
        catch (const std::exception &error)
        {
            HOTSTUFF_LOG_WARN(
                "[EPOCH] Trusted-local bootstrap failed: %s",
                error.what());
            return false;
        }
    }

    void HotStuffBase::rebuild_aggregation_timeout_coordinator()
    {
        AggregationTimeoutEffects aggregation_effects;
        aggregation_effects.try_send_upward =
            [this](const ProposalContextLease &lease,
                   ProposalForwardingClaim claim)
            {
                auto retained = claim.certificate == nullptr
                                    ? quorum_cert_bt()
                                    : claim.certificate->clone();
                const auto signers = claim.signers;
                const auto pending_candidate_id =
                    claim.pending_candidate_id;
                const bool enqueued = send_exact_relay(
                    lease, std::move(claim.certificate));
                if (!enqueued && retained != nullptr)
                    schedule_exact_forwarding_retry(
                        lease,
                        std::move(retained),
                        signers,
                        pending_candidate_id,
                        ExactForwardingRole::initial_aggregate,
                        1);
                return enqueued;
            };
        aggregation_effects.record_timeout =
            [this](const ProposalContextLease &lease,
                   const std::set<ReplicaID> &missing)
            {
                record_aggregation_timeout(lease, missing);
            };
        aggregation_effects.record_optional_absence =
            [this](const ProposalContextLease &lease,
                   const std::set<ReplicaID> &missing)
            {
                record_optional_aggregation_absence(lease, missing);
            };
        aggregation_effects.record_initial_forwarding =
            [this](const ProposalContextLease &lease,
                   const std::set<ReplicaID> &signers,
                   AggregationForwardingObservation observation)
            {
                auto transition =
                    AdaptiveAggregationTransition::initial_reserved;
                switch (observation)
                {
                    case AggregationForwardingObservation::reserved:
                        transition = AdaptiveAggregationTransition::
                            initial_reserved;
                        break;
                    case AggregationForwardingObservation::enqueued:
                        transition = AdaptiveAggregationTransition::
                            initial_enqueued;
                        break;
                    case AggregationForwardingObservation::committed:
                        transition = AdaptiveAggregationTransition::
                            initial_committed;
                        break;
                    case AggregationForwardingObservation::released:
                        transition = AdaptiveAggregationTransition::
                            initial_released;
                        break;
                }
                emit_adaptive_aggregation_event(
                    transition, lease, &signers);
            };
        aggregation_timeout_coordinator =
            std::make_unique<AggregationTimeoutCoordinator>(
                *proposal_contexts,
                aggregation_timeout_policy,
                std::move(aggregation_effects),
                [this](const ProposalContextLease &lease)
                {
                    return verified_aggregation_candidate(lease);
                });
    }

    void HotStuffBase::set_aggregation_timeout(double timeout_seconds)
    {
        if (proposal_contexts->active_configuration().has_value())
            throw std::logic_error(
                "aggregation timeout must be configured before startup");
        aggregation_timeout_policy = AggregationTimeoutPolicy(
            adaptive_timeout_from_seconds(timeout_seconds));
        rebuild_aggregation_timeout_coordinator();
    }

    void HotStuffBase::configure_epoch_change_pre_vote_gate(
        EpochChangeIssuer issuer,
        EpochChangeDelayBounds delay_bounds,
        std::size_t maximum_block_extra_bytes,
        std::size_t maximum_ancestry_blocks)
    {
        if (epoch_protocol_mode != EpochProtocolMode::adaptive_v2)
            throw std::logic_error(
                "epoch-change voting is available only in adaptive-v2");
        if (proposal_contexts->active_configuration().has_value())
            throw std::logic_error(
                "epoch-change voting must be configured before startup");
        if (epoch_change_verifier != nullptr)
            throw std::logic_error(
                "epoch-change voting configuration is already pinned");
        if (maximum_block_extra_bytes == 0 ||
            maximum_ancestry_blocks == 0)
            throw std::invalid_argument(
                "epoch-change voting bounds must be nonzero");

        auto verifier = std::make_unique<EpochChangeVerifier>(
            std::move(issuer), delay_bounds);
        epoch_change_maximum_block_extra_bytes =
            maximum_block_extra_bytes;
        epoch_change_maximum_ancestry_blocks =
            maximum_ancestry_blocks;
        epoch_change_verifier = std::move(verifier);
    }

    void HotStuffBase::bind_structured_event_emitters(
        StructuredEventEmitter *lifecycle_emitter,
        AdaptiveStructuredEventEmitter *aggregation_emitter) noexcept
    {
        structured_event_emitter = lifecycle_emitter;
        adaptive_event_emitter = aggregation_emitter;
    }

    bool HotStuffBase::admit_local(const Proposal &prop)
    {
        const auto metadata = exact_context_metadata(prop.key());
        if (!metadata.has_value() || metadata->tree.root != get_id())
            return false;
        const auto active = proposal_contexts->active_configuration();
        if (!active.has_value() || *active != prop.configuration())
            return false;
        const auto gate = pre_vote_epoch_change_gate(prop);
        if (gate.disposition !=
                EpochChangeProposalDisposition::accepted &&
            gate.disposition !=
                EpochChangeProposalDisposition::duplicate)
            return false;
        return admit_exact_context(
                   *metadata,
                   ProposalContextOrigin::leader_local)
            .has_value();
    }

    void HotStuffBase::apply_local_vote(const Vote &vote)
    {
        const auto lease =
            proposal_contexts->acquire_open_context(vote.key());
        if (!lease.has_value() || vote.cert == nullptr)
            return;
        auto forwarding_candidate =
            make_exact_direct_forwarding_candidate(*lease, vote);
        if (forwarding_candidate == nullptr ||
            !proposal_contexts->record_local_part(
                *lease,
                config,
                get_id(),
                *vote.cert,
                std::move(forwarding_candidate)))
            return;
        if (proposal_contexts->delta_open_enabled(*lease))
        {
            if (!lease->tree().parent.has_value())
            {
                try_finish_exact_context(*lease);
                return;
            }
            static_cast<void>(forward_exact_direct(*lease, vote));
            return;
        }
        try_finish_exact_context(*lease);
    }

    void HotStuffBase::on_verified_local_proposal_progress(
        const ProposalKey &key)
    {
        pmaker->record_verified_progress(
            key.configuration,
            LeaderProgressEvent::verified_proposal);
    }

    void HotStuffBase::on_verified_commit_progress(
        const ProposalKey &key)
    {
        pmaker->record_verified_progress(
            key.configuration,
            LeaderProgressEvent::commit);
    }

    void HotStuffBase::do_broadcast_proposal(const Proposal &prop)
    {
        HOTSTUFF_LOG_PROTO("[BROADCASTING] Broadcasting proposal of size %llu bytes in epoch_nr:%d on tid=%d.", sizeof(prop), prop.epoch_nr, prop.tid);

        const auto *tree = find_exact_runtime_tree(prop.configuration());
        if (tree == nullptr)
        {
            HOTSTUFF_LOG_WARN(
                "[BROADCASTING] Refusing proposal for unknown exact configuration");
            return;
        }
        const auto lease =
            proposal_contexts->acquire_open_context(prop.key());
        const auto metadata = exact_context_metadata(prop.key());
        const bool finalized_before_broadcast =
            !lease.has_value() &&
            proposal_contexts->context_status(prop.key()) ==
                ProposalContextStatus::terminal_closed &&
            prop.blk != nullptr && prop.blk->self_qc != nullptr &&
            prop.blk->self_qc->get_proposal_key() == prop.key() &&
            prop.blk->self_qc->has_n(config.nmajority);
        if (!metadata.has_value() ||
            (!lease.has_value() && !finalized_before_broadcast))
        {
            HOTSTUFF_LOG_WARN(
                "[BROADCASTING] Refusing an inadmissible exact proposal");
            return;
        }
        if (lease.has_value())
        {
            create_expected_vote_state(prop.key());
            start_latency_deadline(prop.key());
            start_aggregation_timer(prop.key());
        }

        bytearray_t adaptive_payload;
        if (epoch_protocol_mode == EpochProtocolMode::adaptive_v1)
        {
            if (adaptive_epoch_runtime == nullptr)
                return;
            const auto generation = find_exact_runtime_generation(
                prop.configuration());
            if (!generation.has_value())
                return;
            try
            {
                const MsgPropose native(prop);
                adaptive_payload = adaptive_epoch_consensus_message(
                    prop.configuration(),
                    *generation,
                    EpochConsensusWireKind::proposal,
                    prop.key(),
                    prop.proposer,
                    prop.proposer,
                    static_cast<bytearray_t>(native.serialized),
                    epoch_wire_limits);
            }
            catch (...)
            {
                return;
            }
            if (adaptive_payload.empty())
                return;
        }

        for (const auto child : metadata->tree.direct_children)
        {
            if (epoch_protocol_mode == EpochProtocolMode::adaptive_v1)
                pn.send_msg(
                    MsgPropose(DataStream(adaptive_payload)),
                    config.get_peer_id(child));
            else
                pn.send_msg(
                    MsgPropose(prop), config.get_peer_id(child));
        }
    }

    void HotStuffBase::inc_time(ReconfigurationType reconfig_type)
    {
        pmaker->inc_time(reconfig_type);
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
        if (vote.key() != prop.key() ||
            proposal_admission == nullptr ||
            !proposal_admission->authorize_local_vote(prop.key()))
        {
            HOTSTUFF_LOG_WARN(
                "[CONSENSUS] Refusing a vote outside exact proposal admission");
            return;
        }

        const auto *message_tree =
            find_exact_runtime_tree(prop.configuration());
        if (message_tree == nullptr)
        {
            HOTSTUFF_LOG_WARN(
                "[CONSENSUS] Refusing a vote for an unavailable exact tree");
            return;
        }
        const auto lease =
            proposal_contexts->acquire_open_context(prop.key());
        if (!lease.has_value() || vote.cert == nullptr)
            return;

        pmaker->beat_resp(prop.proposer).then(
            [access = exact_runtime_access,
             vote,
             prop](ReplicaID)
            {
                auto runtime = access->acquire();
                if (!runtime.has_value())
                    return;
                auto &owner = runtime->owner();
                const auto lease = owner.proposal_contexts
                                       ->acquire_open_context(prop.key());
                if (!lease.has_value() || vote.cert == nullptr)
                    return;
                auto forwarding_candidate =
                    owner.make_exact_direct_forwarding_candidate(
                        *lease, vote);
                if (forwarding_candidate == nullptr ||
                    !owner.proposal_contexts->record_local_part(
                        *lease,
                        owner.config,
                        owner.get_id(),
                        *vote.cert,
                        std::move(forwarding_candidate)))
                    return;

                if (owner.proposal_contexts->delta_open_enabled(
                        *lease))
                {
                    if (!lease->tree().parent.has_value())
                    {
                        owner.try_finish_exact_context(*lease);
                        return;
                    }
                    static_cast<void>(
                        owner.forward_exact_direct(*lease, vote));
                    return;
                }

                if (lease->tree().direct_children.empty())
                {
                    if (!lease->tree().parent.has_value())
                    {
                        owner.try_finish_exact_context(*lease);
                        return;
                    }
                    const auto parent = owner.config.get_peer_id(
                        *lease->tree().parent);
                    if (owner.epoch_protocol_mode ==
                        EpochProtocolMode::adaptive_v1)
                    {
                        if (owner.adaptive_epoch_runtime == nullptr)
                            return;
                        const auto generation =
                            owner.find_exact_runtime_generation(
                                vote.configuration());
                        if (!generation.has_value())
                            return;
                        try
                        {
                            const MsgVote native(vote);
                            const auto encoded =
                                adaptive_epoch_consensus_message(
                                    vote.configuration(),
                                    *generation,
                                    EpochConsensusWireKind::vote,
                                    vote.key(),
                                    owner.get_id(),
                                    prop.proposer,
                                    static_cast<bytearray_t>(
                                        native.serialized),
                                    owner.epoch_wire_limits);
                            if (encoded.empty())
                                return;
                            owner.pn.send_msg(
                                MsgVote(DataStream(encoded)), parent);
                        }
                        catch (...)
                        {
                            return;
                        }
                    }
                    else
                        owner.pn.send_msg(MsgVote(vote), parent);
                    owner.proposal_contexts->transition(
                        *lease,
                        ProposalContextEvent::leaf_vote_enqueued);
                    return;
                }
                owner.try_finish_exact_context(*lease);
            });
    }

    std::optional<ProposalKey> HotStuffBase::committed_proposal_key(
        const block_t &blk,
        const std::vector<ProposalKey> &committed_keys) const
    {
        if (blk->self_qc != nullptr)
        {
            const auto &certificate_key =
                blk->self_qc->get_proposal_key();
            if (certificate_key.block_hash == blk->get_hash())
                return certificate_key;
        }
        if (committed_keys.size() == 1)
            return committed_keys.front();
        return std::nullopt;
    }

    void HotStuffBase::record_adaptive_commit_marker(
        const block_t &blk,
        const std::vector<ProposalKey> &committed_keys) const
    {
        if (!adaptive_demo_markers || adaptive_epoch_runtime == nullptr)
            return;

        const auto resolved_key = committed_proposal_key(
            blk, committed_keys);
        if (!resolved_key.has_value())
        {
            HOTSTUFF_LOG_WARN(
                "KAURI_DEMO marker_skipped replica=%u height=%llu "
                "reason=ambiguous_committed_configuration",
                get_id(),
                blk->get_height());
            return;
        }

        const auto &key = *resolved_key;
        const auto *tree = find_exact_runtime_tree(key.configuration);
        if (tree == nullptr)
        {
            HOTSTUFF_LOG_WARN(
                "KAURI_DEMO marker_skipped replica=%u height=%llu "
                "reason=committed_tree_unavailable",
                get_id(),
                blk->get_height());
            return;
        }
        struct timespec event_clock{};
        if (::clock_gettime(CLOCK_MONOTONIC_RAW, &event_clock) != 0)
        {
            HOTSTUFF_LOG_WARN(
                "KAURI_DEMO marker_skipped replica=%u height=%llu "
                "reason=event_clock_unavailable",
                get_id(),
                blk->get_height());
            return;
        }
        const auto monotonic_ns =
            static_cast<long long>(event_clock.tv_sec) * 1000000000LL +
            static_cast<long long>(event_clock.tv_nsec);
        HOTSTUFF_LOG_INFO(
            "KAURI_DEMO commit replica=%u height=%llu epoch=%u "
            "tree=%u root=%u "
            "hash=%s tx_count=%zu monotonic_ns=%lld",
            get_id(),
            static_cast<unsigned long long>(blk->get_height()),
            key.configuration.epoch_number,
            key.configuration.tree_id,
            tree->get_tree().get_tree_root(),
            blk->get_hash().to_hex().c_str(),
            blk->get_cmds().size(),
            static_cast<long long>(monotonic_ns));
    }

    void HotStuffBase::finish_adaptive_epoch_commit(
        const block_t &blk,
        const EpochCommitIngressResult &activation)
    {
        if (activation.transition != ActivationTransition::activated ||
            activation.error != EpochIngressError::none ||
            !activation.update.has_value() ||
            adaptive_epoch_runtime == nullptr)
            return;

        const auto drain = adaptive_epoch_runtime->adapter
                               .drain_activated_futures();
        const auto &configuration =
            activation.update->activation.configuration;
        const auto *definition =
            activation.update->activation.definition;
        emit_epoch_lifecycle_event(
            EpochLifecycleTransition::activated,
            configuration,
            definition == nullptr
                ? blk->get_height()
                : definition->activation_height());
        if (adaptive_demo_markers)
            HOTSTUFF_LOG_INFO(
                "KAURI_DEMO epoch_activated replica=%u epoch=%u "
                "tree=%u root=%u height=%llu epoch_digest=%s",
                get_id(),
                configuration.epoch_number,
                configuration.tree_id,
                adaptive_epoch_runtime->topology.current_tree()
                    .get_tree().get_tree_root(),
                blk->get_height(),
                configuration.epoch_digest.to_hex().c_str());
        HOTSTUFF_LOG_INFO(
            "[EPOCH] Activated epoch=%u tree=%u generation=%llu "
            "at committed height=%llu; future proposals=%zu/%zu",
            configuration.epoch_number,
            configuration.tree_id,
            activation.update->activation.generation,
            blk->get_height(),
            drain.processed,
            drain.remaining);
        if (drain.status != EpochFutureDrainStatus::complete)
            HOTSTUFF_LOG_WARN(
                "[EPOCH] Future proposal drain did not complete");
    }

    void HotStuffBase::advance_committed_retirement_floor(
        const block_t &blk,
        const std::vector<ProposalKey> &committed_keys)
    {
        if (proposal_admission == nullptr || exact_epochs == nullptr)
            return;

        const auto &active_configuration =
            proposal_admission->active_configuration();
        const auto key = committed_proposal_key(blk, committed_keys);
        if (!key.has_value() ||
            key->configuration != active_configuration)
            return;
        const auto *active_epoch = exact_epochs->find_epoch(
            active_configuration.epoch_number);
        if (active_epoch != nullptr &&
            active_epoch->epoch_digest() ==
                active_configuration.epoch_digest &&
            blk->height >= active_epoch->activation_height())
        {
            const auto first_live_epoch =
                active_configuration.epoch_number;
            if (proposal_contexts->has_open_context_before_epoch(
                    first_live_epoch))
                return;
            proposal_admission->advance_retirement_floor(
                first_live_epoch);
            pending_exact_contributions.purge_before_epoch(
                first_live_epoch);
            proposal_contexts->advance_retirement_floor(
                first_live_epoch);
        }
    }

    void HotStuffBase::do_consensus(const block_t &blk)
    {
        record_committed_epoch_change_history(blk);
        const auto keys =
            proposal_contexts->close_committed_block(blk->get_hash());
        record_adaptive_commit_marker(blk, keys);
        pending_exact_contributions.purge_block(blk->get_hash());
        for (const auto &key : keys)
        {
            purge_pending_exact_contributions(key);
            proposal_admission->retire_proposal(key);
        }
        const auto activation = epoch_live_binding == nullptr
            ? EpochCommitIngressResult{}
            : epoch_live_binding->on_predecessor_commit(
                  blk->get_height(),
                  get_epoch_digest(get_cur_epoch_nr()));
        finish_adaptive_epoch_commit(blk, activation);
        advance_committed_retirement_floor(blk, keys);
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
        if (epoch_protocol_mode == EpochProtocolMode::adaptive_v1 &&
            adaptive_epoch_runtime != nullptr)
            return adaptive_epoch_runtime->activation
                .active_effect().configuration.tree_id;
        return current_tree.get_tid();
    }

    /**
     * Get the current epoch number
     */
    uint32_t HotStuffBase::get_cur_epoch_nr()
    {
        if (epoch_protocol_mode == EpochProtocolMode::adaptive_v1 &&
            adaptive_epoch_runtime != nullptr)
            return adaptive_epoch_runtime->activation
                .active_effect().configuration.epoch_number;
        return pmaker->get_current_epoch();
    }

    uint256_t HotStuffBase::get_epoch_digest(uint32_t epoch_number)
    {
        if (exact_epochs == nullptr)
            throw std::logic_error(
                "cannot create proposal before exact epochs are initialized");
        const auto *epoch = exact_epochs->find_epoch(epoch_number);
        if (epoch == nullptr)
            throw std::out_of_range(
                "cannot create proposal for an unknown exact epoch");
        return epoch->epoch_digest();
    }

    ConfigurationId HotStuffBase::get_exact_tree_configuration(
        std::uint32_t epoch_number,
        std::uint32_t tree_id) const
    {
        return exact_configuration(epoch_number, tree_id);
    }

    ReplicaID HotStuffBase::get_exact_tree_root(
        std::uint32_t epoch_number,
        std::uint32_t tree_id) const
    {
        if (exact_epochs == nullptr)
            throw std::logic_error(
                "cannot resolve a leader before exact epochs exist");
        const auto *tree = exact_epochs->find_tree(
            epoch_number, tree_id);
        if (tree == nullptr || tree->members_breadth_first.empty())
            throw std::out_of_range(
                "cannot resolve an unknown exact tree leader");
        return tree->members_breadth_first.front();
    }

    LeaderTimeoutRotationDisposition
    HotStuffBase::rotate_tree_on_leader_timeout(
        const LeaderViewId &expired_view) noexcept
    {
        if (epoch_protocol_mode != EpochProtocolMode::adaptive_v1)
            return LeaderTimeoutRotationDisposition::legacy_fallback;
        if (adaptive_epoch_runtime == nullptr ||
            epoch_live_binding == nullptr)
            return LeaderTimeoutRotationDisposition::rejected;

        const auto active =
            adaptive_epoch_runtime->activation.active_effect();
        if (expired_view.configuration != active.configuration ||
            expired_view.view_generation != active.generation)
            return LeaderTimeoutRotationDisposition::rejected;
        const auto next_tree =
            adaptive_epoch_runtime->topology.next_tree_id();
        if (!next_tree.has_value())
            return LeaderTimeoutRotationDisposition::rejected;

        const auto rotation =
            epoch_live_binding->rotate_to_tree(*next_tree);
        if (rotation.error != EpochIngressError::none ||
            !rotation.update.has_value())
            return LeaderTimeoutRotationDisposition::rejected;
        HOTSTUFF_LOG_INFO(
            "[EPOCH] Rotated active epoch=%u to tree=%u after leader timeout",
            rotation.update->activation.configuration.epoch_number,
            rotation.update->activation.configuration.tree_id);
        return LeaderTimeoutRotationDisposition::rotated;
    }

    size_t HotStuffBase::get_total_system_trees()
    {
        if (epoch_protocol_mode == EpochProtocolMode::adaptive_v1 &&
            adaptive_epoch_runtime != nullptr)
            return adaptive_epoch_runtime->topology.tree_count();
        return system_trees.size();
    }

    ReplicaID HotStuffBase::get_system_tree_root(int tid)
    {
        if (epoch_protocol_mode == EpochProtocolMode::adaptive_v1 &&
            adaptive_epoch_runtime != nullptr)
        {
            const auto *tree = adaptive_epoch_runtime->topology.active_tree(
                static_cast<std::uint32_t>(tid));
            if (tree == nullptr)
                throw std::out_of_range("unknown adaptive runtime tree");
            return tree->get_tree().get_tree_root();
        }
        return system_trees.at(static_cast<size_t>(tid))
            .get_tree().get_tree_root();
    }

    ReplicaID HotStuffBase::get_current_system_tree_root()
    {
        if (epoch_protocol_mode == EpochProtocolMode::adaptive_v1 &&
            adaptive_epoch_runtime != nullptr)
            return adaptive_epoch_runtime->topology.current_tree()
                .get_tree().get_tree_root();
        return current_tree.get_tree_root();
    }

    TreeNetwork HotStuffBase::get_current_tree_network()
    {
        if (epoch_protocol_mode == EpochProtocolMode::adaptive_v1 &&
            adaptive_epoch_runtime != nullptr)
            return adaptive_epoch_runtime->topology.current_tree();
        return current_tree_network;
    }

    HotStuffBase::~HotStuffBase()
    {
        pmaker->shutdown();
        epoch_live_binding = nullptr;
        adaptive_epoch_runtime.reset();
        cancel_all_exact_forwarding_retries();
        exact_runtime_access->close_and_wait();
        proposal_contexts->shutdown();
        pending_exact_contributions.clear();
        blk_delivery_orchestrator.cancel(nullptr);
    }

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
            }
        }

        else
        {
            throw std::runtime_error("tree_config: Invalid tree generation algorithm!");
        }

        epochs.push_back(Epoch(0, default_trees));
        register_legacy_epoch(epochs.back());
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
        }

        epochs.push_back(Epoch(epochs[0].get_epoch_num() + 1, default_trees));
        register_legacy_epoch(epochs.back());
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

            fixed_membership.clear();
            fixed_membership.reserve(size);
            for (std::size_t replica = 0; replica < size; ++replica)
                fixed_membership.push_back(
                    static_cast<ReplicaID>(replica));
            exact_epochs =
                std::make_unique<EpochStore>(fixed_membership);

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

                    reputation.insert((std::make_pair(i, 0)));
                }
            }

            // Creates a system_tree object based on a file or a an algorithm
            tree_config(std::move(global_replicas));
            // Change from here justs reads a new epoch from file
            // read_epoch_from_file(std::move(global_replicas));
        }

        /* Update the current tree */
        auto current_epoch_nr = get_pace_maker()->get_current_epoch();
        offset = get_pace_maker()->get_current_tid();

        system_trees = epochs[current_epoch_nr].get_system_trees();
        current_tree_network = system_trees[offset];
        current_tree = current_tree_network.get_tree();
        if (startup)
        {
            AggregationTimeoutPolicy::Duration maximum_timeout =
                AggregationTimeoutPolicy::Duration::zero();
            for (const auto &entry : system_trees)
            {
                const auto candidate =
                    aggregation_timeout_policy.timeout_for(
                        0,
                        static_cast<std::uint32_t>(
                            entry.second.get_max_level()));
                maximum_timeout = std::max(
                    maximum_timeout, candidate);
            }
            if (maximum_timeout <=
                    AggregationTimeoutPolicy::Duration::zero() ||
                !pmaker->configure_leader_progress(maximum_timeout))
                throw std::logic_error(
                    "failed to configure leader progress safely");
        }
        if (!startup)
        {
            activate_proposal_configuration(exact_configuration(
                static_cast<uint32_t>(current_epoch_nr),
                current_tree.get_tid()));
        }

        /* Adjust fanout and pipeline stretch accordingly */
        config.async_blocks = current_tree.get_pipeline_stretch();
        config.fanout = current_tree.get_fanout();

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

        // if (warmup_counter < get_total_system_trees())
        // {
        //     // Do 1 block for each tree in schedule to warmup
        //     current_tree_network.set_target(lastCheckedHeight + 1);
        //     warmup_counter++;
        // }
        // else
        //     current_tree_network.set_target(lastCheckedHeight + config.tree_switch_period);

        if (!warmup_finished)
        {
            if (warmup_counter < get_total_system_trees())
            {
                current_tree_network.set_target(reconfig_count + 1);
                warmup_counter++;
            }
            else
            {
                // Warmup just ended
                reconfig_count = 0;
                warmup_finished = true;
                current_tree_network.set_target((reconfig_count + 1) * config.tree_switch_period);
            }
        }
        else
        {
            // Normal logic
            current_tree_network.set_target((reconfig_count + 1) * config.tree_switch_period);
        }

        /*See if the epochs are being initialized correctly */
        HOTSTUFF_LOG_PROTO("%s", std::string(epochs[current_epoch_nr]).c_str());
        /* ---------------------------------------------------------*/
        HOTSTUFF_LOG_PROTO("%s", std::string(current_tree_network).c_str());
        HOTSTUFF_LOG_PROTO("Next tree switch will happen at block %llu.", current_tree_network.get_target());

        LOG_PROTO("\n=========================== Finished Tree Switch =================================\n");

        /* Proposer opens client for himself */
        // open_client(get_system_tree_root(offset));
    }

    // TODO: this is not being used
    void HotStuffBase::change_epoch()
    {

        LOG_PROTO("\n=========================== Strating Epoch Change =================================\n");

        size_t offset = 0;

        // Updates epoch
        cur_epoch = on_hold_epoch;
        on_hold_epoch = Epoch(cur_epoch.get_epoch_num() + 1);

        // Updates system trees
        system_trees = cur_epoch.get_system_trees();
        current_tree_network = system_trees[offset];

        HOTSTUFF_LOG_PROTO("%s", std::string(cur_epoch).c_str());
        HOTSTUFF_LOG_PROTO("%s", std::string(current_tree_network).c_str());

        LOG_PROTO("\n=========================== Finished Epoch Switch =================================\n");
    }

#if 0
    void HotStuffBase::record_latency(size_t epoch_nr, size_t tid, const PeerId &peer, const uint256_t &blk_hash)
    {
        HOTSTUFF_LOG_INFO("[REPORT] Recording latency for block:%.10s from replica %d", blk_hash.to_hex().c_str(), peer_id_map.at(peer));

        BlockPeerKey k(blk_hash, peer);
        auto it = lat_start.find(k);
        if (it != lat_start.end())
        {
            struct timeval tv_end;
            gettimeofday(&tv_end, nullptr);

            auto tv_start = it->second;

            // Print start and end time for debugging
            HOTSTUFF_LOG_INFO("[REPORT] Start time: %ld.%06ld seconds", tv_start.tv_sec, tv_start.tv_usec);
            HOTSTUFF_LOG_INFO("[REPORT] End time: %ld.%06ld seconds", tv_end.tv_sec, tv_end.tv_usec);

            uint32_t elapsed_us = (tv_end.tv_sec - tv_start.tv_sec) * 1000000LL + (tv_end.tv_usec - tv_start.tv_usec);

            std::cout << "[REPORT] took "
                      << elapsed_us
                      << " us to respond."
                      << std::endl;

            LatMeasure lat(peer_id_map.at(peer), epoch_nr, tid, elapsed_us);

            peer_latencies.push_back(lat);

            lat_start.erase(it); // done
        }
        else
        {
            HOTSTUFF_LOG_WARN("[REPORT] Start time not found for block:%.10s from replica %d", blk_hash.to_hex().c_str(), peer_id_map.at(peer));
        }
    }

#endif

    void HotStuffBase::on_report_timer()
    {
        HOTSTUFF_LOG_INFO("[REPORT TIMER] Timer triggered for sending reports to reputation server.");

        if (!peer_latencies.empty())
        {
            HOTSTUFF_LOG_INFO("[REPORT TIMER] Preparing latency report...");
            LatencyReport report(get_id(), peer_latencies);

            if (reputation_server_conn != nullptr)
            {
                rn.send_msg(MsgLatencyReport(report), reputation_server_conn);
                HOTSTUFF_LOG_INFO("[REPORT TIMER] Sent %zu latency reports to reputation server.", peer_latencies.size());
            }
            else
            {
                HOTSTUFF_LOG_WARN("[REPORT TIMER] Reputation server connection is null. Cannot send reports.");
            }

            // Clear the peer_latencies map after sending the reports
            HOTSTUFF_LOG_INFO("[REPORT TIMER] Clearing latency records after sending.");
            peer_latencies.clear();
        }
        else
        {
            HOTSTUFF_LOG_INFO("[REPORT TIMER] No latencies to report. Skipping latency report.");
        }

        // Schedule the next report
        HOTSTUFF_LOG_INFO("[REPORT TIMER] Scheduling the next report in %.2f seconds.", report_period);
        ev_report_timer.add(report_period);
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

    void HotStuffBase::update_system_trees()
    {
        HOTSTUFF_LOG_INFO("[UPDATE SYSTEM TREES] Updating system trees and reputation for the new epoch");
        system_trees = epochs[pmaker->get_current_epoch()].get_system_trees();
        reputation = reputation_on_hold;

        HOTSTUFF_LOG_INFO("Reputation map updated:");
        for (const auto &pair : reputation)
        {
            HOTSTUFF_LOG_INFO("Replica %d -> Reputation: %d", pair.first, pair.second);
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
    ReconfigurationType HotStuffBase::isTreeSwitch(int bheight)
    {
        if (epoch_protocol_mode != EpochProtocolMode::legacy_static)
            return NO_SWITCH;

        if (bheight > lastCheckedHeight)
        {
            lastCheckedHeight = bheight;
        }

        if (lastCheckedHeight == 1000)
            return EPOCH_SWITCH;

        if (lastCheckedHeight == current_tree_network.get_target())
            return TREE_SWITCH;

        return NO_SWITCH;
    }

    void HotStuffBase::start(std::vector<std::tuple<NetAddr, pubkey_bt, uint256_t>> &&replicas, bool ec_loop)
    {

        /* ./examples/hotstuff-client */
        // snprintf(client_prog, sizeof(client_prog), "./examples/hotstuff-client --idx %d --iter -1 --max-async 50 > clientlog%d &", get_id(), get_id());

        /* Initial tree config */

        HotStuffBase::tree_scheduler(std::move(replicas), true);
        /* ((n - 1) + 1 - 1) / 3 */
        uint32_t nfaulty = peers.size() / 3;
        const auto byzantine =
            epoch_protocol_mode == EpochProtocolMode::adaptive_v2
                ? derive_byzantine_quorum(config.nreplicas)
                : std::optional<ByzantineQuorum>();
        if (epoch_protocol_mode == EpochProtocolMode::adaptive_v2 &&
            (!byzantine.has_value() ||
             byzantine->fault_threshold != nfaulty))
        {
            throw HotStuffError(
                "adaptive-v2 startup requires exact N = 3f + 1");
        }
        for (const PeerId &peer : peers)
        {
            pn.conn_peer(peer);
        }
        if (nfaulty == 0)
            LOG_WARN("too few replicas in the system to tolerate any failure");
        on_init(nfaulty);
        if (epoch_protocol_mode == EpochProtocolMode::adaptive_v2 &&
            config.nmajority != byzantine->quorum)
        {
            throw HotStuffError(
                "adaptive-v2 startup quorum does not equal 2f + 1");
        }
        pmaker->init(this);

        // TODO: Make this less ugly
        // Due to how the system is deployed, this is an alternative to correctly setup the PM's first proposer
        get_pace_maker()->update_tree_proposer();
        activate_initial_leader_view();
        if (epoch_protocol_mode == EpochProtocolMode::adaptive_v1)
            initialize_adaptive_epoch_runtime();
        get_pace_maker()->setup();

        if (ec_loop)
            ec.dispatch();

        // max_cmd_pending_size = blk_size * 100; // Hold up till 100 block worth of commands
        max_cmd_pending_size = blk_size; // Hold up till 1 block worth of commands
        final_buffer.reserve(blk_size);
        cmd_pending_buffer.reserve(max_cmd_pending_size);

        ev_report_timer = TimerEvent(ec, [this](TimerEvent &)
                                     { this->on_report_timer(); });
        ev_report_timer.add(report_period);

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
            HOTSTUFF_LOG_PROTO("[BEAT] Proposer ID: %d, Current Replica ID: %d", proposer, get_id());
            struct timeval timeStart, timeEnd;
            gettimeofday(&timeStart, NULL);

            auto parents = pmaker->get_parents();

            struct timeval current_time;
            gettimeofday(&current_time, NULL);
            block_t current = pmaker->get_current_proposal();

            HOTSTUFF_LOG_PROTO("[BEAT] Current Proposal Block Height: %llu", current->height);

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
                    const auto configuration = exact_configuration(
                        get_cur_epoch_nr(), get_tree_id());
                    piped_queue.push_back(piped_block->hash);
                    HOTSTUFF_LOG_PROTO("[PIPELINING] Pushed piped block into queue: %.10s", piped_block->hash.to_hex().c_str());
                    print_pipe_queues(true, false);
                    HOTSTUFF_LOG_PROTO("propose piped %s", std::string(*piped_block).c_str());

                    /* broadcast to other replicas */
                    gettimeofday(&last_block_time, NULL);
                    on_deliver_blk(piped_block);
                    Proposal prop = process_block(
                        piped_block, false, configuration);
                    on_verified_local_proposal_progress(prop.key());
                    piped_block->piped_delivered = true;
                    do_broadcast_proposal(prop);
                    /*if (id == get_pace_maker()->get_proposer()) {
                        gettimeofday(&timeEnd, NULL);
                        long usec = ((timeEnd.tv_sec - timeStart.tv_sec) * 1000000 + timeEnd.tv_usec - timeStart.tv_usec);
                        stats.insert(std::make_pair(piped_block->hash, usec));
                    }*/
                    piped_submitted = false;

                    // TREE ROTATION FOR PROPOSER CASE 2
                    if (epoch_protocol_mode ==
                        EpochProtocolMode::legacy_static)
                    {
                        auto switch_type =
                            isTreeSwitch(piped_block->get_height());

                        if (switch_type == TREE_SWITCH)
                        {
                            LOG_PROTO("[PROPOSER] Forcing a reconfiguration for changing current tree! (block height is now %llu)", piped_block->get_height());
                            inc_time(switch_type);
                        }
                        else if (switch_type == EPOCH_SWITCH)
                        {
                            LOG_PROTO("[PROPOSER] Forcing a reconfiguration for changing current epoch! (block height is now %llu)", piped_block->get_height());
                            inc_time(switch_type);
                        }
                        else if (piped_block->get_height() >
                                 get_total_system_trees())
                            inc_time(switch_type);
                    }
                    

                    /** 
                    if (isTreeSwitch(piped_block->get_height())) {
                        LOG_PROTO("[PROPOSER] Forcing a reconfiguration! (piped block height is now %llu)", piped_block->get_height());
                        inc_time(true);
                    }
                    else if (piped_block->get_height() >  get_total_system_trees()) {
                        inc_time(false);
                    }
                    */
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
