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


@pytest.mark.parametrize("packet_size", [1024, 2048, 4096])
def test_packet_diagnostic_accepts_only_supported_sizes(packet_size):
    validate_command("flash.read", {
        "address": 0x8000, "size": 0x100000,
        "reader": "packet-diagnostic", "packet_size": packet_size
    }, DEVICE, {})


@pytest.mark.parametrize("packet_size", [None, 0, 512, 3072, 8192, -1, True, False, 1.5, "1024", {}, []])
def test_packet_diagnostic_refuses_invalid_or_missing_sizes(packet_size):
    args = {"address": 0x8000, "size": 0x100000, "reader": "packet-diagnostic"}
    if packet_size is not None:
        args["packet_size"] = packet_size
    with pytest.raises(Fault):
        validate_command("flash.read", args, DEVICE, {})


@pytest.mark.parametrize("reader", [None, "reference", "preallocated"])
def test_standard_readers_refuse_packet_size_override(reader):
    args = {"address": 0x8000, "size": 0x100000, "packet_size": 1024}
    if reader is not None:
        args["reader"] = reader
    with pytest.raises(Fault):
        validate_command("flash.read", args, DEVICE, {})


def test_md5_only_diagnostic_accepts_one_mib_read():
    validate_command("flash.read",
                     {"address": 0x8000, "size": 1024 * 1024,
                      "reader": "md5-only"}, DEVICE, {})


@pytest.mark.parametrize("args", [
    {"address": 0x8000, "size": 1024 * 1024 + 1, "reader": "md5-only"},
    {"address": 0x8000, "size": 1024, "reader": "md5-only", "packet_size": 4096},
    {"address": 0x8000, "size": 1024, "reader": "md5-only", "packet_size": None},
])
def test_md5_only_diagnostic_refuses_oversize_and_packet_overrides(args):
    with pytest.raises(Fault):
        validate_command("flash.read", args, DEVICE, {})
