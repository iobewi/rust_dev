#!/bin/bash
# Builds the rollback test image D from a clean export of #105 (9ba9533) plus the one-line trace patch. Nothing is written to the board.
# The secrets and network parameters come from the environment and stay on this machine (the images embed them: never commit or publish them):
#   IOBEWI_OTA_TOKEN  IOBEWI_TLS_CERT_PEM  IOBEWI_TLS_KEY_PEM  IOBEWI_STA_SSID  IOBEWI_STA_PASSWORD
# usage: build_d.sh WORKDIR RSA_KEY_PEM GEN_ESP32PART_PY [REPO]   -> WORKDIR/out/{app.bin,app.env,app.iap1,app.elf}
set -euo pipefail
W=$(realpath "$1"); KEY=$(realpath "$2"); PT=$(realpath "$3"); REPO=${4:-$(dirname "$0")/../../rust-mcu-libs}
here=$(cd "$(dirname "$0")" && pwd)
mkdir -p "$W/src" && git -C "$REPO" archive 9ba9533 | tar -x -C "$W/src"
patch -d "$W/src" -p1 < "$here/d-hang-trace.patch"
[ -f "${LOCKFILE:-/nonexistent}" ] && cp "$LOCKFILE" "$W/src/Cargo.lock"            # Cargo.lock is not tracked: pin it from the lock used for A''/B''
export CARGO_TARGET_DIR="$W/target" ESP_BOOTLOADER_ESP_IDF_CONFIG_PARTITION_TABLE_OFFSET=0x10000
cd "$W/src"
python3 examples/firmware/ota-system/prepare.py --chip esp32s3 --flash-size 16MB --auth signed-rsa --boot verified --fault hang-self-check \
  --rsa-key "$KEY" --partition-tool "$PT"
mkdir -p "$W/out" && cp "$CARGO_TARGET_DIR/ota-system/esp32s3/"* "$W/out/" && cp "$CARGO_TARGET_DIR/xtensa-esp32s3-none-elf/release/firmware-ota-system-esp32" "$W/out/app.elf"
( cd "$W/out" && sha256sum app.bin app.env app.iap1 )
