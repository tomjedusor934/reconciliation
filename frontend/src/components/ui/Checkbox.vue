<script setup lang="ts">
import { computed } from 'vue';
import { useRoute } from 'vue-router';
import { useAuthStore } from '@/stores/auth';
import { cn } from '@/utils/cn';

const route = useRoute();
const authStore = useAuthStore();

const props = defineProps<{
  label?: string;
  id?: string;
  disabled?: boolean;
  error?: string;
  theme?: 'default' | 'reveals';
}>();

const checked = defineModel<boolean>({ default: false });

const isReadOnly = computed(() => {
  const level = authStore.getPermissionLevel(route.path);
  return level === 'NONE';
});

const isDisabled = computed(() => props.disabled || isReadOnly.value);
</script>

<template>
  <div class="flex items-start">
    <div class="flex h-6 items-center">
      <input
        :id="id"
        v-model="checked"
        type="checkbox"
        :disabled="isDisabled"
        :class="cn(
          'h-4 w-4 rounded border',
          theme === 'reveals' 
            ? 'border-space-indigo/30 text-tropical-mint focus:ring-tropical-mint focus:ring-offset-0'
            : 'border-gray-300 text-indigo-600 focus:ring-indigo-600',
          isDisabled && 'cursor-not-allowed opacity-50',
          error && 'border-red-300 text-red-600 focus:ring-red-600',
          $attrs.class as string
        )"
      />
    </div>
    <div v-if="label" class="ml-3 text-sm leading-6">
      <label 
        :for="id" 
        :class="cn(
          'font-medium',
          theme === 'reveals' ? 'text-space-indigo font-inter' : 'text-gray-900',
          isDisabled && 'opacity-50'
        )"
      >
        {{ label }}
      </label>
      <p v-if="error" class="text-red-500 text-xs mt-1">{{ error }}</p>
    </div>
  </div>
</template>
