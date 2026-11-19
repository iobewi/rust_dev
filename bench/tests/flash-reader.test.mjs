import test from 'node:test';
import assert from 'node:assert/strict';
import { readFlashPreallocated } from '../web/flash-read-preallocated.js';
import { EspEngine } from '../web/esp-engine.js';

const MiB = 1024 * 1024;
function fakeLoader(data, { failAt = null } = {}) {
  const calls = [], acks = [], received = [];
  let index = 0;
  const loader = {
    ESP_READ_FLASH: 0xd2, FLASH_READ_TIMEOUT: 100000,
    async checkCommand(label, op, payload) {
      calls.push({ label, op, payload: Uint8Array.from(payload) });
      return 0;
    },
    transport: {
      async read(timeout) {
        assert.equal(timeout,100000);
        if (index === failAt) throw new Error('simulated-USB-loss');
        const packet = data.subarray(index, index + 4096);
        index += packet.length;
        received.push(packet.length);
        return packet;
      },
      async write(bytes) { acks.push(Uint8Array.from(bytes)); },
    },
  };
  return { loader, calls, acks, received };
}

function parameters(bytes) {
  const v = new DataView(bytes.buffer, bytes.byteOffset, bytes.byteLength);
  return [v.getUint32(0,true),v.getUint32(4,true),v.getUint32(8,true),v.getUint32(12,true)];
}

test('experimental reader uses esptool-js wire parameters and cumulative ACKs with single allocation',async()=>{
  const original = Uint8Array.from({length:8192},(_,i)=>i&255);
  const {loader,calls,acks,received}=fakeLoader(original);
  const progress=[];
  const result=await readFlashPreallocated(loader,0x10000,original.length,(_packet,n,total)=>progress.push([n,total]));
  assert.deepEqual(result,original);
  assert.deepEqual(calls.map(c=>[c.label,c.op,parameters(c.payload)]),
    [['read flash',0xd2,[0x10000,8192,4096,1024]]]);
  assert.deepEqual(received,[4096,4096]);
  assert.deepEqual(acks.map(parametersAck),[4096,8192]);
  assert.deepEqual(progress,[[4096,8192],[8192,8192]]);
});
function parametersAck(bytes) { return new DataView(bytes.buffer,bytes.byteOffset,bytes.byteLength).getUint32(0,true); }

test('experimental reader rejects invalid or incomplete frames rather than publishing padded data',async()=>{
  for (const packet of [new Uint8Array(0),new Uint8Array(4097),new Uint8Array(100)]) {
    let written=0;
    const loader={ESP_READ_FLASH:0xd2,FLASH_READ_TIMEOUT:100000,checkCommand:async()=>0,
      transport:{read:async()=>packet,write:async()=>{written++;}}};
    // Small valid packet alone is insufficient: the second read must fail.
    if(packet.length===100){let count=0;loader.transport.read=async()=>++count===1?packet:new Uint8Array(0);}
    await assert.rejects(readFlashPreallocated(loader,0,4096));
    assert.ok(written<=1);
  }
  await assert.rejects(readFlashPreallocated(fakeLoader(new Uint8Array(2)).loader,0,MiB+1),RangeError);
});

test('engine selects A by default or B explicitly without changing device control or published bytes',async()=>{
  const original=Uint8Array.from({length:8192},(_,i)=>(i*31)&255);
  async function run(reader) {
    const {loader,calls,acks}=fakeLoader(original);
    loader.readFlash=async (_address,size,progress)=>{
      const bytes=original.slice(0,size);
      progress?.(bytes,size,size);
      return bytes;
    };
    const engine=new EspEngine({getInfo:()=>({})},{},()=>{});
    engine.identity=async()=>({chip:'esp32s3',flash_size:16*MiB});
    engine.loader=loader;
    let uploads=0;
    const context={check(){},checkLive(){},live:()=>true,device:{chip:'esp32s3',flash_size:16*MiB},
      report(){},upload:async data=>{uploads++;assert.deepEqual(data,original);return {size:data.length};}};
    const args={address:0x10000,size:original.length,return_console:false};
    if(reader)args.reader=reader;
    const result=await engine.execute('flash.read',args,context);
    return {result,uploads,calls,acks};
  }
  const a=await run(null),b=await run('preallocated');
  assert.equal(a.result.transfer.reader,'reference');
  assert.equal(b.result.transfer.reader,'preallocated');
  assert.equal(a.uploads,1);assert.equal(b.uploads,1);
  assert.equal(b.calls.length,1);assert.deepEqual(b.acks.map(parametersAck),[4096,8192]);
});
