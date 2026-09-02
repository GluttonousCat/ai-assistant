// range_trading 数据 API 客户端 (带 JWT 鉴权, 复用平台 token)
import { auth } from './platformApi'

const BASE = '/api/range'

function headers() {
  const h = {}
  const token = auth.getToken()
  if (token) h['Authorization'] = `Bearer ${token}`
  return h
}

function onUnauthorized() {
  auth.clear()
  window.dispatchEvent(new CustomEvent('auth:expired'))
}

async function get(path, params = {}) {
  const qs = new URLSearchParams(
    Object.entries(params).filter(([, v]) => v !== undefined && v !== null)
  ).toString()
  const url = qs ? `${BASE}${path}?${qs}` : `${BASE}${path}`
  const res = await fetch(url, { headers: headers() })
  if (res.status === 401) {
    onUnauthorized()
    throw new Error('登录已失效, 请重新登录')
  }
  if (!res.ok) {
    const body = await res.json().catch(() => ({}))
    throw new Error(body.detail || `HTTP ${res.status}`)
  }
  return res.json()
}

async function post(path) {
  const res = await fetch(`${BASE}${path}`, { method: 'POST', headers: headers() })
  if (res.status === 401) {
    onUnauthorized()
    throw new Error('登录已失效, 请重新登录')
  }
  return res.json()
}

export const api = {
  latest: (minScore = 70, rangeOnly = true) =>
    get('/latest', { min_score: minScore, range_only: rangeOnly }),
  scan: (date, minScore = 70, rangeOnly = true) =>
    get(`/scan/${date}`, { min_score: minScore, range_only: rangeOnly }),
  dates: () => get('/dates'),
  triggerScan: () => post('/scan'),
  chart: (symbol, days = 60) => get(`/chart/${symbol}`, { days }),
  tracking: (minScore = 70, horizon = 10, lookbackDays = 60) =>
    get('/tracking', { min_score: minScore, horizon, lookback_days: lookbackDays }),
  setupLatest: (minScore = 55, maxScore = 80, detector = 'all') =>
    get('/setup/latest', { min_score: minScore, max_score: maxScore, detector }),
  setupTriggerScan: () => post('/setup/scan'),
  setupBacktest: () => get('/setup/backtest'),
  setupMarket: () => get('/setup/market'),
}
