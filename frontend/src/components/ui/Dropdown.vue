<script setup lang="ts">
import { ref, computed } from 'vue';
import { onClickOutside } from '@vueuse/core';
import Button from './Button.vue';
import { cn } from '@/utils/cn';

const props = withDefaults(defineProps<{
  label: string;
  items: { label: string; action: () => void }[];
  align?: 'left' | 'right';
  theme?: 'default' | 'reveals';
}>(), {
  align: 'left',
  theme: 'default',
});

const isOpen = ref(false);
const target = ref(null);

onClickOutside(target, () => isOpen.value = false);

const buttonVariant = computed(() => {
  return props.theme === 'reveals' ? 'reveals-secondary' : 'secondary';
});

const menuItemClasses = computed(() => {
  if (props.theme === 'reveals') {
    return 'text-space-indigo block px-4 py-2 text-sm w-full text-left hover:bg-tropical-mint/10 focus:bg-tropical-mint/10 outline-none font-inter';
  }
  return 'text-space-indigo block px-4 py-2 text-sm w-full text-left hover:bg-gray-50 focus:bg-gray-50 outline-none';
});

const iconClasses = computed(() => {
  return props.theme === 'reveals' ? '-mr-1 h-5 w-5 text-space-indigo/50' : '-mr-1 h-5 w-5 text-gray-400';
});
</script>

<template>
  <div ref="target" class="relative inline-block text-left">
    <Button @click="isOpen = !isOpen" type="button" :variant="buttonVariant" class="inline-flex w-full justify-center gap-x-1.5" :aria-expanded="isOpen">
      {{ label }}
      <svg :class="iconClasses" viewBox="0 0 20 20" fill="currentColor" aria-hidden="true">
        <path fill-rule="evenodd" d="M5.23 7.21a.75.75 0 011.06.02L10 11.168l3.71-3.938a.75.75 0 111.08 1.04l-4.25 4.5a.75.75 0 01-1.08 0l-4.25-4.5a.75.75 0 01.02-1.06z" clip-rule="evenodd" />
      </svg>
    </Button>

    <transition
      enter-active-class="transition ease-out duration-100"
      enter-from-class="transform opacity-0 scale-95"
      enter-to-class="transform opacity-100 scale-100"
      leave-active-class="transition ease-in duration-75"
      leave-from-class="transform opacity-100 scale-100"
      leave-to-class="transform opacity-0 scale-95"
    >
      <div
        v-if="isOpen"
        :class="cn(
          'absolute z-10 mt-2 w-56 origin-top-right rounded-md bg-white shadow-lg ring-1 ring-black ring-opacity-5 focus:outline-none',
          align === 'right' ? 'right-0' : 'left-0'
        )"
        role="menu"
        aria-orientation="vertical"
        tabindex="-1"
      >
        <div class="py-1" role="none">
          <button
            v-for="(item, index) in items"
            :key="index"
            @click="item.action(); isOpen = false"
            :class="menuItemClasses"
            role="menuitem"
          >
            {{ item.label }}
          </button>
        </div>
      </div>
    </transition>
  </div>
</template>
