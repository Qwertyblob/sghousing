// Number and date formatting shared by every page.

const integer = new Intl.NumberFormat("en-SG", { maximumFractionDigits: 0 });
const monthName = new Intl.DateTimeFormat("en-SG", { month: "short", year: "numeric", timeZone: "UTC" });
const dayName = new Intl.DateTimeFormat("en-SG", { day: "numeric", month: "short", year: "numeric" });

/** "S$631,694": Singapore dollars, which en-SG formatting would write as a bare "$". */
export const money = (value: number): string => `S$${integer.format(Math.round(value))}`;

/** S$615k / S$1.2M, for chart axes and compact figures. */
export function moneyShort(value: number): string {
  if (Math.abs(value) >= 1e6) return `S$${(value / 1e6).toFixed(2).replace(/\.?0+$/, "")}M`;
  if (Math.abs(value) >= 1e3) return `S$${Math.round(value / 1e3)}k`;
  return `S$${Math.round(value)}`;
}

export const count = (value: number): string => integer.format(value);

export const psm = (value: number): string => `S$${integer.format(value)}`;

export function percent(value: number, digits = 1): string {
  return `${(value * 100).toFixed(digits)}%`;
}

/** "+3.2%" / "−1.0%" with a real minus sign. */
export function signedPercent(value: number, digits = 1): string {
  const text = `${Math.abs(value * 100).toFixed(digits)}%`;
  return value > 0 ? `+${text}` : value < 0 ? `−${text}` : text;
}

export function signedMoney(value: number): string {
  const text = moneyShort(Math.abs(value));
  return value > 0 ? `+${text}` : value < 0 ? `−${text}` : text;
}

/** "2026-10" -> "Oct 2026". */
export function month(value: string): string {
  const [year, mon] = value.split("-").map(Number) as [number, number];
  return monthName.format(new Date(Date.UTC(year, mon - 1, 1)));
}

export const day = (iso: string): string => dayName.format(new Date(iso));

/** "ANG MO KIO" -> "Ang Mo Kio"; "KALLANG/WHAMPOA" -> "Kallang/Whampoa". */
export function title(value: string): string {
  return value
    .toLowerCase()
    .replace(/(^|[\s/(-])([a-z])/g, (_, gap: string, c: string) => gap + c.toUpperCase())
    .replace(/\b(Mrt|Lrt)\b/g, (word) => word.toUpperCase());
}

/** Flat types as HDB writes them, readable: "4 ROOM" -> "4-room", "EXECUTIVE" -> "Executive". */
export function flatType(value: string): string {
  if (value === "ALL") return "All flat types";
  const rooms = /^(\d) ROOM$/.exec(value);
  return rooms ? `${rooms[1]}-room` : title(value);
}
