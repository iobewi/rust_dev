"""Opt-in pytest integration; the server/browser remain the hardware authority."""
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import uuid

import pytest
from .client import BenchClient


def pytest_addoption(parser):
    group = parser.getgroup("iobewi-bench")
    group.addoption("--bench", action="store_true", help="Enable tests using the bench API")
    group.addoption("--bench-url", default=os.environ.get("BENCH_URL", "http://127.0.0.1:8080"))
    group.addoption("--bench-device", default=os.environ.get("BENCH_DEVICE", "esp32-local"))
    group.addoption("--bench-lease-ttl", type=int, default=900)
    group.addoption("--bench-allow-destructive", action="store_true", help="Allow explicit destructive calls; server scope, grant and consent still required")
    group.addoption("--bench-results", default=".bench-runs", help="Private artifact directory, outside Git")


def pytest_configure(config):
    config.addinivalue_line("markers", "bench: recipe requiring an explicitly enabled hardware bench")


def pytest_collection_modifyitems(config, items):
    if not config.getoption("--bench"):
        skip = pytest.mark.skip(reason="Bench recipes require --bench")
        for item in items:
            if item.get_closest_marker("bench"):
                item.add_marker(skip)


@pytest.fixture(scope="session")
def bench_client(pytestconfig):
    if not pytestconfig.getoption("--bench"):
        pytest.skip("Bench access requires --bench")
    token = os.environ.get("BENCH_CLIENT_TOKEN")
    if not token:
        pytest.fail("Set BENCH_CLIENT_TOKEN in the environment; never put it in a recipe", pytrace=False)
    return BenchClient(pytestconfig.getoption("--bench-url"), token)


@pytest.fixture(scope="session")
def bench_run_dir(pytestconfig):
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ") + "-" + uuid.uuid4().hex[:8]
    path = Path(pytestconfig.getoption("--bench-results")) / stamp
    path.mkdir(parents=True, mode=0o700)
    return path


@pytest.fixture
def board(bench_client, bench_run_dir, request, pytestconfig):
    path = bench_run_dir / hashlib.sha256(request.node.nodeid.encode()).hexdigest()[:16]
    path.mkdir(mode=0o700)
    target = bench_client.reserve(pytestconfig.getoption("--bench-device"),
        ttl=pytestconfig.getoption("--bench-lease-ttl"),
        allow_destructive=pytestconfig.getoption("--bench-allow-destructive"), output_dir=path)
    try:
        recipe = Path(request.node.path).read_bytes()
        (path / "recipe.py").write_bytes(recipe)
        (path / "run.json").write_text(json.dumps({
            "recipe_sha256": hashlib.sha256(recipe).hexdigest(),
            "test": request.node.nodeid, "device": target.device_id,
            "epoch": target.epoch, "generation": target.generation,
            "started_at": datetime.now(timezone.utc).isoformat(),
        }, indent=2))
        yield target
    finally:
        target.close()
