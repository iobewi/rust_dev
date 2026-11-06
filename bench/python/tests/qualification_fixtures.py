"""Synthetic qualification evidence in the REAL on-disk format (SIMULATION ONLY - nothing here comes from a card).

make_run() writes a complete, internally consistent run directory; tests then damage it on purpose.
"""
import hashlib
import json
from pathlib import Path

GIT = "0e1f890c312c033b8c551047a2251a36c5d9ac23"
SERVER_SHA = "7de247710401117f" + "0" * 48
AGENT_SHA = "6db0030b11e5" + "0" * 52
PATTERN_SHA = "7e9a1cfda00bc6ee" + "0" * 48
FF8K = b"\xff" * 8192


def sha(data):
    return hashlib.sha256(data).hexdigest()


def canonical(document):
    return hashlib.sha256(json.dumps(document, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def write_json(path, value):
    path.write_text(json.dumps(value, indent=2))


def write_jsonl(path, rows):
    path.write_text("".join(json.dumps(r) + "\n" for r in rows))


CONTEXTS = {"esp32s3": ("external_uart", 16 * 1024 * 1024, 0x400000), "esp32c3": ("usb_serial_jtag", 4 * 1024 * 1024, 0x3FE000)}
COMMANDS = {"Q1": ["partitions", "flash.read", "flash.write"], "Q2a": ["trigger"], "Q2b": ["trigger"], "Q2c": ["trigger"], "Q2d": ["trigger"],
            "Q3": [], "C3R-0": ["identify"], "C3R-1": ["flash.read", "console"], "C3R-2": ["partitions", "flash.read", "console"], "C3R-3": ["reset"],
            "S3R-1": ["reset"], "S3R-2": ["boot.download"], "S3R-3": ["flash.read"],
            "R4": ["partitions"] + ["flash.read"] * 6 + ["flash.write"] * 3}
SUBMISSIONS = {"Q1": {"primary": 10}, "Q2a": {"primary": 3}, "Q2b": {"primary": 3}, "Q2c": {"primary": 2}, "Q2d": {"primary": 1, "secondary-1": 1},
               "Q3": {"primary": 0}, "C3R-0": {"primary": 1}, "C3R-1": {"primary": 3}, "C3R-2": {"primary": 4}, "C3R-3": {"primary": 1},
               "S3R-1": {"primary": 1}, "S3R-2": {"primary": 2}, "S3R-3": {"primary": 2}, "R4": {"primary": 20}}


def refusal(at, client, code):
    return {"at": at, "client": client, "method": "POST", "path": "/api/v1/operations", "exception_type": "BenchHTTPError", "status": 403, "code": code}


def make_run(root, scenario, *, ts="20261106T100000Z", chip="esp32s3", status="PASS", epoch="e0000001", generation="g0000001",
             recipe_text="# simulated recipe\n", clean=True, **_):
    transport, flash_size, region = CONTEXTS[chip]
    run = Path(root) / f"{scenario}-{ts}"
    (run / "support").mkdir(parents=True)
    (run / "board-0").mkdir()
    support = {"session.py": b"# support\n", "harness.py": b"# harness\n"}
    for name, data in support.items():
        (run / "support" / name).write_bytes(data)
    (run / "recipe.py").write_text(recipe_text)
    sources = {"client.py": b"# client\n", "pytest_plugin.py": b"# plugin\n"}
    for name, data in sources.items():
        (run / name).write_bytes(data)
    (run / "qualification.py").write_bytes(b"# qualification\n")
    manifest = {"version": 1, "id": f"{scenario}-board", "boards": [{"id": "esp32-local", "chip": chip, "flash_size": flash_size, "transports": [transport]}],
                "write_regions": ([{"board": "esp32-local", "address": region, "size": 8192}] if scenario == "Q1" else
                                  [{"board": "esp32-local", "address": 0x200000 + i * 0x10000, "size": 8192} for i in range(2)] if scenario == "R4" else []),
                "private_evidence": "/simulated"}
    write_json(run / "manifest.json", manifest)
    write_json(run / "run.json", {"test": scenario, "recipe_sha256": sha(recipe_text.encode()), "support_sha256": sha(b"# qualification\n"),
                                  "device": "esp32-local", "client_source_sha256": {n: sha(d) for n, d in sources.items()},
                                  "python": "3.12.16", "pytest": "9.2.0", "iobewi-bench": "0.2.0", "manifest_sha256": canonical(manifest),
                                  "declared_environment": {}, "allow_destructive": scenario in ("Q1", "R4")})
    write_json(run / "provenance.json", {"git_sha": GIT, "clean": clean, "dirty_paths": [], "dirty_allowed_by_option": False,
                                         "support_sha256": {n: sha(d) for n, d in support.items()}})
    device = {"id": "esp32-local", "generation": generation, "connected": True, "quarantined": False, "reservation": None, "privileged_seconds": 0,
              "chip": chip, "flash_size": flash_size, "provenance": {"transport": transport, "agent_sha256": AGENT_SHA}}
    server = {"version": "0.2.0", "git_sha": GIT, "server_sha256": SERVER_SHA}
    after_epoch, after_generation = (("e0000002", "g0000002") if scenario == "Q3" else (epoch, generation))
    write_json(run / "before.json", {"epoch": epoch, "device": device, "server_observed": server, "durability": "sqlite-full"})
    write_json(run / "after.json", {"epoch": after_epoch, "device": {**device, "generation": after_generation}, "server_observed": server, "durability": "sqlite-full"})
    t0 = 1000.0
    grants, rows, evidence, restoration, extra = [], [{"at": t0, "client": "primary", "method": "GET", "path": "/health", "ok": True}], [], "not applicable (no mutation)", {}
    if scenario == "Q1":
        for name in ("ref-a", "ref-b"):
            (run / "board-0" / f"{name}.bin").write_bytes(FF8K)
            write_json(run / "board-0" / f"{name}.json", {"address": region, "size": 8192, "sha256": sha(FF8K), "complete": True, "segments": []})
        (run / "board-0" / "flash-001-00000000.bin").write_bytes(FF8K)
        write_json(run / "board-0" / "flash-001-00000000.json", {"address": region, "size": 8192, "sha256": sha(FF8K)})
        evidence = ["partition_count=5 region disjoint from table and partitions", f"ref-a sha256={sha(FF8K)} size=8192", f"ref-b sha256={sha(FF8K)}",
                    f"programmed; independent readback equal to pattern sha256={PATTERN_SHA}"]
        restoration = "restored; independent compare equal to ref-a.bin"
        grants = [(t0 + 10, 1800), (t0 + 130, 0)]
    elif scenario == "Q2a":
        grants = [(t0 + 10, 60)]
        rows.append(refusal(t0 + 75, "primary", "grant_required"))
        evidence = ["armed with 60 s grant remaining", "rearm refused 403 grant_required; owner disarm settled; grant still 0"]
    elif scenario == "Q2b":
        grants = [(t0 + 10, 300), (t0 + 35, 0)]
        rows.append(refusal(t0 + 36, "primary", "grant_required"))
        evidence = ["armed with 300 s grant remaining", "rearm refused 403 grant_required; owner disarm settled; grant still 0"]
    elif scenario in ("Q2c", "Q2d"):
        grants = [(t0 + 10, 300), (t0 + 40, 0)]
        rows.append(refusal(t0 + 20, "primary" if scenario == "Q2c" else "secondary-1", "trigger_not_owned"))
        evidence = ["disarm by " + ("same owner, new reservation" if scenario == "Q2c" else "another identity") + " refused 403 trigger_not_owned with a valid grant"]
    elif scenario == "Q3":
        write_jsonl(run / "windows.jsonl", [{"window": 0, "event": "interrupted"}, {"window": 1, "event": "opened"}])
        write_jsonl(run / "abandoned-handles.jsonl", [{"lease": "x", "reason": "restart"}])
        evidence = ["launcher pid 1, child 2; endpoint, listening socket and state_dir lock/journal match one server",
                    "launcher log: old child exited (line 3) before the replacement started (line 4); no overlap in 49 samples",
                    "epoch changed; same journal and lock files; old Board handle rejected",
                    "agent reconnected; generation g0000001->g0000002; grant 0; no reservation"]
    if scenario == "R4":
        for i in range(2):
            for name in ("a", "b"):
                (run / "board-0" / f"ref-{i}-{name}.bin").write_bytes(FF8K)
                write_json(run / "board-0" / f"ref-{i}-{name}.json", {"address": 0x200000 + i * 0x10000, "size": 8192, "sha256": sha(FF8K), "complete": True, "segments": []})
        evidence = ["partition_count=5; 2 regions disjoint from the table and from non-owned partitions",
                    "region 0 ref sha256=" + sha(FF8K) + " size=8192; pattern sha256=" + sha(b"p0"),
                    "region 1 ref sha256=" + sha(FF8K) + " size=8192; pattern sha256=" + sha(b"p1"),
                    "control windows (stable, outside the regions): 0x10000+0x1000=" + sha(b"w")[:16],
                    "single multi-image write; every region equal to its own pattern",
                    "control windows byte-identical after the write (no implicit erase seen where we looked)",
                    "control windows byte-identical after the restoration",
                    "server readback ab12cd34 deleted after its private copy was re-read and matched"]
        restoration = "restored; independent compare equal to every verified backup"
        grants = [(t0 + 10, 1800), (t0 + 200, 0)]
    if scenario.startswith("S3R"):
        write_json(run / "transport.json", {"declared_by_operator": transport, "inferred_by_agent": transport, "provenance_source": "agent_reported"})
        evidence = [f"transport declared={transport} inferred={transport} (inferred_from_webserial_vid_pid; agent_reported)"]
        if scenario == "S3R-1":
            write_jsonl(run / "reset-requests.jsonl", [{"marker": "ESP-ROM:esp32s3"}])
            evidence += ["reset settled; result fields: boot_verified, method, reset", "'ESP-ROM:esp32s3' observed after the reset, same proof window"]
        elif scenario == "S3R-2":
            write_jsonl(run / "bootloader-requests.jsonl", [{"hold_ms": 500, "marker_len": 17}])
            evidence += ["boot.download settled; result fields: boot_verified, download_mode_requested, hold_ms, method",
                         "download-mode marker observed after the command, same proof window", "the grant expired naturally; the card is left in ROM download mode"]
            grants = [(t0 + 10, 60)]
        elif scenario == "S3R-3":
            evidence += ["read 0x0+0x1000 settled (sha256=" + sha(b"x") + "); transport events after the cursor: console opened",
                         "server readback ab12cd34 deleted after its private copy was re-read and matched",
                         "console return shown by a transport event after the settled read (TRANSPORT proof only; not an application boot)"]
    if scenario.startswith("C3R"):
        write_json(run / "transport.json", {"declared_by_operator": transport, "inferred_by_agent": transport, "provenance_source": "agent_reported"})
        evidence = [f"transport declared={transport} inferred={transport} (inferred_from_webserial_vid_pid; agent_reported)"]
        if scenario == "C3R-0":
            write_json(run / "identity.json", {"chip": chip, "flash_size": flash_size})
            evidence.append(f"measured chip={chip} flash_size={flash_size} (equal to the declaration)")
        elif scenario == "C3R-1":
            evidence += [f"0x0+0x1000: two reads equal, sha256={sha(b'x')}", "server readback ab12cd34 deleted after its private copy was re-read and matched"]
        elif scenario == "C3R-2":
            write_json(run / "table-probes.json", [{"address": 32768, "result": "valid", "entries": 5}, {"address": 65536, "result": "partition_table_invalid"}])
            evidence.append("one valid table at 0x8000: nvs@0x9000+0x6000")
        elif scenario == "C3R-3":
            write_jsonl(run / "reset-requests.jsonl", [{"marker": "ESP-ROM:esp32c3"}])
            evidence += ["reset settled; result fields: boot_verified, method, reset", "'ESP-ROM:esp32c3' observed after the reset, same proof window"]
    gestures = {"Q1": ["GRANT 1800 s locally in the page", "REVOKE the grant locally in the page"], "Q2a": ["GRANT the SHORTEST local duration (60 s)"],
                "Q2b": ["GRANT 300 s locally; then wait for the revoke request", "REVOKE the grant locally NOW"], "Q2c": ["GRANT 90 s locally", "REVOKE the grant locally (end of ownership test)"],
                "Q2d": ["GRANT 90 s locally", "REVOKE the grant locally (end of ownership test)"], "Q3": ["If the page does not reconnect by itself, reload it"],
                "S3R-2": ["GRANT the SHORTEST local duration (60 s) and then do nothing"],
                "R4": ["GRANT 1800 s locally in the page", "REVOKE the grant locally in the page"]}
    if scenario in gestures:
        write_jsonl(run / "operator-requests.jsonl", [{"request": text, "timeout_s": 900} for text in gestures[scenario]])
    events = [{"window": 0, "epoch": epoch, "lost": 0}] + [{"seq": i, "epoch": epoch, "at": at, "kind": "grant", "seconds": s} for i, (at, s) in enumerate(grants)]
    write_jsonl(run / "events.jsonl", events)
    write_jsonl(run / "requests.jsonl", rows)
    ops = []
    for n, command in enumerate(COMMANDS[scenario] * (2 if scenario == "Q2a" or scenario == "Q2b" else 1)):
        ops.append({"id": f"op{n}", "device": "esp32-local", "generation": generation, "command": command, "state": "completed", "settled": True,
                    "unsettled": False, "error_code": None, "http_status": None})
    write_jsonl(run / "board-0" / "operations.jsonl", ops)
    write_json(run / "cleanup.json", {"errors": [], "submissions": SUBMISSIONS[scenario]})
    test = {"id": scenario, "status": status, "preconditions": f"{chip} / {transport}", "evidence": evidence, "incidents": [] if status == "PASS" else ["[bench] simulated"],
            "restoration": restoration, "limits": "simulated"}
    write_json(run / "report.json", {"tests": [test], "windows": 1})
    write_jsonl(run / "verdicts.jsonl", [test])
    return run


def tree_digest(root):
    digest = hashlib.sha256()
    for path in sorted(Path(root).rglob("*")):
        if path.is_file():
            digest.update(str(path.relative_to(root)).encode() + b"\0" + path.read_bytes() + b"\0")
    return digest.hexdigest()
