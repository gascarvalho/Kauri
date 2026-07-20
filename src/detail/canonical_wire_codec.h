#ifndef HOTSTUFF_DETAIL_CANONICAL_WIRE_CODEC_H_INCLUDED
#define HOTSTUFF_DETAIL_CANONICAL_WIRE_CODEC_H_INCLUDED

#include <algorithm>
#include <cstddef>
#include <cstdint>
#include <stdexcept>
#include <string>
#include <string_view>
#include <type_traits>
#include <utility>

#include "hotstuff/type.h"

namespace hotstuff::detail
{

constexpr std::size_t kCanonicalWireDigestBytes = 32;

class CanonicalWireWriter final
{
public:
    CanonicalWireWriter() = default;

    CanonicalWireWriter(
        std::size_t maximum_size,
        const char *limit_error_message) noexcept
        : maximum_size_(maximum_size),
          limit_error_message_(limit_error_message),
          bounded_(true)
    {}

    template<typename UInt>
    void integer(UInt value)
    {
        static_assert(
            std::is_unsigned<UInt>::value,
            "canonical wire integers must be unsigned");
        ensure(sizeof(UInt));
        for (std::size_t shift = sizeof(UInt); shift > 0; --shift)
        {
            bytes_.push_back(static_cast<std::uint8_t>(
                value >> ((shift - 1) * 8)));
        }
    }

    void domain(std::string_view value)
    {
        bytes(value);
    }

    void digest(
        const uint256_t &value,
        const char *invalid_digest_message)
    {
        const bytearray_t encoded = static_cast<bytearray_t>(value);
        if (encoded.size() != kCanonicalWireDigestBytes)
            throw std::logic_error(invalid_digest_message);
        bytes(encoded);
    }

    void bytes(const bytearray_t &value)
    {
        bytes(value.data(), value.size());
    }

    void bytes(std::string_view value)
    {
        bytes(
            reinterpret_cast<const std::uint8_t *>(value.data()),
            value.size());
    }

    void bytes(const std::uint8_t *data, std::size_t size)
    {
        ensure(size);
        if (size != 0)
            bytes_.insert(bytes_.end(), data, data + size);
    }

    bytearray_t finish() &&
    {
        return std::move(bytes_);
    }

private:
    void ensure(std::size_t additional) const
    {
        if (bounded_ &&
            (bytes_.size() > maximum_size_ ||
             additional > maximum_size_ - bytes_.size()))
        {
            throw std::length_error(limit_error_message_);
        }
    }

    std::size_t maximum_size_{0};
    const char *limit_error_message_{nullptr};
    bool bounded_{false};
    bytearray_t bytes_;
};

template<typename Failure, typename Error>
class CanonicalWireReader final
{
public:
    CanonicalWireReader(
        const bytearray_t &bytes,
        Error truncated_error) noexcept
        : bytes_(bytes), truncated_error_(truncated_error)
    {}

    void domain(std::string_view expected, Error invalid_domain_error)
    {
        require(expected.size());
        if (!std::equal(
                expected.begin(),
                expected.end(),
                bytes_.begin() + offset_))
        {
            throw Failure{invalid_domain_error};
        }
        offset_ += expected.size();
    }

    template<typename UInt>
    UInt integer()
    {
        static_assert(
            std::is_unsigned<UInt>::value,
            "canonical wire integers must be unsigned");
        require(sizeof(UInt));
        UInt value = 0;
        for (std::size_t index = 0; index < sizeof(UInt); ++index)
        {
            value = static_cast<UInt>(
                (value << 8) | bytes_[offset_ + index]);
        }
        offset_ += sizeof(UInt);
        return value;
    }

    uint256_t digest()
    {
        require(kCanonicalWireDigestBytes);
        const uint256_t value(bytes_.data() + offset_);
        offset_ += kCanonicalWireDigestBytes;
        return value;
    }

    bytearray_t bytes(std::size_t size)
    {
        require(size);
        bytearray_t value(
            bytes_.begin() + offset_, bytes_.begin() + offset_ + size);
        offset_ += size;
        return value;
    }

    std::string string(std::size_t size)
    {
        require(size);
        const auto *const begin = reinterpret_cast<const char *>(
            bytes_.data() + offset_);
        std::string value(begin, begin + size);
        offset_ += size;
        return value;
    }

    std::size_t remaining() const noexcept
    {
        return bytes_.size() - offset_;
    }

    bool empty() const noexcept
    {
        return offset_ == bytes_.size();
    }

    void require(std::size_t size) const
    {
        if (offset_ > bytes_.size() ||
            size > bytes_.size() - offset_)
        {
            throw Failure{truncated_error_};
        }
    }

private:
    const bytearray_t &bytes_;
    Error truncated_error_;
    std::size_t offset_{0};
};

} // namespace hotstuff::detail

#endif
