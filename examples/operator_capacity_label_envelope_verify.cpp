/* No-launch verifier for a signed N=31 operator-capacity Stage-A envelope. */
#include <algorithm>
#include <array>
#include <charconv>
#include <cstdint>
#include <cerrno>
#include <fcntl.h>
#include <filesystem>
#include <iostream>
#include <optional>
#include <set>
#include <stdexcept>
#include <string>
#include <string_view>
#include <sys/stat.h>
#include <time.h>
#include <unistd.h>
#include <vector>

#include <sodium.h>

#include "hotstuff/configuration.h"
#include "hotstuff/operator_capacity_label_envelope.h"

namespace {
constexpr std::size_t kMaximumTreeFileBytes = 8 * 1024;
constexpr std::size_t kMaximumEnvelopeBytes = 32 * 1024;
constexpr std::uint32_t kN = 31, kTrees = 21, kFanout = 5, kPipe = 2;
constexpr char kReceiptKind[] =
    "kauri-operator-capacity-native-envelope-verification-receipt-v1";
constexpr char kReceiptVerdict[] = "NATIVE_ENVELOPE_VERIFIED_NO_EXECUTION";

struct Arguments {
    std::filesystem::path tree_file, envelope_file, output;
    std::string issuer_id, issuer_reference, issuer_public_key_hex;
    std::string issuer_public_key_fingerprint, approved_capacity_digest;
    std::string arm, source_revision;
    std::optional<std::uint64_t> replay_at_monotonic_raw_ns;
};

bool lowercase_hex(std::string_view value, std::size_t size)
{
    return value.size() == size && std::all_of(value.begin(), value.end(),
        [](unsigned char character) {
            return (character >= '0' && character <= '9') ||
                   (character >= 'a' && character <= 'f');
        });
}

std::uint64_t decimal(std::string_view value, const char *name)
{
    std::uint64_t result{};
    const auto parsed = std::from_chars(value.data(), value.data() + value.size(), result);
    if (value.empty() || parsed.ec != std::errc{} ||
        parsed.ptr != value.data() + value.size())
        throw std::invalid_argument(std::string("invalid ") + name);
    return result;
}

std::string json_string(std::string_view value)
{
    std::string result{"\""};
    for (unsigned char character : value) {
        if (character == '\"' || character == '\\') result += '\\';
        if (character < 0x20 || character > 0x7e)
            throw std::invalid_argument("receipt string is not printable ASCII");
        result += static_cast<char>(character);
    }
    return result + '\"';
}

std::string sha256_hex(const hotstuff::bytearray_t &bytes)
{
    std::array<unsigned char, crypto_hash_sha256_BYTES> digest{};
    crypto_hash_sha256(digest.data(), bytes.data(), bytes.size());
    static constexpr char hex[] = "0123456789abcdef";
    std::string result;
    result.reserve(digest.size() * 2);
    for (const auto byte : digest) {
        result += hex[byte >> 4U]; result += hex[byte & 0x0fU];
    }
    return result;
}

hotstuff::bytearray_t bounded_regular_bytes(const std::filesystem::path &path,
                                            std::size_t maximum, const char *label)
{
    // O_NONBLOCK makes FIFO/device rejection happen after open, not after a
    // potentially unbounded wait for a writer. fstat still requires a regular
    // file before any bytes are consumed.
    const int descriptor = open(path.c_str(), O_RDONLY | O_CLOEXEC | O_NOFOLLOW | O_NONBLOCK);
    if (descriptor < 0)
        throw std::runtime_error(std::string(label) + " must be a regular non-symlink file");
    try {
        struct stat metadata {};
        if (fstat(descriptor, &metadata) != 0 || !S_ISREG(metadata.st_mode) ||
            metadata.st_size < 0)
            throw std::runtime_error(std::string(label) + " must be a regular non-symlink file");
        const auto size = static_cast<std::uintmax_t>(metadata.st_size);
        if (size > maximum)
            throw std::runtime_error(std::string(label) + " exceeds its byte limit");
        hotstuff::bytearray_t bytes(static_cast<std::size_t>(size));
        std::size_t offset{};
        while (offset < bytes.size()) {
            const auto count = read(descriptor, bytes.data() + offset, bytes.size() - offset);
            if (count < 0 && errno == EINTR) continue;
            if (count <= 0) throw std::runtime_error(std::string("cannot read ") + label);
            offset += static_cast<std::size_t>(count);
        }
        std::uint8_t extra{};
        for (;;) {
            const auto count = read(descriptor, &extra, 1);
            if (count < 0 && errno == EINTR) continue;
            if (count != 0) throw std::runtime_error(std::string(label) + " changed during read");
            break;
        }
        if (close(descriptor) != 0) throw std::runtime_error(std::string("cannot close ") + label);
        return bytes;
    } catch (...) {
        close(descriptor); throw;
    }
}

void write_exclusive(const std::filesystem::path &path, const std::string &content)
{
    const int descriptor = open(path.c_str(), O_WRONLY | O_CREAT | O_EXCL | O_NOFOLLOW, 0600);
    if (descriptor < 0) throw std::runtime_error("receipt output must be a fresh regular path");
    try {
        std::size_t written{};
        while (written < content.size()) {
            const auto count = write(descriptor, content.data() + written, content.size() - written);
            if (count <= 0) throw std::runtime_error("cannot write receipt output");
            written += static_cast<std::size_t>(count);
        }
        if (fsync(descriptor) != 0 || close(descriptor) != 0)
            throw std::runtime_error("cannot finalize receipt output");
    } catch (...) {
        close(descriptor); unlink(path.c_str()); throw;
    }
}

Arguments arguments(int argc, char **argv)
{
    if (argc != 21 && argc != 23) throw std::invalid_argument("invalid argument count");
    Arguments result;
    std::set<std::string> seen;
    for (int index = 1; index < argc; index += 2) {
        const std::string flag(argv[index]), value(argv[index + 1]);
        if (!seen.insert(flag).second) throw std::invalid_argument("duplicate argument: " + flag);
        if (flag == "--epoch0-tree-file") result.tree_file = value;
        else if (flag == "--stage-a-envelope-wire") result.envelope_file = value;
        else if (flag == "--issuer-id") result.issuer_id = value;
        else if (flag == "--issuer-reference") result.issuer_reference = value;
        else if (flag == "--issuer-public-key-hex") result.issuer_public_key_hex = value;
        else if (flag == "--issuer-public-key-fingerprint") result.issuer_public_key_fingerprint = value;
        else if (flag == "--approved-capacity-digest") result.approved_capacity_digest = value;
        else if (flag == "--arm") result.arm = value;
        else if (flag == "--source-revision") result.source_revision = value;
        else if (flag == "--output") result.output = value;
        else if (flag == "--replay-at-monotonic-raw-ns") {
            const auto tick = decimal(value, "historical verification time");
            if (tick == 0 || std::to_string(tick) != value)
                throw std::invalid_argument("invalid historical verification time");
            result.replay_at_monotonic_raw_ns = tick;
        }
        else throw std::invalid_argument("unknown argument: " + flag);
    }
    if (result.tree_file.empty() || result.envelope_file.empty() || result.output.empty() ||
        result.issuer_reference.empty() || result.issuer_reference.size() > 128 ||
        !lowercase_hex(result.issuer_public_key_hex, 66) ||
        !lowercase_hex(result.issuer_public_key_fingerprint, 64) ||
        !lowercase_hex(result.approved_capacity_digest, 64) ||
        !lowercase_hex(result.source_revision, 40) ||
        (result.arm != "fast_priority_treatment" && result.arm != "exact_copy_sham") ||
        decimal(result.issuer_id, "issuer id") == 0 ||
        decimal(result.issuer_id, "issuer id") > UINT32_MAX)
        throw std::invalid_argument("missing or malformed verifier argument");
    return result;
}

std::vector<hotstuff::ReplicaID> membership()
{
    std::vector<hotstuff::ReplicaID> result;
    result.reserve(kN);
    for (hotstuff::ReplicaID replica = 0; replica < kN; ++replica) result.push_back(replica);
    return result;
}

void validate_n31_trees(const std::vector<hotstuff::EpochTreeDefinition> &trees)
{
    if (trees.size() != kTrees) throw std::runtime_error("E0 tree file must contain exactly 21 trees");
    for (std::uint32_t tree = 0; tree < kTrees; ++tree) {
        const auto &value = trees.at(tree);
        if (value.tree_id != tree || value.fanout != kFanout ||
            value.pipeline_stretch != kPipe || value.members_breadth_first.size() != kN)
            throw std::runtime_error("E0 tree file differs from frozen N31/F5/P2 topology");
    }
}

hotstuff::OperatorCapacityArm parse_arm(const std::string &arm)
{
    return arm == "fast_priority_treatment"
        ? hotstuff::OperatorCapacityArm::fast_priority_treatment
        : hotstuff::OperatorCapacityArm::exact_copy_sham;
}

std::string receipt(const Arguments &args, const std::string &envelope_sha256,
                    const std::string &epoch0_tree_file_sha256,
                    const hotstuff::uint256_t &envelope_digest,
                    const hotstuff::uint256_t &epoch0_digest,
                    const hotstuff::uint256_t &topology_digest,
                    std::uint64_t verification_monotonic_raw_ns)
{
    const auto kind = args.replay_at_monotonic_raw_ns
        ? "kauri-operator-capacity-historical-envelope-verification-receipt-v1" : kReceiptKind;
    const auto verdict = args.replay_at_monotonic_raw_ns
        ? "HISTORICAL_ENVELOPE_VERIFIED_NO_EXECUTION" : kReceiptVerdict;
    return "{\"schema_version\":1,\"kind\":" + json_string(kind) +
        ",\"verdict\":" + json_string(verdict) +
        ",\"envelope_wire_sha256\":" + json_string(envelope_sha256) +
        ",\"envelope_canonical_digest\":" + json_string(envelope_digest.to_hex()) +
        ",\"approved_capacity_digest\":" + json_string(args.approved_capacity_digest) +
        ",\"issuer_id\":" + args.issuer_id +
        ",\"issuer_reference\":" + json_string(args.issuer_reference) +
        ",\"issuer_public_key_fingerprint\":" + json_string(args.issuer_public_key_fingerprint) +
        ",\"arm\":" + json_string(args.arm) +
        ",\"source_revision\":" + json_string(args.source_revision) +
        ",\"verification_monotonic_raw_ns\":" + std::to_string(verification_monotonic_raw_ns) +
        ",\"epoch0_tree_file_sha256\":" + json_string(epoch0_tree_file_sha256) +
        ",\"epoch0_consensus_digest\":" + json_string(epoch0_digest.to_hex()) +
        ",\"epoch0_topology_digest\":" + json_string(topology_digest.to_hex()) + "}\n";
}

std::uint64_t monotonic_raw_now_ns()
{
    timespec value{};
    if (clock_gettime(CLOCK_MONOTONIC_RAW, &value) != 0 || value.tv_sec < 0 || value.tv_nsec < 0)
        throw std::runtime_error("cannot sample CLOCK_MONOTONIC_RAW");
    const auto seconds = static_cast<std::uint64_t>(value.tv_sec);
    if (seconds > UINT64_MAX / 1'000'000'000ULL)
        throw std::runtime_error("CLOCK_MONOTONIC_RAW overflow");
    return seconds * 1'000'000'000ULL + static_cast<std::uint64_t>(value.tv_nsec);
}
} // namespace

int main(int argc, char **argv)
{
    try {
        const auto args = arguments(argc, argv);
        const auto tree_bytes = bounded_regular_bytes(args.tree_file, kMaximumTreeFileBytes, "E0 tree file");
        const auto members = membership();
        const auto trees = hotstuff::parse_adaptive_v2_epoch_zero_tree_bytes(tree_bytes, members);
        validate_n31_trees(trees);
        const auto input = hotstuff::adaptive_v2_epoch_zero_input(members, trees);
        const hotstuff::AdaptationEpochId epoch0{0, hotstuff::compute_epoch_digest(input)};
        const auto topology_digest = hotstuff::operator_capacity_baseline_topology_digest(epoch0, members, trees);
        const auto envelope_bytes = bounded_regular_bytes(args.envelope_file, kMaximumEnvelopeBytes, "Stage-A envelope");
        constexpr hotstuff::OperatorCapacityLabelEnvelopeWireLimits limits{kMaximumEnvelopeBytes};
        const auto decoded = hotstuff::decode_operator_capacity_label_envelope(envelope_bytes, limits);
        if (!decoded || decoded.value->arm != parse_arm(args.arm))
            throw std::runtime_error("Stage-A envelope is malformed or has the wrong arm");
        const auto issuer_id = static_cast<std::uint32_t>(decimal(args.issuer_id, "issuer id"));
        const hotstuff::PubKeySecp256k1 public_key(hotstuff::from_hex(args.issuer_public_key_hex));
        hotstuff::DataStream public_key_stream;
        public_key.serialize(public_key_stream);
        const auto public_key_bytes = static_cast<hotstuff::bytearray_t>(std::move(public_key_stream));
        if (sha256_hex(public_key_bytes) != args.issuer_public_key_fingerprint)
            throw std::runtime_error("issuer public key does not match pinned fingerprint");
        const hotstuff::OperatorCapacityLabelEnvelopeIssuer issuer{
            issuer_id, args.issuer_reference,
            hotstuff::uint256_t(hotstuff::from_hex(args.approved_capacity_digest)), public_key};
        const auto now_ns = monotonic_raw_now_ns();
        if (args.replay_at_monotonic_raw_ns && *args.replay_at_monotonic_raw_ns > now_ns)
            throw std::runtime_error("historical verification time is in the future");
        const auto verification_monotonic_raw_ns = args.replay_at_monotonic_raw_ns.value_or(now_ns);
        if (!hotstuff::verify_operator_capacity_label_envelope(
                *decoded.value, issuer, members, epoch0, topology_digest,
                verification_monotonic_raw_ns))
            throw std::runtime_error("Stage-A envelope failed native verification");
        write_exclusive(args.output, receipt(args, sha256_hex(envelope_bytes),
            sha256_hex(tree_bytes),
            hotstuff::operator_capacity_label_envelope_digest(*decoded.value),
            epoch0.epoch_digest, topology_digest, verification_monotonic_raw_ns));
        return 0;
    } catch (const std::exception &error) {
        std::cerr << "operator-capacity-label-envelope-verify: " << error.what() << '\n';
        return 2;
    }
}
