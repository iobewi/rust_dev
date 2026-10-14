#!/usr/bin/env python3
"""Fault-injection requests for the rust-mcu-libs OTA A example. TLS is always verified with --ca.
The Bearer token is read from IOBEWI_OTA_TOKEN, never from the command line.

Modes:
  health                       GET /health
  activate ID                  POST /ota/activate for ID (valid token)
  bad-digest IMAGE ID          upload IMAGE announcing a digest that does not match
  truncated IMAGE ID --cut N   announce full length, send N bytes, then close the socket
  nonbootable IMAGE ID         flip one byte in IMAGE, announce the digest of the bytes sent
  no-token IMAGE ID            upload without Authorization
  bad-token IMAGE ID           upload with a wrong bearer token
  activate-no-token ID / activate-bad-token ID
"""
import argparse
import hashlib
import http.client
import os
import ssl
import time
from pathlib import Path


def connect(args):
    context = ssl.create_default_context(cafile=args.ca)
    return http.client.HTTPSConnection(args.host, 443, context=context, timeout=90)


def upload(args, data, digest, token, cut=None):
    c = connect(args)
    c.putrequest("POST", "/ota/upload")
    headers = {"X-Deployment-ID": args.id, "X-Image-SHA256": "sha256:" + digest,
               "Content-Length": str(len(data))}
    if token is not None:
        headers["Authorization"] = "Bearer " + token
    for k, v in headers.items():
        c.putheader(k, v)
    c.endheaders()
    sent = 0
    limit = len(data) if cut is None else cut
    t0 = time.monotonic()
    while sent < limit:
        chunk = data[sent:min(sent + 2048, limit)]
        c.send(chunk)
        sent += len(chunk)
    if cut is not None:
        c.sock.close()
        print(f"sent {sent}/{len(data)} bytes then closed the connection")
        return
    r = c.getresponse()
    body = r.read(2048).decode(errors="replace")
    print(f"upload -> {r.status} {body!r} ({time.monotonic() - t0:.1f}s, {len(data)} bytes)")


def activate(args, token):
    c = connect(args)
    headers = {"X-Deployment-ID": args.id}
    if token is not None:
        headers["Authorization"] = "Bearer " + token
    c.request("POST", "/ota/activate", body=b"", headers=headers)
    r = c.getresponse()
    print(f"activate -> {r.status} {r.read(2048).decode(errors='replace')!r}")


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("mode")
    p.add_argument("rest", nargs="*")
    p.add_argument("--host", default=os.environ.get("BENCH_OTA_HOST", "iobewi-ota.test"))
    p.add_argument("--ca", default=os.environ.get("BENCH_CA"), required="BENCH_CA" not in os.environ)
    p.add_argument("--cut", type=int)
    args = p.parse_args()
    token = os.environ.get("IOBEWI_OTA_TOKEN") or p.error("set IOBEWI_OTA_TOKEN")
    if args.mode == "health":
        c = connect(args); c.request("GET", "/health"); r = c.getresponse()
        print(f"health -> {r.status} {r.read().decode()!r}")
        return
    if args.mode.startswith("activate"):
        args.id = args.rest[0]
        activate(args, {"activate": token, "activate-no-token": None,
                        "activate-bad-token": "x" + token[1:]}[args.mode])
        return
    image, args.id = Path(args.rest[0]), args.rest[1]
    data = bytearray(image.read_bytes())
    digest = hashlib.sha256(data).hexdigest()
    if args.mode == "bad-digest":
        upload(args, data, hashlib.sha256(b"not-this-image").hexdigest(), token)
    elif args.mode == "truncated":
        upload(args, data, digest, token, cut=args.cut or len(data) // 2)
    elif args.mode == "nonbootable":
        data[len(data) // 2] ^= 0xFF
        upload(args, bytes(data), hashlib.sha256(data).hexdigest(), token)
    elif args.mode == "no-token":
        upload(args, data, digest, None)
    elif args.mode == "bad-token":
        upload(args, data, digest, "x" + token[1:])
    else:
        p.error("unknown mode")


if __name__ == "__main__":
    main()
