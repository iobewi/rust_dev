import { sha256, manifestPlan } from './protocol.js';
export class BenchClient {
  constructor(token) { this.token = token; }
  async request(path, { method = 'GET', data, bytes, headers = {} } = {}) {
    const response = await fetch('/api/v1/' + path, {
      method, headers: { Authorization: 'Bearer ' + this.token, ...headers,
        ...(data ? { 'Content-Type': 'application/json' } : {}) },
      ...(data ? { body: JSON.stringify(data) } : {}), ...(bytes ? { body: bytes } : {}),
    });
    if (!response.ok) throw new Error((await response.json()).error?.code || 'API error');
    return response.json();
  }
  async upload(bytes) {
    return this.request('artifacts', { method: 'POST', bytes, headers: { 'X-SHA256': await sha256(bytes) } });
  }
  async installManifest(lease, manifest, chip, files, allowDestructive) {
    return this.request('operations', { method: 'POST', data: {
      lease, command: 'flash.write', args: manifestPlan(manifest, chip, files), allow_destructive: allowDestructive,
    } });
  }
}
