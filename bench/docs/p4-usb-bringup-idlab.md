# ESP32-P4 USB bring-up in IDLab

Command: `diagnostic.p4_usb_bringup` (fixed `ohai_ping2` scenario).

Use the existing IDLab P4 ROM-only session on native USB-OTG, the exact
reviewed PR #11 JSON and a dedicated `BENCH_P4_BRINGUP_SHA256S` allowlist.
The JSON is SHA-checked and loaded using the already qualified
MEM_BEGIN / MEM_DATA / MEM_END path. After OHAI, send exactly two
separate PING payloads using the SAME BenchTransport and assert PONG
after each. The legacy trace opcode must not be sent.

Security: existing scope `control` + `program`, explicit RAM-code consent,
agent grant where applicable, board-generation check and broker quarantine.
Once RAM execution may have started, even on PASS, operator ROM recovery
is required; never automatically retry, reset or return to console.

The operation itself has no flash commands. Existing ROM-only chip
identification may query JEDEC; no claim of zero flash reads is made.

Hardware qualification remains separate: first run OHAI + PING/PONG x2,
then an explicitly approved external reset, ROM re-identification, and
a second run with the SAME firmware SHA. No hardware actions on this branch.
