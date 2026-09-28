#include <fstream>
#include <filesystem>
#include <iostream>
#include <iterator>
#include <string>
#include <set>

#include "hotstuff/operator_capacity_policy.h"

int main(int argc, char **argv)
{
    if (argc == 2 &&
        (std::string(argv[1]) == "--emit-test-fixture" ||
         std::string(argv[1]) == "--emit-test-fixture-one-label" ||
         std::string(argv[1]) == "--emit-test-fixture-swapped-labels"))
    {
        try
        {
            hotstuff::OperatorCapacitySnapshot snapshot;
            snapshot.issuer_reference = "native-fixture";
            snapshot.valid_from_monotonic_ns = 100;
            snapshot.valid_until_monotonic_ns = 200;
            const bool one_label =
                std::string(argv[1]) == "--emit-test-fixture-one-label";
            const hotstuff::ReplicaID label_count = one_label ? 1 : 31;
            for (hotstuff::ReplicaID replica = 0; replica < label_count; ++replica)
                snapshot.labels.push_back({
                    replica, replica < 6 ? hotstuff::OperatorCapacityClass::slow :
                                           hotstuff::OperatorCapacityClass::fast});
            if (std::string(argv[1]) == "--emit-test-fixture-swapped-labels")
            {
                snapshot.labels[0].capacity = hotstuff::OperatorCapacityClass::fast;
                snapshot.labels[6].capacity = hotstuff::OperatorCapacityClass::slow;
            }
            snapshot.canonical_digest =
                hotstuff::operator_capacity_snapshot_digest(snapshot);
            constexpr hotstuff::OperatorCapacitySnapshotWireLimits limits{16 * 1024};
            const auto wire = hotstuff::encode_operator_capacity_snapshot(snapshot, limits);
            std::cout.write(reinterpret_cast<const char *>(wire.data()), wire.size());
            return std::cout ? 0 : 1;
        }
        catch (...) { return 1; }
    }
    const bool validate_n31 = argc == 4 &&
        std::string(argv[1]) == "--validate-n31";
    if (argc != 2 && !validate_n31)
    {
        std::cerr << "usage: operator-capacity-snapshot-digest "
                     "[--validate-n31 EPOCH0-DIGEST] SNAPSHOT-WIRE\n";
        return 2;
    }
    try
    {
        const char *wire_path = argv[validate_n31 ? 3 : 1];
        const auto status = std::filesystem::status(wire_path);
        if (!std::filesystem::is_regular_file(status) ||
            std::filesystem::file_size(wire_path) > 16 * 1024)
            return 2;
        std::ifstream input(wire_path, std::ios::binary);
        if (!input) return 2;
        bytearray_t wire{
            std::istreambuf_iterator<char>(input), std::istreambuf_iterator<char>()};
        constexpr hotstuff::OperatorCapacitySnapshotWireLimits limits{16 * 1024};
        const auto decoded = hotstuff::decode_operator_capacity_snapshot(wire, limits);
        if (!decoded)
            return 1;
        if (validate_n31)
        {
            const auto &snapshot = *decoded.value;
            std::set<hotstuff::ReplicaID> members;
            std::size_t slow_count = 0;
            std::size_t fast_count = 0;
            for (const auto &label : snapshot.labels)
            {
                members.insert(label.replica_id);
                const auto expected = label.replica_id < 6 ?
                    hotstuff::OperatorCapacityClass::slow :
                    hotstuff::OperatorCapacityClass::fast;
                if (label.capacity != expected)
                    return 1;
                if (label.capacity == hotstuff::OperatorCapacityClass::slow)
                    ++slow_count;
                else if (label.capacity == hotstuff::OperatorCapacityClass::fast)
                    ++fast_count;
                else
                    return 1;
            }
            if (snapshot.predecessor.epoch_number != 0 ||
                snapshot.predecessor.epoch_digest.to_hex() != argv[2] ||
                snapshot.valid_until_monotonic_ns <=
                    snapshot.valid_from_monotonic_ns ||
                snapshot.issuer_reference.empty() ||
                snapshot.labels.size() != 31 || members.size() != 31 ||
                slow_count != 6 || fast_count != 25)
                return 1;
            for (hotstuff::ReplicaID replica = 0; replica < 31; ++replica)
                if (members.count(replica) == 0)
                    return 1;
        }
        std::cout << decoded.value->canonical_digest.to_hex() << '\n';
        return 0;
    }
    catch (...) { return 1; }
}
