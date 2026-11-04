"""Small read-only recipe. No reset, erase, programming or local grant needed."""
import pytest

pytestmark = pytest.mark.bench


def test_identification_and_repeatable_read(board):
    identity = board.identify()
    assert identity["chip"] == "esp32s3"
    assert identity["flash_size"] >= 0x11000
    first = board.read_flash(address=0x10000, size=4096)
    second = board.read_flash(address=0x10000, size=4096)
    assert first == second
