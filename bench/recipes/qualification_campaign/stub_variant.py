"""P4-STUB-VARIANT: pure comparison of two series of measurements, the official stub and an experimental variant (no network, no bench access).

Each series is the same short sequence (chip MD5, read with the stub's own digest, chip MD5), one loader session per command. `integrity.analyse` classifies a series; this module
checks which stub actually ran (from the identity the agent reported) and compares the series with each other and with the MD5 the ROM gave for the range, when known.
Rows are hypotheses; no cause is named, and a missing or inconsistent identity is never a success.
"""
from .integrity import PLAN_SESSION, analyse

CAUTION = ("No cause is established. One run, one range of 4 KiB, MD5 and digests carried by the same serial link. The official series runs at the agent's default UART baud rate and the "
           "variant at 115200: a difference between the series is not attributed to the clock change alone.")
ROWS = {
    "SV-A": "the variant series is stable (and agrees with the ROM MD5 when one is given) while the official series is not: the transition removed in the variant is implicated, as a hypothesis",
    "SV-B": "both series are stable: the instability of the official stub was not reproduced in this run, so nothing can be compared",
    "SV-C": "the variant series is not stable either: removing the transition did not remove the instability in this run",
    "SV-D": "a measurement is missing, or the identity of the stub that ran is not the expected one: no conclusion",
    "SV-E": "the variant series is stable but its MD5 differs from the ROM MD5: stable is not the same as correct",
}


def _identity_problems(label, observations, expected_variant, expected_sha):
    problems = []
    for o in observations:
        ident = o.get("stub_identity") or {}
        if ident.get("variant") != expected_variant:
            problems.append(f"{label} #{o.get('index')}: stub identity is {ident.get('variant')!r}, expected {expected_variant!r}")
        elif expected_variant != "official" and (ident.get("verified") is not True or ident.get("file_sha256") != expected_sha):
            problems.append(f"{label} #{o.get('index')}: the variant is not reported as verified against {str(expected_sha)[:12]}")
        if o.get("kind") == "chip_md5" and not (o.get("ram_load_ops") or 0) > 0:
            problems.append(f"{label} #{o.get('index')}: no stub upload seen on the wire (ram_load_ops={o.get('ram_load_ops')})")
    return problems


def compare(official, variant, *, variant_name, expected_sha256, reference_md5=None):
    """official / variant: lists of observations in the order taken. Returns {conclusion, rows, series, findings, caution}."""
    series = {"official": analyse(official, PLAN_SESSION), "variant": analyse(variant, PLAN_SESSION)}
    problems = _identity_problems("official", official, "official", None) + _identity_problems("variant", variant, variant_name, expected_sha256)
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
    return {"conclusion": conclusion, "rows": rows, "series": series, "findings": findings, "caution": CAUTION,
            "variant_equals_reference": v_equals_ref, "official_equals_reference": o_equals_ref}


def summary(result):
    return f"conclusion={result['conclusion']} rows={','.join(result['rows']) or '-'}"
