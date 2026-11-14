import { useEffect, useLayoutEffect, useRef, useState } from 'react';

export const reduced = () => typeof matchMedia === 'function' && matchMedia('(prefers-reduced-motion: reduce)').matches;

// A number rolls to its new value over 300 ms.
export function useTween(value, ms = 300) {
  const [shown, setShown] = useState(value);
  const from = useRef(value);
  useEffect(() => {
    if (reduced() || from.current === value) {
      from.current = value;
      setShown(value);
      return undefined;
    }
    const start = performance.now();
    const a = from.current;
    let raf;
    const step = (now) => {
      const k = Math.min(1, (now - start) / ms);
      const v = Math.round(a + (value - a) * k);
      from.current = v;
      setShown(v);
      if (k < 1) raf = requestAnimationFrame(step);
    };
    raf = requestAnimationFrame(step);
    return () => cancelAnimationFrame(raf);
  }, [value, ms]);
  return shown;
}

// Scale a fixed-size drawing to fit the box it sits in, like "fit view".
export function useFitBox(w, h, max = 1.25) {
  const ref = useRef(null);
  const [scale, setScale] = useState(1);
  useLayoutEffect(() => {
    const el = ref.current;
    if (!el) return undefined;
    const fit = () => setScale(Math.max(0.4, Math.min(max, el.clientWidth / w, el.clientHeight / h)));
    fit();
    const ro = new ResizeObserver(fit);
    ro.observe(el);
    return () => ro.disconnect();
  }, [w, h, max]);
  return [ref, scale];
}

export function useHotkeys(map) {
  const ref = useRef(map);
  ref.current = map;
  useEffect(() => {
    const onKey = (e) => {
      if (e.metaKey || e.ctrlKey || e.altKey) return;
      const tag = e.target?.tagName;
      if (tag === 'INPUT' || tag === 'TEXTAREA') return;
      const fn = ref.current[e.key === ' ' ? 'space' : e.key.toLowerCase()];
      if (fn) {
        e.preventDefault();
        fn();
      }
    };
    window.addEventListener('keydown', onKey);
    return () => window.removeEventListener('keydown', onKey);
  }, []);
}
