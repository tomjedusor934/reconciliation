<script setup lang="ts">
import { ref } from 'vue';
import { useRouter } from 'vue-router';
import { useAuthStore } from '@/stores/auth';
import Card from '@/components/ui/Card.vue';
import Button from '@/components/ui/Button.vue';
import ChangePasswordFields from '@/components/block/ChangePasswordFields.vue';
import userService from '@/services/userService';
import { success, error } from '@/utils/toaster';

const router = useRouter();
const authStore = useAuthStore();

const submitting = ref(false);
const pwdFields = ref<InstanceType<typeof ChangePasswordFields> | null>(null);

async function handleSubmit() {
  if (!pwdFields.value) return;

  const validation = pwdFields.value.validate();
  if (!validation.valid) {
    error(validation.error || 'Invalid password');
    return;
  }

  submitting.value = true;
  try {
    const pwdData = pwdFields.value.getData();
    await userService.updateMe(pwdData);

    // Reset expiration flags
    authStore.mustChangePassword = false;
    authStore.passwordDaysRemaining = null;

    success('Password changed successfully');
    router.push('/');
  } catch (err: any) {
    if (err.response?.status === 400) {
      error(err.response?.data?.detail || 'Invalid current password');
    } else {
      error('Failed to change password');
    }
  } finally {
    submitting.value = false;
  }
}

async function handleCancel() {
  await authStore.logout();
}
</script>

<template>
  <div class="min-h-screen bg-gradient-to-br from-lavender-blush via-white to-lavender-blush">
    <!-- Decorative elements -->
    <div class="fixed top-20 right-20 w-72 h-72 bg-tropical-mint opacity-5 rounded-full blur-3xl"></div>
    <div class="fixed bottom-40 left-10 w-64 h-64 bg-ocean-mist opacity-5 rounded-full blur-3xl"></div>

    <div class="relative z-10 flex items-center justify-center min-h-screen p-4">
      <div class="w-full max-w-lg">
        <!-- Header section -->
        <div class="text-center mb-8">
          <div class="flex justify-center mb-4">
            <div class="w-16 h-16 bg-red-50 border border-red-500 rounded-full flex items-center justify-center">
              <svg class="w-8 h-8 text-red-500" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M12 15v2m-6 4h12a2 2 0 002-2v-6a2 2 0 00-2-2H6a2 2 0 00-2 2v6a2 2 0 002 2zm10-10V7a4 4 0 00-8 0v4h8z" />
              </svg>
            </div>
          </div>
          <h1 class="text-3xl font-bold text-space-indigo mb-2">Update Your Password</h1>
          <p class="text-space-indigo/60 text-base">
            Your password has expired. Please create a new one to continue.
          </p>
        </div>

        <!-- Card with form -->
        <Card class="shadow-soft">
          <!-- Info banner -->
          <div class="mb-6 p-4 bg-red-50 border border-red-500 rounded-lg">
            <div class="flex gap-3">
              <div class="flex-shrink-0">
                <svg class="w-5 h-5 text-red-500" fill="currentColor" viewBox="0 0 20 20">
                  <path fill-rule="evenodd" d="M8.257 3.099c.765-1.36 2.722-1.36 3.486 0l5.58 9.92c.75 1.334-.213 2.98-1.742 2.98H4.42c-1.53 0-2.493-1.646-1.743-2.98l5.58-9.92zM11 13a1 1 0 11-2 0 1 1 0 012 0zm-1-8a1 1 0 00-1 1v3a1 1 0 002 0V6a1 1 0 00-1-1z" clip-rule="evenodd" />
                </svg>
              </div>
              <div>
                <p class="text-sm font-medium text-red-500">Action Required</p>
                <p class="text-xs text-red-500 mt-1">You must update your password to access your account</p>
              </div>
            </div>
          </div>

          <!-- Form -->
          <form @submit.prevent="handleSubmit" class="space-y-6">
            <ChangePasswordFields ref="pwdFields" />
            
            <!-- Action buttons -->
            <div class="flex justify-end gap-3 pt-4 border-t border-lavender-blush">
              <Button type="button" @click="handleCancel" variant="secondary">
                Cancel & Logout
              </Button>
              <Button type="submit" :disabled="submitting" variant="reveals-primary">
                {{ submitting ? 'Updating...' : 'Update Password' }}
              </Button>
            </div>
          </form>
        </Card>

        <!-- Footer text -->
        <div class="text-center mt-6">
          <p class="text-xs text-space-indigo/40">
            Your password must meet the security requirements shown above
          </p>
        </div>
      </div>
    </div>
  </div>
</template>
