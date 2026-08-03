<script setup lang="ts">
import { ref, onMounted, onUnmounted, watch } from 'vue';

const props = defineProps<{
  isOpen: boolean;
  title?: string;
  size?: 'sm' | 'md' | 'lg' | 'xl' | '2xl';
  noOverlay?: boolean;
}>();

const emit = defineEmits(['close']);

const sizeClasses = {
  sm: 'max-w-md',
  md: 'max-w-lg',
  lg: 'max-w-2xl',
  xl: 'max-w-4xl',
  '2xl': 'max-w-7xl',
};

const close = () => {
  emit('close');
};

const handleKeydown = (e: KeyboardEvent) => {
  if (e.key === 'Escape' && props.isOpen) {
    close();
  }
};

onMounted(() => {
  document.addEventListener('keydown', handleKeydown);
});

onUnmounted(() => {
  document.removeEventListener('keydown', handleKeydown);
});

// Prevent body scroll when open (skip for noOverlay panels)
watch(() => props.isOpen, (val: any) => {
  if (props.noOverlay) return;
  if (val) {
    document.body.style.overflow = 'hidden';
  } else {
    document.body.style.overflow = '';
  }
});
</script>

<template>
  <Teleport to="body">
    <div v-if="isOpen" class="relative z-50" aria-labelledby="slide-over-title" role="dialog" :aria-modal="!noOverlay">
      <!-- Background backdrop -->
      <div 
        v-if="!noOverlay"
        class="fixed inset-0 bg-space-indigo/60 transition-opacity" 
        @click="close"
        aria-hidden="true"
      ></div>

      <div :class="noOverlay ? 'fixed top-0 right-0 z-50' : 'fixed inset-0 overflow-hidden'">
        <div :class="noOverlay ? '' : 'absolute inset-0 overflow-hidden'">
          <div :class="noOverlay ? '' : 'pointer-events-none fixed inset-y-0 right-0 flex max-w-full pl-10'">
            <div 
              class="pointer-events-auto w-screen transform transition ease-in-out duration-500 sm:duration-700 bg-white shadow-xl"
              :class="[sizeClasses[props.size || 'md'], noOverlay ? 'rounded-bl-lg max-h-[90vh]' : '']"
            >
              <div :class="noOverlay ? 'flex flex-col overflow-visible bg-white' : 'flex h-full flex-col overflow-y-scroll bg-white shadow-xl'">
                <div class="px-4 py-4 sm:px-6">
                  <div class="flex items-start justify-between">
                    <h2 class="text-lg font-medium text-space-indigo" id="slide-over-title">
                      {{ title }}
                    </h2>
                    <div class="ml-3 flex h-7 items-center">
                      <button 
                        type="button" 
                        class="rounded-md bg-white text-space-indigo/40 hover:text-space-indigo/60 focus:outline-none focus:ring-2 focus:ring-tropical-mint focus:ring-offset-2"
                        @click="close"
                      >
                        <span class="sr-only">Close panel</span>
                        <svg class="h-6 w-6" fill="none" viewBox="0 0 24 24" stroke-width="1.5" stroke="currentColor" aria-hidden="true">
                          <path stroke-linecap="round" stroke-linejoin="round" d="M6 18L18 6M6 6l12 12" />
                        </svg>
                      </button>
                    </div>
                  </div>
                </div>
                <div class="relative px-4 pb-4 sm:px-6" :class="noOverlay ? '' : 'flex-1 py-6'">
                  <!-- Content Slot -->
                  <slot></slot>
                </div>
              </div>
            </div>
          </div>
        </div>
      </div>
    </div>
  </Teleport>
</template>
