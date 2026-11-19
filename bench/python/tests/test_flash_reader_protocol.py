"""Flash reader selection is validated by the broker protocol, without hardware."""
import sys
from pathlib import Path
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from protocol import Fault, validate_command

DEVICE = {"chip": "esp32p4", "flash_size": 32 * 1024 * 1024,
          "capabilities": ["flash.read"]}


@pytest.mark.parametrize("reader", [None, "reference", "preallocated"])
def test_read_reader_valid(reader):
    args = {"address": 0x10000, "size": 0x100000}
    if reader is not None:
        args["reader"] = reader
    validate_command("flash.read", args, DEVICE, {})


@pytest.mark.parametrize("reader", ["unknown", "", False, 3, [], {}, None])
def test_invalid_reader_rejected_without_broker_crash(reader):
    with pytest.raises(Fault):
        validate_command("flash.read",
                         {"address": 0x10000, "size": 0x100000, "reader": reader},
                         DEVICE, {})
