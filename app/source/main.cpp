/**
 * @file main.cpp
 * @brief Entrypoint for `picast-player` POSIX UDP RTP receiver, jitter reorderer, and GStreamer playback.
 */

#include "picast/app_config.hpp"
#include "picast/media_pipeline.hpp"
#include "picast/platform_probe.hpp"
#include "picast/rtp_jitter.hpp"

#include <arpa/inet.h>
#include <netinet/in.h>
#include <sys/socket.h>
#include <unistd.h>

#include <array>
#include <atomic>
#include <cerrno>
#include <chrono>
#include <csignal>
#include <cstring>
#include <exception>
#include <format>
#include <iostream>
#include <string_view>

namespace picast {

namespace {

constexpr int kInvalidSocket = -1;
std::atomic<bool> g_stop_requested{false};

void handle_signal(int /*sig*/) noexcept {
    g_stop_requested.store(true, std::memory_order_relaxed);
}

void install_signal_handlers() noexcept {
    struct sigaction sa {};
    sa.sa_handler = handle_signal;
    sigemptyset(&sa.sa_mask);
    sa.sa_flags = 0;  // Clear SA_RESTART so blocking recv() wakes immediately on signal.
    (void)sigaction(SIGINT, &sa, nullptr);
    (void)sigaction(SIGTERM, &sa, nullptr);
}

/** @brief RAII guard ensuring POSIX socket descriptors are closed deterministically. */
class SocketGuard {
public:
    explicit SocketGuard(int fd = kInvalidSocket) noexcept : fd_(fd) {}
    ~SocketGuard() noexcept { reset(); }

    SocketGuard(const SocketGuard&) = delete;
    SocketGuard& operator=(const SocketGuard&) = delete;

    [[nodiscard]] int get() const noexcept { return fd_; }
    [[nodiscard]] bool valid() const noexcept { return fd_ != kInvalidSocket; }

    void reset() noexcept {
        if (fd_ != kInvalidSocket) {
            close(fd_);
            fd_ = kInvalidSocket;
        }
    }

private:
    int fd_;
};

void send_udp_signal(
    int idr_fd,
    std::uint16_t idr_port,
    std::string_view msg) noexcept {
    if (idr_fd == kInvalidSocket || idr_port == 0U) {
        return;
    }
    sockaddr_in addr{};
    addr.sin_family = AF_INET;
    addr.sin_port = htons(idr_port);
    addr.sin_addr.s_addr = htonl(INADDR_LOOPBACK);
    (void)sendto(idr_fd, msg.data(), msg.size(), MSG_DONTWAIT,
                 reinterpret_cast<const sockaddr*>(&addr), sizeof(addr));
}

}  // namespace

}  // namespace picast

int main(int argc, char* argv[]) {
    try {
        picast::install_signal_handlers();

        const auto cfg_res = picast::parse_app_args(
            std::span<const char* const>{argv, static_cast<std::size_t>(argc)});
        if (!cfg_res.has_value()) {
            std::cerr << "[picast-player] ERROR Argument error: " << cfg_res.error() << '\n';
            return 2;
        }
        const auto cfg = *cfg_res;
        const auto hw = picast::probe_hardware();

        if (cfg.probe_only) {
            std::cout << std::format(
                "board={}\nmodel={}\nv4l2_h264={}\nv4l2_hevc={}\n5ghz_wifi={}\nht40={}\ndecoder={}\ndrm={}\n",
                picast::to_string(hw.board),
                hw.model_name,
                hw.has_v4l2_h264_m2m,
                hw.has_v4l2_hevc_stateless,
                hw.has_5ghz_wifi,
                hw.supports_ht40,
                picast::to_string(picast::resolve_video_decoder(cfg.pipeline, hw)),
                hw.drm_connector);
            return 0;
        }

        picast::SocketGuard rtp_sock{socket(AF_INET, SOCK_DGRAM, 0)};
        picast::SocketGuard idr_sock{socket(AF_INET, SOCK_DGRAM, 0)};
        if (!rtp_sock.valid()) {
            std::cerr << std::format(
                "[picast-player] ERROR Failed to open RTP UDP socket: {}\n",
                std::strerror(errno));
            return 1;
        }

        int reuse = 1;
        (void)setsockopt(rtp_sock.get(), SOL_SOCKET, SO_REUSEADDR, &reuse, sizeof(reuse));
        int rcvbuf = 4 * 1024 * 1024;
        (void)setsockopt(rtp_sock.get(), SOL_SOCKET, SO_RCVBUF, &rcvbuf, sizeof(rcvbuf));

        // 20 ms recv timeout keeps jitter hold expiry prompt without busy-waiting.
        timeval tv{};
        tv.tv_sec = 0;
        tv.tv_usec = 20000;
        (void)setsockopt(rtp_sock.get(), SOL_SOCKET, SO_RCVTIMEO, &tv, sizeof(tv));

        sockaddr_in bind_addr{};
        bind_addr.sin_family = AF_INET;
        bind_addr.sin_port = htons(cfg.rtp_port);
        bind_addr.sin_addr.s_addr = htonl(INADDR_ANY);
        if (bind(rtp_sock.get(), reinterpret_cast<const sockaddr*>(&bind_addr),
                 sizeof(bind_addr)) != 0) {
            std::cerr << std::format(
                "[picast-player] ERROR Failed to bind UDP port {}: {}\n",
                cfg.rtp_port,
                std::strerror(errno));
            return 1;
        }

        picast::MediaPipeline pipeline;
        const auto start_res = pipeline.start(cfg.pipeline, hw, [&](std::string_view err_msg) {
            std::cerr << "[picast-player] ERROR Pipeline error: " << err_msg << '\n';
            picast::send_udp_signal(idr_sock.get(), cfg.idr_port, "recv error");
            picast::g_stop_requested.store(true, std::memory_order_relaxed);
        });
        if (!start_res.has_value()) {
            std::cerr << "[picast-player] ERROR Failed to start pipeline: " << start_res.error()
                      << '\n';
            return 1;
        }

        std::cout << std::format("[picast-player] INFO Listening on UDP :{}\n", cfg.rtp_port);

        picast::RtpJitterBuffer jitter(
            picast::JitterConfig{
                .capacity_packets = picast::kDefaultJitterCapacity,
                .max_hold_time = std::chrono::milliseconds{cfg.jitter_ms},
            },
            [&]() { picast::send_udp_signal(idr_sock.get(), cfg.idr_port, "idr_request"); });

        std::array<std::uint8_t, 2048> rx_buf{};
        while (!picast::g_stop_requested.load(std::memory_order_relaxed)) {
            const ssize_t n = recv(rtp_sock.get(), rx_buf.data(), rx_buf.size(), 0);
            const auto now = std::chrono::steady_clock::now();
            if (n > 0) {
                (void)jitter.push_datagram(
                    std::span<const std::uint8_t>{rx_buf.data(), static_cast<std::size_t>(n)},
                    now);
            } else if (n < 0) {
                const int err = errno;
                if (err != EAGAIN && err != EWOULDBLOCK && err != EINTR) {
                    std::cerr << std::format(
                        "[picast-player] ERROR UDP recv failed: {}\n", std::strerror(err));
                    picast::send_udp_signal(idr_sock.get(), cfg.idr_port, "recv error");
                    break;
                }
            }
            (void)jitter.drain([&](std::span<const std::uint8_t> ts_chunk) {
                (void)pipeline.push_mpegts(ts_chunk);
            }, now);
            pipeline.poll_events();
        }

        pipeline.stop();
        rtp_sock.reset();
        idr_sock.reset();
        return 0;
    } catch (const std::exception& ex) {
        std::cerr << "[picast-player] FATAL Unhandled exception: " << ex.what() << '\n';
        return 1;
    } catch (...) {
        std::cerr << "[picast-player] FATAL Unknown unhandled exception\n";
        return 1;
    }
}
