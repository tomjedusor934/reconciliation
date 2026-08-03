<script setup lang="ts">
import { ref, watch, nextTick } from 'vue';
import Button from '@/components/ui/Button.vue';

const props = defineProps<{
  isOpen: boolean;
  title?: string;
  variant?: 'default' | 'blurred-backdrop';
}>();

const emit = defineEmits<{
  (e: 'close'): void;
}>();

const modalRef = ref<HTMLElement | null>(null);

watch(() => props.isOpen, async (val:any) => {
  if (val) {
    await nextTick();
    // Simple focus management: focus the modal container or first focusable element could be added here
    // manually if needed, without external dependencies.
    if (modalRef.value) {
      modalRef.value.focus();
    }
  }
});
</script>

<template>
  <Teleport to="body">
    <div v-if="isOpen" class="relative z-50" :aria-labelledby="title ? 'modal-title' : undefined" role="dialog" aria-modal="true">
      <!-- Background backdrop -->
      <div v-if="props.variant === 'default' || !props.variant" class="fixed inset-0 bg-space-indigo/60 transition-opacity" @click="emit('close')"></div>
      <div v-else-if="props.variant === 'blurred-backdrop'" class="fixed inset-0 bg-space-indigo/50 backdrop-blur-sm" @click="console.log('Backdrop clicked')"></div>

      <div class="fixed inset-0 z-10 overflow-y-auto">
        <div class="flex min-h-full items-end justify-center p-4 text-center sm:items-center sm:p-0">
          <!-- Modal panel -->
          <div ref="modalRef" class="relative transform rounded-lg bg-white text-left shadow-xl transition-all sm:my-8 sm:w-full sm:max-w-lg">
            <div class="bg-white px-4 pb-4 pt-5 sm:p-6 sm:pb-4 min-w-0">
              <div class="sm:flex sm:items-start">
                <div class="mt-3 text-center sm:ml-4 sm:mt-0 sm:text-left w-full">
                  <h3 v-if="title" class="text-base font-semibold leading-6 text-space-indigo" id="modal-title">{{ title }}</h3>
                  <div class="mt-2">
                    <slot />
                  </div>
                </div>
              </div>
            </div>
            <div class="bg-gray-100 px-4 py-3 sm:flex sm:flex-row-reverse sm:px-6">
              <slot name="footer">
                <Button variant="secondary" @click="emit('close')">Close</Button>
              </slot>
            </div>
          </div>
        </div>
      </div>
    </div>
  </Teleport>
</template>
