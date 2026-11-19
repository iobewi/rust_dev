/*
 * Host harness for the IDLab P4 trace: drives the REAL patched command_handler.c and usb_otg.c
 * (compiled natively, only the `rdcycle` read replaced by a simulated clock) through a scripted
 * transport. It checks the counter semantics, the error paths and the IDT1 report. It is NOT a
 * hardware test: nothing here proves the ROM, the USB timing or the real cycle counter.
 *
 * Output: one `key=value` line per check, `RESULT ...` lines for the Python driver.
 */
#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

#include "command_handler.h"
#include "commands.h"
#include "transport.h"

/* ---- simulated cycle counter (replaces `rdcycle`) ---------------------------------------- */
static uint32_t g_clock;           /* the simulated 32-bit counter */
static uint32_t g_read_step = 1;   /* added after every read; 0 simulates an inhibited counter */
uint32_t idlab_test_cycles(void)
{
    uint32_t value = g_clock;
    g_clock += g_read_step;
    return value;
}

/* ---- costs charged to the simulated clock ------------------------------------------------- */
static uint32_t g_flash_cost = 1000, g_md5_cost = 200, g_fill_cost = 50, g_ack_cost = 700;

/* ---- fakes for the stub library ------------------------------------------------------------ */
struct stub_lib_md5_ctx_fake { uint64_t sum; };
static uint32_t g_flash_calls, g_fail_flash_at = 0;
int stub_lib_flash_read_buff(uint32_t addr, void *buffer, uint32_t size)
{
    g_flash_calls++;
    if (g_fail_flash_at != 0 && g_flash_calls == g_fail_flash_at) return 1; /* != STUB_LIB_OK */
    for (uint32_t i = 0; i < size; i++) ((uint8_t *)buffer)[i] = (uint8_t)((addr + i) * 7U);
    g_clock += g_flash_cost;
    return 0; /* STUB_LIB_OK */
}
static uint32_t g_md5_updates;
void stub_lib_md5_init(void *ctx) { memset(ctx, 0, sizeof(struct stub_lib_md5_ctx_fake)); }
void stub_lib_md5_update(void *ctx, const uint8_t *data, uint32_t len)
{
    struct stub_lib_md5_ctx_fake *c = ctx;
    for (uint32_t i = 0; i < len; i++) c->sum += data[i];
    g_md5_updates++;
    g_clock += g_md5_cost;
}
void stub_lib_md5_final(void *ctx, uint8_t digest[16])
{
    struct stub_lib_md5_ctx_fake *c = ctx;
    memset(digest, 0, 16);
    memcpy(digest, &c->sum, sizeof(c->sum));
}

/* ---- fakes for the ROM CDC FIFO used by usb_otg.c ---------------------------------------- */
void *uart_acm_dev;
static uint32_t g_rom_fill_calls;
int esp_rom_cdc_acm_fifo_fill(void *dev, const uint8_t *data, int len)
{
    (void)dev; (void)data;
    g_rom_fill_calls++;
    g_clock += g_fill_cost;
    return len;
}
extern uint8_t stub_lib_usb_otg_tx_one_char(uint8_t c);
extern void stub_lib_usb_otg_tx_flush(void);
extern void idlab_otg_trace_snapshot(uint64_t *cycles, uint32_t *calls);

/* ---- scripted transport -------------------------------------------------------------------- */
enum ack_mode { ACK_NORMAL, ACK_BAD_SIZE };
static enum ack_mode g_ack_mode = ACK_NORMAL;
static uint32_t g_pending_ack;     /* cumulative byte count to deliver, 0 if none */
static bool g_ack_ready;
static uint32_t g_ack_value;
static uint32_t g_acked_total;
static uint32_t g_frames_sent, g_frame_bytes_total;
static uint32_t g_polls_without_ack;
static uint8_t g_last_response[128];
static size_t g_last_response_len;
static uint32_t g_ack_wait_polls = 3;

static bool fake_send_frame(const void *data, size_t len)
{
    g_frames_sent++;
    g_frame_bytes_total += (uint32_t)len;
    /* Model of the USB OTG transport: every byte goes through tx_one_char (64-byte flushes) and the
     * frame ends with an explicit flush. */
    for (size_t i = 0; i < len; i++) stub_lib_usb_otg_tx_one_char(((const uint8_t *)data)[i]);
    stub_lib_usb_otg_tx_flush();
    if (len <= sizeof(g_last_response)) { memcpy(g_last_response, data, len); g_last_response_len = len; }
    if (len > 74) {
        /* A data frame (a command response is at most 8 + 64 + 2 = 74 bytes): the host acknowledges the
         * cumulative length after a wait. The 16-byte MD5 frame and the responses are not acknowledged. */
        g_acked_total += (uint32_t)len;
        g_pending_ack = g_acked_total;
        g_polls_without_ack = 0;
    }
    return true;
}
static uint8_t g_ack_bytes[8];
static const uint8_t *fake_recv_poll(size_t *len, bool *error)
{
    *error = false;
    if (g_pending_ack == 0) { *len = 0; return NULL; }
    if (g_polls_without_ack++ < g_ack_wait_polls) { *len = 0; return NULL; }
    g_clock += g_ack_cost; /* the whole wait is charged once, when the ACK shows up */
    if (g_ack_mode == ACK_BAD_SIZE) {
        g_pending_ack = 0;
        *len = 3;
        return g_ack_bytes;
    }
    g_ack_value = g_pending_ack;
    memcpy(g_ack_bytes, &g_ack_value, 4);
    g_pending_ack = 0;
    *len = 4;
    return g_ack_bytes;
}
static void fake_recv_release(void) {}
static const struct stub_transport_ops g_ops = {fake_recv_poll, fake_recv_release, fake_send_frame};

/* ---- helpers ------------------------------------------------------------------------------- */
static uint8_t g_cmd[64];
static size_t build_command(uint8_t opcode, const uint8_t *payload, uint16_t payload_len)
{
    g_cmd[0] = 0; g_cmd[1] = opcode;
    g_cmd[2] = (uint8_t)(payload_len & 0xFF); g_cmd[3] = (uint8_t)(payload_len >> 8);
    memset(&g_cmd[4], 0, 4);
    if (payload_len) memcpy(&g_cmd[8], payload, payload_len);
    return (size_t)payload_len + 8;
}
static void le32(uint8_t *p, uint32_t v) { memcpy(p, &v, 4); }

static void reset_world(void)
{
    g_flash_calls = 0; g_fail_flash_at = 0; g_md5_updates = 0; g_rom_fill_calls = 0;
    g_pending_ack = 0; g_acked_total = 0; g_frames_sent = 0; g_frame_bytes_total = 0;
    g_ack_mode = ACK_NORMAL; g_read_step = 1; g_ack_wait_polls = 3;
}

static void run_read(uint8_t opcode, uint32_t addr, uint32_t size)
{
    uint8_t payload[16];
    le32(payload, addr); le32(payload + 4, size); le32(payload + 8, 4096); le32(payload + 12, 1);
    size_t n = build_command(opcode, payload, 16);
    handle_command(g_cmd, n, &g_ops);
}

struct report { uint32_t magic, version; uint64_t flash, md5, send, ack, fill; uint32_t fill_calls, packets, ack_count, bytes; };
static int get_report(struct report *out, uint16_t status_hint_len, int *status)
{
    size_t n = build_command(0xE0, NULL, status_hint_len);
    g_last_response_len = 0;
    handle_command(g_cmd, n, &g_ops);
    /* response = 8-byte header + data + 2 status bytes (status[0] != 0 means error) */
    if (g_last_response_len < 10) { *status = -1; return -1; }
    uint16_t data_len;
    memcpy(&data_len, &g_last_response[2], 2);
    const uint8_t *data = &g_last_response[8];
    *status = data[data_len - 2] ? (int)(data[data_len - 2]) : 0;
    if (data_len - 2 == 64) {
        memcpy(&out->magic, data, 4); memcpy(&out->version, data + 4, 4);
        memcpy(&out->flash, data + 8, 8); memcpy(&out->md5, data + 16, 8);
        memcpy(&out->send, data + 24, 8); memcpy(&out->ack, data + 32, 8); memcpy(&out->fill, data + 40, 8);
        memcpy(&out->fill_calls, data + 48, 4); memcpy(&out->packets, data + 52, 4);
        memcpy(&out->ack_count, data + 56, 4); memcpy(&out->bytes, data + 60, 4);
        return 0;
    }
    return 1; /* error response without payload */
}

static void print_report(const char *label, const struct report *r, int status)
{
    printf("RESULT %s status=%d magic=0x%08x version=%u flash=%llu md5=%llu send=%llu ack=%llu fill=%llu "
           "fill_calls=%u packets=%u ack_count=%u bytes=%u\n",
           label, status, r->magic, r->version, (unsigned long long)r->flash, (unsigned long long)r->md5,
           (unsigned long long)r->send, (unsigned long long)r->ack, (unsigned long long)r->fill,
           r->fill_calls, r->packets, r->ack_count, r->bytes);
}

int main(void)
{
    struct report r; int status;

    /* S1: nominal traced read, 3 full packets + a 100-byte tail, unaligned start offset */
    reset_world(); g_clock = 5000;
    run_read(0xDF, 0x8001, 3 * 4096 + 100);
    memset(&r, 0, sizeof r);
    get_report(&r, 0, &status);
    print_report("s1_nominal", &r, status);
    printf("RESULT s1_world frames=%u frame_bytes=%u flash_calls=%u md5_updates=%u rom_fill_calls=%u acked=%u\n",
           g_frames_sent, g_frame_bytes_total, g_flash_calls, g_md5_updates, g_rom_fill_calls, g_acked_total);

    /* S2: the standard READ_FLASH (0xD2) through the instrumented image leaves the counters alone */
    struct report before = r, after; memset(&after, 0, sizeof after);
    uint32_t fills_before = g_rom_fill_calls;
    reset_world(); g_rom_fill_calls = fills_before;
    run_read(0xD2, 0x8000, 2 * 4096);
    get_report(&after, 0, &status);
    print_report("s2_standard_read_counters", &after, status);
    printf("RESULT s2_unchanged %d\n", memcmp(&before, &after, sizeof before) == 0);

    /* S3: flash error on the 2nd packet: tracing stops, counters are partial, the error is carried */
    reset_world(); g_clock = 100; g_fail_flash_at = 2;
    run_read(0xDF, 0x8000, 4 * 4096);
    uint64_t fc = 0; uint32_t fcalls = 0, fills_after_error;
    memset(&r, 0, sizeof r);
    int rc1 = get_report(&r, 0, &status);          /* the carried error answers this one */
    print_report("s3_after_error_first_get", &r, status);
    printf("RESULT s3_first_get rc=%d status=%d\n", rc1, status);
    memset(&r, 0, sizeof r);
    get_report(&r, 0, &status);                    /* the next one returns the partial counters */
    print_report("s3_partial_counters", &r, status);
    idlab_otg_trace_snapshot(&fc, &fcalls);
    stub_lib_usb_otg_tx_one_char(0); stub_lib_usb_otg_tx_flush();   /* a fill AFTER the error */
    uint64_t fc2 = 0; uint32_t fcalls2 = 0; idlab_otg_trace_snapshot(&fc2, &fcalls2);
    fills_after_error = fcalls2 - fcalls;
    printf("RESULT s3_fill_counted_after_error %u\n", fills_after_error);

    /* S4: malformed ACK (3 bytes): the loop leaves, the MD5 frame is sent, counters expose the mismatch */
    reset_world(); g_clock = 7; g_ack_mode = ACK_BAD_SIZE;
    run_read(0xDF, 0x8000, 3 * 4096);
    memset(&r, 0, sizeof r);
    get_report(&r, 0, &status);
    print_report("s4_bad_ack", &r, status);
    printf("RESULT s4_frames frames=%u\n", g_frames_sent);

    /* S5: GET with a payload is refused (BAD_DATA_LEN) */
    reset_world();
    {
        uint8_t junk[4] = {1, 2, 3, 4};
        size_t n = build_command(0xE0, junk, 4);
        g_last_response_len = 0;
        handle_command(g_cmd, n, &g_ops);
        uint16_t data_len; memcpy(&data_len, &g_last_response[2], 2);
        printf("RESULT s5_get_with_payload status_hi=0x%02x status_lo=0x%02x\n",
               g_last_response[8 + data_len - 2], g_last_response[8 + data_len - 1]);
    }

    /* S6: frozen counter: the probe fails closed */
    reset_world(); g_read_step = 0;
    memset(&r, 0, sizeof r);
    int rc6 = get_report(&r, 0, &status);
    printf("RESULT s6_frozen_counter rc=%d status=%d\n", rc6, status);
    g_read_step = 1;

    /* S7: 32-bit wrap of the counter during the run, deltas must stay exact */
    reset_world(); g_clock = 0xFFFFFF00U;
    run_read(0xDF, 0x8000, 2 * 4096);
    memset(&r, 0, sizeof r);
    get_report(&r, 0, &status);
    print_report("s7_wrap", &r, status);

    /* S8: 64-bit accumulation: 3 packets of ~3.0e9 flash cycles each exceed 2^32 in total */
    reset_world(); g_flash_cost = 3000000000U; g_clock = 0;
    run_read(0xDF, 0x8000, 3 * 4096);
    memset(&r, 0, sizeof r);
    get_report(&r, 0, &status);
    print_report("s8_u64", &r, status);
    return 0;
}
