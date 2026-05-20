#!/usr/bin/env python3
"""
hold-queue.py – Professional Music-on-Hold with announcement insertion.

Reads configuration from hold-queue-config.json by default,
or from a path passed as first argument.

Usage:
  ./hold-queue.py                          # use hold-queue-config.json
  ./hold-queue.py /pfad/zu/config.json     # custom config path
"""
import glob
import json
import logging
import math
import os
import random
import socket
import struct
import subprocess
import sys
import threading
import time
import wave

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), 'src'))

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)

from simple_sip.sip_sdp import parse_sdp


# ── Audio pipeline ───────────────────────────────────────────────

def linear2ulaw(sample: int) -> int:
    BIAS = 0x84; CLIP = 32635; sig = 0
    if sample < 0:
        sample = -sample; sig = 0x80
    if sample > CLIP: sample = CLIP
    sample += BIAS
    if sample < 0x100: e = 0
    elif sample < 0x200: e = 1
    elif sample < 0x400: e = 2
    elif sample < 0x800: e = 3
    elif sample < 0x1000: e = 4
    elif sample < 0x2000: e = 5
    elif sample < 0x4000: e = 6
    else: e = 7
    return (~(sig | (e << 4) | ((sample >> (e + 3)) & 0x0F))) & 0xFF


def pcm16_to_ulaw(pcm: bytes) -> bytes:
    out = bytearray(len(pcm) // 2)
    for i in range(0, len(pcm), 2):
        out[i // 2] = linear2ulaw(struct.unpack_from("<h", pcm, i)[0])
    return bytes(out)


def load_audio(path: str) -> bytes:
    ext = os.path.splitext(path)[1].lower()
    if ext == ".wav":
        return _load_wav(path)
    return _load_ffmpeg(path)


def _load_wav(path: str) -> bytes:
    with wave.open(path, "rb") as w:
        raw = w.readframes(w.getnframes())
        rate = w.getframerate()
        channels = w.getnchannels()
        sampwidth = w.getsampwidth()
    if sampwidth == 1:
        arr = __import__("array").array("b", raw)
        raw = struct.pack("<" + "h" * len(arr),
                          *((v << 8) - 32768 for v in arr))
    elif sampwidth != 2:
        raise ValueError(f"Unsupported sample width: {sampwidth}")
    if channels > 1:
        frames = struct.unpack("<" + "h" * (len(raw) // 2))
        mon = [sum(frames[i:i + channels]) // channels
               for i in range(0, len(frames), channels)]
        raw = struct.pack("<" + "h" * len(mon), *mon)
    if rate != 8000:
        raw = _resample(raw, rate, 8000)
    return raw


def _load_ffmpeg(path: str) -> bytes:
    if not _which("ffmpeg"):
        logging.error("ffmpeg not found – use .wav or install ffmpeg")
        sys.exit(1)
    cmd = ["ffmpeg", "-v", "quiet", "-i", path,
           "-f", "s16le", "-acodec", "pcm_s16le",
           "-ar", "8000", "-ac", "1", "-"]
    try:
        r = subprocess.run(cmd, capture_output=True, timeout=30)
    except subprocess.TimeoutExpired:
        logging.error("ffmpeg timeout"); sys.exit(1)
    if r.returncode != 0:
        logging.error("ffmpeg error: %s", r.stderr.decode(errors="replace"))
        sys.exit(1)
    return r.stdout


def _resample(raw: bytes, in_rate: int, out_rate: int) -> bytes:
    if not _which("ffmpeg"):
        logging.warning("Resampling unavailable (ffmpeg missing)")
        return raw
    cmd = ["ffmpeg", "-v", "quiet",
           "-f", "s16le", "-ar", str(in_rate), "-ac", "1", "-i", "-",
           "-f", "s16le", "-ar", str(out_rate), "-ac", "1", "-"]
    r = subprocess.run(cmd, input=raw, capture_output=True, timeout=30)
    return r.stdout if r.returncode == 0 else raw


def _which(name: str) -> bool:
    return subprocess.run(["which", name], capture_output=True).returncode == 0


def generate_tone(freq: float = 440, duration: float = 10.0) -> bytes:
    n = int(8000 * duration)
    samples = [int(16384 * math.sin(2 * math.pi * freq * (i / 8000)))
               for i in range(n)]
    return struct.pack("<" + "h" * n, *samples)


def load_ulaw(path: str) -> bytes:
    if not os.path.exists(path):
        logging.warning("File not found: %s", path)
        return b""
    return pcm16_to_ulaw(load_audio(path))


# ── RTP Streamer with interrupt support ──────────────────────────

class RTPStreamer:
    def __init__(self, remote_addr: tuple, pt: int = 0):
        self.remote = remote_addr
        self.pt = pt
        self.ssrc = random.randint(0, 0xFFFFFFFF)
        self._seq = 0
        self._ts = 0
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.sock.settimeout(1.0)
        self.running = False
        self._thread: threading.Thread = None
        self._loop_data: bytes = b""
        self._interrupt_data: bytes = b""
        self._interrupt_pos = 0
        self._playing_interrupt = False
        self._on_interrupt_done = None

    def set_loop(self, data: bytes):
        self._loop_data = data

    def play_once(self, data: bytes, on_done=None):
        self._interrupt_data = data
        self._interrupt_pos = 0
        self._playing_interrupt = True
        self._on_interrupt_done = on_done

    def start(self):
        self.running = True
        self._thread = threading.Thread(target=self._send_loop, daemon=True)
        self._thread.start()

    def stop(self):
        self.running = False
        try:
            self.sock.close()
        except OSError:
            pass

    def _build_packet(self, payload: bytes) -> bytes:
        return struct.pack(">BBHII",
            0x80, self.pt & 0x7F, self._seq, self._ts, self.ssrc) + payload

    def _send_loop(self):
        loop_offset = 0
        while self.running and self.sock.fileno() >= 0:
            chunk = 160
            if self._playing_interrupt and self._interrupt_data:
                seg = self._interrupt_data[
                    self._interrupt_pos:self._interrupt_pos + chunk]
                if len(seg) < chunk:
                    self._playing_interrupt = False
                    cb = self._on_interrupt_done
                    self._on_interrupt_done = None
                    self._interrupt_data = b""
                    self._interrupt_pos = 0
                    if cb:
                        cb()
                    continue
                self._interrupt_pos += chunk
            else:
                if not self._loop_data:
                    time.sleep(0.1)
                    continue
                seg = self._loop_data[loop_offset:loop_offset + chunk]
                if len(seg) < chunk:
                    loop_offset = 0
                    seg = self._loop_data[:chunk]
                loop_offset += chunk
            try:
                self.sock.sendto(self._build_packet(seg), self.remote)
            except OSError:
                break
            self._seq = (self._seq + 1) & 0xFFFF
            self._ts = (self._ts + len(seg)) & 0xFFFFFFFF
            time.sleep(chunk / 8000.0)


# ── Announcement Scheduler ───────────────────────────────────────

class AnnouncementScheduler:
    def __init__(self, streamer: RTPStreamer,
                 announcements: list,
                 interval_min: float,
                 interval_max: float):
        self.streamer = streamer
        self.announcements = announcements
        self.interval_min = interval_min
        self.interval_max = interval_max
        self.running = False
        self._timer: threading.Timer = None

    def start(self):
        self.running = True
        self._schedule_next()

    def stop(self):
        self.running = False
        if self._timer:
            self._timer.cancel()
            self._timer = None

    def _schedule_next(self):
        if not self.running or not self.announcements:
            return
        delay = random.uniform(self.interval_min, self.interval_max)
        self._timer = threading.Timer(delay, self._fire)
        self._timer.daemon = True
        self._timer.start()

    def _fire(self):
        if not self.running:
            return
        ann = random.choice(self.announcements)
        logging.info("Playing announcement (%d bytes, %.1fs)",
                     len(ann), len(ann) / 8000)
        self.streamer.play_once(ann, on_done=self._on_announcement_done)

    def _on_announcement_done(self):
        self._schedule_next()


# ── SIP integration ──────────────────────────────────────────────

def extract_remote_addr(invite_body: str) -> tuple:
    sdp = parse_sdp(invite_body)
    if not sdp or not sdp.media:
        raise ValueError("No SDP media in INVITE")
    m = sdp.media[0]
    return (sdp.unicast_address, m.port)


# ── Config loader ────────────────────────────────────────────────

DEFAULT_CONFIG_PATH = os.path.join(
    os.path.dirname(os.path.abspath(__file__)),
    "hold-queue-config.json")


def load_config(path: str) -> dict:
    if not os.path.exists(path):
        logging.error("Config not found: %s", path)
        print("  Create hold-queue-config.json or pass a config path.")
        sys.exit(1)
    with open(path) as f:
        return json.load(f)


# ── Main ─────────────────────────────────────────────────────────

def main():
    cfg_path = sys.argv[1] if len(sys.argv) > 1 else DEFAULT_CONFIG_PATH
    cfg = load_config(cfg_path)

    sip_cfg = cfg.get("sip", {})
    audio_cfg = cfg.get("audio", {})

    SERVER = sip_cfg.get("server", "192.168.178.1")
    PORT = int(sip_cfg.get("port", 5060))
    USER = sip_cfg.get("username", os.getenv("SIP_USER", ""))
    PASS = sip_cfg.get("password", "")

    welcome_path = audio_cfg.get("welcome", "")
    hold_path = audio_cfg.get("hold_music", "")
    ann_paths = audio_cfg.get("announcements", [])
    interval = audio_cfg.get("announcement_interval", {"min": 60, "max": 180})
    interval_min = float(interval.get("min", 60))
    interval_max = float(interval.get("max", 180))

    if not hold_path:
        logging.error("No hold_music configured")
        sys.exit(1)

    # ── Load audio ──────────────────────────────────────────────
    logging.info("Loading audio files…")

    welcome_ulaw = load_ulaw(welcome_path) if welcome_path else b""
    if welcome_ulaw:
        logging.info("  Welcome:  %s (%.1fs)", welcome_path,
                     len(welcome_ulaw) / 8000)

    hold_ulaw = load_ulaw(hold_path)
    if not hold_ulaw:
        logging.error("Could not load hold_music: %s", hold_path)
        sys.exit(1)
    logging.info("  Hold:     %s (%.1fs)", hold_path, len(hold_ulaw) / 8000)

    announcements = []
    for p in ann_paths:
        data = load_ulaw(p)
        if data:
            announcements.append(data)
            logging.info("  Ann:      %s (%.1fs)", p, len(data) / 8000)

    logging.info("  Ann intv: %s–%s seconds", interval_min, interval_max)
    print()

    # ── SIP Client ──────────────────────────────────────────────
    from simple_sip import SIPClient

    client = SIPClient()
    client.on("registered",
              lambda h: logging.info("✓ Registered at %s", h))

    streamer = None
    scheduler = None

    def on_invite(call):
        nonlocal streamer, scheduler
        logging.info("📞 Call from: %s", call.caller_number)

        if streamer:
            streamer.stop()
        if scheduler:
            scheduler.stop()

        try:
            remote = extract_remote_addr(call._invite_req.body or "")
        except (ValueError, AttributeError) as e:
            logging.error("SDP error: %s", e)
            call.reject(488, "Not Acceptable Here")
            return

        call.accept()
        logging.info("✓ Accepted, sending audio to %s:%d",
                     remote[0], remote[1])

        streamer = RTPStreamer(remote, pt=0)
        streamer.set_loop(hold_ulaw)
        if welcome_ulaw:
            logging.info("  → Playing welcome")
            streamer.play_once(welcome_ulaw)
        streamer.start()

        if announcements and interval_min > 0:
            scheduler = AnnouncementScheduler(
                streamer, announcements, interval_min, interval_max)
            scheduler.start()

    def on_call_ended(c):
        nonlocal streamer, scheduler
        logging.info("✗ Call ended: %s", c.caller_number)
        if scheduler:
            scheduler.stop()
            scheduler = None
        if streamer:
            streamer.stop()
            streamer = None

    client.on("invite", on_invite)
    client.on("call_ended", on_call_ended)
    client.on("error", lambda m: logging.error("⚠ %s", m))

    print("SIP Hold-Queue")
    print(f"  Config: {cfg_path}")
    print(f"  Server: {SERVER}:{PORT}")
    print(f"  User:   {USER}")
    print(f"  Hold:   {os.path.basename(hold_path)}")
    print(f"  Anns:   {len(announcements)} files ({interval_min}–{interval_max}s)")
    print()

    ok = client.connect(SERVER, PORT, USER, PASS, local_port=0)
    if not ok:
        logging.error("Connection failed")
        sys.exit(1)

    logging.info("Waiting for calls… (Ctrl+C to stop)")
    try:
        client.run()
    except KeyboardInterrupt:
        logging.info("Shutting down…")
    finally:
        if scheduler:
            scheduler.stop()
        if streamer:
            streamer.stop()
        client.stop()


if __name__ == "__main__":
    main()
