import { defineStore } from 'pinia';
import { ref } from 'vue';

export const useSidebarStore = defineStore('sidebar', () => {
  const isOpen = ref(true);

  const toggle = () => {
    isOpen.value = !isOpen.value;
  };

  return { isOpen, toggle };
});
