// A real Chromium page and WebSocket, with a simulated hardware engine. No USB qualification.
import test from 'node:test';
import assert from 'node:assert/strict';
import { createRequire } from 'node:module';
import { spawn } from 'node:child_process';
import { createInterface } from 'node:readline';
const { chromium }=createRequire(import.meta.url)('playwright');

test('manual UI and independent API drive the same browser Agent; binary round-trip and disconnect',async()=>{
 const proc=spawn('python3',['tests/browser_fixture.py'],{cwd:new URL('..',import.meta.url),stdio:['ignore','pipe','pipe']});
 let stderr='';proc.stderr.on('data',b=>stderr+=b);
 const lines=createInterface({input:proc.stdout});
 let browser;
 try{
  const url=await new Promise((resolve,reject)=>{lines.once('line',s=>resolve(JSON.parse(s).url));proc.once('exit',()=>reject(new Error(stderr)));});
  browser=await chromium.launch({headless:true,executablePath:chromium.executablePath(),args:['--no-sandbox']});const page=await browser.newPage();
  const errors=[];page.on('pageerror',e=>errors.push(e.message));await page.goto(url);
  await page.evaluate(async()=>{
   const {Agent}=await import('/agent.js');
   const flash=new Uint8Array(4194304).fill(255);
   const engine={hold:false,persistHold(){},setHold(v){this.hold=v;},async execute(name,args,ctx){
    ctx.check();
    if(name==='flash.write'){for(const i of args.images)flash.set(await ctx.download(i.artifact),i.address);return {written:true};}
    if(name==='flash.read')return {artifact:await ctx.upload(flash.slice(args.address,args.address+args.size))};
    if(name==='reset')window.testAgent.event('uart',{data:'AP8NCg==',at:Date.now()});
    return {simulated:true,command:name};
   }};
   window.testAgent=new Agent({engine,device:{id:'board',chip:'esp32s3',flash_size:4194304,capabilities:['identify','reset','mark','flash.write','flash.read']},token:'agent-secret-12345678'});
   window.testAgent.connect();
  });
  await page.waitForFunction(()=>window.testAgent?.generation);
  await page.evaluate(async()=>{
   const {Agent}=await import('/agent.js');
   const engine={hold:false,persistHold(){},setHold(v){this.hold=v;},async execute(name){return {command:name,board:'board-b'};}};
   window.secondAgent=new Agent({engine,device:{id:'board-b',chip:'esp32s3',flash_size:4194304,capabilities:['identify','reset']},token:'agent-second-12345678'});
   window.secondAgent.connect();
  });
  await page.waitForFunction(()=>window.secondAgent?.generation);
  await page.fill('#client-token','client-alice-123456789');await page.click('#refresh');
  await page.waitForSelector('#devices option',{state:'attached'});await page.click('#reserve');await page.waitForFunction(()=>document.querySelector('#lease-state').textContent.includes('Réservation'));
  await page.click('[data-command="identify"]');await page.waitForFunction(()=>JSON.parse(document.querySelector('#result').textContent).state==='completed');
  const op=JSON.parse(await page.textContent('#result'));assert.equal(op.result.simulated,true);
  assert.equal(await page.locator('#devices').isDisabled(),true);
  await page.click('#refresh');await page.waitForFunction(()=>Array.isArray(JSON.parse(document.querySelector('#result').textContent).devices)&&document.querySelector('#devices').value==='board'&&document.querySelector('#devices').disabled);
  let submissions=0;page.on('request',request=>{if(request.method()==='POST'&&request.url().endsWith('/api/v1/operations'))submissions++;});
  await page.evaluate(()=>{document.querySelector('#devices').value='board-b';});
  await page.click('[data-command="reset"]');
  await page.waitForFunction(()=>document.querySelector('#client-state').textContent.includes('diffère'));
  assert.equal(submissions,0);
  page.on('dialog',dialog=>dialog.accept());
  await page.click('#write');await page.waitForFunction(()=>document.querySelector('#client-state').textContent.includes('diffère'));
  assert.equal(submissions,0);
  await page.evaluate(()=>{document.querySelector('#devices').value='board';});
  await page.click('#release');await page.waitForFunction(()=>document.querySelector('#lease-state').textContent==='Libérée.');
  assert.equal(await page.locator('#devices').isDisabled(),false);
  await page.selectOption('#devices','board-b');await page.click('#reserve');
  await page.waitForFunction(()=>document.querySelector('#lease-state').textContent.includes('board-b'));
  await page.click('[data-command="reset"]');
  await page.waitForFunction(()=>{const op=JSON.parse(document.querySelector('#result').textContent);return op.state==='completed'&&op.device==='board-b';});
  assert.equal(JSON.parse(await page.textContent('#result')).result.board,'board-b');
  await page.click('#release');await page.waitForFunction(()=>document.querySelector('#lease-state').textContent==='Libérée.');
  await page.evaluate(()=>window.testAgent.grant(60));
  const auth={Authorization:'Bearer client-alice-123456789'};
  async function api(path,method='GET',body){const r=await fetch(url+'/api/v1/'+path,{method,headers:{...auth,'Content-Type':'application/json'},...(body?{body:JSON.stringify(body)}:{})});assert.ok(r.ok,await r.clone().text());return r.json();}
  const lease=await api('leases','POST',{device:'board',ttl:60});
  async function command(name,args={},extra={}){
   let op=await api('operations','POST',{lease:lease.id,command:name,args,...extra});
   for(let i=0;i<100&&!['completed','failed','indeterminate'].includes(op.state);i++){await new Promise(r=>setTimeout(r,20));op=await api('operations/'+op.id);}
   assert.equal(op.state,'completed');return op;
  }
  const bytes=Uint8Array.from({length:8192},(_,i)=>i%256),digest=await crypto.subtle.digest('SHA-256',bytes);
  const sha=Buffer.from(digest).toString('hex');
  const upload=await fetch(url+'/api/v1/artifacts',{method:'POST',headers:{...auth,'X-SHA256':sha},body:bytes});assert.equal(upload.status,201);const artifact=await upload.json();
  await command('flash.write',{chip:'esp32s3',images:[{artifact:artifact.id,sha256:sha,address:65536}]},{allow_destructive:true});
  const read=await command('flash.read',{address:65536,size:bytes.length});
  const download=await fetch(url+'/api/v1/artifacts/'+read.result.artifact.id,{headers:auth});assert.deepEqual(new Uint8Array(await download.arrayBuffer()),bytes);
  await command('reset');const events=await api('devices/board/events');assert.ok(events.events.some(e=>e.kind==='uart'&&e.data==='AP8NCg=='));
  await page.evaluate(()=>window.testAgent.socket.close());
  for(let i=0;i<50;i++){if(!(await api('devices')).devices[0].connected)break;await new Promise(r=>setTimeout(r,10));}
  assert.equal((await api('devices')).devices[0].connected,false);
  assert.deepEqual(errors,[]);
 }finally{await browser?.close();proc.kill('SIGTERM');lines.close();}
});
