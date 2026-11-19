import test from 'node:test';
import assert from 'node:assert/strict';
import {decodeTraceReport, readFlashWithP4StubTrace, convertTraceCycles, impliedCpuHz, IdlabTraceError,
  IDLAB_READ_FLASH_TRACE, IDLAB_GET_FLASH_TRACE} from '../web/p4-stub-trace.js';

function report({bytes=8192, packets=2, ackCount=2}={}) {
  const b = new Uint8Array(64), v = new DataView(b.buffer);
  v.setUint32(0,0x31544449,true);v.setUint32(4,1,true);
  for(let i=0;i<5;i++) v.setBigUint64(8+i*8,BigInt(1000+i*300),true);
  for(const [i,n] of [3,packets,ackCount,bytes].entries()) v.setUint32(48+i*4,n,true);
  return b;
}
test('report layout is 64 bytes, little endian, with lossless 64-bit cycle strings',()=>{
  const r=decodeTraceReport(report());
  assert.deepEqual(r,{version:1,flash_cycles:'1000',md5_cycles:'1300',
    send_cycles:'1600',ack_cycles:'1900',fill_cycles:'2200',
    fill_calls:3,packets:2,ack_count:2,bytes:8192});
});
test('wrong report signature or size fails closed',()=>{
  for(const value of [null,new Uint8Array(0),new Uint8Array(64),new Uint8Array(60)]) {
    assert.throws(()=>decodeTraceReport(value));
  }
});
test('diagnostic must identify the custom RAM stub before any commands',async()=>{
  let calls=0;
  const loader={IS_STUB:true,checkCommand:async()=>{calls++;return 0;}};
  await assert.rejects(readFlashWithP4StubTrace(loader,0,8192),/not_verified/);
  assert.equal(calls,0);
});
test('trace reads two frames, ACKs cumulative lengths, drains MD5, then retrieves counters',async()=>{
  const commands=[],events=[],acks=[];
  const frames=[new Uint8Array(4096).fill(0x12),
    new Uint8Array(4096).fill(0x34),new Uint8Array(16).fill(0x56)];
  const loader={IS_STUB:true,__idlabTraceStub:true,
    async checkCommand(_label,op,params,_chk,expected) {
      commands.push({op,params,expected});
      events.push('command'+op);
      return op===IDLAB_GET_FLASH_TRACE?report():0;
    },
    transport:{async read(){events.push('read');return frames.shift();},
      async write(b){acks.push(new DataView(b.buffer).getUint32(0,true));events.push('ack');}}
  };
  const r=await readFlashWithP4StubTrace(loader,0x8000,8192);
  assert.equal(r.data.length,8192);
  assert.equal(r.data[0],0x12);assert.equal(r.data[4096],0x34);
  assert.equal(r.md5,'56'.repeat(16));
  assert.deepEqual(acks,[4096,8192]);
  assert.deepEqual(commands.map(x=>x.op),[IDLAB_GET_FLASH_TRACE,IDLAB_READ_FLASH_TRACE,IDLAB_GET_FLASH_TRACE]);
  assert.equal(commands[0].expected,64);
  assert.equal(commands[2].expected,64);
  assert.equal(events.at(-2),'read');
  assert.equal(events.at(-1),'command224');
  const view=new DataView(commands[1].params.buffer);
  assert.deepEqual([0,4,8,12].map(i=>view.getUint32(i,true)),[0x8000,8192,4096,1]);
});
test('trace rejects mismatched counters and aborts publication',async()=>{
  const frames=[new Uint8Array(4096),new Uint8Array(4096),new Uint8Array(16)];
  const loader={IS_STUB:true,__idlabTraceStub:true,checkCommand:async(_l,op)=>
    op===IDLAB_GET_FLASH_TRACE?report({ackCount:1}):0,
    transport:{read:async()=>frames.shift(),write:async()=>{}}};
  await assert.rejects(readFlashWithP4StubTrace(loader,0,8192),/counter_mismatch/);
});

// ---- cycles versus microseconds, and the subset relation between fill and send
test('cycles are converted to microseconds only with an explicit CPU clock, and fill is never added to send',()=>{
  const r=decodeTraceReport(report());   // send 1600, fill 2200 in this fixture: invalid, refused
  assert.throws(()=>convertTraceCycles(r,200000000),/fill_cycles_exceeds_send_cycles/);
  const ok={...r,send_cycles:'4000000',fill_cycles:'3000000',flash_cycles:'400000',md5_cycles:'200000',ack_cycles:'600000'};
  const c=convertTraceCycles(ok,200000000);
  assert.deepEqual(c.cycles,{flash_cycles:'400000',md5_cycles:'200000',send_cycles:'4000000',ack_cycles:'600000',fill_cycles:'3000000'});
  assert.deepEqual(c.microseconds,{flash:2000,md5:1000,send_total:20000,fill_within_send:15000,ack_wait:3000,send_without_fill:5000});
  assert.equal(c.microseconds.send_without_fill,c.microseconds.send_total-c.microseconds.fill_within_send);
  for(const bad of [undefined,0,-1,1.5,'200000000',NaN]) assert.throws(()=>convertTraceCycles(ok,bad),/cpu_hz_required/);
});
test('the implied CPU clock cross-checks the counters against the wall time',()=>{
  const r={flash_cycles:'400000000',md5_cycles:'35000000',send_cycles:'3600000000',ack_cycles:'100000000'};
  assert.equal(impliedCpuHz(r,20675),200000000);
  assert.throws(()=>impliedCpuHz(r,0),/wall_time_required/);
});

// ---- error paths: what is refused before the stream, what leaves the session undetermined
function traceLoader({probe=()=>report(),frames=[],ackFails=false,commandResult=0}={}) {
  const log={reads:0,writes:0,commands:[]};
  return {log,loader:{IS_STUB:true,__idlabTraceStub:true,
    async checkCommand(_l,op) {log.commands.push(op); if(op===IDLAB_GET_FLASH_TRACE) return probe(); return commandResult;},
    transport:{async read(){log.reads++;const f=frames.shift();if(f instanceof Error) throw f;return f;},
      async write(){log.writes++;if(ackFails) throw new Error('usb gone');}}}};
}
test('a stock stub rejecting the probe stops before any bulk command and is not an undetermined session',async()=>{
  const {loader,log}=traceLoader({probe:()=>{throw new Error('invalid command');}});
  await assert.rejects(readFlashWithP4StubTrace(loader,0,8192),e=>e instanceof IdlabTraceError&&e.code==='idlab_trace_probe_failed'&&e.sessionIndeterminate===false);
  assert.deepEqual(log.commands,[IDLAB_GET_FLASH_TRACE]);
  assert.equal(log.reads,0);
});
test('an invalid range is refused before anything is sent',async()=>{
  const {loader,log}=traceLoader();
  for(const [a,n] of [[-1,4096],[0,0],[0,1024*1024+1],[0xfffff000,0x2000],[1.5,4096]])
    await assert.rejects(readFlashWithP4StubTrace(loader,a,n),/invalid_idlab_trace_range/);
  assert.equal(log.commands.length,0);
});
test('a rejected trace command leaves the session undetermined and reads nothing',async()=>{
  const {loader,log}=traceLoader({commandResult:1});
  await assert.rejects(readFlashWithP4StubTrace(loader,0,4096),e=>e.code==='idlab_trace_command_failed'&&e.sessionIndeterminate===true&&e.noRetry===true);
  assert.equal(log.reads,0);
});
test('a read timeout in the middle of the stream is undetermined and is never retried',async()=>{
  const {loader,log}=traceLoader({frames:[new Uint8Array(4096),new Error('timeout')]});
  await assert.rejects(readFlashWithP4StubTrace(loader,0,8192),e=>e instanceof IdlabTraceError&&e.code==='idlab_trace_stream_failed'&&e.sessionIndeterminate===true&&e.noRetry===true);
  assert.equal(log.reads,2);              // exactly the two reads that happened, no retry
  assert.equal(log.writes,1);             // one ACK only
  assert.equal(log.commands.filter(c=>c===IDLAB_READ_FLASH_TRACE).length,1);
});
test('a lost ACK write is undetermined and stops the loop at once',async()=>{
  const {loader,log}=traceLoader({frames:[new Uint8Array(4096),new Uint8Array(4096),new Uint8Array(16)],ackFails:true});
  await assert.rejects(readFlashWithP4StubTrace(loader,0,8192),e=>e.sessionIndeterminate===true);
  assert.equal(log.reads,1);
});
test('wrong frames are rejected: oversized, empty, not bytes, overrunning the requested size, short MD5',async()=>{
  const cases={
    oversized:[new Uint8Array(4097)], empty:[new Uint8Array(0)], notBytes:['text'],
    overrun:[new Uint8Array(4096),new Uint8Array(4096),new Uint8Array(1)].slice(0,1).concat([new Uint8Array(4096+1)]),
  };
  for(const [name,frames] of Object.entries(cases)) {
    const {loader}=traceLoader({frames:[...frames]});
    await assert.rejects(readFlashWithP4StubTrace(loader,0,8192),e=>e.code==='idlab_trace_invalid_data_frame'&&e.sessionIndeterminate===true,name);
  }
  const {loader}=traceLoader({frames:[new Uint8Array(4096),new Uint8Array(4096),new Uint8Array(15)]});
  await assert.rejects(readFlashWithP4StubTrace(loader,0,8192),e=>e.code==='idlab_trace_missing_md5_frame');
});
test('a counters answer with a bad signature or size aborts publication',async()=>{
  let calls=0;
  const {loader}=traceLoader({frames:[new Uint8Array(4096),new Uint8Array(4096),new Uint8Array(16)],
    probe:()=>{calls++;return calls===1?report():new Uint8Array(64);}});
  await assert.rejects(readFlashWithP4StubTrace(loader,0,8192),e=>e.sessionIndeterminate===true);
});
test('the IDT1 decoder rejects every wrong version, magic and length',()=>{
  for(const mutate of [b=>b[0]^=1,b=>b[4]=2,b=>b[7]=1]) { const b=report(); mutate(b); assert.throws(()=>decodeTraceReport(b)); }
  assert.throws(()=>decodeTraceReport(new Uint8Array(65)));
  assert.throws(()=>decodeTraceReport([...report()]));
});
test('64-bit cycle counters survive the round trip beyond 2^53 as decimal strings',()=>{
  const b=report(), v=new DataView(b.buffer);
  v.setBigUint64(8,(1n<<63n)+5n,true);
  assert.equal(decodeTraceReport(b).flash_cycles,((1n<<63n)+5n).toString());
});
