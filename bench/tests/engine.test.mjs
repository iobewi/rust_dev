import test from 'node:test';
import assert from 'node:assert/strict';
import {EspEngine} from '../web/esp-engine.js';
import {fakeLoader} from './fakes.mjs';
import {sha256} from '../web/protocol.js';

async function setup(overrides={}){
 const engine=new EspEngine({getInfo:()=>({})},{},()=>{});const loader=fakeLoader(overrides);
 engine.identity=async()=>({chip:'esp32s3',flash_size:4194304});engine.loader=loader;
 let consoleOpens=0;engine.openConsole=async()=>{consoleOpens++;};
 const data=new Uint8Array(128).fill(42),sha=await sha256(data);
 const args={chip:'esp32s3',images:[{artifact:'a',address:65536,sha256:sha}]};
 const context={check(){},live:()=>true,device:{chip:'esp32s3',flash_size:4194304},download:async()=>data,upload:async()=>({})};
 return {engine,loader,data,args,context,consoleOpens:()=>consoleOpens};
}
test('hardware engine validates digest before programming',async()=>{
 const x=await setup();x.args.images[0].sha256='bad';await assert.rejects(x.engine.execute('flash.write',x.args,x.context));
 assert.equal(x.loader.calls.length,0);assert.equal(x.consoleOpens(),0);
});
test('physical identity mismatch prevents any flash begin',async()=>{
 const x=await setup();x.context.device.chip='esp32c3';await assert.rejects(x.engine.execute('flash.write',x.args,x.context));assert.equal(x.loader.calls.length,0);
});
test('engine retains hold and never reopens console after ambiguous write',async()=>{
 const x=await setup({flashDeflBegin:()=>{throw new Error('lost acknowledgement');}});
 const result=await x.engine.execute('flash.write',x.args,x.context);
 assert.equal(result.state,'indeterminate');assert.equal(x.engine.hold,true);assert.equal(x.consoleOpens(),0);
 assert.equal(x.loader.calls.filter(c=>c[0]==='flashDeflBegin').length,1);
});
test('engine rejects overlapping erase sectors before first flash mutation',async()=>{
 const x=await setup();x.args.images.push({...x.args.images[0],address:65664});
 await assert.rejects(x.engine.execute('flash.write',x.args,x.context));assert.equal(x.loader.calls.length,0);
});
test('readback requires exact length and no fake artifact on short data',async()=>{
 const x=await setup();x.loader.readFlash=async()=>new Uint8Array(2);
 let uploaded=0;x.context.upload=async()=>{uploaded++;};
 await assert.rejects(x.engine.execute('flash.read',{address:0,size:128},x.context));assert.equal(uploaded,0);
});
test('conditional byte trigger crosses chunk boundary and fires once',async()=>{
 const x=await setup(),events=[];x.engine.event=(name,data)=>events.push({name,...data});
 await x.engine.execute('trigger',{data:'YWJj',action:'mark',delay_ms:0,expires_ms:1000},x.context);
 x.engine.matchTrigger(new TextEncoder().encode('xa'));x.engine.matchTrigger(new TextEncoder().encode('bcabc'));
 await new Promise(r=>setTimeout(r,5));assert.equal(events.filter(e=>e.name==='trigger').length,1);assert.equal(events.filter(e=>e.name==='mark').length,1);
});

test('continuous UART capture splits large Web Serial reads into valid bounded binary frames',async()=>{
 const bytes=Uint8Array.from({length:50000},(_,i)=>i%256),frames=[];let reads=0;
 const port={getInfo:()=>({}),readable:{getReader:()=>({read:async()=>reads++?{done:true}:{value:bytes},releaseLock(){}})}};
 const engine=new EspEngine(port,{},(kind,data)=>{if(kind==='uart')frames.push(data.data);});await engine.readLoop();
 const {unb64}=await import('../web/protocol.js');assert.equal(frames.length,4);assert.ok(frames.every(s=>unb64(s).length<=16384));
 assert.deepEqual(Buffer.concat(frames.map(s=>Buffer.from(unb64(s)))),Buffer.from(bytes));
});

for(const fail of [false,true]){
 test(`triggered reset holds the hardware lock until ${fail?'failure':'completion'}`,async()=>{
  const x=await setup();x.engine.mode='console';
  let startedReset,finishReset,finishedReset,identityCalls=0;
  const started=new Promise(resolve=>{startedReset=resolve;});
  const gate=new Promise(resolve=>{finishReset=resolve;});
  const finished=new Promise(resolve=>{finishedReset=resolve;});
  x.engine.reset=async()=>{startedReset();await gate;if(fail)throw new Error('Reset transport failure');};
  x.engine.identity=async()=>{identityCalls++;return {chip:'esp32s3',flash_size:4194304};};
  x.engine.event=(kind)=>{if(kind==='transport')finishedReset();};
  await x.engine.execute('trigger',{data:'YQ==',action:'reset',delay_ms:0,expires_ms:1000},x.context);
  x.engine.matchTrigger(new Uint8Array([97]));await started;
  try{
   assert.equal(x.engine.busy,true);
   await assert.rejects(x.engine.execute('identify',{},x.context),/Device is busy/);
   assert.equal(identityCalls,0);
  }finally{finishReset();}
  if(fail)await finished;
  else await new Promise(resolve=>setTimeout(resolve,0));
  assert.equal(x.engine.busy,false);
  await x.engine.execute('identify',{},x.context);assert.equal(identityCalls,1);
 });
}

for(const action of ['mark','reset']){
 test(`disarm cancels a matched, delayed ${action} trigger`,async()=>{
  const x=await setup(),events=[];let resets=0;x.engine.mode='console';
  x.engine.event=kind=>events.push(kind);x.engine.reset=async()=>{resets++;};
  await x.engine.execute('trigger',{data:'YQ==',action,delay_ms:20,expires_ms:1000},x.context);
  x.engine.matchTrigger(new Uint8Array([97]));
  await x.engine.execute('trigger',{action:'disarm'},x.context);
  await new Promise(resolve=>setTimeout(resolve,40));
  assert.equal(resets,0);assert.equal(events.includes('mark'),false);assert.equal(x.engine.trigger,null);
 });
}
test('rearming cancels a pending action from the previous trigger',async()=>{
 const x=await setup();let resets=0;x.engine.mode='console';x.engine.reset=async()=>{resets++;};
 await x.engine.execute('trigger',{data:'YQ==',action:'reset',delay_ms:20,expires_ms:1000},x.context);
 x.engine.matchTrigger(new Uint8Array([97]));
 await x.engine.execute('trigger',{data:'Yg==',action:'reset',delay_ms:0,expires_ms:1000},x.context);
 await new Promise(resolve=>setTimeout(resolve,40));assert.equal(resets,0);
 x.engine.matchTrigger(new Uint8Array([98]));await new Promise(resolve=>setTimeout(resolve,5));assert.equal(resets,1);
});

test('partition failure identifies the address problem without exposing raw flash bytes',async()=>{
 const x=await setup();x.loader.readFlash=async()=>new Uint8Array(0xc00).fill(255);
 await assert.rejects(x.engine.execute('partitions',{address:0x8000},x.context),e=>e.code==='partition_table_invalid');
});


test('known USB Serial/JTAG exposes an internal reset but never IO0',async()=>{
 const signals=[],port={getInfo:()=>({usbVendorId:0x303a,usbProductId:0x1001}),setSignals:async value=>signals.push(value)};
 const engine=new EspEngine(port,{},()=>{});engine.mode='console';
 assert.equal(engine.capabilities().includes('reset'),true);assert.equal(engine.capabilities().includes('io0'),false);
 const result=await engine.execute('reset',{}, {check(){}});
 assert.deepEqual(result,{reset:true,method:'usb_serial_jtag',boot_verified:false});
 assert.deepEqual(signals,[{dataTerminalReady:false,requestToSend:true},{dataTerminalReady:false,requestToSend:false}]);
 assert.equal(engine.mode,'console');assert.equal(engine.busy,false);
 await assert.rejects(engine.execute('io0',{duration_ms:1},{check(){}}),error=>error.code==='external_uart_required');
 assert.equal(signals.length,2);
});

test('native OTG or unidentified Espressif ports never advertise or attempt internal reset',async()=>{
 for(const info of [{usbVendorId:0x303a},{usbVendorId:0x303a,usbProductId:0x4001}]){
  let calls=0;const engine=new EspEngine({getInfo:()=>info,setSignals:async()=>{calls++;}},{},()=>{});engine.mode='console';
  assert.equal(engine.capabilities().includes('reset'),false);
  await assert.rejects(engine.reset(),error=>error.code==='external_uart_required');assert.equal(calls,0);
 }
});

test('native reset is refused in loader mode and while quarantined without signaling',async()=>{
 let calls=0;const engine=new EspEngine({getInfo:()=>({usbVendorId:0x303a,usbProductId:0x1001}),setSignals:async()=>{calls++;}},{},()=>{});
 engine.mode='loader';await assert.rejects(engine.execute('reset',{}, {check(){}}),error=>error.code==='console_required');
 engine.mode='console';engine.hold=true;await assert.rejects(engine.execute('reset',{}, {check(){}}));assert.equal(calls,0);
});

test('native reset attempts release after an assertion failure and preserves the first error',async()=>{
 const original=new Error('assertion failed'),signals=[];
 const engine=new EspEngine({getInfo:()=>({usbVendorId:0x303a,usbProductId:0x1001}),setSignals:async value=>{signals.push(value);throw original;}},{},()=>{});engine.mode='console';
 await assert.rejects(engine.reset(),error=>error===original);
 assert.equal(signals.length,2);assert.equal(signals[1].requestToSend,false);
});


test('close releases a partially opened Web Serial port even when engine mode is off',async()=>{
 let closes=0;const port={getInfo:()=>({}),readable:new ReadableStream(),writable:new WritableStream(),async close(){closes++;this.readable=null;this.writable=null;}};
 const engine=new EspEngine(port,{},()=>{});await engine.close();assert.equal(closes,1);assert.equal(port.readable,null);assert.equal(port.writable,null);
 await engine.close();assert.equal(closes,1);
});

test('a rejected reader cancellation does not skip Web Serial close',async()=>{
 let closes=0;const port={getInfo:()=>({}),async close(){closes++;}};
 const engine=new EspEngine(port,{},()=>{});engine.mode='console';engine.reader={cancel:async()=>{throw new Error('reader already lost');}};engine.readDone=Promise.resolve();
 await engine.close();assert.equal(closes,1);assert.equal(engine.mode,'off');
});
