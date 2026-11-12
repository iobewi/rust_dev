"""P4-ROM-MD5: pure analysis of the ROM-only MD5 measurements (no network, no bench access).

Measurements (loader without stub, SPI_FLASH_MD5 computed by the ROM): 1) first command of a loader session, 2) second command of the SAME session, 3) a command after a
NEW loader entry, taken only if 1 and 2 agree. The analysis says what was observed; the lines that compare it with the stub are hypotheses, never causes. A missing, malformed
or non-ROM answer is never a success.
"""
CAUTION = ("No cause is established. Equal or different MD5 of one range say nothing about which bytes differ, and a ROM answer is itself carried by the serial link. "
           "The stub's own variability comes from earlier runs (PC-7 v1: three distinct chip MD5 for the same range), not from this scenario.")
ROWS = {
    "ROM-A": "the ROM MD5 is identical in the three measurements: the ROM read path is stable here; with a variable stub MD5 this points at the stub's own read path "
             "(4-byte fast read for a flash larger than 16 MiB) or its initialisation",
    "ROM-B": "the two ROM MD5 of the same session differ: a variation exists inside one loader session on the ROM path too; a cause common to both paths "
             "(SPI configuration, power, hardware) is suspected",
    "ROM-C": "the two ROM MD5 of one session agree but the one after a new entry differs: the variation appears between loader entries on the ROM path too",
    "ROM-D": "a measurement is missing, malformed or was not taken without the stub: no conclusion",
}


def _hex32(value):
    return isinstance(value, str) and len(value) == 32 and all(c in "0123456789abcdef" for c in value)


def analyse_rom(observations):
    """observations: [{index, md5, format, loader, identify:{loader_stub, identify_index}}...] in the order taken. Returns {conclusion, rows, findings, flags, caution}."""
    findings, problems = [], []
    for position, o in enumerate(observations):
        if not _hex32(o.get("md5")):
            problems.append(f"measurement {position + 1}: not a valid 32-hex MD5")
        if o.get("format") != "rom-ascii32" or o.get("loader") != "rom" or (o.get("identify") or {}).get("loader_stub") != 0:
            problems.append(f"measurement {position + 1}: not a ROM answer without stub (format={o.get('format')}, loader={o.get('loader')}, "
                            f"loader_stub={(o.get('identify') or {}).get('loader_stub')})")
    n = len(observations)
    md5s = [o.get("md5") for o in observations]
    flags = {"measurements": n, "same_session_equal": None, "after_new_entry_equal": None}
    if n >= 2 and not problems:
        flags["same_session_equal"] = md5s[0] == md5s[1]
    if n >= 3 and not problems and flags["same_session_equal"]:
        flags["after_new_entry_equal"] = md5s[2] == md5s[0]
    rows = []
    if problems or n < 2:
        rows.append("ROM-D")
        findings += [{"id": "problem", "level": "observation", "text": t} for t in problems] or \
                    [{"id": "short", "level": "observation", "text": f"{n} measurement(s) taken, at least 2 expected"}]
        conclusion = "incomplete"
    elif not flags["same_session_equal"]:
        rows.append("ROM-B")
        conclusion = "discordance"
        findings.append({"id": "same_session", "level": "observation", "text": "the two ROM MD5 of the same loader session differ"})
    elif n == 2:
        rows.append("ROM-D")
        conclusion = "incomplete"
        findings.append({"id": "third_missing", "level": "observation", "text": "the first two agree: the measurement after a new entry was expected and is missing"})
    elif not flags["after_new_entry_equal"]:
        rows.append("ROM-C")
        conclusion = "discordance"
        findings.append({"id": "new_entry", "level": "observation", "text": "the ROM MD5 after a new loader entry differs from the first two"})
    else:
        rows.append("ROM-A")
        conclusion = "consistent"
        findings.append({"id": "all_equal", "level": "observation", "text": "the three ROM MD5 are identical"})
    findings += [{"id": r, "level": "hypothesis", "text": ROWS[r]} for r in rows if r != "ROM-D"]
    return {"conclusion": conclusion, "rows": rows, "findings": findings, "flags": flags, "caution": CAUTION}


def summary(analysis):
    return f"conclusion={analysis['conclusion']} rows={','.join(analysis['rows']) or '-'}"
