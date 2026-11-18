// Run one request at a time; a stopped view cannot schedule or deliver a new poll.
export function startPolling(load, {
  interval = 60000, initialDelay = 0, pauseWhenHidden = false,
  onError = () => {}, visibility = globalThis.document,
} = {}) {
  const controller = new AbortController()
  let timer
  let pending = false
  let stopped = false
  const tick = async () => {
    if (stopped || pending) return
    clearTimeout(timer)
    if (pauseWhenHidden && visibility?.hidden) return
    pending = true
    try {
      await load(controller.signal)
    } catch (error) {
      if (!controller.signal.aborted) onError(error)
    } finally {
      pending = false
      if (!stopped && interval > 0) timer = setTimeout(tick, interval)
    }
  }
  const visible = () => { if (!visibility?.hidden) tick() }
  if (pauseWhenHidden) visibility?.addEventListener('visibilitychange', visible)
  timer = setTimeout(tick, initialDelay)
  return () => {
    stopped = true
    clearTimeout(timer)
    controller.abort()
    if (pauseWhenHidden) visibility?.removeEventListener('visibilitychange', visible)
  }
}
