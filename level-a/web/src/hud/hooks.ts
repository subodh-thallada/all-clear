import { useCallback, useEffect, useLayoutEffect, useRef, useState } from "react";
import { watchMock } from "../api/client";

/**
 * Poll `load` every `intervalMs`, one request at a time. Keeps the last good value through
 * errors (the screen must not blank on a dropped poll) and reports when it last succeeded.
 */
export function usePolling<T>(load: (signal: AbortSignal) => Promise<T>, intervalMs: number) {
  const [data, setData] = useState<T | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [lastOk, setLastOk] = useState<number | null>(null);
  const loadRef = useRef(load);
  loadRef.current = load;
  const kick = useRef<() => void>(() => undefined);

  useEffect(() => {
    let stopped = false;
    let timer: number | undefined;
    let controller: AbortController | null = null;
    const tick = async () => {
      window.clearTimeout(timer);
      controller?.abort();
      const mine = new AbortController();
      controller = mine;
      try {
        const value = await loadRef.current(mine.signal);
        if (stopped || mine.signal.aborted) return;
        setData(value);
        setError(null);
        setLastOk(Date.now());
      } catch (err) {
        if (stopped || mine.signal.aborted) return;
        setError(err instanceof Error ? err.message : String(err));
      }
      if (!stopped) timer = window.setTimeout(tick, intervalMs);
    };
    kick.current = () => void tick();
    void tick();
    const unwatch = watchMock(() => void tick());
    return () => {
      stopped = true;
      window.clearTimeout(timer);
      controller?.abort();
      unwatch();
    };
  }, [intervalMs]);

  const refresh = useCallback(() => kick.current(), []);
  return { data, error, lastOk, refresh };
}

export function usePrefersReducedMotion(): boolean {
  const query = "(prefers-reduced-motion: reduce)";
  const [reduce, setReduce] = useState(() => window.matchMedia?.(query).matches ?? false);
  useEffect(() => {
    const mq = window.matchMedia?.(query);
    if (!mq) return;
    const on = () => setReduce(mq.matches);
    mq.addEventListener("change", on);
    return () => mq.removeEventListener("change", on);
  }, []);
  return reduce;
}

/**
 * Shrink a single-line element's font until its text fits its box. The stylesheet sets the
 * largest size (it differs between the full and the compact layout); minPx is the floor.
 */
export function useFitText<T extends HTMLElement>(text: string, minPx: number) {
  const ref = useRef<T>(null);
  useLayoutEffect(() => {
    const el = ref.current;
    if (!el) return;
    const fit = () => {
      el.style.fontSize = "";
      const box = el.clientWidth;
      const need = el.scrollWidth;
      if (box > 0 && need > box) {
        const base = parseFloat(window.getComputedStyle(el).fontSize);
        el.style.fontSize = `${Math.max(minPx, Math.floor(((base * box) / need) * 10) / 10 - 0.3)}px`;
      }
    };
    fit();
    const ro = new ResizeObserver(fit);
    ro.observe(el);
    void document.fonts?.ready.then(fit);
    return () => ro.disconnect();
  }, [text, minPx]);
  return ref;
}
