import test from 'node:test';
import assert from 'node:assert/strict';
import { Agent } from '../web/agent.js';
import { EspEngine } from '../web/esp-engine.js';

function setup(){
 let runs=0,holds=[];
 const engine={hold:false,persistHold:value=>holds.push(value),setHold(value){this.hold=value;},async execute(){runs++;return {ok:true};}};
 const agent=new Agent({engine,device:{id:'board'},token:'secret'});
 const messages=[];agent.socket={readyState:1,bufferedAmount:0,send:s=>messages.push(JSON.parse(s))};
 agent.generation='gen';agent.serverOffset=1000;agent.grantUntil=performance.now()+10000;
 const offer=()=>({type:'offer',id:'op',generation:'gen',command:'identify',args:{},lease:'lease',
  deadline_ms:performance.now()+1000+10000,remaining_ms:10000,lease_deadline_ms:performance.now()+1000+20000});
 return {agent,engine,messages,offer,runs:()=>runs,holds};
}
test('agent acknowledges then executes once, duplicate execute is ignored',async()=>{
 const x=setup();await x.agent.message(x.offer());assert.equal(x.runs(),0);assert.equal(x.messages[0].type,'ready');
 const execute={type:'execute',id:'op',execution_deadline_ms:performance.now()+121000,deadline_ms:performance.now()+11000,remaining_ms:10000};
 await x.agent.message(execute);await x.agent.message(execute);assert.equal(x.runs(),1);assert.equal(x.messages.at(-1).state,'completed');
});
test('stale or expired offer never executes hardware',async()=>{
 const x=setup();await x.agent.message({...x.offer(),deadline_ms:0});
 await x.agent.message({type:'execute',id:'op',execution_deadline_ms:performance.now()+121000,deadline_ms:0,remaining_ms:0});
 assert.equal(x.runs(),0);assert.equal(x.messages.at(-1).state,'cancelled');
 await x.agent.message({...x.offer(),id:'other',generation:'old'});assert.equal(x.agent.pending,null);
});
test('delayed execute uses conservative server-to-local clock mapping',async()=>{
 const x=setup();await x.agent.message(x.offer());
 await x.agent.message({type:'execute',id:'op',execution_deadline_ms:performance.now()+121000,deadline_ms:performance.now()+1000-1,remaining_ms:10000});
 assert.equal(x.runs(),0);assert.equal(x.messages.at(-1).state,'cancelled');
});
test('privileged hardware cannot execute without live local grant',async()=>{
 const x=setup();x.agent.grantUntil=0;await x.agent.message({...x.offer(),command:'flash.erase'});
 await x.agent.message({type:'execute',id:'op',execution_deadline_ms:performance.now()+121000,deadline_ms:performance.now()+11000});
 assert.equal(x.runs(),0);assert.equal(x.messages.at(-1).state,'failed');
});
test('lease revocation cancels armed trigger and prevents delayed execution',async()=>{
 const x=setup();x.engine.trigger={};await x.agent.message(x.offer());
 await x.agent.message({type:'lease_revoked',lease:'lease'});
 await x.agent.message({type:'execute',id:'op',execution_deadline_ms:performance.now()+121000,deadline_ms:performance.now()+11000});
 assert.equal(x.runs(),0);assert.equal(x.engine.trigger,null);
});
test('UART backpressure drops are observable in the source sequence',()=>{
 const x=setup();x.agent.event('uart',{data:'AA=='});x.agent.socket.bufferedAmount=2*1024*1024;
 x.agent.event('uart',{data:'AA=='});x.agent.socket.bufferedAmount=0;x.agent.event('uart',{data:'AA=='});
 assert.deepEqual(x.messages.map(m=>m.seq),[1,3]);
});
test('engine never resets native USB using EN or IO0',async()=>{
 const engine=new EspEngine({getInfo:()=>({usbVendorId:0x303a})},{},()=>{});
 assert.equal(engine.capabilities().includes('reset'),false);
 await assert.rejects(engine.reset());
 await assert.rejects(engine.execute('io0',{duration_ms:1},{check(){}}));
});
test('engine guards unsafe operations while quarantined',async()=>{
 const engine=new EspEngine({getInfo:()=>({})},{},()=>{});engine.hold=true;
 await assert.rejects(engine.execute('console',{},{}));
 await assert.rejects(engine.execute('uart.write',{data:'AA=='},{}));
});
test('operator recovery is refused while hardware is active',()=>{
 const x=setup();x.agent.active={};assert.throws(()=>x.agent.recover());
});

test('operator hold is cleared only after server recovery acknowledgement',async()=>{
 const x=setup();x.engine.hold=true;x.agent.recover();assert.equal(x.engine.hold,true);
 await x.agent.message({type:'recovered',generation:'old'});assert.equal(x.engine.hold,true);
 await x.agent.message({type:'recovered',generation:'gen'});assert.equal(x.engine.hold,false);
});

// Exercise Agent and the actual flash engine together; the loader is a controlled double.
for (const command of ['flash.write','flash.erase']) {
 test(`execution timeout preserves hold after late ${command} success without reopening console`,async()=>{
  const {fakeLoader}=await import('./fakes.mjs');
  const {sha256}=await import('../web/protocol.js');
  const {md5hex}=await import('../web/md5.js');
  let now=1000,consoleOpens=0,finishHardware,hardwareStarted;
  const started=new Promise(resolve=>{hardwareStarted=resolve;});
  const gate=new Promise(resolve=>{finishHardware=resolve;});
  const data=new Uint8Array([1,2,3,4]);
  const loader=fakeLoader({
   flashDeflBlock:async()=>{hardwareStarted();await gate;},
   eraseFlash:async()=>{hardwareStarted();await gate;},
   flashMd5sum:()=>md5hex(data),
  });
  const holds=[],engine=new EspEngine({getInfo:()=>({})},{},()=>{},value=>holds.push(value));
  engine.loader=loader;engine.identity=async()=>({chip:'esp32s3',flash_size:4194304});
  engine.openConsole=async()=>{consoleOpens++;};
  const device={id:'board',chip:'esp32s3',flash_size:4194304};
  const agent=new Agent({engine,device,token:'secret',now:()=>now}),messages=[];
  agent.socket={readyState:1,send:s=>messages.push(JSON.parse(s))};agent.generation='gen';agent.serverOffset=1000;agent.grantUntil=50000;
  agent.artifact=async()=>data;
  const args={chip:'esp32s3',return_console:true,...(command==='flash.write'?{images:[{artifact:'a',address:65536,sha256:await sha256(data)}]}:{})};
  await agent.message({type:'offer',id:'op',generation:'gen',command,args,lease:'lease',
   deadline_ms:31000,lease_deadline_ms:61000,timeout_ms:1000});
  const running=agent.message({type:'execute',id:'op',deadline_ms:31000,execution_deadline_ms:3100});
  await started;
  now=2200; // Beyond the execution deadline; reservation, grant and start TTL remain valid.
  assert.equal(engine.busy,true);assert.equal(engine.hold,true);
  finishHardware();await running;
  assert.equal(consoleOpens,0);assert.equal(engine.hold,true);assert.equal(holds.at(-1),true);
  assert.equal(messages.at(-1).hold,true);assert.equal(messages.at(-1).state,'completed');
  // The physical result can be late evidence, but it must not release the safety hold or retry.
  assert.equal(loader.calls.filter(c=>c[0]===(command==='flash.write'?'flashDeflBegin':'eraseFlash')).length,1);
 });
}

test('execute delayed past the server execution deadline never starts hardware',async()=>{
 const x=setup();await x.agent.message(x.offer());
 await x.agent.message({type:'execute',id:'op',deadline_ms:performance.now()+11000,execution_deadline_ms:0});
 assert.equal(x.runs(),0);assert.equal(x.messages.at(-1).state,'failed');
});

test('completed trigger keeps its own lifetime after the setup execution deadline',async()=>{
 let now=1000;
 const engine=new EspEngine({getInfo:()=>({})},{},()=>{});
 const agent=new Agent({engine,device:{id:'board'},token:'secret',now:()=>now});
 agent.socket={readyState:1,send(){}};agent.generation='gen';agent.serverOffset=1000;agent.grantUntil=50000;
 await agent.message({type:'offer',id:'op',generation:'gen',lease:'lease',command:'trigger',
  args:{data:'YQ==',action:'mark',expires_ms:30000},deadline_ms:31000,lease_deadline_ms:61000});
 await agent.message({type:'execute',id:'op',deadline_ms:31000,execution_deadline_ms:3100});
 now=2200;assert.equal(engine.trigger.live(),true);
 await agent.message({type:'lease_revoked',lease:'lease'});assert.equal(engine.trigger,null);
});

test('completion remains persistently blocked until the matching result acknowledgement',async()=>{
 const x=setup();await x.agent.message(x.offer());
 await x.agent.message({type:'execute',id:'op',deadline_ms:performance.now()+11000,execution_deadline_ms:performance.now()+121000});
 assert.equal(x.engine.hold,true);assert.equal(x.holds.at(-1),true);assert.equal(x.agent.awaitingResult.id,'op');
 for(const ack of [{id:'other',generation:'gen'},{id:'op',generation:'stale'}]){
  await x.agent.message({type:'result_ack',...ack,quarantined:false});assert.equal(x.engine.hold,true);
 }
 await x.agent.message({type:'result_ack',id:'op',generation:'gen',quarantined:false});
 assert.equal(x.engine.hold,false);assert.equal(x.agent.awaitingResult,null);
});
test('server quarantine acknowledgement never clears the local hold',async()=>{
 const x=setup();await x.agent.message(x.offer());
 await x.agent.message({type:'execute',id:'op',deadline_ms:performance.now()+11000,execution_deadline_ms:performance.now()+121000});
 await x.agent.message({type:'result_ack',id:'op',generation:'gen',quarantined:true});
 assert.equal(x.engine.hold,true);
});
test('disconnect after sending an unacknowledged result preserves the hold and never replays',async()=>{
 const x=setup();globalThis.location={href:'http://localhost/',protocol:'http:'};
 x.agent.socketFactory=()=>x.agent.socket;x.agent.connect();
 await x.agent.message(x.offer());
 await x.agent.message({type:'execute',id:'op',deadline_ms:performance.now()+11000,execution_deadline_ms:performance.now()+121000});
 x.agent.socket.readyState=3;x.agent.socket.onclose();
 assert.equal(x.engine.hold,true);assert.equal(x.holds.at(-1),true);assert.equal(x.runs(),1);
 await x.agent.message({type:'result_ack',id:'op',generation:'gen',quarantined:false});assert.equal(x.engine.hold,true);
});
test('next offer waits until acknowledged console return has finished',async()=>{
 const x=setup();let finishConsole,startedConsole;
 const gate=new Promise(resolve=>{finishConsole=resolve;});const started=new Promise(resolve=>{startedConsole=resolve;});
 x.engine.execute=async(name,args,ctx)=>{ctx.complete({hold:false,returnConsole:true});return {ok:true};};
 x.engine.openConsole=async()=>{startedConsole();await gate;};
 await x.agent.message(x.offer());await x.agent.message({type:'execute',id:'op',deadline_ms:performance.now()+11000,execution_deadline_ms:performance.now()+121000});
 const ack=x.agent.message({type:'result_ack',id:'op',generation:'gen',quarantined:false});await started;
 const before=x.messages.length;await x.agent.message({...x.offer(),id:'second'});assert.equal(x.messages.length,before);
 finishConsole();await ack;assert.deepEqual(x.messages.at(-1),{type:'ready',id:'second'});
});

test('actual flash engine defers hold release and console return until result acknowledgement',async()=>{
 const {fakeLoader}=await import('./fakes.mjs');const {sha256}=await import('../web/protocol.js');const {md5hex}=await import('../web/md5.js');
 const data=new Uint8Array([1,2,3]),holds=[];let consoleOpens=0;
 const engine=new EspEngine({getInfo:()=>({})},{},()=>{},v=>holds.push(v));
 engine.loader=fakeLoader({flashMd5sum:()=>md5hex(data)});engine.identity=async()=>({chip:'esp32s3',flash_size:4194304});
 engine.openConsole=async()=>{consoleOpens++;};
 const agent=new Agent({engine,device:{id:'board',chip:'esp32s3',flash_size:4194304},token:'secret',now:()=>1000});
 const messages=[];agent.socket={readyState:1,send:s=>messages.push(JSON.parse(s))};agent.generation='gen';agent.serverOffset=0;agent.grantUntil=60000;agent.artifact=async()=>data;
 await agent.message({type:'offer',id:'op',generation:'gen',lease:'lease',command:'flash.write',
  args:{chip:'esp32s3',images:[{artifact:'a',address:65536,sha256:await sha256(data)}]},deadline_ms:30000,lease_deadline_ms:60000});
 await agent.message({type:'execute',id:'op',deadline_ms:30000,execution_deadline_ms:120000});
 assert.equal(consoleOpens,0);assert.equal(engine.hold,true);assert.equal(holds.at(-1),true);
 assert.equal(messages.at(-1).hold,false);assert.equal(messages.at(-1).state,'completed');
 await agent.message({type:'result_ack',id:'op',generation:'gen',quarantined:false});
 assert.equal(consoleOpens,1);assert.equal(engine.hold,false);assert.equal(holds.at(-1),false);
});
test('console return failure reasserts the hold and informs the broker',async()=>{
 const x=setup();x.engine.execute=async(name,args,ctx)=>{ctx.complete({hold:false,returnConsole:true});return {};};
 x.engine.openConsole=async()=>{throw new Error('Port lost');};
 await x.agent.message(x.offer());await x.agent.message({type:'execute',id:'op',deadline_ms:performance.now()+11000,execution_deadline_ms:performance.now()+121000});
 await x.agent.message({type:'result_ack',id:'op',generation:'gen',quarantined:false});
 assert.equal(x.engine.hold,true);assert.equal(x.messages.at(-1).type,'hold');
});

test('agent publishes safe diagnostics and activity until acknowledgement',async()=>{
 const { HardwareError }=await import('../web/hardware-errors.js');const x=setup(),logs=[],activity=[];
 x.agent.onLog=e=>logs.push(e);x.agent.onActivity=v=>activity.push(v);
 x.engine.execute=async()=>{throw new HardwareError('console_required');};
 await x.agent.message(x.offer());await x.agent.message({type:'execute',id:'op',deadline_ms:performance.now()+11000,execution_deadline_ms:performance.now()+121000});
 assert.equal(x.messages.at(-1).error.code,'console_required');assert.equal(logs.at(-1).command,'identify');assert.equal(activity[0],true);
 await x.agent.message({type:'result_ack',id:'op',generation:'gen',quarantined:false});assert.equal(activity.at(-1),false);
});
test('local release refuses active work and does not clear a safety hold',()=>{
 const x=setup();x.engine.hold=true;x.agent.active={};assert.throws(()=>x.agent.releaseControl());
 x.agent.active=null;x.agent.releaseControl();assert.equal(x.engine.hold,true);assert.equal(x.agent.grantUntil<=performance.now(),true);
 assert.equal(x.messages.at(-1).type,'release_control');
});
