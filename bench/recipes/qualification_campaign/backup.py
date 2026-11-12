"""Full-flash backup support (G1, PC-BACKUP): plan, verify and assemble. Pure functions over local files, no network, no bench access.

A backup is a set of SEGMENT files (one per flash.read, each with the sidecar Orchestrator.backup wrote) plus an assembled image and a
manifest. The manifest is what makes the backup trustworthy: it lists every segment with its address, size, SHA-256 and the operation that
produced it, and the global SHA-256. verify_backup() re-reads every file from disk and refuses a missing, duplicated, shifted or altered segment.
"""
import hashlib
import json
import os
import shutil
from pathlib import Path

SECTOR = 4096


class BackupError(ValueError):
    """The backup is incomplete or inconsistent. Nothing was written to the card."""


def plan_segments(total, segment):
    """[(index, address, size)] covering [0, total) exactly, in address order. The last segment may be shorter."""
    if type(total) is not int or type(segment) is not int or total <= 0 or segment <= 0 or segment % SECTOR:
        raise BackupError("total and segment must be positive integers, the segment a multiple of 4 KiB")
    return [(i, a, min(segment, total - a)) for i, a in enumerate(range(0, total, segment))]


def segment_name(index):
    return f"seg-{index:04d}.bin"


def free_space_problem(directory, needed):
    """None if the nearest existing ancestor of `directory` has `needed` free bytes, else a text. Nothing is created."""
    path = Path(os.path.realpath(directory))
    while not path.exists() and path != path.parent:
        path = path.parent
    free = shutil.disk_usage(path).free
    return None if free >= needed else f"{free} bytes free on the evidence storage, {needed} needed"


def _sha256_file(path):
    digest = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def build_manifest(directory, total, segment, rows, *, epoch, generation, device):
    """Manifest document from the per-segment metadata returned by Orchestrator.backup.

    rows: [{"index", "address", "size", "sha256", "operation", "file"}]. The document is verified by verify_backup before it is trusted."""
    return {"version": 1, "device": device, "epoch": epoch, "generation": generation, "total_size": total, "segment_size": segment,
            "segments": rows, "global_sha256": None, "assembled": None, "consistency": "sequential_observations"}


def verify_backup(directory, manifest, *, expected_total):
    """Verify a manifest against the files on disk. Returns the verified global SHA-256, or raises BackupError.

    Checked: the exact plan (no missing, duplicated or shifted segment), distinct producing operations, every file's size and SHA-256 as
    re-read from disk, and the sum of the sizes. The assembled image (when named) is re-read and compared byte for byte with the concatenation."""
    directory = Path(directory)
    if manifest.get("version") != 1 or manifest.get("total_size") != expected_total:
        raise BackupError("manifest version or total size differs from the expected flash size")
    plan = plan_segments(expected_total, manifest["segment_size"])
    rows = manifest.get("segments")
    if not isinstance(rows, list):
        raise BackupError("manifest has no segment list")
    indexes = [r.get("index") for r in rows]
    missing = [i for i in plan_indexes(plan) if i not in indexes]
    if missing:
        raise BackupError(f"missing segment(s): {missing}")
    if len(set(indexes)) != len(indexes):
        raise BackupError("duplicated segment index in the manifest")
    if len(rows) != len(plan):
        raise BackupError("unexpected extra segment(s) in the manifest")
    operations = [r.get("operation") for r in rows]
    if len(set(operations)) != len(operations) or None in operations:
        raise BackupError("two segments claim the same producing operation (or none): a read was repeated or invented")
    whole, total = hashlib.sha256(), 0
    for (index, address, size), row in zip(plan, sorted(rows, key=lambda r: r["index"])):
        if (row["index"], row["address"], row["size"]) != (index, address, size):
            raise BackupError(f"segment {index}: address/size {row['address']:#x}+{row['size']:#x} differ from the plan {address:#x}+{size:#x}")
        path = directory / row["file"]
        if Path(row["file"]).name != row["file"] or not path.is_file():
            raise BackupError(f"segment {index}: file missing or not a plain name")
        data = path.read_bytes()
        if len(data) != size:
            raise BackupError(f"segment {index}: file holds {len(data)} bytes, expected {size}")
        if hashlib.sha256(data).hexdigest() != row["sha256"]:
            raise BackupError(f"segment {index}: SHA-256 differs from the manifest")
        whole.update(data)
        total += len(data)
    if total != expected_total:
        raise BackupError(f"the segments hold {total} bytes, expected {expected_total}")
    verified = whole.hexdigest()
    if manifest.get("global_sha256") not in (None, verified):
        raise BackupError("global SHA-256 differs from the manifest")
    assembled = manifest.get("assembled")
    if assembled is not None:
        if Path(assembled).name != assembled or not (directory / assembled).is_file():
            raise BackupError("assembled image missing or not a plain name")
        if (directory / assembled).stat().st_size != expected_total or _sha256_file(directory / assembled) != verified:
            raise BackupError("assembled image differs from the concatenation of the verified segments")
    return verified


def plan_indexes(plan):
    return [i for i, _, _ in plan]


def assemble(directory, manifest, name="full-flash.bin"):
    """Concatenate the segment files into `name` (never over an existing file), fsync it, return the global SHA-256."""
    directory = Path(directory)
    target = directory / name
    digest = hashlib.sha256()
    partial = target.with_name(name + ".partial")
    with partial.open("xb") as out:
        for row in sorted(manifest["segments"], key=lambda r: r["index"]):
            data = (directory / row["file"]).read_bytes()
            out.write(data)
            digest.update(data)
        out.flush()
        os.fsync(out.fileno())
    os.link(partial, target)                       # fails if the name exists: no evidence is ever replaced
    partial.unlink()
    return digest.hexdigest()
