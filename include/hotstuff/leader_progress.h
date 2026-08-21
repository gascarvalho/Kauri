/**
 * Independent leader-progress monitoring for an exact active view.
 */

#ifndef HOTSTUFF_LEADER_PROGRESS_H_INCLUDED
#define HOTSTUFF_LEADER_PROGRESS_H_INCLUDED

#include <chrono>
#include <cstdint>
#include <functional>
#include <memory>
#include <optional>
#include <set>

#include "hotstuff/configuration.h"

namespace hotstuff
{

enum class LeaderProgressEvent
{
    verified_proposal,
    quorum_certificate,
    commit
};

struct LeaderProgressConfig
{
    using Duration = std::chrono::nanoseconds;

    Duration activation_grace;
    Duration progress_timeout;
    Duration maximum_aggregation_timeout;
    std::set<LeaderProgressEvent> reset_on;
};

class LeaderProgressScheduler
{
public:
    using Duration = LeaderProgressConfig::Duration;
    using Callback = std::function<void()>;
    using Cancellation = std::function<void()>;

    virtual ~LeaderProgressScheduler() = default;
    virtual Cancellation schedule_after(Duration delay,
                                        Callback callback) = 0;
};

struct LeaderProgressEffects
{
    std::function<void(const LeaderViewId &)> rotate_active_view;
};

/**
 * Owns the progress deadline for one exact LeaderViewId.
 *
 * Every activation carries a strictly increasing wire generation. Reusing a
 * ConfigurationId is safe only with a newer generation; delayed callbacks and
 * messages from an older activation are rejected by the exact view identity.
 *
 * The scheduler used by the first successful activation owns every later
 * deadline. It must outlive this monitor; calls that provide a different
 * scheduler are rejected without changing monitor state.
 */
class LeaderProgressMonitor
{
public:
    LeaderProgressMonitor(LeaderProgressConfig config,
                          LeaderProgressEffects effects);
    ~LeaderProgressMonitor();

    LeaderProgressMonitor(const LeaderProgressMonitor &) = delete;
    LeaderProgressMonitor &operator=(const LeaderProgressMonitor &) = delete;
    LeaderProgressMonitor(LeaderProgressMonitor &&) = delete;
    LeaderProgressMonitor &operator=(LeaderProgressMonitor &&) = delete;

    bool activate(const LeaderViewId &view,
                  LeaderProgressScheduler &scheduler);
    bool record_verified_progress(const LeaderViewId &view,
                                  LeaderProgressEvent event,
                                  LeaderProgressScheduler &scheduler);
    bool grant_bounded_epoch_command_window(
        const LeaderViewId &view,
        LeaderProgressScheduler &scheduler);

    bool dispatch_grace(const LeaderViewId &view,
                        std::uint64_t deadline_generation,
                        LeaderProgressScheduler &scheduler);
    bool dispatch_timeout(const LeaderViewId &view,
                          std::uint64_t deadline_generation);

    std::optional<LeaderViewId> active_view() const;
    void shutdown();

private:
    struct State;

    static bool dispatch_grace_for_state(
        const std::shared_ptr<State> &state,
        const LeaderViewId &view,
        std::uint64_t deadline_generation,
        LeaderProgressScheduler &scheduler);
    static bool dispatch_timeout_for_state(
        const std::shared_ptr<State> &state,
        const LeaderViewId &view,
        std::uint64_t deadline_generation);

    std::shared_ptr<State> state_;
};

} // namespace hotstuff

#endif
