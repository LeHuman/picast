/**
 * @file test_platform_probe.cpp
 * @brief Offline unit tests for the `picast::platform_probe` module.
 */

#include "picast/platform_probe.hpp"

#include <iostream>
#include <string_view>

namespace {

int g_failures = 0;

void expect_true(bool cond, std::string_view expr, int line) {
    if (!cond) {
        std::cerr << "[FAIL] line " << line << ": " << expr << '\n';
        ++g_failures;
    }
}

#define EXPECT_TRUE(cond) expect_true(static_cast<bool>(cond), #cond, __LINE__)

void test_board_classification_and_strings() {
    EXPECT_TRUE(picast::classify_board_model("Raspberry Pi Zero 2 W Rev 1.0", "") ==
                picast::PiBoardModel::PiZero2W);
    EXPECT_TRUE(picast::classify_board_model("Raspberry Pi 5 Model B Rev 1.0", "BCM2712") ==
                picast::PiBoardModel::Pi5);
    EXPECT_TRUE(picast::classify_board_model("Raspberry Pi 500 Rev 1.0", "BCM2712") ==
                picast::PiBoardModel::Pi5);
    EXPECT_TRUE(picast::classify_board_model("Raspberry Pi 4 Model B", "BCM2711") ==
                picast::PiBoardModel::Pi4);
    EXPECT_TRUE(picast::classify_board_model("Raspberry Pi 3 Model B Plus", "") ==
                picast::PiBoardModel::Pi3);

    EXPECT_TRUE(picast::to_string(picast::DecoderBackend::V4l2H264M2M) == "v4l2h264dec");
    EXPECT_TRUE(picast::to_string(picast::DecoderBackend::V4l2HevcStateless) == "v4l2slh265dec");
    EXPECT_TRUE(picast::to_string(picast::AudioSinkKind::HeadphoneJack) == "jack");
}

void test_missing_drm_sysfs_returns_error() {
    const auto edid = picast::read_drm_edid("/nonexistent_picast_drm_path");
    EXPECT_TRUE(!edid.has_value());
}

}  // namespace

int main() {
    test_board_classification_and_strings();
    test_missing_drm_sysfs_returns_error();

    if (g_failures > 0) {
        std::cerr << g_failures << " assertion(s) failed.\n";
        return 1;
    }
    std::cout << "All picast_platform_probe unit tests passed.\n";
    return 0;
}
