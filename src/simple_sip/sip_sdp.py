import re
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

CODEC_NAMES = {0: "PCMU", 8: "PCMA", 3: "GSM", 4: "G723", 9: "G722"}


@dataclass
class SDPMedia:
    media: str  # audio/video
    port: int
    transport: str
    formats: List[str]
    attributes: Dict[str, str]


@dataclass
class SDP:
    username: str = "-"
    session_id: str = "0"
    session_version: str = "0"
    nettype: str = "IN"
    addrtype: str = "IP4"
    unicast_address: str = "0.0.0.0"
    session_name: str = "-"
    media: List[SDPMedia] = field(default_factory=list)


def parse_sdp(body: str) -> Optional[SDP]:
    sdp = SDP()
    current_media = None

    for line in body.strip().splitlines():
        line = line.strip()
        if not line or len(line) < 2 or line[1] != "=":
            continue
        key = line[0]
        value = line[2:]

        if key == "v":
            pass
        elif key == "o":
            parts = value.split()
            if len(parts) >= 6:
                sdp.username = parts[0]
                sdp.session_id = parts[1]
                sdp.session_version = parts[2]
                sdp.nettype = parts[3]
                sdp.addrtype = parts[4]
                sdp.unicast_address = parts[5]
        elif key == "s":
            sdp.session_name = value
        elif key == "c":
            parts = value.split()
            if len(parts) >= 3:
                sdp.nettype = parts[0]
                sdp.addrtype = parts[1]
                sdp.unicast_address = parts[2]
        elif key == "m":
            if current_media:
                sdp.media.append(current_media)
            parts = value.split()
            if len(parts) >= 4:
                current_media = SDPMedia(
                    media=parts[0],
                    port=int(parts[1]),
                    transport=parts[2],
                    formats=parts[3:],
                    attributes={},
                )
            else:
                current_media = None
        elif key == "a" and current_media:
            if ":" in value:
                ak, av = value.split(":", 1)
                if ak == "rtpmap":
                    codec_id = av.split()[0] if av.split() else "0"
                    current_media.attributes[f"rtpmap:{codec_id}"] = av
                else:
                    current_media.attributes[ak] = av
            else:
                current_media.attributes[value] = ""

    if current_media:
        sdp.media.append(current_media)

    return sdp


def make_sdp_answer(offer_body: str, local_ip: str, local_port: int,
                    payload_type: int = 0) -> str:
    """Generate a minimal SDP answer from an offer.

    local_port is the port we claim to accept RTP on.
    Port 0 = media not wanted (RFC 3264).
    payload_type: selected codec (0=PCMU, 8=PCMA, etc.)
    """
    lines = []
    lines.append("v=0")
    lines.append(f"o=- 0 0 IN IP4 {local_ip}")
    lines.append("s=-")
    lines.append(f"c=IN IP4 {local_ip}")
    lines.append("t=0 0")

    offer = parse_sdp(offer_body)
    codec_str = str(payload_type)
    if offer and offer.media:
        for m in offer.media:
            lines.append(f"m={m.media} {local_port} {m.transport} {codec_str}")
            fallback_name = CODEC_NAMES.get(payload_type, "PCMU")
            rtpmap = m.attributes.get(f"rtpmap:{codec_str}",
                                      f"{codec_str} {fallback_name}/8000")
            lines.append(f"a=rtpmap:{rtpmap}")
            lines.append("a=sendrecv")
    else:
        lines.append("m=audio 0 RTP/AVP 0")

    lines.append("")
    return "\r\n".join(lines)


def make_sdp_offer(local_ip: str, local_port: int,
                   codecs: Optional[List[int]] = None) -> str:
    """Generate an SDP offer for an outbound call."""
    if codecs is None:
        codecs = [0, 8]
    lines = []
    lines.append("v=0")
    lines.append(f"o=- 0 0 IN IP4 {local_ip}")
    lines.append("s=-")
    lines.append(f"c=IN IP4 {local_ip}")
    lines.append("t=0 0")
    codec_str = " ".join(str(c) for c in codecs)
    lines.append(f"m=audio {local_port} RTP/AVP {codec_str}")
    for c in codecs:
        name = CODEC_NAMES.get(c, "PCMU")
        lines.append(f"a=rtpmap:{c} {name}/8000")
    lines.append("a=sendrecv")
    lines.append("")
    return "\r\n".join(lines)
