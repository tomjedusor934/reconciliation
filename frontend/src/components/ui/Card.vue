<script setup lang="ts">
defineProps<{
  title?: string;
  description?: string;
}>();
</script>

<template>
  <div class="bg-white shadow rounded-lg border border-space-indigo/10 overflow-hidden">
    <div v-if="title || $slots.header" class="px-4 py-5 sm:px-6 border-b border-space-indigo/10 bg-gray-50">
      <slot name="header">
        <h3 class="text-lg leading-6 font-medium text-space-indigo">{{ title }}</h3>
        <p v-if="description" class="mt-1 max-w-2xl text-sm text-space-indigo/50">{{ description }}</p>
      </slot>
    </div>
    <div class="px-4 py-5 sm:p-6 min-w-0">
      <slot />
    </div>
    <div v-if="$slots.footer" class="px-4 py-4 sm:px-6 bg-gray-50 border-t border-space-indigo/10">
      <slot name="footer" />
    </div>
  </div>
</template>
