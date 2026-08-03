<script setup lang="ts">
import { useAuthStore } from '@/stores/auth';
import { useRouter } from 'vue-router';
import Button from '@/components/ui/Button.vue';

const authStore = useAuthStore();
const router = useRouter();

const logout = async () => {
  await authStore.logout();
  router.push('/login');
};
</script>

<style scoped>
  /* "inline-flex items-center border-b-2 border-transparent px-1 pt-1 text-sm font-medium text-gray-500 hover:border-gray-300 hover:text-gray-700" */
  .navigation {
    @apply inline-flex items-center border-b-2 border-transparent px-1 pt-1 text-sm font-medium text-space-indigo/60 hover:border-ocean-mist hover:text-space-indigo;
  }
</style>

<template>
  <nav class="bg-white shadow">
    <div class="mx-auto max-w-7xl px-4 sm:px-6 lg:px-8">
      <div class="flex h-16 justify-between">
        <div class="flex">
          <div class="flex flex-shrink-0 items-center">
            <span class="text-xl font-bold text-ocean-mist">Orchestro</span>
          </div>
          <div class="hidden sm:ml-6 sm:flex sm:space-x-8">
            <router-link to="/" class="navigation" active-class="border-ocean-mist text-space-indigo">
              Dashboard
            </router-link>
             <router-link v-if="authStore.hasPermission('/users')" to="/users" class="navigation" active-class="border-ocean-mist text-space-indigo">
              Users
            </router-link>
             <router-link v-if="authStore.hasPermission('/roles')" to="/roles" class="navigation" active-class="border-ocean-mist text-space-indigo">
              Roles
            </router-link>
          </div>
        </div>
        <div class="flex items-center">
          <div class="flex-shrink-0">
            <Button variant="secondary" @click="logout">Logout</Button>
          </div>
        </div>
      </div>
    </div>
  </nav>
</template>
