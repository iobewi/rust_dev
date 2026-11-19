// Reliable ROM / stub detection for the P4 diagnostic admission. No single index is trusted:
//  - IS_STUB            flag kept by the page for the loader it drives: only a `true` is a vote (stub); `false` abstains;
//  - syncStubDetected   esptool-js heuristic, true only when ALL sync answers carry val === 0 (not an absolute proof);
//  - raw sync values    the eight `val` fields of a FRESH ESP_SYNC exchange, taken now, on the current session;
//  - md5 answer shape   OPTIONAL independent evidence: ROM answers ESP_SPI_FLASH_MD5 with 32 ASCII bytes, a stub with 16 raw bytes.
// Anything but a full agreement of all indices available is refused: stub => 'stub_reported_running',
// disagreement => 'state_indeterminate', no observation => 'sync_failed'. Observing sends sync frames only (no write, no reset).
export const SYNC_SAMPLES = 8;
export const SYNC_FRAME = Uint8Array.from([0x07, 0x07, 0x12, 0x20, ...new Array(32).fill(0x55)]);

// 'rom' | 'stub' | 'unknown' from the length/shape of the ESP_SPI_FLASH_MD5 answer. Pure: no I/O.
export function classifyMd5Answer(answer) {
  if (!(answer instanceof Uint8Array)) return 'unknown';
  if (answer.length === 32 && answer.every(b => (b >= 0x30 && b <= 0x39) || (b >= 0x61 && b <= 0x66))) return 'rom';
  if (answer.length === 16) return 'stub';
  return 'unknown';
}

// Pure decision. `evidence`: { isStub: bool, syncStubDetected: bool|undefined, syncValues: number[], md5?: 'rom'|'stub'|'unknown' }
// Returns { verdict: 'rom' | 'stub' | 'indeterminate', votes, zero_count }.
export function judgeSessionState(evidence) {
  const values = Array.isArray(evidence.syncValues) ? evidence.syncValues : [];
  const zeros = values.filter(v => v === 0).length;
  // IS_STUB is a page-local record: true is a claim (stub), false carries no information (a stub left by an earlier page stays invisible to it).
  const votes = {};
  if (evidence.isStub === true) votes.is_stub = 'stub';
  if (typeof evidence.syncStubDetected === 'boolean') votes.sync_stub_detected = evidence.syncStubDetected ? 'stub' : 'rom';
  if (values.length === SYNC_SAMPLES) votes.sync_values = zeros === SYNC_SAMPLES ? 'stub' : zeros === 0 ? 'rom' : 'mixed';
  else votes.sync_values = 'missing';
  if (evidence.md5 === 'rom' || evidence.md5 === 'stub') votes.md5 = evidence.md5;
  const seen = new Set(Object.values(votes));
  let verdict;
  if (seen.has('missing') || seen.has('mixed') || !('sync_stub_detected' in votes) || (seen.has('rom') && seen.has('stub'))) verdict = 'indeterminate';
  else verdict = seen.has('stub') ? 'stub' : 'rom';
  return { verdict, votes, zero_count: zeros };
}

// Observes the CURRENT session (fresh sync, then eight raw ESP_SYNC answers). Throws the caller's error on any I/O failure.
export async function observeSessionState(loader, { md5Probe } = {}) {
  await loader.sync();                    // refreshes syncStubDetected on this very session
  const syncValues = [];
  for (let i = 0; i < SYNC_SAMPLES; i++) {
    const [val] = await loader.command(loader.ESP_SYNC, SYNC_FRAME, 0, true, 100);
    syncValues.push(val >>> 0);
  }
  const md5 = md5Probe ? classifyMd5Answer(await md5Probe(loader)) : undefined;
  return { isStub: loader.IS_STUB === true, syncStubDetected: loader.syncStubDetected === true, syncValues, md5 };
}
