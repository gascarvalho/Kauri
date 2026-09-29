#include "catch.hpp"

#include <array>
#include <cerrno>
#include <cstdint>
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
#include "hotstuff/operator_capacity_policy.h"

namespace {
constexpr std::uint32_t kN = 31, kTrees = 21;
struct TemporaryDirectory {
    std::filesystem::path path;
    TemporaryDirectory() {
        std::array<char, 64> name{};
        std::snprintf(name.data(), name.size(), "/tmp/kauri-w18-sign-XXXXXX");
        const auto *created = mkdtemp(name.data()); REQUIRE(created != nullptr); path = created;
    }
    ~TemporaryDirectory() { std::error_code ignored; std::filesystem::remove_all(path, ignored); }
};

std::vector<hotstuff::ReplicaID> members() {
    std::vector<hotstuff::ReplicaID> result; result.reserve(kN);
    for (hotstuff::ReplicaID id = 0; id < kN; ++id) result.push_back(id);
    return result;
}
std::vector<hotstuff::EpochTreeDefinition> trees() {
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
                const std::vector<hotstuff::EpochTreeDefinition> &value) {
    std::ofstream output(path, std::ios::binary); REQUIRE(output.is_open());
    for (const auto &tree : value) {
        output << "fan:" << tree.fanout << " pipe:" << tree.pipeline_stretch;
        for (const auto id : tree.members_breadth_first) output << ' ' << id;
        output << '\n';
    }
    REQUIRE(output.good());
}
void write_bytes(const std::filesystem::path &path, const hotstuff::bytearray_t &bytes) {
    std::ofstream output(path, std::ios::binary); REQUIRE(output.is_open());
    output.write(reinterpret_cast<const char *>(bytes.data()), bytes.size()); REQUIRE(output.good());
}
std::uint64_t raw_now() {
    timespec value{}; REQUIRE(clock_gettime(CLOCK_MONOTONIC_RAW, &value) == 0);
    return static_cast<std::uint64_t>(value.tv_sec) * 1'000'000'000ULL + value.tv_nsec;
}
hotstuff::PrivKeySecp256k1 test_key() {
    hotstuff::PrivKeySecp256k1 key;
    key.from_hex("4aede145d13021fb43c938bced67511a7740c05786d3e0b94ffbdaa7f15afc57");
    return key;
}
std::string sha256_hex(const hotstuff::bytearray_t &bytes) {
    std::array<unsigned char, crypto_hash_sha256_BYTES> digest{};
    crypto_hash_sha256(digest.data(), bytes.data(), bytes.size());
    static constexpr char hex[] = "0123456789abcdef";
    std::string result;
    for (const auto byte : digest) { result += hex[byte >> 4U]; result += hex[byte & 0x0fU]; }
    return result;
}
int invoke(const std::vector<std::string> &args) {
    const auto child = fork(); REQUIRE(child >= 0);
    if (child == 0) {
        alarm(5); std::vector<char *> raw; raw.reserve(args.size() + 1);
        for (const auto &arg : args) raw.push_back(const_cast<char *>(arg.c_str()));
        raw.push_back(nullptr); execv(raw.front(), raw.data()); _exit(127);
    }
    int status{}; REQUIRE(waitpid(child, &status, 0) == child);
    return WIFEXITED(status) ? WEXITSTATUS(status) : 255;
}
std::vector<std::string> command(const std::filesystem::path &tree,
                                 const std::filesystem::path &snapshot,
                                 const std::filesystem::path &key,
                                 const std::filesystem::path &wire,
                                 const std::filesystem::path &receipt,
                                 std::uint64_t from, std::uint64_t until) {
    return {KAURI_OPERATOR_CAPACITY_LABEL_ENVELOPE_SIGN,
        "--epoch0-tree-file", tree.string(), "--capacity-snapshot-wire", snapshot.string(),
        "--issuer-private-key-file", key.string(), "--issuer-id", "73",
        "--issuer-reference", "label-test", "--arm", "fast_priority_treatment",
        "--valid-from-monotonic-raw-ns", std::to_string(from),
        "--valid-until-monotonic-raw-ns", std::to_string(until),
        "--source-revision", std::string(40, 'a'), "--wire-output", wire.string(),
        "--receipt-output", receipt.string()};
}
} // namespace

TEST_CASE("operator-capacity Stage-A producer signs only bounded N31 capacity input",
          "[operator-capacity][label-envelope][sign][cli]") {
    TemporaryDirectory directory;
    const auto tree_path = directory.path / "epoch0.tree";
    const auto snapshot_path = directory.path / "capacity.wire";
    const auto key_path = directory.path / "issuer.key";
    const auto wire_path = directory.path / "stage-a.wire";
    const auto receipt_path = directory.path / "receipt.json";
    const auto baseline = trees(); write_tree(tree_path, baseline);
    const auto membership = members();
    const auto input = hotstuff::adaptive_v2_epoch_zero_input(membership, baseline);
    const hotstuff::AdaptationEpochId epoch0{0, hotstuff::compute_epoch_digest(input)};
    const auto now = raw_now();
    hotstuff::OperatorCapacitySnapshot snapshot;
    snapshot.issuer_reference = "label-test"; snapshot.predecessor = epoch0;
    snapshot.valid_from_monotonic_ns = now - 1'000'000'000ULL;
    snapshot.valid_until_monotonic_ns = now + 5'000'000'000ULL;
    for (const auto id : membership) snapshot.labels.push_back({id,
        id < 6 ? hotstuff::OperatorCapacityClass::slow : hotstuff::OperatorCapacityClass::fast});
    snapshot.canonical_digest = hotstuff::operator_capacity_snapshot_digest(snapshot);
    constexpr hotstuff::OperatorCapacitySnapshotWireLimits snapshot_limits{32 * 1024};
    write_bytes(snapshot_path, hotstuff::encode_operator_capacity_snapshot(snapshot, snapshot_limits));
    { std::ofstream key(key_path); REQUIRE(key.is_open()); key << "4aede145d13021fb43c938bced67511a7740c05786d3e0b94ffbdaa7f15afc57"; }
    REQUIRE(chmod(key_path.c_str(), 0600) == 0);

    REQUIRE(invoke(command(tree_path, snapshot_path, key_path, wire_path, receipt_path,
                           snapshot.valid_from_monotonic_ns, snapshot.valid_until_monotonic_ns)) == 0);
    REQUIRE(std::filesystem::exists(wire_path));
    std::ifstream receipt(receipt_path); const std::string receipt_text{
        std::istreambuf_iterator<char>(receipt), std::istreambuf_iterator<char>()};
    CHECK(receipt_text.find("NATIVE_ENVELOPE_PRODUCED_NO_EXECUTION") != std::string::npos);
    CHECK(receipt_text.find("4aede145") == std::string::npos);

    SECTION("produced wire passes the independent native Stage-A verifier") {
        const hotstuff::PubKeySecp256k1 public_key(test_key());
        hotstuff::DataStream stream;
        public_key.serialize(stream);
        const auto public_bytes = static_cast<hotstuff::bytearray_t>(std::move(stream));
        static constexpr char hex[] = "0123456789abcdef";
        std::string public_hex;
        for (const auto byte : public_bytes) {
            public_hex += hex[byte >> 4U]; public_hex += hex[byte & 0x0fU];
        }
        const auto verified = directory.path / "verified.json";
        CHECK(invoke({KAURI_OPERATOR_CAPACITY_LABEL_ENVELOPE_VERIFY,
            "--epoch0-tree-file", tree_path.string(),
            "--stage-a-envelope-wire", wire_path.string(),
            "--issuer-id", "73", "--issuer-reference", "label-test",
            "--issuer-public-key-hex", public_hex,
            "--issuer-public-key-fingerprint", sha256_hex(public_bytes),
            "--approved-capacity-digest", snapshot.canonical_digest.to_hex(),
            "--arm", "fast_priority_treatment",
            "--source-revision", std::string(40, 'a'),
            "--output", verified.string()}) == 0);
        CHECK(std::filesystem::is_regular_file(verified));
    }

    SECTION("rejects unsafe private-key permissions") {
        const auto bad_wire = directory.path / "bad.wire";
        const auto bad_receipt = directory.path / "bad.json";
        REQUIRE(chmod(key_path.c_str(), 0644) == 0);
        CHECK(invoke(command(tree_path, snapshot_path, key_path, bad_wire, bad_receipt,
                             snapshot.valid_from_monotonic_ns, snapshot.valid_until_monotonic_ns)) == 2);
        CHECK_FALSE(std::filesystem::exists(bad_wire));
    }
    SECTION("rejects a zero-length Stage-A RAW validity window") {
        const auto bad_wire = directory.path / "zero-window.wire";
        const auto bad_receipt = directory.path / "zero-window.json";
        CHECK(invoke(command(tree_path, snapshot_path, key_path, bad_wire,
                             bad_receipt, snapshot.valid_from_monotonic_ns,
                             snapshot.valid_from_monotonic_ns)) == 2);
        CHECK_FALSE(std::filesystem::exists(bad_wire));
    }
    SECTION("rejects a FIFO key path without blocking") {
        const auto fifo = directory.path / "issuer.fifo";
        REQUIRE(mkfifo(fifo.c_str(), 0600) == 0);
        const auto bad_wire = directory.path / "fifo.wire";
        const auto bad_receipt = directory.path / "fifo.json";
        CHECK(invoke(command(tree_path, snapshot_path, fifo, bad_wire, bad_receipt,
                             snapshot.valid_from_monotonic_ns, snapshot.valid_until_monotonic_ns)) == 2);
        CHECK_FALSE(std::filesystem::exists(bad_wire));
    }
    SECTION("rejects a symlink key path") {
        const auto link = directory.path / "issuer-link";
        REQUIRE(symlink(key_path.c_str(), link.c_str()) == 0);
        const auto bad_wire = directory.path / "link.wire";
        const auto bad_receipt = directory.path / "link.json";
        CHECK(invoke(command(tree_path, snapshot_path, link, bad_wire, bad_receipt,
                             snapshot.valid_from_monotonic_ns, snapshot.valid_until_monotonic_ns)) == 2);
        CHECK_FALSE(std::filesystem::exists(bad_wire));
    }
}
