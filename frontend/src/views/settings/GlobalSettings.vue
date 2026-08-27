<script setup lang="ts">
import { ref } from 'vue';
import { useRoute, useRouter } from 'vue-router';
import ApplicationSettingsView from './ApplicationSettingsView.vue';
import PasswordSettingsView from './PasswordSettingsView.vue';
import ConnectionsSettingsView from './ConnectionsSettingsView.vue';
import SsoSettingsView from './SsoSettingsView.vue';
import MaintenanceSettingsView from './MaintenanceSettingsView.vue';
import RcpReattributionView from './RcpReattributionView.vue';

type TabId = 'application' | 'password' | 'connections' | 'sso' | 'maintenance' | 'rcp';

const route = useRoute();
const router = useRouter();
const activeTab = ref<TabId>(
  (route.query.tab as TabId) || 'application'
);

const tabs = [
  { id: 'application', label: 'Application', name: 'Application Settings' },
  { id: 'password', label: 'Security', name: 'Security Settings' },
  { id: 'connections', label: 'Connections', name: 'Data Connections' },
  { id: 'sso', label: 'Single Sign-On', name: 'SSO Settings' },
  { id: 'maintenance', label: 'Maintenance', name: 'Maintenance' },
  // Temporary operator tool — see views/settings/RcpReattributionView.vue.
  { id: 'rcp', label: 'Réattribution RCP', name: 'RCP reattribution' }
];

const switchTab = (tabId: TabId) => {
  activeTab.value = tabId;
  // Keep the tab in the URL: a reload (or coming back to the tab) used to drop
  // the operator back on "Application", which looked like the running job had
  // been cancelled.
  router.replace({ query: { ...route.query, tab: tabId } });
};
</script>

<template>
  <div>
    <!-- Header -->
    <div class="mb-8">
      <h1 class="text-3xl font-bold text-space-indigo">Global Settings</h1>
      <p class="text-space-indigo/60 mt-2">Manage the application's general settings</p>
    </div>

    <!-- Tabs Navigation -->
    <div class="border-b border-space-indigo/10 mb-8">
      <div class="flex gap-8">
        <button
          v-for="tab in tabs"
          :key="tab.id"
          @click="switchTab(tab.id as TabId)"
          :class="[
            'px-4 py-3 font-medium border-b-2 transition-colors',
            activeTab === tab.id
              ? 'text-tropical-mint border-tropical-mint'
              : 'text-space-indigo/60 border-transparent hover:text-space-indigo'
          ]"
        >
          {{ tab.label }}
        </button>
      </div>
    </div>

    <!-- Tab Content -->
    <div>
      <ApplicationSettingsView v-if="activeTab === 'application'" />
      <PasswordSettingsView v-if="activeTab === 'password'" />
      <ConnectionsSettingsView v-if="activeTab === 'connections'" />
      <SsoSettingsView v-if="activeTab === 'sso'" />
      <MaintenanceSettingsView v-if="activeTab === 'maintenance'" />
      <RcpReattributionView v-if="activeTab === 'rcp'" />
    </div>
  </div>
</template>
