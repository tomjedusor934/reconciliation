import { createRouter, createWebHistory } from 'vue-router';
import { useAuthStore } from '@/stores/auth';
import LoginView from '@/views/LoginView.vue';
import AppShellView from '@/views/AppShellView.vue';
// Users
import UserList from '@/views/users/UserList.vue';
import UserForm from '@/views/users/UserForm.vue';
import UserProfile from '@/views/users/UserProfile.vue';
import ChangePasswordView from '@/views/users/ChangePasswordView.vue';

// Roles
import RoleList from '@/views/roles/RoleList.vue';
import RoleForm from '@/views/roles/RoleForm.vue';

// Settings
import GlobalSettings from '@/views/settings/GlobalSettings.vue';

// Reconciliation
import FlowList from '@/views/flows/FlowList.vue';
import FlowForm from '@/views/flows/FlowForm.vue';
import RecoDashboard from '@/views/reconciliation/DashboardView.vue';
import RecoOperational from '@/views/reconciliation/OperationalView.vue';
import RecoTransversal from '@/views/reconciliation/TransversalView.vue';
import RecoRuns from '@/views/reconciliation/RunsView.vue';
import RecoAudit from '@/views/reconciliation/AuditView.vue';
import RecoArchive from '@/views/reconciliation/ArchiveView.vue';
import LotListView from '@/views/reconciliation/lots/LotListView.vue';
import LotDetailView from '@/views/reconciliation/lots/LotDetailView.vue';


const router = createRouter({
  history: createWebHistory("/"),
  routes: [
    {
      path: '/login',
      name: 'login',
      component: LoginView,
      meta: { guest: true }
    },
    {
      path: '/change-password',
      name: 'change-password',
      component: ChangePasswordView,
      meta: { hideInNav: true }
    },
    {
      path: '/',
      component: AppShellView,
      children: [
        {
          path: '',
          name: 'home',
          component: RecoDashboard,
        },

        // Users
        { path: 'profile', component: UserProfile},
        { path: 'users', component: UserList, meta: { requiresAuth: true , adminOnly: true } },
        { path: 'users/create', component: UserForm, meta: { requiresAuth: true , adminOnly: true } },
        { path: 'users/:id', component: UserForm, meta: { requiresAuth: true , adminOnly: true } },

        // Roles
        { path: 'roles', component: RoleList, meta: { requiresAuth: true , adminOnly: true } },
        { path: 'roles/create', component: RoleForm, meta: { requiresAuth: true , adminOnly: true } },
        { path: 'roles/:id', component: RoleForm, meta: { requiresAuth: true , adminOnly: true } },

        // Admin Settings
        { path: 'admin/settings', component: GlobalSettings, meta: { requiresAuth: true , adminOnly: true } },

        // Reconciliation
        { path: 'flows', component: FlowList, meta: { requiresAuth: true } },
        { path: 'flows/create', component: FlowForm, meta: { requiresAuth: true } },
        { path: 'flows/:id', component: FlowForm, meta: { requiresAuth: true } },
        { path: 'reconciliation', component: RecoDashboard, meta: { requiresAuth: true } },
        { path: 'reconciliation/operational', component: RecoOperational, meta: { requiresAuth: true } },
        { path: 'reconciliation/transversal', component: RecoTransversal, meta: { requiresAuth: true } },
        { path: 'reconciliation/lots', component: LotListView, meta: { requiresAuth: true } },
        { path: 'reconciliation/lots/:id', component: LotDetailView, meta: { requiresAuth: true } },
        { path: 'reconciliation/runs', component: RecoRuns, meta: { requiresAuth: true } },
        { path: 'reconciliation/audit', component: RecoAudit, meta: { requiresAuth: true } },
        { path: 'reconciliation/archive', component: RecoArchive, meta: { requiresAuth: true } },
      ]
    },
    {
      path: '/:pathMatch(.*)*',
      redirect: '/'
    }
  ]
});

router.beforeEach(async (to: any, from: any, next: any) => {
  // use from to avoid usage warning
  from = from;

  const authStore = useAuthStore();

  // Try to restore session if user is null
  if (!authStore.user) {
    await authStore.fetchUser();
  }

  // Check metas in the route hierarchy (parent + children)
  const requiresAuth = to.matched.some((record: any) => record.meta?.requiresAuth);
  const isGuestRoute = to.matched.some((record: any) => record.meta?.guest);
  const isAdminRoute = to.matched.some((record: any) => record.meta?.adminOnly);

  // If user is blocked, deny all access
  if (authStore.isAuthenticated && authStore.user?.blocked) {
    await authStore.logout();
    return;
  }

  // If password is expired, block ALL navigation except /change-password and /login
  if (authStore.isAuthenticated && authStore.mustChangePassword) {
    if (to.path !== '/change-password') {
      next('/change-password');
      return;
    }
    // Allow access to /change-password
    next();
    return;
  }

  if (requiresAuth && !authStore.isAuthenticated) {
    next('/login');
  } else if (isGuestRoute && authStore.isAuthenticated) {
    next('/');
  } else if (isAdminRoute && !authStore.isAdmin) {
    // Admin-only route accessed by non-admin → redirect to home
    next('/');
  } else if (requiresAuth) {
    // Check permissions for all authenticated routes
    if (authStore.hasPermission(to.path)) {
      next();
    } else {
      // Fallback: If attempting to access root and denied, maybe allow or show error.
      // For now, if denied access to specific page, redirect to root.
      if (to.path !== '/') {
        next('/');
      } else {
        // If root is denied, just let them through? Or logout?
        // Allowing root by default as a safe landing page implies it should be unrestricted within app or have basic layout.
        // If strict RBAC is desired, one should configure '/' in rules. 
        // But to prevent lockout loop:
        console.warn('Access denied to root, but allowing to prevent loop.');
        next();
      }
    }
  } else {
    next();
  }
});

export default router;