#!/usr/bin/env python3
"""Tiny HTTP target for the H10 qualification (issue #2, requirement 10). Run it on ANOTHER machine of the LAN, never on the bench PC.

    python lan_http_target.py --bind 192.168.1.50 --port 8099

It answers /ok, redirects /redirect to /after-redirect, and exposes everything it received at /__log (JSON). It records only: time, method,
path and whether the requests carried a Cookie, an Authorization, a Referer or an Origin header (presence, never values). It serves nothing
else, writes nothing to disk and must be stopped after the campaign. Not part of the bench server; a test fixture only."""
import argparse
import time

from aiohttp import web

LOG = []


def record(request):
    LOG.append({"at": time.time(), "method": request.method, "path": request.path,
                "cookie": "Cookie" in request.headers, "authorization": "Authorization" in request.headers,
                "referer": "Referer" in request.headers, "origin": "Origin" in request.headers})


async def ok(request):
    record(request)
    return web.Response(text="ok", headers={"Cache-Control": "no-store"})


async def after_redirect(request):
    record(request)
    return web.Response(text="reached through a redirect")


async def redirect(request):
    record(request)
    raise web.HTTPFound("/after-redirect")


async def log(request):
    return web.json_response(LOG)


def create_app():
    app = web.Application()
    app.add_routes([web.get("/ok", ok), web.post("/ok", ok), web.get("/redirect", redirect), web.get("/after-redirect", after_redirect), web.get("/__log", log)])
    return app


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--bind", required=True, help="the LAN address to listen on (never 0.0.0.0 by default)")
    parser.add_argument("--port", type=int, default=8099)
    args = parser.parse_args()
    web.run_app(create_app(), host=args.bind, port=args.port, access_log=None)
