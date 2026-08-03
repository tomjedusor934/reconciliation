<script setup lang="ts">
import { ref, onMounted } from 'vue';
import { useRouter } from 'vue-router';
import { useAuthStore } from '@/stores/auth';
import Card from '@/components/ui/Card.vue';
import Button from '@/components/ui/Button.vue';
import Input from '@/components/ui/Input.vue';
import Loader from '@/components/ui/Loader.vue';
import ChangePasswordFields from '@/components/block/ChangePasswordFields.vue';
import userService from '@/services/userService';
import toaster from '@/utils/toaster';

const router = useRouter();
const authStore = useAuthStore();

const loading = ref(false);
const submitting = ref(false);
const showPasswordField = ref(false);
const pwdFields = ref<InstanceType<typeof ChangePasswordFields> | null>(null);

const form = ref({
  email: '',
  full_name: '',
});

const fetchData = async () => {
  loading.value = true;
  try {
    if (authStore.user) {
      form.value = {
        email: authStore.user.email,
        full_name: authStore.user.full_name,
      };
    }
  } catch (error) {
    console.error(error);
    toaster.error('Failed to load profile');
  } finally {
    loading.value = false;
  }
};

const handleSubmit = async () => {
  if (!form.value.email) {
    toaster.error('Email is required');
    return;
  }

  const payload: any = {
    email: form.value.email,
    full_name: form.value.full_name,
  };

  // Validate password fields if the section is open
  if (showPasswordField.value && pwdFields.value) {
    const validation = pwdFields.value.validate();
    if (!validation.valid) {
      toaster.error(validation.error || 'Invalid password');
      return;
    }
    const pwdData = pwdFields.value.getData();
    payload.password = pwdData.password;
    payload.current_password = pwdData.current_password;
  }

  submitting.value = true;
  try {
    if (authStore.user) {
      await userService.updateMe(payload);
      await authStore.fetchUser();
      toaster.success('Profile updated successfully');
      router.push('/');
    }
  } catch (error: any) {
    console.error(error);
    if (error.response?.status === 400) {
      toaster.error(error.response?.data?.detail || 'Invalid current password');
    } else {
      toaster.error('Failed to save profile');
    }
  } finally {
    submitting.value = false;
  }
};

onMounted(() => fetchData());
</script>

<template>
    <div class="mb-6">
      <h1 class="text-2xl font-bold">Edit Profile</h1>
      <p class="text-space-indigo/60">Manage your account settings</p>
    </div>
    <Card title="Personal Information">
        <Loader v-if="loading" />
        <form v-else @submit.prevent="handleSubmit" class="space-y-4">
        <Input 
          v-model="form.email" 
          label="Email" 
          type="email"
          required 
          theme="reveals"
        />
        <Input 
          v-model="form.full_name" 
          label="Full Name" 
          theme="reveals"
        />

            <div>
                <Button
                    v-if="!showPasswordField"
                    type="button"
                    variant="ghost"
                    size="sm"
                    @click="showPasswordField = true"
                >
                    Change Password
                </Button>
                <ChangePasswordFields v-if="showPasswordField" ref="pwdFields" />
            </div>

            <div class="flex justify-end gap-3 pt-4">
            <Button 
                type="button"
                variant="secondary" 
                @click="router.back()"
            >
                Cancel
            </Button>
            <Button 
                type="submit"
                :disabled="submitting"
                variant="reveals-primary"
            >
                {{ submitting ? 'Saving...' : 'Save Changes' }}
            </Button>
            </div>
        </form>
    </Card>
</template>
