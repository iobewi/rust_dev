#!/usr/bin/env python3
"""Static qualification of the P4 diagnostic stub: ELF + loader JSON, no device involved.

The pure checks (`check_stub`, `compare`) take plain dictionaries, so they are unit tested
without a compiler. `extract` reads a real ELF (pyelftools + the riscv32-esp-elf objdump).

A PASS here means: the image is consistent and fits the RAM regions the stub linker script
declares. It does NOT mean the image works on a chip: that is a hardware question.
"""
import base64
import hashlib
import json
import re
import subprocess
import sys
from pathlib import Path

# src/ld/esp32p4.ld of esp-flasher-stub v1.2.2
REGIONS = {"iram": (0x4FF50000, 0x4500), "dram": (0x4FF60000, 0x18000)}
SECTION_REGION = {".text": "iram", ".bss": "dram", ".data": "dram"}
JSON_KEYS = {"entry", "text", "text_start", "data", "data_start", "bss_start"}
IDLAB_SYMBOL_PREFIX = "idlab_"
OPCODES = (0xDF, 0xE0)  # IDLAB_READ_FLASH_TRACE, IDLAB_GET_FLASH_TRACE


def sha256(data):
    return hashlib.sha256(data).hexdigest()


def extract(elf_path, toolchain_bin):
    """Reads an ELF: sections (address, size, bytes), entry, symbols, and disassembly counters."""
    from elftools.elf.elffile import ELFFile  # imported here: only the real build needs it
    path = Path(elf_path)
    with open(path, "rb") as stream:
        elf = ELFFile(stream)
        sections = {}
        for section in elf.iter_sections():
            if section.name in SECTION_REGION:
                data = b"" if section["sh_type"] == "SHT_NOBITS" else section.data()
                sections[section.name] = {"addr": section["sh_addr"], "size": section["sh_size"],
                                          "data": data}
        symtab = elf.get_section_by_name(".symtab")
        symbols = {sym.name: {"addr": sym["st_value"], "size": sym["st_size"]}
                   for sym in symtab.iter_symbols() if sym.name} if symtab else {}
        entry = elf.header["e_entry"]
    objdump = Path(toolchain_bin) / "riscv32-esp-elf-objdump"
    disassembly = subprocess.run([str(objdump), "-d", str(path)], capture_output=True, text=True,
                                 check=True).stdout
    return {"sha256": sha256(path.read_bytes()), "entry": entry, "sections": sections, "symbols": symbols,
            "rdcycle": len(re.findall(r"\brdcycle\b", disassembly))}


def symbol_bytes(info, name):
    """The bytes of a data symbol, read from the ELF section that holds it (None if absent)."""
    symbol = info["symbols"].get(name)
    if symbol is None:
        return None
    for section in info["sections"].values():
        if section["addr"] <= symbol["addr"] and symbol["addr"] + symbol["size"] <= _end(section):
            start = symbol["addr"] - section["addr"]
            return section["data"][start:start + symbol["size"]] if section["data"] else None
    return None


def _check(name, ok, detail=""):
    return {"check": name, "ok": bool(ok), "detail": detail}


def _end(section):
    return section["addr"] + section["size"]


def check_stub(info, loader_json, *, traced):
    """Pure checks of one image. `info` comes from `extract`, `loader_json` is the parsed JSON."""
    out = []
    sections = info["sections"]
    out.append(_check("the three loadable sections exist", set(sections) == set(SECTION_REGION), sorted(sections)))
    for name, region in SECTION_REGION.items():
        if name not in sections:
            continue
        base, length = REGIONS[region]
        inside = base <= sections[name]["addr"] and _end(sections[name]) <= base + length
        out.append(_check(f"{name} is inside {region}", inside,
                          {"start": hex(sections[name]["addr"]), "end": hex(_end(sections[name])),
                           "region": [hex(base), hex(base + length)],
                           "free_bytes": base + length - _end(sections[name])}))
    names = sorted(sections)
    overlaps = [(a, b) for i, a in enumerate(names) for b in names[i + 1:]
                if sections[a]["addr"] < _end(sections[b]) and sections[b]["addr"] < _end(sections[a])]
    out.append(_check("no two sections overlap", not overlaps, overlaps))
    if ".text" in sections:
        out.append(_check("the entry point is inside .text",
                          sections[".text"]["addr"] <= info["entry"] < _end(sections[".text"]), hex(info["entry"])))
    # --- loader JSON consistency
    out.append(_check("the loader JSON has exactly the expected keys", set(loader_json) == JSON_KEYS,
                      sorted(set(loader_json) ^ JSON_KEYS)))
    if set(loader_json) == JSON_KEYS and {".text", ".data", ".bss"} <= set(sections):
        out.append(_check("JSON entry equals the ELF entry", loader_json["entry"] == info["entry"]))
        out.append(_check("JSON text_start equals .text", loader_json["text_start"] == sections[".text"]["addr"]))
        out.append(_check("JSON data_start equals .data", loader_json["data_start"] == sections[".data"]["addr"]))
        out.append(_check("JSON bss_start equals .bss", loader_json["bss_start"] == sections[".bss"]["addr"]))
        for key, name in (("text", ".text"), ("data", ".data")):
            blob, raw = base64.b64decode(loader_json[key]), sections[name]["data"]
            padded = len(raw) + (-len(raw)) % 4  # the upstream loader JSON pads each segment to 4 bytes
            out.append(_check(f"JSON {key} bytes equal the ELF {name} (4-byte padded with zeros)",
                              blob[:len(raw)] == raw and len(blob) == padded and not any(blob[len(raw):]),
                              {"elf": len(raw), "json": len(blob)}))
    # --- the diagnostic content
    idlab = [s for s in info["symbols"] if s.startswith(IDLAB_SYMBOL_PREFIX)]
    table = symbol_bytes(info, "idlab_trace_opcodes")
    if traced:
        out.append(_check("the diagnostic counters are linked in", len(idlab) >= 10, len(idlab)))
        out.append(_check("the cycle counter is read by the image", info["rdcycle"] >= 10, info["rdcycle"]))
        out.append(_check("the linked opcode table is exactly 0xDF (trace read) and 0xE0 (counters)",
                          table == bytes(OPCODES), table.hex() if table else None))
    else:
        out.append(_check("the standard image has no diagnostic symbol", not idlab, idlab))
        out.append(_check("the standard image never reads the cycle counter", info["rdcycle"] == 0, info["rdcycle"]))
        out.append(_check("the standard image has no diagnostic opcode table", table is None))
    return out


def compare(stock, traced):
    """Size and layout delta between the standard and the diagnostic image."""
    delta = {name: traced["sections"][name]["size"] - stock["sections"][name]["size"] for name in SECTION_REGION}
    same_layout = all(stock["sections"][n]["addr"] == traced["sections"][n]["addr"] for n in (".text", ".bss"))
    return {"size_delta_bytes": delta, "text_and_bss_start_unchanged": same_layout,
            "entry_stock": hex(stock["entry"]), "entry_traced": hex(traced["entry"])}


def qualify(stock_elf, stock_json, traced_elf, traced_json, toolchain_bin):
    stock, traced = extract(stock_elf, toolchain_bin), extract(traced_elf, toolchain_bin)
    stock_checks = check_stub(stock, json.loads(Path(stock_json).read_text()), traced=False)
    traced_checks = check_stub(traced, json.loads(Path(traced_json).read_text()), traced=True)
    ok = all(c["ok"] for c in stock_checks + traced_checks)
    return {"qualified_statically": ok, "stock": stock_checks, "traced": traced_checks,
            "comparison": compare(stock, traced),
            "images": {"stock_elf_sha256": stock["sha256"], "traced_elf_sha256": traced["sha256"],
                       "stock_json_sha256": sha256(Path(stock_json).read_bytes()),
                       "traced_json_sha256": sha256(Path(traced_json).read_bytes())},
            "limits": "Static checks only: consistent and fits the declared RAM; says nothing about hardware."}


if __name__ == "__main__":
    if len(sys.argv) != 6:
        raise SystemExit("Usage: qualify_stub.py STOCK.elf STOCK.json TRACED.elf TRACED.json TOOLCHAIN_BIN")
    report = qualify(*sys.argv[1:])
    print(json.dumps(report, indent=2, default=str))
    raise SystemExit(0 if report["qualified_statically"] else 1)
