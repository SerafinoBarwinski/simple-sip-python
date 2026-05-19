import socket
import threading
import time
import logging
from typing import Callable, Dict, Optional, Tuple
from .sip_message import SIPMessage

logger = logging.getLogger("sip.transport")


class SIPTransport:
    """UDP transport for SIP messages with retransmission."""

    def __init__(self, local_host: str = "0.0.0.0", local_port: int = 5060):
        self.local_host = local_host
        self.local_port = local_port
        self.sock: Optional[socket.socket] = None
        self.running = False
        self.recv_thread: Optional[threading.Thread] = None
        self._callbacks: Dict[str, Callable] = {}
        self._server_addr: Optional[Tuple[str, int]] = None

    def on_receive(self, callback: Callable[[SIPMessage, Tuple[str, int]], None]):
        self._callbacks["receive"] = callback

    def open(self) -> bool:
        try:
            self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            self.sock.bind((self.local_host, self.local_port))
            self.sock.settimeout(1.0)
            self.running = True
            self.recv_thread = threading.Thread(target=self._recv_loop, daemon=True)
            self.recv_thread.start()
            logger.info(f"Transport listening on {self.local_host}:{self.local_port}")
            return True
        except OSError as e:
            logger.error(f"Failed to bind: {e}")
            return False

    def close(self):
        self.running = False
        if self.sock:
            try:
                self.sock.close()
            except OSError:
                pass
        if self.recv_thread and self.recv_thread.is_alive():
            self.recv_thread.join(timeout=2)

    def send_to(self, data: bytes, addr: Tuple[str, int]) -> bool:
        if not self.sock:
            logger.error("Socket not open")
            return False
        try:
            self.sock.sendto(data, addr)
            return True
        except OSError as e:
            logger.error(f"Send failed: {e}")
            return False

    def send_with_retry(self, data: bytes, addr: Tuple[str, int],
                        retries: int = 3, timeout: float = 1.0) -> Optional[SIPMessage]:
        """Send with exponential backoff retransmission."""
        for attempt in range(retries):
            if not self.send_to(data, addr):
                return None
            if attempt == retries - 1:
                return None
            time.sleep(timeout * (2 ** attempt))
        return None

    def send_and_wait(self, data: bytes, addr: Tuple[str, int],
                      timeout: float = 3.0) -> Optional[SIPMessage]:
        """Send and wait for a response.  Matches responses from the same
        IP (port-agnostic) to handle servers that respond from a
        different source port."""
        target_ip = addr[0]
        result = []
        event = threading.Event()

        def handler(msg, src):
            # Accept from same IP regardless of port
            if src[0] == target_ip:
                result.append(msg)
                event.set()

        old_cb = self._callbacks.get("receive_tmp")
        self._callbacks["receive_tmp"] = handler
        self.send_to(data, addr)

        waited = 0
        while waited < timeout:
            event.wait(timeout=0.5)
            if event.is_set():
                break
            self.send_to(data, addr)
            waited += 0.5

        self._callbacks.pop("receive_tmp", None)
        if old_cb:
            self._callbacks["receive_tmp"] = old_cb

        return result[0] if result else None

    def send_message(self, msg: SIPMessage, addr: Tuple[str, int]) -> bool:
        return self.send_to(msg.serialize(), addr)

    def _recv_loop(self):
        while self.running:
            try:
                data, addr = self.sock.recvfrom(65535)
                if not data:
                    continue
                # RTP/RTCP packets beginnen mit 0x80–0xBF (Version 2)
                # oder 0x00–0x3F (STUN). Leise verwerfen.
                if data[0] & 0xC0 == 0x80:
                    continue
                try:
                    parsed = SIPMessage.parse(data)
                    cb = self._callbacks.get("receive")
                    if cb:
                        cb(parsed, addr)
                    tmp_cb = self._callbacks.get("receive_tmp")
                    if tmp_cb:
                        tmp_cb(parsed, addr)
                except ValueError as e:
                    logger.debug("Parse error from %s: %s", addr, e)
            except socket.timeout:
                continue
            except OSError:
                if self.running:
                    logger.error("Socket error in recv loop")
                break
