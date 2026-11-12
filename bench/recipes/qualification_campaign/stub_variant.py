"""P4-STUB-VARIANT: pure comparison of two series of measurements, the official stub and an experimental variant, both at the SAME UART speed (no network, no bench access).

Each series is the same sequence (chip MD5, read with the stub's own digest, chip MD5, read, chip MD5), one loader session per command. `integrity.analyse` classifies a series; this module
checks which stub actually ran (from the identity the agent reported) and compares the series with each other and with the MD5 the ROM gave for the range, when known.
Rows are hypotheses; no cause is named, and a missing or inconsistent identity is never a success.
"""
from .integrity import PLAN, analyse

CAUTION = ("No cause is established. One run, one range of 4 KiB, MD5 and digests carried by the same serial link. Both series run at the same UART speed; what differs is the stub "
           "(the variant skips the DCDC handoff and the CPU/APB boost on UART), so a difference between the series is attributed to that change only as a hypothesis.")
ROWS = {
    "SV-A": "the variant series is stable (and agrees with the ROM MD5 when one is given) while the official series is not: the transition removed in the variant is implicated, as a hypothesis",
    "SV-B": "both series are stable: the instability of the official stub was not reproduced in this run, so nothing can be compared",
    "SV-C": "the variant series is not stable either: removing the transition did not remove the instability in this run",
    "SV-D": "a measurement is missing, or the identity of the stub that ran is not the expected one: no conclusion",
    "SV-E": "the variant series is stable but its MD5 differs from the ROM MD5: stable is not the same as correct",
}


def _identity_problems(label, observations, expected_variant, expected_sha, expected_baud=None):
    problems = []
    for o in observations:
        ident = o.get("stub_identity") or {}
        if expected_baud is not None and ident.get("uart_baud") != expected_baud:
            problems.append(f"{label} #{o.get('index')}: UART speed is {ident.get('uart_baud')}, expected {expected_baud} (the two series must run at the same speed)")
        if ident.get("variant") != expected_variant:
            problems.append(f"{label} #{o.get('index')}: stub identity is {ident.get('variant')!r}, expected {expected_variant!r}")
        elif expected_variant != "official" and (ident.get("verified") is not True or ident.get("file_sha256") != expected_sha):
            problems.append(f"{label} #{o.get('index')}: the variant is not reported as verified against {str(expected_sha)[:12]}")
        if o.get("kind") == "chip_md5" and not (o.get("ram_load_ops") or 0) > 0:
            problems.append(f"{label} #{o.get('index')}: no stub upload seen on the wire (ram_load_ops={o.get('ram_load_ops')})")
    return problems


def _deviations(read, truth):
    """Positions where a read differs from the ground truth, their bits and the position inside the 16-byte chunk of the stub's SPI read. No content is kept."""
    positions = [i for i, (a, b) in enumerate(zip(read, truth)) if a != b]
    lanes = {}
    for i in positions:
        lanes[i % 16] = lanes.get(i % 16, 0) + 1
    late = [i for i in positions if i >= 32]
    return {"deviations": len(positions), "bits": sum(bin(read[i] ^ truth[i]).count("1") for i in positions), "positions": [f"{i:#x}" for i in positions[:64]],
            "lane_mod16": dict(sorted(lanes.items())), "late_deviations": len(late), "late_all_in_lanes_1_3": bool(late) and all(i % 16 in (1, 2, 3) for i in late)}


def byte_comparison(reads, reference_md5):
    """reads: {series: [bytes...]} in the order taken. The ground truth is the first read whose MD5 equals the ROM's; without one, nothing is said about correctness."""
    import hashlib
    truth = next((r for rs in reads.values() for r in rs if reference_md5 and hashlib.md5(r).hexdigest() == reference_md5), None)
    out = {"truth_available": truth is not None, "series": {}}
    for label, rs in reads.items():
        out["series"][label] = [{"index": n, "equals_truth": (None if truth is None else r == truth), **(_deviations(r, truth) if truth is not None and len(r) == len(truth) else {})}
                                for n, r in enumerate(rs)]
    return out


def compare(official, variant, *, variant_name, expected_sha256, reference_md5=None, reads=None, expected_baud=115200):
    """official / variant: lists of observations in the order taken. `reads`: optional {series: [bytes]} for the byte-level view. Returns {conclusion, rows, series, findings, caution}."""
    series = {"official": analyse(official, PLAN), "variant": analyse(variant, PLAN)}
    problems = (_identity_problems("official", official, "official", None, expected_baud) + _identity_problems("variant", variant, variant_name, expected_sha256, expected_baud))
    md5s = lambda obs: {o["md5"] for o in obs if o.get("kind") == "chip_md5" and o.get("md5")} | {o["md5_client"] for o in obs if o.get("kind") == "read" and o.get("md5_client")}      # noqa: E731
    v_equals_ref = None if reference_md5 is None else (md5s(variant) == {reference_md5})
    o_equals_ref = None if reference_md5 is None else (md5s(official) == {reference_md5})
    findings, rows = [], []
    complete = all(a["complete"] for a in series.values()) and not problems
    v_ok = series["variant"]["conclusion"] == "consistent"
    o_ok = series["official"]["conclusion"] == "consistent"
    if not complete:
        rows.append("SV-D")
        findings += [{"id": "problem", "level": "observation", "text": t} for t in problems]
        conclusion = "incomplete"
    elif v_ok and v_equals_ref is False:
        rows.append("SV-E")
        conclusion = "discordance"
    elif v_ok and not o_ok:
        rows.append("SV-A")
        conclusion = "variant-stable"
    elif v_ok and o_ok:
        rows.append("SV-B")
        conclusion = "reference-not-reproduced"
    else:
        rows.append("SV-C")
        conclusion = "discordance"
    findings.append({"id": "series", "level": "observation", "text": f"official: {series['official']['conclusion']}; variant: {series['variant']['conclusion']}; "
                     f"variant equals the ROM MD5: {v_equals_ref}; official equals the ROM MD5: {o_equals_ref}"})
    findings += [{"id": r, "level": "hypothesis", "text": ROWS[r]} for r in rows if r != "SV-D"]
    result = {"conclusion": conclusion, "rows": rows, "series": series, "findings": findings, "caution": CAUTION,
              "variant_equals_reference": v_equals_ref, "official_equals_reference": o_equals_ref}
    if reads:
        result["bytes"] = byte_comparison(reads, reference_md5)
        for label, rows_ in result["bytes"]["series"].items():
            late = [r for r in rows_ if r.get("late_deviations")]
            if late:
                result["findings"].append({"id": f"lanes-{label}", "level": "observation", "text": f"{label}: {sum(r['late_deviations'] for r in late)} deviation(s) beyond the first 32 bytes; "
                                           f"all in positions 1-3 of their 16-byte chunk in every read: {all(r['late_all_in_lanes_1_3'] for r in late)}"})
    return result


def summary(result):
    return f"conclusion={result['conclusion']} rows={','.join(result['rows']) or '-'}"
