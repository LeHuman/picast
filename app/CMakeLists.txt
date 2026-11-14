if(CMAKE_SOURCE_DIR STREQUAL CMAKE_CURRENT_SOURCE_DIR)
    cmake_minimum_required(VERSION 3.25)
    project(picast_app VERSION 2.0.0 LANGUAGES CXX)
endif()

include("${CMAKE_CURRENT_LIST_DIR}/../cmake/picast_defaults.cmake")
picast_configure_common_options()

if(NOT TARGET picast::platform_probe)
    add_subdirectory("${CMAKE_CURRENT_LIST_DIR}/../modules/platform_probe" "${CMAKE_CURRENT_BINARY_DIR}/deps/platform_probe")
endif()
if(NOT TARGET picast::rtp_jitter)
    add_subdirectory("${CMAKE_CURRENT_LIST_DIR}/../modules/rtp_jitter" "${CMAKE_CURRENT_BINARY_DIR}/deps/rtp_jitter")
endif()
if(NOT TARGET picast::media_pipeline)
    add_subdirectory("${CMAKE_CURRENT_LIST_DIR}/../modules/media_pipeline" "${CMAKE_CURRENT_BINARY_DIR}/deps/media_pipeline")
endif()

add_library(picast_app_lib STATIC
    "${CMAKE_CURRENT_SOURCE_DIR}/source/app_config.cpp"
)
target_include_directories(picast_app_lib PUBLIC
    "${CMAKE_CURRENT_SOURCE_DIR}/include"
)
target_link_libraries(picast_app_lib PUBLIC
    picast::compile_options
    picast::platform_probe
    picast::rtp_jitter
    picast::media_pipeline
)

add_executable(picast-player
    "${CMAKE_CURRENT_SOURCE_DIR}/source/main.cpp"
)
target_link_libraries(picast-player PRIVATE picast_app_lib)

if(PICAST_BUILD_TESTS)
    enable_testing()
    file(GLOB_RECURSE APP_TEST_SOURCES CONFIGURE_DEPENDS
        "${CMAKE_CURRENT_SOURCE_DIR}/test/*.cpp"
    )
    add_executable(picast_app_tests ${APP_TEST_SOURCES})
    target_link_libraries(picast_app_tests PRIVATE picast_app_lib)
    add_test(NAME picast.app_tests COMMAND picast_app_tests)
endif()

include(GNUInstallDirs)
install(TARGETS picast-player
    RUNTIME DESTINATION ${CMAKE_INSTALL_BINDIR}
)
