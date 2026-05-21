# simple-sip-client

[![PyPI](https://img.shields.io/pypi/v/simple-sip-client)](https://pypi.org/project/simple-sip-client/)
[![GitHub](https://img.shields.io/badge/github-SerafinoBarwinski%2Fsimple--sip--python-blue?logo=github)](https://github.com/SerafinoBarwinski/simple-sip-python)
[![Build](https://github.com/SerafinoBarwinski/simple-sip-python/actions/workflows/publish.yml/badge.svg?branch=main)](https://github.com/SerafinoBarwinski/simple-sip-python/actions/workflows/publish.yml)

Pure-Python SIP VoIP client library (no external dependencies for SIP/RTP).

Full documentation: [docs/index.md](docs/index.md)

## Installation

```bash
# from PyPI
pip install simple-sip-client

# or from source (editable)
pip install -e .
```

Then use from any script:

```python
from simple_sip import SIPClient
```

Or run demos directly (auto-adds `src/` to path):

```bash
python demo.py
python hold-queue.py
```

## Project Structure

```
voip/
├── README.md
├── setup.py
├── pyproject.toml
├── hold-queue-config.json
├── demo.py              # Interactive SIP client demo
├── hold-queue.py        # Music-on-hold with announcements
├── queue-demo.py        # Simple audio streaming demo
├── debug_audio.py       # RTP audio dump tool
├── main.py              # CLI-based SIP client
└── src/
    └── simple_sip/      # Python package
        ├── __init__.py
        ├── sip_client.py   # SIP client (register, call, events)
        ├── sip_message.py  # SIP message parser/generator
        ├── sip_auth.py     # Digest authentication
        ├── sip_transport.py # UDP transport
        ├── sip_sdp.py      # SDP parser/answer/offer
        ├── sip_media.py    # RTP media stream
        └── ringtone.py     # Ringtone player
```

## Demos

- **demo.py** – Interactive SIP client (receive/make calls)
- **hold-queue.py** – Auto-answer with music-on-hold + announcements
- **queue-demo.py** – Stream audio file as RTP to caller
- **debug_audio.py** – Save incoming RTP audio to WAV files
- **main.py** – CLI with `--on-invite` scripts

## Configuration

Edit `hold-queue-config.json` for hold-queue.py settings.
Environment variables `SIP_SERVER`, `SIP_PORT`, `SIP_USER`, `SIP_PASS`
override defaults in demo scripts.

## License

MIT
