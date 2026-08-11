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
#include <chrono>
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

#include "hotstuff/adaptation_manager.h"
#include "hotstuff/adaptation_manager_profile.h"
#include "hotstuff/adaptive_v2_convergence_ack_wire.h"
#include "hotstuff/adaptive_v2_manager_session.h"
#include "hotstuff/structured_event.h"
#include "hotstuff/util.h"

namespace
{

using hotstuff::AdaptiveV2ManagerControllerConfig;
using hotstuff::AdaptiveV2ManagerControllerStatus;
using hotstuff::AdaptiveV2ManagerCycleOutcome;
using hotstuff::AdaptiveV2ManagerCycleTerminalReason;
using hotstuff::AdaptiveV2ManagerConvergenceDisposition;
using hotstuff::AdaptiveV2ManagerConvergenceStatus;
using hotstuff::AdaptiveV2ManagerRequestSequence;
using hotstuff::AdaptiveV2ManagerSession;
using hotstuff::AdaptiveV2ManagerSessionConfig;
using hotstuff::AdaptiveV2ConvergenceAckDisposition;
using hotstuff::AdaptiveV2ConvergenceObservationAck;
using hotstuff::AdaptiveV2ConvergenceObservationKind;
using hotstuff::AdaptiveV2ManagerIngressStatus;
using hotstuff::AdaptiveV2ManagerRuntimeShape;
using hotstuff::AdaptiveV2TransitionPolicy;
using hotstuff::AuthenticatedReporter;
using hotstuff::DataStream;
using hotstuff::EpochDefinitionInput;
using hotstuff::EpochTreeDefinition;
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
using hotstuff::TreePolicyKind;
using hotstuff::bytearray_t;
using hotstuff::opcode_t;
using salticidae::Config;
using salticidae::EventContext;
using salticidae::NetAddr;
using salticidae::PeerId;

using ManagerNetwork = salticidae::PeerNetwork<opcode_t>;

constexpr std::uint32_t kInitialTreeId = 0;
constexpr std::uint64_t kInitialActivationGeneration = 1;
constexpr std::uint64_t kSnapshotSeed = 0xA2F7;
constexpr std::uint32_t kTimeoutsPerReporter = 2;
constexpr std::uint64_t kConvergenceRetryIntervalTicks = 1;
constexpr std::uint32_t kConvergenceMaximumAttempts = 5;
constexpr std::uint64_t kConvergenceTicksPerSecond = 10;
constexpr std::uint64_t kConvergenceDefaultDeadlineTicks = 120;
constexpr double kConvergenceTimerSeconds = 0.1;
// Bound isolated-ingress latency while batching a burst at one fixed deadline.
constexpr double kEvaluationCoalescingSeconds = 0.05;
// Cover the replica outbox's one-second capped ACK retry backoff and leave
// one convergence timer interval for scheduling and transport dispatch.
constexpr double kConvergenceAckDrainSeconds = 1.1;
constexpr std::size_t kMaximumTransitionRequestBytes = 16 * 1024;
constexpr std::size_t kMaximumTransitionArtifactIdBytes = 128;
constexpr std::size_t kMaximumTransitionPathBytes = 4096;
constexpr std::uint32_t kMaximumPredecessorResidencyMs = 3'600'000;
constexpr opcode_t kCommittedObservationOpcode =
    MsgAdaptiveV2EpochChangeCommittedObservation::opcode;
constexpr opcode_t kActivatedObservationOpcode =
    MsgAdaptiveV2EpochActivatedObservation::opcode;

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

struct TransitionRequest
{
    AdaptiveV2TransitionPolicy policy;
    bool resolve_containment_roots_from_predecessor{false};
    std::string evidence_window_rule;
    std::string transition_artifact_id;
    std::string declared_bundle_path;
    std::string evidence_snapshot_path;
    std::uint32_t predecessor_epoch_number{0};
    std::uint32_t successor_epoch_number{0};
    std::uint32_t minimum_predecessor_residency_ms{0};
    std::uint32_t minimum_post_baseline_observation_ms{0};
    std::string bundle_output;
    std::string evidence_snapshot_output;
};

struct CycleAuditContext
{
    std::string transition_artifact_id;
    std::uint64_t activation_generation{0};
    std::uint64_t baseline_evidence_cutoff{0};
    std::uint64_t current_evidence_cutoff{0};
    bool shape_decision_emitted{false};
    bool evidence_snapshot_emitted{false};
    bool fault_containment_coverage_ready_emitted{false};
};

struct ManagerOptions
{
    NetAddr listen_address;
    bytearray_t tls_private_key_der;
    bytearray_t tls_certificate_der;
    PeerId local_peer_id;
    std::vector<ReplicaEndpoint> replicas;
    std::vector<ReplicaID> membership;
    AdaptiveV2ManagerRuntimeShape runtime_shape;
    std::uint32_t required_nonresponsive{0};
    hotstuff::EpochChangeIssuerId issuer_id{0};
    PrivKeySecp256k1 issuer_private_key;
    std::uint64_t activation_delay_blocks{5};
    std::uint64_t convergence_deadline_ticks{
        kConvergenceDefaultDeadlineTicks};
    hotstuff::AdaptationPolicy responsiveness_policy{
        hotstuff::kAdaptationSchemaVersion,
        "adaptive-v2-controller-responsiveness-v1",
        32,
        2,
        750'000,
        250'000,
        2,
        5'000};
    std::vector<std::uint32_t> shape_candidate_fanouts{2, 3, 5};
    std::uint64_t shape_deterministic_seed{kSnapshotSeed};
    bool shape_adaptation_enabled{false};
    std::uint64_t fault_containment_evidence_start_monotonic_ns{0};
    std::uint32_t fault_containment_required_tree_coverage{0};
    std::uint64_t cycle_1_selection_not_before_monotonic_ns{0};
    std::vector<TransitionRequest> transition_requests;
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

class TransitionJsonParser final
{
public:
    TransitionJsonParser(
        const std::string &text,
        std::uint32_t replica_count,
        std::uint32_t tree_count,
        bool default_apply_shape_selection)
        : text_(text),
          replica_count_(replica_count),
          tree_count_(tree_count),
          default_apply_shape_selection_(
              default_apply_shape_selection)
    {
        if (text_.empty() ||
            text_.size() > kMaximumTransitionRequestBytes)
        {
            throw std::invalid_argument(
                "transition request JSON size is invalid");
        }
    }

    TransitionRequest parse()
    {
        TransitionRequest request;
        std::optional<std::string> policy_intent;
        std::optional<std::string> evidence_window_rule;
        std::optional<std::string> transition_artifact_id;
        std::optional<std::string> bundle_path;
        std::optional<std::string> evidence_snapshot_path;
        std::optional<std::uint32_t> predecessor_epoch_number;
        std::optional<std::uint32_t> successor_epoch_number;
        std::optional<std::uint32_t> minimum_predecessor_residency_ms;
        std::optional<std::uint32_t>
            minimum_post_baseline_observation_ms;
        std::optional<bool> apply_shape_selection;
        std::optional<std::string> containment_baseline_root_source;
        std::optional<std::vector<hotstuff::BaselineRoot>> baseline_roots;

        expect('{');
        bool first = true;
        while (!consume('}'))
        {
            if (!first)
                expect(',');
            first = false;
            const auto key = parse_string();
            expect(':');
            if (key == "policy_intent")
                assign_once(policy_intent, parse_string(), key);
            else if (key == "evidence_window_rule")
                assign_once(evidence_window_rule, parse_string(), key);
            else if (key == "transition_artifact_id")
                assign_once(transition_artifact_id, parse_string(), key);
            else if (key == "bundle_path")
                assign_once(bundle_path, parse_string(), key);
            else if (key == "evidence_snapshot_path")
                assign_once(evidence_snapshot_path, parse_string(), key);
            else if (key == "predecessor_epoch_number")
            {
                assign_once(
                    predecessor_epoch_number, parse_u32(), key);
            }
            else if (key == "successor_epoch_number")
            {
                assign_once(successor_epoch_number, parse_u32(), key);
            }
            else if (key == "minimum_predecessor_residency_ms")
            {
                assign_once(
                    minimum_predecessor_residency_ms,
                    parse_u32(),
                    key);
            }
            else if (key == "minimum_post_baseline_observation_ms")
            {
                assign_once(
                    minimum_post_baseline_observation_ms,
                    parse_u32(),
                    key);
            }
            else if (key == "apply_shape_selection")
            {
                assign_once(
                    apply_shape_selection, parse_bool(), key);
            }
            else if (key == "containment_baseline_root_source")
            {
                assign_once(
                    containment_baseline_root_source,
                    parse_string(),
                    key);
            }
            else if (key == "policy_parameters")
            {
                if (baseline_roots.has_value())
                    duplicate(key);
                baseline_roots = parse_policy_parameters();
            }
            else
                unknown(key);
        }
        skip_whitespace();
        if (position_ != text_.size())
            fail("trailing bytes");

        if (!policy_intent || !evidence_window_rule ||
            !transition_artifact_id || !bundle_path ||
            !evidence_snapshot_path || !predecessor_epoch_number ||
            !successor_epoch_number ||
            !minimum_predecessor_residency_ms || !baseline_roots)
        {
            fail("missing required field");
        }
        if (*evidence_window_rule !=
            "fresh_exact_predecessor_after_common_commit")
        {
            fail("unsupported evidence_window_rule");
        }
        if (transition_artifact_id->empty() ||
            *transition_artifact_id == "." ||
            *transition_artifact_id == ".." ||
            transition_artifact_id->size() >
                kMaximumTransitionArtifactIdBytes ||
            !std::all_of(
                transition_artifact_id->begin(),
                transition_artifact_id->end(),
                [](unsigned char value) {
                    return std::isalnum(value) != 0 ||
                        value == '-' || value == '_' || value == '.';
                }))
        {
            fail("invalid transition_artifact_id");
        }
        if (*predecessor_epoch_number ==
                std::numeric_limits<std::uint32_t>::max() ||
            *successor_epoch_number !=
                *predecessor_epoch_number + 1)
        {
            fail("transition must bind an exact successor");
        }
        if (*minimum_predecessor_residency_ms >
            kMaximumPredecessorResidencyMs)
        {
            fail("minimum_predecessor_residency_ms exceeds bound");
        }
        if (minimum_post_baseline_observation_ms.value_or(0) >
            kMaximumPredecessorResidencyMs)
        {
            fail(
                "minimum_post_baseline_observation_ms exceeds bound");
        }

        const auto artifact_prefix =
            std::string{"transitions/"} +
            *transition_artifact_id + "/";
        if (*bundle_path != artifact_prefix + "successor.bundle" ||
            *evidence_snapshot_path !=
                artifact_prefix + "evidence-snapshot.json")
        {
            fail("transition artifact paths are not canonical");
        }

        if (*policy_intent == "fault_containment")
        {
            request.policy.intent = TreePolicyKind::fault_containment;
            if (containment_baseline_root_source.has_value())
            {
                if (*containment_baseline_root_source !=
                        "live_predecessor_roots" ||
                    !baseline_roots->empty())
                {
                    fail("unsupported containment baseline root source");
                }
                request.resolve_containment_roots_from_predecessor = true;
            }
            else
            {
                validate_containment_roots(*baseline_roots);
                request.policy.containment_baseline_roots =
                    std::move(*baseline_roots);
            }
        }
        else if (*policy_intent == "performance_optimization")
        {
            if (!baseline_roots->empty() ||
                containment_baseline_root_source.has_value())
            {
                fail("optimization policy parameters must be empty");
            }
            request.policy.intent =
                TreePolicyKind::performance_optimization;
        }
        else
            fail("unsupported policy_intent");

        request.policy.apply_shape_selection =
            apply_shape_selection.value_or(
                default_apply_shape_selection_);

        request.evidence_window_rule =
            std::move(*evidence_window_rule);
        request.transition_artifact_id =
            std::move(*transition_artifact_id);
        request.declared_bundle_path = std::move(*bundle_path);
        request.evidence_snapshot_path =
            std::move(*evidence_snapshot_path);
        request.predecessor_epoch_number =
            *predecessor_epoch_number;
        request.successor_epoch_number = *successor_epoch_number;
        request.minimum_predecessor_residency_ms =
            *minimum_predecessor_residency_ms;
        request.minimum_post_baseline_observation_ms =
            minimum_post_baseline_observation_ms.value_or(0);
        return request;
    }

private:
    template<typename Value>
    void assign_once(
        std::optional<Value> &destination,
        Value value,
        const std::string &key)
    {
        if (destination.has_value())
            duplicate(key);
        destination = std::move(value);
    }

    [[noreturn]] void fail(const std::string &reason) const
    {
        throw std::invalid_argument(
            "invalid transition request JSON: " + reason);
    }

    [[noreturn]] void duplicate(const std::string &key) const
    {
        fail("duplicate field " + key);
    }

    [[noreturn]] void unknown(const std::string &key) const
    {
        fail("unknown field " + key);
    }

    void skip_whitespace()
    {
        while (position_ < text_.size() &&
               std::isspace(
                   static_cast<unsigned char>(text_[position_])) != 0)
        {
            ++position_;
        }
    }

    bool consume(char expected)
    {
        skip_whitespace();
        if (position_ >= text_.size() ||
            text_[position_] != expected)
        {
            return false;
        }
        ++position_;
        return true;
    }

    void expect(char expected)
    {
        if (!consume(expected))
            fail(std::string{"expected "} + expected);
    }

    std::string parse_string()
    {
        skip_whitespace();
        if (position_ >= text_.size() || text_[position_] != '"')
            fail("expected string");
        ++position_;
        std::string result;
        while (position_ < text_.size())
        {
            const auto value = static_cast<unsigned char>(
                text_[position_++]);
            if (value == '"')
                return result;
            if (value < 0x20)
                fail("control byte in string");
            if (value != '\\')
            {
                result.push_back(static_cast<char>(value));
                if (result.size() > kMaximumTransitionPathBytes)
                    fail("string is too long");
                continue;
            }
            if (position_ >= text_.size())
                fail("truncated string escape");
            const auto escaped = text_[position_++];
            switch (escaped)
            {
                case '"':
                case '\\':
                case '/':
                    result.push_back(escaped);
                    break;
                case 'b': result.push_back('\b'); break;
                case 'f': result.push_back('\f'); break;
                case 'n': result.push_back('\n'); break;
                case 'r': result.push_back('\r'); break;
                case 't': result.push_back('\t'); break;
                default:
                    fail("unsupported string escape");
            }
            if (result.size() > kMaximumTransitionPathBytes)
                fail("string is too long");
        }
        fail("unterminated string");
    }

    std::uint32_t parse_u32()
    {
        skip_whitespace();
        const auto begin = position_;
        while (position_ < text_.size() &&
               std::isdigit(
                   static_cast<unsigned char>(text_[position_])) != 0)
        {
            ++position_;
        }
        if (begin == position_ ||
            (position_ - begin > 1 && text_[begin] == '0'))
        {
            fail("expected canonical unsigned integer");
        }
        return parse_unsigned<std::uint32_t>(
            text_.substr(begin, position_ - begin),
            "transition request integer", false);
    }

    bool parse_bool()
    {
        skip_whitespace();
        if (text_.compare(position_, 4, "true") == 0)
        {
            position_ += 4;
            return true;
        }
        if (text_.compare(position_, 5, "false") == 0)
        {
            position_ += 5;
            return false;
        }
        fail("expected boolean");
    }

    std::vector<hotstuff::BaselineRoot> parse_policy_parameters()
    {
        std::vector<hotstuff::BaselineRoot> roots;
        expect('{');
        if (consume('}'))
            return roots;
        const auto key = parse_string();
        if (key != "containment_baseline_roots")
            unknown(key);
        expect(':');
        roots = parse_baseline_roots();
        if (consume(','))
            fail("unknown policy parameter");
        expect('}');
        return roots;
    }

    std::vector<hotstuff::BaselineRoot> parse_baseline_roots()
    {
        std::vector<hotstuff::BaselineRoot> roots;
        expect('[');
        bool first = true;
        while (!consume(']'))
        {
            if (!first)
                expect(',');
            first = false;
            roots.push_back(parse_baseline_root());
            if (roots.size() > tree_count_)
                fail("too many containment baseline roots");
        }
        return roots;
    }

    hotstuff::BaselineRoot parse_baseline_root()
    {
        std::optional<std::uint32_t> tree_id;
        std::optional<std::uint32_t> replica_id;
        expect('{');
        bool first = true;
        while (!consume('}'))
        {
            if (!first)
                expect(',');
            first = false;
            const auto key = parse_string();
            expect(':');
            if (key == "tree_id")
                assign_once(tree_id, parse_u32(), key);
            else if (key == "replica_id")
                assign_once(replica_id, parse_u32(), key);
            else
                unknown(key);
        }
        if (!tree_id || !replica_id ||
            *replica_id >= replica_count_)
        {
            fail("invalid containment baseline root");
        }
        return {*tree_id, static_cast<ReplicaID>(*replica_id)};
    }

    void validate_containment_roots(
        const std::vector<hotstuff::BaselineRoot> &roots)
    {
        if (roots.size() != tree_count_)
            fail("containment requires one baseline root per tree");
        std::set<std::uint32_t> tree_ids;
        std::set<ReplicaID> replica_ids;
        for (const auto &root : roots)
        {
            if (root.tree_id >= tree_count_ ||
                !tree_ids.insert(root.tree_id).second ||
                !replica_ids.insert(root.replica_id).second)
            {
                fail("containment baseline roots are not unique");
            }
        }
        for (std::uint32_t tree_id = 0;
             tree_id < tree_count_;
             ++tree_id)
        {
            if (tree_ids.count(tree_id) == 0)
                fail("containment baseline roots omit a tree");
        }
    }

    const std::string &text_;
    std::uint32_t replica_count_{0};
    std::uint32_t tree_count_{0};
    bool default_apply_shape_selection_{false};
    std::size_t position_{0};
};

TransitionRequest parse_transition_request(
    const std::string &text,
    const AdaptiveV2ManagerRuntimeShape &runtime_shape,
    bool default_apply_shape_selection)
{
    return TransitionJsonParser(
        text,
        runtime_shape.quorum.replica_count,
        runtime_shape.tree_shape.tree_count,
        default_apply_shape_selection)
        .parse();
}

ExperimentBundleAttempt parse_experiment_bundle_attempt(
    const std::string &text,
    std::uint32_t replica_count)
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
    if (recipient >= replica_count ||
        attempt > kConvergenceMaximumAttempts)
    {
        throw std::invalid_argument(
            "experiment bundle attempt is outside convergence bounds");
    }
    return {static_cast<ReplicaID>(recipient), attempt};
}

std::vector<std::uint32_t> parse_shape_candidate_fanouts(
    const std::string &text)
{
    if (text.empty() || text.back() == ',')
    {
        throw std::invalid_argument(
            "shape candidate fanouts must use canonical CSV");
    }
    std::vector<std::uint32_t> fanouts;
    std::set<std::uint32_t> unique;
    std::size_t begin = 0;
    while (begin < text.size())
    {
        const auto separator = text.find(',', begin);
        const auto end = separator == std::string::npos
            ? text.size()
            : separator;
        const auto fanout = parse_unsigned<std::uint32_t>(
            text.substr(begin, end - begin),
            "shape candidate fanout", true);
        if (fanout > 255 || !unique.insert(fanout).second)
        {
            throw std::invalid_argument(
                "shape candidate fanouts must be unique uint8 values");
        }
        fanouts.push_back(fanout);
        if (separator == std::string::npos)
            break;
        begin = separator + 1;
    }
    if (fanouts.empty() || fanouts.size() > 255)
    {
        throw std::invalid_argument(
            "shape candidate fanouts must be nonempty and bounded");
    }
    return fanouts;
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

EpochDefinitionInput manager_epoch_zero(const ManagerOptions &options)
{
    auto epoch = hotstuff::derive_adaptive_v2_cyclic_epoch_zero(
        options.membership,
        options.runtime_shape.tree_shape.fanout,
        options.runtime_shape.tree_shape.pipeline_stretch);
    if (!epoch.has_value())
    {
        throw std::logic_error(
            "validated manager shape cannot derive canonical epoch zero");
    }
    return std::move(*epoch);
}

AdaptiveV2ManagerControllerConfig manager_controller_config(
    const ManagerOptions &options)
{
    AdaptiveV2ManagerControllerConfig config;
    config.selection.required_nonresponsive =
        options.required_nonresponsive;
    config.selection.minimum_score_drop =
        options.runtime_shape.minimum_score_drop;
    config.selection.minimum_timeouts_per_reporter =
        kTimeoutsPerReporter;
    config.selection.maximum_post_baseline_timeout_attempts =
        options.runtime_shape.maximum_post_baseline_timeout_attempts;
    config.selection.responsiveness_policy =
        options.responsiveness_policy;
    config.selection.snapshot_seed = kSnapshotSeed;
    config.selection.fault_containment_evidence_start_monotonic_ns =
        options.fault_containment_evidence_start_monotonic_ns;
    config.selection.fault_containment_required_tree_coverage =
        options.fault_containment_required_tree_coverage;
    config.reputation_limits.maximum_audit_updates =
        options.runtime_shape.ingress_limits.evidence_store
            .maximum_accepted_records;
    config.placement = TreePlacementInput{
        options.membership,
        options.runtime_shape.tree_shape,
        kSnapshotSeed,
        "adaptive-v2-performance-optimization-v1"};
    config.activation_delay_blocks = options.activation_delay_blocks;
    config.issuer_id = options.issuer_id;
    config.issuer_private_key = options.issuer_private_key;
    config.bundle_limits = options.runtime_shape.bundle_limits;
    config.shape_selection.candidate_fanouts =
        options.shape_candidate_fanouts;
    config.shape_selection.fixed_pipeline_stretch =
        options.runtime_shape.tree_shape.pipeline_stretch;
    config.shape_selection.deterministic_seed =
        options.shape_deterministic_seed;
    config.shape_adaptation_enabled =
        options.shape_adaptation_enabled;
    return config;
}

AdaptiveV2ManagerSessionConfig manager_session_config(
    const ManagerOptions &options)
{
    AdaptiveV2ManagerSessionConfig config;
    config.active_tree_id = kInitialTreeId;
    config.activation_generation = kInitialActivationGeneration;
    config.ingress_limits = options.runtime_shape.ingress_limits;
    config.controller = manager_controller_config(options);
    config.retry_interval_ticks = kConvergenceRetryIntervalTicks;
    config.maximum_attempts_per_recipient =
        kConvergenceMaximumAttempts;
    config.convergence_window_ticks =
        options.convergence_deadline_ticks;
    return config;
}

std::vector<AdaptiveV2TransitionPolicy> transition_policies(
    const ManagerOptions &options)
{
    std::vector<AdaptiveV2TransitionPolicy> policies;
    policies.reserve(options.transition_requests.size());
    for (const auto &request : options.transition_requests)
        policies.push_back(request.policy);
    return policies;
}

std::string transition_bundle_output_path(
    const TransitionRequest &request,
    const hotstuff::AdaptiveV2EpochChangeBundle &bundle,
    const AdaptiveV2ManagerSession &session)
{
    const auto &definition = bundle.definition();
    const auto &payload = bundle.command().payload;
    const auto predecessor_epoch_number =
        session.ingress().current_epoch().epoch_number();
    const auto successor_epoch_number =
        definition.epoch_number;
    const auto successor_epoch_digest =
        payload.successor_epoch_digest;
    if (request.bundle_output.empty() ||
        request.predecessor_epoch_number != predecessor_epoch_number ||
        request.successor_epoch_number != successor_epoch_number ||
        predecessor_epoch_number ==
            std::numeric_limits<std::uint32_t>::max() ||
        successor_epoch_number != predecessor_epoch_number + 1 ||
        payload.successor_epoch_number != successor_epoch_number ||
        definition.previous_epoch_digest !=
            session.ingress().current_epoch().epoch_digest() ||
        payload.predecessor_epoch_digest !=
            definition.previous_epoch_digest ||
        successor_epoch_digest !=
            hotstuff::compute_epoch_digest(definition))
    {
        throw std::logic_error(
            "successor bundle does not match the explicit transition request");
    }
    return request.bundle_output;
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

void write_exclusive_json(
    const std::string &path,
    const std::string &json)
{
    if (path.empty() || json.empty() || json.back() != '\n')
        throw std::invalid_argument(
            "JSON output path or canonical record is invalid");

    int fd = ::open(
        path.c_str(),
        O_WRONLY | O_CREAT | O_EXCL | O_CLOEXEC | O_NOFOLLOW,
        0600);
    if (fd < 0)
    {
        throw std::system_error(
            errno, std::generic_category(),
            "cannot exclusively create JSON output");
    }

    try
    {
        std::size_t offset = 0;
        while (offset < json.size())
        {
            const auto written = ::write(
                fd,
                json.data() + offset,
                json.size() - offset);
            if (written < 0 && errno == EINTR)
                continue;
            if (written <= 0)
            {
                throw std::system_error(
                    written < 0 ? errno : EIO,
                    std::generic_category(),
                    "cannot write canonical JSON output");
            }
            offset += static_cast<std::size_t>(written);
        }
        if (::fsync(fd) != 0)
        {
            throw std::system_error(
                errno, std::generic_category(),
                "cannot sync canonical JSON output");
        }
        const auto close_result = ::close(fd);
        fd = -1;
        if (close_result != 0)
        {
            throw std::system_error(
                errno, std::generic_category(),
                "cannot close canonical JSON output");
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
    auto opt_required_nonresponsive = Config::OptValStr::create();
    auto opt_responsiveness_policy_version =
        Config::OptValStr::create(
            "adaptive-v2-controller-responsiveness-v1");
    auto opt_responsiveness_attempt_window =
        Config::OptValStr::create("32");
    auto opt_responsiveness_minimum_attempts =
        Config::OptValStr::create("2");
    auto opt_responsiveness_minimum_response_rate_ppm =
        Config::OptValStr::create("750000");
    auto opt_responsiveness_maximum_timeout_rate_ppm =
        Config::OptValStr::create("250000");
    auto opt_responsiveness_trailing_timeout_streak =
        Config::OptValStr::create("2");
    auto opt_responsiveness_latency_percentile_basis_points =
        Config::OptValStr::create("5000");
    auto opt_tree_fanout = Config::OptValStr::create("2");
    auto opt_pipeline_stretch = Config::OptValStr::create("2");
    auto opt_shape_candidate_fanouts =
        Config::OptValStr::create("2,3,5");
    auto opt_shape_deterministic_seed =
        Config::OptValStr::create("41719");
    auto opt_shape_adaptation_enabled =
        Config::OptValFlag::create(false);
    auto opt_fault_containment_evidence_start_monotonic_ns =
        Config::OptValStr::create("0");
    auto opt_fault_containment_required_tree_coverage =
        Config::OptValStr::create("0");
    auto opt_cycle_1_selection_not_before_monotonic_ns =
        Config::OptValStr::create("0");
    auto opt_transition_requests = Config::OptValStrVec::create();
    auto opt_bundle_outputs = Config::OptValStrVec::create();
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
    config.add_opt(
        "required-nonresponsive",
        opt_required_nonresponsive,
        Config::SET_VAL);
    config.add_opt(
        "responsiveness-policy-version",
        opt_responsiveness_policy_version,
        Config::SET_VAL);
    config.add_opt(
        "responsiveness-attempt-window",
        opt_responsiveness_attempt_window,
        Config::SET_VAL);
    config.add_opt(
        "responsiveness-minimum-attempts",
        opt_responsiveness_minimum_attempts,
        Config::SET_VAL);
    config.add_opt(
        "responsiveness-minimum-response-rate-ppm",
        opt_responsiveness_minimum_response_rate_ppm,
        Config::SET_VAL);
    config.add_opt(
        "responsiveness-maximum-timeout-rate-ppm",
        opt_responsiveness_maximum_timeout_rate_ppm,
        Config::SET_VAL);
    config.add_opt(
        "responsiveness-trailing-timeout-streak",
        opt_responsiveness_trailing_timeout_streak,
        Config::SET_VAL);
    config.add_opt(
        "responsiveness-latency-percentile-basis-points",
        opt_responsiveness_latency_percentile_basis_points,
        Config::SET_VAL);
    config.add_opt(
        "tree-fanout", opt_tree_fanout, Config::SET_VAL);
    config.add_opt(
        "pipeline-stretch", opt_pipeline_stretch, Config::SET_VAL);
    config.add_opt(
        "shape-candidate-fanouts",
        opt_shape_candidate_fanouts,
        Config::SET_VAL);
    config.add_opt(
        "shape-deterministic-seed",
        opt_shape_deterministic_seed,
        Config::SET_VAL);
    config.add_opt(
        "shape-adaptation-enabled",
        opt_shape_adaptation_enabled,
        Config::SWITCH_ON);
    config.add_opt(
        "fault-containment-evidence-start-monotonic-ns",
        opt_fault_containment_evidence_start_monotonic_ns,
        Config::SET_VAL);
    config.add_opt(
        "fault-containment-required-tree-coverage",
        opt_fault_containment_required_tree_coverage,
        Config::SET_VAL);
    config.add_opt(
        "cycle-1-selection-not-before-monotonic-ns",
        opt_cycle_1_selection_not_before_monotonic_ns,
        Config::SET_VAL);
    config.add_opt(
        "transition-request", opt_transition_requests, Config::APPEND);
    config.add_opt(
        "bundle-output", opt_bundle_outputs, Config::APPEND);
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

    options.responsiveness_policy.policy_version =
        opt_responsiveness_policy_version->get();
    options.responsiveness_policy.attempt_window =
        parse_unsigned<std::uint32_t>(
            opt_responsiveness_attempt_window->get(),
            "responsiveness attempt window",
            true);
    options.responsiveness_policy.minimum_attempts =
        parse_unsigned<std::uint32_t>(
            opt_responsiveness_minimum_attempts->get(),
            "responsiveness minimum attempts",
            true);
    options.responsiveness_policy.minimum_response_rate_ppm =
        parse_unsigned<hotstuff::RatePpm>(
            opt_responsiveness_minimum_response_rate_ppm->get(),
            "responsiveness minimum response rate ppm",
            false);
    options.responsiveness_policy.maximum_timeout_rate_ppm =
        parse_unsigned<hotstuff::RatePpm>(
            opt_responsiveness_maximum_timeout_rate_ppm->get(),
            "responsiveness maximum timeout rate ppm",
            false);
    options.responsiveness_policy.trailing_timeout_streak =
        parse_unsigned<std::uint32_t>(
            opt_responsiveness_trailing_timeout_streak->get(),
            "responsiveness trailing timeout streak",
            true);
    options.responsiveness_policy.latency_percentile_basis_points =
        parse_unsigned<std::uint16_t>(
            opt_responsiveness_latency_percentile_basis_points->get(),
            "responsiveness latency percentile basis points",
            true);
    const auto &responsiveness = options.responsiveness_policy;
    if (responsiveness.policy_version.empty() ||
        responsiveness.policy_version.size() >
            hotstuff::kMaximumAdaptationPolicyVersionBytes ||
        responsiveness.attempt_window >
            hotstuff::kMaximumAdaptationAttemptWindow ||
        responsiveness.minimum_attempts >
            responsiveness.attempt_window ||
        responsiveness.trailing_timeout_streak < 2 ||
        responsiveness.trailing_timeout_streak >
            responsiveness.attempt_window ||
        responsiveness.minimum_response_rate_ppm >
            hotstuff::kRatePpmScale ||
        responsiveness.maximum_timeout_rate_ppm >
            hotstuff::kRatePpmScale ||
        responsiveness.latency_percentile_basis_points >
            hotstuff::kPercentileBasisPointScale)
    {
        throw std::invalid_argument(
            "responsiveness policy values are out of bounds");
    }

    for (const auto &raw : opt_replicas->get())
        options.replicas.push_back(parse_replica_endpoint(raw));
    std::sort(
        options.replicas.begin(), options.replicas.end(),
        [](const auto &left, const auto &right) {
            return left.replica_id < right.replica_id;
        });

    std::set<ReplicaID> ids;
    std::set<PeerId> peer_ids;
    std::set<std::string> addresses;
    options.membership.reserve(options.replicas.size());
    for (std::size_t index = 0;
         index < options.replicas.size();
         ++index)
    {
        const auto &replica = options.replicas[index];
        if (index > std::numeric_limits<ReplicaID>::max() ||
            replica.replica_id != static_cast<ReplicaID>(index) ||
            !ids.insert(replica.replica_id).second ||
            !peer_ids.insert(replica.peer_id).second ||
            !addresses.insert(std::string(replica.address)).second ||
            replica.peer_id == options.local_peer_id)
        {
            throw std::invalid_argument(
                "replica IDs, addresses, and TLS identities must be exact and unique");
        }
        options.membership.push_back(replica.replica_id);
    }

    const auto tree_fanout = parse_unsigned<std::uint32_t>(
        opt_tree_fanout->get(), "tree fanout", true);
    const auto pipeline_stretch = parse_unsigned<std::uint32_t>(
        opt_pipeline_stretch->get(), "pipeline stretch", true);
    const auto runtime_shape =
        hotstuff::derive_adaptive_v2_manager_runtime_shape(
            options.membership, tree_fanout, pipeline_stretch);
    if (!runtime_shape.has_value())
    {
        throw std::invalid_argument(
            "replicas and topology must define a bounded contiguous N=3f+1 adaptive-v2 manager shape");
    }
    options.runtime_shape = *runtime_shape;
    options.required_nonresponsive =
        opt_required_nonresponsive->get().empty()
            ? options.runtime_shape.required_nonresponsive
            : parse_unsigned<std::uint32_t>(
                  opt_required_nonresponsive->get(),
                  "required nonresponsive", true);
    if (options.required_nonresponsive >
        options.runtime_shape.quorum.fault_threshold)
    {
        throw std::invalid_argument(
            "required nonresponsive must not exceed the derived fault threshold");
    }
    options.shape_candidate_fanouts =
        parse_shape_candidate_fanouts(
            opt_shape_candidate_fanouts->get());
    options.shape_deterministic_seed =
        parse_unsigned<std::uint64_t>(
            opt_shape_deterministic_seed->get(),
            "shape deterministic seed", false);
    if (std::find(
            options.shape_candidate_fanouts.begin(),
            options.shape_candidate_fanouts.end(),
            tree_fanout) == options.shape_candidate_fanouts.end())
    {
        throw std::invalid_argument(
            "shape candidate fanouts must contain the initial fanout");
    }
    options.shape_adaptation_enabled =
        opt_shape_adaptation_enabled->get();
    options.fault_containment_evidence_start_monotonic_ns =
        parse_unsigned<std::uint64_t>(
            opt_fault_containment_evidence_start_monotonic_ns->get(),
            "fault containment evidence start monotonic ns",
            false);
    options.fault_containment_required_tree_coverage =
        parse_unsigned<std::uint32_t>(
            opt_fault_containment_required_tree_coverage->get(),
            "fault containment required tree coverage",
            false);
    options.cycle_1_selection_not_before_monotonic_ns =
        parse_unsigned<std::uint64_t>(
            opt_cycle_1_selection_not_before_monotonic_ns->get(),
            "cycle 1 selection not before monotonic ns",
            false);
    const bool fault_coverage_enabled =
        options.fault_containment_evidence_start_monotonic_ns != 0;
    if (fault_coverage_enabled !=
            (options.fault_containment_required_tree_coverage != 0) ||
        (fault_coverage_enabled &&
         options.fault_containment_required_tree_coverage !=
             options.membership.size()))
    {
        throw std::invalid_argument(
            "fault containment coverage requires a nonzero timestamp and the exact predecessor tree count");
    }

    const auto &raw_transition_requests =
        opt_transition_requests->get();
    const auto &bundle_outputs = opt_bundle_outputs->get();
    if (raw_transition_requests.empty() ||
        raw_transition_requests.size() != bundle_outputs.size())
    {
        throw std::invalid_argument(
            "transition requests and bundle outputs must be nonempty and paired");
    }

    std::set<std::string> artifact_ids;
    std::set<std::string> declared_bundle_paths;
    std::set<std::string> evidence_snapshot_paths;
    std::set<std::string> exclusive_artifact_outputs;
    for (std::size_t index = 0;
         index < raw_transition_requests.size();
         ++index)
    {
        auto request = parse_transition_request(
            raw_transition_requests[index],
            options.runtime_shape,
            options.shape_adaptation_enabled);
        request.bundle_output = bundle_outputs[index];
        if (request.bundle_output.size() >=
            request.declared_bundle_path.size())
        {
            const auto artifact_root_bytes =
                request.bundle_output.size() -
                request.declared_bundle_path.size();
            request.evidence_snapshot_output =
                request.bundle_output.substr(0, artifact_root_bytes) +
                request.evidence_snapshot_path;
        }
        if (request.bundle_output.empty() ||
            request.bundle_output.front() != '/' ||
            request.bundle_output.size() >
                kMaximumTransitionPathBytes ||
            request.bundle_output.find("//") != std::string::npos ||
            request.bundle_output.find("/./") != std::string::npos ||
            request.bundle_output.find("/../") != std::string::npos ||
            request.bundle_output.size() <
                request.declared_bundle_path.size() ||
            request.bundle_output.compare(
                request.bundle_output.size() -
                    request.declared_bundle_path.size(),
                request.declared_bundle_path.size(),
                request.declared_bundle_path) != 0 ||
            (request.bundle_output.size() >
                 request.declared_bundle_path.size() &&
             request.bundle_output[
                 request.bundle_output.size() -
                 request.declared_bundle_path.size() - 1] != '/') ||
            !artifact_ids.insert(
                request.transition_artifact_id).second ||
            !declared_bundle_paths.insert(
                request.declared_bundle_path).second ||
            !evidence_snapshot_paths.insert(
                request.evidence_snapshot_path).second ||
            request.evidence_snapshot_output.empty() ||
            request.evidence_snapshot_output.front() != '/' ||
            request.evidence_snapshot_output.size() >
                kMaximumTransitionPathBytes ||
            request.evidence_snapshot_output.find("//") !=
                std::string::npos ||
            request.evidence_snapshot_output.find("/./") !=
                std::string::npos ||
            request.evidence_snapshot_output.find("/../") !=
                std::string::npos ||
            !exclusive_artifact_outputs.insert(
                request.bundle_output).second ||
            !exclusive_artifact_outputs.insert(
                request.evidence_snapshot_output).second)
        {
            throw std::invalid_argument(
                "transition artifact identities and paths must be exact and unique");
        }
        if (options.transition_requests.empty() &&
            request.predecessor_epoch_number != 0)
        {
            throw std::invalid_argument(
                "the first transition must name epoch zero as predecessor");
        }
        if (options.transition_requests.empty() &&
            request.minimum_predecessor_residency_ms != 0)
        {
            throw std::invalid_argument(
                "the first transition residency must be zero");
        }
        if (!options.transition_requests.empty() &&
            (options.transition_requests.back()
                     .successor_epoch_number !=
                 request.predecessor_epoch_number))
        {
            throw std::invalid_argument(
                "transition requests must form one exact successor chain");
        }
        options.transition_requests.push_back(std::move(request));
    }
    if (options.cycle_1_selection_not_before_monotonic_ns != 0 &&
        (options.transition_requests.size() != 2 ||
         options.fault_containment_evidence_start_monotonic_ns == 0 ||
         options.cycle_1_selection_not_before_monotonic_ns <=
             options.fault_containment_evidence_start_monotonic_ns ||
         options.transition_requests[1].predecessor_epoch_number != 1 ||
         options.transition_requests[1].successor_epoch_number != 2 ||
         options.transition_requests[1].minimum_predecessor_residency_ms !=
             60'000 ||
         options.transition_requests[1]
                 .minimum_post_baseline_observation_ms != 0))
    {
        throw std::invalid_argument(
            "cycle 1 selection gate requires the exact two-transition repair path");
    }
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
    if (exclusive_artifact_outputs.count(
            options.structured_event_output) != 0)
    {
        throw std::invalid_argument(
            "structured-event and artifact outputs must be distinct");
    }
    if (!opt_experiment_drop_bundle_attempt->get().empty())
    {
        options.experiment_drop_bundle_attempt =
            parse_experiment_bundle_attempt(
                opt_experiment_drop_bundle_attempt->get(),
                options.runtime_shape.quorum.replica_count);
    }
    if (!opt_experiment_drop_activation_ack->get().empty())
    {
        const auto ordinal = parse_unsigned<std::uint32_t>(
            opt_experiment_drop_activation_ack->get(),
            "experiment activation ACK ordinal", true);
        if (ordinal != options.runtime_shape.quorum.quorum)
        {
            throw std::invalid_argument(
                "experiment activation ACK ordinal must equal quorum");
        }
        options.experiment_drop_activation_ack = ordinal;
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
        hotstuff::StructuredEventClock &monotonic_raw_clock,
        hotstuff::StructuredEventSink &structured_event_sink)
        : event_context_(event_context),
          monotonic_raw_clock_(monotonic_raw_clock),
          options_(std::move(options)),
          network_(event_context_, net_config),
          session_(
              options_.membership,
              manager_epoch_zero(options_),
              manager_session_config(options_)),
          request_sequence_(transition_policies(options_)),
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
        predecessor_residency_timer = salticidae::TimerEvent(
            event_context_,
            [this](salticidae::TimerEvent &) {
                handle_predecessor_residency_timer();
            });
        post_baseline_observation_timer = salticidae::TimerEvent(
            event_context_,
            [this](salticidae::TimerEvent &) {
                handle_post_baseline_observation_timer();
            });
        cycle_1_selection_gate_timer = salticidae::TimerEvent(
            event_context_,
            [this](salticidae::TimerEvent &) {
                handle_cycle_1_selection_gate_timer();
            });
        evaluation_timer = salticidae::TimerEvent(
            event_context_,
            [this](salticidae::TimerEvent &) {
                handle_evaluation_timer();
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

            if (!begin_current_cycle())
                fail("manager_cycle_start_failed");
            else
                evaluate();

            if (!failed_)
            {
                HOTSTUFF_LOG_INFO(
                    "KAURI_ADAPTIVE_MANAGER listening=%s n=%u f=%u quorum=%u "
                    "fanout=%u pipeline_stretch=%u",
                    std::string(options_.listen_address).c_str(),
                    options_.runtime_shape.quorum.replica_count,
                    options_.runtime_shape.quorum.fault_threshold,
                    options_.runtime_shape.quorum.quorum,
                    options_.runtime_shape.tree_shape.fanout,
                    options_.runtime_shape.tree_shape.pipeline_stretch);
                emit_process_lifecycle(
                    hotstuff::ProcessLifecycleState::ready);
                structured_event_drain_timer.add(0.05);
                event_context_.dispatch();
            }

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
            return failed_ || !request_sequence_.shutdown_eligible()
                ? 1
                : 0;
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
        bool terminal_counts_applied = false;
        if (event.identity.has_value())
        {
            const auto &records = session_.terminal_records();
            const auto record = std::find_if(
                records.rbegin(),
                records.rend(),
                [&event](const auto &candidate) {
                    return candidate.winning_activation.has_value() &&
                        *candidate.winning_activation == *event.identity;
                });
            if (record != records.rend())
            {
                event.accepted_commit_count =
                    record->accepted_commit_count;
                event.accepted_activation_count =
                    record->accepted_activation_count;
                terminal_counts_applied = true;
            }
        }
        if (!terminal_counts_applied)
        {
            const auto convergence = session_.convergence_audit();
            if (convergence.has_value())
            {
                event.accepted_commit_count =
                    convergence->accepted_commit_count;
                event.accepted_activation_count =
                    transition ==
                                hotstuff::AdaptiveV2ConvergenceTransition::
                                    converged ||
                            transition ==
                                hotstuff::AdaptiveV2ConvergenceTransition::
                                    ready
                        ? convergence->winning_activation_count
                        : convergence->accepted_activation_count;
            }
        }
        event.required_activation_count =
            options_.runtime_shape.quorum.quorum;
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

    const TransitionRequest *current_transition_request() const noexcept
    {
        const auto cursor = request_sequence_.cursor();
        return cursor < options_.transition_requests.size()
            ? &options_.transition_requests[cursor]
            : nullptr;
    }

    bool transition_policy_matches_current_roots(
        const AdaptiveV2TransitionPolicy &policy) const noexcept
    {
        if (policy.intent !=
            TreePolicyKind::fault_containment)
        {
            return policy.containment_baseline_roots.empty();
        }

        const auto &trees =
            session_.ingress().current_epoch().trees();
        if (policy.containment_baseline_roots.size() !=
            options_.runtime_shape.tree_shape.tree_count)
        {
            return false;
        }
        for (const auto &root : policy.containment_baseline_roots)
        {
            const auto tree = std::find_if(
                trees.begin(), trees.end(),
                [&root](const EpochTreeDefinition &candidate) {
                    return candidate.tree_id == root.tree_id;
                });
            if (tree == trees.end() ||
                tree->members_breadth_first.empty() ||
                tree->members_breadth_first.front() !=
                    root.replica_id)
            {
                return false;
            }
        }
        return true;
    }

    std::optional<AdaptiveV2TransitionPolicy>
    resolved_transition_policy(
        const TransitionRequest &request) const noexcept
    {
        try
        {
            auto resolved = request.policy;
            if (request.resolve_containment_roots_from_predecessor)
            {
                if (resolved.intent != TreePolicyKind::fault_containment ||
                    !resolved.containment_baseline_roots.empty())
                {
                    return std::nullopt;
                }
                const auto tree_count =
                    options_.runtime_shape.tree_shape.tree_count;
                std::vector<std::optional<ReplicaID>> roots(tree_count);
                for (const auto &tree :
                     session_.ingress().current_epoch().trees())
                {
                    if (tree.tree_id >= tree_count)
                        continue;
                    if (tree.members_breadth_first.empty() ||
                        roots[tree.tree_id].has_value())
                    {
                        return std::nullopt;
                    }
                    roots[tree.tree_id] =
                        tree.members_breadth_first.front();
                }
                std::set<ReplicaID> unique_roots;
                for (std::uint32_t tree_id = 0;
                     tree_id < tree_count;
                     ++tree_id)
                {
                    if (!roots[tree_id].has_value() ||
                        !unique_roots.insert(*roots[tree_id]).second)
                    {
                        return std::nullopt;
                    }
                    resolved.containment_baseline_roots.push_back(
                        hotstuff::BaselineRoot{
                            tree_id, *roots[tree_id]});
                }
            }
            return transition_policy_matches_current_roots(resolved)
                ? std::optional<AdaptiveV2TransitionPolicy>{
                      std::move(resolved)}
                : std::nullopt;
        }
        catch (...)
        {
            return std::nullopt;
        }
    }

    bool add_cycle_audit_context(
        const TransitionRequest &request) noexcept
    {
        if (request.predecessor_epoch_number !=
            session_.ingress().current_epoch().epoch_number())
        {
            return false;
        }
        try
        {
            const auto activation_generation =
                session_.ingress().activation_generation();
            if (activation_generation == 0)
                return false;
            cycle_audits_.push_back(CycleAuditContext{
                request.transition_artifact_id,
                activation_generation,
                0,
                0});
            return true;
        }
        catch (...)
        {
            return false;
        }
    }

    bool begin_current_cycle() noexcept
    {
        cancel_pending_evaluation();
        last_evaluated_ready_members_.reset();
        last_evaluated_evidence_cutoff_.reset();
        const auto *request = current_transition_request();
        const auto *sequence_policy = request_sequence_.current_policy();
        if (request == nullptr || sequence_policy == nullptr ||
            sequence_policy->intent != request->policy.intent ||
            sequence_policy->apply_shape_selection !=
                request->policy.apply_shape_selection)
        {
            return false;
        }
        auto policy = resolved_transition_policy(*request);
        if (!policy.has_value() || !add_cycle_audit_context(*request))
            return false;
        if (!session_.begin_cycle(*policy))
        {
            cycle_audits_.pop_back();
            return false;
        }
        emitted_score_trajectory_ = 0;
        accepted_activation_ack_ordinal_ = 0;
        convergence_failure_emitted_ = false;
        refresh_cycle_audit();
        return true;
    }

    bool schedule_post_baseline_observation(
        const TransitionRequest &request) noexcept
    {
        if (post_baseline_observation_pending_)
            return false;
        const auto audit = session_.controller_audit();
        if (!audit.has_value() || !audit->baseline_frozen)
            return false;
        if (request.minimum_post_baseline_observation_ms == 0)
        {
            schedule_post_baseline_evaluation();
            return true;
        }

        try
        {
            post_baseline_observation_deadline_ =
                std::chrono::steady_clock::now() +
                std::chrono::milliseconds(
                    request.minimum_post_baseline_observation_ms);
            post_baseline_observation_pending_ = true;
            post_baseline_observation_timer.add(
                static_cast<double>(
                    request.minimum_post_baseline_observation_ms) /
                1000.0);
            return true;
        }
        catch (...)
        {
            post_baseline_observation_pending_ = false;
            post_baseline_observation_timer.del();
            return false;
        }
    }

    void schedule_post_baseline_evaluation() noexcept
    {
        last_evaluated_ready_members_.reset();
        last_evaluated_evidence_cutoff_.reset();
        schedule_evaluation();
    }

    void handle_post_baseline_observation_timer() noexcept
    {
        if (!post_baseline_observation_pending_ || failed_)
            return;

        try
        {
            const auto now = std::chrono::steady_clock::now();
            if (now < post_baseline_observation_deadline_)
            {
                post_baseline_observation_timer.add(
                    std::chrono::duration<double>(
                        post_baseline_observation_deadline_ - now)
                        .count());
                return;
            }

            post_baseline_observation_pending_ = false;
            evaluate();
        }
        catch (...)
        {
            post_baseline_observation_pending_ = false;
            fail("post_baseline_observation_timer_failed");
        }
    }

    void cancel_cycle_1_selection_gate() noexcept
    {
        cycle_1_selection_gate_timer.del();
        cycle_1_selection_gate_pending_ = false;
    }

    bool arm_cycle_1_selection_gate(std::uint64_t now_ns) noexcept
    {
        const auto selection_not_before_ns =
            options_.cycle_1_selection_not_before_monotonic_ns;
        if (selection_not_before_ns == 0 ||
            now_ns > selection_not_before_ns)
        {
            return false;
        }
        if (cycle_1_selection_gate_pending_)
            return true;

        try
        {
            const auto remaining_ns = selection_not_before_ns - now_ns;
            const auto delay_seconds =
                static_cast<double>(remaining_ns) / 1'000'000'000.0 +
                kEvaluationCoalescingSeconds;
            cycle_1_selection_gate_pending_ = true;
            cycle_1_selection_gate_timer.add(delay_seconds);
            return true;
        }
        catch (...)
        {
            cancel_cycle_1_selection_gate();
            return false;
        }
    }

    bool cycle_1_selection_gate_ready() noexcept
    {
        const auto selection_not_before_ns =
            options_.cycle_1_selection_not_before_monotonic_ns;
        if (selection_not_before_ns == 0 ||
            request_sequence_.cursor() != 1)
        {
            return true;
        }

        const auto *request = current_transition_request();
        if (request == nullptr ||
            request->predecessor_epoch_number != 1 ||
            request->successor_epoch_number != 2)
        {
            fail("cycle_1_selection_gate_context_invalid");
            return false;
        }

        const auto now_ns = monotonic_raw_clock_.now_ns();
        if (now_ns == 0 || !monotonic_raw_clock_.healthy())
        {
            fail("cycle_1_selection_gate_clock_unhealthy");
            return false;
        }
        if (now_ns > selection_not_before_ns)
        {
            cancel_cycle_1_selection_gate();
            return true;
        }
        if (!arm_cycle_1_selection_gate(now_ns))
            fail("cycle_1_selection_gate_timer_failed");
        return false;
    }

    void handle_cycle_1_selection_gate_timer() noexcept
    {
        if (!cycle_1_selection_gate_pending_ || failed_)
            return;
        cycle_1_selection_gate_pending_ = false;
        if (!cycle_1_selection_gate_ready())
            return;
        evaluate();
    }

    bool schedule_current_predecessor_residency() noexcept
    {
        const auto *request = current_transition_request();
        if (request == nullptr || predecessor_residency_pending_)
            return false;

        try
        {
            if (request->minimum_predecessor_residency_ms == 0)
            {
                if (!begin_current_cycle())
                    return false;
                evaluate();
                return !failed_;
            }

            predecessor_residency_deadline_ =
                std::chrono::steady_clock::now() +
                std::chrono::milliseconds(
                    request->minimum_predecessor_residency_ms);
            predecessor_residency_pending_ = true;
            predecessor_residency_timer.add(
                static_cast<double>(
                    request->minimum_predecessor_residency_ms) /
                1000.0);
            return true;
        }
        catch (...)
        {
            predecessor_residency_pending_ = false;
            predecessor_residency_timer.del();
            return false;
        }
    }

    void handle_predecessor_residency_timer() noexcept
    {
        if (!predecessor_residency_pending_ || failed_)
            return;

        try
        {
            const auto now = std::chrono::steady_clock::now();
            if (now < predecessor_residency_deadline_)
            {
                predecessor_residency_timer.add(
                    std::chrono::duration<double>(
                        predecessor_residency_deadline_ - now)
                        .count());
                return;
            }

            predecessor_residency_pending_ = false;
            if (!begin_current_cycle())
            {
                fail("manager_cycle_start_failed_after_residency");
                return;
            }
            evaluate();
        }
        catch (...)
        {
            predecessor_residency_pending_ = false;
            fail("predecessor_residency_timer_failed");
        }
    }

    void refresh_cycle_audit() noexcept
    {
        if (cycle_audits_.empty())
            return;
        const auto audit = session_.controller_audit();
        if (!audit.has_value())
            return;
        cycle_audits_.back().baseline_evidence_cutoff =
            audit->baseline_cutoff;
        cycle_audits_.back().current_evidence_cutoff =
            audit->current_cutoff;
    }

    void emit_shape_decision(
        const TransitionRequest &request,
        const hotstuff::AdaptiveV2EpochChangeBundle &bundle)
    {
        if (cycle_audits_.empty() ||
            request_sequence_.cursor() != cycle_audits_.size() - 1)
        {
            throw std::logic_error(
                "shape decision has no exact cycle audit context");
        }

        auto &cycle = cycle_audits_.back();
        const auto controller = session_.controller_audit();
        if (cycle.transition_artifact_id !=
                request.transition_artifact_id ||
            !controller.has_value())
        {
            throw std::logic_error(
                "shape decision audit context was absent or rebound");
        }

        const auto &predecessor = session_.ingress().current_epoch();
        const auto &successor = bundle.definition();
        const auto preserves_initial_containment_shape =
            request.predecessor_epoch_number == 0 &&
            request.policy.intent ==
                hotstuff::TreePolicyKind::fault_containment &&
            !request.policy.apply_shape_selection;
        if (preserves_initial_containment_shape)
        {
            if (predecessor.epoch_number() != 0 ||
                cycle.shape_decision_emitted ||
                controller->shape_decision.has_value() ||
                predecessor.trees().empty() ||
                successor.trees.empty())
            {
                throw std::logic_error(
                    "initial containment shape audit was invalid");
            }
            const auto fanout =
                predecessor.trees().front().fanout;
            const auto pipeline_stretch =
                predecessor.trees().front().pipeline_stretch;
            const auto has_preserved_shape =
                std::all_of(
                    predecessor.trees().begin(),
                    predecessor.trees().end(),
                    [fanout, pipeline_stretch](const auto &tree) {
                        return tree.fanout == fanout &&
                            tree.pipeline_stretch == pipeline_stretch;
                    }) &&
                std::all_of(
                    successor.trees.begin(),
                    successor.trees.end(),
                    [fanout, pipeline_stretch](const auto &tree) {
                        return tree.fanout == fanout &&
                            tree.pipeline_stretch == pipeline_stretch;
                    });
            if (!has_preserved_shape)
            {
                throw std::logic_error(
                    "initial containment shape was not preserved");
            }
            return;
        }
        if (cycle.shape_decision_emitted ||
            !controller->shape_decision.has_value())
        {
            throw std::logic_error(
                "shape decision was duplicated or absent");
        }

        const auto &decision = *controller->shape_decision;
        if (decision.epoch_number != predecessor.epoch_number() ||
            decision.epoch_digest != predecessor.epoch_digest() ||
            decision.evidence_cutoff != cycle.current_evidence_cutoff ||
            decision.predecessor_tree_count !=
                predecessor.trees().size() ||
            decision.tree_count != successor.trees.size() ||
            !std::all_of(
                successor.trees.begin(), successor.trees.end(),
                [&decision](const auto &tree) {
                    return tree.fanout == decision.applied_fanout &&
                        tree.pipeline_stretch ==
                            decision.fixed_pipeline_stretch;
                }))
        {
            throw std::logic_error(
                "shape decision does not bind the successor topology");
        }

        hotstuff::AdaptiveV2ShapeDecisionStructuredEvent event;
        event.cycle_ordinal = request_sequence_.cursor();
        event.transition_artifact_id =
            request.transition_artifact_id;
        event.decision = decision;
        structured_event_sink_.emit_audit(
            hotstuff::AuditStructuredEventPayload{std::move(event)});
        structured_event_sink_.drain();
        if (!structured_event_sink_.health().healthy)
        {
            throw std::runtime_error(
                "shape decision audit drain failed");
        }
        cycle.shape_decision_emitted = true;
    }

    void emit_evidence_snapshot(
        const TransitionRequest &request,
        const hotstuff::AdaptiveV2EpochChangeBundle &bundle)
    {
        if (cycle_audits_.empty() ||
            request_sequence_.cursor() != cycle_audits_.size() - 1)
        {
            throw std::logic_error(
                "evidence snapshot has no exact cycle audit context");
        }

        auto &audit = cycle_audits_.back();
        if (audit.evidence_snapshot_emitted ||
            audit.transition_artifact_id !=
                request.transition_artifact_id)
        {
            throw std::logic_error(
                "evidence snapshot was duplicated or rebound");
        }

        const auto &ingress = session_.ingress();
        const auto &predecessor = ingress.current_epoch();
        const auto &ledger = ingress.ledger();
        const auto &definition = bundle.definition();
        if (request.predecessor_epoch_number !=
                predecessor.epoch_number() ||
            definition.previous_epoch_digest !=
                predecessor.epoch_digest() ||
            definition.evidence_cutoff !=
                audit.current_evidence_cutoff ||
            definition.evidence_snapshot_id.empty() ||
            audit.activation_generation !=
                ingress.activation_generation() ||
            audit.baseline_evidence_cutoff == 0 ||
            audit.current_evidence_cutoff <=
                audit.baseline_evidence_cutoff ||
            ledger.high_watermark() !=
                audit.current_evidence_cutoff)
        {
            throw std::logic_error(
                "evidence snapshot window is not the selected exact prefix");
        }

        hotstuff::AdaptiveV2EvidenceSnapshotStructuredEvent event;
        event.cycle_ordinal = request_sequence_.cursor();
        event.policy_intent = request.policy.intent;
        event.transition_artifact_id =
            request.transition_artifact_id;
        event.predecessor_epoch_number =
            predecessor.epoch_number();
        event.predecessor_epoch_digest =
            predecessor.epoch_digest();
        event.activation_generation = audit.activation_generation;
        event.baseline_cutoff = audit.baseline_evidence_cutoff;
        event.current_cutoff = audit.current_evidence_cutoff;

        std::uint64_t previous_ingestion_sequence = 0;
        std::size_t accepted_prefix_count = 0;
        bool has_post_baseline_observation = false;
        for (const auto &record : ledger.accepted())
        {
            if (record.ingestion_sequence == 0 ||
                record.ingestion_sequence <=
                previous_ingestion_sequence)
            {
                throw std::logic_error(
                    "accepted evidence is not canonically ordered");
            }
            previous_ingestion_sequence = record.ingestion_sequence;
            if (record.ingestion_sequence > event.current_cutoff)
                continue;

            const auto &observation = record.observation;
            if (observation.configuration.epoch_number !=
                    event.predecessor_epoch_number ||
                observation.configuration.epoch_digest !=
                    event.predecessor_epoch_digest)
            {
                throw std::logic_error(
                    "evidence snapshot contains a mixed epoch");
            }

            if (observation.outcome == hotstuff::ResponseOutcome::timeout)
            {
                if (observation.response_duration_us != 0)
                {
                    throw std::logic_error(
                        "timeout evidence has a response duration");
                }
            }
            else if (observation.outcome ==
                         hotstuff::ResponseOutcome::late &&
                     observation.response_duration_us == 0)
            {
                throw std::logic_error(
                    "late evidence has no response duration");
            }
            ++accepted_prefix_count;
            has_post_baseline_observation =
                has_post_baseline_observation ||
                record.ingestion_sequence > event.baseline_cutoff;
        }
        if (accepted_prefix_count == 0 ||
            accepted_prefix_count > event.current_cutoff ||
            !has_post_baseline_observation)
        {
            throw std::logic_error(
                "evidence snapshot accepted prefix count is invalid");
        }

        const hotstuff::AcceptedEvidenceView full_prefix{
            ledger.accepted().data(), accepted_prefix_count};
        const auto full_prefix_snapshot =
            hotstuff::build_adaptation_snapshot(
                options_.membership,
                hotstuff::AdaptationEpochId{
                    event.predecessor_epoch_number,
                    event.predecessor_epoch_digest},
                full_prefix,
                event.current_cutoff,
                options_.responsiveness_policy,
                kSnapshotSeed);
        if (full_prefix_snapshot.accepted_record_count() !=
                accepted_prefix_count ||
            full_prefix_snapshot.evidence_cutoff() !=
                event.current_cutoff)
        {
            throw std::logic_error(
                "full evidence prefix snapshot is inconsistent");
        }
        event.full_prefix_snapshot_id = hotstuff::uint256_t(parse_hex(
            full_prefix_snapshot.snapshot_id(),
            "full prefix evidence snapshot id",
            64));
        event.evidence_snapshot_id = hotstuff::uint256_t(parse_hex(
            definition.evidence_snapshot_id,
            "selected evidence snapshot id",
            64));
        event.accepted_prefix_count = accepted_prefix_count;
        if (event.full_prefix_snapshot_id == hotstuff::uint256_t{} ||
            event.evidence_snapshot_id == hotstuff::uint256_t{})
        {
            throw std::logic_error(
                "evidence snapshot commitment is zero");
        }

        std::set<ReplicaID> eligible_leaders;
        for (const auto &tree : definition.trees)
        {
            if (tree.members_breadth_first.empty() ||
                !eligible_leaders.insert(
                    tree.members_breadth_first.front()).second)
            {
                throw std::logic_error(
                    "successor tree leaders are not an exact ranking");
            }
            event.eligible_ranking.push_back(
                tree.members_breadth_first.front());
        }

        auto canonical_payload =
            hotstuff::serialize_adaptive_v2_evidence_snapshot_payload(
                event,
                hotstuff::StructuredEventLimits{}.maximum_line_bytes);
        canonical_payload.push_back('\n');
        structured_event_sink_.emit_audit(
            hotstuff::AuditStructuredEventPayload{event});
        structured_event_sink_.drain();
        if (!structured_event_sink_.health().healthy)
        {
            throw std::runtime_error(
                "evidence snapshot audit drain failed");
        }
        write_exclusive_json(
            request.evidence_snapshot_output, canonical_payload);
        audit.evidence_snapshot_emitted = true;
    }

    void emit_new_session_terminals() noexcept
    {
        try
        {
            const auto &records = session_.terminal_records();
            while (emitted_session_terminals_ < records.size())
            {
                const auto &record = records[emitted_session_terminals_];
                if (record.cycle_ordinal >= cycle_audits_.size())
                {
                    failed_ = true;
                    event_context_.stop();
                    return;
                }
                const auto &audit = cycle_audits_[
                    static_cast<std::size_t>(record.cycle_ordinal)];
                hotstuff::AdaptiveV2ManagerSessionTerminalStructuredEvent
                    event;
                event.cycle_ordinal = record.cycle_ordinal;
                event.policy_intent = record.policy_intent;
                event.outcome = record.outcome;
                event.reason = record.reason;
                event.transition_artifact_id =
                    audit.transition_artifact_id;
                event.predecessor_epoch_number =
                    record.predecessor_epoch_number;
                event.predecessor_epoch_digest =
                    record.predecessor_epoch_digest;
                event.successor_epoch_number =
                    record.successor_epoch_number;
                event.successor_epoch_digest =
                    record.successor_epoch_digest;
                event.command_payload_digest =
                    record.command_payload_digest;
                event.winning_activation = record.winning_activation;
                event.evidence_window_activation_generation =
                    audit.activation_generation;
                event.baseline_evidence_cutoff =
                    audit.baseline_evidence_cutoff;
                event.current_evidence_cutoff =
                    audit.current_evidence_cutoff;
                structured_event_sink_.emit_audit(
                    hotstuff::AuditStructuredEventPayload{
                        std::move(event)});
                if (!structured_event_sink_.health().healthy)
                {
                    failed_ = true;
                    event_context_.stop();
                    return;
                }
                ++emitted_session_terminals_;
            }
        }
        catch (...)
        {
            failed_ = true;
            event_context_.stop();
        }
    }

    void fail(const char *reason) noexcept
    {
        cancel_pending_evaluation();
        cancel_post_baseline_observation();
        cancel_cycle_1_selection_gate();
        const auto convergence = session_.convergence_status();
        if (convergence.has_value() && !convergence_failure_emitted_)
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
        refresh_cycle_audit();
        static_cast<void>(session_.finalize_failed_cycle(
            AdaptiveV2ManagerCycleTerminalReason::caller_failed));
        emit_new_session_terminals();
        failed_ = true;
        HOTSTUFF_LOG_WARN(
            "KAURI_ADAPTIVE_MANAGER fatal reason=%s", reason);
        event_context_.stop();
    }

    void stop_runtime() noexcept
    {
        event_context_.stop();
        cancel_pending_evaluation();
        convergence_timer.del();
        convergence_ack_drain_timer.del();
        predecessor_residency_timer.del();
        predecessor_residency_pending_ = false;
        cancel_post_baseline_observation();
        cancel_cycle_1_selection_gate();
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
        if (!session_stopped_)
        {
            session_stopped_ = true;
            session_.shutdown();
            emit_new_session_terminals();
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

    void emit_new_accepted_observations() noexcept
    {
        const auto &records =
            session_.ingress().ledger().accepted();
        if (emitted_accepted_observations_ > records.size())
        {
            fail("accepted_observation_cursor_regression");
            return;
        }
        while (emitted_accepted_observations_ < records.size())
        {
            structured_event_sink_.drain();
            if (!structured_event_sink_.health().healthy)
            {
                fail("structured_event_observation_drain_failed");
                return;
            }
            const auto &record =
                records[emitted_accepted_observations_];
            const hotstuff::AuditStructuredEventPayload event{
                hotstuff::EvidenceObservationAcceptedStructuredEvent{
                    record}};
            structured_event_sink_.emit_audit(event);
            if (!structured_event_sink_.health().healthy)
            {
                fail("structured_event_observation_emit_failed");
                return;
            }
            ++emitted_accepted_observations_;
        }
        structured_event_sink_.drain();
        if (!structured_event_sink_.health().healthy)
        {
            fail("structured_event_observation_final_drain_failed");
        }
    }

    void emit_new_score_trajectory() noexcept
    {
        const auto audit = session_.controller_audit();
        if (!audit.has_value())
            return;
        const auto &trajectory = audit->score_trajectory;
        const auto evidence_cutoff = audit->current_cutoff;
        while (emitted_score_trajectory_ < trajectory.size())
        {
            structured_event_sink_.drain();
            if (!structured_event_sink_.health().healthy)
            {
                fail("structured_event_reputation_drain_failed");
                return;
            }
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
        structured_event_sink_.drain();
        if (!structured_event_sink_.health().healthy)
        {
            fail("structured_event_reputation_final_drain_failed");
            return;
        }
    }

    void cancel_pending_evaluation() noexcept
    {
        evaluation_timer.del();
        evaluation_timer_pending_ = false;
    }

    void cancel_post_baseline_observation() noexcept
    {
        post_baseline_observation_timer.del();
        post_baseline_observation_pending_ = false;
    }

    void schedule_evaluation() noexcept
    {
        if (failed_ || request_sequence_.shutdown_eligible() ||
            predecessor_residency_pending_ ||
            post_baseline_observation_pending_ ||
            cycle_1_selection_gate_pending_ ||
            session_.convergence_status().has_value() ||
            evaluation_timer_pending_)
        {
            return;
        }

        const auto readiness =
            session_.ingress().readiness_stats();
        const auto evidence_cutoff =
            session_.ingress().ledger().high_watermark();
        if (last_evaluated_ready_members_.has_value() &&
            last_evaluated_evidence_cutoff_.has_value() &&
            *last_evaluated_ready_members_ == readiness.ready_members &&
            *last_evaluated_evidence_cutoff_ == evidence_cutoff)
        {
            return;
        }

        try
        {
            evaluation_timer_pending_ = true;
            evaluation_timer.add(kEvaluationCoalescingSeconds);
        }
        catch (...)
        {
            evaluation_timer_pending_ = false;
            fail("manager_evaluation_timer_schedule_failed");
        }
    }

    void handle_evaluation_timer() noexcept
    {
        if (!evaluation_timer_pending_)
            return;
        evaluation_timer_pending_ = false;
        try
        {
            evaluate();
        }
        catch (...)
        {
            fail("manager_evaluation_timer_failed");
        }
    }

    bool fault_containment_coverage_ready(
        const TransitionRequest &request,
        std::uint64_t evidence_cutoff,
        bool emit_ready_event) noexcept
    {
        if (options_
                .fault_containment_evidence_start_monotonic_ns == 0 ||
            request.policy.intent != TreePolicyKind::fault_containment ||
            request_sequence_.cursor() != 0 ||
            request.predecessor_epoch_number != 0)
        {
            return true;
        }
        if (cycle_audits_.empty() ||
            request_sequence_.cursor() != cycle_audits_.size() - 1)
        {
            fail("fault_containment_coverage_has_no_cycle");
            return false;
        }
        auto &cycle = cycle_audits_.back();
        if (cycle.fault_containment_coverage_ready_emitted)
            return true;

        try
        {
            const auto &epoch = session_.ingress().current_epoch();
            std::vector<std::uint32_t> required_tree_ids;
            required_tree_ids.reserve(epoch.trees().size());
            for (const auto &tree : epoch.trees())
                required_tree_ids.push_back(tree.tree_id);
            std::sort(
                required_tree_ids.begin(), required_tree_ids.end());
            if (required_tree_ids.size() !=
                    options_
                        .fault_containment_required_tree_coverage ||
                std::adjacent_find(
                    required_tree_ids.begin(),
                    required_tree_ids.end()) !=
                    required_tree_ids.end())
            {
                fail("fault_containment_coverage_tree_set_mismatch");
                return false;
            }

            const auto coverage = hotstuff::
                evaluate_adaptive_v2_fault_containment_coverage(
                    session_.ingress().ledger().accepted(),
                    hotstuff::AdaptationEpochId{
                        epoch.epoch_number(), epoch.epoch_digest()},
                    evidence_cutoff,
                    options_
                        .fault_containment_evidence_start_monotonic_ns,
                    required_tree_ids);
            if (coverage.status ==
                hotstuff::AdaptiveV2FaultContainmentCoverageStatus::
                    incomplete)
            {
                return false;
            }
            if (coverage.status !=
                hotstuff::AdaptiveV2FaultContainmentCoverageStatus::ready)
            {
                fail("fault_containment_coverage_invalid");
                return false;
            }

            if (!emit_ready_event)
                return true;

            structured_event_sink_.drain();
            if (!structured_event_sink_.health().healthy)
            {
                fail("fault_containment_coverage_drain_failed");
                return false;
            }
            structured_event_sink_.emit_audit(
                hotstuff::AuditStructuredEventPayload{
                    hotstuff::
                        AdaptiveV2FaultContainmentCoverageReadyStructuredEvent{
                            static_cast<std::uint64_t>(
                                request_sequence_.cursor()),
                            request.transition_artifact_id,
                            epoch.epoch_number(),
                            epoch.epoch_digest(),
                            coverage
                                .fault_evidence_start_monotonic_ns,
                            coverage.evidence_cutoff,
                            coverage.required_tree_ids,
                            coverage.observed_tree_ids}});
            structured_event_sink_.drain();
            if (!structured_event_sink_.health().healthy)
            {
                fail("fault_containment_coverage_emit_failed");
                return false;
            }
            cycle.fault_containment_coverage_ready_emitted = true;
            return true;
        }
        catch (...)
        {
            fail("fault_containment_coverage_internal_failure");
            return false;
        }
    }

    void evaluate()
    {
        if (failed_ || request_sequence_.shutdown_eligible() ||
            predecessor_residency_pending_ ||
            post_baseline_observation_pending_)
            return;
        if (session_.convergence_status().has_value())
        {
            cancel_pending_evaluation();
            return;
        }
        const auto readiness =
            session_.ingress().readiness_stats();
        const auto evidence_cutoff =
            session_.ingress().ledger().high_watermark();
        last_evaluated_ready_members_ = readiness.ready_members;
        last_evaluated_evidence_cutoff_ = evidence_cutoff;
        refresh_cycle_audit();
        const auto *request = current_transition_request();
        const auto controller = session_.controller_audit();
        if (request == nullptr)
        {
            fail("missing_transition_request_before_evaluation");
            return;
        }
        if (controller.has_value() && controller->baseline_frozen &&
            !fault_containment_coverage_ready(
                *request, evidence_cutoff, false))
        {
            return;
        }
        if (controller.has_value() && controller->baseline_frozen &&
            !cycle_1_selection_gate_ready())
        {
            return;
        }
        const auto status = session_.evaluate();
        refresh_cycle_audit();
        if ((status ==
                 AdaptiveV2ManagerControllerStatus::successor_ready ||
             status ==
                 AdaptiveV2ManagerControllerStatus::already_ready) &&
            !fault_containment_coverage_ready(
                *request, evidence_cutoff, true))
        {
            return;
        }
        emit_new_score_trajectory();
        if (failed_)
            return;
        HOTSTUFF_LOG_INFO(
            "KAURI_ADAPTIVE_MANAGER state=%s cutoff=%llu",
            controller_status_name(status),
            static_cast<unsigned long long>(
                cycle_audits_.empty()
                    ? 0
                    : cycle_audits_.back().current_evidence_cutoff));
        if (status == AdaptiveV2ManagerControllerStatus::unhealthy)
        {
            emit_new_session_terminals();
            fail("controller_unhealthy");
            return;
        }
        if (status ==
            AdaptiveV2ManagerControllerStatus::baseline_frozen)
        {
            const auto *request = current_transition_request();
            if (request == nullptr ||
                !schedule_post_baseline_observation(*request))
            {
                fail("post_baseline_observation_schedule_failed");
            }
            return;
        }
        if (status != AdaptiveV2ManagerControllerStatus::successor_ready &&
            status != AdaptiveV2ManagerControllerStatus::already_ready)
            return;

        request = current_transition_request();
        const auto *bundle = session_.successor_bundle();
        if (request == nullptr || bundle == nullptr)
        {
            fail("missing_successor_bundle");
            return;
        }
        try
        {
            cancel_pending_evaluation();
            const auto output_path = transition_bundle_output_path(
                *request, *bundle, session_);
            write_exclusive_bundle(
                output_path, bundle->canonical_bytes());
            emit_shape_decision(*request, *bundle);
            emit_evidence_snapshot(*request, *bundle);
            if (!session_.start_convergence(convergence_tick_))
                throw std::runtime_error(
                    "manager session rejected convergence start");
            const auto &payload = bundle->command().payload;
            HOTSTUFF_LOG_INFO(
                "KAURI_ADAPTIVE_MANAGER convergence_started "
                "epoch=%u digest=%s bytes=%zu required=%u",
                payload.successor_epoch_number,
                payload.successor_epoch_digest.to_hex().c_str(),
                bundle->canonical_bytes().size(),
                options_.runtime_shape.quorum.quorum);
            drive_convergence();
        }
        catch (const std::exception &error)
        {
            HOTSTUFF_LOG_WARN(
                "KAURI_ADAPTIVE_MANAGER distribution_error=%s",
                error.what());
            emit_new_session_terminals();
            fail("successor_distribution_failed");
        }
    }

    void handle_convergence_status() noexcept
    {
        if (failed_)
            return;
        const auto status_value = session_.convergence_status();
        if (!status_value.has_value())
        {
            const auto &records = session_.terminal_records();
            if (records.size() > emitted_session_terminals_)
            {
                const auto &terminal =
                    records[emitted_session_terminals_];
                const char *failure_reason = nullptr;
                if (terminal.reason ==
                    AdaptiveV2ManagerCycleTerminalReason::
                        convergence_retry_exhausted)
                {
                    failure_reason = "convergence_retry_exhausted";
                }
                else if (terminal.reason ==
                    AdaptiveV2ManagerCycleTerminalReason::
                        convergence_conflicting_observation)
                {
                    failure_reason =
                        "convergence_conflicting_observation";
                }
                if (failure_reason != nullptr &&
                    !convergence_failure_emitted_)
                {
                    convergence_failure_emitted_ = true;
                    emit_convergence_event(
                        hotstuff::AdaptiveV2ConvergenceTransition::failure,
                        std::nullopt,
                        0,
                        std::nullopt,
                        "",
                        failure_reason);
                }
                emit_new_session_terminals();
                fail("convergence_terminal_failure");
            }
            return;
        }

        const auto status = *status_value;
        if (status ==
            AdaptiveV2ManagerConvergenceStatus::awaiting_activations)
        {
            return;
        }
        if (status ==
            AdaptiveV2ManagerConvergenceStatus::ready_for_optimization)
        {
            const auto convergence = session_.convergence_audit();
            if (!convergence.has_value() ||
                !convergence->winning_identity.has_value())
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
                *convergence->winning_identity);
            if (failed_)
                return;
            emit_convergence_event(
                hotstuff::AdaptiveV2ConvergenceTransition::ready,
                std::nullopt,
                0,
                *convergence->winning_identity);
            begin_convergence_ack_drain();
            return;
        }
        if (status ==
            AdaptiveV2ManagerConvergenceStatus::retry_exhausted)
        {
            static_cast<void>(session_.finalize_failed_cycle(
                AdaptiveV2ManagerCycleTerminalReason::
                    convergence_retry_exhausted));
            emit_new_session_terminals();
            fail("convergence_retry_exhausted");
            return;
        }
        if (status ==
            AdaptiveV2ManagerConvergenceStatus::conflicting_observation)
        {
            static_cast<void>(session_.finalize_failed_cycle(
                AdaptiveV2ManagerCycleTerminalReason::
                    convergence_conflicting_observation));
            emit_new_session_terminals();
            fail("convergence_conflicting_observation");
        }
    }

    void begin_convergence_ack_drain() noexcept
    {
        cancel_pending_evaluation();
        convergence_timer.del();
        convergence_ack_drain_timer.del();
        refresh_cycle_audit();
        if (!session_.consume_ready_and_rotate())
        {
            emit_new_session_terminals();
            fail("convergence_ready_consumption_failed");
            return;
        }
        emitted_accepted_observations_ = 0;
        emit_new_session_terminals();
        const auto previous_cursor = request_sequence_.cursor();
        if (!request_sequence_.observe_terminal_records(
                session_.terminal_records()) ||
            request_sequence_.cursor() == previous_cursor)
        {
            fail("transition_request_did_not_advance");
            return;
        }
        if (request_sequence_.shutdown_eligible())
        {
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
            return;
        }

        if (!schedule_current_predecessor_residency())
        {
            if (!failed_)
                fail("predecessor_residency_schedule_failed");
            return;
        }
    }

    void drive_convergence() noexcept
    {
        if (failed_ || !session_.convergence_status().has_value())
        {
            return;
        }

        try
        {
            const auto requests =
                session_.due_deliveries(convergence_tick_);
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

                const auto recorded = session_.record_enqueue_result(
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
            const auto status = session_.convergence_status();
            if (!failed_ && status.has_value() &&
                *status ==
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
        const auto &ingress = session_.ingress();
        const auto audit = ingress.audit_stats();
        const auto lifecycle = ingress.lifecycle_stats();
        const auto &ledger = ingress.ledger();
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
        if (request_sequence_.shutdown_eligible())
            return;
        const auto source = authenticated_source(connection);
        if (!source.has_value())
            return;
        const auto result = operation(
            AuthenticatedReporter{*source}, message);
        emit_new_accepted_observations();
        if (failed_)
            return;
        if (result.status ==
                AdaptiveV2ManagerIngressStatus::evidence_unhealthy ||
            result.status == AdaptiveV2ManagerIngressStatus::stopped)
        {
            log_ingress_failure(
                message_kind, result.status, *source);
            fail("manager_ingress_unhealthy");
            return;
        }
        schedule_evaluation();
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
                        return session_.ingest_readiness(source, value);
                    });
            });
        network_.reg_handler(
            [this](MsgProposalLifecycleNotice &&message,
                   const ManagerNetwork::conn_t &connection) {
                ingest("lifecycle",
                    std::move(message), connection,
                    [this](const AuthenticatedReporter &source,
                           const MsgProposalLifecycleNotice &value) {
                        return session_.ingest_lifecycle(source, value);
                    });
            });
        network_.reg_handler(
            [this](MsgEvidenceReport &&message,
                   const ManagerNetwork::conn_t &connection) {
                ingest("evidence",
                    std::move(message), connection,
                    [this](const AuthenticatedReporter &source,
                           const MsgEvidenceReport &value) {
                        return session_.ingest_evidence(source, value);
                    });
            });
        network_.reg_handler(
            [this](
                MsgAdaptiveV2EpochChangeCommittedObservation &&message,
                const ManagerNetwork::conn_t &connection) {
                if (failed_)
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
                const auto disposition = session_.observe_commit(
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
                if (failed_)
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
                const auto disposition = session_.observe_activation(
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
                    if (disposition ==
                        AdaptiveV2ManagerConvergenceDisposition::accepted)
                    {
                        ++accepted_activation_ack_ordinal_;
                        acknowledgement_injected_drop =
                            options_.experiment_drop_activation_ack.has_value() &&
                            !experiment_activation_ack_drop_consumed_ &&
                            *options_.experiment_drop_activation_ack ==
                                accepted_activation_ack_ordinal_ &&
                            session_.convergence_status() ==
                                std::optional<
                                    AdaptiveV2ManagerConvergenceStatus>{
                                    AdaptiveV2ManagerConvergenceStatus::
                                        ready_for_optimization};
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
    hotstuff::StructuredEventClock &monotonic_raw_clock_;
    ManagerOptions options_;
    ManagerNetwork network_;
    std::unordered_map<PeerId, ReplicaID> peer_to_replica_;
    AdaptiveV2ManagerSession session_;
    AdaptiveV2ManagerRequestSequence request_sequence_;
    hotstuff::StructuredEventSink &structured_event_sink_;
    hotstuff::AdaptiveV2ConvergenceWireLimits
        convergence_wire_limits_;
    salticidae::TimerEvent convergence_timer;
    salticidae::TimerEvent convergence_ack_drain_timer;
    salticidae::TimerEvent predecessor_residency_timer;
    salticidae::TimerEvent post_baseline_observation_timer;
    salticidae::TimerEvent cycle_1_selection_gate_timer;
    salticidae::TimerEvent evaluation_timer;
    std::chrono::steady_clock::time_point
        predecessor_residency_deadline_{};
    std::chrono::steady_clock::time_point
        post_baseline_observation_deadline_{};
    std::uint64_t convergence_tick_{0};
    std::optional<std::size_t> last_evaluated_ready_members_;
    std::optional<std::uint64_t> last_evaluated_evidence_cutoff_;
    std::size_t emitted_score_trajectory_{0};
    std::size_t emitted_accepted_observations_{0};
    std::size_t emitted_session_terminals_{0};
    std::vector<CycleAuditContext> cycle_audits_;
    std::uint32_t accepted_activation_ack_ordinal_{0};
    bool network_stop_required_{false};
    bool network_stopped_{false};
    bool session_stopped_{false};
    bool convergence_failure_emitted_{false};
    bool predecessor_residency_pending_{false};
    bool post_baseline_observation_pending_{false};
    bool cycle_1_selection_gate_pending_{false};
    bool evaluation_timer_pending_{false};
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
            structured_event_clock,
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
