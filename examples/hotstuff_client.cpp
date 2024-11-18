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

#include <cassert>
#include <random>
#include <queue>
#include <signal.h>
#include <sys/time.h>

#include <fstream>
#include <sstream>
#include <arpa/inet.h> // Include for inet_ntoa

#include "salticidae/type.h"
#include "salticidae/netaddr.h"
#include "salticidae/network.h"
#include "salticidae/util.h"

#include "hotstuff/util.h"
#include "hotstuff/type.h"
#include "hotstuff/client.h"
#include "hotstuff/hotstuff.h"

using salticidae::Config;

using hotstuff::command_t;
using hotstuff::CommandDummy;
using hotstuff::Epoch;
using hotstuff::EventContext;
using hotstuff::HotStuffError;
using hotstuff::MsgDeployEpoch;
using hotstuff::MsgReqCmd;
using hotstuff::MsgRespCmd;
using hotstuff::NetAddr;
using hotstuff::opcode_t;
using hotstuff::ReplicaID;
using hotstuff::Tree;
using hotstuff::uint256_t;

EventContext ec;
ReplicaID proposer;
size_t max_async_num;
int max_iter_num;
uint32_t cid;
uint32_t cnt = 0;
uint32_t nfaulty;
Epoch new_epoch;

struct Request
{
    command_t cmd;
    size_t confirmed;
    salticidae::ElapsedTime et;
    Request(const command_t &cmd) : cmd(cmd), confirmed(0) { et.start(); }
};

using Net = salticidae::MsgNetwork<opcode_t>;

std::unordered_map<ReplicaID, Net::conn_t> conns;
size_t waiting_receival = 0;
std::unordered_map<const uint256_t, Request> waiting_finalized;
std::unordered_map<const uint256_t, Request> waiting;
std::vector<NetAddr> replicas;
std::vector<std::pair<struct timeval, double>> elapsed;
Net mn(ec, Net::Config());

// Helper function to convert NetAddr to string
std::string netaddr_to_string(const salticidae::NetAddr &addr)
{
    struct in_addr ip_addr;
    ip_addr.s_addr = addr.ip; // Set the IP address
    std::stringstream ss;

    // Convert IP to string and append port
    ss << inet_ntoa(ip_addr) << ":" << ntohs(addr.port); // Convert port from network byte order
    return ss.str();
}

bool try_send(bool check = true)
{
    if ((!check || waiting.size() < max_async_num) && max_iter_num)
    {

        HOTSTUFF_LOG_INFO("Attempting to send MsgDeployEpoch with epoch number: %u", new_epoch.get_epoch_num());

        // auto cmd = new CommandDummy(cid, cnt++);
        MsgDeployEpoch msg(new_epoch);
        for (auto &p : conns)
        {
            std::string addr_str = netaddr_to_string(p.second->get_addr());

            HOTSTUFF_LOG_INFO("Sending MsgDeployEpoch to replica at address: %s", addr_str.c_str());

            mn.send_msg(msg, p.second);

            HOTSTUFF_LOG_INFO("MsgDeployEpoch sent to replica successfully.");
        }

#ifndef HOTSTUFF_ENABLE_BENCHMARK
        // HOTSTUFF_LOG_INFO("send new cmd %.10s", get_hex(cmd->get_hash()).c_str());
#endif
        // waiting_finalized.insert(std::make_pair(cmd->get_hash(), Request(cmd)));

        // waiting.insert(std::make_pair(
        //     cmd->get_hash(), Request(cmd)));
        if (max_iter_num > 0)
            max_iter_num--;
        return false;
    }

    HOTSTUFF_LOG_INFO("Conditions not met for sending MsgDeployEpoch (waiting.size: %zu, max_async_num: %zu, max_iter_num: %d)", waiting.size(), max_async_num, max_iter_num);

    return false;
}

void client_resp_cmd_handler(MsgRespCmd &&msg, const Net::conn_t &)
{
    auto &fin = msg.fin;
    // Ignore intermediate decisions
    // HOTSTUFF_LOG_INFO("got %s", std::string(msg.fin).c_str());

    if (fin.decision == 1)
    {
        const uint256_t &cmd_hash = fin.cmd_hash;
        auto it = waiting_finalized.find(cmd_hash);
        auto &et = it->second.et;
        if (it == waiting_finalized.end())
            return;
        et.stop();

#ifndef HOTSTUFF_ENABLE_BENCHMARK
        // HOTSTUFF_LOG_INFO("got fin %s with wall: %.3f, cpu: %.3f",
        //                     std::string(fin).c_str(),
        //                     et.elapsed_sec, et.cpu_elapsed_sec);
#else
        struct timeval tv;
        gettimeofday(&tv, nullptr);
        elapsed.push_back(std::make_pair(tv, et.elapsed_sec));
#endif

        return;
    }

    const uint256_t &cmd_hash = fin.cmd_hash;
    auto it = waiting.find(cmd_hash);
    if (it == waiting.end())
        return;
    usleep(10);

    waiting_finalized.insert(*it);
    waiting.erase(it);
    while (try_send())
        ;
}

std::pair<std::string, std::string> split_ip_port_cport(const std::string &s)
{
    auto ret = salticidae::trim_all(salticidae::split(s, ";"));
    return std::make_pair(ret[0], ret[1]);
}

Epoch parse_epoch_config(const std::string &file_path)
{

    std::vector<Tree> new_trees;
    std::ifstream file(file_path);
    std::string line;
    size_t tid = 0;

    if (!file.is_open())
    {
        std::string str = "tree_config: Provided treegen file path is invalid! Failed to open file " + file_path;
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

        new_trees.push_back(Tree(tid, fanout, pipe_stretch, new_tree_array));
        tid++;
    }

    return Epoch(1, new_trees);
}

int main(int argc, char **argv)
{
    Config config("hotstuff.gen.conf");

    auto opt_idx = Config::OptValInt::create(0);
    auto opt_replicas = Config::OptValStrVec::create();
    auto opt_max_iter_num = Config::OptValInt::create(100);
    auto opt_max_async_num = Config::OptValInt::create(10);
    auto opt_cid = Config::OptValInt::create(-1);
    auto opt_client_target = Config::OptValStr::create("local");
    auto opt_new_epoch = Config::OptValStr::create("newepoch.conf");

    auto shutdown = [&](int)
    { ec.stop(); };
    salticidae::SigEvent ev_sigint(ec, shutdown);
    salticidae::SigEvent ev_sigterm(ec, shutdown);
    ev_sigint.add(SIGINT);
    ev_sigterm.add(SIGTERM);

    mn.reg_handler(client_resp_cmd_handler);
    mn.start();

    config.add_opt("idx", opt_idx, Config::SET_VAL);
    config.add_opt("cid", opt_cid, Config::SET_VAL);
    config.add_opt("replica", opt_replicas, Config::APPEND);
    config.add_opt("iter", opt_max_iter_num, Config::SET_VAL);
    config.add_opt("max-async", opt_max_async_num, Config::SET_VAL);
    config.add_opt("client-target", opt_client_target, Config::SET_VAL, 'x', "specify the replicas the client will communicate with (local, global)");

    config.add_opt("new-epoch", opt_new_epoch, Config::SET_VAL, 'e', "File with new epoch configuration");

    config.parse(argc, argv);

    auto idx = opt_idx->get();
    max_iter_num = opt_max_iter_num->get();
    max_async_num = opt_max_async_num->get();

    std::vector<std::string> raw;

    std::cout << "I am client with id = " << cid << std::endl;

    new_epoch = parse_epoch_config(opt_new_epoch->get());

    if (opt_client_target->get() == "local")
    {
        // Connect client to same id replica only

        for (const auto &s : opt_replicas->get())
        {
            auto res = salticidae::trim_all(salticidae::split(s, ","));
            if (res.size() < 1)
                throw HotStuffError("format error");
            raw.push_back(res[0]);
        }

        if (!(0 <= idx && (size_t)idx < raw.size() && raw.size() > 0))
            throw std::invalid_argument("out of range");
        cid = opt_cid->get() != -1 ? opt_cid->get() : idx;
        int i = 0;

        const auto my_replica = raw[cid];
        auto _p = split_ip_port_cport(my_replica);
        size_t _;
        replicas.push_back(NetAddr(NetAddr(_p.first).ip, htons(stoi(_p.second, &_))));

        HOTSTUFF_LOG_INFO("client sees replica num = %zu", replicas.size());

        conns.insert(std::make_pair(0, mn.connect_sync(replicas[0])));
    }
    else if (opt_client_target->get() == "global")
    {
        // Connect client to all replicas

        for (const auto &s : opt_replicas->get())
        {
            auto res = salticidae::trim_all(salticidae::split(s, ","));
            if (res.size() < 1)
                throw HotStuffError("format error");
            raw.push_back(res[0]);
        }

        if (!(0 <= idx && (size_t)idx < raw.size() && raw.size() > 0))
            throw std::invalid_argument("out of range");
        cid = opt_cid->get() != -1 ? opt_cid->get() : idx;
        for (const auto &p : raw)
        {
            auto _p = split_ip_port_cport(p);
            size_t _;
            replicas.push_back(NetAddr(NetAddr(_p.first).ip, htons(stoi(_p.second, &_))));
        }

        HOTSTUFF_LOG_INFO("client sees replica num = %zu", replicas.size());
        nfaulty = (replicas.size() - 1) / 3;
        // HOTSTUFF_LOG_INFO("nfaulty = %zu", nfaulty);

        for (size_t i = 0; i < replicas.size(); i++)
            conns.insert(std::make_pair(i, mn.connect_sync(replicas[i])));
    }
    else
    {
        throw std::invalid_argument("client-target must be either local or global");
    }

    while (try_send())
        ;

    ec.dispatch();

#ifdef HOTSTUFF_ENABLE_BENCHMARK
    for (const auto &e : elapsed)
    {
        char fmt[64];
        struct tm *tmp = localtime(&e.first.tv_sec);
        strftime(fmt, sizeof fmt, "%Y-%m-%d %H:%M:%S.%%06u [hotstuff info] %%.6f\n", tmp);
        fprintf(stderr, fmt, e.first.tv_usec, e.second);
    }
#endif
    return 0;
}
