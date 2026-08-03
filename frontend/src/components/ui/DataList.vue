<script setup lang="ts">
import { computed } from 'vue';

const props = withDefaults(defineProps<{
  items: { label: string; value: string | number }[];
  title?: string;
  subtitle?: string;
  theme?: 'default' | 'reveals';
}>(), {
  theme: 'default',
});

const titleClasses = computed(() => {
  if (props.theme === 'reveals') {
    return 'text-base font-semibold leading-7 text-space-indigo font-rubik';
  }
  return 'text-base font-semibold leading-7 text-space-indigo';
});

const subtitleClasses = computed(() => {
  if (props.theme === 'reveals') {
    return 'mt-1 max-w-2xl text-sm leading-6 text-space-indigo/60 font-inter';
  }
  return 'mt-1 max-w-2xl text-sm leading-6 text-space-indigo/50';
});

const borderClasses = computed(() => {
  return props.theme === 'reveals' ? 'border-t border-space-indigo/10' : 'border-t border-space-indigo/10';
});

const dividerClasses = computed(() => {
  return props.theme === 'reveals' ? 'divide-y divide-space-indigo/10' : 'divide-y divide-space-indigo/10';
});

const labelClasses = computed(() => {
  if (props.theme === 'reveals') {
    return 'text-sm font-medium leading-6 text-space-indigo font-inter';
  }
  return 'text-sm font-medium leading-6 text-space-indigo';
});

const valueClasses = computed(() => {
  if (props.theme === 'reveals') {
    return 'mt-1 text-sm leading-6 text-space-indigo/70 sm:col-span-2 sm:mt-0 font-inter';
  }
  return 'mt-1 text-sm leading-6 text-space-indigo/70 sm:col-span-2 sm:mt-0';
});
</script>

<template>
  <div>
    <div v-if="title || subtitle" class="px-4 sm:px-0 mb-4">
      <h3 v-if="title" :class="titleClasses">{{ title }}</h3>
      <p v-if="subtitle" :class="subtitleClasses">{{ subtitle }}</p>
    </div>
    <div :class="['mt-2', borderClasses]">
      <dl :class="dividerClasses">
        <div v-for="(item, index) in items" :key="index" class="px-4 py-3 sm:grid sm:grid-cols-3 sm:gap-4 sm:px-0">
          <dt :class="labelClasses">{{ item.label }}</dt>
          <dd :class="valueClasses">{{ item.value }}</dd>
        </div>
      </dl>
    </div>
  </div>
</template>
