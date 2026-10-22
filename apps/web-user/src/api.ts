import { createClient } from '@cyshuhe/shared'

export const api = createClient('user', () => {
  window.location.assign('/login')
})