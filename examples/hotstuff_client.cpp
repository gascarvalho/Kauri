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

#include "salticidae/type.h"
#include "salticidae/netaddr.h"
#include "salticidae/network.h"
#include "salticidae/util.h"

#include "hotstuff/util.h"
#include "hotstuff/type.h"
#include "hotstuff/client.h"

using salticidae::Config;

using hotstuff::command_t;
using hotstuff::EventContext;
using hotstuff::HotStuffError;
using hotstuff::MsgLatencyReport;
using hotstuff::NetAddr;
using hotstuff::opcode_t;
using hotstuff::ReplicaID;
using hotstuff::uint256_t;

EventContext ec;
ReplicaID proposer;
size_t max_async_num;
int max_iter_num;
uint32_t cid;
uint32_t cnt = 0;
uint32_t nfaulty;

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

void msg_reports_handler(MsgLatencyReport &&msg, const Net::conn_t &conn)
{

    HOTSTUFF_LOG_INFO("OLÁ ESTOU A RECEBER MERDAS ");
    auto &report = msg.report; // Access the encapsulated report data

    // Example: Log the report details
    HOTSTUFF_LOG_INFO("[REPORT HANDLER] Received report from replica %d", report.reporter);

    for (const auto &latMeasure : report.lats)
    {
        HOTSTUFF_LOG_INFO("From replica=%d, Latency=%llu, epoch_nr=%d, tid=%d", latMeasure.child, latMeasure.latency_us, latMeasure.epoch_nr, latMeasure.tid);
    }
}

std::pair<std::string, std::string> split_ip_port_cport(const std::string &s)
{
    auto ret = salticidae::trim_all(salticidae::split(s, ";"));
    return std::make_pair(ret[0], ret[1]);
}

int main(int argc, char **argv)
{
    Config config("hotstuff.gen.conf");

    auto opt_idx = Config::OptValInt::create(0);
    auto opt_replicas = Config::OptValStrVec::create();
    auto opt_max_iter_num = Config::OptValInt::create(100);
    auto opt_max_async_num = Config::OptValInt::create(10);
    auto opt_cid = Config::OptValInt::create(-1);

    auto shutdown = [&](int)
    { ec.stop(); };
    salticidae::SigEvent ev_sigint(ec, shutdown);
    salticidae::SigEvent ev_sigterm(ec, shutdown);
    ev_sigint.add(SIGINT);
    ev_sigterm.add(SIGTERM);

    mn.reg_handler(msg_reports_handler);
    mn.start();
    mn.listen(NetAddr("0.0.0.0", 50500));

    config.add_opt("idx", opt_idx, Config::SET_VAL);
    config.add_opt("cid", opt_cid, Config::SET_VAL);
    config.add_opt("replica", opt_replicas, Config::APPEND);
    config.add_opt("iter", opt_max_iter_num, Config::SET_VAL);
    config.add_opt("max-async", opt_max_async_num, Config::SET_VAL);

    config.parse(argc, argv);

    auto idx = opt_idx->get();
    max_iter_num = opt_max_iter_num->get();
    max_async_num = opt_max_async_num->get();
    std::vector<std::string> raw;

    // std::cout << "I am client with id = " << cid << std::endl;

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
    HOTSTUFF_LOG_INFO("nfaulty = %zu", nfaulty);

    for (size_t i = 0; i < replicas.size(); i++)
        conns.insert(std::make_pair(i, mn.connect_sync(replicas[i])));

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
