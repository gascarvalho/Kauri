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
#include "hotstuff/hotstuff.h"
#include "hotstuff/liveness.h"

#ifndef KAURI_PROJECT_SOURCE_DIR
#error "KAURI_PROJECT_SOURCE_DIR must name the repository root"
#endif

#ifndef KAURI_HOTSTUFF_APP_PATH
#error "KAURI_HOTSTUFF_APP_PATH must name the hotstuff-app executable"
#endif

namespace
{

class ConstructorGuardHotStuff final : public hotstuff::HotStuffNoSig
{
public:
    using hotstuff::HotStuffNoSig::HotStuffNoSig;

protected:
    void state_machine_execute(const hotstuff::Finality &) override {}
};

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

std::string hotstuff_consensus_body(const std::string &contents)
{
    return function_body(
        contents,
        "void HotStuffBase::do_consensus_with_identity_provenance(");
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

TEST_CASE("adaptive v3 construction requires complete configuration before side effects",
          "[cert13][p3][hotstuff][archive-isolation]")
{
    using namespace hotstuff;

    EventContext event_context;
    CHECK_THROWS_WITH(
        ConstructorGuardHotStuff(
            1,
            0,
            bytearray_t{},
            NetAddr("127.0.0.1:0"),
            new PaceMakerDummy(1),
            event_context,
            0,
            HotStuffBase::Net::Config(),
            NetAddr(),
            EpochProtocolMode::adaptive_v3),
        "adaptive-v3 runtime requires one complete isolated configuration");

    const auto implementation = source("src/hotstuff.cpp");
    const auto constructor = function_body(
        implementation, "HotStuffBase::HotStuffBase(");

    REQUIRE_FALSE(constructor.empty());
    CHECK(contains_in_order(
        constructor,
        {"if ((epoch_protocol_mode == EpochProtocolMode::adaptive_v3) !=",
         "throw HotStuffError(",
         "adaptive-v3 runtime requires one complete isolated configuration",
         "initialize_committed_epoch_change_history()",
         "install_adaptive_v3_handlers()",
         "pn.start()",
         "pn.listen(listen_addr)",
         "pn.conn_peer(adaptive_v3_config->manager_peer)"}));
    CHECK(count_occurrences(
              constructor,
              "adaptive-v3 runtime requires one complete isolated configuration") ==
          1);
}

TEST_CASE("adaptive v3 live path fences before observation and activates only from certificate",
          "[cert13][p3][hotstuff][activation-readiness]")
{
    const auto implementation = source("src/hotstuff.cpp");
    const auto post_commit = function_body(
        implementation,
        "void HotStuffBase::process_adaptive_v3_post_block_commit(");
    const auto certificate = function_body(
        implementation,
        "void HotStuffBase::adaptive_v3_readiness_certificate_handler(");
    const auto retry = function_body(
        implementation,
        "void HotStuffBase::transmit_adaptive_v3_observation(");

    REQUIRE_FALSE(post_commit.empty());
    CHECK(contains_in_order(
        post_commit,
        {"prepare_committed_v3(",
         "on_v3_post_block_commit(",
         "result.boundary.observation",
         "enqueue_adaptive_v3_observation(",
         "if (result.update)",
         "publish_adaptive_v3_activation("}));
    CHECK(contains_all(
        post_commit,
        {"adaptive_v3_deferred_observation",
         "adaptive_v3_runtime_prepared",
         "scheduled_readiness_height"}));

    REQUIRE_FALSE(certificate.empty());
    CHECK(contains_in_order(
        certificate,
        {"pn.get_peer_conn(*epoch_manager_peer)",
         "PeerId(*certificate) != *epoch_manager_peer",
         "decode_activation_readiness_certificate(",
         "AdaptiveV3ReadinessTransition::wire_rejected",
         "emit_adaptive_v3_readiness_event(",
         "std::move(event), true",
         "if (!adaptive_v3_runtime_prepared)",
         "adaptive_v3_deferred_certificate",
         "ingest_adaptive_v3_readiness_certificate("}));
    CHECK(contains_all(
        certificate,
        {"wire_opcode", "wire_payload_size",
         "canonical_wire_payload", "certificate_decode"}));

    REQUIRE_FALSE(retry.empty());
    CHECK(contains_in_order(
        retry,
        {"adaptive_v3_observation_attempts >=",
         "maximum_observation_attempts",
         "adaptive_v3_observation_retry_exhausted = true",
         "AdaptiveV3ReadinessTransition::",
         "observation_retry_exhausted",
         "retry_exhausted",
         "pn.get_peer_conn(*epoch_manager_peer)",
         "schedule_adaptive_v3_observation_retry()"}));
    CHECK(retry.find("emit_adaptive_v3_observation_terminal(") ==
          std::string::npos);
    CHECK(certificate.find(
              "adaptive_v3_observation_retry_exhausted") ==
          std::string::npos);
}

TEST_CASE("adaptive v3 app and client expose isolated canonical flags",
          "[cert13][p3][cli][archive-isolation]")
{
    const auto app = source("examples/hotstuff_app.cpp");
    const auto client = source("examples/hotstuff_client.cpp");
    CHECK(contains_all(
        app,
        {"\"adaptive_v3\"",
         "\"activation-readiness-member\"",
         "\"activation-readiness-maximum-observation-attempts\"",
         "\"activation-readiness-observation-retry-interval-ms\"",
         "adaptive-v3 forbids trusted-local epoch bootstrap",
         "AdaptiveV3RuntimeConfig"}));
    CHECK(contains_all(
        client,
        {"\"adaptive_v3\"",
         "EpochProtocolMode::adaptive_v3",
         "derive_byzantine_quorum"}));
}

TEST_CASE(
    "adaptive v3 definition recovery remains v3 typed and resumes preparation",
    "[cert13][p3][hotstuff][definition-recovery][intentional-red]")
{
    const auto implementation = source("src/hotstuff.cpp");
    const auto request = function_body(
        implementation,
        "HotStuffBase::adaptive_definition_request_handler(");
    const auto reply = function_body(
        implementation,
        "HotStuffBase::adaptive_definition_reply_handler(");
    const auto bundle = function_body(
        implementation,
        "void HotStuffBase::adaptive_v3_epoch_change_bundle_handler(");

    REQUIRE_FALSE(request.empty());
    REQUIRE_FALSE(reply.empty());
    REQUIRE_FALSE(bundle.empty());
    CHECK(request.find(
              "epoch_protocol_mode != EpochProtocolMode::adaptive_v2") ==
          std::string::npos);
    CHECK(reply.find(
              "epoch_protocol_mode != EpochProtocolMode::adaptive_v2") ==
          std::string::npos);
    CHECK(contains_all(
        request,
        {"EpochProtocolMode::adaptive_v3",
         "epoch_wire_schema_for_mode(epoch_protocol_mode)",
         "decode_epoch_definition_request("}));
    CHECK(contains_all(
        reply,
        {"EpochProtocolMode::adaptive_v3",
         "epoch_wire_schema_for_mode(epoch_protocol_mode)",
         "decode_epoch_definition_reply("}));
    CHECK(contains_all(
        bundle,
        {"successor == nullptr", "send_epoch_definition_request("}));
    CHECK(contains_in_order(
        reply,
        {"stage_available_v2(",
         "prepare_committed_v3(",
         "adaptive_v3_runtime_prepared = true"}));
}

TEST_CASE(
    "adaptive v3 replica lifecycle retires E1 and admits one exact E2 cycle",
    "[cert13][p3][hotstuff][two-cycle][retirement][intentional-red]")
{
    const auto header = source("include/hotstuff/hotstuff.h");
    const auto implementation = source("src/hotstuff.cpp");
    const auto post_commit = function_body(
        implementation,
        "void HotStuffBase::process_adaptive_v3_post_block_commit(");
    const auto publish = function_body(
        implementation,
        "void HotStuffBase::publish_adaptive_v3_activation(");
    const auto certificate = function_body(
        implementation,
        "void HotStuffBase::adaptive_v3_readiness_certificate_handler(");

    REQUIRE_FALSE(post_commit.empty());
    REQUIRE_FALSE(publish.empty());
    REQUIRE_FALSE(certificate.empty());
    CHECK(header.find("struct AdaptiveV3RetiredActivationReceipt") !=
          std::string::npos);
    CHECK(header.find(
              "std::unique_ptr<AdaptiveV3RetiredActivationReceipt>") !=
          std::string::npos);
    CHECK(contains_all(
        publish,
        {"adaptive_v3_retired_activation_receipt",
         "adaptive_v3_retired_activation_receipt.swap(prepared_receipt)",
         "adaptive_v3_activation_gate.reset()",
         "adaptive_v3_committed_command.reset()",
         "adaptive_v3_signed_observation.reset()",
         "adaptive_v3_accepted_certificate.reset()",
         "adaptive_v3_boundary_block = nullptr",
         "adaptive_v3_latest_committed_block = nullptr",
         "adaptive_v3_runtime_prepared = false",
         "adaptive_v3_observation_attempts = 0",
         "adaptive_v3_observation_retry_exhausted = false",
         "adaptive_v3_observation_terminal = false"}));
    CHECK(post_commit.find(
              "adaptive_v3_retired_activation_receipt") !=
          std::string::npos);
    CHECK(certificate.find(
              "adaptive_v3_retired_activation_receipt") !=
          std::string::npos);
}

TEST_CASE(
    "adaptive v3 retirement prebuilds all fallible bytes before live authority",
    "[cert13][p3][hotstuff][retirement][atomic][failure-injection]")
{
    const auto implementation = source("src/hotstuff.cpp");
    const auto ingest = function_body(
        implementation,
        "void HotStuffBase::ingest_adaptive_v3_readiness_certificate(");
    const auto publish = function_body(
        implementation,
        "void HotStuffBase::publish_adaptive_v3_activation(");
    REQUIRE_FALSE(ingest.empty());
    REQUIRE_FALSE(publish.empty());
    CHECK(contains_in_order(
        ingest,
        {"prepare_adaptive_v3_retirement(",
         "std::make_unique<ActivationReadinessCertificateV1>",
         "std::make_unique<AdaptiveV3ReadinessStructuredEvent>",
         "apply_v3_readiness_certificate(",
         "publish_adaptive_v3_activation("}));
    CHECK(contains_in_order(
        publish,
        {"adaptive_v3_retired_activation_receipt.swap(prepared_receipt)",
         "adaptive_v3_activation_gate.reset()",
         "adaptive_v3_committed_command.reset()",
         "adaptive_v3_signed_observation.reset()",
         "adaptive_v3_prepared_activation_receipt.reset()",
         "cancel_adaptive_v3_observation_retry()",
         "emit_adaptive_v3_readiness_event(",
         "drain_activated_futures()",
         "emit_epoch_lifecycle_event(",
         "transmit_adaptive_v3_acknowledgement("}));
}

TEST_CASE(
    "adaptive v3 terminal evidence separates readiness and command identity",
    "[cert13][p3][hotstuff][terminal][fail-closed]")
{
    const auto implementation = source("src/hotstuff.cpp");
    const auto process = function_body(
        implementation,
        "void HotStuffBase::process_adaptive_v3_post_block_commit(");
    const auto certificate = function_body(
        implementation,
        "void HotStuffBase::adaptive_v3_readiness_certificate_handler(");
    const auto recovery = function_body(
        implementation,
        "void HotStuffBase::dispatch_committed_epoch_definition_retry(");
    const auto command_terminal = function_body(
        implementation,
        "void HotStuffBase::emit_adaptive_v3_command_terminal(");
    REQUIRE_FALSE(process.empty());
    REQUIRE_FALSE(certificate.empty());
    REQUIRE_FALSE(recovery.empty());
    REQUIRE_FALSE(command_terminal.empty());
    CHECK(process.find("adaptive_v3_observation_terminal = true") ==
          std::string::npos);
    CHECK(certificate.find("adaptive_v3_observation_terminal = true") ==
          std::string::npos);
    CHECK(contains_all(
        process,
        {"invalid_committed_command",
         "wrong_active_predecessor",
         "invalid_successor_generation",
         "successor_runtime_preparation_failed",
         "committed_definition_recovery_failed",
         "readiness_source_sequence_exhausted",
         "readiness_boundary_rejected",
         "emit_adaptive_v3_observation_terminal("}));
    CHECK(recovery.find(
              "committed_definition_retry_schedule_failed") !=
          std::string::npos);
    CHECK(contains_all(
        command_terminal,
        {"adaptive_v3_command_terminal_emitted",
         "adaptive_v3_command_evidence",
         "AdaptiveV3CommandTerminalStructuredEvent",
         "emit_audit("}));
}

TEST_CASE(
    "adaptive v3 lifecycle evidence and duplicate ACK replay are complete",
    "[cert13][p3][hotstuff][events][ack-replay][intentional-red]")
{
    const auto implementation = source("src/hotstuff.cpp");
    const auto manager = source("examples/adaptation_manager.cpp");
    const auto post_commit = function_body(
        implementation,
        "void HotStuffBase::process_adaptive_v3_post_block_commit(");
    const auto ingest = function_body(
        implementation,
        "void HotStuffBase::ingest_adaptive_v3_readiness_certificate(");
    const auto acknowledge = function_body(
        implementation,
        "void HotStuffBase::acknowledge_adaptive_v3_certificate(");

    REQUIRE_FALSE(post_commit.empty());
    REQUIRE_FALSE(ingest.empty());
    REQUIRE_FALSE(acknowledge.empty());
    CHECK(post_commit.find(
              "AdaptiveV3ReadinessTransition::activation_prepared") !=
          std::string::npos);
    CHECK(manager.find("certificate_acknowledged") !=
          std::string::npos);
    CHECK(manager.find("AdaptiveV3ReadinessTransition::terminal") !=
          std::string::npos);
    CHECK(ingest.find("AdaptiveV3CertificateDisposition::duplicate") !=
          std::string::npos);
    CHECK(ingest.find("acknowledge_adaptive_v3_certificate()") !=
          std::string::npos);
    CHECK(acknowledge.find("adaptive_v3_certificate_ack_sent ||") ==
          std::string::npos);
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
    const auto consensus = hotstuff_consensus_body(implementation);
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
    const auto enqueue_initialized = function_body(
        implementation,
        "bool HotStuffBase::\n"
        "    try_enqueue_adaptive_v2_runtime_initialized_report(");
    const auto committed = function_body(
        implementation,
        "void HotStuffBase::report_adaptive_v2_committed(");
    const auto enqueue_committed = function_body(
        implementation,
        "bool HotStuffBase::try_enqueue_adaptive_v2_commit_report(");
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
         "adaptive_v2_durable_initialization_reports",
         "adaptive_v2_durable_commit_reports",
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
        {"if (epoch_protocol_mode != EpochProtocolMode::adaptive_v2 &&",
         "epoch_protocol_mode != EpochProtocolMode::adaptive_v3",
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
        {"is_adaptive_epoch_mode(epoch_protocol_mode)",
         "adaptive_v2_readiness_enqueued",
         "configuration.epoch_number != 0",
         "configuration.tree_id != 0",
         "generation.has_value()",
         "*generation != 1",
         "enqueue_readiness(",
         "configuration, *generation, 0",
         "schedule_adaptive_v2_reporting_flush"}));
    CHECK(contains_in_order(
        transmit,
        {"EpochProtocolMode::adaptive_v3",
         "AdaptiveV2ReportingStream::readiness",
         "AdaptiveV2ReportingStream::lifecycle",
         "AdaptiveV2ReportingStream::evidence"}));

    REQUIRE_FALSE(admit.empty());
    CHECK(contains_in_order(
        admit,
        {"initialize_accumulator(",
         "report_adaptive_v2_runtime_initialized(metadata.key)"}));
    REQUIRE_FALSE(initialized.empty());
    CHECK(contains_in_order(
        initialized,
        {"epoch_protocol_mode != EpochProtocolMode::adaptive_v2 &&",
         "epoch_protocol_mode != EpochProtocolMode::adaptive_v3",
         "adaptive_v2_durable_initialization_reports.size()",
         "maximum_proposal_view_generation_observations",
         "adaptive_v2_durable_initialization_reports.emplace",
         "AdaptiveV2DurableInitializationPhase::",
         "ready_to_enqueue",
         "try_enqueue_adaptive_v2_runtime_initialized_report"}));
    REQUIRE_FALSE(enqueue_initialized.empty());
    CHECK(contains_in_order(
        enqueue_initialized,
        {"NormalProposalRuntimeInitialized",
         "enqueue_lifecycle",
         "AdaptiveV2ReportingEnqueueStatus::queued",
         "AdaptiveV2DurableInitializationPhase::queued",
         "schedule_adaptive_v2_reporting_flush"}));
    CHECK(enqueue_initialized.find(
              "AdaptiveV2ReportingEnqueueStatus::capacity_exceeded") !=
          std::string::npos);
    REQUIRE_FALSE(consensus.empty());
    CHECK(contains_in_order(
        consensus,
        {"resolve_committed_proposal_identity(",
         "blk, keys, verified_direct_certifier",
         "cache_adaptive_v2_commit(",
         "identity",
         "verified_direct_certifier != nullptr",
         "report_adaptive_v2_committed("}));
    REQUIRE_FALSE(committed.empty());
    CHECK(contains_in_order(
        committed,
        {"should_defer_commit_report",
         "persist_adaptive_v2_commit_report",
         "try_enqueue_adaptive_v2_commit_report"}));
    REQUIRE_FALSE(enqueue_committed.empty());
    CHECK(contains_in_order(
        enqueue_committed,
        {"try_enqueue_adaptive_v2_runtime_initialized_report",
         "ProposalCommitted",
         "enqueue_lifecycle",
         "AdaptiveV2ReportingEnqueueStatus::queued",
         "adaptive_v2_durable_commit_reports.erase",
         "adaptive_v2_durable_initialization_reports.erase",
         "schedule_adaptive_v2_reporting_flush"}));
    CHECK(enqueue_committed.find(
              "AdaptiveV2ReportingEnqueueStatus::capacity_exceeded") !=
          std::string::npos);

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
        {"if (epoch_protocol_mode != EpochProtocolMode::adaptive_v2 &&",
         "epoch_protocol_mode != EpochProtocolMode::adaptive_v3",
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
    const auto consensus = hotstuff_consensus_body(implementation);

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
    const auto consensus = hotstuff_consensus_body(implementation);

    CHECK(header.find(
              "const std::optional<ProposalKey> &committed_key") !=
          std::string::npos);
    REQUIRE_FALSE(marker.empty());
    CHECK(contains_all(
        marker,
        {"committed_key.has_value()",
         "const auto &key = *committed_key",
         "key.configuration",
         "find_exact_runtime_tree(key.configuration)"}));
    CHECK(marker.find("activation.active_effect()") == std::string::npos);

    REQUIRE_FALSE(consensus.empty());
    CHECK(contains_in_order(
        consensus,
        {"close_committed_block(blk->get_hash())",
         "resolve_committed_proposal_identity(",
         "record_adaptive_commit_marker(blk, authoritative_key)",
         "proposal_admission->retire_proposal(key)"}));
}

TEST_CASE(
    "verified core certifiers reach exact commit identity and generation recovery",
    "[adaptive-v2][commit][certifier][identity][generation][wiring]")
{
    const auto consensus_header = source("include/hotstuff/consensus.h");
    const auto hotstuff_header = source("include/hotstuff/hotstuff.h");
    const auto core = source("src/consensus.cpp");
    const auto implementation = source("src/hotstuff.cpp");
    const auto update = function_body(core, "void HotStuffCore::update(");
    const auto legal_skip = function_body(
        core, "bool HotStuffCore::has_verified_legal_qc_skip(");
    const auto commit = hotstuff_consensus_body(implementation);
    const auto compatibility_commit = function_body(
        implementation, "void HotStuffBase::do_consensus(const block_t &blk)");
    const auto core_commit = function_body(
        implementation,
        "void HotStuffBase::do_consensus(\n"
        "        const block_t &blk,\n"
        "        const quorum_cert_bt &verified_direct_certifier)");
    const auto classified_core_commit = function_body(
        implementation,
        "void HotStuffBase::do_consensus(\n"
        "        const block_t &blk,\n"
        "        const quorum_cert_bt &verified_direct_certifier,\n"
        "        CommitCertifierDisposition certifier_disposition)");
    const auto resolve = function_body(
        implementation,
        "HotStuffBase::resolve_committed_proposal_identity(");
    const auto cache = function_body(
        implementation, "void HotStuffBase::cache_adaptive_v2_commit(");

    CHECK(contains_all(
        consensus_header,
        {"enum class CommitCertifierDisposition",
         "virtual void do_consensus(\n"
         "            const block_t &blk,",
         "const quorum_cert_bt &verified_direct_certifier,\n"
         "            CommitCertifierDisposition certifier_disposition)"}));
    CHECK(contains_all(
        hotstuff_header,
        {"void do_consensus(const block_t &blk) override;",
         "const quorum_cert_bt &verified_direct_certifier,\n"
         "            CommitCertifierDisposition certifier_disposition) override;"}));

    REQUIRE_FALSE(update.empty());
    CHECK(contains_in_order(
        update,
        {"const block_t &direct_certifier = queue_index == 0",
         "has_valid_qc_ancestry(direct_certifier, blk)",
         "verified_direct_certifier",
         "has_verified_legal_qc_skip(",
         "legal_qc_skipped_ancestor",
         "CommitCertifierDisposition::unproven"}));
    REQUIRE_FALSE(legal_skip.empty());
    CHECK(contains_in_order(
        legal_skip,
        {"!certifier->qc_ref->delivered",
         "certifier->qc_ref == committed",
         "certificate_key.block_hash == alternate->hash",
         "alternate->height < committed->height",
         "is_ancestor(alternate, certifier)",
         "is_ancestor(alternate, committed)",
         "is_ancestor(committed, certifier)",
         "certifier->qc->has_n(config.nmajority)",
         "certifier->qc->verify(config)"}));
    CHECK(legal_skip.find("catch (...)") != std::string::npos);

    REQUIRE_FALSE(commit.empty());
    REQUIRE_FALSE(compatibility_commit.empty());
    CHECK(contains_in_order(
        compatibility_commit,
        {"do_consensus_with_identity_provenance(",
         "nullptr",
         "CommittedProposalIdentityProvenance::compatibility_unknown"}));
    REQUIRE_FALSE(core_commit.empty());
    CHECK(contains_in_order(
        core_commit,
        {"do_consensus_with_identity_provenance(",
         "verified_direct_certifier == nullptr",
         "compatibility_unknown",
         "verified_direct_certifier"}));
    REQUIRE_FALSE(classified_core_commit.empty());
    CHECK(contains_in_order(
        classified_core_commit,
        {"CommittedProposalIdentityProvenance::core_unproven",
         "CommitCertifierDisposition::verified_direct_certifier",
         "verified_direct_certifier != nullptr",
         "CommitCertifierDisposition::",
         "legal_qc_skipped_ancestor",
         "verified_direct_certifier == nullptr",
         "CommittedProposalIdentityProvenance::",
         "legal_qc_skipped_ancestor",
         "do_consensus_with_identity_provenance("}));
    CHECK(contains_in_order(
        commit,
        {"proposal_contexts->close_committed_block(blk->get_hash())",
         "resolve_committed_proposal_identity(",
         "blk, keys, verified_direct_certifier, provenance",
         "const auto &authoritative_key = identity.key",
         "cache_adaptive_v2_commit(",
         "identity",
         "verified_direct_certifier != nullptr"}));

    REQUIRE_FALSE(resolve.empty());
    CHECK(contains_in_order(
        resolve,
        {"verified_direct_certifier != nullptr",
         "verified_direct_certifier->get_obj_hash()",
         "verified_direct_certifier->has_n(config.nmajority)",
         "verified_direct_certifier->verify(config)",
         "merge(certificate_key)",
         "blk->self_qc",
         "for (const auto &committed_key : committed_keys)",
         "if (!resolved.has_value())",
         "legal_qc_skipped_ancestor",
         "CommittedProposalIdentityDisposition::unavailable",
         "CommittedProposalIdentityDisposition::conflicting",
         "CommittedProposalIdentityDisposition::exact"}));

    REQUIRE_FALSE(cache.empty());
    const auto existing = cache.find(
        "proposal_view_generations.find(*exact_key)");
    const auto recovery = cache.find(
        "else if (allow_runtime_generation_recovery)");
    REQUIRE(existing != std::string::npos);
    REQUIRE(recovery != std::string::npos);
    CHECK(existing < recovery);
    CHECK(contains_in_order(
        cache,
        {"else if (allow_runtime_generation_recovery)",
         "find_exact_runtime_generation(",
         "observe_proposal_view_generation(",
         "generation = proposal_view_generation(*exact_key)",
         "if (!generation.has_value())",
         "CommittedProposalIdentityDisposition::",
         "conflicting"}));
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

TEST_CASE("adaptive proposal beats stop before construction while fenced",
          "[adaptive-v3][activation-readiness][leader-local-admission]")
{
    const auto implementation = source("src/hotstuff.cpp");
    const auto authority = function_body(
        implementation, "bool HotStuffBase::may_begin_local_proposal(");
    const auto local = function_body(
        implementation, "bool HotStuffBase::admit_local(");
    const auto beat = function_body(
        implementation, "void HotStuffBase::beat()");

    REQUIRE_FALSE(authority.empty());
    CHECK(contains_all(
        authority,
        {"EpochProtocolMode::adaptive_v2",
         "EpochProtocolMode::adaptive_v3",
         "activation.admits_new_proposals()",
         "activation.active_effect()",
         "proposal_contexts->active_configuration()",
         "find_exact_runtime_tree(configuration)",
         "tree->get_tree().get_tree_root() != get_id()",
         "adaptive_v3_activation_gate->may_authorize_vote("}));

    REQUIRE_FALSE(local.empty());
    CHECK(contains_in_order(
        local,
        {"may_begin_local_proposal(prop.configuration())",
         "exact_context_metadata(prop.key())",
         "pre_vote_epoch_change_gate(prop)",
         "admit_exact_context("}));

    REQUIRE_FALSE(beat.empty());
    const auto authority_check = beat.find(
        "if (!may_begin_local_proposal(configuration))");
    const auto command_reservation = beat.find(
        "reserve_adaptive_v2_command(parents, false)");
    const auto consume_commands = beat.find(
        "auto cmds = std::move(final_buffer)");
    const auto create_block = beat.find(
        "piped_block = storage->add_blk(new Block(");
    const auto local_delivery = beat.find("on_deliver_blk(piped_block)");
    REQUIRE(authority_check != std::string::npos);
    REQUIRE(command_reservation != std::string::npos);
    REQUIRE(consume_commands != std::string::npos);
    REQUIRE(create_block != std::string::npos);
    REQUIRE(local_delivery != std::string::npos);
    CHECK(authority_check < command_reservation);
    CHECK(authority_check < consume_commands);
    CHECK(authority_check < create_block);
    CHECK(authority_check < local_delivery);
    CHECK(beat.find("reason=authority_fenced") != std::string::npos);
}

TEST_CASE("active proposals pass the semantic gate before protocol mutation",
          "[c08][epoch-change][pre-vote][integration][intentional-red]")
{
    const auto implementation = source("src/hotstuff.cpp");
    const auto local = function_body(
        implementation, "bool HotStuffBase::admit_local(");
    const auto remote = function_body(
        implementation, "bool HotStuffBase::process_active(");
    const auto ingress = function_body(
        implementation, "void HotStuffBase::propose_handler(");

    REQUIRE_FALSE(local.empty());
    CHECK(contains_in_order(
        local,
        {"pre_vote_epoch_change_gate(", "admit_exact_context("}));

    REQUIRE_FALSE(remote.empty());
    const auto compact_remote = without_whitespace(remote);
    CHECK(contains_in_order(
        compact_remote,
        {"abort(\"proposal_retired\",", "return;"}));
    CHECK(contains_in_order(
        remote,
        {"delivered->get_hash() != metadata.key.block_hash",
         "owner.proposal_admission == nullptr",
         "!owner.proposal_admission->contains_admitted(",
         "metadata.key",
         "abort(",
         "\"proposal_retired\"",
         "return;",
         "pre_vote_epoch_change_gate(",
         "EpochChangeProposalDisposition::defer",
         "retain_deferred_epoch_change(",
         "return;",
         "EpochChangeProposalDisposition::rejected",
         "abort(",
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
         "abort(",
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

TEST_CASE(
    "authoritative absent-context commits retire delayed callbacks before reporting",
    "[adaptive-v2][commit][no-local-context][proposal-retirement][wiring]")
{
    const auto header = source("include/hotstuff/hotstuff.h");
    const auto implementation = source("src/hotstuff.cpp");
    const auto admission_implementation =
        source("src/proposal_admission.cpp");
    const auto remote = function_body(
        implementation, "bool HotStuffBase::process_active(");
    const auto receive = function_body(
        admission_implementation,
        "ProposalAdmissionResult ProposalAdmissionCoordinator::receive(");
    const auto activation = function_body(
        implementation,
        "void HotStuffBase::activate_proposal_configuration(");
    const auto consensus = hotstuff_consensus_body(implementation);
    const auto retire_absent = function_body(
        implementation,
        "void HotStuffBase::retire_authoritative_absent_context(");
    const auto committed = function_body(
        implementation, "void HotStuffBase::report_adaptive_v2_committed(");
    const auto enqueue = function_body(
        implementation,
        "bool HotStuffBase::try_enqueue_adaptive_v2_commit_report(");

    REQUIRE_FALSE(remote.empty());
    const auto compact_remote = without_whitespace(remote);
    CHECK(contains_in_order(
        compact_remote,
        {"abort(\"proposal_retired\",", "return;"}));
    CHECK(contains_in_order(
        remote,
        {"delivered == nullptr",
         "owner.proposal_admission == nullptr",
         "contains_admitted(",
         "metadata.key",
         "abort(",
         "\"proposal_retired\"",
         "return;",
         "pre_vote_epoch_change_gate(",
         "admit_exact_context(",
         "attempt_proposal_evidence_before_exposure(",
         "relay_once(deferred)",
         "on_receive_proposal(parsed)",
         "acquire_open_context(metadata.key)",
         "start_latency_deadline(metadata.key)",
         "start_aggregation_timer(metadata.key)",
         "drain_pending_exact_contributions(metadata.key)"}));
    CHECK(remote.find(
              "owner.epoch_protocol_mode ==\n"
              "                                EpochProtocolMode::adaptive_v3") !=
          std::string::npos);
    CHECK(remote.find("if (certified_adaptive_mode)") !=
          std::string::npos);
    CHECK(remote.find("if (!certified_adaptive_mode)") !=
          std::string::npos);
    REQUIRE_FALSE(receive.empty());
    CHECK(contains_in_order(
        receive,
        {"relay_policy_ ==",
         "ProposalRelayPolicy::eager_before_processing",
         "effects_.relay_once("}));
    REQUIRE_FALSE(activation.empty());
    CHECK(contains_in_order(
        activation,
        {"epoch_protocol_mode == EpochProtocolMode::adaptive_v2",
         "ProposalRelayPolicy::",
         "adaptive_v2_deferred_until_arm_attempt",
         "ProposalRelayPolicy::eager_before_processing"}));

    REQUIRE_FALSE(consensus.empty());
    CHECK(contains_in_order(
        consensus,
        {"proposal_contexts->close_committed_block(",
         "resolve_committed_proposal_identity(",
         "const auto &authoritative_key = identity.key",
         "authoritative_key_has_local_context",
         "std::find(",
         "keys.begin(), keys.end(), *authoritative_key",
         "retire_authoritative_absent_context(",
         "authoritative_key_has_local_context",
         "cache_adaptive_v2_commit(",
         "report_adaptive_v2_committed(",
         "authoritative_key_has_local_context"}));

    REQUIRE_FALSE(retire_absent.empty());
    CHECK(contains_in_order(
        retire_absent,
        {"authoritative_key.has_value()",
         "!authoritative_key_has_local_context",
         "proposal_admission != nullptr",
         "proposal_admission->retire_proposal(*authoritative_key)"}));

    CHECK(header.find(
              "bool initialization_predecessor_required{true};") !=
          std::string::npos);
    REQUIRE_FALSE(committed.empty());
    CHECK(contains_in_order(
        committed,
        {"commit_without_context_has_false_report_state",
         "commit_initialization_predecessor_conflicted",
         "should_defer_commit_report",
         "commit_without_context_has_response_evidence_state",
         "state.initialization_predecessor_required =",
         "initialization_predecessor_required"}));
    REQUIRE_FALSE(enqueue.empty());
    CHECK(contains_in_order(
        enqueue,
        {"adaptive_v2_durable_initialization_reports.find(key)",
         "durable->second.initialization_predecessor_required",
         "ProposalCommitted{key}",
         "enqueue_lifecycle(fact)"}));
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
        {"epoch_wire_schema_for_mode(epoch_protocol_mode)",
         "record.command_commit_height != block->get_height()",
         "record.activation_height <",
         "EpochDefinitionRequest request{",
         "*recovery_wire_schema",
         "epoch_protocol_mode",
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
        implementation, "bool HotStuffBase::process_active(");
    const auto activate = function_body(
        implementation,
        "void HotStuffBase::activate_proposal_configuration(");
    const auto retire = function_body(
        implementation,
        "void HotStuffBase::advance_committed_retirement_floor(");
    const auto retire_block = function_body(
        implementation,
        "void HotStuffBase::retire_deferred_epoch_changes_for_block(");
    const auto consensus = hotstuff_consensus_body(implementation);
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
         "abort("}));

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
    const auto consensus = hotstuff_consensus_body(implementation);

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
        {"CommitCertifierDisposition::",
         "verified_direct_certifier",
         "CommitCertifierDisposition::",
         "legal_qc_skipped_ancestor",
         "CommitCertifierDisposition::unproven",
         "b_exec = blk;"}));
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
    const auto consensus = hotstuff_consensus_body(implementation);
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
        {"may_begin_local_proposal(prop.configuration())",
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
        {"CommitCertifierDisposition::",
         "verified_direct_certifier",
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
    const auto identity_unavailable_event = function_body(
        implementation,
        "void HotStuffBase::emit_commit_identity_unavailable_event(");
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
    const auto local_admission = function_body(
        implementation, "bool HotStuffBase::admit_local(");
    const auto remote_proposal = function_body(
        implementation, "bool HotStuffBase::process_active(");
    const auto retain_proposal_bridge = function_body(
        implementation,
        "bool HotStuffBase::retain_authenticated_proposal_commit_event_identities(");
    const auto preserve_cross_epoch_intermediate = function_body(
        implementation,
        "preserve_adaptive_v3_cross_epoch_bridge_intermediate(");
    const auto rollback_proposal_bridge = function_body(
        implementation,
        "void HotStuffBase::rollback_retained_commit_event_identity_mutations(");
    const auto bridge_heights = function_body(
        implementation,
        "bool HotStuffBase::has_adjacent_proposal_commit_event_bridge_heights(");
    const auto bridge_configuration = function_body(
        implementation,
        "authenticated_proposal_commit_event_bridge_configuration(");
    const auto do_consensus = hotstuff_consensus_body(implementation);
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
    const auto report_commit = function_body(
        implementation, "void HotStuffBase::report_adaptive_v2_committed(");
    const auto commit_marker = function_body(
        implementation, "void HotStuffBase::record_adaptive_commit_marker(");
    const auto observe_commit = function_body(
        implementation, "void HotStuffBase::observe_authoritative_commit(");
    const auto retire_absent = function_body(
        implementation, "void HotStuffBase::retire_authoritative_absent_context(");

    CHECK(contains_all(
        header,
        {"AuditStructuredEventEmitter *audit_event_emitter{nullptr}",
         "AuditStructuredEventEmitter *audit_emitter",
         "maximum_proposal_view_generation_observations",
         "proposal_view_generations",
         "std::optional<std::uint64_t> view_generation",
         "std::optional<ProposalKey> committed_key",
         "CommittedProposalIdentityDisposition identity_disposition",
         "std::optional<ProposalKey> event_committed_key",
         "std::optional<std::uint64_t> event_view_generation",
         "CommittedProposalIdentityDisposition event_identity_disposition",
         "maximum_proposal_commit_event_bridge_intermediates",
         "retain_authenticated_proposal_commit_event_identities("}));
    CHECK(contains_in_order(
        consensus_header,
        {"do_post_block_commit(",
         "const block_t &",
         "std::uint64_t commit_batch_index)"}));
    CHECK(contains_in_order(
        consensus,
        {"std::uint64_t commit_batch_index = 0",
         "for (std::size_t queue_index = commit_queue.size()",
         "queue_index-- > 0",
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

    REQUIRE_FALSE(identity_unavailable_event.empty());
    CHECK(contains_all(
        identity_unavailable_event,
        {"structured_event_emitter == nullptr",
         "blk == nullptr",
         "blk->get_height()",
         "blk->get_hash()",
         "blk->get_parent_hashes()",
         "blk->get_cmds().size()",
         "commit_batch_index",
         "CommitIdentityUnavailableStructuredEvent",
         "no_authenticated_exact_identity_source",
         "false",
         "StructuredEventPayload"}));
    CHECK(identity_unavailable_event.find("ProposalKey") ==
          std::string::npos);
    CHECK(identity_unavailable_event.find("designated_observer") ==
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
        {"find_exact_runtime_generation(",
         "if (!generation.has_value())",
         "adaptive_epoch_consensus_message(",
         "if (adaptive_payload.empty())",
         "is_adaptive_epoch_mode(epoch_protocol_mode)",
         "observe_proposal_view_generation(",
         "prop.key()",
         "*generation",
         "retain_authenticated_proposal_commit_event_identities(",
         "prop, *generation, nullptr, true"}));
    CHECK(count_occurrences(
              local_proposal,
              "retain_authenticated_proposal_commit_event_identities(") ==
          1);
    REQUIRE_FALSE(local_admission.empty());
    CHECK(local_admission.find(
              "retain_authenticated_proposal_commit_event_identities(") ==
          std::string::npos);
    REQUIRE_FALSE(retain_proposal_bridge.empty());
    CHECK(contains_all(
        retain_proposal_bridge,
        {"is_adaptive_epoch_mode(epoch_protocol_mode)",
         "generation == 0",
         "proposal.key().block_hash != proposal.blk->get_hash()",
         "retain_owned(\n                proposal.key(),",
         "proposal.key().configuration.epoch_number",
         "certifier->parents.size() != 1",
         "certifier->qc_ref == nullptr",
         "maximum_proposal_commit_event_bridge_intermediates",
         "while (cursor != alternate)",
         "intermediate_count == intermediates.size()",
         "cursor->parents.size() != 1",
         "has_verified_legal_qc_skip(certifier, cursor)",
         "physical_predecessor->height + 1",
         "has_bounded_proposal_commit_event_bridge_intermediates(",
         "intermediates[index]->get_hash()",
         "certifier->qc->get_proposal_key()",
         "const auto alternate_ingress",
         "const auto certifier_ingress",
         "authenticated_proposal_ingress.find(alternate_key)",
         "authenticated_proposal_ingress.find(certifier_key)",
         "locally_constructed_certifier",
         "certifier_authority_generation",
         "authenticated_proposal_commit_event_bridge_configuration(",
         "find_exact_runtime_generation(",
         "alternate_key.configuration",
         "certifier_key.configuration",
         "certifier_key.configuration.epoch_number",
         "preserve_adaptive_v3_cross_epoch_bridge_intermediate(",
         "bridged_configuration->first",
         "bridged_configuration->second"}));
    REQUIRE_FALSE(preserve_cross_epoch_intermediate.empty());
    CHECK(contains_all(
        preserve_cross_epoch_intermediate,
        {"EpochProtocolMode::adaptive_v3",
         "retained_commit_event_identities.find(",
         "return true",
         "exact_predecessor",
         "exact_successor",
         "identity.max_observed_epoch"}));
    REQUIRE_FALSE(bridge_heights.empty());
    CHECK(contains_all(
        bridge_heights,
        {"alternate_height !=",
         "std::numeric_limits<std::uint32_t>::max()",
         "skipped_height == alternate_height + 1",
         "skipped_height !=",
         "certifier_height == skipped_height + 1"}));
    REQUIRE_FALSE(bridge_configuration.empty());
    CHECK(contains_all(
        bridge_configuration,
        {"certifier_generation == 0",
         "certifier_ingress_generation != certifier_generation",
         "alternate_configuration == certifier_configuration",
         "mode == EpochProtocolMode::adaptive_v3",
         "alternate_configuration.epoch_number + 1",
         "certifier_configuration.tree_id == 0",
         "certifier_configuration.epoch_digest !=",
         "alternate_runtime_generation.has_value()",
         "certifier_runtime_generation != certifier_generation",
         "*alternate_runtime_generation == certifier_generation",
         "alternate_ingress_generation != alternate_runtime_generation",
         "alternate_configuration, *alternate_runtime_generation"}));
    CHECK(retain_proposal_bridge.find("observe_proposal_view_generation(") ==
          std::string::npos);
    CHECK(retain_proposal_bridge.find("proposal_view_generations") ==
          std::string::npos);
    CHECK(retain_proposal_bridge.find("proposal_admission") ==
          std::string::npos);
    CHECK(retain_proposal_bridge.find("pending_adaptive_v2_commit") ==
          std::string::npos);
    CHECK(retain_proposal_bridge.find("rotate_adaptive_v2_after_commit") ==
          std::string::npos);
    REQUIRE_FALSE(rollback_proposal_bridge.empty());
    CHECK(contains_all(
        rollback_proposal_bridge,
        {"rollback.owned_mutation_count",
         "rollback.owned_mutations.size()",
         "!retained->second.key.has_value()",
         "!retained->second.view_generation.has_value()",
         "*retained->second.key != owned.key",
         "*retained->second.view_generation !=",
         "owned.view_generation",
         "retained_commit_event_identities.erase(retained)"}));
    CHECK(rollback_proposal_bridge.find("proposal_view_generations") ==
          std::string::npos);
    CHECK(rollback_proposal_bridge.find("proposal_admission") ==
          std::string::npos);
    CHECK(rollback_proposal_bridge.find("pending_adaptive_v2_commit") ==
          std::string::npos);
    REQUIRE_FALSE(remote_proposal.empty());
    CHECK(contains_in_order(
        remote_proposal,
        {"authenticated_proposal_source_replica",
         "MsgPropose message(",
         "message.postponed_parse(this)",
         "parsed.metadata().key() != proposal.metadata.key()",
         "if (!block)",
         "exact_context_metadata(parsed.key())",
         "async_deliver_blk(block->get_hash(), source)",
         "if (delivered == nullptr || !delivered->delivered)",
         "delivery_hash_mismatch",
         "proposal_retired",
         "RetainedCommitEventIdentityRollback",
         "pre_vote_epoch_change_gate(parsed)",
         "EpochChangeProposalDisposition::defer",
         "EpochChangeProposalDisposition::rejected",
         "admit_exact_context(",
         "retain_authenticated_proposal_commit_event_identities(",
         "parsed,",
         "deferred.view_generation",
         "&retained_identity_rollback",
         "const bool proposal_accepted =\n                            owner.on_receive_proposal(parsed);"}));
    CHECK(count_occurrences(
              remote_proposal,
              "retain_authenticated_proposal_commit_event_identities(") == 1);
    CHECK(count_occurrences(
              remote_proposal,
              "rollback_retained_commit_event_identity_mutations(") == 2);

    REQUIRE_FALSE(cache_commit.empty());
    CHECK(contains_all(
        cache_commit,
        {"auto disposition = identity.disposition",
         "auto exact_key = identity.key",
         "proposal_view_generations.find(*exact_key)",
         "allow_runtime_generation_recovery",
         "find_exact_runtime_generation(",
         "observe_proposal_view_generation(",
         "proposal_view_generation(*exact_key)",
         "CommittedProposalIdentityDisposition::unavailable",
         "CommittedProposalIdentityDisposition::conflicting",
         "auto event_disposition = disposition",
         "auto event_key = exact_key",
         "auto event_generation = generation",
         "retained_commit_event_identities.find(blk->get_hash())",
         "PendingAdaptiveV2Commit"}));
    CHECK(count_occurrences(
              cache_commit,
              "CommittedProposalIdentityDisposition::unavailable") == 2);
    CHECK(contains_in_order(
        cache_commit,
        {"unavailable_resolution",
         "CommittedProposalIdentityDisposition::unavailable",
         "!exact_key.has_value()",
         "identity.provenance",
         "legal_qc_skipped_ancestor"}));
    CHECK(contains_in_order(
        cache_commit,
        {"PendingAdaptiveV2Commit{",
         "blk->get_hash(),",
         "exact_key,",
         "generation,",
         "disposition,",
         "event_key,",
         "event_generation,",
         "event_disposition,"}));
    REQUIRE_FALSE(do_consensus.empty());
    CHECK(contains_in_order(
        do_consensus,
        {"record_committed_epoch_change_history(blk)",
         "retire_deferred_epoch_changes_for_block(blk->get_hash())",
         "close_committed_block(blk->get_hash())",
         "resolve_committed_proposal_identity(",
         "cache_adaptive_v2_commit(",
         "verified_direct_certifier != nullptr",
         "forget_proposal_view_generation(key)",
         "forget_proposal_view_generations_for_block(blk->get_hash())"}));
    CHECK(contains_in_order(
        do_consensus,
        {"const auto &authoritative_key = identity.key",
         "retire_authoritative_absent_context(",
         "observe_authoritative_commit(",
         "cache_adaptive_v2_commit(",
         "report_adaptive_v2_committed(",
         "pending_adaptive_v2_commit->committed_key",
         "record_adaptive_commit_marker(blk, authoritative_key)",
         "advance_committed_retirement_floor(blk, authoritative_key)"}));
    CHECK(do_consensus.find("event_") == std::string::npos);
    REQUIRE_FALSE(report_commit.empty());
    REQUIRE_FALSE(commit_marker.empty());
    REQUIRE_FALSE(observe_commit.empty());
    REQUIRE_FALSE(retire_absent.empty());
    CHECK(report_commit.find("event_committed_key") == std::string::npos);
    CHECK(report_commit.find("event_view_generation") == std::string::npos);
    CHECK(commit_marker.find("event_committed_key") == std::string::npos);
    CHECK(commit_marker.find("event_view_generation") == std::string::npos);
    CHECK(observe_commit.find("event_") == std::string::npos);
    CHECK(retire_absent.find("event_") == std::string::npos);
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
              post_commit, "emit_commit_observed_event(") == 2);
    CHECK(count_occurrences(
              post_commit, "emit_committed_block_event(") == 2);
    CHECK(count_occurrences(
              post_commit,
              "emit_commit_identity_unavailable_event(") == 2);
    CHECK(count_occurrences(
              post_commit, "emit_epoch_command_committed_event(") == 1);
    CHECK(contains_in_order(
        post_commit,
        {"EpochProtocolMode::adaptive_v2",
         "if (blk == nullptr)",
         "emit_commit_observed_event(",
         "std::optional<ProposalKey> committed_key",
         "committed_key = pending_adaptive_v2_commit->committed_key",
         "event_committed_key =",
         "->event_committed_key",
         "event_view_generation =",
         "->event_view_generation",
         "event_identity_disposition =",
         "->event_identity_disposition",
         "pending_adaptive_v2_commit.reset()",
         "CommittedProposalIdentityDisposition::unavailable",
         "emit_commit_identity_unavailable_event(",
         "CommittedProposalIdentityDisposition::exact",
         "emit_committed_block_event(",
         "event_committed_key",
         "event_view_generation",
         "reporter_local_commit_monotonic_ns"}));
    CHECK(contains_in_order(
        post_commit,
        {"record_committed_v2(command, blk->get_height())",
         "ActivationRecordDisposition::recorded",
         "recorded.record.has_value()",
         "emit_epoch_command_committed_event(",
         "pending_committed_epoch_change.reset()",
         "on_v2_post_block_commit("}));
    CHECK(post_commit.find(
              "emit_committed_block_event(\n                blk,\n                committed_key") ==
          std::string::npos);
    CHECK(post_commit.find(
              "on_v2_post_block_commit(\n                blk, event_committed_key") ==
          std::string::npos);
    CHECK(post_commit.find(
              "rotate_adaptive_v2_after_commit(event_committed_key)") ==
          std::string::npos);
}

TEST_CASE("every adaptive topology publication emits active configuration",
          "[adaptive-v2][adaptive-v3][structured-event][topology][runtime]")
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
         "EpochProtocolMode::adaptive_v3",
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

TEST_CASE("adaptive v3 uses the exact serialized rotation owner",
          "[cert13][adaptive-v3][rotation][runtime]")
{
    const auto implementation = source("src/hotstuff.cpp");
    const auto application = source("examples/hotstuff_app.cpp");
    const auto runtime = source("src/epoch_runtime.cpp");
    const auto initialize = function_body(
        implementation,
        "void HotStuffBase::initialize_adaptive_epoch_runtime()");
    const auto configure = function_body(
        implementation, "void HotStuffBase::set_tree_period(size_t nblocks)");
    const auto start = function_body(
        implementation, "void HotStuffBase::start(");
    const auto post_commit = function_body(
        implementation, "void HotStuffBase::do_post_block_commit(");
    const auto v3_post_commit = function_body(
        implementation,
        "void HotStuffBase::process_adaptive_v3_post_block_commit(");
    const auto periodic = function_body(
        implementation,
        "void HotStuffBase::rotate_adaptive_v2_after_commit(");
    const auto local_command = function_body(
        implementation,
        "void HotStuffBase::on_local_proposal_processed(");
    const auto timeout = function_body(
        implementation,
        "HotStuffBase::rotate_tree_on_leader_timeout(");
    const auto publish = function_body(
        implementation,
        "void HotStuffBase::publish_adaptive_v3_activation(");
    const auto finish_v3 = function_body(
        runtime, "std::optional<EpochRuntimeUpdate> finish_v3_activation(");
    const auto rotate_runtime = function_body(
        runtime, "HotStuffEpochRuntimeAdapter::rotate_to_tree(");

    REQUIRE_FALSE(initialize.empty());
    CHECK(contains_in_order(
        application,
        {"opt_epoch_protocol_mode->get() == \"adaptive_v2\"",
         "opt_epoch_protocol_mode->get() == \"adaptive_v3\"",
         "adaptive tree switch period must be a finite positive integer"}));
    CHECK(contains_in_order(
        application,
        {"epoch_protocol_mode == EpochProtocolMode::adaptive_v2",
         "epoch_protocol_mode == EpochProtocolMode::adaptive_v3",
         "static_cast<std::size_t>(tree_switch_period)"}));
    CHECK(contains_in_order(
        initialize,
        {"EpochProtocolMode::adaptive_v2",
         "EpochProtocolMode::adaptive_v3",
         "adaptive_v2_tree_switch_period",
         "AdaptiveV2RotationCoordinator"}));

    REQUIRE_FALSE(configure.empty());
    CHECK(contains_in_order(
        configure,
        {"EpochProtocolMode::adaptive_v2",
         "EpochProtocolMode::adaptive_v3",
         "adaptive_v2_tree_switch_period = nblocks"}));

    REQUIRE_FALSE(start.empty());
    CHECK(contains_in_order(
        start,
        {"EpochProtocolMode::adaptive_v2",
         "EpochProtocolMode::adaptive_v3",
         "adaptive_v2_tree_switch_period.has_value()"}));

    REQUIRE_FALSE(post_commit.empty());
    CHECK(contains_in_order(
        post_commit,
        {"EpochProtocolMode::adaptive_v3",
         "committed_key =",
         "committed_generation =",
         "observational_commit =",
         "process_adaptive_v3_post_block_commit(",
         "observational_commit.has_value()",
         "rotate_adaptive_v2_after_commit(committed_key)"}));

    REQUIRE_FALSE(v3_post_commit.empty());
    CHECK(contains_in_order(
        v3_post_commit,
        {"committed_key->block_hash != block->get_hash()",
         "*committed_generation > active.generation",
         "exact_epochs->find_tree(",
         "predecessor_configuration =",
         "committed_key->configuration",
         "predecessor_generation = *committed_generation"}));

    REQUIRE_FALSE(periodic.empty());
    CHECK(contains_in_order(
        periodic,
        {"EpochProtocolMode::adaptive_v2",
         "EpochProtocolMode::adaptive_v3",
         "adaptive_v3_activation_gate",
         "adaptive_v2_command_inbox->snapshot()",
         "AdaptiveV2CommandInboxState::available",
         "AdaptiveV2CommandInboxState::reserved",
         "AdaptiveV2CommandInboxState::in_flight",
         "adaptive_v2_rotation_coordinator->on_commit("}));
    CHECK(periodic.find("adaptive_v2_command_inbox->snapshot()") <
          periodic.find("adaptive_epoch_runtime->activation.active_effect()"));

    REQUIRE_FALSE(local_command.empty());
    CHECK(contains_in_order(
        local_command,
        {"EpochProtocolMode::adaptive_v3",
         "pmaker->grant_bounded_epoch_command_window(",
         "adaptive_v3_pending_command_priority_fanout = key"}));

    REQUIRE_FALSE(timeout.empty());
    CHECK(contains_in_order(
        timeout,
        {"EpochProtocolMode::adaptive_v1",
         "EpochProtocolMode::adaptive_v2",
         "EpochProtocolMode::adaptive_v3",
         "adaptive_v3_activation_gate",
         "adaptive_v2_rotation_coordinator->on_timeout("}));

    REQUIRE_FALSE(publish.empty());
    CHECK(contains_in_order(
        publish,
        {"adaptive_v2_rotation_coordinator->reset_for_activation()",
         "adaptive_v3_activation_gate.reset()"}));

    REQUIRE_FALSE(finish_v3.empty());
    CHECK(contains_in_order(
        finish_v3,
        {"state.future_drain_configuration = update.activation.configuration",
         "state.v3_gate = nullptr",
         "return update"}));

    REQUIRE_FALSE(rotate_runtime.empty());
    CHECK(contains_in_order(
        rotate_runtime,
        {"state_->mode == EpochProtocolMode::adaptive_v2",
         "state_->mode == EpochProtocolMode::adaptive_v3",
         "state_->future_drain_configuration =",
         "update.activation.configuration"}));
}

TEST_CASE("adaptive v2 rotates only after exact post-commit cadence",
          "[c08][adaptive-v2][commit-cadence][post-block]")
{
    const auto header = source("include/hotstuff/hotstuff.h");
    const auto runtime_header = source("include/hotstuff/epoch_runtime.h");
    const auto runtime = source("src/epoch_runtime.cpp");
    const auto implementation = source("src/hotstuff.cpp");
    const auto consensus = hotstuff_consensus_body(implementation);
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
         "resolve_committed_proposal_identity(",
         "cache_adaptive_v2_commit("}));
    CHECK(consensus.find("rotate_adaptive_v2_after_commit(") ==
          std::string::npos);

    REQUIRE_FALSE(cache.empty());
    CHECK(contains_in_order(
        cache,
        {"EpochProtocolMode::adaptive_v2",
         "proposal_view_generations.find(*exact_key)",
         "allow_runtime_generation_recovery",
         "find_exact_runtime_generation(",
         "proposal_view_generation(*exact_key)",
         "pending_adaptive_v2_commit.emplace(",
         "PendingAdaptiveV2Commit"}));
    CHECK(cache.find("observed_committed_proposal_key(") ==
          std::string::npos);
    CHECK(contains_in_order(
        cache,
        {"identity.key",
         "generation",
         "blk->get_hash(),",
         "exact_key,",
         "generation,",
         "disposition,",
         "event_key,",
         "event_generation,",
         "event_disposition,",
         "std::nullopt"}));

    REQUIRE_FALSE(post_commit.empty());
    CHECK(contains_in_order(
        post_commit,
        {"pending_adaptive_v2_commit",
         "block_hash == blk->get_hash()",
         "committed_key =",
         "event_committed_key =",
         "event_view_generation =",
         "event_identity_disposition =",
         "pending_adaptive_v2_commit.reset()",
         "epoch_live_binding->on_v2_post_block_commit(",
         "finish_adaptive_epoch_commit(blk, activation)",
         "rotate_adaptive_v2_after_commit(committed_key)"}));
    CHECK(post_commit.find(
              "rotate_adaptive_v2_after_commit(event_view_generation)") ==
          std::string::npos);
    CHECK(post_commit.find(
              "rotate_adaptive_v2_after_commit(event_committed_key)") ==
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
              "epoch protocol mode (legacy_static, adaptive_v1, adaptive_v2, adaptive_v3)") !=
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

TEST_CASE(
    "adaptive v2 executable loads retention schema v2 from main config syntax",
    "[adaptive-v2][evidence][retention-v2][cli][config][subprocess]"
    "[v40][intentional-red]")
{
    auto arguments = valid_adaptive_v2_arguments();
    const auto config_path =
        arguments.temporary_directory + "/main.conf";
    {
        std::ofstream config(config_path, std::ios::binary);
        REQUIRE(config.good());
        config <<
            "experiment-responsive-cross-commit-retention-v2 = true\n";
        REQUIRE(config.good());
    }
    arguments.values.insert(
        arguments.values.end(),
        {"--conf", config_path,
         "--replica", "127.0.0.1:19000,unused,unused"});

    const auto result = run_hotstuff_app(arguments.values);
    ::unlink(config_path.c_str());

    CHECK(result.status != 0);
    CHECK(result.output.find(
              "experiment responsive cross-commit retention v2 requires "
              "tiered responsive omission v2 actors") !=
          std::string::npos);
    CHECK(result.output.find("replica idx out of range") ==
          std::string::npos);
}

TEST_CASE(
    "exact timeout attempt evidence v3 is explicit and evidence-only",
    "[adaptive-v2][evidence][schema-v3][cli][wiring]")
{
    const auto app = source("examples/hotstuff_app.cpp");
    const auto header = source("include/hotstuff/hotstuff.h");
    const auto implementation = source("src/hotstuff.cpp");
    const auto bridge_implementation =
        source("src/adaptive_v2_response_evidence.cpp");
    const auto main = function_body(app, "int main(");
    const auto enable = function_body(
        implementation,
        "void HotStuffBase::enable_experiment_exact_timeout_attempt_evidence_v3(");
    const auto bridge_enable = function_body(
        bridge_implementation,
        "AdaptiveV2ResponseEvidenceBridge::\n"
        "enable_exact_timeout_attempt_evidence_v3(");

    const auto binding = option_binding(
        app, "experiment-exact-timeout-attempt-evidence-v3");
    REQUIRE(binding.has_value());
    CHECK(*binding ==
          "opt_experiment_exact_timeout_attempt_evidence_v3");
    const auto option = app.find(
        "\"experiment-exact-timeout-attempt-evidence-v3\"");
    REQUIRE(option != std::string::npos);
    const auto option_end = app.find(';', option);
    REQUIRE(option_end != std::string::npos);
    CHECK(app.substr(option, option_end - option).find("Config::SWITCH_ON") !=
          std::string::npos);

    REQUIRE_FALSE(main.empty());
    CHECK(contains_in_order(
        main,
        {"opt_experiment_exact_timeout_attempt_evidence_v3->get()",
         "papp->enable_experiment_exact_timeout_attempt_evidence_v3()",
         "papp->start(reps)"}));
    CHECK(count_occurrences(
              main,
              "papp->enable_experiment_exact_timeout_attempt_evidence_v3()") ==
          1);

    REQUIRE_FALSE(enable.empty());
    CHECK(contains_in_order(
        enable,
        {"!is_adaptive_epoch_mode(epoch_protocol_mode)",
         "proposal_contexts->active_configuration().has_value()",
         "adaptive_v2_response_evidence == nullptr",
         "enable_exact_timeout_attempt_evidence_v3()",
         "experiment_exact_timeout_attempt_evidence_v3 = true"}));
    REQUIRE_FALSE(bridge_enable.empty());
    CHECK(contains_in_order(
        bridge_enable,
        {"auto &state = *state_",
         "!state.healthy",
         "!state.handles.empty()",
         "state.reporter.enable_exact_timeout_attempt_evidence_v3()"}));

    CHECK(header.find(
              "bool experiment_exact_timeout_attempt_evidence_v3{false};") !=
          std::string::npos);
    CHECK(count_occurrences(
              implementation,
              "experiment_exact_timeout_attempt_evidence_v3") == 3);
    const auto constructor = function_body(
        implementation, "HotStuffBase::HotStuffBase(");
    const auto enqueue = function_body(
        implementation,
        "EvidenceTransportResult HotStuffBase::enqueue_adaptive_v2_evidence_report(");
    REQUIRE_FALSE(constructor.empty());
    REQUIRE_FALSE(enqueue.empty());
    CHECK(contains_in_order(
        constructor,
        {"epoch_protocol_mode == EpochProtocolMode::adaptive_v3",
         "std::make_unique<AdaptiveV2ResponseEvidenceBridge>",
         "bind_transport",
         "enqueue_adaptive_v2_evidence_report"}));
    CHECK(contains_in_order(
        enqueue,
        {"epoch_protocol_mode == EpochProtocolMode::adaptive_v3",
         "enqueue_evidence",
         "EvidenceTransportResult::accepted"}));
    for (const auto *forbidden : {
             "do_consensus",
             "do_vote",
             "do_broadcast_proposal",
             "send_exact_relay",
             "epoch_live_binding",
             "fixed_quorum_size",
             "on_receive_proposal",
             "on_receive_vote"})
    {
        CAPTURE(forbidden);
        CHECK(enable.find(forbidden) == std::string::npos);
        CHECK(bridge_enable.find(forbidden) == std::string::npos);
    }
}

TEST_CASE(
    "fault-window arm CLI partitions schema-specific evidence bindings",
    "[adaptive-v2][fault-window-arm][v7][cli][wiring]")
{
    const auto manager = source("examples/adaptation_manager.cpp");
    CHECK(contains_in_order(
        manager,
        {"opt_fault_window_arm_snapshot_evidence_basis",
         "opt_fault_window_arm_selection_cardinality_policy",
         "\"fault-window-arm-snapshot-evidence-basis\"",
         "\"fault-window-arm-selection-cardinality-policy\"",
         "const std::array<const std::string *, 13> common_arm_values",
         "const std::array<const std::string *, 5> extended_arm_values",
         "const auto timeout_evidence_basis =",
         "const auto required_observation_schema =",
         "const auto clock_domain =",
         "const auto snapshot_evidence_basis =",
         "const auto selection_cardinality_policy =",
         "arm.schema_version == 4"}));
    CHECK(contains_all(
        manager,
        {"arm.schema_version == 1 &&",
         "!timeout_evidence_basis.empty()",
         "!required_observation_schema.empty()",
         "!clock_domain.empty()",
         "!snapshot_evidence_basis.empty()",
         "!selection_cardinality_policy.empty()",
         "common_arm_values",
         "extended_arm_values",
         "extended_arm_values.begin(), extended_arm_values.end()",
         "arm.schema_version == 2 && (arm.domain != \"kauri-focused-fault-window-arm-v2\" ||",
         "!arm.snapshot_evidence_basis.empty()",
         "arm.schema_version == 3 && (arm.domain != \"kauri-focused-fault-window-arm-v3\" ||",
         "arm.snapshot_evidence_basis != \"exact_post_fault_attempt_start_v1\"",
         "arm.schema_version == 4 &&",
         "arm.domain != \"kauri-focused-fault-window-arm-v4\"",
         "arm.selection_cardinality_policy !=",
         "\"all_guarded_up_to_fault_bound_v1\""}));
}

TEST_CASE(
    "retention v2 shares one reporter-local commit sample with rich audit",
    "[adaptive-v2][evidence][retention-v2][commit][wiring][v40]")
{
    const auto implementation = source("src/hotstuff.cpp");
    const auto report = function_body(
        implementation, "void HotStuffBase::report_adaptive_v2_committed(");
    const auto post = function_body(
        implementation, "void HotStuffBase::do_post_block_commit(");
    const auto emit = function_body(
        implementation, "void HotStuffBase::emit_committed_block_event(");
    REQUIRE_FALSE(report.empty());
    REQUIRE_FALSE(post.empty());
    REQUIRE_FALSE(emit.empty());

    CHECK(count_occurrences(
              report, "adaptive_evidence_monotonic_now_ns()") == 1);
    CHECK(contains_in_order(
        report,
        {"const auto local_commit_monotonic_ns =",
         "record_reporter_local_commit(",
         "*key, local_commit_monotonic_ns",
         "reporter_local_commit_monotonic_ns =",
         "local_commit_monotonic_ns"}));
    CHECK(contains_in_order(
        post,
        {"pending_adaptive_v2_commit",
         "reporter_local_commit_monotonic_ns",
         "emit_committed_block_event(",
         "reporter_local_commit_monotonic_ns"}));
    CHECK(emit.find("reporter_local_commit_monotonic_ns") !=
          std::string::npos);
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
    CHECK(rejected.output.find("adaptive tree switch period") !=
          std::string::npos);
    CHECK(rejected.output.find("replica idx out of range") ==
          std::string::npos);

    const auto adaptive_v1 = run_hotstuff_app(
        {"--epoch-protocol-mode", "adaptive_v1",
         "--tree-switch-period", "0"});
    CHECK(adaptive_v1.status != 0);
    CHECK(adaptive_v1.output.find("replica idx out of range") !=
          std::string::npos);
    CHECK(adaptive_v1.output.find("adaptive tree switch period") ==
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
        CHECK(rejected.output.find("adaptive tree switch period") !=
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
        CHECK(compatible.output.find("adaptive tree switch period") ==
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

TEST_CASE(
    "adaptive v2 attempts deadline arm before exposure without gating consensus",
    "[adaptive-v2][evidence][deadline-arm][relay-order][wiring]")
{
    const auto implementation = source("src/hotstuff.cpp");
    const auto header = source("include/hotstuff/hotstuff.h");
    const auto remote = function_body(
        implementation, "bool HotStuffBase::process_active(");
    const auto root = function_body(
        implementation, "void HotStuffBase::do_broadcast_proposal(");
    const auto attempt = function_body(
        implementation,
        "void HotStuffBase::attempt_proposal_evidence_before_exposure(");
    const auto poison = function_body(
        implementation,
        "void HotStuffBase::poison_response_attempt_arm_once(");
    const auto finalized = function_body(
        implementation,
        "void HotStuffBase::\n"
        "    ensure_finalized_proposal_evidence_before_exposure(");

    REQUIRE_FALSE(remote.empty());
    CHECK(contains_in_order(
        remote,
        {"attempt_proposal_evidence_before_exposure(",
         "relay_once(deferred)",
         "on_receive_proposal(parsed)"}));

    REQUIRE_FALSE(attempt.empty());
    CHECK(contains_in_order(
        attempt,
        {"create_expected_vote_state(key)",
         "start_latency_deadline(key)",
         "poison_response_attempt_arm_once(key, arm_failure_reason)",
         "start_aggregation_timer(key)",
         "aggregation_timer_failed_before_proposal_exposure"}));
    CHECK(attempt.find("catch (...)") != std::string::npos);

    REQUIRE_FALSE(poison.empty());
    CHECK(contains_all(
        poison,
        {"response_attempt_arm_failure_markers.insert(key).second",
         "KAURI_EVIDENCE response_attempt_arm_marker_failed",
         "successful_response_attempt_arm_provenance.erase(key)",
         "mark_adaptive_v2_convergence_evidence_unhealthy(reason)"}));

    REQUIRE_FALSE(finalized.empty());
    CHECK(contains_in_order(
        finalized,
        {"has_successful_response_attempt_arm(key)",
         "attempt_proposal_evidence_before_exposure("}));

    REQUIRE_FALSE(root.empty());
    CHECK(contains_in_order(
        root,
        {"attempt_proposal_evidence_before_exposure(",
         "for (const auto child : metadata->tree.direct_children)",
         "schedule_exact_proposal_fallback(*lease, prop)"}));
    CHECK(root.find(
              "root_response_deadline_arm_failed_before_") !=
          std::string::npos);
    CHECK(contains_in_order(
        root,
        {"ensure_finalized_proposal_evidence_before_exposure(",
         "for (const auto child : metadata->tree.direct_children)"}));
    CHECK(root.find(
              "if (!has_successful_response_attempt_arm(prop.key()))") ==
          std::string::npos);
    CHECK(header.find(
              "bool start_latency_deadline(const ProposalKey &key) override") !=
          std::string::npos);
}

TEST_CASE(
    "adaptive v2 evidence lifecycle failures are globally fail closed",
    "[adaptive-v2][evidence][lifecycle][fail-closed][wiring]")
{
    const auto implementation = source("src/hotstuff.cpp");
    const auto poison = function_body(
        implementation,
        "void HotStuffBase::poison_adaptive_v2_reporting(");
    const auto acknowledgement = function_body(
        implementation,
        "void HotStuffBase::adaptive_v2_convergence_ack_handler(");
    const auto flush = function_body(
        implementation,
        "void HotStuffBase::flush_adaptive_v2_reporting(");
    const auto committed = function_body(
        implementation,
        "void HotStuffBase::report_adaptive_v2_committed(");
    const auto deadline = function_body(
        implementation,
        "bool HotStuffBase::start_latency_deadline(");
    const auto forget_one = function_body(
        implementation,
        "void HotStuffBase::forget_proposal_view_generation(");
    const auto forget_block = function_body(
        implementation,
        "void HotStuffBase::forget_proposal_view_generations_for_block(");
    const auto forget_epoch = function_body(
        implementation,
        "void HotStuffBase::forget_proposal_view_generations_before_epoch(");

    REQUIRE_FALSE(poison.empty());
    CHECK(contains_in_order(
        poison,
        {"suppress_adaptive_v2_lifecycle_reporting(reason)",
         "adaptive_v2_reporting_outbox->shutdown()",
         "cancel_adaptive_v2_reporting_flush()"}));

    REQUIRE_FALSE(acknowledgement.empty());
    CHECK(contains_in_order(
        acknowledgement,
        {"AdaptiveV2ReportingTransitionStatus::failed",
         "poison_adaptive_v2_reporting(",
         "convergence_observation_permanently_rejected"}));

    REQUIRE_FALSE(flush.empty());
    CHECK(count_occurrences(
              flush, "poison_adaptive_v2_reporting(") == 4);
    CHECK(contains_all(
        flush,
        {"shared_outbox_unhealthy",
         "shared_outbox_terminal_report_missing",
         "shared_outbox_terminal_report",
         "shared_outbox_delivery_failed"}));

    REQUIRE_FALSE(committed.empty());
    CHECK(contains_in_order(
        committed,
        {"!pending_adaptive_v2_commit.has_value()",
         "mark_adaptive_v2_convergence_evidence_unhealthy(",
         "authoritative_commit_identity_mismatched_or_conflicted",
         "CommittedProposalIdentityDisposition::unavailable",
         "const bool exact_unavailable",
         "!key.has_value()",
         "!cached.committed_key.has_value()",
         "!cached.view_generation.has_value()",
         "adaptive_v2_committed_convergence_identity.has_value()",
         "cached.block_hash != convergence.command_block_hash",
         "!pending_committed_epoch_change.has_value()",
         "pending_committed_epoch_change->block_hash !=",
         "cached.block_hash",
         "!distinct_from_convergence_command",
         "!distinct_from_pending_epoch_change",
         "CommittedProposalIdentityDisposition::conflicting",
         "event_identity_disposition =",
         "CommittedProposalIdentityDisposition::conflicting",
         "authoritative_commit_identity_mismatched_or_",
         "conflicted",
         "CommittedProposalIdentityDisposition::exact"}));
    CHECK(committed.find(
              "authoritative_commit_identity_unavailable_while_") ==
          std::string::npos);
    CHECK(count_occurrences(
              committed,
              "authoritative_commit_identity_mismatched_or_conflicted") ==
          3);

    REQUIRE_FALSE(deadline.empty());
    CHECK(contains_in_order(
        deadline,
        {"if (!evidence_armed)",
         "cancel_false_report(",
         "adaptive_v2_response_evidence->retire(key)",
         "suppress_adaptive_v2_lifecycle_reporting(",
         "false_report_evidence_arm_failed"}));

    REQUIRE_FALSE(forget_one.empty());
    CHECK(forget_one.find(
              "retire_adaptive_v2_runtime_initialized_report(key)") !=
          std::string::npos);
    REQUIRE_FALSE(forget_block.empty());
    CHECK(forget_block.find(
              "adaptive_v2_runtime_initialization_is_referenced(") !=
          std::string::npos);
    REQUIRE_FALSE(forget_epoch.empty());
    CHECK(forget_epoch.find(
              "adaptive_v2_runtime_initialization_is_referenced(") !=
          std::string::npos);
}

TEST_CASE(
    "adaptive v3 stale proposal repair applies certified progress without votes",
    "[cert13][adaptive-v3][proposal-repair][catch-up][wiring]")
{
    const auto implementation = source("src/hotstuff.cpp");
    const auto catch_up = function_body(
        implementation,
        "bool HotStuffBase::process_exact_proposal_catchup(");
    const auto ingress = function_body(
        implementation,
        "void HotStuffBase::adaptive_propose_handler(");
    const auto initial_repair = function_body(
        implementation,
        "bool HotStuffBase::broadcast_exact_proposal_fallback(");
    const auto retry_repair = function_body(
        implementation,
        "bool HotStuffBase::broadcast_exact_proposal_pre_quorum_retry(");
    const auto tail_repair = function_body(
        implementation,
        "bool HotStuffBase::broadcast_exact_proposal_repair_tail(");

    REQUIRE_FALSE(catch_up.empty());
    CHECK(contains_in_order(
        catch_up,
        {"EpochProtocolMode::adaptive_v3",
         "EpochConsensusWireKind::proposal_repair",
         "message.postponed_parse(this)",
         "proposal.metadata().key() != envelope.key()",
         "async_deliver_blk(expected_hash, source)",
         "on_receive_certified_proposal_catchup(\n"
         "                                proposal, generation)",
         "KAURI_PROPOSAL_CATCHUP"}));
    const auto certified_catch_up = function_body(
        source("src/consensus.cpp"),
        "HotStuffCore::on_receive_certified_proposal_catchup(");
    REQUIRE_FALSE(certified_catch_up.empty());
    CHECK(contains_in_order(
        certified_catch_up,
        {"block->qc->has_n(config.nmajority)",
         "block->verify(this)",
         "on_verified_certified_proposal_catchup(",
         "update(block)",
         "on_qc_finish(block->qc_ref)"}));
    CHECK(catch_up.find("on_receive_proposal") == std::string::npos);
    CHECK(catch_up.find("do_vote") == std::string::npos);
    CHECK(catch_up.find("relay_once") == std::string::npos);
    CHECK(catch_up.find("admit_exact_context") == std::string::npos);
    CHECK(catch_up.find("create_expected_vote_state") ==
          std::string::npos);
    CHECK(catch_up.find("start_latency_deadline") == std::string::npos);
    CHECK(catch_up.find("start_aggregation_timer") ==
          std::string::npos);

    REQUIRE_FALSE(ingress.empty());
    CHECK(contains_in_order(
        ingress,
        {"epoch_live_binding->handle_proposal(",
         "EpochConsensusPermission::catch_up_only",
         "process_exact_proposal_catchup("}));

    REQUIRE_FALSE(initial_repair.empty());
    CHECK(initial_repair.find("EpochConsensusWireKind::proposal") !=
          std::string::npos);
    CHECK(initial_repair.find("EpochConsensusWireKind::proposal_repair") ==
          std::string::npos);

    REQUIRE_FALSE(retry_repair.empty());
    CHECK(retry_repair.find("EpochConsensusWireKind::proposal") !=
          std::string::npos);
    CHECK(retry_repair.find("EpochConsensusWireKind::proposal_repair") ==
          std::string::npos);

    REQUIRE_FALSE(tail_repair.empty());
    CHECK(contains_in_order(
        tail_repair,
        {"job.tail_armed",
         "epoch_protocol_mode == EpochProtocolMode::adaptive_v3",
         "EpochConsensusWireKind::proposal_repair",
         "EpochConsensusWireKind::proposal",
         "adaptive_epoch_consensus_message("}));
    CHECK(tail_repair.find("active.configuration != job.key.configuration") ==
          std::string::npos);
    CHECK(tail_repair.find("active.generation != job.epoch_generation") ==
          std::string::npos);

    const auto broadcast = function_body(
        implementation,
        "void HotStuffBase::do_broadcast_proposal(");
    const auto post_commit_repair = function_body(
        implementation,
        "HotStuffBase::encode_adaptive_v3_post_commit_proposal_repair(");
    REQUIRE_FALSE(broadcast.empty());
    REQUIRE_FALSE(post_commit_repair.empty());
    CHECK(contains_all(
        post_commit_repair,
        {"EpochProtocolMode::adaptive_v3",
         "active.configuration != proposal.configuration()",
         "generation < active.generation",
         "EpochConsensusWireKind::proposal_repair"}));
    CHECK(contains_in_order(
        broadcast,
        {"adaptive_v3_post_commit_repair_payload =",
         "encode_adaptive_v3_post_commit_proposal_repair(",
         "if (!adaptive_v3_post_commit_repair_payload.empty())",
         "metadata->tree.assigned_subtree",
         "pn.send_msg_urgent(",
         "stage=v3_post_commit_repair_fanout"}));
}
