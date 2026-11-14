import { useCallback, useEffect, useLayoutEffect, useRef, useState } from "react";
import { MOCK, getWallState } from "../api/client";
import CityScene from "../scene/CityScene";
import Feed from "./Feed";
import Header from "./Header";
import Inspector from "./Inspector";
import Metrics from "./Metrics";
import Monitoring from "./Monitoring";
import Operator from "./Operator";
import Systems from "./Systems";
import Toasts from "./Toasts";
import Workers from "./Workers";
import { usePolling, usePrefersReducedMotion } from "./hooks";
import { useUi } from "./store";

type Insets = { top: number; right: number; bottom: number; left: number };

/** The inspector narrows the city only where the centre is wide enough to afford it. */
const MIN_CITY_WIDTH = 480;

function sameInsets(a: Insets, b: Insets): boolean {
  return a.top === b.top && a.right === b.right && a.bottom === b.bottom && a.left === b.left;
}

export default function App() {
  const { data: state, error, lastOk, refresh } = usePolling(getWallState, 1000);
  const selected = useUi((s) => s.selected);
  const select = useUi((s) => s.select);
  const calmToggle = useUi((s) => s.calmToggle);
  const reducedMotion = usePrefersReducedMotion();
  const calm = reducedMotion || calmToggle;
  const admin = !!state?.admin;

  const hudRef = useRef<HTMLDivElement>(null);
  const [insets, setInsets] = useState<Insets>({ top: 0, right: 0, bottom: 0, left: 0 });

  const measure = useCallback(() => {
    const hud = hudRef.current;
    if (!hud) return;
    const rect = (sel: string) => hud.querySelector(sel)?.getBoundingClientRect() ?? null;
    const vw = window.innerWidth;
    const vh = window.innerHeight;
    const top = rect(".top");
    const left = rect(".col-left");
    const right = rect(".col-right");
    const bottom = rect(".bottom");
    const inspector = rect(".inspector");
    let rightEdge = right ? right.left : vw;
    const leftEdge = left ? left.right : 0;
    if (inspector && inspector.left - leftEdge >= MIN_CITY_WIDTH) rightEdge = Math.min(rightEdge, inspector.left);
    const next: Insets = {
      top: Math.round(top ? top.bottom : 0),
      left: Math.round(leftEdge),
      right: Math.round(vw - rightEdge),
      bottom: Math.round(bottom ? vh - bottom.top : 0),
    };
    setInsets((prev) => (sameInsets(prev, next) ? prev : next));
  }, []);

  useLayoutEffect(() => {
    const hud = hudRef.current;
    if (!hud) return;
    measure();
    const ro = new ResizeObserver(measure);
    for (const el of Array.from(hud.children)) ro.observe(el);
    window.addEventListener("resize", measure);
    return () => {
      ro.disconnect();
      window.removeEventListener("resize", measure);
    };
  }, [measure, admin, !!state]);

  useLayoutEffect(measure, [measure, selected, state]);

  useEffect(() => {
    if (state?.headline) document.title = `All Clear · ${state.headline}`;
  }, [state?.headline]);

  const stale = error && state
    ? `API unreachable · last update ${lastOk ? Math.max(1, Math.round((Date.now() - lastOk) / 1000)) : "?"} s ago`
    : null;

  return (
    <div className={`screen${calm ? " calm" : ""}`}>
      <div className="stage">
        <CityScene state={state} selectedPartyId={selected} onSelectParty={select} insets={insets} calm={calm} />
      </div>
      <div ref={hudRef} className={`hud${admin ? "" : " viewer"}`}>
        <Header state={state} stale={stale} calm={calm} reducedMotion={reducedMotion} />
        <div className="col-left">
          <Systems state={state} />
          <Monitoring monitoring={state?.monitoring ?? null} admin={admin} onAction={refresh} />
          <Workers state={state} />
        </div>
        <section className="center" aria-label="The recall as a city">
          <Inspector state={state} />
          <Toasts />
          {!state && (
            <div className="connecting">
              <p className="glass" role="status">
                {error ? `Waiting for the API: ${error}` : MOCK ? "Starting the mock run…" : "Connecting to the API…"}
              </p>
            </div>
          )}
        </section>
        <Metrics metrics={state?.metrics ?? null} />
        <div className="col-right">
          <Feed feed={state?.feed ?? []} parties={state?.parties ?? []} />
        </div>
        {admin && state && <Operator state={state} onAction={refresh} />}
      </div>
    </div>
  );
}
