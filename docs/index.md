# simple-sip-client Documentation

## Installation

```bash
pip install simple-sip-client
```

## Quick Start

### Basic SIP client

```python
from simple_sip import SIPClient

client = SIPClient()

def on_invite(call):
    print(f"Incoming call from: {call.caller_number}")
    call.accept()

client.on("invite", on_invite)
client.on("registered", lambda h: print(f"Registered at {h}"))
client.on("call_ended", lambda c: print(f"Call ended: {c.caller_number}"))

client.connect("sip.example.com", username="user", password="pass")
client.run()
```

## API Reference

### SIPClient

Main client for SIP registration and call handling.

```python
client = SIPClient()
client.connect(server, port=5060, username="", password="", local_port=5060)
```

**Events:**

| Event | Arguments | Description |
|-------|-----------|-------------|
| `registered` | `host` | Successfully registered |
| `register_response` | `status_code, reason` | Registration response |
| `connecting` | `server, port` | Connecting to server |
| `invite` | `call` | Incoming call |
| `call_accepted` | `call` | Outgoing call accepted |
| `call_rejected` | `call, code, reason` | Call rejected |
| `call_active` | `call` | Call established (ACK sent) |
| `call_ended` | `call` | Call terminated |
| `ringing` | `call` | Remote is ringing (180) |
| `error` | `message` | Error occurred |
| `hangup` | `call` | Received BYE |
| `cancel` | `call` | Received CANCEL |
| `notify` | `req` | SIP NOTIFY received |
| `message` | `req` | SIP MESSAGE received |
| `response` | `status_code, reason` | Any SIP response |
| `unknown_request` | `req` | Unknown SIP method |

### Call

Represents a SIP call session.

```python
call.caller_number   # str – caller's phone number
call.callee_number   # str – callee's phone number
call.state           # CallState enum

call.accept(sdp="")       # Accept incoming call
call.reject(code=486, reason="Busy Here")  # Reject incoming call
call.hangup()             # End the call
```

### CallState

```python
CallState.IDLE
CallState.RINGING
CallState.ACTIVE
CallState.TERMINATED
```

### Making outgoing calls

```python
call = client.make_call("sip:1234@sip.example.com")
```

### Ringtone

Local ringtone playback for incoming calls.

```python
from simple_sip import Ringtone, PATTERNS

ringtone = Ringtone(pattern="ring")  # or "busy"
ringtone.play(loop=True)
ringtone.stop()
```

### MediaStream

RTP audio receive/playback.

```python
from simple_sip import MediaStream

media = MediaStream()
port = media.start(local_ip="0.0.0.0", payload_type=0)  # PCMU
media.stop()
```

### DebugMedia (audio dump)

```python
from simple_sip.sip_media import RTPPacket, ulaw2linear, alaw2linear
```

### SDP helpers

```python
from simple_sip.sip_sdp import make_sdp_offer, make_sdp_answer, parse_sdp
```

### Digest Authentication

```python
from simple_sip.sip_auth import DigestAuth
```

## Demos

| Demo | Description |
|------|-------------|
| `demo.py` | Interactive SIP client – receive/make calls |
| `hold-queue.py` | Auto-answer with music-on-hold + announcements |
| `queue-demo.py` | Stream audio file as RTP to caller |
| `debug_audio.py` | Save incoming RTP audio to WAV files |
| `main.py` | CLI with `--on-invite` scripts |

## Configuration

### Environment variables

| Variable | Default | Description |
|----------|---------|-------------|
| `SIP_SERVER` | `192.168.178.1` | SIP server hostname |
| `SIP_PORT` | `5060` | SIP server port |
| `SIP_USER` | – | SIP username |
| `SIP_PASS` | – | SIP password |

### hold-queue-config.json

```json
{
    "sip": {
        "server": "sip.example.com",
        "port": 5060,
        "username": "<username>",
        "password": "<password>"
    },
    "audio": {
        "welcome": "/path/to/welcome.wav",
        "hold_music": "/path/to/hold_music.wav",
        "announcements": ["/path/to/ann1.wav"],
        "announcement_interval": {"min": 60, "max": 180}
    }
}
```
