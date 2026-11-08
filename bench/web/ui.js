import { HardwareError, diagnostic } from './hardware-errors.js';
import { WorkWakeLock } from './wake-lock.js';
import { BoardManager } from './board-manager.js';
import { AgentJournal } from './agent-journal.js';
import { Agent } from './agent.js';
import { EspEngine } from './esp-engine.js';
import { BenchClient } from './api-client.js';
import { b64, unb64, manifestPlan } from './protocol.js';
import { ConsoleBuffer } from './console-buffer.js';
import { address, hex, flashPlan } from './ui-model.js';

const $ = id => document.getElementById(id);
document.title = 'IOBEWI Device Lab';
const terminal = new Set(['completed', 'failed', 'expired', 'cancelled', 'indeterminate']);
const safeCommands = new Set(['identify', 'partitions', 'flash.read', 'mark']);
const actions = { 'try-partition-address': 'partitions', read: 'flash.read', write: 'flash.write', erase: 'flash.erase', send: 'uart.write',
  configure: 'uart.configure', io0: 'io0', mark: 'mark', trigger: 'trigger', disarm: 'trigger', http: 'http.request' };
let usbBusy = false, usbDisconnecting = false;
let agent, engine, localDevice, portConnected = false, current;
const inFlight = new Map();
const isWorking = () => inFlight.has(selected());
let reconnect = { waiting: false, running: false, stop() {}, forget() {} };
let esptoolDeps;
const boardBusy = new Map();
let knownDevices = [], imagePlan = [], artifactList = [], confirmation;
let format = 'text', paused = false, pausedAt = Infinity, renderPending = false, selectedOperation, lastConsoleKey;
let streamController, streamGeneration = 0, refreshPromise, sessionOpened = false;
let session, sessionPromise;
const partitionSuggestions = new Map();
const captures = new Map(), cursors = new Map(), operationLog = new Map(), partitionCache = new Map();
const api = () => new BenchClient();
const selected = () => $('devices').value;
const info = id => knownDevices.find(d => d.id === (id || selected()));
const capture = id => { if (!captures.has(id)) captures.set(id, new ConsoleBuffer()); return captures.get(id); };
const feedback = message => { $('client-state').textContent = message; };
const errorText = error => ({ unauthenticated: 'Session du banc expirée : rétablissez la liaison au serveur', local_session_only: 'La session du banc est refusée pour ce chemin de connexion ; vérifiez la configuration de la redirection locale', device_changed: 'La carte a changé : vérifiez et confirmez de nouveau la cible', forbidden: 'Droits API insuffisants',
  grant_required: 'Autorisation locale temporaire requise sur l’agent de cette carte', device_reserved: 'Carte déjà pilotée par un autre client',
  device_quarantined: 'Carte bloquée : relisez la flash avant la récupération opérateur', lease_expired: 'Pilotage expiré : reprenez la main',
  device_unavailable: 'Carte indisponible', flash_overlap: 'Les secteurs d’effacement se chevauchent' }[error.message] || diagnostic(error.message));
const safely = fn => async () => { try { await fn(); } catch (error) {
  if (['lease_expired', 'device_changed'].includes(error.message)) { confirmation = null; $('program-confirmation').hidden = true; }
  feedback(errorText(error)); renderState();
} };
const hook = (id, fn) => { $(id).onclick = safely(fn); };
const json = value => { $('result').textContent = JSON.stringify(value, null, 2); };
$('server-origin').textContent = location.origin;

const journals = new Map();
const journalFor = id => {
  if (!journals.has(id)) journals.set(id, new AgentJournal());
  return journals.get(id);
};
function renderJournal() {
  const body = $('agent-log'), previousScroll = body.scrollTop;
  const follow = body.scrollHeight - body.clientHeight - body.scrollTop < 30;
  body.replaceChildren();
  for (const row of (journals.get(selected())?.rows || [])) {
    const line = document.createElement('div'); line.className = 'journal-row';
    const time = document.createElement('time'); time.textContent = new Date(row.at).toLocaleTimeString('fr-FR', { hour12: false });
    const text = document.createElement('span'); text.textContent = row.text; line.append(time, text); body.append(line);
  }
  body.scrollTop = follow ? body.scrollHeight : previousScroll;
}
function logAgent(event) {
  const device = event.device || localDevice?.id || selected();
  if (!device) return;
  journalFor(device).append(event);
  if (device === selected()) renderJournal();
}
hook('agent-log-clear', () => { if (selected()) journalFor(selected()).rows = []; renderJournal(); });
hook('agent-log-toggle', () => { const open = $('agent-log').hidden; $('agent-log').hidden = !open; $('agent-log-toggle').textContent = open ? 'Réduire' : 'Développer'; $('agent-log-toggle').setAttribute('aria-expanded', String(open)); });
const wake = new WorkWakeLock({ wakeLock: navigator.wakeLock, visible: () => document.visibilityState === 'visible', onState: wakeIndicator });
const activity = () => wake.update(boards.list().some(ctx => ctx.status === 'connected' &&
  (ctx.engine?.busy || ctx.agent?.active || ctx.agent?.awaitingResult)));
document.addEventListener('visibilitychange', () => wake.update(wake.active, true));
const boards = new BoardManager({
  allowed: [], ports: () => navigator.serial.getPorts(),
  createEngine(ctx, port, emit) {
    const hardware = new EspEngine(port, esptoolDeps, emit,
      held => localStorage.setItem('bench.v1.hold.' + ctx.id, held ? '1' : '0'));
    hardware.hold = localStorage.getItem('bench.v1.hold.' + ctx.id) === '1';
    return hardware;
  },
  makeDevice(ctx, identity, hardware) {
    return { id: ctx.id, ...identity, capabilities: hardware.capabilities(),
      connected: true, localOnly: true, http_origins: ctx.options.http_origins || [] };
  },
  createAgent(ctx, hardware) {
    return new Agent({ engine: hardware, device: ctx.localDevice, scopedBrowserAgent: true,
      onLog: event => logAgent({ ...event, device: ctx.id }),
      onActivity: activity,
      onState: state => {
        ctx.generation = ctx.agent?.generation || null;
        if (selected() === ctx.id) {
          $('agent-state').textContent = state === 'Agent connected' ? 'Agent connecté' :
            state.startsWith('Agent disconnected') ? 'Agent déconnecté · aucune commande rejouée' : state;
          if (!ctx.generation) { confirmation = null; $('program-confirmation').hidden = true; }
          $('server-state').textContent = ctx.generation ? 'agent connecté' :
            sessionOpened ? 'client connecté · agent absent' : 'agent non connecté';
          renderState();
          if (state === 'Agent connected') closeAccess();
        }
        if (state === 'Agent connected' || state === 'Pilotage libéré par l’opérateur')
          refreshDevices(false).catch(error => feedback(errorText(error)));
      },
    });
  },
  onEvent(ctx, type, event) {
    if (type === 'uart') receive(unb64(event.data), ctx.id, event.at);
    else if (type === 'transport') {
      append('SYS', event.detail, ctx.id);
      if (event.detail === 'opening console') logAgent({ stage: 'console', device: ctx.id });
      if (event.detail === 'opening loader') logAgent({ stage: 'loader', device: ctx.id });
    } else if (type === 'usb_lost') {
      append('PERTE', 'Port USB déconnecté', ctx.id);
      logAgent({ stage: 'usb_lost', device: ctx.id });
    } else if (type === 'reconnect') {
      logAgent({ stage: event.stage, device: ctx.id });
    }
  },
  onState(ctx) {
    if (selected() === ctx.id) syncContext();
    renderActiveCards();
    renderState();
  },
});
function syncContext() {
  const ctx = boards.get(selected());
  agent = ctx?.agent || null;
  engine = ctx?.engine || null;
  localDevice = ctx?.localDevice || null;
  portConnected = ctx?.status === 'connected';
  reconnect = ctx?.reconnect || { waiting: false, running: false, stop() {}, forget() {} };
}

function renderActiveCards() {
  const body = $('active-cards'), previous = $('add-device-id').value;
  body.replaceChildren();
  for (const ctx of boards.list()) {
    const row = document.createElement('div');
    row.className = 'active-board';
    row.dataset.device = ctx.id;
    row.dataset.selected = String(selected() === ctx.id);
    const name = document.createElement('button');
    name.type = 'button'; name.className = 'board-name'; name.textContent = ctx.id;
    name.setAttribute('aria-pressed', String(selected() === ctx.id));
    name.title = 'Afficher cette carte, sans action matérielle';
    name.onclick = () => {
      $('devices').value = ctx.id;
      boards.select(ctx.id);
      $('devices').onchange?.();
    };
    const chip = document.createElement('span');
    chip.className = 'board-chip'; chip.textContent = ctx.identity?.chip || 'MCU inconnu';
    const status = document.createElement('button');
    status.type = 'button'; status.className = 'board-status';
    status.dataset.state = ctx.status;
    status.textContent = ({ connected: 'Connectée', connecting: 'Connexion…',
      usb_lost: ctx.reconnect?.waiting ? 'Reconnexion en attente' : 'USB perdu',
      reconnecting: 'Reconnexion…', disconnected: 'Déconnectée' })[ctx.status] || 'Indisponible';
    status.title = ctx.status === 'connected' ? 'Déconnecter uniquement ce port USB' :
      ctx.reconnect?.waiting ? 'Annuler la reconnexion de cette carte' :
      'Reconnecter cette carte à son port précédemment sélectionné';
    status.disabled = ctx.disconnecting || ctx.busy || ctx.engine?.busy || ctx.agent?.active ||
      ctx.agent?.awaitingResult || ctx.reconnect?.running || ctx.status === 'connecting';
    status.onclick = safely(async () => {
      if (ctx.status === 'connected') {
        if (ctx.agent?.active || ctx.agent?.awaitingResult || ctx.engine?.busy) throw new Error('Opération en cours');
        await boards.disconnect(ctx.id);
      } else if (ctx.reconnect?.waiting) {
        boards.cancelReconnect(ctx.id);
      } else {
        await boards.reconnect(ctx.id);
      }
      await refreshDevices(false);
      renderDevices();
    });
    row.append(name, chip, status);
    if (ctx.status === 'disconnected') {
      const forget = document.createElement('button');
      forget.type = 'button'; forget.className = 'board-forget';
      forget.textContent = 'Retirer'; forget.title = 'Oublier le port dans cette fenêtre sans supprimer les preuves serveur';
      forget.onclick = safely(() => {
        boards.contexts.forget(ctx.id);
        renderDevices();
      });
      row.append(forget);
    }
    body.append(row);
  }
  if (!body.children.length) {
    const empty = document.createElement('p'); empty.className = 'hint';
    empty.textContent = 'Aucune carte connectée dans cette fenêtre.'; body.append(empty);
  }
  const free = (session?.devices || []).filter(id => !boards.get(id));
  const select = $('add-device-id');
  select.replaceChildren();
  for (const id of free) {
    const opt = document.createElement('option'); opt.value = id; opt.textContent = id;
    select.append(opt);
  }
  if (free.includes(previous)) select.value = previous;
  if (!free.length) {
    const option = document.createElement('option'); option.textContent = 'Aucun ID libre'; option.value = ''; select.append(option);
  }
  select.disabled = !free.length;
  $('add-device').disabled = !free.length || usbBusy;
}
function renderLocalCardChoices(devices) {
  boards.contexts.setAllowed(devices);
  const select = $('device-id');
  select.replaceChildren();
  for (const id of devices) {
    const option = document.createElement('option'); option.value = id; option.textContent = id;
    select.append(option);
  }
  renderActiveCards();
  renderState();
}

async function openSession() {
  if (session) return session;
  if (!sessionPromise) sessionPromise = (async () => {
    const response = await fetch('/session/v1/open', { method: 'POST', credentials: 'same-origin' });
    if (!response.ok) throw new Error((await response.json()).error?.code || 'Session du banc indisponible');
    session = await response.json();
    renderLocalCardChoices(session.devices);
    $('server-state').textContent = 'session locale ouverte';
    return session;
  })();
  try { return await sessionPromise; } finally { sessionPromise = null; }
}
function selectedTarget() {
  const d = info();
  if (!session || !d?.connected || boards.get(d.id) && boards.get(d.id).status !== 'connected')
    throw new Error('Connectez une carte au banc');
  return { device: d.id, chip: d.chip, flash_size: d.flash_size, generation: d.generation };
}
function selectTabs(attribute, name, panelSelector, panelPrefix) {
  document.querySelectorAll(`[${attribute}]`).forEach(button => {
    const active = button.getAttribute(attribute) === name;
    button.setAttribute('aria-selected', String(active)); button.tabIndex = active ? 0 : -1;
  });
  let focusHidden = false;
  document.querySelectorAll(panelSelector).forEach(panel => {
    panel.hidden = panel.id !== panelPrefix + name;
    if (panel.hidden && panel.contains(document.activeElement)) focusHidden = true;
  });
  if (focusHidden) document.querySelector(`[${attribute}="${name}"]`).focus();
}
let consoleView;
function workspaceTab(name) {
  const wasHidden = $('workspace-consoles').hidden;
  if (!wasHidden && name !== 'consoles') {
    consoleView = ['uart-log'].map(id => {
      const node = $(id);
      return { id, scroll: node.scrollTop, follow: node.scrollHeight - node.clientHeight - node.scrollTop < 30 };
    });
  }
  selectTabs('data-workspace-tab', name, '.workspace-panel', 'workspace-');
  if (name === 'consoles') {
    if (wasHidden && consoleView) {
      for (const { id, scroll, follow } of consoleView) $(id).scrollTop = follow ? $(id).scrollHeight : scroll;
    }
    scheduleConsole();
  }
}
function tab(name) {
  workspaceTab('operations');
  selectTabs('data-tab', name, '.tab-panel', 'panel-');
}
function wireTabs(attribute, select) {
  const buttons = [...document.querySelectorAll(`[${attribute}]`)];
  buttons.forEach(button => {
    button.tabIndex = button.getAttribute('aria-selected') === 'true' ? 0 : -1;
    button.onclick = () => select(button.getAttribute(attribute));
    button.onkeydown = event => {
      let index = buttons.indexOf(button);
      if (event.key === 'ArrowRight') index = (index + 1) % buttons.length;
      else if (event.key === 'ArrowLeft') index = (index + buttons.length - 1) % buttons.length;
      else if (event.key === 'Home') index = 0;
      else if (event.key === 'End') index = buttons.length - 1;
      else return;
      event.preventDefault(); buttons[index].click(); buttons[index].focus();
    };
  });
}
wireTabs('data-workspace-tab', workspaceTab);
wireTabs('data-tab', tab);
document.querySelectorAll('.help').forEach(help => {
  help.addEventListener('keydown', event => {
    if (event.key === 'Escape') { help.classList.add('dismissed'); help.querySelector('button').blur(); }
  });
  help.addEventListener('mouseenter', () => help.classList.remove('dismissed'));
  help.addEventListener('focusin', () => help.classList.remove('dismissed'));
});
function indicator(name, label, icon, tone = 'neutral') {
  const button = $('status-' + name);
  $('status-label-' + name).textContent = label;
  button.dataset.tone = tone;
  button.querySelector('use').setAttribute('href', '#icon-' + icon);
  button.setAttribute('aria-label', ({ card: 'Carte', usb: 'USB', agent: 'Agent', control: 'Pilotage', permissions: 'Autorisations', wake: 'Veille' }[name]) + ' : ' + label);
}
function statusDetails(name = null) {
  document.querySelectorAll('[data-status]').forEach(button => {
    const open = button.dataset.status === name;
    const panel = $('status-panel-' + button.dataset.status);
    const restoreFocus = !open && panel.contains(document.activeElement);
    panel.hidden = !open;
    button.setAttribute('aria-expanded', String(open));
    if (restoreFocus) button.focus();
    if (!open) button.closest('.help').classList.add('dismissed');
  });
  if (name) {
    const title = $('status-panel-' + name).querySelector('.pane-title');
    title.style.scrollMarginTop = document.querySelector('.app-header').offsetHeight + 'px';
    title.scrollIntoView({ block: 'nearest' });
  }
}
new ResizeObserver(() => {
  document.documentElement.style.setProperty('--header-height', document.querySelector('.app-header').offsetHeight + 'px');
}).observe(document.querySelector('.app-header'));
document.querySelectorAll('[data-status]').forEach(button => {
  button.onclick = () => statusDetails(button.getAttribute('aria-expanded') === 'true' ? null : button.dataset.status);
});
document.querySelectorAll('[data-close-status]').forEach(button => { button.onclick = () => statusDetails(); });
document.addEventListener('keydown', event => {
  if (event.key === 'Escape' && document.querySelector('.status-panel:not([hidden])')) {
    event.preventDefault(); statusDetails();
  }
});
function wakeIndicator(state) {
  $('wake-state').textContent = state;
  const held = state === 'Écran maintenu allumé', normal = state === 'Veille : normale';
  indicator('wake', held ? 'Écran actif' : normal ? 'Veille normale' : state.includes('masqué') ? 'Onglet masqué' : 'Veille non protégée', held ? 'sun' : normal ? 'moon' : 'alert', held ? 'active' : normal ? 'neutral' : 'warning');
}
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
  if ($('workspace-consoles').hidden) return;
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
let consoleFollowing = true;
function followState() {
  if ($('workspace-consoles').hidden) return;
  const body = $('uart-log'), following = body.scrollHeight - body.clientHeight - body.scrollTop < 30;
  consoleFollowing = following;
  $('follow-state').textContent = paused ? 'Affichage en pause · capture active' : following ? 'Suivi du direct' : 'Lecture de l’historique · capture active';
  $('follow').hidden = !paused && following;
}
let consoleHeight = $('uart-log').clientHeight;
$('uart-log').addEventListener('scroll', () => {
  // A resize may emit scroll before ResizeObserver. Preserve the prior follow
  // intent until the observer has adjusted the viewport.
  if ($('uart-log').clientHeight === consoleHeight) followState();
});
new ResizeObserver(() => {
  if ($('workspace-consoles').hidden) return;
  if (consoleFollowing && !paused) $('uart-log').scrollTop = $('uart-log').scrollHeight;
  consoleHeight = $('uart-log').clientHeight;
  followState();
}).observe($('uart-log'));
hook('follow', () => { paused = false; pausedAt = Infinity; $('pause').textContent = 'Pause affichage'; renderConsole(); $('uart-log').scrollTop = $('uart-log').scrollHeight; followState(); });
hook('console-auto-size', () => {
  const body = $('uart-log'), follow = body.scrollHeight - body.clientHeight - body.scrollTop < 30;
  body.style.removeProperty('height');
  if (follow) body.scrollTop = body.scrollHeight;
  followState();
});
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
  const previous = selected();
  const devices = [...knownDevices];
  for (const ctx of boards.list()) {
    if (!devices.some(d => d.id === ctx.id)) devices.push({ ...ctx.localDevice,
      id: ctx.id, chip: ctx.identity?.chip || 'inconnu', connected: ctx.status === 'connected', localOnly: true });
  }
  $('devices').replaceChildren();
  for (const d of devices) {
    const option = document.createElement('option'); option.value = d.id;
    option.textContent = `${d.id} · ${d.chip}${d.connected ? '' : ' · absent'}`;
    $('devices').append(option);
  }
  $('devices').value = previous;
  if (!selected() && devices.length) $('devices').value = boards.list()[0]?.id || devices[0].id;
  syncContext();
  renderActiveCards(); renderState(); renderPartitions(); renderConsole(); renderJournal();
}
function renderState() {
  syncContext();
  current = inFlight.get(selected())?.id || null;
  const d = info() || (localDevice?.id === selected() ? localDevice : null);
  const local = localDevice && selected() === localDevice.id;
  const blocked = d?.quarantined || local && engine?.hold;
  $('card-details').hidden = !(boards.get(selected()) || d?.connected);
  const ownBusy = local && (engine?.busy || agent?.active || agent?.awaitingResult);
  const capability = new Set(d?.capabilities || []);
  const otherOwner = d?.reservation && d.reservation.owner !== session?.client;
  const base = Boolean(session && info()?.connected && (!local || portConnected && agent?.generation && agent.socket?.readyState === 1) &&
    !isWorking() && !ownBusy && !usbBusy && !reconnect.running && !otherOwner);
  document.querySelectorAll('[data-hardware]').forEach(button => {
    const command = button.dataset.command || actions[button.id];
    button.disabled = !base || !capability.has(command) || Boolean(blocked && !safeCommands.has(command)) ||
      command === 'console' && (!local || engine?.mode !== 'loader') ||
      command === 'reset' && local && engine?.mode !== 'console';
  });
  $('partition-suggestion').hidden = partitionSuggestions.get(selected()) !== d?.generation || !d?.generation || Number($('partition-address').value) !== 0x8000;
  $('devices').disabled = false;
  // A logical ID cannot change while its port is connected or automatic reconnection owns it.
  $('device-id').disabled = Boolean(!session || !session.devices.length || portConnected || usbBusy || reconnect.waiting || reconnect.running);
  $('cancel').disabled = !isWorking() || !current;
  $('usb-state').textContent = portConnected ? 'connecté' : 'non connecté';
  $('card-commands').dataset.mode = local ? engine?.mode || 'off' : 'remote';
  $('chip-info').textContent = d?.chip || '—'; $('flash-info').textContent = d?.flash_size ? `${hex(d.flash_size)} · ${d.flash_size} octets` : '—';
  $('rdid-info').textContent = local && localDevice.flash_id != null ? hex(localDevice.flash_id) : '—';
  $('interface-info').textContent = local ? engine?.usbSerialJtag ? 'USB Serial/JTAG' : engine?.native ? 'USB natif (OTG ou non identifié)' : 'UART externe' : 'Agent du banc';
  const resetButton = $('reset-device'), usbReset = local && engine?.usbSerialJtag;
  $('card-reset-actions').hidden = !capability.has('reset');
  resetButton.hidden = !capability.has('reset');
  $('card-boot-actions').hidden = !capability.has('io0');
  resetButton.textContent = usbReset ? 'Reset USB' : local ? 'Reset EN' : 'Redémarrer';
  resetButton.title = usbReset ? 'Redémarre via le contrôleur USB Serial/JTAG, sans piloter EN ou IO0.' : 'Redémarre par EN, en mode console avec un pont UART externe.';

  $('control-state').textContent = isWorking() ? 'Opération manuelle en cours' : d?.reservation ? (otherOwner ? 'Pilotée par : ' + d.reservation.owner : 'Déclencheur ou opération en cours') : d?.connected && !d?.localOnly ? 'Disponible · commandes directes' : 'Local · observation';
  $('grant').disabled = !local || !portConnected || !agent?.generation; $('revoke').disabled = $('grant').disabled;
  $('grant-state').textContent = local && agent?.grantUntil > performance.now() ? `Opérations distantes autorisées : ${Math.ceil((agent.grantUntil - performance.now()) / 1000)} s` : 'À la demande · aucune autorisation distante active.';
  $('revoke').hidden = !(local && agent?.grantUntil > performance.now());
  $('release-control').disabled = !local || !agent?.generation || Boolean(ownBusy || isWorking()) || !d?.reservation;
  activity();
  $('safety-state').textContent = local && agent?.awaitingResult ? 'Attente de l’accusé serveur' : blocked ? 'Blocage : récupération opérateur requise' : d ? 'Aucun blocage signalé' : 'Aucun état matériel connu';
  $('safety-state').classList.toggle('danger', Boolean(blocked));
  $('recover').hidden = !blocked; $('recovery-help').hidden = !blocked;
  $('recover').disabled = !local || !portConnected || !agent?.generation || Boolean(ownBusy) || !blocked;
  const usbAction = usbBusy ? usbDisconnecting ? 'Déconnexion…' : 'Connexion…' : portConnected ? 'Déconnecter' : reconnect.running ? 'Reconnexion…' : reconnect.waiting ? 'Annuler la reconnexion' : 'Connecter';
  $('usb-toggle').textContent = usbAction;
  $('usb-toggle').disabled = !session || !session.devices.length || usbBusy || isWorking() || Boolean(ownBusy || reconnect.running);
  $('usb-toggle').title = portConnected ? 'Déconnecter volontairement le port désactive sa reconnexion automatique.' : reconnect.waiting ? 'Attendre la même carte ou annuler la reconnexion pour choisir un autre port.' : 'Choisir le port série à connecter. Son ouverture peut redémarrer la carte.';
  $('usb-indicator').dataset.tone = portConnected ? 'positive' : reconnect.waiting || usbBusy ? 'active' : 'neutral';
  $('usb-indicator').querySelector('use').setAttribute('href', '#icon-' + (portConnected ? 'usb' : reconnect.waiting || usbBusy ? 'busy' : 'unplug'));
  indicator('card', blocked ? 'Carte bloquée' : selected() || 'Carte', blocked ? 'alert' : 'chip', blocked ? 'warning' : 'neutral');
  const linked = local ? Boolean(agent?.generation && agent?.socket?.readyState === 1) : Boolean(d?.connected);
  const serverLost = $('server-state').textContent === 'liaison interrompue';
  indicator('agent', serverLost ? 'Liaison interrompue' : linked ? 'Agent connecté' : 'Agent hors ligne', linked && !serverLost ? 'link' : 'offline', serverLost ? 'warning' : linked ? 'positive' : 'neutral');
  indicator('control', otherOwner ? 'Occupée · ' + d.reservation.owner : isWorking() || ownBusy || d?.reservation ? 'En cours' : d?.connected ? 'Disponible' : 'En attente', otherOwner ? 'user' : isWorking() || ownBusy || d?.reservation ? 'busy' : 'check', otherOwner || isWorking() || ownBusy || d?.reservation ? 'active' : 'neutral');
  const granted = local && agent?.grantUntil > performance.now();
  indicator('permissions', granted ? `Distant autorisé · ${Math.ceil((agent.grantUntil - performance.now()) / 1000)} s` : 'Autorisations', granted ? 'unlock' : 'lock', granted ? 'warning' : 'neutral');
  $('footer-target').textContent = selected() ? `${selected()} · ${$('control-state').textContent}` : 'Aucune carte sélectionnée';
}
$('devices').onchange = () => {
  confirmation = null; $('program-confirmation').hidden = true; $('download').replaceChildren(); $('hex-dump').textContent = 'Aucune donnée lue pour cette carte.';
  if (paused) pausedAt = capture(selected()).sequence;
  selectedOperation = null;
  syncContext(); renderActiveCards();
  renderOperations(); json({});
  renderState(); renderPartitions(); renderConsole(); renderJournal(); startStream();
};
async function refreshDevices(publish = true) {
  if (!session) return;
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
hook('refresh', async () => { await openSession(); await refreshDevices(); });

async function startStream() {
  const id = selected();
  if (!id || !session || !knownDevices.some(d => d.id === id)) {
    streamController?.abort(); streamController = null; streamGeneration++; return;
  }
  const key = id + ':' + session.client;
  if (streamController?.key === key && !streamController.signal.aborted) return;
  streamController?.abort(); const controller = new AbortController(); controller.key = key; streamController = controller;
  const generation = ++streamGeneration, cursor = cursors.get(id) || { next: 0, epoch: null };
  try {
    for await (const batch of api().events(id, cursor.next, controller.signal)) {
      if (controller.signal.aborted || generation !== streamGeneration) break;
      if (cursor.epoch && cursor.epoch !== batch.epoch) {
        append('PERTE', 'Serveur redémarré : rupture de continuité', id); confirmation = null; $('program-confirmation').hidden = true; cursor.next = 0; cursor.epoch = batch.epoch;
        controller.abort(); streamController = null; if (selected() === id) startStream(); break;
      }
      cursor.epoch = batch.epoch;
      if (batch.lost) append('PERTE', `${batch.lost} événements hors du tampon serveur`, id);
      for (const event of batch.events) {
        if (event.kind === 'uart') {
          if (event.source_lost) append('PERTE', `${event.source_lost} chunks perdus à la source`, id, event.at * 1000);
          if (!(boards.get(id)?.status === 'connected' && boards.get(id)?.agent?.generation))
            receive(unb64(event.data), id, event.agent_at || event.at * 1000);
        } else if (event.kind === 'mark') append('REP', event.detail, id, event.at * 1000);
        else if (['connected', 'disconnected', 'quarantined', 'recovered'].includes(event.kind)) append('SYS', event.kind, id, event.at * 1000);
      }
      cursor.next = batch.next; cursors.set(id, cursor);
    }
  } catch (error) {
    if (!controller.signal.aborted) { append('PERTE', 'Abonnement interrompu : ' + errorText(error), id); controller.abort(); }
  }
}

async function addCard(id) {
  if (!navigator.serial) throw new Error('Web Serial indisponible dans ce navigateur ou ce contexte');
  if (!session?.devices.includes(id)) throw new Error('Identifiant non autorisé');
  if (boards.get(id)) throw new Error('Carte déjà ajoutée dans cette fenêtre');
  if (usbBusy) throw new Error('Sélection USB déjà en cours');
  // requestPort must be called directly from the click activation, before any await.
  const port = await navigator.serial.requestPort();
  usbBusy = true; renderActiveCards();
  try {
    esptoolDeps ||= await import('https://unpkg.com/esptool-js@0.7.0/bundle.js');
    const http_origins = $('http-origins').value.split(',').map(v => v.trim()).filter(Boolean)
      .map(v => new URL(v).origin);
    await boards.add(id, port, { http_origins });
    renderDevices();
    $('devices').value = id;
    boards.select(id);
    $('devices').onchange?.();
    feedback('Carte connectée. Les autres ports restent actifs.');
    await refreshDevices(false);
  } finally { usbBusy = false; renderActiveCards(); renderState(); }
}
hook('add-device', async () => {
  const id = $('add-device-id').value;
  if (!id) throw new Error('Choisissez un identifiant libre');
  await addCard(id);
});
function connectAgent() {
  const ctx = boards.get(selected());
  if (!ctx?.agent || ctx.status !== 'connected') throw new Error('Connectez d’abord un port Web Serial');
  if (!session) throw new Error('Session locale du banc indisponible');
  if (ctx.agent.socket?.readyState === 1) throw new Error('Agent déjà connecté');
  ctx.agent.reconnectNow();
}
hook('agent-connect', connectAgent);
hook('grant', () => { if (!agent?.generation) throw new Error('Agent local non connecté'); if (confirm(`Autoriser les opérations privilégiées sur ${localDevice.id} pendant ${$('grant-time').selectedOptions[0].textContent} ?`)) { agent.grant(Number($('grant-time').value)); renderState(); } });
hook('revoke', () => { agent.grant(0); renderState(); });
hook('release-control', () => { if (confirm('Libérer le pilotage abandonné et révoquer l’autorisation distante ? Une opération en cours empêchera cette action.')) agent.releaseControl(); });
hook('recover', () => { if (confirm(`Après relecture et vérification de ${localDevice.id}, lever le blocage ? Cela autorisera de nouveaux resets.`)) agent.recover(); });

function renderOperations() {
  const body = $('operations-body'); body.replaceChildren();
  for (const record of [...operationLog.values()].filter(op => op.device === selected()).reverse()) {
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
  if (op.device === selected()) {
    renderOperations();
    if (!selectedOperation || selectedOperation === op.id) json(op);
  }
}
async function submit(command, args = {}, destructive = false) {
  if (isWorking()) throw new Error('Une opération manuelle est déjà en cours sur cette carte');
  const target = selectedTarget();
  if (command === 'partitions') { partitionSuggestions.delete(target.device); partitionCache.delete(target.device); renderPartitions(); }
  const ticket = { id: null };
  inFlight.set(target.device, ticket);
  renderState(); const started = performance.now(), client = api();
  try {
    let op = await client.request('manual-operations', { method: 'POST', data: { device: target.device, generation: target.generation, command, args, allow_destructive: destructive, timeout: command === 'flash.read' ? 600 : 120 } });
    ticket.id = op.id;
    if (selected() === target.device) selectedOperation = op.id;
    recordOperation(op, started); renderState();
    for (;;) {
      if (terminal.has(op.state)) break;
      await new Promise(resolve => setTimeout(resolve, 250));
      op = await client.request('operations/' + op.id); recordOperation(op, started);
    }
    if (op.state !== 'completed') {
      const suggest = command === 'partitions' && args.address === 0x8000 && op.error?.code === 'partition_table_invalid';
      if (suggest) partitionSuggestions.set(target.device, target.generation);
      if (selected() === target.device) {
        feedback(`Opération ${op.state} : ${diagnostic(op.error?.code || 'inspectez le résultat')}`);
        tab(suggest ? 'partitions' : 'operations');
      }
      return op;
    }
    if (selected() === target.device) feedback(`${command} : terminé sur ${target.device}`);
    if (command === 'partitions' && op.result?.partitions) {
      partitionCache.set(target.device, op.result.partitions);
      if (selected() === target.device) renderPartitions();
    }
    if (command === 'identify' && op.result?.chip) { const d = info(target.device); if (d) Object.assign(d, op.result); renderState(); }
    if (command === 'flash.read' && op.result?.artifact && selected() === target.device)
      await showDump(op.result.artifact, args.address, client);
    return op;
  } finally {
    if (inFlight.get(target.device) === ticket) inFlight.delete(target.device);
    await refreshDevices(false).catch(() => {}); renderState();
  }
}
document.querySelectorAll('[data-command]').forEach(button => { button.onclick = safely(() => submit(button.dataset.command,
  button.dataset.command === 'partitions' ? { address: address($('partition-address').value) } : {})); });
$('partition-address').addEventListener('input', () => { partitionSuggestions.delete(selected()); renderState(); });
hook('try-partition-address', () => {
  if (partitionSuggestions.get(selected()) !== info()?.generation) throw new Error('La carte a changé : relisez la table à son adresse explicite');
  $('partition-address').value = '0x10000';
  return submit('partitions', { address: 0x10000 });
});
hook('cancel', async () => {
  const device = selected(), id = inFlight.get(device)?.id;
  if (!id) throw new Error('Aucune opération à annuler sur cette carte');
  const op = await api().request('operations/' + id, { method: 'DELETE' });
  if (selected() === device) {
    json(op);
    feedback(op.cancel_requested ? 'Annulation demandée ; interruption matérielle non confirmée.' : 'Commande annulée avant exécution.');
  }
});

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
  const target = selectedTarget(), images = flashPlan(imagePlan, target.flash_size);
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
  const { target, images } = validatePlan(); confirmation = { generation: target.generation, device: target.device, chip: target.chip, images };
  $('confirmation-target').textContent = `${target.device} · ${target.chip} · ${target.flash_size} octets`;
  const body = $('confirmation-regions'); body.replaceChildren();
  images.forEach((image, i) => { const row = document.createElement('tr'); for (const value of [imagePlan[i].name, hex(image.address), hex(image.address + imagePlan[i].size)]) { const cell = document.createElement('td'); cell.textContent = value; cell.className = 'mono'; row.append(cell); } body.append(row); });
  $('program-confirmation').hidden = false;
});
hook('cancel-write', () => { confirmation = null; $('program-confirmation').hidden = true; });
hook('confirm-write', async () => {
  const target = selectedTarget();
  if (!confirmation || confirmation.generation !== target.generation || confirmation.device !== selected()) throw new Error('La carte a changé : confirmez à nouveau le plan');
  const plan = confirmation; confirmation = null; $('program-confirmation').hidden = true;
  await submit('flash.write', { chip: plan.chip, images: plan.images, erase: false, return_console: true }, true);
});
hook('erase', async () => { const target = selectedTarget(); if (confirm(`Effacer TOUTE la flash de ${target.device} (${target.chip}, ${target.flash_size} octets) ?`)) await submit('flash.erase', { chip: target.chip, return_console: false }, true); });
hook('install', async () => {
  const target = selectedTarget(), manifest = $('manifest').files[0]; if (!manifest) throw new Error('Choisissez un manifeste');
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
  const ctx = boards.lost(port);
  if (!ctx) return;
  ctx.engine.disconnected = true;
  feedback('USB perdu sur ' + ctx.id + ' ; aucune opération rejouée.');
  renderDevices();
});

let refreshTimer;
async function backgroundRefresh() {
  if (sessionOpened) await refreshDevices(false).catch(async error => { $('server-state').textContent = 'liaison interrompue'; feedback(errorText(error)); if (error.message === 'unauthenticated') { session = null; await openSession().catch(() => {}); } });
  refreshTimer = setTimeout(backgroundRefresh, 4000);
}
setInterval(renderState, 1000); backgroundRefresh();
window.addEventListener('pagehide', () => {
  streamController?.abort(); clearTimeout(refreshTimer);
  for (const ctx of boards.list()) ctx.reconnect?.stop();
  wake.update(false);
});
renderState(); renderConsole();

openSession().then(() => refreshDevices(false)).catch(error => feedback(errorText(error)));
