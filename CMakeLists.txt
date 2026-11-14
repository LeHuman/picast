cmake_minimum_required(VERSION 3.25)

project(
    picast
    VERSION 2.0.0
    DESCRIPTION "Low-latency Miracast / Wi-Fi Display receiver for Raspberry Pi OS Trixie"
    LANGUAGES CXX
)

include("${CMAKE_CURRENT_LIST_DIR}/cmake/picast_defaults.cmake")
picast_configure_common_options()

if(PICAST_BUILD_TESTS)
    enable_testing()
endif()

# Native C++26 media engine (static + LTO + --gc-sections) + Python 3.13+ systemd service
add_subdirectory(modules/platform_probe)
add_subdirectory(modules/rtp_jitter)
add_subdirectory(modules/media_pipeline)
add_subdirectory(app)
add_subdirectory(service)

# ---------------------------------------------------------------------------
# Systemd service installation & .deb packaging (Raspberry Pi OS Trixie)
# ---------------------------------------------------------------------------
include(GNUInstallDirs)

install(PROGRAMS "${CMAKE_CURRENT_SOURCE_DIR}/packaging/picast-cli"
    DESTINATION ${CMAKE_INSTALL_BINDIR}
    RENAME picast
)
install(FILES "${CMAKE_CURRENT_SOURCE_DIR}/packaging/picast.service"
    DESTINATION "/lib/systemd/system"
)
install(FILES "${CMAKE_CURRENT_SOURCE_DIR}/packaging/picast.conf.default"
    DESTINATION "/etc/picast"
    RENAME picast.conf
)
install(PROGRAMS "${CMAKE_CURRENT_SOURCE_DIR}/packaging/picast-restore-network.sh"
    DESTINATION "/usr/lib/picast"
)

set(CPACK_GENERATOR "DEB")
set(CPACK_PACKAGE_NAME "picast")
set(CPACK_PACKAGE_VERSION "${PROJECT_VERSION}")
set(CPACK_PACKAGE_VENDOR "LeHuman")
set(CPACK_PACKAGE_CONTACT "picast maintainers <maintainers@example.invalid>")
set(CPACK_PACKAGE_DESCRIPTION_SUMMARY "Modern low-latency Miracast (Wi-Fi Display & MICE) receiver for Raspberry Pi OS Trixie")
set(CPACK_PACKAGING_INSTALL_PREFIX "/usr")
set(CPACK_DEBIAN_PACKAGE_SECTION "video")
set(CPACK_DEBIAN_PACKAGE_PRIORITY "optional")
set(CPACK_DEBIAN_FILE_NAME DEB-DEFAULT)
set(CPACK_DEBIAN_PACKAGE_SHLIBDEPS ON)
set(CPACK_DEBIAN_PACKAGE_DEPENDS
    "python3 (>= 3.13), wpasupplicant, iw, dnsmasq-base, iproute2, avahi-utils, gstreamer1.0-tools, gstreamer1.0-plugins-base, gstreamer1.0-plugins-good, gstreamer1.0-plugins-bad, gstreamer1.0-libav, gstreamer1.0-alsa"
)
set(CPACK_DEBIAN_PACKAGE_RECOMMENDS
    "gstreamer1.0-plugins-ugly, pipewire-alsa, zram-tools"
)
set(CPACK_DEBIAN_PACKAGE_CONTROL_EXTRA
    "${CMAKE_CURRENT_SOURCE_DIR}/packaging/conffiles;${CMAKE_CURRENT_SOURCE_DIR}/packaging/postinst;${CMAKE_CURRENT_SOURCE_DIR}/packaging/prerm;${CMAKE_CURRENT_SOURCE_DIR}/packaging/postrm"
)
include(CPack)
