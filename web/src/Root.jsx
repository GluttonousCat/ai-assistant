import { useState } from 'react'
import Login from './Login.jsx'
import Platform, { useSession } from './Platform.jsx'
import { auth } from './platformApi'

// 根组件: 有登录态 -> 平台; 无 -> 登录页
export default function Root() {
  const { user, checked, setUser } = useSession()

  const logout = () => {
    auth.clear()
    setUser(null)
  }

  if (!checked) {
    return <div className="login-page"><div className="loading"><div className="spinner" />检查登录态…</div></div>
  }

  if (!user) return <Login onLogin={setUser} />
  return <Platform user={user} onLogout={logout} />
}
