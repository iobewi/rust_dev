import { diagnostic } from './hardware-errors.js';
const stages = {
  connection: 'Connexion au serveur', connected: 'Agent connecté', disconnected: 'Liaison agent interrompue',
  execute: 'Début', result: 'Résultat', acknowledged: 'Résultat accepté par le serveur',
  upload: 'Transfert vers le serveur', download: 'Transfert depuis le serveur', transfer_failed: 'Échec de transfert',
  grant: 'Autorisation locale', control_released: 'Pilotage libéré par l’opérateur', protocol_error: 'Refus du protocole',
  usb_lost: 'USB débranché — aucune opération ne sera rejouée', reconnecting: 'Reconnexion de la carte',
  usb_connected: 'Port série connecté', reconnect_failed: 'Reconnexion en attente', identity_changed: 'Carte différente : sélection du port requise',
  console: 'Ouverture de console — peut redémarrer la carte', loader: 'Entrée en mode chargeur',
};
const commands = new Set(['identify', 'partitions', 'flash.read', 'flash.write', 'flash.erase', 'console', 'reset', 'io0', 'mark', 'uart.write', 'uart.configure', 'trigger', 'http.request']);
export class AgentJournal {
  constructor(limit = 300) { this.limit = limit; this.rows = []; }
  append(event) {
    if (!stages[event.stage]) return;
    const parts = [stages[event.stage]];
    if (commands.has(event.command)) parts.push(event.command);
    if (/^[a-z0-9]{1,64}$/i.test(event.id || '')) parts.push(event.id.slice(0, 10));
    if (['completed', 'failed', 'cancelled', 'indeterminate'].includes(event.state)) parts.push(event.state);
    if (event.code && diagnostic(event.code) !== event.code) parts.push(diagnostic(event.code));
    if (Number.isFinite(event.bytes)) parts.push(`${event.bytes} octets`);
    if (Number.isFinite(event.duration_ms)) parts.push(`${event.duration_ms} ms`);
    if (Number.isFinite(event.seconds)) parts.push(`${event.seconds} s`);
    this.rows.push({ at: event.at || Date.now(), text: parts.join(' · ') });
    if (this.rows.length > this.limit) this.rows.shift();
  }
}
