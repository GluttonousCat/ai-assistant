import { useEffect, useState } from 'react'
import { platformApi, auth } from '../platformApi'

// Persistent login session check
export default function useSession() {
  const [user, setUser] = useState(null)
  const [checked, setChecked] = useState(false)

  useEffect(() => {
    const saved = auth.getUser()
    const token = auth.getToken()
    if (saved && token) {
      platformApi
        .me()
        .then((me) => setUser({ ...saved, ...me }))
        .catch(() => auth.clear())
        .finally(() => setChecked(true))
    } else {
      setChecked(true)
    }
  }, [])

  return { user, checked, setUser }
}
