<script setup lang="ts">
import { useSidebarStore } from '@/stores/sidebar';
import { useRoute } from 'vue-router';
import { computed, ref, onMounted } from 'vue';
import HamburgerButton from '@/components/ui/HamburgerButton.vue';
import Breadcrumb from '../ui/Breadcrumb.vue';
import UserMenu from '@/components/ui/UserMenu.vue';
import logoPrincipal from '@/assets/images/logoPrincipal.svg';
import settingsService from '@/services/settingsService';

const sidebarStore = useSidebarStore();
const route = useRoute();
const appName = ref('OrchestroTemplate');
const appIconSvg = ref<string | null>(null);

onMounted(async () => {
  try {
    const { data } = await settingsService.getAppName();
    appName.value = data.value;
  } catch (error) {
    console.error('Error loading app name', error);
  }

  // Load SVG icon if it exists
  try {
    const { data } = await settingsService.getAppIcon();
    if (data.value) {
      appIconSvg.value = data.value;
    }
  } catch (error) {
    console.warn('No icon configured, using default', error);
  }
});

const breadcrumbItems = computed(() => {
  const pathSegments = route.path.split('/').filter(Boolean);
  
  if (pathSegments.length === 0) {
    return [];
  }

  const items = [];
  let currentPath = '';

  for (let i = 0; i < pathSegments.length; i++) {
    currentPath += '/' + pathSegments[i];
    
    // Ne pas inclure le dernier segment (page actuelle) comme cliquable
    const isLast = i === pathSegments.length - 1;
    
    // Capitaliser le label
    const label = pathSegments[i].charAt(0).toUpperCase() + pathSegments[i].slice(1);
    
    items.push({
      label,
      to: isLast ? undefined : currentPath,
      disabled: isLast
    });
  }

  return items;
});
</script>

<template>
  <nav class="fixed top-0 left-0 right-0 bg-white shadow z-40">
    <div class="mx-auto px-4 sm:px-6 lg:px-8">
      <div class="flex h-16 justify-between items-center" >
        <div class="flex items-center gap-8">
          <div class="flex items-center gap-4">
            <HamburgerButton :isOpen="sidebarStore.isOpen" @toggle="sidebarStore.toggle()" />
            <div class="flex items-center gap-2">
              <!-- Display custom SVG icon if available, otherwise the default -->
              <div v-if="appIconSvg" class="w-6 h-6 filter drop-shadow-lemon [&_svg]:w-6 [&_svg]:h-6 [&_svg]:viewBox" v-html="appIconSvg"></div>
              <img v-else :src="logoPrincipal" alt="OrchestroTemplate Logo" class="w-6 h-6 filter drop-shadow-lemon" />
              <span class="font-jakarta text-2xl bg-gradient-brand bg-clip-text text-transparent">{{ appName }}</span>
            </div>
          </div>
          <div class="hidden sm:ml-6 sm:flex sm:space-x-8" style="margin-left: 2rem;">
            <Breadcrumb :items="breadcrumbItems"/>
        </div>
        </div>
        <div class="flex items-center">
          <UserMenu />
        </div>
      </div>
    </div>
  </nav>
</template>
