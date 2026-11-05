# Material requalification after reliability changes

**Status: NOT EXECUTED.** Software simulations do not qualify ESP32 hardware.
The historical recipes, PR #5 and its C3/S3 matrix remain preserved. New hardware
runs must use new recipe/evidence directories; do not edit their original proofs.
Run these steps separately for S3 UART, S3 USB Serial/JTAG where applicable, and C3
on its observed transport. A transport not available is BLOCKED, not PASS.

| ID | Preconditions and explicit action | Required evidence | Acceptance / restoration |
| --- | --- | --- | --- |
| R0 | Inspect USB IDs, chip, measured flash size, revisions; validate manifest | before snapshot, recipe/client/manifest/source hashes, observed vs declared fields | target matches; persistent journal enabled |
| R1 | Identify exact owned scratch erase sectors; back up all affected sectors privately | SHA-256, size, region, repeated immutable-region reads | verified reference before each batch |
| R2 | Operator grants 1800 s; reserve explicitly; attempt missing and present consent | exact HTTP/exception outcomes, grant lifetime, scopes | missing grant/scope/consent refuses before hardware |
| R3 | Upload known image; program only authorised sectors; read back without console return | upload and independent readback SHA-256, settled result, progress | exact byte equality; explicit console return only when safe |
| R4 | Upload disposable binaries near capacity; attempt extra upload; delete own unused data | inventory total/used/reserved, HTTP 503 capacity, cross-owner refusal | no limit increase; no deletion of active/uncertain artifacts |
| R5 | Stop/restart server before execute, during a deliberately authorised scratch write, after result receipt and before/after settlement | journal operation ID/state/result; each epoch window and generation | no replay; unknown stays unknown; known result preserved; holds preserved |
| R6 | Disconnect network/browser/USB during an authorised test operation | reconnect generation, zero grant, quarantine and no extra execute | local reconciliation and readback before programming resumes |
| R7 | Revoke/expire grant while running or before ready | timing and structured errors; no newly started programming | no automatic grant; preserve uncertainty where applicable |
| R8 | Read a large flash in bounded segments with console return disabled | per-segment size/SHA, whole backup hash, durations and operation/lease/grant budgets | complete data only; dynamic regions labelled; no silent retry |
| R9 | Explicitly restore every affected sector, independently read back and compare | restoration metadata and final equality, reference hash | firmware/reference restored, manual boot observation as required |
| R10 | Revoke locally and release; close campaign | final snapshot, cleanup, report table | connected/recovered card, no reservation, grant zero |

For a whole-flash backup, explicitly distinguish NVS and other firmware-mutated
regions. The initial loader entry can reset the MCU; `return_console=False` does
not prevent that first reset. Segmented reads are sequential observations, not an
atomic whole-flash snapshot. Abort on missing bytes or lost continuity. Budget
long UART transfers in separate explicit reservations/grants; the server's 600 s
per-operation ceiling still applies. Never silently resume a partial read.

Write the consumer's pytest tests using `CampaignManifest`, `Campaign` and
`Orchestrator`; no private HTTP reimplementation is needed. The manifest contains
**operator-approved** exact addresses and sizes, not example guessed partition
locations. `board.command(...)` is guarded by the manifest when reserved through
that campaign. pytest owns assertions and verdicts; report statuses distinguish
PASS / FAIL / SKIP / BLOCKED / INDETERMINATE, incidents and restoration limits.

Physical operator interventions still required: USB port selection and wiring,
identifying writable scratch sectors, local grants/revocation, deliberate power or
USB cuts, server stop/restart coordination, uncertain-outcome inspection/readback,
local recovery and any required boot/audio/firmware observation. Neither this
protocol nor a manifest is consent to perform those gestures remotely.
