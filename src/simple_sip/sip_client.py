import logging
import socket
import threading
import time
from enum import Enum, auto
from dataclasses import dataclass, field
from typing import Callable, Dict, List, Optional, Tuple, Union

from .sip_message import (
    SIPMessage, SIPRequest, SIPResponse, SIPURI, parse_sip_uri,
    parse_from_header,
    make_register, make_response, make_invite, make_tag, make_call_id,
    make_via,
)
from .sip_auth import DigestAuth
from .sip_transport import SIPTransport
from .sip_sdp import make_sdp_answer, make_sdp_offer, parse_sdp
from .sip_media import MediaStream

logger = logging.getLogger("sip.client")


class CallState(Enum):
    IDLE = auto()
    RINGING = auto()
    ACTIVE = auto()
    TERMINATED = auto()


@dataclass
class Call:
    call_id: str
    caller_uri: SIPURI
    callee_uri: SIPURI
    state: CallState
    cseq: int
    from_tag: str
    to_tag: str
    _client: "SIPClient" = field(repr=False)
    _invite_req: Optional[SIPRequest] = field(default=None, repr=False)
    _peer_addr: Tuple[str, int] = field(default=("", 0), repr=False)
    _media: Optional[MediaStream] = field(default=None, repr=False)
    _auth_attempted: bool = field(default=False, repr=False)

    @property
    def caller_number(self) -> str:
        return self.caller_uri.user or self.caller_uri.host

    @property
    def callee_number(self) -> str:
        return self.callee_uri.user or self.callee_uri.host

    def accept(self, sdp: str = ""):
        if self.state != CallState.RINGING:
            return

        # Media-Stream + SDP generieren (auch wenn _media schon gesetzt)
        if not self._media:
            self._media = MediaStream()

        cu = self._client.contact_uri
        local_ip = cu.host if cu else "0.0.0.0"

        pt = 0
        if self._invite_req and self._invite_req.body:
            from .sip_sdp import parse_sdp
            offer = parse_sdp(self._invite_req.body)
            if offer and offer.media:
                for fmt in offer.media[0].formats:
                    try:
                        f = int(fmt)
                        if f in (0, 8):
                            pt = f
                            break
                    except ValueError:
                        pass

        rtp_port = self._media.start(local_ip, payload_type=pt)
        logger.info("Media: RTP auf %s:%d (PT=%d)", local_ip, rtp_port, pt)
        offer_body = self._invite_req.body if self._invite_req else ""
        sdp = make_sdp_answer(offer_body, local_ip, rtp_port, payload_type=pt)

        self._client._accept_call(self, sdp)

    def reject(self, code: int = 486, reason: str = "Busy Here"):
        if self.state != CallState.RINGING:
            return
        if self._media:
            self._media.stop()
            self._media = None
        self._client._reject_call(self, code, reason)

    def hangup(self):
        if self._media:
            self._media.stop()
            self._media = None
        self._client._hangup_call(self)


class SIPClient:
    def __init__(self):
        self.transport = SIPTransport()
        self.auth: Optional[DigestAuth] = None
        self.server_addr: Tuple[str, int] = ("", 0)
        self.server_uri: Optional[SIPURI] = None
        self.contact_uri: Optional[SIPURI] = None
        self.call_id: str = make_call_id()
        self.tag: str = make_tag()
        self.cseq: int = 0
        self.registered: bool = False
        self._running = False
        self._callbacks: Dict[str, List[Callable]] = {}
        self.calls: Dict[str, Call] = {}

        self.transport.on_receive(self._on_message)

    # --- Event System ---

    def on(self, event: str, callback: Callable):
        if event not in self._callbacks:
            self._callbacks[event] = []
        self._callbacks[event].append(callback)

    def _emit(self, event: str, *args, **kwargs):
        for cb in self._callbacks.get(event, []):
            try:
                cb(*args, **kwargs)
            except Exception as e:
                logger.error(f"Error in {event} handler: {e}")

    # --- Connection ---

    def connect(self, server: str, port: int = 5060,
                username: str = "", password: str = "",
                display_name: str = "",
                local_port: int = 5060) -> bool:
        # Resolve hostname to IP (so send_and_wait comparisons work)
        server_ip = self._resolve_host(server)
        if not server_ip:
            logger.error(f"Cannot resolve {server}")
            return False

        self.server_addr = (server_ip, port)
        self.server_uri = SIPURI(host=server, port=port)
        if username:
            self.server_uri.user = username

        local_ip = self._get_local_ip(server_ip)
        self.transport.local_port = local_port

        if username and password:
            self.auth = DigestAuth(username, password)

        if not self.transport.open():
            return False

        # Echten Port auslesen (wichtig bei local_port=0)
        actual_port = self.transport.sock.getsockname()[1]
        self.contact_uri = SIPURI(
            user=username or f"user{actual_port}",
            host=local_ip,
            port=actual_port,
        )

        self._running = True
        self._emit("connecting", server, port)
        return self._register()

    def stop(self):
        self._running = False
        if self.registered:
            self._unregister()
        self.transport.close()

    def run(self):
        """Keep client alive (blocking)."""
        while self._running:
            time.sleep(1)

    # --- Registration ---

    def _register(self) -> bool:
        self.cseq += 1
        req = make_register(self.server_uri, self.contact_uri,
                            self.call_id, self.cseq, self.tag)
        logger.info(f"REGISTER -> {self.server_addr}")
        logger.debug("REGISTER request:\n%s",
                     req.serialize().decode(errors="replace"))
        resp = self.transport.send_and_wait(req.serialize(), self.server_addr,
                                            timeout=3.0)

        if resp is None:
            logger.error("No response to REGISTER (timeout / no answer)")
            self._emit("error", "No response to REGISTER")
            return False

        if isinstance(resp, SIPResponse):
            logger.info(f"REGISTER response: {resp.status_code} {resp.reason}")
            self._emit("register_response", resp.status_code, resp.reason)

            if resp.status_code == 401 and self.auth:
                www_auth = resp.get_header("WWW-Authenticate")
                if www_auth and self.auth.parse_challenge(www_auth):
                    self.cseq += 1
                    req2 = make_register(self.server_uri, self.contact_uri,
                                         self.call_id, self.cseq, self.tag)
                    auth_value = self.auth.build_authorization(
                        "REGISTER", str(self.server_uri.without_user()))
                    req2.set_header("Authorization", auth_value)
                    logger.info("REGISTER (auth) -> %s", self.server_addr)
                    logger.debug("REGISTER (auth) request:\n%s",
                                 req2.serialize().decode(errors="replace"))
                    resp2 = self.transport.send_and_wait(
                        req2.serialize(), self.server_addr, timeout=3.0)

                    if resp2 and isinstance(resp2, SIPResponse):
                        logger.info("REGISTER (auth) response: %s %s",
                                    resp2.status_code, resp2.reason)
                        self._emit("register_response", resp2.status_code, resp2.reason)
                        if resp2.status_code == 200:
                            self.registered = True
                            self._emit("registered", self.server_uri.host)
                            return True
                    else:
                        logger.error("No response to authenticated REGISTER")
                        self._emit("error", "No response to authenticated REGISTER")
                        return False
                else:
                    logger.error("Could not parse WWW-Authenticate header: %s",
                                 www_auth)

            elif resp.status_code == 200:
                self.registered = True
                self._emit("registered", self.server_uri.host)
                return True

        logger.error(f"Registration failed: {resp.status_code if isinstance(resp, SIPResponse) else 'unknown'} {resp.reason if isinstance(resp, SIPResponse) else ''}")
        self._emit("error",
                   f"Registration failed: {resp.status_code if isinstance(resp, SIPResponse) else 'unknown'}")
        return False

    def _unregister(self):
        self.cseq += 1
        req = make_register(self.server_uri, self.contact_uri,
                            self.call_id, self.cseq, self.tag, expires=0)
        self.transport.send_message(req, self.server_addr)
        self.registered = False

    # --- Call Handling ---

    def make_call(self, target: str) -> Optional[Call]:
        """Initiate an outbound call."""
        if not self.registered:
            logger.error("Not registered")
            return None

        callee = parse_sip_uri(target)
        if not callee.host:
            callee.host = self.server_uri.host
            callee.port = self.server_uri.port

        call_id = make_call_id()
        cseq = self.cseq + 1
        self.cseq = cseq

        cu = self.contact_uri
        local_ip = cu.host if cu else "0.0.0.0"

        media = MediaStream()
        rtp_port = media.start(local_ip, payload_type=0)
        sdp_offer = make_sdp_offer(local_ip, rtp_port)

        call = Call(
            call_id=call_id,
            caller_uri=self.contact_uri,
            callee_uri=callee,
            state=CallState.RINGING,
            cseq=cseq,
            from_tag=self.tag,
            to_tag="",
            _client=self,
            _peer_addr=self.server_addr,
            _media=media,
        )
        self.calls[call_id] = call

        req = make_invite(callee, self.contact_uri, call_id, cseq,
                          self.tag, sdp=sdp_offer)
        call._invite_req = req
        logger.info("INVITE -> %s (RTP auf %s:%d)",
                    self.server_addr, local_ip, rtp_port)
        self.transport.send_message(req, self.server_addr)
        self._emit("call_outgoing", call)
        return call

    def _accept_call(self, call: Call, sdp: str = ""):
        """Accept a ringing call (send 200 OK)."""
        if call.state != CallState.RINGING:
            return
        call.state = CallState.ACTIVE

        extra = {"Contact": f"<{self.contact_uri}>",
                 "To": f"<{call.callee_uri}>;tag={call.to_tag}"}
        if sdp:
            extra["Content-Type"] = "application/sdp"

        req = call._invite_req or SIPRequest("INVITE", str(call.callee_uri))
        resp = make_response(req, 200, "OK", extra, body=sdp)
        resp.set_header("Content-Length", str(len(sdp)))

        peer = call._peer_addr if call._peer_addr != ("", 0) else self.server_addr
        if sdp:
            logger.info("Sending 200 OK with SDP (RTP port in answer)")
            for line in sdp.strip().split("\r\n"):
                logger.info("  SDP: %s", line)
        self.transport.send_message(resp, peer)
        self._emit("call_accepted", call)

    def _reject_call(self, call: Call, code: int, reason: str):
        """Reject a ringing call."""
        call.state = CallState.TERMINATED
        req = call._invite_req or SIPRequest("INVITE", str(call.callee_uri))
        resp = make_response(req, code, reason,
                             {"To": f"<{call.callee_uri}>;tag={call.to_tag}"})
        peer = call._peer_addr if call._peer_addr != ("", 0) else self.server_addr
        self.transport.send_message(resp, peer)
        self._emit("call_rejected", call, code, reason)

    def _hangup_call(self, call: Call):
        """End an active call with BYE."""
        if call.state != CallState.ACTIVE:
            logger.warning("Cannot hangup: call is not active (state: %s)", call.state)
            return
        
        call.state = CallState.TERMINATED
        self.cseq += 1
        bye = SIPRequest("BYE", str(call.callee_uri))
        bye.set_header("Via", f"SIP/2.0/UDP {self.contact_uri.host}:{self.contact_uri.port}")
        bye.set_header("Max-Forwards", "70")
        bye.set_header("To", f"<{call.callee_uri}>;tag={call.to_tag}")
        bye.set_header("From", f"<{call.caller_uri}>;tag={call.from_tag}")
        bye.set_header("Call-ID", call.call_id)
        bye.set_header("CSeq", f"{self.cseq} BYE")
        bye.set_header("Content-Length", "0")
        bye.set_header("Contact", f"<{self.contact_uri}>")
        
        logger.info("BYE -> %s (CSeq: %d)", self.server_addr, self.cseq)
        self.transport.send_message(bye, self.server_addr)
        self._emit("call_ended", call)
        
        # Remove call from active calls dict
        if call.call_id in self.calls:
            del self.calls[call.call_id]

    # --- Message Handling ---

    def _on_message(self, msg: SIPMessage, addr: Tuple[str, int]):
        if isinstance(msg, SIPRequest):
            self._handle_request(msg, addr)
        elif isinstance(msg, SIPResponse):
            self._handle_response(msg, addr)

    def _handle_request(self, req: SIPRequest, addr: Tuple[str, int]):
        method = req.method
        logger.debug(f"<<< {method} from {addr}")

        if method == "INVITE":
            self._handle_invite(req, addr)
        elif method == "ACK":
            self._handle_ack(req, addr)
        elif method == "BYE":
            self._handle_bye(req, addr)
        elif method == "CANCEL":
            self._handle_cancel(req, addr)
        elif method == "OPTIONS":
            self._handle_options(req, addr)
        elif method == "NOTIFY":
            self._emit("notify", req)
        elif method == "MESSAGE":
            self._emit("message", req)
        else:
            self._emit("unknown_request", req)

    def _handle_response(self, resp: SIPResponse, addr: Tuple[str, int]):
        logger.debug(f"<<< {resp.status_code} {resp.reason}")

        call_id = resp.get_header("Call-ID") or ""
        call = self.calls.get(call_id)
        cseq_line = resp.get_header("CSeq") or ""
        is_invite_response = "INVITE" in cseq_line.upper()

        if (resp.status_code == 401 and call and is_invite_response
                and call.state == CallState.RINGING
                and not call._auth_attempted
                and self.auth):
            self._retry_invite_with_auth(call, resp)

        elif resp.status_code == 200 and call and call.state == CallState.RINGING and is_invite_response:
            to_hdr = resp.get_header("To") or ""
            to_tag, _ = parse_from_header(to_hdr)
            call.to_tag = to_tag or ""

            call.state = CallState.ACTIVE

            ack = SIPRequest("ACK", str(call.callee_uri))
            via_val = call._invite_req.get_header("Via") if call._invite_req else None
            if via_val:
                ack.set_header("Via", via_val)
            else:
                ack.set_header("Via",
                    f"SIP/2.0/UDP {self.contact_uri.host}:{self.contact_uri.port}")
            ack.set_header("Max-Forwards", "70")
            ack.set_header("To", f"<{call.callee_uri}>;tag={call.to_tag}")
            ack.set_header("From", f"<{call.caller_uri}>;tag={call.from_tag}")
            ack.set_header("Call-ID", call.call_id)
            ack.set_header("CSeq", f"{call.cseq} ACK")
            ack.set_header("Content-Length", "0")
            ack.set_header("Contact", f"<{self.contact_uri}>")
            self.transport.send_message(ack, self.server_addr)
            logger.info("Call active (ACK sent)")

            self._emit("call_active", call)

        elif resp.status_code == 180 and call:
            if call.state == CallState.RINGING:
                to_hdr = resp.get_header("To") or ""
                to_tag, _ = parse_from_header(to_hdr)
                call.to_tag = to_tag or ""
            self._emit("ringing", call)

        self._emit("response", resp.status_code, resp.reason)

    def _retry_invite_with_auth(self, call: Call, resp: SIPResponse):
        www_auth = resp.get_header("WWW-Authenticate")
        if not www_auth or not self.auth.parse_challenge(www_auth):
            logger.error("Could not parse WWW-Authenticate for INVITE")
            return
        call._auth_attempted = True

        orig = call._invite_req
        if not orig:
            logger.error("No original INVITE to retry")
            return

        # ACK for the 401 (non-2xx final response) – same branch, same CSeq
        ack = SIPRequest("ACK", str(call.callee_uri))
        via_val = orig.get_header("Via")
        if via_val:
            ack.set_header("Via", via_val)
        else:
            ack.set_header("Via", make_via(self.contact_uri.host,
                                           self.contact_uri.port or 5060))
        ack.set_header("Max-Forwards", "70")
        ack.set_header("To", resp.get_header("To") or str(call.callee_uri))
        ack.set_header("From", orig.get_header("From") or
                       f"<{call.caller_uri}>;tag={call.from_tag}")
        ack.set_header("Call-ID", call.call_id)
        ack.set_header("CSeq", f"{call.cseq} ACK")
        ack.set_header("Content-Length", "0")
        self.transport.send_message(ack, self.server_addr)
        logger.debug("ACK for 401 sent (branch=%s)", via_val.split(";branch=")[-1] if via_val and ";branch=" in via_val else "?")

        self.cseq += 1
        auth_value = self.auth.build_authorization(
            "INVITE", str(call.callee_uri))

        new_req = make_invite(call.callee_uri, self.contact_uri,
                              call.call_id, self.cseq, call.from_tag,
                              sdp=orig.body or "")
        new_req.set_header("Authorization", auth_value)
        call.cseq = self.cseq
        call._invite_req = new_req

        logger.info("INVITE (auth) -> %s", self.server_addr)
        self.transport.send_message(new_req, self.server_addr)

    def _handle_invite(self, req: SIPRequest, addr: Tuple[str, int]):
        """Incoming call."""
        from_tag, caller_uri = parse_from_header(req.get_header("From") or "")
        _, callee_uri = parse_from_header(req.get_header("To") or "")
        to_tag = make_tag()
        call_id = req.get_header("Call-ID") or ""

        cseq_line = req.get_header("CSeq") or ""
        cseq_num = 1
        if cseq_line:
            try:
                cseq_num = int(cseq_line.split()[0])
            except (ValueError, IndexError):
                pass

        call = Call(
            call_id=call_id,
            caller_uri=caller_uri,
            callee_uri=callee_uri,
            state=CallState.RINGING,
            cseq=cseq_num,
            from_tag=from_tag or "",
            to_tag=to_tag,
            _client=self,
            _invite_req=req,
            _peer_addr=addr,
        )
        self.calls[call_id] = call

        trying = make_response(req, 100, "Trying")
        self.transport.send_message(trying, addr)

        ringing = make_response(req, 180, "Ringing",
                                {"Contact": f"<{self.contact_uri}>",
                                 "To": f"<{callee_uri}>;tag={to_tag}"})
        self.transport.send_message(ringing, addr)

        self._emit("invite", call)

    def _handle_ack(self, req: SIPRequest, addr: Tuple[str, int]):
        call_id = req.get_header("Call-ID") or ""
        call = self.calls.get(call_id)
        if call and call.state == CallState.RINGING:
            call.state = CallState.ACTIVE

    def _handle_bye(self, req: SIPRequest, addr: Tuple[str, int]):
        call_id = req.get_header("Call-ID") or ""
        call = self.calls.get(call_id)
        if call:
            call.state = CallState.TERMINATED
            if call._media:
                call._media.stop()
                call._media = None
            logger.info("Received BYE for call: %s", call_id)
        self._emit("call_ended", call)

        ok = make_response(req, 200, "OK")
        self.transport.send_message(ok, addr)
        
        # Remove call from active calls dict
        if call_id in self.calls:
            del self.calls[call_id]

    def _handle_cancel(self, req: SIPRequest, addr: Tuple[str, int]):
        call_id = req.get_header("Call-ID") or ""
        call = self.calls.get(call_id)
        if call:
            call.state = CallState.TERMINATED
            if call._media:
                call._media.stop()
                call._media = None
        self._emit("cancel", call)

        ok = make_response(req, 200, "OK")
        self.transport.send_message(ok, addr)

    def _handle_options(self, req: SIPRequest, addr: Tuple[str, int]):
        resp = make_response(req, 200, "OK", {
            "Allow": "INVITE, ACK, BYE, CANCEL, OPTIONS, NOTIFY, MESSAGE",
            "Accept": "application/sdp",
        })
        self.transport.send_message(resp, addr)

    def _handle_register_response(self, resp: SIPResponse):
        pass

    # --- Utility ---

    def _resolve_host(self, host: str) -> Optional[str]:
        """Resolve hostname to IPv4 address."""
        try:
            return socket.getaddrinfo(host, 0, socket.AF_INET)[0][4][0]
        except OSError:
            return None

    def _get_local_ip(self, target_ip: str) -> str:
        """Get local IP that routes to target IP."""
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            s.settimeout(0.1)
            s.connect((target_ip, 1))
            ip = s.getsockname()[0]
            s.close()
            return ip
        except Exception:
            return "127.0.0.1"
