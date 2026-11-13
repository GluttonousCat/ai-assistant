import { useEffect, useRef, useState } from 'react'
import { platformApi } from '../platformApi'
import { boundedCache } from './chartData'

// Page-local, snapshot-bound cache. Abort obsolete requests when quickly switching.
export default function useStockChart(code, runId, timeframe, revision = 0) {
  const cache = useRef(null)
  const [view, setView] = useState({ key: '', identity: '', detail: null, status: 'idle', error: '' })
  const identity = code && runId ? `${runId}:${code}:${timeframe}` : ''
  const key = identity ? `${identity}:${revision}` : ''

  useEffect(() => {
    if (!key) return
    if (!cache.current) cache.current = boundedCache(20)
    const saved = cache.current.get(key)
    if (saved) {
      setView({ key, identity, detail: saved, status: 'ready', error: '' })
      return
    }
    const controller = new AbortController()
    setView((old) => ({ key, identity, detail: old.identity === identity ? old.detail : null, status: 'loading', error: '' }))
    platformApi.marketStock(code, runId, timeframe, { signal: controller.signal }).then((detail) => {
      if (controller.signal.aborted) return
      cache.current.set(key, detail)
      setView({ key, identity, detail, status: 'ready', error: '' })
    }).catch((e) => {
      if (!controller.signal.aborted) setView((old) => ({ ...old, key, identity, status: 'error', error: e.message }))
    })
    return () => controller.abort()
  }, [key, identity, code, runId, timeframe])

  const matches = Boolean(identity && view.identity === identity)
  return {
    detail: matches ? view.detail : null,
    busy: Boolean(key && (view.key !== key || view.status === 'loading')),
    error: matches && view.key === key ? view.error : '',
  }
}
