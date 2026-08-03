<script setup lang="ts">
import { computed } from 'vue';
import { cn } from '@/utils/cn';
import { cva, type VariantProps } from 'class-variance-authority';

const avatarVariants = cva(
  'inline-flex items-center justify-center rounded-full font-semibold overflow-hidden ring-2 ring-white text-white flex-shrink-0', 
  {
    variants: {
      size: {
        sm: 'h-8 w-8 text-xs',
        md: 'h-10 w-10 text-sm',
        lg: 'h-14 w-14 text-base',
      },
      variant: {
        default: 'bg-gradient-to-br from-space-indigo to-space-indigo-600',
        mint: 'bg-gradient-to-br from-tropical-mint to-ocean-mist',
        ocean: 'bg-gradient-to-br from-ocean-mist to-turquoise-surf',
        indigo: 'bg-gradient-to-br from-space-indigo-600 to-space-indigo-700',
      },
    },
    defaultVariants: {
      size: 'md',
      variant: 'default',
    },
  }
);

const props = defineProps<{
  src?: string | null;
  alt?: string;
  initials?: string;
  size?: VariantProps<typeof avatarVariants>['size'];
  variant?: VariantProps<typeof avatarVariants>['variant'];
  class?: string;
}>();

const derivedInitials = computed(() => {
  if (props.initials) return props.initials.slice(0, 2).toUpperCase();
  if (props.alt) {
    return props.alt
      .split(' ')
      .map((n) => n[0])
      .slice(0, 2)
      .join('')
      .toUpperCase();
  }
  return '??';
});
</script>

<template>
  <div :class="cn(avatarVariants({ size, variant }), $props.class)">
    <img
      v-if="src"
      :src="src"
      :alt="alt || 'Avatar'"
      class="h-full w-full object-cover"
    />
    <span v-else>
      {{ derivedInitials }}
    </span>
  </div>
</template>
