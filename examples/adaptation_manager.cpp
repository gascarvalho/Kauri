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
#include "hotstuff/adaptive_v2_manager_convergence.h"
#include "hotstuff/adaptive_v2_convergence_ack_wire.h"
#include "hotstuff/structured_event.h"
#include "hotstuff/util.h"

namespace
{

using hotstuff::AdaptiveV2ManagerController;
using hotstuff::AdaptiveV2ManagerControllerConfig;
using hotstuff::AdaptiveV2ManagerControllerStatus;
using hotstuff::AdaptiveV2ManagerConvergence;
using hotstuff::AdaptiveV2ManagerConvergenceConfig;
using hotstuff::AdaptiveV2ManagerConvergenceDisposition;
using hotstuff::AdaptiveV2ManagerConvergenceStatus;
using hotstuff::AdaptiveV2ConvergenceAckDisposition;
using hotstuff::AdaptiveV2ConvergenceObservationAck;
using hotstuff::AdaptiveV2ConvergenceObservationKind;
using hotstuff::AdaptiveV2ManagerIngress;
using hotstuff::AdaptiveV2ManagerIngressLimits;
using hotstuff::AdaptiveV2ManagerIngressStatus;
using hotstuff::AuthenticatedReporter;
using hotstuff::DataStream;
using hotstuff::EpochChangeBundleLimits;
using hotstuff::EpochDefinitionInput;
using hotstuff::EpochTreeDefinition;
using hotstuff::EpochWireLimits;
using hotstuff::MsgAdaptiveV2EpochChangeBundle;
using hotstuff::MsgAdaptiveV2EpochChangeCommittedObservation;
using hotstuff::MsgAdaptiveV2EpochActivatedObservation;
using hotstuff::MsgAdaptiveV2ConvergenceObservationAck;
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
constexpr std::size_t kMaximumExactProposals = 8192;
constexpr std::size_t kMaximumEvidenceRecords = 131072;
constexpr std::size_t kMaximumQuarantinedRecords = 1024;
constexpr std::size_t kMaximumQuarantinedBytes = 256 * 1024;
constexpr std::size_t kMaximumQuarantinedSignerEntries = 8192;
constexpr std::size_t kMaximumQuarantinedPerReporter = 128;
constexpr std::uint64_t kConvergenceRetryIntervalTicks = 1;
constexpr std::uint32_t kConvergenceMaximumAttempts = 5;
constexpr std::uint64_t kConvergenceTicksPerSecond = 10;
constexpr std::uint64_t kConvergenceDefaultDeadlineTicks = 120;
constexpr double kConvergenceTimerSeconds = 0.1;
// Cover the replica outbox's one-second capped ACK retry backoff and leave
// one convergence timer interval for scheduling and transport dispatch.
constexpr double kConvergenceAckDrainSeconds = 1.1;
constexpr opcode_t kCommittedObservationOpcode =
    MsgAdaptiveV2EpochChangeCommittedObservation::opcode;
constexpr opcode_t kActivatedObservationOpcode =
    MsgAdaptiveV2EpochActivatedObservation::opcode;

static_assert(kMinimumScoreDrop == 6);

struct ReplicaEndpoint
{
    ReplicaID replica_id{0};
    NetAddr address;
    PeerId peer_id;
};

struct ExperimentBundleAttempt
{
    ReplicaID recipient{0};
    std::uint32_t attempt{0};
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
    std::uint64_t convergence_deadline_ticks{
        kConvergenceDefaultDeadlineTicks};
    std::string bundle_output;
    std::string structured_event_run_id;
    std::string structured_event_source_instance;
    std::string structured_event_output;
    std::optional<ExperimentBundleAttempt>
        experiment_drop_bundle_attempt;
    std::optional<std::uint32_t>
        experiment_drop_activation_ack;
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

ExperimentBundleAttempt parse_experiment_bundle_attempt(
    const std::string &text)
{
    const auto separator = text.find(':');
    if (separator == std::string::npos || separator == 0 ||
        separator + 1 >= text.size() ||
        text.find(':', separator + 1) != std::string::npos)
    {
        throw std::invalid_argument(
            "experiment bundle attempt must use replica:attempt");
    }
    const auto recipient = parse_unsigned<std::uint32_t>(
        text.substr(0, separator),
        "experiment bundle recipient", false);
    const auto attempt = parse_unsigned<std::uint32_t>(
        text.substr(separator + 1),
        "experiment bundle attempt", true);
    if (recipient >= kSmokeReplicaCount ||
        attempt > kConvergenceMaximumAttempts)
    {
        throw std::invalid_argument(
            "experiment bundle attempt is outside convergence bounds");
    }
    return {static_cast<ReplicaID>(recipient), attempt};
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
    limits.proposal_index = {kMaximumExactProposals, 16};
    limits.evidence_store = {
        kMaximumEvidenceRecords, kMaximumEvidenceRecords};
    limits.lifecycle = {
        kMaximumQuarantinedRecords,
        kMaximumQuarantinedBytes,
        kSmokeReplicaCount,
        kMaximumQuarantinedSignerEntries,
        kMaximumQuarantinedRecords,
        kSmokeReplicaCount,
        kMaximumQuarantinedPerReporter};
    limits.lifecycle_accounting = {
        kMaximumQuarantinedRecords,
        kMaximumQuarantinedBytes,
        kMaximumQuarantinedSignerEntries};
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
    config.reputation_limits.maximum_audit_updates =
        kMaximumEvidenceRecords;
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

const char *convergence_disposition_name(
    AdaptiveV2ManagerConvergenceDisposition disposition) noexcept
{
    switch (disposition)
    {
    case AdaptiveV2ManagerConvergenceDisposition::accepted:
        return "accepted";
    case AdaptiveV2ManagerConvergenceDisposition::duplicate:
        return "duplicate";
    case AdaptiveV2ManagerConvergenceDisposition::advisory_enqueue_recorded:
        return "advisory_enqueue_recorded";
    case AdaptiveV2ManagerConvergenceDisposition::conflicting_enqueue_result:
        return "conflicting_enqueue_result";
    case AdaptiveV2ManagerConvergenceDisposition::rejected_nonmember:
        return "rejected_nonmember";
    case AdaptiveV2ManagerConvergenceDisposition::rejected_spoofed_source:
        return "rejected_spoofed_source";
    case AdaptiveV2ManagerConvergenceDisposition::rejected_stale:
        return "rejected_stale";
    case AdaptiveV2ManagerConvergenceDisposition::rejected_wrong_identity:
        return "rejected_wrong_identity";
    case AdaptiveV2ManagerConvergenceDisposition::conflicting_observation:
        return "conflicting_observation";
    case AdaptiveV2ManagerConvergenceDisposition::terminal:
        return "terminal";
    }
    return "terminal";
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
    auto opt_convergence_deadline_seconds =
        Config::OptValStr::create("12");
    auto opt_bundle_output = Config::OptValStr::create();
    auto opt_structured_event_run_id = Config::OptValStr::create();
    auto opt_structured_event_source_instance =
        Config::OptValStr::create();
    auto opt_structured_event_output = Config::OptValStr::create();
    auto opt_experiment_drop_bundle_attempt =
        Config::OptValStr::create();
    auto opt_experiment_drop_activation_ack =
        Config::OptValStr::create();

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
    config.add_opt(
        "convergence-deadline-seconds",
        opt_convergence_deadline_seconds,
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
    config.add_opt(
        "experiment-drop-bundle-attempt",
        opt_experiment_drop_bundle_attempt,
        Config::SET_VAL);
    config.add_opt(
        "experiment-drop-activation-ack",
        opt_experiment_drop_activation_ack,
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
    const auto convergence_deadline_seconds =
        parse_unsigned<std::uint64_t>(
            opt_convergence_deadline_seconds->get(),
            "convergence deadline seconds", true);
    if (convergence_deadline_seconds >
        std::numeric_limits<std::uint64_t>::max() /
            kConvergenceTicksPerSecond)
    {
        throw std::invalid_argument(
            "convergence deadline seconds are out of range");
    }
    options.convergence_deadline_ticks =
        convergence_deadline_seconds * kConvergenceTicksPerSecond;
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
    if (!opt_experiment_drop_bundle_attempt->get().empty())
    {
        options.experiment_drop_bundle_attempt =
            parse_experiment_bundle_attempt(
                opt_experiment_drop_bundle_attempt->get());
    }
    if (!opt_experiment_drop_activation_ack->get().empty())
    {
        options.experiment_drop_activation_ack =
            parse_unsigned<std::uint32_t>(
                opt_experiment_drop_activation_ack->get(),
                "experiment activation ACK ordinal", true);
    }

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
        convergence_timer = salticidae::TimerEvent(
            event_context_,
            [this](salticidae::TimerEvent &) {
                if (convergence_tick_ ==
                    std::numeric_limits<std::uint64_t>::max())
                {
                    fail("convergence_tick_exhausted");
                    return;
                }
                ++convergence_tick_;
                drive_convergence();
            });
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
            return failed_ || !convergence_succeeded_ ? 1 : 0;
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

    void emit_convergence_event(
        hotstuff::AdaptiveV2ConvergenceTransition transition,
        std::optional<ReplicaID> replica_id = std::nullopt,
        std::uint32_t delivery_attempt = 0,
        std::optional<hotstuff::AdaptiveV2EpochChangeIdentity> identity =
            std::nullopt,
        const char *disposition = "",
        const char *failure_reason = "",
        std::optional<hotstuff::uint256_t>
            canonical_payload_digest = std::nullopt) noexcept
    {
        if (!structured_event_sink_.health().healthy)
            return;

        hotstuff::AdaptiveV2ConvergenceStructuredEvent event;
        event.transition = transition;
        event.replica_id = replica_id;
        event.delivery_attempt = delivery_attempt;
        event.disposition = disposition;
        event.identity = std::move(identity);
        if (convergence_ != nullptr)
        {
            event.accepted_commit_count =
                convergence_->accepted_commit_count();
            event.accepted_activation_count =
                transition ==
                            hotstuff::AdaptiveV2ConvergenceTransition::
                                converged ||
                        transition ==
                            hotstuff::AdaptiveV2ConvergenceTransition::ready
                    ? convergence_->winning_activation_count()
                    : convergence_->accepted_activation_count();
        }
        event.required_activation_count = kSmokeQuorum;
        event.canonical_payload_digest =
            std::move(canonical_payload_digest);
        event.failure_reason = failure_reason;
        structured_event_sink_.emit_audit(
            hotstuff::AuditStructuredEventPayload{std::move(event)});
        if (!structured_event_sink_.health().healthy)
        {
            failed_ = true;
            event_context_.stop();
        }
    }

    void fail(const char *reason) noexcept
    {
        if (convergence_ != nullptr && !convergence_failure_emitted_)
        {
            convergence_failure_emitted_ = true;
            emit_convergence_event(
                hotstuff::AdaptiveV2ConvergenceTransition::failure,
                std::nullopt,
                0,
                std::nullopt,
                "",
                reason);
        }
        failed_ = true;
        HOTSTUFF_LOG_WARN(
            "KAURI_ADAPTIVE_MANAGER fatal reason=%s", reason);
        event_context_.stop();
    }

    void stop_runtime() noexcept
    {
        event_context_.stop();
        convergence_timer.del();
        convergence_ack_drain_timer.del();
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
        if (failed_ || convergence_ != nullptr)
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
            AdaptiveV2ManagerConvergenceConfig convergence_config;
            convergence_config.membership = smoke_membership();
            convergence_config.retry_interval_ticks =
                kConvergenceRetryIntervalTicks;
            convergence_config.maximum_attempts_per_recipient =
                kConvergenceMaximumAttempts;
            if (options_.convergence_deadline_ticks >
                std::numeric_limits<std::uint64_t>::max() -
                    convergence_tick_)
            {
                throw std::overflow_error(
                    "convergence deadline tick overflow");
            }
            convergence_config.convergence_deadline_tick =
                convergence_tick_ + options_.convergence_deadline_ticks;
            convergence_ =
                std::make_unique<AdaptiveV2ManagerConvergence>(
                    *bundle,
                    std::move(convergence_config),
                    convergence_tick_);
            const auto &payload = bundle->command().payload;
            HOTSTUFF_LOG_INFO(
                "KAURI_ADAPTIVE_MANAGER convergence_started "
                "epoch=%u digest=%s bytes=%zu required=%u",
                payload.successor_epoch_number,
                payload.successor_epoch_digest.to_hex().c_str(),
                bundle->canonical_bytes().size(),
                kSmokeQuorum);
            drive_convergence();
        }
        catch (const std::exception &error)
        {
            HOTSTUFF_LOG_WARN(
                "KAURI_ADAPTIVE_MANAGER distribution_error=%s",
                error.what());
            fail("successor_distribution_failed");
        }
    }

    void handle_convergence_status() noexcept
    {
        if (convergence_ == nullptr || failed_)
            return;
        if (convergence_succeeded_)
            return;

        const auto status = convergence_->status();
        if (status ==
            AdaptiveV2ManagerConvergenceStatus::awaiting_activations)
        {
            return;
        }
        if (status ==
            AdaptiveV2ManagerConvergenceStatus::ready_for_optimization)
        {
            const auto *winning_identity =
                convergence_->winning_identity();
            if (winning_identity == nullptr)
            {
                fail("convergence_winning_identity_missing");
                return;
            }
            if ((options_.experiment_drop_bundle_attempt.has_value() &&
                 !experiment_bundle_drop_consumed_) ||
                (options_.experiment_drop_activation_ack.has_value() &&
                 !experiment_activation_ack_drop_consumed_))
            {
                fail("experiment_convergence_loss_not_observed");
                return;
            }
            emit_convergence_event(
                hotstuff::AdaptiveV2ConvergenceTransition::converged,
                std::nullopt,
                0,
                *winning_identity);
            if (failed_)
                return;
            if (!convergence_->consume_ready_for_optimization())
            {
                fail("convergence_ready_consumption_failed");
                return;
            }
            convergence_succeeded_ = true;
            emit_convergence_event(
                hotstuff::AdaptiveV2ConvergenceTransition::ready,
                std::nullopt,
                0,
                *winning_identity);
            begin_convergence_ack_drain();
            return;
        }
        if (status ==
            AdaptiveV2ManagerConvergenceStatus::retry_exhausted)
        {
            fail("convergence_retry_exhausted");
            return;
        }
        if (status ==
            AdaptiveV2ManagerConvergenceStatus::conflicting_observation)
        {
            fail("convergence_conflicting_observation");
        }
    }

    void begin_convergence_ack_drain() noexcept
    {
        convergence_timer.del();
        convergence_ack_drain_timer.del();
        try
        {
            convergence_ack_drain_timer = salticidae::TimerEvent(
                event_context_,
                [this](salticidae::TimerEvent &) {
                    event_context_.stop();
                });
            convergence_ack_drain_timer.add(
                kConvergenceAckDrainSeconds);
        }
        catch (...)
        {
            fail("convergence_ack_drain_timer_failed");
        }
    }

    void drive_convergence() noexcept
    {
        if (convergence_ == nullptr || failed_ ||
            convergence_succeeded_)
        {
            return;
        }

        try
        {
            const auto requests =
                convergence_->due_deliveries(convergence_tick_);
            for (const auto &request : requests)
            {
                if (request.canonical_bundle_bytes == nullptr ||
                    request.recipient >= options_.replicas.size())
                {
                    throw std::logic_error(
                        "convergence produced an invalid delivery request");
                }
                const auto canonical_payload_digest =
                    DataStream(*request.canonical_bundle_bytes).get_hash();
                const bool injected_drop =
                    options_.experiment_drop_bundle_attempt.has_value() &&
                    !experiment_bundle_drop_consumed_ &&
                    options_.experiment_drop_bundle_attempt->recipient ==
                        request.recipient &&
                    options_.experiment_drop_bundle_attempt->attempt ==
                        request.attempt;
                if (injected_drop)
                    experiment_bundle_drop_consumed_ = true;

                bool enqueued = false;
                if (!injected_drop)
                {
                    const auto &replica =
                        options_.replicas[request.recipient];
                    try
                    {
                        const auto connection =
                            network_.get_peer_conn(replica.peer_id);
                        const auto source =
                            authenticated_source(connection);
                        if (connection != nullptr &&
                            !connection->is_terminated() &&
                            source.has_value() &&
                            *source == request.recipient)
                        {
                            enqueued = network_.send_msg(
                                MsgAdaptiveV2EpochChangeBundle(
                                    DataStream(
                                        *request.canonical_bundle_bytes)),
                                connection);
                        }
                    }
                    catch (const std::exception &error)
                    {
                        HOTSTUFF_LOG_WARN(
                            "KAURI_ADAPTIVE_MANAGER bundle_enqueue_failed replica=%u reason=%s",
                            request.recipient,
                            error.what());
                    }
                    catch (...)
                    {
                        HOTSTUFF_LOG_WARN(
                            "KAURI_ADAPTIVE_MANAGER bundle_enqueue_failed "
                            "replica=%u reason=unknown",
                            request.recipient);
                    }
                }

                const auto recorded = convergence_->record_enqueue_result(
                    request.recipient,
                    request.attempt,
                    enqueued);
                emit_convergence_event(
                    hotstuff::AdaptiveV2ConvergenceTransition::delivery_attempt,
                    request.recipient,
                    request.attempt,
                    std::nullopt,
                    injected_drop
                        ? "injected_drop"
                        : enqueued ? "enqueued" : "enqueue_failed",
                    "",
                    canonical_payload_digest);
                if (recorded ==
                    AdaptiveV2ManagerConvergenceDisposition::
                        conflicting_enqueue_result)
                {
                    fail("convergence_enqueue_result_conflict");
                    return;
                }
            }

            handle_convergence_status();
            if (!failed_ && !convergence_succeeded_ &&
                convergence_->status() ==
                    AdaptiveV2ManagerConvergenceStatus::
                        awaiting_activations)
            {
                convergence_timer.add(kConvergenceTimerSeconds);
            }
        }
        catch (...)
        {
            fail("convergence_delivery_failed");
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
            "audit_evidence_sequence_rejections=%llu "
            "audit_lifecycle_fence_mismatch_rejections=%llu "
            "audit_lifecycle_quota_rejections=%llu "
            "audit_capacity_failures=%llu "
            "audit_corroboration_threshold=%zu "
            "audit_pending_facts=%zu "
            "audit_pending_associations=%zu "
            "audit_reporter_causal_retained_proposals=%zu "
            "audit_reporter_causal_open_reporters=%zu "
            "lifecycle_quarantined_records=%zu "
            "lifecycle_quarantined_bytes=%zu "
            "lifecycle_reporter_queues=%zu "
            "lifecycle_signer_entries=%zu "
            "lifecycle_deduplication_entries=%zu "
            "lifecycle_sources=%zu "
            "lifecycle_duplicate_observations=%llu "
            "lifecycle_applied_notices=%llu "
            "lifecycle_quarantine_quota_rejections=%llu "
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
                audit.evidence_sequence_rejections),
            static_cast<unsigned long long>(
                audit.lifecycle_fence_mismatch_rejections),
            static_cast<unsigned long long>(
                audit.lifecycle_quota_rejections),
            static_cast<unsigned long long>(audit.capacity_failures),
            audit.lifecycle_corroboration_threshold,
            audit.pending_lifecycle_facts,
            audit.pending_lifecycle_associations,
            audit.reporter_causal_retained_proposals,
            audit.reporter_causal_open_reporters,
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
                lifecycle.quarantine_quota_rejections),
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
        network_.reg_handler(
            [this](
                MsgAdaptiveV2EpochChangeCommittedObservation &&message,
                const ManagerNetwork::conn_t &connection) {
                if (convergence_ == nullptr || failed_)
                    return;
                const auto source = authenticated_source(connection);
                if (!source.has_value())
                {
                    emit_convergence_event(
                        hotstuff::AdaptiveV2ConvergenceTransition::commit_observed,
                        std::nullopt,
                        0,
                        std::nullopt,
                        "rejected_unauthenticated_source");
                    return;
                }
                if (message.serialized.size() >
                    convergence_wire_limits_.maximum_payload_bytes)
                {
                    emit_convergence_event(
                        hotstuff::AdaptiveV2ConvergenceTransition::commit_observed,
                        *source,
                        0,
                        std::nullopt,
                        "rejected_wire_decode");
                    return;
                }
                const auto canonical_observation =
                    static_cast<bytearray_t>(message.serialized);
                const auto decoded =
                    hotstuff::
                        decode_adaptive_v2_epoch_change_committed_observation(
                            canonical_observation,
                            convergence_wire_limits_);
                if (!decoded)
                {
                    emit_convergence_event(
                        hotstuff::AdaptiveV2ConvergenceTransition::commit_observed,
                        *source,
                        0,
                        std::nullopt,
                        "rejected_wire_decode");
                    return;
                }
                const auto disposition = convergence_->observe_commit(
                    *source, *decoded.observation);

                bool acknowledgement_sent = false;
                std::optional<hotstuff::uint256_t>
                    canonical_payload_digest;
                try
                {
                    AdaptiveV2ConvergenceObservationAck acknowledgement;
                    acknowledgement.target_replica_id = *source;
                    acknowledgement.observation_kind =
                        AdaptiveV2ConvergenceObservationKind::commit;
                    acknowledgement.identity =
                        decoded.observation->identity;
                    acknowledgement.observation_digest =
                        hotstuff::adaptive_v2_convergence_observation_digest(
                            kCommittedObservationOpcode,
                            canonical_observation);
                    canonical_payload_digest =
                        acknowledgement.observation_digest;
                    acknowledgement.disposition =
                        disposition ==
                                    AdaptiveV2ManagerConvergenceDisposition::
                                        accepted ||
                                disposition ==
                                    AdaptiveV2ManagerConvergenceDisposition::duplicate
                            ? AdaptiveV2ConvergenceAckDisposition::positive
                            : AdaptiveV2ConvergenceAckDisposition::permanent_rejection;
                    emit_convergence_event(
                        hotstuff::AdaptiveV2ConvergenceTransition::commit_observed,
                        *source,
                        0,
                        decoded.observation->identity,
                        convergence_disposition_name(disposition),
                        "",
                        canonical_payload_digest);
                    acknowledgement_sent = network_.send_msg(
                        MsgAdaptiveV2ConvergenceObservationAck(
                            acknowledgement,
                            convergence_wire_limits_),
                        connection);
                }
                catch (...)
                {
                    acknowledgement_sent = false;
                }
                if (!acknowledgement_sent)
                {
                    HOTSTUFF_LOG_WARN(
                        "KAURI_ADAPTIVE_MANAGER convergence_ack_send_failed "
                        "kind=commit replica=%u",
                        *source);
                    emit_convergence_event(
                        hotstuff::AdaptiveV2ConvergenceTransition::commit_observed,
                        *source,
                        0,
                        decoded.observation->identity,
                        "ack_send_failed",
                        "",
                        canonical_payload_digest);
                }
                else
                {
                    emit_convergence_event(
                        hotstuff::AdaptiveV2ConvergenceTransition::commit_observed,
                        *source,
                        0,
                        decoded.observation->identity,
                        "ack_sent",
                        "",
                        canonical_payload_digest);
                }
                handle_convergence_status();
            });
        network_.reg_handler(
            [this](MsgAdaptiveV2EpochActivatedObservation &&message,
                   const ManagerNetwork::conn_t &connection) {
                if (convergence_ == nullptr || failed_)
                    return;
                const auto source = authenticated_source(connection);
                if (!source.has_value())
                {
                    emit_convergence_event(
                        hotstuff::AdaptiveV2ConvergenceTransition::activation_observed,
                        std::nullopt,
                        0,
                        std::nullopt,
                        "rejected_unauthenticated_source");
                    return;
                }
                if (message.serialized.size() >
                    convergence_wire_limits_.maximum_payload_bytes)
                {
                    emit_convergence_event(
                        hotstuff::AdaptiveV2ConvergenceTransition::activation_observed,
                        *source,
                        0,
                        std::nullopt,
                        "rejected_wire_decode");
                    return;
                }
                const auto canonical_observation =
                    static_cast<bytearray_t>(message.serialized);
                const auto decoded =
                    hotstuff::
                        decode_adaptive_v2_epoch_activated_observation(
                            canonical_observation,
                            convergence_wire_limits_);
                if (!decoded)
                {
                    emit_convergence_event(
                        hotstuff::AdaptiveV2ConvergenceTransition::activation_observed,
                        *source,
                        0,
                        std::nullopt,
                        "rejected_wire_decode");
                    return;
                }
                const auto disposition = convergence_->observe_activation(
                    *source, *decoded.observation);

                bool acknowledgement_sent = false;
                bool acknowledgement_injected_drop = false;
                std::optional<hotstuff::uint256_t>
                    canonical_payload_digest;
                try
                {
                    AdaptiveV2ConvergenceObservationAck acknowledgement;
                    acknowledgement.target_replica_id = *source;
                    acknowledgement.observation_kind =
                        AdaptiveV2ConvergenceObservationKind::activation;
                    acknowledgement.identity =
                        decoded.observation->identity;
                    acknowledgement.observation_digest =
                        hotstuff::adaptive_v2_convergence_observation_digest(
                            kActivatedObservationOpcode,
                            canonical_observation);
                    canonical_payload_digest =
                        acknowledgement.observation_digest;
                    acknowledgement.disposition =
                        disposition ==
                                    AdaptiveV2ManagerConvergenceDisposition::
                                        accepted ||
                                disposition ==
                                    AdaptiveV2ManagerConvergenceDisposition::duplicate
                            ? AdaptiveV2ConvergenceAckDisposition::positive
                            : AdaptiveV2ConvergenceAckDisposition::permanent_rejection;
                    emit_convergence_event(
                        hotstuff::AdaptiveV2ConvergenceTransition::activation_observed,
                        *source,
                        0,
                        decoded.observation->identity,
                        convergence_disposition_name(disposition),
                        "",
                        canonical_payload_digest);
                    if (acknowledgement.disposition ==
                        AdaptiveV2ConvergenceAckDisposition::positive)
                    {
                        ++positive_activation_ack_ordinal_;
                        acknowledgement_injected_drop =
                            options_.experiment_drop_activation_ack.has_value() &&
                            !experiment_activation_ack_drop_consumed_ &&
                            *options_.experiment_drop_activation_ack ==
                                positive_activation_ack_ordinal_;
                        if (acknowledgement_injected_drop)
                        {
                            experiment_activation_ack_drop_consumed_ =
                                true;
                        }
                    }
                    if (!acknowledgement_injected_drop)
                    {
                        acknowledgement_sent = network_.send_msg(
                            MsgAdaptiveV2ConvergenceObservationAck(
                                acknowledgement,
                                convergence_wire_limits_),
                            connection);
                    }
                }
                catch (...)
                {
                    acknowledgement_sent = false;
                }
                if (acknowledgement_injected_drop)
                {
                    emit_convergence_event(
                        hotstuff::AdaptiveV2ConvergenceTransition::activation_observed,
                        *source,
                        0,
                        decoded.observation->identity,
                        "ack_injected_drop",
                        "",
                        canonical_payload_digest);
                }
                else if (!acknowledgement_sent)
                {
                    HOTSTUFF_LOG_WARN(
                        "KAURI_ADAPTIVE_MANAGER convergence_ack_send_failed "
                        "kind=activation replica=%u",
                        *source);
                    emit_convergence_event(
                        hotstuff::AdaptiveV2ConvergenceTransition::activation_observed,
                        *source,
                        0,
                        decoded.observation->identity,
                        "ack_send_failed",
                        "",
                        canonical_payload_digest);
                }
                else
                {
                    emit_convergence_event(
                        hotstuff::AdaptiveV2ConvergenceTransition::activation_observed,
                        *source,
                        0,
                        decoded.observation->identity,
                        "ack_sent",
                        "",
                        canonical_payload_digest);
                }
                handle_convergence_status();
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
    std::unique_ptr<AdaptiveV2ManagerConvergence> convergence_;
    hotstuff::AdaptiveV2ConvergenceWireLimits
        convergence_wire_limits_;
    salticidae::TimerEvent convergence_timer;
    salticidae::TimerEvent convergence_ack_drain_timer;
    std::uint64_t convergence_tick_{0};
    std::size_t emitted_score_trajectory_{0};
    std::uint32_t positive_activation_ack_ordinal_{0};
    bool network_stop_required_{false};
    bool network_stopped_{false};
    bool ingress_stopped_{false};
    bool convergence_succeeded_{false};
    bool convergence_failure_emitted_{false};
    bool experiment_bundle_drop_consumed_{false};
    bool experiment_activation_ack_drop_consumed_{false};
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
