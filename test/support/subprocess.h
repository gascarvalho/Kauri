#ifndef KAURI_TEST_SUPPORT_SUBPROCESS_H_INCLUDED
#define KAURI_TEST_SUPPORT_SUBPROCESS_H_INCLUDED

#include <cerrno>
#include <stdexcept>
#include <string>
#include <sys/wait.h>
#include <unistd.h>
#include <vector>

namespace kauri::test_support
{

struct ProcessResult
{
    int status{0};
    std::string output;
    std::string error;
};

inline std::string read_all(int descriptor)
{
    std::string contents;
    char buffer[4096];
    while (true)
    {
        const auto count = ::read(descriptor, buffer, sizeof(buffer));
        if (count > 0)
        {
            contents.append(buffer, static_cast<std::size_t>(count));
            continue;
        }
        if (count < 0 && errno == EINTR)
            continue;
        if (count < 0)
            throw std::runtime_error("failed to read subprocess output");
        return contents;
    }
}

inline void write_all(int descriptor, const std::string &input)
{
    std::size_t offset = 0;
    while (offset < input.size())
    {
        const auto count = ::write(
            descriptor, input.data() + offset, input.size() - offset);
        if (count > 0)
        {
            offset += static_cast<std::size_t>(count);
            continue;
        }
        if (count < 0 && errno == EINTR)
            continue;
        throw std::runtime_error("failed to write subprocess input");
    }
}

[[noreturn]] inline void execute(
    const char *path, const std::vector<std::string> &arguments)
{
    std::vector<std::string> owned_arguments;
    owned_arguments.reserve(arguments.size() + 1);
    owned_arguments.emplace_back(path);
    owned_arguments.insert(
        owned_arguments.end(), arguments.begin(), arguments.end());
    std::vector<char *> raw_arguments;
    raw_arguments.reserve(owned_arguments.size() + 1);
    for (auto &argument : owned_arguments)
        raw_arguments.push_back(argument.data());
    raw_arguments.push_back(nullptr);
    ::execv(path, raw_arguments.data());
    _exit(127);
}

inline int wait_for(pid_t child)
{
    int status = 0;
    while (::waitpid(child, &status, 0) < 0)
    {
        if (errno != EINTR)
            throw std::runtime_error("failed to wait for executable");
    }
    return WIFEXITED(status) ? WEXITSTATUS(status)
                            : 128 + WTERMSIG(status);
}

inline ProcessResult run_program(
    const char *path,
    const std::vector<std::string> &arguments,
    const std::string &input = {})
{
    int input_pipe[2];
    int output_pipe[2];
    int error_pipe[2];
    if (::pipe(input_pipe) != 0 || ::pipe(output_pipe) != 0 ||
        ::pipe(error_pipe) != 0)
    {
        throw std::runtime_error("failed to create subprocess pipes");
    }

    const auto child = ::fork();
    if (child < 0)
        throw std::runtime_error("failed to fork executable");
    if (child == 0)
    {
        ::close(input_pipe[1]);
        ::close(output_pipe[0]);
        ::close(error_pipe[0]);
        if (::dup2(input_pipe[0], STDIN_FILENO) < 0 ||
            ::dup2(output_pipe[1], STDOUT_FILENO) < 0 ||
            ::dup2(error_pipe[1], STDERR_FILENO) < 0)
        {
            _exit(126);
        }
        ::close(input_pipe[0]);
        ::close(output_pipe[1]);
        ::close(error_pipe[1]);

        execute(path, arguments);
    }

    ::close(input_pipe[0]);
    ::close(output_pipe[1]);
    ::close(error_pipe[1]);
    write_all(input_pipe[1], input);
    ::close(input_pipe[1]);

    ProcessResult result;
    result.output = read_all(output_pipe[0]);
    result.error = read_all(error_pipe[0]);
    ::close(output_pipe[0]);
    ::close(error_pipe[0]);

    result.status = wait_for(child);
    return result;
}

inline ProcessResult run_program_merged(
    const char *path,
    const std::vector<std::string> &arguments)
{
    int output_pipe[2];
    if (::pipe(output_pipe) != 0)
        throw std::runtime_error("failed to create subprocess pipe");

    const auto child = ::fork();
    if (child < 0)
    {
        ::close(output_pipe[0]);
        ::close(output_pipe[1]);
        throw std::runtime_error("failed to fork executable");
    }
    if (child == 0)
    {
        ::close(output_pipe[0]);
        if (::dup2(output_pipe[1], STDOUT_FILENO) < 0 ||
            ::dup2(output_pipe[1], STDERR_FILENO) < 0)
        {
            _exit(126);
        }
        ::close(output_pipe[1]);

        execute(path, arguments);
    }

    ::close(output_pipe[1]);
    ProcessResult result;
    result.output = read_all(output_pipe[0]);
    ::close(output_pipe[0]);

    result.status = wait_for(child);
    return result;
}

} // namespace kauri::test_support

#endif
