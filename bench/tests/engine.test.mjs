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
