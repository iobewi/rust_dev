"""Session configuration for generic bench qualification recipes.

Pure functions: importing or validating a session never touches the network,
a serial port or the bench server. A session file is *private* data (board
facts, approved regions, consent, launcher control); a template alone never
authorises touching a card. No destructive region exists by default.
"""
import json
from pathlib import Path

from iobewi_bench import CampaignManifest

CHIPS = {"esp32s3", "esp32c3"}
TRANSPORTS = {"external_uart", "usb_serial_jtag", "native_usb_other", "unknown"}
SCENARIOS = {"Q1", "Q2", "Q3", "C3R-READ", "C3R-RESET"}
TOP_LEVEL = {"version", "board_id", "chip", "flash_size", "transport", "partition_table_offset",
             "approved_regions", "evidence_root", "operation_timeout_s", "operator_timeout_s",
             "consent", "q2", "q3", "readonly", "accept_unknown_transport"}
DEFAULTS = {"operation_timeout_s": 120, "operator_timeout_s": 900, "approved_regions": [],
            "consent": {"scenarios": [], "note": ""}, "q2": {}, "q3": {},
            "readonly": {}, "accept_unknown_transport": False}
Q2_DEFAULTS = {"trigger_pattern_b64": "SU9CRVdJLVEyLU5FVkVSLVNUSU1VTEFURUQ=",  # inert, never stimulated
               "expires_ms": 120000, "reservation_ttl_s": 600}
Q3_DEFAULTS = {"allow_restart": False, "launcher_pid_file": None, "state_dir": None, "launcher_log": None,
               "stop_timeout_s": 60, "health_timeout_s": 60, "agent_reconnect_timeout_s": 120}
READONLY_KEYS = {"table_candidates", "reads", "reset"}
RESET_KEYS = {"marker", "marker_timeout_s", "reconnect_timeout_s"}


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
    regions = cfg["approved_regions"]
    if not isinstance(regions, list):
        raise SessionError("approved_regions must be a list")
    for region in regions:  # exact shape and sector alignment are enforced by the manifest too
        if not isinstance(region, dict) or set(region) != {"address", "size"}:
            raise SessionError("A region needs exactly address and size")
        if region["address"] + region["size"] > cfg["flash_size"]:
            raise SessionError("Approved region exceeds the declared flash size")
    return cfg


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
