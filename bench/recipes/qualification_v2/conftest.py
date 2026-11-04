"""Versioned campaign; physical gestures and deliberate failure are opt-in."""
import hashlib
import os
from pathlib import Path

import pytest

from iobewi_bench.qualification import Campaign


def pytest_addoption(parser):
    group = parser.getgroup("qualification-v2")
    group.addoption("--bench-reference", help="Private full flash reference file")
    group.addoption("--bench-reference-sha256", help="Previously verified full reference SHA-256")
    group.addoption("--bench-interactive", action="store_true", help="Enable coordinated USB gestures")
    group.addoption("--bench-deliberate-failure", action="store_true", help="Enable intentionally failing M4")
    group.addoption("--bench-c3-table-address", type=lambda value: int(value, 0),
                    help="Known C3 partition table address; otherwise probe 0x8000 and 0x10000 read-only")
    group.addoption("--bench-c3-reset", action="store_true", help="Enable the C3 reset and ROM capture test")


@pytest.fixture
def qualification(bench_client, bench_run_dir, request, pytestconfig):
    path = bench_run_dir / hashlib.sha256(request.node.nodeid.encode()).hexdigest()[:16]
    declarations = {key: os.environ.get(key, "not recorded") for key in
                    ("BENCH_CLIENT_REVISION", "BENCH_SERVER_REVISION", "BENCH_PAGE_REVISION", "BENCH_TRANSPORT")}
    case = Campaign(bench_client, pytestconfig.getoption("--bench-device"), path,
                    request.node.path, request.node.nodeid, revisions=declarations)
    support = Path(__file__).read_bytes()
    (path / "conftest.py").write_bytes(support)
    case.write("conftest-sha256.json", {"sha256": hashlib.sha256(support).hexdigest()})
    request.node._qualification = case
    try:
        case.start()
        yield case
    finally:
        case.close()


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_makereport(item, call):
    outcome = yield
    report = outcome.get_result()
    case = getattr(item, "_qualification", None)
    if case:
        case.client.record("assertions.jsonl", {"phase": report.when, "outcome": report.outcome,
            "exception_type": call.excinfo.typename if call.excinfo else None})


@pytest.fixture
def second_token():
    token = os.environ.get("BENCH_CLIENT_TOKEN_B")
    if not token:
        pytest.skip("M5 concurrency needs an independently configured second client token")
    return token


@pytest.fixture
def reference(pytestconfig):
    path, digest = (pytestconfig.getoption(name) for name in
                    ("--bench-reference", "--bench-reference-sha256"))
    if not path or not digest:
        pytest.skip("M6 needs a private reference and its independently verified full SHA-256")
    return Path(path), digest


@pytest.fixture
def physical_gesture(pytestconfig):
    if not pytestconfig.getoption("--bench-interactive"):
        pytest.skip("Coordinate the USB gesture before using --bench-interactive")


@pytest.fixture
def intentional_failure(pytestconfig):
    if not pytestconfig.getoption("--bench-deliberate-failure"):
        pytest.skip("M4 is intentionally red; enable --bench-deliberate-failure explicitly")


@pytest.fixture
def c3_table_addresses(pytestconfig):
    address = pytestconfig.getoption("--bench-c3-table-address")
    return [address] if address is not None else [0x8000, 0x10000]


@pytest.fixture
def c3_reset_enabled(pytestconfig):
    if not pytestconfig.getoption("--bench-c3-reset"):
        pytest.skip("C3 reset needs explicit --bench-c3-reset")
