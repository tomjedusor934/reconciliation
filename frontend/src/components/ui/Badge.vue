<script setup lang="ts">
import { cva, type VariantProps } from 'class-variance-authority';
import { cn } from '@/utils/cn';

const badgeVariants = cva(
  'inline-flex items-center rounded-full px-2.5 py-0.5 text-xs font-medium transition-colors focus:outline-none focus:ring-2 focus:ring-ring focus:ring-offset-2',
  {
    variants: {
      variant: {
        default: 'bg-gray-100 text-gray-800 hover:bg-gray-200',
        success: 'bg-green-100 text-green-800 hover:bg-green-200',
        warning: 'bg-yellow-100 text-yellow-800 hover:bg-yellow-200',
        danger: 'bg-red-100 text-red-800 hover:bg-red-200',
        info: 'bg-blue-100 text-blue-800 hover:bg-blue-200',
        // Reveals Brand Variants
        'reveals-default': 'bg-lavender-blush text-space-indigo',
        'reveals-success': 'bg-tropical-mint text-space-indigo',
        'reveals-warning': 'bg-bright-lemon text-space-indigo',
        'reveals-info': 'bg-turquoise-surf text-white',
        'reveals-secondary': 'bg-ocean-mist text-space-indigo',
        // Modern Minimalist Variants
        'modern-default': 'bg-zinc-100 text-zinc-700',
        'modern-success': 'bg-emerald-50 text-emerald-700 ring-1 ring-inset ring-emerald-600/20',
        'modern-warning': 'bg-amber-50 text-amber-700 ring-1 ring-inset ring-amber-600/20',
        'modern-danger': 'bg-rose-50 text-rose-700 ring-1 ring-inset ring-rose-600/20',
        'modern-info': 'bg-sky-50 text-sky-700 ring-1 ring-inset ring-sky-600/20',
        'modern-gradient': 'bg-gradient-to-r from-violet-500 to-purple-500 text-white shadow-sm',
        // Modern Reveals Variants
        'modern-reveals-default': 'bg-lavender-blush text-space-indigo ring-1 ring-inset ring-space-indigo/10',
        'modern-reveals-success': 'bg-tropical-mint/20 text-space-indigo ring-1 ring-inset ring-tropical-mint/30',
        'modern-reveals-warning': 'bg-bright-lemon/30 text-space-indigo ring-1 ring-inset ring-bright-lemon/50',
        'modern-reveals-info': 'bg-turquoise-surf/20 text-space-indigo ring-1 ring-inset ring-turquoise-surf/30',
        'modern-reveals-gradient': 'bg-gradient-to-r from-tropical-mint to-ocean-mist text-space-indigo shadow-sm',
      },
    },
    defaultVariants: {
      variant: 'default',
    },
  }
);

defineProps<{
  variant?: VariantProps<typeof badgeVariants>['variant'];
  class?: string;
}>();
</script>

<template>
  <span :class="cn(badgeVariants({ variant }), $props.class)">
    <slot />
  </span>
</template>
