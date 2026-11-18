import test from 'node:test'
import assert from 'node:assert/strict'
import { startPolling } from './polling.js'

const flush = async () => { for (let n = 0; n < 8; n++) await Promise.resolve() }

test('slow requests remain serial, and stopping aborts without rearming timers', async (t) => {
  t.mock.timers.enable({ apis: ['setTimeout'] })
  let finish
  let signal
  let calls = 0
  const stop = startPolling((next) => {
    signal = next
    calls++
    return new Promise((resolve) => { finish = resolve })
  }, { interval: 20 })
  t.mock.timers.tick(1)
  t.mock.timers.tick(200)
  assert.equal(calls, 1)
  finish()
  await flush()
  t.mock.timers.tick(20)
  assert.equal(calls, 2)
  stop()
  assert.equal(signal.aborted, true)
  finish()
  await flush()
  t.mock.timers.tick(200)
  assert.equal(calls, 2)
})

test('hidden views wait for visibility and dispose their listener', async (t) => {
  t.mock.timers.enable({ apis: ['setTimeout'] })
  const listeners = new Set()
  const visibility = {
    hidden: true,
    addEventListener: (_, fn) => listeners.add(fn),
    removeEventListener: (_, fn) => listeners.delete(fn),
  }
  let calls = 0
  const stop = startPolling(async () => { calls++ }, { pauseWhenHidden: true, visibility })
  t.mock.timers.tick(1)
  assert.equal(calls, 0)
  visibility.hidden = false
  listeners.forEach((fn) => fn())
  await flush()
  assert.equal(calls, 1)
  stop()
  assert.equal(listeners.size, 0)
})

test('errors retry; single-shot mode runs once and delayed disposal runs nothing', async (t) => {
  t.mock.timers.enable({ apis: ['setTimeout'] })
  const errors = []
  let calls = 0
  const stop = startPolling(async () => { calls++; throw Error('offline') }, {
    interval: 20, onError: (e) => errors.push(e.message),
  })
  t.mock.timers.tick(1)
  await flush()
  t.mock.timers.tick(20)
  await flush()
  assert.equal(calls, 2)
  assert.deepEqual(errors, ['offline', 'offline'])
  stop()
  const once = startPolling(async () => { calls++ }, { interval: 0 })
  t.mock.timers.tick(1)
  await flush()
  t.mock.timers.tick(100)
  assert.equal(calls, 3)
  once()
  startPolling(async () => { calls++ }, { initialDelay: 30 })()
  t.mock.timers.tick(100)
  assert.equal(calls, 3)
})
