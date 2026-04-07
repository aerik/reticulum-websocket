"""
RNS WebSocket Bridge Server

Connects to the public Reticulum network via TCP and exposes
a WebSocket server on localhost for browser clients.

Browser clients connecting via WebSocket become full RNS participants --
they see network announces and can send their own.

Usage:
    python examples/bridge-server.py [--ws-port 8765] [--rns-host rns.beleth.net] [--rns-port 4242]

Requires:
    pip install rns websockets
"""

import sys
import os
import time
import argparse
import shutil

def main():
    parser = argparse.ArgumentParser(description='RNS WebSocket Bridge Server')
    parser.add_argument('--ws-port', type=int, default=8765, help='WebSocket server port (default: 8765)')
    parser.add_argument('--rns-host', default='rns.noderage.org', help='RNS TCP node to connect to')
    parser.add_argument('--rns-port', type=int, default=4242, help='RNS TCP node port')
    parser.add_argument('--no-announce', action='store_true', help='Do not announce the bridge')
    args = parser.parse_args()

    # Set up config directory
    config_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), '.bridge_config')
    os.makedirs(config_dir, exist_ok=True)

    # Install WebSocket interface
    iface_dir = os.path.join(config_dir, 'interfaces')
    os.makedirs(iface_dir, exist_ok=True)
    ws_iface_src = os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', 'WebSocketInterface.py')
    shutil.copy2(ws_iface_src, os.path.join(iface_dir, 'WebSocketInterface.py'))

    # Write config
    config = f"""[reticulum]
  enable_transport = True
  share_instance = No

[logging]
  loglevel = 4

[interfaces]
  [[RNS Network]]
    type = TCPClientInterface
    enabled = yes
    target_host = {args.rns_host}
    target_port = {args.rns_port}

  [[Browser WebSocket]]
    type = WebSocketInterface
    enabled = yes
    mode = server
    listen_ip = 0.0.0.0
    listen_port = {args.ws_port}
"""
    with open(os.path.join(config_dir, 'config'), 'w') as f:
        f.write(config)

    import RNS

    print("=== RNS WebSocket Bridge Server ===")
    print(f"  RNS Network : {args.rns_host}:{args.rns_port}")
    print(f"  WebSocket   : ws://0.0.0.0:{args.ws_port}")
    print()

    reticulum = RNS.Reticulum(configdir=config_dir)

    identity = RNS.Identity()

    # Create a destination for the bridge itself
    destination = RNS.Destination(
        identity,
        RNS.Destination.IN,
        RNS.Destination.SINGLE,
        "rns_bridge",
        "websocket"
    )

    if not args.no_announce:
        destination.announce(app_data=b"RNS WebSocket Bridge")
        print(f"Bridge identity:    {identity.hexhash}")
        print(f"Bridge destination: {destination.hexhash}")
    print()

    # Stats tracking
    announce_count = [0]

    def count_announces(destination_hash, announced_identity, app_data):
        announce_count[0] += 1
        app_str = ""
        if app_data:
            try:
                s = app_data.decode('utf-8')
                if all(32 <= ord(c) < 127 for c in s):
                    app_str = f' app="{s}"'
            except Exception:
                app_str = f" ({len(app_data)}b)"
        if announce_count[0] <= 20 or announce_count[0] % 50 == 0:
            print(f"  Announce #{announce_count[0]}: {RNS.prettyhexrep(destination_hash)}{app_str}")

    RNS.Transport.register_announce_handler(count_announces)

    print("Bridge is running. Browser clients can connect to:")
    print(f"  ws://localhost:{args.ws_port}")
    print()
    print("Press Ctrl+C to stop.")
    print()

    # Run until interrupted
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        print("\nShutting down...")
        reticulum.teardown()
        print("Done.")

if __name__ == '__main__':
    main()
