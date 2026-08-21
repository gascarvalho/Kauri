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
#include <optional>
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
        constexpr auto adaptive_v2_evidence_retry_delay =
            std::chrono::milliseconds(5);
        constexpr auto committed_epoch_definition_retry_base_delay =
            std::chrono::milliseconds(25);
        constexpr auto committed_epoch_definition_retry_maximum_delay =
            std::chrono::seconds(1);
        constexpr std::uint32_t
            adaptive_v2_reporting_maximum_delivery_attempts = 32;
        constexpr auto adaptive_v2_reporting_maximum_retry_delay =
            std::chrono::seconds(1);

        std::string experiment_audit_signers(
            const std::set<ReplicaID> &signers)
        {
            if (signers.empty())
                return "-";
            std::ostringstream output;
            bool first = true;
            for (const auto signer : signers)
            {
                if (!first)
                    output << ',';
                first = false;
                output << static_cast<unsigned>(signer);
            }
            return output.str();
        }

        uint256_t experiment_audit_certificate_fingerprint(
            const bytearray_t &serialized)
        {
            if (serialized.empty())
                return uint256_t{};
            return DataStream(serialized).get_hash();
        }

        bool experiment_audit_qc_unchanged(
            const QuorumCert *certificate,
            const ExperimentPostQcAuditRootSnapshot &snapshot) noexcept
        {
            if (certificate == nullptr ||
                certificate->get_proposal_key() != snapshot.proposal)
                return false;
            try
            {
                DataStream serialized;
                const_cast<QuorumCert *>(certificate)->serialize(
                    serialized);
                return static_cast<bytearray_t>(serialized) ==
                       snapshot.frozen_qc;
            }
            catch (...)
            {
                return false;
            }
        }

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

        bool is_adaptive_epoch_mode(EpochProtocolMode mode) noexcept
        {
            return mode == EpochProtocolMode::adaptive_v1 ||
                   mode == EpochProtocolMode::adaptive_v2 ||
                   mode == EpochProtocolMode::adaptive_v3;
        }

        std::uint64_t adaptive_monotonic_now_ns() noexcept
        {
            // Keep retry scheduling on the scheduler-compatible steady clock.
            const auto elapsed = std::chrono::duration_cast<
                std::chrono::nanoseconds>(
                std::chrono::steady_clock::now().time_since_epoch())
                                     .count();
            return elapsed > 0
                       ? static_cast<std::uint64_t>(elapsed)
                       : 0;
        }

        std::uint64_t adaptive_evidence_monotonic_now_ns() noexcept
        {
            // Evidence facts and fault markers share this frozen raw-clock
            // domain so campaign validators can compare their timestamps.
            struct timespec timestamp{};
            if (::clock_gettime(CLOCK_MONOTONIC_RAW, &timestamp) != 0 ||
                timestamp.tv_sec < 0 || timestamp.tv_nsec < 0 ||
                timestamp.tv_nsec >= 1'000'000'000)
                return 0;

            constexpr std::uint64_t nanoseconds_per_second =
                1'000'000'000;
            const auto seconds =
                static_cast<std::uint64_t>(timestamp.tv_sec);
            const auto nanoseconds =
                static_cast<std::uint64_t>(timestamp.tv_nsec);
            if (seconds >
                (std::numeric_limits<std::uint64_t>::max() -
                 nanoseconds) /
                    nanoseconds_per_second)
                return 0;
            return seconds * nanoseconds_per_second + nanoseconds;
        }

        std::optional<std::uint64_t>
        experiment_fault_marker_monotonic_now_ns() noexcept
        {
            const auto monotonic_ns =
                adaptive_evidence_monotonic_now_ns();
            if (monotonic_ns == 0)
                return std::nullopt;
            return monotonic_ns;
        }

        std::uint64_t scheduled_omission_monotonic_now_ns(
            const ExperimentByzantineAdapter *adapter) noexcept
        {
            return adapter != nullptr &&
                           adapter->scheduled_omission_enabled()
                       ? adaptive_evidence_monotonic_now_ns()
                       : 0;
        }

        ExperimentReplicaRole experiment_replica_role(
            const ProposalTreeSnapshot &tree) noexcept
        {
            if (!tree.parent.has_value())
                return ExperimentReplicaRole::root;
            return tree.direct_children.empty()
                       ? ExperimentReplicaRole::leaf
                       : ExperimentReplicaRole::internal;
        }

        std::uint64_t adaptive_deadline_duration_us(
            AggregationTimeoutPolicy::Duration duration) noexcept
        {
            constexpr auto nanoseconds_per_microsecond = 1000;
            const auto nanoseconds = duration.count();
            if (nanoseconds < nanoseconds_per_microsecond)
                return 0;
            return static_cast<std::uint64_t>(
                nanoseconds / nanoseconds_per_microsecond);
        }

        AggregationScheduler::Duration
        committed_epoch_definition_retry_delay(
            std::uint64_t completed_attempts) noexcept
        {
            const auto maximum = std::chrono::duration_cast<
                AggregationScheduler::Duration>(
                committed_epoch_definition_retry_maximum_delay);
            auto delay = std::chrono::duration_cast<
                AggregationScheduler::Duration>(
                committed_epoch_definition_retry_base_delay);
            for (std::uint64_t attempt = 1;
                 attempt < completed_attempts && delay < maximum;
                 ++attempt)
            {
                delay = std::min(maximum, delay * 2);
            }
            return delay;
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

        EpochDefinitionInput available_epoch_definition(
            const EpochDefinition &definition)
        {
            EpochDefinitionInput input;
            input.schema_version = definition.schema_version();
            input.epoch_number = definition.epoch_number();
            input.previous_epoch_digest =
                definition.previous_epoch_digest();
            input.membership_digest = definition.membership_digest();
            input.trees = definition.trees();
            input.activation_height = definition.activation_height();
            input.generation_seed = definition.generation_seed();
            input.policy_version = definition.policy_version();
            input.evidence_snapshot_id =
                definition.evidence_snapshot_id();
            input.evidence_cutoff = definition.evidence_cutoff();
            input.epoch_digest = definition.epoch_digest();
            return input;
        }

        void append_epoch_trees(
            EpochDefinitionInput &input,
            const Epoch &epoch)
        {
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

            if (!input.trees.empty())
                return;
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

            Duration monotonic_now() const noexcept override
            {
                return std::chrono::duration_cast<Duration>(
                    std::chrono::steady_clock::now().time_since_epoch());
            }

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

        class ProposalProcessingAttempt final
        {
        public:
            void install_completion(
                ProposalProcessingCompletion completion) noexcept
            {
                completion_ = std::move(completion);
            }

            bool begin_processing() noexcept
            {
                AggregationScheduler::Cancellation cancellation;
                {
                    std::lock_guard<std::mutex> lock(mutex_);
                    if (phase_ != Phase::waiting)
                        return false;
                    phase_ = Phase::processing;
                    cancellation = std::move(cancellation_);
                }
                cancel(std::move(cancellation));
                return true;
            }

            void install_timeout(
                AggregationScheduler::Cancellation cancellation) noexcept
            {
                bool cancel_now = false;
                {
                    std::lock_guard<std::mutex> lock(mutex_);
                    if (phase_ == Phase::waiting)
                        cancellation_ = std::move(cancellation);
                    else
                        cancel_now = true;
                }
                if (cancel_now)
                    cancel(std::move(cancellation));
            }

            void resolve(ProposalProcessingOutcome outcome) noexcept
            {
                ProposalProcessingCompletion completion;
                AggregationScheduler::Cancellation cancellation;
                {
                    std::lock_guard<std::mutex> lock(mutex_);
                    if (phase_ == Phase::resolved)
                        return;
                    phase_ = Phase::resolved;
                    completion = std::move(completion_);
                    cancellation = std::move(cancellation_);
                }
                cancel(std::move(cancellation));
                try
                {
                    if (completion)
                        completion(outcome);
                }
                catch (...)
                {}
            }

        private:
            enum class Phase
            {
                waiting,
                processing,
                resolved,
            };

            static void cancel(
                AggregationScheduler::Cancellation cancellation) noexcept
            {
                try
                {
                    if (cancellation)
                        cancellation();
                }
                catch (...)
                {}
            }

            std::mutex mutex_;
            Phase phase_{Phase::waiting};
            ProposalProcessingCompletion completion_;
            AggregationScheduler::Cancellation cancellation_;
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

    struct HotStuffBase::ExactVoteFallbackJob final
    {
        ExactVoteFallbackJob(
            const ProposalContextLease &lease,
            std::uint64_t exact_epoch_generation,
            ReplicaID exact_root,
            const Vote &exact_vote)
            : key(lease.key()),
              context_generation(lease.generation()),
              epoch_generation(exact_epoch_generation),
              root(exact_root),
              vote(std::make_shared<const Vote>(exact_vote))
        {}

        const ProposalKey key;
        const std::uint64_t context_generation;
        const std::uint64_t epoch_generation;
        const ReplicaID root;
        const std::shared_ptr<const Vote> vote;
        AggregationScheduler::Cancellation cancellation;
    };

    struct HotStuffBase::ExactProposalFallbackJob final
    {
        struct PendingPreQuorumRefresh final
        {
            ReplicaID target;
        };

        ExactProposalFallbackJob(
            const ProposalContextLease &lease,
            std::uint64_t exact_epoch_generation,
            const Proposal &exact_proposal,
            std::size_t exact_global_quorum,
            std::size_t exact_total_attempt_budget,
            std::size_t exact_stage_target_limit,
            AggregationTimeoutPolicy::Duration exact_stage_interval)
            : key(lease.key()),
              context_generation(lease.generation()),
              epoch_generation(exact_epoch_generation),
              proposal(std::make_shared<const Proposal>(exact_proposal)),
              global_quorum(exact_global_quorum),
              total_attempt_budget(exact_total_attempt_budget),
              stage_target_limit(exact_stage_target_limit),
              stage_interval(exact_stage_interval)
        {}

        const ProposalKey key;
        const std::uint64_t context_generation;
        const std::uint64_t epoch_generation;
        const std::shared_ptr<const Proposal> proposal;
        const std::size_t global_quorum;
        const std::size_t total_attempt_budget;
        const std::size_t stage_target_limit;
        const AggregationTimeoutPolicy::Duration stage_interval;
        std::size_t target_cursor{0};
        std::size_t total_send_attempts{0};
        std::vector<ReplicaID> attempted_targets;
        std::vector<ReplicaID> pre_quorum_retry_targets;
        std::size_t pre_quorum_retry_cursor{0};
        std::vector<PendingPreQuorumRefresh>
            pending_pre_quorum_refresh_batch;
        std::set<ReplicaID> confirmed_signers;
        std::vector<ReplicaID> tail_targets;
        std::size_t tail_target_cursor{0};
        std::uint32_t completed_stages{0};
        bool quorum_observed{false};
        bool pre_quorum_retry_armed{false};
        bool tail_armed{false};
        bool dispatching{false};
        AggregationScheduler::Cancellation cancellation;
    };

    class HotStuffBase::ExactContributionEffects final
        : public ExactVoteHandlerEffects
    {
    public:
        ExactContributionEffects(
            std::shared_ptr<ExactRuntimeAccess> access,
            PeerId source_peer,
            std::uint64_t received_ns)
            : access_(std::move(access)),
              source_peer_(std::move(source_peer)),
              received_ns_(received_ns)
        {}

        promise_t start_worker_verification(
            ExactContributionKind kind,
            const ExactContributionEnvelope &contribution) override
        {
            auto runtime = access_->acquire();
            if (!runtime.has_value())
                return resolved(false);
            auto &owner = runtime->owner();
            auto verification = owner.verify_exact_contribution(
                kind, contribution);
            if (kind != ExactContributionKind::direct_vote ||
                contribution.direct_vote == nullptr ||
                contribution.direct_vote->cert == nullptr ||
                owner.experiment_post_qc_audit == nullptr)
                return verification;
            const auto generation = owner.find_exact_runtime_generation(
                contribution.message_key.configuration);
            if (!generation.has_value())
                return verification;
            bool audit_started = false;
            try
            {
                audit_started = owner.experiment_post_qc_audit
                    ->begin_target_verification(
                        contribution.message_key,
                        *generation,
                        contribution.authenticated_sender,
                        ExperimentPostQcAuditTargetPhase::open,
                        received_ns_);
            }
            catch (...)
            {
                audit_started = false;
            }
            if (!audit_started)
                return verification;
            const auto vote = contribution.direct_vote;
            const auto sender = contribution.authenticated_sender;
            const auto key = contribution.message_key;
            const auto received_ns = received_ns_;
            const auto access = access_;
            return verification.then(
                [access,
                 vote,
                 sender,
                 key,
                 generation = *generation,
                 received_ns](bool verified)
                {
                    auto runtime = access->acquire();
                    if (!runtime.has_value())
                        return false;
                    auto &owner = runtime->owner();
                    try
                    {
                        const auto observation =
                            owner.experiment_post_qc_audit
                                ->complete_target_verification(
                                    key,
                                    generation,
                                    sender,
                                    owner.config,
                                    *vote->cert,
                                    verified);
                        if (observation.has_value())
                            owner.emit_experiment_post_qc_audit_target(
                                *observation);
                    }
                    catch (...)
                    {
                    }
                    return verified;
                });
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
                lease, kind, contribution, received_ns_);
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
        std::uint64_t received_ns_{0};
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
                if (next_token == 0 || plan.trees.empty() ||
                    plan.protocol_mode != owner.epoch_protocol_mode ||
                    plan.epoch_digest == uint256_t{})
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
                            plan.epoch_number ||
                        input.configuration.epoch_digest !=
                            plan.epoch_digest ||
                        input.configuration.tree_id !=
                            input.tree.tree_id ||
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
                if (owner.epoch_protocol_mode ==
                        EpochProtocolMode::adaptive_v2 ||
                    owner.epoch_protocol_mode ==
                        EpochProtocolMode::adaptive_v3)
                    owner.emit_active_configuration_event(
                        update.activation.configuration);
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
                    if (!validate_authenticated_vote(
                            owner.config,
                            peer.source_peer,
                            message.vote))
                        return;
                    const auto received_ns =
                        adaptive_evidence_monotonic_now_ns();
                    if (owner.observe_experiment_post_qc_audit_terminal_vote(
                            message.vote,
                            *peer.replica_id,
                            received_ns))
                        return;
                    owner.buffer_or_dispatch_exact_contribution(
                        ExactContributionKind::direct_vote,
                        make_exact_direct_envelope(
                            message.vote, *peer.replica_id),
                        peer.source_peer,
                        received_ns);
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
                        peer.source_peer,
                        adaptive_evidence_monotonic_now_ns());
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
                  owner.epoch_protocol_mode,
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

    const opcode_t MsgExperimentPostQcAuditRelay::opcode;
    MsgExperimentPostQcAuditRelay::MsgExperimentPostQcAuditRelay(
        const ExperimentPostQcAuditRelay &relay)
    {
        relay.serialize(serialized);
        wire_bytes = serialized.size();
    }

    bool MsgExperimentPostQcAuditRelay::postponed_parse(
        HotStuffCore *hsc) noexcept
    {
        try
        {
            DataStream input(serialized);
            ExperimentPostQcAuditRelay parsed;
            if (!parsed.parse(input, hsc))
                return false;
            relay = std::move(parsed);
            serialized = std::move(input);
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

    const EpochDefinition &HotStuffBase::register_initial_epoch(
        const Epoch &epoch)
    {
        if (epoch_protocol_mode != EpochProtocolMode::adaptive_v2 &&
            epoch_protocol_mode != EpochProtocolMode::adaptive_v3)
            return register_legacy_epoch(epoch);
        if (exact_epochs == nullptr)
            throw std::logic_error(
                "exact epoch store is not initialized");
        if (epoch.get_epoch_num() != 0)
            throw std::invalid_argument(
                "adaptive-v2 bootstrap must be epoch 0");

        EpochDefinitionInput topology;
        append_epoch_trees(topology, epoch);
        auto input = adaptive_v2_epoch_zero_input(
            fixed_membership, std::move(topology.trees));

        if (const auto *existing = exact_epochs->find_epoch(0))
        {
            if (existing->epoch_digest() != compute_epoch_digest(input))
                throw std::invalid_argument(
                    "initial epoch conflicts with staged exact definition");
            return *existing;
        }

        EpochValidationContext context;
        context.current_height = 0;
        context.minimum_activation_grace = 0;
        return exact_epochs->stage(input, context);
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

        append_epoch_trees(input, epoch);

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
        if (is_adaptive_epoch_mode(epoch_protocol_mode) &&
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
        if (!is_adaptive_epoch_mode(epoch_protocol_mode) ||
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
            forget_proposal_view_generation(metadata.key);
            return std::nullopt;
        }
        report_adaptive_v2_runtime_initialized(metadata.key);
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
                    static_cast<ProposalAdmissionEffects &>(*this),
                    (epoch_protocol_mode == EpochProtocolMode::adaptive_v2 ||
                     epoch_protocol_mode == EpochProtocolMode::adaptive_v3)
                        ? ProposalRelayPolicy::
                              adaptive_v2_deferred_until_arm_attempt
                        : ProposalRelayPolicy::eager_before_processing);
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

        auto runtime =
            std::make_unique<AdaptiveEpochRuntime>(*this, *active);
        std::unique_ptr<AdaptiveV2RotationCoordinator> coordinator;
        if (epoch_protocol_mode == EpochProtocolMode::adaptive_v2 ||
            epoch_protocol_mode == EpochProtocolMode::adaptive_v3)
        {
            if (!adaptive_v2_tree_switch_period.has_value())
                throw std::logic_error(
                    "adaptive tree switch period is not configured");
            coordinator =
                std::make_unique<AdaptiveV2RotationCoordinator>(
                    *adaptive_v2_tree_switch_period,
                    runtime->binding);
        }
        adaptive_epoch_runtime = std::move(runtime);
        epoch_live_binding = &adaptive_epoch_runtime->binding;
        adaptive_v2_rotation_coordinator = std::move(coordinator);
        enqueue_initial_adaptive_v2_readiness();
    }

    EpochChangeProposalChainResult
    HotStuffBase::pre_vote_epoch_change_gate(
        const Proposal &proposal) const noexcept
    {
        if (epoch_protocol_mode != EpochProtocolMode::adaptive_v2 &&
            epoch_protocol_mode != EpochProtocolMode::adaptive_v3)
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
                *exact_epochs,
                epoch_protocol_mode);
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

    bool HotStuffBase::retain_deferred_epoch_change(
        BufferedProposal proposal,
        const EpochDefinitionRequest &request) noexcept
    {
        if (epoch_protocol_mode != EpochProtocolMode::adaptive_v2 ||
            request.wire_schema_version != kEpochWireSchemaVersionV2 ||
            request.protocol_mode != EpochProtocolMode::adaptive_v2 ||
            request.successor_epoch_digest == uint256_t{} ||
            exact_epochs == nullptr ||
            exact_epochs->find_epoch_by_digest(
                request.successor_epoch_digest) != nullptr)
            return false;

        try
        {
            auto recovery = deferred_epoch_definition_recoveries.find(
                request.successor_epoch_digest);
            bool created = false;
            if (recovery == deferred_epoch_definition_recoveries.end())
            {
                if (deferred_epoch_definition_recoveries.size() >=
                    maximum_pending_epoch_definition_digests)
                    return false;
                const auto inserted =
                    deferred_epoch_definition_recoveries.emplace(
                        request.successor_epoch_digest,
                        DeferredEpochDefinitionRecovery{
                            request, true, {}});
                if (!inserted.second)
                    return false;
                recovery = inserted.first;
                created = true;
            }
            else if (recovery->second.request.wire_schema_version !=
                         request.wire_schema_version ||
                     recovery->second.request.protocol_mode !=
                         request.protocol_mode ||
                     recovery->second.request.successor_epoch_digest !=
                         request.successor_epoch_digest)
            {
                return false;
            }

            const auto key = proposal.metadata.key();
            if (recovery->second.proposals.count(key) != 0)
                return true;
            if (deferred_epoch_change_proposal_count >=
                maximum_deferred_epoch_change_proposals)
            {
                if (created)
                    deferred_epoch_definition_recoveries.erase(recovery);
                return false;
            }
            const auto inserted = recovery->second.proposals.emplace(
                key, std::move(proposal));
            if (!inserted.second)
                return true;
            ++deferred_epoch_change_proposal_count;
            if (created)
                send_epoch_definition_request(request);
            return true;
        }
        catch (...)
        {
            for (auto recovery =
                     deferred_epoch_definition_recoveries.begin();
                 recovery != deferred_epoch_definition_recoveries.end();)
            {
                if (recovery->second.proposals.empty())
                    recovery = deferred_epoch_definition_recoveries.erase(
                        recovery);
                else
                    ++recovery;
            }
            return false;
        }
    }

    void HotStuffBase::erase_deferred_epoch_change(
        const ProposalKey &key) noexcept
    {
        for (auto recovery =
                 deferred_epoch_definition_recoveries.begin();
             recovery != deferred_epoch_definition_recoveries.end();)
        {
            const auto proposal = recovery->second.proposals.find(key);
            if (proposal == recovery->second.proposals.end())
            {
                ++recovery;
                continue;
            }
            recovery->second.proposals.erase(proposal);
            if (deferred_epoch_change_proposal_count != 0)
                --deferred_epoch_change_proposal_count;
            if (recovery->second.proposals.empty())
                deferred_epoch_definition_recoveries.erase(recovery);
            return;
        }
    }

    void HotStuffBase::send_epoch_definition_request(
        const EpochDefinitionRequest &request) noexcept
    {
        if (epoch_protocol_mode != EpochProtocolMode::adaptive_v2 &&
            epoch_protocol_mode != EpochProtocolMode::adaptive_v3)
            return;
        try
        {
            std::vector<std::pair<ReplicaID, PeerId>> targets;
            targets.reserve(peer_id_map.size());
            for (const auto &authenticated : peer_id_map)
            {
                if (authenticated.first.is_null() ||
                    authenticated.second >= fixed_membership.size())
                    continue;
                const auto replica = static_cast<ReplicaID>(
                    authenticated.second);
                if (fixed_membership[authenticated.second] != replica)
                    continue;
                targets.emplace_back(replica, authenticated.first);
            }
            std::sort(
                targets.begin(), targets.end(),
                [](const auto &left, const auto &right) {
                    return left.first < right.first;
                });
            ReplicaID previous = 0;
            bool have_previous = false;
            for (const auto &target : targets)
            {
                if (have_previous && target.first == previous)
                    continue;
                previous = target.first;
                have_previous = true;
                try
                {
                    const MsgEpochDefinitionRequest message(
                        request, epoch_wire_limits);
                    pn.send_msg(message, target.second);
                }
                catch (...)
                {
                    HOTSTUFF_LOG_WARN(
                        "[EPOCH] Failed to request definition from replica %u",
                        target.first);
                }
            }
        }
        catch (...)
        {
            HOTSTUFF_LOG_WARN(
                "[EPOCH] Failed to fan out definition request");
        }
    }

    bool HotStuffBase::queue_deferred_epoch_change_retries(
        const uint256_t &successor_epoch_digest) noexcept
    {
        try
        {
            const auto access = exact_runtime_access;
            tcall.async_call(
                [access, successor_epoch_digest](
                    salticidae::ThreadCall::Handle &)
                {
                    const auto runtime = access->acquire();
                    if (!runtime.has_value())
                        return;
                    runtime->owner().retry_deferred_epoch_changes(
                        successor_epoch_digest);
                });
            return true;
        }
        catch (...)
        {
            return false;
        }
    }

    void HotStuffBase::retry_deferred_epoch_changes(
        const uint256_t &successor_epoch_digest) noexcept
    {
        const auto recovery = deferred_epoch_definition_recoveries.find(
            successor_epoch_digest);
        if (recovery == deferred_epoch_definition_recoveries.end() ||
            recovery->second.request_live || exact_epochs == nullptr ||
            exact_epochs->find_epoch_by_digest(
                successor_epoch_digest) == nullptr)
            return;

        try
        {
            std::vector<BufferedProposal> proposals;
            proposals.reserve(recovery->second.proposals.size());
            for (const auto &proposal : recovery->second.proposals)
                proposals.push_back(proposal.second);
            for (const auto &proposal : proposals)
                process_active(proposal);
        }
        catch (...)
        {
            const auto retryable =
                deferred_epoch_definition_recoveries.find(
                    successor_epoch_digest);
            if (retryable !=
                deferred_epoch_definition_recoveries.end())
            {
                retryable->second.request_live = true;
                send_epoch_definition_request(
                    retryable->second.request);
            }
            HOTSTUFF_LOG_WARN(
                "[EPOCH] Failed to retry deferred epoch-change proposals");
        }
    }

    bool HotStuffBase::retain_committed_epoch_definition_recovery(
        const block_t &block,
        const AuthorizedEpochChange &command,
        const ActivationRecord &record) noexcept
    {
        const auto recovery_wire_schema =
            epoch_wire_schema_for_mode(epoch_protocol_mode);
        if (!(epoch_protocol_mode == EpochProtocolMode::adaptive_v2 ||
              epoch_protocol_mode == EpochProtocolMode::adaptive_v3) ||
            !recovery_wire_schema ||
            command.protocol_mode != epoch_protocol_mode || block == nullptr ||
            block->get_hash() == uint256_t{} ||
            command.payload.successor_epoch_digest == uint256_t{} ||
            record.predecessor_epoch_digest !=
                command.payload.predecessor_epoch_digest ||
            record.successor_epoch_number !=
                command.payload.successor_epoch_number ||
            record.successor_epoch_digest !=
                command.payload.successor_epoch_digest ||
            record.payload_digest !=
                epoch_change_payload_digest(command.payload) ||
            record.command_commit_height != block->get_height() ||
            record.activation_delay_blocks !=
                command.payload.activation_delay_blocks ||
            record.activation_height <
                record.command_commit_height)
            return false;

        try
        {
            if (committed_epoch_definition_recovery)
            {
                const auto &existing =
                    *committed_epoch_definition_recovery;
                const bool same_recovery =
                    existing.command_block_hash == block->get_hash() &&
                    existing.payload_digest == record.payload_digest &&
                    existing.command_commit_height ==
                        record.command_commit_height &&
                    existing.activation_height ==
                        record.activation_height &&
                    encode_authorized_epoch_change(existing.command) ==
                        encode_authorized_epoch_change(command);
                if (!same_recovery)
                    return false;
                if (existing.definition_recovered ||
                    existing.retry_cancellation)
                    return true;
                return schedule_committed_epoch_definition_retry();
            }

            const EpochDefinitionRequest request{
                *recovery_wire_schema,
                epoch_protocol_mode,
                command.payload.successor_epoch_digest};
            auto retry_generation =
                next_committed_epoch_definition_retry_generation++;
            if (retry_generation == 0)
            {
                retry_generation =
                    next_committed_epoch_definition_retry_generation++;
            }
            committed_epoch_definition_recovery.emplace(
                CommittedEpochDefinitionRecovery{
                    request,
                    command,
                    block->get_hash(),
                    record.payload_digest,
                    record.command_commit_height,
                    record.activation_height,
                    nullptr,
                    false,
                    retry_generation,
                    1,
                    {}});
            send_epoch_definition_request(request);
            if (!schedule_committed_epoch_definition_retry())
            {
                reset_committed_epoch_definition_recovery();
                return false;
            }
            return true;
        }
        catch (...)
        {
            reset_committed_epoch_definition_recovery();
            return false;
        }
    }

    bool HotStuffBase::schedule_committed_epoch_definition_retry() noexcept
    {
        if (!committed_epoch_definition_recovery ||
            committed_epoch_definition_recovery->definition_recovered ||
            aggregation_scheduler == nullptr)
            return false;
        if (committed_epoch_definition_recovery->retry_cancellation)
            return true;

        try
        {
            const auto &recovery =
                *committed_epoch_definition_recovery;
            const auto retry_generation =
                recovery.retry_generation;
            const auto command_block_hash =
                recovery.command_block_hash;
            const auto successor_epoch_digest =
                recovery.request.successor_epoch_digest;
            auto cancellation = aggregation_scheduler->schedule_after(
                committed_epoch_definition_retry_delay(
                    recovery.retry_attempts),
                [access = exact_runtime_access,
                 retry_generation,
                 command_block_hash,
                 successor_epoch_digest]() {
                    auto runtime = access->acquire();
                    if (!runtime.has_value())
                        return;
                    runtime->owner()
                        .dispatch_committed_epoch_definition_retry(
                            retry_generation,
                            command_block_hash,
                            successor_epoch_digest);
                });
            if (!cancellation)
                return false;
            committed_epoch_definition_recovery->retry_cancellation =
                std::move(cancellation);
            return true;
        }
        catch (...)
        {
            return false;
        }
    }

    void HotStuffBase::dispatch_committed_epoch_definition_retry(
        std::uint64_t retry_generation,
        const uint256_t &command_block_hash,
        const uint256_t &successor_epoch_digest) noexcept
    {
        if (!committed_epoch_definition_recovery)
            return;
        auto &recovery = *committed_epoch_definition_recovery;
        if (recovery.definition_recovered ||
            recovery.retry_generation != retry_generation ||
            recovery.command_block_hash != command_block_hash ||
            recovery.request.successor_epoch_digest !=
                successor_epoch_digest)
            return;

        recovery.retry_cancellation = {};
        send_epoch_definition_request(recovery.request);
        if (recovery.retry_attempts !=
            std::numeric_limits<std::uint64_t>::max())
        {
            ++recovery.retry_attempts;
        }
        if (schedule_committed_epoch_definition_retry())
            return;

        HOTSTUFF_LOG_WARN(
            "[EPOCH] Failed to schedule committed definition retry");
        pending_committed_epoch_change.reset();
        reset_committed_epoch_definition_recovery();
        committed_epoch_change_history.reset();
        if (epoch_protocol_mode == EpochProtocolMode::adaptive_v2)
        {
            mark_adaptive_v2_convergence_evidence_unhealthy(
                "committed_definition_retry_schedule_failed");
            if (adaptive_epoch_runtime != nullptr)
                adaptive_epoch_runtime->adapter.fail_committed_v2(
                    ActivationBlockReason::invalid_activation_record);
        }
        else if (epoch_protocol_mode == EpochProtocolMode::adaptive_v3)
            emit_adaptive_v3_command_terminal(
                AdaptiveV3CommandTerminalReason::
                    committed_definition_retry_schedule_failed);
    }

    void HotStuffBase::cancel_committed_epoch_definition_retry() noexcept
    {
        if (!committed_epoch_definition_recovery)
            return;
        auto cancellation = std::move(
            committed_epoch_definition_recovery->retry_cancellation);
        committed_epoch_definition_recovery->retry_cancellation = {};
        if (!cancellation)
            return;
        try
        {
            cancellation();
        }
        catch (...)
        {
            // Cancellation is best effort during recovery teardown.
        }
    }

    void HotStuffBase::reset_committed_epoch_definition_recovery() noexcept
    {
        cancel_committed_epoch_definition_retry();
        committed_epoch_definition_recovery.reset();
    }

    bool HotStuffBase::recover_committed_epoch_definition(
        const EpochDefinition &definition) noexcept
    {
        if (!committed_epoch_definition_recovery ||
            adaptive_epoch_runtime == nullptr ||
            epoch_live_binding == nullptr ||
            exact_epochs == nullptr)
            return false;

        try
        {
            auto &recovery = *committed_epoch_definition_recovery;
            const auto &command = recovery.command;
            const auto &payload = command.payload;
            if (definition.schema_version() !=
                    kEpochDefinitionSchemaVersionV2 ||
                definition.activation_height() != 0 ||
                definition.epoch_number() !=
                    payload.successor_epoch_number ||
                definition.previous_epoch_digest() !=
                    payload.predecessor_epoch_digest ||
                definition.epoch_digest() !=
                    payload.successor_epoch_digest ||
                exact_epochs->find_epoch_by_digest(
                    payload.successor_epoch_digest) != &definition ||
                definition.canonical_serialization().empty() ||
                DataStream(definition.canonical_serialization()).get_hash() !=
                    payload.successor_epoch_digest ||
                recovery.payload_digest !=
                    epoch_change_payload_digest(payload) ||
                recovery.activation_height <
                    recovery.command_commit_height ||
                recovery.activation_height -
                        recovery.command_commit_height !=
                    payload.activation_delay_blocks)
                return false;

            if (!recovery.definition_recovered)
            {
                const auto prepared =
                    adaptive_epoch_runtime->adapter.prepare_committed_v2(
                        definition);
                if (prepared != EpochIngressError::none)
                    return false;

                const auto replayed =
                    adaptive_epoch_runtime->activation.record_committed_v2(
                        command, recovery.command_commit_height);
                if ((replayed.disposition !=
                         ActivationRecordDisposition::recorded &&
                     replayed.disposition !=
                         ActivationRecordDisposition::duplicate) ||
                    !replayed.record ||
                    replayed.record->payload_digest !=
                        recovery.payload_digest ||
                    replayed.record->command_commit_height !=
                        recovery.command_commit_height ||
                    replayed.record->activation_height !=
                        recovery.activation_height ||
                    replayed.record->predecessor_epoch_digest !=
                        payload.predecessor_epoch_digest ||
                    replayed.record->successor_epoch_number !=
                        payload.successor_epoch_number ||
                    replayed.record->successor_epoch_digest !=
                        payload.successor_epoch_digest ||
                    adaptive_epoch_runtime->activation.blocked_reason() !=
                        ActivationBlockReason::none ||
                    !adaptive_epoch_runtime->activation
                         .admits_new_proposals())
                    return false;
                recovery.definition_recovered = true;
                cancel_committed_epoch_definition_retry();
            }

            if (recovery.activation_block == nullptr)
                return true;
            if (recovery.activation_block->get_height() !=
                    recovery.activation_height)
                return false;

            const auto activation =
                epoch_live_binding->on_v2_post_block_commit(
                    recovery.activation_height,
                    payload.predecessor_epoch_digest);
            if (activation.error != EpochIngressError::none ||
                (activation.transition != ActivationTransition::activated &&
                 activation.transition !=
                     ActivationTransition::already_active))
                return false;

            const auto activation_block = recovery.activation_block;
            finish_adaptive_epoch_commit(activation_block, activation);
            reset_committed_epoch_definition_recovery();
            return true;
        }
        catch (...)
        {
            return false;
        }
    }

    void HotStuffBase::initialize_committed_epoch_change_history() noexcept
    {
        committed_epoch_change_history.reset();
        pending_committed_epoch_change.reset();
        reset_committed_epoch_definition_recovery();
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
            pending_committed_epoch_change.reset();
        }
    }

    void HotStuffBase::record_committed_epoch_change_history(
        const block_t &block) noexcept
    {
        if (epoch_protocol_mode != EpochProtocolMode::adaptive_v2 &&
            epoch_protocol_mode != EpochProtocolMode::adaptive_v3)
            return;
        if (!committed_epoch_change_history ||
            pending_committed_epoch_change)
        {
            pending_committed_epoch_change.reset();
            committed_epoch_change_history.reset();
            return;
        }

        const auto fail_closed = [this]() noexcept {
            pending_committed_epoch_change.reset();
            committed_epoch_change_history.reset();
        };
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
                fail_closed();
                return;
            }

            const auto &parent_hashes = block->get_parent_hashes();
            const auto &parents = block->get_parents();
            if (parent_hashes.empty() || parents.empty() ||
                parents.front() != previous.head ||
                parent_hashes.front() != previous.head->get_hash())
            {
                fail_closed();
                return;
            }

            auto command = previous.snapshot.command;
            const auto extracted =
                epoch_protocol_mode == EpochProtocolMode::adaptive_v3
                    ? extract_epoch_change_block_extra_v3(
                          block->get_extra(),
                          epoch_change_maximum_block_extra_bytes)
                    : extract_epoch_change_block_extra(
                          block->get_extra(),
                          epoch_change_maximum_block_extra_bytes);
            if (extracted.disposition ==
                    EpochChangeExtraDisposition::present &&
                extracted.command && extracted.payload_digest &&
                extracted.envelope_digest)
            {
                if (epoch_change_verifier == nullptr ||
                    exact_epochs == nullptr || proposal_contexts == nullptr)
                {
                    fail_closed();
                    return;
                }
                const auto active_configuration =
                    proposal_contexts->active_configuration();
                if (!active_configuration.has_value())
                {
                    fail_closed();
                    return;
                }
                const auto *const active_epoch = exact_epochs->find_epoch(
                    active_configuration->epoch_number);
                if (active_epoch == nullptr ||
                    active_epoch->epoch_digest() !=
                        active_configuration->epoch_digest)
                {
                    fail_closed();
                    return;
                }

                const auto history = EpochChangeHistoryView{
                    std::nullopt,
                    previous.snapshot.command &&
                            previous.snapshot.command->predecessor_epoch_digest ==
                                active_epoch->epoch_digest()
                        ? std::optional<uint256_t>(
                              previous.snapshot.command->payload_digest)
                        : std::nullopt};
                const auto validation = epoch_change_verifier->validate(
                    *extracted.command,
                    *active_epoch,
                    *exact_epochs,
                    history);
                const auto *const successor =
                    exact_epochs->find_epoch_by_digest(
                        extracted.command->payload.successor_epoch_digest);
                const bool available_definition =
                    (validation.disposition ==
                         EpochChangeDisposition::accepted ||
                     validation.disposition ==
                         EpochChangeDisposition::duplicate) &&
                    validation.successor_definition != nullptr &&
                    successor != nullptr &&
                    validation.successor_definition == successor &&
                    validation.successor_definition->epoch_digest() ==
                        extracted.command->payload.successor_epoch_digest;
                const bool recoverable_missing_definition =
                    validation.disposition ==
                        EpochChangeDisposition::defer_missing_definition &&
                    validation.successor_definition == nullptr &&
                    successor == nullptr &&
                    validation.recovery_request.has_value() &&
                    validation.recovery_request->wire_schema_version ==
                        (epoch_protocol_mode == EpochProtocolMode::adaptive_v3
                             ? kEpochWireSchemaVersionV3
                             : kEpochWireSchemaVersionV2) &&
                    validation.recovery_request->protocol_mode ==
                        epoch_protocol_mode &&
                    validation.recovery_request->successor_epoch_digest ==
                        extracted.command->payload.successor_epoch_digest;
                if ((!available_definition &&
                     !recoverable_missing_definition) ||
                    validation.payload_digest != *extracted.payload_digest ||
                    validation.envelope_digest !=
                        *extracted.envelope_digest)
                {
                    fail_closed();
                    return;
                }
                command = EpochChangeCommittedHistoryEntry{
                    extracted.command->payload.predecessor_epoch_digest,
                    *extracted.payload_digest};
            }
            else if (extracted.disposition !=
                         EpochChangeExtraDisposition::absent)
            {
                fail_closed();
                return;
            }

            committed_epoch_change_history =
                CommittedEpochChangeHistoryState{
                    block,
                    EpochChangeCommittedHistorySnapshot{
                        block->get_hash(),
                        block->get_height(),
                        std::move(command)}};
            if (extracted.command)
                pending_committed_epoch_change.emplace(
                    PendingCommittedEpochChange{
                        block->get_hash(),
                        *extracted.command,
                        *extracted.payload_digest});
        }
        catch (...)
        {
            fail_closed();
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

    void HotStuffBase::retire_deferred_epoch_changes_before_epoch(
        std::uint32_t first_live_epoch) noexcept
    {
        for (auto recovery =
                 deferred_epoch_definition_recoveries.begin();
             recovery != deferred_epoch_definition_recoveries.end();)
        {
            auto &proposals = recovery->second.proposals;
            for (auto proposal = proposals.begin();
                 proposal != proposals.end();)
            {
                if (proposal->first.configuration.epoch_number >=
                    first_live_epoch)
                {
                    ++proposal;
                    continue;
                }
                const auto key = proposal->first;
                if (proposal_admission != nullptr)
                {
                    try
                    {
                        proposal_admission->retire_proposal(key);
                    }
                    catch (...)
                    {}
                }
                try
                {
                    purge_pending_exact_contributions(key);
                }
                catch (...)
                {}
                forget_proposal_view_generation(key);
                proposal = proposals.erase(proposal);
                if (deferred_epoch_change_proposal_count != 0)
                    --deferred_epoch_change_proposal_count;
            }
            if (proposals.empty())
                recovery =
                    deferred_epoch_definition_recoveries.erase(recovery);
            else
                ++recovery;
        }
    }

    void HotStuffBase::process_active(const BufferedProposal &proposal)
    {
        static_cast<void>(process_active(proposal, {}));
    }

    void HotStuffBase::cleanup_retryable_proposal_attempt(
        const ProposalKey &key) noexcept
    {
        try
        {
            erase_deferred_epoch_change(key);
        }
        catch (...)
        {}
        try
        {
            proposal_contexts->close(
                key, ProposalContextEvent::proposal_aborted);
        }
        catch (...)
        {}
        forget_proposal_view_generation(key);
        try
        {
            purge_pending_exact_contributions(key);
        }
        catch (...)
        {}
    }

    bool HotStuffBase::process_active(
        const BufferedProposal &proposal,
        ProposalProcessingCompletion completion)
    {
        const bool retryable_claim = static_cast<bool>(completion);
        std::shared_ptr<ProposalProcessingAttempt> attempt_state;
        if (completion)
        {
            try
            {
                attempt_state =
                    std::make_shared<ProposalProcessingAttempt>();
                attempt_state->install_completion(std::move(completion));
            }
            catch (...)
            {
                completion(
                    ProposalProcessingOutcome::retryable_pre_relay_failure);
                return true;
            }
        }
        const auto resolve_processing =
            [attempt_state](ProposalProcessingOutcome outcome) noexcept {
                if (attempt_state != nullptr)
                    attempt_state->resolve(outcome);
            };
        const auto proposal_key = proposal.metadata.key();
        const bool certified_adaptive_mode =
            epoch_protocol_mode == EpochProtocolMode::adaptive_v2 ||
            epoch_protocol_mode == EpochProtocolMode::adaptive_v3;
        static_cast<void>(observe_proposal_view_generation(
            proposal_key, proposal.view_generation));
        if (proposal.source_peer.is_null() ||
            (certified_adaptive_mode &&
             !proposal.authenticated_proposal_source_replica.has_value()))
        {
            erase_deferred_epoch_change(proposal_key);
            proposal_contexts->close(
                proposal_key,
                ProposalContextEvent::proposal_aborted);
            forget_proposal_view_generation(proposal_key);
            if (proposal_admission != nullptr)
                proposal_admission->retire_proposal(proposal_key);
            purge_pending_exact_contributions(proposal_key);
            HOTSTUFF_LOG_WARN(
                "[PROP HANDLER] Active proposal has no authenticated source");
            resolve_processing(
                ProposalProcessingOutcome::terminal_pre_relay);
            return true;
        }

        if (proposal.authenticated_proposal_source_replica.has_value())
            authenticated_proposal_ingress.insert_or_assign(
                proposal_key,
                AuthenticatedProposalIngress{
                    proposal.view_generation,
                    *proposal.authenticated_proposal_source_replica});

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
            if (attempt_state != nullptr)
            {
                const std::weak_ptr<ProposalProcessingAttempt> weak_attempt(
                    attempt_state);
                const auto expire_attempt =
                    [weak_attempt, access, proposal_key]() noexcept {
                        const auto attempt = weak_attempt.lock();
                        if (attempt == nullptr ||
                            !attempt->begin_processing())
                            return;
                        const auto runtime = access->acquire();
                        if (runtime.has_value())
                            runtime->owner()
                                .cleanup_retryable_proposal_attempt(
                                    proposal_key);
                        attempt->resolve(
                            ProposalProcessingOutcome::
                                retryable_pre_relay_failure);
                    };
                try
                {
                    if (aggregation_scheduler == nullptr)
                        expire_attempt();
                    else
                    {
                        auto cancellation =
                            aggregation_scheduler->schedule_after(
                                adaptive_timeout_from_seconds(
                                    ent_waiting_timeout),
                                expire_attempt);
                        if (!cancellation)
                            expire_attempt();
                        else
                            attempt_state->install_timeout(
                                std::move(cancellation));
                    }
                }
                catch (...)
                {
                    expire_attempt();
                }
            }
            const auto delivery_key = metadata->key;
            const auto delivery_recipient = metadata->tree.local_replica;
            const auto delivery_root = metadata->tree.root;
            delivery.then(
                [access,
                 metadata = std::move(*metadata),
                 parsed = std::move(parsed),
                 deferred = proposal,
                 retryable_claim,
                 attempt_state,
                 resolve_processing](
                    const block_t &delivered) mutable
                {
                    if (attempt_state != nullptr &&
                        !attempt_state->begin_processing())
                        return;
                    const auto log_callback = [&metadata](
                        const char *stage, const char *outcome) {
                        HOTSTUFF_LOG_INFO(
                            "KAURI_PROPOSAL_PROCESS stage=%s outcome=%s "
                            "recipient=%u root=%u epoch=%u tree=%u "
                            "block=%s",
                            stage,
                            outcome,
                            static_cast<unsigned>(
                                metadata.tree.local_replica),
                            static_cast<unsigned>(metadata.tree.root),
                            metadata.key.configuration.epoch_number,
                            metadata.key.configuration.tree_id,
                            metadata.key.block_hash.to_hex().c_str());
                    };
                    log_callback("callback_begin", "started");
                    auto runtime = access->acquire();
                    if (!runtime.has_value())
                    {
                        log_callback(
                            "callback_abort", "runtime_unavailable");
                        resolve_processing(
                            ProposalProcessingOutcome::terminal_pre_relay);
                        return;
                    }
                    auto &owner = runtime->owner();
                    const auto abort =
                        [&owner,
                         &metadata,
                         &log_callback,
                         &resolve_processing](
                            const char *reason,
                            ProposalProcessingOutcome outcome,
                            bool retire_admission) {
                        owner.erase_deferred_epoch_change(metadata.key);
                        owner.proposal_contexts->close(
                            metadata.key,
                            ProposalContextEvent::proposal_aborted);
                        owner.forget_proposal_view_generation(metadata.key);
                        if (retire_admission &&
                            owner.proposal_admission != nullptr)
                            owner.proposal_admission->retire_proposal(
                                metadata.key);
                        owner.purge_pending_exact_contributions(metadata.key);
                        log_callback("callback_abort", reason);
                        resolve_processing(outcome);
                    };

                    if (delivered == nullptr || !delivered->delivered)
                    {
                        abort(
                            "delivery_unavailable",
                            retryable_claim
                                ? ProposalProcessingOutcome::
                                      retryable_pre_relay_failure
                                : ProposalProcessingOutcome::
                                      terminal_pre_relay,
                            !retryable_claim);
                        return;
                    }
                    if (delivered->get_hash() != metadata.key.block_hash)
                    {
                        abort(
                            "delivery_hash_mismatch",
                            ProposalProcessingOutcome::terminal_pre_relay,
                            true);
                        return;
                    }
                    if (owner.proposal_admission == nullptr ||
                        !owner.proposal_admission->contains_admitted(
                            metadata.key))
                    {
                        abort(
                            "proposal_retired",
                            ProposalProcessingOutcome::terminal_pre_relay,
                            true);
                        return;
                    }

                    bool relay_exposure_attempted = false;
                    RetainedCommitEventIdentityRollback
                        retained_identity_rollback{};
                    try
                    {
                        const auto gate =
                            owner.pre_vote_epoch_change_gate(parsed);
                        switch (gate.disposition)
                        {
                        case EpochChangeProposalDisposition::accepted:
                        case EpochChangeProposalDisposition::duplicate:
                            owner.erase_deferred_epoch_change(
                                metadata.key);
                            break;
                        case EpochChangeProposalDisposition::defer:
                            if (!gate.recovery_request ||
                                !owner.retain_deferred_epoch_change(
                                    std::move(deferred),
                                    *gate.recovery_request))
                                abort(
                                    "defer_retention_failed",
                                    ProposalProcessingOutcome::
                                        terminal_pre_relay,
                                    true);
                            else
                            {
                                log_callback("callback_end", "deferred");
                                resolve_processing(
                                    ProposalProcessingOutcome::
                                        completed_ownership_transferred);
                            }
                            return;
                        case EpochChangeProposalDisposition::rejected:
                            abort(
                                "epoch_change_rejected",
                                ProposalProcessingOutcome::
                                    terminal_pre_relay,
                                true);
                            return;
                        }

                        auto lease = owner.admit_exact_context(
                            metadata,
                            ProposalContextOrigin::remote);
                        if (!lease.has_value())
                        {
                            abort(
                                "context_admission_failed",
                                ProposalProcessingOutcome::
                                    terminal_pre_relay,
                                true);
                            return;
                        }
                        const bool certified_adaptive_mode =
                            owner.epoch_protocol_mode ==
                                EpochProtocolMode::adaptive_v2 ||
                            owner.epoch_protocol_mode ==
                                EpochProtocolMode::adaptive_v3;
                        if (certified_adaptive_mode)
                        {
                            owner.attempt_proposal_evidence_before_exposure(
                                metadata.key,
                                "response_deadline_arm_failed_before_"
                                "proposal_exposure");
                        }
                        if (certified_adaptive_mode)
                        {
                            relay_exposure_attempted = true;
                            owner.relay_once(deferred);
                        }

                        if (certified_adaptive_mode)
                            static_cast<void>(
                                owner.
                                    retain_authenticated_proposal_commit_event_identities(
                                        parsed,
                                        deferred.view_generation,
                                        &retained_identity_rollback));
                        const bool proposal_accepted =
                            owner.on_receive_proposal(parsed);
                        if (proposal_accepted)
                        {
                            owner.pmaker->record_verified_progress(
                                metadata.key.configuration,
                                LeaderProgressEvent::verified_proposal);
                        }
                        const auto timing_lease =
                            owner.proposal_contexts->acquire_open_context(metadata.key);
                        if (!timing_lease.has_value())
                        {
                            // A successful non-root send may close its exact
                            // context synchronously. Keep only its already-
                            // scheduled, immutable own-vote fallback alive so
                            // a crashed parent cannot strand that verified
                            // vote. Abort, commit, retirement, and shutdown
                            // still use the default full cleanup path.
                            owner.purge_pending_exact_contributions(
                                metadata.key, true);
                            log_callback(
                                "callback_end", "context_closed");
                            resolve_processing(
                                ProposalProcessingOutcome::
                                    completed_exposed);
                            return;
                        }
                        if (!certified_adaptive_mode)
                        {
                            owner.create_expected_vote_state(metadata.key);
                            static_cast<void>(
                                owner.start_latency_deadline(metadata.key));
                            owner.start_aggregation_timer(metadata.key);
                        }
                        owner.drain_pending_exact_contributions(metadata.key);
                        log_callback("callback_end", "complete");
                        resolve_processing(
                            ProposalProcessingOutcome::completed_exposed);
                    }
                    catch (const std::exception &error)
                    {
                        owner.rollback_retained_commit_event_identity_mutations(
                            retained_identity_rollback);
                        if (relay_exposure_attempted)
                            owner.mark_adaptive_v2_convergence_evidence_unhealthy(
                                "proposal_processing_failed_after_exposure");
                        abort(
                            "processing_exception",
                            relay_exposure_attempted
                                ? ProposalProcessingOutcome::
                                      terminal_post_relay
                                : ProposalProcessingOutcome::
                                      terminal_pre_relay,
                            true);
                        HOTSTUFF_LOG_WARN(
                            "[PROP HANDLER] Active proposal failed: %s",
                            error.what());
                    }
                    catch (...)
                    {
                        owner.rollback_retained_commit_event_identity_mutations(
                            retained_identity_rollback);
                        if (relay_exposure_attempted)
                            owner.mark_adaptive_v2_convergence_evidence_unhealthy(
                                "proposal_processing_failed_after_exposure");
                        abort(
                            "processing_exception",
                            relay_exposure_attempted
                                ? ProposalProcessingOutcome::
                                      terminal_post_relay
                                : ProposalProcessingOutcome::
                                      terminal_pre_relay,
                            true);
                        HOTSTUFF_LOG_WARN(
                            "[PROP HANDLER] Active proposal failed");
                    }
                },
                [access,
                 delivery_key,
                 delivery_recipient,
                 delivery_root,
                 retryable_claim,
                 attempt_state,
                 resolve_processing]() {
                    if (attempt_state != nullptr &&
                        !attempt_state->begin_processing())
                        return;
                    HOTSTUFF_LOG_INFO(
                        "KAURI_PROPOSAL_PROCESS stage=callback_begin "
                        "outcome=delivery_rejected recipient=%u root=%u "
                        "epoch=%u tree=%u block=%s",
                        static_cast<unsigned>(delivery_recipient),
                        static_cast<unsigned>(delivery_root),
                        delivery_key.configuration.epoch_number,
                        delivery_key.configuration.tree_id,
                        delivery_key.block_hash.to_hex().c_str());
                    auto runtime = access->acquire();
                    if (!runtime.has_value())
                    {
                        HOTSTUFF_LOG_INFO(
                            "KAURI_PROPOSAL_PROCESS stage=callback_abort "
                            "outcome=runtime_unavailable recipient=%u "
                            "root=%u epoch=%u tree=%u block=%s",
                            static_cast<unsigned>(delivery_recipient),
                            static_cast<unsigned>(delivery_root),
                            delivery_key.configuration.epoch_number,
                            delivery_key.configuration.tree_id,
                            delivery_key.block_hash.to_hex().c_str());
                        resolve_processing(
                            ProposalProcessingOutcome::terminal_pre_relay);
                        return;
                    }
                    auto &owner = runtime->owner();
                    owner.erase_deferred_epoch_change(delivery_key);
                    owner.proposal_contexts->close(
                        delivery_key,
                        ProposalContextEvent::proposal_aborted);
                    owner.forget_proposal_view_generation(delivery_key);
                    if (!retryable_claim &&
                        owner.proposal_admission != nullptr)
                        owner.proposal_admission->retire_proposal(
                            delivery_key);
                    owner.purge_pending_exact_contributions(delivery_key);
                    HOTSTUFF_LOG_INFO(
                        "KAURI_PROPOSAL_PROCESS stage=callback_abort "
                        "outcome=delivery_rejected recipient=%u root=%u "
                        "epoch=%u tree=%u block=%s",
                        static_cast<unsigned>(delivery_recipient),
                        static_cast<unsigned>(delivery_root),
                        delivery_key.configuration.epoch_number,
                        delivery_key.configuration.tree_id,
                        delivery_key.block_hash.to_hex().c_str());
                    resolve_processing(
                        retryable_claim
                            ? ProposalProcessingOutcome::
                                  retryable_pre_relay_failure
                            : ProposalProcessingOutcome::terminal_pre_relay);
                });
            return true;
        }
        catch (const std::bad_alloc &)
        {
            erase_deferred_epoch_change(proposal_key);
            proposal_contexts->close(
                proposal_key,
                ProposalContextEvent::proposal_aborted);
            forget_proposal_view_generation(proposal_key);
            if (!retryable_claim && proposal_admission != nullptr)
                proposal_admission->retire_proposal(proposal_key);
            purge_pending_exact_contributions(proposal_key);
            HOTSTUFF_LOG_WARN(
                "[PROP HANDLER] Active proposal allocation failed");
            resolve_processing(
                retryable_claim
                    ? ProposalProcessingOutcome::
                          retryable_pre_relay_failure
                    : ProposalProcessingOutcome::terminal_pre_relay);
        }
        catch (const std::exception &error)
        {
            erase_deferred_epoch_change(proposal_key);
            proposal_contexts->close(
                proposal_key,
                ProposalContextEvent::proposal_aborted);
            forget_proposal_view_generation(proposal_key);
            if (!retryable_claim && proposal_admission != nullptr)
                proposal_admission->retire_proposal(proposal_key);
            purge_pending_exact_contributions(proposal_key);
            HOTSTUFF_LOG_WARN(
                "[PROP HANDLER] Rejecting malformed active proposal: %s",
                error.what());
            resolve_processing(
                ProposalProcessingOutcome::terminal_pre_relay);
        }
        catch (...)
        {
            erase_deferred_epoch_change(proposal_key);
            proposal_contexts->close(
                proposal_key,
                ProposalContextEvent::proposal_aborted);
            forget_proposal_view_generation(proposal_key);
            if (!retryable_claim && proposal_admission != nullptr)
                proposal_admission->retire_proposal(proposal_key);
            purge_pending_exact_contributions(proposal_key);
            HOTSTUFF_LOG_WARN(
                "[PROP HANDLER] Rejecting malformed active proposal");
            resolve_processing(
                ProposalProcessingOutcome::terminal_pre_relay);
        }
        return true;
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
        PeerId authenticated_source,
        std::uint64_t received_ns)
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
            std::move(fingerprint),
            received_ns};
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
            contribution.authenticated_source,
            contribution.received_ns);
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
        const ProposalKey &key,
        bool preserve_scheduled_vote_fallback,
        bool preserve_response_evidence_until_deadline)
    {
        if (experiment_post_qc_audit != nullptr)
        {
            const auto generation =
                find_exact_runtime_generation(key.configuration);
            if (generation.has_value())
                static_cast<void>(
                    experiment_post_qc_audit
                        ->close_reporter_context(
                            key,
                            *generation,
                            adaptive_evidence_monotonic_now_ns()));
        }
        discard_exact_forwarding_retries(key);
        discard_exact_fallbacks(
            key, preserve_scheduled_vote_fallback);
        exact_root_repair_deliveries.erase(key);
        if (!preserve_scheduled_vote_fallback &&
            !preserve_response_evidence_until_deadline)
            authenticated_proposal_ingress.erase(key);
        static_cast<void>(pending_exact_contributions.purge(key));
        if (adaptive_v2_response_evidence != nullptr)
        {
            const bool durable_response_commit =
                has_durable_adaptive_v2_commit_report(key);
            const auto false_report =
                experiment_false_timeout_states.find(key);
            const bool durable_false_report_commit =
                false_report != experiment_false_timeout_states.end() &&
                false_report->second.commit_deferred;
            const bool retain_experiment_evidence =
                (preserve_response_evidence_until_deadline ||
                 durable_false_report_commit) &&
                experiment_byzantine_adapter != nullptr &&
                experiment_byzantine_adapter
                    ->should_retain_response_evidence(
                        ExperimentByzantineContext{
                            key,
                            experiment_diagnostic_window});
            // The false-report experiment owns its sole exact deadline and
            // releases the deliberately deferred commit marker after it
            // persists that observation. No normal deadline is armed for
            // this exact proposal.
            if (!retain_experiment_evidence)
            {
                if (preserve_response_evidence_until_deadline ||
                    durable_response_commit)
                    static_cast<void>(
                        adaptive_v2_response_evidence
                            ->close_consensus_context(key));
                else
                    static_cast<void>(
                        adaptive_v2_response_evidence->retire(key));
            }
        }
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

    bool HotStuffBase::consume_experiment_outbound_direct_vote(
        const ProposalKey &key,
        const ProposalTreeSnapshot &tree)
    {
        if (experiment_byzantine_adapter == nullptr ||
            tree.local_replica != get_id() ||
            !tree.parent.has_value() || !tree.direct_children.empty())
            return false;

        const auto scheduled_monotonic_ns =
            scheduled_omission_monotonic_now_ns(
                experiment_byzantine_adapter.get());
        ExperimentByzantineContext context{
            key,
            experiment_diagnostic_window};
        context.view_generation = proposal_view_generation(key);
        context.physical_parent = tree.parent;
        if (epoch_protocol_mode == EpochProtocolMode::adaptive_v2 &&
            experiment_byzantine_adapter->scheduled_omission_enabled())
        {
            const auto ingress = authenticated_proposal_ingress.find(key);
            if (!context.view_generation.has_value() ||
                ingress == authenticated_proposal_ingress.end() ||
                ingress->second.view_generation !=
                    *context.view_generation ||
                ingress->second.authenticated_proposal_source_replica !=
                    *tree.parent)
                return false;
            context.authenticated_proposal_source_replica =
                ingress->second.authenticated_proposal_source_replica;
        }
        context.expected_message_type =
            ExpectedMessageType::direct_vote;
        const auto disposition = experiment_byzantine_adapter
                                     ->consume_outbound_direct_vote(
            context,
            ExperimentReplicaRole::leaf,
            scheduled_monotonic_ns);
        if (disposition == ExperimentDirectVoteDisposition::forward)
            return false;
        if (disposition ==
            ExperimentDirectVoteDisposition::omit_repeat)
            return true;
        if (experiment_byzantine_adapter->scheduled_omission_enabled())
            return true;

        const auto marker_monotonic_ns =
            experiment_fault_marker_monotonic_now_ns();
        if (marker_monotonic_ns.has_value())
            HOTSTUFF_LOG_INFO(
                "KAURI_FAULT direct_vote_omitted replica=%u parent=%u "
                "epoch=%u tree=%u block=%s window=%s monotonic_ns=%llu",
                get_id(),
                *tree.parent,
                key.configuration.epoch_number,
                key.configuration.tree_id,
                key.block_hash.to_hex().c_str(),
                experiment_diagnostic_window.c_str(),
                static_cast<unsigned long long>(*marker_monotonic_ns));
        else
            HOTSTUFF_LOG_WARN(
                "KAURI_FAULT marker_skipped marker=direct_vote_omitted "
                "replica=%u epoch=%u tree=%u block=%s "
                "reason=event_clock_unavailable",
                get_id(),
                key.configuration.epoch_number,
                key.configuration.tree_id,
                key.block_hash.to_hex().c_str());
        return true;
    }

    bool HotStuffBase::consume_experiment_outbound_aggregate(
        const ProposalKey &key,
        const ProposalTreeSnapshot &tree)
    {
        if (experiment_byzantine_adapter == nullptr)
            return false;
        ExperimentByzantineContext context{
            key, experiment_diagnostic_window};
        context.view_generation = proposal_view_generation(key);
        context.physical_parent = tree.parent;
        if (epoch_protocol_mode == EpochProtocolMode::adaptive_v2 &&
            experiment_byzantine_adapter->scheduled_omission_enabled())
        {
            const auto ingress = authenticated_proposal_ingress.find(key);
            if (!context.view_generation.has_value() ||
                !tree.parent.has_value() ||
                ingress == authenticated_proposal_ingress.end() ||
                ingress->second.view_generation !=
                    *context.view_generation ||
                ingress->second.authenticated_proposal_source_replica !=
                    *tree.parent)
                return false;
            context.authenticated_proposal_source_replica =
                ingress->second.authenticated_proposal_source_replica;
        }
        context.expected_message_type =
            ExpectedMessageType::aggregate_relay;
        const auto omission_monotonic_ns =
            scheduled_omission_monotonic_now_ns(
                experiment_byzantine_adapter.get());
        if (!experiment_byzantine_adapter->consume_outbound_aggregate(
                context,
                experiment_replica_role(tree),
                omission_monotonic_ns))
        {
            return false;
        }
        if (!experiment_byzantine_adapter
                 ->consume_outbound_aggregate_marker(context))
        {
            return true;
        }

        const auto marker_monotonic_ns =
            experiment_fault_marker_monotonic_now_ns();
        if (marker_monotonic_ns.has_value())
            HOTSTUFF_LOG_INFO(
                "KAURI_FAULT aggregate_omitted replica=%u parent=%u "
                "epoch=%u tree=%u block=%s window=%s monotonic_ns=%llu",
                get_id(),
                tree.parent.value_or(get_id()),
                key.configuration.epoch_number,
                key.configuration.tree_id,
                key.block_hash.to_hex().c_str(),
                experiment_diagnostic_window.c_str(),
                static_cast<unsigned long long>(*marker_monotonic_ns));
        else
            HOTSTUFF_LOG_WARN(
                "KAURI_FAULT marker_skipped marker=aggregate_omitted "
                "replica=%u epoch=%u tree=%u block=%s "
                "reason=event_clock_unavailable",
                get_id(),
                key.configuration.epoch_number,
                key.configuration.tree_id,
                key.block_hash.to_hex().c_str());
        return true;
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

        if (experiment_byzantine_adapter != nullptr &&
            experiment_byzantine_adapter
                ->outbound_direct_vote_omitted(
                    ExperimentByzantineContext{
                        lease.key(),
                        experiment_diagnostic_window}))
            return true;

        if (consume_experiment_outbound_aggregate(
                lease.key(), lease.tree()))
            return true;

        try
        {
            VoteRelay relay(
                lease.key(), std::move(certificate), this);
            if (is_adaptive_epoch_mode(epoch_protocol_mode))
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
                    epoch_wire_limits,
                    epoch_protocol_mode);
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
        const bool trace_non_root =
            lease.tree().parent.has_value() &&
            lease.tree().root != get_id();
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
        if (trace_non_root)
            HOTSTUFF_LOG_INFO(
                "KAURI_RELAY_EGRESS stage=send_begin replica=%u parent=%u "
                "root=%u epoch=%u tree=%u block=%s role=%u attempts=%u "
                "signers=%zu",
                static_cast<unsigned>(get_id()),
                static_cast<unsigned>(*lease.tree().parent),
                static_cast<unsigned>(lease.tree().root),
                lease.key().configuration.epoch_number,
                lease.key().configuration.tree_id,
                lease.key().block_hash.to_hex().c_str(),
                static_cast<unsigned>(role),
                attempts,
                signers.size());
        const bool enqueued = send_exact_relay(
            lease, std::move(claim.certificate));
        if (trace_non_root)
            HOTSTUFF_LOG_INFO(
                "KAURI_RELAY_EGRESS stage=send_return replica=%u "
                "parent=%u root=%u epoch=%u tree=%u block=%s "
                "enqueued=%u",
                static_cast<unsigned>(get_id()),
                static_cast<unsigned>(*lease.tree().parent),
                static_cast<unsigned>(lease.tree().root),
                lease.key().configuration.epoch_number,
                lease.key().configuration.tree_id,
                lease.key().block_hash.to_hex().c_str(),
                static_cast<unsigned>(enqueued));
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
        if (trace_non_root)
            HOTSTUFF_LOG_INFO(
                "KAURI_RELAY_EGRESS stage=claim_commit_begin replica=%u "
                "parent=%u root=%u epoch=%u tree=%u block=%s",
                static_cast<unsigned>(get_id()),
                static_cast<unsigned>(*lease.tree().parent),
                static_cast<unsigned>(lease.tree().root),
                lease.key().configuration.epoch_number,
                lease.key().configuration.tree_id,
                lease.key().block_hash.to_hex().c_str());
        const bool committed =
            proposal_contexts->commit_forwarding_claim(
                lease, reservation_id);
        if (trace_non_root)
            HOTSTUFF_LOG_INFO(
                "KAURI_RELAY_EGRESS stage=claim_commit_result replica=%u "
                "parent=%u root=%u epoch=%u tree=%u block=%s committed=%u",
                static_cast<unsigned>(get_id()),
                static_cast<unsigned>(*lease.tree().parent),
                static_cast<unsigned>(lease.tree().root),
                lease.key().configuration.epoch_number,
                lease.key().configuration.tree_id,
                lease.key().block_hash.to_hex().c_str(),
                static_cast<unsigned>(committed));
        if (!committed)
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
        if (trace_non_root)
            HOTSTUFF_LOG_INFO(
                "KAURI_RELAY_EGRESS stage=complete_begin replica=%u "
                "parent=%u root=%u epoch=%u tree=%u block=%s",
                static_cast<unsigned>(get_id()),
                static_cast<unsigned>(*lease.tree().parent),
                static_cast<unsigned>(lease.tree().root),
                lease.key().configuration.epoch_number,
                lease.key().configuration.tree_id,
                lease.key().block_hash.to_hex().c_str());
        complete_exact_forwarding(lease, role);
        if (trace_non_root)
            HOTSTUFF_LOG_INFO(
                "KAURI_RELAY_EGRESS stage=complete_end replica=%u parent=%u "
                "root=%u epoch=%u tree=%u block=%s",
                static_cast<unsigned>(get_id()),
                static_cast<unsigned>(*lease.tree().parent),
                static_cast<unsigned>(lease.tree().root),
                lease.key().configuration.epoch_number,
                lease.key().configuration.tree_id,
                lease.key().block_hash.to_hex().c_str());
        return true;
    }

    quorum_cert_bt HotStuffBase::make_exact_direct_forwarding_candidate(
        const ProposalContextLease &lease,
        const Vote &vote)
    {
        if (vote.cert == nullptr || vote.key() != lease.key())
            return nullptr;
        const bool trace_local_root =
            vote.voter == get_id() && lease.tree().root == get_id();
        try
        {
            auto certificate = create_quorum_cert(lease.key());
            certificate->add_verified_part(
                config, vote.voter, *vote.cert);
            if (trace_local_root)
                HOTSTUFF_LOG_INFO(
                    "KAURI_LOCAL_PROPOSAL stage=candidate_compute_begin "
                    "replica=%u epoch=%u tree=%u block=%s",
                    static_cast<unsigned>(get_id()),
                    lease.key().configuration.epoch_number,
                    lease.key().configuration.tree_id,
                    lease.key().block_hash.to_hex().c_str());
            certificate->compute();
            if (trace_local_root)
                HOTSTUFF_LOG_INFO(
                    "KAURI_LOCAL_PROPOSAL stage=candidate_compute_end "
                    "replica=%u epoch=%u tree=%u block=%s",
                    static_cast<unsigned>(get_id()),
                    lease.key().configuration.epoch_number,
                    lease.key().configuration.tree_id,
                    lease.key().block_hash.to_hex().c_str());
            if (trace_local_root)
                HOTSTUFF_LOG_INFO(
                    "KAURI_LOCAL_PROPOSAL stage=candidate_verify_begin "
                    "replica=%u epoch=%u tree=%u block=%s",
                    static_cast<unsigned>(get_id()),
                    lease.key().configuration.epoch_number,
                    lease.key().configuration.tree_id,
                    lease.key().block_hash.to_hex().c_str());
            const bool verified = certificate->verify(config);
            if (trace_local_root)
                HOTSTUFF_LOG_INFO(
                    "KAURI_LOCAL_PROPOSAL stage=candidate_verify_end "
                    "replica=%u epoch=%u tree=%u block=%s verified=%u",
                    static_cast<unsigned>(get_id()),
                    lease.key().configuration.epoch_number,
                    lease.key().configuration.tree_id,
                    lease.key().block_hash.to_hex().c_str(),
                    static_cast<unsigned>(verified));
            if (!verified)
                return nullptr;
            return certificate;
        }
        catch (...)
        {
            if (trace_local_root)
                HOTSTUFF_LOG_INFO(
                    "KAURI_LOCAL_PROPOSAL stage=candidate_exception "
                    "replica=%u epoch=%u tree=%u block=%s",
                    static_cast<unsigned>(get_id()),
                    lease.key().configuration.epoch_number,
                    lease.key().configuration.tree_id,
                    lease.key().block_hash.to_hex().c_str());
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
            if (transition ==
                    ProposalTransitionResult::terminal_closed &&
                experiment_post_qc_audit != nullptr)
            {
                const auto generation = find_exact_runtime_generation(
                    lease.key().configuration);
                if (generation.has_value())
                    static_cast<void>(
                        experiment_post_qc_audit
                            ->close_reporter_context(
                                lease.key(),
                                *generation,
                                adaptive_evidence_monotonic_now_ns()));
            }
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
        static_cast<void>(proposal_contexts->transition(
            lease, ProposalContextEvent::proposal_aborted));
        forget_proposal_view_generation(lease.key());
        purge_pending_exact_contributions(lease.key());
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

    void HotStuffBase::schedule_exact_vote_fallback(
        const ProposalContextLease &lease,
        const Vote &vote)
    {
        if (!lease.tree().parent.has_value() ||
            lease.tree().local_replica != get_id() ||
            vote.voter != get_id() || vote.key() != lease.key() ||
            vote.cert == nullptr || aggregation_scheduler == nullptr ||
            proposal_admission == nullptr ||
            exact_vote_fallback_jobs.count(lease.key()) != 0)
            return;
        const auto *tree = find_exact_runtime_tree(
            lease.key().configuration);
        const auto generation = find_exact_runtime_generation(
            lease.key().configuration);
        if (tree == nullptr || !generation.has_value() ||
            tree->get_tree().get_tree_root() != lease.tree().root)
            return;

        // A proposal received directly from its authenticated root by a
        // non-child descendant is already on the repair path.  Returning the
        // same exact vote now replaces (rather than supplements) the delayed
        // fallback, removing a second full-tree timeout from recovery.
        const auto repair = exact_root_repair_deliveries.find(lease.key());
        if (repair != exact_root_repair_deliveries.end() &&
            repair->second == *generation)
        {
            exact_root_repair_deliveries.erase(repair);
            const bool sent = send_exact_vote_to_root(
                lease.key(), *generation, lease.tree().root, vote);
            HOTSTUFF_LOG_INFO(
                "KAURI_EXACT_REPAIR stage=vote_fast_return outcome=%s "
                "replica=%u root=%u epoch=%u tree=%u block=%s "
                "trigger=local_vote",
                sent ? "enqueued" : "deferred",
                static_cast<unsigned>(get_id()),
                static_cast<unsigned>(lease.tree().root),
                lease.key().configuration.epoch_number,
                lease.key().configuration.tree_id,
                lease.key().block_hash.to_hex().c_str());
            if (sent)
                return;
        }

        try
        {
            const auto delay = aggregation_timeout_policy.timeout_for(
                0,
                static_cast<std::uint32_t>(tree->get_max_level()));
            auto job = std::make_shared<ExactVoteFallbackJob>(
                lease, *generation, lease.tree().root, vote);
            exact_vote_fallback_jobs.emplace(lease.key(), job);
            auto cancellation = aggregation_scheduler->schedule_after(
                delay,
                [access = exact_runtime_access,
                 key = lease.key(),
                 context_generation = lease.generation()]() {
                    auto runtime = access->acquire();
                    if (!runtime.has_value())
                        return;
                    runtime->owner().dispatch_exact_vote_fallback(
                        key, context_generation);
                });
            if (!cancellation)
            {
                exact_vote_fallback_jobs.erase(lease.key());
                return;
            }
            job->cancellation = std::move(cancellation);
        }
        catch (...)
        {
            exact_vote_fallback_jobs.erase(lease.key());
        }
    }

    void HotStuffBase::observe_exact_root_repair_delivery(
        const EpochConsensusEnvelope &envelope,
        ReplicaID authenticated_sender,
        ProposalDisposition disposition)
    {
        if ((envelope.kind != EpochConsensusWireKind::proposal &&
             envelope.kind !=
                 EpochConsensusWireKind::proposal_repair) ||
            (disposition != ProposalDisposition::admitted_active &&
             disposition != ProposalDisposition::duplicate))
            return;
        const auto key = envelope.key();
        const auto active = proposal_contexts->active_configuration();
        const auto generation = find_exact_runtime_generation(
            key.configuration);
        const auto metadata = exact_context_metadata(key);
        if (!active.has_value() || *active != key.configuration ||
            !generation.has_value() ||
            *generation != envelope.view_generation ||
            !metadata.has_value() ||
            proposal_admission == nullptr ||
            !proposal_admission->contains_admitted(key) ||
            metadata->tree.local_replica != get_id() ||
            metadata->tree.root != envelope.proposer ||
            authenticated_sender != metadata->tree.root ||
            !metadata->tree.parent.has_value() ||
            *metadata->tree.parent == authenticated_sender)
            return;

        const auto found = exact_vote_fallback_jobs.find(key);
        if (found == exact_vote_fallback_jobs.end())
        {
            // The first copy may still be fetching or verifying when repair
            // arrives as a duplicate, so remember both accepted dispositions
            // until that exact local vote is authorized.  Do not recreate a
            // marker after the proposal has reached a terminal context, or
            // after an open context has already recorded this replica's vote.
            const auto status = proposal_contexts->context_status(key);
            if (status == ProposalContextStatus::terminal_closed ||
                status == ProposalContextStatus::retired)
                return;
            const auto snapshot = proposal_contexts->snapshot(key);
            if (snapshot.has_value() &&
                snapshot->verified_signers.count(get_id()) != 0)
                return;
            exact_root_repair_deliveries.insert_or_assign(
                key, *generation);
            return;
        }
        const auto &job = found->second;
        // A successful leaf-to-parent forward can synchronously close the
        // exact context while deliberately preserving this immutable vote
        // fallback.  The fallback job itself, active exact generation and
        // admitted identity are sufficient; requiring an open lease here
        // would reintroduce the second full-tree deadline for that common
        // repair path.
        if (job == nullptr || job->key != key ||
            job->epoch_generation != *generation ||
            job->root != metadata->tree.root || job->vote == nullptr ||
            job->vote->cert == nullptr)
            return;

        const bool sent = send_exact_vote_to_root(
            key, job->epoch_generation, job->root, *job->vote);
        HOTSTUFF_LOG_INFO(
            "KAURI_EXACT_REPAIR stage=vote_fast_return outcome=%s "
            "replica=%u root=%u epoch=%u tree=%u block=%s "
            "trigger=repair_delivery",
            sent ? "enqueued" : "deferred",
            static_cast<unsigned>(get_id()),
            static_cast<unsigned>(job->root),
            key.configuration.epoch_number,
            key.configuration.tree_id,
            key.block_hash.to_hex().c_str());
        if (!sent)
            return;

        auto cancellation = std::move(job->cancellation);
        exact_vote_fallback_jobs.erase(found);
        exact_root_repair_deliveries.erase(key);
        if (cancellation)
            try
            {
                cancellation();
            }
            catch (...)
            {}
    }

    bool HotStuffBase::process_exact_proposal_catchup(
        const EpochConsensusEnvelope &envelope,
        const PeerId &source_peer) noexcept
    {
        if (epoch_protocol_mode != EpochProtocolMode::adaptive_v3 ||
            envelope.kind !=
                EpochConsensusWireKind::proposal_repair ||
            source_peer.is_null())
            return false;
        try
        {
            MsgPropose message(DataStream(envelope.body), true);
            message.postponed_parse(this);
            auto proposal = std::move(message.proposal);
            if (proposal.metadata().key() != envelope.key() ||
                proposal.proposer != envelope.proposer ||
                proposal.blk == nullptr)
                return false;

            const auto expected_hash = envelope.block_hash;
            const auto configuration = envelope.configuration;
            const auto generation = envelope.view_generation;
            const auto source = source_peer;
            async_deliver_blk(expected_hash, source).then(
                [access = exact_runtime_access,
                 expected_hash,
                 configuration,
                 generation](const block_t &delivered) {
                    auto runtime = access->acquire();
                    if (!runtime.has_value())
                        return;
                    HOTSTUFF_LOG_INFO(
                        "KAURI_PROPOSAL_CATCHUP outcome=%s replica=%u "
                        "epoch=%u tree=%u block=%s generation=%llu",
                        delivered != nullptr && delivered->delivered &&
                                delivered->get_hash() == expected_hash
                            ? "delivered"
                            : "rejected",
                        static_cast<unsigned>(
                            runtime->owner().get_id()),
                        configuration.epoch_number,
                        configuration.tree_id,
                        expected_hash.to_hex().c_str(),
                        static_cast<unsigned long long>(generation));
                });
            return true;
        }
        catch (...)
        {
            return false;
        }
    }

    void HotStuffBase::dispatch_exact_vote_fallback(
        const ProposalKey &key,
        std::uint64_t context_generation)
    {
        const auto found = exact_vote_fallback_jobs.find(key);
        if (found == exact_vote_fallback_jobs.end() ||
            found->second->context_generation != context_generation)
            return;
        auto job = found->second;
        job->cancellation = {};
        exact_vote_fallback_jobs.erase(found);
        exact_root_repair_deliveries.erase(key);

        const auto active = proposal_contexts->active_configuration();
        const auto generation = find_exact_runtime_generation(
            key.configuration);
        const auto metadata = exact_context_metadata(key);
        if (!active.has_value() || *active != key.configuration ||
            !generation.has_value() || *generation != job->epoch_generation ||
            metadata == std::nullopt || metadata->tree.root != job->root ||
            metadata->tree.local_replica != get_id() ||
            std::find(
                metadata->tree.assigned_subtree.begin(),
                metadata->tree.assigned_subtree.end(),
                get_id()) == metadata->tree.assigned_subtree.end() ||
            proposal_admission == nullptr ||
            !proposal_admission->contains_admitted(key) ||
            job->vote == nullptr || job->vote->cert == nullptr)
            return;

        static_cast<void>(send_exact_vote_to_root(
            key, job->epoch_generation, job->root, *job->vote));
    }

    bool HotStuffBase::send_exact_vote_to_root(
        const ProposalKey &key,
        std::uint64_t epoch_generation,
        ReplicaID root,
        const Vote &vote)
    {
        if (vote.key() != key || vote.voter != get_id() ||
            vote.cert == nullptr || root == get_id())
            return false;
        if (experiment_byzantine_adapter != nullptr &&
            experiment_byzantine_adapter
                ->outbound_direct_vote_omitted(
                    ExperimentByzantineContext{
                        key,
                        experiment_diagnostic_window}))
            return true;
        const auto peer = config.get_peer_id(root);
        if (peer.is_null())
            return false;
        try
        {
            if (is_adaptive_epoch_mode(epoch_protocol_mode))
            {
                if (adaptive_epoch_runtime == nullptr)
                    return false;
                const MsgVote native(vote);
                const auto encoded = adaptive_epoch_consensus_message(
                    key.configuration,
                    epoch_generation,
                    EpochConsensusWireKind::vote,
                    key,
                    get_id(),
                    root,
                    static_cast<bytearray_t>(native.serialized),
                    epoch_wire_limits,
                    epoch_protocol_mode);
                if (encoded.empty())
                    return false;
                return pn.send_msg(MsgVote(DataStream(encoded)), peer);
            }
            return pn.send_msg(MsgVote(vote), peer);
        }
        catch (...)
        {
            return false;
        }
    }

    void HotStuffBase::schedule_exact_proposal_fallback(
        const ProposalContextLease &lease,
        const Proposal &proposal)
    {
        if (lease.tree().parent.has_value() ||
            lease.tree().local_replica != get_id() ||
            lease.tree().root != get_id() || proposal.key() != lease.key() ||
            aggregation_scheduler == nullptr ||
            exact_proposal_fallback_jobs.count(lease.key()) != 0)
        {
            HOTSTUFF_LOG_INFO(
                "KAURI_PROPOSAL_BROADCAST stage=fallback "
                "outcome=skipped reason=precondition replica=%u epoch=%u "
                "tree=%u block=%s parent=%u local_match=%u root_match=%u "
                "key_match=%u scheduler=%u duplicate=%u",
                static_cast<unsigned>(get_id()),
                lease.key().configuration.epoch_number,
                lease.key().configuration.tree_id,
                lease.key().block_hash.to_hex().c_str(),
                lease.tree().parent.has_value() ? 1U : 0U,
                lease.tree().local_replica == get_id() ? 1U : 0U,
                lease.tree().root == get_id() ? 1U : 0U,
                proposal.key() == lease.key() ? 1U : 0U,
                aggregation_scheduler != nullptr ? 1U : 0U,
                exact_proposal_fallback_jobs.count(lease.key()) != 0
                    ? 1U
                    : 0U);
            return;
        }
        const auto *tree = find_exact_runtime_tree(
            lease.key().configuration);
        const auto generation = find_exact_runtime_generation(
            lease.key().configuration);
        if (tree == nullptr || !generation.has_value())
        {
            HOTSTUFF_LOG_INFO(
                "KAURI_PROPOSAL_BROADCAST stage=fallback "
                "outcome=skipped reason=exact_runtime replica=%u epoch=%u "
                "tree=%u block=%s exact_tree=%u generation=%u",
                static_cast<unsigned>(get_id()),
                lease.key().configuration.epoch_number,
                lease.key().configuration.tree_id,
                lease.key().block_hash.to_hex().c_str(),
                tree != nullptr ? 1U : 0U,
                generation.has_value() ? 1U : 0U);
            return;
        }

        try
        {
            const auto maximum_level =
                static_cast<std::uint32_t>(tree->get_max_level());
            const auto delay = aggregation_timeout_policy.timeout_for(
                0, maximum_level);
            const auto leaf_delay = aggregation_timeout_policy.timeout_for(
                maximum_level, maximum_level);
            const auto target_count = static_cast<std::size_t>(std::count_if(
                lease.tree().assigned_subtree.begin(),
                lease.tree().assigned_subtree.end(),
                [this](ReplicaID member) { return member != get_id(); }));
            const auto global_quorum =
                proposal_contexts->frozen_global_quorum(lease);
            if (!global_quorum.has_value() || target_count == 0)
                return;
            const auto stage_target_limit = std::max<std::size_t>(
                1, lease.tree().fanout);
            const auto stage_count = std::max<std::size_t>(
                1,
                (target_count + stage_target_limit - 1) /
                    stage_target_limit);
            auto stage_interval = leaf_delay;
            if (stage_count > 1)
            {
                const auto divisor = static_cast<
                    AggregationTimeoutPolicy::Duration::rep>(
                    stage_count - 1);
                stage_interval = AggregationTimeoutPolicy::Duration(
                    std::max<AggregationTimeoutPolicy::Duration::rep>(
                        1, leaf_delay.count() / divisor));
            }
            auto job = std::make_shared<ExactProposalFallbackJob>(
                lease,
                *generation,
                proposal,
                *global_quorum,
                target_count,
                stage_target_limit,
                stage_interval);
            exact_proposal_fallback_jobs.emplace(lease.key(), job);
            auto cancellation = aggregation_scheduler->schedule_after(
                delay,
                [access = exact_runtime_access,
                 key = lease.key(),
                 context_generation = lease.generation()]() {
                    auto runtime = access->acquire();
                    if (!runtime.has_value())
                        return;
                    runtime->owner().dispatch_exact_proposal_fallback(
                        key, context_generation);
                });
            if (!cancellation)
            {
                exact_proposal_fallback_jobs.erase(lease.key());
                HOTSTUFF_LOG_INFO(
                    "KAURI_PROPOSAL_BROADCAST stage=fallback "
                    "outcome=skipped reason=schedule_rejected replica=%u "
                    "epoch=%u tree=%u block=%s generation=%llu",
                    static_cast<unsigned>(get_id()),
                    lease.key().configuration.epoch_number,
                    lease.key().configuration.tree_id,
                    lease.key().block_hash.to_hex().c_str(),
                    static_cast<unsigned long long>(*generation));
                return;
            }
            job->cancellation = std::move(cancellation);
            HOTSTUFF_LOG_INFO(
                "KAURI_PROPOSAL_BROADCAST stage=fallback outcome=armed "
                "reason=none replica=%u epoch=%u tree=%u block=%s "
                "generation=%llu delay_ticks=%lld stage_targets=%zu "
                "stages=%zu stage_interval_ticks=%lld",
                static_cast<unsigned>(get_id()),
                lease.key().configuration.epoch_number,
                lease.key().configuration.tree_id,
                lease.key().block_hash.to_hex().c_str(),
                static_cast<unsigned long long>(*generation),
                static_cast<long long>(delay.count()),
                stage_target_limit,
                stage_count,
                static_cast<long long>(stage_interval.count()));
        }
        catch (...)
        {
            exact_proposal_fallback_jobs.erase(lease.key());
            HOTSTUFF_LOG_INFO(
                "KAURI_PROPOSAL_BROADCAST stage=fallback "
                "outcome=skipped reason=schedule_exception replica=%u "
                "epoch=%u tree=%u block=%s generation=%llu",
                static_cast<unsigned>(get_id()),
                lease.key().configuration.epoch_number,
                lease.key().configuration.tree_id,
                lease.key().block_hash.to_hex().c_str(),
                static_cast<unsigned long long>(*generation));
        }
    }

    void HotStuffBase::dispatch_exact_proposal_fallback(
        const ProposalKey &key,
        std::uint64_t context_generation)
    {
        const auto found = exact_proposal_fallback_jobs.find(key);
        if (found == exact_proposal_fallback_jobs.end() ||
            found->second->context_generation != context_generation)
            return;
        auto job = found->second;
        if (job->tail_armed || job->quorum_observed)
            return;
        if (job->dispatching)
            return;
        job->dispatching = true;
        job->cancellation = {};

        const auto erase_job = [this, &key, &job]() {
            const auto current = exact_proposal_fallback_jobs.find(key);
            if (current != exact_proposal_fallback_jobs.end() &&
                current->second == job)
                exact_proposal_fallback_jobs.erase(current);
        };

        const auto lease = proposal_contexts->acquire_open_context(key);
        const auto *tree = find_exact_runtime_tree(key.configuration);
        const auto generation = find_exact_runtime_generation(
            key.configuration);
        const bool may_drain_exact_configuration =
            adaptive_epoch_runtime != nullptr &&
            adaptive_epoch_runtime->activation.may_drain_exact_context(
                key.configuration);
        if (!lease.has_value() ||
            lease->generation() != context_generation ||
            lease->tree().root != get_id() ||
            lease->tree().parent.has_value() ||
            !may_drain_exact_configuration ||
            tree == nullptr ||
            !generation.has_value() || *generation != job->epoch_generation ||
            adaptive_epoch_runtime == nullptr ||
            !adaptive_epoch_runtime->activation.admits_new_proposals() ||
            job->proposal == nullptr)
        {
            erase_job();
            return;
        }
        const auto before = proposal_contexts->snapshot(key);
        const auto quorum = proposal_contexts->frozen_global_quorum(*lease);
        if (!before.has_value() || !quorum.has_value() ||
            *quorum != job->global_quorum)
        {
            erase_job();
            return;
        }
        if (before->verified_signers.size() >= *quorum)
        {
            arm_exact_proposal_repair_tail(*lease);
            job->dispatching = false;
            return;
        }
        if (job->pending_pre_quorum_refresh_batch.empty() &&
            job->total_send_attempts >= job->total_attempt_budget)
        {
            erase_job();
            return;
        }

        if (!job->pre_quorum_retry_armed)
        {
            std::set<ReplicaID> missing_members;
            for (const auto member : lease->tree().assigned_subtree)
                if (member != get_id() &&
                    before->verified_signers.count(member) == 0 &&
                    missing_members.insert(member).second)
                    job->pre_quorum_retry_targets.push_back(member);
            if (job->pre_quorum_retry_targets.empty())
            {
                erase_job();
                return;
            }
            job->pre_quorum_retry_armed = true;
            HOTSTUFF_LOG_INFO(
                "KAURI_PROPOSAL_BROADCAST stage=pre_qc_retry "
                "outcome=armed reason=fresh_first root=%u epoch=%u tree=%u "
                "block=%s verified=%zu quorum=%zu candidates=%zu attempts=%zu "
                "budget=%zu",
                static_cast<unsigned>(get_id()),
                key.configuration.epoch_number,
                key.configuration.tree_id,
                key.block_hash.to_hex().c_str(),
                before->verified_signers.size(),
                *quorum,
                job->pre_quorum_retry_targets.size(),
                job->total_send_attempts,
                job->total_attempt_budget);
        }

        const auto retry_cursor_before = job->pre_quorum_retry_cursor;
        const auto attempts_before = job->total_send_attempts;
        ++job->completed_stages;
        if (!job->pending_pre_quorum_refresh_batch.empty())
            static_cast<void>(
                broadcast_exact_proposal_pre_quorum_retry(*lease, *job));

        const auto refreshed = proposal_contexts->snapshot(key);
        if (!refreshed.has_value())
        {
            erase_job();
            return;
        }
        if (refreshed->verified_signers.size() >= *quorum)
        {
            arm_exact_proposal_repair_tail(*lease);
            job->dispatching = false;
            return;
        }
        const auto remaining_budget =
            job->total_attempt_budget - job->total_send_attempts;
        const auto maximum_attempts = std::min(
            job->stage_target_limit, remaining_budget);
        static_cast<void>(reserve_exact_proposal_pre_quorum_refresh(
            *lease, *job, maximum_attempts));
        if (!job->pending_pre_quorum_refresh_batch.empty())
            static_cast<void>(
                broadcast_exact_proposal_pre_quorum_retry(*lease, *job));

        const auto next_lease =
            proposal_contexts->acquire_open_context(key);
        if (!next_lease.has_value() ||
            next_lease->generation() != context_generation)
        {
            erase_job();
            return;
        }
        const auto after = proposal_contexts->snapshot(key);
        if (!after.has_value())
        {
            erase_job();
            return;
        }
        if (after->verified_signers.size() >= *quorum)
        {
            arm_exact_proposal_repair_tail(*next_lease);
            job->dispatching = false;
            return;
        }

        if (job->pending_pre_quorum_refresh_batch.empty() &&
            job->total_send_attempts >= job->total_attempt_budget)
        {
            erase_job();
            return;
        }
        if (job->pre_quorum_retry_armed)
        {
            const bool no_reservation_progress =
                job->pending_pre_quorum_refresh_batch.empty() &&
                job->pre_quorum_retry_cursor == retry_cursor_before &&
                job->total_send_attempts == attempts_before;
            const bool candidates_exhausted =
                job->pre_quorum_retry_cursor >=
                job->pre_quorum_retry_targets.size();
            if (no_reservation_progress ||
                (job->pending_pre_quorum_refresh_batch.empty() &&
                 candidates_exhausted))
            {
                // The first pass may finish before a delayed contribution
                // completes the quorum.  Keep the immutable job parked so
                // try_finish_exact_context can spend the remaining budget on
                // the post-QC repair tail.  Terminal context cleanup still
                // erases an unarmed job, and parking sends no extra traffic.
                job->dispatching = false;
                job->cancellation = {};
                HOTSTUFF_LOG_INFO(
                    "KAURI_PROPOSAL_BROADCAST stage=fallback "
                    "outcome=parked "
                    "reason=pre_qc_candidates_exhausted root=%u epoch=%u "
                    "tree=%u block=%s verified=%zu quorum=%zu attempts=%zu "
                    "budget=%zu",
                    static_cast<unsigned>(get_id()),
                    key.configuration.epoch_number,
                    key.configuration.tree_id,
                    key.block_hash.to_hex().c_str(),
                    after->verified_signers.size(),
                    *quorum,
                    job->total_send_attempts,
                    job->total_attempt_budget);
                return;
            }
        }

        bool rearm_failed = false;
        try
        {
            auto cancellation = aggregation_scheduler->schedule_after(
                job->stage_interval,
                [access = exact_runtime_access,
                 key,
                 context_generation]() {
                    auto runtime = access->acquire();
                    if (!runtime.has_value())
                        return;
                    runtime->owner().dispatch_exact_proposal_fallback(
                        key, context_generation);
                });
            if (!cancellation)
                rearm_failed = true;
            else
            {
                job->cancellation = std::move(cancellation);
                job->dispatching = false;
                HOTSTUFF_LOG_INFO(
                    "KAURI_PROPOSAL_BROADCAST stage=fallback_stage "
                    "outcome=armed root=%u epoch=%u tree=%u block=%s "
                    "completed_stages=%u next_stage=%u verified=%zu "
                    "quorum=%zu cursor=%zu pending_refresh=%zu "
                    "delay_ticks=%lld",
                    static_cast<unsigned>(get_id()),
                    key.configuration.epoch_number,
                    key.configuration.tree_id,
                    key.block_hash.to_hex().c_str(),
                    job->completed_stages,
                    job->completed_stages + 1,
                    after->verified_signers.size(),
                    *quorum,
                    job->pre_quorum_retry_armed
                        ? job->pre_quorum_retry_cursor
                        : job->target_cursor,
                    job->pending_pre_quorum_refresh_batch.size(),
                    static_cast<long long>(job->stage_interval.count()));
                return;
            }
        }
        catch (...)
        {
            rearm_failed = true;
        }

        // A scheduler failure gets one bounded chance to dispatch work that
        // was already reserved. It never falls back to a blind send.
        if (!rearm_failed)
            return;
        const auto drain_lease =
            proposal_contexts->acquire_open_context(key);
        if (!drain_lease.has_value() ||
            drain_lease->generation() != context_generation)
        {
            erase_job();
            return;
        }
        if (!job->pending_pre_quorum_refresh_batch.empty())
            static_cast<void>(broadcast_exact_proposal_pre_quorum_retry(
                *drain_lease, *job));
        erase_job();
    }

    void HotStuffBase::arm_exact_proposal_repair_tail(
        const ProposalContextLease &lease) noexcept
    {
        try
        {
            const auto found =
                exact_proposal_fallback_jobs.find(lease.key());
            if (found == exact_proposal_fallback_jobs.end() ||
                found->second->context_generation != lease.generation())
                return;
            auto job = found->second;
            if (job->tail_armed || job->proposal == nullptr)
                return;
            const auto snapshot = proposal_contexts->snapshot(job->key);
            if (!snapshot.has_value() ||
                snapshot->verified_signers.size() < job->global_quorum)
                return;

            // This bit stops an in-flight first-pass send loop even when
            // every attempted target is already confirmed and no tail is
            // necessary.
            job->quorum_observed = true;
            job->confirmed_signers = snapshot->verified_signers;
            const auto remaining_budget =
                job->total_send_attempts < job->total_attempt_budget
                    ? job->total_attempt_budget - job->total_send_attempts
                    : 0;

            // Spend the immutable N-1 fallback budget on fresh repair targets
            // before a retry.  A replica omitted from every repair stage
            // cannot process a later pipelined proposal if this proposal is
            // missing from its ancestry.  The old attempted-only tail could
            // therefore retry a slow path while leaving a fresh path behind.
            std::set<ReplicaID> attempted_targets(
                job->attempted_targets.begin(),
                job->attempted_targets.end());
            std::set<ReplicaID> assigned_targets(
                lease.tree().assigned_subtree.begin(),
                lease.tree().assigned_subtree.end());
            std::set<ReplicaID> queued_targets;
            const auto queue_tail_target = [&](ReplicaID member) {
                if (member == get_id() ||
                    assigned_targets.count(member) == 0 ||
                    snapshot->verified_signers.count(member) != 0 ||
                    !queued_targets.insert(member).second)
                    return;
                job->tail_targets.push_back(member);
            };
            for (const auto member : lease.tree().assigned_subtree)
                if (attempted_targets.count(member) == 0)
                    queue_tail_target(member);
            const auto fresh_candidate_count = job->tail_targets.size();
            for (const auto member : job->attempted_targets)
                queue_tail_target(member);
            const auto retry_candidate_count =
                job->tail_targets.size() - fresh_candidate_count;

            auto previous = std::move(job->cancellation);
            job->cancellation = {};
            if (previous)
                try
                {
                    previous();
                }
                catch (...)
                {}

            if (remaining_budget == 0 || job->tail_targets.empty() ||
                aggregation_scheduler == nullptr)
            {
                exact_proposal_fallback_jobs.erase(found);
                return;
            }

            job->tail_armed = true;
            try
            {
                auto cancellation = aggregation_scheduler->schedule_after(
                    job->stage_interval,
                    [access = exact_runtime_access,
                     job]() {
                        auto runtime = access->acquire();
                        if (!runtime.has_value())
                            return;
                        runtime->owner()
                            .dispatch_exact_proposal_repair_tail(job);
                    });
                if (!cancellation)
                {
                    exact_proposal_fallback_jobs.erase(found);
                    return;
                }
                job->cancellation = std::move(cancellation);
                HOTSTUFF_LOG_INFO(
                    "KAURI_PROPOSAL_BROADCAST stage=repair_tail "
                    "outcome=armed root=%u epoch=%u tree=%u block=%s "
                    "confirmed=%zu candidates=%zu fresh_candidates=%zu "
                    "retry_candidates=%zu attempts=%zu budget=%zu "
                    "delay_ticks=%lld",
                    static_cast<unsigned>(get_id()),
                    job->key.configuration.epoch_number,
                    job->key.configuration.tree_id,
                    job->key.block_hash.to_hex().c_str(),
                    job->confirmed_signers.size(),
                    job->tail_targets.size(),
                    fresh_candidate_count,
                    retry_candidate_count,
                    job->total_send_attempts,
                    job->total_attempt_budget,
                    static_cast<long long>(job->stage_interval.count()));
            }
            catch (...)
            {
                exact_proposal_fallback_jobs.erase(found);
            }
        }
        catch (...)
        {
            // Repair is best-effort dissemination. Resource or scheduler
            // failure must never escape into the verified QC path.
            const auto failed =
                exact_proposal_fallback_jobs.find(lease.key());
            if (failed == exact_proposal_fallback_jobs.end())
                return;
            auto cancellation = std::move(failed->second->cancellation);
            exact_proposal_fallback_jobs.erase(failed);
            if (cancellation)
                try
                {
                    cancellation();
                }
                catch (...)
                {}
        }
    }

    void HotStuffBase::dispatch_exact_proposal_repair_tail(
        const std::shared_ptr<ExactProposalFallbackJob> &job)
    {
        if (job == nullptr)
            return;
        const auto found = exact_proposal_fallback_jobs.find(job->key);
        if (found == exact_proposal_fallback_jobs.end() ||
            found->second != job || !job->tail_armed || job->dispatching)
            return;
        job->dispatching = true;
        job->cancellation = {};

        const auto generation = find_exact_runtime_generation(
            job->key.configuration);
        const bool may_drain_exact_configuration =
            adaptive_epoch_runtime != nullptr &&
            adaptive_epoch_runtime->activation.may_drain_exact_context(
                job->key.configuration);
        if (!may_drain_exact_configuration ||
            !generation.has_value() ||
            *generation != job->epoch_generation ||
            adaptive_epoch_runtime == nullptr ||
            !adaptive_epoch_runtime->activation.admits_new_proposals() ||
            aggregation_scheduler == nullptr ||
            job->proposal == nullptr || job->proposal->key() != job->key ||
            job->total_send_attempts >= job->total_attempt_budget)
        {
            exact_proposal_fallback_jobs.erase(found);
            return;
        }

        ++job->completed_stages;
        const auto cursor_before = job->tail_target_cursor;
        const auto attempts_before = job->total_send_attempts;
        static_cast<void>(broadcast_exact_proposal_repair_tail(*job));
        job->dispatching = false;
        if ((job->tail_target_cursor == cursor_before &&
             job->total_send_attempts == attempts_before) ||
            job->tail_target_cursor >= job->tail_targets.size() ||
            job->total_send_attempts >= job->total_attempt_budget)
        {
            exact_proposal_fallback_jobs.erase(found);
            return;
        }

        try
        {
            auto cancellation = aggregation_scheduler->schedule_after(
                job->stage_interval,
                [access = exact_runtime_access,
                 job]() {
                    auto runtime = access->acquire();
                    if (!runtime.has_value())
                        return;
                    runtime->owner().dispatch_exact_proposal_repair_tail(
                        job);
                });
            if (!cancellation)
            {
                exact_proposal_fallback_jobs.erase(found);
                return;
            }
            job->cancellation = std::move(cancellation);
        }
        catch (...)
        {
            exact_proposal_fallback_jobs.erase(found);
        }
    }

    bool HotStuffBase::broadcast_exact_proposal_fallback(
        const ProposalContextLease &lease,
        std::uint64_t epoch_generation,
        const Proposal &proposal,
        std::size_t &target_cursor,
        std::size_t &total_send_attempts,
        std::vector<ReplicaID> &attempted_targets,
        const bool &quorum_observed,
        std::size_t total_attempt_budget,
        std::size_t maximum_attempts,
        std::uint32_t repair_stage)
    {
        if (proposal.key() != lease.key() ||
            lease.tree().root != get_id() ||
            lease.tree().parent.has_value())
            return false;
        std::size_t send_attempts = 0;
        std::size_t send_successes = 0;
        try
        {
            bytearray_t encoded;
            if (is_adaptive_epoch_mode(epoch_protocol_mode))
            {
                const MsgPropose native(proposal);
                encoded = adaptive_epoch_consensus_message(
                    lease.key().configuration,
                    epoch_generation,
                    epoch_protocol_mode ==
                            EpochProtocolMode::adaptive_v3
                        ? EpochConsensusWireKind::proposal_repair
                        : EpochConsensusWireKind::proposal,
                    lease.key(),
                    get_id(),
                    get_id(),
                    static_cast<bytearray_t>(native.serialized),
                    epoch_wire_limits,
                    epoch_protocol_mode);
                if (encoded.empty())
                    return false;
            }

            bool enqueued = false;
            std::size_t skipped_verified = 0;
            const auto snapshot =
                proposal_contexts->snapshot(proposal.key());
            if (!snapshot.has_value())
                return false;
            const auto &targets = lease.tree().assigned_subtree;
            while (target_cursor < targets.size() &&
                   !quorum_observed &&
                   send_attempts < maximum_attempts &&
                   total_send_attempts < total_attempt_budget)
            {
                const auto member = targets[target_cursor++];
                if (member == get_id())
                    continue;
                // A verified signature proves that this replica already
                // received this exact proposal. Repair only the missing
                // recipients instead of rebroadcasting to the full tree.
                if (snapshot->verified_signers.count(member) != 0)
                {
                    ++skipped_verified;
                    continue;
                }
                const auto peer = config.get_peer_id(member);
                if (peer.is_null())
                    continue;
                attempted_targets.push_back(member);
                ++send_attempts;
                ++total_send_attempts;
                const bool sent = is_adaptive_epoch_mode(epoch_protocol_mode)
                    ? pn.send_msg(
                          MsgPropose(DataStream(encoded)), peer)
                    : pn.send_msg(MsgPropose(proposal), peer);
                if (sent)
                    ++send_successes;
                HOTSTUFF_LOG_INFO(
                    "KAURI_PROPOSAL_BROADCAST "
                    "stage=fallback_target_result root=%u target=%u "
                    "epoch=%u tree=%u block=%s stage=%u enqueued=%u",
                    static_cast<unsigned>(get_id()),
                    static_cast<unsigned>(member),
                    lease.key().configuration.epoch_number,
                    lease.key().configuration.tree_id,
                    lease.key().block_hash.to_hex().c_str(),
                    repair_stage,
                    static_cast<unsigned>(sent));
                enqueued = sent || enqueued;
            }
            HOTSTUFF_LOG_INFO(
                "KAURI_PROPOSAL_BROADCAST "
                "stage=fallback_dispatch_summary outcome=complete "
                "root=%u epoch=%u tree=%u block=%s stage=%u attempts=%zu "
                "successes=%zu skipped_verified=%zu cursor=%zu total=%zu "
                "budget=%zu enqueued=%u",
                static_cast<unsigned>(get_id()),
                lease.key().configuration.epoch_number,
                lease.key().configuration.tree_id,
                lease.key().block_hash.to_hex().c_str(),
                repair_stage,
                send_attempts,
                send_successes,
                skipped_verified,
                target_cursor,
                total_send_attempts,
                total_attempt_budget,
                static_cast<unsigned>(enqueued));
            return enqueued;
        }
        catch (...)
        {
            HOTSTUFF_LOG_INFO(
                "KAURI_PROPOSAL_BROADCAST "
                "stage=fallback_dispatch_summary outcome=exception "
                "root=%u epoch=%u tree=%u block=%s stage=%u attempts=%zu "
                "successes=%zu cursor=%zu total=%zu budget=%zu enqueued=0",
                static_cast<unsigned>(get_id()),
                lease.key().configuration.epoch_number,
                lease.key().configuration.tree_id,
                lease.key().block_hash.to_hex().c_str(),
                repair_stage,
                send_attempts,
                send_successes,
                target_cursor,
                total_send_attempts,
                total_attempt_budget);
            return false;
        }
    }

    bool HotStuffBase::reserve_exact_proposal_pre_quorum_refresh(
        const ProposalContextLease &lease,
        ExactProposalFallbackJob &job,
        std::size_t maximum_attempts)
    {
        if (job.proposal == nullptr || job.proposal->key() != lease.key() ||
            job.key != lease.key() || lease.tree().root != get_id() ||
            lease.tree().parent.has_value() ||
            !job.pre_quorum_retry_armed ||
            maximum_attempts == 0)
            return false;
        std::size_t refresh_attempts = 0;
        try
        {
            std::size_t skipped_confirmed = 0;
            const auto snapshot = proposal_contexts->snapshot(job.key);
            if (!snapshot.has_value())
                return false;
            if (snapshot->verified_signers.size() >= job.global_quorum)
                return false;
            const auto fresh_missing_votes =
                job.global_quorum - snapshot->verified_signers.size();
            maximum_attempts = std::min(
                maximum_attempts, fresh_missing_votes);
            while (job.pre_quorum_retry_cursor <
                       job.pre_quorum_retry_targets.size() &&
                   !job.quorum_observed &&
                   refresh_attempts < maximum_attempts &&
                   refresh_attempts < job.stage_target_limit &&
                   job.total_send_attempts < job.total_attempt_budget)
            {
                const auto member = job.pre_quorum_retry_targets[
                    job.pre_quorum_retry_cursor++];
                if (member == get_id() ||
                    snapshot->verified_signers.count(member) != 0)
                {
                    ++skipped_confirmed;
                    continue;
                }
                const auto peer = config.get_peer_id(member);
                if (peer.is_null())
                    continue;
                const auto current_connection = pn.get_peer_conn(peer);
                const bool reconnect_request_required =
                    current_connection == nullptr;
                const bool reconnect_in_progress =
                    current_connection != nullptr &&
                    current_connection->is_terminated();
                job.pending_pre_quorum_refresh_batch.push_back({member});
                ++refresh_attempts;
                // Kauri configures every peer with Salticidae's infinite
                // retry policy at startup.  Preserve a healthy path and join
                // an existing reconnect instead of restarting it.  The
                // PeerId send below uses the replacement if ready; otherwise
                // finish_handshake migrates the terminated connection's
                // buffered bytes into that replacement.
                if (reconnect_request_required)
                    pn.conn_peer(peer);
                const auto *refresh_outcome =
                    reconnect_request_required
                        ? "requested"
                        : reconnect_in_progress
                            ? "joined"
                            : "live_preserved";
                HOTSTUFF_LOG_INFO(
                    "KAURI_PROPOSAL_BROADCAST "
                    "stage=pre_qc_refresh_target_result root=%u target=%u "
                    "epoch=%u tree=%u block=%s stage=%u refresh=%s "
                    "reconnect_request_required=%u "
                    "reconnect_in_progress=%u reserved=%zu total=%zu "
                    "budget=%zu",
                    static_cast<unsigned>(get_id()),
                    static_cast<unsigned>(member),
                    job.key.configuration.epoch_number,
                    job.key.configuration.tree_id,
                    job.key.block_hash.to_hex().c_str(),
                    job.completed_stages,
                    refresh_outcome,
                    static_cast<unsigned>(reconnect_request_required),
                    static_cast<unsigned>(reconnect_in_progress),
                    refresh_attempts,
                    job.total_send_attempts,
                    job.total_attempt_budget);
            }
            HOTSTUFF_LOG_INFO(
                "KAURI_PROPOSAL_BROADCAST "
                "stage=pre_qc_refresh_dispatch_summary outcome=complete "
                "root=%u epoch=%u tree=%u block=%s stage=%u "
                "reserved=%zu skipped_confirmed=%zu cursor=%zu total=%zu "
                "budget=%zu pending=%zu",
                static_cast<unsigned>(get_id()),
                job.key.configuration.epoch_number,
                job.key.configuration.tree_id,
                job.key.block_hash.to_hex().c_str(),
                job.completed_stages,
                refresh_attempts,
                skipped_confirmed,
                job.pre_quorum_retry_cursor,
                job.total_send_attempts,
                job.total_attempt_budget,
                job.pending_pre_quorum_refresh_batch.size());
            return !job.pending_pre_quorum_refresh_batch.empty();
        }
        catch (...)
        {
            HOTSTUFF_LOG_INFO(
                "KAURI_PROPOSAL_BROADCAST "
                "stage=pre_qc_refresh_dispatch_summary outcome=exception "
                "root=%u epoch=%u tree=%u block=%s stage=%u reserved=%zu "
                "cursor=%zu total=%zu budget=%zu pending=%zu",
                static_cast<unsigned>(get_id()),
                job.key.configuration.epoch_number,
                job.key.configuration.tree_id,
                job.key.block_hash.to_hex().c_str(),
                job.completed_stages,
                refresh_attempts,
                job.pre_quorum_retry_cursor,
                job.total_send_attempts,
                job.total_attempt_budget,
                job.pending_pre_quorum_refresh_batch.size());
            return false;
        }
    }

    bool HotStuffBase::broadcast_exact_proposal_pre_quorum_retry(
        const ProposalContextLease &lease,
        ExactProposalFallbackJob &job)
    {
        if (job.proposal == nullptr || job.proposal->key() != lease.key() ||
            job.key != lease.key() || lease.tree().root != get_id() ||
            lease.tree().parent.has_value() ||
            !job.pre_quorum_retry_armed ||
            job.pending_pre_quorum_refresh_batch.empty())
            return false;
        std::size_t send_attempts = 0;
        std::size_t send_dispatches = 0;
        std::size_t reconnect_path_dispatches = 0;
        try
        {
            bytearray_t encoded;
            if (is_adaptive_epoch_mode(epoch_protocol_mode))
            {
                const MsgPropose native(*job.proposal);
                encoded = adaptive_epoch_consensus_message(
                    job.key.configuration,
                    job.epoch_generation,
                    epoch_protocol_mode ==
                            EpochProtocolMode::adaptive_v3
                        ? EpochConsensusWireKind::proposal_repair
                        : EpochConsensusWireKind::proposal,
                    job.key,
                    get_id(),
                    get_id(),
                    static_cast<bytearray_t>(native.serialized),
                    epoch_wire_limits,
                    epoch_protocol_mode);
                if (encoded.empty())
                {
                    job.pending_pre_quorum_refresh_batch.clear();
                    return false;
                }
            }

            bool dispatched = false;
            std::size_t skipped_confirmed = 0;
            const auto snapshot = proposal_contexts->snapshot(job.key);
            if (!snapshot.has_value() ||
                snapshot->verified_signers.size() >= job.global_quorum)
                return false;
            auto pending_batch =
                std::move(job.pending_pre_quorum_refresh_batch);
            job.pending_pre_quorum_refresh_batch.clear();
            for (const auto &pending : pending_batch)
            {
                if (snapshot->verified_signers.count(pending.target) != 0)
                {
                    ++skipped_confirmed;
                    continue;
                }
                const auto peer = config.get_peer_id(pending.target);
                if (peer.is_null())
                    continue;
                const auto current_connection = pn.get_peer_conn(peer);
                const bool reconnect_path_observed =
                    current_connection == nullptr ||
                    current_connection->is_terminated();
                if (job.total_send_attempts >= job.total_attempt_budget)
                {
                    HOTSTUFF_LOG_INFO(
                        "KAURI_PROPOSAL_BROADCAST "
                        "stage=pre_qc_retry_target_result outcome="
                        "budget_exhausted_no_dispatch root=%u target=%u "
                        "epoch=%u tree=%u block=%s stage=%u total=%zu "
                        "budget=%zu",
                        static_cast<unsigned>(get_id()),
                        static_cast<unsigned>(pending.target),
                        job.key.configuration.epoch_number,
                        job.key.configuration.tree_id,
                        job.key.block_hash.to_hex().c_str(),
                        job.completed_stages,
                        job.total_send_attempts,
                        job.total_attempt_budget);
                    continue;
                }
                const bool sent =
                    is_adaptive_epoch_mode(epoch_protocol_mode)
                        ? pn.send_msg(
                              MsgPropose(DataStream(encoded)), peer)
                        : pn.send_msg(MsgPropose(*job.proposal), peer);
                ++send_attempts;
                ++job.total_send_attempts;
                bool retained = false;
                if (sent)
                {
                    ++send_dispatches;
                    job.attempted_targets.push_back(pending.target);
                    if (reconnect_path_observed)
                        ++reconnect_path_dispatches;
                    dispatched = true;
                }
                else
                {
                    if (job.total_send_attempts < job.total_attempt_budget)
                    {
                        job.pending_pre_quorum_refresh_batch.push_back(
                            pending);
                        retained = true;
                    }
                }
                HOTSTUFF_LOG_INFO(
                    "KAURI_PROPOSAL_BROADCAST "
                    "stage=pre_qc_retry_target_result outcome=%s "
                    "root=%u target=%u epoch=%u tree=%u block=%s "
                    "stage=%u reconnect_path_observed=%u enqueued=%u "
                    "retained=%u total=%zu budget=%zu",
                    sent
                        ? reconnect_path_observed
                            ? "reconnect_path_dispatch"
                            : "live_connection_dispatch"
                        : retained
                            ? "enqueue_failed_retained"
                            : "enqueue_failed_budget_exhausted",
                    static_cast<unsigned>(get_id()),
                    static_cast<unsigned>(pending.target),
                    job.key.configuration.epoch_number,
                    job.key.configuration.tree_id,
                    job.key.block_hash.to_hex().c_str(),
                    job.completed_stages,
                    static_cast<unsigned>(reconnect_path_observed),
                    static_cast<unsigned>(sent),
                    static_cast<unsigned>(retained),
                    job.total_send_attempts,
                    job.total_attempt_budget);
            }
            HOTSTUFF_LOG_INFO(
                "KAURI_PROPOSAL_BROADCAST "
                "stage=pre_qc_retry_dispatch_summary outcome=complete "
                "root=%u epoch=%u tree=%u block=%s stage=%u attempts=%zu "
                "dispatches=%zu reconnect_path_dispatches=%zu "
                "skipped_confirmed=%zu cursor=%zu total=%zu "
                "budget=%zu pending=%zu dispatched=%u",
                static_cast<unsigned>(get_id()),
                job.key.configuration.epoch_number,
                job.key.configuration.tree_id,
                job.key.block_hash.to_hex().c_str(),
                job.completed_stages,
                send_attempts,
                send_dispatches,
                reconnect_path_dispatches,
                skipped_confirmed,
                job.pre_quorum_retry_cursor,
                job.total_send_attempts,
                job.total_attempt_budget,
                job.pending_pre_quorum_refresh_batch.size(),
                static_cast<unsigned>(dispatched));
            return dispatched;
        }
        catch (...)
        {
            job.pending_pre_quorum_refresh_batch.clear();
            HOTSTUFF_LOG_INFO(
                "KAURI_PROPOSAL_BROADCAST "
                "stage=pre_qc_retry_dispatch_summary outcome=exception "
                "root=%u epoch=%u tree=%u block=%s stage=%u attempts=%zu "
                "dispatches=%zu reconnect_path_dispatches=%zu cursor=%zu "
                "total=%zu budget=%zu dispatched=%u",
                static_cast<unsigned>(get_id()),
                job.key.configuration.epoch_number,
                job.key.configuration.tree_id,
                job.key.block_hash.to_hex().c_str(),
                job.completed_stages,
                send_attempts,
                send_dispatches,
                reconnect_path_dispatches,
                job.pre_quorum_retry_cursor,
                job.total_send_attempts,
                job.total_attempt_budget,
                static_cast<unsigned>(send_dispatches != 0));
            return false;
        }
    }

    bool HotStuffBase::broadcast_exact_proposal_repair_tail(
        ExactProposalFallbackJob &job)
    {
        if (job.proposal == nullptr || job.proposal->key() != job.key ||
            !job.tail_armed)
            return false;
        std::size_t send_attempts = 0;
        std::size_t send_successes = 0;
        try
        {
            bytearray_t encoded;
            if (is_adaptive_epoch_mode(epoch_protocol_mode))
            {
                const MsgPropose native(*job.proposal);
                encoded = adaptive_epoch_consensus_message(
                    job.key.configuration,
                    job.epoch_generation,
                    epoch_protocol_mode ==
                            EpochProtocolMode::adaptive_v3
                        ? EpochConsensusWireKind::proposal_repair
                        : EpochConsensusWireKind::proposal,
                    job.key,
                    get_id(),
                    get_id(),
                    static_cast<bytearray_t>(native.serialized),
                    epoch_wire_limits,
                    epoch_protocol_mode);
                if (encoded.empty())
                    return false;
            }

            bool enqueued = false;
            std::size_t skipped_confirmed = 0;
            while (job.tail_target_cursor < job.tail_targets.size() &&
                   send_attempts < job.stage_target_limit &&
                   job.total_send_attempts < job.total_attempt_budget)
            {
                const auto member =
                    job.tail_targets[job.tail_target_cursor++];
                if (member == get_id() ||
                    job.confirmed_signers.count(member) != 0)
                {
                    ++skipped_confirmed;
                    continue;
                }
                const auto peer = config.get_peer_id(member);
                if (peer.is_null())
                    continue;
                ++send_attempts;
                ++job.total_send_attempts;
                const bool sent = is_adaptive_epoch_mode(epoch_protocol_mode)
                    ? pn.send_msg(
                          MsgPropose(DataStream(encoded)), peer)
                    : pn.send_msg(MsgPropose(*job.proposal), peer);
                if (sent)
                    ++send_successes;
                HOTSTUFF_LOG_INFO(
                    "KAURI_PROPOSAL_BROADCAST "
                    "stage=repair_tail_target_result root=%u target=%u "
                    "epoch=%u tree=%u block=%s stage=%u enqueued=%u",
                    static_cast<unsigned>(get_id()),
                    static_cast<unsigned>(member),
                    job.key.configuration.epoch_number,
                    job.key.configuration.tree_id,
                    job.key.block_hash.to_hex().c_str(),
                    job.completed_stages,
                    static_cast<unsigned>(sent));
                enqueued = sent || enqueued;
            }
            HOTSTUFF_LOG_INFO(
                "KAURI_PROPOSAL_BROADCAST "
                "stage=repair_tail_dispatch_summary outcome=complete "
                "root=%u epoch=%u tree=%u block=%s stage=%u attempts=%zu "
                "successes=%zu skipped_confirmed=%zu cursor=%zu total=%zu "
                "budget=%zu enqueued=%u",
                static_cast<unsigned>(get_id()),
                job.key.configuration.epoch_number,
                job.key.configuration.tree_id,
                job.key.block_hash.to_hex().c_str(),
                job.completed_stages,
                send_attempts,
                send_successes,
                skipped_confirmed,
                job.tail_target_cursor,
                job.total_send_attempts,
                job.total_attempt_budget,
                static_cast<unsigned>(enqueued));
            return enqueued;
        }
        catch (...)
        {
            HOTSTUFF_LOG_INFO(
                "KAURI_PROPOSAL_BROADCAST "
                "stage=repair_tail_dispatch_summary outcome=exception "
                "root=%u epoch=%u tree=%u block=%s stage=%u attempts=%zu "
                "successes=%zu cursor=%zu total=%zu budget=%zu enqueued=0",
                static_cast<unsigned>(get_id()),
                job.key.configuration.epoch_number,
                job.key.configuration.tree_id,
                job.key.block_hash.to_hex().c_str(),
                job.completed_stages,
                send_attempts,
                send_successes,
                job.tail_target_cursor,
                job.total_send_attempts,
                job.total_attempt_budget);
            return false;
        }
    }

    void HotStuffBase::discard_exact_fallbacks(
        const ProposalKey &key,
        bool preserve_scheduled_vote_fallback)
    {
        std::vector<AggregationScheduler::Cancellation> cancellations;
        const auto vote = exact_vote_fallback_jobs.find(key);
        if (!preserve_scheduled_vote_fallback &&
            vote != exact_vote_fallback_jobs.end())
        {
            if (vote->second->cancellation)
                cancellations.push_back(
                    std::move(vote->second->cancellation));
            exact_vote_fallback_jobs.erase(vote);
        }
        const auto proposal = exact_proposal_fallback_jobs.find(key);
        // Only a verified root quorum can arm this tail. Preserve that
        // bounded dissemination job across terminal context/commit cleanup;
        // generation guards and shutdown still cancel it.
        if (proposal != exact_proposal_fallback_jobs.end() &&
            !proposal->second->tail_armed)
        {
            if (proposal->second->cancellation)
                cancellations.push_back(
                    std::move(proposal->second->cancellation));
            exact_proposal_fallback_jobs.erase(proposal);
        }
        for (auto &cancel : cancellations)
            cancel();
    }

    void HotStuffBase::discard_exact_fallbacks_before_epoch(
        std::uint32_t first_live_epoch) noexcept
    {
        std::vector<AggregationScheduler::Cancellation> cancellations;
        for (auto job = exact_vote_fallback_jobs.begin();
             job != exact_vote_fallback_jobs.end();)
        {
            if (job->first.configuration.epoch_number >= first_live_epoch)
            {
                ++job;
                continue;
            }
            if (job->second->cancellation)
                cancellations.push_back(
                    std::move(job->second->cancellation));
            job = exact_vote_fallback_jobs.erase(job);
        }
        for (auto job = exact_proposal_fallback_jobs.begin();
             job != exact_proposal_fallback_jobs.end();)
        {
            if (job->first.configuration.epoch_number >= first_live_epoch)
            {
                ++job;
                continue;
            }
            if (job->second->cancellation)
                cancellations.push_back(
                    std::move(job->second->cancellation));
            job = exact_proposal_fallback_jobs.erase(job);
        }
        for (auto ingress = authenticated_proposal_ingress.begin();
             ingress != authenticated_proposal_ingress.end();)
        {
            if (ingress->first.configuration.epoch_number >=
                first_live_epoch)
            {
                ++ingress;
                continue;
            }
            ingress = authenticated_proposal_ingress.erase(ingress);
        }
        for (auto arm =
                 successful_response_attempt_arm_provenance.begin();
             arm != successful_response_attempt_arm_provenance.end();)
        {
            if (arm->first.configuration.epoch_number >= first_live_epoch)
            {
                ++arm;
                continue;
            }
            arm = successful_response_attempt_arm_provenance.erase(arm);
        }
        for (auto failure = response_attempt_arm_failure_markers.begin();
             failure != response_attempt_arm_failure_markers.end();)
        {
            if (failure->configuration.epoch_number >= first_live_epoch)
            {
                ++failure;
                continue;
            }
            failure = response_attempt_arm_failure_markers.erase(failure);
        }
        for (auto &cancel : cancellations)
            try
            {
                cancel();
            }
            catch (...)
            {}
    }

    void HotStuffBase::cancel_all_exact_fallbacks() noexcept
    {
        std::vector<AggregationScheduler::Cancellation> cancellations;
        for (auto &entry : exact_vote_fallback_jobs)
            if (entry.second->cancellation)
                cancellations.push_back(
                    std::move(entry.second->cancellation));
        for (auto &entry : exact_proposal_fallback_jobs)
            if (entry.second->cancellation)
                cancellations.push_back(
                    std::move(entry.second->cancellation));
        exact_vote_fallback_jobs.clear();
        exact_root_repair_deliveries.clear();
        authenticated_proposal_ingress.clear();
        retained_commit_event_identities.clear();
        successful_response_attempt_arm_provenance.clear();
        response_attempt_arm_failure_markers.clear();
        exact_proposal_fallback_jobs.clear();
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

        if (adaptive_v2_response_evidence != nullptr)
        {
            const auto recorded =
                adaptive_v2_response_evidence->record_timeouts(
                    lease.key(),
                    missing,
                    adaptive_evidence_monotonic_now_ns());
            const auto diagnostics =
                adaptive_v2_response_evidence->diagnostics();
            HOTSTUFF_LOG_INFO(
                "[EVIDENCE] Timeout bridge epoch=%u tree=%u block=%.10s "
                "requested=%zu recorded=%zu missing_handles=%llu "
                "ineligible=%llu tracker_rejections=%llu "
                "retention_failures=%llu late_reservation_failures=%llu "
                "exceptions=%llu healthy=%u",
                lease.key().configuration.epoch_number,
                lease.key().configuration.tree_id,
                lease.key().block_hash.to_hex().c_str(),
                missing.size(),
                recorded,
                static_cast<unsigned long long>(
                    diagnostics.timeout_missing_handles),
                static_cast<unsigned long long>(
                    diagnostics.timeout_ineligible_attempts),
                static_cast<unsigned long long>(
                    diagnostics.timeout_tracker_rejections),
                static_cast<unsigned long long>(
                    diagnostics.retention_capacity_failures),
                static_cast<unsigned long long>(
                    diagnostics.late_compensation_capacity_failures),
                static_cast<unsigned long long>(
                    diagnostics.timeout_exceptions),
                diagnostics.healthy ? 1U : 0U);
        }

        // Adaptive modes emit only exact, locally derived response facts. The
        // legacy timeout message has no authenticated manager-ingress seam.
        if (is_adaptive_epoch_mode(epoch_protocol_mode))
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

    void HotStuffBase::emit_fault_contribution_opportunity(
        const ExperimentOmissionMarker &marker) noexcept
    {
        if (audit_event_emitter == nullptr || marker.actor != get_id() ||
            marker.action == ExperimentOmissionAction::capacity_exhausted ||
            marker.physical_role == ExperimentReplicaRole::root ||
            !marker.view_generation.has_value() ||
            !marker.physical_parent.has_value() ||
            !marker.authenticated_proposal_source_replica.has_value() ||
            marker.authenticated_proposal_source_replica !=
                marker.physical_parent ||
            !marker.expected_message_type.has_value() ||
            marker.fault_mode !=
                "tiered_persistent_responsive_omission_v2")
            return;
        try
        {
            FaultContributionOpportunityStructuredEvent event;
            event.actor = marker.actor;
            event.proposal = marker.proposal;
            event.view_generation = *marker.view_generation;
            event.physical_role = marker.physical_role;
            event.parent_replica = *marker.physical_parent;
            event.authenticated_proposal_source_replica =
                *marker.authenticated_proposal_source_replica;
            event.expected_message_type =
                *marker.expected_message_type;
            event.cohort = marker.cohort;
            event.diagnostic_window = marker.diagnostic_window;
            event.window_start_monotonic_ns =
                marker.window_start_monotonic_ns;
            event.window_end_monotonic_ns =
                marker.window_end_monotonic_ns;
            event.decision_monotonic_ns = marker.monotonic_ns;
            event.contribution_ordinal = marker.contribution_ordinal;
            event.role_contribution_ordinal =
                marker.role_contribution_ordinal;
            event.scheduled_action = marker.action;
            event.responsive_omission_period =
                marker.responsive_omission_period;
            event.fault_threshold = marker.fault_threshold;
            event.hard_actor_count = marker.hard_actor_count;
            event.responsive_degraded_actor_count =
                marker.responsive_degraded_actor_count;
            event.fault_mode = marker.fault_mode;
            audit_event_emitter->emit_audit(
                AuditStructuredEventPayload{std::move(event)});
        }
        catch (...)
        {
            // Evidence failure invalidates the run, never protocol behavior.
        }
    }

    void HotStuffBase::emit_committed_block_event(
        const block_t &blk,
        const std::optional<ProposalKey> &committed_key,
        const std::optional<std::uint64_t> &view_generation,
        std::uint64_t commit_batch_index,
        const std::optional<std::uint64_t> &
            reporter_local_commit_monotonic_ns) noexcept
    {
        if (structured_event_emitter == nullptr || blk == nullptr ||
            !committed_key.has_value() || !view_generation.has_value())
            return;
        try
        {
            const auto &key = *committed_key;
            if (key.block_hash != blk->get_hash())
                return;

            std::optional<uint256_t> parent_hash;
            const auto &parent_hashes = blk->get_parent_hashes();
            if (!parent_hashes.empty())
                parent_hash = parent_hashes.front();
            structured_event_emitter->emit(
                StructuredEventPayload{CommitStructuredEvent{
                    blk->get_height(),
                    blk->get_hash(),
                    parent_hash,
                    static_cast<std::uint64_t>(blk->get_cmds().size()),
                    key,
                    view_generation,
                    commit_batch_index,
                    reporter_local_commit_monotonic_ns}});
        }
        catch (...)
        {
            // Evidence failure invalidates the run, never protocol behavior.
        }
    }

    void HotStuffBase::emit_commit_observed_event(
        const block_t &blk,
        std::uint64_t commit_batch_index) noexcept
    {
        if (structured_event_emitter == nullptr || blk == nullptr)
            return;
        try
        {
            std::optional<uint256_t> parent_hash;
            const auto &parent_hashes = blk->get_parent_hashes();
            if (!parent_hashes.empty())
                parent_hash = parent_hashes.front();
            structured_event_emitter->emit(
                StructuredEventPayload{CommitObservedStructuredEvent{
                    blk->get_height(),
                    blk->get_hash(),
                    parent_hash,
                    static_cast<std::uint64_t>(blk->get_cmds().size()),
                    commit_batch_index}});
        }
        catch (...)
        {
            // Evidence failure invalidates the run, never protocol behavior.
        }
    }

    void HotStuffBase::emit_commit_identity_unavailable_event(
        const block_t &blk,
        std::uint64_t commit_batch_index) noexcept
    {
        if (structured_event_emitter == nullptr || blk == nullptr)
            return;
        try
        {
            std::optional<uint256_t> parent_hash;
            const auto &parent_hashes = blk->get_parent_hashes();
            if (!parent_hashes.empty())
                parent_hash = parent_hashes.front();
            structured_event_emitter->emit(
                StructuredEventPayload{
                    CommitIdentityUnavailableStructuredEvent{
                        blk->get_height(),
                        blk->get_hash(),
                        parent_hash,
                        static_cast<std::uint64_t>(
                            blk->get_cmds().size()),
                        commit_batch_index,
                        CommitIdentityUnavailableReason::
                            no_authenticated_exact_identity_source,
                        false}});
        }
        catch (...)
        {
            // Evidence failure invalidates the run, never protocol behavior.
        }
    }

    void HotStuffBase::emit_commit_identity_witness_event(
        const block_t &blk,
        const ProposalKey &key,
        std::uint64_t view_generation,
        std::uint64_t commit_batch_index) noexcept
    {
        if (structured_event_emitter == nullptr || blk == nullptr ||
            key.block_hash != blk->get_hash() || view_generation == 0)
            return;
        try
        {
            std::optional<uint256_t> parent_hash;
            const auto &parent_hashes = blk->get_parent_hashes();
            if (!parent_hashes.empty())
                parent_hash = parent_hashes.front();
            structured_event_emitter->emit(
                StructuredEventPayload{
                    CommitIdentityWitnessStructuredEvent{
                        blk->get_height(),
                        blk->get_hash(),
                        parent_hash,
                        static_cast<std::uint64_t>(
                            blk->get_cmds().size()),
                        key,
                        view_generation,
                        commit_batch_index}});
        }
        catch (...)
        {
            // Evidence failure invalidates the run, never protocol behavior.
        }
    }

    void HotStuffBase::emit_epoch_command_committed_event(
        const block_t &blk,
        const AuthorizedEpochChange &command,
        const ActivationRecord &record) noexcept
    {
        if (audit_event_emitter == nullptr || blk == nullptr)
            return;
        try
        {
            const auto payload_digest =
                epoch_change_payload_digest(command.payload);
            if (record.command_commit_height != blk->get_height() ||
                record.payload_digest != payload_digest ||
                record.predecessor_epoch_digest !=
                    command.payload.predecessor_epoch_digest ||
                record.successor_epoch_number !=
                    command.payload.successor_epoch_number ||
                record.successor_epoch_digest !=
                    command.payload.successor_epoch_digest ||
                record.activation_delay_blocks !=
                    command.payload.activation_delay_blocks)
                return;

            audit_event_emitter->emit_audit(
                AuditStructuredEventPayload{
                    EpochCommandCommittedStructuredEvent{
                        blk->get_height(),
                        blk->get_hash(),
                        record.predecessor_epoch_number,
                        record.predecessor_epoch_digest,
                        record.successor_epoch_number,
                        record.successor_epoch_digest,
                        record.payload_digest,
                        record.activation_delay_blocks,
                        record.activation_height}});
        }
        catch (...)
        {
            // Evidence failure invalidates the run, never protocol behavior.
        }
    }

    void HotStuffBase::emit_epoch_lifecycle_event(
        EpochLifecycleTransition transition,
        const ConfigurationId &configuration,
        std::uint64_t activation_height,
        std::optional<std::uint64_t> certificate_apply_height,
        std::optional<uint256_t> certificate_digest) noexcept
    {
        if (structured_event_emitter == nullptr)
            return;
        try
        {
            structured_event_emitter->emit(
                StructuredEventPayload{EpochLifecycleEvent{
                    transition,
                    configuration,
                    activation_height,
                    certificate_apply_height,
                    certificate_digest}});
        }
        catch (...)
        {
        }
    }

    void HotStuffBase::continue_exact_contribution(
        const ProposalContextLease &lease,
        ExactContributionKind kind,
        const ExactContributionEnvelope &contribution,
        std::uint64_t received_ns)
    {
        if (!proposal_contexts->revalidate(lease))
            return;

        bool accepted = false;
        auto aggregate_disposition =
            VerifiedAggregateCertificateDisposition::rejected;
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
            const bool descendant_root_fallback =
                lease.tree().local_replica == lease.tree().root &&
                !lease.tree().parent.has_value() &&
                lease.tree().child_subtrees.count(
                    contribution.authenticated_sender) == 0;
            accepted = descendant_root_fallback
                ? proposal_contexts->record_verified_root_fallback_part(
                      lease,
                      config,
                      contribution.authenticated_sender,
                      *contribution.claimed_voter,
                      *vote->cert,
                      std::move(forwarding_candidate))
                : proposal_contexts->record_verified_direct_part(
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
            aggregate_disposition = proposal_contexts
                ->record_verified_aggregate_certificate_with_disposition(
                    lease,
                    contribution.authenticated_sender,
                    *relay->cert);
            accepted = aggregate_disposition ==
                VerifiedAggregateCertificateDisposition::accepted;
        }
        if (!accepted)
        {
            if (kind == ExactContributionKind::aggregate_relay &&
                aggregate_disposition ==
                    VerifiedAggregateCertificateDisposition::redundant)
            {
                const auto signers =
                    contribution.aggregate_relay->cert->get_signers();
                const std::set<ReplicaID> contribution_signers(
                    signers.begin(), signers.end());
                const auto response_monotonic_ns =
                    adaptive_evidence_monotonic_now_ns();
                const auto false_timeout =
                    experiment_false_timeout_states.find(lease.key());
                const bool suppress_positive_observation =
                    false_timeout != experiment_false_timeout_states.end() &&
                    false_timeout->second.may_suppress(
                        contribution.authenticated_sender) &&
                    experiment_byzantine_adapter != nullptr &&
                    experiment_byzantine_adapter->on_verified_response(
                        ExperimentByzantineContext{
                            lease.key(), experiment_diagnostic_window},
                        contribution.authenticated_sender);
                const bool response_fact_recorded =
                    adaptive_v2_response_evidence != nullptr &&
                    !suppress_positive_observation &&
                    adaptive_v2_response_evidence
                        ->record_verified_response(
                            lease.key(),
                            contribution.authenticated_sender,
                            ExpectedMessageType::aggregate_relay,
                            contribution_signers,
                            response_monotonic_ns);
                HOTSTUFF_LOG_INFO(
                    "KAURI_RESPONSE_EVIDENCE "
                    "disposition=verified_redundant_aggregate_evidence_only "
                    "consensus_accepted=0 response_fact_recorded=%u "
                    "positive_suppressed=%u "
                    "reporter=%u child=%u epoch=%u tree=%u "
                    "epoch_digest=%s block=%s response_monotonic_ns=%llu",
                    response_fact_recorded ? 1U : 0U,
                    suppress_positive_observation ? 1U : 0U,
                    static_cast<unsigned>(get_id()),
                    static_cast<unsigned>(
                        contribution.authenticated_sender),
                    lease.key().configuration.epoch_number,
                    lease.key().configuration.tree_id,
                    lease.key().configuration.epoch_digest.to_hex().c_str(),
                    lease.key().block_hash.to_hex().c_str(),
                    static_cast<unsigned long long>(response_monotonic_ns));
            }
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

        synchronize_experiment_post_qc_audit(
            lease,
            contribution.authenticated_sender,
            received_ns == 0
                ? adaptive_evidence_monotonic_now_ns()
                : received_ns);

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
            const auto false_timeout =
                experiment_false_timeout_states.find(lease.key());
            const bool suppress_positive_observation =
                false_timeout != experiment_false_timeout_states.end() &&
                false_timeout->second.may_suppress(
                    contribution.authenticated_sender) &&
                experiment_byzantine_adapter != nullptr &&
                experiment_byzantine_adapter->on_verified_response(
                    ExperimentByzantineContext{
                        lease.key(),
                        experiment_diagnostic_window},
                    contribution.authenticated_sender);
            const bool emit_positive_suppression_marker =
                suppress_positive_observation &&
                experiment_byzantine_adapter
                    ->consume_false_report_positive_marker(
                        ExperimentByzantineContext{
                            lease.key(),
                            experiment_diagnostic_window},
                        contribution.authenticated_sender);
            if (adaptive_v2_response_evidence != nullptr &&
                !suppress_positive_observation)
            {
                const auto response_monotonic_ns =
                    adaptive_evidence_monotonic_now_ns();
                const auto response_message_type =
                    kind == ExactContributionKind::direct_vote
                        ? ExpectedMessageType::direct_vote
                        : ExpectedMessageType::aggregate_relay;
                const bool first_call_recorded =
                    adaptive_v2_response_evidence->record_verified_response(
                        lease.key(),
                        contribution.authenticated_sender,
                        response_message_type,
                        contribution_signers,
                        response_monotonic_ns);

                const auto child_subtree =
                    lease.tree().child_subtrees.find(
                        contribution.authenticated_sender);
                bool expected_probe_unconsumed = false;
                if (first_call_recorded &&
                    experiment_response_evidence_duplicate_probe ==
                        kExperimentResponseEvidenceDuplicateProbeMode &&
                    kind == ExactContributionKind::aggregate_relay &&
                    lease.key().configuration.epoch_number == 1 &&
                    response_monotonic_ns != 0 &&
                    response_monotonic_ns >=
                        experiment_response_evidence_duplicate_probe_window_end_ns &&
                    child_subtree != lease.tree().child_subtrees.end() &&
                    child_subtree->second.size() > 1 &&
                    experiment_byzantine_adapter != nullptr &&
                    experiment_byzantine_adapter
                        ->is_tiered_responsive_degraded_actor(
                            contribution.authenticated_sender) &&
                    experiment_response_evidence_duplicate_probe_consumed
                        .compare_exchange_strong(
                            expected_probe_unconsumed, true))
                {
                    const bool second_call_recorded =
                        adaptive_v2_response_evidence
                            ->record_verified_response(
                                lease.key(),
                                contribution.authenticated_sender,
                                response_message_type,
                                contribution_signers,
                                response_monotonic_ns);
                    HOTSTUFF_LOG_INFO(
                        "KAURI_EXPERIMENT response_duplicate_probe "
                        "mode=%s consensus_accepted=1 reporter=%u "
                        "child=%u epoch=%u tree=%u digest=%s block=%s "
                        "message_type=aggregate_relay "
                        "response_monotonic_ns=%llu "
                        "window_end_monotonic_ns=%llu "
                        "first_call_recorded=1 second_call_recorded=%u",
                        experiment_response_evidence_duplicate_probe.c_str(),
                        static_cast<unsigned>(get_id()),
                        static_cast<unsigned>(
                            contribution.authenticated_sender),
                        lease.key().configuration.epoch_number,
                        lease.key().configuration.tree_id,
                        lease.key()
                            .configuration.epoch_digest.to_hex().c_str(),
                        lease.key().block_hash.to_hex().c_str(),
                        static_cast<unsigned long long>(
                            response_monotonic_ns),
                        static_cast<unsigned long long>(
                            experiment_response_evidence_duplicate_probe_window_end_ns),
                        second_call_recorded ? 1U : 0U);
                }
            }
            if (emit_positive_suppression_marker)
            {
                const auto marker_monotonic_ns =
                    experiment_fault_marker_monotonic_now_ns();
                if (marker_monotonic_ns.has_value())
                    HOTSTUFF_LOG_INFO(
                        "KAURI_FAULT false_report_positive_suppressed "
                        "reporter=%u target=%u epoch=%u tree=%u block=%s "
                        "window=%s monotonic_ns=%llu",
                        get_id(),
                        contribution.authenticated_sender,
                        lease.key().configuration.epoch_number,
                        lease.key().configuration.tree_id,
                        lease.key().block_hash.to_hex().c_str(),
                        experiment_diagnostic_window.c_str(),
                        static_cast<unsigned long long>(
                            *marker_monotonic_ns));
                else
                    HOTSTUFF_LOG_WARN(
                        "KAURI_FAULT marker_skipped "
                        "marker=false_report_positive_suppressed "
                        "reporter=%u target=%u epoch=%u tree=%u block=%s "
                        "reason=event_clock_unavailable",
                        get_id(),
                        contribution.authenticated_sender,
                        lease.key().configuration.epoch_number,
                        lease.key().configuration.tree_id,
                        lease.key().block_hash.to_hex().c_str());
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

    namespace detail
    {
        namespace
        {
            bool is_delivered_first_parent_ancestor(
                const block_t &maybe_ancestor,
                const block_t &descendant)
            {
                if (maybe_ancestor == nullptr || descendant == nullptr ||
                    !maybe_ancestor->is_delivered() ||
                    !descendant->is_delivered() ||
                    maybe_ancestor->get_height() >=
                        descendant->get_height())
                    return false;

                block_t cursor = descendant;
                while (cursor != nullptr &&
                       cursor->get_height() >
                           maybe_ancestor->get_height())
                {
                    const auto &parents = cursor->get_parents();
                    if (parents.empty() || parents.front() == nullptr ||
                        !parents.front()->is_delivered() ||
                        parents.front()->get_height() >=
                            cursor->get_height())
                        return false;
                    cursor = parents.front();
                }
                return cursor == maybe_ancestor;
            }
        }

        bool consume_delivered_ancestor_piped_prefix(
            std::deque<uint256_t> &piped,
            std::deque<uint256_t> &ready,
            const block_t &candidate,
            EntityStorage &storage)
        {
            if (candidate == nullptr || !candidate->is_delivered())
                return false;

            const auto candidate_position = std::find(
                piped.begin(), piped.end(), candidate->get_hash());
            if (candidate_position == piped.end())
                return true;

            for (auto queued = piped.begin();
                 queued != candidate_position; ++queued)
            {
                const auto predecessor = storage.find_blk(*queued);
                if (!is_delivered_first_parent_ancestor(
                        predecessor, candidate))
                    return false;
            }

            std::vector<uint256_t> consumed(
                piped.begin(), std::next(candidate_position));
            piped.erase(piped.begin(), std::next(candidate_position));
            ready.erase(
                std::remove_if(
                    ready.begin(), ready.end(),
                    [&consumed](const uint256_t &hash)
                    {
                        return std::find(
                                   consumed.begin(), consumed.end(), hash) !=
                               consumed.end();
                    }),
                ready.end());
            return true;
        }

        std::optional<RootQcQueueBlockedStructuredEvent>
        make_root_qc_queue_blocked_event(
            const ProposalContextLease &candidate_lease,
            const ProposalContextLifecycle &proposal_contexts,
            const std::deque<uint256_t> &piped,
            const block_t &candidate,
            EntityStorage &storage,
            ReplicaID observer_replica)
        {
            if (candidate == nullptr || piped.size() < 2 ||
                candidate_lease.key().block_hash !=
                    candidate->get_hash() ||
                candidate_lease.tree().parent.has_value() ||
                candidate_lease.tree().root != observer_replica ||
                candidate_lease.tree().local_replica != observer_replica)
                return std::nullopt;

            const auto active =
                proposal_contexts.active_configuration();
            if (!active.has_value() ||
                *active != candidate_lease.key().configuration ||
                !proposal_contexts.revalidate(candidate_lease))
                return std::nullopt;

            const auto candidate_position = std::find(
                piped.begin(), piped.end(), candidate->get_hash());
            if (candidate_position != std::next(piped.begin()))
                return std::nullopt;

            const auto head_hash = piped.front();
            const auto &parent_hashes =
                candidate->get_parent_hashes();
            if (parent_hashes.empty() ||
                parent_hashes.front() != head_hash)
                return std::nullopt;

            const auto head = storage.find_blk(head_hash);
            if (head == nullptr || head->get_hash() != head_hash ||
                head->get_height() ==
                    std::numeric_limits<std::uint32_t>::max() ||
                candidate->get_height() != head->get_height() + 1)
                return std::nullopt;

            const ProposalKey head_key{
                candidate_lease.key().configuration, head_hash};
            const auto head_lease =
                proposal_contexts.acquire_open_context(head_key);
            if (!head_lease.has_value() ||
                head_lease->tree().parent.has_value() ||
                head_lease->tree().root != observer_replica ||
                head_lease->tree().local_replica != observer_replica ||
                head_lease->generation() >=
                    candidate_lease.generation())
                return std::nullopt;

            const auto head_snapshot =
                proposal_contexts.snapshot(head_key);
            const auto candidate_snapshot = proposal_contexts.snapshot(
                candidate_lease.key());
            const auto head_quorum =
                proposal_contexts.frozen_global_quorum(*head_lease);
            const auto candidate_quorum =
                proposal_contexts.frozen_global_quorum(candidate_lease);
            if (!head_snapshot.has_value() ||
                !candidate_snapshot.has_value() ||
                !head_quorum.has_value() ||
                !candidate_quorum.has_value() ||
                *head_quorum != *candidate_quorum ||
                *candidate_quorum == 0 ||
                head_snapshot->verified_signers.size() >=
                    *candidate_quorum ||
                candidate_snapshot->verified_signers.size() <
                    *candidate_quorum)
                return std::nullopt;

            RootQcQueueBlockedStructuredEvent event;
            event.configuration = candidate_lease.key().configuration;
            event.observer_replica = observer_replica;
            event.global_quorum = *candidate_quorum;
            event.queue_head_position = 0;
            event.queued_candidate_position = 1;
            event.queue_head_context_generation =
                head_lease->generation();
            event.queued_candidate_context_generation =
                candidate_lease.generation();
            event.queue_head_block_height = head->get_height();
            event.queue_head_block_hash = head_hash;
            event.queued_candidate_block_height =
                candidate->get_height();
            event.queued_candidate_block_hash =
                candidate->get_hash();
            event.queued_candidate_parent_hash =
                parent_hashes.front();
            event.queue_head_signer_count =
                head_snapshot->verified_signers.size();
            event.queued_candidate_signer_count =
                candidate_snapshot->verified_signers.size();
            event.queued_candidate_qc_ready = true;
            event.queued_candidate_qc_published = false;
            return event;
        }
    }

    void HotStuffBase::emit_root_qc_queue_blocked_event(
        const ProposalContextLease &candidate_lease,
        const block_t &candidate) noexcept
    {
        if (epoch_protocol_mode != EpochProtocolMode::adaptive_v2 ||
            audit_event_emitter == nullptr)
            return;
        try
        {
            auto event = detail::make_root_qc_queue_blocked_event(
                candidate_lease,
                *proposal_contexts,
                piped_queue,
                candidate,
                *storage,
                get_id());
            if (!event.has_value())
                return;
            audit_event_emitter->emit_audit(
                AuditStructuredEventPayload{std::move(*event)});
        }
        catch (...)
        {
            // Evidence failure invalidates the run, never protocol behavior.
        }
    }

    bool HotStuffBase::publish_exact_root_qc(
        const ProposalContextLease &lease,
        quorum_cert_bt final_qc)
    {
        if (!proposal_contexts->revalidate(lease) ||
            lease.tree().parent.has_value())
            return false;
        if (final_qc == nullptr ||
            final_qc->get_proposal_key() != lease.key())
            return false;

        auto block = storage->find_blk(lease.key().block_hash);
        if (block == nullptr || !block->delivered)
            return false;
        if (block->self_qc != nullptr &&
            block->self_qc->get_proposal_key() != lease.key())
            return false;

        block->self_qc = final_qc->clone();
        const auto piped = std::find(
            piped_queue.begin(), piped_queue.end(), lease.key().block_hash);
        const bool queued_behind_head =
            piped != piped_queue.end() && piped != piped_queue.begin();
        bool active_supersession = true;
        if (queued_behind_head)
        {
            const auto active_configuration =
                proposal_contexts->active_configuration();
            active_supersession =
                active_configuration.has_value() &&
                *active_configuration == lease.key().configuration;
        }
        const bool prefix_consumed =
            active_supersession &&
            detail::consume_delivered_ancestor_piped_prefix(
                piped_queue, rdy_queue, block, *storage);
        if (!prefix_consumed)
        {
            if (queued_behind_head && active_supersession)
                emit_root_qc_queue_blocked_event(lease, block);
            if (queued_behind_head &&
                std::find(
                    rdy_queue.begin(), rdy_queue.end(),
                    lease.key().block_hash) == rdy_queue.end())
                rdy_queue.push_back(lease.key().block_hash);
            return false;
        }

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
            activate_experiment_post_qc_audit_root(key);
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
            prepare_experiment_post_qc_audit_root(
                lease, *final_qc);
            if (proposal_contexts->claim_root_qc_progress(lease))
                pmaker->record_verified_progress(
                    lease.key().configuration,
                    LeaderProgressEvent::quorum_certificate);
            // Capture the verified signer set while the exact context is
            // still open. QC publication may synchronously commit or compact
            // it; the asynchronous tail owns only immutable proposal data.
            arm_exact_proposal_repair_tail(lease);
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
            activate_experiment_post_qc_audit_root(lease.key());
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

    void HotStuffBase::poison_response_attempt_arm_once(
        const ProposalKey &key,
        const char *reason) noexcept
    {
        try
        {
            successful_response_attempt_arm_provenance.erase(key);
            if (response_attempt_arm_failure_markers.insert(key).second)
            {
                HOTSTUFF_LOG_WARN(
                    "KAURI_EVIDENCE response_attempt_arm_marker_failed "
                    "reason=%s reporter=%u epoch=%u tree=%u "
                    "epoch_digest=%s block=%s",
                    reason == nullptr ? "unknown" : reason,
                    get_id(),
                    key.configuration.epoch_number,
                    key.configuration.tree_id,
                    key.configuration.epoch_digest.to_hex().c_str(),
                    key.block_hash.to_hex().c_str());
            }
        }
        catch (...)
        {
            // Evidence bookkeeping failure cannot gate consensus transport.
            HOTSTUFF_LOG_WARN(
                "KAURI_EVIDENCE response_attempt_arm_marker_failed "
                "reason=poison_bookkeeping reporter=%u epoch=%u tree=%u "
                "epoch_digest=%s block=%s",
                get_id(),
                key.configuration.epoch_number,
                key.configuration.tree_id,
                key.configuration.epoch_digest.to_hex().c_str(),
                key.block_hash.to_hex().c_str());
        }
        mark_adaptive_v2_convergence_evidence_unhealthy(reason);
    }

    void HotStuffBase::record_successful_response_attempt_arm(
        const ProposalKey &key) noexcept
    {
        const auto generation =
            find_exact_runtime_generation(key.configuration);
        if (!generation.has_value())
            return;
        try
        {
            if (successful_response_attempt_arm_provenance.size() >=
                    maximum_proposal_view_generation_observations &&
                successful_response_attempt_arm_provenance.count(key) == 0)
                return;
            successful_response_attempt_arm_provenance.insert_or_assign(
                key, *generation);
        }
        catch (...)
        {
            // Missing retransmit provenance later fails closed, while this
            // already-armed dissemination remains consensus-live.
        }
    }

    bool HotStuffBase::has_successful_response_attempt_arm(
        const ProposalKey &key) const noexcept
    {
        const auto generation =
            find_exact_runtime_generation(key.configuration);
        if (!generation.has_value())
            return false;
        try
        {
            const auto found =
                successful_response_attempt_arm_provenance.find(key);
            return found !=
                       successful_response_attempt_arm_provenance.end() &&
                   found->second == *generation;
        }
        catch (...)
        {
            return false;
        }
    }

    void HotStuffBase::
    ensure_finalized_proposal_evidence_before_exposure(
        const ProposalKey &key,
        const char *arm_failure_reason) noexcept
    {
        if (!has_successful_response_attempt_arm(key))
            attempt_proposal_evidence_before_exposure(
                key, arm_failure_reason);
    }

    bool HotStuffBase::start_latency_deadline(const ProposalKey &key)
    {
        bool normal_evidence_armed = false;
        const bool response_evidence_enabled =
            epoch_protocol_mode == EpochProtocolMode::adaptive_v2 ||
            (epoch_protocol_mode == EpochProtocolMode::adaptive_v3 &&
             experiment_exact_timeout_attempt_evidence_v3);
        const auto lease = proposal_contexts->acquire_open_context(key);
        if (!lease.has_value())
        {
            if (response_evidence_enabled)
                poison_response_attempt_arm_once(
                    key, "proposal_context_unavailable");
            return false;
        }
        try
        {
            for (const auto child : lease->tree().direct_children)
                proposal_contexts->record_latency_start(*lease, child);

            arm_experiment_post_qc_audit(*lease);

            if (!response_evidence_enabled ||
                lease->tree().direct_children.empty())
                return true;
            if (adaptive_v2_response_evidence == nullptr)
            {
                poison_response_attempt_arm_once(
                    key, "response_evidence_unavailable");
                return false;
            }
            const auto *tree = find_exact_runtime_tree(key.configuration);
            if (tree == nullptr)
            {
                poison_response_attempt_arm_once(
                    key, "runtime_tree_unavailable");
                return false;
            }
            const auto duration = aggregation_timeout_policy.timeout_for(
                static_cast<std::uint32_t>(tree->get_level(get_id())),
                static_cast<std::uint32_t>(tree->get_max_level()));
            std::optional<ReplicaID> false_report_target;
            if (experiment_byzantine_adapter != nullptr)
            {
                for (const auto child : lease->tree().direct_children)
                {
                    const ExperimentByzantineContext context{
                        key,
                        experiment_diagnostic_window};
                    if (experiment_byzantine_adapter->arm_false_report(
                            context, child))
                    {
                        false_report_target = child;
                        break;
                    }
                }
            }

            const auto start_ns =
                adaptive_evidence_monotonic_now_ns();
            const auto deadline_us =
                adaptive_deadline_duration_us(duration);
            const auto armed_deadlines_before =
                false_report_target.has_value()
                    ? std::uint64_t{0}
                    : adaptive_v2_response_evidence->diagnostics()
                          .armed_deadlines;
            const bool evidence_armed =
                false_report_target.has_value()
                    ? adaptive_v2_response_evidence->arm(
                          key,
                          lease->tree(),
                          start_ns,
                          deadline_us)
                    : adaptive_v2_response_evidence
                          ->arm_with_deadline(
                              key,
                              lease->tree(),
                              start_ns,
                              deadline_us);
            normal_evidence_armed =
                evidence_armed && !false_report_target.has_value();
            if (!evidence_armed)
            {
                const auto diagnostics =
                    adaptive_v2_response_evidence->diagnostics();
                HOTSTUFF_LOG_WARN(
                    "[EVIDENCE] Observation deadline arm failed "
                    "epoch=%u tree=%u block=%.10s "
                    "schedule_failures=%llu callback_failures=%llu "
                    "healthy=%u",
                    key.configuration.epoch_number,
                    key.configuration.tree_id,
                    key.block_hash.to_hex().c_str(),
                    static_cast<unsigned long long>(
                        diagnostics.deadline_schedule_failures),
                    static_cast<unsigned long long>(
                        diagnostics.deadline_callback_failures),
                    diagnostics.healthy ? 1U : 0U);
                if (false_report_target.has_value())
                {
                    static_cast<void>(
                        experiment_byzantine_adapter
                            ->cancel_false_report(
                                ExperimentByzantineContext{
                                    key,
                                    experiment_diagnostic_window},
                                *false_report_target));
                    static_cast<void>(
                        adaptive_v2_response_evidence->retire(key));
                    suppress_adaptive_v2_lifecycle_reporting(
                        "false_report_evidence_arm_failed");
                }
                poison_response_attempt_arm_once(
                    key, "deadline_arm_failed");
                return false;
            }
            if (false_report_target.has_value())
            {
                HOTSTUFF_LOG_INFO(
                    "KAURI_FAULT false_report_armed reporter=%u "
                    "target=%u epoch=%u tree=%u block=%s window=%s",
                    get_id(),
                    *false_report_target,
                    key.configuration.epoch_number,
                    key.configuration.tree_id,
                    key.block_hash.to_hex().c_str(),
                    experiment_diagnostic_window.c_str());
                schedule_experiment_false_timeout(
                    key, *false_report_target, duration);
                record_successful_response_attempt_arm(key);
                return true;
            }
            else
            {
                const bool tiered_marker_required =
                    experiment_byzantine_adapter != nullptr &&
                    std::any_of(
                        lease->tree().direct_children.begin(),
                        lease->tree().direct_children.end(),
                        [this, &lease](ReplicaID child) {
                            const auto required =
                                lease->tree()
                                    .required_child_subtrees.find(child);
                            return required !=
                                       lease->tree()
                                           .required_child_subtrees.end() &&
                                   !required->second.empty() &&
                                   experiment_byzantine_adapter
                                       ->is_tiered_responsive_degraded_actor(
                                           child);
                        });
                if (!tiered_marker_required)
                {
                    record_successful_response_attempt_arm(key);
                    return true;
                }
                const auto armed_deadlines_after =
                    adaptive_v2_response_evidence->diagnostics()
                        .armed_deadlines;
                if (armed_deadlines_after == armed_deadlines_before)
                {
                    // A matching live arm is idempotent. It creates no new
                    // deadline and therefore must not claim a second raw arm.
                    HOTSTUFF_LOG_WARN(
                        "KAURI_EVIDENCE response_attempt_arm_duplicate "
                        "reporter=%u epoch=%u tree=%u epoch_digest=%s "
                        "block=%s",
                        get_id(),
                        key.configuration.epoch_number,
                        key.configuration.tree_id,
                        key.configuration.epoch_digest.to_hex().c_str(),
                        key.block_hash.to_hex().c_str());
                    record_successful_response_attempt_arm(key);
                    return true;
                }

                constexpr std::uint64_t nanoseconds_per_microsecond =
                    1000;
                const auto maximum =
                    std::numeric_limits<std::uint64_t>::max();
                const bool invalid_deadline_counter =
                    armed_deadlines_before == maximum ||
                    armed_deadlines_after != armed_deadlines_before + 1;
                const bool duration_overflow =
                    deadline_us >
                    maximum / nanoseconds_per_microsecond;
                const auto deadline_duration_ns = duration_overflow
                    ? std::uint64_t{0}
                    : deadline_us * nanoseconds_per_microsecond;
                const bool absolute_deadline_overflow =
                    duration_overflow || start_ns == 0 ||
                    start_ns > maximum - deadline_duration_ns;
                if (invalid_deadline_counter ||
                    absolute_deadline_overflow)
                {
                    static_cast<void>(
                        adaptive_v2_response_evidence->retire(key));
                    suppress_adaptive_v2_lifecycle_reporting(
                        "response_attempt_arm_marker_failed");
                    poison_response_attempt_arm_once(
                        key,
                        invalid_deadline_counter
                            ? "deadline_counter"
                            : "deadline_overflow");
                    return false;
                }
                const auto absolute_deadline_ns =
                    start_ns + deadline_duration_ns;
                for (const auto child :
                     lease->tree().direct_children)
                {
                    if (!experiment_byzantine_adapter
                             ->is_tiered_responsive_degraded_actor(child))
                        continue;
                    const auto required =
                        lease->tree().required_child_subtrees.find(child);
                    if (required ==
                            lease->tree().required_child_subtrees.end() ||
                        required->second.empty())
                        continue;
                    const auto subtree =
                        lease->tree().child_subtrees.find(child);
                    if (subtree ==
                            lease->tree().child_subtrees.end() ||
                        subtree->second.empty())
                    {
                        static_cast<void>(
                            adaptive_v2_response_evidence->retire(key));
                        suppress_adaptive_v2_lifecycle_reporting(
                            "response_attempt_arm_marker_failed");
                        poison_response_attempt_arm_once(
                            key, "topology");
                        return false;
                    }
                    const char *expected_message_type =
                        subtree->second.size() > 1
                            ? "aggregate_relay"
                            : "direct_vote";
                    HOTSTUFF_LOG_INFO(
                        "KAURI_EVIDENCE response_attempt_armed "
                        "reporter=%u child=%u epoch=%u tree=%u "
                        "epoch_digest=%s block=%s "
                        "expected_message_type=%s "
                        "start_monotonic_ns=%llu "
                        "deadline_duration_us=%llu "
                        "absolute_deadline_ns=%llu",
                        get_id(),
                        child,
                        key.configuration.epoch_number,
                        key.configuration.tree_id,
                        key.configuration.epoch_digest.to_hex().c_str(),
                        key.block_hash.to_hex().c_str(),
                        expected_message_type,
                        static_cast<unsigned long long>(start_ns),
                        static_cast<unsigned long long>(deadline_us),
                        static_cast<unsigned long long>(
                            absolute_deadline_ns));
                }
                record_successful_response_attempt_arm(key);
                return true;
            }
        }
        catch (...)
        {
            // Consensus progress is independent. A successful normal arm
            // without its raw provenance marker is unusable convergence
            // evidence, so retire it and suppress its commit notice.
            if (normal_evidence_armed)
            {
                static_cast<void>(
                    adaptive_v2_response_evidence->retire(key));
                suppress_adaptive_v2_lifecycle_reporting(
                    "response_attempt_arm_marker_exception");
            }
            poison_response_attempt_arm_once(key, "exception");
            return false;
        }
        return true;
    }

    void HotStuffBase::attempt_proposal_evidence_before_exposure(
        const ProposalKey &key,
        const char *arm_failure_reason) noexcept
    {
        bool armed = false;
        try
        {
            create_expected_vote_state(key);
            armed = start_latency_deadline(key);
        }
        catch (...)
        {
            armed = false;
        }
        if (!armed)
            poison_response_attempt_arm_once(key, arm_failure_reason);

        try
        {
            start_aggregation_timer(key);
        }
        catch (...)
        {
            poison_response_attempt_arm_once(
                key,
                "aggregation_timer_failed_before_proposal_exposure");
        }
    }

    void HotStuffBase::schedule_experiment_false_timeout(
        const ProposalKey &key,
        ReplicaID target,
        AggregationScheduler::Duration delay)
    {
        if (aggregation_scheduler == nullptr ||
            experiment_byzantine_adapter == nullptr ||
            adaptive_v2_response_evidence == nullptr)
        {
            cancel_experiment_false_timeout(
                key, target, "dependencies");
            return;
        }
        if (experiment_false_timeout_states.find(key) !=
            experiment_false_timeout_states.end())
            return;
        if (maximum_experiment_false_timeout_contexts == 0 ||
            experiment_false_timeout_states.size() >=
                maximum_experiment_false_timeout_contexts)
        {
            cancel_experiment_false_timeout(
                key, target, "capacity");
            return;
        }
        try
        {
            const auto access = exact_runtime_access;
            const auto window = experiment_diagnostic_window;
            const auto now = aggregation_scheduler->monotonic_now();
            if (delay <= AggregationScheduler::Duration::zero() ||
                now > AggregationScheduler::Duration::max() - delay)
            {
                cancel_experiment_false_timeout(
                    key, target, "deadline");
                return;
            }
            const auto deadline = now + delay;
            const auto inserted = experiment_false_timeout_states.emplace(
                key, ExperimentFalseTimeoutState{target, true, false});
            if (!inserted.second)
                return;
            const auto cancellation = schedule_at_or_after_deadline(
                *aggregation_scheduler,
                deadline,
                [access, key, target, window]()
                {
                    auto runtime = access->acquire();
                    if (!runtime.has_value())
                        return;
                    runtime->owner().dispatch_experiment_false_timeout(
                        key, target, window);
                },
                [access, key, target]()
                {
                    auto runtime = access->acquire();
                    if (!runtime.has_value())
                        return;
                    runtime->owner().cancel_experiment_false_timeout(
                        key, target, "scheduler");
                });
            if (!cancellation)
                return;
        }
        catch (...)
        {
            cancel_experiment_false_timeout(
                key, target, "scheduler");
        }
    }

    void HotStuffBase::dispatch_experiment_false_timeout(
        const ProposalKey &key,
        ReplicaID target,
        std::string window)
    {
        if (experiment_byzantine_adapter == nullptr ||
            adaptive_v2_response_evidence == nullptr)
        {
            cancel_experiment_false_timeout(
                key, target, "dependencies");
            return;
        }
        if (experiment_false_timeout_states.find(key) ==
            experiment_false_timeout_states.end())
            return;

        std::size_t recorded = 0;
        bool evidence_queued_before_commit = false;
        bool false_timeout_consumed = false;
        try
        {
            false_timeout_consumed =
                experiment_byzantine_adapter->consume_false_timeout(
                    ExperimentByzantineContext{key, window},
                    target);
            if (false_timeout_consumed)
            {
                const auto evidence_sequence_before =
                    adaptive_v2_reporting_outbox != nullptr
                        ? adaptive_v2_reporting_outbox
                              ->diagnostics()
                              .last_evidence_sequence
                        : std::uint64_t{0};
                recorded = adaptive_v2_response_evidence->record_timeouts(
                    key,
                    std::set<ReplicaID>{target},
                    adaptive_evidence_monotonic_now_ns());
                const auto bridge =
                    adaptive_v2_response_evidence->diagnostics();
                const auto evidence_sequence_after =
                    adaptive_v2_reporting_outbox != nullptr
                        ? adaptive_v2_reporting_outbox
                              ->diagnostics()
                              .last_evidence_sequence
                        : std::uint64_t{0};
                evidence_queued_before_commit =
                    recorded == 1 &&
                    evidence_sequence_after > evidence_sequence_before &&
                    bridge.pending_reports == 0 &&
                    bridge.retained_facts == 0 &&
                    bridge.pending_late_compensations == 0;
                if (recorded == 1)
                {
                    static_cast<void>(
                        adaptive_v2_response_evidence->retire(key));
                    const auto marker_monotonic_ns =
                        experiment_fault_marker_monotonic_now_ns();
                    if (marker_monotonic_ns.has_value())
                        HOTSTUFF_LOG_INFO(
                            "KAURI_FAULT false_timeout_emitted "
                            "reporter=%u target=%u epoch=%u tree=%u "
                            "block=%s window=%s monotonic_ns=%llu",
                            get_id(),
                            target,
                            key.configuration.epoch_number,
                            key.configuration.tree_id,
                            key.block_hash.to_hex().c_str(),
                            window.c_str(),
                            static_cast<unsigned long long>(
                                *marker_monotonic_ns));
                    else
                        HOTSTUFF_LOG_WARN(
                            "KAURI_FAULT marker_skipped "
                            "marker=false_timeout_emitted reporter=%u "
                            "target=%u epoch=%u tree=%u block=%s "
                            "reason=event_clock_unavailable",
                            get_id(),
                            target,
                            key.configuration.epoch_number,
                            key.configuration.tree_id,
                            key.block_hash.to_hex().c_str());
                }
            }
        }
        catch (...)
        {
            HOTSTUFF_LOG_WARN(
                "KAURI_FAULT false_timeout_failed reporter=%u "
                "target=%u epoch=%u tree=%u block=%s",
                get_id(),
                target,
                key.configuration.epoch_number,
                key.configuration.tree_id,
                key.block_hash.to_hex().c_str());
        }
        release_experiment_false_report_commit(
            key, recorded, evidence_queued_before_commit);
        if (!false_timeout_consumed || recorded != 1)
        {
            if (experiment_byzantine_adapter != nullptr)
                static_cast<void>(
                    experiment_byzantine_adapter->cancel_false_report(
                        ExperimentByzantineContext{key, window},
                        target));
        }
    }

    void HotStuffBase::cancel_experiment_false_timeout(
        const ProposalKey &key,
        ReplicaID target,
        const char *reason) noexcept
    {
        const auto pending = experiment_false_timeout_states.find(key);
        const bool deferred_commit =
            pending != experiment_false_timeout_states.end() &&
            pending->second.commit_deferred;
        if (deferred_commit)
        {
            AdaptiveV2DurableCommitReportState suppressed;
            suppressed.phase =
                AdaptiveV2DurableCommitPhase::suppressed;
            suppressed.experiment_false_report = true;
            suppressed.experiment_false_target = target;
            static_cast<void>(persist_adaptive_v2_commit_report(
                key,
                suppressed,
                "false_report_cancel_tombstone_capacity_exceeded"));
            mark_adaptive_v2_convergence_evidence_unhealthy(
                "false_report_deferred_commit_cancelled");
        }
        experiment_false_timeout_states.erase(key);
        try
        {
            if (experiment_byzantine_adapter != nullptr)
                static_cast<void>(
                    experiment_byzantine_adapter->cancel_false_report(
                        ExperimentByzantineContext{
                            key,
                            experiment_diagnostic_window},
                        target));
        }
        catch (...)
        {}
        HOTSTUFF_LOG_WARN(
            "KAURI_FAULT false_timeout_schedule_rejected reporter=%u "
            "target=%u epoch=%u tree=%u block=%s reason=%s",
            get_id(),
            target,
            key.configuration.epoch_number,
            key.configuration.tree_id,
            key.block_hash.to_hex().c_str(),
            reason == nullptr ? "unknown" : reason);
    }

    void HotStuffBase::release_experiment_false_report_commit(
        const ProposalKey &key,
        std::size_t recorded_evidence,
        bool evidence_queued_before_commit) noexcept
    {
        try
        {
            const auto pending =
                experiment_false_timeout_states.find(key);
            if (pending == experiment_false_timeout_states.end())
                return;
            const auto target = pending->second.target;
            const auto action = pending->second.complete(
                evidence_queued_before_commit);
            if (action ==
                ExperimentFalseTimeoutCompletionAction::
                    no_deferred_commit)
            {
                if (!evidence_queued_before_commit)
                {
                    AdaptiveV2DurableCommitReportState suppressed;
                    suppressed.phase =
                        AdaptiveV2DurableCommitPhase::suppressed;
                    suppressed.experiment_false_report = true;
                    suppressed.experiment_false_target = target;
                    suppressed.experiment_false_recorded_evidence =
                        recorded_evidence;
                    static_cast<void>(persist_adaptive_v2_commit_report(
                        key,
                        suppressed,
                        "false_report_precommit_tombstone_capacity_exceeded"));
                    experiment_false_timeout_states.erase(pending);
                    HOTSTUFF_LOG_WARN(
                        "KAURI_FAULT false_report_commit_suppressed "
                        "reporter=%u target=%u epoch=%u tree=%u block=%s "
                        "evidence=%zu reason=evidence_not_queued_before_commit",
                        get_id(),
                        target,
                        key.configuration.epoch_number,
                        key.configuration.tree_id,
                        key.block_hash.to_hex().c_str(),
                        recorded_evidence);
                    mark_adaptive_v2_convergence_evidence_unhealthy(
                        "false_report_precommit_evidence_not_queued");
                    return;
                }
                experiment_false_timeout_states.erase(pending);
                return;
            }
            if (action ==
                ExperimentFalseTimeoutCompletionAction::fail_closed)
            {
                AdaptiveV2DurableCommitReportState suppressed;
                suppressed.phase =
                    AdaptiveV2DurableCommitPhase::suppressed;
                suppressed.experiment_false_report = true;
                suppressed.experiment_false_target = target;
                suppressed.experiment_false_recorded_evidence =
                    recorded_evidence;
                static_cast<void>(persist_adaptive_v2_commit_report(
                    key,
                    suppressed,
                    "false_report_tombstone_capacity_exceeded"));
                experiment_false_timeout_states.erase(pending);
                HOTSTUFF_LOG_WARN(
                    "KAURI_FAULT false_report_commit_suppressed reporter=%u "
                    "target=%u epoch=%u tree=%u block=%s evidence=%zu "
                    "reason=evidence_not_queued",
                    get_id(),
                    target,
                    key.configuration.epoch_number,
                    key.configuration.tree_id,
                    key.block_hash.to_hex().c_str(),
                    recorded_evidence);
                mark_adaptive_v2_convergence_evidence_unhealthy(
                    "false_report_evidence_not_queued_before_commit");
                return;
            }
            AdaptiveV2DurableCommitReportState ready;
            ready.phase =
                AdaptiveV2DurableCommitPhase::ready_to_enqueue;
            ready.experiment_false_report = true;
            ready.experiment_false_target = target;
            ready.experiment_false_recorded_evidence = recorded_evidence;
            if (!persist_adaptive_v2_commit_report(
                    key,
                    ready,
                    "false_report_commit_state_capacity_exceeded"))
            {
                experiment_false_timeout_states.erase(pending);
                HOTSTUFF_LOG_WARN(
                    "KAURI_FAULT false_report_commit_suppressed reporter=%u "
                    "target=%u epoch=%u tree=%u block=%s evidence=%zu "
                    "reason=commit_state_not_retained",
                    get_id(),
                    target,
                    key.configuration.epoch_number,
                    key.configuration.tree_id,
                    key.block_hash.to_hex().c_str(),
                    recorded_evidence);
                return;
            }
            experiment_false_timeout_states.erase(pending);
            static_cast<void>(
                try_enqueue_adaptive_v2_commit_report(key));
        }
        catch (...)
        {
            // Experiment reporting cannot affect consensus progress.
            experiment_false_timeout_states.erase(key);
            suppress_adaptive_v2_lifecycle_reporting(
                "false_report_commit_release_exception");
        }
    }

    void HotStuffBase::start_aggregation_timer(const ProposalKey &key)
    {
        const auto *tree = find_exact_runtime_tree(key.configuration);
        const auto lease = proposal_contexts->acquire_open_context(key);
        const bool leaf = lease.has_value() &&
                          lease->tree().direct_children.empty();
        const bool missing_required_state =
            tree == nullptr || !lease.has_value() ||
            aggregation_timeout_coordinator == nullptr ||
            aggregation_scheduler == nullptr;
        if (missing_required_state || leaf)
        {
            HOTSTUFF_LOG_INFO(
                "KAURI_PROPOSAL_BROADCAST stage=aggregation_timer "
                "outcome=skipped reason=precondition replica=%u epoch=%u "
                "tree=%u block=%s exact_tree=%u context_open=%u "
                "direct_children=%zu coordinator=%u scheduler=%u",
                static_cast<unsigned>(get_id()),
                key.configuration.epoch_number,
                key.configuration.tree_id,
                key.block_hash.to_hex().c_str(),
                tree != nullptr ? 1U : 0U,
                lease.has_value() ? 1U : 0U,
                lease.has_value()
                    ? lease->tree().direct_children.size()
                    : std::size_t{0},
                aggregation_timeout_coordinator != nullptr ? 1U : 0U,
                aggregation_scheduler != nullptr ? 1U : 0U);
            if (epoch_protocol_mode == EpochProtocolMode::adaptive_v2 &&
                !leaf)
                poison_response_attempt_arm_once(
                    key,
                    tree == nullptr
                        ? "aggregation_timer_tree_unavailable"
                        : !lease.has_value()
                            ? "aggregation_timer_context_unavailable"
                            : aggregation_timeout_coordinator == nullptr
                                ? "aggregation_timer_coordinator_unavailable"
                                : "aggregation_timer_scheduler_unavailable");
            return;
        }

        const auto timer_generation =
            aggregation_timeout_coordinator->arm_timeout(
            *lease,
            *aggregation_scheduler,
            static_cast<std::uint32_t>(tree->get_level(get_id())),
            static_cast<std::uint32_t>(tree->get_max_level()));
        if (timer_generation == 0 &&
            epoch_protocol_mode == EpochProtocolMode::adaptive_v2)
            poison_response_attempt_arm_once(
                key, "aggregation_timer_arm_rejected");
        HOTSTUFF_LOG_INFO(
            "KAURI_PROPOSAL_BROADCAST stage=aggregation_timer outcome=%s "
            "reason=%s replica=%u epoch=%u tree=%u block=%s "
            "timer_generation=%llu",
            timer_generation == 0 ? "skipped" : "armed",
            timer_generation == 0 ? "context_rejected" : "none",
            static_cast<unsigned>(get_id()),
            key.configuration.epoch_number,
            key.configuration.tree_id,
            key.block_hash.to_hex().c_str(),
            static_cast<unsigned long long>(timer_generation));
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

    void HotStuffBase::adaptive_definition_request_handler(
        MsgEpochDefinitionRequest &&message,
        const Net::conn_t &conn)
    {
        if (!(epoch_protocol_mode == EpochProtocolMode::adaptive_v2 ||
              epoch_protocol_mode == EpochProtocolMode::adaptive_v3) ||
            conn == nullptr || exact_epochs == nullptr)
            return;
        const auto peer = conn->get_peer_id();
        const auto authenticated = peer_id_map.find(peer);
        if (peer.is_null() || authenticated == peer_id_map.end() ||
            authenticated->second >= fixed_membership.size() ||
            fixed_membership[authenticated->second] !=
                static_cast<ReplicaID>(authenticated->second))
            return;

        const auto decoded = decode_epoch_definition_request(
            static_cast<bytearray_t>(message.serialized),
            epoch_protocol_mode,
            epoch_wire_limits);
        if (!decoded)
            return;
        const auto *definition = exact_epochs->find_epoch_by_digest(
            decoded.value->successor_epoch_digest);
        if (definition == nullptr ||
            definition->schema_version() !=
                kEpochDefinitionSchemaVersionV2 ||
            definition->epoch_digest() !=
                decoded.value->successor_epoch_digest)
            return;

        try
        {
            const auto reply_wire_schema =
                epoch_wire_schema_for_mode(epoch_protocol_mode);
            if (!reply_wire_schema)
                return;
            const EpochDefinitionReply reply{
                *reply_wire_schema,
                epoch_protocol_mode,
                definition->epoch_digest(),
                available_epoch_definition(*definition)};
            const MsgEpochDefinitionReply response(
                reply, epoch_wire_limits);
            pn.send_msg(response, peer);
        }
        catch (...)
        {
            HOTSTUFF_LOG_WARN(
                "[EPOCH] Failed to reply with an available definition");
        }
    }

    void HotStuffBase::adaptive_definition_reply_handler(
        MsgEpochDefinitionReply &&message,
        const Net::conn_t &conn)
    {
        if (!(epoch_protocol_mode == EpochProtocolMode::adaptive_v2 ||
              epoch_protocol_mode == EpochProtocolMode::adaptive_v3) ||
            conn == nullptr || exact_epochs == nullptr ||
            proposal_admission == nullptr)
            return;
        const auto peer = conn->get_peer_id();
        const auto authenticated = peer_id_map.find(peer);
        if (peer.is_null() || authenticated == peer_id_map.end() ||
            authenticated->second >= fixed_membership.size() ||
            fixed_membership[authenticated->second] !=
                static_cast<ReplicaID>(authenticated->second))
            return;

        const auto decoded = decode_epoch_definition_reply(
            static_cast<bytearray_t>(message.serialized),
            epoch_protocol_mode,
            epoch_wire_limits);
        if (!decoded)
            return;

        const auto successor_epoch_digest =
            decoded.value->successor_epoch_digest;
        auto deferred = deferred_epoch_definition_recoveries.find(
            successor_epoch_digest);
        const bool deferred_recovery_live =
            epoch_protocol_mode == EpochProtocolMode::adaptive_v2 &&
            deferred != deferred_epoch_definition_recoveries.end() &&
            deferred->second.request_live &&
            deferred->second.request.successor_epoch_digest ==
                successor_epoch_digest;
        const bool committed_recovery_live =
            committed_epoch_definition_recovery.has_value() &&
            epoch_wire_schema_for_mode(epoch_protocol_mode).has_value() &&
            committed_epoch_definition_recovery->request
                    .wire_schema_version ==
                *epoch_wire_schema_for_mode(epoch_protocol_mode) &&
            committed_epoch_definition_recovery->request.protocol_mode ==
                epoch_protocol_mode &&
            committed_epoch_definition_recovery->request
                    .successor_epoch_digest ==
                successor_epoch_digest;
        if (!deferred_recovery_live && !committed_recovery_live)
            return;

        const auto &active_configuration =
            proposal_admission->active_configuration();
        const auto *active = exact_epochs->find_epoch(
            active_configuration.epoch_number);
        if (active == nullptr ||
            active->epoch_digest() != active_configuration.epoch_digest)
            return;

        bytearray_t canonical_definition;
        try
        {
            canonical_definition =
                canonical_serialize_epoch(decoded.value->definition);
        }
        catch (...)
        {
            return;
        }
        if (canonical_definition.empty() ||
            DataStream(canonical_definition).get_hash() !=
                successor_epoch_digest ||
            (decoded.value->definition.epoch_digest &&
             *decoded.value->definition.epoch_digest !=
                 successor_epoch_digest))
            return;
        if (committed_recovery_live)
        {
            const auto &payload =
                committed_epoch_definition_recovery->command.payload;
            if (decoded.value->definition.epoch_number !=
                    payload.successor_epoch_number ||
                decoded.value->definition.previous_epoch_digest !=
                    payload.predecessor_epoch_digest ||
                active->epoch_digest() !=
                    payload.predecessor_epoch_digest)
                return;
        }

        DefinitionAvailabilityResult staged;
        try
        {
            staged = exact_epochs->stage_available_v2(
                decoded.value->definition, *active);
        }
        catch (...)
        {
            return;
        }
        if ((staged.disposition !=
                 DefinitionAvailabilityDisposition::staged &&
             staged.disposition !=
                 DefinitionAvailabilityDisposition::duplicate) ||
            staged.definition == nullptr ||
            staged.definition->epoch_digest() !=
                successor_epoch_digest ||
            staged.definition->canonical_serialization() !=
                canonical_definition)
            return;

        if (committed_recovery_live &&
            epoch_protocol_mode == EpochProtocolMode::adaptive_v2 &&
            !recover_committed_epoch_definition(*staged.definition))
            return;
        if (committed_recovery_live &&
            epoch_protocol_mode == EpochProtocolMode::adaptive_v3)
        {
            if (!adaptive_v3_activation_gate ||
                !adaptive_v3_committed_command ||
                adaptive_epoch_runtime == nullptr ||
                adaptive_v3_committed_command->protocol_mode !=
                    EpochProtocolMode::adaptive_v3 ||
                adaptive_v3_committed_command->payload
                        .successor_epoch_digest !=
                    successor_epoch_digest ||
                adaptive_epoch_runtime->adapter.prepare_committed_v3(
                    *staged.definition) != EpochIngressError::none)
                return;
            adaptive_v3_runtime_prepared = true;
            reset_committed_epoch_definition_recovery();
            resume_adaptive_v3_runtime_preparation();
        }

        if (deferred_recovery_live)
        {
            deferred = deferred_epoch_definition_recoveries.find(
                successor_epoch_digest);
            if (deferred == deferred_epoch_definition_recoveries.end() ||
                !deferred->second.request_live)
                return;
            deferred->second.request_live = false;
            if (!queue_deferred_epoch_change_retries(
                    successor_epoch_digest))
                deferred->second.request_live = true;
        }
    }

    void HotStuffBase::adaptive_v2_epoch_change_bundle_handler(
        MsgAdaptiveV2EpochChangeBundle &&message,
        const Net::conn_t &conn)
    {
        if (epoch_protocol_mode != EpochProtocolMode::adaptive_v2 ||
            conn == nullptr || adaptive_v2_command_inbox == nullptr ||
            !adaptive_v2_epoch_change_bundle_limits.has_value() ||
            epoch_change_verifier == nullptr || exact_epochs == nullptr ||
            adaptive_epoch_runtime == nullptr)
            return;

        const auto peer = conn->get_peer_id();
        if (!authorize_manager_peer(peer))
            return;
        const auto pinned_connection = pn.get_peer_conn(peer);
        const auto *certificate = conn->get_peer_cert();
        if (pinned_connection == nullptr || pinned_connection != conn ||
            certificate == nullptr || PeerId(*certificate) != peer)
            return;

        const auto decoded = decode_adaptive_v2_epoch_change_bundle(
            static_cast<bytearray_t>(message.serialized),
            *adaptive_v2_epoch_change_bundle_limits);
        if (!decoded)
            return;

        const auto active_configuration =
            adaptive_epoch_runtime->activation.active_effect().configuration;
        const auto *active_epoch = exact_epochs->find_epoch(
            active_configuration.epoch_number);
        if (active_epoch == nullptr ||
            active_epoch->epoch_digest() !=
                active_configuration.epoch_digest)
            return;

        auto &command_inbox = *adaptive_v2_command_inbox;
        const auto result = command_inbox.ingest(
            *decoded.value,
            *active_epoch,
            *epoch_change_verifier,
            *exact_epochs);
        if (result.disposition ==
                AdaptiveV2CommandIngestDisposition::internal_failure)
        {
            HOTSTUFF_LOG_WARN(
                "[EPOCH] Adaptive-v2 successor inbox failed internally");
        }
    }

    void HotStuffBase::adaptive_v3_epoch_change_bundle_handler(
        MsgAdaptiveV3EpochChangeBundle &&message,
        const Net::conn_t &conn)
    {
        if (epoch_protocol_mode != EpochProtocolMode::adaptive_v3 ||
            conn == nullptr || adaptive_v2_command_inbox == nullptr ||
            !adaptive_v2_epoch_change_bundle_limits.has_value() ||
            epoch_change_verifier == nullptr || exact_epochs == nullptr ||
            adaptive_epoch_runtime == nullptr)
            return;
        const auto peer = conn->get_peer_id();
        const auto pinned_connection = pn.get_peer_conn(peer);
        const auto *certificate = conn->get_peer_cert();
        if (!authorize_manager_peer(peer) || pinned_connection == nullptr ||
            pinned_connection != conn || certificate == nullptr ||
            PeerId(*certificate) != peer)
            return;
        const auto decoded = decode_adaptive_v3_epoch_change_bundle(
            static_cast<bytearray_t>(message.serialized),
            *adaptive_v2_epoch_change_bundle_limits);
        if (!decoded)
            return;
        const auto active =
            adaptive_epoch_runtime->activation.active_effect().configuration;
        const auto *active_epoch = exact_epochs->find_epoch(
            active.epoch_number);
        if (active_epoch == nullptr ||
            active_epoch->epoch_digest() != active.epoch_digest)
            return;
        const auto result = adaptive_v2_command_inbox->ingest(
            *decoded.value, *active_epoch, *epoch_change_verifier,
            *exact_epochs);
        if (result.disposition ==
            AdaptiveV2CommandIngestDisposition::internal_failure)
            HOTSTUFF_LOG_WARN(
                "[EPOCH] Adaptive-v3 successor inbox failed internally");
        if ((result.disposition ==
                 AdaptiveV2CommandIngestDisposition::accepted ||
             result.disposition ==
                 AdaptiveV2CommandIngestDisposition::duplicate) &&
            adaptive_v3_committed_command && result.material &&
            result.material->payload_digest ==
                epoch_change_payload_digest(
                    adaptive_v3_committed_command->payload))
        {
            const auto *successor = exact_epochs->find_epoch_by_digest(
                adaptive_v3_committed_command->payload
                    .successor_epoch_digest);
            if (successor == nullptr)
            {
                if (committed_epoch_definition_recovery &&
                    committed_epoch_definition_recovery->request
                            .protocol_mode ==
                        EpochProtocolMode::adaptive_v3)
                    send_epoch_definition_request(
                        committed_epoch_definition_recovery->request);
                return;
            }
            if (adaptive_epoch_runtime->adapter.prepare_committed_v3(
                    *successor) == EpochIngressError::none)
            {
                adaptive_v3_runtime_prepared = true;
                reset_committed_epoch_definition_recovery();
                resume_adaptive_v3_runtime_preparation();
            }
        }
    }

    void HotStuffBase::resume_adaptive_v3_runtime_preparation() noexcept
    {
        if (!adaptive_v3_config || !adaptive_v3_runtime_prepared ||
            !adaptive_v3_activation_gate ||
            adaptive_v3_observation_terminal ||
            adaptive_v3_command_terminal_emitted)
            return;
        std::optional<ActivationReadyIdentityV1> failure_identity;
        try
        {
            const auto boundary_block = adaptive_v3_boundary_block;
            const auto latest_block = adaptive_v3_latest_committed_block;
            if (adaptive_v3_deferred_observation)
            {
                failure_identity =
                    adaptive_v3_deferred_observation->identity;
                const auto observation = *adaptive_v3_deferred_observation;
                adaptive_v3_deferred_observation.reset();
                enqueue_adaptive_v3_observation(observation);
            }
            if (adaptive_v3_deferred_certificate)
            {
                failure_identity =
                    adaptive_v3_deferred_certificate->identity;
                const auto deferred = *adaptive_v3_deferred_certificate;
                adaptive_v3_deferred_certificate.reset();
                const auto payload = encode_activation_readiness_certificate(
                    deferred,
                    adaptive_v3_config->readiness_wire_limits);
                ingest_adaptive_v3_readiness_certificate(deferred, payload);
            }
            if (adaptive_v3_activation_gate && boundary_block != nullptr)
                process_adaptive_v3_post_block_commit(boundary_block);
            if (adaptive_v3_activation_gate && latest_block != nullptr &&
                latest_block != boundary_block)
                process_adaptive_v3_post_block_commit(latest_block);
        }
        catch (...)
        {
            if (failure_identity)
                emit_adaptive_v3_observation_terminal(
                    *failure_identity, "observation_internal_failure");
            else
                emit_adaptive_v3_command_terminal(
                    AdaptiveV3CommandTerminalReason::
                        readiness_internal_failure);
        }
    }

    bool HotStuffBase::emit_adaptive_v3_readiness_event(
        AdaptiveV3ReadinessStructuredEvent event,
        bool drain_before_return) noexcept
    {
        if (audit_event_emitter == nullptr)
            return false;
        try
        {
            audit_event_emitter->emit_audit(
                AuditStructuredEventPayload{std::move(event)});
            auto *owner = dynamic_cast<StructuredEventDrainOwner *>(
                audit_event_emitter);
            if (drain_before_return)
            {
                // Malformed authenticated wire must be durable before the
                // handler returns; a queued-only record is not evidence.
                if (owner == nullptr)
                    return false;
                owner->drain();
            }
            return owner == nullptr || owner->health().healthy;
        }
        catch (...)
        {
            return false;
        }
    }

    void HotStuffBase::fail_adaptive_v3_readiness_audit() noexcept
    {
        // No readiness identity is available for malformed wire, so this is
        // deliberately a local fail-closed fence rather than a terminal audit
        // that would manufacture an identity.
        adaptive_v3_observation_terminal = true;
    }

    void HotStuffBase::emit_adaptive_v3_observation_terminal(
        const ActivationReadyIdentityV1 &identity,
        const char *disposition) noexcept
    {
        if (adaptive_v3_observation_terminal)
            return;
        adaptive_v3_observation_terminal = true;
        AdaptiveV3ReadinessStructuredEvent event;
        event.transition = AdaptiveV3ReadinessTransition::terminal;
        event.identity = identity;
        event.replica_id = get_id();
        event.disposition = disposition == nullptr
            ? "observation_internal_failure" : disposition;
        emit_adaptive_v3_readiness_event(std::move(event));
    }

    void HotStuffBase::emit_adaptive_v3_command_terminal(
        AdaptiveV3CommandTerminalReason reason) noexcept
    {
        if (adaptive_v3_command_terminal_emitted ||
            !adaptive_v3_command_evidence)
            return;
        adaptive_v3_command_terminal_emitted = true;
        adaptive_v3_observation_terminal = true;
        try
        {
            AdaptiveV3CommandTerminalStructuredEvent event;
            event.command = *adaptive_v3_command_evidence;
            event.reason = reason;
            if (audit_event_emitter)
                audit_event_emitter->emit_audit(
                    AuditStructuredEventPayload{std::move(event)});
        }
        catch (...)
        {}
    }

    void HotStuffBase::cancel_adaptive_v3_observation_retry() noexcept
    {
        auto cancellation =
            std::move(adaptive_v3_observation_retry_cancellation);
        adaptive_v3_observation_retry_cancellation = {};
        if (!cancellation)
            return;
        try
        {
            cancellation();
        }
        catch (...)
        {}
    }

    void HotStuffBase::schedule_adaptive_v3_observation_retry() noexcept
    {
        if (!adaptive_v3_config || adaptive_v3_observation_terminal ||
            adaptive_v3_observation_retry_exhausted ||
            adaptive_v3_pending_observation == std::nullopt ||
            adaptive_v3_observation_retry_cancellation)
            return;
        const auto delay = std::chrono::milliseconds(
            adaptive_v3_config->observation_retry_interval_ms);
        const auto access = exact_runtime_access;
        adaptive_v3_observation_retry_cancellation =
            aggregation_scheduler->schedule_after(
                std::chrono::duration_cast<
                    AggregationScheduler::Duration>(delay),
                [access]() {
                    auto runtime = access->acquire();
                    if (!runtime)
                        return;
                    auto &owner = runtime->owner();
                    owner.adaptive_v3_observation_retry_cancellation = {};
                    owner.transmit_adaptive_v3_observation();
                });
        if (!adaptive_v3_observation_retry_cancellation &&
            adaptive_v3_signed_observation)
            emit_adaptive_v3_observation_terminal(
                adaptive_v3_signed_observation->identity,
                "observation_schedule_failed");
    }

    void HotStuffBase::transmit_adaptive_v3_observation() noexcept
    {
        if (!adaptive_v3_config || !adaptive_v3_pending_observation ||
            adaptive_v3_observation_terminal ||
            adaptive_v3_observation_retry_exhausted ||
            !epoch_manager_peer.has_value())
            return;
        if (!adaptive_v3_signed_observation)
        {
            emit_adaptive_v3_command_terminal(
                AdaptiveV3CommandTerminalReason::
                    readiness_internal_failure);
            return;
        }
        if (adaptive_v3_observation_attempts >=
            adaptive_v3_config->maximum_observation_attempts)
        {
            adaptive_v3_observation_retry_exhausted = true;
            AdaptiveV3ReadinessStructuredEvent event;
            event.transition = AdaptiveV3ReadinessTransition::
                observation_retry_exhausted;
            event.identity = adaptive_v3_signed_observation->identity;
            event.replica_id = get_id();
            event.signer_source_sequence =
                adaptive_v3_signed_observation->signer_source_sequence;
            event.signer_monotonic_raw_ns =
                adaptive_v3_signed_observation->signer_monotonic_raw_ns;
            event.observation_digest =
                activation_ready_observation_digest(
                    *adaptive_v3_signed_observation);
            event.canonical_wire_payload =
                *adaptive_v3_pending_observation;
            event.disposition = "retry_exhausted";
            emit_adaptive_v3_readiness_event(std::move(event));
            HOTSTUFF_LOG_WARN(
                "[EPOCH] Adaptive-v3 readiness observation retry exhausted");
            return;
        }
        ++adaptive_v3_observation_attempts;
        try
        {
            const auto connection = pn.get_peer_conn(*epoch_manager_peer);
            const auto *certificate = connection == nullptr
                ? nullptr : connection->get_peer_cert();
            if (connection != nullptr && !connection->is_terminated() &&
                certificate != nullptr &&
                PeerId(*certificate) == *epoch_manager_peer)
                pn.send_msg(
                    MsgActivationReadyObservation(
                        DataStream(*adaptive_v3_pending_observation)),
                    connection);
        }
        catch (...)
        {}
        schedule_adaptive_v3_observation_retry();
    }

    void HotStuffBase::enqueue_adaptive_v3_observation(
        const AdaptiveV3ActivationReadyObservation &observation) noexcept
    {
        if (!adaptive_v3_config || adaptive_v3_pending_observation ||
            adaptive_v3_signed_observation)
            return;
        try
        {
            const auto payload = encode_activation_ready_observation(
                observation,
                adaptive_v3_config->readiness_wire_limits);
            adaptive_v3_signed_observation = observation;
            adaptive_v3_pending_observation = payload;
            AdaptiveV3ReadinessStructuredEvent event;
            event.transition =
                AdaptiveV3ReadinessTransition::activation_ready_signed;
            event.identity = observation.identity;
            event.replica_id = get_id();
            event.signer_source_sequence =
                observation.signer_source_sequence;
            event.signer_monotonic_raw_ns =
                observation.signer_monotonic_raw_ns;
            event.observation_digest =
                activation_ready_observation_digest(observation);
            event.canonical_wire_payload = payload;
            emit_adaptive_v3_readiness_event(std::move(event));
            transmit_adaptive_v3_observation();
        }
        catch (...)
        {
            emit_adaptive_v3_observation_terminal(
                observation.identity,
                "observation_encoding_failed");
        }
    }

    void HotStuffBase::acknowledge_adaptive_v3_certificate() noexcept
    {
        if (!adaptive_v3_config || !adaptive_v3_accepted_certificate ||
            !epoch_manager_peer)
            return;
        try
        {
            const auto certificate_payload =
                encode_activation_readiness_certificate(
                    *adaptive_v3_accepted_certificate,
                    adaptive_v3_config->readiness_wire_limits);
            ActivationReadinessAckV1 acknowledgement;
            acknowledgement.acknowledged_opcode =
                MsgActivationReadinessCertificate::opcode;
            acknowledgement.recipient_replica_id = get_id();
            acknowledgement.identity =
                adaptive_v3_accepted_certificate->identity;
            acknowledgement.certificate_digest =
                adaptive_v3_accepted_certificate->certificate_digest;
            acknowledgement.payload_digest =
                activation_readiness_ack_payload_digest(
                    MsgActivationReadinessCertificate::opcode,
                    certificate_payload);
            const auto acknowledgement_payload =
                encode_activation_readiness_ack(
                    acknowledgement,
                    adaptive_v3_config->readiness_wire_limits);
            adaptive_v3_certificate_ack_sent =
                transmit_adaptive_v3_acknowledgement(
                    acknowledgement_payload);
        }
        catch (...)
        {}
    }

    bool HotStuffBase::transmit_adaptive_v3_acknowledgement(
        const bytearray_t &canonical_payload) noexcept
    {
        if (!adaptive_v3_config || !epoch_manager_peer ||
            canonical_payload.empty())
            return false;
        try
        {
            const auto decoded = decode_activation_readiness_ack(
                canonical_payload,
                adaptive_v3_config->readiness_wire_limits);
            if (!decoded ||
                decoded.value->recipient_replica_id != get_id())
                return false;
            const auto connection = pn.get_peer_conn(*epoch_manager_peer);
            const auto *peer_certificate = connection == nullptr
                ? nullptr : connection->get_peer_cert();
            if (connection == nullptr || connection->is_terminated() ||
                peer_certificate == nullptr ||
                PeerId(*peer_certificate) != *epoch_manager_peer)
                return false;
            pn.send_msg(
                MsgActivationReadinessAck(
                    DataStream(canonical_payload)),
                connection);
            return true;
        }
        catch (...)
        {
            return false;
        }
    }

    std::unique_ptr<HotStuffBase::AdaptiveV3RetiredActivationReceipt>
    HotStuffBase::prepare_adaptive_v3_retirement(
        const AdaptiveV3ActivationReadinessCertificate &certificate,
        const bytearray_t &canonical_certificate_payload) const
    {
        if (!adaptive_v3_config)
            throw std::logic_error(
                "adaptive-v3 retirement requires runtime configuration");
        const auto encoded_certificate =
            encode_activation_readiness_certificate(
                certificate,
                adaptive_v3_config->readiness_wire_limits);
        if (encoded_certificate != canonical_certificate_payload)
            throw std::invalid_argument(
                "adaptive-v3 retirement certificate is noncanonical");
        ActivationReadinessAckV1 acknowledgement;
        acknowledgement.acknowledged_opcode =
            MsgActivationReadinessCertificate::opcode;
        acknowledgement.recipient_replica_id = get_id();
        acknowledgement.identity = certificate.identity;
        acknowledgement.certificate_digest =
            certificate.certificate_digest;
        acknowledgement.payload_digest =
            activation_readiness_ack_payload_digest(
                MsgActivationReadinessCertificate::opcode,
                canonical_certificate_payload);
        auto acknowledgement_payload = encode_activation_readiness_ack(
            acknowledgement,
            adaptive_v3_config->readiness_wire_limits);
        return std::make_unique<AdaptiveV3RetiredActivationReceipt>(
            AdaptiveV3RetiredActivationReceipt{
                certificate.identity,
                certificate.certificate_digest,
                canonical_certificate_payload,
                std::move(acknowledgement_payload)});
    }

    void HotStuffBase::publish_adaptive_v3_activation(
        const EpochRuntimeUpdate &update,
        std::unique_ptr<AdaptiveV3RetiredActivationReceipt>
            prepared_receipt,
        std::unique_ptr<AdaptiveV3ReadinessStructuredEvent>
            accepted_event) noexcept
    {
        if (!adaptive_v3_activation_gate || !prepared_receipt ||
            adaptive_epoch_runtime == nullptr)
            return;

        // The live binding has already installed update. Ownership transfer
        // and current-cycle retirement are therefore deliberately allocation
        // free and precede every drain, event, or network side effect.
        adaptive_v3_retired_activation_receipt.swap(prepared_receipt);
        const auto scheduled_height = adaptive_v3_activation_gate
            ->scheduled_readiness_height().value_or(0);
        const auto applied_height = adaptive_v3_activation_gate
            ->certificate_apply_committed_height();
        const auto certificate_digest =
            adaptive_v3_retired_activation_receipt->certificate_digest;
        if (adaptive_v2_rotation_coordinator != nullptr)
            adaptive_v2_rotation_coordinator->reset_for_activation();
        adaptive_v3_activation_gate.reset();
        adaptive_v3_committed_command.reset();
        adaptive_v3_pending_observation.reset();
        adaptive_v3_signed_observation.reset();
        adaptive_v3_deferred_observation.reset();
        adaptive_v3_accepted_certificate.reset();
        adaptive_v3_deferred_certificate.reset();
        adaptive_v3_prepared_activation_receipt.reset();
        adaptive_v3_boundary_block = nullptr;
        adaptive_v3_latest_committed_block = nullptr;
        adaptive_v3_runtime_prepared = false;
        adaptive_v3_observation_attempts = 0;
        adaptive_v3_observation_retry_exhausted = false;
        adaptive_v3_observation_terminal = false;
        adaptive_v3_certificate_ack_sent = false;
        adaptive_v3_command_evidence.reset();
        adaptive_v3_command_terminal_emitted = false;

        cancel_adaptive_v3_observation_retry();
        reset_committed_epoch_definition_recovery();
        if (accepted_event)
            emit_adaptive_v3_readiness_event(
                std::move(*accepted_event));
        const auto drain =
            adaptive_epoch_runtime->adapter.drain_activated_futures();
        if (adaptive_v2_command_inbox)
            static_cast<void>(
                adaptive_v2_command_inbox->observe_activation(
                    update.activation.configuration,
                    update.activation.generation));
        emit_epoch_lifecycle_event(
            EpochLifecycleTransition::activated,
            update.activation.configuration,
            scheduled_height,
            applied_height,
            certificate_digest);
        if (drain.status != EpochFutureDrainStatus::complete)
            HOTSTUFF_LOG_WARN(
                "[EPOCH] Adaptive-v3 future proposal drain incomplete");
        adaptive_v3_certificate_ack_sent =
            transmit_adaptive_v3_acknowledgement(
                adaptive_v3_retired_activation_receipt
                    ->canonical_acknowledgement_payload);
    }

    void HotStuffBase::adaptive_v3_readiness_certificate_handler(
        MsgActivationReadinessCertificate &&message,
        const Net::conn_t &connection)
    {
        if (epoch_protocol_mode != EpochProtocolMode::adaptive_v3 ||
            !adaptive_v3_config || connection == nullptr ||
            !epoch_manager_peer)
            return;
        const auto pinned = pn.get_peer_conn(*epoch_manager_peer);
        const auto *certificate = connection->get_peer_cert();
        if (pinned == nullptr || pinned != connection ||
            certificate == nullptr ||
            PeerId(*certificate) != *epoch_manager_peer)
            return;
        const auto payload = static_cast<bytearray_t>(message.serialized);
        const auto decoded = decode_activation_readiness_certificate(
            payload, adaptive_v3_config->readiness_wire_limits);
        if (!decoded)
        {
            // TLS has authenticated the manager peer, but no identity can be
            // recovered from malformed certificate wire.  Seal raw evidence
            // before returning so a later valid certificate cannot erase it.
            try
            {
                AdaptiveV3ReadinessStructuredEvent event;
                event.transition = AdaptiveV3ReadinessTransition::wire_rejected;
                event.replica_id = get_id();
                event.wire_opcode = MsgActivationReadinessCertificate::opcode;
                event.wire_payload_size = payload.size();
                event.payload_digest = DataStream(payload).get_hash();
                event.canonical_wire_payload = payload;
                event.disposition = "certificate_decode";
                if (!emit_adaptive_v3_readiness_event(
                        std::move(event), true))
                    fail_adaptive_v3_readiness_audit();
            }
            catch (...)
            {
                fail_adaptive_v3_readiness_audit();
            }
            return;
        }
        if (adaptive_v3_retired_activation_receipt &&
            decoded.value->identity ==
                adaptive_v3_retired_activation_receipt->identity &&
            decoded.value->certificate_digest ==
                adaptive_v3_retired_activation_receipt
                    ->certificate_digest &&
            payload == adaptive_v3_retired_activation_receipt
                ->canonical_certificate_payload)
        {
            adaptive_v3_certificate_ack_sent =
                transmit_adaptive_v3_acknowledgement(
                    adaptive_v3_retired_activation_receipt
                        ->canonical_acknowledgement_payload);
            return;
        }
        if (!adaptive_v3_activation_gate)
            return;
        if (adaptive_v3_observation_terminal ||
            adaptive_v3_command_terminal_emitted)
            return;
        if (epoch_live_binding == nullptr)
            return;
        if (!adaptive_v3_runtime_prepared)
        {
            if (!adaptive_v3_deferred_certificate)
                adaptive_v3_deferred_certificate = *decoded.value;
            else if (encode_activation_readiness_certificate(
                         *adaptive_v3_deferred_certificate,
                         adaptive_v3_config->readiness_wire_limits) !=
                     payload)
                emit_adaptive_v3_observation_terminal(
                    adaptive_v3_deferred_certificate->identity,
                    "observation_internal_failure");
            return;
        }
        ingest_adaptive_v3_readiness_certificate(*decoded.value, payload);
    }

    void HotStuffBase::ingest_adaptive_v3_readiness_certificate(
        const AdaptiveV3ActivationReadinessCertificate &certificate,
        const bytearray_t &canonical_payload) noexcept
    {
        if (!adaptive_v3_runtime_prepared ||
            !adaptive_v3_activation_gate || epoch_live_binding == nullptr ||
            adaptive_v3_observation_terminal ||
            adaptive_v3_command_terminal_emitted)
            return;
        std::unique_ptr<AdaptiveV3RetiredActivationReceipt>
            prepared_receipt;
        std::unique_ptr<ActivationReadinessCertificateV1>
            accepted_certificate;
        std::unique_ptr<AdaptiveV3ReadinessStructuredEvent> event;
        try
        {
            prepared_receipt = prepare_adaptive_v3_retirement(
                certificate, canonical_payload);
            accepted_certificate =
                std::make_unique<ActivationReadinessCertificateV1>(
                    certificate);
            event = std::make_unique<AdaptiveV3ReadinessStructuredEvent>();
            event->identity = certificate.identity;
            event->replica_id = get_id();
            event->certificate_digest = certificate.certificate_digest;
            event->payload_digest = activation_readiness_ack_payload_digest(
                MsgActivationReadinessCertificate::opcode,
                canonical_payload);
            event->canonical_wire_payload = canonical_payload;
        }
        catch (...)
        {
            emit_adaptive_v3_observation_terminal(
                certificate.identity, "observation_internal_failure");
            return;
        }
        const auto result = epoch_live_binding
            ->apply_v3_readiness_certificate(
                *adaptive_v3_activation_gate, certificate);
        if (result.error == EpochIngressError::none &&
            (result.disposition == AdaptiveV3CertificateDisposition::accepted ||
             result.disposition ==
                 AdaptiveV3CertificateDisposition::buffered_early ||
             result.disposition == AdaptiveV3CertificateDisposition::duplicate))
        {
            event->transition =
                AdaptiveV3ReadinessTransition::certificate_accepted;
            if (result.disposition ==
                AdaptiveV3CertificateDisposition::buffered_early)
            {
                adaptive_v3_accepted_certificate =
                    std::move(accepted_certificate);
                adaptive_v3_prepared_activation_receipt =
                    std::move(prepared_receipt);
                emit_adaptive_v3_readiness_event(std::move(*event));
                return;
            }
            if (result.disposition !=
                AdaptiveV3CertificateDisposition::buffered_early)
            {
                cancel_adaptive_v3_observation_retry();
                adaptive_v3_pending_observation.reset();
                if (result.update)
                    publish_adaptive_v3_activation(
                        *result.update,
                        std::move(prepared_receipt),
                        std::move(event));
                else
                {
                    adaptive_v3_accepted_certificate =
                        std::move(accepted_certificate);
                    emit_adaptive_v3_readiness_event(std::move(*event));
                    acknowledge_adaptive_v3_certificate();
                }
            }
            return;
        }
        event->transition =
            AdaptiveV3ReadinessTransition::certificate_rejected;
        event->disposition = "rejected";
        emit_adaptive_v3_readiness_event(std::move(*event));
    }

    void HotStuffBase::process_adaptive_v3_post_block_commit(
        const block_t &block,
        const std::optional<ProposalKey> &committed_key,
        std::optional<std::uint64_t> committed_generation) noexcept
    {
        if (epoch_protocol_mode != EpochProtocolMode::adaptive_v3 ||
            !adaptive_v3_config || block == nullptr ||
            adaptive_epoch_runtime == nullptr ||
            epoch_live_binding == nullptr || exact_epochs == nullptr ||
            adaptive_v3_observation_terminal ||
            adaptive_v3_command_terminal_emitted)
            return;
        try
        {
            adaptive_v3_latest_committed_block = block;
            if (pending_committed_epoch_change &&
                pending_committed_epoch_change->block_hash ==
                    block->get_hash())
            {
                const auto command =
                    pending_committed_epoch_change->command;
                const auto payload_digest =
                    pending_committed_epoch_change->payload_digest;
                pending_committed_epoch_change.reset();
                const auto raw_activation_height =
                    command.payload.activation_delay_blocks != 0 &&
                    block->get_height() <=
                        std::numeric_limits<std::uint64_t>::max() -
                            command.payload.activation_delay_blocks
                    ? block->get_height() +
                        command.payload.activation_delay_blocks
                    : 0;
                adaptive_v3_command_evidence =
                    EpochCommandCommittedStructuredEvent{
                        block->get_height(),
                        block->get_hash(),
                        command.payload.successor_epoch_number == 0
                            ? 0
                            : command.payload.successor_epoch_number - 1,
                        command.payload.predecessor_epoch_digest,
                        command.payload.successor_epoch_number,
                        command.payload.successor_epoch_digest,
                        payload_digest,
                        command.payload.activation_delay_blocks,
                        raw_activation_height};
                adaptive_v3_command_terminal_emitted = false;
                if (command.protocol_mode !=
                        EpochProtocolMode::adaptive_v3 ||
                    command.schema_version !=
                        kEpochChangeSchemaVersionV2 ||
                    (adaptive_v3_retired_activation_receipt &&
                     payload_digest ==
                         adaptive_v3_retired_activation_receipt->identity
                             .command_payload_digest) ||
                    adaptive_v3_activation_gate ||
                    adaptive_v3_committed_command ||
                    command.payload.activation_delay_blocks == 0 ||
                    block->get_height() >
                        std::numeric_limits<std::uint64_t>::max() -
                            command.payload.activation_delay_blocks)
                {
                    emit_adaptive_v3_command_terminal(
                        AdaptiveV3CommandTerminalReason::
                            invalid_committed_command);
                    return;
                }
                const auto active =
                    adaptive_epoch_runtime->activation.active_effect();
                if (active.definition == nullptr ||
                    active.configuration.epoch_digest !=
                        command.payload.predecessor_epoch_digest ||
                    active.configuration.epoch_number ==
                        std::numeric_limits<std::uint32_t>::max() ||
                    command.payload.successor_epoch_number !=
                        active.configuration.epoch_number + 1)
                {
                    emit_adaptive_v3_command_terminal(
                        AdaptiveV3CommandTerminalReason::
                            wrong_active_predecessor);
                    return;
                }
                const auto successor_generation =
                    checked_activation_generation(
                        command.payload.successor_epoch_number, 0);
                if (!successor_generation)
                {
                    emit_adaptive_v3_command_terminal(
                        AdaptiveV3CommandTerminalReason::
                            invalid_successor_generation);
                    return;
                }
                const auto activation_height =
                    block->get_height() +
                    command.payload.activation_delay_blocks;
                AdaptiveV3ActivationSchedule schedule{
                    active.definition->membership_digest(),
                    active.configuration.epoch_number,
                    active.configuration.epoch_digest,
                    command.payload.successor_epoch_number,
                    command.payload.successor_epoch_digest,
                    *successor_generation,
                    payload_digest,
                    block->get_height(),
                    block->get_hash(),
                    command.payload.activation_delay_blocks,
                    activation_height};
                const auto member_count =
                    adaptive_v3_config->readiness_membership.size();
                const auto fixed_quorum =
                    2 * ((member_count - 1) / 3) + 1;
                adaptive_v3_activation_gate =
                    std::make_unique<AdaptiveV3CertifiedActivationGate>(
                        schedule,
                        get_id(),
                        adaptive_v3_config
                            ->local_readiness_private_key,
                        adaptive_v3_config->readiness_membership,
                        fixed_quorum);
                adaptive_v3_committed_command = command;
                adaptive_v3_observation_attempts = 0;
                adaptive_v3_observation_retry_exhausted = false;
                adaptive_v3_observation_terminal = false;
                adaptive_v3_certificate_ack_sent = false;
                const auto *successor = exact_epochs->find_epoch_by_digest(
                    command.payload.successor_epoch_digest);
                if (successor != nullptr)
                {
                    if (adaptive_epoch_runtime->adapter.prepare_committed_v3(
                            *successor) != EpochIngressError::none)
                    {
                        emit_adaptive_v3_command_terminal(
                            AdaptiveV3CommandTerminalReason::
                                successor_runtime_preparation_failed);
                        return;
                    }
                    adaptive_v3_runtime_prepared = true;
                }
                const ActivationRecord record{
                    active.configuration.epoch_number,
                    active.configuration.epoch_digest,
                    command.payload.successor_epoch_number,
                    command.payload.successor_epoch_digest,
                    payload_digest,
                    block->get_height(),
                    command.payload.activation_delay_blocks,
                    activation_height};
                emit_epoch_command_committed_event(block, command, record);
                if (successor == nullptr &&
                    !retain_committed_epoch_definition_recovery(
                        block, command, record))
                {
                    emit_adaptive_v3_command_terminal(
                        AdaptiveV3CommandTerminalReason::
                            committed_definition_recovery_failed);
                    return;
                }
            }

            if (!adaptive_v3_activation_gate)
                return;
            const auto scheduled = adaptive_v3_activation_gate
                                       ->scheduled_readiness_height();
            if (scheduled && block->get_height() == *scheduled)
                adaptive_v3_boundary_block = block;
            const auto active =
                adaptive_epoch_runtime->activation.active_effect();
            auto predecessor_configuration = active.configuration;
            auto predecessor_generation = active.generation;
            auto source_sequence = std::uint64_t{0};
            if (scheduled && block->get_height() == *scheduled &&
                !adaptive_v3_signed_observation &&
                !adaptive_v3_deferred_observation)
            {
                if (!committed_key.has_value() ||
                    !committed_generation.has_value() ||
                    committed_key->block_hash != block->get_hash() ||
                    committed_key->configuration.epoch_number !=
                        active.configuration.epoch_number ||
                    committed_key->configuration.epoch_digest !=
                        active.configuration.epoch_digest ||
                    *committed_generation > active.generation ||
                    (*committed_generation == active.generation &&
                     committed_key->configuration !=
                         active.configuration) ||
                    exact_epochs->find_tree(
                        committed_key->configuration.epoch_number,
                        committed_key->configuration.tree_id) == nullptr)
                {
                    emit_adaptive_v3_command_terminal(
                        AdaptiveV3CommandTerminalReason::
                            readiness_boundary_rejected);
                    return;
                }
                predecessor_configuration =
                    committed_key->configuration;
                predecessor_generation = *committed_generation;
                if (adaptive_v3_readiness_source_sequence ==
                    std::numeric_limits<std::uint64_t>::max())
                {
                    emit_adaptive_v3_command_terminal(
                        AdaptiveV3CommandTerminalReason::
                            readiness_source_sequence_exhausted);
                    return;
                }
                source_sequence =
                    ++adaptive_v3_readiness_source_sequence;
            }
            const auto result =
                epoch_live_binding->on_v3_post_block_commit(
                    *adaptive_v3_activation_gate,
                    block->get_height(),
                    predecessor_configuration,
                    predecessor_generation,
                    block->get_hash(),
                    source_sequence,
                    adaptive_evidence_monotonic_now_ns());
            if (result.error != EpochIngressError::none ||
                result.boundary.disposition ==
                    AdaptiveV3BoundaryDisposition::rejected)
            {
                emit_adaptive_v3_command_terminal(
                    AdaptiveV3CommandTerminalReason::
                        readiness_boundary_rejected);
                return;
            }
            if (result.boundary.observation)
            {
                AdaptiveV3ReadinessStructuredEvent prepared;
                prepared.transition =
                    AdaptiveV3ReadinessTransition::activation_prepared;
                prepared.identity = result.boundary.observation->identity;
                prepared.replica_id = get_id();
                emit_adaptive_v3_readiness_event(std::move(prepared));
                emit_epoch_lifecycle_event(
                    EpochLifecycleTransition::activation_armed,
                    active.configuration,
                    scheduled.value_or(block->get_height()));
                if (adaptive_v3_runtime_prepared)
                    enqueue_adaptive_v3_observation(
                        *result.boundary.observation);
                else
                    adaptive_v3_deferred_observation =
                        *result.boundary.observation;
            }
            if (result.update)
                publish_adaptive_v3_activation(
                    *result.update,
                    std::move(
                        adaptive_v3_prepared_activation_receipt));
        }
        catch (...)
        {
            if (adaptive_v3_signed_observation)
                emit_adaptive_v3_observation_terminal(
                    adaptive_v3_signed_observation->identity,
                    "observation_internal_failure");
            else if (adaptive_v3_deferred_observation)
                emit_adaptive_v3_observation_terminal(
                    adaptive_v3_deferred_observation->identity,
                    "observation_internal_failure");
            else if (adaptive_v3_accepted_certificate)
                emit_adaptive_v3_observation_terminal(
                    adaptive_v3_accepted_certificate->identity,
                    "observation_internal_failure");
            else
                emit_adaptive_v3_command_terminal(
                    AdaptiveV3CommandTerminalReason::
                        readiness_internal_failure);
            HOTSTUFF_LOG_WARN(
                "[EPOCH] Failed adaptive-v3 post-block commit processing");
        }
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
        const auto source = authenticated_peer.source_peer.to_hex();
        HOTSTUFF_LOG_INFO(
            "KAURI_PROPOSAL_INGRESS stage=begin recipient=%u "
            "source_replica=%u source_peer=%s",
            static_cast<unsigned>(get_id()),
            static_cast<unsigned>(
                authenticated_peer.replica_id.value_or(0)),
            source.c_str());
        const auto result = epoch_live_binding->handle_proposal(
            std::move(message), authenticated_peer);
        const auto *envelope = result.decoded_envelope.has_value()
            ? &*result.decoded_envelope
            : nullptr;
        if (envelope != nullptr &&
            authenticated_peer.replica_id.has_value() &&
            result.error == EpochIngressError::none &&
            result.admission_disposition.has_value() &&
            (*result.admission_disposition ==
                 ProposalDisposition::admitted_active ||
             *result.admission_disposition ==
                 ProposalDisposition::duplicate))
            observe_exact_root_repair_delivery(
                *envelope,
                *authenticated_peer.replica_id,
                *result.admission_disposition);
        if (envelope != nullptr &&
            result.permission ==
                EpochConsensusPermission::catch_up_only)
            static_cast<void>(process_exact_proposal_catchup(
                *envelope, authenticated_peer.source_peer));
        const auto block = envelope != nullptr
            ? envelope->block_hash.to_hex()
            : std::string{"none"};
        HOTSTUFF_LOG_INFO(
            "KAURI_PROPOSAL_INGRESS stage=result recipient=%u "
            "source_replica=%u "
            "source_peer=%s error=%u wire_error=%u permission=%u "
            "disposition=%d envelope=%u epoch=%u tree=%u block=%s "
            "generation=%llu",
            static_cast<unsigned>(get_id()),
            static_cast<unsigned>(
                authenticated_peer.replica_id.value_or(0)),
            source.c_str(),
            static_cast<unsigned>(result.error),
            static_cast<unsigned>(result.wire_error),
            static_cast<unsigned>(result.permission),
            result.admission_disposition.has_value()
                ? static_cast<int>(*result.admission_disposition)
                : -1,
            envelope != nullptr ? 1U : 0U,
            envelope != nullptr
                ? envelope->configuration.epoch_number
                : 0U,
            envelope != nullptr
                ? envelope->configuration.tree_id
                : 0U,
            block.c_str(),
            static_cast<unsigned long long>(
                envelope != nullptr ? envelope->view_generation : 0));
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
        const auto source_replica =
            authenticated_peer.replica_id.value_or(0);
        HOTSTUFF_LOG_INFO(
            "KAURI_RELAY_INGRESS stage=begin recipient=%u "
            "source_replica=%u",
            static_cast<unsigned>(get_id()),
            static_cast<unsigned>(source_replica));
        const auto result = epoch_live_binding->handle_relay(
            std::move(message), authenticated_peer);
        const auto *envelope = result.decoded_envelope.has_value()
            ? &*result.decoded_envelope
            : nullptr;
        const auto block = envelope != nullptr
            ? envelope->block_hash.to_hex()
            : std::string{"none"};
        const bool dispatched =
            result.permission ==
                EpochConsensusPermission::accept_contribution &&
            envelope != nullptr;
        HOTSTUFF_LOG_INFO(
            "KAURI_RELAY_INGRESS stage=result recipient=%u "
            "source_replica=%u root=%u error=%u wire_error=%u "
            "permission=%u envelope=%u epoch=%u tree=%u block=%s "
            "generation=%llu",
            static_cast<unsigned>(get_id()),
            static_cast<unsigned>(source_replica),
            static_cast<unsigned>(
                envelope != nullptr ? envelope->proposer : 0),
            static_cast<unsigned>(result.error),
            static_cast<unsigned>(result.wire_error),
            static_cast<unsigned>(result.permission),
            envelope != nullptr ? 1U : 0U,
            envelope != nullptr
                ? envelope->configuration.epoch_number
                : 0U,
            envelope != nullptr
                ? envelope->configuration.tree_id
                : 0U,
            block.c_str(),
            static_cast<unsigned long long>(
                envelope != nullptr ? envelope->view_generation : 0));
        HOTSTUFF_LOG_INFO(
            "KAURI_RELAY_INGRESS stage=dispatch_complete recipient=%u "
            "source_replica=%u root=%u dispatched=%u",
            static_cast<unsigned>(get_id()),
            static_cast<unsigned>(source_replica),
            static_cast<unsigned>(
                envelope != nullptr ? envelope->proposer : 0),
            static_cast<unsigned>(dispatched));
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
            if (epoch_protocol_mode == EpochProtocolMode::adaptive_v2)
                return cert != nullptr &&
                       valid_tls_certs.count(
                           salticidae::get_hash(cert->get_der())) != 0;
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
                               EpochProtocolMode protocol_mode,
                               std::optional<AdaptiveV3RuntimeConfig> v3_config) : HotStuffCore(rid, std::move(priv_key)),
                                                          listen_addr(listen_addr),
                                                          blk_size(blk_size),
                                                          ec(ec),
                                                          tcall(ec),
                                                          vpool(ec, nworker),
                                                          pn(ec, netconfig),
                                                          epoch_protocol_mode(protocol_mode),
                                                          adaptive_v3_config(std::move(v3_config)),
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
        if ((epoch_protocol_mode == EpochProtocolMode::adaptive_v3) !=
            adaptive_v3_config.has_value())
            throw HotStuffError(
                "adaptive-v3 runtime requires one complete isolated configuration");
        if (adaptive_v3_config)
        {
            const auto &configured = *adaptive_v3_config;
            const auto member_count = configured.readiness_membership.size();
            if (configured.manager_peer.is_null() ||
                configured.manager_address.is_null() ||
                configured.local_readiness_private_key == nullptr ||
                member_count < 4 || (member_count - 1) % 3 != 0 ||
                configured.readiness_wire_limits.maximum_members !=
                    member_count ||
                configured.readiness_wire_limits.maximum_payload_bytes == 0 ||
                configured.maximum_block_extra_bytes == 0 ||
                configured.maximum_ancestry_blocks == 0 ||
                configured.maximum_bundle_bytes == 0 ||
                configured.maximum_block_extra_bytes >
                    configured.maximum_bundle_bytes ||
                configured.maximum_observation_attempts == 0 ||
                configured.observation_retry_interval_ms == 0 ||
                rid >= member_count)
                throw HotStuffError(
                    "adaptive-v3 runtime configuration is incomplete");
            std::set<std::string> public_keys;
            for (std::size_t index = 0; index < member_count; ++index)
            {
                const auto &member = configured.readiness_membership[index];
                if (member.replica_id != static_cast<ReplicaID>(index) ||
                    !public_keys.insert(
                        salticidae::get_hex(member.public_key.to_bytes()))
                         .second)
                    throw HotStuffError(
                        "adaptive-v3 readiness membership is not canonical");
            }
            if (configured.readiness_membership[rid].public_key.to_bytes() !=
                PubKeyBLS(*configured.local_readiness_private_key).to_bytes())
                throw HotStuffError(
                    "adaptive-v3 local readiness key does not match membership");
            epoch_manager_peer = configured.manager_peer;
            epoch_manager_address = configured.manager_address;
            epoch_change_maximum_block_extra_bytes =
                configured.maximum_block_extra_bytes;
            epoch_change_maximum_ancestry_blocks =
                configured.maximum_ancestry_blocks;
            adaptive_v2_epoch_change_bundle_limits = EpochChangeBundleLimits{
                configured.maximum_bundle_bytes,
                configured.maximum_block_extra_bytes,
                epoch_wire_limits};
            epoch_change_verifier = std::make_unique<EpochChangeVerifier>(
                configured.epoch_change_issuer,
                configured.epoch_change_delay_bounds,
                EpochProtocolMode::adaptive_v3);
            adaptive_v2_command_inbox =
                std::make_unique<AdaptiveV2CommandInbox>(
                    AdaptiveV2CommandInboxLimits{
                        configured.maximum_bundle_bytes,
                        configured.maximum_block_extra_bytes,
                        std::numeric_limits<std::uint64_t>::max()},
                    EpochProtocolMode::adaptive_v3);
            valid_tls_certs.insert(
                static_cast<const uint256_t &>(configured.manager_peer));
        }

        initialize_committed_epoch_change_history();
        if (epoch_protocol_mode == EpochProtocolMode::adaptive_v2)
        {
            AdaptiveV2ReportingOutboxConfig reporting_config;
            reporting_config.source_replica_id = get_id();
            reporting_config.limits.maximum_delivery_attempts =
                adaptive_v2_reporting_maximum_delivery_attempts;
            reporting_config.limits.initial_retry_backoff_ns =
                static_cast<std::uint64_t>(
                    std::chrono::duration_cast<std::chrono::nanoseconds>(
                        adaptive_v2_evidence_retry_delay).count());
            reporting_config.limits.maximum_retry_backoff_ns =
                static_cast<std::uint64_t>(
                    std::chrono::duration_cast<std::chrono::nanoseconds>(
                        adaptive_v2_reporting_maximum_retry_delay).count());
            adaptive_v2_reporting_outbox =
                std::make_unique<AdaptiveV2ReportingOutbox>(
                    std::move(reporting_config));
        }
        if (epoch_protocol_mode == EpochProtocolMode::adaptive_v2)
        {
            adaptive_v2_response_evidence =
                std::make_unique<AdaptiveV2ResponseEvidenceBridge>(
                    get_id());
            adaptive_v2_response_evidence
                ->bind_deadline_result_callback(
                    [access = exact_runtime_access](
                        const ProposalKey &key,
                        EvidenceDeadlineResult result) {
                        auto runtime = access->acquire();
                        if (!runtime.has_value())
                            return;
                        runtime->owner()
                            .observe_adaptive_v2_response_deadline_result(
                                key, result);
                    });
            adaptive_v2_response_evidence->bind_deadline_scheduler(
                [this](
                    const ProposalKey &,
                    std::uint64_t deadline_duration_us,
                    EvidenceDeadlineCallback deadline,
                    EvidenceDeadlineFailureCallback failure) {
                    constexpr std::uint64_t nanoseconds_per_microsecond =
                        1000;
                    const auto maximum_delay = static_cast<std::uint64_t>(
                        AggregationScheduler::Duration::max().count());
                    if (deadline_duration_us == 0 ||
                        deadline_duration_us >
                            maximum_delay /
                                nanoseconds_per_microsecond)
                        return EvidenceDeadlineCancellation{};
                    const auto delay = AggregationScheduler::Duration(
                        static_cast<
                            AggregationScheduler::Duration::rep>(
                            deadline_duration_us *
                            nanoseconds_per_microsecond));
                    const auto now =
                        aggregation_scheduler->monotonic_now();
                    if (delay <=
                            AggregationScheduler::Duration::zero() ||
                        now >
                            AggregationScheduler::Duration::max() - delay)
                        return EvidenceDeadlineCancellation{};
                    const auto access = exact_runtime_access;
                    return schedule_at_or_after_deadline(
                        *aggregation_scheduler,
                        now + delay,
                        [access,
                         deadline = std::move(deadline)]() mutable {
                            auto runtime = access->acquire();
                            if (!runtime.has_value())
                                return;
                            deadline(
                                adaptive_evidence_monotonic_now_ns());
                        },
                        [access,
                         failure = std::move(failure)]() mutable {
                            auto runtime = access->acquire();
                            if (!runtime.has_value())
                                return;
                            failure();
                        });
                });
            adaptive_v2_response_evidence->bind_retry_scheduler(
                [this](EvidenceRetryCallback retry) {
                    const auto access = exact_runtime_access;
                    return aggregation_scheduler->schedule_after(
                        adaptive_v2_evidence_retry_delay,
                        [access, retry = std::move(retry)]() mutable {
                            auto runtime = access->acquire();
                            if (!runtime.has_value())
                                return;
                            retry();
                        });
                });
        }
        else if (epoch_protocol_mode == EpochProtocolMode::adaptive_v3)
        {
            // V3 reuses the bounded reporting FIFO for the common initial
            // operational-readiness notice, exact lifecycle, and schema-v3
            // response-evidence notices only. V2 convergence, durable
            // initialization, and durable commit state remain absent here.
            AdaptiveV2ReportingOutboxConfig reporting_config;
            reporting_config.source_replica_id = get_id();
            reporting_config.limits.maximum_delivery_attempts =
                adaptive_v2_reporting_maximum_delivery_attempts;
            reporting_config.limits.initial_retry_backoff_ns =
                static_cast<std::uint64_t>(
                    std::chrono::duration_cast<std::chrono::nanoseconds>(
                        adaptive_v2_evidence_retry_delay).count());
            reporting_config.limits.maximum_retry_backoff_ns =
                static_cast<std::uint64_t>(
                    std::chrono::duration_cast<std::chrono::nanoseconds>(
                        adaptive_v2_reporting_maximum_retry_delay).count());
            adaptive_v2_reporting_outbox =
                std::make_unique<AdaptiveV2ReportingOutbox>(
                    std::move(reporting_config));
            adaptive_v2_response_evidence =
                std::make_unique<AdaptiveV2ResponseEvidenceBridge>(
                    get_id());
            adaptive_v2_response_evidence->bind_deadline_scheduler(
                [this](
                    const ProposalKey &,
                    std::uint64_t deadline_duration_us,
                    EvidenceDeadlineCallback deadline,
                    EvidenceDeadlineFailureCallback failure) {
                    constexpr std::uint64_t nanoseconds_per_microsecond =
                        1000;
                    const auto maximum_delay = static_cast<std::uint64_t>(
                        AggregationScheduler::Duration::max().count());
                    if (deadline_duration_us == 0 ||
                        deadline_duration_us >
                            maximum_delay / nanoseconds_per_microsecond)
                        return EvidenceDeadlineCancellation{};
                    const auto delay = AggregationScheduler::Duration(
                        static_cast<AggregationScheduler::Duration::rep>(
                            deadline_duration_us *
                            nanoseconds_per_microsecond));
                    const auto now = aggregation_scheduler->monotonic_now();
                    if (delay <= AggregationScheduler::Duration::zero() ||
                        now > AggregationScheduler::Duration::max() - delay)
                        return EvidenceDeadlineCancellation{};
                    const auto access = exact_runtime_access;
                    return schedule_at_or_after_deadline(
                        *aggregation_scheduler,
                        now + delay,
                        [access,
                         deadline = std::move(deadline)]() mutable {
                            auto runtime = access->acquire();
                            if (!runtime.has_value())
                                return;
                            deadline(adaptive_evidence_monotonic_now_ns());
                        },
                        [access,
                         failure = std::move(failure)]() mutable {
                            auto runtime = access->acquire();
                            if (!runtime.has_value())
                                return;
                            failure();
                        });
                });
            adaptive_v2_response_evidence->bind_retry_scheduler(
                [this](EvidenceRetryCallback retry) {
                    const auto access = exact_runtime_access;
                    return aggregation_scheduler->schedule_after(
                        adaptive_v2_evidence_retry_delay,
                        [access, retry = std::move(retry)]() mutable {
                            auto runtime = access->acquire();
                            if (!runtime.has_value())
                                return;
                            retry();
                        });
                });
            adaptive_v2_response_evidence->bind_transport(
                [this](const EvidenceReportEnvelope &report) {
                    return enqueue_adaptive_v2_evidence_report(report);
                });
        }
        rebuild_aggregation_timeout_coordinator();

        /* register the handlers for msg from replicas */
        if (epoch_protocol_mode == EpochProtocolMode::adaptive_v1)
        {
            install_adaptive_epoch_handlers();
            install_adaptive_consensus_handlers();
        }
        else if (epoch_protocol_mode == EpochProtocolMode::adaptive_v2)
        {
            install_adaptive_consensus_handlers();
            install_adaptive_v2_definition_handlers();
        }
        else if (epoch_protocol_mode == EpochProtocolMode::adaptive_v3)
        {
            install_adaptive_consensus_handlers();
            install_adaptive_v3_handlers();
        }
        else
            install_legacy_consensus_handlers();
        if (adaptive_v3_config)
        {
            pn.add_peer(adaptive_v3_config->manager_peer);
            pn.set_peer_addr(
                adaptive_v3_config->manager_peer,
                adaptive_v3_config->manager_address);
        }
        pn.reg_handler(salticidae::generic_bind(&HotStuffBase::req_blk_handler, this, _1, _2));
        pn.reg_handler(salticidae::generic_bind(&HotStuffBase::resp_blk_handler, this, _1, _2));
        pn.reg_conn_handler(salticidae::generic_bind(&HotStuffBase::conn_handler, this, _1, _2));
        pn.start();
        pn.listen(listen_addr);
        if (adaptive_v3_config)
            pn.conn_peer(adaptive_v3_config->manager_peer);

        if (epoch_protocol_mode != EpochProtocolMode::adaptive_v2 &&
            epoch_protocol_mode != EpochProtocolMode::adaptive_v3)
        {
            rn.start();
            reputation_server_conn = rn.connect_sync(reputation_addr);
        }
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
    }

    void HotStuffBase::install_adaptive_consensus_handlers()
    {
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

    void HotStuffBase::install_adaptive_v2_definition_handlers()
    {
        static_assert(
            MsgAdaptiveV2ConvergenceObservationAck::opcode == 0x1E,
            "adaptive-v2 convergence ACK opcode must stay registered");
        static_assert(
            MsgExperimentPostQcAuditRelay::opcode == 0x1F,
            "experiment post-QC audit opcode must stay distinct");
        pn.reg_handler(salticidae::generic_bind(
            &HotStuffBase::adaptive_v2_epoch_change_bundle_handler,
            this, _1, _2));
        pn.reg_handler(salticidae::generic_bind(
            &HotStuffBase::adaptive_v2_convergence_ack_handler,
            this, _1, _2));
        pn.reg_handler(salticidae::generic_bind(
            &HotStuffBase::adaptive_definition_request_handler,
            this, _1, _2));
        pn.reg_handler(salticidae::generic_bind(
            &HotStuffBase::adaptive_definition_reply_handler,
            this, _1, _2));
        pn.reg_handler(salticidae::generic_bind(
            &HotStuffBase::experiment_post_qc_audit_relay_handler,
            this, _1, _2));
    }

    void HotStuffBase::install_adaptive_v3_handlers()
    {
        static_assert(
            MsgAdaptiveV3EpochChangeBundle::opcode == 0x20 &&
            MsgActivationReadyObservation::opcode == 0x21 &&
            MsgActivationReadinessCertificate::opcode == 0x22 &&
            MsgActivationReadinessAck::opcode == 0x23,
            "adaptive-v3 opcodes must remain isolated");
        pn.reg_handler(salticidae::generic_bind(
            &HotStuffBase::adaptive_v3_epoch_change_bundle_handler,
            this, _1, _2));
        pn.reg_handler(salticidae::generic_bind(
            &HotStuffBase::adaptive_v3_readiness_certificate_handler,
            this, _1, _2));
        pn.reg_handler(salticidae::generic_bind(
            &HotStuffBase::adaptive_definition_request_handler,
            this, _1, _2));
        pn.reg_handler(salticidae::generic_bind(
            &HotStuffBase::adaptive_definition_reply_handler,
            this, _1, _2));
    }

    bool HotStuffBase::authorize_manager_peer(
        const PeerId &peer) const noexcept
    {
        return is_adaptive_epoch_mode(epoch_protocol_mode) &&
               epoch_manager_peer.has_value() && !peer.is_null() &&
               peer == *epoch_manager_peer;
    }

    void HotStuffBase::adaptive_v2_convergence_ack_handler(
        MsgAdaptiveV2ConvergenceObservationAck &&message,
        const Net::conn_t &connection)
    {
        if (epoch_protocol_mode != EpochProtocolMode::adaptive_v2 ||
            adaptive_v2_reporting_outbox == nullptr ||
            !epoch_manager_peer.has_value() || connection == nullptr)
            return;

        const auto manager_peer = *epoch_manager_peer;
        if (!authorize_manager_peer(manager_peer))
            return;
        const auto pinned_connection = pn.get_peer_conn(manager_peer);
        const auto *manager_certificate = connection->get_peer_cert();
        if (pinned_connection == nullptr ||
            pinned_connection != connection ||
            manager_certificate == nullptr ||
            PeerId(*manager_certificate) != manager_peer)
            return;

        const AdaptiveV2ConvergenceWireLimits convergence_wire_limits;
        if (message.serialized.size() >
            convergence_wire_limits.maximum_payload_bytes)
            return;
        const auto canonical_ack =
            static_cast<bytearray_t>(message.serialized);
        const auto decoded =
            decode_adaptive_v2_convergence_observation_ack(
                canonical_ack, convergence_wire_limits);
        if (!decoded ||
            decoded.acknowledgement->target_replica_id != get_id())
            return;

        const auto *pending = adaptive_v2_reporting_outbox->front();
        if (pending == nullptr)
            return;
        const auto report_id = pending->report_id;
        const auto transition =
            adaptive_v2_reporting_outbox
                ->acknowledge_convergence_observation(
                    *decoded.acknowledgement);
        if (transition == AdaptiveV2ReportingTransitionStatus::delivered)
        {
            if (adaptive_v2_reporting_outbox->release_terminal(report_id) !=
                AdaptiveV2ReportingReleaseStatus::released)
                return;
            retry_ready_adaptive_v2_runtime_initialized_reports();
            retry_ready_adaptive_v2_commit_reports();
            enqueue_pending_adaptive_v2_commit_observation();
            enqueue_pending_adaptive_v2_activation_observation();
            schedule_adaptive_v2_reporting_flush(
                adaptive_v2_evidence_retry_delay);
        }
        else if (transition ==
                 AdaptiveV2ReportingTransitionStatus::failed)
        {
            poison_adaptive_v2_reporting(
                "convergence_observation_permanently_rejected");
        }
    }

    void HotStuffBase::configure_epoch_manager(
        const PeerId &manager_peer,
        const NetAddr &manager_address)
    {
        if (!is_adaptive_epoch_mode(epoch_protocol_mode) ||
            manager_peer.is_null())
            throw std::logic_error(
                "epoch manager is available only in adaptive mode");
        if (epoch_manager_peer.has_value() ||
            epoch_manager_address.has_value())
        {
            if (!epoch_manager_peer.has_value() ||
                !epoch_manager_address.has_value() ||
                *epoch_manager_peer != manager_peer ||
                *epoch_manager_address != manager_address)
                throw std::logic_error(
                    "epoch manager identity cannot be repinned");
            if (epoch_protocol_mode == EpochProtocolMode::adaptive_v2)
            {
                bind_adaptive_v2_manager_reporting_transport();
                schedule_adaptive_v2_reporting_flush(
                    adaptive_v2_evidence_retry_delay);
            }
            return;
        }
        epoch_manager_peer = manager_peer;
        epoch_manager_address = manager_address;
        valid_tls_certs.insert(
            static_cast<const uint256_t &>(manager_peer));
        pn.add_peer(manager_peer);
        pn.set_peer_addr(manager_peer, manager_address);
        pn.conn_peer(manager_peer);
        if (epoch_protocol_mode == EpochProtocolMode::adaptive_v2)
        {
            bind_adaptive_v2_manager_reporting_transport();
            schedule_adaptive_v2_reporting_flush(
                adaptive_v2_evidence_retry_delay);
        }
    }

    void HotStuffBase::bind_adaptive_v2_manager_reporting_transport()
    {
        if (epoch_protocol_mode != EpochProtocolMode::adaptive_v2 ||
            adaptive_v2_response_evidence == nullptr ||
            adaptive_v2_reporting_outbox == nullptr)
            return;
        adaptive_v2_response_evidence->bind_transport(
            [this](const EvidenceReportEnvelope &report) {
                return enqueue_adaptive_v2_evidence_report(report);
            });
    }

    EvidenceTransportResult HotStuffBase::enqueue_adaptive_v2_evidence_report(
        const EvidenceReportEnvelope &report) noexcept
    {
        if (adaptive_v2_reporting_outbox == nullptr ||
            adaptive_v2_lifecycle_reporting_suppressed)
            return EvidenceTransportResult::permanent_failure;

        if (epoch_protocol_mode == EpochProtocolMode::adaptive_v3)
        {
            const auto status =
                adaptive_v2_reporting_outbox->enqueue_evidence(
                    report.canonical_payload);
            if (report.observation.outcome == ResponseOutcome::timeout)
            {
                const auto diagnostics =
                    adaptive_v2_reporting_outbox->diagnostics();
                HOTSTUFF_LOG_INFO(
                    "[EVIDENCE] V3 timeout report enqueue "
                    "reporter=%u target=%u sequence=%llu status=%u "
                    "pending=%zu pending_bytes=%zu last_sequence=%llu "
                    "healthy=%u",
                    get_id(),
                    report.observation.observed_replica_id,
                    static_cast<unsigned long long>(
                        report.observation.reporter_sequence),
                    static_cast<unsigned>(status),
                    diagnostics.pending_reports,
                    diagnostics.pending_payload_bytes,
                    static_cast<unsigned long long>(
                        diagnostics.last_evidence_sequence),
                    diagnostics.healthy ? 1U : 0U);
            }
            if (status == AdaptiveV2ReportingEnqueueStatus::queued)
            {
                schedule_adaptive_v2_reporting_flush(
                    adaptive_v2_evidence_retry_delay);
                return EvidenceTransportResult::accepted;
            }
            if (status ==
                AdaptiveV2ReportingEnqueueStatus::capacity_exceeded)
                return EvidenceTransportResult::temporary_failure;
            poison_adaptive_v2_reporting("v3_evidence_enqueue_failed");
            return EvidenceTransportResult::permanent_failure;
        }
        if (epoch_protocol_mode != EpochProtocolMode::adaptive_v2)
            return EvidenceTransportResult::permanent_failure;

        const ProposalKey key{
            report.observation.configuration,
            report.observation.block_hash};
        const auto initialized =
            adaptive_v2_durable_initialization_reports.find(key);
        if (initialized ==
                adaptive_v2_durable_initialization_reports.end() ||
            initialized->second ==
                AdaptiveV2DurableInitializationPhase::suppressed)
        {
            suppress_adaptive_v2_lifecycle_reporting(
                "evidence_without_runtime_initialization");
            return EvidenceTransportResult::permanent_failure;
        }
        if (initialized->second ==
                AdaptiveV2DurableInitializationPhase::ready_to_enqueue &&
            !try_enqueue_adaptive_v2_runtime_initialized_report(key))
        {
            return adaptive_v2_lifecycle_reporting_suppressed
                       ? EvidenceTransportResult::permanent_failure
                       : EvidenceTransportResult::temporary_failure;
        }

        const auto status =
            adaptive_v2_reporting_outbox->enqueue_evidence(
                report.canonical_payload);
        if (status == AdaptiveV2ReportingEnqueueStatus::queued)
        {
            schedule_adaptive_v2_reporting_flush(
                adaptive_v2_evidence_retry_delay);
            return EvidenceTransportResult::accepted;
        }
        if (status ==
            AdaptiveV2ReportingEnqueueStatus::capacity_exceeded)
            return EvidenceTransportResult::temporary_failure;
        suppress_adaptive_v2_lifecycle_reporting(
            "evidence_enqueue_failed");
        return EvidenceTransportResult::permanent_failure;
    }

    void HotStuffBase::enqueue_initial_adaptive_v2_readiness() noexcept
    {
        if (!is_adaptive_epoch_mode(epoch_protocol_mode) ||
            adaptive_v2_readiness_enqueued ||
            adaptive_v2_reporting_outbox == nullptr ||
            adaptive_epoch_runtime == nullptr)
            return;
        try
        {
            const auto configuration = adaptive_epoch_runtime->activation
                                           .active_effect().configuration;
            if (configuration.epoch_number != 0 ||
                configuration.tree_id != 0)
                return;
            const auto generation =
                find_exact_runtime_generation(configuration);
            if (!generation.has_value() || *generation != 1)
                return;
            if (adaptive_v2_reporting_outbox->enqueue_readiness(
                    configuration, *generation, 0) !=
                AdaptiveV2ReportingEnqueueStatus::queued)
                return;
            adaptive_v2_readiness_enqueued = true;
            schedule_adaptive_v2_reporting_flush(
                adaptive_v2_evidence_retry_delay);
        }
        catch (...)
        {
            // Reporting is observational and cannot affect consensus startup.
        }
    }

    void HotStuffBase::report_adaptive_v2_runtime_initialized(
        const ProposalKey &key) noexcept
    {
        // V3 timeout evidence is proposal-lifecycle bound just like V2
        // evidence.  A proposal that cannot commit after the injected fault
        // still needs its authenticated initialization notice to reach the
        // manager; otherwise the exact timeout stays quarantined forever.
        // Reusing the bounded reporting retention here is observational only:
        // it does not enable V2 convergence or durable consensus state in V3.
        if ((epoch_protocol_mode != EpochProtocolMode::adaptive_v2 &&
             epoch_protocol_mode != EpochProtocolMode::adaptive_v3) ||
            adaptive_v2_reporting_outbox == nullptr ||
            adaptive_v2_lifecycle_reporting_suppressed)
            return;
        try
        {
            const auto existing =
                adaptive_v2_durable_initialization_reports.find(key);
            if (existing !=
                adaptive_v2_durable_initialization_reports.end())
            {
                if (existing->second ==
                    AdaptiveV2DurableInitializationPhase::ready_to_enqueue)
                    static_cast<void>(
                        try_enqueue_adaptive_v2_runtime_initialized_report(
                            key));
                return;
            }
            if (adaptive_v2_durable_initialization_reports.size() >=
                maximum_proposal_view_generation_observations)
            {
                suppress_adaptive_v2_lifecycle_reporting(
                    "runtime_initialization_capacity_exceeded");
                return;
            }
            const auto inserted =
                adaptive_v2_durable_initialization_reports.emplace(
                    key,
                    AdaptiveV2DurableInitializationPhase::
                        ready_to_enqueue);
            if (!inserted.second)
                return;
            static_cast<void>(
                try_enqueue_adaptive_v2_runtime_initialized_report(key));
        }
        catch (...)
        {
            suppress_adaptive_v2_lifecycle_reporting(
                "runtime_initialization_exception");
        }
    }

    bool HotStuffBase::
    try_enqueue_adaptive_v2_runtime_initialized_report(
        const ProposalKey &key) noexcept
    {
        try
        {
            const auto initialized =
                adaptive_v2_durable_initialization_reports.find(key);
            if (initialized ==
                adaptive_v2_durable_initialization_reports.end())
                return false;
            if (initialized->second ==
                AdaptiveV2DurableInitializationPhase::queued)
                return true;
            if (initialized->second !=
                    AdaptiveV2DurableInitializationPhase::
                        ready_to_enqueue ||
                adaptive_v2_reporting_outbox == nullptr ||
                adaptive_v2_lifecycle_reporting_suppressed)
                return false;

            const ProposalLifecycleFact fact =
                NormalProposalRuntimeInitialized{key};
            const auto status =
                adaptive_v2_reporting_outbox->enqueue_lifecycle(fact);
            if (status == AdaptiveV2ReportingEnqueueStatus::queued)
            {
                initialized->second =
                    AdaptiveV2DurableInitializationPhase::queued;
                schedule_adaptive_v2_reporting_flush(
                    adaptive_v2_evidence_retry_delay);
                return true;
            }
            if (status ==
                AdaptiveV2ReportingEnqueueStatus::capacity_exceeded)
            {
                schedule_adaptive_v2_reporting_flush(
                    adaptive_v2_evidence_retry_delay);
                return false;
            }
            suppress_adaptive_v2_lifecycle_reporting(
                "runtime_initialization_enqueue_failed");
            return false;
        }
        catch (...)
        {
            suppress_adaptive_v2_lifecycle_reporting(
                "runtime_initialization_enqueue_exception");
            return false;
        }
    }

    void HotStuffBase::
    retry_ready_adaptive_v2_runtime_initialized_reports() noexcept
    {
        while (!adaptive_v2_lifecycle_reporting_suppressed)
        {
            auto ready =
                adaptive_v2_durable_initialization_reports.end();
            for (auto entry =
                     adaptive_v2_durable_initialization_reports.begin();
                 entry !=
                     adaptive_v2_durable_initialization_reports.end();
                 ++entry)
            {
                if (entry->second ==
                    AdaptiveV2DurableInitializationPhase::ready_to_enqueue)
                {
                    ready = entry;
                    break;
                }
            }
            if (ready ==
                adaptive_v2_durable_initialization_reports.end())
                return;
            const auto key = ready->first;
            if (!try_enqueue_adaptive_v2_runtime_initialized_report(key))
                return;
        }
    }

    void HotStuffBase::retire_authoritative_absent_context(
        const std::optional<ProposalKey> &authoritative_key,
        bool authoritative_key_has_local_context) noexcept
    {
        if (authoritative_key.has_value() &&
            !authoritative_key_has_local_context &&
            proposal_admission != nullptr)
            proposal_admission->retire_proposal(*authoritative_key);
    }

    void HotStuffBase::report_adaptive_v2_committed(
        const std::optional<ProposalKey> &key,
        bool initialization_predecessor_required) noexcept
    {
        if (epoch_protocol_mode == EpochProtocolMode::adaptive_v3)
        {
            if (!key.has_value() || adaptive_v2_reporting_outbox == nullptr)
                return;
            try
            {
                const auto status = adaptive_v2_reporting_outbox->enqueue_lifecycle(
                    ProposalLifecycleFact{ProposalCommitted{*key}});
                if (status == AdaptiveV2ReportingEnqueueStatus::queued ||
                    status == AdaptiveV2ReportingEnqueueStatus::capacity_exceeded)
                    schedule_adaptive_v2_reporting_flush(
                        adaptive_v2_evidence_retry_delay);
            }
            catch (...)
            {
                // The authenticated manager lifecycle report is observational;
                // never let allocation or transport preparation affect consensus.
            }
            return;
        }
        if (epoch_protocol_mode != EpochProtocolMode::adaptive_v2 &&
            epoch_protocol_mode != EpochProtocolMode::adaptive_v3)
            return;
        if (!pending_adaptive_v2_commit.has_value())
        {
            mark_adaptive_v2_convergence_evidence_unhealthy(
                "authoritative_commit_identity_mismatched_or_conflicted");
            return;
        }
        const auto &cached = *pending_adaptive_v2_commit;
        if (cached.identity_disposition ==
            CommittedProposalIdentityDisposition::unavailable)
        {
            const bool exact_unavailable =
                !key.has_value() && !cached.committed_key.has_value() &&
                !cached.view_generation.has_value();
            if (!exact_unavailable)
            {
                pending_adaptive_v2_commit->identity_disposition =
                    CommittedProposalIdentityDisposition::conflicting;
                pending_adaptive_v2_commit->event_identity_disposition =
                    CommittedProposalIdentityDisposition::conflicting;
                mark_adaptive_v2_convergence_evidence_unhealthy(
                    "authoritative_commit_identity_mismatched_or_conflicted");
                return;
            }
            if (adaptive_v2_committed_convergence_identity.has_value())
            {
                const auto &convergence =
                    *adaptive_v2_committed_convergence_identity;
                const bool distinct_from_convergence_command =
                    cached.block_hash != convergence.command_block_hash;
                const bool distinct_from_pending_epoch_change =
                    !pending_committed_epoch_change.has_value() ||
                    pending_committed_epoch_change->block_hash !=
                        cached.block_hash;
                if (!distinct_from_convergence_command ||
                    !distinct_from_pending_epoch_change)
                {
                    pending_adaptive_v2_commit->identity_disposition =
                        CommittedProposalIdentityDisposition::conflicting;
                    pending_adaptive_v2_commit->event_identity_disposition =
                        CommittedProposalIdentityDisposition::conflicting;
                    mark_adaptive_v2_convergence_evidence_unhealthy(
                        "authoritative_commit_identity_mismatched_or_"
                        "conflicted");
                    return;
                }
            }
            // A later legal QC-skipped commit does not change the exact
            // epoch-change identity that was already committed and queued
            // for convergence reporting. Keep that identity available for
            // the matching activation observation; the unavailable commit
            // remains explicit in the structured evidence stream and is
            // never promoted into a protocol ProposalKey.
            return;
        }
        const bool exact_authoritative_identity =
            cached.identity_disposition ==
                CommittedProposalIdentityDisposition::exact &&
            key.has_value() && cached.block_hash == key->block_hash &&
            cached.committed_key.has_value() &&
            *cached.committed_key == *key &&
            cached.view_generation.has_value();
        if (!exact_authoritative_identity)
        {
            pending_adaptive_v2_commit->identity_disposition =
                CommittedProposalIdentityDisposition::conflicting;
            pending_adaptive_v2_commit->event_identity_disposition =
                CommittedProposalIdentityDisposition::conflicting;
            mark_adaptive_v2_convergence_evidence_unhealthy(
                "authoritative_commit_identity_mismatched_or_conflicted");
            return;
        }
        if (adaptive_v2_reporting_outbox == nullptr)
        {
            suppress_adaptive_v2_lifecycle_reporting(
                "authoritative_commit_outbox_missing");
            return;
        }
        try
        {
            if (adaptive_v2_lifecycle_reporting_suppressed)
                return;
            if (experiment_responsive_cross_commit_retention_v2)
            {
                const auto local_commit_monotonic_ns =
                    adaptive_evidence_monotonic_now_ns();
                if (local_commit_monotonic_ns == 0 ||
                    adaptive_v2_response_evidence == nullptr ||
                    !adaptive_v2_response_evidence
                         ->record_reporter_local_commit(
                             *key, local_commit_monotonic_ns))
                {
                    mark_adaptive_v2_convergence_evidence_unhealthy(
                        "cross_commit_retention_clock_or_state_failed");
                    return;
                }
                pending_adaptive_v2_commit
                    ->reporter_local_commit_monotonic_ns =
                    local_commit_monotonic_ns;
            }
            const auto pending =
                experiment_false_timeout_states.find(*key);
            if (pending != experiment_false_timeout_states.end())
            {
                if (!initialization_predecessor_required)
                {
                    suppress_adaptive_v2_lifecycle_reporting(
                        "commit_without_context_has_false_report_state");
                    return;
                }
                const bool retain_response_evidence =
                    experiment_byzantine_adapter != nullptr &&
                    experiment_byzantine_adapter
                        ->should_retain_response_evidence(
                            ExperimentByzantineContext{
                                *key,
                                experiment_diagnostic_window});
                const auto action = pending->second.observe_commit(
                    retain_response_evidence);
                if (action !=
                    ExperimentFalseTimeoutCommitAction::report_now)
                {
                    if (action ==
                        ExperimentFalseTimeoutCommitAction::deferred)
                        HOTSTUFF_LOG_INFO(
                            "KAURI_FAULT false_report_commit_deferred "
                            "reporter=%u target=%u epoch=%u tree=%u "
                            "block=%s",
                            get_id(),
                            pending->second.target,
                            key->configuration.epoch_number,
                            key->configuration.tree_id,
                            key->block_hash.to_hex().c_str());
                    return;
                }
            }
            const auto durable =
                adaptive_v2_durable_commit_reports.find(*key);
            if (durable != adaptive_v2_durable_commit_reports.end())
            {
                if (durable->second.initialization_predecessor_required !=
                        initialization_predecessor_required ||
                    (!initialization_predecessor_required &&
                     (durable->second.phase ==
                          AdaptiveV2DurableCommitPhase::awaiting_evidence ||
                      durable->second.experiment_false_report)))
                {
                    suppress_adaptive_v2_lifecycle_reporting(
                        "commit_initialization_predecessor_conflicted");
                    return;
                }
                if (durable->second.phase ==
                    AdaptiveV2DurableCommitPhase::ready_to_enqueue)
                    static_cast<void>(
                        try_enqueue_adaptive_v2_commit_report(*key));
                return;
            }
            const bool defer_for_response_evidence =
                adaptive_v2_response_evidence != nullptr &&
                adaptive_v2_response_evidence
                    ->should_defer_commit_report(*key);
            if (!initialization_predecessor_required &&
                defer_for_response_evidence)
            {
                suppress_adaptive_v2_lifecycle_reporting(
                    "commit_without_context_has_response_evidence_state");
                return;
            }
            AdaptiveV2DurableCommitReportState state;
            state.initialization_predecessor_required =
                initialization_predecessor_required;
            state.phase = defer_for_response_evidence
                ? AdaptiveV2DurableCommitPhase::awaiting_evidence
                : AdaptiveV2DurableCommitPhase::ready_to_enqueue;
            if (!persist_adaptive_v2_commit_report(
                    *key,
                    state,
                    "response_commit_state_capacity_exceeded"))
            {
                if (defer_for_response_evidence &&
                    adaptive_v2_response_evidence != nullptr)
                    static_cast<void>(
                        adaptive_v2_response_evidence->retire(*key));
                return;
            }
            if (defer_for_response_evidence)
            {
                HOTSTUFF_LOG_INFO(
                    "[EVIDENCE] Commit notice deferred until response "
                    "deadline epoch=%u tree=%u block=%.10s",
                    key->configuration.epoch_number,
                    key->configuration.tree_id,
                    key->block_hash.to_hex().c_str());
                return;
            }
            static_cast<void>(
                try_enqueue_adaptive_v2_commit_report(*key));
        }
        catch (...)
        {
            suppress_adaptive_v2_lifecycle_reporting(
                "response_commit_reporting_exception");
        }
    }

    void HotStuffBase::observe_adaptive_v2_response_deadline_result(
        const ProposalKey &key,
        EvidenceDeadlineResult result) noexcept
    {
        try
        {
            auto durable =
                adaptive_v2_durable_commit_reports.find(key);
            if (result == EvidenceDeadlineResult::evidence_accepted)
            {
                if (durable ==
                        adaptive_v2_durable_commit_reports.end() ||
                    durable->second.phase ==
                        AdaptiveV2DurableCommitPhase::suppressed)
                    return;
                durable->second.phase =
                    AdaptiveV2DurableCommitPhase::ready_to_enqueue;
                static_cast<void>(
                    try_enqueue_adaptive_v2_commit_report(key));
                return;
            }

            if (durable != adaptive_v2_durable_commit_reports.end())
                durable->second.phase =
                    AdaptiveV2DurableCommitPhase::suppressed;
            else
            {
                AdaptiveV2DurableCommitReportState suppressed;
                suppressed.phase =
                    AdaptiveV2DurableCommitPhase::suppressed;
                static_cast<void>(persist_adaptive_v2_commit_report(
                    key,
                    suppressed,
                    "response_deadline_tombstone_capacity_exceeded"));
            }
            mark_adaptive_v2_convergence_evidence_unhealthy(
                "response_deadline_evidence_failed");
        }
        catch (...)
        {
            suppress_adaptive_v2_lifecycle_reporting(
                "response_deadline_result_exception");
        }
    }

    bool HotStuffBase::persist_adaptive_v2_commit_report(
        const ProposalKey &key,
        AdaptiveV2DurableCommitReportState state,
        const char *failure_reason) noexcept
    {
        if (adaptive_v2_lifecycle_reporting_suppressed)
            return false;
        try
        {
            if (adaptive_v2_durable_commit_reports.find(key) !=
                adaptive_v2_durable_commit_reports.end())
                return true;
            if (adaptive_v2_durable_commit_reports.size() >=
                maximum_proposal_view_generation_observations)
            {
                suppress_adaptive_v2_lifecycle_reporting(failure_reason);
                return false;
            }
            const auto inserted =
                adaptive_v2_durable_commit_reports.emplace(
                    key, std::move(state));
            if (!inserted.second)
            {
                suppress_adaptive_v2_lifecycle_reporting(failure_reason);
                return false;
            }
            return true;
        }
        catch (...)
        {
            suppress_adaptive_v2_lifecycle_reporting(failure_reason);
            return false;
        }
    }

    bool HotStuffBase::try_enqueue_adaptive_v2_commit_report(
        const ProposalKey &key) noexcept
    {
        try
        {
            const auto durable =
                adaptive_v2_durable_commit_reports.find(key);
            if (durable == adaptive_v2_durable_commit_reports.end())
                return true;
            if (durable->second.phase !=
                    AdaptiveV2DurableCommitPhase::ready_to_enqueue ||
                adaptive_v2_lifecycle_reporting_suppressed)
                return false;
            if (adaptive_v2_reporting_outbox == nullptr)
            {
                suppress_adaptive_v2_lifecycle_reporting(
                    "commit_outbox_unavailable");
                return false;
            }
            const auto initialized =
                adaptive_v2_durable_initialization_reports.find(key);
            if (initialized ==
                    adaptive_v2_durable_initialization_reports.end() &&
                durable->second.initialization_predecessor_required)
            {
                suppress_adaptive_v2_lifecycle_reporting(
                    "commit_without_runtime_initialization");
                return false;
            }
            if (initialized !=
                    adaptive_v2_durable_initialization_reports.end() &&
                initialized->second ==
                    AdaptiveV2DurableInitializationPhase::suppressed)
            {
                suppress_adaptive_v2_lifecycle_reporting(
                    "commit_without_runtime_initialization");
                return false;
            }
            if (initialized !=
                    adaptive_v2_durable_initialization_reports.end() &&
                initialized->second ==
                    AdaptiveV2DurableInitializationPhase::
                        ready_to_enqueue &&
                !try_enqueue_adaptive_v2_runtime_initialized_report(key))
                return false;

            const ProposalLifecycleFact fact = ProposalCommitted{key};
            const auto status =
                adaptive_v2_reporting_outbox->enqueue_lifecycle(fact);
            if (status == AdaptiveV2ReportingEnqueueStatus::queued)
            {
                if (durable->second.experiment_false_report)
                    HOTSTUFF_LOG_INFO(
                        "KAURI_FAULT false_report_commit_released "
                        "reporter=%u target=%u epoch=%u tree=%u block=%s "
                        "evidence=%zu",
                        get_id(),
                        durable->second.experiment_false_target,
                        key.configuration.epoch_number,
                        key.configuration.tree_id,
                        key.block_hash.to_hex().c_str(),
                        durable->second
                            .experiment_false_recorded_evidence);
                adaptive_v2_durable_commit_reports.erase(durable);
                adaptive_v2_durable_initialization_reports.erase(key);
                schedule_adaptive_v2_reporting_flush(
                    adaptive_v2_evidence_retry_delay);
                return true;
            }
            if (status ==
                AdaptiveV2ReportingEnqueueStatus::capacity_exceeded)
            {
                schedule_adaptive_v2_reporting_flush(
                    adaptive_v2_evidence_retry_delay);
                return false;
            }

            if (durable->second.experiment_false_report)
                HOTSTUFF_LOG_WARN(
                    "KAURI_FAULT false_report_commit_suppressed "
                    "reporter=%u target=%u epoch=%u tree=%u block=%s "
                    "evidence=%zu reason=commit_not_queued",
                    get_id(),
                    durable->second.experiment_false_target,
                    key.configuration.epoch_number,
                    key.configuration.tree_id,
                    key.block_hash.to_hex().c_str(),
                    durable->second.experiment_false_recorded_evidence);
            suppress_adaptive_v2_lifecycle_reporting(
                "commit_enqueue_failed");
            return false;
        }
        catch (...)
        {
            suppress_adaptive_v2_lifecycle_reporting(
                "commit_enqueue_exception");
            return false;
        }
    }

    void HotStuffBase::
    retry_ready_adaptive_v2_commit_reports() noexcept
    {
        while (!adaptive_v2_lifecycle_reporting_suppressed)
        {
            auto ready =
                adaptive_v2_durable_commit_reports.end();
            for (auto entry =
                     adaptive_v2_durable_commit_reports.begin();
                 entry != adaptive_v2_durable_commit_reports.end();
                 ++entry)
                if (entry->second.phase ==
                    AdaptiveV2DurableCommitPhase::ready_to_enqueue)
                {
                    ready = entry;
                    break;
                }
            if (ready == adaptive_v2_durable_commit_reports.end())
                return;
            const auto key = ready->first;
            if (!try_enqueue_adaptive_v2_commit_report(key))
                return;
        }
    }

    bool HotStuffBase::has_durable_adaptive_v2_commit_report(
        const ProposalKey &key) const noexcept
    {
        try
        {
            const auto found =
                adaptive_v2_durable_commit_reports.find(key);
            return found != adaptive_v2_durable_commit_reports.end() &&
                   found->second.phase !=
                       AdaptiveV2DurableCommitPhase::suppressed;
        }
        catch (...)
        {
            return false;
        }
    }

    bool HotStuffBase::has_pending_adaptive_v2_lifecycle_fence()
        const noexcept
    {
        try
        {
            const bool pending_initialization = std::any_of(
                adaptive_v2_durable_initialization_reports.begin(),
                adaptive_v2_durable_initialization_reports.end(),
                [](const auto &entry) {
                    return entry.second ==
                        AdaptiveV2DurableInitializationPhase::
                            ready_to_enqueue;
                });
            const bool pending_commit = std::any_of(
                adaptive_v2_durable_commit_reports.begin(),
                adaptive_v2_durable_commit_reports.end(),
                [](const auto &entry) {
                    return entry.second.phase !=
                        AdaptiveV2DurableCommitPhase::suppressed;
                });
            const bool pending_false_report = std::any_of(
                experiment_false_timeout_states.begin(),
                experiment_false_timeout_states.end(),
                [](const auto &entry) {
                    return entry.second.commit_deferred;
                });
            return pending_initialization || pending_commit ||
                   pending_false_report;
        }
        catch (...)
        {
            return true;
        }
    }

    void HotStuffBase::suppress_adaptive_v2_lifecycle_reporting(
        const char *reason) noexcept
    {
        adaptive_v2_lifecycle_reporting_suppressed = true;
        for (auto &entry : adaptive_v2_durable_initialization_reports)
        {
            if (entry.second !=
                AdaptiveV2DurableInitializationPhase::queued)
                entry.second =
                    AdaptiveV2DurableInitializationPhase::suppressed;
        }
        for (auto &entry : adaptive_v2_durable_commit_reports)
            entry.second.phase =
                AdaptiveV2DurableCommitPhase::suppressed;
        mark_adaptive_v2_convergence_evidence_unhealthy(reason);
    }

    void HotStuffBase::poison_adaptive_v2_reporting(
        const char *reason) noexcept
    {
        suppress_adaptive_v2_lifecycle_reporting(reason);
        if (adaptive_v2_reporting_outbox != nullptr)
            adaptive_v2_reporting_outbox->shutdown();
        cancel_adaptive_v2_reporting_flush();
    }

    AdaptiveV2ReportingDeliveryResult
    HotStuffBase::transmit_adaptive_v2_report(
        const AdaptiveV2PendingReport &report) noexcept
    {
        if (epoch_protocol_mode == EpochProtocolMode::adaptive_v3 &&
            report.stream != AdaptiveV2ReportingStream::readiness &&
            report.stream != AdaptiveV2ReportingStream::lifecycle &&
            report.stream != AdaptiveV2ReportingStream::evidence)
            return AdaptiveV2ReportingDeliveryResult::permanent_failure;
        if (!epoch_manager_peer.has_value() ||
            !authorize_manager_peer(*epoch_manager_peer))
            return AdaptiveV2ReportingDeliveryResult::permanent_failure;
        try
        {
            const auto manager_peer = *epoch_manager_peer;
            const auto manager_connection =
                pn.get_peer_conn(manager_peer);
            if (manager_connection == nullptr ||
                manager_connection->is_terminated())
                return AdaptiveV2ReportingDeliveryResult::temporary_failure;
            const auto *manager_certificate =
                manager_connection->get_peer_cert();
            if (manager_certificate == nullptr ||
                PeerId(*manager_certificate) != manager_peer)
                return AdaptiveV2ReportingDeliveryResult::temporary_failure;

            bool sent = false;
            switch (report.stream)
            {
            case AdaptiveV2ReportingStream::readiness:
                if (report.opcode !=
                    MsgAdaptiveV2ReadinessNotice::opcode)
                    return AdaptiveV2ReportingDeliveryResult::
                        permanent_failure;
                sent = pn.send_msg(
                    MsgAdaptiveV2ReadinessNotice(
                        DataStream(report.canonical_payload)),
                    manager_connection);
                break;
            case AdaptiveV2ReportingStream::lifecycle:
                if (report.opcode != MsgProposalLifecycleNotice::opcode)
                    return AdaptiveV2ReportingDeliveryResult::
                        permanent_failure;
                sent = pn.send_msg(
                    MsgProposalLifecycleNotice(
                        DataStream(report.canonical_payload)),
                    manager_connection);
                break;
            case AdaptiveV2ReportingStream::evidence:
                if (report.opcode != MsgEvidenceReport::opcode)
                    return AdaptiveV2ReportingDeliveryResult::
                        permanent_failure;
                sent = pn.send_msg(
                    MsgEvidenceReport(
                        DataStream(report.canonical_payload)),
                    manager_connection);
                break;
            case AdaptiveV2ReportingStream::convergence:
                if (report.opcode ==
                    MsgAdaptiveV2EpochChangeCommittedObservation::opcode)
                {
                    sent = pn.send_msg(
                        MsgAdaptiveV2EpochChangeCommittedObservation(
                            DataStream(report.canonical_payload)),
                        manager_connection);
                }
                else if (report.opcode ==
                         MsgAdaptiveV2EpochActivatedObservation::opcode)
                {
                    sent = pn.send_msg(
                        MsgAdaptiveV2EpochActivatedObservation(
                            DataStream(report.canonical_payload)),
                        manager_connection);
                }
                else
                {
                    return AdaptiveV2ReportingDeliveryResult::
                        permanent_failure;
                }
                break;
            }
            return sent
                       ? AdaptiveV2ReportingDeliveryResult::delivered
                       : AdaptiveV2ReportingDeliveryResult::
                             temporary_failure;
        }
        catch (...)
        {
            return AdaptiveV2ReportingDeliveryResult::temporary_failure;
        }
    }

    void HotStuffBase::schedule_adaptive_v2_reporting_flush(
        AggregationScheduler::Duration delay) noexcept
    {
        if (!is_adaptive_epoch_mode(epoch_protocol_mode) ||
            adaptive_v2_reporting_outbox == nullptr ||
            aggregation_scheduler == nullptr ||
            !epoch_manager_peer.has_value() ||
            !authorize_manager_peer(*epoch_manager_peer) ||
            adaptive_v2_reporting_flush_cancellation ||
            delay <= AggregationScheduler::Duration::zero())
            return;
        try
        {
            const auto access = exact_runtime_access;
            auto cancellation = aggregation_scheduler->schedule_after(
                delay,
                [access]() {
                    auto runtime = access->acquire();
                    if (!runtime.has_value())
                        return;
                    auto &owner = runtime->owner();
                    owner.adaptive_v2_reporting_flush_cancellation = {};
                    owner.flush_adaptive_v2_reporting();
                });
            if (cancellation)
                adaptive_v2_reporting_flush_cancellation =
                    std::move(cancellation);
        }
        catch (...)
        {
            // Reporting scheduler failure cannot affect consensus progress.
        }
    }

    void HotStuffBase::cancel_adaptive_v2_reporting_flush() noexcept
    {
        auto cancellation =
            std::move(adaptive_v2_reporting_flush_cancellation);
        adaptive_v2_reporting_flush_cancellation = {};
        if (!cancellation)
            return;
        try
        {
            cancellation();
        }
        catch (...)
        {
            // Cancellation is best effort during shutdown.
        }
    }

    void HotStuffBase::flush_adaptive_v2_reporting() noexcept
    {
        if (!is_adaptive_epoch_mode(epoch_protocol_mode) ||
            adaptive_v2_reporting_outbox == nullptr ||
            !epoch_manager_peer.has_value() ||
            !authorize_manager_peer(*epoch_manager_peer))
            return;

        retry_ready_adaptive_v2_runtime_initialized_reports();
        retry_ready_adaptive_v2_commit_reports();
        enqueue_pending_adaptive_v2_commit_observation();
        enqueue_pending_adaptive_v2_activation_observation();

        while (true)
        {
            const auto now = adaptive_monotonic_now_ns();
            const auto attempt =
                adaptive_v2_reporting_outbox->begin_delivery(now);
            if (attempt.status ==
                AdaptiveV2ReportingAttemptStatus::unhealthy)
            {
                poison_adaptive_v2_reporting(
                    "shared_outbox_unhealthy");
                return;
            }
            if (attempt.status ==
                    AdaptiveV2ReportingAttemptStatus::empty ||
                attempt.status ==
                    AdaptiveV2ReportingAttemptStatus::already_in_flight ||
                attempt.status ==
                    AdaptiveV2ReportingAttemptStatus::stopped)
                return;

            if (attempt.status ==
                AdaptiveV2ReportingAttemptStatus::retry_not_due)
            {
                if (attempt.report == nullptr ||
                    attempt.report->next_attempt_monotonic_ns <= now)
                    return;
                const auto remaining =
                    attempt.report->next_attempt_monotonic_ns - now;
                const auto maximum = static_cast<std::uint64_t>(
                    std::numeric_limits<
                        AggregationScheduler::Duration::rep>::max());
                schedule_adaptive_v2_reporting_flush(
                    AggregationScheduler::Duration(
                        static_cast<
                            AggregationScheduler::Duration::rep>(
                            std::min(remaining, maximum))));
                return;
            }

            if (attempt.status ==
                AdaptiveV2ReportingAttemptStatus::terminal)
            {
                if (attempt.report == nullptr)
                {
                    poison_adaptive_v2_reporting(
                        "shared_outbox_terminal_report_missing");
                    return;
                }
                const auto terminal_state =
                    attempt.report->delivery_state;
                if (terminal_state ==
                    AdaptiveV2ReportingDeliveryState::failed)
                {
                    poison_adaptive_v2_reporting(
                        "shared_outbox_terminal_report");
                    return;
                }
                if (adaptive_v2_reporting_outbox->release_terminal(
                        attempt.report->report_id) !=
                    AdaptiveV2ReportingReleaseStatus::released)
                    return;
                retry_ready_adaptive_v2_runtime_initialized_reports();
                retry_ready_adaptive_v2_commit_reports();
                enqueue_pending_adaptive_v2_commit_observation();
                enqueue_pending_adaptive_v2_activation_observation();
                continue;
            }

            if (attempt.status !=
                    AdaptiveV2ReportingAttemptStatus::started ||
                !attempt.token.has_value() || attempt.report == nullptr)
                return;

            const auto report_id = attempt.report->report_id;
            const auto delivery =
                transmit_adaptive_v2_report(*attempt.report);
            const auto transition =
                adaptive_v2_reporting_outbox->acknowledge_delivery(
                    *attempt.token, delivery, now);
            if (transition ==
                AdaptiveV2ReportingTransitionStatus::delivered)
            {
                if (adaptive_v2_reporting_outbox->release_terminal(
                        report_id) !=
                    AdaptiveV2ReportingReleaseStatus::released)
                    return;
                retry_ready_adaptive_v2_runtime_initialized_reports();
                retry_ready_adaptive_v2_commit_reports();
                enqueue_pending_adaptive_v2_commit_observation();
                enqueue_pending_adaptive_v2_activation_observation();
                continue;
            }
            if (transition ==
                AdaptiveV2ReportingTransitionStatus::retry_scheduled)
            {
                const auto *pending =
                    adaptive_v2_reporting_outbox->front();
                if (pending == nullptr ||
                    pending->next_attempt_monotonic_ns <= now)
                    return;
                const auto remaining =
                    pending->next_attempt_monotonic_ns - now;
                const auto maximum = static_cast<std::uint64_t>(
                    std::numeric_limits<
                        AggregationScheduler::Duration::rep>::max());
                schedule_adaptive_v2_reporting_flush(
                    AggregationScheduler::Duration(
                        static_cast<
                            AggregationScheduler::Duration::rep>(
                            std::min(remaining, maximum))));
                return;
            }
            if (transition ==
                AdaptiveV2ReportingTransitionStatus::failed)
            {
                poison_adaptive_v2_reporting(
                    "shared_outbox_delivery_failed");
                return;
            }
            return;
        }
    }

    void HotStuffBase::mark_adaptive_v2_convergence_evidence_unhealthy(
        const char *reason) noexcept
    {
        if (!adaptive_v2_convergence_evidence_healthy)
            return;
        adaptive_v2_convergence_evidence_healthy = false;
        adaptive_v2_commit_observation_enqueued = false;
        adaptive_v2_activation_observation_pending = false;
        adaptive_v2_committed_convergence_identity.reset();
        HOTSTUFF_LOG_WARN(
            "[EPOCH] Adaptive-v2 convergence evidence unhealthy: %s",
            reason == nullptr ? "unknown" : reason);
    }

    void HotStuffBase::enqueue_pending_adaptive_v2_commit_observation()
        noexcept
    {
        if (!adaptive_v2_convergence_evidence_healthy ||
            adaptive_v2_commit_observation_enqueued ||
            !adaptive_v2_committed_convergence_identity.has_value() ||
            adaptive_v2_reporting_outbox == nullptr ||
            has_pending_adaptive_v2_lifecycle_fence())
            return;

        const auto status =
            adaptive_v2_reporting_outbox->enqueue_convergence_observation(
                AdaptiveV2ConvergenceObservationKind::commit,
                *adaptive_v2_committed_convergence_identity);
        if (status == AdaptiveV2ReportingEnqueueStatus::queued)
        {
            adaptive_v2_commit_observation_enqueued = true;
            schedule_adaptive_v2_reporting_flush(
                adaptive_v2_evidence_retry_delay);
            return;
        }
        if (status ==
            AdaptiveV2ReportingEnqueueStatus::capacity_exceeded)
        {
            schedule_adaptive_v2_reporting_flush(
                adaptive_v2_evidence_retry_delay);
            return;
        }
        mark_adaptive_v2_convergence_evidence_unhealthy(
            "commit_observation_enqueue_failed");
    }

    void HotStuffBase::enqueue_pending_adaptive_v2_activation_observation(
        ) noexcept
    {
        if (!adaptive_v2_convergence_evidence_healthy ||
            !adaptive_v2_activation_observation_pending ||
            !adaptive_v2_committed_convergence_identity.has_value() ||
            adaptive_v2_reporting_outbox == nullptr ||
            has_pending_adaptive_v2_lifecycle_fence())
            return;

        enqueue_pending_adaptive_v2_commit_observation();
        if (!adaptive_v2_commit_observation_enqueued)
            return;

        const auto status =
            adaptive_v2_reporting_outbox->enqueue_epoch_activated(
                *adaptive_v2_committed_convergence_identity);
        if (status == AdaptiveV2ReportingEnqueueStatus::queued)
        {
            adaptive_v2_activation_observation_pending = false;
            adaptive_v2_commit_observation_enqueued = false;
            adaptive_v2_committed_convergence_identity.reset();
            schedule_adaptive_v2_reporting_flush(
                adaptive_v2_evidence_retry_delay);
            return;
        }
        if (status ==
            AdaptiveV2ReportingEnqueueStatus::capacity_exceeded)
        {
            schedule_adaptive_v2_reporting_flush(
                adaptive_v2_evidence_retry_delay);
            return;
        }
        mark_adaptive_v2_convergence_evidence_unhealthy(
            "activation_observation_enqueue_failed");
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
                if (experiment_byzantine_adapter != nullptr &&
                    experiment_byzantine_adapter
                        ->outbound_direct_vote_omitted(
                            ExperimentByzantineContext{
                                lease.key(),
                                experiment_diagnostic_window}))
                    return true;
                if (consume_experiment_outbound_aggregate(
                        lease.key(), lease.tree()))
                    return true;
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
        aggregation_effects.record_timer_failure =
            [this](const ProposalKey &key)
            {
                if (epoch_protocol_mode == EpochProtocolMode::adaptive_v2)
                    poison_response_attempt_arm_once(
                        key, "aggregation_timer_rearm_failed");
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

    void HotStuffBase::configure_experiment_byzantine_faults(
        ExperimentByzantineOptions options)
    {
        if (epoch_protocol_mode != EpochProtocolMode::adaptive_v2)
            throw std::logic_error(
                "Byzantine experiment faults require adaptive-v2");
        if (proposal_contexts->active_configuration().has_value())
            throw std::logic_error(
                "Byzantine experiment faults must be configured "
                "before startup");
        if (!options.enabled ||
            (!options.rotating_omission.has_value() &&
             options.configuration.epoch_digest.is_null()))
            throw std::invalid_argument(
                "Byzantine experiment fault configuration is invalid");
        if (options.false_report_target.has_value() &&
            *options.false_report_target == get_id())
            throw std::invalid_argument(
                "false-report target must differ from local reporter");
        if (options.rotating_omission.has_value() &&
            options.rotating_omission->local_replica != get_id())
            throw std::invalid_argument(
                "scheduled omission local replica does not match runtime");
        if (experiment_responsive_cross_commit_retention_v2 &&
            (!options.rotating_omission.has_value() ||
             options.rotating_omission->mode !=
                 "tiered_persistent_responsive_omission_v2" ||
             options.rotating_omission
                 ->responsive_degraded_actor_ids.empty()))
        {
            throw std::invalid_argument(
                "cross-commit retention v2 requires tiered responsive "
                "omission v2 actors");
        }
        if (!options.response_evidence_duplicate_probe.empty() &&
            (options.response_evidence_duplicate_probe !=
                 kExperimentResponseEvidenceDuplicateProbeMode ||
             !options.rotating_omission.has_value() ||
             options.rotating_omission->mode !=
                 "tiered_persistent_responsive_omission_v2"))
            throw std::invalid_argument(
                "response-evidence duplicate probe configuration is invalid");
        const auto false_timeout_context_bound =
            options.false_report_target.has_value()
                ? options.maximum_false_report_contexts
                : 0;
        if (options.rotating_omission.has_value())
        {
            auto configured_marker_emitter =
                std::move(options.omission_marker_emitter);
            options.omission_marker_emitter =
                [this,
                 configured_marker_emitter =
                     std::move(configured_marker_emitter)](
                    const ExperimentOmissionMarker &marker)
                {
                    if (marker.fault_mode !=
                        "tiered_persistent_responsive_omission_v2")
                    {
                        if (configured_marker_emitter)
                            configured_marker_emitter(marker);
                        const auto encoded =
                            format_experiment_omission_marker(marker);
                        HOTSTUFF_LOG_INFO("%s", encoded.c_str());
                        return;
                    }
                    emit_fault_contribution_opportunity(marker);
                    try
                    {
                        if (configured_marker_emitter)
                            configured_marker_emitter(marker);
                    }
                    catch (...)
                    {
                    }
                    try
                    {
                        const auto encoded =
                            format_experiment_omission_marker(marker);
                        HOTSTUFF_LOG_INFO("%s", encoded.c_str());
                    }
                    catch (...)
                    {
                    }
                };
        }
        auto diagnostic_window = options.diagnostic_window;
        auto response_evidence_duplicate_probe =
            options.response_evidence_duplicate_probe;
        const auto response_evidence_duplicate_probe_window_end_ns =
            response_evidence_duplicate_probe.empty()
                ? std::uint64_t{0}
                : options.rotating_omission->window_end_monotonic_ns;
        auto adapter =
            std::make_unique<ExperimentByzantineAdapter>(
                std::move(options));
        maximum_experiment_false_timeout_contexts =
            false_timeout_context_bound;
        experiment_diagnostic_window = std::move(diagnostic_window);
        experiment_response_evidence_duplicate_probe =
            std::move(response_evidence_duplicate_probe);
        experiment_response_evidence_duplicate_probe_window_end_ns =
            response_evidence_duplicate_probe_window_end_ns;
        experiment_response_evidence_duplicate_probe_consumed.store(false);
        experiment_byzantine_adapter = std::move(adapter);
    }

    void HotStuffBase::
    enable_experiment_responsive_cross_commit_retention_v2()
    {
        if (epoch_protocol_mode != EpochProtocolMode::adaptive_v2)
            throw std::logic_error(
                "cross-commit retention v2 requires adaptive-v2");
        if (proposal_contexts->active_configuration().has_value())
            throw std::logic_error(
                "cross-commit retention v2 must be enabled before startup");
        if (adaptive_v2_response_evidence == nullptr ||
            !adaptive_v2_response_evidence
                 ->enable_cross_commit_retention_v2())
        {
            throw std::logic_error(
                "cross-commit retention v2 bridge is unavailable");
        }
        experiment_responsive_cross_commit_retention_v2 = true;
    }

    void HotStuffBase::enable_experiment_exact_timeout_attempt_evidence_v3()
    {
        if (!is_adaptive_epoch_mode(epoch_protocol_mode) ||
            proposal_contexts->active_configuration().has_value() ||
            adaptive_v2_response_evidence == nullptr ||
            !adaptive_v2_response_evidence
                 ->enable_exact_timeout_attempt_evidence_v3())
            throw std::logic_error("exact timeout evidence v3 unavailable");
        experiment_exact_timeout_attempt_evidence_v3 = true;
    }

    void HotStuffBase::configure_experiment_post_qc_audit(
        ExperimentPostQcAuditOptions options)
    {
        if (epoch_protocol_mode != EpochProtocolMode::adaptive_v2)
            throw std::logic_error(
                "post-QC audit requires adaptive-v2");
        if (proposal_contexts->active_configuration().has_value())
            throw std::logic_error(
                "post-QC audit must be configured before startup");
        if (!options.enabled)
            throw std::invalid_argument(
                "post-QC audit configuration is disabled");
        experiment_post_qc_audit =
            std::make_unique<ExperimentPostQcAudit>(
                std::move(options), get_id());
    }

    void HotStuffBase::emit_experiment_post_qc_audit_target(
        const ExperimentPostQcAuditTargetObservation &observation)
        const noexcept
    {
        if (experiment_post_qc_audit == nullptr)
            return;
        try
        {
            const auto &options =
                experiment_post_qc_audit->options();
            const auto signers =
                experiment_audit_signers(observation.signers);
            HOTSTUFF_LOG_INFO(
                "KAURI_AUDIT target_verified phase=%s reporter=%u "
                "target=%u root=%u epoch=%u tree=%u epoch_digest=%s "
                "block=%s generation=%llu window=%s armed_ns=%llu "
                "deadline_ns=%llu arrival_ns=%llu signers=%s",
                to_string(observation.phase),
                static_cast<unsigned>(options.reporter),
                static_cast<unsigned>(options.target),
                static_cast<unsigned>(options.root),
                observation.proposal.configuration.epoch_number,
                observation.proposal.configuration.tree_id,
                observation.proposal.configuration.epoch_digest
                    .to_hex().c_str(),
                observation.proposal.block_hash.to_hex().c_str(),
                static_cast<unsigned long long>(
                    observation.generation),
                options.diagnostic_window.c_str(),
                static_cast<unsigned long long>(
                    observation.armed_ns),
                static_cast<unsigned long long>(
                    observation.deadline_ns),
                static_cast<unsigned long long>(
                    observation.arrival_ns),
                signers.c_str());
        }
        catch (...)
        {
        }
    }

    void HotStuffBase::emit_experiment_post_qc_audit_root_prepared(
        const ExperimentPostQcAuditRootSnapshot &snapshot)
        const noexcept
    {
        if (experiment_post_qc_audit == nullptr)
            return;
        try
        {
            const auto &options =
                experiment_post_qc_audit->options();
            const auto signers =
                experiment_audit_signers(snapshot.qc_signers);
            const auto fingerprint =
                experiment_audit_certificate_fingerprint(
                    snapshot.frozen_qc);
            HOTSTUFF_LOG_INFO(
                "KAURI_AUDIT root_prepared phase=pre_qc reporter=%u "
                "target=%u root=%u epoch=%u tree=%u epoch_digest=%s "
                "block=%s generation=%llu context_generation=%llu "
                "window=%s prepared_ns=%llu "
                "qc_signers=%s qc_fingerprint=%s",
                static_cast<unsigned>(options.reporter),
                static_cast<unsigned>(options.target),
                static_cast<unsigned>(options.root),
                snapshot.proposal.configuration.epoch_number,
                snapshot.proposal.configuration.tree_id,
                snapshot.proposal.configuration.epoch_digest
                    .to_hex().c_str(),
                snapshot.proposal.block_hash.to_hex().c_str(),
                static_cast<unsigned long long>(snapshot.generation),
                static_cast<unsigned long long>(
                    snapshot.context_generation),
                options.diagnostic_window.c_str(),
                static_cast<unsigned long long>(snapshot.prepared_ns),
                signers.c_str(),
                fingerprint.to_hex().c_str());
        }
        catch (...)
        {
        }
    }

    void HotStuffBase::emit_experiment_post_qc_audit_root_snapshot(
        const ExperimentPostQcAuditRootSnapshot &snapshot)
        const noexcept
    {
        if (experiment_post_qc_audit == nullptr)
            return;
        try
        {
            const auto &options =
                experiment_post_qc_audit->options();
            const auto signers =
                experiment_audit_signers(snapshot.qc_signers);
            const auto fingerprint =
                experiment_audit_certificate_fingerprint(
                    snapshot.frozen_qc);
            HOTSTUFF_LOG_INFO(
                "KAURI_AUDIT root_snapshot phase=post_qc reporter=%u "
                "target=%u root=%u epoch=%u tree=%u epoch_digest=%s "
                "block=%s generation=%llu context_generation=%llu "
                "window=%s prepared_ns=%llu "
                "qc_published_ns=%llu retention_deadline_ns=%llu "
                "qc_signers=%s qc_fingerprint=%s "
                "consensus_context=terminal qc_unchanged=1",
                static_cast<unsigned>(options.reporter),
                static_cast<unsigned>(options.target),
                static_cast<unsigned>(options.root),
                snapshot.proposal.configuration.epoch_number,
                snapshot.proposal.configuration.tree_id,
                snapshot.proposal.configuration.epoch_digest
                    .to_hex().c_str(),
                snapshot.proposal.block_hash.to_hex().c_str(),
                static_cast<unsigned long long>(snapshot.generation),
                static_cast<unsigned long long>(
                    snapshot.context_generation),
                options.diagnostic_window.c_str(),
                static_cast<unsigned long long>(snapshot.prepared_ns),
                static_cast<unsigned long long>(snapshot.published_ns),
                static_cast<unsigned long long>(snapshot.expiry_ns),
                signers.c_str(),
                fingerprint.to_hex().c_str());
        }
        catch (...)
        {
        }
    }

    void HotStuffBase::arm_experiment_post_qc_audit(
        const ProposalContextLease &lease) noexcept
    {
        if (experiment_post_qc_audit == nullptr ||
            !experiment_post_qc_audit->is_reporter() ||
            aggregation_scheduler == nullptr ||
            experiment_post_qc_audit->diagnostics().reporter_armed)
            return;
        try
        {
            const auto generation = find_exact_runtime_generation(
                lease.key().configuration);
            auto accumulator =
                proposal_contexts->clone_accumulator(lease);
            const auto armed_ns =
                adaptive_evidence_monotonic_now_ns();
            if (!generation.has_value() || accumulator == nullptr ||
                armed_ns == 0 ||
                !experiment_post_qc_audit->arm_reporter(
                    lease.key(),
                    *generation,
                    lease.tree(),
                    *accumulator,
                    armed_ns))
                return;
            const auto delay = std::chrono::duration_cast<
                AggregationScheduler::Duration>(
                std::chrono::milliseconds(
                    kExperimentPostQcAuditDeadlineMs));
            const auto now = aggregation_scheduler->monotonic_now();
            if (delay <= AggregationScheduler::Duration::zero() ||
                now > AggregationScheduler::Duration::max() - delay)
                return;
            const auto access = exact_runtime_access;
            experiment_post_qc_audit_deadline_cancellation =
                schedule_at_or_after_deadline(
                    *aggregation_scheduler,
                    now + delay,
                    [access]()
                    {
                        auto runtime = access->acquire();
                        if (runtime.has_value())
                            runtime->owner()
                                .dispatch_experiment_post_qc_audit_deadline();
                    },
                    [access]()
                    {
                        auto runtime = access->acquire();
                        if (runtime.has_value())
                            HOTSTUFF_LOG_WARN(
                                "KAURI_AUDIT deadline_schedule_failed "
                                "reporter=%u",
                                static_cast<unsigned>(
                                    runtime->owner().get_id()));
                    });
        }
        catch (...)
        {
        }
    }

    void HotStuffBase::synchronize_experiment_post_qc_audit(
        const ProposalContextLease &lease,
        ReplicaID authenticated_sender,
        std::uint64_t arrival_ns) noexcept
    {
        if (experiment_post_qc_audit == nullptr ||
            !experiment_post_qc_audit->is_reporter())
            return;
        try
        {
            const auto generation = find_exact_runtime_generation(
                lease.key().configuration);
            auto accumulator =
                proposal_contexts->clone_accumulator(lease);
            if (!generation.has_value() || accumulator == nullptr)
                return;
            const auto observation = experiment_post_qc_audit
                ->synchronize_reporter_accumulator(
                    lease.key(),
                    *generation,
                    *accumulator,
                    authenticated_sender,
                    arrival_ns);
            if (observation.has_value())
                emit_experiment_post_qc_audit_target(*observation);
        }
        catch (...)
        {
        }
    }

    bool HotStuffBase::observe_experiment_post_qc_audit_terminal_vote(
        const Vote &vote,
        ReplicaID authenticated_sender,
        std::uint64_t arrival_ns)
    {
        if (experiment_post_qc_audit == nullptr || vote.cert == nullptr ||
            proposal_contexts->context_status(vote.key()) !=
                ProposalContextStatus::terminal_closed)
            return false;
        const auto generation = find_exact_runtime_generation(
            vote.configuration());
        if (!generation.has_value())
            return false;
        bool started = false;
        try
        {
            started = experiment_post_qc_audit
                ->begin_target_verification(
                    vote.key(),
                    *generation,
                    authenticated_sender,
                    ExperimentPostQcAuditTargetPhase::post_close,
                    arrival_ns);
        }
        catch (...)
        {
            return false;
        }
        if (!started)
            return false;

        const auto access = exact_runtime_access;
        const auto retained_vote = std::make_shared<const Vote>(vote);
        try
        {
            retained_vote->cert
                ->verify(config.get_pubkey(authenticated_sender), vpool)
                .then(
                    [access,
                     retained_vote,
                     authenticated_sender,
                     generation = *generation](bool verified)
                    {
                        auto runtime = access->acquire();
                        if (!runtime.has_value())
                            return;
                        auto &owner = runtime->owner();
                        try
                        {
                            const auto observation =
                                owner.experiment_post_qc_audit
                                    ->complete_target_verification(
                                        retained_vote->key(),
                                        generation,
                                        authenticated_sender,
                                        owner.config,
                                        *retained_vote->cert,
                                        verified);
                            if (observation.has_value())
                                owner.emit_experiment_post_qc_audit_target(
                                    *observation);
                        }
                        catch (...)
                        {
                        }
                    });
        }
        catch (...)
        {
            static_cast<void>(experiment_post_qc_audit
                ->complete_target_verification(
                    vote.key(),
                    *generation,
                    authenticated_sender,
                    config,
                    *vote.cert,
                    false));
        }
        return true;
    }

    void HotStuffBase::dispatch_experiment_post_qc_audit_deadline()
        noexcept
    {
        if (experiment_post_qc_audit == nullptr)
            return;
        try
        {
            const auto now = adaptive_evidence_monotonic_now_ns();
            const auto claim = experiment_post_qc_audit
                ->consume_reporter_deadline(now);
            if (!claim.has_value())
                return;
            claim->relay.certificate->compute();
            if (!claim->relay.certificate->verify(config))
            {
                static_cast<void>(
                    experiment_post_qc_audit
                        ->complete_reporter_relay(false));
                return;
            }
            const auto &relay = claim->relay;
            const auto signers =
                experiment_audit_signers(claim->signers);
            HOTSTUFF_LOG_INFO(
                "KAURI_AUDIT missing_claim claim=missing_target reporter=%u "
                "target=%u root=%u epoch=%u tree=%u epoch_digest=%s "
                "block=%s generation=%llu window=%s armed_ns=%llu "
                "deadline_ns=%llu emitted_ns=%llu signers=%s",
                static_cast<unsigned>(relay.reporter),
                static_cast<unsigned>(relay.target),
                static_cast<unsigned>(relay.root),
                relay.proposal.configuration.epoch_number,
                relay.proposal.configuration.tree_id,
                relay.proposal.configuration.epoch_digest
                    .to_hex().c_str(),
                relay.proposal.block_hash.to_hex().c_str(),
                static_cast<unsigned long long>(relay.generation),
                relay.diagnostic_window.c_str(),
                static_cast<unsigned long long>(relay.armed_ns),
                static_cast<unsigned long long>(relay.deadline_ns),
                static_cast<unsigned long long>(relay.emitted_ns),
                signers.c_str());

            MsgExperimentPostQcAuditRelay message(relay);
            const auto wire_bytes = message.wire_bytes;
            const auto root_peer = config.get_peer_id(relay.root);
            const bool sent = !root_peer.is_null() &&
                pn.send_msg(message, root_peer);
            const auto sent_ns = adaptive_evidence_monotonic_now_ns();
            static_cast<void>(experiment_post_qc_audit
                ->complete_reporter_relay(sent));
            if (sent)
                HOTSTUFF_LOG_INFO(
                    "KAURI_AUDIT relay_sent reporter=%u target=%u "
                    "root=%u epoch=%u tree=%u epoch_digest=%s block=%s "
                    "generation=%llu window=%s deadline_ns=%llu "
                    "sent_ns=%llu signers=%s wire_bytes=%zu",
                    static_cast<unsigned>(relay.reporter),
                    static_cast<unsigned>(relay.target),
                    static_cast<unsigned>(relay.root),
                    relay.proposal.configuration.epoch_number,
                    relay.proposal.configuration.tree_id,
                    relay.proposal.configuration.epoch_digest
                        .to_hex().c_str(),
                    relay.proposal.block_hash.to_hex().c_str(),
                    static_cast<unsigned long long>(relay.generation),
                    relay.diagnostic_window.c_str(),
                    static_cast<unsigned long long>(relay.deadline_ns),
                    static_cast<unsigned long long>(sent_ns),
                    signers.c_str(),
                    wire_bytes);
        }
        catch (...)
        {
            static_cast<void>(experiment_post_qc_audit
                ->complete_reporter_relay(false));
        }
    }

    void HotStuffBase::prepare_experiment_post_qc_audit_root(
        const ProposalContextLease &lease,
        const QuorumCert &verified_qc) noexcept
    {
        if (experiment_post_qc_audit == nullptr ||
            !experiment_post_qc_audit->is_root())
            return;
        try
        {
            const auto generation = find_exact_runtime_generation(
                lease.key().configuration);
            const auto frozen_global_quorum =
                proposal_contexts->frozen_global_quorum(lease);
            const auto prepared_ns =
                adaptive_evidence_monotonic_now_ns();
            if (!generation.has_value() ||
                !frozen_global_quorum.has_value() || prepared_ns == 0)
                return;
            const auto snapshot = experiment_post_qc_audit->prepare_root(
                lease.key(),
                *generation,
                lease.generation(),
                lease.tree(),
                verified_qc,
                *frozen_global_quorum,
                prepared_ns);
            if (snapshot.has_value())
                emit_experiment_post_qc_audit_root_prepared(*snapshot);
        }
        catch (...)
        {
        }
    }

    void HotStuffBase::activate_experiment_post_qc_audit_root(
        const ProposalKey &key) noexcept
    {
        if (experiment_post_qc_audit == nullptr ||
            !experiment_post_qc_audit->is_root() ||
            aggregation_scheduler == nullptr)
            return;
        try
        {
            const auto prepared =
                experiment_post_qc_audit->root_snapshot();
            const auto block = storage->find_blk(key.block_hash);
            const auto published_ns =
                adaptive_evidence_monotonic_now_ns();
            const bool terminal =
                proposal_contexts->context_status(key) ==
                ProposalContextStatus::terminal_closed;
            if (!prepared.has_value() || prepared->proposal != key ||
                block == nullptr || block->self_qc == nullptr ||
                !experiment_post_qc_audit->activate_root(
                    key,
                    prepared->generation,
                    *block->self_qc,
                    published_ns,
                    terminal))
                return;
            const auto activated =
                experiment_post_qc_audit->root_snapshot();
            if (!activated.has_value())
                return;
            emit_experiment_post_qc_audit_root_snapshot(*activated);

            const auto delay = std::chrono::duration_cast<
                AggregationScheduler::Duration>(
                std::chrono::milliseconds(
                    kExperimentPostQcAuditRetentionMs));
            const auto now = aggregation_scheduler->monotonic_now();
            if (delay <= AggregationScheduler::Duration::zero() ||
                now > AggregationScheduler::Duration::max() - delay)
                return;
            const auto access = exact_runtime_access;
            experiment_post_qc_audit_expiry_cancellation =
                schedule_at_or_after_deadline(
                    *aggregation_scheduler,
                    now + delay,
                    [access]()
                    {
                        auto runtime = access->acquire();
                        if (runtime.has_value())
                            runtime->owner()
                                .expire_experiment_post_qc_audit_root();
                    });
        }
        catch (...)
        {
        }
    }

    void HotStuffBase::expire_experiment_post_qc_audit_root()
        noexcept
    {
        if (experiment_post_qc_audit == nullptr)
            return;
        static_cast<void>(experiment_post_qc_audit->expire(
            adaptive_evidence_monotonic_now_ns()));
    }

    void HotStuffBase::experiment_post_qc_audit_relay_handler(
        MsgExperimentPostQcAuditRelay &&message,
        const Net::conn_t &connection)
    {
        if (experiment_post_qc_audit == nullptr ||
            !experiment_post_qc_audit->is_root() ||
            connection == nullptr)
            return;
        const auto peer = connection->get_peer_id();
        const auto authenticated = peer_id_map.find(peer);
        if (peer.is_null() || authenticated == peer_id_map.end() ||
            !message.postponed_parse(this))
            return;
        const auto received_ns =
            adaptive_evidence_monotonic_now_ns();
        std::optional<ExperimentPostQcAuditRootVerification> request;
        try
        {
            request = experiment_post_qc_audit
                ->begin_root_verification(
                    message.relay,
                    authenticated->second,
                    received_ns);
        }
        catch (...)
        {
            return;
        }
        if (!request.has_value() ||
            request->relay.certificate == nullptr)
            return;

        const auto access = exact_runtime_access;
        const auto wire_bytes = message.wire_bytes;
        auto retained = std::make_shared<
            const ExperimentPostQcAuditRootVerification>(
                std::move(*request));
        try
        {
            retained->relay.certificate->verify(config, vpool).then(
                [access, retained, wire_bytes](bool verified)
                {
                auto runtime = access->acquire();
                if (!runtime.has_value())
                    return;
                auto &owner = runtime->owner();
                const auto verified_ns =
                    adaptive_evidence_monotonic_now_ns();
                const bool terminal = owner.proposal_contexts
                    ->context_status(retained->relay.proposal) ==
                    ProposalContextStatus::terminal_closed;
                const auto block = owner.storage->find_blk(
                    retained->relay.proposal.block_hash);
                const bool qc_unchanged =
                    experiment_audit_qc_unchanged(
                        block == nullptr
                            ? nullptr
                            : block->self_qc.get(),
                        retained->root_snapshot);
                const bool accepted = owner.experiment_post_qc_audit
                    ->complete_root_verification(
                        retained->relay.proposal,
                        retained->relay.generation,
                        verified_ns,
                        verified,
                        terminal,
                        qc_unchanged);
                if (!accepted)
                    return;
                const auto &options =
                    owner.experiment_post_qc_audit->options();
                const auto audit_signers =
                    experiment_audit_signers(
                        retained->audit_signers);
                const auto qc_signers =
                    experiment_audit_signers(
                        retained->root_snapshot.qc_signers);
                const auto qc_fingerprint =
                    experiment_audit_certificate_fingerprint(
                        retained->root_snapshot.frozen_qc);
                HOTSTUFF_LOG_INFO(
                    "KAURI_AUDIT root_witness phase=post_qc "
                    "reporter=%u target=%u root=%u epoch=%u tree=%u "
                    "epoch_digest=%s block=%s generation=%llu "
                    "context_generation=%llu window=%s "
                    "prepared_ns=%llu qc_published_ns=%llu "
                    "received_ns=%llu verified_ns=%llu "
                    "retention_deadline_ns=%llu deadline_ns=%llu "
                    "signers=%s qc_signers=%s wire_bytes=%zu "
                    "qc_fingerprint=%s consensus_context=terminal "
                    "qc_unchanged=1",
                    static_cast<unsigned>(options.reporter),
                    static_cast<unsigned>(options.target),
                    static_cast<unsigned>(options.root),
                    retained->relay.proposal.configuration.epoch_number,
                    retained->relay.proposal.configuration.tree_id,
                    retained->relay.proposal.configuration.epoch_digest
                        .to_hex().c_str(),
                    retained->relay.proposal.block_hash.to_hex().c_str(),
                    static_cast<unsigned long long>(
                        retained->relay.generation),
                    static_cast<unsigned long long>(
                        retained->root_snapshot.context_generation),
                    retained->relay.diagnostic_window.c_str(),
                    static_cast<unsigned long long>(
                        retained->root_snapshot.prepared_ns),
                    static_cast<unsigned long long>(
                        retained->root_snapshot.published_ns),
                    static_cast<unsigned long long>(
                        retained->received_ns),
                    static_cast<unsigned long long>(verified_ns),
                    static_cast<unsigned long long>(
                        retained->root_snapshot.expiry_ns),
                    static_cast<unsigned long long>(
                        retained->relay.deadline_ns),
                    audit_signers.c_str(),
                    qc_signers.c_str(),
                    wire_bytes,
                    qc_fingerprint.to_hex().c_str());
                });
        }
        catch (...)
        {
            const bool terminal = proposal_contexts->context_status(
                retained->relay.proposal) ==
                ProposalContextStatus::terminal_closed;
            const auto block = storage->find_blk(
                retained->relay.proposal.block_hash);
            const auto verified_ns =
                adaptive_evidence_monotonic_now_ns();
            static_cast<void>(experiment_post_qc_audit
                ->complete_root_verification(
                    retained->relay.proposal,
                    retained->relay.generation,
                    verified_ns,
                    false,
                    terminal,
                    experiment_audit_qc_unchanged(
                        block == nullptr
                            ? nullptr
                            : block->self_qc.get(),
                        retained->root_snapshot)));
        }
    }

    void HotStuffBase::set_tree_period(size_t nblocks)
    {
        if (epoch_protocol_mode != EpochProtocolMode::adaptive_v2 &&
            epoch_protocol_mode != EpochProtocolMode::adaptive_v3)
        {
            HotStuffCore::set_tree_period(nblocks);
            return;
        }
        if (nblocks == 0)
            throw std::invalid_argument(
                "adaptive tree switch period must be positive");
        if (adaptive_epoch_runtime != nullptr)
            throw std::logic_error(
                "adaptive tree switch period must be configured before startup");

        HotStuffCore::set_tree_period(nblocks);
        adaptive_v2_tree_switch_period = nblocks;
    }

    void HotStuffBase::configure_epoch_change_pre_vote_gate(
        EpochChangeIssuer issuer,
        EpochChangeDelayBounds delay_bounds,
        std::size_t maximum_block_extra_bytes,
        std::size_t maximum_ancestry_blocks,
        std::size_t maximum_bundle_bytes)
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
            maximum_ancestry_blocks == 0 || maximum_bundle_bytes == 0 ||
            maximum_block_extra_bytes > maximum_bundle_bytes)
            throw std::invalid_argument(
                "epoch-change voting bounds are invalid");

        auto verifier = std::make_unique<EpochChangeVerifier>(
            std::move(issuer), delay_bounds);
        const EpochChangeBundleLimits bundle_limits{
            maximum_bundle_bytes,
            maximum_block_extra_bytes,
            epoch_wire_limits};
        auto command_inbox = std::make_unique<AdaptiveV2CommandInbox>(
            AdaptiveV2CommandInboxLimits{
                maximum_bundle_bytes,
                maximum_block_extra_bytes,
                std::numeric_limits<std::uint64_t>::max()});
        epoch_change_maximum_block_extra_bytes =
            maximum_block_extra_bytes;
        epoch_change_maximum_ancestry_blocks =
            maximum_ancestry_blocks;
        adaptive_v2_epoch_change_bundle_limits = bundle_limits;
        adaptive_v2_command_inbox = std::move(command_inbox);
        epoch_change_verifier = std::move(verifier);
    }

    void HotStuffBase::bind_structured_event_emitters(
        StructuredEventEmitter *lifecycle_emitter,
        AdaptiveStructuredEventEmitter *aggregation_emitter,
        AuditStructuredEventEmitter *audit_emitter) noexcept
    {
        structured_event_emitter = lifecycle_emitter;
        adaptive_event_emitter = aggregation_emitter;
        audit_event_emitter = audit_emitter;
    }

    void HotStuffBase::bind_adaptive_v2_evidence_transport(
        EvidenceTransportCallback transport)
    {
        if (epoch_protocol_mode != EpochProtocolMode::adaptive_v2 ||
            adaptive_v2_response_evidence == nullptr)
            throw std::logic_error(
                "response evidence transport is available only in adaptive-v2");
        adaptive_v2_response_evidence->bind_transport(
            std::move(transport));
    }

    void HotStuffBase::unbind_adaptive_v2_evidence_transport() noexcept
    {
        if (adaptive_v2_response_evidence != nullptr)
            adaptive_v2_response_evidence->unbind_transport();
    }

    std::size_t HotStuffBase::flush_adaptive_v2_evidence() noexcept
    {
        if (epoch_protocol_mode != EpochProtocolMode::adaptive_v2 ||
            adaptive_v2_response_evidence == nullptr)
            return 0;
        return adaptive_v2_response_evidence->flush();
    }

    bool HotStuffBase::may_begin_local_proposal(
        const ConfigurationId &configuration) const noexcept
    {
        if (epoch_protocol_mode != EpochProtocolMode::adaptive_v2 &&
            epoch_protocol_mode != EpochProtocolMode::adaptive_v3)
            return true;
        if (adaptive_epoch_runtime == nullptr ||
            proposal_contexts == nullptr ||
            !adaptive_epoch_runtime->activation.admits_new_proposals())
            return false;
        const auto active =
            adaptive_epoch_runtime->activation.active_effect();
        if (active.configuration != configuration)
            return false;
        const auto proposal_configuration =
            proposal_contexts->active_configuration();
        if (!proposal_configuration.has_value() ||
            *proposal_configuration != configuration)
            return false;
        const auto *tree = find_exact_runtime_tree(configuration);
        if (tree == nullptr ||
            tree->get_tree().get_tree_root() != get_id())
            return false;
        return epoch_protocol_mode != EpochProtocolMode::adaptive_v3 ||
               adaptive_v3_activation_gate == nullptr ||
               adaptive_v3_activation_gate->may_authorize_vote(
                   active.configuration, active.generation);
    }

    bool HotStuffBase::admit_local(const Proposal &prop)
    {
        if (!may_begin_local_proposal(prop.configuration()))
            return false;
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
        const bool trace_local_root = lease->tree().root == get_id();
        if (trace_local_root)
            HOTSTUFF_LOG_INFO(
                "KAURI_LOCAL_PROPOSAL stage=candidate_begin replica=%u "
                "epoch=%u tree=%u block=%s",
                static_cast<unsigned>(get_id()),
                lease->key().configuration.epoch_number,
                lease->key().configuration.tree_id,
                lease->key().block_hash.to_hex().c_str());
        auto forwarding_candidate =
            make_exact_direct_forwarding_candidate(*lease, vote);
        if (trace_local_root)
            HOTSTUFF_LOG_INFO(
                "KAURI_LOCAL_PROPOSAL stage=candidate_end replica=%u "
                "epoch=%u tree=%u block=%s accepted=%u",
                static_cast<unsigned>(get_id()),
                lease->key().configuration.epoch_number,
                lease->key().configuration.tree_id,
                lease->key().block_hash.to_hex().c_str(),
                static_cast<unsigned>(forwarding_candidate != nullptr));
        if (forwarding_candidate == nullptr)
            return;
        if (trace_local_root)
            HOTSTUFF_LOG_INFO(
                "KAURI_LOCAL_PROPOSAL stage=record_begin replica=%u "
                "epoch=%u tree=%u block=%s",
                static_cast<unsigned>(get_id()),
                lease->key().configuration.epoch_number,
                lease->key().configuration.tree_id,
                lease->key().block_hash.to_hex().c_str());
        const bool recorded = proposal_contexts->record_local_part(
            *lease,
            config,
            get_id(),
            *vote.cert,
            std::move(forwarding_candidate));
        if (trace_local_root)
            HOTSTUFF_LOG_INFO(
                "KAURI_LOCAL_PROPOSAL stage=record_end replica=%u "
                "epoch=%u tree=%u block=%s accepted=%u",
                static_cast<unsigned>(get_id()),
                lease->key().configuration.epoch_number,
                lease->key().configuration.tree_id,
                lease->key().block_hash.to_hex().c_str(),
                static_cast<unsigned>(recorded));
        if (!recorded)
            return;
        synchronize_experiment_post_qc_audit(
            *lease,
            get_id(),
            adaptive_evidence_monotonic_now_ns());
        if (consume_experiment_outbound_direct_vote(
                lease->key(), lease->tree()))
        {
            static_cast<void>(proposal_contexts->close(
                lease->key(),
                ProposalContextEvent::proposal_aborted));
            return;
        }
        schedule_exact_vote_fallback(*lease, vote);
        if (proposal_contexts->delta_open_enabled(*lease))
        {
            if (!lease->tree().parent.has_value())
            {
                if (trace_local_root)
                    HOTSTUFF_LOG_INFO(
                        "KAURI_LOCAL_PROPOSAL stage=finish_begin "
                        "replica=%u epoch=%u tree=%u block=%s",
                        static_cast<unsigned>(get_id()),
                        lease->key().configuration.epoch_number,
                        lease->key().configuration.tree_id,
                        lease->key().block_hash.to_hex().c_str());
                try_finish_exact_context(*lease);
                if (trace_local_root)
                    HOTSTUFF_LOG_INFO(
                        "KAURI_LOCAL_PROPOSAL stage=finish_end "
                        "replica=%u epoch=%u tree=%u block=%s",
                        static_cast<unsigned>(get_id()),
                        lease->key().configuration.epoch_number,
                        lease->key().configuration.tree_id,
                        lease->key().block_hash.to_hex().c_str());
                return;
            }
            static_cast<void>(forward_exact_direct(*lease, vote));
            return;
        }
        if (trace_local_root)
            HOTSTUFF_LOG_INFO(
                "KAURI_LOCAL_PROPOSAL stage=finish_begin replica=%u "
                "epoch=%u tree=%u block=%s",
                static_cast<unsigned>(get_id()),
                lease->key().configuration.epoch_number,
                lease->key().configuration.tree_id,
                lease->key().block_hash.to_hex().c_str());
        try_finish_exact_context(*lease);
        if (trace_local_root)
            HOTSTUFF_LOG_INFO(
                "KAURI_LOCAL_PROPOSAL stage=finish_end replica=%u "
                "epoch=%u tree=%u block=%s",
                static_cast<unsigned>(get_id()),
                lease->key().configuration.epoch_number,
                lease->key().configuration.tree_id,
                lease->key().block_hash.to_hex().c_str());
    }

    void HotStuffBase::on_local_proposal_constructed(
        const Proposal &proposal)
    {
        if (epoch_protocol_mode != EpochProtocolMode::adaptive_v3 ||
            adaptive_epoch_runtime == nullptr || proposal.blk == nullptr)
            return;
        const auto generation =
            find_exact_runtime_generation(proposal.configuration());
        if (!generation.has_value() || *generation == 0)
            return;
        static_cast<void>(observe_proposal_view_generation(
            proposal.key(), *generation));
        // This is evidence-only. Capture the exact locally constructed
        // ProposalKey before admit_local or pipeline processing can block;
        // it never grants admission, a vote, cadence, or activation authority.
        static_cast<void>(
            retain_authenticated_proposal_commit_event_identities(
                proposal, *generation, nullptr, true));
    }

    void HotStuffBase::on_local_proposal_processed(
        const ProposalKey &key)
    {
        // Self-authored traffic is not evidence that another replica can
        // make progress in this view. Only received proposals, QCs, and
        // commits may postpone leader suspicion.
        if ((epoch_protocol_mode != EpochProtocolMode::adaptive_v2 &&
             epoch_protocol_mode != EpochProtocolMode::adaptive_v3) ||
            adaptive_v2_command_inbox == nullptr ||
            !adaptive_v2_pending_command_reservation.has_value())
            return;

        const auto reservation_token =
            *adaptive_v2_pending_command_reservation;
        adaptive_v2_pending_command_reservation.reset();
        auto &command_inbox = *adaptive_v2_command_inbox;
        if (!command_inbox.mark_proposed(reservation_token, key))
        {
            static_cast<void>(command_inbox.release(reservation_token));
            HOTSTUFF_LOG_WARN(
                "[EPOCH] Failed to bind successor command to exact proposal");
        }
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
        HOTSTUFF_LOG_INFO(
            "KAURI_PROPOSAL_BROADCAST stage=entry outcome=begin "
            "reason=none replica=%u epoch=%u tree=%u block=%s",
            static_cast<unsigned>(get_id()),
            prop.configuration().epoch_number,
            prop.configuration().tree_id,
            prop.key().block_hash.to_hex().c_str());

        const auto *tree = find_exact_runtime_tree(prop.configuration());
        if (tree == nullptr)
        {
            HOTSTUFF_LOG_INFO(
                "KAURI_PROPOSAL_BROADCAST stage=context outcome=skipped "
                "reason=exact_tree_missing replica=%u epoch=%u tree=%u "
                "block=%s",
                static_cast<unsigned>(get_id()),
                prop.configuration().epoch_number,
                prop.configuration().tree_id,
                prop.key().block_hash.to_hex().c_str());
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
            HOTSTUFF_LOG_INFO(
                "KAURI_PROPOSAL_BROADCAST stage=context outcome=skipped "
                "reason=inadmissible_context replica=%u epoch=%u tree=%u "
                "block=%s metadata=%u lease=%u finalized=%u",
                static_cast<unsigned>(get_id()),
                prop.configuration().epoch_number,
                prop.configuration().tree_id,
                prop.key().block_hash.to_hex().c_str(),
                metadata.has_value() ? 1U : 0U,
                lease.has_value() ? 1U : 0U,
                finalized_before_broadcast ? 1U : 0U);
            HOTSTUFF_LOG_WARN(
                "[BROADCASTING] Refusing an inadmissible exact proposal");
            return;
        }
        HOTSTUFF_LOG_INFO(
            "KAURI_PROPOSAL_BROADCAST stage=context outcome=ready "
            "reason=none replica=%u epoch=%u tree=%u block=%s root=%u "
            "direct_children=%zu lease=%u finalized=%u",
            static_cast<unsigned>(get_id()),
            prop.configuration().epoch_number,
            prop.configuration().tree_id,
            prop.key().block_hash.to_hex().c_str(),
            static_cast<unsigned>(metadata->tree.root),
            metadata->tree.direct_children.size(),
            lease.has_value() ? 1U : 0U,
            finalized_before_broadcast ? 1U : 0U);
        if (lease.has_value())
        {
            HOTSTUFF_LOG_INFO(
                "KAURI_PROPOSAL_BROADCAST stage=local_timers "
                "outcome=begin reason=none replica=%u epoch=%u tree=%u "
                "block=%s",
                static_cast<unsigned>(get_id()),
                prop.configuration().epoch_number,
                prop.configuration().tree_id,
                prop.key().block_hash.to_hex().c_str());
            if (epoch_protocol_mode == EpochProtocolMode::adaptive_v2)
                attempt_proposal_evidence_before_exposure(
                    prop.key(),
                    "root_response_deadline_arm_failed_before_"
                    "proposal_exposure");
            else
            {
                create_expected_vote_state(prop.key());
                static_cast<void>(start_latency_deadline(prop.key()));
                start_aggregation_timer(prop.key());
            }
            HOTSTUFF_LOG_INFO(
                "KAURI_PROPOSAL_BROADCAST stage=local_timers "
                "outcome=returned reason=none replica=%u epoch=%u tree=%u "
                "block=%s",
                static_cast<unsigned>(get_id()),
                prop.configuration().epoch_number,
                prop.configuration().tree_id,
                prop.key().block_hash.to_hex().c_str());
        }
        else if (epoch_protocol_mode == EpochProtocolMode::adaptive_v2)
        {
            // A finalized retransmit may reuse only a successful exact arm
            // from this live view generation. Without that provenance it
            // remains consensus-live but poisons evidence before transport.
            ensure_finalized_proposal_evidence_before_exposure(
                prop.key(),
                "root_response_deadline_arm_failed_before_"
                "proposal_exposure");
        }

        bytearray_t adaptive_payload;
        std::uint64_t wire_generation = 0;
        if (is_adaptive_epoch_mode(epoch_protocol_mode))
        {
            if (adaptive_epoch_runtime == nullptr)
            {
                HOTSTUFF_LOG_INFO(
                    "KAURI_PROPOSAL_BROADCAST stage=payload "
                    "outcome=skipped reason=runtime_missing replica=%u "
                    "epoch=%u tree=%u block=%s",
                    static_cast<unsigned>(get_id()),
                    prop.configuration().epoch_number,
                    prop.configuration().tree_id,
                    prop.key().block_hash.to_hex().c_str());
                return;
            }
            const auto generation = find_exact_runtime_generation(
                prop.configuration());
            if (!generation.has_value())
            {
                HOTSTUFF_LOG_INFO(
                    "KAURI_PROPOSAL_BROADCAST stage=payload "
                    "outcome=skipped reason=generation_missing replica=%u "
                    "epoch=%u tree=%u block=%s",
                    static_cast<unsigned>(get_id()),
                    prop.configuration().epoch_number,
                    prop.configuration().tree_id,
                    prop.key().block_hash.to_hex().c_str());
                return;
            }
            wire_generation = *generation;
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
                    epoch_wire_limits,
                    epoch_protocol_mode);
            }
            catch (...)
            {
                HOTSTUFF_LOG_INFO(
                    "KAURI_PROPOSAL_BROADCAST stage=payload "
                    "outcome=skipped reason=encode_exception replica=%u "
                    "epoch=%u tree=%u block=%s generation=%llu",
                    static_cast<unsigned>(get_id()),
                    prop.configuration().epoch_number,
                    prop.configuration().tree_id,
                    prop.key().block_hash.to_hex().c_str(),
                    static_cast<unsigned long long>(wire_generation));
                return;
            }
            if (adaptive_payload.empty())
            {
                HOTSTUFF_LOG_INFO(
                    "KAURI_PROPOSAL_BROADCAST stage=payload "
                    "outcome=skipped reason=empty_payload replica=%u "
                    "epoch=%u tree=%u block=%s generation=%llu",
                    static_cast<unsigned>(get_id()),
                    prop.configuration().epoch_number,
                    prop.configuration().tree_id,
                    prop.key().block_hash.to_hex().c_str(),
                    static_cast<unsigned long long>(wire_generation));
                return;
            }
            HOTSTUFF_LOG_INFO(
                "KAURI_PROPOSAL_BROADCAST stage=payload outcome=ready "
                "reason=none replica=%u epoch=%u tree=%u block=%s "
                "generation=%llu bytes=%zu",
                static_cast<unsigned>(get_id()),
                prop.configuration().epoch_number,
                prop.configuration().tree_id,
                prop.key().block_hash.to_hex().c_str(),
                static_cast<unsigned long long>(wire_generation),
                adaptive_payload.size());
            if (is_adaptive_epoch_mode(epoch_protocol_mode))
            {
                // A locally proposed block does not traverse the
                // authenticated remote-proposal callback. Retain the same
                // evidence-only identity that callback owns so a designated
                // adaptive-v3 observer can emit a gap-free authoritative
                // commit chain when its own proposal later commits.
                static_cast<void>(observe_proposal_view_generation(
                    prop.key(), *generation));
                static_cast<void>(
                    retain_authenticated_proposal_commit_event_identities(
                        prop, *generation, nullptr, true));
            }
        }

        std::size_t send_attempts = 0;
        std::size_t send_successes = 0;
        for (const auto child : metadata->tree.direct_children)
        {
            bool enqueued = false;
            if (is_adaptive_epoch_mode(epoch_protocol_mode))
                enqueued = pn.send_msg(
                    MsgPropose(DataStream(adaptive_payload)),
                    config.get_peer_id(child));
            else
                enqueued = pn.send_msg(
                    MsgPropose(prop), config.get_peer_id(child));
            ++send_attempts;
            if (enqueued)
                ++send_successes;
        }
        HOTSTUFF_LOG_INFO(
            "KAURI_PROPOSAL_BROADCAST stage=direct_children "
            "outcome=returned reason=none replica=%u epoch=%u tree=%u "
            "block=%s attempts=%zu successes=%zu",
            static_cast<unsigned>(get_id()),
            prop.configuration().epoch_number,
            prop.configuration().tree_id,
            prop.key().block_hash.to_hex().c_str(),
            send_attempts,
            send_successes);
        if (lease.has_value())
        {
            HOTSTUFF_LOG_INFO(
                "KAURI_PROPOSAL_BROADCAST stage=fallback_call "
                "outcome=begin reason=none replica=%u epoch=%u tree=%u "
                "block=%s",
                static_cast<unsigned>(get_id()),
                prop.configuration().epoch_number,
                prop.configuration().tree_id,
                prop.key().block_hash.to_hex().c_str());
            schedule_exact_proposal_fallback(*lease, prop);
            HOTSTUFF_LOG_INFO(
                "KAURI_PROPOSAL_BROADCAST stage=fallback_call "
                "outcome=returned reason=none replica=%u epoch=%u tree=%u "
                "block=%s",
                static_cast<unsigned>(get_id()),
                prop.configuration().epoch_number,
                prop.configuration().tree_id,
                prop.key().block_hash.to_hex().c_str());
        }
        HOTSTUFF_LOG_INFO(
            "KAURI_PROPOSAL_BROADCAST stage=summary outcome=complete "
            "reason=none replica=%u epoch=%u tree=%u block=%s "
            "generation=%llu payload_bytes=%zu children=%zu successes=%zu "
            "fallback_requested=%u",
            static_cast<unsigned>(get_id()),
            prop.configuration().epoch_number,
            prop.configuration().tree_id,
            prop.key().block_hash.to_hex().c_str(),
            static_cast<unsigned long long>(wire_generation),
            adaptive_payload.size(),
            send_attempts,
            send_successes,
            lease.has_value() ? 1U : 0U);
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
                owner.synchronize_experiment_post_qc_audit(
                    *lease,
                    owner.get_id(),
                    adaptive_evidence_monotonic_now_ns());
                if (owner.consume_experiment_outbound_direct_vote(
                        lease->key(), lease->tree()))
                {
                    static_cast<void>(owner.proposal_contexts->close(
                        lease->key(),
                        ProposalContextEvent::proposal_aborted));
                    return;
                }
                owner.schedule_exact_vote_fallback(*lease, vote);

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
                    if (is_adaptive_epoch_mode(
                            owner.epoch_protocol_mode))
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
                                    owner.epoch_wire_limits,
                                    owner.epoch_protocol_mode);
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
        const std::vector<ProposalKey> &committed_keys,
        const quorum_cert_bt &verified_direct_certifier) const
    {
        return resolve_committed_proposal_identity(
                   blk,
                   committed_keys,
                   verified_direct_certifier,
                   verified_direct_certifier == nullptr
                       ? CommittedProposalIdentityProvenance::
                             compatibility_unknown
                       : CommittedProposalIdentityProvenance::
                             verified_direct_certifier)
            .key;
    }

    HotStuffBase::CommittedProposalIdentityResolution
    HotStuffBase::resolve_committed_proposal_identity(
        const block_t &blk,
        const std::vector<ProposalKey> &committed_keys,
        const quorum_cert_bt &verified_direct_certifier,
        CommittedProposalIdentityProvenance provenance) const
    {
        const bool exact_provenance =
            (provenance == CommittedProposalIdentityProvenance::
                               verified_direct_certifier &&
             verified_direct_certifier != nullptr) ||
            (provenance == CommittedProposalIdentityProvenance::
                               legal_qc_skipped_ancestor &&
             verified_direct_certifier == nullptr) ||
            (provenance == CommittedProposalIdentityProvenance::
                               compatibility_unknown &&
             verified_direct_certifier == nullptr);
        if (blk == nullptr)
            return {
                std::nullopt,
                CommittedProposalIdentityDisposition::conflicting,
                provenance};
        if (!exact_provenance)
            return {
                std::nullopt,
                CommittedProposalIdentityDisposition::conflicting,
                provenance};

        std::optional<ProposalKey> resolved;
        const auto merge = [&blk, &resolved](
            const ProposalKey &candidate) noexcept {
            if (candidate.block_hash != blk->get_hash() ||
                (resolved.has_value() && *resolved != candidate))
                return false;
            resolved = candidate;
            return true;
        };

        try
        {
            if (verified_direct_certifier != nullptr)
            {
                const auto &certificate_key =
                    verified_direct_certifier->get_proposal_key();
                if (verified_direct_certifier->get_obj_hash() !=
                        blk->get_hash() ||
                    !verified_direct_certifier->has_n(config.nmajority) ||
                    !verified_direct_certifier->verify(config) ||
                    !merge(certificate_key))
                    return {
                        std::nullopt,
                        CommittedProposalIdentityDisposition::conflicting,
                        provenance};
            }

            if (blk->self_qc != nullptr &&
                !merge(blk->self_qc->get_proposal_key()))
                return {
                    std::nullopt,
                    CommittedProposalIdentityDisposition::conflicting,
                    provenance};

            for (const auto &committed_key : committed_keys)
                if (!merge(committed_key))
                    return {
                        std::nullopt,
                        CommittedProposalIdentityDisposition::conflicting,
                        provenance};

        }
        catch (...)
        {
            return {
                std::nullopt,
                CommittedProposalIdentityDisposition::conflicting,
                provenance};
        }

        if (!resolved.has_value())
            return {
                std::nullopt,
                provenance == CommittedProposalIdentityProvenance::
                                  legal_qc_skipped_ancestor
                    ? CommittedProposalIdentityDisposition::unavailable
                    : CommittedProposalIdentityDisposition::conflicting,
                provenance};
        return {
            resolved,
            CommittedProposalIdentityDisposition::exact,
            provenance};
    }

    bool HotStuffBase::observe_proposal_view_generation(
        const ProposalKey &key,
        std::uint64_t generation) noexcept
    {
        if (!is_adaptive_epoch_mode(epoch_protocol_mode) ||
            generation == 0)
            return false;
        try
        {
            const auto found = proposal_view_generations.find(key);
            if (found != proposal_view_generations.end())
            {
                if (!found->second || *found->second != generation)
                {
                    // A conflict is permanent for this exact key. Keeping the
                    // tombstone prevents a replay from restoring false
                    // precision to the evidence identity.
                    found->second.reset();
                    return false;
                }
                return true;
            }
            if (proposal_view_generations.size() >=
                maximum_proposal_view_generation_observations)
                return false;
            proposal_view_generations.emplace(key, generation);
            return true;
        }
        catch (...)
        {
            // Observation failure affects evidence completeness only.
            return false;
        }
    }

    bool HotStuffBase::retain_commit_event_identity(
        const ProposalKey &key,
        std::uint64_t generation) noexcept
    {
        if ((epoch_protocol_mode != EpochProtocolMode::adaptive_v2 &&
             epoch_protocol_mode != EpochProtocolMode::adaptive_v3) ||
            generation == 0)
            return false;
        try
        {
            auto found = retained_commit_event_identities.find(
                key.block_hash);
            if (found == retained_commit_event_identities.end())
            {
                if (retained_commit_event_identities.size() >=
                    maximum_proposal_view_generation_observations)
                    return false;
                retained_commit_event_identities.emplace(
                    key.block_hash,
                    RetainedCommitEventIdentity{
                        key,
                        generation,
                        key.configuration.epoch_number});
                return true;
            }

            auto &retained = found->second;
            retained.max_observed_epoch = std::max(
                retained.max_observed_epoch,
                key.configuration.epoch_number);
            if (!retained.key.has_value() ||
                !retained.view_generation.has_value())
                return false;
            if (*retained.key == key &&
                *retained.view_generation == generation)
                return true;
            retained.key.reset();
            retained.view_generation.reset();
            return false;
        }
        catch (...)
        {
            // Retention is evidence-only. Consensus remains live while the
            // later commit event fails closed as unavailable/conflicting.
            return false;
        }
    }

    bool HotStuffBase::retain_commit_event_identity_through_epoch(
        const ProposalKey &key,
        std::uint64_t generation,
        std::uint32_t last_live_epoch) noexcept
    {
        const auto identity_epoch = key.configuration.epoch_number;
        const bool adjacent_v3_retention =
            epoch_protocol_mode == EpochProtocolMode::adaptive_v3 &&
            identity_epoch != std::numeric_limits<std::uint32_t>::max() &&
            last_live_epoch == identity_epoch + 1;
        if (last_live_epoch < identity_epoch ||
            (last_live_epoch != identity_epoch &&
             !adjacent_v3_retention) ||
            !retain_commit_event_identity(key, generation))
            return false;
        try
        {
            const auto retained =
                retained_commit_event_identities.find(key.block_hash);
            if (retained == retained_commit_event_identities.end() ||
                retained->second.key != key ||
                retained->second.view_generation != generation)
                return false;
            retained->second.max_observed_epoch = std::max(
                retained->second.max_observed_epoch, last_live_epoch);
            return true;
        }
        catch (...)
        {
            // Extending evidence retention can only preserve an already
            // authenticated exact identity. Failure leaves the original
            // bounded lifetime unchanged and cannot affect consensus.
            return false;
        }
    }

    bool HotStuffBase::has_adjacent_proposal_commit_event_bridge_heights(
        std::uint32_t alternate_height,
        std::uint32_t skipped_height,
        std::uint32_t certifier_height) noexcept
    {
        return alternate_height !=
                   std::numeric_limits<std::uint32_t>::max() &&
               skipped_height == alternate_height + 1 &&
               skipped_height !=
                   std::numeric_limits<std::uint32_t>::max() &&
               certifier_height == skipped_height + 1;
    }

    bool HotStuffBase::
    has_bounded_proposal_commit_event_bridge_intermediates(
        EpochProtocolMode mode,
        const ConfigurationId &alternate_configuration,
        const ConfigurationId &certifier_configuration,
        std::size_t intermediate_count) noexcept
    {
        if (intermediate_count == 1)
            return true;
        if (intermediate_count !=
                maximum_proposal_commit_event_bridge_intermediates ||
            mode != EpochProtocolMode::adaptive_v3)
            return false;
        if (alternate_configuration == certifier_configuration)
            return true;
        return alternate_configuration.epoch_number !=
                   std::numeric_limits<std::uint32_t>::max() &&
               certifier_configuration.epoch_number ==
                   alternate_configuration.epoch_number + 1 &&
               certifier_configuration.tree_id == 0 &&
               certifier_configuration.epoch_digest !=
                   alternate_configuration.epoch_digest;
    }

    std::optional<std::pair<ConfigurationId, std::uint64_t>>
    HotStuffBase::
    authenticated_proposal_commit_event_bridge_configuration(
        EpochProtocolMode mode,
        const ConfigurationId &alternate_configuration,
        const ConfigurationId &certifier_configuration,
        std::uint64_t certifier_generation,
        std::optional<std::uint64_t> alternate_runtime_generation,
        std::optional<std::uint64_t> certifier_runtime_generation,
        std::optional<std::uint64_t> alternate_ingress_generation,
        std::optional<std::uint64_t> certifier_ingress_generation) noexcept
    {
        if (certifier_generation == 0 ||
            certifier_ingress_generation != certifier_generation)
            return std::nullopt;

        if (alternate_configuration == certifier_configuration)
        {
            if (alternate_ingress_generation.has_value())
                return *alternate_ingress_generation == certifier_generation
                    ? std::optional<
                          std::pair<ConfigurationId, std::uint64_t>>{
                          {certifier_configuration, certifier_generation}}
                    : std::nullopt;
            return mode == EpochProtocolMode::adaptive_v3 &&
                    certifier_runtime_generation == certifier_generation
                ? std::optional<
                      std::pair<ConfigurationId, std::uint64_t>>{
                      {certifier_configuration, certifier_generation}}
                : std::nullopt;
        }

        const bool adjacent_v3_activation_boundary =
            mode == EpochProtocolMode::adaptive_v3 &&
            alternate_configuration.epoch_number !=
                std::numeric_limits<std::uint32_t>::max() &&
            certifier_configuration.epoch_number ==
                alternate_configuration.epoch_number + 1 &&
            certifier_configuration.tree_id == 0 &&
            certifier_configuration.epoch_digest !=
                alternate_configuration.epoch_digest;
        if (!adjacent_v3_activation_boundary ||
            !alternate_runtime_generation.has_value() ||
            certifier_runtime_generation != certifier_generation ||
            *alternate_runtime_generation == certifier_generation ||
            (alternate_ingress_generation.has_value() &&
             alternate_ingress_generation != alternate_runtime_generation))
            return std::nullopt;
        return std::pair<ConfigurationId, std::uint64_t>{
            alternate_configuration, *alternate_runtime_generation};
    }

    bool HotStuffBase::
    preserve_adaptive_v3_cross_epoch_bridge_intermediate(
        const ProposalKey &inferred_predecessor_key,
        std::uint64_t predecessor_generation,
        const ConfigurationId &successor_configuration,
        std::uint64_t successor_generation,
        std::uint32_t last_live_epoch) noexcept
    {
        const auto &predecessor_configuration =
            inferred_predecessor_key.configuration;
        if (epoch_protocol_mode != EpochProtocolMode::adaptive_v3 ||
            predecessor_generation == 0 || successor_generation == 0 ||
            predecessor_configuration.epoch_number ==
                std::numeric_limits<std::uint32_t>::max() ||
            successor_configuration.epoch_number !=
                predecessor_configuration.epoch_number + 1 ||
            successor_configuration.tree_id != 0 ||
            successor_configuration.epoch_digest ==
                predecessor_configuration.epoch_digest ||
            last_live_epoch != successor_configuration.epoch_number)
            return false;
        try
        {
            const auto retained = retained_commit_event_identities.find(
                inferred_predecessor_key.block_hash);
            if (retained == retained_commit_event_identities.end())
            {
                // A physical intermediate at an activation boundary may
                // belong to either epoch. The certifier QC authenticates the
                // predecessor alternate, but it does not identify every
                // intervening block. Preserve completeness only when an
                // independently authenticated exact proposal was already
                // retained; never invent the intermediate configuration.
                return true;
            }
            auto &identity = retained->second;
            if (!identity.key.has_value() ||
                !identity.view_generation.has_value())
                return false;
            const bool exact_predecessor =
                *identity.key == inferred_predecessor_key &&
                *identity.view_generation == predecessor_generation;
            const bool exact_successor =
                identity.key->block_hash ==
                    inferred_predecessor_key.block_hash &&
                identity.key->configuration == successor_configuration &&
                *identity.view_generation == successor_generation;
            if (!exact_predecessor && !exact_successor)
                return false;
            identity.max_observed_epoch = std::max(
                identity.max_observed_epoch, last_live_epoch);
            return true;
        }
        catch (...)
        {
            return false;
        }
    }

    bool HotStuffBase::retain_authenticated_proposal_commit_event_identities(
        const Proposal &proposal,
        std::uint64_t generation,
        RetainedCommitEventIdentityRollback *rollback,
        bool locally_constructed_certifier) noexcept
    {
        if (!is_adaptive_epoch_mode(epoch_protocol_mode) ||
            generation == 0 || proposal.blk == nullptr ||
            proposal.key().block_hash != proposal.blk->get_hash())
            return false;

        const auto retain_owned = [this, rollback](
            const ProposalKey &key,
            std::uint64_t retained_generation,
            std::uint32_t last_live_epoch) noexcept {
            const bool absent_before =
                retained_commit_event_identities.find(key.block_hash) ==
                retained_commit_event_identities.end();
            if (!retain_commit_event_identity_through_epoch(
                    key, retained_generation, last_live_epoch))
                return false;
            if (absent_before && rollback != nullptr &&
                rollback->owned_mutation_count <
                    rollback->owned_mutations.size())
                rollback->owned_mutations[
                    rollback->owned_mutation_count++] =
                    RetainedCommitEventIdentityOwnedMutation{
                        key.block_hash, key, retained_generation};
            return true;
        };

        if (!retain_owned(
                proposal.key(),
                generation,
                proposal.key().configuration.epoch_number))
            return false;

        try
        {
            const auto &certifier = proposal.blk;
            if (certifier->parents.size() != 1 ||
                certifier->qc_ref == nullptr)
                return true;
            const auto &alternate = certifier->qc_ref;
            std::array<block_t,
                       maximum_proposal_commit_event_bridge_intermediates>
                intermediates{};
            std::size_t intermediate_count = 0;
            auto cursor = certifier->parents.front();
            while (cursor != alternate)
            {
                if (cursor == nullptr ||
                    intermediate_count == intermediates.size() ||
                    cursor->parents.size() != 1 ||
                    !has_verified_legal_qc_skip(certifier, cursor))
                    return true;
                intermediates[intermediate_count++] = cursor;
                cursor = cursor->parents.front();
            }
            if (intermediate_count == 0)
                return true;

            auto physical_predecessor = alternate;
            for (std::size_t index = intermediate_count; index-- > 0;)
            {
                const auto &intermediate = intermediates[index];
                if (physical_predecessor == nullptr ||
                    physical_predecessor->height ==
                        std::numeric_limits<std::uint32_t>::max() ||
                    intermediate->height !=
                        physical_predecessor->height + 1 ||
                    intermediate->parents.front() != physical_predecessor)
                    return true;
                physical_predecessor = intermediate;
            }
            if (physical_predecessor->height ==
                    std::numeric_limits<std::uint32_t>::max() ||
                certifier->height != physical_predecessor->height + 1 ||
                certifier->parents.front() != physical_predecessor)
                return true;

            const auto &alternate_key =
                certifier->qc->get_proposal_key();
            const auto &certifier_key = proposal.key();
            if (!has_bounded_proposal_commit_event_bridge_intermediates(
                    epoch_protocol_mode,
                    alternate_key.configuration,
                    certifier_key.configuration,
                    intermediate_count))
                return true;
            const auto alternate_ingress =
                authenticated_proposal_ingress.find(alternate_key);
            const auto certifier_ingress =
                authenticated_proposal_ingress.find(certifier_key);
            const auto certifier_authority_generation =
                locally_constructed_certifier
                    ? std::optional<std::uint64_t>{generation}
                    : certifier_ingress == authenticated_proposal_ingress.end()
                        ? std::optional<std::uint64_t>{}
                        : std::optional<std::uint64_t>{
                              certifier_ingress->second.view_generation};
            const auto bridged_configuration =
                authenticated_proposal_commit_event_bridge_configuration(
                    epoch_protocol_mode,
                    alternate_key.configuration,
                    certifier_key.configuration,
                    generation,
                    find_exact_runtime_generation(
                        alternate_key.configuration),
                    find_exact_runtime_generation(
                        certifier_key.configuration),
                    alternate_ingress == authenticated_proposal_ingress.end()
                        ? std::optional<std::uint64_t>{}
                        : std::optional<std::uint64_t>{
                              alternate_ingress->second.view_generation},
                    certifier_authority_generation);
            if (!bridged_configuration.has_value())
                return true;

            if (alternate_ingress == authenticated_proposal_ingress.end())
            {
                // The quorum certificate has already been verified by
                // has_verified_legal_qc_skip and carries alternate_key.  V3
                // may activate while predecessor blocks are still draining,
                // so the designated observer can first see the certified
                // successor chain at this authenticated certifier. Retain
                // the QC-authenticated alternate and its bounded physical
                // successors under the exact predecessor configuration and
                // generation authorized above. This is
                // evidence-only and does not enter proposal admission,
                // voting, rotation, or cadence.
                if (!retain_owned(
                        alternate_key,
                        bridged_configuration->second,
                        certifier_key.configuration.epoch_number))
                    return false;
            }

            // This bridge is deliberately evidence-only. The verified QC
            // carries the alternate's exact key, while the authenticated
            // certifier and exact active/draining runtimes bind the boundary
            // configuration and generation. V2 additionally requires
            // alternate ingress. V2 remains limited to one skipped physical
            // ancestor. V3 may recover a second for its exact configured
            // pipeline stretch, either within one configuration or across
            // the exact adjacent activation boundary. Recovered keys never
            // enter proposal admission, cadence, rotation, or consensus
            // identity state.
            for (std::size_t index = intermediate_count; index-- > 0;)
            {
                const ProposalKey intermediate_key{
                    bridged_configuration->first,
                    intermediates[index]->get_hash()};
                if (bridged_configuration->first !=
                    certifier_key.configuration)
                {
                    if (!preserve_adaptive_v3_cross_epoch_bridge_intermediate(
                            intermediate_key,
                            bridged_configuration->second,
                            certifier_key.configuration,
                            generation,
                            certifier_key.configuration.epoch_number))
                        return false;
                    continue;
                }
                if (!retain_owned(
                        intermediate_key,
                        bridged_configuration->second,
                        certifier_key.configuration.epoch_number))
                    return false;
            }
            return true;
        }
        catch (...)
        {
            // The outer authenticated proposal remains retained. Failure to
            // recover its skipped parent only reduces event completeness.
            return true;
        }
    }

    void HotStuffBase::rollback_retained_commit_event_identity_mutations(
        const RetainedCommitEventIdentityRollback &rollback) noexcept
    {
        try
        {
            for (std::size_t index = 0;
                 index < rollback.owned_mutation_count &&
                 index < rollback.owned_mutations.size();
                 ++index)
            {
                const auto &owned = rollback.owned_mutations[index];
                const auto retained = retained_commit_event_identities.find(
                    owned.block_hash);
                if (retained == retained_commit_event_identities.end() ||
                    !retained->second.key.has_value() ||
                    !retained->second.view_generation.has_value() ||
                    *retained->second.key != owned.key ||
                    *retained->second.view_generation !=
                        owned.view_generation)
                    continue;
                retained_commit_event_identities.erase(retained);
            }
        }
        catch (...)
        {
            // Rollback is evidence-only. A failure can only leave bounded
            // retained state that remains subject to normal capacity and
            // epoch-retirement rules.
        }
    }

    void HotStuffBase::
    forget_retained_commit_event_identities_before_epoch(
        std::uint32_t first_live_epoch) noexcept
    {
        try
        {
            for (auto retained =
                     retained_commit_event_identities.begin();
                 retained != retained_commit_event_identities.end();)
            {
                const auto retained_epoch =
                    retained->second.max_observed_epoch;
                const bool preserve_adjacent_v3_predecessor =
                    epoch_protocol_mode == EpochProtocolMode::adaptive_v3 &&
                    retained->second.key.has_value() &&
                    retained->second.key->configuration.epoch_number ==
                        retained_epoch &&
                    retained_epoch !=
                        std::numeric_limits<std::uint32_t>::max() &&
                    retained_epoch + 1 == first_live_epoch;
                if (retained_epoch < first_live_epoch &&
                    !preserve_adjacent_v3_predecessor)
                    retained = retained_commit_event_identities.erase(
                        retained);
                else
                    ++retained;
            }
        }
        catch (...)
        {}
    }

    std::optional<std::uint64_t>
    HotStuffBase::proposal_view_generation(
        const ProposalKey &key) const noexcept
    {
        try
        {
            const auto found = proposal_view_generations.find(key);
            if (found == proposal_view_generations.end())
                return std::nullopt;
            return found->second;
        }
        catch (...)
        {
            return std::nullopt;
        }
    }

    bool HotStuffBase::
    adaptive_v2_runtime_initialization_is_referenced(
        const ProposalKey &key) const noexcept
    {
        if (adaptive_v2_lifecycle_reporting_suppressed)
            return false;
        try
        {
            const auto commit =
                adaptive_v2_durable_commit_reports.find(key);
            if (commit != adaptive_v2_durable_commit_reports.end() &&
                commit->second.phase !=
                    AdaptiveV2DurableCommitPhase::suppressed)
                return true;
            const auto false_report =
                experiment_false_timeout_states.find(key);
            return false_report != experiment_false_timeout_states.end() &&
                   false_report->second.commit_deferred;
        }
        catch (...)
        {
            // Retention is safer than erasing a lifecycle predecessor whose
            // durable successor could not be inspected.
            return true;
        }
    }

    void HotStuffBase::retire_adaptive_v2_runtime_initialized_report(
        const ProposalKey &key) noexcept
    {
        try
        {
            if (!adaptive_v2_runtime_initialization_is_referenced(key))
                adaptive_v2_durable_initialization_reports.erase(key);
        }
        catch (...)
        {
            suppress_adaptive_v2_lifecycle_reporting(
                "runtime_initialization_retirement_failed");
        }
    }

    void HotStuffBase::forget_proposal_view_generation(
        const ProposalKey &key) noexcept
    {
        try
        {
            proposal_view_generations.erase(key);
            successful_response_attempt_arm_provenance.erase(key);
            response_attempt_arm_failure_markers.erase(key);
        }
        catch (...)
        {}
        retire_adaptive_v2_runtime_initialized_report(key);
    }

    void HotStuffBase::forget_proposal_view_generations_for_block(
        const uint256_t &block_hash) noexcept
    {
        try
        {
            for (auto observation = proposal_view_generations.begin();
                 observation != proposal_view_generations.end();)
            {
                if (observation->first.block_hash == block_hash)
                    observation = proposal_view_generations.erase(observation);
                else
                    ++observation;
            }
        }
        catch (...)
        {}
        try
        {
            for (auto arm =
                     successful_response_attempt_arm_provenance.begin();
                 arm != successful_response_attempt_arm_provenance.end();)
            {
                if (arm->first.block_hash == block_hash)
                    arm = successful_response_attempt_arm_provenance.erase(
                        arm);
                else
                    ++arm;
            }
            for (auto failure =
                     response_attempt_arm_failure_markers.begin();
                 failure != response_attempt_arm_failure_markers.end();)
            {
                if (failure->block_hash == block_hash)
                    failure =
                        response_attempt_arm_failure_markers.erase(failure);
                else
                    ++failure;
            }
        }
        catch (...)
        {}
        try
        {
            for (auto report =
                     adaptive_v2_durable_initialization_reports.begin();
                 report !=
                     adaptive_v2_durable_initialization_reports.end();)
            {
                if (report->first.block_hash == block_hash &&
                    !adaptive_v2_runtime_initialization_is_referenced(
                        report->first))
                    report =
                        adaptive_v2_durable_initialization_reports.erase(
                            report);
                else
                    ++report;
            }
        }
        catch (...)
        {
            suppress_adaptive_v2_lifecycle_reporting(
                "runtime_initialization_block_retirement_failed");
        }
    }

    void HotStuffBase::forget_proposal_view_generations_before_epoch(
        std::uint32_t first_live_epoch) noexcept
    {
        try
        {
            for (auto observation = proposal_view_generations.begin();
                 observation != proposal_view_generations.end();)
            {
                if (observation->first.configuration.epoch_number <
                    first_live_epoch)
                    observation = proposal_view_generations.erase(observation);
                else
                    ++observation;
            }
        }
        catch (...)
        {}
        try
        {
            for (auto arm =
                     successful_response_attempt_arm_provenance.begin();
                 arm != successful_response_attempt_arm_provenance.end();)
            {
                if (arm->first.configuration.epoch_number <
                    first_live_epoch)
                    arm = successful_response_attempt_arm_provenance.erase(
                        arm);
                else
                    ++arm;
            }
            for (auto failure =
                     response_attempt_arm_failure_markers.begin();
                 failure != response_attempt_arm_failure_markers.end();)
            {
                if (failure->configuration.epoch_number < first_live_epoch)
                    failure =
                        response_attempt_arm_failure_markers.erase(failure);
                else
                    ++failure;
            }
        }
        catch (...)
        {}
        try
        {
            for (auto report =
                     adaptive_v2_durable_initialization_reports.begin();
                 report !=
                     adaptive_v2_durable_initialization_reports.end();)
            {
                if (report->first.configuration.epoch_number <
                        first_live_epoch &&
                    !adaptive_v2_runtime_initialization_is_referenced(
                        report->first))
                    report =
                        adaptive_v2_durable_initialization_reports.erase(
                            report);
                else
                    ++report;
            }
        }
        catch (...)
        {
            suppress_adaptive_v2_lifecycle_reporting(
                "runtime_initialization_epoch_retirement_failed");
        }
    }

    void HotStuffBase::record_adaptive_commit_marker(
        const block_t &blk,
        const std::optional<ProposalKey> &committed_key) const
    {
        if (!adaptive_demo_markers || adaptive_epoch_runtime == nullptr)
            return;

        if (!committed_key.has_value())
        {
            HOTSTUFF_LOG_WARN(
                "KAURI_DEMO marker_skipped replica=%u height=%llu "
                "reason=ambiguous_committed_configuration",
                get_id(),
                blk->get_height());
            return;
        }

        const auto &key = *committed_key;
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

        const bool report_adaptive_v2_activation =
            epoch_protocol_mode == EpochProtocolMode::adaptive_v2;

        if (adaptive_v2_rotation_coordinator != nullptr)
            adaptive_v2_rotation_coordinator->reset_for_activation();

        const auto drain = adaptive_epoch_runtime->adapter
                               .drain_activated_futures();
        const auto &configuration =
            activation.update->activation.configuration;
        const auto generation =
            activation.update->activation.generation;
        if (report_adaptive_v2_activation &&
            adaptive_v2_reporting_outbox != nullptr &&
            adaptive_v2_committed_convergence_identity.has_value())
        {
            const auto &identity =
                *adaptive_v2_committed_convergence_identity;
            const auto successor_epoch_number =
                identity.successor_epoch_number;
            const auto committed_height = blk->get_height();
            const auto activation_height = identity.activation_height;
            if (configuration.epoch_number == successor_epoch_number &&
                configuration.epoch_digest ==
                    identity.successor_epoch_digest &&
                committed_height == activation_height)
            {
                adaptive_v2_activation_observation_pending = true;
                enqueue_pending_adaptive_v2_activation_observation();
            }
        }
        if (adaptive_v2_command_inbox != nullptr)
        {
            static_cast<void>(adaptive_v2_command_inbox->observe_activation(
                configuration, generation));
        }
        const auto *definition =
            activation.update->activation.definition;
        const auto event_activation_height =
            epoch_protocol_mode == EpochProtocolMode::adaptive_v2 ||
                definition == nullptr
                ? blk->get_height()
                : definition->activation_height();
        emit_epoch_lifecycle_event(
            EpochLifecycleTransition::activated,
            configuration,
            event_activation_height);
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
        const std::optional<ProposalKey> &committed_key)
    {
        if (proposal_admission == nullptr || exact_epochs == nullptr)
            return;

        const auto &active_configuration =
            proposal_admission->active_configuration();
        if (!committed_key.has_value() ||
            committed_key->configuration != active_configuration)
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
            retire_deferred_epoch_changes_before_epoch(
                first_live_epoch);
            proposal_admission->advance_retirement_floor(
                first_live_epoch);
            pending_exact_contributions.purge_before_epoch(
                first_live_epoch);
            // The open-context guard above preserves delayed-QC repair. Once
            // the old configuration is irreversibly retired, no parked or
            // armed fallback from it may retain immutable proposal state.
            discard_exact_fallbacks_before_epoch(first_live_epoch);
            proposal_contexts->advance_retirement_floor(
                first_live_epoch);
            forget_proposal_view_generations_before_epoch(
                first_live_epoch);
            forget_retained_commit_event_identities_before_epoch(
                first_live_epoch);
        }
    }

    void HotStuffBase::retire_deferred_epoch_changes_for_block(
        const uint256_t &block_hash) noexcept
    {
        for (auto recovery =
                 deferred_epoch_definition_recoveries.begin();
             recovery != deferred_epoch_definition_recoveries.end();)
        {
            auto &proposals = recovery->second.proposals;
            for (auto proposal = proposals.begin();
                 proposal != proposals.end();)
            {
                if (proposal->first.block_hash != block_hash)
                {
                    ++proposal;
                    continue;
                }
                const auto key = proposal->first;
                if (proposal_admission != nullptr)
                {
                    try
                    {
                        proposal_admission->retire_proposal(key);
                    }
                    catch (...)
                    {}
                }
                try
                {
                    purge_pending_exact_contributions(key);
                }
                catch (...)
                {}
                proposal = proposals.erase(proposal);
                if (deferred_epoch_change_proposal_count != 0)
                    --deferred_epoch_change_proposal_count;
            }
            if (proposals.empty())
                recovery =
                    deferred_epoch_definition_recoveries.erase(recovery);
            else
                ++recovery;
        }
    }

    void HotStuffBase::cache_adaptive_v2_commit(
        const block_t &blk,
        const CommittedProposalIdentityResolution &identity,
        bool allow_runtime_generation_recovery) noexcept
    {
        if (epoch_protocol_mode != EpochProtocolMode::adaptive_v2 &&
            epoch_protocol_mode != EpochProtocolMode::adaptive_v3)
            return;
        pending_adaptive_v2_commit.reset();
        if (blk == nullptr)
            return;
        auto disposition = identity.disposition;
        auto exact_key = identity.key;
        std::optional<std::uint64_t> generation;
        try
        {
            const bool exact_resolution =
                disposition ==
                    CommittedProposalIdentityDisposition::exact &&
                exact_key.has_value() &&
                exact_key->block_hash == blk->get_hash();
            const bool unavailable_resolution =
                disposition ==
                    CommittedProposalIdentityDisposition::unavailable &&
                !exact_key.has_value() &&
                identity.provenance ==
                    CommittedProposalIdentityProvenance::
                        legal_qc_skipped_ancestor;
            if (!exact_resolution && !unavailable_resolution)
            {
                disposition =
                    CommittedProposalIdentityDisposition::conflicting;
                exact_key.reset();
            }
            else if (exact_resolution)
            {
                const auto existing =
                    proposal_view_generations.find(*exact_key);
                if (existing != proposal_view_generations.end())
                {
                    generation = existing->second;
                    if (!generation.has_value())
                        disposition =
                            CommittedProposalIdentityDisposition::
                                conflicting;
                }
                else if (allow_runtime_generation_recovery)
                {
                    const auto runtime_generation =
                        find_exact_runtime_generation(
                            exact_key->configuration);
                    if (!runtime_generation.has_value())
                    {
                        disposition =
                            CommittedProposalIdentityDisposition::
                                conflicting;
                    }
                    else if (observe_proposal_view_generation(
                                 *exact_key, *runtime_generation))
                    {
                        generation = proposal_view_generation(*exact_key);
                        if (!generation.has_value())
                            disposition =
                                CommittedProposalIdentityDisposition::
                                    conflicting;
                    }
                    else
                    {
                        disposition =
                            CommittedProposalIdentityDisposition::
                                conflicting;
                    }
                }
                else
                {
                    disposition =
                        CommittedProposalIdentityDisposition::conflicting;
                }
            }
        }
        catch (...)
        {
            disposition =
                CommittedProposalIdentityDisposition::conflicting;
            generation.reset();
        }
        if (disposition != CommittedProposalIdentityDisposition::exact)
            exact_key.reset();

        auto event_disposition = disposition;
        auto event_key = exact_key;
        auto event_generation = generation;
        try
        {
            const auto retained =
                retained_commit_event_identities.find(blk->get_hash());
            if (disposition ==
                    CommittedProposalIdentityDisposition::exact &&
                exact_key.has_value() && generation.has_value())
            {
                if (retained != retained_commit_event_identities.end() &&
                    (!retained->second.key.has_value() ||
                     !retained->second.view_generation.has_value() ||
                     *retained->second.key != *exact_key ||
                     *retained->second.view_generation != *generation))
                {
                    event_disposition =
                        CommittedProposalIdentityDisposition::conflicting;
                    event_key.reset();
                    event_generation.reset();
                }
            }
            else if (
                disposition ==
                    CommittedProposalIdentityDisposition::unavailable &&
                identity.provenance ==
                    CommittedProposalIdentityProvenance::
                        legal_qc_skipped_ancestor &&
                retained != retained_commit_event_identities.end())
            {
                if (retained->second.key.has_value() &&
                    retained->second.view_generation.has_value() &&
                    retained->second.key->block_hash == blk->get_hash() &&
                    *retained->second.view_generation != 0)
                {
                    event_disposition =
                        CommittedProposalIdentityDisposition::exact;
                    event_key = retained->second.key;
                    event_generation =
                        retained->second.view_generation;
                }
                else
                {
                    event_disposition =
                        CommittedProposalIdentityDisposition::conflicting;
                    event_key.reset();
                    event_generation.reset();
                }
            }
            else if (
                epoch_protocol_mode == EpochProtocolMode::adaptive_v3 &&
                disposition ==
                    CommittedProposalIdentityDisposition::conflicting &&
                identity.provenance ==
                    CommittedProposalIdentityProvenance::core_unproven &&
                retained != retained_commit_event_identities.end())
            {
                // A multi-block commit batch may not carry a direct certifier
                // for every ancestor.  V3 retains the exact authenticated
                // proposal identity before consensus cleanup, with permanent
                // conflict tombstones.  Reuse only that evidence identity so
                // the designated observer can emit a complete authoritative
                // commit chain; protocol admission and consensus are
                // unchanged.
                if (retained->second.key.has_value() &&
                    retained->second.view_generation.has_value() &&
                    retained->second.key->block_hash == blk->get_hash() &&
                    *retained->second.view_generation != 0)
                {
                    event_disposition =
                        CommittedProposalIdentityDisposition::exact;
                    event_key = retained->second.key;
                    event_generation = retained->second.view_generation;
                }
                else
                {
                    event_disposition =
                        CommittedProposalIdentityDisposition::conflicting;
                    event_key.reset();
                    event_generation.reset();
                }
            }
            retained_commit_event_identities.erase(blk->get_hash());
        }
        catch (...)
        {
            event_disposition =
                CommittedProposalIdentityDisposition::conflicting;
            event_key.reset();
            event_generation.reset();
        }
        pending_adaptive_v2_commit.emplace(
            PendingAdaptiveV2Commit{
                blk->get_hash(),
                exact_key,
                generation,
                disposition,
                event_key,
                event_generation,
                event_disposition,
                std::nullopt});
    }

    std::optional<uint256_t>
    HotStuffBase::adaptive_v2_committed_epoch_change_payload_digest(
        const block_t &blk) const noexcept
    {
        if (!pending_committed_epoch_change.has_value() || blk == nullptr ||
            pending_committed_epoch_change->block_hash != blk->get_hash())
            return std::nullopt;
        return pending_committed_epoch_change->payload_digest;
    }

    void HotStuffBase::observe_authoritative_commit(
        const std::optional<ProposalKey> &committed_proposal,
        const std::optional<uint256_t> &committed_payload_digest) noexcept
    {
        if (adaptive_v2_command_inbox == nullptr ||
            !committed_proposal.has_value())
            return;
        static_cast<void>(
            adaptive_v2_command_inbox->observe_authoritative_commit(
                *committed_proposal, committed_payload_digest));
    }

    void HotStuffBase::do_consensus(const block_t &blk)
    {
        do_consensus_with_identity_provenance(
            blk,
            nullptr,
            CommittedProposalIdentityProvenance::compatibility_unknown);
    }

    void HotStuffBase::do_consensus(
        const block_t &blk,
        const quorum_cert_bt &verified_direct_certifier)
    {
        do_consensus_with_identity_provenance(
            blk,
            verified_direct_certifier,
            verified_direct_certifier == nullptr
                ? CommittedProposalIdentityProvenance::
                      compatibility_unknown
                : CommittedProposalIdentityProvenance::
                      verified_direct_certifier);
    }

    void HotStuffBase::do_consensus(
        const block_t &blk,
        const quorum_cert_bt &verified_direct_certifier,
        CommitCertifierDisposition certifier_disposition)
    {
        auto provenance =
            CommittedProposalIdentityProvenance::core_unproven;
        if (certifier_disposition ==
                CommitCertifierDisposition::verified_direct_certifier &&
            verified_direct_certifier != nullptr)
            provenance = CommittedProposalIdentityProvenance::
                verified_direct_certifier;
        else if (certifier_disposition ==
                     CommitCertifierDisposition::
                         legal_qc_skipped_ancestor &&
                 verified_direct_certifier == nullptr)
            provenance = CommittedProposalIdentityProvenance::
                legal_qc_skipped_ancestor;

        do_consensus_with_identity_provenance(
            blk, verified_direct_certifier, provenance);
    }

    void HotStuffBase::do_consensus_with_identity_provenance(
        const block_t &blk,
        const quorum_cert_bt &verified_direct_certifier,
        CommittedProposalIdentityProvenance provenance)
    {
        record_committed_epoch_change_history(blk);
        retire_deferred_epoch_changes_for_block(blk->get_hash());
        const auto keys =
            proposal_contexts->close_committed_block(blk->get_hash());
        const auto identity = resolve_committed_proposal_identity(
            blk, keys, verified_direct_certifier, provenance);
        const auto &authoritative_key = identity.key;
        const bool authoritative_key_has_local_context =
            authoritative_key.has_value() &&
            std::find(
                keys.begin(), keys.end(), *authoritative_key) != keys.end();
        retire_authoritative_absent_context(
            authoritative_key, authoritative_key_has_local_context);
        const auto committed_payload_digest =
            adaptive_v2_committed_epoch_change_payload_digest(blk);
        observe_authoritative_commit(
            authoritative_key, committed_payload_digest);
        // Preserve the authoritative committed key for protocol cadence and
        // copy optional evidence metadata before terminal cache cleanup.
        cache_adaptive_v2_commit(
            blk,
            identity,
            verified_direct_certifier != nullptr);
        report_adaptive_v2_committed(
            epoch_protocol_mode == EpochProtocolMode::adaptive_v3
                ? authoritative_key
                : pending_adaptive_v2_commit.has_value()
                ? pending_adaptive_v2_commit->committed_key
                : std::nullopt,
            !authoritative_key.has_value() ||
                authoritative_key_has_local_context);
        record_adaptive_commit_marker(blk, authoritative_key);
        pending_exact_contributions.purge_block(blk->get_hash());
        for (const auto &key : keys)
        {
            const bool preserve_authoritative_response_evidence =
                authoritative_key.has_value() &&
                key == *authoritative_key;
            purge_pending_exact_contributions(
                key,
                false,
                preserve_authoritative_response_evidence);
            erase_deferred_epoch_change(key);
            proposal_admission->retire_proposal(key);
            forget_proposal_view_generation(key);
        }
        forget_proposal_view_generations_for_block(blk->get_hash());
        const auto activation =
            epoch_protocol_mode == EpochProtocolMode::adaptive_v1 &&
                epoch_live_binding != nullptr
                ? epoch_live_binding->on_predecessor_commit(
                      blk->get_height(),
                      get_epoch_digest(get_cur_epoch_nr()))
                : EpochCommitIngressResult{};
        finish_adaptive_epoch_commit(blk, activation);
        advance_committed_retirement_floor(blk, authoritative_key);
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

    void HotStuffBase::do_post_block_commit(
        const block_t &blk,
        std::uint64_t commit_batch_index)
    {
        if (epoch_protocol_mode == EpochProtocolMode::adaptive_v3)
        {
            if (blk == nullptr)
            {
                pending_committed_epoch_change.reset();
                return;
            }
            emit_commit_observed_event(blk, commit_batch_index);
            if (pending_adaptive_v2_commit &&
                pending_adaptive_v2_commit->block_hash == blk->get_hash())
            {
                const auto disposition =
                    pending_adaptive_v2_commit->event_identity_disposition;
                if (disposition == CommittedProposalIdentityDisposition::exact &&
                    structured_event_emitter != nullptr &&
                    structured_event_emitter->is_designated_commit_observer())
                    emit_committed_block_event(
                        blk, pending_adaptive_v2_commit->event_committed_key,
                        pending_adaptive_v2_commit->event_view_generation,
                        commit_batch_index, std::nullopt);
                else if (
                    disposition == CommittedProposalIdentityDisposition::exact &&
                    pending_adaptive_v2_commit->event_committed_key.has_value() &&
                    pending_adaptive_v2_commit->event_view_generation.has_value())
                    emit_commit_identity_witness_event(
                        blk,
                        *pending_adaptive_v2_commit->event_committed_key,
                        *pending_adaptive_v2_commit->event_view_generation,
                        commit_batch_index);
                else if (disposition ==
                         CommittedProposalIdentityDisposition::unavailable)
                    emit_commit_identity_unavailable_event(
                        blk, commit_batch_index);
            }
            const auto committed_key =
                pending_adaptive_v2_commit &&
                    pending_adaptive_v2_commit->block_hash == blk->get_hash()
                ? pending_adaptive_v2_commit->committed_key
                : std::optional<ProposalKey>{};
            const auto committed_generation =
                pending_adaptive_v2_commit &&
                    pending_adaptive_v2_commit->block_hash == blk->get_hash()
                ? pending_adaptive_v2_commit->view_generation
                : std::optional<std::uint64_t>{};
            pending_adaptive_v2_commit.reset();
            process_adaptive_v3_post_block_commit(
                blk, committed_key, committed_generation);
            rotate_adaptive_v2_after_commit(committed_key);
            return;
        }
        if (epoch_protocol_mode != EpochProtocolMode::adaptive_v2)
            return;

        if (blk == nullptr)
        {
            pending_adaptive_v2_commit.reset();
            return;
        }
        emit_commit_observed_event(blk, commit_batch_index);

        std::optional<ProposalKey> committed_key;
        std::optional<ProposalKey> event_committed_key;
        std::optional<std::uint64_t> event_view_generation;
        std::optional<std::uint64_t>
            reporter_local_commit_monotonic_ns;
        auto event_identity_disposition =
            CommittedProposalIdentityDisposition::conflicting;
        if (pending_adaptive_v2_commit &&
            pending_adaptive_v2_commit->block_hash == blk->get_hash())
        {
            committed_key = pending_adaptive_v2_commit->committed_key;
            event_committed_key =
                pending_adaptive_v2_commit->event_committed_key;
            event_view_generation =
                pending_adaptive_v2_commit->event_view_generation;
            reporter_local_commit_monotonic_ns =
                pending_adaptive_v2_commit
                    ->reporter_local_commit_monotonic_ns;
            event_identity_disposition =
                pending_adaptive_v2_commit
                    ->event_identity_disposition;
        }
        pending_adaptive_v2_commit.reset();
        if (event_identity_disposition ==
            CommittedProposalIdentityDisposition::unavailable)
            emit_commit_identity_unavailable_event(
                blk, commit_batch_index);
        else if (event_identity_disposition ==
                 CommittedProposalIdentityDisposition::exact)
            emit_committed_block_event(
                blk,
                event_committed_key,
                event_view_generation,
                commit_batch_index,
                reporter_local_commit_monotonic_ns);
        else if (event_identity_disposition ==
                 CommittedProposalIdentityDisposition::conflicting)
            mark_adaptive_v2_convergence_evidence_unhealthy(
                "commit_event_identity_mismatched_or_conflicted");

        const auto fail_closed = [this](ActivationBlockReason reason) noexcept {
            pending_committed_epoch_change.reset();
            reset_committed_epoch_definition_recovery();
            mark_adaptive_v2_convergence_evidence_unhealthy(
                "activation_pipeline_failed");
            committed_epoch_change_history.reset();
            if (adaptive_epoch_runtime != nullptr)
                adaptive_epoch_runtime->adapter.fail_committed_v2(reason);
        };
        try
        {
            if (pending_committed_epoch_change &&
                pending_committed_epoch_change->block_hash != blk->get_hash())
            {
                fail_closed(
                    ActivationBlockReason::invalid_activation_record);
            }

            if (adaptive_epoch_runtime == nullptr ||
                epoch_live_binding == nullptr || exact_epochs == nullptr)
            {
                fail_closed(
                    ActivationBlockReason::invalid_activation_record);
                return;
            }

            if (pending_committed_epoch_change &&
                pending_committed_epoch_change->block_hash == blk->get_hash())
            {
                const auto &command =
                    pending_committed_epoch_change->command;
                if (committed_epoch_definition_recovery)
                {
                    const auto &recovery =
                        *committed_epoch_definition_recovery;
                    const bool exact_recovery_duplicate =
                        recovery.payload_digest ==
                            pending_committed_epoch_change->payload_digest &&
                        recovery.request.successor_epoch_digest ==
                            command.payload.successor_epoch_digest &&
                        encode_authorized_epoch_change(recovery.command) ==
                            encode_authorized_epoch_change(command);
                    if (!exact_recovery_duplicate)
                    {
                        HOTSTUFF_LOG_WARN(
                            "[EPOCH] Committed v2 duplicate conflicts with "
                            "the retained recovery identity");
                        fail_closed(
                            ActivationBlockReason::
                                conflicting_activation_record);
                        return;
                    }
                }
                const auto *const successor =
                    exact_epochs->find_epoch_by_digest(
                        command.payload.successor_epoch_digest);
                if (successor != nullptr &&
                    (successor->schema_version() !=
                        kEpochDefinitionSchemaVersionV2 ||
                    successor->activation_height() != 0 ||
                    successor->epoch_number() !=
                        command.payload.successor_epoch_number ||
                    successor->previous_epoch_digest() !=
                        command.payload.predecessor_epoch_digest ||
                    successor->epoch_digest() !=
                        command.payload.successor_epoch_digest))
                {
                    HOTSTUFF_LOG_WARN(
                        "[EPOCH] Committed v2 successor is invalid");
                    fail_closed(
                        ActivationBlockReason::invalid_activation_record);
                    return;
                }

                if (successor != nullptr)
                {
                    const auto prepared = adaptive_epoch_runtime->adapter
                                              .prepare_committed_v2(*successor);
                    if (prepared != EpochIngressError::none)
                    {
                        HOTSTUFF_LOG_WARN(
                            "[EPOCH] Failed to prepare committed v2 runtime");
                        fail_closed(
                            ActivationBlockReason::invalid_activation_record);
                        return;
                    }
                }

                const auto recorded =
                    adaptive_epoch_runtime->activation
                        .record_committed_v2(command, blk->get_height());
                const bool exact_existing_recovery =
                    committed_epoch_definition_recovery &&
                    recorded.record.has_value() &&
                    recorded.record->payload_digest ==
                        committed_epoch_definition_recovery->payload_digest &&
                    recorded.record->command_commit_height ==
                        committed_epoch_definition_recovery
                            ->command_commit_height &&
                    recorded.record->activation_height ==
                        committed_epoch_definition_recovery
                            ->activation_height &&
                    recorded.record->predecessor_epoch_digest ==
                        command.payload.predecessor_epoch_digest &&
                    recorded.record->successor_epoch_number ==
                        command.payload.successor_epoch_number &&
                    recorded.record->successor_epoch_digest ==
                        command.payload.successor_epoch_digest;
                const bool recoverable_missing_definition =
                    successor == nullptr &&
                    recorded.disposition ==
                        ActivationRecordDisposition::missing_definition &&
                    recorded.record.has_value() &&
                    recorded.record->payload_digest ==
                        pending_committed_epoch_change->payload_digest;
                const bool recoverable_missing_definition_duplicate =
                    successor == nullptr &&
                    recorded.disposition ==
                        ActivationRecordDisposition::duplicate &&
                    exact_existing_recovery;
                const bool available_definition_recorded =
                    successor != nullptr &&
                    (recorded.disposition ==
                         ActivationRecordDisposition::recorded ||
                     recorded.disposition ==
                         ActivationRecordDisposition::duplicate) &&
                    (!committed_epoch_definition_recovery ||
                     exact_existing_recovery);
                if (!recoverable_missing_definition &&
                    !recoverable_missing_definition_duplicate &&
                    !available_definition_recorded)
                {
                    HOTSTUFF_LOG_WARN(
                        "[EPOCH] Failed to record committed v2 command");
                    fail_closed(
                        adaptive_epoch_runtime->activation.blocked_reason());
                    return;
                }

                if ((recorded.disposition ==
                         ActivationRecordDisposition::recorded ||
                     recorded.disposition ==
                         ActivationRecordDisposition::missing_definition) &&
                    recorded.record.has_value())
                {
                    AdaptiveV2EpochChangeIdentity identity{
                        recorded.record->predecessor_epoch_number,
                        recorded.record->predecessor_epoch_digest,
                        recorded.record->successor_epoch_number,
                        recorded.record->successor_epoch_digest,
                        recorded.record->payload_digest,
                        blk->get_height(),
                        blk->get_hash(),
                        recorded.record->activation_delay_blocks,
                        recorded.record->activation_height};
                    adaptive_v2_committed_convergence_identity =
                        identity;
                    adaptive_v2_commit_observation_enqueued =
                        false;
                    emit_epoch_command_committed_event(
                        blk, command, *recorded.record);
                    if (adaptive_v2_reporting_outbox == nullptr)
                    {
                        mark_adaptive_v2_convergence_evidence_unhealthy(
                            "commit_observation_outbox_missing");
                    }
                    else
                        enqueue_pending_adaptive_v2_commit_observation();
                }

                if (recoverable_missing_definition &&
                    !retain_committed_epoch_definition_recovery(
                        blk, command, *recorded.record))
                {
                    fail_closed(
                        ActivationBlockReason::invalid_activation_record);
                    return;
                }
                pending_committed_epoch_change.reset();
            }

            if (committed_epoch_definition_recovery)
            {
                auto &recovery =
                    *committed_epoch_definition_recovery;
                if (blk->get_height() == recovery.activation_height)
                    recovery.activation_block = blk;
                else if (blk->get_height() > recovery.activation_height &&
                         recovery.activation_block == nullptr)
                {
                    fail_closed(
                        ActivationBlockReason::missed_activation_height);
                    return;
                }

                const auto *const recovered_definition =
                    exact_epochs->find_epoch_by_digest(
                        recovery.request.successor_epoch_digest);
                if (recovered_definition != nullptr &&
                    !recover_committed_epoch_definition(
                        *recovered_definition))
                {
                    fail_closed(
                        ActivationBlockReason::invalid_activation_record);
                    return;
                }
            }

            const auto active =
                adaptive_epoch_runtime->activation.active_effect();
            const auto &configuration = active.configuration;
            auto post_block_height = blk->get_height();
            if (committed_epoch_definition_recovery &&
                post_block_height >
                    committed_epoch_definition_recovery->activation_height)
            {
                post_block_height =
                    committed_epoch_definition_recovery->activation_height;
            }
            const auto activation =
                epoch_live_binding->on_v2_post_block_commit(
                    post_block_height, configuration.epoch_digest);
            finish_adaptive_epoch_commit(blk, activation);
            rotate_adaptive_v2_after_commit(committed_key);
        }
        catch (...)
        {
            fail_closed(
                ActivationBlockReason::invalid_activation_record);
            HOTSTUFF_LOG_WARN(
                "[EPOCH] Failed adaptive-v2 post-block commit processing");
        }
    }

    void HotStuffBase::rotate_adaptive_v2_after_commit(
        const std::optional<ProposalKey> &committed_key) noexcept
    {
        // A committed v3 transition pins one exact predecessor
        // configuration/generation until its readiness certificate is
        // applied.  Intra-epoch rotation is therefore paused only for that
        // bounded gate lifetime and resumes from successor tree zero after
        // publication resets the cadence.
        if ((epoch_protocol_mode != EpochProtocolMode::adaptive_v2 &&
             epoch_protocol_mode != EpochProtocolMode::adaptive_v3) ||
            adaptive_epoch_runtime == nullptr ||
            adaptive_v2_rotation_coordinator == nullptr ||
            (epoch_protocol_mode == EpochProtocolMode::adaptive_v3 &&
             adaptive_v3_activation_gate != nullptr))
            return;

        const auto active =
            adaptive_epoch_runtime->activation.active_effect();
        const auto rotation =
            adaptive_v2_rotation_coordinator->on_commit(
                committed_key,
                active.configuration,
                active.generation);
        if (rotation.disposition !=
                AdaptiveV2RotationDisposition::rotated ||
            !rotation.update.has_value())
            return;

        HOTSTUFF_LOG_INFO(
            "[EPOCH] Rotated active epoch=%u to tree=%u after %zu commits",
            rotation.update->activation.configuration.epoch_number,
            rotation.update->activation.configuration.tree_id,
            adaptive_v2_rotation_coordinator->period());
    }

    /**
     * Get the current tree id. Used for proposals.
     */
    uint32_t HotStuffBase::get_tree_id()
    {
        if (is_adaptive_epoch_mode(epoch_protocol_mode) &&
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
        if (is_adaptive_epoch_mode(epoch_protocol_mode) &&
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
        if (epoch_protocol_mode != EpochProtocolMode::adaptive_v1 &&
            epoch_protocol_mode != EpochProtocolMode::adaptive_v2 &&
            epoch_protocol_mode != EpochProtocolMode::adaptive_v3)
            return LeaderTimeoutRotationDisposition::legacy_fallback;
        if (adaptive_epoch_runtime == nullptr ||
            epoch_live_binding == nullptr)
            return LeaderTimeoutRotationDisposition::rejected;

        if (epoch_protocol_mode == EpochProtocolMode::adaptive_v2 ||
            epoch_protocol_mode == EpochProtocolMode::adaptive_v3)
        {
            // See rotate_adaptive_v2_after_commit: changing the predecessor
            // view after v3 has latched it would invalidate the certified
            // activation identity.
            if (adaptive_v2_rotation_coordinator == nullptr ||
                (epoch_protocol_mode == EpochProtocolMode::adaptive_v3 &&
                 adaptive_v3_activation_gate != nullptr))
                return LeaderTimeoutRotationDisposition::rejected;
            const auto rotation =
                adaptive_v2_rotation_coordinator->on_timeout(
                    expired_view.configuration,
                    expired_view.view_generation);
            if (rotation.disposition !=
                    AdaptiveV2RotationDisposition::rotated ||
                !rotation.update.has_value())
                return LeaderTimeoutRotationDisposition::rejected;
            HOTSTUFF_LOG_INFO(
                "[EPOCH] Rotated active epoch=%u to tree=%u after leader timeout",
                rotation.update->activation.configuration.epoch_number,
                rotation.update->activation.configuration.tree_id);
            return LeaderTimeoutRotationDisposition::rotated;
        }

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
        if (is_adaptive_epoch_mode(epoch_protocol_mode) &&
            adaptive_epoch_runtime != nullptr)
            return adaptive_epoch_runtime->topology.tree_count();
        return system_trees.size();
    }

    ReplicaID HotStuffBase::get_system_tree_root(int tid)
    {
        if (is_adaptive_epoch_mode(epoch_protocol_mode) &&
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
        if (is_adaptive_epoch_mode(epoch_protocol_mode) &&
            adaptive_epoch_runtime != nullptr)
            return adaptive_epoch_runtime->topology.current_tree()
                .get_tree().get_tree_root();
        return current_tree.get_tree_root();
    }

    TreeNetwork HotStuffBase::get_current_tree_network()
    {
        if (is_adaptive_epoch_mode(epoch_protocol_mode) &&
            adaptive_epoch_runtime != nullptr)
            return adaptive_epoch_runtime->topology.current_tree();
        return current_tree_network;
    }

    HotStuffBase::~HotStuffBase()
    {
        pmaker->shutdown();
        try
        {
            if (experiment_post_qc_audit_deadline_cancellation)
                experiment_post_qc_audit_deadline_cancellation();
            if (experiment_post_qc_audit_expiry_cancellation)
                experiment_post_qc_audit_expiry_cancellation();
        }
        catch (...)
        {}
        experiment_post_qc_audit_deadline_cancellation = {};
        experiment_post_qc_audit_expiry_cancellation = {};
        cancel_adaptive_v2_reporting_flush();
        cancel_adaptive_v3_observation_retry();
        reset_committed_epoch_definition_recovery();
        epoch_live_binding = nullptr;
        adaptive_epoch_runtime.reset();
        cancel_all_exact_forwarding_retries();
        cancel_all_exact_fallbacks();
        const bool unfinished_response_deadline = std::any_of(
            adaptive_v2_durable_commit_reports.begin(),
            adaptive_v2_durable_commit_reports.end(),
            [](const auto &entry) {
                return entry.second.phase !=
                    AdaptiveV2DurableCommitPhase::suppressed;
            });
        if (unfinished_response_deadline)
            mark_adaptive_v2_convergence_evidence_unhealthy(
                "response_deadline_cancelled_during_shutdown");
        // ExactRuntimeAccess leases do not own HotStuffBase and cannot start
        // destruction. The external owner closes this gate, waits for every
        // in-flight event-loop callback, and only then mutates or destroys the
        // bridge captured by those callbacks. Timers that wake afterward fail
        // acquire() without touching bridge state.
        exact_runtime_access->close_and_wait();
        if (adaptive_v2_response_evidence != nullptr)
            adaptive_v2_response_evidence->shutdown();
        adaptive_v2_durable_commit_reports.clear();
        adaptive_v2_durable_initialization_reports.clear();
        deferred_epoch_definition_recoveries.clear();
        deferred_epoch_change_proposal_count = 0;
        proposal_contexts->shutdown();
        pending_exact_contributions.clear();
        adaptive_v2_committed_convergence_identity.reset();
        if (adaptive_v2_response_evidence != nullptr)
        {
            adaptive_v2_response_evidence->unbind_transport();
            adaptive_v2_response_evidence.reset();
        }
        if (adaptive_v2_reporting_outbox != nullptr)
        {
            adaptive_v2_reporting_outbox->shutdown();
            adaptive_v2_reporting_outbox.reset();
        }
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
        register_initial_epoch(epochs.back());
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
                !pmaker->configure_leader_progress(
                    exact_fallback_recovery_horizon(
                        maximum_timeout)))
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
        if (epoch_protocol_mode == EpochProtocolMode::adaptive_v2)
            return;

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

        if ((epoch_protocol_mode == EpochProtocolMode::adaptive_v2 ||
             epoch_protocol_mode == EpochProtocolMode::adaptive_v3) &&
            !adaptive_v2_tree_switch_period.has_value())
        {
            throw HotStuffError(
                "adaptive startup requires a positive tree switch period");
        }
        if (epoch_protocol_mode == EpochProtocolMode::adaptive_v3)
        {
            if (!adaptive_v3_config ||
                replicas.size() !=
                    adaptive_v3_config->readiness_membership.size() ||
                !derive_byzantine_quorum(replicas.size()).has_value())
                throw HotStuffError(
                    "adaptive-v3 startup requires the exact configured N=3f+1 membership");
            for (std::size_t index = 0; index < replicas.size(); ++index)
            {
                const auto *key = dynamic_cast<const PubKeyBLS *>(
                    std::get<1>(replicas[index]).get());
                if (key == nullptr || key->to_bytes() !=
                    adaptive_v3_config->readiness_membership[index]
                        .public_key.to_bytes())
                    throw HotStuffError(
                        "adaptive-v3 consensus and readiness membership keys differ");
            }
        }
        if (epoch_protocol_mode == EpochProtocolMode::adaptive_v2 &&
            (epoch_change_verifier == nullptr ||
             epoch_change_maximum_block_extra_bytes == 0 ||
             epoch_change_maximum_ancestry_blocks == 0 ||
             !adaptive_v2_epoch_change_bundle_limits.has_value() ||
             adaptive_v2_command_inbox == nullptr ||
             !epoch_manager_peer.has_value() ||
             !epoch_manager_address.has_value()))
        {
            throw HotStuffError(
                "adaptive-v2 startup requires pinned verifier, bounds, inbox, and manager TLS peer");
        }
        if (epoch_protocol_mode == EpochProtocolMode::adaptive_v2 &&
            !derive_byzantine_quorum(replicas.size()).has_value())
        {
            throw HotStuffError(
                "adaptive-v2 startup requires exact N = 3f + 1");
        }

        /* ./examples/hotstuff-client */
        // snprintf(client_prog, sizeof(client_prog), "./examples/hotstuff-client --idx %d --iter -1 --max-async 50 > clientlog%d &", get_id(), get_id());

        /* Initial tree config */

        HotStuffBase::tree_scheduler(std::move(replicas), true);
        /* ((n - 1) + 1 - 1) / 3 */
        uint32_t nfaulty = peers.size() / 3;
        const auto byzantine =
            (epoch_protocol_mode == EpochProtocolMode::adaptive_v2 ||
             epoch_protocol_mode == EpochProtocolMode::adaptive_v3)
                ? derive_byzantine_quorum(config.nreplicas)
                : std::optional<ByzantineQuorum>();
        if ((epoch_protocol_mode == EpochProtocolMode::adaptive_v2 ||
             epoch_protocol_mode == EpochProtocolMode::adaptive_v3) &&
            (!byzantine.has_value() ||
             byzantine->fault_threshold != nfaulty))
        {
            throw HotStuffError(
                "adaptive startup requires exact N = 3f + 1");
        }
        for (const PeerId &peer : peers)
        {
            pn.conn_peer(peer);
        }
        if (nfaulty == 0)
            LOG_WARN("too few replicas in the system to tolerate any failure");
        on_init(nfaulty);
        if ((epoch_protocol_mode == EpochProtocolMode::adaptive_v2 ||
             epoch_protocol_mode == EpochProtocolMode::adaptive_v3) &&
            config.nmajority != byzantine->quorum)
        {
            throw HotStuffError(
                "adaptive startup quorum does not equal 2f + 1");
        }
        pmaker->init(this);

        // TODO: Make this less ugly
        // Due to how the system is deployed, this is an alternative to correctly setup the PM's first proposer
        get_pace_maker()->update_tree_proposer();
        activate_initial_leader_view();
        if (epoch_protocol_mode == EpochProtocolMode::adaptive_v1 ||
            epoch_protocol_mode == EpochProtocolMode::adaptive_v2 ||
            epoch_protocol_mode == EpochProtocolMode::adaptive_v3)
            initialize_adaptive_epoch_runtime();
        get_pace_maker()->setup();

        if (ec_loop)
            ec.dispatch();

        // max_cmd_pending_size = blk_size * 100; // Hold up till 100 block worth of commands
        max_cmd_pending_size = blk_size; // Hold up till 1 block worth of commands
        final_buffer.reserve(blk_size);
        cmd_pending_buffer.reserve(max_cmd_pending_size);

        if (epoch_protocol_mode != EpochProtocolMode::adaptive_v2 &&
            epoch_protocol_mode != EpochProtocolMode::adaptive_v3)
        {
            ev_report_timer = TimerEvent(ec, [this](TimerEvent &)
                                         { this->on_report_timer(); });
            ev_report_timer.add(report_period);
        }

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
        const auto reserve_adaptive_v2_command =
            [this](const std::vector<block_t> &proposal_parents,
                   bool include_latest_piped_parent)
            -> std::optional<AdaptiveV2CommandReservation>
        {
            if ((epoch_protocol_mode != EpochProtocolMode::adaptive_v2 &&
                 epoch_protocol_mode != EpochProtocolMode::adaptive_v3) ||
                adaptive_v2_command_inbox == nullptr ||
                adaptive_epoch_runtime == nullptr ||
                !committed_epoch_change_history.has_value() ||
                committed_epoch_change_history->head == nullptr ||
                proposal_parents.empty() || hqc.second == nullptr)
                return std::nullopt;

            auto &command_inbox = *adaptive_v2_command_inbox;
            const auto inbox_snapshot = command_inbox.snapshot();
            if ((inbox_snapshot.state !=
                     AdaptiveV2CommandInboxState::available &&
                 inbox_snapshot.state !=
                     AdaptiveV2CommandInboxState::in_flight) ||
                inbox_snapshot.material == nullptr)
                return std::nullopt;

            const auto active_effect =
                adaptive_epoch_runtime->activation.active_effect();
            const auto *active_tree = find_exact_runtime_tree(
                active_effect.configuration);
            if (active_tree == nullptr)
                return std::nullopt;

            auto exact_proposal_parents = proposal_parents;
            if (include_latest_piped_parent && !piped_queue.empty())
            {
                const auto piped_block =
                    storage->find_blk(piped_queue.back());
                if (exact_proposal_parents.front()->height <=
                    piped_block->height)
                    exact_proposal_parents.insert(
                        exact_proposal_parents.begin(), piped_block);
            }
            Block history_probe(
                exact_proposal_parents,
                {},
                hqc.second->clone(),
                bytearray_t{},
                exact_proposal_parents.front()->get_height() + 1,
                hqc.first,
                nullptr);
            const auto history = build_epoch_change_proposal_history(
                history_probe,
                *committed_epoch_change_history->head,
                active_effect.configuration.epoch_digest,
                committed_epoch_change_history->snapshot,
                epoch_change_maximum_block_extra_bytes,
                epoch_change_maximum_ancestry_blocks,
                epoch_protocol_mode);
            if (!history)
                return std::nullopt;

            const AdaptiveV2ProposalPreparation preparation{
                active_effect.configuration,
                active_effect.generation,
                get_id(),
                static_cast<ReplicaID>(
                    active_tree->get_tree().get_tree_root()),
                history.history};
            const auto prepared =
                command_inbox.prepare_for_proposal(preparation);
            return prepared.reservation;
        };

        if (piped_queue.size() > get_config().async_blocks + 1) {
            HOTSTUFF_LOG_PROTO("[PIPELINING] Piped queue is full! Current size: %d, Max Async Blocks: %d", piped_queue.size(), get_config().async_blocks);
            return;
        }

        // HOTSTUFF_LOG_PROTO("[INSIDE] Current proposer: %d", proposer);
        // HOTSTUFF_LOG_PROTO("[INSIDE] get_id: %d", get_id());

        if (proposer == get_id()) {
            const auto configuration = exact_configuration(
                get_cur_epoch_nr(), get_tree_id());
            if (!may_begin_local_proposal(configuration))
            {
                HOTSTUFF_LOG_INFO(
                    "KAURI_LOCAL_PROPOSAL stage=beat_callback_exit "
                    "replica=%u proposer=%u reason=authority_fenced",
                    static_cast<unsigned>(get_id()),
                    static_cast<unsigned>(proposer));
                return;
            }
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
                    const auto command_reservation =
                        reserve_adaptive_v2_command(parents, false);
                    block_t piped_block;
                    try
                    {
                        bytearray_t block_extra;
                        if (command_reservation.has_value())
                        {
                            adaptive_v2_pending_command_reservation =
                                command_reservation->token;
                            block_extra = command_reservation->material
                                              ->canonical_block_extra;
                        }
                        auto cmds = std::move(final_buffer);
                        piped_block = storage->add_blk(new Block(
                            parents,
                            cmds,
                            hqc.second->clone(),
                            std::move(block_extra),
                            parents[0]->height + 1,
                            current,
                            nullptr));
                        piped_queue.push_back(piped_block->hash);
                        HOTSTUFF_LOG_PROTO("[PIPELINING] Pushed piped block into queue: %.10s", piped_block->hash.to_hex().c_str());
                        print_pipe_queues(true, false);
                        HOTSTUFF_LOG_PROTO("propose piped %s", std::string(*piped_block).c_str());

                        /* broadcast to other replicas */
                        gettimeofday(&last_block_time, NULL);
                        on_deliver_blk(piped_block);
                        HOTSTUFF_LOG_INFO(
                            "KAURI_LOCAL_PROPOSAL "
                            "stage=piped_process_begin replica=%u "
                            "epoch=%u tree=%u block=%s",
                            static_cast<unsigned>(get_id()),
                            configuration.epoch_number,
                            configuration.tree_id,
                            piped_block->hash.to_hex().c_str());
                        Proposal prop = process_block(
                            piped_block, false, configuration);
                        HOTSTUFF_LOG_INFO(
                            "KAURI_LOCAL_PROPOSAL "
                            "stage=piped_process_end replica=%u "
                            "epoch=%u tree=%u block=%s",
                            static_cast<unsigned>(get_id()),
                            configuration.epoch_number,
                            configuration.tree_id,
                            piped_block->hash.to_hex().c_str());
                        on_local_proposal_processed(prop.key());
                        piped_block->piped_delivered = true;
                        HOTSTUFF_LOG_INFO(
                            "KAURI_LOCAL_PROPOSAL "
                            "stage=piped_broadcast_begin replica=%u "
                            "epoch=%u tree=%u block=%s",
                            static_cast<unsigned>(get_id()),
                            configuration.epoch_number,
                            configuration.tree_id,
                            piped_block->hash.to_hex().c_str());
                        do_broadcast_proposal(prop);
                        HOTSTUFF_LOG_INFO(
                            "KAURI_LOCAL_PROPOSAL "
                            "stage=piped_broadcast_end replica=%u "
                            "epoch=%u tree=%u block=%s",
                            static_cast<unsigned>(get_id()),
                            configuration.epoch_number,
                            configuration.tree_id,
                            piped_block->hash.to_hex().c_str());
                    }
                    catch (...)
                    {
                        if (command_reservation.has_value())
                        {
                            auto &command_inbox =
                                *adaptive_v2_command_inbox;
                            static_cast<void>(command_inbox.release(
                                command_reservation->token));
                            adaptive_v2_pending_command_reservation.reset();
                        }
                        throw;
                    }
                    HOTSTUFF_LOG_INFO(
                        "KAURI_LOCAL_PROPOSAL "
                        "stage=piped_scope_tail replica=%u block=%s",
                        static_cast<unsigned>(get_id()),
                        piped_block->hash.to_hex().c_str());
                    /*if (id == get_pace_maker()->get_proposer()) {
                        gettimeofday(&timeEnd, NULL);
                        long usec = ((timeEnd.tv_sec - timeStart.tv_sec) * 1000000 + timeEnd.tv_usec - timeStart.tv_usec);
                        stats.insert(std::make_pair(piped_block->hash, usec));
                    }*/
                    piped_submitted = false;
                    HOTSTUFF_LOG_INFO(
                        "KAURI_LOCAL_PROPOSAL "
                        "stage=piped_tail_released replica=%u block=%s "
                        "piped_submitted=%u",
                        static_cast<unsigned>(get_id()),
                        piped_block->hash.to_hex().c_str(),
                        piped_submitted ? 1U : 0U);

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
                    HOTSTUFF_LOG_INFO(
                        "KAURI_LOCAL_PROPOSAL "
                        "stage=piped_tail_end replica=%u block=%s",
                        static_cast<unsigned>(get_id()),
                        piped_block->hash.to_hex().c_str());
                    

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
                const auto command_reservation =
                    reserve_adaptive_v2_command(parents, true);
                try
                {
                    bytearray_t block_extra;
                    if (command_reservation.has_value())
                    {
                        adaptive_v2_pending_command_reservation =
                            command_reservation->token;
                        block_extra = command_reservation->material
                                          ->canonical_block_extra;
                    }
                    auto cmds = std::move(final_buffer);
                    on_propose(
                        cmds,
                        std::move(parents),
                        std::move(block_extra));
                }
                catch (...)
                {
                    if (command_reservation.has_value())
                    {
                        auto &command_inbox = *adaptive_v2_command_inbox;
                        static_cast<void>(command_inbox.release(
                            command_reservation->token));
                        adaptive_v2_pending_command_reservation.reset();
                    }
                    throw;
                }
            }
        }
        HOTSTUFF_LOG_INFO(
            "KAURI_LOCAL_PROPOSAL stage=beat_callback_exit replica=%u "
            "proposer=%u",
            static_cast<unsigned>(get_id()),
            static_cast<unsigned>(proposer));
        });
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
