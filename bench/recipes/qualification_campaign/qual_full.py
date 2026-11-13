"""P4-QUAL-FULL: pure support for the single qualification campaign (phases A-D). No network, no bench access; nothing here touches a card.

Constants (the read points and the three write sectors), deterministic test patterns, a parser of an ESP-IDF partition table that only reports structure (types, offsets, sizes, labels: never
data), a boot-criticality check for the write sectors, a streaming file comparison and an inventory of a flash image that reports counts and offsets, never content.
"""
import hashlib
import struct
from pathlib import Path

SECTOR = 4096
FLASH = 32 * 1024 * 1024
JEDEC = 0x1940C8                                   # 24 bits, as read by the agent (manufacturer, type, capacity)
UART_BAUD = 115200
READ_REGIONS = (("flash-start", 0x00000000), ("partition-table", 0x00008000), ("middle", 0x00800000),
                ("below-16MiB", 0x00FFF000), ("above-16MiB", 0x01000000), ("last-sector", 0x01FFF000))
D_SECTORS = (("D1", 0x00800000), ("D2", 0x01000000), ("D3", 0x01FFF000))
WRITE_STEPS = ("pattern-A", "blank", "pattern-B", "restore")        # see pattern_a / blank / pattern_b; 'restore' writes back the sector of BACKUP-A


def pattern_a(address):
    """4096 deterministic bytes that depend on every word's own address (an addressing error shows), with 0 and 1 bits in every position over the sector."""
    words = []
    for i in range(SECTOR // 4):
        w = ((address + 4 * i) * 0x9E3779B1 + 0x7F4A7C15) & 0xFFFFFFFF
        w ^= w >> 15
        w = (w * 0x2C1B3C6D) & 0xFFFFFFFF
        words.append(w ^ (w >> 12))
    return struct.pack("<%dI" % len(words), *words)


def pattern_b(address):
    """The bitwise complement of pattern A: every bit that was 1 must now be 0 and conversely (exercises the NOR transitions in both directions)."""
    return bytes(b ^ 0xFF for b in pattern_a(address))


def alias(address):
    """The sector an address would hit if the 24-bit flash addressing wrapped at 16 MiB (D2 -> sector 0, the bootloader). A write that landed there would not be seen by its own readback."""
    return address ^ 0x1000000


def blank():
    return b"\xff" * SECTOR


def plan_checks():
    """Static properties of the plan, asserted by the tests and by the recipe before anything runs."""
    assert all(a % SECTOR == 0 and 0 <= a <= FLASH - SECTOR for _, a in READ_REGIONS + D_SECTORS)
    assert len({a for _, a in D_SECTORS}) == 3
    return True


def partition_table(data):
    """Entries of an ESP-IDF partition table found in the 4096 bytes read at 0x8000, or None if there is none. Structure only."""
    entries = []
    for i in range(0, min(len(data), 0xC00), 32):
        magic, ptype, subtype, offset, size = struct.unpack_from("<HBBII", data, i)
        if magic == 0xEBEB:
            break
        if magic != 0x50AA or ptype not in (0, 1) and ptype > 0x7F:
            return entries or None
        label = data[i + 12:i + 28].split(b"\0")[0]
        if not label.isascii() or not all(32 <= c < 127 for c in label):
            return entries or None
        entries.append({"type": ptype, "subtype": subtype, "offset": offset, "size": size, "label": label.decode()})
    return entries or None


def boot_critical(address, table):
    """(True, why) if a sector must not be used for the write test: the first 64 KiB (bootloader, partition table), an application partition or the OTA data."""
    if address < 0x10000:
        return True, "within the first 64 KiB (second-stage bootloader and partition table)"
    for entry in table or []:
        if entry["offset"] <= address < entry["offset"] + entry["size"] and (entry["type"] == 0 or (entry["type"] == 1 and entry["subtype"] == 0)):
            return True, f"inside the {entry['label']!r} partition (type {entry['type']}, subtype {entry['subtype']})"
    return False, "no boot-critical structure found at this address"


def compare_files(path_a, path_b, block=1024 * 1024):
    """Streaming byte comparison. {equal, size_a, size_b, first_difference, differing_blocks}."""
    a, b = Path(path_a), Path(path_b)
    first, blocks, offset = None, 0, 0
    with a.open("rb") as fa, b.open("rb") as fb:
        while True:
            x, y = fa.read(block), fb.read(block)
            if not x and not y:
                break
            if x != y:
                blocks += 1
                if first is None:
                    first = offset + next((i for i, (p, q) in enumerate(zip(x, y)) if p != q), min(len(x), len(y)))
            offset += block
    return {"equal": first is None and a.stat().st_size == b.stat().st_size, "size_a": a.stat().st_size, "size_b": b.stat().st_size, "first_difference": first, "differing_blocks": blocks}


def inventory(path, block=1024 * 1024):
    """Counts and offsets only: non-0xFF bytes per MiB, the partition table structure, offsets that start like an application image. Never content."""
    per_mib, hints = [], []
    with Path(path).open("rb") as stream:
        index = 0
        while True:
            data = stream.read(block)
            if not data:
                break
            per_mib.append(len(data) - data.count(b"\xff"))
            if index == 0:
                table_bytes = data[0x8000:0x9000]
            for off in range(0, len(data), SECTOR):
                if data[off] == 0xE9 and 1 <= data[off + 1] <= 16 and len(hints) < 64:
                    hints.append(index * block + off)
            index += 1
    table = partition_table(table_bytes) if per_mib else None
    return {"size": Path(path).stat().st_size, "non_ff_bytes_per_mib": per_mib, "non_ff_total": sum(per_mib),
            "partition_table": table, "image_header_hints": [f"{h:#x}" for h in hints], "note": "counts and offsets only; no flash content"}


def sha256_file(path):
    digest = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()
