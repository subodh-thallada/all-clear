// Shared formatting. Simulated time is hours since the run began: 158 is day 6, 14:00.

export const pad = (n, w = 2) => String(n).padStart(w, '0');

// The nouns on screen follow the data. The food recall counts tins at stores;
// the earlier fire-alarm build of the API counts panels at parties.
export const WORDS = {
  tins: {
    unit: 'tin', units: 'tins', party: 'store', parties: 'stores', Party: 'Store', person: 'Store manager',
    shelf: 'on a shelf', removed: 'held or returned', mid: 'sold, counted by the till', shop: 'A named shop is a new store',
  },
  panels: {
    unit: 'panel', units: 'panels', party: 'party', parties: 'parties', Party: 'Party', person: 'Contact',
    shelf: 'awaiting fix', removed: 'fixed or returned', mid: 'in stock, counted', shop: 'A named party is a new party',
  },
};

export function simParts(h) {
  const day = Math.floor(h / 24);
  const hour = Math.floor(h % 24);
  const minute = Math.floor((h * 60) % 60);
  return { day, hour, minute };
}

export const simClock = (h) => {
  const { hour, minute } = simParts(h);
  return `${pad(hour)}:${pad(minute)}`;
};

export const simStamp = (h) => `d${simParts(h).day} ${simClock(h)}`;

export function hms(totalSeconds) {
  const s = Math.max(0, Math.floor(totalSeconds));
  return `${pad(Math.floor(s / 3600))}:${pad(Math.floor((s % 3600) / 60))}:${pad(s % 60)}`;
}

export function ms(totalSeconds) {
  const s = Math.max(0, Math.floor(totalSeconds));
  return `${Math.floor(s / 60)}:${pad(s % 60)}`;
}

export const num = (n) => Number(n ?? 0).toLocaleString('en-US');

export const plural = (n, one, many = `${one}s`) => `${num(n)} ${n === 1 ? one : many}`;
