#include "hotstuff/structured_event.h"

#include <algorithm>
#include <charconv>
#include <deque>
#include <limits>
#include <new>
#include <string>
#include <string_view>
#include <utility>

namespace hotstuff
{

namespace
{

class LineLimitExceeded final
{};

class ReentrancyScope final
{
public:
    explicit ReentrancyScope(bool &active) noexcept
        : active_(active)
    {
        active_ = true;
    }

    ~ReentrancyScope() noexcept
    {
        active_ = false;
    }

    ReentrancyScope(const ReentrancyScope &) = delete;
    ReentrancyScope &operator=(const ReentrancyScope &) = delete;

private:
    bool &active_;
};

class JsonLineBuilder final
{
public:
    explicit JsonLineBuilder(std::size_t maximum_bytes)
        : maximum_bytes_(maximum_bytes)
    {
    }

    void append(std::string_view text)
    {
        ensure(text.size());
        value_.append(text.data(), text.size());
    }

    void append(char value)
    {
        ensure(1);
        value_.push_back(value);
    }

    template<typename Integer>
    void append_integer(Integer value)
    {
        char buffer[std::numeric_limits<Integer>::digits10 + 4];
        const auto converted = std::to_chars(
            buffer, buffer + sizeof(buffer), value);
        if (converted.ec != std::errc{})
            throw LineLimitExceeded{};
        append(std::string_view{
            buffer,
            static_cast<std::size_t>(converted.ptr - buffer)});
    }

    void append_escaped(std::string_view value)
    {
        static constexpr char hexadecimal[] = "0123456789abcdef";
        append('"');
        for (const unsigned char byte : value)
        {
            switch (byte)
            {
                case '"':
                    append("\\\"");
                    break;
                case '\\':
                    append("\\\\");
                    break;
                case '\b':
                    append("\\b");
                    break;
                case '\f':
                    append("\\f");
                    break;
                case '\n':
                    append("\\n");
                    break;
                case '\r':
                    append("\\r");
                    break;
                case '\t':
                    append("\\t");
                    break;
                default:
                    if (byte < 0x20)
                    {
                        char escape[]{
                            '\\',
                            'u',
                            '0',
                            '0',
                            hexadecimal[(byte >> 4) & 0x0f],
                            hexadecimal[byte & 0x0f]};
                        append(std::string_view{escape, sizeof(escape)});
                    }
                    else
                    {
                        append(static_cast<char>(byte));
                    }
                    break;
            }
        }
        append('"');
    }

    std::string finish()
    {
        append('\n');
        return std::move(value_);
    }

private:
    void ensure(std::size_t additional) const
    {
        if (additional > maximum_bytes_ - std::min(
                maximum_bytes_, value_.size()))
            throw LineLimitExceeded{};
    }

    const std::size_t maximum_bytes_;
    std::string value_;
};

const char *source_kind_name(StructuredEventSourceKind kind) noexcept
{
    switch (kind)
    {
        case StructuredEventSourceKind::replica:
            return "replica";
        case StructuredEventSourceKind::adaptation_manager:
            return "adaptation_manager";
        case StructuredEventSourceKind::orchestrator:
            return "orchestrator";
        case StructuredEventSourceKind::workload_client:
            return "workload_client";
    }
    return nullptr;
}

bool valid_source_kind(StructuredEventSourceKind kind) noexcept
{
    return source_kind_name(kind) != nullptr;
}

bool same_source(const StructuredEventSource &left,
                 const StructuredEventSource &right) noexcept
{
    return left.kind == right.kind &&
           left.logical_id == right.logical_id &&
           left.instance_id == right.instance_id;
}

bool valid_utf8(std::string_view value) noexcept
{
    std::size_t index = 0;
    while (index < value.size())
    {
        const auto lead = static_cast<std::uint8_t>(value[index++]);
        if (lead < 0x80)
            continue;

        std::size_t continuation_bytes = 0;
        std::uint8_t second_minimum = 0x80;
        std::uint8_t second_maximum = 0xbf;
        if (lead >= 0xc2 && lead <= 0xdf)
        {
            continuation_bytes = 1;
        }
        else if (lead >= 0xe0 && lead <= 0xef)
        {
            continuation_bytes = 2;
            if (lead == 0xe0)
                second_minimum = 0xa0;
            else if (lead == 0xed)
                second_maximum = 0x9f;
        }
        else if (lead >= 0xf0 && lead <= 0xf4)
        {
            continuation_bytes = 3;
            if (lead == 0xf0)
                second_minimum = 0x90;
            else if (lead == 0xf4)
                second_maximum = 0x8f;
        }
        else
        {
            return false;
        }

        for (std::size_t continuation_index = 0;
             continuation_index < continuation_bytes;
             ++continuation_index)
        {
            if (index == value.size())
                return false;
            const auto continuation =
                static_cast<std::uint8_t>(value[index++]);
            if (continuation_index == 0)
            {
                if (continuation < second_minimum ||
                    continuation > second_maximum)
                    return false;
            }
            else if (continuation < 0x80 || continuation > 0xbf)
            {
                return false;
            }
        }
    }
    return true;
}

bool valid_identity(const std::string &value) noexcept
{
    return !value.empty() && valid_utf8(value);
}

bool valid_source_identity(const StructuredEventSource &source) noexcept
{
    return valid_source_kind(source.kind) &&
           valid_identity(source.logical_id) &&
           valid_identity(source.instance_id);
}

bool same_source_token(const StructuredEventSourceToken &token,
                       const StructuredEventConfig &config) noexcept
{
    return token.run_id == config.run_id &&
           same_source(token.source, config.source);
}

bool valid_cursor(const StructuredEventCursor &cursor,
                  const StructuredEventConfig &config) noexcept
{
    const bool has_sequence = cursor.last_source_sequence != 0;
    const bool has_time = cursor.has_last_monotonic_ns;
    const bool has_token = cursor.source_token.has_value();

    if (!has_sequence && !has_time && !has_token)
        return cursor.last_monotonic_ns == 0;
    if (!has_sequence || !has_time || !has_token)
        return false;
    return same_source_token(*cursor.source_token, config);
}

bool payload_type(const StructuredEventPayload &payload,
                  StructuredEventType &type) noexcept
{
    switch (payload.index())
    {
        case 0:
            switch (std::get<ProcessLifecycleEvent>(payload).state)
            {
                case ProcessLifecycleState::started:
                    type = StructuredEventType::process_started;
                    return true;
                case ProcessLifecycleState::ready:
                    type = StructuredEventType::process_ready;
                    return true;
                case ProcessLifecycleState::stopping:
                    type = StructuredEventType::process_stopping;
                    return true;
                case ProcessLifecycleState::stopped:
                    type = StructuredEventType::process_stopped;
                    return true;
                case ProcessLifecycleState::forced_crash_requested:
                    type = StructuredEventType::process_forced_crash_requested;
                    return true;
                case ProcessLifecycleState::exited:
                    type = StructuredEventType::process_exited;
                    return true;
            }
            return false;
        case 1:
            switch (std::get<EpochLifecycleEvent>(payload).transition)
            {
                case EpochLifecycleTransition::generated:
                    type = StructuredEventType::epoch_generated;
                    return true;
                case EpochLifecycleTransition::staged:
                    type = StructuredEventType::epoch_staged;
                    return true;
                case EpochLifecycleTransition::acknowledged:
                    type = StructuredEventType::epoch_acknowledged;
                    return true;
                case EpochLifecycleTransition::activation_armed:
                    type = StructuredEventType::epoch_activation_armed;
                    return true;
                case EpochLifecycleTransition::activated:
                    type = StructuredEventType::epoch_activated;
                    return true;
            }
            return false;
        case 2:
            type = StructuredEventType::block_committed;
            return true;
        default:
            return false;
    }
}

bool adaptive_payload_type(
    const AdaptiveAggregationStructuredEvent &event,
    StructuredEventType &type) noexcept
{
    switch (event.transition)
    {
        case AdaptiveAggregationTransition::configuration_active:
            type = StructuredEventType::adaptive_configuration_active;
            return true;
        case AdaptiveAggregationTransition::required_set_ready:
            type = StructuredEventType::aggregation_required_set_ready;
            return true;
        case AdaptiveAggregationTransition::initial_reserved:
            type = StructuredEventType::aggregation_initial_reserved;
            return true;
        case AdaptiveAggregationTransition::initial_enqueued:
            type = StructuredEventType::aggregation_initial_enqueued;
            return true;
        case AdaptiveAggregationTransition::initial_committed:
            type = StructuredEventType::aggregation_initial_committed;
            return true;
        case AdaptiveAggregationTransition::initial_released:
            type = StructuredEventType::aggregation_initial_released;
            return true;
        case AdaptiveAggregationTransition::delta_reserved:
            type = StructuredEventType::aggregation_delta_reserved;
            return true;
        case AdaptiveAggregationTransition::delta_enqueued:
            type = StructuredEventType::aggregation_delta_enqueued;
            return true;
        case AdaptiveAggregationTransition::delta_committed:
            type = StructuredEventType::aggregation_delta_committed;
            return true;
        case AdaptiveAggregationTransition::delta_released:
            type = StructuredEventType::aggregation_delta_released;
            return true;
        case AdaptiveAggregationTransition::delta_rejected:
            type = StructuredEventType::aggregation_delta_rejected;
            return true;
        case AdaptiveAggregationTransition::required_branch_incomplete:
            type = StructuredEventType::aggregation_required_branch_incomplete;
            return true;
        case AdaptiveAggregationTransition::
                 wait_exempt_absent_at_observation_deadline:
            type = StructuredEventType::aggregation_wait_exempt_absent;
            return true;
        case AdaptiveAggregationTransition::wait_exempt_late_accepted:
            type = StructuredEventType::aggregation_wait_exempt_late_accepted;
            return true;
        case AdaptiveAggregationTransition::retry_exhausted:
            type = StructuredEventType::aggregation_retry_exhausted;
            return true;
        case AdaptiveAggregationTransition::proposal_aborted:
            type = StructuredEventType::aggregation_proposal_aborted;
            return true;
        case AdaptiveAggregationTransition::root_quorum_progress:
            type = StructuredEventType::aggregation_root_quorum_progress;
            return true;
        case AdaptiveAggregationTransition::root_qc_published:
            type = StructuredEventType::aggregation_root_qc_published;
            return true;
    }
    return false;
}

bool strictly_increasing(const std::vector<ReplicaID> &values) noexcept
{
    return std::adjacent_find(
               values.begin(), values.end(),
               [](ReplicaID left, ReplicaID right) {
                   return left >= right;
               }) == values.end();
}

bool valid_adaptive_payload(
    const AdaptiveAggregationStructuredEvent &event) noexcept
{
    StructuredEventType ignored{};
    if (!adaptive_payload_type(event, ignored) ||
        event.configuration.epoch_digest == uint256_t{} ||
        !valid_utf8(event.rejection_reason) ||
        !strictly_increasing(event.wait_exempt_signers) ||
        !strictly_increasing(event.accepted_signers) ||
        !strictly_increasing(event.absent_direct_children) ||
        !strictly_increasing(event.missing_optional_signers))
        return false;

    if (event.transition !=
            AdaptiveAggregationTransition::configuration_active &&
        (!event.block_hash.has_value() ||
         *event.block_hash == uint256_t{} ||
         !event.context_generation.has_value() ||
         *event.context_generation == 0))
        return false;
    if (event.transition ==
            AdaptiveAggregationTransition::configuration_active &&
        (event.block_hash.has_value() ||
         event.context_generation.has_value()))
        return false;
    if ((event.transition ==
             AdaptiveAggregationTransition::delta_rejected ||
         event.transition ==
             AdaptiveAggregationTransition::proposal_aborted) &&
        event.rejection_reason.empty())
        return false;
    const bool root_observation =
        event.transition ==
            AdaptiveAggregationTransition::root_quorum_progress ||
        event.transition ==
            AdaptiveAggregationTransition::root_qc_published;
    if ((root_observation ||
         event.transition ==
             AdaptiveAggregationTransition::configuration_active) &&
        event.global_quorum == 0)
        return false;
    if (root_observation &&
        event.root_signer_count != event.accepted_signers.size())
        return false;
    if (event.transition ==
            AdaptiveAggregationTransition::root_qc_published &&
        event.root_signer_count < event.global_quorum)
        return false;

    ReplicaID previous_child{0};
    bool first_child = true;
    for (const auto &gap : event.required_branch_gaps)
    {
        if (gap.missing_required_signers.empty() ||
            !strictly_increasing(gap.missing_required_signers) ||
            (!first_child && gap.direct_child <= previous_child))
            return false;
        previous_child = gap.direct_child;
        first_child = false;
    }
    return true;
}

void append_configuration(JsonLineBuilder &builder,
                          const ConfigurationId &configuration)
{
    builder.append("\"epoch_number\":");
    builder.append_integer(configuration.epoch_number);
    builder.append(",\"tree_id\":");
    builder.append_integer(configuration.tree_id);
    builder.append(",\"epoch_digest\":");
    builder.append_escaped(configuration.epoch_digest.to_hex());
}

void append_process_payload(JsonLineBuilder &builder,
                            const ProcessLifecycleEvent &event)
{
    builder.append("{\"exit_status\":");
    if (event.exit_status)
        builder.append_integer(*event.exit_status);
    else
        builder.append("null");
    builder.append('}');
}

void append_epoch_payload(JsonLineBuilder &builder,
                          const EpochLifecycleEvent &event)
{
    builder.append('{');
    append_configuration(builder, event.configuration);
    builder.append(",\"activation_height\":");
    builder.append_integer(event.activation_height);
    builder.append('}');
}

void append_commit_payload(JsonLineBuilder &builder,
                           const StructuredEventConfig &config,
                           const CommitStructuredEvent &event)
{
    builder.append("{\"block_height\":");
    builder.append_integer(event.block_height);
    builder.append(",\"block_hash\":");
    builder.append_escaped(event.block_hash.to_hex());
    builder.append(",\"parent_hash\":");
    if (event.parent_hash)
        builder.append_escaped(event.parent_hash->to_hex());
    else
        builder.append("null");
    builder.append(",\"transaction_count\":");
    builder.append_integer(event.transaction_count);
    builder.append(",\"designated_observer\":");
    const bool designated = config.designated_commit_observer &&
        same_source(config.source, *config.designated_commit_observer);
    builder.append(designated ? "true" : "false");
    builder.append(",\"decision_proof\":{");
    append_configuration(builder, event.decision_proof.configuration);
    builder.append(",\"block_hash\":");
    builder.append_escaped(event.decision_proof.block_hash.to_hex());
    builder.append("},\"view_generation\":");
    if (event.view_generation)
        builder.append_integer(*event.view_generation);
    else
        builder.append("null");
    builder.append(",\"commit_batch_index\":");
    builder.append_integer(event.commit_batch_index);
    builder.append('}');
}

void append_replica_ids(JsonLineBuilder &builder,
                        const std::vector<ReplicaID> &values)
{
    builder.append('[');
    bool first = true;
    for (const auto value : values)
    {
        if (!first)
            builder.append(',');
        builder.append_integer(value);
        first = false;
    }
    builder.append(']');
}

void append_adaptive_payload(
    JsonLineBuilder &builder,
    const AdaptiveAggregationStructuredEvent &event)
{
    builder.append('{');
    append_configuration(builder, event.configuration);
    builder.append(",\"block_hash\":");
    if (event.block_hash)
        builder.append_escaped(event.block_hash->to_hex());
    else
        builder.append("null");
    builder.append(",\"context_generation\":");
    if (event.context_generation)
        builder.append_integer(*event.context_generation);
    else
        builder.append("null");
    builder.append(",\"observer_replica\":");
    builder.append_integer(event.observer_replica);
    builder.append(",\"wait_exempt_signers\":");
    append_replica_ids(builder, event.wait_exempt_signers);
    builder.append(",\"accepted_signers\":");
    append_replica_ids(builder, event.accepted_signers);
    builder.append(",\"absent_direct_children\":");
    append_replica_ids(builder, event.absent_direct_children);
    builder.append(",\"missing_optional_signers\":");
    append_replica_ids(builder, event.missing_optional_signers);
    builder.append(",\"required_branch_gaps\":[");
    bool first = true;
    for (const auto &gap : event.required_branch_gaps)
    {
        if (!first)
            builder.append(',');
        builder.append("{\"direct_child\":");
        builder.append_integer(gap.direct_child);
        builder.append(",\"missing_required_signers\":");
        append_replica_ids(builder, gap.missing_required_signers);
        builder.append('}');
        first = false;
    }
    builder.append("]");
    builder.append(",\"root_signer_count\":");
    builder.append_integer(event.root_signer_count);
    builder.append(",\"global_quorum\":");
    builder.append_integer(event.global_quorum);
    builder.append(",\"rejection_reason\":");
    if (event.rejection_reason.empty())
        builder.append("null");
    else
        builder.append_escaped(event.rejection_reason);
    builder.append('}');
}

std::string serialize_event(const StructuredEventConfig &config,
                            const StructuredEventPayload &payload,
                            StructuredEventType type,
                            std::uint64_t sequence,
                            std::uint64_t monotonic_ns)
{
    JsonLineBuilder builder(config.limits.maximum_line_bytes);
    builder.append("{\"event_schema_version\":");
    builder.append_integer(kStructuredEventSchemaVersion);
    builder.append(",\"run_id\":");
    builder.append_escaped(config.run_id);
    builder.append(",\"source_kind\":");
    builder.append_escaped(source_kind_name(config.source.kind));
    builder.append(",\"source_id\":");
    builder.append_escaped(config.source.logical_id);
    builder.append(",\"source_instance\":");
    builder.append_escaped(config.source.instance_id);
    builder.append(",\"source_sequence\":");
    builder.append_integer(sequence);
    builder.append(",\"source_monotonic_ns\":");
    builder.append_integer(monotonic_ns);
    builder.append(",\"event_type\":");
    builder.append_escaped(structured_event_type_name(type));
    builder.append(",\"payload\":");

    switch (payload.index())
    {
        case 0:
            append_process_payload(
                builder, std::get<ProcessLifecycleEvent>(payload));
            break;
        case 1:
            append_epoch_payload(
                builder, std::get<EpochLifecycleEvent>(payload));
            break;
        case 2:
            append_commit_payload(
                builder, config, std::get<CommitStructuredEvent>(payload));
            break;
        default:
            throw std::bad_variant_access{};
    }
    builder.append('}');
    return builder.finish();
}

std::string serialize_adaptive_event(
    const StructuredEventConfig &config,
    const AdaptiveAggregationStructuredEvent &event,
    StructuredEventType type,
    std::uint64_t sequence,
    std::uint64_t monotonic_ns)
{
    JsonLineBuilder builder(config.limits.maximum_line_bytes);
    builder.append("{\"event_schema_version\":");
    builder.append_integer(kStructuredEventSchemaVersion);
    builder.append(",\"run_id\":");
    builder.append_escaped(config.run_id);
    builder.append(",\"source_kind\":");
    builder.append_escaped(source_kind_name(config.source.kind));
    builder.append(",\"source_id\":");
    builder.append_escaped(config.source.logical_id);
    builder.append(",\"source_instance\":");
    builder.append_escaped(config.source.instance_id);
    builder.append(",\"source_sequence\":");
    builder.append_integer(sequence);
    builder.append(",\"source_monotonic_ns\":");
    builder.append_integer(monotonic_ns);
    builder.append(",\"event_type\":");
    builder.append_escaped(structured_event_type_name(type));
    builder.append(",\"payload\":");
    append_adaptive_payload(builder, event);
    builder.append('}');
    return builder.finish();
}

bool add_identity_bytes(std::size_t value,
                        std::size_t maximum,
                        std::size_t &total) noexcept
{
    if (value > maximum || value > std::numeric_limits<std::size_t>::max() - total)
        return false;
    total += value;
    return true;
}

bool valid_identity_limits(const StructuredEventConfig &config) noexcept
{
    std::size_t total = 0;
    const auto per_identity = config.limits.maximum_identity_bytes;
    if (!add_identity_bytes(config.run_id.size(), per_identity, total) ||
        !add_identity_bytes(
            config.source.logical_id.size(), per_identity, total) ||
        !add_identity_bytes(
            config.source.instance_id.size(), per_identity, total))
        return false;

    if (config.designated_commit_observer &&
        (!add_identity_bytes(
             config.designated_commit_observer->logical_id.size(),
             per_identity,
             total) ||
         !add_identity_bytes(
             config.designated_commit_observer->instance_id.size(),
             per_identity,
             total)))
        return false;

    return total <= config.limits.maximum_total_identity_bytes;
}

bool valid_configuration(const StructuredEventConfig &config) noexcept
{
    return config.limits.maximum_line_bytes != 0 &&
           config.limits.maximum_queued_events != 0 &&
           config.limits.maximum_queued_bytes != 0 &&
           config.limits.maximum_identity_bytes != 0 &&
           config.limits.maximum_total_identity_bytes != 0 &&
           valid_identity(config.run_id) &&
           valid_source_identity(config.source) &&
           (!config.designated_commit_observer ||
            valid_source_identity(*config.designated_commit_observer));
}

bool is_hexadecimal(std::uint8_t value) noexcept
{
    return (value >= '0' && value <= '9') ||
           (value >= 'a' && value <= 'f') ||
           (value >= 'A' && value <= 'F');
}

std::uint16_t hexadecimal_value(std::uint8_t value) noexcept
{
    if (value >= '0' && value <= '9')
        return static_cast<std::uint16_t>(value - '0');
    if (value >= 'a' && value <= 'f')
        return static_cast<std::uint16_t>(value - 'a' + 10);
    return static_cast<std::uint16_t>(value - 'A' + 10);
}

class JsonSyntaxParser final
{
public:
    JsonSyntaxParser(const std::uint8_t *begin,
                     const std::uint8_t *end) noexcept
        : cursor_(begin), end_(end)
    {
    }

    bool parse() noexcept
    {
        skip_whitespace();
        if (!parse_value(0))
            return false;
        skip_whitespace();
        return cursor_ == end_;
    }

private:
    static constexpr std::size_t maximum_depth = 256;

    void skip_whitespace() noexcept
    {
        while (cursor_ != end_ &&
               (*cursor_ == ' ' || *cursor_ == '\t' ||
                *cursor_ == '\r' || *cursor_ == '\n'))
            ++cursor_;
    }

    bool consume(std::uint8_t expected) noexcept
    {
        if (cursor_ == end_ || *cursor_ != expected)
            return false;
        ++cursor_;
        return true;
    }

    bool consume_literal(const char *literal) noexcept
    {
        for (const char *character = literal; *character != '\0'; ++character)
        {
            if (!consume(static_cast<std::uint8_t>(*character)))
                return false;
        }
        return true;
    }

    bool parse_value(std::size_t depth) noexcept
    {
        if (cursor_ == end_)
            return false;
        switch (*cursor_)
        {
            case '{':
                return parse_object(depth);
            case '[':
                return parse_array(depth);
            case '"':
                return parse_string();
            case 't':
                return consume_literal("true");
            case 'f':
                return consume_literal("false");
            case 'n':
                return consume_literal("null");
            default:
                return parse_number();
        }
    }

    bool parse_object(std::size_t depth) noexcept
    {
        if (depth >= maximum_depth || !consume('{'))
            return false;
        skip_whitespace();
        if (consume('}'))
            return true;
        while (true)
        {
            if (!parse_string())
                return false;
            skip_whitespace();
            if (!consume(':'))
                return false;
            skip_whitespace();
            if (!parse_value(depth + 1))
                return false;
            skip_whitespace();
            if (consume('}'))
                return true;
            if (!consume(','))
                return false;
            skip_whitespace();
        }
    }

    bool parse_array(std::size_t depth) noexcept
    {
        if (depth >= maximum_depth || !consume('['))
            return false;
        skip_whitespace();
        if (consume(']'))
            return true;
        while (true)
        {
            if (!parse_value(depth + 1))
                return false;
            skip_whitespace();
            if (consume(']'))
                return true;
            if (!consume(','))
                return false;
            skip_whitespace();
        }
    }

    bool parse_unicode_escape() noexcept
    {
        std::uint16_t code_unit = 0;
        for (std::size_t index = 0; index < 4; ++index)
        {
            if (cursor_ == end_ || !is_hexadecimal(*cursor_))
                return false;
            code_unit = static_cast<std::uint16_t>(
                code_unit * 16 + hexadecimal_value(*cursor_++));
        }

        if (code_unit >= 0xdc00 && code_unit <= 0xdfff)
            return false;
        if (code_unit < 0xd800 || code_unit > 0xdbff)
            return true;
        if (!consume('\\') || !consume('u'))
            return false;

        std::uint16_t low_surrogate = 0;
        for (std::size_t index = 0; index < 4; ++index)
        {
            if (cursor_ == end_ || !is_hexadecimal(*cursor_))
                return false;
            low_surrogate = static_cast<std::uint16_t>(
                low_surrogate * 16 + hexadecimal_value(*cursor_++));
        }
        return low_surrogate >= 0xdc00 && low_surrogate <= 0xdfff;
    }

    bool parse_utf8() noexcept
    {
        const auto lead = *cursor_++;
        std::size_t continuation_bytes = 0;
        std::uint8_t second_minimum = 0x80;
        std::uint8_t second_maximum = 0xbf;
        if (lead >= 0xc2 && lead <= 0xdf)
        {
            continuation_bytes = 1;
        }
        else if (lead >= 0xe0 && lead <= 0xef)
        {
            continuation_bytes = 2;
            if (lead == 0xe0)
                second_minimum = 0xa0;
            else if (lead == 0xed)
                second_maximum = 0x9f;
        }
        else if (lead >= 0xf0 && lead <= 0xf4)
        {
            continuation_bytes = 3;
            if (lead == 0xf0)
                second_minimum = 0x90;
            else if (lead == 0xf4)
                second_maximum = 0x8f;
        }
        else
        {
            return false;
        }

        for (std::size_t index = 0; index < continuation_bytes; ++index)
        {
            if (cursor_ == end_)
                return false;
            const auto continuation = *cursor_++;
            if (index == 0)
            {
                if (continuation < second_minimum ||
                    continuation > second_maximum)
                    return false;
            }
            else if (continuation < 0x80 || continuation > 0xbf)
            {
                return false;
            }
        }
        return true;
    }

    bool parse_string() noexcept
    {
        if (!consume('"'))
            return false;
        while (cursor_ != end_)
        {
            const auto value = *cursor_;
            if (value == '"')
            {
                ++cursor_;
                return true;
            }
            if (value == '\\')
            {
                ++cursor_;
                if (cursor_ == end_)
                    return false;
                const auto escape = *cursor_++;
                if (escape == 'u')
                {
                    if (!parse_unicode_escape())
                        return false;
                }
                else if (escape != '"' && escape != '\\' && escape != '/' &&
                         escape != 'b' && escape != 'f' && escape != 'n' &&
                         escape != 'r' && escape != 't')
                {
                    return false;
                }
                continue;
            }
            if (value < 0x20)
                return false;
            if (value < 0x80)
            {
                ++cursor_;
                continue;
            }
            if (!parse_utf8())
                return false;
        }
        return false;
    }

    bool parse_digits() noexcept
    {
        const auto *const begin = cursor_;
        while (cursor_ != end_ && *cursor_ >= '0' && *cursor_ <= '9')
            ++cursor_;
        return cursor_ != begin;
    }

    bool parse_number() noexcept
    {
        consume('-');
        if (cursor_ == end_)
            return false;
        if (*cursor_ == '0')
        {
            ++cursor_;
            if (cursor_ != end_ && *cursor_ >= '0' && *cursor_ <= '9')
                return false;
        }
        else if (*cursor_ >= '1' && *cursor_ <= '9')
        {
            if (!parse_digits())
                return false;
        }
        else
        {
            return false;
        }

        if (cursor_ != end_ && *cursor_ == '.')
        {
            ++cursor_;
            if (!parse_digits())
                return false;
        }
        if (cursor_ != end_ && (*cursor_ == 'e' || *cursor_ == 'E'))
        {
            ++cursor_;
            if (cursor_ != end_ && (*cursor_ == '+' || *cursor_ == '-'))
                ++cursor_;
            if (!parse_digits())
                return false;
        }
        return true;
    }

    const std::uint8_t *cursor_;
    const std::uint8_t *const end_;
};

bool valid_json_record(const std::uint8_t *begin,
                       const std::uint8_t *end) noexcept
{
    return JsonSyntaxParser(begin, end).parse();
}

} // namespace

StructuredEventType structured_event_type(
    const StructuredEventPayload &payload) noexcept
{
    StructuredEventType type{};
    if (payload_type(payload, type))
        return type;
    return static_cast<StructuredEventType>(0);
}

const char *structured_event_type_name(StructuredEventType type) noexcept
{
    switch (type)
    {
        case StructuredEventType::process_started:
            return "process.started";
        case StructuredEventType::process_ready:
            return "process.ready";
        case StructuredEventType::process_stopping:
            return "process.stopping";
        case StructuredEventType::process_stopped:
            return "process.stopped";
        case StructuredEventType::process_forced_crash_requested:
            return "process.forced_crash_requested";
        case StructuredEventType::process_exited:
            return "process.exited";
        case StructuredEventType::epoch_generated:
            return "epoch.generated";
        case StructuredEventType::epoch_staged:
            return "epoch.staged";
        case StructuredEventType::epoch_acknowledged:
            return "epoch.acknowledged";
        case StructuredEventType::epoch_activation_armed:
            return "epoch.activation_armed";
        case StructuredEventType::epoch_activated:
            return "epoch.activated";
        case StructuredEventType::block_committed:
            return "block.committed";
        case StructuredEventType::adaptive_configuration_active:
            return "adaptive.configuration_active";
        case StructuredEventType::aggregation_required_set_ready:
            return "aggregation.required_set_ready";
        case StructuredEventType::aggregation_initial_reserved:
            return "aggregation.initial_reserved";
        case StructuredEventType::aggregation_initial_enqueued:
            return "aggregation.initial_enqueued";
        case StructuredEventType::aggregation_initial_committed:
            return "aggregation.initial_committed";
        case StructuredEventType::aggregation_initial_released:
            return "aggregation.initial_released";
        case StructuredEventType::aggregation_delta_reserved:
            return "aggregation.delta_reserved";
        case StructuredEventType::aggregation_delta_enqueued:
            return "aggregation.delta_enqueued";
        case StructuredEventType::aggregation_delta_committed:
            return "aggregation.delta_committed";
        case StructuredEventType::aggregation_delta_released:
            return "aggregation.delta_released";
        case StructuredEventType::aggregation_delta_rejected:
            return "aggregation.delta_rejected";
        case StructuredEventType::aggregation_required_branch_incomplete:
            return "aggregation.required_branch_incomplete";
        case StructuredEventType::aggregation_wait_exempt_absent:
            return "aggregation.wait_exempt_absent_at_observation_deadline";
        case StructuredEventType::aggregation_wait_exempt_late_accepted:
            return "aggregation.wait_exempt_late_accepted";
        case StructuredEventType::aggregation_retry_exhausted:
            return "aggregation.retry_exhausted";
        case StructuredEventType::aggregation_proposal_aborted:
            return "aggregation.proposal_aborted";
        case StructuredEventType::aggregation_root_quorum_progress:
            return "aggregation.root_quorum_progress";
        case StructuredEventType::aggregation_root_qc_published:
            return "aggregation.root_qc_published";
    }
    return "unknown";
}

struct StructuredEventSink::State final
{
    State(StructuredEventConfig value,
          StructuredEventClock &event_clock,
          StructuredEventOutput &event_output,
          StructuredEventCursor cursor)
        : config(std::move(value)),
          clock(event_clock),
          output(event_output)
    {
        if (!valid_configuration(config))
            fail(StructuredEventFailure::invalid_configuration);
        else if (!valid_identity_limits(config))
            fail(StructuredEventFailure::identity_too_large);
        else if (!valid_cursor(cursor, config))
            fail(StructuredEventFailure::invalid_configuration);
        else
        {
            status.last_assigned_sequence = cursor.last_source_sequence;
            status.has_last_monotonic_ns = cursor.has_last_monotonic_ns;
            status.last_monotonic_ns = cursor.last_monotonic_ns;
        }
    }

    void fail(StructuredEventFailure failure) noexcept
    {
        if (status.first_failure == StructuredEventFailure::none)
            status.first_failure = failure;
        status.healthy = false;
        status.stopped = true;
    }

    void fail_admission(StructuredEventFailure failure) noexcept
    {
        if (status.dropped_records !=
            std::numeric_limits<std::uint64_t>::max())
            ++status.dropped_records;
        fail(failure);
    }

    void fail_reentrant() noexcept
    {
        reentrant_call_detected = true;
        fail(StructuredEventFailure::reentrant_call);
    }

    void discard_queue() noexcept
    {
        queue.clear();
        write_offset = 0;
        status.queued_events = 0;
        status.queued_bytes = 0;
    }

    template <typename Serializer>
    void admit(bool valid_payload, Serializer &&serializer) noexcept
    {
        if (active_call)
        {
            fail_reentrant();
            return;
        }
        if (!status.healthy || closed)
            return;
        ReentrancyScope scope(active_call);

        if (!valid_payload)
        {
            fail_admission(StructuredEventFailure::invalid_payload);
            return;
        }
        if (status.last_assigned_sequence ==
            std::numeric_limits<std::uint64_t>::max())
        {
            fail_admission(StructuredEventFailure::sequence_exhausted);
            return;
        }

        const auto sequence = status.last_assigned_sequence + 1;
        const auto monotonic_ns = clock.now_ns();
        if (!status.healthy)
        {
            output_failed = true;
            discard_queue();
            return;
        }
        if (status.has_last_monotonic_ns &&
            monotonic_ns < status.last_monotonic_ns)
        {
            fail_admission(StructuredEventFailure::clock_regression);
            return;
        }

        try
        {
            auto record = serializer(sequence, monotonic_ns);
            if (record.size() > config.limits.maximum_line_bytes)
            {
                fail_admission(StructuredEventFailure::line_too_large);
                return;
            }
            if (status.queued_events >=
                    config.limits.maximum_queued_events ||
                record.size() >
                    config.limits.maximum_queued_bytes -
                        std::min(config.limits.maximum_queued_bytes,
                                 status.queued_bytes))
            {
                fail_admission(StructuredEventFailure::queue_full);
                return;
            }

            const auto record_size = record.size();
            queue.push_back(std::move(record));
            ++status.queued_events;
            status.queued_bytes += record_size;
            status.last_assigned_sequence = sequence;
            status.has_last_monotonic_ns = true;
            status.last_monotonic_ns = monotonic_ns;
        }
        catch (const LineLimitExceeded &)
        {
            fail_admission(StructuredEventFailure::line_too_large);
        }
        catch (...)
        {
            fail_admission(StructuredEventFailure::allocation_failure);
        }
    }

    StructuredEventConfig config;
    StructuredEventClock &clock;
    StructuredEventOutput &output;
    StructuredEventHealth status;
    std::deque<std::string> queue;
    std::size_t write_offset{0};
    bool active_call{false};
    bool reentrant_call_detected{false};
    bool output_failed{false};
    bool closed{false};
};

StructuredEventSink::StructuredEventSink(
    StructuredEventConfig config,
    StructuredEventClock &clock,
    StructuredEventOutput &output,
    StructuredEventCursor cursor)
    : state_(std::make_unique<State>(
          std::move(config), clock, output, cursor))
{
}

StructuredEventSink::~StructuredEventSink() noexcept
{
    shutdown();
}

void StructuredEventSink::emit(
    const StructuredEventPayload &payload) noexcept
{
    auto &state = *state_;
    StructuredEventType type{};
    const auto valid = payload_type(payload, type);
    state.admit(valid, [&state, &payload, type](
                           std::uint64_t sequence,
                           std::uint64_t monotonic_ns) {
        return serialize_event(
            state.config, payload, type, sequence, monotonic_ns);
    });
}

void StructuredEventSink::emit_adaptive(
    const AdaptiveAggregationStructuredEvent &event) noexcept
{
    auto &state = *state_;
    StructuredEventType type{};
    const auto valid = valid_adaptive_payload(event) &&
                       adaptive_payload_type(event, type);
    state.admit(valid, [&state, &event, type](
                           std::uint64_t sequence,
                           std::uint64_t monotonic_ns) {
        return serialize_adaptive_event(
            state.config, event, type, sequence, monotonic_ns);
    });
}

void StructuredEventSink::drain() noexcept
{
    auto &state = *state_;
    if (state.active_call)
    {
        state.fail_reentrant();
        return;
    }
    if (state.closed || state.output_failed)
        return;
    ReentrancyScope scope(state.active_call);

    while (!state.queue.empty())
    {
        const auto &record = state.queue.front();
        if (state.write_offset > record.size())
        {
            state.output_failed = true;
            state.fail(StructuredEventFailure::write_failure);
            state.discard_queue();
            return;
        }
        const auto remaining = record.size() - state.write_offset;
        if (remaining == 0)
        {
            state.queue.pop_front();
            state.write_offset = 0;
            if (state.status.queued_events != 0)
                --state.status.queued_events;
            if (state.status.complete_records !=
                std::numeric_limits<std::uint64_t>::max())
                ++state.status.complete_records;
            continue;
        }

        const auto *const bytes = reinterpret_cast<const std::uint8_t *>(
            record.data() + state.write_offset);
        const auto result = state.output.write_some(bytes, remaining);
        if (state.reentrant_call_detected)
        {
            state.status.interrupted_tail =
                state.write_offset != 0 || result.bytes_written != 0;
            state.output_failed = true;
            state.discard_queue();
            return;
        }
        if (result.status == StructuredEventWriteStatus::interrupted &&
            result.bytes_written == 0)
            continue;
        if (result.status != StructuredEventWriteStatus::progress ||
            result.bytes_written == 0 ||
            result.bytes_written > remaining)
        {
            state.status.interrupted_tail = state.write_offset != 0;
            state.output_failed = true;
            state.fail(StructuredEventFailure::write_failure);
            state.discard_queue();
            return;
        }

        state.write_offset += result.bytes_written;
        state.status.queued_bytes -= result.bytes_written;
        if (state.write_offset == record.size())
        {
            state.queue.pop_front();
            state.write_offset = 0;
            --state.status.queued_events;
            if (state.status.complete_records !=
                std::numeric_limits<std::uint64_t>::max())
                ++state.status.complete_records;
        }
    }
}

void StructuredEventSink::shutdown() noexcept
{
    auto &state = *state_;
    if (state.active_call)
    {
        state.fail_reentrant();
        return;
    }
    if (state.closed)
        return;
    drain();
    state.closed = true;
    state.status.stopped = true;
    ReentrancyScope scope(state.active_call);
    const bool closed_cleanly = state.output.close();
    if (!closed_cleanly)
        state.fail(StructuredEventFailure::close_failure);
}

StructuredEventHealth StructuredEventSink::health() const noexcept
{
    auto &state = *state_;
    if (state.active_call)
        state.fail_reentrant();
    return state.status;
}

StructuredEventPrefixResult parse_structured_event_prefix(
    const bytearray_t &bytes) noexcept
{
    StructuredEventPrefixResult result;
    std::size_t record_begin = 0;
    try
    {
        for (std::size_t index = 0; index < bytes.size(); ++index)
        {
            if (bytes[index] != static_cast<std::uint8_t>('\n'))
                continue;
            if (!valid_json_record(
                    bytes.data() + record_begin, bytes.data() + index))
            {
                result.status = StructuredEventPrefixStatus::malformed_record;
                return result;
            }
            ++result.complete_records;
            result.complete_bytes = index + 1;
            record_begin = index + 1;
        }
        if (record_begin != bytes.size())
            result.status = StructuredEventPrefixStatus::interrupted_tail;
    }
    catch (...)
    {
        result.status = StructuredEventPrefixStatus::allocation_failure;
    }
    return result;
}

} // namespace hotstuff
