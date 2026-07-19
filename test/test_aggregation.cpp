#include <chrono>
#include <cstddef>
#include <cstdint>
#include <functional>
#include <map>
#include <optional>
#include <set>
#include <string>
#include <type_traits>
#include <utility>
#include <vector>

#include "catch.hpp"
#include "hotstuff/proposal_context.h"
#include "support/bls_fixtures.h"
#include "support/fake_clock.h"

/*
 * A06 aggregation-timeout contract.
 *
 * ProposalContextLifecycle remains the only owner of exact proposal state,
 * verified contributions, immutable tree membership, timer generation, and
 * pass-through state.  The new coordinator is deliberately state-free: it
 * schedules one lifecycle-owned deadline and applies timeout-only policy to a
 * computed, already-verified accumulator supplied by the caller.
 *
 * The fallback mirrors that narrow API so this tests-first commit compiles
 * before aggregation.h exists.  It intentionally implements no behavior.
 */
#if __has_include("hotstuff/aggregation.h")
#include "hotstuff/aggregation.h"
#define KAURI_HAS_AGGREGATION_TIMEOUT 1
#else
#define KAURI_HAS_AGGREGATION_TIMEOUT 0

namespace hotstuff
{

struct ProposalForwardingClaim
{
    quorum_cert_bt certificate;
    std::set<ReplicaID> signers;
};

class AggregationScheduler
{
public:
    using Duration = std::chrono::nanoseconds;
    using Callback = std::function<void()>;
    using Cancellation = std::function<void()>;

    virtual ~AggregationScheduler() = default;
    virtual Cancellation schedule_after(Duration, Callback) = 0;
};

struct AggregationTimeoutEffects
{
    std::function<void(const ProposalContextLease &,
                       ProposalForwardingClaim)>
        send_upward;
    std::function<void(const ProposalContextLease &,
                       const std::set<ReplicaID> &)>
        record_timeout;
};

class AggregationTimeoutPolicy
{
public:
    using Duration = AggregationScheduler::Duration;

    explicit AggregationTimeoutPolicy(Duration) {}

    Duration timeout_for(std::uint32_t, std::uint32_t) const
    {
        return Duration::zero();
    }
};

AggregationTimeoutPolicy::Duration exact_fallback_recovery_horizon(
    AggregationTimeoutPolicy::Duration)
{
    return AggregationTimeoutPolicy::Duration::zero();
}

class AggregationTimeoutCoordinator
{
public:
    using VerifiedCandidateProvider =
        std::function<quorum_cert_bt(const ProposalContextLease &)>;

    AggregationTimeoutCoordinator(ProposalContextLifecycle &,
                                  AggregationTimeoutPolicy,
                                  AggregationTimeoutEffects,
                                  VerifiedCandidateProvider)
    {}

    std::uint64_t arm_timeout(const ProposalContextLease &,
                              AggregationScheduler &,
                              std::uint32_t,
                              std::uint32_t)
    {
        return 0;
    }

    bool dispatch_timeout(const ProposalKey &, std::uint64_t)
    {
        return false;
    }
};

} // namespace hotstuff
#endif

using hotstuff::AggregationScheduler;
using hotstuff::AggregationTimeoutCoordinator;
using hotstuff::AggregationTimeoutEffects;
using hotstuff::AggregationTimeoutPolicy;
using hotstuff::exact_fallback_recovery_horizon;
using hotstuff::ConfigurationId;
using hotstuff::ProposalForwardingClaim;
using hotstuff::ProposalContextEvent;
using hotstuff::ProposalContextLease;
using hotstuff::ProposalContextLifecycle;
using hotstuff::ProposalContextMetadata;
using hotstuff::ProposalContextStatus;
using hotstuff::ProposalKey;
using hotstuff::ProposalTreeSnapshot;
using hotstuff::QuorumCert;
using hotstuff::QuorumCertAggBLS;
using hotstuff::ReplicaID;
using hotstuff::quorum_cert_bt;
using hotstuff::test::BlsTestCore;
using hotstuff::test::DeterministicScheduler;
using hotstuff::test::FakeClock;
using hotstuff::test::add_valid_signers;
using hotstuff::test::make_digest;
using hotstuff::test::make_test_proposal_key;

namespace
{

using Duration = AggregationScheduler::Duration;
using ChildSubtrees = std::map<ReplicaID, std::set<ReplicaID>>;

template<typename Effects, typename = void>
struct has_signing_effect : std::false_type
{};

template<typename Effects>
struct has_signing_effect<
    Effects,
    std::void_t<decltype(std::declval<Effects &>().sign_local)>>
    : std::true_type
{};

template<typename Effects, typename = void>
struct has_leader_rotation_effect : std::false_type
{};

template<typename Effects>
struct has_leader_rotation_effect<
    Effects,
    std::void_t<decltype(std::declval<Effects &>().rotate_leader)>>
    : std::true_type
{};

template<typename Lifecycle, typename = void>
struct has_atomic_forwarding_claim : std::false_type
{};

template<typename Lifecycle>
struct has_atomic_forwarding_claim<
    Lifecycle,
    std::void_t<decltype(
        std::declval<Lifecycle &>().claim_unforwarded_certificate(
            std::declval<const ProposalContextLease &>(),
            std::declval<quorum_cert_bt>()))>> : std::true_type
{};

template<typename Lifecycle>
std::optional<ProposalForwardingClaim> claim_unforwarded(
    Lifecycle &contexts,
    const ProposalContextLease &lease,
    quorum_cert_bt candidate)
{
    if constexpr (has_atomic_forwarding_claim<Lifecycle>::value)
        return contexts.claim_unforwarded_certificate(
            lease, std::move(candidate));
    else
        return std::nullopt;
}

ProposalTreeSnapshot make_tree(
    ReplicaID local_replica,
    ReplicaID root,
    std::optional<ReplicaID> parent,
    std::vector<ReplicaID> direct_children,
    std::vector<ReplicaID> assigned_subtree,
    ChildSubtrees child_subtrees)
{
    ProposalTreeSnapshot tree;
    tree.local_replica = local_replica;
    tree.root = root;
    tree.parent = parent;
    tree.direct_children = std::move(direct_children);
    tree.assigned_subtree = std::move(assigned_subtree);
    tree.child_subtrees = std::move(child_subtrees);
    tree.fanout = 2;
    tree.pipeline_stretch = 2;
    return tree;
}

ProposalTreeSnapshot internal_tree()
{
    return make_tree(
        1, 0, 0, {3, 4}, {1, 3, 4}, {{3, {3}}, {4, {4}}});
}

ProposalTreeSnapshot wait_exempt_internal_tree()
{
    auto tree = internal_tree();
    tree.required_subtree = {1, 3};
    tree.optional_subtree = {4};
    tree.required_child_subtrees = {{3, {3}}, {4, {}}};
    return tree;
}

ProposalTreeSnapshot wide_internal_tree()
{
    return make_tree(
        1,
        0,
        0,
        {3, 4},
        {1, 3, 4, 5, 6},
        {{3, {3}}, {4, {4, 5, 6}}});
}

ProposalTreeSnapshot leaf_tree()
{
    return make_tree(3, 0, 1, {}, {3}, {});
}

ProposalTreeSnapshot root_tree()
{
    return make_tree(
        0,
        0,
        std::nullopt,
        {1, 2},
        {0, 1, 2, 3, 4, 5, 6},
        {{1, {1, 3, 4}}, {2, {2, 5, 6}}});
}

ProposalContextMetadata metadata(const ProposalKey &key,
                                 ProposalTreeSnapshot tree)
{
    return ProposalContextMetadata{key, std::move(tree), 5};
}

std::set<ReplicaID> signer_set(const QuorumCert &certificate)
{
    const auto listed = certificate.get_signers();
    return std::set<ReplicaID>(listed.begin(), listed.end());
}

quorum_cert_bt aggregate(BlsTestCore &core,
                         const ProposalKey &key,
                         const std::vector<ReplicaID> &signers)
{
    auto certificate = quorum_cert_bt(
        new QuorumCertAggBLS(core.get_config(), key));
    auto &bls = dynamic_cast<QuorumCertAggBLS &>(*certificate);
    add_valid_signers(bls, core, signers, key);
    certificate->compute();
    REQUIRE(certificate->verify(core.get_config()));
    return certificate;
}

class FakeAggregationScheduler final : public AggregationScheduler
{
public:
    FakeAggregationScheduler(): scheduler_(clock_) {}

    Cancellation schedule_after(Duration delay, Callback callback) override
    {
        const auto key = "aggregation-" + std::to_string(++next_key_);
        scheduler_.schedule(
            key, clock_.now() + delay, std::move(callback));
        return [this, key]() { static_cast<void>(scheduler_.cancel(key)); };
    }

    void advance_by(Duration duration)
    {
        scheduler_.advance_by(duration);
    }

    std::size_t pending() const noexcept
    {
        return scheduler_.pending();
    }

private:
    FakeClock clock_;
    DeterministicScheduler scheduler_;
    std::uint64_t next_key_{0};
};

struct SigningSpy
{
    explicit SigningSpy(BlsTestCore &core): core(core) {}

    hotstuff::part_cert_bt sign(const ProposalKey &key, ReplicaID signer)
    {
        ++calls;
        return core.make_part(signer, key);
    }

    BlsTestCore &core;
    std::size_t calls{0};
};

struct TransportSpy
{
    explicit TransportSpy(BlsTestCore &core): core(core) {}

    void send(const ProposalContextLease &lease,
              ProposalForwardingClaim claim)
    {
        if (claim.certificate == nullptr ||
            claim.certificate->get_proposal_key() != lease.key() ||
            claim.signers != signer_set(*claim.certificate))
        {
            valid.push_back(false);
            return;
        }
        claim.certificate->compute();
        valid.push_back(claim.certificate->verify(core.get_config()));
        signer_sets.push_back(std::move(claim.signers));
    }

    BlsTestCore &core;
    std::vector<std::set<ReplicaID>> signer_sets;
    std::vector<bool> valid;
};

struct PacemakerSpy
{
    void rotate(const ConfigurationId &configuration)
    {
        rotations.push_back(configuration);
    }

    std::vector<ConfigurationId> rotations;
};

struct TimeoutSpy
{
    void record(const ProposalContextLease &lease,
                const std::set<ReplicaID> &missing)
    {
        keys.push_back(lease.key());
        missing_children.push_back(missing);
    }

    std::vector<ProposalKey> keys;
    std::vector<std::set<ReplicaID>> missing_children;
};

struct Harness
{
    explicit Harness(ReplicaID local_replica)
        : core(7, local_replica),
          scheduler(),
          contexts(),
          signing(core),
          transport(core),
          pacemaker(),
          timeouts(),
          policy(std::chrono::milliseconds(10)),
          effects(make_effects()),
          coordinator(
              contexts,
              policy,
              effects,
              [this](const ProposalContextLease &lease) {
                  ++candidate_requests;
                  auto candidate = contexts.clone_accumulator(lease);
                  if (candidate == nullptr)
                      return quorum_cert_bt();
                  candidate->compute();
                  if (!candidate->verify(core.get_config()))
                      return quorum_cert_bt();
                  return candidate;
              })
    {}

    AggregationTimeoutEffects make_effects()
    {
        AggregationTimeoutEffects value;
        value.send_upward = [this](
                                  const ProposalContextLease &lease,
                                  ProposalForwardingClaim claim) {
            transport.send(lease, std::move(claim));
        };
        value.record_timeout = [this](
                                   const ProposalContextLease &lease,
                                   const std::set<ReplicaID> &missing) {
            timeouts.record(lease, missing);
        };
        value.record_optional_absence = [this](
                                            const ProposalContextLease &,
                                            const std::set<ReplicaID> &missing) {
            optional_absence.push_back(missing);
        };
        return value;
    }

    ProposalContextLease admit(const ProposalKey &key,
                               ProposalTreeSnapshot tree)
    {
        auto lease = contexts.admit_remote(metadata(key, std::move(tree)));
        REQUIRE(lease.has_value());
        REQUIRE(contexts.initialize_accumulator(
            *lease, core.create_quorum_cert(key)));
        return *lease;
    }

    void send_claim(const ProposalContextLease &lease,
                    std::optional<ProposalForwardingClaim> claim)
    {
        REQUIRE(claim.has_value());
        transport.send(lease, std::move(*claim));
    }

    BlsTestCore core;
    // Scheduler precedes contexts so lifecycle cancellation remains safe.
    FakeAggregationScheduler scheduler;
    ProposalContextLifecycle contexts;
    SigningSpy signing;
    TransportSpy transport;
    PacemakerSpy pacemaker;
    TimeoutSpy timeouts;
    AggregationTimeoutPolicy policy;
    AggregationTimeoutEffects effects;
    AggregationTimeoutCoordinator coordinator;
    std::size_t candidate_requests{0};
    std::vector<std::set<ReplicaID>> optional_absence;
};

constexpr auto test_delay = std::chrono::milliseconds(25);
constexpr std::uint32_t internal_level = 1;
constexpr std::uint32_t maximum_level = 3;

} // namespace

TEST_CASE("A06 deterministic scheduler runs and cancels at exact deadlines",
          "[a06][aggregation][control][clock]")
{
    FakeAggregationScheduler scheduler;
    std::size_t calls = 0;
    auto cancel = scheduler.schedule_after(
        test_delay, [&calls]() { ++calls; });
    REQUIRE(scheduler.pending() == 1);
    scheduler.advance_by(test_delay - std::chrono::nanoseconds(1));
    CHECK(calls == 0);
    scheduler.advance_by(std::chrono::nanoseconds(1));
    CHECK(calls == 1);
    CHECK(scheduler.pending() == 0);

    cancel = scheduler.schedule_after(
        test_delay, [&calls]() { ++calls; });
    REQUIRE(scheduler.pending() == 1);
    cancel();
    scheduler.advance_by(test_delay);
    CHECK(calls == 1);
    CHECK(scheduler.pending() == 0);
}

TEST_CASE("A06 lifecycle timer generations are exact-key isolated",
          "[a06][aggregation][control][generation][configuration]")
{
    ProposalContextLifecycle contexts;
    const auto block = make_digest(0xa0);
    const auto key_a = make_test_proposal_key(block, 0xa1, 10, 1);
    const auto key_b = make_test_proposal_key(block, 0xa2, 11, 1);
    auto lease_a = contexts.admit_remote(metadata(key_a, internal_tree()));
    auto lease_b = contexts.admit_remote(metadata(key_b, internal_tree()));
    REQUIRE(lease_a.has_value());
    REQUIRE(lease_b.has_value());

    const auto generation_a = contexts.arm_timer(*lease_a);
    const auto generation_b = contexts.arm_timer(*lease_b);
    REQUIRE(generation_a != 0);
    REQUIRE(generation_b != 0);
    REQUIRE(generation_a != generation_b);
    std::size_t callbacks = 0;
    REQUIRE(contexts.dispatch_timer(
        key_a,
        generation_a,
        [&callbacks](const ProposalContextLease &) { ++callbacks; }));
    CHECK_FALSE(contexts.dispatch_timer(
        key_a,
        generation_b,
        [&callbacks](const ProposalContextLease &) { ++callbacks; }));
    CHECK(contexts.snapshot(key_b)->timer_generation == generation_b);
    CHECK(callbacks == 1);
}

TEST_CASE("timeout effects structurally exclude signing and leader rotation",
          "[a06][aggregation][timeout][separation][control]")
{
    CHECK_FALSE(has_signing_effect<AggregationTimeoutEffects>::value);
    CHECK_FALSE(has_leader_rotation_effect<AggregationTimeoutEffects>::value);
}

TEST_CASE("aggregation timeout policy is positive and level aware",
          "[a06][aggregation][timeout][policy][steady-clock]"
          "[intentional-red]")
{
    AggregationTimeoutPolicy policy(std::chrono::milliseconds(10));
    const auto deepest = policy.timeout_for(3, 3);
    REQUIRE(deepest > Duration::zero());
    const auto middle = policy.timeout_for(2, 3);
    const auto root = policy.timeout_for(0, 3);
    CHECK(middle > deepest);
    CHECK(root > middle);
    CHECK(deepest == std::chrono::milliseconds(10));
    CHECK(middle == std::chrono::milliseconds(20));
    CHECK(root == std::chrono::milliseconds(40));
}

TEST_CASE("two-stage fallback horizon must precede independent suspicion",
          "[fallback][aggregation][leader-progress][boundary]")
{
    const auto maximum_deadline = std::chrono::milliseconds(1500);
    CHECK(exact_fallback_recovery_horizon(maximum_deadline) ==
          std::chrono::seconds(3));
    REQUIRE_THROWS_AS(
        exact_fallback_recovery_horizon(Duration::zero()),
        std::invalid_argument);
    REQUIRE_THROWS_AS(
        exact_fallback_recovery_horizon(Duration::max()),
        std::overflow_error);

    const auto grace = std::chrono::seconds(1);
    const auto leader_progress_timeout = std::chrono::seconds(5);
    CHECK(exact_fallback_recovery_horizon(maximum_deadline) <
          grace + leader_progress_timeout);
    CHECK_FALSE(exact_fallback_recovery_horizon(
                    std::chrono::seconds(3)) <
                grace + leader_progress_timeout);
}

TEST_CASE("leaf contexts never arm aggregation timeouts",
          "[a06][aggregation][timeout][leaf][intentional-red]")
{
    Harness harness(3);
    const auto key = make_test_proposal_key(
        make_digest(0xaf), 0xb0, 19, 1);
    const auto lease = harness.admit(key, leaf_tree());
    REQUIRE(lease.tree().parent.has_value());
    REQUIRE(lease.tree().direct_children.empty());

    const auto generation = harness.coordinator.arm_timeout(
        lease,
        harness.scheduler,
        maximum_level,
        maximum_level);

    CHECK(generation == 0);
    CHECK(harness.scheduler.pending() == 0);
    const auto before = harness.contexts.snapshot(key);
    REQUIRE(before.has_value());
    CHECK(before->timer_generation == 0);
    CHECK_FALSE(before->pass_through);

    harness.scheduler.advance_by(
        harness.policy.timeout_for(maximum_level, maximum_level));

    CHECK(harness.candidate_requests == 0);
    CHECK(harness.transport.signer_sets.empty());
    CHECK(harness.timeouts.keys.empty());
    const auto after = harness.contexts.snapshot(key);
    REQUIRE(after.has_value());
    CHECK(after->timer_generation == 0);
    CHECK_FALSE(after->pass_through);
}

TEST_CASE("zero-signature timeout sends and signs nothing but enters pass-through",
          "[a06][aggregation][timeout][zero-signature][intentional-red]")
{
    Harness harness(1);
    const auto key = make_test_proposal_key(
        make_digest(0xb0), 0xb1, 20, 1);
    const auto lease = harness.admit(key, internal_tree());

    REQUIRE(harness.coordinator.arm_timeout(
                lease,
                harness.scheduler,
                internal_level,
                maximum_level) != 0);
    harness.scheduler.advance_by(
        harness.policy.timeout_for(internal_level, maximum_level));

    CHECK(harness.signing.calls == 0);
    CHECK(harness.pacemaker.rotations.empty());
    CHECK(harness.transport.signer_sets.empty());
    REQUIRE(harness.timeouts.missing_children.size() == 1);
    CHECK(harness.timeouts.missing_children.front() ==
          std::set<ReplicaID>{3, 4});
    CHECK(harness.contexts.context_status(key) ==
          ProposalContextStatus::admitted_open);
    const auto snapshot = harness.contexts.snapshot(key);
    REQUIRE(snapshot.has_value());
    CHECK(snapshot->pass_through);
    CHECK(snapshot->verified_signers.empty());
}

TEST_CASE("partial timeout claims only verified unforwarded descendants",
          "[a06][aggregation][timeout][partial][non-voting]"
          "[intentional-red]")
{
    Harness harness(1);
    const auto key = make_test_proposal_key(
        make_digest(0xb2), 0xb3, 21, 1);
    const auto lease = harness.admit(key, internal_tree());
    auto child = harness.core.make_part(3, key);
    REQUIRE(harness.contexts.record_verified_direct_part(
        lease, harness.core.get_config(), 3, 3, *child));

    REQUIRE(harness.coordinator.arm_timeout(
                lease,
                harness.scheduler,
                internal_level,
                maximum_level) != 0);
    harness.scheduler.advance_by(
        harness.policy.timeout_for(internal_level, maximum_level));

    CHECK(harness.signing.calls == 0);
    CHECK(harness.pacemaker.rotations.empty());
    REQUIRE(harness.transport.signer_sets.size() == 1);
    CHECK(harness.transport.signer_sets.front() ==
          std::set<ReplicaID>{3});
    CHECK(harness.transport.valid == std::vector<bool>{true});
    const auto snapshot = harness.contexts.snapshot(key);
    REQUIRE(snapshot.has_value());
    CHECK(snapshot->pass_through);
    CHECK(snapshot->forwarded_signers == std::set<ReplicaID>{3});
    CHECK(harness.contexts.context_status(key) ==
          ProposalContextStatus::admitted_open);
}

TEST_CASE("missing upward effect preserves partial forwarding ownership",
          "[a06][aggregation][timeout][partial][no-transport][control]")
{
    Harness harness(1);
    const auto key = make_test_proposal_key(
        make_digest(0xb3), 0xb4, 121, 1);
    const auto lease = harness.admit(key, internal_tree());
    auto child = harness.core.make_part(3, key);
    REQUIRE(harness.contexts.record_verified_direct_part(
        lease, harness.core.get_config(), 3, 3, *child));

    auto effects = harness.make_effects();
    effects.send_upward = {};
    REQUIRE_FALSE(static_cast<bool>(effects.send_upward));
    std::size_t provider_calls = 0;
    AggregationTimeoutCoordinator coordinator(
        harness.contexts,
        harness.policy,
        std::move(effects),
        [&harness, &provider_calls](const ProposalContextLease &candidate_lease) {
            ++provider_calls;
            return harness.contexts.clone_accumulator(candidate_lease);
        });

    REQUIRE(coordinator.arm_timeout(
                lease,
                harness.scheduler,
                internal_level,
                maximum_level) != 0);
    harness.scheduler.advance_by(
        harness.policy.timeout_for(internal_level, maximum_level));

    CHECK(provider_calls == 0);
    CHECK(harness.transport.signer_sets.empty());
    REQUIRE(harness.timeouts.missing_children.size() == 1);
    CHECK(harness.timeouts.missing_children.front() ==
          std::set<ReplicaID>{4});
    const auto snapshot = harness.contexts.snapshot(key);
    REQUIRE(snapshot.has_value());
    CHECK(snapshot->verified_signers == std::set<ReplicaID>{3});
    CHECK(snapshot->forwarded_signers.empty());
    CHECK(snapshot->pass_through);
}

TEST_CASE("pre-timeout completion waits for immutable assigned subtree",
          "[a06][aggregation][quorum-independence][reputation]"
          "[intentional-red]")
{
    Harness harness(1);
    const auto key = make_test_proposal_key(
        make_digest(0xb4), 0xb5, 22, 1);
    auto staging_tree = internal_tree();
    const auto lease = harness.admit(key, staging_tree);

    // Mutating future ranking/staging input cannot alter the admitted tree.
    staging_tree.assigned_subtree = {1, 3};
    staging_tree.direct_children = {3};
    staging_tree.child_subtrees.erase(4);

    auto local = harness.signing.sign(key, 1);
    REQUIRE(harness.contexts.record_local_part(
        lease, harness.core.get_config(), 1, *local));
    auto child_three = harness.core.make_part(3, key);
    REQUIRE(harness.contexts.record_verified_direct_part(
        lease, harness.core.get_config(), 3, 3, *child_three));
    CHECK_FALSE(harness.contexts.assigned_subtree_complete(lease));
    CHECK(harness.transport.signer_sets.empty());

    auto child_four = harness.core.make_part(4, key);
    REQUIRE(harness.contexts.record_verified_direct_part(
        lease, harness.core.get_config(), 4, 4, *child_four));
    REQUIRE(harness.contexts.assigned_subtree_complete(lease));
    auto candidate = harness.contexts.clone_accumulator(lease);
    REQUIRE(candidate != nullptr);
    candidate->compute();
    REQUIRE(candidate->verify(harness.core.get_config()));
    harness.send_claim(
        lease, claim_unforwarded(harness.contexts, lease,
                                 std::move(candidate)));

    REQUIRE(harness.transport.signer_sets.size() == 1);
    CHECK(harness.transport.signer_sets.front() ==
          std::set<ReplicaID>{1, 3, 4});
    CHECK(harness.contexts.transition(
              lease,
              ProposalContextEvent::non_root_aggregate_enqueued) ==
          hotstuff::ProposalTransitionResult::terminal_closed);
    CHECK(harness.contexts.context_status(key) ==
          ProposalContextStatus::terminal_closed);
}

TEST_CASE("WE06-C05 wait-exempt readiness preserves optional contributions",
          "[we06][c05][aggregation][wait-exempt][delta-open]")
{
    SECTION("missing local signer still flushes verified descendants")
    {
        Harness harness(1);
        const auto key = make_test_proposal_key(
            make_digest(0xce), 0xcf, 39, 1);
        const auto lease = harness.admit(
            key, wait_exempt_internal_tree());
        REQUIRE(harness.coordinator.arm_timeout(
                    lease,
                    harness.scheduler,
                    internal_level,
                    maximum_level) != 0);

        auto required_child = harness.core.make_part(3, key);
        REQUIRE(harness.contexts.record_verified_direct_part(
            lease,
            harness.core.get_config(),
            3,
            3,
            *required_child));
        REQUIRE_FALSE(
            harness.contexts.required_subtree_complete(lease));
        const auto pending_required =
            harness.contexts.pending_required_child_branches(lease);
        REQUIRE(pending_required.has_value());
        REQUIRE(pending_required->empty());

        harness.scheduler.advance_by(
            harness.policy.timeout_for(
                internal_level, maximum_level));

        CHECK(harness.transport.signer_sets ==
              std::vector<std::set<ReplicaID>>{{3}});
        CHECK(harness.contexts.delta_open_enabled(lease));
    }

    SECTION("optional absence forwards once before its observation deadline")
    {
        Harness harness(1);
        const auto key = make_test_proposal_key(
            make_digest(0xd0), 0xd1, 40, 1);
        const auto lease = harness.admit(
            key, wait_exempt_internal_tree());
        REQUIRE(harness.coordinator.arm_timeout(
                    lease,
                    harness.scheduler,
                    internal_level,
                    maximum_level) != 0);

        auto local = harness.signing.sign(key, 1);
        REQUIRE(harness.contexts.record_local_part(
            lease, harness.core.get_config(), 1, *local));
        auto required_child = harness.core.make_part(3, key);
        REQUIRE(harness.contexts.record_verified_direct_part(
            lease,
            harness.core.get_config(),
            3,
            3,
            *required_child));
        REQUIRE(harness.contexts.required_subtree_complete(lease));
        CHECK_FALSE(harness.contexts.assigned_subtree_complete(lease));
        CHECK(harness.contexts.missing_optional_signers(lease) ==
              std::optional<std::set<ReplicaID>>({4}));

        auto candidate = harness.contexts.clone_accumulator(lease);
        REQUIRE(candidate != nullptr);
        candidate->compute();
        REQUIRE(candidate->verify(harness.core.get_config()));
        auto claim = harness.contexts
                         .claim_initial_certificate_reservation(
                             lease, std::move(candidate));
        REQUIRE(claim.has_value());
        CHECK(claim->signers == std::set<ReplicaID>{1, 3});
        const auto reservation_id = claim->reservation_id;
        harness.transport.send(lease, std::move(*claim));
        REQUIRE(harness.contexts.commit_forwarding_claim(
            lease, reservation_id));
        REQUIRE(harness.contexts.transition(
                    lease,
                    ProposalContextEvent::non_root_aggregate_enqueued) ==
                hotstuff::ProposalTransitionResult::retained_open);
        REQUIRE(harness.contexts.delta_open_enabled(lease));
        const auto signing_calls = harness.signing.calls;

        harness.scheduler.advance_by(
            harness.policy.timeout_for(
                internal_level, maximum_level));

        CHECK(harness.signing.calls == signing_calls);
        CHECK(harness.pacemaker.rotations.empty());
        CHECK(harness.candidate_requests == 0);
        CHECK(harness.timeouts.keys.empty());
        REQUIRE(harness.transport.signer_sets.size() == 1);
        CHECK(harness.transport.signer_sets.front() ==
              std::set<ReplicaID>{1, 3});
        CHECK(harness.optional_absence ==
              std::vector<std::set<ReplicaID>>{{4}});
        CHECK(harness.contexts.delta_open_enabled(lease));
    }

    SECTION("an on-time optional vote remains in the initial aggregate")
    {
        Harness harness(1);
        const auto key = make_test_proposal_key(
            make_digest(0xd2), 0xd3, 41, 1);
        const auto lease = harness.admit(
            key, wait_exempt_internal_tree());
        auto local = harness.signing.sign(key, 1);
        REQUIRE(harness.contexts.record_local_part(
            lease, harness.core.get_config(), 1, *local));
        for (const ReplicaID child : {3, 4})
        {
            auto part = harness.core.make_part(child, key);
            REQUIRE(harness.contexts.record_verified_direct_part(
                lease,
                harness.core.get_config(),
                child,
                child,
                *part));
        }
        REQUIRE(harness.contexts.required_subtree_complete(lease));
        REQUIRE(harness.contexts.assigned_subtree_complete(lease));

        auto candidate = harness.contexts.clone_accumulator(lease);
        REQUIRE(candidate != nullptr);
        candidate->compute();
        REQUIRE(candidate->verify(harness.core.get_config()));
        auto claim = harness.contexts
                         .claim_initial_certificate_reservation(
                             lease, std::move(candidate));
        REQUIRE(claim.has_value());
        CHECK(claim->signers == std::set<ReplicaID>{1, 3, 4});
        const auto reservation_id = claim->reservation_id;
        harness.transport.send(lease, std::move(*claim));
        REQUIRE(harness.contexts.commit_forwarding_claim(
            lease, reservation_id));
        CHECK(harness.contexts.transition(
                  lease,
                  ProposalContextEvent::non_root_aggregate_enqueued) ==
              hotstuff::ProposalTransitionResult::terminal_closed);
        REQUIRE(harness.transport.signer_sets.size() == 1);
        CHECK(harness.transport.signer_sets.front() ==
              std::set<ReplicaID>{1, 3, 4});
    }
}

TEST_CASE("late direct and aggregate certificates each claim once",
          "[a06][aggregation][late-votes][direct][aggregate]"
          "[dedup][intentional-red]")
{
    Harness harness(1);
    const auto key = make_test_proposal_key(
        make_digest(0xb6), 0xb7, 23, 1);
    const auto lease = harness.admit(key, wide_internal_tree());
    REQUIRE(harness.coordinator.arm_timeout(
                lease,
                harness.scheduler,
                internal_level,
                maximum_level) != 0);
    harness.scheduler.advance_by(
        harness.policy.timeout_for(internal_level, maximum_level));
    REQUIRE(harness.transport.signer_sets.empty());

    auto direct_part = harness.core.make_part(3, key);
    REQUIRE(harness.contexts.record_verified_direct_part(
        lease, harness.core.get_config(), 3, 3, *direct_part));
    CHECK_FALSE(harness.contexts.record_verified_direct_part(
        lease, harness.core.get_config(), 3, 3, *direct_part));
    auto direct_certificate = aggregate(harness.core, key, {3});
    auto direct_claim = claim_unforwarded(
        harness.contexts, lease, direct_certificate->clone());
    REQUIRE(direct_claim.has_value());
    harness.transport.send(lease, std::move(*direct_claim));
    CHECK_FALSE(claim_unforwarded(
        harness.contexts, lease, direct_certificate->clone()).has_value());

    auto relay = aggregate(harness.core, key, {4, 5});
    REQUIRE(harness.contexts.record_verified_aggregate_certificate(
        lease, 4, *relay));
    CHECK_FALSE(harness.contexts.record_verified_aggregate_certificate(
        lease, 4, *relay));
    auto relay_claim = claim_unforwarded(
        harness.contexts, lease, relay->clone());
    REQUIRE(relay_claim.has_value());
    harness.transport.send(lease, std::move(*relay_claim));
    CHECK_FALSE(claim_unforwarded(
        harness.contexts, lease, relay->clone()).has_value());

    REQUIRE(harness.transport.signer_sets.size() == 2);
    CHECK(harness.transport.signer_sets[0] == std::set<ReplicaID>{3});
    CHECK(harness.transport.signer_sets[1] ==
          std::set<ReplicaID>{4, 5});
    CHECK(harness.transport.valid == std::vector<bool>{true, true});
    CHECK(harness.signing.calls == 0);
    CHECK(harness.contexts.context_status(key) ==
          ProposalContextStatus::admitted_open);
}

TEST_CASE("overlapping late aggregate is rejected atomically",
          "[a06][aggregation][late-votes][overlap][intentional-red]")
{
    Harness harness(1);
    const auto key = make_test_proposal_key(
        make_digest(0xb8), 0xb9, 24, 1);
    const auto lease = harness.admit(key, wide_internal_tree());
    REQUIRE(harness.coordinator.arm_timeout(
                lease,
                harness.scheduler,
                internal_level,
                maximum_level) != 0);
    harness.scheduler.advance_by(
        harness.policy.timeout_for(internal_level, maximum_level));

    auto accepted = aggregate(harness.core, key, {4, 5});
    REQUIRE(harness.contexts.record_verified_aggregate_certificate(
        lease, 4, *accepted));
    harness.send_claim(
        lease, claim_unforwarded(
                   harness.contexts, lease, accepted->clone()));
    const auto before = harness.contexts.snapshot(key);
    REQUIRE(before.has_value());

    auto overlapping = aggregate(harness.core, key, {5, 6});
    CHECK_FALSE(harness.contexts.record_verified_aggregate_certificate(
        lease, 4, *overlapping));
    CHECK_FALSE(claim_unforwarded(
        harness.contexts, lease, overlapping->clone()).has_value());
    const auto after = harness.contexts.snapshot(key);
    REQUIRE(after.has_value());
    CHECK(after->verified_signers == before->verified_signers);
    CHECK(after->forwarded_signers == before->forwarded_signers);
    REQUIRE(harness.transport.signer_sets.size() == 1);
    CHECK(harness.transport.signer_sets.front() ==
          std::set<ReplicaID>{4, 5});
}

TEST_CASE("root timeout records state and continues toward frozen 2f plus 1",
          "[a06][aggregation][root][timeout][quorum-independence]"
          "[intentional-red]")
{
    Harness harness(0);
    const auto key = make_test_proposal_key(
        make_digest(0xba), 0xbb, 25, 1);
    const auto lease = harness.admit(key, root_tree());

    auto local = harness.signing.sign(key, 0);
    REQUIRE(harness.contexts.record_local_part(
        lease, harness.core.get_config(), 0, *local));
    auto first = aggregate(harness.core, key, {1, 3, 4});
    REQUIRE(harness.contexts.record_verified_aggregate_certificate(
        lease, 1, *first));
    REQUIRE(harness.contexts.clone_publishable_root_qc(lease) == nullptr);
    const auto signing_before_timeout = harness.signing.calls;

    REQUIRE(harness.coordinator.arm_timeout(
                lease, harness.scheduler, 0, maximum_level) != 0);
    harness.scheduler.advance_by(
        harness.policy.timeout_for(0, maximum_level));

    CHECK(harness.signing.calls == signing_before_timeout);
    CHECK(harness.pacemaker.rotations.empty());
    CHECK(harness.transport.signer_sets.empty());
    CHECK(harness.candidate_requests == 0);
    REQUIRE(harness.timeouts.missing_children.size() == 1);
    CHECK(harness.timeouts.missing_children.front() ==
          std::set<ReplicaID>{2});
    CHECK(harness.contexts.context_status(key) ==
          ProposalContextStatus::admitted_open);

    auto second = aggregate(harness.core, key, {2, 5, 6});
    REQUIRE(harness.contexts.record_verified_aggregate_certificate(
        lease, 2, *second));
    auto publishable = harness.contexts.clone_publishable_root_qc(lease);
    REQUIRE(publishable != nullptr);
    CHECK(signer_set(*publishable) ==
          std::set<ReplicaID>{0, 1, 2, 3, 4, 5, 6});
    CHECK(harness.contexts.transition(
              lease, ProposalContextEvent::root_qc_published) ==
          hotstuff::ProposalTransitionResult::terminal_closed);
}

TEST_CASE("stale generation cannot affect same block in another configuration",
          "[a06][aggregation][generation][configuration][stale]"
          "[intentional-red]")
{
    Harness harness(1);
    const auto block = make_digest(0xbc);
    const auto key_a = make_test_proposal_key(block, 0xbd, 26, 1);
    const auto key_b = make_test_proposal_key(block, 0xbe, 27, 1);
    const auto lease_a = harness.admit(key_a, internal_tree());
    const auto lease_b = harness.admit(key_b, internal_tree());
    const auto generation_a = harness.coordinator.arm_timeout(
        lease_a, harness.scheduler, internal_level, maximum_level);
    const auto generation_b = harness.coordinator.arm_timeout(
        lease_b, harness.scheduler, internal_level, maximum_level);
    REQUIRE(generation_a != 0);
    REQUIRE(generation_b != 0);
    REQUIRE(harness.contexts.close(
        key_a, ProposalContextEvent::proposal_aborted));

    CHECK_FALSE(harness.coordinator.dispatch_timeout(
        key_a, generation_a));
    CHECK_FALSE(harness.coordinator.dispatch_timeout(
        key_b, generation_a));
    CHECK(harness.transport.signer_sets.empty());
    CHECK(harness.timeouts.keys.empty());
    CHECK(harness.contexts.context_status(key_a) ==
          ProposalContextStatus::terminal_closed);
    CHECK(harness.contexts.context_status(key_b) ==
          ProposalContextStatus::admitted_open);
    CHECK(harness.contexts.snapshot(key_b)->timer_generation == generation_b);

    REQUIRE(harness.coordinator.dispatch_timeout(
        key_b, generation_b));
    CHECK(harness.timeouts.keys == std::vector<ProposalKey>{key_b});
    CHECK(harness.pacemaker.rotations.empty());
}

TEST_CASE("terminal cleanup cancels timers and retirement bounds storage",
          "[a06][aggregation][cleanup][bounded][intentional-red]")
{
    Harness harness(1);
    constexpr std::uint32_t context_count = 24;

    for (std::uint32_t index = 0; index < context_count; ++index)
    {
        const auto key = make_test_proposal_key(
            make_digest(static_cast<std::uint8_t>(0xc1 + index)),
            static_cast<std::uint8_t>(0xd0 + index),
            100 + index,
            1);
        const auto lease = harness.admit(key, internal_tree());
        REQUIRE(harness.coordinator.arm_timeout(
                    lease,
                    harness.scheduler,
                    internal_level,
                    maximum_level) != 0);
        REQUIRE(harness.contexts.close(
            key, ProposalContextEvent::committed));
    }

    CHECK(harness.scheduler.pending() == 0);
    auto stats = harness.contexts.storage_stats();
    CHECK(stats.retained_tree_snapshots == 0);
    CHECK(stats.retained_runtime_states == 0);
    CHECK(stats.retained_accumulators == 0);
    CHECK(stats.retained_latency_entries == 0);
    CHECK(stats.terminal_tombstones == context_count);
    REQUIRE(harness.contexts.advance_retirement_floor(1000) ==
            context_count);
    stats = harness.contexts.storage_stats();
    CHECK(stats.terminal_tombstones == 0);
    CHECK(stats.retired_configuration_tombstones == 0);
}
