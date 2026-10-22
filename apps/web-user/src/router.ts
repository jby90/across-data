import { createRouter, createWebHistory } from 'vue-router'
import { api } from './api'
import { useAuth } from './auth'

const router = createRouter({
  history: createWebHistory(),
  routes: [
    { path: '/login', name: 'login', component: () => import('./views/Login.vue') },
    {
      path: '/',
      component: () => import('./views/Layout.vue'),
      children: [
        { path: '', name: 'home', component: () => import('./views/Home.vue') },
        {
          path: 'members',
          name: 'members',
          component: () => import('./views/Members.vue'),
          meta: { roles: ['org_admin'] },
        },
      ],
    },
    { path: '/:pathMatch(.*)*', redirect: '/' },
  ],
})

router.beforeEach(async (to) => {
  if (to.name === 'login') return true
  if (!api.token()) return { name: 'login' }
  const auth = useAuth()
  try {
    await auth.load()
  } catch {
    auth.logout()
    return { name: 'login' }
  }
  const roles = to.meta.roles as string[] | undefined
  if (roles && !(auth.user && roles.includes(auth.user.role))) return { name: 'home' }
  return true
})

export default router