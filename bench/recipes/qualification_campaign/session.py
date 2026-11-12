"""Session configuration for generic bench qualification recipes.

Pure functions: importing or validating a session never touches the network,
a serial port or the bench server. A session file is *private* data (board
facts, approved regions, consent, launcher control); a template alone never
authorises touching a card. No destructive region exists by default.
"""
import json
from pathlib import Path

from iobewi_bench import CampaignManifest

PC_CHIPS = {"esp32p4", "esp32c6"}          # G1: qualified generically by the P4/C6 recipes (test_pc_*.py)
CHIPS = {"esp32s3", "esp32c3"} | PC_CHIPS
TRANSPORTS = {"external_uart", "usb_serial_jtag", "native_usb_other", "unknown"}
SCENARIOS = {"Q1", "Q2", "Q3", "C3R-READ", "C3R-RESET", "S3R-RESET", "S3R-BOOT", "S3R-READ", "R4",
             "NUSB-READ", "NUSB-RESET", "T7", "R5", "H10", "U8", "M2", "M40-11", "M40-12A", "M40-11C", "M40-4-RESET",
             "M40-ID", "M40-ID-ENROLL", "M40-PHYS-B", "M40-PHYS-B-ID", "PC-READ", "PC-RESET", "PC-CYCLE", "PC-BACKUP", "P4-JEDEC-READ", "P4-FLASH-INTEGRITY"}
TOP_LEVEL = {"version", "board_id", "chip", "flash_size", "transport", "partition_table_offset",
             "approved_regions", "evidence_root", "operation_timeout_s", "operator_timeout_s",
             "consent", "q2", "q3", "readonly", "bootloader", "r4", "faults", "second_board", "lan_http", "capture", "accept_unknown_transport", "m40", "loader", "images", "backup", "jedec", "integrity"}
DEFAULTS = {"operation_timeout_s": 120, "operator_timeout_s": 900, "approved_regions": [],
            "consent": {"scenarios": [], "note": ""}, "q2": {}, "q3": {},
            "readonly": {}, "bootloader": {}, "r4": {}, "faults": {}, "second_board": {}, "lan_http": {}, "capture": {}, "accept_unknown_transport": False,
            "m40": {}, "loader": {}, "images": [], "backup": {}, "jedec": {}, "integrity": {}}
Q2_DEFAULTS = {"trigger_pattern_b64": "SU9CRVdJLVEyLU5FVkVSLVNUSU1VTEFURUQ=",  # inert, never stimulated
               "expires_ms": 120000, "reservation_ttl_s": 600}
Q3_DEFAULTS = {"allow_restart": False, "launcher_pid_file": None, "state_dir": None, "launcher_log": None,
               "stop_timeout_s": 60, "health_timeout_s": 60, "agent_reconnect_timeout_s": 120}
READONLY_KEYS = {"table_candidates", "reads", "reset"}
RESET_KEYS = {"marker", "marker_timeout_s", "reconnect_timeout_s"}
BOOTLOADER_KEYS = {"marker", "hold_ms", "marker_timeout_s"}
R4_KEYS = {"owned_partitions", "control_windows"}


LOADER_KEYS = {"entry", "download_marker", "marker_timeout_s"}
IMAGE_KEYS = {"label", "path", "sha256", "address"}


class SessionError(ValueError):
    pass


def load_session(path):
    document = json.loads(Path(path).read_text())
    return validate_session(document)


def validate_session(document):
    if not isinstance(document, dict) or document.get("version") != 1 or type(document.get("version")) is not int:
        raise SessionError("Session version 1 required")
    unknown = document.keys() - TOP_LEVEL
    if unknown:
        raise SessionError(f"Unknown session fields: {sorted(unknown)}")
    cfg = {**DEFAULTS, **document}
    for key in ("board_id", "chip", "transport", "evidence_root"):
        if not isinstance(cfg.get(key), str) or not cfg[key]:
            raise SessionError(f"{key} must be a non-empty string")
    if cfg["chip"] not in CHIPS:
        raise SessionError(f"chip must be one of {sorted(CHIPS)}")
    if cfg["transport"] not in TRANSPORTS:
        raise SessionError("Invalid transport")
    for key in ("flash_size", "partition_table_offset"):
        unknown_offset = key == "partition_table_offset" and cfg.get(key) is None   # discovery run: offset not yet known
        if not unknown_offset and (type(cfg.get(key)) is not int or cfg[key] < 0):
            raise SessionError(f"{key} must be a non-negative integer" + (" or null" if key == "partition_table_offset" else ""))
    if (cfg["partition_table_offset"] or 0) % 4096 or cfg["flash_size"] % 4096 or cfg["flash_size"] <= 0:
        raise SessionError("Flash size and table offset must be 4-KiB aligned")
    if type(cfg["accept_unknown_transport"]) is not bool:
        raise SessionError("accept_unknown_transport must be a boolean")
    if not Path(cfg["evidence_root"]).is_absolute():
        raise SessionError("evidence_root must be an absolute private path outside Git")
    for key in ("operation_timeout_s", "operator_timeout_s"):
        if type(cfg[key]) is not int or not 1 <= cfg[key] <= 3600:
            raise SessionError(f"{key} must be 1..3600 seconds")
    if cfg["operation_timeout_s"] > 600:
        raise SessionError("operation_timeout_s is limited to 600 s by the server")
    consent = cfg["consent"]
    if not isinstance(consent, dict) or not set(consent.get("scenarios", [])) <= SCENARIOS:
        raise SessionError("consent.scenarios must list known scenarios")
    cfg["q2"] = {**Q2_DEFAULTS, **cfg["q2"]}
    cfg["q3"] = {**Q3_DEFAULTS, **cfg["q3"]}
    if type(cfg["q2"]["expires_ms"]) is not int or not 1 <= cfg["q2"]["expires_ms"] <= 120000:
        raise SessionError("q2.expires_ms must be 1..120000")
    cfg["readonly"] = validate_readonly(cfg["readonly"], cfg)
    cfg["bootloader"] = validate_bootloader(cfg["bootloader"])
    cfg["r4"] = validate_r4(cfg["r4"], cfg)
    cfg["faults"] = validate_faults(cfg["faults"], cfg)
    cfg["second_board"] = validate_second_board(cfg["second_board"], cfg)
    cfg["m40"] = validate_m40(cfg["m40"], cfg)
    cfg["loader"] = validate_loader(cfg["loader"])
    cfg["backup"] = validate_backup(cfg["backup"])
    cfg["jedec"] = validate_jedec(cfg["jedec"])
    cfg["integrity"] = validate_integrity(cfg["integrity"])
    cfg["lan_http"] = validate_lan_http(cfg["lan_http"])
    cfg["capture"] = validate_capture(cfg["capture"])
    regions = cfg["approved_regions"]
    if not isinstance(regions, list):
        raise SessionError("approved_regions must be a list")
    for region in regions:  # exact shape and sector alignment are enforced by the manifest too
        if not isinstance(region, dict) or set(region) != {"address", "size"}:
            raise SessionError("A region needs exactly address and size")
        if region["address"] + region["size"] > cfg["flash_size"]:
            raise SessionError("Approved region exceeds the declared flash size")
    cfg["images"] = validate_images(cfg["images"], cfg)
    return cfg


def validate_loader(section):
    """P4/C6 (G1): how the ROM loader is entered. `auto` = the agent's own DTR/RTS sequence (a wiring inference, never a measurement).
    `manual` = the operator holds the download strap (C6: GPIO9 to GND, then resets the chip); the recipe then waits for the ROM's
    download-mode banner on the UART. The banner has NO default: it must be fixed from the ROM documentation BEFORE a run."""
    if not isinstance(section, dict) or section.keys() - LOADER_KEYS:
        raise SessionError(f"loader accepts only {sorted(LOADER_KEYS)}")
    out = {"entry": "auto", "marker_timeout_s": 120, **section}
    if out["entry"] not in ("auto", "manual"):
        raise SessionError("loader.entry must be 'auto' or 'manual'")
    if out["entry"] == "manual":
        if "download_marker" not in out:
            raise SessionError("loader.entry 'manual' needs loader.download_marker (no default: fix it from the ROM documentation)")
        _marker(out["download_marker"], "loader.download_marker")
    elif "download_marker" in out:
        raise SessionError("loader.download_marker only makes sense with loader.entry 'manual'")
    if type(out["marker_timeout_s"]) is not int or not 5 <= out["marker_timeout_s"] <= 300:
        raise SessionError("loader.marker_timeout_s must be 5..300 seconds")
    return out


BACKUP_KEYS = {"segment_size", "return_console"}


def validate_jedec(section):
    """P4-JEDEC-READ: the digest of the page assets (the agent) that the operator reviewed and deployed. The recipe refuses to run against any other agent,
    because the raw JEDEC diagnostics exist only in that version. No default: nothing is presumed about what is deployed."""
    if not isinstance(section, dict) or section.keys() - {"expected_assets_sha256"}:
        raise SessionError("jedec accepts only expected_assets_sha256")
    digest = section.get("expected_assets_sha256")
    if digest is not None and not (isinstance(digest, str) and len(digest) == 64 and all(c in "0123456789abcdef" for c in digest)):
        raise SessionError("jedec.expected_assets_sha256 must be 64 lowercase hex characters")
    return dict(section)


def validate_integrity(section):
    """P4-FLASH-INTEGRITY: same contract as `jedec`: the reviewed and deployed page assets digest, with no default. The recipe also needs the `flash.md5`
    capability on the card, checked before any command."""
    if not isinstance(section, dict) or section.keys() - {"expected_assets_sha256"}:
        raise SessionError("integrity accepts only expected_assets_sha256")
    digest = section.get("expected_assets_sha256")
    if digest is not None and not (isinstance(digest, str) and len(digest) == 64 and all(c in "0123456789abcdef" for c in digest)):
        raise SessionError("integrity.expected_assets_sha256 must be 64 lowercase hex characters")
    return dict(section)


def validate_backup(section):
    """PC-BACKUP (G1): how the whole flash is read. `segment_size` is the size of every read operation (one artifact each): a multiple of
    4 KiB, at most 16 MiB (the artifact limit). `return_console` tells whether each read returns the card to the console: True makes every
    segment a fresh loader session (the safe choice while chained reads in one session are not qualified on the P4), False keeps one."""
    if not isinstance(section, dict) or section.keys() - BACKUP_KEYS:
        raise SessionError(f"backup accepts only {sorted(BACKUP_KEYS)}")
    out = {"segment_size": 4 * 1024 * 1024, "return_console": True, **section}
    size = out["segment_size"]
    if type(size) is not int or not 4096 <= size <= 16 * 1024 * 1024 or size % 4096:
        raise SessionError("backup.segment_size must be a multiple of 4096 between 4096 and 16777216")
    if type(out["return_console"]) is not bool:
        raise SessionError("backup.return_console must be a boolean")
    return out


def validate_images(items, cfg):
    """PC-CYCLE (G1): firmware images the recipe may program. Each is a PRIVATE absolute file pinned by its SHA-256 and a 4-KiB aligned
    address that must start inside an operator-approved region. The whole span (sector rounded) is checked again, with the real size,
    before any command (images.plan)."""
    import re
    if not isinstance(items, list) or len(items) > 16:
        raise SessionError("images: a list of at most 16 entries")
    if items and not cfg["approved_regions"]:
        raise SessionError("images need approved_regions (an image never creates write permission)")
    labels = set()
    for item in items:
        if not isinstance(item, dict) or set(item) != IMAGE_KEYS:
            raise SessionError(f"images: each entry is exactly {sorted(IMAGE_KEYS)}")
        if not isinstance(item["label"], str) or not re.fullmatch(r"[A-Za-z0-9._-]{1,32}", item["label"]) or item["label"] in labels:
            raise SessionError("images.label: 1..32 characters [A-Za-z0-9._-], unique")
        labels.add(item["label"])
        if not isinstance(item["path"], str) or not Path(item["path"]).is_absolute() or item["path"].endswith("/"):
            raise SessionError("images.path must be an absolute file path (kept outside Git)")
        if not (isinstance(item["sha256"], str) and len(item["sha256"]) == 64 and all(c in "0123456789abcdef" for c in item["sha256"])):
            raise SessionError("images.sha256 must be 64 lowercase hex characters")
        address = item["address"]
        if type(address) is not int or address < 0 or address % 4096 or address >= cfg["flash_size"]:
            raise SessionError("images.address: a 4-KiB aligned offset inside the declared flash")
        if not any(r["address"] <= address < r["address"] + r["size"] for r in cfg["approved_regions"]):
            raise SessionError("images.address must lie inside an approved region")
    return [dict(item) for item in items]


def validate_readonly(section, cfg):
    """C3-R parameters. Nothing is presumed: no default table address, no default read range."""
    if not isinstance(section, dict) or section.keys() - READONLY_KEYS:
        raise SessionError(f"readonly accepts only {sorted(READONLY_KEYS)}")
    size = cfg["flash_size"]
    candidates = section.get("table_candidates", [])
    if (not isinstance(candidates, list) or len(candidates) > 4 or len(set(candidates)) != len(candidates)
            or any(type(a) is not int or a % 4096 or not 0 <= a <= size - 4096 for a in candidates)):
        raise SessionError("readonly.table_candidates: at most 4 distinct aligned addresses inside the flash")
    reads = section.get("reads", [])
    if not isinstance(reads, list) or len(reads) > 8:
        raise SessionError("readonly.reads: at most 8 ranges")
    for item in reads:
        if (not isinstance(item, dict) or set(item) != {"address", "size"} or type(item["address"]) is not int
                or type(item["size"]) is not int or not 0 < item["size"] <= 65536 or item["address"] < 0
                or item["address"] + item["size"] > size):
            raise SessionError("readonly.reads: {address, size<=64KiB} inside the flash")
    reset = section.get("reset", {})
    if not isinstance(reset, dict) or reset.keys() - RESET_KEYS:
        raise SessionError(f"readonly.reset accepts only {sorted(RESET_KEYS)}")
    reset = {"marker": f"ESP-ROM:{cfg['chip']}", "marker_timeout_s": 15, "reconnect_timeout_s": 60, **reset}
    if not isinstance(reset["marker"], str) or not 1 <= len(reset["marker"]) <= 128:
        raise SessionError("readonly.reset.marker must be 1..128 characters")
    for key in ("marker_timeout_s", "reconnect_timeout_s"):
        if type(reset[key]) is not int or not 1 <= reset[key] <= 300:
            raise SessionError(f"readonly.reset.{key} must be 1..300 seconds")
    return {"table_candidates": candidates, "reads": reads, "reset": reset}


def validate_bootloader(section):
    """S3R-BOOT parameters. The download-mode marker has NO default: it must be fixed from the ROM documentation BEFORE a run,
    never copied from a previous result. Without it the recipe is BLOCKED."""
    if not isinstance(section, dict) or section.keys() - BOOTLOADER_KEYS:
        raise SessionError(f"bootloader accepts only {sorted(BOOTLOADER_KEYS)}")
    out = {"hold_ms": 500, "marker_timeout_s": 15, **section}
    if "marker" in out and (not isinstance(out["marker"], str) or not 1 <= len(out["marker"]) <= 128):
        raise SessionError("bootloader.marker must be 1..128 characters")
    if type(out["hold_ms"]) is not int or not 100 <= out["hold_ms"] <= 3000:
        raise SessionError("bootloader.hold_ms must be 100..3000")
    if type(out["marker_timeout_s"]) is not int or not 1 <= out["marker_timeout_s"] <= 300:
        raise SessionError("bootloader.marker_timeout_s must be 1..300 seconds")
    return out


def validate_faults(section, cfg):
    """R5 parameters: ONE long READ-ONLY range during which the operator (or the launcher) injects the fault. Nothing is presumed."""
    if not isinstance(section, dict) or section.keys() - {"read"}:
        raise SessionError("faults accepts only 'read'")
    item = section.get("read")
    if item is None:
        return {}
    if (not isinstance(item, dict) or set(item) != {"address", "size", "interrupt_after_s"} or type(item["address"]) is not int
            or type(item["size"]) is not int or type(item["interrupt_after_s"]) is not int or item["address"] < 0
            or not 4096 <= item["size"] <= 8 * 1024 * 1024 or not 1 <= item["interrupt_after_s"] <= 120
            or item["address"] + item["size"] > cfg["flash_size"]):
        raise SessionError("faults.read: {address, size 4 KiB..8 MiB inside the flash, interrupt_after_s 1..120}")
    return {"read": item}


def validate_second_board(section, cfg):
    """M2: the logical id of a SECOND physical card of the SAME chip family, connected at the same time. Never simulated, never presumed."""
    if not isinstance(section, dict) or section.keys() - {"board_id"}:
        raise SessionError("second_board accepts only board_id")
    if not section:
        return {}
    if not isinstance(section.get("board_id"), str) or not section["board_id"] or section["board_id"] == cfg["board_id"]:
        raise SessionError("second_board.board_id must be a non-empty logical id different from board_id")
    return dict(section)


M40_KEYS = {"read", "avoid", "sample_interval_s", "compatible_server_sha256", "known", "uart", "identity", "phys_b"}
M40_BLOCK = 1024 * 1024                       # the agent reads in blocks of at most 1 MiB: one block keeps the proof a single transfer


def validate_m40(section, cfg):
    """M40-11a (issue #40): one explicit READ range on the first card while the second card is only observed.

    Never presumed: absent section = the scenario is BLOCKED. `avoid` lists regions the operator declares sensitive
    (configuration, otadata...); the range must not touch them. Ranges are 4-KiB aligned, inside the declared flash and
    at most one agent block (1 MiB). `compatible_server_sha256` lists {sha256, reason}: server code digests the operator reviewed as compatible
    when they differ from this checkout's, each with its written justification, kept in the PRIVATE session (see m40.assess_server_build). Reads only: nothing here authorises a write or an erase."""
    if not isinstance(section, dict) or section.keys() - M40_KEYS:
        raise SessionError(f"m40 accepts only {sorted(M40_KEYS)}")
    if not section:
        return {}

    def region(item, name):
        if not isinstance(item, dict) or set(item) != {"address", "size"}:
            raise SessionError(f"m40.{name}: exactly {{address, size}}")
        address, size = item["address"], item["size"]
        if (type(address) is not int or type(size) is not int or address < 0 or size <= 0 or address % 4096 or size % 4096
                or address + size > cfg["flash_size"]):
            raise SessionError(f"m40.{name}: 4-KiB aligned {{address, size}} inside the declared flash")
        return {"address": address, "size": size}

    if "avoid" in section and "read" not in section:
        raise SessionError("m40.avoid only makes sense with m40.read")
    read = region(section["read"], "read") if "read" in section else None
    if read and read["size"] > M40_BLOCK:
        raise SessionError("m40.read.size is limited to one agent block (1 MiB)")
    avoid = section.get("avoid", [])
    if not isinstance(avoid, list) or len(avoid) > 16:
        raise SessionError("m40.avoid: at most 16 regions")
    avoid = [region(item, "avoid") for item in avoid]
    for item in avoid:
        if read["address"] < item["address"] + item["size"] and item["address"] < read["address"] + read["size"]:
            raise SessionError("m40.read intersects a region declared sensitive in m40.avoid")
    interval = section.get("sample_interval_s", 0.5)
    if type(interval) not in (int, float) or isinstance(interval, bool) or not 0.1 <= interval <= 5:
        raise SessionError("m40.sample_interval_s must be 0.1..5 seconds")
    compatible = section.get("compatible_server_sha256", [])
    if not isinstance(compatible, list) or len(compatible) > 8:
        raise SessionError("m40.compatible_server_sha256: a list of at most 8 {sha256, reason} entries")
    seen = set()
    for entry in compatible:
        if not isinstance(entry, dict) or set(entry) != {"sha256", "reason"}:
            raise SessionError("m40.compatible_server_sha256: each entry is exactly {sha256, reason}")
        digest, reason = entry["sha256"], entry["reason"]
        if not (isinstance(digest, str) and len(digest) == 64 and all(c in "0123456789abcdef" for c in digest)):
            raise SessionError("m40.compatible_server_sha256: sha256 must be 64 lowercase hex characters")
        if digest in seen:
            raise SessionError("m40.compatible_server_sha256: duplicate sha256")
        seen.add(digest)
        if not isinstance(reason, str) or not reason.strip() or len(reason) > 500:
            raise SessionError("m40.compatible_server_sha256: every sha256 needs a non-empty written justification (at most 500 characters)")
    compatible = [{"sha256": e["sha256"], "reason": e["reason"].strip()} for e in compatible]
    result = {"sample_interval_s": float(interval), "compatible_server_sha256": compatible}
    if read:
        result.update(read=read, avoid=avoid)
    if "known" in section:
        result["known"] = validate_m40_known(section["known"], cfg)
    if "uart" in section:
        result["uart"] = validate_m40_uart(section["uart"], cfg)
    if "identity" in section:
        result["identity"] = validate_m40_identity(section["identity"], cfg)
    if "phys_b" in section:
        result["phys_b"] = validate_m40_phys_b(section["phys_b"])
    return result


def _marker(value, name):
    if not isinstance(value, str) or not 1 <= len(value) <= 128 or not value.isprintable() or value != value.strip():
        raise SessionError(f"{name}: a printable text of 1..128 characters without leading or trailing space")
    return value


def validate_m40_known(section, cfg):
    """M40-11c/d: where the partition table is (declared, never guessed) and the three UART markers of a boot, all declared by the operator."""
    keys = {"table_offset", "rom_marker", "bootloader_marker", "app_marker", "marker_timeout_s"}
    if not isinstance(section, dict) or set(section) != keys:
        raise SessionError(f"m40.known requires exactly {sorted(keys)}")
    offset = section["table_offset"]
    if type(offset) is not int or offset < 0 or offset % 4096 or offset + 4096 > cfg["flash_size"]:
        raise SessionError("m40.known.table_offset: a 4-KiB aligned offset inside the declared flash")
    markers = [_marker(section[k], "m40.known." + k) for k in ("rom_marker", "bootloader_marker", "app_marker")]
    if len(set(markers)) != 3:
        raise SessionError("m40.known: the ROM, bootloader and application markers must be three different texts")
    timeout = section["marker_timeout_s"]
    if type(timeout) not in (int, float) or isinstance(timeout, bool) or not 5 <= timeout <= 120:
        raise SessionError("m40.known.marker_timeout_s must be 5..120 seconds")
    return {**section, "marker_timeout_s": float(timeout)}


def validate_m40_identity(section, cfg):
    """M40-ID / M40-PHYS-B-ID: where the private identity key and the reference live. Both are absolute, distinct, and OUTSIDE the campaign
    evidence root (a reference must survive the cleaning or rewriting of campaign results). `create_key` must be an explicit opt-in."""
    keys = {"key_file", "reference_file", "create_key"}
    if not isinstance(section, dict) or not {"key_file", "reference_file"} <= set(section) or set(section) - keys:
        raise SessionError(f"m40.identity requires key_file and reference_file and accepts only {sorted(keys)}")
    paths = {}
    for name in ("key_file", "reference_file"):
        value = section[name]
        if not isinstance(value, str) or not value or not Path(value).is_absolute() or value.endswith("/"):
            raise SessionError(f"m40.identity.{name} must be an absolute file path")
        paths[name] = value
    if paths["key_file"] == paths["reference_file"]:
        raise SessionError("m40.identity: the key and the reference must be two different files")
    root = Path(cfg["evidence_root"])
    for name, value in paths.items():
        if Path(value) == root or root in Path(value).parents:
            raise SessionError(f"m40.identity.{name} must be outside the campaign evidence_root (it must survive the cleaning of campaign results)")
    create = section.get("create_key", False)
    if type(create) is not bool:
        raise SessionError("m40.identity.create_key must be a boolean")
    return {**paths, "create_key": create}


def validate_m40_phys_b(section):
    """M40-PHYS-B: bounded waits for the operator's cable gesture (seconds)."""
    keys = {"disconnect_timeout_s", "return_timeout_s", "settle_s"}
    if not isinstance(section, dict) or set(section) != keys:
        raise SessionError(f"m40.phys_b requires exactly {sorted(keys)}")
    for key, low, high in (("disconnect_timeout_s", 5, 900), ("return_timeout_s", 5, 900), ("settle_s", 0, 60)):
        value = section[key]
        if type(value) not in (int, float) or isinstance(value, bool) or not low <= value <= high:
            raise SessionError(f"m40.phys_b.{key} must be {low}..{high} seconds")
    return {key: float(value) for key, value in section.items()}


def validate_m40_uart(section, cfg):
    """M40-4: capture window, thresholds, each card's DISTINCTIVE markers (disjoint) and the optional stimulus."""
    keys = {"window_s", "min_bytes", "min_overlap_s", "min_interleavings", "stimulus", "markers"}
    if not isinstance(section, dict) or set(section) != keys:
        raise SessionError(f"m40.uart requires exactly {sorted(keys)}")
    second = cfg["second_board"].get("board_id")
    if not second:
        raise SessionError("m40.uart needs second_board.board_id")
    for key, low, high in (("window_s", 5, 600), ("min_overlap_s", 0.1, 600)):
        value = section[key]
        if type(value) not in (int, float) or isinstance(value, bool) or not low <= value <= high:
            raise SessionError(f"m40.uart.{key} must be {low}..{high}")
    for key, low, high in (("min_bytes", 1, 1 << 24), ("min_interleavings", 1, 100000)):
        if type(section[key]) is not int or not low <= section[key] <= high:
            raise SessionError(f"m40.uart.{key} must be an integer {low}..{high}")
    if section["stimulus"] not in ("none", "reset_both"):
        raise SessionError("m40.uart.stimulus must be 'none' (passive capture) or 'reset_both' (needs M40-4-RESET and --qual-allow-reset)")
    markers = section["markers"]
    ids = {cfg["board_id"], second}
    if not isinstance(markers, dict) or set(markers) != ids:
        raise SessionError("m40.uart.markers: exactly one list per card (board_id and second_board.board_id)")
    seen = {}
    for card, items in markers.items():
        if not isinstance(items, list) or not 1 <= len(items) <= 4:
            raise SessionError("m40.uart.markers: 1..4 distinctive markers per card")
        for item in items:
            _marker(item, "m40.uart.markers")
            if item in seen:
                raise SessionError("m40.uart.markers must be distinct across cards and within a card")
            seen[item] = card
    return {**section, "window_s": float(section["window_s"]), "min_overlap_s": float(section["min_overlap_s"])}


def validate_lan_http(section):
    """H10: ONE private-network HTTP origin of the test target (tools/lan_http_target.py on another LAN machine). Nothing is presumed."""
    from urllib.parse import urlsplit
    import ipaddress
    if not isinstance(section, dict) or section.keys() - {"origin"}:
        raise SessionError("lan_http accepts only origin")
    if not section:
        return {}
    parts = urlsplit(section.get("origin", "")) if isinstance(section.get("origin"), str) else None
    try:
        address = ipaddress.ip_address(parts.hostname) if parts and parts.hostname else None
    except ValueError:
        address = None
    if (not parts or parts.scheme != "http" or address is None or not address.is_private or address.is_loopback or parts.port is None
            or parts.path not in ("", "/") or parts.username or parts.password or parts.query or parts.fragment):
        raise SessionError("lan_http.origin must be http://<private LAN IPv4>:<port> (no credentials, path or query; never loopback)")
    return {"origin": f"http://{parts.hostname}:{parts.port}"}


def validate_capture(section):
    """U8: the observation window of a sustained UART capture. The SOURCE (a firmware printing continuously) is not provided by the bench."""
    keys = {"duration_s", "min_bytes", "resume_every_s", "max_flagged_loss"}
    if not isinstance(section, dict) or section.keys() - keys:
        raise SessionError(f"capture accepts only {sorted(keys)}")
    if not section:
        return {}
    out = {"resume_every_s": 20, "max_flagged_loss": 0, **section}
    bounds = {"duration_s": (1, 900), "min_bytes": (1, 1 << 30), "resume_every_s": (1, 900), "max_flagged_loss": (0, 1 << 20)}
    for key, (low, high) in bounds.items():
        if key not in out or type(out[key]) is not int or not low <= out[key] <= high:
            raise SessionError(f"capture.{key} must be an integer {low}..{high}" + (" (required)" if key in ("duration_s", "min_bytes") else ""))
    return out


def validate_r4(section, cfg):
    """R4 (multi-image) parameters. Nothing is presumed: no default control window, no partition allowed to overlap a region.

    owned_partitions: labels of partitions the operator OWNS for the test (a dedicated scratch partition); only those may overlap a region.
    control_windows: explicit zones read before and after to show that nothing outside the approved regions changed.
    They are the "no implicit global erase" oracle, so they must be spread over the flash and must not intersect an approved region."""
    if not isinstance(section, dict) or section.keys() - R4_KEYS:
        raise SessionError(f"r4 accepts only {sorted(R4_KEYS)}")
    owned = section.get("owned_partitions", [])
    if not isinstance(owned, list) or len(owned) > 8 or any(not isinstance(x, str) or not 1 <= len(x) <= 16 for x in owned):
        raise SessionError("r4.owned_partitions: at most 8 partition labels (1..16 characters)")
    windows = section.get("control_windows", [])
    if not isinstance(windows, list) or len(windows) > 8:
        raise SessionError("r4.control_windows: at most 8 zones")
    for item in windows:
        if (not isinstance(item, dict) or set(item) != {"address", "size"} or type(item["address"]) is not int or type(item["size"]) is not int
                or item["address"] % 4096 or item["size"] % 4096 or not 0 < item["size"] <= 65536 or item["address"] < 0
                or item["address"] + item["size"] > cfg["flash_size"]):
            raise SessionError("r4.control_windows: {address, size} 4-KiB aligned, size <= 64 KiB, inside the flash")
        for region in cfg["approved_regions"]:
            if item["address"] < region["address"] + region["size"] and region["address"] < item["address"] + item["size"]:
                raise SessionError("r4.control_windows must not intersect an approved region")
    return {"owned_partitions": owned, "control_windows": windows}


def table_candidates(cfg):
    """Explicit candidates, else the single declared offset; never a built-in guess."""
    declared = cfg["readonly"]["table_candidates"]
    return declared or ([cfg["partition_table_offset"]] if cfg["partition_table_offset"] is not None else [])


def build_manifest(cfg, scenario_id, *, grant_seconds=1800, steps=(), regions=None):
    """Version-1 CampaignManifest document. Regions are only the operator-approved ones."""
    approved = cfg["approved_regions"] if regions is None else regions
    return {"version": 1, "id": f"{scenario_id}-{cfg['board_id']}",
        "boards": [{"id": cfg["board_id"], "chip": cfg["chip"], "flash_size": cfg["flash_size"],
                    "transports": [cfg["transport"]] + (["unknown"] if cfg["accept_unknown_transport"] else [])}],
        "write_regions": [{"board": cfg["board_id"], **r} for r in approved],
        "max_interruptions": 0 if scenario_id != "Q3" else 1,
        "backup_policy": "Two sequential reads of every approved sector; identical SHA-256; private fsynced files",
        "restore_policy": "Restore from the verified reference with explicit consent, then independent compare",
        "grant_seconds": grant_seconds,
        "operator_steps": list(steps) or ["Gestures are announced by the recipe and observed with bounded waits"],
        "start_criteria": ["Connected, not quarantined, unreserved; chip/flash/transport match the session",
                           "Verified reference backup before any mutation"],
        "close_criteria": ["Final state equals reference", "Reservation released",
                           "Grant revoked locally", "Campaign.close() succeeds"],
        "private_evidence": cfg["evidence_root"]}


def validate_manifest_for(cfg, scenario_id, **kw):
    return CampaignManifest(build_manifest(cfg, scenario_id, **kw))
