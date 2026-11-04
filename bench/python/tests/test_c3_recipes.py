"""Recipe regression checks only; these do not qualify a physical C3."""
import importlib.util
from pathlib import Path
import struct
from unittest.mock import Mock

import pytest

from iobewi_bench import OperationError

path = Path(__file__).resolve().parents[2] / "recipes/qualification_v2/test_c3.py"
spec = importlib.util.spec_from_file_location("c3_recipe_under_test", path)
recipe = importlib.util.module_from_spec(spec)
spec.loader.exec_module(recipe)


def case():
    campaign = Mock()
    board = campaign.reserve.return_value
    board.identify.return_value = {"chip": "esp32c3", "flash_size": 4 * 1024 * 1024}
    campaign.client.device.return_value = {"capabilities": ["reset"]}
    return campaign, board


def invalid_table(code="partition_table_invalid"):
    return OperationError({"id": "probe", "state": "failed", "error": {"code": code}})


def table_bytes(rows):
    return b"".join(b"\xaa\x50" + struct.pack("<BBII", 0, 0, r["address"], r["size"]) +
                    r["label"].encode().ljust(16, b"\0") + b"\0" * 4 for r in rows).ljust(4096, b"\xff")


def test_wrong_family_stops_before_flash_access():
    campaign, board = case()
    board.identify.return_value["chip"] = "esp32s3"
    with pytest.raises(AssertionError, match="Select a C3"):
        recipe.test_c3_identity_and_repeatable_read(campaign)
    board.read_flash.assert_not_called()


def test_c3_reads_must_be_exact_and_equal():
    campaign, board = case()
    board.read_flash.side_effect = [b"a" * 4096, b"b" * 4096]
    with pytest.raises(AssertionError, match="differ"):
        recipe.test_c3_identity_and_repeatable_read(campaign)
    board.read_flash.side_effect = [b"a", b"a"]
    with pytest.raises(AssertionError):
        recipe.test_c3_identity_and_repeatable_read(campaign)
    board.read_flash.side_effect = [b"a" * 4096, b"a" * 4096]
    recipe.test_c3_identity_and_repeatable_read(campaign)


def test_c3_partition_probe_and_independent_read():
    campaign, board = case()
    rows = [{"label": "app", "address": 0x20000, "size": 0x10000}]
    board.partitions.side_effect = [invalid_table(), rows]
    board.read_flash.return_value = table_bytes(rows)
    recipe.test_c3_partitions(campaign, [0x8000, 0x10000])
    board.read_flash.assert_called_once_with(address=0x10000, size=4096)


@pytest.mark.parametrize("answers", [[invalid_table(), invalid_table()],
    [[{"label": "a", "address": 0x20000, "size": 4096}]] * 2,
    [invalid_table("serial_disconnected")]])
def test_c3_partition_probe_never_hides_absence_ambiguity_or_transport_failure(answers):
    campaign, board = case()
    board.partitions.side_effect = answers
    with pytest.raises(AssertionError):
        recipe.test_c3_partitions(campaign, [0x8000, 0x10000])
    board.read_flash.assert_not_called()


def test_c3_rejects_a_raw_table_that_does_not_match_api():
    campaign, board = case()
    board.partitions.return_value = [{"label": "app", "address": 0x20000, "size": 0x10000}]
    board.read_flash.return_value = b"\xff" * 4096
    with pytest.raises(AssertionError, match="differs"):
        recipe.test_c3_partitions(campaign, [0x8000])


def test_native_c3_reset_requires_rom_observation_without_exposing_io0():
    campaign, board = case()
    board.command.return_value = {"reset": True, "method": "usb_serial_jtag", "boot_verified": False}
    recipe.test_c3_reset_and_rom_capture(None, campaign)
    board.wait_uart.assert_called_once_with(b"ESP-ROM:esp32c3", cursor=board.cursor.return_value, timeout=15)
    campaign.client.device.return_value["capabilities"].append("io0")
    with pytest.raises(AssertionError, match="must not expose"):
        recipe.test_c3_reset_and_rom_capture(None, campaign)


def test_reset_without_rom_marker_is_not_a_pass():
    campaign, board = case()
    board.command.return_value = {"reset": True, "method": "en", "boot_verified": False}
    board.wait_uart.side_effect = TimeoutError("no ROM")
    with pytest.raises(TimeoutError):
        recipe.test_c3_reset_and_rom_capture(None, campaign)
