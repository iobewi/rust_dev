import hashlib
import importlib.util
import json
from pathlib import Path

import pytest

source = Path(__file__).resolve().parents[2] / "tools/prepare_flash_qualification.py"
spec = importlib.util.spec_from_file_location("offline_flash_preparation", source)
preparation = importlib.util.module_from_spec(spec)
spec.loader.exec_module(preparation)


def backup(tmp_path):
    data = bytes(range(256)) * 64
    path = tmp_path / "backup.bin"
    path.write_bytes(data)
    return path, data, hashlib.sha256(data).hexdigest()


def test_offline_preparation_preserves_originals_and_builds_explicit_non_erasing_manifests(tmp_path):
    path, raw, digest = backup(tmp_path)
    output = tmp_path / "private-plan"
    plan = preparation.prepare(path, digest, "esp32c3", [0, 4096], [8192], output, flash_size=len(raw))
    assert path.read_bytes() == raw
    assert plan["approved"] is False and plan["flash_operations_executed"] == 0
    assert plan["restoration_verified"] is False
    assert plan["return_console"] is False and plan["manifest_requires_args_override"] is True
    for item in plan["images"]:
        assert (output / item["path"]).read_bytes() != raw[item["address"]:item["address"] + 4096]
    for item in plan["restore"] + plan["witnesses"]:
        data = (output / item["path"]).read_bytes()
        assert data == raw[item["address"]:item["address"] + 4096]
        assert hashlib.sha256(data).hexdigest() == item["sha256"]
        assert (output / item["path"]).stat().st_mode & 0o777 == 0o600
    manifest = json.loads((output / "manifest.json").read_text())
    assert manifest["new_install_prompt_erase"] is False
    assert manifest["builds"][0]["chipFamily"] == "ESP32-C3"
    assert [p["offset"] for p in manifest["builds"][0]["parts"]] == [0, 4096]
    restore = json.loads((output / "restore/manifest.json").read_text())
    assert all((output / "restore" / p["path"]).is_file() for p in restore["builds"][0]["parts"])
    with pytest.raises(FileExistsError):
        preparation.prepare(path, digest, "esp32c3", [0, 4096], [8192], output, flash_size=len(raw))


@pytest.mark.parametrize("images,witnesses", [([0], [8192]), ([0, 0], [8192]),
    ([0, 4096], [4096]), ([0, 4097], [8192]), ([0, 16384], [8192]), ([-4096, 0], [8192]),
    ([0, 4096], []), ([True, 4096], [8192])])
def test_unsafe_geometry_is_rejected_before_creating_outputs(tmp_path, images, witnesses):
    path, _, digest = backup(tmp_path)
    with pytest.raises(ValueError):
        preparation.prepare(path, digest, "esp32s3", images, witnesses, tmp_path / "plan", flash_size=16384)
    assert not (tmp_path / "plan").exists()


def test_wrong_backup_digest_is_rejected_before_preparing_images(tmp_path):
    path, _, _ = backup(tmp_path)
    with pytest.raises(ValueError, match="SHA-256"):
        preparation.prepare(path, "0" * 64, "esp32s3", [0, 4096], [8192], tmp_path / "plan", flash_size=16384)
    assert not (tmp_path / "plan").exists()


def test_partial_backup_cannot_be_presented_as_the_full_flash(tmp_path):
    path, _, digest = backup(tmp_path)
    with pytest.raises(ValueError, match="capacity"):
        preparation.prepare(path, digest, "esp32s3", [0, 4096], [8192], tmp_path / "plan", flash_size=32768)
    assert not (tmp_path / "plan").exists()
