#include "catch.hpp"

#include <algorithm>
#include <cctype>
#include <cstdlib>
#include <filesystem>
#include <fstream>
#include <limits>
#include <stdexcept>
#include <string>
#include <utility>
#include <vector>

#include "hotstuff/structured_event.h"
#include "support/adaptive_v3_manager_session_fixture.h"
#include "support/subprocess.h"

#ifndef KAURI_EPOCH_PROFILE_DIGEST_PATH
#error "KAURI_EPOCH_PROFILE_DIGEST_PATH must name epoch-profile-digest"
#endif

namespace {

using namespace hotstuff;
using namespace kauri::test_support::cert13;
using kauri::test_support::run_program;

struct ExportRoot {
    std::filesystem::path path;
    bool temporary{false};

    ExportRoot()
    {
        if (const auto *configured = std::getenv("KAURI_CERT13_EXPORT_DIR");
            configured != nullptr && *configured != '\0') {
            path = configured;
            std::filesystem::create_directories(path);
            return;
        }
        std::string template_path = "/tmp/kauri-cert13-v13-export-XXXXXX";
        std::vector<char> writable(template_path.begin(), template_path.end());
        writable.push_back('\0');
        const auto *created = ::mkdtemp(writable.data());
        if (created == nullptr) throw std::runtime_error("failed to create exporter directory");
        path = created;
        temporary = true;
    }

    ~ExportRoot()
    {
        if (temporary) std::filesystem::remove_all(path);
    }
};

void write_bytes(const std::filesystem::path &path, const bytearray_t &contents)
{
    std::filesystem::create_directories(path.parent_path());
    std::ofstream output(path, std::ios::binary | std::ios::trunc);
    if (!output) throw std::runtime_error("failed to open export artifact");
    output.write(reinterpret_cast<const char *>(contents.data()),
                 static_cast<std::streamsize>(contents.size()));
    if (!output) throw std::runtime_error("failed to write export artifact");
}

void write_text(const std::filesystem::path &path, const std::string &contents)
{
    write_bytes(path, bytearray_t(contents.begin(), contents.end()));
}

std::string hex_line(const bytearray_t &bytes) { return get_hex(bytes) + "\n"; }

std::size_t event_count(const std::string &contents, const std::string &event_type)
{
    const auto needle = "\"event_type\":\"" + event_type + "\"";
    std::size_t count = 0;
    std::size_t offset = 0;
    while ((offset = contents.find(needle, offset)) != std::string::npos) {
        ++count;
        offset += needle.size();
    }
    return count;
}

bytearray_t text_bytes(const std::string &text)
{
    return {text.begin(), text.end()};
}

std::string fixture_environment(const char *name, const std::string &fallback)
{
    const auto *value = std::getenv(name);
    return value == nullptr || *value == '\0' ? fallback : value;
}

std::string fixture_run_id()
{
    return fixture_environment(
        "KAURI_CERT13_FIXTURE_RUN_ID", "cert13-v13-public-fixture");
}

std::string fixture_source_instance(const std::string &logical_id)
{
    std::string name = "KAURI_CERT13_FIXTURE_SOURCE_INSTANCE_";
    for (const auto character : logical_id)
        name.push_back(static_cast<char>(
            std::isalnum(static_cast<unsigned char>(character))
                ? std::toupper(static_cast<unsigned char>(character))
                : '_'));
    return fixture_environment(name.c_str(), logical_id + "-fixture");
}

std::string fixture_digest(const char *name, const std::string &fallback_seed)
{
    const auto fallback = digest(fallback_seed).to_hex();
    const auto value = fixture_environment(name, fallback);
    REQUIRE(value.size() == 64);
    REQUIRE(std::all_of(value.begin(), value.end(), [](const char character) {
        return (character >= '0' && character <= '9') ||
               (character >= 'a' && character <= 'f');
    }));
    return value;
}

std::string readiness_manifest(const BlsMembership &membership)
{
    const auto digest = canonical_activation_readiness_membership_digest(
        membership.public_keys);
    std::string result =
        "{\"algorithm\":\"bls-pop\",\"domain\":\"kauri-adaptive-v3-readiness-public-key-manifest-v1\",\"members\":[";
    for (std::size_t index = 0; index < membership.public_keys.size(); ++index) {
        if (index != 0) result += ',';
        const auto &member = membership.public_keys.at(index);
        result += "{\"public_key_hex\":\"" + get_hex(member.second) +
                  "\",\"replica_id\":" + std::to_string(member.first) + '}';
    }
    return result +
        "],\"membership_digest\":\"" + digest.to_hex() +
        "\",\"profile_id\":\"n7-f2-q5-two-crash-pair-smoke-v13\",\"profile_sha256\":\"" +
        "3aa61c80d1c777db1469658532c978afc6fb52291cc6859cb5a889810541bd03" +
        "\",\"protocol_mode\":\"adaptive_v3\",\"schema_version\":1}\n";
}

struct ArtifactFile {
    std::string relative_path;
    bytearray_t contents;
};

struct SelectionArtifacts {
    std::vector<AcceptedEvidenceRecord> accepted_records;
    AdaptiveV2EvidenceSnapshotStructuredEvent snapshot;
    std::optional<FaultWindowArmedStructuredEvent> fault_window_arm;
};

SelectionArtifacts capture_selection(
    const Fixture &fixture,
    std::uint64_t cycle_ordinal,
    TreePolicyKind intent,
    const std::string &transition_artifact_id,
    const std::optional<AdaptiveV2FaultWindowArm> &fault_window_arm)
{
    const auto controller = fixture.session.controller_audit();
    const auto *bundle = fixture.session.successor_bundle();
    REQUIRE(controller.has_value());
    REQUIRE(controller->baseline_frozen);
    REQUIRE(bundle != nullptr);
    const auto &ingress = fixture.session.ingress();
    const auto &ledger = ingress.ledger();
    const auto &definition = bundle->definition();
    REQUIRE(controller->baseline_cutoff != 0);
    REQUIRE(controller->current_cutoff > controller->baseline_cutoff);
    REQUIRE(ledger.high_watermark() == controller->current_cutoff);
    REQUIRE(definition.evidence_cutoff == controller->current_cutoff);

    SelectionArtifacts result;
    result.accepted_records = ledger.accepted();
    result.snapshot.cycle_ordinal = cycle_ordinal;
    result.snapshot.policy_intent = intent;
    result.snapshot.transition_artifact_id = transition_artifact_id;
    result.snapshot.predecessor_epoch_number =
        ingress.current_epoch().epoch_number();
    result.snapshot.predecessor_epoch_digest =
        ingress.current_epoch().epoch_digest();
    result.snapshot.activation_generation = ingress.activation_generation();
    result.snapshot.baseline_cutoff = controller->baseline_cutoff;
    result.snapshot.current_cutoff = controller->current_cutoff;

    std::size_t accepted_prefix_count = 0;
    for (const auto &record : result.accepted_records) {
        if (record.ingestion_sequence <= result.snapshot.current_cutoff)
            ++accepted_prefix_count;
    }
    REQUIRE(accepted_prefix_count != 0);
    const AcceptedEvidenceView accepted_prefix{
        result.accepted_records.data(), accepted_prefix_count};
    const auto full_prefix = build_adaptation_snapshot(
        fixture.replicas,
        AdaptationEpochId{result.snapshot.predecessor_epoch_number,
                          result.snapshot.predecessor_epoch_digest},
        accepted_prefix,
        result.snapshot.current_cutoff,
        fixture.config.controller.selection.responsiveness_policy,
        kSeed);
    REQUIRE(full_prefix.accepted_record_count() == accepted_prefix_count);
    result.snapshot.full_prefix_snapshot_id = uint256_t(
        hotstuff::from_hex(full_prefix.snapshot_id()));
    result.snapshot.evidence_snapshot_id = uint256_t(
        hotstuff::from_hex(definition.evidence_snapshot_id));
    result.snapshot.accepted_prefix_count = accepted_prefix_count;
    for (const auto &tree : definition.trees) {
        REQUIRE(!tree.members_breadth_first.empty());
        result.snapshot.eligible_ranking.push_back(
            tree.members_breadth_first.front());
    }

    if (fault_window_arm.has_value()) {
        FaultWindowArmedStructuredEvent event;
        event.schema_version = 4;
        event.kind = "kauri-focused-fault-window-arm-v4";
        event.run_id = fixture_run_id();
        event.profile_id = "n7-f2-q5-two-crash-pair-smoke-v13";
        event.profile_sha256 =
            "3aa61c80d1c777db1469658532c978afc6fb52291cc6859cb5a889810541bd03";
        event.topology_proof_sha256 =
            "8a43f845b41b8813df0c689adc1042baa4457af1c6061ad6db677434e5d4b99d";
        event.request_sha256 = fixture_digest(
            "KAURI_CERT13_FIXTURE_PARENT_REQUEST_SHA256",
            "fixture-parent-authorization");
        event.epoch_number = fault_window_arm->predecessor_epoch_number;
        event.epoch_digest = fault_window_arm->predecessor_epoch_digest;
        event.fault_receipt_sha256 = fixture_digest(
            "KAURI_CERT13_FIXTURE_FAULT_RECEIPT_SHA256",
            "fixture-fault-receipt");
        event.evidence_start_monotonic_ns =
            fault_window_arm->evidence_start_monotonic_ns;
        event.prefault_tree_id = fault_window_arm->prefault_tree_id;
        event.required_tree_positions =
            static_cast<std::uint32_t>(fault_window_arm->required_tree_ids.size());
        event.required_tree_ids = fault_window_arm->required_tree_ids;
        event.clock_domain = "same_host_clock_monotonic_raw";
        event.required_observation_schema = 3;
        event.timeout_evidence_basis = "exact_timeout_attempt_id_v1";
        event.snapshot_evidence_basis = "exact_post_fault_attempt_start_v1";
        event.selection_cardinality_policy =
            "all_guarded_up_to_fault_bound_v1";
        event.fault_window_arm_sha256 = fixture_digest(
            "KAURI_CERT13_FIXTURE_FAULT_WINDOW_ARM_SHA256",
            "fixture-fault-window-arm");
        result.fault_window_arm = std::move(event);
    }
    return result;
}

void assert_public(const ArtifactFile &file)
{
    const std::string contents(file.contents.begin(), file.contents.end());
    REQUIRE(contents.find("private") == std::string::npos);
    REQUIRE(contents.find("secret") == std::string::npos);
    REQUIRE(contents.find("scalar") == std::string::npos);
    REQUIRE(contents.find("tls") == std::string::npos);
}

void add_cycle_files(std::vector<ArtifactFile> &files,
                     const std::string &directory,
                     const std::string &epoch,
                     const CompletedReadinessArtifacts &artifacts)
{
    files.push_back({directory + "/raw/" + epoch + ".bundle", artifacts.bundle_canonical_bytes});
    const auto identity = hex_line(artifacts.identity_bytes);
    const auto certificate = hex_line(artifacts.certificate_bytes);
    files.push_back({directory + "/" + epoch + ".identity.hex",
                     text_bytes(identity)});
    files.push_back({directory + "/" + epoch + ".certificate.hex",
                     text_bytes(certificate)});
}

void verify_cycle(const std::filesystem::path &root,
                  const std::filesystem::path &manifest,
                  const std::string &directory,
                  const std::string &epoch,
    const CompletedReadinessArtifacts &artifacts)
{
    const auto certificate = root / directory / (epoch + ".certificate.hex");
    const auto identity = root / directory / (epoch + ".identity.hex");
    const auto result = run_program(
        KAURI_EPOCH_PROFILE_DIGEST_PATH,
        {"--verify-adaptive-v3-readiness-v1",
         manifest.string(), certificate.string(), identity.string()});
    REQUIRE(result.status == 0);
    REQUIRE(result.error.empty());
    REQUIRE(result.output.find("\"valid\":true") != std::string::npos);
    REQUIRE(result.output.find("\"certificate_digest\":\"" +
        artifacts.certificate_digest.to_hex() + "\"") != std::string::npos);
}

class DeterministicRawClock final : public StructuredEventClock {
public:
    explicit DeterministicRawClock(std::vector<std::uint64_t> ticks)
        : ticks_(std::move(ticks)) { REQUIRE(!ticks_.empty()); }
    std::uint64_t now_ns() noexcept override
    {
        const auto tick = ticks_.at(index_);
        if (index_ + 1 < ticks_.size()) ++index_;
        return tick;
    }
private:
    std::vector<std::uint64_t> ticks_;
    std::size_t index_{0};
};

std::uint64_t delivery_tick_for(const CompletedReadinessArtifacts &artifacts,
                                ReplicaID replica)
{
    const auto acknowledgement = std::find_if(artifacts.acknowledgement_payloads.begin(),
        artifacts.acknowledgement_payloads.end(), [replica](const auto &payload) {
            const auto decoded = decode_activation_readiness_ack(
                payload, ActivationReadinessWireLimits{64 * 1024, 7});
            return decoded && decoded.value->recipient_replica_id == replica;
        });
    REQUIRE(acknowledgement != artifacts.acknowledgement_payloads.end());
    const auto ordinal = static_cast<std::uint64_t>(std::distance(
        artifacts.acknowledgement_payloads.begin(), acknowledgement));
    const auto count = artifacts.acknowledgement_payloads.size();
    REQUIRE(count != 0);
    const auto increment = count == 1 ? 0 :
        (artifacts.final_ack_tick - artifacts.first_delivery_tick) / (count - 1);
    return artifacts.first_delivery_tick + ordinal * increment;
}

void append_manager_cycle_ticks(std::vector<std::uint64_t> &ticks,
                                const CompletedReadinessArtifacts &artifacts)
{
    std::uint64_t max_signer_tick = artifacts.certificate_tick;
    for (const auto &observation : artifacts.observations) {
        const auto tick = std::max(artifacts.readiness_tick,
                                   observation.signer_monotonic_raw_ns);
        ticks.push_back(tick);
        max_signer_tick = std::max(max_signer_tick, observation.signer_monotonic_raw_ns);
    }
    ticks.push_back(std::max(artifacts.certificate_tick, max_signer_tick));
    const auto count = artifacts.acknowledgement_payloads.size();
    const auto increment = count <= 1 ? 0 :
        (artifacts.final_ack_tick - artifacts.first_delivery_tick) / (count - 1);
    for (std::size_t index = 0; index < count; ++index) {
        const auto tick = artifacts.first_delivery_tick + index * increment;
        ticks.push_back(tick);
        ticks.push_back(tick);
    }
}

void append_manager_selection_ticks(
    std::vector<std::uint64_t> &ticks,
    const SelectionArtifacts &selection)
{
    std::uint64_t last = ticks.empty() ? 0 : ticks.back();
    auto append_record = [&](const AcceptedEvidenceRecord &record) {
        last = std::max(last, record.observation.reporter_monotonic_ns);
        ticks.push_back(last);
    };
    for (const auto &record : selection.accepted_records)
        if (record.ingestion_sequence <= selection.snapshot.baseline_cutoff)
            append_record(record);
    if (selection.fault_window_arm.has_value()) {
        constexpr std::uint64_t configuration_coverage_ns = 6'000'000'000ULL;
        REQUIRE(selection.fault_window_arm->evidence_start_monotonic_ns <=
                std::numeric_limits<std::uint64_t>::max() -
                    configuration_coverage_ns);
        last = std::max(
            last,
            selection.fault_window_arm->evidence_start_monotonic_ns +
                configuration_coverage_ns);
        ticks.push_back(last);
    }
    for (const auto &record : selection.accepted_records)
        if (record.ingestion_sequence > selection.snapshot.baseline_cutoff)
            append_record(record);
    REQUIRE(last != std::numeric_limits<std::uint64_t>::max());
    ticks.push_back(last + 1U);
}

void emit_manager_selection(
    StructuredEventSink &sink,
    const SelectionArtifacts &selection)
{
    for (const auto &record : selection.accepted_records)
        if (record.ingestion_sequence <= selection.snapshot.baseline_cutoff)
            sink.emit_audit(AuditStructuredEventPayload{
                EvidenceObservationAcceptedStructuredEvent{record}});
    if (selection.fault_window_arm.has_value())
        sink.emit_audit(AuditStructuredEventPayload{
            *selection.fault_window_arm});
    for (const auto &record : selection.accepted_records)
        if (record.ingestion_sequence > selection.snapshot.baseline_cutoff)
            sink.emit_audit(AuditStructuredEventPayload{
                EvidenceObservationAcceptedStructuredEvent{record}});
    sink.emit_audit(AuditStructuredEventPayload{selection.snapshot});
}

bytearray_t read_bytes(const std::filesystem::path &path)
{
    std::ifstream input(path, std::ios::binary);
    if (!input) throw std::runtime_error("failed to open structured event export");
    return {std::istreambuf_iterator<char>(input), std::istreambuf_iterator<char>()};
}

StructuredEventConfig event_config(StructuredEventSourceKind kind,
                                   const std::string &logical_id)
{
    return {fixture_run_id(),
            StructuredEventSource{kind, logical_id,
                                  fixture_source_instance(logical_id)},
            std::nullopt, StructuredEventLimits{}};
}

void emit_manager_cycle(StructuredEventSink &sink,
                        const CompletedReadinessArtifacts &artifacts)
{
    const auto identity = decode_activation_ready_identity_v1(
        artifacts.identity_bytes, ActivationReadinessWireLimits{64 * 1024, 7});
    const auto certificate = decode_activation_readiness_certificate(
        artifacts.certificate_bytes, ActivationReadinessWireLimits{64 * 1024, 7});
    REQUIRE(identity);
    REQUIRE(certificate);
    for (std::size_t index = 0; index < artifacts.observations.size(); ++index) {
        const auto &observation = artifacts.observations.at(index);
        AdaptiveV3ReadinessStructuredEvent event;
        event.transition = AdaptiveV3ReadinessTransition::observation_accepted;
        event.identity = *identity.value;
        event.replica_id = observation.signer_replica_id;
        event.signer_source_sequence = observation.signer_source_sequence;
        event.signer_monotonic_raw_ns = observation.signer_monotonic_raw_ns;
        event.observation_digest = activation_ready_observation_digest(observation);
        event.canonical_wire_payload = artifacts.observation_payloads.at(index);
        event.disposition = index + 1 == artifacts.observations.size() ? "released" : "accepted";
        sink.emit_audit(AuditStructuredEventPayload{std::move(event)});
    }
    std::vector<ReplicaID> signers;
    for (const auto &observation : certificate.value->observations)
        signers.push_back(observation.signer_replica_id);
    const auto certificate_payload_digest = activation_readiness_ack_payload_digest(
        MsgActivationReadinessCertificate::opcode, artifacts.certificate_bytes);
    AdaptiveV3ReadinessStructuredEvent assembled;
    assembled.transition = AdaptiveV3ReadinessTransition::certificate_assembled;
    assembled.identity = *identity.value;
    assembled.certificate_digest = artifacts.certificate_digest;
    assembled.payload_digest = certificate_payload_digest;
    assembled.observed_signers = signers;
    assembled.required_release_count = signers.size();
    assembled.canonical_wire_payload = artifacts.certificate_bytes;
    sink.emit_audit(AuditStructuredEventPayload{std::move(assembled)});
    for (std::size_t index = 0; index < artifacts.acknowledgement_payloads.size(); ++index) {
        const auto acknowledgement = decode_activation_readiness_ack(
            artifacts.acknowledgement_payloads.at(index),
            ActivationReadinessWireLimits{64 * 1024, 7});
        REQUIRE(acknowledgement);
        AdaptiveV3ReadinessStructuredEvent delivery;
        delivery.transition = AdaptiveV3ReadinessTransition::certificate_delivery;
        delivery.identity = *identity.value;
        delivery.replica_id = acknowledgement.value->recipient_replica_id;
        delivery.certificate_digest = artifacts.certificate_digest;
        delivery.payload_digest = certificate_payload_digest;
        delivery.delivery_attempt = 1;
        delivery.delivery_enqueued = true;
        delivery.canonical_wire_payload = artifacts.certificate_bytes;
        delivery.disposition = "queued";
        sink.emit_audit(AuditStructuredEventPayload{std::move(delivery)});

        AdaptiveV3ReadinessStructuredEvent acknowledged;
        acknowledged.transition = AdaptiveV3ReadinessTransition::certificate_acknowledged;
        acknowledged.identity = *identity.value;
        acknowledged.replica_id = acknowledgement.value->recipient_replica_id;
        acknowledged.certificate_digest = artifacts.certificate_digest;
        acknowledged.payload_digest = acknowledgement.value->payload_digest;
        acknowledged.canonical_wire_payload = artifacts.acknowledgement_payloads.at(index);
        acknowledged.disposition = "acknowledged";
        sink.emit_audit(AuditStructuredEventPayload{std::move(acknowledged)});
    }
}

void emit_terminal(StructuredEventSink &sink,
                   const CompletedReadinessArtifacts &artifacts,
                   const AdaptiveV3ManagerSessionTerminalRecord &record)
{
    const auto identity = decode_activation_ready_identity_v1(
        artifacts.identity_bytes, ActivationReadinessWireLimits{64 * 1024, 7});
    REQUIRE(identity);
    AdaptiveV3ReadinessStructuredEvent terminal;
    terminal.transition = AdaptiveV3ReadinessTransition::terminal;
    terminal.identity = record.identity.value_or(*identity.value);
    terminal.terminal_cycle_ordinal = record.cycle_ordinal;
    terminal.terminal_reason = static_cast<std::uint8_t>(record.reason);
    terminal.terminal_identity = record.identity;
    terminal.terminal_bundle_digest = record.bundle_digest;
    terminal.observed_signers = record.r_audit_sources;
    terminal.required_release_count = record.r_audit_sources.size();
    terminal.disposition = "session_terminal";
    sink.emit_audit(AuditStructuredEventPayload{std::move(terminal)});
}

struct CommitPlan {
    std::uint64_t tick{0};
    std::uint64_t height{0};
    uint256_t block_hash;
    uint256_t parent_hash;
    std::uint64_t transaction_count{0};
    ProposalKey decision_proof;
    std::uint64_t view_generation{0};
    bool common_witness{false};
};

void emit_configuration_active(StructuredEventSink &sink,
                               const ConfigurationId &configuration,
                               ReplicaID observer)
{
    AdaptiveAggregationStructuredEvent event;
    event.transition = AdaptiveAggregationTransition::configuration_active;
    event.configuration = configuration;
    event.observer_replica = observer;
    event.global_quorum = 5;
    sink.emit_adaptive(event);
}

void emit_commit_plan(StructuredEventSink &sink, const CommitPlan &plan,
                      bool designated)
{
    sink.emit(StructuredEventPayload{CommitObservedStructuredEvent{
        plan.height, plan.block_hash, plan.parent_hash,
        plan.transaction_count, 0}});
    if (!designated) {
        sink.emit(StructuredEventPayload{CommitIdentityWitnessStructuredEvent{
            plan.height,
            plan.block_hash,
            plan.parent_hash,
            plan.transaction_count,
            plan.decision_proof,
            plan.view_generation,
            0}});
        return;
    }
    CommitStructuredEvent commit;
    commit.block_height = plan.height;
    commit.block_hash = plan.block_hash;
    commit.parent_hash = plan.parent_hash;
    commit.transaction_count = plan.transaction_count;
    commit.decision_proof = plan.decision_proof;
    commit.view_generation = plan.view_generation;
    commit.commit_batch_index = 0;
    sink.emit(StructuredEventPayload{std::move(commit)});
}

void emit_replica_cycle(
    StructuredEventSink &sink,
    ReplicaID replica,
    const CompletedReadinessArtifacts &artifacts,
    const std::optional<CommitPlan> &authoritative_command = std::nullopt,
    const std::vector<CommitPlan> &authoritative_pre_readiness = {})
{
    const ActivationReadinessWireLimits limits{64 * 1024, 7};
    const auto identity = decode_activation_ready_identity_v1(artifacts.identity_bytes, limits);
    const auto certificate = decode_activation_readiness_certificate(artifacts.certificate_bytes, limits);
    REQUIRE(identity); REQUIRE(certificate);
    if (authoritative_command)
        emit_commit_plan(sink, *authoritative_command, true);
    EpochCommandCommittedStructuredEvent command;
    command.command_block_height = identity.value->command_block_height;
    command.command_block_hash = identity.value->command_block_hash;
    command.predecessor_epoch_number = identity.value->predecessor_boundary_configuration.epoch_number;
    command.predecessor_epoch_digest = identity.value->predecessor_boundary_configuration.epoch_digest;
    command.successor_epoch_number = identity.value->successor_configuration.epoch_number;
    command.successor_epoch_digest = identity.value->successor_configuration.epoch_digest;
    command.payload_digest = identity.value->command_payload_digest;
    command.activation_delay_blocks = identity.value->activation_delay_blocks;
    command.activation_height = identity.value->activation_height;
    sink.emit_audit(AuditStructuredEventPayload{command});
    for (const auto &commit : authoritative_pre_readiness)
        emit_commit_plan(sink, commit, true);
    AdaptiveV3ReadinessStructuredEvent prepared;
    prepared.transition = AdaptiveV3ReadinessTransition::activation_prepared;
    prepared.identity = *identity.value; prepared.replica_id = replica;
    sink.emit_audit(AuditStructuredEventPayload{prepared});
    const auto observation = std::find_if(certificate.value->observations.begin(),
        certificate.value->observations.end(), [replica](const auto &value) {
            return value.signer_replica_id == replica; });
    REQUIRE(observation != certificate.value->observations.end());
    const auto index = static_cast<std::size_t>(std::distance(certificate.value->observations.begin(), observation));
    AdaptiveV3ReadinessStructuredEvent signed_event;
    signed_event.transition = AdaptiveV3ReadinessTransition::activation_ready_signed;
    signed_event.identity = *identity.value; signed_event.replica_id = replica;
    signed_event.signer_source_sequence = observation->signer_source_sequence;
    signed_event.signer_monotonic_raw_ns = observation->signer_monotonic_raw_ns;
    signed_event.observation_digest = activation_ready_observation_digest(*observation);
    signed_event.canonical_wire_payload = artifacts.observation_payloads.at(index);
    sink.emit_audit(AuditStructuredEventPayload{signed_event});
    AdaptiveV3ReadinessStructuredEvent accepted;
    accepted.transition = AdaptiveV3ReadinessTransition::certificate_accepted;
    accepted.identity = *identity.value; accepted.replica_id = replica;
    accepted.certificate_digest = artifacts.certificate_digest;
    accepted.payload_digest = activation_readiness_ack_payload_digest(
        MsgActivationReadinessCertificate::opcode, artifacts.certificate_bytes);
    accepted.canonical_wire_payload = artifacts.certificate_bytes;
    sink.emit_audit(AuditStructuredEventPayload{accepted});
    sink.emit(StructuredEventPayload{EpochLifecycleEvent{
        EpochLifecycleTransition::activated, identity.value->successor_configuration,
        identity.value->activation_height, identity.value->activation_height,
        artifacts.certificate_digest}});
}

bytearray_t emit_replica_stream(
    const std::filesystem::path &path,
    ReplicaID replica,
    const std::vector<const CompletedReadinessArtifacts *> &cycles,
    const std::vector<ConfigurationId> &active_configurations = {},
    const std::vector<CommitPlan> &commit_plans = {},
    ReplicaID designated_replica = 0)
{
    constexpr std::size_t predecessor_configuration_count = 12;
    ExclusiveFileStructuredEventOutput output(path.string());
    std::vector<std::uint64_t> ticks;
    const auto append_commit_ticks = [&ticks](const CommitPlan &plan) {
        ticks.push_back(plan.tick);
        ticks.push_back(plan.tick);
    };
    ticks.push_back(1'000'000ULL + replica);
    ticks.push_back(2'000'000ULL + replica);
    if (!cycles.empty() && replica == designated_replica) {
        REQUIRE(active_configurations.size() ==
                cycles.size() + predecessor_configuration_count);
        REQUIRE(commit_plans.size() == (cycles.size() == 1 ? 10 : 17));
        ticks.push_back(9'000'000'000ULL);
        append_commit_ticks(commit_plans.front());
        for (std::size_t tree = 1; tree < 7; ++tree)
            ticks.push_back((10'000ULL + tree) * 1'000'000ULL);
        for (std::size_t tree = 0; tree < 5; ++tree)
            ticks.push_back((41'000ULL + tree * 1'000ULL) * 1'000'000ULL);
    }
    for (std::size_t cycle_index = 0; cycle_index < cycles.size(); ++cycle_index) {
        const auto *cycle = cycles.at(cycle_index);
        const auto observation = std::find_if(cycle->observations.begin(), cycle->observations.end(),
            [replica](const auto &value) { return value.signer_replica_id == replica; });
        REQUIRE(observation != cycle->observations.end());
        const auto delivery_tick = delivery_tick_for(*cycle, replica);
        const auto cycle_base = 1U + cycle_index * 8U;
        if (replica == designated_replica) {
            append_commit_ticks(commit_plans.at(cycle_base));
            for (std::size_t offset = 1; offset <= 5; ++offset)
                append_commit_ticks(commit_plans.at(cycle_base + offset));
        }
        ticks.insert(ticks.end(), {cycle->readiness_tick, cycle->readiness_tick,
                                   observation->signer_monotonic_raw_ns,
                                   delivery_tick, delivery_tick});
        if (replica == designated_replica) {
            ticks.push_back(cycle_index == 0
                                ? 71'500'000'000ULL
                                : 137'500'000'000ULL);
        }
        const auto common_index = cycle_base + 6U;
        REQUIRE(common_index < commit_plans.size());
        append_commit_ticks(commit_plans.at(common_index));
        const auto measurement_index = cycle_base + 7U;
        REQUIRE(measurement_index < commit_plans.size());
        append_commit_ticks(commit_plans.at(measurement_index));
    }
    if (cycles.size() == 1 && replica == designated_replica)
        append_commit_ticks(commit_plans.at(9));
    DeterministicRawClock clock(std::move(ticks));
    auto config = event_config(StructuredEventSourceKind::replica,
        "replica-" + std::to_string(replica));
    if (!cycles.empty() && replica == designated_replica)
        config.designated_commit_observer = config.source;
    StructuredEventSink sink(config, clock, output);
    sink.emit(StructuredEventPayload{ProcessLifecycleEvent{
        ProcessLifecycleState::started, std::nullopt}});
    sink.emit(StructuredEventPayload{ProcessLifecycleEvent{
        ProcessLifecycleState::ready, std::nullopt}});
    if (!cycles.empty()) {
        const bool designated = replica == designated_replica;
        if (designated) {
            emit_configuration_active(sink, active_configurations.at(0), replica);
            emit_commit_plan(sink, commit_plans.at(0), true);
            for (std::size_t index = 1;
                 index < predecessor_configuration_count; ++index)
                emit_configuration_active(
                    sink, active_configurations.at(index), replica);
        }
        for (std::size_t index = 0; index < cycles.size(); ++index) {
            const auto cycle_base = 1U + index * 8U;
            std::vector<CommitPlan> pre_readiness;
            if (designated)
                pre_readiness.assign(
                    commit_plans.begin() + static_cast<std::ptrdiff_t>(cycle_base + 1U),
                    commit_plans.begin() + static_cast<std::ptrdiff_t>(cycle_base + 6U));
            emit_replica_cycle(
                sink, replica, *cycles.at(index),
                designated
                    ? std::optional<CommitPlan>{commit_plans.at(cycle_base)}
                    : std::nullopt,
                pre_readiness);
            if (designated)
                emit_configuration_active(
                    sink,
                    active_configurations.at(
                        predecessor_configuration_count + index),
                    replica);
            const auto common_index = cycle_base + 6U;
            emit_commit_plan(sink, commit_plans.at(common_index), designated);
            const auto measurement_index = cycle_base + 7U;
            emit_commit_plan(sink, commit_plans.at(measurement_index), designated);
        }
        if (cycles.size() == 1)
            emit_commit_plan(sink, commit_plans.at(9), designated);
    }
    sink.shutdown();
    INFO("manager sink failure=" << static_cast<unsigned>(sink.health().first_failure));
    REQUIRE(sink.health().healthy);
    const auto bytes = read_bytes(path);
    REQUIRE(parse_structured_event_prefix(bytes).status == StructuredEventPrefixStatus::complete);
    return bytes;
}

void emit_e2_eligibility(StructuredEventSink &sink,
                         const CompletedReadinessArtifacts &artifacts,
                         const AdaptiveV3E2EligibilityAuditSnapshot &audit)
{
    const auto identity = decode_activation_ready_identity_v1(
        artifacts.identity_bytes, ActivationReadinessWireLimits{64 * 1024, 7});
    REQUIRE(identity);
    AdaptiveV3ReadinessStructuredEvent event;
    event.transition = AdaptiveV3ReadinessTransition::e2_eligibility;
    event.identity = *identity.value;
    event.observed_signers = audit.common_commit_sources;
    event.required_release_count = audit.common_commit_sources.size();
    event.disposition = "eligible";
    event.e2_cycle_ordinal = audit.cycle_ordinal;
    event.e1_bundle_digest = audit.e1_bundle_digest;
    event.e2_final_ack_raw_ns = audit.final_ack_tick;
    event.e2_common_commit = audit.common_commit;
    event.e2_common_commit_sources = audit.common_commit_sources;
    event.e2_common_commit_raw_ns = audit.common_commit_tick;
    event.e2_earliest_raw_ns = audit.earliest_e2_tick;
    event.e2_actual_begin_raw_ns = audit.actual_e2_begin_tick;
    event.e2_hard_deadline_raw_ns = audit.hard_deadline_tick;
    event.e2_reserve_raw_ns = audit.reserve_ticks;
    sink.emit_audit(AuditStructuredEventPayload{std::move(event)});
}

std::size_t emit_event_file(const std::filesystem::path &path,
                            StructuredEventSourceKind kind,
                            const std::string &source_id,
                            const std::vector<const CompletedReadinessArtifacts *> &cycles,
                            bool terminal)
{
    std::filesystem::create_directories(path.parent_path());
    ExclusiveFileStructuredEventOutput output(path.string());
    DeterministicRawClock clock({10'000'000'000ULL});
    StructuredEventSink sink(event_config(kind, source_id), clock, output);
    if (kind == StructuredEventSourceKind::adaptation_manager) {
        for (const auto *cycle : cycles) emit_manager_cycle(sink, *cycle);
        static_cast<void>(terminal);
    } else {
        sink.emit(StructuredEventPayload{ProcessLifecycleEvent{ProcessLifecycleState::ready, std::nullopt}});
    }
    sink.shutdown();
    const auto health = sink.health();
    REQUIRE(health.healthy);
    REQUIRE(health.complete_records != 0);
    const auto bytes = read_bytes(path);
    const auto parsed = parse_structured_event_prefix(bytes);
    REQUIRE(parsed.status == StructuredEventPrefixStatus::complete);
    REQUIRE(parsed.complete_records == health.complete_records);
    return health.complete_records;
}

std::size_t emit_manager_file(const std::filesystem::path &path,
                              const std::vector<const SelectionArtifacts *> &selections,
                              const std::vector<const CompletedReadinessArtifacts *> &cycles,
                              const std::vector<AdaptiveV3ManagerSessionTerminalRecord> &terminals,
                              const std::optional<AdaptiveV3E2EligibilityAuditSnapshot> &e2)
{
    std::filesystem::create_directories(path.parent_path());
    ExclusiveFileStructuredEventOutput output(path.string());
    std::vector<std::uint64_t> ticks;
    for (std::size_t index = 0; index < cycles.size(); ++index) {
        REQUIRE(index < selections.size());
        append_manager_selection_ticks(ticks, *selections.at(index));
        append_manager_cycle_ticks(ticks, *cycles.at(index));
        REQUIRE(index < terminals.size());
        ticks.push_back(cycles.at(index)->final_ack_tick);
        if (index == 0 && e2) ticks.push_back(e2->actual_e2_begin_tick);
    }
    DeterministicRawClock clock(std::move(ticks));
    StructuredEventSink sink(event_config(StructuredEventSourceKind::adaptation_manager,
                                          "adaptive-manager"), clock, output);
    for (std::size_t index = 0; index < cycles.size(); ++index) {
        REQUIRE(index < selections.size());
        emit_manager_selection(sink, *selections.at(index));
        REQUIRE(sink.health().healthy);
        emit_manager_cycle(sink, *cycles.at(index));
        CAPTURE(index);
        REQUIRE(sink.health().healthy);
        REQUIRE(index < terminals.size());
        emit_terminal(sink, *cycles.at(index), terminals.at(index));
        REQUIRE(sink.health().first_failure == StructuredEventFailure::none);
        if (index == 0 && e2) emit_e2_eligibility(sink, *cycles.at(index), *e2);
        REQUIRE(sink.health().healthy);
    }
    sink.shutdown(); REQUIRE(sink.health().healthy);
    const auto bytes = read_bytes(path);
    const auto parsed = parse_structured_event_prefix(bytes);
    REQUIRE(parsed.status == StructuredEventPrefixStatus::complete);
    return parsed.complete_records;
}

std::string source_inventory(const std::vector<std::pair<std::string, std::string>> &sources)
{
    std::string output = "{\"sources\":[";
    for (std::size_t index = 0; index < sources.size(); ++index) {
        if (index != 0) output += ',';
        output += "[\"" + sources.at(index).first + "\",\"" + sources.at(index).second + "\"]";
    }
    return output + "]}\n";
}

std::string output_manifest(const std::vector<ArtifactFile> &files)
{
    std::string result = "{\"schema\":\"kauri-cert13-v13-public-fixture-v1\",\"files\":[";
    for (std::size_t index = 0; index < files.size(); ++index) {
        if (index != 0) result += ',';
        const auto &file = files.at(index);
        result += "{\"path\":\"" + file.relative_path + "\",\"sha256\":\"" +
                  DataStream(file.contents).get_hash().to_hex() + "\"}";
    }
    return result + "]}\n";
}

std::string phase_windows(bool adaptive)
{
    const auto late_start = adaptive ? 168'000'000'000ULL
                                     : 162'000'000'000ULL;
    const auto late_epoch = adaptive ? 2 : 1;
    return
        "{\"domain\":\"kauri-focused-causal-phase-windows-v1\","
        "\"phases\":["
        "{\"end_ns\":40000000000,\"epoch_number\":0,\"phase\":\"baseline\",\"start_ns\":10000000000},"
        "{\"end_ns\":70001000000,\"epoch_number\":0,\"phase\":\"fault\",\"start_ns\":40001000000},"
        "{\"end_ns\":132000000000,\"epoch_number\":1,\"phase\":\"epoch1\",\"start_ns\":102000000000},"
        "{\"end_ns\":" + std::to_string(late_start + 30'000'000'000ULL) +
        ",\"epoch_number\":" + std::to_string(late_epoch) +
        ",\"phase\":\"late\",\"start_ns\":" + std::to_string(late_start) +
        "}],\"schema_version\":1}\n";
}

} // namespace

TEST_CASE("CERT13 v13 exporter emits verified public readiness fixtures",
          "[cert13][fixture-exporter][adaptive-v3]")
{
    const std::vector<ReplicaID> survivors{2, 3, 4, 5, 6};
    constexpr std::uint64_t raw_ns = 1'000'000ULL;
    constexpr std::uint64_t fault_confirmed_raw_ns = 40'001 * raw_ns;
    constexpr std::uint64_t hard_deadline =
        fault_confirmed_raw_ns + 480'000'000'000ULL;
    ExportRoot root;
    std::vector<ArtifactFile> files;

    Fixture control(7, survivors, 1,
                    BlsMembershipConstruction::deterministic_fixed_scalars, raw_ns,
                    true);
    const auto control_arm = control.select_containment_exact_v13(
        {0, 1}, survivors, fault_confirmed_raw_ns, hard_deadline);
    const auto control_selection = capture_selection(
        control, 0, TreePolicyKind::fault_containment,
        "e0-to-e1-containment",
        std::optional<AdaptiveV2FaultWindowArm>{control_arm});
    const auto control_e1 = complete_readiness_capture(
        control, survivors, 71'000 * raw_ns, 1000, "fixture-control-e1", 1,
        71'000 * raw_ns);
    REQUIRE(control.session.status() == AdaptiveV3ManagerSessionStatus::terminal);

    Fixture adaptive(7, survivors, 2,
                     BlsMembershipConstruction::deterministic_fixed_scalars, raw_ns,
                     true);
    const auto adaptive_arm = adaptive.select_containment_exact_v13(
        {0, 1}, survivors, fault_confirmed_raw_ns, hard_deadline);
    const auto adaptive_e1_selection = capture_selection(
        adaptive, 0, TreePolicyKind::fault_containment,
        "e0-to-e1-containment",
        std::optional<AdaptiveV2FaultWindowArm>{adaptive_arm});
    const auto adaptive_e1 = complete_readiness_capture(
        adaptive, survivors, 71'000 * raw_ns, 1000, "fixture-adaptive-e1", 1,
        71'000 * raw_ns);
    record_common_commit(
        adaptive, survivors, 72'000 * raw_ns, "fixture-adaptive-commit");
    adaptive.session.advance(136'015 * raw_ns);
    AdaptiveV2TransitionPolicy optimization;
    optimization.intent = TreePolicyKind::performance_optimization;
    const auto e2_audit = adaptive.session.e2_eligibility_audit(136'015 * raw_ns);
    REQUIRE(e2_audit);
    REQUIRE(e2_audit->earliest_e2_tick == 136'005 * raw_ns);
    REQUIRE(e2_audit->hard_deadline_tick == hard_deadline);
    REQUIRE(adaptive.session.begin_e2_at(136'015 * raw_ns, optimization));
    adaptive.select_optimization_exact_v13(
        survivors, 136'016 * raw_ns);
    const auto adaptive_e2_selection = capture_selection(
        adaptive, 1, TreePolicyKind::performance_optimization,
        "e1-to-e2-optimization", std::nullopt);
    const auto adaptive_e2 = complete_readiness_capture(
        adaptive, survivors, 137'000 * raw_ns, 1008, "fixture-adaptive-e2", 2,
        137'000 * raw_ns);
    REQUIRE(adaptive.session.status() == AdaptiveV3ManagerSessionStatus::terminal);

    const ActivationReadinessWireLimits readiness_limits{64 * 1024, 7};
    const auto control_e1_identity = decode_activation_ready_identity_v1(
        control_e1.identity_bytes, readiness_limits);
    const auto adaptive_e1_identity = decode_activation_ready_identity_v1(
        adaptive_e1.identity_bytes, readiness_limits);
    const auto adaptive_e2_identity = decode_activation_ready_identity_v1(
        adaptive_e2.identity_bytes, readiness_limits);
    REQUIRE(control_e1_identity);
    REQUIRE(adaptive_e1_identity);
    REQUIRE(adaptive_e2_identity);
    const ConfigurationId control_epoch0{
        0, 0, control_arm.predecessor_epoch_digest};
    const ConfigurationId adaptive_epoch0{
        0, 0, adaptive_arm.predecessor_epoch_digest};
    const auto control_epoch1 =
        control_e1_identity.value->successor_configuration;
    const auto adaptive_epoch1 =
        adaptive_e1_identity.value->successor_configuration;
    const auto adaptive_epoch2 =
        adaptive_e2_identity.value->successor_configuration;

    auto configuration_schedule = [](const ConfigurationId &epoch0,
                                     const std::vector<ConfigurationId> &successors) {
        std::vector<ConfigurationId> result;
        for (std::uint32_t tree = 0; tree < 7; ++tree)
            result.push_back(
                ConfigurationId{epoch0.epoch_number, tree, epoch0.epoch_digest});
        for (std::uint32_t tree = 0; tree < 5; ++tree)
            result.push_back(
                ConfigurationId{epoch0.epoch_number, tree, epoch0.epoch_digest});
        result.insert(result.end(), successors.begin(), successors.end());
        return result;
    };
    const auto control_configurations = configuration_schedule(
        control_epoch0, {control_epoch1});
    const auto adaptive_configurations = configuration_schedule(
        adaptive_epoch0, {adaptive_epoch1, adaptive_epoch2});

    auto append_commit = [](std::vector<CommitPlan> &commits,
                            std::uint64_t tick,
                            std::uint64_t height,
                            const uint256_t &block_hash,
                            std::uint64_t transaction_count,
                            const ConfigurationId &configuration,
                            std::uint64_t generation,
                            bool common_witness = false) {
        REQUIRE(!commits.empty());
        commits.push_back(CommitPlan{
            tick, height, block_hash, commits.back().block_hash,
            transaction_count, ProposalKey{configuration, block_hash},
            generation, common_witness});
    };

    const auto control_baseline_hash = digest("fixture-control-baseline");
    std::vector<CommitPlan> control_commits{
        {10'000 * raw_ns, 999, control_baseline_hash,
         digest("fixture-control-genesis-parent"), 1000,
         ProposalKey{control_epoch0, control_baseline_hash}, 1, false},
    };
    const auto &control_boundary_configuration =
        control_e1_identity.value->predecessor_boundary_configuration;
    const auto control_boundary_generation =
        control_e1_identity.value->predecessor_boundary_generation;
    append_commit(control_commits, 70'000 * raw_ns,
                  control_e1_identity.value->command_block_height,
                  control_e1_identity.value->command_block_hash, 0,
                  control_boundary_configuration, control_boundary_generation);
    for (std::uint64_t height = 1001; height < 1005; ++height)
        append_commit(control_commits,
                      (69'100 + height) * raw_ns,
                      height,
                      digest("fixture-control-pre-boundary-" +
                             std::to_string(height)),
                      0, control_boundary_configuration,
                      control_boundary_generation);
    append_commit(control_commits, 70'500 * raw_ns,
                  control_e1_identity.value->activation_height,
                  control_e1_identity.value->activation_boundary_block_hash, 0,
                  control_boundary_configuration, control_boundary_generation);
    append_commit(control_commits, 72'000 * raw_ns, 1006,
                  digest("fixture-control-common-e1"), 0,
                  control_epoch1, (1ULL << 32U) + 1U, true);
    append_commit(control_commits, 110'000 * raw_ns, 1007,
                  digest("fixture-control-epoch1-measurement"), 1000,
                  control_epoch1, (1ULL << 32U) + 1U);
    append_commit(control_commits, 170'000 * raw_ns, 1008,
                  digest("fixture-control-late"), 1000,
                  control_epoch1, (1ULL << 32U) + 1U);

    const auto adaptive_baseline_hash = digest("fixture-adaptive-baseline");
    std::vector<CommitPlan> adaptive_commits{
        {10'000 * raw_ns, 999, adaptive_baseline_hash,
         digest("fixture-adaptive-genesis-parent"), 1000,
         ProposalKey{adaptive_epoch0, adaptive_baseline_hash}, 1, false},
    };
    const auto &adaptive_e1_boundary_configuration =
        adaptive_e1_identity.value->predecessor_boundary_configuration;
    const auto adaptive_e1_boundary_generation =
        adaptive_e1_identity.value->predecessor_boundary_generation;
    append_commit(adaptive_commits, 70'000 * raw_ns,
                  adaptive_e1_identity.value->command_block_height,
                  adaptive_e1_identity.value->command_block_hash, 0,
                  adaptive_e1_boundary_configuration,
                  adaptive_e1_boundary_generation);
    for (std::uint64_t height = 1001; height < 1005; ++height)
        append_commit(adaptive_commits,
                      (69'100 + height) * raw_ns,
                      height,
                      digest("fixture-adaptive-e1-pre-boundary-" +
                             std::to_string(height)),
                      0, adaptive_e1_boundary_configuration,
                      adaptive_e1_boundary_generation);
    append_commit(adaptive_commits, 70'500 * raw_ns,
                  adaptive_e1_identity.value->activation_height,
                  adaptive_e1_identity.value->activation_boundary_block_hash, 0,
                  adaptive_e1_boundary_configuration,
                  adaptive_e1_boundary_generation);
    append_commit(adaptive_commits, 72'000 * raw_ns, 1006,
                  e2_audit->common_commit.block_hash, 0,
                  e2_audit->common_commit.configuration,
                  (1ULL << 32U) + 1U, true);
    append_commit(adaptive_commits, 110'000 * raw_ns, 1007,
                  digest("fixture-adaptive-epoch1-measurement"), 1000,
                  adaptive_epoch1, (1ULL << 32U) + 1U);

    const auto &adaptive_e2_boundary_configuration =
        adaptive_e2_identity.value->predecessor_boundary_configuration;
    const auto adaptive_e2_boundary_generation =
        adaptive_e2_identity.value->predecessor_boundary_generation;
    append_commit(adaptive_commits, 136'500 * raw_ns,
                  adaptive_e2_identity.value->command_block_height,
                  adaptive_e2_identity.value->command_block_hash, 0,
                  adaptive_e2_boundary_configuration,
                  adaptive_e2_boundary_generation);
    for (std::uint64_t height = 1009; height < 1013; ++height)
        append_commit(adaptive_commits,
                      (135'600 + height) * raw_ns,
                      height,
                      digest("fixture-adaptive-e2-pre-boundary-" +
                             std::to_string(height)),
                      0, adaptive_e2_boundary_configuration,
                      adaptive_e2_boundary_generation);
    append_commit(adaptive_commits, 136'950 * raw_ns,
                  adaptive_e2_identity.value->activation_height,
                  adaptive_e2_identity.value->activation_boundary_block_hash, 0,
                  adaptive_e2_boundary_configuration,
                  adaptive_e2_boundary_generation);
    append_commit(adaptive_commits, 138'000 * raw_ns, 1014,
                  digest("fixture-adaptive-common-e2"), 0,
                  adaptive_epoch2, (2ULL << 32U) + 1U, true);
    append_commit(adaptive_commits, 175'000 * raw_ns, 1015,
                  digest("fixture-adaptive-late"), 1000,
                  adaptive_epoch2, (2ULL << 32U) + 1U);
    for (const auto &observation : control_e1.observations) {
        REQUIRE(observation.signer_source_sequence == 1);
        REQUIRE(observation.signer_monotonic_raw_ns >= 71'000 * raw_ns);
    }
    for (const auto &observation : adaptive_e1.observations) {
        REQUIRE(observation.signer_source_sequence == 1);
        REQUIRE(observation.signer_monotonic_raw_ns >= 71'000 * raw_ns);
    }
    for (const auto &observation : adaptive_e2.observations) {
        REQUIRE(observation.signer_source_sequence == 2);
        REQUIRE(observation.signer_monotonic_raw_ns >= 137'000 * raw_ns);
    }

    const auto manifest_path = root.path / "shared/arm_manifest.json";
    const auto manifest = readiness_manifest(control.bls);
    files.push_back({"shared/arm_manifest.json", bytearray_t(manifest.begin(), manifest.end())});
    files.push_back({"control/runtime/activation-readiness-public-manifest.json",
                     bytearray_t(manifest.begin(), manifest.end())});
    files.push_back({"adaptive/runtime/activation-readiness-public-manifest.json",
                     bytearray_t(manifest.begin(), manifest.end())});
    const auto issuer_public_key = get_hex(PubKeySecp256k1(issuer_key())) + "\n";
    files.push_back({"control/raw/issuer-public-key.txt",
                     bytearray_t(issuer_public_key.begin(), issuer_public_key.end())});
    files.push_back({"adaptive/raw/issuer-public-key.txt",
                     bytearray_t(issuer_public_key.begin(), issuer_public_key.end())});
    const auto control_windows = phase_windows(false);
    const auto adaptive_windows = phase_windows(true);
    files.push_back({"control/derived/phase-windows.json",
                     text_bytes(control_windows)});
    files.push_back({"adaptive/derived/phase-windows.json",
                     text_bytes(adaptive_windows)});
    add_cycle_files(files, "control", "epoch1", control_e1);
    add_cycle_files(files, "adaptive", "epoch1", adaptive_e1);
    add_cycle_files(files, "adaptive", "epoch2", adaptive_e2);
    for (const auto &file : files) {
        assert_public(file);
        write_bytes(root.path / file.relative_path, file.contents);
    }
    verify_cycle(root.path, manifest_path, "control", "epoch1", control_e1);
    verify_cycle(root.path, manifest_path, "adaptive", "epoch1", adaptive_e1);
    verify_cycle(root.path, manifest_path, "adaptive", "epoch2", adaptive_e2);
    const auto control_replica = root.path / "control/raw/replica-events.jsonl";
    const auto control_manager = root.path / "control/raw/adaptive-manager-events.jsonl";
    const auto control_client = root.path / "control/raw/client-events.jsonl";
    const auto adaptive_replica = root.path / "adaptive/raw/replica-events.jsonl";
    const auto adaptive_manager = root.path / "adaptive/raw/adaptive-manager-events.jsonl";
    const auto adaptive_client = root.path / "adaptive/raw/client-events.jsonl";
    bytearray_t control_replica_bytes;
    bytearray_t adaptive_replica_bytes;
    for (ReplicaID replica = 0; replica < 7; ++replica) {
        const auto control_part = root.path / "control/raw" /
            ("replica-" + std::to_string(replica) + ".jsonl");
        const auto adaptive_part = root.path / "adaptive/raw" /
            ("replica-" + std::to_string(replica) + ".jsonl");
        std::filesystem::create_directories(control_part.parent_path());
        std::filesystem::create_directories(adaptive_part.parent_path());
        const bool survivor = std::find(survivors.begin(), survivors.end(), replica) != survivors.end();
        const auto control_bytes = survivor
            ? emit_replica_stream(
                  control_part, replica, {&control_e1},
                  control_configurations, control_commits,
                  survivors.front())
            : emit_replica_stream(control_part, replica, {});
        const auto adaptive_bytes = survivor ? emit_replica_stream(
            adaptive_part, replica, {&adaptive_e1, &adaptive_e2},
            adaptive_configurations,
            adaptive_commits, survivors.front()) :
            emit_replica_stream(adaptive_part, replica, {});
        control_replica_bytes.insert(control_replica_bytes.end(), control_bytes.begin(), control_bytes.end());
        adaptive_replica_bytes.insert(adaptive_replica_bytes.end(), adaptive_bytes.begin(), adaptive_bytes.end());
        files.push_back({std::filesystem::relative(control_part, root.path).string(), control_bytes});
        files.push_back({std::filesystem::relative(adaptive_part, root.path).string(), adaptive_bytes});
    }
    write_bytes(control_replica, control_replica_bytes);
    write_bytes(adaptive_replica, adaptive_replica_bytes);
    const auto control_replica_count = parse_structured_event_prefix(control_replica_bytes).complete_records;
    const auto adaptive_replica_count = parse_structured_event_prefix(adaptive_replica_bytes).complete_records;
    const auto control_manager_count = emit_manager_file(
        control_manager, {&control_selection}, {&control_e1},
        control.session.terminal_records(), std::nullopt);
    const auto adaptive_manager_count = emit_manager_file(
        adaptive_manager, {&adaptive_e1_selection, &adaptive_e2_selection},
        {&adaptive_e1, &adaptive_e2},
        adaptive.session.terminal_records(), e2_audit);
    write_bytes(control_client, {});
    write_bytes(adaptive_client, {});
    REQUIRE(control_replica_count == 96);
    REQUIRE(control_manager_count > survivors.size());
    REQUIRE(adaptive_replica_count == 144);
    REQUIRE(adaptive_manager_count > control_manager_count);
    const auto control_replica_text = std::string(control_replica_bytes.begin(),
                                                   control_replica_bytes.end());
    const auto adaptive_replica_text = std::string(adaptive_replica_bytes.begin(),
                                                    adaptive_replica_bytes.end());
    for (const auto *event_type : {"epoch.command_committed", "epoch.activation_prepared",
                                   "epoch.activation_ready_signed",
                                   "adaptive_v3.readiness_certificate_accepted",
                                   "epoch.activated"}) {
        REQUIRE(event_count(control_replica_text, event_type) == survivors.size());
        REQUIRE(event_count(adaptive_replica_text, event_type) == survivors.size() * 2);
    }
    REQUIRE(event_count(control_replica_text, "process.started") == 7);
    REQUIRE(event_count(adaptive_replica_text, "process.started") == 7);
    REQUIRE(event_count(control_replica_text, "process.ready") == 7);
    REQUIRE(event_count(adaptive_replica_text, "process.ready") == 7);
    REQUIRE(event_count(control_replica_text, "block.committed") == 10);
    REQUIRE(event_count(control_replica_text, "block.commit_observed") == 22);
    REQUIRE(event_count(control_replica_text,
                        "block.commit_identity_witness") == 12);
    REQUIRE(event_count(adaptive_replica_text, "block.committed") == 17);
    REQUIRE(event_count(adaptive_replica_text, "block.commit_observed") == 33);
    REQUIRE(event_count(adaptive_replica_text,
                        "block.commit_identity_witness") == 16);
    const auto designated_part = root.path / "adaptive/raw/replica-2.jsonl";
    const auto designated_bytes = read_bytes(designated_part);
    const auto designated_text = std::string(
        designated_bytes.begin(), designated_bytes.end());
    REQUIRE(designated_text.find("\"event_type\":\"block.commit_observed\"") !=
            std::string::npos);
    REQUIRE(designated_text.find("\"event_type\":\"block.committed\"") !=
            std::string::npos);
    REQUIRE(designated_text.find(
        "\"source_monotonic_ns\":72000000000,\"event_type\":\"block.committed\"") !=
            std::string::npos);
    REQUIRE(designated_text.find("\"designated_observer\":true") !=
            std::string::npos);
    REQUIRE(designated_text.find("\"view_generation\":1") !=
            std::string::npos);
    REQUIRE(designated_text.find("reporter_local_commit_monotonic_ns") ==
            std::string::npos);
    const auto control_manager_bytes = read_bytes(control_manager);
    const auto control_manager_text = std::string(control_manager_bytes.begin(),
                                                  control_manager_bytes.end());
    REQUIRE(event_count(control_manager_text,
                        "adaptive_v3.readiness_observation_accepted") == survivors.size());
    REQUIRE(event_count(control_manager_text,
                        "adaptive_v3.readiness_certificate_assembled") == 1);
    REQUIRE(event_count(control_manager_text,
                        "adaptive_v3.readiness_certificate_delivery") == survivors.size());
    REQUIRE(event_count(control_manager_text,
                        "adaptive_v3.readiness_certificate_acknowledged") == survivors.size());
    REQUIRE(event_count(control_manager_text, "adaptive_v3.readiness_terminal") == 1);
    REQUIRE(event_count(control_manager_text, "fault_window_armed") == 1);
    REQUIRE(event_count(control_manager_text, "adaptive_v2_evidence_snapshot") == 1);
    REQUIRE(event_count(control_manager_text, "evidence.observation_accepted") > 0);
    const auto adaptive_manager_bytes = read_bytes(adaptive_manager);
    const auto adaptive_manager_text = std::string(
        adaptive_manager_bytes.begin(), adaptive_manager_bytes.end());
    const auto first_terminal = adaptive_manager_text.find("\"event_type\":\"adaptive_v3.readiness_terminal\"");
    const auto e2_event = adaptive_manager_text.find("\"event_type\":\"adaptive_v3.e2_eligibility\"");
    const auto second_terminal = adaptive_manager_text.find(
        "\"event_type\":\"adaptive_v3.readiness_terminal\"", first_terminal + 1);
    REQUIRE(first_terminal != std::string::npos);
    REQUIRE(e2_event != std::string::npos);
    REQUIRE(second_terminal != std::string::npos);
    REQUIRE(first_terminal < e2_event);
    REQUIRE(e2_event < second_terminal);
    REQUIRE(event_count(adaptive_manager_text,
                        "adaptive_v3.readiness_observation_accepted") == survivors.size() * 2);
    REQUIRE(event_count(adaptive_manager_text, "fault_window_armed") == 1);
    REQUIRE(event_count(adaptive_manager_text, "adaptive_v2_evidence_snapshot") == 2);
    REQUIRE(event_count(adaptive_manager_text, "evidence.observation_accepted") > 0);
    REQUIRE(event_count(adaptive_manager_text,
                        "adaptive_v3.readiness_certificate_assembled") == 2);
    REQUIRE(event_count(adaptive_manager_text,
                        "adaptive_v3.readiness_certificate_delivery") == survivors.size() * 2);
    REQUIRE(event_count(adaptive_manager_text,
                        "adaptive_v3.readiness_certificate_acknowledged") == survivors.size() * 2);
    REQUIRE(event_count(adaptive_manager_text, "adaptive_v3.readiness_terminal") == 2);
    REQUIRE(event_count(adaptive_manager_text, "adaptive_v3.e2_eligibility") == 1);
    for (const auto &path : {control_replica, adaptive_replica}) {
        const auto bytes = read_bytes(path);
        const auto contents = std::string(bytes.begin(), bytes.end());
        REQUIRE(contents.find("\"source_kind\":\"replica\"") != std::string::npos);
        for (ReplicaID replica = 0; replica < 7; ++replica)
            REQUIRE(contents.find("\"source_id\":\"replica-" + std::to_string(replica) + "\"") != std::string::npos);
    }
    for (ReplicaID replica = 0; replica < 7; ++replica) {
        REQUIRE(std::filesystem::exists(root.path / "control/raw" /
                                        ("replica-" + std::to_string(replica) + ".jsonl")));
        REQUIRE(std::filesystem::exists(root.path / "adaptive/raw" /
                                        ("replica-" + std::to_string(replica) + ".jsonl")));
    }
    for (const auto survivor : survivors) {
        const auto control_part = root.path / "control/raw" /
            ("replica-" + std::to_string(survivor) + ".jsonl");
        const auto adaptive_part = root.path / "adaptive/raw" /
            ("replica-" + std::to_string(survivor) + ".jsonl");
        const auto adaptive_part_bytes = read_bytes(adaptive_part);
        const auto adaptive_part_text = std::string(adaptive_part_bytes.begin(),
                                                    adaptive_part_bytes.end());
        REQUIRE(adaptive_part_text.find("\"signer_source_sequence\":1") != std::string::npos);
        REQUIRE(adaptive_part_text.find("\"signer_source_sequence\":2") != std::string::npos);
        REQUIRE(adaptive_part_text.find("\"source_monotonic_ns\":137000000000") != std::string::npos);
        REQUIRE(adaptive_part_text.find("\"source_monotonic_ns\":72000000000") != std::string::npos);
        if (survivor == survivors.front()) {
            REQUIRE(adaptive_part_text.find("\"event_type\":\"block.committed\"") != std::string::npos);
            REQUIRE(adaptive_part_text.find("reporter_local_commit_monotonic_ns") == std::string::npos);
            REQUIRE(adaptive_part_text.find("\"block_hash\":\"" +
                e2_audit->common_commit.block_hash.to_hex() + "\"") != std::string::npos);
        }
        REQUIRE(adaptive_part_text.find("\"event_type\":\"block.commit_observed\"") != std::string::npos);
    }
    REQUIRE(adaptive_manager_text.find("\"source_monotonic_ns\":136015000000") != std::string::npos);
    REQUIRE(adaptive_manager_text.find("\"source_monotonic_ns\":137000000002") != std::string::npos);
    REQUIRE(adaptive_manager_text.find("\"source_monotonic_ns\":137000000006") != std::string::npos);
    for (const auto &path : {control_manager, adaptive_manager}) {
        const auto bytes = read_bytes(path);
        const auto contents = std::string(bytes.begin(), bytes.end());
        REQUIRE(contents.find("\"source_kind\":\"adaptation_manager\"") != std::string::npos);
        REQUIRE(contents.find("\"source_id\":\"adaptive-manager\"") != std::string::npos);
    }
    const std::vector<std::pair<std::string, std::string>> sources{
        {"adaptation_manager", "adaptive-manager"}, {"replica", "replica-0"}, {"replica", "replica-1"}, {"replica", "replica-2"},
        {"replica", "replica-3"}, {"replica", "replica-4"},
        {"replica", "replica-5"}, {"replica", "replica-6"}};
    const auto inventory = source_inventory(sources);
    REQUIRE(inventory == "{\"sources\":[[\"adaptation_manager\",\"adaptive-manager\"],"
                         "[\"replica\",\"replica-0\"],[\"replica\",\"replica-1\"],[\"replica\",\"replica-2\"],[\"replica\",\"replica-3\"],"
                         "[\"replica\",\"replica-4\"],[\"replica\",\"replica-5\"],"
                         "[\"replica\",\"replica-6\"]]}\n");
    write_text(root.path / "control/runtime/source-inventory.json", inventory);
    write_text(root.path / "adaptive/runtime/source-inventory.json", inventory);
    for (const auto &path : {control_replica, control_manager, control_client,
                             adaptive_replica, adaptive_manager, adaptive_client}) {
        const auto relative = std::filesystem::relative(path, root.path).string();
        files.push_back({relative, read_bytes(path)});
    }
    files.push_back({"control/runtime/source-inventory.json", text_bytes(inventory)});
    files.push_back({"adaptive/runtime/source-inventory.json", text_bytes(inventory)});
    write_text(root.path / "output_manifest.json", output_manifest(files));
    REQUIRE(std::filesystem::exists(root.path / "output_manifest.json"));
    const auto output_manifest_bytes = read_bytes(root.path / "output_manifest.json");
    const auto output_manifest_text = std::string(output_manifest_bytes.begin(),
                                                  output_manifest_bytes.end());
    for (const auto &relative : {"control/runtime/activation-readiness-public-manifest.json",
                                 "adaptive/runtime/activation-readiness-public-manifest.json",
                                 "control/raw/replica-2.jsonl",
                                 "adaptive/raw/replica-2.jsonl"})
        REQUIRE(output_manifest_text.find("\"path\":\"" + std::string(relative) + "\"") != std::string::npos);
}
