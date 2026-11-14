if(CMAKE_SOURCE_DIR STREQUAL CMAKE_CURRENT_SOURCE_DIR)
    cmake_minimum_required(VERSION 3.25)
    project(picast_rtp_jitter VERSION 2.0.0 LANGUAGES CXX)
endif()

include("${CMAKE_CURRENT_LIST_DIR}/../../cmake/picast_defaults.cmake")
picast_configure_common_options()

file(GLOB_RECURSE RTP_JITTER_SOURCES CONFIGURE_DEPENDS
    "${CMAKE_CURRENT_SOURCE_DIR}/source/*.cpp"
)

add_library(picast_rtp_jitter STATIC ${RTP_JITTER_SOURCES})
add_library(picast::rtp_jitter ALIAS picast_rtp_jitter)

target_include_directories(picast_rtp_jitter PUBLIC
    "${CMAKE_CURRENT_SOURCE_DIR}/include"
)
target_link_libraries(picast_rtp_jitter PUBLIC picast::compile_options)

if(PICAST_BUILD_TESTS)
    enable_testing()
    file(GLOB_RECURSE RTP_JITTER_TEST_SOURCES CONFIGURE_DEPENDS
        "${CMAKE_CURRENT_SOURCE_DIR}/test/*.cpp"
    )
    add_executable(picast_rtp_jitter_tests ${RTP_JITTER_TEST_SOURCES})
    target_link_libraries(picast_rtp_jitter_tests PRIVATE picast::rtp_jitter)
    add_test(NAME picast.rtp_jitter_tests COMMAND picast_rtp_jitter_tests)
endif()
