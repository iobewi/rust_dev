"""Offline tests of the P4 trace patch generator (no network, no hardware, no compiler)."""
import importlib.util
import subprocess
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[2] / "diagnostics" / "p4_usb_trace" / "apply_trace.py"
spec = importlib.util.spec_from_file_location("apply_trace", SCRIPT)
apply_trace = importlib.util.module_from_spec(spec)
spec.loader.exec_module(apply_trace)

# Literal excerpts of esp-flasher-stub v1.2.2 / esp-stub-lib d61983fa around the insertion points.
USB = '''#include <stdint.h>
uint8_t stub_lib_usb_otg_tx_one_char(uint8_t c)
{
    s_cdcacm_txbuf[s_cdcacm_txpos++] = c;
    return 0;
}

void stub_lib_usb_otg_tx_flush(void)
{
    esp_rom_cdc_acm_fifo_fill(uart_acm_dev, s_cdcacm_txbuf, s_cdcacm_txpos);
    s_cdcacm_txpos = 0;
}
'''
COMMAND = '''static int s_read_flash_post_process(const struct cmd_ctx *ctx)
{
    struct stub_lib_md5_ctx md5_ctx;
    stub_lib_md5_init(&md5_ctx);

    while (read_size_remaining > 0 || acked_data_size < read_size) {
            memcpy(&acked_data_size, ack_data, sizeof(acked_data_size));
            acked_packets++;
            ctx->transport->recv_release();
            if (stub_lib_flash_read_buff(aligned_offset, data, aligned_size) != STUB_LIB_OK) {
                return RESPONSE_FAILED_SPI_OP;
            }
            stub_lib_md5_update(&md5_ctx, actual_data, actual_read_size);
            ctx->transport->send_frame(actual_data, actual_read_size);
            offset += actual_read_size;
    ctx->transport->send_frame(md5, sizeof(md5));

    return RESPONSE_SUCCESS;
    case ESP_READ_FLASH:
        accumulated_result = s_read_flash(&ctx);
        break;
'''
TEXTS = {apply_trace.USB: USB, apply_trace.COMMAND: COMMAND}


def test_every_insertion_point_is_used_exactly_once():
    patched = apply_trace.build_patched(TEXTS)
    usb, command = patched[apply_trace.USB], patched[apply_trace.COMMAND]
    assert usb.count("idlab_cycle32()") == 2 and usb.count("void idlab_otg_trace_start") == 1  # two reads around the fill
    assert command.count("case IDLAB_READ_FLASH_TRACE:") == 1
    assert command.count("case IDLAB_GET_FLASH_TRACE:") == 1
    assert command.count("idlab_otg_trace_stop();") == 2  # flash error path and before the MD5 frame


def test_the_standard_read_flash_opcode_is_untouched():
    command = apply_trace.build_patched(TEXTS)[apply_trace.COMMAND]
    assert "case ESP_READ_FLASH:\n        accumulated_result = s_read_flash(&ctx);\n        break;" in command
    assert "0xD2" in command and "do not modify ESP_READ_FLASH" in command


def test_an_already_patched_tree_is_refused():
    once = apply_trace.build_patched(TEXTS)
    with pytest.raises(ValueError, match="already applied"):
        apply_trace.build_patched(once)


@pytest.mark.parametrize("path", [apply_trace.USB, apply_trace.COMMAND])
def test_a_missing_or_duplicated_insertion_point_is_refused(path):
    missing = dict(TEXTS)
    missing[path] = TEXTS[path].replace("send_frame", "send_other", 1) if path == apply_trace.COMMAND \
        else TEXTS[path].replace("esp_rom_cdc_acm_fifo_fill", "other_fill")
    with pytest.raises(ValueError, match="expected one insertion point, found 0"):
        apply_trace.build_patched(missing)
    duplicated = dict(TEXTS)
    duplicated[path] = TEXTS[path] + TEXTS[path]
    with pytest.raises(ValueError, match="found 2"):
        apply_trace.build_patched(duplicated)


def test_nothing_is_written_when_the_second_file_fails(tmp_path):
    (tmp_path / "src").mkdir()
    (tmp_path / "esp-stub-lib/src").mkdir(parents=True)
    (tmp_path / apply_trace.USB).write_text(USB)
    (tmp_path / apply_trace.COMMAND).write_text(COMMAND.replace("send_frame(md5", "send_frame(other"))
    with pytest.raises(ValueError):
        apply_trace.patch(tmp_path, verify=False)
    assert (tmp_path / apply_trace.USB).read_text() == USB  # first file not modified


def test_patching_a_directory_writes_both_files(tmp_path):
    (tmp_path / "src").mkdir()
    (tmp_path / "esp-stub-lib/src").mkdir(parents=True)
    (tmp_path / apply_trace.USB).write_text(USB)
    (tmp_path / apply_trace.COMMAND).write_text(COMMAND)
    apply_trace.patch(tmp_path, verify=False)
    assert "idlab_" in (tmp_path / apply_trace.USB).read_text()
    assert "idlab_" in (tmp_path / apply_trace.COMMAND).read_text()


def _repo(path, with_submodule_name="esp-stub-lib"):
    subprocess.run(["git", "init", "-q", str(path)], check=True)
    for key, value in (("user.email", "t@example.org"), ("user.name", "t")):
        subprocess.run(["git", "-C", str(path), "config", key, value], check=True)
    (path / "f.txt").write_text("x")
    subprocess.run(["git", "-C", str(path), "add", "."], check=True)
    subprocess.run(["git", "-C", str(path), "commit", "-q", "-m", "c"], check=True)


def test_a_checkout_that_is_not_the_pinned_upstream_commit_is_refused(tmp_path):
    _repo(tmp_path)
    with pytest.raises(ValueError, match="expected v1.2.2"):
        apply_trace.verify_upstream(tmp_path)


def test_a_directory_without_git_is_refused(tmp_path):
    with pytest.raises(ValueError, match="git rev-parse HEAD failed"):
        apply_trace.verify_upstream(tmp_path)


def test_the_pinned_identifiers_are_the_documented_ones():
    readme = (SCRIPT.parent / "README.md").read_text()
    for value in (apply_trace.UPSTREAM_TAG, apply_trace.SUBMODULE_COMMIT):
        assert value in readme


def test_the_patched_c_keeps_the_report_layout_documented_in_the_readme():
    command = apply_trace.build_patched(TEXTS)[apply_trace.COMMAND]
    order = ["idlab_flash_cycles", "idlab_md5_cycles", "idlab_send_cycles", "idlab_ack_cycles", "fill_cycles",
             "fill_calls", "idlab_data_packets", "idlab_ack_count", "idlab_bytes"]
    positions = []
    for name in order:
        marker = f"memcpy(ptr, &{name}"
        assert marker in command, name
        positions.append(command.index(marker))
    assert positions == sorted(positions)
    assert "response->data_size = 64;" in command and "0x31544449U" in command
    assert "_Static_assert(MAX_RESPONSE_DATA_SIZE >= 64U" in command
    assert "RESPONSE_CMD_NOT_IMPLEMENTED" in command  # cycle counter liveness fails closed
