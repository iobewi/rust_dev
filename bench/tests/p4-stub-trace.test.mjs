import test from 'node:test';
import {md5hex} from '../web/md5.js';
const md5Of=(...parts)=>{const all=new Uint8Array(parts.reduce((n,x)=>n+x.length,0));let o=0;for(const x of parts){all.set(x,o);o+=x.length;}return Uint8Array.from(md5hex(all).match(/../g),h=>parseInt(h,16));};
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
  const d1=new Uint8Array(4096).fill(0x12),d2=new Uint8Array(4096).fill(0x34);
  const frames=[d1,d2,md5Of(d1,d2)];
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
  assert.equal(r.md5,md5hex(r.data));assert.equal(r.md5_verified,true);
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
  const z=new Uint8Array(4096);
  const frames=[z,z,md5Of(z,z)];
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

test('a corrupted data packet with valid framing is caught by the MD5 of the received bytes and ends the session indeterminate',async()=>{
  const good=new Uint8Array(4096).fill(7),bad=good.slice();bad[100]^=1;
  const frames=[bad,md5Of(good)];
  const loader={IS_STUB:true,__idlabTraceStub:true,checkCommand:async(_l,op)=>op===IDLAB_GET_FLASH_TRACE?report({bytes:4096,packets:1,ackCount:1}):0,
    transport:{read:async()=>frames.shift(),write:async()=>{}}};
  await assert.rejects(readFlashWithP4StubTrace(loader,0,4096),e=>e.code==='idlab_trace_md5_mismatch'&&e.sessionIndeterminate===true&&e.noRetry===true);
});

// ---------------------------------------------------------------- per-packet SHA-256 of the received bytes (localises a divergence, never exposes flash bytes)
import {packetHashes} from '../web/p4-stub-trace.js';
import {createHash} from 'node:crypto';
const nodeSha=b=>createHash('sha256').update(b).digest('hex');
// deterministic pseudo-flash: erased sectors (0xFF) and sectors with content
const flashPattern=(size)=>{const b=new Uint8Array(size).fill(0xff);for(let i=0;i<size;i++){const sec=Math.floor(i/4096);if(sec%5===1||sec%7===3)b[i]=(i*31+sec*17)&0xff;}return b;};
async function runTrace(flash,address,{packet=4096,mutate=null}={}){
  const frames=[];for(let o=0;o<flash.length;o+=packet)frames.push(flash.slice(o,Math.min(o+packet,flash.length)));
  const sent=frames.map(f=>f.slice());if(mutate)mutate(sent);
  sent.push(md5Of(...frames));
  const acks=[];
  const loader={IS_STUB:true,__idlabTraceStub:true,checkCommand:async(_l,op)=>op===IDLAB_GET_FLASH_TRACE?report({bytes:flash.length,packets:frames.length,ackCount:frames.length}):0,
    transport:{read:async()=>sent.shift(),write:async b=>{acks.push(new DataView(b.buffer).getUint32(0,true));}}};
  return {r:await readFlashWithP4StubTrace(loader,address,flash.length),acks};
}
test('1 MiB read: 256 packets, one SHA-256 each, absolute NOR addresses, exact hashes, global consistency',async()=>{
  const flash=flashPattern(1048576),{r,acks}=await runTrace(flash,0x8000);
  assert.equal(r.packet_layout.length,256);
  const hashes=await packetHashes(r.data,0x8000,r.packet_layout);
  assert.equal(hashes.length,256);
  for(const [i,h] of hashes.entries()){
    assert.deepEqual([h.packet,h.address,h.size,h.ack_index,h.ack_bytes,h.acked],[i,0x8000+i*4096,4096,i+1,(i+1)*4096,1],'packet '+i);
    assert.equal(h.sha256,nodeSha(flash.subarray(i*4096,(i+1)*4096)),'sha of packet '+i);
    assert.deepEqual(Object.keys(h).sort(),['ack_bytes','ack_index','acked','address','packet','sha256','size']);
  }
  assert.equal(hashes.reduce((n,h)=>n+h.size,0),flash.length);
  assert.deepEqual(acks,hashes.map(h=>h.ack_bytes),'ACK values are the cumulative sizes, one per packet');
  assert.equal(await (await import('../web/protocol.js')).sha256(r.data),nodeSha(flash),'global SHA-256 = SHA-256 of the same bytes');
  assert.equal(r.md5,md5hex(flash));
});
test('tracing does not modify, shift or duplicate the data: the received bytes are the sent bytes',async()=>{
  const flash=flashPattern(8*4096),{r}=await runTrace(flash,0x1000);
  assert.deepEqual(r.data,flash);
  const hashes=await packetHashes(r.data,0x1000,r.packet_layout);
  assert.equal(new Set(hashes.map(h=>h.address)).size,hashes.length,'no duplicated address');
  assert.deepEqual(hashes.map(h=>h.address),[...hashes.keys()].map(i=>0x1000+i*4096),'no gap, no shift');
});
test('partial packets keep their effective size and the sum of sizes equals the request',async()=>{
  const flash=flashPattern(10000),{r}=await runTrace(flash,0x8000);
  const hashes=await packetHashes(r.data,0x8000,r.packet_layout);
  assert.deepEqual(hashes.map(h=>h.size),[4096,4096,1808]);
  assert.deepEqual(hashes.map(h=>h.address),[0x8000,0x9000,0xa000]);
  assert.equal(hashes[2].sha256,nodeSha(flash.subarray(8192)));
  // smaller packets from the stub (not 4096): layout follows what was actually received
  const small=await runTrace(flashPattern(3000),0,{packet:1000});
  assert.deepEqual((await packetHashes(small.r.data,0,small.r.packet_layout)).map(h=>[h.address,h.size]),[[0,1000],[1000,1000],[2000,1000]]);
});
test('a packet corrupted in flight changes ONLY its own hash (the global MD5 still stops the run first)',async()=>{
  const flash=flashPattern(4*4096);
  await assert.rejects(runTrace(flash,0,{mutate:fr=>{fr[2][10]^=0xff;}}),e=>e.code==='idlab_trace_md5_mismatch');
  const a=await packetHashes(flash,0,[0,1,2,3].map(i=>({offset:i*4096,size:4096,ack_bytes:(i+1)*4096})));
  const bad=flash.slice();bad[2*4096+10]^=0xff;
  const b=await packetHashes(bad,0,[0,1,2,3].map(i=>({offset:i*4096,size:4096,ack_bytes:(i+1)*4096})));
  assert.deepEqual(a.map((h,i)=>h.sha256===b[i].sha256),[true,true,false,true]);
});
test('invalid layouts or inputs are refused, never hashed partially',async()=>{
  const data=new Uint8Array(8192);
  for(const layout of [[{offset:0,size:4096,ack_bytes:4096}],[{offset:0,size:4096,ack_bytes:4096},{offset:4097,size:4095,ack_bytes:8192}],
      [{offset:0,size:8192,ack_bytes:8191}],[{offset:0,size:0,ack_bytes:0},{offset:0,size:8192,ack_bytes:8192}],[{offset:0,size:9000,ack_bytes:9000}],[]]) 
    await assert.rejects(packetHashes(data,0,layout),/invalid_packet_layout/);
  await assert.rejects(packetHashes('x',0,[]),/invalid_packet_hash_input/); await assert.rejects(packetHashes(data,1.5,[]),/invalid_packet_hash_input/);
  await assert.rejects(packetHashes(data,0,null),/invalid_packet_hash_input/);
});
