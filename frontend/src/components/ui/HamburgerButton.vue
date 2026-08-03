<script setup lang="ts">
import { Menu } from 'lucide-vue-next';

defineProps<{
  isOpen: boolean;
}>();

defineEmits<{
  toggle: [];
}>();
</script>

<template>
  <button
    @click="$emit('toggle')"
    class="p-2 rounded-lg hover:bg-gray-50 transition-colors"
    :aria-label="isOpen ? 'Close menu' : 'Open menu'"
  >
    <Menu class="w-6 h-6 text-space-indigo" />
  </button>
</template>
