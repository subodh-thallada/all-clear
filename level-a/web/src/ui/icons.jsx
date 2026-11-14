// Small line icons, drawn here so the page needs no icon package.
const I = ({ children, size = 18 }) => (
  <svg width={size} height={size} viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.9" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
    {children}
  </svg>
);

export const Bolt = (p) => <I {...p}><path d="M13 3 5 13.5h6L10 21l8-10.5h-6z" /></I>;
export const Database = (p) => <I {...p}><ellipse cx="12" cy="6" rx="7" ry="3" /><path d="M5 6v6c0 1.7 3.1 3 7 3s7-1.3 7-3V6" /><path d="M5 12v6c0 1.7 3.1 3 7 3s7-1.3 7-3v-6" /></I>;
export const History = (p) => <I {...p}><path d="M4 12a8 8 0 1 0 2.6-5.9" /><path d="M4 5v4h4" /><path d="M12 8v4.5l3 1.8" /></I>;
export const Phone = (p) => <I {...p}><path d="M6.5 3.5h3l1.5 4-2 1.5a12 12 0 0 0 6 6l1.5-2 4 1.5v3a2 2 0 0 1-2.2 2A17 17 0 0 1 4.5 5.700 2 2 0 0 1 6.500 3.500Z" /></I>;
export const Shield = (p) => <I {...p}><path d="M12 3 5 6v5.500c0 4.300 2.900 7.700 7 9.500 4.100-1.800 7-5.200 7-9.500V6z" /></I>;
export const ShieldX = (p) => <I {...p}><path d="M12 3 5 6v5.500c0 4.300 2.900 7.700 7 9.500 4.100-1.800 7-5.200 7-9.500V6z" /><path d="m9.500 9.500 5 5m0-5-5 5" /></I>;
export const Save = (p) => <I {...p}><rect x="4" y="4" width="16" height="16" rx="3" /><path d="m8.500 12.500 2.500 2.500 4.500-5" /></I>;
export const Branch = (p) => <I {...p}><circle cx="6" cy="5.500" r="2" /><circle cx="6" cy="18.500" r="2" /><circle cx="18" cy="8" r="2" /><path d="M6 7.500v9" /><path d="M18 10c0 4-6 3-6 6.500" transform="translate(-6 0)" /></I>;
export const Flag = (p) => <I {...p}><path d="M6 21V4" /><path d="M6 4.500h11l-2 4 2 4H6" /></I>;
export const Lock = (p) => <I {...p}><rect x="5" y="11" width="14" height="9" rx="2.500" /><path d="M8.500 11V8a3.500 3.500 0 0 1 7 0v3" /></I>;
export const Ask = (p) => <I {...p}><circle cx="12" cy="12" r="8.500" /><path d="M9.800 9.500a2.300 2.300 0 1 1 3.300 2.100c-.700.400-1.100.900-1.100 1.700" /><path d="M12 16.500v.100" /></I>;
export const Play = (p) => <I {...p}><path d="M8 5.500v13l10-6.500z" /></I>;
export const Stop = (p) => <I {...p}><rect x="6.500" y="6.500" width="11" height="11" rx="2" /></I>;
export const Send = (p) => <I {...p}><path d="M5 12h13" /><path d="m13 6.500 5.500 5.500-5.500 5.500" /></I>;
export const Skull = (p) => <I {...p}><path d="M5.500 5.500 18.500 18.500" /><path d="M18.500 5.500 5.500 18.500" /><circle cx="12" cy="12" r="8.500" /></I>;
export const Swap = (p) => <I {...p}><path d="M5 8.500h13" /><path d="m15 5 3.500 3.500L15 12" /><path d="M19 15.500H6" /><path d="M9 12l-3.500 3.500L9 19" /></I>;
export const Dots = (p) => <I {...p}><circle cx="6" cy="12" r="1.200" /><circle cx="12" cy="12" r="1.200" /><circle cx="18" cy="12" r="1.200" /></I>;
export const Check = (p) => <I {...p}><path d="m5.500 12.500 4 4 9-9.500" /></I>;
export const Clock = (p) => <I {...p}><circle cx="12" cy="12" r="8.500" /><path d="M12 7.500V12l3 2" /></I>;
export const Chevron = (p) => <I {...p}><path d="m9.500 6.500 5.500 5.500-5.500 5.500" /></I>;
export const Logo = ({ size = 34 }) => (
  <svg width={size} height={size} viewBox="0 0 34 34" aria-hidden="true">
    <rect width="34" height="34" rx="9" fill="#00684A" />
    <circle cx="17" cy="17" r="8.200" fill="none" stroke="#fff" strokeWidth="2.400" />
    <path d="m13.200 17.200 2.700 2.700 5-5.400" fill="none" stroke="#00ED64" strokeWidth="2.600" strokeLinecap="round" strokeLinejoin="round" />
  </svg>
);
