# Frontend Architecture Deep Dive

## Overview
Vue 3 + Composition API (strict: `<script setup>` + TypeScript only, no Options API).
Pinia for state management, Tailwind CSS for styling, reusable UI component library.

## Directory Structure

```
frontend/src/
├── main.ts                          # Bootstrap (createApp, mount)
├── App.vue                          # Root component
├── style.css                        # Tailwind @import directives
│
├── api/
│   └── axios.ts                     # HTTP client (withCredentials, CSRF interceptor)
│
├── types/
│   └── index.ts                     # All TypeScript interfaces (centralized)
│
├── stores/
│   ├── auth.ts                      # Pinia: login, user, permissions
│   └── sidebar.ts                   # Pinia: isOpen (toggle state)
│
├── services/                        # API client layer (literal objects, not classes)
│   ├── userService.ts               # /users CRUD
│   ├── roleService.ts               # /roles CRUD
│   ├── settingsService.ts           # /admin/settings
│   ├── ssoService.ts                # SSO configuration
│   ├── flowService.ts               # /flows CRUD (NEW)
│   ├── reconciliationService.ts     # /reconciliation-entries ops (NEW)
│   ├── matchGroupService.ts         # /match-groups ops (NEW)
│   ├── dashboardService.ts          # /dashboards endpoints (NEW)
│   ├── runService.ts                # /ingestion-runs, /reconciliation-runs (NEW)
│   └── auditService.ts              # /audit endpoints (NEW)
│
├── utils/
│   ├── cn.ts                        # clsx + twMerge (Tailwind class merging)
│   ├── toaster.ts                   # Global toast notifications
│   └── routeUtils.ts                # RBAC route helpers
│
├── router/
│   └── index.ts                     # Route definitions + navigation guards
│
├── components/
│   ├── layout/
│   │   ├── AppLayout.vue            # Main app layout wrapper
│   │   ├── Navbar.vue               # Top bar with user menu
│   │   ├── Sidebar.vue              # Collapsible left sidebar
│   │   ├── Titlebar.vue             # Page title bar
│   │   └── Container.vue            # Content wrapper
│   │
│   └── ui/                          # 🧰 Reusable component library
│       ├── Avatar.vue
│       ├── Badge.vue
│       ├── Button.vue               # Action prop for RBAC
│       ├── Card.vue
│       ├── Checkbox.vue             # Integrated RBAC
│       ├── DataList.vue
│       ├── Drawer.vue
│       ├── Dropdown.vue
│       ├── FilterBar.vue
│       ├── Input.vue                # Integrated RBAC
│       ├── Loader.vue
│       ├── Modal.vue
│       ├── MultiSelect.vue          # Integrated RBAC
│       ├── Pagination.vue
│       ├── Select.vue               # Integrated RBAC
│       ├── Table.vue                # Search, sort, pagination
│       ├── TextArea.vue             # Integrated RBAC
│       ├── Toast.vue
│       └── UserMenu.vue
│
├── config/
│   └── sidebarLinks.ts              # Sidebar menu structure + permissions
│
└── views/                           # Page components (plural kebab-case folder names)
    ├── LoginView.vue
    ├── AppShellView.vue             # Main layout wrapper
    ├── HomeView.vue
    ├── DashboardView.vue
    │
    ├── users/
    │   ├── UserList.vue
    │   ├── UserForm.vue
    │   ├── UserProfile.vue
    │   └── ChangePasswordView.vue
    │
    ├── roles/
    │   ├── RoleList.vue
    │   └── RoleForm.vue
    │
    ├── settings/
    │   └── GlobalSettings.vue
    │
    ├── flows/                       # NEW
    │   ├── FlowList.vue
    │   └── FlowForm.vue
    │
    └── reconciliation/              # NEW
        ├── DashboardView.vue        # KPIs by flow
        ├── OperationalView.vue      # Pending entries + force/exclude
        │   ├── ForceMatchModal.vue  # Modal for force matching
        │   └── ExclusionModal.vue   # Modal for exclusion
        ├── TransversalView.vue      # By reco_id view
        ├── RunsView.vue             # Ingestion + reconciliation runs
        └── AuditView.vue            # Data + UI action logs
```

## Key Files Explained

### 1. `api/axios.ts` — HTTP Client

```typescript
const api = axios.create({
  baseURL: import.meta.env.VITE_API_URL || 'http://localhost:8000/api/v1',
  withCredentials: true,  // Send cookies (auth token)
});

// CSRF protection
api.interceptors.request.use(config => {
  if (['post', 'put', 'delete', 'patch'].includes(config.method?.toLowerCase())) {
    const csrfToken = getCookie('csrf_token');
    if (csrfToken) {
      config.headers['X-CSRF-Token'] = csrfToken;
    }
  }
  return config;
});

// Auto-logout on 401
api.interceptors.response.use(
  response => response,
  error => {
    if (error.response?.status === 401) {
      window.location.href = '/login';
    }
    return Promise.reject(error);
  }
);
```

**Key points**:
- `withCredentials: true` sends auth cookies
- CSRF token auto-injected on mutation methods
- 401 redirects to login

### 2. `types/index.ts` — Centralized Interfaces

```typescript
// User/Auth (from Orchestro)
export interface User { ... }
export interface Role { ... }

// Reconciliation (NEW)
export interface Flow {
  id: number;
  code: string;
  name: string;
  source_type: FlowSourceType;
  parser_type: ParserType;
  match_key_strategy: MatchKeyStrategy;
  accounts: FlowAccount[];
}

export interface ReconciliationEntry {
  id: number;
  flow_id: number;
  reco_id?: string;
  amount: string;
  currency: string;
  value_date: string;
  status: EntryStatus;
  match_group_id?: number;
}

export interface MatchGroup {
  id: number;
  flow_id: number;
  reco_id?: string;
  currency: string;
  total: string;
  mode: 'auto' | 'forced';
  created_at: string;
}

// ... more types
```

**All types centralized** — services and components import from here.

### 3. `stores/auth.ts` — Authentication & Permissions

```typescript
export const useAuthStore = defineStore('auth', () => {
  const user = ref<User | null>(null);
  const isAuthenticated = computed(() => !!user.value);
  
  const hasPermission = (path: string) => {
    // Check if user's roles have access to path
    return user.value?.roles.some(role =>
      role.accessible_pages.some(p => p.path === path && p.access_level !== 'NONE')
    );
  };
  
  const fetchUser = async () => {
    try {
      const { data } = await userService.getMe();
      user.value = data;
    } catch {
      user.value = null;
    }
  };
  
  const login = async (email: string, password: string) => {
    const { data } = await authService.login(email, password);
    user.value = data.user;
  };
  
  // ... logout, etc.
});
```

**Used in**:
- Router guards (check permissions before route)
- UI components (show/hide based on access level)
- Sidebar (filter menu items by permission)

### 4. `services/flowService.ts` — Example Service

```typescript
import api from '@/api/axios';
import type { FlowCreate, FlowUpdate } from '@/types';

const resource = '/flows';

export default {
  getAll() {
    return api.get(`${resource}/`);
  },
  get(id: number) {
    return api.get(`${resource}/${id}`);
  },
  create(data: FlowCreate) {
    return api.post(`${resource}/`, data);
  },
  update(id: number, data: FlowUpdate) {
    return api.put(`${resource}/${id}`, data);
  },
  delete(id: number) {
    return api.delete(`${resource}/${id}`);
  },
};
```

**Pattern**:
- Literal object (not class)
- Single responsibility (one resource)
- Returns Axios Promise (caller handles `.data`, error handling)

### 5. `router/index.ts` — Route Definitions & Guards

```typescript
const router = createRouter({
  history: createWebHistory(),
  routes: [
    {
      path: '/login',
      name: 'login',
      component: LoginView,
      meta: { guest: true },  // Only for non-authenticated
    },
    {
      path: '/',
      component: AppShellView,
      children: [
        { path: '', name: 'home', component: HomeView },
        { path: 'flows', component: FlowList, meta: { requiresAuth: true } },
        { path: 'flows/:id', component: FlowForm, meta: { requiresAuth: true } },
        { path: 'reconciliation', component: DashboardView, meta: { requiresAuth: true } },
        { path: 'reconciliation/operational', ... },
        // ... more routes
      ],
    },
  ],
});

router.beforeEach(async (to, from, next) => {
  const authStore = useAuthStore();
  
  // Restore session
  if (!authStore.user) {
    await authStore.fetchUser();
  }
  
  // Block if user is blocked
  if (authStore.isAuthenticated && authStore.user?.blocked) {
    await authStore.logout();
    return;
  }
  
  // Redirect if password expired
  if (authStore.mustChangePassword && to.path !== '/change-password') {
    next('/change-password');
    return;
  }
  
  // Check permissions
  const requiresAuth = to.matched.some(r => r.meta?.requiresAuth);
  if (requiresAuth && !authStore.hasPermission(to.path)) {
    next('/');  // Redirect to home
    return;
  }
  
  next();
});
```

**Key checks**:
1. Restore session if user is null
2. Block if user is blocked
3. Force password change if expired
4. Check RBAC permissions

### 6. `components/ui/Button.vue` — RBAC-Integrated Component

```vue
<script setup lang="ts">
import { computed } from 'vue';
import { useAuthStore } from '@/stores/auth';

interface Props {
  action?: 'create' | 'edit' | 'delete' | 'view';
  variant?: 'primary' | 'secondary' | 'danger';
  disabled?: boolean;
}

const props = withDefaults(defineProps<Props>(), {
  variant: 'secondary',
});

const authStore = useAuthStore();

// Map action to required permission level
const actionToPermission: Record<string, string> = {
  create: 'create',
  edit: 'edit',
  delete: 'delete',
};

const canAccess = computed(() => {
  if (!props.action) return true;
  // Check if user has permission for this action
  // (simplified; real logic checks accessible_pages)
  return authStore.hasPermission(`/${props.action}`);
});
</script>

<template>
  <button
    v-if="canAccess"
    :class="variantClass"
    :disabled="disabled"
    @click="$emit('click')"
  >
    <slot />
  </button>
</template>
```

**Key**: `action` prop auto-hides button if user lacks permission.

### 7. `components/ui/Table.vue` — Data Display Component

```vue
<script setup lang="ts">
interface Props {
  columns: Array<{ key: string; label: string; sortable?: boolean }>;
  items: Array<any>;
  searchable?: boolean;
  searchPlaceholder?: string;
}

// Computed: filtered, sorted, paginated items
// Methods: sort, search, paginate
</script>

<template>
  <div class="space-y-4">
    <input v-if="searchable" v-model="search" :placeholder="searchPlaceholder" />
    <table>
      <thead>
        <tr>
          <th v-for="col in columns" :key="col.key">
            {{ col.label }}
            <span v-if="col.sortable">↕</span>
          </th>
        </tr>
      </thead>
      <tbody>
        <tr v-for="item in filteredItems" :key="item.id">
          <td v-for="col in columns" :key="col.key">
            <slot :name="`cell-${col.key}`" :item="item">
              {{ item[col.key] }}
            </slot>
          </td>
        </tr>
      </tbody>
    </table>
    <Pagination v-model="currentPage" :total-pages="totalPages" />
  </div>
</template>
```

**Features**:
- Configurable columns
- Search built-in
- Slot-based cells (custom rendering)
- Pagination

### 8. `views/flows/FlowList.vue` — Example List View

```vue
<script setup lang="ts">
import { ref, onMounted } from 'vue';
import flowService from '@/services/flowService';
import type { Flow } from '@/types';

const loading = ref(true);
const items = ref<Flow[]>([]);

const fetchData = async () => {
  try {
    const { data } = await flowService.getAll();
    items.value = data;
  } catch (e) {
    toaster.error('Failed to load flows');
  } finally {
    loading.value = false;
  }
};

onMounted(fetchData);
</script>

<template>
  <div class="flex justify-between items-center mb-6">
    <h1 class="text-2xl font-bold">Flows</h1>
    <Button action="create" @click="router.push('/flows/create')">Create</Button>
  </div>
  
  <div v-if="loading"><Loader /></div>
  
  <Card v-else>
    <Table :columns="columns" :items="items" searchable>
      <template #cell-is_active="{ item }">
        <Badge :variant="item.is_active ? 'success' : 'secondary'">
          {{ item.is_active ? 'Active' : 'Inactive' }}
        </Badge>
      </template>
      <template #cell-actions="{ item }">
        <Button action="edit" @click="...">Edit</Button>
      </template>
    </Table>
  </Card>
</template>
```

**Pattern**:
1. Declare refs (loading, items)
2. Define fetchData() method
3. Call onMounted(() => fetchData())
4. Template: show loader → table

### 9. `views/reconciliation/OperationalView.vue` — Complex View

Reconciliation operational view with:
- Flow / Status / Reco ID filters
- Multi-select entries
- Force match modal
- Exclusion modal
- Dynamic actions (Exclude button only for pending)

**Template structure**:
1. Filter card (flow, status, reco_id, currency, date range)
2. Table with checkboxes
3. Modals (ForceMatchModal, ExclusionModal)

**Key interactions**:
- Checkbox toggles entry selection
- "Force match" button opens modal with selected entries
- Modal validates: same flow/currency, sum=0 or comment required
- "Exclude" button per entry opens exclusion modal
- Modal requires mandatory reason

### 10. `config/sidebarLinks.ts` — Navigation Menu

```typescript
export const sidebarGroups: SidebarGroup[] = [
  {
    id: 'main-nav',
    items: [
      { label: 'Dashboard', to: '/reconciliation', icon: LayoutDashboard, permission: '/reconciliation' },
      { label: 'Operational', to: '/reconciliation/operational', ... },
      { label: 'Transversal', to: '/reconciliation/transversal', ... },
      { label: 'Runs', to: '/reconciliation/runs', ... },
      { label: 'Audit', to: '/reconciliation/audit', ... },
    ],
  },
];

export const adminItems: AdminItem[] = [
  { label: 'Flows', to: '/flows', icon: Workflow, permission: '/flows' },
  { label: 'Settings', to: '/admin/settings', icon: Settings, permission: '/admin/settings' },
  // ... user/role management
];

// These are filtered by authStore.hasPermission()
```

---

## Frontend Rules (Strict)

1. **`<script setup lang="ts">` ALWAYS**
   ```vue
   ❌ <script> export default { ... }
   ✅ <script setup lang="ts"> ... </script>
   ```

2. **Type all props and emits**
   ```typescript
   interface Props { ... }
   const props = defineProps<Props>();
   const emit = defineEmits<{ (e: 'submit', data: any): void }>();
   ```

3. **No `this`, no `reactive()` for primitives**
   ```typescript
   ❌ this.data = 'value'
   ❌ const state = reactive({ count: 0 })
   ✅ const data = ref('value')
   ✅ const count = ref(0)
   ```

4. **Reuse UI components**
   ```typescript
   ❌ <input type="text" /> → ✅ <Input />
   ❌ <div class="modal" /> → ✅ <Modal />
   ❌ alert("error") → ✅ toaster.error("error")
   ❌ <table> raw → ✅ <Table :columns :items />
   ```

5. **Use services, not direct fetch**
   ```typescript
   ❌ fetch('/api/flows')
   ✅ flowService.getAll()
   ```

6. **RBAC via Button `action` prop and stores**
   ```vue
   <Button action="create">Create</Button>  <!-- Auto-hidden if no permission -->
   <Input :disabled="!authStore.hasPermission('/edit')">
   ```

---

## Common Patterns

### Fetch & Display Data
```typescript
const loading = ref(true);
const items = ref([]);

onMounted(async () => {
  try {
    const { data } = await service.getAll();
    items.value = data;
  } catch (e) {
    toaster.error('Failed to load');
  } finally {
    loading.value = false;
  }
});
```

### Form Submission
```typescript
const form = ref({ name: '', email: '' });
const submitting = ref(false);

const submit = async () => {
  submitting.value = true;
  try {
    await service.create(form.value);
    toaster.success('Created');
    router.push('/items');
  } catch (e: any) {
    toaster.error(e?.response?.data?.detail || 'Save failed');
  } finally {
    submitting.value = false;
  }
};
```

### Modal with Validation
```typescript
const props = defineProps<{ modelValue: boolean; item: Item | null }>();
const emit = defineEmits<{ (e: 'update:modelValue', v: boolean): void; (e: 'saved'): void }>();

const close = () => emit('update:modelValue', false);

const submit = async () => {
  if (!validate()) return toaster.error('...');
  try {
    await service.save(item);
    emit('saved');
    close();
  } catch (e) {
    toaster.error('...');
  }
};
```

---

## Performance Tips

- **Computed vs watch**: Use computed for derived state, watch for side effects
- **v-if vs v-show**: Use v-if to remove from DOM, v-show for CSS toggle
- **Key in v-for**: Always provide `:key` (object id, not index)
- **Debounce search**: Use debounce for input filters

---

## Testing

- No vitest yet (awaiting setup)
- Manual testing via dev server: `npm run dev`
- Browser DevTools console for errors
- Network tab to inspect API calls
