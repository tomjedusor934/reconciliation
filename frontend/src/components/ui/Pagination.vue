<script setup lang="ts">
import { computed } from 'vue';

const props = withDefaults(defineProps<{
  page: number;
  totalPages: number;
  totalItems?: number;
  itemsPerPage?: number;
  theme?: 'default' | 'reveals';
}>(), {
  theme: 'default',
});

const emit = defineEmits<{
  (e: 'update:page', page: number): void;
}>();

const pages = computed(() => {
  const range = [];
  for (let i = 1; i <= props.totalPages; i++) {
    if (
      i === 1 ||
      i === props.totalPages ||
      (i >= props.page - 1 && i <= props.page + 1)
    ) {
      range.push(i);
    } else if (
      (i === props.page - 2 && i > 1) ||
      (i === props.page + 2 && i < props.totalPages)
    ) {
      range.push('...');
    }
  }
  return [...new Set(range)];
});

// Theme-aware classes
const containerClasses = computed(() => {
  const base = 'flex items-center justify-between px-4 py-3 sm:px-6';
  if (props.theme === 'reveals') {
    return `${base} border-t border-space-indigo/10 bg-white`;
  }
  return `${base} border-t border-space-indigo/10 bg-white`;
});

const textClasses = computed(() => {
  return props.theme === 'reveals' 
    ? 'text-sm text-space-indigo/70 font-inter' 
    : 'text-sm text-gray-700';
});

const strongTextClasses = computed(() => {
  return props.theme === 'reveals' ? 'font-medium text-space-indigo' : 'font-medium';
});

const navButtonClasses = computed(() => {
  const base = 'relative inline-flex items-center px-2 py-2 ring-1 ring-inset focus:z-20 focus:outline-offset-0 disabled:opacity-50 disabled:cursor-not-allowed';
  if (props.theme === 'reveals') {
    return `${base} text-space-indigo/50 ring-space-indigo/30 hover:bg-tropical-mint/10`;
  }
  return `${base} text-space-indigo/40 ring-space-indigo/20 hover:bg-gray-50`;
});

const pageButtonClasses = (p: number | string) => {
  if (p === props.page) {
    // Active page
    if (props.theme === 'reveals') {
      return 'relative z-10 inline-flex items-center bg-tropical-mint px-4 py-2 text-sm font-semibold text-space-indigo focus:z-20 focus-visible:outline focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-tropical-mint';
    }
    return 'relative z-10 inline-flex items-center bg-tropical-mint px-4 py-2 text-sm font-semibold text-space-indigo focus:z-20 focus-visible:outline focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-tropical-mint';
  }
  // Inactive page
  if (props.theme === 'reveals') {
    return 'relative inline-flex items-center px-4 py-2 text-sm font-semibold text-space-indigo ring-1 ring-inset ring-space-indigo/30 hover:bg-tropical-mint/10 focus:z-20 focus:outline-offset-0';
  }
  return 'relative inline-flex items-center px-4 py-2 text-sm font-semibold text-space-indigo ring-1 ring-inset ring-space-indigo/20 hover:bg-gray-50 focus:z-20 focus:outline-offset-0';
};

const ellipsisClasses = computed(() => {
  if (props.theme === 'reveals') {
    return 'relative inline-flex items-center px-4 py-2 text-sm font-semibold text-space-indigo ring-1 ring-inset ring-space-indigo/30 focus:outline-offset-0';
  }
  return 'relative inline-flex items-center px-4 py-2 text-sm font-semibold text-space-indigo ring-1 ring-inset ring-space-indigo/20 focus:outline-offset-0';
});
</script>

<template>
  <div :class="containerClasses">
    <div class="hidden sm:flex sm:flex-1 sm:items-center sm:justify-between">
      <div>
        <p v-if="totalItems" :class="textClasses">
          Showing
          <span :class="strongTextClasses">{{ (page - 1) * (itemsPerPage || 10) + 1 }}</span>
          to
          <span :class="strongTextClasses">{{ Math.min(page * (itemsPerPage || 10), totalItems) }}</span>
          of
          <span :class="strongTextClasses">{{ totalItems }}</span>
          results
        </p>
      </div>
      <div>
        <nav class="isolate inline-flex -space-x-px rounded-md shadow-sm" aria-label="Pagination">
          <button
            @click="emit('update:page', page - 1)"
            :disabled="page === 1"
            :class="[navButtonClasses, 'rounded-l-md']"
          >
            <span class="sr-only">Previous</span>
            <svg class="h-5 w-5" viewBox="0 0 20 20" fill="currentColor" aria-hidden="true">
              <path fill-rule="evenodd" d="M12.79 5.23a.75.75 0 01-.02 1.06L8.832 10l3.938 3.71a.75.75 0 11-1.04 1.08l-4.5-4.25a.75.75 0 010-1.08l4.5-4.25a.75.75 0 011.06.02z" clip-rule="evenodd" />
            </svg>
          </button>
          
          <template v-for="(p, index) in pages" :key="index">
            <span
              v-if="p === '...'"
              :class="ellipsisClasses"
            >...</span>
            <button
              v-else
              @click="emit('update:page', p as number)"
              :class="pageButtonClasses(p as number)"
            >
              {{ p }}
            </button>
          </template>

          <button
            @click="emit('update:page', page + 1)"
            :disabled="page === totalPages"
            :class="[navButtonClasses, 'rounded-r-md']"
          >
            <span class="sr-only">Next</span>
            <svg class="h-5 w-5" viewBox="0 0 20 20" fill="currentColor" aria-hidden="true">
              <path fill-rule="evenodd" d="M7.21 14.77a.75.75 0 01.02-1.06L11.168 10 7.23 6.29a.75.75 0 111.04-1.08l4.5 4.25a.75.75 0 010 1.08l-4.5 4.25a.75.75 0 01-1.06-.02z" clip-rule="evenodd" />
            </svg>
          </button>
        </nav>
      </div>
    </div>
  </div>
</template>
