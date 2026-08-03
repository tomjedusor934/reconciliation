<script setup lang="ts">
import { ref } from 'vue';
import Input from '../ui/Input.vue';
import PasswordField from './PasswordField.vue';

const currentPassword = ref('');
const newPassword = ref('');
const confirmPassword = ref('');
const passwordMeetsCriteria = ref(false);

function validate(): { valid: boolean; error?: string } {
  if (!currentPassword.value) {
    return { valid: false, error: 'Current password is required' };
  }
  if (!newPassword.value) {
    return { valid: false, error: 'New password is required' };
  }
  if (!passwordMeetsCriteria.value) {
    return { valid: false, error: 'Password does not meet the required criteria' };
  }
  if (newPassword.value !== confirmPassword.value) {
    return { valid: false, error: 'Passwords do not match' };
  }
  if (newPassword.value === currentPassword.value) {
    return { valid: false, error: 'New password cannot be the same as the current password' };
  }
  return { valid: true };
}

function getData(): { current_password: string; password: string } {
  return {
    current_password: currentPassword.value,
    password: newPassword.value,
  };
}

function reset() {
  currentPassword.value = '';
  newPassword.value = '';
  confirmPassword.value = '';
  passwordMeetsCriteria.value = false;
}

defineExpose({ validate, getData, reset });
</script>

<template>
  <div class="space-y-4">
    <Input
      v-model="currentPassword"
      label="Current Password"
      type="password"
      placeholder="Current password"
      theme="reveals"
    />
    <PasswordField
      v-model="newPassword"
      label="New Password"
      :is-edit-mode="false"
      @validation-change="passwordMeetsCriteria = $event"
    />
    <Input
      v-model="confirmPassword"
      label="Confirm New Password"
      type="password"
      placeholder="Confirm new password"
      theme="reveals"
    />
  </div>
</template>
