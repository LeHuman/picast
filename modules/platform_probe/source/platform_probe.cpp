/**
 * @file platform_probe.cpp
 * @brief Implementation of hardware capability discovery and DRM sysfs EDID reading.
 */

#include "picast/platform_probe.hpp"

#include <algorithm>
#include <cctype>
#include <fstream>
#include <iterator>
#include <sstream>

namespace picast {

namespace {

[[nodiscard]] std::string read_text_file(const std::filesystem::path& path) {
    std::ifstream file(path, std::ios::binary);
    if (!file.is_open()) {
        return {};
    }
    constexpr std::size_t kMaxTextBytes = 65536U;
    std::string out(kMaxTextBytes, '\0');
    file.read(out.data(), static_cast<std::streamsize>(kMaxTextBytes));
    out.resize(static_cast<std::size_t>(file.gcount()));
    out.erase(std::remove(out.begin(), out.end(), '\0'), out.end());
    while (!out.empty() && (out.back() == '\n' || out.back() == '\r' || out.back() == ' ')) {
        out.pop_back();
    }
    return out;
}

[[nodiscard]] std::vector<std::uint8_t> read_binary_file(const std::filesystem::path& path) {
    std::ifstream file(path, std::ios::binary);
    if (!file.is_open()) {
        return {};
    }
    constexpr std::size_t kMaxEdidBytes = 16384U;
    std::vector<std::uint8_t> buf(kMaxEdidBytes);
    file.read(reinterpret_cast<char*>(buf.data()), static_cast<std::streamsize>(kMaxEdidBytes));
    buf.resize(static_cast<std::size_t>(file.gcount()));
    return buf;
}

[[nodiscard]] bool contains_ci(std::string_view haystack, std::string_view needle) noexcept {
    if (needle.empty() || haystack.size() < needle.size()) {
        return false;
    }
    const auto it = std::search(
        haystack.begin(),
        haystack.end(),
        needle.begin(),
        needle.end(),
        [](char a, char b) noexcept {
            return std::tolower(static_cast<unsigned char>(a)) ==
                   std::tolower(static_cast<unsigned char>(b));
        });
    return it != haystack.end();
}

[[nodiscard]] std::string find_v4l2_node_by_name(
    const std::filesystem::path& v4l2_sysfs,
    const std::filesystem::path& dev_dir,
    std::string_view driver_needle) {
    std::error_code ec;
    if (!std::filesystem::exists(v4l2_sysfs, ec)) {
        return {};
    }
    std::vector<std::filesystem::path> nodes;
    for (const auto& entry : std::filesystem::directory_iterator(v4l2_sysfs, ec)) {
        if (ec) {
            break;
        }
        nodes.push_back(entry.path());
    }
    std::sort(nodes.begin(), nodes.end());
    for (const auto& node : nodes) {
        const std::string name = read_text_file(node / "name");
        if (contains_ci(name, driver_needle)) {
            const auto dev_node = dev_dir / node.filename();
            if (std::filesystem::exists(dev_node, ec)) {
                return dev_node.string();
            }
        }
    }
    return {};
}

}  // namespace

std::string_view to_string(PiBoardModel model) noexcept {
    switch (model) {
        case PiBoardModel::PiZero2W:
            return "Raspberry Pi Zero 2 W";
        case PiBoardModel::Pi3:
            return "Raspberry Pi 3";
        case PiBoardModel::Pi4:
            return "Raspberry Pi 4";
        case PiBoardModel::Pi5:
            return "Raspberry Pi 5";
        case PiBoardModel::GenericLinux:
            return "Generic Linux";
        case PiBoardModel::Unknown:
        default:
            return "Unknown";
    }
}

std::string_view to_string(DecoderBackend backend) noexcept {
    switch (backend) {
        case DecoderBackend::V4l2H264M2M:
            return "v4l2h264dec";
        case DecoderBackend::V4l2HevcStateless:
            return "v4l2slh265dec";
        case DecoderBackend::SoftwareNeonH264:
            return "avdec_h264";
        case DecoderBackend::SoftwareHevc:
            return "avdec_h265";
        case DecoderBackend::Auto:
        default:
            return "auto";
    }
}

std::string_view to_string(AudioSinkKind sink) noexcept {
    switch (sink) {
        case AudioSinkKind::Hdmi:
            return "hdmi";
        case AudioSinkKind::HeadphoneJack:
            return "jack";
        case AudioSinkKind::Alsa:
            return "alsa";
        case AudioSinkKind::PipeWire:
            return "pipewire";
        case AudioSinkKind::Auto:
        default:
            return "auto";
    }
}

PiBoardModel classify_board_model(std::string_view model_str, std::string_view cpuinfo_str) noexcept {
    if (contains_ci(model_str, "Raspberry Pi Zero 2")) {
        return PiBoardModel::PiZero2W;
    }
    if (contains_ci(model_str, "Raspberry Pi 5") || contains_ci(model_str, "Compute Module 5") ||
        contains_ci(cpuinfo_str, "BCM2712")) {
        return PiBoardModel::Pi5;
    }
    if (contains_ci(model_str, "Raspberry Pi 4") || contains_ci(model_str, "Raspberry Pi 400") ||
        contains_ci(model_str, "Compute Module 4") || contains_ci(cpuinfo_str, "BCM2711")) {
        return PiBoardModel::Pi4;
    }
    if (contains_ci(model_str, "Raspberry Pi 3")) {
        return PiBoardModel::Pi3;
    }
    if (!model_str.empty() || !cpuinfo_str.empty()) {
        return PiBoardModel::GenericLinux;
    }
    return PiBoardModel::Unknown;
}

std::expected<std::vector<std::uint8_t>, std::string> read_drm_edid(
    const std::filesystem::path& drm_sysfs,
    std::optional<std::string_view> preferred_connector) {
    std::error_code ec;
    if (!std::filesystem::exists(drm_sysfs, ec)) {
        return std::unexpected("DRM sysfs directory unavailable: " + drm_sysfs.string());
    }

    std::vector<std::filesystem::path> connectors;
    for (const auto& entry : std::filesystem::directory_iterator(drm_sysfs, ec)) {
        if (ec) {
            break;
        }
        const std::string name = entry.path().filename().string();
        if (name.find("HDMI-A-") != std::string::npos) {
            connectors.push_back(entry.path());
        }
    }
    std::sort(connectors.begin(), connectors.end());

    if (preferred_connector.has_value()) {
        std::stable_partition(connectors.begin(), connectors.end(), [&](const auto& path) {
            return path.filename().string().find(*preferred_connector) != std::string::npos;
        });
    }

    for (const auto& connector : connectors) {
        const std::string status = read_text_file(connector / "status");
        if (!status.empty() && status != "connected") {
            continue;
        }
        auto edid = read_binary_file(connector / "edid");
        if (edid.size() >= 128U) {
            return edid;
        }
    }

    for (const auto& connector : connectors) {
        auto edid = read_binary_file(connector / "edid");
        if (edid.size() >= 128U) {
            return edid;
        }
    }

    return std::unexpected("No connected HDMI DRM EDID found in " + drm_sysfs.string());
}

HardwareCapabilities probe_hardware() {
    HardwareCapabilities caps;

    const std::filesystem::path dt_model{"/proc/device-tree/model"};
    const std::filesystem::path cpuinfo{"/proc/cpuinfo"};
    const std::filesystem::path drm_sysfs{"/sys/class/drm"};
    const std::filesystem::path dev_dir{"/dev"};
    const std::filesystem::path v4l2_sysfs{"/sys/class/video4linux"};
    const std::filesystem::path firmware_edid{"/lib/firmware/picast-edid.bin"};

    const std::string model_str = read_text_file(dt_model);
    const std::string cpuinfo_str = read_text_file(cpuinfo);
    caps.board = classify_board_model(model_str, cpuinfo_str);
    caps.model_name = !model_str.empty() ? model_str : std::string{to_string(caps.board)};

    // Pi Zero 2 W hardware is 2.4 GHz-only (802.11n); Pi 3B+/Pi 4/Pi 5 support 5 GHz + HT40.
    switch (caps.board) {
        case PiBoardModel::PiZero2W:
            caps.has_5ghz_wifi = false;
            caps.supports_ht40 = false;
            caps.supports_1080p60_hw = false;
            break;
        case PiBoardModel::Pi3:
            caps.has_5ghz_wifi = contains_ci(model_str, "Plus") || contains_ci(model_str, "3 A+") ||
                                 contains_ci(model_str, "3 B+");
            caps.supports_ht40 = caps.has_5ghz_wifi;
            caps.supports_1080p60_hw = false;
            break;
        case PiBoardModel::Pi4:
        case PiBoardModel::Pi5:
            caps.has_5ghz_wifi = true;
            caps.supports_ht40 = true;
            caps.supports_1080p60_hw = true;
            break;
        default:
            break;
    }

    std::error_code ec;
    if (caps.board != PiBoardModel::Pi5) {
        std::string h264_dev =
            find_v4l2_node_by_name(v4l2_sysfs, dev_dir, "bcm2835-codec-decode");
        if (h264_dev.empty()) {
            const auto fallback_v10 = dev_dir / "video10";
            if (std::filesystem::exists(fallback_v10, ec)) {
                h264_dev = fallback_v10.string();
            }
        }
        if (!h264_dev.empty()) {
            caps.has_v4l2_h264_m2m = true;
            caps.v4l2_h264_device = std::move(h264_dev);
        }
    }

    if (caps.board == PiBoardModel::Pi4 || caps.board == PiBoardModel::Pi5) {
        std::string hevc_dev = find_v4l2_node_by_name(v4l2_sysfs, dev_dir, "rpi-hevc-dec");
        if (hevc_dev.empty()) {
            const auto fallback_v19 = dev_dir / "video19";
            if (std::filesystem::exists(fallback_v19, ec)) {
                hevc_dev = fallback_v19.string();
            }
        }
        if (!hevc_dev.empty()) {
            caps.has_v4l2_hevc_stateless = true;
            caps.v4l2_hevc_device = std::move(hevc_dev);
        }
    }

    caps.recommended_h264 =
        caps.has_v4l2_h264_m2m ? DecoderBackend::V4l2H264M2M : DecoderBackend::SoftwareNeonH264;

    if (std::filesystem::exists(firmware_edid, ec)) {
        auto override_bytes = read_binary_file(firmware_edid);
        if (override_bytes.size() >= 128U) {
            caps.edid = std::move(override_bytes);
        }
    }
    if (caps.edid.empty()) {
        if (auto edid_res = read_drm_edid(drm_sysfs); edid_res.has_value()) {
            caps.edid = std::move(*edid_res);
            caps.drm_connected = true;
        }
    }

    if (std::filesystem::exists(drm_sysfs, ec)) {
        std::vector<std::filesystem::path> connectors;
        for (const auto& entry : std::filesystem::directory_iterator(drm_sysfs, ec)) {
            if (ec) {
                break;
            }
            if (entry.path().filename().string().find("HDMI-A-") != std::string::npos) {
                connectors.push_back(entry.path());
            }
        }
        std::sort(connectors.begin(), connectors.end());
        std::filesystem::path chosen;
        for (const auto& conn : connectors) {
            if (read_text_file(conn / "status") == "connected") {
                chosen = conn;
                caps.drm_connected = true;
                break;
            }
        }
        if (chosen.empty() && !connectors.empty()) {
            chosen = connectors.front();
        }
        if (!chosen.empty()) {
            caps.drm_connector = chosen.filename().string();
            const std::string cid_str = read_text_file(chosen / "connector_id");
            if (!cid_str.empty()) {
                try {
                    const unsigned long parsed_id = std::stoul(cid_str);
                    if (parsed_id > 0UL) {
                        caps.drm_connector_id = static_cast<std::uint32_t>(parsed_id);
                    }
                } catch (...) {
                }
            }
        }
    }

    return caps;
}

}  // namespace picast
