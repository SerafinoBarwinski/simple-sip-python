import io
import math
import os
import struct
import subprocess
import shutil
import tempfile
import threading
import time
import wave
from typing import Optional


PATTERNS = {
    "ring": {
        "desc": "ding ding – pause – ding ding",
        "freq": 800,
        "duty": [(0.15, 0.15, 0.15, 2.05)],  # (on, off, on, off) cycle
    },
    "busy": {
        "desc": "schnelle Belegt-Töne",
        "freq": 440,
        "duty": [(0.25, 0.25)],
    },
}


def _generate_wav(pattern: str = "ring", duration: float = 30.0,
                  sample_rate: int = 8000) -> bytes:
    """Generate WAV data for the given pattern."""
    cfg = PATTERNS.get(pattern)
    if not cfg:
        pattern = "ring"
        cfg = PATTERNS["ring"]

    freq = cfg["freq"]
    duty = cfg["duty"][0]
    cycle_samples = sum(duty) * sample_rate

    frames = bytearray()
    total_samples = int(sample_rate * duration)
    i = 0
    while i < total_samples:
        for j, dur in enumerate(duty):
            n = int(dur * sample_rate)
            if j % 2 == 0:
                for k in range(min(n, total_samples - i)):
                    t = (i + k) / sample_rate
                    val = math.sin(2 * math.pi * freq * t)
                    val = int(val * 0.4 * 32767)
                    frames += struct.pack('<h', val)
            else:
                frames += b'\x00\x00' * min(n, total_samples - i)
            i += n
            if i >= total_samples:
                break

    with io.BytesIO() as buf:
        with wave.open(buf, 'wb') as wf:
            wf.setnchannels(1)
            wf.setsampwidth(2)
            wf.setframerate(sample_rate)
            wf.writeframes(bytes(frames))
        return buf.getvalue()


def _write_wav_temp(data: bytes) -> str:
    fd, path = tempfile.mkstemp(suffix='.wav')
    os.close(fd)
    with open(path, 'wb') as f:
        f.write(data)
    return path


class Ringtone:
    """Plays a local ringing sound.

    Patterns:
      "ring" – ding-ding-pause-ding-ding (800 Hz)
      "busy" – schnelle Belegt-Töne (440 Hz)

    Tries sounddevice first, then aplay/paplay/ffplay/play.
    """

    def __init__(self, pattern: str = "ring"):
        self.pattern = pattern if pattern in PATTERNS else "ring"
        self._process: Optional[subprocess.Popen] = None
        self._wav_path: Optional[str] = None
        self._player: Optional[str] = None
        self._detect_player()

    def _detect_player(self):
        if shutil.which('sounddevice'):
            self._player = 'sounddevice'
            return
        for cmd in ['paplay', 'aplay', 'ffplay', 'play']:
            if shutil.which(cmd):
                self._player = cmd
                return
        self._player = None

    @property
    def available(self) -> bool:
        try:
            import sounddevice
            return True
        except ImportError:
            pass
        return self._player is not None

    def play(self, loop: bool = True):
        """Start playback in background (non-blocking)."""
        self.stop()
        wav_data = _generate_wav(pattern=self.pattern, duration=60.0 if loop else 8.0)
        self._wav_path = _write_wav_temp(wav_data)

        if self._player == 'sounddevice':
            self._play_sounddevice(wav_data, loop)
        elif self._player in ('paplay', 'aplay'):
            devnull = subprocess.DEVNULL
            self._process = subprocess.Popen(
                [self._player, self._wav_path],
                stdout=devnull, stderr=devnull,
            )
        elif self._player == 'ffplay':
            devnull = subprocess.DEVNULL
            self._process = subprocess.Popen(
                ['ffplay', '-nodisp', '-autoexit', '-loglevel', 'quiet',
                 self._wav_path],
                stdout=devnull, stderr=devnull,
            )
        elif self._player == 'play':
            devnull = subprocess.DEVNULL
            self._process = subprocess.Popen(
                ['play', '-q', self._wav_path],
                stdout=devnull, stderr=devnull,
            )

    def _play_sounddevice(self, wav_data: bytes, loop: bool):
        import sounddevice as sd
        import numpy as np
        import wave as wavemod

        with io.BytesIO(wav_data) as buf:
            with wavemod.open(buf, 'rb') as wf:
                frames = wf.readframes(wf.getnframes())
                samplerate = wf.getframerate()
                audio = np.frombuffer(frames, dtype=np.int16).astype(np.float32) / 32768.0

        def _run():
            if loop:
                while threading.main_thread().is_alive():
                    sd.play(audio, samplerate, blocking=True)
            else:
                sd.play(audio, samplerate, blocking=True)

        t = threading.Thread(target=_run, daemon=True)
        t.start()
        self._thread = t

    def stop(self):
        if self._process:
            try:
                self._process.terminate()
                self._process.wait(timeout=1)
            except Exception:
                pass
            self._process = None
        if self._wav_path:
            try:
                os.unlink(self._wav_path)
            except OSError:
                pass
            self._wav_path = None
