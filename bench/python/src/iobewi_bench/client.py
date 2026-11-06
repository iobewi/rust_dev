"""Synchronous API v1 client. No mutation retries, serial access or local authority."""
from __future__ import annotations

import base64
from dataclasses import dataclass
import hashlib
import json
import math
from pathlib import Path
import time
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlsplit
from urllib.request import build_opener, HTTPRedirectHandler, Request

TERMINAL = {"completed", "failed", "expired", "cancelled", "indeterminate"}


class BenchError(RuntimeError):
    """A bounded diagnostic, without server messages or credentials."""


class BenchHTTPError(BenchError):
    def __init__(self, status, code):
        self.status, self.code = status, code
        super().__init__(f"HTTP {status}: {code}")


class OperationError(BenchError):
    def __init__(self, operation):
        self.operation = operation
        super().__init__(f"Operation {operation['id']} {operation['state']}: "
                         f"{(operation.get('error') or {}).get('code', 'unknown')}")


class PendingOperation(BenchError):
    """Hardware outcome is not settled; do not revoke its reservation automatically."""


class ContinuityError(BenchError):
    """A positive UART assertion cannot cross lost events or a different device session."""


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def _duration(value, low, high):
    if isinstance(value, bool) or not math.isfinite(value) or not low <= value <= high:
        raise ValueError(f"Duration must be between {low} and {high} seconds")
    return value


@dataclass
class Cursor:
    epoch: str
    generation: str
    after: int
    index: int = -1          # n-th cursor taken by its Board (see cursors.jsonl); -1 = not taken by Board.cursor()
    submitted: int = -1      # commands already submitted by that Board when the cursor was taken


class BenchClient:
    def __init__(self, url, token, *, request_timeout=10, poll_interval=0.05):
        parsed = urlsplit(url)
        if (parsed.scheme not in {"http", "https"} or not parsed.hostname or
                parsed.username or parsed.password or parsed.query or parsed.fragment or
                parsed.path not in {"", "/"}):
            raise ValueError("Use a bench HTTP(S) origin without credentials, query or path")
        if not token or any(c in token for c in "\r\n"):
            raise ValueError("A client Bearer token is required")
        self.url, self._token = url.rstrip("/"), token
        self.request_timeout = _duration(request_timeout, 0.01, 60)
        self.poll_interval = _duration(poll_interval, 0.001, 10)
        self._http = build_opener(_NoRedirect())

    def _request(self, method, path, body=None, *, binary=False):
        headers = {"Authorization": "Bearer " + self._token}
        data = None
        if body is not None:
            if isinstance(body, bytes):
                data = body
                headers.update({"Content-Type": "application/octet-stream", "X-SHA256": hashlib.sha256(body).hexdigest()})
            else:
                data = json.dumps(body).encode()
                headers["Content-Type"] = "application/json"
        request = Request(self.url + path, data=data, headers=headers, method=method)
        try:
            with self._http.open(request, timeout=self.request_timeout) as response:
                payload = response.read()
                return payload if binary else json.loads(payload)
        except HTTPError as error:
            code = "http_error"
            try:
                code = json.loads(error.read(65536))["error"]["code"]
            except (ValueError, KeyError, TypeError):
                pass
            raise BenchHTTPError(error.code, code) from None
        except (URLError, TimeoutError, OSError, ValueError) as error:
            raise BenchError(f"Transport or response failure ({type(error).__name__}); request not retried") from None

    def identity(self):
        return self._request("GET", "/api/v1/identity")

    def health(self):
        return self._request("GET", "/health")

    def devices(self):
        return self._request("GET", "/api/v1/devices")["devices"]

    def device(self, device_id):
        for device in self.devices():
            if device["id"] == device_id:
                return device
        raise BenchError("Device not found")

    def reserve(self, device_id, *, ttl=900, allow_destructive=False, output_dir=None):
        _duration(ttl, 1, 3600)
        if type(allow_destructive) is not bool:
            raise ValueError("Destructive consent must be a boolean")
        health = self.health()
        device = self.device(device_id)
        if not device["connected"]:
            raise BenchError("Device is disconnected")
        lease = self._request("POST", "/api/v1/leases", {"device": device_id, "ttl": ttl})
        board = Board(self, device, health["epoch"], lease["id"], allow_destructive, output_dir)
        try:
            board._check_target()
        except BaseException:
            board.close()
            raise
        return board

    def operation(self, operation_id):
        """Inspect a stable operation ID; never replay or reconcile it."""
        return self._request("GET", "/api/v1/operations/" + quote(operation_id, safe=""))

    def artifacts(self):
        return self._request("GET", "/api/v1/artifacts")

    def delete_artifact(self, artifact_id):
        return self._request("DELETE", "/api/v1/artifacts/" + quote(artifact_id, safe=""))

    def upload(self, source):
        """Upload once, verify metadata; source is a path or a binary buffer."""
        limit = 16 * 1024 * 1024
        if isinstance(source, (str, Path)):
            with Path(source).open("rb") as stream:
                data = stream.read(limit + 1)
        elif isinstance(source, (bytes, bytearray, memoryview)):
            if len(source) > limit:
                raise ValueError("Artifact must contain 1..16777216 bytes")
            data = bytes(source)
        else:
            raise TypeError("Expected a file path or binary buffer")
        if not 0 < len(data) <= limit:
            raise ValueError("Artifact must contain 1..16777216 bytes")
        if len(data) > self.artifacts()["storage"]["available"]:
            raise BenchHTTPError(503, "capacity")
        artifact = self._request("POST", "/api/v1/artifacts", data)
        if (not isinstance(artifact, dict) or not isinstance(artifact.get("id"), str) or
                not 1 <= len(artifact["id"]) <= 256 or type(artifact.get("size")) is not int or artifact.get("size") != len(data) or
                artifact.get("sha256") != hashlib.sha256(data).hexdigest()):
            raise BenchError("Upload response size or SHA-256 mismatch; request not retried")
        return artifact

    def wait_device(self, device_id, *, timeout=120, grant_seconds=0, recovered=False):
        """Observe only. Never create a grant, recover, reserve or replay commands."""
        _duration(timeout, 0.01, 3600)
        _duration(grant_seconds, 0, 14400)
        deadline = time.monotonic() + timeout
        while True:
            state = next((d for d in self.devices() if d["id"] == device_id), None)
            if (state is not None and state["connected"] and state["privileged_seconds"] >= grant_seconds and
                    (not recovered or not state["quarantined"])):
                return state
            if time.monotonic() >= deadline:
                raise TimeoutError("Operator/device preconditions not observed")
            time.sleep(self.poll_interval)

    def download(self, artifact):
        data = self._request("GET", "/api/v1/artifacts/" + quote(artifact["id"], safe=""), binary=True)
        if len(data) != artifact["size"] or hashlib.sha256(data).hexdigest() != artifact["sha256"]:
            raise BenchError("Artifact size or SHA-256 mismatch")
        return data


class Board:
    def __init__(self, client, device, epoch, lease_id, allow_destructive, output_dir):
        self.client, self.device_id = client, device["id"]
        self.generation, self.epoch, self.lease_id = device["generation"], epoch, lease_id
        self.allow_destructive, self.output_dir = allow_destructive, Path(output_dir) if output_dir else None
        self.pending = None
        self.uncertain = False
        self.closed = False
        self.last_operation = None
        self.manifest = None
        self._dumps = 0
        self._last_record = None
        self._submissions = 0                      # commands submitted by this Board (commands.jsonl sequence)
        self._current_submission = None
        self._cursors = 0

    def _append(self, name, row):
        """Raw execution evidence of the CLIENT (hashed with the sources): arguments, cursors, UART matches. Never UART content."""
        if self.output_dir:
            self.output_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
            with (self.output_dir / name).open("a") as file:
                file.write(json.dumps(row, sort_keys=True) + "\n")

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        self.close()

    def _check_target(self):
        if self.closed:
            raise BenchError("Reservation is closed")
        if self.client.health()["epoch"] != self.epoch:
            raise ContinuityError("Bench epoch changed; start a new test explicitly")
        device = self.client.device(self.device_id)
        if not device["connected"] or device["generation"] != self.generation:
            raise ContinuityError("Device disconnected or generation changed; no automatic resume")
        if self.manifest is not None:
            self.manifest.check_device(device)
        return device

    @staticmethod
    def _settled(op):
        return (op["state"] in TERMINAL and not op.get("unsettled") and
                (not op.get("requires_settle") or op.get("settled")))

    def _record(self, op):
        self.last_operation = op
        if self.output_dir:
            self.output_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
            safe = {k: op.get(k) for k in ("id", "device", "generation", "command", "state", "settled", "unsettled")}
            safe["submit_seq"] = self._current_submission
            safe["error_code"] = (op.get("error") or {}).get("code")
            safe["http_status"] = (op.get("error") or {}).get("http_status")
            record = json.dumps(safe)
            if record != self._last_record:
                with (self.output_dir / "operations.jsonl").open("a") as file:
                    file.write(record + "\n")
                self._last_record = record

    def command(self, name, args=None, *, timeout=120, start_timeout=30, allow_destructive=False):
        _duration(timeout, 0.1, 600)
        _duration(start_timeout, 0.1, 120)
        if self.pending or self.uncertain:
            raise PendingOperation("Previous outcome is unresolved; no new command submitted")
        self._check_target()
        if name in {"flash.write", "flash.erase"} and not (self.allow_destructive is True and allow_destructive is True):
            raise BenchError("Destructive command needs run-level opt-in and explicit per-command consent")
        if self.manifest is not None:
            self.manifest.check_command(self, name, args or {})
        # Even a lost POST response may have started hardware. Never resend or release blindly.
        self.uncertain = True
        self._current_submission = self._submissions
        self._submissions += 1
        # The submitted arguments are recorded BEFORE the POST: a lost response still leaves what was asked (no secrets: artifact ids, hashes).
        self._append("commands.jsonl", {"seq": self._current_submission, "command": name, "args": args or {},
                                        "args_sha256": hashlib.sha256(json.dumps(args or {}, sort_keys=True, separators=(",", ":")).encode()).hexdigest(),
                                        "allow_destructive": allow_destructive is True, "generation": self.generation, "epoch": self.epoch})
        try:
            op = self.client._request("POST", "/api/v1/operations", {
                "lease": self.lease_id, "command": name, "args": args or {},
                "timeout": timeout, "ttl": start_timeout, "allow_destructive": allow_destructive,
            })
        except BenchHTTPError as error:
            if 400 <= error.status < 500:
                self.uncertain = False  # A definitive refusal did not enqueue hardware work.
            raise
        self.pending, self.uncertain = op["id"], False
        deadline = time.monotonic() + start_timeout + timeout + self.client.request_timeout
        while True:
            self._record(op)
            if op["state"] == "indeterminate":
                raise PendingOperation(f"Operation {op['id']} has an indeterminate hardware outcome")
            if self._settled(op):
                self.pending = None
                self._check_target()
                if op["state"] != "completed":
                    raise OperationError(op)
                return op.get("result")
            if op["state"] == "indeterminate" or time.monotonic() >= deadline:
                raise PendingOperation(f"Operation {op['id']} is not settled; reservation retained")
            time.sleep(self.client.poll_interval)
            self._check_target()
            op = self.client._request("GET", "/api/v1/operations/" + quote(self.pending, safe=""))

    def identify(self):
        return self.command("identify")

    def partitions(self, *, address=0x8000):
        return self.command("partitions", {"address": address})["partitions"]

    def read_flash(self, *, address, size, timeout=600, return_console=True):
        if type(return_console) is not bool:
            raise ValueError("return_console must be a boolean")
        result = self.command("flash.read", {"address": address, "size": size,
                                             "return_console": return_console}, timeout=timeout)
        data = self.client.download(result["artifact"])
        if len(data) != size:
            raise BenchError("Flash read size differs from the requested region")
        if self.output_dir:
            self._dumps += 1
            path = self.output_dir / f"flash-{self._dumps:03d}-{address:08x}.bin"
            path.write_bytes(data)
            path.with_suffix(".json").write_text(json.dumps({
                "address": address, "size": size, "sha256": hashlib.sha256(data).hexdigest(),
                "artifact": result["artifact"]["id"], "operation": self.last_operation["id"],
            }, indent=2))
        return data

    def console(self):
        return self.command("console")

    def renew(self, *, ttl=900):
        self._check_target()
        return self.client._request("PATCH", "/api/v1/leases/" + quote(self.lease_id, safe=""), {"ttl": _duration(ttl, 1, 3600)})

    def close(self):
        if self.closed:
            return
        if self.uncertain:
            raise PendingOperation(f"Submission outcome unknown; reservation {self.lease_id} retained until expiry")
        if self.pending:
            op = self.client._request("GET", "/api/v1/operations/" + quote(self.pending, safe=""))
            if not self._settled(op):
                raise PendingOperation(f"Operation {self.pending} not settled; reservation retained until expiry")
            self.pending = None
        try:
            self.client._request("DELETE", "/api/v1/leases/" + quote(self.lease_id, safe=""))
        except BenchHTTPError as error:
            if error.code != "lease_expired":
                raise
        self.closed = True

    def retire_after_recovery(self):
        """Retire an unusable handle after explicit local recovery, never certify its outcome."""
        state, epoch = self.client.device(self.device_id), self.client.health()["epoch"]
        if (not state["connected"] or state["quarantined"] or state.get("reservation") is not None or
                not state.get("recovered_at") or
                (epoch == self.epoch and state["generation"] == self.generation)):
            raise PendingOperation("A new, locally recovered and unreserved session is required")
        self.closed = True  # pending/uncertain remain recorded, never become success
        if self.output_dir:
            self.output_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
            (self.output_dir / "retired.json").write_text(json.dumps({"prior_epoch":self.epoch,
                "prior_generation":self.generation,"epoch":epoch,"generation":state["generation"],
                "operation":self.pending,"submission_uncertain":self.uncertain,
                "outcome":"not_certified","recovered_at":state["recovered_at"]}))

    def cursor(self):
        self._check_target()
        batch = self.client._request("GET", f"/api/v1/devices/{quote(self.device_id, safe='')}/events?after=0")
        if batch["epoch"] != self.epoch:
            raise ContinuityError("Bench epoch changed")
        cursor = Cursor(self.epoch, self.generation, batch["latest"], self._cursors, self._submissions)
        self._cursors += 1
        self._append("cursors.jsonl", {"index": cursor.index, "after": cursor.after, "submitted": cursor.submitted,
                                       "epoch": cursor.epoch, "generation": cursor.generation})
        return cursor

    def events(self, cursor):
        self._check_target()
        if cursor.generation != self.generation or cursor.epoch != self.epoch:
            raise ContinuityError("Cursor belongs to another session")
        batch = self.client._request("GET", f"/api/v1/devices/{quote(self.device_id, safe='')}/events?after={cursor.after}")
        if batch["epoch"] != cursor.epoch or batch["lost"]:
            raise ContinuityError("Event epoch changed or server events were lost")
        for event in batch["events"]:
            if event.get("source_lost") or event["kind"] == "disconnected" or (
                    event["kind"] == "connected" and event.get("generation") != cursor.generation):
                raise ContinuityError("UART continuity interrupted")
        cursor.after = batch["next"]
        return batch["events"]

    def wait_uart(self, pattern: bytes, *, cursor=None, timeout=10):
        _duration(timeout, 0.01, 600)
        if not isinstance(pattern, bytes) or not 1 <= len(pattern) <= 4096:
            raise ValueError("UART pattern must be 1..4096 bytes")
        cursor = cursor or self.cursor()
        start_after = cursor.after
        deadline, tail = time.monotonic() + timeout, b""
        while time.monotonic() < deadline:
            for event in self.events(cursor):
                if event["kind"] == "uart":
                    chunk = base64.b64decode(event["data"], validate=True)
                    data = tail + chunk
                    if pattern in data:
                        # Only a hash and a length of the pattern, the cursor it was waited from and the sequence of the matching event.
                        self._append("uart-matches.jsonl", {
                            "cursor_index": cursor.index, "cursor_after": start_after, "cursor_submitted": cursor.submitted,
                            "matched_seq": event.get("seq"), "pattern_sha256": hashlib.sha256(pattern).hexdigest(),
                            "pattern_len": len(pattern), "epoch": cursor.epoch, "generation": cursor.generation,
                            "operation": (self.last_operation or {}).get("id")})
                        return pattern
                    tail = data[-(len(pattern) - 1):] if len(pattern) > 1 else b""
            time.sleep(self.client.poll_interval)
        raise TimeoutError("UART pattern not observed within the requested timeout")
