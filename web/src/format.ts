/** Number and time formatting. Missing values are shown as a dash, never as zero. */

export const MISSING = "–";

const moneyFormat = new Intl.NumberFormat("en-US", {
  style: "currency",
  currency: "USD",
  minimumFractionDigits: 2,
  maximumFractionDigits: 2,
});

export function money(value: number | null | undefined): string {
  return value == null ? MISSING : moneyFormat.format(value);
}

export function signedMoney(value: number | null | undefined): string {
  if (value == null) return MISSING;
  return `${value >= 0 ? "+" : "-"}${moneyFormat.format(Math.abs(value))}`;
}

export function price(value: number | null | undefined, digits = 2): string {
  return value == null ? MISSING : value.toFixed(digits);
}

/** `fraction` is 0.0125 for 1.25%. */
export function percent(fraction: number | null | undefined, digits = 2): string {
  return fraction == null ? MISSING : `${(fraction * 100).toFixed(digits)}%`;
}

export function signedPercent(fraction: number | null | undefined, digits = 2): string {
  if (fraction == null) return MISSING;
  return `${fraction >= 0 ? "+" : "-"}${Math.abs(fraction * 100).toFixed(digits)}%`;
}

export function direction(value: number | null | undefined): "up" | "down" | "flat" {
  if (value == null || value === 0) return "flat";
  return value > 0 ? "up" : "down";
}

export function directionGlyph(value: number | null | undefined): string {
  return { up: "▲", down: "▼", flat: "·" }[direction(value)];
}

/** 2025-03-13T21:00:00Z -> "2025-03-13 21:00 UTC" */
export function barTime(timestamp: string | null | undefined): string {
  if (!timestamp) return MISSING;
  const parsed = new Date(timestamp);
  if (Number.isNaN(parsed.getTime())) return MISSING;
  return `${parsed.toISOString().slice(0, 10)} ${parsed.toISOString().slice(11, 16)} UTC`;
}

/** 2025-03-13T21:00:00Z -> "03-13" */
export function shortDate(timestamp: string | null | undefined): string {
  const formatted = barTime(timestamp);
  return formatted === MISSING ? MISSING : formatted.slice(5, 10);
}

export function units(value: number | null | undefined): string {
  if (value == null) return MISSING;
  return Number.isInteger(value) ? String(value) : value.toFixed(4);
}

/** "queued_for_next_open" -> "queued for next open" */
export function plainReason(reason: string): string {
  return reason.replaceAll("_", " ");
}
