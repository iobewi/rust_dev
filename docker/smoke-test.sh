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
test "$(git config --system --get core.autocrlf)" = input
test "$(git config --system --get core.eol)" = lf
