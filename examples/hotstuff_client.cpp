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

#include <cstdlib> // For rand(), RAND_MAX
#include <ctime>   // For time() - to seed the random generator

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

using hotstuff::CollectedReport;
using hotstuff::command_t;
using hotstuff::Epoch;
using hotstuff::EpochReputation;
using hotstuff::EventContext;
using hotstuff::HotStuffError;
using hotstuff::MISSING_PROPOSAL;
using hotstuff::MISSING_VOTE;
using hotstuff::MsgDeployEpoch;
using hotstuff::MsgDeployEpochReputation;
using hotstuff::MsgLatencyReport;
using hotstuff::MsgTimeoutReport;
using hotstuff::NetAddr;
using hotstuff::opcode_t;
using hotstuff::ReplicaID;
using hotstuff::ReportType;
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
bool mock_mode;

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

std::unordered_map<uint32_t, std::vector<CollectedReport>> reportsByTid;

Net mn(ec, Net::Config());

// Delay connectio to replicas
std::unordered_map<ReplicaID, bool> replica_connected;

// Reputation metrics
std::unordered_map<ReplicaID, double> rep_score;

// Global structure to store votes received per node in the current epoch.
std::unordered_map<ReplicaID, std::set<ReplicaID>> votesReceived;
std::unordered_map<ReplicaID, std::set<ReplicaID>> votesIssued;

std::set<std::pair<ReplicaID, ReplicaID>> tree_contraints;

// Timers
static salticidae::TimerEvent ev_epoch_timer;
static salticidae::TimerEvent ev_connect_timer;

const double delta = 1.0;

// Helper function to log the entire reputation table.
void log_reputation_table()
{
    std::ostringstream oss;

    oss << "Current Reputation Table: \n";

    for (const auto &p : rep_score)
    {
        oss << "[Replica " << p.first << ": " << p.second << "]\n";
    }

    HOTSTUFF_LOG_INFO("%s", oss.str().c_str());
}

void msg_reports_handler(MsgLatencyReport &&msg, const Net::conn_t &conn)
{
    /*Later we will use this i trust*/
}

void process_report(ReplicaID reporter, ReplicaID target)
{

    if (votesReceived[target].find(reporter) != votesReceived[target].end())
    {
        HOTSTUFF_LOG_INFO("[REPORT] Duplicate report: Reporter %d already reported Target %d. Ignored.", reporter, target);
        return;
    }

    votesReceived[target].insert(reporter);
    votesIssued[reporter].insert(target);

    rep_score[reporter] -= delta;
    rep_score[target] -= delta;

    HOTSTUFF_LOG_INFO("[REPORT] Reporter %d -> Target %d. Decrease both by %.2f", reporter, target, delta);
}

void msg_timeout_report_handler(MsgTimeoutReport &&msg, const Net::conn_t &conn)
{
    auto &report = msg.report;

    ReplicaID reporter = report.reporter;
    std::vector<hotstuff::TimeoutMeasure> timeouts = report.timeouts;

    HOTSTUFF_LOG_INFO("[TIMEOUT HANDLER] Received timeout report from %d with %zu reports", reporter, timeouts.size());

    for (auto &tm : timeouts)
    {

        CollectedReport cr;

        cr.reporter = reporter;
        cr.target = tm.non_responsive_replica;
        cr.epoch = tm.epoch_nr;
        cr.tid = tm.tid;
        cr.missing_voter = tm.missing_voter;

        if (cr.tid >= current_epoch.get_trees().size())
        {
            HOTSTUFF_LOG_WARN("[TIMEOUT HANDLER] Invalid tree id %d in report from Replica %d. Skipping report.", cr.tid, reporter);
            continue;
        }

        const Tree &tree = current_epoch.get_trees()[cr.tid];

        HOTSTUFF_LOG_INFO("[TIMEOUT HANDLER] Processing report: Reporter %d, Target %d, Missing voter %d, Epoch %d, Tid %d", cr.reporter, cr.target, cr.missing_voter, cr.epoch, cr.tid);

        if (tree.is_parent_of(cr.target, reporter))
        {
            cr.report_type = MISSING_PROPOSAL;
            cr.missing_voter = reporter; // Just to ensure there's no proposal missings of another replica in the tree that not just the parent
            HOTSTUFF_LOG_INFO("[TIMEOUT HANDLER] Determined as MISSING_PROPOSAL: Reporter %d is child of Target %d", cr.reporter, cr.target);
        }
        else if (tree.is_parent_of(reporter, cr.target))
        {
            cr.report_type = MISSING_VOTE;
            HOTSTUFF_LOG_INFO("[TIMEOUT HANDLER] Determined as MISSING_VOTE: Reporter %d is parent of Target %d", cr.reporter, cr.target);
        }
        else
        {
            HOTSTUFF_LOG_WARN("[TIMEOUT HANDLER] Ambiguous report from Replica %d: Neither direct parent-child relation found between Reporter %d and Target %d. Skipping report.", reporter, cr.reporter, cr.target);
            continue;
        }

        reportsByTid[cr.tid].push_back(cr);
        HOTSTUFF_LOG_INFO("[TIMEOUT HANDLER] Added report from Replica %d for tree %d", reporter, cr.tid);
    }
}

Tree tree_generator(
    uint32_t tid,
    uint8_t fanout,
    uint8_t pipe_stretch,
    const std::vector<uint32_t> &all_nodes,
    const std::unordered_map<ReplicaID, double> &rep_score,
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
                  return ra < rb;
              });

    if ((int)sorted_nodes.size() < f)
    {
        std::random_shuffle(sorted_nodes.begin(), sorted_nodes.end());
        return Tree(tid, fanout, pipe_stretch, sorted_nodes);
    }

    std::vector<uint32_t> lowrep(sorted_nodes.begin(), sorted_nodes.begin() + f);
    std::vector<uint32_t> better(sorted_nodes.begin() + f, sorted_nodes.end());

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
        HOTSTUFF_LOG_INFO("[TREE GEN] Node %d at BFS index %zu, rep_score = %.2f", tree_array[i], i, rep_score.at(tree_array[i]));
    }

    return Tree(tid, fanout, pipe_stretch, tree_array);
}

Tree constraint_tree(
    uint32_t tid,
    uint8_t fanout,
    uint8_t pipe_stretch,
    const std::vector<uint32_t> &all_nodes,
    const std::unordered_map<ReplicaID, double> &rep_score,
    int f,
    int maxAttempts = 10)
{

    Tree candidateTree;
    int attempt = 0;
    bool valid = false;

    while (attempt < maxAttempts && !valid)
    {
        candidateTree = tree_generator(tid, fanout, pipe_stretch, all_nodes, rep_score, f);

        if (!candidateTree.violates_votes_constraint(tree_contraints))
        {
            valid = true;
        }
        else
        {
            HOTSTUFF_LOG_INFO("[TREE GEN] Candidate tree (attempt %d) violates votes constraint. Retrying...", attempt);
            attempt++;
        }
    }

    if (!valid)
        HOTSTUFF_LOG_WARN("[TREE GEN] Failed to generate a candidate tree without reported direct relationships after %d attempts.", maxAttempts);

    return candidateTree;
}

Epoch epoch_generator(
    const std::unordered_map<ReplicaID, double> &rep_score,
    const std::vector<uint32_t> &all_nodes,
    uint8_t fanout,
    uint8_t pipe_stretch,
    int f)
{
    Epoch new_epoch;
    new_epoch.epoch_num = current_epoch.epoch_num + 1;

    int num_trees_to_generate = std::min(current_epoch.get_trees().size(), all_nodes.size() - f);

    for (int t = 0; t < num_trees_to_generate; t++)
    {
        Tree new_tree = constraint_tree(
            t, /*tid=*/
            fanout,
            pipe_stretch,
            all_nodes,
            rep_score,
            f);

        new_epoch.trees.push_back(new_tree);
    }

    HOTSTUFF_LOG_INFO("[EPOCH GEN] Generated epoch with %d trees", num_trees_to_generate);

    return new_epoch;
}

double score_epoch(const Epoch &epoch, const std::unordered_map<ReplicaID, double> &rep_score)
{
    double total_score = 0.0;

    for (const auto &tree : epoch.get_trees())
    {
        double tree_score = 0.0;
        const auto &arr = tree.get_tree_array();

        size_t idx = 0;
        size_t layer_num = 0;
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

                int weight = std::max(static_cast<int>(tree.get_height()) - static_cast<int>(layer_num), 1);
                tree_score += node_rep * weight;
            }
            layer_num++;
            layer_size *= fan;
        }

        total_score += tree_score;
    }

    return total_score;
}

Epoch generate_and_evaluate_epoch(
    const std::unordered_map<ReplicaID, double> &rep_score,
    const Epoch &old_epoch,
    const std::vector<uint32_t> &all_nodes,
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
        Epoch candidate = epoch_generator(rep_score, all_nodes, fanout, pipe_stretch, f);

        double cand_score = score_epoch(candidate, rep_score);

        if (cand_score > best_score + improvement_needed)
        {
            best_score = cand_score;
            best_epoch = candidate;
            break;
        }
    }

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

void broadcast_epoch(Epoch &epoch)
{

    if (mock_mode)
    {
        std::cout << "New epoch generated:\n"
                  << std::string(epoch).c_str()
                  << std::endl;
    }
    else
    {
        for (const auto &[replica_id, conn] : conns)
        {
            if (conn != nullptr)
            {
                // mn.send_msg(MsgDeployEpochReputation(EpochReputation(epoch, rep_score)), conn);
                HOTSTUFF_LOG_INFO("Broadcasted epoch to replica %u", replica_id);
            }
            else
            {
                HOTSTUFF_LOG_WARN("Failed to broadcast epoch to replica %u: not connected", replica_id);
            }
        }
    }
}

size_t get_level(const Tree &tree, size_t pos)
{
    if (pos >= tree.get_tree_array().size() || pos == 0)
        return 0;
    double m = static_cast<double>(tree.get_fanout());
    return static_cast<size_t>(std::floor(std::log(pos + 1) / std::log(m)));
}

std::vector<CollectedReport> process_missing_voter_chain(const Tree &tree, ReplicaID missingVoter, const std::vector<CollectedReport> &reportsForX)
{
    // Sort reportsForX by reporter depth descending (deepest first)
    std::vector<CollectedReport> sortedReports = reportsForX;
    std::sort(sortedReports.begin(), sortedReports.end(), [&](const CollectedReport &a, const CollectedReport &b)
              {
                size_t posA = tree.get_node_position(a.reporter);
                size_t posB = tree.get_node_position(b.reporter);
                return get_level(tree, posA) > get_level(tree, posB); });

    if (sortedReports.empty())
        return sortedReports;

    // Start from the deepest report.
    std::vector<CollectedReport> useful;

    CollectedReport current = sortedReports[0];

    HOTSTUFF_LOG_INFO("[CHAIN] Storing processing for missing voter %d from reporter %d to target %d", missingVoter, current.reporter, current.target);

    useful.push_back(current);

    HOTSTUFF_LOG_INFO("[CHAIN] Starting chain processing for missing voter %d from reporter %d", missingVoter, current.reporter);
    size_t pos = tree.get_node_position(current.reporter);

    while (pos > 0 && pos < tree.get_tree_array().size())
    {
        size_t parentPos = (pos - 1) / tree.get_fanout();
        ReplicaID parent = tree.get_tree_array()[parentPos];

        bool found = false;
        for (const auto &r : sortedReports)
        {
            if (r.reporter == parent)
            {
                found = true;

                HOTSTUFF_LOG_INFO("[CHAIN] Found report from parent %d in chain for missing voter %d", parent, missingVoter);
                break;
            }
        }
        if (!found)
        {
            rep_score[parent] -= delta;
            HOTSTUFF_LOG_INFO("[CHAIN] Missing report: Penalizing expected parent %d for missing voter %d, penalty = %.2f", parent, missingVoter, delta);
        }

        pos = parentPos;
    }

    // Log the final chain reports being returned.
    HOTSTUFF_LOG_INFO("[CHAIN] Final chain reports for missing voter %d:", missingVoter);
    for (const auto &r : useful)
    {
        size_t level = get_level(tree, tree.get_node_position(r.reporter));
        HOTSTUFF_LOG_INFO("[CHAIN]   Reporter %d -> Target %d (level %zu)", r.reporter, r.target, level);
    }

    return useful;
}

void process_reports_for_tid(uint32_t tid)
{
    auto &reports = reportsByTid[tid];

    HOTSTUFF_LOG_INFO("[PROCESS] Processing reports for tree id %d. Total reports = %zu", tid, reports.size());

    // Group reports by missing voter and type.
    std::unordered_map<ReplicaID, std::vector<CollectedReport>> missing_vote_reports;

    std::vector<CollectedReport> filtered; // final reports to be processed

    for (const auto &r : reports)
    {
        if (r.report_type == MISSING_VOTE)
            missing_vote_reports[r.missing_voter].push_back(r);
        else if (r.report_type == MISSING_PROPOSAL)
            filtered.push_back(r);
    }

    const Tree &tree = current_epoch.get_trees()[tid];

    // For each missing voter, process the chain.
    for (const auto &entry : missing_vote_reports)
    {
        ReplicaID missingVoter = entry.first;

        const std::vector<CollectedReport> &reportsForX = entry.second;

        HOTSTUFF_LOG_INFO("[PROCESS] Processing chain for missing voter %d in tree %d, with %zu reports.", missingVoter, tid, reportsForX.size());

        auto chainReports = process_missing_voter_chain(tree, missingVoter, reportsForX);

        filtered.insert(filtered.end(), chainReports.begin(), chainReports.end());
    }

    // After chain processing, process each report normally.
    for (const auto &r : filtered)
    {
        HOTSTUFF_LOG_INFO("[PROCESS] Processing report: Reporter %d -> Target %d", r.reporter, r.target);
        tree_contraints.insert(std::pair(r.reporter, r.target));
        process_report(r.reporter, r.target);
    }
}

void reward_unvoted_replicas()
{

    for (const auto &entry : rep_score)
    {
        ReplicaID replica = entry.first;

        bool noIssued = (votesIssued.find(replica) == votesIssued.end()) || votesIssued[replica].empty();
        bool noReceived = (votesReceived.find(replica) == votesReceived.end()) || votesReceived[replica].empty();

        if (noIssued && noReceived)
        {
            rep_score[replica] += delta;
            HOTSTUFF_LOG_INFO("[REWARD] Rewarding Replica %d with delta %.2f (did not vote or receive any vote).", replica, delta);
        }
    }
}

void process_all_reports()
{
    log_reputation_table();

    for (auto &entry : reportsByTid)
    {
        uint32_t tid = entry.first;
        process_reports_for_tid(tid);
    }

    reportsByTid.clear();

    reward_unvoted_replicas();

    // Clear the epoch votes since the contrainsts are already captured
    votesIssued.clear();
    votesReceived.clear();

    log_reputation_table();
}

void on_epoch_timer(salticidae::TimerEvent &te, int unused)
{

    process_all_reports();

    Epoch old_epoch = current_epoch;

    double improvement_needed = 0.0;
    int max_tries = 5;
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

        ReplicaID reporter, target, missing_voter;
        uint32_t epoch, tid;

        char comma;
        iss >> reporter >> comma >> target >> comma >> epoch >> comma >> tid >> comma >> missing_voter;

        TimeoutMeasure tm{target, epoch, tid, missing_voter};
        std::vector<TimeoutMeasure> vec{tm};
        TimeoutReport r{reporter, vec};

        MsgTimeoutReport msg(std::move(r), true);

        Net::conn_t dummy_conn = nullptr;
        msg_timeout_report_handler(std::move(msg), dummy_conn);
    }
}

int main(int argc, char **argv)
{
    srand(static_cast<unsigned int>(time(nullptr)));

    Config config("hotstuff.gen.conf");

    auto opt_idx = Config::OptValInt::create(0);
    auto opt_replicas = Config::OptValStrVec::create();
    auto opt_max_iter_num = Config::OptValInt::create(100);
    auto opt_max_async_num = Config::OptValInt::create(10);
    auto opt_mock_mode = Config::OptValFlag::create(true);

    auto opt_cid = Config::OptValInt::create(-1);
    auto opt_default_epoch = Config::OptValStr::create("treegen.conf");
    auto opt_mock_timeouts = Config::OptValStr::create("timeouts4.conf");

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
    config.add_opt("mock", opt_mock_mode, Config::SET_VAL);
    config.add_opt("default_epoch", opt_default_epoch, Config::SET_VAL);
    config.add_opt("timeouts", opt_mock_timeouts, Config::SET_VAL);

    config.parse(argc, argv);

    mock_mode = opt_mock_mode->get();

    HOTSTUFF_LOG_INFO("Reading epoch from %s", opt_default_epoch->get().c_str());

    current_epoch = parse_default_epoch_config(opt_default_epoch->get());

    ev_epoch_timer = salticidae::TimerEvent(ec, std::bind(&on_epoch_timer, std::placeholders::_1, 0));
    // start it (e.g. in 30s)
    ev_epoch_timer.add(5);

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

    // Initialize rep_score to 0 for every replica (ID in [0..replicas.size()-1])
    for (size_t i = 0; i < replicas.size(); i++)
    {
        rep_score[i] = 0.0; // start from zero
    }

    // for (size_t i = 0; i < replicas.size(); i++)
    //     conns.insert(std::make_pair(i, mn.connect_sync(replicas[i])));

    if (mock_mode)
    {
        replay_mock_timeouts(opt_mock_timeouts->get());
    }
    else
    {
        // Initialize the connection status map
        for (size_t i = 0; i < replicas.size(); i++)
        {
            replica_connected[i] = false; // Mark all replicas as not connected
        }

        // Schedule the connection retries
        ev_connect_timer = salticidae::TimerEvent(ec, std::bind(&try_connect_to_replicas, std::placeholders::_1, 0));
        ev_connect_timer.add(90.0); // Start retrying 2 seconds after startup
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
