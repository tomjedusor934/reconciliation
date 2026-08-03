<script setup lang="ts">
import { ChevronRightIcon, HomeIcon } from 'lucide-vue-next';

interface BreadcrumbItem {
  label: string;
  to?: string | object;
  disabled?: boolean;
}

defineProps<{
  items: BreadcrumbItem[];
}>();
</script>

<template>
  <nav class="flex" aria-label="Breadcrumb">
    <ol role="list" class="flex items-center space-x-2">
      <li>
        <div>
          <router-link to="/" class="text-space-indigo/40 hover:text-space-indigo/50">
            <HomeIcon class="h-5 w-5 flex-shrink-0" aria-hidden="true" />
            <span class="sr-only">Home</span>
          </router-link>
        </div>
      </li>
      
      <li v-for="item in items" :key="item.label">
        <div class="flex items-center">
          <ChevronRightIcon class="h-5 w-5 flex-shrink-0 text-space-indigo/40" aria-hidden="true" />
          <span
             v-if="item.disabled || !item.to"
             class="ml-2 text-sm font-medium text-space-indigo/50"
             aria-current="page"
          >
            {{ item.label }}
          </span>
          <router-link
            v-else
            :to="item.to"
            class="ml-2 text-sm font-medium text-space-indigo/50 hover:text-space-indigo/70"
          >
            {{ item.label }}
          </router-link>
        </div>
      </li>
    </ol>
  </nav>
</template>
