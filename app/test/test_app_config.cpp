/**
 * @file test_app_config.cpp
 * @brief Offline unit tests for `picast-player` CLI argument parsing.
 */

#include "picast/app_config.hpp"

#include <array>
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

void test_valid_cli_arguments() {
    const std::array argv{
        "picast-player",
        "--rtp-port",
        "1030",
        "--idr-port",
        "1031",
        "--latency-ms",
        "60",
        "--codec",
        "hevc",
        "--av-sync",
        "--audio-sink",
        "pipewire",
        "--dry-run",
    };

    const auto parsed = picast::parse_app_args(argv);
    EXPECT_TRUE(parsed.has_value());
    if (parsed.has_value()) {
        EXPECT_TRUE(parsed->rtp_port == 1030U);
        EXPECT_TRUE(parsed->idr_port == 1031U);
        EXPECT_TRUE(parsed->jitter_ms == 60U);
        // `--latency-ms` tunes the jitter buffer only: tsdemux latency stays 0
        // so the 45 ms hold is not double-buffered into the pipeline as well.
        EXPECT_TRUE(parsed->pipeline.ts_demux_latency_ms == 0U);
        EXPECT_TRUE(parsed->pipeline.video_codec == picast::VideoCodec::H265);
        EXPECT_TRUE(!parsed->pipeline.low_latency_sync);
        EXPECT_TRUE(parsed->pipeline.audio_sink == picast::AudioSinkKind::PipeWire);
        EXPECT_TRUE(parsed->pipeline.dry_run);
    }
}

void test_ts_demux_latency_defaults_to_zero_and_override_flag() {
    const std::array defaults{"picast-player"};
    const auto parsed = picast::parse_app_args(defaults);
    EXPECT_TRUE(parsed.has_value());
    if (parsed.has_value()) {
        EXPECT_TRUE(parsed->jitter_ms == 45U);
        EXPECT_TRUE(parsed->pipeline.ts_demux_latency_ms == 0U);
    }

    const std::array override_argv{"picast-player", "--ts-demux-latency-ms", "120"};
    const auto overridden = picast::parse_app_args(override_argv);
    EXPECT_TRUE(overridden.has_value());
    if (overridden.has_value()) {
        EXPECT_TRUE(overridden->pipeline.ts_demux_latency_ms == 120U);
        EXPECT_TRUE(overridden->jitter_ms == 45U);  // Independent of --ts-demux-latency-ms.
    }

    const std::array both_argv{"picast-player", "--latency-ms", "30", "--ts-demux-latency-ms", "15"};
    const auto both = picast::parse_app_args(both_argv);
    EXPECT_TRUE(both.has_value());
    if (both.has_value()) {
        EXPECT_TRUE(both->jitter_ms == 30U);
        EXPECT_TRUE(both->pipeline.ts_demux_latency_ms == 15U);
    }
}

void test_invalid_cli_arguments_return_error() {
    const std::array bad_port{"picast-player", "--rtp-port", "99999"};
    EXPECT_TRUE(!picast::parse_app_args(bad_port).has_value());

    const std::array missing_val{"picast-player", "--codec"};
    EXPECT_TRUE(!picast::parse_app_args(missing_val).has_value());

    const std::array unknown_flag{"picast-player", "--bogus"};
    EXPECT_TRUE(!picast::parse_app_args(unknown_flag).has_value());
}

}  // namespace

int main() {
    test_valid_cli_arguments();
    test_ts_demux_latency_defaults_to_zero_and_override_flag();
    test_invalid_cli_arguments_return_error();

    if (g_failures > 0) {
        std::cerr << g_failures << " assertion(s) failed.\n";
        return 1;
    }
    std::cout << "All picast_app unit tests passed.\n";
    return 0;
}
