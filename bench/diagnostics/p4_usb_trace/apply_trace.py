#!/usr/bin/env python3
"""Apply the IDLab read-only trace to esp-flasher-stub v1.2.2 + pinned esp-stub-lib.

Usage, in a clean checkout of https://github.com/espressif/esp-flasher-stub
at tag v1.2.2 with its pinned git submodule populated:
    python3 /path/to/apply_trace.py /path/to/esp-flasher-stub

Targets ESP32-P4 only. Modifies the local checkout, never a device.
"""
from pathlib import Path
import sys

def replace_once(text, before, after, name):
    occurrences = text.count(before)
    if occurrences != 1:
        raise ValueError(f"{name}: expected one insertion point, found {occurrences}")
    return text.replace(before, after, 1)

def patch_file(root, path, replacements):
    p = root / path
    old = p.read_text()
    updated = old
    for before, after in replacements:
        updated = replace_once(updated, before, after, str(p))
    p.write_text(updated)
    print(f"patched {path}")

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
/* Diagnostic-only opcodes, do not modify ESP_READ_FLASH (0xD2). */
#define IDLAB_READ_FLASH_TRACE 0xDF
#define IDLAB_GET_FLASH_TRACE  0xE0

extern void idlab_otg_trace_start(void);
extern void idlab_otg_trace_stop(void);
extern void idlab_otg_trace_snapshot(uint64_t *cycles, uint32_t *calls);
static uint64_t idlab_flash_cycles, idlab_md5_cycles;
static uint64_t idlab_send_cycles, idlab_ack_cycles;
static uint32_t idlab_data_packets, idlab_ack_count, idlab_bytes;
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

def patch(root):
    root = Path(root).resolve()
    command = "src/command_handler.c"
    usb = "esp-stub-lib/src/usb_otg.c"
    if not (root / command).is_file() or not (root / usb).is_file():
        raise ValueError("Expected esp-flasher-stub with populated esp-stub-lib submodule")

    patch_file(root, usb, [
        ('uint8_t stub_lib_usb_otg_tx_one_char(uint8_t c)\n{',
         TRACE_COUNTERS + '\nuint8_t stub_lib_usb_otg_tx_one_char(uint8_t c)\n{'),
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
    ])
    patch_file(root, command, [
        ('static int s_read_flash_post_process(const struct cmd_ctx *ctx)\n{',
         COMMAND_COUNTERS + '\nstatic int s_read_flash_post_process(const struct cmd_ctx *ctx)\n{'),
        ('''    struct stub_lib_md5_ctx md5_ctx;
    stub_lib_md5_init(&md5_ctx);

    while (read_size_remaining > 0 || acked_data_size < read_size) {''',
         '''    const bool tracing = (ctx->command == IDLAB_READ_FLASH_TRACE);
    if (tracing) { idlab_trace_reset(); idlab_otg_trace_start(); }
    uint32_t ack_started = 0;
    struct stub_lib_md5_ctx md5_ctx;
    stub_lib_md5_init(&md5_ctx);

    while (read_size_remaining > 0 || acked_data_size < read_size) {'''),
        ('''            memcpy(&acked_data_size, ack_data, sizeof(acked_data_size));
            acked_packets++;
            ctx->transport->recv_release();''',
         '''            if (tracing && ack_started != 0) {
                idlab_ack_cycles += (uint32_t)(idlab_cycle32() - ack_started);
                idlab_ack_count++;
                ack_started = 0;
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
    ])
    print("Local source instrumentation complete; BUILD AND TEST before any device use.")

if __name__ == "__main__":
    if len(sys.argv) != 2:
        raise SystemExit("Usage: apply_trace.py <esp-flasher-stub-v1.2.2-checkout>")
    patch(sys.argv[1])
