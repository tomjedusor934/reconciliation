<script setup lang="ts">
import { cva, type VariantProps } from 'class-variance-authority';
import { cn } from '@/utils/cn';

const loaderVariants = cva('animate-spin text-current', {
  variants: {
    size: {
      sm: 'h-4 w-4',
      md: 'h-8 w-8',
      lg: 'h-12 w-12',
    },
    variant: {
      primary: 'text-indigo-600',
      white: 'text-white',
      gray: 'text-gray-400',
      // Reveals Brand
      'reveals': 'text-tropical-mint',
      'reveals-secondary': 'text-turquoise-surf',
      // Modern Minimalist
      'modern': 'text-zinc-900',
      'modern-secondary': 'text-violet-500',
      // Modern Reveals
      'modern-reveals': 'text-tropical-mint',
      'modern-reveals-secondary': 'text-turquoise-surf',
    },
  },
  defaultVariants: {
    size: 'md',
    variant: 'primary',
  },
});

defineProps<{
  size?: VariantProps<typeof loaderVariants>['size'];
  variant?: VariantProps<typeof loaderVariants>['variant'];
  class?: string;
}>();
</script>

<template>
  <svg
    :class="cn(loaderVariants({ size, variant }), $props.class)"
    xmlns="http://www.w3.org/2000/svg"
    fill="none"
    viewBox="0 0 24 24"
  >
    <circle
      class="opacity-25"
      cx="12"
      cy="12"
      r="10"
      stroke="currentColor"
      stroke-width="4"
    ></circle>
    <path
      class="opacity-75"
      fill="currentColor"
      d="M4 12a8 8 0 018-8V0C5.373 0 0 5.373 0 12h4zm2 5.291A7.962 7.962 0 014 12H0c0 3.042 1.135 5.824 3 7.938l3-2.647z"
    ></path>
  </svg>
</template>
