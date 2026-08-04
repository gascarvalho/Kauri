add_executable(test_secp256k1 test_secp256k1.cpp)
target_link_libraries(test_secp256k1 hotstuff_static)
add_test(NAME test_secp256k1 COMMAND test_secp256k1)
set_tests_properties(test_secp256k1 PROPERTIES LABELS "crypto;legacy")

add_library(kauri_catch_main OBJECT test_main.cpp)
target_include_directories(kauri_catch_main PRIVATE
    "${PROJECT_SOURCE_DIR}/bls/contrib/catch")

add_library(kauri_test_support INTERFACE)
target_include_directories(kauri_test_support INTERFACE
    "${PROJECT_SOURCE_DIR}"
    "${PROJECT_SOURCE_DIR}/include"
    "${PROJECT_SOURCE_DIR}/salticidae/include"
    "${PROJECT_SOURCE_DIR}/bls/contrib/catch"
    "${CMAKE_CURRENT_SOURCE_DIR}")
target_link_libraries(kauri_test_support INTERFACE hotstuff_static)

function(add_kauri_catch_test target source)
    add_executable(
        ${target}
        ${source}
        $<TARGET_OBJECTS:kauri_catch_main>)
    target_link_libraries(${target} PRIVATE kauri_test_support)
    add_test(NAME ${target} COMMAND ${target})
    set_tests_properties(${target} PROPERTIES LABELS "unit;adaptive")
endfunction()

add_kauri_catch_test(test_test_support test_test_support.cpp)
add_kauri_catch_test(test_client_opcodes test_client_opcodes.cpp)
set_tests_properties(
    test_client_opcodes PROPERTIES
    LABELS "unit;adaptive;client;opcode")
add_kauri_catch_test(test_crypto_aggregation test_crypto_aggregation.cpp)
add_kauri_catch_test(test_vote_validation test_vote_validation.cpp)
add_kauri_catch_test(
    test_crypto_aggregation_remediation
    test_crypto_aggregation_remediation.cpp)
add_kauri_catch_test(
    test_vote_validation_remediation
    test_vote_validation_remediation.cpp)
add_kauri_catch_test(
    test_vote_validation_coordination
    test_vote_validation_coordination.cpp)
add_kauri_catch_test(
    test_exact_vote_identity
    test_exact_vote_identity.cpp)
set_tests_properties(
    test_exact_vote_identity PROPERTIES
    LABELS "unit;adaptive;intentional-red")
add_kauri_catch_test(
    test_exact_vote_identity_integration
    test_exact_vote_identity_integration.cpp)
target_compile_definitions(
    test_exact_vote_identity_integration PRIVATE
    KAURI_PROJECT_SOURCE_DIR="${PROJECT_SOURCE_DIR}")
set_tests_properties(
    test_exact_vote_identity_integration PROPERTIES
    LABELS "unit;adaptive;intentional-red"
    TIMEOUT 5)
add_kauri_catch_test(
    test_exact_proposal_context
    test_exact_proposal_context.cpp)
add_kauri_catch_test(
    test_committed_context_cleanup
    test_committed_context_cleanup.cpp)
target_sources(
    test_committed_context_cleanup PRIVATE
    test_committed_context_cleanup_wiring.cpp)
target_compile_definitions(
    test_committed_context_cleanup PRIVATE
    KAURI_PROJECT_SOURCE_DIR="${PROJECT_SOURCE_DIR}")
set_tests_properties(
    test_committed_context_cleanup PROPERTIES
    LABELS "unit;adaptive;intentional-red"
    TIMEOUT 5)
add_kauri_catch_test(
    test_exact_vote_handler_integration
    test_exact_vote_handler_integration.cpp)
target_compile_definitions(
    test_exact_vote_handler_integration PRIVATE
    KAURI_PROJECT_SOURCE_DIR="${PROJECT_SOURCE_DIR}")
set_tests_properties(
    test_exact_proposal_context
    test_exact_vote_handler_integration
    PROPERTIES
    LABELS "unit;adaptive;intentional-red"
    TIMEOUT 5)
add_kauri_catch_test(
    test_pending_exact_contribution_buffer
    test_pending_exact_contribution_buffer.cpp)
target_compile_definitions(
    test_pending_exact_contribution_buffer PRIVATE
    KAURI_PROJECT_SOURCE_DIR="${PROJECT_SOURCE_DIR}")
set_tests_properties(
    test_pending_exact_contribution_buffer PROPERTIES
    LABELS "unit;adaptive;intentional-red"
    TIMEOUT 5)
add_kauri_catch_test(
    test_commit_rule_integration
    test_commit_rule.cpp)
add_kauri_catch_test(
    test_consensus_conflicts
    test_consensus_conflicts.cpp)
add_kauri_catch_test(
    test_commit_rule_progression
    test_commit_progression.cpp)
add_kauri_catch_test(
    test_configuration
    test_configuration.cpp)
add_kauri_catch_test(
    test_epoch_change
    test_epoch_change.cpp)
set_tests_properties(
    test_epoch_change PROPERTIES
    LABELS "unit;adaptive;epoch-change;c07"
    TIMEOUT 10)
add_kauri_catch_test(
    test_epoch_change_bundle
    test_epoch_change_bundle.cpp)
set_tests_properties(
    test_epoch_change_bundle PROPERTIES
    LABELS "unit;adaptive;epoch-change;bundle;c09"
    TIMEOUT 10)
add_kauri_catch_test(
    test_epoch_change_inbox
    test_epoch_change_inbox.cpp)
set_tests_properties(
    test_epoch_change_inbox PROPERTIES
    LABELS "unit;adaptive;epoch-change;inbox;c09"
    TIMEOUT 10)
add_kauri_catch_test(
    test_evidence
    test_evidence.cpp)
set_tests_properties(
    test_evidence PROPERTIES
    LABELS "unit;adaptive;intentional-red;evidence"
    TIMEOUT 5)
add_kauri_catch_test(
    test_response_attempt
    test_response_attempt.cpp)
target_compile_definitions(
    test_response_attempt PRIVATE
    KAURI_PROJECT_SOURCE_DIR="${PROJECT_SOURCE_DIR}")
set_tests_properties(
    test_response_attempt PROPERTIES
    LABELS "unit;adaptive;intentional-red;evidence;response-attempt"
    TIMEOUT 5)
add_kauri_catch_test(
    test_evidence_reporter
    test_evidence_reporter.cpp)
target_compile_definitions(
    test_evidence_reporter PRIVATE
    KAURI_PROJECT_SOURCE_DIR="${PROJECT_SOURCE_DIR}")
set_tests_properties(
    test_evidence_reporter PROPERTIES
    LABELS "unit;adaptive;intentional-red;evidence;evidence-reporter"
    TIMEOUT 5)
add_kauri_catch_test(
    test_adaptive_v2_response_evidence
    test_adaptive_v2_response_evidence.cpp)
target_compile_definitions(
    test_adaptive_v2_response_evidence PRIVATE
    KAURI_PROJECT_SOURCE_DIR="${PROJECT_SOURCE_DIR}")
set_tests_properties(
    test_adaptive_v2_response_evidence PROPERTIES
    LABELS "unit;adaptive;evidence;response-attempt;adaptive-v2"
    TIMEOUT 5)
add_kauri_catch_test(
    test_experiment_byzantine_adapter
    test_experiment_byzantine_adapter.cpp)
target_compile_definitions(
    test_experiment_byzantine_adapter PRIVATE
    KAURI_PROJECT_SOURCE_DIR="${PROJECT_SOURCE_DIR}")
set_tests_properties(
    test_experiment_byzantine_adapter PROPERTIES
    LABELS "unit;adaptive;experiment;byzantine;intentional-red"
    TIMEOUT 5)
add_kauri_catch_test(
    test_experiment_byzantine_runtime_integration
    test_experiment_byzantine_runtime_integration.cpp)
set_tests_properties(
    test_experiment_byzantine_runtime_integration PROPERTIES
    LABELS "unit;adaptive;experiment;byzantine;runtime-integration"
    TIMEOUT 10)
add_kauri_catch_test(
    test_experiment_post_qc_audit
    test_experiment_post_qc_audit.cpp)
target_compile_definitions(
    test_experiment_post_qc_audit PRIVATE
    KAURI_PROJECT_SOURCE_DIR="${PROJECT_SOURCE_DIR}")
set_tests_properties(
    test_experiment_post_qc_audit PROPERTIES
    LABELS "unit;adaptive;experiment;post-qc-audit;intentional-red"
    TIMEOUT 10)
add_kauri_catch_test(
    test_evidence_ingress
    test_evidence_ingress.cpp)
target_compile_definitions(
    test_evidence_ingress PRIVATE
    KAURI_PROJECT_SOURCE_DIR="${PROJECT_SOURCE_DIR}")
set_tests_properties(
    test_evidence_ingress PROPERTIES
    LABELS "unit;adaptive;intentional-red;evidence;evidence-ingress"
    TIMEOUT 10)
add_kauri_catch_test(
    test_proposal_evidence_index
    test_proposal_evidence_index.cpp)
target_compile_definitions(
    test_proposal_evidence_index PRIVATE
    KAURI_PROJECT_SOURCE_DIR="${PROJECT_SOURCE_DIR}")
set_tests_properties(
    test_proposal_evidence_index PROPERTIES
    LABELS "unit;adaptive;intentional-red;evidence;proposal-evidence-index"
    TIMEOUT 5)
add_kauri_catch_test(
    test_evidence_lifecycle
    test_evidence_lifecycle.cpp)
set_tests_properties(
    test_evidence_lifecycle PROPERTIES
    LABELS "unit;adaptive;intentional-red;evidence;evidence-lifecycle"
    TIMEOUT 10)
add_kauri_catch_test(
    test_evidence_lifecycle_wire
    test_evidence_lifecycle_wire.cpp)
set_tests_properties(
    test_evidence_lifecycle_wire PROPERTIES
    LABELS "unit;adaptive;evidence;evidence-lifecycle;wire;adaptive-v2"
    TIMEOUT 10)
add_kauri_catch_test(
    test_adaptation
    test_adaptation.cpp)
target_compile_definitions(
    test_adaptation PRIVATE
    KAURI_PROJECT_SOURCE_DIR="${PROJECT_SOURCE_DIR}")
set_tests_properties(
    test_adaptation PROPERTIES
    LABELS "unit;adaptive;intentional-red;adaptation"
    TIMEOUT 5)
add_kauri_catch_test(
    test_adaptive_v2_selection
    test_adaptive_v2_selection.cpp)
set_tests_properties(
    test_adaptive_v2_selection PROPERTIES
    LABELS "unit;adaptive;selection;byzantine-guard;n7"
    TIMEOUT 10)
add_kauri_catch_test(
    test_adaptive_v2_shape_selection
    test_adaptive_v2_shape_selection.cpp)
target_compile_definitions(
    test_adaptive_v2_shape_selection PRIVATE
    KAURI_PROJECT_SOURCE_DIR="${PROJECT_SOURCE_DIR}")
set_tests_properties(
    test_adaptive_v2_shape_selection PROPERTIES
    LABELS "unit;adaptive;shape-selection;shape25"
    TIMEOUT 10)
add_kauri_catch_test(
    test_adaptive_v2_epoch_factory
    test_adaptive_v2_epoch_factory.cpp)
set_tests_properties(
    test_adaptive_v2_epoch_factory PROPERTIES
    LABELS "unit;adaptive;epoch-factory;bundle;adaptive-v2;n7"
    TIMEOUT 10)
add_kauri_catch_test(
    test_adaptive_v2_readiness_wire
    test_adaptive_v2_readiness_wire.cpp)
set_tests_properties(
    test_adaptive_v2_readiness_wire PROPERTIES
    LABELS "unit;adaptive;readiness;wire;adaptive-v2"
    TIMEOUT 10)
add_kauri_catch_test(
    test_adaptive_v2_manager_ingress
    test_adaptive_v2_manager_ingress.cpp)
set_tests_properties(
    test_adaptive_v2_manager_ingress PROPERTIES
    LABELS "adaptive-v2;manager-ingress;n7;evidence;unit"
    TIMEOUT 10)
add_kauri_catch_test(
    test_adaptive_v2_manager_controller
    test_adaptive_v2_manager_controller.cpp)
set_tests_properties(
    test_adaptive_v2_manager_controller PROPERTIES
    LABELS "adaptive-v2;manager-controller;n7;evidence;selection;epoch-factory;unit"
    TIMEOUT 10)
add_kauri_catch_test(
    test_adaptive_v2_manager_session
    test_adaptive_v2_manager_session.cpp)
set_tests_properties(
    test_adaptive_v2_manager_session PROPERTIES
    LABELS "adaptive-v2;manager-session;recurring;n7;integration;contract"
    TIMEOUT 15)
add_kauri_catch_test(
    test_adaptive_v2_manager_delivery
    test_adaptive_v2_manager_delivery.cpp)
target_compile_definitions(
    test_adaptive_v2_manager_delivery PRIVATE
    KAURI_PROJECT_SOURCE_DIR="${PROJECT_SOURCE_DIR}")
set_tests_properties(
    test_adaptive_v2_manager_delivery PROPERTIES
    LABELS "adaptive-v2;manager-delivery;one-shot;quorum;unit"
    TIMEOUT 5)
add_kauri_catch_test(
    test_adaptive_v2_convergence_wire
    test_adaptive_v2_convergence_wire.cpp)
set_tests_properties(
    test_adaptive_v2_convergence_wire PROPERTIES
    LABELS "unit;adaptive;adaptive-v2;convergence;wire;c1"
    TIMEOUT 10)
add_kauri_catch_test(
    test_adaptive_v2_convergence_ack_wire
    test_adaptive_v2_convergence_ack_wire.cpp)
set_tests_properties(
    test_adaptive_v2_convergence_ack_wire PROPERTIES
    LABELS "unit;adaptive;adaptive-v2;convergence;ack;wire"
    TIMEOUT 10)
add_kauri_catch_test(
    test_adaptive_v2_manager_convergence
    test_adaptive_v2_manager_convergence.cpp)
target_compile_definitions(
    test_adaptive_v2_manager_convergence PRIVATE
    KAURI_PROJECT_SOURCE_DIR="${PROJECT_SOURCE_DIR}")
set_tests_properties(
    test_adaptive_v2_manager_convergence PROPERTIES
    LABELS "unit;adaptive;adaptive-v2;manager-convergence;c3"
    TIMEOUT 10)
add_kauri_catch_test(
    test_adaptive_v2_replica_convergence_wiring
    test_adaptive_v2_replica_convergence_wiring.cpp)
target_compile_definitions(
    test_adaptive_v2_replica_convergence_wiring PRIVATE
    KAURI_PROJECT_SOURCE_DIR="${PROJECT_SOURCE_DIR}")
set_tests_properties(
    test_adaptive_v2_replica_convergence_wiring PROPERTIES
    LABELS "integration;adaptive;adaptive-v2;convergence;c5;replica-wiring"
    TIMEOUT 10)
add_kauri_catch_test(
    test_adaptive_v2_manager_convergence_wiring
    test_adaptive_v2_manager_convergence_wiring.cpp)
target_compile_definitions(
    test_adaptive_v2_manager_convergence_wiring PRIVATE
    KAURI_PROJECT_SOURCE_DIR="${PROJECT_SOURCE_DIR}")
set_tests_properties(
    test_adaptive_v2_manager_convergence_wiring PROPERTIES
    LABELS "integration;adaptive;adaptive-v2;convergence;c6;manager-wiring;structured-event"
    TIMEOUT 10)
add_kauri_catch_test(
    test_adaptive_v2_reporting_outbox
    test_adaptive_v2_reporting_outbox.cpp)
set_tests_properties(
    test_adaptive_v2_reporting_outbox PROPERTIES
    LABELS "unit;adaptive;adaptive-v2;reporting-outbox;evidence"
    TIMEOUT 10)
add_kauri_catch_test(
    test_tree_policy
    test_tree_policy.cpp)
target_compile_definitions(
    test_tree_policy PRIVATE
    KAURI_PROJECT_SOURCE_DIR="${PROJECT_SOURCE_DIR}")
set_tests_properties(
    test_tree_policy PROPERTIES
    LABELS "unit;adaptive;intentional-red;tree-policy"
    TIMEOUT 10)
add_kauri_catch_test(
    test_adaptation_manager
    test_adaptation_manager.cpp)
set_tests_properties(
    test_adaptation_manager PROPERTIES
    LABELS "unit;adaptive;intentional-red;adaptation-manager;m12"
    TIMEOUT 10)
add_kauri_catch_test(
    test_adaptation_manager_profile
    test_adaptation_manager_profile.cpp)
set_tests_properties(
    test_adaptation_manager_profile PROPERTIES
    LABELS "unit;adaptive;adaptation-manager;manager-profile;n7;n31"
    TIMEOUT 10)
add_kauri_catch_test(
    test_simple_reputation
    test_simple_reputation.cpp)
set_tests_properties(
    test_simple_reputation PROPERTIES
    LABELS "unit;adaptive;intentional-red;reputation;prototype"
    TIMEOUT 5)
add_kauri_catch_test(
    test_evidence_reputation
    test_evidence_reputation.cpp)
set_tests_properties(
    test_evidence_reputation PROPERTIES
    LABELS "unit;adaptive;intentional-red;evidence;reputation;projection"
    TIMEOUT 5)
add_kauri_catch_test(
    test_diagnosis
    test_diagnosis.cpp)
set_tests_properties(
    test_diagnosis PROPERTIES
    LABELS "unit;adaptive;diagnosis;f09a"
    TIMEOUT 10)
add_kauri_catch_test(
    test_proposal_admission
    test_proposal_admission.cpp)
add_kauri_catch_test(
    test_proposal_retirement_integration
    test_proposal_retirement_integration.cpp)
target_compile_definitions(
    test_proposal_retirement_integration PRIVATE
    KAURI_PROJECT_SOURCE_DIR="${PROJECT_SOURCE_DIR}")
set_tests_properties(
    test_proposal_retirement_integration PROPERTIES
    LABELS "unit;adaptive;intentional-red"
    TIMEOUT 5)
add_kauri_catch_test(
    test_future_proposals
    test_future_proposals.cpp)
add_kauri_catch_test(
    test_proposal_body
    test_proposal_body.cpp)
add_kauri_catch_test(
    test_block_delivery_lifecycle
    test_block_delivery_lifecycle.cpp)
add_kauri_catch_test(
    test_block_delivery_review_contract
    test_block_delivery_review_contract.cpp)
add_kauri_catch_test(
    test_block_delivery_orchestration
    test_block_delivery_orchestration.cpp)
add_kauri_catch_test(
    test_block_delivery_hotstuff_integration
    test_block_delivery_hotstuff_integration.cpp)
target_compile_definitions(
    test_block_delivery_hotstuff_integration PRIVATE
    KAURI_PROJECT_SOURCE_DIR="${PROJECT_SOURCE_DIR}")
set_tests_properties(
    test_block_delivery_orchestration
    test_block_delivery_hotstuff_integration
    PROPERTIES
    LABELS "unit;adaptive;intentional-red"
    TIMEOUT 5)

add_kauri_catch_test(
    test_aggregation
    test_aggregation.cpp)
add_kauri_catch_test(
    test_leader_progress
    test_leader_progress.cpp)
target_compile_definitions(
    test_leader_progress PRIVATE
    KAURI_PROJECT_SOURCE_DIR="${PROJECT_SOURCE_DIR}")
set_tests_properties(
    test_leader_progress PROPERTIES
    LABELS "unit;adaptive;intentional-red"
    TIMEOUT 5)
set_tests_properties(
    test_aggregation PROPERTIES
    LABELS "unit;adaptive;intentional-red;aggregation;late_votes;quorum_independence"
    TIMEOUT 5)

add_executable(
    test_promise_stack_free
    test_promise_stack_free.cpp
    $<TARGET_OBJECTS:kauri_catch_main>)
target_include_directories(test_promise_stack_free PRIVATE
    "${PROJECT_SOURCE_DIR}/include"
    "${PROJECT_SOURCE_DIR}/bls/contrib/catch")
target_compile_definitions(
    test_promise_stack_free PRIVATE CPPROMISE_USE_STACK_FREE)
if(CMAKE_CXX_COMPILER_ID STREQUAL "AppleClang")
    # The desktop test sandbox cannot initialize Apple's ASan runtime, so keep
    # the bounded lifetime probe under UBSan here. Other Clang/GNU builds use
    # both sanitizers.
    target_compile_options(test_promise_stack_free PRIVATE
        -fno-omit-frame-pointer
        -fsanitize=undefined)
    target_link_options(test_promise_stack_free PRIVATE
        -fsanitize=undefined)
elseif(CMAKE_CXX_COMPILER_ID MATCHES "Clang|GNU")
    target_compile_options(test_promise_stack_free PRIVATE
        -fno-omit-frame-pointer
        -fsanitize=address,undefined)
    target_link_options(test_promise_stack_free PRIVATE
        -fsanitize=address,undefined)
endif()
add_test(NAME test_promise_stack_free COMMAND test_promise_stack_free)
set_tests_properties(test_promise_stack_free PROPERTIES
    LABELS "unit;adaptive;intentional-red"
    TIMEOUT 5)
add_test(
    NAME test_promise_stack_free_known_divergence
    COMMAND test_promise_stack_free "[known-divergence]")
set_tests_properties(test_promise_stack_free_known_divergence PROPERTIES
    LABELS "unit;adaptive;known-divergence"
    TIMEOUT 5
    WILL_FAIL TRUE)

add_kauri_catch_test(
    test_epoch_activation
    test_epoch_activation.cpp)
target_compile_definitions(
    test_epoch_activation PRIVATE
    KAURI_PROJECT_SOURCE_DIR="${PROJECT_SOURCE_DIR}")
set_tests_properties(
    test_epoch_activation PROPERTIES
    LABELS "unit;adaptive;intentional-red;epoch-activation;d11"
    TIMEOUT 10)
add_kauri_catch_test(
    test_epoch_activation_integration
    test_epoch_activation_integration.cpp)
target_compile_definitions(
    test_epoch_activation_integration PRIVATE
    KAURI_PROJECT_SOURCE_DIR="${PROJECT_SOURCE_DIR}")
set_tests_properties(
    test_epoch_activation_integration PROPERTIES
    LABELS "unit;adaptive;intentional-red;epoch-activation;d11;integration"
    TIMEOUT 10)
add_kauri_catch_test(
    test_epoch_hotstuff_runtime_integration
    test_epoch_hotstuff_runtime_integration.cpp)
target_compile_definitions(
    test_epoch_hotstuff_runtime_integration PRIVATE
    KAURI_PROJECT_SOURCE_DIR="${PROJECT_SOURCE_DIR}")
set_tests_properties(
    test_epoch_hotstuff_runtime_integration PROPERTIES
    LABELS "unit;adaptive;intentional-red;epoch-runtime;d11;integration"
    TIMEOUT 10)
add_kauri_catch_test(
    test_epoch_hotstuff_call_sites
    test_epoch_hotstuff_call_sites.cpp)
target_compile_definitions(
    test_epoch_hotstuff_call_sites PRIVATE
    KAURI_PROJECT_SOURCE_DIR="${PROJECT_SOURCE_DIR}"
    KAURI_HOTSTUFF_APP_PATH="$<TARGET_FILE:hotstuff-app>")
add_dependencies(test_epoch_hotstuff_call_sites hotstuff-app)
set_tests_properties(
    test_epoch_hotstuff_call_sites PROPERTIES
    LABELS "unit;adaptive;intentional-red;epoch-runtime;d11;integration"
    TIMEOUT 20)

add_kauri_catch_test(
    test_structured_event
    test_structured_event.cpp)
set_tests_properties(
    test_structured_event PROPERTIES
    LABELS "unit;adaptive;intentional-red;structured-event;v13"
    TIMEOUT 10)

add_kauri_catch_test(
    test_structured_event_ownership_wiring
    test_structured_event_ownership_wiring.cpp)
target_compile_definitions(
    test_structured_event_ownership_wiring PRIVATE
    KAURI_PROJECT_SOURCE_DIR="${PROJECT_SOURCE_DIR}"
    KAURI_HOTSTUFF_APP_PATH="$<TARGET_FILE:hotstuff-app>"
    KAURI_ADAPTATION_MANAGER_PATH="$<TARGET_FILE:adaptation-manager>")
add_dependencies(
    test_structured_event_ownership_wiring
    hotstuff-app
    adaptation-manager)
set_tests_properties(
    test_structured_event_ownership_wiring PROPERTIES
    LABELS "integration;adaptive;adaptive-v2;intentional-red;structured-event;we06-c10;ownership"
    TIMEOUT 20)

add_kauri_catch_test(
    test_adaptive_v2_live_successor_wiring
    test_adaptive_v2_live_successor_wiring.cpp)
target_compile_definitions(
    test_adaptive_v2_live_successor_wiring PRIVATE
    KAURI_PROJECT_SOURCE_DIR="${PROJECT_SOURCE_DIR}"
    KAURI_HOTSTUFF_APP_PATH="$<TARGET_FILE:hotstuff-app>")
add_dependencies(
    test_adaptive_v2_live_successor_wiring
    hotstuff-app)
set_tests_properties(
    test_adaptive_v2_live_successor_wiring PROPERTIES
    LABELS "wiring;adaptive;adaptive-v2;intentional-red;live-successor;we08"
    TIMEOUT 20)

set(KAURI_COMPONENT_TEST_TARGETS
    test_commit_rule
    test_protocol_integration)

foreach(target IN LISTS KAURI_COMPONENT_TEST_TARGETS)
    add_kauri_catch_test(${target} infrastructure_sanity.cpp)
    target_compile_definitions(${target} PRIVATE
        KAURI_SANITY_COMPONENT="${target}")
endforeach()
