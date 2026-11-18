// The app's state, shared by the shell and every view. Views subscribe while they are
// mounted; `update()` merges a patch and tells all subscribers.

export const HISTORY = 60; // throughput samples kept (one per second)

export const state = {
  status: null, // GET /api/status
  config: null, // GET /api/config
  outputs: [], // GET /api/outputs
  setup: null, // GET /api/setup (receiver "headless")
  offline: false, // the last status request failed
  history: [], // throughput in kbit/s, one sample per poll
  kbps: 0,
  peak: 0,
  dirty: false, // unsaved edits in the Settings view
};

const listeners = new Set();

export function subscribe(listener) {
  listeners.add(listener);
  return () => listeners.delete(listener);
}

export function update(patch = {}) {
  Object.assign(state, patch);
  for (const listener of [...listeners]) {
    try {
      listener(state);
    } catch (err) {
      console.error("view update failed:", err);
    }
  }
}

// The speakers as the engine has them now: volume and mute can change live, so a
// stale copy of the config must never overwrite a newer value.
export function liveSpeakers() {
  return (state.status?.speakers || []).map((s) => ({
    match: s.match,
    delay_ms: s.delay_ms,
    volume: s.volume,
    muted: s.muted,
  }));
}
