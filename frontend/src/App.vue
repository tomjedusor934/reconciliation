<script setup lang="ts">
import { onMounted, onUnmounted } from 'vue';
import Toast from '@/components/ui/Toast.vue';
import GlobalModal from '@/components/ui/GlobalModal.vue';
import settingsService from '@/services/settingsService';
import { useAuthStore } from '@/stores/auth';
import api from '@/api/axios';

const authStore = useAuthStore();

let lastActivity = Date.now();
let lastRefresh = Date.now();
let sessionTimeoutMs = 60 * 60 * 1000; // default 60 min, overridden on mount
let checkInterval: ReturnType<typeof setInterval> | null = null;
const REFRESH_INTERVAL_MS = 10 * 60 * 1000; // refresh token every 10 min of activity
const ACTIVITY_EVENTS = ['mousemove', 'keydown', 'click', 'scroll', 'touchstart'];

function onUserActivity() {
  lastActivity = Date.now();
}

async function initSessionManagement() {
  try {
    const { data } = await settingsService.getPasswordSettings();
    if (data?.session_timeout_minutes) {
      sessionTimeoutMs = data.session_timeout_minutes * 60 * 1000;
    }
  } catch {
    // use default
  }

  ACTIVITY_EVENTS.forEach(e => document.addEventListener(e, onUserActivity, { passive: true }));

  checkInterval = setInterval(async () => {
    if (!authStore.isAuthenticated) return;
    const now = Date.now();
    const idle = now - lastActivity;
    if (idle >= sessionTimeoutMs) {
      await authStore.logout();
      return;
    }
    if (now - lastRefresh >= REFRESH_INTERVAL_MS) {
      try {
        await api.post('/auth/refresh');
        lastRefresh = now;
      } catch {
        // If refresh fails (401), the axios interceptor will redirect to login
      }
    }
  }, 60 * 1000);
}

onMounted(async () => {
  // Load app title
  try {
    const { data } = await settingsService.getAppName();
    document.title = data.value;
  } catch (error) {
    console.error('Error loading app title:', error);
  }



  // Load app icon
  try {
    const { data } = await settingsService.getAppIcon();
    if (data.value) {
      let svgString = data.value as string;

      // Add xmlns if missing (required for SVG favicons)
      if (!svgString.includes('xmlns')) {
        svgString = svgString.replace('<svg', '<svg xmlns="http://www.w3.org/2000/svg"');
      }

      // Remove all existing favicons
      document.querySelectorAll('link[rel="icon"], link[rel="shortcut icon"]').forEach(el => el.remove());

      // Encode to base64 (Unicode compatible)
      const base64 = btoa(unescape(encodeURIComponent(svgString)));
      const faviconLink = document.createElement('link');
      faviconLink.rel = 'icon';
      faviconLink.type = 'image/svg+xml';
      faviconLink.href = `data:image/svg+xml;base64,${base64}`;
      document.head.appendChild(faviconLink);
    }
  } catch (error) {
    console.warn('No icon configured, using default favicon:', error);
  }

  await initSessionManagement();
});

onUnmounted(() => {
  if (checkInterval) clearInterval(checkInterval);
  ACTIVITY_EVENTS.forEach(e => document.removeEventListener(e, onUserActivity));
});
</script>

<template>
  <Toast />
  <GlobalModal />
  <router-view></router-view>
</template>

