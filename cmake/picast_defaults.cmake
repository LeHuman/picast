# Shared safety-critical compiler, optimization, LTO, and section-GC helper
# Used by both the root project and standalone per-module / app builds (Linux / Raspberry Pi).

include_guard(GLOBAL)

include(CheckIPOSupported)

function(picast_configure_common_options)
    set(CMAKE_CXX_STANDARD 26 PARENT_SCOPE)
    set(CMAKE_CXX_STANDARD_REQUIRED ON PARENT_SCOPE)
    set(CMAKE_CXX_EXTENSIONS OFF PARENT_SCOPE)
    set(CMAKE_EXPORT_COMPILE_COMMANDS ON PARENT_SCOPE)

    option(PICAST_BUILD_TESTS "Build C++ unit tests for CTest" ON)
    option(PICAST_ENABLE_GSTREAMER "Enable GStreamer 1.0 media backend when available" ON)
    option(PICAST_WARNINGS_AS_ERRORS "Treat compiler warnings as errors" OFF)
    option(PICAST_ENABLE_LTO "Enable Link-Time Optimization on Release builds when supported" ON)
    set(PICAST_TARGET_CPU "" CACHE STRING "Optional GCC/Clang -mcpu target (e.g. cortex-a53, cortex-a76)")

    if(NOT TARGET picast_compile_options)
        add_library(picast_compile_options INTERFACE)
        target_compile_options(picast_compile_options INTERFACE
            -Wall
            -Wextra
            -Wpedantic
            -Wconversion
            -Wsign-conversion
            -Wshadow
            -Wnon-virtual-dtor
            -Wold-style-cast
            -Wnull-dereference
            -ffunction-sections
            -fdata-sections
            $<$<BOOL:${PICAST_WARNINGS_AS_ERRORS}>:-Werror>
        )
        if(PICAST_TARGET_CPU)
            target_compile_options(picast_compile_options INTERFACE "-mcpu=${PICAST_TARGET_CPU}")
        endif()
        target_link_options(picast_compile_options INTERFACE
            $<$<CONFIG:Release,RelWithDebInfo,MinSizeRel>:-Wl,--gc-sections>
        )
        add_library(picast::compile_options ALIAS picast_compile_options)
    endif()

    if(PICAST_ENABLE_LTO)
        check_ipo_supported(RESULT _picast_ipo_ok OUTPUT _picast_ipo_err LANGUAGES CXX)
        if(_picast_ipo_ok)
            set(CMAKE_INTERPROCEDURAL_OPTIMIZATION_RELEASE ON PARENT_SCOPE)
            set(CMAKE_INTERPROCEDURAL_OPTIMIZATION_RELWITHDEBINFO ON PARENT_SCOPE)
            set(CMAKE_INTERPROCEDURAL_OPTIMIZATION_MINSIZEREL ON PARENT_SCOPE)
        endif()
    endif()
endfunction()
