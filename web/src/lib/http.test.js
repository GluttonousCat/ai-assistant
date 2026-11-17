import { afterEach, beforeEach, test } from 'node:test'
import assert from 'node:assert/strict'
import { auth, authorizedFetch, request, sseStream } from './http.js'
import { platformApi } from '../platformApi.js'

const saved = { fetch: globalThis.fetch, localStorage: globalThis.localStorage, window: globalThis.window }
let events
beforeEach(() => {
  const storage = new Map()
  globalThis.localStorage = {
    getItem: (key) => storage.get(key) ?? null,
    setItem: (key, value) => storage.set(key, value),
    removeItem: (key) => storage.delete(key),
  }
  events = []
  globalThis.window = { dispatchEvent: (event) => events.push(event.type) }
})
afterEach(() => { Object.assign(globalThis, saved) })

test('JSON requests preserve authentication, payload and cancellation', async () => {
  auth.save('test-token', { username: 'tester' })
  const controller = new AbortController()
  globalThis.fetch = async (path, options) => {
    assert.equal(path, '/records')
    assert.equal(options.headers.get('Authorization'), 'Bearer test-token')
    assert.equal(options.body, '{"q":"中文"}')
    assert.equal(options.signal, controller.signal)
    return Response.json({ ok: true })
  }
  assert.deepEqual(await request('POST', '/records', { q: '中文' }, { signal: controller.signal }), { ok: true })
})

test('wrong login password does not clear the session; expired API auth does', async () => {
  auth.save('test-token', { username: 'tester' })
  globalThis.fetch = async () => Response.json({ detail: '密码错误' }, { status: 401 })
  await assert.rejects(request('POST', '/api/auth/login', {}), /密码错误/)
  assert.equal(auth.getToken(), 'test-token')
  assert.deepEqual(events, [])
  await assert.rejects(authorizedFetch('/api/reports/1/download'), /登录已失效/)
  assert.equal(auth.getToken(), '')
  assert.deepEqual(events, ['auth:expired'])
})

test('validation arrays and non-JSON server errors remain readable', async () => {
  globalThis.fetch = async () => Response.json({ detail: [{ msg: '参数无效' }] }, { status: 422 })
  await assert.rejects(request('GET', '/records'), /参数无效/)
  globalThis.fetch = async () => new Response('gateway error', { status: 502 })
  await assert.rejects(request('GET', '/records'), /HTTP 502/)
})

test('restricted browser storage does not prevent session expiry handling', async () => {
  globalThis.localStorage = {
    getItem: () => { throw new Error('storage unavailable') },
    removeItem: () => { throw new Error('storage unavailable') },
  }
  assert.equal(auth.getToken(), '')
  globalThis.fetch = async () => Response.json({}, { status: 401 })
  await assert.rejects(request('GET', '/records'), /登录已失效/)
  assert.deepEqual(events, ['auth:expired'])
})

test('SSE preserves Chinese and final unterminated frames across byte chunks', async () => {
  const received = []
  const bytes = new TextEncoder().encode('event: delta\ndata: {"text":"中文"}\n\nevent: done\ndata: {}')
  globalThis.fetch = async () => new Response(new ReadableStream({
    start(controller) {
      for (const byte of bytes) controller.enqueue(new Uint8Array([byte]))
      controller.close()
    },
  }))
  const stream = sseStream('/stream', {}, (event) => received.push(event))
  await stream.promise
  assert.deepEqual(received, [{ type: 'delta', text: '中文' }, { type: 'done' }])
})

test('report list and details forward AbortSignal without changing query contracts', async () => {
  const seen = []
  const controller = new AbortController()
  globalThis.fetch = async (path, options) => {
    seen.push([path, options.signal])
    return Response.json({})
  }
  await platformApi.reports({ keyword: '中文', page: 2, absent: null }, { signal: controller.signal })
  await platformApi.reportDetail(7, { signal: controller.signal })
  assert.deepEqual(seen, [['/api/reports?keyword=%E4%B8%AD%E6%96%87&page=2', controller.signal], ['/api/reports/7', controller.signal]])
})
