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
  placeholder?: string;
  required?: boolean;
  error?: string;
  class?: string;
  rows?: number;
  theme?: 'default' | 'reveals' | 'modern' | 'modern-reveals';
  disabled?: boolean;
}>();

const model = defineModel<string | number>();

const isReadOnly = computed(() => {
  const level = authStore.getPermissionLevel(route.path);
  return level === 'NONE';
});

const isDisabled = computed(() => props.disabled || isReadOnly.value);

const labelClasses = computed(() => {
  switch (props.theme) {
    case 'reveals':
      return 'text-sm font-medium text-space-indigo font-inter';
    case 'modern':
      return 'text-sm font-medium text-zinc-700 font-jakarta';
    case 'modern-reveals':
      return 'text-sm font-medium text-space-indigo font-jakarta';
    default:
      return 'text-sm font-medium text-gray-700';
  }
});

const inputClasses = computed(() => {
  const base = 'w-full px-4 py-2.5 text-sm transition-all duration-200 focus:outline-none disabled:cursor-not-allowed disabled:bg-gray-100 disabled:text-space-indigo/50';
  
  switch (props.theme) {
    case 'reveals':
      return `${base} border rounded border-space-indigo/30 text-space-indigo placeholder:text-space-indigo/40 focus:ring-2 focus:ring-tropical-mint font-inter`;
    case 'modern':
      return `${base} border rounded-xl border-zinc-300 text-zinc-900 placeholder:text-zinc-400 focus:border-zinc-500 focus:ring-4 focus:ring-zinc-500/10 font-jakarta bg-white`;
    case 'modern-reveals':
      return `${base} border rounded-xl border-space-indigo/30 text-space-indigo placeholder:text-space-indigo/40 focus:border-tropical-mint focus:ring-4 focus:ring-tropical-mint/20 font-jakarta bg-white`;
    default:
      return `${base} border rounded border-gray-300 focus:ring-2 focus:ring-blue-500`;
  }
});
</script>

<template>
  <div class="flex flex-col gap-1">
    <label 
      v-if="label" 
      :for="id" 
      :class="labelClasses"
    >
      {{ label }} <span v-if="required" class="text-red-500">*</span>
    </label>
    
    <textarea
      :id="id"
      v-model="model"
      :class="cn(inputClasses, props.class)"
      :placeholder="placeholder"
      :rows="rows || 3"
      :required="required"
      :disabled="isDisabled"
    ></textarea>
    
    <span v-if="error" class="text-xs text-red-500">{{ error }}</span>
  </div>
</template>
