import json
from pathlib import Path
import selectors
import subprocess
import sys
import time
import pytest
from iobewi_bench import BenchClient

pytest_plugins = ["pytester"]
TOKEN = "pytest-client-test-token"


@pytest.fixture(scope="session")
def sim_bench():
    process = subprocess.Popen([sys.executable, str(Path(__file__).with_name("sim_server.py"))],
                               stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    try:
        selector = selectors.DefaultSelector()
        selector.register(process.stdout, selectors.EVENT_READ)
        assert selector.select(10), "Simulated server did not start"
        selector.close()
        url = json.loads(process.stdout.readline())["url"]
        client = BenchClient(url, TOKEN)
        deadline = time.monotonic() + 10
        while not client.devices():
            assert time.monotonic() < deadline, "Simulated agent did not connect"
            time.sleep(.02)
        yield client
    finally:
        process.terminate()
        try:
            process.communicate(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
            process.communicate()
