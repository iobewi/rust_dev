# Firefox qualification of the bench page (no hardware)

Runs the **real** `bench/web/index.html` in Firefox with the **real** esptool-js 0.7.0 (loaded from unpkg, pinned in the
page) on top of a **simulated ESP32-S3** behind a fake Web Serial port. Firefox has no Web Serial; `sim.js` replaces
`navigator.serial`. Nothing here opens a real port or touches a board.

## What is simulated (`sim.js`)
* SLIP framing and the loader commands esptool-js uses: SYNC (answers as a running stub, so no stub upload),
  GET_SECURITY_INFO, READ_REG/WRITE_REG, SPI_ATTACH/SET_PARAMS, CHANGE_BAUDRATE, FLASH_DEFL_BEGIN/DATA/END (real zlib
  inflate), SPI_FLASH_MD5, ERASE_FLASH, READ_FLASH. DTR/RTS sequences switch app / ROM download mode.
* Scripted faults (`__sim.cfg`): delayed or dropped begin acknowledgement, port lost at begin or at a block, silent or
  wrong MD5, no sync, throttled timers (`__sim.throttle`).
* Not simulated: the real stub upload, real flash timing, partial writes (data is committed at the last block), a really
  hidden tab (headless Firefox keeps every tab visible; visibility is forced and timers are held to a coarse grid).

## Files
| File | Content |
|---|---|
| `qual-base.mjs` | page and modules load, esptool-js bundle identity, identify / readpt / dump, inventory refresh |
| `qual-critical.mjs` | delayed begin ack, throttled timers, port closed mid-operation, reload during a write, late result, expired commands, protection after `indeterminate`, reconnection |
| `qual-verdicts-inventory.mjs` | `failed` / `indeterminate` display, background tab (emulated), 600+ file inventory, stock `Transport` vs `BenchTransport` |
| `qual-security.mjs` | server restart mid-write, command claimed without acknowledgement, confirmation before reopening the console |
| `qual-truncation.mjs` | an intermediary cuts every `/api/images` answer at 64 KiB (incident P5-A): the write resolves its file by name and completes; a cut lookup is refused with figures, nothing programmed |
| `lib.mjs`, `fixture.py`, `run-all.mjs` | helpers, test data (608 files), runner |

## Reproduce
```sh
cd bench/tests/browser
npm ci                                        # playwright-core 1.48.2 (pinned in package.json / lockfile)
npx playwright-core install firefox           # Firefox 131 into $PLAYWRIGHT_BROWSERS_PATH (default $BENCH_QUAL_WORK/browsers)
# Debian/Ubuntu also needs: libgtk-3-0 libxcursor1 libpangocairo-1.0-0 libcairo-gobject2 libgdk-pixbuf-2.0-0 libdbus-glib-1-2 libxt6 libasound2 libxtst6
npm run qualify                               # about 5 minutes; exit status 1 if any check fails
node qual-critical.mjs delayed_begin          # a single scenario (names are the keys of `scenarios`)
```
Environment: `BENCH_QUAL_WORK` (scratch directory, default `$TMPDIR/bench-qual`; holds logs, test data, screenshots, browsers;
never versioned) and `BENCH_QUAL_PORT` (default 8082; 8080 and 8081 are refused: they are the live benches).
The harness starts and stops its own `bench_server.py` (only the pid it started). Needs network access to unpkg.com.
Last qualified run: 118 browser checks PASS, 37 Node tests, 39 Python tests.

`npm test` in `bench/` does not run these (it needs a browser).
