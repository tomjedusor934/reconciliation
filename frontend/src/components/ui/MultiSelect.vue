<script setup lang="ts">
import { ref, computed } from 'vue';
import { onClickOutside } from '@vueuse/core';
import { useRoute } from 'vue-router';
import { useAuthStore } from '@/stores/auth';

const route = useRoute();
const authStore = useAuthStore();

interface Option {
  value: string | number;
  label: string;
}

const props = defineProps<{
  modelValue: (string | number)[];
  options: Option[];
  label?: string;
  placeholder?: string;
  theme?: 'default' | 'reveals';
  disabled?: boolean;
  searchable?: boolean;
}>();

const emit = defineEmits<{
  (e: 'update:modelValue', value: (string | number)[]): void;
}>();

const isReadOnly = computed(() => {
  if (route.path === '/login' || route.path === '/change-password') return false;
  const level = authStore.getPermissionLevel(route.path);
  return level === 'NONE';
});

const isDisabled = computed(() => props.disabled || isReadOnly.value);

const isOpen = ref(false);
const searchQuery = ref('');
const containerRef = ref<HTMLElement | null>(null);

onClickOutside(containerRef, () => {
  isOpen.value = false;
  searchQuery.value = '';
});

const filteredOptions = computed(() => {
  if (!props.searchable || !searchQuery.value.trim()) return props.options;
  const q = searchQuery.value.trim().toLowerCase();
  return props.options.filter((opt: Option) => opt.label.toLowerCase().includes(q));
});

const handleToggle = () => {
    if (isDisabled.value) return;
    isOpen.value = !isOpen.value;
}

const selectedLabels = computed(() => {
  return props.options
    .filter((opt: Option) => props.modelValue.includes(opt.value))
    .map((opt: Option) => opt.label);
});

const toggleOption = (value: string | number) => {
  if (isDisabled.value) return; 
  
  const newValue = [...props.modelValue];
  const index = newValue.indexOf(value);
  
  if (index === -1) {
    newValue.push(value);
  } else {
    newValue.splice(index, 1);
  }
  
  emit('update:modelValue', newValue);
};

// Theme-aware classes
const labelClasses = computed(() => {
  if (props.theme === 'reveals') {
    return 'block text-sm font-medium leading-6 text-space-indigo font-inter mb-1';
  }
  return 'block text-sm font-medium leading-6 text-space-indigo mb-1';
});

const triggerClasses = computed(() => {
  let base = 'min-h-[38px] w-full cursor-pointer rounded-md border-0 py-1.5 pl-3 pr-10 sm:text-sm sm:leading-6 bg-white relative ring-1 ring-inset';
  
  if (isDisabled.value) {
      base = 'min-h-[38px] w-full cursor-not-allowed rounded-md border-0 py-1.5 pl-3 pr-10 sm:text-sm sm:leading-6 bg-gray-100 relative ring-1 ring-inset text-space-indigo/50';
      return base + ' ring-space-indigo/20';
  }

  if (props.theme === 'reveals') {
    return `${base} text-space-indigo ring-space-indigo/30 focus:ring-2 focus:ring-tropical-mint`;
  }
  return `${base} text-space-indigo ring-space-indigo/20 focus:ring-2 focus:ring-tropical-mint`;
});

const tagClasses = computed(() => {
  if (props.theme === 'reveals') {
    return 'inline-flex items-center rounded-md bg-tropical-mint/20 px-2 py-1 text-xs font-medium text-space-indigo ring-1 ring-inset ring-tropical-mint/30';
  }
  return 'inline-flex items-center rounded-md bg-indigo-50 px-2 py-1 text-xs font-medium text-gray-700 ring-1 ring-inset ring-gray-300';
});

const optionHoverClass = computed(() => {
  if (props.theme === 'reveals') {
    return 'hover:bg-tropical-mint/10';
  }
  return 'hover:bg-indigo-50';
});

const checkIconClass = computed(() => {
  if (props.theme === 'reveals') {
    return 'absolute inset-y-0 right-0 flex items-center pr-4 text-tropical-mint';
  }
  return 'absolute inset-y-0 right-0 flex items-center pr-4 text-indigo-600';
});

</script>

<template>
  <div ref="containerRef" class="relative">
    <label v-if="label" :class="labelClasses">{{ label }}</label>
    
    <div
      @click="handleToggle"
      :class="triggerClasses"
    >
      <div v-if="selectedLabels.length === 0" :class="theme === 'reveals' ? 'text-space-indigo/50' : 'text-gray-500'">
        {{ placeholder || 'Select options...' }}
      </div>
      <div v-else class="flex flex-wrap gap-1">
        <span
          v-for="label in selectedLabels"
          :key="label"
          :class="tagClasses"
        >
          {{ label }}
        </span>
      </div>
      
      <span class="pointer-events-none absolute inset-y-0 right-0 flex items-center pr-2">
        <svg :class="theme === 'reveals' ? 'h-5 w-5 text-space-indigo/40' : 'h-5 w-5 text-gray-400'" viewBox="0 0 20 20" fill="currentColor" aria-hidden="true">
          <path fill-rule="evenodd" d="M10 3a1 1 0 01.707.293l3 3a1 1 0 01-1.414 1.414L10 5.414 7.707 7.707a1 1 0 01-1.414-1.414l3-3A1 1 0 0110 3zm-3.707 9.293a1 1 0 011.414 0L10 14.586l2.293-2.293a1 1 0 011.414 1.414l-3 3a1 1 0 01-1.414 0l-3-3a1 1 0 010-1.414z" clip-rule="evenodd" />
        </svg>
      </span>
    </div>

    <div
      v-if="isOpen"
      class="absolute z-10 mt-1 max-h-60 w-full overflow-auto rounded-md bg-white py-1 text-base shadow-lg ring-1 ring-black ring-opacity-5 focus:outline-none sm:text-sm"
    >
      <div v-if="searchable" class="px-2 pb-1 pt-1 sticky top-0 bg-white">
        <input
          v-model="searchQuery"
          type="text"
          class="w-full rounded border border-gray-200 px-2 py-1 text-sm focus:outline-none focus:ring-1 focus:ring-tropical-mint"
          placeholder="Rechercher…"
          @click.stop
        />
      </div>
      <div
        v-for="option in filteredOptions"
        :key="option.value"
        @click="toggleOption(option.value)"
        :class="['relative cursor-default select-none py-2 pl-3 pr-9', optionHoverClass, theme === 'reveals' ? 'text-space-indigo' : 'text-gray-900']"
      >
        <span :class="[modelValue.includes(option.value) ? 'font-semibold' : 'font-normal', 'block truncate']">
          {{ option.label }}
        </span>

        <span
          v-if="modelValue.includes(option.value)"
          :class="checkIconClass"
        >
          <svg class="h-5 w-5" viewBox="0 0 20 20" fill="currentColor" aria-hidden="true">
            <path fill-rule="evenodd" d="M16.704 4.153a.75.75 0 01.143 1.052l-8 10.5a.75.75 0 01-1.127.075l-4.5-4.5a.75.75 0 011.06-1.06l3.894 3.893 7.48-9.817a.75.75 0 011.05-.143z" clip-rule="evenodd" />
          </svg>
        </span>
      </div>
    </div>
  </div>
</template>
