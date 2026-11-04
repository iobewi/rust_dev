import test from 'node:test';
import assert from 'node:assert/strict';
import { WorkWakeLock } from '../web/wake-lock.js';
import { SerialReconnect } from '../web/serial-reconnect.js';
import { AgentJournal } from '../web/agent-journal.js';
import { HardwareError, hardwareError } from '../web/hardware-errors.js';
function fixture() {
 let visible=true, requests=0, releases=0;const locks=[],states=[];
 const wake=new WorkWakeLock({visible:()=>visible,onState:s=>states.push(s),wakeLock:{async request(){requests++;const lock=new EventTarget();lock.release=async()=>{releases++;lock.dispatchEvent(new Event('release'));};locks.push(lock);return lock;}}});
 return {wake,locks,states,requests:()=>requests,releases:()=>releases,visible:v=>visible=v};
}
test('wake lock follows actual work, releases on hidden/end and reacquires while work remains',async()=>{
 const x=fixture();await x.wake.update(true);await x.wake.update(true);assert.equal(x.requests(),1);
 x.visible(false);await x.wake.update(true,true);assert.equal(x.releases(),1);
 x.visible(true);await x.wake.update(true,true);assert.equal(x.requests(),2);
 await x.wake.update(false);assert.equal(x.releases(),2);await x.wake.update(false,true);assert.equal(x.requests(),2);
});
test('wake lock acquired after work ended is immediately released',async()=>{
 let finish,released=0;const lock={release:async()=>released++};
 const wake=new WorkWakeLock({visible:()=>true,wakeLock:{request:()=>new Promise(r=>finish=r)}});
 const pending=wake.update(true);await wake.update(false);finish(lock);await pending;assert.equal(released,1);assert.equal(wake.lock,null);
});
test('refused or unsupported wake lock never blocks work or retries in a loop',async()=>{
 let requests=0;const states=[];const wake=new WorkWakeLock({visible:()=>true,onState:s=>states.push(s),wakeLock:{async request(){requests++;throw new Error('denied');}}});
 await wake.update(true);await wake.update(true);assert.equal(requests,1);assert.match(states.at(-1),/refusée/);
 await wake.update(true,true);assert.equal(requests,2);await new WorkWakeLock({visible:()=>true}).update(true);
});
test('system wake lock release is visible and can be retried on visibility change',async()=>{
 const x=fixture();await x.wake.update(true);x.locks[0].dispatchEvent(new Event('release'));await x.wake.update(true);
 assert.equal(x.requests(),1);assert.match(x.states.at(-1),/interrompue/);await x.wake.update(true,true);assert.equal(x.requests(),2);
});
function reconnection() {
 const port={},other={},calls=[],states=[];let idle=true,available=[other];
 const reconnect=new SerialReconnect({ports:async()=>available,idle:()=>idle,attach:async(...args)=>calls.push(args),state:s=>states.push(s),schedule:()=>1,cancel(){}});
 const identity={mac:'card-a'};reconnect.remember(port,identity);
 return {reconnect,port,other,identity,calls,states,idle:v=>idle=v,ports:v=>available=v};
}
test('USB reconnect never selects another authorized port and waits for active hardware',async()=>{
 const x=reconnection();x.reconnect.disconnected(x.port);await x.reconnect.attempt();assert.equal(x.calls.length,0);
 x.ports([x.port,x.other]);x.idle(false);await x.reconnect.attempt();assert.equal(x.calls.length,0);
 x.idle(true);await x.reconnect.attempt();assert.deepEqual(x.calls,[[x.port,x.identity]]);assert.equal(x.reconnect.waiting,false);
});
test('explicit disconnect cancels reconnect and an identity mismatch stops retries',async()=>{
 const x=reconnection();x.ports([x.port]);x.reconnect.disconnected(x.port);x.reconnect.stop();await x.reconnect.attempt();assert.equal(x.calls.length,0);
 x.reconnect.attach=async()=>{throw new HardwareError('physical_identity_changed');};x.reconnect.disconnected(x.port);await x.reconnect.attempt();
 assert.equal(x.reconnect.waiting,false);assert.equal(x.states.at(-1),'identity_changed');
});
test('journal is bounded and excludes arbitrary errors, credentials and payloads',()=>{
 const journal=new AgentJournal(2),secret='secret-SSID-or-token';
 journal.append({stage:'execute',command:'flash.write',id:'abcd',args:{token:secret},data:secret});
 journal.append({stage:'result',code:secret,state:'failed'});journal.append({stage:'upload',bytes:4096});
 assert.equal(journal.rows.length,2);assert.equal(JSON.stringify(journal.rows).includes(secret),false);assert.match(journal.rows.at(-1).text,/4096 octets/);
 assert.equal(hardwareError(new Error(secret)).code,'hardware_command_failed');assert.equal(hardwareError(new Error(secret),true).code,'serial_disconnected');
});


test('forgetting a voluntarily disconnected port prevents delayed events from restarting it',async()=>{
 const x=reconnection();x.ports([x.port]);x.reconnect.forget();x.reconnect.disconnected(x.port);await x.reconnect.attempt();
 assert.equal(x.reconnect.waiting,false);assert.equal(x.reconnect.port,null);assert.equal(x.calls.length,0);
});
