import { sha256, manifestPlan } from './protocol.js';
export class BenchClient {
  constructor(token = null) { this.token = token; }
  async request(path, { method = 'GET', data, bytes, headers = {} } = {}) {
    const response = await fetch('/api/v1/' + path, {
      method, headers: { ...(this.token ? { Authorization: 'Bearer ' + this.token } : {}), ...headers,
        ...(data ? { 'Content-Type': 'application/json' } : {}) },
      ...(data ? { body: JSON.stringify(data) } : {}), ...(bytes ? { body: bytes } : {}),
    });
    if (!response.ok) throw new Error((await response.json()).error?.code || 'API error');
    return response.json();
  }
  async upload(bytes) {
    return this.request('artifacts', { method: 'POST', bytes, headers: { 'X-SHA256': await sha256(bytes) } });
  }
  async download(artifact) {
    const response = await fetch('/api/v1/artifacts/' + encodeURIComponent(artifact.id), { headers: this.token ? { Authorization: 'Bearer ' + this.token } : {} });
    if (!response.ok) throw new Error('Relecture indisponible');
    const data = new Uint8Array(await response.arrayBuffer());
    if (data.length !== artifact.size || await sha256(data) !== artifact.sha256) throw new Error('Intégrité de la relecture invalide');
    return data;
  }
  async *events(device, after, signal) {
    const response = await fetch('/api/v1/devices/' + encodeURIComponent(device) + '/events/stream?after=' + after,
      { headers: this.token ? { Authorization: 'Bearer ' + this.token } : {}, signal });
    if (!response.ok) throw new Error('Abonnement UART refusé');
    const reader = response.body.getReader(), decoder = new TextDecoder(); let pending = '';
    try {
      for (;;) {
        const { value, done } = await reader.read();
        if (done) throw new Error('Flux UART interrompu');
        pending += decoder.decode(value, { stream: true });
        let end;
        while ((end = pending.indexOf('\n\n')) !== -1) {
          const event = pending.slice(0, end); pending = pending.slice(end + 2);
          if (event.startsWith('data: ')) yield JSON.parse(event.slice(6));
        }
        if (pending.length > 128 * 1024) throw new Error('Événement UART trop volumineux');
      }
    } finally { await reader.cancel().catch(() => {}); reader.releaseLock(); }
  }
  async installManifest(lease, manifest, chip, files, allowDestructive) {
    return this.request('operations', { method: 'POST', data: {
      lease, command: 'flash.write', args: manifestPlan(manifest, chip, files), allow_destructive: allowDestructive,
    } });
  }
}
