#include "hotstuff/epoch_change.h"

#include <algorithm>
#include <limits>
#include <stdexcept>
#include <type_traits>
#include <utility>

namespace hotstuff
{
namespace
{

constexpr char kEpochChangePayloadDomain[] =
    "kauri-epoch-change-payload-v1";
constexpr char kAuthorizedEpochChangeDomain[] =
    "kauri-authorized-epoch-change-v1";
constexpr std::size_t kSecp256k1SignatureBytes = 64;

class Writer final
{
public:
    template <typename UInt>
    void integer(UInt value)
    {
        static_assert(std::is_unsigned<UInt>::value,
                      "epoch-change integers must be unsigned");
        for (std::size_t shift = sizeof(UInt); shift > 0; --shift)
        {
            bytes_.push_back(static_cast<std::uint8_t>(
                value >> ((shift - 1) * 8)));
        }
    }

    void digest(const uint256_t &value)
    {
        const bytearray_t bytes = static_cast<bytearray_t>(value);
        if (bytes.size() != 32)
        {
            throw std::logic_error(
                "epoch-change digest is not exactly 32 bytes");
        }
        append(bytes);
    }

    void domain(const char *value, std::size_t size)
    {
        bytes_.insert(bytes_.end(), value, value + size);
    }

    void append(const bytearray_t &value)
    {
        bytes_.insert(bytes_.end(), value.begin(), value.end());
    }

    bytearray_t finish() &&
    {
        return std::move(bytes_);
    }

private:
    bytearray_t bytes_;
};

enum class ReadFailure : std::uint8_t
{
    truncated,
    invalid_domain,
    unsupported_schema,
    unsupported_mode,
};

struct ReadException
{
    ReadFailure failure;
};

class Reader final
{
public:
    explicit Reader(const bytearray_t &bytes) : bytes_(bytes) {}

    void domain(const char *expected, std::size_t size)
    {
        require(size);
        if (!std::equal(
                expected, expected + size, bytes_.begin() + offset_))
        {
            throw ReadException{ReadFailure::invalid_domain};
        }
        offset_ += size;
    }

    template <typename UInt>
    UInt integer()
    {
        static_assert(std::is_unsigned<UInt>::value,
                      "epoch-change integers must be unsigned");
        require(sizeof(UInt));
        UInt value = 0;
        for (std::size_t index = 0; index < sizeof(UInt); ++index)
        {
            value = static_cast<UInt>(
                (value << 8) | bytes_[offset_ + index]);
        }
        offset_ += sizeof(UInt);
        return value;
    }

    uint256_t digest()
    {
        constexpr std::size_t digest_bytes = 32;
        require(digest_bytes);
        const uint256_t value(bytes_.data() + offset_);
        offset_ += digest_bytes;
        return value;
    }

    bytearray_t bytes(std::size_t size)
    {
        require(size);
        bytearray_t result(
            bytes_.begin() + offset_, bytes_.begin() + offset_ + size);
        offset_ += size;
        return result;
    }

    bool empty() const noexcept
    {
        return offset_ == bytes_.size();
    }

private:
    void require(std::size_t size) const
    {
        if (size > bytes_.size() - offset_)
        {
            throw ReadException{ReadFailure::truncated};
        }
    }

    const bytearray_t &bytes_;
    std::size_t offset_{0};
};

void append_payload_fields(Writer &writer, const EpochChangePayload &payload)
{
    writer.integer(payload.successor_epoch_number);
    writer.digest(payload.predecessor_epoch_digest);
    writer.digest(payload.successor_epoch_digest);
    writer.integer(payload.activation_delay_blocks);
}

bytearray_t canonical_unsigned_command(
    std::uint32_t schema_version,
    EpochProtocolMode protocol_mode,
    const EpochChangePayload &payload,
    EpochChangeIssuerId issuer_id)
{
    if (schema_version != kEpochChangeSchemaVersionV1)
    {
        throw std::invalid_argument(
            "unsupported epoch-change command schema");
    }
    if (protocol_mode != EpochProtocolMode::adaptive_v2)
    {
        throw std::invalid_argument(
            "epoch-change command requires adaptive-v2 mode");
    }

    Writer writer;
    writer.domain(
        kAuthorizedEpochChangeDomain,
        sizeof(kAuthorizedEpochChangeDomain) - 1);
    writer.integer(schema_version);
    writer.integer(static_cast<std::uint8_t>(protocol_mode));
    writer.integer(issuer_id);
    append_payload_fields(writer, payload);
    return std::move(writer).finish();
}

uint256_t authorization_digest(
    std::uint32_t schema_version,
    EpochProtocolMode protocol_mode,
    const EpochChangePayload &payload,
    EpochChangeIssuerId issuer_id)
{
    return DataStream(canonical_unsigned_command(
                          schema_version,
                          protocol_mode,
                          payload,
                          issuer_id))
        .get_hash();
}

EpochChangeValidationResult validation_result(
    EpochChangeDisposition disposition,
    const uint256_t &payload_digest,
    const uint256_t &envelope_digest = {})
{
    EpochChangeValidationResult result;
    result.disposition = disposition;
    result.payload_digest = payload_digest;
    result.envelope_digest = envelope_digest;
    return result;
}

} // namespace

bool EpochChangePayload::operator==(
    const EpochChangePayload &other) const noexcept
{
    return successor_epoch_number == other.successor_epoch_number &&
           predecessor_epoch_digest == other.predecessor_epoch_digest &&
           successor_epoch_digest == other.successor_epoch_digest &&
           activation_delay_blocks == other.activation_delay_blocks;
}

bool EpochChangePayload::operator!=(
    const EpochChangePayload &other) const noexcept
{
    return !(*this == other);
}

bytearray_t canonical_serialize_epoch_change_payload(
    const EpochChangePayload &payload)
{
    Writer writer;
    writer.domain(
        kEpochChangePayloadDomain,
        sizeof(kEpochChangePayloadDomain) - 1);
    append_payload_fields(writer, payload);
    return std::move(writer).finish();
}

uint256_t epoch_change_payload_digest(const EpochChangePayload &payload)
{
    return DataStream(canonical_serialize_epoch_change_payload(payload))
        .get_hash();
}

AuthorizedEpochChange authorize_epoch_change(
    const EpochChangePayload &payload,
    EpochChangeIssuerId issuer_id,
    const PrivKeySecp256k1 &private_key)
{
    SigSecp256k1 signature;
    signature.sign(
        authorization_digest(
            kEpochChangeSchemaVersionV1,
            EpochProtocolMode::adaptive_v2,
            payload,
            issuer_id),
        private_key);
    return AuthorizedEpochChange{
        kEpochChangeSchemaVersionV1,
        EpochProtocolMode::adaptive_v2,
        payload,
        issuer_id,
        std::move(signature)};
}

bool verify_epoch_change_signature(
    const AuthorizedEpochChange &command,
    const EpochChangeIssuer &issuer) noexcept
{
    if (command.schema_version != kEpochChangeSchemaVersionV1 ||
        command.protocol_mode != EpochProtocolMode::adaptive_v2 ||
        command.issuer_id != issuer.issuer_id)
    {
        return false;
    }

    try
    {
        auto signature = command.authorization;
        return signature.verify(
            authorization_digest(
                command.schema_version,
                command.protocol_mode,
                command.payload,
                command.issuer_id),
            issuer.public_key);
    }
    catch (...)
    {
        return false;
    }
}

bytearray_t canonical_epoch_change_signing_bytes(
    const AuthorizedEpochChange &command)
{
    return canonical_unsigned_command(
        command.schema_version,
        command.protocol_mode,
        command.payload,
        command.issuer_id);
}

bytearray_t encode_authorized_epoch_change(
    const AuthorizedEpochChange &command)
{
    auto encoded = canonical_epoch_change_signing_bytes(command);
    const auto signature = command.authorization.to_bytes();
    if (signature.size() != kSecp256k1SignatureBytes)
    {
        throw std::logic_error(
            "epoch-change signature is not exactly 64 bytes");
    }
    encoded.insert(encoded.end(), signature.begin(), signature.end());
    return encoded;
}

uint256_t epoch_change_envelope_digest(
    const AuthorizedEpochChange &command)
{
    return DataStream(encode_authorized_epoch_change(command)).get_hash();
}

EpochChangeDecodeResult decode_authorized_epoch_change(
    const bytearray_t &payload,
    std::size_t maximum_payload_bytes) noexcept
{
    if (maximum_payload_bytes == 0)
    {
        return {EpochChangeWireError::invalid_limit, std::nullopt};
    }
    if (payload.size() > maximum_payload_bytes)
    {
        return {EpochChangeWireError::payload_too_large, std::nullopt};
    }

    try
    {
        Reader reader(payload);
        reader.domain(
            kAuthorizedEpochChangeDomain,
            sizeof(kAuthorizedEpochChangeDomain) - 1);
        const auto schema_version = reader.integer<std::uint32_t>();
        if (schema_version != kEpochChangeSchemaVersionV1)
        {
            throw ReadException{ReadFailure::unsupported_schema};
        }
        const auto protocol_mode = static_cast<EpochProtocolMode>(
            reader.integer<std::uint8_t>());
        if (protocol_mode != EpochProtocolMode::adaptive_v2)
        {
            throw ReadException{ReadFailure::unsupported_mode};
        }
        const auto issuer_id = reader.integer<EpochChangeIssuerId>();

        EpochChangePayload decoded_payload;
        decoded_payload.successor_epoch_number =
            reader.integer<std::uint32_t>();
        decoded_payload.predecessor_epoch_digest = reader.digest();
        decoded_payload.successor_epoch_digest = reader.digest();
        decoded_payload.activation_delay_blocks =
            reader.integer<std::uint64_t>();
        const auto signature_bytes =
            reader.bytes(kSecp256k1SignatureBytes);
        if (!reader.empty())
        {
            return {EpochChangeWireError::trailing_bytes, std::nullopt};
        }

        SigSecp256k1 signature(secp256k1_default_verify_ctx);
        signature.from_bytes(signature_bytes);
        return {
            EpochChangeWireError::none,
            AuthorizedEpochChange{
                schema_version,
                protocol_mode,
                std::move(decoded_payload),
                issuer_id,
                std::move(signature)}};
    }
    catch (const ReadException &error)
    {
        switch (error.failure)
        {
        case ReadFailure::truncated:
            return {EpochChangeWireError::truncated, std::nullopt};
        case ReadFailure::invalid_domain:
            return {EpochChangeWireError::invalid_domain, std::nullopt};
        case ReadFailure::unsupported_schema:
            return {EpochChangeWireError::unsupported_schema, std::nullopt};
        case ReadFailure::unsupported_mode:
            return {EpochChangeWireError::unsupported_mode, std::nullopt};
        }
    }
    catch (const std::bad_alloc &)
    {
        return {EpochChangeWireError::allocation_failure, std::nullopt};
    }
    catch (const std::invalid_argument &)
    {
        return {EpochChangeWireError::malformed_signature, std::nullopt};
    }
    catch (...)
    {
        return {EpochChangeWireError::internal_failure, std::nullopt};
    }
    return {EpochChangeWireError::internal_failure, std::nullopt};
}

EpochChangeVerifier::EpochChangeVerifier(
    EpochChangeIssuer issuer,
    EpochChangeDelayBounds delay_bounds)
    : issuer_(std::move(issuer)),
      delay_bounds_(delay_bounds)
{
    if (delay_bounds_.minimum_blocks == 0 ||
        delay_bounds_.maximum_blocks < delay_bounds_.minimum_blocks)
    {
        throw std::invalid_argument(
            "epoch-change delay bounds must be nonzero and ordered");
    }
}

EpochChangeValidationResult EpochChangeVerifier::validate(
    const AuthorizedEpochChange &command,
    const EpochDefinition &active_epoch,
    const EpochStore &store,
    const EpochChangeHistoryView &history) const
{
    const auto payload_digest = epoch_change_payload_digest(command.payload);
    if (command.schema_version != kEpochChangeSchemaVersionV1)
    {
        return validation_result(
            EpochChangeDisposition::unsupported_schema, payload_digest);
    }
    if (command.protocol_mode != EpochProtocolMode::adaptive_v2)
    {
        return validation_result(
            EpochChangeDisposition::unsupported_mode, payload_digest);
    }
    const auto envelope_digest = epoch_change_envelope_digest(command);
    const auto result_for = [&](EpochChangeDisposition disposition) {
        return validation_result(
            disposition, payload_digest, envelope_digest);
    };
    if (command.issuer_id != issuer_.issuer_id)
    {
        return result_for(EpochChangeDisposition::unauthorized_issuer);
    }
    if (!verify_epoch_change_signature(command, issuer_))
    {
        return result_for(EpochChangeDisposition::invalid_signature);
    }
    if (command.payload.activation_delay_blocks <
            delay_bounds_.minimum_blocks ||
        command.payload.activation_delay_blocks >
            delay_bounds_.maximum_blocks)
    {
        return result_for(EpochChangeDisposition::invalid_delay);
    }
    if (store.find_epoch(active_epoch.epoch_number()) != &active_epoch)
    {
        return result_for(EpochChangeDisposition::wrong_predecessor);
    }
    if (command.payload.successor_epoch_number <= active_epoch.epoch_number())
    {
        return result_for(EpochChangeDisposition::stale);
    }
    if (active_epoch.epoch_number() ==
            std::numeric_limits<std::uint32_t>::max() ||
        command.payload.successor_epoch_number !=
            active_epoch.epoch_number() + 1)
    {
        return result_for(EpochChangeDisposition::invalid_successor);
    }
    if (command.payload.predecessor_epoch_digest !=
        active_epoch.epoch_digest())
    {
        return result_for(EpochChangeDisposition::wrong_predecessor);
    }
    if ((history.ancestry_payload_digest &&
         *history.ancestry_payload_digest != payload_digest) ||
        (history.committed_payload_digest &&
         *history.committed_payload_digest != payload_digest))
    {
        return result_for(EpochChangeDisposition::conflicting_successor);
    }
    if (history.ancestry_payload_digest || history.committed_payload_digest)
    {
        return result_for(EpochChangeDisposition::duplicate);
    }

    const auto *const successor = store.find_epoch_by_digest(
        command.payload.successor_epoch_digest);
    if (successor == nullptr)
    {
        auto result = result_for(
            EpochChangeDisposition::defer_missing_definition);
        result.recovery_request = EpochDefinitionRequest{
            kEpochWireSchemaVersionV2,
            EpochProtocolMode::adaptive_v2,
            command.payload.successor_epoch_digest};
        return result;
    }
    if (successor->schema_version() != kEpochDefinitionSchemaVersionV2 ||
        successor->activation_height() != 0 ||
        successor->epoch_number() !=
            command.payload.successor_epoch_number ||
        successor->previous_epoch_digest() !=
            command.payload.predecessor_epoch_digest ||
        successor->epoch_digest() != command.payload.successor_epoch_digest)
    {
        return result_for(EpochChangeDisposition::invalid_definition);
    }

    auto result = result_for(EpochChangeDisposition::accepted);
    result.successor_definition = successor;
    return result;
}

} // namespace hotstuff
