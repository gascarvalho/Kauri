/* No-launch verifier for the signed, live-finalized Stage-B decision. */
#include <algorithm>
#include <array>
#include <charconv>
#include <cstdint>
#include <cerrno>
#include <fcntl.h>
#include <filesystem>
#include <iostream>
#include <stdexcept>
#include <string>
#include <string_view>
#include <sys/stat.h>
#include <unistd.h>
#include <vector>

#include <sodium.h>

#include "hotstuff/configuration.h"
#include "hotstuff/operator_capacity_authorization.h"
#include "hotstuff/operator_capacity_label_envelope.h"

namespace {
constexpr std::size_t kMaximumTreeFileBytes = 8 * 1024;
constexpr std::size_t kMaximumAuthorizationBytes = 128 * 1024;
constexpr std::uint32_t kN = 31, kTrees = 21, kFanout = 5, kPipe = 2;

struct Arguments {
    std::filesystem::path tree_file, authorization_file, output;
    std::string issuer_id, issuer_reference, issuer_public_key_hex;
    std::string issuer_public_key_fingerprint, label_issuer_reference;
    std::string approved_capacity_digest, arm, source_revision;
};

bool lower_hex(std::string_view value, std::size_t size)
{
    return value.size() == size && std::all_of(value.begin(), value.end(),
        [](unsigned char character) {
            return (character >= '0' && character <= '9') ||
                   (character >= 'a' && character <= 'f');
        });
}

std::uint32_t positive_id(std::string_view value)
{
    std::uint64_t number{};
    const auto parsed = std::from_chars(value.data(), value.data() + value.size(), number);
    if (value.empty() || parsed.ec != std::errc{} ||
        parsed.ptr != value.data() + value.size() || number == 0 || number > UINT32_MAX)
        throw std::invalid_argument("issuer id is invalid");
    return static_cast<std::uint32_t>(number);
}

std::string json_string(std::string_view value)
{
    std::string result{"\""};
    for (const unsigned char character : value) {
        if (character < 0x20 || character > 0x7e)
            throw std::invalid_argument("receipt string is not printable ASCII");
        if (character == '"' || character == '\\') result.push_back('\\');
        result.push_back(static_cast<char>(character));
    }
    return result + '"';
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
                                            std::size_t maximum)
{
    const int descriptor = open(path.c_str(), O_RDONLY | O_CLOEXEC | O_NOFOLLOW | O_NONBLOCK);
    if (descriptor < 0) throw std::runtime_error("input must be a regular non-symlink file");
    try {
        struct stat metadata {};
        if (fstat(descriptor, &metadata) != 0 || !S_ISREG(metadata.st_mode) ||
            metadata.st_size < 0 || static_cast<std::uintmax_t>(metadata.st_size) > maximum)
            throw std::runtime_error("input is not a bounded regular file");
        hotstuff::bytearray_t bytes(static_cast<std::size_t>(metadata.st_size));
        std::size_t offset{};
        while (offset < bytes.size()) {
            const auto count = read(descriptor, bytes.data() + offset, bytes.size() - offset);
            if (count < 0 && errno == EINTR) continue;
            if (count <= 0) throw std::runtime_error("input changed during read");
            offset += static_cast<std::size_t>(count);
        }
        std::uint8_t extra{};
        for (;;) {
            const auto count = read(descriptor, &extra, 1);
            if (count < 0 && errno == EINTR) continue;
            if (count != 0) throw std::runtime_error("input changed during read");
            break;
        }
        if (close(descriptor) != 0) throw std::runtime_error("cannot close input");
        return bytes;
    } catch (...) {
        close(descriptor);
        throw;
    }
}

void write_exclusive(const std::filesystem::path &path, const std::string &content)
{
    const int descriptor = open(path.c_str(), O_WRONLY | O_CREAT | O_EXCL | O_NOFOLLOW, 0600);
    if (descriptor < 0) throw std::runtime_error("receipt output must be fresh");
    try {
        std::size_t written{};
        while (written < content.size()) {
            const auto count = write(descriptor, content.data() + written, content.size() - written);
            if (count < 0 && errno == EINTR) continue;
            if (count <= 0) throw std::runtime_error("cannot write receipt");
            written += static_cast<std::size_t>(count);
        }
        if (fsync(descriptor) != 0) throw std::runtime_error("cannot sync receipt");
        if (close(descriptor) != 0) throw std::runtime_error("cannot close receipt");
    } catch (...) {
        close(descriptor);
        // Retain partial failed output for diagnostic inspection.
        throw;
    }
}

Arguments arguments(int argc, char **argv)
{
    if (argc != 23) throw std::invalid_argument("invalid argument count");
    Arguments result;
    for (int index = 1; index < argc; index += 2) {
        const std::string flag(argv[index]), value(argv[index + 1]);
        if (flag == "--epoch0-tree-file") result.tree_file = value;
        else if (flag == "--stage-b-authorization-wire") result.authorization_file = value;
        else if (flag == "--issuer-id") result.issuer_id = value;
        else if (flag == "--issuer-reference") result.issuer_reference = value;
        else if (flag == "--issuer-public-key-hex") result.issuer_public_key_hex = value;
        else if (flag == "--issuer-public-key-fingerprint") result.issuer_public_key_fingerprint = value;
        else if (flag == "--label-issuer-reference") result.label_issuer_reference = value;
        else if (flag == "--approved-capacity-digest") result.approved_capacity_digest = value;
        else if (flag == "--arm") result.arm = value;
        else if (flag == "--source-revision") result.source_revision = value;
        else if (flag == "--output") result.output = value;
        else throw std::invalid_argument("unknown argument: " + flag);
    }
    if (result.tree_file.empty() || result.authorization_file.empty() || result.output.empty() ||
        result.issuer_reference.empty() || result.issuer_reference.size() > 128 ||
        result.label_issuer_reference.empty() || result.label_issuer_reference.size() > 128 ||
        !lower_hex(result.issuer_public_key_hex, 66) ||
        !lower_hex(result.issuer_public_key_fingerprint, 64) ||
        !lower_hex(result.approved_capacity_digest, 64) ||
        !lower_hex(result.source_revision, 40) ||
        (result.arm != "fast_priority_treatment" && result.arm != "exact_copy_sham"))
        throw std::invalid_argument("missing or malformed verifier argument");
    positive_id(result.issuer_id);
    return result;
}

std::vector<hotstuff::ReplicaID> membership()
{
    std::vector<hotstuff::ReplicaID> result;
    result.reserve(kN);
    for (hotstuff::ReplicaID id = 0; id < kN; ++id) result.push_back(id);
    return result;
}

void validate_n31_trees(const std::vector<hotstuff::EpochTreeDefinition> &trees)
{
    if (trees.size() != kTrees) throw std::runtime_error("Epoch-0 tree count differs");
    for (std::uint32_t index = 0; index < kTrees; ++index) {
        const auto &tree = trees.at(index);
        if (tree.tree_id != index || tree.fanout != kFanout ||
            tree.pipeline_stretch != kPipe || tree.members_breadth_first.size() != kN)
            throw std::runtime_error("Epoch-0 tree shape differs");
    }
}

std::string receipt(const Arguments &args, const hotstuff::OperatorCapacityAuthorization &authorization,
                    const hotstuff::uint256_t &epoch0_digest,
                    const hotstuff::uint256_t &topology_digest,
                    const std::string &wire_sha256, const std::string &tree_sha256)
{
    const auto &policy = authorization.policy;
    return std::string{"{"} +
        "\"schema_version\":1,\"kind\":\"kauri-operator-capacity-native-stage-b-verification-receipt-v1\"" +
        ",\"verdict\":\"NATIVE_STAGE_B_VERIFIED_NO_EXECUTION\"" +
        ",\"source_revision\":" + json_string(args.source_revision) +
        ",\"authorization_wire_sha256\":" + json_string(wire_sha256) +
        ",\"authorization_canonical_digest\":" +
            json_string(hotstuff::operator_capacity_authorization_digest(authorization).to_hex()) +
        ",\"epoch0_tree_file_sha256\":" + json_string(tree_sha256) +
        ",\"epoch0_consensus_digest\":" + json_string(epoch0_digest.to_hex()) +
        ",\"epoch0_topology_digest\":" + json_string(topology_digest.to_hex()) +
        ",\"issuer_id\":" + std::to_string(positive_id(args.issuer_id)) +
        ",\"issuer_reference\":" + json_string(args.issuer_reference) +
        ",\"issuer_public_key_fingerprint\":" +
            json_string(args.issuer_public_key_fingerprint) +
        ",\"label_issuer_reference\":" + json_string(args.label_issuer_reference) +
        ",\"approved_capacity_digest\":" + json_string(args.approved_capacity_digest) +
        ",\"arm\":" + json_string(args.arm) +
        ",\"baseline_snapshot_id\":" +
            json_string(policy.expected_responsiveness_snapshot_id) +
        ",\"baseline_evidence_cutoff\":" +
            std::to_string(policy.expected_evidence_cutoff) +
        ",\"decision_monotonic_raw_ns\":" +
            std::to_string(policy.decision_monotonic_ns) + "}\n";
}
} // namespace

int main(int argc, char **argv)
{
    try {
        const auto args = arguments(argc, argv);
        const auto tree_bytes = bounded_regular_bytes(args.tree_file, kMaximumTreeFileBytes);
        const auto members = membership();
        const auto trees = hotstuff::parse_adaptive_v2_epoch_zero_tree_bytes(tree_bytes, members);
        validate_n31_trees(trees);
        const auto input = hotstuff::adaptive_v2_epoch_zero_input(members, trees);
        const hotstuff::AdaptationEpochId epoch0{0, hotstuff::compute_epoch_digest(input)};
        const auto topology = hotstuff::operator_capacity_baseline_topology_digest(epoch0, members, trees);
        const auto bytes = bounded_regular_bytes(args.authorization_file, kMaximumAuthorizationBytes);
        const hotstuff::OperatorCapacityAuthorizationWireLimits limits{kMaximumAuthorizationBytes};
        const auto decoded = hotstuff::decode_operator_capacity_authorization(bytes, limits);
        if (!decoded) throw std::runtime_error("Stage-B wire is malformed");
        const auto &authorization = *decoded.value;
        const auto &policy = authorization.policy;
        const auto expected_arm = args.arm == "fast_priority_treatment"
            ? hotstuff::OperatorCapacityArm::fast_priority_treatment
            : hotstuff::OperatorCapacityArm::exact_copy_sham;
        if (policy.policy_version != "operator-capacity-v1" ||
            policy.arm != expected_arm || policy.capacity_snapshot.predecessor != epoch0 ||
            policy.baseline_trees.size() != kTrees ||
            hotstuff::operator_capacity_baseline_topology_digest(
                epoch0, members, policy.baseline_trees) != topology ||
            policy.expected_responsiveness_snapshot_id.empty() ||
            policy.expected_evidence_cutoff == 0 || policy.decision_monotonic_ns == 0)
            throw std::runtime_error("Stage-B predecessor, arm, or baseline differs");
        const hotstuff::PubKeySecp256k1 key(hotstuff::from_hex(args.issuer_public_key_hex));
        hotstuff::DataStream key_stream;
        key.serialize(key_stream);
        const auto key_bytes = static_cast<hotstuff::bytearray_t>(std::move(key_stream));
        if (sha256_hex(key_bytes) != args.issuer_public_key_fingerprint)
            throw std::runtime_error("Stage-B pinned key fingerprint differs");
        const hotstuff::OperatorCapacityIssuer issuer{
            positive_id(args.issuer_id), args.issuer_reference,
            hotstuff::uint256_t(hotstuff::from_hex(args.approved_capacity_digest)),
            key, args.label_issuer_reference};
        if (!hotstuff::verify_operator_capacity_authorization(authorization, issuer, members))
            throw std::runtime_error("Stage-B signature or pinned input differs");
        write_exclusive(args.output, receipt(args, authorization, epoch0.epoch_digest,
            topology, sha256_hex(bytes), sha256_hex(tree_bytes)));
        return 0;
    } catch (const std::exception &error) {
        std::cerr << "operator-capacity-authorization-verify: " << error.what() << '\n';
        return 2;
    }
}
