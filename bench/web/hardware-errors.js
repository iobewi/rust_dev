// Only known diagnostics cross the protocol: never include arbitrary loader/UART text.
export class HardwareError extends Error {
  constructor(code) { super(code); this.code = code; }
}
export function hardwareError(error, disconnected = false) {
  const codes = new Set(['console_required', 'external_uart_required', 'partition_table_invalid', 'physical_identity_changed', 'operator_recovery_required']);
  return { code: disconnected || error?.name === 'NetworkError' ? 'serial_disconnected' : codes.has(error?.code) ? error.code : 'hardware_command_failed' };
}
export const diagnostic = code => ({
  serial_disconnected: 'Port série déconnecté pendant l’opération.',
  console_required: 'Action impossible en mode chargeur : revenez en console.',
  external_uart_required: 'Cette action nécessite un pont UART externe ; EN/IO0 ne sont pas disponibles sur USB natif.',
  partition_table_invalid: 'Aucune table de partitions valide à cette adresse. Vérifiez son emplacement dans le projet (par exemple 0x10000) ; 0x8000 n’est pas universel.',
  physical_identity_changed: 'L’identité physique de la carte a changé : choisissez explicitement le port.',
  operator_recovery_required: 'Récupération opérateur requise avant de rouvrir la console.',
  program_permission_required: 'Le client ne possède pas le droit de programmation.',
  destructive_consent_required: 'Le consentement explicite à l’écriture ou à l’effacement est absent.',
  hardware_command_failed: 'Échec de la commande matérielle. Consultez le journal de l’agent.',
}[code] || code);
