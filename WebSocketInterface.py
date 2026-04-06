"""
WebSocket Interface for Reticulum Network Stack.

Behaves identically to TCPInterface but over WebSocket transport.
Uses HDLC framing, tunnel synthesis, and per-client interface spawning
— exactly like TCPServerInterface/TCPClientInterface.

Browsers and Node.js clients connect via WebSocket and become full
transport peers on the RNS network.

Two modes:
  - Server mode: accepts incoming WebSocket connections (like TCPServerInterface)
  - Client mode: connects to a WebSocket server (like TCPClientInterface)

Installation:
  1. Copy to ~/.reticulum/interfaces/WebSocketInterface.py
  2. pip install websockets
  3. Add to ~/.reticulum/config:

    [[WebSocket Server]]
      type = WebSocketInterface
      enabled = yes
      mode = server
      listen_ip = 0.0.0.0
      listen_port = 8765
"""

import struct
import threading
import asyncio
import time
import RNS

try:
    import websockets
    import websockets.asyncio.server
    import websockets.asyncio.client
except ImportError:
    raise ImportError("pip install websockets")

if 'Interface' not in dir():
    from RNS.Interfaces.Interface import Interface

# HDLC framing — identical to TCPInterface
HDLC_FLAG     = 0x7E
HDLC_ESC      = 0x7D
HDLC_ESC_MASK = 0x20
HEADER_MINSIZE = 2 + 1 + (128 // 8)  # 19 bytes


def hdlc_escape(data):
    data = data.replace(bytes([HDLC_ESC]), bytes([HDLC_ESC, HDLC_ESC ^ HDLC_ESC_MASK]))
    data = data.replace(bytes([HDLC_FLAG]), bytes([HDLC_ESC, HDLC_FLAG ^ HDLC_ESC_MASK]))
    return data


def hdlc_frame(data):
    return bytes([HDLC_FLAG]) + hdlc_escape(data) + bytes([HDLC_FLAG])


class WebSocketClientInterface(Interface):
    """
    Per-client interface spawned by WebSocketServerInterface.
    Mirrors TCPClientInterface behaviour: HDLC framing, tunnel synthesis.
    Also usable standalone as a WebSocket client (like TCPClientInterface).
    """
    DEFAULT_IFAC_SIZE = 16
    BITRATE_GUESS     = 10_000_000
    RECONNECT_WAIT    = 5

    def __init__(self, owner, name, ws=None, loop=None,
                 target_host=None, target_port=None,
                 parent_interface=None):
        super().__init__()
        self.owner = owner
        self.name = name
        self.IN = True
        self.OUT = True
        self.FWD = False
        self.RPT = False
        self.online = False
        self.bitrate = self.BITRATE_GUESS
        self.HW_MTU = 262144
        self.mode = Interface.MODE_FULL
        self.ifac_size = self.DEFAULT_IFAC_SIZE
        self.announce_cap = RNS.Reticulum.ANNOUNCE_CAP
        self.parent_interface = parent_interface

        self._ws = ws
        self._loop = loop
        self._frame_buffer = bytearray()
        self._initiator = (ws is None)  # True if we initiate the connection

        # Client mode (initiator)
        self.target_host = target_host
        self.target_port = target_port

        if ws is not None:
            # Spawned from server — already connected
            self.online = True
            self.wants_tunnel = True
        elif target_host:
            # Client mode — start connection
            self._loop = asyncio.new_event_loop()
            thread = threading.Thread(target=self._run_client, daemon=True)
            thread.start()

    # --- Outgoing ---

    def process_outgoing(self, data):
        if self.online and self._ws:
            try:
                framed = hdlc_frame(data)
                self.txb += len(data)
                if self.parent_interface:
                    self.parent_interface.txb += len(data)

                if self._loop and self._loop.is_running():
                    asyncio.run_coroutine_threadsafe(
                        self._ws.send(framed), self._loop
                    )
                else:
                    # Fallback: try websockets.broadcast for server-spawned
                    websockets.broadcast({self._ws}, framed)

            except Exception as e:
                RNS.log(f"[WS] Send error on {self.name}: {e}", RNS.LOG_WARNING)
                self.online = False

    # --- Incoming (called by server's recv loop or client's recv loop) ---

    def _feed_data(self, raw_message):
        """Feed a raw WebSocket message into the HDLC deframer."""
        self._frame_buffer.extend(raw_message)
        self._process_frames()

    def _process_frames(self):
        """Extract complete HDLC frames from the buffer."""
        while True:
            # Find first FLAG
            try:
                start = self._frame_buffer.index(HDLC_FLAG)
            except ValueError:
                self._frame_buffer.clear()
                return

            # Find next FLAG after start
            try:
                end = self._frame_buffer.index(HDLC_FLAG, start + 1)
            except ValueError:
                # Keep from start onwards, wait for more data
                if start > 0:
                    del self._frame_buffer[:start]
                return

            # Extract frame between flags
            frame_escaped = bytes(self._frame_buffer[start + 1:end])
            del self._frame_buffer[:end]

            if len(frame_escaped) == 0:
                continue

            # Unescape
            frame = frame_escaped.replace(
                bytes([HDLC_ESC, HDLC_FLAG ^ HDLC_ESC_MASK]), bytes([HDLC_FLAG])
            ).replace(
                bytes([HDLC_ESC, HDLC_ESC ^ HDLC_ESC_MASK]), bytes([HDLC_ESC])
            )

            if len(frame) >= HEADER_MINSIZE:
                self.process_incoming(frame)

    def process_incoming(self, data):
        self.rxb += len(data)
        if self.parent_interface:
            self.parent_interface.rxb += len(data)
        if not self.owner:
            return

        raw = bytes(data)

        # For packets from WebSocket clients that need transport routing,
        # inject our transport_id so Transport.inbound() creates link_table
        # entries (required for proof routing back to the client).
        # This mirrors what Transport does for local shared-instance clients
        # but without the hops==0 restriction.
        if (len(raw) >= HEADER_MINSIZE and
            hasattr(RNS, 'Transport') and
            hasattr(RNS.Transport, 'identity') and
            RNS.Transport.identity is not None and
            RNS.Reticulum.transport_enabled()):

            flags = raw[0]
            header_type = (flags & 0x40) >> 6
            packet_type = flags & 0x03

            # HEADER_1 packets that need forwarding: inject transport_id
            # to make them go through the inbound() forwarding path
            if header_type == 0 and packet_type != 0x01:  # not ANNOUNCE
                DST_LEN = RNS.Reticulum.TRUNCATED_HASHLENGTH // 8
                dest_hash = raw[2:2+DST_LEN]

                if dest_hash in RNS.Transport.path_table:
                    # Convert to HEADER_2 with our transport_id
                    new_flags = (1 << 6) | (1 << 4) | (flags & 0x0F)
                    new_raw = struct.pack("!B", new_flags)
                    new_raw += raw[1:2]  # hops
                    new_raw += RNS.Transport.identity.hash  # transport_id
                    new_raw += raw[2:]   # rest of packet
                    self.owner.inbound(new_raw, self)
                    return

        self.owner.inbound(raw, self)

    # --- Client mode ---

    def _run_client(self):
        asyncio.set_event_loop(self._loop)
        self._loop.run_until_complete(self._connect_loop())

    async def _connect_loop(self):
        url = f"ws://{self.target_host}:{self.target_port}"
        while True:
            try:
                async with websockets.asyncio.client.connect(url) as ws:
                    self._ws = ws
                    self.online = True
                    self.wants_tunnel = True
                    RNS.log(f"[WS] Connected to {url}", RNS.LOG_NOTICE)
                    try:
                        async for message in ws:
                            if isinstance(message, bytes):
                                self._feed_data(message)
                    except websockets.exceptions.ConnectionClosed:
                        pass
            except Exception as e:
                RNS.log(f"[WS] Connection to {url} failed: {e}", RNS.LOG_WARNING)

            self.online = False
            self._ws = None
            RNS.log(f"[WS] Reconnecting to {url} in {self.RECONNECT_WAIT}s...", RNS.LOG_INFO)
            await asyncio.sleep(self.RECONNECT_WAIT)

    def detach(self):
        self.online = False
        if self._loop and not self._initiator:
            pass  # Server-spawned, don't stop the loop
        elif self._loop:
            self._loop.call_soon_threadsafe(self._loop.stop)


class WebSocketInterface(Interface):
    """
    WebSocket Server Interface — mirrors TCPServerInterface.
    Spawns a WebSocketClientInterface per connection, each with
    HDLC framing and tunnel synthesis.
    """
    DEFAULT_IFAC_SIZE = 16
    BITRATE_GUESS     = 10_000_000

    def __init__(self, owner, configuration):
        super().__init__()
        c = Interface.get_config_obj(configuration)
        name = c.get("name", "WebSocketInterface")

        self.owner = owner
        self.name = name
        self.IN = True
        self.OUT = False  # Server itself doesn't send — spawned interfaces do
        self.online = False
        self.bitrate = self.BITRATE_GUESS
        self.HW_MTU = 262144
        self.mode = Interface.MODE_FULL
        self.ifac_size = self.DEFAULT_IFAC_SIZE
        self.announce_cap = RNS.Reticulum.ANNOUNCE_CAP

        self.spawned_interfaces = []
        self._loop = None
        self._thread = None

        ws_mode = c.get("mode", "server")

        if ws_mode == "server":
            self.listen_ip = c.get("listen_ip", "0.0.0.0")
            self.listen_port = int(c.get("listen_port", 8765))
            self._start_server()

        elif ws_mode == "client":
            # For client mode, we create a single WebSocketClientInterface
            target_host = c.get("target_host", "127.0.0.1")
            target_port = int(c.get("target_port", 8765))
            self._client_iface = WebSocketClientInterface(
                owner, f"WSClient {target_host}:{target_port}",
                target_host=target_host, target_port=target_port,
            )
            self._client_iface.parent_interface = self
            self._client_iface.ifac_size = self.ifac_size
            # Register the client interface with Transport
            if hasattr(owner, 'register_interface'):
                owner.register_interface(self._client_iface)
            self.online = True

    @property
    def clients(self):
        return len(self.spawned_interfaces)

    def _start_server(self):
        self._loop = asyncio.new_event_loop()
        self._thread = threading.Thread(target=self._run_server, daemon=True)
        self._thread.start()

    def _run_server(self):
        asyncio.set_event_loop(self._loop)
        self._loop.run_until_complete(self._serve())

    async def _serve(self):
        try:
            async with websockets.asyncio.server.serve(
                self._handle_connection, self.listen_ip, self.listen_port,
                reuse_address=True,
            ):
                self.online = True
                RNS.log(f"[WS] Server listening on {self.listen_ip}:{self.listen_port}", RNS.LOG_NOTICE)
                await asyncio.Future()
        except OSError as e:
            RNS.log(f"[WS] Cannot bind {self.listen_ip}:{self.listen_port}: {e}", RNS.LOG_ERROR)

    async def _handle_connection(self, ws):
        addr = ws.remote_address
        RNS.log(f"[WS] Client connected: {addr}", RNS.LOG_INFO)

        # Spawn a per-client interface — just like TCPServerInterface does
        client_name = f"WS Client {addr[0]}:{addr[1]}"
        spawned = WebSocketClientInterface(
            self.owner, client_name,
            ws=ws, loop=self._loop,
            parent_interface=self,
        )
        spawned.OUT = self.OUT or True  # Spawned clients can send
        spawned.IN = self.IN
        spawned.ifac_size = self.ifac_size
        spawned.ifac_netname = getattr(self, 'ifac_netname', None)
        spawned.ifac_netkey = getattr(self, 'ifac_netkey', None)

        # Copy IFAC config if present
        if spawned.ifac_netname or spawned.ifac_netkey:
            ifac_origin = b""
            if spawned.ifac_netname:
                ifac_origin += RNS.Identity.full_hash(spawned.ifac_netname.encode("utf-8"))
            if spawned.ifac_netkey:
                ifac_origin += RNS.Identity.full_hash(spawned.ifac_netkey.encode("utf-8"))
            ifac_origin_hash = RNS.Identity.full_hash(ifac_origin)
            spawned.ifac_key = RNS.Cryptography.hkdf(
                length=64, derive_from=ifac_origin_hash,
                salt=RNS.Reticulum.IFAC_SALT, context=None,
            )
            spawned.ifac_identity = RNS.Identity.from_bytes(spawned.ifac_key)

        self.spawned_interfaces.append(spawned)

        # Register with Transport so it can route through this interface
        RNS.Transport.interfaces.append(spawned)

        # Register as local client so Transport forwards announces to browser
        if spawned not in RNS.Transport.local_client_interfaces:
            RNS.Transport.local_client_interfaces.append(spawned)

        try:
            async for message in ws:
                if isinstance(message, bytes):
                    spawned._feed_data(message)
        except websockets.exceptions.ConnectionClosed:
            pass
        except Exception as e:
            RNS.log(f"[WS] Client error ({addr}): {e}", RNS.LOG_WARNING)
        finally:
            spawned.online = False
            spawned.detach()
            if spawned in self.spawned_interfaces:
                self.spawned_interfaces.remove(spawned)
            if spawned in RNS.Transport.interfaces:
                RNS.Transport.interfaces.remove(spawned)
            if spawned in RNS.Transport.local_client_interfaces:
                RNS.Transport.local_client_interfaces.remove(spawned)
            RNS.log(f"[WS] Client disconnected: {addr}", RNS.LOG_INFO)

    def process_outgoing(self, data):
        pass  # Server interface doesn't send directly

    def detach(self):
        self.online = False
        for iface in list(self.spawned_interfaces):
            iface.detach()
        if self._loop:
            self._loop.call_soon_threadsafe(self._loop.stop)
        if self._thread:
            self._thread.join(timeout=2)


# Required by RNS external interface loader
interface_class = WebSocketInterface
