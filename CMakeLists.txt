# Copyright 2018 VMware
# Copyright 2018 Ted Yin
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

cmake_minimum_required(VERSION 3.16)
project(hotstuff)
set(CMAKE_CXX_STANDARD 17)
set(CMAKE_CXX_STANDARD_REQUIRED ON)
set(BUILD_SHARED_LIBS OFF CACHE BOOL "Build bundled dependencies statically" FORCE)

# The example client still uses std::random_shuffle. libc++ hides that legacy
# API in C++17 unless this compatibility switch is enabled. Keeping the API
# available here preserves the existing client behavior; the adaptive
# experiment will replace the shuffle deterministically in its own task.
if(APPLE AND CMAKE_CXX_COMPILER_ID MATCHES "Clang")
    add_compile_definitions(_LIBCPP_ENABLE_CXX17_REMOVED_RANDOM_SHUFFLE)
endif()

# RELIC's pinned revision still declares compatibility with CMake versions
# older than 3.5. CMake 4 removed that compatibility unless the embedding
# project supplies an explicit policy floor.
if(CMAKE_VERSION VERSION_GREATER_EQUAL 4.0 AND
   NOT DEFINED CMAKE_POLICY_VERSION_MINIMUM)
    set(CMAKE_POLICY_VERSION_MINIMUM 3.5 CACHE STRING
        "Minimum policy version for legacy bundled dependencies")
endif()

include(CTest)

set(CMAKE_MODULE_PATH ${CMAKE_MODULE_PATH} "${CMAKE_SOURCE_DIR}/salticidae/cmake/Modules/")
set(CMAKE_MODULE_PATH ${CMAKE_MODULE_PATH} ${CMAKE_CURRENT_SOURCE_DIR}/cmake_modules)

# BLS extra:
set(sodium_USE_STATIC_LIBS TRUE)
find_package(sodium REQUIRED)
message(STATUS "Found libsodium")
message(STATUS "Sodium include dir = ${sodium_INCLUDE_DIR}")
set(BLSALLOC_SODIUM "1" CACHE STRING "")
set(SODIUM_STATIC "1" CACHE STRING "")
include_directories(${sodium_INCLUDE_DIR})

find_package(gmp)
if (GMP_FOUND)
    message(STATUS "Found libgmp")
    include_directories(${GMP_INCLUDE_DIR})
    set(ARITH "gmp" CACHE STRING "")
else()
    set(ARITH "easy" CACHE STRING "")
endif()

set(TIMER "CYCLE" CACHE STRING "")
set(CHECK "off" CACHE STRING "")
set(VERBS "off" CACHE STRING "")
set(ALLOC "AUTO" CACHE STRING "")
set(SHLIB "OFF" CACHE STRING "")
set(MULTI "PTHREAD" CACHE STRING "")
set(DOCUM "off" CACHE STRING "")

set(FP_PRIME 381 CACHE STRING "")

IF (${CMAKE_SYSTEM_NAME} MATCHES "Darwin")
    set(DSEED "UDEV" CACHE STRING "")
    set(FP_QNRES "off" CACHE STRING "")
ELSEIF (${CMAKE_SYSTEM_NAME} MATCHES "Windows")
    set(SEED "WCGR" CACHE STRING "")
    set(FP_QNRES "on" CACHE STRING "")
ELSE()
    set(DSEED "UDEV" CACHE STRING "")
    set(FP_QNRES "on" CACHE STRING "")
ENDIF()
set(STBIN "OFF" CACHE STRING "" FORCE)

set(FP_METHD "INTEG;INTEG;INTEG;MONTY;LOWER;SLIDE" CACHE STRING "")
set(COMP "-O3 -funroll-loops -fomit-frame-pointer" CACHE STRING "")
set(FP_PMERS "off" CACHE STRING "")
set(FPX_METHD "INTEG;INTEG;LAZYR" CACHE STRING "")
set(EP_PLAIN "off" CACHE STRING "")
set(EP_SUPER "off" CACHE STRING "")
# Disable relic tests and benchmarks
set(TESTS 0 CACHE STRING "")
set(BENCH 0 CACHE STRING "")
set(QUIET 1 CACHE STRING "")
set(PP_EXT "LAZYR" CACHE STRING "")
set(PP_METHD "LAZYR;OATEP" CACHE STRING "")

include(FetchContent)
FetchContent_Declare(
    relic
    GIT_REPOSITORY https://github.com/relic-toolkit/relic.git
    GIT_TAG ed485dccb23f9f5615eac5c71b3e37bd1bc8fb3e
    PATCH_COMMAND
        ${CMAKE_COMMAND}
        -DRELIC_SOURCE_DIR=<SOURCE_DIR>
        -P ${CMAKE_CURRENT_SOURCE_DIR}/cmake_modules/PatchRelicBlake2.cmake
)
FetchContent_MakeAvailable(relic)

add_subdirectory(bls/src)

include_directories(
        ${INCLUDE_DIRECTORIES}
        salticidae/include
        ${CMAKE_CURRENT_BINARY_DIR}/include
        ${CMAKE_CURRENT_BINARY_DIR}/salticidae/include
        ${CMAKE_CURRENT_BINARY_DIR}/salticidae/include/salticidae
        ${CMAKE_CURRENT_SOURCE_DIR}/secp256k1/include
        ${relic_SOURCE_DIR}/include
        ${relic_BINARY_DIR}/include
        ${CMAKE_CURRENT_SOURCE_DIR}/../contrib/catch
        ${GMP_INCLUDES}
)

# ---------------------------

# Salticidae's bundled CMake file links a target named `uv` instead of the
# absolute path reported by FindLibuv. Define that imported target up front so
# Homebrew and other non-system libuv installations link reproducibly.
find_package(Libuv 1.10.0 REQUIRED)
if(NOT TARGET uv)
    add_library(uv UNKNOWN IMPORTED GLOBAL)
    set_target_properties(uv PROPERTIES
        IMPORTED_LOCATION "${LIBUV_LIBRARY}"
        INTERFACE_INCLUDE_DIRECTORIES "${LIBUV_INCLUDE_DIR}")
endif()
find_package(OpenSSL REQUIRED)
if(NOT TARGET crypto)
    add_library(crypto INTERFACE IMPORTED GLOBAL)
    set_property(TARGET crypto PROPERTY
        INTERFACE_LINK_LIBRARIES OpenSSL::Crypto)
endif()
if(NOT TARGET ssl)
    add_library(ssl INTERFACE IMPORTED GLOBAL)
    set_property(TARGET ssl PROPERTY
        INTERFACE_LINK_LIBRARIES OpenSSL::SSL)
endif()
add_subdirectory(salticidae)

find_package(Threads REQUIRED)

# Use the bundled secp256k1 CMake project directly. The former ExternalProject
# used a relative SOURCE_DIR (resolved below the binary directory) and required
# undeclared Autotools programs even though the submodule provides CMake.
set(SECP256K1_DISABLE_SHARED ON CACHE BOOL "" FORCE)
set(SECP256K1_ENABLE_MODULE_RECOVERY ON CACHE BOOL "" FORCE)
set(SECP256K1_BUILD_BENCHMARK OFF CACHE BOOL "" FORCE)
set(SECP256K1_BUILD_TESTS OFF CACHE BOOL "" FORCE)
set(SECP256K1_BUILD_EXHAUSTIVE_TESTS OFF CACHE BOOL "" FORCE)
set(SECP256K1_BUILD_CTIME_TESTS OFF CACHE BOOL "" FORCE)
set(SECP256K1_BUILD_EXAMPLES OFF CACHE BOOL "" FORCE)
set(SECP256K1_INSTALL OFF CACHE BOOL "" FORCE)
add_subdirectory(
    "${CMAKE_CURRENT_SOURCE_DIR}/secp256k1"
    "${CMAKE_CURRENT_BINARY_DIR}/secp256k1")

# add libraries

include_directories(./)
include_directories(include)
add_library(hotstuff
    OBJECT
    src/adaptation.cpp
    src/adaptation_manager.cpp
    src/adaptive_v2_epoch_factory.cpp
    src/adaptive_v2_manager_controller.cpp
    src/adaptive_v2_manager_ingress.cpp
    src/adaptive_v2_reporting_outbox.cpp
    src/adaptive_v2_response_evidence.cpp
    src/adaptive_v2_readiness_wire.cpp
    src/adaptive_v2_selection.cpp
    src/aggregation.cpp
    src/util.cpp
    src/block_delivery.cpp
    src/block_delivery_completion.cpp
    src/block_delivery_orchestration.cpp
    src/client.cpp
    src/configuration.cpp
    src/crypto.cpp
    src/entity.cpp
    src/epoch_activation.cpp
    src/epoch_change.cpp
    src/epoch_change_bundle.cpp
    src/epoch_change_inbox.cpp
    src/epoch_live_binding.cpp
    src/epoch_runtime.cpp
    src/epoch_runtime_wiring.cpp
    src/epoch_store.cpp
    src/epoch_wire.cpp
    src/evidence.cpp
    src/evidence_lifecycle.cpp
    src/evidence_lifecycle_wire.cpp
    src/evidence_ingress.cpp
    src/evidence_reporter.cpp
    src/evidence_reputation.cpp
    src/exact_vote_handler.cpp
    src/future_proposal_buffer.cpp
    src/pending_exact_contribution_buffer.cpp
    src/proposal_evidence_index.cpp
    src/proposal_context.cpp
    src/proposal_admission.cpp
    src/proposal_body.cpp
    src/response_attempt.cpp
    src/simple_reputation.cpp
    src/structured_event.cpp
    src/tree_policy.cpp
    src/consensus.cpp
    src/hotstuff.cpp
    src/leader_progress.cpp
    src/vote_identity.cpp
)

add_library(hotstuff_static STATIC $<TARGET_OBJECTS:hotstuff>)
set_target_properties(hotstuff_static PROPERTIES OUTPUT_NAME "hotstuff")
target_link_libraries(hotstuff_static PRIVATE salticidae_static secp256k1 crypto ${CMAKE_THREAD_LIBS_INIT} ${GMP_LIBRARIES} ${GMPXX_LIBRARIES} blstmp relic_s pthread sodium)

if(BUILD_TESTING)
    add_subdirectory(test)
endif()

if(NOT CMAKE_BUILD_TYPE AND NOT CMAKE_CONFIGURATION_TYPES)
    set(CMAKE_BUILD_TYPE "Release")
endif()

option(HOTSTUFF_DEBUG_LOG "enable debug log" OFF)
option(HOTSTUFF_NORMAL_LOG "enable normal log" ON)
option(HOTSTUFF_PROTO_LOG "enable protocol log" OFF)
option(HOTSTUFF_MSG_STAT "enable message statistics" ON)
option(HOTSTUFF_BLK_PROFILE "enable block profiling" OFF)
option(HOTSTUFF_TWO_STEP "use two-step HotStuff (instead of three-step HS)" OFF)
option(BUILD_EXAMPLES "build examples" ON)

configure_file(src/config.h.in include/hotstuff/config.h @ONLY)

# build examples
if(BUILD_EXAMPLES)
    add_subdirectory(examples)
endif()

# build tools
add_executable(hotstuff-keygen
    src/hotstuff_keygen.cpp)
target_link_libraries(hotstuff-keygen hotstuff_static blstmp relic_s relic_s pthread sodium)

add_executable(hotstuff-tls-keygen
    src/hotstuff_tls_keygen.cpp)
target_link_libraries(hotstuff-tls-keygen hotstuff_static blstmp relic_s pthread sodium)

find_package(Doxygen)
if (DOXYGEN_FOUND)
    add_custom_target(doc
        ${DOXYGEN_EXECUTABLE} doc/doxygen.conf WORKING_DIRECTORY ${CMAKE_CURRENT_SOURCE_DIR})
endif(DOXYGEN_FOUND)

#set(CMAKE_CXX_FLAGS_RELEASE "${CMAKE_CXX_FLAGS_RELEASE} -no-pie -pg")
set(CMAKE_CXX_FLAGS_RELEASE "${CMAKE_CXX_FLAGS_RELEASE} -W -Wall -Wextra -pedantic -Wsuggest-override -O3")
set(CMAKE_CXX_FLAGS_DEBUG "${CMAKE_CXX_FLAGS_DEBUG} -W -Wall -Wextra -pedantic -Wsuggest-override")

macro(remove_cxx_flag flag)
  string(REPLACE "${flag}" "" CMAKE_CXX_FLAGS_RELEASE "${CMAKE_CXX_FLAGS_RELEASE}")
endmacro()

remove_cxx_flag("-DNDEBUG")
