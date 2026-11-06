# Reliability work — software audit (2026-11-05)

Reference: main `7e7595b`; open PR #5 / `feat/2-c3-qualification`
adds qualification preparation and transport simulations, not these fixes.
No physical hardware qualification or write is performed by this work.

| Defect | Component | Correction / regression |
| --- | --- | --- |
| Campaign rejects any grant and loses consent | Python Campaign | explicit `allow_destructive=True`; per-command consent still mandatory; final grant must be zero |
| No public upload | Python client | bounded file/buffer upload; SHA-256 and metadata checks; no retry |
| Implicit console return | Python Board | `read_flash(return_console=False)` and explicit `console()` |
| Storage full / no deletion | broker/API | capacity, owner deletion, pins for active/uncertain work |
| Operations lost at restart | broker | durable outcome journal and strict recovery |
| WS remains disconnected | browser agent | safe reconnect without command replay |

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

## Browser recovery and provenance

The agent retries a closed WebSocket once per second while the page remains open.
It waits until hardware execution ends. Reconnection clears old offers/deferred
commands and abandons unacknowledged results for explicit reconciliation; none
are resent. Grants stay zero. A held engine registers held, under a new generation.
Local recovery remains a separate operator action. A voluntary disconnect cancels
reconnection. After server restart, cookie-only agents reopen a local browser
session under the same loopback/origin checks before authenticating again.

`/health.build` identifies server version, local Git HEAD (which alone does not
prove a clean tree), actual source/asset SHA-256 and a separately labelled
optional declared build SHA. The delivered agent module is stamped with its own
source hash and the server's asset-set hash at delivery. Its registration reports
those stamps, WebSerial VID/PID, and transport **inferred** from known USB IDs.
Unknown USB IDs stay unknown; VID/PID cannot prove the physical wiring. These
fields are labelled `agent_reported`, not independently verified server facts.
The asset-set stamp identifies resources available at delivery, not proof that
every dependency executed. Third-party esptool remains pinned by version, not
attested by this stamp. No USB serial number or MAC is copied into provenance.

Each campaign snapshot records available server and agent provenance; client
source, version, recipe hash and operator declarations remain separate.
`interrupt_window(reason)` records the old epoch/cursor and any capture failure.
`open_window(reason, allow_quarantine=...)` opens a numbered, fresh window with
its new epoch/generation, without reviving old Board handles or asserting
continuity across the gap. Existing strict epoch checks still reject a silent
restart. An intentional quarantine can be observed in a new window; successful
campaign cleanup still requires a recovered card and revoked grant.

## Manifest and pytest orchestration

`CampaignManifest(document)` validates exact version-1 fields before any bench
operation. `Campaign(..., manifest=document, allow_destructive=True)` stores its
canonical SHA-256 and a copy. Fields: version, id, boards (id/chip/flash_size/
transports), write_regions (board/address/size), max_interruptions, backup_policy,
restore_policy, grant_seconds, operator_steps, start_criteria, close_criteria,
private_evidence. Criteria/policies are declarations to be asserted by pytest;
they are not executed as a second recipe language. Regions are disjoint whole
4-KiB sectors. The evidence directory must lie under the declared private root.
Every Board command validates target identity/transport and programming sectors;
whole-chip erase, including write-with-erase, requires a whole-chip permission.
These client constraints never replace the server scopes and physical grant.

`Orchestrator(campaign)` provides passive readiness/recovery/grant observation,
manual gesture announcements with an interruption budget, segmented verified
backups, explicitly consented programming with independent readback, and explicitly
consented restoration. No method starts a sensitive action merely because it was
listed in a manifest. Programming/readback requires both a remaining grant and reservation of
2 × timeout + 90 s; destructive Campaign reservations default to 1800 s;
renew reservations and ask the operator to extend grants between planned lots.
A large restoration validates the entire backup and authorised region first,
then programs independent aligned segments. Each write has its own consent flag
and readback. A failure stops the sequence; it is never resumed automatically.

Backups keep `.partial` files after interruption and only receive complete
metadata after the full read. Disposable server artifacts are removed after
verified local retention/settlement. Private backup files remain available for
restoration. The client does not atomically snapshot dynamic flash regions.
Each segment is flushed, synchronized with `fsync`, and read back locally before
deleting its server artifact. A synchronized `.partial.jsonl` records its region,
operation and digest; disk failures stop cleanup. Final binary and JSON files are
published without replacing existing evidence, with directory synchronization.
Backup names ending in `.json` are refused before hardware access because that
suffix is reserved for metadata. Interrupted files stay available for inspection;
they do not become a complete backup automatically. Restoration also checks each
segment against the hashes captured during full-file verification, so modifying
the source between segments stops the next write.
`Campaign.result(...)` records explicit recipe verdicts in append-only JSONL and
consolidated JSON/Markdown tables; duplicate IDs cannot replace old verdicts.
Never pass secret payloads as incident text or evidence labels.

The devcontainer uses Python 3.12 and Node 22, installs the client/pytest and
Chromium test prerequisites, and retains the Rust/ESP tooling. It no longer starts
an unrelated static server on the bench port. Start the actual bench explicitly
with an external credentials file. A named volume preserves BENCH_STATE_DIR.
Docker image construction itself still requires validation on a Docker host.

### Targeted trigger and process lifecycle improvements

An external client may disarm a trigger without renewing an expired/revoked grant,
using exactly `trigger` with `{"action":"disarm"}`. It still needs control scope,
the active lease that armed the trigger, the same generation and all continuity
checks. Both broker and agent check the lease association; rearming and sensitive
execution still require a grant. Quarantine and indeterminate outcomes are not
cleared by disarming.

`python bench/tools/run_server.py` is a foreground launcher. Send SIGHUP to its
printed PID to request a restart, or SIGTERM/SIGINT to stop. It terminates and
waits only for its own child and never spawns a replacement before that child has
exited. Shutdown timeout returns 124 without killing the remaining process or
starting another. There is no automatic crash restart. Lock waiting is bounded
and explicit (`--lock-timeout`, or BENCH_LOCK_TIMEOUT for direct startup); the
journal lock and database are retained. Tests use real isolated processes and
temporary journals, including delayed shutdown, timeout and lock contention. `tests/test_shutdown.py` covers the real
server and launcher with a connected agent WebSocket and an actively read UART event stream (the Q3 incident: before the
fix the server needed about 121 s to exit, the launcher refused the replacement after 30 s and exited 124).

Browser agent uploads use `X-Bench-Artifact-Role: agent`. For cookie sessions the
server binds this role to the authenticated session's connected WebSocket agent;
an API client cannot provide an arbitrary agent identity. Ordinary browser client
uploads keep client ownership. This protects readbacks before result association
and lets the operation's API owner clean them after settlement. Manual server
reconnection reuses the existing Agent and cancels its automatic retry, preserving
holds and never replaying an abandoned result or command.

`GET /api/v1/identity` exposes effective scopes without credentials. Campaign
preflight checks read/control and, for destructive runs, program. Parameters are
recorded by canonical SHA-256 only. After an uncertain submission loses its
session, `retire_after_recovery()` can retire the old handle only in a new,
locally recovered and unreserved session. The old outcome stays uncertified.
Generation changes now also interrupt Campaign event proof; preserve old recipes
and create a new consumer recipe version using explicit window boundaries.
`compare(board, backup)` performs the final independent segmented SHA-256 check.
