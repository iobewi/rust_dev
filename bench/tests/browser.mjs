// A real Chromium page and WebSocket, with a simulated hardware engine. No USB qualification.
import test from 'node:test';
import assert from 'node:assert/strict';
import { createRequire } from 'node:module';
import { spawn } from 'node:child_process';
import { createInterface } from 'node:readline';
const { chromium }=createRequire(import.meta.url)('playwright');

test('automatic manual operations and independent API share exclusion across two browser agents',async()=>{
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
    ctx.check();if(name==='identify'&&window.blockIdentify)await new Promise(resolve=>{window.releaseIdentify=resolve;});
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
   window.secondAgent=new Agent({engine,device:{id:'board-b',chip:'esp32s3',flash_size:4194304,capabilities:['identify','reset','flash.write']},token:'agent-second-12345678'});
   window.secondAgent.connect();
  });
  await page.waitForFunction(()=>window.secondAgent?.generation);
  await page.waitForFunction(()=>document.querySelector('#devices').options.length===2);
  assert.equal(await page.locator('#client-token').count(),0);assert.equal(await page.locator('#reserve').count(),0);
  await page.selectOption('#devices','board');await page.evaluate(()=>{window.blockIdentify=true;});
  await page.click('[data-command="identify"]');await page.waitForFunction(()=>typeof window.releaseIdentify==='function');
  assert.equal(await page.locator('#devices').isDisabled(),true);
  await page.evaluate(()=>{document.querySelector('#devices').value='board-b';});await page.click('#refresh');
  await page.waitForFunction(()=>document.querySelector('#devices').value==='board');
  await page.evaluate(()=>{window.releaseIdentify();window.blockIdentify=false;});
  await page.waitForFunction(()=>JSON.parse(document.querySelector('#result').textContent).state==='completed');
  assert.equal(JSON.parse(await page.textContent('#result')).device,'board');
  await page.waitForFunction(async()=>{const r=await fetch('/api/v1/devices');return (await r.json()).devices.find(d=>d.id==='board').reservation===null;});
  await page.waitForFunction(()=>!document.querySelector('#devices').disabled);
  await page.selectOption('#devices','board-b');await page.click('[data-command="reset"]');
  await page.waitForFunction(()=>{const op=JSON.parse(document.querySelector('#result').textContent);return op.state==='completed'&&op.device==='board-b';});
  assert.equal(JSON.parse(await page.textContent('#result')).result.board,'board-b');
  await page.waitForFunction(async()=>{const r=await fetch('/api/v1/devices');return (await r.json()).devices.find(d=>d.id==='board-b').reservation===null;});
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

test('local browser session connects Web Serial and drives automatic per-operation control without tokens', async () => {
 const { readFile } = await import('node:fs/promises');
 const proc=spawn('python3',['tests/browser_fixture.py'],{cwd:new URL('..',import.meta.url),stdio:['ignore','pipe','pipe']});
 let stderr='';proc.stderr.on('data',b=>stderr+=b);const lines=createInterface({input:proc.stdout});let browser;
 try {
  const url=await new Promise((resolve,reject)=>{lines.once('line',s=>resolve(JSON.parse(s).url));proc.once('exit',()=>reject(new Error(stderr)));});
  browser=await chromium.launch({headless:true,executablePath:chromium.executablePath(),args:['--no-sandbox']});
  const page=await browser.newPage({viewport:{width:1280,height:1000}}),errors=[];page.on('pageerror',e=>errors.push(e.message));
  await page.route('https://unpkg.com/esptool-js@0.7.0/bundle.js',route=>route.fulfill({contentType:'application/javascript',headers:{'Access-Control-Allow-Origin':'*'},path:new URL('serial-loader-fixture.js',import.meta.url).pathname}));
  await page.addInitScript(()=>{
   const serial=new EventTarget();
   const port={readable:null,writable:null,controller:null,signals:[],getInfo:()=>({usbVendorId:0x1a86}),
    async open(){if(this.readable)throw new Error('Port already open');this.readable=new ReadableStream({start:controller=>{this.controller=controller;controller.enqueue(new Uint8Array([82,69,65,68,89,13,10,0,255]));}});this.writable=new WritableStream();},
    async close(){try{this.controller?.close();}catch{}this.readable=null;this.writable=null;},async setSignals(value){this.signals.push(value);}};
   serial.requestPort=async()=>port;serial.getPorts=async()=>[port];window.__serialPort=port;window.__serial=serial;
   window.__wakeRequests=0;window.__wakeReleases=0;
   Object.defineProperty(navigator,'wakeLock',{value:{request:async()=>{window.__wakeRequests++;const lock=new EventTarget();lock.release=async()=>{window.__wakeReleases++;lock.dispatchEvent(new Event('release'));};return lock;}},configurable:true});
   Object.defineProperty(navigator,'serial',{value:serial,configurable:true});
  });
  await page.goto(url);assert.equal(await page.locator('#access-panel').isVisible(),false);
  await page.waitForFunction(()=>document.querySelector('#device-id').value==='board');
  await page.click('#connect');await page.waitForFunction(()=>document.querySelector('#usb-state').textContent==='connecté'||!document.querySelector('#client-state').textContent.startsWith('Connectez un port série'));
  assert.equal(await page.textContent('#usb-state'),'connecté',JSON.stringify({feedback:await page.textContent('#client-state'),errors}));
  await page.waitForFunction(()=>document.querySelector('#uart-log').textContent.includes('READY'));
  await page.waitForFunction(()=>document.querySelector('#agent-state').textContent==='Agent connecté');
  await page.waitForFunction(()=>!document.querySelector('[data-command="reset"]').disabled);
  await page.click('#format-hex');await page.waitForFunction(()=>document.querySelector('#uart-log').textContent.includes('00 ff'));
  await page.click('#pause');const frozen=await page.textContent('#uart-log');
  await page.evaluate(()=>window.__serialPort.controller.enqueue(new Uint8Array([42,43])));
  await page.waitForTimeout(50);assert.equal(await page.textContent('#uart-log'),frozen);
  await page.click('#pause');await page.waitForFunction(()=>document.querySelector('#uart-log').textContent.includes('2a 2b'));
  await page.click('#clear-display');const downloading=page.waitForEvent('download');await page.click('#export');
  const download=await downloading;assert.deepEqual(new Uint8Array(await readFile(await download.path())),new Uint8Array([82,69,65,68,89,13,10,0,255,42,43]));
  assert.equal(await page.locator('#agent-token').count(),0);assert.equal(await page.locator('#client-token').count(),0);
  assert.equal(await page.locator('#reserve').count(),0);
  assert.equal(await page.locator('#access-panel').isVisible(),false);
  assert.equal(await page.locator('#workspace-consoles').isVisible(),true);
  assert.equal(await page.locator('#workspace-operations').isVisible(),false);
  // Both tab groups support keyboard navigation; capture continues while operations are visible.
  await page.focus('#workspace-tab-consoles');await page.keyboard.press('ArrowRight');
  assert.equal(await page.locator('#workspace-operations').isVisible(),true);
  assert.equal(await page.locator('#workspace-consoles').isVisible(),false);
  await page.evaluate(()=>window.__serialPort.controller.enqueue(new Uint8Array([65,66,67,10])));
  await page.click('#workspace-tab-consoles');
  await page.waitForFunction(()=>document.querySelector('#uart-log').textContent.includes('41 42 43 0a'));
  await page.click('#workspace-tab-operations');
  await page.click('#tab-partitions');await page.click('[data-command="partitions"]');
  await page.waitForFunction(()=>document.querySelector('#partitions-body').textContent.includes('factory'));
  await page.waitForFunction(()=>!document.querySelector('#read').disabled);
  await page.waitForFunction(()=>window.__wakeRequests>0&&window.__wakeReleases>0);
  assert.ok((await page.textContent('#agent-log')).includes('partitions'));
  await page.click('#tab-memory');await page.fill('#address','0x10000');await page.fill('#size','64');await page.click('#read');
  await page.waitForFunction(()=>document.querySelector('#dump-state').textContent.includes('64 octets'));
  assert.ok((await page.textContent('#hex-dump')).includes('ff ff'));
  // A loader SerialFault must survive engine cleanup, WS, broker and UI translation.
  await page.evaluate(()=>{window.__readFault='closed';});await page.click('#read');
  await page.waitForFunction(()=>{const op=JSON.parse(document.querySelector('#result').textContent);return op.state==='failed'&&op.error?.code==='serial_disconnected';});
  assert.equal(JSON.parse(await page.textContent('#result')).error.phase,'flash_read');
  assert.equal(await page.locator('#workspace-operations').isVisible(),true);
  assert.equal(await page.locator('#panel-operations').isVisible(),true);
  assert.ok((await page.textContent('#client-state')).includes('Port série déconnecté'));
  await page.waitForFunction(()=>!document.querySelector('[data-command="console"]').disabled);
  await page.evaluate(()=>{window.__readFault=null;});await page.click('[data-command="console"]');
  await page.waitForFunction(()=>{const op=JSON.parse(document.querySelector('#result').textContent);return op.command==='console'&&op.state==='completed';});

  await page.click('#tab-flash');await page.setInputFiles('#binary',{name:'sample.bin',mimeType:'application/octet-stream',buffer:Buffer.from([1,2,3,4])});
  await page.waitForSelector('#images-body input');await page.locator('#images-body input').fill('0x20000');
  await page.click('#validate-plan');await page.waitForFunction(()=>document.querySelector('#plan-state').textContent.includes('images vérifiées'));
  await page.click('#write');await page.waitForSelector('#program-confirmation');assert.ok((await page.textContent('#confirmation-target')).includes('board'));
  assert.ok((await page.textContent('#confirmation-regions')).includes('0x00020004'));
  await page.click('#cancel-write');assert.equal(await page.locator('#program-confirmation').isVisible(),false);
  await page.click('#workspace-tab-consoles');
  // Local UART TX uses only this operation's authority; it must not open a remote grant.
  await page.waitForFunction(()=>!document.querySelector('#send').disabled);
  await page.fill('#uart','00 ff');await page.click('#send');
  await page.waitForFunction(()=>{const op=JSON.parse(document.querySelector('#result').textContent);return op.command==='uart.write'&&op.state==='completed';});
  await page.waitForFunction(async()=>{const r=await fetch('/api/v1/devices');return (await r.json()).devices[0].reservation===null;});
  assert.equal(await page.locator('#revoke').isVisible(),false);
  const localState=await page.evaluate(async()=>{const r=await fetch('/api/v1/devices');return (await r.json()).devices[0];});
  assert.equal(localState.privileged_seconds,0);
  assert.equal(await page.textContent('#usb-state'),'connecté');
  // Physical agent can release an orphan reservation without restarting the server.
  const orphan=await fetch(url+'/api/v1/leases',{method:'POST',headers:{Authorization:'Bearer client-bob-1234567890','Content-Type':'application/json'},body:JSON.stringify({device:'board',ttl:60})});
  assert.equal(orphan.status,201);
  await page.waitForFunction(()=>!document.querySelector('#release-control').disabled);
  page.once('dialog',dialog=>dialog.accept());await page.click('#release-control');
  await page.waitForFunction(()=>document.querySelector('#agent-log').textContent.includes('Pilotage libéré'));
  const released=await fetch(url+'/api/v1/devices',{headers:{Authorization:'Bearer client-alice-123456789'}});
  assert.equal((await released.json()).devices.find(d=>d.id==='board').reservation,null);
  // USB return reconnects only the selected port, with a fresh agent generation.
  const generation=await page.evaluate(async()=>{const r=await fetch('/api/v1/devices',{headers:{Authorization:'Bearer client-alice-123456789'}});return (await r.json()).devices[0].generation;});
  await page.evaluate(async()=>{const port=window.__serialPort;port.controller.error(new DOMException('Unplugged','NetworkError'));await port.close();const event=new Event('disconnect');Object.defineProperty(event,'port',{value:port});window.__serial.dispatchEvent(event);});
  await page.waitForFunction(()=>document.querySelector('#usb-state').textContent==='non connecté');
  await page.waitForFunction(async old=>{const r=await fetch('/api/v1/devices',{headers:{Authorization:'Bearer client-alice-123456789'}});const d=(await r.json()).devices[0];return d.connected&&d.generation!==old;},generation);
  try { await page.waitForFunction(()=>document.querySelector('#usb-state').textContent==='connecté'); }
  catch(error) { throw new Error(JSON.stringify({feedback:await page.textContent('#client-state'),journal:await page.textContent('#agent-log'),errors}),{cause:error}); }
  assert.ok((await page.textContent('#agent-log')).includes('Reconnexion de la carte'));
  // Following the tail is explicit and history selection never loses captured bytes.
  await page.click('#format-text');
  await page.evaluate(()=>{for(let i=0;i<60;i++)window.__serialPort.controller.enqueue(new TextEncoder().encode('line '+i+'\n'));});
  await page.waitForFunction(()=>document.querySelector('#uart-log').textContent.includes('line 59'));
  await page.locator('#uart-log').evaluate(node=>{node.scrollTop=0;node.dispatchEvent(new Event('scroll'));});
  await page.waitForSelector('#follow',{state:'visible'});
  await page.click('#workspace-tab-operations');
  await page.evaluate(()=>window.__serialPort.controller.enqueue(new TextEncoder().encode('captured while hidden\n')));
  await page.click('#workspace-tab-consoles');
  await page.waitForFunction(()=>document.querySelector('#uart-log').textContent.includes('captured while hidden'));
  assert.equal(await page.locator('#follow').isVisible(),true);
  assert.equal(await page.locator('#uart-log').evaluate(node=>node.scrollTop),0);
  await page.click('#follow');
  await page.waitForFunction(()=>document.querySelector('#follow').hidden);
  // Text rows follow firmware newlines, never arbitrary USB read boundaries.
  await page.evaluate(()=>{for(const text of ['I','NFO - board connected, ip = 192','.','168.1.2\r','\n'])window.__serialPort.controller.enqueue(new TextEncoder().encode(text));});
  await page.waitForFunction(()=>[...document.querySelectorAll('#uart-log .payload')].some(node=>node.textContent==='INFO - board connected, ip = 192.168.1.2'));
  // A narrow viewport must retain the console and put dense tables in their own horizontal scroller.
  await page.setViewportSize({width:360,height:900});assert.equal(await page.evaluate(()=>document.documentElement.scrollWidth>innerWidth),false);
  // Context help is available without persistent prose, by keyboard and pointer.
  await page.focus('[aria-describedby="help-commandes"]');
  assert.equal(await page.locator('#help-commandes').isVisible(),true);
  await page.keyboard.press('Escape');assert.equal(await page.locator('#help-commandes').isVisible(),false);
  await page.locator('[aria-describedby="help-commandes"]').hover();
  assert.equal(await page.locator('#help-commandes').isVisible(),true);
  assert.equal(await page.evaluate(()=>document.documentElement.scrollWidth>innerWidth),false);
  await page.locator('#workspace-tab-operations').click();
  assert.equal(await page.evaluate(()=>document.documentElement.scrollWidth>innerWidth),false);
  await page.click('#workspace-tab-consoles');
  // A persisted uncertain outcome survives automatic reconnect; no API work is replayed.
  let replayed=0;page.on('request',r=>{if(r.method()==='POST'&&r.url().endsWith('/api/v1/operations'))replayed++;});
  await page.evaluate(async()=>{localStorage.setItem('bench.v1.hold.board','1');const port=window.__serialPort;port.controller.error(new DOMException('Unplugged','NetworkError'));await port.close();const e=new Event('disconnect');Object.defineProperty(e,'port',{value:port});window.__serial.dispatchEvent(e);});
  await page.waitForFunction(()=>document.querySelector('#usb-state').textContent==='non connecté');
  await page.waitForFunction(()=>document.querySelector('#usb-state').textContent==='connecté');
  await page.waitForFunction(async()=>{const r=await fetch('/api/v1/devices',{headers:{Authorization:'Bearer client-alice-123456789'}});return (await r.json()).devices[0].quarantined;});
  assert.equal(await page.textContent('#device-mode'),'loader');assert.equal(replayed,0);
  assert.deepEqual(errors,[]);
 } finally {await browser?.close();proc.kill('SIGTERM');lines.close();}
});

test('automatic browser session and manual API work through a real HTTP and WebSocket port forward',async()=>{
 const proc=spawn('python3',['tests/browser_fixture.py','--forwarded'],{cwd:new URL('..',import.meta.url),stdio:['ignore','pipe','pipe']});
 let stderr='';proc.stderr.on('data',b=>stderr+=b);const lines=createInterface({input:proc.stdout});let browser;
 try {
  const url=await new Promise((resolve,reject)=>{lines.once('line',s=>resolve(JSON.parse(s).url));proc.once('exit',()=>reject(Error(stderr)));});
  browser=await chromium.launch({headless:true,executablePath:chromium.executablePath(),args:['--no-sandbox']});
  const page=await browser.newPage(),errors=[];page.on('pageerror',e=>errors.push(e.message));await page.goto(url);
  await page.waitForFunction(()=>document.querySelector('#server-state').textContent==='session ouverte');
  assert.equal(await page.locator('#agent-token').count(),0);assert.equal(await page.locator('#client-token').count(),0);
  await page.evaluate(async()=>{
   const {Agent}=await import('/agent.js');
   const engine={hold:false,persistHold(){},setHold(v){this.hold=v;},async execute(name,args,context){context.check();return {simulated:true,command:name};}};
   window.forwardedAgent=new Agent({engine,device:{id:'board',chip:'esp32s3',flash_size:4194304,capabilities:['identify']}});
   window.forwardedAgent.connect();
  });
  await page.waitForFunction(()=>window.forwardedAgent.generation);
  await page.click('#refresh');await page.waitForFunction(()=>!document.querySelector('[data-command="identify"]').disabled);
  await page.click('[data-command="identify"]');
  await page.waitForFunction(()=>{const op=JSON.parse(document.querySelector('#result').textContent);return op.state==='completed'&&op.result?.simulated;});
  await page.waitForFunction(async()=>{const r=await fetch('/api/v1/devices');const d=(await r.json()).devices[0];return d.reservation===null;});
  assert.equal(await page.locator('#reserve').count(),0);assert.deepEqual(errors,[]);
 } finally {await browser?.close();proc.kill('SIGTERM');lines.close();}
});
