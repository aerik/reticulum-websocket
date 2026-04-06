# Reticulum WebSocket Interface

A WebSocket transport interface for the [Reticulum Network Stack](https://github.com/markqvist/Reticulum). Enables browser-based and other WebSocket clients to join the RNS network through any TCP-capable Reticulum node.

## How It Works

The WebSocket interface operates identically to Reticulum's built-in `TCPServerInterface` — it spawns a per-client interface for each WebSocket connection, with HDLC framing over WebSocket. From the Transport layer's perspective, WebSocket clients are indistinguishable from TCP clients. Link proofs, announce propagation, and path resolution all work correctly through the bridge.

## Installation

1. Install the dependency:
   ```
   pip install websockets
   ```

2. Copy `WebSocketInterface.py` to your Reticulum interfaces directory:
   ```
   cp WebSocketInterface.py ~/.reticulum/interfaces/
   ```

3. Add the interface to your Reticulum config (`~/.reticulum/config`):
   ```ini
   [interfaces]
     [[Browser WebSocket]]
       type = WebSocketInterface
       enabled = yes
       mode = server
       listen_ip = 0.0.0.0
       listen_port = 8765
   ```

4. Restart Reticulum. WebSocket clients can now connect to `ws://your-host:8765`.

## Configuration Options

| Option | Default | Description |
|--------|---------|-------------|
| `mode` | `server` | `server` to accept connections |
| `listen_ip` | `0.0.0.0` | Address to bind |
| `listen_port` | `8765` | Port to listen on |

## Protocol

Uses HDLC framing over WebSocket binary frames — identical to how `TCPInterface` frames packets. This means:

- The Transport layer handles all routing, link management, and proof forwarding
- Each client gets its own interface instance (like `TCPServerInterface` spawning `TCPClientInterface`)
- Transport-ID injection on outbound packets enables correct `link_table` routing back to specific clients

## Examples

The `examples/` directory contains demo servers:

- **`bridge-server.py`** — Minimal WebSocket bridge that connects to an RNS network via TCP and exposes a WebSocket server for browser clients
- **`chat-server.py`** — Chat bot + bridge server with a NomadNet-compatible node destination

```bash
# Start a bridge connecting to the default RNS network
python examples/bridge-server.py

# Start with a specific upstream host
python examples/chat-server.py --rns-host rns.example.com --ws-port 8765
```

## Browser Client

This interface is designed to work with [reticulum-js](https://github.com/aerik/reticulum-js), a JavaScript port of the Reticulum protocol stack that runs in browsers. The JS client connects via `WebSocketClientInterface` using the same HDLC framing.

## Requirements

- Python 3.8+
- `websockets` package (`pip install websockets`)
- Reticulum (`pip install rns`)

## License

MIT License — see [LICENSE](LICENSE).
