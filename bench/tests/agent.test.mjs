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
 const execute={type:'execute',id:'op',deadline_ms:performance.now()+11000,remaining_ms:10000};
 await x.agent.message(execute);await x.agent.message(execute);assert.equal(x.runs(),1);assert.equal(x.messages.at(-1).state,'completed');
});
test('stale or expired offer never executes hardware',async()=>{
 const x=setup();await x.agent.message({...x.offer(),deadline_ms:0});
 await x.agent.message({type:'execute',id:'op',deadline_ms:0,remaining_ms:0});
 assert.equal(x.runs(),0);assert.equal(x.messages.at(-1).state,'cancelled');
 await x.agent.message({...x.offer(),id:'other',generation:'old'});assert.equal(x.agent.pending,null);
});
test('delayed execute uses conservative server-to-local clock mapping',async()=>{
 const x=setup();await x.agent.message(x.offer());
 await x.agent.message({type:'execute',id:'op',deadline_ms:performance.now()+1000-1,remaining_ms:10000});
 assert.equal(x.runs(),0);assert.equal(x.messages.at(-1).state,'cancelled');
});
test('privileged hardware cannot execute without live local grant',async()=>{
 const x=setup();x.agent.grantUntil=0;await x.agent.message({...x.offer(),command:'flash.erase'});
 await x.agent.message({type:'execute',id:'op',deadline_ms:performance.now()+11000});
 assert.equal(x.runs(),0);assert.equal(x.messages.at(-1).state,'failed');
});
test('lease revocation cancels armed trigger and prevents delayed execution',async()=>{
 const x=setup();x.engine.trigger={};await x.agent.message(x.offer());
 await x.agent.message({type:'lease_revoked',lease:'lease'});
 await x.agent.message({type:'execute',id:'op',deadline_ms:performance.now()+11000});
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
