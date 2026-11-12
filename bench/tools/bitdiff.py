#!/usr/bin/env python3
"""Offline comparison of several reads of the same flash range. No network, no bench, no hardware.

Prints positions, XOR masks, bit counts and directions: never the content of a dump. It states what differs and where; it never infers a cause.
Usage: python tools/bitdiff.py [--json] FILE FILE [FILE ...]   (the first file is the reference of every pair)
"""
import argparse
import itertools
import json
import sys
from pathlib import Path


def diff(a, b):
    """Differing byte positions between two equal-length buffers: [{offset, xor, bits, to_zero, to_one}] (direction: a -> b)."""
    if len(a) != len(b):
        raise ValueError(f"sizes differ: {len(a)} vs {len(b)}")
    rows = []
    for offset, (x, y) in enumerate(zip(a, b)):
        if x != y:
            mask = x ^ y
            rows.append({"offset": offset, "xor": mask, "bits": bin(mask).count("1"),
                         "to_zero": bin(x & mask).count("1"), "to_one": bin(y & mask).count("1")})
    return rows


def spacings(offsets):
    return [b - a for a, b in zip(offsets, offsets[1:])]


def alignments(offsets):
    """How many differing offsets fall on each residue, for several moduli. Descriptive only."""
    return {str(m): dict(sorted({r: sum(1 for o in offsets if o % m == r) for r in {o % m for o in offsets}}.items())) for m in (4, 16, 32, 64)}


def summarise(rows):
    offsets = [r["offset"] for r in rows]
    return {"bytes": len(rows), "bits": sum(r["bits"] for r in rows), "to_zero": sum(r["to_zero"] for r in rows), "to_one": sum(r["to_one"] for r in rows),
            "xor_masks": {f"{m:#04x}": sum(1 for r in rows if r["xor"] == m) for m in sorted({r["xor"] for r in rows})},
            "offsets": [f"{o:#x}" for o in offsets], "spacings": spacings(offsets), "alignments": alignments(offsets),
            "single_bit_bytes": sum(1 for r in rows if r["bits"] == 1)}


def compare(buffers):
    """buffers: {name: bytes}. Returns pairwise summaries and the offsets that differ in at least one pair / in every pair."""
    names = list(buffers)
    pairs = {f"{a} -> {b}": summarise(diff(buffers[a], buffers[b])) for a, b in itertools.combinations(names, 2)}
    unions = [set(r["offset"] for r in diff(buffers[a], buffers[b])) for a, b in itertools.combinations(names, 2)]
    return {"pairs": pairs, "differ_in_any_pair": len(set().union(*unions)) if unions else 0, "differ_in_every_pair": len(set.intersection(*unions)) if unions else 0}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("files", nargs="+", type=Path)
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)
    if len(args.files) < 2:
        parser.error("at least two files")
    result = compare({str(p.name): p.read_bytes() for p in args.files})
    if args.json:
        json.dump(result, sys.stdout, indent=2)
        print()
        return 0
    for name, s in result["pairs"].items():
        print(f"{name}: {s['bytes']} byte(s), {s['bits']} bit(s) (to 0: {s['to_zero']}, to 1: {s['to_one']}), single-bit bytes {s['single_bit_bytes']}")
        print(f"  offsets {' '.join(s['offsets'])}")
        print(f"  xor masks {s['xor_masks']}")
    print(f"differ in any pair: {result['differ_in_any_pair']}, in every pair: {result['differ_in_every_pair']}")
    print("No cause is inferred from positions or regularity.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
