#!/usr/bin/env bash
set -euo pipefail

. /opt/esp/export-esp.sh
rustc --version
cargo --version
rustc +esp --version
espup --version
espflash --version
esp-generate --version
claude --version

# The Python client talks to the broker API, never to USB.
python -m pip install --no-deps --no-build-isolation -e '/workspace/bench/python'
