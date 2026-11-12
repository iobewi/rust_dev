// Registry of the EXPERIMENTAL flasher stubs the agent may upload instead of the official one (the stub bundled with esptool-js).
// Nothing here is used unless a command asks for a variant by name AND gives the SHA-256 pinned below. The official stub stays the default.
// Each entry is rebuilt from pinned sources by bench/experiments/<name>/build.sh; the published file is checked byte for byte at load time.
export const STUB_VARIANTS = Object.freeze({
  'p4-no-clock-boost': Object.freeze({
    chip: 'ESP32-P4',                 // esptool-js CHIP_NAME: the variant is refused on any other chip
    minRevision: 300,                 // the non-rev1 stub (v3.x silicon, ECO5 and later ROM symbols); refused on v1.x
    file: 'stub-esp32p4-noboost.json',
    sha256: '911c86173efd9e51a12f289b4b8f1adb9046d1326b426a7ecfd9a646f8366f24',        // SHA-256 of the file bytes
    text_sha256: 'cc2865bba8c3984eb2a4ca64b2a1447cdf0407389da3c1825d94565a00331928',   // SHA-256 of the "text" base64 string
    decoded_text_bytes: 8176, decoded_data_bytes: 204,
    uart_baud: 115200,                // first qualification: the ROM baud rate, no baud change
    allowed_commands: Object.freeze(['flash.md5', 'flash.read']),     // read only: the variant is never used to write or erase
    origin: Object.freeze({
      stub: 'esp-flasher-stub v1.2.2', stub_commit: '6f8dc174cd9742979bd6c4fb796ad60837800547', lib_commit: 'd61983fa4d1f66b9c088608c1f702ad877121279',
      patch: 'bench/experiments/p4-stub-no-clock-boost/p4-uart-no-clock-boost.patch',
      patch_sha256: '6916f39b1c9943622532e8bd9672b1dc9dea31e7c5d5ebedeff1a3e50ec2c14f',
      official_text_sha256: '722c15bbe563df347662c824f5262670fa34ac46e95aae6c3d50d0b2b799af8b',
    }),
  }),
});
