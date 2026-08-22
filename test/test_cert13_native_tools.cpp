#include "catch.hpp"

#include <cstdio>
#include <fstream>
#include <memory>
#include <sstream>
#include <stdexcept>
#include <string>
#include <unordered_set>
#include <utility>
#include <vector>

#include "hotstuff/activation_readiness_wire.h"
#include "support/subprocess.h"

#ifndef KAURI_HOTSTUFF_KEYGEN_PATH
#error "KAURI_HOTSTUFF_KEYGEN_PATH must name hotstuff-keygen"
#endif
#ifndef KAURI_EPOCH_PROFILE_DIGEST_PATH
#error "KAURI_EPOCH_PROFILE_DIGEST_PATH must name epoch-profile-digest"
#endif

namespace
{

using kauri::test_support::ProcessResult;
using kauri::test_support::run_program;

bool lowercase_hex(const std::string &value, std::size_t size)
{
    if (value.size() != size)
        return false;
    for (const auto character : value)
        if (!((character >= '0' && character <= '9') ||
              (character >= 'a' && character <= 'f')))
            return false;
    return true;
}

std::vector<std::pair<std::string, std::string>> parse_key_pairs(
    const std::string &output,
    std::size_t public_key_size = 96)
{
    std::vector<std::pair<std::string, std::string>> pairs;
    std::size_t offset = 0;
    while (offset < output.size())
    {
        const auto newline = output.find('\n', offset);
        REQUIRE(newline != std::string::npos);
        const auto line = output.substr(offset, newline - offset);
        REQUIRE(line.rfind("pub:", 0) == 0);
        const auto separator = line.find(" sec:");
        REQUIRE(separator != std::string::npos);
        const auto public_key = line.substr(4, separator - 4);
        const auto private_key = line.substr(separator + 5);
        REQUIRE(lowercase_hex(public_key, public_key_size));
        REQUIRE(lowercase_hex(private_key, 64));
        pairs.emplace_back(public_key, private_key);
        offset = newline + 1;
    }
    return pairs;
}

struct TemporaryDirectory
{
    std::string path;

    TemporaryDirectory()
    {
        std::string pattern = "/tmp/kauri-cert13-tools-XXXXXX";
        std::vector<char> writable(pattern.begin(), pattern.end());
        writable.push_back('\0');
        const auto created = ::mkdtemp(writable.data());
        if (created == nullptr)
            throw std::runtime_error("failed to create temporary directory");
        path = created;
    }

    ~TemporaryDirectory()
    {
        for (const auto &name : {"manifest.json", "certificate.hex",
                                 "identity.hex", "manifest-target.json",
                                 "certificate-target.hex",
                                 "identity-target.hex"})
            ::unlink((path + "/" + name).c_str());
        ::rmdir(path.c_str());
    }

    std::string write(const std::string &name, const std::string &contents)
    {
        const auto destination = path + "/" + name;
        std::ofstream output(destination, std::ios::binary | std::ios::trunc);
        if (!output)
            throw std::runtime_error("failed to open temporary file");
        output.write(contents.data(), static_cast<std::streamsize>(contents.size()));
        output.close();
        if (!output)
            throw std::runtime_error("failed to write temporary file");
        return destination;
    }
};

hotstuff::uint256_t tool_test_digest(const std::string &text)
{
    return hotstuff::DataStream(text).get_hash();
}

hotstuff::bytearray_t tool_private_key_bytes(hotstuff::ReplicaID id)
{
    hotstuff::bytearray_t bytes(bls::PrivateKey::PRIVATE_KEY_SIZE, 0);
    bytes.back() = static_cast<std::uint8_t>(id + 1);
    return bytes;
}

struct ToolReadinessFixture
{
    static constexpr std::size_t member_count = 7;
    static constexpr std::size_t quorum = 5;
    hotstuff::ActivationReadinessWireLimits limits{16384, member_count};
    std::vector<std::unique_ptr<hotstuff::PrivKeyBLS>> private_keys;
    std::vector<std::pair<hotstuff::ReplicaID, hotstuff::PubKeyBLS>> members;
    hotstuff::ActivationReadyIdentityV1 identity;

    ToolReadinessFixture()
    {
        for (hotstuff::ReplicaID id = 0; id < member_count; ++id)
        {
            auto key = std::make_unique<hotstuff::PrivKeyBLS>(
                tool_private_key_bytes(id));
            members.emplace_back(id, hotstuff::PubKeyBLS(*key));
            private_keys.emplace_back(std::move(key));
        }
        identity.membership_digest =
            hotstuff::canonical_activation_readiness_membership_digest(members);
        identity.predecessor_boundary_configuration =
            hotstuff::ConfigurationId{7, 1, tool_test_digest("predecessor")};
        identity.predecessor_boundary_generation =
            *hotstuff::checked_activation_generation(7, 3);
        identity.successor_configuration =
            hotstuff::ConfigurationId{8, 0, tool_test_digest("successor")};
        identity.successor_activation_generation =
            *hotstuff::checked_activation_generation(8, 0);
        identity.command_payload_digest = tool_test_digest("command");
        identity.command_block_height = 100;
        identity.command_block_hash = tool_test_digest("command-block");
        identity.activation_delay_blocks = 5;
        identity.activation_height = 105;
        identity.activation_boundary_block_hash =
            tool_test_digest("boundary-block");
    }

    hotstuff::ActivationReadinessCertificateV1 certificate(
        std::size_t signer_count = quorum) const
    {
        std::vector<hotstuff::ActivationReadyObservationV1> observations;
        for (hotstuff::ReplicaID id = 0; id < signer_count; ++id)
            observations.emplace_back(
                hotstuff::sign_activation_ready_observation(
                    identity, id, id + 1, 1000 + id,
                    *private_keys.at(id)));
        return hotstuff::make_activation_readiness_certificate(
            identity, std::move(observations));
    }

    std::string manifest() const
    {
        std::ostringstream output;
        output << "{\"algorithm\":\"bls-pop\""
               << ",\"domain\":\"kauri-adaptive-v3-readiness-public-key-manifest-v1\""
               << ",\"members\":[";
        for (std::size_t index = 0; index < members.size(); ++index)
        {
            if (index != 0)
                output << ',';
            output << "{\"public_key_hex\":\""
                   << hotstuff::get_hex(members[index].second)
                   << "\",\"replica_id\":" << members[index].first << '}';
        }
        output << "]"
               << ",\"membership_digest\":\""
               << identity.membership_digest.to_hex() << '"'
               << ",\"profile_id\":\"focused-n7-v13-test\""
               << ",\"profile_sha256\":\""
               << std::string(64, '1')
               << "\",\"protocol_mode\":\"adaptive_v3\""
               << ",\"schema_version\":1}\n";
        return output.str();
    }
};

std::string hex_line(const hotstuff::bytearray_t &bytes)
{
    return hotstuff::get_hex(bytes) + "\n";
}

ProcessResult run_readiness_verifier(
    const std::string &manifest,
    const hotstuff::bytearray_t &certificate,
    const hotstuff::bytearray_t &identity)
{
    TemporaryDirectory directory;
    const auto manifest_path = directory.write("manifest.json", manifest);
    const auto certificate_path = directory.write(
        "certificate.hex", hex_line(certificate));
    const auto identity_path = directory.write(
        "identity.hex", hex_line(identity));
    return run_program(
        KAURI_EPOCH_PROFILE_DIGEST_PATH,
        {"--verify-adaptive-v3-readiness-v1", manifest_path,
         certificate_path, identity_path});
}

} // namespace

TEST_CASE("CERT13 secure BLS preallocation is buffered distinct and derivable")
{
    const auto first = run_program(
        KAURI_HOTSTUFF_KEYGEN_PATH,
        {"--num", "7", "--algo", "bls",
         "--secure-bls-preallocation"});
    REQUIRE(first.status == 0);
    REQUIRE(first.error.empty());
    const auto first_pairs = parse_key_pairs(first.output);
    REQUIRE(first_pairs.size() == 7);

    std::unordered_set<std::string> public_keys;
    std::unordered_set<std::string> private_keys;
    for (const auto &pair : first_pairs)
    {
        REQUIRE(public_keys.insert(pair.first).second);
        REQUIRE(private_keys.insert(pair.second).second);
        const auto derived = run_program(
            KAURI_HOTSTUFF_KEYGEN_PATH,
            {"--derive-bls-public"},
            pair.second + "\n");
        REQUIRE(derived.status == 0);
        REQUIRE(derived.error.empty());
        REQUIRE(derived.output == "pub:" + pair.first + "\n");
    }

    const auto second = run_program(
        KAURI_HOTSTUFF_KEYGEN_PATH,
        {"--num", "7", "--algo", "bls",
         "--secure-bls-preallocation"});
    REQUIRE(second.status == 0);
    REQUIRE(second.error.empty());
    REQUIRE(second.output != first.output);
}

TEST_CASE("CERT13 key tooling rejects malformed modes without partial stdout")
{
    const auto wrong_algorithm = run_program(
        KAURI_HOTSTUFF_KEYGEN_PATH,
        {"--num", "7", "--algo", "secp256k1",
         "--secure-bls-preallocation"});
    REQUIRE(wrong_algorithm.status != 0);
    REQUIRE(wrong_algorithm.output.empty());

    const auto zero = run_program(
        KAURI_HOTSTUFF_KEYGEN_PATH,
        {"--num", "0", "--algo", "bls",
         "--secure-bls-preallocation"});
    REQUIRE(zero.status != 0);
    REQUIRE(zero.output.empty());

    const auto noncanonical_count = run_program(
        KAURI_HOTSTUFF_KEYGEN_PATH,
        {"--num", "7junk", "--algo", "bls",
         "--secure-bls-preallocation"});
    REQUIRE(noncanonical_count.status != 0);
    REQUIRE(noncanonical_count.output.empty());

    const auto excessive_count = run_program(
        KAURI_HOTSTUFF_KEYGEN_PATH,
        {"--num", "1025", "--algo", "bls",
         "--secure-bls-preallocation"});
    REQUIRE(excessive_count.status != 0);
    REQUIRE(excessive_count.output.empty());

    const auto uppercase = run_program(
        KAURI_HOTSTUFF_KEYGEN_PATH,
        {"--derive-bls-public"},
        std::string(64, 'A') + "\n");
    REQUIRE(uppercase.status != 0);
    REQUIRE(uppercase.output.empty());

    const auto missing_newline = run_program(
        KAURI_HOTSTUFF_KEYGEN_PATH,
        {"--derive-bls-public"},
        std::string(64, '1'));
    REQUIRE(missing_newline.status != 0);
    REQUIRE(missing_newline.output.empty());

    const auto mixed_mode = run_program(
        KAURI_HOTSTUFF_KEYGEN_PATH,
        {"--derive-bls-public", "--num", "1"},
        std::string(64, '1') + "\n");
    REQUIRE(mixed_mode.status != 0);
    REQUIRE(mixed_mode.output.empty());

    const auto invalid_scalar = run_program(
        KAURI_HOTSTUFF_KEYGEN_PATH,
        {"--derive-bls-public"},
        std::string(64, '0') + "\n");
    REQUIRE(invalid_scalar.status != 0);
    REQUIRE(invalid_scalar.output.empty());

    const auto out_of_range_scalar = run_program(
        KAURI_HOTSTUFF_KEYGEN_PATH,
        {"--derive-bls-public"},
        "73eda753299d7d483339d80809a1d805"
        "53bda402fffe5bfeffffffff00000001\n");
    REQUIRE(out_of_range_scalar.status != 0);
    REQUIRE(out_of_range_scalar.output.empty());
}

TEST_CASE("legacy hotstuff-keygen BLS output remains byte identical")
{
    const auto result = run_program(
        KAURI_HOTSTUFF_KEYGEN_PATH,
        {"--num", "2", "--algo", "bls"});
    REQUIRE(result.status == 0);
    REQUIRE(result.error.empty());
    REQUIRE(result.output ==
            "pub:ab6393d39d8d1e7f395fd5ea91b30452236875af1738204ad25b0d996e8bd7c8261a21c277efc31a0c64d18ed87dfdd9 sec:600d7df0e15025e4f1fdd09528e74df2a6b7243411de96680da5e87f7c953918\n"
            "pub:80a7614d859735f28328f5d487f29132d16f1b28e5468d08a5a731be3ea179738c231144bfc428f7a26255d8755a0ddc sec:03849e4fd26cf7f486740fbe6833d230db2802d4221b6728dfd70ba0f4e7dfea\n");

    const auto default_result = run_program(
        KAURI_HOTSTUFF_KEYGEN_PATH, {});
    REQUIRE(default_result.status == 0);
    REQUIRE(default_result.error.empty());
    REQUIRE(default_result.output ==
            "pub:ab6393d39d8d1e7f395fd5ea91b30452236875af1738204ad25b0d996e8bd7c8261a21c277efc31a0c64d18ed87dfdd9 sec:600d7df0e15025e4f1fdd09528e74df2a6b7243411de96680da5e87f7c953918\n");

    const auto secp_result = run_program(
        KAURI_HOTSTUFF_KEYGEN_PATH,
        {"--num", "2", "--algo", "secp256k1"});
    REQUIRE(secp_result.status == 0);
    REQUIRE(secp_result.error.empty());
    const auto secp_pairs = parse_key_pairs(secp_result.output, 66);
    REQUIRE(secp_pairs.size() == 2);
    for (const auto &pair : secp_pairs)
    {
        REQUIRE(lowercase_hex(pair.first, 66));
        REQUIRE(lowercase_hex(pair.second, 64));
    }
}

TEST_CASE("CERT13 profile digest tool verifies a whole readiness certificate")
{
    ToolReadinessFixture fixture;
    const auto certificate = fixture.certificate();
    const auto certificate_bytes =
        hotstuff::encode_activation_readiness_certificate(
            certificate, fixture.limits);
    const auto identity_bytes = hotstuff::encode_activation_ready_identity_v1(
        fixture.identity, fixture.limits);

    const auto result = run_readiness_verifier(
        fixture.manifest(), certificate_bytes, identity_bytes);
    INFO("verifier stderr: " << result.error);
    INFO("verifier stdout: " << result.output);
    REQUIRE(result.status == 0);
    REQUIRE(result.error.empty());
    REQUIRE(result.output ==
            "{\"certificate_digest\":\"" +
                certificate.certificate_digest.to_hex() +
                "\",\"certificate_payload_digest\":\"" +
                hotstuff::DataStream(certificate_bytes).get_hash().to_hex() +
                "\",\"expected_identity_payload_digest\":\"" +
                hotstuff::DataStream(identity_bytes).get_hash().to_hex() +
                "\",\"manifest_payload_digest\":\"" +
                hotstuff::DataStream(fixture.manifest()).get_hash().to_hex() +
                "\",\"member_count\":7,\"membership_digest\":\"" +
                fixture.identity.membership_digest.to_hex() +
                "\",\"observation_count\":5"
                ",\"schema\":\"kauri-adaptive-v3-readiness-verification-v1\""
                ",\"valid\":true}\n");
}

TEST_CASE("CERT13 readiness verifier rejects crypto and identity mutations")
{
    ToolReadinessFixture fixture;
    const auto original = fixture.certificate();
    std::vector<hotstuff::ActivationReadyObservationV1>
        bad_signature_observations;
    bad_signature_observations.emplace_back(
        hotstuff::ActivationReadyObservationV1{
            fixture.identity, 0, 1, 1000, true,
            hotstuff::SigSecBLS(
                tool_test_digest("wrong-signature-domain"),
                *fixture.private_keys[0])});
    for (std::size_t index = 1;
         index < original.observations.size(); ++index)
        bad_signature_observations.emplace_back(
            original.observations[index]);
    hotstuff::ActivationReadinessCertificateV1 bad_signature;
    bad_signature.identity = fixture.identity;
    bad_signature.observations = std::move(bad_signature_observations);
    bad_signature.certificate_digest =
        hotstuff::activation_readiness_certificate_digest(bad_signature);
    const auto bad_signature_bytes =
        hotstuff::encode_activation_readiness_certificate(
            bad_signature, fixture.limits);
    const auto identity_bytes = hotstuff::encode_activation_ready_identity_v1(
        fixture.identity, fixture.limits);
    const auto invalid_signature = run_readiness_verifier(
        fixture.manifest(), bad_signature_bytes, identity_bytes);
    REQUIRE(invalid_signature.status == 1);
    REQUIRE(invalid_signature.error.empty());
    REQUIRE(invalid_signature.output.find("\"valid\":false}") !=
            std::string::npos);

    auto other_identity = fixture.identity;
    other_identity.activation_boundary_block_hash =
        tool_test_digest("other-boundary");
    const auto other_identity_bytes =
        hotstuff::encode_activation_ready_identity_v1(
            other_identity, fixture.limits);
    const auto identity_mismatch = run_readiness_verifier(
        fixture.manifest(),
        hotstuff::encode_activation_readiness_certificate(
            fixture.certificate(), fixture.limits),
        other_identity_bytes);
    REQUIRE(identity_mismatch.status == 1);
    REQUIRE(identity_mismatch.error.empty());
    REQUIRE(identity_mismatch.output.find("\"valid\":false}") !=
            std::string::npos);

    const auto below_quorum = run_readiness_verifier(
        fixture.manifest(),
        hotstuff::encode_activation_readiness_certificate(
            fixture.certificate(4), fixture.limits),
        identity_bytes);
    REQUIRE(below_quorum.status == 1);
    REQUIRE(below_quorum.error.empty());
    REQUIRE(below_quorum.output.find("\"observation_count\":4") !=
            std::string::npos);
}

TEST_CASE("CERT13 readiness verifier rejects malformed inputs without stdout")
{
    ToolReadinessFixture fixture;
    const auto certificate_bytes =
        hotstuff::encode_activation_readiness_certificate(
            fixture.certificate(), fixture.limits);
    const auto identity_bytes = hotstuff::encode_activation_ready_identity_v1(
        fixture.identity, fixture.limits);

    auto malformed_manifest = fixture.manifest();
    const auto member = malformed_manifest.find("\"replica_id\":1");
    REQUIRE(member != std::string::npos);
    malformed_manifest.replace(
        member, std::string("\"replica_id\":1").size(),
        "\"replica_id\":0");
    const auto duplicate = run_readiness_verifier(
        malformed_manifest, certificate_bytes, identity_bytes);
    REQUIRE(duplicate.status == 2);
    REQUIRE(duplicate.output.empty());

    auto duplicate_key_manifest = fixture.manifest();
    const auto first_key = hotstuff::get_hex(fixture.members[0].second);
    const auto second_key = hotstuff::get_hex(fixture.members[1].second);
    const auto second_key_offset = duplicate_key_manifest.find(second_key);
    REQUIRE(second_key_offset != std::string::npos);
    duplicate_key_manifest.replace(
        second_key_offset, second_key.size(), first_key);
    const auto duplicate_key = run_readiness_verifier(
        duplicate_key_manifest, certificate_bytes, identity_bytes);
    REQUIRE(duplicate_key.status == 2);
    REQUIRE(duplicate_key.output.empty());

    auto digest_mismatch_manifest = fixture.manifest();
    const auto digest_offset = digest_mismatch_manifest.find(
        fixture.identity.membership_digest.to_hex());
    REQUIRE(digest_offset != std::string::npos);
    digest_mismatch_manifest[digest_offset] =
        digest_mismatch_manifest[digest_offset] == '0' ? '1' : '0';
    const auto digest_mismatch = run_readiness_verifier(
        digest_mismatch_manifest, certificate_bytes, identity_bytes);
    REQUIRE(digest_mismatch.status == 2);
    REQUIRE(digest_mismatch.output.empty());

    auto noncanonical_manifest = fixture.manifest();
    noncanonical_manifest.insert(1, " ");
    const auto noncanonical = run_readiness_verifier(
        noncanonical_manifest, certificate_bytes, identity_bytes);
    REQUIRE(noncanonical.status == 2);
    REQUIRE(noncanonical.output.empty());

    auto truncated = certificate_bytes;
    truncated.pop_back();
    const auto truncated_result = run_readiness_verifier(
        fixture.manifest(), truncated, identity_bytes);
    REQUIRE(truncated_result.status == 2);
    REQUIRE(truncated_result.output.empty());

    auto trailing = certificate_bytes;
    trailing.push_back(0);
    const auto trailing_result = run_readiness_verifier(
        fixture.manifest(), trailing, identity_bytes);
    REQUIRE(trailing_result.status == 2);
    REQUIRE(trailing_result.output.empty());

    auto wrong_identity_domain = identity_bytes;
    REQUIRE_FALSE(wrong_identity_domain.empty());
    wrong_identity_domain.front() ^= 1;
    const auto wrong_identity = run_readiness_verifier(
        fixture.manifest(), certificate_bytes, wrong_identity_domain);
    REQUIRE(wrong_identity.status == 2);
    REQUIRE(wrong_identity.output.empty());

    TemporaryDirectory directory;
    const auto manifest_path = directory.write(
        "manifest.json", fixture.manifest());
    auto uppercase = hex_line(certificate_bytes);
    uppercase[0] = 'A';
    const auto certificate_path = directory.write(
        "certificate.hex", uppercase);
    const auto identity_path = directory.write(
        "identity.hex", hex_line(identity_bytes));
    const auto uppercase_result = run_program(
        KAURI_EPOCH_PROFILE_DIGEST_PATH,
        {"--verify-adaptive-v3-readiness-v1", manifest_path,
         certificate_path, identity_path});
    REQUIRE(uppercase_result.status == 2);
    REQUIRE(uppercase_result.output.empty());
}

TEST_CASE("CERT13 readiness verifier rejects symlinked and oversized inputs")
{
    ToolReadinessFixture fixture;
    const auto certificate_bytes =
        hotstuff::encode_activation_readiness_certificate(
            fixture.certificate(), fixture.limits);
    const auto identity_bytes = hotstuff::encode_activation_ready_identity_v1(
        fixture.identity, fixture.limits);

    SECTION("direct manifest symlink")
    {
        TemporaryDirectory directory;
        const auto target = directory.write(
            "manifest-target.json", fixture.manifest());
        const auto link = directory.path + "/manifest.json";
        REQUIRE(::symlink(target.c_str(), link.c_str()) == 0);
        const auto certificate_path = directory.write(
            "certificate.hex", hex_line(certificate_bytes));
        const auto identity_path = directory.write(
            "identity.hex", hex_line(identity_bytes));
        const auto result = run_program(
            KAURI_EPOCH_PROFILE_DIGEST_PATH,
            {"--verify-adaptive-v3-readiness-v1", link,
             certificate_path, identity_path});
        REQUIRE(result.status == 2);
        REQUIRE(result.output.empty());
    }

    SECTION("regular certificate replaced by symlink")
    {
        TemporaryDirectory directory;
        const auto certificate_path = directory.write(
            "certificate.hex", hex_line(certificate_bytes));
        const auto target = directory.path + "/certificate-target.hex";
        REQUIRE(::rename(certificate_path.c_str(), target.c_str()) == 0);
        REQUIRE(::symlink(target.c_str(), certificate_path.c_str()) == 0);
        const auto manifest_path = directory.write(
            "manifest.json", fixture.manifest());
        const auto identity_path = directory.write(
            "identity.hex", hex_line(identity_bytes));
        const auto result = run_program(
            KAURI_EPOCH_PROFILE_DIGEST_PATH,
            {"--verify-adaptive-v3-readiness-v1", manifest_path,
             certificate_path, identity_path});
        REQUIRE(result.status == 2);
        REQUIRE(result.output.empty());
    }

    SECTION("oversized manifest")
    {
        TemporaryDirectory directory;
        const auto manifest_path = directory.write(
            "manifest.json", std::string(64 * 1024 + 1, 'x'));
        const auto certificate_path = directory.write(
            "certificate.hex", hex_line(certificate_bytes));
        const auto identity_path = directory.write(
            "identity.hex", hex_line(identity_bytes));
        const auto result = run_program(
            KAURI_EPOCH_PROFILE_DIGEST_PATH,
            {"--verify-adaptive-v3-readiness-v1", manifest_path,
             certificate_path, identity_path});
        REQUIRE(result.status == 2);
        REQUIRE(result.output.empty());
    }
}

TEST_CASE("legacy epoch-profile-digest three-positional output remains byte identical")
{
    const auto result = run_program(
        KAURI_EPOCH_PROFILE_DIGEST_PATH, {"4", "2", "2"});
    REQUIRE(result.status == 0);
    REQUIRE(result.error.empty());
    REQUIRE(result.output ==
            "{\"schema\":\"kauri-adaptive-v2-epoch-profile-digest-v1\",\"replica_count\":4,\"fault_threshold\":1,\"quorum\":3,\"fanout\":2,\"pipeline_stretch\":2,\"membership\":[0,1,2,3],\"epoch_zero\":{\"schema_version\":2,\"epoch_number\":0,\"previous_epoch_digest\":\"0000000000000000000000000000000000000000000000000000000000000000\",\"membership_digest\":\"5c839b0d0864bba2114f2dd1832defd3b16528611167b78119e4cf7c0ef56f6f\",\"activation_height\":0,\"generation_seed\":0,\"policy_version\":\"adaptive-v2-bootstrap\",\"evidence_snapshot_id\":\"adaptive-v2-bootstrap-epoch-zero\",\"evidence_cutoff\":0,\"canonical_size_bytes\":290,\"epoch_digest\":\"2ce2c512fbbc6bdc9d72046a32273d89ba39bafb28c008fc9a3add9612b37bcc\",\"tree_count\":4,\"trees\":[{\"tree_id\":0,\"fanout\":2,\"pipeline_stretch\":2,\"members_breadth_first\":[0,1,2,3],\"wait_exempt_leaves\":[]},{\"tree_id\":1,\"fanout\":2,\"pipeline_stretch\":2,\"members_breadth_first\":[1,2,3,0],\"wait_exempt_leaves\":[]},{\"tree_id\":2,\"fanout\":2,\"pipeline_stretch\":2,\"members_breadth_first\":[2,3,0,1],\"wait_exempt_leaves\":[]},{\"tree_id\":3,\"fanout\":2,\"pipeline_stretch\":2,\"members_breadth_first\":[3,0,1,2],\"wait_exempt_leaves\":[]}]}}\n");
}
