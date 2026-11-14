if(CMAKE_SOURCE_DIR STREQUAL CMAKE_CURRENT_SOURCE_DIR)
    cmake_minimum_required(VERSION 3.25)
    project(picast_platform_probe VERSION 2.0.0 LANGUAGES CXX)
endif()

include("${CMAKE_CURRENT_LIST_DIR}/../../cmake/picast_defaults.cmake")
picast_configure_common_options()

file(GLOB_RECURSE PLATFORM_PROBE_SOURCES CONFIGURE_DEPENDS
    "${CMAKE_CURRENT_SOURCE_DIR}/source/*.cpp"
)

add_library(picast_platform_probe STATIC ${PLATFORM_PROBE_SOURCES})
add_library(picast::platform_probe ALIAS picast_platform_probe)

target_include_directories(picast_platform_probe PUBLIC
    "${CMAKE_CURRENT_SOURCE_DIR}/include"
)
target_link_libraries(picast_platform_probe PUBLIC picast::compile_options)

if(PICAST_BUILD_TESTS)
    enable_testing()
    file(GLOB_RECURSE PLATFORM_PROBE_TEST_SOURCES CONFIGURE_DEPENDS
        "${CMAKE_CURRENT_SOURCE_DIR}/test/*.cpp"
    )
    add_executable(picast_platform_probe_tests ${PLATFORM_PROBE_TEST_SOURCES})
    target_link_libraries(picast_platform_probe_tests PRIVATE picast::platform_probe)
    add_test(NAME picast.platform_probe_tests COMMAND picast_platform_probe_tests)
endif()
