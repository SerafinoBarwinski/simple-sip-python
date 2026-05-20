#!/usr/bin/env python3
"""
SIP VoIP Client – Demo (Bibliotheksnutzung)
"""
import logging
import os
import socket
import sys
import threading

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), 'src'))

# Debug-Logging aktivieren (auskommentieren für weniger Ausgabe)
logging.basicConfig(
    level=logging.DEBUG,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)

from simple_sip import SIPClient, CallState, Ringtone, PATTERNS, MediaStream

# "ring" = ding ding – pause – ding ding (800 Hz)
# "busy" = schnelle Belegt-Töne (440 Hz)
ringtone = Ringtone(pattern="ring")
if ringtone.available:
    print(f"  ✓ Ringtone: {ringtone._player}  ({PATTERNS[ringtone.pattern]['desc']})")
else:
    print("  ! Kein Audio-Player – Installiere 'pip install sounddevice'")
    print("    oder sorge für aplay/paplay/ffplay")

try:
    import sounddevice
    print("  ✓ Audio-Wiedergabe: sounddevice")
except ImportError:
    print("  ! Kein Audio bei Anrufen – 'pip install sounddevice' für Sprachausgabe")


# --- Connectivity-Check vor dem Connect ---
def check_server(host: str, port: int) -> bool:
    """Prüft ob der Server überhaupt erreichbar ist (UDP)."""
    try:
        ip = socket.getaddrinfo(host, port, socket.AF_INET)[0][4][0]
        print(f"  ✓ DNS: {host} -> {ip}")
    except OSError as e:
        print(f"  ✗ DNS-Fehler: {e}")
        return False

    # UDP "connect" ohne Daten zu senden (testet nur Routing)
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.settimeout(1)
        s.connect((ip, port))
        print(f"  ✓ Route zu {ip}:{port} OK (keine Firewall-Blockade auf IP-Ebene)")
        s.close()
        return True
    except OSError as e:
        print(f"  ! Route zu {ip}:{port} fehlgeschlagen: {e}")
        return False


# --- Client Setup ---
client = SIPClient()

# Events
client.on("registered", lambda host: print(f"\n✓ Registriert bei {host}"))

def on_invite(call):
    print(f"\n📞 Eingehender Anruf von: {call.caller_number}")
    ringtone.play()

    def interactive():
        print("  [Enter] annehmen  |  [r] ablehnen")
        choice = input().strip().lower()
        ringtone.stop()
        if choice == "r":
            call.reject()
            print("   → Abgewiesen")
        else:
            call.accept()
            print("   → Angenommen")

    threading.Thread(target=interactive, daemon=True).start()

client.on("invite", on_invite)
client.on("call_accepted", lambda c: print(f"✓ Anruf aktiv: {c.caller_number}"))
client.on("call_rejected", lambda c, code, reason: ringtone.stop())

def on_call_ended(c):
    # Ausgehender Call: callee anzeigen, eingehender: caller anzeigen
    our_user = client.contact_uri.user if client.contact_uri else ""
    peer = c.callee_number if c.caller_uri.user == our_user else c.caller_number
    print(f"✗ Anruf beendet: {peer}")

client.on("call_ended", on_call_ended)
client.on("call_active", lambda c: print(f"✓ Verbunden mit {c.callee_number}"))
client.on("ringing", lambda c: print(f"🔔 Klingelt bei {c.callee_number}"))
client.on("error", lambda msg: print(f"\n⚠ Fehler: {msg}"))


# --- Verbinden ---
SERVER = "192.168.178.1"
PORT = 5060
USER = os.getenv("SIP_USER", "changeme")
PASS = os.getenv("SIP_PASS", "")

print(f"SIP Client starte:")
print(f"  Server: {SERVER}:{PORT}")
print(f"  User:   {USER}")

if not check_server(SERVER, PORT):
    print("\n✗ Server nicht erreichbar – Netzwerk/Firewall prüfen!")
    print("  Tipp: Läuft der SIP-Server auf einem anderen Port?")
    sys.exit(1)

ok = client.connect(
    server=SERVER,
    port=PORT,
    username=USER,
    password=PASS,
    local_port=0,  # 0 = OS wählt freien Port (vermeidet Konflikte)
)

if ok:
    print("\nVerbunden. Warte auf Anrufe... (Strg+C zum Beenden)")
    print("  Rufnummer eingeben + Enter → ausgehender Anruf")
    print("  Enter bei eingehendem Anruf → annehmen")
    print("  'h' + Enter → aktiven Anruf beenden")
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
                    print(f"  → Rufe {line} an...")
                    call = client.make_call(number)
                    if not call:
                        print("  ✗ Konnte Anruf nicht starten")
    except KeyboardInterrupt:
        print("\nBeende...")
    finally:
        client.stop()
else:
    print("\n✗ Verbindung fehlgeschlagen")
    print("  Tipp: - Prüfe ob Server/Port stimmen")
    print("        - Prüfe ob ein anderer Dienst den Port blockt")
    print("        - Starte mit local_port=0 (wie oben)")
