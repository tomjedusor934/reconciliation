/**
 * Monetary amount formatting (French locale: "1 234 567,89 €").
 *
 * Amounts come from the API as strings (e.g. "1234.56"), with a dot decimal
 * separator. These helpers are purely presentational — they never alter the
 * underlying value used for calculations.
 */

const DEFAULT_CURRENCY = 'EUR'

// Cache one formatter per (currency, compact) combination — building an
// Intl.NumberFormat is relatively expensive and we render many cells.
const formatterCache = new Map<string, Intl.NumberFormat>()

function getFormatter(currency: string, compact: boolean): Intl.NumberFormat {
  const key = `${currency}|${compact}`
  let fmt = formatterCache.get(key)
  if (!fmt) {
    fmt = new Intl.NumberFormat('fr-FR', {
      style: 'currency',
      currency,
      ...(compact
        ? { notation: 'compact', minimumFractionDigits: 0, maximumFractionDigits: 1 }
        : { minimumFractionDigits: 2, maximumFractionDigits: 2 }),
    })
    formatterCache.set(key, fmt)
  }
  return fmt
}

/**
 * Parse an amount that may be a string ("1234.56"), a number, null or undefined.
 * Returns null when the value is empty or not a finite number.
 */
function parseAmount(value: string | number | null | undefined): number | null {
  if (value === null || value === undefined || value === '') return null
  const n = typeof value === 'number' ? value : Number(value)
  return Number.isFinite(n) ? n : null
}

/**
 * Format a monetary amount: "1 234 567,89 €". Returns "—" for empty/invalid.
 */
export function formatAmount(
  value: string | number | null | undefined,
  currency: string | null | undefined = DEFAULT_CURRENCY,
): string {
  const n = parseAmount(value)
  if (n === null) return '—'
  return getFormatter(currency || DEFAULT_CURRENCY, false).format(n)
}

/**
 * Compact format for large numbers (KPIs): "1,2 M €". Returns "—" for empty/invalid.
 */
export function formatCompactAmount(
  value: string | number | null | undefined,
  currency: string | null | undefined = DEFAULT_CURRENCY,
): string {
  const n = parseAmount(value)
  if (n === null) return '—'
  return getFormatter(currency || DEFAULT_CURRENCY, true).format(n)
}

/**
 * Whether an amount is strictly negative (for color-coding). Empty/invalid → false.
 */
export function isNegativeAmount(value: string | number | null | undefined): boolean {
  const n = parseAmount(value)
  return n !== null && n < 0
}
