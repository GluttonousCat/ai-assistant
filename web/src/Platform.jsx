import { useEffect, useState } from 'react'
import { platformApi, auth } from './platformApi'
import AgentChat from './AgentChat'
import Reports from './Reports'
import App from './App' // 区间扫描看板 (原有页面, 仅 admin)
import UserAdmin from './UserAdmin' // 用户管理 (仅 admin)

// 导航定义: adminOnly 的项非管理员置灰不可点
const SUPER_ADMIN = 'Gluttonouscat'

const NAV = [
  { key: 'chat', label: 'Agent 对话', icon: '✦' },
  { key: 'reports', label: '研报中心', icon: '▤' },
  { key: 'range', label: '区间看板', icon: '◫', adminOnly: true },      // admin 可见可点, user 置灰 🔒
  { key: 'users', label: '用户管理', icon: '⚙', superOnly: true },       // 仅超级管理员渲染
]

// 平台主框架: 侧边导航 + 顶栏(用户信息/退出) + 角色门控
export default function Platform({ user, onLogout }) {
  const [nav, setNav] = useState('chat')
  const isAdmin = user?.role === 'admin'
  const isSuper = isAdmin && user?.username === SUPER_ADMIN

  useEffect(() => {
    const onExpired = () => onLogout()
    window.addEventListener('auth:expired', onExpired)
    return () => window.removeEventListener('auth:expired', onExpired)
  }, [onLogout])

  useEffect(() => {
    const hash = window.location.hash.replace('#', '')
    const target = NAV.find((n) => n.key === hash)
    // 权限不足的 hash 直达 (adminOnly/superOnly) -> 回落对话页
    if (target && !(target.adminOnly && !isAdmin) && !(target.superOnly && !isSuper)) {
      setNav(hash)
    }
  }, [isAdmin, isSuper])

  useEffect(() => {
    window.location.hash = nav
  }, [nav])

  return (
    <div className="platform">
      <aside className="sidebar">
        <div className="side-brand">
          <span className="logo-mark">◆</span>
          <div>
            <div className="brand-name">Alpha Finance Radar</div>
            <div className="brand-sub">Insight Engine</div>
          </div>
        </div>
        <nav className="side-nav">
          {NAV.filter((n) => !n.superOnly || isSuper).map((n) => {
            const locked = n.adminOnly && !isAdmin
            return (
              <button
                key={n.key}
                className={nav === n.key ? 'on' : ''}
                disabled={locked}
                title={locked ? '仅管理员可用' : undefined}
                onClick={() => setNav(n.key)}
              >
                <span className="nav-icon">{locked ? '🔒' : n.icon}</span>{n.label}
              </button>
            )
          })}
        </nav>
        <div className="side-foot">
          <div className="user-chip">
            <span className="avatar">{(user.display_name || user.username || '?').slice(0, 1).toUpperCase()}</span>
            <div className="user-meta">
              <div className="user-name">{user.display_name || user.username}</div>
              <div className="user-role">{isAdmin ? '管理员' : '研究员'}</div>
            </div>
          </div>
          <button className="btn ghost logout-btn" onClick={onLogout}>退出登录</button>
        </div>
      </aside>

      <main className="main">
        {nav === 'chat' && <AgentChat />}
        {nav === 'reports' && <Reports />}
        {nav === 'range' && isAdmin && <App embedded />}
        {nav === 'users' && isSuper && <UserAdmin />}
      </main>
    </div>
  )
}

// 持久登录态检查
export function useSession() {
  const [user, setUser] = useState(null)
  const [checked, setChecked] = useState(false)

  useEffect(() => {
    const saved = auth.getUser()
    const token = auth.getToken()
    if (saved && token) {
      platformApi
        .me()
        .then((me) => setUser({ ...saved, role: me.role }))
        .catch(() => auth.clear())
        .finally(() => setChecked(true))
    } else {
      setChecked(true)
    }
  }, [])

  return { user, checked, setUser }
}
