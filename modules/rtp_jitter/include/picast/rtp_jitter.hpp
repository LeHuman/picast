/**
 * @file rtp_jitter.hpp
 * @brief Bounded, zero-hot-path-allocation circular RTP jitter buffer and RFC 3550 parser.
 */

#pragma once

#include <array>
#include <chrono>
#include <cstddef>
#include <cstdint>
#include <expected>
#include <functional>
#include <optional>
#include <span>
#include <string>
#include <vector>

namespace picast {

/** @brief Maximum single UDP RTP payload size held per fixed ring slot (MTU-safe). */
inline constexpr std::size_t kMaxRtpPayloadBytes = 1500U;

/** @brief Default circular jitter ring capacity in packets. */
inline constexpr std::size_t kDefaultJitterCapacity = 256U;

/** @brief Maximum accumulated MPEG-TS bytes emitted per `drain_coalesced()` burst chunk. */
inline constexpr std::size_t kCoalesceChunkBytes = 32768U;

/** @brief Zero-copy view of a parsed RFC 3550 RTP datagram. */
struct RtpPacketView {
    bool marker{false};
    std::uint8_t payload_type{0U};
    std::uint16_t sequence_number{0U};
    std::uint32_t timestamp{0U};
    std::span<const std::uint8_t> payload{};
};

/** @brief Configuration controlling circular jitter buffer capacity and IDR recovery timing. */
struct JitterConfig {
    std::size_t capacity_packets{kDefaultJitterCapacity};
    std::chrono::milliseconds max_hold_time{std::chrono::milliseconds{45}};
    std::chrono::milliseconds idr_cooldown{std::chrono::milliseconds{4000}};
    std::uint8_t expected_payload_type{33U};  // 33 = MPEG-2 TS (RFC 2250), 0 = accept any.
};

/** @brief Telemetry counters exposed by `RtpJitterBuffer` for diagnostics and unit tests. */
struct JitterStats {
    std::uint64_t packets_received{0U};
    std::uint64_t packets_emitted{0U};
    std::uint64_t packets_dropped_late{0U};
    std::uint64_t packets_dropped_duplicate{0U};
    std::uint64_t packets_reordered{0U};
    std::uint64_t sequence_gaps{0U};
    std::uint64_t parse_errors{0U};
    std::uint64_t idr_requests_triggered{0U};
};

/**
 * @brief Wraparound-safe 16-bit unsigned RTP sequence comparison (`lhs < rhs` mod 65536).
 * @param lhs First sequence number.
 * @param rhs Second sequence number.
 * @return True if `lhs` strictly precedes `rhs` within half the 16-bit space.
 */
[[nodiscard]] bool seq_before(std::uint16_t lhs, std::uint16_t rhs) noexcept;

/**
 * @brief Signed modular forward distance `(to - from) mod 65536`.
 * @param from Starting sequence number.
 * @param to Target sequence number.
 * @return Unsigned wraparound distance.
 */
[[nodiscard]] std::uint16_t seq_distance(std::uint16_t from, std::uint16_t to) noexcept;

/**
 * @brief Parse an RFC 3550 RTP datagram without heap allocation.
 * @param raw_datagram Raw UDP packet span.
 * @return `RtpPacketView` referencing the payload inside `raw_datagram`, or error description.
 */
[[nodiscard]] std::expected<RtpPacketView, std::string> parse_rtp_packet(
    std::span<const std::uint8_t> raw_datagram) noexcept;

/**
 * @brief Deterministic pre-allocated circular RTP jitter reorder buffer with IDR loss signaling.
 */
class RtpJitterBuffer {
public:
    using IdrCallback = std::function<void()>;

    /**
     * @brief Construct a fixed-capacity ring buffer; allocates all slots upfront.
     * @param config Capacity, hold deadline, cooldown, and payload-type filter.
     * @param on_idr Optional callback invoked (rate-limited) when a sequence gap is skipped.
     */
    explicit RtpJitterBuffer(JitterConfig config = {}, IdrCallback on_idr = {});

    /** @brief Reset all ring slots, sequence tracking, and statistics without reallocating. */
    void reset() noexcept;

    /**
     * @brief Validate, parse, and insert an incoming UDP RTP datagram into the ring.
     * @param raw_datagram Raw UDP bytes.
     * @param now Monotonic arrival timestamp.
     * @return True if stored, false if malformed, wrong PT, late, or duplicate.
     */
    [[nodiscard]] bool push_datagram(
        std::span<const std::uint8_t> raw_datagram,
        std::chrono::steady_clock::time_point now = std::chrono::steady_clock::now()) noexcept;

    /**
     * @brief Copy the next in-order (or hold-expired) payload into a caller-provided scratch buffer.
     * @param out_buf Destination buffer (must hold at least `kMaxRtpPayloadBytes`).
     * @param now Current monotonic clock time.
     * @return Number of bytes copied if a packet was ready, or `std::nullopt`.
     */
    [[nodiscard]] std::optional<std::size_t> pop_ready_into(
        std::span<std::uint8_t> out_buf,
        std::chrono::steady_clock::time_point now = std::chrono::steady_clock::now()) noexcept;

    /**
     * @brief Drain all currently ready payloads sequentially into a zero-allocation callback.
     * @tparam Callback Callable accepting `std::span<const std::uint8_t>`.
     * @param on_packet Invoked for each emitted MPEG-TS payload slice.
     * @param now Current monotonic clock time.
     * @return Count of packets drained during this call.
     */
    template <typename Callback>
    std::size_t drain(
        Callback&& on_packet,
        std::chrono::steady_clock::time_point now = std::chrono::steady_clock::now()) noexcept {
        std::array<std::uint8_t, kMaxRtpPayloadBytes> scratch{};
        std::size_t count = 0U;
        while (const auto len = pop_ready_into(scratch, now)) {
            on_packet(std::span<const std::uint8_t>{scratch.data(), *len});
            ++count;
        }
        return count;
    }

    /** @brief Return accumulated jitter buffer telemetry counters. */
    [[nodiscard]] const JitterStats& stats() const noexcept { return stats_; }

    /** @brief Return current number of buffered packets awaiting emission. */
    [[nodiscard]] std::size_t occupied() const noexcept { return occupied_; }

    /**
     * @brief Drain all currently ready payloads coalesced into ≤32 KiB burst chunks.
     *
     * Consecutive ready MPEG-TS payloads are concatenated into a single stack
     * scratch buffer and flushed through `on_chunk` once per burst, cutting the
     * per-packet `gst_buffer_new_allocate` + `gst_app_src_push_buffer` overhead
     * by 5-10x on the `appsrc` hot path. A flush happens eagerly when appending
     * the next packet would exceed `kCoalesceChunkBytes`.
     * @tparam Callback Callable accepting `std::span<const std::uint8_t>`.
     * @param on_chunk Invoked once per accumulated burst (never with an empty span).
     * @param now Current monotonic clock time.
     * @return Count of packets drained (and coalesced) during this call.
     */
    template <typename Callback>
    std::size_t drain_coalesced(
        Callback&& on_chunk,
        std::chrono::steady_clock::time_point now = std::chrono::steady_clock::now()) noexcept {
        std::array<std::uint8_t, kCoalesceChunkBytes> burst{};
        std::array<std::uint8_t, kMaxRtpPayloadBytes> staging{};
        std::size_t packets = 0U;
        std::size_t filled = 0U;
        while (const auto len = pop_ready_into(staging, now)) {
            if (filled > 0U && (filled + *len) > burst.size()) {
                on_chunk(std::span<const std::uint8_t>{burst.data(), filled});
                filled = 0U;
            }
            std::copy_n(staging.begin(), *len, burst.begin() + filled);
            filled += *len;
            ++packets;
        }
        if (filled > 0U) {
            on_chunk(std::span<const std::uint8_t>{burst.data(), filled});
        }
        return packets;
    }

    /**
     * @brief Report the monotonic deadline when the earliest held packet is force-emitted.
     *
     * Returns `ring_[earliest].arrived_at + max_hold_time` while a sequence gap
     * blocks in-order emission (`occupied_ > 0` and the packet at
     * `next_expected_seq_` is still missing), so the UDP event loop can sleep
     * exactly until the hold deadline. Returns `std::nullopt` when the buffer is
     * empty or the head packet is already ready for immediate emission.
     * @return Hold-expiry deadline, or `std::nullopt` when empty/ready.
     */
    [[nodiscard]] std::optional<std::chrono::steady_clock::time_point> next_deadline() const noexcept;

private:
    struct Slot {
        bool occupied{false};
        std::uint16_t seq{0U};
        std::uint16_t length{0U};
        std::chrono::steady_clock::time_point arrived_at{};
        std::array<std::uint8_t, kMaxRtpPayloadBytes> payload{};
    };

    /** @brief Advance `earliest_seq_` to the next occupied slot in ring order after a pop. */
    void advance_earliest_from(std::size_t vacated_idx) noexcept;

    /** @brief Full-ring recompute of `earliest_seq_` (rare paths: eviction, slot overwrite). */
    void recompute_earliest() noexcept;
    void maybe_trigger_idr(std::chrono::steady_clock::time_point now) noexcept;

    JitterConfig config_{};
    IdrCallback on_idr_{};
    std::vector<Slot> ring_{};
    std::size_t occupied_{0U};
    bool initialized_{false};
    std::uint16_t next_expected_seq_{0U};
    /**
     * @brief Lowest occupied sequence number; valid only while `occupied_ > 0`.
     *
     * Kept in O(1) on the hot paths: advanced incrementally past vacated slots
     * on pop, updated on out-of-order push, and recomputed via a full-ring scan
     * only on rare paths (capacity eviction, same-slot overwrite).
     */
    std::uint16_t earliest_seq_{0U};
    std::chrono::steady_clock::time_point last_idr_request_{};
    JitterStats stats_{};
};

}  // namespace picast
