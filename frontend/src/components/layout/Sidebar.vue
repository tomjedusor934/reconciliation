<script setup lang="ts">
import { computed } from 'vue'
import { useAuthStore } from '@/stores/auth'
import { useSidebarStore } from '@/stores/sidebar'
import SidebarGroup from '../ui/SidebarGroup.vue'
import { sidebarGroups, adminItems, filterSidebarGroups, filterAdminItems, isLink, isGroup } from '@/config/sidebarLinks'
import type { SidebarLink, SidebarGroup as SidebarGroupType } from '@/config/sidebarLinks'

const authStore = useAuthStore()
const sidebarStore = useSidebarStore()

const filteredMainNavGroups = computed(() =>
  filterSidebarGroups(sidebarGroups, (path) => authStore.hasPermission(path))
)

const filteredAdminItems = computed(() =>
  filterAdminItems(adminItems, (path) => authStore.hasPermission(path))
)

const isNavEmpty = computed(() => 
  filteredMainNavGroups.value.length === 0 && filteredAdminItems.value.length === 0
)
</script>

<style scoped>
  .sidebar {
    @apply transition-all duration-300 ease-in-out fixed left-0 z-30;
    top: 64px;
    height: calc(100vh - 64px);
  }

  .sidebar.closed {
    @apply w-20;
  }

  .sidebar.open {
    @apply w-64;
  }

  .navigation {
    @apply flex items-center gap-3 border-l-4 border-transparent px-4 py-3 text-sm font-medium text-space-indigo/60 hover:bg-gray-50 hover:border-ocean-mist hover:text-space-indigo transition-colors;
  }

  .navigation.router-link-active,
  .navigation.nav-item-active {
    @apply border-ocean-mist bg-ocean-mist-50 text-ocean-mist-700;
  }

  .nav-text {
    @apply transition-opacity duration-300;
  }

  .closed .nav-text {
    @apply opacity-0 hidden;
  }

  .open .nav-text {
    @apply opacity-100;
  }

  .closed :deep(.nav-item-text) {
    @apply opacity-0 hidden;
  }

  .open :deep(.nav-item-text) {
    @apply opacity-100;
  }
</style>

<template>
  <aside :class="['sidebar', 'flex', 'flex-col', 'bg-white', 'shadow-lg', sidebarStore.isOpen ? 'open' : 'closed']">
    <!-- Main Navigation -->
    <nav class="flex-1 overflow-y-auto">
      <!-- Groupes principaux -->
      <template v-for="(group, idx) in filteredMainNavGroups" :key="group.id">
        <!-- Section principale directe (sans titre ni bord) -->
        <div v-if="group.id === 'main-nav'" class="space-y-1 px-2 py-4">
          <template v-for="item in group.items" :key="isLink(item) ? (item as SidebarLink).permission : (item as SidebarGroupType).id">
            <!-- Afficher un lien simple -->
            <router-link 
              v-if="isLink(item)"
              :to="(item as SidebarLink).to" 
              class="navigation"
              exact-active-class="nav-item-active"
              :title="!sidebarStore.isOpen ? (item as SidebarLink).label : ''"
            >
              <component :is="(item as SidebarLink).icon" class="w-5 h-5 flex-shrink-0" />
              <span class="nav-text">{{ (item as SidebarLink).label }}</span>
            </router-link>

            <!-- Afficher un groupe avec expand/collapse -->
            <SidebarGroup 
              v-else-if="isGroup(item)"
              :id="(item as SidebarGroupType).id"
              :title="sidebarStore.isOpen ? (item as SidebarGroupType).title : ''"
              :icon="(item as SidebarGroupType).icon" 
              :items="(item as SidebarGroupType).items"
            />
          </template>
        </div>

        <!-- Groupes secondaires (avec titre et bord) -->
        <div v-else class="border-t border-space-indigo/10" :class="idx > 0 ? '' : ''">
          <nav class="space-y-1 px-2 py-4">
            <div 
              v-if="group.title && sidebarStore.isOpen" 
              class="text-xs font-semibold text-space-indigo/50 px-4 py-2 uppercase tracking-wide"
            >
              {{ group.title }}
            </div>

            <SidebarGroup 
              :id="group.id"
              :title="sidebarStore.isOpen ? group.title : ''"
              :icon="group.icon" 
              :items="group.items"
            />
          </nav>
        </div>
      </template>
    </nav>

    <!-- Admin Section (en bas) -->
    <div v-if="filteredAdminItems.length > 0 && authStore.isAdmin" class="border-t border-space-indigo/10">
      <nav class="space-y-1 px-2 py-4">
        <div v-if="sidebarStore.isOpen" class="text-xs font-semibold text-space-indigo/50 px-4 py-2 uppercase tracking-wide">
          Admin
        </div>
        <template v-for="item in filteredAdminItems" :key="isLink(item) ? (item as SidebarLink).to : (item as SidebarGroupType).id">
          <!-- Afficher un lien simple -->
          <router-link 
            v-if="isLink(item)"
            :to="(item as SidebarLink).to" 
            class="navigation"
            exact-active-class="nav-item-active"
            :title="!sidebarStore.isOpen ? (item as SidebarLink).label : ''"
          >
            <component :is="(item as SidebarLink).icon" class="w-5 h-5 flex-shrink-0" />
            <span class="nav-text">{{ (item as SidebarLink).label }}</span>
          </router-link>

          <!-- Afficher un groupe avec expand/collapse -->
          <SidebarGroup 
            v-else-if="isGroup(item)"
            :id="(item as SidebarGroupType).id"
            :title="sidebarStore.isOpen ? (item as SidebarGroupType).title : ''"
            :icon="(item as SidebarGroupType).icon" 
            :items="(item as SidebarGroupType).items"
          />
        </template>
      </nav>
    </div>

    <!-- État vide -->
    <div v-if="isNavEmpty" class="px-4 py-8 text-center text-xs text-space-indigo/40">
      No accessible items
    </div>
  </aside>
</template>
