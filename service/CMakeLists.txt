if(CMAKE_SOURCE_DIR STREQUAL CMAKE_CURRENT_SOURCE_DIR)
    cmake_minimum_required(VERSION 3.25)
    project(picast_service VERSION 2.0.0 LANGUAGES NONE)
    option(PICAST_BUILD_TESTS "Run service Python tests via CTest when pytest is installed" ON)
endif()

find_package(Python3 COMPONENTS Interpreter QUIET)

if(Python3_Interpreter_FOUND)
    file(GLOB SERVICE_PYTHON_FILES CONFIGURE_DEPENDS
        "${CMAKE_CURRENT_SOURCE_DIR}/src/*.py"
        "${CMAKE_CURRENT_SOURCE_DIR}/test/*.py"
    )

    add_custom_target(picast_service ALL
        COMMAND "${Python3_EXECUTABLE}" -B -c
            "import ast, pathlib; [ast.parse(p.read_text(encoding='utf-8'), str(p)) for p in sorted(pathlib.Path(r'${CMAKE_CURRENT_SOURCE_DIR}').rglob('*.py'))]"
        SOURCES ${SERVICE_PYTHON_FILES}
        COMMENT "Validating picast Python 3.13+ service syntax"
        VERBATIM
    )

    if(PICAST_BUILD_TESTS)
        enable_testing()
        execute_process(
            COMMAND "${Python3_EXECUTABLE}" -c "import pytest"
            RESULT_VARIABLE _picast_has_pytest
            OUTPUT_QUIET
            ERROR_QUIET
        )
        if(_picast_has_pytest EQUAL 0)
            add_test(
                NAME picast.service_tests
                COMMAND "${Python3_EXECUTABLE}" -m pytest
                WORKING_DIRECTORY "${CMAKE_CURRENT_SOURCE_DIR}"
            )
        else()
            message(STATUS "picast_service: pytest not installed in ${Python3_EXECUTABLE}; skipping CTest registration (run `cd service && pytest`)")
        endif()
    endif()
endif()

install(DIRECTORY "${CMAKE_CURRENT_SOURCE_DIR}/src/"
    DESTINATION "lib/picast/python/picast"
    PATTERN "__pycache__" EXCLUDE
    PATTERN "*.pyc" EXCLUDE
)
