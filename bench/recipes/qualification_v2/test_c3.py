"""C3 identity, partition and reset qualification. Never programs flash."""
import struct

import pytest

from iobewi_bench import OperationError

pytestmark = pytest.mark.bench


def _c3(qualification):
    board = qualification.reserve()
    identity = board.identify()
    assert identity["chip"] == "esp32c3", "Select a C3; this is not the S3 recipe"
    assert isinstance(identity["flash_size"], int) and identity["flash_size"] >= 4096
    qualification.write("c3-identity.json", {"chip": identity["chip"], "flash_size": identity["flash_size"]})
    return board, identity["flash_size"]


def test_c3_identity_and_repeatable_read(qualification):
    board, _ = _c3(qualification)
    first = board.read_flash(address=0, size=4096)
    second = board.read_flash(address=0, size=4096)
    assert len(first) == len(second) == 4096
    assert first == second, "C3 flash reads differ"


def test_c3_partitions(qualification, c3_table_addresses):
    board, flash_size = _c3(qualification)
    valid, probes = [], []
    for address in c3_table_addresses:
        assert 0 <= address <= flash_size - 4096 and address % 4096 == 0, "Invalid table read address"
        try:
            rows = board.partitions(address=address)
        except OperationError as error:
            assert error.operation["state"] == "failed"
            assert error.operation["error"]["code"] == "partition_table_invalid", "Unexpected probe failure"
            probes.append({"address": address, "result": "partition_table_invalid"})
        else:
            assert rows, "Empty partition table"
            probes.append({"address": address, "result": "valid"})
            valid.append((address, rows))
    qualification.write("c3-table-probes.json", probes)
    assert len(valid) == 1, "Expected one valid table; specify the firmware's address if absent or ambiguous"
    address, rows = valid[0]
    raw = board.read_flash(address=address, size=4096)
    decoded = []
    for offset in range(0, 0xc00, 32):
        entry = raw[offset:offset + 32]
        if entry[:2] != b"\xaa\x50":
            break
        _, _, start, size = struct.unpack("<BBII", entry[2:12])
        label = entry[12:28].split(b"\0", 1)[0].decode("utf-8")
        decoded.append((label, start, size))
    assert decoded == [(row["label"], row["address"], row["size"]) for row in rows], "Raw table differs from API"
    previous_end = 0
    for row in sorted(rows, key=lambda r: r["address"]):
        assert row["size"] > 0 and row["address"] >= previous_end, "Partition overlap"
        previous_end = row["address"] + row["size"]
        assert previous_end <= flash_size, "Partition beyond flash capacity"
    qualification.write("c3-partitions.json", {"table_address": address, "entries": rows})


def test_c3_reset_and_rom_capture(c3_reset_enabled, qualification):
    board, _ = _c3(qualification)
    capabilities = set(qualification.client.device(qualification.device)["capabilities"])
    if "reset" not in capabilities:
        pytest.skip("No reset capability exposed by this transport")
    cursor = board.cursor()
    result = board.command("reset", timeout=30)
    assert result.get("reset") is True
    assert result.get("method") in {"en", "usb_serial_jtag"}
    assert result.get("boot_verified") is False  # Sending reset is not proof of boot.
    if result["method"] == "usb_serial_jtag":
        assert "io0" not in capabilities, "Native USB must not expose BOOT/IO0"
    board.wait_uart(b"ESP-ROM:esp32c3", cursor=cursor, timeout=15)
    qualification.write("c3-reset.json", {"method": result["method"], "rom_marker_observed": True,
                                          "firmware_boot_verified": False})
