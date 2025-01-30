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

#include "salticidae/type.h"
#include "salticidae/netaddr.h"
#include "salticidae/network.h"
#include "salticidae/util.h"
#include "salticidae/event.h"

#include "hotstuff/util.h"
#include "hotstuff/type.h"
#include "hotstuff/client.h"
#include "hotstuff/hotstuff.h"

#include <cstdlib>   // for rand()
#include <algorithm> // for find()
#include <cmath>     // for max()

using salticidae::Config;

using hotstuff::command_t;
using hotstuff::Epoch;
using hotstuff::EventContext;
using hotstuff::HotStuffError;
using hotstuff::MsgDeployEpoch;
using hotstuff::MsgLatencyReport;
using hotstuff::MsgTimeoutReport;
using hotstuff::NetAddr;
using hotstuff::opcode_t;
using hotstuff::ReplicaID;
using hotstuff::TimeoutMeasure;
using hotstuff::TimeoutReport;
using hotstuff::Tree;
using hotstuff::TreeNetwork;
using hotstuff::uint256_t;

EventContext ec;
ReplicaID proposer;
size_t max_async_num;
int max_iter_num;
uint32_t cid;
uint32_t cnt = 0;
uint32_t f;
Epoch current_epoch;

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

// Delay connectio to replicas
std::unordered_map<ReplicaID, bool> replica_connected;

// Reputation metrics
std::unordered_map<ReplicaID, int> rep_score;
// For each "suspected" node, store a set of reporter IDs who alleged it timed out
std::unordered_map<ReplicaID, std::unordered_set<ReplicaID>> timeout_evidence;

std::unordered_map<ReplicaID, int> reporter_trust;

// Timers
static salticidae::TimerEvent ev_epoch_timer;
static salticidae::TimerEvent ev_connect_timer;

void msg_reports_handler(MsgLatencyReport &&msg, const Net::conn_t &conn)
{
    /*Later we will use this i trust*/
}

void msg_timeout_report_handler(MsgTimeoutReport &&msg, const Net::conn_t &conn)
{
    auto &report = msg.report;

    ReplicaID reporter = report.reporter;
    std::vector<hotstuff::TimeoutMeasure> timeouts = report.timeouts;

    HOTSTUFF_LOG_INFO("[TIMEOUT HANDLER] Received timeout report from %d with %zu reports", reporter, timeouts.size());

    for (auto &tm : timeouts)
    {
        ReplicaID target = tm.non_responsive_replica;

        timeout_evidence[target].insert(reporter);

        HOTSTUFF_LOG_INFO("[TIMEOUT HANDLER] Replica %d reported timeout for %d", reporter, target);

        if (timeout_evidence[target].size() >= f + 1)
        {

            rep_score[target] -= 5;

            timeout_evidence[target].clear();

            HOTSTUFF_LOG_INFO("[TIMEOUT HANDLER] Confirmed multi-source timeout for replica %d, new rep_score=%d", target, rep_score[target]);
        }
        else
        {
            HOTSTUFF_LOG_INFO("[TIMEOUT HANDLER] Timeout evidence for replica %d: %zu/%d reporters", target, timeout_evidence[target].size(), f + 1);
        }
    }
}

/** ------------------------------------------------------------
 * Simplified Tree Generation:
 *   place worst f nodes as leaves, random-shuffle the rest
 * ------------------------------------------------------------
 */

/**
 * Build a single Tree by:
 * 1) Sort all_nodes by ascending reputation
 * 2) The first 'f' nodes in that sorted list -> "lowest rep"
 * 3) The rest -> "better"
 * 4) random shuffle "better", then append "lowest rep" at the end
 *    => The BFS array means last 'f' are leaves, the rest are internal
 */
Tree build_single_tree_simpler(
    uint32_t tid,
    uint8_t fanout,
    uint8_t pipe_stretch,
    const std::vector<uint32_t> &all_nodes,
    const std::unordered_map<ReplicaID, int> &rep_score,
    int f)
{
    std::vector<uint32_t> sorted_nodes(all_nodes.begin(), all_nodes.end());

    std::sort(sorted_nodes.begin(), sorted_nodes.end(),
              [&](uint32_t a, uint32_t b)
              {
                  int ra = 0, rb = 0;
                  auto ita = rep_score.find(a);
                  if (ita != rep_score.end())
                      ra = ita->second;
                  auto itb = rep_score.find(b);
                  if (itb != rep_score.end())
                      rb = itb->second;
                  return ra < rb; // ascending
              });

    if ((int)sorted_nodes.size() < f)
    {
        // edge case: if we have fewer than f nodes, just do a random shuffle
        // or handle differently
        std::random_shuffle(sorted_nodes.begin(), sorted_nodes.end());
        return Tree(tid, fanout, pipe_stretch, sorted_nodes);
    }

    // Extract the lowest f
    std::vector<uint32_t> lowrep(sorted_nodes.begin(), sorted_nodes.begin() + f);
    // The rest
    std::vector<uint32_t> better(sorted_nodes.begin() + f, sorted_nodes.end());

    // Shuffle the better portion
    std::random_shuffle(better.begin(), better.end());

    std::vector<uint32_t> tree_array;
    tree_array.reserve(sorted_nodes.size());

    for (auto &n : better)
        tree_array.push_back(n);
    for (auto &n : lowrep)
        tree_array.push_back(n);

    HOTSTUFF_LOG_INFO("[TREE GEN] Tree %d built with BFS array of size %zu", tid, tree_array.size());
    for (size_t i = 0; i < tree_array.size(); i++)
    {
        HOTSTUFF_LOG_INFO("[TREE GEN] Node %d at BFS index %zu, rep_score = %d", tree_array[i], i, rep_score.at(tree_array[i]));
    }

    // Construct a Tree
    return Tree(tid, fanout, pipe_stretch, tree_array);
}

/**
 * Generate a new Epoch with `num_trees` trees,
 * each built by the simpler approach of:
 *  - taking the f worst nodes as leaves,
 *  - random shuffling the rest as root & internal
 */
Epoch simpler_epoch_generation(
    const std::unordered_map<ReplicaID, int> &rep_score,
    const std::vector<uint32_t> &all_nodes,
    int num_trees,
    uint8_t fanout,
    uint8_t pipe_stretch,
    int f)
{
    Epoch ep;
    ep.epoch_num = 0; // set to an appropriate value if needed

    for (int t = 0; t < num_trees; t++)
    {
        // build one tree with the simpler approach
        Tree new_tree = build_single_tree_simpler(
            /*tid=*/t,
            fanout,
            pipe_stretch,
            all_nodes,
            rep_score,
            f);

        ep.trees.push_back(new_tree);
    }

    HOTSTUFF_LOG_INFO("[EPOCH GEN] Generated epoch with %d trees", num_trees);

    return ep;
}

/** ------------------------------------------------------------
 * Evaluate (score) an epoch
 * ------------------------------------------------------------
 */

/**
 * Score function:
 * For each Tree, interpret the tree_array as BFS order.
 * The root is weighted 3, next layer weighted 2, subsequent layers weighted 1.
 */
double score_epoch(const Epoch &epoch, const std::unordered_map<ReplicaID, int> &rep_score)
{
    double total_score = 0.0;

    for (const auto &tree : epoch.get_trees())
    {
        double tree_score = 0.0;
        const auto &arr = tree.get_tree_array();

        size_t idx = 0;
        size_t layer_num = 0; // 0 => root
        size_t layer_size = 1;
        uint8_t fan = tree.get_fanout();

        while (idx < arr.size())
        {
            for (size_t i = 0; i < layer_size && idx < arr.size(); i++)
            {
                uint32_t node = arr[idx++];
                int node_rep = 0;
                auto it = rep_score.find(node);
                if (it != rep_score.end())
                {
                    node_rep = it->second;
                }

                int weight = std::max(3 - static_cast<int>(layer_num), 1);
                tree_score += node_rep * weight;
            }
            layer_num++;
            layer_size *= fan;
        }

        total_score += tree_score;
    }

    return total_score;
}

/** ------------------------------------------------------------
 * Example Heuristic "Generate-and-Evaluate" if needed
 * ------------------------------------------------------------
 */

/**
 * Attempt up to max_tries to generate an epoch that is better
 * than old_epoch by at least improvement_needed, using simpler approach.
 */
Epoch generate_and_evaluate_epoch(
    const std::unordered_map<ReplicaID, int> &rep_score,
    const Epoch &old_epoch,
    const std::vector<uint32_t> &all_nodes,
    int num_trees,
    uint8_t fanout,
    uint8_t pipe_stretch,
    int f,
    double improvement_needed,
    int max_tries)
{
    double best_score = score_epoch(old_epoch, rep_score);
    Epoch best_epoch = old_epoch;

    for (int t = 0; t < max_tries; t++)
    {
        // generate a candidate
        Epoch candidate = simpler_epoch_generation(rep_score, all_nodes, num_trees, fanout, pipe_stretch, f);

        double cand_score = score_epoch(candidate, rep_score);

        if (cand_score > best_score + improvement_needed)
        {
            best_score = cand_score;
            best_epoch = candidate;
        }
    }

    best_epoch.epoch_num = old_epoch.get_epoch_num() + 1;

    return best_epoch;
}

Epoch parse_default_epoch_config(const std::string &file_path)
{

    std::vector<Tree> default_trees;
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

        default_trees.push_back(Tree(tid, fanout, pipe_stretch, new_tree_array));
        std::cout << default_trees.size() << std::endl;
        tid++;
    }

    return Epoch{0, default_trees};
}

std::pair<std::string, std::string> split_ip_port_cport(const std::string &s)
{
    auto ret = salticidae::trim_all(salticidae::split(s, ";"));
    return std::make_pair(ret[0], ret[1]);
}

void broadcast_epoch(const Epoch &epoch)
{

    for (const auto &[replica_id, conn] : conns)
    {
        if (conn != nullptr)
        {
            mn.send_msg(MsgDeployEpoch(epoch), conn);
            HOTSTUFF_LOG_INFO("Broadcasted epoch to replica %u", replica_id);
        }
        else
        {
            HOTSTUFF_LOG_WARN("Failed to broadcast epoch to replica %u: not connected", replica_id);
        }
    }
}

void on_epoch_timer(salticidae::TimerEvent &te, int unused)
{

    Epoch old_epoch = current_epoch;

    double improvement_needed = 0.0;
    int max_tries = 5;
    int num_trees = 4;
    uint8_t fanout = 2;
    uint8_t pipe_stretch = 2;

    // gather node IDs
    std::vector<uint32_t> all_nodes;
    for (auto &p : rep_score)
    {
        all_nodes.push_back(p.first);
    }

    Epoch candidate = generate_and_evaluate_epoch(
        rep_score,
        old_epoch,
        all_nodes,
        num_trees,
        fanout,
        pipe_stretch,
        f,
        improvement_needed,
        max_tries);

    double old_score = score_epoch(old_epoch, rep_score);
    double new_score = score_epoch(candidate, rep_score);

    HOTSTUFF_LOG_INFO("[EPOCH TIMER] Old epoch score = %.2f, New epoch score = %.2f", old_score, new_score);

    if (new_score > old_score)
    {
        // We found something better
        HOTSTUFF_LOG_INFO("Found a better epoch with score=%.2f, old=%.2f, adopting new epoch_num=%d",
                          score_epoch(candidate, rep_score),
                          score_epoch(old_epoch, rep_score),
                          candidate.get_epoch_num());

        current_epoch = candidate;

        broadcast_epoch(current_epoch);
    }
    else
    {
        HOTSTUFF_LOG_INFO("No improvement over old epoch, keep old one");
    }

    // re-schedule the timer if you want repeated triggers

    te.add(30.0);
}

void try_connect_to_replicas(salticidae::TimerEvent &te, int unused)
{
    bool all_connected = true;

    for (size_t i = 0; i < replicas.size(); i++)
    {
        if (!replica_connected[i])
        { // Check if not connected
            try
            {
                auto conn = mn.connect_sync(replicas[i]);
                if (conn != nullptr)
                {
                    conns[i] = conn;
                    replica_connected[i] = true;
                    HOTSTUFF_LOG_INFO("Successfully connected to replica %zu", i);
                }
                else
                {
                    all_connected = false;
                }
            }
            catch (const std::exception &e)
            {
                HOTSTUFF_LOG_WARN("Failed to connect to replica %zu: %s", i, e.what());
                all_connected = false;
            }
        }
    }

    if (!all_connected)
    {
        // Retry in 5 seconds if not all replicas are connected
        HOTSTUFF_LOG_INFO("Retrying connection to replicas in 5 seconds...");
        te.add(5.0);
    }
    else
    {
        HOTSTUFF_LOG_INFO("All replicas are connected.");
    }
}

void replay_mock_timeouts(const std::string &file_name)
{

    std::ifstream in(file_name);
    if (!in.is_open())
    {
        std::cerr << "Cannot open mock report file: " << file_name << std::endl;
        return;
    }

    std::string line;
    while (std::getline(in, line))
    {
        if (line.empty() || line[0] == '#')
            continue; // skip comments

        std::istringstream iss(line);
        uint32_t reporter, target, epoch, tid;
        char comma;
        iss >> reporter >> comma >> target >> comma >> epoch >> comma >> tid;

        TimeoutMeasure tm{target, epoch, tid};
        std::vector<hotstuff::TimeoutMeasure> vec{tm};
        TimeoutReport r{(ReplicaID)reporter, vec};

        MsgTimeoutReport msg(std::move(r));

        Net::conn_t dummy_conn = nullptr;
        msg_timeout_report_handler(std::move(msg), dummy_conn);
    }
}

int main(int argc, char **argv)
{
    Config config("hotstuff.gen.conf");

    auto opt_idx = Config::OptValInt::create(0);
    auto opt_replicas = Config::OptValStrVec::create();
    auto opt_max_iter_num = Config::OptValInt::create(100);
    auto opt_max_async_num = Config::OptValInt::create(10);
    auto opt_cid = Config::OptValInt::create(-1);
    auto opt_default_epoch = Config::OptValStr::create("treegen.conf");

    auto opt_mock_timeouts = Config::OptValStr::create("timeouts.conf");

    auto shutdown = [&](int)
    { ec.stop(); };
    salticidae::SigEvent ev_sigint(ec, shutdown);
    salticidae::SigEvent ev_sigterm(ec, shutdown);
    ev_sigint.add(SIGINT);
    ev_sigterm.add(SIGTERM);

    mn.reg_handler(msg_reports_handler);
    mn.reg_handler(msg_timeout_report_handler);

    mn.start();
    mn.listen(NetAddr("0.0.0.0", 50500));

    config.add_opt("idx", opt_idx, Config::SET_VAL);
    config.add_opt("cid", opt_cid, Config::SET_VAL);
    config.add_opt("replica", opt_replicas, Config::APPEND);
    config.add_opt("iter", opt_max_iter_num, Config::SET_VAL);
    config.add_opt("max-async", opt_max_async_num, Config::SET_VAL);
    config.add_opt("default_epoch", opt_default_epoch, Config::SET_VAL);
    config.add_opt("timeouts", opt_mock_timeouts, Config::SET_VAL);

    config.parse(argc, argv);

    HOTSTUFF_LOG_INFO("Reading epoch from %s", opt_default_epoch->get());

    current_epoch = parse_default_epoch_config(opt_default_epoch->get());

    ev_epoch_timer = salticidae::TimerEvent(ec, std::bind(&on_epoch_timer, std::placeholders::_1, 0));
    // start it (e.g. in 30s)
    ev_epoch_timer.add(30);

    HOTSTUFF_LOG_INFO("%s\n", std::string(current_epoch).c_str());

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
    f = (replicas.size() - 1) / 3;
    HOTSTUFF_LOG_INFO("nfaulty = %zu", f);

    // for (size_t i = 0; i < replicas.size(); i++)
    //     conns.insert(std::make_pair(i, mn.connect_sync(replicas[i])));

    // Initialize the connection status map
    for (size_t i = 0; i < replicas.size(); i++)
    {
        replica_connected[i] = false; // Mark all replicas as not connected
    }

    // Schedule the connection retries
    ev_connect_timer = salticidae::TimerEvent(ec, std::bind(&try_connect_to_replicas, std::placeholders::_1, 0));
    ev_connect_timer.add(90.0); // Start retrying 2 seconds after startup

    // Initialize rep_score to 0 for every replica (ID in [0..replicas.size()-1])
    for (size_t i = 0; i < replicas.size(); i++)
    {
        rep_score[i] = 0; // start from zero
    }

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
