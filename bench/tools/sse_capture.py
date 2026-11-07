#!/usr/bin/env python3
"""External SSE archiving client for the UART capture qualification (issue #2, requirement 8).

Reads a device's event stream through the official client, archives the raw UART bytes in a PRIVATE file (mode 0600, never in Git), writes an
index with metadata only (no UART content) and computes the continuity independently of the server's flags:

  * a gap in `source_seq` must be announced by `source_lost` on the event that follows it (a SILENT loss is a defect);
  * a repeated `seq` is a duplicate; an `epoch` change or a `lost` > 0 is reported, never hidden;
  * the client can drop its connection every `resume_every_s` seconds and resume at the last `next` cursor: no duplicate, no gap may result.

Library: capture(client, device, seconds, ...). CLI: BENCH_URL / BENCH_CLIENT_TOKEN from the environment, never printed."""
import argparse
import base64
import hashlib
import json
import os
import sys
import time
from pathlib import Path


def analyse(rows):
    """Continuity verdict from the index rows only (so a comparator can recompute it)."""
    silent = flagged = duplicates = 0
    seen, previous = set(), {}
    for row in rows:
        if row["seq"] in seen:
            duplicates += 1
        seen.add(row["seq"])
        generation = row.get("generation")
        last = previous.get(generation)
        gap = 0 if last is None else max(0, row["source_seq"] - last - 1)
        previous[generation] = row["source_seq"]
        flagged += row["source_lost"]
        if gap != row["source_lost"] and last is not None:
            silent += abs(gap - row["source_lost"])
    return {"events": len(rows), "bytes": sum(r["bytes"] for r in rows), "silent_loss": silent, "flagged_loss": flagged, "duplicates": duplicates}


def capture(client, device, seconds, *, index_path, archive_path, resume_every_s=None, stream_timeout=5):
    """Archive for `seconds`. Returns the statistics; the index and the archive are written next to each other."""
    index_path, archive_path = Path(index_path), Path(archive_path)
    fd = os.open(archive_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    stats = {"resumes": 0, "server_lost": 0, "epochs": [], "disconnected_events": 0, "generations": 0, "seconds": seconds}
    rows, after, generation = [], 0, 0
    deadline, next_drop = time.monotonic() + seconds, (time.monotonic() + resume_every_s) if resume_every_s else None
    with os.fdopen(fd, "wb") as archive, index_path.open("x") as index:
        while time.monotonic() < deadline:
            for batch in client.event_stream(device, after=after, timeout=stream_timeout):
                if batch["epoch"] not in stats["epochs"]:
                    stats["epochs"].append(batch["epoch"])
                stats["server_lost"] += batch["lost"]
                for event in batch["events"]:
                    if event["kind"] == "connected":
                        generation += 1
                    elif event["kind"] == "disconnected":
                        stats["disconnected_events"] += 1
                    elif event["kind"] == "uart":
                        data = base64.b64decode(event["data"])
                        archive.write(data)
                        row = {"seq": event["seq"], "source_seq": event["source_seq"], "source_lost": event["source_lost"], "bytes": len(data),
                               "sha256": hashlib.sha256(data).hexdigest(),     # digest of this chunk of the archive (never its content)
                               "generation": generation, "at": event["at"]}
                        rows.append(row)
                        index.write(json.dumps(row) + "\n")
                after = batch["next"]
                if time.monotonic() >= deadline:
                    break
                if next_drop and time.monotonic() >= next_drop:
                    stats["resumes"] += 1                      # deliberate drop; the loop reconnects at the last cursor
                    next_drop = time.monotonic() + resume_every_s
                    break
        archive.flush()
        os.fsync(archive.fileno())
    stats["generations"] = generation
    stats["archive_bytes"] = archive_path.stat().st_size                  # re-read from disk: what was actually persisted
    stats["archive_sha256"] = hashlib.sha256(archive_path.read_bytes()).hexdigest()
    return {**stats, **analyse(rows), "rate_bytes_s": round(sum(r["bytes"] for r in rows) / max(seconds, 1), 1)}


if __name__ == "__main__":
    from iobewi_bench import BenchClient
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--device", required=True)
    parser.add_argument("--seconds", type=int, required=True)
    parser.add_argument("--index", required=True)
    parser.add_argument("--archive", required=True)
    parser.add_argument("--resume-every", type=int)
    args = parser.parse_args()
    result = capture(BenchClient(os.environ["BENCH_URL"], os.environ["BENCH_CLIENT_TOKEN"]), args.device, args.seconds,
                     index_path=args.index, archive_path=args.archive, resume_every_s=args.resume_every)
    print(json.dumps(result, indent=2))
    sys.exit(1 if result["silent_loss"] or result["duplicates"] else 0)
