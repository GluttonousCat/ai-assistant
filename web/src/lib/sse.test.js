import test from 'node:test'
import assert from 'node:assert/strict'
import { createEventParser } from './sse.js'

test('every possible chunk boundary preserves Chinese, CRLF and multiline JSON', () => {
  const source = ': heartbeat\r\nevent:delta\r\ndata:{\r\ndata: "text":"中文回复"}\r\n\r\nevent: done\r\ndata: {}\r\n\r\n'
  const bytes = new TextEncoder().encode(source)
  for (let split = 0; split <= bytes.length; split++) {
    const events = []
    const parse = createEventParser((event) => events.push(event))
    const decoder = new TextDecoder()
    parse(decoder.decode(bytes.slice(0, split), { stream: true }))
    parse(decoder.decode(bytes.slice(split), { stream: true }))
    parse(decoder.decode(), true)
    assert.deepEqual(events, [{ text: '中文回复', type: 'delta' }, { type: 'done' }])
  }
})

test('flushes unterminated final frame and ignores comment-only frames', () => {
  const events = []
  const parse = createEventParser((event) => events.push(event))
  parse(': comment\n\nevent: delta\ndata: {"text":"末尾"}', true)
  assert.deepEqual(events, [{ text: '末尾', type: 'delta' }])
})

test('wire event type wins over payload type', () => {
  const events = []
  createEventParser((event) => events.push(event))('event: error\ndata: {"type":"done"}\n\n')
  assert.equal(events[0].type, 'error')
})
