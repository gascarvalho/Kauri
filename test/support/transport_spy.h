#ifndef KAURI_TEST_SUPPORT_TRANSPORT_SPY_H
#define KAURI_TEST_SUPPORT_TRANSPORT_SPY_H

#include <cstddef>
#include <utility>
#include <vector>

#include "hotstuff/type.h"

namespace hotstuff::test
{

template<typename Message>
class TransportSpy
{
public:
    struct SentMessage
    {
        ReplicaID destination;
        Message message;
    };

    void send(ReplicaID destination, Message message)
    {
        messages_.push_back(SentMessage{destination, std::move(message)});
    }

    const std::vector<SentMessage> &messages() const noexcept
    {
        return messages_;
    }

    std::size_t count_for(ReplicaID destination) const
    {
        std::size_t count = 0;
        for (const auto &sent : messages_)
            if (sent.destination == destination)
                ++count;
        return count;
    }

    void clear() noexcept
    {
        messages_.clear();
    }

private:
    std::vector<SentMessage> messages_;
};

} // namespace hotstuff::test

#endif
