// Helper (not a *.test.mjs): drives the REAL Agent + EspEngine + runP4UsbBringup against a SIMULATED P4 whose admission sees a
// stub, and prints the exact `result` message the agent sends to the broker. Consumed by test_p4_admission_persistence.py.
// stdin: {op_id, device, args, stub_b64}. No board, no Web Serial, no network.
import {Agent} from '../web/agent.js';
import {EspEngine} from '../web/esp-engine.js';

const input = JSON.parse(await new Promise(resolve => { let s = ''; process.stdin.on('data', c => s += c).on('end', () => resolve(s)); }));
const stubBytes = Uint8Array.from(Buffer.from(input.stub_b64, 'base64'));
const calls = [];
const loader = {
  IS_STUB: false, ESP_RAM_BLOCK: 0x1800, DEFAULT_TIMEOUT: 1000,
  chip: {CHIP_NAME: 'ESP32-P4', USB_RAM_BLOCK: 0x800, getChipRevision: async () => 302},
  usesUsbOtg: async () => true,
  sync: async () => { loader.syncStubDetected = true; calls.push('sync'); throw new Error('no complete exchange'); },
  command: async () => { calls.push('sync_frame'); return [0, new Uint8Array(0)]; },   // a stub answers once, with 0
  readPacket: async () => { throw new Error('no further answer'); },
  memBegin: async () => { calls.push('MEM_BEGIN'); }, memBlock: async () => { calls.push('MEM_DATA'); }, memFinish: async () => { calls.push('MEM_END'); },
  flashBegin: async () => { calls.push('FLASH'); }, flashBlock: async () => { calls.push('FLASH'); }, flashFinish: async () => { calls.push('FLASH'); },
  transport: {flushInput() {}, async read() { calls.push('READ'); return new Uint8Array(0); }, async write() { calls.push('WRITE'); }},
};
const engine = new EspEngine({getInfo: () => ({})}, {}, () => {});
engine.romOnly = true; engine.native = true; engine.usbSerialJtag = false; engine.chipDescription = 'ESP32-P4';
engine.openLoader = async () => loader;
const agent = new Agent({engine, device: input.device, token: 'secret'});
const sent = [];
agent.socket = {readyState: 1, bufferedAmount: 0, send: s => sent.push(JSON.parse(s))};
agent.generation = 'gen'; agent.serverOffset = 1000; agent.grantUntil = performance.now() + 10000;
agent.artifact = async () => stubBytes;
const now = performance.now();
await agent.message({type: 'offer', id: input.op_id, generation: 'gen', command: 'diagnostic.p4_usb_bringup', args: input.args, lease: 'lease',
  deadline_ms: now + 11000, remaining_ms: 10000, lease_deadline_ms: now + 21000});
await agent.message({type: 'execute', id: input.op_id, execution_deadline_ms: now + 121000, deadline_ms: now + 11000, remaining_ms: 10000});
const result = sent.find(m => m.type === 'result');
console.log(JSON.stringify({message: result, calls}));
