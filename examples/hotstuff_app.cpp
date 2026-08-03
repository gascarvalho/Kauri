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

#include <iostream>
#include <cstring>
#include <cassert>
#include <algorithm>
#include <cctype>
#include <charconv>
#include <cmath>
#include <csignal>
#include <exception>
#include <limits>
#include <memory>
#include <optional>
#include <random>
#include <system_error>
#include <unistd.h>
#include <signal.h>

#include "salticidae/stream.h"
#include "salticidae/util.h"
#include "salticidae/network.h"
#include "salticidae/msg.h"

#include "hotstuff/promise.hpp"
#include "hotstuff/type.h"
#include "hotstuff/entity.h"
#include "hotstuff/util.h"
#include "hotstuff/client.h"
#include "hotstuff/hotstuff.h"
#include "hotstuff/liveness.h"
#include "hotstuff/structured_event.h"

using salticidae::_1;
using salticidae::_2;
using salticidae::ClientNetwork;
using salticidae::Config;
using salticidae::ElapsedTime;
using salticidae::MsgNetwork;
using salticidae::split;
using salticidae::static_pointer_cast;
using salticidae::trim_all;

using hotstuff::bytearray_t;
using hotstuff::command_t;
using hotstuff::CommandDummy;
using hotstuff::DataStream;
using hotstuff::EventContext;
using hotstuff::EpochProtocolMode;
using hotstuff::Finality;
using hotstuff::get_hash;
using hotstuff::HotStuffError;
using hotstuff::MsgDeployEpoch;
using hotstuff::MsgDeployEpochReputation;
using hotstuff::MsgReqCmd;
using hotstuff::MsgRespCmd;
using hotstuff::NetAddr;
using hotstuff::opcode_t;
using hotstuff::promise_t;
using hotstuff::ReplicaID;
using hotstuff::TimerEvent;
using hotstuff::uint256_t;

using HotStuff = hotstuff::HotStuffAgg;

class HotStuffApp : public HotStuff
{
    double stat_period;
    EventContext ec;
    EventContext req_ec;
    EventContext resp_ec;
    /** Network messaging between a replica and its client. */
    ClientNetwork<opcode_t> cn;
    /** Timer object to schedule a periodic printing of system statistics */
    TimerEvent ev_stat_timer;
    /** The listen address for client RPC */
    NetAddr clisten_addr;
    std::string adaptive_epoch_file;
    std::uint64_t adaptive_activation_height{0};

    std::unordered_map<const uint256_t, promise_t> unconfirmed;

    using conn_t = ClientNetwork<opcode_t>::conn_t;
    using resp_queue_t = salticidae::MPSCQueueEventDriven<std::pair<Finality, NetAddr>>;

    /* for the dedicated thread sending responses to the clients */
    std::thread req_thread;
    std::thread resp_thread;
    resp_queue_t resp_queue;
    salticidae::BoxObj<salticidae::ThreadCall> resp_tcall;
    salticidae::BoxObj<salticidae::ThreadCall> req_tcall;

    void epoch_handler(MsgDeployEpochReputation &&, const conn_t &);

    static command_t parse_cmd(DataStream &s)
    {
        auto cmd = new CommandDummy();
        s >> *cmd;
        return cmd;
    }

    void state_machine_execute(const Finality &fin) override
    {
        HOTSTUFF_LOG_DEBUG("executing command: %s", std::string(fin));
    }

#ifdef HOTSTUFF_MSG_STAT
    std::unordered_set<conn_t> client_conns;
    void print_stat() const;
#endif

public:
    HotStuffApp(uint32_t blk_size,
                double stat_period,
                ReplicaID idx,
                const bytearray_t &raw_privkey,
                NetAddr plisten_addr,
                NetAddr clisten_addr,
                hotstuff::pacemaker_bt pmaker,
                const EventContext &ec,
                size_t nworker,
                const Net::Config &repnet_config,
                const ClientNetwork<opcode_t>::Config &clinet_config,
                NetAddr reputation_addr,
                EpochProtocolMode protocol_mode);

    void start(
        const std::vector<
            std::tuple<NetAddr, bytearray_t, bytearray_t>> &reps);
    void bind_process_lifecycle_emitter(
        hotstuff::StructuredEventEmitter *emitter) noexcept;
    void set_fanout(int32_t fanout);
    void set_piped_latency(int32_t piped_latency, int32_t async_blocks);
    void set_tree_period(size_t nblocks);
    void set_tree_generation(std::string genAlgo, std::string fpath);
    void set_new_epoch(std::string new_epoch);
    void set_adaptive_bootstrap(
        std::string epoch_file,
        std::uint64_t activation_height);
    void set_client_ip(std::string client_ip);
    void stop();

private:
    hotstuff::StructuredEventEmitter *process_lifecycle_emitter_{nullptr};
    bool process_stopping_emitted_{false};
};

std::pair<std::string, std::string> split_ip_port_cport(const std::string &s)
{
    auto ret = trim_all(split(s, ";"));
    if (ret.size() != 2)
        throw std::invalid_argument("invalid cport format");
    return std::make_pair(ret[0], ret[1]);
}

struct AdaptiveV2PreVoteConfig
{
    hotstuff::EpochChangeIssuer issuer;
    hotstuff::EpochChangeDelayBounds delay_bounds;
    std::size_t maximum_block_extra_bytes{0};
    std::size_t maximum_ancestry_blocks{0};
};

struct AdaptiveV2ManagerPin
{
    NetAddr address;
    salticidae::PeerId peer;
};

struct ReplicaStructuredEventOptions
{
    hotstuff::StructuredEventConfig config;
    std::string output_path;
};

template<typename Value>
Value parse_adaptive_v2_unsigned(
    const std::string &raw_value,
    const char *name,
    bool must_be_positive)
{
    if (raw_value.empty())
        throw HotStuffError(
            std::string("adaptive-v2 ") + name + " is required");

    Value value{0};
    const auto *begin = raw_value.data();
    const auto *end = begin + raw_value.size();
    const auto parsed = std::from_chars(begin, end, value, 10);
    if (parsed.ec != std::errc{} || parsed.ptr != end)
        throw HotStuffError(
            std::string("adaptive-v2 ") + name +
            " must be a canonical unsigned decimal in range");
    if (must_be_positive && value == 0)
        throw HotStuffError(
            std::string("adaptive-v2 ") + name + " must be positive");
    return value;
}

std::optional<hotstuff::ExperimentByzantineOptions>
parse_experiment_byzantine_options(
    const std::string &protocol_mode,
    ReplicaID local_replica,
    std::size_t replica_count,
    const std::string &raw_configuration,
    const std::string &raw_additional_omission_configuration,
    const std::string &diagnostic_window,
    const std::string &raw_false_report_target,
    bool omit_outbound_aggregate,
    bool omit_outbound_direct_vote,
    int context_limit)
{
    const bool requested =
        !raw_configuration.empty() ||
        !raw_additional_omission_configuration.empty() ||
        !diagnostic_window.empty() ||
        !raw_false_report_target.empty() ||
        omit_outbound_aggregate ||
        omit_outbound_direct_vote ||
        context_limit != 0;
    if (!requested)
        return std::nullopt;
    if (protocol_mode != "adaptive_v2")
        throw HotStuffError(
            "experiment Byzantine faults require adaptive-v2");
    if (raw_configuration.empty() || diagnostic_window.empty())
        throw HotStuffError(
            "experiment Byzantine configuration and window are required");
    if (diagnostic_window.size() > 128 ||
        !std::all_of(
            diagnostic_window.begin(),
            diagnostic_window.end(),
            [](unsigned char character)
            {
                return std::isalnum(character) != 0 ||
                       character == '-' || character == '_' ||
                       character == '.';
            }))
        throw HotStuffError(
            "experiment Byzantine window must be a safe identifier");
    if (context_limit <= 0)
        throw HotStuffError(
            "experiment Byzantine context limit must be positive");
    const auto fault_mode_count =
        static_cast<unsigned>(!raw_false_report_target.empty()) +
        static_cast<unsigned>(omit_outbound_aggregate) +
        static_cast<unsigned>(omit_outbound_direct_vote);
    if (fault_mode_count != 1)
        throw HotStuffError(
            "select exactly one experiment Byzantine fault mode");
    if (omit_outbound_direct_vote &&
        !raw_additional_omission_configuration.empty())
        throw HotStuffError(
            "direct-vote omission does not accept an additional "
            "configuration");
    if (!raw_additional_omission_configuration.empty() &&
        !omit_outbound_aggregate)
        throw HotStuffError(
            "additional experiment omission configuration requires "
            "aggregate omission");
    const auto parse_configuration =
        [](const std::string &raw_value,
           const std::string &name) -> hotstuff::ConfigurationId
        {
            const auto parts = trim_all(split(raw_value, ":"));
            if (parts.size() != 3)
                throw HotStuffError(
                    name + " must use epoch:tree:digest");
            const auto epoch_name = name + " epoch";
            const auto tree_name = name + " tree";
            const auto epoch =
                parse_adaptive_v2_unsigned<std::uint32_t>(
                    parts[0], epoch_name.c_str(), false);
            const auto tree =
                parse_adaptive_v2_unsigned<std::uint32_t>(
                    parts[1], tree_name.c_str(), false);
            if (parts[2].size() != 64 ||
                !std::all_of(
                    parts[2].begin(),
                    parts[2].end(),
                    [](unsigned char character)
                    {
                        return std::isxdigit(character) != 0;
                    }))
                throw HotStuffError(name + " digest is invalid");
            return hotstuff::ConfigurationId{
                epoch,
                tree,
                uint256_t(hotstuff::from_hex(parts[2]))};
        };

    hotstuff::ExperimentByzantineOptions options;
    options.enabled = true;
    options.configuration = parse_configuration(
        raw_configuration,
        "experiment Byzantine configuration");
    if (!raw_additional_omission_configuration.empty())
    {
        const auto additional = parse_configuration(
            raw_additional_omission_configuration,
            "additional experiment omission configuration");
        if (additional.epoch_number !=
                options.configuration.epoch_number ||
            additional.epoch_digest !=
                options.configuration.epoch_digest)
            throw HotStuffError(
                "additional experiment omission configuration must "
                "share the primary epoch and digest");
        if (additional.tree_id == options.configuration.tree_id)
            throw HotStuffError(
                "additional experiment omission configuration must "
                "use a distinct tree");
        options.additional_omission_configuration = additional;
    }
    options.diagnostic_window = diagnostic_window;
    if (!raw_false_report_target.empty())
    {
        const auto target =
            parse_adaptive_v2_unsigned<ReplicaID>(
                raw_false_report_target,
                "experiment false-report target",
                false);
        if (target >= replica_count || target == local_replica)
            throw HotStuffError(
                "experiment false-report target is invalid");
        options.false_report_target = target;
        options.maximum_false_report_contexts =
            static_cast<std::size_t>(context_limit);
    }
    else if (omit_outbound_aggregate)
    {
        options.omit_outbound_aggregate = true;
        options.maximum_omission_contexts =
            static_cast<std::size_t>(context_limit);
    }
    else
    {
        options.omit_outbound_direct_vote = true;
        options.maximum_direct_vote_omission_contexts =
            static_cast<std::size_t>(context_limit);
    }
    return options;
}

hotstuff::PubKeySecp256k1 parse_adaptive_v2_issuer_public_key(
    const std::string &issuer_public_key_hex)
{
    if (issuer_public_key_hex.empty())
        throw HotStuffError(
            "adaptive-v2 epoch-change issuer public key is required");
    if (issuer_public_key_hex.size() != 66 ||
        !std::all_of(
            issuer_public_key_hex.begin(),
            issuer_public_key_hex.end(),
            [](unsigned char character)
            {
                return std::isxdigit(character) != 0;
            }))
        throw HotStuffError(
            "adaptive-v2 epoch-change issuer public key is invalid");
    try
    {
        return hotstuff::PubKeySecp256k1(
            hotstuff::from_hex(issuer_public_key_hex));
    }
    catch (const std::exception &)
    {
        throw HotStuffError(
            "adaptive-v2 epoch-change issuer public key is invalid");
    }
}

std::optional<AdaptiveV2PreVoteConfig>
parse_adaptive_v2_pre_vote_config(
    const std::string &protocol_mode,
    const std::string &issuer_id,
    const std::string &issuer_public_key,
    const std::string &minimum_activation_delay,
    const std::string &maximum_activation_delay,
    const std::string &maximum_block_extra_bytes,
    const std::string &maximum_ancestry_blocks)
{
    if (protocol_mode != "adaptive_v2")
        return std::nullopt;

    const auto minimum_delay = parse_adaptive_v2_unsigned<std::uint64_t>(
        minimum_activation_delay,
        "minimum activation delay",
        true);
    const auto maximum_delay = parse_adaptive_v2_unsigned<std::uint64_t>(
        maximum_activation_delay,
        "maximum activation delay",
        true);
    if (maximum_delay < minimum_delay)
        throw HotStuffError(
            "adaptive-v2 activation delay bounds must be ordered");

    return AdaptiveV2PreVoteConfig{
        hotstuff::EpochChangeIssuer{
            parse_adaptive_v2_unsigned<hotstuff::EpochChangeIssuerId>(
                issuer_id,
                "epoch-change issuer ID",
                false),
            parse_adaptive_v2_issuer_public_key(issuer_public_key)},
        hotstuff::EpochChangeDelayBounds{
            minimum_delay,
            maximum_delay},
        parse_adaptive_v2_unsigned<std::size_t>(
            maximum_block_extra_bytes,
            "maximum block-extra bytes",
            true),
        parse_adaptive_v2_unsigned<std::size_t>(
            maximum_ancestry_blocks,
            "maximum ancestry blocks",
            true)};
}

std::optional<AdaptiveV2ManagerPin> parse_adaptive_v2_manager_pin(
    const std::string &protocol_mode,
    const std::string &manager_address,
    const std::string &manager_tls_certificate_hex)
{
    if (protocol_mode != "adaptive_v2")
        return std::nullopt;
    if (manager_address.empty())
        throw HotStuffError(
            "adaptive-v2 epoch manager address is required");
    if (manager_tls_certificate_hex.empty() ||
        manager_tls_certificate_hex.size() % 2 != 0 ||
        !std::all_of(
            manager_tls_certificate_hex.begin(),
            manager_tls_certificate_hex.end(),
            [](unsigned char character) {
                return std::isxdigit(character) != 0;
            }))
    {
        throw HotStuffError(
            "adaptive-v2 epoch manager TLS certificate is invalid");
    }

    try
    {
        NetAddr address(manager_address);
        if (address.is_null())
            throw HotStuffError(
                "adaptive-v2 epoch manager address is invalid");
        const auto manager_certificate = salticidae::X509::create_from_der(
            hotstuff::from_hex(manager_tls_certificate_hex));
        const salticidae::PeerId manager_peer(manager_certificate);
        if (manager_peer.is_null())
            throw HotStuffError(
                "adaptive-v2 epoch manager TLS certificate is invalid");
        return AdaptiveV2ManagerPin{address, manager_peer};
    }
    catch (const HotStuffError &)
    {
        throw;
    }
    catch (const std::exception &)
    {
        throw HotStuffError(
            "adaptive-v2 epoch manager address or TLS certificate is invalid");
    }
}

std::optional<ReplicaStructuredEventOptions>
parse_replica_structured_event_options(
    const std::string &protocol_mode,
    ReplicaID replica_id,
    const std::string &run_id,
    const std::string &source_instance,
    const std::string &output_path,
    const std::string &commit_observer_id,
    const std::string &commit_observer_instance)
{
    if (protocol_mode != "adaptive_v2")
        return std::nullopt;

    const auto require_value = [](const std::string &value,
                                  const char *name)
    {
        if (value.empty())
            throw HotStuffError(
                std::string("adaptive-v2 structured-event ") + name +
                " is required");
    };
    require_value(run_id, "run ID");
    require_value(source_instance, "source instance");
    require_value(output_path, "output path");
    require_value(commit_observer_id, "commit observer ID");
    require_value(
        commit_observer_instance, "commit observer instance");

    const hotstuff::StructuredEventSource source{
        hotstuff::StructuredEventSourceKind::replica,
        "replica-" + std::to_string(replica_id),
        source_instance};
    hotstuff::StructuredEventConfig structured_event_config{
        run_id,
        source,
        std::nullopt,
        hotstuff::StructuredEventLimits{}};
    structured_event_config.designated_commit_observer =
        hotstuff::StructuredEventSource{
            hotstuff::StructuredEventSourceKind::replica,
            commit_observer_id,
            commit_observer_instance};
    return ReplicaStructuredEventOptions{
        std::move(structured_event_config),
        output_path};
}

salticidae::BoxObj<HotStuffApp> papp = nullptr;

int main(int argc, char **argv)
{
    std::signal(SIGPIPE, SIG_IGN);
    Config config("hotstuff.gen.conf");

    ElapsedTime elapsed;
    elapsed.start();

    auto opt_blk_size = Config::OptValInt::create(1);
    auto opt_client_ip = Config::OptValStr::create();
    auto opt_parent_limit = Config::OptValInt::create(-1);
    auto opt_stat_period = Config::OptValDouble::create(15);
    auto opt_replicas = Config::OptValStrVec::create();
    auto opt_idx = Config::OptValInt::create(0);
    auto opt_client_port = Config::OptValInt::create(-1);
    auto opt_privkey = Config::OptValStr::create();
    auto opt_tls_privkey = Config::OptValStr::create();
    auto opt_tls_cert = Config::OptValStr::create();
    auto opt_help = Config::OptValFlag::create(false);
    auto opt_pace_maker = Config::OptValStr::create("dummy");
    auto opt_fixed_proposer = Config::OptValInt::create(1);
    auto opt_base_timeout = Config::OptValDouble::create(10);
    auto opt_prop_delay = Config::OptValDouble::create(1);
    auto opt_imp_timeout = Config::OptValDouble::create(10);
    auto opt_aggregation_timeout = Config::OptValDouble::create(0.5);
    auto opt_leader_progress_timeout = Config::OptValDouble::create(20);
    auto opt_leader_activation_grace = Config::OptValDouble::create(5);
    auto opt_nworker = Config::OptValInt::create(2);
    auto opt_repnworker = Config::OptValInt::create(2);
    auto opt_repburst = Config::OptValInt::create(10000);
    auto opt_clinworker = Config::OptValInt::create(1);
    auto opt_cliburst = Config::OptValInt::create(10000);
    auto opt_notls = Config::OptValFlag::create(false);

    auto opt_max_rep_msg = Config::OptValInt::create(4 << 20); // 4m by default
    auto opt_max_cli_msg = Config::OptValInt::create(65536);   // 64k by default
    auto opt_fanout = Config::OptValInt::create(2);            // 2 by default
    auto opt_piped_latency = Config::OptValInt::create(10);    // 10ms by default
    auto opt_async_blocks = Config::OptValInt::create(0);      // 0 by default

    auto opt_tree_switch_period = Config::OptValDouble::create(100); // Period of each tree
    auto opt_tree_generation = Config::OptValStr::create("file");
    auto opt_tree_generation_fpath = Config::OptValStr::create("treegen.conf");

    auto opt_new_epoch = Config::OptValStr::create("newepoch.conf");
    auto opt_epoch_protocol_mode =
        Config::OptValStr::create("legacy_static");
    auto opt_adaptive_epoch_file = Config::OptValStr::create("");
    auto opt_adaptive_activation_height = Config::OptValInt::create(20);
    auto opt_epoch_change_issuer_id = Config::OptValStr::create("");
    auto opt_epoch_change_issuer_public_key =
        Config::OptValStr::create("");
    auto opt_epoch_change_minimum_activation_delay =
        Config::OptValStr::create("");
    auto opt_epoch_change_maximum_activation_delay =
        Config::OptValStr::create("");
    auto opt_epoch_change_maximum_block_extra_bytes =
        Config::OptValStr::create("");
    auto opt_epoch_change_maximum_ancestry_blocks =
        Config::OptValStr::create("");
    auto opt_epoch_manager_address = Config::OptValStr::create("");
    auto opt_epoch_manager_tls_cert = Config::OptValStr::create("");
    auto opt_structured_event_run_id = Config::OptValStr::create("");
    auto opt_structured_event_source_instance =
        Config::OptValStr::create("");
    auto opt_structured_event_output = Config::OptValStr::create("");
    auto opt_structured_event_commit_observer_id =
        Config::OptValStr::create("");
    auto opt_structured_event_commit_observer_instance =
        Config::OptValStr::create("");
    auto opt_experiment_byzantine_configuration =
        Config::OptValStr::create("");
    auto opt_experiment_omission_additional_configuration =
        Config::OptValStr::create("");
    auto opt_experiment_byzantine_window =
        Config::OptValStr::create("");
    auto opt_experiment_false_report_target =
        Config::OptValStr::create("");
    auto opt_experiment_omit_outbound_aggregate =
        Config::OptValFlag::create(false);
    auto opt_experiment_omit_outbound_direct_vote =
        Config::OptValFlag::create(false);
    auto opt_experiment_byzantine_context_limit =
        Config::OptValInt::create(0);

    config.add_opt("block-size", opt_blk_size, Config::SET_VAL);
    config.add_opt("client-ip", opt_client_ip, Config::SET_VAL);
    config.add_opt("parent-limit", opt_parent_limit, Config::SET_VAL);
    config.add_opt("stat-period", opt_stat_period, Config::SET_VAL);
    config.add_opt("replica", opt_replicas, Config::APPEND, 'a', "add an replica to the list");
    config.add_opt("idx", opt_idx, Config::SET_VAL, 'i', "specify the index in the replica list");
    config.add_opt("cport", opt_client_port, Config::SET_VAL, 'c', "specify the port listening for clients");
    config.add_opt("privkey", opt_privkey, Config::SET_VAL);
    config.add_opt("tls-privkey", opt_tls_privkey, Config::SET_VAL);
    config.add_opt("tls-cert", opt_tls_cert, Config::SET_VAL);
    config.add_opt("pace-maker", opt_pace_maker, Config::SET_VAL, 'p', "specify pace maker (dummy, rr)");
    config.add_opt("proposer", opt_fixed_proposer, Config::SET_VAL, 'l', "set the fixed proposer (for dummy)");
    config.add_opt("base-timeout", opt_base_timeout, Config::SET_VAL, 't', "set the initial timeout for the Round-Robin Pacemaker");
    config.add_opt("prop-delay", opt_prop_delay, Config::SET_VAL, 't', "set the delay that follows the timeout for the Round-Robin Pacemaker");
    config.add_opt("imp-timeout", opt_imp_timeout, Config::SET_VAL, 'u', "set impeachment timeout (for sticky)");
    config.add_opt("aggregation-timeout", opt_aggregation_timeout, Config::SET_VAL, -1, "per-level aggregation timeout in seconds");
    config.add_opt("leader-progress-timeout", opt_leader_progress_timeout, Config::SET_VAL, -1, "leader progress timeout in seconds");
    config.add_opt("leader-activation-grace", opt_leader_activation_grace, Config::SET_VAL, -1, "new leader activation grace in seconds");
    config.add_opt("nworker", opt_nworker, Config::SET_VAL, 'n', "the number of threads for verification");
    config.add_opt("repnworker", opt_repnworker, Config::SET_VAL, 'm', "the number of threads for replica network");
    config.add_opt("repburst", opt_repburst, Config::SET_VAL, 'b', "");
    config.add_opt("clinworker", opt_clinworker, Config::SET_VAL, 'M', "the number of threads for client network");
    config.add_opt("cliburst", opt_cliburst, Config::SET_VAL, 'B', "");
    config.add_opt("notls", opt_notls, Config::SWITCH_ON, 's', "disable TLS");
    config.add_opt("max-rep-msg", opt_max_rep_msg, Config::SET_VAL, 'S', "the maximum replica message size");
    config.add_opt("max-cli-msg", opt_max_cli_msg, Config::SET_VAL, 'S', "the maximum client message size");
    config.add_opt("help", opt_help, Config::SWITCH_ON, 'h', "show this help info");
    config.add_opt("fan-out", opt_fanout, Config::SET_VAL, 'F', "fanout");
    config.add_opt("piped_latency", opt_piped_latency, Config::SET_VAL, 'P', "Latency between the block pipelining");
    config.add_opt("async_blocks", opt_async_blocks, Config::SET_VAL, 'A', "Async blocks to pipeline");

    config.add_opt("tree-switch-period", opt_tree_switch_period, Config::SET_VAL, 'T', "Period (in blocks) for switching the system's tree");
    config.add_opt("tree-generation", opt_tree_generation, Config::SET_VAL, 'G', "Tree generation algorithm (default, file)");
    config.add_opt("tree-generation-fpath", opt_tree_generation_fpath, Config::SET_VAL, 'g', "File path for the tree generation when file is selected");

    config.add_opt("new-epoch", opt_new_epoch, Config::SET_VAL, 'e', "File with new epoch configuration");
    config.add_opt(
        "epoch-protocol-mode",
        opt_epoch_protocol_mode,
        Config::SET_VAL,
        -1,
        "epoch protocol mode (legacy_static, adaptive_v1, adaptive_v2)");
    config.add_opt(
        "adaptive-epoch-file",
        opt_adaptive_epoch_file,
        Config::SET_VAL,
        -1,
        "trusted-local successor epoch tree file");
    config.add_opt(
        "adaptive-activation-height",
        opt_adaptive_activation_height,
        Config::SET_VAL,
        -1,
        "committed height for trusted-local epoch activation");
    config.add_opt(
        "epoch-change-issuer-id",
        opt_epoch_change_issuer_id,
        Config::SET_VAL,
        -1,
        "authorized adaptive-v2 epoch-change issuer ID");
    config.add_opt(
        "epoch-change-issuer-public-key",
        opt_epoch_change_issuer_public_key,
        Config::SET_VAL,
        -1,
        "authorized adaptive-v2 epoch-change issuer public key");
    config.add_opt(
        "epoch-change-minimum-activation-delay",
        opt_epoch_change_minimum_activation_delay,
        Config::SET_VAL,
        -1,
        "minimum adaptive-v2 activation delay in committed blocks");
    config.add_opt(
        "epoch-change-maximum-activation-delay",
        opt_epoch_change_maximum_activation_delay,
        Config::SET_VAL,
        -1,
        "maximum adaptive-v2 activation delay in committed blocks");
    config.add_opt(
        "epoch-change-maximum-block-extra-bytes",
        opt_epoch_change_maximum_block_extra_bytes,
        Config::SET_VAL,
        -1,
        "maximum adaptive-v2 epoch-change block-extra bytes");
    config.add_opt(
        "epoch-change-maximum-ancestry-blocks",
        opt_epoch_change_maximum_ancestry_blocks,
        Config::SET_VAL,
        -1,
        "maximum adaptive-v2 proposal ancestry blocks");
    config.add_opt(
        "epoch-manager-address",
        opt_epoch_manager_address,
        Config::SET_VAL,
        -1,
        "pinned adaptive-v2 manager replica-network address");
    config.add_opt(
        "epoch-manager-tls-cert",
        opt_epoch_manager_tls_cert,
        Config::SET_VAL,
        -1,
        "pinned adaptive-v2 manager TLS certificate DER in hex");
    config.add_opt(
        "structured-event-run-id",
        opt_structured_event_run_id,
        Config::SET_VAL,
        -1,
        "exact run identity for adaptive-v2 structured events");
    config.add_opt(
        "structured-event-source-instance",
        opt_structured_event_source_instance,
        Config::SET_VAL,
        -1,
        "unique process instance identity for structured events");
    config.add_opt(
        "structured-event-output",
        opt_structured_event_output,
        Config::SET_VAL,
        -1,
        "exclusive structured-event JSONL output path");
    config.add_opt(
        "structured-event-commit-observer-id",
        opt_structured_event_commit_observer_id,
        Config::SET_VAL,
        -1,
        "exact logical ID of the designated commit observer");
    config.add_opt(
        "structured-event-commit-observer-instance",
        opt_structured_event_commit_observer_instance,
        Config::SET_VAL,
        -1,
        "exact instance ID of the designated commit observer");
    config.add_opt(
        "experiment-byzantine-configuration",
        opt_experiment_byzantine_configuration,
        Config::SET_VAL,
        -1,
        "exact epoch:tree:digest for experiment-only Byzantine faults");
    config.add_opt(
        "experiment-omission-additional-configuration",
        opt_experiment_omission_additional_configuration,
        Config::SET_VAL,
        -1,
        "optional second exact epoch:tree:digest for aggregate omission");
    config.add_opt(
        "experiment-byzantine-window",
        opt_experiment_byzantine_window,
        Config::SET_VAL,
        -1,
        "frozen experiment-only diagnostic window identity");
    config.add_opt(
        "experiment-false-report-target",
        opt_experiment_false_report_target,
        Config::SET_VAL,
        -1,
        "target for a local authenticated false timeout report");
    config.add_opt(
        "experiment-omit-outbound-aggregate",
        opt_experiment_omit_outbound_aggregate,
        Config::SWITCH_ON,
        -1,
        "omit one timeout-flushed aggregate per exact proposal");
    config.add_opt(
        "experiment-omit-outbound-direct-vote",
        opt_experiment_omit_outbound_direct_vote,
        Config::SWITCH_ON,
        -1,
        "omit one leaf's outbound direct vote per exact proposal");
    config.add_opt(
        "experiment-byzantine-context-limit",
        opt_experiment_byzantine_context_limit,
        Config::SET_VAL,
        -1,
        "maximum exact proposal contexts affected by the fault");

    EventContext ec;
    config.parse(argc, argv);
    if (opt_help->get())
    {
        config.print_help();
        exit(0);
    }
    const auto adaptive_v2_pre_vote_config =
        parse_adaptive_v2_pre_vote_config(
            opt_epoch_protocol_mode->get(),
            opt_epoch_change_issuer_id->get(),
            opt_epoch_change_issuer_public_key->get(),
            opt_epoch_change_minimum_activation_delay->get(),
            opt_epoch_change_maximum_activation_delay->get(),
            opt_epoch_change_maximum_block_extra_bytes->get(),
            opt_epoch_change_maximum_ancestry_blocks->get());
    const auto replica_structured_event_options =
        parse_replica_structured_event_options(
            opt_epoch_protocol_mode->get(),
            static_cast<ReplicaID>(opt_idx->get()),
            opt_structured_event_run_id->get(),
            opt_structured_event_source_instance->get(),
            opt_structured_event_output->get(),
            opt_structured_event_commit_observer_id->get(),
            opt_structured_event_commit_observer_instance->get());
    const auto tree_switch_period = opt_tree_switch_period->get();
    if (opt_epoch_protocol_mode->get() == "adaptive_v2" &&
        (!std::isfinite(tree_switch_period) ||
         tree_switch_period < 1.0 ||
         std::trunc(tree_switch_period) != tree_switch_period ||
         tree_switch_period >= std::ldexp(
             1.0,
             std::numeric_limits<std::size_t>::digits)))
        throw HotStuffError(
            "adaptive-v2 tree switch period must be a finite positive integer within size_t range");
    auto idx = opt_idx->get();
    auto client_port = opt_client_port->get();
    std::vector<std::tuple<std::string, std::string, std::string>> replicas;
    for (const auto &s : opt_replicas->get())
    {
        auto res = trim_all(split(s, ","));
        if (res.size() != 3)
            throw HotStuffError("invalid replica info");
        replicas.push_back(std::make_tuple(res[0], res[1], res[2]));
    }

    if (!(0 <= idx && (size_t)idx < replicas.size()))
        throw HotStuffError("replica idx out of range");

    EpochProtocolMode epoch_protocol_mode;
    if (opt_epoch_protocol_mode->get() == "legacy_static")
        epoch_protocol_mode = EpochProtocolMode::legacy_static;
    else if (opt_epoch_protocol_mode->get() == "adaptive_v1")
        epoch_protocol_mode = EpochProtocolMode::adaptive_v1;
    else if (opt_epoch_protocol_mode->get() == "adaptive_v2")
        epoch_protocol_mode = EpochProtocolMode::adaptive_v2;
    else
        throw HotStuffError("invalid epoch protocol mode");
    const auto experiment_byzantine_options =
        parse_experiment_byzantine_options(
            opt_epoch_protocol_mode->get(),
            static_cast<ReplicaID>(idx),
            replicas.size(),
            opt_experiment_byzantine_configuration->get(),
            opt_experiment_omission_additional_configuration->get(),
            opt_experiment_byzantine_window->get(),
            opt_experiment_false_report_target->get(),
            opt_experiment_omit_outbound_aggregate->get(),
            opt_experiment_omit_outbound_direct_vote->get(),
            opt_experiment_byzantine_context_limit->get());
    const auto adaptive_v2_manager_pin = parse_adaptive_v2_manager_pin(
        opt_epoch_protocol_mode->get(),
        opt_epoch_manager_address->get(),
        opt_epoch_manager_tls_cert->get());
    if (epoch_protocol_mode == EpochProtocolMode::adaptive_v2 &&
        (opt_notls->get() || opt_tls_privkey->get().empty() ||
         opt_tls_cert->get().empty()))
    {
        throw HotStuffError(
            "adaptive-v2 replica networking requires TLS credentials");
    }
    if (epoch_protocol_mode == EpochProtocolMode::adaptive_v2 &&
        opt_max_rep_msg->get() <= 0)
        throw HotStuffError(
            "adaptive-v2 maximum replica message size must be positive");
    if (opt_adaptive_activation_height->get() < 0)
        throw HotStuffError("adaptive activation height must be non-negative");
    std::string binding_addr = std::get<0>(replicas[idx]);
    if (client_port == -1)
    {
        auto p = split_ip_port_cport(binding_addr);
        size_t idx;
        try
        {
            client_port = stoi(p.second, &idx);
        }
        catch (std::invalid_argument &)
        {
            throw HotStuffError("client port not specified");
        }
    }

    NetAddr plisten_addr{split_ip_port_cport(binding_addr).first};

    auto parent_limit = opt_parent_limit->get();
    hotstuff::pacemaker_bt pmaker;
    if (opt_pace_maker->get() == "dummy")
    {
        HOTSTUFF_LOG_PROTO("Starting Pacemaker as a dummy!");
        pmaker = new hotstuff::PaceMakerMultitree(
            ec,
            parent_limit,
            opt_base_timeout->get(),
            opt_prop_delay->get(),
            opt_leader_progress_timeout->get(),
            opt_leader_activation_grace->get());
    }
    else
    {
        HOTSTUFF_LOG_PROTO("Starting Pacemaker as a Roundrobin!");
        pmaker = new hotstuff::PaceMakerRR(ec, parent_limit, opt_base_timeout->get(), opt_prop_delay->get());
    }

    HotStuffApp::Net::Config repnet_config;
    ClientNetwork<opcode_t>::Config clinet_config;
    repnet_config.max_msg_size(opt_max_rep_msg->get());
    repnet_config.nworker(opt_repnworker->get());
    clinet_config.max_msg_size(opt_max_cli_msg->get());
    if (!opt_tls_privkey->get().empty() && !opt_notls->get())
    {
        auto tls_priv_key = new salticidae::PKey(
            salticidae::PKey::create_privkey_from_der(
                hotstuff::from_hex(opt_tls_privkey->get())));
        auto tls_cert = new salticidae::X509(
            salticidae::X509::create_from_der(
                hotstuff::from_hex(opt_tls_cert->get())));
        repnet_config
            .enable_tls(true)
            .tls_key(tls_priv_key)
            .tls_cert(tls_cert);
    }
    clinet_config
        .burst_size(opt_cliburst->get())
        .nworker(opt_clinworker->get());
    papp = new HotStuffApp(opt_blk_size->get(),
                           opt_stat_period->get(),
                           idx,
                           hotstuff::from_hex(opt_privkey->get()),
                           plisten_addr,
                           NetAddr("0.0.0.0", client_port),
                           std::move(pmaker),
                           ec,
                           opt_nworker->get(),
                           repnet_config,
                           clinet_config,
                           NetAddr(opt_client_ip->get(), 50500),
                           epoch_protocol_mode);

    std::optional<hotstuff::MonotonicRawStructuredEventClock>
        structured_event_clock;
    std::optional<hotstuff::ExclusiveFileStructuredEventOutput>
        structured_event_output;
    std::optional<hotstuff::StructuredEventSink> structured_event_sink;
    hotstuff::StructuredEventSink *structured_event_sink_ptr = nullptr;
    if (replica_structured_event_options.has_value())
    {
        structured_event_clock.emplace();
        structured_event_output.emplace(
            replica_structured_event_options->output_path);
        structured_event_sink.emplace(
            replica_structured_event_options->config,
            structured_event_clock.value(),
            structured_event_output.value());
        structured_event_sink_ptr = &structured_event_sink.value();
        if (!structured_event_sink_ptr->health().healthy)
            throw HotStuffError(
                "adaptive-v2 structured-event sink is unhealthy");
        papp->bind_structured_event_emitters(
            structured_event_sink_ptr,
            structured_event_sink_ptr,
            structured_event_sink_ptr);
        papp->bind_process_lifecycle_emitter(
            structured_event_sink_ptr);
        structured_event_sink_ptr->emit(
            hotstuff::StructuredEventPayload{
                hotstuff::ProcessLifecycleEvent{
                    hotstuff::ProcessLifecycleState::started,
                    std::nullopt}});
        structured_event_sink_ptr->drain();
        if (!structured_event_sink_ptr->health().healthy)
            throw HotStuffError(
                "adaptive-v2 structured-event startup record failed");
    }

    std::vector<std::tuple<NetAddr, bytearray_t, bytearray_t>> reps;
    for (auto &r : replicas)
    {
        auto p = split_ip_port_cport(std::get<0>(r));
        reps.push_back(std::make_tuple(
            NetAddr(p.first),
            hotstuff::from_hex(std::get<1>(r)),
            hotstuff::from_hex(std::get<2>(r))));
    }

    papp->set_fanout(opt_fanout->get());
    papp->set_piped_latency(opt_piped_latency->get(), opt_async_blocks->get());
    papp->set_tree_generation(opt_tree_generation->get(), opt_tree_generation_fpath->get());
    if (epoch_protocol_mode == EpochProtocolMode::adaptive_v2)
        papp->set_tree_period(
            static_cast<std::size_t>(tree_switch_period));
    else
        papp->set_tree_period(opt_tree_switch_period->get());
    papp->set_new_epoch(opt_new_epoch->get());
    papp->set_adaptive_bootstrap(
        opt_adaptive_epoch_file->get(),
        static_cast<std::uint64_t>(
            opt_adaptive_activation_height->get()));
    papp->set_aggregation_timeout(opt_aggregation_timeout->get());
    if (experiment_byzantine_options.has_value())
        papp->configure_experiment_byzantine_faults(
            *experiment_byzantine_options);
    if (epoch_protocol_mode == EpochProtocolMode::adaptive_v2)
    {
        if (!adaptive_v2_pre_vote_config.has_value())
            throw HotStuffError(
                "adaptive-v2 pre-vote configuration is unavailable");
        const auto &pre_vote_config = *adaptive_v2_pre_vote_config;
        papp->configure_epoch_change_pre_vote_gate(
            pre_vote_config.issuer,
            pre_vote_config.delay_bounds,
            pre_vote_config.maximum_block_extra_bytes,
            pre_vote_config.maximum_ancestry_blocks,
            static_cast<std::size_t>(opt_max_rep_msg->get()));
        if (!adaptive_v2_manager_pin.has_value())
            throw HotStuffError(
                "adaptive-v2 epoch manager pin is unavailable");
        papp->configure_epoch_manager(
            adaptive_v2_manager_pin->peer,
            adaptive_v2_manager_pin->address);
    }

    HOTSTUFF_LOG_INFO("*** thread info ***");
    HOTSTUFF_LOG_INFO("Verification workers = %lu", opt_nworker->get());
    HOTSTUFF_LOG_INFO("Replica workers = %lu", opt_repnworker->get());
    HOTSTUFF_LOG_INFO("Replica burst = %lu", opt_repburst->get());
    HOTSTUFF_LOG_INFO("*******************");

    bool structured_event_failed = false;
    TimerEvent structured_event_drain_timer;
    if (structured_event_sink_ptr != nullptr)
    {
        structured_event_drain_timer = TimerEvent(
            ec,
            [&](TimerEvent &timer)
            {
                structured_event_sink.value().drain();
                const auto health =
                    structured_event_sink.value().health();
                if (!health.healthy)
                {
                    structured_event_failed = true;
                    papp->stop();
                    return;
                }
                timer.add(0.05);
            });
        structured_event_drain_timer.add(0.05);
    }

    auto shutdown = [&](int)
    { papp->stop(); };
    salticidae::SigEvent ev_sigint(ec, shutdown);
    salticidae::SigEvent ev_sigterm(ec, shutdown);
    ev_sigint.add(SIGINT);
    ev_sigterm.add(SIGTERM);

    std::exception_ptr start_failure;
    try
    {
        papp->start(reps);
    }
    catch (...)
    {
        start_failure = std::current_exception();
    }
    structured_event_drain_timer.del();
    papp->stop();

    if (structured_event_sink_ptr != nullptr)
    {
        structured_event_sink_ptr->drain();
        if (!structured_event_sink_ptr->health().healthy)
            structured_event_failed = true;
        papp->bind_structured_event_emitters(
            nullptr, nullptr, nullptr);
        papp->bind_process_lifecycle_emitter(nullptr);
    }
    papp = salticidae::BoxObj<HotStuffApp>();

    if (structured_event_sink.has_value())
    {
        structured_event_sink.value().emit(
            hotstuff::StructuredEventPayload{
                hotstuff::ProcessLifecycleEvent{
                    hotstuff::ProcessLifecycleState::stopped,
                    std::nullopt}});
        structured_event_sink.value().drain();
        if (!structured_event_sink.value().health().healthy)
            structured_event_failed = true;
        structured_event_sink.value().shutdown();
        const auto health = structured_event_sink.value().health();
        if (!health.healthy)
            structured_event_failed = true;
    }

    elapsed.stop(true);
    if (structured_event_failed)
        return 1;
    if (start_failure != nullptr)
        std::rethrow_exception(start_failure);
    return 0;
}

HotStuffApp::HotStuffApp(uint32_t blk_size,
                         double stat_period,
                         ReplicaID idx,
                         const bytearray_t &raw_privkey,
                         NetAddr plisten_addr,
                         NetAddr clisten_addr,
                         hotstuff::pacemaker_bt pmaker,
                         const EventContext &ec,
                         size_t nworker,
                         const Net::Config &repnet_config,
                         const ClientNetwork<opcode_t>::Config &clinet_config,
                         NetAddr reputation_addr,
                         EpochProtocolMode protocol_mode) : HotStuff(blk_size, idx, raw_privkey, plisten_addr, std::move(pmaker), ec, nworker, repnet_config, reputation_addr, protocol_mode),
                                                    stat_period(stat_period),
                                                    ec(ec),
                                                    cn(req_ec, clinet_config),
                                                    clisten_addr(clisten_addr)
{
    /* prepare the thread used for sending back confirmations */
    resp_tcall = new salticidae::ThreadCall(resp_ec);
    req_tcall = new salticidae::ThreadCall(req_ec);
    resp_queue.reg_handler(resp_ec, [this](resp_queue_t &q)
                           {
        std::pair<Finality, NetAddr> p;
        while (q.try_dequeue(p))
        {
            try {
                cn.send_msg(MsgRespCmd(std::move(p.first)), p.second);
            } catch (std::exception &err) {
                //HOTSTUFF_LOG_WARN("unable to send to the client: %s", err.what());
            }
        }
        return false; });

    /* register the handlers for msg from clients */
    cn.reg_handler(salticidae::generic_bind(&HotStuffApp::epoch_handler, this, _1, _2));
}

void HotStuffApp::epoch_handler(MsgDeployEpochReputation &&msg, const conn_t &conn)
{
    const NetAddr addr = conn->get_addr();

    HOTSTUFF_LOG_INFO("RECEIVED NEW EPOCH FROM %zu", addr.ip);

    auto epoch_reputation = msg.get_epoch_reputation();

    HOTSTUFF_LOG_INFO("[EPOCH HANDLER] Received new epoch %s", std::string(epoch_reputation.epoch).c_str());

    stage_epoch(epoch_reputation);

    // auto cmd = parse_cmd(msg.serialized);
    // const auto &cmd_hash = cmd->get_hash();
    // HOTSTUFF_LOG_DEBUG("processing command %s", std::string(*cmd).c_str());
    // exec_command(cmd_hash, [this, addr](Finality fin)
    //              { resp_queue.enqueue(std::make_pair(fin, addr)); });
}

void HotStuffApp::start(
    const std::vector<
        std::tuple<NetAddr, bytearray_t, bytearray_t>> &reps)
{
    ev_stat_timer = TimerEvent(ec, [this](TimerEvent &)
                               {
        HotStuff::print_stat();
        HotStuffApp::print_stat();
        //HotStuffCore::prune(100);
        ev_stat_timer.add(stat_period); });
    ev_stat_timer.add(stat_period);
    HOTSTUFF_LOG_INFO("** starting the system with parameters **");
    HOTSTUFF_LOG_INFO("blk_size = %lu", blk_size);
    HOTSTUFF_LOG_INFO("conns = %lu", HotStuff::size());
    HOTSTUFF_LOG_INFO("** starting the event loop...");
    HotStuff::start(reps);
    if (!adaptive_epoch_file.empty() &&
        !bootstrap_adaptive_epoch_from_file(
            adaptive_epoch_file, adaptive_activation_height))
    {
        HOTSTUFF_LOG_WARN(
            "KAURI_DEMO fatal replica=%u reason=bootstrap_failed",
            get_id());
        throw HotStuffError(
            "failed to stage and arm trusted-local adaptive epoch");
    }
    cn.reg_conn_handler([this](const salticidae::ConnPool::conn_t &_conn, bool connected)
                        {
        auto conn = salticidae::static_pointer_cast<conn_t::type>(_conn);
        if (connected)
            client_conns.insert(conn);
        else
            client_conns.erase(conn);
        return true; });
    cn.start();
    cn.listen(clisten_addr);
    if (process_lifecycle_emitter_ != nullptr)
    {
        process_lifecycle_emitter_->emit(
            hotstuff::StructuredEventPayload{
                hotstuff::ProcessLifecycleEvent{
                    hotstuff::ProcessLifecycleState::ready,
                    std::nullopt}});
    }
    req_thread = std::thread([this]()
                             { req_ec.dispatch(); });
    resp_thread = std::thread([this]()
                              { resp_ec.dispatch(); });
    /* enter the event main loop */
    ec.dispatch();
}

void HotStuffApp::bind_process_lifecycle_emitter(
    hotstuff::StructuredEventEmitter *emitter) noexcept
{
    process_lifecycle_emitter_ = emitter;
}

void HotStuffApp::stop()
{
    if (!process_stopping_emitted_ &&
        process_lifecycle_emitter_ != nullptr)
    {
        process_lifecycle_emitter_->emit(
            hotstuff::StructuredEventPayload{
                hotstuff::ProcessLifecycleEvent{
                    hotstuff::ProcessLifecycleState::stopping,
                    std::nullopt}});
        process_stopping_emitted_ = true;
    }
    if (req_thread.joinable())
    {
        req_tcall->async_call([this](salticidae::ThreadCall::Handle &)
                              { req_ec.stop(); });
        req_thread.join();
    }
    if (resp_thread.joinable())
    {
        resp_tcall->async_call([this](salticidae::ThreadCall::Handle &)
                               { resp_ec.stop(); });
        resp_thread.join();
    }
    ec.stop();
}

void HotStuffApp::print_stat() const
{
#ifdef HOTSTUFF_MSG_STAT
    HOTSTUFF_LOG_INFO("--- client msg. (10s) ---");
    size_t _nsent = 0;
    size_t _nrecv = 0;
    for (const auto &conn : client_conns)
    {
        if (conn == nullptr)
            continue;
        size_t ns = conn->get_nsent();
        size_t nr = conn->get_nrecv();
        size_t nsb = conn->get_nsentb();
        size_t nrb = conn->get_nrecvb();
        conn->clear_msgstat();
        HOTSTUFF_LOG_INFO("%s: %u(%u), %u(%u)",
                          std::string(conn->get_addr()).c_str(), ns, nsb, nr, nrb);
        _nsent += ns;
        _nrecv += nr;
    }
    HOTSTUFF_LOG_INFO("--- end client msg. ---");
#endif
}

void HotStuffApp::set_fanout(int32_t fanout)
{
    HotStuff::set_fanout(fanout);
}

void HotStuffApp::set_piped_latency(int32_t piped_latency, int32_t async_blocks)
{
    HotStuff::set_piped_latency(piped_latency, async_blocks);
}

void HotStuffApp::set_tree_period(size_t nblocks)
{
    HotStuff::set_tree_period(nblocks);
}

void HotStuffApp::set_tree_generation(std::string genAlgo, std::string fpath)
{
    HotStuff::set_tree_generation(genAlgo, fpath);
}

void HotStuffApp::set_new_epoch(std::string new_epoch)
{
    HotStuff::set_new_epoch(new_epoch);
}

void HotStuffApp::set_adaptive_bootstrap(
    std::string epoch_file,
    std::uint64_t activation_height)
{
    adaptive_epoch_file = std::move(epoch_file);
    adaptive_activation_height = activation_height;
}
