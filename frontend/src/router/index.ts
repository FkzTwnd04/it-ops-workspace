import { createRouter, createWebHistory } from 'vue-router'
import { useAuthStore } from '@/stores/auth'

const router = createRouter({
  history: createWebHistory(),
  routes: [
    {
      path: '/login',
      name: 'login',
      component: () => import('@/views/LoginView.vue'),
      meta: { public: true },
    },
    {
      path: '/',
      component: () => import('@/components/layout/AppLayout.vue'),
      meta: { requiresAuth: true },
      children: [
        { path: '', name: 'home', redirect: '/tickets' },
        {
          path: 'dashboard',
          name: 'dashboard',
          component: () => import('@/views/DashboardView.vue'),
          meta: { requiresOps: true },
        },
        {
          path: 'tickets',
          name: 'tickets',
          component: () => import('@/views/tickets/TicketListView.vue'),
        },
        {
          path: 'tickets/:id',
          name: 'ticket-detail',
          component: () => import('@/views/tickets/TicketDetailView.vue'),
        },
        {
          path: 'approvals',
          name: 'approvals',
          component: () => import('@/views/ApprovalView.vue'),
          meta: { requiresOps: true },
        },
      ],
    },
    { path: '/:pathMatch(.*)*', redirect: '/tickets' },
  ],
})

router.beforeEach((to, _from, next) => {
  const auth = useAuthStore()

  if (to.meta.public) {
    if (auth.isLoggedIn && to.name === 'login') return next('/tickets')
    return next()
  }

  if (!auth.isLoggedIn) return next('/login')

  if (to.meta.requiresOps && !auth.isOps) return next('/tickets')

  next()
})

export default router
