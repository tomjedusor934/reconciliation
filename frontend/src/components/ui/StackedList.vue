<script setup lang="ts">
import { computed } from 'vue';
import Avatar from './Avatar.vue';

export interface StackedListItem {
  id: string | number;
  title: string;
  subtitle?: string;
  initials?: string;
  avatarSrc?: string;
  meta?: string;
}

const props = withDefaults(defineProps<{
  items: StackedListItem[];
  theme?: 'default' | 'reveals';
}>(), {
  theme: 'default',
});

const emit = defineEmits<{
  (e: 'click', item: StackedListItem): void;
}>();

const listClasses = computed(() => {
  const base = 'divide-y rounded-md border shadow-sm';
  if (props.theme === 'reveals') {
    return `${base} divide-space-indigo/10 border-space-indigo/20 bg-white`;
  }
  return `${base} divide-space-indigo/10 border-space-indigo/10 bg-white`;
});

const itemClasses = computed(() => {
  const base = 'flex justify-between gap-x-6 py-5 px-5 cursor-pointer transition-colors first:rounded-t-md last:rounded-b-md';
  if (props.theme === 'reveals') {
    return `${base} hover:bg-tropical-mint/10`;
  }
  return `${base} hover:bg-gray-50`;
});

const titleClasses = computed(() => {
  if (props.theme === 'reveals') {
    return 'text-sm font-semibold leading-6 text-space-indigo font-inter';
  }
  return 'text-sm font-semibold leading-6 text-space-indigo';
});

const subtitleClasses = computed(() => {
  if (props.theme === 'reveals') {
    return 'mt-1 truncate text-xs leading-5 text-space-indigo/60 font-inter';
  }
  return 'mt-1 truncate text-xs leading-5 text-space-indigo/50';
});

const metaClasses = computed(() => {
  if (props.theme === 'reveals') {
    return 'text-sm leading-6 text-space-indigo font-inter';
  }
  return 'text-sm leading-6 text-space-indigo';
});
</script>

<template>
  <ul role="list" :class="listClasses">
    <li
      v-for="item in items"
      :key="item.id"
      :class="itemClasses"
      @click="emit('click', item)"
    >
      <div class="flex min-w-0 gap-x-4">
        <Avatar v-if="item.avatarSrc || item.initials" :src="item.avatarSrc" :initials="item.initials" size="sm" :class="theme === 'reveals' ? 'flex-none bg-gray-50' : 'flex-none bg-gray-50'" />
        <div class="min-w-0 flex-auto">
          <p :class="titleClasses">{{ item.title }}</p>
          <p v-if="item.subtitle" :class="subtitleClasses">{{ item.subtitle }}</p>
        </div>
      </div>
      <div v-if="$slots.meta || item.meta" class="hidden shrink-0 sm:flex sm:flex-col sm:items-end">
        <slot name="meta" :item="item">
          <p :class="metaClasses">{{ item.meta }}</p>
        </slot>
      </div>
    </li>
  </ul>
</template>
