export const stockSearchText = (stock, industries) => `${stock.ts_code}${stock.name}${industries?.[stock.ts_code] || ''}`.toUpperCase()

export function compareStockScores(a, b) {
  const score = (value) => value != null && Number.isFinite(Number(value)) ? Number(value) : -Infinity
  const left = score(a.score)
  const right = score(b.score)
  return left === right ? a.ts_code.localeCompare(b.ts_code) : left > right ? -1 : 1
}
