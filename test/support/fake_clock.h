#ifndef KAURI_TEST_SUPPORT_FAKE_CLOCK_H
#define KAURI_TEST_SUPPORT_FAKE_CLOCK_H

#include <algorithm>
#include <chrono>
#include <cstdint>
#include <functional>
#include <map>
#include <stdexcept>
#include <string>
#include <utility>
#include <vector>

namespace hotstuff::test
{

class FakeClock
{
public:
    using duration = std::chrono::nanoseconds;
    using time_point = std::chrono::time_point<FakeClock, duration>;
    static constexpr bool is_steady = true;

    time_point now() const noexcept
    {
        return now_;
    }

    void advance(duration amount)
    {
        if (amount < duration::zero())
            throw std::invalid_argument("fake clock cannot move backwards");
        now_ += amount;
    }

    void advance_to(time_point target)
    {
        if (target < now_)
            throw std::invalid_argument("fake clock cannot move backwards");
        now_ = target;
    }

private:
    time_point now_{duration::zero()};
};

class DeterministicScheduler
{
public:
    using Key = std::string;
    using Callback = std::function<void()>;
    using time_point = FakeClock::time_point;
    using duration = FakeClock::duration;

    explicit DeterministicScheduler(FakeClock &clock): clock_(clock) {}

    std::uint64_t schedule(Key key, time_point deadline, Callback callback)
    {
        const auto generation = ++last_generation_;
        tasks_[std::move(key)] = Task{
            deadline, generation, ++last_sequence_, std::move(callback)};
        return generation;
    }

    bool cancel(const Key &key)
    {
        return tasks_.erase(key) != 0;
    }

    bool contains(const Key &key) const
    {
        return tasks_.find(key) != tasks_.end();
    }

    std::size_t pending() const noexcept
    {
        return tasks_.size();
    }

    void advance_by(duration amount)
    {
        clock_.advance(amount);
        run_due();
    }

    void advance_to(time_point target)
    {
        clock_.advance_to(target);
        run_due();
    }

    void run_due()
    {
        while (true)
        {
            auto next = tasks_.end();
            for (auto it = tasks_.begin(); it != tasks_.end(); ++it)
            {
                if (it->second.deadline > clock_.now())
                    continue;
                if (next == tasks_.end() || comes_before(it->second, next->second))
                    next = it;
            }

            if (next == tasks_.end())
                return;

            auto callback = std::move(next->second.callback);
            tasks_.erase(next);
            callback();
        }
    }

    std::vector<Key> pending_keys() const
    {
        std::vector<Key> keys;
        keys.reserve(tasks_.size());
        for (const auto &entry : tasks_)
            keys.push_back(entry.first);
        return keys;
    }

private:
    struct Task
    {
        time_point deadline;
        std::uint64_t generation;
        std::uint64_t sequence;
        Callback callback;
    };

    static bool comes_before(const Task &lhs, const Task &rhs)
    {
        if (lhs.deadline != rhs.deadline)
            return lhs.deadline < rhs.deadline;
        return lhs.sequence < rhs.sequence;
    }

    FakeClock &clock_;
    std::map<Key, Task> tasks_;
    std::uint64_t last_generation_{0};
    std::uint64_t last_sequence_{0};
};

} // namespace hotstuff::test

#endif
