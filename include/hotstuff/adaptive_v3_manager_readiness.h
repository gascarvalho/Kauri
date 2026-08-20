#ifndef HOTSTUFF_ADAPTIVE_V3_MANAGER_READINESS_H_INCLUDED
#define HOTSTUFF_ADAPTIVE_V3_MANAGER_READINESS_H_INCLUDED

#include <cstddef>
#include <cstdint>
#include <memory>
#include <optional>
#include <utility>
#include <vector>

#include "hotstuff/activation_readiness_wire.h"
#include "hotstuff/epoch_change_bundle.h"

namespace hotstuff {

/** Immutable, non-dynamic portion of a v3 readiness identity.  Command
 * commit/boundary facts are deliberately absent: only signed observations
 * can establish those values. */
struct AdaptiveV3TransitionProjection {
    ConfigurationId predecessor_configuration;
    std::uint64_t predecessor_generation{0};
    ConfigurationId successor_configuration;
    std::uint64_t successor_generation{0};
    uint256_t membership_digest;
    uint256_t command_payload_digest;
    std::uint64_t activation_delay_blocks{0};
    uint256_t canonical_bundle_digest;
    std::uint64_t cycle_ordinal{0};

    bool matches_static(const ActivationReadyIdentityV1 &identity) const noexcept;
};

std::optional<AdaptiveV3TransitionProjection>
make_adaptive_v3_transition_projection(
    const AdaptiveV3EpochChangeBundle &bundle,
    const ConfigurationId &current_predecessor_configuration,
    std::uint64_t current_predecessor_generation,
    std::uint64_t cycle_ordinal,
    const std::vector<std::pair<ReplicaID, PubKeyBLS>> &readiness_membership) noexcept;

struct AdaptiveV3ManagerReadinessConfig {
    std::vector<std::pair<ReplicaID, PubKeyBLS>> membership;
    std::size_t required_release_count{0};
    std::optional<AdaptiveV3TransitionProjection> projection;
};

enum class AdaptiveV3ManagerReadinessDisposition : std::uint8_t {
    accepted = 1, duplicate, quarantined, rejected_peer_binding,
    rejected_nonmember, rejected_invalid_observation, rejected_wrong_identity,
    rejected_conflict, released,
};

/** Manager-only availability collector. R controls release only; the W1
 * verifier remains the sole owner of Q derivation. Calls are single-writer. */
class AdaptiveV3ManagerReadinessCollector final {
public:
    AdaptiveV3ManagerReadinessCollector(ActivationReadyIdentityV1 expected,
                                        AdaptiveV3ManagerReadinessConfig config);
    /** Projection-only construction deliberately has no dynamic identity.
     * A complete signed candidate is selected only when Q observations agree. */
    AdaptiveV3ManagerReadinessCollector(
        AdaptiveV3TransitionProjection projection,
        AdaptiveV3ManagerReadinessConfig config);
    ~AdaptiveV3ManagerReadinessCollector();
    AdaptiveV3ManagerReadinessCollector(const AdaptiveV3ManagerReadinessCollector &) = delete;
    AdaptiveV3ManagerReadinessCollector &operator=(const AdaptiveV3ManagerReadinessCollector &) = delete;
    AdaptiveV3ManagerReadinessDisposition ingest(ReplicaID tls_peer,
                                                   const ActivationReadyObservationV1 &observation) noexcept;
    const ActivationReadinessCertificateV1 *certificate() const noexcept;
    bool released() const noexcept;
    std::size_t accepted_count() const noexcept;
    bool quarantined(ReplicaID source) const noexcept;
private:
    struct State; std::unique_ptr<State> state_;
};

} // namespace hotstuff
#endif
