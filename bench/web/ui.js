import { HardwareError, diagnostic } from './hardware-errors.js';
import { WorkWakeLock } from './wake-lock.js';
import { SerialReconnect } from './serial-reconnect.js';
import { AgentJournal } from './agent-journal.js';
import { Agent } from './agent.js';
import { EspEngine } from './esp-engine.js';
import { BenchClient } from './api-client.js';
import { b64, unb64, manifestPlan } from './protocol.js';
import { ConsoleBuffer } from './console-buffer.js';
import { address, hex, flashPlan, requireTarget } from './ui-model.js';

const $ = id => document.getElementById(id);
const terminal = new Set(['completed', 'failed', 'expired', 'cancelled', 'indeterminate']);
const safeCommands = new Set(['identify', 'partitions', 'flash.read', 'mark']);
const actions = { read: 'flash.read', write: 'flash.write', erase: 'flash.erase', send: 'uart.write',
  configure: 'uart.configure', io0: 'io0', mark: 'mark', trigger: 'trigger', disarm: 'trigger', http: 'http.request' };
let authorizing = false;
let agent, engine, localDevice, portConnected = false, lease, current, working = false;
let knownDevices = [], imagePlan = [], artifactList = [], confirmation;
let format = 'text', paused = false, pausedAt = Infinity, renderPending = false, selectedOperation, lastConsoleKey;
let streamController, streamGeneration = 0, refreshPromise, sessionOpened = false;
const manualHistory = new Set();
const captures = new Map(), cursors = new Map(), operationLog = new Map(), partitionCache = new Map();
const api = () => new BenchClient($('client-token').value);
const selected = () => $('devices').value;
const info = id => knownDevices.find(d => d.id === (id || selected()));
const capture = id => { if (!captures.has(id)) captures.set(id, new ConsoleBuffer()); return captures.get(id); };
const feedback = message => { $('client-state').textContent = message; };
const errorText = error => ({ unauthenticated: 'Accès refusé : vérifiez les tokens de session', forbidden: 'Droits API insuffisants',
  grant_required: 'Autorisation locale temporaire requise sur l’agent de cette carte', device_reserved: 'Carte déjà pilotée par un autre client',
  device_quarantined: 'Carte bloquée : relisez la flash avant la récupération opérateur', lease_expired: 'Pilotage expiré : reprenez la main',
  device_unavailable: 'Carte indisponible', flash_overlap: 'Les secteurs d’effacement se chevauchent' }[error.message] || diagnostic(error.message));
const safely = fn => async () => { try { await fn(); } catch (error) {
  if (error.message === 'lease_expired') { lease = null; confirmation = null; $('program-confirmation').hidden = true; }
  feedback(errorText(error)); renderState();
} };
const hook = (id, fn) => { $(id).onclick = safely(fn); };
const json = value => { $('result').textContent = JSON.stringify(value, null, 2); };
$('server-origin').textContent = location.origin;

const journal = new AgentJournal();
function logAgent(event) {
  journal.append(event);
  const body = $('agent-log'), follow = body.scrollHeight - body.clientHeight - body.scrollTop < 30;
  body.replaceChildren();
  for (const row of journal.rows) {
    const line = document.createElement('div'); line.className = 'journal-row';
    const time = document.createElement('time'); time.textContent = new Date(row.at).toLocaleTimeString('fr-FR', { hour12: false });
    const text = document.createElement('span'); text.textContent = row.text; line.append(time, text); body.append(line);
  }
  if (follow) body.scrollTop = body.scrollHeight;
}
hook('agent-log-clear', () => { journal.rows = []; $('agent-log').replaceChildren(); });
hook('agent-log-toggle', () => { const open = $('agent-log').hidden; $('agent-log').hidden = !open; $('agent-log-toggle').textContent = open ? 'Réduire' : 'Développer'; $('agent-log-toggle').setAttribute('aria-expanded', String(open)); });
const wake = new WorkWakeLock({ wakeLock: navigator.wakeLock, visible: () => document.visibilityState === 'visible', onState: state => { $('wake-state').textContent = state; } });
const activity = () => wake.update(Boolean(portConnected && (engine?.busy || agent?.active || agent?.awaitingResult)));
document.addEventListener('visibilitychange', () => wake.update(wake.active, true));
const reconnect = new SerialReconnect({ ports: () => navigator.serial.getPorts(), idle: () => !working && !engine?.busy && !agent?.active && agent?.socket?.readyState !== 1,
  attach: (port, identity) => attach(port, identity), state: stage => { logAgent({ stage });
    feedback(stage === 'identity_changed' ? diagnostic('physical_identity_changed') : stage === 'usb_connected' ? 'Carte reconnectée. Aucune opération rejouée.' : 'Reconnexion automatique de la carte…');
  } });

function tab(name) {
  document.querySelectorAll('[data-tab]').forEach(button => button.setAttribute('aria-selected', String(button.dataset.tab === name)));
  document.querySelectorAll('.tab-panel').forEach(panel => { panel.hidden = panel.id !== 'panel-' + name; });
}
document.querySelectorAll('[data-tab]').forEach(button => { button.onclick = () => tab(button.dataset.tab); });
function closeAccess() { $('access-panel').hidden = true; $('access-toggle').setAttribute('aria-expanded', 'false'); }
function openAccess() { $('access-panel').hidden = false; $('access-toggle').setAttribute('aria-expanded', 'true'); }
hook('access-toggle', () => { const open = $('access-panel').hidden; $('access-panel').hidden = !open; $('access-toggle').setAttribute('aria-expanded', String(open)); });

function append(kind, message, id = selected(), at) {
  if (!id) return;
  capture(id).note(kind, message, at); scheduleConsole();
}
function receive(bytes, id, at) { capture(id).append(bytes, { at }); scheduleConsole(); }
function scheduleConsole() {
  if (renderPending || paused) return;
  renderPending = true;
  requestAnimationFrame(() => { renderPending = false; if (!paused) renderConsole(); followState(); });
}
function renderConsole() {
  const id = selected(), body = $('uart-log');
  const previousScroll = body.scrollTop, follow = body.scrollHeight - body.clientHeight - body.scrollTop < 30;
  if (!id || !captures.has(id)) {
    if (lastConsoleKey === 'empty:' + id) return;
    lastConsoleKey = 'empty:' + id; body.replaceChildren();
    const empty = document.createElement('div'); empty.className = 'console-empty';
    empty.textContent = 'Aucun octet reçu. Connectez un port Web Serial ou observez une carte du banc.'; body.append(empty);
    $('capture-state').textContent = 'Aucune capture'; return;
  }
  const buffer = capture(id), timestamps = $('timestamps').checked;
  $('capture-state').textContent = `${buffer.byteLength} octets conservés` + (buffer.droppedBytes || buffer.droppedEvents ? ` · tampon : ${buffer.droppedBytes} octets / ${buffer.droppedEvents} événements évincés` : '');
  const key = JSON.stringify([id, Math.min(buffer.sequence, paused ? pausedAt : Infinity), buffer.displayAfter, format, $('console-search').value, timestamps]);
  if (key === lastConsoleKey) return;
  lastConsoleKey = key; body.replaceChildren();
  for (const event of buffer.visible({ format, query: $('console-search').value, before: paused ? pausedAt : Infinity })) {
    const row = document.createElement('div'); row.className = 'console-row' + (timestamps ? '' : ' no-time') + (event.kind === 'REP' ? ' mark' : '');
    const cells = [['span', 'seq', event.seq], ...(timestamps ? [['time', '', new Date(event.at).toLocaleTimeString('fr-FR', { hour12: false }) + '.' + String(new Date(event.at).getMilliseconds()).padStart(3, '0')]] : []), ['span', 'kind', event.kind], ['span', 'payload', event.display]];
    for (const [tagName, className, value] of cells) { const cell = document.createElement(tagName); cell.className = className; cell.textContent = value; row.append(cell); }
    body.append(row);
  }
  if (!body.children.length) { const empty = document.createElement('div'); empty.className = 'console-empty'; empty.textContent = 'Aucune entrée affichée. La capture brute est conservée.'; body.append(empty); }
  body.scrollTop = follow ? body.scrollHeight : previousScroll; followState();
}
function followState() {
  const body = $('uart-log'), following = body.scrollHeight - body.clientHeight - body.scrollTop < 30;
  $('follow-state').textContent = paused ? 'Affichage en pause · capture active' : following ? 'Suivi du direct' : 'Lecture de l’historique · capture active';
  $('follow').hidden = !paused && following;
}
$('uart-log').addEventListener('scroll', followState);
hook('follow', () => { paused = false; pausedAt = Infinity; $('pause').textContent = 'Pause affichage'; renderConsole(); $('uart-log').scrollTop = $('uart-log').scrollHeight; followState(); });
hook('format-text', () => { format = 'text'; $('format-text').setAttribute('aria-pressed', 'true'); $('format-hex').setAttribute('aria-pressed', 'false'); renderConsole(); });
hook('format-hex', () => { format = 'hex'; $('format-text').setAttribute('aria-pressed', 'false'); $('format-hex').setAttribute('aria-pressed', 'true'); renderConsole(); });
$('console-search').oninput = renderConsole; $('timestamps').onchange = renderConsole;
hook('pause', () => { paused = !paused; pausedAt = paused && selected() ? capture(selected()).sequence : Infinity; $('pause').textContent = paused ? 'Reprendre l’affichage' : 'Pause affichage'; if (!paused) renderConsole(); followState(); });
hook('clear-display', () => { if (selected()) capture(selected()).clearDisplay(); renderConsole(); feedback('Affichage effacé ; les octets capturés sont conservés.'); });
function save(blob, filename) { const url = URL.createObjectURL(blob), link = document.createElement('a'); link.href = url; link.download = filename; link.click(); setTimeout(() => URL.revokeObjectURL(url), 30000); }
hook('export', () => { if (!selected()) throw new Error('Aucune carte sélectionnée'); save(new Blob([capture(selected()).bytes()], { type: 'application/octet-stream' }), 'uart-buffer.bin'); });
hook('export-events', () => { if (!selected()) throw new Error('Aucune carte sélectionnée'); const buffer = capture(selected()); save(new Blob([JSON.stringify({ device: selected(), dropped_bytes: buffer.droppedBytes, dropped_events: buffer.droppedEvents,
  events: buffer.events.map(({ bytes, ...rest }) => ({ ...rest, data: b64(bytes) })) }, null, 2)], { type: 'application/json' }), 'hardware-events.json'); });

function renderDevices() {
  const previous = lease?.device || selected();
  const devices = [...knownDevices];
  if (localDevice && !devices.some(d => d.id === localDevice.id)) devices.push({ ...localDevice, connected: portConnected, localOnly: true });
  $('devices').replaceChildren();
  for (const d of devices) { const option = document.createElement('option'); option.value = d.id; option.textContent = `${d.id} · ${d.chip}${d.connected ? '' : ' · absent'}`; $('devices').append(option); }
  $('devices').value = previous;
  if (!selected() && !lease && devices.length) $('devices').value = localDevice?.id || devices[0].id;
  renderState(); renderPartitions(); renderConsole();
}
function renderState() {
  if (lease && lease.localUntil <= performance.now() && !working) { lease = null; confirmation = null; $('program-confirmation').hidden = true; }
  const d = info() || (localDevice?.id === selected() ? localDevice : null);
  const local = localDevice && selected() === localDevice.id;
  const blocked = d?.quarantined || local && engine?.hold;
  const ownBusy = local && (engine?.busy || agent?.active || agent?.awaitingResult);
  const capability = new Set(d?.capabilities || []);
  const base = Boolean(lease && d?.connected && !working && !ownBusy);
  document.querySelectorAll('[data-hardware]').forEach(button => { const command = button.dataset.command || actions[button.id]; button.disabled = !base || !capability.has(command) || Boolean(blocked && !safeCommands.has(command)); });
  $('devices').disabled = Boolean(lease || working);
  $('reserve').textContent = manualHistory.has(selected()) ? 'Reprendre la main' : 'Prendre la main';
  $('reserve').hidden = Boolean(lease); $('release').hidden = !lease;
  $('reserve').disabled = working || !info()?.connected || Boolean(d?.reservation && !lease);
  $('release').disabled = working || Boolean(ownBusy);
  $('cancel').disabled = !working || !current;
  $('usb-state').textContent = portConnected ? 'connecté' : 'non connecté';
  $('device-mode').textContent = local ? engine?.mode || '—' : d?.connected ? 'Agent distant' : '—';
  $('chip-info').textContent = d?.chip || '—'; $('flash-info').textContent = d?.flash_size ? `${hex(d.flash_size)} · ${d.flash_size} octets` : '—';
  $('rdid-info').textContent = local && localDevice.flash_id != null ? hex(localDevice.flash_id) : '—';
  $('interface-info').textContent = local ? engine?.native ? 'USB Serial/JTAG' : 'UART externe' : 'Agent du banc';
  $('signals-info').textContent = capability.has('reset') && capability.has('io0') ? 'Disponibles' : 'Non exposés';
  $('control-state').textContent = lease ? 'Pilotage manuel · vous' : d?.reservation ? 'Pilotée par : ' + d.reservation.owner : d?.connected && !d?.localOnly ? 'Disponible pour le pilotage distant' : 'Local · observation';
  if (lease) $('lease-state').textContent = `Pilotage manuel : ${lease.device}`;
  else $('lease-state').textContent = d?.reservation ? `Pilotée par : ${d.reservation.owner}` : 'Disponible pour le pilotage distant.';
  $('grant').disabled = !local || !agent?.generation; $('revoke').disabled = $('grant').disabled;
  $('grant-state').textContent = local && agent?.grantUntil > performance.now() ? `Opérations sensibles autorisées : ${Math.ceil((agent.grantUntil - performance.now()) / 1000)} s` : 'Opérations sensibles : verrouillées';
  $('revoke').hidden = !(local && agent?.grantUntil > performance.now());
  $('release-control').disabled = !local || !agent?.generation || Boolean(ownBusy || working) || !d?.reservation;
  activity();
  $('safety-state').textContent = local && agent?.awaitingResult ? 'Attente de l’accusé serveur' : blocked ? 'Blocage : récupération opérateur requise' : d ? 'Aucun blocage signalé' : 'Aucun état matériel connu';
  $('safety-state').classList.toggle('danger', Boolean(blocked));
  $('recover').disabled = !local || !agent?.generation || Boolean(ownBusy) || !blocked;
  $('disconnect').disabled = !portConnected || working || Boolean(lease || ownBusy);
  $('connect').disabled = working || Boolean(lease || portConnected);
  $('reconnect').disabled = $('connect').disabled;
  $('footer-target').textContent = selected() ? `${selected()} · ${$('control-state').textContent}` : 'Aucune carte sélectionnée';
}
$('devices').onchange = () => {
  if (lease && selected() !== lease.device) { $('devices').value = lease.device; feedback('La carte active reste liée à sa réservation.'); return; }
  confirmation = null; $('program-confirmation').hidden = true; $('download').replaceChildren(); $('hex-dump').textContent = 'Aucune donnée lue pour cette carte.';
  if (paused) pausedAt = capture(selected()).sequence;
  renderState(); renderPartitions(); renderConsole(); startStream();
};
async function refreshDevices(publish = true) {
  if (!$('client-token').value) return;
  if (!refreshPromise) {
    refreshPromise = (async () => {
      const data = await api().request('devices'); knownDevices = data.devices; sessionOpened = true;
      $('server-state').textContent = 'session ouverte'; renderDevices(); startStream(); return data;
    })();
  }
  const request = refreshPromise;
  try { const data = await request; if (publish) json(data); return data; }
  finally { if (refreshPromise === request) refreshPromise = null; }
}
hook('refresh', async () => { if (!$('client-token').value) { openAccess(); throw new Error('Ouvrez la session du banc pour lister les cartes'); } await refreshDevices(); });

async function startStream() {
  const id = selected(), token = $('client-token').value;
  if (!id || !token || !knownDevices.some(d => d.id === id)) {
    streamController?.abort(); streamController = null; streamGeneration++; return;
  }
  const key = id + ':' + token;
  if (streamController?.key === key && !streamController.signal.aborted) return;
  streamController?.abort(); const controller = new AbortController(); controller.key = key; streamController = controller;
  const generation = ++streamGeneration, cursor = cursors.get(id) || { next: 0, epoch: null };
  try {
    for await (const batch of new BenchClient(token).events(id, cursor.next, controller.signal)) {
      if (controller.signal.aborted || generation !== streamGeneration) break;
      if (cursor.epoch && cursor.epoch !== batch.epoch) {
        append('PERTE', 'Serveur redémarré : rupture de continuité', id); lease = null; confirmation = null; $('program-confirmation').hidden = true; cursor.next = 0; cursor.epoch = batch.epoch;
        controller.abort(); streamController = null; if (selected() === id) startStream(); break;
      }
      cursor.epoch = batch.epoch;
      if (batch.lost) append('PERTE', `${batch.lost} événements hors du tampon serveur`, id);
      for (const event of batch.events) {
        if (event.kind === 'uart') {
          if (event.source_lost) append('PERTE', `${event.source_lost} chunks perdus à la source`, id, event.at * 1000);
          if (!(portConnected && localDevice?.id === id && agent?.generation)) receive(unb64(event.data), id, event.agent_at || event.at * 1000);
        } else if (event.kind === 'mark') append('REP', event.detail, id, event.at * 1000);
        else if (['connected', 'disconnected', 'quarantined', 'recovered'].includes(event.kind)) append('SYS', event.kind, id, event.at * 1000);
      }
      cursor.next = batch.next; cursors.set(id, cursor);
    }
  } catch (error) {
    if (!controller.signal.aborted) { append('PERTE', 'Abonnement interrompu : ' + errorText(error), id); controller.abort(); }
  }
}

async function attach(port, expectedIdentity = null) {
  if (working || lease || agent?.active || engine?.busy || agent?.socket?.readyState === 1) throw new Error('Terminez le pilotage actif avant de changer de port');
  if (agent?.awaitingResult) engine.setHold(true);
  if (agent?.socket) { agent.socket.onclose = null; agent.socket.close(); }
  agent = null;
  if (engine) { try { await engine.close(); } finally { engine = null; } }
  const logicalId = $('device-id').value.trim();
  if (!logicalId) throw new Error('Identifiant de carte vide');
  const held = localStorage.getItem('bench.v1.hold.' + logicalId) === '1';
  const deps = await import('https://unpkg.com/esptool-js@0.7.0/bundle.js');
  const hardware = new EspEngine(port, deps, (type, event) => {
    agent?.event(type, event);
    if (type === 'uart') receive(unb64(event.data), logicalId, event.at);
    else if (type === 'transport') {
      append('SYS', event.detail, logicalId);
      if (event.detail === 'opening console') logAgent({ stage: 'console' });
      if (event.detail === 'opening loader') logAgent({ stage: 'loader' });
    }
  }, hold => localStorage.setItem('bench.v1.hold.' + logicalId, hold ? '1' : '0'));
  hardware.hold = held; engine = hardware;
  try {
    const identity = await hardware.identity();
    if (expectedIdentity && ['chip', 'flash_size', 'flash_id', 'mac'].some(key => identity[key] !== expectedIdentity[key])) throw new HardwareError('physical_identity_changed');
    localDevice = { id: logicalId, ...identity, capabilities: hardware.capabilities(), connected: true, localOnly: true,
      http_origins: $('http-origins').value.split(',').map(s => s.trim()).filter(Boolean).map(s => new URL(s).origin) };
    if (!held) await hardware.openConsole();
    portConnected = true; $('device-id').disabled = true; renderDevices(); $('devices').value = logicalId;
    renderState(); renderConsole(); feedback('Port Web Serial connecté. Capture locale active ; ouvrez la session du banc pour le pilotage manuel ou distant.');
    if (!expectedIdentity) reconnect.remember(port, identity);
    logAgent({ stage: 'usb_connected' });
    if ($('agent-token').value) connectAgent();
  } catch (error) {
    try { await hardware.close(); } catch { /* The failed port remains offline. */ }
    engine = null; localDevice = null; portConnected = false; renderState(); throw error;
  }
}
function connectAgent() {
  if (!engine || !localDevice || !portConnected) throw new Error('Connectez d’abord un port Web Serial');
  if (!$('agent-token').value) { openAccess(); throw new Error('Renseignez l’accès agent pour le pilotage par API'); }
  if (engine.busy || agent?.active || [0, 1, 2].includes(agent?.socket?.readyState)) throw new Error('Agent déjà connecté ou matériel occupé');
  if (agent?.awaitingResult) engine.setHold(true);
  agent = new Agent({ engine, device: localDevice, token: $('agent-token').value, onLog: logAgent, onActivity: activity, onState: state => {
    $('agent-state').textContent = state === 'Agent connected' ? 'Agent connecté' : state.startsWith('Agent disconnected') ? 'Agent déconnecté · aucune commande rejouée' : state;
    if (!agent?.generation && lease?.device === localDevice.id) { lease = null; confirmation = null; $('program-confirmation').hidden = true; }
    $('server-state').textContent = agent?.generation ? 'agent connecté' : sessionOpened ? 'client connecté · agent absent' : 'agent non connecté';
    renderState();
    if (state === 'Agent connected') closeAccess();
    if (state === 'Pilotage libéré par l’opérateur') { lease = null; refreshDevices(false).catch(error => feedback(errorText(error))); }
    if (state === 'Agent connected' && $('client-token').value) {
      (async () => { await refreshDevices(false); if (!info(localDevice.id)) await refreshDevices(false); })().catch(error => feedback(errorText(error)));
    }
  } });
  agent.connect();
}
hook('connect', async () => { if (!navigator.serial) throw new Error('Web Serial indisponible dans ce navigateur ou ce contexte'); await attach(await navigator.serial.requestPort()); });
hook('reconnect', async () => { if (!navigator.serial) throw new Error('Web Serial indisponible'); const ports = await navigator.serial.getPorts(); if (ports.length !== 1) throw new Error('Choisissez explicitement le port série'); await attach(ports[0]); });
hook('disconnect', async () => {
  if (working || lease || agent?.active || agent?.awaitingResult) throw new Error('Terminez la réservation et l’opération avant de déconnecter le port');
  reconnect.stop(); agent?.socket?.close(); await engine.close(); portConnected = false; $('device-id').disabled = false; renderState(); append('SYS', 'Port fermé par l’opérateur', localDevice.id);
});
hook('agent-connect', connectAgent);
hook('session-open', async () => {
  if (!$('client-token').value && !$('agent-token').value) throw new Error('Renseignez au moins un accès de session');
  streamController?.abort(); streamController = null;
  if (portConnected && $('agent-token').value && !agent?.generation) connectAgent();
  if ($('client-token').value) { await refreshDevices(); closeAccess(); }
  feedback('Session du banc ouverte sur ' + location.origin + '.');
});
hook('grant', () => { if (!agent?.generation) throw new Error('Agent local non connecté'); if (confirm(`Autoriser les opérations privilégiées sur ${localDevice.id} pendant ${$('grant-time').selectedOptions[0].textContent} ?`)) { agent.grant(Number($('grant-time').value)); renderState(); } });
hook('revoke', () => { agent.grant(0); renderState(); });
hook('release-control', () => { if (confirm('Libérer le pilotage abandonné et révoquer l’autorisation locale ? Une opération en cours empêchera cette action.')) agent.releaseControl(); });
hook('recover', () => { if (confirm(`Après relecture et vérification de ${localDevice.id}, lever le blocage ? Cela autorisera de nouveaux resets.`)) agent.recover(); });

hook('reserve', async () => {
  if (!$('client-token').value) { openAccess(); throw new Error('Ouvrez la session du banc avant le pilotage manuel'); }
  if (lease) throw new Error('Terminez le pilotage manuel actif avant de changer de carte');
  const id = selected(), d = info(id);
  if (!id || !d) throw new Error('Choisissez une carte enregistrée sur le banc');
  const reserved = await api().request('leases', { method: 'POST', data: { device: id, ttl: 900 } });
  lease = { ...reserved, chip: d.chip, flash_size: d.flash_size, localUntil: performance.now() + 900000 };
  manualHistory.add(id);
  $('devices').value = lease.device; renderState(); confirmation = null; $('program-confirmation').hidden = true;
});
hook('release', async () => {
  if (working || agent?.active || agent?.awaitingResult) throw new Error('Attendez la fin de l’opération avant de changer de pilotage');
  await api().request('leases/' + lease.id, { method: 'DELETE' }); lease = null;
  await refreshDevices(false); feedback('Pilotage distant autorisé : carte disponible pour les clients API authentifiés.');
});
async function renewLease() { if (!lease || renewing || !sessionOpened || performance.now() < nextRenewal || performance.now() < lease.localUntil - 60000) return; renewing = true; nextRenewal = performance.now() + 30000; try { const target = lease; await api().request('leases/' + target.id, { method: 'PATCH', data: { ttl: 900 } }); if (lease === target) lease.localUntil = performance.now() + 900000; } catch (error) { feedback(errorText(error)); } finally { renewing = false; } }
let renewing = false, nextRenewal = 0;

function renderOperations() {
  const body = $('operations-body'); body.replaceChildren();
  for (const record of [...operationLog.values()].reverse()) {
    const row = document.createElement('tr');
    for (const value of [record.id.slice(0, 10), record.device, record.command, record.state, Math.round((record.finished || performance.now()) - record.started) + ' ms']) {
      const cell = document.createElement('td'); cell.textContent = value; cell.className = 'mono'; row.append(cell);
    }
    const cell = document.createElement('td'), button = document.createElement('button'); button.textContent = 'Inspecter';
    button.onclick = () => { selectedOperation = record.id; json(record.snapshot); };
    cell.append(button); row.append(cell); body.append(row);
  }
}
function recordOperation(op, started) {
  const previous = operationLog.get(op.id);
  operationLog.set(op.id, { ...previous, id: op.id, device: op.device, command: op.command, state: op.state, snapshot: op,
    started: previous?.started || started, finished: terminal.has(op.state) ? previous?.finished || performance.now() : null });
  while (operationLog.size > 200) operationLog.delete(operationLog.keys().next().value);
  renderOperations(); if (!selectedOperation || selectedOperation === op.id) json(op);
}
const privilegedCommands = new Set(['flash.write', 'flash.erase', 'uart.write', 'io0', 'trigger', 'http.request']);
async function authorizeIfNeeded(command, device) {
  if (!privilegedCommands.has(command) || localDevice?.id !== device || !agent?.generation || agent.grantUntil > performance.now()) return;
  const seconds = Number($('grant-time').value);
  if (!confirm(`Autoriser les opérations sensibles sur ${device} pendant ${$('grant-time').selectedOptions[0].textContent} pour ${command} ?`)) throw new Error('Autorisation annulée : aucune commande envoyée');
  agent.grant(seconds); renderState();
  // Do not submit before the broker has received the grant on its separate WS connection.
  const until = performance.now() + 5000;
  while (performance.now() < until) {
    await refreshDevices(false);
    if (info(device)?.privileged_seconds > 0) return;
    await new Promise(resolve => setTimeout(resolve, 100));
  }
  throw new Error('Autorisation non confirmée par le serveur : aucune commande envoyée');
}
async function submit(command, args = {}, destructive = false) {
  const target = requireTarget(lease, selected());
  if (working || authorizing) throw new Error('Une opération manuelle est déjà en cours');
  authorizing = true;
  try { await authorizeIfNeeded(command, target.device); } finally { authorizing = false; }
  requireTarget(lease, selected());
  working = true; current = null; renderState(); const started = performance.now(), client = api();
  try {
    let op = await client.request('operations', { method: 'POST', data: { lease: target.id, command, args, allow_destructive: destructive } });
    current = op.id; selectedOperation = op.id; recordOperation(op, started); renderState();
    for (;;) {
      if (terminal.has(op.state)) break;
      await new Promise(resolve => setTimeout(resolve, 250));
      op = await client.request('operations/' + op.id); recordOperation(op, started);
    }
    if (op.state !== 'completed') { feedback(`Opération ${op.state} : ${diagnostic(op.error?.code || 'inspectez le résultat')}`); tab('operations'); return op; }
    feedback(`${command} : terminé sur ${target.device}`);
    if (command === 'partitions' && op.result?.partitions) { partitionCache.set(target.device, op.result.partitions); renderPartitions(); }
    if (command === 'identify' && op.result?.chip) { const d = info(target.device); if (d) Object.assign(d, op.result); renderState(); }
    if (command === 'flash.read' && op.result?.artifact) await showDump(op.result.artifact, args.address, client);
    return op;
  } finally {
    working = false; current = null;
    await refreshDevices(false).catch(() => {}); renderState();
  }
}
document.querySelectorAll('[data-command]').forEach(button => { button.onclick = safely(() => submit(button.dataset.command,
  button.dataset.command === 'partitions' ? { address: address($('partition-address').value) } : {})); });
hook('cancel', async () => { const op = await api().request('operations/' + current, { method: 'DELETE' }); json(op); feedback(op.cancel_requested ? 'Annulation demandée ; interruption matérielle non confirmée.' : 'Commande annulée avant exécution.'); });

function renderPartitions() {
  const rows = partitionCache.get(selected()) || [], body = $('partitions-body'); body.replaceChildren();
  for (const part of rows) {
    const row = document.createElement('tr');
    for (const value of [part.label, hex(part.type) + ' / ' + hex(part.subtype), hex(part.address), hex(part.size), hex(part.address + part.size)]) {
      const cell = document.createElement('td'); cell.textContent = value; cell.className = 'mono'; row.append(cell);
    }
    const cell = document.createElement('td'), button = document.createElement('button'); button.textContent = 'Lire'; button.dataset.hardware = ''; button.dataset.command = 'flash.read';
    button.onclick = safely(() => { $('address').value = hex(part.address); $('size').value = hex(part.size); tab('memory'); return submit('flash.read', { address: part.address, size: part.size }); });
    cell.append(button); row.append(cell); body.append(row);
  }
  $('partition-state').textContent = rows.length ? `${rows.length} partitions lues sur ${selected()}` : 'Aucune table lue pour cette carte.';
  renderState();
}
async function showDump(artifact, offset, client) {
  const data = await client.download(artifact), lines = ['Offset      00 01 02 03 04 05 06 07 08 09 0a 0b 0c 0d 0e 0f'];
  for (let i = 0; i < Math.min(data.length, 256); i += 16) lines.push(hex(offset + i) + '  ' + [...data.slice(i, i + 16)].map(b => b.toString(16).padStart(2, '0')).join(' '));
  $('hex-dump').textContent = lines.join('\n');
  $('dump-state').textContent = `${data.length} octets · SHA-256 ${artifact.sha256} · aperçu ${Math.min(data.length, 256)} octets`;
  const button = document.createElement('button'); button.textContent = 'Télécharger .bin'; button.onclick = () => save(new Blob([data], { type: 'application/octet-stream' }), artifact.id + '.bin');
  $('download').replaceChildren(button); tab('memory');
}
hook('read', () => submit('flash.read', { address: address($('address').value), size: address($('size').value) }));

function invalidatePlan() { confirmation = null; $('program-confirmation').hidden = true; $('plan-state').textContent = 'Plan modifié · validation requise. Aucun effacement global.'; }
function renderImages() {
  const body = $('images-body'); body.replaceChildren();
  for (const entry of imagePlan) {
    const row = document.createElement('tr'), name = document.createElement('td'); name.textContent = entry.name; name.className = 'mono'; row.append(name);
    const addr = document.createElement('td'), input = document.createElement('input'); input.value = entry.address; input.placeholder = 'Adresse requise'; input.setAttribute('aria-label', 'Offset de ' + entry.name);
    input.oninput = () => { entry.address = input.value; invalidatePlan(); }; addr.append(input); row.append(addr);
    for (const value of [entry.size, entry.sha256]) { const cell = document.createElement('td'); cell.textContent = value; cell.className = 'mono digest'; row.append(cell); }
    const actions = document.createElement('td'), remove = document.createElement('button'); remove.textContent = 'Retirer'; remove.onclick = () => { imagePlan = imagePlan.filter(item => item !== entry); renderImages(); invalidatePlan(); }; actions.append(remove); row.append(actions); body.append(row);
  }
}
function validatePlan() {
  const target = requireTarget(lease, selected()), images = flashPlan(imagePlan, target.flash_size);
  $('plan-state').textContent = `${images.length} images vérifiées · secteurs disjoints · aucune écriture effectuée.`;
  return { target, images };
}
$('binary').onchange = safely(async () => {
  const files = [...$('binary').files]; if (imagePlan.length + files.length > 16) throw new Error('Maximum 16 images par plan');
  for (const file of files) {
    if (!file.size || file.size > 16 * 1024 * 1024) throw new Error('Binaire vide ou supérieur à 16 Mio');
    const artifact = await api().upload(new Uint8Array(await file.arrayBuffer()));
    imagePlan.push({ artifact: artifact.id, name: file.name, size: artifact.size, sha256: artifact.sha256, address: '' });
    renderImages(); invalidatePlan();
  }
  renderImages(); invalidatePlan(); tab('flash'); $('binary').value = '';
});
hook('clear-plan', () => { imagePlan = []; renderImages(); invalidatePlan(); });
hook('validate-plan', () => validatePlan());
hook('write', () => {
  const { target, images } = validatePlan(); confirmation = { lease: target.id, device: target.device, chip: target.chip, images };
  $('confirmation-target').textContent = `${target.device} · ${target.chip} · ${target.flash_size} octets`;
  const body = $('confirmation-regions'); body.replaceChildren();
  images.forEach((image, i) => { const row = document.createElement('tr'); for (const value of [imagePlan[i].name, hex(image.address), hex(image.address + imagePlan[i].size)]) { const cell = document.createElement('td'); cell.textContent = value; cell.className = 'mono'; row.append(cell); } body.append(row); });
  $('program-confirmation').hidden = false;
});
hook('cancel-write', () => { confirmation = null; $('program-confirmation').hidden = true; });
hook('confirm-write', async () => {
  const target = requireTarget(lease, selected());
  if (!confirmation || confirmation.lease !== target.id || confirmation.device !== selected()) throw new Error('Confirmez à nouveau le plan pour cette réservation');
  const plan = confirmation; confirmation = null; $('program-confirmation').hidden = true;
  await submit('flash.write', { chip: plan.chip, images: plan.images, erase: false, return_console: true }, true);
});
hook('erase', async () => { const target = requireTarget(lease, selected()); if (confirm(`Effacer TOUTE la flash de ${target.device} (${target.chip}, ${target.flash_size} octets) ?`)) await submit('flash.erase', { chip: target.chip, return_console: false }, true); });
hook('install', async () => {
  const target = requireTarget(lease, selected()), manifest = $('manifest').files[0]; if (!manifest) throw new Error('Choisissez un manifeste');
  const parsed = JSON.parse(await manifest.text()), files = new Map();
  for (const file of [...$('manifest-files').files, ...$('manifest-directory').files]) {
    const path = file.webkitRelativePath ? file.webkitRelativePath.split('/').slice(1).join('/') : file.name;
    if (files.has(path)) throw new Error('Chemin dupliqué : ' + path);
    if (!file.size || file.size > 16 * 1024 * 1024) throw new Error('Taille de fichier invalide');
    files.set(path, await api().upload(new Uint8Array(await file.arrayBuffer())));
  }
  const plan = manifestPlan(parsed, target.chip, files);
  imagePlan = plan.images.map(image => { const artifact = [...files.values()].find(item => item.id === image.artifact); return { ...image, name: [...files].find(([, value]) => value.id === image.artifact)?.[0] || image.artifact, size: artifact.size, address: hex(image.address) }; });
  renderImages(); invalidatePlan(); validatePlan(); feedback('Manifeste chargé dans le plan. Vérifiez les régions avant de programmer.');
});
hook('artifacts', async () => {
  artifactList = (await api().request('artifacts')).artifacts; $('artifact-list').replaceChildren();
  for (const item of artifactList) { const option = document.createElement('option'); option.value = item.id; option.textContent = `${item.id} · ${item.size} octets · ${item.sha256.slice(0, 12)}`; $('artifact-list').append(option); }
});
hook('select-artifact', () => {
  const item = artifactList.find(entry => entry.id === $('artifact-list').value); if (!item) throw new Error('Choisissez un artefact');
  imagePlan.push({ artifact: item.id, name: item.id + '.bin', sha256: item.sha256, size: item.size, address: '' }); renderImages(); invalidatePlan();
});
hook('send', () => {
  const value = $('uart').value; let bytes;
  if ($('uart-format').value === 'text') bytes = new TextEncoder().encode(value);
  else { const text = value.replace(/\s/g, ''); if (!/^(?:[a-f0-9]{2})+$/i.test(text)) throw new Error('Octets hexadécimaux attendus'); bytes = Uint8Array.from(text.match(/../g), n => parseInt(n, 16)); }
  if (!bytes.length) throw new Error('Aucun octet à émettre');
  return submit('uart.write', { data: b64(bytes) });
});
hook('configure', () => submit('uart.configure', { baudrate: address($('baud').value) }));
hook('mark', () => submit('mark', { label: $('marker').value }));
hook('io0', () => submit('io0', { duration_ms: address($('io0-duration').value) }));
hook('trigger', () => submit('trigger', { data: b64(new TextEncoder().encode($('trigger-text').value)), action: $('trigger-action').value,
  delay_ms: address($('trigger-delay').value), expires_ms: address($('trigger-expiry').value) * 1000 }));
hook('disarm', () => submit('trigger', { action: 'disarm' }));
hook('http', () => submit('http.request', { url: $('http-url').value, method: 'GET' }));
hook('events', async () => { const data = await api().request('devices/' + encodeURIComponent(selected()) + '/events'); json(data); tab('operations'); });

navigator.serial?.addEventListener('disconnect', event => {
  const port = event.port || event.target;
  if (!portConnected || port !== engine?.port) return;
  engine.disconnected = true;
  if (agent?.active || agent?.awaitingResult) engine.setHold(true);
  engine.cancelTrigger(); engine.transport?.abort('closed', 'USB disconnected'); agent?.socket?.close();
  portConnected = false; lease = null; confirmation = null; $('program-confirmation').hidden = true;
  renderState(); append('PERTE', 'Port USB déconnecté', localDevice.id); logAgent({ stage: 'usb_lost' });
  feedback('USB déconnecté. Reconnexion automatique de la même carte à son retour ; aucune opération rejouée.');
  reconnect.disconnected(port);
});
let refreshTimer;
async function backgroundRefresh() {
  if (sessionOpened && $('client-token').value) await refreshDevices(false).catch(error => { $('server-state').textContent = 'liaison interrompue'; feedback(errorText(error)); });
  refreshTimer = setTimeout(backgroundRefresh, 4000);
}
setInterval(() => { renderState(); renewLease(); }, 1000); backgroundRefresh();
window.addEventListener('pagehide', () => { streamController?.abort(); clearTimeout(refreshTimer); reconnect.stop(); wake.update(false); });
renderState(); renderConsole();
