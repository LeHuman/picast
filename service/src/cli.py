"""Command-line interface (`picast`), guided configurator, doctor, and KMS display ops."""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
import re
import shutil
import subprocess
import sys
from collections.abc import Sequence
from pathlib import Path

from .config import (
    DEFAULT_CONFIG_PATH,
    SETTING_GUIDES,
    AudioSinkMode,
    CodecPreference,
    PinMode,
    SyncMode,
    TransportMode,
    WifiBandMode,
    load_config,
    save_config,
)
from .daemon import PicastDaemon
from .edid import inspect_display
from .platform import (
    probe_host_capabilities,
    resolve_effective_fps,
    resolve_p2p_ht40_allowed,
    resolve_p2p_oper_freq,
    resolve_wfd2_hevc_enabled,
)

logger = logging.getLogger(__name__)
CMDLINE_PATH = Path("/boot/firmware/cmdline.txt")
SAVED_TARGET_STATE = Path("/var/lib/picast/previous-default-target")
FIRMWARE_EDID_PATH = Path("/lib/firmware/picast-edid.bin")
_KMS_MODE_RE = re.compile(r"^\d{3,4}x\d{3,4}(?:@\d{2,3}[A-Za-z]?)?$")


def _prompt_with_guide(key: str, current: str) -> str:
    guide = SETTING_GUIDES.get(key)
    if guide:
        print(f"\n[{key}] {guide.summary}")
        print(f"  Recommended: {guide.recommended}")
    raw = input(f"  Enter {key} [{current}]: ").strip()
    return raw if raw else current


def _prompt_bool_with_guide(key: str, current: bool) -> bool:
    cur_str = "true" if current else "false"
    ans = _prompt_with_guide(key, cur_str).lower()
    return ans in {"1", "true", "yes", "y", "on"}


def cmd_configure(config_path: Path, non_interactive: bool) -> int:
    """Run guided configuration setup explaining every option concisely."""
    caps = probe_host_capabilities()
    cfg = load_config(config_path)

    print(f"Detected hardware: {caps.model_name} ({caps.board.value})")
    if not non_interactive:
        cfg.device_name = _prompt_with_guide("device_name", cfg.device_name)

        t_ans = _prompt_with_guide("transport_mode", cfg.transport_mode.value).lower()
        if t_ans in {m.value for m in TransportMode}:
            cfg.transport_mode = TransportMode(t_ans)

        p_ans = _prompt_with_guide("pin_mode", cfg.pin_mode.value).lower()
        if p_ans in {m.value for m in PinMode}:
            cfg.pin_mode = PinMode(p_ans)
        if cfg.pin_mode is PinMode.FIXED:
            cfg.fixed_pin = _prompt_with_guide("fixed_pin", cfg.fixed_pin)

        cfg.max_fps = _prompt_with_guide("max_fps", cfg.max_fps)
        j_ans = _prompt_with_guide("jitter_ms", str(cfg.jitter_ms))
        if j_ans.isdigit():
            cfg.jitter_ms = int(j_ans)

        s_ans = _prompt_with_guide("sync_mode", cfg.sync_mode.value).lower()
        if s_ans in {m.value for m in SyncMode}:
            cfg.sync_mode = SyncMode(s_ans)

        c_ans = _prompt_with_guide("codec", cfg.codec.value).lower()
        if c_ans in {m.value for m in CodecPreference}:
            cfg.codec = CodecPreference(c_ans)

        b_ans = _prompt_with_guide("wifi_band", cfg.wifi_band.value).lower()
        if b_ans in {m.value for m in WifiBandMode}:
            cfg.wifi_band = WifiBandMode(b_ans)

        cfg.ht40 = _prompt_bool_with_guide("ht40", cfg.ht40)
        cfg.wifi_power_save = _prompt_bool_with_guide("wifi_power_save", cfg.wifi_power_save)
        cfg.suppress_bg_scans = _prompt_bool_with_guide(
            "suppress_bg_scans", cfg.suppress_bg_scans
        )
        cfg.tcp_nodelay = _prompt_bool_with_guide("tcp_nodelay", cfg.tcp_nodelay)
        cfg.idr_on_loss = _prompt_bool_with_guide("idr_on_loss", cfg.idr_on_loss)
        cfg.uibc_enabled = _prompt_bool_with_guide("uibc_enabled", cfg.uibc_enabled)
        a_ans = _prompt_with_guide("audio_sink", cfg.audio_sink.value).lower()
        if a_ans in {m.value for m in AudioSinkMode}:
            cfg.audio_sink = AudioSinkMode(a_ans)

    save_config(cfg, config_path)
    print(f"\nSaved configuration to {config_path}")
    return 0


def cmd_doctor(config_path: Path) -> int:
    """Validate hardware decode nodes, DRM/EDID, binaries, and Wi-Fi settings."""
    cfg = load_config(config_path)
    caps = probe_host_capabilities()
    disp = inspect_display()

    print("=== picast doctor ===")
    print(f"Board:            {caps.model_name} ({caps.board.value})")
    print(f"V4L2 H.264 (/dev/video10): {'yes' if caps.has_v4l2_h264 else 'no'}")
    print(f"V4L2 HEVC (/dev/video19):  {'yes' if caps.has_v4l2_hevc else 'no'}")
    print(f"Effective FPS cap:         {resolve_effective_fps(cfg, caps)}")
    print(f"WFD2 HEVC advertised:      {'yes' if resolve_wfd2_hevc_enabled(cfg, caps) else 'no'}")
    print(
        f"P2P Freq / HT40:           {resolve_p2p_oper_freq(cfg, caps)} MHz "
        f"(HT40={'on' if resolve_p2p_ht40_allowed(cfg, caps) else 'off'})"
    )
    print(
        f"DRM HDMI Display:          {disp.connector} "
        f"({'connected' if disp.connected else 'disconnected'}) "
        f"{disp.width}x{disp.height}@{disp.refresh_hz}"
    )

    for binary in ("picast-player", "wpa_cli", "iw", "dnsmasq", "avahi-publish-service"):
        loc = shutil.which(binary) or "MISSING"
        print(f"Binary {binary:<20} {loc}")

    if CMDLINE_PATH.exists():
        cmdline = CMDLINE_PATH.read_text(encoding="utf-8", errors="ignore")
        kms_forced = "vc4.force_hotplug=1" in cmdline
        print(f"KMS Always-On HDMI:        {'enabled' if kms_forced else 'disabled'}")
    return 0


def update_cmdline_kms_hotplug(
    cmdline_text: str,
    enable: bool,
    mode: str,
    connector: str = "HDMI-A-1",
) -> str:
    """Safely add or remove `vc4.force_hotplug=1` & `video=<connector>:...` tokens."""
    if connector not in {"HDMI-A-1", "HDMI-A-2"}:
        raise ValueError(f"Unsupported DRM connector: {connector!r}")
    if enable and not _KMS_MODE_RE.match(mode.strip()):
        raise ValueError(f"Invalid KMS mode specification: {mode!r}")
    tokens = cmdline_text.strip().split()
    video_prefix = f"video={connector}:"
    filtered = [
        t
        for t in tokens
        if not t.startswith("vc4.force_hotplug=") and not t.startswith(video_prefix)
    ]
    if enable:
        filtered.append("vc4.force_hotplug=1")
        filtered.append(f"{video_prefix}{mode.strip()}")
    return " ".join(filtered) + "\n"


def _atomic_write_text(target: Path, content: str) -> None:
    tmp_path = target.with_name(f".{target.name}.tmp")
    tmp_path.write_text(content, encoding="utf-8")
    os.replace(tmp_path, target)


def cmd_display(
    action: str,
    mode: str,
    cmdline_path: Path = CMDLINE_PATH,
    connector: str = "HDMI-A-1",
) -> int:
    """Manage KMS Always-On HDMI boot arguments and persistent DRM EDID capture."""
    if action == "capture-edid":
        disp = inspect_display(preferred_connector=connector, override_edid_path=None)
        if not disp.connected or len(disp.edid_bytes) < 128:
            print("No connected HDMI EDID available to capture.", file=sys.stderr)
            return 1
        FIRMWARE_EDID_PATH.parent.mkdir(parents=True, exist_ok=True)
        FIRMWARE_EDID_PATH.write_bytes(disp.edid_bytes)
        print(f"Captured {len(disp.edid_bytes)}-byte EDID from {disp.connector}.")
        return 0

    if not cmdline_path.exists():
        print(f"Kernel cmdline file not found: {cmdline_path}", file=sys.stderr)
        return 1

    backup = cmdline_path.with_suffix(".txt.picast.bak")
    original = cmdline_path.read_text(encoding="utf-8")
    if not backup.exists():
        _atomic_write_text(backup, original)

    enable = action == "force-hdmi"
    try:
        updated = update_cmdline_kms_hotplug(
            original,
            enable=enable,
            mode=mode,
            connector=connector,
        )
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return 2

    _atomic_write_text(cmdline_path, updated)
    state_label = f"enabled ({connector}:{mode})" if enable else f"disabled ({connector})"
    print(f"KMS force-hotplug {state_label} in {cmdline_path}. Reboot to apply.")
    return 0


def _run_systemctl(systemctl: str, *args: str) -> int:
    try:
        res = subprocess.run([systemctl, *args], check=False, timeout=15.0)
        return res.returncode
    except (OSError, subprocess.TimeoutExpired) as exc:
        print(f"systemctl {' '.join(args)} failed: {exc}", file=sys.stderr)
        return 1


def cmd_service(action: str) -> int:
    """Enable/disable exclusive headless high-performance mode or pause/resume service."""
    systemctl = shutil.which("systemctl")
    if not systemctl:
        print("systemctl not found on this host.", file=sys.stderr)
        return 1

    if action == "enable":
        try:
            res = subprocess.run(
                [systemctl, "get-default"],
                check=False,
                timeout=10.0,
                capture_output=True,
                text=True,
            )
            current_target = res.stdout.strip()
        except (OSError, subprocess.TimeoutExpired) as exc:
            print(f"Failed checking default systemd target: {exc}", file=sys.stderr)
            return 1
        if current_target and current_target != "multi-user.target":
            SAVED_TARGET_STATE.parent.mkdir(parents=True, exist_ok=True)
            SAVED_TARGET_STATE.write_text(current_target + "\n", encoding="utf-8")
            rc = _run_systemctl(systemctl, "set-default", "multi-user.target")
            if rc != 0:
                return rc
        return _run_systemctl(systemctl, "enable", "--now", "picast.service")

    if action == "disable":
        rc = _run_systemctl(systemctl, "disable", "--now", "picast.service")
        if rc != 0:
            return rc
        if SAVED_TARGET_STATE.exists():
            prev = SAVED_TARGET_STATE.read_text(encoding="utf-8").strip()
            if prev:
                return _run_systemctl(systemctl, "set-default", prev)
        return 0

    if action == "pause":
        return _run_systemctl(systemctl, "stop", "picast.service")

    if action == "resume":
        return _run_systemctl(systemctl, "start", "picast.service")

    return 1


def build_parser() -> argparse.ArgumentParser:
    """Create the root `picast` CLI argument parser."""
    parser = argparse.ArgumentParser(
        prog="picast",
        description="picast — Low-latency Miracast receiver for Raspberry Pi OS Trixie",
    )
    parser.add_argument(
        "--config",
        type=Path,
        default=DEFAULT_CONFIG_PATH,
        help="Path to picast.conf",
    )
    parser.add_argument(
        "-v",
        "--verbose",
        action="store_true",
        help="Enable debug logging output",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("run", help="Run Miracast control plane daemon in foreground")

    cfg_p = sub.add_parser("configure", help="Interactive guided configuration with hints")
    cfg_p.add_argument(
        "--non-interactive",
        action="store_true",
        help="Write validated defaults/existing config without prompting",
    )

    sub.add_parser("doctor", help="Inspect codecs, DRM HDMI status, binaries, and Wi-Fi")
    sub.add_parser("status", help="Show current picast config and systemd service state")

    disp_p = sub.add_parser("display", help="Manage KMS always-on HDMI hotplug & EDID")
    disp_p.add_argument(
        "action",
        choices=["force-hdmi", "unforce-hdmi", "capture-edid"],
    )
    disp_p.add_argument(
        "--mode",
        default="1920x1080@60D",
        help="KMS video mode string when enabling force-hdmi",
    )
    disp_p.add_argument(
        "--connector",
        default="HDMI-A-1",
        choices=["HDMI-A-1", "HDMI-A-2"],
        help="Target DRM HDMI connector (HDMI-A-2 available on Pi 4/5)",
    )

    svc_p = sub.add_parser("service", help="Toggle exclusive headless service & boot target")
    svc_p.add_argument(
        "action",
        choices=["enable", "disable", "pause", "resume"],
    )
    return parser


def _configure_logging(verbose: bool) -> None:
    level = logging.DEBUG if verbose else logging.INFO
    logging.basicConfig(
        level=level,
        format="%(levelname)s [%(name)s] %(message)s",
        force=True,
    )


def main(argv: Sequence[str] | None = None) -> int:
    """CLI entrypoint returning POSIX exit status."""
    args = build_parser().parse_args(argv)
    _configure_logging(bool(args.verbose))
    config_path: Path = args.config

    try:
        if args.command == "configure":
            return cmd_configure(config_path, args.non_interactive)
        if args.command == "doctor":
            return cmd_doctor(config_path)
        if args.command == "display":
            return cmd_display(args.action, args.mode, connector=args.connector)
        if args.command == "service":
            return cmd_service(args.action)
        if args.command == "status":
            cfg = load_config(config_path)
            print(
                f"device={cfg.device_name} transport={cfg.transport_mode.value} "
                f"pin_mode={cfg.pin_mode.value} codec={cfg.codec.value}"
            )
            return 0
        if args.command == "run":
            cfg = load_config(config_path)
            daemon = PicastDaemon(cfg=cfg)
            asyncio.run(daemon.run())
            return 0
    except KeyboardInterrupt:
        logger.info("Interrupted by user")
        return 130
    except Exception:
        logger.exception("Command %r failed unexpectedly", args.command)
        return 1
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
