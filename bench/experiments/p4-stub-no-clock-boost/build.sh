#!/usr/bin/env bash
# Reproducible HOST-ONLY build of esp-flasher-stub v1.2.2 for the ESP32-P4 (baseline) and of an experimental variant without the
# DCDC / CPU 200 MHz boost on the UART transport. No device is opened, nothing is loaded on a board, the production stub is not replaced.
#   WORK=<dir> (default ./work)   TOOLCHAIN_TARBALL=<file> (optional, an already downloaded toolchain; its SHA-256 is still verified)
set -euo pipefail
HERE=$(cd "$(dirname "$0")" && pwd)
WORK=${WORK:-$PWD/work}; mkdir -p "$WORK"; cd "$WORK"

STUB_SHA=6f8dc174cd9742979bd6c4fb796ad60837800547      # esp-flasher-stub tag v1.2.2
LIB_SHA=d61983fa4d1f66b9c088608c1f702ad877121279       # esp-stub-lib pinned by that tag
STUB_TREE=1771d47bb8e6b71e6224292450e06fb6bbeeb63e82adbc0b3d9d719aa5d154d6
LIB_TREE=24a10fda08ee1f1f3bddc109e0fde851a9fbf1ad17f7e840d8123e864a879eca
TOOLCHAIN_URL=https://github.com/espressif/crosstool-NG/releases/download/esp-15.1.0_20250607/riscv32-esp-elf-15.1.0_20250607-x86_64-linux-gnu.tar.xz
TOOLCHAIN_SHA256=9bfe9b4fe08bd07614d085482301f5206a15a075ce4e9cf1c9c3d9d3e0d6b110
# Published esp32p4.json of v1.2.2 (esptool-js 0.7.0/0.7.1, esptool commit 4b64d421): SHA-256 of the base64 strings
SHIPPED_TEXT=722c15bbe563df347662c824f5262670fa34ac46e95aae6c3d50d0b2b799af8b
SHIPPED_DATA=82d043b5e425cc14e0867a82e3b711e735f8eeb057da10494cf1848bcfa198cd
SHIPPED_ENTRY=1341461590

treehash() { python3 - "$@" <<'PY'
import hashlib, os, sys
root, skip = sys.argv[1], set(sys.argv[2:]); h = hashlib.sha256()
for d, dirs, files in os.walk(root):
    dirs[:] = sorted(x for x in dirs if x not in skip and x != '.git')
    for f in sorted(files):
        p = os.path.join(d, f); h.update(os.path.relpath(p, root).encode() + b'\0' + hashlib.sha256(open(p, 'rb').read()).digest())
print(h.hexdigest())
PY
}
say() { printf '\n== %s\n' "$*"; }

say "1. sources at the pinned commits (verified by tree hash)"
if [ ! -d src-stub ]; then mkdir src-stub && curl -fsSL "https://github.com/espressif/esp-flasher-stub/archive/$STUB_SHA.tar.gz" | tar xz -C src-stub --strip-components=1; fi
if [ ! -d src-lib ];  then mkdir src-lib  && curl -fsSL "https://github.com/espressif/esp-stub-lib/archive/$LIB_SHA.tar.gz"  | tar xz -C src-lib  --strip-components=1; fi
[ "$(treehash src-stub esp-stub-lib)" = "$STUB_TREE" ] || { echo "stub source tree differs from the pinned one" >&2; exit 1; }
[ "$(treehash src-lib)" = "$LIB_TREE" ] || { echo "esp-stub-lib source tree differs from the pinned one" >&2; exit 1; }
echo "stub $STUB_SHA and lib $LIB_SHA: source trees verified"

say "2. tools (versions pinned)"
if [ ! -d toolchain ]; then
  mkdir toolchain
  TB=${TOOLCHAIN_TARBALL:-toolchain.tar.xz}
  [ -f "$TB" ] || curl -fsSL -o "$TB" "$TOOLCHAIN_URL"
  [ "$(sha256sum "$TB" | cut -d' ' -f1)" = "$TOOLCHAIN_SHA256" ] || { echo "toolchain tarball SHA-256 mismatch" >&2; rm -rf toolchain; exit 1; }
  tar -xf "$TB" -C toolchain
fi
[ -d venv ] || { python3 -m venv venv && venv/bin/pip -q install pyelftools==0.33 cmake==4.4.4 ninja==1.13.2; }
export PATH="$WORK/venv/bin:$WORK/toolchain/riscv32-esp-elf/bin:$PATH"
riscv32-esp-elf-gcc --version | head -1; cmake --version | head -1; echo "ninja $(ninja --version)"

prepare() {  # prepare <name>: a stub tree with the library in place
  rm -rf "$1"; mkdir "$1"; (cd src-stub && tar --exclude=esp-stub-lib -cf - .) | tar -xf - -C "$1"; mkdir "$1/esp-stub-lib"; (cd src-lib && tar -cf - .) | tar -xf - -C "$1/esp-stub-lib"
}
build() {    # build <tree> <builddir> [extra cmake args]
  local tree=$1 out=$2; shift 2
  (cd "$tree" && cmake -G Ninja -B "$out" -DTARGET_CHIP=esp32p4 --fresh -Wno-dev "$@" >"$out.configure.log" 2>&1 && ninja -C "$out" >"$out.build.log" 2>&1)
}

say "3. baseline (unmodified v1.2.2) must reproduce the published esp32p4.json byte for byte"
prepare base; build base build-esp32p4
python3 - "$SHIPPED_TEXT" "$SHIPPED_DATA" "$SHIPPED_ENTRY" <<'PY'
import hashlib, json, sys
d = json.load(open('base/build-esp32p4/esp32p4.json'))
t, da, e = hashlib.sha256(d['text'].encode()).hexdigest(), hashlib.sha256(d['data'].encode()).hexdigest(), str(d['entry'])
ok = (t, da, e) == tuple(sys.argv[1:4]); print('baseline text', t[:16], 'data', da[:16], 'entry', e, '->', 'IDENTICAL to the published stub' if ok else 'DIFFERENT')
sys.exit(0 if ok else 1)
PY

say "4. patch (the only difference between the two source trees)"
prepare variant; (cd variant && patch -p1 --dry-run -s < "$HERE/p4-uart-no-clock-boost.patch" && patch -p1 -s < "$HERE/p4-uart-no-clock-boost.patch")
diff -rq base variant -x 'build-*' || true

say "5. patched tree, macro OFF (neutrality) and macro ON (experiment)"
build variant build-neutral
build variant build-noboost -DCMAKE_C_FLAGS=-DSTUB_P4_UART_NO_CLOCK_BOOST

say "6. comparison"
python3 "$HERE/compare.py" "$WORK"
