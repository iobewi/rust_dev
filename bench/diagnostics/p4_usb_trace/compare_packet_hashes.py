#!/usr/bin/env python3
"""Offline comparison of the per-packet SHA-256 of a T run with a reference dump (e.g. the private A2.bin).
usage: compare_packet_hashes.py T_RESULT.json REFERENCE.bin [--address N]
T_RESULT.json: the T operation result (or a phaseN-results.json holding it under "result").
Only hashes, counts and positions are printed: never a flash byte. A packet hash locates the differing packets, not the differing bytes inside a packet.
Three explicit outcomes (field "status", exit code):
  match               0  every packet matches the reference AND the global SHA-256 declared by T is the reference's
  content_divergence  1  the evidence is consistent and at least one packet differs from the reference
  invalid_evidence    2  the evidence contradicts itself or is malformed (format, sizes, addresses, packet numbers, ACK sequence,
                         per-packet hashes vs global SHA-256, unreadable input): NO conclusion may be drawn, it is never a success"""
import hashlib
import json
import math
import re
import sys
from collections import Counter
from functools import reduce

ERASED = 0xFF
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")


class InvalidEvidence(ValueError):
    """The result (or the pairing with the dump) is malformed or self-contradictory."""

    def __init__(self, problems):
        self.problems = list(problems)
        super().__init__("; ".join(self.problems))


def _int(v):
    return isinstance(v, int) and not isinstance(v, bool)


def load_result(path):
    data = json.load(open(path))
    return data.get("result", data) if isinstance(data, dict) else data


def validate(result, dump, address=None):
    """Every structural check; returns (base, total) or raises InvalidEvidence listing ALL the problems found."""
    problems = []
    hashes = result.get("packet_hashes") if isinstance(result, dict) else None
    if not isinstance(hashes, list) or not hashes:
        raise InvalidEvidence(["no_packet_hashes_in_result"])
    declared = result.get("data_sha256")
    if not isinstance(declared, str) or not SHA256_RE.match(declared):
        problems.append("malformed_global_sha256")
    first = hashes[0].get("address") if isinstance(hashes[0], dict) else None
    base = address if address is not None else result.get("address", first)
    if not _int(base) or base < 0:
        problems.append("invalid_base_address")
        base = first if _int(first) else 0
    if address is None and _int(result.get("address")) and _int(first) and first != result["address"]:
        problems.append("first_packet_address_differs_from_declared_address")
    expected, ack = base, 0
    structural = False
    for i, h in enumerate(hashes):
        bad = []
        if not isinstance(h, dict):
            bad.append(f"packet_{i}_not_an_object")
        else:
            if h.get("packet") != i or not _int(h.get("packet")):
                bad.append(f"packet_number_{i}")
            if not _int(h.get("size")) or h["size"] <= 0:
                bad.append(f"packet_{i}_size")
            if h.get("address") != expected or not _int(h.get("address")):
                bad.append(f"packet_{i}_address_expected_{expected}")
            if not isinstance(h.get("sha256"), str) or not SHA256_RE.match(h["sha256"]):
                bad.append(f"packet_{i}_malformed_sha256")
            if not bad or _int(h.get("size")) and h["size"] > 0:
                ack_after = ack + (h["size"] if _int(h.get("size")) and h["size"] > 0 else 0)
                if h.get("ack_index") != i + 1 or not _int(h.get("ack_index")):
                    bad.append(f"packet_{i}_ack_index_expected_{i + 1}")
                if h.get("ack_bytes") != ack_after or not _int(h.get("ack_bytes")):
                    bad.append(f"packet_{i}_ack_bytes_expected_{ack_after}")
                if h.get("acked") != 1 or isinstance(h.get("acked"), bool):
                    bad.append(f"packet_{i}_not_acked")
        if bad:                                          # the first inconsistent packet is enough: later ones would only cascade
            problems.extend(bad)
            structural = True
            break
        ack += h["size"]
        expected += h["size"]
    total = expected - base
    if structural:                                       # totals are meaningless past the first break: refuse now
        raise InvalidEvidence(problems)
    for key in ("size", "bytes"):
        if key in result and result[key] != total:
            problems.append(f"declared_{key}_{result[key]}_differs_from_packets_total_{total}")
    for key, want in (("packets", len(hashes)), ("acks", len(hashes))):
        if key in result and result[key] != want:
            problems.append(f"declared_{key}_{result[key]}_differs_from_packet_count_{want}")
    if len(dump) != total:
        problems.append(f"dump_size_{len(dump)}_differs_from_packets_total_{total}")
    if problems:
        raise InvalidEvidence(problems)
    return base, total


def compare(result, dump, address=None):
    """Returns the summary dict with "status" in {"match", "content_divergence"}; raises InvalidEvidence otherwise."""
    base, total = validate(result, dump, address)
    hashes = result["packet_hashes"]
    rows = []
    for h in hashes:
        off = h["address"] - base
        chunk = dump[off:off + h["size"]]
        ref = hashlib.sha256(chunk).hexdigest()
        rows.append({"packet": h["packet"], "same": ref == h["sha256"], "erased": chunk.count(ERASED) == len(chunk)})
    different = [r["packet"] for r in rows if not r["same"]]
    reference_sha = hashlib.sha256(dump).hexdigest()
    global_match = reference_sha == result["data_sha256"]
    # The two views of the same bytes must agree: all packets equal <=> the global digest equals the reference's.
    if (len(different) == 0) != global_match:
        raise InvalidEvidence(["per_packet_hashes_contradict_global_sha256: "
                               + ("all packets match the reference but the declared global SHA-256 differs" if not different
                                  else "the global SHA-256 equals the reference but some packet hashes differ")])
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
        "status": "match" if not different else "content_divergence",
        "packets": len(rows), "identical": len(rows) - len(different), "different": len(different),
        "different_packets": different[:128], "different_truncated": max(0, len(different) - 128),
        "runs": [{"from": a, "to": b, "count": b - a + 1, "address_from": hashes[a]["address"]} for a, b in runs[:64]],
        "by_content_of_reference": cls, "periodic": periodic,
        "global": {"bytes": total, "reference_sha256": reference_sha, "t_data_sha256": result["data_sha256"], "global_match": global_match},
        "note": "a packet hash locates the differing packets, not the differing bytes inside a packet",
    }


def main(argv):
    args = [a for a in argv if not a.startswith("--")]
    address = None
    if "--address" in argv:
        i = argv.index("--address")
        try:
            address = int(argv[i + 1], 0)
        except (IndexError, ValueError):
            print(json.dumps({"status": "invalid_evidence", "problems": ["bad_address_option"]}))
            return 2
        args.remove(argv[i + 1])
    if len(args) != 2:
        print(__doc__)
        return 2
    try:
        summary = compare(load_result(args[0]), open(args[1], "rb").read(), address)
    except InvalidEvidence as error:
        print(json.dumps({"status": "invalid_evidence", "problems": error.problems}))
        return 2
    except (ValueError, OSError, KeyError, TypeError, AttributeError) as error:        # unreadable / not JSON / wrong shape
        print(json.dumps({"status": "invalid_evidence", "problems": [f"unusable_input: {type(error).__name__}"]}))
        return 2
    print(json.dumps(summary, indent=1))
    return 0 if summary["status"] == "match" else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
