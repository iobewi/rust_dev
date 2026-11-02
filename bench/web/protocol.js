export const b64 = bytes => {
  let s = ''; for (const n of bytes) s += String.fromCharCode(n);
  return btoa(s);
};
export const unb64 = value => Uint8Array.from(atob(value), c => c.charCodeAt(0));
export const sha256 = async bytes => [...new Uint8Array(await crypto.subtle.digest('SHA-256', bytes))].map(n => n.toString(16).padStart(2, '0')).join('');
export const family = name => String(name).toLowerCase().replace(/[^a-z0-9]/g, '');
export function region(address, size, capacity) {
  if (!Number.isSafeInteger(address) || !Number.isSafeInteger(size) || address < 0 || size <= 0 || address + size > capacity) throw new Error('Flash region out of bounds');
}
export function validateImages(images, capacity) {
  if (!Array.isArray(images) || !images.length || images.length > 16) throw new Error('Invalid flash plan');
  const spans = [];
  for (const image of images) {
    region(image.address, image.data.length, capacity);
    const start = Math.floor(image.address / 4096) * 4096;
    const end = Math.ceil((image.address + image.data.length) / 4096) * 4096;
    if (spans.some(([a, b]) => start < b && a < end)) throw new Error('Flash erase sectors overlap');
    spans.push([start, end]);
  }
}
export function partitions(bytes, capacity) {
  const view = new DataView(bytes.buffer, bytes.byteOffset, bytes.byteLength), rows = [];
  for (let i = 0; i + 32 <= bytes.length; i += 32) {
    const magic = view.getUint16(i, true);
    if (magic === 0xffff || magic === 0xebeb) break;
    if (magic !== 0x50aa) throw new Error('Invalid partition table magic');
    const address = view.getUint32(i + 4, true), size = view.getUint32(i + 8, true);
    region(address, size, capacity);
    rows.push({ label: new TextDecoder().decode(bytes.slice(i + 12, i + 28)).replace(/\0.*$/s, ''),
      type: bytes[i + 2], subtype: bytes[i + 3], address, size, flags: view.getUint32(i + 28, true) });
  }
  return rows;
}
// Manifests only resolve already-uploaded local files. No HTTP fetch, URL, path traversal or implicit erase.
export function manifestPlan(manifest, chip, files) {
  if (!manifest || !Array.isArray(manifest.builds)) throw new Error('Invalid ESP Web Tools manifest');
  const builds = manifest.builds.filter(b => family(b.chipFamily) === family(chip));
  if (builds.length !== 1 || !Array.isArray(builds[0].parts) || !builds[0].parts.length) throw new Error('Expected one matching chip build');
  const images = builds[0].parts.map(part => {
    if (typeof part.path !== 'string' || part.path.startsWith('/') || part.path.includes('..') || /[:\\?#]/.test(part.path)) throw new Error('Unsafe manifest path');
    const file = files.get(part.path);
    if (!file || !Number.isSafeInteger(part.offset) || part.offset < 0) throw new Error('Missing manifest image or invalid offset: ' + part.path);
    return { artifact: file.id, sha256: file.sha256, address: part.offset };
  });
  return { chip, images, erase: false, return_console: true };
}
