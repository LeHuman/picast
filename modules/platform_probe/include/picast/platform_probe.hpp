/**
 * @file platform_probe.hpp
 * @brief Hardware capability discovery, SoC classification, and DRM sysfs EDID probing.
 */

#pragma once

#include <cstdint>
#include <expected>
#include <filesystem>
#include <optional>
#include <string>
#include <string_view>
#include <vector>

namespace picast {

/** @brief Supported Raspberry Pi board families and development hosts. */
enum class PiBoardModel {
    Unknown,
    PiZero2W,
    Pi3,
    Pi4,
    Pi5,
    GenericLinux,
};

/** @brief Video decoder backends supported by GStreamer 1.0 on Debian Trixie. */
enum class DecoderBackend {
    Auto,
    V4l2H264M2M,
    V4l2HevcStateless,
    SoftwareNeonH264,
    SoftwareHevc,
};

/** @brief Target audio sink backend identifiers. */
enum class AudioSinkKind {
    Auto,
    Hdmi,
    HeadphoneJack,
    Alsa,
    PipeWire,
};

/** @brief Filesystem paths inspected during platform probing (overridable in unit tests). */
struct ProbePaths {
    std::filesystem::path device_tree_model{"/proc/device-tree/model"};
    std::filesystem::path cpuinfo{"/proc/cpuinfo"};
    std::filesystem::path drm_sysfs{"/sys/class/drm"};
    std::filesystem::path dev_dir{"/dev"};
    std::filesystem::path v4l2_sysfs{"/sys/class/video4linux"};
    std::filesystem::path override_edid{};
};

/** @brief Aggregated runtime hardware and display capabilities for the active host. */
struct HardwareCapabilities {
    PiBoardModel board{PiBoardModel::Unknown};
    std::string model_name{"Unknown"};
    bool has_v4l2_h264_m2m{false};
    bool has_v4l2_hevc_stateless{false};
    bool has_5ghz_wifi{false};
    bool supports_ht40{false};
    bool supports_1080p60_hw{false};
    bool drm_connected{false};
    std::string v4l2_h264_device{};
    std::string v4l2_hevc_device{};
    std::string drm_connector{};
    DecoderBackend recommended_h264{DecoderBackend::SoftwareNeonH264};
    std::vector<std::uint8_t> edid{};
};

/**
 * @brief Convert a board model enum to a human-readable identifier.
 * @param model Board classification value.
 * @return Null-terminated static string view naming the board.
 */
[[nodiscard]] std::string_view to_string(PiBoardModel model) noexcept;

/**
 * @brief Convert a decoder backend enum to its GStreamer element name.
 * @param backend Decoder backend enum.
 * @return GStreamer element name or `"auto"`.
 */
[[nodiscard]] std::string_view to_string(DecoderBackend backend) noexcept;

/**
 * @brief Convert an audio sink kind enum to a configuration string.
 * @param sink Audio sink enum.
 * @return Short sink identifier (`hdmi`, `jack`, `alsa`, `pipewire`, `auto`).
 */
[[nodiscard]] std::string_view to_string(AudioSinkKind sink) noexcept;

/**
 * @brief Classify a Raspberry Pi board or host environment from procfs strings.
 * @param model_str Contents of `/proc/device-tree/model` (empty on Windows/containers).
 * @param cpuinfo_str Contents of `/proc/cpuinfo`.
 * @return Identified `PiBoardModel`.
 */
[[nodiscard]] PiBoardModel classify_board_model(
    std::string_view model_str,
    std::string_view cpuinfo_str) noexcept;

/**
 * @brief Read raw EDID bytes from a DRM sysfs connector EDID node (e.g.
 *        `/sys/class/drm/card1-HDMI-A-1/edid`).
 * @param drm_sysfs Root DRM sysfs directory.
 * @param preferred_connector Optional substring filter (e.g., `"HDMI-A-1"`).
 * @return EDID binary blob (>= 128 bytes) or descriptive error string.
 */
[[nodiscard]] std::expected<std::vector<std::uint8_t>, std::string> read_drm_edid(
    const std::filesystem::path& drm_sysfs = "/sys/class/drm",
    std::optional<std::string_view> preferred_connector = std::nullopt);

/**
 * @brief Probe board model, V4L2 decode nodes, radio capabilities, and DRM EDID state.
 * @param paths Optional path overrides (defaults inspect live procfs/sysfs/devfs).
 * @return Populated `HardwareCapabilities` structure.
 */
[[nodiscard]] HardwareCapabilities probe_hardware(const ProbePaths& paths = {});

}  // namespace picast
