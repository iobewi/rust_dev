import test from 'node:test';
import assert from 'node:assert/strict';
import {decodeTraceReport, readFlashWithP4StubTrace,
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
