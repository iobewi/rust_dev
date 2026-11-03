import test from 'node:test';
import assert from 'node:assert/strict';
import { validateImages, manifestPlan, partitions, b64, unb64, sha256 } from '../web/protocol.js';

test('flash plans refuse out-of-range and overlapping erase sectors',()=>{
  const data=new Uint8Array(64);
  validateImages([{address:4096,data},{address:8192,data}],16384);
  for(const images of [[{address:-1,data}],[{address:16370,data}],[{address:4096,data},{address:4160,data}]])assert.throws(()=>validateImages(images,16384));
});
test('manifest resolves exact chip and local uploaded bytes, never infers erase',()=>{
  const files=new Map([['app.bin',{id:'a',sha256:'digest'}]]);
  const manifest={new_install_prompt_erase:true,builds:[{chipFamily:'ESP32-S3',parts:[{path:'app.bin',offset:65536}]}]};
  const plan=manifestPlan(manifest,'esp32s3',files);assert.equal(plan.erase,false);assert.equal(plan.images[0].artifact,'a');
  for(const path of ['../app.bin','https://evil/app.bin','/app.bin','missing.bin'])assert.throws(()=>manifestPlan({builds:[{chipFamily:'ESP32-S3',parts:[{path,offset:0}]}]},'esp32s3',files));
  assert.throws(()=>manifestPlan(manifest,'esp32c3',files));
});
test('binary base64 and SHA-256 preserve all octet values',async()=>{
  const bytes=Uint8Array.from({length:256},(_,i)=>i);assert.deepEqual(unb64(b64(bytes)),bytes);
  assert.equal(await sha256(bytes),'40aff2e9d2d8922e47afd4648e6967497158785fbd1da870e7110266bf944880');
});
test('partition decoder validates bounds and preserves numeric types',()=>{
  const bytes=new Uint8Array(64).fill(255),v=new DataView(bytes.buffer);
  v.setUint16(0,0x50aa,true);bytes[2]=0;bytes[3]=16;v.setUint32(4,65536,true);v.setUint32(8,131072,true);
  bytes.set(new TextEncoder().encode('app\0'),12);v.setUint32(28,1,true);
  assert.deepEqual(partitions(bytes,4194304),[{label:'app',type:0,subtype:16,address:65536,size:131072,flags:1}]);
  assert.throws(()=>partitions(bytes,4096));bytes[0]=0;assert.throws(()=>partitions(bytes,4194304));
});
