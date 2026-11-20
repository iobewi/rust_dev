import { validateImages } from './protocol.js';
export function address(value) {
  const text = String(value).trim();
  if (!/^(?:0x[0-9a-f]+|[0-9]+)$/i.test(text)) throw new Error('Adresse ou taille invalide');
  const result = Number(text);
  if (!Number.isSafeInteger(result) || result < 0) throw new Error('Adresse ou taille invalide');
  return result;
}
export const hex = value => '0x' + value.toString(16).padStart(8, '0');
export function flashPlan(entries, capacity) {
  const images = entries.map(entry => ({ artifact: entry.artifact, sha256: entry.sha256, address: address(entry.address) }));
  if (entries.some(entry => !entry.artifact || !/^[a-f0-9]{64}$/.test(entry.sha256) || !Number.isSafeInteger(entry.size))) {
    throw new Error('Artefact absent ou non vérifié');
  }
  validateImages(entries.map((entry, i) => ({ address: images[i].address, data: { length: entry.size } })), capacity);
  return images;
}
export function requireTarget(lease, selected) {
  if (!lease) throw new Error('Prenez le pilotage manuel de la carte');
  if (selected !== lease.device) throw new Error('La carte sélectionnée diffère de la carte réservée');
  return lease;
}

// Presentation-only fallback: server identity may outlive the local Web Serial attachment.
// This must never be used to authorize hardware reconnects or validate a physical device.
export function displayChip(localIdentity, serverDevice) {
  return localIdentity?.chip || serverDevice?.chip || 'MCU inconnu';
}

// ---- target of a manual command: identity (connected board) is separate from the flash capacity ----
// Only these commands really depend on a known flash capacity (bounds, partition table, images, erase). identify, mark and the
// P4 diagnostic (fixed region, read-only) never do: a ROM-only session with an unknown capacity (flash_size null) can use them.
export const CAPACITY_COMMANDS = new Set(['flash.read', 'partitions', 'flash.write', 'flash.erase']);
// The connected board a command is aimed at, whatever its capacity (flash_size is null when unknown, never guessed).
export function connectedTarget(device, { session, board } = {}) {
  if (!session || !device?.connected || (board && board.status !== 'connected')) throw new Error('Connectez une carte au banc');
  return { device: device.id, chip: device.chip, flash_size: Number.isInteger(device.flash_size) && device.flash_size > 0 ? device.flash_size : null, generation: device.generation };
}
export function requireKnownCapacity(target) {
  if (!Number.isInteger(target.flash_size) || target.flash_size <= 0) throw new Error('Capacité flash inconnue : cette opération est indisponible sur cette session');
  return target;
}
// capacityNeeded: true for image preparation / write / erase handlers, or a command name for submit().
export function targetFor(device, context, capacityNeeded = false) {
  const target = connectedTarget(device, context);
  const needed = typeof capacityNeeded === 'string' ? CAPACITY_COMMANDS.has(capacityNeeded) : capacityNeeded === true;
  return needed ? requireKnownCapacity(target) : target;
}
