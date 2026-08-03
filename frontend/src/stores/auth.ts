import { defineStore } from 'pinia';
import { ref, computed } from 'vue';
import api from '@/api/axios';
import type { User, LoginCredentials, RegisterData, LoginResponse } from '@/types';
import { useRouter } from 'vue-router';

export const useAuthStore = defineStore('auth', () => {
  const user = ref<User | null>(null);
  const permissions = ref<Record<string, string>>({});
  const passwordDaysRemaining = ref<number | null>(null);
  const mustChangePassword = ref(false);
  const router = useRouter();

  const isAuthenticated = computed(() => !!user.value);
  const isAdmin = computed(() => user.value?.is_superuser || false);

  async function login(credentials: LoginCredentials): Promise<LoginResponse> {
    const params = new URLSearchParams();
    params.append('username', credentials.email);
    params.append('password', credentials.password);
    
    const response = await api.post<LoginResponse>('/auth/login', params, {
      headers: {
        'Content-Type': 'application/x-www-form-urlencoded'
      }
    });
    
    const data = response.data;
    passwordDaysRemaining.value = data.password_days_remaining;
    mustChangePassword.value = data.must_change_password;
    
    await fetchUser();
    
    return data;
  }

  async function register(data: RegisterData) {
    await api.post('/auth/signup', data);
  }

  async function fetchUser() {
    try {
      const response = await api.get('/users/me');
      user.value = response.data;

      const permResponse = await api.get('/users/me/permissions');
      permissions.value = permResponse.data || {};

      // Restore password expiration state
      const pwdStatus = await api.get('/auth/password-status');
      passwordDaysRemaining.value = pwdStatus.data.password_days_remaining;
      mustChangePassword.value = pwdStatus.data.must_change_password;
    } catch (error) {
      user.value = null;
      permissions.value = {};
      passwordDaysRemaining.value = null;
      mustChangePassword.value = false;
    }
  }

  function hasPermission(path: string): boolean {
    if (isAdmin.value) return true;
    
    // Check for exact match or directory match
    return Object.keys(permissions.value).some(allowedPath => {
      if (allowedPath === path) return true;
      if (path.startsWith(allowedPath + '/')) return true;
      return false;
    });
  }

  function getPermissionLevel(path: string): 'ALL' | 'EDIT' | 'NONE' | null {
    if (isAdmin.value) return 'ALL';
    
    // Find the matching permission path
    const matchedPath = Object.keys(permissions.value).find(allowedPath => {
      if (allowedPath === path) return true;
      if (path.startsWith(allowedPath + '/')) return true;
      return false;
    });

    if (matchedPath) return permissions.value[matchedPath] as 'ALL' | 'EDIT' | 'NONE';
    return null;
  }

  async function logout() {
    try {
      await api.post('/auth/logout');
    } catch (error) {
      console.error('Logout failed', error);
    } finally {
      user.value = null;
      permissions.value = {};
      window.location.href = '/login';
    }
  }

  return {
    user,
    permissions,
    passwordDaysRemaining,
    mustChangePassword,
    isAuthenticated,
    isAdmin,
    login,
    register,
    fetchUser,
    hasPermission,
    getPermissionLevel,
    logout
  };
});
