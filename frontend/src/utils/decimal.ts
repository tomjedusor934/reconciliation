/**
 * Exact arithmetic on monetary amounts.
 *
 * Amounts come from the API as strings backed by `Numeric(20, 4)`. Summing them
 * as floats and comparing against a tolerance (the old `|Σ| < 0.005`) does not
 * agree with the backend, which requires `total == Decimal("0")` EXACTLY
 * (`reconciliation_service.force_match`). The UI could therefore show
 * "balanced" and the force still come back as a 400.
 *
 * So we sum in minor units — integers scaled by 10^4 — which is exact. The
 * largest amounts in the book are ~3.6e8, i.e. ~3.6e12 once scaled; a basket of
 * a few thousand of those stays far below Number.MAX_SAFE_INTEGER (9.0e15).
 */

/** Numeric(20, 4) → four decimal places. */
const SCALE = 10_000;

/** "364681616.94" → 3646816169400. Empty/invalid → 0. */
export function toMinor(value: string | number | null | undefined): number {
  if (value === null || value === undefined || value === '') return 0;
  const n = typeof value === 'number' ? value : Number(value);
  if (!Number.isFinite(n)) return 0;
  // Round away the binary-representation dust Number() leaves behind: the
  // string has at most 4 decimals, so the scaled value is an integer.
  return Math.round(n * SCALE);
}

/** Sum of amounts, in minor units — exact. */
export function sumMinor(values: (string | number | null | undefined)[]): number {
  let total = 0;
  for (const v of values) total += toMinor(v);
  return total;
}

/** 3646816169400 → 364681616.94, for display through formatAmount. */
export function fromMinor(minor: number): number {
  return minor / SCALE;
}

/** A group only balances on an exact zero — same rule as the backend. */
export function isBalancedMinor(minor: number): boolean {
  return minor === 0;
}
