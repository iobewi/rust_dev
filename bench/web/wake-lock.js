// A screen lock is advisory and only available while the document is visible.
export class WorkWakeLock {
  constructor({ wakeLock, visible, onState = () => {} }) {
    Object.assign(this, { wakeLock, visible, onState }); this.active = false; this.lock = null; this.pending = null;
  }
  async update(active = this.active, retry = false) {
    if (active !== this.active || retry) this.denied = false;
    this.active = active;
    if (!active || !this.visible()) {
      const lock = this.lock; this.lock = null;
      await lock?.release().catch(() => {});
      this.onState(active ? 'Veille : onglet masqué' : 'Veille : normale'); return;
    }
    if (!this.wakeLock) { this.onState('Anti-veille indisponible'); return; }
    if (this.lock || this.pending || this.denied) return;
    this.pending = this.wakeLock.request('screen');
    try {
      const lock = await this.pending;
      if (!this.active || !this.visible()) { await lock.release(); return; }
      this.lock = lock; this.onState('Écran maintenu allumé');
      lock.addEventListener('release', () => {
        if (this.lock !== lock) return;
        this.lock = null; this.denied = true; this.onState('Anti-veille interrompue');
      });
    } catch { this.denied = true; this.onState('Anti-veille refusée par le navigateur'); }
    finally { this.pending = null; }
  }
}
