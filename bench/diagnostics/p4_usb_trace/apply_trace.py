#!/usr/bin/env python3
"""Apply the IDLab read-only trace to esp-flasher-stub v1.2.2 + pinned esp-stub-lib.

Usage, in a clean checkout of https://github.com/espressif/esp-flasher-stub
at tag v1.2.2 with its pinned git submodule populated:
    python3 /path/to/apply_trace.py /path/to/esp-flasher-stub

Targets ESP32-P4 only. Modifies the local checkout, never a device.

Safeguards (each one fails closed):
- the checkout must be the exact upstream commit of v1.2.2 and the submodule the exact
  pinned commit, and both trees must be clean (``--allow-unverified`` skips only this
  check, for development; every insertion point must still be unique);
- the patch is applied to BOTH files or to none (nothing is written on any failure);
- a tree that already carries the IDLab trace is refused, never patched twice.
"""
from pathlib import Path
import subprocess
import sys

UPSTREAM_TAG = "v1.2.2"
UPSTREAM_COMMIT = "23959b780454adf885916d42f2274ec648e96a94"
SUBMODULE_COMMIT = "d61983fa4d1f66b9c088608c1f702ad877121279"
MARKER = "IDLab P4 trace"

COMMAND = "src/command_handler.c"
USB = "esp-stub-lib/src/usb_otg.c"


def replace_once(text, before, after, name):
    occurrences = text.count(before)
    if occurrences != 1:
        raise ValueError(f"{name}: expected one insertion point, found {occurrences}")
    return text.replace(before, after, 1)


TRACE_COUNTERS = r"""
/* IDLab P4 trace: cycles are CPU cycle counts, not microseconds. */
static volatile uint32_t idlab_fill_enabled;
static uint64_t idlab_fill_cycles;
static uint32_t idlab_fill_calls;
static inline uint32_t idlab_cycle32(void)
{
    uint32_t t;
    __asm__ volatile ("rdcycle %0" : "=r"(t));
    return t;
}
void idlab_otg_trace_start(void)
{
    idlab_fill_enabled = 0;
    idlab_fill_cycles = 0;
    idlab_fill_calls = 0;
    idlab_fill_enabled = 1;
}
void idlab_otg_trace_stop(void) { idlab_fill_enabled = 0; }
void idlab_otg_trace_snapshot(uint64_t *cycles, uint32_t *calls)
{
    *cycles = idlab_fill_cycles;
    *calls = idlab_fill_calls;
}
"""

COMMAND_COUNTERS = r"""
/* Diagnostic-only opcodes, do not modify ESP_READ_FLASH (0xD2).
 * 0xDF and 0xE0 lie inside the upstream plugin opcode range (0xD5..0xEF): in this
 * diagnostic image the explicit cases below shadow those two plugin slots. This image
 * must never be combined with plugins. */
#define IDLAB_READ_FLASH_TRACE 0xDF
#define IDLAB_GET_FLASH_TRACE  0xE0

/* The two opcodes as DATA, so that the image can be checked statically without relying on how
 * the compiler encodes the switch. */
const uint8_t idlab_trace_opcodes[2] __attribute__((used)) = {IDLAB_READ_FLASH_TRACE, IDLAB_GET_FLASH_TRACE};

extern void idlab_otg_trace_start(void);
extern void idlab_otg_trace_stop(void);
extern void idlab_otg_trace_snapshot(uint64_t *cycles, uint32_t *calls);
static uint64_t idlab_flash_cycles, idlab_md5_cycles;
static uint64_t idlab_send_cycles, idlab_ack_cycles;
static uint32_t idlab_data_packets, idlab_ack_count, idlab_bytes;
/* The IDT1 report is exactly one response payload. */
_Static_assert(MAX_RESPONSE_DATA_SIZE >= 64U, "IDT1 report must fit one response payload");
static inline uint32_t idlab_cycle32(void)
{
    uint32_t t;
    __asm__ volatile ("rdcycle %0" : "=r"(t));
    return t;
}
static void idlab_trace_reset(void)
{
    idlab_flash_cycles = idlab_md5_cycles = 0;
    idlab_send_cycles = idlab_ack_cycles = 0;
    idlab_data_packets = idlab_ack_count = idlab_bytes = 0;
}
static int idlab_get_flash_trace(const struct cmd_ctx *ctx,
                                 struct command_response_data *response)
{
    if (ctx->packet_size != 0) return RESPONSE_BAD_DATA_LEN;
    /* Liveness of the cycle counter: a counter that does not advance (inhibited) would
     * produce silent zeros. The probe then fails closed, before any bulk read. */
    const uint32_t c0 = idlab_cycle32();
    for (volatile uint32_t spin = 0; spin < 32U; spin++) { }
    if ((uint32_t)(idlab_cycle32() - c0) == 0U) return RESPONSE_CMD_NOT_IMPLEMENTED;
    uint8_t *ptr = response->data;
    const uint32_t magic = 0x31544449U; /* IDT1 in little endian */
    const uint32_t version = 1;
    uint64_t fill_cycles = 0;
    uint32_t fill_calls = 0;
    idlab_otg_trace_snapshot(&fill_cycles, &fill_calls);
    memcpy(ptr, &magic, 4); ptr += 4;
    memcpy(ptr, &version, 4); ptr += 4;
    memcpy(ptr, &idlab_flash_cycles, 8); ptr += 8;
    memcpy(ptr, &idlab_md5_cycles, 8); ptr += 8;
    memcpy(ptr, &idlab_send_cycles, 8); ptr += 8;
    memcpy(ptr, &idlab_ack_cycles, 8); ptr += 8;
    memcpy(ptr, &fill_cycles, 8); ptr += 8;
    memcpy(ptr, &fill_calls, 4); ptr += 4;
    memcpy(ptr, &idlab_data_packets, 4); ptr += 4;
    memcpy(ptr, &idlab_ack_count, 4); ptr += 4;
    memcpy(ptr, &idlab_bytes, 4); ptr += 4;
    response->data_size = 64;
    return RESPONSE_SUCCESS;
}
"""


def build_patched(texts):
    """Pure transformation: ``{path: text}`` of the two upstream files -> patched texts."""
    for path in (COMMAND, USB):
        if path not in texts:
            raise ValueError(f"missing source: {path}")
        if MARKER in texts[path] or "idlab_" in texts[path]:
            raise ValueError(f"{path}: the IDLab trace is already applied")
    usb = texts[USB]
    for before, after in [
        ('uint8_t stub_lib_usb_otg_tx_one_char(uint8_t c)\n{\n    s_cdcacm_txbuf[s_cdcacm_txpos++] = c;',
         TRACE_COUNTERS + '\nuint8_t stub_lib_usb_otg_tx_one_char(uint8_t c)\n{\n    s_cdcacm_txbuf[s_cdcacm_txpos++] = c;'),
        ('''    esp_rom_cdc_acm_fifo_fill(uart_acm_dev, s_cdcacm_txbuf, s_cdcacm_txpos);
    s_cdcacm_txpos = 0;''',
         '''    uint32_t t0 = idlab_cycle32();
    esp_rom_cdc_acm_fifo_fill(uart_acm_dev, s_cdcacm_txbuf, s_cdcacm_txpos);
    uint32_t cycles = idlab_cycle32() - t0;
    if (idlab_fill_enabled) {
        idlab_fill_cycles += cycles;
        idlab_fill_calls++;
    }
    s_cdcacm_txpos = 0;'''),
    ]:
        usb = replace_once(usb, before, after, USB)
    command = texts[COMMAND]
    for before, after in [
        ('static int s_read_flash_post_process(const struct cmd_ctx *ctx)\n{',
         COMMAND_COUNTERS + '\nstatic int s_read_flash_post_process(const struct cmd_ctx *ctx)\n{'),
        ('''    struct stub_lib_md5_ctx md5_ctx;
    stub_lib_md5_init(&md5_ctx);

    while (read_size_remaining > 0 || acked_data_size < read_size) {''',
         '''    const bool tracing = (ctx->command == IDLAB_READ_FLASH_TRACE);
    if (tracing) { idlab_trace_reset(); idlab_otg_trace_start(); }
    uint32_t ack_started = 0;
    bool ack_pending = false;
    struct stub_lib_md5_ctx md5_ctx;
    stub_lib_md5_init(&md5_ctx);

    while (read_size_remaining > 0 || acked_data_size < read_size) {'''),
        ('''            memcpy(&acked_data_size, ack_data, sizeof(acked_data_size));
            acked_packets++;
            ctx->transport->recv_release();''',
         '''            if (tracing && ack_pending) {
                idlab_ack_cycles += (uint32_t)(idlab_cycle32() - ack_started);
                idlab_ack_count++;
                ack_pending = false;
            }
            memcpy(&acked_data_size, ack_data, sizeof(acked_data_size));
            acked_packets++;
            ctx->transport->recv_release();'''),
        ('''            if (stub_lib_flash_read_buff(aligned_offset, data, aligned_size) != STUB_LIB_OK) {
                return RESPONSE_FAILED_SPI_OP;
            }''',
         '''            uint32_t t_flash = tracing ? idlab_cycle32() : 0;
            int flash_status = stub_lib_flash_read_buff(aligned_offset, data, aligned_size);
            if (tracing) idlab_flash_cycles += (uint32_t)(idlab_cycle32() - t_flash);
            if (flash_status != STUB_LIB_OK) {
                if (tracing) idlab_otg_trace_stop();
                return RESPONSE_FAILED_SPI_OP;
            }'''),
        ('''            stub_lib_md5_update(&md5_ctx, actual_data, actual_read_size);
            ctx->transport->send_frame(actual_data, actual_read_size);
            offset += actual_read_size;''',
         '''            uint32_t t_md5 = tracing ? idlab_cycle32() : 0;
            stub_lib_md5_update(&md5_ctx, actual_data, actual_read_size);
            if (tracing) idlab_md5_cycles += (uint32_t)(idlab_cycle32() - t_md5);
            uint32_t t_send = tracing ? idlab_cycle32() : 0;
            ctx->transport->send_frame(actual_data, actual_read_size);
            if (tracing) {
                idlab_send_cycles += (uint32_t)(idlab_cycle32() - t_send);
                idlab_data_packets++;
                idlab_bytes += actual_read_size;
                ack_started = idlab_cycle32();
                ack_pending = true;
            }
            offset += actual_read_size;'''),
        ('''    ctx->transport->send_frame(md5, sizeof(md5));

    return RESPONSE_SUCCESS;''',
         '''    if (tracing) idlab_otg_trace_stop();
    ctx->transport->send_frame(md5, sizeof(md5));

    return RESPONSE_SUCCESS;'''),
        ('''    case ESP_READ_FLASH:
        accumulated_result = s_read_flash(&ctx);
        break;''',
         '''    case ESP_READ_FLASH:
        accumulated_result = s_read_flash(&ctx);
        break;

    case IDLAB_READ_FLASH_TRACE:
        accumulated_result = s_read_flash(&ctx);
        break;

    case IDLAB_GET_FLASH_TRACE:
        accumulated_result = idlab_get_flash_trace(&ctx, &response);
        break;'''),
    ]:
        command = replace_once(command, before, after, COMMAND)
    return {USB: usb, COMMAND: command}


def _git(root, *args):
    result = subprocess.run(["git", "-C", str(root), *args], capture_output=True, text=True)
    if result.returncode != 0:
        raise ValueError(f"git {' '.join(args)} failed in {root}: {result.stderr.strip()}")
    return result.stdout.strip()


def verify_upstream(root):
    """Exact upstream commit, exact submodule commit, both trees clean."""
    root = Path(root)
    head = _git(root, "rev-parse", "HEAD")
    if head != UPSTREAM_COMMIT:
        raise ValueError(f"checkout is {head}, expected {UPSTREAM_TAG} = {UPSTREAM_COMMIT}")
    pinned = _git(root, "ls-tree", "HEAD", "esp-stub-lib").split()[2]
    if pinned != SUBMODULE_COMMIT:
        raise ValueError(f"submodule is pinned to {pinned}, expected {SUBMODULE_COMMIT}")
    present = _git(root / "esp-stub-lib", "rev-parse", "HEAD")
    if present != SUBMODULE_COMMIT:
        raise ValueError(f"esp-stub-lib is checked out at {present}, expected {SUBMODULE_COMMIT}")
    for where in (root, root / "esp-stub-lib"):
        if _git(where, "status", "--porcelain"):
            raise ValueError(f"{where} is not clean")


def patch(root, verify=True):
    root = Path(root).resolve()
    for path in (COMMAND, USB):
        if not (root / path).is_file():
            raise ValueError("Expected esp-flasher-stub with populated esp-stub-lib submodule")
    if verify:
        verify_upstream(root)
    patched = build_patched({path: (root / path).read_text() for path in (COMMAND, USB)})
    # Both files are computed before anything is written: no partial application.
    for path, text in patched.items():
        (root / path).write_text(text)
        print(f"patched {path}")
    print("Local source instrumentation complete; BUILD AND TEST before any device use.")


if __name__ == "__main__":
    args = [a for a in sys.argv[1:] if a != "--allow-unverified"]
    if len(args) != 1:
        raise SystemExit("Usage: apply_trace.py [--allow-unverified] <esp-flasher-stub-v1.2.2-checkout>")
    try:
        patch(args[0], verify="--allow-unverified" not in sys.argv[1:])
    except ValueError as error:
        raise SystemExit(f"refused: {error}")
