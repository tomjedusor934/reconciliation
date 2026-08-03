<script setup lang="ts">
import { computed } from 'vue';
import { useRoute } from 'vue-router';
import { useAuthStore } from '@/stores/auth';
import { cn } from '@/utils/cn';

const route = useRoute();
const authStore = useAuthStore();

const props = defineProps<{
  label?: string;
  placeholder?: string;
  options: { value: string | number; label: string }[];
  id?: string;
  class?: string;
  theme?: 'default' | 'reveals';
  disabled?: boolean;
  clearable?: boolean;
}>();

const model = defineModel<string | number | null>();

const isReadOnly = computed(() => {
  if (route.path === '/login' || route.path === '/change-password') return false;
  const level = authStore.getPermissionLevel(route.path);
  return level === 'NONE';
});

const isDisabled = computed(() => props.disabled || isReadOnly.value);

const selectClasses = computed(() => {
  const base = 'mt-2 block w-full rounded-md border-0 py-1.5 pl-3 pr-10 sm:text-sm sm:leading-6 ring-1 ring-inset disabled:cursor-not-allowed disabled:bg-gray-100 disabled:text-space-indigo/50';
  
  if (props.theme === 'reveals') {
    return cn(
      base,
      'text-space-indigo ring-space-indigo/30 focus:ring-2 focus:ring-tropical-mint bg-white',
      props.class
    );
  }
  
  return cn(
    base,
    'text-space-indigo ring-space-indigo/20 focus:ring-2 focus:ring-tropical-mint',
    props.class
  );
});

const labelClasses = computed(() => {
  if (props.theme === 'reveals') {
    return 'block text-sm font-medium leading-6 text-space-indigo font-inter';
  }
  return 'block text-sm font-medium leading-6 text-space-indigo';
});
</script>

<template>
  <div>
    <label v-if="label" :for="id" :class="labelClasses">{{ label }}</label>
    <select
      :id="id"
      v-model="model"
      :class="selectClasses"
      :disabled="isDisabled"
    >
      <option v-if="placeholder && !clearable" value="" disabled selected hidden>{{ placeholder }}</option>
      <option v-if="clearable" :value="null">{{ placeholder || '— Aucun —' }}</option>
      <option v-for="option in options" :key="option.value" :value="option.value">
        {{ option.label }}
      </option>
    </select>
  </div>
</template>
