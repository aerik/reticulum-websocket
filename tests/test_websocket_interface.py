"""
Unit tests for Python WebSocketInterface.

Tests both server and client modes independently, and together.
Uses websockets library directly for test clients/servers.

Run: .venv/Scripts/python.exe -m pytest python/test_websocket_interface.py -v
"""

import asyncio
import os
import sys
import time
import threading
import pytest

# Add the python directory to path so we can import the interface
sys.path.insert(0, os.path.join(os.path.dirname(__file__)))

import websockets
import websockets.asyncio.server
import websockets.asyncio.client

# We need RNS for the interface base class
config_dir = os.path.join(os.path.dirname(__file__), '..', '.rns_pytest_config')
os.makedirs(config_dir, exist_ok=True)

# Write minimal config with no interfaces
config_path = os.path.join(config_dir, 'config')
with open(config_path, 'w') as f:
    f.write("[reticulum]\n  share_instance = No\n[logging]\n  loglevel = 2\n[interfaces]\n")

import RNS

# Initialize RNS once for the test session
_reticulum = RNS.Reticulum(configdir=config_dir)

from WebSocketInterface import WebSocketInterface, HEADER_MINSIZE


# --- Helpers ---

def make_server_config(port):
    """Create a config dict for server mode."""
    return {
        "name": f"Test WS Server {port}",
        "mode": "server",
        "listen_ip": "127.0.0.1",
        "listen_port": str(port),
    }


def make_client_config(port):
    """Create a config dict for client mode."""
    return {
        "name": f"Test WS Client {port}",
        "mode": "client",
        "target_host": "127.0.0.1",
        "target_port": str(port),
    }


def make_test_packet(size=30):
    """Create a fake RNS packet (just random bytes >= HEADER_MINSIZE)."""
    return os.urandom(max(size, HEADER_MINSIZE))


class MockOwner:
    """Mock for RNS.Transport — captures inbound calls."""

    def __init__(self):
        self.received = []
        self.event = threading.Event()

    def inbound(self, data, interface):
        self.received.append(bytes(data))
        self.event.set()

    def wait_for_packet(self, timeout=5):
        self.event.wait(timeout)
        self.event.clear()


# --- Tests ---

class TestWebSocketServerMode:
    """Test WebSocketInterface in server mode."""

    def test_server_starts_and_accepts_connections(self):
        """Server should start listening and accept a WebSocket client."""
        owner = MockOwner()
        iface = WebSocketInterface(owner, make_server_config(19001))
        time.sleep(0.5)  # let server start

        assert iface.online is True

        # Connect a raw websocket client
        received = []

        async def client():
            async with websockets.asyncio.client.connect("ws://127.0.0.1:19001") as ws:
                received.append("connected")
                # Send a packet
                packet = make_test_packet()
                await ws.send(packet)
                received.append("sent")
                await asyncio.sleep(0.5)

        asyncio.run(client())

        assert "connected" in received
        assert "sent" in received
        iface.detach()

    def test_server_receives_packets(self):
        """Packets sent by WebSocket client should arrive via process_incoming."""
        owner = MockOwner()
        iface = WebSocketInterface(owner, make_server_config(19002))
        time.sleep(0.5)

        packet = make_test_packet(40)

        async def client():
            async with websockets.asyncio.client.connect("ws://127.0.0.1:19002") as ws:
                await ws.send(packet)
                await asyncio.sleep(0.5)

        asyncio.run(client())
        owner.wait_for_packet(timeout=3)

        assert len(owner.received) >= 1
        assert owner.received[0] == packet
        iface.detach()

    def test_server_sends_to_clients(self):
        """process_outgoing should deliver packets to connected clients."""
        owner = MockOwner()
        iface = WebSocketInterface(owner, make_server_config(19003))
        time.sleep(0.5)

        packet = make_test_packet(50)
        received_by_client = []

        async def client():
            async with websockets.asyncio.client.connect("ws://127.0.0.1:19003") as ws:
                # Give server time to register us
                await asyncio.sleep(0.3)
                # Trigger send from server side
                iface.process_outgoing(packet)
                try:
                    msg = await asyncio.wait_for(ws.recv(), timeout=3)
                    received_by_client.append(bytes(msg))
                except asyncio.TimeoutError:
                    pass

        asyncio.run(client())

        assert len(received_by_client) == 1
        assert received_by_client[0] == packet
        iface.detach()

    def test_server_drops_small_packets(self):
        """Packets smaller than HEADER_MINSIZE should be dropped."""
        owner = MockOwner()
        iface = WebSocketInterface(owner, make_server_config(19004))
        time.sleep(0.5)

        async def client():
            async with websockets.asyncio.client.connect("ws://127.0.0.1:19004") as ws:
                await ws.send(b"\x01\x02\x03")  # too small
                await asyncio.sleep(0.5)

        asyncio.run(client())
        time.sleep(0.5)

        assert len(owner.received) == 0
        iface.detach()

    def test_server_handles_multiple_clients(self):
        """Server should broadcast to all connected clients."""
        owner = MockOwner()
        iface = WebSocketInterface(owner, make_server_config(19005))
        time.sleep(0.5)

        packet = make_test_packet(35)
        results = {"c1": None, "c2": None}

        async def run_clients():
            async with websockets.asyncio.client.connect("ws://127.0.0.1:19005") as ws1:
                async with websockets.asyncio.client.connect("ws://127.0.0.1:19005") as ws2:
                    await asyncio.sleep(0.3)
                    iface.process_outgoing(packet)
                    try:
                        results["c1"] = bytes(await asyncio.wait_for(ws1.recv(), timeout=3))
                        results["c2"] = bytes(await asyncio.wait_for(ws2.recv(), timeout=3))
                    except asyncio.TimeoutError:
                        pass

        asyncio.run(run_clients())

        assert results["c1"] == packet
        assert results["c2"] == packet
        iface.detach()


class TestWebSocketClientMode:
    """Test WebSocketInterface in client mode."""

    def test_client_connects_and_receives(self):
        """Client should connect to a WebSocket server and receive packets."""
        owner = MockOwner()
        received_by_client = []

        # Start a simple WS server
        async def handler(ws):
            await asyncio.sleep(0.3)
            packet = make_test_packet(45)
            await ws.send(packet)
            received_by_client.append(packet)
            await asyncio.sleep(1)

        async def run_server():
            async with websockets.asyncio.server.serve(handler, "127.0.0.1", 19010):
                await asyncio.sleep(3)

        server_thread = threading.Thread(target=lambda: asyncio.run(run_server()), daemon=True)
        server_thread.start()
        time.sleep(0.5)

        iface = WebSocketInterface(owner, make_client_config(19010))
        time.sleep(1)

        owner.wait_for_packet(timeout=3)

        assert iface.online is True
        assert len(owner.received) >= 1
        assert owner.received[0] == received_by_client[0]
        iface.detach()

    def test_client_sends_packets(self):
        """process_outgoing should send packets to the server."""
        owner = MockOwner()
        server_received = []

        async def handler(ws):
            try:
                msg = await asyncio.wait_for(ws.recv(), timeout=5)
                server_received.append(bytes(msg))
            except (asyncio.TimeoutError, Exception):
                pass

        async def run_server():
            async with websockets.asyncio.server.serve(handler, "127.0.0.1", 19011):
                await asyncio.sleep(5)

        server_thread = threading.Thread(target=lambda: asyncio.run(run_server()), daemon=True)
        server_thread.start()
        time.sleep(0.5)

        iface = WebSocketInterface(owner, make_client_config(19011))
        time.sleep(0.5)

        packet = make_test_packet(55)
        iface.process_outgoing(packet)
        time.sleep(1)

        assert len(server_received) >= 1
        assert server_received[0] == packet
        iface.detach()


class TestWebSocketBidirectional:
    """Test server and client WebSocketInterfaces talking to each other."""

    def test_server_to_client_via_rns(self):
        """A server-mode interface and client-mode interface should exchange packets."""
        server_owner = MockOwner()
        client_owner = MockOwner()

        server = WebSocketInterface(server_owner, make_server_config(19020))
        time.sleep(0.5)

        client = WebSocketInterface(client_owner, make_client_config(19020))
        time.sleep(1)

        # Client sends to server
        pkt1 = make_test_packet(30)
        client.process_outgoing(pkt1)
        server_owner.wait_for_packet(timeout=3)
        assert len(server_owner.received) >= 1
        assert server_owner.received[0] == pkt1

        # Server sends to client
        pkt2 = make_test_packet(40)
        server.process_outgoing(pkt2)
        client_owner.wait_for_packet(timeout=3)
        assert len(client_owner.received) >= 1
        assert client_owner.received[0] == pkt2

        server.detach()
        client.detach()

    def test_stats_tracking(self):
        """txb and rxb should be updated."""
        server_owner = MockOwner()
        client_owner = MockOwner()

        server = WebSocketInterface(server_owner, make_server_config(19021))
        time.sleep(0.5)

        client = WebSocketInterface(client_owner, make_client_config(19021))
        time.sleep(1)

        pkt = make_test_packet(50)
        client.process_outgoing(pkt)
        server_owner.wait_for_packet(timeout=3)

        assert client.txb >= len(pkt)
        assert server.rxb >= len(pkt)

        server.detach()
        client.detach()
