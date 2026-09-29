/**
 * No-launch parity check for the private identity bundle used by W18 N31.
 *
 * The input bundle is deliberately private (owner-only 0600); the receipt
 * contains only hashes, counts, the source revision, and public fingerprint.
 */
#include <array>
#include <algorithm>
#include <cerrno>
#include <cstdint>
#include <fcntl.h>
#include <iostream>
#include <set>
#include <stdexcept>
#include <string>
#include <sys/stat.h>
#include <unistd.h>
#include <vector>

#include <sodium.h>

#include "hotstuff/crypto.h"

namespace {
constexpr std::size_t kN = 31;
constexpr std::size_t kMaximumBundleBytes = 256 * 1024;
constexpr const char *kReceiptKind =
    "kauri-operator-capacity-native-identity-parity-receipt-v1";
constexpr const char *kReceiptVerdict =
    "NATIVE_IDENTITY_PARITY_VERIFIED_NO_EXECUTION";

struct Arguments {
    std::string bundle_path;
    std::string source_revision;
    std::string expected_fingerprint;
    std::string output_path;
};

struct BundleWiper {
    std::string &value;
    ~BundleWiper() { if (!value.empty()) sodium_memzero(value.data(), value.size()); }
};

bool lower_hex(const std::string &value, const std::size_t size)
{
    return value.size() == size && std::all_of(
        value.begin(), value.end(), [](const unsigned char character) {
            return (character >= '0' && character <= '9') ||
                   (character >= 'a' && character <= 'f');
        });
}

std::string hex(const hotstuff::bytearray_t &bytes)
{
    static constexpr char alphabet[] = "0123456789abcdef";
    std::string result;
    result.reserve(bytes.size() * 2);
    for (const auto byte : bytes) {
        result.push_back(alphabet[byte >> 4U]);
        result.push_back(alphabet[byte & 0x0fU]);
    }
    return result;
}

template <typename Key>
std::string serialized_hex(const Key &key)
{
    hotstuff::DataStream stream;
    key.serialize(stream);
    return hex(static_cast<hotstuff::bytearray_t>(std::move(stream)));
}

std::string sha256_hex(const std::string &value)
{
    std::array<unsigned char, crypto_hash_sha256_BYTES> digest{};
    crypto_hash_sha256(digest.data(),
        reinterpret_cast<const unsigned char *>(value.data()), value.size());
    hotstuff::bytearray_t bytes(digest.begin(), digest.end());
    return hex(bytes);
}

std::string read_private_bundle(const std::string &path)
{
    const int fd = open(path.c_str(), O_RDONLY | O_CLOEXEC | O_NOFOLLOW | O_NONBLOCK);
    if (fd < 0) throw std::runtime_error("identity bundle cannot be opened safely");
    try {
        struct stat info {};
        if (fstat(fd, &info) != 0 || !S_ISREG(info.st_mode) ||
            info.st_uid != geteuid() || (info.st_mode & 0777) != 0600 ||
            info.st_size < 0 || static_cast<std::size_t>(info.st_size) > kMaximumBundleBytes)
            throw std::runtime_error("identity bundle is not an owner-only bounded regular file");
        std::string result(static_cast<std::size_t>(info.st_size), '\0');
        std::size_t offset{};
        while (offset < result.size()) {
            const auto count = read(fd, result.data() + offset, result.size() - offset);
            if (count <= 0) throw std::runtime_error("identity bundle changed while reading");
            offset += static_cast<std::size_t>(count);
        }
        char extra{};
        if (read(fd, &extra, 1) != 0)
            throw std::runtime_error("identity bundle changed while reading");
        close(fd);
        return result;
    } catch (...) {
        close(fd);
        throw;
    }
}

std::vector<std::string> fields(const std::string &line)
{
    std::vector<std::string> result;
    std::size_t start{};
    while (true) {
        const auto separator = line.find(':', start);
        result.push_back(line.substr(start, separator - start));
        if (separator == std::string::npos) break;
        start = separator + 1;
    }
    return result;
}

std::uint32_t decimal_id(const std::string &value)
{
    if (value.empty() || (value.size() > 1 && value.front() == '0'))
        throw std::runtime_error("identity bundle replica ID is invalid");
    std::uint64_t result{};
    for (const auto character : value) {
        if (character < '0' || character > '9' || result > 1000000U)
            throw std::runtime_error("identity bundle replica ID is invalid");
        result = result * 10U + static_cast<std::uint64_t>(character - '0');
    }
    return static_cast<std::uint32_t>(result);
}

std::string json_string(const std::string &value)
{
    // Every receipt string is fixed or validated lowercase hex, so this is
    // intentionally narrow rather than accepting arbitrary unescaped input.
    if (!std::all_of(value.begin(), value.end(), [](unsigned char character) {
            return (character >= '0' && character <= '9') ||
                   (character >= 'a' && character <= 'z') ||
                   (character >= 'A' && character <= 'Z') ||
                   character == '-' || character == '_';
        }))
        throw std::runtime_error("receipt string is not safely encodable");
    return "\"" + value + "\"";
}

Arguments arguments(int argc, char **argv)
{
    Arguments result;
    for (int index = 1; index < argc; index += 2) {
        if (index + 1 >= argc) throw std::runtime_error("missing CLI value");
        const std::string option(argv[index]);
        const std::string value(argv[index + 1]);
        std::string *destination = nullptr;
        if (option == "--identity-bundle") destination = &result.bundle_path;
        else if (option == "--source-revision") destination = &result.source_revision;
        else if (option == "--expected-public-fingerprint") destination = &result.expected_fingerprint;
        else if (option == "--output") destination = &result.output_path;
        else throw std::runtime_error("unsupported CLI option");
        if (!destination->empty() || value.empty()) throw std::runtime_error("duplicate or empty CLI value");
        *destination = value;
    }
    if (result.bundle_path.empty() || result.output_path.empty() ||
        !lower_hex(result.source_revision, 40) ||
        !lower_hex(result.expected_fingerprint, 64))
        throw std::runtime_error("identity parity CLI arguments are invalid");
    return result;
}

std::string verify_bundle(std::string &bundle)
{
    std::string canonical = "kauri-operator-capacity-identity-public-v1\n";
    std::set<std::string> bls_public, tls_cids, tls_public_keys;
    std::size_t offset{};
    const auto next_line = [&bundle, &offset]() -> std::string {
        const auto end = bundle.find('\n', offset);
        if (end == std::string::npos) throw std::runtime_error("identity bundle line termination is invalid");
        const auto result = bundle.substr(offset, end - offset);
        offset = end + 1;
        return result;
    };
    for (std::uint32_t id = 0; id < kN; ++id) {
        const auto row = fields(next_line());
        if (row.size() != 4 || row[0] != "bls" || decimal_id(row[1]) != id ||
            !lower_hex(row[2], 96) || !lower_hex(row[3], 64))
            throw std::runtime_error("BLS identity row is invalid");
        const hotstuff::PrivKeyBLS private_key(hotstuff::from_hex(row[3]));
        const hotstuff::PubKeyBLS public_key(private_key);
        if (serialized_hex(public_key) != row[2] || !bls_public.insert(row[2]).second)
            throw std::runtime_error("BLS identity parity failed");
        canonical += "bls:" + std::to_string(id) + ":" + row[2] + "\n";
    }
    for (std::uint32_t id = 0; id <= kN; ++id) {
        const auto row = fields(next_line());
        if (row.size() != 5 || row[0] != "tls" || decimal_id(row[1]) != id ||
            !lower_hex(row[2], row[2].size()) || !lower_hex(row[3], row[3].size()) ||
            !lower_hex(row[4], 64))
            throw std::runtime_error("TLS identity row is invalid");
        const auto certificate = salticidae::X509::create_from_der(hotstuff::from_hex(row[2]));
        const auto private_key = salticidae::PKey::create_privkey_from_der(hotstuff::from_hex(row[3]));
        const auto tls_public_key = hex(private_key.get_pubkey_der());
        if (hex(certificate.get_pubkey().get_pubkey_der()) != tls_public_key ||
            hex(salticidae::get_hash(certificate.get_der())) != row[4] ||
            !tls_cids.insert(row[4]).second ||
            !tls_public_keys.insert(tls_public_key).second)
            throw std::runtime_error("TLS identity parity failed");
        canonical += "tls:" + std::to_string(id) + ":" + row[2] + ":" + row[4] + "\n";
    }
    const auto row = fields(next_line());
    if (row.size() != 3 || row[0] != "issuer")
        throw std::runtime_error("issuer identity row format is invalid");
    if (!lower_hex(row[1], 66) || !lower_hex(row[2], 64))
        throw std::runtime_error("issuer identity key encoding is invalid");
    if (offset != bundle.size())
        throw std::runtime_error("identity bundle has trailing rows");
    const hotstuff::PrivKeySecp256k1 private_key(hotstuff::from_hex(row[2]));
    const hotstuff::PubKeySecp256k1 public_key(private_key);
    if (serialized_hex(public_key) != row[1])
        throw std::runtime_error("issuer identity parity failed");
    canonical += "issuer:" + row[1] + "\n";
    return sha256_hex(canonical);
}

void write_receipt(const Arguments &args, const std::string &bundle_sha,
                   const std::string &fingerprint)
{
    const int fd = open(args.output_path.c_str(),
        O_WRONLY | O_CREAT | O_EXCL | O_NOFOLLOW | O_CLOEXEC, 0644);
    if (fd < 0) throw std::runtime_error("receipt output must be fresh");
    const std::string output = std::string{"{\"schema_version\":1,\"kind\":"} +
        json_string(kReceiptKind) + ",\"verdict\":" + json_string(kReceiptVerdict) +
        ",\"source_revision\":" + json_string(args.source_revision) +
        ",\"identity_bundle_sha256\":" + json_string(bundle_sha) +
        ",\"public_identity_fingerprint\":" + json_string(fingerprint) +
        ",\"bls_replicas\":31,\"tls_identities\":32}\n";
    const auto written = write(fd, output.data(), output.size());
    if (written != static_cast<ssize_t>(output.size()) || fsync(fd) != 0) {
        close(fd); unlink(args.output_path.c_str());
        throw std::runtime_error("receipt write failed");
    }
    close(fd);
}
} // namespace

int main(int argc, char **argv)
{
    try {
        const auto args = arguments(argc, argv);
        auto bundle = read_private_bundle(args.bundle_path);
        BundleWiper wipe{bundle};
        const auto bundle_sha = sha256_hex(bundle);
        const auto fingerprint = verify_bundle(bundle);
        if (fingerprint != args.expected_fingerprint)
            throw std::runtime_error("public identity fingerprint differs");
        write_receipt(args, bundle_sha, fingerprint);
        return 0;
    } catch (const std::exception &error) {
        std::cerr << "operator-capacity-identity-parity-verify: "
                  << error.what() << '\n';
        return 2;
    }
}
