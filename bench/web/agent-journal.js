import { diagnostic, phaseLabel } from './hardware-errors.js';
const stages = {
  connection: 'Connexion au serveur', connected: 'Agent connecté', disconnected: 'Liaison agent interrompue',
  progress: 'Progression', execute: 'Début', result: 'Résultat', acknowledged: 'Résultat accepté par le serveur',
  upload: 'Transfert vers le serveur', download: 'Transfert depuis le serveur', transfer_failed: 'Échec de transfert',
  grant: 'Autorisation locale', control_released: 'Pilotage libéré par l’opérateur', protocol_error: 'Refus du protocole',
  usb_lost: 'USB débranché — aucune opération ne sera rejouée', reconnecting: 'Reconnexion de la carte',
  usb_connected: 'Port série connecté', reconnect_failed: 'Reconnexion en attente', identity_changed: 'Carte différente : sélection du port requise',
  console: 'Ouverture de console — peut redémarrer la carte', loader: 'Entrée en mode chargeur',
};
const commands = new Set(['identify', 'partitions', 'flash.read', 'flash.write', 'flash.erase', 'console', 'reset', 'io0', 'boot.download', 'mark', 'uart.write', 'uart.configure', 'trigger', 'http.request']);
export class AgentJournal {
  constructor(limit = 300) { this.limit = limit; this.rows = []; }
  append(event) {
    if (!stages[event.stage]) return;
    const parts = [stages[event.stage]];
    if (commands.has(event.command)) parts.push(event.command);
    if (/^[a-z0-9]{1,64}$/i.test(event.id || '')) parts.push(event.id.slice(0, 10));
    if (['completed', 'failed', 'cancelled', 'indeterminate'].includes(event.state)) parts.push(event.state);
    if (event.code && diagnostic(event.code) !== event.code) parts.push(diagnostic(event.code));
    const detail = event.detail;
    if (detail && typeof detail === 'object') {
      parts.push(phaseLabel(detail.phase));
      if (Number.isInteger(detail.http_status)) parts.push(`HTTP ${detail.http_status}`);
      if (Number.isFinite(detail.received_bytes) && Number.isFinite(detail.expected_bytes)) parts.push(`${detail.received_bytes} / ${detail.expected_bytes} octets`);
      if (['SerialFault', 'ESPError', 'NetworkError', 'AbortError', 'TypeError', 'RangeError', 'SyntaxError', 'Error', 'HardwareError', 'SecurityError', 'InvalidStateError'].includes(detail.error_type)) parts.push(detail.error_type);
      if (['closed', 'no-response', 'partial-packet', 'invalid-packet'].includes(detail.transport_kind)) parts.push(detail.transport_kind);
      if (Number.isFinite(detail.execution_remaining_ms)) parts.push(`exécution : ${detail.execution_remaining_ms} ms restantes`);
      if (Number.isFinite(detail.lease_remaining_ms)) parts.push(`pilotage : ${detail.lease_remaining_ms} ms restantes`);
      if (Number.isFinite(detail.silence_limit_ms)) parts.push(`silence maximal : ${detail.silence_limit_ms} ms`);
      if (['visible', 'hidden', 'unavailable'].includes(detail.visibility)) parts.push(`onglet : ${detail.visibility}`);
    }

    if (Number.isFinite(event.bytes)) parts.push(`${event.bytes} octets`);
    if (Number.isFinite(event.duration_ms)) parts.push(`${event.duration_ms} ms`);
    if (Number.isFinite(event.seconds)) parts.push(`${event.seconds} s`);
    this.rows.push({ at: event.at || Date.now(), text: parts.join(' · ') });
    if (this.rows.length > this.limit) this.rows.shift();
  }
}
