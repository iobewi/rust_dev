import test from 'node:test';
import assert from 'node:assert/strict';
import { Agent } from '../web/agent.js';
import { EspEngine } from '../web/esp-engine.js';

function fixture() {
 globalThis.location={href:'http://localhost:8080',protocol:'http:'};
 const sockets=[],scheduled=[],states=[],cancelled=[];
 const engine={hold:false,busy:false,persistHold(){},setHold(v){this.hold=v;},cancelTrigger(){}};
 const agent=new Agent({engine,device:{id:'board'},token:'test-token',reconnect:true,
  schedule:fn=>{scheduled.push(fn);return scheduled.length;},cancel:id=>cancelled.push(id),onState:s=>states.push(s),
  socketFactory:()=>{const socket={readyState:1,sent:[],send(s){this.sent.push(JSON.parse(s));},close(){this.readyState=3;this.onclose();}};sockets.push(socket);return socket;}});
 return {agent,engine,sockets,scheduled,states,cancelled};
}

test('server reconnect gets a new generation, revokes grant and never replays pending/result messages', async()=>{
 const x=fixture();x.agent.connect();await x.sockets[0].onopen();x.agent.generation='old';x.agent.grant(1800);
 x.agent.pending={id:'never-execute'};x.agent.awaitingResult={id:'completed-but-unacked'};
 x.sockets[0].close();assert.equal(x.engine.hold,true);assert.equal(x.agent.grantUntil,0);
 x.scheduled.shift()();await x.sockets[1].onopen();
 await x.agent.message({type:'welcome',server_ms:1,epoch:'new'});
 await x.agent.message({type:'registered',generation:'new'});
 assert.equal(x.agent.orphanedOperation,'completed-but-unacked');
 assert.equal(x.agent.awaitingResult,null);assert.equal(x.agent.pending,null);
 assert.ok(x.sockets[1].sent.every(m=>['auth','register'].includes(m.type)));
 assert.equal(x.sockets[1].sent[1].device.hold,true);
 assert.match(x.states.at(-1),/récupération/);
 // Old socket callbacks cannot change the current generation or execute.
 x.sockets[0].onmessage({data:JSON.stringify({type:'registered',generation:'stale'})});
 assert.equal(x.agent.generation,'new');
 x.agent.recover();assert.equal(x.engine.hold,true);
 await x.agent.message({type:'recovered',generation:'new'});assert.equal(x.engine.hold,false);
});

test('reconnect waits for real hardware completion and voluntary disconnect cancels it', ()=>{
 const x=fixture();x.agent.connect();x.agent.active={id:'running'};x.sockets[0].close();
 x.scheduled.shift()();assert.equal(x.sockets.length,1);
 x.agent.active=null;x.agent.disconnect();x.scheduled.shift()();assert.equal(x.sockets.length,1);
});

test('WebSerial provenance distinguishes observed identifiers from inferred transport', ()=>{
 for(const [vid,pid,transport] of [[0x10c4,0xea60,'external_uart'],[0x303a,0x1001,'usb_serial_jtag'],[0x303a,0x4001,'native_usb_other'],[123,456,'unknown']]) {
  const engine=new EspEngine({getInfo:()=>({usbVendorId:vid,usbProductId:pid})},{},()=>{});
  assert.deepEqual(engine.provenance(),{usb_vid:vid,usb_pid:pid,transport,transport_basis:'inferred_from_webserial_vid_pid'});
 }
});

test('manual reconnect reuses the agent and cancels its scheduled automatic retry',()=>{
 const x=fixture();x.agent.connect();x.agent.generation='old';x.agent.awaitingResult={id:'lost-result'};
 x.sockets[0].close();const timer=x.agent.reconnectTimer;assert.ok(timer);
 x.agent.reconnectNow();
 assert.ok(x.cancelled.includes(timer));assert.equal(x.agent.reconnectTimer,null);
 assert.equal(x.sockets.length,2);assert.equal(x.engine.hold,true);
 assert.equal(x.agent.orphanedOperation,'lost-result');assert.equal(x.agent.awaitingResult,null);
 assert.throws(()=>x.agent.reconnectNow());assert.equal(x.sockets.length,2);
});

test('manual reconnect cannot interrupt result finalization',()=>{
 const x=fixture();x.agent.connect();x.sockets[0].close();
 x.agent.awaitingResult={id:'finishing',finalizing:true};
 assert.throws(()=>x.agent.reconnectNow());assert.equal(x.sockets.length,1);
});

test('agent artifact uploads explicitly identify their role without exposing a browser identity',async()=>{
 const original=globalThis.fetch,calls=[];
 globalThis.fetch=async(url,options)=>{calls.push({url,options});return {ok:true,json:async()=>({id:'readback'})};};
 try {
  const x=fixture();x.agent.token=undefined;
  await x.agent.artifact(null,new Uint8Array([1,2,3]));
  assert.equal(calls[0].options.headers['X-Bench-Artifact-Role'],'agent');
  assert.equal(calls[0].options.headers.Authorization,undefined);
  assert.deepEqual(Object.keys(calls[0].options.headers).sort(),['X-Bench-Artifact-Role','X-SHA256']);
 } finally {globalThis.fetch=original;}
});
