#!/usr/bin/env bash
set -eo pipefail
. /opt/esp/export-esp.sh
rustc --version
cargo --version
rustc +esp --version
cargo fmt --version
cargo clippy --version
espup --version
espflash --version
esp-generate --version
claude --version
python --version
node --version
python -m pytest --version
python -c 'from importlib.metadata import version; from idlab_client import IDLabClient; print("idlab-client", version("idlab-client"))'
test "$(git config --system --get core.autocrlf)" = input
test "$(git config --system --get core.whitespace)" = cr-at-eol
