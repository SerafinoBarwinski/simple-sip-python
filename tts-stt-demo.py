#!/usr/bin/env python3
"""
tts-stt-demo.py – Answer SIP calls and chat via voice (local STT/TTS).

Transcribes caller speech to text (Vosk), lets you reply by typing,
and speaks your response back via RTP (pyttsx3).

Requirements:
  pip install vosk pyttsx3 simple-sip-client
  # Linux TTS backend:
  apt install espeak-ng          # Debian/Ubuntu
  pacman -S espeak-ng            # Arch
  brew install espeak-ng         # macOS

Usage:
  python tts-stt-demo.py

Then call in from a SIP phone and start chatting.
"""
import io
import logging
import os
import struct
import sys
import threading
import time
import wave
from collections import deque

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), 'src'))

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)

logger = logging.getLogger("voicechat")

HERE = os.path.dirname(os.path.abspath(__file__))
VOSK_MODEL_DIR = os.path.join(HERE, "vosk-model")
VOSK_MODEL_URL = "https://alphacephei.com/vosk/models/vosk-model-small-de-0.15.zip"

def ensure_vosk_model():
    if os.path.isdir(os.path.join(VOSK_MODEL_DIR, "am")):
        return
    if not os.path.isdir(VOSK_MODEL_DIR):
        os.makedirs(VOSK_MODEL_DIR, exist_ok=True)
    print("  ⬇ Downloading Vosk model (~40 MB)...")
    import urllib.request
    import zipfile
    zip_path = os.path.join(VOSK_MODEL_DIR, "model.zip")
    urllib.request.urlretrieve(VOSK_MODEL_URL, zip_path)
    with zipfile.ZipFile(zip_path, "r") as zf:
        zf.extractall(VOSK_MODEL_DIR)
    os.unlink(zip_path)
    extracted = [d for d in os.listdir(VOSK_MODEL_DIR)
                 if os.path.isdir(os.path.join(VOSK_MODEL_DIR, d))
                 and d != "__pycache__"]
    if extracted:
        inner = os.path.join(VOSK_MODEL_DIR, extracted[0])
        for item in os.listdir(inner):
            os.rename(os.path.join(inner, item), os.path.join(VOSK_MODEL_DIR, item))
        os.rmdir(inner)

try:
    from simple_sip import SIPClient
    from simple_sip.sip_media import RTPPacket, ulaw2linear
except ImportError:
    print("Install simple-sip-client first: pip install simple-sip-client")
    sys.exit(1)

try:
    import vosk
except ImportError:
    print("Install Vosk: pip install vosk")
    sys.exit(1)

pyttsx3 = None
if os.name == "posix":
    import subprocess
    if subprocess.run(["which", "espeak-ng"], capture_output=True).returncode == 0:
        TTS_ENGINE = "espeak-ng"
    else:
        TTS_ENGINE = None
else:
    TTS_ENGINE = None

try:
    import pyttsx3
except ImportError:
    pass

if TTS_ENGINE is None:
    try:
        import pyttsx3
        engine = pyttsx3.init()
        engine.stop()
        TTS_ENGINE = "pyttsx3"
    except Exception:
        TTS_ENGINE = None

if TTS_ENGINE is None:
    print("No TTS engine found.")
    print("  pip install pyttsx3")
    print("  # and on Linux:")
    print("  apt install espeak-ng")
    sys.exit(1)


# ── Vosk STT ─────────────────────────────────────────────────────

class SpeechTranscriber:
    def __init__(self, model_path: str, sample_rate: int = 8000):
        logger.info("Loading Vosk model from %s ...", model_path)
        self.model = vosk.Model(model_path)
        self.sample_rate = sample_rate
        self.rec = vosk.KaldiRecognizer(self.model, sample_rate)
        self.buffer = bytearray()
        self.running = False
        self.transcriptions = deque(maxlen=100)
        self._lock = threading.Lock()

    def feed(self, pcm_data: bytes):
        with self._lock:
            self.buffer.extend(pcm_data)
            if len(self.buffer) >= 3200:
                self.rec.AcceptWaveform(bytes(self.buffer))
                self.buffer.clear()
                result = self.rec.Result()
                import json
                data = json.loads(result)
                if data.get("text"):
                    self.transcriptions.append(data["text"])
                    print(f"\n  🗣 Caller: {data['text']}")

    def get_latest(self) -> str:
        with self._lock:
            parts = []
            while self.transcriptions:
                parts.append(self.transcriptions.popleft())
            return " ".join(parts)


# ── TTS ──────────────────────────────────────────────────────────

def generate_tts_audio(text: str, rate: int = 8000) -> bytes:
    pcm = bytearray()
    if TTS_ENGINE == "pyttsx3":
        import tempfile
        engine = pyttsx3.init()
        engine.setProperty("rate", 150)
        with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as f:
            tmp_path = f.name
        engine.save_to_file(text, tmp_path)
        engine.runAndWait()
        with wave.open(tmp_path, "rb") as wf:
            raw = wf.readframes(wf.getnframes())
            sr = wf.getframerate()
        os.unlink(tmp_path)
        pcm = bytearray(raw)
        if sr != rate:
            pcm = bytearray(_resample_pcm(bytes(pcm), sr, rate))
    elif TTS_ENGINE == "espeak-ng":
        import subprocess
        r = subprocess.run(
            ["espeak-ng", "--stdout", "-s", "150", text],
            capture_output=True, timeout=30,
        )
        if r.returncode == 0 and r.stdout:
            with wave.open(io.BytesIO(r.stdout), "rb") as wf:
                raw = wf.readframes(wf.getnframes())
                sr = wf.getframerate()
            pcm = bytearray(raw)
            if sr != rate:
                pcm = bytearray(_resample_pcm(bytes(pcm), sr, rate))
    return bytes(pcm)


def _resample_pcm(raw: bytes, in_rate: int, out_rate: int) -> bytes:
    import subprocess
    r = subprocess.run(
        ["ffmpeg", "-v", "quiet",
         "-f", "s16le", "-ar", str(in_rate), "-ac", "1", "-i", "-",
         "-f", "s16le", "-ar", str(out_rate), "-ac", "1", "-"],
        input=raw, capture_output=True, timeout=30,
    )
    return r.stdout if r.returncode == 0 else raw


# ── RTP Streamer for TTS playback ────────────────────────────────

class RTPTalker:
    def __init__(self, remote_addr: tuple, transcriber: SpeechTranscriber):
        self.remote = remote_addr
        self.transcriber = transcriber
        import socket as _socket
        self.sock = _socket.socket(_socket.AF_INET, _socket.SOCK_DGRAM)
        self.ssrc = 0xCAFEBABE
        self.seq = 0
        self.ts = 0

    def ulaw_encode(self, pcm: bytes) -> bytes:
        out = bytearray(len(pcm) // 2)
        for i in range(0, len(pcm), 2):
            s = struct.unpack_from("<h", pcm, i)[0]
            s = max(-32768, min(32767, s))
            bias = 0x84
            sign = 0
            if s < 0:
                s = -s
                sign = 0x80
            if s > 32635:
                s = 32635
            s += bias
            if s < 0x100:
                e = 0
            elif s < 0x200:
                e = 1
            elif s < 0x400:
                e = 2
            elif s < 0x800:
                e = 3
            elif s < 0x1000:
                e = 4
            elif s < 0x2000:
                e = 5
            elif s < 0x4000:
                e = 6
            else:
                e = 7
            out[i // 2] = (~(sign | (e << 4) | ((s >> (e + 3)) & 0x0F))) & 0xFF
        return bytes(out)

    def play(self, text: str):
        pcm = generate_tts_audio(text)
        if not pcm:
            return
        ulaw = self.ulaw_encode(pcm)
        chunk = 160
        for offset in range(0, len(ulaw), chunk):
            seg = ulaw[offset:offset + chunk]
            pkt = struct.pack(">BBHII", 0x80, 0, self.seq, self.ts, self.ssrc) + seg
            try:
                self.sock.sendto(pkt, self.remote)
            except OSError:
                break
            self.seq = (self.seq + 1) & 0xFFFF
            self.ts = (self.ts + len(seg)) & 0xFFFFFFFF
            time.sleep(chunk / 8000.0)

    def stop(self):
        try:
            self.sock.close()
        except OSError:
            pass


# ── Main Application ─────────────────────────────────────────────

def main():
    SERVER = os.environ.get("SIP_SERVER", "192.168.178.1")
    PORT = int(os.environ.get("SIP_PORT", "5060"))
    USER = os.environ.get("SIP_USER", "changeme")
    PASS = os.environ.get("SIP_PASS", "")

    print("SIP Voice Chat  (tts-stt-demo)")
    print(f"  Server: {SERVER}:{PORT}")
    print(f"  User:   {USER}")
    print("  Reqs:  pip install vosk pyttsx3 simple-sip-client")
    print("         apt install espeak-ng")
    print()
    print("  ⬇ Loading Vosk model (first run downloads ~40 MB)...")
    ensure_vosk_model()
    transcriber = SpeechTranscriber(VOSK_MODEL_DIR)
    print("  ✓ Vosk ready")
    print(f"  ✓ TTS engine: {TTS_ENGINE}")
    print()

    client = SIPClient()
    talker = None

    client.on("registered", lambda h: print(f"✓ Registered at {h}"))

    def on_invite(call):
        nonlocal talker
        print(f"\n📞 Incoming call from: {call.caller_number}")
        call.accept()
        print("  ✓ Call accepted – starting voice chat")
        print("  ─────────────────────────────────────")
        print("  🗣 Caller speech → transcribed below")
        print("  ⌨ Type your reply + Enter → spoken back")
        print("  📞 h + Enter → hang up")
        print("  ─────────────────────────────────────\n")

        from simple_sip.sip_sdp import parse_sdp
        sdp = parse_sdp(call._invite_req.body or "")
        remote = (sdp.unicast_address, sdp.media[0].port) if sdp and sdp.media else call._peer_addr
        talker = RTPTalker(remote, transcriber)

        def audio_loop():
            media = call._media
            if not media:
                return
            while media and media.running:
                try:
                    data, addr = media.sock.recvfrom(2048)
                    if not data or len(data) < 12:
                        continue
                    if data[0] & 0xC0 != 0x80:
                        continue
                    pkt = RTPPacket(data)
                    samples = bytearray()
                    for b in pkt.payload:
                        sample = ulaw2linear(b)
                        samples += struct.pack("<h", sample)
                    transcriber.feed(bytes(samples))
                except (OSError, socket.timeout):
                    break

        import socket
        t = threading.Thread(target=audio_loop, daemon=True)
        t.start()

        def chat_loop():
            nonlocal talker
            try:
                while True:
                    line = sys.stdin.readline()
                    if not line:
                        break
                    line = line.strip()
                    if line.lower() == "h":
                        call.hangup()
                        break
                    if line:
                        print(f"  💬 You: {line}")
                        talker.play(line)
            except (EOFError, KeyboardInterrupt):
                call.hangup()

        threading.Thread(target=chat_loop, daemon=True).start()

    client.on("invite", on_invite)
    client.on("call_ended", lambda c: print(f"✗ Call ended: {c.caller_number}"))
    client.on("error", lambda m: print(f"⚠ Error: {m}"))

    ok = client.connect(SERVER, PORT, USER, PASS, local_port=0)
    if not ok:
        logger.error("Connection failed")
        sys.exit(1)

    try:
        client.run()
    except KeyboardInterrupt:
        logger.info("Shutting down...")
    finally:
        client.stop()


if __name__ == "__main__":
    main()
