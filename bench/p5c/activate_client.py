#!/usr/bin/env python3
"""ONE POST /ota/activate (empty body), TLS verified against the bench CA with the real server name; the board's address is
pinned by patching name resolution only. Refuses to run twice (marker file). The token comes from IOBEWI_OTA_TOKEN and is never printed.

  IOBEWI_OTA_TOKEN=... activate_client.py CA.pem DEPLOYMENT_ID ENVELOPE_FILE MARKER_FILE [--ip ADDRESS]
"""
import http.client
import os
import socket
import ssl
import sys
import time

HOST = "iobewi-ota.test"
args = [x for x in sys.argv[1:] if not x.startswith("--")]
ip = sys.argv[sys.argv.index("--ip") + 1] if "--ip" in sys.argv else os.environ.get("P5_IP", "")
if len(args) < 4 or not ip:
    sys.exit(__doc__)
ca, dep, envfile, marker = args[:4]
if ip in args:
    args.remove(ip)
if os.path.exists(marker):
    sys.exit("already sent once: refusing to repeat (" + marker + ")")
open(marker, "w").write(time.strftime("%FT%T") + "\n")
_g = socket.getaddrinfo
socket.getaddrinfo = lambda h, *a, **k: _g(ip if h == HOST else h, *a, **k)
ctx = ssl.create_default_context(cafile=ca)
c = http.client.HTTPSConnection(HOST, 443, context=ctx, timeout=60)
t0 = time.time()
c.putrequest("POST", "/ota/activate")
for k, v in {"Authorization": "Bearer " + os.environ["IOBEWI_OTA_TOKEN"], "X-Deployment-ID": dep, "Content-Length": "0",
             "X-Image-Manifest": open(envfile).read().strip()}.items():
    c.putheader(k, v)
c.endheaders()
tls = c.sock.version()
r = c.getresponse()
print("status", r.status, repr(r.read(256).decode(errors="replace")), "elapsed %.1fs" % (time.time() - t0), "tls", tls)
