#!/usr/bin/env python3
"""
SIP VoIP Client – Demo (library usage)
"""
import logging
import os
import socket
import sys
import threading

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), 'src'))

# Enable debug logging (comment out for less output)
logging.basicConfig(
    level=logging.DEBUG,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)

from simple_sip import SIPClient, CallState, Ringtone, PATTERNS, MediaStream

# "ring" = ding ding – pause – ding ding (800 Hz)
# "busy" = fast busy tone (440 Hz)
ringtone = Ringtone(pattern="ring")
if ringtone.available:
    print(f"  ✓ Ringtone: {ringtone._player}  ({PATTERNS[ringtone.pattern]['desc']})")
else:
    print("  ! No audio player – Install 'pip install sounddevice'")
    print("    or ensure aplay/paplay/ffplay is available")

try:
    import sounddevice
    print("  ✓ Audio-Wiedergabe: sounddevice")
except ImportError:
    print("  ! No audio on calls – 'pip install sounddevice' for voice playback")


# --- Connectivity check before connecting ---
def check_server(host: str, port: int) -> bool:
    """Check if the server is reachable (UDP)."""
    try:
        ip = socket.getaddrinfo(host, port, socket.AF_INET)[0][4][0]
        print(f"  ✓ DNS: {host} -> {ip}")
    except OSError as e:
        print(f"  ✗ DNS error: {e}")
        return False

    # UDP "connect" without sending data (tests routing only)
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.settimeout(1)
        s.connect((ip, port))
        print(f"  ✓ Route to {ip}:{port} OK (no firewall block at IP level)")
        s.close()
        return True
    except OSError as e:
        print(f"  ! Route to {ip}:{port} failed: {e}")
        return False


# --- Client Setup ---
client = SIPClient()

# Events
client.on("registered", lambda host: print(f"\n✓ Registered at {host}"))

def on_invite(call):
    print(f"\n📞 Incoming call from: {call.caller_number}")
    ringtone.play()

    def interactive():
        print("  [Enter] accept  |  [r] reject")
        choice = input().strip().lower()
        ringtone.stop()
        if choice == "r":
            call.reject()
            print("   → Rejected")
        else:
            call.accept()
            print("   → Accepted")

    threading.Thread(target=interactive, daemon=True).start()

client.on("invite", on_invite)
client.on("call_accepted", lambda c: print(f"✓ Call active: {c.caller_number}"))
client.on("call_rejected", lambda c, code, reason: ringtone.stop())

def on_call_ended(c):
    # Outgoing call: show callee, incoming: show caller
    our_user = client.contact_uri.user if client.contact_uri else ""
    peer = c.callee_number if c.caller_uri.user == our_user else c.caller_number
    print(f"✗ Call ended: {peer}")

client.on("call_ended", on_call_ended)
client.on("call_active", lambda c: print(f"✓ Connected to {c.callee_number}"))
client.on("ringing", lambda c: print(f"🔔 Ringing at {c.callee_number}"))
client.on("error", lambda msg: print(f"\n⚠ Error: {msg}"))


# --- Connect ---
SERVER = "192.168.178.1"
PORT = 5060
USER = os.getenv("SIP_USER", "changeme")
PASS = os.getenv("SIP_PASS", "")

print(f"SIP Client starting:")
print(f"  Server: {SERVER}:{PORT}")
print(f"  User:   {USER}")

if not check_server(SERVER, PORT):
    print("\n✗ Server unreachable – check network/firewall!")
    print("  Tip: Is the SIP server running on a different port?")
    sys.exit(1)

ok = client.connect(
    server=SERVER,
    port=PORT,
    username=USER,
    password=PASS,
    local_port=0,  # 0 = OS picks free port (avoids conflicts)
)

if ok:
    print("\nConnected. Waiting for calls... (Ctrl+C to quit)")
    print("  Enter number + Enter → outgoing call")
    print("  Enter on incoming call → accept")
    print("  'h' + Enter → hang up active call")
    try:
        while True:
            import select
            if sys.stdin in select.select([sys.stdin], [], [], 0.5)[0]:
                line = sys.stdin.readline().strip().lower()
                if line == "h":
                    for c in list(client.calls.values()):
                        if c.state == CallState.ACTIVE:
                            c.hangup()
                elif line:
                    line = line.replace("+", "")
                    if not line.startswith("0"):
                        line = "0" + line
                    number = f"sip:{line}@{SERVER}"
                    print(f"  → Calling {line}...")
                    call = client.make_call(number)
                    if not call:
                        print("  ✗ Could not start call")
    except KeyboardInterrupt:
        print("\nShutting down...")
    finally:
        client.stop()
else:
    print("\n✗ Connection failed")
    print("  Tip: - Check server/port are correct")
    print("        - Check if another service is blocking the port")
    print("        - Start with local_port=0 (as above)")
