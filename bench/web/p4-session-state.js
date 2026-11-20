// Reliable ROM / stub detection for the P4 diagnostic admission. No single index is trusted:
//  - IS_STUB            flag kept by the page for the loader it drives: only a `true` is a vote (stub); `false` abstains;
//  - loader.sync()      esptool-js 0.7.0: sends ESP_SYNC (opcode 0x08), reads 7 more answers (a ROM answers 8 times, a stub once)
//                       and sets `syncStubDetected` (all `val` === 0). It THROWS on a stub (second answer never comes): an
//                       incomplete sync is therefore compatible with a stub only, never with a ROM;
//  - raw exchange       our own ESP_SYNC exchange, mirroring sync() but tolerant of missing answers: every `val` actually
//                       received is kept as is (signed int32, as esptool-js decodes it); an invalid value aborts, a missing
//                       answer is NOT turned into 0. 8 non-zero answers = ROM, one answer equal to 0 = stub (esptool-js'
//                       own heuristic, hence not absolute), anything else = mixed;
//  - md5 answer shape   OPTIONAL independent evidence (never wired into the agent): ROM answers ESP_SPI_FLASH_MD5 with
//                       32 ASCII bytes, a stub with 16 raw bytes. An 'unknown' shape forbids a positive ROM verdict.
// Anything but a full agreement of the available indices is refused. Observing TRANSMITS ESP_SYNC frames to the chip (no
// memory/flash write, no reset); it never retries.
export const SYNC_OPCODE = 0x08;        // esptool-js 0.7.0 esploader.js sync(): command(0x08, …) / readPacket(0x08, …) (there is no loader.ESP_SYNC)
export const SYNC_ANSWERS_FROM_ROM = 8;
export const SYNC_TIMEOUT_MS = 100;
export const SYNC_FRAME = Uint8Array.from([0x07, 0x07, 0x12, 0x20, ...new Array(32).fill(0x55)]);

export class InvalidSyncValue extends Error { constructor() { super('invalid_sync_value'); this.name = 'InvalidSyncValue'; } }
const validVal = v => typeof v === 'number' && Number.isInteger(v) && v >= -0x80000000 && v <= 0x7fffffff;

// 'rom' | 'stub' | 'unknown' from the length/shape of the ESP_SPI_FLASH_MD5 answer. Pure: no I/O.
export function classifyMd5Answer(answer) {
  if (!(answer instanceof Uint8Array)) return 'unknown';
  if (answer.length === 32 && answer.every(b => (b >= 0x30 && b <= 0x39) || (b >= 0x61 && b <= 0x66))) return 'rom';
  if (answer.length === 16) return 'stub';
  return 'unknown';
}

// Pure decision. `evidence`: { isStub, syncCompleted, syncStubDetected, syncValues: int32[], md5?: 'rom'|'stub'|'unknown' }
// Returns { verdict: 'rom' | 'stub' | 'indeterminate', votes, zero_count }.
export function judgeSessionState(evidence) {
  const values = Array.isArray(evidence.syncValues) && evidence.syncValues.every(validVal) ? evidence.syncValues : [];
  const zeros = values.filter(v => v === 0).length;
  const votes = {};
  if (evidence.isStub === true) votes.is_stub = 'stub';
  // an incomplete sync() is what a stub does (it answers once); a ROM completes it
  votes.sync_stub_detected = evidence.syncCompleted === true ? (evidence.syncStubDetected === true ? 'stub' : 'rom') : 'stub';
  if (values.length === 0) votes.sync_values = 'missing';
  else if (zeros === values.length && (values.length === 1 || values.length === SYNC_ANSWERS_FROM_ROM)) votes.sync_values = 'stub';
  else if (zeros === 0 && values.length === SYNC_ANSWERS_FROM_ROM) votes.sync_values = 'rom';
  else votes.sync_values = 'mixed';
  if (evidence.md5 === 'rom' || evidence.md5 === 'stub') votes.md5 = evidence.md5;
  const seen = new Set(Object.values(votes));
  let verdict;
  if (seen.has('missing') || seen.has('mixed') || (seen.has('rom') && seen.has('stub'))) verdict = 'indeterminate';
  else verdict = seen.has('stub') ? 'stub' : 'rom';
  if (verdict === 'rom' && evidence.md5 === 'unknown') verdict = 'indeterminate';   // a probe that cannot tell forbids a positive ROM verdict
  return { verdict, votes, zero_count: zeros };
}

// Observes the CURRENT session on the real esptool-js 0.7.0 API (see tests/esptool-real-sync.test.mjs).
// Throws on a communication failure of the first answer, on an invalid value, or when the md5 probe fails.
export async function observeSessionState(loader, { md5Probe } = {}) {
  let syncCompleted = false;
  try { await loader.sync(); syncCompleted = true; } catch { /* expected on a stub; judged together with the raw exchange */ }
  const syncValues = [];
  const first = await loader.command(SYNC_OPCODE, SYNC_FRAME, 0, true, SYNC_TIMEOUT_MS);   // an error here: unobservable, the caller refuses
  if (!Array.isArray(first) || !validVal(first[0])) throw new InvalidSyncValue();
  syncValues.push(first[0]);
  for (let i = 1; i < SYNC_ANSWERS_FROM_ROM; i++) {
    let answer;
    try { answer = await loader.readPacket(SYNC_OPCODE, SYNC_TIMEOUT_MS); }
    catch { break; }                    // no further answer: what a stub does; the count is part of the evidence
    if (!Array.isArray(answer) || !validVal(answer[0])) throw new InvalidSyncValue();
    syncValues.push(answer[0]);
  }
  loader.transport?.flushInput?.();     // nothing stale may be read as the answer of the next command
  const md5 = md5Probe ? classifyMd5Answer(await md5Probe(loader)) : undefined;
  return { isStub: loader.IS_STUB === true, syncCompleted, syncStubDetected: loader.syncStubDetected === true, syncValues, md5 };
}

// Allowlisted raw observations published with a refusal (see hardware-errors.js); shared by the admission and the recovery proof.
export function sessionStateReport(evidence, judged) {
  return { sync_values: evidence.syncValues, sync_count: evidence.syncValues.length, sync_zero_count: judged.zero_count,
    sync_completed: evidence.syncCompleted ? 1 : 0, sync_stub_detected: evidence.syncStubDetected ? 1 : 0, loader_stub: evidence.isStub ? 1 : 0,
    ...(evidence.md5 ? { md5_shape: evidence.md5 } : {}) };
}
