#include "catch.hpp"

#include "hotstuff/client.h"

namespace
{

const hotstuff::opcode_t *const request_opcode =
    &hotstuff::MsgReqCmd::opcode;
const hotstuff::opcode_t *const response_opcode =
    &hotstuff::MsgRespCmd::opcode;

} // namespace

TEST_CASE("client request and response opcodes have linkable storage",
          "[client][opcode]")
{
    REQUIRE(*request_opcode == 0x4);
    REQUIRE(*response_opcode == 0x5);
    REQUIRE(*request_opcode != *response_opcode);
    REQUIRE(request_opcode != response_opcode);
}
