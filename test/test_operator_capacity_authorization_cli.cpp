#include "catch.hpp"

#include <array>
#include <cstdint>
#include <cstdio>
#include <cstdlib>
#include <filesystem>
#include <fstream>
#include <iterator>
#include <string>
#include <sys/stat.h>
#include <sys/wait.h>
#include <unistd.h>
#include <vector>

#include <sodium.h>

#include "hotstuff/configuration.h"
#include "hotstuff/operator_capacity_authorization.h"

namespace {
struct TemporaryDirectory {
    std::filesystem::path path;
    TemporaryDirectory() {
        std::array<char, 64> pattern{};
        std::snprintf(pattern.data(), pattern.size(), "/tmp/kauri-w18-stage-b-XXXXXX");
        const auto *created = mkdtemp(pattern.data());
        REQUIRE(created != nullptr);
        path = created;
    }
    ~TemporaryDirectory() {
        std::error_code ignored;
        std::filesystem::remove_all(path, ignored);
    }
};

std::vector<hotstuff::ReplicaID> members()
{
    std::vector<hotstuff::ReplicaID> result;
    for (hotstuff::ReplicaID id = 0; id < 31; ++id) result.push_back(id);
    return result;
}

std::vector<hotstuff::EpochTreeDefinition> trees()
{
    std::vector<hotstuff::EpochTreeDefinition> result;
    for (std::uint32_t index = 0; index < 21; ++index) {
        std::vector<hotstuff::ReplicaID> order;
        for (hotstuff::ReplicaID position = 0; position < 31; ++position)
            order.push_back((index + position) % 31);
        result.push_back({index, 5, 2, std::move(order), {}});
    }
    return result;
}

hotstuff::PrivKeySecp256k1 key()
{
    hotstuff::PrivKeySecp256k1 result;
    result.from_hex("4aede145d13021fb43c938bced67511a7740c05786d3e0b94ffbdaa7f15afc57");
    return result;
}

std::string hex(const hotstuff::bytearray_t &bytes)
{
    static constexpr char digits[] = "0123456789abcdef";
    std::string result;
    for (const auto byte : bytes) { result += digits[byte >> 4U]; result += digits[byte & 0x0fU]; }
    return result;
}

std::string sha256(const hotstuff::bytearray_t &bytes)
{
    std::array<unsigned char, crypto_hash_sha256_BYTES> digest{};
    crypto_hash_sha256(digest.data(), bytes.data(), bytes.size());
    return hex({digest.begin(), digest.end()});
}

void write_tree(const std::filesystem::path &path,
                const std::vector<hotstuff::EpochTreeDefinition> &trees)
{
    std::ofstream output(path, std::ios::binary);
    REQUIRE(output.is_open());
    for (const auto &tree : trees) {
        output << "fan:5 pipe:2";
        for (const auto id : tree.members_breadth_first) output << ' ' << id;
        output << '\n';
    }
    REQUIRE(output.good());
}

void write_wire(const std::filesystem::path &path, const hotstuff::bytearray_t &wire)
{
    std::ofstream output(path, std::ios::binary);
    REQUIRE(output.is_open());
    output.write(reinterpret_cast<const char *>(wire.data()), wire.size());
    REQUIRE(output.good());
}

int invoke(const std::vector<std::string> &arguments)
{
    const auto child = fork();
    REQUIRE(child >= 0);
    if (child == 0) {
        alarm(5);
        std::vector<char *> raw;
        for (const auto &argument : arguments) raw.push_back(const_cast<char *>(argument.c_str()));
        raw.push_back(nullptr);
        execv(raw.front(), raw.data());
        _exit(127);
    }
    int status{};
    REQUIRE(waitpid(child, &status, 0) == child);
    return WIFEXITED(status) ? WEXITSTATUS(status) : 255;
}

std::vector<std::string> command(const std::filesystem::path &tree,
                                 const std::filesystem::path &wire,
                                 const std::filesystem::path &output,
                                 const std::string &public_key,
                                 const std::string &fingerprint,
                                 const std::string &digest,
                                 const std::string &label_reference = "label-issuer")
{
    return {KAURI_OPERATOR_CAPACITY_AUTHORIZATION_VERIFY,
        "--epoch0-tree-file", tree.string(),
        "--stage-b-authorization-wire", wire.string(),
        "--issuer-id", "91", "--issuer-reference", "epoch-issuer",
        "--issuer-public-key-hex", public_key,
        "--issuer-public-key-fingerprint", fingerprint,
        "--label-issuer-reference", label_reference,
        "--approved-capacity-digest", digest,
        "--arm", "exact_copy_sham",
        "--source-revision", std::string(40, 'a'),
        "--output", output.string()};
}
} // namespace

TEST_CASE("native Stage-B CLI verifies distinct signer and label issuer",
          "[operator-capacity][authorization][cli]")
{
    TemporaryDirectory directory;
    const auto tree = directory.path / "epoch0.tree";
    const auto wire_file = directory.path / "stage-b.wire";
    const auto output = directory.path / "verified.json";
    const auto baseline = trees();
    write_tree(tree, baseline);
    const auto identities = members();
    const auto e0_input = hotstuff::adaptive_v2_epoch_zero_input(identities, baseline);
    const hotstuff::AdaptationEpochId epoch0{0, hotstuff::compute_epoch_digest(e0_input)};
    hotstuff::OperatorCapacitySnapshot labels;
    labels.issuer_reference = "label-issuer";
    labels.predecessor = epoch0;
    labels.valid_from_monotonic_ns = 100;
    labels.valid_until_monotonic_ns = 200;
    for (const auto id : identities)
        labels.labels.push_back({id, id < 6
            ? hotstuff::OperatorCapacityClass::slow : hotstuff::OperatorCapacityClass::fast});
    labels.canonical_digest = hotstuff::operator_capacity_snapshot_digest(labels);
    hotstuff::OperatorCapacityPolicyConfig policy;
    policy.arm = hotstuff::OperatorCapacityArm::exact_copy_sham;
    policy.policy_version = "operator-capacity-v1";
    policy.decision_monotonic_ns = 150;
    policy.expected_responsiveness_snapshot_id = std::string(64, 'b');
    policy.expected_evidence_cutoff = 71;
    policy.approved_capacity_digest = labels.canonical_digest;
    policy.capacity_snapshot = labels;
    policy.fanout = 5;
    policy.tree_count = 21;
    policy.baseline_trees = baseline;
    const auto private_key = key();
    const hotstuff::PubKeySecp256k1 public_key(private_key);
    hotstuff::DataStream stream;
    public_key.serialize(stream);
    const auto public_bytes = static_cast<hotstuff::bytearray_t>(std::move(stream));
    const auto signed_input = hotstuff::authorize_operator_capacity(
        identities, policy, 91, private_key);
    const hotstuff::OperatorCapacityAuthorizationWireLimits limits{128 * 1024};
    const auto wire = hotstuff::encode_operator_capacity_authorization(signed_input, limits);
    write_wire(wire_file, wire);
    const auto args = command(tree, wire_file, output, hex(public_bytes),
        sha256(public_bytes), labels.canonical_digest.to_hex());
    REQUIRE(invoke(args) == 0);
    std::ifstream input(output, std::ios::binary);
    const std::string receipt(std::istreambuf_iterator<char>{input}, {});
    CHECK(receipt.find("NATIVE_STAGE_B_VERIFIED_NO_EXECUTION") != std::string::npos);
    CHECK(receipt.find(sha256(wire)) != std::string::npos);
    CHECK(receipt.find(epoch0.epoch_digest.to_hex()) != std::string::npos);
    CHECK(receipt.find("\"label_issuer_reference\":\"label-issuer\"") != std::string::npos);
    CHECK(invoke(args) == 2); // A receipt is exclusive, never overwritten.

    const auto wrong_label_output = directory.path / "wrong-label.json";
    CHECK(invoke(command(tree, wire_file, wrong_label_output, hex(public_bytes),
        sha256(public_bytes), labels.canonical_digest.to_hex(), "different")) == 2);
    CHECK_FALSE(std::filesystem::exists(wrong_label_output));

    auto corrupted = wire;
    corrupted.back() ^= 1U;
    const auto corrupted_file = directory.path / "corrupted.wire";
    write_wire(corrupted_file, corrupted);
    const auto bad_signature_output = directory.path / "bad-signature.json";
    CHECK(invoke(command(tree, corrupted_file, bad_signature_output, hex(public_bytes),
        sha256(public_bytes), labels.canonical_digest.to_hex())) == 2);
    CHECK_FALSE(std::filesystem::exists(bad_signature_output));

    const auto fifo = directory.path / "stage-b.fifo";
    REQUIRE(mkfifo(fifo.c_str(), 0600) == 0);
    const auto fifo_output = directory.path / "fifo.json";
    CHECK(invoke(command(tree, fifo, fifo_output, hex(public_bytes),
        sha256(public_bytes), labels.canonical_digest.to_hex())) == 2);
    CHECK_FALSE(std::filesystem::exists(fifo_output));

    auto wrong_policy = policy;
    wrong_policy.policy_version = "operator-capacity-v0";
    const auto wrong_policy_wire = hotstuff::encode_operator_capacity_authorization(
        hotstuff::authorize_operator_capacity(identities, wrong_policy, 91, private_key), limits);
    const auto wrong_policy_file = directory.path / "wrong-policy.wire";
    write_wire(wrong_policy_file, wrong_policy_wire);
    const auto wrong_policy_output = directory.path / "wrong-policy.json";
    CHECK(invoke(command(tree, wrong_policy_file, wrong_policy_output, hex(public_bytes),
        sha256(public_bytes), labels.canonical_digest.to_hex())) == 2);
    CHECK_FALSE(std::filesystem::exists(wrong_policy_output));
}
