#include <cerrno>
#include <cctype>
#include <cstddef>
#include <cstdlib>
#include <fstream>
#include <initializer_list>
#include <optional>
#include <stdexcept>
#include <sstream>
#include <string>
#include <utility>
#include <vector>

#include <sys/types.h>
#include <sys/wait.h>
#include <unistd.h>

#include "catch.hpp"

#ifndef KAURI_PROJECT_SOURCE_DIR
#error "KAURI_PROJECT_SOURCE_DIR must name the repository root"
#endif

#ifndef KAURI_HOTSTUFF_APP_PATH
#error "KAURI_HOTSTUFF_APP_PATH must name the hotstuff-app executable"
#endif

namespace
{

std::string source(const char *relative_path)
{
    std::ifstream input(
        std::string(KAURI_PROJECT_SOURCE_DIR) + "/" + relative_path);
    REQUIRE(input.good());
    std::ostringstream contents;
    contents << input.rdbuf();
    return contents.str();
}

std::string function_body(
    const std::string &contents,
    const std::string &signature)
{
    const auto declaration = contents.find(signature);
    if (declaration == std::string::npos)
        return {};
    const auto opening = contents.find('{', declaration + signature.size());
    if (opening == std::string::npos)
        return {};

    std::size_t depth = 0;
    for (std::size_t cursor = opening; cursor < contents.size(); ++cursor)
    {
        if (contents[cursor] == '{')
            ++depth;
        else if (contents[cursor] == '}' && --depth == 0)
            return contents.substr(opening, cursor - opening + 1);
    }
    return {};
}

bool contains_all(
    const std::string &contents,
    std::initializer_list<const char *> needles)
{
    for (const auto *needle : needles)
        if (contents.find(needle) == std::string::npos)
            return false;
    return true;
}

bool contains_in_order(
    const std::string &contents,
    std::initializer_list<const char *> needles)
{
    std::size_t cursor = 0;
    for (const auto *needle : needles)
    {
        const auto found = contents.find(needle, cursor);
        if (found == std::string::npos)
            return false;
        cursor = found + std::string(needle).size();
    }
    return true;
}

std::size_t count_occurrences(
    const std::string &contents,
    const std::string &needle)
{
    std::size_t count = 0;
    for (std::size_t cursor = 0;
         (cursor = contents.find(needle, cursor)) != std::string::npos;
         cursor += needle.size())
        ++count;
    return count;
}

std::optional<std::string> option_binding(
    const std::string &contents,
    const std::string &option)
{
    const auto option_literal = "\"" + option + "\"";
    const auto option_position = contents.find(option_literal);
    if (option_position == std::string::npos)
        return std::nullopt;
    const auto add_option = contents.rfind("config.add_opt(", option_position);
    if (add_option == std::string::npos)
        return std::nullopt;
    auto cursor = contents.find(',', option_position + option_literal.size());
    if (cursor == std::string::npos)
        return std::nullopt;
    ++cursor;
    while (cursor < contents.size() &&
           std::isspace(static_cast<unsigned char>(contents[cursor])) != 0)
        ++cursor;
    const auto begin = cursor;
    while (cursor < contents.size())
    {
        const auto character =
            static_cast<unsigned char>(contents[cursor]);
        if (std::isalnum(character) == 0 && contents[cursor] != '_')
            break;
        ++cursor;
    }
    if (cursor == begin)
        return std::nullopt;
    return contents.substr(begin, cursor - begin);
}

std::string without_whitespace(const std::string &contents)
{
    std::string compact;
    compact.reserve(contents.size());
    for (const auto character : contents)
        if (std::isspace(static_cast<unsigned char>(character)) == 0)
            compact.push_back(character);
    return compact;
}

struct ProcessResult
{
    int status{0};
    std::string output;
};

ProcessResult run_hotstuff_app(const std::vector<std::string> &arguments)
{
    int output_pipe[2];
    if (pipe(output_pipe) != 0)
        throw std::runtime_error("failed to create subprocess pipe");

    const auto child = fork();
    if (child < 0)
    {
        close(output_pipe[0]);
        close(output_pipe[1]);
        throw std::runtime_error("failed to fork hotstuff-app");
    }
    if (child == 0)
    {
        close(output_pipe[0]);
        if (dup2(output_pipe[1], STDOUT_FILENO) < 0 ||
            dup2(output_pipe[1], STDERR_FILENO) < 0)
            _exit(126);
        close(output_pipe[1]);

        std::vector<std::string> owned_arguments;
        owned_arguments.reserve(arguments.size() + 1);
        owned_arguments.emplace_back(KAURI_HOTSTUFF_APP_PATH);
        owned_arguments.insert(
            owned_arguments.end(), arguments.begin(), arguments.end());
        std::vector<char *> raw_arguments;
        raw_arguments.reserve(owned_arguments.size() + 1);
        for (auto &argument : owned_arguments)
            raw_arguments.push_back(&argument[0]);
        raw_arguments.push_back(nullptr);
        execv(KAURI_HOTSTUFF_APP_PATH, raw_arguments.data());
        _exit(127);
    }

    close(output_pipe[1]);
    ProcessResult result;
    char buffer[4096];
    while (true)
    {
        const auto count = read(output_pipe[0], buffer, sizeof(buffer));
        if (count > 0)
        {
            result.output.append(buffer, static_cast<std::size_t>(count));
            continue;
        }
        if (count < 0 && errno == EINTR)
            continue;
        break;
    }
    close(output_pipe[0]);

    int wait_status = 0;
    while (waitpid(child, &wait_status, 0) < 0)
        if (errno != EINTR)
            throw std::runtime_error("failed to wait for hotstuff-app");
    result.status = WIFEXITED(wait_status)
                        ? WEXITSTATUS(wait_status)
                        : 128 + WTERMSIG(wait_status);
    return result;
}

struct AdaptiveV2Arguments
{
    std::vector<std::string> values;
    std::string temporary_directory;
    std::string structured_event_output;

    AdaptiveV2Arguments()
    {
        char directory_template[] =
            "/tmp/kauri-call-site-events-XXXXXX";
        const auto *created_directory = ::mkdtemp(directory_template);
        if (created_directory == nullptr)
            throw std::runtime_error(
                "failed to create structured-event fixture directory");
        temporary_directory = created_directory;
        structured_event_output =
            temporary_directory + "/replica-events.jsonl";
        const auto token = temporary_directory.substr(
            temporary_directory.find_last_of('/') + 1);
        values = {
            "--epoch-protocol-mode", "adaptive_v2",
            "--epoch-change-issuer-id", "0",
            "--epoch-change-issuer-public-key",
            "022543a7f8dd080a3e44c4fac62194129ac260a3896ee9d546bfb08bbb379067c1",
            "--epoch-change-minimum-activation-delay", "2",
            "--epoch-change-maximum-activation-delay", "20",
            "--epoch-change-maximum-block-extra-bytes", "4096",
            "--epoch-change-maximum-ancestry-blocks", "128",
            "--structured-event-run-id", "call-site-" + token,
            "--structured-event-source-instance", "replica-" + token,
            "--structured-event-output", structured_event_output,
            "--structured-event-commit-observer-id", "replica-0",
            "--structured-event-commit-observer-instance",
            "observer-" + token};
    }

    AdaptiveV2Arguments(const AdaptiveV2Arguments &) = delete;
    AdaptiveV2Arguments &operator=(const AdaptiveV2Arguments &) = delete;

    AdaptiveV2Arguments(AdaptiveV2Arguments &&other) noexcept
        : values(std::move(other.values)),
          temporary_directory(std::move(other.temporary_directory)),
          structured_event_output(
              std::move(other.structured_event_output))
    {
        other.temporary_directory.clear();
        other.structured_event_output.clear();
    }

    ~AdaptiveV2Arguments()
    {
        if (!structured_event_output.empty())
            ::unlink(structured_event_output.c_str());
        if (!temporary_directory.empty())
            ::rmdir(temporary_directory.c_str());
    }
};

AdaptiveV2Arguments valid_adaptive_v2_arguments()
{
    return AdaptiveV2Arguments{};
}

void set_option_value(
    std::vector<std::string> &arguments,
    const std::string &option,
    const std::string &value)
{
    for (std::size_t index = 0; index + 1 < arguments.size(); ++index)
    {
        if (arguments[index] != option)
            continue;
        arguments[index + 1] = value;
        return;
    }
    throw std::runtime_error("test option was not found");
}

void remove_option(
    std::vector<std::string> &arguments,
    const std::string &option)
{
    for (auto item = arguments.begin(); item != arguments.end(); ++item)
    {
        if (*item != option)
            continue;
        arguments.erase(item, item + 2);
        return;
    }
    throw std::runtime_error("test option was not found");
}

bool accepts_adaptive_v2(const std::string &contents)
{
    return contents.find("EpochProtocolMode::adaptive_v2") !=
               std::string::npos ||
           contents.find(
               "epoch_protocol_mode != EpochProtocolMode::legacy_static") !=
               std::string::npos ||
           contents.find("is_adaptive_epoch_mode") != std::string::npos;
}

} // namespace

TEST_CASE("HotStuffBase owns one explicitly selected epoch protocol binding",
          "[rem-d11][epoch-live-binding][hotstuff][intentional-red]")
{
    const auto header = source("include/hotstuff/hotstuff.h");
    const auto implementation = source("src/hotstuff.cpp");
    const auto constructor = function_body(
        implementation, "HotStuffBase::HotStuffBase(");
    const auto tree_switch = function_body(
        implementation, "ReconfigurationType HotStuffBase::isTreeSwitch(");

    CHECK(header.find("HotStuffEpochLiveBinding") != std::string::npos);
    CHECK(header.find(
              "EpochProtocolMode::legacy_static") != std::string::npos);
    REQUIRE_FALSE(constructor.empty());
    CHECK(contains_in_order(
        constructor,
        {"EpochProtocolMode::adaptive_v1",
         "install_adaptive_epoch_handlers",
         "else",
         "install_legacy_consensus_handlers"}));
    CHECK(count_occurrences(
              constructor, "install_adaptive_epoch_handlers") == 1);
    CHECK(count_occurrences(
              constructor, "install_legacy_consensus_handlers") == 1);
    REQUIRE_FALSE(tree_switch.empty());
    CHECK((contains_in_order(
               tree_switch,
               {"EpochProtocolMode::adaptive_v2",
                "return NO_SWITCH",
                "lastCheckedHeight"}) ||
           contains_in_order(
               tree_switch,
               {"epoch_protocol_mode != EpochProtocolMode::legacy_static",
                "return NO_SWITCH",
                "lastCheckedHeight"})));
}

TEST_CASE("adaptive pn handlers authenticate the connection before delegation",
          "[rem-d11][epoch-live-binding][network][intentional-red]")
{
    const auto implementation = source("src/hotstuff.cpp");

    for (const auto &expected : {
             std::pair<const char *, const char *>{
                 "void HotStuffBase::adaptive_stage_epoch_handler(",
                 "handle_stage("},
             {"void HotStuffBase::adaptive_arm_epoch_handler(",
              "handle_arm("}})
    {
        const auto handler = function_body(implementation, expected.first);
        CAPTURE(expected.first);
        REQUIRE_FALSE(handler.empty());
        CHECK(contains_in_order(
            handler,
            {"conn->get_peer_id()",
             "authorize_manager_peer",
             expected.second}));
        CHECK(count_occurrences(handler, expected.second) == 1);
    }

    for (const auto &expected : {
             std::pair<const char *, const char *>{
                 "void HotStuffBase::adaptive_propose_handler(",
                 "handle_proposal("},
             {"void HotStuffBase::adaptive_vote_handler(", "handle_vote("},
             {"void HotStuffBase::adaptive_relay_handler(", "handle_relay("}})
    {
        const auto handler = function_body(implementation, expected.first);
        CAPTURE(expected.first);
        REQUIRE_FALSE(handler.empty());
        CHECK(contains_in_order(
            handler,
            {"conn->get_peer_id()",
             "peer_id_map.find",
             "authenticated_epoch_replica",
             expected.second}));
        CHECK(count_occurrences(handler, expected.second) == 1);
    }
}

TEST_CASE(
    "adaptive v2 core reports only over pinned authenticated pn transport",
    "[adaptive-v2][manager-reporting][pn][tls][lifecycle][retry]")
{
    const auto header = source("include/hotstuff/hotstuff.h");
    const auto implementation = source("src/hotstuff.cpp");
    const auto constructor = function_body(
        implementation, "HotStuffBase::HotStuffBase(");
    const auto connection = function_body(
        implementation, "bool HotStuffBase::conn_handler(");
    const auto authorize = function_body(
        implementation, "bool HotStuffBase::authorize_manager_peer(");
    const auto configure = function_body(
        implementation, "void HotStuffBase::configure_epoch_manager(");
    const auto initialize = function_body(
        implementation, "void HotStuffBase::initialize_adaptive_epoch_runtime(");
    const auto admit = function_body(
        implementation, "HotStuffBase::admit_exact_context(");
    const auto consensus = function_body(
        implementation, "void HotStuffBase::do_consensus(");
    const auto start = function_body(
        implementation, "void HotStuffBase::start(");
    const auto legacy_report = function_body(
        implementation, "void HotStuffBase::on_report_timer(");
    const auto bind = function_body(
        implementation,
        "void HotStuffBase::bind_adaptive_v2_manager_reporting_transport(");
    const auto evidence = function_body(
        implementation,
        "HotStuffBase::enqueue_adaptive_v2_evidence_report(");
    const auto readiness = function_body(
        implementation,
        "void HotStuffBase::enqueue_initial_adaptive_v2_readiness(");
    const auto initialized = function_body(
        implementation,
        "void HotStuffBase::report_adaptive_v2_runtime_initialized(");
    const auto committed = function_body(
        implementation,
        "void HotStuffBase::report_adaptive_v2_committed(");
    const auto schedule = function_body(
        implementation,
        "void HotStuffBase::schedule_adaptive_v2_reporting_flush(");
    const auto flush = function_body(
        implementation,
        "void HotStuffBase::flush_adaptive_v2_reporting(");
    const auto transmit = function_body(
        implementation,
        "HotStuffBase::transmit_adaptive_v2_report(");

    CHECK(contains_all(
        header,
        {"AdaptiveV2ReportingOutbox",
         "adaptive_v2_reporting_outbox",
         "adaptive_v2_reporting_flush_cancellation",
         "adaptive_v2_readiness_enqueued",
         "adaptive_v2_initialized_lifecycle_reports",
         "epoch_manager_address"}));

    REQUIRE_FALSE(constructor.empty());
    CHECK(contains_in_order(
        constructor,
        {"EpochProtocolMode::adaptive_v2",
         "AdaptiveV2ReportingOutboxConfig",
         "adaptive_v2_reporting_outbox",
         "AdaptiveV2ResponseEvidenceBridge"}));
    CHECK(contains_all(
        constructor,
        {"adaptive_v2_reporting_maximum_delivery_attempts",
         "initial_retry_backoff_ns",
         "maximum_retry_backoff_ns"}));
    CHECK(contains_in_order(
        constructor,
        {"if (epoch_protocol_mode != EpochProtocolMode::adaptive_v2)",
         "rn.start()",
         "rn.connect_sync(reputation_addr)"}));

    REQUIRE_FALSE(connection.empty());
    CHECK(contains_in_order(
        connection,
        {"conn->get_peer_cert()",
         "EpochProtocolMode::adaptive_v2",
         "cert != nullptr",
         "valid_tls_certs.count"}));

    REQUIRE_FALSE(authorize.empty());
    CHECK(authorize.find("is_adaptive_epoch_mode") != std::string::npos);
    REQUIRE_FALSE(configure.empty());
    CHECK(contains_in_order(
        configure,
        {"is_adaptive_epoch_mode",
         "manager_peer.is_null()",
         "epoch_manager_peer.has_value()",
         "epoch_manager_address.has_value()",
         "epoch manager identity cannot be repinned",
         "valid_tls_certs.insert",
         "pn.add_peer(manager_peer)",
         "pn.conn_peer(manager_peer)",
         "EpochProtocolMode::adaptive_v2",
         "bind_adaptive_v2_manager_reporting_transport",
         "schedule_adaptive_v2_reporting_flush"}));

    REQUIRE_FALSE(initialize.empty());
    CHECK(contains_in_order(
        initialize,
        {"adaptive_epoch_runtime = std::move(runtime)",
         "epoch_live_binding =",
         "enqueue_initial_adaptive_v2_readiness"}));
    REQUIRE_FALSE(readiness.empty());
    CHECK(contains_all(
        readiness,
        {"adaptive_v2_readiness_enqueued",
         "configuration.epoch_number != 0",
         "configuration.tree_id != 0",
         "generation.has_value()",
         "*generation != 1",
         "enqueue_readiness(",
         "configuration, *generation, 0",
         "schedule_adaptive_v2_reporting_flush"}));

    REQUIRE_FALSE(admit.empty());
    CHECK(contains_in_order(
        admit,
        {"initialize_accumulator(",
         "report_adaptive_v2_runtime_initialized(metadata.key)"}));
    REQUIRE_FALSE(initialized.empty());
    CHECK(contains_all(
        initialized,
        {"NormalProposalRuntimeInitialized",
         "enqueue_lifecycle",
         "adaptive_v2_initialized_lifecycle_reports",
         "schedule_adaptive_v2_reporting_flush"}));
    REQUIRE_FALSE(consensus.empty());
    CHECK(contains_in_order(
        consensus,
        {"cache_adaptive_v2_commit(blk, keys)",
         "report_adaptive_v2_committed("}));
    REQUIRE_FALSE(committed.empty());
    CHECK(contains_all(
        committed,
        {"ProposalCommitted",
         "enqueue_lifecycle",
         "schedule_adaptive_v2_reporting_flush"}));

    REQUIRE_FALSE(bind.empty());
    CHECK(bind.find("adaptive_v2_response_evidence->bind_transport(") !=
          std::string::npos);
    REQUIRE_FALSE(evidence.empty());
    CHECK(contains_in_order(
        evidence,
        {"adaptive_v2_reporting_outbox->enqueue_evidence(",
         "AdaptiveV2ReportingEnqueueStatus::queued",
         "schedule_adaptive_v2_reporting_flush",
         "EvidenceTransportResult::accepted"}));
    CHECK(evidence.find("rn.send_msg") == std::string::npos);

    REQUIRE_FALSE(transmit.empty());
    CHECK(contains_all(
        transmit,
        {"authorize_manager_peer",
         "pn.get_peer_conn",
         "is_terminated()",
         "get_peer_cert()",
         "PeerId(*manager_certificate) != manager_peer",
         "MsgAdaptiveV2ReadinessNotice",
         "MsgProposalLifecycleNotice",
         "MsgEvidenceReport",
         "pn.send_msg",
         "manager_connection"}));
    CHECK(transmit.find("rn.send_msg") == std::string::npos);
    REQUIRE_FALSE(schedule.empty());
    CHECK(contains_all(
        schedule,
        {"aggregation_scheduler->schedule_after(",
         "exact_runtime_access",
         "flush_adaptive_v2_reporting"}));
    REQUIRE_FALSE(flush.empty());
    CHECK(contains_all(
        flush,
        {"begin_delivery(",
         "transmit_adaptive_v2_report(",
         "acknowledge_delivery(",
         "release_terminal(",
         "retry_not_due",
         "schedule_adaptive_v2_reporting_flush"}));

    REQUIRE_FALSE(start.empty());
    CHECK(contains_in_order(
        start,
        {"if (epoch_protocol_mode != EpochProtocolMode::adaptive_v2)",
         "ev_report_timer = TimerEvent",
         "ev_report_timer.add(report_period)"}));
    REQUIRE_FALSE(legacy_report.empty());
    CHECK(contains_in_order(
        legacy_report,
        {"EpochProtocolMode::adaptive_v2", "return", "rn.send_msg"}));
}

TEST_CASE("live topology is prepared before the nofail exact-height swap",
          "[rem-d11][epoch-live-binding][two-phase][intentional-red]")
{
    const auto transaction = source("src/epoch_runtime_wiring.cpp");
    const auto live = source("src/epoch_live_binding.cpp");
    const auto prepare = function_body(
        transaction, "HotStuffEpochRuntimeTransaction::prepare(");
    const auto commit = function_body(
        transaction, "void HotStuffEpochRuntimeTransaction::commit(");
    const auto apply = function_body(
        live, "void HotStuffEpochLiveState::apply_update(");

    REQUIRE_FALSE(prepare.empty());
    REQUIRE_FALSE(commit.empty());
    REQUIRE_FALSE(apply.empty());
    CHECK(prepare.find("live_state") != std::string::npos);
    CHECK(prepare.find(".prepare(") != std::string::npos);
    CHECK(commit.find("live_state") != std::string::npos);
    CHECK(commit.find(".arm(") != std::string::npos);
    for (const auto *forbidden : {
             "find_exact_runtime_tree",
             "TreeNetwork(",
             "push_back(",
             "emplace(",
             "activate_leader_view"})
    {
        CAPTURE(forbidden);
        CHECK(apply.find(forbidden) == std::string::npos);
    }
}

TEST_CASE("the actual commit path delegates once to the live binding",
          "[rem-d11][epoch-live-binding][commit][intentional-red]")
{
    const auto implementation = source("src/hotstuff.cpp");
    const auto consensus = function_body(
        implementation, "void HotStuffBase::do_consensus(");

    REQUIRE_FALSE(consensus.empty());
    const std::string delegation =
        "epoch_live_binding->on_predecessor_commit(";
    const auto first = consensus.find(delegation);
    REQUIRE(first != std::string::npos);
    CHECK(consensus.find(delegation, first + delegation.size()) ==
          std::string::npos);
}

TEST_CASE("adaptive outbound consensus uses exact envelopes and legacy bytes remain",
          "[rem-d11][epoch-live-binding][outbound][intentional-red]")
{
    const auto header = source("include/hotstuff/hotstuff.h");
    const auto implementation = source("src/hotstuff.cpp");
    const auto wiring = source("src/epoch_live_binding.cpp");
    const auto encoder = function_body(
        wiring, "bytearray_t adaptive_epoch_consensus_message(");
    const auto generation_lookup = function_body(
        implementation,
        "HotStuffBase::find_exact_runtime_generation(");

    REQUIRE_FALSE(encoder.empty());
    CHECK(contains_all(
        encoder,
        {"configuration",
         "generation",
         "encode_epoch_consensus_envelope"}));
    CHECK(header.find("find_exact_runtime_generation") !=
          std::string::npos);
    REQUIRE_FALSE(generation_lookup.empty());
    CHECK(contains_all(
        generation_lookup,
        {"may_drain_exact_context", "topology.find_generation"}));

    for (const auto &expected : {
             std::pair<const char *, const char *>{
                 "void HotStuffBase::do_broadcast_proposal(",
                 "MsgPropose("},
             {"void HotStuffBase::do_vote(", "MsgVote("},
             {"bool HotStuffBase::send_exact_relay(", "MsgRelay("}})
    {
        const auto outbound = function_body(implementation, expected.first);
        CAPTURE(expected.first);
        REQUIRE_FALSE(outbound.empty());
        CHECK(contains_all(
            outbound,
            {"is_adaptive_epoch_mode(",
             "find_exact_runtime_generation(",
             "adaptive_epoch_consensus_message(",
             expected.second}));
        CHECK(outbound.find("activation.active_effect()") ==
              std::string::npos);
    }

    const auto relay = function_body(
        implementation, "void HotStuffBase::relay_once(");
    REQUIRE_FALSE(relay.empty());
    CHECK(contains_all(
        relay,
        {"EpochProtocolMode::adaptive_v1",
         "proposal.wire_payload",
         "MsgPropose("}));
}

TEST_CASE("adaptive commit markers retain the committed proposal configuration",
          "[rem-d11][epoch-live-binding][markers][intentional-red]")
{
    const auto header = source("include/hotstuff/hotstuff.h");
    const auto implementation = source("src/hotstuff.cpp");
    const auto marker = function_body(
        implementation,
        "void HotStuffBase::record_adaptive_commit_marker(");
    const auto consensus = function_body(
        implementation, "void HotStuffBase::do_consensus(");

    CHECK(header.find(
              "const std::vector<ProposalKey> &committed_keys") !=
          std::string::npos);
    REQUIRE_FALSE(marker.empty());
    CHECK(contains_all(
        marker,
        {"committed_keys",
         "key.configuration",
         "find_exact_runtime_tree(key.configuration)"}));
    CHECK(marker.find("activation.active_effect()") == std::string::npos);

    REQUIRE_FALSE(consensus.empty());
    CHECK(contains_in_order(
        consensus,
        {"close_committed_block(blk->get_hash())",
         "record_adaptive_commit_marker(blk, keys)",
         "proposal_admission->retire_proposal(key)"}));
}

TEST_CASE("adaptive v2 exposes one fail-closed pre-vote semantic gate",
          "[c08][epoch-change][pre-vote][intentional-red]")
{
    const auto implementation = source("src/hotstuff.cpp");
    const auto gate = function_body(
        implementation,
        "HotStuffBase::pre_vote_epoch_change_gate(");

    REQUIRE_FALSE(gate.empty());
    CHECK(contains_all(
        gate,
        {"EpochProtocolMode::adaptive_v2",
         "evaluate_epoch_change_proposal_chain(",
         "EpochChangeProposalDisposition::accepted",
         "EpochChangeProposalDisposition::duplicate",
         "EpochChangeProposalDisposition::defer",
         "EpochChangeProposalDisposition::rejected"}));
    CHECK(contains_in_order(
        gate,
        {"case EpochChangeProposalDisposition::defer:",
         "if (!result.recovery_request)",
         "return reject_epoch_change_gate();",
         "return result;"}));
}

TEST_CASE("active proposals pass the semantic gate before protocol mutation",
          "[c08][epoch-change][pre-vote][integration][intentional-red]")
{
    const auto implementation = source("src/hotstuff.cpp");
    const auto local = function_body(
        implementation, "bool HotStuffBase::admit_local(");
    const auto remote = function_body(
        implementation, "void HotStuffBase::process_active(");
    const auto ingress = function_body(
        implementation, "void HotStuffBase::propose_handler(");

    REQUIRE_FALSE(local.empty());
    CHECK(contains_in_order(
        local,
        {"pre_vote_epoch_change_gate(", "admit_exact_context("}));

    REQUIRE_FALSE(remote.empty());
    CHECK(contains_in_order(
        remote,
        {"delivered->get_hash() != metadata.key.block_hash",
         "pre_vote_epoch_change_gate(",
         "EpochChangeProposalDisposition::defer",
         "retain_deferred_epoch_change(",
         "return;",
         "EpochChangeProposalDisposition::rejected",
         "abort();",
         "admit_exact_context(",
         "ProposalContextOrigin::remote",
         "on_receive_proposal(parsed)",
         "create_expected_vote_state(metadata.key)",
         "start_latency_deadline(metadata.key)",
         "start_aggregation_timer(metadata.key)"}));

    const auto gate = remote.find("pre_vote_epoch_change_gate(");
    const auto context = remote.find(
        "admit_exact_context(", gate);
    REQUIRE(gate != std::string::npos);
    REQUIRE(context != std::string::npos);
    const auto fail_closed_path = remote.substr(gate, context - gate);
    CHECK(contains_all(
        fail_closed_path,
        {"gate.recovery_request",
         "retain_deferred_epoch_change(",
         "abort();",
         "return;"}));
    for (const auto *forbidden : {
             "admit_exact_context(",
             "on_receive_proposal(parsed)",
             "create_expected_vote_state(",
             "start_latency_deadline(",
             "start_aggregation_timer("})
    {
        CAPTURE(forbidden);
        CHECK(fail_closed_path.find(forbidden) == std::string::npos);
    }

    REQUIRE_FALSE(ingress.empty());
    CHECK(ingress.find("pre_vote_epoch_change_gate(") ==
          std::string::npos);
}

TEST_CASE("adaptive v2 definition recovery is bounded and digest coalesced",
          "[c08][epoch-change][definition-recovery][intentional-red]")
{
    const auto header = source("include/hotstuff/hotstuff.h");
    const auto implementation = source("src/hotstuff.cpp");
    const auto retain = function_body(
        implementation,
        "HotStuffBase::retain_deferred_epoch_change(");
    const auto request = function_body(
        implementation,
        "HotStuffBase::send_epoch_definition_request(");

    CHECK(contains_all(
        header,
        {"struct DeferredEpochDefinitionRecovery",
         "EpochDefinitionRequest request;",
         "bool request_live",
         "std::map<ProposalKey, BufferedProposal> proposals;",
         "maximum_pending_epoch_definition_digests",
         "maximum_deferred_epoch_change_proposals",
         "deferred_epoch_definition_recoveries"}));

    REQUIRE_FALSE(retain.empty());
    CHECK(contains_all(
        retain,
        {"EpochProtocolMode::adaptive_v2",
         "request.successor_epoch_digest",
         "maximum_pending_epoch_definition_digests",
         "maximum_deferred_epoch_change_proposals",
         ".emplace(",
         "send_epoch_definition_request("}));

    REQUIRE_FALSE(request.empty());
    CHECK(contains_in_order(
        request,
        {"peer_id_map",
         "MsgEpochDefinitionRequest message(",
         "pn.send_msg("}));
    CHECK(request.find("epoch_manager_peer") == std::string::npos);
}

TEST_CASE("adaptive v2 recovery handlers authenticate and retry off ingress",
          "[c08][epoch-change][definition-recovery][network][intentional-red]")
{
    const auto implementation = source("src/hotstuff.cpp");
    const auto constructor = function_body(
        implementation, "HotStuffBase::HotStuffBase(");
    const auto install = function_body(
        implementation,
        "HotStuffBase::install_adaptive_v2_definition_handlers(");
    const auto request = function_body(
        implementation,
        "HotStuffBase::adaptive_definition_request_handler(");
    const auto reply = function_body(
        implementation,
        "HotStuffBase::adaptive_definition_reply_handler(");
    const auto queue = function_body(
        implementation,
        "HotStuffBase::queue_deferred_epoch_change_retries(");
    const auto retry = function_body(
        implementation,
        "HotStuffBase::retry_deferred_epoch_changes(");

    REQUIRE_FALSE(constructor.empty());
    CHECK(contains_in_order(
        constructor,
        {"EpochProtocolMode::adaptive_v2",
         "install_adaptive_consensus_handlers();",
         "install_adaptive_v2_definition_handlers();"}));

    REQUIRE_FALSE(install.empty());
    CHECK(contains_all(
        install,
        {"adaptive_definition_request_handler",
         "adaptive_definition_reply_handler"}));
    CHECK(install.find("adaptive_stage_epoch_handler") ==
          std::string::npos);
    CHECK(install.find("adaptive_arm_epoch_handler") ==
          std::string::npos);

    REQUIRE_FALSE(request.empty());
    CHECK(contains_in_order(
        request,
        {"conn->get_peer_id()",
         "peer_id_map.find(peer)",
         "decode_epoch_definition_request(",
         "find_epoch_by_digest(",
         "kEpochDefinitionSchemaVersionV2",
         "MsgEpochDefinitionReply response(",
         "pn.send_msg("}));

    REQUIRE_FALSE(reply.empty());
    CHECK(contains_in_order(
        reply,
        {"conn->get_peer_id()",
         "peer_id_map.find(peer)",
         "decode_epoch_definition_reply(",
         "deferred_epoch_definition_recoveries.find(",
         "request_live",
         "stage_available_v2(",
         "successor_epoch_digest",
         "queue_deferred_epoch_change_retries("}));

    REQUIRE_FALSE(queue.empty());
    CHECK(queue.find("tcall.async_call(") != std::string::npos);
    REQUIRE_FALSE(retry.empty());
    CHECK(retry.find("process_active(") != std::string::npos);
    CHECK(retry.find("process_claimed_active(") == std::string::npos);
    CHECK(contains_in_order(
        retry,
        {"try",
         "proposals.reserve(",
         "process_active(",
         "catch (...)",
         "deferred_epoch_definition_recoveries.find(",
         "request_live = true",
         "send_epoch_definition_request("}));
}

TEST_CASE(
    "REM-D11 committed definition recovery is exact and independent",
    "[rem-d11][epoch-change][committed-definition-recovery][source-wiring]")
{
    const auto header = source("include/hotstuff/hotstuff.h");
    const auto implementation = source("src/hotstuff.cpp");
    const auto state = function_body(
        header, "struct CommittedEpochDefinitionRecovery");
    const auto retain = function_body(
        implementation,
        "HotStuffBase::retain_committed_epoch_definition_recovery(");
    const auto request = function_body(
        implementation,
        "HotStuffBase::send_epoch_definition_request(");
    const auto reply = function_body(
        implementation,
        "HotStuffBase::adaptive_definition_reply_handler(");
    const auto recover = function_body(
        implementation,
        "HotStuffBase::recover_committed_epoch_definition(");
    const auto retire_deferred = function_body(
        implementation,
        "HotStuffBase::retire_deferred_epoch_changes_for_block(");
    const auto post_commit = function_body(
        implementation, "void HotStuffBase::do_post_block_commit(");

    REQUIRE_FALSE(state.empty());
    CHECK(contains_all(
        state,
        {"EpochDefinitionRequest request;",
         "AuthorizedEpochChange command;",
         "uint256_t command_block_hash;",
         "uint256_t payload_digest;",
         "std::uint64_t command_commit_height",
         "std::uint64_t activation_height",
         "block_t activation_block;",
         "bool definition_recovered"}));
    CHECK(header.find(
              "std::optional<CommittedEpochDefinitionRecovery>") !=
          std::string::npos);

    REQUIRE_FALSE(retain.empty());
    CHECK(contains_in_order(
        retain,
        {"record.command_commit_height != block->get_height()",
         "record.activation_height <",
         "EpochDefinitionRequest request{",
         "kEpochWireSchemaVersionV2",
         "EpochProtocolMode::adaptive_v2",
         "command.payload.successor_epoch_digest",
         "committed_epoch_definition_recovery.emplace(",
         "block->get_hash()",
         "record.command_commit_height",
         "record.activation_height",
         "send_epoch_definition_request(request)"}));
    CHECK(retain.find("deferred_epoch_definition_recoveries") ==
          std::string::npos);

    REQUIRE_FALSE(request.empty());
    CHECK(contains_in_order(
        request,
        {"peer_id_map",
         "authenticated.second >= fixed_membership.size()",
         "fixed_membership[authenticated.second] != replica",
         "targets.emplace_back(replica, authenticated.first)",
         "std::sort(",
         "target.first == previous",
         "MsgEpochDefinitionRequest message(",
         "request, epoch_wire_limits",
         "pn.send_msg(message, target.second)"}));

    REQUIRE_FALSE(reply.empty());
    CHECK(contains_in_order(
        reply,
        {"conn->get_peer_id()",
         "peer_id_map.find(peer)",
         "authenticated->second >= fixed_membership.size()",
         "fixed_membership[authenticated->second] !=",
         "decode_epoch_definition_reply(",
         "const bool deferred_recovery_live =",
         "const bool committed_recovery_live =",
         "committed_epoch_definition_recovery->request",
         "successor_epoch_digest",
         "canonical_serialize_epoch(decoded.value->definition)",
         "DataStream(canonical_definition).get_hash() !=",
         "payload.successor_epoch_number",
         "payload.predecessor_epoch_digest",
         "stage_available_v2(",
         "staged.definition->canonical_serialization() !=",
         "if (committed_recovery_live &&",
         "recover_committed_epoch_definition(*staged.definition)",
         "if (deferred_recovery_live)",
         "queue_deferred_epoch_change_retries("}));

    REQUIRE_FALSE(recover.empty());
    CHECK(contains_in_order(
        recover,
        {"definition.schema_version() !=",
         "kEpochDefinitionSchemaVersionV2",
         "definition.activation_height() != 0",
         "definition.epoch_number() !=",
         "payload.successor_epoch_number",
         "definition.previous_epoch_digest() !=",
         "payload.predecessor_epoch_digest",
         "definition.epoch_digest() !=",
         "payload.successor_epoch_digest",
         "exact_epochs->find_epoch_by_digest(",
         "definition.canonical_serialization().empty()",
         "DataStream(definition.canonical_serialization()).get_hash() !=",
         "prepare_committed_v2(",
         "record_committed_v2(",
         "command, recovery.command_commit_height",
         "replayed.record->command_commit_height !=",
         "recovery.command_commit_height",
         "replayed.record->activation_height !=",
         "recovery.activation_height",
         "recovery.definition_recovered = true",
         "recovery.activation_block == nullptr",
         "recovery.activation_block->get_height() !=",
         "recovery.activation_height",
         "on_v2_post_block_commit(",
         "recovery.activation_height",
         "payload.predecessor_epoch_digest",
         "const auto activation_block = recovery.activation_block",
         "finish_adaptive_epoch_commit(activation_block, activation)",
         "reset_committed_epoch_definition_recovery()"}));
    CHECK(recover.find("deferred_epoch_definition_recoveries") ==
          std::string::npos);

    REQUIRE_FALSE(retire_deferred.empty());
    CHECK(retire_deferred.find(
              "committed_epoch_definition_recovery") ==
          std::string::npos);

    REQUIRE_FALSE(post_commit.empty());
    CHECK(contains_in_order(
        post_commit,
        {"record_committed_v2(command, blk->get_height())",
         "const bool exact_existing_recovery",
         "recorded.record->command_commit_height ==",
         "->command_commit_height",
         "recorded.record->activation_height ==",
         "->activation_height",
         "const bool recoverable_missing_definition",
         "ActivationRecordDisposition::missing_definition",
         "recorded.record.has_value()",
         "const bool recoverable_missing_definition_duplicate",
         "successor == nullptr",
         "ActivationRecordDisposition::duplicate",
         "exact_existing_recovery",
         "!recoverable_missing_definition_duplicate",
         "if ((recorded.disposition ==",
         "ActivationRecordDisposition::recorded",
         "ActivationRecordDisposition::missing_definition",
         "emit_epoch_command_committed_event(",
         "if (recoverable_missing_definition &&",
         "!retain_committed_epoch_definition_recovery(",
         "pending_committed_epoch_change.reset()",
         "blk->get_height() == recovery.activation_height",
         "recovery.activation_block = blk",
         "recover_committed_epoch_definition("}));
    CHECK(contains_in_order(
        post_commit,
        {"const bool exact_recovery_duplicate",
         "recovery.payload_digest ==",
         "pending_committed_epoch_change->payload_digest",
         "recovery.request.successor_epoch_digest ==",
         "command.payload.successor_epoch_digest",
         "encode_authorized_epoch_change(recovery.command) ==",
         "encode_authorized_epoch_change(command)",
         "if (!exact_recovery_duplicate)",
         "ActivationBlockReason::",
         "conflicting_activation_record"}));
    CHECK(count_occurrences(
              post_commit,
              "retain_committed_epoch_definition_recovery(") == 1);
    CHECK(count_occurrences(
              post_commit, "emit_epoch_command_committed_event(") == 1);
    CHECK(post_commit.find("send_epoch_definition_request(") ==
          std::string::npos);
    CHECK(post_commit.find(
              "ActivationBlockReason::missing_definition") ==
          std::string::npos);
}

TEST_CASE(
    "REM-D11 committed definition recovery retries until a terminal path",
    "[rem-d11][epoch-change][committed-definition-recovery][retry]"
    "[source-wiring][intentional-red]")
{
    const auto header = source("include/hotstuff/hotstuff.h");
    const auto implementation = source("src/hotstuff.cpp");
    const auto state = function_body(
        header, "struct CommittedEpochDefinitionRecovery");
    const auto retain = function_body(
        implementation,
        "HotStuffBase::retain_committed_epoch_definition_recovery(");
    const auto retry_delay = function_body(
        implementation,
        "committed_epoch_definition_retry_delay(");
    const auto schedule = function_body(
        implementation,
        "HotStuffBase::schedule_committed_epoch_definition_retry(");
    const auto dispatch = function_body(
        implementation,
        "HotStuffBase::dispatch_committed_epoch_definition_retry(");
    const auto cancel = function_body(
        implementation,
        "HotStuffBase::cancel_committed_epoch_definition_retry(");
    const auto reset = function_body(
        implementation,
        "HotStuffBase::reset_committed_epoch_definition_recovery(");
    const auto recover = function_body(
        implementation,
        "HotStuffBase::recover_committed_epoch_definition(");
    const auto initialize = function_body(
        implementation,
        "HotStuffBase::initialize_committed_epoch_change_history(");
    const auto post_commit = function_body(
        implementation, "void HotStuffBase::do_post_block_commit(");
    const auto destructor = function_body(
        implementation, "HotStuffBase::~HotStuffBase(");

    REQUIRE_FALSE(state.empty());
    CHECK(contains_all(
        state,
        {"std::uint64_t retry_generation",
         "std::uint64_t retry_attempts",
         "AggregationScheduler::Cancellation retry_cancellation"}));
    CHECK(count_occurrences(
              state, "AggregationScheduler::Cancellation") == 1);
    CHECK(state.find("std::vector<") == std::string::npos);
    CHECK(state.find("std::map<") == std::string::npos);
    CHECK(contains_all(
        header,
        {"schedule_committed_epoch_definition_retry()",
         "dispatch_committed_epoch_definition_retry(",
         "cancel_committed_epoch_definition_retry()",
         "reset_committed_epoch_definition_recovery()"}));

    REQUIRE_FALSE(retry_delay.empty());
    CHECK(contains_all(
        retry_delay,
        {"committed_epoch_definition_retry_base_delay",
         "committed_epoch_definition_retry_maximum_delay",
         "std::min",
         "completed_attempts"}));
    CHECK(retry_delay.find("maximum_retry_attempts") ==
          std::string::npos);
    CHECK(retry_delay.find("retry_exhausted") == std::string::npos);

    REQUIRE_FALSE(retain.empty());
    CHECK(contains_in_order(
        retain,
        {"EpochDefinitionRequest request{",
         "command.payload.successor_epoch_digest",
         "committed_epoch_definition_recovery.emplace(",
         "send_epoch_definition_request(request)",
         "schedule_committed_epoch_definition_retry()"}));

    REQUIRE_FALSE(schedule.empty());
    CHECK(contains_all(
        schedule,
        {"committed_epoch_definition_recovery",
         "aggregation_scheduler",
         "retry_cancellation",
         "retry_generation",
         "retry_attempts",
         "committed_epoch_definition_retry_delay(",
         "schedule_after(",
         "dispatch_committed_epoch_definition_retry(",
         "command_block_hash",
         "request",
         "successor_epoch_digest"}));
    CHECK(schedule.find("maximum_retry_attempts") == std::string::npos);
    CHECK(schedule.find("retry_exhausted") == std::string::npos);

    REQUIRE_FALSE(dispatch.empty());
    CHECK(contains_in_order(
        dispatch,
        {"committed_epoch_definition_recovery",
         "retry_generation != retry_generation",
         "command_block_hash != command_block_hash",
         "request.successor_epoch_digest !=",
         "successor_epoch_digest",
         "retry_cancellation = {}",
         "send_epoch_definition_request(recovery.request)",
         "std::numeric_limits<std::uint64_t>::max()",
         "++recovery.retry_attempts",
         "schedule_committed_epoch_definition_retry()"}));
    CHECK(dispatch.find("maximum_retry_attempts") ==
          std::string::npos);
    CHECK(dispatch.find("retry_exhausted") == std::string::npos);
    CHECK(dispatch.find("record_committed_v2(") ==
          std::string::npos);
    CHECK(dispatch.find("retain_committed_epoch_definition_recovery(") ==
          std::string::npos);
    CHECK(dispatch.find("do_vote(") == std::string::npos);
    CHECK(dispatch.find("do_consensus(") == std::string::npos);
    CHECK(dispatch.find("certificate") == std::string::npos);

    REQUIRE_FALSE(cancel.empty());
    CHECK(contains_in_order(
        cancel,
        {"committed_epoch_definition_recovery",
         "std::move(",
         "committed_epoch_definition_recovery->retry_cancellation",
         "committed_epoch_definition_recovery->retry_cancellation = {}",
         "cancellation()"}));

    REQUIRE_FALSE(reset.empty());
    CHECK(contains_in_order(
        reset,
        {"cancel_committed_epoch_definition_retry()",
         "committed_epoch_definition_recovery.reset()"}));

    REQUIRE_FALSE(recover.empty());
    CHECK(contains_in_order(
        recover,
        {"record_committed_v2(",
         "command, recovery.command_commit_height",
         "recovery.definition_recovered = true",
         "cancel_committed_epoch_definition_retry()",
         "recovery.activation_block->get_height() !=",
         "recovery.activation_height",
         "const auto activation_block = recovery.activation_block",
         "finish_adaptive_epoch_commit(activation_block, activation)",
         "reset_committed_epoch_definition_recovery()"}));

    REQUIRE_FALSE(post_commit.empty());
    const auto fail_closed = function_body(
        post_commit, "const auto fail_closed =");
    REQUIRE_FALSE(fail_closed.empty());
    CHECK(contains_in_order(
        fail_closed,
        {"pending_committed_epoch_change.reset()",
         "reset_committed_epoch_definition_recovery()",
         "adaptive_epoch_runtime->adapter.fail_committed_v2(reason)"}));

    REQUIRE_FALSE(initialize.empty());
    CHECK(initialize.find(
              "reset_committed_epoch_definition_recovery()") !=
          std::string::npos);
    REQUIRE_FALSE(destructor.empty());
    CHECK(destructor.find(
              "reset_committed_epoch_definition_recovery()") !=
          std::string::npos);
}

TEST_CASE("deferred recovery is cleared only on deterministic terminal paths",
          "[c08][epoch-change][definition-recovery][lifecycle][intentional-red]")
{
    const auto implementation = source("src/hotstuff.cpp");
    const auto remote = function_body(
        implementation, "void HotStuffBase::process_active(");
    const auto activate = function_body(
        implementation,
        "void HotStuffBase::activate_proposal_configuration(");
    const auto retire = function_body(
        implementation,
        "void HotStuffBase::advance_committed_retirement_floor(");
    const auto retire_block = function_body(
        implementation,
        "void HotStuffBase::retire_deferred_epoch_changes_for_block(");
    const auto consensus = function_body(
        implementation, "void HotStuffBase::do_consensus(");
    const auto reply = function_body(
        implementation,
        "HotStuffBase::adaptive_definition_reply_handler(");
    const auto destructor = function_body(
        implementation, "HotStuffBase::~HotStuffBase(");

    REQUIRE_FALSE(remote.empty());
    CHECK(contains_in_order(
        remote,
        {"case EpochChangeProposalDisposition::accepted:",
         "case EpochChangeProposalDisposition::duplicate:",
         "erase_deferred_epoch_change(",
         "case EpochChangeProposalDisposition::defer:",
         "retain_deferred_epoch_change(",
         "case EpochChangeProposalDisposition::rejected:",
         "abort();"}));

    REQUIRE_FALSE(activate.empty());
    CHECK(activate.find(
              "retire_deferred_epoch_changes_before_epoch(") ==
          std::string::npos);
    REQUIRE_FALSE(retire.empty());
    CHECK(retire.find(
              "retire_deferred_epoch_changes_before_epoch(") !=
          std::string::npos);

    REQUIRE_FALSE(retire_block.empty());
    CHECK(contains_in_order(
        retire_block,
        {"proposal->first.block_hash != block_hash",
         "proposal_admission->retire_proposal(key)",
         "purge_pending_exact_contributions(key)",
         "proposal = proposals.erase(proposal)",
         "deferred_epoch_definition_recoveries.erase(recovery)"}));

    REQUIRE_FALSE(consensus.empty());
    CHECK(contains_in_order(
        consensus,
        {"retire_deferred_epoch_changes_for_block(blk->get_hash())",
         "close_committed_block(blk->get_hash())",
         "for (const auto &key : keys)",
         "erase_deferred_epoch_change(key)",
         "proposal_admission->retire_proposal(key)"}));

    REQUIRE_FALSE(reply.empty());
    CHECK(contains_in_order(
        reply,
        {"const bool deferred_recovery_live =",
         "const bool committed_recovery_live =",
         "if (!deferred_recovery_live && !committed_recovery_live)",
         "return;"}));

    REQUIRE_FALSE(destructor.empty());
    CHECK(contains_in_order(
        destructor,
        {"exact_runtime_access->close_and_wait()",
         "deferred_epoch_definition_recoveries.clear()",
         "deferred_epoch_change_proposal_count = 0",
         "proposal_contexts->shutdown()"}));
}

TEST_CASE("committed epoch history owns one coherent exact head snapshot",
          "[c08][epoch-change][pre-vote][committed-history]")
{
    const auto header = source("include/hotstuff/hotstuff.h");
    const auto implementation = source("src/hotstuff.cpp");
    const auto core = source("src/consensus.cpp");
    const auto constructor = function_body(
        implementation, "HotStuffBase::HotStuffBase(");
    const auto initialize = function_body(
        implementation,
        "HotStuffBase::initialize_committed_epoch_change_history(");
    const auto record = function_body(
        implementation,
        "HotStuffBase::record_committed_epoch_change_history(");
    const auto gate = function_body(
        implementation,
        "HotStuffBase::pre_vote_epoch_change_gate(");
    const auto consensus = function_body(
        implementation, "void HotStuffBase::do_consensus(");

    CHECK(contains_all(
        header,
        {"struct CommittedEpochChangeHistoryState",
         "block_t head;",
         "EpochChangeCommittedHistorySnapshot snapshot;",
         "committed_epoch_change_history;"}));
    REQUIRE_FALSE(constructor.empty());
    CHECK(constructor.find(
              "initialize_committed_epoch_change_history();") !=
          std::string::npos);

    REQUIRE_FALSE(initialize.empty());
    CHECK(contains_in_order(
        initialize,
        {"const auto &genesis = committed_head()",
         "genesis->get_decision()",
         "CommittedEpochChangeHistoryState{",
         "genesis",
         "EpochChangeCommittedHistorySnapshot{",
         "genesis->get_hash()",
         "genesis->get_height()"}));

    REQUIRE_FALSE(record.empty());
    CHECK(contains_in_order(
        record,
        {"const auto &previous = *committed_epoch_change_history",
         "parents.front() != previous.head",
         "extract_epoch_change_block_extra(",
         "committed_epoch_change_history =",
         "CommittedEpochChangeHistoryState{",
         "block",
         "EpochChangeCommittedHistorySnapshot{"}));

    REQUIRE_FALSE(gate.empty());
    CHECK(contains_in_order(
        gate,
        {"const auto &committed = committed_epoch_change_history",
         "*committed->head",
         "committed->snapshot"}));
    CHECK(gate.find("b_exec") == std::string::npos);
    CHECK(gate.find("committed_head()") == std::string::npos);

    REQUIRE_FALSE(consensus.empty());
    CHECK(contains_in_order(
        consensus,
        {"record_committed_epoch_change_history(blk)",
         "close_committed_block(blk->get_hash())"}));
    CHECK(contains_in_order(
        core,
        {"do_consensus(blk);", "b_exec = blk;"}));
}

TEST_CASE("committed adaptive v2 commands are cached after history advances",
          "[c08][epoch-change][commit-hook][cache][intentional-red]")
{
    const auto header = source("include/hotstuff/hotstuff.h");
    const auto implementation = source("src/hotstuff.cpp");
    const auto pending = function_body(
        header, "struct PendingCommittedEpochChange");
    const auto initialize = function_body(
        implementation,
        "HotStuffBase::initialize_committed_epoch_change_history(");
    const auto record = function_body(
        implementation,
        "HotStuffBase::record_committed_epoch_change_history(");

    REQUIRE_FALSE(pending.empty());
    CHECK(contains_all(
        pending,
        {"uint256_t block_hash;", "AuthorizedEpochChange command;"}));
    CHECK(header.find(
              "std::optional<PendingCommittedEpochChange>") !=
          std::string::npos);
    CHECK(header.find("pending_committed_epoch_change") !=
          std::string::npos);

    REQUIRE_FALSE(initialize.empty());
    CHECK(initialize.find(
              "pending_committed_epoch_change.reset()") !=
          std::string::npos);

    REQUIRE_FALSE(record.empty());
    CHECK(contains_in_order(
        record,
        {"EpochProtocolMode::adaptive_v2",
         "pending_committed_epoch_change.reset()",
         "const auto &previous = *committed_epoch_change_history",
         "previous.snapshot.committed_head_hash",
         "parents.front() != previous.head",
         "extract_epoch_change_block_extra(",
         "EpochChangeHistoryView{",
         "previous.snapshot.command",
         "previous.snapshot.command->payload_digest",
         "epoch_change_verifier->validate(",
         "*extracted.command",
         "*active_epoch",
         "*exact_epochs",
         "validation.disposition",
         "committed_epoch_change_history =",
         "CommittedEpochChangeHistoryState{",
         "if (extracted.command)",
         "pending_committed_epoch_change.emplace(",
         "block->get_hash()",
         "*extracted.command"}));
    CHECK(contains_all(
        record,
        {"EpochChangeDisposition::accepted",
         "EpochChangeDisposition::duplicate",
         "const bool available_definition",
         "validation.successor_definition != nullptr",
         "exact_epochs->find_epoch_by_digest(",
         "extracted.command->payload.successor_epoch_digest",
         "validation.successor_definition == successor",
         "EpochChangeDisposition::defer_missing_definition",
         "const bool recoverable_missing_definition",
         "validation.successor_definition == nullptr",
         "successor == nullptr",
         "validation.recovery_request.has_value()",
         "validation.recovery_request->successor_epoch_digest =="}));
    CHECK((contains_all(
               record,
               {"proposal_contexts->active_configuration()",
                "exact_epochs->find_epoch(",
                "active_configuration->epoch_number",
                "active_epoch->epoch_digest() !=",
                "active_configuration->epoch_digest"}) ||
           contains_all(
               record,
               {"adaptive_epoch_runtime->activation.active_effect()",
                "active.definition",
                "active.configuration.epoch_digest"})));
    CHECK(contains_in_order(
        record,
        {"previous.snapshot.command",
         "previous.snapshot.command->predecessor_epoch_digest ==",
         "active_epoch->epoch_digest()",
         "previous.snapshot.command->payload_digest",
         ": std::nullopt"}));

    const auto validation = record.find(
        "epoch_change_verifier->validate(");
    const auto history_assignment = record.find(
        "committed_epoch_change_history =", validation);
    const auto cache_assignment = record.find(
        "pending_committed_epoch_change.emplace(", history_assignment);
    REQUIRE(validation != std::string::npos);
    REQUIRE(history_assignment != std::string::npos);
    REQUIRE(cache_assignment != std::string::npos);
    CHECK(validation < history_assignment);
    CHECK(history_assignment < cache_assignment);

    const auto extracted_present = record.find(
        "if (extracted.disposition ==");
    const auto extracted_failure = record.find(
        "else if (extracted.disposition !=", extracted_present);
    REQUIRE(extracted_present != std::string::npos);
    REQUIRE(extracted_failure != std::string::npos);
    const auto present_path = record.substr(
        extracted_present, extracted_failure - extracted_present);
    CHECK(contains_all(
        present_path,
        {"EpochChangeExtraDisposition::present",
         "extracted.command",
         "extracted.payload_digest"}));
    CHECK(present_path.find(
              "pending_committed_epoch_change.emplace(") ==
          std::string::npos);
}

TEST_CASE("adaptive v2 activates only from the matching post-block command",
          "[c08][epoch-change][commit-hook][post-block][intentional-red]")
{
    const auto header = source("include/hotstuff/hotstuff.h");
    const auto implementation = source("src/hotstuff.cpp");
    const auto core = source("src/consensus.cpp");
    const auto consensus = function_body(
        implementation, "void HotStuffBase::do_consensus(");
    const auto admit_local = function_body(
        implementation, "bool HotStuffBase::admit_local(");
    const auto post_commit = function_body(
        implementation, "void HotStuffBase::do_post_block_commit(");
    const auto finish_commit = function_body(
        implementation,
        "void HotStuffBase::finish_adaptive_epoch_commit(");

    CHECK(header.find(
              "void do_post_block_commit(") !=
          std::string::npos);

    REQUIRE_FALSE(consensus.empty());
    CHECK(contains_in_order(
        consensus,
        {"EpochProtocolMode::adaptive_v1",
         "epoch_live_binding->on_predecessor_commit("}));
    CHECK(consensus.find("on_v2_post_block_commit(") ==
          std::string::npos);

    REQUIRE_FALSE(post_commit.empty());
    CHECK(contains_in_order(
        post_commit,
        {"EpochProtocolMode::adaptive_v2",
         "pending_committed_epoch_change",
         "block_hash != blk->get_hash()",
         "fail_closed(",
         "pending_committed_epoch_change",
         "block_hash == blk->get_hash()",
         "find_epoch_by_digest(",
         "command.payload.successor_epoch_digest",
         "prepare_committed_v2(*successor)",
         "record_committed_v2(command, blk->get_height())",
         "recoverable_missing_definition",
         "ActivationRecordDisposition::missing_definition",
         "ActivationRecordDisposition::recorded",
         "ActivationRecordDisposition::duplicate",
         "retain_committed_epoch_definition_recovery(",
         "pending_committed_epoch_change.reset()",
         "committed_epoch_definition_recovery",
         "recovery.activation_block = blk",
         "recover_committed_epoch_definition(",
         "activation.active_effect()",
         "post_block_height",
         "epoch_live_binding->on_v2_post_block_commit(",
         "post_block_height",
         "configuration.epoch_digest",
         "finish_adaptive_epoch_commit(blk, activation)"}));
    CHECK(contains_all(
        post_commit,
        {"successor == nullptr",
         "successor->schema_version()",
         "kEpochDefinitionSchemaVersionV2",
         "successor->epoch_number()",
         "command.payload.successor_epoch_number",
         "successor->epoch_digest()",
         "command.payload.successor_epoch_digest",
         "EpochIngressError::none"}));
    CHECK(count_occurrences(
              post_commit, "prepare_committed_v2(*successor)") == 1);
    CHECK(count_occurrences(
              post_commit, "record_committed_v2(command, blk->get_height())") ==
          1);
    CHECK(count_occurrences(
              post_commit, "on_v2_post_block_commit(") == 1);
    CHECK(post_commit.find(
              "ActivationBlockReason::missing_definition") ==
          std::string::npos);
    CHECK(post_commit.find(
              "ActivationBlockReason::invalid_activation_record") !=
          std::string::npos);
    const auto fail_close = function_body(
        post_commit, "const auto fail_closed =");
    REQUIRE_FALSE(fail_close.empty());
    CHECK(contains_in_order(
        fail_close,
        {"pending_committed_epoch_change.reset()",
         "reset_committed_epoch_definition_recovery()",
         "committed_epoch_change_history.reset()",
         "adaptive_epoch_runtime->adapter.fail_committed_v2(reason)"}));
    REQUIRE_FALSE(admit_local.empty());
    CHECK(contains_in_order(
        admit_local,
        {"EpochProtocolMode::adaptive_v2",
         "adaptive_epoch_runtime",
         "activation.admits_new_proposals()",
         "return false"}));
    REQUIRE_FALSE(finish_commit.empty());
    CHECK(contains_in_order(
        finish_commit,
        {"epoch_protocol_mode == EpochProtocolMode::adaptive_v2",
         "blk->get_height()",
         "definition->activation_height()",
         "emit_epoch_lifecycle_event(",
         "EpochLifecycleTransition::activated"}));
    const auto prepare_failure = function_body(
        post_commit, "if (prepared != EpochIngressError::none)");
    REQUIRE_FALSE(prepare_failure.empty());
    CHECK(contains_all(
        prepare_failure,
        {"fail_closed(",
         "ActivationBlockReason::invalid_activation_record"}));
    const auto record_call = post_commit.find(
        "record_committed_v2(command, blk->get_height())");
    const auto duplicate = post_commit.find(
        "ActivationRecordDisposition::duplicate", record_call);
    const auto successful_consume = post_commit.find(
        "pending_committed_epoch_change.reset()", duplicate);
    REQUIRE(record_call != std::string::npos);
    REQUIRE(duplicate != std::string::npos);
    REQUIRE(successful_consume != std::string::npos);
    CHECK(record_call < successful_consume);
    CHECK(post_commit.find("on_predecessor_commit(") ==
          std::string::npos);
    CHECK(post_commit.find("handle_arm(") == std::string::npos);
    CHECK(post_commit.find("ArmActivation") == std::string::npos);

    CHECK(contains_in_order(
        core,
        {"do_consensus(blk);",
         "do_decide(Finality(",
         "do_post_block_commit(blk, commit_batch_index);"}));
}

TEST_CASE("adaptive v2 emits exact structured commit and command evidence",
          "[adaptive-v2][structured-event][commit-hook][audit]")
{
    const auto header = source("include/hotstuff/hotstuff.h");
    const auto consensus_header = source("include/hotstuff/consensus.h");
    const auto implementation = source("src/hotstuff.cpp");
    const auto consensus = source("src/consensus.cpp");
    const auto binding = function_body(
        implementation, "void HotStuffBase::bind_structured_event_emitters(");
    const auto commit_event = function_body(
        implementation, "void HotStuffBase::emit_committed_block_event(");
    const auto commit_observed_event = function_body(
        implementation, "void HotStuffBase::emit_commit_observed_event(");
    const auto command_event = function_body(
        implementation,
        "void HotStuffBase::emit_epoch_command_committed_event(");
    const auto post_commit = function_body(
        implementation, "void HotStuffBase::do_post_block_commit(");
    const auto cache_commit = function_body(
        implementation, "void HotStuffBase::cache_adaptive_v2_commit(");
    const auto observe_generation = function_body(
        implementation,
        "bool HotStuffBase::observe_proposal_view_generation(");
    const auto local_proposal = function_body(
        implementation, "void HotStuffBase::do_broadcast_proposal(");
    const auto remote_proposal = function_body(
        implementation, "void HotStuffBase::process_active(");
    const auto do_consensus = function_body(
        implementation, "void HotStuffBase::do_consensus(");
    const auto retire_before_epoch = function_body(
        implementation,
        "void HotStuffBase::retire_deferred_epoch_changes_before_epoch(");
    const auto retire_for_block = function_body(
        implementation,
        "void HotStuffBase::retire_deferred_epoch_changes_for_block(");
    const auto retirement_floor = function_body(
        implementation,
        "void HotStuffBase::advance_committed_retirement_floor(");
    const auto forwarding_abort = function_body(
        implementation, "void HotStuffBase::abort_exact_forwarding(");

    CHECK(contains_all(
        header,
        {"AuditStructuredEventEmitter *audit_event_emitter{nullptr}",
         "AuditStructuredEventEmitter *audit_emitter",
         "maximum_proposal_view_generation_observations",
         "proposal_view_generations",
         "std::optional<std::uint64_t> view_generation"}));
    CHECK(contains_in_order(
        consensus_header,
        {"do_post_block_commit(",
         "const block_t &",
         "std::uint64_t commit_batch_index)"}));
    CHECK(contains_in_order(
        consensus,
        {"std::uint64_t commit_batch_index = 0",
         "commit_queue.rbegin()",
         "do_post_block_commit(blk, commit_batch_index)",
         "++commit_batch_index"}));
    REQUIRE_FALSE(binding.empty());
    CHECK(contains_all(
        binding,
        {"structured_event_emitter = lifecycle_emitter",
         "adaptive_event_emitter = aggregation_emitter",
         "audit_event_emitter = audit_emitter"}));

    REQUIRE_FALSE(commit_event.empty());
    CHECK(contains_all(
        commit_event,
        {"structured_event_emitter == nullptr",
         "committed_key.has_value()",
         "view_generation.has_value()",
         "key.block_hash != blk->get_hash()",
         "blk->get_parent_hashes()",
         "blk->get_cmds().size()",
         "key,",
         "commit_batch_index",
         "CommitStructuredEvent",
         "StructuredEventPayload"}));
    CHECK(commit_event.find("activation.active_effect()") ==
          std::string::npos);
    CHECK(commit_event.find("find_exact_runtime_generation(") ==
          std::string::npos);

    REQUIRE_FALSE(commit_observed_event.empty());
    CHECK(contains_all(
        commit_observed_event,
        {"structured_event_emitter == nullptr",
         "blk == nullptr",
         "blk->get_height()",
         "blk->get_hash()",
         "blk->get_parent_hashes()",
         "blk->get_cmds().size()",
         "commit_batch_index",
         "CommitObservedStructuredEvent",
         "StructuredEventPayload"}));
    CHECK(commit_observed_event.find("ProposalKey") == std::string::npos);
    CHECK(commit_observed_event.find("committed_key") == std::string::npos);
    CHECK(commit_observed_event.find("view_generation") ==
          std::string::npos);

    REQUIRE_FALSE(observe_generation.empty());
    CHECK(contains_all(
        observe_generation,
        {"generation == 0",
         "proposal_view_generations.find(key)",
         "found->second.reset()",
         "maximum_proposal_view_generation_observations",
         "proposal_view_generations.emplace(key, generation)",
         "catch (...)"}));
    REQUIRE_FALSE(local_proposal.empty());
    CHECK(contains_in_order(
        local_proposal,
        {"adaptive_epoch_consensus_message(",
         "if (adaptive_payload.empty())",
         "observe_proposal_view_generation(",
         "prop.key()",
         "*generation"}));
    REQUIRE_FALSE(remote_proposal.empty());
    CHECK(contains_in_order(
        remote_proposal,
        {"proposal.metadata.key()",
         "observe_proposal_view_generation(",
         "proposal.view_generation"}));

    REQUIRE_FALSE(cache_commit.empty());
    CHECK(contains_all(
        cache_commit,
        {"committed_proposal_key(blk, committed_keys)",
         "proposal_view_generation(*committed_key)",
         "PendingAdaptiveV2Commit"}));
    REQUIRE_FALSE(do_consensus.empty());
    CHECK(contains_in_order(
        do_consensus,
        {"record_committed_epoch_change_history(blk)",
         "retire_deferred_epoch_changes_for_block(blk->get_hash())",
         "close_committed_block(blk->get_hash())",
         "cache_adaptive_v2_commit(blk, keys)",
         "forget_proposal_view_generation(key)",
         "forget_proposal_view_generations_for_block(blk->get_hash())"}));
    REQUIRE_FALSE(retire_before_epoch.empty());
    CHECK(retire_before_epoch.find(
              "forget_proposal_view_generation(key)") !=
          std::string::npos);
    REQUIRE_FALSE(retire_for_block.empty());
    CHECK(retire_for_block.find(
              "forget_proposal_view_generation(key)") ==
          std::string::npos);
    REQUIRE_FALSE(retirement_floor.empty());
    CHECK(retirement_floor.find(
              "forget_proposal_view_generations_before_epoch(") !=
          std::string::npos);
    REQUIRE_FALSE(forwarding_abort.empty());
    CHECK(forwarding_abort.find(
              "forget_proposal_view_generation(lease.key())") !=
          std::string::npos);

    REQUIRE_FALSE(command_event.empty());
    CHECK(contains_all(
        command_event,
        {"audit_event_emitter == nullptr",
         "record.command_commit_height",
         "blk->get_height()",
         "epoch_change_payload_digest(command.payload)",
         "record.payload_digest",
         "record.predecessor_epoch_number",
         "record.predecessor_epoch_digest",
         "record.successor_epoch_number",
         "record.successor_epoch_digest",
         "record.activation_delay_blocks",
         "record.activation_height",
         "EpochCommandCommittedStructuredEvent",
         "AuditStructuredEventPayload"}));
    CHECK(command_event.find("activation.active_effect()") ==
          std::string::npos);

    REQUIRE_FALSE(post_commit.empty());
    CHECK(count_occurrences(
              post_commit, "emit_commit_observed_event(") == 1);
    CHECK(count_occurrences(
              post_commit, "emit_committed_block_event(") == 1);
    CHECK(count_occurrences(
              post_commit, "emit_epoch_command_committed_event(") == 1);
    CHECK(contains_in_order(
        post_commit,
        {"EpochProtocolMode::adaptive_v2",
         "if (blk == nullptr)",
         "emit_commit_observed_event(",
         "std::optional<ProposalKey> committed_key",
         "committed_key = pending_adaptive_v2_commit->committed_key",
         "view_generation =",
         "pending_adaptive_v2_commit->view_generation",
         "pending_adaptive_v2_commit.reset()",
         "emit_committed_block_event(",
         "commit_batch_index",
         "record_committed_v2(command, blk->get_height())",
         "ActivationRecordDisposition::recorded",
         "recorded.record.has_value()",
         "emit_epoch_command_committed_event(",
         "pending_committed_epoch_change.reset()",
         "on_v2_post_block_commit("}));
}

TEST_CASE("every adaptive v2 topology publication emits active configuration",
          "[adaptive-v2][structured-event][topology][runtime]")
{
    const auto implementation = source("src/hotstuff.cpp");
    const auto runtime = source("src/epoch_runtime.cpp");
    const auto live_binding = source("src/epoch_live_binding.cpp");
    const auto apply = function_body(
        implementation,
        "void apply(const EpochRuntimeUpdate &update) noexcept");
    const auto successor = function_body(
        live_binding, "HotStuffEpochLiveBinding::finish_commit(");
    const auto rotate = function_body(
        live_binding, "HotStuffEpochLiveBinding::rotate_to_tree(");
    const auto compare_and_rotate = function_body(
        runtime, "AdaptiveV2RotationCoordinator::compare_and_rotate(");
    const auto periodic = function_body(
        implementation,
        "void HotStuffBase::rotate_adaptive_v2_after_commit(");
    const auto timeout = function_body(
        implementation,
        "HotStuffBase::rotate_tree_on_leader_timeout(");

    REQUIRE_FALSE(apply.empty());
    CHECK(count_occurrences(
              apply, "emit_active_configuration_event(") == 1);
    CHECK(contains_in_order(
        apply,
        {"activate_runtime_view(",
         "active_index = armed_index",
         "owner.config.async_blocks",
         "owner.config.fanout",
         "armed_topology = nullptr",
         "EpochProtocolMode::adaptive_v2",
         "owner.emit_active_configuration_event(",
         "update.activation.configuration"}));
    CHECK(count_occurrences(
              implementation, "topology.apply(update)") == 1);

    REQUIRE_FALSE(successor.empty());
    CHECK(contains_in_order(
        successor,
        {"ActivationTransition::activated",
         "live_effects_.apply_update(*result.update)"}));

    REQUIRE_FALSE(compare_and_rotate.empty());
    CHECK(contains_in_order(
        compare_and_rotate,
        {"effects_.rotate_to_tree(*next_tree)",
         "rotation.update.has_value()"}));
    REQUIRE_FALSE(rotate.empty());
    CHECK(contains_in_order(
        rotate,
        {"adapter_.rotate_to_tree(tree_id)",
         "live_effects_.apply_update(*result.update)"}));

    REQUIRE_FALSE(periodic.empty());
    CHECK(periodic.find(
              "adaptive_v2_rotation_coordinator->on_commit(") !=
          std::string::npos);
    REQUIRE_FALSE(timeout.empty());
    CHECK(timeout.find(
              "adaptive_v2_rotation_coordinator->on_timeout(") !=
          std::string::npos);
}

TEST_CASE("adaptive v2 rotates only after exact post-commit cadence",
          "[c08][adaptive-v2][commit-cadence][post-block]")
{
    const auto header = source("include/hotstuff/hotstuff.h");
    const auto runtime_header = source("include/hotstuff/epoch_runtime.h");
    const auto runtime = source("src/epoch_runtime.cpp");
    const auto implementation = source("src/hotstuff.cpp");
    const auto consensus = function_body(
        implementation, "void HotStuffBase::do_consensus(");
    const auto cache = function_body(
        implementation, "void HotStuffBase::cache_adaptive_v2_commit(");
    const auto post_commit = function_body(
        implementation, "void HotStuffBase::do_post_block_commit(");
    const auto finish_commit = function_body(
        implementation,
        "void HotStuffBase::finish_adaptive_epoch_commit(");
    const auto periodic_rotation = function_body(
        implementation,
        "void HotStuffBase::rotate_adaptive_v2_after_commit(");
    const auto timeout_rotation = function_body(
        implementation,
        "HotStuffBase::rotate_tree_on_leader_timeout(");

    CHECK(runtime_header.find("AdaptiveV2RotationCoordinator") !=
          std::string::npos);
    CHECK(runtime_header.find("std::mutex mutex_") != std::string::npos);
    CHECK(header.find("PendingAdaptiveV2Commit") != std::string::npos);

    REQUIRE_FALSE(consensus.empty());
    CHECK(contains_in_order(
        consensus,
        {"proposal_contexts->close_committed_block(",
         "cache_adaptive_v2_commit(blk, keys)"}));
    CHECK(consensus.find("rotate_adaptive_v2_after_commit(") ==
          std::string::npos);

    REQUIRE_FALSE(cache.empty());
    CHECK(contains_in_order(
        cache,
        {"EpochProtocolMode::adaptive_v2",
         "committed_proposal_key(blk, committed_keys)",
         "proposal_view_generation(*committed_key)",
         "pending_adaptive_v2_commit.emplace(",
         "PendingAdaptiveV2Commit"}));
    CHECK(cache.find("observed_committed_proposal_key(") ==
          std::string::npos);
    CHECK(contains_in_order(
        cache,
        {"committed_key",
         "generation",
         "blk->get_hash(), committed_key, generation"}));

    REQUIRE_FALSE(post_commit.empty());
    CHECK(contains_in_order(
        post_commit,
        {"pending_adaptive_v2_commit",
         "block_hash == blk->get_hash()",
         "committed_key =",
         "view_generation =",
         "pending_adaptive_v2_commit.reset()",
         "epoch_live_binding->on_v2_post_block_commit(",
         "finish_adaptive_epoch_commit(blk, activation)",
         "rotate_adaptive_v2_after_commit(committed_key)"}));
    CHECK(post_commit.find(
              "rotate_adaptive_v2_after_commit(view_generation)") ==
          std::string::npos);

    REQUIRE_FALSE(finish_commit.empty());
    CHECK(contains_in_order(
        finish_commit,
        {"ActivationTransition::activated",
         "adaptive_v2_rotation_coordinator->reset_for_activation()"}));

    REQUIRE_FALSE(periodic_rotation.empty());
    CHECK(contains_in_order(
        periodic_rotation,
        {"activation.active_effect()",
         "adaptive_v2_rotation_coordinator->on_commit(",
         "committed_key",
         "active.configuration",
         "active.generation"}));
    CHECK(periodic_rotation.find("view_generation") ==
          std::string::npos);
    CHECK(periodic_rotation.find("nmajority") == std::string::npos);

    REQUIRE_FALSE(timeout_rotation.empty());
    CHECK(accepts_adaptive_v2(timeout_rotation));
    CHECK(contains_in_order(
        timeout_rotation,
        {"EpochProtocolMode::adaptive_v2",
         "adaptive_v2_rotation_coordinator->on_timeout(",
         "expired_view.configuration",
         "expired_view.view_generation"}));
    CHECK(timeout_rotation.find("nmajority") == std::string::npos);

    CHECK(runtime.find("const std::lock_guard<std::mutex> lock(mutex_)") !=
          std::string::npos);
    CHECK(runtime.find("matches_expected_view(") != std::string::npos);
}

TEST_CASE("adaptive v2 pre-vote authorization is pinned once",
          "[c08][epoch-change][pre-vote][configuration]")
{
    const auto implementation = source("src/hotstuff.cpp");
    const auto configure = function_body(
        implementation,
        "HotStuffBase::configure_epoch_change_pre_vote_gate(");

    REQUIRE_FALSE(configure.empty());
    CHECK(contains_in_order(
        configure,
        {"epoch_change_verifier != nullptr",
         "configuration is already pinned",
         "std::make_unique<EpochChangeVerifier>(",
         "epoch_change_verifier = std::move(verifier)"}));
}

TEST_CASE("adaptive v2 startup is pinned and bootstraps a schedule-free epoch",
          "[c08][adaptive-v2][startup][bootstrap][intentional-red]")
{
    const auto header = source("include/hotstuff/hotstuff.h");
    const auto implementation = source("src/hotstuff.cpp");
    const auto bootstrap = function_body(
        implementation, "HotStuffBase::register_initial_epoch(");
    const auto legacy = function_body(
        implementation, "HotStuffBase::register_legacy_epoch(");
    const auto tree_config = function_body(
        implementation, "void HotStuffBase::tree_config(");
    const auto start = function_body(
        implementation, "void HotStuffBase::start(");

    CHECK(header.find("register_initial_epoch") != std::string::npos);
    CHECK_FALSE(bootstrap.empty());
    CHECK(contains_in_order(
        bootstrap,
        {"EpochProtocolMode::adaptive_v2",
         "append_epoch_trees(",
         "adaptive_v2_epoch_zero_input(",
         "exact_epochs->stage("}));

    REQUIRE_FALSE(legacy.empty());
    CHECK(contains_all(
        legacy,
        {"kEpochDefinitionSchemaVersion",
         "static_cast<std::uint64_t>(input.epoch_number) * 1000"}));
    REQUIRE_FALSE(tree_config.empty());
    CHECK(tree_config.find("register_initial_epoch(epochs.back())") !=
          std::string::npos);
    CHECK(tree_config.find("register_legacy_epoch(epochs.back())") ==
          std::string::npos);

    REQUIRE_FALSE(start.empty());
    CHECK(contains_in_order(
        start,
        {"EpochProtocolMode::adaptive_v2",
         "epoch_change_verifier",
         "epoch_change_maximum_block_extra_bytes",
         "epoch_change_maximum_ancestry_blocks",
         "throw HotStuffError(",
         "tree_scheduler("}));
    CHECK(contains_all(
        start,
        {"derive_byzantine_quorum(config.nreplicas)",
         "byzantine->fault_threshold",
         "config.nmajority",
         "byzantine->quorum",
         "EpochProtocolMode::adaptive_v1",
         "initialize_adaptive_epoch_runtime()"}));
    const auto runtime_init = start.find(
        "initialize_adaptive_epoch_runtime()");
    REQUIRE(runtime_init != std::string::npos);
    CHECK(start.rfind("EpochProtocolMode::adaptive_v2", runtime_init) !=
          std::string::npos);
}

TEST_CASE("adaptive v2 uses adaptive consensus handlers without stage or arm",
          "[c08][adaptive-v2][startup][transport][intentional-red]")
{
    const auto header = source("include/hotstuff/hotstuff.h");
    const auto implementation = source("src/hotstuff.cpp");
    const auto wiring = source("src/epoch_live_binding.cpp");
    const auto constructor = function_body(
        implementation, "HotStuffBase::HotStuffBase(");
    const auto control = function_body(
        implementation, "HotStuffBase::install_adaptive_epoch_handlers(");
    const auto consensus = function_body(
        implementation,
        "HotStuffBase::install_adaptive_consensus_handlers(");
    const auto definitions = function_body(
        implementation,
        "HotStuffBase::install_adaptive_v2_definition_handlers(");
    const auto encoder = function_body(
        wiring, "bytearray_t adaptive_epoch_consensus_message(");

    CHECK(header.find("install_adaptive_consensus_handlers") !=
          std::string::npos);
    REQUIRE_FALSE(constructor.empty());
    CHECK(contains_all(
        constructor,
        {"EpochProtocolMode::adaptive_v1",
         "EpochProtocolMode::adaptive_v2",
         "install_adaptive_epoch_handlers()",
         "install_adaptive_consensus_handlers()",
         "install_adaptive_v2_definition_handlers()",
         "install_legacy_consensus_handlers()"}));

    REQUIRE_FALSE(control.empty());
    CHECK(contains_all(
        control,
        {"adaptive_stage_epoch_handler", "adaptive_arm_epoch_handler"}));
    CHECK(control.find("adaptive_propose_handler") == std::string::npos);
    CHECK(control.find("adaptive_vote_handler") == std::string::npos);
    CHECK(control.find("adaptive_relay_handler") == std::string::npos);

    CHECK_FALSE(consensus.empty());
    CHECK(contains_all(
        consensus,
        {"adaptive_propose_handler",
         "adaptive_vote_handler",
         "adaptive_relay_handler"}));
    CHECK(consensus.find("adaptive_stage_epoch_handler") ==
          std::string::npos);
    CHECK(consensus.find("adaptive_arm_epoch_handler") ==
          std::string::npos);
    REQUIRE_FALSE(definitions.empty());
    CHECK(definitions.find("adaptive_arm_epoch_handler") ==
          std::string::npos);

    REQUIRE_FALSE(encoder.empty());
    CHECK(encoder.find("protocol_mode") != std::string::npos);
    CHECK(encoder.find("EpochProtocolMode::adaptive_v1") ==
          std::string::npos);

    for (const auto *signature : {
             "HotStuffBase::find_exact_runtime_tree(",
             "HotStuffBase::find_exact_runtime_generation(",
             "uint32_t HotStuffBase::get_tree_id(",
             "uint32_t HotStuffBase::get_cur_epoch_nr(",
             "void HotStuffBase::do_broadcast_proposal(",
             "void HotStuffBase::do_vote(",
             "bool HotStuffBase::send_exact_relay("})
    {
        const auto body = function_body(implementation, signature);
        CAPTURE(signature);
        REQUIRE_FALSE(body.empty());
        CHECK(accepts_adaptive_v2(body));
    }
}

TEST_CASE("adaptive v2 server CLI exposes every pinned pre-vote input",
          "[c08][adaptive-v2][cli][configuration][intentional-red]")
{
    const auto app = source("examples/hotstuff_app.cpp");
    const auto main = function_body(app, "int main(");
    REQUIRE_FALSE(main.empty());

    CHECK(app.find(
              "epoch protocol mode (legacy_static, adaptive_v1, adaptive_v2)") !=
          std::string::npos);
    CHECK(contains_in_order(
        main,
        {"opt_epoch_protocol_mode->get() == \"adaptive_v1\"",
         "opt_epoch_protocol_mode->get() == \"adaptive_v2\"",
         "EpochProtocolMode::adaptive_v2"}));

    struct RequiredOption
    {
        const char *name;
        const char *missing_sentinel;
    };
    for (const RequiredOption expected : {
             RequiredOption{"epoch-change-issuer-id",
                            "Config::OptValStr::create(\"\")"},
             {"epoch-change-issuer-public-key",
              "Config::OptValStr::create(\"\")"},
             {"epoch-change-minimum-activation-delay",
              "Config::OptValStr::create(\"\")"},
             {"epoch-change-maximum-activation-delay",
              "Config::OptValStr::create(\"\")"},
             {"epoch-change-maximum-block-extra-bytes",
              "Config::OptValStr::create(\"\")"},
             {"epoch-change-maximum-ancestry-blocks",
              "Config::OptValStr::create(\"\")"}})
    {
        CAPTURE(expected.name);
        const auto binding = option_binding(app, expected.name);
        REQUIRE(binding.has_value());
        const auto declaration = app.find("auto " + *binding);
        REQUIRE(declaration != std::string::npos);
        const auto terminator = app.find(';', declaration);
        REQUIRE(terminator != std::string::npos);
        CHECK(app.substr(declaration, terminator - declaration)
                  .find(expected.missing_sentinel) != std::string::npos);
    }
}

TEST_CASE("adaptive v2 server CLI rejects missing and invalid pre-vote inputs",
          "[c08][adaptive-v2][cli][fail-closed][intentional-red]")
{
    const auto app = source("examples/hotstuff_app.cpp");
    const auto numeric_parser = function_body(
        app, "Value parse_adaptive_v2_unsigned(");
    const auto key_parser = function_body(
        app, "parse_adaptive_v2_issuer_public_key(");
    const auto config_parser = function_body(
        app, "parse_adaptive_v2_pre_vote_config(");

    REQUIRE_FALSE(numeric_parser.empty());
    CHECK(contains_all(
        numeric_parser,
        {"raw_value.empty()",
         "std::from_chars(begin, end, value, 10)",
         "parsed.ec != std::errc{}",
         "parsed.ptr != end",
         "must_be_positive && value == 0",
         "throw HotStuffError("}));
    REQUIRE_FALSE(key_parser.empty());
    CHECK(contains_all(
        key_parser,
        {"issuer_public_key_hex.empty()",
         "issuer_public_key_hex.size() != 66",
         "std::isxdigit(character)",
         "hotstuff::PubKeySecp256k1(",
         "hotstuff::from_hex(issuer_public_key_hex)",
         "catch (const std::exception &)",
         "throw HotStuffError("}));
    REQUIRE_FALSE(config_parser.empty());
    CHECK(contains_all(
        config_parser,
        {"protocol_mode != \"adaptive_v2\"",
         "parse_adaptive_v2_unsigned<hotstuff::EpochChangeIssuerId>(",
         "parse_adaptive_v2_unsigned<std::uint64_t>(",
         "parse_adaptive_v2_unsigned<std::size_t>(",
         "maximum_delay < minimum_delay",
         "hotstuff::EpochChangeIssuer{",
         "hotstuff::EpochChangeDelayBounds{"}));
}

TEST_CASE("accepted adaptive v2 CLI pins the configured verifier before start",
          "[c08][adaptive-v2][cli][wiring][intentional-red]")
{
    const auto app = source("examples/hotstuff_app.cpp");
    const auto main = function_body(app, "int main(");
    const auto configure = main.find(
        "papp->configure_epoch_change_pre_vote_gate(");
    const auto start = main.find("papp->start(reps)");
    REQUIRE(configure != std::string::npos);
    REQUIRE(start != std::string::npos);
    CHECK(configure < start);
    CHECK(count_occurrences(
              main, "papp->configure_epoch_change_pre_vote_gate(") == 1);

    const auto guard = main.rfind(
        "if (epoch_protocol_mode == EpochProtocolMode::adaptive_v2)",
        configure);
    REQUIRE(guard != std::string::npos);
    CHECK(main.find('}', guard) > configure);

    const auto call_end = main.find(';', configure);
    REQUIRE(call_end != std::string::npos);
    const auto call = main.substr(configure, call_end - configure);
    CHECK(contains_all(
        call,
        {"pre_vote_config.issuer",
         "pre_vote_config.delay_bounds",
         "pre_vote_config.maximum_block_extra_bytes",
         "pre_vote_config.maximum_ancestry_blocks"}));

    const auto parser_call = main.find(
        "parse_adaptive_v2_pre_vote_config(");
    REQUIRE(parser_call != std::string::npos);
    const auto parser_call_end = main.find(';', parser_call);
    REQUIRE(parser_call_end != std::string::npos);
    const auto parsed_options = main.substr(
        parser_call, parser_call_end - parser_call);
    for (const auto *option : {
             "epoch-change-issuer-id",
             "epoch-change-issuer-public-key",
             "epoch-change-minimum-activation-delay",
             "epoch-change-maximum-activation-delay",
             "epoch-change-maximum-block-extra-bytes",
             "epoch-change-maximum-ancestry-blocks"})
    {
        const auto binding = option_binding(app, option);
        CAPTURE(option);
        REQUIRE(binding.has_value());
        CHECK(parsed_options.find(*binding + "->get()") !=
              std::string::npos);
    }
    CHECK(parser_call < main.find("replica idx out of range"));
}

TEST_CASE("adaptive v2 executable validates exact pre-vote configuration",
          "[c08][adaptive-v2][cli][subprocess][intentional-red]")
{
    const auto accepted_arguments = valid_adaptive_v2_arguments();
    const auto accepted = run_hotstuff_app(accepted_arguments.values);
    CHECK(accepted.status != 0);
    CHECK(accepted.output.find("replica idx out of range") !=
          std::string::npos);
    CHECK(accepted.output.find("adaptive-v2 epoch-change") ==
          std::string::npos);

    for (const auto *option : {
             "--epoch-change-issuer-id",
             "--epoch-change-issuer-public-key",
             "--epoch-change-minimum-activation-delay",
             "--epoch-change-maximum-activation-delay",
             "--epoch-change-maximum-block-extra-bytes",
             "--epoch-change-maximum-ancestry-blocks"})
    {
        auto arguments = valid_adaptive_v2_arguments();
        remove_option(arguments.values, option);
        const auto missing = run_hotstuff_app(arguments.values);
        CAPTURE(option);
        CAPTURE(missing.output);
        CHECK(missing.status != 0);
        CHECK(missing.output.find("replica idx out of range") ==
              std::string::npos);
        CHECK(missing.output.find("adaptive-v2") != std::string::npos);
    }
}

TEST_CASE("adaptive v2 numeric options reject noncanonical and overflowing input",
          "[c08][adaptive-v2][cli][subprocess][fail-closed][intentional-red]")
{
    struct InvalidValue
    {
        const char *option;
        const char *value;
    };
    for (const InvalidValue invalid : {
             InvalidValue{"--epoch-change-issuer-id", "-1"},
             {"--epoch-change-issuer-id", "+1"},
             {"--epoch-change-minimum-activation-delay", " 2"},
             {"--epoch-change-maximum-activation-delay", "20 "},
             {"--epoch-change-maximum-block-extra-bytes", "2junk"},
             {"--epoch-change-maximum-ancestry-blocks", ""},
             {"--epoch-change-issuer-id",
              "9999999999999999999999999999999999999999"},
             {"--epoch-change-maximum-activation-delay",
              "9999999999999999999999999999999999999999"},
             {"--epoch-change-maximum-block-extra-bytes",
              "9999999999999999999999999999999999999999"}})
    {
        auto arguments = valid_adaptive_v2_arguments();
        set_option_value(
            arguments.values, invalid.option, invalid.value);
        const auto rejected = run_hotstuff_app(arguments.values);
        CAPTURE(invalid.option);
        CAPTURE(invalid.value);
        CAPTURE(rejected.output);
        CHECK(rejected.status != 0);
        CHECK(rejected.output.find("replica idx out of range") ==
              std::string::npos);
        CHECK(rejected.output.find("adaptive-v2") != std::string::npos);
    }
}

TEST_CASE("adaptive v2 executable rejects semantic bounds and malformed keys",
          "[c08][adaptive-v2][cli][subprocess][fail-closed][intentional-red]")
{
    struct InvalidValue
    {
        const char *option;
        const char *value;
    };
    for (const InvalidValue invalid : {
             InvalidValue{"--epoch-change-minimum-activation-delay", "0"},
             {"--epoch-change-maximum-activation-delay", "1"},
             {"--epoch-change-maximum-block-extra-bytes", "0"},
             {"--epoch-change-maximum-ancestry-blocks", "0"},
             {"--epoch-change-issuer-public-key", "02not-hex"},
             {"--epoch-change-issuer-public-key",
              "000000000000000000000000000000000000000000000000000000000000000000"}})
    {
        auto arguments = valid_adaptive_v2_arguments();
        set_option_value(
            arguments.values, invalid.option, invalid.value);
        const auto rejected = run_hotstuff_app(arguments.values);
        CAPTURE(invalid.option);
        CAPTURE(invalid.value);
        CAPTURE(rejected.output);
        CHECK(rejected.status != 0);
        CHECK(rejected.output.find("replica idx out of range") ==
              std::string::npos);
        CHECK(rejected.output.find("adaptive-v2") != std::string::npos);
    }
}

TEST_CASE("adaptive v2 executable rejects a zero commit rotation period",
          "[c08][adaptive-v2][commit-cadence][cli][subprocess]")
{
    auto adaptive_v2 = valid_adaptive_v2_arguments();
    adaptive_v2.values.insert(
        adaptive_v2.values.end(),
        {"--tree-switch-period", "0"});
    const auto rejected = run_hotstuff_app(adaptive_v2.values);
    CHECK(rejected.status != 0);
    CHECK(rejected.output.find("adaptive-v2 tree switch period") !=
          std::string::npos);
    CHECK(rejected.output.find("replica idx out of range") ==
          std::string::npos);

    const auto adaptive_v1 = run_hotstuff_app(
        {"--epoch-protocol-mode", "adaptive_v1",
         "--tree-switch-period", "0"});
    CHECK(adaptive_v1.status != 0);
    CHECK(adaptive_v1.output.find("replica idx out of range") !=
          std::string::npos);
    CHECK(adaptive_v1.output.find("adaptive-v2 tree switch period") ==
          std::string::npos);
}

TEST_CASE("adaptive v2 executable accepts only finite integral rotation periods",
          "[c08][adaptive-v2][commit-cadence][cli][subprocess][bounds]")
{
    for (const auto *invalid : {
             "-1", "0.5", "1.5", "nan", "inf", "1e300",
             "18446744073709551616"})
    {
        auto arguments = valid_adaptive_v2_arguments();
        arguments.values.insert(
            arguments.values.end(),
            {"--tree-switch-period", invalid});
        const auto rejected = run_hotstuff_app(arguments.values);
        CAPTURE(invalid);
        CAPTURE(rejected.output);
        CHECK(rejected.status != 0);
        CHECK(rejected.output.find("adaptive-v2 tree switch period") !=
              std::string::npos);
        CHECK(rejected.output.find("replica idx out of range") ==
              std::string::npos);
    }

    for (const auto *mode : {"legacy_static", "adaptive_v1"})
    {
        const auto compatible = run_hotstuff_app(
            {"--epoch-protocol-mode", mode,
             "--tree-switch-period", "1.5"});
        CAPTURE(mode);
        CHECK(compatible.status != 0);
        CHECK(compatible.output.find("replica idx out of range") !=
              std::string::npos);
        CHECK(compatible.output.find("adaptive-v2 tree switch period") ==
              std::string::npos);
    }
}

TEST_CASE("adaptive v1 executable does not require adaptive v2 inputs",
          "[c08][adaptive-v1][cli][subprocess][compatibility]")
{
    const auto result = run_hotstuff_app(
        {"--epoch-protocol-mode", "adaptive_v1"});
    CHECK(result.status != 0);
    CHECK(result.output.find("replica idx out of range") !=
          std::string::npos);
    CHECK(result.output.find("adaptive-v2") == std::string::npos);
}

TEST_CASE("local executables ignore SIGPIPE before opening network sockets",
          "[rem-d11][local-demo][sigpipe][intentional-red]")
{
    for (const auto *path : {
             "examples/hotstuff_app.cpp",
             "examples/hotstuff_client.cpp"})
    {
        const auto contents = source(path);
        const auto main = function_body(contents, "int main(");
        CAPTURE(path);
        REQUIRE_FALSE(main.empty());
        CHECK(contains_in_order(
            main,
            {"std::signal(SIGPIPE, SIG_IGN)", "Config config("}));
        CHECK(count_occurrences(
                  main, "std::signal(SIGPIPE, SIG_IGN)") == 1);
    }
}

TEST_CASE("leader timeout rotates adaptively without falling into legacy mutation",
          "[rem-d11][epoch-live-binding][leader-timeout][intentional-red]")
{
    const auto implementation = source("src/hotstuff.cpp");
    const auto liveness = source("include/hotstuff/liveness.h");
    const auto adaptive = function_body(
        implementation,
        "HotStuffBase::rotate_tree_on_leader_timeout(");
    const auto timeout = function_body(
        liveness, "void rotate_active_tree_on_timeout(");

    REQUIRE_FALSE(adaptive.empty());
    CHECK(contains_all(
        adaptive,
        {"EpochProtocolMode::adaptive_v1",
         "epoch_live_binding",
         "rotate_to_tree(",
         "legacy_fallback",
         "rejected",
         "rotated"}));

    REQUIRE_FALSE(timeout.empty());
    CHECK(contains_in_order(
        timeout,
        {"hsc->rotate_tree_on_leader_timeout(",
         "legacy_fallback",
         "return",
         "activate_leader_view("}));
}
