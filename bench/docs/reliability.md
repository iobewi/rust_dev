# Reliability work — software audit (2026-11-05)

Reference: main `7e7595b`; open PR #5 / `feat/2-c3-qualification`
adds qualification preparation and transport simulations, not these fixes.
No physical hardware qualification or write is performed by this work.

| Defect | Component | Correction / regression |
| --- | --- | --- |
| Campaign rejects any grant and loses consent | Python Campaign | explicit `allow_destructive=True`; per-command consent still mandatory; final grant must be zero |
| No public upload | Python client | bounded file/buffer upload; SHA-256 and metadata checks; no retry |
| Implicit console return | Python Board | `read_flash(return_console=False)` and explicit `console()` |
| Storage full / no deletion | broker/API | capacity, owner deletion, pins for active/uncertain work (next change) |
| Operations lost at restart | broker | durable outcome journal and strict recovery (next change) |
| WS remains disconnected | browser agent | safe reconnect without command replay (next change) |

## Client contract

A destructive campaign opts in with `Campaign(..., allow_destructive=True)`.
Its `reserve()` propagates that consent; every `flash.write` / `flash.erase`
still requires `allow_destructive=True`. The server enforces read scope on GET,
control on mutations, program on programming, and the physical agent grant.
`wait_grant(minimum_seconds=..., timeout=...)` observes only: it cannot grant,
recover, reserve or submit. For a long backup/program/readback scenario, the
operator should grant 1800 seconds and use reservations long enough for each
step (renew explicitly, maximum 3600 seconds). Operation timeout remains at most
600 seconds. Teardown checks revocation; the operator must revoke locally.

`upload(path_or_buffer)` performs exactly one POST. It accepts nonempty binaries
up to 16 MiB, computes SHA-256 and verifies returned ID, size and digest.
HTTP refusals remain `BenchHTTPError(status, code)`; a lost response is not a
reason to upload or execute again automatically. No binary body is logged.

`read_flash(..., return_console=False)` leaves the loader session open if the
transport supports it. Call `board.console()` explicitly afterwards. Entering the
loader may itself reset the MCU. Repeated whole-flash hashes can differ when NVS
or other dynamic regions change during firmware execution; record those regions
separately from immutable code. Unsupported transports fail explicitly.

## Exceptions

- `OperationError`: a settled failure, safe to inspect as a definitive outcome.
- `PendingOperation`: unresolved submission or execution, reservation retained;
  new commands are refused. It is not a definitive hardware failure.
- `ContinuityError`: epoch, generation or event continuity broke; no implicit resume.
- `BenchHTTPError`: a determined HTTP refusal with status/code. A 5xx after an
  operation POST remains conservative submission uncertainty.

Historical recipes and evidence must not be rewritten to make interrupted tests
pass. The new support files are copied into each evidence directory as before.

## Temporary artifact store

`GET /api/v1/artifacts` now includes `storage` (total, used, reserved,
available, max_artifact, all in bytes) and a `pinned` flag per artifact.
Total stays 64 MiB; individual artifacts stay at most 16 MiB. Upload requires
Content-Length; admission reserves capacity before reading the body. Concurrent
uploads cannot overcommit admission. Capacity refusal is HTTP 503 / `capacity`.
The client's preflight is advisory; server admission remains authoritative.

`DELETE /api/v1/artifacts/{id}` requires control scope and ownership. Clients
can delete their own uploads and readbacks produced for their own operations.
Agents cannot delete. Artifacts remain readable under the existing API policy
(all authenticated clients with read scope); this is not a private artifact vault.
No tokens or binary data are returned in the inventory.

Active, unsettled, unacknowledged and unreconciled indeterminate operations pin
input/output artifacts. Agent uploads are conservatively pinned while their agent
has unresolved work, including the interval before a result associates the ID.
The one-hour cleanup never overrides these pins. Operator recovery explicitly
reconciles indeterminate operations, without changing their verdict to success.
Recipes should download and verify readbacks, then explicitly delete disposable
artifacts after settlement. Backups needed for restoration must be retained
privately by the recipe; there is no implicit cleanup of those backups.

## Durable operations and restart

The executable server now defaults to a private SQLite journal under
`~/.local/state/iobewi-bench`; set `BENCH_STATE_DIR` to a persistent private
volume in containers. One process owns its lock. `/health.durability` reports
`sqlite-full` or `volatile` (the programmatic test/simulation factory remains
volatile unless supplied a `Broker(state_dir=...)`). Do not use volatile mode for
qualification. SQLite uses synchronous FULL transactions. A checkpoint failure
latches execution closed (`journal_unavailable`, 503); restart and inspect the
storage before continuing. Health becomes degraded.

Transitions are committed before emitting an offer, execution permit or result
acknowledgement. Inputs and uploaded readbacks are retained in the same private
database within the existing 64 MiB store limit. No UART payload, MAC address,
HTTP request/response body, arbitrary trace or flash dump is copied into outcome
JSON. Flash verification metadata and the digest of the original result are
preserved; the binary artifact table remains private and bounded. The journal
is operational recovery state, not an immutable archive of campaign evidence.
Copy qualification evidence separately; do not publish the database.

| Last durable state | State after restart | Recovery |
| --- | --- | --- |
| queued/offered | cancelled, never executed | fresh reservation; no replay |
| running | indeterminate | quarantine, local reconciliation |
| result received, not settled | original result/state retained | quarantine, local reconciliation of settlement |
| result received and settled | original result/state retained | fresh session; grants and leases invalidated |

Every restart generates a new epoch. Old device generations, sessions, leases
and grants cannot authorize execution. Stale results cannot complete an old
operation. Local recovery records its time and new generation without changing
an indeterminate verdict into success. Unknown operations are never replayed.
A crash before the durable result transaction can still lose that result; it is
then indeterminate. A result persisted before a lost acknowledgement survives,
but does not prove console settlement. Filesystem/hardware durability depends on
the underlying persistent volume honoring fsync; there is no distributed journal.
The one-hour operational retention never evicts unresolved recovery records.
