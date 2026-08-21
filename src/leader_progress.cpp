#include "hotstuff/leader_progress.h"

#include <condition_variable>
#include <limits>
#include <mutex>
#include <stdexcept>
#include <thread>
#include <utility>

namespace hotstuff
{
namespace
{

class ScheduledCancellation final
{
public:
    using Cancellation = LeaderProgressScheduler::Cancellation;

    ~ScheduledCancellation()
    {
        cancel();
    }

    void install(Cancellation cancellation) noexcept
    {
        Cancellation cancel_now;
        {
            std::lock_guard<std::mutex> lock(mutex_);
            if (cancel_requested_)
                cancel_now = std::move(cancellation);
            else
                cancellation_ = std::move(cancellation);
        }
        if (cancel_now)
        {
            try
            {
                cancel_now();
            }
            catch (...)
            {}
        }
    }

    void cancel() noexcept
    {
        Cancellation cancellation;
        {
            std::lock_guard<std::mutex> lock(mutex_);
            if (cancel_requested_)
                return;
            cancel_requested_ = true;
            cancellation = std::move(cancellation_);
        }
        if (cancellation)
        {
            try
            {
                cancellation();
            }
            catch (...)
            {}
        }
    }

private:
    std::mutex mutex_;
    Cancellation cancellation_;
    bool cancel_requested_{false};
};

class ScheduledDispatch final
{
public:
    explicit ScheduledDispatch(std::function<void()> callback)
        : callback_(std::move(callback))
    {}

    void fire()
    {
        std::function<void()> callback;
        {
            std::lock_guard<std::mutex> lock(mutex_);
            if (cancelled_)
                return;
            if (!armed_)
            {
                fired_ = true;
                return;
            }
            cancelled_ = true;
            callback = std::move(callback_);
        }
        if (callback)
            callback();
    }

    void arm()
    {
        std::function<void()> callback;
        {
            std::lock_guard<std::mutex> lock(mutex_);
            if (cancelled_)
                return;
            armed_ = true;
            if (!fired_)
                return;
            cancelled_ = true;
            callback = std::move(callback_);
        }
        if (callback)
            callback();
    }

    void cancel() noexcept
    {
        std::lock_guard<std::mutex> lock(mutex_);
        cancelled_ = true;
        callback_ = {};
    }

private:
    std::mutex mutex_;
    std::function<void()> callback_;
    bool armed_{false};
    bool fired_{false};
    bool cancelled_{false};
};

class EffectDispatch final
{
public:
    bool try_start()
    {
        std::lock_guard<std::mutex> lock(mutex_);
        if (phase_ != Phase::pending)
            return false;
        phase_ = Phase::running;
        running_thread_ = std::this_thread::get_id();
        return true;
    }

    void finish()
    {
        {
            std::lock_guard<std::mutex> lock(mutex_);
            if (phase_ != Phase::running)
                return;
            phase_ = Phase::done;
            running_thread_ = {};
        }
        completed_.notify_all();
    }

    bool cancel_or_wait()
    {
        std::unique_lock<std::mutex> lock(mutex_);
        if (phase_ == Phase::pending)
        {
            phase_ = Phase::cancelled;
            lock.unlock();
            completed_.notify_all();
            return true;
        }
        if (phase_ == Phase::running)
        {
            if (running_thread_ == std::this_thread::get_id())
                return false;
            completed_.wait(lock, [this]() {
                return phase_ != Phase::running;
            });
        }
        return true;
    }

private:
    enum class Phase
    {
        pending,
        running,
        cancelled,
        done
    };

    std::mutex mutex_;
    std::condition_variable completed_;
    Phase phase_{Phase::pending};
    std::thread::id running_thread_;
};

LeaderProgressScheduler::Cancellation bind_cancellation(
    const std::shared_ptr<ScheduledDispatch> &dispatch,
    LeaderProgressScheduler::Cancellation cancellation)
{
    return [dispatch, cancellation = std::move(cancellation)]() mutable {
        dispatch->cancel();
        if (cancellation)
            cancellation();
    };
}

bool aggregation_deadline_precedes_suspicion(
    const LeaderProgressConfig &config)
{
    // Compare maximum_aggregation_timeout < activation_grace +
    // progress_timeout without overflowing Duration::rep.
    if (config.maximum_aggregation_timeout < config.activation_grace)
        return true;
    return config.maximum_aggregation_timeout - config.activation_grace <
           config.progress_timeout;
}

void invalidate_deadline_generation(std::uint64_t &generation) noexcept
{
    if (generation != std::numeric_limits<std::uint64_t>::max())
        ++generation;
}

} // namespace

struct LeaderProgressMonitor::State
{
    enum class Phase
    {
        inactive,
        activation_grace,
        progress_timeout,
        expired
    };

    State(LeaderProgressConfig config_, LeaderProgressEffects effects_)
        : config(std::move(config_)), effects(std::move(effects_))
    {}

    const LeaderProgressConfig config;
    const LeaderProgressEffects effects;
    mutable std::mutex mutex;
    bool stopped{false};
    std::optional<LeaderViewId> active;
    std::uint64_t last_view_generation{0};
    std::uint64_t deadline_generation{0};
    Phase phase{Phase::inactive};
    LeaderProgressScheduler *scheduler{nullptr};
    std::shared_ptr<ScheduledCancellation> deadline;
    std::shared_ptr<EffectDispatch> effect_dispatch;
    bool bounded_epoch_command_window_granted{false};

    void settle_effect(
        const std::shared_ptr<EffectDispatch> &effect)
    {
        if (!effect || !effect->cancel_or_wait())
            return;

        std::lock_guard<std::mutex> lock(mutex);
        if (effect_dispatch == effect)
            effect_dispatch.reset();
    }
};

LeaderProgressMonitor::LeaderProgressMonitor(
    LeaderProgressConfig config,
    LeaderProgressEffects effects)
{
    if (config.activation_grace <= LeaderProgressConfig::Duration::zero() ||
        config.progress_timeout <= LeaderProgressConfig::Duration::zero() ||
        config.maximum_aggregation_timeout <=
            LeaderProgressConfig::Duration::zero())
    {
        throw std::invalid_argument(
            "leader progress durations must be positive");
    }
    if (!aggregation_deadline_precedes_suspicion(config))
    {
        throw std::invalid_argument(
            "aggregation timeout must precede leader suspicion");
    }
    state_ = std::make_shared<State>(
        std::move(config), std::move(effects));
}

LeaderProgressMonitor::~LeaderProgressMonitor()
{
    try
    {
        shutdown();
    }
    catch (...)
    {}
}

bool LeaderProgressMonitor::activate(
    const LeaderViewId &view,
    LeaderProgressScheduler &scheduler)
{
    const auto state = state_;

    std::optional<LeaderViewId> previous_active;
    std::shared_ptr<ScheduledCancellation> previous_deadline;
    std::shared_ptr<EffectDispatch> previous_effect;
    LeaderProgressScheduler *previous_scheduler;
    State::Phase previous_phase;
    std::uint64_t previous_last_view_generation;
    std::uint64_t previous_deadline_generation;
    std::uint64_t generation;
    {
        std::lock_guard<std::mutex> lock(state->mutex);
        if (state->stopped || view.view_generation == 0 ||
            view.view_generation <= state->last_view_generation ||
            (state->scheduler != nullptr &&
             state->scheduler != &scheduler) ||
            state->deadline_generation ==
                std::numeric_limits<std::uint64_t>::max())
        {
            return false;
        }

        previous_active = state->active;
        previous_deadline = state->deadline;
        previous_scheduler = state->scheduler;
        previous_phase = state->phase;
        previous_last_view_generation = state->last_view_generation;
        previous_deadline_generation = state->deadline_generation;
        generation = previous_deadline_generation + 1;

    }

    std::weak_ptr<State> weak_state = state;
    auto *const scheduler_owner = &scheduler;
    auto dispatch = std::make_shared<ScheduledDispatch>(
        [weak_state, view, generation, scheduler_owner]() {
            if (const auto current = weak_state.lock())
            {
                static_cast<void>(dispatch_grace_for_state(
                    current, view, generation, *scheduler_owner));
            }
        });
    auto deadline = std::make_shared<ScheduledCancellation>();
    try
    {
        auto cancellation = scheduler.schedule_after(
            state->config.activation_grace,
            [dispatch]() { dispatch->fire(); });
        deadline->install(bind_cancellation(
            dispatch, std::move(cancellation)));
    }
    catch (...)
    {
        dispatch->cancel();
        deadline->cancel();
        throw;
    }

    bool committed = false;
    {
        std::lock_guard<std::mutex> lock(state->mutex);
        if (!state->stopped && state->active == previous_active &&
            state->scheduler == previous_scheduler &&
            state->phase == previous_phase &&
            state->last_view_generation ==
                previous_last_view_generation &&
            state->deadline_generation ==
                previous_deadline_generation &&
            state->deadline == previous_deadline)
        {
            state->scheduler = &scheduler;
            state->last_view_generation = view.view_generation;
            state->active = view;
            state->phase = State::Phase::activation_grace;
            state->bounded_epoch_command_window_granted = false;
            state->deadline_generation = generation;
            state->deadline = deadline;
            previous_effect = state->effect_dispatch;
            committed = true;
        }
    }

    if (!committed)
    {
        deadline->cancel();
        return false;
    }
    if (previous_deadline)
        previous_deadline->cancel();
    state->settle_effect(previous_effect);
    dispatch->arm();
    return true;
}

bool LeaderProgressMonitor::record_verified_progress(
    const LeaderViewId &view,
    LeaderProgressEvent event,
    LeaderProgressScheduler &scheduler)
{
    const auto state = state_;

    std::shared_ptr<ScheduledCancellation> previous_deadline;
    std::uint64_t previous_generation;
    std::uint64_t generation;
    {
        std::lock_guard<std::mutex> lock(state->mutex);
        if (state->stopped || state->active != view ||
            state->scheduler != &scheduler ||
            state->config.reset_on.count(event) == 0)
        {
            return false;
        }

        if (state->phase == State::Phase::activation_grace)
            return true;
        if (state->phase != State::Phase::progress_timeout ||
            state->deadline_generation ==
                std::numeric_limits<std::uint64_t>::max())
        {
            return false;
        }

        previous_generation = state->deadline_generation;
        generation = previous_generation + 1;
        previous_deadline = state->deadline;
    }

    std::weak_ptr<State> weak_state = state;
    auto dispatch = std::make_shared<ScheduledDispatch>(
        [weak_state, view, generation]() {
            if (const auto current = weak_state.lock())
            {
                static_cast<void>(dispatch_timeout_for_state(
                    current, view, generation));
            }
        });
    auto deadline = std::make_shared<ScheduledCancellation>();
    try
    {
        auto cancellation = scheduler.schedule_after(
            state->config.progress_timeout,
            [dispatch]() { dispatch->fire(); });
        deadline->install(bind_cancellation(
            dispatch, std::move(cancellation)));
    }
    catch (...)
    {
        dispatch->cancel();
        deadline->cancel();
        throw;
    }

    bool committed = false;
    {
        std::lock_guard<std::mutex> lock(state->mutex);
        if (!state->stopped && state->active == view &&
            state->scheduler == &scheduler &&
            state->phase == State::Phase::progress_timeout &&
            state->deadline_generation == previous_generation &&
            state->deadline == previous_deadline)
        {
            state->deadline_generation = generation;
            state->deadline = deadline;
            committed = true;
        }
    }

    if (!committed)
    {
        deadline->cancel();
        return false;
    }
    if (previous_deadline)
        previous_deadline->cancel();
    dispatch->arm();
    return true;
}

bool LeaderProgressMonitor::grant_bounded_epoch_command_window(
    const LeaderViewId &view,
    LeaderProgressScheduler &scheduler)
{
    const auto state = state_;
    if (state->config.progress_timeout >
        LeaderProgressConfig::Duration::max() / 2)
        return false;
    const auto delay = state->config.progress_timeout * 2;

    std::shared_ptr<ScheduledCancellation> previous_deadline;
    State::Phase previous_phase;
    std::uint64_t previous_generation;
    std::uint64_t generation;
    {
        std::lock_guard<std::mutex> lock(state->mutex);
        if (state->stopped || state->active != view ||
            state->scheduler != &scheduler ||
            state->bounded_epoch_command_window_granted ||
            (state->phase != State::Phase::activation_grace &&
             state->phase != State::Phase::progress_timeout) ||
            state->deadline_generation ==
                std::numeric_limits<std::uint64_t>::max())
        {
            return false;
        }
        previous_phase = state->phase;
        previous_generation = state->deadline_generation;
        generation = previous_generation + 1;
        previous_deadline = state->deadline;
    }

    std::weak_ptr<State> weak_state = state;
    auto dispatch = std::make_shared<ScheduledDispatch>(
        [weak_state, view, generation]() {
            if (const auto current = weak_state.lock())
            {
                static_cast<void>(dispatch_timeout_for_state(
                    current, view, generation));
            }
        });
    auto deadline = std::make_shared<ScheduledCancellation>();
    try
    {
        auto cancellation = scheduler.schedule_after(
            delay, [dispatch]() { dispatch->fire(); });
        deadline->install(bind_cancellation(
            dispatch, std::move(cancellation)));
    }
    catch (...)
    {
        dispatch->cancel();
        deadline->cancel();
        throw;
    }

    bool committed = false;
    {
        std::lock_guard<std::mutex> lock(state->mutex);
        if (!state->stopped && state->active == view &&
            state->scheduler == &scheduler &&
            !state->bounded_epoch_command_window_granted &&
            state->phase == previous_phase &&
            state->deadline_generation == previous_generation &&
            state->deadline == previous_deadline)
        {
            state->phase = State::Phase::progress_timeout;
            state->bounded_epoch_command_window_granted = true;
            state->deadline_generation = generation;
            state->deadline = deadline;
            committed = true;
        }
    }

    if (!committed)
    {
        deadline->cancel();
        return false;
    }
    if (previous_deadline)
        previous_deadline->cancel();
    dispatch->arm();
    return true;
}

bool LeaderProgressMonitor::dispatch_grace(
    const LeaderViewId &view,
    std::uint64_t deadline_generation,
    LeaderProgressScheduler &scheduler)
{
    return dispatch_grace_for_state(
        state_, view, deadline_generation, scheduler);
}

bool LeaderProgressMonitor::dispatch_grace_for_state(
    const std::shared_ptr<State> &state,
    const LeaderViewId &view,
    std::uint64_t deadline_generation,
    LeaderProgressScheduler &scheduler)
{
    std::shared_ptr<ScheduledCancellation> previous_deadline;
    std::uint64_t next_generation;
    {
        std::lock_guard<std::mutex> lock(state->mutex);
        if (state->stopped || state->active != view ||
            state->scheduler != &scheduler ||
            state->phase != State::Phase::activation_grace ||
            state->deadline_generation != deadline_generation ||
            state->deadline_generation ==
                std::numeric_limits<std::uint64_t>::max())
        {
            return false;
        }

        next_generation = deadline_generation + 1;
        previous_deadline = state->deadline;
    }

    std::weak_ptr<State> weak_state = state;
    auto dispatch = std::make_shared<ScheduledDispatch>(
        [weak_state, view, next_generation]() {
            if (const auto current = weak_state.lock())
            {
                static_cast<void>(dispatch_timeout_for_state(
                    current, view, next_generation));
            }
        });
    auto deadline = std::make_shared<ScheduledCancellation>();
    try
    {
        auto cancellation = scheduler.schedule_after(
            state->config.progress_timeout,
            [dispatch]() { dispatch->fire(); });
        deadline->install(bind_cancellation(
            dispatch, std::move(cancellation)));
    }
    catch (...)
    {
        dispatch->cancel();
        deadline->cancel();
        std::shared_ptr<ScheduledCancellation> failed_deadline;
        {
            std::lock_guard<std::mutex> lock(state->mutex);
            if (state->active == view &&
                state->scheduler == &scheduler &&
                state->phase == State::Phase::activation_grace &&
                state->deadline_generation == deadline_generation &&
                state->deadline == previous_deadline)
            {
                state->phase = State::Phase::expired;
                invalidate_deadline_generation(
                    state->deadline_generation);
                failed_deadline = std::move(state->deadline);
            }
        }
        if (failed_deadline)
            failed_deadline->cancel();
        throw;
    }

    bool committed = false;
    {
        std::lock_guard<std::mutex> lock(state->mutex);
        if (!state->stopped && state->active == view &&
            state->scheduler == &scheduler &&
            state->phase == State::Phase::activation_grace &&
            state->deadline_generation == deadline_generation &&
            state->deadline == previous_deadline)
        {
            state->phase = State::Phase::progress_timeout;
            state->deadline_generation = next_generation;
            state->deadline = deadline;
            committed = true;
        }
    }

    if (!committed)
    {
        deadline->cancel();
        return false;
    }
    if (previous_deadline)
        previous_deadline->cancel();
    dispatch->arm();
    return true;
}

bool LeaderProgressMonitor::dispatch_timeout(
    const LeaderViewId &view,
    std::uint64_t deadline_generation)
{
    return dispatch_timeout_for_state(
        state_, view, deadline_generation);
}

bool LeaderProgressMonitor::dispatch_timeout_for_state(
    const std::shared_ptr<State> &state,
    const LeaderViewId &view,
    std::uint64_t deadline_generation)
{
    auto rotate = state->effects.rotate_active_view;
    auto effect = std::make_shared<EffectDispatch>();
    std::shared_ptr<ScheduledCancellation> deadline;
    {
        std::lock_guard<std::mutex> lock(state->mutex);
        if (state->stopped || state->active != view ||
            state->phase != State::Phase::progress_timeout ||
            state->deadline_generation != deadline_generation)
        {
            return false;
        }

        state->phase = State::Phase::expired;
        invalidate_deadline_generation(state->deadline_generation);
        deadline = std::move(state->deadline);
        state->effect_dispatch = effect;
    }

    if (deadline)
        deadline->cancel();

    if (!effect->try_start())
    {
        std::lock_guard<std::mutex> lock(state->mutex);
        if (state->effect_dispatch == effect)
            state->effect_dispatch.reset();
        return true;
    }

    try
    {
        if (rotate)
            rotate(view);
    }
    catch (...)
    {
        effect->finish();
        std::lock_guard<std::mutex> lock(state->mutex);
        if (state->effect_dispatch == effect)
            state->effect_dispatch.reset();
        throw;
    }
    effect->finish();
    {
        std::lock_guard<std::mutex> lock(state->mutex);
        if (state->effect_dispatch == effect)
            state->effect_dispatch.reset();
    }
    return true;
}

std::optional<LeaderViewId> LeaderProgressMonitor::active_view() const
{
    const auto state = state_;
    std::lock_guard<std::mutex> lock(state->mutex);
    if (state->stopped)
        return std::nullopt;
    return state->active;
}

void LeaderProgressMonitor::shutdown()
{
    const auto state = state_;
    std::shared_ptr<ScheduledCancellation> deadline;
    std::shared_ptr<EffectDispatch> effect;
    {
        std::lock_guard<std::mutex> lock(state->mutex);
        if (state->stopped)
        {
            effect = state->effect_dispatch;
        }
        else
        {
            state->stopped = true;
            state->active.reset();
            state->phase = State::Phase::inactive;
            invalidate_deadline_generation(state->deadline_generation);
            state->scheduler = nullptr;
            deadline = std::move(state->deadline);
            effect = state->effect_dispatch;
        }
    }
    if (deadline)
        deadline->cancel();
    state->settle_effect(effect);
}

} // namespace hotstuff
