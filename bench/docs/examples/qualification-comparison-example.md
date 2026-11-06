<!-- Generated from SIMULATED fixtures (python/tests/qualification_fixtures.py) to show the report format. It is NOT evidence about any card.
     The tool and criteria hashes below change whenever qualification_compare.py or qualification_criteria.json change. -->

# Qualification comparison report

Tool v1 (sha256 `3c3177ec30a60129`) - criteria sha256 `2dfc47e20ff7e4e2`

> **SIMULATED EVIDENCE** - produced from synthetic fixtures; it shows the report format, it qualifies nothing.

## Runs

| root | role | run | scenario | context | recorded | effective | verification |
|---|---|---|---|---|---|---|---|
| hist | historic | Q1-20261105T080000Z | Q1 | esp32s3/external_uart | PASS | **INDETERMINATE** | UNVERIFIED |
| hist | historic | Q2a-20261105T090000Z | Q2a | esp32s3/external_uart | FAIL | **FAIL** | VERIFIED |
| hist | historic | Q3-20261105T091000Z | Q3 | esp32s3/external_uart | INDETERMINATE | **INDETERMINATE** | UNVERIFIED |
| repeat-s3 | candidate | Q1-20261106110100Z | Q1 | esp32s3/external_uart | PASS | **PASS** | VERIFIED |
| repeat-s3 | candidate | Q2a-20261106110000Z | Q2a | esp32s3/external_uart | PASS | **PASS** | VERIFIED |
| repeat-s3 | candidate | Q2a-20261106110001Z | Q2a | esp32s3/external_uart | PASS | **PASS** | VERIFIED |
| repeat-s3 | candidate | Q2a-20261106110002Z | Q2a | esp32s3/external_uart | PASS | **PASS** | VERIFIED |
| repeat-s3 | candidate | Q2b-20261106110100Z | Q2b | esp32s3/external_uart | PASS | **PASS** | VERIFIED |
| repeat-s3 | candidate | Q2c-20261106110200Z | Q2c | esp32s3/external_uart | PASS | **PASS** | VERIFIED |
| repeat-s3 | candidate | Q2c-20261106110201Z | Q2c | esp32s3/external_uart | PASS | **PASS** | VERIFIED |
| repeat-s3 | candidate | Q2d-20261106110100Z | Q2d | esp32s3/external_uart | PASS | **PASS** | VERIFIED |
| repeat-s3 | candidate | Q3-20261106110100Z | Q3 | esp32s3/external_uart | PASS | **PASS** | VERIFIED |
| repeat-c3 | candidate | C3R-0-20261106120000Z | C3R-0 | esp32c3/usb_serial_jtag | PASS | **PASS** | VERIFIED |
| repeat-c3 | candidate | C3R-1-20261106120000Z | C3R-1 | esp32c3/usb_serial_jtag | PASS | **PASS** | VERIFIED |
| repeat-c3 | candidate | C3R-2-20261106120000Z | C3R-2 | esp32c3/usb_serial_jtag | PASS | **PASS** | VERIFIED |
| repeat-c3 | candidate | C3R-3-20261106120000Z | C3R-3 | esp32c3/usb_serial_jtag | PASS | **PASS** | VERIFIED |
| repeat-c3 | candidate | Q1-20261106120000Z | Q1 | esp32c3/usb_serial_jtag | PASS | **PASS** | VERIFIED |
| repeat-c3 | candidate | Q2a-20261106120000Z | Q2a | esp32c3/usb_serial_jtag | PASS | **PASS** | VERIFIED |
| repeat-c3 | candidate | Q2b-20261106120000Z | Q2b | esp32c3/usb_serial_jtag | PASS | **PASS** | VERIFIED |
| repeat-c3 | candidate | Q2c-20261106120000Z | Q2c | esp32c3/usb_serial_jtag | PASS | **PASS** | VERIFIED |
| repeat-c3 | candidate | Q2d-20261106120000Z | Q2d | esp32c3/usb_serial_jtag | PASS | **PASS** | VERIFIED |
| repeat-c3 | candidate | Q3-20261106120000Z | Q3 | esp32c3/usb_serial_jtag | PASS | **PASS** | VERIFIED |

## Requirement matrix

### REQ-01 - Complete destructive cycle through Campaign/Orchestrator: verified backups, programming, independent readback, restoration, final compare, clean close
- `esp32c3/usb_serial_jtag`: functional **DEMONSTRATED**, repeatable **NOT_DEMONSTRATED**
  - Q1: PASS=1; candidate PASS runs 1; stable True
- `esp32s3/external_uart`: functional **DEMONSTRATED**, repeatable **NOT_DEMONSTRATED**
  - Q1: PASS=1, INDETERMINATE=1; candidate PASS runs 1; stable True

### REQ-02 - A grant that expires naturally cannot be re-armed; the owner can still disarm; no grant is re-created
- `esp32c3/usb_serial_jtag`: functional **DEMONSTRATED**, repeatable **NOT_DEMONSTRATED**
  - Q2a: PASS=1; candidate PASS runs 1; stable True
- `esp32s3/external_uart`: functional **DEMONSTRATED**, repeatable **DEMONSTRATED**
  - Q2a: PASS=3, FAIL=1; candidate PASS runs 3; stable True

### REQ-03 - A local revocation is distinguished from an expiry and has the same consequences
- `esp32c3/usb_serial_jtag`: functional **DEMONSTRATED**, repeatable **NOT_DEMONSTRATED**
  - Q2b: PASS=1; candidate PASS runs 1; stable True
- `esp32s3/external_uart`: functional **DEMONSTRATED**, repeatable **NOT_DEMONSTRATED**
  - Q2b: PASS=1; candidate PASS runs 1; stable True

### REQ-04 - Ownership: a new reservation or another identity cannot disarm a trigger, with a VALID grant (not a grant refusal)
- `esp32c3/usb_serial_jtag`: functional **DEMONSTRATED**, repeatable **NOT_DEMONSTRATED**
  - Q2c: PASS=1; candidate PASS runs 1; stable True
  - Q2d: PASS=1; candidate PASS runs 1; stable True
- `esp32s3/external_uart`: functional **DEMONSTRATED**, repeatable **NOT_DEMONSTRATED**
  - Q2c: PASS=2; candidate PASS runs 2; stable False
  - Q2d: PASS=1; candidate PASS runs 1; stable True

### REQ-05 - Coordinated server restart at rest: ordered stop/start, new epoch, same durable journal, agent reconnects without replay, fresh proof window
- `esp32c3/usb_serial_jtag`: functional **DEMONSTRATED**, repeatable **NOT_DEMONSTRATED**
  - Q3: PASS=1; candidate PASS runs 1; stable True
- `esp32s3/external_uart`: functional **DEMONSTRATED**, repeatable **NOT_DEMONSTRATED**
  - Q3: PASS=1, INDETERMINATE=1; candidate PASS runs 1; stable True

### REQ-06 - Read-only identification, repeatable reads and partition table of a C3, with declared transport and agent inference both recorded
- `esp32c3/usb_serial_jtag`: functional **DEMONSTRATED**, repeatable **NOT_DEMONSTRATED**
  - C3R-0: PASS=1; candidate PASS runs 1; stable True
  - C3R-1: PASS=1; candidate PASS runs 1; stable True
  - C3R-2: PASS=1; candidate PASS runs 1; stable True

### REQ-07 - One explicit reset with observation of the ROM banner in the same proof window
- `esp32c3/usb_serial_jtag`: functional **DEMONSTRATED**, repeatable **NOT_DEMONSTRATED**
  - C3R-3: PASS=1; candidate PASS runs 1; stable True

## Repeatability (candidate passes only)

- `esp32c3/usb_serial_jtag` C3R-0: 1 run(s), stable **True**
- `esp32c3/usb_serial_jtag` C3R-1: 1 run(s), stable **True**
- `esp32c3/usb_serial_jtag` C3R-2: 1 run(s), stable **True**
- `esp32c3/usb_serial_jtag` C3R-3: 1 run(s), stable **True**
- `esp32c3/usb_serial_jtag` Q1: 1 run(s), stable **True**
- `esp32c3/usb_serial_jtag` Q2a: 1 run(s), stable **True**
- `esp32c3/usb_serial_jtag` Q2b: 1 run(s), stable **True**
- `esp32c3/usb_serial_jtag` Q2c: 1 run(s), stable **True**
- `esp32c3/usb_serial_jtag` Q2d: 1 run(s), stable **True**
- `esp32c3/usb_serial_jtag` Q3: 1 run(s), stable **True**
- `esp32s3/external_uart` Q1: 1 run(s), stable **True**
- `esp32s3/external_uart` Q2a: 3 run(s), stable **True**
- `esp32s3/external_uart` Q2b: 1 run(s), stable **True**
- `esp32s3/external_uart` Q2c: 2 run(s), stable **False**; drift: ["Q2c-20261106110201Z: behaviour.commands: ['trigger'] != ['trigger', 'mark']"]
- `esp32s3/external_uart` Q2d: 1 run(s), stable **True**
- `esp32s3/external_uart` Q3: 1 run(s), stable **True**

## Inter-card behaviour

- Q1 ['esp32c3/usb_serial_jtag', 'esp32s3/external_uart']: same behaviour
- Q2a ['esp32c3/usb_serial_jtag', 'esp32s3/external_uart']: same behaviour
- Q2b ['esp32c3/usb_serial_jtag', 'esp32s3/external_uart']: same behaviour
- Q2c ['esp32c3/usb_serial_jtag', 'esp32s3/external_uart']: same behaviour
- Q2d ['esp32c3/usb_serial_jtag', 'esp32s3/external_uart']: same behaviour
- Q3 ['esp32c3/usb_serial_jtag', 'esp32s3/external_uart']: same behaviour

## Evidence problems (never hidden)

- Q1-20261105T080000Z (PASS -> INDETERMINATE, UNVERIFIED): files_present: missing: provenance.json; support_hashes: no support_sha256 in provenance.json; provenance: provenance.json absent (run predates the provenance contract)
- Q3-20261105T091000Z (INDETERMINATE -> INDETERMINATE, UNVERIFIED): files_present: missing: after.json

## Not covered

- **fault_injection**: Resistance to interruptions and faults (USB/power cut, browser/server loss during a write): NOT qualified
- **endurance**: Endurance and statistical repeatability beyond the planned passes: NOT qualified
- **consumer_firmware**: Behaviour of a consumer firmware, application boot (boot_verified is always false by design), OTA: out of the bench scope
- **certification**: External certification: not engaged
