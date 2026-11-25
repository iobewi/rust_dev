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
tmp="$(mktemp -d)"
trap 'rm -rf "$tmp"' EXIT
printf '#![no_std]\npub fn smoke() -> u32 { 42 }\n' > "$tmp/smoke.rs"
rustc --crate-type lib "$tmp/smoke.rs" -o "$tmp/host.rlib"
for target in riscv32imc-unknown-none-elf riscv32imac-unknown-none-elf; do
    rustc --crate-type lib --target "$target" "$tmp/smoke.rs" -o "$tmp/$target.rlib"
done
rustc +esp --crate-type lib --target xtensa-esp32s3-none-elf "$tmp/smoke.rs" -o "$tmp/xtensa.rlib"
