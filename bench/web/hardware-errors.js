// Only allowlisted diagnostics cross the protocol: no arbitrary loader/UART text.
export class HardwareError extends Error {
  constructor(code, detail = {}) { super(code); this.code = code; this.detail = detail; }
}
const codes = new Set(['loader_baud_invalid', 'capacity', 'console_required', 'external_uart_required', 'partition_table_invalid', 'physical_identity_changed',
  'operator_recovery_required', 'physical_identity_unverified', 'signal_release_failed', 'serial_disconnected', 'serial_timeout', 'serial_partial_packet', 'serial_invalid_packet', 'short_flash_read', 'flash_capacity_unknown', 'artifact_transfer_failed', 'artifact_integrity_failed',
  'agent_disconnected', 'reservation_expired', 'trigger_not_owned', 'grant_expired', 'execution_deadline_exceeded', 'command_start_expired']);
const phases = new Set(['prepare', 'identify', 'flash_read', 'artifact_upload', 'artifact_download', 'flash_write', 'flash_erase', 'console', 'control',
  'diagnostic_stub_load', 'diagnostic_read']);
const kinds = new Set(['closed', 'no-response', 'partial-packet', 'invalid-packet']);
const types = new Set(['IdlabDiagnosticError', 'IdlabTraceError', 'Error', 'ESPError', 'SerialFault', 'HardwareError', 'NetworkError', 'AbortError', 'TypeError', 'RangeError', 'SyntaxError', 'SecurityError', 'InvalidStateError']);
// Codes of the P4 diagnostic cross the protocol as they are (fixed vocabulary, lowercase, bounded): never free text from the loader.
const diagnosticCode = code => typeof code === 'string' && (/^(diagnostic|idlab_trace)_[a-z0-9_]{1,80}$/.test(code) || code === 'manual_download_required');
const categories = new Set(['local_validation', 'session_open', 'refused_before_load', 'ram_load_failed', 'stream_failed', 'unexpected']);
const ramLoad = new Set(['not_started', 'started', 'unknown']);
const counters = ['chip_revision', 'elapsed_ms', 'received_bytes', 'expected_bytes', 'block_address', 'block_size', 'serial_reads', 'serial_bytes',
  'serial_timeouts', 'silence_limit_ms', 'last_read_ms', 'last_packet_bytes', 'execution_remaining_ms', 'lease_remaining_ms',
  'grant_remaining_ms', 'start_remaining_ms', 'http_status', 'jedec_id', 'identify_index', 'loader_stub', 'sync_zero_count', 'sync_stub_detected', 'sync_count', 'sync_completed',
  'ram_block_size', 'ram_acked_count', 'ram_failed_index', 'ram_last_acked_index', 'ram_block_default', 'usb_otg_confirmed'];
const md5Shapes = new Set(['rom', 'stub', 'unknown']);
// Typed per-step report of the RAM download. All-or-nothing: one malformed step drops the whole list (no partial, no free text).
const stepNames = new Set(['mem_begin', 'mem_block', 'mem_finish']), segments = new Set(['text', 'data', 'entry']);
const stepOutcomes = new Set(['ack', 'timeout', 'closed', 'invalid', 'error']);
const u32 = v => Number.isInteger(v) && v >= 0 && v <= 0xffffffff;
export function ramStepsOf(list) {
  if (!Array.isArray(list) || list.length === 0 || list.length > 32) return null;
  const out = [];
  for (const e of list) {
    if (!e || typeof e !== 'object' || !stepNames.has(e.step) || !segments.has(e.segment) || !stepOutcomes.has(e.outcome)) return null;
    if (![e.address, e.size, e.ram_block, e.duration_ms].every(u32) || ![e.started, e.acked].every(v => v === 0 || v === 1)) return null;
    if (e.acked === 1 && e.outcome !== 'ack' || e.acked === 0 && e.outcome === 'ack') return null;
    const step = { step: e.step, segment: e.segment, address: e.address, size: e.size, ram_block: e.ram_block, started: e.started, acked: e.acked, duration_ms: e.duration_ms, outcome: e.outcome };
    for (const key of ['block', 'blocks']) if (e[key] !== undefined) { if (!u32(e[key])) return null; step[key] = e[key]; }
    out.push(step);
  }
  return out;
}
export function hardwareError(error, disconnected = false, details = {}) {
  // `error.diagnostic` is the structured, allowlisted origin report built by the P4 orchestrator (never the loader's own text).
  const origin = error?.diagnostic && typeof error.diagnostic === 'object' ? error.diagnostic : {};
  const input = { ...details, ...error?.detail, ...origin }, phase = phases.has(input.phase) ? input.phase : 'prepare';
  const kind = kinds.has(error?.kind) ? error.kind : kinds.has(input.transport_kind) ? input.transport_kind : null;
  const transfer = phase === 'artifact_upload' || phase === 'artifact_download';
  const code = codes.has(error?.code) || diagnosticCode(error?.code) ? error.code : transfer ? 'artifact_transfer_failed'
    : disconnected || kind === 'closed' || error?.name === 'NetworkError' ? 'serial_disconnected'
    : ({ 'no-response': 'serial_timeout', 'partial-packet': 'serial_partial_packet', 'invalid-packet': 'serial_invalid_packet' }[kind] || 'hardware_command_failed');
  const out = { code, phase, error_type: types.has(error?.name) ? error.name : 'Error' };
  if (kind) out.transport_kind = kind;
  if (categories.has(input.category)) out.category = input.category;
  if (ramLoad.has(input.ram_load)) out.ram_load = input.ram_load;
  if (diagnosticCode(input.origin_code)) out.origin_code = input.origin_code;
  if (types.has(input.origin_error_type)) out.origin_error_type = input.origin_error_type;
  if (['visible', 'hidden', 'prerender', 'unavailable'].includes(input.visibility)) out.visibility = input.visibility;
  // Raw observations of the ROM/stub admission: eight signed 32-bit sync values at most, a fixed vocabulary for the MD5 shape.
  if (Array.isArray(input.sync_values) && input.sync_values.length <= 8 && input.sync_values.every(v => Number.isInteger(v) && v >= -0x80000000 && v <= 0x7fffffff))
    out.sync_values = [...input.sync_values];
  if (md5Shapes.has(input.md5_shape)) out.md5_shape = input.md5_shape;
  if (['usb_otg_cap', 'default'].includes(input.ram_block_source)) out.ram_block_source = input.ram_block_source;
  if (['rom', 'stub', 'indeterminate'].includes(input.admission_verdict)) out.admission_verdict = input.admission_verdict;
  const ramSteps = ramStepsOf(input.ram_steps); if (ramSteps) out.ram_steps = ramSteps;
  for (const key of counters) if (Number.isFinite(input[key]) && input[key] >= 0) out[key] = Math.round(input[key]);
  return out;
}
export const diagnostic = code => ({
  physical_identity_unverified: 'Identité physique non vérifiable (adresse MAC illisible ou invalide) : connexion refusée.',
  diagnostic_transport_unconfirmed: 'Transport USB OTG non confirmé positivement : chargement RAM refusé (aucun octet RAM envoyé) ; la carte reste sous quarantaine.',
  diagnostic_ram_block_unexpected: 'La taille de bloc USB OTG de la bibliothèque diffère de la valeur revue (0x800) : chargement RAM refusé.',
  diagnostic_ram_load_already_started: 'Un chargement RAM a déjà commencé sur ce chargeur : aucune nouvelle tentative.',
  diagnostic_stub_requires_a_rom_session: 'Ancien code (runs 001-002) : un stub était signalé en marche ; l’indice n’était pas démontré. Rien n’a été chargé.',
  diagnostic_stub_reported_running: 'Tous les indices frais indiquent qu’un stub tourne (aucune preuve absolue) : rien n’a été chargé ; BOOT + reset requis.',
  diagnostic_session_state_indeterminate: 'Indices ROM/stub contradictoires ou incomplets : état de la session indéterminé, rien n’a été chargé ; la carte reste sous quarantaine.',
  diagnostic_admission_sync_failed: 'Synchronisation d’admission impossible : état ROM/stub inobservable, rien n’a été chargé ; la carte reste sous quarantaine.',
  diagnostic_stub_wrong_chip: 'Puce différente d’un ESP32-P4 : rien n’a été chargé.',
  diagnostic_stub_unsupported_chip_revision: 'Révision de puce non prise en charge (≥ 3.00 requise) : rien n’a été chargé.',
  diagnostic_stub_ram_load_failed: 'Chargement RAM du stub interrompu : état de la session indéterminé.',
  diagnostic_stub_no_greeting: 'Le stub chargé n’a pas répondu : état de la session indéterminé.',
  diagnostic_stub_bad_greeting: 'Réponse inattendue après le chargement : état de la session indéterminé.',
  diagnostic_stub_not_identified: 'Le stub diagnostique n’a pas été identifié : état de la session indéterminé.',
  diagnostic_consent_invalid: 'Consentement du diagnostic absent ou ne correspondant pas à la carte et au stub.',
  diagnostic_parameter_not_allowed: 'Paramètre du diagnostic différent de la valeur figée.',
  diagnostic_recovery_state_indeterminate: 'Indices ROM/stub contradictoires ou incomplets : le retour en ROM n’est pas démontré ; la carte reste sous quarantaine.',
  diagnostic_recovery_sync_failed: 'Synchronisation de preuve impossible : le retour en ROM n’est pas démontré ; la carte reste sous quarantaine.',
  diagnostic_stub_still_running: 'Un stub tourne encore : le retour en ROM n’est pas démontré.',
  diagnostic_rom_only_required: 'Le diagnostic exige une connexion « ROM seule » (option avancée de connexion).',
  diagnostic_rom_only_session: 'Session « ROM seule » : seules l’identification, les repères et le diagnostic P4 sont disponibles.',
  manual_download_required: 'Entrée manuelle en mode ROM requise (case « BOOT manuel »).',
  idlab_trace_md5_mismatch: 'MD5 des octets reçus différent de celui du stub : session indéterminée.',
  idlab_trace_stream_failed: 'Flux de lecture diagnostique interrompu : session indéterminée.',
  loader_baud_invalid: 'Vitesse du chargeur UART invalide.',
  serial_disconnected: 'Port série déconnecté pendant l’opération.',
  serial_timeout: 'Lecture série interrompue : aucun nouvel octet avant le délai de silence.',
  serial_partial_packet: 'Lecture série interrompue : paquet incomplet avant le délai de silence.',
  serial_invalid_packet: 'Paquet série invalide : erreur de trame SLIP.',
  short_flash_read: 'Lecture flash incomplète : aucun dump partiel publié.',
  flash_capacity_unknown: 'Capacité flash JEDEC inconnue : identifiant brut et rang de lecture disponibles dans le diagnostic.',
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
  flash_erase: 'Effacement', console: 'Console', control: 'Commande de contrôle',
  diagnostic_stub_load: 'Chargement RAM du stub', diagnostic_read: 'Lecture diagnostique' }[phase] || 'Préparation');
