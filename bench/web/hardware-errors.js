// Only allowlisted diagnostics cross the protocol: no arbitrary loader/UART text.
export class HardwareError extends Error {
  constructor(code, detail = {}) { super(code); this.code = code; this.detail = detail; }
}
const codes = new Set(['capacity', 'console_required', 'external_uart_required', 'partition_table_invalid', 'physical_identity_changed',
  'operator_recovery_required', 'signal_release_failed', 'serial_disconnected', 'serial_timeout', 'serial_partial_packet', 'serial_invalid_packet', 'short_flash_read', 'flash_capacity_unknown', 'artifact_transfer_failed', 'artifact_integrity_failed',
  'agent_disconnected', 'reservation_expired', 'trigger_not_owned', 'grant_expired', 'execution_deadline_exceeded', 'command_start_expired']);
const phases = new Set(['prepare', 'identify', 'flash_read', 'artifact_upload', 'artifact_download', 'flash_write', 'flash_erase', 'console', 'control']);
const kinds = new Set(['closed', 'no-response', 'partial-packet', 'invalid-packet']);
const types = new Set(['Error', 'ESPError', 'SerialFault', 'HardwareError', 'NetworkError', 'AbortError', 'TypeError', 'RangeError', 'SyntaxError', 'SecurityError', 'InvalidStateError']);
const counters = ['elapsed_ms', 'received_bytes', 'expected_bytes', 'block_address', 'block_size', 'serial_reads', 'serial_bytes',
  'serial_timeouts', 'silence_limit_ms', 'last_read_ms', 'last_packet_bytes', 'execution_remaining_ms', 'lease_remaining_ms',
  'grant_remaining_ms', 'start_remaining_ms', 'http_status', 'jedec_id', 'identify_index', 'loader_stub'];
export function hardwareError(error, disconnected = false, details = {}) {
  const input = { ...details, ...error?.detail }, phase = phases.has(input.phase) ? input.phase : 'prepare';
  const kind = kinds.has(error?.kind) ? error.kind : kinds.has(input.transport_kind) ? input.transport_kind : null;
  const transfer = phase === 'artifact_upload' || phase === 'artifact_download';
  const code = codes.has(error?.code) ? error.code : transfer ? 'artifact_transfer_failed'
    : disconnected || kind === 'closed' || error?.name === 'NetworkError' ? 'serial_disconnected'
    : ({ 'no-response': 'serial_timeout', 'partial-packet': 'serial_partial_packet', 'invalid-packet': 'serial_invalid_packet' }[kind] || 'hardware_command_failed');
  const out = { code, phase, error_type: types.has(error?.name) ? error.name : 'Error' };
  if (kind) out.transport_kind = kind;
  if (['visible', 'hidden', 'prerender', 'unavailable'].includes(input.visibility)) out.visibility = input.visibility;
  for (const key of counters) if (Number.isFinite(input[key]) && input[key] >= 0) out[key] = Math.round(input[key]);
  return out;
}
export const diagnostic = code => ({
  serial_disconnected: 'Port série déconnecté pendant l’opération.',
  serial_timeout: 'Lecture série interrompue : aucun nouvel octet avant le délai de silence.',
  serial_partial_packet: 'Lecture série interrompue : paquet incomplet avant le délai de silence.',
  serial_invalid_packet: 'Paquet série invalide : erreur de trame SLIP.',
  short_flash_read: 'Lecture flash incomplète : aucun dump partiel publié.',
  flash_capacity_unknown: 'Identification physique : l’identifiant JEDEC lu ne correspond à aucune capacité connue (valeur brute dans le diagnostic).',
  capacity: 'Stockage temporaire du serveur saturé ; nettoyage explicite requis.',
  artifact_transfer_failed: 'Échec du transfert d’artefact avec le serveur.',
  artifact_integrity_failed: 'Empreinte de l’artefact incorrecte.',
  agent_disconnected: 'Liaison de l’agent au serveur interrompue.',
  reservation_expired: 'Le droit de pilotage a expiré pendant l’opération.',
  grant_expired: 'L’autorisation locale a expiré pendant l’opération.',
  trigger_not_owned: 'Le déclencheur appartient à une autre réservation ou session.',
  execution_deadline_exceeded: 'Délai maximal d’exécution dépassé.',
  command_start_expired: 'Délai de départ dépassé : commande non démarrée.',
  console_required: 'Action impossible en mode chargeur : revenez en console.',
  external_uart_required: 'Cette action nécessite un pont UART externe ; EN/IO0 ne sont pas disponibles sur USB natif.',
  partition_table_invalid: 'Aucune table de partitions valide à cette adresse. Vérifiez son emplacement dans le projet (par exemple 0x10000) ; 0x8000 n’est pas universel.',
  physical_identity_changed: 'L’identité physique de la carte a changé : choisissez explicitement le port.',
  operator_recovery_required: 'Récupération opérateur requise avant de rouvrir la console.',
  program_permission_required: 'Le client ne possède pas le droit de programmation.',
  destructive_consent_required: 'Le consentement explicite à l’écriture ou à l’effacement est absent.',
  hardware_command_failed: 'Échec de la commande matérielle. Consultez le journal de l’agent.',
}[code] || code);
export const phaseLabel = phase => ({ prepare: 'Préparation', identify: 'Identification', flash_read: 'Lecture flash',
  artifact_upload: 'Envoi du dump au serveur', artifact_download: 'Réception du binaire', flash_write: 'Programmation',
  flash_erase: 'Effacement', console: 'Console', control: 'Commande de contrôle' }[phase] || 'Préparation');
