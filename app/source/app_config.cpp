/**
 * @file app_config.cpp
 * @brief Command-line flag parser implementation for `picast-player`.
 */

#include "picast/app_config.hpp"

#include <string>
#include <string_view>

namespace picast {

namespace {

[[nodiscard]] std::expected<std::uint16_t, std::string> parse_u16(std::string_view text) {
    try {
        std::size_t idx = 0U;
        const unsigned long val = std::stoul(std::string{text}, &idx, 10);
        if (idx != text.size() || val > 65535UL) {
            return std::unexpected("Port/value out of u16 range: " + std::string{text});
        }
        return static_cast<std::uint16_t>(val);
    } catch (...) {
        return std::unexpected("Invalid numeric argument: " + std::string{text});
    }
}

}  // namespace

std::expected<AppConfig, std::string> parse_app_args(std::span<const char* const> args) {
    AppConfig cfg;
    // `RtpJitterBuffer` already reorders out-of-order UDP RTP before `appsrc`,
    // so `tsdemux` adds no demux-side buffering by default. Historically
    // `--latency-ms` double-buffered the value into BOTH the jitter buffer and
    // `tsdemux` (45 ms became 90 ms of glass-to-glass delay); it now tunes the
    // jitter buffer only, and `--ts-demux-latency-ms` provides an explicit
    // override (e.g. for the direct `udpsrc` pipeline path).
    cfg.pipeline.ts_demux_latency_ms = 0U;
    for (std::size_t i = 1U; i < args.size(); ++i) {
        const std::string_view arg = args[i];
        auto next_arg = [&](std::string_view flag) -> std::expected<std::string_view, std::string> {
            if (i + 1U >= args.size()) {
                return std::unexpected("Missing value for flag " + std::string{flag});
            }
            return std::string_view{args[++i]};
        };

        if (arg == "--probe") {
            cfg.probe_only = true;
        } else if (arg == "--dry-run") {
            cfg.pipeline.dry_run = true;
        } else if (arg == "--no-audio") {
            cfg.pipeline.enable_audio = false;
        } else if (arg == "--low-latency") {
            cfg.pipeline.low_latency_sync = true;
        } else if (arg == "--av-sync") {
            cfg.pipeline.low_latency_sync = false;
        } else if (arg == "--rtp-port") {
            const auto val = next_arg(arg);
            if (!val) return std::unexpected(val.error());
            const auto port = parse_u16(*val);
            if (!port) return std::unexpected(port.error());
            cfg.rtp_port = *port;
        } else if (arg == "--idr-port") {
            const auto val = next_arg(arg);
            if (!val) return std::unexpected(val.error());
            const auto port = parse_u16(*val);
            if (!port) return std::unexpected(port.error());
            cfg.idr_port = *port;
        } else if (arg == "--latency-ms") {
            const auto val = next_arg(arg);
            if (!val) return std::unexpected(val.error());
            const auto ms = parse_u16(*val);
            if (!ms) return std::unexpected(ms.error());
            cfg.jitter_ms = *ms;
        } else if (arg == "--ts-demux-latency-ms") {
            const auto val = next_arg(arg);
            if (!val) return std::unexpected(val.error());
            const auto ms = parse_u16(*val);
            if (!ms) return std::unexpected(ms.error());
            cfg.pipeline.ts_demux_latency_ms = *ms;
        } else if (arg == "--codec") {
            const auto val = next_arg(arg);
            if (!val) return std::unexpected(val.error());
            if (*val == "h264") {
                cfg.pipeline.video_codec = VideoCodec::H264;
            } else if (*val == "h265" || *val == "hevc") {
                cfg.pipeline.video_codec = VideoCodec::H265;
            } else {
                return std::unexpected("Unsupported --codec value: " + std::string{*val});
            }
        } else if (arg == "--decoder") {
            const auto val = next_arg(arg);
            if (!val) return std::unexpected(val.error());
            if (*val == "auto") {
                cfg.pipeline.decoder_preference = DecoderBackend::Auto;
            } else if (*val == "v4l2") {
                cfg.pipeline.decoder_preference = DecoderBackend::V4l2H264M2M;
            } else if (*val == "sw") {
                cfg.pipeline.decoder_preference = DecoderBackend::SoftwareNeonH264;
            } else {
                return std::unexpected("Unsupported --decoder value: " + std::string{*val});
            }
        } else if (arg == "--audio-sink") {
            const auto val = next_arg(arg);
            if (!val) return std::unexpected(val.error());
            if (*val == "auto") {
                cfg.pipeline.audio_sink = AudioSinkKind::Auto;
            } else if (*val == "hdmi") {
                cfg.pipeline.audio_sink = AudioSinkKind::Hdmi;
            } else if (*val == "jack") {
                cfg.pipeline.audio_sink = AudioSinkKind::HeadphoneJack;
            } else if (*val == "alsa") {
                cfg.pipeline.audio_sink = AudioSinkKind::Alsa;
            } else if (*val == "pipewire") {
                cfg.pipeline.audio_sink = AudioSinkKind::PipeWire;
            } else {
                return std::unexpected("Unsupported --audio-sink value: " + std::string{*val});
            }
        } else {
            return std::unexpected("Unknown argument: " + std::string{arg});
        }
    }
    return cfg;
}

}  // namespace picast
