#ifndef KAURI_ADAPTATION_MANAGER_EPOCH_ZERO_TREE_FILE_H
#define KAURI_ADAPTATION_MANAGER_EPOCH_ZERO_TREE_FILE_H

#include <array>
#include <cerrno>
#include <cstddef>
#include <cstdint>
#include <stdexcept>
#include <string>
#include <system_error>
#include <utility>

#include <fcntl.h>
#include <sys/stat.h>
#include <unistd.h>

#include "hotstuff/crypto.h"
#include "salticidae/util.h"

namespace adaptation_manager_detail
{

constexpr std::size_t kMaximumEpochZeroTreeBytes = 64 * 1024;

struct EpochZeroTreeFile final
{
    hotstuff::bytearray_t bytes;
    std::string sha256;
};

inline bool same_file_metadata(const struct stat &before,
                               const struct stat &after) noexcept
{
    if (!S_ISREG(after.st_mode) || after.st_dev != before.st_dev ||
        after.st_ino != before.st_ino || after.st_size != before.st_size ||
        after.st_mtime != before.st_mtime || after.st_ctime != before.st_ctime)
        return false;
#if defined(__APPLE__)
    return after.st_mtimespec.tv_nsec == before.st_mtimespec.tv_nsec &&
           after.st_ctimespec.tv_nsec == before.st_ctimespec.tv_nsec;
#else
    return after.st_mtim.tv_nsec == before.st_mtim.tv_nsec &&
           after.st_ctim.tv_nsec == before.st_ctim.tv_nsec;
#endif
}

inline std::string sha256_lower_hex(const hotstuff::bytearray_t &bytes)
{
    hotstuff::SHA256 hasher;
    if (!bytes.empty())
        hasher.update(bytes.data(), bytes.size());
    return salticidae::get_hex(hasher.digest());
}

inline EpochZeroTreeFile read_epoch_zero_tree_file(const std::string &path)
{
    const int fd = ::open(path.c_str(), O_RDONLY | O_CLOEXEC | O_NOFOLLOW |
                                           O_NONBLOCK);
    if (fd < 0)
        throw std::system_error(
            errno, std::generic_category(), "open epoch-zero tree file");

    try
    {
        struct stat before {};
        if (::fstat(fd, &before) != 0)
            throw std::system_error(
                errno, std::generic_category(), "stat epoch-zero tree file");
        if (!S_ISREG(before.st_mode))
            throw std::invalid_argument(
                "epoch-zero tree file must be a regular file");
        if (before.st_size < 0 ||
            static_cast<std::uintmax_t>(before.st_size) >
                kMaximumEpochZeroTreeBytes)
            throw std::invalid_argument("epoch-zero tree file exceeds byte limit");

        hotstuff::bytearray_t bytes(static_cast<std::size_t>(before.st_size));
        std::size_t offset = 0;
        while (offset < bytes.size())
        {
            const auto read_count = ::read(
                fd, bytes.data() + offset, bytes.size() - offset);
            if (read_count < 0)
            {
                if (errno == EINTR)
                    continue;
                throw std::system_error(
                    errno, std::generic_category(), "read epoch-zero tree file");
            }
            if (read_count == 0)
                throw std::runtime_error("epoch-zero tree file changed during read");
            offset += static_cast<std::size_t>(read_count);
        }

        std::array<unsigned char, 1> extra{};
        for (;;)
        {
            const auto read_count = ::read(fd, extra.data(), extra.size());
            if (read_count < 0)
            {
                if (errno == EINTR)
                    continue;
                throw std::system_error(
                    errno, std::generic_category(), "read epoch-zero tree EOF");
            }
            if (read_count != 0)
                throw std::runtime_error("epoch-zero tree file grew during read");
            break;
        }

        struct stat after {};
        if (::fstat(fd, &after) != 0)
            throw std::system_error(
                errno, std::generic_category(), "restat epoch-zero tree file");
        if (!same_file_metadata(before, after))
            throw std::runtime_error("epoch-zero tree file changed during read");
        auto sha256 = sha256_lower_hex(bytes);
        ::close(fd);
        return {std::move(bytes), std::move(sha256)};
    }
    catch (...)
    {
        ::close(fd);
        throw;
    }
}

} // namespace adaptation_manager_detail

#endif
