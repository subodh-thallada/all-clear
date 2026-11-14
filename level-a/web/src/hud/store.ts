// UI state that is not the server's: the selected party, operator toasts, the calm toggle.

import { create } from "zustand";
import type { OperatorResult } from "../api/types";

export interface Toast {
  id: number;
  title: string;
  result: OperatorResult;
}

interface UiState {
  selected: string | null;
  select: (partyId: string | null) => void;
  toasts: Toast[];
  pushToast: (title: string, result: OperatorResult) => void;
  dismissToast: (id: number) => void;
  calmToggle: boolean;
  setCalmToggle: (on: boolean) => void;
}

const CALM_KEY = "allclear.calm";
const TOAST_MS = { ok: 7000, refused: 14000 };

function readCalm(): boolean {
  try {
    return window.localStorage.getItem(CALM_KEY) === "1";
  } catch {
    return false;
  }
}

let nextToast = 1;

export const useUi = create<UiState>((set, get) => ({
  selected: null,
  select: (partyId) => set({ selected: partyId }),
  toasts: [],
  pushToast: (title, result) => {
    const id = nextToast++;
    set({ toasts: [...get().toasts.slice(-2), { id, title, result }] });
    window.setTimeout(() => get().dismissToast(id), result.ok ? TOAST_MS.ok : TOAST_MS.refused);
  },
  dismissToast: (id) => set({ toasts: get().toasts.filter((t) => t.id !== id) }),
  calmToggle: readCalm(),
  setCalmToggle: (on) => {
    try {
      window.localStorage.setItem(CALM_KEY, on ? "1" : "0");
    } catch {
      // A per-viewer convenience only.
    }
    set({ calmToggle: on });
  },
}));
