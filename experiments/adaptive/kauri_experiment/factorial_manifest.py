"""Frozen planning/preflight contract for the SHAPE40 factorial."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import asdict, dataclass
import hashlib
import itertools
import json
import math
from pathlib import Path
from typing import Any, Callable

LEGACY_MANIFEST_ID = "shape-placement-factorial-v1"
LEGACY_MANIFEST_SHA256 = (
    "58ebd0cc8ceb5ae4dc5981e9475747ea4b2a5b63fe334bfa6a44f552b8495542"
)
LEGACY_SEMANTIC_SHA256 = (
    "3ca6665ec108c8547b8d483ca509a25ae93be037595e9059276422c28cf77687"
)
LEGACY_PLAN_SHA256 = "e5e1acf5228446795810e8f2ad0ef2f9af25203a2a213a2a427b218f19907fb1"

V2_MANIFEST_ID = "shape-placement-factorial-v2"
V2_MANIFEST_SHA256 = (
    "ef1133f2d3b5204bdb8fd4be5ebd4990801b77e2aafa3b6a03cc5c262a40a8fd"
)
V2_SEMANTIC_SHA256 = (
    "5d10ff2498fb265b19c3990df87e06db212687d9a55585b37545b4dbeb2f96c9"
)
V2_PLAN_SHA256 = "b17fb3fa654d44080ad52e01b24aeed8c38726e33149229d0f6e540dc7f97516"

V3_MANIFEST_ID = "shape-placement-factorial-v3"
V3_MANIFEST_SHA256 = (
    "5f741a08c104801d83e8d25668fd74a34ebb41f99421cba736843bae89b1ea01"
)
V3_SEMANTIC_SHA256 = (
    "63862ce82ddb651e35dffb20d81bdfa5af8e5803b169e3c27847ba3e0b96963c"
)
V3_PLAN_SHA256 = "18d407a1b6c4a49a2246706c758a8b2ac152555cfcc11e09e1c3c07dbd979636"

V4_MANIFEST_ID = "shape-placement-factorial-v4"
V4_MANIFEST_SHA256 = (
    "9c4585b44c77a945ecf759c452ebd584eaa02243c86970b02d49eb781ae7cf01"
)
V4_SEMANTIC_SHA256 = (
    "237939ab2e790561971106b54a97a026368028159cb6d2aaca6cd6b1305e56e2"
)
V4_PLAN_SHA256 = "332a9e3d02a72403cd7013e76d44b47d39009c01f33ed4d0ef41fb6a7e17e3ba"

V5_MANIFEST_ID = "shape-placement-factorial-v5"
V5_MANIFEST_SHA256 = (
    "20e7ef84aba690c5bc42acb470877db8e1278939c8be762a42a1f1aa0feb0e12"
)
V5_SEMANTIC_SHA256 = (
    "89cc98e6c0159454eb006b14dfc4718ae6861a5e9658a30d8bb7ff5c2538a161"
)
V5_PLAN_SHA256 = "5566cf5262fa1b41cf60bc8328e37cdf9439cc60a3d90c03140d0ebec31f040f"

V6_MANIFEST_ID = "shape-placement-factorial-v6"
V6_MANIFEST_SHA256 = (
    "a513f5de8e04ea1a677e365dffcb086a27299bd7179c231d98d52fa104a059d3"
)
V6_SEMANTIC_SHA256 = (
    "bfb6f4d3f6e88fb5f58cd37fde7db4f67e2927ed81d0bcfb7059e59aa5c4f66f"
)
V6_PLAN_SHA256 = "674fdac9511bac8362bce0aa3dd939a4a65117f35b3652a8cd6c153a8a7fdc57"

V7_MANIFEST_ID = "shape-placement-factorial-v7"
V7_MANIFEST_SHA256 = (
    "3160855b1c023269c775ecf5b8d11b6cd19931dd686ff6875ea9c305b1744a18"
)
V7_SEMANTIC_SHA256 = (
    "e6a8ec618e7b5dddbd01f86846ea0b13cc4ee5acfc82436d63572410d7170b12"
)
V7_PLAN_SHA256 = "18469f3b00f3dfb9b92c586ec46cce1f1b76b49faf506840eb30bd769e5f7026"

V8_MANIFEST_ID = "shape-placement-factorial-v8"
V8_MANIFEST_SHA256 = (
    "05d8f3bbf38b2da8000d4a7788b0376803c71feaf8bb44e52e19c8e248757dee"
)
V8_SEMANTIC_SHA256 = (
    "af47ea9d35c1db786e5609b53bdffc93f18b47db40db948908edb611c92f76a3"
)
V8_PLAN_SHA256 = "0f1d1c321109f795c03d658208d340fff5b38da7d74986996ed52bd7828a8598"

V9_MANIFEST_ID = "shape-placement-factorial-v9"
V9_MANIFEST_SHA256 = (
    "878394cf2fc9bbb283daa2667d9c4312ede2285522452079b7289bbe005b6c02"
)
V9_SEMANTIC_SHA256 = (
    "f72e3ece6b73490a372c03479e6456976134dd4f8c8662d32e841d5920e5aaac"
)
V9_PLAN_SHA256 = "36ba999a4e585ee5203f1a0acbdecc728815ad1a44bf5d43b11f10a7df57c2f6"

V10_MANIFEST_ID = "shape-placement-factorial-v10"
V10_MANIFEST_SHA256 = (
    "3d661c652133be1873c203e85d6dc6cde2279c6877f6f74544696aa22d300856"
)
V10_SEMANTIC_SHA256 = (
    "0c7694677823b65fbd6369c729aca3bc39e3997f2fed5f7a74af4a67a620c91e"
)
V10_PLAN_SHA256 = "38b1881e3841f7aad76d5c3a0097d99b9e4c96326dda7a4046cb1b13bd5c4d2c"

V11_MANIFEST_ID = "shape-placement-factorial-v11"
V11_MANIFEST_SHA256 = (
    "ddb7dc1c18af85028242754114a8f04222087088d0bb33adad6def5e18028993"
)
V11_SEMANTIC_SHA256 = (
    "5877e24974c7d626340ac4729dd5917dafe5c119d0f0cc11aba18149fd181c6b"
)
V11_PLAN_SHA256 = "ce0d379f2b8795b0938d1185c604c182c848cbf71c4e0c582c73ad3f8a2a37c0"

V12_MANIFEST_ID = "shape-placement-factorial-v12"
V12_MANIFEST_SHA256 = (
    "48cf75f68691804c96cc3a5f35e5430b5b57aa62c6178dbc45e3959f60cd8c6a"
)
V12_SEMANTIC_SHA256 = (
    "accc776a797283b89847ca3a278a72a1080d76719584795f474e2ea0268b943c"
)
V12_PLAN_SHA256 = "1025a7eca7c41e0c5619e9ad357c07709b0ffe66addbc14ba3cd2592f121d298"

V13_MANIFEST_ID = "shape-placement-factorial-v13"
V13_MANIFEST_SHA256 = (
    "546ce4a3bfecfe62678926f8a0d71cc8ce7db771ed5dd347ef42d15ba39af36b"
)
V13_SEMANTIC_SHA256 = (
    "3676585af0ff9afe4e36a66e692ab8b604495188fa40afaaff75e5c574b3c629"
)
V13_PLAN_SHA256 = "bbca567114a12034fd41318c856a15eda66bf2cf2efe076398978ca1164bc7ed"

V14_MANIFEST_ID = "shape-placement-factorial-v14"
V14_MANIFEST_SHA256 = (
    "f2e03faf00749f8098c9e6e136b5d82a657d8239858ff3571b636ef781bedc08"
)
V14_SEMANTIC_SHA256 = (
    "9611a2b61343c73fc99fea12d8aca097369328301f9a0b92b23a93eb619b9de1"
)
V14_PLAN_SHA256 = "674ee1227c70f9aa3d08fe060583adf7ae47fd167d5ba7decaa14e5edd5c4653"

V15_MANIFEST_ID = "shape-placement-factorial-v15"
V15_MANIFEST_SHA256 = (
    "394c37abefe95fb03a3d68cc09fdfc2634e7850d5b3e7da3f108a854256db6cd"
)
V15_SEMANTIC_SHA256 = (
    "1b5145a1edf9021578f1a5a1b162f98ca7f101e065c559d6e356b20ad7adc8a0"
)
V15_PLAN_SHA256 = "584e0bb644f5290bd3175109a441e354cf0a388f223611b11371f8111ea5dadc"

V16_MANIFEST_ID = "shape-placement-factorial-v16"
V16_MANIFEST_SHA256 = (
    "4f7db572c3d2577853185ecdbbd13143b48792764758f16c2d6048a5e754e033"
)
V16_SEMANTIC_SHA256 = (
    "52f8cd5918c5c0e73a2134ab565101374098d77ed6b23d966d9dc1b7b1ae10ec"
)
V16_PLAN_SHA256 = "a5c18c24e25f61177b714708dfdf41de6273076f731613642b0f7f8513c59851"

V17_MANIFEST_ID = "shape-placement-factorial-v17"
V17_MANIFEST_SHA256 = (
    "2683754d39fc0107d45a80eff606284adc5cf74cb325c8ff0813790d80c4aae2"
)
V17_SEMANTIC_SHA256 = (
    "1e6aaa830d47a9a92c9c98d008ee1a3eaf58b7797d0e7292f759cdfee7b71ad1"
)
V17_PLAN_SHA256 = "9dfcf4753febfd519ad7927e54e49fbf9bb53d60c5a0765bf779eb9c15280d89"

V18_MANIFEST_ID = "shape-placement-factorial-v18"
V18_MANIFEST_SHA256 = (
    "dd9bc66ea3d0e17f293921bc3a2ffa2d742c319ae8897e5d0f2d48026ff6ec94"
)
V18_SEMANTIC_SHA256 = (
    "7d45aaa6282e072a4693ad25f1d6806f4e79ebf2727e7f91eca9b568a969d878"
)
V18_PLAN_SHA256 = "68046b493221692a7c1835f4e31bef21c86f1b6eca85afdbe7bddb011dcdc32f"

V19_MANIFEST_ID = "shape-placement-factorial-v19"
V19_MANIFEST_SHA256 = (
    "63fd0ef4b36a026aeaf2158dcac52482e2a61fffbba7679c6722db359b9c518a"
)
V19_SEMANTIC_SHA256 = (
    "6ca7779a897aab370cebacee3ce464fa7c738a60b28f880e46a4dce6b72ca1ca"
)
V19_PLAN_SHA256 = "4be51a1f09dac3044ba6eb150b27a91d5c3db9e21be95b29a23488b92350d0d8"

V20_MANIFEST_ID = "shape-placement-factorial-v20"
V20_MANIFEST_SHA256 = (
    "3f28dd15f9774fe616c7f473da3a1305b6d0647ddf43c0363f2f096d231f9114"
)
V20_SEMANTIC_SHA256 = (
    "572d333bffd9fc66499d87b5791dc1347adb941a52e81900be2b769232ccc86d"
)
V20_PLAN_SHA256 = "78c21a436a1680ecc5c3a253cd7d214cfaef88dbdf94cf90c05110975a6017ef"

V21_MANIFEST_ID = "shape-placement-factorial-v21"
V21_MANIFEST_SHA256 = (
    "81e1f15b630f6e877a92131bdc456047de516de9c98827b74f8d08a0ffeee853"
)
V21_SEMANTIC_SHA256 = (
    "84f279aaba107f1d54db0143b1a9e39ebd7b292d6b652fc67ab2c8e583cb0296"
)
V21_PLAN_SHA256 = "287c333eae3c1bcc1ed62f6db61745f36871098645c1f2e93147fa46f3960c9b"

V22_MANIFEST_ID = "shape-placement-factorial-v22"
V22_MANIFEST_SHA256 = (
    "61b28d7a3f24b2472967499a632098fff93c7b57ed75fcb1db759cfb0bab82c4"
)
V22_SEMANTIC_SHA256 = (
    "8719011642f91e891239e5cd43af1b580fd2c9caa58ed01c46e9a7293bc1d292"
)
V22_PLAN_SHA256 = "2234fd682335553281351c2fdda022851d7c5d94b0e68e115a993bc80c3cec9c"

V23_MANIFEST_ID = "shape-placement-factorial-v23"
V23_MANIFEST_SHA256 = (
    "6d174528b8d703f7d835e8e2322bc96b8320e574bc68209378bff9789eacf881"
)
V23_SEMANTIC_SHA256 = (
    "f810691049c559101c1545e739751e19cd116829b3e9d366b9cdc366a107645d"
)
V23_PLAN_SHA256 = "45fad5271ddc39bf66a8ece24d1045241f41e2b03c0731d1e386890586fe561d"

V24_MANIFEST_ID = "shape-placement-factorial-v24"
V24_MANIFEST_SHA256 = (
    "3ed23783d141d2e59f571ca6278082d644bb5e02430f7cffbb5c2e1ee3c246ff"
)
V24_SEMANTIC_SHA256 = (
    "9a8228cf1cf04149763cf46c475d0b91d452adbb7bd5bb239449e1b41526d4ae"
)
V24_PLAN_SHA256 = "730758eec13959160e1f5a2b6678888f45d58f36b570f40587bad1f40f0c051a"

V25_MANIFEST_ID = "shape-placement-factorial-v25"
V25_MANIFEST_SHA256 = (
    "43d07e83f88059e73143dad564b3c287e809750e7d76f96f1aa284eece893557"
)
V25_SEMANTIC_SHA256 = (
    "0a7eb5f0634f163dca8984dcd63a0747f69feb20c7bf298eb433ec6505e9dd41"
)
V25_PLAN_SHA256 = (
    "e8893986bb8d10840cf7e7c798425128347eba9eddd9845cab79fafde59c110a"
)

V26_MANIFEST_ID = "shape-placement-factorial-v26"
V26_MANIFEST_SHA256 = (
    "ecae475a18707ec479c0bebcbf6891ab7df024352744c540fe2e6b42030fcdb5"
)
V26_SEMANTIC_SHA256 = (
    "8dc7e8138ff8ba5b31c24468d66d75dcd5208852b5dd05cd69caf41e6a68eddc"
)
V26_PLAN_SHA256 = (
    "38f00662630843697e8c9f1636988fd0084a1ba580040049989bfdfb6f70d646"
)

V27_MANIFEST_ID = "shape-placement-factorial-v27"
V27_MANIFEST_SHA256 = (
    "1ef65e96e2a355ed55c754af40777ab3ee872c7c68744dc0ccac7e0b1d7e20f8"
)
V27_SEMANTIC_SHA256 = (
    "f7c7a6577090deaf53641e564ec1eee26fcad7e5710d59f3efeb2b0ae7adf385"
)
V27_PLAN_SHA256 = (
    "df296fb07159f0ae4faa106025f39a93a32d90f432c1e5792eccca6e082ad96d"
)

V28_MANIFEST_ID = "shape-placement-factorial-v28"
V28_MANIFEST_SHA256 = (
    "fda2a5e79ebd04e67d9e7db10675b5c31b6072803d986dad229ef6b115e0a659"
)
V28_SEMANTIC_SHA256 = (
    "42ca63b1fb6d256852cd0f758fbf852b01b31c594705c0bb051b6bc18ca1f989"
)
V28_PLAN_SHA256 = (
    "1b038d38cf9879b32581068e266c521fd5368aa5e57d50e21f979c25b2148f76"
)

V29_MANIFEST_ID = "shape-placement-factorial-v29"
V29_MANIFEST_SHA256 = (
    "e012be15b6193263138de7b5aee5f78eb67de10182bc07e4494373d648fde5fa"
)
V29_SEMANTIC_SHA256 = (
    "605992c544a12c3652f1279bb960325bc94fe7cd72ebb251d1c5b8e101701361"
)
V29_PLAN_SHA256 = (
    "8ca31c9d0ae26c7deedbb1932a161652f146e2ee6cb54bf08e9bf4ae5088bc91"
)

V30_MANIFEST_ID = "shape-placement-factorial-v30"
V30_MANIFEST_SHA256 = (
    "c868d9ebce0f3afbdfd6011787f385395b6df43b633f6cfad284726c2d3f52e4"
)
V30_SEMANTIC_SHA256 = (
    "4d3130b1a02c9a0f4c67f8a29fb22000e15c2d9bf64ebd87af32518f22fb15b2"
)
V30_PLAN_SHA256 = (
    "3ed7a8a9b80f18c4b48d2a3daf16c44fba458f47ae84c6e4a4bdf907b4d65e1f"
)

V31_MANIFEST_ID = "shape-placement-factorial-v31"
V31_MANIFEST_SHA256 = (
    "fce9417a0f50b5a741069730d479dcb58d9502c86d3bc53a51bb06def07592c7"
)
V31_SEMANTIC_SHA256 = (
    "d09a082b895e88360b236b49998d8671440189dedbacbe1bfdd9d2ae5090a95c"
)
V31_PLAN_SHA256 = (
    "f589888a4456910f3188ee602565e990ea7c4fb45dedd798d8804b679b1591fa"
)

V32_MANIFEST_ID = "shape-placement-factorial-v32"
V32_MANIFEST_SHA256 = (
    "cbc03d8c58b8192b70b5c0f8c0a504dc07076f8e78895a3a2c802b98658d691d"
)
V32_SEMANTIC_SHA256 = (
    "eb44b9c5c229db10fc967b7d3834029f8780c48f6ae4740b213a692c9af8cb42"
)
V32_PLAN_SHA256 = (
    "3325d3d1b0b1bf2569686db9d28e3cc8d6cd6e9b6d6fc4ddf97e48b56c1bc2fa"
)

V33_MANIFEST_ID = "shape-placement-factorial-v33"
V33_MANIFEST_SHA256 = (
    "aa109a401ef88f7d135ff7f590ae1f372329ed5165c223ce0e3bf44536cf61fc"
)
V33_SEMANTIC_SHA256 = (
    "8faee7b8cf1148a7acec48b75f02ca3f5b864ee6981555ae78624664e246509f"
)
V33_PLAN_SHA256 = (
    "0897fc233ae6fc25898ee576066403f122ad65416322dec2d4e99e03503fd81c"
)

V34_MANIFEST_ID = "shape-placement-factorial-v34"
V34_MANIFEST_SHA256 = (
    "1b0c22f5f517487c507c7699198f597093dbc879994925ddbb8220c67decedb0"
)
V34_SEMANTIC_SHA256 = (
    "e963372c05c51367c6a2d54ff2f1065b1bfb136b4e0857f2dbb69793078fc0a4"
)
V34_PLAN_SHA256 = (
    "6e48cd511a0788ea1e2e5b42fb9b09e47a4c269d40fe18779cb559990b536e92"
)

V35_MANIFEST_ID = "shape-placement-factorial-v35"
V35_MANIFEST_SHA256 = (
    "28303487578594d7eac64aa1eda11ea891a85b8a6d6ff386d42e920dc8b82b95"
)
V35_SEMANTIC_SHA256 = (
    "40422f2b8ac74e695fdfa18b687fce2b31b516f36bd2b6bfe51f53602bd4d0c8"
)
V35_PLAN_SHA256 = (
    "6c558653d0db5e5f646de58c1a853d656fedbd7e61257e13755ef01ea9d42025"
)

V36_MANIFEST_ID = "shape-placement-factorial-v36"
V36_MANIFEST_SHA256 = (
    "50761ebcd8693c33ca30257b3abee6f44f992481b6f10e57732d098b029073d1"
)
V36_SEMANTIC_SHA256 = (
    "87cd28e7df12aeb9f54386623b207526ea71d096d3699d687dc07784219d63ed"
)
V36_PLAN_SHA256 = (
    "d5075db22099788a1c687ddc72cc4953a2d665fc1fa09104ba69ab128f91a65f"
)

V37_MANIFEST_ID = "shape-placement-factorial-v37"
V37_MANIFEST_SHA256 = (
    "a926192d3c6a5129ea8304504317f921a8a1a681b489e1503e715dcbd3e8be11"
)
V37_SEMANTIC_SHA256 = (
    "63efea700bd3a0fa3d59b7876602e3a5f99a3d71edb4e57ed218a8e6ea4bec8c"
)
V37_PLAN_SHA256 = (
    "e413ff98733b5e462b058018ce25ca4c77013a760fffe730dcbfd50150379e36"
)

V38_MANIFEST_ID = "shape-placement-factorial-v38"
V38_MANIFEST_SHA256 = (
    "aec2c4f2a9cb53e7b3d8d212bc0b56c008679aa97ebba140db7bac9404415698"
)
V38_SEMANTIC_SHA256 = (
    "9af592b436d934d13b1243439b84f78e1a78c19b035b37dbfe73bc168926b277"
)
V38_PLAN_SHA256 = (
    "7e731a7f36a49a5e49aa62601165bd1fe8c846e3eff20001d0fccfe37b6050e0"
)

V39_MANIFEST_ID = "shape-placement-factorial-v39"
V39_MANIFEST_SHA256 = (
    "ce6fb4c999275b575f1cf522524a5f3b41d109a6dcb3a1b77e789946b67b042f"
)
V39_SEMANTIC_SHA256 = (
    "14a3c3b910c89368481780d176e031e3bffeb15731b3448359fd73b10aee0a5a"
)
V39_PLAN_SHA256 = (
    "481b9df491a68355eade98bf241e9b2805430276681c148cf2178ad5e3c794a0"
)

FROZEN_MANIFEST_ID = "shape-placement-factorial-v40"
FROZEN_MANIFEST_SHA256 = (
    "a058ad5ac30aebbf866ba96c3ca60411b831875c36e198d6f6a4de08fbfa476d"
)
FROZEN_SEMANTIC_SHA256 = (
    "6669bb5c4165eb83e26771348cb2ab06723782a7b3a111342ed56f9740746e5a"
)
FROZEN_PLAN_SHA256 = (
    "561f6f74fd27e165eed38d24e1aeca6c6fd4acc3f93743a7b2387e8e8da2e8d7"
)

RESPONSIVE_PENDING_ATTEMPT_RETENTION_V1 = (
    "retain_unanswered_exact_parent_child_attempt_across_consensus_commit_until_"
    "original_aggregation_derived_deadline_observational_only_no_consensus_"
    "authority_v1"
)
RESPONSIVE_CAUSAL_TIMEOUT_LINKAGE_V1 = (
    "fault_marker_to_exact_parent_attempt_to_scored_timeout_required_v1"
)
RESPONSIVE_CAUSAL_TIMEOUT_PROVENANCE_WINDOW_V1 = (
    "epoch1_manager_ingestion_sequence_full_prefix_zero_exclusive_current_"
    "inclusive_v1"
)
RESPONSIVE_CAUSAL_INTERNAL_WITNESS_CANDIDATES_V1 = (
    "actor_level_strict_reporter_local_epoch1_internal_omit_aggregate_cross_"
    "commit_v1"
)
RESPONSIVE_CAUSAL_SELECTION_LINKAGE_WINDOW_V1 = (
    "epoch1_manager_ingestion_sequence_baseline_exclusive_current_inclusive_v1"
)
RESPONSIVE_MARKER_COMPLETENESS_WITNESS_V1 = (
    "exact_proposal_root_local_aggregation_activity_in_frozen_interior_v1"
)
RESPONSIVE_MARKER_COMPLETENESS_WITNESS_V2 = (
    "source_bound_fault_contribution_opportunity_to_kauri_fault_exact_"
    "bijection_with_per_phase_actor_nonvacuity_v2"
)
RESPONSIVE_CAUSAL_TIMEOUT_ELIGIBILITY_V1 = (
    "exact_parent_attempt_absolute_deadline_strictly_before_selecting_transition_v1"
)
RESPONSIVE_CAUSAL_TIMEOUT_ELIGIBILITY_V2 = (
    "selection_visible_exact_outstanding_timeout_witnesses_with_internal_and_f_plus_"
    "one_actor_gates_immature_tail_nonwitness_v2"
)
RESPONSIVE_CAUSAL_TIMEOUT_ELIGIBILITY_V3 = (
    "selection_visible_exact_outstanding_timeout_witnesses_with_internal_and_f_plus_"
    "one_actor_gates_hard_and_responsive_degraded_absent_prefix_timeout_nonwitness_"
    "present_prefix_mismatch_fatal_v3"
)
PRECONTAINMENT_FAULT_COVERAGE_GATE_V1 = (
    "all_exact_predecessor_tree_ids_have_accepted_on_time_direct_vote_proposal_"
    "keys_with_conservative_attempt_start_lower_bound_at_or_after_sealed_fault_"
    "open_and_guarded_reporter_timeouts_restricted_to_that_exact_post_fault_"
    "proposal_key_set_v1"
)
PRECONTAINMENT_SHAPE_EVALUATION_CONTRACT_V1 = (
    "epoch_zero_fault_containment_preserves_current_fanout_without_shape_v1_"
    "decision_later_transition_retains_exact_shape_v1_v1"
)
PRECONTAINMENT_GUARDED_SELECTION_CONTRACT_V1 = (
    "post_baseline_high_water_drawdown_and_exact_post_fault_proposal_key_timeout_"
    "witnesses_are_independent_factory_validated_domains_v1"
)
FUTURE_TREE_PROPOSAL_DELIVERY_CONTRACT_V1 = (
    "exact_immediate_successor_tree_proposals_relayed_and_buffered_without_pre_"
    "activation_protocol_effects_then_revalidated_and_replayed_once_after_exact_"
    "activation_v1"
)
FUTURE_TREE_PROPOSAL_DELIVERY_CONTRACT_V2 = (
    "exact_nonwrapping_same_epoch_tree_count_minus_one_future_proposal_horizon_"
    "is_capacity_bounded_relayed_and_buffered_without_pre_activation_protocol_"
    "effects_then_revalidated_and_replayed_once_after_each_exact_activation_v2"
)
SOURCE_BOUND_PROPOSAL_WITNESS_CONTRACT_V1 = (
    "strictly_bijected_source_bound_fault_contribution_opportunity_proposal_"
    "keys_are_native_proposal_configuration_witnesses_after_exact_topology_"
    "validation_v1"
)
EVIDENCE_SNAPSHOT_SELECTION_CONTRACT_V1 = (
    "full_prefix_without_inherited_consensus_wait_exempt_and_baseline_exclusive_"
    "suffix_with_exact_inherited_consensus_wait_exempt_v1"
)
INHERITED_CONSENSUS_WAIT_EXEMPT_PLACEMENT_CONTRACT_V1 = (
    "exact_selected_wait_exempt_replicas_are_excluded_from_root_and_internal_"
    "assignment_and_placed_as_leaves_in_every_successor_tree_v1"
)
VERIFIED_RESPONSE_DUPLICATE_DELIVERY_CONTRACT_V1 = (
    "exact_duplicate_verified_child_response_is_idempotent_and_cannot_fail_the_"
    "response_deadline_or_suppress_later_convergence_observations_v1"
)
VERIFIED_RESPONSE_DUPLICATE_DELIVERY_CONTRACT_V2 = (
    "exact_duplicate_verified_child_response_is_idempotent_guard_marker_follows_"
    "completed_accepted_ingress_and_cannot_fail_the_response_deadline_or_suppress_"
    "later_convergence_observations_v2"
)
EXCLUDED_REPAIR_SMOKE_OBSERVATION_CONTRACT_V1 = (
    "exact_excluded_repair_smoke_epoch1_selection_terminal_all_replica_command_"
    "activation_and_stable_end_before_fault_end_then_fault_end_before_cycle1_"
    "selection_then_epoch2_terminal_all_replica_command_activation_stable_end_"
    "and_drain_before_shared_hard_deadline_v1"
)
EXCLUDED_REPAIR_SMOKE_OBSERVATION_CONTRACT_V2 = (
    "exact_excluded_repair_smoke_fault_evidence_and_epoch1_stable_are_the_only_"
    "fault_active_causal_phases_with_epoch1_selection_terminal_all_replica_"
    "command_activation_and_stable_end_before_fault_end_then_epoch2_is_post_"
    "fault_recovery_and_stability_with_fault_end_before_cycle1_selection_then_"
    "epoch2_terminal_all_replica_command_activation_stable_end_and_drain_before_"
    "shared_hard_deadline_v2"
)
EXCLUDED_REPAIR_SMOKE_OBSERVATION_CONTRACT_V3 = (
    "exact_excluded_repair_smoke_source_fault_window_duration_450s_effective_"
    "fault_window_duration_330s_hard_deadline_650s_with_fault_evidence_and_"
    "epoch1_stable_as_the_only_fault_active_causal_phases_with_epoch1_selection_"
    "terminal_all_replica_command_activation_and_stable_end_before_fault_end_"
    "then_epoch2_as_post_fault_recovery_and_stability_with_fault_end_before_"
    "cycle1_selection_then_epoch2_terminal_all_replica_command_activation_"
    "stable_end_and_drain_before_shared_hard_deadline_v3"
)
EXCLUDED_REPAIR_SMOKE_OBSERVATION_CONTRACT_V4 = (
    "exact_excluded_repair_smoke_source_fault_window_duration_450s_effective_"
    "fault_window_duration_330s_hard_deadline_650s_post_fault_cycle1_selection_"
    "observation_grace_5s_with_exact_cycle1_manager_selection_gate_bound_to_the_"
    "materialized_shared_clock_fault_end_plus_the_frozen_observation_grace_as_"
    "an_exclusive_lower_bound_with_fault_evidence_and_epoch1_stable_as_the_only_"
    "fault_active_causal_phases_with_epoch1_selection_terminal_all_replica_"
    "command_activation_and_stable_end_before_fault_end_then_epoch2_as_post_"
    "fault_recovery_and_stability_with_fault_end_before_cycle1_selection_and_"
    "all_replica_commands_then_epoch2_terminal_all_replica_activation_stable_"
    "end_and_drain_before_shared_hard_deadline_v4"
)
EXCLUDED_REPAIR_SMOKE_OBSERVATION_CONTRACT_V5 = (
    "exact_excluded_repair_smoke_source_fault_window_duration_450s_effective_"
    "fault_window_duration_360s_hard_deadline_650s_post_fault_cycle1_selection_"
    "observation_grace_5s_with_exact_cycle1_manager_selection_gate_bound_to_the_"
    "materialized_shared_clock_fault_end_plus_the_frozen_observation_grace_as_an_"
    "exclusive_lower_bound_and_exact_cycle1_inherited_wait_exempt_eligibility_"
    "gate_requiring_every_canonical_inherited_wait_exempt_replica_to_be_"
    "responsive_and_eligible_in_the_exact_baseline_exclusive_selected_suffix_at_"
    "the_same_cycle1_selection_cutoff_and_exact_cycle1_authenticated_reporter_"
    "cross_commit_retention_readiness_gate_requiring_one_schema_v2_attempt_start_"
    "and_reporter_local_commit_timeout_fact_per_canonical_responsive_degraded_"
    "actor_and_at_least_one_aggregate_relay_fact_at_the_same_evidence_high_water_"
    "cutoff_with_fault_evidence_and_epoch1_stable_as_the_only_fault_active_"
    "causal_phases_with_epoch1_selection_terminal_all_replica_command_activation_"
    "and_stable_end_before_fault_end_then_epoch2_as_post_fault_recovery_and_"
    "stability_with_fault_end_before_cycle1_selection_and_all_replica_commands_"
    "then_epoch2_terminal_all_replica_activation_stable_end_drain_and_runner_"
    "terminal_before_shared_hard_deadline_v5"
)
EXCLUDED_REPAIR_SMOKE_VERIFIED_RESPONSE_DUPLICATE_PROBE_CONTRACT_V1 = (
    "exact_excluded_repair_smoke_at_least_one_post_fault_epoch1_internal_"
    "responsive_child_response_is_replayed_only_into_response_evidence_bridge_"
    "after_consensus_acceptance_and_first_evidence_record_with_at_most_one_probe_"
    "per_reporter_v1"
)
POST_FINAL_CONVERGENCE_UNMATCHED_COMMIT_EVIDENCE_CONTRACT_V1 = (
    "exact_v36_non_designated_legal_qc_skipped_ancestor_without_authenticated_"
    "exact_identity_source_authoritative_commit_identity_absence_strictly_after_"
    "final_cycle_successor_converged_terminal_is_scoped_only_when_each_gap_has_"
    "one_same_source_native_marker_every_replica_has_exactly_one_matching_commit_"
    "observed_the_designated_observer_stream_is_complete_and_at_least_derived_q_"
    "distinct_source_bound_rich_block_committed_proofs_match_height_hash_parent_"
    "transaction_count_commit_batch_index_epoch_tree_digest_and_view_generation_"
    "without_synthesizing_commit_evidence_or_changing_consensus_or_throughput_"
    "authority_v1"
)
POST_FINAL_CONVERGENCE_UNMATCHED_COMMIT_EVIDENCE_CONTRACT_V2 = (
    "exact_v38_legal_qc_skipped_ancestor_without_authenticated_exact_identity_"
    "source_authoritative_commit_identity_absence_strictly_after_final_cycle_"
    "successor_converged_terminal_is_scoped_only_when_each_gap_has_one_same_"
    "source_native_marker_every_replica_has_exactly_one_matching_commit_observed_"
    "and_at_least_derived_q_distinct_source_bound_rich_block_committed_proofs_"
    "match_height_hash_parent_transaction_count_commit_batch_index_epoch_tree_"
    "digest_and_view_generation_non_designated_gaps_require_the_designated_"
    "observer_rich_proof_while_at_most_one_designated_observer_gap_is_permitted_"
    "only_for_zero_transactions_with_exact_height_adjacent_designated_observer_"
    "rich_predecessor_and_successor_parent_chain_and_configuration_generation_"
    "closure_every_positive_transaction_designated_observer_observation_remains_"
    "complete_without_synthesizing_commit_evidence_or_changing_consensus_or_"
    "transaction_throughput_authority_v2"
)
RESPONSIVE_ROLE_SCOPED_SCHEDULE_V1 = (
    "omit_every_41st_unique_non_root_contribution_per_exact_epoch_identity_"
    "physical_role_stream_v1"
)
EXECUTION_CLEANUP_CONTRACT_V1 = (
    "replicas_sigint_only_manager_sigint_or_exact_terminal_authorized_zero_v1"
)
BREAKTHROUGH_STRUCTURAL_GATE_V4 = (
    "all_n31_f5_p_ps_slots_validate_source_bound_fault_contribution_"
    "opportunity_to_kauri_fault_exact_bijection_with_per_phase_actor_"
    "nonvacuity_and_tiered_markers_match_hard_and_every_41st_unique_non_"
    "root_responsive_degraded_omission_schedule_and_each_hard_actor_has_f_"
    "plus_1_distinct_exact_role_bound_timeout_reporters_and_at_least_one_"
    "internal_omit_aggregate_proof_and_each_responsive_degraded_actor_has_"
    "its_own_exact_reporter_local_epoch1_internal_omit_aggregate_cross_"
    "commit_witness_and_responsive_degraded_replicas_rank_below_every_fast_"
    "replica_and_epoch1_places_every_responsive_degraded_replica_as_a_root_"
    "and_exposes_each_in_an_internal_role_and_epoch2_roots_equal_top_q_fast_"
    "replicas_with_only_fast_replicas_in_root_and_internal_roles_and_all_f_"
    "worse_replicas_as_physical_leaves_and_only_hard_cohort_wait_exempt_v4"
)
EXPECTED_REPLICA_COUNTS = (13, 22, 31)
EXPECTED_INITIAL_FANOUTS = (2, 3, 5)
EXPECTED_CANDIDATE_FANOUTS = (2, 3, 5)
EXPECTED_ARM_CODES = ("00", "P", "S", "PS")
EXPECTED_BLOCK_COUNT = 17
EXPECTED_SLOT_COUNT = 68

_FORBIDDEN_AUTHORITY_FIELDS = frozenset(
    {"f", "Q", "fault_threshold", "quorum", "tree_count"}
)


class FactorialManifestError(ValueError):
    """The manifest or derived plan violates the frozen SHAPE26 contract."""


class _Document:
    def as_document(self) -> dict[str, object]:
        return dict(asdict(self))


@dataclass(frozen=True, slots=True)
class FactorialArm(_Document):
    code: str
    placement_adaptation: bool
    shape_adaptation: bool


@dataclass(frozen=True, slots=True)
class ByzantineActions(_Document):
    root: str
    internal: str
    leaf: str


@dataclass(frozen=True, slots=True)
class ActorRotationVector(_Document):
    epoch_number: int
    tree_id: int
    epoch_digest: str
    block_hash: str
    sorted_actor_ids: tuple[int, ...]
    fnv1a64: int
    selected_actor: int


@dataclass(frozen=True, slots=True)
class ActorSelectionVector(_Document):
    replica_count: int
    q: int
    scientific_seed: int
    selected_actor_ids: tuple[int, ...]


@dataclass(frozen=True, slots=True)
class ResponsiveDegradationContract(_Document):
    actor_count_rule: str
    actor_selection: str
    actor_selection_preimage: str
    actor_selection_inputs: tuple[str, ...]
    actor_selection_vectors: tuple[ActorSelectionVector, ...]
    observer_isolation: str
    actor_schedule: str
    omission_period: int
    pending_attempt_retention: str | None = None
    causal_timeout_linkage: str | None = None
    causal_timeout_provenance_window: str | None = None
    causal_internal_witness_candidates: str | None = None
    causal_selection_linkage_window: str | None = None
    marker_completeness_witness: str | None = None
    causal_timeout_eligibility: str | None = None
    precontainment_fault_coverage_gate: str | None = None
    precontainment_shape_evaluation_contract: str | None = None
    precontainment_guarded_selection_contract: str | None = None
    future_tree_proposal_delivery_contract: str | None = None
    source_bound_proposal_witness_contract: str | None = None
    evidence_snapshot_selection_contract: str | None = None
    inherited_consensus_wait_exempt_placement_contract: str | None = None
    verified_response_duplicate_delivery_contract: str | None = None
    excluded_repair_smoke_observation_contract: str | None = None
    excluded_repair_smoke_verified_response_duplicate_probe_contract: str | None = (
        None
    )
    post_final_convergence_unmatched_commit_evidence_contract: str | None = None
    minimum_primary_n31_f5_epoch1_internal_role_opportunities_per_actor_before_selection: (
        int | None
    ) = None

    def as_document(self) -> dict[str, object]:
        document = _Document.as_document(self)
        for field in (
            "pending_attempt_retention",
            "causal_timeout_linkage",
            "causal_timeout_provenance_window",
            "causal_internal_witness_candidates",
            "causal_selection_linkage_window",
            "marker_completeness_witness",
            "causal_timeout_eligibility",
            "precontainment_fault_coverage_gate",
            "precontainment_shape_evaluation_contract",
            "precontainment_guarded_selection_contract",
            "future_tree_proposal_delivery_contract",
            "source_bound_proposal_witness_contract",
            "evidence_snapshot_selection_contract",
            "inherited_consensus_wait_exempt_placement_contract",
            "verified_response_duplicate_delivery_contract",
            "excluded_repair_smoke_observation_contract",
            "excluded_repair_smoke_verified_response_duplicate_probe_contract",
            "post_final_convergence_unmatched_commit_evidence_contract",
            "minimum_primary_n31_f5_epoch1_internal_role_opportunities_per_actor_before_selection",
        ):
            if document[field] is None:
                document.pop(field)
        return document


@dataclass(frozen=True, slots=True)
class ByzantineContract(_Document):
    mode: str
    actor_count: int
    actor_count_rule: str
    actor_selection: str
    actor_selection_preimage: str
    actor_selection_inputs: tuple[str, ...]
    actor_selection_vectors: tuple[ActorSelectionVector, ...]
    actor_schedule: str
    actor_rotation_vectors: tuple[ActorRotationVector, ...]
    maximum_rotating_contexts: int
    start_after_prelaunch_anchor_s: int
    duration_s: int
    max_omissions_per_proposal: int | None
    actions: ByzantineActions
    responsive_degradation: ResponsiveDegradationContract | None = None
    max_omissions_per_proposal_rule: str | None = None

    def as_document(self) -> dict[str, object]:
        document = _Document.as_document(self)
        if self.mode == "rotating_intermittent_omission_v1":
            document["actor_rotation"] = document.pop("actor_schedule")
        if self.responsive_degradation is None:
            document.pop("responsive_degradation")
        else:
            document["responsive_degradation"] = (
                self.responsive_degradation.as_document()
            )
        if self.max_omissions_per_proposal is None:
            document.pop("max_omissions_per_proposal")
        if self.max_omissions_per_proposal_rule is None:
            document.pop("max_omissions_per_proposal_rule")
        return document


@dataclass(frozen=True, slots=True)
class WorkloadContract(_Document):
    block_size: int
    piped_latency_ms: int
    tree_switch_period_blocks: int
    bucket_width_s: int
    baseline_bucket_count: int
    fault_evidence_bucket_count: int
    epoch1_stable_bucket_count: int
    epoch2_stable_bucket_count: int
    epoch1_preselection_residency_ms: int | None = None

    def as_document(self) -> dict[str, object]:
        document = _Document.as_document(self)
        if self.epoch1_preselection_residency_ms is None:
            document.pop("epoch1_preselection_residency_ms")
        return document


@dataclass(frozen=True, slots=True)
class ResponsivenessPolicyContract(_Document):
    policy_version: str
    attempt_window: int
    minimum_attempts: int
    minimum_response_rate_ppm: int
    maximum_timeout_rate_ppm: int
    trailing_timeout_streak: int
    latency_percentile_basis_points: int


@dataclass(frozen=True, slots=True)
class CommonTimers(_Document):
    depth_policy: str
    global_worst_candidate_depth: int
    aggregation_timeout_ms_per_depth: int
    leader_progress_timeout_ms_per_depth: int
    leader_activation_grace_ms: int
    activation_delay_blocks: int
    transition_convergence_deadline_s: int
    schedule_slack_s: int
    drain_margin_s: int
    startup_timeout_s: int
    hard_timeout_s: int
    transition_observation_bound_rule: str = "phase_deadline_v1"

    def as_document(self) -> dict[str, object]:
        document = _Document.as_document(self)
        if self.transition_observation_bound_rule == "phase_deadline_v1":
            document.pop("transition_observation_bound_rule")
        return document

    @property
    def aggregation_timeout_ms(self) -> int:
        return self.aggregation_timeout_ms_per_depth * self.global_worst_candidate_depth

    @property
    def leader_progress_timeout_ms(self) -> int:
        return (
            self.leader_progress_timeout_ms_per_depth
            * self.global_worst_candidate_depth
        )


@dataclass(frozen=True, slots=True)
class ResourceContract(_Document):
    minimum_free_bytes: int
    minimum_free_bytes_interpretation: str
    max_parallel_slots: int
    peer_port_base: int
    client_port_base: int
    manager_port_base: int
    slot_port_stride: int


@dataclass(frozen=True, slots=True)
class ClaimScope(_Document):
    other_cells: str
    causal_inference: str
    headline_estimator: str
    uncertainty_interval: str
    directional_claim_rule: str
    phase_sequence_endpoint: str
    placement_headline_replica_count: int
    placement_headline_initial_fanout: int
    placement_headline_block_count: int
    shape_and_joint_headline_replica_count: int
    shape_and_joint_headline_initial_fanout: int
    shape_and_joint_headline_block_count: int
    breakthrough_scope: str | None = None
    breakthrough_structural_gate: str | None = None
    breakthrough_structural_required_slot_count: int | None = None
    breakthrough_realized_placement_rule: str | None = None
    breakthrough_realized_placement_per_arm_requirement: int | None = None
    breakthrough_primary_throughput_estimand: str | None = None
    breakthrough_throughput_claim_rule: str | None = None
    breakthrough_positive_block_requirement: int | None = None
    breakthrough_epoch1_baseline_ratio_role: str | None = None
    breakthrough_absolute_phase_sequence_estimands: str | None = None
    breakthrough_phase_window_interpretation: str | None = None
    breakthrough_pre_epoch1_placebo_estimand: str | None = None
    breakthrough_placebo_equivalence_rule: str | None = None
    breakthrough_placebo_equivalence_margin_log: float | None = None
    breakthrough_secondary_scope: str | None = None
    breakthrough_secondary_status_rule: str | None = None
    breakthrough_secondary_structural_gate: str | None = None
    breakthrough_secondary_structural_required_slot_count: int | None = None
    breakthrough_secondary_realized_placement_rule: str | None = None
    breakthrough_secondary_realized_placement_per_arm_requirement: int | None = None
    breakthrough_secondary_throughput_estimand: str | None = None
    breakthrough_secondary_throughput_claim_rule: str | None = None
    breakthrough_secondary_positive_block_requirement: int | None = None
    breakthrough_secondary_pre_epoch1_placebo_estimand: str | None = None
    breakthrough_secondary_placebo_equivalence_rule: str | None = None
    breakthrough_secondary_placebo_equivalence_margin_log: float | None = None

    def as_document(self) -> dict[str, object]:
        return {
            key: value
            for key, value in _Document.as_document(self).items()
            if value is not None
        }


@dataclass(frozen=True, slots=True)
class BlockExecutionSchedule(_Document):
    block_id: str
    block_execution_ordinal: int
    arm_order: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class PortAllocation(_Document):
    peer_base: int
    client_base: int
    manager: int


@dataclass(frozen=True, slots=True)
class ConsensusShape(_Document):
    replica_count: int
    f: int
    q: int
    tree_count: int
    initial_fanout: int
    initial_depth: int
    candidate_depths: tuple[tuple[int, int], ...]
    worst_candidate_depth: int


@dataclass(frozen=True, slots=True)
class TieredCohorts(_Document):
    hard_actor_ids: tuple[int, ...]
    responsive_degraded_actor_ids: tuple[int, ...]
    fast_replica_ids: tuple[int, ...]


@dataclass(frozen=True, slots=True)
class FrozenFactorialManifest:
    manifest_id: str
    manifest_sha256: str
    replica_counts: tuple[int, ...]
    initial_fanouts: tuple[int, ...]
    candidate_fanouts: tuple[int, ...]
    pipeline_stretch: int
    epoch_fanout_policy: str
    pipeline_policy: str
    arms: tuple[FactorialArm, ...]
    default_blocks_per_cell: int
    repetition_overrides: tuple[tuple[int, int, int], ...]
    byzantine: ByzantineContract
    workload: WorkloadContract
    responsiveness_policy: ResponsivenessPolicyContract
    common_timers: CommonTimers
    scientific_seed_base: int
    scientific_seed_rule: str
    slot_order: str
    slot_nonce_rule: str
    campaign_order_seed: int
    execution_block_order: str
    arm_counterbalancing: str
    scheduling_outcome_dependent_order: bool
    claim_scope: ClaimScope
    results_root: str
    canonical_plan_filename: str
    one_directory_per_slot: bool
    preserve_outcomes: tuple[str, ...]
    evidence_snapshot_format: str
    resources: ResourceContract
    execution_mode: str
    automatic_retries: int
    replacement_policy: str
    execution_outcome_dependent_order: bool
    execution_authorized: bool
    execution_receipt_required: bool
    cleanup_contract: str | None = None

    def blocks_for(self, replica_count: int, initial_fanout: int) -> int:
        for override_n, override_fanout, blocks in self.repetition_overrides:
            if (replica_count, initial_fanout) == (override_n, override_fanout):
                return blocks
        return self.default_blocks_per_cell


@dataclass(frozen=True, slots=True)
class FactorialSlot(_Document):
    ordinal: int
    slot_nonce: int
    slot_id: str
    block_id: str
    block_index: int
    blocks_in_cell: int
    block_execution_ordinal: int
    arm_execution_position: int
    execution_ordinal: int
    scientific_seed: int
    consensus: ConsensusShape
    candidate_fanouts: tuple[int, ...]
    pipeline_stretch: int
    epoch_fanout_policy: str
    pipeline_policy: str
    arm: FactorialArm
    byzantine: ByzantineContract
    byzantine_actor_ids: tuple[int, ...]
    workload: WorkloadContract
    responsiveness_policy: ResponsivenessPolicyContract
    common_timers: CommonTimers
    ports: PortAllocation
    result_path: str
    cleanup_contract: str | None = None
    responsive_degraded_actor_ids: tuple[int, ...] = ()
    fast_replica_ids: tuple[int, ...] = ()
    max_omissions_per_proposal: int | None = None

    def as_document(self) -> dict[str, object]:
        document = _Document.as_document(self)
        document["byzantine"] = self.byzantine.as_document()
        document["common_timers"] = self.common_timers.as_document()
        document["workload"] = self.workload.as_document()
        if self.cleanup_contract is None:
            document.pop("cleanup_contract")
        if self.byzantine.responsive_degradation is None:
            document.pop("responsive_degraded_actor_ids")
            document.pop("fast_replica_ids")
            document.pop("max_omissions_per_proposal")
        return document

    @property
    def replica_count(self) -> int:
        return self.consensus.replica_count

    @property
    def f(self) -> int:
        return self.consensus.f

    @property
    def q(self) -> int:
        return self.consensus.q

    @property
    def tree_count(self) -> int:
        return self.consensus.tree_count

    @property
    def initial_fanout(self) -> int:
        return self.consensus.initial_fanout

    @property
    def initial_depth(self) -> int:
        return self.consensus.initial_depth

    @property
    def candidate_depths(self) -> tuple[tuple[int, int], ...]:
        return self.consensus.candidate_depths

    @property
    def worst_candidate_depth(self) -> int:
        return self.consensus.worst_candidate_depth

    @property
    def arm_code(self) -> str:
        return self.arm.code

    @property
    def placement_adaptation(self) -> bool:
        return self.arm.placement_adaptation

    @property
    def shape_adaptation(self) -> bool:
        return self.arm.shape_adaptation

    @property
    def seed(self) -> int:
        return self.scientific_seed

    @property
    def maximum_omissions_per_proposal(self) -> int:
        if (
            self.byzantine.max_omissions_per_proposal_rule
            == "derived_f_per_slot_v1"
        ):
            if self.max_omissions_per_proposal != self.f:
                _error("slot Byzantine omission maximum must equal derived f")
            return self.max_omissions_per_proposal
        maximum = self.byzantine.max_omissions_per_proposal
        if type(maximum) is not int or maximum < 1:
            _error("slot Byzantine omission maximum is not derivable")
        return maximum


@dataclass(frozen=True, slots=True)
class FactorialPlan(_Document):
    manifest_id: str
    manifest_sha256: str
    execution_authorized: bool
    execution_receipt_required: bool
    execution_mode: str
    automatic_retries: int
    replacement_policy: str
    outcome_dependent_order: bool
    campaign_order_seed: int
    execution_block_order: str
    arm_counterbalancing: str
    execution_schedule: tuple[BlockExecutionSchedule, ...]
    claim_scope: ClaimScope
    preserve_outcomes: tuple[str, ...]
    results_root: str
    canonical_plan_filename: str
    minimum_free_bytes: int
    minimum_free_bytes_interpretation: str
    max_parallel_slots: int
    global_worst_candidate_depth: int
    slots: tuple[FactorialSlot, ...]

    def as_document(self) -> dict[str, object]:
        document = _Document.as_document(self)
        document["claim_scope"] = self.claim_scope.as_document()
        document["slots"] = tuple(slot.as_document() for slot in self.slots)
        document.update(
            {
                "plan_id": f"{self.manifest_id}-plan-v1",
                "schema_version": 1,
                "slot_count": len(self.slots),
            }
        )
        return document

    @property
    def canonical_bytes(self) -> bytes:
        return canonical_plan_bytes(self)

    @property
    def plan_sha256(self) -> str:
        return hashlib.sha256(self.canonical_bytes).hexdigest()

    def require_execution_authorized(self) -> None:
        if not self.execution_authorized or self.execution_receipt_required:
            _error(
                "factorial execution is not authorized; a later sealed "
                "execution receipt is required"
            )


def _error(message: str) -> None:
    raise FactorialManifestError(message)


def _duplicate_rejecting_object(
    pairs: list[tuple[str, Any]],
) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            _error(f"JSON document contains duplicate key: {key}")
        result[key] = value
    return result


def _parse_json(payload: bytes) -> dict[str, Any]:
    try:
        value = json.loads(
            payload.decode("utf-8"),
            object_pairs_hook=_duplicate_rejecting_object,
            parse_constant=lambda constant: _error(
                f"manifest contains non-finite constant: {constant}"
            ),
        )
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise FactorialManifestError("manifest is not strict UTF-8 JSON") from error
    if not isinstance(value, dict):
        _error("manifest must be a JSON object")
    return value


def _mapping(value: object, label: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        _error(f"{label} must be a JSON object")
    return value


def _array(value: object, label: str) -> list[Any]:
    if not isinstance(value, list):
        _error(f"{label} must be a JSON array")
    return value


def _integer(value: object, label: str, *, minimum: int = 1) -> int:
    if type(value) is not int or value < minimum:
        _error(f"{label} must be an integer >= {minimum}")
    return value


def _fanouts(value: object, label: str) -> tuple[int, ...]:
    fanouts = tuple(
        _integer(item, f"{label}[{index}]")
        for index, item in enumerate(_array(value, label))
    )
    noun = label[:-1] if label.endswith("s") else label
    if any(fanout > 255 for fanout in fanouts):
        _error(f"{noun} must be 1..255")
    if len(set(fanouts)) != len(fanouts):
        _error(f"{noun} values must be unique")
    return fanouts


def _reject_authority_fields(value: object, label: str = "manifest") -> None:
    if isinstance(value, Mapping):
        for key, nested in value.items():
            if key in _FORBIDDEN_AUTHORITY_FIELDS:
                _error(f"{label}.{key} is derived from N and cannot be supplied")
            _reject_authority_fields(nested, f"{label}.{key}")
    elif isinstance(value, list):
        for index, nested in enumerate(value):
            _reject_authority_fields(nested, f"{label}[{index}]")


def _canonical_json_bytes(value: object) -> bytes:
    try:
        return (
            json.dumps(
                value,
                allow_nan=False,
                ensure_ascii=True,
                separators=(",", ":"),
                sort_keys=True,
            ).encode("utf-8")
            + b"\n"
        )
    except (TypeError, ValueError) as error:
        raise FactorialManifestError("document is not canonical JSON") from error


def rotating_omission_actor(
    actor_ids: Sequence[int],
    *,
    epoch_number: int,
    tree_id: int,
    epoch_digest: str,
    block_hash: str,
) -> tuple[int, int]:
    """Return the FNV-1a hash and actor used by the native adapter."""

    actors = tuple(
        sorted(_integer(actor, "rotating actor", minimum=0) for actor in actor_ids)
    )
    if not actors or len(set(actors)) != len(actors):
        _error("rotating actors must be a non-empty unique set")
    epoch = _integer(epoch_number, "proposal epoch", minimum=0)
    tree = _integer(tree_id, "proposal tree", minimum=0)
    if epoch > 0xFFFF_FFFF or tree > 0xFFFF_FFFF:
        _error("proposal epoch and tree must fit uint32")
    try:
        epoch_bytes = bytes.fromhex(epoch_digest)
        block_bytes = bytes.fromhex(block_hash)
    except ValueError as error:
        raise FactorialManifestError("proposal digests must be hexadecimal") from error
    if len(epoch_bytes) != 32 or len(block_bytes) != 32:
        _error("proposal digests must each contain exactly 32 bytes")

    value = 14_695_981_039_346_656_037
    for byte in (
        epoch.to_bytes(4, "big") + tree.to_bytes(4, "big") + epoch_bytes + block_bytes
    ):
        value ^= byte
        value = (value * 1_099_511_628_211) & 0xFFFF_FFFF_FFFF_FFFF
    return value, actors[value % len(actors)]


def _validate_frozen_semantics(document: Mapping[str, Any]) -> None:
    manifest_id = document.get("manifest_id")
    if manifest_id not in {
        LEGACY_MANIFEST_ID,
        V2_MANIFEST_ID,
        V3_MANIFEST_ID,
        V4_MANIFEST_ID,
        V5_MANIFEST_ID,
        V6_MANIFEST_ID,
        V7_MANIFEST_ID,
        V8_MANIFEST_ID,
        V9_MANIFEST_ID,
        V10_MANIFEST_ID,
        V11_MANIFEST_ID,
        V12_MANIFEST_ID,
        V13_MANIFEST_ID,
        V14_MANIFEST_ID,
        V15_MANIFEST_ID,
        V16_MANIFEST_ID,
        V17_MANIFEST_ID,
        V18_MANIFEST_ID,
        V19_MANIFEST_ID,
        V20_MANIFEST_ID,
        V21_MANIFEST_ID,
        V22_MANIFEST_ID,
        V23_MANIFEST_ID,
        V24_MANIFEST_ID,
        V25_MANIFEST_ID,
        V26_MANIFEST_ID,
        V27_MANIFEST_ID,
        V28_MANIFEST_ID,
        V29_MANIFEST_ID,
        V30_MANIFEST_ID,
        V31_MANIFEST_ID,
        V32_MANIFEST_ID,
        V33_MANIFEST_ID,
        V34_MANIFEST_ID,
        V35_MANIFEST_ID,
        V36_MANIFEST_ID,
        V37_MANIFEST_ID,
        V38_MANIFEST_ID,
        V39_MANIFEST_ID,
        FROZEN_MANIFEST_ID,
    }:
        _error("manifest ID is not a known frozen SHAPE40 contract")
    tiered = manifest_id in {
        V8_MANIFEST_ID,
        V9_MANIFEST_ID,
        V10_MANIFEST_ID,
        V11_MANIFEST_ID,
        V12_MANIFEST_ID,
        V13_MANIFEST_ID,
        V14_MANIFEST_ID,
        V15_MANIFEST_ID,
        V16_MANIFEST_ID,
        V17_MANIFEST_ID,
        V18_MANIFEST_ID,
        V19_MANIFEST_ID,
        V20_MANIFEST_ID,
        V21_MANIFEST_ID,
        V22_MANIFEST_ID,
        V23_MANIFEST_ID,
        V24_MANIFEST_ID,
        V25_MANIFEST_ID,
        V26_MANIFEST_ID,
        V27_MANIFEST_ID,
        V28_MANIFEST_ID,
        V29_MANIFEST_ID,
        V30_MANIFEST_ID,
        V31_MANIFEST_ID,
        V32_MANIFEST_ID,
        V33_MANIFEST_ID,
        V34_MANIFEST_ID,
        V35_MANIFEST_ID,
        V36_MANIFEST_ID,
        V37_MANIFEST_ID,
        V38_MANIFEST_ID,
        V39_MANIFEST_ID,
        FROZEN_MANIFEST_ID,
    }
    causal_measurement = manifest_id in {
        V9_MANIFEST_ID,
        V10_MANIFEST_ID,
        V11_MANIFEST_ID,
        V12_MANIFEST_ID,
        V13_MANIFEST_ID,
        V14_MANIFEST_ID,
        V15_MANIFEST_ID,
        V16_MANIFEST_ID,
        V17_MANIFEST_ID,
        V18_MANIFEST_ID,
        V19_MANIFEST_ID,
        V20_MANIFEST_ID,
        V21_MANIFEST_ID,
        V22_MANIFEST_ID,
        V23_MANIFEST_ID,
        V24_MANIFEST_ID,
        V25_MANIFEST_ID,
        V26_MANIFEST_ID,
        V27_MANIFEST_ID,
        V28_MANIFEST_ID,
        V29_MANIFEST_ID,
        V30_MANIFEST_ID,
        V31_MANIFEST_ID,
        V32_MANIFEST_ID,
        V33_MANIFEST_ID,
        V34_MANIFEST_ID,
        V35_MANIFEST_ID,
        V36_MANIFEST_ID,
        V37_MANIFEST_ID,
        V38_MANIFEST_ID,
        V39_MANIFEST_ID,
        FROZEN_MANIFEST_ID,
    }
    explicit_causal_windows = manifest_id in {
        V10_MANIFEST_ID,
        V11_MANIFEST_ID,
        V12_MANIFEST_ID,
        V13_MANIFEST_ID,
        V14_MANIFEST_ID,
        V15_MANIFEST_ID,
        V16_MANIFEST_ID,
        V17_MANIFEST_ID,
        V18_MANIFEST_ID,
        V19_MANIFEST_ID,
        V20_MANIFEST_ID,
        V21_MANIFEST_ID,
        V22_MANIFEST_ID,
        V23_MANIFEST_ID,
        V24_MANIFEST_ID,
        V25_MANIFEST_ID,
        V26_MANIFEST_ID,
        V27_MANIFEST_ID,
        V28_MANIFEST_ID,
        V29_MANIFEST_ID,
        V30_MANIFEST_ID,
        V31_MANIFEST_ID,
        V32_MANIFEST_ID,
        V33_MANIFEST_ID,
        V34_MANIFEST_ID,
        V35_MANIFEST_ID,
        V36_MANIFEST_ID,
        V37_MANIFEST_ID,
        V38_MANIFEST_ID,
        V39_MANIFEST_ID,
        FROZEN_MANIFEST_ID,
    }
    explicit_causal_edge_eligibility = manifest_id in {
        V11_MANIFEST_ID,
        V12_MANIFEST_ID,
        V13_MANIFEST_ID,
        V14_MANIFEST_ID,
        V15_MANIFEST_ID,
        V16_MANIFEST_ID,
        V17_MANIFEST_ID,
        V18_MANIFEST_ID,
        V19_MANIFEST_ID,
        V20_MANIFEST_ID,
        V21_MANIFEST_ID,
        V22_MANIFEST_ID,
        V23_MANIFEST_ID,
        V24_MANIFEST_ID,
        V25_MANIFEST_ID,
        V26_MANIFEST_ID,
        V27_MANIFEST_ID,
        V28_MANIFEST_ID,
        V29_MANIFEST_ID,
        V30_MANIFEST_ID,
        V31_MANIFEST_ID,
        V32_MANIFEST_ID,
        V33_MANIFEST_ID,
        V34_MANIFEST_ID,
        V35_MANIFEST_ID,
        V36_MANIFEST_ID,
        V37_MANIFEST_ID,
        V38_MANIFEST_ID,
        V39_MANIFEST_ID,
        FROZEN_MANIFEST_ID,
    }
    persistent = manifest_id in {
        V2_MANIFEST_ID,
        V3_MANIFEST_ID,
        V4_MANIFEST_ID,
        V5_MANIFEST_ID,
        V6_MANIFEST_ID,
        V7_MANIFEST_ID,
        V8_MANIFEST_ID,
        V9_MANIFEST_ID,
        V10_MANIFEST_ID,
        V11_MANIFEST_ID,
        V12_MANIFEST_ID,
        V13_MANIFEST_ID,
        V14_MANIFEST_ID,
        V15_MANIFEST_ID,
        V16_MANIFEST_ID,
        V17_MANIFEST_ID,
        V18_MANIFEST_ID,
        V19_MANIFEST_ID,
        V20_MANIFEST_ID,
        V21_MANIFEST_ID,
        V22_MANIFEST_ID,
        V23_MANIFEST_ID,
        V24_MANIFEST_ID,
        V25_MANIFEST_ID,
        V26_MANIFEST_ID,
        V27_MANIFEST_ID,
        V28_MANIFEST_ID,
        V29_MANIFEST_ID,
        V30_MANIFEST_ID,
        V31_MANIFEST_ID,
        V32_MANIFEST_ID,
        V33_MANIFEST_ID,
        V34_MANIFEST_ID,
        V35_MANIFEST_ID,
        V36_MANIFEST_ID,
        V37_MANIFEST_ID,
        V38_MANIFEST_ID,
        V39_MANIFEST_ID,
        FROZEN_MANIFEST_ID,
    }
    compact_snapshot = manifest_id in {
        V3_MANIFEST_ID,
        V4_MANIFEST_ID,
        V5_MANIFEST_ID,
        V6_MANIFEST_ID,
        V7_MANIFEST_ID,
        V8_MANIFEST_ID,
        V9_MANIFEST_ID,
        V10_MANIFEST_ID,
        V11_MANIFEST_ID,
        V12_MANIFEST_ID,
        V13_MANIFEST_ID,
        V14_MANIFEST_ID,
        V15_MANIFEST_ID,
        V16_MANIFEST_ID,
        V17_MANIFEST_ID,
        V18_MANIFEST_ID,
        V19_MANIFEST_ID,
        V20_MANIFEST_ID,
        V21_MANIFEST_ID,
        V22_MANIFEST_ID,
        V23_MANIFEST_ID,
        V24_MANIFEST_ID,
        V25_MANIFEST_ID,
        V26_MANIFEST_ID,
        V27_MANIFEST_ID,
        V28_MANIFEST_ID,
        V29_MANIFEST_ID,
        V30_MANIFEST_ID,
        V31_MANIFEST_ID,
        V32_MANIFEST_ID,
        V33_MANIFEST_ID,
        V34_MANIFEST_ID,
        V35_MANIFEST_ID,
        V36_MANIFEST_ID,
        V37_MANIFEST_ID,
        V38_MANIFEST_ID,
        V39_MANIFEST_ID,
        FROZEN_MANIFEST_ID,
    }
    replica_counts = tuple(
        _integer(item, f"replica_counts[{index}]")
        for index, item in enumerate(
            _array(document.get("replica_counts"), "replica_counts")
        )
    )
    if replica_counts != EXPECTED_REPLICA_COUNTS or 7 in replica_counts:
        _error("replica_counts must be exactly 13, 22, 31; N=7 smoke is excluded")
    if any((replica_count - 1) % 3 for replica_count in replica_counts):
        _error("every replica count must satisfy N = 3f + 1")

    responsiveness = _mapping(
        document.get("responsiveness_policy"), "responsiveness_policy"
    )
    expected_policy_version = (
        "shape25-direct-vote-responsiveness-v2"
        if manifest_id
        in {
            V13_MANIFEST_ID,
            V14_MANIFEST_ID,
            V15_MANIFEST_ID,
            V16_MANIFEST_ID,
            V17_MANIFEST_ID,
            V18_MANIFEST_ID,
            V19_MANIFEST_ID,
            V20_MANIFEST_ID,
            V21_MANIFEST_ID,
            V22_MANIFEST_ID,
            V23_MANIFEST_ID,
            V24_MANIFEST_ID,
            V25_MANIFEST_ID,
            V26_MANIFEST_ID,
            V27_MANIFEST_ID,
            V28_MANIFEST_ID,
            V29_MANIFEST_ID,
            V30_MANIFEST_ID,
            V31_MANIFEST_ID,
            V32_MANIFEST_ID,
            V33_MANIFEST_ID,
            V34_MANIFEST_ID,
            V35_MANIFEST_ID,
            V36_MANIFEST_ID,
            V37_MANIFEST_ID,
            V38_MANIFEST_ID,
            V39_MANIFEST_ID,
            FROZEN_MANIFEST_ID,
        }
        else "shape25-sensitive-responsiveness-v1"
    )
    if responsiveness.get("policy_version") != expected_policy_version:
        _error("responsiveness policy version must be the frozen SHAPE40 policy")
    attempt_window = _integer(
        responsiveness.get("attempt_window"),
        "responsiveness_policy.attempt_window",
    )
    minimum_attempts = _integer(
        responsiveness.get("minimum_attempts"),
        "responsiveness_policy.minimum_attempts",
    )
    minimum_response_rate_ppm = _integer(
        responsiveness.get("minimum_response_rate_ppm"),
        "responsiveness_policy.minimum_response_rate_ppm",
        minimum=0,
    )
    maximum_timeout_rate_ppm = _integer(
        responsiveness.get("maximum_timeout_rate_ppm"),
        "responsiveness_policy.maximum_timeout_rate_ppm",
        minimum=0,
    )
    trailing_timeout_streak = _integer(
        responsiveness.get("trailing_timeout_streak"),
        "responsiveness_policy.trailing_timeout_streak",
        minimum=2,
    )
    latency_percentile_basis_points = _integer(
        responsiveness.get("latency_percentile_basis_points"),
        "responsiveness_policy.latency_percentile_basis_points",
    )
    expected_minimum_attempts = (
        60
        if manifest_id
        in {
            V12_MANIFEST_ID,
            V13_MANIFEST_ID,
            V14_MANIFEST_ID,
            V15_MANIFEST_ID,
            V16_MANIFEST_ID,
            V17_MANIFEST_ID,
            V18_MANIFEST_ID,
            V19_MANIFEST_ID,
            V20_MANIFEST_ID,
            V21_MANIFEST_ID,
            V22_MANIFEST_ID,
            V23_MANIFEST_ID,
            V24_MANIFEST_ID,
            V25_MANIFEST_ID,
            V26_MANIFEST_ID,
            V27_MANIFEST_ID,
            V28_MANIFEST_ID,
            V29_MANIFEST_ID,
            V30_MANIFEST_ID,
            V31_MANIFEST_ID,
            V32_MANIFEST_ID,
            V33_MANIFEST_ID,
            V34_MANIFEST_ID,
            V35_MANIFEST_ID,
            V36_MANIFEST_ID,
            V37_MANIFEST_ID,
            V38_MANIFEST_ID,
            V39_MANIFEST_ID,
            FROZEN_MANIFEST_ID,
        }
        else (41 if causal_measurement else 32)
    )
    expected_trailing_timeout_streak = (
        7
        if manifest_id
        in {
            V12_MANIFEST_ID,
            V13_MANIFEST_ID,
            V14_MANIFEST_ID,
            V15_MANIFEST_ID,
            V16_MANIFEST_ID,
            V17_MANIFEST_ID,
            V18_MANIFEST_ID,
            V19_MANIFEST_ID,
            V20_MANIFEST_ID,
            V21_MANIFEST_ID,
            V22_MANIFEST_ID,
            V23_MANIFEST_ID,
            V24_MANIFEST_ID,
            V25_MANIFEST_ID,
            V26_MANIFEST_ID,
            V27_MANIFEST_ID,
            V28_MANIFEST_ID,
            V29_MANIFEST_ID,
            V30_MANIFEST_ID,
            V31_MANIFEST_ID,
            V32_MANIFEST_ID,
            V33_MANIFEST_ID,
            V34_MANIFEST_ID,
            V35_MANIFEST_ID,
            V36_MANIFEST_ID,
            V37_MANIFEST_ID,
            V38_MANIFEST_ID,
            V39_MANIFEST_ID,
            FROZEN_MANIFEST_ID,
        }
        else 2
    )
    if (
        attempt_window != 128
        or minimum_attempts != expected_minimum_attempts
        or minimum_response_rate_ppm != 950_000
        or maximum_timeout_rate_ppm != 50_000
        or trailing_timeout_streak != expected_trailing_timeout_streak
        or latency_percentile_basis_points != 5_000
    ):
        _error("responsiveness policy must equal the frozen sensitive profile")
    if (
        minimum_attempts > attempt_window
        or trailing_timeout_streak > attempt_window
        or minimum_response_rate_ppm > 1_000_000
        or maximum_timeout_rate_ppm > 1_000_000
        or latency_percentile_basis_points > 10_000
    ):
        _error("responsiveness policy values are outside fixed bounds")
    if 1_000_000 // 3 <= maximum_timeout_rate_ppm:
        _error(
            "selected actor expected timeout share must strictly exceed the "
            "frozen timeout threshold for every replica count"
        )

    initial_fanouts = _fanouts(document.get("initial_fanouts"), "initial fanouts")
    candidate_fanouts = _fanouts(document.get("candidate_fanouts"), "candidate fanouts")
    if initial_fanouts != EXPECTED_INITIAL_FANOUTS:
        _error("initial fanouts must be the exact frozen 2/3/5 set")
    if candidate_fanouts != EXPECTED_CANDIDATE_FANOUTS:
        _error("candidate fanouts must be the exact frozen 2/3/5 set")

    byzantine = _mapping(document.get("byzantine"), "byzantine")
    if (
        byzantine.get("actor_count") != 3
        or byzantine.get("actor_count_rule") != "fixed_3_bounded_by_derived_f"
        or byzantine.get("actor_selection")
        != "sha256_ranked_canonical_epoch0_non_reference_roots_v1"
        or byzantine.get("actor_selection_preimage")
        != (
            "ascii_csv_membership_nul_decimal_q_nul_decimal_scientific_"
            "seed_nul_decimal_replica_id_v1"
        )
        or byzantine.get("actor_selection_inputs")
        != [
            "membership",
            "derived_q",
            "canonical_epoch0_reference_roots_0_through_q_minus_1_v1",
            "scientific_block_seed",
        ]
    ):
        _error(
            "actors must be three seed-ranked members of the canonical "
            "epoch-0 non-reference-root pool"
        )
    if any(3 > (replica_count - 1) // 3 for replica_count in replica_counts):
        _error("fixed campaign actor count must not exceed derived f")
    if manifest_id in {
        V12_MANIFEST_ID,
        V13_MANIFEST_ID,
        V14_MANIFEST_ID,
        V15_MANIFEST_ID,
        V16_MANIFEST_ID,
        V17_MANIFEST_ID,
        V18_MANIFEST_ID,
        V19_MANIFEST_ID,
        V20_MANIFEST_ID,
        V21_MANIFEST_ID,
        V22_MANIFEST_ID,
        V23_MANIFEST_ID,
        V24_MANIFEST_ID,
        V25_MANIFEST_ID,
        V26_MANIFEST_ID,
        V27_MANIFEST_ID,
        V28_MANIFEST_ID,
        V29_MANIFEST_ID,
        V30_MANIFEST_ID,
        V31_MANIFEST_ID,
        V32_MANIFEST_ID,
        V33_MANIFEST_ID,
        V34_MANIFEST_ID,
        V35_MANIFEST_ID,
        V36_MANIFEST_ID,
        V37_MANIFEST_ID,
        V38_MANIFEST_ID,
        V39_MANIFEST_ID,
        FROZEN_MANIFEST_ID,
    }:
        expected_mode = "tiered_persistent_responsive_omission_v2"
    elif tiered:
        expected_mode = "tiered_persistent_responsive_omission_v1"
    elif persistent:
        expected_mode = "persistent_selected_omission_v1"
    else:
        expected_mode = "rotating_intermittent_omission_v1"
    if byzantine.get("mode") != expected_mode:
        _error(f"{manifest_id} requires Byzantine mode {expected_mode}")
    if byzantine.get("actions") != {
        "root": "normal",
        "internal": "omit_aggregate",
        "leaf": "omit_direct_vote",
    }:
        _error("Byzantine actions must match the role-aware omission contract")
    selection_vectors = _array(
        byzantine.get("actor_selection_vectors"),
        "byzantine.actor_selection_vectors",
    )
    expected_selection_vector_inputs = (
        (13, 9, 41_719),
        (22, 15, 41_722),
        (31, 21, 41_725),
    )
    if len(selection_vectors) != len(expected_selection_vector_inputs):
        _error("actor selection requires the three frozen ranking vectors")
    for index, (raw_vector, expected_inputs) in enumerate(
        zip(selection_vectors, expected_selection_vector_inputs)
    ):
        vector = _mapping(raw_vector, f"actor selection vector {index}")
        replica_count, q, scientific_seed = expected_inputs
        expected_selected = derive_actor_ids(
            replica_count,
            q,
            3,
            scientific_seed,
        )
        if vector != {
            "replica_count": replica_count,
            "q": q,
            "scientific_seed": scientific_seed,
            "selected_actor_ids": list(expected_selected),
        }:
            _error("actor selection vector disagrees with the SHA-256 ranking")
    expected_actor_schedule = (
        "all_hard_actors_per_proposal_v1"
        if tiered
        else (
            "all_selected_actors_per_proposal_v1"
            if persistent
            else (
                "fnv1a64_be_epoch_tree_epoch_digest_block_hash_"
                "modulo_sorted_actors_v1"
            )
        )
    )
    actor_schedule = byzantine.get(
        "actor_schedule" if persistent else "actor_rotation"
    )
    if actor_schedule != expected_actor_schedule:
        _error("actor schedule must name the exact native omission contract")
    if persistent and "actor_rotation" in byzantine:
        _error("persistent omission must not be mislabeled as actor rotation")
    if not persistent and "actor_schedule" in byzantine:
        _error("legacy rotating omission must retain its frozen schema")
    if (
        _integer(
            byzantine.get("maximum_rotating_contexts"),
            "byzantine.maximum_rotating_contexts",
        )
        != 100_000
    ):
        _error("rotating context capacity must equal the frozen bound")
    if persistent and "actor_rotation_vectors" in byzantine:
        _error("persistent omission must not carry legacy rotation vectors")
    vectors = _array(
        byzantine.get("actor_rotation_vectors", []),
        "byzantine.actor_rotation_vectors",
    )
    if len(vectors) != (0 if persistent else 3):
        _error(
            "persistent omission has no rotation vectors; rotating omission "
            "requires the three frozen cross-language vectors"
        )
    for index, raw_vector in enumerate(vectors):
        vector = _mapping(raw_vector, f"actor rotation vector {index}")
        computed_hash, computed_actor = rotating_omission_actor(
            _array(vector.get("sorted_actor_ids"), "sorted actor IDs"),
            epoch_number=vector.get("epoch_number"),
            tree_id=vector.get("tree_id"),
            epoch_digest=vector.get("epoch_digest"),
            block_hash=vector.get("block_hash"),
        )
        if (
            vector.get("fnv1a64") != computed_hash
            or vector.get("selected_actor") != computed_actor
        ):
            _error("actor rotation vector disagrees with the FNV-1a reference")
    if manifest_id in {
        V12_MANIFEST_ID,
        V13_MANIFEST_ID,
        V14_MANIFEST_ID,
        V15_MANIFEST_ID,
        V16_MANIFEST_ID,
        V17_MANIFEST_ID,
        V18_MANIFEST_ID,
        V19_MANIFEST_ID,
        V20_MANIFEST_ID,
        V21_MANIFEST_ID,
        V22_MANIFEST_ID,
        V23_MANIFEST_ID,
        V24_MANIFEST_ID,
        V25_MANIFEST_ID,
        V26_MANIFEST_ID,
        V27_MANIFEST_ID,
        V28_MANIFEST_ID,
        V29_MANIFEST_ID,
        V30_MANIFEST_ID,
        V31_MANIFEST_ID,
        V32_MANIFEST_ID,
        V33_MANIFEST_ID,
        V34_MANIFEST_ID,
        V35_MANIFEST_ID,
        V36_MANIFEST_ID,
        V37_MANIFEST_ID,
        V38_MANIFEST_ID,
        V39_MANIFEST_ID,
        FROZEN_MANIFEST_ID,
    }:
        expected_responsive_schedule = RESPONSIVE_ROLE_SCOPED_SCHEDULE_V1
    elif causal_measurement:
        expected_responsive_schedule = (
            "omit_every_41st_unique_non_root_contribution_per_"
            "responsive_degraded_actor_v2"
        )
    else:
        expected_responsive_schedule = (
            "omit_every_32nd_unique_non_root_contribution_per_"
            "responsive_degraded_actor_v1"
        )
    expected_responsive_period = 41 if causal_measurement else 32
    if tiered:
        responsive = _mapping(
            byzantine.get("responsive_degradation"),
            "byzantine.responsive_degradation",
        )
        expected_responsive_fields = {
            "actor_count_rule",
            "actor_selection",
            "actor_selection_preimage",
            "actor_selection_inputs",
            "actor_selection_vectors",
            "observer_isolation",
            "actor_schedule",
            "omission_period",
        }
        if causal_measurement:
            expected_responsive_fields.update(
                {"pending_attempt_retention", "causal_timeout_linkage"}
            )
        if explicit_causal_windows:
            expected_responsive_fields.update(
                {
                    "causal_timeout_provenance_window",
                    "causal_internal_witness_candidates",
                    "causal_selection_linkage_window",
                }
            )
        if explicit_causal_edge_eligibility:
            expected_responsive_fields.update(
                {
                    "marker_completeness_witness",
                    "causal_timeout_eligibility",
                }
            )
        if manifest_id in {
            V15_MANIFEST_ID,
            V16_MANIFEST_ID,
            V17_MANIFEST_ID,
            V18_MANIFEST_ID,
            V19_MANIFEST_ID,
            V20_MANIFEST_ID,
            V21_MANIFEST_ID,
            V22_MANIFEST_ID,
            V23_MANIFEST_ID,
            V24_MANIFEST_ID,
            V25_MANIFEST_ID,
            V26_MANIFEST_ID,
            V27_MANIFEST_ID,
            V28_MANIFEST_ID,
            V29_MANIFEST_ID,
            V30_MANIFEST_ID,
            V31_MANIFEST_ID,
            V32_MANIFEST_ID,
            V33_MANIFEST_ID,
            V34_MANIFEST_ID,
            V35_MANIFEST_ID,
            V36_MANIFEST_ID,
            V37_MANIFEST_ID,
            V38_MANIFEST_ID,
            V39_MANIFEST_ID,
            FROZEN_MANIFEST_ID,
        }:
            expected_responsive_fields.add("precontainment_fault_coverage_gate")
        if manifest_id in {
            V17_MANIFEST_ID,
            V18_MANIFEST_ID,
            V19_MANIFEST_ID,
            V20_MANIFEST_ID,
            V21_MANIFEST_ID,
            V22_MANIFEST_ID,
            V23_MANIFEST_ID,
            V24_MANIFEST_ID,
            V25_MANIFEST_ID,
            V26_MANIFEST_ID,
            V27_MANIFEST_ID,
            V28_MANIFEST_ID,
            V29_MANIFEST_ID,
            V30_MANIFEST_ID,
            V31_MANIFEST_ID,
            V32_MANIFEST_ID,
            V33_MANIFEST_ID,
            V34_MANIFEST_ID,
            V35_MANIFEST_ID,
            V36_MANIFEST_ID,
            V37_MANIFEST_ID,
            V38_MANIFEST_ID,
            V39_MANIFEST_ID,
            FROZEN_MANIFEST_ID,
        }:
            expected_responsive_fields.add(
                "precontainment_shape_evaluation_contract"
            )
        if manifest_id in {
            V18_MANIFEST_ID,
            V19_MANIFEST_ID,
            V20_MANIFEST_ID,
            V21_MANIFEST_ID,
            V22_MANIFEST_ID,
            V23_MANIFEST_ID,
            V24_MANIFEST_ID,
            V25_MANIFEST_ID,
            V26_MANIFEST_ID,
            V27_MANIFEST_ID,
            V28_MANIFEST_ID,
            V29_MANIFEST_ID,
            V30_MANIFEST_ID,
            V31_MANIFEST_ID,
            V32_MANIFEST_ID,
            V33_MANIFEST_ID,
            V34_MANIFEST_ID,
            V35_MANIFEST_ID,
            V36_MANIFEST_ID,
            V37_MANIFEST_ID,
            V38_MANIFEST_ID,
            V39_MANIFEST_ID,
            FROZEN_MANIFEST_ID,
        }:
            expected_responsive_fields.add(
                "precontainment_guarded_selection_contract"
            )
        if manifest_id in {
            V19_MANIFEST_ID,
            V20_MANIFEST_ID,
            V21_MANIFEST_ID,
            V22_MANIFEST_ID,
            V23_MANIFEST_ID,
            V24_MANIFEST_ID,
            V25_MANIFEST_ID,
            V26_MANIFEST_ID,
            V27_MANIFEST_ID,
            V28_MANIFEST_ID,
            V29_MANIFEST_ID,
            V30_MANIFEST_ID,
            V31_MANIFEST_ID,
            V32_MANIFEST_ID,
            V33_MANIFEST_ID,
            V34_MANIFEST_ID,
            V35_MANIFEST_ID,
            V36_MANIFEST_ID,
            V37_MANIFEST_ID,
            V38_MANIFEST_ID,
            V39_MANIFEST_ID,
            FROZEN_MANIFEST_ID,
        }:
            expected_responsive_fields.add(
                "future_tree_proposal_delivery_contract"
            )
        if manifest_id in {
            V20_MANIFEST_ID,
            V21_MANIFEST_ID,
            V22_MANIFEST_ID,
            V23_MANIFEST_ID,
            V24_MANIFEST_ID,
            V25_MANIFEST_ID,
            V26_MANIFEST_ID,
            V27_MANIFEST_ID,
            V28_MANIFEST_ID,
            V29_MANIFEST_ID,
            V30_MANIFEST_ID,
            V31_MANIFEST_ID,
            V32_MANIFEST_ID,
            V33_MANIFEST_ID,
            V34_MANIFEST_ID,
            V35_MANIFEST_ID,
            V36_MANIFEST_ID,
            V37_MANIFEST_ID,
            V38_MANIFEST_ID,
            V39_MANIFEST_ID,
            FROZEN_MANIFEST_ID,
        }:
            expected_responsive_fields.add(
                "source_bound_proposal_witness_contract"
            )
        if manifest_id in {
            V22_MANIFEST_ID,
            V23_MANIFEST_ID,
            V24_MANIFEST_ID,
            V25_MANIFEST_ID,
            V26_MANIFEST_ID,
            V27_MANIFEST_ID,
            V28_MANIFEST_ID,
            V29_MANIFEST_ID,
            V30_MANIFEST_ID,
            V31_MANIFEST_ID,
            V32_MANIFEST_ID,
            V33_MANIFEST_ID,
            V34_MANIFEST_ID,
            V35_MANIFEST_ID,
            V36_MANIFEST_ID,
            V37_MANIFEST_ID,
            V38_MANIFEST_ID,
            V39_MANIFEST_ID,
            FROZEN_MANIFEST_ID,
        }:
            expected_responsive_fields.add(
                "evidence_snapshot_selection_contract"
            )
        if manifest_id in {
            V24_MANIFEST_ID,
            V25_MANIFEST_ID,
            V26_MANIFEST_ID,
            V27_MANIFEST_ID,
            V28_MANIFEST_ID,
            V29_MANIFEST_ID,
            V30_MANIFEST_ID,
            V31_MANIFEST_ID,
            V32_MANIFEST_ID,
            V33_MANIFEST_ID,
            V34_MANIFEST_ID,
            V35_MANIFEST_ID,
            V36_MANIFEST_ID,
            V37_MANIFEST_ID,
            V38_MANIFEST_ID,
            V39_MANIFEST_ID,
            FROZEN_MANIFEST_ID,
        }:
            expected_responsive_fields.add(
                "minimum_primary_n31_f5_epoch1_internal_role_opportunities_per_actor_before_selection"
            )
        if manifest_id in {
            V25_MANIFEST_ID,
            V26_MANIFEST_ID,
            V27_MANIFEST_ID,
            V28_MANIFEST_ID,
            V29_MANIFEST_ID,
            V30_MANIFEST_ID,
            V31_MANIFEST_ID,
            V32_MANIFEST_ID,
            V33_MANIFEST_ID,
            V34_MANIFEST_ID,
            V35_MANIFEST_ID,
            V36_MANIFEST_ID,
            V37_MANIFEST_ID,
            V38_MANIFEST_ID,
            V39_MANIFEST_ID,
            FROZEN_MANIFEST_ID,
        }:
            expected_responsive_fields.add(
                "inherited_consensus_wait_exempt_placement_contract"
            )
            if (
                "inherited_consensus_wait_exempt_placement_contract"
                not in responsive
            ):
                _error(
                    "responsive-degradation inherited wait-exempt placement "
                    "contract is required"
                )
        if manifest_id in {
            V26_MANIFEST_ID,
            V27_MANIFEST_ID,
            V28_MANIFEST_ID,
            V29_MANIFEST_ID,
            V30_MANIFEST_ID,
            V31_MANIFEST_ID,
            V32_MANIFEST_ID,
            V33_MANIFEST_ID,
            V34_MANIFEST_ID,
            V35_MANIFEST_ID,
            V36_MANIFEST_ID,
            V37_MANIFEST_ID,
            V38_MANIFEST_ID,
            V39_MANIFEST_ID,
            FROZEN_MANIFEST_ID,
        }:
            expected_responsive_fields.add(
                "verified_response_duplicate_delivery_contract"
            )
            if "verified_response_duplicate_delivery_contract" not in responsive:
                _error(
                    "responsive-degradation verified-response duplicate delivery "
                    "contract is required"
                )
        if manifest_id in {
            V28_MANIFEST_ID,
            V29_MANIFEST_ID,
            V30_MANIFEST_ID,
            V31_MANIFEST_ID,
            V32_MANIFEST_ID,
            V33_MANIFEST_ID,
            V34_MANIFEST_ID,
            V35_MANIFEST_ID,
            V36_MANIFEST_ID,
            V37_MANIFEST_ID,
            V38_MANIFEST_ID,
            V39_MANIFEST_ID,
            FROZEN_MANIFEST_ID,
        }:
            expected_responsive_fields.update(
                {
                    "excluded_repair_smoke_observation_contract",
                    "excluded_repair_smoke_verified_response_duplicate_probe_contract",
                }
            )
            if (
                "excluded_repair_smoke_observation_contract" not in responsive
                or "excluded_repair_smoke_verified_response_duplicate_probe_contract"
                not in responsive
            ):
                _error(
                    "responsive-degradation excluded repair smoke contracts are required"
                )
        if manifest_id in {
            V36_MANIFEST_ID,
            V37_MANIFEST_ID,
            V38_MANIFEST_ID,
            V39_MANIFEST_ID,
            FROZEN_MANIFEST_ID,
        }:
            expected_responsive_fields.add(
                "post_final_convergence_unmatched_commit_evidence_contract"
            )
            if (
                "post_final_convergence_unmatched_commit_evidence_contract"
                not in responsive
            ):
                _error(
                    "responsive-degradation post-final-convergence unmatched "
                    "commit evidence contract is required"
                )
        if set(responsive) != expected_responsive_fields:
            _error("responsive-degradation contract fields are not frozen")
        if (
            responsive.get("actor_count_rule")
            != "derived_f_minus_hard_actor_count_v1"
            or responsive.get("actor_selection")
            != (
                "sha256_ranked_canonical_epoch0_reference_roots_"
                "excluding_commit_observer_v1"
            )
            or responsive.get("actor_selection_preimage")
            != (
                r"kauri.shape25.responsive-degraded.v1\0{membership_csv}"
                r"\0{q}\0{scientific_seed}\0{replica_id}"
            )
            or responsive.get("actor_selection_inputs")
            != [
                "membership",
                "derived_q",
                "canonical_epoch0_reference_roots_1_through_q_minus_1_v1",
                "replica_0_reserved_authoritative_commit_observer_v1",
                "scientific_block_seed",
            ]
            or responsive.get("observer_isolation")
            != "replica_0_reserved_authoritative_commit_observer_v1"
            or responsive.get("actor_schedule")
            != expected_responsive_schedule
            or _integer(
                responsive.get("omission_period"),
                "byzantine.responsive_degradation.omission_period",
                minimum=2,
            )
            != expected_responsive_period
        ):
            _error("responsive-degradation semantics differ from frozen profile")
        if manifest_id in {
            V12_MANIFEST_ID,
            V13_MANIFEST_ID,
            V14_MANIFEST_ID,
            V15_MANIFEST_ID,
            V16_MANIFEST_ID,
            V17_MANIFEST_ID,
            V18_MANIFEST_ID,
            V19_MANIFEST_ID,
            V20_MANIFEST_ID,
            V21_MANIFEST_ID,
            V22_MANIFEST_ID,
            V23_MANIFEST_ID,
            V24_MANIFEST_ID,
            V25_MANIFEST_ID,
            V26_MANIFEST_ID,
            V27_MANIFEST_ID,
            V28_MANIFEST_ID,
            V29_MANIFEST_ID,
            V30_MANIFEST_ID,
            V31_MANIFEST_ID,
            V32_MANIFEST_ID,
            V33_MANIFEST_ID,
            V34_MANIFEST_ID,
            V35_MANIFEST_ID,
            V36_MANIFEST_ID,
            V37_MANIFEST_ID,
            V38_MANIFEST_ID,
            V39_MANIFEST_ID,
            FROZEN_MANIFEST_ID,
        }:
            rate_eligible_timeout_counts = {
                attempt_count: max(
                    timeout_count
                    for timeout_count in range(attempt_count + 1)
                    if timeout_count * 1_000_000 // attempt_count
                    <= maximum_timeout_rate_ppm
                )
                for attempt_count in range(minimum_attempts, attempt_window + 1)
            }
            maximum_rate_eligible_timeout_count = max(
                rate_eligible_timeout_counts.values()
            )
            if (
                rate_eligible_timeout_counts.get(119) != 5
                or any(
                    rate_eligible_timeout_counts.get(attempt_count) != 6
                    for attempt_count in range(120, 129)
                )
                or maximum_rate_eligible_timeout_count != 6
                or trailing_timeout_streak
                != maximum_rate_eligible_timeout_count + 1
            ):
                _error(
                    "role-scoped trailing timeout streak must be one above the "
                    "maximum timeout count accepted by the rate gate"
                )
            worst_omissions_by_attempt_count: dict[int, int] = {}
            for attempt_count in range(minimum_attempts, attempt_window + 1):
                maximum_combined_omissions = max(
                    sum(
                        (stream_attempts + expected_responsive_period - 1)
                        // expected_responsive_period
                        for stream_attempts in (
                            internal_attempts,
                            attempt_count - internal_attempts,
                        )
                        if stream_attempts
                    )
                    for internal_attempts in range(attempt_count + 1)
                )
                worst_omissions_by_attempt_count[attempt_count] = (
                    maximum_combined_omissions
                )
                if (
                    maximum_combined_omissions * 1_000_000
                    > maximum_timeout_rate_ppm * attempt_count
                ):
                    _error(
                        "role-scoped responsive omission schedule can exceed the "
                        "frozen timeout ceiling"
                    )
            if (
                worst_omissions_by_attempt_count.get(60) != 3
                or worst_omissions_by_attempt_count.get(84) != 4
                or worst_omissions_by_attempt_count.get(125) != 5
            ):
                _error(
                    "role-scoped omission ceilings must bind at 3/60, 4/84, "
                    "and 5/125"
                )
        elif causal_measurement:
            cycle_lengths = {
                cycle
                for replica_count in (*replica_counts, 7)
                for cycle in (
                    replica_count - 1,
                    2 * ((replica_count - 1) // 3),
                )
            }
            if any(
                math.gcd(expected_responsive_period, cycle) != 1
                for cycle in cycle_lengths
            ):
                _error(
                    "causal responsive omission period must be coprime to every "
                    "campaign and excluded-smoke N-1/Q-1 role cycle"
                )
            if any(
                (
                    (attempt_count + expected_responsive_period - 1)
                    // expected_responsive_period
                )
                * 1_000_000
                > maximum_timeout_rate_ppm * attempt_count
                for attempt_count in range(minimum_attempts, attempt_window + 1)
            ):
                _error(
                    "causal responsive omission schedule can exceed the frozen "
                    "timeout ceiling in a selected attempt window"
                )
        if causal_measurement and (
            responsive.get("pending_attempt_retention")
            != RESPONSIVE_PENDING_ATTEMPT_RETENTION_V1
            or responsive.get("causal_timeout_linkage")
            != RESPONSIVE_CAUSAL_TIMEOUT_LINKAGE_V1
        ):
            _error("responsive-degradation causal measurement contract drifted")
        if explicit_causal_windows and (
            responsive.get("causal_timeout_provenance_window")
            != RESPONSIVE_CAUSAL_TIMEOUT_PROVENANCE_WINDOW_V1
            or responsive.get("causal_internal_witness_candidates")
            != RESPONSIVE_CAUSAL_INTERNAL_WITNESS_CANDIDATES_V1
            or responsive.get("causal_selection_linkage_window")
            != RESPONSIVE_CAUSAL_SELECTION_LINKAGE_WINDOW_V1
        ):
            _error("responsive-degradation causal linkage windows drifted")
        expected_marker_completeness_witness = (
            RESPONSIVE_MARKER_COMPLETENESS_WITNESS_V2
            if manifest_id
            in {
                V14_MANIFEST_ID,
                V15_MANIFEST_ID,
                V16_MANIFEST_ID,
                V17_MANIFEST_ID,
                V18_MANIFEST_ID,
                V19_MANIFEST_ID,
                V20_MANIFEST_ID,
                V21_MANIFEST_ID,
                V22_MANIFEST_ID,
                V23_MANIFEST_ID,
                V24_MANIFEST_ID,
                V25_MANIFEST_ID,
                V26_MANIFEST_ID,
                V27_MANIFEST_ID,
                V28_MANIFEST_ID,
                V29_MANIFEST_ID,
                V30_MANIFEST_ID,
                V31_MANIFEST_ID,
                V32_MANIFEST_ID,
                V33_MANIFEST_ID,
                V34_MANIFEST_ID,
                V35_MANIFEST_ID,
                V36_MANIFEST_ID,
                V37_MANIFEST_ID,
                V38_MANIFEST_ID,
                V39_MANIFEST_ID,
                FROZEN_MANIFEST_ID,
            }
            else RESPONSIVE_MARKER_COMPLETENESS_WITNESS_V1
        )
        expected_timeout_eligibility = (
            RESPONSIVE_CAUSAL_TIMEOUT_ELIGIBILITY_V3
            if manifest_id
            in {
                V21_MANIFEST_ID,
                V22_MANIFEST_ID,
                V23_MANIFEST_ID,
                V24_MANIFEST_ID,
                V25_MANIFEST_ID,
                V26_MANIFEST_ID,
                V27_MANIFEST_ID,
                V28_MANIFEST_ID,
                V29_MANIFEST_ID,
                V30_MANIFEST_ID,
                V31_MANIFEST_ID,
                V32_MANIFEST_ID,
                V33_MANIFEST_ID,
                V34_MANIFEST_ID,
                V35_MANIFEST_ID,
                V36_MANIFEST_ID,
                V37_MANIFEST_ID,
                V38_MANIFEST_ID,
                V39_MANIFEST_ID,
                FROZEN_MANIFEST_ID,
            }
            else (
                RESPONSIVE_CAUSAL_TIMEOUT_ELIGIBILITY_V2
                if manifest_id
                in {
                    V16_MANIFEST_ID,
                    V17_MANIFEST_ID,
                    V18_MANIFEST_ID,
                    V19_MANIFEST_ID,
                    V20_MANIFEST_ID,
                }
                else RESPONSIVE_CAUSAL_TIMEOUT_ELIGIBILITY_V1
            )
        )
        if explicit_causal_edge_eligibility and (
            responsive.get("marker_completeness_witness")
            != expected_marker_completeness_witness
            or responsive.get("causal_timeout_eligibility")
            != expected_timeout_eligibility
        ):
            _error("responsive-degradation causal edge eligibility drifted")
        expected_precontainment_gate = (
            PRECONTAINMENT_FAULT_COVERAGE_GATE_V1
            if manifest_id
            in {
                V15_MANIFEST_ID,
                V16_MANIFEST_ID,
                V17_MANIFEST_ID,
                V18_MANIFEST_ID,
                V19_MANIFEST_ID,
                V20_MANIFEST_ID,
                V21_MANIFEST_ID,
                V22_MANIFEST_ID,
                V23_MANIFEST_ID,
                V24_MANIFEST_ID,
                V25_MANIFEST_ID,
                V26_MANIFEST_ID,
                V27_MANIFEST_ID,
                V28_MANIFEST_ID,
                V29_MANIFEST_ID,
                V30_MANIFEST_ID,
                V31_MANIFEST_ID,
                V32_MANIFEST_ID,
                V33_MANIFEST_ID,
                V34_MANIFEST_ID,
                V35_MANIFEST_ID,
                V36_MANIFEST_ID,
                V37_MANIFEST_ID,
                V38_MANIFEST_ID,
                V39_MANIFEST_ID,
                FROZEN_MANIFEST_ID,
            }
            else None
        )
        if responsive.get("precontainment_fault_coverage_gate") != (
            expected_precontainment_gate
        ):
            _error("responsive-degradation precontainment coverage gate drifted")
        expected_precontainment_shape_contract = (
            PRECONTAINMENT_SHAPE_EVALUATION_CONTRACT_V1
            if manifest_id
            in {
                V17_MANIFEST_ID,
                V18_MANIFEST_ID,
                V19_MANIFEST_ID,
                V20_MANIFEST_ID,
                V21_MANIFEST_ID,
                V22_MANIFEST_ID,
                V23_MANIFEST_ID,
                V24_MANIFEST_ID,
                V25_MANIFEST_ID,
                V26_MANIFEST_ID,
                V27_MANIFEST_ID,
                V28_MANIFEST_ID,
                V29_MANIFEST_ID,
                V30_MANIFEST_ID,
                V31_MANIFEST_ID,
                V32_MANIFEST_ID,
                V33_MANIFEST_ID,
                V34_MANIFEST_ID,
                V35_MANIFEST_ID,
                V36_MANIFEST_ID,
                V37_MANIFEST_ID,
                V38_MANIFEST_ID,
                V39_MANIFEST_ID,
                FROZEN_MANIFEST_ID,
            }
            else None
        )
        if responsive.get("precontainment_shape_evaluation_contract") != (
            expected_precontainment_shape_contract
        ):
            _error(
                "responsive-degradation precontainment shape evaluation "
                "contract drifted"
            )
        expected_precontainment_guarded_selection_contract = (
            PRECONTAINMENT_GUARDED_SELECTION_CONTRACT_V1
            if manifest_id
            in {
                V18_MANIFEST_ID,
                V19_MANIFEST_ID,
                V20_MANIFEST_ID,
                V21_MANIFEST_ID,
                V22_MANIFEST_ID,
                V23_MANIFEST_ID,
                V24_MANIFEST_ID,
                V25_MANIFEST_ID,
                V26_MANIFEST_ID,
                V27_MANIFEST_ID,
                V28_MANIFEST_ID,
                V29_MANIFEST_ID,
                V30_MANIFEST_ID,
                V31_MANIFEST_ID,
                V32_MANIFEST_ID,
                V33_MANIFEST_ID,
                V34_MANIFEST_ID,
                V35_MANIFEST_ID,
                V36_MANIFEST_ID,
                V37_MANIFEST_ID,
                V38_MANIFEST_ID,
                V39_MANIFEST_ID,
                FROZEN_MANIFEST_ID,
            }
            else None
        )
        if responsive.get("precontainment_guarded_selection_contract") != (
            expected_precontainment_guarded_selection_contract
        ):
            _error(
                "responsive-degradation precontainment guarded selection "
                "contract drifted"
            )
        expected_future_tree_proposal_delivery_contract = (
            FUTURE_TREE_PROPOSAL_DELIVERY_CONTRACT_V2
            if manifest_id
            in {
                V23_MANIFEST_ID,
                V24_MANIFEST_ID,
                V25_MANIFEST_ID,
                V26_MANIFEST_ID,
                V27_MANIFEST_ID,
                V28_MANIFEST_ID,
                V29_MANIFEST_ID,
                V30_MANIFEST_ID,
                V31_MANIFEST_ID,
                V32_MANIFEST_ID,
                V33_MANIFEST_ID,
                V34_MANIFEST_ID,
                V35_MANIFEST_ID,
                V36_MANIFEST_ID,
                V37_MANIFEST_ID,
                V38_MANIFEST_ID,
                V39_MANIFEST_ID,
                FROZEN_MANIFEST_ID,
            }
            else (
                FUTURE_TREE_PROPOSAL_DELIVERY_CONTRACT_V1
                if manifest_id
                in {
                    V19_MANIFEST_ID,
                    V20_MANIFEST_ID,
                    V21_MANIFEST_ID,
                    V22_MANIFEST_ID,
                }
                else None
            )
        )
        if responsive.get("future_tree_proposal_delivery_contract") != (
            expected_future_tree_proposal_delivery_contract
        ):
            _error(
                "responsive-degradation future-tree proposal delivery "
                "contract drifted"
            )
        expected_source_bound_proposal_witness_contract = (
            SOURCE_BOUND_PROPOSAL_WITNESS_CONTRACT_V1
            if manifest_id
            in {
                V20_MANIFEST_ID,
                V21_MANIFEST_ID,
                V22_MANIFEST_ID,
                V23_MANIFEST_ID,
                V24_MANIFEST_ID,
                V25_MANIFEST_ID,
                V26_MANIFEST_ID,
                V27_MANIFEST_ID,
                V28_MANIFEST_ID,
                V29_MANIFEST_ID,
                V30_MANIFEST_ID,
                V31_MANIFEST_ID,
                V32_MANIFEST_ID,
                V33_MANIFEST_ID,
                V34_MANIFEST_ID,
                V35_MANIFEST_ID,
                V36_MANIFEST_ID,
                V37_MANIFEST_ID,
                V38_MANIFEST_ID,
                V39_MANIFEST_ID,
                FROZEN_MANIFEST_ID,
            }
            else None
        )
        if responsive.get("source_bound_proposal_witness_contract") != (
            expected_source_bound_proposal_witness_contract
        ):
            _error(
                "responsive-degradation source-bound proposal witness "
                "contract drifted"
            )
        expected_evidence_snapshot_selection_contract = (
            EVIDENCE_SNAPSHOT_SELECTION_CONTRACT_V1
            if manifest_id
            in {
                V22_MANIFEST_ID,
                V23_MANIFEST_ID,
                V24_MANIFEST_ID,
                V25_MANIFEST_ID,
                V26_MANIFEST_ID,
                V27_MANIFEST_ID,
                V28_MANIFEST_ID,
                V29_MANIFEST_ID,
                V30_MANIFEST_ID,
                V31_MANIFEST_ID,
                V32_MANIFEST_ID,
                V33_MANIFEST_ID,
                V34_MANIFEST_ID,
                V35_MANIFEST_ID,
                V36_MANIFEST_ID,
                V37_MANIFEST_ID,
                V38_MANIFEST_ID,
                V39_MANIFEST_ID,
                FROZEN_MANIFEST_ID,
            }
            else None
        )
        if responsive.get("evidence_snapshot_selection_contract") != (
            expected_evidence_snapshot_selection_contract
        ):
            _error(
                "responsive-degradation evidence snapshot selection "
                "contract drifted"
            )
        expected_inherited_wait_exempt_placement_contract = (
            INHERITED_CONSENSUS_WAIT_EXEMPT_PLACEMENT_CONTRACT_V1
            if manifest_id
            in {
                V25_MANIFEST_ID,
                V26_MANIFEST_ID,
                V27_MANIFEST_ID,
                V28_MANIFEST_ID,
                V29_MANIFEST_ID,
                V30_MANIFEST_ID,
                V31_MANIFEST_ID,
                V32_MANIFEST_ID,
                V33_MANIFEST_ID,
                V34_MANIFEST_ID,
                V35_MANIFEST_ID,
                V36_MANIFEST_ID,
                V37_MANIFEST_ID,
                V38_MANIFEST_ID,
                V39_MANIFEST_ID,
                FROZEN_MANIFEST_ID,
            }
            else None
        )
        if responsive.get(
            "inherited_consensus_wait_exempt_placement_contract"
        ) != expected_inherited_wait_exempt_placement_contract:
            _error(
                "responsive-degradation inherited wait-exempt placement "
                "contract drifted"
            )
        expected_verified_response_duplicate_delivery_contract = (
            VERIFIED_RESPONSE_DUPLICATE_DELIVERY_CONTRACT_V2
            if manifest_id
            in {
                V27_MANIFEST_ID,
                V28_MANIFEST_ID,
                V29_MANIFEST_ID,
                V30_MANIFEST_ID,
                V31_MANIFEST_ID,
                V32_MANIFEST_ID,
                V33_MANIFEST_ID,
                V34_MANIFEST_ID,
                V35_MANIFEST_ID,
                V36_MANIFEST_ID,
                V37_MANIFEST_ID,
                V38_MANIFEST_ID,
                V39_MANIFEST_ID,
                FROZEN_MANIFEST_ID,
            }
            else (
                VERIFIED_RESPONSE_DUPLICATE_DELIVERY_CONTRACT_V1
                if manifest_id == V26_MANIFEST_ID
                else None
            )
        )
        if responsive.get("verified_response_duplicate_delivery_contract") != (
            expected_verified_response_duplicate_delivery_contract
        ):
            _error(
                "responsive-degradation verified-response duplicate delivery "
                "contract drifted"
            )
        expected_excluded_repair_observation_contract = (
            EXCLUDED_REPAIR_SMOKE_OBSERVATION_CONTRACT_V5
            if manifest_id == FROZEN_MANIFEST_ID
            else (
                EXCLUDED_REPAIR_SMOKE_OBSERVATION_CONTRACT_V4
                if manifest_id == V39_MANIFEST_ID
                else (
                    EXCLUDED_REPAIR_SMOKE_OBSERVATION_CONTRACT_V3
                    if manifest_id in {V37_MANIFEST_ID, V38_MANIFEST_ID}
                    else (
                        EXCLUDED_REPAIR_SMOKE_OBSERVATION_CONTRACT_V2
                        if manifest_id
                        in {V34_MANIFEST_ID, V35_MANIFEST_ID, V36_MANIFEST_ID}
                        else (
                            EXCLUDED_REPAIR_SMOKE_OBSERVATION_CONTRACT_V1
                            if manifest_id
                            in {
                                V28_MANIFEST_ID,
                                V29_MANIFEST_ID,
                                V30_MANIFEST_ID,
                                V31_MANIFEST_ID,
                                V32_MANIFEST_ID,
                                V33_MANIFEST_ID,
                            }
                            else None
                        )
                    )
                )
            )
        )
        if responsive.get("excluded_repair_smoke_observation_contract") != (
            expected_excluded_repair_observation_contract
        ):
            _error("responsive-degradation excluded repair observation drifted")
        expected_excluded_repair_duplicate_probe_contract = (
            EXCLUDED_REPAIR_SMOKE_VERIFIED_RESPONSE_DUPLICATE_PROBE_CONTRACT_V1
            if manifest_id in {V28_MANIFEST_ID, V29_MANIFEST_ID, V30_MANIFEST_ID, V31_MANIFEST_ID, V32_MANIFEST_ID, V33_MANIFEST_ID, V34_MANIFEST_ID, V35_MANIFEST_ID, V36_MANIFEST_ID, V37_MANIFEST_ID, V38_MANIFEST_ID, V39_MANIFEST_ID, FROZEN_MANIFEST_ID}
            else None
        )
        if responsive.get(
            "excluded_repair_smoke_verified_response_duplicate_probe_contract"
        ) != expected_excluded_repair_duplicate_probe_contract:
            _error("responsive-degradation excluded repair duplicate probe drifted")
        expected_post_final_convergence_unmatched_commit_contract = (
            POST_FINAL_CONVERGENCE_UNMATCHED_COMMIT_EVIDENCE_CONTRACT_V2
            if manifest_id in {V38_MANIFEST_ID, V39_MANIFEST_ID, FROZEN_MANIFEST_ID}
            else (
                POST_FINAL_CONVERGENCE_UNMATCHED_COMMIT_EVIDENCE_CONTRACT_V1
                if manifest_id in {V36_MANIFEST_ID, V37_MANIFEST_ID}
                else None
            )
        )
        if responsive.get(
            "post_final_convergence_unmatched_commit_evidence_contract"
        ) != expected_post_final_convergence_unmatched_commit_contract:
            _error(
                "responsive-degradation post-final-convergence unmatched "
                "commit evidence contract drifted"
            )
        expected_minimum_primary_internal_opportunities = (
            82
            if manifest_id
            in {
                V24_MANIFEST_ID,
                V25_MANIFEST_ID,
                V26_MANIFEST_ID,
                V27_MANIFEST_ID,
                V28_MANIFEST_ID,
                V29_MANIFEST_ID,
                V30_MANIFEST_ID,
                V31_MANIFEST_ID,
                V32_MANIFEST_ID,
                V33_MANIFEST_ID,
                V34_MANIFEST_ID,
                V35_MANIFEST_ID,
                V36_MANIFEST_ID,
                V37_MANIFEST_ID,
                V38_MANIFEST_ID,
                V39_MANIFEST_ID,
                FROZEN_MANIFEST_ID,
            }
            else None
        )
        if responsive.get(
            "minimum_primary_n31_f5_epoch1_internal_role_opportunities_per_actor_before_selection"
        ) != expected_minimum_primary_internal_opportunities:
            _error(
                "responsive-degradation primary N31/f5 internal opportunity "
                "minimum drifted"
            )
        responsive_vectors = _array(
            responsive.get("actor_selection_vectors"),
            "byzantine.responsive_degradation.actor_selection_vectors",
        )
        if len(responsive_vectors) != len(expected_selection_vector_inputs):
            _error("responsive degradation requires three frozen ranking vectors")
        for index, (raw_vector, expected_inputs) in enumerate(
            zip(responsive_vectors, expected_selection_vector_inputs)
        ):
            vector = _mapping(
                raw_vector, f"responsive degradation selection vector {index}"
            )
            replica_count, q, scientific_seed = expected_inputs
            hard = derive_actor_ids(replica_count, q, 3, scientific_seed)
            expected_selected = derive_responsive_degraded_actor_ids(
                replica_count,
                q,
                hard,
                scientific_seed,
            )
            if vector != {
                "replica_count": replica_count,
                "q": q,
                "scientific_seed": scientific_seed,
                "selected_actor_ids": list(expected_selected),
            }:
                _error(
                    "responsive degradation vector disagrees with SHA-256 ranking"
                )
        if "max_omissions_per_proposal" in byzantine:
            _error("tiered maximum omissions must be derived per slot, not scalar")
        if (
            byzantine.get("max_omissions_per_proposal_rule")
            != "derived_f_per_slot_v1"
        ):
            _error("tiered maximum omissions must equal derived f per slot")
    else:
        if "responsive_degradation" in byzantine:
            _error("only tiered manifests may carry responsive-degradation semantics")
        if "max_omissions_per_proposal_rule" in byzantine:
            _error("legacy manifests must retain their scalar omission maximum")
        expected_max_omissions = 3 if persistent else 1
        if (
            _integer(
                byzantine.get("max_omissions_per_proposal"),
                "byzantine.max_omissions_per_proposal",
            )
            != expected_max_omissions
        ):
            _error(
                "maximum omissions per proposal must equal the frozen actor "
                "schedule cardinality"
            )

    global_worst_candidate_depth = max(
        tree_depth(replica_count, fanout)
        for replica_count in replica_counts
        for fanout in candidate_fanouts
    )
    timers = _mapping(document.get("timers"), "timers")
    if timers.get("depth_policy") != "global_worst_candidate_depth_linear_v1":
        _error("timer depth policy must use the frozen global-depth rule")
    timer_depth = _integer(
        timers.get("global_worst_candidate_depth"),
        "timers.global_worst_candidate_depth",
    )
    if timer_depth != global_worst_candidate_depth:
        _error("timer depth must equal the global worst candidate depth")
    aggregation_ms_per_depth = _integer(
        timers.get("aggregation_timeout_ms_per_depth"),
        "timers.aggregation_timeout_ms_per_depth",
    )
    leader_progress_ms_per_depth = _integer(
        timers.get("leader_progress_timeout_ms_per_depth"),
        "timers.leader_progress_timeout_ms_per_depth",
    )
    if aggregation_ms_per_depth * timer_depth != 500:
        _error("aggregation timer policy must preserve 500 ms at depth 4")
    if leader_progress_ms_per_depth * timer_depth != 20_000:
        _error("leader-progress timer policy must preserve 20000 ms at depth 4")
    _integer(
        timers.get("leader_activation_grace_ms"),
        "timers.leader_activation_grace_ms",
    )
    startup_timeout_s = _integer(
        timers.get("startup_timeout_s"), "timers.startup_timeout_s"
    )
    hard_timeout_s = _integer(timers.get("hard_timeout_s"), "timers.hard_timeout_s")
    observation_bound_rule = timers.get(
        "transition_observation_bound_rule", "phase_deadline_v1"
    )
    expected_observation_bound_rule = (
        "shared_slot_hard_deadline_until_manager_selection_v1"
        if persistent
        else "phase_deadline_v1"
    )
    if observation_bound_rule != expected_observation_bound_rule:
        _error(
            "transition observation must use the frozen manager-selection "
            "clock boundary"
        )
    convergence_deadline_s = _integer(
        timers.get("transition_convergence_deadline_s"),
        "timers.transition_convergence_deadline_s",
    )
    expected_convergence_deadline_s = (
        30 if manifest_id in {V33_MANIFEST_ID, V34_MANIFEST_ID, V35_MANIFEST_ID, V36_MANIFEST_ID, V37_MANIFEST_ID, V38_MANIFEST_ID, V39_MANIFEST_ID, FROZEN_MANIFEST_ID} else 20
    )
    if convergence_deadline_s != expected_convergence_deadline_s:
        _error(
            "transition convergence deadline must equal the exact frozen "
            f"{expected_convergence_deadline_s} second contract"
        )
    schedule_slack_s = _integer(
        timers.get("schedule_slack_s"),
        "timers.schedule_slack_s",
    )
    if schedule_slack_s < 30:
        _error("transition schedule requires at least 30 seconds of explicit slack")
    drain_margin_s = _integer(timers.get("drain_margin_s"), "timers.drain_margin_s")
    workload = _mapping(document.get("workload"), "workload")
    if (
        _integer(
            workload.get("piped_latency_ms"),
            "workload.piped_latency_ms",
        )
        != 1
    ):
        _error("piped latency must equal the frozen explicit 1 ms value")
    bucket_width_s = _integer(workload.get("bucket_width_s"), "workload.bucket_width_s")
    baseline_bucket_count = _integer(
        workload.get("baseline_bucket_count"),
        "workload.baseline_bucket_count",
    )
    fault_evidence_bucket_count = _integer(
        workload.get("fault_evidence_bucket_count"),
        "workload.fault_evidence_bucket_count",
    )
    epoch1_stable_bucket_count = _integer(
        workload.get("epoch1_stable_bucket_count"),
        "workload.epoch1_stable_bucket_count",
    )
    epoch2_stable_bucket_count = _integer(
        workload.get("epoch2_stable_bucket_count"),
        "workload.epoch2_stable_bucket_count",
    )
    expected_epoch1_preselection_residency_ms = (
        60_000
        if manifest_id
        in {
            V24_MANIFEST_ID,
            V25_MANIFEST_ID,
            V26_MANIFEST_ID,
            V27_MANIFEST_ID,
            V28_MANIFEST_ID,
            V29_MANIFEST_ID,
            V30_MANIFEST_ID,
            V31_MANIFEST_ID,
            V32_MANIFEST_ID,
            V33_MANIFEST_ID,
            V34_MANIFEST_ID,
            V35_MANIFEST_ID,
            V36_MANIFEST_ID,
            V37_MANIFEST_ID,
            V38_MANIFEST_ID,
            V39_MANIFEST_ID,
            FROZEN_MANIFEST_ID,
        }
        else None
    )
    if workload.get("epoch1_preselection_residency_ms") != (
        expected_epoch1_preselection_residency_ms
    ):
        _error("Epoch-1 preselection residency must equal the frozen contract")
    window = _mapping(byzantine.get("window"), "byzantine.window")
    start_after_prelaunch_anchor_s = _integer(
        window.get("start_after_prelaunch_anchor_s"),
        "byzantine.window.start_after_prelaunch_anchor_s",
    )
    expected_start_s = startup_timeout_s + (baseline_bucket_count * bucket_width_s)
    if start_after_prelaunch_anchor_s != expected_start_s:
        _error(
            "fault window start must equal startup timeout plus the clean "
            "baseline duration from the shared pre-launch anchor"
        )
    duration_s = _integer(window.get("duration_s"), "byzantine.window.duration_s")
    minimum_duration_s = (
        fault_evidence_bucket_count * bucket_width_s
        + 2 * convergence_deadline_s
        + (
            expected_epoch1_preselection_residency_ms // 1_000
            if expected_epoch1_preselection_residency_ms is not None
            else epoch1_stable_bucket_count * bucket_width_s
        )
        + epoch2_stable_bucket_count * bucket_width_s
        + drain_margin_s
        + schedule_slack_s
    )
    if duration_s < minimum_duration_s:
        _error(
            "fault window duration must cover fault evidence, both transition "
            "convergence deadlines, both stable windows, the drain margin, "
            "and explicit schedule slack"
        )
    if hard_timeout_s < (start_after_prelaunch_anchor_s + duration_s + drain_margin_s):
        _error(
            "hard timeout must cover the pre-launch offset, full fault "
            "window, and final drain margin"
        )

    constraints = _mapping(document.get("shape_constraints"), "shape_constraints")
    if constraints.get("epoch_fanout_policy") != "one_uniform_fanout_per_epoch":
        _error("mixed fanout within an epoch is forbidden")
    if constraints.get("pipeline_policy") != "fixed_first_slice":
        _error("adaptive pipeline selection is forbidden in the first slice")

    execution = _mapping(document.get("execution"), "execution")
    scheduling = _mapping(document.get("scheduling"), "scheduling")
    resources = _mapping(document.get("resources"), "resources")
    artifacts = _mapping(document.get("artifacts"), "artifacts")
    claim_scope = _mapping(document.get("claim_scope"), "claim_scope")
    expected_counterbalancing = (
        "stratified_greedy_minimum_position_imbalance_"
        "sha256_tiebreak_v2"
        if tiered
        else "greedy_minimum_position_imbalance_sha256_tiebreak_v1"
    )
    if (
        scheduling.get("execution_block_order") != "sha256_ranked_block_ids_v1"
        or scheduling.get("arm_counterbalancing")
        != expected_counterbalancing
        or type(scheduling.get("campaign_order_seed")) is not int
        or scheduling.get("campaign_order_seed") < 0
    ):
        _error("execution schedule must use the frozen counterbalancing rule")
    expected_claim_scope: dict[str, object] = {
        "other_cells": "parameter_coverage_only",
        "causal_inference": ("matched_repeated_blocks_within_prespecified_strata_only"),
        "headline_estimator": (
            "arithmetic_mean_of_five_matched_block_contrasts_v1"
        ),
        "uncertainty_interval": "two_sided_student_t_95_df4_v1",
        "directional_claim_rule": (
            "lower_95_ci_strictly_greater_than_zero_v1"
        ),
        "phase_sequence_endpoint": (
            "adaptive_arms_mean_tps_matched_by_block_v1"
        ),
        "placement_headline_replica_count": 31,
        "placement_headline_initial_fanout": 5,
        "placement_headline_block_count": 5,
        "shape_and_joint_headline_replica_count": 31,
        "shape_and_joint_headline_initial_fanout": 2,
        "shape_and_joint_headline_block_count": 5,
    }
    if tiered:
        breakthrough_structural_gate = (
            BREAKTHROUGH_STRUCTURAL_GATE_V4
            if manifest_id
            in {
                V14_MANIFEST_ID,
                V15_MANIFEST_ID,
                V16_MANIFEST_ID,
                V17_MANIFEST_ID,
                V18_MANIFEST_ID,
                V19_MANIFEST_ID,
                V20_MANIFEST_ID,
                V21_MANIFEST_ID,
                V22_MANIFEST_ID,
                V23_MANIFEST_ID,
                V24_MANIFEST_ID,
                V25_MANIFEST_ID,
                V26_MANIFEST_ID,
                V27_MANIFEST_ID,
                V28_MANIFEST_ID,
                V29_MANIFEST_ID,
                V30_MANIFEST_ID,
                V31_MANIFEST_ID,
                V32_MANIFEST_ID,
                V33_MANIFEST_ID,
                V34_MANIFEST_ID,
                V35_MANIFEST_ID,
                V36_MANIFEST_ID,
                V37_MANIFEST_ID,
                V38_MANIFEST_ID,
                V39_MANIFEST_ID,
                FROZEN_MANIFEST_ID,
            }
            else (
                "all_n31_f5_p_ps_slots_validate_tiered_markers_match_hard_"
                "and_every_41st_unique_non_root_responsive_degraded_omission_"
                "schedule_and_each_hard_actor_has_f_plus_1_distinct_exact_role_"
                "bound_timeout_reporters_and_at_least_one_internal_omit_aggregate_"
                "proof_and_each_responsive_degraded_actor_has_its_own_exact_"
                "reporter_local_epoch1_internal_omit_aggregate_cross_commit_"
                "witness_and_responsive_degraded_replicas_rank_below_every_fast_"
                "replica_and_epoch1_places_every_responsive_degraded_replica_as_a_"
                "root_and_exposes_each_in_an_internal_role_and_epoch2_roots_equal_"
                "top_q_fast_replicas_with_only_fast_replicas_in_root_and_internal_"
                "roles_and_all_f_worse_replicas_as_physical_leaves_and_only_hard_"
                "cohort_wait_exempt_v3"
                if causal_measurement
                else (
                    "all_n31_f5_p_ps_slots_validate_tiered_markers_match_hard_"
                    "and_every_32nd_unique_non_root_responsive_degraded_omission_"
                    "schedule_and_each_hard_actor_has_f_plus_1_distinct_exact_"
                    "role_bound_timeout_reporters_and_at_least_one_internal_"
                    "omit_aggregate_proof_and_responsive_degraded_replicas_rank_"
                    "below_every_fast_replica_and_epoch1_places_every_responsive_"
                    "degraded_replica_as_a_root_and_exposes_each_in_an_internal_"
                    "role_and_epoch2_roots_equal_top_q_fast_replicas_with_only_"
                    "fast_replicas_in_root_and_internal_roles_and_all_f_worse_"
                    "replicas_as_physical_leaves_and_only_hard_cohort_wait_exempt_v2"
                )
            )
        )
        expected_claim_scope.update(
            {
                "breakthrough_scope": (
                    "n31_f5_placement_arms_p_and_ps_five_matched_blocks_v1"
                ),
                "breakthrough_structural_gate": breakthrough_structural_gate,
                "breakthrough_structural_required_slot_count": 10,
                "breakthrough_realized_placement_rule": (
                    "for_each_p_and_ps_arm_all_5_of_5_n31_f5_blocks_have_"
                    "epoch1_to_epoch2_demoted_set_exactly_responsive_degraded_"
                    "cohort_and_promoted_set_exactly_canonical_non_reference_"
                    "root_pool_minus_hard_cohort_v2"
                ),
                "breakthrough_realized_placement_per_arm_requirement": 5,
                "breakthrough_primary_throughput_estimand": (
                    "d_b=0.5*[log((P_e2/P_e1)/(00_e2/00_e1))+"
                    "log((PS_e2/PS_e1)/(S_e2/S_e1))]"
                ),
                "breakthrough_throughput_claim_rule": (
                    "two_sided_student_t_95_df4_lower_log_bound_strictly_"
                    "greater_than_zero_and_at_least_4_of_5_block_effects_"
                    "strictly_greater_than_zero_and_absolute_fault_drop_"
                    "containment_recovery_and_pooled_optimization_each_two_"
                    "sided_student_t_95_df4_lower_tps_bound_strictly_greater_"
                    "than_zero_and_at_least_4_of_5_blocks_strictly_greater_"
                    "than_zero_and_p_and_ps_each_absolute_epoch2_minus_epoch1_"
                    "tps_strictly_greater_than_zero_in_at_least_4_of_5_blocks_v2"
                ),
                "breakthrough_positive_block_requirement": 4,
                "breakthrough_epoch1_baseline_ratio_role": (
                    "descriptive_only_no_noninferiority_threshold_v1"
                ),
                "breakthrough_absolute_phase_sequence_estimands": (
                    "fault_drop_b=0.5*((P_baseline-P_fault)+(PS_baseline-PS_fault));"
                    "containment_recovery_b=0.5*((P_e1-P_fault)+(PS_e1-PS_fault));"
                    "pooled_optimization_b=0.5*((P_e2-P_e1)+(PS_e2-PS_e1));"
                    "per_arm_optimization_b=(P_e2-P_e1,PS_e2-PS_e1)_v1"
                ),
                "breakthrough_phase_window_interpretation": (
                    "fixed_six_5_second_bucket_windows_with_epoch_windows_"
                    "anchored_at_first_authoritative_post_activation_commit_"
                    "and_a_common_q_commit_required_within_each_window_not_"
                    "steady_state_v1"
                ),
                "breakthrough_pre_epoch1_placebo_estimand": (
                    "pP_b=log((P_fault/P_baseline)/(00_fault/00_baseline));"
                    "pPS_b=log((PS_fault/PS_baseline)/(S_fault/S_baseline))"
                ),
                "breakthrough_placebo_equivalence_rule": (
                    "both_component_two_one_sided_5_percent_tests_df4_90_cis_"
                    "strictly_within_plus_minus_log_1p10_v2"
                ),
                "breakthrough_placebo_equivalence_margin_log": (
                    0.09531017980432493
                ),
            }
        )
        if causal_measurement:
            expected_claim_scope.update(
                {
                    "breakthrough_secondary_scope": (
                        "n31_f2_placement_arms_p_and_ps_five_matched_blocks_"
                        "prespecified_secondary_v1"
                    ),
                    "breakthrough_secondary_status_rule": (
                        "supported_only_if_primary_f5_supported_and_all_"
                        "secondary_f2_gates_pass_not_supported_if_primary_f5_"
                        "supported_and_any_secondary_gate_fails_descriptive_"
                        "only_if_primary_f5_not_supported_v1"
                    ),
                    "breakthrough_secondary_structural_gate": (
                        "all_n31_f2_p_ps_slots_validate_existing_tiered_full_"
                        "hierarchy_and_exact_epoch1_to_epoch2_degraded_demotion_"
                        "and_fast_promotion_proofs_v1"
                    ),
                    "breakthrough_secondary_structural_required_slot_count": 10,
                    "breakthrough_secondary_realized_placement_rule": (
                        "for_each_p_and_ps_arm_all_5_of_5_n31_f2_blocks_have_"
                        "epoch1_to_epoch2_demoted_set_exactly_responsive_"
                        "degraded_cohort_and_promoted_set_exactly_canonical_"
                        "non_reference_root_pool_minus_hard_cohort_v1"
                    ),
                    "breakthrough_secondary_realized_placement_per_arm_requirement": 5,
                    "breakthrough_secondary_throughput_estimand": (
                        "d2_b=0.5*[log((P_e2/P_e1)/(00_e2/00_e1))+"
                        "log((PS_e2/PS_e1)/(S_e2/S_e1))]"
                    ),
                    "breakthrough_secondary_throughput_claim_rule": (
                        "two_sided_student_t_95_df4_lower_log_bound_strictly_"
                        "greater_than_zero_and_at_least_4_of_5_block_effects_"
                        "strictly_greater_than_zero_v1"
                    ),
                    "breakthrough_secondary_positive_block_requirement": 4,
                    "breakthrough_secondary_pre_epoch1_placebo_estimand": (
                        "pP2_b=log((P_fault/P_baseline)/(00_fault/00_baseline));"
                        "pPS2_b=log((PS_fault/PS_baseline)/(S_fault/S_baseline))"
                    ),
                    "breakthrough_secondary_placebo_equivalence_rule": (
                        "both_component_two_one_sided_5_percent_tests_df4_90_"
                        "cis_strictly_within_plus_minus_log_1p10_v2"
                    ),
                    "breakthrough_secondary_placebo_equivalence_margin_log": (
                        0.09531017980432493
                    ),
                }
            )
    if claim_scope != expected_claim_scope:
        _error(
            "claim scope must preserve the exact frozen estimands and strata"
        )
    if (
        document.get("execution_authorized") is not False
        or document.get("execution_receipt_required") is not True
    ):
        _error("planning manifest must require a later execution receipt")
    if (
        execution.get("mode") != "fixed_sequential"
        or execution.get("automatic_retries") != 0
        or execution.get("replacement_policy") != "none"
        or execution.get("outcome_dependent_order") is not False
        or execution.get("cleanup_contract")
        != (
            EXECUTION_CLEANUP_CONTRACT_V1
            if manifest_id
            in {
                V14_MANIFEST_ID,
                V15_MANIFEST_ID,
                V16_MANIFEST_ID,
                V17_MANIFEST_ID,
                V18_MANIFEST_ID,
                V19_MANIFEST_ID,
                V20_MANIFEST_ID,
                V21_MANIFEST_ID,
                V22_MANIFEST_ID,
                V23_MANIFEST_ID,
                V24_MANIFEST_ID,
                V25_MANIFEST_ID,
                V26_MANIFEST_ID,
                V27_MANIFEST_ID,
                V28_MANIFEST_ID,
                V29_MANIFEST_ID,
                V30_MANIFEST_ID,
                V31_MANIFEST_ID,
                V32_MANIFEST_ID,
                V33_MANIFEST_ID,
                V34_MANIFEST_ID,
                V35_MANIFEST_ID,
                V36_MANIFEST_ID,
                V37_MANIFEST_ID,
                V38_MANIFEST_ID,
                V39_MANIFEST_ID,
                FROZEN_MANIFEST_ID,
            }
            else None
        )
        or ("cleanup_contract" in execution)
        is not (
            manifest_id
            in {
                V14_MANIFEST_ID,
                V15_MANIFEST_ID,
                V16_MANIFEST_ID,
                V17_MANIFEST_ID,
                V18_MANIFEST_ID,
                V19_MANIFEST_ID,
                V20_MANIFEST_ID,
                V21_MANIFEST_ID,
                V22_MANIFEST_ID,
                V23_MANIFEST_ID,
                V24_MANIFEST_ID,
                V25_MANIFEST_ID,
                V26_MANIFEST_ID,
                V27_MANIFEST_ID,
                V28_MANIFEST_ID,
                V29_MANIFEST_ID,
                V30_MANIFEST_ID,
                V31_MANIFEST_ID,
                V32_MANIFEST_ID,
                V33_MANIFEST_ID,
                V34_MANIFEST_ID,
                V35_MANIFEST_ID,
                V36_MANIFEST_ID,
                V37_MANIFEST_ID,
                V38_MANIFEST_ID,
                V39_MANIFEST_ID,
                FROZEN_MANIFEST_ID,
            }
        )
        or scheduling.get("outcome_dependent_order") is not False
        or resources.get("max_parallel_slots") != 1
    ):
        _error("execution must be sequential, no-retry, and no-replacement")
    if artifacts.get("preserve_outcomes") != [
        "NOT_STARTED",
        "PASS",
        "FAIL",
        "INCOMPLETE",
    ]:
        _error("all terminal and unstarted slot outcomes must be preserved")
    evidence_snapshot_format = artifacts.get(
        "evidence_snapshot_format", "full_prefix_v1"
    )
    expected_snapshot_format = (
        "digest_commitment_v2" if compact_snapshot else "full_prefix_v1"
    )
    if evidence_snapshot_format != expected_snapshot_format:
        _error(
            f"{manifest_id} requires evidence snapshot format "
            f"{expected_snapshot_format}"
        )
    if compact_snapshot != ("evidence_snapshot_format" in artifacts):
        _error(
            "only shape-placement-factorial-v3 through v40 may carry the compact "
            "snapshot format field"
        )

    semantic_sha256 = hashlib.sha256(_canonical_json_bytes(document)).hexdigest()
    expected_semantic_sha256 = {
        LEGACY_MANIFEST_ID: LEGACY_SEMANTIC_SHA256,
        V2_MANIFEST_ID: V2_SEMANTIC_SHA256,
        V3_MANIFEST_ID: V3_SEMANTIC_SHA256,
        V4_MANIFEST_ID: V4_SEMANTIC_SHA256,
        V5_MANIFEST_ID: V5_SEMANTIC_SHA256,
        V6_MANIFEST_ID: V6_SEMANTIC_SHA256,
        V7_MANIFEST_ID: V7_SEMANTIC_SHA256,
        V8_MANIFEST_ID: V8_SEMANTIC_SHA256,
        V9_MANIFEST_ID: V9_SEMANTIC_SHA256,
        V10_MANIFEST_ID: V10_SEMANTIC_SHA256,
        V11_MANIFEST_ID: V11_SEMANTIC_SHA256,
        V12_MANIFEST_ID: V12_SEMANTIC_SHA256,
        V13_MANIFEST_ID: V13_SEMANTIC_SHA256,
        V14_MANIFEST_ID: V14_SEMANTIC_SHA256,
        V15_MANIFEST_ID: V15_SEMANTIC_SHA256,
        V16_MANIFEST_ID: V16_SEMANTIC_SHA256,
        V17_MANIFEST_ID: V17_SEMANTIC_SHA256,
        V18_MANIFEST_ID: V18_SEMANTIC_SHA256,
        V19_MANIFEST_ID: V19_SEMANTIC_SHA256,
        V20_MANIFEST_ID: V20_SEMANTIC_SHA256,
        V21_MANIFEST_ID: V21_SEMANTIC_SHA256,
        V22_MANIFEST_ID: V22_SEMANTIC_SHA256,
        V23_MANIFEST_ID: V23_SEMANTIC_SHA256,
        V24_MANIFEST_ID: V24_SEMANTIC_SHA256,
        V25_MANIFEST_ID: V25_SEMANTIC_SHA256,
        V26_MANIFEST_ID: V26_SEMANTIC_SHA256,
        V27_MANIFEST_ID: V27_SEMANTIC_SHA256,
        V28_MANIFEST_ID: V28_SEMANTIC_SHA256,
        V29_MANIFEST_ID: V29_SEMANTIC_SHA256,
        V30_MANIFEST_ID: V30_SEMANTIC_SHA256,
        V31_MANIFEST_ID: V31_SEMANTIC_SHA256,
        V32_MANIFEST_ID: V32_SEMANTIC_SHA256,
        V33_MANIFEST_ID: V33_SEMANTIC_SHA256,
        V34_MANIFEST_ID: V34_SEMANTIC_SHA256,
        V35_MANIFEST_ID: V35_SEMANTIC_SHA256,
        V36_MANIFEST_ID: V36_SEMANTIC_SHA256,
        V37_MANIFEST_ID: V37_SEMANTIC_SHA256,
        V38_MANIFEST_ID: V38_SEMANTIC_SHA256,
        V39_MANIFEST_ID: V39_SEMANTIC_SHA256,
        FROZEN_MANIFEST_ID: FROZEN_SEMANTIC_SHA256,
    }[manifest_id]
    if semantic_sha256 != expected_semantic_sha256:
        _error("manifest differs from the frozen semantic contract")


def tree_depth(replica_count: int, fanout: int) -> int:
    """Return the minimum edge depth of a uniform-fanout tree covering N."""

    n = _integer(replica_count, "replica count")
    width = _integer(fanout, "fanout")
    if width > 255:
        _error("fanout must be 1..255")
    if n == 1:
        return 0
    depth, covered, level_width = 0, 1, 1
    while covered < n:
        level_width *= width
        covered += level_width
        depth += 1
    return depth


def epoch0_internal_tree_ids(
    replica_count: int,
    *,
    initial_fanout: int,
    replica_id: int,
) -> tuple[int, ...]:
    """Return active epoch-0 tree IDs where a member is physically internal.

    Native ``tree-generation = default`` rotates canonical membership left by
    the tree ID, then constructs a breadth-first uniform-fanout tree.  Epoch 0
    actively rotates all N trees; the Q-tree prefix is only a shape-scoring
    reference set.
    """

    n = _integer(replica_count, "replica count")
    fanout = _integer(initial_fanout, "initial fanout")
    member = _integer(replica_id, "replica ID", minimum=0)
    if member >= n:
        _error("epoch-0 tree role input is outside membership")
    return tuple(
        tree_id
        for tree_id in range(n)
        if (position := (member - tree_id) % n) != 0 and position * fanout + 1 < n
    )


def epoch0_distinct_parent_ids(
    replica_count: int,
    *,
    initial_fanout: int,
    replica_id: int,
) -> tuple[int, ...]:
    """Return every distinct physical parent available across epoch-0 trees."""

    n = _integer(replica_count, "replica count")
    fanout = _integer(initial_fanout, "initial fanout")
    member = _integer(replica_id, "replica ID", minimum=0)
    if member >= n:
        _error("epoch-0 parent input is outside membership")
    parents = {
        (tree_id + ((position - 1) // fanout)) % n
        for tree_id in range(n)
        if (position := (member - tree_id) % n) != 0
    }
    return tuple(sorted(parents))


def derive_consensus_shape(
    replica_count: int,
    *,
    initial_fanout: int,
    candidate_fanouts: Sequence[int],
) -> ConsensusShape:
    """Derive f, Q, tree count, and uniform tree depths from N."""

    n = _integer(replica_count, "N")
    if (n - 1) % 3:
        _error("replica count must satisfy N = 3f + 1")
    initial = _fanouts([initial_fanout], "initial fanouts")[0]
    candidates = _fanouts(list(candidate_fanouts), "candidate fanouts")
    f = (n - 1) // 3
    q = 2 * f + 1
    if q > 255:
        _error("derived Q/tree count exceeds the uint8 runtime bound")
    candidate_depths = tuple((fanout, tree_depth(n, fanout)) for fanout in candidates)
    return ConsensusShape(
        replica_count=n,
        f=f,
        q=q,
        tree_count=q,
        initial_fanout=initial,
        initial_depth=tree_depth(n, initial),
        candidate_depths=candidate_depths,
        worst_candidate_depth=max(depth for _, depth in candidate_depths),
    )


def parse_manifest_bytes(payload: bytes) -> FrozenFactorialManifest:
    """Validate semantics while retaining the exact source-byte digest."""

    document = _parse_json(payload)
    _reject_authority_fields(document)
    _validate_frozen_semantics(document)

    byzantine = document["byzantine"]
    window = byzantine["window"]
    resources = document["resources"]
    ports = resources["port_bases"]
    repetitions = document["repetitions"]
    scheduling = document["scheduling"]
    artifacts = document["artifacts"]
    execution = document["execution"]
    return FrozenFactorialManifest(
        manifest_id=document["manifest_id"],
        manifest_sha256=hashlib.sha256(payload).hexdigest(),
        replica_counts=tuple(document["replica_counts"]),
        initial_fanouts=tuple(document["initial_fanouts"]),
        candidate_fanouts=tuple(document["candidate_fanouts"]),
        pipeline_stretch=document["pipeline_stretch"],
        epoch_fanout_policy=document["shape_constraints"]["epoch_fanout_policy"],
        pipeline_policy=document["shape_constraints"]["pipeline_policy"],
        arms=tuple(FactorialArm(**arm) for arm in document["arms"]),
        default_blocks_per_cell=repetitions["default_blocks_per_cell"],
        repetition_overrides=tuple(
            (item["replica_count"], item["initial_fanout"], item["blocks"])
            for item in repetitions["cell_overrides"]
        ),
        byzantine=ByzantineContract(
            mode=byzantine["mode"],
            actor_count=byzantine["actor_count"],
            actor_count_rule=byzantine["actor_count_rule"],
            actor_selection=byzantine["actor_selection"],
            actor_selection_preimage=byzantine["actor_selection_preimage"],
            actor_selection_inputs=tuple(byzantine["actor_selection_inputs"]),
            actor_selection_vectors=tuple(
                ActorSelectionVector(
                    replica_count=vector["replica_count"],
                    q=vector["q"],
                    scientific_seed=vector["scientific_seed"],
                    selected_actor_ids=tuple(vector["selected_actor_ids"]),
                )
                for vector in byzantine["actor_selection_vectors"]
            ),
            actor_schedule=byzantine.get(
                "actor_schedule", byzantine.get("actor_rotation")
            ),
            actor_rotation_vectors=tuple(
                ActorRotationVector(
                    epoch_number=vector["epoch_number"],
                    tree_id=vector["tree_id"],
                    epoch_digest=vector["epoch_digest"],
                    block_hash=vector["block_hash"],
                    sorted_actor_ids=tuple(vector["sorted_actor_ids"]),
                    fnv1a64=vector["fnv1a64"],
                    selected_actor=vector["selected_actor"],
                )
                for vector in byzantine.get("actor_rotation_vectors", [])
            ),
            maximum_rotating_contexts=byzantine["maximum_rotating_contexts"],
            start_after_prelaunch_anchor_s=window["start_after_prelaunch_anchor_s"],
            duration_s=window["duration_s"],
            max_omissions_per_proposal=byzantine.get(
                "max_omissions_per_proposal"
            ),
            actions=ByzantineActions(**byzantine["actions"]),
            responsive_degradation=(
                ResponsiveDegradationContract(
                    actor_count_rule=(
                        byzantine["responsive_degradation"]["actor_count_rule"]
                    ),
                    actor_selection=(
                        byzantine["responsive_degradation"]["actor_selection"]
                    ),
                    actor_selection_preimage=(
                        byzantine["responsive_degradation"][
                            "actor_selection_preimage"
                        ]
                    ),
                    actor_selection_inputs=tuple(
                        byzantine["responsive_degradation"][
                            "actor_selection_inputs"
                        ]
                    ),
                    actor_selection_vectors=tuple(
                        ActorSelectionVector(
                            replica_count=vector["replica_count"],
                            q=vector["q"],
                            scientific_seed=vector["scientific_seed"],
                            selected_actor_ids=tuple(vector["selected_actor_ids"]),
                        )
                        for vector in byzantine["responsive_degradation"][
                            "actor_selection_vectors"
                        ]
                    ),
                    observer_isolation=(
                        byzantine["responsive_degradation"]["observer_isolation"]
                    ),
                    actor_schedule=(
                        byzantine["responsive_degradation"]["actor_schedule"]
                    ),
                    omission_period=(
                        byzantine["responsive_degradation"]["omission_period"]
                    ),
                    pending_attempt_retention=(
                        byzantine["responsive_degradation"].get(
                            "pending_attempt_retention"
                        )
                    ),
                    causal_timeout_linkage=(
                        byzantine["responsive_degradation"].get(
                            "causal_timeout_linkage"
                        )
                    ),
                    causal_timeout_provenance_window=(
                        byzantine["responsive_degradation"].get(
                            "causal_timeout_provenance_window"
                        )
                    ),
                    causal_internal_witness_candidates=(
                        byzantine["responsive_degradation"].get(
                            "causal_internal_witness_candidates"
                        )
                    ),
                    causal_selection_linkage_window=(
                        byzantine["responsive_degradation"].get(
                            "causal_selection_linkage_window"
                        )
                    ),
                    marker_completeness_witness=(
                        byzantine["responsive_degradation"].get(
                            "marker_completeness_witness"
                        )
                    ),
                    causal_timeout_eligibility=(
                        byzantine["responsive_degradation"].get(
                            "causal_timeout_eligibility"
                        )
                    ),
                    precontainment_fault_coverage_gate=(
                        byzantine["responsive_degradation"].get(
                            "precontainment_fault_coverage_gate"
                        )
                    ),
                    precontainment_shape_evaluation_contract=(
                        byzantine["responsive_degradation"].get(
                            "precontainment_shape_evaluation_contract"
                        )
                    ),
                    precontainment_guarded_selection_contract=(
                        byzantine["responsive_degradation"].get(
                            "precontainment_guarded_selection_contract"
                        )
                    ),
                    future_tree_proposal_delivery_contract=(
                        byzantine["responsive_degradation"].get(
                            "future_tree_proposal_delivery_contract"
                        )
                    ),
                    source_bound_proposal_witness_contract=(
                        byzantine["responsive_degradation"].get(
                            "source_bound_proposal_witness_contract"
                        )
                    ),
                    evidence_snapshot_selection_contract=(
                        byzantine["responsive_degradation"].get(
                            "evidence_snapshot_selection_contract"
                        )
                    ),
                    inherited_consensus_wait_exempt_placement_contract=(
                        byzantine["responsive_degradation"].get(
                            "inherited_consensus_wait_exempt_placement_contract"
                        )
                    ),
                    verified_response_duplicate_delivery_contract=(
                        byzantine["responsive_degradation"].get(
                            "verified_response_duplicate_delivery_contract"
                        )
                    ),
                    excluded_repair_smoke_observation_contract=(
                        byzantine["responsive_degradation"].get(
                            "excluded_repair_smoke_observation_contract"
                        )
                    ),
                    excluded_repair_smoke_verified_response_duplicate_probe_contract=(
                        byzantine["responsive_degradation"].get(
                            "excluded_repair_smoke_verified_response_duplicate_probe_contract"
                        )
                    ),
                    post_final_convergence_unmatched_commit_evidence_contract=(
                        byzantine["responsive_degradation"].get(
                            "post_final_convergence_unmatched_commit_evidence_contract"
                        )
                    ),
                    minimum_primary_n31_f5_epoch1_internal_role_opportunities_per_actor_before_selection=(
                        byzantine["responsive_degradation"].get(
                            "minimum_primary_n31_f5_epoch1_internal_role_opportunities_per_actor_before_selection"
                        )
                    ),
                )
                if "responsive_degradation" in byzantine
                else None
            ),
            max_omissions_per_proposal_rule=byzantine.get(
                "max_omissions_per_proposal_rule"
            ),
        ),
        workload=WorkloadContract(**document["workload"]),
        responsiveness_policy=ResponsivenessPolicyContract(
            **document["responsiveness_policy"]
        ),
        common_timers=CommonTimers(**document["timers"]),
        scientific_seed_base=scheduling["scientific_seed_base"],
        scientific_seed_rule=scheduling["scientific_seed_rule"],
        slot_order=scheduling["slot_order"],
        slot_nonce_rule=scheduling["slot_nonce_rule"],
        campaign_order_seed=scheduling["campaign_order_seed"],
        execution_block_order=scheduling["execution_block_order"],
        arm_counterbalancing=scheduling["arm_counterbalancing"],
        scheduling_outcome_dependent_order=scheduling["outcome_dependent_order"],
        claim_scope=ClaimScope(**document["claim_scope"]),
        results_root=artifacts["results_root"],
        canonical_plan_filename=artifacts["canonical_plan_filename"],
        one_directory_per_slot=artifacts["one_directory_per_slot"],
        preserve_outcomes=tuple(artifacts["preserve_outcomes"]),
        evidence_snapshot_format=artifacts.get(
            "evidence_snapshot_format", "full_prefix_v1"
        ),
        resources=ResourceContract(
            minimum_free_bytes=resources["minimum_free_bytes"],
            minimum_free_bytes_interpretation=(
                resources["minimum_free_bytes_interpretation"]
            ),
            max_parallel_slots=resources["max_parallel_slots"],
            peer_port_base=ports["peer"],
            client_port_base=ports["client"],
            manager_port_base=ports["manager"],
            slot_port_stride=resources["slot_port_stride"],
        ),
        execution_mode=execution["mode"],
        automatic_retries=execution["automatic_retries"],
        replacement_policy=execution["replacement_policy"],
        execution_outcome_dependent_order=execution["outcome_dependent_order"],
        execution_authorized=document["execution_authorized"],
        execution_receipt_required=document["execution_receipt_required"],
        cleanup_contract=execution.get("cleanup_contract"),
    )


def load_frozen_manifest_bytes(payload: bytes) -> FrozenFactorialManifest:
    manifest = parse_manifest_bytes(payload)
    expected_sha256 = {
        LEGACY_MANIFEST_ID: LEGACY_MANIFEST_SHA256,
        V2_MANIFEST_ID: V2_MANIFEST_SHA256,
        V3_MANIFEST_ID: V3_MANIFEST_SHA256,
        V4_MANIFEST_ID: V4_MANIFEST_SHA256,
        V5_MANIFEST_ID: V5_MANIFEST_SHA256,
        V6_MANIFEST_ID: V6_MANIFEST_SHA256,
        V7_MANIFEST_ID: V7_MANIFEST_SHA256,
        V8_MANIFEST_ID: V8_MANIFEST_SHA256,
        V9_MANIFEST_ID: V9_MANIFEST_SHA256,
        V10_MANIFEST_ID: V10_MANIFEST_SHA256,
        V11_MANIFEST_ID: V11_MANIFEST_SHA256,
        V12_MANIFEST_ID: V12_MANIFEST_SHA256,
        V13_MANIFEST_ID: V13_MANIFEST_SHA256,
        V14_MANIFEST_ID: V14_MANIFEST_SHA256,
        V15_MANIFEST_ID: V15_MANIFEST_SHA256,
        V16_MANIFEST_ID: V16_MANIFEST_SHA256,
        V17_MANIFEST_ID: V17_MANIFEST_SHA256,
        V18_MANIFEST_ID: V18_MANIFEST_SHA256,
        V19_MANIFEST_ID: V19_MANIFEST_SHA256,
        V20_MANIFEST_ID: V20_MANIFEST_SHA256,
        V21_MANIFEST_ID: V21_MANIFEST_SHA256,
        V22_MANIFEST_ID: V22_MANIFEST_SHA256,
        V23_MANIFEST_ID: V23_MANIFEST_SHA256,
        V24_MANIFEST_ID: V24_MANIFEST_SHA256,
        V25_MANIFEST_ID: V25_MANIFEST_SHA256,
        V26_MANIFEST_ID: V26_MANIFEST_SHA256,
        V27_MANIFEST_ID: V27_MANIFEST_SHA256,
        V28_MANIFEST_ID: V28_MANIFEST_SHA256,
        V29_MANIFEST_ID: V29_MANIFEST_SHA256,
        V30_MANIFEST_ID: V30_MANIFEST_SHA256,
        V31_MANIFEST_ID: V31_MANIFEST_SHA256,
        V32_MANIFEST_ID: V32_MANIFEST_SHA256,
        V33_MANIFEST_ID: V33_MANIFEST_SHA256,
        V34_MANIFEST_ID: V34_MANIFEST_SHA256,
        V35_MANIFEST_ID: V35_MANIFEST_SHA256,
        V36_MANIFEST_ID: V36_MANIFEST_SHA256,
        V37_MANIFEST_ID: V37_MANIFEST_SHA256,
        V38_MANIFEST_ID: V38_MANIFEST_SHA256,
        V39_MANIFEST_ID: V39_MANIFEST_SHA256,
        FROZEN_MANIFEST_ID: FROZEN_MANIFEST_SHA256,
    }.get(manifest.manifest_id)
    if manifest.manifest_sha256 != expected_sha256:
        _error("input does not match the exact frozen manifest bytes")
    return manifest


def load_frozen_manifest(path: Path) -> FrozenFactorialManifest:
    try:
        return load_frozen_manifest_bytes(path.read_bytes())
    except OSError as error:
        raise FactorialManifestError(f"cannot read manifest: {path}") from error


def _frozen_block_ids() -> tuple[str, ...]:
    result: list[str] = []
    for replica_count in EXPECTED_REPLICA_COUNTS:
        for initial_fanout in EXPECTED_INITIAL_FANOUTS:
            blocks = 5 if replica_count == 31 and initial_fanout in (2, 5) else 1
            result.extend(
                f"n{replica_count}-f{initial_fanout}-b{index:02d}"
                for index in range(1, blocks + 1)
            )
    return tuple(result)


def derive_slot_nonce(block_id: str, arm_code: str) -> int:
    """Derive a non-scientific identity/port/path nonce from block and arm."""

    try:
        block_ordinal = _frozen_block_ids().index(block_id)
        arm_ordinal = EXPECTED_ARM_CODES.index(arm_code)
    except ValueError as error:
        raise FactorialManifestError(
            f"unknown frozen block/arm pair: {block_id}/{arm_code}"
        ) from error
    return block_ordinal * len(EXPECTED_ARM_CODES) + arm_ordinal


def _derive_balanced_execution_schedule(
    block_ids: Sequence[str],
    arm_codes: Sequence[str],
    campaign_order_seed: int,
    *,
    stratum_key: Callable[[str], str] | None,
) -> tuple[BlockExecutionSchedule, ...]:
    blocks = tuple(block_ids)
    arms = tuple(arm_codes)
    seed = _integer(campaign_order_seed, "campaign order seed", minimum=0)
    if not blocks or len(set(blocks)) != len(blocks):
        _error("execution schedule block IDs must be non-empty and unique")
    if not arms or len(set(arms)) != len(arms):
        _error("execution schedule arm codes must be non-empty and unique")

    def block_rank(block_id: str) -> tuple[bytes, str]:
        payload = f"{seed}\x00{block_id}".encode("ascii")
        return hashlib.sha256(payload).digest(), block_id

    ranked_blocks = tuple(sorted(blocks, key=block_rank))
    counts_by_stratum: dict[str | None, dict[tuple[str, int], int]] = {}
    block_counts: dict[str | None, int] = {}
    result: list[BlockExecutionSchedule] = []
    for block_ordinal, block_id in enumerate(ranked_blocks, start=1):
        stratum = None if stratum_key is None else stratum_key(block_id)
        counts = counts_by_stratum.setdefault(
            stratum,
            {
                (arm, position): 0
                for arm in arms
                for position in range(len(arms))
            },
        )
        block_counts[stratum] = block_counts.get(stratum, 0) + 1
        scored: list[tuple[tuple[int, int, bytes], tuple[str, ...]]] = []
        for permutation in itertools.permutations(arms):
            prospective = dict(counts)
            for position, arm in enumerate(permutation):
                prospective[arm, position] += 1
            values = tuple(prospective.values())
            tie_parts = [str(seed)]
            if stratum is not None:
                tie_parts.append(stratum)
            tie_parts.extend((block_id, ",".join(permutation)))
            tie_payload = "\x00".join(tie_parts).encode("ascii")
            score = (
                max(values) - min(values),
                sum(value * value for value in values),
                hashlib.sha256(tie_payload).digest(),
            )
            scored.append((score, permutation))
        _, arm_order = min(scored)
        for position, arm in enumerate(arm_order):
            counts[arm, position] += 1
        result.append(
            BlockExecutionSchedule(
                block_id=block_id,
                block_execution_ordinal=block_ordinal,
                arm_order=arm_order,
            )
        )

    for stratum, counts in counts_by_stratum.items():
        lower = block_counts[stratum] // len(arms)
        upper = lower + int(block_counts[stratum] % len(arms) != 0)
        if set(counts.values()) - {lower, upper}:
            suffix = "" if stratum is None else f" in {stratum}"
            _error(f"execution schedule did not balance arm positions{suffix}")
    return tuple(result)


def derive_execution_schedule(
    block_ids: Sequence[str],
    arm_codes: Sequence[str],
    campaign_order_seed: int,
) -> tuple[BlockExecutionSchedule, ...]:
    """Predeclare a result-independent, position-balanced launch order."""

    return _derive_balanced_execution_schedule(
        block_ids,
        arm_codes,
        campaign_order_seed,
        stratum_key=None,
    )


def derive_stratified_execution_schedule(
    block_ids: Sequence[str],
    arm_codes: Sequence[str],
    campaign_order_seed: int,
) -> tuple[BlockExecutionSchedule, ...]:
    """Balance arm positions inside each replica-count/fanout stratum.

    Block launch order remains the frozen result-independent SHA-256 order.
    Arm-position counts are reset for each ``nN-fF`` cell so a global balance
    cannot hide a systematic order imbalance in either repeated headline cell.
    """

    def stratum(block_id: str) -> str:
        cell, separator, repetition = block_id.rpartition("-b")
        if not separator or not cell or not repetition.isdigit():
            _error("stratified execution requires nN-fF-bNN block IDs")
        return cell

    return _derive_balanced_execution_schedule(
        block_ids,
        arm_codes,
        campaign_order_seed,
        stratum_key=stratum,
    )


def derive_actor_ids(
    replica_count: int,
    quorum: int,
    actor_count: int,
    scientific_seed: int,
) -> tuple[int, ...]:
    pool = tuple(range(quorum, replica_count))
    if actor_count > len(pool):
        _error("campaign actor count exceeds the canonical non-reference-root pool")
    membership = ",".join(str(member) for member in range(replica_count))

    def rank(member: int) -> tuple[bytes, int]:
        value = (f"{membership}\x00{quorum}\x00{scientific_seed}\x00{member}").encode(
            "ascii"
        )
        return hashlib.sha256(value).digest(), member

    return tuple(sorted(sorted(pool, key=rank)[:actor_count]))


def derive_responsive_degraded_actor_ids(
    replica_count: int,
    quorum: int,
    hard_actor_ids: Sequence[int],
    scientific_seed: int,
) -> tuple[int, ...]:
    """Derive responsive-degraded actors while isolating commit observer 0.

    The rank preimage is exactly the ASCII byte sequence
    ``kauri.shape25.responsive-degraded.v1\0{membership_csv}\0{q}\0{seed}\0{id}``.
    Candidate membership is the canonical epoch-0 reference-root pool
    ``[1, Q)``; replica 0 is excluded because it is the authoritative commit
    observer for the throughput endpoint.
    """

    n = _integer(replica_count, "N")
    q = _integer(quorum, "Q")
    seed = _integer(scientific_seed, "scientific seed", minimum=0)
    if (n - 1) % 3:
        _error("replica count must satisfy N = 3f + 1")
    f = (n - 1) // 3
    if q != 2 * f + 1:
        _error("responsive-degraded derivation requires Q = 2f + 1")
    hard = tuple(
        sorted(_integer(actor, "hard actor", minimum=0) for actor in hard_actor_ids)
    )
    if (
        not hard
        or len(set(hard)) != len(hard)
        or len(hard) >= f
        or any(actor < q or actor >= n for actor in hard)
    ):
        _error("hard actors must be a unique proper subset of f in [Q, N)")
    actor_count = f - len(hard)
    pool = tuple(range(1, q))
    if actor_count > len(pool):
        _error("responsive-degraded actor count exceeds isolated root pool")
    membership = ",".join(str(member) for member in range(n))

    def rank(member: int) -> tuple[bytes, int]:
        value = (
            "kauri.shape25.responsive-degraded.v1"
            f"\x00{membership}\x00{q}\x00{seed}\x00{member}"
        ).encode("ascii")
        return hashlib.sha256(value).digest(), member

    return tuple(sorted(sorted(pool, key=rank)[:actor_count]))


def derive_tiered_cohorts(
    replica_count: int,
    quorum: int,
    hard_actor_count: int,
    scientific_seed: int,
) -> TieredCohorts:
    """Derive disjoint hard, responsive-degraded, and fast cohorts."""

    hard = derive_actor_ids(
        replica_count,
        quorum,
        hard_actor_count,
        scientific_seed,
    )
    responsive_degraded = derive_responsive_degraded_actor_ids(
        replica_count,
        quorum,
        hard,
        scientific_seed,
    )
    worse = frozenset((*hard, *responsive_degraded))
    fast = tuple(member for member in range(replica_count) if member not in worse)
    f = (replica_count - 1) // 3
    if (
        len(worse) != f
        or len(fast) != quorum
        or 0 not in fast
        or set(hard).intersection(responsive_degraded)
    ):
        _error("tiered cohorts must be disjoint f-sized worse and Q-sized fast sets")
    return TieredCohorts(
        hard_actor_ids=hard,
        responsive_degraded_actor_ids=responsive_degraded,
        fast_replica_ids=fast,
    )


def build_factorial_plan(manifest: FrozenFactorialManifest) -> FactorialPlan:
    """Derive the immutable 17-block, 68-slot preflight plan."""

    slots: list[FactorialSlot] = []
    schedule_builder = (
        derive_stratified_execution_schedule
        if manifest.arm_counterbalancing
        == "stratified_greedy_minimum_position_imbalance_sha256_tiebreak_v2"
        else derive_execution_schedule
    )
    execution_schedule = schedule_builder(
        _frozen_block_ids(),
        tuple(arm.code for arm in manifest.arms),
        manifest.campaign_order_seed,
    )
    execution_by_block = {
        scheduled.block_id: scheduled for scheduled in execution_schedule
    }
    block_ordinal = 0
    for replica_count in manifest.replica_counts:
        for initial_fanout in manifest.initial_fanouts:
            consensus = derive_consensus_shape(
                replica_count,
                initial_fanout=initial_fanout,
                candidate_fanouts=manifest.candidate_fanouts,
            )
            if any(
                not epoch0_internal_tree_ids(
                    replica_count,
                    initial_fanout=initial_fanout,
                    replica_id=actor,
                )
                for actor in range(consensus.q, replica_count)
            ):
                _error(
                    "canonical actor pool contains a member that cannot be "
                    "internal in any active epoch-0 tree"
                )
            blocks_in_cell = manifest.blocks_for(replica_count, initial_fanout)
            for block_index in range(1, blocks_in_cell + 1):
                block_id = f"n{replica_count}-f{initial_fanout}-b{block_index:02d}"
                scientific_seed = manifest.scientific_seed_base + block_ordinal
                tiered_cohorts = (
                    derive_tiered_cohorts(
                        replica_count,
                        consensus.q,
                        manifest.byzantine.actor_count,
                        scientific_seed,
                    )
                    if manifest.byzantine.responsive_degradation is not None
                    else None
                )
                actor_ids = (
                    tiered_cohorts.hard_actor_ids
                    if tiered_cohorts is not None
                    else derive_actor_ids(
                        replica_count,
                        consensus.q,
                        manifest.byzantine.actor_count,
                        scientific_seed,
                    )
                )
                if any(
                    not epoch0_internal_tree_ids(
                        replica_count,
                        initial_fanout=initial_fanout,
                        replica_id=actor,
                    )
                    for actor in actor_ids
                ):
                    _error("selected actor cannot be internal in an epoch-0 tree")
                if any(
                    len(
                        epoch0_distinct_parent_ids(
                            replica_count,
                            initial_fanout=initial_fanout,
                            replica_id=actor,
                        )
                    )
                    < consensus.f + 1
                    for actor in actor_ids
                ):
                    _error(
                        "selected actor cannot obtain the frozen f+1 causal "
                        "reporter guard across epoch-0 physical roles"
                    )
                scheduled = execution_by_block[block_id]
                for arm in manifest.arms:
                    nonce = derive_slot_nonce(block_id, arm.code)
                    if nonce != len(slots):
                        _error("derived slot nonce disagrees with frozen order")
                    ordinal = nonce + 1
                    arm_execution_position = scheduled.arm_order.index(arm.code) + 1
                    execution_ordinal = (scheduled.block_execution_ordinal - 1) * len(
                        manifest.arms
                    ) + arm_execution_position
                    slot_id = f"slot-{ordinal:03d}-{block_id}-{arm.code}"
                    port_offset = nonce * manifest.resources.slot_port_stride
                    ports = PortAllocation(
                        peer_base=manifest.resources.peer_port_base + port_offset,
                        client_base=manifest.resources.client_port_base + port_offset,
                        manager=manifest.resources.manager_port_base + port_offset,
                    )
                    if (
                        max(
                            ports.peer_base + replica_count - 1,
                            ports.client_base + replica_count - 1,
                            ports.manager,
                        )
                        > 65_535
                    ):
                        _error(f"derived ports exceed uint16 for {slot_id}")
                    slots.append(
                        FactorialSlot(
                            ordinal=ordinal,
                            slot_nonce=nonce,
                            slot_id=slot_id,
                            block_id=block_id,
                            block_index=block_index,
                            blocks_in_cell=blocks_in_cell,
                            block_execution_ordinal=(scheduled.block_execution_ordinal),
                            arm_execution_position=arm_execution_position,
                            execution_ordinal=execution_ordinal,
                            scientific_seed=scientific_seed,
                            consensus=consensus,
                            candidate_fanouts=manifest.candidate_fanouts,
                            pipeline_stretch=manifest.pipeline_stretch,
                            epoch_fanout_policy=manifest.epoch_fanout_policy,
                            pipeline_policy=manifest.pipeline_policy,
                            arm=arm,
                            byzantine=manifest.byzantine,
                            byzantine_actor_ids=actor_ids,
                            workload=manifest.workload,
                            responsiveness_policy=manifest.responsiveness_policy,
                            common_timers=manifest.common_timers,
                            ports=ports,
                            result_path=f"{manifest.results_root}/{slot_id}",
                            cleanup_contract=manifest.cleanup_contract,
                            responsive_degraded_actor_ids=(
                                tiered_cohorts.responsive_degraded_actor_ids
                                if tiered_cohorts is not None
                                else ()
                            ),
                            fast_replica_ids=(
                                tiered_cohorts.fast_replica_ids
                                if tiered_cohorts is not None
                                else ()
                            ),
                            max_omissions_per_proposal=(
                                consensus.f if tiered_cohorts is not None else None
                            ),
                        )
                    )
                block_ordinal += 1

    if (block_ordinal, len(slots)) != (EXPECTED_BLOCK_COUNT, EXPECTED_SLOT_COUNT):
        _error("frozen matrix must derive exactly 17 blocks and 68 slots")
    if len({slot.slot_id for slot in slots}) != EXPECTED_SLOT_COUNT:
        _error("derived slot IDs are not unique")
    if {slot.execution_ordinal for slot in slots} != set(
        range(1, EXPECTED_SLOT_COUNT + 1)
    ):
        _error("execution schedule must cover each global ordinal exactly once")

    plan = FactorialPlan(
        manifest_id=manifest.manifest_id,
        manifest_sha256=manifest.manifest_sha256,
        execution_authorized=manifest.execution_authorized,
        execution_receipt_required=manifest.execution_receipt_required,
        execution_mode=manifest.execution_mode,
        automatic_retries=manifest.automatic_retries,
        replacement_policy=manifest.replacement_policy,
        outcome_dependent_order=(
            manifest.scheduling_outcome_dependent_order
            or manifest.execution_outcome_dependent_order
        ),
        campaign_order_seed=manifest.campaign_order_seed,
        execution_block_order=manifest.execution_block_order,
        arm_counterbalancing=manifest.arm_counterbalancing,
        execution_schedule=execution_schedule,
        claim_scope=manifest.claim_scope,
        preserve_outcomes=manifest.preserve_outcomes,
        results_root=manifest.results_root,
        canonical_plan_filename=manifest.canonical_plan_filename,
        minimum_free_bytes=manifest.resources.minimum_free_bytes,
        minimum_free_bytes_interpretation=(
            manifest.resources.minimum_free_bytes_interpretation
        ),
        max_parallel_slots=manifest.resources.max_parallel_slots,
        global_worst_candidate_depth=max(slot.worst_candidate_depth for slot in slots),
        slots=tuple(slots),
    )
    if (
        manifest.common_timers.global_worst_candidate_depth
        != plan.global_worst_candidate_depth
    ):
        _error("common timer depth disagrees with the derived global depth")
    expected_plan_identity = {
        LEGACY_MANIFEST_ID: None,
        V2_MANIFEST_ID: (V2_MANIFEST_SHA256, V2_PLAN_SHA256),
        V3_MANIFEST_ID: (V3_MANIFEST_SHA256, V3_PLAN_SHA256),
        V4_MANIFEST_ID: (V4_MANIFEST_SHA256, V4_PLAN_SHA256),
        V5_MANIFEST_ID: (V5_MANIFEST_SHA256, V5_PLAN_SHA256),
        V6_MANIFEST_ID: (V6_MANIFEST_SHA256, V6_PLAN_SHA256),
        V7_MANIFEST_ID: (V7_MANIFEST_SHA256, V7_PLAN_SHA256),
        V8_MANIFEST_ID: (V8_MANIFEST_SHA256, V8_PLAN_SHA256),
        V9_MANIFEST_ID: (V9_MANIFEST_SHA256, V9_PLAN_SHA256),
        V10_MANIFEST_ID: (V10_MANIFEST_SHA256, V10_PLAN_SHA256),
        V11_MANIFEST_ID: (V11_MANIFEST_SHA256, V11_PLAN_SHA256),
        V12_MANIFEST_ID: (V12_MANIFEST_SHA256, V12_PLAN_SHA256),
        V13_MANIFEST_ID: (V13_MANIFEST_SHA256, V13_PLAN_SHA256),
        V14_MANIFEST_ID: (V14_MANIFEST_SHA256, V14_PLAN_SHA256),
        V15_MANIFEST_ID: (V15_MANIFEST_SHA256, V15_PLAN_SHA256),
        V16_MANIFEST_ID: (V16_MANIFEST_SHA256, V16_PLAN_SHA256),
        V17_MANIFEST_ID: (V17_MANIFEST_SHA256, V17_PLAN_SHA256),
        V18_MANIFEST_ID: (V18_MANIFEST_SHA256, V18_PLAN_SHA256),
        V19_MANIFEST_ID: (V19_MANIFEST_SHA256, V19_PLAN_SHA256),
        V20_MANIFEST_ID: (V20_MANIFEST_SHA256, V20_PLAN_SHA256),
        V21_MANIFEST_ID: (V21_MANIFEST_SHA256, V21_PLAN_SHA256),
        V22_MANIFEST_ID: (V22_MANIFEST_SHA256, V22_PLAN_SHA256),
        V23_MANIFEST_ID: (V23_MANIFEST_SHA256, V23_PLAN_SHA256),
        V24_MANIFEST_ID: (V24_MANIFEST_SHA256, V24_PLAN_SHA256),
        V25_MANIFEST_ID: (V25_MANIFEST_SHA256, V25_PLAN_SHA256),
        V26_MANIFEST_ID: (V26_MANIFEST_SHA256, V26_PLAN_SHA256),
        V27_MANIFEST_ID: (V27_MANIFEST_SHA256, V27_PLAN_SHA256),
        V28_MANIFEST_ID: (V28_MANIFEST_SHA256, V28_PLAN_SHA256),
        V29_MANIFEST_ID: (V29_MANIFEST_SHA256, V29_PLAN_SHA256),
        V30_MANIFEST_ID: (V30_MANIFEST_SHA256, V30_PLAN_SHA256),
        V31_MANIFEST_ID: (V31_MANIFEST_SHA256, V31_PLAN_SHA256),
        V32_MANIFEST_ID: (V32_MANIFEST_SHA256, V32_PLAN_SHA256),
        V33_MANIFEST_ID: (V33_MANIFEST_SHA256, V33_PLAN_SHA256),
        V34_MANIFEST_ID: (V34_MANIFEST_SHA256, V34_PLAN_SHA256),
        V35_MANIFEST_ID: (V35_MANIFEST_SHA256, V35_PLAN_SHA256),
        V36_MANIFEST_ID: (V36_MANIFEST_SHA256, V36_PLAN_SHA256),
        V37_MANIFEST_ID: (V37_MANIFEST_SHA256, V37_PLAN_SHA256),
        V38_MANIFEST_ID: (V38_MANIFEST_SHA256, V38_PLAN_SHA256),
        V39_MANIFEST_ID: (V39_MANIFEST_SHA256, V39_PLAN_SHA256),
        FROZEN_MANIFEST_ID: (FROZEN_MANIFEST_SHA256, FROZEN_PLAN_SHA256),
    }[manifest.manifest_id]
    if (
        expected_plan_identity is not None
        and manifest.manifest_sha256 == expected_plan_identity[0]
        and plan.plan_sha256 != expected_plan_identity[1]
    ):
        _error("canonical plan bytes differ from the frozen plan identity")
    return plan


def canonical_plan_bytes(plan: FactorialPlan) -> bytes:
    if not isinstance(plan, FactorialPlan):
        _error("canonical plan input must be a FactorialPlan")
    return _canonical_json_bytes(plan.as_document())


__all__ = (
    "BREAKTHROUGH_STRUCTURAL_GATE_V4",
    "EVIDENCE_SNAPSHOT_SELECTION_CONTRACT_V1",
    "EXCLUDED_REPAIR_SMOKE_OBSERVATION_CONTRACT_V1",
    "EXCLUDED_REPAIR_SMOKE_OBSERVATION_CONTRACT_V2",
    "EXCLUDED_REPAIR_SMOKE_OBSERVATION_CONTRACT_V3",
    "EXCLUDED_REPAIR_SMOKE_OBSERVATION_CONTRACT_V4",
    "EXCLUDED_REPAIR_SMOKE_OBSERVATION_CONTRACT_V5",
    "EXCLUDED_REPAIR_SMOKE_VERIFIED_RESPONSE_DUPLICATE_PROBE_CONTRACT_V1",
    "POST_FINAL_CONVERGENCE_UNMATCHED_COMMIT_EVIDENCE_CONTRACT_V1",
    "POST_FINAL_CONVERGENCE_UNMATCHED_COMMIT_EVIDENCE_CONTRACT_V2",
    "EXECUTION_CLEANUP_CONTRACT_V1",
    "EXPECTED_ARM_CODES",
    "EXPECTED_BLOCK_COUNT",
    "EXPECTED_CANDIDATE_FANOUTS",
    "EXPECTED_INITIAL_FANOUTS",
    "EXPECTED_REPLICA_COUNTS",
    "EXPECTED_SLOT_COUNT",
    "FUTURE_TREE_PROPOSAL_DELIVERY_CONTRACT_V1",
    "FUTURE_TREE_PROPOSAL_DELIVERY_CONTRACT_V2",
    "FROZEN_MANIFEST_ID",
    "FROZEN_MANIFEST_SHA256",
    "FROZEN_PLAN_SHA256",
    "FROZEN_SEMANTIC_SHA256",
    "INHERITED_CONSENSUS_WAIT_EXEMPT_PLACEMENT_CONTRACT_V1",
    "VERIFIED_RESPONSE_DUPLICATE_DELIVERY_CONTRACT_V1",
    "VERIFIED_RESPONSE_DUPLICATE_DELIVERY_CONTRACT_V2",
    "PRECONTAINMENT_FAULT_COVERAGE_GATE_V1",
    "PRECONTAINMENT_GUARDED_SELECTION_CONTRACT_V1",
    "PRECONTAINMENT_SHAPE_EVALUATION_CONTRACT_V1",
    "RESPONSIVE_CAUSAL_TIMEOUT_LINKAGE_V1",
    "RESPONSIVE_CAUSAL_TIMEOUT_ELIGIBILITY_V1",
    "RESPONSIVE_CAUSAL_TIMEOUT_ELIGIBILITY_V2",
    "RESPONSIVE_CAUSAL_TIMEOUT_ELIGIBILITY_V3",
    "RESPONSIVE_CAUSAL_TIMEOUT_PROVENANCE_WINDOW_V1",
    "RESPONSIVE_CAUSAL_INTERNAL_WITNESS_CANDIDATES_V1",
    "RESPONSIVE_CAUSAL_SELECTION_LINKAGE_WINDOW_V1",
    "RESPONSIVE_MARKER_COMPLETENESS_WITNESS_V1",
    "RESPONSIVE_MARKER_COMPLETENESS_WITNESS_V2",
    "RESPONSIVE_PENDING_ATTEMPT_RETENTION_V1",
    "RESPONSIVE_ROLE_SCOPED_SCHEDULE_V1",
    "SOURCE_BOUND_PROPOSAL_WITNESS_CONTRACT_V1",
    "LEGACY_MANIFEST_ID",
    "LEGACY_MANIFEST_SHA256",
    "LEGACY_PLAN_SHA256",
    "LEGACY_SEMANTIC_SHA256",
    "V2_MANIFEST_ID",
    "V2_MANIFEST_SHA256",
    "V2_PLAN_SHA256",
    "V2_SEMANTIC_SHA256",
    "V3_MANIFEST_ID",
    "V3_MANIFEST_SHA256",
    "V3_PLAN_SHA256",
    "V3_SEMANTIC_SHA256",
    "V4_MANIFEST_ID",
    "V4_MANIFEST_SHA256",
    "V4_PLAN_SHA256",
    "V4_SEMANTIC_SHA256",
    "V5_MANIFEST_ID",
    "V5_MANIFEST_SHA256",
    "V5_PLAN_SHA256",
    "V5_SEMANTIC_SHA256",
    "V6_MANIFEST_ID",
    "V6_MANIFEST_SHA256",
    "V6_PLAN_SHA256",
    "V6_SEMANTIC_SHA256",
    "V7_MANIFEST_ID",
    "V7_MANIFEST_SHA256",
    "V7_PLAN_SHA256",
    "V7_SEMANTIC_SHA256",
    "V8_MANIFEST_ID",
    "V8_MANIFEST_SHA256",
    "V8_PLAN_SHA256",
    "V8_SEMANTIC_SHA256",
    "V9_MANIFEST_ID",
    "V9_MANIFEST_SHA256",
    "V9_PLAN_SHA256",
    "V9_SEMANTIC_SHA256",
    "V10_MANIFEST_ID",
    "V10_MANIFEST_SHA256",
    "V10_PLAN_SHA256",
    "V10_SEMANTIC_SHA256",
    "V11_MANIFEST_ID",
    "V11_MANIFEST_SHA256",
    "V11_PLAN_SHA256",
    "V11_SEMANTIC_SHA256",
    "V12_MANIFEST_ID",
    "V12_MANIFEST_SHA256",
    "V12_PLAN_SHA256",
    "V12_SEMANTIC_SHA256",
    "V13_MANIFEST_ID",
    "V13_MANIFEST_SHA256",
    "V13_PLAN_SHA256",
    "V13_SEMANTIC_SHA256",
    "V14_MANIFEST_ID",
    "V14_MANIFEST_SHA256",
    "V14_PLAN_SHA256",
    "V14_SEMANTIC_SHA256",
    "V15_MANIFEST_ID",
    "V15_MANIFEST_SHA256",
    "V15_PLAN_SHA256",
    "V15_SEMANTIC_SHA256",
    "V16_MANIFEST_ID",
    "V16_MANIFEST_SHA256",
    "V16_PLAN_SHA256",
    "V16_SEMANTIC_SHA256",
    "V17_MANIFEST_ID",
    "V17_MANIFEST_SHA256",
    "V17_PLAN_SHA256",
    "V17_SEMANTIC_SHA256",
    "V18_MANIFEST_ID",
    "V18_MANIFEST_SHA256",
    "V18_PLAN_SHA256",
    "V18_SEMANTIC_SHA256",
    "V19_MANIFEST_ID",
    "V19_MANIFEST_SHA256",
    "V19_PLAN_SHA256",
    "V19_SEMANTIC_SHA256",
    "V20_MANIFEST_ID",
    "V20_MANIFEST_SHA256",
    "V20_PLAN_SHA256",
    "V20_SEMANTIC_SHA256",
    "V21_MANIFEST_ID",
    "V21_MANIFEST_SHA256",
    "V21_PLAN_SHA256",
    "V21_SEMANTIC_SHA256",
    "V22_MANIFEST_ID",
    "V22_MANIFEST_SHA256",
    "V22_PLAN_SHA256",
    "V22_SEMANTIC_SHA256",
    "V23_MANIFEST_ID",
    "V23_MANIFEST_SHA256",
    "V23_PLAN_SHA256",
    "V23_SEMANTIC_SHA256",
    "V24_MANIFEST_ID",
    "V24_MANIFEST_SHA256",
    "V24_PLAN_SHA256",
    "V24_SEMANTIC_SHA256",
    "V25_MANIFEST_ID",
    "V25_MANIFEST_SHA256",
    "V25_PLAN_SHA256",
    "V25_SEMANTIC_SHA256",
    "V26_MANIFEST_ID",
    "V26_MANIFEST_SHA256",
    "V26_PLAN_SHA256",
    "V26_SEMANTIC_SHA256",
    "V27_MANIFEST_ID",
    "V27_MANIFEST_SHA256",
    "V27_PLAN_SHA256",
    "V27_SEMANTIC_SHA256",
    "V28_MANIFEST_ID",
    "V28_MANIFEST_SHA256",
    "V28_PLAN_SHA256",
    "V28_SEMANTIC_SHA256",
    "V29_MANIFEST_ID",
    "V29_MANIFEST_SHA256",
    "V29_PLAN_SHA256",
    "V29_SEMANTIC_SHA256",
    "V30_MANIFEST_ID",
    "V30_MANIFEST_SHA256",
    "V30_PLAN_SHA256",
    "V30_SEMANTIC_SHA256",
    "V31_MANIFEST_ID",
    "V31_MANIFEST_SHA256",
    "V31_PLAN_SHA256",
    "V31_SEMANTIC_SHA256",
    "V32_MANIFEST_ID",
    "V32_MANIFEST_SHA256",
    "V32_PLAN_SHA256",
    "V32_SEMANTIC_SHA256",
    "V33_MANIFEST_ID",
    "V33_MANIFEST_SHA256",
    "V33_PLAN_SHA256",
    "V33_SEMANTIC_SHA256",
    "V34_MANIFEST_ID",
    "V34_MANIFEST_SHA256",
    "V34_PLAN_SHA256",
    "V34_SEMANTIC_SHA256",
    "V35_MANIFEST_ID",
    "V35_MANIFEST_SHA256",
    "V35_PLAN_SHA256",
    "V35_SEMANTIC_SHA256",
    "V36_MANIFEST_ID",
    "V36_MANIFEST_SHA256",
    "V36_PLAN_SHA256",
    "V36_SEMANTIC_SHA256",
    "V37_MANIFEST_ID",
    "V37_MANIFEST_SHA256",
    "V37_PLAN_SHA256",
    "V37_SEMANTIC_SHA256",
    "V38_MANIFEST_ID",
    "V38_MANIFEST_SHA256",
    "V38_PLAN_SHA256",
    "V38_SEMANTIC_SHA256",
    "V39_MANIFEST_ID",
    "V39_MANIFEST_SHA256",
    "V39_PLAN_SHA256",
    "V39_SEMANTIC_SHA256",
    "ActorSelectionVector",
    "ActorRotationVector",
    "ByzantineActions",
    "ByzantineContract",
    "BlockExecutionSchedule",
    "ClaimScope",
    "CommonTimers",
    "ConsensusShape",
    "FactorialArm",
    "FactorialManifestError",
    "FactorialPlan",
    "FactorialSlot",
    "FrozenFactorialManifest",
    "PortAllocation",
    "ResponsivenessPolicyContract",
    "ResourceContract",
    "ResponsiveDegradationContract",
    "TieredCohorts",
    "WorkloadContract",
    "build_factorial_plan",
    "canonical_plan_bytes",
    "derive_actor_ids",
    "derive_consensus_shape",
    "derive_execution_schedule",
    "derive_stratified_execution_schedule",
    "derive_responsive_degraded_actor_ids",
    "derive_slot_nonce",
    "derive_tiered_cohorts",
    "epoch0_distinct_parent_ids",
    "epoch0_internal_tree_ids",
    "load_frozen_manifest",
    "load_frozen_manifest_bytes",
    "parse_manifest_bytes",
    "rotating_omission_actor",
    "tree_depth",
)
