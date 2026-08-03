import { ref, markRaw, type Component } from 'vue';

export interface ModalButton {
  label: string;
  variant?: 'primary' | 'secondary' | 'danger' | 'ghost';
  action: () => void;
}

export interface GlobalModalOptions {
  title?: string;
  message?: string;
  messageHtml?: string; // HTML message (use for styled content)
  component?: Component;
  componentProps?: Record<string, any>;
  buttons?: ModalButton[];
  closable?: boolean;
  variant?: 'default' | 'blurred-backdrop';
}

const isOpen = ref(false);
const options = ref<GlobalModalOptions>({});

/**
 * Open the global modal with the given options.
 */
const open = (opts: GlobalModalOptions) => {
  options.value = {
    ...opts,
    component: opts.component ? markRaw(opts.component) : undefined,
  };
  isOpen.value = true;
};

/**
 * Close the global modal.
 */
const close = () => {
  isOpen.value = false;
  options.value = {};
};

/**
 * Composable to access the global modal state (used by GlobalModal.vue).
 */
export const useGlobalModal = () => {
  return { isOpen, options, close };
};

export default { open, close };
