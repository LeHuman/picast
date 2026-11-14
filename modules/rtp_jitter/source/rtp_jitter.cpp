/**
 * @file rtp_jitter.cpp
 * @brief Deterministic bounded RTP jitter buffer and RFC 3550 parser implementation.
 */

#include "picast/rtp_jitter.hpp"

#include <algorithm>
#include <bit>

namespace picast {

namespace {

[[nodiscard]] std::uint16_t read_u16_be(
    std::span<const std::uint8_t> buf,
    std::size_t offset) noexcept {
    return static_cast<std::uint16_t>((static_cast<std::uint16_t>(buf[offset]) << 8U) |
                                      static_cast<std::uint16_t>(buf[offset + 1U]));
}

[[nodiscard]] std::uint32_t read_u32_be(
    std::span<const std::uint8_t> buf,
    std::size_t offset) noexcept {
    return (static_cast<std::uint32_t>(buf[offset]) << 24U) |
           (static_cast<std::uint32_t>(buf[offset + 1U]) << 16U) |
           (static_cast<std::uint32_t>(buf[offset + 2U]) << 8U) |
           static_cast<std::uint32_t>(buf[offset + 3U]);
}

}  // namespace

bool seq_before(std::uint16_t lhs, std::uint16_t rhs) noexcept {
    const auto diff = static_cast<std::uint16_t>(rhs - lhs);
    return diff != 0U && diff < 0x8000U;
}

std::uint16_t seq_distance(std::uint16_t from, std::uint16_t to) noexcept {
    return static_cast<std::uint16_t>(to - from);
}

std::expected<RtpPacketView, std::string> parse_rtp_packet(
    std::span<const std::uint8_t> raw_datagram) noexcept {
    if (raw_datagram.size() < 12U) {
        return std::unexpected("RTP datagram shorter than 12-byte fixed header");
    }

    const std::uint8_t v_p_x_cc = raw_datagram[0];
    const std::uint8_t version = static_cast<std::uint8_t>(v_p_x_cc >> 6U);
    if (version != 2U) {
        return std::unexpected("Unsupported RTP version");
    }

    const bool has_padding = (v_p_x_cc & 0x20U) != 0U;
    const bool has_extension = (v_p_x_cc & 0x10U) != 0U;
    const std::size_t csrc_count = static_cast<std::size_t>(v_p_x_cc & 0x0FU);

    std::size_t header_len = 12U + (csrc_count * 4U);
    if (raw_datagram.size() < header_len) {
        return std::unexpected("Truncated RTP CSRC header");
    }

    if (has_extension) {
        if (raw_datagram.size() < header_len + 4U) {
            return std::unexpected("Truncated RTP extension header");
        }
        const std::size_t ext_words = read_u16_be(raw_datagram, header_len + 2U);
        header_len += 4U + (ext_words * 4U);
        if (raw_datagram.size() < header_len) {
            return std::unexpected("Truncated RTP extension body");
        }
    }

    std::size_t payload_end = raw_datagram.size();
    if (has_padding) {
        const std::size_t pad_len = raw_datagram.back();
        if (pad_len == 0U || pad_len > (payload_end - header_len)) {
            return std::unexpected("Invalid RTP padding length");
        }
        payload_end -= pad_len;
    }

    if (payload_end <= header_len) {
        return std::unexpected("Empty RTP payload");
    }

    RtpPacketView view;
    view.marker = (raw_datagram[1] & 0x80U) != 0U;
    view.payload_type = static_cast<std::uint8_t>(raw_datagram[1] & 0x7FU);
    view.sequence_number = read_u16_be(raw_datagram, 2U);
    view.timestamp = read_u32_be(raw_datagram, 4U);
    view.payload = raw_datagram.subspan(header_len, payload_end - header_len);
    return view;
}

RtpJitterBuffer::RtpJitterBuffer(JitterConfig config, IdrCallback on_idr)
    : config_(config), on_idr_(std::move(on_idr)) {
    const std::size_t clamped = std::clamp<std::size_t>(config_.capacity_packets, 16U, 32768U);
    config_.capacity_packets = std::bit_ceil(clamped);
    ring_.resize(config_.capacity_packets);
}

void RtpJitterBuffer::reset() noexcept {
    for (auto& slot : ring_) {
        slot.occupied = false;
        slot.seq = 0U;
        slot.length = 0U;
    }
    occupied_ = 0U;
    initialized_ = false;
    next_expected_seq_ = 0U;
    last_idr_request_ = {};
    stats_ = {};
}

bool RtpJitterBuffer::push_datagram(
    std::span<const std::uint8_t> raw_datagram,
    std::chrono::steady_clock::time_point now) noexcept {
    const auto parsed = parse_rtp_packet(raw_datagram);
    if (!parsed.has_value()) {
        ++stats_.parse_errors;
        return false;
    }

    const auto& pkt = *parsed;
    if (config_.expected_payload_type != 0U && pkt.payload_type != config_.expected_payload_type) {
        ++stats_.parse_errors;
        return false;
    }
    if (pkt.payload.size() > kMaxRtpPayloadBytes) {
        ++stats_.parse_errors;
        return false;
    }

    ++stats_.packets_received;

    if (!initialized_) {
        initialized_ = true;
        next_expected_seq_ = pkt.sequence_number;
    }

    if (seq_before(pkt.sequence_number, next_expected_seq_)) {
        ++stats_.packets_dropped_late;
        return false;
    }

    const std::uint16_t dist = seq_distance(next_expected_seq_, pkt.sequence_number);
    if (dist >= config_.capacity_packets) {
        const std::uint16_t new_base = static_cast<std::uint16_t>(
            pkt.sequence_number - static_cast<std::uint16_t>(config_.capacity_packets / 2U));
        for (auto& slot : ring_) {
            if (slot.occupied && seq_before(slot.seq, new_base)) {
                slot.occupied = false;
                slot.length = 0U;
                --occupied_;
                ++stats_.sequence_gaps;
            }
        }
        next_expected_seq_ = new_base;
        maybe_trigger_idr(now);
    }

    if (pkt.sequence_number != next_expected_seq_) {
        ++stats_.packets_reordered;
    }

    const std::size_t idx = static_cast<std::size_t>(pkt.sequence_number) % config_.capacity_packets;
    auto& slot = ring_[idx];
    if (slot.occupied) {
        if (slot.seq == pkt.sequence_number) {
            ++stats_.packets_dropped_duplicate;
            return false;
        }
        --occupied_;
        ++stats_.sequence_gaps;
    }

    slot.occupied = true;
    slot.seq = pkt.sequence_number;
    slot.length = static_cast<std::uint16_t>(pkt.payload.size());
    slot.arrived_at = now;
    std::copy(pkt.payload.begin(), pkt.payload.end(), slot.payload.begin());
    ++occupied_;
    return true;
}

std::optional<std::size_t> RtpJitterBuffer::find_earliest_occupied_index() const noexcept {
    std::optional<std::size_t> best;
    for (std::size_t i = 0U; i < ring_.size(); ++i) {
        if (!ring_[i].occupied) {
            continue;
        }
        if (!best.has_value() || seq_before(ring_[i].seq, ring_[*best].seq)) {
            best = i;
        }
    }
    return best;
}

void RtpJitterBuffer::maybe_trigger_idr(std::chrono::steady_clock::time_point now) noexcept {
    if (!on_idr_) {
        return;
    }
    if (last_idr_request_.time_since_epoch().count() == 0 ||
        (now - last_idr_request_) >= config_.idr_cooldown) {
        last_idr_request_ = now;
        ++stats_.idr_requests_triggered;
        try {
            on_idr_();
        } catch (...) {
            // Suppress exceptions on real-time packet boundary.
        }
    }
}

std::optional<std::size_t> RtpJitterBuffer::pop_ready_into(
    std::span<std::uint8_t> out_buf,
    std::chrono::steady_clock::time_point now) noexcept {
    if (!initialized_ || occupied_ == 0U || out_buf.size() < kMaxRtpPayloadBytes) {
        return std::nullopt;
    }

    const std::size_t head_idx =
        static_cast<std::size_t>(next_expected_seq_) % config_.capacity_packets;
    if (ring_[head_idx].occupied && ring_[head_idx].seq == next_expected_seq_) {
        const std::size_t len = ring_[head_idx].length;
        std::copy_n(ring_[head_idx].payload.begin(), len, out_buf.begin());
        ring_[head_idx].occupied = false;
        ring_[head_idx].length = 0U;
        --occupied_;
        next_expected_seq_ = static_cast<std::uint16_t>(next_expected_seq_ + 1U);
        ++stats_.packets_emitted;
        return len;
    }

    const auto earliest = find_earliest_occupied_index();
    if (!earliest.has_value()) {
        return std::nullopt;
    }

    const auto elapsed = now - ring_[*earliest].arrived_at;
    if (elapsed < config_.max_hold_time && occupied_ < (config_.capacity_packets / 2U)) {
        return std::nullopt;
    }

    const std::uint16_t missing_span = seq_distance(next_expected_seq_, ring_[*earliest].seq);
    stats_.sequence_gaps += missing_span;
    maybe_trigger_idr(now);

    const std::size_t len = ring_[*earliest].length;
    const std::uint16_t seq = ring_[*earliest].seq;
    std::copy_n(ring_[*earliest].payload.begin(), len, out_buf.begin());
    ring_[*earliest].occupied = false;
    ring_[*earliest].length = 0U;
    --occupied_;
    next_expected_seq_ = static_cast<std::uint16_t>(seq + 1U);
    ++stats_.packets_emitted;
    return len;
}

}  // namespace picast
