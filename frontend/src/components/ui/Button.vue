<script setup lang="ts">
import { computed } from 'vue';
import { useRoute } from 'vue-router';
import { useAuthStore } from '@/stores/auth';
import { cva, type VariantProps } from 'class-variance-authority';
import { cn } from '@/utils/cn';

const route = useRoute();
const authStore = useAuthStore();

const buttonVariants = cva(
  'inline-flex items-center justify-center font-medium focus:outline-none disabled:opacity-50 disabled:cursor-not-allowed transition-all duration-200',
  {
    variants: {
      variant: {
        primary: 'rounded bg-blue-600 text-white hover:bg-blue-700 focus:ring-2 focus:ring-offset-2 focus:ring-blue-500',
        secondary: 'rounded bg-gray-200 text-gray-700 hover:bg-gray-300 focus:ring-2 focus:ring-offset-2 focus:ring-gray-500',
        danger: 'rounded bg-red-600 text-white hover:bg-red-700 focus:ring-2 focus:ring-offset-2 focus:ring-red-500',
        ghost: 'rounded bg-transparent hover:bg-gray-100 text-gray-700',
        // Reveals Brand Variants
        'reveals-primary': 'rounded bg-tropical-mint text-space-indigo hover:bg-ocean-mist focus:ring-2 focus:ring-offset-2 focus:ring-tropical-mint',
        'reveals-secondary': 'rounded bg-ocean-mist text-space-indigo hover:bg-turquoise-surf focus:ring-2 focus:ring-offset-2 focus:ring-ocean-mist',
        'reveals-ghost': 'rounded bg-transparent border border-space-indigo/30 text-space-indigo hover:bg-lavender-blush focus:ring-2 focus:ring-offset-2 focus:ring-space-indigo',
        'reveals-accent': 'rounded bg-bright-lemon text-space-indigo hover:opacity-80 focus:ring-2 focus:ring-offset-2 focus:ring-bright-lemon',
        // Modern Minimalist Variants
        'modern-primary': 'rounded-lg bg-zinc-900 text-white hover:bg-zinc-800 active:scale-[0.98]',
        'modern-secondary': 'rounded-lg bg-transparent text-zinc-700 border border-zinc-300 hover:bg-zinc-50 hover:border-zinc-400',
        'modern-gradient': 'rounded-lg bg-gradient-to-r from-violet-600 to-indigo-600 text-white shadow-lg shadow-violet-500/25 hover:shadow-xl hover:shadow-violet-500/30 hover:-translate-y-0.5',
        'modern-ghost': 'rounded-lg text-zinc-600 hover:bg-zinc-100',
        // Modern Reveals (Gradient with Reveals colors)
        'modern-reveals-primary': 'rounded-lg bg-gradient-to-r from-tropical-mint to-ocean-mist text-space-indigo shadow-lg shadow-tropical-mint/25 hover:shadow-xl hover:-translate-y-0.5',
        'modern-reveals-secondary': 'rounded-lg bg-transparent text-space-indigo border border-space-indigo/30 hover:bg-lavender-blush hover:border-space-indigo/50',
        'modern-reveals-gradient': 'rounded-lg bg-gradient-to-r from-turquoise-surf to-tropical-mint text-space-indigo shadow-lg shadow-turquoise-surf/25 hover:shadow-xl hover:-translate-y-0.5',
        'modern-reveals-ghost': 'rounded-lg text-space-indigo hover:bg-lavender-blush',
      },
      size: {
        sm: 'px-2.5 py-1.5 text-xs',
        md: 'px-4 py-2 text-sm',
        lg: 'px-6 py-3 text-base',
      },
    },
    defaultVariants: {
      variant: 'primary',
      size: 'md',
    },
  }
);

const props = defineProps<{
  type?: 'button' | 'submit' | 'reset';
  variant?: VariantProps<typeof buttonVariants>['variant'];
  size?: VariantProps<typeof buttonVariants>['size'];
  class?: string;
  action?: 'create' | 'update' | 'delete' | 'save'; // Automatic RBAC handling
}>();

const isVisible = computed(() => {
  if (!props.action) return true;
  
  // Attempt to get context from route
  const level = authStore.getPermissionLevel(route.path);
  
  // If no specific permission found, assume visible (legacy behavior)
  if (!level) return true;

  if (props.action === 'delete') {
    return level === 'ALL';
  }
  
  if (['create', 'update', 'save'].includes(props.action)) {
    return level === 'ALL' || level === 'EDIT';
  }

  return true;
});
</script>

<template>
  <button
    v-if="isVisible"
    :type="type || 'button'"
    :class="cn(buttonVariants({ variant, size }), $props.class)"
  >
    <slot />
  </button>
</template>
