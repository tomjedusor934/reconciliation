<script setup lang="ts">
import { useGlobalModal } from '@/utils/modal';
import Modal from '@/components/ui/Modal.vue';
import Button from '@/components/ui/Button.vue';

const { isOpen, options, close } = useGlobalModal();
</script>

<template>
  <Modal
    :isOpen="isOpen"
    :title="options.title"
    :variant="options.variant"
    @close="options.closable !== false ? close() : undefined"
  >
    <!-- Simple message -->
    <p v-if="options.message && !options.messageHtml" class="text-sm text-space-indigo/70">{{ options.message }}</p>

    <!-- HTML message (use v-html for styled content) -->
    <div v-if="options.messageHtml" class="text-sm text-space-indigo/70" v-html="options.messageHtml"></div>

    <!-- Or a custom component -->
    <component
      v-if="options.component"
      :is="options.component"
      v-bind="options.componentProps || {}"
    />

    <template #footer>
      <div class="flex gap-3 w-full justify-end">
        <Button
          v-for="(btn, i) in options.buttons"
          :key="i"
          :variant="btn.variant || 'primary'"
          @click="btn.action"
        >
          {{ btn.label }}
        </Button>
        <!-- Default close button if no buttons provided -->
        <Button
          v-if="!options.buttons?.length && options.closable !== false"
          variant="secondary"
          @click="close()"
        >
          Close
        </Button>
      </div>
    </template>
  </Modal>
</template>
