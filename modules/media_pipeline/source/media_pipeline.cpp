/**
 * @file media_pipeline.cpp
 * @brief GStreamer 1.0 pipeline assembly, V4L2/NEON selection, and RAII playback control.
 */

#include "picast/media_pipeline.hpp"

#include <format>
#include <mutex>
#include <utility>

#if PICAST_HAS_GSTREAMER
#include <gst/app/gstappsrc.h>
#include <gst/gst.h>
#endif

namespace picast {

std::string_view to_string(VideoCodec codec) noexcept {
    switch (codec) {
        case VideoCodec::H265:
            return "h265";
        case VideoCodec::H264:
        default:
            return "h264";
    }
}

std::string_view to_string(AudioCodec codec) noexcept {
    switch (codec) {
        case AudioCodec::Aac:
            return "aac";
        case AudioCodec::Lpcm:
        default:
            return "lpcm";
    }
}

DecoderBackend resolve_video_decoder(
    const PipelineConfig& cfg,
    const HardwareCapabilities& hw) noexcept {
    if (cfg.decoder_preference != DecoderBackend::Auto) {
        return cfg.decoder_preference;
    }
    if (cfg.video_codec == VideoCodec::H265) {
        return hw.has_v4l2_hevc_stateless ? DecoderBackend::V4l2HevcStateless
                                          : DecoderBackend::SoftwareHevc;
    }
    return hw.has_v4l2_h264_m2m ? DecoderBackend::V4l2H264M2M : DecoderBackend::SoftwareNeonH264;
}

namespace {

/** @brief Historical `tsdemux`/`rtpjitterbuffer` latency used when no explicit override is set. */
constexpr std::uint16_t kDefaultTsdemuxLatencyMs = 45U;

[[nodiscard]] std::string build_video_decoder_chain(DecoderBackend decoder) {
    // Explicit video/* caps BEFORE `queue` are mandatory: `queue` has `ANY`
    // sink caps, so without a media-type filter `gst_parse_launch` links
    // `tsdemux`'s dynamic audio pad to the video queue whenever PMT/PES
    // declares audio first, causing `streaming stopped, reason not-linked (-1)`.
    // The pre-decode `queue` is explicitly bounded (12 buffers, no byte/time
    // caps) so compressed frames never accumulate to GStreamer's default
    // 200-buffer / 1 s limits during Wi-Fi bursts.
    switch (decoder) {
        case DecoderBackend::V4l2H264M2M:
            // Non-leaky bounded pre-decode queue + config-interval=-1 + caps
            // normalization + DMA-BUF capture-io-mode=4. On RPi Trixie
            // (`bcm2835-codec`), `v4l2h264dec` omits `constrained-high`/
            // `progressive-high` from its V4L2 profile menu and rejects
            // non-YUV VUI colorimetries (`sRGB`), even though WFD Constrained
            // High Profile (0x02) is a strict subset of High Profile decoded
            // natively by VC4.
            return "video/x-h264 ! queue max-size-buffers=12 max-size-bytes=0 max-size-time=0 ! "
                   "h264parse config-interval=-1 ! "
                   "video/x-h264,stream-format=byte-stream,alignment=au ! "
                   "capssetter join=true replace=false "
                   "caps=\"video/x-h264,profile=(string)high,colorimetry=(string)bt709\" ! "
                   "v4l2h264dec capture-io-mode=4";
        case DecoderBackend::V4l2HevcStateless:
            return "video/x-h265 ! queue max-size-buffers=12 max-size-bytes=0 max-size-time=0 ! "
                   "h265parse config-interval=-1 ! v4l2slh265dec";
        case DecoderBackend::SoftwareHevc:
            return "video/x-h265 ! queue max-size-buffers=12 max-size-bytes=0 max-size-time=0 ! "
                   "h265parse config-interval=-1 ! avdec_h265 max-threads=0";
        case DecoderBackend::SoftwareNeonH264:
        case DecoderBackend::Auto:
        default:
            return "video/x-h264 ! queue max-size-buffers=12 max-size-bytes=0 max-size-time=0 ! "
                   "h264parse config-interval=-1 ! avdec_h264 max-threads=0";
    }
}

[[nodiscard]] std::string build_audio_sink_element(AudioSinkKind sink) {
    // On Raspberry Pi OS Trixie, `pipewire-alsa` maps ALSA `default` to the
    // logged-in user's PipeWire daemon, which `picast.service` (root) cannot
    // open (`Host is down`). `alsasink device=sysdefault` opens the ALSA
    // hardware card directly while still letting explicit `pipewire` work.
    switch (sink) {
        case AudioSinkKind::PipeWire:
            return "pipewiresink";
        case AudioSinkKind::HeadphoneJack:
            return "alsasink device=plughw:Headphones,0";
        case AudioSinkKind::Hdmi:
        case AudioSinkKind::Alsa:
        case AudioSinkKind::Auto:
        default:
            return "alsasink device=sysdefault";
    }
}

}  // namespace

std::string build_gstreamer_pipeline_description(
    const PipelineConfig& cfg,
    const HardwareCapabilities& hw,
    bool use_appsrc,
    std::uint16_t udp_port) {
    const DecoderBackend decoder = resolve_video_decoder(cfg, hw);
    const std::string sync_flag = cfg.low_latency_sync ? "false" : "true";
    const std::string video_chain = build_video_decoder_chain(decoder);

    // `kmssink` defaults to `can-scale=true`, so no explicit property is needed.
    const std::string kms_props = std::format("driver-name=vc4 sync={}", sync_flag);

    std::string source_stage;
    if (use_appsrc) {
        // `RtpJitterBuffer` already reorders out-of-order UDP RTP upstream, so
        // `tsdemux` adds no demux-side latency unless explicitly overridden
        // (the app defaults `ts_demux_latency_ms` to 0 for this path).
        source_stage = std::format(
            "appsrc name=ts_src is-live=true do-timestamp=true format=time "
            "block=false max-bytes=524288 leaky-type=downstream "
            "caps=video/mpegts,systemstream=true,packetsize=188 ! "
            "tsdemux name=demux latency={}",
            cfg.ts_demux_latency_ms);
    } else {
        // Direct `udpsrc` path has no upstream jitter buffer of our own, so
        // fall back to the historical 45 ms `rtpjitterbuffer`/`tsdemux` latency.
        const std::uint16_t latency_ms =
            cfg.ts_demux_latency_ms > 0U ? cfg.ts_demux_latency_ms : kDefaultTsdemuxLatencyMs;
        source_stage = std::format(
            "udpsrc port={} buffer-size=4194304 ! "
            "rtpjitterbuffer latency={} drop-on-latency=false do-lost=true faststart-min-packets=2 ! "
            "rtpmp2tdepay ! tsdemux name=demux latency={}",
            udp_port,
            latency_ms,
            latency_ms);
    }

    std::string pipeline = std::format(
        "{} "
        "demux. ! {} ! queue leaky=downstream max-size-buffers=2 ! kmssink {}",
        source_stage,
        video_chain,
        kms_props);

    if (cfg.enable_audio) {
        const std::string audio_sink = build_audio_sink_element(cfg.audio_sink);
        pipeline += std::format(
            " demux. ! audio/mpeg ! queue leaky=downstream max-size-time=100000000 ! "
            "decodebin ! audioconvert ! audioresample ! {} sync={} async=false",
            audio_sink,
            sync_flag);
    } else {
        // Drain multiplexed WFD AAC audio into `fakesink` without opening ALSA
        // so `tsdemux` never sees an unlinked pad (`not-linked (-1)`) if audio
        // PES packets arrive ahead of the first H.264 IDR keyframe.
        pipeline +=
            " demux. ! audio/mpeg ! queue leaky=downstream max-size-buffers=4 ! "
            "fakesink sync=false async=false";
    }

    return pipeline;
}

struct MediaPipeline::Impl {
    bool running{false};
    bool dry_run{false};
    std::string pipeline_desc{};
    ErrorCallback on_error{};
#if PICAST_HAS_GSTREAMER
    GstElement* pipeline{nullptr};
    GstElement* appsrc{nullptr};
    GstBus* bus{nullptr};
#endif
};

MediaPipeline::MediaPipeline() : impl_(std::make_unique<Impl>()) {}

MediaPipeline::~MediaPipeline() {
    stop();
}

std::expected<void, std::string> MediaPipeline::start(
    const PipelineConfig& cfg,
    const HardwareCapabilities& hw,
    ErrorCallback on_error) {
    stop();
    impl_->dry_run = cfg.dry_run;
    impl_->on_error = std::move(on_error);
    impl_->pipeline_desc = build_gstreamer_pipeline_description(cfg, hw, true);

    if (cfg.dry_run) {
        impl_->running = true;
        return {};
    }

#if PICAST_HAS_GSTREAMER
    static std::once_flag gst_init_once;
    std::call_once(gst_init_once, []() { gst_init(nullptr, nullptr); });

    GError* err = nullptr;
    GstElement* elem = gst_parse_launch(impl_->pipeline_desc.c_str(), &err);
    if (err != nullptr || elem == nullptr) {
        std::string msg = err != nullptr ? err->message : "Failed to construct GStreamer pipeline";
        if (err != nullptr) {
            g_error_free(err);
        }
        if (elem != nullptr) {
            gst_object_unref(elem);
        }
        return std::unexpected(std::move(msg));
    }

    impl_->pipeline = elem;
    impl_->appsrc = gst_bin_get_by_name(GST_BIN(elem), "ts_src");
    impl_->bus = gst_element_get_bus(elem);

    const GstStateChangeReturn ret = gst_element_set_state(impl_->pipeline, GST_STATE_PLAYING);
    if (ret == GST_STATE_CHANGE_FAILURE) {
        stop();
        return std::unexpected("GStreamer pipeline failed to transition to PLAYING");
    }

    impl_->running = true;
    return {};
#else
    impl_->running = true;
    return {};
#endif
}

bool MediaPipeline::push_mpegts(std::span<const std::uint8_t> ts_bytes) {
    if (!impl_->running || ts_bytes.empty()) {
        return false;
    }
    if (impl_->dry_run) {
        return true;
    }

#if PICAST_HAS_GSTREAMER
    if (impl_->appsrc == nullptr) {
        return false;
    }
    GstBuffer* buffer = gst_buffer_new_allocate(nullptr, ts_bytes.size(), nullptr);
    if (buffer == nullptr) {
        return false;
    }
    gst_buffer_fill(buffer, 0, ts_bytes.data(), ts_bytes.size());
    const GstFlowReturn flow = gst_app_src_push_buffer(GST_APP_SRC(impl_->appsrc), buffer);
    return flow == GST_FLOW_OK;
#else
    return true;
#endif
}

void MediaPipeline::poll_events() {
#if PICAST_HAS_GSTREAMER
    if (!impl_->running || impl_->bus == nullptr) {
        return;
    }
    while (impl_->running && impl_->bus != nullptr) {
        GstMessage* msg = gst_bus_pop_filtered(
            impl_->bus,
            static_cast<GstMessageType>(GST_MESSAGE_ERROR | GST_MESSAGE_EOS));
        if (msg == nullptr) {
            break;
        }
        const GstMessageType mtype = GST_MESSAGE_TYPE(msg);
        std::string callback_msg;
        bool invoke_error = false;
        if (mtype == GST_MESSAGE_ERROR) {
            GError* err = nullptr;
            gchar* dbg = nullptr;
            gst_message_parse_error(msg, &err, &dbg);
            callback_msg = err != nullptr ? err->message : "Unknown GStreamer error";
            if (dbg != nullptr && dbg[0] != '\0') {
                callback_msg += std::format(" ({})", dbg);
            }
            if (err != nullptr) {
                g_error_free(err);
            }
            if (dbg != nullptr) {
                g_free(dbg);
            }
            invoke_error = true;
        } else if (mtype == GST_MESSAGE_EOS) {
            callback_msg = "GStreamer pipeline reached EOS";
            invoke_error = true;
        }
        gst_message_unref(msg);
        if (invoke_error && impl_->on_error) {
            impl_->on_error(callback_msg);
        }
    }
#endif
}

void MediaPipeline::stop() noexcept {
#if PICAST_HAS_GSTREAMER
    if (impl_->pipeline != nullptr) {
        gst_element_set_state(impl_->pipeline, GST_STATE_NULL);
    }
    if (impl_->appsrc != nullptr) {
        gst_object_unref(impl_->appsrc);
        impl_->appsrc = nullptr;
    }
    if (impl_->bus != nullptr) {
        gst_object_unref(impl_->bus);
        impl_->bus = nullptr;
    }
    if (impl_->pipeline != nullptr) {
        gst_object_unref(impl_->pipeline);
        impl_->pipeline = nullptr;
    }
#endif
    impl_->running = false;
}

bool MediaPipeline::is_running() const noexcept {
    return impl_->running;
}

std::string_view MediaPipeline::active_pipeline_description() const noexcept {
    return impl_->pipeline_desc;
}

}  // namespace picast
