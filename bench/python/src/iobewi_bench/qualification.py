"""Read-only qualification evidence. No implicit retry, recovery or hardware grant."""
from datetime import datetime, timezone
import hashlib
from importlib.metadata import version
import json
from pathlib import Path
import platform
import time
from urllib.parse import quote

from .client import BenchClient, BenchError, BenchHTTPError, ContinuityError, OperationError


def expect_http_refusal(action, *, status, code):
    try:
        action()
    except BenchHTTPError as error:
        assert (error.status, error.code) == (status, code), "Unexpected HTTP refusal"
        return
    raise AssertionError("Expected HTTP refusal, command succeeded")


def compare_reference(data, path, *, sha256, size):
    """Verify the entire reference before comparing its requested prefix."""
    reference = Path(path).read_bytes()
    assert len(reference) >= size, "Reference is too short"
    assert hashlib.sha256(reference).hexdigest() == sha256, "Reference digest mismatch"
    assert len(data) == size, "Read size mismatch"
    assert data == reference[:size], "Flash differs from verified reference"
    return {"size": size, "reference_sha256": sha256,
            "read_sha256": hashlib.sha256(data).hexdigest(), "equal": True}


def expect_disconnected_read(board, *, size):
    try:
        board.read_flash(address=0, size=size, timeout=300)
    except OperationError as failure:
        op = failure.operation
        error = op.get("error") or {}
        assert op["state"] == "failed", "Interrupted read must fail"
        assert error.get("code") == "serial_disconnected", "Unexpected read error"
        assert error.get("phase") == "flash_read", "Unexpected error phase"
        assert op.get("result") is None, "Interrupted read returned a result"
        assert error.get("expected_bytes") == size, "Missing transfer size"
        assert 0 <= error.get("received_bytes", -1) < size, "Invalid partial progress"
        if board.output_dir:
            assert not list(board.output_dir.glob("flash-*.bin")), "Partial dump was exported"
        return op["id"]
    raise AssertionError("Read completed without the expected disconnection")


def expect_stale_board(board, observed_client):
    count = observed_client.submissions
    try:
        board.identify()
    except ContinuityError:
        assert observed_client.submissions == count, "Stale Board submitted an operation"
        return
    raise AssertionError("Stale Board accepted the command")


class ObservedClient(BenchClient):
    """Record request types and safe response fields, never bodies or UART data."""
    def __init__(self, source, directory, label):
        super().__init__(source.url, source._token, request_timeout=source.request_timeout,
                         poll_interval=source.poll_interval)
        self.directory, self.label, self.submissions = directory, label, 0

    def record(self, filename, value):
        with (self.directory / filename).open("a") as file:
            file.write(json.dumps(value) + "\n")

    def _request(self, method, path, body=None, *, binary=False):
        if method == "POST" and path == "/api/v1/operations":
            self.submissions += 1
        entry = {"at": time.time(), "client": self.label, "method": method, "path": path}
        try:
            response = super()._request(method, path, body, binary=binary)
        except BaseException as error:
            entry["exception_type"] = type(error).__name__
            if isinstance(error, BenchHTTPError):
                entry.update(status=error.status, code=error.code)
            raise
        else:
            entry["ok"] = True
            if isinstance(response, dict) and path.split("?")[0].startswith("/api/v1/operations"):
                safe = {k: response[k] for k in ("id", "device", "generation", "command", "state",
                        "requires_settle", "settled", "unsettled") if k in response}
                error = response.get("error") or {}
                safe["error"] = {k: error[k] for k in ("code", "phase", "transport_kind",
                    "received_bytes", "expected_bytes", "elapsed_ms", "http_status") if k in error}
                safe["has_result"] = response.get("result") is not None
                self.record("diagnostics.jsonl", safe)
            return response
        finally:
            self.record("requests.jsonl", entry)


class Campaign:
    """Per-test record and explicit reservation factory, including independent clients."""
    def __init__(self, source, device, directory, recipe, test_id, *, revisions=None, allow_destructive=False, manifest=None, parameters=None):
        from .manifest import CampaignManifest
        self.manifest = CampaignManifest(manifest) if isinstance(manifest, dict) else manifest
        if self.manifest is not None and not isinstance(self.manifest, CampaignManifest):
            raise ValueError("Expected a validated campaign manifest")
        self.results = []
        self.directory = Path(directory)
        self.directory.mkdir(parents=True, mode=0o700)
        if type(allow_destructive) is not bool:
            raise ValueError("Campaign consent must be an explicit boolean")
        self.allow_destructive = allow_destructive
        if self.manifest is not None:
            root = Path(self.manifest.document["private_evidence"]).resolve()
            if not self.directory.resolve().is_relative_to(root):
                raise ValueError("Campaign evidence must stay under the manifest private root")
            self.write("manifest.json", self.manifest.document)
        self.device = device
        self.client = ObservedClient(source, self.directory, "primary")
        self.boards, self.clients = [], [self.client]
        self.reservation_attempts = 0
        self.after = None
        self.epoch = None
        self.window = 0
        self.started = False
        self.manual_interruptions = 0
        self.generation = None
        recipe = Path(recipe)
        (self.directory / "recipe.py").write_bytes(recipe.read_bytes())
        module = Path(__file__).read_bytes()
        (self.directory / "qualification.py").write_bytes(module)
        sources = {}
        for name in ("client.py", "pytest_plugin.py"):
            content = Path(__file__).with_name(name).read_bytes()
            (self.directory / name).write_bytes(content)
            sources[name] = hashlib.sha256(content).hexdigest()
        self.write("run.json", {"test": test_id, "recipe_sha256": hashlib.sha256(recipe.read_bytes()).hexdigest(),
            "support_sha256": hashlib.sha256(module).hexdigest(), "device": device,
            "client_source_sha256": sources,
            "python": platform.python_version(), "pytest": version("pytest"),
            "iobewi-bench": version("iobewi-bench"), "declared_environment": revisions or {},
            "parameters_sha256": hashlib.sha256(json.dumps(parameters or {}, sort_keys=True).encode()).hexdigest(),
            "manifest_sha256": self.manifest.sha256 if self.manifest else None,
            "allow_destructive": allow_destructive,
            "started_at": datetime.now(timezone.utc).isoformat()})

    def write(self, filename, value):
        (self.directory / filename).write_text(json.dumps(value, indent=2))

    def snapshot(self, name):
        health, device = self.client.health(), self.client.device(self.device)
        self.observed_epoch = health["epoch"]
        safe = {k: device[k] for k in ("id", "generation", "connected", "quarantined",
                "reservation", "privileged_seconds", "capabilities", "chip", "flash_size", "provenance", "provenance_source", "recovered_at") if k in device}
        self.write(name + ".json", {"epoch": health["epoch"], "device": safe,
            "server_observed": health.get("build"), "durability": health.get("durability", "unavailable")})
        return safe

    def start(self):
        if self.started:
            raise ContinuityError("Campaign already started; open a new evidence window explicitly")
        self.started = True
        scopes = set(self.client.identity()["scopes"])
        assert {"read", "control"} <= scopes, "Campaign requires read/control scopes"
        if self.allow_destructive:
            assert "program" in scopes, "Destructive campaign requires program scope"
        state = self.snapshot("before")
        if self.manifest is not None:
            self.manifest.check_device(state)
        assert state["connected"] and not state["quarantined"], "Card is not ready"
        assert state["reservation"] is None, "Card is already reserved"
        if not self.allow_destructive:
            assert state["privileged_seconds"] == 0, "Revoke remote grant before qualification"
        batch = self.client._request("GET", f"/api/v1/devices/{quote(self.device, safe='')}/events?after=0")
        assert batch["epoch"] == self.observed_epoch, "Server epoch changed during preflight"
        self.epoch, self.after = batch["epoch"], batch["latest"]
        self.generation = state["generation"]
        self.write("event-start.json", {"epoch": self.epoch, "after": self.after,
                                       "generation": state["generation"]})

    def interrupt_window(self, reason):
        """Explicitly end a proof window; this does not assert a continuous session."""
        if not isinstance(reason, str) or not reason:
            raise ValueError("An interruption reason is required")
        failure = None
        if self.after is not None:
            try:
                self.events()
            except (AssertionError, BenchError) as error:
                failure = type(error).__name__
        self.client.record("windows.jsonl", {"window": self.window, "epoch": self.epoch,
            "after": self.after, "event": "interrupted", "reason": reason, "capture_error": failure})
        self.after = None

    def open_window(self, reason, *, allow_quarantine=False):
        """Begin fresh evidence after an explicit interruption, never renew old Boards."""
        if self.after is not None:
            raise ContinuityError("End the previous proof window explicitly")
        if not isinstance(reason, str) or not reason:
            raise ValueError("A new-window reason is required")
        self.window += 1
        state = self.snapshot(f"window-{self.window:03d}-before")
        assert state["connected"], "Card is disconnected"
        assert allow_quarantine or not state["quarantined"], "Card requires local recovery"
        batch = self.client._request("GET", f"/api/v1/devices/{quote(self.device, safe='')}/events?after=0")
        assert batch["epoch"] == self.observed_epoch, "Server changed during window opening"
        self.epoch, self.after = batch["epoch"], batch["latest"]
        self.generation = state["generation"]
        self.client.record("windows.jsonl", {"window": self.window, "event": "opened", "reason": reason,
            "epoch": self.epoch, "generation": state["generation"], "after": self.after,
            "quarantined": state["quarantined"]})

    def wait_grant(self, *, minimum_seconds=60, timeout=120):
        if not self.allow_destructive:
            raise ValueError("Destructive campaign consent required")
        return self.client.wait_device(self.device, timeout=timeout, grant_seconds=minimum_seconds, recovered=True)

    def independent_client(self, token):
        source = BenchClient(self.client.url, token)
        client = ObservedClient(source, self.directory, "secondary-" + str(len(self.clients)))
        self.clients.append(client)
        return client

    def reserve(self, *, client=None, ttl=None):
        ttl = (1800 if self.allow_destructive else 900) if ttl is None else ttl
        client = client or self.client
        if self.allow_destructive:
            assert {"read", "control", "program"} <= set(client.identity()["scopes"]), "Destructive reservation requires all scopes"
        path = self.directory / ("board-" + str(self.reservation_attempts))
        self.reservation_attempts += 1
        path.mkdir(mode=0o700)
        if self.manifest is not None:
            self.manifest.check_device(client.device(self.device))
        board = client.reserve(self.device, ttl=ttl, output_dir=path, allow_destructive=self.allow_destructive)
        board.manifest = self.manifest
        self.boards.append(board)
        self.client.record("reservations.jsonl", {"client": client.label, "lease": board.lease_id,
                           "epoch": board.epoch, "generation": board.generation})
        return board

    def events(self):
        """Drain bounded pages; transitions are evidence, losses invalidate the proof."""
        assert self.after is not None, "Campaign did not start"
        result = []
        for _ in range(100):
            batch = self.client._request("GET", f"/api/v1/devices/{quote(self.device, safe='')}/events?after={self.after}")
            self.client.record("events.jsonl", {"window": self.window, **{k: batch[k] for k in ("epoch", "lost", "next", "latest")}})
            assert batch["epoch"] == self.epoch, "Server epoch changed; proof interrupted"
            assert batch["lost"] == 0, "Server events lost; proof incomplete"
            for event in batch["events"]:
                safe = {k: event[k] for k in ("seq", "epoch", "at", "kind", "id", "state",
                        "generation", "source_lost", "seconds") if k in event}
                self.client.record("events.jsonl", safe)
                assert not event.get("source_lost"), "Agent UART loss; proof incomplete"
                if event["kind"] == "disconnected" or (event["kind"] == "connected" and
                        self.generation is not None and event.get("generation") != self.generation):
                    raise ContinuityError("Device continuity interrupted; open a fresh proof window explicitly")
                result.append(safe)
            self.after = batch["next"]
            if self.after >= batch["latest"]:
                return result
        raise AssertionError("Event capture exceeded 100 pages")

    def result(self, test_id, status, *, preconditions, evidence, incidents, restoration, limits):
        """Record the recipe/pytest verdict, never infer firmware success."""
        if status not in {"PASS", "FAIL", "SKIP", "BLOCKED", "INDETERMINATE"}:
            raise ValueError("Invalid qualification status")
        if any(row["id"] == test_id for row in self.results):
            raise ValueError("A historical verdict cannot be replaced")
        row = {"id": test_id, "status": status, "preconditions": preconditions, "evidence": evidence,
               "incidents": incidents, "restoration": restoration, "limits": limits}
        self.client.record("verdicts.jsonl", row)
        self.results.append(row)
        self.write("report.json", {"tests": self.results, "windows": self.window + 1})
        def cell(value):
            return str(value).replace("|", "\\|").replace("\n", " ")
        columns = ["id", "preconditions", "status", "evidence", "incidents", "restoration", "limits"]
        rows = ["| " + " | ".join(columns) + " |", "| " + " | ".join(["---"] * len(columns)) + " |"]
        rows += ["| " + " | ".join(cell(row[key]) for key in columns) + " |" for row in self.results]
        (self.directory / "report.md").write_text("\n".join(rows) + "\n")

    def close(self):
        errors = []
        for board in reversed(self.boards):
            try:
                board.close()
            except BaseException as error:
                errors.append(error)
        try:
            if self.after is not None:
                self.events()
        except BaseException as error:
            errors.append(error)
        try:
            state = self.snapshot("after")
            assert state["connected"] and not state["quarantined"], "Card did not recover"
            assert state["reservation"] is None, "Reservation remains after cleanup"
            assert state["privileged_seconds"] == 0, "Unexpected active remote grant"
        except BaseException as error:
            errors.append(error)
        self.write("cleanup.json", {"errors": [type(error).__name__ for error in errors],
                                   "submissions": {c.label: c.submissions for c in self.clients}})
        if errors:
            raise BaseExceptionGroup("Qualification cleanup/evidence failed", errors)
