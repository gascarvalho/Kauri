#include <fstream>
#include <sstream>
#include <string>

#include "catch.hpp"
#include "hotstuff/type.h"
#include "salticidae/buffer.h"

namespace {

std::string read_file(const std::string &path) {
    std::ifstream input(path);
    REQUIRE(input.good());
    std::ostringstream contents;
    contents << input.rdbuf();
    return contents.str();
}

std::size_t occurrences(
        const std::string &contents,
        const std::string &needle) {
    std::size_t count = 0;
    std::size_t position = 0;
    while ((position = contents.find(needle, position)) != std::string::npos) {
        ++count;
        position += needle.size();
    }
    return count;
}

} // namespace

TEST_CASE(
        "Salticidae TLS receive logging is type safe",
        "[network][salticidae][tls][regression]") {
    const auto conn_source = read_file(KAURI_SALTICIDAE_CONN_SOURCE);
    const auto util_header = read_file(KAURI_SALTICIDAE_UTIL_HEADER);

    REQUIRE(
        occurrences(
            conn_source,
            "SALTICIDAE_LOG_INFO(\"ssl recv(%d) failure: %d %d\", "
            "fd, err, errno);") == 1);
    REQUIRE(conn_source.find("failure: %d %s\", fd, err, errno)") ==
            std::string::npos);
    REQUIRE(
        occurrences(
            util_header,
            "__attribute__((format(printf, format_index, "
            "first_argument)))") == 1);
    REQUIRE(
        occurrences(
            util_header,
            "void info(const char *fmt, ...) "
            "SALTICIDAE_PRINTF_LIKE(2, 3);") == 1);
}

TEST_CASE(
        "Salticidae urgent messages bypass queued repair traffic",
        "[network][salticidae][priority][regression]") {
    salticidae::MPSCWriteBuffer buffer;
    REQUIRE(buffer.push(salticidae::bytearray_t{1}, true));
    REQUIRE(buffer.push_priority(salticidae::bytearray_t{2}, true));
    REQUIRE(buffer.push_urgent(salticidae::bytearray_t{3}, true));

    CHECK(buffer.move_pop() == salticidae::bytearray_t{3});
    CHECK(buffer.move_pop() == salticidae::bytearray_t{2});
    CHECK(buffer.move_pop() == salticidae::bytearray_t{1});

    REQUIRE(buffer.push_priority(salticidae::bytearray_t{4}, true));
    REQUIRE(buffer.push_urgent(salticidae::bytearray_t{5}, true));
    buffer.rewind(salticidae::bytearray_t{9});
    CHECK(buffer.move_pop() == salticidae::bytearray_t{9});
    CHECK(buffer.move_pop() == salticidae::bytearray_t{5});
    CHECK(buffer.move_pop() == salticidae::bytearray_t{4});
}
