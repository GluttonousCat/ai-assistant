import { createEventParser } from './sse.js'

// Platform API client (JWT-authenticated)
const TOKEN_KEY = 'aiass_token'
const USER_KEY = 'aiass_user'

export const auth = {
  getToken: () => {
    try { return localStorage.getItem(TOKEN_KEY) || '' } catch { return '' }
  },
  getUser: () => {
    try { return JSON.parse(localStorage.getItem(USER_KEY) || 'null') } catch { return null }
  },
  save(token, user) {
    localStorage.setItem(TOKEN_KEY, token)
    localStorage.setItem(USER_KEY, JSON.stringify(user))
  },
  clear() {
    for (const key of [TOKEN_KEY, USER_KEY]) {
      try { localStorage.removeItem(key) } catch { /* Logout must still update the current UI. */ }
    }
  },
}

function responseMessage(detail, fallback) {
  if (typeof detail === 'string') return detail
  if (Array.isArray(detail)) return detail.map((item) => item.msg || String(item)).join('；')
  return fallback
}

export async function authorizedFetch(path, options = {}) {
  const headers = new Headers(options.headers)
  const token = auth.getToken()
  if (token) headers.set('Authorization', `Bearer ${token}`)
  const res = await fetch(path, { ...options, headers })
  if (res.status === 401 && !path.startsWith('/api/auth/login')) {
    auth.clear()
    window.dispatchEvent(new CustomEvent('auth:expired'))
    throw new Error('登录已失效, 请重新登录')
  }
  if (!res.ok) {
    const data = await res.json().catch(() => ({}))
    throw new Error(responseMessage(data?.detail, `HTTP ${res.status}`))
  }
  return res
}

export async function request(method, path, body, options = {}) {
  const res = await authorizedFetch(path, {
    method,
    headers: { 'Content-Type': 'application/json' },
    body: body === undefined ? undefined : JSON.stringify(body),
    signal: options.signal,
  })
  return res.json()
}

// Streaming SSE POST: invokes onEvent({type, ...}) per event.
// Returns {promise, abort} immediately: promise resolves when the stream ends/fails; abort() can interrupt at any time.
export function sseStream(path, body, onEvent) {
  const ac = new AbortController()
  const promise = (async () => {
    const res = await authorizedFetch(path, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body),
      signal: ac.signal,
    })
    if (!res.body) throw new Error('服务未返回可读取的响应')
    const reader = res.body.getReader()
    const decoder = new TextDecoder('utf-8')
    const parse = createEventParser(onEvent)
    try {
      for (;;) {
        const { done, value } = await reader.read()
        if (done) { parse(decoder.decode(), true); break }
        parse(decoder.decode(value, { stream: true }))
      }
    } catch (err) {
      if (err.name !== 'AbortError') throw err
    } finally {
      reader.releaseLock?.()
    }
  })()
  return { promise, signal: ac.signal, abort: () => ac.abort() }
}
