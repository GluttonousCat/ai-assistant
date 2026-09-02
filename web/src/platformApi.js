// 平台 API 客户端 (带 JWT 鉴权)
const TOKEN_KEY = 'aiass_token'
const USER_KEY = 'aiass_user'

export const auth = {
  getToken: () => localStorage.getItem(TOKEN_KEY) || '',
  getUser: () => {
    try { return JSON.parse(localStorage.getItem(USER_KEY) || 'null') } catch { return null }
  },
  save(token, user) {
    localStorage.setItem(TOKEN_KEY, token)
    localStorage.setItem(USER_KEY, JSON.stringify(user))
  },
  clear() {
    localStorage.removeItem(TOKEN_KEY)
    localStorage.removeItem(USER_KEY)
  },
}

async function request(method, path, body) {
  const headers = { 'Content-Type': 'application/json' }
  const token = auth.getToken()
  if (token) headers['Authorization'] = `Bearer ${token}`
  const res = await fetch(path, {
    method,
    headers,
    body: body === undefined ? undefined : JSON.stringify(body),
  })
  // 401: 登录接口的 401 是"密码错误", 其他接口的 401 是"登录失效"
  if (res.status === 401 && !path.startsWith('/api/auth/login')) {
    auth.clear()
    window.dispatchEvent(new CustomEvent('auth:expired'))
    throw new Error('登录已失效, 请重新登录')
  }
  if (!res.ok) {
    const data = await res.json().catch(() => ({}))
    throw new Error(data.detail || `HTTP ${res.status}`)
  }
  return res.json()
}

// SSE 流式 POST: 逐事件回调 onEvent({type, ...})。
// 立即返回 {promise, abort}: promise 流结束/失败时 resolve; abort() 可随时中断。
function sseStream(path, body, onEvent) {
  const ac = new AbortController()
  const promise = (async () => {
    const res = await fetch(path, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json', Authorization: `Bearer ${auth.getToken()}` },
      body: JSON.stringify(body),
      signal: ac.signal,
    })
    if (res.status === 401) {
      auth.clear()
      window.dispatchEvent(new CustomEvent('auth:expired'))
      throw new Error('登录已失效, 请重新登录')
    }
    if (!res.ok) {
      const errBody = await res.json().catch(() => ({}))
      throw new Error(errBody.detail || `HTTP ${res.status}`)
    }
    const reader = res.body.getReader()
    const decoder = new TextDecoder('utf-8')
    let buf = ''
    try {
      for (;;) {
        const { done, value } = await reader.read()
        if (done) break
        buf += decoder.decode(value, { stream: true })
        // SSE 事件以空行分隔
        const parts = buf.split('\n\n')
        buf = parts.pop()
        for (const part of parts) {
          let event = 'message'
          let data = ''
          for (const line of part.split('\n')) {
            if (line.startsWith('event: ')) event = line.slice(7).trim()
            else if (line.startsWith('data: ')) data += line.slice(6)
          }
          if (!data) continue
          let payload
          try { payload = JSON.parse(data) } catch { payload = { text: data } }
          onEvent({ type: event, ...payload })
        }
      }
    } catch (err) {
      if (err.name !== 'AbortError') throw err
    } finally {
      reader.releaseLock?.()
    }
  })()
  return { promise, abort: () => ac.abort() }
}

export const platformApi = {
  login: (username, password) =>
    request('POST', '/api/auth/login', { username, password }),
  register: (username, password, inviteCode) =>
    request('POST', '/api/auth/register', {
      username, password, invite_code: inviteCode,
    }),
  me: () => request('GET', '/api/auth/me'),
  listUsers: () => request('GET', '/api/auth/users'),
  setUserRole: (userId, role) =>
    request('POST', `/api/auth/users/${userId}/role`, { role }),

  // Agent 对话 (财务查询 + 研报解读): 一次性返回
  chat: (text) => request('POST', '/api/v1/query', { text }),

  // Agent 对话流式版 (SSE): onEvent({type, ...}) 依次收到
  //   {type:'stage', message} {type:'data', data...} {type:'delta', text} {type:'done'} {type:'error', message}
  chatStream: async (text, onEvent) => {
    await sseStream('/api/v1/query/stream', { text }, onEvent).promise
  },

  // 研报中心
  reports: (params = {}) => {
    const qs = new URLSearchParams(
      Object.entries(params).filter(([, v]) => v !== undefined && v !== null && v !== '')
    ).toString()
    return request('GET', `/api/reports${qs ? `?${qs}` : ''}`)
  },
  reportTags: () => request('GET', '/api/reports/tags'),
  reportStocks: () => request('GET', '/api/reports/stocks'),
  reportDetail: (id) => request('GET', `/api/reports/${id}`),
  analyzeReport: (id) => request('POST', `/api/reports/${id}/analyze`),
  // 单篇研报 AI 分析流式版 (SSE): 事件与 chatStream 同构
  //   {type:'stage', stage, message} {type:'delta', text} {type:'data', data} {type:'error'|'done'}
  // 返回 {promise, abort}: 关闭弹窗时调 abort() 中断连接 (后台分析仍会完成入库)
  analyzeReportStream: (id, onEvent) =>
    sseStream(`/api/reports/${id}/analyze/stream`, {}, onEvent),

  // 工作台概览
  health: () => request('GET', '/health'),
}
