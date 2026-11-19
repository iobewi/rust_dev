import test from 'node:test';
import assert from 'node:assert/strict';
import { flashMd5Diagnostic, MD5_DIAGNOSTIC_TIMEOUT_MS } from '../web/flash-md5-diagnostic.js';
import { EspEngine } from '../web/esp-engine.js';

const MiB = 1024 * 1024;
test('D sends only SPI_FLASH_MD5 with the exact region and a bounded extended timeout',async()=>{
  const calls=[], md5=Uint8Array.from({length:16},(_,i)=>i);
  const loader={IS_STUB:true,ESP_SPI_FLASH_MD5:0x13,
    async checkCommand(...args){calls.push(args);return md5;}};
  assert.equal(await flashMd5Diagnostic(loader,0x8000,MiB),'000102030405060708090a0b0c0d0e0f');
  assert.equal(calls.length,1);
  const [label,op,payload,checksum,size,timeout]=calls[0];
  assert.equal(label,'calculate md5sum');assert.equal(op,0x13);
  assert.equal(size,16);assert.equal(timeout,MD5_DIAGNOSTIC_TIMEOUT_MS);
  assert.equal(checksum,undefined);assert.equal(payload.byteLength,16);
  const view=new DataView(payload.buffer);
  assert.deepEqual([0,4,8,12].map(at=>view.getUint32(at,true)),[0x8000,MiB,0,0]);
});
test('D rejects invalid regions and refuses ROM mode before any command',async()=>{
  let calls=0;
  const loader={IS_STUB:true,ESP_SPI_FLASH_MD5:0x13,
    async checkCommand(){calls++;return new Uint8Array(16);}};
  for(const [address,size] of [[0,0],[0,MiB+1],[-1,4096],[0x100000000,4096]]) {
    await assert.rejects(flashMd5Diagnostic(loader,address,size),RangeError);
  }
  await assert.rejects(flashMd5Diagnostic({...loader,IS_STUB:false},0,4096),/requires_stub/);
  assert.equal(calls,0);
});
test('D rejects malformed MD5 replies instead of publishing misleading measurements',async()=>{
  for(const reply of [null,0,Uint8Array.of(1),new Uint8Array(32)]) {
    const loader={IS_STUB:true,ESP_SPI_FLASH_MD5:0x13,checkCommand:async()=>reply};
    await assert.rejects(flashMd5Diagnostic(loader,0,4096),/invalid_flash_md5_response/);
  }
});
test('engine D publishes timing and digest, but no flash artifact or bulk reader',async()=>{
  const engine=new EspEngine({getInfo:()=>({})},{},()=>{});
  const calls=[];
  const loader={IS_STUB:true,ESP_SPI_FLASH_MD5:0x13,checkCommand:async(...args)=>{
    calls.push(args);return new Uint8Array(16).fill(0x5a);
  }};
  engine.identity=async()=>({chip:'esp32p4',flash_size:32*MiB});
  engine.loader=loader;
  let uploaded=0;
  const context={check(){},checkLive(){},live:()=>true,
    device:{chip:'esp32p4',flash_size:32*MiB},report(){},
    upload:async()=>{uploaded++;throw new Error('D must not upload');}};
  const result=await engine.execute('flash.read',
    {reader:'md5-only',address:0x8000,size:MiB,return_console:false},context);
  assert.equal(result.md5,'5a'.repeat(16));
  assert.equal(result.transfer.reader,'md5-only');
  assert.equal(result.transfer.transmitted_flash_bytes,0);
  assert.ok(result.transfer.elapsed_ms>=0);
  assert.equal(result.artifact,undefined);
  assert.equal(uploaded,0);
  assert.equal(calls.length,1);
});
