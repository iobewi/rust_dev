# Reliability audit and software delivery — 2026-11-05

Reference main: `7e7595baeec060184c99d0e3e9fe44a6fef8dcbc`.
Only main and `feat/2-c3-qualification` existed at the initial inventory. PR #5
and issue #2 remain open; neither is a completed physical qualification.
The five reliability PRs are stacked and must be reviewed/merged in order, with
explicit user validation. No merge, physical write or rust-ota change was made.

## Confirmed defects and corrections

| Priority / defect | Confirmed cause | Correction | Regression evidence |
| --- | --- | --- | --- |
| P0.1 destructive campaigns | Campaign asserted grant zero and dropped consent | explicit campaign and per-command consent, effective scopes, passive grant observation, final revocation check | test_reliability, test_orchestration, test_core grant/scope cases |
| P0.2 upload | installable client had no upload method | bounded file/buffer upload with SHA-256/response checks; no retries | invalid/maximum size, path/buffer, digest refusal, API roundtrip |
| P0.3 saturation / cleanup | 64 MiB RAM store with TTL only; indeterminate inputs could expire | reserved capacity, owned deletion, active/uncertain pins, capacity/503 diagnostics | storage HTTP/core tests, uncertain retention, owner changes across browser sessions |
| P0.4 lost result | broker state lived only in RAM | private synchronous SQLite checkpoint before execute/result ACK; strict restart matrix | test_journal: before/during/after result, settlement, disk error, double instance |
| P1.1 exceptions | interruption assertions expected the wrong definitive failure | documented distinctions; acknowledged indeterminate remains PendingOperation | client uncertainty/settlement tests |
| P1.2 flash read | return_console was absent from Python API | explicit loader session option and console return | command argument/API roundtrip tests |
| P1.3 evidence windows | one implicit epoch/generation assumption | explicit interruption/opening, old handles guarded, explicit retirement after local recovery | window/generation/retirement tests |
| P1.4 reconnect | closed WS stayed disconnected | wait for hardware, fresh generation, no replay/grant, preserve quarantine | Node reconnect tests and real Chromium/WebSocket recovery scenario |
| P1.5 provenance | server/page/client/transport origins incomplete | source and module stamps, client/recipe/parameter hashes, WebSerial IDs and qualified inference | HTTP/provenance/window tests |
| P2.1 manifest | no generic preflight constraints | validated versioned manifest, sector bounds, interruption budget and declared policies | invalid schema, overlaps, whole-chip erase refusal, target checks |
| P2.2 orchestration | recipes reimplemented HTTP and sequencing | explicit backup/program/readback/restore/compare primitives; pytest owns verdict | end-to-end RAM agent API sequence |
| P2.3 large flash | capacity/lifetimes/segmentation difficult to coordinate | bounded segments, verified local retention, coherent grant/lease budgets, no partial resume | maximum 16 MiB artifact, segmented backup and restoration tests |
| P2.4 reports | incomplete common status vocabulary | append-only verdicts, consolidated JSON/Markdown, AGENTS rules | duplicate-verdict refusal and report checks |
| P2.5 devcontainer | Debian defaults did not meet Python/Node requirements; wrong static service on 8080 | Python 3.12, Node 22, npm lock, pytest/Chromium setup, persistent journal volume | source/config reviewed; Docker image build still unexecuted |

A detached checkout of the original main also reproduced the missing public upload,
missing Campaign consent parameter, missing return_console parameter and actual
deletion of a programming input after an indeterminate disconnect plus >1 h TTL.
No historic recipe, private proof or flash dump was changed or committed.

## Validation

Local final software suite: **73 pytest PASS**, **9 opt-in hardware tests SKIP**,
**71 server unittest PASS**, **102 Node tests PASS**. Existing browser scenarios
and a fifth real WebSocket recovery scenario run in GitHub CI, with simulated
hardware. See PR checks for exact commit/run results. Local Chromium installation
was unavailable (download returned a truncated archive); no local browser PASS
is claimed. Docker is absent locally; no Docker image build PASS is claimed.

PRs: [#6](https://github.com/iobewi/rust_dev/pull/6) client;
[#7](https://github.com/iobewi/rust_dev/pull/7) artifacts;
[#8](https://github.com/iobewi/rust_dev/pull/8) durability;
[#9](https://github.com/iobewi/rust_dev/pull/9) browser/provenance;
[#10](https://github.com/iobewi/rust_dev/pull/10) orchestration/devcontainer.

## Remaining acceptance

Physical requalification remains **NOT EXECUTED** for S3/C3 and each available
transport. Follow [the explicit protocol](../docs/requalification-reliability.md)
in fresh consumer recipe/evidence directories. Operator-approved scratch sectors,
verified reference backups, local grants/revocation, deliberate cuts/restarts,
manual reconciliation and final restoration checks are still required.
The Docker image also needs a build on a Docker-capable host. The software work
does not imply hardware qualification, certification or permission to write.
