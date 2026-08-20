/**
 * Copyright 2026 Goncalo Carvalho
 *
 * Licensed under the Apache License, Version 2.0 (the "License");
 * you may not use this file except in compliance with the License.
 * You may obtain a copy of the License at
 *
 *     http://www.apache.org/licenses/LICENSE-2.0
 *
 * Unless required by applicable law or agreed to in writing, software
 * distributed under the License is distributed on an "AS IS" BASIS,
 * WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
 * See the License for the specific language governing permissions and
 * limitations under the License.
 */

#include <charconv>
#include <cerrno>
#include <cstdint>
#include <filesystem>
#include <fcntl.h>
#include <iostream>
#include <limits>
#include <stdexcept>
#include <string>
#include <sys/stat.h>
#include <system_error>
#include <unistd.h>
#include <unordered_set>
#include <utility>
#include <vector>

#include "hotstuff/adaptation_manager_profile.h"
#include "hotstuff/activation_readiness_wire.h"

namespace
{

constexpr std::size_t kMaximumReadinessManifestBytes = 64 * 1024;
constexpr std::size_t kMaximumReadinessPayloadBytes = 64 * 1024;
constexpr std::size_t kMaximumReadinessMembers = 1024;
const std::string kReadinessVerificationMode =
    "--verify-adaptive-v3-readiness-v1";

bool lowercase_hex(const std::string &value, std::size_t expected_size)
{
    if (value.size() != expected_size)
        return false;
    for (const auto character : value)
        if (!((character >= '0' && character <= '9') ||
              (character >= 'a' && character <= 'f')))
            return false;
    return true;
}

hotstuff::bytearray_t decode_lowercase_hex(const std::string &value)
{
    hotstuff::bytearray_t bytes;
    bytes.reserve(value.size() / 2);
    const auto digit = [](char character) -> std::uint8_t {
        if (character >= '0' && character <= '9')
            return static_cast<std::uint8_t>(character - '0');
        return static_cast<std::uint8_t>(character - 'a' + 10);
    };
    for (std::size_t offset = 0; offset < value.size(); offset += 2)
        bytes.push_back(static_cast<std::uint8_t>(
            digit(value[offset]) * 16 + digit(value[offset + 1])));
    return bytes;
}

class FileDescriptor
{
    int value{-1};

public:
    explicit FileDescriptor(int descriptor) noexcept : value(descriptor) {}
    FileDescriptor(const FileDescriptor &) = delete;
    FileDescriptor &operator=(const FileDescriptor &) = delete;
    ~FileDescriptor()
    {
        if (value >= 0)
            ::close(value);
    }

    int get() const noexcept { return value; }
};

std::string read_bounded_regular_file(
    const std::filesystem::path &path,
    std::size_t maximum_size,
    const char *field)
{
    int raw_descriptor;
    do
    {
        raw_descriptor = ::open(
            path.c_str(), O_RDONLY | O_NOFOLLOW | O_CLOEXEC);
    } while (raw_descriptor < 0 && errno == EINTR);
    if (raw_descriptor < 0)
        throw std::invalid_argument(
            std::string("cannot securely open ") + field);
    const FileDescriptor descriptor(raw_descriptor);

    struct stat metadata{};
    int stat_result;
    do
    {
        stat_result = ::fstat(descriptor.get(), &metadata);
    } while (stat_result < 0 && errno == EINTR);
    if (stat_result < 0 || !S_ISREG(metadata.st_mode))
        throw std::invalid_argument(
            std::string(field) + " must be a regular non-symlink file");
    if (metadata.st_size <= 0 ||
        static_cast<std::uintmax_t>(metadata.st_size) > maximum_size)
        throw std::invalid_argument(
            std::string(field) + " exceeds its exact byte bound");

    std::string contents(static_cast<std::size_t>(metadata.st_size), '\0');
    std::size_t offset = 0;
    while (offset < contents.size())
    {
        const auto count = ::read(
            descriptor.get(), contents.data() + offset,
            contents.size() - offset);
        if (count > 0)
        {
            offset += static_cast<std::size_t>(count);
            continue;
        }
        if (count < 0 && errno == EINTR)
            continue;
        throw std::invalid_argument(
            std::string(field) + " changed while reading");
    }

    char trailing;
    ssize_t trailing_count;
    do
    {
        trailing_count = ::read(descriptor.get(), &trailing, 1);
    } while (trailing_count < 0 && errno == EINTR);
    if (trailing_count != 0)
        throw std::invalid_argument(
            std::string(field) + " changed while reading");
    return contents;
}

hotstuff::bytearray_t read_canonical_hex_file(
    const std::filesystem::path &path,
    std::size_t maximum_payload_bytes,
    const char *field)
{
    const auto maximum_file_bytes = maximum_payload_bytes * 2 + 1;
    const auto contents = read_bounded_regular_file(
        path, maximum_file_bytes, field);
    if (contents.size() < 3 || contents.back() != '\n')
        throw std::invalid_argument(
            std::string(field) + " must end in exactly one newline");
    const auto hex = contents.substr(0, contents.size() - 1);
    if (hex.size() % 2 != 0 || !lowercase_hex(hex, hex.size()))
        throw std::invalid_argument(
            std::string(field) + " is not canonical lowercase hex");
    return decode_lowercase_hex(hex);
}

class CanonicalManifestReader
{
    const std::string &input;
    std::size_t cursor{0};

public:
    explicit CanonicalManifestReader(const std::string &value) : input(value) {}

    void expect(const std::string &literal)
    {
        if (input.compare(cursor, literal.size(), literal) != 0)
            throw std::invalid_argument(
                "readiness manifest is not canonical JSON");
        cursor += literal.size();
    }

    std::string hex(std::size_t size)
    {
        if (cursor > input.size() || size > input.size() - cursor)
            throw std::invalid_argument("readiness manifest is truncated");
        const auto value = input.substr(cursor, size);
        if (!lowercase_hex(value, size))
            throw std::invalid_argument(
                "readiness manifest contains noncanonical hex");
        cursor += size;
        return value;
    }

    std::uint32_t replica_id()
    {
        const auto begin = cursor;
        while (cursor < input.size() && input[cursor] >= '0' &&
               input[cursor] <= '9')
            ++cursor;
        if (cursor == begin ||
            (cursor - begin > 1 && input[begin] == '0'))
            throw std::invalid_argument(
                "readiness manifest replica ID is not canonical");
        std::uint32_t value{0};
        const auto parsed = std::from_chars(
            input.data() + begin, input.data() + cursor, value, 10);
        if (parsed.ec != std::errc{} || parsed.ptr != input.data() + cursor)
            throw std::invalid_argument(
                "readiness manifest replica ID is out of range");
        return value;
    }

    std::string profile_id()
    {
        const auto begin = cursor;
        while (cursor < input.size())
        {
            const auto character = input[cursor];
            if (!((character >= '0' && character <= '9') ||
                  (character >= 'A' && character <= 'Z') ||
                  (character >= 'a' && character <= 'z') ||
                  character == '-' || character == '_' || character == '.'))
                break;
            ++cursor;
        }
        if (cursor == begin || cursor - begin > 128)
            throw std::invalid_argument(
                "readiness manifest profile ID is not canonical");
        return input.substr(begin, cursor - begin);
    }

    char current() const
    {
        if (cursor >= input.size())
            throw std::invalid_argument("readiness manifest is truncated");
        return input[cursor];
    }

    bool done() const noexcept { return cursor == input.size(); }
};

struct ReadinessPublicManifest
{
    hotstuff::uint256_t manifest_payload_digest;
    std::string membership_digest;
    std::vector<std::pair<hotstuff::ReplicaID, hotstuff::PubKeyBLS>> members;
};

ReadinessPublicManifest parse_readiness_manifest(
    const std::filesystem::path &path)
{
    auto contents = read_bounded_regular_file(
        path, kMaximumReadinessManifestBytes, "readiness public manifest");
    if (contents.back() != '\n')
        throw std::invalid_argument(
            "readiness public manifest must end in exactly one newline");
    contents.pop_back();

    CanonicalManifestReader reader(contents);
    reader.expect("{\"algorithm\":\"bls-pop\",\"domain\":\"kauri-adaptive-v3-readiness-public-key-manifest-v1\",\"members\":[");
    ReadinessPublicManifest manifest;
    manifest.manifest_payload_digest =
        hotstuff::DataStream(contents + "\n").get_hash();

    std::unordered_set<std::string> public_keys;
    hotstuff::ReplicaID previous_id{0};
    bool first = true;
    while (true)
    {
        if (manifest.members.size() >= kMaximumReadinessMembers)
            throw std::invalid_argument(
                "readiness manifest exceeds member bound");
        reader.expect("{\"public_key_hex\":\"");
        const auto public_key_hex = reader.hex(96);
        reader.expect("\",\"replica_id\":");
        const auto replica_id = reader.replica_id();
        reader.expect("}");
        if ((!first && replica_id <= previous_id) ||
            !public_keys.insert(public_key_hex).second)
            throw std::invalid_argument(
                "readiness manifest members are not distinct and sorted");
        manifest.members.emplace_back(
            replica_id,
            hotstuff::PubKeyBLS(decode_lowercase_hex(public_key_hex)));
        first = false;
        previous_id = replica_id;

        if (reader.current() == ']')
        {
            reader.expect("]");
            break;
        }
        reader.expect(",");
    }
    reader.expect(",\"membership_digest\":\"");
    manifest.membership_digest = reader.hex(64);
    reader.expect("\"");
    reader.expect(",\"profile_id\":\"");
    static_cast<void>(reader.profile_id());
    reader.expect("\",\"profile_sha256\":\"");
    static_cast<void>(reader.hex(64));
    reader.expect("\",\"protocol_mode\":\"adaptive_v3\",\"schema_version\":1}");
    if (!reader.done() || manifest.members.empty())
        throw std::invalid_argument(
            "readiness manifest has trailing or missing content");

    const auto computed =
        hotstuff::canonical_activation_readiness_membership_digest(
            manifest.members)
            .to_hex();
    if (computed != manifest.membership_digest)
        throw std::invalid_argument(
            "readiness manifest membership digest does not match members");
    return manifest;
}

int verify_adaptive_v3_readiness(
    const std::filesystem::path &manifest_path,
    const std::filesystem::path &certificate_path,
    const std::filesystem::path &identity_path)
{
    try
    {
        const auto manifest = parse_readiness_manifest(manifest_path);
        const auto certificate_bytes = read_canonical_hex_file(
            certificate_path, kMaximumReadinessPayloadBytes,
            "readiness certificate");
        const auto identity_bytes = read_canonical_hex_file(
            identity_path, kMaximumReadinessPayloadBytes,
            "readiness expected identity");
        const hotstuff::ActivationReadinessWireLimits limits{
            kMaximumReadinessPayloadBytes, manifest.members.size()};
        const auto certificate =
            hotstuff::decode_activation_readiness_certificate(
                certificate_bytes, limits);
        if (!certificate)
            throw std::invalid_argument(
                "readiness certificate is malformed or noncanonical");
        const auto identity = hotstuff::decode_activation_ready_identity_v1(
            identity_bytes, limits);
        if (!identity)
            throw std::invalid_argument(
                "readiness expected identity is malformed or noncanonical");

        const auto valid =
            hotstuff::verify_activation_readiness_certificate(
                *certificate.value,
                *identity.value,
                identity.value->membership_digest,
                manifest.members);
        std::cout
            << "{\"certificate_digest\":\""
            << certificate.value->certificate_digest.to_hex()
            << "\",\"certificate_payload_digest\":\""
            << hotstuff::DataStream(certificate_bytes).get_hash().to_hex()
            << "\",\"expected_identity_payload_digest\":\""
            << hotstuff::DataStream(identity_bytes).get_hash().to_hex()
            << "\",\"manifest_payload_digest\":\""
            << manifest.manifest_payload_digest.to_hex()
            << "\",\"member_count\":" << manifest.members.size()
            << ",\"membership_digest\":\""
            << identity.value->membership_digest.to_hex()
            << "\",\"observation_count\":"
            << certificate.value->observations.size()
            << ",\"schema\":\"kauri-adaptive-v3-readiness-verification-v1\""
            << ",\"valid\":" << (valid ? "true" : "false") << "}\n";
        return valid ? 0 : 1;
    }
    catch (const std::exception &error)
    {
        std::cerr << "epoch-profile-digest: " << error.what() << '\n';
        return 2;
    }
}

std::uint32_t parse_positive_u32(
    const char *raw,
    const char *field)
{
    const std::string text(raw == nullptr ? "" : raw);
    if (text.empty() ||
        (text.size() > 1 && text.front() == '0'))
    {
        throw std::invalid_argument(
            std::string(field) +
            " must be a canonical positive unsigned decimal");
    }

    std::uint32_t value{0};
    const auto parsed = std::from_chars(
        text.data(), text.data() + text.size(), value, 10);
    if (parsed.ec != std::errc{} ||
        parsed.ptr != text.data() + text.size() ||
        value == 0)
    {
        throw std::invalid_argument(
            std::string(field) +
            " must be a canonical positive unsigned decimal");
    }
    return value;
}

void print_members(std::ostream &output,
                   const std::vector<hotstuff::ReplicaID> &members)
{
    output << '[';
    for (std::size_t index = 0; index < members.size(); ++index)
    {
        if (index != 0)
            output << ',';
        output << members[index];
    }
    output << ']';
}

void print_json(
    std::ostream &output,
    const hotstuff::AdaptiveV2ManagerRuntimeShape &shape,
    const std::vector<hotstuff::ReplicaID> &membership,
    const hotstuff::EpochDefinitionInput &epoch)
{
    const auto digest = hotstuff::compute_epoch_digest(epoch);
    const auto canonical = hotstuff::canonical_serialize_epoch(epoch);

    output
        << "{\"schema\":\"kauri-adaptive-v2-epoch-profile-digest-v1\""
        << ",\"replica_count\":" << shape.quorum.replica_count
        << ",\"fault_threshold\":" << shape.quorum.fault_threshold
        << ",\"quorum\":" << shape.quorum.quorum
        << ",\"fanout\":" << shape.tree_shape.fanout
        << ",\"pipeline_stretch\":"
        << shape.tree_shape.pipeline_stretch
        << ",\"membership\":";
    print_members(output, membership);
    output
        << ",\"epoch_zero\":{\"schema_version\":"
        << epoch.schema_version
        << ",\"epoch_number\":" << epoch.epoch_number
        << ",\"previous_epoch_digest\":\""
        << epoch.previous_epoch_digest.to_hex()
        << "\",\"membership_digest\":\""
        << epoch.membership_digest.to_hex()
        << "\",\"activation_height\":" << epoch.activation_height
        << ",\"generation_seed\":" << epoch.generation_seed
        << ",\"policy_version\":\"" << epoch.policy_version
        << "\",\"evidence_snapshot_id\":\""
        << epoch.evidence_snapshot_id
        << "\",\"evidence_cutoff\":" << epoch.evidence_cutoff
        << ",\"canonical_size_bytes\":" << canonical.size()
        << ",\"epoch_digest\":\"" << digest.to_hex()
        << "\",\"tree_count\":" << epoch.trees.size()
        << ",\"trees\":[";

    for (std::size_t index = 0; index < epoch.trees.size(); ++index)
    {
        const auto &tree = epoch.trees[index];
        if (index != 0)
            output << ',';
        output
            << "{\"tree_id\":" << tree.tree_id
            << ",\"fanout\":" << tree.fanout
            << ",\"pipeline_stretch\":" << tree.pipeline_stretch
            << ",\"members_breadth_first\":";
        print_members(output, tree.members_breadth_first);
        output << ",\"wait_exempt_leaves\":";
        print_members(output, tree.wait_exempt_leaves);
        output << '}';
    }
    output << "]}}\n";
}

} // namespace

int main(int argc, char **argv)
{
    if (argc >= 2 && argv[1] == kReadinessVerificationMode)
    {
        if (argc != 5)
        {
            std::cerr
                << "usage: epoch-profile-digest "
                << kReadinessVerificationMode
                << " <public-manifest.json> <certificate.hex> "
                   "<expected-identity.hex>\n";
            return 2;
        }
        return verify_adaptive_v3_readiness(argv[2], argv[3], argv[4]);
    }

    if (argc != 4)
    {
        std::cerr
            << "usage: epoch-profile-digest "
            << "<replica-count> <fanout> <pipeline-stretch>\n";
        return 2;
    }

    try
    {
        const auto replica_count =
            parse_positive_u32(argv[1], "replica count");
        const auto fanout = parse_positive_u32(argv[2], "fanout");
        const auto pipeline_stretch =
            parse_positive_u32(argv[3], "pipeline stretch");
        if (replica_count >
            hotstuff::kMaximumAdaptiveV2ManagerMembers)
        {
            throw std::invalid_argument(
                "replica count exceeds the adaptive-v2 manager bound");
        }

        std::vector<hotstuff::ReplicaID> membership;
        membership.reserve(replica_count);
        for (std::uint32_t member = 0;
             member < replica_count;
             ++member)
        {
            membership.push_back(member);
        }

        const auto shape =
            hotstuff::derive_adaptive_v2_manager_runtime_shape(
                membership, fanout, pipeline_stretch);
        const auto epoch =
            hotstuff::derive_adaptive_v2_cyclic_epoch_zero(
                membership, fanout, pipeline_stretch);
        if (!shape.has_value() || !epoch.has_value())
        {
            throw std::invalid_argument(
                "arguments must define a bounded contiguous N=3f+1 "
                "adaptive-v2 manager shape");
        }

        print_json(std::cout, *shape, membership, *epoch);
        return 0;
    }
    catch (const std::exception &error)
    {
        std::cerr << "epoch-profile-digest: " << error.what() << '\n';
        return 2;
    }
}
