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

#include "hotstuff/client.h"

namespace hotstuff
{

    const opcode_t MsgLatencyReport::opcode;
    const opcode_t MsgTimeoutReport::opcode;
    const opcode_t MsgDeployEpoch::opcode;
    const opcode_t MsgDeployEpochReputation::opcode;

    // const opcode_t MsgReqCmd::opcode;
    // const opcode_t MsgRespCmd::opcode;
    // #ifdef HOTSTUFF_AUTOCLI
    // const opcode_t MsgDemandCmd::opcode;
    // #endif

}
