/**
 * @file media_pipeline.hpp
 * @brief GStreamer 1.0 V4L2/NEON/KMS media pipeline builder and RAII lifecycle wrapper.
 */

#pragma once

#include "picast/platform_probe.hpp"

#include <cstdint>
#include <expected>
#include <functional>
#include <memory>
#include <span>
#include <string>
#include <string_view>

namespace picast {

/** @brief Negotiated Miracast video payload codec. */
enum class VideoCodec {
    H264,
    H265,
};

/** @brief Negotiated Miracast audio payload codec. */
enum class AudioCodec {
    Lpcm,
    Aac,
};

/** @brief Pipeline runtime tuning options derived from CLI flags and `picast.conf`. */
struct PipelineConfig {
    VideoCodec video_codec{VideoCodec::H264};
    AudioCodec audio_codec{AudioCodec::Lpcm};
    DecoderBackend decoder_preference{DecoderBackend::Auto};
    AudioSinkKind audio_sink{AudioSinkKind::Auto};
    bool low_latency_sync{true};
    bool enable_audio{true};
    std::uint16_t ts_demux_latency_ms{45U};
    std::string drm_connector{};
};

/**
 * @brief Convert video codec enum to lowercase identifier.
 * @param codec Video codec enum.
 * @return `"h264"` or `"h265"`.
 */
[[nodiscard]] std::string_view to_string(VideoCodec codec) noexcept;

/**
 * @brief Convert audio codec enum to lowercase identifier.
 * @param codec Audio codec enum.
 * @return `"lpcm"` or `"aac"`.
 */
[[nodiscard]] std::string_view to_string(AudioCodec codec) noexcept;

/**
 * @brief Resolve the concrete GStreamer video decoder element for the active board and codec.
 * @param cfg Requested pipeline parameters.
 * @param hw Probed hardware capabilities.
 * @return Resolved `DecoderBackend`.
 */
[[nodiscard]] DecoderBackend resolve_video_decoder(
    const PipelineConfig& cfg,
    const HardwareCapabilities& hw) noexcept;

/**
 * @brief Construct a GStreamer `gst_parse_launch` pipeline string tuned for Trixie DRM/KMS.
 * @param cfg Pipeline options (codec, latency, sync, audio sink).
 * @param hw Probed hardware capabilities.
 * @return Validated GStreamer pipeline description string.
 */
[[nodiscard]] std::string build_gstreamer_pipeline_description(
    const PipelineConfig& cfg,
    const HardwareCapabilities& hw);

/**
 * @brief RAII controller wrapping GStreamer MPEG-TS demuxing, hardware/NEON decoding, and kmssink.
 */
class MediaPipeline {
public:
    using ErrorCallback = std::function<void(std::string_view)>;

    MediaPipeline();
    ~MediaPipeline();

    MediaPipeline(const MediaPipeline&) = delete;
    MediaPipeline& operator=(const MediaPipeline&) = delete;

    /**
     * @brief Build and start the GStreamer playback pipeline.
     * @param cfg Runtime pipeline configuration.
     * @param hw Hardware probe snapshot.
     * @param on_error Callback fired if GStreamer bus reports an error or EOS.
     * @return Void on success, or error string if pipeline creation fails.
     */
    [[nodiscard]] std::expected<void, std::string> start(
        const PipelineConfig& cfg,
        const HardwareCapabilities& hw,
        ErrorCallback on_error = {});

    /**
     * @brief Feed an ordered MPEG-TS chunk into the live `appsrc` stage.
     * @param ts_bytes Byte span emitted by `RtpJitterBuffer`.
     * @return True if accepted by pipeline.
     */
    [[nodiscard]] bool push_mpegts(std::span<const std::uint8_t> ts_bytes);

    /** @brief Poll GStreamer message bus without blocking. */
    void poll_events();

    /** @brief Stop playback and release GStreamer elements deterministically. */
    void stop() noexcept;

    /** @brief Query whether pipeline is actively running. */
    [[nodiscard]] bool is_running() const noexcept;

    /** @brief Inspect the active GStreamer launch string constructed at `start()`. */
    [[nodiscard]] std::string_view active_pipeline_description() const noexcept;

private:
    struct Impl;
    std::unique_ptr<Impl> impl_;
};

}  // namespace picast
