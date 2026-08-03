<script setup lang="ts">
import Titlebar from './Titlebar.vue';
import Sidebar from './Sidebar.vue';
import { useSidebarStore } from '@/stores/sidebar';
import Container from './Container.vue';

const sidebarStore = useSidebarStore();
</script>

<template>
  <div class="min-h-screen bg-gray-50 flex flex-col">
    <Titlebar />
    <div class="flex flex-1 overflow-hidden pt-16">
      <Sidebar />
      <main :class="['flex-1 overflow-hidden', 'transition-all', 'duration-300', sidebarStore.isOpen ? 'ml-64' : 'ml-20']">
        <Container>
          <slot />
        </Container>
      </main>
    </div>
  </div>
</template>