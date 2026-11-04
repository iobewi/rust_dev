#!/usr/bin/env python3
"""Prepare private images, witnesses and restore patches OFFLINE; never access a bench."""
import argparse
import hashlib
import json
import os
from pathlib import Path

SECTOR = 4096


def digest(data):
    return hashlib.sha256(data).hexdigest()


def prepare(backup, expected_sha256, chip, addresses, witnesses, output, *, flash_size):
    if chip not in {"esp32c3", "esp32s3"}:
        raise ValueError("Explicit C3 or S3 target required")
    raw = Path(backup).read_bytes()
    if not 2 * SECTOR <= len(raw) <= 64 * 1024 * 1024 or len(raw) % SECTOR:
        raise ValueError("Full backup must have a sector-aligned supported capacity")
    if isinstance(flash_size, bool) or not isinstance(flash_size, int) or len(raw) != flash_size:
        raise ValueError("Full backup size differs from declared physical flash capacity")
    if digest(raw) != expected_sha256:
        raise ValueError("Full backup SHA-256 mismatch")
    if not 2 <= len(addresses) <= 16 or not witnesses:
        raise ValueError("Provide 2..16 explicit image addresses and at least one witness")
    all_addresses = list(addresses) + list(witnesses)
    if len(set(all_addresses)) != len(all_addresses):
        raise ValueError("Images and witnesses must occupy distinct sectors")
    for address in all_addresses:
        if (isinstance(address, bool) or not isinstance(address, int) or address < 0 or
                address % SECTOR or address + SECTOR > len(raw)):
            raise ValueError("Each image/witness must be a complete in-bounds aligned sector")
    output = Path(output)
    output.mkdir(parents=True, mode=0o700)  # Never overwrite a previous preparation.
    (output / "restore").mkdir(mode=0o700)
    (output / "witnesses").mkdir(mode=0o700)

    def save(path, data):
        with os.fdopen(os.open(output / path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), "wb") as file:
            file.write(data)
        return {"path": path, "size": len(data), "sha256": digest(data)}

    family = {"esp32c3": "ESP32-C3", "esp32s3": "ESP32-S3"}[chip]
    images, restores, probes = [], [], []
    for index, address in enumerate(addresses):
        payload = bytes((index * 97 + offset) % 256 for offset in range(SECTOR))
        if payload == raw[address:address + SECTOR]:
            payload = bytes(value ^ 0xff for value in payload)
        images.append(save(f"pattern-{index}.bin", payload) | {"address": address})
        restores.append(save(f"restore/original-{index}.bin", raw[address:address + SECTOR]) | {"address": address})
    for index, address in enumerate(witnesses):
        probes.append(save(f"witnesses/before-{index}.bin", raw[address:address + SECTOR]) | {"address": address})

    def manifest(items, relative=False):
        return {"name": "IOBEWI private qualification patterns" if not relative else "Restore original sectors",
                "new_install_prompt_erase": False, "builds": [{"chipFamily": family, "parts": [
                    {"path": Path(item["path"]).name if relative else item["path"], "offset": item["address"]}
                    for item in items]}]}

    save("manifest.json", json.dumps(manifest(images), indent=2).encode())
    save("restore/manifest.json", json.dumps(manifest(restores, relative=True), indent=2).encode())
    plan = {"stage": "offline-preparation-only", "approved": False, "restoration_verified": False,
            "flash_operations_executed": 0, "chip": chip, "capacity": len(raw), "backup_sha256": expected_sha256,
            "erase_all": False, "return_console": False, "manifest_requires_args_override": True, "images": images, "restore": restores, "witnesses": probes,
            "requires": ["physical identity/capacity and current backup confirmed", "region ownership verified",
                         "operator approval of regions and interruption scenarios", "local grant and explicit consent",
                         "readback of patterns and unchanged witnesses", "restore and verify original bytes"]}
    save("review-plan.json", json.dumps(plan, indent=2).encode())
    return plan


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--backup", required=True, type=Path)
    parser.add_argument("--sha256", required=True)
    parser.add_argument("--flash-size", type=lambda value: int(value, 0), required=True)
    parser.add_argument("--chip", choices=("esp32c3", "esp32s3"), required=True)
    parser.add_argument("--image-address", type=lambda value: int(value, 0), action="append", required=True)
    parser.add_argument("--witness-address", type=lambda value: int(value, 0), action="append", required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    try:
        prepare(args.backup, args.sha256, args.chip, args.image_address, args.witness_address, args.output, flash_size=args.flash_size)
    except (ValueError, OSError) as error:
        parser.error(str(error))
    print("Offline preparation complete. No bench accessed, no flash written; review-plan.json requires approval.")


if __name__ == "__main__":
    main()
