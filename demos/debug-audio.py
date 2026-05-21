#!/usr/bin/env python3
"""
Debug: Saves raw audio stream from caller as WAV + Raw dump.

Use it just like demo.py, but it writes audio data to files
instead of playing them. Send me the files for analysis.

Usage:
  python debug_audio.py
  # Make call, answer, wait, then Ctrl+C
  # Creates: audio_dump_*.raw + audio_dump_*.wav
"""
import logging
import os
import socket
import struct
import sys
import threading
import time

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'src'))

logging.basicConfig(
    level=logging.DEBUG,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)

from simple_sip import SIPClient, Ringtone
from simple_sip.sip_media import RTPPacket, ulaw2linear, alaw2linear


class DebugMedia:
    """Captures RTP audio and writes it to files."""

    def __init__(self):
        self.sock = None
        self.rtp_port = 0
        self.running = False
        self._thread = None
        self._raw_payloads = bytearray()   # rohe RTP-Payloads (μ-law / A-law)
        self._decoded_pcm = bytearray()    # decoded 16-bit PCM
        self._payload_type = 0
        self._decode_fn = ulaw2linear
        self._packet_count = 0
        self._start_time = None

    def start(self, local_ip: str, payload_type: int = 0) -> int:
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.sock.bind(("0.0.0.0", 0))
        self.rtp_port = self.sock.getsockname()[1]
        self._payload_type = payload_type
        self._decode_fn = ulaw2linear if payload_type == 0 else alaw2linear
        self.running = True
        self._start_time = time.time()
        self._thread = threading.Thread(target=self._recv_loop, daemon=True)
        self._thread.start()
        print(f"   DebugMedia auf Port {self.rtp_port} (PT={payload_type})")
        return self.rtp_port

    def stop(self):
        self.running = False
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=2)
        if self.sock:
            try:
                self.sock.close()
            except OSError:
                pass
            self.sock = None
        self._save_files()

    def _recv_loop(self):
        while self.running:
            try:
                data, addr = self.sock.recvfrom(2048)
                if not data or len(data) < 12:
                    continue
                if data[0] & 0xC0 != 0x80:
                    continue
                try:
                    pkt = RTPPacket(data)
                except ValueError:
                    continue
                if pkt.payload_type != self._payload_type:
                    continue
                self._packet_count += 1

                # Rohdaten sammeln
                self._raw_payloads.extend(pkt.payload)

                # Dekodieren
                for b in pkt.payload:
                    sample = self._decode_fn(b)
                    self._decoded_pcm += struct.pack('<h', sample)

            except socket.timeout:
                continue
            except OSError:
                break

    def _save_files(self):
        if self._packet_count == 0:
            print("  ! Keine RTP-Pakete empfangen – nichts zu speichern")
            return

        ts = int(self._start_time or time.time())
        duration = len(self._decoded_pcm) / 16000  # 2 bytes per sample, 8000 Hz
        codec_name = "PCMU" if self._payload_type == 0 else "PCMA"

        # Raw-Dump (rohe Codec-Bytes)
        raw_path = f"audio_dump_{ts}.raw"
        with open(raw_path, "wb") as f:
            f.write(bytes(self._raw_payloads))
        print(f"   Raw-Dump: {raw_path}  ({len(self._raw_payloads)} Bytes, {codec_name})")

        # WAV (dekodiertes 16-bit PCM)
        wav_path = f"audio_dump_{ts}.wav"
        import wave
        with wave.open(wav_path, "wb") as wf:
            wf.setnchannels(1)
            wf.setsampwidth(2)
            wf.setframerate(8000)
            wf.writeframes(bytes(self._decoded_pcm))
        print(f"   WAV-Datei: {wav_path}  ({len(self._decoded_pcm)} Bytes, ~{duration:.1f}s)")
        print(f"   Pakete: {self._packet_count}")


# === Client-Code ===
client = SIPClient()
ringtone = Ringtone(pattern="ring")
debug = DebugMedia()

client.on("registered", lambda host: print(f"\n Registered at {host}"))

def on_invite(call):
    print(f"\n Incoming call from: {call.caller_number}")
    ringtone.play()

    def interactive():
        print("  [Enter] accept (Audio-Debug)  |  [r] reject")
        choice = input().strip().lower()
        ringtone.stop()
        if choice == "r":
            call.reject()
            print("   → Rejected")
        else:
            # Replace media with DebugMedia
            call._media = debug
            call.accept()
            print(f"   → Accepted – recording audio... Ctrl+C to stop")

    threading.Thread(target=interactive, daemon=True).start()

client.on("invite", on_invite)
client.on("call_accepted", lambda c: print(f" Call active: {c.caller_number}"))
client.on("call_rejected", lambda c, code, reason: ringtone.stop())
client.on("call_ended", lambda c: print(f" Call ended: {c.caller_number}"))
client.on("error", lambda msg: print(f"\n Error: {msg}"))

# Connect
SERVER = "192.168.178.1"
PORT = 5060
USER = os.getenv("SIP_USER", "changeme")
PASS = os.getenv("SIP_PASS", "")

print(f"SIP Client – Audio Debug")
print(f"  Server: {SERVER}:{PORT}")
print(f"  User:   {USER}")

ok = client.connect(
    server=SERVER, port=PORT,
    username=USER, password=PASS,
    local_port=0,
)

if ok:
    print("\nConnected. Make a call now!")
    print("  Enter → accept & record audio")
    print("  Ctrl+C → stop & save files\n")
    try:
        client.run()
    except KeyboardInterrupt:
        print("\n\nSaving audio...")
    finally:
        client.stop()
else:
    print("\n Connection failed")
