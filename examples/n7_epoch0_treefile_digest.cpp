// Read-only preflight for the experimental N=7 file-bootstrap Epoch-0.
// It deliberately shares the manager's parser and never falls back to the
// cyclic schedule when a file is supplied.

#include <exception>
#include <iostream>
#include <string>
#include <vector>

#include "hotstuff/configuration.h"

int main(int argc, char **argv)
{
    if (argc != 2)
    {
        std::cerr << "usage: n7-epoch0-treefile-digest <epoch0.tree>\n";
        return 2;
    }

    try
    {
        const std::vector<hotstuff::ReplicaID> membership{
            0, 1, 2, 3, 4, 5, 6};
        const auto trees = hotstuff::parse_adaptive_v2_epoch_zero_tree_file(
            argv[1], membership);
        const auto epoch = hotstuff::adaptive_v2_epoch_zero_input(
            membership, trees);
        std::cout << hotstuff::compute_epoch_digest(epoch).to_hex() << '\n';
        return 0;
    }
    catch (const std::exception &error)
    {
        std::cerr << "n7-epoch0-treefile-digest: " << error.what() << '\n';
        return 2;
    }
}
