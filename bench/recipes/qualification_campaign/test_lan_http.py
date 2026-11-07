"""H10 - HTTP to a LAN target through the agent (issue #2, requirement 10). Needs a REAL target on another LAN machine
(`tools/lan_http_target.py`) and a REAL browser: the contract lives in the page's agent, not in the server alone.

Operator gestures: start the target on the LAN machine; in the page put its origin in "Origines HTTP LAN autorisées" and reconnect the
agent; grant locally; revoke at the end. The recipe reads the target's own log (`/__log`) from the bench container: if the target is not
reachable from there the run is BLOCKED (nothing is inferred).

Criteria, written before any run:
  ok           GET <origin>/ok completes with response "opaque" and verified false; the target saw exactly one /ok without Cookie,
               Authorization, Referer or Origin header (credentials omitted, no-referrer);
  redirect     GET <origin>/redirect does NOT complete (the redirect is refused: `redirect: error`); the target saw /redirect but never /after-redirect;
  not_listed   a URL on another origin than the listed one is refused 403 http_origin_forbidden before any network access (the target log is unchanged);
  https        an https URL is refused 400; a URL with credentials is refused 400 (neither reaches the network);
  PASS         all cases as specified, the grant revoked locally;  FAIL  any other result;  BLOCKED  no consent / no origin / target unreachable /
               the origin is not in the device's http_origins / grant not observed.
The result of a request is OPAQUE: the bench never verifies the response (no-cors); this recipe says nothing about what the target returned.
Hardware status: NOT executed.
"""
import json
import time
import urllib.error
import urllib.request
from urllib.parse import urlsplit

import pytest

from iobewi_bench.client import BenchError, OperationError
from iobewi_bench.qualification import expect_http_refusal

from .harness import Blocked, Outcome, make_campaign, operator_gesture, recorded, wait_until
from .readonly import record_transport, require_consent, start_or_block
from .session import build_manifest

pytestmark = pytest.mark.bench
ASK_GRANT, MIN_GRANT = 300, 120


def target_log(origin):
    try:
        with urllib.request.urlopen(origin + "/__log", timeout=5) as response:
            return json.loads(response.read())
    except (urllib.error.URLError, OSError, ValueError) as error:
        raise Blocked(f"the target log {origin}/__log is not reachable from the bench container: {type(error).__name__}") from error


def test_h10_lan_http_contract(bench_client, qual_session, pytestconfig):
    cfg = qual_session
    require_consent(cfg, "H10")
    origin = cfg["lan_http"].get("origin")
    if not origin:
        pytest.skip("BLOCKED: no lan_http.origin in the session (a real LAN target is required)")
    other = f"http://{urlsplit(origin).hostname}:{urlsplit(origin).port + 1}"
    manifest = build_manifest(cfg, "H10", regions=[], steps=["Origin listed in the page and agent reconnected", f"GRANT {ASK_GRANT} s locally", "REVOKE the grant locally"])
    case, out = make_campaign(bench_client, cfg, "H10", __file__, pytestconfig, manifest=manifest), Outcome()
    with recorded(case, "H10", out, preconditions=f"{cfg['chip']} declared {cfg['transport']}, target {origin}",
                  limits="Opaque (no-cors) requests to ONE LAN target; the response is never verified; one browser; no other origin or network is opened; one run"):
        start_or_block(case)
        record_transport(case, bench_client, cfg, out)
        if origin not in (bench_client.device(cfg["board_id"]).get("http_origins") or []):
            raise Blocked(f"{origin} is not in the device's http_origins: list it in the page and reconnect the agent")
        before = target_log(origin)
        board = case.reserve(ttl=600)
        operator_gesture(case, f"GRANT {ASK_GRANT} s locally in the page", timeout=cfg["operator_timeout_s"])
        try:
            case.client.wait_device(cfg["board_id"], timeout=cfg["operator_timeout_s"], grant_seconds=MIN_GRANT, recovered=True)
        except TimeoutError as error:
            raise Blocked("Local grant not observed") from error
        cases = []
        result = board.command("http.request", {"url": origin + "/ok", "method": "GET", "timeout_ms": 10000}, timeout=60)
        cases.append({"name": "ok", "state": "completed", "result": {k: result.get(k) for k in ("response", "verified")}})
        try:
            board.command("http.request", {"url": origin + "/redirect", "method": "GET", "timeout_ms": 10000}, timeout=60)
            cases.append({"name": "redirect", "state": "completed"})
        except OperationError as error:
            cases.append({"name": "redirect", "state": error.operation.get("state"), "error_code": (error.operation.get("error") or {}).get("code")})
        for name, url, status, code in (("not_listed", other + "/ok", 403, "http_origin_forbidden"), ("https", origin.replace("http://", "https://") + "/ok", 400, "invalid_request"),
                                        ("credentials", origin.replace("http://", "http://user:secret@") + "/ok", 400, "invalid_request")):
            expect_http_refusal(lambda: board.command("http.request", {"url": url, "method": "GET"}), status=status, code=code)
            cases.append({"name": name, "refused": [status, code]})
        time.sleep(1)
        after = target_log(origin)[len(before):]
        case.write("lan-observations.json", {"origin_host_port": urlsplit(origin).netloc, "cases": cases, "target_hits": after})
        paths = [hit["path"] for hit in after]
        assert paths.count("/ok") == 1 and paths.count("/redirect") == 1 and "/after-redirect" not in paths and len(paths) == 2, f"target saw {paths}"
        assert not any(hit[h] for hit in after for h in ("cookie", "authorization", "referer", "origin")), "credentials / referrer / origin were sent"
        assert cases[0]["result"] == {"response": "opaque", "verified": False}, f"ok case result {cases[0]['result']}"
        assert cases[1]["state"] in {"failed", "indeterminate"}, "the redirect was followed or completed"
        out.evidence.append("ok: opaque and unverified; one request without Cookie/Authorization/Referer/Origin")
        out.evidence.append("redirect: refused, the target never saw /after-redirect")
        out.evidence.append("not_listed refused 403 http_origin_forbidden; https and credentials refused 400; none reached the network (target log unchanged)")
        operator_gesture(case, "REVOKE the grant locally in the page", timeout=cfg["operator_timeout_s"])
        wait_until(lambda: bench_client.device(cfg["board_id"])["privileged_seconds"] == 0, timeout=cfg["operator_timeout_s"], what="local grant revocation")
