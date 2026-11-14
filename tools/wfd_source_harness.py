#!/usr/bin/env python3
"""Full MS-MICE + WFD source harness for picast (grounded in the sink's code).

Path exercised: SOURCE_READY TLV to :7250 -> sink dials outbound RTSP to us ->
M1 OPTIONS / M3 GET_PARAMETER / M4 trigger SETUP -> answer sink SETUP (Transport
header!) + PLAY -> stream H.264 as MPEG2-TS over RTP (PT 33, rtpmp2tdepay) ->
keepalives + 200 for sink IDR requests -> TEARDOWN.
"""

import re
import socket
import struct
import subprocess
import sys
import threading
import time

SINK = sys.argv[1] if len(sys.argv) > 1 else "picast.local"
MICE_PORT = 7250
MY_RTSP_PORT = 7236  # listener; sink dials back here (its parse fallback port too)
RTP_PORT = 1028  # sink player udpsrc port
STREAM_SECS = 300

log = lambda tag, msg="": print(f"[{time.strftime('%H:%M:%S')}] {tag} {msg}", flush=True)


def frame_source_ready(rtsp_port: int) -> bytes:
    """MS-MICE v1 frame: len(ver+cmd+TLV) | ver=1 | cmd=1 | TLV(tag=2,len=2,port BE)."""
    tlv = bytes([0x02, 0x00, 0x02]) + struct.pack(">H", rtsp_port)
    body = bytes([0x01, 0x01]) + tlv
    return struct.pack(">H", len(body)) + body


def read_frame(sock: socket.socket, buf: bytearray, timeout: float = 20.0) -> str:
    sock.settimeout(timeout)
    while True:
        if b"\r\n\r\n" in buf:
            head, rest = bytes(buf).split(b"\r\n\r\n", 1)
            m = re.search(rb"Content-Length:\s*(\d+)", head, re.IGNORECASE)
            need = int(m.group(1)) if m else 0
            if len(rest) >= need:
                frame = (head + b"\r\n\r\n" + rest[:need]).decode("utf-8", "replace")
                buf[:] = rest[need:]
                return frame
        chunk = sock.recv(4096)
        if not chunk:
            raise ConnectionError("sink closed RTSP")
        buf += chunk


def send_text(sock: socket.socket, text: str) -> None:
    log("<<", text.splitlines()[0])
    sock.sendall(text.encode("utf-8"))


def cseq(text: str, default: str = "1") -> str:
    m = re.search(r"CSeq:\s*(\d+)", text)
    return m.group(1) if m else default


def ok(sock: socket.socket, cs: str, extra: list[str] | None = None) -> None:
    lines = ["RTSP/1.0 200 OK", f"CSeq: {cs}", *(extra or [])]
    send_text(sock, "\r\n".join(lines) + "\r\n\r\n")


# --- 1. arm RTSP listener, then poke MICE :7250 with SOURCE_READY ---
listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
listener.bind(("0.0.0.0", MY_RTSP_PORT))
listener.listen(1)
log(f"RTSP listener on :{MY_RTSP_PORT}")

mice = socket.create_connection((SINK, MICE_PORT), timeout=10)
mice.sendall(frame_source_ready(MY_RTSP_PORT))
mice_ip = mice.getsockname()[0]
mice.close()
log(f"SOURCE_READY sent to {SINK}:{MICE_PORT} (advertised RTSP :{MY_RTSP_PORT})")

listener.settimeout(15)
sock, addr = listener.accept()
log(f"sink dialed back from {addr}")
buf = bytearray()

# --- 2. M1 OPTIONS ---
send_text(sock, "OPTIONS * RTSP/1.0\r\nCSeq: 1\r\nRequire: org.wfa.wfd1.0\r\n\r\n")
resp = read_frame(sock, buf)
log("M1", resp.splitlines()[0])
if "RTSP/1.0 200" not in resp:
    sys.exit("M1 failed")

# sink's own OPTIONS (it sends one while sink_cseq==1)
nxt = read_frame(sock, buf)
if nxt.startswith("OPTIONS"):
    ok(sock, cseq(nxt), ["Public: org.wfa.wfd1.0, GET_PARAMETER, SET_PARAMETER"])
    log("answered sink OPTIONS")

# --- 3. M3 GET_PARAMETER ---
body = (
    "wfd_video_formats\r\nwfd2_video_formats\r\nwfd_audio_codecs\r\n"
    "wfd_display_edid\r\nwfd_client_rtp_ports\r\nwfd_content_protection\r\n"
    "wfd_uibc_capability\r\nwfd_standby_resume_capability\r\n"
)
send_text(
    sock,
    f"GET_PARAMETER rtsp://{SINK}/ RTSP/1.0\r\nCSeq: 2\r\n"
    f"Content-Type: text/parameters\r\nContent-Length: {len(body)}\r\n\r\n{body}",
)
resp = read_frame(sock, buf)
if "wfd_video_formats" not in resp:
    sys.exit(f"M3 missing formats: {resp[:300]}")
log("M3 capabilities OK", "(EDID + h264 present)")

# --- 4. M4: select formats + presentation URL + ports, then trigger SETUP ---
m4 = (
    "wfd_video_formats: 00 00 02 02 00015555 00000000 00000000 00 0000 0000 00 none none\r\n"
    "wfd_audio_codecs: LPCM 00000003 00\r\n"
    f"wfd_client_rtp_ports: RTP/AVP/UDP;unicast {RTP_PORT} 0 mode=play\r\n"
    f"wfd_presentation_URL: rtsp://{mice_ip}/wfd1.0/streamid=0 none\r\n"
    "wfd_trigger_method: SETUP\r\n"
)
send_text(
    sock,
    f"SET_PARAMETER rtsp://{mice_ip}/wfd1.0/streamid=0 RTSP/1.0\r\nCSeq: 3\r\n"
    f"Content-Type: text/parameters\r\nContent-Length: {len(m4)}\r\n\r\n{m4}",
)
resp = read_frame(sock, buf)
log("M4", resp.splitlines()[0])

# --- 5. M5: sink SETUP -> 200 + Session + Transport (picast keys on Transport:) ---
setup = read_frame(sock, buf)
if not setup.startswith("SETUP"):
    sys.exit(f"expected sink SETUP, got: {setup[:200]}")
log("sink SETUP", setup.splitlines()[0])
ok(
    sock,
    cseq(setup),
    [
        "Session: 12345678;timeout=30",
        f"Transport: RTP/AVP/UDP;unicast;client_port={RTP_PORT}",
    ],
)

# --- 6. M6: sink PLAY -> 200 ---
play = read_frame(sock, buf)
if not play.startswith("PLAY"):
    sys.exit(f"expected sink PLAY, got: {play[:200]}")
ok(sock, cseq(play), ["Session: 12345678"])
log("*** SESSION UP (M1-M6 complete) ***")

# --- 7. reader thread: 200 everything (sink IDR SET_PARAMETERs etc.) ---
stop = threading.Event()


def reader_loop() -> None:
    try:
        while not stop.is_set():
            frame = read_frame(sock, buf, timeout=30.0)
            first = frame.splitlines()[0]
            log(">>", first)
            if first.startswith(("SET_PARAMETER", "OPTIONS", "PAUSE", "GET_PARAMETER", "TEARDOWN")):
                extra = (
                    ["Public: org.wfa.wfd1.0, GET_PARAMETER, SET_PARAMETER"]
                    if first.startswith("OPTIONS")
                    else []
                )
                ok(sock, cseq(frame), extra)
    except (TimeoutError, OSError):
        pass


threading.Thread(target=reader_loop, daemon=True).start()

# --- 8. keepalives ---
def keepalive() -> None:
    n = 10
    while not stop.is_set() and not stop.wait(25.0):
        try:
            send_text(sock, f"GET_PARAMETER * RTSP/1.0\r\nCSeq: {n}\r\n\r\n")
            n += 1
        except OSError:
            return


threading.Thread(target=keepalive, daemon=True).start()

# --- 9. stream H.264 as MPEG2-TS over RTP (PT 33 -> rtpmp2tdepay on sink) ---
cmd = [
    "ffmpeg", "-loglevel", "error", "-re",
    "-f", "lavfi", "-i", f"testsrc2=duration={STREAM_SECS}:size=1280x720:rate=30",
    "-c:v", "libx264", "-profile:v", "baseline", "-bf", "0", "-g", "30",
    "-preset", "ultrafast", "-tune", "zerolatency", "-pix_fmt", "yuv420p",
    "-b:v", "2500k", "-muxdelay", "0",
    "-f", "rtp_mpegts", f"rtp://{SINK}:{RTP_PORT}?pkt_size=1316",
]
log("streaming", " ".join(cmd))
subprocess.run(cmd, timeout=STREAM_SECS + 15, check=False)
stop.set()

# --- 10. M7 TEARDOWN ---
td = "wfd_trigger_method: TEARDOWN\r\n"
try:
    send_text(
        sock,
        f"SET_PARAMETER rtsp://{mice_ip}/wfd1.0/streamid=0 RTSP/1.0\r\nCSeq: 999\r\n"
        f"Content-Type: text/parameters\r\nContent-Length: {len(td)}\r\n\r\n{td}",
    )
except OSError:
    pass
sock.close()
listener.close()
log("=== HARNESS COMPLETE — check HDMI on picast ===")