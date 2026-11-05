import test from 'node:test';
import assert from 'node:assert/strict';
import { EspEngine } from '../web/esp-engine.js';
import { Agent } from '../web/agent.js';
import { hardwareError, HardwareError } from '../web/hardware-errors.js';
import { makeBenchTransport } from '../web/bench-transport.js';
import { FakeTransport, ESPError } from './fakes.mjs';
const { SerialFault } = makeBenchTransport(FakeTransport, ESPError);
const MiB = 1024 * 1024;
function setup(readFlash) {
 const engine=new EspEngine({getInfo:()=>({})},{},()=>{});
 engine.identity=async()=>({chip:'esp32s3',flash_size:16*MiB});engine.loader={readFlash};
 const reports=[];let uploaded=0,checks=0;
 const context={check(){checks++;},live:()=>true,checkLive(){},device:{chip:'esp32s3',flash_size:16*MiB},
  report:d=>reports.push(d),upload:async data=>{uploaded++;return {size:data.length};}};
 return {engine,context,reports,uploaded:()=>uploaded,checks:()=>checks};
}
test('10 MiB read uses bounded sequential blocks and publishes exactly one complete artifact',async()=>{
 const calls=[];let clock=0;
 const x=setup(async(address,size,progress)=>{calls.push([address,size]);clock+=35000;const bytes=new Uint8Array(size).fill(address/MiB);progress(bytes,size,size);return bytes;});
 x.context.check=()=>{assert.ok(clock<30000,'start TTL must not be rechecked after read starts');};
 x.context.upload=async data=>{assert.equal(calls.length,10);assert.equal(data.length,10*MiB);for(let i=0;i<10;i++)assert.equal(data[i*MiB],i+1);return {size:data.length};};
 const op=await x.engine.execute('flash.read',{address:MiB,size:10*MiB,return_console:false},x.context);
 assert.equal(op.artifact.size,10*MiB);assert.equal(clock,350000);assert.ok(calls.every(([,size])=>size===MiB));
 assert.equal(x.reports.at(-1).phase,'artifact_upload');assert.equal(x.reports.at(-1).received_bytes,10*MiB);
});
test('failure in a later flash block reports original serial cause and completed prefix without publishing partial data or retry',async()=>{
 let calls=0,closed=0;const x=setup(async(_address,size)=>{calls++;if(calls===3)throw new SerialFault('closed','private payload');return new Uint8Array(size);});
 x.engine.close=async()=>{closed++;throw Error('cleanup failure must not mask serial cause');};
 await assert.rejects(x.engine.execute('flash.read',{address:0,size:4*MiB},x.context),error=>{
  const d=hardwareError(error);assert.equal(d.code,'serial_disconnected');assert.equal(d.phase,'flash_read');assert.equal(d.transport_kind,'closed');
  assert.equal(d.received_bytes,2*MiB);assert.equal(d.expected_bytes,4*MiB);assert.equal(JSON.stringify(d).includes('private'),false);return true;
 });
 assert.equal(calls,3);assert.equal(closed,1);assert.equal(x.uploaded(),0);assert.equal(x.engine.busy,false);
});
test('short block never becomes an artifact and deadline/grant loss stops further read blocks',async()=>{
 const short=setup(async()=>new Uint8Array(1));await assert.rejects(short.engine.execute('flash.read',{address:0,size:MiB},short.context),e=>e.code==='short_flash_read');assert.equal(short.uploaded(),0);
 let live=true,calls=0;const x=setup(async(_address,size)=>{calls++;live=false;return new Uint8Array(size);});
 x.context.checkLive=()=>{if(!live)throw new HardwareError('execution_deadline_exceeded');};
 await assert.rejects(x.engine.execute('flash.read',{address:0,size:2*MiB},x.context),e=>e.code==='execution_deadline_exceeded');assert.equal(calls,1);assert.equal(x.uploaded(),0);
});
test('actual transport fault kinds are classified without relying on USB event timing',()=>{
 for(const [kind,code] of [['closed','serial_disconnected'],['no-response','serial_timeout'],['partial-packet','serial_partial_packet'],['invalid-packet','serial_invalid_packet']]){
  const out=hardwareError(new SerialFault(kind,'secret'),false,{phase:'flash_read'});assert.equal(out.code,code);assert.equal(out.error_type,'SerialFault');assert.equal(out.phase,'flash_read');assert.equal(JSON.stringify(out).includes('secret'),false);
 }
 const out=hardwareError(Object.assign(new Error('secret'),{name:'NetworkError'}),false,{phase:'artifact_upload',payload:'secret'});
 assert.equal(out.code,'artifact_transfer_failed');assert.equal(JSON.stringify(out).includes('secret'),false);
});
test('agent returns useful timing, phase and progress for a late serial timeout',async()=>{
 let now=1000;const messages=[],logs=[];
 const engine={hold:false,persistHold(){},setHold(v){this.hold=v;},diagnostics:()=>({phase:'flash_read',received_bytes:8*MiB,expected_bytes:10*MiB,silence_limit_ms:100000}),
  execute:async()=>{now=350800;throw new SerialFault('no-response','secret-ssid');}};
 const agent=new Agent({engine,device:{id:'board'},token:'secret-token',now:()=>now,onLog:row=>logs.push(row)});
 agent.socket={readyState:1,send:s=>messages.push(JSON.parse(s))};agent.generation='g';agent.serverOffset=0;
 await agent.message({type:'offer',id:'op',generation:'g',lease:'lease',command:'flash.read',args:{address:0,size:10*MiB},deadline_ms:30000,lease_deadline_ms:900000});
 await agent.message({type:'execute',id:'op',deadline_ms:30000,execution_deadline_ms:600000});
 const error=messages.at(-1).error;
 assert.equal(error.code,'serial_timeout');assert.equal(error.phase,'flash_read');assert.equal(error.elapsed_ms,349800);assert.equal(error.start_remaining_ms,0);
 assert.ok(error.execution_remaining_ms>0);assert.ok(error.lease_remaining_ms>0);assert.equal(error.received_bytes,8*MiB);
 assert.equal(JSON.stringify(error).includes('secret'),false);assert.equal(logs.at(-1).detail.code,'serial_timeout');
});
test('artifact transfer failure stays distinct from USB loss and never leaks its response text',async()=>{
 const original=globalThis.fetch;
 try {
  globalThis.fetch=async()=>({ok:false,status:413});const agent=new Agent({engine:{},device:{id:'board'},token:'secret'});
  await assert.rejects(agent.artifact(null,new Uint8Array([1])),e=>e.code==='artifact_transfer_failed'&&e.detail.phase==='artifact_upload'&&e.detail.http_status===413);
  globalThis.fetch=async()=>{throw new TypeError('secret URL or token');};
  await assert.rejects(agent.artifact('artifact'),e=>e.code==='artifact_transfer_failed'&&e.detail.phase==='artifact_download'&&!JSON.stringify(e).includes('secret'));
 } finally {globalThis.fetch=original;}
});
test('ACK writer lock is always released when USB disappears during a flash read',async()=>{
 const {BenchTransport}=makeBenchTransport(FakeTransport,ESPError);let released=0;
 const t=new BenchTransport({writable:{getWriter:()=>({write:async()=>{throw new DOMException('unplug','NetworkError');},releaseLock(){released++;}})}});
 t.slipWriter=data=>FakeTransport.frame(data);
 await assert.rejects(t.write(new Uint8Array([4])),e=>e.kind==='closed');assert.equal(released,1);
 await assert.rejects(t.read(100),e=>e.kind==='closed');
});
test('SLIP reader decodes large escaped packets without appending/reallocating per byte',async()=>{
 const {BenchTransport}=makeBenchTransport(FakeTransport,ESPError),t=new BenchTransport({});let appends=0;
 const append=t.appendArray.bind(t);t.appendArray=(...args)=>{appends++;return append(...args);};
 const bytes=Uint8Array.from({length:65536},(_,i)=>i%256);t.buffer=FakeTransport.frame(bytes);
 assert.deepEqual(await t.read(100),bytes);assert.equal(appends,0);
});

test('artifact capacity HTTP status survives the diagnostic and visible journal', async()=>{
 const { AgentJournal } = await import('../web/agent-journal.js');
 const original = globalThis.fetch;
 globalThis.fetch = async()=>({ok:false,status:503,json:async()=>({error:{code:'capacity',message:'secret'}})});
 try {
  const agent = new Agent({engine:{},device:{id:'board'}});
  await assert.rejects(agent.artifact(null,new Uint8Array([1])), error=>{
   const detail=hardwareError(error);
   assert.equal(detail.code,'capacity'); assert.equal(detail.http_status,503);
   const journal=new AgentJournal();journal.append({stage:'result',detail,code:detail.code});
   assert.match(journal.rows[0].text,/HTTP 503/);
   assert.equal(JSON.stringify(journal.rows).includes('secret'),false);return true;
  });
 } finally {globalThis.fetch=original;}
});
