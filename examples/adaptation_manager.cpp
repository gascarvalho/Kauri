/**
 * Copyright 2026 Goncalo Carvalho
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

#include <algorithm>
#include <cerrno>
#include <charconv>
#include <cctype>
#include <csignal>
#include <cstdint>
#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <exception>
#include <fcntl.h>
#include <limits>
#include <memory>
#include <optional>
#include <set>
#include <stdexcept>
#include <string>
#include <system_error>
#include <unistd.h>
#include <unordered_map>
#include <utility>
#include <vector>

#include "salticidae/crypto.h"
#include "salticidae/event.h"
#include "salticidae/network.h"
#include "salticidae/util.h"

#include "hotstuff/adaptive_v2_manager_controller.h"
#include "hotstuff/adaptive_v2_manager_delivery.h"
#include "hotstuff/structured_event.h"
#include "hotstuff/util.h"

namespace
{

using hotstuff::AdaptiveV2ManagerController;
using hotstuff::AdaptiveV2ManagerControllerConfig;
using hotstuff::AdaptiveV2ManagerControllerStatus;
using hotstuff::AdaptiveV2BundleDeliveryAttempt;
using hotstuff::AdaptiveV2BundleDeliveryStatus;
using hotstuff::AdaptiveV2ManagerIngress;
using hotstuff::AdaptiveV2ManagerIngressLimits;
using hotstuff::AdaptiveV2ManagerIngressStatus;
using hotstuff::AuthenticatedReporter;
using hotstuff::EpochChangeBundleLimits;
using hotstuff::EpochDefinitionInput;
using hotstuff::EpochTreeDefinition;
using hotstuff::EpochWireLimits;
using hotstuff::MsgAdaptiveV2EpochChangeBundle;
using hotstuff::MsgAdaptiveV2ReadinessNotice;
using hotstuff::MsgEvidenceReport;
using hotstuff::MsgProposalLifecycleNotice;
using hotstuff::PrivKeySecp256k1;
using hotstuff::ReplicaID;
using hotstuff::TreePlacementInput;
using hotstuff::TreeShape;
using hotstuff::bytearray_t;
using hotstuff::opcode_t;
using salticidae::Config;
using salticidae::EventContext;
using salticidae::NetAddr;
using salticidae::PeerId;

using ManagerNetwork = salticidae::PeerNetwork<opcode_t>;

constexpr std::size_t kSmokeReplicaCount = 7;
constexpr std::uint32_t kSmokeFaultThreshold = 2;
constexpr std::uint32_t kSmokeQuorum = 5;
constexpr std::uint32_t kInitialTreeId = 0;
constexpr std::uint64_t kInitialActivationGeneration = 1;
constexpr std::uint64_t kSnapshotSeed = 0xA2F7;
constexpr std::uint32_t kTimeoutsPerReporter = 2;
constexpr std::uint32_t kMinimumScoreDrop =
    (kSmokeFaultThreshold + 1) * kTimeoutsPerReporter;

static_assert(kMinimumScoreDrop == 6);

struct ReplicaEndpoint
{
    ReplicaID replica_id{0};
    NetAddr address;
    PeerId peer_id;
};

struct ManagerOptions
{
    NetAddr listen_address;
    bytearray_t tls_private_key_der;
    bytearray_t tls_certificate_der;
    PeerId local_peer_id;
    std::vector<ReplicaEndpoint> replicas;
    hotstuff::EpochChangeIssuerId issuer_id{0};
    PrivKeySecp256k1 issuer_private_key;
    std::uint64_t activation_delay_blocks{5};
    std::string bundle_output;
    std::string structured_event_run_id;
    std::string structured_event_source_instance;
    std::string structured_event_output;
};

template <typename Value>
Value parse_unsigned(
    const std::string &text,
    const char *field,
    bool positive)
{
    if (text.empty())
        throw std::invalid_argument(
            std::string(field) + " is required");

    Value value{0};
    const auto *begin = text.data();
    const auto *end = begin + text.size();
    const auto parsed = std::from_chars(begin, end, value, 10);
    if (parsed.ec != std::errc{} || parsed.ptr != end ||
        (positive && value == 0))
    {
        throw std::invalid_argument(
            std::string(field) +
            (positive
                 ? " must be a canonical positive unsigned decimal"
                 : " must be a canonical unsigned decimal"));
    }
    return value;
}

bytearray_t parse_hex(
    const std::string &text,
    const char *field,
    std::size_t exact_hex_characters = 0)
{
    if (text.empty() || text.size() % 2 != 0 ||
        (exact_hex_characters != 0 &&
         text.size() != exact_hex_characters) ||
        !std::all_of(
            text.begin(), text.end(),
            [](unsigned char value) {
                return std::isxdigit(value) != 0;
            }))
    {
        throw std::invalid_argument(
            std::string(field) + " must be canonical hexadecimal bytes");
    }
    return hotstuff::from_hex(text);
}

ReplicaEndpoint parse_replica_endpoint(const std::string &raw)
{
    const auto fields = salticidae::trim_all(
        salticidae::split(raw, ","));
    if (fields.size() != 3)
    {
        throw std::invalid_argument(
            "replica must use id,address,tls-certificate-der-hex");
    }

    const auto id = parse_unsigned<std::uint32_t>(
        fields[0], "replica id", false);
    if (id > std::numeric_limits<ReplicaID>::max())
        throw std::invalid_argument("replica id is out of range");

    NetAddr address(fields[1]);
    if (address.is_null())
        throw std::invalid_argument("replica address is invalid");

    const auto certificate_der = parse_hex(
        fields[2], "replica TLS certificate");
    const auto certificate = salticidae::X509::create_from_der(
        certificate_der);
    return {
        static_cast<ReplicaID>(id),
        address,
        PeerId(certificate)};
}

std::vector<ReplicaID> smoke_membership()
{
    std::vector<ReplicaID> membership;
    membership.reserve(kSmokeReplicaCount);
    for (std::size_t index = 0; index < kSmokeReplicaCount; ++index)
        membership.push_back(static_cast<ReplicaID>(index));
    return membership;
}

EpochDefinitionInput smoke_epoch_zero()
{
    const auto membership = smoke_membership();
    std::vector<EpochTreeDefinition> trees;
    trees.reserve(membership.size());
    for (std::uint32_t root = 0; root < membership.size(); ++root)
    {
        std::vector<ReplicaID> breadth_first;
        breadth_first.reserve(membership.size());
        for (std::size_t offset = 0;
             offset < membership.size();
             ++offset)
        {
            breadth_first.push_back(static_cast<ReplicaID>(
                (root + offset) % membership.size()));
        }
        trees.push_back(EpochTreeDefinition{
            root, 2, 2, std::move(breadth_first), {}});
    }
    return hotstuff::adaptive_v2_epoch_zero_input(
        membership, std::move(trees));
}

AdaptiveV2ManagerIngressLimits smoke_ingress_limits()
{
    AdaptiveV2ManagerIngressLimits limits;
    limits.maximum_members = kSmokeReplicaCount;
    limits.readiness_wire.maximum_payload_bytes = 256;
    limits.lifecycle_wire.maximum_payload_bytes = 512;
    limits.evidence_wire = {4096, 8, kSmokeReplicaCount};
    limits.proposal_index = {256, 16};
    limits.evidence_store = {512, 128};
    limits.lifecycle = {
        64, 32 * 1024, kSmokeReplicaCount, 512, 256,
        kSmokeReplicaCount};
    limits.lifecycle_accounting = {64, 32 * 1024, 512};
    limits.maximum_pending_lifecycle_facts_per_source = 64;
    return limits;
}

EpochChangeBundleLimits smoke_bundle_limits()
{
    return {
        64 * 1024,
        4096,
        EpochWireLimits{32 * 1024, 8, 8, 128, 2}};
}

AdaptiveV2ManagerControllerConfig smoke_controller_config(
    const ManagerOptions &options)
{
    AdaptiveV2ManagerControllerConfig config;
    config.selection.required_nonresponsive = kSmokeFaultThreshold;
    config.selection.minimum_score_drop = kMinimumScoreDrop;
    config.selection.minimum_timeouts_per_reporter =
        kTimeoutsPerReporter;
    config.selection.maximum_post_baseline_timeout_attempts = 128;
    config.selection.responsiveness_policy.policy_version =
        "adaptive-v2-controller-responsiveness-v1";
    config.selection.responsiveness_policy.attempt_window = 32;
    config.selection.responsiveness_policy.minimum_attempts = 2;
    config.selection.responsiveness_policy.minimum_response_rate_ppm =
        750'000;
    config.selection.responsiveness_policy.maximum_timeout_rate_ppm =
        250'000;
    config.selection.responsiveness_policy.trailing_timeout_streak = 2;
    config.selection.responsiveness_policy
        .latency_percentile_basis_points = 5'000;
    config.selection.snapshot_seed = kSnapshotSeed;
    config.placement = TreePlacementInput{
        smoke_membership(),
        TreeShape{2, 2, 5},
        kSnapshotSeed,
        "adaptive-v2-performance-optimization-v1"};
    config.activation_delay_blocks = options.activation_delay_blocks;
    config.issuer_id = options.issuer_id;
    config.issuer_private_key = options.issuer_private_key;
    config.bundle_limits = smoke_bundle_limits();
    return config;
}

hotstuff::StructuredEventConfig manager_structured_event_config(
    const ManagerOptions &options)
{
    return hotstuff::StructuredEventConfig{
        options.structured_event_run_id,
        hotstuff::StructuredEventSource{
            hotstuff::StructuredEventSourceKind::adaptation_manager,
            "adaptive-manager",
            options.structured_event_source_instance},
        std::nullopt,
        hotstuff::StructuredEventLimits{}};
}

const char *controller_status_name(
    AdaptiveV2ManagerControllerStatus status) noexcept
{
    switch (status)
    {
    case AdaptiveV2ManagerControllerStatus::awaiting_readiness:
        return "awaiting_readiness";
    case AdaptiveV2ManagerControllerStatus::awaiting_responsive_baseline:
        return "awaiting_responsive_baseline";
    case AdaptiveV2ManagerControllerStatus::baseline_frozen:
        return "baseline_frozen";
    case AdaptiveV2ManagerControllerStatus::awaiting_guarded_selection:
        return "awaiting_guarded_selection";
    case AdaptiveV2ManagerControllerStatus::successor_ready:
        return "successor_ready";
    case AdaptiveV2ManagerControllerStatus::already_ready:
        return "already_ready";
    case AdaptiveV2ManagerControllerStatus::unhealthy:
        return "unhealthy";
    }
    return "unknown";
}

const char *ingress_status_name(
    AdaptiveV2ManagerIngressStatus status) noexcept
{
    switch (status)
    {
    case AdaptiveV2ManagerIngressStatus::processed:
        return "processed";
    case AdaptiveV2ManagerIngressStatus::rejected_nonmember:
        return "rejected_nonmember";
    case AdaptiveV2ManagerIngressStatus::rejected_spoofed_source:
        return "rejected_spoofed_source";
    case AdaptiveV2ManagerIngressStatus::rejected_sequence:
        return "rejected_sequence";
    case AdaptiveV2ManagerIngressStatus::rejected_configuration:
        return "rejected_configuration";
    case AdaptiveV2ManagerIngressStatus::rejected_generation:
        return "rejected_generation";
    case AdaptiveV2ManagerIngressStatus::rejected_height_regression:
        return "rejected_height_regression";
    case AdaptiveV2ManagerIngressStatus::awaiting_corroboration:
        return "awaiting_corroboration";
    case AdaptiveV2ManagerIngressStatus::already_applied:
        return "already_applied";
    case AdaptiveV2ManagerIngressStatus::rejected_capacity:
        return "rejected_capacity";
    case AdaptiveV2ManagerIngressStatus::rejected_wire:
        return "rejected_wire";
    case AdaptiveV2ManagerIngressStatus::rejected_lifecycle:
        return "rejected_lifecycle";
    case AdaptiveV2ManagerIngressStatus::evidence_unhealthy:
        return "evidence_unhealthy";
    case AdaptiveV2ManagerIngressStatus::stopped:
        return "stopped";
    }
    return "unknown";
}

void write_exclusive_bundle(
    const std::string &path,
    const bytearray_t &bytes)
{
    if (path.empty() || bytes.empty())
        throw std::invalid_argument("bundle output path or bytes are empty");

    int fd = ::open(
        path.c_str(),
        O_WRONLY | O_CREAT | O_EXCL | O_CLOEXEC | O_NOFOLLOW,
        0600);
    if (fd < 0)
    {
        throw std::system_error(
            errno, std::generic_category(),
            "cannot exclusively create bundle output");
    }

    try
    {
        std::size_t offset = 0;
        while (offset < bytes.size())
        {
            const auto written = ::write(
                fd,
                bytes.data() + offset,
                bytes.size() - offset);
            if (written < 0 && errno == EINTR)
                continue;
            if (written <= 0)
            {
                throw std::system_error(
                    written < 0 ? errno : EIO,
                    std::generic_category(),
                    "cannot write canonical bundle output");
            }
            offset += static_cast<std::size_t>(written);
        }
        if (::fsync(fd) != 0)
        {
            throw std::system_error(
                errno, std::generic_category(),
                "cannot sync canonical bundle output");
        }
        const auto close_result = ::close(fd);
        fd = -1;
        if (close_result != 0)
        {
            throw std::system_error(
                errno, std::generic_category(),
                "cannot close canonical bundle output");
        }
    }
    catch (...)
    {
        if (fd >= 0)
            ::close(fd);
        ::unlink(path.c_str());
        throw;
    }
}

ManagerOptions parse_options(int argc, char **argv)
{
    Config config("hotstuff.gen.conf");
    auto opt_help = Config::OptValFlag::create(false);
    auto opt_listen = Config::OptValStr::create();
    auto opt_replicas = Config::OptValStrVec::create();
    auto opt_tls_private_key = Config::OptValStr::create();
    auto opt_tls_certificate = Config::OptValStr::create();
    auto opt_issuer_id = Config::OptValStr::create();
    auto opt_issuer_private_key = Config::OptValStr::create();
    auto opt_activation_delay = Config::OptValStr::create("5");
    auto opt_bundle_output = Config::OptValStr::create();
    auto opt_structured_event_run_id = Config::OptValStr::create();
    auto opt_structured_event_source_instance =
        Config::OptValStr::create();
    auto opt_structured_event_output = Config::OptValStr::create();

    config.add_opt("help", opt_help, Config::SWITCH_ON, 'h');
    config.add_opt("listen", opt_listen, Config::SET_VAL);
    config.add_opt("replica", opt_replicas, Config::APPEND);
    config.add_opt("tls-privkey", opt_tls_private_key, Config::SET_VAL);
    config.add_opt("tls-cert", opt_tls_certificate, Config::SET_VAL);
    config.add_opt("issuer-id", opt_issuer_id, Config::SET_VAL);
    config.add_opt(
        "issuer-private-key", opt_issuer_private_key, Config::SET_VAL);
    config.add_opt(
        "activation-delay-blocks", opt_activation_delay,
        Config::SET_VAL);
    config.add_opt("bundle-output", opt_bundle_output, Config::SET_VAL);
    config.add_opt(
        "structured-event-run-id",
        opt_structured_event_run_id,
        Config::SET_VAL);
    config.add_opt(
        "structured-event-source-instance",
        opt_structured_event_source_instance,
        Config::SET_VAL);
    config.add_opt(
        "structured-event-output",
        opt_structured_event_output,
        Config::SET_VAL);
    config.parse(argc, argv);
    if (opt_help->get())
    {
        config.print_help();
        std::exit(0);
    }

    ManagerOptions options;
    if (opt_listen->get().empty())
        throw std::invalid_argument("listen address is required");
    options.listen_address = NetAddr(opt_listen->get());
    if (options.listen_address.is_null())
        throw std::invalid_argument("listen address is invalid");

    options.tls_private_key_der = parse_hex(
        opt_tls_private_key->get(), "manager TLS private key");
    options.tls_certificate_der = parse_hex(
        opt_tls_certificate->get(), "manager TLS certificate");
    const auto local_certificate = salticidae::X509::create_from_der(
        options.tls_certificate_der);
    options.local_peer_id = PeerId(local_certificate);

    options.issuer_id = parse_unsigned<hotstuff::EpochChangeIssuerId>(
        opt_issuer_id->get(), "issuer id", true);
    options.issuer_private_key = PrivKeySecp256k1(parse_hex(
        opt_issuer_private_key->get(),
        "issuer private key", 64));
    // Force scalar validation while parsing, before network startup.
    hotstuff::PubKeySecp256k1 issuer_public_key(
        options.issuer_private_key);
    (void)issuer_public_key;
    options.activation_delay_blocks = parse_unsigned<std::uint64_t>(
        opt_activation_delay->get(), "activation delay", true);
    options.bundle_output = opt_bundle_output->get();
    if (options.bundle_output.empty())
        throw std::invalid_argument("bundle output path is required");
    options.structured_event_run_id =
        opt_structured_event_run_id->get();
    if (options.structured_event_run_id.empty())
        throw std::invalid_argument(
            "structured-event run ID is required");
    options.structured_event_source_instance =
        opt_structured_event_source_instance->get();
    if (options.structured_event_source_instance.empty())
        throw std::invalid_argument(
            "structured-event source instance is required");
    options.structured_event_output = opt_structured_event_output->get();
    if (options.structured_event_output.empty())
        throw std::invalid_argument(
            "structured-event output path is required");
    if (options.structured_event_output == options.bundle_output)
        throw std::invalid_argument(
            "structured-event and bundle outputs must be distinct");

    for (const auto &raw : opt_replicas->get())
        options.replicas.push_back(parse_replica_endpoint(raw));
    if (options.replicas.size() != kSmokeReplicaCount)
    {
        throw std::invalid_argument(
            "the frozen smoke manager requires exactly seven replicas");
    }
    std::sort(
        options.replicas.begin(), options.replicas.end(),
        [](const auto &left, const auto &right) {
            return left.replica_id < right.replica_id;
        });

    std::set<ReplicaID> ids;
    std::set<PeerId> peer_ids;
    std::set<std::string> addresses;
    for (std::size_t index = 0;
         index < options.replicas.size();
         ++index)
    {
        const auto &replica = options.replicas[index];
        if (replica.replica_id != index ||
            !ids.insert(replica.replica_id).second ||
            !peer_ids.insert(replica.peer_id).second ||
            !addresses.insert(std::string(replica.address)).second ||
            replica.peer_id == options.local_peer_id)
        {
            throw std::invalid_argument(
                "replica IDs, addresses, and TLS identities must be exact and unique");
        }
    }

    const auto quorum = hotstuff::derive_byzantine_quorum(
        options.replicas.size());
    if (!quorum.has_value() ||
        quorum->replica_count != kSmokeReplicaCount ||
        quorum->fault_threshold != kSmokeFaultThreshold ||
        quorum->quorum != kSmokeQuorum)
    {
        throw std::logic_error(
            "the frozen smoke manager must preserve N=7, f=2, Q=5");
    }
    return options;
}

ManagerNetwork::Config network_config(const ManagerOptions &options)
{
    ManagerNetwork::Config config;
    config.max_msg_size(4 << 20);
    config.nworker(1);
    config.enable_tls(true)
        .tls_key(new salticidae::PKey(
            salticidae::PKey::create_privkey_from_der(
                options.tls_private_key_der)))
        .tls_cert(new salticidae::X509(
            salticidae::X509::create_from_der(
                options.tls_certificate_der)));
    config.allow_unknown_peer(false);
    config.id_mode(ManagerNetwork::IdentityMode::CERT_BASED);
    return config;
}

class AdaptationManager final
{
public:
    AdaptationManager(
        EventContext &event_context,
        ManagerOptions options,
        const ManagerNetwork::Config &net_config,
        hotstuff::StructuredEventSink &structured_event_sink)
        : event_context_(event_context),
          options_(std::move(options)),
          network_(event_context_, net_config),
          ingress_(
              smoke_membership(),
              smoke_epoch_zero(),
              kInitialTreeId,
              kInitialActivationGeneration,
              smoke_ingress_limits()),
          controller_(ingress_, smoke_controller_config(options_)),
          structured_event_sink_(structured_event_sink)
    {
        for (const auto &replica : options_.replicas)
        {
            peer_to_replica_.emplace(
                replica.peer_id, replica.replica_id);
        }
        register_handlers();
    }

    int run()
    {
        salticidae::SigEvent interrupt(
            event_context_, [this](int) { event_context_.stop(); });
        salticidae::SigEvent terminate(
            event_context_, [this](int) { event_context_.stop(); });
        interrupt.add(SIGINT);
        terminate.add(SIGTERM);

        salticidae::TimerEvent structured_event_drain_timer;
        bool process_started = false;
        try
        {
            structured_event_drain_timer = salticidae::TimerEvent(
                event_context_,
                [this](salticidae::TimerEvent &timer) {
                    structured_event_sink_.drain();
                    const auto health = structured_event_sink_.health();
                    if (!health.healthy)
                    {
                        fail("structured_event_unhealthy");
                        return;
                    }
                    timer.add(0.05);
                });

            emit_process_lifecycle(
                hotstuff::ProcessLifecycleState::started);
            process_started = true;
            structured_event_sink_.drain();
            if (!structured_event_sink_.health().healthy)
            {
                failed_ = true;
                emit_process_lifecycle(
                    hotstuff::ProcessLifecycleState::stopping);
                event_context_.stop();
                structured_event_drain_timer.del();
                stop_runtime();
                emit_process_lifecycle(
                    hotstuff::ProcessLifecycleState::stopped);
                return 1;
            }

            network_stop_required_ = true;
            network_.start();
            for (const auto &replica : options_.replicas)
            {
                network_.add_peer(replica.peer_id);
                network_.set_peer_addr(
                    replica.peer_id, replica.address);
            }
            network_.listen(options_.listen_address);
            for (const auto &replica : options_.replicas)
                network_.conn_peer(replica.peer_id);

            HOTSTUFF_LOG_INFO(
                "KAURI_ADAPTIVE_MANAGER listening=%s n=7 f=2 quorum=5",
                std::string(options_.listen_address).c_str());
            emit_process_lifecycle(
                hotstuff::ProcessLifecycleState::ready);
            structured_event_drain_timer.add(0.05);
            event_context_.dispatch();

            emit_process_lifecycle(
                hotstuff::ProcessLifecycleState::stopping);
            event_context_.stop();
            structured_event_drain_timer.del();
            structured_event_sink_.drain();
            if (!structured_event_sink_.health().healthy)
                failed_ = true;
            stop_runtime();
            emit_process_lifecycle(
                hotstuff::ProcessLifecycleState::stopped);
            structured_event_sink_.drain();
            if (!structured_event_sink_.health().healthy)
                failed_ = true;
            return failed_ || !successor_distributed_ ? 1 : 0;
        }
        catch (...)
        {
            if (process_started)
            {
                emit_process_lifecycle(
                    hotstuff::ProcessLifecycleState::stopping);
            }
            event_context_.stop();
            structured_event_drain_timer.del();
            structured_event_sink_.drain();
            stop_runtime();
            if (process_started)
            {
                emit_process_lifecycle(
                    hotstuff::ProcessLifecycleState::stopped);
                structured_event_sink_.drain();
            }
            throw;
        }
    }

private:
    std::optional<ReplicaID> authenticated_source(
        const ManagerNetwork::conn_t &connection) const noexcept
    {
        try
        {
            if (connection == nullptr)
                return std::nullopt;
            const auto *certificate = connection->get_peer_cert();
            if (certificate == nullptr)
                return std::nullopt;
            const PeerId peer(*certificate);
            const auto found = peer_to_replica_.find(peer);
            if (found == peer_to_replica_.end())
                return std::nullopt;
            return found->second;
        }
        catch (...)
        {
            return std::nullopt;
        }
    }

    void fail(const char *reason) noexcept
    {
        failed_ = true;
        HOTSTUFF_LOG_WARN(
            "KAURI_ADAPTIVE_MANAGER fatal reason=%s", reason);
        event_context_.stop();
    }

    void stop_runtime() noexcept
    {
        event_context_.stop();
        if (network_stop_required_ && !network_stopped_)
        {
            network_stopped_ = true;
            try
            {
                network_.stop();
            }
            catch (...)
            {
                failed_ = true;
                HOTSTUFF_LOG_WARN(
                    "KAURI_ADAPTIVE_MANAGER fatal reason=network_stop_failed");
            }
        }
        if (!ingress_stopped_)
        {
            ingress_stopped_ = true;
            ingress_.shutdown();
        }
    }

    void emit_process_lifecycle(
        hotstuff::ProcessLifecycleState state) noexcept
    {
        structured_event_sink_.emit(
            hotstuff::StructuredEventPayload{
                hotstuff::ProcessLifecycleEvent{
                    state, std::nullopt}});
    }

    void emit_new_score_trajectory() noexcept
    {
        const auto &trajectory = controller_.score_trajectory();
        const auto evidence_cutoff = controller_.current_cutoff();
        while (emitted_score_trajectory_ < trajectory.size())
        {
            const hotstuff::AuditStructuredEventPayload event{
                hotstuff::ReputationEvidenceAppliedStructuredEvent{
                    evidence_cutoff,
                    trajectory[emitted_score_trajectory_]}};
            structured_event_sink_.emit_audit(event);
            if (!structured_event_sink_.health().healthy)
            {
                fail("structured_event_reputation_emit_failed");
                return;
            }
            ++emitted_score_trajectory_;
        }
    }

    void evaluate()
    {
        if (failed_ || successor_distributed_)
            return;
        const auto status = controller_.evaluate();
        emit_new_score_trajectory();
        if (failed_)
            return;
        HOTSTUFF_LOG_INFO(
            "KAURI_ADAPTIVE_MANAGER state=%s cutoff=%llu",
            controller_status_name(status),
            static_cast<unsigned long long>(
                controller_.current_cutoff()));
        if (status == AdaptiveV2ManagerControllerStatus::unhealthy)
        {
            fail("controller_unhealthy");
            return;
        }
        if (status != AdaptiveV2ManagerControllerStatus::successor_ready)
            return;

        const auto *bundle = controller_.successor_bundle();
        if (bundle == nullptr)
        {
            fail("missing_successor_bundle");
            return;
        }
        try
        {
            write_exclusive_bundle(
                options_.bundle_output, bundle->canonical_bytes());
            const MsgAdaptiveV2EpochChangeBundle message(*bundle);
            std::vector<AdaptiveV2BundleDeliveryAttempt> attempts;
            attempts.reserve(options_.replicas.size());
            for (const auto &replica : options_.replicas)
            {
                bool enqueued = false;
                try
                {
                    const auto connection =
                        network_.get_peer_conn(replica.peer_id);
                    const auto source = authenticated_source(connection);
                    if (connection == nullptr ||
                        connection->is_terminated() ||
                        !source.has_value() ||
                        *source != replica.replica_id)
                    {
                        HOTSTUFF_LOG_WARN(
                            "KAURI_ADAPTIVE_MANAGER bundle_recipient_unavailable replica=%u",
                            replica.replica_id);
                    }
                    else
                    {
                        enqueued = network_.send_msg(
                            message, connection);
                    }
                }
                catch (const std::exception &error)
                {
                    HOTSTUFF_LOG_WARN(
                        "KAURI_ADAPTIVE_MANAGER bundle_enqueue_failed replica=%u reason=%s",
                        replica.replica_id,
                        error.what());
                }
                catch (...)
                {
                    HOTSTUFF_LOG_WARN(
                        "KAURI_ADAPTIVE_MANAGER bundle_enqueue_failed replica=%u reason=unknown",
                        replica.replica_id);
                }
                attempts.push_back({
                    replica.replica_id, enqueued});
            }
            const auto delivery =
                hotstuff::assess_adaptive_v2_bundle_delivery(
                    smoke_membership(), kSmokeQuorum, attempts);
            if (delivery.status !=
                    AdaptiveV2BundleDeliveryStatus::assessed ||
                !delivery.delivery_requirement_satisfied)
            {
                throw std::runtime_error(
                    "fewer than Q distinct replicas accepted the one-shot bundle");
            }
            successor_distributed_ = true;
            const auto &payload = bundle->command().payload;
            HOTSTUFF_LOG_INFO(
                "KAURI_ADAPTIVE_MANAGER successor_distributed "
                "epoch=%u digest=%s bytes=%zu attempted=%zu "
                "successful=%zu required=%zu",
                payload.successor_epoch_number,
                payload.successor_epoch_digest.to_hex().c_str(),
                bundle->canonical_bytes().size(),
                delivery.attempted_recipients,
                delivery.successful_recipients,
                delivery.required_recipients);
        }
        catch (const std::exception &error)
        {
            HOTSTUFF_LOG_WARN(
                "KAURI_ADAPTIVE_MANAGER distribution_error=%s",
                error.what());
            fail("successor_distribution_failed");
        }
    }

    void log_ingress_failure(
        const char *message_kind,
        AdaptiveV2ManagerIngressStatus status,
        ReplicaID source) const noexcept
    {
        const auto audit = ingress_.audit_stats();
        const auto lifecycle = ingress_.lifecycle_stats();
        const auto &ledger = ingress_.ledger();
        HOTSTUFF_LOG_WARN(
            "KAURI_ADAPTIVE_MANAGER ingress_failure "
            "kind=%s status=%s status_code=%u source=%u "
            "audit_readiness_wire_rejections=%llu "
            "audit_lifecycle_wire_rejections=%llu "
            "audit_evidence_wire_rejections=%llu "
            "audit_nonmember_rejections=%llu "
            "audit_spoofed_source_rejections=%llu "
            "audit_state_rejections=%llu "
            "audit_lifecycle_quota_rejections=%llu "
            "audit_capacity_failures=%llu "
            "audit_corroboration_threshold=%zu "
            "audit_pending_facts=%zu "
            "audit_pending_associations=%zu "
            "lifecycle_quarantined_records=%zu "
            "lifecycle_quarantined_bytes=%zu "
            "lifecycle_reporter_queues=%zu "
            "lifecycle_signer_entries=%zu "
            "lifecycle_deduplication_entries=%zu "
            "lifecycle_sources=%zu "
            "lifecycle_duplicate_observations=%llu "
            "lifecycle_applied_notices=%llu "
            "lifecycle_capacity_failures=%llu "
            "lifecycle_healthy=%d lifecycle_stopped=%d "
            "ledger_accepted=%zu ledger_rejected=%zu "
            "ledger_high_watermark=%llu",
            message_kind,
            ingress_status_name(status),
            static_cast<unsigned>(status),
            static_cast<unsigned>(source),
            static_cast<unsigned long long>(
                audit.readiness_wire_rejections),
            static_cast<unsigned long long>(
                audit.lifecycle_wire_rejections),
            static_cast<unsigned long long>(
                audit.evidence_wire_rejections),
            static_cast<unsigned long long>(
                audit.nonmember_rejections),
            static_cast<unsigned long long>(
                audit.spoofed_source_rejections),
            static_cast<unsigned long long>(audit.state_rejections),
            static_cast<unsigned long long>(
                audit.lifecycle_quota_rejections),
            static_cast<unsigned long long>(audit.capacity_failures),
            audit.lifecycle_corroboration_threshold,
            audit.pending_lifecycle_facts,
            audit.pending_lifecycle_associations,
            lifecycle.quarantined_records,
            lifecycle.quarantined_bytes,
            lifecycle.reporter_queues,
            lifecycle.signer_entries,
            lifecycle.deduplication_entries,
            lifecycle.lifecycle_sources,
            static_cast<unsigned long long>(
                lifecycle.duplicate_observations),
            static_cast<unsigned long long>(
                lifecycle.applied_lifecycle_notices),
            static_cast<unsigned long long>(
                lifecycle.capacity_failures),
            lifecycle.healthy ? 1 : 0,
            lifecycle.stopped ? 1 : 0,
            ledger.accepted().size(),
            ledger.rejected().size(),
            static_cast<unsigned long long>(ledger.high_watermark()));
    }

    template <typename Message, typename Ingest>
    void ingest(
        const char *message_kind,
        Message &&message,
        const ManagerNetwork::conn_t &connection,
        Ingest &&operation)
    {
        const auto source = authenticated_source(connection);
        if (!source.has_value())
            return;
        const auto result = operation(
            AuthenticatedReporter{*source}, message);
        if (result.status ==
                AdaptiveV2ManagerIngressStatus::evidence_unhealthy ||
            result.status == AdaptiveV2ManagerIngressStatus::stopped)
        {
            log_ingress_failure(
                message_kind, result.status, *source);
            fail("manager_ingress_unhealthy");
            return;
        }
        evaluate();
    }

    void register_handlers()
    {
        network_.reg_conn_handler(
            [this](const salticidae::ConnPool::conn_t &connection,
                   bool connected) {
                if (!connected)
                    return true;
                const auto *certificate = connection->get_peer_cert();
                if (certificate == nullptr)
                    return false;
                const PeerId peer(*certificate);
                return peer_to_replica_.count(peer) != 0;
            });
        network_.reg_handler(
            [this](MsgAdaptiveV2ReadinessNotice &&message,
                   const ManagerNetwork::conn_t &connection) {
                ingest("readiness",
                    std::move(message), connection,
                    [this](const AuthenticatedReporter &source,
                           const MsgAdaptiveV2ReadinessNotice &value) {
                        return ingress_.ingest_readiness(source, value);
                    });
            });
        network_.reg_handler(
            [this](MsgProposalLifecycleNotice &&message,
                   const ManagerNetwork::conn_t &connection) {
                ingest("lifecycle",
                    std::move(message), connection,
                    [this](const AuthenticatedReporter &source,
                           const MsgProposalLifecycleNotice &value) {
                        return ingress_.ingest_lifecycle(source, value);
                    });
            });
        network_.reg_handler(
            [this](MsgEvidenceReport &&message,
                   const ManagerNetwork::conn_t &connection) {
                ingest("evidence",
                    std::move(message), connection,
                    [this](const AuthenticatedReporter &source,
                           const MsgEvidenceReport &value) {
                        return ingress_.ingest_evidence(source, value);
                    });
            });
        network_.reg_error_handler(
            [this](const std::exception_ptr error,
                   bool fatal,
                   std::int32_t async_id) {
                try
                {
                    std::rethrow_exception(error);
                }
                catch (const std::exception &exception)
                {
                    HOTSTUFF_LOG_WARN(
                        "KAURI_ADAPTIVE_MANAGER network_error fatal=%d async_id=%d reason=%s",
                        fatal ? 1 : 0,
                        async_id,
                        exception.what());
                }
                if (fatal)
                    fail("network_fatal");
            });
    }

    EventContext &event_context_;
    ManagerOptions options_;
    ManagerNetwork network_;
    std::unordered_map<PeerId, ReplicaID> peer_to_replica_;
    AdaptiveV2ManagerIngress ingress_;
    AdaptiveV2ManagerController controller_;
    hotstuff::StructuredEventSink &structured_event_sink_;
    std::size_t emitted_score_trajectory_{0};
    bool network_stop_required_{false};
    bool network_stopped_{false};
    bool ingress_stopped_{false};
    bool successor_distributed_{false};
    bool failed_{false};
};

} // namespace

int main(int argc, char **argv)
{
    std::signal(SIGPIPE, SIG_IGN);
    try
    {
        auto options = parse_options(argc, argv);
        const auto structured_event_config =
            manager_structured_event_config(options);
        hotstuff::MonotonicRawStructuredEventClock
            structured_event_clock;
        hotstuff::ExclusiveFileStructuredEventOutput
            structured_event_output(options.structured_event_output);
        hotstuff::StructuredEventSink structured_event_sink(
            structured_event_config,
            structured_event_clock,
            structured_event_output);
        if (!structured_event_sink.health().healthy)
            throw std::runtime_error(
                "structured-event sink configuration is unhealthy");
        const auto net_config = network_config(options);
        EventContext event_context;
        auto manager = std::make_unique<AdaptationManager>(
            event_context,
            std::move(options),
            net_config,
            structured_event_sink);
        int run_status = 1;
        std::exception_ptr run_failure;
        try
        {
            run_status = manager->run();
        }
        catch (...)
        {
            run_failure = std::current_exception();
        }
        manager.reset();
        structured_event_sink.shutdown();
        const auto health = structured_event_sink.health();
        if (!health.healthy)
            return 1;
        if (run_failure != nullptr)
            std::rethrow_exception(run_failure);
        return run_status;
    }
    catch (const std::exception &error)
    {
        std::fprintf(
            stderr, "adaptation-manager: %s\n", error.what());
        return 2;
    }
}
