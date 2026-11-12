// Incremental SSE decoder. A frame may span network chunks, including CRLF and UTF-8.
export function createEventParser(onEvent) {
  let buffer = ''
  let event = 'message'
  let data = []
  const line = (value) => {
    if (!value) {
      if (data.length) {
        const raw = data.join('\n')
        let payload
        try { payload = JSON.parse(raw) } catch { payload = { text: raw } }
        onEvent({ ...payload, type: event })
      }
      event = 'message'
      data = []
      return
    }
    if (value.startsWith(':')) return
    const colon = value.indexOf(':')
    const field = colon < 0 ? value : value.slice(0, colon)
    const content = colon < 0 ? '' : value.slice(colon + 1).replace(/^ /, '')
    if (field === 'event') event = content || 'message'
    if (field === 'data') data.push(content)
  }
  return (chunk, end = false) => {
    buffer += chunk
    for (;;) {
      const match = /[\r\n]/.exec(buffer)
      if (!match) break
      const index = match.index
      // A CR at the chunk boundary may be the first half of CRLF.
      if (!end && buffer[index] === '\r' && index === buffer.length - 1) break
      line(buffer.slice(0, index))
      const width = buffer.slice(index, index + 2) === '\r\n' ? 2 : 1
      buffer = buffer.slice(index + width)
    }
    if (end) {
      // Accept the last frame from servers that omit its final blank line.
      if (buffer) line(buffer)
      buffer = ''
      line('')
    }
  }
}
