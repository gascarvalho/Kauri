#include <string>

#include "catch.hpp"

#ifndef KAURI_SANITY_COMPONENT
#define KAURI_SANITY_COMPONENT "unknown"
#endif

TEST_CASE("component test target uses the shared Catch harness",
          "[infrastructure]")
{
    const std::string component = KAURI_SANITY_COMPONENT;

    INFO("registered component target: " << component);
    REQUIRE(component != "unknown");
    REQUIRE(component.rfind("test_", 0) == 0);
}
