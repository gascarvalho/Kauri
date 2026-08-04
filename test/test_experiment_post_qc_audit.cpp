#include <algorithm>
#include <cctype>
#include <cstdint>
#include <fstream>
#include <iterator>
#include <set>
#include <stdexcept>
#include <string>
#include <vector>

#include "catch.hpp"
#include "hotstuff/experiment_post_qc_audit.h"
#include "hotstuff/hotstuff.h"
#include "support/bls_fixtures.h"

#ifndef KAURI_PROJECT_SOURCE_DIR
#error "KAURI_PROJECT_SOURCE_DIR must name the repository root"
#endif

namespace
{

using namespace hotstuff;
using hotstuff::test::BlsTestCore;
using hotstuff::test::add_valid_signers;

constexpr ReplicaID kReporter = 0;
constexpr ReplicaID kTarget = 5;
constexpr ReplicaID kRoot = 30;
constexpr std::uint64_t kGeneration = 41;
constexpr std::uint64_t kArmedNs = 1'000'000'000;
constexpr std::uint64_t kDeadlineNs =
    kArmedNs + kExperimentPostQcAuditDeadlineMs * 1'000'000;
constexpr std::uint64_t kPreparedNs = kArmedNs + 50'000'000;
constexpr std::uint64_t kPublishedNs = kPreparedNs + 1'000'000;
constexpr std::uint64_t kReceivedNs = kDeadlineNs + 1'000'000;
constexpr std::uint64_t kVerifiedNs = kReceivedNs + 1'000'000;
constexpr std::uint64_t kExpiryNs =
    kPublishedNs + kExperimentPostQcAuditRetentionMs * 1'000'000;

uint256_t digest(const std::string &label)
{
    return DataStream(label).get_hash();
}

ConfigurationId configuration()
{
    return ConfigurationId{7, 30, digest("pqar-epoch")};
}

ProposalKey proposal()
{
    return ProposalKey{configuration(), digest("pqar-block")};
}

ExperimentPostQcAuditOptions options(bool forge = false)
{
    ExperimentPostQcAuditOptions result;
    result.enabled = true;
    result.configuration = configuration();
    result.diagnostic_window = "pqar-window-1";
    result.reporter = kReporter;
    result.target = kTarget;
    result.root = kRoot;
    result.forge_missing_claim = forge;
    result.deadline_ms = kExperimentPostQcAuditDeadlineMs;
    result.retention_ms = kExperimentPostQcAuditRetentionMs;
    result.maximum_contexts = kExperimentPostQcAuditContextLimit;
    return result;
}

ProposalTreeSnapshot reporter_tree()
{
    ProposalTreeSnapshot tree;
    tree.local_replica = kReporter;
    tree.root = kRoot;
    tree.parent = kRoot;
    tree.direct_children = {kTarget, 6, 7, 8, 9};
    tree.assigned_subtree = {kReporter, kTarget, 6, 7, 8, 9};
    tree.child_subtrees = {
        {kTarget, {kTarget}}, {6, {6}}, {7, {7}}, {8, {8}}, {9, {9}}};
    tree.fanout = 5;
    tree.pipeline_stretch = 1;
    return tree;
}

ProposalTreeSnapshot root_tree()
{
    ProposalTreeSnapshot tree;
    tree.local_replica = kRoot;
    tree.root = kRoot;
    tree.parent = std::nullopt;
    tree.direct_children = {kReporter, 1, 2, 3, 4};
    for (ReplicaID replica = 0; replica <= kRoot; ++replica)
        tree.assigned_subtree.push_back(replica);
    tree.child_subtrees = {
        {kReporter, {kReporter, kTarget, 6, 7, 8, 9}},
        {1, {1, 10, 11, 12, 13, 14}},
        {2, {2, 15, 16, 17, 18, 19}},
        {3, {3, 20, 21, 22, 23, 24}},
        {4, {4, 25, 26, 27, 28, 29}}};
    tree.fanout = 5;
    tree.pipeline_stretch = 2;
    return tree;
}

std::vector<ReplicaID> n31_reporter_complement()
{
    return {
        1, 2, 3, 4,
        10, 11, 12, 13, 14,
        15, 16, 17, 18, 19,
        20, 21, 22, 23, 24,
        25, 26, 27, 28, 29, 30};
}

quorum_cert_bt aggregate(BlsTestCore &core,
                         const std::vector<ReplicaID> &signers)
{
    auto certificate = quorum_cert_bt(
        new QuorumCertAggBLS(core.get_config(), proposal()));
    if (!signers.empty())
    {
        auto &bls = dynamic_cast<QuorumCertAggBLS &>(*certificate);
        add_valid_signers(bls, core, signers, proposal());
        certificate->compute();
        REQUIRE(certificate->verify(core.get_config()));
    }
    return certificate;
}

std::set<ReplicaID> signers(const QuorumCert &certificate)
{
    const auto listed = certificate.get_signers();
    return std::set<ReplicaID>(listed.begin(), listed.end());
}

bytearray_t certificate_bytes(const QuorumCert &certificate)
{
    DataStream stream;
    const_cast<QuorumCert &>(certificate).serialize(stream);
    return static_cast<bytearray_t>(stream);
}

void arm(ExperimentPostQcAudit &audit,
         BlsTestCore &core,
         const std::vector<ReplicaID> &initial = {kReporter})
{
    auto accumulator = aggregate(core, initial);
    REQUIRE(audit.arm_reporter(
        proposal(),
        kGeneration,
        reporter_tree(),
        *accumulator,
        kArmedNs));
}

ExperimentPostQcAuditRelay relay(
    BlsTestCore &core,
    const std::vector<ReplicaID> &relay_signers,
    std::uint64_t emitted_ns = kDeadlineNs)
{
    ExperimentPostQcAuditRelay result;
    result.proposal = proposal();
    result.generation = kGeneration;
    result.diagnostic_window = options().diagnostic_window;
    result.reporter = kReporter;
    result.target = kTarget;
    result.root = kRoot;
    result.armed_ns = kArmedNs;
    result.deadline_ns = kDeadlineNs;
    result.emitted_ns = emitted_ns;
    result.certificate = aggregate(core, relay_signers);
    return result;
}

void prepare_and_activate_root(ExperimentPostQcAudit &root,
                               BlsTestCore &core)
{
    auto qc = aggregate(core, {kRoot, 1, 2});
    const auto prepared = root.prepare_root(
        proposal(), kGeneration, root_tree(), *qc, 3, kPreparedNs);
    REQUIRE(prepared.has_value());
    CHECK(prepared->published_ns == 0);
    CHECK(prepared->expiry_ns == 0);
    REQUIRE(root.activate_root(
        proposal(), kGeneration, *qc, kPublishedNs, true));
    const auto active = root.root_snapshot();
    REQUIRE(active.has_value());
    CHECK(active->published_ns == kPublishedNs);
    CHECK(active->expiry_ns == kExpiryNs);
}

std::string read_source(const std::string &relative_path)
{
    const std::string path =
        std::string(KAURI_PROJECT_SOURCE_DIR) + "/" + relative_path;
    std::ifstream input(path);
    REQUIRE(input.good());
    return std::string(std::istreambuf_iterator<char>(input),
                       std::istreambuf_iterator<char>());
}

std::string function_body(const std::string &source,
                          const std::string &signature)
{
    const auto start = source.find(signature);
    REQUIRE(start != std::string::npos);
    const auto opening = source.find('{', start);
    REQUIRE(opening != std::string::npos);
    std::size_t depth = 0;
    for (std::size_t index = opening; index < source.size(); ++index)
    {
        if (source[index] == '{')
            ++depth;
        else if (source[index] == '}' && --depth == 0)
            return source.substr(opening, index - opening + 1);
    }
    FAIL("unterminated function body for " << signature);
    return {};
}

std::size_t certificate_bitmap_offset(
    const ExperimentPostQcAuditRelay &value)
{
    DataStream prefix;
    const auto window_size = static_cast<std::uint16_t>(
        value.diagnostic_window.size());
    prefix << htole(kExperimentPostQcAuditWireSchemaVersion)
           << htole(value.proposal.configuration.epoch_number)
           << htole(value.proposal.configuration.tree_id)
           << value.proposal.configuration.epoch_digest
           << value.proposal.block_hash
           << htole(value.generation)
           << htole(value.reporter)
           << htole(value.target)
           << htole(value.root)
           << htole(value.armed_ns)
           << htole(value.deadline_ns)
           << htole(value.emitted_ns)
           << htole(window_size)
           << value.diagnostic_window;
    DataStream key;
    serialize_proposal_key(key, value.proposal);
    return prefix.size() + key.size();
}

TEST_CASE("post-QC audit is disabled by default")
{
    ExperimentPostQcAudit audit;
    CHECK_FALSE(audit.enabled());
    CHECK_FALSE(audit.is_reporter());
    CHECK_FALSE(audit.is_root());
    CHECK_FALSE(audit.diagnostics().reporter_armed);
    CHECK_FALSE(audit.diagnostics().root_prepared);
}

TEST_CASE("disabled and enabled audit paths preserve consensus-owned QC bytes")
{
    BlsTestCore core(31);
    auto reporter_accumulator = aggregate(core, {kReporter});
    const auto reporter_before = certificate_bytes(*reporter_accumulator);

    ExperimentPostQcAudit disabled;
    CHECK_FALSE(disabled.arm_reporter(
        proposal(),
        kGeneration,
        reporter_tree(),
        *reporter_accumulator,
        kArmedNs));
    CHECK(certificate_bytes(*reporter_accumulator) == reporter_before);

    ExperimentPostQcAudit reporter(options(), kReporter);
    REQUIRE(reporter.arm_reporter(
        proposal(),
        kGeneration,
        reporter_tree(),
        *reporter_accumulator,
        kArmedNs));
    CHECK(certificate_bytes(*reporter_accumulator) == reporter_before);

    auto synchronized = aggregate(core, {kReporter, kTarget});
    const auto synchronized_before = certificate_bytes(*synchronized);
    REQUIRE(reporter.synchronize_reporter_accumulator(
        proposal(),
        kGeneration,
        *synchronized,
        kTarget,
        kDeadlineNs - 1).has_value());
    CHECK(certificate_bytes(*synchronized) == synchronized_before);

    auto published_qc = aggregate(core, {kRoot, 1, 2});
    const auto published_before = certificate_bytes(*published_qc);
    ExperimentPostQcAudit root(options(), kRoot);
    REQUIRE(root.prepare_root(
        proposal(),
        kGeneration,
        root_tree(),
        *published_qc,
        3,
        kPreparedNs).has_value());
    REQUIRE(root.activate_root(
        proposal(),
        kGeneration,
        *published_qc,
        kPublishedNs,
        true));
    CHECK(certificate_bytes(*published_qc) == published_before);
}

TEST_CASE("post-QC audit pins timing identity and one-context bounds")
{
    auto invalid = options();
    invalid.deadline_ms = 149;
    CHECK_THROWS_AS(ExperimentPostQcAudit(invalid, kReporter),
                    std::invalid_argument);
    invalid = options();
    invalid.retention_ms = 249;
    CHECK_THROWS_AS(ExperimentPostQcAudit(invalid, kReporter),
                    std::invalid_argument);
    invalid = options();
    invalid.maximum_contexts = 2;
    CHECK_THROWS_AS(ExperimentPostQcAudit(invalid, kReporter),
                    std::invalid_argument);
    invalid = options();
    invalid.diagnostic_window = "unsafe window";
    CHECK_THROWS_AS(ExperimentPostQcAudit(invalid, kReporter),
                    std::invalid_argument);
    invalid = options();
    invalid.target = invalid.reporter;
    CHECK_THROWS_AS(ExperimentPostQcAudit(invalid, kReporter),
                    std::invalid_argument);
    invalid = options();
    invalid.configuration.epoch_digest = uint256_t{};
    CHECK_THROWS_AS(ExperimentPostQcAudit(invalid, kReporter),
                    std::invalid_argument);
}

TEST_CASE("post-QC audit exposes only configured local roles")
{
    ExperimentPostQcAudit reporter(options(), kReporter);
    ExperimentPostQcAudit root(options(), kRoot);
    ExperimentPostQcAudit bystander(options(), ReplicaID{7});
    CHECK(reporter.is_reporter());
    CHECK_FALSE(reporter.is_root());
    CHECK(root.is_root());
    CHECK_FALSE(root.is_reporter());
    CHECK_FALSE(bystander.is_reporter());
    CHECK_FALSE(bystander.is_root());
}

TEST_CASE("verified target is observed before the strict deadline")
{
    BlsTestCore core(31);
    ExperimentPostQcAudit audit(options(), kReporter);
    arm(audit, core);
    auto accumulated = aggregate(core, {kReporter, kTarget});
    const auto observed = audit.synchronize_reporter_accumulator(
        proposal(), kGeneration, *accumulated, kTarget, kDeadlineNs - 1);
    REQUIRE(observed.has_value());
    CHECK(observed->phase == ExperimentPostQcAuditTargetPhase::open);
    CHECK(observed->arrival_ns == kDeadlineNs - 1);
    CHECK(observed->signers == std::set<ReplicaID>{kReporter, kTarget});
    CHECK_FALSE(audit.consume_reporter_deadline(kDeadlineNs).has_value());
    CHECK(audit.diagnostics().reporter_terminal);
}

TEST_CASE("verified target remains auditable after consensus context close")
{
    BlsTestCore core(31);
    ExperimentPostQcAudit audit(options(), kReporter);
    arm(audit, core);
    REQUIRE(audit.close_reporter_context(
        proposal(), kGeneration, kDeadlineNs - 10));
    CHECK(audit.accepts_post_close_target(
        proposal(), kGeneration, kTarget, kDeadlineNs - 1));
    auto part = core.make_part(kTarget, proposal());
    const auto observed = audit.record_verified_post_close_target(
        proposal(),
        kGeneration,
        kTarget,
        core.get_config(),
        *part,
        kDeadlineNs - 1);
    REQUIRE(observed.has_value());
    CHECK(observed->phase ==
          ExperimentPostQcAuditTargetPhase::post_close);
    CHECK(observed->signers == std::set<ReplicaID>{kReporter, kTarget});
    CHECK_FALSE(audit.consume_reporter_deadline(kDeadlineNs).has_value());
}

TEST_CASE("target arrival exactly at deadline is rejected")
{
    BlsTestCore core(31);
    ExperimentPostQcAudit audit(options(), kReporter);
    arm(audit, core);
    auto accumulated = aggregate(core, {kReporter, kTarget});
    CHECK_FALSE(audit.synchronize_reporter_accumulator(
        proposal(), kGeneration, *accumulated, kTarget, kDeadlineNs)
                    .has_value());
    CHECK_FALSE(audit.begin_target_verification(
        proposal(),
        kGeneration,
        kTarget,
        ExperimentPostQcAuditTargetPhase::open,
        kDeadlineNs));
    const auto claim = audit.consume_reporter_deadline(kDeadlineNs);
    REQUIRE(claim.has_value());
    CHECK(claim->signers == std::set<ReplicaID>{kReporter});
}

TEST_CASE("honest omission emits exactly one independently owned relay")
{
    BlsTestCore core(31);
    ExperimentPostQcAudit audit(options(), kReporter);
    arm(audit, core, {kReporter, 6, 7, 8, 9});
    CHECK_FALSE(audit.consume_reporter_deadline(kDeadlineNs - 1).has_value());
    const auto claim = audit.consume_reporter_deadline(kDeadlineNs);
    REQUIRE(claim.has_value());
    CHECK(claim->signers ==
          std::set<ReplicaID>{kReporter, 6, 7, 8, 9});
    CHECK_FALSE(audit.consume_reporter_deadline(kDeadlineNs + 1).has_value());
    CHECK(audit.complete_reporter_relay(true));
    CHECK_FALSE(audit.complete_reporter_relay(true));
    const auto diagnostics = audit.diagnostics();
    CHECK(diagnostics.reporter_relay_attempted);
    CHECK(diagnostics.reporter_relay_sent);
    CHECK(diagnostics.reporter_terminal);
}

TEST_CASE("forged missing claim emits exactly once with the target present")
{
    BlsTestCore core(31);
    ExperimentPostQcAudit audit(options(true), kReporter);
    arm(audit, core, {kReporter, kTarget, 6, 7, 8, 9});
    const auto claim = audit.consume_reporter_deadline(kDeadlineNs);
    REQUIRE(claim.has_value());
    CHECK(claim->signers ==
          std::set<ReplicaID>{kReporter, kTarget, 6, 7, 8, 9});
    CHECK_FALSE(audit.consume_reporter_deadline(kDeadlineNs).has_value());
    CHECK(audit.complete_reporter_relay(true));
}

TEST_CASE("child-first then local-part synchronization preserves exact snapshot")
{
    BlsTestCore core(31);
    ExperimentPostQcAudit audit(options(true), kReporter);
    arm(audit, core, {});
    auto children = aggregate(core, {kTarget, 6, 7, 8, 9});
    REQUIRE(audit.synchronize_reporter_accumulator(
        proposal(), kGeneration, *children, kTarget, kDeadlineNs - 2)
                .has_value());
    auto with_local = aggregate(
        core, {kReporter, kTarget, 6, 7, 8, 9});
    CHECK_FALSE(audit.synchronize_reporter_accumulator(
        proposal(), kGeneration, *with_local, kReporter, kDeadlineNs - 1)
                    .has_value());
    const auto claim = audit.consume_reporter_deadline(kDeadlineNs);
    REQUIRE(claim.has_value());
    CHECK(claim->signers ==
          std::set<ReplicaID>{kReporter, kTarget, 6, 7, 8, 9});
}

TEST_CASE("deadline during target verification fails closed as incomplete")
{
    BlsTestCore core(31);
    ExperimentPostQcAudit audit(options(), kReporter);
    arm(audit, core);
    REQUIRE(audit.begin_target_verification(
        proposal(),
        kGeneration,
        kTarget,
        ExperimentPostQcAuditTargetPhase::open,
        kDeadlineNs - 1));
    CHECK_FALSE(audit.consume_reporter_deadline(kDeadlineNs).has_value());
    const auto diagnostics = audit.diagnostics();
    CHECK(diagnostics.reporter_incomplete);
    CHECK(diagnostics.reporter_terminal);
    auto part = core.make_part(kTarget, proposal());
    CHECK_FALSE(audit.complete_target_verification(
        proposal(),
        kGeneration,
        kTarget,
        core.get_config(),
        *part,
        true).has_value());
    CHECK_FALSE(audit.consume_reporter_deadline(kDeadlineNs + 1).has_value());
}

TEST_CASE("root activates only after terminal QC publication")
{
    BlsTestCore core(31);
    ExperimentPostQcAudit root(options(), kRoot);
    auto qc = aggregate(core, {kRoot, 1, 2});
    const auto prepared = root.prepare_root(
        proposal(), kGeneration, root_tree(), *qc, 3, kPreparedNs);
    REQUIRE(prepared.has_value());
    CHECK_FALSE(root.activate_root(
        proposal(), kGeneration, *qc, kPublishedNs, false));
    CHECK_FALSE(root.activate_root(
        proposal(), kGeneration, *qc, kPreparedNs - 1, true));
    CHECK(root.activate_root(
        proposal(), kGeneration, *qc, kPublishedNs, true));
    CHECK_FALSE(root.activate_root(
        proposal(), kGeneration, *qc, kPublishedNs + 1, true));
}

TEST_CASE("root preparation requires a disjoint in-tree frozen quorum")
{
    BlsTestCore core(31);
    ExperimentPostQcAudit root(options(), kRoot);
    auto tree = root_tree();

    SECTION("below frozen quorum")
    {
        auto qc = aggregate(core, {kRoot, 1});
        CHECK_FALSE(root.prepare_root(
            proposal(), kGeneration, tree, *qc, 3, kPreparedNs)
                        .has_value());
    }
    SECTION("overlaps reporter subtree")
    {
        auto qc = aggregate(core, {kRoot, kReporter, 1});
        CHECK_FALSE(root.prepare_root(
            proposal(), kGeneration, tree, *qc, 3, kPreparedNs)
                        .has_value());
    }
    SECTION("contains signer outside exact root membership")
    {
        tree.assigned_subtree.erase(
            std::remove(
                tree.assigned_subtree.begin(),
                tree.assigned_subtree.end(),
                ReplicaID{2}),
            tree.assigned_subtree.end());
        auto qc = aggregate(core, {kRoot, 1, 2});
        CHECK_FALSE(root.prepare_root(
            proposal(), kGeneration, tree, *qc, 3, kPreparedNs)
                        .has_value());
    }
    SECTION("exact quorum boundary is accepted")
    {
        auto qc = aggregate(core, {kRoot, 1, 2});
        CHECK(root.prepare_root(
            proposal(), kGeneration, tree, *qc, 3, kPreparedNs)
                  .has_value());
    }
}

TEST_CASE("N31 1-5-25 root geometry anchors complement-Q25 evidence")
{
    BlsTestCore core(31);
    ExperimentPostQcAudit root(options(), kRoot);
    auto tree = root_tree();
    const auto complement = n31_reporter_complement();
    REQUIRE(complement.size() == 25);
    REQUIRE(tree.direct_children ==
            std::vector<ReplicaID>{kReporter, 1, 2, 3, 4});
    REQUIRE(tree.fanout == 5);
    REQUIRE(tree.pipeline_stretch == 2);

    SECTION("exact complement Q25")
    {
        auto qc = aggregate(core, complement);
        const auto prepared = root.prepare_root(
            proposal(), kGeneration, tree, *qc, 21, kPreparedNs);
        REQUIRE(prepared.has_value());
        CHECK(prepared->qc_signers.size() == 25);
        CHECK(prepared->reporter_subtree ==
              std::set<ReplicaID>{kReporter, kTarget, 6, 7, 8, 9});
    }
    SECTION("Q20 below frozen quorum")
    {
        auto q20 = complement;
        q20.resize(20);
        auto qc = aggregate(core, q20);
        CHECK_FALSE(root.prepare_root(
            proposal(), kGeneration, tree, *qc, 21, kPreparedNs)
                        .has_value());
    }
    SECTION("Q25 with reporter overlap")
    {
        auto overlapping = complement;
        overlapping.back() = kReporter;
        auto qc = aggregate(core, overlapping);
        CHECK_FALSE(root.prepare_root(
            proposal(), kGeneration, tree, *qc, 21, kPreparedNs)
                        .has_value());
    }
    SECTION("Q25 contains signer outside frozen root membership")
    {
        tree.assigned_subtree.erase(
            std::remove(
                tree.assigned_subtree.begin(),
                tree.assigned_subtree.end(),
                ReplicaID{29}),
            tree.assigned_subtree.end());
        auto qc = aggregate(core, complement);
        CHECK_FALSE(root.prepare_root(
            proposal(), kGeneration, tree, *qc, 21, kPreparedNs)
                        .has_value());
    }
}

TEST_CASE("sham snapshot rejects changed published QC bytes")
{
    BlsTestCore core(31);
    ExperimentPostQcAudit root(options(false), kRoot);
    auto frozen_qc = aggregate(core, {kRoot, 1, 2});
    REQUIRE(root.prepare_root(
        proposal(),
        kGeneration,
        root_tree(),
        *frozen_qc,
        3,
        kPreparedNs).has_value());
    auto changed_qc = aggregate(core, {kRoot, 1, 3});
    CHECK_FALSE(root.activate_root(
        proposal(), kGeneration, *changed_qc, kPublishedNs, true));
    const auto diagnostics = root.diagnostics();
    CHECK_FALSE(diagnostics.root_active);
    CHECK(diagnostics.root_terminal);
}

TEST_CASE("root rejects publication outside reporter deadline chronology")
{
    BlsTestCore core(31);
    ExperimentPostQcAudit root(options(), kRoot);
    auto qc = aggregate(core, {kRoot, 1, 2});
    REQUIRE(root.prepare_root(
        proposal(),
        kGeneration,
        root_tree(),
        *qc,
        3,
        kPreparedNs).has_value());
    REQUIRE(root.activate_root(
        proposal(), kGeneration, *qc, kDeadlineNs, true));
    auto value = relay(core, {kReporter, 6, 7});
    CHECK_FALSE(root.begin_root_verification(
        value, kReporter, kDeadlineNs + 1).has_value());
}

TEST_CASE("root cheap gates reject wrong source subtree and identity")
{
    BlsTestCore core(31);
    ExperimentPostQcAudit root(options(), kRoot);
    prepare_and_activate_root(root, core);
    auto valid = relay(core, {kReporter, 6, 7});

    CHECK_FALSE(root.begin_root_verification(
        valid, ReplicaID{1}, kReceivedNs).has_value());

    auto outside = relay(core, {kReporter, 10});
    CHECK_FALSE(root.begin_root_verification(
        outside, kReporter, kReceivedNs).has_value());

    auto wrong_identity = valid;
    wrong_identity.diagnostic_window = "wrong-window";
    CHECK_FALSE(root.begin_root_verification(
        wrong_identity, kReporter, kReceivedNs).has_value());

    auto wrong_chronology = valid;
    wrong_chronology.armed_ns = kPreparedNs + 1;
    wrong_chronology.deadline_ns =
        wrong_chronology.armed_ns +
        kExperimentPostQcAuditDeadlineMs * 1'000'000;
    wrong_chronology.emitted_ns = wrong_chronology.deadline_ns;
    CHECK_FALSE(root.begin_root_verification(
        wrong_chronology,
        kReporter,
        wrong_chronology.emitted_ns + 1).has_value());

    const auto request = root.begin_root_verification(
        valid, kReporter, kReceivedNs);
    REQUIRE(request.has_value());
    CHECK(request->audit_signers ==
          std::set<ReplicaID>{kReporter, 6, 7});
}

TEST_CASE("root deduplicates verification and failed verification is terminal")
{
    BlsTestCore core(31);
    ExperimentPostQcAudit root(options(), kRoot);
    prepare_and_activate_root(root, core);
    auto value = relay(core, {kReporter, 6, 7});
    REQUIRE(root.begin_root_verification(
        value, kReporter, kReceivedNs).has_value());
    CHECK_FALSE(root.begin_root_verification(
        value, kReporter, kReceivedNs + 1).has_value());
    CHECK_FALSE(root.complete_root_verification(
        proposal(), kGeneration, kVerifiedNs, false, true, true));
    const auto diagnostics = root.diagnostics();
    CHECK(diagnostics.root_verification_attempted);
    CHECK_FALSE(diagnostics.root_accepted);
    CHECK(diagnostics.root_terminal);
    CHECK_FALSE(root.begin_root_verification(
        value, kReporter, kReceivedNs + 2).has_value());
}

TEST_CASE("root accepts one independently verified terminal witness")
{
    BlsTestCore core(31);
    ExperimentPostQcAudit root(options(), kRoot);
    prepare_and_activate_root(root, core);
    auto value = relay(core, {kReporter, 6, 7});
    const auto request = root.begin_root_verification(
        value, kReporter, kReceivedNs);
    REQUIRE(request.has_value());
    REQUIRE(request->relay.certificate->verify(core.get_config()));
    CHECK(root.complete_root_verification(
        proposal(), kGeneration, kVerifiedNs, true, true, true));
    CHECK(root.diagnostics().root_accepted);
}

TEST_CASE("root completion enforces strict receive and retention boundaries")
{
    BlsTestCore core(31);
    ExperimentPostQcAudit root(options(), kRoot);
    prepare_and_activate_root(root, core);
    auto value = relay(core, {kReporter, 6, 7});
    REQUIRE(root.begin_root_verification(
        value, kReporter, kReceivedNs).has_value());

    SECTION("before authenticated receipt")
    {
        CHECK_FALSE(root.complete_root_verification(
            proposal(),
            kGeneration,
            kReceivedNs - 1,
            true,
            true,
            true));
    }
    SECTION("at authenticated receipt")
    {
        CHECK(root.complete_root_verification(
            proposal(),
            kGeneration,
            kReceivedNs,
            true,
            true,
            true));
    }
    SECTION("last nanosecond before expiry")
    {
        CHECK(root.complete_root_verification(
            proposal(),
            kGeneration,
            kExpiryNs - 1,
            true,
            true,
            true));
    }
    SECTION("exact expiry is rejected")
    {
        CHECK_FALSE(root.complete_root_verification(
            proposal(),
            kGeneration,
            kExpiryNs,
            true,
            true,
            true));
    }
    SECTION("after expiry is rejected")
    {
        CHECK_FALSE(root.complete_root_verification(
            proposal(),
            kGeneration,
            kExpiryNs + 1,
            true,
            true,
            true));
    }
    CHECK(root.diagnostics().root_terminal);
}

TEST_CASE("root derives contradiction only from verified signer presence")
{
    BlsTestCore core(31);
    // The root has no reporter-local forge switch. A target-present relay is
    // accepted cryptographically and classified only by downstream analysis.
    ExperimentPostQcAudit root(options(false), kRoot);
    prepare_and_activate_root(root, core);
    auto value = relay(core, {kReporter, kTarget, 6, 7});
    const auto request = root.begin_root_verification(
        value, kReporter, kReceivedNs);
    REQUIRE(request.has_value());
    CHECK(request->audit_signers.count(kTarget) == 1);
    REQUIRE(request->relay.certificate->verify(core.get_config()));
    CHECK(root.complete_root_verification(
        proposal(), kGeneration, kVerifiedNs, true, true, true));
}

TEST_CASE("root retention expires at the frozen publication-relative bound")
{
    BlsTestCore core(31);
    ExperimentPostQcAudit root(options(), kRoot);
    prepare_and_activate_root(root, core);
    CHECK_FALSE(root.expire(kExpiryNs - 1));
    CHECK(root.expire(kExpiryNs));
    auto value = relay(core, {kReporter, 6, 7});
    CHECK_FALSE(root.begin_root_verification(
        value, kReporter, kExpiryNs).has_value());
}

TEST_CASE("dedicated PQAR wire round trips under strict bounds")
{
    BlsTestCore core(31);
    auto value = relay(core, {kReporter, 6, 7});
    DataStream wire;
    value.serialize(wire);
    REQUIRE(wire.size() <= kExperimentPostQcAuditMaximumWireBytes);
    ExperimentPostQcAuditRelay parsed;
    REQUIRE(parsed.parse(wire, &core));
    CHECK(wire.size() == 0);
    CHECK(parsed.proposal == value.proposal);
    CHECK(parsed.generation == value.generation);
    CHECK(parsed.diagnostic_window == value.diagnostic_window);
    CHECK(signers(*parsed.certificate) ==
          std::set<ReplicaID>{kReporter, 6, 7});
}

TEST_CASE("dedicated PQAR wire rejects malformed boundedness cases")
{
    BlsTestCore core(31);
    auto value = relay(core, {kReporter, 6, 7});
    DataStream serialized;
    value.serialize(serialized);
    const auto valid = static_cast<bytearray_t>(serialized);

    SECTION("null parser")
    {
        DataStream input(valid);
        ExperimentPostQcAuditRelay parsed;
        CHECK_FALSE(parsed.parse(input, nullptr));
    }
    SECTION("truncated bitmap")
    {
        auto bytes = valid;
        bytes.resize(certificate_bitmap_offset(value) + 2);
        DataStream input(std::move(bytes));
        ExperimentPostQcAuditRelay parsed;
        CHECK_FALSE(parsed.parse(input, &core));
    }
    SECTION("oversized bitmap claim")
    {
        auto bytes = valid;
        const auto offset = certificate_bitmap_offset(value);
        DataStream encoded;
        encoded << htole(std::uint32_t{4096});
        const auto replacement = static_cast<bytearray_t>(encoded);
        REQUIRE(offset + replacement.size() <= bytes.size());
        std::copy(replacement.begin(), replacement.end(),
                  bytes.begin() + offset);
        DataStream input(std::move(bytes));
        ExperimentPostQcAuditRelay parsed;
        CHECK_FALSE(parsed.parse(input, &core));
    }
    SECTION("trailing byte")
    {
        auto bytes = valid;
        bytes.push_back(0);
        DataStream input(std::move(bytes));
        ExperimentPostQcAuditRelay parsed;
        CHECK_FALSE(parsed.parse(input, &core));
    }
    SECTION("maximum total payload")
    {
        bytearray_t bytes(kExperimentPostQcAuditMaximumWireBytes + 1, 0);
        DataStream input(std::move(bytes));
        ExperimentPostQcAuditRelay parsed;
        CHECK_FALSE(parsed.parse(input, &core));
    }
}

TEST_CASE("runtime initializes before arming and synchronizes every mutation")
{
    const auto source = read_source("src/hotstuff.cpp");
    const auto admission = function_body(
        source, "HotStuffBase::admit_exact_context(");
    CHECK(admission.find("initialize_accumulator(") != std::string::npos);
    CHECK(admission.find("return lease;") >
          admission.find("initialize_accumulator("));

    const auto remote_admit = source.find("owner.admit_exact_context(");
    const auto receive = source.find("owner.on_receive_proposal(", remote_admit);
    const auto deadline = source.find(
        "owner.start_latency_deadline(", receive);
    REQUIRE(remote_admit != std::string::npos);
    REQUIRE(receive != std::string::npos);
    REQUIRE(deadline != std::string::npos);
    CHECK(remote_admit < receive);
    CHECK(receive < deadline);

    const auto arm_body = function_body(
        source, "void HotStuffBase::arm_experiment_post_qc_audit(");
    CHECK(arm_body.find("clone_accumulator(") <
          arm_body.find("arm_reporter("));

    const auto apply_local = function_body(
        source, "void HotStuffBase::apply_local_vote(");
    CHECK(apply_local.find("record_local_part(") <
          apply_local.find("synchronize_experiment_post_qc_audit("));

    const auto do_vote = function_body(
        source, "void HotStuffBase::do_vote(");
    CHECK(do_vote.find("record_local_part(") <
          do_vote.find("synchronize_experiment_post_qc_audit("));

    const auto accepted = function_body(
        source, "void HotStuffBase::continue_exact_contribution(");
    CHECK(accepted.find("synchronize_experiment_post_qc_audit(") !=
          std::string::npos);
}

TEST_CASE("dedicated path stays isolated from consensus and MsgRelay")
{
    const auto header = read_source("include/hotstuff/hotstuff.h");
    const auto source = read_source("src/hotstuff.cpp");
    const auto component = read_source(
        "src/experiment_post_qc_audit.cpp");
    const auto handler = function_body(
        source,
        "void HotStuffBase::experiment_post_qc_audit_relay_handler(");
    const auto preparation = function_body(
        source,
        "void HotStuffBase::prepare_experiment_post_qc_audit_root(");
    const auto activation = function_body(
        source,
        "void HotStuffBase::activate_experiment_post_qc_audit_root(");
    const auto snapshot_marker = function_body(
        source,
        "void HotStuffBase::emit_experiment_post_qc_audit_root_snapshot(");

    CHECK(header.find("MsgExperimentPostQcAuditRelay") !=
          std::string::npos);
    CHECK(header.find("opcode = 0x1F") != std::string::npos);
    CHECK(component.find("MsgRelay") == std::string::npos);
    CHECK(handler.find("MsgRelay") == std::string::npos);
    CHECK(handler.find("certificate->verify(config, vpool)") !=
          std::string::npos);
    CHECK(handler.find("complete_root_verification(") !=
          std::string::npos);
    CHECK(preparation.find("frozen_global_quorum(lease)") !=
          std::string::npos);
    CHECK(activation.find("block->self_qc == nullptr") <
          activation.find("->activate_root("));
    CHECK(activation.find("*block->self_qc") !=
          std::string::npos);
    CHECK(snapshot_marker.find("qc_unchanged=1") !=
          std::string::npos);
    CHECK(handler.find("verified_ns") <
          handler.find("complete_root_verification("));
    CHECK(source.find(
              "KAURI_AUDIT missing_claim claim=missing_target") !=
          std::string::npos);
    CHECK(source.find("KAURI_AUDIT missing_claim kind=") ==
          std::string::npos);
    CHECK(source.find("qc_fingerprint=%s consensus_context=terminal") !=
          std::string::npos);
    CHECK(component.find("ExperimentPostQcAuditClaimKind") ==
          std::string::npos);
    CHECK(handler.find("manager") == std::string::npos);
    CHECK(handler.find("reputation") == std::string::npos);
    CHECK(handler.find("adaptive_epoch_runtime") == std::string::npos);
    CHECK(handler.find("experiment_false_timeout") == std::string::npos);
    CHECK(handler.find("record_timeout") == std::string::npos);
}

TEST_CASE("post-QC audit marker tokens stay parseable")
{
    CHECK(std::string(to_string(
              ExperimentPostQcAuditTargetPhase::open)) == "open");
    CHECK(std::string(to_string(
              ExperimentPostQcAuditTargetPhase::post_close)) ==
          "post_close");
}

} // namespace
