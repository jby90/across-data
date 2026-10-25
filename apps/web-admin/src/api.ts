import { createClient } from '@cyshuhe/shared'

export const api = createClient('admin', () => {
  window.location.assign(import.meta.env.BASE_URL + 'login')
})