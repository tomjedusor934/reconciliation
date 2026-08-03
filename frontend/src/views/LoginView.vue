<script setup lang="ts">
import { ref, computed, onMounted } from 'vue';
import { useAuthStore } from '@/stores/auth';
import { useRouter, useRoute } from 'vue-router';
import { z } from 'zod';
import Button from '@/components/ui/Button.vue';
import Input from '@/components/ui/Input.vue';
import modal from '@/utils/modal';
import { warning, error as toastError } from '@/utils/toaster';
import type { LoginResponse, SSOPublicProvider } from '@/types';
import api from '@/api/axios';
import logoImage from '@/assets/images/logoPrincipal.svg';
import settingsService from '@/services/settingsService';
import ssoService from '@/services/ssoService';

const authStore = useAuthStore();
const router = useRouter();
const route = useRoute();

const email = ref('');
const password = ref('');
const errors = ref<{ email?: string; password?: string }>({});
const loading = ref(false);
const loginError = ref('');
const appName = ref('OrchestroTemplate');
const appIconSvg = ref<string | null>(null);

const ssoProviders = ref<SSOPublicProvider[]>([]);
const passwordLoginEnabled = ref(true);
const ssoEnabled = ref(false);

const showPasswordForm = computed(() => passwordLoginEnabled.value);
const showSSOButtons = computed(() => ssoEnabled.value && ssoProviders.value.length > 0);
const showSeparator = computed(() => showPasswordForm.value && showSSOButtons.value);

const SSO_ERROR_MESSAGES: Record<string, string> = {
  state_mismatch: 'Single Sign-On security check failed. Please try again.',
  provider_mismatch: 'Single Sign-On provider mismatch. Please try again.',
  missing_params: 'Single Sign-On response was incomplete. Please try again.',
  unknown_provider: 'This Single Sign-On provider is no longer available.',
  not_provisioned: 'No account exists for this identity. Contact an administrator.',
  user_disabled: 'Your account is disabled. Contact an administrator.',
  no_email: 'The identity provider did not return an email address.',
  token_exchange_failed: 'Could not contact the identity provider. Please try again.',
  userinfo_failed: 'Could not retrieve your profile from the identity provider.',
  no_subject: 'The identity provider did not return a stable user identifier.',
  access_denied: 'Single Sign-On request was cancelled.',
};

const schema = z.object({
  email: z.string().email('Invalid email address'),
  password: z.string().min(1, 'Password is required'),
});

function showBlockedModal() {
  modal.open({
    title: 'Account Locked',
    messageHtml: `<p class="text-sm text-space-indigo/70">Your account is locked. Please contact an administrator.</p>`,
    closable: false,
    buttons: [
      {
        label: 'Close',
        variant: 'secondary',
        action: () => {
          modal.close();
        },
      },
    ],
  });
}

function showExpirationModal(daysRemaining: number) {
  modal.open({
    title: 'Password Expiring Soon',
    messageHtml: `<p class="mb-3"><span class="font-semibold text-red-600">Warning!</span> Your password expires in <span class="font-bold">${daysRemaining} day${daysRemaining > 1 ? 's' : ''}</span>. It is strongly recommended to change it now to avoid being locked out.</p>`,
    closable: false,
    buttons: [
      {
        label: 'Later',
        variant: 'secondary',
        action: () => {
          modal.close();
          router.push('/');
        },
      },
      {
        label: 'Change Now',
        variant: 'reveals-primary',
        action: () => {
          modal.close();
          router.push('/profile');
        },
      },
    ],
  });
}

async function handleLogin() {
  errors.value = {};
  loginError.value = '';
  
  const result = schema.safeParse({ email: email.value, password: password.value });
  
  if (!result.success) {
    const formattedErrors = result.error.format();
    errors.value = {
      email: formattedErrors.email?._errors[0],
      password: formattedErrors.password?._errors[0],
    };
    return;
  }

  loading.value = true;
  try {
    const params = new URLSearchParams();
    params.append('username', email.value);
    params.append('password', password.value);

    const response = await api.post<LoginResponse>('/auth/login', params, {
      headers: { 'Content-Type': 'application/x-www-form-urlencoded' },
    });

    const data = response.data;

    if (data.is_blocked) {
      showBlockedModal();
      return;
    }

    console.log('Login response data:', data); // Debug log to inspect the response structure

    if (!data.user) {
      const remaining = data.remaining_attempts;
      if (remaining !== null && remaining <= 3 && remaining > 0) {
        warning(`Warning, you have only ${remaining} attempt${remaining > 1 ? 's' : ''} left before lockout.`, 6000);
      }
      loginError.value = 'Incorrect email or password';
      return;
    }

    await authStore.fetchUser();

    const daysRemaining = data.password_days_remaining;
    const mustChange = data.must_change_password;

    if (mustChange) {
      router.push('/change-password');
      return;
    }

    if (daysRemaining !== null && daysRemaining <= 5) {
      showExpirationModal(daysRemaining);
      return;
    }

    if (daysRemaining !== null && daysRemaining <= 15) {
      warning(`Your password expires in ${daysRemaining} day${daysRemaining > 1 ? 's' : ''}. Consider changing it soon.`, 6000);
    }

    router.push('/');
  } catch (e: any) {
    if (e?.response?.status === 403 && typeof e?.response?.data?.detail === 'string') {
      loginError.value = e.response.data.detail;
    } else {
      loginError.value = 'Error during login';
    }
  } finally {
    loading.value = false;
  }
}

function handleSSOLogin(providerName: string) {
  ssoService.loginWith(providerName);
}

async function loadSSOConfig() {
  try {
    const { data } = await ssoService.getPublicConfig();
    ssoEnabled.value = data.sso_enabled;
    passwordLoginEnabled.value = data.password_login_enabled;
    ssoProviders.value = data.providers || [];
  } catch (err) {
    console.warn('Could not load SSO config', err);
  }
}

async function loadAppSettings() {
  try {
    const { data } = await settingsService.getAppName();
    appName.value = data.value;
  } catch (err) {
    console.error('Error loading app name', err);
  }

  try {
    const { data } = await settingsService.getAppIcon();
    if (data.value) {
      appIconSvg.value = data.value;
    }
  } catch (err) {
    console.warn('No icon configured, using default', err);
  }
}

function checkSSOErrorParam() {
  const ssoErr = route.query.sso_error;
  if (typeof ssoErr === 'string' && ssoErr.length > 0) {
    const message = SSO_ERROR_MESSAGES[ssoErr] || `Single Sign-On error: ${ssoErr}`;
    toastError(message, 8000);
    // Clean the URL
    router.replace({ path: route.path, query: {} });
  }
}

onMounted(() => {
  loadAppSettings();
  loadSSOConfig();
  checkSSOErrorParam();
});
</script>

<template>
  <div class="min-h-screen flex bg-white">
    <!-- Left section - Branding -->
    <div class="hidden lg:flex lg:w-1/2 bg-gradient-to-br from-space-indigo via-space-indigo to-space-indigo-700 text-white flex-col items-center justify-center p-12 relative overflow-hidden">
      <div class="absolute top-10 right-10 w-40 h-40 bg-tropical-mint opacity-10 rounded-full blur-3xl"></div>
      <div class="absolute bottom-20 left-20 w-56 h-56 bg-ocean-mist opacity-10 rounded-full blur-3xl"></div>

      <div class="relative z-10 text-center">
        <div class="mb-8 flex justify-center">
          <div v-if="appIconSvg" class="w-20 h-20 filter drop-shadow-lg [&_svg]:w-20 [&_svg]:h-20" v-html="appIconSvg"></div>
          <img v-else :src="logoImage" alt="App Logo" class="h-20 w-auto drop-shadow-lg" />
        </div>

        <h1 class="text-5xl font-bold mb-4 bg-gradient-brand bg-clip-text text-transparent">{{ appName }}</h1>

        <p class="text-xl text-white/80 mb-12 max-w-md mx-auto leading-relaxed">
          Welcome to your workspace
        </p>
      </div>

      <div class="absolute bottom-8 left-0 right-0 text-center z-10">
        <p class="text-sm text-white/60">Powered by</p>
        <p class="text-base font-semibold text-white/80">Reveals</p>
        <p class="text-xs text-white/40 mt-4">© 2026 All rights reserved</p>
      </div>
    </div>

    <!-- Right section - Login Form -->
    <div class="w-full lg:w-1/2 flex items-center justify-center p-6 sm:p-12">
      <div class="w-full max-w-md">
        <!-- Mobile header -->
        <div class="lg:hidden mb-8 text-center">
          <div v-if="appIconSvg" class="w-12 h-12 filter drop-shadow-lg mx-auto mb-4 [&_svg]:w-12 [&_svg]:h-12" v-html="appIconSvg"></div>
          <img v-else :src="logoImage" alt="App Logo" class="h-12 w-auto mx-auto mb-4" />
          <h2 class="text-2xl font-bold text-space-indigo">Welcome back</h2>
          <p class="text-space-indigo/60 text-sm mt-2">Sign in to your account to continue</p>
        </div>

        <!-- Form header -->
        <div class="hidden lg:block mb-8">
          <h2 class="text-2xl font-bold text-space-indigo mb-2">Welcome Back</h2>
          <p class="text-space-indigo/60 text-sm">
            {{ showPasswordForm ? 'Enter your credentials to access your account' : 'Sign in with your Single Sign-On provider' }}
          </p>
        </div>

        <!-- Login form (hidden when SSO is forced) -->
        <form v-if="showPasswordForm" class="space-y-6" @submit.prevent="handleLogin">
          <div>
            <Input
              id="email"
              label="Email Address"
              placeholder="you@example.com"
              v-model="email"
              :error="errors.email"
              theme="reveals"
            />
          </div>

          <div>
            <Input
              id="password"
              type="password"
              label="Password"
              placeholder="••••••••"
              v-model="password"
              :error="errors.password"
              theme="reveals"
            />
          </div>

          <p v-if="loginError" class="text-sm text-red-600 bg-red-50 px-4 py-3 rounded-lg text-center">
            {{ loginError }}
          </p>

          <div class="pt-2">
            <Button
              type="submit"
              class="w-full"
              :disabled="loading"
              variant="reveals-primary"
            >
              {{ loading ? 'Signing in...' : 'Sign in' }}
            </Button>
          </div>
        </form>

        <!-- Separator -->
        <div v-if="showSeparator" class="my-6 flex items-center gap-3">
          <div class="flex-1 h-px bg-space-indigo/15"></div>
          <span class="text-xs uppercase tracking-wider text-space-indigo/50">or continue with</span>
          <div class="flex-1 h-px bg-space-indigo/15"></div>
        </div>

        <!-- SSO buttons -->
        <div v-if="showSSOButtons" class="space-y-3" :class="{ 'mt-2': !showPasswordForm }">
          <button
            v-for="provider in ssoProviders"
            :key="provider.name"
            type="button"
            class="w-full inline-flex items-center justify-center gap-2 px-4 py-2.5 rounded border border-space-indigo/20 bg-white text-space-indigo font-medium text-sm hover:bg-space-indigo/5 focus:outline-none focus:ring-2 focus:ring-tropical-mint transition-all duration-200"
            :style="provider.button_color ? { borderColor: provider.button_color } : {}"
            @click="handleSSOLogin(provider.name)"
          >
            <span v-if="provider.icon" class="w-5 h-5 inline-flex items-center justify-center [&_svg]:w-5 [&_svg]:h-5" v-html="provider.icon"></span>
            <span>Continue with {{ provider.display_name }}</span>
          </button>
        </div>

        <p v-if="!showPasswordForm && !showSSOButtons" class="text-sm text-red-600 bg-red-50 px-4 py-3 rounded-lg text-center">
          No login method is currently available. Please contact an administrator.
        </p>

        <!-- Footer text -->
        <div class="mt-8 text-center text-sm text-space-indigo/60">
          <p>Protected by enterprise security</p>
        </div>
      </div>
    </div>
  </div>
</template>
