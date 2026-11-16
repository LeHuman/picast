if(CMAKE_SOURCE_DIR STREQUAL CMAKE_CURRENT_SOURCE_DIR)
    cmake_minimum_required(VERSION 3.25)
    project(picast_media_pipeline VERSION 2.0.0 LANGUAGES CXX)
endif()

include("${CMAKE_CURRENT_LIST_DIR}/../../cmake/picast_defaults.cmake")
picast_configure_common_options()

if(NOT TARGET picast::platform_probe)
    add_subdirectory("${CMAKE_CURRENT_LIST_DIR}/../platform_probe" "${CMAKE_CURRENT_BINARY_DIR}/deps/platform_probe")
endif()
if(NOT TARGET picast::rtp_jitter)
    add_subdirectory("${CMAKE_CURRENT_LIST_DIR}/../rtp_jitter" "${CMAKE_CURRENT_BINARY_DIR}/deps/rtp_jitter")
endif()

set(PICAST_HAS_GSTREAMER OFF)
if(PICAST_ENABLE_GSTREAMER)
    find_package(PkgConfig QUIET)
    if(PkgConfig_FOUND)
        pkg_check_modules(GST QUIET IMPORTED_TARGET
            gstreamer-1.0
            gstreamer-app-1.0
            gstreamer-video-1.0
            glib-2.0
        )
        if(GST_FOUND)
            set(PICAST_HAS_GSTREAMER ON)
            message(STATUS "picast_media_pipeline: GStreamer 1.0 enabled (${GST_VERSION})")
        endif()
    endif()
endif()

if(NOT PICAST_HAS_GSTREAMER)
    message(STATUS "picast_media_pipeline: GStreamer dev headers not found; using no-gstreamer stub backend")
endif()

file(GLOB_RECURSE MEDIA_PIPELINE_SOURCES CONFIGURE_DEPENDS
    "${CMAKE_CURRENT_SOURCE_DIR}/source/*.cpp"
)

add_library(picast_media_pipeline STATIC ${MEDIA_PIPELINE_SOURCES})
add_library(picast::media_pipeline ALIAS picast_media_pipeline)

target_include_directories(picast_media_pipeline PUBLIC
    "${CMAKE_CURRENT_SOURCE_DIR}/include"
)
target_link_libraries(picast_media_pipeline PUBLIC
    picast::compile_options
    picast::platform_probe
    picast::rtp_jitter
)

if(PICAST_HAS_GSTREAMER)
    target_compile_definitions(picast_media_pipeline PUBLIC PICAST_HAS_GSTREAMER=1)
    target_link_libraries(picast_media_pipeline PUBLIC PkgConfig::GST)
else()
    target_compile_definitions(picast_media_pipeline PUBLIC PICAST_HAS_GSTREAMER=0)
endif()

if(PICAST_BUILD_TESTS)
    enable_testing()
    file(GLOB_RECURSE MEDIA_PIPELINE_TEST_SOURCES CONFIGURE_DEPENDS
        "${CMAKE_CURRENT_SOURCE_DIR}/test/*.cpp"
    )
    add_executable(picast_media_pipeline_tests ${MEDIA_PIPELINE_TEST_SOURCES})
    target_link_libraries(picast_media_pipeline_tests PRIVATE picast::media_pipeline)
    add_test(NAME picast.media_pipeline_tests COMMAND picast_media_pipeline_tests)
endif()
