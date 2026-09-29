#include "catch.hpp"

#include <array>
#include <cerrno>
#include <cstdio>
#include <cstdint>
#include <filesystem>
#include <fcntl.h>
#include <fstream>
#include <iterator>
#include <string>
#include <sys/stat.h>
#include <sys/wait.h>
#include <unistd.h>
#include <vector>

#include <sodium.h>

#include "hotstuff/crypto.h"

namespace {
struct TemporaryDirectory {
    std::filesystem::path path;
    TemporaryDirectory() {
        std::array<char, 64> pattern{};
        std::snprintf(pattern.data(), pattern.size(), "/tmp/kauri-w18-identity-XXXXXX");
        const auto *created = mkdtemp(pattern.data());
        REQUIRE(created != nullptr);
        path = created;
        REQUIRE(chmod(path.c_str(), 0700) == 0);
    }
    ~TemporaryDirectory() { std::error_code ignored; std::filesystem::remove_all(path, ignored); }
};

struct Captured {
    int status{};
    std::string output;
};

Captured capture(const std::vector<std::string> &arguments)
{
    int pipe_fd[2]{};
    REQUIRE(pipe(pipe_fd) == 0);
    const auto child = fork();
    REQUIRE(child >= 0);
    if (child == 0) {
        alarm(30);
        close(pipe_fd[0]);
        if (dup2(pipe_fd[1], STDOUT_FILENO) < 0) _exit(127);
        if (dup2(pipe_fd[1], STDERR_FILENO) < 0) _exit(127);
        std::vector<char *> raw;
        raw.reserve(arguments.size() + 1);
        for (const auto &argument : arguments) raw.push_back(const_cast<char *>(argument.c_str()));
        raw.push_back(nullptr);
        execv(raw.front(), raw.data());
        _exit(127);
    }
    close(pipe_fd[1]);
    std::string output;
    std::array<char, 4096> buffer{};
    while (true) {
        const auto count = read(pipe_fd[0], buffer.data(), buffer.size());
        if (count <= 0) break;
        output.append(buffer.data(), static_cast<std::size_t>(count));
    }
    close(pipe_fd[0]);
    int status{};
    REQUIRE(waitpid(child, &status, 0) == child);
    return {WIFEXITED(status) ? WEXITSTATUS(status) : 255, std::move(output)};
}

std::vector<std::string> split(const std::string &line, char separator)
{
    std::vector<std::string> result;
    std::size_t start{};
    while (true) {
        const auto end = line.find(separator, start);
        result.push_back(line.substr(start, end - start));
        if (end == std::string::npos) return result;
        start = end + 1;
    }
}

std::vector<std::string> lines(const std::string &value)
{
    auto result = split(value, '\n');
    if (!result.empty() && result.back().empty()) result.pop_back();
    return result;
}

std::pair<std::string, std::string> key_pair(const std::string &line)
{
    const auto items = split(line, ' ');
    REQUIRE(items.size() == 2);
    REQUIRE(items[0].rfind("pub:", 0) == 0);
    REQUIRE(items[1].rfind("sec:", 0) == 0);
    return {items[0].substr(4), items[1].substr(4)};
}

std::string sha256_hex(const std::string &value)
{
    std::array<unsigned char, crypto_hash_sha256_BYTES> digest{};
    crypto_hash_sha256(digest.data(),
        reinterpret_cast<const unsigned char *>(value.data()), value.size());
    static constexpr char hex[] = "0123456789abcdef";
    std::string result;
    for (const auto byte : digest) { result += hex[byte >> 4U]; result += hex[byte & 0x0fU]; }
    return result;
}

std::string write_bundle(const std::filesystem::path &path,
                         const std::string &bls, const std::string &tls,
                         const std::string &issuer)
{
    const auto bls_rows = lines(bls), tls_rows = lines(tls);
    REQUIRE(bls_rows.size() == 31);
    REQUIRE(tls_rows.size() == 32);
    std::string bundle;
    std::string public_rows = "kauri-operator-capacity-identity-public-v1\n";
    for (std::size_t id = 0; id < bls_rows.size(); ++id) {
        const auto [pub, sec] = key_pair(bls_rows.at(id));
        bundle += "bls:" + std::to_string(id) + ":" + pub + ":" + sec + "\n";
        public_rows += "bls:" + std::to_string(id) + ":" + pub + "\n";
    }
    for (std::size_t id = 0; id < tls_rows.size(); ++id) {
        const auto values = split(tls_rows.at(id), ' ');
        REQUIRE(values.size() == 3);
        REQUIRE(values[0].rfind("crt:", 0) == 0);
        REQUIRE(values[1].rfind("sec:", 0) == 0);
        REQUIRE(values[2].rfind("cid:", 0) == 0);
        const auto crt = values[0].substr(4), sec = values[1].substr(4), cid = values[2].substr(4);
        bundle += "tls:" + std::to_string(id) + ":" + crt + ":" + sec + ":" + cid + "\n";
        public_rows += "tls:" + std::to_string(id) + ":" + crt + ":" + cid + "\n";
    }
    const auto issuer_rows = lines(issuer);
    REQUIRE(issuer_rows.size() == 1);
    const auto [issuer_pub, issuer_sec] = key_pair(issuer_rows.front());
    bundle += "issuer:" + issuer_pub + ":" + issuer_sec + "\n";
    public_rows += "issuer:" + issuer_pub + "\n";
    const int fd = open(path.c_str(), O_WRONLY | O_CREAT | O_EXCL | O_NOFOLLOW, 0600);
    REQUIRE(fd >= 0);
    REQUIRE(write(fd, bundle.data(), bundle.size()) == static_cast<ssize_t>(bundle.size()));
    REQUIRE(fsync(fd) == 0);
    REQUIRE(close(fd) == 0);
    return sha256_hex(public_rows);
}

std::vector<std::string> command(const std::filesystem::path &bundle,
                                 const std::filesystem::path &receipt,
                                 const std::string &fingerprint)
{
    return {KAURI_OPERATOR_CAPACITY_IDENTITY_PARITY_VERIFY,
        "--identity-bundle", bundle.string(), "--source-revision", std::string(40, 'a'),
        "--expected-public-fingerprint", fingerprint, "--output", receipt.string()};
}

void write_private(const std::filesystem::path &path, const std::string &value)
{
    const int fd = open(path.c_str(), O_WRONLY | O_CREAT | O_EXCL | O_NOFOLLOW, 0600);
    REQUIRE(fd >= 0);
    REQUIRE(write(fd, value.data(), value.size()) == static_cast<ssize_t>(value.size()));
    REQUIRE(fsync(fd) == 0);
    REQUIRE(close(fd) == 0);
}

std::string corrupt_final_field(std::string value, const std::size_t row)
{
    std::size_t start{};
    for (std::size_t index = 0; index < row; ++index) {
        start = value.find('\n', start) + 1;
        REQUIRE(start != 0);
    }
    const auto end = value.find('\n', start);
    REQUIRE(end != std::string::npos);
    const auto colon = value.rfind(':', end - 1);
    REQUIRE(colon != std::string::npos);
    REQUIRE(colon >= start);
    value.replace(colon + 1, end - colon - 1, std::string(64, '0'));
    return value;
}

std::string replace_row(std::string value, const std::size_t row,
                        const std::string &replacement)
{
    std::size_t start{};
    for (std::size_t index = 0; index < row; ++index) {
        start = value.find('\n', start) + 1;
        REQUIRE(start != 0);
    }
    const auto end = value.find('\n', start);
    REQUIRE(end != std::string::npos);
    value.replace(start, end - start, replacement);
    return value;
}
} // namespace

TEST_CASE("operator-capacity identity parity CLI verifies real generated key bundles",
          "[operator-capacity][identity-parity][cli]")
{
    TemporaryDirectory directory;
    const auto bls = capture({KAURI_HOTSTUFF_KEYGEN_PATH,
        "--secure-bls-preallocation", "--num", "31", "--algo", "bls"});
    REQUIRE(bls.status == 0);
    const auto tls = capture({KAURI_HOTSTUFF_TLS_KEYGEN_PATH, "--num", "32"});
    REQUIRE(tls.status == 0);
    const auto issuer = capture({KAURI_HOTSTUFF_KEYGEN_PATH,
        "--num", "1", "--algo", "secp256k1"});
    REQUIRE(issuer.status == 0);

    const auto bundle = directory.path / "identities.bundle";
    const auto fingerprint = write_bundle(bundle, bls.output, tls.output, issuer.output);
    const auto receipt = directory.path / "receipt.json";
    const auto verified = capture(command(bundle, receipt, fingerprint));
    INFO(verified.output);
    REQUIRE(verified.status == 0);
    REQUIRE(std::filesystem::is_regular_file(receipt));
    std::ifstream input(receipt, std::ios::binary);
    const std::string contents{std::istreambuf_iterator<char>(input), {}};
    CHECK(contents.find("\"verdict\":\"NATIVE_IDENTITY_PARITY_VERIFIED_NO_EXECUTION\"") != std::string::npos);
    CHECK(contents.find("\"source_revision\":\"" + std::string(40, 'a') + "\"") != std::string::npos);
    CHECK(contents.find("\"public_identity_fingerprint\":\"" + fingerprint + "\"") != std::string::npos);
    CHECK(contents.find(" sec:") == std::string::npos);

    const auto text = [&] { std::ifstream source(bundle, std::ios::binary); return std::string{std::istreambuf_iterator<char>(source), {}}; }();
    for (const auto [label, row] : std::vector<std::pair<std::string, std::size_t>>{
             {"bls", 0}, {"tls", 31}, {"issuer", 63}}) {
        const auto corrupted = directory.path / ("corrupted-" + label + ".bundle");
        write_private(corrupted, corrupt_final_field(text, row));
        const auto failed_receipt = directory.path / ("failed-" + label + ".json");
        CHECK(capture(command(corrupted, failed_receipt, fingerprint)).status == 2);
        CHECK_FALSE(std::filesystem::exists(failed_receipt));
    }

    const auto first_tls = split(lines(text).at(31), ':');
    REQUIRE(first_tls.size() == 5);
    const auto reused_key = salticidae::PKey::create_privkey_from_der(
        hotstuff::from_hex(first_tls.at(3)));
    const auto different_certificate =
        salticidae::X509::create_self_signed_from_pubkey(
            reused_key, "PT", "w18-reused-tls-key");
    const auto repeated_key_row = std::string{"tls:1:"} +
        salticidae::get_hex(different_certificate.get_der()) + ":" +
        first_tls.at(3) + ":" +
        salticidae::get_hex(salticidae::get_hash(
            different_certificate.get_der()));
    const auto repeated_key_bundle = directory.path / "repeated-tls-key.bundle";
    write_private(repeated_key_bundle, replace_row(text, 32, repeated_key_row));
    const auto repeated_key_receipt = directory.path / "repeated-tls-key.json";
    CHECK(capture(command(repeated_key_bundle, repeated_key_receipt,
                          fingerprint)).status == 2);
    CHECK_FALSE(std::filesystem::exists(repeated_key_receipt));
}
