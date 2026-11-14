/**
 * @file test_rtp_jitter.cpp
 * @brief Offline unit tests for the `picast::RtpJitterBuffer` and RFC 3550 parser.
 */

#include "picast/rtp_jitter.hpp"

#include <array>
#include <chrono>
#include <cstdint>
#include <iostream>
#include <string_view>
#include <vector>

namespace {

int g_failures = 0;

void expect_true(bool cond, std::string_view expr, int line) {
    if (!cond) {
        std::cerr << "[FAIL] line " << line << ": " << expr << '\n';
        ++g_failures;
    }
}

#define EXPECT_TRUE(cond) expect_true(static_cast<bool>(cond), #cond, __LINE__)

std::vector<std::uint8_t> make_rtp(
    std::uint16_t seq,
    std::span<const std::uint8_t> payload,
    std::uint8_t pt = 33U) {
    std::vector<std::uint8_t> pkt(12U + payload.size(), 0U);
    pkt[0] = 0x80U;
    pkt[1] = static_cast<std::uint8_t>(pt & 0x7FU);
    pkt[2] = static_cast<std::uint8_t>((seq >> 8U) & 0xFFU);
    pkt[3] = static_cast<std::uint8_t>(seq & 0xFFU);
    std::copy(payload.begin(), payload.end(), pkt.begin() + 12U);
    return pkt;
}

void test_rtp_sequence_wraparound_and_reordering() {
    EXPECT_TRUE(picast::seq_before(65534U, 65535U));
    EXPECT_TRUE(picast::seq_before(65535U, 0U));
    EXPECT_TRUE(picast::seq_before(65535U, 10U));
    EXPECT_TRUE(!picast::seq_before(10U, 65535U));

    bool idr_called = false;
    picast::RtpJitterBuffer jitter(
        picast::JitterConfig{
            .capacity_packets = 64U,
            .max_hold_time = std::chrono::milliseconds{30},
            .idr_cooldown = std::chrono::milliseconds{100},
        },
        [&]() { idr_called = true; });

    const auto t0 = std::chrono::steady_clock::now();
    const std::array<std::uint8_t, 4> p1{0x47U, 1U, 2U, 3U};
    const std::array<std::uint8_t, 4> p2{0x47U, 4U, 5U, 6U};
    const std::array<std::uint8_t, 4> p3{0x47U, 7U, 8U, 9U};

    EXPECT_TRUE(jitter.push_datagram(make_rtp(65535U, p1), t0));
    EXPECT_TRUE(jitter.push_datagram(make_rtp(1U, p3), t0));

    std::vector<std::uint8_t> emitted;
    const auto n1 = jitter.drain([&](std::span<const std::uint8_t> chunk) {
        emitted.insert(emitted.end(), chunk.begin(), chunk.end());
    }, t0);
    EXPECT_TRUE(n1 == 1U);
    EXPECT_TRUE(emitted == std::vector<std::uint8_t>(p1.begin(), p1.end()));
    EXPECT_TRUE(!idr_called);

    EXPECT_TRUE(jitter.push_datagram(make_rtp(0U, p2), t0 + std::chrono::milliseconds{5}));
    const auto n2 = jitter.drain([&](std::span<const std::uint8_t> chunk) {
        emitted.insert(emitted.end(), chunk.begin(), chunk.end());
    }, t0 + std::chrono::milliseconds{5});
    EXPECT_TRUE(n2 == 2U);
    EXPECT_TRUE(emitted.size() == 12U);
    EXPECT_TRUE(emitted[5] == 4U && emitted[9] == 7U);
    EXPECT_TRUE(!idr_called);
}

void test_rtp_gap_triggers_idr_after_hold_deadline() {
    int idr_count = 0;
    picast::RtpJitterBuffer jitter(
        picast::JitterConfig{
            .capacity_packets = 32U,
            .max_hold_time = std::chrono::milliseconds{25},
            .idr_cooldown = std::chrono::milliseconds{200},
        },
        [&]() { ++idr_count; });

    const auto t0 = std::chrono::steady_clock::now();
    const std::array<std::uint8_t, 2> p_ok{0x47U, 0xAAU};
    const std::array<std::uint8_t, 2> p_skip{0x47U, 0xBBU};

    EXPECT_TRUE(jitter.push_datagram(make_rtp(10U, p_ok), t0));
    EXPECT_TRUE(jitter.push_datagram(make_rtp(12U, p_skip), t0));

    std::size_t drained = 0U;
    drained += jitter.drain([](std::span<const std::uint8_t>) {}, t0 + std::chrono::milliseconds{5});
    EXPECT_TRUE(drained == 1U);
    EXPECT_TRUE(idr_count == 0);

    drained += jitter.drain([](std::span<const std::uint8_t>) {}, t0 + std::chrono::milliseconds{40});
    EXPECT_TRUE(drained == 2U);
    EXPECT_TRUE(idr_count == 1);
    EXPECT_TRUE(jitter.stats().sequence_gaps == 1U);
}

void test_non_power_of_two_wraparound_has_no_collision() {
    picast::RtpJitterBuffer jitter(picast::JitterConfig{
        .capacity_packets = 100U,
        .max_hold_time = std::chrono::milliseconds{50},
    });
    const auto t0 = std::chrono::steady_clock::now();
    const std::array<std::uint8_t, 2> head_payload{0x47U, 0x01U};
    const std::array<std::uint8_t, 2> wrap_payload{0x47U, 0x02U};

    // seq 65500 and seq 0 have distance 36 (< 100) and previously collided on (seq % 100 == 0).
    EXPECT_TRUE(jitter.push_datagram(make_rtp(65500U, head_payload), t0));
    EXPECT_TRUE(jitter.push_datagram(make_rtp(65501U, head_payload), t0));
    EXPECT_TRUE(jitter.push_datagram(make_rtp(0U, wrap_payload), t0));
    EXPECT_TRUE(jitter.occupied() == 3U);
}

void test_next_deadline_reports_hold_expiry_during_gap() {
    picast::RtpJitterBuffer jitter(picast::JitterConfig{
        .capacity_packets = 32U,
        .max_hold_time = std::chrono::milliseconds{25},
    });
    const auto t0 = std::chrono::steady_clock::now();
    const std::array<std::uint8_t, 2> p{0x47U, 0x01U};

    // Empty buffer: nothing to wait for.
    EXPECT_TRUE(!jitter.next_deadline().has_value());

    EXPECT_TRUE(jitter.push_datagram(make_rtp(10U, p), t0));
    // In-order head packet is ready for immediate emission: no deadline.
    EXPECT_TRUE(!jitter.next_deadline().has_value());

    EXPECT_TRUE(jitter.push_datagram(make_rtp(12U, p), t0));
    EXPECT_TRUE(!jitter.next_deadline().has_value());  // Head seq 10 still ready.

    std::array<std::uint8_t, picast::kMaxRtpPayloadBytes> scratch{};
    EXPECT_TRUE(jitter.pop_ready_into(scratch, t0).has_value());  // Emits seq 10.
    EXPECT_TRUE(jitter.occupied() == 1U);

    // Seq 11 is missing; seq 12 arrived at t0, so hold expiry is t0 + 25 ms.
    const auto deadline = jitter.next_deadline();
    EXPECT_TRUE(deadline.has_value());
    EXPECT_TRUE(*deadline == t0 + std::chrono::milliseconds{25});

    const auto n = jitter.drain([](std::span<const std::uint8_t>) {}, *deadline);
    EXPECT_TRUE(n == 1U);
    EXPECT_TRUE(jitter.occupied() == 0U);
    EXPECT_TRUE(!jitter.next_deadline().has_value());
}

void test_drain_coalesced_bursts_in_order_packets_into_single_chunk() {
    picast::RtpJitterBuffer jitter(picast::JitterConfig{
        .capacity_packets = 64U,
        .max_hold_time = std::chrono::milliseconds{30},
    });
    const auto t0 = std::chrono::steady_clock::now();
    const std::array<std::uint8_t, 4> payload{0x47U, 0x11U, 0x22U, 0x33U};
    for (std::uint16_t seq = 100U; seq < 110U; ++seq) {
        EXPECT_TRUE(jitter.push_datagram(make_rtp(seq, payload), t0));
    }

    std::vector<std::size_t> chunk_sizes;
    std::vector<std::uint8_t> bytes;
    const auto packets = jitter.drain_coalesced([&](std::span<const std::uint8_t> chunk) {
        chunk_sizes.push_back(chunk.size());
        bytes.insert(bytes.end(), chunk.begin(), chunk.end());
    }, t0);
    EXPECT_TRUE(packets == 10U);
    EXPECT_TRUE(chunk_sizes.size() == 1U);
    EXPECT_TRUE(chunk_sizes.front() == 40U);
    EXPECT_TRUE(bytes.size() == 40U);
    for (std::size_t i = 0U; i < bytes.size(); i += payload.size()) {
        EXPECT_TRUE(bytes[i] == 0x47U && bytes[i + 1U] == 0x11U);
    }
    EXPECT_TRUE(jitter.stats().packets_emitted == 10U);
    EXPECT_TRUE(jitter.occupied() == 0U);

    const auto none = jitter.drain_coalesced([](std::span<const std::uint8_t>) {}, t0);
    EXPECT_TRUE(none == 0U);
}

void test_drain_coalesced_flushes_at_32kib_boundary() {
    picast::RtpJitterBuffer jitter(picast::JitterConfig{
        .capacity_packets = 64U,
        .max_hold_time = std::chrono::milliseconds{30},
    });
    const auto t0 = std::chrono::steady_clock::now();
    std::array<std::uint8_t, 1400> payload{};
    payload[0] = 0x47U;
    for (std::uint16_t seq = 0U; seq < 24U; ++seq) {
        EXPECT_TRUE(jitter.push_datagram(make_rtp(seq, payload), t0));
    }

    std::vector<std::size_t> chunk_sizes;
    const auto packets = jitter.drain_coalesced([&](std::span<const std::uint8_t> chunk) {
        chunk_sizes.push_back(chunk.size());
    }, t0);
    EXPECT_TRUE(packets == 24U);
    EXPECT_TRUE(chunk_sizes.size() == 2U);
    EXPECT_TRUE(chunk_sizes[0] == 23U * 1400U);  // 32200 <= 32768 bytes fit in one burst.
    EXPECT_TRUE(chunk_sizes[1] == 1400U);       // 33600 would exceed the 32 KiB chunk limit.
    EXPECT_TRUE(jitter.stats().packets_emitted == 24U);
}

void test_drain_coalesced_skips_gap_after_hold_expiry() {
    int idr_count = 0;
    picast::RtpJitterBuffer jitter(
        picast::JitterConfig{
            .capacity_packets = 32U,
            .max_hold_time = std::chrono::milliseconds{25},
            .idr_cooldown = std::chrono::milliseconds{200},
        },
        [&]() { ++idr_count; });
    const auto t0 = std::chrono::steady_clock::now();
    const std::array<std::uint8_t, 2> p_ok{0x47U, 0xAAU};
    const std::array<std::uint8_t, 2> p_skip{0x47U, 0xBBU};
    const std::array<std::uint8_t, 2> p_next{0x47U, 0xCCU};

    EXPECT_TRUE(jitter.push_datagram(make_rtp(10U, p_ok), t0));
    EXPECT_TRUE(jitter.push_datagram(make_rtp(12U, p_skip), t0));
    EXPECT_TRUE(jitter.push_datagram(make_rtp(13U, p_next), t0));

    std::vector<std::uint8_t> chunk;
    std::size_t chunks = 0U;
    const auto packets = jitter.drain_coalesced([&](std::span<const std::uint8_t> c) {
        ++chunks;
        chunk.assign(c.begin(), c.end());
    }, t0 + std::chrono::milliseconds{25});
    EXPECT_TRUE(packets == 3U);
    EXPECT_TRUE(chunks == 1U);
    EXPECT_TRUE(chunk.size() == 6U);
    EXPECT_TRUE(chunk[0] == 0x47U && chunk[1] == 0xAAU);
    EXPECT_TRUE(chunk[2] == 0x47U && chunk[3] == 0xBBU);
    EXPECT_TRUE(chunk[4] == 0x47U && chunk[5] == 0xCCU);
    EXPECT_TRUE(idr_count == 1);
    EXPECT_TRUE(jitter.stats().sequence_gaps == 1U);
}

void test_earliest_tracking_survives_eviction_and_reordering() {
    picast::RtpJitterBuffer jitter(picast::JitterConfig{
        .capacity_packets = 16U,
        .max_hold_time = std::chrono::milliseconds{0},
    });
    const auto t0 = std::chrono::steady_clock::now();
    const std::array<std::uint8_t, 2> p{0x47U, 0x55U};

    // Far-future jump triggers eviction of the whole window, then refills
    // out of order; the O(1) earliest tracking must still emit every packet.
    EXPECT_TRUE(jitter.push_datagram(make_rtp(0U, p), t0));
    EXPECT_TRUE(jitter.push_datagram(make_rtp(1U, p), t0));
    EXPECT_TRUE(jitter.push_datagram(make_rtp(500U, p), t0 + std::chrono::milliseconds{1}));
    EXPECT_TRUE(jitter.push_datagram(make_rtp(502U, p), t0 + std::chrono::milliseconds{1}));
    EXPECT_TRUE(jitter.push_datagram(make_rtp(501U, p), t0 + std::chrono::milliseconds{1}));
    EXPECT_TRUE(jitter.occupied() == 3U);  // seq 0/1 evicted by the seq-500 jump; 500-502 held.

    std::vector<std::uint8_t> emitted;
    const auto n = jitter.drain([&](std::span<const std::uint8_t> chunk) {
        emitted.insert(emitted.end(), chunk.begin(), chunk.end());
    }, t0 + std::chrono::milliseconds{2});
    EXPECT_TRUE(n == 3U);
    EXPECT_TRUE(emitted.size() == 6U);
    EXPECT_TRUE(emitted[1] == 0x55U && emitted[3] == 0x55U && emitted[5] == 0x55U);
    EXPECT_TRUE(jitter.occupied() == 0U);
}

}  // namespace

int main() {
    test_rtp_sequence_wraparound_and_reordering();
    test_rtp_gap_triggers_idr_after_hold_deadline();
    test_non_power_of_two_wraparound_has_no_collision();
    test_next_deadline_reports_hold_expiry_during_gap();
    test_drain_coalesced_bursts_in_order_packets_into_single_chunk();
    test_drain_coalesced_flushes_at_32kib_boundary();
    test_drain_coalesced_skips_gap_after_hold_expiry();
    test_earliest_tracking_survives_eviction_and_reordering();

    if (g_failures > 0) {
        std::cerr << g_failures << " assertion(s) failed.\n";
        return 1;
    }
    std::cout << "All picast_rtp_jitter unit tests passed.\n";
    return 0;
}
