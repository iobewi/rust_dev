import { Agent } from './agent.js';
import { EspEngine } from './esp-engine.js';
import { BenchClient } from './api-client.js';
import { b64, unb64, manifestPlan } from './protocol.js';
const $ = id => document.getElementById(id);
let agent, engine, device, lease, current, uploaded, cursor = 0, epoch, imagePlan = [], capture = [], captureSize = 0;
const uartDecoder = new TextDecoder();
const client = () => new BenchClient($('client-token').value);
const safe = fn => async () => { try { await fn(); } catch(e) { $('client-state').textContent = e.message; } };
function hook(id, fn) { $(id).onclick = safe(fn); }
function json(value) { $('result').textContent = JSON.stringify(value, null, 2); }
async function attach(port) {
  if (engine) throw new Error('Libérez le port en rechargeant la page avant de changer de carte');
  const deps = await import('https://unpkg.com/esptool-js@0.7.0/bundle.js');
  const key = 'bench.v1.hold.' + $('device-id').value;
  const persist = value => localStorage.setItem(key, value ? '1' : '0');
  engine = new EspEngine(port, deps, (type, data) => {
    agent?.event(type, data);
    if (type === 'uart') {
      const bytes = unb64(data.data); capture.push(bytes); captureSize += bytes.length;
      while (captureSize > 1024 * 1024) captureSize -= capture.shift().length;
      $('uart-log').textContent = ($('uart-log').textContent + uartDecoder.decode(bytes, { stream: true })).slice(-65536);
    }
  }, persist);
  engine.hold = localStorage.getItem(key) === '1';
  // Identification is read-only; an interrupted previous flash must never reopen the console.
  device = { id: $('device-id').value, ...await engine.identity(), capabilities: engine.capabilities(), http_origins: $('http-origins').value.split(',').map(s=>s.trim()).filter(Boolean).map(s=>new URL(s).origin) };
  if (!engine.hold) await engine.openConsole();
  connectAgent();
}
function connectAgent() {
  if (!engine || engine.busy || agent?.socket?.readyState === 1) throw new Error('Agent déjà connecté ou matériel occupé');
  agent = new Agent({ engine, device, token: $('agent-token').value, onState: s => { $('agent-state').textContent = s; } });
  agent.connect();
}
hook('connect', async () => { if (!navigator.serial) throw new Error('Web Serial indisponible dans ce contexte'); await attach(await navigator.serial.requestPort()); });
hook('reconnect', async () => { const ports = await navigator.serial.getPorts(); if (ports.length !== 1) throw new Error('Choisissez explicitement le port USB'); await attach(ports[0]); });
hook('agent-connect', connectAgent);
hook('grant', () => { if (confirm('Autoriser temporairement programmation, effacement, émission UART et déclencheurs distants ?')) agent.grant(Number($('grant-time').value)); });
hook('revoke', () => agent.grant(0));
hook('recover', () => { if (confirm('Avez-vous relu la flash et vérifié l’état matériel ? Lever le blocage autorise de nouveaux resets.')) agent.recover(); });
hook('refresh', async () => {
  const selected = lease?.device || $('devices').value;
  const data = await client().request('devices'); $('devices').replaceChildren();
  for (const d of data.devices) { const o = document.createElement('option'); o.value = d.id; o.textContent = `${d.id} · ${d.chip} · ${d.connected ? 'connecté' : 'absent'}${d.quarantined ? ' · bloqué' : ''}`; o.dataset.chip = d.chip; $('devices').append(o); }
  $('devices').value = selected;
  if (!$('devices').value && !lease && $('devices').options.length) $('devices').selectedIndex = 0;
  $('devices').disabled = Boolean(lease);
  json(data);
});
hook('reserve', async () => {
  if (lease) throw new Error('Libérez la réservation active avant de changer de carte');
  const deviceId = $('devices').value, family = $('devices').selectedOptions[0]?.dataset.chip;
  const reserved = await client().request('leases', { method:'POST', data:{ device:deviceId, ttl:900 } });
  lease = { ...reserved, chip: family };
  $('devices').value = lease.device; $('devices').disabled = true; cursor=0;
  $('lease-state').textContent='Réservation : '+lease.device+' · '+lease.id;
});
hook('release', async () => { await client().request('leases/'+lease.id, {method:'DELETE'}); lease=null; $('devices').disabled=false; $('lease-state').textContent='Libérée.'; });
function reservedDevice() {
  if (!lease) throw new Error('Réservez une carte');
  if ($('devices').value !== lease.device) throw new Error('La carte sélectionnée diffère de la carte réservée');
  return lease;
}
async function submit(command, args={}, destructive=false) {
  const target = reservedDevice();
  const op = await client().request('operations', { method:'POST', data:{ lease:target.id, command, args, allow_destructive:destructive } });
  current=op.id; json(op); await poll(op.id);
}
async function poll(id) {
  for (;;) {
    const op=await client().request('operations/'+id); json(op);
    if (['completed','failed','expired','cancelled','indeterminate'].includes(op.state)) {
      const artifact=op.result?.artifact;
      if (artifact) {
        const a=document.createElement('button'); a.textContent='Télécharger la relecture · SHA-256 '+artifact.sha256;
        a.onclick=safe(async()=>{
          const r=await fetch('/api/v1/artifacts/'+artifact.id,{headers:{Authorization:'Bearer '+$('client-token').value}});
          if(!r.ok)throw new Error('Relecture indisponible'); save(await r.blob(),artifact.id+'.bin');
        }); $('download').replaceChildren(a);
      }
      return op;
    }
    await new Promise(r=>setTimeout(r,300));
  }
}
function chip(){return reservedDevice().chip;}
function addr(){const n=Number($('address').value);if(!Number.isSafeInteger(n))throw new Error('Adresse invalide');return n;}
document.querySelectorAll('[data-command]').forEach(b=>b.onclick=safe(()=>submit(b.dataset.command)));
hook('read',()=>submit('flash.read',{address:addr(),size:Number($('size').value)}));
hook('upload',async()=>{const f=$('binary').files[0];if(!f)throw new Error('Choisissez un binaire'); uploaded=await client().upload(new Uint8Array(await f.arrayBuffer())); json(uploaded);});
hook('add-image',()=>{if(!uploaded)throw new Error('Envoyez un binaire');imagePlan.push({artifact:uploaded.id,sha256:uploaded.sha256,address:addr()});$('plan-state').textContent=JSON.stringify(imagePlan);});
let artifactList=[];
hook('artifacts',async()=>{artifactList=(await client().request('artifacts')).artifacts;$('artifact-list').replaceChildren();for(const a of artifactList){const o=document.createElement('option');o.value=a.id;o.textContent=a.id+' · '+a.size+' o · '+a.sha256.slice(0,12);$('artifact-list').append(o);}});
hook('select-artifact',()=>{uploaded=artifactList.find(a=>a.id===$('artifact-list').value);json(uploaded);});
hook('disarm',()=>submit('trigger',{action:'disarm'}));
hook('http',()=>submit('http.request',{url:$('http-url').value,method:'GET'}));
hook('clear-plan',()=>{imagePlan=[];$('plan-state').textContent='Plan vide.';});
hook('write',async()=>{if(confirm('Programmer les régions sélectionnées ?'))await submit('flash.write',{chip:chip(),images:imagePlan,return_console:true},true);});
hook('erase',async()=>{if(confirm('Effacer TOUTE la flash ?'))await submit('flash.erase',{chip:chip(),return_console:false},true);});
hook('install',async()=>{
  const m=$('manifest').files[0];if(!m)throw new Error('Choisissez un manifeste');const files=new Map();
  for(const f of [...$('manifest-files').files,...$('manifest-directory').files]){
    const path=f.webkitRelativePath ? f.webkitRelativePath.split('/').slice(1).join('/') : f.name;
    if(files.has(path))throw new Error('Chemin de manifeste dupliqué : '+path);
    files.set(path,await client().upload(new Uint8Array(await f.arrayBuffer())));
  }
  const plan=manifestPlan(JSON.parse(await m.text()),chip(),files);if(confirm('Programmer ce manifeste sans effacement global ?'))await submit('flash.write',plan,true);
});
hook('send',()=>{const text=$('uart').value.replace(/\s/g,'');if(!/^(?:[a-f0-9]{2})+$/i.test(text))throw new Error('Octets hexadécimaux attendus');return submit('uart.write',{data:b64(Uint8Array.from(text.match(/../g),n=>parseInt(n,16)))});});
hook('configure',()=>submit('uart.configure',{baudrate:Number($('baud').value)}));
hook('mark',()=>submit('mark',{label:$('marker').value}));
hook('io0',()=>submit('io0',{duration_ms:Number($('io0-duration').value)}));
hook('trigger',()=>submit('trigger',{data:b64(new TextEncoder().encode($('trigger-text').value)),action:$('trigger-action').value,delay_ms:0,expires_ms:30000}));
hook('cancel',async()=>json(await client().request('operations/'+current,{method:'DELETE'})));
hook('events',async()=>{
  const data=await client().request('devices/'+$('devices').value+'/events?after='+cursor);
  if(epoch&&epoch!==data.epoch)$('client-state').textContent='Serveur redémarré : rupture du flux.';
  if(data.lost)$('client-state').textContent='Événements perdus : '+data.lost;
  epoch=data.epoch;cursor=data.next;json(data);
});
function save(blob,name){const url=URL.createObjectURL(blob),a=document.createElement('a');a.href=url;a.download=name;a.click();setTimeout(()=>URL.revokeObjectURL(url),30000);}
hook('export',()=>save(new Blob(capture,{type:'application/octet-stream'}),'uart-buffer.bin'));
navigator.serial?.addEventListener('disconnect',ev=>{
  if(ev.target!==engine?.port)return;
  if(agent?.active)engine.setHold(true);
  engine.transport?.abort('closed','USB disconnected');agent?.socket?.close();$('agent-state').textContent='USB déconnecté. Reconnexion explicite requise.';
});
