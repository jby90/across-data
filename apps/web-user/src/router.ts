import { createRouter, createWebHistory } from 'vue-router'
import { api } from './api'
import { useAuth } from './auth'

const router = createRouter({
  history: createWebHistory(import.meta.env.BASE_URL),
  routes: [
    { path: '/login', name: 'login', component: () => import('./views/Login.vue') },
    { path: '/licenses', name: 'licenses', component: () => import('./views/Licenses.vue') },
    {
      path: '/',
      component: () => import('./views/Layout.vue'),
      children: [
        { path: '', name: 'home', component: () => import('./views/Home.vue') },
        { path: 'notifications', name: 'notifications', component: () => import('./views/Notifications.vue') },
        {
          path: 'knowledge',
          name: 'knowledge',
          component: () => import('./views/KnowledgeBase.vue'),
        },
        {
          path: 'analysis',
          name: 'analysis',
          component: () => import('./views/ChatAnalysis.vue'),
        },
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
  if (to.name === 'login' || to.name === 'licenses') return true
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
