"""
WebSocket Interface for Reticulum Network Stack.

A dual-protocol transport interface that accepts both WebSocket and plain
TCP connections on a single port. Protocol is auto-detected from the first
bytes of each connection:
  - HTTP upgrade request ("GET ") → WebSocket with HDLC framing
  - Raw binary (0x7E flag etc.) → plain TCP with HDLC framing (like TCPInterface)

This means any existing TCP-based Reticulum node can add browser support
without opening a second port. WebSocket clients and TCP clients coexist
on the same listener.

Two modes:
  - Server mode: accepts incoming connections (like TCPServerInterface)
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


class ClientInterface(Interface):
    """
    Per-client interface spawned by WebSocketInterface for each connection.
    Handles both WebSocket and plain TCP clients identically — the only
    difference is how bytes are sent/received (ws.send vs writer.write).
    """
    DEFAULT_IFAC_SIZE = 16
    BITRATE_GUESS     = 10_000_000
    RECONNECT_WAIT    = 5

    TRANSPORT_WS  = "websocket"
    TRANSPORT_TCP = "tcp"

    def __init__(self, owner, name, transport_type=None,
                 ws=None, reader=None, writer=None, loop=None,
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

        self._transport_type = transport_type or self.TRANSPORT_WS
        self._ws = ws
        self._reader = reader
        self._writer = writer
        self._loop = loop
        self._frame_buffer = bytearray()
        self._initiator = (ws is None and reader is None)

        # Client mode (initiator)
        self.target_host = target_host
        self.target_port = target_port

        if ws is not None or reader is not None:
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
        if self.online:
            try:
                framed = hdlc_frame(data)
                self.txb += len(data)
                if self.parent_interface:
                    self.parent_interface.txb += len(data)

                if self._transport_type == self.TRANSPORT_WS and self._ws:
                    if self._loop and self._loop.is_running():
                        asyncio.run_coroutine_threadsafe(
                            self._ws.send(framed), self._loop
                        )
                    else:
                        websockets.broadcast({self._ws}, framed)

                elif self._transport_type == self.TRANSPORT_TCP and self._writer:
                    if self._loop and self._loop.is_running():
                        self._loop.call_soon_threadsafe(
                            self._writer.write, framed
                        )

            except Exception as e:
                RNS.log(f"[WS] Send error on {self.name}: {e}", RNS.LOG_WARNING)
                self.online = False

    # --- Incoming (called by server's recv loop or client's recv loop) ---

    def _feed_data(self, raw_message):
        """Feed a raw message into the HDLC deframer."""
        self._frame_buffer.extend(raw_message)
        self._process_frames()

    def _process_frames(self):
        """Extract complete HDLC frames from the buffer."""
        while True:
            try:
                start = self._frame_buffer.index(HDLC_FLAG)
            except ValueError:
                self._frame_buffer.clear()
                return

            try:
                end = self._frame_buffer.index(HDLC_FLAG, start + 1)
            except ValueError:
                if start > 0:
                    del self._frame_buffer[:start]
                return

            frame_escaped = bytes(self._frame_buffer[start + 1:end])
            del self._frame_buffer[:end]

            if len(frame_escaped) == 0:
                continue

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

        # For packets from clients that need transport routing,
        # inject our transport_id so Transport.inbound() creates link_table
        # entries (required for proof routing back to the client).
        if (len(raw) >= HEADER_MINSIZE and
            hasattr(RNS, 'Transport') and
            hasattr(RNS.Transport, 'identity') and
            RNS.Transport.identity is not None and
            RNS.Reticulum.transport_enabled()):

            flags = raw[0]
            header_type = (flags & 0x40) >> 6
            packet_type = flags & 0x03

            if header_type == 0 and packet_type != 0x01:  # not ANNOUNCE
                DST_LEN = RNS.Reticulum.TRUNCATED_HASHLENGTH // 8
                dest_hash = raw[2:2+DST_LEN]

                if dest_hash in RNS.Transport.path_table:
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
                    self._transport_type = self.TRANSPORT_WS
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
        if self._writer:
            try:
                self._writer.close()
            except Exception:
                pass
        if self._loop and not self._initiator:
            pass  # Server-spawned, don't stop the loop
        elif self._loop:
            self._loop.call_soon_threadsafe(self._loop.stop)


# Keep old name as alias for backwards compatibility
WebSocketClientInterface = ClientInterface


class WebSocketInterface(Interface):
    """
    Dual-protocol Server Interface — accepts WebSocket and TCP on one port.

    Auto-detects the protocol from the first bytes of each connection:
      - "GET " → WebSocket upgrade → HDLC over WebSocket
      - Anything else → plain TCP → HDLC over TCP (like TCPServerInterface)

    Spawns a ClientInterface per connection with HDLC framing
    and tunnel synthesis.
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
            target_host = c.get("target_host", "127.0.0.1")
            target_port = int(c.get("target_port", 8765))
            self._client_iface = ClientInterface(
                owner, f"WSClient {target_host}:{target_port}",
                target_host=target_host, target_port=target_port,
            )
            self._client_iface.parent_interface = self
            self._client_iface.ifac_size = self.ifac_size
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
        """Start a raw TCP server; auto-detect protocol per connection."""
        try:
            server = await asyncio.start_server(
                self._handle_raw_connection,
                self.listen_ip, self.listen_port,
                reuse_address=True,
            )
            self.online = True
            RNS.log(f"[WS] Server listening on {self.listen_ip}:{self.listen_port}", RNS.LOG_NOTICE)
            async with server:
                await asyncio.Future()  # run forever
        except OSError as e:
            RNS.log(f"[WS] Cannot bind {self.listen_ip}:{self.listen_port}: {e}", RNS.LOG_ERROR)

    async def _handle_raw_connection(self, reader, writer):
        """Peek at first bytes to determine WebSocket vs TCP."""
        addr = writer.get_extra_info('peername')
        try:
            # Peek at the first 4 bytes without consuming them
            peek = await asyncio.wait_for(reader.read(4), timeout=10.0)
            if not peek:
                writer.close()
                return

            if peek[:4] == b"GET ":
                await self._upgrade_to_websocket(reader, writer, peek, addr)
            else:
                await self._handle_tcp_client(reader, writer, peek, addr)

        except asyncio.TimeoutError:
            RNS.log(f"[WS] Connection from {addr} timed out during detection", RNS.LOG_WARNING)
            writer.close()
        except Exception as e:
            RNS.log(f"[WS] Connection error from {addr}: {e}", RNS.LOG_WARNING)
            try:
                writer.close()
            except Exception:
                pass

    async def _upgrade_to_websocket(self, reader, writer, initial_data, addr):
        """Hand off to websockets library for HTTP upgrade, then run HDLC."""
        RNS.log(f"[WS] WebSocket client connected: {addr}", RNS.LOG_INFO)

        # We need to feed the already-read bytes back into the HTTP parser.
        # Use websockets' low-level ServerProtocol to handle the handshake
        # over our existing reader/writer.
        from websockets.asyncio.server import ServerConnection
        from websockets.server import ServerProtocol

        # Create a ServerProtocol (handles HTTP upgrade + framing)
        protocol = ServerProtocol()

        # Feed the bytes we already read
        protocol.receive_data(initial_data)

        # Read more data until we have a complete HTTP request
        while True:
            events = list(protocol.events_received())
            if events:
                break
            # Send any data the protocol wants to send (shouldn't be any yet)
            data_to_send = protocol.data_to_send()
            for chunk in data_to_send:
                writer.write(chunk)
            await writer.drain()
            # Read more
            chunk = await reader.read(4096)
            if not chunk:
                writer.close()
                return
            protocol.receive_data(chunk)

        # Accept the WebSocket handshake
        request = events[0]
        response = protocol.accept(request)

        # Send the HTTP 101 response
        protocol.send_response(response)
        data_to_send = protocol.data_to_send()
        for chunk in data_to_send:
            writer.write(chunk)
        await writer.drain()

        # Now we have a WebSocket connection — wrap it for convenience
        # Use a lightweight adapter so the spawned interface can use ws.send()
        ws_adapter = _AsyncWSAdapter(protocol, reader, writer)

        spawned = self._spawn_interface(
            addr, ClientInterface.TRANSPORT_WS,
            ws=ws_adapter, loop=self._loop,
        )

        try:
            async for message in ws_adapter:
                if isinstance(message, bytes):
                    spawned._feed_data(message)
        except Exception:
            pass
        finally:
            self._despawn_interface(spawned, addr, "WebSocket")

    async def _handle_tcp_client(self, reader, writer, initial_data, addr):
        """Handle a plain TCP client with HDLC framing."""
        RNS.log(f"[WS] TCP client connected: {addr}", RNS.LOG_INFO)

        spawned = self._spawn_interface(
            addr, ClientInterface.TRANSPORT_TCP,
            reader=reader, writer=writer, loop=self._loop,
        )

        # Feed the bytes we already consumed during detection
        spawned._feed_data(initial_data)

        try:
            while True:
                data = await reader.read(4096)
                if not data:
                    break
                spawned._feed_data(data)
        except (ConnectionResetError, asyncio.IncompleteReadError):
            pass
        except Exception as e:
            RNS.log(f"[WS] TCP client error ({addr}): {e}", RNS.LOG_WARNING)
        finally:
            self._despawn_interface(spawned, addr, "TCP")
            writer.close()

    def _spawn_interface(self, addr, transport_type, **kwargs):
        """Create and register a per-client interface."""
        label = "WS" if transport_type == ClientInterface.TRANSPORT_WS else "TCP"
        client_name = f"{label} Client {addr[0]}:{addr[1]}"
        spawned = ClientInterface(
            self.owner, client_name,
            transport_type=transport_type,
            parent_interface=self,
            **kwargs,
        )
        spawned.OUT = True
        spawned.IN = self.IN
        spawned.ifac_size = self.ifac_size
        spawned.ifac_netname = getattr(self, 'ifac_netname', None)
        spawned.ifac_netkey = getattr(self, 'ifac_netkey', None)

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
        RNS.Transport.interfaces.append(spawned)

        if spawned not in RNS.Transport.local_client_interfaces:
            RNS.Transport.local_client_interfaces.append(spawned)

        return spawned

    def _despawn_interface(self, spawned, addr, label):
        """Unregister and clean up a per-client interface."""
        spawned.online = False
        spawned.detach()
        if spawned in self.spawned_interfaces:
            self.spawned_interfaces.remove(spawned)
        if spawned in RNS.Transport.interfaces:
            RNS.Transport.interfaces.remove(spawned)
        if spawned in RNS.Transport.local_client_interfaces:
            RNS.Transport.local_client_interfaces.remove(spawned)
        RNS.log(f"[WS] {label} client disconnected: {addr}", RNS.LOG_INFO)

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


class _AsyncWSAdapter:
    """
    Wraps a websockets ServerProtocol + asyncio reader/writer to provide
    the async iterable and send() interface that ClientInterface expects.
    """
    def __init__(self, protocol, reader, writer):
        self._protocol = protocol
        self._reader = reader
        self._writer = writer
        self._closed = False
        self._message_queue = asyncio.Queue()
        self._recv_task = None

    async def send(self, data):
        """Send a binary WebSocket message."""
        if self._closed:
            return
        self._protocol.send_binary(data)
        for chunk in self._protocol.data_to_send():
            self._writer.write(chunk)
        await self._writer.drain()

    async def _pump(self):
        """Read from the TCP stream and feed into the WebSocket protocol."""
        try:
            while not self._closed:
                data = await self._reader.read(4096)
                if not data:
                    self._closed = True
                    break
                self._protocol.receive_data(data)
                for event in self._protocol.events_received():
                    if hasattr(event, 'data'):
                        # This is a Frame with data (text or binary message)
                        await self._message_queue.put(event.data)
                    elif hasattr(event, 'code'):
                        # Close frame
                        self._closed = True
                        return
        except Exception:
            self._closed = True

    def __aiter__(self):
        return self

    async def __anext__(self):
        # Start the pump task on first iteration
        if self._recv_task is None:
            self._recv_task = asyncio.ensure_future(self._pump())

        # Wait for a message or pump completion
        while True:
            if not self._message_queue.empty():
                return self._message_queue.get_nowait()
            if self._closed and self._message_queue.empty():
                raise StopAsyncIteration
            try:
                msg = await asyncio.wait_for(self._message_queue.get(), timeout=0.5)
                return msg
            except asyncio.TimeoutError:
                if self._closed:
                    raise StopAsyncIteration
                continue

    @property
    def remote_address(self):
        return self._writer.get_extra_info('peername')

    def close(self):
        self._closed = True
        try:
            self._writer.close()
        except Exception:
            pass


# Required by RNS external interface loader
interface_class = WebSocketInterface
