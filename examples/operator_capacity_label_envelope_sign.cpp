/* No-launch producer for a signed N=31 operator-capacity Stage-A envelope. */
#include <algorithm>
#include <array>
#include <charconv>
#include <cerrno>
#include <cctype>
#include <cstdint>
#include <filesystem>
#include <iostream>
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
#include "hotstuff/operator_capacity_policy.h"

namespace {
constexpr std::uint32_t kN = 31, kTrees = 21, kFanout = 5, kPipe = 2;
constexpr std::size_t kMaximumTreeBytes = 8 * 1024;
constexpr std::size_t kMaximumSnapshotBytes = 16 * 1024;
constexpr std::size_t kMaximumPrivateKeyBytes = 128;

struct Arguments {
    std::filesystem::path tree, snapshot, private_key, wire_output, receipt_output;
    std::string issuer_id, issuer_reference, arm, valid_from, valid_until,
        source_revision;
};

bool lowercase_hex(std::string_view value, std::size_t size) {
    return value.size() == size && std::all_of(value.begin(), value.end(),
        [](unsigned char character) { return (character >= '0' && character <= '9') ||
            (character >= 'a' && character <= 'f'); });
}

std::uint64_t decimal(std::string_view value, const char *label) {
    std::uint64_t result{};
    const auto parsed = std::from_chars(value.data(), value.data() + value.size(), result);
    if (value.empty() || parsed.ec != std::errc{} || parsed.ptr != value.data() + value.size())
        throw std::invalid_argument(std::string("invalid ") + label);
    return result;
}

bool canonical_decimal(std::string_view value) {
    return !value.empty() && (value.size() == 1 || value.front() != '0') &&
        std::all_of(value.begin(), value.end(),
            [](unsigned char character) { return character >= '0' && character <= '9'; });
}

bool safe_reference(std::string_view value) {
    return !value.empty() && std::all_of(value.begin(), value.end(),
        [](unsigned char character) { return std::isalnum(character) || character == '-' || character == '_'; });
}

std::string sha256_hex(const hotstuff::bytearray_t &bytes) {
    std::array<unsigned char, crypto_hash_sha256_BYTES> digest{};
    crypto_hash_sha256(digest.data(), bytes.data(), bytes.size());
    static constexpr char hex[] = "0123456789abcdef";
    std::string result; result.reserve(digest.size() * 2);
    for (const auto byte : digest) { result += hex[byte >> 4U]; result += hex[byte & 0x0fU]; }
    return result;
}

hotstuff::bytearray_t read_regular(const std::filesystem::path &path,
                                   std::size_t maximum, const char *label,
                                   bool require_private_mode = false) {
    const int fd = open(path.c_str(), O_RDONLY | O_CLOEXEC | O_NOFOLLOW | O_NONBLOCK);
    if (fd < 0) throw std::runtime_error(std::string(label) + " must be a regular non-symlink file");
    try {
        struct stat before{};
        if (fstat(fd, &before) != 0 || !S_ISREG(before.st_mode) || before.st_size < 0 ||
            (require_private_mode && ((before.st_mode & 0777) != 0600 ||
                                      before.st_uid != geteuid())))
            throw std::runtime_error(std::string(label) + " has unsafe metadata");
        if (static_cast<std::uintmax_t>(before.st_size) > maximum)
            throw std::runtime_error(std::string(label) + " exceeds byte limit");
        hotstuff::bytearray_t bytes(static_cast<std::size_t>(before.st_size));
        for (std::size_t offset = 0; offset < bytes.size();) {
            const auto count = read(fd, bytes.data() + offset, bytes.size() - offset);
            if (count < 0 && errno == EINTR) continue;
            if (count <= 0) throw std::runtime_error(std::string("cannot read ") + label);
            offset += static_cast<std::size_t>(count);
        }
        std::uint8_t extra{};
        for (;;) {
            const auto count = read(fd, &extra, 1);
            if (count < 0 && errno == EINTR) continue;
            if (count != 0) throw std::runtime_error(std::string(label) + " changed during read");
            break;
        }
        struct stat after{};
        if (fstat(fd, &after) != 0 || after.st_dev != before.st_dev ||
            after.st_ino != before.st_ino || after.st_size != before.st_size ||
            after.st_mtime != before.st_mtime || after.st_ctime != before.st_ctime)
            throw std::runtime_error(std::string(label) + " changed during read");
        if (close(fd) != 0) throw std::runtime_error(std::string("cannot close ") + label);
        return bytes;
    } catch (...) { close(fd); throw; }
}

void write_exclusive(const std::filesystem::path &path, const hotstuff::bytearray_t &bytes,
                     const char *label) {
    if (path.empty() || bytes.empty()) throw std::invalid_argument(std::string("empty ") + label);
    const int fd = open(path.c_str(), O_WRONLY | O_CREAT | O_EXCL | O_NOFOLLOW, 0600);
    if (fd < 0) throw std::runtime_error(std::string(label) + " must be a fresh path");
    try {
        for (std::size_t offset = 0; offset < bytes.size();) {
            const auto count = write(fd, bytes.data() + offset, bytes.size() - offset);
            if (count < 0 && errno == EINTR) continue;
            if (count <= 0) throw std::runtime_error(std::string("cannot write ") + label);
            offset += static_cast<std::size_t>(count);
        }
        if (fsync(fd) != 0 || close(fd) != 0) throw std::runtime_error(std::string("cannot finalize ") + label);
    } catch (...) { close(fd); unlink(path.c_str()); throw; }
}

void write_receipt(const std::filesystem::path &path, const std::string &text) {
    write_exclusive(path, hotstuff::bytearray_t(text.begin(), text.end()), "receipt output");
}

std::uint64_t raw_now() {
    timespec value{};
    if (clock_gettime(CLOCK_MONOTONIC_RAW, &value) != 0 || value.tv_sec < 0 || value.tv_nsec < 0)
        throw std::runtime_error("cannot sample CLOCK_MONOTONIC_RAW");
    const auto seconds = static_cast<std::uint64_t>(value.tv_sec);
    if (seconds > UINT64_MAX / 1'000'000'000ULL) throw std::runtime_error("CLOCK_MONOTONIC_RAW overflow");
    return seconds * 1'000'000'000ULL + static_cast<std::uint64_t>(value.tv_nsec);
}

Arguments arguments(int argc, char **argv) {
    if (argc != 23) throw std::invalid_argument("invalid argument count");
    Arguments result;
    for (int index = 1; index < argc; index += 2) {
        const std::string flag(argv[index]), value(argv[index + 1]);
        if (flag == "--epoch0-tree-file") result.tree = value;
        else if (flag == "--capacity-snapshot-wire") result.snapshot = value;
        else if (flag == "--issuer-private-key-file") result.private_key = value;
        else if (flag == "--issuer-id") result.issuer_id = value;
        else if (flag == "--issuer-reference") result.issuer_reference = value;
        else if (flag == "--arm") result.arm = value;
        else if (flag == "--valid-from-monotonic-raw-ns") result.valid_from = value;
        else if (flag == "--valid-until-monotonic-raw-ns") result.valid_until = value;
        else if (flag == "--source-revision") result.source_revision = value;
        else if (flag == "--wire-output") result.wire_output = value;
        else if (flag == "--receipt-output") result.receipt_output = value;
        else throw std::invalid_argument("unknown argument: " + flag);
    }
    const auto issuer = decimal(result.issuer_id, "issuer id");
    const auto from = decimal(result.valid_from, "valid from");
    const auto until = decimal(result.valid_until, "valid until");
    if (result.tree.empty() || result.snapshot.empty() || result.private_key.empty() ||
        result.wire_output.empty() || result.receipt_output.empty() ||
        result.wire_output == result.receipt_output || issuer == 0 || issuer > UINT32_MAX ||
        !canonical_decimal(result.issuer_id) || !canonical_decimal(result.valid_from) ||
        !canonical_decimal(result.valid_until) || !safe_reference(result.issuer_reference) ||
        result.issuer_reference.size() > 128 ||
        from == 0 || until <= from || !lowercase_hex(result.source_revision, 40) ||
        (result.arm != "fast_priority_treatment" && result.arm != "exact_copy_sham"))
        throw std::invalid_argument("missing or malformed producer argument");
    return result;
}

std::vector<hotstuff::ReplicaID> membership() {
    std::vector<hotstuff::ReplicaID> result; result.reserve(kN);
    for (hotstuff::ReplicaID id = 0; id < kN; ++id) result.push_back(id);
    return result;
}

void validate_trees(const std::vector<hotstuff::EpochTreeDefinition> &trees) {
    if (trees.size() != kTrees) throw std::runtime_error("E0 must contain exactly 21 trees");
    for (std::uint32_t index = 0; index < kTrees; ++index) {
        const auto &tree = trees.at(index);
        if (tree.tree_id != index || tree.fanout != kFanout || tree.pipeline_stretch != kPipe ||
            tree.members_breadth_first.size() != kN || !tree.wait_exempt_leaves.empty())
            throw std::runtime_error("E0 differs from frozen N31/F5/P2 topology");
    }
}

hotstuff::OperatorCapacityArm arm(const std::string &value) {
    return value == "fast_priority_treatment"
        ? hotstuff::OperatorCapacityArm::fast_priority_treatment
        : hotstuff::OperatorCapacityArm::exact_copy_sham;
}

std::string receipt(const Arguments &args, const hotstuff::bytearray_t &wire,
                    const hotstuff::uint256_t &digest, const hotstuff::uint256_t &epoch0,
                    const hotstuff::uint256_t &topology, const std::string &public_fingerprint) {
    return "{\"schema_version\":1,\"kind\":\"kauri-operator-capacity-native-envelope-production-receipt-v1\","
        "\"verdict\":\"NATIVE_ENVELOPE_PRODUCED_NO_EXECUTION\",\"stage_a_wire_sha256\":\"" +
        sha256_hex(wire) + "\",\"stage_a_semantic_digest\":\"" + digest.to_hex() +
        "\",\"issuer_id\":" + args.issuer_id + ",\"issuer_reference\":\"" +
        args.issuer_reference + "\",\"issuer_public_key_fingerprint\":\"" + public_fingerprint +
        "\",\"arm\":\"" + args.arm + "\",\"source_revision\":\"" + args.source_revision +
        "\",\"epoch0_digest\":\"" + epoch0.to_hex() + "\",\"epoch0_topology_digest\":\"" +
        topology.to_hex() + "\",\"valid_from_monotonic_raw_ns\":" + args.valid_from +
        ",\"valid_until_monotonic_raw_ns\":" + args.valid_until + "}\n";
}
} // namespace

int main(int argc, char **argv) {
    try {
        const auto args = arguments(argc, argv);
        const auto members = membership();
        const auto tree_bytes = read_regular(args.tree, kMaximumTreeBytes, "E0 tree file");
        const auto trees = hotstuff::parse_adaptive_v2_epoch_zero_tree_bytes(tree_bytes, members);
        validate_trees(trees);
        const auto input = hotstuff::adaptive_v2_epoch_zero_input(members, trees);
        const hotstuff::AdaptationEpochId epoch0{0, hotstuff::compute_epoch_digest(input)};
        const auto topology = hotstuff::operator_capacity_baseline_topology_digest(epoch0, members, trees);
        const auto snapshot_wire = read_regular(args.snapshot, kMaximumSnapshotBytes, "capacity snapshot");
        constexpr hotstuff::OperatorCapacitySnapshotWireLimits snapshot_limits{kMaximumSnapshotBytes};
        const auto decoded = hotstuff::decode_operator_capacity_snapshot(snapshot_wire, snapshot_limits);
        if (!decoded || decoded.value->issuer_reference != args.issuer_reference ||
            decoded.value->predecessor != epoch0 ||
            decoded.value->valid_from_monotonic_ns != decimal(args.valid_from, "valid from") ||
            decoded.value->valid_until_monotonic_ns != decimal(args.valid_until, "valid until"))
            throw std::runtime_error("capacity snapshot does not bind the requested Stage-A context");
        std::size_t slow{};
        for (const auto &label : decoded.value->labels) {
            if (label.capacity == hotstuff::OperatorCapacityClass::slow) ++slow;
            else if (label.capacity != hotstuff::OperatorCapacityClass::fast)
                throw std::runtime_error("capacity snapshot has an invalid label");
        }
        if (decoded.value->labels.size() != kN || slow != 6)
            throw std::runtime_error("capacity snapshot must label exactly six of N31 replicas slow");
        for (std::size_t index = 0; index < decoded.value->labels.size(); ++index) {
            const auto &label = decoded.value->labels[index];
            if (label.replica_id != index ||
                label.capacity != (index < 6 ? hotstuff::OperatorCapacityClass::slow :
                                               hotstuff::OperatorCapacityClass::fast))
                throw std::runtime_error("capacity snapshot must use frozen slow IDs 0 through 5");
        }
        const auto now = raw_now();
        if (now < decoded.value->valid_from_monotonic_ns || now > decoded.value->valid_until_monotonic_ns)
            throw std::runtime_error("capacity snapshot is outside its RAW validity window");
        const auto private_bytes = read_regular(args.private_key, kMaximumPrivateKeyBytes,
                                                "issuer private key", true);
        const std::string private_hex(private_bytes.begin(), private_bytes.end());
        if (!lowercase_hex(private_hex, 64))
            throw std::runtime_error("issuer private key must be exactly lowercase hexadecimal");
        hotstuff::PrivKeySecp256k1 key(hotstuff::from_hex(private_hex));
        const hotstuff::PubKeySecp256k1 public_key(key);
        hotstuff::DataStream public_stream; public_key.serialize(public_stream);
        const auto public_bytes = static_cast<hotstuff::bytearray_t>(std::move(public_stream));
        const auto envelope = hotstuff::sign_operator_capacity_label_envelope(
            members, epoch0, topology, arm(args.arm), *decoded.value,
            static_cast<std::uint32_t>(decimal(args.issuer_id, "issuer id")), key);
        constexpr hotstuff::OperatorCapacityLabelEnvelopeWireLimits envelope_limits{kMaximumSnapshotBytes};
        const auto wire = hotstuff::encode_operator_capacity_label_envelope(envelope, envelope_limits);
        write_exclusive(args.wire_output, wire, "Stage-A wire output");
        write_receipt(args.receipt_output, receipt(args, wire,
            hotstuff::operator_capacity_label_envelope_digest(envelope), epoch0.epoch_digest,
            topology, sha256_hex(public_bytes)));
        return 0;
    } catch (const std::exception &error) {
        std::cerr << "operator-capacity-label-envelope-sign: " << error.what() << '\n';
        return 2;
    }
}
