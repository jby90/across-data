import { createClient } from '@cyshuhe/shared'

export const api = createClient('admin', () => {
  window.location.assign('/login')
})