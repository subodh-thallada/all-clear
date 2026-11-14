import { useEffect, useMemo, useSyncExternalStore } from 'react';
import { createEngine } from '../sim/engine.js';
import { createLive } from './live.js';

// Where the wall's state comes from.
//
//   live      the API server: GET /wall/state once a second. This is what the
//             page does when the API serves it at /wall.
//   fixture   the engine in src/sim, scripted in this browser. The default on
//             the dev server and from disk; also ?source=fixture (or ?mock=1).
//
// Live can also be forced with ?source=live, ?api=https://host, or VITE_API_URL.
// A live screen never falls back to demo data: if the API stops answering, it says so.

export function pickSource() {
  const q = new URLSearchParams(window.location.search);
  const api = (q.get('api') || import.meta.env.VITE_API_URL || '').replace(/\/$/, '');
  const forced = q.get('source');
  if (forced === 'fixture' || q.get('mock') === '1') return { kind: 'fixture' };
  // Served by the API at /wall, this is the room's screen: it reads the real run.
  const served = window.location.pathname.replace(/\/$/, '') === '/wall';
  if (forced === 'live' || api || served) return { kind: 'live', api };
  return { kind: 'fixture' };
}

export function useWall() {
  const store = useMemo(() => {
    const src = pickSource();
    const made = src.kind === 'live' ? createLive(src.api) : createEngine();
    window.__allclear = made; // for rehearsal and tests: window.__allclear.actions, .getSnapshot()
    return made;
  }, []);
  useEffect(() => {
    store.start();
    return () => store.stop();
  }, [store]);
  const state = useSyncExternalStore(store.subscribe, store.getSnapshot);
  return { state, actions: store.actions };
}
