<script setup lang="ts">
import { ref, computed } from 'vue';
import { ChevronDown } from 'lucide-vue-next';
import { useSidebarStore } from '@/stores/sidebar';

interface NavItem {
  label: string;
  to: string;
  icon: any;
  permission?: string;
}

const props = defineProps<{
  id: string;
  title: string;
  icon: any;
  items: NavItem[];
}>();

const sidebarStore = useSidebarStore();

// Create a unique key based on the id (not title, which can change)
const storageKey = computed(() => `sidebar-group-${props.id}-expanded`);

// Initialize from localStorage or default to true
const getInitialState = () => {
  const stored = localStorage.getItem(storageKey.value);
  return stored ? JSON.parse(stored) : true;
};

const isExpanded = ref(getInitialState());

// Display state: expanded only if sidebar is open AND isExpanded is true
const displayExpanded = computed(() => isExpanded.value && sidebarStore.isOpen);

// Toggle expanded and open sidebar if it's closed
const toggleExpanded = () => {
  if (!sidebarStore.isOpen) {
    sidebarStore.toggle();
  }
  isExpanded.value = !isExpanded.value;
  localStorage.setItem(storageKey.value, JSON.stringify(isExpanded.value));
};
</script>

<style scoped>
  .nav-item-active {
    @apply border-ocean-mist bg-ocean-mist-50 text-ocean-mist-700;
  }
</style>

<template>
  <div>
    <!-- Group Header -->
    <div class="flex items-center gap-3 border-l-4 border-transparent px-4 py-3 text-sm font-medium text-space-indigo/60 hover:bg-gray-50 hover:border-ocean-mist hover:text-space-indigo cursor-pointer transition-colors" @click="toggleExpanded">
      <component :is="icon" class="w-5 h-5 flex-shrink-0" />
      <span class="transition-opacity duration-300 flex-1">{{ title }}</span>
      <ChevronDown v-if="sidebarStore.isOpen" :class="['w-4 h-4 transition-transform duration-300 flex-shrink-0', { 'rotate-180': displayExpanded }]" />
    </div>

    <!-- Group Items -->
    <div :class="['overflow-hidden transition-all duration-300', displayExpanded ? 'max-h-96' : 'max-h-0']">
      <router-link
        v-for="item in items"
        :key="item.to"
        :to="item.to"
        @click.stop
        exact-active-class="nav-item-active"
        class="flex items-center gap-3 border-l-4 border-transparent px-4 py-3 text-sm font-medium text-space-indigo/60 hover:bg-gray-50 hover:border-ocean-mist hover:text-space-indigo transition-colors ml-4"
      >
        <component :is="item.icon" class="w-5 h-5 flex-shrink-0" />
        <span class="transition-opacity duration-300">{{ item.label }}</span>
      </router-link>
    </div>
  </div>
</template>
