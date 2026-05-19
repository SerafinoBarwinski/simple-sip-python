import re
import uuid
import time
from dataclasses import dataclass, field
from typing import Optional, Dict, List, Tuple

SIP_VERSION = "SIP/2.0"

COMPACT_HEADERS = {
    'v': 'Via', 'f': 'From', 't': 'To', 'i': 'Call-ID',
    'm': 'Contact', 'l': 'Content-Length', 'e': 'Content-Encoding',
    'c': 'Content-Type', 's': 'Subject',
}

@dataclass
class SIPURI:
    scheme: str = "sip"
    user: Optional[str] = None
    password: Optional[str] = None
    host: str = ""
    port: Optional[int] = None
    params: Dict[str, str] = field(default_factory=dict)
    headers: Dict[str, str] = field(default_factory=dict)

    def __str__(self):
        result = f"{self.scheme}:"
        if self.user:
            result += self.user
            if self.password:
                result += f":{self.password}"
            result += "@"
        result += self.host
        if self.port is not None:
            result += f":{self.port}"
        for k, v in self.params.items():
            result += f";{k}"
            if v:
                result += f"={v}"
        if self.headers:
            result += "?" + "&".join(f"{k}={v}" for k, v in self.headers.items())
        return result

    def __repr__(self):
        return f"<{self.scheme}:{self.user or ''}@{self.host}>"

    def without_user(self) -> "SIPURI":
        return SIPURI(scheme=self.scheme, host=self.host, port=self.port)

    def as_contact(self, display_name: str = "") -> str:
        uri = str(self)
        if display_name:
            return f'"{display_name}" <{uri}>'
        return f"<{uri}>"


def parse_sip_uri(uri_str: str) -> SIPURI:
    uri_str = uri_str.strip()
    display_name = None
    if uri_str.startswith('"'):
        m = re.match(r'"([^"]*)"\s*<(.*)>', uri_str)
        if m:
            display_name = m.group(1)
            uri_str = m.group(2)
    elif uri_str.startswith('<') and '>' in uri_str:
        m = re.match(r'<(.*)>', uri_str)
        if m:
            uri_str = m.group(1)

    if ':' in uri_str:
        scheme, rest = uri_str.split(':', 1)
    else:
        scheme = "sip"
        rest = uri_str

    uri = SIPURI(scheme=scheme.lower())
    header_str = ""
    param_str = ""

    if '?' in rest:
        rest, header_str = rest.split('?', 1)
    if ';' in rest:
        rest, param_str = rest.split(';', 1)

    if header_str:
        for hdr in header_str.split('&'):
            if '=' in hdr:
                k, v = hdr.split('=', 1)
                uri.headers[k] = v

    if param_str:
        for param in param_str.split(';'):
            if '=' in param:
                k, v = param.split('=', 1)
                uri.params[k] = v
            else:
                uri.params[param] = ""

    if '@' in rest:
        userinfo, hostport = rest.split('@', 1)
        if ':' in userinfo:
            uri.user, uri.password = userinfo.split(':', 1)
        else:
            uri.user = userinfo
    else:
        hostport = rest

    if ':' in hostport:
        uri.host, port_str = hostport.split(':', 1)
        uri.port = int(port_str)
    else:
        uri.host = hostport

    return uri


class SIPMessage:
    def __init__(self):
        self.headers: Dict[str, List[str]] = {}
        self.body: str = ""
        self.raw: Optional[bytes] = None

    def get_header(self, name: str) -> Optional[str]:
        for k, v in self.headers.items():
            if k.lower() == name.lower():
                return v[0] if v else None
        return None

    def get_all_headers(self, name: str) -> List[str]:
        for k, v in self.headers.items():
            if k.lower() == name.lower():
                return v
        return []

    def set_header(self, name: str, value: str):
        for k in list(self.headers.keys()):
            if k.lower() == name.lower():
                if name != k:
                    self.headers[name] = self.headers.pop(k)
                self.headers[name] = [value]
                return
        self.headers[name] = [value]

    def add_header(self, name: str, value: str):
        self.headers.setdefault(name, []).append(value)

    def remove_header(self, name: str):
        for k in list(self.headers.keys()):
            if k.lower() == name.lower():
                del self.headers[k]

    def serialize_headers(self) -> bytes:
        lines = []
        for name, values in self.headers.items():
            for value in values:
                lines.append(f"{name}: {value}")
        return "\r\n".join(lines).encode()

    def serialize(self) -> bytes:
        raise NotImplementedError

    @property
    def content_length(self) -> int:
        cl = self.get_header("Content-Length")
        return int(cl) if cl else 0

    @classmethod
    def parse(cls, data: bytes) -> "SIPMessage":
        text = data.decode("utf-8", errors="replace")
        lines = text.split("\r\n")

        if not lines:
            raise ValueError("Empty SIP message")

        first_line = lines[0]

        if first_line.startswith("SIP/2.0"):
            msg = SIPResponse._parse_first_line(first_line)
        elif "SIP/2.0" in first_line:
            msg = SIPRequest._parse_first_line(first_line)
        else:
            raise ValueError(f"Unknown SIP message: {first_line[:80]}")

        msg.raw = data
        idx = 1
        while idx < len(lines):
            line = lines[idx]
            if line == "":
                idx += 1
                break
            if line.startswith(" ") or line.startswith("\t"):
                folded = line.strip()
                if msg.headers:
                    last_key = list(msg.headers.keys())[-1]
                    msg.headers[last_key][-1] += " " + folded
            elif ":" in line:
                hname, hvalue = line.split(":", 1)
                hname = hname.strip()
                hvalue = hvalue.strip()
                canonical = COMPACT_HEADERS.get(hname, hname)
                msg.headers.setdefault(canonical, []).append(hvalue)
            idx += 1

        msg.body = "\r\n".join(lines[idx:])
        return msg

    def __repr__(self):
        return self.serialize().decode("utf-8", errors="replace")


class SIPRequest(SIPMessage):
    def __init__(self, method: str = "", uri: str = ""):
        super().__init__()
        self.method = method.upper()
        self.uri = uri

    def serialize(self) -> bytes:
        if not self.body and self.content_length == 0:
            self.set_header("Content-Length", "0")
        first = f"{self.method} {self.uri} {SIP_VERSION}\r\n".encode()
        return first + self.serialize_headers() + b"\r\n\r\n" + self.body.encode()

    @staticmethod
    def _parse_first_line(line: str) -> "SIPRequest":
        parts = line.split(" ", 2)
        if len(parts) != 3:
            raise ValueError(f"Invalid request line: {line}")
        return SIPRequest(method=parts[0], uri=parts[1])


class SIPResponse(SIPMessage):
    def __init__(self, status_code: int = 0, reason: str = ""):
        super().__init__()
        self.status_code = status_code
        self.reason = reason

    def serialize(self) -> bytes:
        if not self.body and self.content_length == 0:
            self.set_header("Content-Length", "0")
        first = f"{SIP_VERSION} {self.status_code} {self.reason}\r\n".encode()
        return first + self.serialize_headers() + b"\r\n\r\n" + self.body.encode()

    @staticmethod
    def _parse_first_line(line: str) -> "SIPResponse":
        parts = line.split(" ", 2)
        if len(parts) < 3:
            raise ValueError(f"Invalid status line: {line}")
        return SIPResponse(status_code=int(parts[1]), reason=parts[2])


def make_via(host: str, port: int, branch: Optional[str] = None) -> str:
    b = branch or f"z9hG4bK{uuid.uuid4().hex[:12]}"
    return f"SIP/2.0/UDP {host}:{port};branch={b}"


def make_tag() -> str:
    return uuid.uuid4().hex[:16]


def make_call_id() -> str:
    return f"{uuid.uuid4().hex[:16]}@sip"


def make_register(uri: SIPURI, contact: SIPURI, call_id: str,
                  cseq: int, tag: str, expires: int = 3600) -> SIPRequest:
    """Build SIP REGISTER request."""
    aor = str(uri)  # sip:user@host:port
    domain = str(uri.without_user())  # sip:host:port
    req = SIPRequest("REGISTER", domain)
    req.set_header("Via", make_via(contact.host, contact.port or 5060))
    req.set_header("Max-Forwards", "70")
    req.set_header("To", f"<{aor}>")
    req.set_header("From", f"<{aor}>;tag={tag}")
    req.set_header("Call-ID", call_id)
    req.set_header("CSeq", f"{cseq} REGISTER")
    req.set_header("Contact", f"<{contact}>")
    req.set_header("User-Agent", "PySIP/0.1")
    req.set_header("Expires", str(expires))
    req.set_header("Content-Length", "0")
    return req


def make_invite(callee_uri: SIPURI, contact_uri: SIPURI,
                call_id: str, cseq: int, tag: str, sdp: str = "") -> SIPRequest:
    """Build SIP INVITE request."""
    req = SIPRequest("INVITE", str(callee_uri))
    req.set_header("Via", make_via(contact_uri.host, contact_uri.port or 5060))
    req.set_header("Max-Forwards", "70")
    req.set_header("To", str(callee_uri))
    req.set_header("From", f"<{contact_uri}>;tag={tag}")
    req.set_header("Call-ID", call_id)
    req.set_header("CSeq", f"{cseq} INVITE")
    req.set_header("Contact", f"<{contact_uri}>")
    req.set_header("Content-Type", "application/sdp")
    req.set_header("Content-Length", str(len(sdp)))
    req.body = sdp
    return req


def make_response(req: SIPMessage, status: int, reason: str,
                  extra_headers: Optional[Dict[str, str]] = None,
                  body: str = "") -> SIPResponse:
    """Build SIP response to a request, copying Via/To/From/Call-ID/CSeq."""
    resp = SIPResponse(status, reason)
    for h in req.get_all_headers("Via"):
        resp.add_header("Via", h)
    resp.set_header("To", req.get_header("To") or "")
    resp.set_header("From", req.get_header("From") or "")
    resp.set_header("Call-ID", req.get_header("Call-ID") or "")
    resp.set_header("CSeq", req.get_header("CSeq") or "")
    resp.set_header("Content-Length", str(len(body)))

    if (extra_headers):
        for k, v in extra_headers.items():
            resp.set_header(k, v)
    if body:
        resp.body = body
    return resp


def parse_contact_header(value: str) -> SIPURI:
    """Parse a Contact header value into a SIPURI."""
    return parse_sip_uri(value)


def parse_from_header(value: str) -> Tuple[Optional[str], SIPURI]:
    """Parse From/To header returning (tag, SIPURI)."""
    tag = None
    if ';' in value:
        base, params = value.split(';', 1)
        for p in params.split(';'):
            p = p.strip()
            if p.startswith("tag="):
                tag = p[4:]
        value = base
    return tag, parse_sip_uri(value)
