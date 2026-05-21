#!/usr/bin/env python3
"""
queue-demo.py – Auto-announcement/music on incoming calls

Streams an audio file as RTP (G.711 μ-law/PCMU) to the caller.
Supported: WAV (stdlib), MP3/OGG/FLAC/... (via ffmpeg)
"""
import argparse
import logging
import math
import os
import socket
import struct
import subprocess
import sys
import threading
import time
import wave

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'src'))

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)

from simple_sip import SIPClient, CallState, Ringtone
from simple_sip.sip_sdp import parse_sdp


# ── G.711 μ-law encoder ──────────────────────────────────────────

from simple_sip.sip_media import ulaw2linear

def linear2ulaw(sample: int) -> int:
    """16-bit linear PCM to 8-bit G.711 μ-law (direct formula)."""
    BIAS = 0x84; CLIP = 32635
    sign = 0
    if sample < 0:
        sample = -sample; sign = 0x80
    if sample > CLIP: sample = CLIP
    sample += BIAS
    if sample < 0x100: exp = 0
    elif sample < 0x200: exp = 1
    elif sample < 0x400: exp = 2
    elif sample < 0x800: exp = 3
    elif sample < 0x1000: exp = 4
    elif sample < 0x2000: exp = 5
    elif sample < 0x4000: exp = 6
    else: exp = 7
    mant = (sample >> (exp + 3)) & 0x0F
    return (~(sign | (exp << 4) | mant)) & 0xFF


def pcm16_to_ulaw(pcm: bytes) -> bytes:
    """Convert 16-bit signed little-endian PCM to G.711 μ-law."""
    out = bytearray(len(pcm) // 2)
    for i in range(0, len(pcm), 2):
        sample = struct.unpack_from("<h", pcm, i)[0]
        out[i // 2] = linear2ulaw(sample)
    return bytes(out)


# ── Audio-Loader ─────────────────────────────────────────────────

def load_audio(path: str) -> bytes:
    """Load any audio file, return 8000 Hz mono 16-bit PCM."""
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
        import array
        arr = array.array("b", raw)
        raw = struct.pack("<" + "h" * len(arr), *((v << 8) - 32768 for v in arr))
    elif sampwidth == 2 and sys.byteorder == "big":
        raw = raw  # ensure little-endian
    elif sampwidth != 2:
        raise ValueError(f"Unsupported sample width: {sampwidth}")
    if channels > 1:
        frames = struct.unpack("<" + "h" * (len(raw) // 2))
        mono = [sum(frames[i:i + channels]) // channels
                for i in range(0, len(frames), channels)]
        raw = struct.pack("<" + "h" * len(mono), *mono)
    if rate != 8000:
        return _resample(raw, rate, 8000)
    return raw


def _load_ffmpeg(path: str) -> bytes:
    if not shutil_which("ffmpeg"):
        logging.error("ffmpeg not found – install it or use .wav")
        sys.exit(1)
    cmd = [
        "ffmpeg", "-v", "quiet", "-i", path,
        "-f", "s16le", "-acodec", "pcm_s16le",
        "-ar", "8000", "-ac", "1", "-",
    ]
    try:
        result = subprocess.run(cmd, capture_output=True, timeout=30)
    except subprocess.TimeoutExpired:
        logging.error("ffmpeg timeout decoding %s", path)
        sys.exit(1)
    if result.returncode != 0:
        logging.error("ffmpeg error: %s", result.stderr.decode(errors="replace"))
        sys.exit(1)
    return result.stdout


def _resample(raw: bytes, in_rate: int, out_rate: int) -> bytes:
    if not shutil_which("ffmpeg"):
        logging.warning("ffmpeg missing – cannot convert sample rate %d→%d",
                        in_rate, out_rate)
        return raw
    cmd = [
        "ffmpeg", "-v", "quiet",
        "-f", "s16le", "-ar", str(in_rate), "-ac", "1", "-i", "-",
        "-f", "s16le", "-ar", str(out_rate), "-ac", "1", "-",
    ]
    result = subprocess.run(cmd, input=raw, capture_output=True, timeout=30)
    if result.returncode != 0:
        logging.warning("Resampling failed, using original rate")
        return raw
    return result.stdout


def shutil_which(name: str) -> bool:
    return subprocess.run(["which", name], capture_output=True).returncode == 0


def generate_tone(freq: float = 440, duration: float = 10.0,
                  rate: int = 8000) -> bytes:
    """Generate a simple sine wave as 16-bit PCM."""
    n = int(rate * duration)
    samples = [int(16384 * math.sin(2 * math.pi * freq * (i / rate))) for i in range(n)]
    return struct.pack("<" + "h" * n, *samples)


# ── RTP Streamer ─────────────────────────────────────────────────

class RTPStreamer:
    """Sends μ-law audio as RTP to the caller."""

    def __init__(self, remote_addr: tuple, pt: int = 0):
        self.remote = remote_addr
        self.pt = pt
        self.ssrc = 0xDEADBEEF
        self.seq = 0
        self.timestamp = 0
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.sock.settimeout(1.0)
        self.running = False
        self._thread: threading.Thread = None

    def start(self, ulaw_data: bytes):
        self.running = True
        self._thread = threading.Thread(
            target=self._send_loop, args=(ulaw_data,), daemon=True)
        self._thread.start()

    def stop(self):
        self.running = False
        try:
            self.sock.close()
        except OSError:
            pass

    def _send_loop(self, ulaw_data: bytes):
        chunk = 160  # 20ms at 8000 Hz
        offset = 0
        while self.running and self.sock.fileno() >= 0:
            seg = ulaw_data[offset:offset + chunk]
            if len(seg) < chunk:
                offset = 0
                seg = ulaw_data[offset:offset + chunk]
                if len(seg) < chunk:
                    time.sleep(0.1)
                    continue
            try:
                self.sock.sendto(self._build_packet(seg), self.remote)
            except OSError:
                break
            offset += chunk
            self.seq = (self.seq + 1) & 0xFFFF
            self.timestamp = (self.timestamp + len(seg)) & 0xFFFFFFFF
            time.sleep(chunk / 8000.0)

    def _build_packet(self, payload: bytes) -> bytes:
        return struct.pack(">BBHII",
            0x80,
            self.pt & 0x7F,
            self.seq,
            self.timestamp,
            self.ssrc,
        ) + payload


def extract_remote_addr(invite_body: str) -> tuple:
    """Extract caller (ip, port) from INVITE SDP."""
    sdp = parse_sdp(invite_body)
    if not sdp or not sdp.media:
        raise ValueError("No SDP media in INVITE")
    m = sdp.media[0]
    return (sdp.unicast_address, m.port)


# ── Main ─────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="Queue-Demo – music for callers")
    parser.add_argument("audio", nargs="?", default=None,
                        help="Audio file (wav/mp3/ogg/...)")
    parser.add_argument("--loop", action="store_true", default=True,
                        help="Loop file continuously")
    parser.add_argument("--no-loop", dest="loop", action="store_false")
    parser.add_argument("--tone", type=float, default=None,
                        help="Frequency in Hz instead of file (e.g. 440)")
    args = parser.parse_args()

    if not args.audio and args.tone is None:
        print("Usage: queue-demo.py <audio-file> [--no-loop]")
        print("  or:   queue-demo.py --tone 440")
        sys.exit(1)

    logging.info("Loading audio...")
    if args.tone:
        pcm = generate_tone(args.tone)
        src_desc = f"Sine wave {args.tone} Hz"
    else:
        pcm = load_audio(args.audio)
        src_desc = os.path.basename(args.audio)

    ulaw = pcm16_to_ulaw(pcm)
    duration = len(ulaw) / 8000
    logging.info("Audio loaded: %.1f seconds (%d bytes μ-law, %s)",
                 duration, len(ulaw), src_desc)

    client = SIPClient()
    client.on("registered", lambda h: logging.info(" Registered at %s", h))

    active_streamer = None

    def on_invite(call):
        nonlocal active_streamer
        logging.info(" Incoming call from: %s", call.caller_number)

        if active_streamer:
            active_streamer.stop()

        try:
            remote = extract_remote_addr(call._invite_req.body or "")
        except (ValueError, AttributeError) as e:
            logging.error("SDP error: %s", e)
            call.reject(488, "Not Acceptable Here")
            return

        call.accept()
        logging.info(" Call accepted, streaming audio -> %s:%d",
                      remote[0], remote[1])

        streamer = RTPStreamer(remote, pt=0)
        streamer.start(ulaw)
        active_streamer = streamer

    def on_call_ended(c):
        nonlocal active_streamer
        logging.info(" Call ended: %s", c.caller_number)
        if active_streamer:
            active_streamer.stop()
            active_streamer = None

    client.on("invite", on_invite)
    client.on("call_ended", on_call_ended)
    client.on("error", lambda m: logging.error(" %s", m))

    SERVER = os.environ.get("SIP_SERVER", "192.168.178.1")
    PORT = int(os.environ.get("SIP_PORT", "5060"))
    USER = os.environ.get("SIP_USER", "changeme")
    PASS = os.environ.get("SIP_PASS", "")

    print(f"SIP Queue-Demo")
    print(f"  Server: {SERVER}:{PORT}")
    print(f"  User:   {USER}")
    print(f"  Audio:  {src_desc} ({duration:.0f}s{' – Looping' if args.loop else ''})")
    print()

    ok = client.connect(SERVER, PORT, USER, PASS, local_port=0)
    if not ok:
        logging.error("Connection failed")
        sys.exit(1)

    logging.info("Waiting for calls... (Ctrl+C to quit)")
    try:
        client.run()
    except KeyboardInterrupt:
        logging.info("Shutting down...")
    finally:
        client.stop()


if __name__ == "__main__":
    main()
