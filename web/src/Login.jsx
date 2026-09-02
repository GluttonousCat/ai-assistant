import { useState } from 'react'
import { platformApi, auth } from './platformApi'

// 登录/注册页: 需要认证的平台入口
// 校验规则: 用户名仅英文/数字/下划线且 >=6 位; 密码 >=8 位
const USERNAME_RE = /^[A-Za-z0-9_]{6,}$/

export default function Login({ onLogin }) {
  const [mode, setMode] = useState('login')
  const [username, setUsername] = useState('')
  const [password, setPassword] = useState('')
  const [inviteCode, setInviteCode] = useState('')
  const [showPwd, setShowPwd] = useState(false)
  const [msg, setMsg] = useState(null) // {type: 'error'|'ok', text}
  const [busy, setBusy] = useState(false)

  function validate() {
    const u = username.trim()
    if (!u || !password) return '请输入用户名和密码'
    if (!USERNAME_RE.test(u)) return '用户名仅限英文字母/数字/下划线, 至少 6 位'
    if (mode === 'register' && password.length < 8) return '密码至少 8 位'
    if (mode === 'register' && !inviteCode.trim()) return '请输入注册码'
    return null
  }

  async function submit(e) {
    e.preventDefault()
    setMsg(null)
    const err = validate()
    if (err) {
      setMsg({ type: 'error', text: err })
      return
    }
    setBusy(true)
    try {
      if (mode === 'login') {
        const res = await platformApi.login(username.trim(), password)
        auth.save(res.token, res.user)
        onLogin(res.user)
      } else {
        await platformApi.register(username.trim(), password, inviteCode.trim())
        // 注册成功直接登录
        const res = await platformApi.login(username.trim(), password)
        auth.save(res.token, res.user)
        onLogin(res.user)
      }
    } catch (err) {
      setMsg({ type: 'error', text: err.message })
    } finally {
      setBusy(false)
    }
  }

  return (
    <div className="login-page">
      <div className="login-card">
        <div className="login-brand">
          <span className="logo-mark">◎</span>
          <h1>Alpha Finance Radar</h1>
          <p className="tagline">上市公司财务与研报智能分析平台</p>
        </div>

        <div className="seg login-seg">
          <button className={mode === 'login' ? 'on' : ''} onClick={() => { setMode('login'); setMsg(null) }}>登录</button>
          <button className={mode === 'register' ? 'on' : ''} onClick={() => { setMode('register'); setMsg(null) }}>注册</button>
        </div>

        <form onSubmit={submit} className="login-form">
          <label>
            <span>用户名 <em className="field-hint">英文/数字/下划线 ≥ 6 位</em></span>
            <input
              value={username}
              onChange={(e) => setUsername(e.target.value)}
              placeholder="username"
              autoComplete="username"
              autoFocus
            />
          </label>
          {mode === 'register' && (
            <label>
              <span>注册码</span>
              <input
                value={inviteCode}
                onChange={(e) => setInviteCode(e.target.value)}
                placeholder="请输入注册码"
                autoComplete="off"
              />
            </label>
          )}
          <label>
            <span>
              密码
              {mode === 'register' && <em className="field-hint">≥ 8 位</em>}
            </span>
            <div className="pwd-wrap">
              <input
                type={showPwd ? 'text' : 'password'}
                value={password}
                onChange={(e) => setPassword(e.target.value)}
                placeholder={mode === 'register' ? '至少 8 位' : '请输入密码'}
                autoComplete={mode === 'login' ? 'current-password' : 'new-password'}
                className="pwd-input"
              />
              <button type="button" className="pwd-toggle" aria-label={showPwd ? '隐藏密码' : '显示密码'}
                onClick={() => setShowPwd((v) => !v)}>
                {showPwd ? '🙈' : '👁'}
              </button>
            </div>
          </label>

          {msg && <div className={`login-msg ${msg.type}`}>{msg.text}</div>}

          <button className="btn login-btn" type="submit" disabled={busy}>
            {busy ? '请稍候…' : mode === 'login' ? '登 录' : '注册并登录'}
          </button>
        </form>

        <div className="login-foot">
          Tushare 财务数据 × 知识星球研报 × Agent 分析
        </div>
      </div>
    </div>
  )
}
