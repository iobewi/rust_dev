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
    return this.events.filter(event => event.seq > this.displayAfter && event.seq <= before).map(event => ({
      ...event, display: format === 'hex' && event.bytes.length
        ? [...event.bytes].map(b => b.toString(16).padStart(2, '0')).join(' ')
        : event.text.replace(/[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]/g, c => '\\x' + c.charCodeAt(0).toString(16).padStart(2, '0')),
    })).filter(event => event.display.toLocaleLowerCase().includes(needle)).slice(-limit);
  }
  bytes() {
    const out = new Uint8Array(this.byteLength); let offset = 0;
    for (const event of this.events) { out.set(event.bytes, offset); offset += event.bytes.length; }
    return out;
  }
}
