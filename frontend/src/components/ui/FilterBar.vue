<script setup lang="ts">
defineProps<{
  title?: string;
}>();

const emit = defineEmits<{
  (e: 'clear'): void;
}>();
</script>

<template>
  <div class="bg-white p-4 rounded-lg shadow-sm border border-space-indigo/10 mb-6">
    <div class="flex flex-col sm:flex-row sm:items-center justify-between gap-4">
      <div v-if="title" class="text-sm font-medium text-space-indigo/50">
        {{ title }}
      </div>
      
      <div class="flex-1 flex flex-col sm:flex-row flex-wrap gap-4 items-start sm:items-center">
        <slot />
      </div>

      <div v-if="$slots.actions" class="flex items-center gap-2 flex-shrink-0 sm:self-auto self-stretch">
        <slot name="actions" />
      </div>
    </div>
  </div>
</template>
