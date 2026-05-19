#!/usr/bin/env python3
"""
SIP VoIP Client – from scratch, no external deps.

Usage:
  python main.py connect sip.example.com --username alice --password secret

  python main.py connect sip.example.com --username alice --password secret \\
      --on-invite "print(f'Call from {c.caller_number}'); c.accept()"
"""
import argparse
import logging
import os
import sys

# Ermöglicht `python voip/main.py` und `python -m voip.main`
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), 'src'))

import simple_sip
from simple_sip import SIPClient, Call

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)


def main():
    parser = argparse.ArgumentParser(
        description="SIP VoIP Client (pure Python, no deps)",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  python main.py connect sip.provider.com --user alice --pass secret

  python main.py connect sip.provider.com --user alice --pass secret \\
      --on-invite "c.accept()"

  python main.py connect sip.provider.com --user alice --pass secret \\
      --on-invite "print(f'Call from {c.caller_number}'); c.reject()"

  # As library in your own script:
  #   from voip import SIPClient
  #   cli = SIPClient()
  #   cli.on("invite", lambda c: print(f"Incoming: {c.caller_number}"))
  #   cli.connect("sip.provider.com", username="alice", password="secret")
  #   cli.run()
""",
    )

    sub = parser.add_subparsers(dest="command", required=True)

    connect = sub.add_parser("connect", help="Connect to a SIP server")
    connect.add_argument("server", help="SIP server hostname or IP")
    connect.add_argument("-p", "--port", type=int, default=5060,
                         help="SIP port (default: 5060)")
    connect.add_argument("-u", "--username", default="",
                         help="SIP username for authentication")
    connect.add_argument("-pw", "--password", default="",
                         help="SIP password")
    connect.add_argument("-lp", "--local-port", type=int, default=5060,
                         help="Local UDP port (default: 5060)")
    connect.add_argument("-v", "--verbose", action="store_true",
                         help="Enable debug logging")
    connect.add_argument("--on-invite", default="",
                         help="Python code for incoming calls. "
                              "Variable 'c' is the Call object. "
                              "Example: --on-invite \"c.accept()\"")
    connect.add_argument("--auto-accept", action="store_true",
                         help="Shortcut: auto-accept all calls")

    args = parser.parse_args()

    if args.verbose:
        logging.getLogger("sip").setLevel(logging.DEBUG)

    client = SIPClient()

    client.on("registered", lambda host: print(f"Registered at {host}"))

    def on_invite(call: Call):
        print(f"\nIncoming call from: {call.caller_number}")
        print(f"  To: {call.callee_number}")

        if args.auto_accept:
            call.accept()
            print("  → Accepted")
            return

        if args.on_invite:
            try:
                exec(args.on_invite, {"c": call, "print": print})
            except Exception as e:
                print(f"  ! Handler error: {e}")
                call.reject()
            return

        print("  → Rejected (no handler)")
        call.reject()

    client.on("invite", on_invite)
    client.on("call_accepted", lambda c: print(f"Call active: {c.caller_number}"))
    client.on("call_ended", lambda c: print(f"Call ended: {c.caller_number}"))
    client.on("call_rejected", lambda c, code, reason:
              print(f"Call rejected: {code} {reason}"))
    client.on("error", lambda msg: print(f"Error: {msg}"))

    try:
        ok = client.connect(
            server=args.server,
            port=args.port,
            username=args.username,
            password=args.password,
            local_port=args.local_port,
        )
        if ok:
            print("Waiting for events... (Ctrl+C to quit)")
            client.run()
        else:
            print("Connection failed")
            sys.exit(1)
    except KeyboardInterrupt:
        print("\nShutting down...")
    finally:
        client.stop()


if __name__ == "__main__":
    main()
