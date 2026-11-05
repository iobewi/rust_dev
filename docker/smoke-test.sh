#!/usr/bin/env bash
# Software-only checks; do not access USB or the live server's journal.
set -euo pipefail
. /opt/esp/export-esp.sh
python --version
node --version
rustc --version
rustc +esp --version
espup --version
espflash --version
esp-generate --version
claude --version
python - <<'PY'
import json
import os
from pathlib import Path
import sys
import tempfile
import aiohttp
import iobewi_bench
import pytest

assert sys.version_info[:2] == (3,12)
assert Path(iobewi_bench.__file__).resolve().is_relative_to('/workspace/bench/python')
assert Path('/root/.claude').is_dir()
assert os.environ['CLAUDE_CONFIG_DIR'] == '/root/.claude'
state = Path(os.environ['BENCH_STATE_DIR'])
state.mkdir(parents=True, exist_ok=True)
sys.path.insert(0,'/workspace/bench')
from server.core import Broker
with tempfile.TemporaryDirectory(dir=state) as directory:
    broker = Broker(state_dir=directory)
    broker.journal.close()
print(json.dumps({'python_client':'editable workspace','journal':'isolated SQLite smoke passed'}))
PY
cd /workspace/bench
node --input-type=module - <<'JS'
import assert from 'node:assert/strict';
assert.equal(process.versions.node.split('.')[0],'22');
JS
