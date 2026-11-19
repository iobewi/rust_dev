#!/usr/bin/env python3
"""Analysis of the P4 USB OTG read-profiling campaign (A, D, instrumented trace, A again).

Pure functions over the evidence collected by the browser tooling (see RECIPE.md). Nothing here
reaches a device. Cycles (from the stub) and microseconds (converted here) are kept in separate
fields, and `fill_cycles` is a SUBSET of `send_cycles`: it is never added to it.

    python3 analyze_trace.py evidence.json --cpu-hz 200000000
"""
import json
import math
import sys

SCHEMA = "idlab-p4-usb-trace-evidence/1"
CYCLE_KEYS = ("flash_cycles", "md5_cycles", "send_cycles", "ack_cycles", "fill_cycles")
STABILITY_LIMIT = 0.10       # two standard reads (A before / A after) must agree within 10 %
CLOCK_RATIO_RANGE = (0.8, 1.2)   # counted cycles over wall time must match the assumed CPU clock
DOMINANT_SHARE = 0.5         # a phase is "dominant" above half of the counted cycles


def _runs(evidence):
    if evidence.get("schema") != SCHEMA:
        raise ValueError(f"unknown evidence schema {evidence.get('schema')!r}")
    runs = {r["label"]: r for r in evidence["runs"]}
    for label in ("A1", "D", "T", "A2"):
        if label not in runs:
            raise ValueError(f"missing run {label}")
    return runs


def integrity(runs):
    """Every data run must carry the same bytes; the MD5 of D must equal them too."""
    problems = []
    data_hashes = {r["label"]: r["sha256"] for r in runs.values() if r.get("sha256")}
    if len(set(data_hashes.values())) > 1:
        problems.append({"kind": "sha256_differs", "hashes": data_hashes})
    md5 = {label: r["md5"] for label, r in runs.items() if r.get("md5")}
    if len(set(md5.values())) > 1:
        problems.append({"kind": "md5_differs", "md5": md5})
    for label in ("A1", "A2", "T"):
        if not runs[label].get("sha256"):
            problems.append({"kind": "missing_sha256", "run": label})
    return {"ok": not problems, "problems": problems}


def stability(runs):
    a1, a2 = runs["A1"]["duration_ms"], runs["A2"]["duration_ms"]
    spread = abs(a1 - a2) / max(a1, a2)
    return {"ok": spread <= STABILITY_LIMIT, "relative_spread": round(spread, 4), "limit": STABILITY_LIMIT}


def trace_consistency(run, region):
    trace = {k: int(run["trace"][k]) for k in CYCLE_KEYS}
    trace.update({k: int(run["trace"][k]) for k in ("fill_calls", "packets", "ack_count", "bytes")})
    packets = math.ceil(region["size"] / region["packet_size"])
    problems = []
    if trace["bytes"] != region["size"]:
        problems.append("bytes differs from the region size")
    if trace["packets"] != packets:
        problems.append(f"packets {trace['packets']} != expected {packets}")
    if trace["ack_count"] != trace["packets"]:
        problems.append("ack_count differs from packets")
    if trace["fill_cycles"] > trace["send_cycles"]:
        problems.append("fill_cycles exceeds send_cycles although it is a subset of it")
    if any(trace[k] <= 0 for k in CYCLE_KEYS):
        problems.append("a cycle counter is zero or negative: the counter was not running")
    return trace, {"ok": not problems, "problems": problems}


def analyze(evidence, cpu_hz):
    """Returns the full report. `cpu_hz` is an explicit input: nothing is assumed silently."""
    runs, region = _runs(evidence), evidence["region"]
    report = {"schema": SCHEMA, "region": region, "cpu_hz_used": cpu_hz,
              "integrity": integrity(runs), "stability": stability(runs)}
    trace, consistency = trace_consistency(runs["T"], region)
    report["trace_consistency"] = consistency
    report["cycles"] = trace  # raw counters, exactly as reported by the stub
    if not (report["integrity"]["ok"] and consistency["ok"]):
        report["verdict"] = "INVALID"
        report["reason"] = "data integrity or trace consistency failed: no conclusion is drawn from the timing"
        return report
    counted = sum(trace[k] for k in ("flash_cycles", "md5_cycles", "send_cycles", "ack_cycles"))
    to_us = lambda cycles: cycles * 1_000_000 / cpu_hz  # noqa: E731
    send_wo_fill = trace["send_cycles"] - trace["fill_cycles"]
    report["microseconds"] = {
        "flash": to_us(trace["flash_cycles"]), "md5": to_us(trace["md5_cycles"]),
        "send_total": to_us(trace["send_cycles"]), "fill_within_send": to_us(trace["fill_cycles"]),
        "send_without_fill": to_us(send_wo_fill), "ack_wait": to_us(trace["ack_cycles"]),
        "counted_total": to_us(counted)}
    wall_s = runs["T"]["duration_ms"] / 1000
    implied = counted / wall_s
    ratio = implied / cpu_hz
    report["clock_cross_check"] = {
        "implied_cpu_hz": implied, "ratio_to_assumed": round(ratio, 3),
        "ok": CLOCK_RATIO_RANGE[0] <= ratio <= CLOCK_RATIO_RANGE[1],
        "meaning": "counted cycles / wall time of the traced run: must be close to the CPU clock if the "
                   "counter runs at the assumed frequency and the loop dominates the run"}
    shares = {"flash": trace["flash_cycles"] / counted, "md5": trace["md5_cycles"] / counted,
              "send_without_fill": send_wo_fill / counted, "fill": trace["fill_cycles"] / counted,
              "ack_wait": trace["ack_cycles"] / counted}
    report["shares_of_counted_cycles"] = {k: round(v, 4) for k, v in shares.items()}
    a_mean = (runs["A1"]["duration_ms"] + runs["A2"]["duration_ms"]) / 2
    excess_ms = a_mean - runs["D"]["duration_ms"]
    attributed_ms = (to_us(trace["send_cycles"]) + to_us(trace["ack_cycles"])) / 1000
    report["excess_over_md5_only"] = {
        "standard_mean_ms": a_mean, "md5_only_ms": runs["D"]["duration_ms"], "excess_ms": excess_ms,
        "send_plus_ack_ms": attributed_ms, "unattributed_ms": excess_ms - attributed_ms}
    phase, share = max(shares.items(), key=lambda kv: kv[1])
    if not report["clock_cross_check"]["ok"]:
        report["verdict"] = "CLOCK_ASSUMPTION_SUSPECT"
        report["reason"] = "the counters do not match the wall time at the assumed CPU clock; fix the clock first"
    elif not report["stability"]["ok"]:
        report["verdict"] = "UNSTABLE"
        report["reason"] = "the two standard reads disagree beyond the limit; repeat before concluding"
    elif share < DOMINANT_SHARE:
        report["verdict"] = "NO_SINGLE_DOMINANT_PHASE"
        report["reason"] = f"largest share is {phase} at {share:.0%}, below {DOMINANT_SHARE:.0%}"
    else:
        report["verdict"] = {"flash": "READING_FLASH", "md5": "MD5", "send_without_fill": "SLIP_ENCODING_AND_SEND",
                             "fill": "ROM_CDC_FIFO_FILL", "ack_wait": "ACK_WAIT_AND_HOST_SYNC"}[phase]
        report["reason"] = f"{phase} holds {share:.0%} of the counted cycles"
    return report


def render(report):
    lines = [f"# P4 USB OTG read profiling — {report['verdict']}", "", report.get("reason", ""), ""]
    if "microseconds" in report:
        lines += ["| phase | microseconds | share of counted cycles |", "| --- | --- | --- |"]
        for key, label in (("flash", "flash"), ("md5", "md5"), ("send_without_fill", "send (without fill)"),
                           ("fill", "fill (inside send)"), ("ack_wait", "ack wait")):
            us = report["microseconds"]["fill_within_send" if key == "fill" else
                                        "send_without_fill" if key == "send_without_fill" else
                                        "ack_wait" if key == "ack_wait" else key]
            lines.append(f"| {label} | {us:,.0f} | {report['shares_of_counted_cycles'][key]:.1%} |")
        lines += ["", "`fill` is part of `send`; do not add them.", ""]
    lines.append(f"- integrity: {'ok' if report['integrity']['ok'] else report['integrity']['problems']}")
    lines.append(f"- stability of the two standard reads: {report['stability']}")
    if "clock_cross_check" in report:
        lines.append(f"- clock cross-check: {report['clock_cross_check']}")
        lines.append(f"- excess over the MD5-only read: {report['excess_over_md5_only']}")
    return "\n".join(lines) + "\n"


if __name__ == "__main__":
    args = sys.argv[1:]
    if len(args) != 3 or args[1] != "--cpu-hz":
        raise SystemExit("Usage: analyze_trace.py evidence.json --cpu-hz HZ   (the clock is never assumed)")
    result = analyze(json.load(open(args[0])), int(args[2]))
    print(json.dumps(result, indent=2))
    print(render(result), file=sys.stderr)
