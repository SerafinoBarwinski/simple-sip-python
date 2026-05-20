import logging
import socket
import struct
import threading
import time
from collections import deque
from typing import Optional, Tuple

logger = logging.getLogger("sip.media")


def ulaw2linear(ulaw: int) -> int:
    """G.711 μ-law to 16-bit linear PCM."""
    ulaw = ~ulaw & 0xFF
    sign = ulaw & 0x80
    exp = (ulaw >> 4) & 0x07
    mant = ulaw & 0x0F
    sample = ((mant << 3) + 0x84) << exp
    if sign:
        return -sample
    return sample


def alaw2linear(alaw: int) -> int:
    """G.711 A-law to 16-bit linear PCM."""
    alaw ^= 0x55
    sign = alaw & 0x80
    exp = (alaw >> 4) & 0x07
    mant = alaw & 0x0F
    sample = ((mant << 4) + 0x08) << exp
    if sign:
        return -sample
    return sample


CODECS = {
    0:  ("PCMU", 8000, 1, ulaw2linear),
    8:  ("PCMA", 8000, 1, alaw2linear),
    101: ("telephone-event", 8000, 1, None),
}


class RTPPacket:
    __slots__ = ("version", "padding", "extension", "csrc_count",
                 "marker", "payload_type", "sequence", "timestamp",
                 "ssrc", "payload")

    def __init__(self, data: bytes):
        if len(data) < 12:
            raise ValueError("RTP packet too short")
        first = data[0]
        self.version = first >> 6
        self.padding = (first >> 5) & 1
        self.extension = (first >> 4) & 1
        self.csrc_count = first & 0x0F
        second = data[1]
        self.marker = (second >> 7) & 1
        self.payload_type = second & 0x7F
        self.sequence = (data[2] << 8) | data[3]
        self.timestamp = struct.unpack(">I", data[4:8])[0]
        self.ssrc = struct.unpack(">I", data[8:12])[0]
        offset = 12 + self.csrc_count * 4
        if self.extension and len(data) > offset + 4:
            ext_len = struct.unpack(">H", data[offset + 2:offset + 4])[0]
            offset += 4 + ext_len * 4
        self.payload = data[offset:]


class MediaStream:
    """Receives RTP audio and plays it via sounddevice."""

    def __init__(self):
        self.sock: Optional[socket.socket] = None
        self.rtp_port: int = 0
        self.running = False
        self._thread: Optional[threading.Thread] = None
        self._queue: deque = deque(maxlen=8000)
        self._stream = None
        self._payload_type: int = 0
        self._decode_fn = ulaw2linear
        self._remote: Optional[Tuple[str, int]] = None

    @property
    def port(self) -> int:
        return self.rtp_port

    def start(self, local_ip: str, payload_type: int = 0,
              remote: Optional[Tuple[str, int]] = None) -> int:
        """Bind RTP socket, start receive + playback threads.
        Returns the allocated RTP port."""
        self.stop()
        codec = CODECS.get(payload_type)
        if codec:
            self._payload_type = payload_type
            self._decode_fn = codec[3] or ulaw2linear
        else:
            self._payload_type = 0
            self._decode_fn = ulaw2linear

        self._remote = remote
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.sock.bind(("0.0.0.0", 0))
        self.rtp_port = self.sock.getsockname()[1]
        self.running = True
        self._queue.clear()

        self._thread = threading.Thread(target=self._recv_loop, daemon=True)
        self._thread.start()
        self._start_playback()
        logger.info("MediaStream on port %d (%s)",
                    self.rtp_port, CODECS.get(self._payload_type, ("?",))[0])
        return self.rtp_port

    def stop(self):
        self.running = False
        if self._stream:
            try:
                self._stream.stop()
                self._stream.close()
            except Exception:
                pass
            self._stream = None
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=1)
        if self.sock:
            try:
                self.sock.close()
            except OSError:
                pass
            self.sock = None
        self._queue.clear()

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
                samples = [self._decode_fn(b) for b in pkt.payload]
                self._queue.extend(samples)
            except socket.timeout:
                continue
            except OSError:
                if self.running:
                    logger.error("RTP socket error")
                break

    def _start_playback(self):
        try:
            import sounddevice as sd
            import numpy as np
        except ImportError:
            logger.warning("sounddevice not installed – no audio output")
            return

        def callback(outdata, frames, time_info, status):
            samples = []
            for _ in range(frames):
                if self._queue:
                    samples.append(self._queue.popleft())
                else:
                    samples.append(0)
            outdata[:] = np.array(samples, dtype=np.float32).reshape(-1, 1) / 32768.0

        try:
            self._stream = sd.OutputStream(
                samplerate=8000, channels=1,
                callback=callback, blocksize=160,
                dtype='float32',
            )
            self._stream.start()
            logger.debug("Audio playback started")
        except Exception as e:
            logger.error("Audio playback failed: %s", e)
