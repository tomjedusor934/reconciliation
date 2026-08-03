<script setup lang="ts">
import { ref, computed, onMounted } from 'vue';
import { useRouter } from 'vue-router';
import { useAuthStore } from '@/stores/auth';
import { PencilLine, LogOut, User, Mail } from 'lucide-vue-next';
import Avatar from '@/components/ui/Avatar.vue';
import { onClickOutside } from '@vueuse/core';

const authStore = useAuthStore();
const router = useRouter();
const isMenuOpen = ref(false);
const menuRef = ref<HTMLDivElement>();

const userInitials = computed(() => {
  if (!authStore.user) return 'U';
  
  const name = authStore.user.full_name || authStore.user.email;
  if (authStore.user.full_name) {
    return authStore.user.full_name
      .split(' ')
      .map((n) => n[0])
      .slice(0, 2)
      .join('')
      .toUpperCase();
  }
  
  // For email, take first letter + first letter of domain
  const email = authStore.user.email || '';
  if (!email) return 'U';
  const parts = email.split('@');
  return (parts[0][0] + (parts[1]?.[0] || '')).toUpperCase();
});

const handleEdit = () => {
  router.push('/profile');
  isMenuOpen.value = false;
};

const handleLogout = async () => {
  await authStore.logout();
  router.push('/login');
  isMenuOpen.value = false;
};

onMounted(() => {
  if (menuRef.value) {
    onClickOutside(menuRef.value, () => {
      isMenuOpen.value = false;
    });
  }
});
</script>

<template>
  <div class="relative" ref="menuRef">
    <button 
      @click="isMenuOpen = !isMenuOpen"
      class="cursor-pointer transition-all duration-200 hover:scale-105 hover:shadow-glow-indigo rounded-full"
      aria-label="User menu"
      :class="{ 'ring-2 ring-tropical-mint': isMenuOpen }"
    >
      <Avatar size="md" :initials="userInitials" variant="mint" />
    </button>
    
    <!-- Dropdown Menu -->
    <transition
      enter-active-class="transition ease-out duration-150"
      enter-from-class="transform opacity-0 scale-95 -translate-y-2"
      enter-to-class="transform opacity-100 scale-100 translate-y-0"
      leave-active-class="transition ease-in duration-100"
      leave-from-class="transform opacity-100 scale-100 translate-y-0"
      leave-to-class="transform opacity-0 scale-95 -translate-y-2"
    >
      <div
        v-show="isMenuOpen"
        class="absolute right-0 mt-3 w-64 bg-white rounded-lg shadow-lg z-50 border border-lavender-blush overflow-hidden"
        @click="isMenuOpen = false"
      >
        <!-- User Info Header -->
        <div class="bg-gradient-to-r from-space-indigo to-space-indigo-600 px-4 py-4 text-white">
          <div class="flex items-center gap-3">
            <Avatar size="lg" :initials="userInitials" variant="mint" />
            <div class="flex-1 min-w-0">
              <p class="font-semibold truncate">
                {{ authStore.user?.full_name || 'User' }}
              </p>
              <p class="text-sm text-white/70 truncate flex items-center gap-1">
                <Mail size="14" />
                {{ authStore.user?.email }}
              </p>
            </div>
          </div>
        </div>

        <!-- Menu Items -->
        <div class="py-2 space-y-1">
          <button
            @click.stop="handleEdit"
            class="flex items-center gap-3 w-full px-4 py-3 text-sm text-space-indigo hover:bg-lavender-blush/50 transition-colors duration-150 group"
          >
            <div class="w-8 h-8 rounded-lg bg-tropical-mint/10 group-hover:bg-tropical-mint/20 flex items-center justify-center transition-colors">
              <PencilLine size="16" class="text-tropical-mint" />
            </div>
            <span class="font-medium">Edit Profile</span>
          </button>
          
          <div class="border-t border-lavender-blush my-1"></div>

          <button
            @click.stop="handleLogout"
            class="flex items-center gap-3 w-full px-4 py-3 text-sm text-red-600 hover:bg-red-50 transition-colors duration-150 group"
          >
            <div class="w-8 h-8 rounded-lg bg-red-100 group-hover:bg-red-200 flex items-center justify-center transition-colors">
              <LogOut size="16" class="text-red-600" />
            </div>
            <span class="font-medium">Logout</span>
          </button>
        </div>

        <!-- Footer Info -->
        <div class="px-4 py-3 bg-space-indigo/5 border-t border-lavender-blush text-xs text-space-indigo/60 text-center">
          <p>Signed in as <span class="font-semibold">{{ authStore.user?.email }}</span></p>
        </div>
      </div>
    </transition>
  </div>
</template>
