# Copyright (c) 2019 Ted Yin
#
# Permission is hereby granted, free of charge, to any person obtaining a copy of
# this software and associated documentation files (the "Software"), to deal in
# the Software without restriction, including without limitation the rights to
# use, copy, modify, merge, publish, distribute, sublicense, and/or sell copies
# of the Software, and to permit persons to whom the Software is furnished to do
# so, subject to the following conditions:
#
# The above copyright notice and this permission notice shall be included in all
# copies or substantial portions of the Software.
#
# THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
# IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
# FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
# AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
# LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
# OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
# SOFTWARE.

add_executable(hotstuff-app hotstuff_app.cpp)
add_executable(hotstuff-client hotstuff_client.cpp)
add_executable(adaptation-manager adaptation_manager.cpp)
add_executable(epoch-profile-digest epoch_profile_digest.cpp)
add_executable(static-epoch0-digest static_epoch0_digest.cpp)
add_executable(n7-epoch0-treefile-digest n7_epoch0_treefile_digest.cpp)
add_executable(operator-capacity-snapshot-digest operator_capacity_snapshot_digest.cpp)
add_executable(operator-capacity-label-envelope-verify operator_capacity_label_envelope_verify.cpp)
add_executable(operator-capacity-label-envelope-sign operator_capacity_label_envelope_sign.cpp)
add_executable(operator-capacity-authorization-verify operator_capacity_authorization_verify.cpp)
add_executable(operator-capacity-identity-parity-verify operator_capacity_identity_parity_verify.cpp)
add_executable(struc31-placement-matrix struc31_placement_matrix.cpp)

target_compile_options(hotstuff-app PUBLIC -Wl,-no_pie)
target_compile_options(hotstuff-client PUBLIC -Wl,-no_pie)

target_include_directories(hotstuff-app  PUBLIC  ${relic_BINARY_DIR}/include)
target_include_directories(hotstuff-app  PUBLIC  ${relic_SOURCE_DIR}/include)
target_include_directories(hotstuff-app  PUBLIC  ${GMP_INCLUDES})
target_include_directories(hotstuff-app  PUBLIC  ${CMAKE_CURRENT_SOURCE_DIR}/../bls/contrib/catch)
target_include_directories(hotstuff-app  PUBLIC  ${INCLUDE_DIRECTORIES})

target_include_directories(hotstuff-app  PUBLIC ../bls/src)

TARGET_LINK_LIBRARIES(hotstuff-app PRIVATE ${GMP_LIBRARIES} ${GMPXX_LIBRARIES} hotstuff_static blstmp relic_s pthread sodium)

target_include_directories(hotstuff-client  PUBLIC  ${relic_BINARY_DIR}/include)
target_include_directories(hotstuff-client  PUBLIC  ${relic_SOURCE_DIR}/include)
target_include_directories(hotstuff-client  PUBLIC  ${GMP_INCLUDES})
target_include_directories(hotstuff-client  PUBLIC  ${CMAKE_CURRENT_SOURCE_DIR}/../bls/contrib/catch)
target_include_directories(hotstuff-client  PUBLIC  ${INCLUDE_DIRECTORIES})

target_include_directories(hotstuff-client  PUBLIC ../bls/src)

TARGET_LINK_LIBRARIES(hotstuff-client PRIVATE ${GMP_LIBRARIES} ${GMPXX_LIBRARIES} hotstuff_static blstmp relic_s pthread sodium)

target_include_directories(adaptation-manager PUBLIC ${relic_BINARY_DIR}/include)
target_include_directories(adaptation-manager PUBLIC ${relic_SOURCE_DIR}/include)
target_include_directories(adaptation-manager PUBLIC ${GMP_INCLUDES})
target_include_directories(
    adaptation-manager PUBLIC
    ${CMAKE_CURRENT_SOURCE_DIR}/../bls/contrib/catch)
target_include_directories(adaptation-manager PUBLIC ${INCLUDE_DIRECTORIES})
target_include_directories(adaptation-manager PUBLIC ../bls/src)

TARGET_LINK_LIBRARIES(
    adaptation-manager PRIVATE
    ${GMP_LIBRARIES} ${GMPXX_LIBRARIES}
    hotstuff_static blstmp relic_s pthread sodium)

target_include_directories(
    epoch-profile-digest PUBLIC ${relic_BINARY_DIR}/include)
target_include_directories(
    epoch-profile-digest PUBLIC ${relic_SOURCE_DIR}/include)
target_include_directories(epoch-profile-digest PUBLIC ${GMP_INCLUDES})
target_include_directories(
    epoch-profile-digest PUBLIC
    ${CMAKE_CURRENT_SOURCE_DIR}/../bls/contrib/catch)
target_include_directories(epoch-profile-digest PUBLIC ${INCLUDE_DIRECTORIES})
target_include_directories(epoch-profile-digest PUBLIC ../bls/src)

TARGET_LINK_LIBRARIES(
    epoch-profile-digest PRIVATE
    ${GMP_LIBRARIES} ${GMPXX_LIBRARIES}
    hotstuff_static blstmp relic_s pthread sodium)

target_include_directories(static-epoch0-digest PUBLIC ${relic_BINARY_DIR}/include ${relic_SOURCE_DIR}/include ${GMP_INCLUDES} ${INCLUDE_DIRECTORIES} ../bls/src)
target_include_directories(n7-epoch0-treefile-digest PUBLIC ${relic_BINARY_DIR}/include ${relic_SOURCE_DIR}/include ${GMP_INCLUDES} ${INCLUDE_DIRECTORIES} ../bls/src)
target_include_directories(operator-capacity-snapshot-digest PUBLIC ${relic_BINARY_DIR}/include ${relic_SOURCE_DIR}/include ${GMP_INCLUDES} ${INCLUDE_DIRECTORIES} ../bls/src)
target_include_directories(operator-capacity-label-envelope-verify PUBLIC ${relic_BINARY_DIR}/include ${relic_SOURCE_DIR}/include ${GMP_INCLUDES} ${INCLUDE_DIRECTORIES} ../bls/src)
target_include_directories(operator-capacity-label-envelope-sign PUBLIC ${relic_BINARY_DIR}/include ${relic_SOURCE_DIR}/include ${GMP_INCLUDES} ${INCLUDE_DIRECTORIES} ../bls/src)
target_include_directories(operator-capacity-authorization-verify PUBLIC ${relic_BINARY_DIR}/include ${relic_SOURCE_DIR}/include ${GMP_INCLUDES} ${INCLUDE_DIRECTORIES} ../bls/src)
target_include_directories(operator-capacity-identity-parity-verify PUBLIC ${relic_BINARY_DIR}/include ${relic_SOURCE_DIR}/include ${GMP_INCLUDES} ${INCLUDE_DIRECTORIES} ../bls/src)
TARGET_LINK_LIBRARIES(static-epoch0-digest PRIVATE ${GMP_LIBRARIES} ${GMPXX_LIBRARIES} hotstuff_static blstmp relic_s pthread sodium)
TARGET_LINK_LIBRARIES(n7-epoch0-treefile-digest PRIVATE ${GMP_LIBRARIES} ${GMPXX_LIBRARIES} hotstuff_static blstmp relic_s pthread sodium)
TARGET_LINK_LIBRARIES(operator-capacity-snapshot-digest PRIVATE ${GMP_LIBRARIES} ${GMPXX_LIBRARIES} hotstuff_static blstmp relic_s pthread sodium)
TARGET_LINK_LIBRARIES(operator-capacity-label-envelope-verify PRIVATE ${GMP_LIBRARIES} ${GMPXX_LIBRARIES} hotstuff_static blstmp relic_s pthread sodium)
TARGET_LINK_LIBRARIES(operator-capacity-label-envelope-sign PRIVATE ${GMP_LIBRARIES} ${GMPXX_LIBRARIES} hotstuff_static blstmp relic_s pthread sodium)
TARGET_LINK_LIBRARIES(operator-capacity-authorization-verify PRIVATE ${GMP_LIBRARIES} ${GMPXX_LIBRARIES} hotstuff_static blstmp relic_s pthread sodium)
TARGET_LINK_LIBRARIES(operator-capacity-identity-parity-verify PRIVATE ${GMP_LIBRARIES} ${GMPXX_LIBRARIES} hotstuff_static blstmp relic_s pthread sodium)

target_include_directories(
    struc31-placement-matrix PRIVATE ${relic_BINARY_DIR}/include)
target_include_directories(
    struc31-placement-matrix PRIVATE ${relic_SOURCE_DIR}/include)
target_include_directories(struc31-placement-matrix PRIVATE ${GMP_INCLUDES})
target_include_directories(
    struc31-placement-matrix PRIVATE
    ${CMAKE_CURRENT_SOURCE_DIR}/../bls/contrib/catch)
target_include_directories(
    struc31-placement-matrix PRIVATE ${INCLUDE_DIRECTORIES})
target_include_directories(struc31-placement-matrix PRIVATE ../bls/src)

TARGET_LINK_LIBRARIES(
    struc31-placement-matrix PRIVATE
    ${GMP_LIBRARIES} ${GMPXX_LIBRARIES}
    hotstuff_static blstmp relic_s pthread sodium)
