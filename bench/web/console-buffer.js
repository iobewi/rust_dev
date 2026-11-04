// Raw capture and presentation are separate: clearing or filtering never deletes captured bytes.
export class ConsoleBuffer {
  constructor({ maxBytes = 1024 * 1024, maxEvents = 4096 } = {}) {
    this.maxBytes = maxBytes; this.maxEvents = maxEvents;
    this.events = []; this.byteLength = 0; this.sequence = 0; this.displayAfter = 0;
    this.droppedBytes = 0; this.droppedEvents = 0; this.decoder = new TextDecoder();
  }
  append(bytes, { at = Date.now(), kind = 'RX', message = '' } = {}) {
    let data = new Uint8Array(bytes);
    if (data.length > this.maxBytes) {
      this.droppedBytes += data.length - this.maxBytes;
      data = data.slice(-this.maxBytes); this.decoder = new TextDecoder();
    }
    const event = { seq: ++this.sequence, at, kind, bytes: data,
      text: kind === 'RX' ? this.decoder.decode(data, { stream: true }) : message };
    this.events.push(event); this.byteLength += data.length;
    while (this.byteLength > this.maxBytes || this.events.length > this.maxEvents) {
      const removed = this.events.shift(); this.byteLength -= removed.bytes.length;
      this.droppedBytes += removed.bytes.length; this.droppedEvents++;
    }
    return event;
  }
  note(kind, message, at) { return this.append(new Uint8Array(), { kind, message, at }); }
  clearDisplay() { this.displayAfter = this.sequence; }
  visible({ format = 'text', query = '', limit = 250, before = Infinity } = {}) {
    const needle = query.toLocaleLowerCase();
    const escape = text => text.replace(/[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]/g,
      c => '\\x' + c.charCodeAt(0).toString(16).padStart(2, '0'));
    const rows = [];
    const emit = row => {
      if (!row.display.toLocaleLowerCase().includes(needle)) return;
      rows.push(row);
      // A newline flood must not allocate one DOM/presentation record per byte.
      if (rows.length >= limit * 2) rows.splice(0, limit);
    };
    let line = null, afterCR = false;
    for (const event of this.events) {
      if (event.seq <= this.displayAfter || event.seq > before) continue;
      if (format === 'hex' || event.kind !== 'RX') {
        if (line?.display) emit({ ...line, display: escape(line.display) });
        line = null; afterCR = false;
        emit({ seq: event.seq, at: event.at, kind: event.kind,
          display: format === 'hex' && event.bytes.length
            ? [...event.bytes].map(b => b.toString(16).padStart(2, '0')).join(' ')
            : escape(event.text) });
        continue;
      }
      // Serial reads are arbitrary chunks, not firmware lines. Keep the first
      // fragment's timestamp and show unfinished lines immediately.
      for (const char of event.text) {
        if (afterCR) { afterCR = false; if (char === '\n') continue; }
        line ||= { seq: event.seq, at: event.at, kind: 'RX', display: '' };
        if (char === '\r' || char === '\n') {
          emit({ ...line, display: escape(line.display) });
          line = null; afterCR = char === '\r';
        } else line.display += char;
      }
      // A split UTF-8 character may have bytes but no decoded text yet.
      if (!line && !event.text && event.bytes.length) line = { seq: event.seq, at: event.at, kind: 'RX', display: '' };
    }
    if (line?.display) emit({ ...line, display: escape(line.display) });
    return rows.slice(-limit);
  }
  bytes() {
    const out = new Uint8Array(this.byteLength); let offset = 0;
    for (const event of this.events) { out.set(event.bytes, offset); offset += event.bytes.length; }
    return out;
  }
}
