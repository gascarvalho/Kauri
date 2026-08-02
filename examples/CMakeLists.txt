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
