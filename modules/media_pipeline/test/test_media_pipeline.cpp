/**
 * @file test_media_pipeline.cpp
 * @brief Offline unit tests for `picast::MediaPipeline` construction and decoder selection.
 */

#include "picast/media_pipeline.hpp"

#include <array>
#include <iostream>
#include <string>
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

void test_decoder_and_pipeline_selection() {
    picast::HardwareCapabilities zero2;
    zero2.board = picast::PiBoardModel::PiZero2W;
    zero2.has_v4l2_h264_m2m = true;
    zero2.has_v4l2_hevc_stateless = false;

    picast::PipelineConfig cfg;
    const auto zero_pipe = picast::build_gstreamer_pipeline_description(cfg, zero2);
    EXPECT_TRUE(zero_pipe.find("demux. ! video/x-h264 ! queue") != std::string::npos);
    EXPECT_TRUE(zero_pipe.find("h264parse config-interval=-1") != std::string::npos);
    EXPECT_TRUE(zero_pipe.find("capssetter join=true replace=false") != std::string::npos);
    EXPECT_TRUE(zero_pipe.find("profile=(string)high") != std::string::npos);
    EXPECT_TRUE(zero_pipe.find("v4l2h264dec capture-io-mode=4") != std::string::npos);
    EXPECT_TRUE(zero_pipe.find("kmssink driver-name=vc4 sync=false") != std::string::npos);

    cfg.enable_audio = false;
    const auto muted_pipe = picast::build_gstreamer_pipeline_description(cfg, zero2);
    EXPECT_TRUE(muted_pipe.find("demux. ! audio/mpeg ! queue") != std::string::npos);
    EXPECT_TRUE(muted_pipe.find("fakesink sync=false async=false") != std::string::npos);
    cfg.enable_audio = true;

    picast::HardwareCapabilities pi5;
    pi5.board = picast::PiBoardModel::Pi5;
    pi5.has_v4l2_h264_m2m = false;
    pi5.has_v4l2_hevc_stateless = true;

    const auto pi5_h264 = picast::build_gstreamer_pipeline_description(cfg, pi5);
    EXPECT_TRUE(pi5_h264.find("avdec_h264") != std::string::npos);

    cfg.video_codec = picast::VideoCodec::H265;
    const auto pi5_hevc = picast::build_gstreamer_pipeline_description(cfg, pi5);
    EXPECT_TRUE(pi5_hevc.find("v4l2slh265dec") != std::string::npos);
}

void test_dry_run_pipeline_lifecycle() {
    picast::HardwareCapabilities caps;
    caps.board = picast::PiBoardModel::PiZero2W;
    caps.has_v4l2_h264_m2m = true;

    picast::PipelineConfig cfg;
    cfg.dry_run = true;

    picast::MediaPipeline pipe;
    const auto res = pipe.start(cfg, caps);
    EXPECT_TRUE(res.has_value());
    EXPECT_TRUE(pipe.is_running());

    const std::array<std::uint8_t, 4> dummy_ts{0x47U, 0x00U, 0x00U, 0x00U};
    EXPECT_TRUE(pipe.push_mpegts(dummy_ts));

    pipe.stop();
    EXPECT_TRUE(!pipe.is_running());
}

}  // namespace

int main() {
    test_decoder_and_pipeline_selection();
    test_dry_run_pipeline_lifecycle();

    if (g_failures > 0) {
        std::cerr << g_failures << " assertion(s) failed.\n";
        return 1;
    }
    std::cout << "All picast_media_pipeline unit tests passed.\n";
    return 0;
}
