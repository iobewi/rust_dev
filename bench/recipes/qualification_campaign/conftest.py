"""Opt-in generic qualification campaign. Collection performs no network or serial access."""
import os

import pytest

from .session import load_session


def pytest_addoption(parser):
    group = parser.getgroup("qualification-campaign")
    group.addoption("--qual-session", default=os.environ.get("BENCH_QUAL_SESSION"),
                    help="Private session JSON (board facts, approved regions, consent). Never in Git.")
    group.addoption("--qual-allow-dirty", action="store_true",
                    help="Allow uncommitted bench sources (development/simulation only; recorded in the run)")
    group.addoption("--qual-allow-reset", action="store_true",
                    help="Allow C3R-3 to send an explicit reset command (also needs consent C3R-RESET in the session)")
    group.addoption("--qual-allow-restart", action="store_true",
                    help="Allow Q3 to signal the identified launcher; also needs q3.allow_restart in the session")


@pytest.fixture(scope="session")
def qual_session(pytestconfig):
    path = pytestconfig.getoption("--qual-session", default=None)
    if not path:
        pytest.skip("BLOCKED: no --qual-session / BENCH_QUAL_SESSION private session file")
    return load_session(path)
