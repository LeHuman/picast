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

}  // namespace

int main() {
    test_rtp_sequence_wraparound_and_reordering();
    test_rtp_gap_triggers_idr_after_hold_deadline();
    test_non_power_of_two_wraparound_has_no_collision();

    if (g_failures > 0) {
        std::cerr << g_failures << " assertion(s) failed.\n";
        return 1;
    }
    std::cout << "All picast_rtp_jitter unit tests passed.\n";
    return 0;
}
