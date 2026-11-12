"""P4-FLASH-INTEGRITY: pure analysis of the measurements (no network, no bench access).

Measurements, in the order the recipe takes them: the MD5 of the range computed by the chip (SPI_FLASH_MD5, `chip_md5`), then reads of the same range
(`read`), each with the MD5 of the received bytes computed by the client and the MD5 that the stub sends after the data (`stub`, computed on the bytes
it read from flash BEFORE sending them). Planned sequence: chip_md5, read, chip_md5, read, chip_md5.

The result separates what was observed from what is only possible. It never names a cause: a UART fault, a flash fault or a host fault is, at most,
a hypothesis written as such, and an empty or missing value is never a success.
"""
from collections import Counter

PLAN = ("chip_md5", "read", "chip_md5", "read", "chip_md5")
PLAN_SESSION = ("chip_md5", "read", "chip_md5")          # P4-FLASH-INTEGRITY-SESSION: the three commands share ONE loader session
CAUTION = ("No cause is established by this analysis. The stub's MD5 and the chip's MD5 are themselves carried by the serial link, and different commands "
           "may read the flash through different paths: one discordance alone never proves a UART fault or a flash fault.")

# Grid rows (docs/p4-flash-integrity.md)
ROWS = {
    "R1": "stub MD5 stable while the MD5 of the received bytes varies: a corruption of the transfer or of the host processing is strongly suspected",
    "R2": "stub MD5 variable: an instability upstream of the transfer, or in the stub's computation, is possible",
    "R3": "chip MD5 (SPI_FLASH_MD5) stable while READ_FLASH varies: a difference between read paths, or a corruption during the transfer",
    "R4": "every MD5 and every received byte agree: the integrity of THIS observation is established, nothing more",
    "R5": "an MD5 is missing or failed: incomplete diagnostic, no final conclusion",
}


def _hex32(value):
    return isinstance(value, str) and len(value) == 32 and all(c in "0123456789abcdef" for c in value)


def analyse(observations, plan=PLAN):
    """`plan` is the planned sequence of the scenario. Returns {complete, conclusion, rows, findings, flags, caution}. `conclusion`: incomplete | consistent | discordance."""
    chips = [o for o in observations if o.get("kind") == "chip_md5"]
    reads = [o for o in observations if o.get("kind") == "read"]
    findings, missing = [], []

    expected = Counter(plan)
    if len(chips) < expected["chip_md5"] or len(reads) < expected["read"]:
        missing.append(f"planned {expected['chip_md5']} chip MD5 and {expected['read']} reads, got {len(chips)} and {len(reads)}")
    for o in chips:
        if not _hex32(o.get("md5")):
            missing.append(f"chip MD5 #{o.get('index')} is not a valid 32-hex digest")
    for o in reads:
        if not _hex32(o.get("md5_client")):
            missing.append(f"read #{o.get('index')} has no client MD5")
        stub = o.get("stub")
        if not isinstance(stub, dict):
            missing.append(f"read #{o.get('index')}: the agent reported no stub digest field")
        elif stub.get("state") != "received":
            missing.append(f"read #{o.get('index')}: stub digest {stub.get('state', 'unknown')}")
        elif not _hex32(stub.get("md5")):
            missing.append(f"read #{o.get('index')}: stub digest received but not a valid 32-hex digest")

    chip_md5s = [o["md5"] for o in chips if _hex32(o.get("md5"))]
    client_md5s = [o["md5_client"] for o in reads if _hex32(o.get("md5_client"))]
    stub_md5s = [o["stub"]["md5"] for o in reads if isinstance(o.get("stub"), dict) and o["stub"].get("state") == "received" and _hex32(o["stub"].get("md5"))]

    reads_vary = len(set(client_md5s)) > 1
    chip_varies = len(set(chip_md5s)) > 1
    stub_varies = len(set(stub_md5s)) > 1
    stub_mismatch = [o["index"] for o in reads if isinstance(o.get("stub"), dict) and o["stub"].get("state") == "received"
                     and _hex32(o["stub"].get("md5")) and o["stub"]["md5"] != o.get("md5_client")]
    chip_mismatch = []
    for position, o in enumerate(reads):
        neighbours = {c["md5"] for c in chips if c.get("index") in (o["index"] - 1, o["index"] + 1) and _hex32(c.get("md5"))}
        if neighbours and o.get("md5_client") not in neighbours:
            chip_mismatch.append(o["index"])

    flags = {"reads_vary": reads_vary, "chip_md5_varies": chip_varies, "stub_md5_varies": stub_varies,
             "stub_md5_differs_from_received": stub_mismatch, "chip_md5_differs_from_received": chip_mismatch}
    if reads_vary:
        findings.append({"id": "reads_vary", "level": "observation", "text": f"the received bytes differ between reads ({len(set(client_md5s))} distinct MD5)"})
    if chip_varies:
        findings.append({"id": "chip_md5_varies", "level": "observation", "text": f"the MD5 computed by the chip varies ({len(set(chip_md5s))} distinct values)"})
    if stub_varies:
        findings.append({"id": "stub_md5_varies", "level": "observation", "text": f"the MD5 sent by the stub varies ({len(set(stub_md5s))} distinct values)"})
    if stub_mismatch:
        findings.append({"id": "stub_md5_differs_from_received", "level": "observation", "text": f"read(s) {stub_mismatch}: the stub's MD5 differs from the MD5 of the received bytes"})
    if chip_mismatch:
        findings.append({"id": "chip_md5_differs_from_received", "level": "observation", "text": f"read(s) {chip_mismatch}: the received bytes match neither adjacent chip MD5"})

    rows = []
    complete = not missing
    if missing:
        rows.append("R5")
        findings += [{"id": "missing", "level": "observation", "text": m} for m in missing]
        conclusion = "incomplete"
    else:
        if not (reads_vary or chip_varies or stub_varies or stub_mismatch or chip_mismatch):
            rows.append("R4")
            conclusion = "consistent"
        else:
            conclusion = "discordance"
            if reads_vary and not stub_varies:
                rows.append("R1")
            if stub_varies:
                rows.append("R2")
            if reads_vary and not chip_varies:
                rows.append("R3")
            if not rows:
                findings.append({"id": "unclassified", "level": "observation", "text": "a discordance that no grid row describes: examine the raw measurements"})
    for row in rows:
        findings.append({"id": row, "level": "hypothesis" if row != "R4" else "observation", "text": ROWS[row]})
    return {"complete": complete, "conclusion": conclusion, "rows": rows, "findings": findings, "flags": flags, "caution": CAUTION}


def summary(analysis):
    """One line for the verdict."""
    return f"conclusion={analysis['conclusion']} rows={','.join(analysis['rows']) or '-'} flags=" + ",".join(
        k for k, v in analysis["flags"].items() if v)
