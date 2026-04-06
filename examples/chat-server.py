"""
RNS Chat Server with WebSocket Bridge

Runs an echo/chat bot on the RNS network and exposes a WebSocket
server for browser clients. The chat bot accepts links and echoes
messages back with timestamps.

Usage:
    python examples/chat-server.py [--ws-port 8765] [--rns-host rns.beleth.net] [--rns-port 4242]
"""

import sys
import os
import time
import json
import argparse
import shutil
import threading

def main():
    parser = argparse.ArgumentParser(description='RNS Chat Server + WebSocket Bridge')
    parser.add_argument('--ws-port', type=int, default=8765)
    parser.add_argument('--rns-host', default='rns.noderage.org')
    parser.add_argument('--rns-port', type=int, default=4242)
    args = parser.parse_args()

    config_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), '.chat_config')
    os.makedirs(config_dir, exist_ok=True)
    iface_dir = os.path.join(config_dir, 'interfaces')
    os.makedirs(iface_dir, exist_ok=True)
    shutil.copy2(
        os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', 'python', 'WebSocketInterface.py'),
        os.path.join(iface_dir, 'WebSocketInterface.py')
    )

    config = f"""[reticulum]
  enable_transport = True
  share_instance = No
[logging]
  loglevel = 5
[interfaces]
  [[RNS Network 1]]
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

    print("=== RNS Chat Server ===")
    print(f"  RNS Network : {args.rns_host}:{args.rns_port}")
    print(f"  WebSocket   : ws://0.0.0.0:{args.ws_port}")
    print()

    reticulum = RNS.Reticulum(configdir=config_dir)

    # --- Chat Bot ---
    bot_identity = RNS.Identity()
    bot_dest = RNS.Destination(
        bot_identity, RNS.Destination.IN, RNS.Destination.SINGLE,
        "rns_chat", "bot"
    )

    active_links = []

    def on_link(link):
        link.set_link_closed_callback(on_link_closed)
        link.set_packet_callback(on_packet)
        active_links.append(link)
        print(f"[Bot] Link established from {RNS.prettyhexrep(link.hash)}")

        # Send welcome message
        welcome = json.dumps({
            "type": "system",
            "text": "Connected to RNS Chat Bot. Send me a message!",
            "time": time.time(),
        }).encode()
        RNS.Packet(link, welcome).send()

    def on_link_closed(link):
        if link in active_links:
            active_links.remove(link)
        print(f"[Bot] Link closed")

    def on_packet(message, packet):
        try:
            msg = json.loads(message.decode())
            text = msg.get("text", "")
            sender = msg.get("sender", "unknown")
            print(f"[Bot] {sender}: {text}")

            # Echo back
            reply = json.dumps({
                "type": "message",
                "sender": "ChatBot",
                "text": f"Echo: {text}",
                "time": time.time(),
            }).encode()
            RNS.Packet(packet.link, reply).send()
        except Exception as e:
            # If not JSON, just echo raw
            reply = json.dumps({
                "type": "message",
                "sender": "ChatBot",
                "text": f"Echo: {message.decode('utf-8', errors='replace')}",
                "time": time.time(),
            }).encode()
            RNS.Packet(packet.link, reply).send()

    bot_dest.set_link_established_callback(on_link)

    # --- NomadNet-compatible node (serves pages over Links) ---
    node_dest = RNS.Destination(
        bot_identity, RNS.Destination.IN, RNS.Destination.SINGLE,
        "nomadnetwork", "node"
    )

    page_content = """>RNS WebSocket Bridge Node

Welcome to the *Reticulum Network* from your browser!

This page is being served by a Python RNS node over an
encrypted Link established via WebSocket.

>Connection Info

  Identity : {identity}
  Node     : {node_hash}

>Features

  - Browse the live Reticulum network
  - See real announces from nodes worldwide
  - Establish encrypted Links to network nodes
  - Fetch pages from NomadNet nodes

>About

This is *reticulum-node*, a Node.js port of the
Reticulum Network Stack with browser support.
The browser connects via WebSocket to this bridge,
which relays to the global RNS network.
""".format(identity=bot_identity.hexhash, node_hash=node_dest.hexhash)

    def on_node_link(link):
        link.set_resource_strategy(RNS.Link.ACCEPT_NONE)
        link.set_link_closed_callback(lambda l: print("[Node] Link closed"))
        print(f"[Node] Link established: {RNS.prettyhexrep(link.hash)}")
        print(f"[Node]   attached_interface: {link.attached_interface}")
        print(f"[Node]   status: {link.status}")
        print(f"[Node]   has request handlers: {len(node_dest.request_handlers)}")

        # Debug: monitor all incoming packets and resource hash matching
        original_receive = link.receive
        def debug_recv(pkt):
            info = f"ctx=0x{pkt.context:02x} data={len(pkt.data)}b outgoing_res={len(link.outgoing_resources)}"
            if pkt.context == 0x03 and len(link.outgoing_resources) > 0:  # RESOURCE_REQ
                pt = link.decrypt(pkt.data)
                if pt:
                    req_hash = pt[1:33]
                    for res in link.outgoing_resources:
                        info += f" res_hash_match={res.hash == req_hash} res_status={res.status}"
                        info += f" req_hash={req_hash.hex()[:16]}.. res_hash={res.hash.hex()[:16]}.."
            print(f"[Node] link.recv: {info}", flush=True)
            return original_receive(pkt)
        link.receive = debug_recv

    def page_handler(path, data, request_id, remote_identity, requested_at):
        print(f"[Node] Page request: path={path}, request_id={request_id.hex() if request_id else None}")
        return page_content.encode("utf-8")

    node_dest.set_link_established_callback(on_node_link)
    node_dest.register_request_handler(
        "/page/index.mu",
        response_generator=page_handler,
        allow=RNS.Destination.ALLOW_ALL,
        auto_compress=False
    )

    # Announce both the bot and the node
    bot_dest.announce(app_data=b"RNS Chat Bot")
    node_dest.announce(app_data=b"WS Bridge Node")

    print(f"  Bot identity:    {bot_identity.hexhash}")
    print(f"  Bot destination: {bot_dest.hexhash}")
    print(f"  Node dest (NomadNet): {node_dest.hexhash}")
    print()

    # Re-announce every 30 seconds so browser clients see updates quickly
    def announce_loop():
        while True:
            time.sleep(30)
            bot_dest.announce(app_data=b"RNS Chat Bot")
            node_dest.announce(app_data=b"WS Bridge Node")

    threading.Thread(target=announce_loop, daemon=True).start()

    # Track announces for the log
    announce_count = [0]
    def on_announce(dest_hash, identity, app_data):
        announce_count[0] += 1
        app = ""
        if app_data:
            try:
                s = app_data.decode()
                if all(32 <= ord(c) < 127 for c in s):
                    app = f' "{s}"'
            except Exception:
                pass
        if announce_count[0] <= 10 or announce_count[0] % 25 == 0:
            print(f"  Network announce #{announce_count[0]}: {RNS.prettyhexrep(dest_hash)}{app}")

    RNS.Transport.register_announce_handler(on_announce)

    print("Chat server running. Open browser-chat.html and connect.")
    print(f"  ws://localhost:{args.ws_port}")
    print()
    print("Press Ctrl+C to stop.")

    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        print("\nShutting down...")
        reticulum.teardown()

if __name__ == '__main__':
    main()
