"""
Local-only bridge test: WebSocket server + local announces.
No external network needed. Announces its own destination every 3 seconds.
"""
import sys, os, time, threading, shutil

config_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), '.bridge_local_config')
os.makedirs(config_dir, exist_ok=True)
iface_dir = os.path.join(config_dir, 'interfaces')
os.makedirs(iface_dir, exist_ok=True)
shutil.copy2(
    os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', 'WebSocketInterface.py'),
    os.path.join(iface_dir, 'WebSocketInterface.py')
)

config = """[reticulum]
  enable_transport = False
  share_instance = No
[logging]
  loglevel = 5
[interfaces]
  [[Browser WebSocket]]
    type = WebSocketInterface
    enabled = yes
    mode = server
    listen_ip = 127.0.0.1
    listen_port = 8765
"""
with open(os.path.join(config_dir, 'config'), 'w') as f:
    f.write(config)

import RNS
reticulum = RNS.Reticulum(configdir=config_dir)

identity = RNS.Identity()
destination = RNS.Destination(identity, RNS.Destination.IN, RNS.Destination.SINGLE, "demo", "echo")

print(f"READY:{destination.hash.hex()}", flush=True)

# Wait for a client to connect, then announce
time.sleep(5)

# Announce repeatedly
for i in range(10):
    destination.announce(app_data=f"Demo Node #{i}".encode())
    time.sleep(2)

reticulum.teardown()
