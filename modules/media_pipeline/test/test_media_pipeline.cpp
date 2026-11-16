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

void test_inactive_pipeline_lifecycle() {
    picast::MediaPipeline pipe;
    EXPECT_TRUE(!pipe.is_running());

    const std::array<std::uint8_t, 4> dummy_ts{0x47U, 0x00U, 0x00U, 0x00U};
    EXPECT_TRUE(!pipe.push_mpegts(dummy_ts));

    pipe.poll_events();
    pipe.stop();
    EXPECT_TRUE(!pipe.is_running());
}

void test_latency_defaults_and_queue_bounds() {
    picast::HardwareCapabilities caps;
    caps.board = picast::PiBoardModel::PiZero2W;
    caps.has_v4l2_h264_m2m = true;
    caps.drm_connector = "card0-HDMI-A-1";

    picast::PipelineConfig cfg;
    // `RtpJitterBuffer` already reorders RTP upstream, so `tsdemux` defaults to 0 ms.
    const auto appsrc_pipe = picast::build_gstreamer_pipeline_description(cfg, caps);
    EXPECT_TRUE(appsrc_pipe.find("appsrc name=ts_src") != std::string::npos);
    EXPECT_TRUE(appsrc_pipe.find("leaky-type=downstream") != std::string::npos);
    EXPECT_TRUE(appsrc_pipe.find("tsdemux name=demux latency=0") != std::string::npos);
    // Pre-decode compressed-frame queue must be bounded (no 200-buffer / 1 s defaults).
    EXPECT_TRUE(appsrc_pipe.find("queue max-size-buffers=12 max-size-bytes=0 max-size-time=0")
                != std::string::npos);
    // Post-decode queue stays leaky with the 2-buffer bound before kmssink.
    EXPECT_TRUE(appsrc_pipe.find("queue leaky=downstream max-size-buffers=2 ! kmssink")
                != std::string::npos);
    EXPECT_TRUE(appsrc_pipe.find("can-scale") == std::string::npos);

    // Explicit ts_demux_latency_ms override propagates to tsdemux.
    cfg.ts_demux_latency_ms = 45U;
    const auto custom_latency = picast::build_gstreamer_pipeline_description(cfg, caps);
    EXPECT_TRUE(custom_latency.find("tsdemux name=demux latency=45") != std::string::npos);

    // Default / HDMI ALSA sinks must bypass PipeWire's user-scoped `default` PCM.
    EXPECT_TRUE(custom_latency.find("alsasink device=sysdefault") != std::string::npos);
    cfg.audio_sink = picast::AudioSinkKind::Hdmi;
    const auto hdmi_audio = picast::build_gstreamer_pipeline_description(cfg, caps);
    EXPECT_TRUE(hdmi_audio.find("alsasink device=sysdefault") != std::string::npos);
}

}  // namespace

int main() {
    test_decoder_and_pipeline_selection();
    test_inactive_pipeline_lifecycle();
    test_latency_defaults_and_queue_bounds();

    if (g_failures > 0) {
        std::cerr << g_failures << " assertion(s) failed.\n";
        return 1;
    }
    std::cout << "All picast_media_pipeline unit tests passed.\n";
    return 0;
}
