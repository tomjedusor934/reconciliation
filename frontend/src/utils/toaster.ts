import { ref } from 'vue';
import type { ToastMessage, ToastType } from '@/types';


// Global state for toasts
// This makes the state singleton and shareable
const toasts = ref<ToastMessage[]>([]);
let nextId = 0;

/**
 * Remove a toast by ID
 */
const remove = (id: number) => {
  const index = toasts.value.findIndex((t: ToastMessage) => t.id === id);
  if (index !== -1) {
    toasts.value.splice(index, 1);
  }
};

/**
 * Core function to add a toast
 */
const add = (message: string, type: ToastType = 'info', duration = 3000) => {
  const id = nextId++;
  toasts.value.push({ id, message, type, duration });

  if (duration > 0) {
    setTimeout(() => remove(id), duration);
  }
};

// --- Public helper APIs ---

export const showToast = (message: string, type: ToastType = 'info', duration = 3000) => {
  add(message, type, duration);
};

export const success = (message: string, duration = 3000) => {
  add(message, 'success', duration);
};

export const error = (message: string, duration = 3000) => {
  add(message, 'error', duration);
};

export const warning = (message: string, duration = 3000) => {
  add(message, 'warning', duration);
};

export const info = (message: string, duration = 3000) => {
  add(message, 'info', duration);
};

/**
 * Composable to access the toast state (used by Toast.vue)
 */
export const useToaster = () => {
  return {
    toasts,
    remove,
  };
};

export default {
    showToast,
    success,
    error,
    warning,
    info
};
