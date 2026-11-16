/**
 * @file app_config.hpp
 * @brief Command-line configuration parser for the `picast-player` C++26 binary.
 */

#pragma once

#include "picast/media_pipeline.hpp"

#include <cstdint>
#include <expected>
#include <span>
#include <string>

namespace picast {

/** @brief Top-level CLI configuration for `picast-player`. */
struct AppConfig {
    std::uint16_t rtp_port{1028U};
    std::uint16_t idr_port{0U};
    std::uint16_t jitter_ms{45U};
    PipelineConfig pipeline{};
};

/**
 * @brief Parse `argc`/`argv` into a validated `AppConfig`.
 * @param args Span of C-string arguments including `argv[0]`.
 * @return Parsed configuration or human-readable CLI usage error.
 */
[[nodiscard]] std::expected<AppConfig, std::string> parse_app_args(
    std::span<const char* const> args);

}  // namespace picast
