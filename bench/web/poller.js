// Periodic task that never overlaps itself: the next run starts `interval` ms after the previous one FINISHED,
// extra requests while one is running collapse into a single re-run, and it can be held back (e.g. during a
// flash operation) without losing the request.
export function makePoller(task, interval, env = {}) {
  const setT = env.setTimeout || ((f, ms) => setTimeout(f, ms));
  const clearT = env.clearTimeout || ((id) => clearTimeout(id));
  let running = false, again = false, timer = null, held = () => false, stopped = false;
  async function run() {
    clearT(timer); timer = null;
    if (stopped) return;
    if (running) { again = true; return; }
    if (held()) { schedule(); return; }
    running = true;
    try { await task(); } catch (e) { /* the task reports its own errors */ }
    running = false;
    if (again) { again = false; run(); } else schedule();
  }
  function schedule() { if (!stopped && !timer) timer = setT(run, interval); }
  return {
    start() { stopped = false; run(); },
    trigger() { run(); },
    hold(fn) { held = fn; },
    stop() { stopped = true; clearT(timer); timer = null; },
    get running() { return running; },
  };
}
