"""Firmware images for the PC-CYCLE recipe (G1): load, verify and plan BEFORE any hardware command.

Everything here is a pure check over local files and the private session; nothing is submitted to the bench. The bench
re-checks the plan on its side (chip family, ranges, 4-KiB sector overlaps, digest) and the agent again before programming.
"""
import hashlib
from pathlib import Path

SECTOR = 4096
MAX_IMAGE = 16 * 1024 * 1024               # Orchestrator.program / BenchClient.upload limit
CHUNK = 4 * 1024 * 1024                    # Orchestrator.restore re-programs in segments of this size


class ImagePlanError(ValueError):
    """The image set cannot be programmed as declared; no command was submitted."""


def load_images(items):
    """Read and verify every declared image. Returns [{label, address, data, sha256}]."""
    loaded = []
    for item in items:
        path = Path(item["path"])
        try:
            if not path.is_file():
                raise ImagePlanError(f"image {item['label']!r}: file not found")
            data = path.read_bytes() if path.stat().st_size <= MAX_IMAGE else None
        except OSError as error:
            raise ImagePlanError(f"image {item['label']!r}: cannot be read ({type(error).__name__})") from None
        if data is None or not data:
            raise ImagePlanError(f"image {item['label']!r}: must contain 1..{MAX_IMAGE} bytes")
        digest = hashlib.sha256(data).hexdigest()
        if digest != item["sha256"]:
            raise ImagePlanError(f"image {item['label']!r}: SHA-256 differs from the declared value (file changed since review)")
        loaded.append({"label": item["label"], "address": item["address"], "data": data, "sha256": digest})
    return loaded


def span(address, size):
    """Erase-sector span covered by a write: [lo, hi) rounded to whole 4-KiB sectors."""
    return address // SECTOR * SECTOR, (address + size + SECTOR - 1) // SECTOR * SECTOR


def plan(loaded, regions, flash_size):
    """Plan rows, or ImagePlanError. Each image's sector span must lie in ONE approved region, and the spans of different images must not
    overlap (the bench evaluates overlaps at sector granularity, even when the used bytes do not overlap)."""
    rows, taken = [], []
    for image in loaded:
        lo, hi = span(image["address"], len(image["data"]))
        if hi > flash_size:
            raise ImagePlanError(f"image {image['label']!r}: ends beyond the declared flash")
        region = next((i for i, r in enumerate(regions) if r["address"] <= lo and hi <= r["address"] + r["size"]), None)
        if region is None:
            raise ImagePlanError(f"image {image['label']!r}: sectors {lo:#x}..{hi:#x} are not inside one approved region")
        for other, (olo, ohi) in taken:
            if lo < ohi and olo < hi:
                raise ImagePlanError(f"images {image['label']!r} and {other!r} overlap at sector granularity")
        taken.append((image["label"], (lo, hi)))
        rows.append({"label": image["label"], "address": image["address"], "size": len(image["data"]),
                     "sectors": [lo, hi], "region": region, "sha256": image["sha256"]})
    if not rows:
        raise ImagePlanError("no image to program")
    return rows


def operations(rows, regions, timeout):
    """(operation count, grant seconds) of the whole cycle: one program per image, then one per restored segment.

    Orchestrator.program needs a grant AND a reservation of 2*timeout+90 seconds at each call; the grant has to cover them all."""
    segments = sum(-(-r["size"] // CHUNK) for r in regions)
    count = len(rows) + segments
    return count, count * (2 * timeout + 90) + 60
