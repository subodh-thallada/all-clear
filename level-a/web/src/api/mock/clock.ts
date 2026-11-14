// The mock run's clock. Mock time is milliseconds since the run began (it advances with real time
// unless the mock is frozen); simulated time is hours since day 0 00:00. The two are related by a
// piecewise-linear schedule: slow while the signal is raised, fast through the long run, and at
// the real run's 30 s per simulated hour while the live slice plays. In "hold" mode the clock
// also waits at 08:05 on day 0 until a person approves the signal.

export const SIM_START = 6; // d0 06:00
export const APPROVE_AT = 8 + 5 / 60; // d0 08:05, when operator-1 approves in the scripted run
export const SLICE_AT = 229; // d9 13:00
export const RUN_END = 240; // d10 00:00
/** The real run's speed: leases, ring times and "since" figures are quoted at this rate. */
export const REFERENCE_S_PER_HOUR = 30;

interface Segment {
  until: number; // sim hour where the segment ends
  speed: number; // real seconds per simulated hour
}

const AUTO: Segment[] = [
  { until: 7.8, speed: 4 },
  { until: 8.25, speed: 30 },
  { until: SLICE_AT, speed: 0.5 },
  { until: SLICE_AT + 4, speed: 30 },
  { until: RUN_END, speed: 1 },
];

const HOLD: Segment[] = [
  { until: 7.8, speed: 4 },
  { until: 8.25, speed: 30 },
  { until: SLICE_AT, speed: 0.5 },
  { until: RUN_END, speed: 30 },
];

export function hourLabel(h: number): string {
  const total = Math.max(0, Math.round(h * 60));
  const day = Math.floor(total / 1440);
  const mins = total % 1440;
  return `d${day} ${String(Math.floor(mins / 60)).padStart(2, "0")}:${String(mins % 60).padStart(2, "0")}`;
}

/**
 * simAt(ms) and msAt(sim) for one run. `approvalMs` is when a person approved the signal in hold
 * mode (null until they do: the clock then stays at 08:05).
 */
export class MockClock {
  private readonly segs: Segment[];
  private readonly pauseAt: number | null;
  private readonly pauseMs: number;

  constructor(hold: boolean, approvalMs: number | null) {
    this.segs = hold ? HOLD : AUTO;
    if (!hold) {
      this.pauseAt = null;
      this.pauseMs = 0;
    } else {
      const reach = this.rawMsAt(APPROVE_AT);
      this.pauseAt = APPROVE_AT;
      this.pauseMs = approvalMs === null ? Infinity : Math.max(0, approvalMs - reach);
    }
  }

  /** Mock ms at which the schedule reaches `sim`, ignoring the approval pause. */
  private rawMsAt(sim: number): number {
    let ms = 0;
    let from = SIM_START;
    for (const s of this.segs) {
      if (sim <= s.until) return ms + Math.max(0, sim - from) * s.speed * 1000;
      ms += (s.until - from) * s.speed * 1000;
      from = s.until;
    }
    return ms;
  }

  private rawSimAt(ms: number): number {
    let at = 0;
    let from = SIM_START;
    for (const s of this.segs) {
      const span = (s.until - from) * s.speed * 1000;
      if (ms <= at + span) return from + (ms - at) / (s.speed * 1000);
      at += span;
      from = s.until;
    }
    return RUN_END;
  }

  simAt(ms: number): number {
    if (this.pauseAt === null) return this.rawSimAt(ms);
    const reach = this.rawMsAt(this.pauseAt);
    if (ms <= reach) return this.rawSimAt(ms);
    if (ms <= reach + this.pauseMs) return this.pauseAt;
    return this.rawSimAt(ms - this.pauseMs);
  }

  /** Mock ms when the run reaches `sim`; Infinity when it never will (waiting for approval). */
  msAt(sim: number): number {
    const raw = this.rawMsAt(Math.min(sim, RUN_END));
    if (this.pauseAt === null || sim <= this.pauseAt) return raw;
    return raw + this.pauseMs;
  }

  /** Real seconds per simulated hour at `ms`; 0 while the clock stands still. */
  speedAt(ms: number): number {
    const sim = this.simAt(ms);
    if (sim >= RUN_END) return 0;
    if (this.pauseAt !== null && sim === this.pauseAt && ms > this.rawMsAt(this.pauseAt)) return 0;
    return (this.segs.find((s) => sim < s.until) ?? this.segs[this.segs.length - 1]).speed;
  }

  /**
   * The ms of an event `dh` simulated hours after `ms`. When the clock stands still (the run has
   * ended, or waits for approval) follow-ups still happen, at the real run's speed.
   */
  after(ms: number, dh: number): number {
    const target = this.msAt(this.simAt(ms) + dh);
    if (Number.isFinite(target) && this.simAt(ms) + dh <= RUN_END && target >= ms) return target;
    return ms + dh * REFERENCE_S_PER_HOUR * 1000;
  }
}
