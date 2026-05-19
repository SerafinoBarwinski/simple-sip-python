import hashlib
import re
from typing import Dict, Optional


class DigestAuth:
    """HTTP Digest Authentication (RFC 2617) for SIP."""

    def __init__(self, username: str, password: str):
        self.username = username
        self.password = password
        self.realm: Optional[str] = None
        self.nonce: Optional[str] = None
        self.opaque: Optional[str] = None
        self.qop: Optional[str] = None
        self.algorithm: str = "MD5"

    def parse_challenge(self, www_auth: str) -> bool:
        """Parse WWW-Authenticate header value (Digest challenge)."""
        if not www_auth.lower().startswith("digest"):
            return False

        www_auth = www_auth[7:]  # strip "Digest"
        params = self._parse_params(www_auth)

        self.realm = params.get("realm")
        self.nonce = params.get("nonce")
        self.opaque = params.get("opaque")
        self.qop = params.get("qop")
        self.algorithm = params.get("algorithm", "MD5")

        return bool(self.realm and self.nonce)

    def build_authorization(self, method: str, uri: str,
                            body: str = "") -> str:
        """Build Authorization header value for a SIP request."""
        if not self.realm or not self.nonce:
            raise ValueError("No challenge parsed yet")

        ha1_input = f"{self.username}:{self.realm}:{self.password}"
        ha1 = hashlib.md5(ha1_input.encode()).hexdigest()

        ha2_input = f"{method}:{uri}"
        if body:
            ha2_input += f":{hashlib.md5(body.encode()).hexdigest()}"
        ha2 = hashlib.md5(ha2_input.encode()).hexdigest()

        if self.qop:
            # Pick first qop option (usually "auth")
            qop = self.qop.split(",")[0].strip()
            nc = "00000001"
            cnonce = hashlib.md5(f"{self.username}:{self.nonce}".encode()).hexdigest()[:16]
            response_input = f"{ha1}:{self.nonce}:{nc}:{cnonce}:{qop}:{ha2}"
            response = hashlib.md5(response_input.encode()).hexdigest()
            params = {
                "username": self.username,
                "realm": self.realm,
                "nonce": self.nonce,
                "uri": uri,
                "qop": qop,
                "nc": nc,
                "cnonce": cnonce,
                "response": response,
                "algorithm": self.algorithm,
            }
        else:
            response_input = f"{ha1}:{self.nonce}:{ha2}"
            response = hashlib.md5(response_input.encode()).hexdigest()
            params = {
                "username": self.username,
                "realm": self.realm,
                "nonce": self.nonce,
                "uri": uri,
                "response": response,
                "algorithm": self.algorithm,
            }

        if self.opaque:
            params["opaque"] = self.opaque

        parts = []
        for k, v in params.items():
            if k in ("nc",):
                parts.append(f'{k}={v}')
            else:
                parts.append(f'{k}="{v}"')

        return "Digest " + ", ".join(parts)

    def _parse_params(self, text: str) -> Dict[str, str]:
        """Parse key=value pairs from auth header."""
        params = {}
        for match in re.finditer(r'(\w+)\s*=\s*(?:"([^"]*)"|(\S+))', text):
            key = match.group(1).lower()
            value = match.group(2) or match.group(3)
            params[key] = value
        return params
