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
