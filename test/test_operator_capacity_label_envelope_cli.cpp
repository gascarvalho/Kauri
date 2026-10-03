#include "catch.hpp"

#include <array>
#include <cerrno>
#include <cstdint>
#include <cstdlib>
#include <filesystem>
#include <fstream>
#include <string>
#include <sys/stat.h>
#include <sys/wait.h>
#include <time.h>
#include <unistd.h>
#include <vector>

#include <sodium.h>

#include "hotstuff/configuration.h"
#include "hotstuff/operator_capacity_label_envelope.h"

namespace {
constexpr std::uint32_t kN = 31, kTrees = 21;

struct TemporaryDirectory {
    std::filesystem::path path;
    TemporaryDirectory() {
        std::array<char, 64> template_path{};
        std::snprintf(template_path.data(), template_path.size(), "/tmp/kauri-w18-cli-XXXXXX");
        const auto *created = mkdtemp(template_path.data());
        REQUIRE(created != nullptr);
        path = created;
    }
    ~TemporaryDirectory() { std::error_code ignored; std::filesystem::remove_all(path, ignored); }
};

std::vector<hotstuff::ReplicaID> members()
{
    std::vector<hotstuff::ReplicaID> result;
    result.reserve(kN);
    for (hotstuff::ReplicaID id = 0; id < kN; ++id) result.push_back(id);
    return result;
}

std::vector<hotstuff::EpochTreeDefinition> trees()
{
    std::vector<hotstuff::EpochTreeDefinition> result;
    for (std::uint32_t tree = 0; tree < kTrees; ++tree) {
        std::vector<hotstuff::ReplicaID> order;
        for (hotstuff::ReplicaID position = 0; position < kN; ++position)
            order.push_back((tree + position) % kN);
        result.push_back({tree, 5, 2, std::move(order), {}});
    }
    return result;
}

void write_tree(const std::filesystem::path &path,
                const std::vector<hotstuff::EpochTreeDefinition> &value)
{
    std::ofstream output(path, std::ios::binary);
    REQUIRE(output.is_open());
    for (const auto &tree : value) {
        output << "fan:" << tree.fanout << " pipe:" << tree.pipeline_stretch;
        for (const auto id : tree.members_breadth_first) output << ' ' << id;
        output << '\n';
    }
    REQUIRE(output.good());
}

hotstuff::PrivKeySecp256k1 test_key()
{
    hotstuff::PrivKeySecp256k1 key;
    key.from_hex("4aede145d13021fb43c938bced67511a7740c05786d3e0b94ffbdaa7f15afc57");
    return key;
}

std::string sha256_hex(const hotstuff::bytearray_t &bytes)
{
    std::array<unsigned char, crypto_hash_sha256_BYTES> digest{};
    crypto_hash_sha256(digest.data(), bytes.data(), bytes.size());
    static constexpr char hex[] = "0123456789abcdef";
    std::string result;
    for (const auto byte : digest) { result += hex[byte >> 4U]; result += hex[byte & 0x0fU]; }
    return result;
}

std::string public_key_hex(const hotstuff::PubKeySecp256k1 &key)
{
    hotstuff::DataStream stream; key.serialize(stream);
    const auto bytes = static_cast<hotstuff::bytearray_t>(std::move(stream));
    static constexpr char hex[] = "0123456789abcdef";
    std::string result;
    for (const auto byte : bytes) { result += hex[byte >> 4U]; result += hex[byte & 0x0fU]; }
    return result;
}

void write_bytes(const std::filesystem::path &path, const hotstuff::bytearray_t &bytes)
{
    std::ofstream output(path, std::ios::binary);
    REQUIRE(output.is_open());
    output.write(reinterpret_cast<const char *>(bytes.data()), bytes.size());
    REQUIRE(output.good());
}

int invoke(const std::vector<std::string> &arguments)
{
    const auto child = fork();
    REQUIRE(child >= 0);
    if (child == 0) {
        alarm(5);
        std::vector<char *> raw;
        raw.reserve(arguments.size() + 1);
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
                                 const std::filesystem::path &envelope,
                                 const std::filesystem::path &output,
                                 const std::string &public_hex,
                                 const std::string &fingerprint,
                                 const std::string &capacity_digest,
                                 const std::string &arm = "fast_priority_treatment")
{
    return {KAURI_OPERATOR_CAPACITY_LABEL_ENVELOPE_VERIFY,
        "--epoch0-tree-file", tree.string(), "--stage-a-envelope-wire", envelope.string(),
        "--issuer-id", "73", "--issuer-reference", "test-issuer",
        "--issuer-public-key-hex", public_hex,
        "--issuer-public-key-fingerprint", fingerprint,
        "--approved-capacity-digest", capacity_digest, "--arm", arm,
        "--source-revision", std::string(40, 'a'), "--output", output.string()};
}

std::string receipt_text(const std::filesystem::path &path)
{
    std::ifstream input(path, std::ios::binary);
    return {std::istreambuf_iterator<char>(input), std::istreambuf_iterator<char>()};
}
} // namespace

TEST_CASE("operator-capacity CLI emits a receipt only for exact signed Stage-A input",
          "[operator-capacity][label-envelope][cli]")
{
    TemporaryDirectory directory;
    const auto tree_path = directory.path / "epoch0.tree";
    const auto envelope_path = directory.path / "stage-a.wire";
    const auto receipt_path = directory.path / "receipt.json";
    const auto baseline = trees();
    write_tree(tree_path, baseline);
    const auto membership = members();
    const auto input = hotstuff::adaptive_v2_epoch_zero_input(membership, baseline);
    const hotstuff::AdaptationEpochId epoch0{0, hotstuff::compute_epoch_digest(input)};
    const auto topology = hotstuff::operator_capacity_baseline_topology_digest(epoch0, membership, baseline);
    hotstuff::OperatorCapacitySnapshot snapshot;
    snapshot.issuer_reference = "test-issuer";
    snapshot.predecessor = epoch0;
    timespec now{}; REQUIRE(clock_gettime(CLOCK_MONOTONIC_RAW, &now) == 0);
    const auto now_ns = static_cast<std::uint64_t>(now.tv_sec) * 1'000'000'000ULL + now.tv_nsec;
    snapshot.valid_from_monotonic_ns = now_ns - 1'000'000'000ULL;
    snapshot.valid_until_monotonic_ns = now_ns + 1'000'000'000ULL;
    for (const auto id : membership) snapshot.labels.push_back({id,
        id < 6 ? hotstuff::OperatorCapacityClass::slow : hotstuff::OperatorCapacityClass::fast});
    snapshot.canonical_digest = hotstuff::operator_capacity_snapshot_digest(snapshot);
    const auto private_key = test_key();
    const hotstuff::PubKeySecp256k1 public_key(private_key);
    const auto envelope = hotstuff::sign_operator_capacity_label_envelope(
        membership, epoch0, topology, hotstuff::OperatorCapacityArm::fast_priority_treatment,
        snapshot, 73, private_key);
    constexpr hotstuff::OperatorCapacityLabelEnvelopeWireLimits limits{32 * 1024};
    const auto wire = hotstuff::encode_operator_capacity_label_envelope(envelope, limits);
    write_bytes(envelope_path, wire);
    hotstuff::DataStream public_stream; public_key.serialize(public_stream);
    const auto public_bytes = static_cast<hotstuff::bytearray_t>(std::move(public_stream));
    const auto args = command(tree_path, envelope_path, receipt_path,
        public_key_hex(public_key), sha256_hex(public_bytes), snapshot.canonical_digest.to_hex());

    REQUIRE(invoke(args) == 0);
    REQUIRE(std::filesystem::is_regular_file(receipt_path));
    const auto receipt = receipt_text(receipt_path);
    CHECK(receipt.find("\"verdict\":\"NATIVE_ENVELOPE_VERIFIED_NO_EXECUTION\"") != std::string::npos);
    CHECK(receipt.find("\"envelope_wire_sha256\":\"" + sha256_hex(wire) + "\"") != std::string::npos);
    const auto tree_bytes = [&] { std::ifstream input(tree_path, std::ios::binary); return hotstuff::bytearray_t(
        std::istreambuf_iterator<char>(input), std::istreambuf_iterator<char>()); }();
    CHECK(receipt.find("\"epoch0_tree_file_sha256\":\"" + sha256_hex(tree_bytes) + "\"") != std::string::npos);
    CHECK(receipt.find("\"epoch0_consensus_digest\":\"" + epoch0.epoch_digest.to_hex() + "\"") != std::string::npos);
    CHECK(receipt.find("\"epoch0_topology_digest\":\"" + topology.to_hex() + "\"") != std::string::npos);
    const std::string sampled_time = "\"verification_monotonic_raw_ns\":";
    const auto sampled_time_offset = receipt.find(sampled_time);
    REQUIRE(sampled_time_offset != std::string::npos);
    CHECK(receipt.at(sampled_time_offset + sampled_time.size()) >= '1');

    SECTION("historical replay preserves validity at the sealed time without live admission") {
        auto expired_snapshot = snapshot;
        expired_snapshot.valid_from_monotonic_ns = now_ns - 4'000'000'000ULL;
        expired_snapshot.valid_until_monotonic_ns = now_ns - 3'000'000'000ULL;
        expired_snapshot.canonical_digest = hotstuff::operator_capacity_snapshot_digest(expired_snapshot);
        const auto expired = hotstuff::sign_operator_capacity_label_envelope(
            membership, epoch0, topology, hotstuff::OperatorCapacityArm::fast_priority_treatment,
            expired_snapshot, 73, private_key);
        const auto expired_path = directory.path / "expired.wire";
        write_bytes(expired_path, hotstuff::encode_operator_capacity_label_envelope(expired, limits));
        const auto live_path = directory.path / "expired-live.json";
        CHECK(invoke(command(tree_path, expired_path, live_path, public_key_hex(public_key),
            sha256_hex(public_bytes), expired_snapshot.canonical_digest.to_hex())) == 2);
        CHECK_FALSE(std::filesystem::exists(live_path));
        const auto replay_path = directory.path / "historical.json";
        auto historical = command(tree_path, expired_path, replay_path, public_key_hex(public_key),
            sha256_hex(public_bytes), expired_snapshot.canonical_digest.to_hex());
        const auto historical_tick = now_ns - 3'500'000'000ULL;
        historical.insert(historical.end(), {"--replay-at-monotonic-raw-ns", std::to_string(historical_tick)});
        REQUIRE(invoke(historical) == 0);
        const auto replay = receipt_text(replay_path);
        CHECK(replay.find("\"verdict\":\"HISTORICAL_ENVELOPE_VERIFIED_NO_EXECUTION\"") != std::string::npos);
        CHECK(replay.find("\"verification_monotonic_raw_ns\":" + std::to_string(historical_tick)) != std::string::npos);
        const auto rejected_path = directory.path / "invalid-historical.json";
        historical[historical.size() - 3] = rejected_path.string();
        historical.back() = std::to_string(now_ns);
        CHECK(invoke(historical) == 2);
        CHECK_FALSE(std::filesystem::exists(rejected_path));
        historical.back() = std::to_string(UINT64_MAX);
        CHECK(invoke(historical) == 2);
        CHECK_FALSE(std::filesystem::exists(rejected_path));
        historical.back() = "0";
        CHECK(invoke(historical) == 2);
        CHECK_FALSE(std::filesystem::exists(rejected_path));
    }

    auto corrupted = wire; corrupted.back() ^= 1U;
    const auto corrupted_path = directory.path / "corrupted.wire";
    write_bytes(corrupted_path, corrupted);
    const auto bad_signature_receipt = directory.path / "bad-signature.json";
    CHECK(invoke(command(tree_path, corrupted_path, bad_signature_receipt,
        public_key_hex(public_key), sha256_hex(public_bytes), snapshot.canonical_digest.to_hex())) == 2);
    CHECK_FALSE(std::filesystem::exists(bad_signature_receipt));

    const auto changed_tree = directory.path / "changed.tree";
    auto wrong_shape = baseline; wrong_shape.front().fanout = 4;
    write_tree(changed_tree, wrong_shape);
    const auto bad_tree_receipt = directory.path / "bad-tree.json";
    CHECK(invoke(command(changed_tree, envelope_path, bad_tree_receipt,
        public_key_hex(public_key), sha256_hex(public_bytes), snapshot.canonical_digest.to_hex())) == 2);
    CHECK_FALSE(std::filesystem::exists(bad_tree_receipt));
    const auto bad_arm_receipt = directory.path / "bad-arm.json";
    CHECK(invoke(command(tree_path, envelope_path, bad_arm_receipt,
        public_key_hex(public_key), sha256_hex(public_bytes), snapshot.canonical_digest.to_hex(),
        "exact_copy_sham")) == 2);
    CHECK_FALSE(std::filesystem::exists(bad_arm_receipt));

    const auto fifo_tree = directory.path / "fifo.tree";
    REQUIRE(::mkfifo(fifo_tree.c_str(), 0600) == 0);
    const auto fifo_tree_receipt = directory.path / "fifo-tree.json";
    CHECK(invoke(command(fifo_tree, envelope_path, fifo_tree_receipt,
        public_key_hex(public_key), sha256_hex(public_bytes), snapshot.canonical_digest.to_hex())) == 2);
    CHECK_FALSE(std::filesystem::exists(fifo_tree_receipt));

    const auto fifo_envelope = directory.path / "fifo.wire";
    REQUIRE(::mkfifo(fifo_envelope.c_str(), 0600) == 0);
    const auto fifo_envelope_receipt = directory.path / "fifo-envelope.json";
    CHECK(invoke(command(tree_path, fifo_envelope, fifo_envelope_receipt,
        public_key_hex(public_key), sha256_hex(public_bytes), snapshot.canonical_digest.to_hex())) == 2);
    CHECK_FALSE(std::filesystem::exists(fifo_envelope_receipt));
}
