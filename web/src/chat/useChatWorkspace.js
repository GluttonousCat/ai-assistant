import { useEffect, useRef, useState } from 'react'
import { auth } from '../platformApi'

const newConversation = () => ({ id: crypto.randomUUID?.() || `${Date.now()}-${Math.random().toString(36).slice(2)}`, title: '新对话', messages: [], draft: '' })

// Tab-local, account-scoped history. Never persist tokens or mix different users' chats.
export default function useChatWorkspace() {
  const key = `radar:chats:${auth.getUser()?.user_id ?? 'anonymous'}`
  const [workspace, setWorkspace] = useState(() => {
    try {
      const saved = JSON.parse(sessionStorage.getItem(key))
      if (saved?.version === 1 && saved.chats?.length && saved.chats.every((c) =>
        typeof c.id === 'string' && Array.isArray(c.messages))) {
        return { ...saved, active: saved.chats.some((c) => c.id === saved.active) ? saved.active : saved.chats[0].id }
      }
    } catch { /* Storage can be unavailable or contain an older format. */ }
    const chat = newConversation()
    return { version: 1, active: chat.id, chats: [chat] }
  })
  const latestRef = useRef(workspace)
  latestRef.current = workspace
  const [storageError, setStorageError] = useState(false)
  useEffect(() => {
    const timer = setTimeout(() => {
      try { sessionStorage.setItem(key, JSON.stringify(workspace)); setStorageError(false) }
      catch { setStorageError(true) }
    }, 300)
    return () => clearTimeout(timer)
  }, [key, workspace])
  useEffect(() => () => {
    // Flush once on unmount; do not serialize the whole history on every token.
    try { sessionStorage.setItem(key, JSON.stringify(latestRef.current)) } catch { /* Best effort on unmount. */ }
  }, [key])
  const chat = workspace.chats.find((c) => c.id === workspace.active)
  const update = (field, value) => setWorkspace((w) => ({ ...w, chats: w.chats.map((c) =>
    c.id === chat.id ? { ...c, [field]: typeof value === 'function' ? value(c[field]) : value } : c) }))
  return {
    chat, chats: workspace.chats, storageError,
    setMessages: (value) => update('messages', (messages) =>
      (typeof value === 'function' ? value(messages) : value).slice(-100)),
    setInput: (value) => update('draft', value),
    select: (id) => setWorkspace((w) => ({ ...w, active: id })),
    create: () => {
      const next = newConversation()
      setWorkspace((w) => ({ ...w, active: next.id, chats: [next, ...w.chats].slice(0, 20) }))
    },
    rename: (title) => update('title', title),
    remove: (id) => setWorkspace((w) => {
      const chats = w.chats.filter((c) => c.id !== id)
      if (!chats.length) chats.push(newConversation())
      return { ...w, chats, active: w.active === id ? chats[0].id : w.active }
    }),
  }
}
