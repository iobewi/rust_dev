#!/usr/bin/env python3
"""Offline comparison of the per-packet SHA-256 of a T run with a reference dump (e.g. the private A2.bin).
usage: compare_packet_hashes.py T_RESULT.json REFERENCE.bin [--address N]
T_RESULT.json: the T operation result (or a phaseN-results.json holding it under "result").
Only hashes, counts and positions are printed: never a flash byte. A packet hash locates the differing packets, not the differing bytes inside a packet.
Exit code 0 when every packet matches, 1 when at least one differs, 2 on unusable input."""
import hashlib
import json
import math
import sys
from collections import Counter
from functools import reduce

ERASED = 0xFF


def load_result(path):
    data = json.load(open(path))
    return data.get("result", data) if isinstance(data, dict) else data


def compare(result, dump, address=None):
    hashes = result.get("packet_hashes")
    if not isinstance(hashes, list) or not hashes:
        raise ValueError("no_packet_hashes_in_result")
    base = result.get("address", hashes[0]["address"]) if address is None else address
    expected = base
    for i, h in enumerate(hashes):                                   # layout sanity: no gap, no overlap, ordered
        if h.get("packet") != i or h.get("address") != expected or not isinstance(h.get("size"), int) or h["size"] <= 0:
            raise ValueError(f"invalid_packet_layout_at_{i}")
        expected += h["size"]
    total = expected - base
    if len(dump) != total:
        raise ValueError(f"dump_size_mismatch: result covers {total} bytes, dump has {len(dump)}")
    rows = []
    for h in hashes:
        off = h["address"] - base
        chunk = dump[off:off + h["size"]]
        ref = hashlib.sha256(chunk).hexdigest()
        rows.append({"packet": h["packet"], "same": ref == h["sha256"], "erased": chunk.count(ERASED) == len(chunk)})
    different = [r["packet"] for r in rows if not r["same"]]
    runs = []
    for p in different:
        if runs and p == runs[-1][1] + 1:
            runs[-1][1] = p
        else:
            runs.append([p, p])
    cls = {k: {"identical": 0, "different": 0} for k in ("erased", "with_data")}
    for r in rows:
        cls["erased" if r["erased"] else "with_data"]["identical" if r["same"] else "different"] += 1
    periodic = None
    if len(different) >= 3:
        deltas = [b - a for a, b in zip(different, different[1:])]
        stride = reduce(math.gcd, [p - different[0] for p in different[1:]])
        periodic = {"most_common_delta": Counter(deltas).most_common(1)[0][0], "common_stride": stride,
                    "all_on_stride": stride > 1, "first_packet": different[0]}
    return {
        "packets": len(rows), "identical": len(rows) - len(different), "different": len(different),
        "different_packets": different[:128], "different_truncated": max(0, len(different) - 128),
        "runs": [{"from": a, "to": b, "count": b - a + 1, "address_from": hashes[a]["address"]} for a, b in runs[:64]],
        "by_content_of_reference": cls, "periodic": periodic,
        "global": {"bytes": total, "reference_sha256": hashlib.sha256(dump).hexdigest(), "t_data_sha256": result.get("data_sha256"),
                   "global_match": hashlib.sha256(dump).hexdigest() == result.get("data_sha256")},
        "note": "a packet hash locates the differing packets, not the differing bytes inside a packet",
    }


def main(argv):
    args = [a for a in argv if not a.startswith("--")]
    address = int(argv[argv.index("--address") + 1], 0) if "--address" in argv else None
    if "--address" in argv:
        args.remove(argv[argv.index("--address") + 1])
    if len(args) != 2:
        print(__doc__)
        return 2
    try:
        summary = compare(load_result(args[0]), open(args[1], "rb").read(), address)
    except (ValueError, OSError, KeyError) as error:
        print(json.dumps({"error": str(error)}))
        return 2
    print(json.dumps(summary, indent=1))
    return 0 if summary["different"] == 0 else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
