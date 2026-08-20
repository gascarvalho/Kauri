/**
 * Copyright 2018 VMware
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

#include <array>
#include <charconv>
#include <cstdio>
#include <iostream>
#include <sstream>
#include <string>
#include <unordered_set>
#include <vector>

#include <sodium.h>

#include "salticidae/util.h"
#include "hotstuff/crypto.h"

using salticidae::Config;
using hotstuff::privkey_bt;
using hotstuff::pubkey_bt;

namespace
{

constexpr int kMaximumSecurePreallocationKeys = 1024;

bool is_lowercase_hex(const std::string &value, std::size_t expected_size)
{
    if (value.size() != expected_size)
        return false;
    for (const auto character : value)
        if (!((character >= '0' && character <= '9') ||
              (character >= 'a' && character <= 'f')))
            return false;
    return true;
}

bool is_canonical_bls_scalar(const std::string &value)
{
    static const std::string group_order =
        "73eda753299d7d483339d80809a1d805"
        "53bda402fffe5bfeffffffff00000001";
    return is_lowercase_hex(value, 64) &&
           value != std::string(64, '0') && value < group_order;
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

std::string bytes_to_hex(const std::vector<std::uint8_t> &bytes)
{
    static constexpr char alphabet[] = "0123456789abcdef";
    std::string output;
    output.reserve(bytes.size() * 2);
    for (const auto byte : bytes)
    {
        output.push_back(alphabet[byte >> 4]);
        output.push_back(alphabet[byte & 0x0f]);
    }
    return output;
}

int derive_bls_public()
{
    std::array<char, 66> input{};
    std::cin.read(input.data(), static_cast<std::streamsize>(input.size()));
    const auto count = static_cast<std::size_t>(std::cin.gcount());
    if (count != 65 || input[64] != '\n' || !std::cin.eof())
    {
        std::fprintf(
            stderr,
            "hotstuff-keygen: expected exactly one 64-byte lowercase "
            "BLS scalar followed by newline\n");
        return 2;
    }

    const std::string scalar(input.data(), 64);
    if (!is_canonical_bls_scalar(scalar))
    {
        std::fprintf(
            stderr,
            "hotstuff-keygen: BLS scalar is not canonical, nonzero, and "
            "below the BLS group order\n");
        return 2;
    }

    try
    {
        hotstuff::PrivKeyBLS private_key(decode_lowercase_hex(scalar));
        const hotstuff::PubKeyBLS public_key(private_key);
        const auto output = std::string("pub:") +
                            hotstuff::get_hex(public_key) + "\n";
        if (std::fwrite(output.data(), 1, output.size(), stdout) !=
            output.size())
            return 1;
        return 0;
    }
    catch (const std::exception &error)
    {
        std::fprintf(
            stderr,
            "hotstuff-keygen: invalid BLS scalar: %s\n",
            error.what());
        return 2;
    }
}

class SeedWiper
{
    std::vector<std::uint8_t> &seed;

public:
    explicit SeedWiper(std::vector<std::uint8_t> &value) : seed(value) {}
    ~SeedWiper()
    {
        if (!seed.empty())
            sodium_memzero(seed.data(), seed.size());
    }
};

int secure_bls_preallocation(int count)
{
    if (count < 1 || count > kMaximumSecurePreallocationKeys)
    {
        std::fprintf(
            stderr,
            "hotstuff-keygen: secure BLS preallocation count must be "
            "between 1 and %d\n",
            kMaximumSecurePreallocationKeys);
        return 2;
    }
    if (sodium_init() < 0)
    {
        std::fprintf(stderr, "hotstuff-keygen: libsodium initialization failed\n");
        return 1;
    }

    try
    {
        std::vector<std::string> lines;
        std::unordered_set<std::string> public_keys;
        std::unordered_set<std::string> private_keys;
        lines.reserve(static_cast<std::size_t>(count));
        public_keys.reserve(static_cast<std::size_t>(count));
        private_keys.reserve(static_cast<std::size_t>(count));

        for (int index = 0; index < count; ++index)
        {
            std::vector<std::uint8_t> seed(32);
            SeedWiper wipe(seed);
            randombytes_buf(seed.data(), seed.size());
            const auto private_key = bls::PopSchemeMPL::KeyGen(seed);

            std::array<std::uint8_t, bls::PrivateKey::PRIVATE_KEY_SIZE>
                private_bytes{};
            private_key.Serialize(private_bytes.data());
            const auto public_bytes = private_key.GetG1Element().Serialize();
            const auto private_hex = bytes_to_hex(
                std::vector<std::uint8_t>(
                    private_bytes.begin(), private_bytes.end()));
            const auto public_hex = bytes_to_hex(public_bytes);
            sodium_memzero(private_bytes.data(), private_bytes.size());

            if (!private_keys.insert(private_hex).second ||
                !public_keys.insert(public_hex).second)
            {
                std::fprintf(
                    stderr,
                    "hotstuff-keygen: secure BLS preallocation produced "
                    "a duplicate identity\n");
                return 1;
            }
            lines.emplace_back(
                "pub:" + public_hex + " sec:" + private_hex + "\n");
        }

        std::ostringstream buffered;
        for (const auto &line : lines)
            buffered << line;
        const auto output = buffered.str();
        if (std::fwrite(output.data(), 1, output.size(), stdout) !=
            output.size())
            return 1;
        return 0;
    }
    catch (const std::exception &error)
    {
        std::fprintf(
            stderr,
            "hotstuff-keygen: secure BLS preallocation failed: %s\n",
            error.what());
        return 1;
    }
}

int secure_bls_preallocation_from_arguments(int argc, char **argv)
{
    int count = 1;
    std::string algorithm = "bls";
    bool saw_count = false;
    bool saw_algorithm = false;
    bool saw_mode = false;
    for (int index = 1; index < argc; ++index)
    {
        const std::string argument(argv[index]);
        if (argument == "--secure-bls-preallocation")
        {
            if (saw_mode)
            {
                std::fprintf(
                    stderr,
                    "hotstuff-keygen: duplicate secure preallocation mode\n");
                return 2;
            }
            saw_mode = true;
            continue;
        }
        if (argument == "--num")
        {
            if (saw_count || index + 1 >= argc)
            {
                std::fprintf(
                    stderr,
                    "hotstuff-keygen: invalid secure preallocation count\n");
                return 2;
            }
            saw_count = true;
            const std::string value(argv[++index]);
            if (value.empty() ||
                (value.size() > 1 && value.front() == '0'))
            {
                std::fprintf(
                    stderr,
                    "hotstuff-keygen: invalid secure preallocation count\n");
                return 2;
            }
            const auto parsed = std::from_chars(
                value.data(), value.data() + value.size(), count, 10);
            if (parsed.ec != std::errc{} ||
                parsed.ptr != value.data() + value.size())
            {
                std::fprintf(
                    stderr,
                    "hotstuff-keygen: invalid secure preallocation count\n");
                return 2;
            }
            continue;
        }
        if (argument == "--algo")
        {
            if (saw_algorithm || index + 1 >= argc)
            {
                std::fprintf(
                    stderr,
                    "hotstuff-keygen: invalid secure preallocation algorithm\n");
                return 2;
            }
            saw_algorithm = true;
            algorithm = argv[++index];
            continue;
        }
        std::fprintf(
            stderr,
            "hotstuff-keygen: unsupported secure preallocation argument\n");
        return 2;
    }
    if (!saw_mode || algorithm != "bls")
    {
        std::fprintf(
            stderr,
            "hotstuff-keygen: --secure-bls-preallocation requires "
            "--algo bls\n");
        return 2;
    }
    return secure_bls_preallocation(count);
}

} // namespace

int main(int argc, char **argv) {
    for (int index = 1; index < argc; ++index)
        if (std::string(argv[index]) == "--derive-bls-public")
        {
            if (argc != 2)
            {
                std::fprintf(
                    stderr,
                    "hotstuff-keygen: --derive-bls-public cannot be "
                    "combined with other arguments\n");
                return 2;
            }
            return derive_bls_public();
        }

    for (int index = 1; index < argc; ++index)
        if (std::string(argv[index]) == "--secure-bls-preallocation")
            return secure_bls_preallocation_from_arguments(argc, argv);

    srand(5);
    
    Config config("hotstuff.gen.conf");
    privkey_bt priv_key;
    auto opt_n = Config::OptValInt::create(1);
    auto opt_algo = Config::OptValStr::create("bls");
    config.add_opt("num", opt_n, Config::SET_VAL);
    config.add_opt("algo", opt_algo, Config::SET_VAL);
    config.parse(argc, argv);
    auto &algo = opt_algo->get();
    if (algo == "secp256k1")
        priv_key = new hotstuff::PrivKeySecp256k1();
    else if (algo == "bls")
        priv_key = new hotstuff::PrivKeyBLS();
    else {
        std::fprintf(stderr, "algo not supported\n");
        return 1;
    }
    int n = opt_n->get();
    if (n < 1) {
        std::fprintf(stderr, "n must be >0\n");
        return 1;
    }
    while (n--)
    {
        priv_key->from_rand();
        pubkey_bt pub_key = priv_key->get_pubkey();
        printf("pub:%s sec:%s\n", get_hex(*pub_key).c_str(),
                            get_hex(*priv_key).c_str());
    }
    return 0;
}
